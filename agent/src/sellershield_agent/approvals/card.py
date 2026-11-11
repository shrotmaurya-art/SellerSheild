"""Deterministic builder for seller approval cards and queues."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from sqlalchemy import Engine

from sellershield_agent.approvals.card_db import (
    list_store_approval_ids_sync,
    load_approval_db_context_sync,
)
from sellershield_agent.approvals.helpers import (
    extract_items_and_pairs,
    fetch_live_store_bundle,
    parse_json_dict,
    sanitize_customer_statement,
    sanitize_order_for_card,
    sanitize_seller_note,
)
from sellershield_agent.approvals.models import (
    SellerApproval,
    SellerApprovalListResponse,
)
from sellershield_agent.approvals.revalidation import (
    build_decision_facts_from_bundle,
    compute_recommendation,
    compute_revalidation_diff,
    normalize_refund_proposal_decision,
)
from sellershield_agent.clock import Clock
from sellershield_agent.domain.state import ApprovalStatus
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.policy import loader as policy_loader
from sellershield_agent.policy.decide import Decision, decide
from sellershield_agent.store.client import (
    StoreClient,
    StoreClientError,
    StoreTimeout,
    StoreUnavailable,
)


async def build_approval_card(
    engine: Engine,
    store_client: StoreClient,
    clock: Clock,
    approval_id: str,
    *,
    store_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, PII-free, HTML-escaped seller approval card."""
    apr_row, prop_row, fallback_risk, prev_refunds, snapshot, counts = await asyncio.to_thread(
        load_approval_db_context_sync, engine, approval_id, store_id
    )

    payload = parse_json_dict(prop_row.payload_json)
    amounts = parse_json_dict(prop_row.amounts_json)
    proposal_type = str(prop_row.type)
    customer_id = str(prop_row.customer_id)
    order_id = str(apr_row.order_id)
    return_id = str(apr_row.return_id) if apr_row.return_id else None
    refund_id = str(apr_row.refund_id) if apr_row.refund_id else None
    policy_version = str(apr_row.policy_version)
    approval_status = str(apr_row.status)
    row_store_id = str(getattr(apr_row, "store_id", None) or "nestkart")

    requested_items, req_pairs = extract_items_and_pairs(payload)
    refund_method = str(payload.get("refund_method", "original"))
    resolution = str(payload.get("resolution", "refund"))
    reason_code = str(payload.get("reason_code", "")) or None
    raw_reason_text = payload.get("reason_text")
    raw_ev = payload.get("evidence_ids")
    evidence_ids = (
        [str(e) for e in raw_ev if isinstance(e, str)] if isinstance(raw_ev, list) else []
    )

    escaped_stmt, stmt_truncated = sanitize_customer_statement(
        raw_reason_text if isinstance(raw_reason_text, str) else None
    )

    svc_ctx = build_service_ctx(clock, request_id or f"req-card-{secrets.token_hex(6)}")
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
    except (StoreTimeout, StoreUnavailable):
        raise AppHTTPException(
            status_code=503,
            code="upstream_unavailable",
            message="Store service temporarily unavailable",
        ) from None
    except StoreClientError:
        raise AppHTTPException(
            status_code=502,
            code="upstream_unavailable",
            message="Store service returned an unexpected error",
        ) from None

    live_facts = build_decision_facts_from_bundle(
        proposal_type=proposal_type,
        customer_id=customer_id,
        requested_items=requested_items,
        reason_code=reason_code or "changed_mind",
        resolution=resolution,
        refund_method=refund_method,
        evidence_ids=evidence_ids,
        previous_refunds_30d_paise=prev_refunds,
        fallback_risk=fallback_risk,
        bundle=bundle,
        clock=clock,
    )

    try:
        card_policy = policy_loader.load_policy(policy_version)
    except (FileNotFoundError, ValueError):
        card_policy = policy_loader.load_policy()

    evaluated_decision: Decision | None = None
    if live_facts is not None:
        evaluated_decision = normalize_refund_proposal_decision(
            decide(live_facts, card_policy, clock.now()),
            proposal_type=proposal_type,
            policy=card_policy,
        )

    persisted_diff_raw = getattr(apr_row, "revalidation_diff_json", None)
    revalidation_diff: list[dict[str, Any]] | None = None
    if isinstance(persisted_diff_raw, str) and persisted_diff_raw.strip():
        try:
            loaded_diff = json.loads(persisted_diff_raw)
            if isinstance(loaded_diff, list):
                revalidation_diff = [d for d in loaded_diff if isinstance(d, dict)]
        except Exception:
            revalidation_diff = None

    facts_drifted = approval_status == ApprovalStatus.needs_revalidation.value
    proposal_total_paise = int(amounts.get("total_paise", int(apr_row.amount_paise)))
    if approval_status == ApprovalStatus.pending.value:
        live_diff, _, _ = compute_revalidation_diff(
            stored_snapshot=snapshot,
            live_facts=live_facts,
            customer_id=customer_id,
            proposal_type=proposal_type,
            approval_policy_version=policy_version,
            approval_amount_paise=int(apr_row.amount_paise),
            proposal_total_paise=proposal_total_paise,
            return_id=return_id,
            refund_id=refund_id,
            store_auth_ref=str(apr_row.store_authorization_ref)
            if apr_row.store_authorization_ref
            else None,
            bundle=bundle,
            caller_role=None,
            clock=clock,
        )
        if live_diff:
            facts_drifted = True
            if revalidation_diff is None:
                revalidation_diff = live_diff

    recommendation, rec_reason = compute_recommendation(
        proposal_type=proposal_type,
        approval_status=approval_status,
        decision=evaluated_decision,
        facts_drifted=facts_drifted,
    )

    if evaluated_decision is not None:
        rule_results_out: list[dict[str, Any]] = [
            {
                "id": r.id,
                "passed": bool(r.passed),
                "blocking": bool(r.blocking),
                "outcome_if_failed": r.outcome_if_failed.value if r.outcome_if_failed else None,
                "seller_text": r.seller_text,
                "facts": dict(r.facts),
            }
            for r in evaluated_decision.rule_results
        ]
        risk_out: dict[str, Any] = {
            "score": int(evaluated_decision.risk.score),
            "band": evaluated_decision.risk.band.value,
            "signals": list(evaluated_decision.risk.signals),
            "customer_risk": live_facts.customer_risk.model_dump(mode="json")
            if live_facts is not None
            else fallback_risk.model_dump(mode="json"),
        }
        risk_flags = list(evaluated_decision.risk.signals)
        computed_amount_out: dict[str, Any] = {
            "items_paise": int(evaluated_decision.amounts.items_paise),
            "shipping_refund_paise": int(evaluated_decision.amounts.shipping_refund_paise),
            "fee_paise": int(evaluated_decision.amounts.fee_paise),
            "total_paise": int(evaluated_decision.amounts.total_paise),
            "capped": bool(evaluated_decision.amounts.capped),
        }
    else:
        rule_results_out = []
        cust_risk_used = bundle.customer_risk or fallback_risk
        risk_out = {
            "score": 0,
            "band": "low",
            "signals": [],
            "customer_risk": cust_risk_used.model_dump(mode="json"),
        }
        risk_flags = []
        computed_amount_out = {
            "items_paise": int(amounts.get("items_paise", int(apr_row.amount_paise))),
            "shipping_refund_paise": int(amounts.get("shipping_refund_paise", 0)),
            "fee_paise": int(amounts.get("fee_paise", 0)),
            "total_paise": int(amounts.get("total_paise", int(apr_row.amount_paise))),
            "capped": False,
        }

    created_at_val = (
        getattr(apr_row, "created_at", None)
        or getattr(prop_row, "confirmed_at", None)
        or getattr(prop_row, "created_at", None)
        or apr_row.due_at
    )
    reason_for_card = escaped_stmt or (reason_code or "Seller approval requested")

    card_dict: dict[str, Any] = {
        "approval_id": str(apr_row.approval_id),
        "proposal_id": str(apr_row.proposal_id),
        "store_id": row_store_id,
        "customer_id": customer_id,
        "order_id": order_id,
        "return_id": return_id,
        "refund_id": refund_id,
        "requested_amount_paise": int(apr_row.amount_paise),
        "amount_paise": int(apr_row.amount_paise),
        "required_role": str(apr_row.required_role),
        "policy_version": policy_version,
        "reason": reason_for_card,
        "status": approval_status,
        "expected_version": int(apr_row.version),
        "risk_flags": risk_flags,
        "store_authorization_ref": str(apr_row.store_authorization_ref)
        if apr_row.store_authorization_ref
        else None,
        "created_at": str(created_at_val),
        "due_at": str(apr_row.due_at),
        "expires_at": str(apr_row.expires_at),
        "decided_at": str(apr_row.decided_at) if apr_row.decided_at else None,
        "decided_by": str(apr_row.decided_by) if apr_row.decided_by else None,
        "decision_reason": sanitize_seller_note(apr_row.decision_reason)
        if apr_row.decision_reason
        else None,
        "last_error": str(apr_row.last_error) if getattr(apr_row, "last_error", None) else None,
        "method": refund_method,
        "resolution": resolution,
        "reason_code": reason_code,
        "order": sanitize_order_for_card(bundle.order),
        "computed_amount": computed_amount_out,
        "rule_results": rule_results_out,
        "risk": risk_out,
        "unverified_customer_statement": {
            "label": "Unverified customer statement",
            "text": escaped_stmt,
            "truncated": stmt_truncated,
        },
        "evidence_ids": evidence_ids,
        "recommendation": recommendation,
        "recommendation_reason": rec_reason,
        "similar_past_decisions": counts,
        "revalidation_diff": revalidation_diff,
    }
    validated = SellerApproval.model_validate(card_dict)
    return validated.model_dump(mode="json")


async def list_seller_approvals_cards(
    *,
    engine: Engine,
    store_client: StoreClient,
    clock: Clock,
    store_id: str,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
    request_id: str | None = None,
) -> SellerApprovalListResponse:
    """List paginated seller approval cards scoped strictly to caller's store_id."""
    approval_ids, total = await asyncio.to_thread(
        list_store_approval_ids_sync,
        engine,
        store_id=store_id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )
    cards: list[SellerApproval] = []
    for aid in approval_ids:
        card_dict = await build_approval_card(
            engine,
            store_client,
            clock,
            aid,
            store_id=store_id,
            request_id=request_id,
        )
        cards.append(SellerApproval.model_validate(card_dict))

    return SellerApprovalListResponse(
        approvals=cards,
        total=total,
        limit=limit,
        offset=offset,
    )
