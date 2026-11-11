"""Pure deterministic pre-release safety gate checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sellershield_agent.approvals.helpers import LiveStoreBundle
from sellershield_agent.approvals.revalidation import build_decision_facts_from_bundle
from sellershield_agent.clock import Clock, FixedClock
from sellershield_agent.domain.models import (
    CustomerRefundSummary,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    ReturnStatus,
    StoreSettings,
)
from sellershield_agent.domain.state import ApprovalStatus
from sellershield_agent.execution.common import to_return_items
from sellershield_agent.execution.release_db import ReleaseContext
from sellershield_agent.policy import loader as policy_loader
from sellershield_agent.policy.decide import decide
from sellershield_agent.policy.rules import ApproverRole, DecisionOutcome


@dataclass(frozen=True)
class ReleaseSafetyError:
    """Structured denial reason when a refund release is unsafe."""

    reason: str
    details: dict[str, Any]


def verify_release_safety(
    *,
    rel_ctx: ReleaseContext,
    bundle: LiveStoreBundle,
    store_settings: StoreSettings,
    refund_summary: CustomerRefundSummary,
    clock: Clock,
) -> ReleaseSafetyError | None:
    """Enforce all deterministic safety gates before invoking StoreClient.release_refund."""
    plan = rel_ctx.plan
    ret_id = rel_ctx.return_id
    pid = plan.proposal_id

    if plan.proposal_type != "return":
        return ReleaseSafetyError(
            "proposal_type_not_return",
            {"proposal_id": pid, "proposal_type": plan.proposal_type},
        )
    if bundle.order is None:
        return ReleaseSafetyError(
            "order_not_found", {"proposal_id": pid, "order_id": plan.order_id}
        )
    if bundle.order.customer_id != plan.customer_id:
        return ReleaseSafetyError("order_customer_mismatch", {"proposal_id": pid})
    if bundle.order.status == OrderStatus.cancelled:
        return ReleaseSafetyError(
            "order_cancelled", {"proposal_id": pid, "order_id": plan.order_id}
        )
    if bundle.order.payment_status not in (
        PaymentStatus.paid,
        PaymentStatus.cod_collected,
        PaymentStatus.partially_refunded,
    ):
        return ReleaseSafetyError(
            "order_payment_status_ineligible",
            {"proposal_id": pid, "payment_status": bundle.order.payment_status.value},
        )

    if bundle.own_return is None:
        return ReleaseSafetyError("return_not_found", {"proposal_id": pid, "return_id": ret_id})
    if (
        bundle.own_return.customer_id != plan.customer_id
        or bundle.own_return.order_id != plan.order_id
    ):
        return ReleaseSafetyError(
            "return_owner_mismatch", {"proposal_id": pid, "return_id": ret_id}
        )
    if bundle.own_return.status != ReturnStatus.inspected_ok:
        return ReleaseSafetyError(
            "return_not_inspected_ok",
            {
                "proposal_id": pid,
                "return_id": ret_id,
                "return_status": bundle.own_return.status.value,
            },
        )

    rfd = bundle.own_refund
    if rfd is None:
        return ReleaseSafetyError(
            "refund_not_found", {"proposal_id": pid, "refund_id": rel_ctx.refund_id}
        )
    if rfd.customer_id != plan.customer_id or rfd.order_id != plan.order_id:
        return ReleaseSafetyError("refund_owner_mismatch", {"proposal_id": pid})
    if rfd.return_id is not None and ret_id is not None and rfd.return_id != ret_id:
        return ReleaseSafetyError("refund_return_mismatch", {"proposal_id": pid})
    if rfd.status in (RefundStatus.processing, RefundStatus.completed):
        return ReleaseSafetyError(
            "refund_already_processing_or_completed",
            {"proposal_id": pid, "refund_id": rfd.refund_id, "refund_status": rfd.status.value},
        )
    if rfd.status not in (RefundStatus.reserved, RefundStatus.authorized):
        return ReleaseSafetyError(
            "refund_not_releasable",
            {"proposal_id": pid, "refund_id": rfd.refund_id, "refund_status": rfd.status.value},
        )

    amt = int(rfd.amount_paise)
    if amt <= 0 or amt != int(plan.amount_paise):
        return ReleaseSafetyError(
            "refund_amount_mismatch_with_proposal",
            {"proposal_id": pid, "refund_amount": amt, "proposal_amount": int(plan.amount_paise)},
        )

    if (
        bundle.adjusted_refundable is None
        or amt > int(bundle.adjusted_refundable.max_refundable_paise)
    ):
        return ReleaseSafetyError(
            "exceeds_refundable_balance",
            {"proposal_id": pid, "refund_amount": amt},
        )

    needs_approval_path = plan.decision != "allow" or rel_ctx.approval is not None
    if needs_approval_path:
        apr = rel_ctx.approval
        if apr is None:
            return ReleaseSafetyError("approval_record_missing", {"proposal_id": pid})
        if str(apr.get("status", "")) != ApprovalStatus.approved.value:
            return ReleaseSafetyError(
                "approval_not_approved",
                {"proposal_id": pid, "approval_status": str(apr.get("status", ""))},
            )
        if not apr.get("store_authorization_ref"):
            return ReleaseSafetyError("missing_store_authorization_ref", {"proposal_id": pid})
        if rfd.status != RefundStatus.authorized:
            return ReleaseSafetyError(
                "refund_not_authorized_by_seller",
                {"proposal_id": pid, "refund_status": rfd.status.value},
            )
        if rfd.authorized_by is None or rfd.authorized_amount_paise is None:
            return ReleaseSafetyError("refund_missing_store_authorization", {"proposal_id": pid})
        if (
            amt > int(rfd.authorized_amount_paise)
            or amt != int(rfd.authorized_amount_paise)
            or int(apr["amount_paise"]) != amt
        ):
            return ReleaseSafetyError(
                "refund_amount_exceeds_authorized",
                {
                    "proposal_id": pid,
                    "refund_amount": amt,
                    "authorized_amount": int(rfd.authorized_amount_paise),
                    "approval_amount": int(apr["amount_paise"]),
                },
            )
    else:
        if rfd.authorized_amount_paise is not None and amt > int(rfd.authorized_amount_paise):
            return ReleaseSafetyError(
                "refund_amount_exceeds_authorized",
                {"proposal_id": pid, "refund_amount": amt},
            )
        if amt > int(store_settings.autonomy_cap_paise):
            return ReleaseSafetyError(
                "exceeds_store_autonomy_cap",
                {
                    "proposal_id": pid,
                    "refund_amount": amt,
                    "autonomy_cap": int(store_settings.autonomy_cap_paise),
                },
            )

    eval_now = min(bundle.own_return.created_at, clock.now())
    eval_clock = FixedClock(eval_now)
    effective_prev = max(
        rel_ctx.previous_refunds_30d_paise,
        int(refund_summary.total_refunded_paise),
        int(bundle.customer_risk.refunded_30d_paise) if bundle.customer_risk else 0,
    )
    live_facts = build_decision_facts_from_bundle(
        proposal_type=plan.proposal_type,
        customer_id=plan.customer_id,
        requested_items=to_return_items(plan.items),
        reason_code=plan.reason_code,
        resolution=plan.resolution,
        refund_method=plan.refund_method,
        evidence_ids=rel_ctx.evidence_ids,
        previous_refunds_30d_paise=effective_prev,
        fallback_risk=rel_ctx.fallback_risk,
        bundle=bundle,
        clock=eval_clock,
    )
    if live_facts is None:
        return ReleaseSafetyError("live_facts_unavailable", {"proposal_id": pid})

    active_policy = policy_loader.load_policy()
    prop_policy = policy_loader.load_policy(plan.policy_version)
    active_dec = decide(live_facts, active_policy, eval_now)
    prop_dec = decide(live_facts, prop_policy, eval_now)

    if active_dec.decision in (
        DecisionOutcome.deny,
        DecisionOutcome.escalate,
        DecisionOutcome.needs_info,
    ):
        return ReleaseSafetyError(
            "policy_denied_or_escalated",
            {"proposal_id": pid, "decision": active_dec.decision.value},
        )
    if prop_dec.decision in (
        DecisionOutcome.deny,
        DecisionOutcome.escalate,
        DecisionOutcome.needs_info,
    ):
        return ReleaseSafetyError(
            "proposal_policy_denied_or_escalated",
            {"proposal_id": pid, "decision": prop_dec.decision.value},
        )
    if int(active_dec.amounts.total_paise) < amt or int(prop_dec.amounts.total_paise) < amt:
        return ReleaseSafetyError(
            "policy_amount_reduced",
            {
                "proposal_id": pid,
                "refund_amount": amt,
                "active_total": int(active_dec.amounts.total_paise),
            },
        )
    if amt >= int(active_policy.hard_ceiling_paise):
        return ReleaseSafetyError("exceeds_hard_ceiling", {"proposal_id": pid})

    if not needs_approval_path:
        if (
            active_dec.decision != DecisionOutcome.allow
            or prop_dec.decision != DecisionOutcome.allow
        ):
            return ReleaseSafetyError(
                "policy_requires_seller_approval",
                {
                    "proposal_id": pid,
                    "active_decision": active_dec.decision.value,
                    "prop_decision": prop_dec.decision.value,
                },
            )
    elif rel_ctx.approval is not None:
        if (
            active_dec.required_approver_role == ApproverRole.seller_owner
            and str(rel_ctx.approval.get("required_role", "")) != ApproverRole.seller_owner.value
        ):
            return ReleaseSafetyError("policy_approver_tier_escalated", {"proposal_id": pid})

    return None
