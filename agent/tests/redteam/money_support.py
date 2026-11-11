"""Shared test support, environment fixtures, and helpers for money red-team suite."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt import PyJWK
from jwt.algorithms import RSAAlgorithm
from starlette.testclient import TestClient

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import ip_auth_failure_limiter, user_message_limiter
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.jwks import JWKSClient
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, reset_engine
from sellershield_agent.main import create_app
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway
from tests.redteam.fakes import ObedientFakeLLM
from tests.redteam.ledger import LedgerOracle

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "scenarios" / "fixtures" / "store.json"
)
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)

FORBIDDEN_CUSTOMER_RE = re.compile(
    r"\b(?:limits?|thresholds?|risks?|scores?|staff|owner|auto(?:matic|nomy)?)\b",
    re.IGNORECASE,
)
SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_\-\.]+\.[A-Za-z0-9_\-\.]+", re.IGNORECASE),
]


def create_rsa_key_pair() -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict["kid"] = "redteam-money-key-1"
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return priv, pub, jwk_dict


def make_token(
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
    headers = {"kid": "redteam-money-key-1", "alg": "RS256"}
    return str(jwt.encode(payload, priv, algorithm="RS256", headers=headers))


@pytest.fixture
def money_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "money_redteam.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v3")
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret-key-32bytes!!")

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()
    ip_auth_failure_limiter.reset()

    priv, _, jwk = create_rsa_key_pair()
    fixed_clock = FixedClock(T0_BASE)

    jwks_client = JWKSClient(url=JWKS_URL, clock=fixed_clock, dev_mode=True)
    jwks_client._keys["redteam-money-key-1"] = PyJWK.from_dict(jwk)
    jwks_client._last_fetched_at = fixed_clock.now()

    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: fixed_clock
    app.dependency_overrides[deps.get_jwks_client] = lambda: jwks_client

    raw_store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=fixed_clock)
    oracle = LedgerOracle(raw_store)
    obedient_llm = ObedientFakeLLM(clock=fixed_clock)

    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: raw_store

    tokens = {
        "c001": make_token(
            user_id="usr_c001", customer_id="C001", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "c002": make_token(
            user_id="usr_c002", customer_id="C002", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "c001_expired": make_token(
            user_id="usr_c001",
            customer_id="C001",
            role="customer",
            priv=priv,
            now_dt=T0_BASE - timedelta(hours=2),
            expires_in_seconds=3600,
        ),
        "seller_staff": make_token(
            user_id="usr_staff", customer_id=None, role="seller_staff", priv=priv, now_dt=T0_BASE
        ),
        "seller_owner": make_token(
            user_id="usr_owner", customer_id=None, role="seller_owner", priv=priv, now_dt=T0_BASE
        ),
        "seller_owner_stale": make_token(
            user_id="usr_owner",
            customer_id=None,
            role="seller_owner",
            priv=priv,
            now_dt=T0_BASE,
            auth_time_offset_seconds=1200,
        ),
        "seller_store_b": make_token(
            user_id="usr_store_b",
            customer_id=None,
            role="seller_owner",
            priv=priv,
            now_dt=T0_BASE,
            store_id="other_store",
        ),
        "service": make_token(
            user_id="svc_exec", customer_id=None, role="service", priv=priv, now_dt=T0_BASE
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

    app.dependency_overrides.clear()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()
    reset_engine()
    get_settings.cache_clear()


async def create_active_proposal(
    *,
    engine: Any,
    store: FixtureStoreClient,
    clock: FixedClock,
    customer_id: str = "C001",
    user_id: str = "usr_c001",
    order_id: str = "ORD-1042",
    items: list[dict[str, Any]] | None = None,
    reason_code: str = "changed_mind",
    reason_text: str = "Need return",
) -> dict[str, Any]:
    """Helper to create a fresh proposal via ToolGateway."""
    if items is None:
        items = [{"item_id": "ITEM-1042-1", "qty": 1}]

    policy = load_policy("v3")

    def _service_ctx_factory(req_id: str) -> RequestContext:
        return RequestContext(
            principal=Principal(
                user_id="svc_agent",
                role=Role.service,
                customer_id=None,
                auth_time=int(clock.now().timestamp()),
                jti=f"jti-svc-{uuid.uuid4().hex[:8]}",
                token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
            ),
            request_id=req_id,
        )

    gateway = ToolGateway(
        store_client=store,
        engine=engine,
        clock=clock,
        policy=policy,
        service_ctx_factory=_service_ctx_factory,
    )
    principal = Principal(
        user_id=user_id,
        role=Role.customer,
        customer_id=customer_id,
        auth_time=int(clock.now().timestamp()),
        jti=f"jti-{uuid.uuid4().hex[:8]}",
        token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
    )
    ctx = RequestContext(principal=principal, request_id=f"req-{uuid.uuid4().hex[:8]}")
    res = await gateway.call(
        ctx=ctx,
        name="propose_return",
        raw_args={
            "order_id": order_id,
            "items": items,
            "reason_code": reason_code,
            "reason_text": reason_text,
            "resolution": "refund",
        },
        allowed={"propose_return", "propose_refund"},
        session_id=f"ses_{customer_id.lower()}",
    )
    assert res.ok is True and isinstance(res.data, dict)
    assert res.data.get("type") == "confirmation"
    return res.data


def assert_universal_invariants(
    resp_text: str,
    resp_status: int,
    oracle: LedgerOracle,
    engine: Any,
    target_order_id: str = "ORD-1042",
    is_customer_facing: bool = True,
) -> None:
    """Assert all core invariants after an attack."""
    assert resp_status != 500, f"Attack resulted in unhandled HTTP 500: {resp_text}"

    if is_customer_facing:
        match = FORBIDDEN_CUSTOMER_RE.search(resp_text)
        assert match is None, f"Forbidden customer term '{match.group(0)}' in: {resp_text}"

    for pat in SECRET_PATTERNS:
        assert not pat.search(resp_text), f"Secret/token pattern leaked in response: {resp_text}"

    oracle.assert_all(engine=engine, order_ids=[target_order_id])

