"""Deterministic writer reading domain events and updating structured memory.

Guarantees:
- Zero free text: records only enums, IDs, counts, and dates.
- String length ceiling <= 64 characters.
- In-transaction atomicity: writes memory_events and updates customer_memory in the caller's tx.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import Connection, and_, select

from sellershield_agent.domain.state import (
    customer_memory,
    generate_memory_event_id,
    memory_events,
)
from sellershield_agent.memory.models import (
    _ALLOWED_LANGUAGES,
    _ISO_TS_RE,
    _ITEM_ID_RE,
    _ORDER_ID_RE,
    CustomerProfile,
    OrderDiscussed,
)

_CUSTOMER_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_REF_ID_RE = re.compile(r"^(ORD|RET|RFD|CASE|PRP|APR)-[0-9A-Za-z_-]{1,58}$")
_ALLOWED_EVENT_TYPES = frozenset(
    {
        "proposal.succeeded",
        "proposal.failed",
        "approval.decided",
        "refund.completed",
        "case.opened",
        "case.resolved",
    }
)
_ALLOWED_OUTCOMES = frozenset(
    {
        "succeeded",
        "failed",
        "approved",
        "rejected",
        "completed",
        "open",
        "resolved",
    }
)


def record_memory_event_in_tx(
    tx: Connection,
    *,
    customer_id: str,
    event_type: str,
    ref_id: str,
    outcome: str,
    occurred_at: str,
    order_id: str | None = None,
    language: str | None = None,
) -> str:
    """Record a domain event and deterministically update customer memory."""
    if not customer_id or not _CUSTOMER_ID_RE.match(customer_id):
        raise ValueError("customer_id must be an identifier <= 64 characters")
    if event_type not in _ALLOWED_EVENT_TYPES:
        raise ValueError(f"Unsupported memory event_type: '{event_type}'")
    if not ref_id or not _REF_ID_RE.match(ref_id):
        raise ValueError("ref_id must match allowed ID prefix and <= 64 characters")
    if outcome not in _ALLOWED_OUTCOMES:
        raise ValueError(f"Unsupported memory outcome: '{outcome}'")
    if not occurred_at or len(occurred_at) > 64 or not _ISO_TS_RE.match(occurred_at):
        raise ValueError("occurred_at must be an ISO-8601 timestamp <= 64 characters")
    datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))

    # Check existing prior count events before inserting to keep counts idempotent under retries
    already_counted = False
    if ref_id.startswith("RET-") and outcome in ("succeeded", "approved", "open"):
        already_counted = (
            tx.execute(
                select(memory_events.c.event_id)
                .where(
                    and_(
                        memory_events.c.customer_id == customer_id,
                        memory_events.c.ref_id == ref_id,
                        memory_events.c.outcome.in_(("succeeded", "approved", "open")),
                    )
                )
                .limit(1)
            ).fetchone()
            is not None
        )
    elif ref_id.startswith("RFD-") and outcome in ("succeeded", "completed"):
        already_counted = (
            tx.execute(
                select(memory_events.c.event_id)
                .where(
                    and_(
                        memory_events.c.customer_id == customer_id,
                        memory_events.c.ref_id == ref_id,
                        memory_events.c.outcome.in_(("succeeded", "completed")),
                    )
                )
                .limit(1)
            ).fetchone()
            is not None
        )
    elif event_type == "case.opened":
        already_counted = (
            tx.execute(
                select(memory_events.c.event_id)
                .where(
                    and_(
                        memory_events.c.customer_id == customer_id,
                        memory_events.c.ref_id == ref_id,
                        memory_events.c.type == "case.opened",
                    )
                )
                .limit(1)
            ).fetchone()
            is not None
        )

    # 1. Insert row into memory_events
    event_id = generate_memory_event_id()
    tx.execute(
        memory_events.insert().values(
            event_id=event_id,
            customer_id=customer_id,
            type=event_type,
            ref_id=ref_id,
            outcome=outcome,
            occurred_at=occurred_at,
        )
    )

    # 2. Fetch existing customer_memory profile
    existing_json = tx.execute(
        select(customer_memory.c.profile_json).where(
            customer_memory.c.customer_id == customer_id
        )
    ).scalar_one_or_none()

    if existing_json is not None:
        try:
            profile = CustomerProfile.model_validate_json(existing_json)
        except Exception:
            profile = CustomerProfile()
    else:
        profile = CustomerProfile()

    # 3. Update profile fields deterministically
    profile.last_seen_at = occurred_at

    if language in _ALLOWED_LANGUAGES:
        profile.preferred_language = cast(Literal["en", "hi", "hinglish", "other"], language)

    if order_id and len(order_id) <= 64 and _ORDER_ID_RE.match(order_id):
        profile.last_order_discussed = OrderDiscussed(
            order_id=order_id,
            timestamp=occurred_at,
        )

    # 4. Update deterministic counts (idempotent per ref_id)
    if not already_counted:
        if ref_id.startswith("RET-") and outcome in ("succeeded", "approved", "open"):
            profile.counts.returns += 1
        elif ref_id.startswith("RFD-") and outcome in ("succeeded", "completed"):
            profile.counts.refunds += 1
        elif event_type == "case.opened":
            profile.counts.cases += 1

    # 5. Manage open_item_ids lifecycle
    if _ITEM_ID_RE.match(ref_id):
        is_terminal = outcome in ("completed", "resolved", "failed", "rejected")
        if is_terminal:
            if ref_id in profile.open_item_ids:
                profile.open_item_ids.remove(ref_id)
        else:
            if ref_id not in profile.open_item_ids:
                profile.open_item_ids.append(ref_id)

    # Enforce maximum 20 open items
    profile.open_item_ids = profile.open_item_ids[-20:]

    profile_json = json.dumps(
        profile.model_dump(),
        sort_keys=True,
        separators=(",", ":"),
    )

    # 6. Upsert customer_memory
    if existing_json is not None:
        tx.execute(
            customer_memory.update()
            .where(customer_memory.c.customer_id == customer_id)
            .values(profile_json=profile_json, updated_at=occurred_at)
        )
    else:
        tx.execute(
            customer_memory.insert().values(
                customer_id=customer_id,
                profile_json=profile_json,
                updated_at=occurred_at,
            )
        )

    return event_id


__all__ = ["record_memory_event_in_tx"]
