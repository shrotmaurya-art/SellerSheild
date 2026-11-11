"""Seller approval execution with live fact/policy revalidation and seller JWT."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import Engine

from sellershield_agent.approvals.card import build_approval_card
from sellershield_agent.approvals.common import raise_mapped_store_error
from sellershield_agent.approvals.common_db import append_audit_sync, save_idempotency_sync
from sellershield_agent.approvals.decide_db import (
    claim_approving_sync,
    finalize_approved_sync,
    record_revalidation_needed_sync,
    rollback_approving_to_pending_sync,
)
from sellershield_agent.approvals.helpers import (
    extract_items_and_pairs,
    fetch_live_store_bundle,
    parse_json_dict,
    sanitize_seller_note,
)
from sellershield_agent.approvals.revalidation import (
    build_decision_facts_from_bundle,
    compute_revalidation_diff,
)
from sellershield_agent.audit.models import ActorType, EventType, Outcome
from sellershield_agent.clock import Clock
from sellershield_agent.domain.models import CustomerRiskFacts, RefundStatus, ReturnStatus
from sellershield_agent.domain.state import InvalidTransition, VersionMismatch
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.store.client import RequestContext, StoreClient, StoreClientError


async def execute_approve_decision(
    *,
    engine: Engine,
    store_client: StoreClient,
    clock: Clock,
    seller_ctx: RequestContext,
    exec_seller_ctx: RequestContext,
    apr_row: Any,
    prop_row: Any,
    fallback_risk: CustomerRiskFacts,
    prev_refunds: int,
    snapshot: dict[str, Any] | None,
    expected_version: int,
    reason: str | None,
    caller_store_id: str,
    actor_key: str,
    idempotency_key: str,
    req_hash: str,
) -> dict[str, Any]:
    """Revalidate live facts/policy, authorize & approve with seller JWT, and finalize DB."""
    actor_id = seller_ctx.principal.user_id
    approval_id = str(apr_row.approval_id)
    proposal_id = str(apr_row.proposal_id)
    order_id = str(apr_row.order_id)
    customer_id = str(prop_row.customer_id)
    return_id = str(apr_row.return_id) if apr_row.return_id else None
    refund_id = str(apr_row.refund_id) if apr_row.refund_id else None
    session_id = str(prop_row.session_id)
    policy_version = str(apr_row.policy_version)
    amount_paise = int(apr_row.amount_paise)

    payload = parse_json_dict(prop_row.payload_json)
    amounts = parse_json_dict(prop_row.amounts_json)
    proposal_type = str(prop_row.type)
    proposal_total_paise = int(amounts.get("total_paise", amount_paise))
    req_items, req_pairs = extract_items_and_pairs(payload)
    raw_ev = payload.get("evidence_ids")
    evidence_ids = (
        [str(e) for e in raw_ev if isinstance(e, str)] if isinstance(raw_ev, list) else []
    )

    svc_ctx = build_service_ctx(clock, seller_ctx.request_id)
    try:
        bundle = await fetch_live_store_bundle(
            store_client=store_client,
            svc_ctx=svc_ctx,
            order_id=order_id,
            customer_id=customer_id,
            req_pairs=req_pairs,
            return_id=return_id,
            refund_id=refund_id,
            allow_missing_order=True,
        )
    except (StoreClientError, TimeoutError) as exc:
        await asyncio.to_thread(
            append_audit_sync,
            engine,
            clock=clock,
            request_id=seller_ctx.request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.APPROVAL_DECIDED,
            resource_type="approvals",
            resource_id=approval_id,
            action="approve",
            outcome=Outcome.FAILED,
            policy_version=policy_version,
            details={"reason": "store_revalidation_error", "error": type(exc).__name__},
        )
        raise_mapped_store_error(exc)

    live_facts = build_decision_facts_from_bundle(
        proposal_type=proposal_type,
        customer_id=customer_id,
        requested_items=req_items,
        reason_code=str(payload.get("reason_code", "changed_mind")),
        resolution=str(payload.get("resolution", "refund")),
        refund_method=str(payload.get("refund_method", "original")),
        evidence_ids=evidence_ids,
        previous_refunds_30d_paise=prev_refunds,
        fallback_risk=fallback_risk,
        bundle=bundle,
        clock=clock,
    )
    existing_auth_ref = (
        str(apr_row.store_authorization_ref) if apr_row.store_authorization_ref else None
    )
    diff, _, _ = compute_revalidation_diff(
        stored_snapshot=snapshot,
        live_facts=live_facts,
        customer_id=customer_id,
        proposal_type=proposal_type,
        approval_policy_version=policy_version,
        approval_amount_paise=amount_paise,
        proposal_total_paise=proposal_total_paise,
        return_id=return_id,
        refund_id=refund_id,
        store_auth_ref=existing_auth_ref,
        bundle=bundle,
        caller_role=seller_ctx.principal.role.value,
        clock=clock,
    )
    if diff:
        try:
            await asyncio.to_thread(
                record_revalidation_needed_sync,
                engine,
                approval_id=approval_id,
                expected_version=expected_version,
                diff=diff,
                actor_id=actor_id,
                policy_version=policy_version,
                session_id=session_id,
                clock=clock,
                request_id=seller_ctx.request_id,
            )
        except (InvalidTransition, VersionMismatch):
            raise AppHTTPException(
                status_code=409,
                code="invalid_state",
                message="Approval state changed concurrently",
            ) from None
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message="Order facts or active policy changed since proposal; revalidation required",
            details={"status": "needs_revalidation", "diff": diff},
        )

    # Atomic claim pending -> approving before calling StoreClient (no open tx across I/O)
    try:
        approving_ver = await asyncio.to_thread(
            claim_approving_sync,
            engine,
            approval_id=approval_id,
            expected_version=expected_version,
            actor_id=actor_id,
            clock=clock,
            request_id=seller_ctx.request_id,
        )
    except (InvalidTransition, VersionMismatch):
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message="Approval version conflict or already claimed",
        ) from None

    clean_note = sanitize_seller_note(reason)
    reserved_amount = (
        int(bundle.own_refund.amount_paise) if bundle.own_refund is not None else amount_paise
    )
    obtained_auth_ref = existing_auth_ref
    already_authorized = bool(
        existing_auth_ref
        and bundle.own_refund is not None
        and bundle.own_refund.status == RefundStatus.authorized
        and bundle.own_refund.authorized_amount_paise == reserved_amount
    )

    try:
        if refund_id is not None and not already_authorized:
            auth_res = await store_client.authorize_refund(
                exec_seller_ctx,
                refund_id,
                reserved_amount,
                clean_note,
                f"{approval_id}:authorize_refund",
            )
            obtained_auth_ref = auth_res.gateway_ref or f"AUTH-{approval_id}-{refund_id}"

        if return_id is not None and not (
            bundle.own_return is not None and bundle.own_return.status == ReturnStatus.approved
        ):
            await store_client.transition_return(
                exec_seller_ctx,
                return_id,
                "approved",
                clean_note,
                f"{approval_id}:transition_return",
            )
    except (StoreClientError, TimeoutError) as exc:
        err_summary = f"{type(exc).__name__}: {exc}"[:300]
        await asyncio.to_thread(
            rollback_approving_to_pending_sync,
            engine,
            approval_id=approval_id,
            approving_version=approving_ver,
            actor_id=actor_id,
            error_summary=err_summary,
            store_auth_ref=obtained_auth_ref,
            policy_version=policy_version,
            session_id=session_id,
            clock=clock,
            request_id=seller_ctx.request_id,
        )
        raise_mapped_store_error(exc)

    final_auth_ref = obtained_auth_ref or f"AUTH-{approval_id}-{refund_id or order_id}"
    await asyncio.to_thread(
        finalize_approved_sync,
        engine,
        approval_id=approval_id,
        approving_version=approving_ver,
        proposal_id=proposal_id,
        order_id=order_id,
        customer_id=customer_id,
        return_id=return_id,
        refund_id=refund_id,
        amount_paise=reserved_amount,
        store_auth_ref=final_auth_ref,
        reason=clean_note,
        actor_id=actor_id,
        policy_version=policy_version,
        session_id=session_id,
        clock=clock,
        request_id=seller_ctx.request_id,
    )

    card = await build_approval_card(
        engine,
        store_client,
        clock,
        approval_id,
        store_id=caller_store_id,
        request_id=seller_ctx.request_id,
    )
    await asyncio.to_thread(
        save_idempotency_sync,
        engine,
        actor_key=actor_key,
        idempotency_key=idempotency_key,
        operation="seller_decision",
        target_id=approval_id,
        request_hash=req_hash,
        response_dict=card,
        clock=clock,
    )
    return card
