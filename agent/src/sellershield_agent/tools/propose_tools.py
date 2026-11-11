"""Propose tools implementations (propose_return, propose_refund).

Spec: docs/spec/02-agent.md sections 5 (W3, W4), 6.2, 7, 21 (Amendments 1, 4, 5).
Invariants:
- Propose tools NEVER mutate the store; they create proposals in the agent DB only.
- Zero amount, customer_id, or role arguments exist on any tool schema.
- reason_text is stripped of HTML, capped at 200 chars, stored in payload_json only,
  and NEVER passed to decide() or included in canonical_card / proposal_hash.
- At most 3 open proposals per customer and 10 per simulated IST day.
- Re-proposing the same order and items returns the existing open proposal.
- Customer-visible payloads never contain forbidden words (limit, threshold, risk, staff, owner).
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sellershield_agent.domain.models import (
    CustomerRiskFacts,
    Order,
    PaymentMethod,
    RefundMethod,
    ReturnItem,
    ReturnReasonCode,
    ReturnResolution,
)
from sellershield_agent.policy.decide import Decision, decide
from sellershield_agent.policy.facts import DecisionAction, DecisionFacts
from sellershield_agent.policy.rules import ApproverRole, DecisionOutcome
from sellershield_agent.proposals.service import (
    canonical_item_signature,
    check_limits_readonly,
    persist_proposal_and_build_card,
)
from sellershield_agent.store.client import StoreNotFound
from sellershield_agent.tools.base import (
    Tool,
    ToolContext,
    ToolErrorCode,
    ToolKind,
    ToolResult,
)
from sellershield_agent.tools.messages import get_customer_safe_message
from sellershield_agent.tools.read_tools import OrderItemInput

HTML_TAG_REGEX = re.compile(r"<[^>]*>")
CONTROL_CHAR_REGEX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
EVIDENCE_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
ITEM_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RefundCause(StrEnum):
    """Permitted causes for requesting a refund without a return."""

    lost = "lost"
    undelivered = "undelivered"


def _validate_unique_items(value: list[OrderItemInput]) -> list[OrderItemInput]:
    seen: set[str] = set()
    for item in value:
        clean_id = item.item_id.strip()
        if not clean_id or not ITEM_ID_REGEX.fullmatch(clean_id):
            raise ValueError(f"Invalid item_id: {item.item_id!r}")
        if clean_id in seen:
            raise ValueError(f"Duplicate item_id in items: {clean_id!r}")
        seen.add(clean_id)
    return value


class ProposeReturnArgs(BaseModel):
    """Strict input arguments for propose_return (extra fields forbidden)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    items: list[OrderItemInput] = Field(min_length=1)
    reason_code: ReturnReasonCode
    reason_text: str | None = None
    resolution: ReturnResolution = ReturnResolution.refund
    refund_method: RefundMethod | None = None
    evidence_ids: list[str] | None = None

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[OrderItemInput]) -> list[OrderItemInput]:
        return _validate_unique_items(value)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > 3:
            raise ValueError("At most 3 evidence_ids are allowed")
        cleaned: list[str] = []
        for ev_id in value:
            stripped = ev_id.strip()
            if not stripped or not EVIDENCE_ID_REGEX.fullmatch(stripped):
                raise ValueError(f"Invalid evidence_id format: {ev_id!r}")
            cleaned.append(stripped)
        return cleaned


class ProposeRefundArgs(BaseModel):
    """Strict input arguments for propose_refund (extra fields forbidden)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    cause: RefundCause
    items: list[OrderItemInput] | None = None
    refund_method: RefundMethod | None = None

    @field_validator("items")
    @classmethod
    def validate_optional_items(
        cls, value: list[OrderItemInput] | None
    ) -> list[OrderItemInput] | None:
        if value is None:
            return None
        return _validate_unique_items(value)


class ProposeCancellationArgs(BaseModel):
    """Strict input arguments for propose_cancellation (extra fields forbidden)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    reason_text: str | None = None


def sanitize_reason_text(raw: str | None) -> str:
    """Strip HTML tags and control characters and cap at 200 characters."""
    if raw is None:
        return ""
    cleaned = raw
    # Iteratively strip tags so nested sequences like <scr<script>ipt> cannot survive
    while True:
        next_val = HTML_TAG_REGEX.sub("", cleaned)
        if next_val == cleaned:
            break
        cleaned = next_val
    cleaned = cleaned.replace("<", "").replace(">", "")
    cleaned = CONTROL_CHAR_REGEX.sub("", cleaned).strip()
    return cleaned[:200]


async def _fetch_decision_facts(
    ctx: ToolContext,
    *,
    order: Order,
    customer_id: str,
    action: DecisionAction,
    requested_items: list[ReturnItem],
    reason_code: ReturnReasonCode,
    resolution: ReturnResolution,
    refund_method: RefundMethod | None,
    evidence_present: bool,
) -> DecisionFacts:
    req_pairs = [(it.item_id, it.qty) for it in requested_items]
    refundable = await ctx.store_client.get_refundable(
        ctx.customer_ctx, order.order_id, items=req_pairs
    )
    active_returns = await ctx.store_client.list_returns(ctx.customer_ctx, order.order_id)

    shipment = None
    try:
        shipment = await ctx.store_client.get_shipment(ctx.customer_ctx, order.order_id)
    except StoreNotFound:
        shipment = None

    default_risk = CustomerRiskFacts(
        customer_id=customer_id,
        returns_30d=0,
        refund_ratio_90d_bp=0,
        account_age_days=30,
        refunded_30d_paise=0,
    )
    if ctx.service_ctx_factory:
        service_ctx = ctx.service_ctx_factory(ctx.customer_ctx.request_id)
        try:
            risk_facts = await ctx.store_client.get_customer_risk_facts(service_ctx, customer_id)
        except StoreNotFound:
            risk_facts = default_risk
    else:
        risk_facts = default_risk

    return DecisionFacts(
        action=action,
        order=order,
        items=requested_items,
        reason_code=reason_code,
        reason_text=None,  # Invariant: reason_text is NEVER read by decide()
        resolution=resolution,
        refund_method=refund_method,
        evidence_present=evidence_present,
        active_returns=active_returns,
        previous_refunds_30d_paise=risk_facts.refunded_30d_paise,
        refundable=refundable,
        customer_risk=risk_facts,
        shipment=shipment,
        now=ctx.clock.now(),
    )


def _extract_customer_rule_text(decision: Decision) -> str:
    matching = [
        r.customer_text
        for r in decision.rule_results
        if not r.passed and r.blocking and r.outcome_if_failed == decision.decision
    ]
    if matching:
        return " ".join(matching)
    blocking = [r.customer_text for r in decision.rule_results if not r.passed and r.blocking]
    if blocking:
        return " ".join(blocking)
    return "Your request has been evaluated against store policy."


def _non_proposal_result(order_id: str, decision: Decision) -> ToolResult | None:
    """Return immediate ToolResult for DENY / NEEDS_INFO / ESCALATE without writing."""
    rule_text = _extract_customer_rule_text(decision)
    if decision.decision == DecisionOutcome.deny:
        return ToolResult(
            ok=True,
            data={
                "proposal": None,
                "decision": DecisionOutcome.deny.value.lower(),
                "rule_text": rule_text,
            },
            fact_ids=[order_id],
        )
    if decision.decision == DecisionOutcome.needs_info:
        return ToolResult(
            ok=True,
            data={
                "proposal": None,
                "decision": DecisionOutcome.needs_info.value.lower(),
                "missing_slots": list(decision.missing_slots),
                "rule_text": rule_text,
            },
            fact_ids=[order_id],
        )
    if decision.decision == DecisionOutcome.escalate:
        return ToolResult(
            ok=True,
            data={
                "proposal": None,
                "decision": DecisionOutcome.escalate.value.lower(),
                "handoff_hint": (
                    "This request requires assistance from seller support. "
                    "Would you like me to create a support case?"
                ),
                "rule_text": rule_text,
            },
            fact_ids=[order_id],
        )
    return None


def _rate_limited_result() -> ToolResult:
    return ToolResult(
        ok=False,
        error_code=ToolErrorCode.rate_limited,
        error_message=get_customer_safe_message(ToolErrorCode.rate_limited),
    )


async def handle_propose_return(ctx: ToolContext, args: ProposeReturnArgs) -> ToolResult:
    """Create a deterministic return proposal or return policy decision details."""
    customer_id = ctx.customer_ctx.principal.customer_id
    if not customer_id:
        return ToolResult(
            ok=False,
            error_code=ToolErrorCode.not_permitted,
            error_message=get_customer_safe_message(ToolErrorCode.not_permitted),
        )
    if ctx.engine is None:
        return ToolResult(
            ok=False,
            error_code=ToolErrorCode.internal,
            error_message=get_customer_safe_message(ToolErrorCode.internal),
        )

    order = await ctx.store_client.get_order(ctx.customer_ctx, args.order_id)
    if order.customer_id != customer_id:
        raise StoreNotFound(f"Order {args.order_id} not found")

    if (
        order.payment_method == PaymentMethod.cod
        and args.resolution == ReturnResolution.refund
        and args.refund_method == RefundMethod.original
    ):
        return ToolResult(
            ok=False,
            error_code=ToolErrorCode.invalid_args,
            error_message=get_customer_safe_message(ToolErrorCode.invalid_args),
        )

    if args.evidence_ids:
        list_ev = getattr(ctx.store_client, "list_evidence", None)
        if callable(list_ev):
            store_ev = await list_ev(ctx.customer_ctx, order.order_id)
            valid_ids = {getattr(ev, "evidence_id", None) for ev in store_ev}
            if not set(args.evidence_ids).issubset(valid_ids):
                return ToolResult(
                    ok=False,
                    error_code=ToolErrorCode.invalid_args,
                    error_message=get_customer_safe_message(ToolErrorCode.invalid_args),
                )

    requested_items = [ReturnItem(item_id=it.item_id, qty=it.qty) for it in args.items]
    existing_card, existing_id, rate_limited = check_limits_readonly(
        ctx.engine,
        customer_id=customer_id,
        order_id=order.order_id,
        item_sig=canonical_item_signature(requested_items),
        clock=ctx.clock,
    )
    if existing_card is not None and existing_id is not None:
        return ToolResult(ok=True, data=existing_card, fact_ids=[order.order_id, existing_id])
    if rate_limited:
        return _rate_limited_result()

    if args.resolution == ReturnResolution.store_credit:
        effective_method: RefundMethod | None = RefundMethod.store_credit
    elif args.refund_method is not None:
        effective_method = args.refund_method
    elif order.payment_method == PaymentMethod.prepaid:
        effective_method = RefundMethod.original
    else:
        effective_method = None

    facts = await _fetch_decision_facts(
        ctx,
        order=order,
        customer_id=customer_id,
        action=DecisionAction.return_,
        requested_items=requested_items,
        reason_code=args.reason_code,
        resolution=args.resolution,
        refund_method=effective_method,
        evidence_present=bool(args.evidence_ids),
    )
    decision = decide(facts, ctx.policy, facts.now)
    early = _non_proposal_result(order.order_id, decision)
    if early is not None:
        return early

    method_str = effective_method.value if effective_method else RefundMethod.original.value
    card_payload, proposal_id, rate_limited = persist_proposal_and_build_card(
        ctx.engine,
        clock=ctx.clock,
        policy=ctx.policy,
        request_id=ctx.customer_ctx.request_id,
        session_id=ctx.session_id,
        customer_id=customer_id,
        order=order,
        proposal_type="return",
        requested_items=requested_items,
        facts=facts,
        decision=decision,
        effective_outcome=DecisionOutcome(decision.decision),
        method_str=method_str,
        resolution_str=args.resolution.value,
        sanitized_reason_text=sanitize_reason_text(args.reason_text),
        extra_payload={
            "reason_code": args.reason_code.value,
            "evidence_ids": list(args.evidence_ids or []),
        },
    )
    if rate_limited or card_payload is None or proposal_id is None:
        return _rate_limited_result()
    return ToolResult(ok=True, data=card_payload, fact_ids=[order.order_id, proposal_id])


async def handle_propose_refund(ctx: ToolContext, args: ProposeRefundArgs) -> ToolResult:
    """Create a deterministic refund-without-return proposal (always requires approval)."""
    customer_id = ctx.customer_ctx.principal.customer_id
    if not customer_id:
        return ToolResult(
            ok=False,
            error_code=ToolErrorCode.not_permitted,
            error_message=get_customer_safe_message(ToolErrorCode.not_permitted),
        )
    if ctx.engine is None:
        return ToolResult(
            ok=False,
            error_code=ToolErrorCode.internal,
            error_message=get_customer_safe_message(ToolErrorCode.internal),
        )

    order = await ctx.store_client.get_order(ctx.customer_ctx, args.order_id)
    if order.customer_id != customer_id:
        raise StoreNotFound(f"Order {args.order_id} not found")

    if order.payment_method == PaymentMethod.cod and args.refund_method == RefundMethod.original:
        return ToolResult(
            ok=False,
            error_code=ToolErrorCode.invalid_args,
            error_message=get_customer_safe_message(ToolErrorCode.invalid_args),
        )

    if args.items and len(args.items) > 0:
        requested_items = [ReturnItem(item_id=it.item_id, qty=it.qty) for it in args.items]
    else:
        requested_items = [ReturnItem(item_id=it.item_id, qty=it.qty) for it in order.items]

    existing_card, existing_id, rate_limited = check_limits_readonly(
        ctx.engine,
        customer_id=customer_id,
        order_id=order.order_id,
        item_sig=canonical_item_signature(requested_items),
        clock=ctx.clock,
    )
    if existing_card is not None and existing_id is not None:
        return ToolResult(ok=True, data=existing_card, fact_ids=[order.order_id, existing_id])
    if rate_limited:
        return _rate_limited_result()

    if args.refund_method is not None:
        effective_method: RefundMethod | None = args.refund_method
    elif order.payment_method == PaymentMethod.prepaid:
        effective_method = RefundMethod.original
    else:
        effective_method = None

    facts = await _fetch_decision_facts(
        ctx,
        order=order,
        customer_id=customer_id,
        action=DecisionAction.refund_request,
        requested_items=requested_items,
        reason_code=ReturnReasonCode.defective,
        resolution=ReturnResolution.refund,
        refund_method=effective_method,
        evidence_present=False,
    )
    decision = decide(facts, ctx.policy, facts.now)
    early = _non_proposal_result(order.order_id, decision)
    if early is not None:
        return early

    # Spec §5 W4 & §6.2: Refund requests without a return ALWAYS need seller approval
    effective_outcome = DecisionOutcome.needs_approval
    if decision.required_approver_role is None:
        role = (
            ApproverRole.seller_owner
            if decision.amounts.total_paise > ctx.policy.staff_approval_limit_paise
            else ApproverRole.seller_staff
        )
        decision = decision.model_copy(
            update={"decision": effective_outcome, "required_approver_role": role}
        )

    method_str = effective_method.value if effective_method else RefundMethod.original.value
    card_payload, proposal_id, rate_limited = persist_proposal_and_build_card(
        ctx.engine,
        clock=ctx.clock,
        policy=ctx.policy,
        request_id=ctx.customer_ctx.request_id,
        session_id=ctx.session_id,
        customer_id=customer_id,
        order=order,
        proposal_type="refund",
        requested_items=requested_items,
        facts=facts,
        decision=decision,
        effective_outcome=effective_outcome,
        method_str=method_str,
        resolution_str=ReturnResolution.refund.value,
        sanitized_reason_text="",
        extra_payload={"cause": args.cause.value},
    )
    if rate_limited or card_payload is None or proposal_id is None:
        return _rate_limited_result()
    return ToolResult(ok=True, data=card_payload, fact_ids=[order.order_id, proposal_id])


async def handle_propose_cancellation(
    ctx: ToolContext, args: ProposeCancellationArgs
) -> ToolResult:
    """Create a deterministic pre-dispatch order cancellation proposal."""
    customer_id = ctx.customer_ctx.principal.customer_id
    if not customer_id:
        return ToolResult(
            ok=False,
            error_code=ToolErrorCode.not_permitted,
            error_message=get_customer_safe_message(ToolErrorCode.not_permitted),
        )
    if ctx.engine is None:
        return ToolResult(
            ok=False,
            error_code=ToolErrorCode.internal,
            error_message=get_customer_safe_message(ToolErrorCode.internal),
        )

    order = await ctx.store_client.get_order(ctx.customer_ctx, args.order_id)
    if order.customer_id != customer_id:
        raise StoreNotFound(f"Order {args.order_id} not found")

    requested_items = [ReturnItem(item_id=it.item_id, qty=it.qty) for it in order.items]
    existing_card, existing_id, rate_limited = check_limits_readonly(
        ctx.engine,
        customer_id=customer_id,
        order_id=order.order_id,
        item_sig=canonical_item_signature(requested_items),
        clock=ctx.clock,
    )
    if existing_card is not None and existing_id is not None:
        return ToolResult(ok=True, data=existing_card, fact_ids=[order.order_id, existing_id])
    if rate_limited:
        return _rate_limited_result()

    facts = await _fetch_decision_facts(
        ctx,
        order=order,
        customer_id=customer_id,
        action=DecisionAction.cancellation,
        requested_items=requested_items,
        reason_code=ReturnReasonCode.changed_mind,
        resolution=ReturnResolution.refund,
        refund_method=RefundMethod.original,
        evidence_present=False,
    )
    decision = decide(facts, ctx.policy, facts.now)
    early = _non_proposal_result(order.order_id, decision)
    if early is not None:
        return early

    card_payload, proposal_id, rate_limited = persist_proposal_and_build_card(
        ctx.engine,
        clock=ctx.clock,
        policy=ctx.policy,
        request_id=ctx.customer_ctx.request_id,
        session_id=ctx.session_id,
        customer_id=customer_id,
        order=order,
        proposal_type="cancel",
        requested_items=requested_items,
        facts=facts,
        decision=decision,
        effective_outcome=DecisionOutcome(decision.decision),
        method_str=RefundMethod.original.value,
        resolution_str=ReturnResolution.refund.value,
        sanitized_reason_text=sanitize_reason_text(args.reason_text),
        extra_payload={"reason_code": ReturnReasonCode.changed_mind.value},
    )
    if rate_limited or card_payload is None or proposal_id is None:
        return _rate_limited_result()
    return ToolResult(ok=True, data=card_payload, fact_ids=[order.order_id, proposal_id])


def build_propose_tools() -> dict[str, Tool]:
    """Instantiate all propose tools with ToolKind.PROPOSE."""
    return {
        "propose_return": Tool(
            name="propose_return",
            description=(
                "Create a customer return proposal for confirmation. Never executes a write "
                "directly on the store. Do not use for unowned orders or pass amounts."
            ),
            args_model=ProposeReturnArgs,
            kind=ToolKind.PROPOSE,
            handler=handle_propose_return,
        ),
        "propose_refund": Tool(
            name="propose_refund",
            description=(
                "Create a refund proposal without a return (for lost or undelivered orders). "
                "Always requires seller approval."
            ),
            args_model=ProposeRefundArgs,
            kind=ToolKind.PROPOSE,
            handler=handle_propose_refund,
        ),
        "propose_cancellation": Tool(
            name="propose_cancellation",
            description=(
                "Create a pre-dispatch order cancellation proposal for customer confirmation."
            ),
            args_model=ProposeCancellationArgs,
            kind=ToolKind.PROPOSE,
            handler=handle_propose_cancellation,
        ),
    }
