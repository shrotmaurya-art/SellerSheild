"""Tests for JWT and JWKS verification, role authorization, and header validation.

All RSA keys are generated in-memory only. No private keys are ever written to disk.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import uuid4

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import APIRouter, Depends, FastAPI
from jwt.algorithms import RSAAlgorithm
from starlette.testclient import TestClient

from sellershield_agent.auth.deps import (
    require_customer,
    require_fresh_auth,
    require_owner,
    require_seller,
)
from sellershield_agent.auth.jwks import JWKSClient
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.auth.verify import verify_token
from sellershield_agent.clock import FixedClock
from sellershield_agent.errors import AppHTTPException, register_error_handlers
from sellershield_agent.main import create_app

# ── In-Memory Key Helpers ─────────────────────────────────────────


def _create_rsa_key_pair(kid: str) -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    """Generate in-memory RSA key pair and public JWK representation."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict["kid"] = kid
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return priv, pub, jwk_dict


def _sign_jwt(
    payload: dict[str, Any],
    private_key: RSAPrivateKey,
    kid: str,
    algorithm: str = "RS256",
    headers: dict[str, Any] | None = None,
) -> str:
    """Sign JWT token in memory."""
    h = {"kid": kid, "alg": algorithm}
    if headers:
        h.update(headers)
    token = jwt.encode(payload, private_key, algorithm=algorithm, headers=h)
    return str(token)


# ── Standard Fixtures ─────────────────────────────────────────────

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = ["nestkart-api", "sellershield-agent"]


@pytest.fixture
def key_pair() -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    return _create_rsa_key_pair("key-1")


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock(fixed_now: datetime) -> FixedClock:
    return FixedClock(fixed_now)


def _base_payload(
    fixed_now: datetime,
    role: str = "customer",
    sub: str = "usr_100",
    customer_id: str | None = "CUST-001",
    exp_delta: timedelta = timedelta(minutes=15),
    auth_time_delta: timedelta | None = timedelta(minutes=5),
) -> dict[str, Any]:
    now_ts = int(fixed_now.timestamp())
    payload: dict[str, Any] = {
        "sub": sub,
        "role": role,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "exp": int((fixed_now + exp_delta).timestamp()),
        "iat": now_ts,
        "jti": str(uuid4()),
    }
    if customer_id is not None:
        payload["customer_id"] = customer_id
    if auth_time_delta is not None:
        payload["auth_time"] = int((fixed_now - auth_time_delta).timestamp())
    return payload


# ── Core Unit & Verification Tests ────────────────────────────────


@respx.mock
def test_valid_customer_token(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    payload = _base_payload(fixed_now, role="customer", customer_id="CUST-001")
    token = _sign_jwt(payload, priv, kid="key-1")

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    principal = verify_token(
        token,
        jwks_client=jwks_client,
        expected_issuer=EXPECTED_ISSUER,
        clock=clock,
    )

    assert principal.user_id == "usr_100"
    assert principal.role == Role.customer
    assert principal.customer_id == "CUST-001"
    assert principal.jti == payload["jti"]
    assert principal.token_exp == payload["exp"]


@respx.mock
def test_valid_seller_token(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    payload = _base_payload(fixed_now, role="seller_staff", customer_id=None)
    payload["scopes"] = ["orders:read", "returns:approve"]
    token = _sign_jwt(payload, priv, kid="key-1")

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    principal = verify_token(
        token,
        jwks_client=jwks_client,
        expected_issuer=EXPECTED_ISSUER,
        clock=clock,
    )

    assert principal.user_id == "usr_100"
    assert principal.role == Role.seller_staff
    assert principal.customer_id is None
    assert principal.scopes == ["orders:read", "returns:approve"]


@respx.mock
def test_expired_token(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    # Expired 31 seconds ago (beyond 30s leeway)
    payload = _base_payload(fixed_now, exp_delta=-timedelta(seconds=35))
    token = _sign_jwt(payload, priv, kid="key-1")

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(token, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthenticated"


@respx.mock
def test_wrong_audience(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    payload = _base_payload(fixed_now)
    payload["aud"] = "other-service"
    token = _sign_jwt(payload, priv, kid="key-1")

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(token, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthenticated"


@respx.mock
def test_wrong_issuer(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    payload = _base_payload(fixed_now)
    payload["iss"] = "https://rogue-issuer.example.com"
    token = _sign_jwt(payload, priv, kid="key-1")

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(token, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthenticated"


@respx.mock
def test_alg_none_rejected(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    _, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    payload = _base_payload(fixed_now)
    # alg none with no signature
    token = jwt.encode(payload, key="", algorithm="none", headers={"kid": "key-1", "alg": "none"})

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(token, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthenticated"


@respx.mock
def test_algorithm_confusion_rejected(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    """Algorithm confusion attack: HS256 signed using the public key PEM as the secret."""
    import hashlib
    import hmac

    _, pub, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    pub_pem = pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    payload = _base_payload(fixed_now)

    header_bytes = jwt.utils.base64url_encode(
        json.dumps({"kid": "key-1", "alg": "HS256"}).encode("utf-8")
    )
    payload_bytes = jwt.utils.base64url_encode(json.dumps(payload).encode("utf-8"))
    signing_input = header_bytes + b"." + payload_bytes
    signature = hmac.new(pub_pem, signing_input, hashlib.sha256).digest()
    token = (signing_input + b"." + jwt.utils.base64url_encode(signature)).decode("ascii")

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(token, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthenticated"


@respx.mock
def test_key_rotation_refreshes_unknown_kid(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    """Unknown kid triggers a refresh if >= 30s since last refresh attempt."""
    priv1, _, jwk1 = key_pair
    priv2, _, jwk2 = _create_rsa_key_pair("key-2")

    # Initial response only has key-1
    route = respx.get(JWKS_URL)
    route.side_effect = [
        respx.MockResponse(200, json={"keys": [jwk1]}),
        respx.MockResponse(200, json={"keys": [jwk1, jwk2]}),
    ]

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)

    # First verify key-1 token
    t1 = _sign_jwt(_base_payload(fixed_now), priv1, kid="key-1")
    p1 = verify_token(t1, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)
    assert p1.user_id == "usr_100"

    # Advance clock past 30s rate limit window
    clock.set(fixed_now + timedelta(seconds=35))

    # Now verify key-2 token (unknown kid triggers refetch)
    t2 = _sign_jwt(_base_payload(clock.now()), priv2, kid="key-2")
    p2 = verify_token(t2, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)
    assert p2.user_id == "usr_100"


@respx.mock
def test_jwks_outage_then_recovery(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    """When store JWKS is down, existing cached keys continue working, recovering on restore."""
    priv, _, jwk = key_pair
    route = respx.get(JWKS_URL)
    route.side_effect = [
        respx.MockResponse(200, json={"keys": [jwk]}),
        # Refetch on unknown kid: 3 attempts with 500
        respx.MockResponse(500),
        respx.MockResponse(500),
        respx.MockResponse(500),
        respx.MockResponse(200, json={"keys": [jwk]}),  # Recovered
    ]

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    t = _sign_jwt(_base_payload(fixed_now), priv, kid="key-1")

    # Initial fetch succeeds
    p = verify_token(t, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)
    assert p.user_id == "usr_100"

    # Advance clock 35 seconds and send unknown kid -> refetch fails with 500
    clock.set(fixed_now + timedelta(seconds=35))
    with pytest.raises(AppHTTPException):
        verify_token(
            _sign_jwt(_base_payload(fixed_now), priv, kid="unknown-kid"),
            jwks_client=jwks_client,
            expected_issuer=EXPECTED_ISSUER,
            clock=clock,
        )

    # Existing token still verifies using cached last-good keys
    p2 = verify_token(t, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)
    assert p2.user_id == "usr_100"


@respx.mock
def test_tampered_payload_rejected(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    payload = _base_payload(fixed_now)
    token = _sign_jwt(payload, priv, kid="key-1")

    parts = token.split(".")
    # Modify payload segment
    tampered_payload = json.loads(jwt.utils.base64url_decode(parts[1]))
    tampered_payload["role"] = "seller_owner"
    tampered_bytes = json.dumps(tampered_payload).encode("utf-8")
    tampered_b64 = jwt.utils.base64url_encode(tampered_bytes).decode("ascii")

    tampered_token = f"{parts[0]}.{tampered_b64}.{parts[2]}"

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(
            tampered_token,
            jwks_client=jwks_client,
            expected_issuer=EXPECTED_ISSUER,
            clock=clock,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthenticated"


@respx.mock
def test_missing_role_rejected(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    payload = _base_payload(fixed_now)
    del payload["role"]
    token = _sign_jwt(payload, priv, kid="key-1")

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(token, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthenticated"


@respx.mock
def test_customer_without_customer_id_rejected(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    payload = _base_payload(fixed_now, role="customer", customer_id=None)
    token = _sign_jwt(payload, priv, kid="key-1")

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(token, jwks_client=jwks_client, expected_issuer=EXPECTED_ISSUER, clock=clock)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthenticated"


# ── FastAPI Dependencies & Authorization Routing ──────────────────


_fresh_auth_900 = require_fresh_auth(max_age_seconds=900)


def _build_test_app(jwks_client: JWKSClient, clock: FixedClock) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    from sellershield_agent.auth import deps

    app.dependency_overrides[deps.get_jwks_client] = lambda: jwks_client
    app.dependency_overrides[deps.get_clock] = lambda: clock

    router = APIRouter()

    @router.get("/customer-only")
    def customer_route(
        principal: Annotated[Principal, Depends(require_customer)],
    ) -> dict[str, Any]:
        return {"user_id": principal.user_id, "customer_id": principal.customer_id}

    @router.get("/seller-only")
    def seller_route(
        principal: Annotated[Principal, Depends(require_seller)],
    ) -> dict[str, Any]:
        return {"user_id": principal.user_id, "role": principal.role}

    @router.get("/owner-only")
    def owner_route(
        principal: Annotated[Principal, Depends(require_owner)],
    ) -> dict[str, Any]:
        return {"user_id": principal.user_id, "role": principal.role}

    @router.post("/sensitive-action")
    def sensitive_route(
        principal: Annotated[Principal, Depends(_fresh_auth_900)],
    ) -> dict[str, Any]:
        return {"status": "ok", "user_id": principal.user_id}

    app.include_router(router)
    return app


@respx.mock
def test_staff_token_on_owner_route_gives_403(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    app = _build_test_app(jwks_client, clock)

    # Override verify_token dependency behavior
    from sellershield_agent.auth import deps
    monkeypatch.setattr(deps, "get_jwks_client", lambda: jwks_client)
    monkeypatch.setattr(deps, "get_clock", lambda: clock)

    payload = _base_payload(fixed_now, role="seller_staff", customer_id=None)
    staff_token = _sign_jwt(payload, priv, kid="key-1")

    with TestClient(app) as test_client:
        resp = test_client.get("/owner-only", headers={"Authorization": f"Bearer {staff_token}"})
        assert resp.status_code == 403
        data = resp.json()
        assert data["error"]["code"] == "forbidden"


@respx.mock
def test_stale_auth_time_gives_step_up_required(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    app = _build_test_app(jwks_client, clock)

    from sellershield_agent.auth import deps
    monkeypatch.setattr(deps, "get_jwks_client", lambda: jwks_client)
    monkeypatch.setattr(deps, "get_clock", lambda: clock)

    # auth_time was 950 seconds ago (>900s max_age)
    stale_payload = _base_payload(
        fixed_now,
        role="seller_owner",
        customer_id=None,
        auth_time_delta=timedelta(seconds=950),
    )
    token = _sign_jwt(stale_payload, priv, kid="key-1")

    with TestClient(app) as test_client:
        resp = test_client.post("/sensitive-action", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"]["code"] == "step_up_required"
        assert data["error"]["details"]["auth_time_required_seconds"] == 900


@respx.mock
def test_authorization_header_variants(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    app = _build_test_app(jwks_client, clock)

    from sellershield_agent.auth import deps
    monkeypatch.setattr(deps, "get_jwks_client", lambda: jwks_client)
    monkeypatch.setattr(deps, "get_clock", lambda: clock)

    payload = _base_payload(fixed_now, role="customer", customer_id="CUST-1")
    valid_token = _sign_jwt(payload, priv, kid="key-1")

    with TestClient(app) as test_client:
        # Missing header
        r1 = test_client.get("/customer-only")
        assert r1.status_code == 401
        assert r1.json()["error"]["code"] == "unauthenticated"

        # Empty header
        r2 = test_client.get("/customer-only", headers={"Authorization": ""})
        assert r2.status_code == 401
        assert r2.json()["error"]["code"] == "unauthenticated"

        # Lowercase bearer works
        r3 = test_client.get("/customer-only", headers={"Authorization": f"bearer {valid_token}"})
        assert r3.status_code == 200

        # Two tokens in single header
        dup_header = f"Bearer {valid_token} {valid_token}"
        r4 = test_client.get("/customer-only", headers={"Authorization": dup_header})
        assert r4.status_code == 401
        assert r4.json()["error"]["code"] == "unauthenticated"


@respx.mock
def test_token_never_logged(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    # Expired token to trigger a loggable failure
    token = _sign_jwt(_base_payload(fixed_now, exp_delta=-timedelta(minutes=10)), priv, kid="key-1")
    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AppHTTPException):
            verify_token(
                token,
                jwks_client=jwks_client,
                expected_issuer=EXPECTED_ISSUER,
                clock=clock,
            )

    assert token not in caplog.text


# ── Break-It Adversarial Cases ────────────────────────────────────


@respx.mock
def test_1mb_token_rejected(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
) -> None:
    huge_token = "Bearer " + ("a" * 1024 * 1024 + "xyz")
    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)

    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(huge_token, jwks_client=jwks_client, clock=clock)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthenticated"


@respx.mock
def test_duplicate_authorization_header_rejected(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    app = _build_test_app(jwks_client, clock)

    from sellershield_agent.auth import deps
    monkeypatch.setattr(deps, "get_jwks_client", lambda: jwks_client)
    monkeypatch.setattr(deps, "get_clock", lambda: clock)

    token = _sign_jwt(_base_payload(fixed_now), priv, kid="key-1")

    with TestClient(app) as test_client:
        resp = test_client.get(
            "/customer-only",
            headers=[("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")],
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthenticated"


@respx.mock
def test_non_ascii_header_rejected(
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    app = _build_test_app(jwks_client, clock)

    from sellershield_agent.auth import deps
    monkeypatch.setattr(deps, "get_jwks_client", lambda: jwks_client)
    monkeypatch.setattr(deps, "get_clock", lambda: clock)

    with TestClient(app) as test_client:
        resp = test_client.get("/customer-only", headers=[(b"authorization", b"Bearer token-\xe9")])
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthenticated"


@respx.mock
def test_jwks_url_redirect_rejected(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    """JWKS client must not follow redirects (follow_redirects=False)."""
    priv, _, _ = key_pair
    # JWKS URL returns 302 redirect
    respx.get(JWKS_URL).respond(status_code=302, headers={"Location": "http://evil.com/jwks.json"})

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    token = _sign_jwt(_base_payload(fixed_now), priv, kid="key-1")

    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(token, jwks_client=jwks_client, clock=clock)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthenticated"


@respx.mock
def test_kid_injection_tricks_rejected(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    payload = _base_payload(fixed_now)

    for malicious_kid in ["../../etc/passwd", "' OR '1'='1", "key\x001", ""]:
        token = _sign_jwt(payload, priv, kid=malicious_kid)
        with pytest.raises(AppHTTPException) as exc_info:
            verify_token(token, jwks_client=jwks_client, clock=clock)
        assert exc_info.value.status_code == 401


@respx.mock
def test_role_tampering_rejected(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    payload = _base_payload(fixed_now, role="customer", customer_id="CUST-1")
    token = _sign_jwt(payload, priv, kid="key-1")

    # Manually change "customer" to "seller_owner"
    parts = token.split(".")
    payload_dict = json.loads(jwt.utils.base64url_decode(parts[1]))
    payload_dict["role"] = "seller_owner"
    tampered_bytes = json.dumps(payload_dict).encode("utf-8")
    tampered_b64 = jwt.utils.base64url_encode(tampered_bytes).decode("ascii")
    tampered_token = f"{parts[0]}.{tampered_b64}.{parts[2]}"

    with pytest.raises(AppHTTPException) as exc_info:
        verify_token(tampered_token, jwks_client=jwks_client, clock=clock)

    assert exc_info.value.status_code == 401


@respx.mock
def test_replayed_token_valid_until_expired(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    fixed_now: datetime,
) -> None:
    """Documented JWT property: token is stateless and valid on reuse until exp."""
    priv, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    payload = _base_payload(fixed_now, exp_delta=timedelta(minutes=10))
    token = _sign_jwt(payload, priv, kid="key-1")

    p1 = verify_token(token, jwks_client=jwks_client, clock=clock)
    p2 = verify_token(token, jwks_client=jwks_client, clock=clock)
    assert p1.jti == p2.jti

    # Once expired, fails
    clock.set(fixed_now + timedelta(minutes=11))
    with pytest.raises(AppHTTPException):
        verify_token(token, jwks_client=jwks_client, clock=clock)


@respx.mock
def test_readyz_reports_degraded_when_jwks_down(
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When store is down at startup, readyz reports degraded instead of crashing."""
    respx.get(JWKS_URL).respond(status_code=500)

    jwks_client = JWKSClient(url=JWKS_URL, clock=clock, dev_mode=True)
    from sellershield_agent.auth import deps
    monkeypatch.setattr(deps, "get_jwks_client", lambda: jwks_client)

    app = create_app()
    with TestClient(app) as test_client:
        resp = test_client.get("/readyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert "timestamp" in data
