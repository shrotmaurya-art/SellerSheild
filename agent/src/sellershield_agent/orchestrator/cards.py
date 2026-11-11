"""UI Cards formatting and sanitization for SSE stream."""

from __future__ import annotations

import html
import re
from typing import Any

_HTML_TAG_REGEX = re.compile(r"<[^>]*>")
_CONTROL_CHAR_REGEX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _non_bool_int(value: Any, default: int = 0) -> int:
    """Return value as int only when it is a genuine non-bool integer."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def sanitize_text(value: Any, max_length: int = 200) -> str:
    """Iteratively strip HTML tags/brackets and control characters, then truncate."""
    raw = value if isinstance(value, str) else ("" if value is None else str(value))
    cleaned = raw
    while True:
        next_val = _HTML_TAG_REGEX.sub("", cleaned)
        if next_val == cleaned:
            break
        cleaned = next_val
    cleaned = cleaned.replace("<", "").replace(">", "")
    cleaned = _CONTROL_CHAR_REGEX.sub("", cleaned).strip()
    if len(cleaned) > max_length:
        return cleaned[:max_length]
    return cleaned


def sanitize_card_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitize string fields in card payloads."""
    result: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, str):
            result[k] = html.escape(v)[:200]
        elif isinstance(v, dict):
            result[k] = sanitize_card_payload(v)
        elif isinstance(v, list):
            new_list: list[Any] = []
            for item in v:
                if isinstance(item, str):
                    new_list.append(html.escape(item)[:200])
                elif isinstance(item, dict):
                    new_list.append(sanitize_card_payload(item))
                else:
                    new_list.append(item)
            result[k] = new_list
        else:
            result[k] = v
    return result


def build_chips_card(chips: list[dict[str, str]]) -> dict[str, Any]:
    """Build an interactive chips card."""
    clean_chips = []
    for chip in chips:
        clean_chips.append(
            {
                "label": sanitize_text(chip.get("label", ""), 50),
                "action": sanitize_text(chip.get("action", ""), 50),
                "order_id": (
                    sanitize_text(chip.get("order_id", ""), 32) if chip.get("order_id") else None
                ),
            }
        )
    return {
        "card_type": "chips",
        "payload": {"chips": clean_chips},
    }


def build_order_summary_card(order_data: dict[str, Any]) -> dict[str, Any]:
    """Build order summary card from store order data."""
    items = []
    raw_items = order_data.get("items") or []
    for item in raw_items:
        title = item.get("name") or item.get("title") or ""
        qty = item.get("qty") or item.get("quantity") or 1
        price = (
            item.get("line_paid_paise")
            or item.get("unit_price_paise")
            or item.get("price_paise")
            or 0
        )
        items.append(
            {
                "itemId": sanitize_text(item.get("item_id", ""), 64),
                "name": sanitize_text(title, 100),
                "title": sanitize_text(title, 100),
                "qty": qty,
                "quantity": qty,
                "price_paise": price,
                "linePaidPaise": price,
                "sku": sanitize_text(item.get("sku", ""), 64),
            }
        )
    oid = order_data.get("order_id") or order_data.get("id") or ""
    placed = order_data.get("placed_at") or order_data.get("created_at") or ""
    total = order_data.get("total_paise", 0)
    raw_payload = {
        "orderId": sanitize_text(oid, 32),
        "order_id": sanitize_text(oid, 32),
        "status": sanitize_text(order_data.get("status", ""), 32),
        "placedAt": sanitize_text(placed, 32),
        "created_at": sanitize_text(placed, 32),
        "subtotalPaise": order_data.get("subtotal_paise", total),
        "discountPaise": order_data.get("discount_paise", 0),
        "shippingFeePaise": order_data.get("shipping_fee_paise", 0),
        "totalPaise": total,
        "total_paise": total,
        "currency": "INR",
        "items": items,
    }
    return {
        "card_type": "order_summary",
        "payload": sanitize_card_payload(raw_payload),
    }


def build_shipment_timeline_card(shipment_data: dict[str, Any]) -> dict[str, Any]:
    """Build shipment timeline card from shipping data."""
    raw_events = shipment_data.get("events") or shipment_data.get("timeline") or []
    events = []
    for ev in raw_events:
        ts = ev.get("occurred_at") or ev.get("timestamp") or ""
        desc = ev.get("note") or ev.get("description") or ""
        st = ev.get("status") or ""
        loc = ev.get("location") or ""
        events.append(
            {
                "eventId": sanitize_text(ev.get("event_id", ""), 32),
                "ts": sanitize_text(ts, 32),
                "timestamp": sanitize_text(ts, 32),
                "status": sanitize_text(st, 32),
                "location": sanitize_text(loc, 64),
                "note": sanitize_text(desc, 150),
                "description": sanitize_text(desc, 150),
            }
        )
    oid = shipment_data.get("order_id") or shipment_data.get("id") or ""
    eta = shipment_data.get("eta_at") or shipment_data.get("eta") or ""
    raw_payload = {
        "orderId": sanitize_text(oid, 32),
        "order_id": sanitize_text(oid, 32),
        "shipment_id": sanitize_text(
            shipment_data.get("shipment_id", shipment_data.get("id", "")), 32
        ),
        "carrier": sanitize_text(shipment_data.get("carrier", ""), 32),
        "trackingNo": sanitize_text(shipment_data.get("tracking_no", ""), 64),
        "tracking_no": sanitize_text(shipment_data.get("tracking_no", ""), 64),
        "status": sanitize_text(shipment_data.get("status", ""), 32),
        "etaAt": sanitize_text(eta, 32),
        "eta": sanitize_text(eta, 32),
        "delayed": bool(shipment_data.get("delayed", False)),
        "events": events,
        "timeline": events,
    }
    return {
        "card_type": "shipment_timeline",
        "payload": sanitize_card_payload(raw_payload),
    }


def build_confirmation_card(data: dict[str, Any]) -> dict[str, Any]:
    """Build strict allowlisted confirmation card without leaking internal/untrusted fields."""
    raw_items = data.get("items")
    clean_items: list[dict[str, Any]] = []
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            raw_qty = item.get("quantity", item.get("qty", 1))
            qty = _non_bool_int(raw_qty, 1)
            clean_items.append(
                {
                    "item_id": sanitize_text(item.get("item_id", ""), 64),
                    "quantity": qty,
                    "name": sanitize_text(item.get("name", ""), 120),
                }
            )

    raw_amounts = data.get("amounts")
    amts = raw_amounts if isinstance(raw_amounts, dict) else {}
    clean_amounts: dict[str, Any] = {
        "items_paise": _non_bool_int(amts.get("items_paise"), 0),
        "shipping_refund_paise": _non_bool_int(amts.get("shipping_refund_paise"), 0),
        "fee_paise": _non_bool_int(amts.get("fee_paise"), 0),
        "total_paise": _non_bool_int(amts.get("total_paise"), 0),
    }
    if "capped" in amts:
        clean_amounts["capped"] = bool(amts["capped"])
    for display_key in ("display", "items_display", "shipping_refund_display", "fee_display"):
        if display_key in amts and isinstance(amts[display_key], str):
            clean_amounts[display_key] = sanitize_text(amts[display_key], 64)

    payload: dict[str, Any] = {
        "proposal_id": sanitize_text(data.get("proposal_id", ""), 64),
        "proposal_hash": sanitize_text(data.get("proposal_hash", ""), 64),
        "proposal_type": sanitize_text(data.get("proposal_type", ""), 32),
        "order_id": sanitize_text(data.get("order_id", ""), 32),
        "items": clean_items,
        "amounts": clean_amounts,
        "method": sanitize_text(data.get("method", ""), 32),
        "resolution": sanitize_text(data.get("resolution", ""), 32),
        "decision": sanitize_text(data.get("decision", ""), 32),
        "policy_version": sanitize_text(data.get("policy_version", ""), 32),
        "capped": bool(data.get("capped", False)),
        "expires_at": sanitize_text(data.get("expires_at", ""), 32),
    }
    review_stmt = data.get("review_statement")
    if isinstance(review_stmt, str) and review_stmt.strip():
        payload["review_statement"] = sanitize_text(review_stmt, 200)

    return {
        "card_type": "confirmation",
        "payload": payload,
    }


def build_return_preview_card(data: dict[str, Any]) -> dict[str, Any]:
    """Build strict allowlisted return_preview card excluding seller/internal fields."""
    raw_rules = data.get("rule_results")
    clean_rules: list[dict[str, Any]] = []
    if isinstance(raw_rules, list):
        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue
            clean_rules.append(
                {
                    "rule_id": sanitize_text(rule.get("rule_id", rule.get("id", "")), 32),
                    "passed": bool(rule.get("passed", False)),
                    "customer_text": sanitize_text(rule.get("customer_text", ""), 200),
                }
            )

    payload: dict[str, Any] = {
        "decision": sanitize_text(data.get("decision", ""), 32),
        "total_refund_paise": _non_bool_int(data.get("total_refund_paise"), 0),
        "items_paise": _non_bool_int(data.get("items_paise"), 0),
        "shipping_refund_paise": _non_bool_int(data.get("shipping_refund_paise"), 0),
        "shipping_fee_deduction_paise": _non_bool_int(
            data.get("shipping_fee_deduction_paise", data.get("fee_paise")),
            0,
        ),
        "capped": bool(data.get("capped", False)),
        "rule_results": clean_rules,
    }
    order_id = data.get("order_id")
    if isinstance(order_id, str) and order_id.strip():
        payload["order_id"] = sanitize_text(order_id, 32)

    return {
        "card_type": "return_preview",
        "payload": payload,
    }


def build_case_created_card(
    case_id: str,
    priority: str = "normal",
    sla: str = "within 24 hours",
) -> dict[str, Any]:
    """Build case_created card for client chat UI."""
    return {
        "card_type": "case_created",
        "payload": {
            "case_id": sanitize_text(case_id, 64),
            "priority": sanitize_text(priority, 32),
            "sla": sanitize_text(sla, 64),
        },
    }

