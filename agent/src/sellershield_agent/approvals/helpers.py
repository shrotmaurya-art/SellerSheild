"""Deterministic helpers for seller approval cards, sanitization, and live store reads."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sellershield_agent.domain.models import (
    CustomerRiskFacts,
    Order,
    Refund,
    Refundable,
    RefundableItem,
    RefundStatus,
    ReturnItem,
    ReturnRequest,
    ReturnStatus,
    Shipment,
)
from sellershield_agent.execution.common import format_utc_iso
from sellershield_agent.store.client import (
    RequestContext,
    StoreClient,
    StoreNotFound,
)

MAX_STATEMENT_CHARS = 200
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_RE = re.compile(r"(https?)://", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
STEP_UP_MAX_AGE_SECONDS = 900
STEP_UP_FUTURE_SKEW_SECONDS = 30


@dataclass(frozen=True)
class LiveStoreBundle:
    """Live facts fetched from StoreClient outside any database transaction."""

    order: Order | None
    customer_risk: CustomerRiskFacts | None
    raw_refundable: Refundable | None
    adjusted_refundable: Refundable | None
    all_returns: list[ReturnRequest]
    other_active_returns: list[ReturnRequest]
    all_refunds: list[Refund]
    shipment: Shipment | None
    own_return: ReturnRequest | None
    own_refund: Refund | None


def sanitize_customer_statement(raw: str | None) -> tuple[str, bool]:
    """Normalize, defang links, truncate to 200 chars, and HTML-escape customer text.

    Escaping happens AFTER length slicing so raw HTML entities are never split mid-entity,
    and active angle brackets are always converted into safe HTML entities.
    """
    if not raw or not isinstance(raw, str):
        return "", False
    no_ctrl = _CONTROL_CHARS_RE.sub(" ", raw)
    collapsed = _WHITESPACE_RE.sub(" ", no_ctrl).strip()
    defanged = _URL_RE.sub(
        lambda m: "hxxps://" if m.group(1).lower().endswith("s") else "hxxp://",
        collapsed,
    )
    truncated = len(defanged) > MAX_STATEMENT_CHARS
    snippet = defanged[:MAX_STATEMENT_CHARS]
    escaped = html.escape(snippet, quote=True)
    return escaped, truncated


def sanitize_seller_note(raw: str | None, *, max_len: int = 1000) -> str | None:
    """Clean control characters from seller-provided reason/note while preserving readable text."""
    if raw is None:
        return None
    cleaned = _CONTROL_CHARS_RE.sub(" ", raw).strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def sanitize_order_for_card(order: Order | None) -> dict[str, Any] | None:
    """Project an Order onto a strict zero-PII dictionary for the seller approval card."""
    if order is None:
        return None
    return {
        "order_id": order.order_id,
        "status": order.status.value,
        "payment_method": order.payment_method.value,
        "payment_status": order.payment_status.value,
        "subtotal_paise": int(order.subtotal_paise),
        "discount_paise": int(order.discount_paise),
        "shipping_fee_paise": int(order.shipping_fee_paise),
        "total_paise": int(order.total_paise),
        "placed_at": format_utc_iso(order.placed_at),
        "shipped_at": format_utc_iso(order.shipped_at) if order.shipped_at else None,
        "delivered_at": format_utc_iso(order.delivered_at) if order.delivered_at else None,
        "cancelled_at": format_utc_iso(order.cancelled_at) if order.cancelled_at else None,
        "version": int(order.version),
        "items": [
            {
                "item_id": it.item_id,
                "sku": it.sku,
                "name": it.name,
                "category": it.category,
                "qty": int(it.qty),
                "unit_price_paise": int(it.unit_price_paise),
                "discount_alloc_paise": int(it.discount_alloc_paise),
                "line_paid_paise": int(it.line_paid_paise),
                "flags": dict(it.flags),
            }
            for it in order.items
        ],
    }


def is_default_pipeline_risk(raw_risk: Mapping[str, Any] | None, customer_id: str) -> bool:
    """Return True if stored customer_risk is the synthetic default fallback from ToolGateway."""
    if not isinstance(raw_risk, Mapping):
        return False
    return (
        str(raw_risk.get("customer_id", "")) == customer_id
        and raw_risk.get("returns_30d") == 0
        and raw_risk.get("refund_ratio_90d_bp") == 0
        and raw_risk.get("account_age_days") == 30
        and raw_risk.get("refunded_30d_paise") == 0
    )


def build_adjusted_refundable(
    *,
    order: Order,
    live_returns: list[ReturnRequest],
    live_refunds: list[Refund],
    own_return_id: str | None,
    own_refund_id: str | None,
) -> Refundable:
    """Recompute pre-hold Refundable excluding this approval's own return & refund hold.

    Works identically regardless of whether own_refund is currently reserved or authorized.
    """
    other_refunds_total = sum(
        int(r.amount_paise)
        for r in live_refunds
        if r.refund_id != own_refund_id
        and r.status
        in (
            RefundStatus.reserved,
            RefundStatus.authorized,
            RefundStatus.processing,
            RefundStatus.completed,
        )
    )
    max_order_refundable = max(0, int(order.total_paise) - other_refunds_total)

    ref_items: list[RefundableItem] = []
    for item in order.items:
        returned_qty = sum(
            int(ri.qty)
            for ret in live_returns
            if ret.return_id != own_return_id
            and ret.status not in (ReturnStatus.cancelled, ReturnStatus.rejected)
            for ri in ret.items
            if ri.item_id == item.item_id
        )
        rem_qty = max(0, int(item.qty) - returned_qty)
        item_ref = min(int(item.line_paid_paise), max_order_refundable)
        ref_items.append(
            RefundableItem(
                item_id=item.item_id,
                original_qty=int(item.qty),
                remaining_qty=rem_qty,
                line_paid_paise=int(item.line_paid_paise),
                refundable_paise=item_ref,
            )
        )
    return Refundable(
        order_id=order.order_id,
        max_refundable_paise=max_order_refundable,
        items=ref_items,
    )


async def fetch_live_store_bundle(
    *,
    store_client: StoreClient,
    svc_ctx: RequestContext,
    order_id: str,
    customer_id: str,
    req_pairs: list[tuple[str, int]],
    return_id: str | None,
    refund_id: str | None,
    allow_missing_order: bool = False,
) -> LiveStoreBundle:
    """Fetch order, risk, returns, refunds, and shipment via ServiceContext outside DB locks."""
    try:
        order = await store_client.get_order_admin(svc_ctx, order_id)
    except StoreNotFound:
        if not allow_missing_order:
            raise
        order = None

    try:
        cust_risk: CustomerRiskFacts | None = await store_client.get_customer_risk_facts(
            svc_ctx, customer_id
        )
    except StoreNotFound:
        cust_risk = None

    if order is None:
        return LiveStoreBundle(
            order=None,
            customer_risk=cust_risk,
            raw_refundable=None,
            adjusted_refundable=None,
            all_returns=[],
            other_active_returns=[],
            all_refunds=[],
            shipment=None,
            own_return=None,
            own_refund=None,
        )

    raw_refundable = await store_client.get_refundable(svc_ctx, order_id, items=req_pairs)
    all_returns = await store_client.list_returns(svc_ctx, order_id)
    all_refunds = await store_client.list_refunds(svc_ctx, order_id)
    try:
        shipment: Shipment | None = await store_client.get_shipment(svc_ctx, order_id)
    except StoreNotFound:
        shipment = None

    own_ret: ReturnRequest | None = (
        next((r for r in all_returns if r.return_id == return_id), None) if return_id else None
    )
    if own_ret is None and return_id is not None:
        try:
            own_ret = await store_client.get_return(svc_ctx, return_id)
        except StoreNotFound:
            own_ret = None

    own_rfd: Refund | None = (
        next((r for r in all_refunds if r.refund_id == refund_id), None) if refund_id else None
    )
    if own_rfd is None and refund_id is not None:
        try:
            own_rfd = await store_client.get_refund(svc_ctx, refund_id)
        except StoreNotFound:
            own_rfd = None

    other_returns = [r for r in all_returns if r.return_id != return_id]
    adjusted_refundable = build_adjusted_refundable(
        order=order,
        live_returns=all_returns,
        live_refunds=all_refunds,
        own_return_id=return_id,
        own_refund_id=refund_id,
    )
    return LiveStoreBundle(
        order=order,
        customer_risk=cust_risk,
        raw_refundable=raw_refundable,
        adjusted_refundable=adjusted_refundable,
        all_returns=all_returns,
        other_active_returns=other_returns,
        all_refunds=all_refunds,
        shipment=shipment,
        own_return=own_ret,
        own_refund=own_rfd,
    )


def extract_items_and_pairs(
    payload: dict[str, Any],
) -> tuple[list[ReturnItem], list[tuple[str, int]]]:
    """Extract normalized ReturnItem models and (item_id, qty) pairs from proposal payload."""
    raw_items = payload.get("items", [])
    req_items: list[ReturnItem] = []
    if isinstance(raw_items, list):
        for it in raw_items:
            if not isinstance(it, dict) or not it.get("item_id"):
                continue
            raw_qty = it.get("qty", it.get("quantity", 1))
            qty = int(raw_qty) if isinstance(raw_qty, int) and not isinstance(raw_qty, bool) else 1
            req_items.append(ReturnItem(item_id=str(it["item_id"]), qty=max(1, qty)))
    return req_items, [(it.item_id, it.qty) for it in req_items]


def parse_json_dict(raw: Any) -> dict[str, Any]:
    """Parse JSON column safely into a dict."""
    try:
        parsed = json.loads(str(raw))
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}
