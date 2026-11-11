"""Tests for T3.0 shared stubs and setup.

Verifies:
1. events/emit.py: emit_client_event validates payload with per-type Pydantic models
   (extra='forbid') and inserts into client_events, returning the cursor.
2. outbox/enqueue.py: enqueue_notification inserts a queued notification into notifications_outbox,
   deduplicating by dedupe_key.
3. cases/api.py: open_case creates a case with minimal brief stub and logs an event in case_events.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select

from sellershield_agent.cases.api import open_case
from sellershield_agent.db import get_metadata, init_db, write_tx
from sellershield_agent.domain.state import (
    case_events,
    cases,
    client_events,
    notifications_outbox,
)
from sellershield_agent.events.emit import emit_client_event
from sellershield_agent.outbox.enqueue import enqueue_notification


@pytest.fixture
def test_engine() -> Any:
    """Create an isolated in-memory engine with all tables initialized."""
    engine = create_engine("sqlite:///:memory:")
    init_db(engine, metadata=get_metadata())
    return engine


def test_emit_client_event_works_and_rejects_unsafe_payload(test_engine: Any) -> None:
    """Test 1: emit_client_event works and rejects unsafe payloads with extra fields."""
    with write_tx(test_engine) as tx:
        # Valid payload succeeds and returns cursor
        valid_payload = {
            "approval_id": "APR-001",
            "proposal_id": "PRP-001",
            "order_id": "ORD-1001",
            "status": "approved",
            "message": "Refund approved",
        }
        cursor = emit_client_event(tx, "C001", "approval.decided", valid_payload)
        assert isinstance(cursor, str)
        assert len(cursor) > 0

        # Verify inserted into client_events
        row = tx.execute(
            select(client_events).where(client_events.c.event_id == cursor)
        ).fetchone()
        assert row is not None
        assert row.customer_id == "C001"
        assert row.type == "approval.decided"
        data = json.loads(str(row.payload_json))
        assert data["approval_id"] == "APR-001"

        # Rejects unsafe payload with extra forbidden field
        unsafe_payload = {
            "approval_id": "APR-001",
            "proposal_id": "PRP-001",
            "order_id": "ORD-1001",
            "status": "approved",
            "internal_risk_score": 99,  # Unsafe / unexpected field
        }
        with pytest.raises((ValidationError, ValueError)):
            emit_client_event(tx, "C001", "approval.decided", unsafe_payload)

        # Rejects unknown event type
        with pytest.raises((ValidationError, ValueError)):
            emit_client_event(tx, "C001", "malicious.type", {"foo": "bar"})


def test_enqueue_notification_works_and_deduplicates(test_engine: Any) -> None:
    """Test 2: enqueue_notification inserts row and deduplicates by dedupe_key."""
    with write_tx(test_engine) as tx:
        payload = {"order_id": "ORD-1001", "eta": "2026-11-12T10:00:00Z"}
        dedupe_key = "delay:ORD-1001:2026-11-12"

        # Enqueue first notification
        nid1 = enqueue_notification(
            tx,
            customer_id="C001",
            template="order_delayed",
            payload=payload,
            dedupe_key=dedupe_key,
        )
        assert isinstance(nid1, str)
        assert nid1.startswith("NTF-")

        # Verify row exists in notifications_outbox
        row = tx.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.notification_id == nid1
            )
        ).fetchone()
        assert row is not None
        assert row.status == "queued"
        assert row.attempts == 0
        assert row.template == "order_delayed"

        # Duplicate dedupe_key returns existing id
        nid2 = enqueue_notification(
            tx,
            customer_id="C001",
            template="order_delayed",
            payload=payload,
            dedupe_key=dedupe_key,
        )
        assert nid2 == nid1

        # Rejects non-serializable / unsafe payload
        with pytest.raises((TypeError, ValueError)):
            enqueue_notification(
                tx,
                customer_id="C001",
                template="order_delayed",
                payload={"bad": object()},  # Non-serializable
                dedupe_key="another_key",
            )


def test_open_case_works_and_creates_brief_stub(test_engine: Any) -> None:
    """Test 3: open_case opens case, creates brief stub, case_event, and validates priority."""
    with write_tx(test_engine) as tx:
        case_id = open_case(
            tx,
            customer_id="C001",
            order_id="ORD-1001",
            category="delivery_delay",
            priority="high",
            source="delay_monitor",
        )
        assert isinstance(case_id, str)
        assert case_id.startswith("CASE-")

        # Verify case row
        case_row = tx.execute(
            select(cases).where(cases.c.case_id == case_id)
        ).fetchone()
        assert case_row is not None
        assert case_row.customer_id == "C001"
        assert case_row.order_id == "ORD-1001"
        assert case_row.category == "delivery_delay"
        assert case_row.priority == "high"
        assert case_row.status == "open"
        brief = json.loads(str(case_row.brief_json))
        assert "summary" in brief
        assert "options" in brief

        # Verify case_event
        evt_row = tx.execute(
            select(case_events).where(case_events.c.case_id == case_id)
        ).fetchone()
        assert evt_row is not None
        assert evt_row.actor_type == "system"
        assert evt_row.kind == "case_opened"

        # Rejects invalid priority
        with pytest.raises((ValueError, ValidationError)):
            open_case(
                tx,
                customer_id="C001",
                order_id="ORD-1001",
                category="delivery_delay",
                priority="urgent",  # Invalid priority (only normal / high permitted)
                source="delay_monitor",
            )

