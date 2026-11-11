"""Customer memory snapshot generator for prompts and greeting chips.

Guarantees:
- Safe JSON output: enums, IDs, counts, and dates only (no customer reason text or free text).
- Dynamic greeting chips for open returns, refunds, and cases.
- Live status action: chips never contain static status text; actions trigger live status lookup.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, Engine, select

from sellershield_agent.db import get_engine, read_tx
from sellershield_agent.domain.state import customer_memory
from sellershield_agent.memory.models import CustomerProfile


def _build_chips_from_items(open_item_ids: list[str]) -> list[dict[str, str]]:
    chips: list[dict[str, str]] = []
    for item_id in open_item_ids:
        chip_id = f"chip-{item_id.lower()}"
        if item_id.startswith("RET-"):
            label = f"Check return {item_id}"
        elif item_id.startswith("RFD-"):
            label = f"Check refund {item_id}"
        elif item_id.startswith("CASE-"):
            label = f"Check case {item_id}"
        else:
            label = f"Check item {item_id}"
        chips.append(
            {
                "id": chip_id,
                "label": label,
                "action": "check_status",
                "ref_id": item_id,
            }
        )
    return chips


def _snapshot_from_conn(conn: Connection, customer_id: str) -> dict[str, Any]:
    row = conn.execute(
        select(customer_memory.c.profile_json).where(
            customer_memory.c.customer_id == customer_id
        )
    ).scalar_one_or_none()

    if row is not None:
        try:
            profile = CustomerProfile.model_validate_json(row)
        except Exception:
            profile = CustomerProfile()
    else:
        profile = CustomerProfile()

    greeting_chips = _build_chips_from_items(profile.open_item_ids)

    return {
        "customer_id": customer_id,
        "preferred_language": profile.preferred_language,
        "last_seen_at": profile.last_seen_at,
        "last_order_discussed": (
            profile.last_order_discussed.model_dump()
            if profile.last_order_discussed is not None
            else None
        ),
        "open_item_ids": profile.open_item_ids,
        "counts": profile.counts.model_dump(),
        "greeting_chips": greeting_chips,
    }


def get_memory_snapshot(
    db_or_conn: Engine | Connection,
    customer_id: str,
) -> dict[str, Any]:
    """Return structured memory snapshot for Understanding prompt and greeting chips."""
    if isinstance(db_or_conn, Connection):
        return _snapshot_from_conn(db_or_conn, customer_id)
    with read_tx(db_or_conn) as conn:
        return _snapshot_from_conn(conn, customer_id)


def snapshot(
    customer_id: str,
    db_or_conn: Engine | Connection | None = None,
) -> dict[str, Any]:
    """Convenience entrypoint matching spec signature snapshot(customer_id)."""
    target = db_or_conn if db_or_conn is not None else get_engine()
    return get_memory_snapshot(target, customer_id)


__all__ = ["get_memory_snapshot", "snapshot"]
