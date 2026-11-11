"""Dynamic OpenAPI route sweep security tests.

Verifies:
- Every protected route returns 401 without token (except /healthz, /readyz; webhooks return 401 without signature)
- Customer tokens on seller routes return 403
- Expired and wrong-audience tokens return 401
- IDOR uniformity: exact same status (404) and error shape for another customer's ID vs unknown ID
- Request bodies over 1 MB return 413 on every POST and PUT
- Chat messages over 1000 characters are rejected with 422
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest
import respx
from starlette.testclient import TestClient

from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import ip_auth_failure_limiter, user_message_limiter
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, get_metadata, reset_engine, write_tx
from sellershield_agent.main import create_app

JWKS_URL = "http://test-store:8000/.well-known/jwks.json"


def _mint_jwt(
    priv_key: Any,
    *,
    role: str,
    sub: str,
    customer_id: str | None = None,
    store_id: str = "nestkart",
    aud: str = "sellershield-agent",
    exp_offset_seconds: int = 3600,
    kid: str = "sweep-test-key",
    now_dt: datetime | None = None,
) -> str:
    now = now_dt or datetime.now(UTC)
    now_ts = int(now.timestamp())
    exp_ts = now_ts + exp_offset_seconds
    payload: dict[str, Any] = {
        "sub": sub,
        "role": role,
        "iss": "nestkart-store",
        "aud": aud,
        "exp": exp_ts,
        "iat": now_ts - 60,
        "jti": f"jti-sweep-{uuid.uuid4().hex[:12]}",
        "store_id": store_id,
    }
    if customer_id:
        payload["customer_id"] = customer_id
    if role in ("seller_staff", "seller_owner"):
        payload["auth_time"] = now_ts - 30

    pem = priv_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def sweep_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "route_sweep.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v3")

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    ip_auth_failure_limiter.reset()
    user_message_limiter.reset()

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_jwk = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key())  # type: ignore[no-untyped-call]
    )
    pub_jwk.update({"kid": "sweep-test-key", "alg": "RS256", "use": "sig"})

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get(JWKS_URL).respond(json={"keys": [pub_jwk]})

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        c001_tok = _mint_jwt(priv, role="customer", sub="usr_c001", customer_id="C001")
        c002_tok = _mint_jwt(priv, role="customer", sub="usr_c002", customer_id="C002")
        seller_tok = _mint_jwt(priv, role="seller_staff", sub="usr_s001", store_id="nestkart")

        yield {
            "app": app,
            "client": client,
            "priv": priv,
            "c001_tok": c001_tok,
            "c002_tok": c002_tok,
            "seller_tok": seller_tok,
        }

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    ip_auth_failure_limiter.reset()
    user_message_limiter.reset()


def _format_path(path_template: str, id_sub: str = "dummy-id-01") -> str:
    path = path_template
    replacements = {
        "{session_id}": "sess_sweep_01",
        "{proposal_id}": "PRP-10420001",
        "{request_id}": "req-sweep-01",
        "{version}": "v1",
        "{id}": id_sub,
    }
    for var_name, sub_val in replacements.items():
        path = path.replace(var_name, sub_val)
    return path


def test_every_route_requires_auth_except_healthz_readyz_and_webhook_requires_sig(
    sweep_env: dict[str, Any]
) -> None:
    """Sweep every route from the real app's OpenAPI document: no token gives 401."""
    app = sweep_env["app"]
    client: TestClient = sweep_env["client"]
    openapi_doc = app.openapi()
    paths = openapi_doc.get("paths", {})

    assert len(paths) >= 20

    dummy_body = {
        "text": "test message",
        "client_msg_id": "sweep-msg-01",
        "proposal_hash": "a" * 64,
        "decision": "approve",
        "expected_version": 1,
        "status": "closed",
        "assignee": "S001",
        "reason": "standard test reason",
    }

    for path_template, path_item in paths.items():
        for method in ("get", "post", "put", "delete", "patch"):
            if method not in path_item:
                continue

            resolved_path = _format_path(path_template, id_sub="dummy-id-01")

            # Public exemptions: /healthz, /readyz
            if resolved_path in ("/healthz", "/readyz"):
                res = client.request(method, resolved_path)
                assert res.status_code in (200, 503)
                continue

            # Store webhook: gives 401 without valid HMAC signature
            if "webhook" in resolved_path:
                res = client.request(method, resolved_path, json={"event_id": "evt-1"})
                assert res.status_code == 401
                continue

            # All other routes: no token gives 401
            call_kwargs: dict[str, Any] = {}
            if method in ("post", "put", "patch"):
                call_kwargs["json"] = dummy_body

            # Reset failure limiter so subsequent sweep endpoints are not blocked by 429
            ip_auth_failure_limiter.reset()

            res = client.request(method, resolved_path, **call_kwargs)
            assert res.status_code == 401, (
                f"Route {method.upper()} {resolved_path} did not return 401 without token: "
                f"got {res.status_code} ({res.text})"
            )
            data = res.json()
            assert data.get("error", {}).get("code") in ("unauthenticated", "unauthorized")


def test_customer_token_on_seller_route_returns_403(sweep_env: dict[str, Any]) -> None:
    """A customer token on any seller route gives 403."""
    app = sweep_env["app"]
    client: TestClient = sweep_env["client"]
    c001_tok = sweep_env["c001_tok"]
    paths = app.openapi().get("paths", {})

    dummy_body = {
        "text": "test",
        "client_msg_id": "msg-1",
        "decision": "approve",
        "expected_version": 1,
        "status": "closed",
        "assignee": "S001",
        "reason": "test reason",
    }

    seller_paths_checked = 0
    for path_template, path_item in paths.items():
        if not (path_template.startswith("/v1/seller/") or path_template.startswith("/seller/")):
            continue

        for method in ("get", "post", "put", "delete", "patch"):
            if method not in path_item:
                continue

            resolved_path = _format_path(path_template, id_sub="APR-10420001")
            call_kwargs: dict[str, Any] = {
                "headers": {"Authorization": f"Bearer {c001_tok}"}
            }
            if method in ("post", "put", "patch"):
                call_kwargs["json"] = dummy_body

            # Reset failure limiter so subsequent sweep endpoints are not blocked by 429
            ip_auth_failure_limiter.reset()

            res = client.request(method, resolved_path, **call_kwargs)
            assert res.status_code == 403, (
                f"Customer token on seller route {method.upper()} {resolved_path} "
                f"did not return 403: got {res.status_code} ({res.text})"
            )
            seller_paths_checked += 1

    assert seller_paths_checked >= 10


def test_expired_and_wrong_audience_tokens_return_401(sweep_env: dict[str, Any]) -> None:
    """An expired token and a wrong-audience token give 401 across protected routes."""
    client: TestClient = sweep_env["client"]
    priv = sweep_env["priv"]

    expired_tok = _mint_jwt(
        priv,
        role="customer",
        sub="usr_c001",
        customer_id="C001",
        exp_offset_seconds=-3600,
    )
    wrong_aud_tok = _mint_jwt(
        priv,
        role="customer",
        sub="usr_c001",
        customer_id="C001",
        aud="wrong-audience-svc",
    )

    test_endpoints = [
        ("POST", "/v1/chat/sessions", {}),
        ("GET", "/v1/chat/sessions/sess_sweep_01", None),
        ("GET", "/v1/me/cases", None),
        ("POST", "/v1/me/cases/CASE-1001/messages", {"text": "hello"}),
        ("DELETE", "/v1/me/memory", None),
        ("GET", "/v1/seller/approvals", None),
        ("GET", "/v1/seller/overview", None),
    ]

    for method, path, body in test_endpoints:
        # 1. Expired token
        h_exp = {"Authorization": f"Bearer {expired_tok}"}
        r_exp = client.request(method, path, headers=h_exp, json=body if body else None)
        assert r_exp.status_code == 401, (
            f"Expired token on {method} {path} returned {r_exp.status_code}, expected 401"
        )
        assert r_exp.json().get("error", {}).get("code") == "unauthenticated"

        # 2. Wrong-audience token
        h_aud = {"Authorization": f"Bearer {wrong_aud_tok}"}
        r_aud = client.request(method, path, headers=h_aud, json=body if body else None)
        assert r_aud.status_code == 401, (
            f"Wrong-audience token on {method} {path} returned {r_aud.status_code}, expected 401"
        )
        assert r_aud.json().get("error", {}).get("code") == "unauthenticated"


def test_idor_uniform_response_for_other_customer_and_unknown_id(
    sweep_env: dict[str, Any]
) -> None:
    """Every route with an ID param gives same status and body shape for another customer ID vs unknown ID."""
    client: TestClient = sweep_env["client"]
    c001_tok = sweep_env["c001_tok"]
    c002_tok = sweep_env["c002_tok"]
    engine = get_engine()

    # 1. Seed resources owned by Customer C002
    meta = get_metadata()
    with write_tx(engine) as conn:
        conn.execute(
            meta.tables["sessions"].insert().values(
                id="sess_c002_private",
                customer_id="C002",
                channel="web",
                handler="agent",
                language="en",
                state_json="{}",
                started_at="2026-11-11T10:00:00Z",
                last_active_at="2026-11-11T10:00:00Z",
            )
        )
        conn.execute(
            meta.tables["cases"].insert().values(
                case_id="CASE-C002-99",
                customer_id="C002",
                order_id="ORD-2032",
                category="return_request",
                priority="medium",
                status="open",
                assignee=None,
                brief_json="{}",
                sla_due_at="2026-11-12T10:00:00Z",
                resolution_code=None,
                created_at="2026-11-11T10:00:00Z",
                resolved_at=None,
                version=1,
            )
        )
        conn.execute(
            meta.tables["proposals"].insert().values(
                proposal_id="PRP-C002-99",
                customer_id="C002",
                session_id="sess_c002_private",
                type="return",
                payload_json="{}",
                decision="NEEDS_APPROVAL",
                amounts_json='{"refund_paise": 1000}',
                policy_version="v3",
                facts_hash="0" * 64,
                proposal_hash="f" * 64,
                status="proposed",
                expires_at="2026-11-11T12:00:00Z",
                confirmed_at=None,
                version=1,
            )
        )
        conn.execute(
            meta.tables["approvals"].insert().values(
                approval_id="APR-C002-99",
                proposal_id="PRP-C002-99",
                order_id="ORD-2032",
                return_id=None,
                amount_paise=1000,
                required_role="seller_staff",
                status="pending",
                policy_version="v3",
                facts_hash="0" * 64,
                due_at="2026-11-12T10:00:00Z",
                expires_at="2026-11-12T10:00:00Z",
                decided_by=None,
                decided_at=None,
                decision_reason=None,
                version=1,
            )
        )

    # 2. Check each customer route with an ID parameter
    idor_specs = [
        ("GET", "/v1/chat/sessions/{id}", "sess_c002_private", "sess_unknown_nonexistent", None),
        (
            "POST",
            "/v1/chat/sessions/{id}/messages",
            "sess_c002_private",
            "sess_unknown_nonexistent",
            {"text": "hi", "client_msg_id": "idor_msg_1"},
        ),
        (
            "POST",
            "/v1/proposals/{id}/confirm",
            "PRP-C002-99",
            "PRP-99999999",
            {"proposal_hash": "f" * 64},
        ),
        (
            "POST",
            "/v1/proposals/{id}/cancel",
            "PRP-C002-99",
            "PRP-99999999",
            {},
        ),
        (
            "POST",
            "/v1/me/approvals/{id}/withdraw",
            "PRP-C002-99",
            "PRP-99999999",
            {},
        ),
        ("GET", "/v1/me/cases/{id}", "CASE-C002-99", "CASE-UNKNOWN-99", None),
        (
            "POST",
            "/v1/me/cases/{id}/messages",
            "CASE-C002-99",
            "CASE-UNKNOWN-99",
            {"text": "message for unowned case"},
        ),
    ]

    headers = {
        "Authorization": f"Bearer {c001_tok}",
        "Idempotency-Key": "idem-idor-check-001",
    }

    for method, path_tmpl, c002_id, unknown_id, body in idor_specs:
        path_other = path_tmpl.replace("{id}", c002_id)
        path_unknown = path_tmpl.replace("{id}", unknown_id)

        resp_other = client.request(method, path_other, headers=headers, json=body)
        resp_unknown = client.request(method, path_unknown, headers=headers, json=body)

        assert resp_other.status_code == 404, (
            f"Expected 404 for other customer ID on {method} {path_other}, got {resp_other.status_code}"
        )
        assert resp_unknown.status_code == 404, (
            f"Expected 404 for unknown ID on {method} {path_unknown}, got {resp_unknown.status_code}"
        )

        body_other = resp_other.json().get("error", {})
        body_unknown = resp_unknown.json().get("error", {})

        assert body_other.get("code") == "not_found"
        assert body_unknown.get("code") == "not_found"
        assert set(body_other.keys()) == set(body_unknown.keys())
        assert body_other.get("message") == body_unknown.get("message")
        assert body_other.get("details") == body_unknown.get("details")


def test_body_over_1mb_returns_413_on_every_post_and_put(sweep_env: dict[str, Any]) -> None:
    """A body over 1 MB gives 413 on every POST and PUT path in OpenAPI."""
    app = sweep_env["app"]
    client: TestClient = sweep_env["client"]
    c001_tok = sweep_env["c001_tok"]
    paths = app.openapi().get("paths", {})

    oversized_bytes = b"X" * (1024 * 1024 + 1024)  # 1 MB + 1 KB

    checked_routes = 0
    for path_template, path_item in paths.items():
        for method in ("post", "put"):
            if method not in path_item:
                continue

            resolved_path = _format_path(path_template, id_sub="dummy-id-01")
            res = client.request(
                method,
                resolved_path,
                content=oversized_bytes,
                headers={
                    "Authorization": f"Bearer {c001_tok}",
                    "Content-Type": "application/json",
                },
            )
            assert res.status_code == 413, (
                f"Oversized body on {method.upper()} {resolved_path} did not return 413: "
                f"got {res.status_code}"
            )
            data = res.json()
            assert data.get("error", {}).get("code") == "limit_exceeded"
            checked_routes += 1

    assert checked_routes >= 15


def test_chat_message_length_boundary_rejected_over_1000(sweep_env: dict[str, Any]) -> None:
    """Chat message text up to 1000 characters is accepted; 1001 characters is rejected."""
    client: TestClient = sweep_env["client"]
    c001_tok = sweep_env["c001_tok"]
    headers = {"Authorization": f"Bearer {c001_tok}"}

    # Start session
    s_resp = client.post("/v1/chat/sessions", headers=headers)
    assert s_resp.status_code == 200
    ses_id = s_resp.json()["session_id"]

    # 1. Message with exactly 1000 chars -> Accepted (not 422)
    valid_text = "A" * 1000
    r_valid = client.post(
        f"/v1/chat/sessions/{ses_id}/messages",
        headers=headers,
        json={"text": valid_text, "client_msg_id": "msg-boundary-1000"},
    )
    assert r_valid.status_code != 422, f"1000-char message unexpectedly rejected: {r_valid.text}"

    # 2. Message with 1001 chars -> Rejected with 422
    invalid_text = "A" * 1001
    r_invalid = client.post(
        f"/v1/chat/sessions/{ses_id}/messages",
        headers=headers,
        json={"text": invalid_text, "client_msg_id": "msg-boundary-1001"},
    )
    assert r_invalid.status_code == 422, (
        f"1001-char message should be rejected with 422: got {r_invalid.status_code}"
    )
    assert r_invalid.json().get("error", {}).get("code") == "validation_error"
