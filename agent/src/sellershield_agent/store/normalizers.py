"""Payload normalization helpers adapting Store API JSON to strict agent domain models."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sellershield_agent.domain.models import (
    Address,
    CustomerRefundSummary,
    Order,
    OrderItem,
    Refund,
    RefundableItem,
    ReturnItem,
    ReturnRequest,
    Shipment,
    ShipmentEvent,
    StoreSettings,
)


def ensure_aware_utc(val: Any) -> Any:
    """Normalize naive ISO strings or datetimes into timezone-aware UTC datetimes."""
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None or val.utcoffset() is None:
            return val.replace(tzinfo=UTC)
        return val.astimezone(UTC)
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            return val
        if dt.tzinfo is None or dt.utcoffset() is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return val


def _parse_json_dict(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _normalize_ship_to(raw: dict[str, Any]) -> dict[str, Any] | None:
    known_addr = set(Address.model_fields.keys())
    if "ship_to" in raw and raw["ship_to"] is not None:
        if isinstance(raw["ship_to"], dict):
            return {k: v for k, v in raw["ship_to"].items() if k in known_addr}
        return None

    addr = _parse_json_dict(raw.get("ship_to_json"))
    if not addr:
        return None

    normalized: dict[str, Any] = {
        "full_name": str(addr.get("full_name") or addr.get("name") or ""),
        "line1": str(addr.get("line1") or ""),
        "line2": addr.get("line2"),
        "city": str(addr.get("city") or ""),
        "state": str(addr.get("state") or ""),
        "postal_code": str(addr.get("postal_code") or ""),
        "country": str(addr.get("country") or ""),
        "phone": addr.get("phone"),
    }
    return {k: v for k, v in normalized.items() if k in known_addr}


def _normalize_order_item(raw: dict[str, Any]) -> dict[str, Any]:
    known_item = set(OrderItem.model_fields.keys())
    cleaned = {k: v for k, v in raw.items() if k in known_item}
    if "name" not in cleaned and "name_snapshot" in raw:
        cleaned["name"] = raw["name_snapshot"]
    if "category" not in cleaned and "category_snapshot" in raw:
        cleaned["category"] = raw["category_snapshot"]
    if "flags" not in cleaned:
        parsed_flags = _parse_json_dict(raw.get("flags_json"))
        cleaned["flags"] = parsed_flags if parsed_flags is not None else {}
    return cleaned


def clean_return_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip unknown fields and normalize UTC timestamps for ReturnRequest and ReturnItems."""
    known_ret = set(ReturnRequest.model_fields.keys())
    cleaned = {k: v for k, v in raw.items() if k in known_ret}
    for ts_field in ("created_at", "updated_at"):
        if ts_field in cleaned:
            cleaned[ts_field] = ensure_aware_utc(cleaned[ts_field])
    if "items" in cleaned and isinstance(cleaned["items"], list):
        known_item = set(ReturnItem.model_fields.keys())
        cleaned["items"] = [
            {k: v for k, v in it.items() if k in known_item}
            for it in cleaned["items"]
            if isinstance(it, dict)
        ]
    return cleaned


def clean_refund_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip unknown fields, normalize method/status/timestamps for Refund."""
    known_rfd = set(Refund.model_fields.keys())
    cleaned = {k: v for k, v in raw.items() if k in known_rfd}
    if "client_ref" not in cleaned and "idempotency_key" in raw:
        cleaned["client_ref"] = raw["idempotency_key"]

    method_val = cleaned.get("method")
    if method_val in ("original_payment", "card", "upi"):
        cleaned["method"] = "original"

    status_val = cleaned.get("status")
    if status_val == "cancelled":
        cleaned["status"] = "released"
    elif (
        status_val == "reserved"
        and cleaned.get("authorized_by")
        and cleaned.get("authorized_amount_paise") is not None
    ):
        cleaned["status"] = "authorized"

    for ts_field in ("authorized_at", "created_at", "updated_at"):
        if ts_field in cleaned:
            cleaned[ts_field] = ensure_aware_utc(cleaned[ts_field])
    return cleaned


def clean_order_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip unknown fields and normalize store contract fields/enums for Order."""
    known_order = set(Order.model_fields.keys())
    cleaned = {k: v for k, v in raw.items() if k in known_order}

    pm_val = cleaned.get("payment_method")
    if pm_val in ("upi", "card", "netbanking", "credit_card", "debit_card"):
        cleaned["payment_method"] = "prepaid"
    elif pm_val == "cash_on_delivery":
        cleaned["payment_method"] = "cod"

    ps_val = cleaned.get("payment_status")
    if ps_val in ("captured", "success"):
        cleaned["payment_status"] = "paid"

    for ts_field in ("placed_at", "shipped_at", "delivered_at", "cancelled_at"):
        if ts_field in cleaned:
            cleaned[ts_field] = ensure_aware_utc(cleaned[ts_field])

    if "items" in cleaned and isinstance(cleaned["items"], list):
        cleaned["items"] = [
            _normalize_order_item(it) for it in cleaned["items"] if isinstance(it, dict)
        ]

    if "ship_to" in cleaned or "ship_to_json" in raw:
        cleaned["ship_to"] = _normalize_ship_to(raw)

    if "returns" in cleaned and isinstance(cleaned["returns"], list):
        cleaned["returns"] = [
            clean_return_dict(r) for r in cleaned["returns"] if isinstance(r, dict)
        ]

    if "refunds" in cleaned and isinstance(cleaned["refunds"], list):
        cleaned["refunds"] = [
            clean_refund_dict(rf) for rf in cleaned["refunds"] if isinstance(rf, dict)
        ]

    return cleaned


def clean_shipment_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip unknown fields and normalize timestamps/statuses for Shipment and ShipmentEvents."""
    known_shipment = set(Shipment.model_fields.keys())
    cleaned = {k: v for k, v in raw.items() if k in known_shipment}
    if cleaned.get("status") == "shipped":
        cleaned["status"] = "in_transit"
    elif cleaned.get("status") == "undelivered":
        cleaned["status"] = "exception"
    if "eta_at" in cleaned:
        cleaned["eta_at"] = ensure_aware_utc(cleaned["eta_at"])

    if "events" in cleaned and isinstance(cleaned["events"], list):
        known_event = set(ShipmentEvent.model_fields.keys())
        cleaned_events: list[dict[str, Any]] = []
        for ev in cleaned["events"]:
            if not isinstance(ev, dict):
                continue
            cleaned_ev = {k: v for k, v in ev.items() if k in known_event}
            if "occurred_at" not in cleaned_ev and "ts" in ev:
                cleaned_ev["occurred_at"] = ev["ts"]
            if "occurred_at" in cleaned_ev:
                cleaned_ev["occurred_at"] = ensure_aware_utc(cleaned_ev["occurred_at"])
            if cleaned_ev.get("status") == "shipped":
                cleaned_ev["status"] = "in_transit"
            elif cleaned_ev.get("status") == "undelivered":
                cleaned_ev["status"] = "exception"
            cleaned_events.append(cleaned_ev)
        cleaned["events"] = cleaned_events
    return cleaned


def clean_refundable_item_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize store-api refundable item field names and strip extras."""
    normalized = dict(raw)
    if "original_qty" not in normalized:
        normalized["original_qty"] = normalized.get(
            "requested_qty", normalized.get("remaining_qty", 0)
        )
    if "refundable_paise" not in normalized and "max_refundable_paise" in normalized:
        normalized["refundable_paise"] = normalized["max_refundable_paise"]
    known = set(RefundableItem.model_fields.keys())
    return {k: v for k, v in normalized.items() if k in known}


def clean_store_settings_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip extra audit metadata from store settings response."""
    known = set(StoreSettings.model_fields.keys())
    return {k: v for k, v in raw.items() if k in known}


def clean_customer_refund_summary_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize total_refunds_paise -> total_refunded_paise and drop extra fields."""
    normalized = dict(raw)
    if "total_refunded_paise" not in normalized and "total_refunds_paise" in normalized:
        normalized["total_refunded_paise"] = normalized["total_refunds_paise"]
    if "return_count" not in normalized:
        normalized["return_count"] = 0
    known = set(CustomerRefundSummary.model_fields.keys())
    return {k: v for k, v in normalized.items() if k in known}

