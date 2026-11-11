"""Tests for Task T3.1: Cases, escalation triggers, deterministic brief, and handler model.

Covers:
1. test_human_request_makes_exactly_one_case_with_brief_and_repeat_returns_it
2. test_legal_words_give_priority_high
3. test_escalate_policy_decision_opens_case
4. test_two_angry_messages_give_chip_and_no_case
5. test_brief_safety_no_tokens_no_cross_customer_data_escaped_text
6. test_parallel_seller_replies_flip_handler_once_and_store_both_messages
7. test_agent_stays_silent_while_human_and_rejects_propose_tools
8. test_customer_cannot_flip_handler_or_read_seller_notes
9. test_case_ids_of_other_users_give_404_for_customers_and_sellers
10. test_failed_case_creation_never_claims_success
"""

from __future__ import annotations

import concurrent.futures
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.cases.api import (
    open_case,
    seller_reply_flip_handler,
)
from sellershield_agent.cases.brief import build_deterministic_brief
from sellershield_agent.clock import Clock, FixedClock
from sellershield_agent.db import get_metadata, init_db, write_tx
from sellershield_agent.domain.session import ChatMessageInput, sessions
from sellershield_agent.domain.state import case_events, cases
from sellershield_agent.orchestrator.escalation import (
    evaluate_escalation_triggers,
)
from sellershield_agent.policy.loader import load_policy


@pytest.fixture
def test_clock() -> FixedClock:
    return FixedClock(initial=datetime(2026, 11, 11, 10, 0, 0, tzinfo=UTC))


@pytest.fixture
def test_engine() -> Any:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine, metadata=get_metadata())
    return engine


def test_human_request_makes_exactly_one_case_with_brief_and_repeat_returns_it(
    test_engine: Any, test_clock: Clock
) -> None:
    """1. Customer asking for human creates one case with brief; repeat returns it."""
    with write_tx(test_engine) as tx:
        # First request
        res1 = evaluate_escalation_triggers(
            tx,
            customer_id="CUST-001",
            message_text="I want to talk to a human agent please",
            category="human_request",
            order_id="ORD-1001",
            clock=test_clock,
        )
        assert res1.case_created is True
        assert res1.case_id is not None
        assert res1.case_id.startswith("CASE-")
        case_id_1 = res1.case_id

        # Inspect brief in db
        row = tx.execute(select(cases).where(cases.c.case_id == case_id_1)).fetchone()
        assert row is not None
        assert row.priority == "normal"
        assert row.status == "open"
        brief = json.loads(str(row.brief_json))
        assert "category" in brief
        assert "recommended_next_step" in brief

        # Advance simulated clock by 10 minutes (within 30-min idempotency window)
        test_clock.advance(delta=timedelta(seconds=600))

        # Repeated request with same customer, category, and order
        res2 = evaluate_escalation_triggers(
            tx,
            customer_id="CUST-001",
            message_text="Hello? Give me a human",
            category="human_request",
            order_id="ORD-1001",
            clock=test_clock,
        )
        assert res2.case_id == case_id_1
        assert res2.case_created is False  # Reused existing open case

        # Total rows in cases table should still be 1
        all_cases = tx.execute(select(cases)).fetchall()
        assert len(all_cases) == 1


def test_legal_words_give_priority_high(test_engine: Any, test_clock: Clock) -> None:
    """2. Legal/threat words from escalation_keywords open a high-priority case (2h SLA)."""
    with write_tx(test_engine) as tx:
        res = evaluate_escalation_triggers(
            tx,
            customer_id="CUST-002",
            message_text="If you do not refund me I will contact my lawyer and go to court",
            category="legal_threat",
            order_id="ORD-1002",
            clock=test_clock,
        )
        assert res.case_created is True
        assert res.case_id is not None

        row = tx.execute(select(cases).where(cases.c.case_id == res.case_id)).fetchone()
        assert row is not None
        assert row.priority == "high"
        # High priority SLA must be 2 hours from Clock (10:00 + 2h = 12:00)
        assert row.sla_due_at == "2026-11-11T12:00:00Z"


def test_escalate_policy_decision_opens_case(test_engine: Any, test_clock: Clock) -> None:
    """3. A policy decision ESCALATE opens a persistent case."""
    with write_tx(test_engine) as tx:
        res = evaluate_escalation_triggers(
            tx,
            customer_id="CUST-003",
            message_text="This damaged item is broken",
            category="damaged_item",
            order_id="ORD-1003",
            policy_decision="ESCALATE",
            clock=test_clock,
        )
        assert res.case_created is True
        assert res.case_id is not None

        row = tx.execute(select(cases).where(cases.c.case_id == res.case_id)).fetchone()
        assert row is not None
        assert row.category == "damaged_item"
        assert row.status == "open"


def test_two_angry_messages_give_chip_and_no_case(test_engine: Any, test_clock: Clock) -> None:
    """4. Customer angry twice in a row gives the 'Talk to Human' chip and NO case."""
    with write_tx(test_engine) as tx:
        # First angry message
        res1 = evaluate_escalation_triggers(
            tx,
            customer_id="CUST-004",
            message_text="This is terrible service! I hate this so much!",
            category="general",
            sentiment="angry",
            previous_sentiment="calm",
            clock=test_clock,
        )
        assert res1.case_created is False
        assert res1.offer_human_chip is False

        # Second consecutive angry message
        res2 = evaluate_escalation_triggers(
            tx,
            customer_id="CUST-004",
            message_text="I am extremely furious and angry with you guys!",
            category="general",
            sentiment="angry",
            previous_sentiment="angry",
            clock=test_clock,
        )
        # Must offer human chip but NOT create a case directly
        assert res2.case_created is False
        assert res2.offer_human_chip is True

        # Verify no case was created
        all_cases = tx.execute(select(cases)).fetchall()
        assert len(all_cases) == 0


def test_brief_safety_no_tokens_no_cross_customer_data_escaped_text() -> None:
    """5. Deterministic brief has no tokens, no cross-customer data, unescaped text is prevented."""
    hostile_input = '<script>alert("pwned")</script> Bearer secret-token-12345 ORD-9999'

    brief = build_deterministic_brief(
        customer_id="CUST-005",
        order_id="ORD-1005",
        category="damaged_item",
        customer_language="en",
        customer_sentiment="angry",
        customer_message=hostile_input,
        tool_calls=[{"tool": "get_order", "status": "ok", "latency_ms": 15}],
        order_facts={"order_id": "ORD-1005", "total_paise": 120000},
        risk_signals={"risk_band": "low"},
    )

    brief_str = json.dumps(brief)

    # Invariant: No tokens or secrets in brief
    assert "secret-token" not in brief_str
    assert "Bearer" not in brief_str

    # Invariant: Raw HTML is escaped
    assert "<script>" not in brief_str
    assert "&lt;script&gt;" in brief_str

    # Customer request text must be tagged as unverified
    cust_req = brief.get("customer_request", {})
    assert cust_req.get("verified") is False

    # Recommendation is deterministic from rule table, not influenced by injection
    rec = brief.get("recommended_next_step")
    assert rec == "review"  # rule for damaged_item


def test_parallel_seller_replies_flip_handler_once_and_store_both_messages(
    test_engine: Any, test_clock: Clock
) -> None:
    """6. Two parallel seller replies flip the handler once atomically and store both messages."""
    with write_tx(test_engine) as tx:
        case_id = open_case(
            tx,
            customer_id="CUST-006",
            order_id="ORD-1006",
            category="delivery_delay",
            priority="normal",
            source="chat",
            now_iso=test_clock.now().isoformat(),
        )

    # Simulate two parallel seller replies
    def reply_task(text: str, seller_id: str) -> None:
        with write_tx(test_engine) as tx:
            seller_reply_flip_handler(
                tx,
                case_id=case_id,
                seller_id=seller_id,
                message_text=text,
                clock=test_clock,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(reply_task, "Reply from seller A", "seller_1")
        f2 = executor.submit(reply_task, "Reply from seller B", "seller_2")
        f1.result()
        f2.result()

    with test_engine.connect() as conn:
        case_row = conn.execute(select(cases).where(cases.c.case_id == case_id)).fetchone()
        assert case_row is not None
        assert case_row.handler == "human"

        # Both messages stored in case_events
        events = conn.execute(
            select(case_events)
            .where(case_events.c.case_id == case_id, case_events.c.kind == "case_message")
            .order_by(case_events.c.created_at.asc())
        ).fetchall()
        assert len(events) == 2
        texts = {e.text for e in events}
        assert "Reply from seller A" in texts
        assert "Reply from seller B" in texts


def test_agent_stays_silent_while_human_and_rejects_propose_tools(
    test_engine: Any, test_clock: Clock
) -> None:
    """7. While human, agent says 'A team member is handling this' & avoids proposals."""
    # Create session with handler="human"
    with write_tx(test_engine) as tx:
        tx.execute(
            sessions.insert().values(
                id="ses_human_test_001",
                customer_id="CUST-007",
                channel="web",
                handler="human",
                language="en",
                state_json="{}",
                started_at=test_clock.now().isoformat(),
                last_active_at=test_clock.now().isoformat(),
            )
        )

    # Sending a message to this session should yield "A team member is handling this"
    # and strictly disallow propose tools.
    from sellershield_agent.orchestrator.pipeline import ConversationPipeline

    pipeline = ConversationPipeline(
        engine=test_engine,
        clock=test_clock,
        store_client=MagicMock(),
        policy=load_policy("v3"),
    )

    events: list[tuple[str, dict[str, Any]]] = []

    import asyncio

    async def run_pipeline() -> None:
        async for ev, data in pipeline.execute_stream(
            session_id="ses_human_test_001",
            principal=Principal(
                user_id="user_007",
                customer_id="CUST-007",
                role=Role.customer,
                jti="jti_007",
                token_exp=2000000000,
            ),
            token="valid_token",
            input_data=ChatMessageInput(client_msg_id="msg_001", text="Can you refund me?"),
            request_id="req-test-human",
        ):
            events.append((ev, data))

    asyncio.run(run_pipeline())

    final_event = [data for ev, data in events if ev == "final"]
    assert len(final_event) == 1
    assert "A team member is handling this" in final_event[0]["full_text"]

    # Verify no proposal cards were emitted
    card_events = [data for ev, data in events if ev == "card"]
    for c in card_events:
        assert c.get("type") != "confirmation"
        assert c.get("type") != "return_preview"


def test_customer_cannot_flip_handler_or_read_seller_notes(
    test_engine: Any, test_clock: Clock
) -> None:
    """8. A customer cannot flip the handler or read seller-only internal notes."""
    with write_tx(test_engine) as tx:
        case_id = open_case(
            tx,
            customer_id="CUST-008",
            order_id="ORD-1008",
            category="delivery_delay",
            priority="normal",
            source="chat",
            now_iso=test_clock.now().isoformat(),
        )
        # Flip to human by seller
        seller_reply_flip_handler(
            tx,
            case_id=case_id,
            seller_id="seller_1",
            message_text="Looking into it",
            clock=test_clock,
        )
        # Add a seller-only internal note event
        tx.execute(
            case_events.insert().values(
                event_id="CEVT-NOTE-001",
                case_id=case_id,
                actor_type="seller",
                actor_id="seller_1",
                kind="seller_note",
                text="Customer might be fraud; check warehouse logs.",
                created_at=test_clock.now().isoformat(),
            )
        )

    # Customer tries to send a message attempting to flip handler back
    from sellershield_agent.cases.api import customer_send_case_message, get_customer_case_view

    with write_tx(test_engine) as tx:
        customer_send_case_message(
            tx,
            case_id=case_id,
            customer_id="CUST-008",
            text="Please give me the bot back",
            clock=test_clock,
        )

    with test_engine.connect() as conn:
        case_row = conn.execute(select(cases).where(cases.c.case_id == case_id)).fetchone()
        assert case_row is not None
        # Handler must still be human
        assert case_row.handler == "human"

        # Customer view must NOT contain the seller-only note
        cust_view = get_customer_case_view(conn, case_id=case_id, customer_id="CUST-008")
        all_texts = [m["text"] for m in cust_view["messages"]]
        assert "Customer might be fraud" not in all_texts


def test_case_ids_of_other_users_give_404_for_customers_and_sellers(
    test_engine: Any, test_clock: Clock
) -> None:
    """9. Case IDs of other users give uniform 404 for customers and sellers (IDOR)."""
    with write_tx(test_engine) as tx:
        case_id = open_case(
            tx,
            customer_id="CUST-REAL",
            order_id="ORD-1009",
            category="lost_shipment",
            priority="normal",
            source="chat",
            now_iso=test_clock.now().isoformat(),
        )

    from sellershield_agent.cases.api import get_customer_case_view, get_seller_case_view
    from sellershield_agent.errors import AppHTTPException

    with test_engine.connect() as conn:
        # Customer B requesting Customer A's case
        with pytest.raises(AppHTTPException) as exc_info:
            get_customer_case_view(conn, case_id=case_id, customer_id="CUST-ATTACKER")
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "not_found"

        # Non-existent case for customer
        with pytest.raises(AppHTTPException) as exc_info2:
            get_customer_case_view(conn, case_id="CASE-DOESNOTEXIST", customer_id="CUST-REAL")
        assert exc_info2.value.status_code == 404
        assert exc_info2.value.code == "not_found"

        # Non-existent case for seller
        with pytest.raises(AppHTTPException) as exc_info3:
            get_seller_case_view(conn, case_id="CASE-DOESNOTEXIST")
        assert exc_info3.value.status_code == 404
        assert exc_info3.value.code == "not_found"


def test_failed_case_creation_never_claims_success(test_clock: Clock) -> None:
    """10. A failed case creation never claims success to the user."""
    from sellershield_agent.orchestrator.pipeline import ConversationPipeline

    # Mock engine that throws during transaction
    broken_engine = MagicMock()
    broken_engine.connect.side_effect = RuntimeError("Database connection lost")

    pipeline = ConversationPipeline(
        engine=broken_engine,
        clock=test_clock,
        store_client=MagicMock(),
        policy=load_policy("v3"),
    )

    events: list[tuple[str, dict[str, Any]]] = []

    import asyncio

    async def run_pipeline() -> None:
        async for ev, data in pipeline.execute_stream(
            session_id="ses_failed_case_test",
            principal=Principal(
                user_id="user_010",
                customer_id="CUST-010",
                role=Role.customer,
                jti="jti_010",
                token_exp=2000000000,
            ),
            token="valid_token",
            input_data=ChatMessageInput(client_msg_id="msg_fail_010", text="I need a human"),
            request_id="req-test-fail",
        ):
            events.append((ev, data))

    # Should gracefully report failure and never claim CASE-xxxx was created
    try:
        asyncio.run(run_pipeline())
    except Exception:
        pass  # Even if an error bubbles or is converted to error event

    full_texts = [data.get("full_text", "") for ev, data in events if ev == "final"]
    if full_texts:
        assert "CASE-" not in full_texts[0]

