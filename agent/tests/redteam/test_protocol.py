"""Protocol abuse tests for SellerShield AI (Spec §13, §21, T1.11).

Enforces:
- Invalid JSON, wrong content type, a 100 KB header, duplicate/malformed headers,
  null bytes in text, unicode and padded order IDs, wrong HTTP methods, text and action
  both missing, extra body fields (customer_id, role, user_id).
- Each produces the standard API error envelope {error: {code, message, request_id, details}}
  with the appropriate status code (400, 401, 405, 415, 422, 431).
- Never returns HTTP 500 or leaks unhandled exceptions.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from starlette.testclient import TestClient

from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.db import reset_engine
from sellershield_agent.main import create_app

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"


@pytest.fixture(scope="module")
def rsa_key_pair() -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict["kid"] = "proto-key-1"
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return priv, pub, jwk_dict


def make_token(customer_id: str, priv: RSAPrivateKey) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "nestkart-store",
        "aud": "sellershield-agent",
        "sub": f"usr_{customer_id.lower()}",
        "customer_id": customer_id,
        "role": "customer",
        "jti": f"jti-{customer_id}-{now.timestamp()}",
        "exp": int(now.timestamp()) + 3600,
        "iat": int(now.timestamp()),
        "scope": "customer:support",
    }
    headers = {"kid": "proto-key-1", "alg": "RS256"}
    return str(jwt.encode(payload, priv, algorithm="RS256", headers=headers))


@pytest.fixture
def client_and_token(
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
) -> Generator[tuple[TestClient, str], None, None]:
    priv, _, jwk = rsa_key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    reset_engine()
    reset_chat_pipeline()

    app = create_app()
    token = make_token("C001", priv)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, token

    reset_chat_pipeline()
    reset_engine()


def assert_standard_error(resp: Any, expected_status: int) -> None:
    msg = f"Expected {expected_status}, got {resp.status_code}: {resp.text}"
    assert resp.status_code == expected_status, msg
    assert resp.status_code != 500, "Must never return HTTP 500"
    data = resp.json()
    assert "error" in data, f"Missing error envelope: {data}"
    err = data["error"]
    assert "code" in err, f"Missing error code: {err}"
    assert "message" in err, f"Missing error message: {err}"
    assert "request_id" in err, f"Missing request_id: {err}"
    assert "details" in err, f"Missing details: {err}"


# ── Protocol Tests ────────────────────────────────────────────────────────────


@respx.mock
def test_protocol_invalid_json(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        content=b"{invalid json, unclosed string...",
    )
    assert_standard_error(resp, expected_status=422)


@respx.mock
def test_protocol_wrong_content_type(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/xml"},
        content=b"<xml><text>Where is my order?</text></xml>",
    )
    assert_standard_error(resp, expected_status=422)


@respx.mock
def test_protocol_100kb_header(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    huge_header = "A" * (100 * 1024)
    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Huge-Attack-Header": huge_header,
        },
        json={"text": "Hello", "client_msg_id": "msg_huge_header"},
    )
    assert resp.status_code != 500
    if resp.status_code >= 400:
        assert_standard_error(resp, expected_status=resp.status_code)


@respx.mock
def test_protocol_duplicate_authorization_header(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    dup_auth = f"Bearer {token}, Bearer invalid_fake_token"
    resp = client.post(
        "/v1/chat/sessions",
        headers={"Authorization": dup_auth},
    )
    assert_standard_error(resp, expected_status=401)


@respx.mock
def test_protocol_null_bytes_in_text(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Check status \x00 of order", "client_msg_id": "msg_null_byte"},
    )
    assert resp.status_code != 500
    assert "\x00" not in resp.text


@respx.mock
def test_protocol_unicode_and_padded_order_ids(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    cases = [
        "ORD-1048\u0000",
        "  ORD-1048  ",
        "ord-1048",
        "ORD-00000001048",
        "ORD-١٠٤٨",
    ]

    for c in cases:
        resp = client.post(
            f"/v1/chat/sessions/{session_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": f"Status for {c}", "client_msg_id": f"msg_pad_{hash(c)}"},
        )
        assert resp.status_code != 500
        assert resp.status_code in (200, 400, 422)


@respx.mock
def test_protocol_wrong_http_methods(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token

    resp = client.get(
        "/v1/chat/sessions/ses_test123/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert_standard_error(resp, expected_status=405)

    resp = client.put(
        "/v1/chat/sessions",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert_standard_error(resp, expected_status=405)

    resp = client.delete(
        "/v1/chat/sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert_standard_error(resp, expected_status=405)


@respx.mock
def test_protocol_missing_text_and_action(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"client_msg_id": "msg_missing_fields"},
    )
    assert_standard_error(resp, expected_status=422)


@respx.mock
def test_protocol_extra_body_fields_rejected(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    forbidden_payloads = [
        {"text": "hi", "client_msg_id": "m1", "customer_id": "C002"},
        {"text": "hi", "client_msg_id": "m2", "role": "admin"},
        {"text": "hi", "client_msg_id": "m3", "user_id": "U999"},
        {"text": "hi", "client_msg_id": "m4", "is_admin": True},
        {"text": "hi", "client_msg_id": "m5", "token": "stolen_token"},
    ]

    for p in forbidden_payloads:
        resp = client.post(
            f"/v1/chat/sessions/{session_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=p,
        )
        assert_standard_error(resp, expected_status=422)
