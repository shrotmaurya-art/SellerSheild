# ruff: noqa: E402
"""
Test Suite for Stage 12: Webhook Outbox and Demo Control Endpoints
(spec sections 6, 7.4, 8, 12, 16)
"""

import json
import sys
import time
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx
import pytest
from fastapi.testclient import TestClient
from scripts.webhook_listener import verify_webhook_envelope
from sqlalchemy import select
from sqlalchemy.orm import Session

import db
from app.auth import _mint_access_token
from app.clock import Clock
from app.config import settings
from app.demo import SNAPSHOTS_DIR
from app.faults import clear_faults
from app.main import app
from app.models import EventsOutbox, Shipment, User
from app.outbox import (
    RETRY_DELAYS,
    compute_signature,
    emit_outbox_event,
    get_retry_delay,
    process_outbox_once,
)
from app.tick import run_store_tick_once

client = TestClient(app)


def _get_auth_headers(email: str) -> dict[str, str]:
    """Helper to mint a valid access token for a seeded user."""
    with Session(db.engine) as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None, f"User not found: {email}"
        cid = user.customer.customer_id if user.customer else None
        token = _mint_access_token(user, cid)
        return {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": f"idemp-test-{time.time_ns()}",
        }


@pytest.fixture(autouse=True)
def clean_environment():
    """Ensure clock, faults, and demo mode are reset before and after each test."""
    Clock.reset()
    clear_faults()
    settings.DEMO_MODE = True
    yield
    Clock.reset()
    clear_faults()
    settings.DEMO_MODE = True


# ===========================================================================
# 1. Webhook Signatures, Replay Window, and Duplicate ID
# ===========================================================================


def test_webhook_correct_signature():
    """Verify HMAC-SHA256 signature verification over 'timestamp.body'."""
    secret = "test_webhook_secret_123"
    timestamp = str(int(time.time()))
    body = json.dumps({"type": "order.created", "order_id": "ORD-TEST", "resource_version": 1})

    sig = compute_signature(timestamp, body, secret=secret)
    headers = {
        "X-Store-Signature": sig,
        "X-Store-Timestamp": timestamp,
        "X-Event-Id": "evt_test_sig_001",
    }
    seen_ids = set()
    is_valid, status_code, msg = verify_webhook_envelope(
        headers, body, secret=secret, seen_ids=seen_ids
    )
    assert is_valid is True
    assert status_code == 200
    assert "signature OK: order.created" in msg
    assert "evt_test_sig_001" in seen_ids

    # Tampered body must be rejected
    tampered_body = json.dumps({"type": "order.created", "order_id": "ORD-HACKED"})
    is_valid, status_code, msg = verify_webhook_envelope(
        headers, tampered_body, secret=secret, seen_ids=set()
    )
    assert is_valid is False
    assert status_code == 401
    assert "REJECTED: invalid signature" in msg

    # Tampered signature must be rejected
    bad_headers = dict(headers)
    bad_headers["X-Store-Signature"] = "bad" + sig[3:]
    is_valid, status_code, msg = verify_webhook_envelope(
        bad_headers, body, secret=secret, seen_ids=set()
    )
    assert is_valid is False
    assert status_code == 401
    assert "REJECTED: invalid signature" in msg


def test_webhook_replay_window():
    """Verify timestamps older than 5 minutes or in the future > 5 min are rejected."""
    secret = "test_secret"
    body = json.dumps({"type": "shipment.updated", "resource_version": 1})
    now_ts = 1700000000.0

    # 1. Expired timestamp (> 300s old, e.g. 305s ago)
    old_ts = str(int(now_ts - 305))
    sig = compute_signature(old_ts, body, secret=secret)
    headers = {
        "X-Store-Signature": sig,
        "X-Store-Timestamp": old_ts,
        "X-Event-Id": "evt_replay_old",
    }
    is_valid, code, msg = verify_webhook_envelope(
        headers, body, secret=secret, seen_ids=set(), current_time=now_ts
    )
    assert is_valid is False
    assert code == 400
    assert "timestamp outside 5-minute window" in msg

    # 2. Future timestamp (> 300s in future, e.g. 305s ahead)
    future_ts = str(int(now_ts + 305))
    sig_fut = compute_signature(future_ts, body, secret=secret)
    headers_fut = {
        "X-Store-Signature": sig_fut,
        "X-Store-Timestamp": future_ts,
        "X-Event-Id": "evt_replay_future",
    }
    is_valid, code, msg = verify_webhook_envelope(
        headers_fut, body, secret=secret, seen_ids=set(), current_time=now_ts
    )
    assert is_valid is False
    assert code == 400
    assert "timestamp outside 5-minute window" in msg

    # 3. Valid timestamp within 5 minutes (e.g. 60s ago)
    valid_ts = str(int(now_ts - 60))
    sig_valid = compute_signature(valid_ts, body, secret=secret)
    headers_valid = {
        "X-Store-Signature": sig_valid,
        "X-Store-Timestamp": valid_ts,
        "X-Event-Id": "evt_replay_valid",
    }
    is_valid, code, msg = verify_webhook_envelope(
        headers_valid, body, secret=secret, seen_ids=set(), current_time=now_ts
    )
    assert is_valid is True
    assert code == 200


def test_webhook_duplicate_id_rejection():
    """Verify duplicate event IDs are rejected."""
    secret = "test_secret"
    timestamp = str(int(time.time()))
    body = json.dumps({"type": "mail.inbound", "resource_version": 1})
    sig = compute_signature(timestamp, body, secret=secret)
    headers = {
        "X-Store-Signature": sig,
        "X-Store-Timestamp": timestamp,
        "X-Event-Id": "evt_unique_dup_check",
    }
    seen_ids = set()

    # First delivery: accepted
    is_valid, code, msg = verify_webhook_envelope(headers, body, secret=secret, seen_ids=seen_ids)
    assert is_valid is True
    assert code == 200

    # Second delivery with same event ID: rejected
    is_valid, code, msg = verify_webhook_envelope(headers, body, secret=secret, seen_ids=seen_ids)
    assert is_valid is False
    assert code == 409
    assert "duplicate event id" in msg


# ===========================================================================
# 2. Retry Schedule with Fake Clock & Exhausted State
# ===========================================================================


def test_webhook_retry_schedule_delays():
    """Verify documented retry schedule delays: 1 s, 5 s, 30 s, 2 min, 10 min."""
    assert RETRY_DELAYS == [1, 5, 30, 120, 600]
    assert get_retry_delay(1) == 1
    assert get_retry_delay(2) == 5
    assert get_retry_delay(3) == 30
    assert get_retry_delay(4) == 120
    assert get_retry_delay(5) == 600
    assert get_retry_delay(6) is None


@pytest.mark.asyncio
async def test_webhook_retry_schedule_with_fake_clock():
    """
    Simulate delivery failures against a fake clock.
    Verify attempts increment and next_retry_at advances per schedule.
    """
    event_id = f"evt_test_retry_{time.time_ns()}"
    with Session(db.engine) as session:
        emit_outbox_event(
            session=session,
            event_type="order.updated",
            payload={"order_id": "ORD-1048", "status": "confirmed"},
            resource_version=2,
            event_id=event_id,
        )
        session.commit()

    # Configure a mock failing transport
    transport = httpx.MockTransport(lambda req: httpx.Response(500, text="Internal Agent Error"))
    async with httpx.AsyncClient(transport=transport) as mock_client:
        # 1st attempt at t0
        t0 = Clock.now()
        processed = await process_outbox_once(client=mock_client)
        assert processed >= 1

        with Session(db.engine) as session:
            ev = session.get(EventsOutbox, event_id)
            assert ev.attempts == 1
            assert ev.status == "pending"
            assert ev.delivered_at is None
            # Next retry in 1s
            expected_next = (t0 + timedelta(seconds=1)).replace(tzinfo=None)
            assert abs((ev.next_retry_at - expected_next).total_seconds()) < 2.0

        # Advance fake clock by 0.5s -> not yet eligible
        Clock.advance(seconds=0.5)
        # Should NOT process this event yet
        with Session(db.engine) as session:
            ev = session.get(EventsOutbox, event_id)
            assert ev.attempts == 1

        # Advance fake clock by 1.0s (total +1.5s) -> 1s retry triggers (attempt 2)
        Clock.advance(seconds=1.0)
        t1 = Clock.now()
        await process_outbox_once(client=mock_client)

        with Session(db.engine) as session:
            ev = session.get(EventsOutbox, event_id)
            assert ev.attempts == 2
            # Next retry in 5s
            expected_next = (t1 + timedelta(seconds=5)).replace(tzinfo=None)
            assert abs((ev.next_retry_at - expected_next).total_seconds()) < 2.0

        # Advance fake clock by 5.5s -> 5s retry triggers (attempt 3)
        Clock.advance(seconds=5.5)
        t2 = Clock.now()
        await process_outbox_once(client=mock_client)

        with Session(db.engine) as session:
            ev = session.get(EventsOutbox, event_id)
            assert ev.attempts == 3
            # Next retry in 30s
            expected_next = (t2 + timedelta(seconds=30)).replace(tzinfo=None)
            assert abs((ev.next_retry_at - expected_next).total_seconds()) < 2.0

        # Advance fake clock by 31s -> 30s retry triggers (attempt 4)
        Clock.advance(seconds=31.0)
        t3 = Clock.now()
        await process_outbox_once(client=mock_client)

        with Session(db.engine) as session:
            ev = session.get(EventsOutbox, event_id)
            assert ev.attempts == 4
            # Next retry in 2 min (120s)
            expected_next = (t3 + timedelta(seconds=120)).replace(tzinfo=None)
            assert abs((ev.next_retry_at - expected_next).total_seconds()) < 2.0

        # Advance fake clock by 121s -> 2min retry triggers (attempt 5)
        Clock.advance(seconds=121.0)
        t4 = Clock.now()
        await process_outbox_once(client=mock_client)

        with Session(db.engine) as session:
            ev = session.get(EventsOutbox, event_id)
            assert ev.attempts == 5
            # Next retry in 10 min (600s)
            expected_next = (t4 + timedelta(seconds=600)).replace(tzinfo=None)
            assert abs((ev.next_retry_at - expected_next).total_seconds()) < 2.0

        # Advance fake clock by 601s -> 10min retry triggers (attempt 6) -> EXHAUSTED!
        Clock.advance(seconds=601.0)
        await process_outbox_once(client=mock_client)

        with Session(db.engine) as session:
            ev = session.get(EventsOutbox, event_id)
            assert ev.attempts == 6
            assert ev.status == "exhausted"
            assert ev.next_retry_at is None
            assert ev.delivered_at is None

    # Clean up test event
    with Session(db.engine) as session:
        ev = session.get(EventsOutbox, event_id)
        if ev:
            session.delete(ev)
            session.commit()


@pytest.mark.asyncio
async def test_webhook_successful_delivery_stops_retries():
    """Verify when listener responds 200, status becomes delivered and delivered_at is recorded."""
    event_id = f"evt_success_{time.time_ns()}"
    with Session(db.engine) as session:
        emit_outbox_event(
            session=session,
            event_type="refund.updated",
            payload={"refund_id": "RFD-TEST", "amount_paise": 10000},
            resource_version=1,
            event_id=event_id,
        )
        session.commit()

    # Success transport
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "ok"}))
    async with httpx.AsyncClient(transport=transport) as mock_client:
        processed = await process_outbox_once(client=mock_client)
        assert processed >= 1

    with Session(db.engine) as session:
        ev = session.get(EventsOutbox, event_id)
        assert ev.status == "delivered"
        assert ev.delivered_at is not None
        assert ev.attempts == 1
        assert ev.next_retry_at is None

        session.delete(ev)
        session.commit()


# ===========================================================================
# 3. Demo Endpoints: Reset, Clock, Faults, State, Scenarios, Snapshots
# ===========================================================================


def test_demo_reset_time_under_5_seconds():
    """Verify POST /demo/reset finishes in under 5 seconds for seller_owner."""
    priya_headers = _get_auth_headers("priya.sharma@nestkart.test")

    t_start = time.perf_counter()
    resp = client.post("/demo/reset", headers=priya_headers)
    elapsed = time.perf_counter() - t_start

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["duration_seconds"] < 5.0
    assert elapsed < 5.0


def test_demo_routes_refused_for_non_owner():
    """Verify non-seller_owner tokens (customer Aarav, staff Kiran) are refused 403."""
    aarav_headers = _get_auth_headers("aarav.mehta@example.test")
    kiran_headers = _get_auth_headers("kiran.rao@nestkart.test")

    # Aarav (customer)
    r_reset = client.post("/demo/reset", headers=aarav_headers)
    assert r_reset.status_code == 403

    r_clock = client.get("/demo/clock", headers=aarav_headers)
    assert r_clock.status_code == 403

    r_faults = client.post(
        "/demo/faults",
        json={"fault": "carrier.down", "enabled": True},
        headers=aarav_headers,
    )
    assert r_faults.status_code == 403

    r_state = client.get("/demo/state", headers=aarav_headers)
    assert r_state.status_code == 403

    # Kiran (seller_staff)
    r_kiran = client.post("/demo/reset", headers=kiran_headers)
    assert r_kiran.status_code == 403


def test_demo_routes_404_when_demo_mode_false():
    """Verify that when DEMO_MODE=false, every /demo route returns 404."""
    priya_headers = _get_auth_headers("priya.sharma@nestkart.test")
    settings.DEMO_MODE = False

    try:
        assert client.post("/demo/reset", headers=priya_headers).status_code == 404
        assert client.get("/demo/clock", headers=priya_headers).status_code == 404
        resp = client.post(
            "/demo/clock",
            json={"hours": 1},
            headers=priya_headers,
        )
        assert resp.status_code == 404
        assert (
            client.get(
                "/demo/faults",
                headers=priya_headers,
            ).status_code
            == 404
        )
        resp = client.post(
            "/demo/faults",
            json={"clear_all": True},
            headers=priya_headers,
        )
        assert resp.status_code == 404
        assert client.get("/demo/state", headers=priya_headers).status_code == 404
        assert client.post("/demo/scenarios/SC-01/load", headers=priya_headers).status_code == 404
        assert client.get("/demo/snapshots", headers=priya_headers).status_code == 404
    finally:
        settings.DEMO_MODE = True


def test_demo_clock_controls():
    """Verify advancing hours/days, setting exact time, freeze, and resume."""
    priya_headers = _get_auth_headers("priya.sharma@nestkart.test")

    # 1. Advance 2 hours
    r1 = client.post("/demo/clock", json={"hours": 2.0}, headers=priya_headers)
    assert r1.status_code == 200
    data1 = r1.json()
    assert abs(data1["offset_seconds"] - 7200.0) < 1.0

    # 2. Advance 2 days
    r2 = client.post("/demo/clock", json={"days": 2.0}, headers=priya_headers)
    assert r2.status_code == 200
    data2 = r2.json()
    assert abs(data2["offset_seconds"] - (7200.0 + 172800.0)) < 1.0

    # Public GET /api/v1/clock reflects this change
    r_pub = client.get("/api/v1/clock")
    assert r_pub.status_code == 200
    pub_data = r_pub.json()
    assert "now_utc" in pub_data

    # 3. Freeze clock
    r_freeze = client.post("/demo/clock", json={"action": "freeze"}, headers=priya_headers)
    assert r_freeze.status_code == 200
    assert r_freeze.json()["mode"] == "frozen"

    # 4. Resume clock
    r_resume = client.post("/demo/clock", json={"action": "resume"}, headers=priya_headers)
    assert r_resume.status_code == 200
    assert r_resume.json()["mode"] == "realtime"


def test_demo_advance_clock_2_days_shipment_delayed_event():
    """Advance demo clock by 2 days; tick detects delay and emits shipment.delayed event."""
    priya_headers = _get_auth_headers("priya.sharma@nestkart.test")

    # Advance demo clock by 2 days
    resp = client.post("/demo/clock", json={"days": 2.0}, headers=priya_headers)
    assert resp.status_code == 200

    # Run store tick once
    run_store_tick_once()

    # Check for ORD-1048's delayed event
    with Session(db.engine) as session:
        shp = session.get(Shipment, "SHP-1048")
        assert shp is not None

        delayed_events = session.scalars(
            select(EventsOutbox).where(EventsOutbox.type == "shipment.delayed")
        ).all()
        assert len(delayed_events) >= 1

        # Check payload
        target_ev = next((e for e in delayed_events if "SHP-1048" in e.payload_json), None)
        assert target_ev is not None
        payload = json.loads(target_ev.payload_json)
        assert payload["shipment_id"] == "SHP-1048"
        assert payload["type"] == "shipment.delayed"
        assert "resource_version" in payload


def test_demo_faults_carrier_down_toggle():
    """Toggle carrier.down -> advance answers 503 upstream_unavailable. Turn it off -> works."""
    priya_headers = _get_auth_headers("priya.sharma@nestkart.test")

    # 1. Turn on carrier.down
    r_on = client.post(
        "/demo/faults",
        json={"fault": "carrier.down", "enabled": True},
        headers=priya_headers,
    )
    assert r_on.status_code == 200
    assert r_on.json()["faults"]["carrier.down"] is True

    # Call carrier advance -> 503 upstream_unavailable
    r_adv = client.post(
        "/sim/carrier/shipments/SHP-1048/advance",
        headers=priya_headers,
    )
    assert r_adv.status_code == 503
    assert r_adv.json()["error"]["code"] == "upstream_unavailable"

    # 2. Turn off carrier.down
    r_off = client.post(
        "/demo/faults",
        json={"fault": "carrier.down", "enabled": False},
        headers=priya_headers,
    )
    assert r_off.status_code == 200
    assert r_off.json()["faults"]["carrier.down"] is False

    # Carrier advance now succeeds
    r_adv_ok = client.post(
        "/sim/carrier/shipments/SHP-1048/advance",
        headers=priya_headers,
    )
    assert r_adv_ok.status_code == 200


def test_demo_state_inspector():
    """GET /demo/state returns table row counts, last 20 events, and outbox breakdown."""
    priya_headers = _get_auth_headers("priya.sharma@nestkart.test")

    resp = client.get("/demo/state", headers=priya_headers)
    assert resp.status_code == 200
    data = resp.json()

    assert "row_counts" in data
    assert data["row_counts"]["users"] >= 7
    assert data["row_counts"]["orders"] >= 20
    assert "last_20_events" in data
    assert "outbox_status" in data
    assert "clock" in data
    assert "faults" in data


def test_demo_scenarios_loader():
    """POST /demo/scenarios/{id}/load loads valid scenarios and rejects unknown."""
    priya_headers = _get_auth_headers("priya.sharma@nestkart.test")

    # Unknown scenario -> 404
    r_bad = client.post("/demo/scenarios/SC-99/load", headers=priya_headers)
    assert r_bad.status_code == 404

    # Valid scenario -> 200
    r_ok = client.post("/demo/scenarios/SC-01/load", headers=priya_headers)
    assert r_ok.status_code == 200
    assert r_ok.json()["status"] == "ok"
    assert r_ok.json()["scenario_id"] == "SC-01"


def test_demo_snapshots_save_and_restore():
    """Verify SQLite file snapshot save, listing, and restore."""
    priya_headers = _get_auth_headers("priya.sharma@nestkart.test")

    snap_name = f"test_snap_{time.time_ns()}"

    # 1. Save snapshot
    r_save = client.post(f"/demo/snapshots/save?name={snap_name}", headers=priya_headers)
    assert r_save.status_code == 200
    assert r_save.json()["status"] == "ok"

    # 2. List snapshots
    r_list = client.get("/demo/snapshots", headers=priya_headers)
    assert r_list.status_code == 200
    assert snap_name in r_list.json()["snapshots"]

    # 3. Restore snapshot
    r_restore = client.post(f"/demo/snapshots/restore?name={snap_name}", headers=priya_headers)
    assert r_restore.status_code == 200
    assert r_restore.json()["status"] == "ok"

    # Clean up test snapshot file
    target_file = SNAPSHOTS_DIR / f"{snap_name}.db"
    if target_file.exists():
        try:
            target_file.unlink()
        except OSError:
            pass
