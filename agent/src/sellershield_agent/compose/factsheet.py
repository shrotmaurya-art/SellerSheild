"""Immutable FactSheet extracted from ToolResults.

Spec §4.5, §4.6, §21 (Amendments 2 and 6).
Invariants:
- Immutable Pydantic model with extra='forbid'.
- Money is integer paise only, never float (KP-05).
- Dates converted to IST calendar dates via timeutil (KP-03, KP-06).
- Entity IDs restricted to ORD, RET, RFD, CASE, APR, PRP.
- Customer-safe rule texts; zero risk scores or internal limits.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sellershield_agent.clock import Clock, FixedClock
from sellershield_agent.timeutil import to_ist
from sellershield_agent.tools.base import ToolResult

_ID_PATTERN = re.compile(r"\b(ORD|RET|RFD|CASE|APR|PRP)-[A-Za-z0-9]+\b", re.IGNORECASE)


class FactSheet(BaseModel):
    """Immutable ground truth fact sheet constructed from deterministic tool results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_ids: frozenset[str]
    amounts_paise: frozenset[int]
    dates: frozenset[date]
    relative_facts: dict[str, int]
    supported_claims: frozenset[str]
    promise_facts: frozenset[str]
    rule_texts: tuple[str, ...]
    language: str = "en"
    order_id: str | None = None
    carrier: str | None = None
    tracking_no: str | None = None
    status: str | None = None
    total_paise: int | None = None
    placed_at_ist: date | None = None
    delivered_at_ist: date | None = None
    eta_at_ist: date | None = None
    items: tuple[dict[str, Any], ...] = ()
    returns: tuple[dict[str, Any], ...] = ()
    refunds: tuple[dict[str, Any], ...] = ()
    orders: tuple[dict[str, Any], ...] = ()
    policy_summary_data: dict[str, Any] = Field(default_factory=dict)
    proposal_id: str | None = None
    proposal_total_paise: int | None = None
    proposal_decision: str | None = None


def _extract_ids_recursive(obj: Any, ids: set[str]) -> None:
    if isinstance(obj, str):
        for m in _ID_PATTERN.finditer(obj):
            ids.add(m.group(0).upper())
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.endswith("_id") and isinstance(v, str):
                for m in _ID_PATTERN.finditer(v):
                    ids.add(m.group(0).upper())
            else:
                _extract_ids_recursive(v, ids)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            _extract_ids_recursive(item, ids)


def _extract_amounts_recursive(obj: Any, amounts: set[int]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if (
                isinstance(k, str)
                and k.endswith("_paise")
                and isinstance(v, int)
                and not isinstance(v, bool)
            ):
                amounts.add(v)
            else:
                _extract_amounts_recursive(v, amounts)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            _extract_amounts_recursive(item, amounts)


def _parse_ist_date(val: Any) -> date | None:
    if not isinstance(val, str) or not val:
        return None
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return to_ist(dt).date()
    except Exception:
        return None


def _extract_dates_recursive(obj: Any, dates: set[date]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and (k.endswith("_at") or k == "date") and isinstance(v, str):
                d = _parse_ist_date(v)
                if d is not None:
                    dates.add(d)
            else:
                _extract_dates_recursive(v, dates)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            _extract_dates_recursive(item, dates)


def build_factsheet(
    results: list[ToolResult] | ToolResult,
    language: str = "en",
    clock: Clock | None = None,
) -> FactSheet:
    """Build an immutable FactSheet from ToolResult objects."""
    res_list = results if isinstance(results, list) else [results]

    now_clock = clock or FixedClock(datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC))
    now_ist_date = to_ist(now_clock.now()).date()

    allowed_ids: set[str] = set()
    amounts_paise: set[int] = {0}
    dates: set[date] = set()
    relative_facts: dict[str, int] = {}
    supported_claims: set[str] = set()
    promise_facts: set[str] = set()
    rule_texts: list[str] = []

    order_id: str | None = None
    carrier: str | None = None
    tracking_no: str | None = None
    status: str | None = None
    total_paise: int | None = None
    placed_at_ist: date | None = None
    delivered_at_ist: date | None = None
    eta_at_ist: date | None = None
    items_list: list[dict[str, Any]] = []
    returns_list: list[dict[str, Any]] = []
    refunds_list: list[dict[str, Any]] = []
    orders_list: list[dict[str, Any]] = []
    policy_summary_data: dict[str, Any] = {}
    proposal_id: str | None = None
    proposal_total_paise: int | None = None
    proposal_decision: str | None = None

    for r in res_list:
        if not r.ok:
            for fid in r.fact_ids:
                _extract_ids_recursive(fid, allowed_ids)
            continue

        for fid in r.fact_ids:
            _extract_ids_recursive(fid, allowed_ids)

        data = r.data
        if not isinstance(data, dict):
            continue

        _extract_ids_recursive(data, allowed_ids)
        _extract_amounts_recursive(data, amounts_paise)
        _extract_dates_recursive(data, dates)

        if "order_id" in data and isinstance(data["order_id"], str):
            order_id = data["order_id"]
        if "carrier" in data and isinstance(data["carrier"], str):
            carrier = data["carrier"]
        if "tracking_no" in data and isinstance(data["tracking_no"], str):
            tracking_no = data["tracking_no"]
        if "status" in data and isinstance(data["status"], str):
            status = data["status"]
        if "total_paise" in data and isinstance(data["total_paise"], int):
            total_paise = data["total_paise"]

        if "placed_at" in data:
            placed_at_ist = _parse_ist_date(data["placed_at"])
            if placed_at_ist:
                dates.add(placed_at_ist)
        if "delivered_at" in data:
            delivered_at_ist = _parse_ist_date(data["delivered_at"])
            if delivered_at_ist:
                dates.add(delivered_at_ist)
        if "eta_at" in data:
            eta_at_ist = _parse_ist_date(data["eta_at"])
            if eta_at_ist:
                dates.add(eta_at_ist)

        if "items" in data and isinstance(data["items"], list):
            for it in data["items"]:
                if isinstance(it, dict):
                    items_list.append(it)
                    _extract_ids_recursive(it, allowed_ids)
                    _extract_amounts_recursive(it, amounts_paise)

        if "returns" in data and isinstance(data["returns"], list):
            for ret in data["returns"]:
                if isinstance(ret, dict):
                    returns_list.append(ret)
                    _extract_ids_recursive(ret, allowed_ids)
                    ret_status = ret.get("status")
                    if ret_status == "approved":
                        supported_claims.add("approved")
                        supported_claims.add("created")
                    elif ret_status in ("requested", "received"):
                        supported_claims.add("created")
                    elif ret_status == "cancelled":
                        supported_claims.add("cancelled")

        if "refunds" in data and isinstance(data["refunds"], list):
            for rfd in data["refunds"]:
                if isinstance(rfd, dict):
                    refunds_list.append(rfd)
                    _extract_ids_recursive(rfd, allowed_ids)
                    _extract_amounts_recursive(rfd, amounts_paise)
                    rfd_status = rfd.get("status")
                    if rfd_status == "completed":
                        supported_claims.add("refunded")
                        supported_claims.add("processed")
                    elif rfd_status == "processing":
                        supported_claims.add("processed")
                    elif rfd_status == "reserved":
                        supported_claims.add("created")

        if "orders" in data and isinstance(data["orders"], list):
            for o in data["orders"]:
                if isinstance(o, dict):
                    orders_list.append(o)
                    _extract_ids_recursive(o, allowed_ids)
                    _extract_amounts_recursive(o, amounts_paise)
                    _extract_dates_recursive(o, dates)
                    o_status = o.get("status")
                    if isinstance(o_status, str):
                        if o_status in ("shipped", "in_transit", "out_for_delivery"):
                            supported_claims.add("shipped")
                            supported_claims.add("dispatched")
                        elif o_status == "delivered":
                            supported_claims.add("delivered")
                            supported_claims.add("shipped")
                            supported_claims.add("dispatched")
                        elif o_status == "cancelled":
                            supported_claims.add("cancelled")

        if "rule_results" in data and isinstance(data["rule_results"], list):
            for rr in data["rule_results"]:
                if isinstance(rr, dict) and rr.get("customer_text"):
                    rule_texts.append(str(rr["customer_text"]))

        if "windows" in data or "summary" in data:
            policy_summary_data = data

        if data.get("type") == "confirmation":
            pid_val = data.get("proposal_id")
            if isinstance(pid_val, str) and pid_val:
                proposal_id = pid_val
            dec_val = data.get("decision")
            if isinstance(dec_val, str) and dec_val:
                proposal_decision = dec_val
            amts_val = data.get("amounts")
            if isinstance(amts_val, dict):
                raw_total = amts_val.get("total_paise")
                if isinstance(raw_total, int) and not isinstance(raw_total, bool):
                    proposal_total_paise = raw_total
                    amounts_paise.add(raw_total)

        if "proposal" in data and data.get("proposal") is None:
            rt_val = data.get("rule_text")
            if isinstance(rt_val, str) and rt_val:
                rule_texts.append(rt_val)

    if status:
        if status in ("shipped", "in_transit", "out_for_delivery"):
            supported_claims.add("shipped")
            supported_claims.add("dispatched")
        elif status == "delivered":
            supported_claims.add("delivered")
            supported_claims.add("shipped")
            supported_claims.add("dispatched")
        elif status == "cancelled":
            supported_claims.add("cancelled")

    if eta_at_ist:
        diff_days = (eta_at_ist - now_ist_date).days
        if diff_days >= 0:
            relative_facts["eta_in_days"] = diff_days

    if delivered_at_ist:
        diff_ago = (now_ist_date - delivered_at_ist).days
        if diff_ago >= 0:
            relative_facts["delivered_days_ago"] = diff_ago

    return FactSheet(
        allowed_ids=frozenset(allowed_ids),
        amounts_paise=frozenset(amounts_paise),
        dates=frozenset(dates),
        relative_facts=relative_facts,
        supported_claims=frozenset(supported_claims),
        promise_facts=frozenset(promise_facts),
        rule_texts=tuple(rule_texts),
        language=language,
        order_id=order_id,
        carrier=carrier,
        tracking_no=tracking_no,
        status=status,
        total_paise=total_paise,
        placed_at_ist=placed_at_ist,
        delivered_at_ist=delivered_at_ist,
        eta_at_ist=eta_at_ist,
        items=tuple(items_list),
        returns=tuple(returns_list),
        refunds=tuple(refunds_list),
        orders=tuple(orders_list),
        policy_summary_data=policy_summary_data,
        proposal_id=proposal_id,
        proposal_total_paise=proposal_total_paise,
        proposal_decision=proposal_decision,
    )
