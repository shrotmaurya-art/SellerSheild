"""Deterministic decision engine for SellerShield AI.

Spec §7.3, §7.5, §7.6 & Amendments:
Pure deterministic function decide(facts, policy, now) -> Decision.
Precedence: DENY > ESCALATE > NEEDS_INFO > NEEDS_APPROVAL > ALLOW.
Zero floats, zero LLM, zero I/O, zero clock calls.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from sellershield_agent.domain.models import (
    OrderItem,
    PaymentMethod,
    PaymentStatus,
    ReturnResolution,
    ShipmentStatus,
)
from sellershield_agent.policy.facts import (
    DecisionAction,
    DecisionFacts,
    compute_facts_hash,
)
from sellershield_agent.policy.models import ItemAmounts, PolicyDocument, RefundBreakdown
from sellershield_agent.policy.refund import compute_refund
from sellershield_agent.policy.risk import (
    RiskAssessment,
    RiskBand,
    assess_risk,
)
from sellershield_agent.policy.rules import (
    ApproverRole,
    DecisionOutcome,
    RuleResult,
    rule_r01_order_ownership,
    rule_r02_order_state,
    rule_r03_category_window,
    rule_r04_item_returnable,
    rule_r05_quantity_available,
    rule_r06_active_return,
    rule_r07_evidence_required,
    rule_r08_refund_formula,
    rule_r09_per_refund_auto_limit,
    rule_r10_cumulative_limit_30d,
    rule_r11_refundable_balance_cap,
    rule_r12_staff_limit_and_hard_ceiling,
    rule_r13_risk_signals,
    rule_r14_cod_refund_method,
    rule_r15_cancellation_pre_dispatch,
)


class DecisionFlags(BaseModel):
    """Auxiliary flags returned with a decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capped: bool = False
    recommendation_review: bool = False


class Decision(BaseModel):
    """Complete evaluation output produced by the decision engine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: DecisionOutcome
    rule_results: list[RuleResult]
    amounts: RefundBreakdown
    required_approver_role: ApproverRole | str | None = None
    missing_slots: list[str] = Field(default_factory=list)
    risk: RiskAssessment
    policy_version: str
    evaluated_at: datetime
    facts_hash: str
    flags: DecisionFlags


def decide(
    facts: DecisionFacts,
    policy: PolicyDocument,
    now: datetime,
) -> Decision:
    """Evaluate all policy rules deterministically against facts."""
    action_str = (
        facts.action.value if isinstance(facts.action, DecisionAction) else str(facts.action)
    )
    is_cancellation = action_str == DecisionAction.cancellation.value
    is_refund_req = action_str == DecisionAction.refund_request.value

    # Compute facts hash (invariant: reason_text omitted)
    f_hash = compute_facts_hash(facts)

    # Risk evaluation (invariant: reason_text NEVER passed or evaluated)
    risk = assess_risk(facts.customer_risk, facts.order.total_paise, policy)

    order_items_by_id: dict[str, OrderItem] = {it.item_id: it for it in facts.order.items}
    refundable_items_by_id = {it.item_id: it for it in facts.refundable.items}

    # 1. Calculate RefundBreakdown
    is_lost = bool(facts.shipment and facts.shipment.status == ShipmentStatus.lost)
    ordered_qty_total = sum(it.qty for it in facts.order.items)
    requested_qty_total = sum(it.qty for it in facts.items)
    whole_order_returned = bool(ordered_qty_total > 0 and requested_qty_total == ordered_qty_total)

    if is_cancellation:
        if facts.order.payment_method == PaymentMethod.prepaid:
            total_cancel = facts.order.total_paise
            items_cancel = max(0, facts.order.subtotal_paise - facts.order.discount_paise)
            shipping_cancel = facts.order.shipping_fee_paise
        else:
            total_cancel = 0
            items_cancel = 0
            shipping_cancel = 0
        breakdown = RefundBreakdown(
            item_amounts=ItemAmounts(),
            items_paise=items_cancel,
            shipping_refund_paise=shipping_cancel,
            fee_paise=0,
            total_paise=total_cancel,
            capped=False,
            cap_amount_paise=None,
        )
    else:
        # Check if all requested items belong to this order and qty <= ordered qty
        items_valid_for_calc = all(
            req.item_id in order_items_by_id and 0 <= req.qty <= order_items_by_id[req.item_id].qty
            for req in facts.items
        )
        if items_valid_for_calc:
            breakdown = compute_refund(
                order=facts.order,
                requested_items=facts.items,
                reason_code=facts.reason_code or "",
                resolution=facts.resolution or ReturnResolution.refund,
                policy=policy,
                whole_order_returned=whole_order_returned,
                shipment_lost=is_lost,
                max_refundable_paise=facts.refundable.max_refundable_paise,
            )
        else:
            breakdown = RefundBreakdown(
                item_amounts=ItemAmounts(),
                items_paise=0,
                shipping_refund_paise=0,
                fee_paise=0,
                total_paise=0,
                capped=False,
                cap_amount_paise=None,
            )

    # 2. Evaluate Rule Catalog R-01 to R-15
    rule_results: list[RuleResult] = []

    # R-01: Ownership
    r01 = rule_r01_order_ownership(facts.order.customer_id, facts.customer_risk.customer_id, policy)
    rule_results.append(r01)

    # R-02: Order state
    r02 = rule_r02_order_state(facts.action, facts.order.status, policy, shipment_lost=is_lost)
    rule_results.append(r02)

    # R-03: Return window
    is_partially_refunded = facts.order.payment_status == PaymentStatus.partially_refunded
    if is_cancellation or is_lost or is_partially_refunded:
        r03 = RuleResult(
            id="R-03",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Request is within the return period.",
            seller_text="Not applicable for cancellation, lost shipment, or prior partial refund.",
        )
    else:
        # Find category of first requested item or default
        cat: str | None = None
        for it in facts.items:
            if it.item_id in order_items_by_id:
                cat = order_items_by_id[it.item_id].category
                break
        r03 = rule_r03_category_window(facts.order.delivered_at, cat, now, policy)
    rule_results.append(r03)

    # R-04: Item returnable
    if is_cancellation or is_lost:
        r04 = RuleResult(
            id="R-04",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Item is eligible for return.",
            seller_text="Not applicable for cancellation or lost shipment.",
        )
    else:
        r04_passed = True
        r04_res: RuleResult | None = None
        for it in facts.items:
            if it.item_id in order_items_by_id:
                res = rule_r04_item_returnable(order_items_by_id[it.item_id], policy)
                if not res.passed:
                    r04_passed = False
                    r04_res = res
                    break
        if not r04_passed and r04_res is not None:
            r04 = r04_res
        else:
            r04 = RuleResult(
                id="R-04",
                passed=True,
                blocking=True,
                outcome_if_failed=None,
                customer_text="Item is eligible for return.",
                seller_text="All requested items are returnable.",
            )
    rule_results.append(r04)

    # R-05: Quantity available
    if is_cancellation:
        r05 = RuleResult(
            id="R-05",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Requested quantity is available.",
            seller_text="Not applicable for cancellation.",
        )
    else:
        r05_passed = True
        r05_res: RuleResult | None = None
        for it in facts.items:
            if it.item_id not in order_items_by_id or it.item_id not in refundable_items_by_id:
                r05_passed = False
                r05_res = rule_r05_quantity_available(it.qty, 0, policy)
                break
            ref_it = refundable_items_by_id[it.item_id]
            res = rule_r05_quantity_available(it.qty, ref_it.remaining_qty, policy)
            if not res.passed:
                r05_passed = False
                r05_res = res
                break
        if not r05_passed and r05_res is not None:
            r05 = r05_res
        else:
            r05 = RuleResult(
                id="R-05",
                passed=True,
                blocking=True,
                outcome_if_failed=None,
                customer_text="Requested quantity is available.",
                seller_text="All requested quantities available.",
            )
    rule_results.append(r05)

    # R-06: Active return
    if is_cancellation:
        r06 = RuleResult(
            id="R-06",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="No existing return for this item.",
            seller_text="Not applicable for cancellation.",
        )
    else:
        req_item_ids = {it.item_id for it in facts.items}
        r06 = rule_r06_active_return(req_item_ids, facts.active_returns, policy)
    rule_results.append(r06)

    # R-07: Reason needs evidence
    if is_cancellation:
        r07 = RuleResult(
            id="R-07",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="No additional evidence required.",
            seller_text="Not applicable for cancellation.",
        )
    else:
        r07 = rule_r07_evidence_required(facts.reason_code, facts.evidence_present, policy)
    rule_results.append(r07)

    # R-08: Refund formula
    r08 = rule_r08_refund_formula(breakdown, policy)
    rule_results.append(r08)

    # R-09: Per-refund auto limit
    if is_cancellation:
        r09 = RuleResult(
            id="R-09",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Your request is eligible for standard processing.",
            seller_text="Cancellation evaluated under pre-dispatch cancellation limit (R-15).",
        )
    else:
        r09 = rule_r09_per_refund_auto_limit(breakdown.total_paise, policy)
    rule_results.append(r09)

    # R-10: Customer 30-day cumulative limit
    if is_cancellation:
        r10 = RuleResult(
            id="R-10",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Your request is eligible for standard processing.",
            seller_text="Not applicable for cancellation.",
        )
    else:
        r10 = rule_r10_cumulative_limit_30d(
            facts.previous_refunds_30d_paise, breakdown.total_paise, policy
        )
    rule_results.append(r10)

    # R-11: Refundable balance cap
    r11 = rule_r11_refundable_balance_cap(breakdown.capped, policy)
    rule_results.append(r11)

    # R-12: Staff limit and hard ceiling
    r12 = rule_r12_staff_limit_and_hard_ceiling(breakdown.total_paise, policy)
    rule_results.append(r12)

    # R-13: Risk signals
    r13 = rule_r13_risk_signals(risk, policy)
    rule_results.append(r13)

    # R-14: COD refund method
    if is_cancellation:
        r14 = RuleResult(
            id="R-14",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Payment method verified.",
            seller_text="Not applicable for cancellation.",
        )
    else:
        r14 = rule_r14_cod_refund_method(
            facts.order.payment_method, facts.refund_method, facts.resolution, policy
        )
    rule_results.append(r14)

    # R-15: Cancellation pre-dispatch
    if is_cancellation:
        r15 = rule_r15_cancellation_pre_dispatch(
            facts.order.status, facts.order.total_paise, policy
        )
    else:
        r15 = RuleResult(
            id="R-15",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Cancellation request eligible.",
            seller_text="Not applicable for return/refund.",
        )
    rule_results.append(r15)

    # 3. Precedence resolution: DENY > ESCALATE > NEEDS_INFO > NEEDS_APPROVAL > ALLOW
    missing_slots: list[str] = []
    for r in rule_results:
        for s in r.missing_slots:
            if s not in missing_slots:
                missing_slots.append(s)

    any_deny = any(
        r.outcome_if_failed == DecisionOutcome.deny
        for r in rule_results
        if not r.passed and r.blocking
    )
    any_escalate = any(
        r.outcome_if_failed == DecisionOutcome.escalate
        for r in rule_results
        if not r.passed and r.blocking
    )
    any_needs_info = any(
        r.outcome_if_failed == DecisionOutcome.needs_info
        for r in rule_results
        if not r.passed and r.blocking
    )
    any_needs_approval = any(
        r.outcome_if_failed == DecisionOutcome.needs_approval
        for r in rule_results
        if not r.passed and r.blocking
    )

    # Special rules:
    # 1. High risk forces NEEDS_APPROVAL (never causes DENY)
    high_risk_forces_approval = risk.band == RiskBand.high

    # 2. Lost shipment refund_request is always NEEDS_APPROVAL
    lost_forces_approval = is_lost and is_refund_req

    if any_deny:
        outcome = DecisionOutcome.deny
    elif any_escalate:
        outcome = DecisionOutcome.escalate
    elif any_needs_info or len(missing_slots) > 0:
        outcome = DecisionOutcome.needs_info
    elif any_needs_approval or high_risk_forces_approval or lost_forces_approval:
        outcome = DecisionOutcome.needs_approval
    else:
        outcome = DecisionOutcome.allow

    # 4. Approver Role Assignment
    if outcome == DecisionOutcome.needs_approval:
        if breakdown.total_paise > policy.staff_approval_limit_paise:
            required_approver_role: ApproverRole | str | None = ApproverRole.seller_owner
        else:
            required_approver_role = ApproverRole.seller_staff
    else:
        required_approver_role = None

    # 5. Flags
    flags = DecisionFlags(
        capped=breakdown.capped,
        recommendation_review=(risk.band == RiskBand.medium),
    )

    return Decision(
        decision=outcome,
        rule_results=rule_results,
        amounts=breakdown,
        required_approver_role=required_approver_role,
        missing_slots=missing_slots,
        risk=risk,
        policy_version=policy.version,
        evaluated_at=now,
        facts_hash=f_hash,
        flags=flags,
    )
