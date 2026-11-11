"""Deterministic live-fact revalidation, policy comparison, and seller recommendations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sellershield_agent.approvals.helpers import LiveStoreBundle, is_default_pipeline_risk
from sellershield_agent.clock import Clock
from sellershield_agent.domain.models import (
    CustomerRiskFacts,
    RefundMethod,
    RefundStatus,
    ReturnItem,
    ReturnReasonCode,
    ReturnResolution,
    ReturnStatus,
)
from sellershield_agent.domain.state import ApprovalStatus, sanitize_policy_facts
from sellershield_agent.policy import loader as policy_loader
from sellershield_agent.policy.decide import Decision, decide
from sellershield_agent.policy.facts import DecisionAction, DecisionFacts
from sellershield_agent.policy.models import PolicyDocument
from sellershield_agent.policy.risk import RiskBand
from sellershield_agent.policy.rules import ApproverRole, DecisionOutcome
from sellershield_agent.proposals.revalidate import normalize_non_temporal_facts


def build_decision_facts_from_bundle(
    *,
    proposal_type: str,
    customer_id: str,
    requested_items: list[ReturnItem],
    reason_code: str,
    resolution: str,
    refund_method: str,
    evidence_ids: list[str],
    previous_refunds_30d_paise: int,
    fallback_risk: CustomerRiskFacts,
    bundle: LiveStoreBundle,
    clock: Clock,
) -> DecisionFacts | None:
    """Build live DecisionFacts from store bundle for deterministic decide()."""
    if bundle.order is None or bundle.adjusted_refundable is None:
        return None
    action = DecisionAction.refund_request if proposal_type == "refund" else DecisionAction.return_
    resolved_risk = bundle.customer_risk or fallback_risk
    resolved_prev = max(previous_refunds_30d_paise, resolved_risk.refunded_30d_paise)
    try:
        rm = RefundMethod(refund_method)
    except ValueError:
        rm = RefundMethod.original
    try:
        res = ReturnResolution(resolution)
    except ValueError:
        res = ReturnResolution.refund
    try:
        rc = (
            ReturnReasonCode.defective
            if proposal_type == "refund"
            else ReturnReasonCode(reason_code)
        )
    except ValueError:
        rc = ReturnReasonCode.changed_mind

    evidence_present = bool(evidence_ids) if proposal_type == "return" else False
    # Strip admin-populated nested returns/refunds so Order matches proposal get_order();
    # active returns and refundable balances travel separately via active_returns/refundable.
    clean_order = bundle.order.model_copy(update={"returns": [], "refunds": []})
    return DecisionFacts(
        action=action,
        order=clean_order,
        items=requested_items,
        reason_code=rc,
        reason_text=None,
        resolution=res,
        refund_method=rm,
        evidence_present=evidence_present,
        active_returns=bundle.other_active_returns,
        previous_refunds_30d_paise=resolved_prev,
        refundable=bundle.adjusted_refundable,
        customer_risk=resolved_risk,
        shipment=bundle.shipment,
        now=clock.now(),
    )


def normalize_refund_proposal_decision(
    decision: Decision,
    *,
    proposal_type: str,
    policy: PolicyDocument,
) -> Decision:
    """Apply refund-request approval requirement consistent with revalidate_against_store."""
    if proposal_type != "refund":
        return decision
    if decision.decision in (
        DecisionOutcome.deny,
        DecisionOutcome.needs_info,
        DecisionOutcome.escalate,
    ):
        return decision
    if (
        decision.decision == DecisionOutcome.needs_approval
        and decision.required_approver_role is not None
    ):
        return decision
    role = (
        ApproverRole.seller_owner
        if decision.amounts.total_paise > policy.staff_approval_limit_paise
        else ApproverRole.seller_staff
    )
    return decision.model_copy(
        update={
            "decision": DecisionOutcome.needs_approval,
            "required_approver_role": role,
        }
    )


def _diff_values(prefix: str, before: Any, after: Any, out: list[dict[str, Any]]) -> None:
    if before == after:
        return
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        all_keys = sorted(set(before.keys()) | set(after.keys()), key=str)
        for key in all_keys:
            k_str = str(key)
            path = f"{prefix}.{k_str}" if prefix else k_str
            if k_str not in before:
                out.append({"field": path, "before": None, "after": after[key]})
            elif k_str not in after:
                out.append({"field": path, "before": before[key], "after": None})
            else:
                _diff_values(path, before[key], after[key], out)
        return
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for idx, (b_item, a_item) in enumerate(zip(before, after, strict=True)):
            _diff_values(f"{prefix}[{idx}]", b_item, a_item, out)
        return
    out.append({"field": prefix, "before": before, "after": after})


def _normalize_facts_pair_for_diff(
    stored_snapshot: dict[str, Any],
    live_facts: DecisionFacts,
    *,
    customer_id: str,
    approval_policy: PolicyDocument,
    active_policy: PolicyDocument,
) -> tuple[dict[str, Any], dict[str, Any]]:
    stored_norm = normalize_non_temporal_facts(stored_snapshot)
    live_norm = normalize_non_temporal_facts(live_facts.model_dump(mode="json"))

    for norm_doc in (stored_norm, live_norm):
        order_doc = norm_doc.get("order")
        if isinstance(order_doc, dict):
            order_doc["returns"] = []
            order_doc["refunds"] = []

    # If proposal was built without service_ctx_factory (default risk profile) while live customer
    # risk is strictly older-account zero-activity, normalize benign account_age_days drift.
    stored_risk = stored_norm.get("customer_risk")
    live_risk = live_norm.get("customer_risk")
    max_age_thresh = max(
        approval_policy.risk.account_age_days_max,
        active_policy.risk.account_age_days_max,
    )
    if (
        isinstance(stored_risk, dict)
        and isinstance(live_risk, dict)
        and is_default_pipeline_risk(stored_risk, customer_id)
        and live_risk.get("customer_id") == customer_id
        and live_risk.get("returns_30d") == 0
        and live_risk.get("refund_ratio_90d_bp") == 0
        and live_risk.get("refunded_30d_paise") == 0
        and isinstance(live_risk.get("account_age_days"), int)
        and int(live_risk["account_age_days"]) > max_age_thresh
    ):
        stored_risk = {**stored_risk, "account_age_days": live_risk["account_age_days"]}
        stored_norm["customer_risk"] = stored_risk
        if (
            "previous_refunds_30d_paise" in stored_norm
            and "previous_refunds_30d_paise" in live_norm
        ):
            stored_norm["previous_refunds_30d_paise"] = live_norm["previous_refunds_30d_paise"]

    return stored_norm, live_norm


def compute_revalidation_diff(
    *,
    stored_snapshot: dict[str, Any] | None,
    live_facts: DecisionFacts | None,
    customer_id: str,
    proposal_type: str,
    approval_policy_version: str,
    approval_amount_paise: int,
    proposal_total_paise: int,
    return_id: str | None,
    refund_id: str | None,
    store_auth_ref: str | None,
    bundle: LiveStoreBundle,
    caller_role: str | None = None,
    clock: Clock,
) -> tuple[list[dict[str, Any]], Decision | None, PolicyDocument]:
    """Re-run decide() under approval & active policies; return non-empty sanitized diff."""
    diffs: list[dict[str, Any]] = []
    approval_policy = policy_loader.load_policy(approval_policy_version)
    active_policy = policy_loader.load_policy()

    if approval_amount_paise != proposal_total_paise:
        diffs.append(
            {
                "field": "amount_paise",
                "before": proposal_total_paise,
                "after": approval_amount_paise,
            }
        )

    if live_facts is None or bundle.order is None:
        diffs.append({"field": "order", "before": "present", "after": "missing"})
        return sanitize_policy_facts(diffs), None, active_policy

    if bundle.order.customer_id != customer_id:
        diffs.append({"field": "order.customer_id", "before": customer_id, "after": "mismatch"})

    orig_dec = normalize_refund_proposal_decision(
        decide(live_facts, approval_policy, clock.now()),
        proposal_type=proposal_type,
        policy=approval_policy,
    )
    active_dec = normalize_refund_proposal_decision(
        decide(live_facts, active_policy, clock.now()),
        proposal_type=proposal_type,
        policy=active_policy,
    )

    if stored_snapshot is not None:
        stored_norm, live_norm = _normalize_facts_pair_for_diff(
            stored_snapshot,
            live_facts,
            customer_id=customer_id,
            approval_policy=approval_policy,
            active_policy=active_policy,
        )
        _diff_values("", stored_norm, live_norm, diffs)

    if active_dec.amounts.total_paise != approval_amount_paise:
        diffs.append(
            {
                "field": "computed_amount.total_paise",
                "before": approval_amount_paise,
                "after": active_dec.amounts.total_paise,
            }
        )

    if return_id is not None:
        if bundle.own_return is None:
            diffs.append({"field": "return.status", "before": "requested", "after": "missing"})
        elif bundle.own_return.status not in (ReturnStatus.requested, ReturnStatus.approved):
            diffs.append(
                {
                    "field": "return.status",
                    "before": "requested",
                    "after": bundle.own_return.status.value,
                }
            )
        elif bundle.own_return.status == ReturnStatus.approved and not store_auth_ref:
            diffs.append(
                {
                    "field": "return.status",
                    "before": "requested",
                    "after": bundle.own_return.status.value,
                }
            )

    if refund_id is not None:
        if bundle.own_refund is None:
            diffs.append({"field": "refund.status", "before": "reserved", "after": "missing"})
        elif int(bundle.own_refund.amount_paise) != approval_amount_paise:
            diffs.append(
                {
                    "field": "refund.amount_paise",
                    "before": approval_amount_paise,
                    "after": int(bundle.own_refund.amount_paise),
                }
            )
        elif bundle.own_refund.status == RefundStatus.authorized:
            auth_ok = (
                bool(store_auth_ref)
                and bundle.own_refund.authorized_amount_paise == approval_amount_paise
            )
            if not auth_ok:
                diffs.append(
                    {
                        "field": "refund.status",
                        "before": "reserved",
                        "after": bundle.own_refund.status.value,
                    }
                )
        elif bundle.own_refund.status != RefundStatus.reserved:
            diffs.append(
                {
                    "field": "refund.status",
                    "before": "reserved",
                    "after": bundle.own_refund.status.value,
                }
            )

    # Policy version transition check: trigger when active outcome or blocking rules fail
    orig_by_id = {r.id: r for r in orig_dec.rule_results}
    active_by_id = {r.id: r for r in active_dec.rule_results}
    if active_dec.decision in (
        DecisionOutcome.deny,
        DecisionOutcome.needs_info,
        DecisionOutcome.escalate,
    ):
        diffs.append(
            {
                "field": "policy.decision",
                "before": orig_dec.decision.value,
                "after": active_dec.decision.value,
                "policy_version": active_policy.version,
            }
        )

    for rid, act_rule in active_by_id.items():
        orig_rule = orig_by_id.get(rid)
        if orig_rule is not None and orig_rule.passed and not act_rule.passed and act_rule.blocking:
            diffs.append(
                {
                    "field": f"policy.rule.{rid}",
                    "before": "passed",
                    "after": "failed",
                    "policy_version": active_policy.version,
                    "detail": act_rule.seller_text,
                }
            )

    if (
        caller_role is not None
        and caller_role != ApproverRole.seller_owner.value
        and active_dec.required_approver_role == ApproverRole.seller_owner
        and orig_dec.required_approver_role != ApproverRole.seller_owner
    ):
        diffs.append(
            {
                "field": "required_role",
                "before": str(orig_dec.required_approver_role or ApproverRole.seller_staff.value),
                "after": ApproverRole.seller_owner.value,
                "policy_version": active_policy.version,
            }
        )

    clean_diffs = sanitize_policy_facts(diffs)
    return (list(clean_diffs) if isinstance(clean_diffs, list) else []), active_dec, active_policy


def compute_recommendation(
    *,
    proposal_type: str,
    approval_status: str,
    decision: Decision | None,
    facts_drifted: bool,
) -> tuple[str, str]:
    """Compute strictly rule-based seller recommendation, never influenced by customer text."""
    if approval_status == ApprovalStatus.needs_revalidation.value or facts_drifted:
        return "review", "Facts or active policy changed since proposal creation; review required."
    if decision is None:
        return "review", "Live verification context unavailable; manual review required."
    if proposal_type != "return":
        return "review", "Non-return refund request requires manual verification."
    if decision.risk.band == RiskBand.high:
        return "review", "High customer risk signals detected."
    if decision.risk.band == RiskBand.medium:
        return "review", "Medium customer risk signals detected."

    failed_blocking_ids = [r.id for r in decision.rule_results if not r.passed and r.blocking]
    if decision.decision == DecisionOutcome.needs_approval and failed_blocking_ids == ["R-09"]:
        return (
            "approve",
            "Eligible return with low risk; only per-refund auto-limit rule (R-09) exceeded.",
        )

    return "review", "Additional policy checks require manual seller review."
