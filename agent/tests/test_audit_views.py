"""Step A tests for T2.8 seller audit log, chain verification, filters, and RBAC.

Covers:
- GET /v1/seller/audit/verify: empty chain, valid chain, tamper detection, deleted row detection.
- GET /v1/seller/audit: request_id, resource_id, event_type, from/to (UTC & IST) filters,
  keyset cursor pagination, strict OpenAPI keys, and limit/cursor/date bounds (422).
- Seller-only RBAC across all three endpoints (401 unauth, 403 customer/service even with
  ?limit=10000) and 404 on unknown trace request_id.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select, text
from starlette.testclient import TestClient

from sellershield_agent.api import chat as chat_api
from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome, audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, read_tx, reset_engine, write_tx
from sellershield_agent.main import create_app
from sellershield_agent.store.fixtures import FixtureStoreClient

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scenarios" / "fixtures" / "store.json"
)
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


def make_audit_token(
    *,
    user_id: str,
    customer_id: str | None,
    role: str,
    priv: RSAPrivateKey,
    now_dt: datetime,
    kid: str = "audit-view-key",
    store_id: str = "nestkart",
) -> str:
    """Sign an RS256 test JWT matching NestKart claims."""
    now_ts = int(now_dt.timestamp())
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "role": role,
        "iat": now_ts,
        "exp": now_ts + 3600,
        "auth_time": now_ts,
        "store_id": store_id,
        "jti": str(uuid.uuid4()),
    }
    if customer_id is not None:
        payload["customer_id"] = customer_id
    return str(jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid, "alg": "RS256"}))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "audit_views.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v3")

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(priv.public_key()))
    jwk_dict.update({"kid": "audit-view-key", "alg": "RS256", "use": "sig"})
    respx.get(JWKS_URL).respond(json={"keys": [jwk_dict]})

    clock = FixedClock(T0_BASE)
    store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=clock)
    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: clock
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: store
    monkeypatch.setattr(chat_api, "get_store_client", lambda clock=None, settings=None: store)

    tokens = {
        "c001": make_audit_token(
            user_id="usr_c001", customer_id="C001", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "c002": make_audit_token(
            user_id="usr_c002", customer_id="C002", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "seller_staff": make_audit_token(
            user_id="usr_staff", customer_id=None, role="seller_staff", priv=priv, now_dt=T0_BASE
        ),
        "seller_owner": make_audit_token(
            user_id="usr_owner", customer_id=None, role="seller_owner", priv=priv, now_dt=T0_BASE
        ),
        "service": make_audit_token(
            user_id="svc_exec", customer_id=None, role="service", priv=priv, now_dt=T0_BASE
        ),
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "client": client,
            "engine": get_engine(),
            "clock": clock,
            "store": store,
            "tokens": tokens,
        }

    app.dependency_overrides.clear()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()
    reset_engine()
    get_settings.cache_clear()


@respx.mock
def test_audit_verify_tamper_deletion_and_empty_chain(env: dict[str, Any]) -> None:
    """Verify reports clean empty & intact chains and catches tampering & row deletion."""
    client: TestClient = env["client"]
    engine = env["engine"]
    headers = {"Authorization": f"Bearer {env['tokens']['seller_owner']}"}

    r0 = client.get("/v1/seller/audit/verify", headers=headers)
    assert r0.status_code == 200, r0.text
    b0 = r0.json()
    assert set(b0.keys()) <= {"verified", "total_events", "head_hash", "broken_seq", "error"}
    assert b0["verified"] is True
    assert b0["total_events"] == 0
    assert b0["head_hash"] == "0" * 64
    assert b0.get("broken_seq") is None
    assert b0.get("error") is None

    for i in range(1, 6):
        with write_tx(engine) as tx:
            AuditService.append(
                tx,
                AuditEvent(
                    event_id=f"evt-v-{i}",
                    ts_utc=f"2026-11-08T12:0{i}:00Z",
                    request_id=f"req-v-000{i}",
                    session_id="ses-v",
                    actor_type=ActorType.SYSTEM,
                    actor_id="sys",
                    event_type=EventType.EXECUTION_STEP,
                    resource_type="proposals",
                    resource_id=f"PRP-000{i}",
                    action="step",
                    outcome=Outcome.SUCCESS,
                    details={"i": i},
                ),
            )
    with read_tx(engine) as conn:
        last_hash = conn.execute(
            text("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1")
        ).scalar()

    r1 = client.get("/v1/seller/audit/verify", headers=headers)
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["verified"] is True
    assert b1["total_events"] == 5
    assert b1["head_hash"] == last_hash
    assert b1.get("broken_seq") is None

    with write_tx(engine) as tx:
        orig_json = tx.execute(text("SELECT details_json FROM audit_events WHERE seq = 3")).scalar()
        tx.execute(
            text("UPDATE audit_events SET details_json = '{\"tampered\":true}' WHERE seq = 3")
        )
    r2 = client.get("/v1/seller/audit/verify", headers=headers)
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["verified"] is False
    assert b2["total_events"] == 5
    assert b2["broken_seq"] == 3
    assert isinstance(b2.get("error"), str) and len(b2["error"]) > 0

    with write_tx(engine) as tx:
        tx.execute(
            text("UPDATE audit_events SET details_json = :orig WHERE seq = 3"),
            {"orig": orig_json},
        )
        tx.execute(text("DELETE FROM audit_events WHERE seq = 2"))
    r3 = client.get("/v1/seller/audit/verify", headers=headers)
    assert r3.status_code == 200
    b3 = r3.json()
    assert b3["verified"] is False
    assert b3["total_events"] == 4
    assert b3["broken_seq"] in (2, 3)


@respx.mock
def test_audit_log_filters_and_pagination_bounds(env: dict[str, Any]) -> None:
    """GET /v1/seller/audit supports all filters, keyset cursor pagination, and validates bounds."""
    client: TestClient = env["client"]
    engine = env["engine"]
    headers = {"Authorization": f"Bearer {env['tokens']['seller_staff']}"}

    ev_created = getattr(EventType, "PROPOSAL_CREATED", "proposal_created")
    ev_policy = getattr(EventType, "POLICY_DECISION", "policy_decision")
    ev_confirmed = getattr(EventType, "PROPOSAL_CONFIRMED", "proposal_confirmed")
    seed = [
        ("req-filter-01", "ORD-1042", EventType.TOOL_CALL, "2026-11-08T08:00:00Z"),
        ("req-filter-01", "PRP-1111", ev_created, "2026-11-08T10:00:00Z"),
        ("req-filter-01", "PRP-1111", ev_policy, "2026-11-08T11:00:00Z"),
        ("req-filter-02", "ORD-1042", EventType.AUTH_DENIED, "2026-11-08T12:00:00Z"),
        ("req-filter-01", "PRP-1111", ev_confirmed, "2026-11-08T15:00:00Z"),
    ]
    for idx, (rid, res_id, ev_t, ts) in enumerate(seed, start=1):
        with write_tx(engine) as tx:
            AuditService.append(
                tx,
                AuditEvent(
                    event_id=f"evt-f-{idx}",
                    ts_utc=ts,
                    request_id=rid,
                    session_id="ses-f",
                    actor_type=ActorType.CUSTOMER,
                    actor_id="C001",
                    event_type=ev_t,
                    resource_type="proposals",
                    resource_id=res_id,
                    action="act",
                    outcome=Outcome.DENIED if ev_t == EventType.AUTH_DENIED else Outcome.SUCCESS,
                    details={"idx": idx},
                ),
            )

    r_req = client.get(
        "/v1/seller/audit?request_id=req-filter-01&resource_id=PRP-1111", headers=headers
    )
    assert r_req.status_code == 200, r_req.text
    d_req = r_req.json()
    assert set(d_req.keys()) <= {"events", "next_cursor"}
    assert [e["event_id"] for e in d_req["events"]] == ["evt-f-2", "evt-f-3", "evt-f-5"]

    ev0 = d_req["events"][0]
    allowed_keys = {
        "seq",
        "event_id",
        "event_type",
        "actor",
        "request_id",
        "hash",
        "prev_hash",
        "details",
        "created_at",
    }
    assert set(ev0.keys()) <= allowed_keys
    assert (allowed_keys - {"details"}).issubset(set(ev0.keys()))

    r_et = client.get("/v1/seller/audit?event_type=auth_denied", headers=headers)
    assert r_et.status_code == 200 and [e["event_id"] for e in r_et.json()["events"]] == ["evt-f-4"]

    r_rng = client.get(
        "/v1/seller/audit",
        params={"from": "2026-11-08T15:00:00+05:30", "to": "2026-11-08T13:00:00Z"},
        headers=headers,
    )
    assert r_rng.status_code == 200
    assert [e["event_id"] for e in r_rng.json()["events"]] == ["evt-f-2", "evt-f-3", "evt-f-4"]

    seen_seqs: list[int] = []
    cursor: str | None = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = client.get("/v1/seller/audit", params=params, headers=headers)
        assert page.status_code == 200
        pdata = page.json()
        seen_seqs.extend(int(e["seq"]) for e in pdata["events"])
        cursor = pdata.get("next_cursor")
        if cursor is None:
            break
    assert seen_seqs == sorted(set(seen_seqs)) and len(seen_seqs) == 5

    for bad_params in (
        {"limit": 0},
        {"limit": -1},
        {"limit": 201},
        {"limit": 10000},
        {"cursor": "not-an-int"},
        {"cursor": "-1"},
        {"from": "not-a-timestamp"},
        {"from": "2026-11-08T15:00:00Z", "to": "2026-11-08T10:00:00Z"},
    ):
        bad = client.get("/v1/seller/audit", params=bad_params, headers=headers)
        assert bad.status_code == 422, f"Expected 422 for {bad_params}, got {bad.status_code}"
        assert bad.json()["error"]["code"] == "validation_error"


@respx.mock
def test_audit_and_trace_endpoints_seller_only_and_404(env: dict[str, Any]) -> None:
    """All audit & trace endpoints reject non-sellers and return 404 on unknown trace."""
    client: TestClient = env["client"]
    engine = env["engine"]
    paths = [
        "/v1/seller/audit",
        "/v1/seller/audit?limit=10000",
        "/v1/seller/audit/verify",
        "/v1/seller/traces/req-missing-001",
    ]
    for path in paths:
        assert client.get(path).status_code == 401
        r_cust = client.get(path, headers={"Authorization": f"Bearer {env['tokens']['c001']}"})
        assert r_cust.status_code == 403 and r_cust.json()["error"]["code"] == "forbidden"
        r_svc = client.get(path, headers={"Authorization": f"Bearer {env['tokens']['service']}"})
        assert r_svc.status_code == 403

    with read_tx(engine) as conn:
        denials = conn.execute(
            select(audit_events).where(audit_events.c.event_type == EventType.AUTH_DENIED)
        ).fetchall()
    assert len(denials) >= 4

    r_404 = client.get(
        "/v1/seller/traces/req-nonexistent-99",
        headers={"Authorization": f"Bearer {env['tokens']['seller_staff']}"},
    )
    assert r_404.status_code == 404
    assert r_404.json()["error"]["code"] == "not_found"
