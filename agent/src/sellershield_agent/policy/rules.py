"""Rule catalog R-01 to R-15 for SellerShield AI.

Spec §7.3 & §7.5:
Pure deterministic rule functions returning RuleResult.
Invariant KP-23: customer_text NEVER leaks limits, thresholds, risk, scores,
staff, owner, or approval tiers.
Missing information produces missing_slots, never a guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from sellershield_agent.domain.models import (
    OrderItem,
    OrderStatus,
    PaymentMethod,
    RefundMethod,
    ReturnReasonCode,
    ReturnRequest,
    ReturnResolution,
    ReturnStatus,
)
from sellershield_agent.policy.facts import DecisionAction
from sellershield_agent.policy.models import PolicyDocument, RefundBreakdown
from sellershield_agent.policy.risk import RiskAssessment, RiskBand


class DecisionOutcome(StrEnum):
    """Deterministic policy decision outcome."""

    deny = "DENY"
    escalate = "ESCALATE"
    needs_info = "NEEDS_INFO"
    needs_approval = "NEEDS_APPROVAL"
    allow = "ALLOW"


class ApproverRole(StrEnum):
    """Tier of human seller authorization required."""

    seller_staff = "seller_staff"
    seller_owner = "seller_owner"


class RuleResult(BaseModel):
    """Result of evaluating a single policy rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    passed: bool
    blocking: bool = True
    outcome_if_failed: DecisionOutcome | None = None
    customer_text: str
    seller_text: str
    facts: dict[str, Any] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)


def rule_r01_order_ownership(
    order_customer_id: str,
    authenticated_customer_id: str,
    policy: PolicyDocument,
) -> RuleResult:
    """R-01: Authenticated customer owns the order."""
    if order_customer_id == authenticated_customer_id:
        return RuleResult(
            id="R-01",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Order verified.",
            seller_text="Order customer matches authenticated customer.",
        )
    return RuleResult(
        id="R-01",
        passed=False,
        blocking=True,
        outcome_if_failed=DecisionOutcome.deny,
        customer_text="Order not found.",
        seller_text=(
            f"Customer mismatch: order={order_customer_id}, auth={authenticated_customer_id}."
        ),
    )


def rule_r02_order_state(
    action: DecisionAction | str,
    order_status: OrderStatus | str,
    policy: PolicyDocument,
    shipment_lost: bool = False,
) -> RuleResult:
    """R-02: Order state allows the action (delivered for returns, not shipped for cancellation)."""
    action_str = action.value if isinstance(action, DecisionAction) else str(action)
    status_str = order_status.value if isinstance(order_status, OrderStatus) else str(order_status)

    if action_str == DecisionAction.refund_request.value and shipment_lost:
        return RuleResult(
            id="R-02",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Order status allows this request.",
            seller_text=f"Lost shipment refund request allowed for status {status_str}.",
        )

    if action_str in (DecisionAction.return_.value, DecisionAction.refund_request.value):
        if status_str == OrderStatus.delivered.value:
            return RuleResult(
                id="R-02",
                passed=True,
                blocking=True,
                outcome_if_failed=None,
                customer_text="Order status allows this request.",
                seller_text=f"Order status {status_str} allows {action_str}.",
            )
        return RuleResult(
            id="R-02",
            passed=False,
            blocking=True,
            outcome_if_failed=DecisionOutcome.deny,
            customer_text="Returns can only be requested after an order has been delivered.",
            seller_text=f"Order status {status_str} does not allow returns (must be delivered).",
        )

    if action_str == DecisionAction.cancellation.value:
        if status_str in (OrderStatus.placed.value, OrderStatus.packed.value):
            return RuleResult(
                id="R-02",
                passed=True,
                blocking=True,
                outcome_if_failed=None,
                customer_text="Order status allows cancellation.",
                seller_text=f"Order status {status_str} allows pre-dispatch cancellation.",
            )
        return RuleResult(
            id="R-02",
            passed=False,
            blocking=True,
            outcome_if_failed=DecisionOutcome.deny,
            customer_text="Orders that have already shipped cannot be cancelled.",
            seller_text=f"Cancellation denied because order status is {status_str}.",
        )

    return RuleResult(
        id="R-02",
        passed=True,
        blocking=True,
        outcome_if_failed=None,
        customer_text="Order status verified.",
        seller_text=f"Action {action_str} with status {status_str} allowed.",
    )


def rule_r03_category_window(
    delivered_at: datetime | None,
    category: str | None,
    now: datetime,
    policy: PolicyDocument,
) -> RuleResult:
    """R-03: Within the category return window (calendar days ending at 23:59:59 IST)."""
    if delivered_at is None:
        return RuleResult(
            id="R-03",
            passed=False,
            blocking=True,
            outcome_if_failed=DecisionOutcome.deny,
            customer_text="Return period cannot be verified without delivery confirmation.",
            seller_text="Order delivered_at is None; cannot verify return window.",
        )

    tz = ZoneInfo(policy.timezone)
    delivered_ist = delivered_at.astimezone(tz)
    now_ist = now.astimezone(tz)
    cat_key = category or ""
    window_days = policy.return_windows_days.get(cat_key, 7)

    end_date = (delivered_ist + timedelta(days=window_days)).date()
    window_end_ist = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=tz)

    if now_ist <= window_end_ist:
        return RuleResult(
            id="R-03",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Request is within the return period.",
            seller_text=f"Within {window_days}-day window ending {window_end_ist.isoformat()}.",
            facts={"window_days": window_days, "window_end": window_end_ist.isoformat()},
        )

    return RuleResult(
        id="R-03",
        passed=False,
        blocking=True,
        outcome_if_failed=DecisionOutcome.deny,
        customer_text=(
            "The return period for this item has passed. If you need assistance, "
            "you can ask for seller review."
        ),
        seller_text=(
            f"Outside {window_days}-day return window: delivered {delivered_ist.isoformat()}, "
            f"expired {window_end_ist.isoformat()}, requested {now_ist.isoformat()}."
        ),
        facts={"window_days": window_days, "window_end": window_end_ist.isoformat()},
    )


def rule_r04_item_returnable(
    item: OrderItem,
    policy: PolicyDocument,
) -> RuleResult:
    """R-04: Item is returnable (not final sale, hygiene or custom)."""
    cat_key = item.category or ""
    cat_window = policy.return_windows_days.get(cat_key, 7)
    is_non_returnable = (
        any(item.flags.get(flag) is True for flag in policy.non_returnable_flags) or cat_window == 0
    )

    if is_non_returnable:
        return RuleResult(
            id="R-04",
            passed=False,
            blocking=True,
            outcome_if_failed=DecisionOutcome.deny,
            customer_text="This item is marked as non-returnable.",
            seller_text=(
                f"Item {item.item_id} is non-returnable "
                f"(category={item.category}, flags={item.flags})."
            ),
            facts={"item_id": item.item_id, "flags": item.flags, "category": item.category},
        )

    return RuleResult(
        id="R-04",
        passed=True,
        blocking=True,
        outcome_if_failed=None,
        customer_text="Item is eligible for return.",
        seller_text=f"Item {item.item_id} is returnable.",
        facts={"item_id": item.item_id},
    )


def rule_r05_quantity_available(
    requested_qty: int,
    remaining_qty: int,
    policy: PolicyDocument,
) -> RuleResult:
    """R-05: Quantity at most delivered minus returned minus pending."""
    if 1 <= requested_qty <= remaining_qty and remaining_qty > 0:
        return RuleResult(
            id="R-05",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Requested quantity is available.",
            seller_text=f"Requested qty {requested_qty} <= available {remaining_qty}.",
            facts={"requested_qty": requested_qty, "remaining_qty": remaining_qty},
        )

    return RuleResult(
        id="R-05",
        passed=False,
        blocking=True,
        outcome_if_failed=DecisionOutcome.deny,
        customer_text="The requested quantity is greater than the quantity available for return.",
        seller_text=(
            f"Requested qty {requested_qty} exceeds remaining available qty {remaining_qty}."
        ),
        facts={"requested_qty": requested_qty, "remaining_qty": remaining_qty},
    )


def rule_r06_active_return(
    item_ids: set[str],
    active_returns: list[ReturnRequest],
    policy: PolicyDocument,
) -> RuleResult:
    """R-06: No active return for the item."""
    terminal_statuses = {
        ReturnStatus.rejected.value,
        ReturnStatus.cancelled.value,
        ReturnStatus.completed.value,
    }
    for ret in active_returns:
        status_val = ret.status.value if isinstance(ret.status, ReturnStatus) else str(ret.status)
        if status_val not in terminal_statuses:
            ret_item_ids = {it.item_id for it in ret.items}
            overlapping = item_ids.intersection(ret_item_ids)
            if overlapping:
                return RuleResult(
                    id="R-06",
                    passed=False,
                    blocking=True,
                    outcome_if_failed=DecisionOutcome.deny,
                    customer_text=(
                        f"There is already an active return ({ret.return_id}) for this item."
                    ),
                    seller_text=(
                        f"Active return {ret.return_id} exists with status {status_val} "
                        f"for items {overlapping}."
                    ),
                    facts={
                        "existing_return_id": ret.return_id,
                        "overlapping_items": list(overlapping),
                    },
                )

    return RuleResult(
        id="R-06",
        passed=True,
        blocking=True,
        outcome_if_failed=None,
        customer_text="No existing return for this item.",
        seller_text="No duplicate active returns found.",
    )


def rule_r07_evidence_required(
    reason_code: ReturnReasonCode | str | None,
    evidence_present: bool,
    policy: PolicyDocument,
) -> RuleResult:
    """R-07: Reason needs evidence (damaged, wrong_item, not_as_described)."""
    reason_str = (
        reason_code.value if isinstance(reason_code, ReturnReasonCode) else str(reason_code or "")
    )
    evidence_reasons = {
        ReturnReasonCode.damaged.value,
        ReturnReasonCode.wrong_item.value,
        ReturnReasonCode.not_as_described.value,
    }

    if reason_str in evidence_reasons:
        if not evidence_present:
            return RuleResult(
                id="R-07",
                passed=False,
                blocking=True,
                outcome_if_failed=DecisionOutcome.needs_info,
                customer_text=(
                    "Please provide photo evidence for this item to proceed with your request."
                ),
                seller_text=f"Evidence required for reason {reason_str} but not provided.",
                missing_slots=["evidence"],
            )
        return RuleResult(
            id="R-07",
            passed=False,
            blocking=True,
            outcome_if_failed=DecisionOutcome.needs_approval,
            customer_text="This request needs a quick review by the seller.",
            seller_text=f"Evidence submitted for reason {reason_str}; requires seller review.",
        )

    return RuleResult(
        id="R-07",
        passed=True,
        blocking=True,
        outcome_if_failed=None,
        customer_text="No additional evidence required.",
        seller_text=f"Reason {reason_str} does not require evidence.",
    )


def rule_r08_refund_formula(
    breakdown: RefundBreakdown,
    policy: PolicyDocument,
) -> RuleResult:
    """R-08: Refund computed by the formula in §7.4 (informational)."""
    return RuleResult(
        id="R-08",
        passed=True,
        blocking=False,
        outcome_if_failed=None,
        customer_text="Refund amount calculated according to store return policy.",
        seller_text=(
            f"Breakdown: items={breakdown.items_paise}, fee={breakdown.fee_paise}, "
            f"shipping={breakdown.shipping_refund_paise}, total={breakdown.total_paise}."
        ),
        facts={"breakdown": breakdown.model_dump()},
    )


def rule_r09_per_refund_auto_limit(
    total_paise: int,
    policy: PolicyDocument,
) -> RuleResult:
    """R-09: Amount within the per-refund auto limit."""
    if total_paise <= policy.auto_limit_paise:
        return RuleResult(
            id="R-09",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Your request is eligible for standard processing.",
            seller_text=f"Total {total_paise} <= auto limit {policy.auto_limit_paise}.",
            facts={"total_paise": total_paise, "auto_limit_paise": policy.auto_limit_paise},
        )

    return RuleResult(
        id="R-09",
        passed=False,
        blocking=True,
        outcome_if_failed=DecisionOutcome.needs_approval,
        customer_text="This request needs a quick review by the seller.",
        seller_text=(
            f"Amount {total_paise} paise exceeds per-refund auto limit "
            f"{policy.auto_limit_paise} paise."
        ),
        facts={"total_paise": total_paise, "auto_limit_paise": policy.auto_limit_paise},
    )


def rule_r10_cumulative_limit_30d(
    previous_refunds_30d_paise: int,
    total_paise: int,
    policy: PolicyDocument,
) -> RuleResult:
    """R-10: Customer 30-day cumulative within the limit."""
    if previous_refunds_30d_paise <= 0:
        return RuleResult(
            id="R-10",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Your request is eligible for standard processing.",
            seller_text=(
                "No prior 30-day refunds; cumulative limit evaluated under per-refund limit."
            ),
            facts={
                "cumulative_paise": total_paise,
                "limit_paise": policy.customer_cumulative_auto_limit_30d_paise,
            },
        )

    new_cumulative = previous_refunds_30d_paise + total_paise
    limit = policy.customer_cumulative_auto_limit_30d_paise
    if new_cumulative <= limit:
        return RuleResult(
            id="R-10",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Your request is eligible for standard processing.",
            seller_text=f"Cumulative 30d refunds ({new_cumulative}) within limit ({limit}).",
            facts={"cumulative_paise": new_cumulative, "limit_paise": limit},
        )

    return RuleResult(
        id="R-10",
        passed=False,
        blocking=True,
        outcome_if_failed=DecisionOutcome.needs_approval,
        customer_text="This request needs a quick review by the seller.",
        seller_text=(
            f"Cumulative 30d refunds ({previous_refunds_30d_paise} + {total_paise} = "
            f"{new_cumulative}) exceeds limit {limit}."
        ),
        facts={"cumulative_paise": new_cumulative, "limit_paise": limit},
    )


def rule_r11_refundable_balance_cap(
    capped: bool,
    policy: PolicyDocument,
) -> RuleResult:
    """R-11: Within the order remaining refundable balance."""
    if capped:
        return RuleResult(
            id="R-11",
            passed=True,
            blocking=False,
            outcome_if_failed=None,
            customer_text=(
                "Refund amount adjusted to the maximum remaining refundable balance for this order."
            ),
            seller_text="Refund amount was capped by remaining store balance.",
            facts={"capped": True},
        )

    return RuleResult(
        id="R-11",
        passed=True,
        blocking=False,
        outcome_if_failed=None,
        customer_text="Refund amount within order refundable balance.",
        seller_text="Refund amount within refundable balance.",
        facts={"capped": False},
    )


def rule_r12_staff_limit_and_hard_ceiling(
    total_paise: int,
    policy: PolicyDocument,
) -> RuleResult:
    """R-12: Above the staff limit needs the owner; at or above the hard ceiling -> ESCALATE."""
    if total_paise >= policy.hard_ceiling_paise:
        return RuleResult(
            id="R-12",
            passed=False,
            blocking=True,
            outcome_if_failed=DecisionOutcome.escalate,
            customer_text="This request needs a quick review by the seller.",
            seller_text=(
                f"Amount {total_paise} paise is at or above hard ceiling "
                f"{policy.hard_ceiling_paise} paise; escalated."
            ),
            facts={"required_approver_role": ApproverRole.seller_owner.value},
        )

    if total_paise > policy.staff_approval_limit_paise:
        return RuleResult(
            id="R-12",
            passed=False,
            blocking=True,
            outcome_if_failed=DecisionOutcome.needs_approval,
            customer_text="This request needs a quick review by the seller.",
            seller_text=(
                f"Amount {total_paise} paise exceeds staff approval limit "
                f"{policy.staff_approval_limit_paise} paise; requires owner approval."
            ),
            facts={"required_approver_role": ApproverRole.seller_owner.value},
        )

    return RuleResult(
        id="R-12",
        passed=True,
        blocking=True,
        outcome_if_failed=None,
        customer_text="Your request is eligible for standard review.",
        seller_text=(
            f"Amount {total_paise} paise is within staff limit "
            f"{policy.staff_approval_limit_paise} paise."
        ),
        facts={"required_approver_role": ApproverRole.seller_staff.value},
    )


def rule_r13_risk_signals(
    risk: RiskAssessment,
    policy: PolicyDocument,
) -> RuleResult:
    """R-13: Risk signals (7.5). High risk forces NEEDS_APPROVAL, never DENY."""
    if risk.band == RiskBand.high:
        return RuleResult(
            id="R-13",
            passed=False,
            blocking=True,
            outcome_if_failed=DecisionOutcome.needs_approval,
            customer_text="This request needs a quick review by the seller.",
            seller_text=(
                f"High risk signals detected (score {risk.score}, signals={risk.signals}); "
                "review required."
            ),
            facts={"risk_score": risk.score, "risk_band": risk.band.value},
        )

    if risk.band == RiskBand.medium:
        return RuleResult(
            id="R-13",
            passed=True,
            blocking=False,
            outcome_if_failed=None,
            customer_text="Your request is being processed.",
            seller_text=(
                f"Medium risk signals detected (score {risk.score}, signals={risk.signals}); "
                "review recommended."
            ),
            facts={
                "recommendation_review": True,
                "risk_score": risk.score,
                "risk_band": risk.band.value,
            },
        )

    return RuleResult(
        id="R-13",
        passed=True,
        blocking=False,
        outcome_if_failed=None,
        customer_text="Your request is being processed.",
        seller_text=f"Low risk signals (score {risk.score}).",
        facts={"risk_score": risk.score, "risk_band": risk.band.value},
    )


def rule_r14_cod_refund_method(
    payment_method: PaymentMethod | str,
    refund_method: RefundMethod | str | None,
    resolution: ReturnResolution | str | None,
    policy: PolicyDocument,
) -> RuleResult:
    """R-14: COD order needs a refund method choice."""
    pm_str = (
        payment_method.value if isinstance(payment_method, PaymentMethod) else str(payment_method)
    )
    res_str = (
        resolution.value if isinstance(resolution, ReturnResolution) else str(resolution or "")
    )

    if pm_str == PaymentMethod.cod.value and res_str == ReturnResolution.refund.value:
        if refund_method is None:
            return RuleResult(
                id="R-14",
                passed=False,
                blocking=True,
                outcome_if_failed=DecisionOutcome.needs_info,
                customer_text=(
                    "For Cash on Delivery orders, please choose your preferred refund method."
                ),
                seller_text=(
                    "Cash on Delivery order requires refund method choice "
                    "(store_credit or upi_payout)."
                ),
                missing_slots=["refund_method"],
            )
        rm_str = (
            refund_method.value if isinstance(refund_method, RefundMethod) else str(refund_method)
        )
        return RuleResult(
            id="R-14",
            passed=True,
            blocking=True,
            outcome_if_failed=None,
            customer_text="Refund method selected.",
            seller_text=f"COD refund method selected: {rm_str}.",
            facts={"refund_method": rm_str},
        )

    return RuleResult(
        id="R-14",
        passed=True,
        blocking=True,
        outcome_if_failed=None,
        customer_text="Payment method verified.",
        seller_text=f"Payment method {pm_str} does not require extra refund method selection.",
    )


def rule_r15_cancellation_pre_dispatch(
    order_status: OrderStatus | str,
    total_paise: int,
    policy: PolicyDocument,
) -> RuleResult:
    """R-15: Cancellation only before dispatch."""
    status_str = order_status.value if isinstance(order_status, OrderStatus) else str(order_status)
    if status_str in (
        OrderStatus.shipped.value,
        OrderStatus.out_for_delivery.value,
        OrderStatus.delivered.value,
    ):
        return RuleResult(
            id="R-15",
            passed=False,
            blocking=True,
            outcome_if_failed=DecisionOutcome.deny,
            customer_text="Orders that have already shipped cannot be cancelled.",
            seller_text=f"Cancellation denied because order status is {status_str}.",
        )

    if total_paise > policy.pre_dispatch_cancel_auto_limit_paise:
        return RuleResult(
            id="R-15",
            passed=False,
            blocking=True,
            outcome_if_failed=DecisionOutcome.needs_approval,
            customer_text="This request needs a quick review by the seller.",
            seller_text=(
                f"Pre-dispatch cancellation amount {total_paise} exceeds limit "
                f"{policy.pre_dispatch_cancel_auto_limit_paise}."
            ),
        )

    return RuleResult(
        id="R-15",
        passed=True,
        blocking=True,
        outcome_if_failed=None,
        customer_text="Cancellation request eligible.",
        seller_text=f"Cancellation allowed for status {status_str}.",
    )
