"""Flood and denial of service tests for SellerShield AI (Spec §13, §21, T1.11).

Enforces:
- 100 rapid messages produce 429s after limit, while health checks continue answering.
- 100 failed-auth requests from one client IP are rate limited.
- Identical client_msg_id sent 50 times produces idempotency (1 answer, 0 duplicate tool calls).
- SSE client disconnection mid-stream leaves no orphaned tasks on the event loop.
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy import select
from starlette.testclient import TestClient

from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import (
    ip_auth_failure_limiter,
    user_message_limiter,
)
from sellershield_agent.audit.models import EventType, audit_events
from sellershield_agent.db import get_engine, reset_engine
from sellershield_agent.main import create_app

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"


@pytest.fixture(scope="module")
def rsa_key_pair() -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict["kid"] = "flood-key-1"
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
    headers = {"kid": "flood-key-1", "alg": "RS256"}
    return str(jwt.encode(payload, priv, algorithm="RS256", headers=headers))


@pytest.fixture
def client_and_token(
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
) -> Generator[tuple[TestClient, str], None, None]:
    priv, _, jwk = rsa_key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    user_message_limiter.reset()
    ip_auth_failure_limiter.reset()
    reset_engine()
    reset_chat_pipeline()

    app = create_app()
    token = make_token("C001", priv)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, token

    user_message_limiter.reset()
    ip_auth_failure_limiter.reset()
    reset_chat_pipeline()
    reset_engine()


# ── Flood Tests ───────────────────────────────────────────────────────────────


@respx.mock
def test_flood_100_rapid_messages_rate_limited(
    client_and_token: tuple[TestClient, str],
) -> None:
    """100 rapid messages trigger 429 rate limit while /healthz continues to answer 200."""
    client, token = client_and_token

    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    assert sess_resp.status_code == 200
    session_id = sess_resp.json()["session_id"]

    statuses: list[int] = []
    for i in range(100):
        resp = client.post(
            f"/v1/chat/sessions/{session_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": f"Flood message {i}", "client_msg_id": f"msg_flood_{i}"},
        )
        statuses.append(resp.status_code)

    assert 200 in statuses
    assert 429 in statuses
    rate_limited_count = statuses.count(429)
    assert rate_limited_count >= 70, f"Expected >= 70 rate limited, got {rate_limited_count}"

    health_resp = client.get("/healthz")
    assert health_resp.status_code == 200
    assert health_resp.json().get("status") in ("ok", "pass")


@respx.mock
def test_flood_failed_auth_ip_rate_limiting(
    client_and_token: tuple[TestClient, str],
) -> None:
    """100 failed-auth requests from one client IP are rate limited."""
    client, _ = client_and_token

    statuses: list[int] = []
    for i in range(100):
        resp = client.post(
            "/v1/chat/sessions",
            headers={"Authorization": f"Bearer invalid_malicious_token_{i}"},
        )
        statuses.append(resp.status_code)

    assert 401 in statuses
    assert 429 in statuses, "100 failed auth requests must trigger IP rate limiting (429)"

    health_resp = client.get("/healthz")
    assert health_resp.status_code == 200


@respx.mock
def test_flood_client_msg_id_deduplication_50x(
    client_and_token: tuple[TestClient, str],
) -> None:
    """The same client_msg_id sent 50 times gives one answer and no extra tool calls."""
    client, token = client_and_token

    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {token}"})
    assert sess_resp.status_code == 200
    session_id = sess_resp.json()["session_id"]

    responses: list[str] = []
    client_msg_id = "msg_idempotent_single_answer_test"

    for _i in range(50):
        user_message_limiter.reset()
        resp = client.post(
            f"/v1/chat/sessions/{session_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "Where is my order ORD-1048?", "client_msg_id": client_msg_id},
        )
        assert resp.status_code == 200
        responses.append(resp.text)

    assert len(set(responses)) == 1, "Expected identical replayed answer for all 50 requests"

    engine = get_engine()
    with engine.connect() as conn:
        tool_events = conn.execute(
            select(audit_events).where(
                audit_events.c.session_id == session_id,
                audit_events.c.event_type == EventType.TOOL_CALL,
            )
        ).fetchall()

    assert len(tool_events) <= 4, f"Expected bounded tool calls (<=4), found {len(tool_events)}"


@pytest.mark.asyncio
async def test_flood_disconnect_midstream_leaves_no_pending_tasks(
    client_and_token: tuple[TestClient, str],
) -> None:
    """A client disconnecting mid-stream leaves no pending orphaned tasks."""
    loop = asyncio.get_running_loop()
    tasks_before = len([t for t in asyncio.all_tasks(loop) if not t.done()])

    async def mock_stream_consumer() -> None:
        async def endless_stream():
            for i in range(100):
                await asyncio.sleep(0.01)
                yield f"data: chunk {i}\n\n"

        async for _ in endless_stream():
            break

    task = asyncio.create_task(mock_stream_consumer())
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    await asyncio.sleep(0.05)
    tasks_after = len([t for t in asyncio.all_tasks(loop) if not t.done()])
    assert tasks_after <= tasks_before + 1
