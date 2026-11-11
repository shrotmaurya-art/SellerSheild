"""Read tools implementations for SellerShield AI.

Spec §6.1, §6.2, §13, §21 (Amendment 1 & 6):
- All 8 read tools: list_my_orders, get_order, get_shipping_status,
  get_return_status, get_refund_status, get_policy_summary,
  check_return_eligibility, preview_refund.
- Strict Pydantic models with extra='forbid'.
- Minimal customer-safe outputs: IDs, statuses, item names, dates, amounts.
- Invariant: Zero PII (address, phone, email).
- Invariant: Zero risk scores, rule weights, or internal approval limits.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sellershield_agent.domain.models import (
    CustomerRiskFacts,
    ReturnItem,
    ReturnReasonCode,
    ReturnResolution,
)
from sellershield_agent.policy.decide import decide
from sellershield_agent.policy.facts import DecisionAction, DecisionFacts
from sellershield_agent.policy.refund import compute_refund
from sellershield_agent.policy.summary import customer_safe_summary
from sellershield_agent.store.client import StoreNotFound
from sellershield_agent.tools.base import (
    Tool,
    ToolContext,
    ToolErrorCode,
    ToolKind,
    ToolResult,
)

# ── Argument Models ─────────────────────────────────────────────────


class ListMyOrdersArgs(BaseModel):
    """Arguments for listing orders."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GetOrderArgs(BaseModel):
    """Arguments for fetching a single order."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    order_id: str


class GetShippingStatusArgs(BaseModel):
    """Arguments for fetching shipment tracking."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    order_id: str


class GetReturnStatusArgs(BaseModel):
    """Arguments for fetching return status."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    order_id: str | None = None
    return_id: str | None = None


class GetRefundStatusArgs(BaseModel):
    """Arguments for fetching refund status."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    order_id: str | None = None
    refund_id: str | None = None


class GetPolicySummaryArgs(BaseModel):
    """Arguments for fetching store policy summary."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    category: str | None = None


class OrderItemInput(BaseModel):
    """Item and quantity requested for return or refund preview."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: str
    qty: int = Field(ge=1)


class CheckReturnEligibilityArgs(BaseModel):
    """Arguments for checking item return eligibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    order_id: str
    items: list[OrderItemInput]
    reason_code: ReturnReasonCode
    reason_text: str | None = None
    resolution: ReturnResolution = ReturnResolution.refund


class PreviewRefundArgs(BaseModel):
    """Arguments for previewing refund amount breakdown."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    order_id: str
    items: list[OrderItemInput]
    reason_code: ReturnReasonCode
    resolution: ReturnResolution = ReturnResolution.refund


# ── Tool Handlers ───────────────────────────────────────────────────


async def handle_list_my_orders(ctx: ToolContext, args: ListMyOrdersArgs) -> ToolResult:
    orders = await ctx.store_client.list_orders(ctx.customer_ctx)
    digests = [
        {
            "order_id": o.order_id,
            "status": o.status.value,
            "placed_at": o.placed_at.isoformat(),
            "total_paise": o.total_paise,
            "items": [
                {
                    "item_id": it.item_id,
                    "name": it.name,
                    "qty": it.qty,
                    "unit_price_paise": it.unit_price_paise,
                }
                for it in o.items
            ],
        }
        for o in orders
    ]
    return ToolResult(ok=True, data={"orders": digests})


async def handle_get_order(ctx: ToolContext, args: GetOrderArgs) -> ToolResult:
    order = await ctx.store_client.get_order(ctx.customer_ctx, args.order_id)
    if order is None:
        raise StoreNotFound(f"Order {args.order_id} not found")
    data = {
        "order_id": order.order_id,
        "status": order.status.value,
        "payment_method": order.payment_method.value,
        "subtotal_paise": order.subtotal_paise,
        "discount_paise": order.discount_paise,
        "shipping_fee_paise": order.shipping_fee_paise,
        "total_paise": order.total_paise,
        "placed_at": order.placed_at.isoformat(),
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "items": [
            {
                "item_id": it.item_id,
                "name": it.name,
                "sku": it.sku,
                "qty": it.qty,
                "unit_price_paise": it.unit_price_paise,
                "line_paid_paise": it.line_paid_paise,
            }
            for it in order.items
        ],
    }
    return ToolResult(ok=True, data=data, fact_ids=[f"order:{order.order_id}"])


async def handle_get_shipping_status(ctx: ToolContext, args: GetShippingStatusArgs) -> ToolResult:
    shipment = await ctx.store_client.get_shipment(ctx.customer_ctx, args.order_id)
    if shipment is None:
        raise StoreNotFound(f"Shipment for order {args.order_id} not found")
    last_events = shipment.events[-3:] if shipment.events else []
    data = {
        "order_id": shipment.order_id,
        "carrier": shipment.carrier,
        "tracking_no": shipment.tracking_no,
        "status": shipment.status.value,
        "eta_at": shipment.eta_at.isoformat() if shipment.eta_at else None,
        "delayed": shipment.delayed,
        "events": [
            {
                "occurred_at": ev.occurred_at.isoformat(),
                "status": ev.status.value,
                "location": ev.location,
                "note": ev.note,
            }
            for ev in last_events
        ],
    }
    return ToolResult(ok=True, data=data, fact_ids=[f"shipment:{shipment.shipment_id}"])


async def handle_get_return_status(ctx: ToolContext, args: GetReturnStatusArgs) -> ToolResult:
    if not args.order_id and not args.return_id:
        return ToolResult(
            ok=False,
            error_code=ToolErrorCode.invalid_args,
            error_message="Either order_id or return_id must be provided.",
        )

    if args.return_id and not args.order_id:
        r = await ctx.store_client.get_return(ctx.customer_ctx, args.return_id)
        returns = [r]
    else:
        order_id = args.order_id or ""
        returns = await ctx.store_client.list_returns(ctx.customer_ctx, order_id)
        if args.return_id:
            returns = [r for r in returns if r.return_id == args.return_id]

    data = [
        {
            "return_id": r.return_id,
            "order_id": r.order_id,
            "status": r.status.value,
            "reason_code": r.reason_code.value,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "items": [{"item_id": it.item_id, "qty": it.qty} for it in r.items],
        }
        for r in returns
    ]
    return ToolResult(
        ok=True,
        data={"returns": data},
        fact_ids=[f"return:{r.return_id}" for r in returns],
    )


async def handle_get_refund_status(ctx: ToolContext, args: GetRefundStatusArgs) -> ToolResult:
    if not args.order_id and not args.refund_id:
        return ToolResult(
            ok=False,
            error_code=ToolErrorCode.invalid_args,
            error_message="Either order_id or refund_id must be provided.",
        )

    if args.refund_id and not args.order_id:
        rf = await ctx.store_client.get_refund(ctx.customer_ctx, args.refund_id)
        refunds = [rf]
    else:
        order_id = args.order_id or ""
        refunds = await ctx.store_client.list_refunds(ctx.customer_ctx, order_id)
        if args.refund_id:
            refunds = [rf for rf in refunds if rf.refund_id == args.refund_id]

    data = [
        {
            "refund_id": rf.refund_id,
            "order_id": rf.order_id,
            "status": rf.status.value,
            "amount_paise": rf.amount_paise,
            "method": rf.method.value,
            "created_at": rf.created_at.isoformat(),
            "updated_at": rf.updated_at.isoformat() if rf.updated_at else None,
        }
        for rf in refunds
    ]
    return ToolResult(
        ok=True,
        data={"refunds": data},
        fact_ids=[f"refund:{rf.refund_id}" for rf in refunds],
    )


async def handle_get_policy_summary(ctx: ToolContext, args: GetPolicySummaryArgs) -> ToolResult:
    summary = customer_safe_summary(ctx.policy)
    data: dict[str, Any] = {
        "version": ctx.policy.version,
        "summary": str(summary),
        "windows": summary.windows,
        "return_shipping": summary.return_shipping,
        "refund_timing": summary.refund_timing,
    }
    if args.category and args.category in summary.windows:
        data["category_window_days"] = summary.windows[args.category]
    return ToolResult(ok=True, data=data)


async def handle_check_return_eligibility(
    ctx: ToolContext, args: CheckReturnEligibilityArgs
) -> ToolResult:
    order = await ctx.store_client.get_order(ctx.customer_ctx, args.order_id)
    req_items = [(it.item_id, it.qty) for it in args.items]
    refundable = await ctx.store_client.get_refundable(
        ctx.customer_ctx, args.order_id, items=req_items
    )
    active_returns = await ctx.store_client.list_returns(ctx.customer_ctx, args.order_id)

    shipment = None
    try:
        shipment = await ctx.store_client.get_shipment(ctx.customer_ctx, args.order_id)
    except StoreNotFound:
        shipment = None

    # Fetch risk facts using internal service context if available
    customer_id = ctx.customer_ctx.principal.customer_id or order.customer_id
    if ctx.service_ctx_factory:
        service_ctx = ctx.service_ctx_factory(ctx.customer_ctx.request_id)
        risk_facts = await ctx.store_client.get_customer_risk_facts(service_ctx, customer_id)
    else:
        risk_facts = CustomerRiskFacts(
            customer_id=customer_id,
            returns_30d=0,
            refund_ratio_90d_bp=0,
            account_age_days=30,
            refunded_30d_paise=0,
        )

    facts = DecisionFacts(
        action=DecisionAction.return_,
        order=order,
        items=[ReturnItem(item_id=it.item_id, qty=it.qty) for it in args.items],
        reason_code=args.reason_code,
        reason_text=args.reason_text,
        resolution=args.resolution,
        refundable=refundable,
        customer_risk=risk_facts,
        active_returns=active_returns,
        shipment=shipment,
        now=ctx.clock.now(),
    )

    decision = decide(facts, ctx.policy, facts.now)

    # Customer-safe minimal output: strictly exclude seller_text, risk scores, limits
    rule_results_safe = [
        {
            "rule_id": r.id,
            "passed": r.passed,
            "customer_text": r.customer_text,
        }
        for r in decision.rule_results
    ]

    data = {
        "decision": decision.decision.value,
        "total_refund_paise": decision.amounts.total_paise,
        "items_paise": decision.amounts.items_paise,
        "shipping_fee_deduction_paise": decision.amounts.fee_paise,
        "rule_results": rule_results_safe,
    }
    return ToolResult(ok=True, data=data)


async def handle_preview_refund(ctx: ToolContext, args: PreviewRefundArgs) -> ToolResult:
    order = await ctx.store_client.get_order(ctx.customer_ctx, args.order_id)
    req_items = [(it.item_id, it.qty) for it in args.items]
    refundable = await ctx.store_client.get_refundable(
        ctx.customer_ctx, args.order_id, items=req_items
    )

    whole_order_returned = len(args.items) == len(order.items) and all(
        any(it.item_id == o_it.item_id and it.qty == o_it.qty for o_it in order.items)
        for it in args.items
    )
    breakdown = compute_refund(
        order=order,
        requested_items=[ReturnItem(item_id=it.item_id, qty=it.qty) for it in args.items],
        reason_code=args.reason_code,
        resolution=args.resolution,
        policy=ctx.policy,
        whole_order_returned=whole_order_returned,
        shipment_lost=False,
        max_refundable_paise=refundable.max_refundable_paise,
    )

    is_credit = args.resolution == ReturnResolution.store_credit
    data = {
        "items_total_paise": breakdown.items_paise,
        "shipping_refund_paise": breakdown.shipping_refund_paise,
        "return_fee_paise": breakdown.fee_paise,
        "total_refund_paise": breakdown.total_paise,
        "method": "store_credit" if is_credit else "original",
    }
    return ToolResult(ok=True, data=data)


# ── Tool Catalog Builder ───────────────────────────────────────────


def build_read_tools() -> dict[str, Tool]:
    """Build and return registry of the 8 safe read tools."""
    return {
        "list_my_orders": Tool(
            name="list_my_orders",
            description=(
                "List recent orders placed by the authenticated customer. "
                "Do not use this tool if an order ID is already known "
                "or if looking up details of a single order."
            ),
            args_model=ListMyOrdersArgs,
            kind=ToolKind.READ,
            handler=handle_list_my_orders,
        ),
        "get_order": Tool(
            name="get_order",
            description=(
                "Fetch details and line items of a single order by its order ID. "
                "Do not use this tool to list multiple orders or check carrier tracking."
            ),
            args_model=GetOrderArgs,
            kind=ToolKind.READ,
            handler=handle_get_order,
        ),
        "get_shipping_status": Tool(
            name="get_shipping_status",
            description=(
                "Get carrier tracking status and ETA for an order. "
                "Do not use this tool for return status or general order info."
            ),
            args_model=GetShippingStatusArgs,
            kind=ToolKind.READ,
            handler=handle_get_shipping_status,
        ),
        "get_return_status": Tool(
            name="get_return_status",
            description=(
                "Check return request status and timeline by order ID or return ID. "
                "Do not use this tool to check refunds or shipment status."
            ),
            args_model=GetReturnStatusArgs,
            kind=ToolKind.READ,
            handler=handle_get_return_status,
        ),
        "get_refund_status": Tool(
            name="get_refund_status",
            description=(
                "Check payout status and method of refunds by order ID or refund ID. "
                "Do not use this tool to calculate preview amounts."
            ),
            args_model=GetRefundStatusArgs,
            kind=ToolKind.READ,
            handler=handle_get_refund_status,
        ),
        "get_policy_summary": Tool(
            name="get_policy_summary",
            description=(
                "Get customer-safe store return windows and policies. "
                "Do not use this tool to check eligibility of a specific customer order."
            ),
            args_model=GetPolicySummaryArgs,
            kind=ToolKind.READ,
            handler=handle_get_policy_summary,
        ),
        "check_return_eligibility": Tool(
            name="check_return_eligibility",
            description=(
                "Evaluate whether items in an order are eligible for return under store policy. "
                "Do not use this tool to initiate a return write."
            ),
            args_model=CheckReturnEligibilityArgs,
            kind=ToolKind.READ,
            handler=handle_check_return_eligibility,
        ),
        "preview_refund": Tool(
            name="preview_refund",
            description=(
                "Calculate estimated refund amounts and deductions for items. "
                "Do not use this tool to verify category eligibility."
            ),
            args_model=PreviewRefundArgs,
            kind=ToolKind.READ,
            handler=handle_preview_refund,
        ),
    }
