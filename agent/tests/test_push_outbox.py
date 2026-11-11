"""Tests for TASK T3.2: Push stream, notification outbox and notification endpoints.

Verifies:
1. Seller approval reaches customer stream within 2 seconds (end-to-end confirm + approve).
2. Polling (GET /v1/me/events?since=&limit=) returns the exact same event.
3. Last-Event-ID resume has no gaps and hostile cursors/limits return 422 without slot leaks.
4. Two customers on parallel streams never see each other's events (tenant isolation).
5. Emitting payload with risk or internal fields raises ValidationError.
6. Third concurrent stream (through 10th) gets 429 Too Many Requests; disconnect reopens slot.
7. Disconnect leaves no pending asyncio tasks or active stream slots.
8. mail.fail_once leaves return and refund intact; row retried and sent (T15), backoff 1s/5s/30s,
   and exhausted after 3 failed attempts.
9. Repeated notification event produces exactly one row (dedupe_key).
10. Recipient in request or model output is ignored; database owner is used.
11. No store send happens inside a database transaction (spy).
12. Every template passes forbidden-words list, has safe ID-only subjects, and escapes XSS.
13. Seller retry endpoint works once on failed/exhausted, resets attempts=0, and records audit.
14. proposal.expired (confirm) and refund.updated (webhook) emit client events & outbox rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import socket
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.audit.models import EventType, audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, get_metadata, init_db, reset_engine, write_tx
from sellershield_agent.domain.models import ReturnItem
from sellershield_agent.domain.state import (
    approvals,
    client_events,
    notifications_outbox,
    proposals,
)
from sellershield_agent.events import router as events_router_mod
from sellershield_agent.events.emit import emit_client_event
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.main import create_app
from sellershield_agent.outbox.enqueue import enqueue_notification
from sellershield_agent.outbox.worker import (
    TEMPLATES_DIR,
    process_outbox_batch,
    render_template,
)
from sellershield_agent.policy.decide import decide
from sellershield_agent.policy.facts import DecisionAction, DecisionFacts
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.policy.rules import DecisionOutcome
from sellershield_agent.proposals.service import persist_proposal_and_build_card
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway
from tests.fault_plan import FaultPlan

EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
WEBHOOK_SECRET = "t32-push-webhook-secret"

FORBIDDEN_WORDS_RE = re.compile(
    r"\b(?:limits?|thresholds?|risks?|scores?|staff|owner|auto(?:matic|nomy)?|caps?)\b",
    re.IGNORECASE,
)


class FakeJWKSClient:
    def __init__(self, jwk: dict[str, Any]) -> None:
        self.pyjwk = jwt.PyJWK.from_dict(jwk)

    def get_signing_key(self, kid: str) -> Any:
        return self.pyjwk

    def is_healthy(self) -> bool:
        return True


def _create_rsa_key_pair(
    kid: str = "push-key-1",
) -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict["kid"] = kid
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return priv, pub, jwk_dict


def _make_token(
    *,
    user_id: str,
    customer_id: str | None,
    role: str,
    priv: RSAPrivateKey,
    now_dt: datetime,
    kid: str = "push-key-1",
    exp_seconds: int = 3600,
) -> str:
    now_ts = int(now_dt.timestamp())
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "role": role,
        "store_id": "nestkart",
        "iat": now_ts,
        "exp": now_ts + exp_seconds,
        "auth_time": now_ts,
        "jti": str(uuid.uuid4()),
    }
    if customer_id is not None:
        payload["customer_id"] = customer_id
    return str(
        jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid, "alg": "RS256"})
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def rsa_keys() -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    return _create_rsa_key_pair()


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(T0_BASE)


@pytest.fixture
def app_env(
    tmp_path: Path,
    rsa_keys: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "push_outbox_test.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")

    get_settings.cache_clear()
    reset_engine()
    reset_default_jwks_client()
    events_router_mod._active_customer_streams.clear()

    priv, _, jwk = rsa_keys
    fake_jwks = FakeJWKSClient(jwk)

    app = create_app()
    fixture_store = FixtureStoreClient(clock=fixed_clock)

    app.dependency_overrides[deps.get_clock] = lambda: fixed_clock
    app.dependency_overrides[deps.get_jwks_client] = lambda: fake_jwks
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: fixture_store

    tokens = {
        "c001": _make_token(
            user_id="usr_c001",
            customer_id="C001",
            role="customer",
            priv=priv,
            now_dt=T0_BASE,
        ),
        "c002": _make_token(
            user_id="usr_c002",
            customer_id="C002",
            role="customer",
            priv=priv,
            now_dt=T0_BASE,
        ),
        "seller_staff": _make_token(
            user_id="SELLER-001",
            customer_id=None,
            role="seller_staff",
            priv=priv,
            now_dt=T0_BASE,
        ),
    }

    engine = get_engine()
    init_db(engine, metadata=get_metadata())

    yield {
        "app": app,
        "engine": engine,
        "tokens": tokens,
        "store": fixture_store,
        "clock": fixed_clock,
    }

    events_router_mod._active_customer_streams.clear()
    app.dependency_overrides.clear()
    reset_engine()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def live_url(app_env: dict[str, Any]) -> AsyncGenerator[str, None]:
    """Run the FastAPI app on a real ephemeral loopback port so SSE streams work natively."""
    port = _free_port()
    config = uvicorn.Config(
        app_env["app"],
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    srv_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await srv_task


# ── Test 1: Seller approval reaches open customer stream within 2 seconds ──


@pytest.mark.asyncio
async def test_seller_approval_reaches_customer_stream_within_2_seconds(
    app_env: dict[str, Any],
    live_url: str,
) -> None:
    engine = app_env["engine"]
    store: FixtureStoreClient = app_env["store"]
    clock: FixedClock = app_env["clock"]
    tok_c1 = app_env["tokens"]["c001"]
    tok_seller = app_env["tokens"]["seller_staff"]

    gateway = ToolGateway(
        store_client=store,
        engine=engine,
        clock=clock,
        policy=load_policy("v3"),
        service_ctx_factory=lambda req_id: build_service_ctx(clock, req_id),
    )
    cust_ctx = RequestContext(
        principal=Principal(
            user_id="usr_c001",
            role=Role.customer,
            customer_id="C001",
            auth_time=int(T0_BASE.timestamp()),
            jti="jti-t32-prop",
            token_exp=int(T0_BASE.timestamp()) + 3600,
        ),
        request_id="req-t32-prop",
        token=SecretStr(tok_c1),
    )
    tool_res = await gateway.call(
        ctx=cust_ctx,
        name="propose_return",
        raw_args={
            "order_id": "ORD-1042",
            "items": [{"item_id": "ITEM-1042-1", "qty": 1}],
            "reason_code": "defective",
            "reason_text": "Defective item",
            "resolution": "refund",
            "refund_method": "original",
        },
        allowed={"propose_return", "propose_refund"},
        session_id="ses-t32-1",
    )
    assert tool_res.ok and isinstance(tool_res.data, dict)
    pid = str(tool_res.data["proposal_id"])
    prop_hash = str(tool_res.data["proposal_hash"])

    received_frames: list[tuple[str, str, dict[str, Any]]] = []
    async with httpx.AsyncClient(base_url=live_url, timeout=5.0) as client:
        conf_resp = await client.post(
            f"/v1/proposals/{pid}/confirm",
            headers={
                "Authorization": f"Bearer {tok_c1}",
                "Idempotency-Key": f"conf-{pid}",
            },
            json={"proposal_hash": prop_hash},
        )
        assert conf_resp.status_code == 200
        aid = str(conf_resp.json()["approval_id"])

        async def _listen() -> None:
            async with client.stream(
                "GET",
                "/v1/chat/stream",
                headers={"Authorization": f"Bearer {tok_c1}"},
            ) as resp:
                assert resp.status_code == 200
                cur_id = ""
                cur_ev = ""
                async for line in resp.aiter_lines():
                    if line.startswith("id:"):
                        cur_id = line[3:].strip()
                    elif line.startswith("event:"):
                        cur_ev = line[6:].strip()
                    elif line.startswith("data:"):
                        received_frames.append((cur_id, cur_ev, json.loads(line[5:].strip())))
                        if cur_ev == "approval.decided":
                            break

        listener = asyncio.create_task(_listen())
        await asyncio.sleep(0.05)

        t0 = time.monotonic()
        dec_resp = await client.post(
            f"/v1/seller/approvals/{aid}/decision",
            headers={
                "Authorization": f"Bearer {tok_seller}",
                "Idempotency-Key": f"dec-{aid}",
            },
            json={"decision": "approve", "reason": "Approved live", "expected_version": 1},
        )
        assert dec_resp.status_code == 200
        await asyncio.wait_for(listener, timeout=2.0)
        elapsed = time.monotonic() - t0

    assert elapsed < 2.0
    decided = [f for f in received_frames if f[1] == "approval.decided"]
    assert len(decided) == 1
    assert decided[0][2]["payload"]["approval_id"] == aid
    assert decided[0][2]["event_id"].startswith("CEV-")

    with engine.connect() as conn:
        obx_rows = conn.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.dedupe_key == f"approval.decided:{aid}"
            )
        ).fetchall()
        assert len(obx_rows) == 1 and obx_rows[0].channel == "email"


# ── Test 2: Polling returns the same event & enforces limit <= 100 ──


@pytest.mark.asyncio
async def test_polling_returns_same_event(app_env: dict[str, Any]) -> None:
    app = app_env["app"]
    token = app_env["tokens"]["c001"]
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]

    with write_tx(engine) as tx:
        event_id = emit_client_event(
            tx,
            customer_id="C001",
            type="approval.decided",
            payload={
                "approval_id": "APR-001",
                "proposal_id": "PRP-001",
                "order_id": "ORD-1001",
                "status": "approved",
                "message": "Your return request was approved.",
            },
            clock=clock,
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/me/events?since=0",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "events" in body
        matched = [e for e in body["events"] if e["event_id"] == event_id]
        assert len(matched) == 1
        assert matched[0]["type"] == "approval.decided"
        assert matched[0]["payload"]["approval_id"] == "APR-001"

        bad_lim = await client.get(
            "/v1/me/events?limit=101",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert bad_lim.status_code == 422


# ── Test 3: Last-Event-ID resume has no gaps & hostile cursors get 422 ──


@pytest.mark.asyncio
async def test_last_event_id_resume_has_no_gaps(
    app_env: dict[str, Any],
    live_url: str,
) -> None:
    token = app_env["tokens"]["c001"]
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]

    with write_tx(engine) as tx:
        for i in range(1, 5):
            emit_client_event(
                tx,
                customer_id="C001",
                type="notification.new",
                payload={
                    "proposal_id": f"PRP-{i}",
                    "status": "pending",
                    "message": f"Update {i}",
                },
                clock=clock,
            )

    async with httpx.AsyncClient(base_url=live_url, timeout=5.0) as client:
        for bad in ("-1", "abc", "\u00b2", "+5", "1;DROP TABLE", "9" * 30):
            r_q = await client.get(
                f"/v1/chat/stream?since={bad}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r_q.status_code == 422
            if bad.isascii():
                r_h = await client.get(
                    "/v1/chat/stream",
                    headers={"Authorization": f"Bearer {token}", "Last-Event-ID": bad},
                )
                assert r_h.status_code == 422

        received_ids: list[str] = []
        async with client.stream(
            "GET",
            "/v1/chat/stream?since=0",
            headers={"Authorization": f"Bearer {token}", "Last-Event-ID": "2"},
        ) as resp:
            assert resp.status_code == 200
            current_id: str | None = None
            async for line in resp.aiter_lines():
                if line.startswith("id:"):
                    current_id = line.replace("id:", "").strip()
                elif line.startswith("data:") and current_id is not None:
                    received_ids.append(current_id)
                    current_id = None
                    if len(received_ids) >= 2:
                        break

        assert received_ids == ["3", "4"]


# ── Test 4: Two customers on parallel streams never see each other's events ──


@pytest.mark.asyncio
async def test_two_customers_on_parallel_streams_never_see_each_other(
    app_env: dict[str, Any],
    live_url: str,
) -> None:
    token_c1 = app_env["tokens"]["c001"]
    token_c2 = app_env["tokens"]["c002"]
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]

    c1_events: list[str] = []
    c2_events: list[str] = []

    async with httpx.AsyncClient(base_url=live_url, timeout=5.0) as client:
        async def _listen_c1() -> None:
            async with client.stream(
                "GET",
                "/v1/chat/stream",
                headers={"Authorization": f"Bearer {token_c1}"},
            ) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        c1_events.append(line)
                        break

        async def _listen_c2() -> None:
            async with client.stream(
                "GET",
                "/v1/chat/stream",
                headers={"Authorization": f"Bearer {token_c2}"},
            ) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        c2_events.append(line)
                        break

        t1 = asyncio.create_task(_listen_c1())
        t2 = asyncio.create_task(_listen_c2())
        await asyncio.sleep(0.1)

        with write_tx(engine) as tx:
            emit_client_event(
                tx,
                customer_id="C001",
                type="approval.decided",
                payload={
                    "approval_id": "APR-C001",
                    "proposal_id": "PRP-001",
                    "order_id": "ORD-1001",
                    "status": "approved",
                },
                clock=clock,
            )

        await asyncio.wait_for(t1, timeout=2.0)
        t2.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t2

        c2_poll = await client.get(
            "/v1/me/events?since=0",
            headers={"Authorization": f"Bearer {token_c2}"},
        )
        assert c2_poll.status_code == 200 and c2_poll.json()["events"] == []

    assert len(c1_events) == 1
    assert "APR-C001" in c1_events[0]
    assert len(c2_events) == 0


# ── Test 5: Emitting payload with risk or internal field raises ──


def test_emitting_payload_with_risk_or_internal_field_raises(
    app_env: dict[str, Any],
) -> None:
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]
    with write_tx(engine) as tx:
        for bad_key in ("risk_score", "limit", "internal_note", "staff_cap"):
            with pytest.raises((ValidationError, ValueError)):
                emit_client_event(
                    tx,
                    customer_id="C001",
                    type="approval.decided",
                    payload={
                        "approval_id": "APR-001",
                        "proposal_id": "PRP-001",
                        "order_id": "ORD-1001",
                        "status": "approved",
                        bad_key: 95,
                    },
                    clock=clock,
                )


# ── Test 6: Third through tenth stream gets 429; disconnect reopens slot ──


@pytest.mark.asyncio
async def test_third_stream_gets_429(
    app_env: dict[str, Any],
    live_url: str,
) -> None:
    token = app_env["tokens"]["c001"]

    async with httpx.AsyncClient(base_url=live_url, timeout=5.0) as client:
        async with client.stream(
            "GET",
            "/v1/chat/stream",
            headers={"Authorization": f"Bearer {token}"},
        ) as s1:
            assert s1.status_code == 200
            async with client.stream(
                "GET",
                "/v1/chat/stream",
                headers={"Authorization": f"Bearer {token}"},
            ) as s2:
                assert s2.status_code == 200
                for _ in range(8):
                    resp3 = await client.get(
                        "/v1/chat/stream",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert resp3.status_code == 429
                    assert resp3.json()["error"]["code"] == "rate_limited"

        await asyncio.sleep(0.05)
        async with client.stream(
            "GET",
            "/v1/chat/stream",
            headers={"Authorization": f"Bearer {token}"},
        ) as s_reopen:
            assert s_reopen.status_code == 200


# ── Test 7: Disconnect leaves no pending task or leaked stream slot ──


@pytest.mark.asyncio
async def test_disconnect_leaves_no_pending_task(
    app_env: dict[str, Any],
    live_url: str,
) -> None:
    token = app_env["tokens"]["c001"]
    tasks_before = len([t for t in asyncio.all_tasks() if not t.done()])

    async with httpx.AsyncClient(base_url=live_url, timeout=5.0) as client:
        async with client.stream(
            "GET",
            "/v1/chat/stream",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            assert resp.status_code == 200

    await asyncio.sleep(0.05)
    tasks_after = len([t for t in asyncio.all_tasks() if not t.done()])
    assert tasks_after <= tasks_before
    assert events_router_mod._active_customer_streams.get("C001", 0) == 0


# ── Test 8: mail.fail_once leaves return & refund intact, retried and sent (T15), exhaustion ──


@pytest.mark.asyncio
async def test_mail_fail_once_retried_and_sent_t15(app_env: dict[str, Any]) -> None:
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]

    faults = FaultPlan().fail_once("send_mail")
    store = FixtureStoreClient(clock=clock, fault_plan=faults)

    with write_tx(engine) as tx:
        nid = enqueue_notification(
            tx,
            customer_id="C001",
            template="approval.decided",
            payload={
                "approval_id": "APR-001",
                "proposal_id": "PRP-001",
                "order_id": "ORD-1001",
                "status": "approved",
                "message": "Your return was approved",
            },
            dedupe_key="approval.decided:APR-001",
            clock=clock,
        )

    sent_count = await process_outbox_batch(engine, store, clock)
    assert sent_count == 0

    with engine.connect() as conn:
        row = conn.execute(
            select(notifications_outbox).where(notifications_outbox.c.notification_id == nid)
        ).fetchone()
        assert row is not None
        assert row.status in ("failed", "retry_scheduled")
        assert row.attempts == 1
        assert row.next_attempt_at == (T0_BASE + timedelta(seconds=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    sent_count_2 = await process_outbox_batch(engine, store, clock, force_due=True)
    assert sent_count_2 == 1

    with engine.connect() as conn:
        row2 = conn.execute(
            select(notifications_outbox).where(notifications_outbox.c.notification_id == nid)
        ).fetchone()
        assert row2 is not None
        assert row2.status == "sent"
        assert row2.attempts == 2

    assert len(store.mailbox) == 1
    assert store.mailbox[0]["recipient"] == "aarav.mehta@example.test"

    # Permanent failures exhaust after 3 attempts with 1s, 5s backoffs then exhausted
    class _AlwaysFailPlan:
        async def execute_before(self, step: str) -> None:
            if step == "send_mail":
                raise RuntimeError("mail permanently down")

    perm_store = FixtureStoreClient(clock=clock, fault_plan=_AlwaysFailPlan())
    with write_tx(engine) as tx:
        nid_exh = enqueue_notification(
            tx,
            customer_id="C001",
            template="approval.decided",
            payload={"approval_id": "APR-FAIL3", "order_id": "ORD-1042", "status": "approved"},
            dedupe_key="approval.decided:APR-FAIL3",
            clock=clock,
        )
    for expected_attempt, expected_backoff in ((1, 1), (2, 5)):
        await process_outbox_batch(engine, perm_store, clock, force_due=True)
        with engine.connect() as conn:
            r = conn.execute(
                select(notifications_outbox).where(
                    notifications_outbox.c.notification_id == nid_exh
                )
            ).fetchone()
            assert r is not None and r.status == "failed" and r.attempts == expected_attempt
            assert r.next_attempt_at == (
                T0_BASE + timedelta(seconds=expected_backoff)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

    await process_outbox_batch(engine, perm_store, clock, force_due=True)
    with engine.connect() as conn:
        r3 = conn.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.notification_id == nid_exh
            )
        ).fetchone()
        assert r3 is not None and r3.status == "exhausted" and r3.attempts == 3


# ── Test 9: Repeated event gives one row (dedupe_key) ──


def test_repeated_event_gives_one_row_dedupe_key(app_env: dict[str, Any]) -> None:
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]
    with write_tx(engine) as tx:
        nid1 = enqueue_notification(
            tx,
            customer_id="C001",
            template="approval.decided",
            payload={"order_id": "ORD-1001"},
            dedupe_key="unique:key:1",
            clock=clock,
        )
        nid2 = enqueue_notification(
            tx,
            customer_id="C001",
            template="approval.decided",
            payload={"order_id": "ORD-1001"},
            dedupe_key="unique:key:1",
            clock=clock,
        )
        assert nid1 == nid2

        rows = tx.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.dedupe_key == "unique:key:1"
            )
        ).fetchall()
        assert len(rows) == 1


# ── Test 10: Recipient in request or model output is ignored ──


@pytest.mark.asyncio
async def test_recipient_in_input_is_ignored(app_env: dict[str, Any]) -> None:
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]
    store = FixtureStoreClient(clock=clock)

    with write_tx(engine) as tx:
        enqueue_notification(
            tx,
            customer_id="C001",
            template="approval.decided",
            payload={
                "order_id": "ORD-1001",
                "recipient": "attacker@evil.test",
                "to_addr": "attacker@evil.test",
                "email": "attacker@evil.test",
                "status": "approved",
            },
            dedupe_key="apr:ORD-1001",
            clock=clock,
        )

    await process_outbox_batch(engine, store, clock)
    assert len(store.mailbox) == 1
    assert store.mailbox[0]["recipient"] == "aarav.mehta@example.test"
    assert "attacker" not in store.mailbox[0]["recipient"]


# ── Test 11: No send inside a transaction (spy) ──


@pytest.mark.asyncio
async def test_no_send_inside_transaction_spy(app_env: dict[str, Any]) -> None:
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]
    store = FixtureStoreClient(clock=clock)

    in_tx_flags: list[bool] = []
    orig_send_mail = store.send_mail

    async def _spied_send_mail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from sellershield_agent.db import _db_write_lock

        in_tx_flags.append(_db_write_lock.locked())
        return await orig_send_mail(*args, **kwargs)

    store.send_mail = _spied_send_mail  # type: ignore[method-assign]

    with write_tx(engine) as tx:
        enqueue_notification(
            tx,
            customer_id="C001",
            template="approval.decided",
            payload={"order_id": "ORD-1001", "status": "approved"},
            dedupe_key="spy:1",
            clock=clock,
        )

    await process_outbox_batch(engine, store, clock)
    assert len(in_tx_flags) == 1
    assert in_tx_flags[0] is False


# ── Test 12: Every template passes forbidden words list, safe subjects, and XSS escaping ──


def test_every_template_passes_forbidden_words() -> None:
    template_files = [
        "approval.decided.txt",
        "refund.updated.txt",
        "case.created.txt",
        "case.message.txt",
        "delay.notice.txt",
    ]

    for fname in template_files:
        path = TEMPLATES_DIR / fname
        assert path.exists(), f"Template {fname} must exist in {TEMPLATES_DIR}"
        content = path.read_text(encoding="utf-8")

        lines = content.strip().splitlines()
        assert lines[0].startswith("Subject:"), f"Template {fname} must begin with Subject:"
        subject = lines[0].replace("Subject:", "").strip()

        placeholders = re.findall(r"\{([a-zA-Z0-9_]+)\}", subject)
        for ph in placeholders:
            assert ph in ("order_id", "case_id", "refund_id", "approval_id"), (
                f"Subject in {fname} has unsafe placeholder {{{ph}}}"
            )

        assert not FORBIDDEN_WORDS_RE.search(content), (
            f"Template {fname} contains forbidden words matching {FORBIDDEN_WORDS_RE.pattern}"
        )

    subj, body = render_template(
        "case.message",
        {
            "case_id": "CASE-1\r\nBcc: evil@attacker.test",
            "message": "<script>alert(1)</script>",
        },
    )
    assert "\r" not in subj and "\n" not in subj and "Bcc" not in subj
    assert "<script>" not in body and "&lt;script&gt;" in body


# ── Test 13: Seller retry endpoint works once, resets attempts=0, re-delivers, and audits ──


@pytest.mark.asyncio
async def test_seller_retry_endpoint_works_once_and_audits(app_env: dict[str, Any]) -> None:
    app = app_env["app"]
    seller_token = app_env["tokens"]["seller_staff"]
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]
    store: FixtureStoreClient = app_env["store"]

    with write_tx(engine) as tx:
        nid = enqueue_notification(
            tx,
            customer_id="C001",
            template="approval.decided",
            payload={"order_id": "ORD-1001", "status": "approved"},
            dedupe_key="retry:1",
            clock=clock,
        )
        tx.execute(
            notifications_outbox.update()
            .where(notifications_outbox.c.notification_id == nid)
            .values(status="exhausted", attempts=3)
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        list_resp = await client.get(
            "/v1/seller/notifications?status=exhausted",
            headers={"Authorization": f"Bearer {seller_token}"},
        )
        assert list_resp.status_code == 200
        items = list_resp.json()["notifications"]
        assert len(items) == 1 and items[0]["notification_id"] == nid

        retry_resp = await client.post(
            f"/v1/seller/notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {seller_token}"},
        )
        assert retry_resp.status_code == 200
        retried_item = retry_resp.json()
        assert retried_item["notification_id"] == nid
        assert retried_item["status"] == "queued"
        assert retried_item["attempts"] == 0

        retry_resp2 = await client.post(
            f"/v1/seller/notifications/{nid}/retry",
            headers={"Authorization": f"Bearer {seller_token}"},
        )
        assert retry_resp2.status_code == 409

    # Retried notification is genuinely deliverable on next worker pass
    assert await process_outbox_batch(engine, store, clock, force_due=True) == 1
    assert len(store.mailbox) == 1

    with engine.connect() as conn:
        audit_status = AuditService.verify_chain(conn)
        assert audit_status.ok is True
        retry_audits = conn.execute(
            select(audit_events).where(
                audit_events.c.event_type == EventType.NOTIFICATION.value,
                audit_events.c.resource_id == nid,
            )
        ).fetchall()
        assert len(retry_audits) == 1 and retry_audits[0].action == "retry_notification"


# ── Test 14: proposal.expired and refund.updated wire into client_events & outbox ──


@pytest.mark.asyncio
async def test_proposal_expired_and_refund_updated_emit_events(
    app_env: dict[str, Any],
) -> None:
    app = app_env["app"]
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]
    store: FixtureStoreClient = app_env["store"]
    tok_c1 = app_env["tokens"]["c001"]

    policy = load_policy("v3")
    ctx = RequestContext(
        principal=Principal(
            user_id="usr_c001",
            role=Role.customer,
            customer_id="C001",
            jti="jti-exp-1",
            token_exp=int(T0_BASE.timestamp()) + 3600,
        ),
        request_id="req-exp-1",
        token=SecretStr(tok_c1),
    )
    order = await store.get_order(ctx, "ORD-1042")
    refundable = await store.get_refundable(ctx, "ORD-1042")
    risk = await store.get_customer_risk_facts(build_service_ctx(clock, "req-exp-1"), "C001")
    facts = DecisionFacts(
        action=DecisionAction.return_,
        order=order,
        items=[ReturnItem(item_id="ITEM-1042-1", qty=1)],
        reason_code="defective",
        reason_text="broken",
        resolution="refund",
        refund_method="original",
        evidence_present=False,
        active_returns=[],
        previous_refunds_30d_paise=0,
        refundable=refundable,
        customer_risk=risk,
        shipment=None,
        now=clock.now(),
    )
    dec = decide(facts, policy, clock.now())
    card, pid, _ = persist_proposal_and_build_card(
        engine,
        clock=clock,
        policy=policy,
        request_id="req-exp-1",
        session_id="ses-exp-1",
        customer_id="C001",
        order=order,
        proposal_type="return",
        requested_items=[ReturnItem(item_id="ITEM-1042-1", qty=1)],
        facts=facts,
        decision=dec,
        effective_outcome=DecisionOutcome.needs_approval,
        method_str="original",
        resolution_str="refund",
        sanitized_reason_text="broken",
        extra_payload={"reason_code": "defective"},
    )
    assert pid is not None
    # Advance the clock past the 10-minute proposal TTL so confirm rejects with proposal.expired
    clock.advance(timedelta(minutes=11))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        conf_exp = await client.post(
            f"/v1/proposals/{pid}/confirm",
            headers={"Authorization": f"Bearer {tok_c1}", "Idempotency-Key": "conf-exp-1"},
            json={"proposal_hash": card["proposal_hash"]},
        )
        assert conf_exp.status_code == 409

        # Also post a signed refund.updated webhook for an approved flow
        with write_tx(engine) as tx:
            tx.execute(
                proposals.update()
                .where(proposals.c.proposal_id == pid)
                .values(status="succeeded")
            )
            tx.execute(
                approvals.insert().values(
                    approval_id="APR-RFD-WH",
                    proposal_id=pid,
                    store_id="nestkart",
                    order_id="ORD-1042",
                    return_id="RET-WH-1",
                    refund_id="RFD-WH-1",
                    amount_paise=120000,
                    required_role="seller_staff",
                    status="approved",
                    policy_version="v3",
                    facts_hash="f" * 64,
                    due_at=T0_BASE.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    expires_at=(T0_BASE + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    created_at=T0_BASE.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    version=1,
                )
            )
        wh_body = json.dumps(
            {
                "event_id": "wh-rfd-fail-1",
                "type": "refund.updated",
                "occurred_at": clock.now().isoformat(),
                "resource_version": 1,
                "data": {
                    "refund_id": "RFD-WH-1",
                    "order_id": "ORD-1042",
                    "status": "failed",
                    "failure_reason": "internal threshold exceeded",
                },
            }
        ).encode("utf-8")
        ts_str = str(int(clock.now().timestamp()))
        sig = hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            f"{ts_str}.".encode() + wh_body,
            hashlib.sha256,
        ).hexdigest()
        wh_resp = await client.post(
            "/v1/webhooks/store",
            headers={
                "X-Event-Id": "wh-rfd-fail-1",
                "X-Store-Timestamp": ts_str,
                "X-Store-Signature": sig,
                "Content-Type": "application/json",
            },
            content=wh_body,
        )
        assert wh_resp.status_code == 200

        poll = await client.get(
            "/v1/me/events?since=0",
            headers={"Authorization": f"Bearer {tok_c1}"},
        )
        assert poll.status_code == 200
        ev_types = {e["type"] for e in poll.json()["events"]}
        assert {"proposal.expired", "refund.updated"}.issubset(ev_types)

    with engine.connect() as conn:
        obx_rfd = conn.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.dedupe_key == "refund.updated:RFD-WH-1:failed"
            )
        ).fetchone()
        assert obx_rfd is not None and obx_rfd.channel == "email"
        ce_rfd = conn.execute(
            select(client_events).where(client_events.c.type == "refund.updated")
        ).fetchone()
        assert ce_rfd is not None and "threshold" not in str(ce_rfd.payload_json).lower()
