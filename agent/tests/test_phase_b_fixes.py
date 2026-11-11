"""Regression tests for Phase B fixes: FIX-01, FIX-07, and seller stub shadowing audit.

STEP A tests per AGENTS.md section 8.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from starlette.testclient import TestClient

from sellershield_agent.api import chat as chat_api
from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, get_metadata, init_db, reset_engine
from sellershield_agent.main import create_app
from sellershield_agent.monitor.router import (
    get_monitor_store_client,
    reset_monitor_cooldown,
)
from sellershield_agent.store.fixtures import FixtureStoreClient

T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"


def _create_rsa_key_pair(
    kid: str = "demo-key",
) -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return priv, pub, jwk_dict


class _FakeJWKSClient:
    def __init__(self, jwk: dict[str, Any]) -> None:
        self._pyjwk = jwt.PyJWK.from_dict(jwk)

    def get_signing_key(self, kid: str) -> Any:
        return self._pyjwk

    def is_healthy(self) -> bool:
        return True


def _make_token(
    user_id: str,
    role: str,
    priv_key: RSAPrivateKey,
    kid: str = "demo-key",
    customer_id: str | None = None,
    email: str = "test@example.com",
) -> str:
    payload: dict[str, Any] = {
        "jti": f"token-{uuid.uuid4().hex[:8]}",
        "sub": user_id,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "iat": int(T0_BASE.timestamp()),
        "exp": int((T0_BASE + timedelta(hours=2)).timestamp()),
        "role": role,
        "email": email,
    }
    if customer_id is not None:
        payload["customer_id"] = customer_id
    token = jwt.encode(payload, priv_key, algorithm="RS256", headers={"kid": kid})
    return str(token)


@pytest.fixture
def phase_b_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "phase_b_test.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("STORE_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("STORE_JWKS_URL", "http://localhost:8000/.well-known/jwks.json")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    user_message_limiter.reset()
    reset_default_jwks_client()
    reset_monitor_cooldown()

    priv, _, jwk = _create_rsa_key_pair()
    fake_jwks = _FakeJWKSClient(jwk)
    fixed_clock = FixedClock(T0_BASE)
    store = FixtureStoreClient(clock=fixed_clock)

    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: fixed_clock
    app.dependency_overrides[deps.get_jwks_client] = lambda: fake_jwks
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: store
    app.dependency_overrides[chat_api.get_store_client] = lambda: store
    app.dependency_overrides[get_monitor_store_client] = lambda: store

    engine = get_engine()
    init_db(engine, metadata=get_metadata())

    tokens = {
        "seller_owner": _make_token(
            user_id="usr_owner",
            role="seller_owner",
            priv_key=priv,
            email="priya.sharma@nestkart.test",
        ),
        "seller_staff": _make_token(
            user_id="usr_staff",
            role="seller_staff",
            priv_key=priv,
            email="kiran.rao@nestkart.test",
        ),
        "customer": _make_token(
            user_id="usr_c001",
            role="customer",
            customer_id="C001",
            priv_key=priv,
            email="aarav.mehta@example.test",
        ),
    }

    client = TestClient(app, base_url="http://testserver")

    yield {
        "client": client,
        "tokens": tokens,
        "clock": fixed_clock,
        "app": app,
        "priv": priv,
    }

    reset_monitor_cooldown()
    reset_engine()
    get_settings.cache_clear()


def test_fix01_seller_cases_router_unshadowed(phase_b_env: dict[str, Any]) -> None:
    """FIX-01: GET /v1/seller/cases and case endpoints must NOT return 501 stub."""
    client: TestClient = phase_b_env["client"]
    token = phase_b_env["tokens"]["seller_owner"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. List cases must reach cases/router.py (returns 200 with cases list, NOT 501)
    resp = client.get("/v1/seller/cases", headers=headers)
    assert resp.status_code == 200, f"Expected 200 from cases_router: {resp.text}"
    assert "cases" in resp.json()

    # 2. Get non-existent case must return 404 not_found, NOT 501 not_implemented
    resp_case = client.get("/v1/seller/cases/CASE-NOTFOUND", headers=headers)
    assert resp_case.status_code == 404, f"Expected 404 for unknown case: {resp_case.text}"

    # 3. Get non-existent brief must return 404 not_found, NOT 501 not_implemented
    resp_brief = client.get("/v1/seller/cases/CASE-NOTFOUND/brief", headers=headers)
    assert resp_brief.status_code == 404, f"Expected 404 for unknown brief: {resp_brief.text}"


def test_shadowing_audit_seller_stub(phase_b_env: dict[str, Any]) -> None:
    """Verify approvals, trace, and audit are not shadowed by seller_stub."""
    client: TestClient = phase_b_env["client"]
    token = phase_b_env["tokens"]["seller_owner"]
    headers = {"Authorization": f"Bearer {token}"}

    # Approvals route
    r_app = client.get("/v1/seller/approvals", headers=headers)
    assert r_app.status_code == 200
    assert "approvals" in r_app.json()

    # Audit route
    r_audit = client.get("/v1/seller/audit", headers=headers)
    assert r_audit.status_code == 200
    assert "events" in r_audit.json()

    # Audit verify route
    r_ver = client.get("/v1/seller/audit/verify", headers=headers)
    assert r_ver.status_code == 200
    assert "verified" in r_ver.json()

    # Decision trace route (unknown request_id -> 404, not 501)
    r_trace = client.get("/v1/seller/traces/req-nonexistent", headers=headers)
    assert r_trace.status_code == 404

    # Analytics route (expected 501 stub currently)
    r_analytics = client.get("/v1/seller/analytics", headers=headers)
    assert r_analytics.status_code == 501


def test_fix07_monitor_run_rate_limit_dev_mode_vs_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIX-07: POST /v1/seller/monitor/run allows 6/min (10s) in DEV_MODE, 1/min (60s) in prod."""
    priv, _, jwk = _create_rsa_key_pair()
    fake_jwks = _FakeJWKSClient(jwk)
    fixed_clock = FixedClock(T0_BASE)
    store = FixtureStoreClient(clock=fixed_clock)

    owner_token = _make_token("usr_owner", "seller_owner", priv)
    headers = {"Authorization": f"Bearer {owner_token}"}

    # ── Test in DEV_MODE=true: 10s cooldown (6 per minute) ────────────
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    get_settings.cache_clear()
    reset_monitor_cooldown()

    app_dev = create_app()
    app_dev.dependency_overrides[deps.get_clock] = lambda: fixed_clock
    app_dev.dependency_overrides[deps.get_jwks_client] = lambda: fake_jwks
    app_dev.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: store
    app_dev.dependency_overrides[get_monitor_store_client] = lambda: store
    client_dev = TestClient(app_dev, base_url="http://testserver")

    # Run 1: OK
    r1 = client_dev.post("/v1/seller/monitor/run", headers=headers)
    assert r1.status_code == 200

    # Immediate Run 2 (< 10s): 429
    r2 = client_dev.post("/v1/seller/monitor/run", headers=headers)
    assert r2.status_code == 429
    assert r2.json()["error"]["code"] == "rate_limited"

    # Advance 11s -> succeeds in DEV_MODE
    fixed_clock.advance(timedelta(seconds=11))
    r3 = client_dev.post("/v1/seller/monitor/run", headers=headers)
    assert r3.status_code == 200

    # ── Test in DEV_MODE=false: 60s cooldown (1 per minute) ───────────
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("STORE_MODE", "http")
    get_settings.cache_clear()
    reset_monitor_cooldown()

    app_prod = create_app()
    app_prod.dependency_overrides[deps.get_clock] = lambda: fixed_clock
    app_prod.dependency_overrides[deps.get_jwks_client] = lambda: fake_jwks
    app_prod.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: store
    app_prod.dependency_overrides[get_monitor_store_client] = lambda: store
    client_prod = TestClient(app_prod, base_url="http://testserver")

    # Run 1 in Prod: OK
    r_prod1 = client_prod.post("/v1/seller/monitor/run", headers=headers)
    assert r_prod1.status_code == 200

    # Advance 11s in Prod: STILL RATE LIMITED (60s cooldown in prod)
    fixed_clock.advance(timedelta(seconds=11))
    r_prod2 = client_prod.post("/v1/seller/monitor/run", headers=headers)
    assert r_prod2.status_code == 429, f"Expected 429 in prod at 11s, got {r_prod2.status_code}"

    # Advance total 61s in Prod: Succeeds
    fixed_clock.advance(timedelta(seconds=50))
    r_prod3 = client_prod.post("/v1/seller/monitor/run", headers=headers)
    assert r_prod3.status_code == 200
