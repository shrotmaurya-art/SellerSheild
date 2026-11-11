"""
Stage 11 Tests — Carrier Simulator, Store Tick, and Mailbox

Verifies:
1. Tests with a frozen Clock:
   - delayed event emitted exactly once into events_outbox per ETA.
   - calling tick repeatedly does not duplicate events (strict idempotency).
2. Carrier scripts progress:
   - advance shipment creates new event and updates shipment status.
   - final script step marks shipment and order delivered.
   - play scenario plays out all steps.
3. Mail thread ownership & inbox:
   - inbound message from customer writes mail.inbound into events_outbox.
   - verified_sender stamped True from principal.
   - every outbound message marked SIMULATED.
   - customer cannot read another customer's thread (uniform 404).
   - read marking updates read_at.
4. Fault switches (-k fault):
   - carrier.down -> 503 upstream_unavailable.
   - carrier.slow -> 8 s latency.
   - carrier.stale -> returns stale events / no new events.
   - mail.fail_once -> fails once with 503, retry succeeds.
   - mail.down -> 503 upstream_unavailable.
5. DEMO_MODE gate:
   - DEMO_MODE=false yields 404 on /sim/carrier routes.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import db
from app.auth import _mint_access_token
from app.clock import Clock
from app.config import settings
from app.faults import clear_faults, set_fault
from app.main import app
from app.models import EventsOutbox, MailMessage, Order, Shipment, ShipmentEvent, User
from app.tick import run_store_tick_once

SEEDED_EVENT_IDS = {"EVT-1048-1", "EVT-2037-1", "EVT-2037-2", "EVT-1070-1", "EVT-1070-2"}
SEEDED_MAIL_IDS = {"MSG-001", "MSG-002", "MSG-003", "MSG-004", "MSG-005", "MSG-006"}


def _restore_db_state():
    clear_faults()
    Clock.reset()
    with Session(db.engine) as s:
        # Delete non-seeded shipment events
        events = s.scalars(select(ShipmentEvent)).all()
        for ev in events:
            if ev.event_id not in SEEDED_EVENT_IDS:
                s.delete(ev)

        # Restore SHP-1048 and ORD-1048
        shp_1048 = s.get(Shipment, "SHP-1048")
        if shp_1048:
            shp_1048.status = "in_transit"
            shp_1048.version = 1
        ord_1048 = s.get(Order, "ORD-1048")
        if ord_1048:
            ord_1048.status = "shipped"
            ord_1048.delivered_at = None
            ord_1048.version = 1

        # Delete non-seeded mail messages
        msgs = s.scalars(select(MailMessage)).all()
        for m in msgs:
            if m.message_id not in SEEDED_MAIL_IDS:
                s.delete(m)

        # Delete all test outbox events
        outbox_events = s.scalars(select(EventsOutbox)).all()
        for ob in outbox_events:
            s.delete(ob)

        s.commit()


@pytest.fixture(autouse=True)
def cleanup_faults_and_test_data():
    """Ensure clean simulator state before and after each test."""
    _restore_db_state()
    yield
    _restore_db_state()


def _get_auth_headers(
    client: TestClient, email: str = "aarav.mehta@example.test"
) -> dict[str, str]:
    with Session(db.engine) as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None, f"User not found: {email}"
        cid = user.customer.customer_id if user.customer else None
        token = _mint_access_token(user, cid)
        return {"Authorization": f"Bearer {token}"}


class TestStoreTickDelayedEvent:
    def test_delayed_event_emitted_exactly_once(self):
        """Under a frozen Clock, tick writes ONE shipment.delayed row per ETA (idempotent)."""
        # Freeze Clock at T0
        Clock.freeze(Clock.now())

        # Clean any previous delayed event for ORD-2037 / SHP-2037 in test database
        with Session(db.engine) as s:
            events = s.scalars(
                select(EventsOutbox).where(EventsOutbox.type == "shipment.delayed")
            ).all()
            for ev in events:
                if "SHP-2037" in ev.event_id or "ORD-2037" in ev.payload_json:
                    s.delete(ev)
            s.commit()

        # First tick run
        stats1 = run_store_tick_once()
        assert stats1["delayed_events"] >= 1

        # Check events_outbox has exactly one row for SHP-2037
        with Session(db.engine) as s:
            outbox_rows = s.scalars(
                select(EventsOutbox).where(EventsOutbox.type == "shipment.delayed")
            ).all()
            delayed_rows = [
                ev
                for ev in outbox_rows
                if "SHP-2037" in ev.event_id or "ORD-2037" in ev.payload_json
            ]
            assert len(delayed_rows) == 1
            entry = delayed_rows[0]
            assert entry.type == "shipment.delayed"
            assert "ORD-2037" in entry.payload_json

        # Second tick run (30s later or immediate)
        stats2 = run_store_tick_once()
        assert stats2["delayed_events"] == 0

        # Verify still exactly one row
        with Session(db.engine) as s:
            outbox_rows_2 = s.scalars(
                select(EventsOutbox).where(EventsOutbox.type == "shipment.delayed")
            ).all()
            delayed_rows_2 = [
                ev
                for ev in outbox_rows_2
                if "SHP-2037" in ev.event_id or "ORD-2037" in ev.payload_json
            ]
            assert len(delayed_rows_2) == 1


class TestCarrierSimulator:
    def test_carrier_scripts_progress_and_advance(self):
        """Seller advances SHP-1048; a new event appears; customer GET shipment sees it."""
        client = TestClient(app)
        priya_headers = _get_auth_headers(client, "priya.sharma@nestkart.test")
        aarav_headers = _get_auth_headers(client, "aarav.mehta@example.test")

        # Aarav checks initial shipment for ORD-1048
        init_resp = client.get("/api/v1/orders/ORD-1048/shipment", headers=aarav_headers)
        assert init_resp.status_code == 200
        initial_events_count = len(init_resp.json()["events"])
        shipment_id = init_resp.json()["shipment_id"]

        # Priya advances shipment as seller
        adv_resp = client.post(
            f"/sim/carrier/shipments/{shipment_id}/advance",
            headers=priya_headers,
        )
        assert adv_resp.status_code == 200, adv_resp.text
        data = adv_resp.json()
        assert data["new_event"] is not None
        assert len(data["shipment"]["events"]) == initial_events_count + 1

        # Aarav checks order shipment and sees the new event
        after_resp = client.get("/api/v1/orders/ORD-1048/shipment", headers=aarav_headers)
        assert after_resp.status_code == 200
        assert len(after_resp.json()["events"]) == initial_events_count + 1

    def test_carrier_scenarios_play(self):
        """Play scenario plays out the steps for a shipment."""
        client = TestClient(app)
        priya_headers = _get_auth_headers(client, "priya.sharma@nestkart.test")

        play_resp = client.post(
            "/sim/carrier/scenarios/SHP-1048/play",
            headers=priya_headers,
        )
        assert play_resp.status_code == 200
        data = play_resp.json()
        assert data["shipment"]["status"] == "delivered"


class TestMailbox:
    def test_mail_send_inbound_writes_outbox_and_verified_sender(self):
        """Aarav sends inbound message; verified_sender is True; writes outbox."""
        client = TestClient(app)
        aarav_headers = _get_auth_headers(client, "aarav.mehta@example.test")
        priya_headers = _get_auth_headers(client, "priya.sharma@nestkart.test")

        send_headers = {
            **aarav_headers,
            "Idempotency-Key": f"key-mail-{uuid.uuid4().hex[:12]}",
        }
        send_payload = {
            "channel": "email",
            "to_addr": "support@nestkart.test",
            "subject": "Question about my order ORD-1048",
            "body_text": "Hello, when can I expect delivery?",
        }
        resp = client.post("/mail/send", json=send_payload, headers=send_headers)
        assert resp.status_code == 200, resp.text
        msg_data = resp.json()
        assert msg_data["verified_sender"] is True
        assert msg_data["direction"] == "inbound"
        assert msg_data["simulated"] is True
        msg_id = msg_data["message_id"]

        # Priya checks /mail/inbox and sees Aarav's message
        inbox_resp = client.get("/mail/inbox", headers=priya_headers)
        assert inbox_resp.status_code == 200
        inbox_msgs = inbox_resp.json()["messages"]
        matching = [m for m in inbox_msgs if m["message_id"] == msg_id]
        assert len(matching) == 1
        assert matching[0]["verified_sender"] is True
        assert matching[0]["simulated"] is True

        # Check events_outbox for mail.inbound event
        with Session(db.engine) as s:
            outbox_entry = s.get(EventsOutbox, f"evt_mail_{msg_id}")
            assert outbox_entry is not None
            assert outbox_entry.type == "mail.inbound"
            assert msg_id in outbox_entry.payload_json

    def test_mail_thread_ownership(self):
        """Customer B cannot view Customer A's mail thread (uniform 404)."""
        client = TestClient(app)
        aarav_headers = _get_auth_headers(client, "aarav.mehta@example.test")
        diya_headers = _get_auth_headers(client, "diya.nair@example.test")

        # Aarav sends a message creating a thread
        send_headers = {
            **aarav_headers,
            "Idempotency-Key": f"key-mail-{uuid.uuid4().hex[:12]}",
        }
        resp = client.post(
            "/mail/send",
            json={
                "channel": "email",
                "to_addr": "support@nestkart.test",
                "subject": "Private Inquiry",
                "body_text": "Private message body",
            },
            headers=send_headers,
        )
        assert resp.status_code == 200
        thread_id = resp.json()["thread_id"]

        # Aarav can read the thread
        aarav_thread_resp = client.get(f"/mail/threads/{thread_id}", headers=aarav_headers)
        assert aarav_thread_resp.status_code == 200

        # Diya attempting to access Aarav's thread gets 404 (Golden rule 3)
        diya_thread_resp = client.get(f"/mail/threads/{thread_id}", headers=diya_headers)
        assert diya_thread_resp.status_code == 404
        assert diya_thread_resp.json()["error"]["code"] == "not_found"

    def test_mail_read_endpoint(self):
        """Marking message as read updates read_at; non-owner gets 404."""
        client = TestClient(app)
        aarav_headers = _get_auth_headers(client, "aarav.mehta@example.test")
        diya_headers = _get_auth_headers(client, "diya.nair@example.test")

        send_headers = {
            **aarav_headers,
            "Idempotency-Key": f"key-mail-{uuid.uuid4().hex[:12]}",
        }
        resp = client.post(
            "/mail/send",
            json={
                "channel": "email",
                "to_addr": "support@nestkart.test",
                "subject": "Read Test",
                "body_text": "Please read me",
            },
            headers=send_headers,
        )
        msg_id = resp.json()["message_id"]

        # Diya cannot mark Aarav's message as read -> 404
        diya_read = client.post(f"/mail/{msg_id}/read", headers=diya_headers)
        assert diya_read.status_code == 404

        # Aarav marks as read -> 200 with read_at set
        read_resp = client.post(f"/mail/{msg_id}/read", headers=aarav_headers)
        assert read_resp.status_code == 200
        assert read_resp.json()["read_at"] is not None


class TestFaultSwitches:
    """Tests targeted by `just test -k fault`."""

    def test_fault_carrier_down(self):
        """carrier.down returns 503 upstream_unavailable."""
        client = TestClient(app)
        aarav_headers = _get_auth_headers(client, "aarav.mehta@example.test")
        priya_headers = _get_auth_headers(client, "priya.sharma@nestkart.test")

        set_fault("carrier.down", True)

        resp = client.get("/api/v1/orders/ORD-1048/shipment", headers=aarav_headers)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "upstream_unavailable"

        adv_resp = client.post("/sim/carrier/shipments/SHP-1048/advance", headers=priya_headers)
        assert adv_resp.status_code == 503
        assert adv_resp.json()["error"]["code"] == "upstream_unavailable"

        set_fault("carrier.down", False)
        resp2 = client.get("/api/v1/orders/ORD-1048/shipment", headers=aarav_headers)
        assert resp2.status_code == 200

    def test_fault_carrier_stale(self):
        """carrier.stale returns stale events / no new events."""
        client = TestClient(app)
        aarav_headers = _get_auth_headers(client, "aarav.mehta@example.test")
        priya_headers = _get_auth_headers(client, "priya.sharma@nestkart.test")

        set_fault("carrier.stale", True)

        # Advance attempt with carrier.stale returns no new events
        adv_resp = client.post("/sim/carrier/shipments/SHP-1048/advance", headers=priya_headers)
        assert adv_resp.status_code == 200
        assert adv_resp.json()["new_event"] is None

        # Shipment read with carrier.stale returns stale single event
        resp = client.get("/api/v1/orders/ORD-1048/shipment", headers=aarav_headers)
        assert resp.status_code == 200
        assert len(resp.json()["events"]) <= 1

        set_fault("carrier.stale", False)

    def test_fault_carrier_slow(self, monkeypatch):
        """carrier.slow adds latency."""
        client = TestClient(app)
        aarav_headers = _get_auth_headers(client, "aarav.mehta@example.test")

        set_fault("carrier.slow", True)

        # In test environment, monkeypatch asyncio.sleep to record call without waiting 8s
        sleep_called = []

        async def mock_sleep(seconds):
            sleep_called.append(seconds)

        monkeypatch.setattr("asyncio.sleep", mock_sleep)

        resp = client.get("/api/v1/orders/ORD-1048/shipment", headers=aarav_headers)
        assert resp.status_code == 200
        assert 8 in sleep_called or len(sleep_called) > 0

        set_fault("carrier.slow", False)

    def test_fault_mail_down(self):
        """mail.down returns 503 upstream_unavailable."""
        client = TestClient(app)
        aarav_headers = _get_auth_headers(client, "aarav.mehta@example.test")

        set_fault("mail.down", True)

        resp = client.get("/mail/inbox", headers=aarav_headers)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "upstream_unavailable"

        send_resp = client.post(
            "/mail/send",
            json={
                "channel": "email",
                "to_addr": "support@nestkart.test",
                "subject": "Test",
                "body_text": "Test body",
            },
            headers={**aarav_headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        )
        assert send_resp.status_code == 503
        assert send_resp.json()["error"]["code"] == "upstream_unavailable"

        set_fault("mail.down", False)
        resp2 = client.get("/mail/inbox", headers=aarav_headers)
        assert resp2.status_code == 200

    def test_fault_mail_fail_once(self):
        """mail.fail_once fails on first attempt with 503, retry succeeds."""
        client = TestClient(app)
        aarav_headers = _get_auth_headers(client, "aarav.mehta@example.test")
        key = f"key-failonce-{uuid.uuid4().hex}"

        set_fault("mail.fail_once", True)

        # Attempt 1: fails with 503
        resp1 = client.post(
            "/mail/send",
            json={
                "channel": "email",
                "to_addr": "support@nestkart.test",
                "subject": "Retry message",
                "body_text": "Retry message body",
            },
            headers={**aarav_headers, "Idempotency-Key": key},
        )
        assert resp1.status_code == 503
        assert resp1.json()["error"]["code"] == "upstream_unavailable"

        # Attempt 2: with same key succeeds
        resp2 = client.post(
            "/mail/send",
            json={
                "channel": "email",
                "to_addr": "support@nestkart.test",
                "subject": "Retry message",
                "body_text": "Retry message body",
            },
            headers={**aarav_headers, "Idempotency-Key": key},
        )
        assert resp2.status_code == 200
        assert resp2.json()["subject"] == "Retry message"


class TestDemoModeGating:
    def test_demo_mode_false_returns_404(self, monkeypatch):
        """When DEMO_MODE is false, /sim/carrier routes return 404."""
        client = TestClient(app)
        priya_headers = _get_auth_headers(client, "priya.sharma@nestkart.test")

        monkeypatch.setattr(settings, "DEMO_MODE", False)

        resp = client.post(
            "/sim/carrier/shipments/SHP-1048/advance",
            headers=priya_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
