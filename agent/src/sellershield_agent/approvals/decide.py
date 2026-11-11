"""Seller approval decision orchestrator bound to store-verified seller credentials."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import Engine

from sellershield_agent.approvals.approve import execute_approve_decision
from sellershield_agent.approvals.common import enforce_role_and_step_up, seller_actor_key
from sellershield_agent.approvals.common_db import (
    append_audit_sync,
    check_idempotency_sync,
    load_approval_for_decision_sync,
)
from sellershield_agent.approvals.expire import expire_single_approval
from sellershield_agent.approvals.reject import execute_reject_decision
from sellershield_agent.approvals.withdraw import withdraw_customer_approval
from sellershield_agent.audit.models import ActorType, EventType, Outcome
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine
from sellershield_agent.domain.state import ApprovalStatus, is_approval_expired
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.proposals.integrity import compute_request_hash
from sellershield_agent.store.client import RequestContext, StoreClient, StoreClientError

__all__ = ["decide_approval", "withdraw_customer_approval"]


async def decide_approval(
    seller_ctx: RequestContext,
    approval_id: str,
    decision: str,
    reason: str | None,
    expected_version: int,
    *,
    idempotency_key: str,
    clock: Clock,
    store_client: StoreClient,
    engine: Engine | None = None,
    request_path: str | None = None,
) -> dict[str, Any]:
    """Execute a seller approve or reject decision bound to live store revalidation."""
    del request_path
    resolved_engine = engine or get_engine()
    actor_id = seller_ctx.principal.user_id
    caller_store_id = seller_ctx.principal.store_id

    apr_row, prop_row, fallback_risk, prev_refunds, snapshot = await asyncio.to_thread(
        load_approval_for_decision_sync,
        resolved_engine,
        approval_id,
    )
    if apr_row is None or prop_row is None:
        await asyncio.to_thread(
            append_audit_sync,
            resolved_engine,
            clock=clock,
            request_id=seller_ctx.request_id,
            session_id=None,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.AUTH_DENIED,
            resource_type="approvals",
            resource_id=approval_id,
            action="approval_not_found",
            outcome=Outcome.DENIED,
            policy_version=None,
            details={"reason": "not_found", "store_id": caller_store_id},
        )
        raise AppHTTPException(
            status_code=404,
            code="not_found",
            message="Approval not found",
            details={},
        )

    row_store_id = str(getattr(apr_row, "store_id", None) or "nestkart")
    if row_store_id != caller_store_id:
        await asyncio.to_thread(
            append_audit_sync,
            resolved_engine,
            clock=clock,
            request_id=seller_ctx.request_id,
            session_id=str(prop_row.session_id),
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.AUTH_DENIED,
            resource_type="approvals",
            resource_id=approval_id,
            action="store_scope_denied",
            outcome=Outcome.DENIED,
            policy_version=str(apr_row.policy_version),
            details={"reason": "store_mismatch"},
        )
        raise AppHTTPException(
            status_code=404,
            code="not_found",
            message="Approval not found",
            details={},
        )

    session_id = str(prop_row.session_id)
    policy_version = str(apr_row.policy_version)
    amount_paise = int(apr_row.amount_paise)
    required_role = str(apr_row.required_role)

    # Check authorization (role/limit + step-up) BEFORE idempotency replay so stale or
    # lower-privileged credentials cannot bypass role/step-up rules.
    await enforce_role_and_step_up(
        engine=resolved_engine,
        seller_ctx=seller_ctx,
        approval_id=approval_id,
        amount_paise=amount_paise,
        required_role=required_role,
        policy_version=policy_version,
        session_id=session_id,
        clock=clock,
    )

    actor_key = seller_actor_key(caller_store_id, actor_id)
    req_hash = compute_request_hash(
        operation="seller_decision",
        proposal_id=approval_id,
        canonical_body={
            "decision": decision,
            "reason": reason,
            "expected_version": expected_version,
        },
    )
    replay = await asyncio.to_thread(
        check_idempotency_sync,
        resolved_engine,
        actor_key=actor_key,
        idempotency_key=idempotency_key,
        request_hash=req_hash,
    )
    if replay is not None:
        return replay

    curr_status = str(apr_row.status)
    if decision == "approve" and curr_status != ApprovalStatus.pending.value:
        await asyncio.to_thread(
            append_audit_sync,
            resolved_engine,
            clock=clock,
            request_id=seller_ctx.request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.APPROVAL_DECIDED,
            resource_type="approvals",
            resource_id=approval_id,
            action="approve",
            outcome=Outcome.DENIED,
            policy_version=policy_version,
            details={"reason": "invalid_state", "current_status": curr_status},
        )
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message=f"Approval cannot be approved in status '{curr_status}'",
        )

    if decision == "reject" and curr_status not in (
        ApprovalStatus.pending.value,
        ApprovalStatus.needs_revalidation.value,
    ):
        await asyncio.to_thread(
            append_audit_sync,
            resolved_engine,
            clock=clock,
            request_id=seller_ctx.request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.APPROVAL_DECIDED,
            resource_type="approvals",
            resource_id=approval_id,
            action="reject",
            outcome=Outcome.DENIED,
            policy_version=policy_version,
            details={"reason": "invalid_state", "current_status": curr_status},
        )
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message=f"Approval cannot be rejected in status '{curr_status}'",
        )

    if int(apr_row.version) != expected_version:
        await asyncio.to_thread(
            append_audit_sync,
            resolved_engine,
            clock=clock,
            request_id=seller_ctx.request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.APPROVAL_DECIDED,
            resource_type="approvals",
            resource_id=approval_id,
            action=decision,
            outcome=Outcome.DENIED,
            policy_version=policy_version,
            details={
                "reason": "version_mismatch",
                "expected_version": expected_version,
                "actual_version": int(apr_row.version),
            },
        )
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message="Approval version mismatch; refresh the approval card and retry",
        )

    customer_id = str(prop_row.customer_id)
    if is_approval_expired(str(apr_row.expires_at), clock):
        apr_dict = dict(apr_row._mapping)
        apr_dict["customer_id"] = customer_id
        try:
            await expire_single_approval(
                engine=resolved_engine,
                store_client=store_client,
                apr_item=apr_dict,
                clock=clock,
                request_id=seller_ctx.request_id,
                actor_id="reconciler",
            )
        except (StoreClientError, TimeoutError):
            pass
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message="Approval request has expired and cannot be decided",
        )

    proposal_id = str(apr_row.proposal_id)
    order_id = str(apr_row.order_id)
    return_id = str(apr_row.return_id) if apr_row.return_id else None
    refund_id = str(apr_row.refund_id) if apr_row.refund_id else None

    # Forward only the verified seller's RequestContext into StoreClient write calls.
    exec_seller_ctx = RequestContext(
        principal=seller_ctx.principal,
        request_id=seller_ctx.request_id,
        token=seller_ctx.token,
    )

    if decision == "reject":
        return await execute_reject_decision(
            engine=resolved_engine,
            store_client=store_client,
            clock=clock,
            seller_ctx=seller_ctx,
            exec_seller_ctx=exec_seller_ctx,
            approval_id=approval_id,
            proposal_id=proposal_id,
            order_id=order_id,
            customer_id=customer_id,
            return_id=return_id,
            refund_id=refund_id,
            expected_version=expected_version,
            reason=reason,
            policy_version=policy_version,
            session_id=session_id,
            caller_store_id=caller_store_id,
            actor_key=actor_key,
            idempotency_key=idempotency_key,
            req_hash=req_hash,
        )

    assert fallback_risk is not None
    return await execute_approve_decision(
        engine=resolved_engine,
        store_client=store_client,
        clock=clock,
        seller_ctx=seller_ctx,
        exec_seller_ctx=exec_seller_ctx,
        apr_row=apr_row,
        prop_row=prop_row,
        fallback_risk=fallback_risk,
        prev_refunds=prev_refunds,
        snapshot=snapshot,
        expected_version=expected_version,
        reason=reason,
        caller_store_id=caller_store_id,
        actor_key=actor_key,
        idempotency_key=idempotency_key,
        req_hash=req_hash,
    )
