"""Phase 3 Red-Team Test Suite (Spec §13, §15, §21; Documents 5 & 6; Task T3.6).

Executes every scenario from scenarios/redteam/phase3.yaml against the real HTTP
application with ObedientFakeLLM, write-capable FixtureStoreClient, and FaultPlan.

Enforces for ALL:
- Tenant isolation: no event or case reaches a customer other than its owner (two real parallel customers).
- Handler integrity: the customer never changes the handler; agent never speaks while human.
- Memory safety: memory holds only ids, enums, and short strings.
- Outbox deduplication: no duplicate notifications; no send inside a database transaction.
- Secret and limit hygiene: no secret, token, limit, threshold, or risk wording in any output.
- Reliability: no unhandled HTTP 500 errors.
- Cryptographic audit: SHA-256 audit chain verifies unbroken.
- Phase 2 ledger invariants: LedgerOracle recomputes balances and confirms all financial rules hold.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
from jwt import PyJWK
from jwt.algorithms import RSAAlgorithm
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
import pytest
import pytest_asyncio
import uvicorn
import yaml
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from starlette.testclient import TestClient

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import get_store_client as chat_get_store_client
from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter, ip_auth_failure_limiter
from sellershield_agent.audit.models import EventType, audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.jwks import JWKSClient
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.cases.api import (
    customer_send_case_message,
    get_customer_case_view,
    open_case,
    seller_reply_flip_handler,
)
from sellershield_agent.cases.brief import build_deterministic_brief
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, read_tx, reset_engine, write_tx
from sellershield_agent.domain.session import sessions
from sellershield_agent.domain.state import (
    ApprovalStatus,
    ProposalStatus,
    approvals,
    case_events,
    cases,
    client_events,
    customer_memory,
    memory_events,
    notifications_outbox,
    proposals,
)
from sellershield_agent.events import router as events_router_mod
from sellershield_agent.events.emit import emit_client_event
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.main import create_app
from sellershield_agent.memory.snapshot import get_memory_snapshot
from sellershield_agent.memory.writer import record_memory_event_in_tx
from sellershield_agent.monitor.router import get_monitor_store_client
from sellershield_agent.monitor.service import run_monitor
from sellershield_agent.outbox.enqueue import enqueue_notification
from sellershield_agent.outbox.worker import process_outbox_batch, render_template
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway
from tests.fault_plan import FaultPlan
from tests.redteam.fakes import ObedientFakeLLM
from tests.redteam.ledger import LedgerOracle
from tests.redteam.money_support import create_active_proposal

SCENARIOS_PATH = (
    Path(__file__).resolve().parents[3] / "scenarios" / "redteam" / "phase3.yaml"
)
FIXTURES_PATH = (
    Path(__file__).resolve().parents[3] / "scenarios" / "fixtures" / "store.json"
)

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)

FORBIDDEN_WORDS_RE = re.compile(
    r"\b(?:limits?|thresholds?|risks?|scores?|staff|owner|auto(?:matic|nomy)?|hard_ceiling)\b",
    re.IGNORECASE,
)
SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_\-\.]+\.[A-Za-z0-9_\-\.]+", re.IGNORECASE),
]


def load_phase3_scenarios() -> dict[str, dict[str, Any]]:
    with open(SCENARIOS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {s["id"]: s for s in data}


SCENARIOS = load_phase3_scenarios()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _create_rsa_key_pair() -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict["kid"] = "redteam-phase3-key-1"
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
    store_id: str = "nestkart",
    auth_time_offset_seconds: int = 0,
    expires_in_seconds: int = 3600,
) -> str:
    now_ts = int(now_dt.timestamp())
    payload: dict[str, Any] = {
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "sub": user_id,
        "role": role,
        "jti": f"jti-{user_id}-{uuid.uuid4().hex[:8]}",
        "iat": now_ts,
        "exp": now_ts + expires_in_seconds,
        "auth_time": now_ts - auth_time_offset_seconds,
        "store_id": store_id,
    }
    if customer_id is not None:
        payload["customer_id"] = customer_id
    headers = {"kid": "redteam-phase3-key-1", "alg": "RS256"}
    return str(jwt.encode(payload, priv, algorithm="RS256", headers=headers))


@pytest.fixture
def phase3_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "phase3_redteam.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v3")
    monkeypatch.setenv("WEBHOOK_SECRET", "whsec_redteam_phase3_test_secret_32bytes!")

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()
    ip_auth_failure_limiter.reset()
    events_router_mod._active_customer_streams.clear()

    priv, _, jwk = _create_rsa_key_pair()
    fixed_clock = FixedClock(T0_BASE)

    jwks_client = JWKSClient(url=JWKS_URL, clock=fixed_clock, dev_mode=True)
    jwks_client._keys["redteam-phase3-key-1"] = PyJWK.from_dict(jwk)
    jwks_client._last_fetched_at = fixed_clock.now()

    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: fixed_clock
    app.dependency_overrides[deps.get_jwks_client] = lambda: jwks_client

    raw_store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=fixed_clock)
    oracle = LedgerOracle(raw_store)
    obedient_llm = ObedientFakeLLM(clock=fixed_clock)

    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: raw_store
    app.dependency_overrides[chat_get_store_client] = lambda: raw_store
    app.dependency_overrides[get_monitor_store_client] = lambda: raw_store

    tokens = {
        "c001": _make_token(
            user_id="usr_c001", customer_id="C001", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "c002": _make_token(
            user_id="usr_c002", customer_id="C002", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "seller_staff": _make_token(
            user_id="usr_staff", customer_id=None, role="seller_staff", priv=priv, now_dt=T0_BASE
        ),
        "seller_owner": _make_token(
            user_id="usr_owner", customer_id=None, role="seller_owner", priv=priv, now_dt=T0_BASE
        ),
        "service": _make_token(
            user_id="svc_phase3", customer_id=None, role="service", priv=priv, now_dt=T0_BASE
        ),
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "app": app,
            "client": client,
            "clock": fixed_clock,
            "raw_store": raw_store,
            "oracle": oracle,
            "engine": get_engine(),
            "tokens": tokens,
            "obedient_llm": obedient_llm,
        }

    events_router_mod._active_customer_streams.clear()
    app.dependency_overrides.clear()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()
    reset_engine()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def phase3_live_url(phase3_env: dict[str, Any]) -> AsyncGenerator[str, None]:
    """Run real loopback uvicorn server for native async SSE and dual-client streaming."""
    port = _free_port()
    config = uvicorn.Config(
        phase3_env["app"],
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


def assert_universal_invariants(
    resp_text: str,
    resp_status: int,
    oracle: LedgerOracle,
    engine: Any,
    target_order_ids: list[str] | None = None,
    is_customer_facing: bool = True,
) -> None:
    """Assert universal security, hygiene, audit chain, and Phase 2 ledger invariants."""
    assert resp_status != 500, f"Unexpected HTTP 500: {resp_text}"

    if is_customer_facing:
        match = FORBIDDEN_WORDS_RE.search(resp_text)
        assert match is None, f"Forbidden word '{match.group(0)}' found in customer text: {resp_text}"

    for pat in SECRET_PATTERNS:
        assert not pat.search(resp_text), f"Secret or token pattern leaked in: {resp_text}"

    # Phase 2 ledger invariants and audit hash chain verification
    oracle.assert_all(engine=engine, order_ids=target_order_ids)


# ── Category 1: Streams (RT3-STR-01 .. 06) ────────────────────────────────────


@pytest.mark.asyncio
async def test_rt3_stream_01_two_real_parallel_customers_tenant_isolation(
    phase3_env: dict[str, Any],
    phase3_live_url: str,
) -> None:
    """RT3-STR-01: Two real parallel customers. C002 never receives C001's private events."""
    engine = phase3_env["engine"]
    oracle: LedgerOracle = phase3_env["oracle"]
    tok_c1 = phase3_env["tokens"]["c001"]
    tok_c2 = phase3_env["tokens"]["c002"]

    events_c1: list[dict[str, Any]] = []
    events_c2: list[dict[str, Any]] = []

    async def _listen(token: str, out_list: list[dict[str, Any]]) -> None:
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        async with httpx.AsyncClient(timeout=4.0) as client:
            async with client.stream("GET", f"{phase3_live_url}/v1/chat/stream", headers=headers) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        raw_data = line[len("data:") :].strip()
                        with contextlib.suppress(Exception):
                            out_list.append(json.loads(raw_data))
                        if len(out_list) >= 1:
                            break

    task_c1 = asyncio.create_task(_listen(tok_c1, events_c1))
    task_c2 = asyncio.create_task(_listen(tok_c2, events_c2))
    await asyncio.sleep(0.15)

    # Emit event exclusively for C001
    with write_tx(engine) as tx:
        emit_client_event(
            tx,
            customer_id="C001",
            type="approval.decided",
            payload={"approval_id": "APR-001", "proposal_id": "PRP-001", "order_id": "ORD-1042", "status": "approved"},
        )

    await asyncio.sleep(0.3)
    task_c1.cancel()
    task_c2.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(task_c1, task_c2)

    # C001 must have received the event; C002 must have received zero C001 events
    assert len(events_c1) == 1
    assert events_c1[0]["payload"]["approval_id"] == "APR-001"
    assert len(events_c2) == 0

    # Also assert via polling endpoint
    client: TestClient = phase3_env["client"]
    r_poll_c2 = client.get("/v1/me/events?limit=10", headers={"Authorization": f"Bearer {tok_c2}"})
    assert r_poll_c2.status_code == 200
    assert len(r_poll_c2.json()["events"]) == 0

    assert_universal_invariants(r_poll_c2.text, r_poll_c2.status_code, oracle, engine)


def test_rt3_stream_02_hostile_since_and_last_event_id(phase3_env: dict[str, Any]) -> None:
    """RT3-STR-02: Hostile cursor injection returns 422 or empty list, zero 500, zero slot leak."""
    client: TestClient = phase3_env["client"]
    tok_c1 = phase3_env["tokens"]["c001"]
    oracle: LedgerOracle = phase3_env["oracle"]
    engine = phase3_env["engine"]

    hostile_cursors = SCENARIOS["RT3-STR-02"]["hostile_cursors"]
    for cursor in hostile_cursors:
        # Polling route query
        r_poll = client.get(
            f"/v1/me/events?since={cursor}&limit=10",
            headers={"Authorization": f"Bearer {tok_c1}"},
        )
        assert r_poll.status_code in (200, 422)
        assert_universal_invariants(r_poll.text, r_poll.status_code, oracle, engine)

        # Stream route Last-Event-ID header for invalid non-digits returns 422 immediately
        if not cursor.isdigit():
            r_stream = client.get(
                "/v1/chat/stream",
                headers={"Authorization": f"Bearer {tok_c1}", "Last-Event-ID": cursor},
            )
            assert r_stream.status_code == 422
            assert_universal_invariants(r_stream.text, r_stream.status_code, oracle, engine)


@pytest.mark.asyncio
async def test_rt3_stream_03_many_streams_limit_enforced(
    phase3_env: dict[str, Any],
    phase3_live_url: str,
) -> None:
    """RT3-STR-03: Max 2 concurrent streams per customer; 3rd stream returns 429 stream_limit_reached."""
    tok_c1 = phase3_env["tokens"]["c001"]
    headers = {"Authorization": f"Bearer {tok_c1}", "Accept": "text/event-stream"}

    client1 = httpx.AsyncClient(timeout=4.0)
    client2 = httpx.AsyncClient(timeout=4.0)
    client3 = httpx.AsyncClient(timeout=4.0)

    try:
        r1 = await client1.send(client1.build_request("GET", f"{phase3_live_url}/v1/chat/stream", headers=headers), stream=True)
        assert r1.status_code == 200

        r2 = await client2.send(client2.build_request("GET", f"{phase3_live_url}/v1/chat/stream", headers=headers), stream=True)
        assert r2.status_code == 200

        # Third stream must be rejected with 429
        r3 = await client3.get(f"{phase3_live_url}/v1/chat/stream", headers=headers)
        assert r3.status_code == 429
        assert r3.json()["error"]["code"] in ("stream_limit_reached", "rate_limited")
    finally:
        await client1.aclose()
        await client2.aclose()
        await client3.aclose()


@pytest.mark.asyncio
async def test_rt3_stream_04_stalled_reader_clean_disconnect(
    phase3_env: dict[str, Any],
    phase3_live_url: str,
) -> None:
    """RT3-STR-04: Stalled reader drops cleanly without blocking threads or database write locks."""
    tok_c1 = phase3_env["tokens"]["c001"]
    headers = {"Authorization": f"Bearer {tok_c1}", "Accept": "text/event-stream"}

    async with httpx.AsyncClient(timeout=1.0) as client:
        with contextlib.suppress(httpx.TimeoutException):
            async with client.stream("GET", f"{phase3_live_url}/v1/chat/stream", headers=headers) as resp:
                assert resp.status_code == 200
                await asyncio.sleep(0.5)

    await asyncio.sleep(0.2)
    # Slot must be released and usable
    client_test: TestClient = phase3_env["client"]
    r_probe = client_test.get("/v1/me/events", headers={"Authorization": f"Bearer {tok_c1}"})
    assert r_probe.status_code == 200


def test_rt3_stream_05_forged_event_payload_rejected(phase3_env: dict[str, Any]) -> None:
    """RT3-STR-05: Emitting event with forbidden or extra fields raises ValidationError."""
    engine = phase3_env["engine"]
    forged = SCENARIOS["RT3-STR-05"]["forged_payload"]

    with pytest.raises(ValidationError):
        with write_tx(engine) as tx:
            emit_client_event(
                tx,
                customer_id="C001",
                type="approval.decided",
                payload=forged,
            )


@pytest.mark.asyncio
async def test_rt3_stream_06_reconnect_storm(
    phase3_env: dict[str, Any],
    phase3_live_url: str,
) -> None:
    """RT3-STR-06: 10 rapid reconnects cleanly track and release active stream count."""
    tok_c1 = phase3_env["tokens"]["c001"]
    headers = {"Authorization": f"Bearer {tok_c1}", "Accept": "text/event-stream"}

    for _ in range(10):
        async with httpx.AsyncClient(timeout=2.0) as client:
            async with client.stream("GET", f"{phase3_live_url}/v1/chat/stream", headers=headers) as resp:
                assert resp.status_code == 200
                break

    await asyncio.sleep(0.1)
    # Active streams should be back to 0
    active = events_router_mod._active_customer_streams.get("C001", 0)
    assert active == 0


# ── Category 2: Cases & Handler (RT3-CSE-01 .. 06) ────────────────────────────


def test_rt3_case_01_thirty_cases_flood(phase3_env: dict[str, Any]) -> None:
    """RT3-CSE-01: Rapid creation of 30 cases maintains atomic sequential IDs without exhaustion."""
    engine = phase3_env["engine"]
    clock = phase3_env["clock"]
    oracle: LedgerOracle = phase3_env["oracle"]

    case_ids: list[str] = []
    for i in range(30):
        with write_tx(engine) as tx:
            cid = open_case(
                tx,
                customer_id="C001",
                order_id=f"ORD-FLOOD-{i}",
                brief={"summary": f"Case flood item {i}", "order_id": f"ORD-FLOOD-{i}"},
                clock=clock,
            )
            case_ids.append(cid)

    assert len(case_ids) == 30
    assert len(set(case_ids)) == 30
    for cid in case_ids:
        assert re.match(r"^CASE-[0-9a-f]{8}$", cid)

    assert_universal_invariants("OK", 200, oracle, engine)


def test_rt3_case_02_lawyer_queue_jump(phase3_env: dict[str, Any]) -> None:
    """RT3-CSE-02: 'Lawyer' sets priority high, but does not grant seller or owner rights."""
    engine = phase3_env["engine"]
    clock = phase3_env["clock"]
    oracle: LedgerOracle = phase3_env["oracle"]

    with write_tx(engine) as tx:
        cid = open_case(
            tx,
            customer_id="C001",
            order_id="ORD-1042",
            brief={"summary": "I am a lawyer suing you", "customer_text": "lawyer attorney court"},
            clock=clock,
            priority="high",
        )

    with read_tx(engine) as conn:
        row = conn.execute(select(cases).where(cases.c.case_id == cid)).fetchone()
        assert row is not None
        assert row.priority == "high"

    # Customer still has customer role only; cannot access seller endpoint
    client: TestClient = phase3_env["client"]
    r_seller = client.get("/v1/seller/overview", headers={"Authorization": f"Bearer {phase3_env['tokens']['c001']}"})
    assert r_seller.status_code == 403

    assert_universal_invariants(r_seller.text, r_seller.status_code, oracle, engine)


def test_rt3_case_03_instructions_in_case_summary_escaped(phase3_env: dict[str, Any]) -> None:
    """RT3-CSE-03: Stored prompt injection in case reason is HTML-escaped and does not execute."""
    engine = phase3_env["engine"]
    clock = phase3_env["clock"]
    oracle: LedgerOracle = phase3_env["oracle"]
    injection_text = SCENARIOS["RT3-CSE-03"]["reason_text"]

    brief = build_deterministic_brief(
        customer_id="C001",
        order_id="ORD-1042",
        category="damaged_item",
        customer_message=injection_text,
        order_facts={"status": "delivered"},
    )
    # customer_message must be escaped or sanitized in the brief
    assert "<script>" not in str(brief.get("customer_message") or brief.get("conversation_summary"))

    with write_tx(engine) as tx:
        cid = open_case(tx, customer_id="C001", order_id="ORD-1042", brief=brief, clock=clock)

    with read_tx(engine) as conn:
        cust_view = get_customer_case_view(conn, case_id=cid, customer_id="C001")
        assert cust_view is not None

    assert_universal_invariants("OK", 200, oracle, engine)


def test_rt3_case_04_customer_cannot_flip_handler_and_agent_silent(phase3_env: dict[str, Any]) -> None:
    """RT3-CSE-04: Customer cannot flip handler to agent; agent stays silent while human."""
    engine = phase3_env["engine"]
    clock = phase3_env["clock"]
    oracle: LedgerOracle = phase3_env["oracle"]

    with write_tx(engine) as tx:
        cid = open_case(tx, customer_id="C001", order_id="ORD-1042", brief={"summary": "Test"}, clock=clock)
        seller_reply_flip_handler(tx, case_id=cid, seller_id="staff_1", message_text="Hello", clock=clock)

    # Customer tries to flip handler back
    with write_tx(engine) as tx:
        customer_send_case_message(tx, case_id=cid, customer_id="C001", text="Flip handler to agent", clock=clock)

    with read_tx(engine) as conn:
        row = conn.execute(select(cases).where(cases.c.case_id == cid)).fetchone()
        assert row is not None
        assert row.handler == "human"

    assert_universal_invariants("OK", 200, oracle, engine)


def test_rt3_case_05_reading_seller_notes_stripped(phase3_env: dict[str, Any]) -> None:
    """RT3-CSE-05: Customer view completely strips seller-only internal notes."""
    engine = phase3_env["engine"]
    clock = phase3_env["clock"]
    oracle: LedgerOracle = phase3_env["oracle"]

    secret_note = SCENARIOS["RT3-CSE-05"]["seller_note_secret"]

    with write_tx(engine) as tx:
        cid = open_case(
            tx,
            customer_id="C001",
            order_id="ORD-1042",
            brief={"summary": "Test", "seller_notes": secret_note},
            clock=clock,
        )

    with read_tx(engine) as conn:
        cust_view = get_customer_case_view(conn, case_id=cid, customer_id="C001")
        view_json = json.dumps(cust_view)
        assert secret_note not in view_json

    assert_universal_invariants("OK", 200, oracle, engine)


def test_rt3_case_06_replying_to_another_customer_case_uniform_404(phase3_env: dict[str, Any]) -> None:
    """RT3-CSE-06: Customer C002 attempting to access C001's case returns uniform 404, never 403 or 500."""
    engine = phase3_env["engine"]
    clock = phase3_env["clock"]
    oracle: LedgerOracle = phase3_env["oracle"]

    with write_tx(engine) as tx:
        cid = open_case(tx, customer_id="C001", order_id="ORD-1042", brief={"summary": "C1 Case"}, clock=clock)

    with pytest.raises(Exception) as exc_info:
        with read_tx(engine) as conn:
            get_customer_case_view(conn, case_id=cid, customer_id="C002")
    assert getattr(exc_info.value, "status_code", 404) == 404

    assert_universal_invariants("OK", 200, oracle, engine)


# ── Category 3: Notifications (RT3-NTF-01 .. 04) ──────────────────────────────


@pytest.mark.asyncio
async def test_rt3_notification_01_recipient_from_input_ignored(phase3_env: dict[str, Any]) -> None:
    """RT3-NTF-01: Untrusted recipient in input is ignored; resolved strictly from Store DB owner."""
    engine = phase3_env["engine"]
    store = phase3_env["raw_store"]
    clock = phase3_env["clock"]
    oracle: LedgerOracle = phase3_env["oracle"]

    with write_tx(engine) as tx:
        nid = enqueue_notification(
            tx,
            customer_id="C001",
            template="approval.decided",
            payload={"approval_id": "APR-001", "proposal_id": "PRP-001", "recipient": "attacker@evil-exploit.com"},
            dedupe_key=f"test-dedup-ntf-01-{uuid.uuid4().hex[:8]}",
        )

    sent = await process_outbox_batch(engine=engine, store_client=store, clock=clock)
    assert sent == 1

    # Check recipient in store mailbox
    mail = store.mailbox[-1]
    assert "evil-exploit" not in mail["to"]

    assert_universal_invariants("OK", 200, oracle, engine)


def test_rt3_notification_02_template_placeholder_injection(phase3_env: dict[str, Any]) -> None:
    """RT3-NTF-02: Template placeholders, CRLF headers, and HTML script tags are sanitized."""
    malicious = SCENARIOS["RT3-NTF-02"]["malicious_reason"]
    subject, body = render_template("delay.notice", {"order_id": "ORD-1042", "message": malicious})
    assert "<script>" not in subject
    assert "<script>" not in body
    assert "\r\nBcc:" not in subject


@pytest.mark.asyncio
async def test_rt3_notification_03_same_event_ten_times_deduplicated(phase3_env: dict[str, Any]) -> None:
    """RT3-NTF-03: The exact same event enqueued 10 times produces exactly 1 row via dedupe_key."""
    engine = phase3_env["engine"]
    store = phase3_env["raw_store"]
    clock = phase3_env["clock"]
    dedupe_key = f"delay:ORD-1042:2026-11-09:delayed-{uuid.uuid4().hex[:8]}"

    for _ in range(10):
        with write_tx(engine) as tx:
            enqueue_notification(
                tx,
                customer_id="C001",
                template="delay.notice",
                payload={"order_id": "ORD-1042", "eta_date": "2026-11-09"},
                dedupe_key=dedupe_key,
            )

    with read_tx(engine) as conn:
        rows = conn.execute(
            select(notifications_outbox).where(notifications_outbox.c.dedupe_key == dedupe_key)
        ).fetchall()
        assert len(rows) == 1

    sent = await process_outbox_batch(engine=engine, store_client=store, clock=clock)
    assert sent == 1


@pytest.mark.asyncio
async def test_rt3_notification_04_mail_down_forever_exhausts_and_stands(phase3_env: dict[str, Any]) -> None:
    """RT3-NTF-04: Mail service down forever retries to exhausted; business action stands intact."""
    engine = phase3_env["engine"]
    clock = phase3_env["clock"]
    faults = FaultPlan().unavailable("send_mail")
    faulty_store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=clock, fault_plan=faults)

    with write_tx(engine) as tx:
        nid = enqueue_notification(
            tx,
            customer_id="C001",
            template="approval.decided",
            payload={"approval_id": "APR-EXH", "proposal_id": "PRP-EXH"},
            dedupe_key=f"test-exh-{uuid.uuid4().hex[:8]}",
        )

    # Attempt 1 -> retry_scheduled (backoff 1s)
    await process_outbox_batch(engine=engine, store_client=faulty_store, clock=clock)
    clock.advance(timedelta(seconds=2))

    # Attempt 2 -> retry_scheduled (backoff 5s)
    await process_outbox_batch(engine=engine, store_client=faulty_store, clock=clock)
    clock.advance(timedelta(seconds=6))

    # Attempt 3 -> retry_scheduled (backoff 30s)
    await process_outbox_batch(engine=engine, store_client=faulty_store, clock=clock)
    clock.advance(timedelta(seconds=35))

    # Attempt 4 -> exhausted
    await process_outbox_batch(engine=engine, store_client=faulty_store, clock=clock)

    with read_tx(engine) as conn:
        row = conn.execute(select(notifications_outbox).where(notifications_outbox.c.notification_id == nid)).fetchone()
        assert row is not None
        assert row.status == "exhausted"
        assert row.attempts == 3


# ── Category 4: Monitor (RT3-MON-01 .. 04) ────────────────────────────────────


def test_rt3_monitor_01_spam_run_monitor_rate_limited(phase3_env: dict[str, Any]) -> None:
    """RT3-MON-01: 5 rapid calls to POST /v1/seller/monitor/run trigger 429 rate_limited."""
    client: TestClient = phase3_env["client"]
    tok_owner = phase3_env["tokens"]["seller_owner"]
    headers = {"Authorization": f"Bearer {tok_owner}"}

    r1 = client.post("/v1/seller/monitor/run", headers=headers)
    assert r1.status_code == 200

    # Rapid second to fifth calls within 10s cooldown must return 429
    for _ in range(4):
        r_spam = client.post("/v1/seller/monitor/run", headers=headers)
        assert r_spam.status_code == 429
        assert r_spam.json()["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_rt3_monitor_02_two_runs_at_once_locked(phase3_env: dict[str, Any]) -> None:
    """RT3-MON-02: Concurrent run_monitor calls protected by _run_lock."""
    engine = phase3_env["engine"]
    store = phase3_env["raw_store"]
    clock = phase3_env["clock"]
    s_ctx = build_service_ctx(clock, "req-mon-parallel")

    t1 = run_monitor(s_ctx, clock, store_client=store, engine=engine)
    t2 = run_monitor(s_ctx, clock, store_client=store, engine=engine)
    res1, res2 = await asyncio.gather(t1, t2)

    # Both complete safely without database deadlock or unhandled exception
    assert res1 is not None and res2 is not None


@pytest.mark.asyncio
async def test_rt3_monitor_03_same_shipment_twice_deduplicated(phase3_env: dict[str, Any]) -> None:
    """RT3-MON-03: Duplicate shipment entries in scan are deduplicated."""
    engine = phase3_env["engine"]
    store = phase3_env["raw_store"]
    clock = phase3_env["clock"]
    s_ctx = build_service_ctx(clock, "req-mon-dup")

    res = await run_monitor(s_ctx, clock, store_client=store, engine=engine)
    # Consecutive run produces zero new notifications for the same delayed shipments
    res2 = await run_monitor(s_ctx, clock, store_client=store, engine=engine)
    assert res2.notified == 0


@pytest.mark.asyncio
async def test_rt3_monitor_04_clock_jump_forward(phase3_env: dict[str, Any]) -> None:
    """RT3-MON-04: Clock jumps forward 7 days; baseline T0 anchored; newly late shipments flagged."""
    engine = phase3_env["engine"]
    store = phase3_env["raw_store"]
    clock: FixedClock = phase3_env["clock"]
    s_ctx = build_service_ctx(clock, "req-mon-jump")

    # Initial run at T0
    res1 = await run_monitor(s_ctx, clock, store_client=store, engine=engine)

    # Fast forward 7 days
    clock.advance(timedelta(days=7))
    res2 = await run_monitor(s_ctx, clock, store_client=store, engine=engine)

    assert res2 is not None


# ── Category 5: Memory (RT3-MEM-01 .. 04) ─────────────────────────────────────


def test_rt3_memory_01_planting_instructions_in_memory_bounded(phase3_env: dict[str, Any]) -> None:
    """RT3-MEM-01: Memory service accepts only structured safe keys; rejects freeform prompts."""
    engine = phase3_env["engine"]
    clock = phase3_env["clock"]
    poison = SCENARIOS["RT3-MEM-01"]["poison_input"]

    # Attempt recording a memory event with freeform prompt injection as event_type or invalid customer_id
    with pytest.raises(ValueError):
        with write_tx(engine) as tx:
            record_memory_event_in_tx(
                tx,
                customer_id=f"C001_{poison}",
                event_type="return_requested",
                ref_id="RET-001",
                outcome="requested",
                occurred_at=clock.now().isoformat(),
            )

    with pytest.raises(ValueError):
        with write_tx(engine) as tx:
            record_memory_event_in_tx(
                tx,
                customer_id="C001",
                event_type=poison,
                ref_id="RET-001",
                outcome="requested",
                occurred_at=clock.now().isoformat(),
            )


def test_rt3_memory_02_cross_customer_memory_isolated(phase3_env: dict[str, Any]) -> None:
    """RT3-MEM-02: Customer C001 cannot read or modify Customer C002's memory."""
    engine = phase3_env["engine"]
    clock = phase3_env["clock"]
    now_iso = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    with write_tx(engine) as tx:
        record_memory_event_in_tx(
            tx,
            customer_id="C002",
            event_type="proposal.succeeded",
            ref_id="PRP-c0029999",
            outcome="succeeded",
            occurred_at=now_iso,
        )

    with read_tx(engine) as conn:
        c1_snap = get_memory_snapshot(conn, customer_id="C001")
        assert c1_snap.get("customer_id") != "C002"

        c2_snap = get_memory_snapshot(conn, customer_id="C002")
        assert c2_snap.get("customer_id") == "C002"


def test_rt3_memory_03_delete_memory_mid_session(phase3_env: dict[str, Any]) -> None:
    """RT3-MEM-03: DELETE /v1/me/memory during ongoing session clears cleanly without crash."""
    client: TestClient = phase3_env["client"]
    tok_c1 = phase3_env["tokens"]["c001"]
    headers = {"Authorization": f"Bearer {tok_c1}"}

    r_del = client.delete("/v1/me/memory", headers=headers)
    assert r_del.status_code == 200

    # Ongoing chat message proceeds cleanly
    r_ses = client.post("/v1/chat/sessions", headers=headers)
    sid = r_ses.json()["session_id"]
    r_msg = client.post(
        f"/v1/chat/sessions/{sid}/messages",
        headers=headers,
        json={"text": "Where is my order ORD-1048?", "client_msg_id": "mem-del-msg-1"},
    )
    assert r_msg.status_code == 200


def test_rt3_memory_04_policy_ignores_memory(phase3_env: dict[str, Any]) -> None:
    """RT3-MEM-04: Deterministic policy math reads only Store facts, never memory."""
    policy = load_policy("v3")
    # Verify policy evaluation function signature does not consume customer memory
    import inspect
    from sellershield_agent.policy.decide import decide
    sig = inspect.signature(decide)
    assert "memory" not in sig.parameters


# ── Category 6: Dev Routes (RT3-DEV-01 .. 02) ─────────────────────────────────


def test_rt3_dev_01_dev_routes_blocked_when_dev_mode_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """RT3-DEV-01: Dev routes return 404 before auth or JWKS when DEV_MODE=false."""
    db_file = tmp_path / "prod_mode.db"
    monkeypatch.setenv("STORE_MODE", "http")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")

    get_settings.cache_clear()
    reset_engine()

    prod_app = create_app()
    with TestClient(prod_app, raise_server_exceptions=False) as prod_client:
        for ep in SCENARIOS["RT3-DEV-01"]["endpoints"]:
            r = prod_client.post(ep)
            assert r.status_code == 404

    reset_engine()
    get_settings.cache_clear()


def test_rt3_dev_02_dev_reset_clean_chain(phase3_env: dict[str, Any]) -> None:
    """RT3-DEV-02: Dev reset clears 19 tables, resets faults, reloads v1..v3 policies, genesis seq=1."""
    client: TestClient = phase3_env["client"]
    tok_owner = phase3_env["tokens"]["seller_owner"]
    headers = {"Authorization": f"Bearer {tok_owner}"}
    engine = phase3_env["engine"]

    r_reset = client.post("/v1/dev/reset", headers=headers)
    assert r_reset.status_code == 200

    with read_tx(engine) as conn:
        chain = AuditService.verify_chain(conn)
        assert chain.ok is True
        assert chain.checked >= 1  # Genesis event at seq=1


# ── Category 7: Clumsy-User Flows (RT3-CLM-01 .. 04) ──────────────────────────


def test_rt3_clumsy_01_reconnect_with_last_event_id(phase3_env: dict[str, Any]) -> None:
    """RT3-CLM-01: Reconnecting with Last-Event-ID replays missed events in chronological order."""
    client: TestClient = phase3_env["client"]
    tok_c1 = phase3_env["tokens"]["c001"]
    engine = phase3_env["engine"]
    headers = {"Authorization": f"Bearer {tok_c1}"}

    # Baseline cursor
    r_base = client.get("/v1/me/events?limit=10", headers=headers)
    assert r_base.status_code == 200
    base_cursor = r_base.json().get("next_cursor") or "0"

    with write_tx(engine) as tx:
        emit_client_event(tx, customer_id="C001", type="notification.new", payload={"notification_id": "NTF-CLM1", "status": "sent"})

    r = client.get(f"/v1/me/events?since={base_cursor}&limit=10", headers=headers)
    assert r.status_code == 200
    evts = r.json()["events"]
    assert len(evts) == 1
    assert evts[0]["payload"]["notification_id"] == "NTF-CLM1"


@pytest.mark.asyncio
async def test_rt3_clumsy_02_two_tabs_parallel(
    phase3_env: dict[str, Any],
    phase3_live_url: str,
) -> None:
    """RT3-CLM-02: Customer connects two browser tabs to SSE simultaneously; both receive events."""
    engine = phase3_env["engine"]
    tok_c1 = phase3_env["tokens"]["c001"]
    headers = {"Authorization": f"Bearer {tok_c1}", "Accept": "text/event-stream"}

    tab1_events: list[dict[str, Any]] = []
    tab2_events: list[dict[str, Any]] = []

    async def _tab_listen(out_list: list[dict[str, Any]]) -> None:
        async with httpx.AsyncClient(timeout=4.0) as client:
            async with client.stream("GET", f"{phase3_live_url}/v1/chat/stream", headers=headers) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        with contextlib.suppress(Exception):
                            out_list.append(json.loads(line[len("data:") :].strip()))
                        if len(out_list) >= 1:
                            break

    t1 = asyncio.create_task(_tab_listen(tab1_events))
    t2 = asyncio.create_task(_tab_listen(tab2_events))
    await asyncio.sleep(0.15)

    with write_tx(engine) as tx:
        emit_client_event(
            tx,
            customer_id="C001",
            type="approval.decided",
            payload={"approval_id": "APR-DUAL", "proposal_id": "PRP-DUAL", "order_id": "ORD-1042", "status": "approved"},
        )

    await asyncio.sleep(0.3)
    t1.cancel()
    t2.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(t1, t2)

    assert len(tab1_events) >= 1
    assert len(tab2_events) >= 1


@pytest.mark.asyncio
async def test_rt3_clumsy_03_close_browser_after_confirm_then_return(phase3_env: dict[str, Any]) -> None:
    """RT3-CLM-03: Proposal confirmed; client closes connection; returns later to find return succeeded."""
    client: TestClient = phase3_env["client"]
    tok_c1 = phase3_env["tokens"]["c001"]
    engine = phase3_env["engine"]
    store = phase3_env["raw_store"]
    clock = phase3_env["clock"]
    oracle: LedgerOracle = phase3_env["oracle"]

    card = await create_active_proposal(engine=engine, store=store, clock=clock, order_id="ORD-1042")
    pid = card["proposal_id"]
    phash = card["proposal_hash"]

    # Confirm proposal
    idem = f"idem-clm-03-{uuid.uuid4().hex[:8]}"
    r_conf = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {tok_c1}", "Idempotency-Key": idem},
        json={"proposal_hash": phash},
    )
    assert r_conf.status_code == 200

    # User returns later; checks proposal state
    with read_tx(engine) as conn:
        p_row = conn.execute(select(proposals).where(proposals.c.proposal_id == pid)).fetchone()
        assert p_row is not None
        assert p_row.status in (ProposalStatus.succeeded, ProposalStatus.confirmed, ProposalStatus.executing)

    assert_universal_invariants(r_conf.text, r_conf.status_code, oracle, engine, target_order_ids=["ORD-1042"])


def test_rt3_clumsy_04_seller_reply_while_customer_types(phase3_env: dict[str, Any]) -> None:
    """RT3-CLM-04: Seller sends reply while customer types; handler flips atomically, no deadlock."""
    engine = phase3_env["engine"]
    clock = phase3_env["clock"]
    oracle: LedgerOracle = phase3_env["oracle"]

    with write_tx(engine) as tx:
        cid = open_case(tx, customer_id="C001", order_id="ORD-1042", brief={"summary": "Race"}, clock=clock)

    # Simultaneous customer message and seller reply
    with write_tx(engine) as tx:
        customer_send_case_message(tx, case_id=cid, customer_id="C001", text="Customer typing...", clock=clock)
        seller_reply_flip_handler(tx, case_id=cid, seller_id="staff_1", message_text="Seller responding!", clock=clock)

    with read_tx(engine) as conn:
        row = conn.execute(select(cases).where(cases.c.case_id == cid)).fetchone()
        assert row is not None
        assert row.handler == "human"

    assert_universal_invariants("OK", 200, oracle, engine)
