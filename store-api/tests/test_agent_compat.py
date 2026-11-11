"""Compatibility tests between store-api and SellerShield Agent."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import db
from app.auth import _mint_access_token, _mint_service_token
from app.config import settings
from app.main import app
from app.models import EventsOutbox, OrderItem, Refund, Return, ReturnItem, User
from app.outbox import emit_outbox_event, send_event_http

client = TestClient(app)


def _user_token(email: str) -> str:
    with Session(db.engine) as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None, f"Seeded user missing: {email}"
        cid = user.customer.customer_id if user.customer else None
        return _mint_access_token(user, cid)


def _cleanup_return(ret_id: str | None) -> None:
    if not ret_id:
        return
    with Session(db.engine) as session:
        rfds = session.scalars(select(Refund).where(Refund.return_id == ret_id)).all()
        for rfd in rfds:
            session.delete(rfd)
        ret_items = session.scalars(select(ReturnItem).where(ReturnItem.return_id == ret_id)).all()
        for ri in ret_items:
            session.delete(ri)
        ret = session.get(Return, ret_id)
        if ret is not None:
            session.delete(ret)
        session.commit()


def test_jwt_issuer_kid_iat_and_root_jwks() -> None:
    """Store tokens use iss='nestkart-store', header kid='nestkart-1', iat, and root JWKS."""
    assert settings.JWT_ISSUER == "nestkart-store"

    user_tok = _user_token("aarav.mehta@example.test")
    srv_tok = _mint_service_token("agent-monitor", "orders:read_all mail:send")

    for tok in (user_tok, srv_tok):
        hdr = pyjwt.get_unverified_header(tok)
        claims = pyjwt.decode(tok, options={"verify_signature": False})
        assert hdr.get("kid") == "nestkart-1"
        assert claims.get("iss") == "nestkart-store"
        assert isinstance(claims.get("iat"), int)

    r_root = client.get("/.well-known/jwks.json")
    r_v1 = client.get("/api/v1/.well-known/jwks.json")
    assert r_root.status_code == 200
    assert r_v1.status_code == 200
    assert r_root.json() == r_v1.json()
    assert r_root.json()["keys"][0]["kid"] == "nestkart-1"


def test_admin_settings_allows_monitor_read_blocks_staff_and_service_write() -> None:
    """GET /admin/settings accepts owner & monitor (orders:read_all); PUT remains owner-only."""
    monitor_tok = _mint_service_token("agent-monitor", "orders:read_all mail:send")
    executor_tok = _mint_service_token(
        "agent-executor", "orders:read_all refunds:reserve refunds:release returns:transition"
    )
    executor_no_read_tok = _mint_service_token("agent-executor", "refunds:reserve")
    staff_tok = _user_token("kiran.rao@nestkart.test")
    customer_tok = _user_token("aarav.mehta@example.test")

    r_mon = client.get(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {monitor_tok}"},
    )
    assert r_mon.status_code == 200
    assert "autonomy_cap_paise" in r_mon.json()

    assert (
        client.get(
            "/api/v1/admin/settings",
            headers={"Authorization": f"Bearer {executor_no_read_tok}"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/admin/settings",
            headers={"Authorization": f"Bearer {staff_tok}"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/admin/settings",
            headers={"Authorization": f"Bearer {customer_tok}"},
        ).status_code
        == 403
    )

    assert (
        client.put(
            "/api/v1/admin/settings",
            headers={"Authorization": f"Bearer {monitor_tok}"},
            json={"autonomy_cap_paise": 60000},
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/v1/admin/settings",
            headers={"Authorization": f"Bearer {executor_tok}"},
            json={"autonomy_cap_paise": 60000},
        ).status_code
        == 403
    )


def test_create_return_sets_client_ref_and_admin_order_exposes_returns_and_refunds() -> None:
    """create_return stores Idempotency-Key in client_ref and /admin/orders/{id} embeds details."""
    aarav_tok = _user_token("aarav.mehta@example.test")
    monitor_tok = _mint_service_token("agent-monitor", "orders:read_all")
    exec_tok = _mint_service_token(
        "agent-executor", "orders:read_all refunds:reserve refunds:release returns:transition"
    )

    with Session(db.engine) as session:
        oi = session.scalar(select(OrderItem).where(OrderItem.order_id == "ORD-1042"))
        assert oi is not None
        item_id = oi.item_id
        stale = session.scalars(select(Return).where(Return.order_id == "ORD-1042")).all()
        for r in stale:
            for ri in r.items:
                session.delete(ri)
            session.delete(r)
        session.commit()

    ret_id: str | None = None
    try:
        ret_idem = f"prp-compat-{uuid.uuid4().hex[:8]}:create_return"
        r_ret = client.post(
            "/api/v1/orders/ORD-1042/returns",
            headers={"Authorization": f"Bearer {aarav_tok}", "Idempotency-Key": ret_idem},
            json={
                "items": [{"item_id": item_id, "qty": 1}],
                "reason_code": "defective",
                "reason_text": "Unit compat check",
                "resolution": "refund",
            },
        )
        assert r_ret.status_code == 201
        ret_data = r_ret.json()
        ret_id = ret_data["return_id"]
        assert ret_data["client_ref"] == ret_idem

        rfd_idem = f"prp-compat-{uuid.uuid4().hex[:8]}:reserve_refund"
        r_rfd = client.post(
            "/api/v1/orders/ORD-1042/refund-reservations",
            headers={"Authorization": f"Bearer {exec_tok}", "Idempotency-Key": rfd_idem},
            json={"amount_paise": 50000, "return_id": ret_id, "method": "original"},
        )
        assert r_rfd.status_code == 201
        assert r_rfd.json()["client_ref"] == rfd_idem

        r_admin = client.get(
            "/api/v1/admin/orders/ORD-1042",
            headers={"Authorization": f"Bearer {monitor_tok}"},
        )
        assert r_admin.status_code == 200
        body = r_admin.json()
        assert any(
            r["return_id"] == ret_id and r["client_ref"] == ret_idem for r in body["returns"]
        )
        assert any(rf["client_ref"] == rfd_idem for rf in body["refunds"])

        # Check refund.updated outbox event for reserve carries return_id
        with Session(db.engine) as session:
            evs = session.scalars(
                select(EventsOutbox)
                .where(EventsOutbox.type == "refund.updated")
                .order_by(EventsOutbox.created_at.desc())
            ).all()
            refund_id = r_rfd.json()["refund_id"]
            matching = [
                json.loads(e.payload_json) for e in evs if refund_id in e.payload_json
            ]
            assert matching
            assert matching[0].get("return_id") == ret_id
    finally:
        _cleanup_return(ret_id)


def test_executor_can_cancel_above_cap_return_during_compensation() -> None:
    """Executor can cancel requested return above ₹500 autonomy cap while approved still blocks."""
    aarav_tok = _user_token("aarav.mehta@example.test")
    exec_tok = _mint_service_token(
        "agent-executor", "orders:read_all refunds:reserve refunds:release returns:transition"
    )

    with Session(db.engine) as session:
        oi = session.scalar(select(OrderItem).where(OrderItem.order_id == "ORD-1042"))
        assert oi is not None
        item_id = oi.item_id
        stale = session.scalars(select(Return).where(Return.order_id == "ORD-1042")).all()
        for r in stale:
            for ri in r.items:
                session.delete(ri)
            session.delete(r)
        session.commit()

    ret_id: str | None = None
    try:
        r_ret = client.post(
            "/api/v1/orders/ORD-1042/returns",
            headers={
                "Authorization": f"Bearer {aarav_tok}",
                "Idempotency-Key": f"idem-{uuid.uuid4().hex}",
            },
            json={
                "items": [{"item_id": item_id, "qty": 1}],
                "reason_code": "defective",
                "reason_text": "Above cap item",
                "resolution": "refund",
            },
        )
        assert r_ret.status_code == 201
        ret_id = r_ret.json()["return_id"]

        # Approve still blocked by autonomy cap (120000 > 50000)
        r_app = client.post(
            f"/api/v1/returns/{ret_id}/transition",
            headers={
                "Authorization": f"Bearer {exec_tok}",
                "Idempotency-Key": f"idem-{uuid.uuid4().hex}",
            },
            json={"to": "approved", "reason": "auto"},
        )
        assert r_app.status_code == 409
        assert r_app.json()["error"]["code"] == "limit_exceeded"

        # Cancel allowed for saga compensation even above autonomy cap
        r_can = client.post(
            f"/api/v1/returns/{ret_id}/transition",
            headers={
                "Authorization": f"Bearer {exec_tok}",
                "Idempotency-Key": f"idem-{uuid.uuid4().hex}",
            },
            json={"to": "cancelled", "reason": "Compensating failed proposal"},
        )
        assert r_can.status_code == 200
        assert r_can.json()["status"] == "cancelled"
    finally:
        _cleanup_return(ret_id)


@pytest.mark.asyncio
async def test_send_event_http_wraps_signed_webhook_envelope() -> None:
    """Outbox wire POST wraps flat payload into WebhookEnvelope with tz-aware occurred_at."""
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"received": True})

    event_id = f"evt_wire_{uuid.uuid4().hex[:10]}"
    with Session(db.engine) as session:
        ev = emit_outbox_event(
            session=session,
            event_type="return.updated",
            payload={
                "return_id": "RET-9001",
                "order_id": "ORD-1042",
                "status": "inspected_ok",
            },
            resource_version=4,
            event_id=event_id,
        )
        session.commit()
        session.refresh(ev)

    try:
        secret = "compat_test_webhook_secret_123456"
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as mock_client:
            ok, err = await send_event_http(
                ev,
                webhook_url="http://agent.local/v1/webhooks/store",
                secret=secret,
                client=mock_client,
            )
        assert ok is True, err
        assert len(captured) == 1
        req = captured[0]
        raw_body = req.content.decode("utf-8")
        envelope = json.loads(raw_body)
        assert set(envelope.keys()) == {
            "event_id",
            "type",
            "occurred_at",
            "resource_version",
            "data",
        }
        assert envelope["event_id"] == event_id
        assert envelope["type"] == "return.updated"
        assert envelope["resource_version"] == 4
        dt = datetime.fromisoformat(envelope["occurred_at"])
        assert dt.tzinfo is not None and dt.utcoffset() is not None
        assert envelope["data"]["return_id"] == "RET-9001"
        assert envelope["data"]["status"] == "inspected_ok"

        ts = req.headers["X-Store-Timestamp"]
        sig = req.headers["X-Store-Signature"]
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{ts}.{raw_body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert sig == expected
    finally:
        with Session(db.engine) as session:
            row = session.get(EventsOutbox, event_id)
            if row is not None:
                session.delete(row)
                session.commit()
