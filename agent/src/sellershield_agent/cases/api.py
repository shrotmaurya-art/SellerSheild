"""Case escalation, brief creation, and lifecycle operations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, and_, select

from sellershield_agent.cases.brief import build_deterministic_brief
from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.domain.session import sessions
from sellershield_agent.domain.state import (
    case_events,
    cases,
    generate_case_event_id,
    generate_case_id,
)
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.events.emit import emit_client_event
from sellershield_agent.memory.writer import record_memory_event_in_tx
from sellershield_agent.outbox.enqueue import enqueue_notification

VALID_PRIORITIES: frozenset[str] = frozenset({"normal", "high"})
VALID_STATUSES: frozenset[str] = frozenset(
    {
        "open",
        "in_review",
        "waiting_for_customer",
        "waiting_for_seller",
        "resolved",
        "closed",
        "pending_customer",
    }
)


def create_minimal_brief_stub(
    *,
    customer_id: str,
    order_id: str | None,
    category: str,
    priority: str,
    source: str,
    sla_due_at: str | None = None,
) -> dict[str, Any]:
    """Generate minimal deterministic brief stub."""
    return build_deterministic_brief(
        customer_id=customer_id,
        order_id=order_id,
        category=category,
        customer_sentiment="calm",
        order_facts={"order_id": order_id} if order_id else {},
    )


def open_case(
    tx: Connection,
    customer_id: str,
    order_id: str | None = None,
    category: str = "general",
    priority: str = "normal",
    source: str = "chat",
    brief: dict[str, Any] | None = None,
    now_iso: str | None = None,
    session_id: str | None = None,
    clock: Clock | None = None,
) -> str:
    """Open a support case or return an existing one within 30 minutes (idempotent).

    Returns the case_id (CASE-...).
    """
    if not customer_id or not isinstance(customer_id, str):
        raise ValueError("customer_id must be a non-empty string")
    if not category or not isinstance(category, str):
        raise ValueError("category must be a non-empty string")
    if priority not in VALID_PRIORITIES:
        raise ValueError(
            f"Invalid priority '{priority}'. Allowed priorities: {sorted(VALID_PRIORITIES)}"
        )
    if not source or not isinstance(source, str):
        raise ValueError("source must be a non-empty string")

    if clock is not None:
        now = clock.now()
    elif now_iso is not None:
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    else:
        now = SystemClock().now()

    created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 30-minute idempotency: at most one active case per (customer_id, category, order_id)
    thirty_mins_ago = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    order_cond = (
        cases.c.order_id == order_id if order_id is not None else cases.c.order_id.is_(None)
    )
    existing_stmt = select(cases.c.case_id).where(
        and_(
            cases.c.customer_id == customer_id,
            cases.c.category == category,
            order_cond,
            cases.c.created_at >= thirty_mins_ago,
            cases.c.status != "closed",
        )
    )
    existing_case = tx.execute(existing_stmt).fetchone()
    if existing_case is not None:
        return str(existing_case[0])

    # SLA window: 2 hours for high, 24 hours for normal
    sla_hours = 2 if priority == "high" else 24
    sla_due_at = (now + timedelta(hours=sla_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    case_id = generate_case_id()

    if brief is None:
        brief_data = build_deterministic_brief(
            customer_id=customer_id,
            order_id=order_id,
            category=category,
            order_facts={"order_id": order_id} if order_id else {},
        )
    else:
        brief_data = brief

    brief_json = json.dumps(brief_data, sort_keys=True, separators=(",", ":"))

    tx.execute(
        cases.insert().values(
            case_id=case_id,
            customer_id=customer_id,
            order_id=order_id,
            session_id=session_id,
            category=category,
            priority=priority,
            status="open",
            handler="agent",
            assignee=None,
            brief_json=brief_json,
            sla_due_at=sla_due_at,
            resolution_code=None,
            evidence_ids_json=None,
            created_at=created_at,
            resolved_at=None,
            version=1,
        )
    )

    tx.execute(
        case_events.insert().values(
            event_id=generate_case_event_id(),
            case_id=case_id,
            actor_type="system",
            actor_id=source,
            kind="case_opened",
            text=f"Case opened via {source}",
            created_at=created_at,
        )
    )

    record_memory_event_in_tx(
        tx,
        customer_id=customer_id,
        event_type="case.opened",
        ref_id=case_id,
        outcome="open",
        occurred_at=created_at,
        order_id=order_id,
    )

    return case_id


def seller_reply_flip_handler(
    tx: Connection,
    case_id: str,
    seller_id: str,
    message_text: str,
    clock: Clock | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Seller replies to support case: flips handler to human atomically and records message."""
    if clock is not None:
        now_str = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    elif now_iso is not None:
        now_str = now_iso
    else:
        now_str = SystemClock().now().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Fetch case
    row = tx.execute(select(cases).where(cases.c.case_id == case_id)).fetchone()
    if row is None:
        raise AppHTTPException(status_code=404, code="not_found", message="Case not found")

    customer_id = str(row.customer_id)
    session_id = row.session_id

    # Atomic compare-and-set: flip handler to human and increment version
    tx.execute(
        cases.update()
        .where(cases.c.case_id == case_id)
        .values(
            handler="human",
            status="in_review" if row.status in ("open", "waiting_for_seller") else row.status,
            version=cases.c.version + 1,
        )
    )

    # Sync session handler if session origin exists
    if session_id:
        tx.execute(
            sessions.update()
            .where(sessions.c.id == session_id)
            .values(handler="human")
        )

    event_id = generate_case_event_id()
    tx.execute(
        case_events.insert().values(
            event_id=event_id,
            case_id=case_id,
            actor_type="seller",
            actor_id=seller_id,
            kind="case_message",
            text=message_text,
            created_at=now_str,
        )
    )

    # Emit client event
    emit_client_event(
        tx,
        customer_id=customer_id,
        type="case.message",
        payload={
            "case_id": case_id,
            "text": message_text,
            "actor_type": "seller",
            "message_id": event_id,
            "created_at": now_str,
        },
        now_iso=now_str,
    )

    # Enqueue outbox notification
    enqueue_notification(
        tx,
        customer_id=customer_id,
        template="case_reply",
        payload={"case_id": case_id, "text": message_text},
        dedupe_key=f"case_reply:{case_id}:{event_id}",
        now_iso=now_str,
    )

    return {
        "message_id": event_id,
        "sender_role": "seller",
        "sender_id": seller_id,
        "text": message_text,
        "created_at": now_str,
    }


def customer_send_case_message(
    tx: Connection,
    case_id: str,
    customer_id: str,
    text: str,
    clock: Clock | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Customer sends a message in case thread; cannot flip handler."""
    if clock is not None:
        now_str = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    elif now_iso is not None:
        now_str = now_iso
    else:
        now_str = SystemClock().now().strftime("%Y-%m-%dT%H:%M:%SZ")

    row = tx.execute(
        select(cases).where(
            and_(cases.c.case_id == case_id, cases.c.customer_id == customer_id)
        )
    ).fetchone()
    if row is None:
        raise AppHTTPException(status_code=404, code="not_found", message="Case not found")

    # If waiting for customer, move to in_review
    new_status = "in_review" if row.status == "waiting_for_customer" else row.status
    if new_status != row.status:
        tx.execute(
            cases.update()
            .where(cases.c.case_id == case_id)
            .values(status=new_status, version=cases.c.version + 1)
        )

    event_id = generate_case_event_id()
    tx.execute(
        case_events.insert().values(
            event_id=event_id,
            case_id=case_id,
            actor_type="customer",
            actor_id=customer_id,
            kind="case_message",
            text=text,
            created_at=now_str,
        )
    )

    return {
        "message_id": event_id,
        "sender_role": "customer",
        "sender_id": customer_id,
        "text": text,
        "created_at": now_str,
    }


def handback_case(
    tx: Connection,
    case_id: str,
    seller_id: str,
    instructions: str | None = None,
    clock: Clock | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Hand case back to autonomous agent."""
    if clock is not None:
        now_str = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    elif now_iso is not None:
        now_str = now_iso
    else:
        now_str = SystemClock().now().strftime("%Y-%m-%dT%H:%M:%SZ")

    row = tx.execute(select(cases).where(cases.c.case_id == case_id)).fetchone()
    if row is None:
        raise AppHTTPException(status_code=404, code="not_found", message="Case not found")

    tx.execute(
        cases.update()
        .where(cases.c.case_id == case_id)
        .values(handler="agent", version=cases.c.version + 1)
    )

    if row.session_id:
        tx.execute(
            sessions.update()
            .where(sessions.c.id == row.session_id)
            .values(handler="agent")
        )

    event_id = generate_case_event_id()
    tx.execute(
        case_events.insert().values(
            event_id=event_id,
            case_id=case_id,
            actor_type="seller",
            actor_id=seller_id,
            kind="case_handback",
            text=instructions or "Handed back to agent",
            created_at=now_str,
        )
    )

    return {
        "case_id": case_id,
        "handler": "agent",
        "updated_at": now_str,
    }


def update_case_status(
    tx: Connection,
    case_id: str,
    seller_id: str,
    status: str,
    resolution_note: str | None = None,
    clock: Clock | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Seller updates case status."""
    if status not in VALID_STATUSES:
        raise AppHTTPException(
            status_code=400,
            code="bad_request",
            message=f"Invalid status '{status}'. Allowed: {sorted(VALID_STATUSES)}",
        )

    if clock is not None:
        now_str = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    elif now_iso is not None:
        now_str = now_iso
    else:
        now_str = SystemClock().now().strftime("%Y-%m-%dT%H:%M:%SZ")

    row = tx.execute(select(cases).where(cases.c.case_id == case_id)).fetchone()
    if row is None:
        raise AppHTTPException(status_code=404, code="not_found", message="Case not found")

    resolved_at = now_str if status in ("resolved", "closed") else row.resolved_at

    tx.execute(
        cases.update()
        .where(cases.c.case_id == case_id)
        .values(
            status=status,
            resolved_at=resolved_at,
            version=cases.c.version + 1,
        )
    )

    event_id = generate_case_event_id()
    tx.execute(
        case_events.insert().values(
            event_id=event_id,
            case_id=case_id,
            actor_type="seller",
            actor_id=seller_id,
            kind="case_status",
            text=resolution_note or f"Status updated to {status}",
            created_at=now_str,
        )
    )

    customer_id = str(row.customer_id)
    if status in ("resolved", "closed"):
        record_memory_event_in_tx(
            tx,
            customer_id=customer_id,
            event_type="case.resolved",
            ref_id=case_id,
            outcome="resolved",
            occurred_at=now_str,
            order_id=str(row.order_id) if row.order_id else None,
        )
    emit_client_event(
        tx,
        customer_id=customer_id,
        type="case.status",
        payload={
            "case_id": case_id,
            "status": status,
            "previous_status": row.status,
            "message": resolution_note,
        },
        now_iso=now_str,
    )

    enqueue_notification(
        tx,
        customer_id=customer_id,
        template="case_status_updated",
        payload={"case_id": case_id, "status": status},
        dedupe_key=f"case_status:{case_id}:{status}:{row.version}",
        now_iso=now_str,
    )

    return {
        "case_id": case_id,
        "status": status,
        "resolved_at": resolved_at,
        "updated_at": now_str,
    }


def get_customer_case_view(
    conn: Connection,
    case_id: str,
    customer_id: str,
) -> dict[str, Any]:
    """Retrieve customer case view. Strips seller-only notes and risk signals (IDOR 404)."""
    row = conn.execute(
        select(cases).where(
            and_(cases.c.case_id == case_id, cases.c.customer_id == customer_id)
        )
    ).fetchone()
    if row is None:
        raise AppHTTPException(status_code=404, code="not_found", message="Case not found")

    # Fetch messages only (exclude internal seller_note kinds)
    evt_rows = conn.execute(
        select(case_events)
        .where(
            and_(
                case_events.c.case_id == case_id,
                case_events.c.kind == "case_message",
            )
        )
        .order_by(case_events.c.created_at.asc())
    ).fetchall()

    msgs = [
        {
            "message_id": str(e.event_id),
            "sender_role": str(e.actor_type),
            "sender_id": str(e.actor_id or ""),
            "text": str(e.text or ""),
            "created_at": str(e.created_at),
        }
        for e in evt_rows
    ]

    return {
        "case_id": str(row.case_id),
        "customer_id": str(row.customer_id),
        "order_id": str(row.order_id) if row.order_id else None,
        "status": str(row.status),
        "reason": str(row.category),
        "created_at": str(row.created_at),
        "updated_at": str(row.resolved_at or row.created_at),
        "messages": msgs,
    }


def get_seller_case_view(
    conn: Connection,
    case_id: str,
) -> dict[str, Any]:
    """Retrieve full seller case view including brief and all case events."""
    row = conn.execute(select(cases).where(cases.c.case_id == case_id)).fetchone()
    if row is None:
        raise AppHTTPException(status_code=404, code="not_found", message="Case not found")

    evt_rows = conn.execute(
        select(case_events)
        .where(case_events.c.case_id == case_id)
        .order_by(case_events.c.created_at.asc())
    ).fetchall()

    msgs = [
        {
            "message_id": str(e.event_id),
            "sender_role": str(e.actor_type),
            "sender_id": str(e.actor_id or ""),
            "text": str(e.text or ""),
            "created_at": str(e.created_at),
        }
        for e in evt_rows
    ]

    brief_data = None
    if row.brief_json:
        try:
            brief_data = json.loads(str(row.brief_json))
        except Exception:
            brief_data = {}

    evidence_ids = None
    if row.evidence_ids_json:
        try:
            evidence_ids = json.loads(str(row.evidence_ids_json))
        except Exception:
            evidence_ids = []

    return {
        "case_id": str(row.case_id),
        "customer_id": str(row.customer_id),
        "order_id": str(row.order_id) if row.order_id else None,
        "status": str(row.status),
        "reason": str(row.category),
        "priority": str(row.priority),
        "assigned_to": str(row.assignee) if row.assignee else None,
        "evidence_ids": evidence_ids or [],
        "brief": brief_data,
        "messages": msgs,
        "created_at": str(row.created_at),
        "updated_at": str(row.resolved_at or row.created_at),
    }
