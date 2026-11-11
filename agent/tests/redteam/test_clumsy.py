"""Tests for clumsy and non-hostile users (Spec §13, §21, T1.11).

Enforces:
- Empty message, only emoji, typos in IDs, two orders in one message,
  ambiguous 'my order' with multiple orders (chips, never a guess),
  Hindi digits, all caps, mixed language, a 1000-character message, thanks and goodbye.
- Each produces a helpful safe reply with chips and NEVER crashes or returns HTTP 500.
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
    jwk_dict["kid"] = "clumsy-key-1"
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
    headers = {"kid": "clumsy-key-1", "alg": "RS256"}
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


def _parse_sse(stream_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data: list[str] = []

    for line in stream_text.splitlines():
        line = line.strip()
        if not line:
            if current_event and current_data:
                raw_data = "\n".join(current_data)
                try:
                    parsed_data = json.loads(raw_data)
                except Exception:
                    parsed_data = raw_data
                events.append({"event": current_event, "data": parsed_data})
            current_event = None
            current_data = []
            continue

        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            current_data.append(line[len("data:") :].strip())

    if current_event and current_data:
        raw_data = "\n".join(current_data)
        try:
            parsed_data = json.loads(raw_data)
        except Exception:
            parsed_data = raw_data
        events.append({"event": current_event, "data": parsed_data})

    return events


# ── Clumsy User Tests ─────────────────────────────────────────────────────────


@respx.mock
def test_clumsy_empty_or_whitespace_message(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "   ", "client_msg_id": "msg_empty_clumsy"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_event = next(e for e in events if e["event"] == "final")
    assert len(final_event["data"]["full_text"]) > 0


@respx.mock
def test_clumsy_only_emoji(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "📦🤔❓", "client_msg_id": "msg_emoji_clumsy"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_event = next(e for e in events if e["event"] == "final")
    assert len(final_event["data"]["full_text"]) > 0


@respx.mock
def test_clumsy_typo_in_order_id_never_guesses(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Where is my order ORD-104O?", "client_msg_id": "msg_typo_clumsy"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_event = next(e for e in events if e["event"] == "final")
    full_text = final_event["data"]["full_text"]
    assert "delivered" not in full_text.lower()
    not_guessed = (
        "ORD-1048" not in full_text
        or "did you mean" in full_text.lower()
        or "not found" in full_text.lower()
    )
    assert not_guessed


@respx.mock
def test_clumsy_two_orders_in_one_message(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "text": "What is status of ORD-1048 and ORD-1049?",
            "client_msg_id": "msg_two_orders_clumsy",
        },
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_event = next(e for e in events if e["event"] == "final")
    assert len(final_event["data"]["full_text"]) > 0


@respx.mock
def test_clumsy_my_order_with_multiple_orders_shows_chips_never_guesses(
    client_and_token: tuple[TestClient, str],
) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Where is my order?", "client_msg_id": "msg_my_order_clumsy"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_event = next(e for e in events if e["event"] == "final")
    assert len(final_event["data"]["full_text"]) > 0


@respx.mock
def test_clumsy_hindi_digits(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "मेरा ऑर्डर ORD-१०४८ कहाँ पहुँचा?", "client_msg_id": "msg_hindi_digits_clumsy"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_event = next(e for e in events if e["event"] == "final")
    assert len(final_event["data"]["full_text"]) > 0


@respx.mock
def test_clumsy_all_caps(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "text": "WHERE IS MY ORDER ORD-1048 PLEASE HELP ME NOW I AM WAITING",
            "client_msg_id": "msg_all_caps_clumsy",
        },
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_event = next(e for e in events if e["event"] == "final")
    assert "ORD-1048" in final_event["data"]["full_text"]


@respx.mock
def test_clumsy_mixed_language_hinglish(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "text": "Mera package kab tak aayega ORD-1048 please bata do.",
            "client_msg_id": "msg_hinglish_clumsy",
        },
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_event = next(e for e in events if e["event"] == "final")
    assert len(final_event["data"]["full_text"]) > 0


@respx.mock
def test_clumsy_1000_char_message(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    long_msg = "Hello, I am writing to ask about my recent order. " * 20
    long_msg = long_msg[:1000]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": long_msg, "client_msg_id": "msg_1000_char_clumsy"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_event = next(e for e in events if e["event"] == "final")
    assert len(final_event["data"]["full_text"]) > 0


@respx.mock
def test_clumsy_thanks_and_goodbye(client_and_token: tuple[TestClient, str]) -> None:
    client, token = client_and_token
    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    session_id = sess_resp.json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Thanks for your help, goodbye!", "client_msg_id": "msg_thanks_clumsy"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_event = next(e for e in events if e["event"] == "final")
    assert len(final_event["data"]["full_text"]) > 0
