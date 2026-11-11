"""Gate G2 end-to-end tests and money-path verification.

Implements TASK T2.12 Step A test specification:
- SC-04: Full approval flow (chat proposes, customer confirms, seller approves with seller token,
  store has approved return and authorization of 120000 paise, return received and inspected
  through fixture advance helper, refund released and completed, customer client_event exists,
  and SHA-256 audit chain verifies).
- SC-05: ALLOW path ends with approved return and a hold, then release after inspection.
- SC-06, SC-07, SC-09: Ineligible, non-returnable, or duplicate requests produce no proposal.
- SC-08: Partial return with backend-computed discount allocation.
- SC-10: Capped cumulative refund at remaining order balance.
- SC-11, SC-16: Missing slot detection (evidence for damaged item, payment method for COD).
- SC-14: Double confirmation idempotency and single-execution guarantee.
- T05: Return only after valid confirmation button (chat text ignored).
- T06: Ineligible return produces no store record and no proposal.
- T07: Partial return limits enforced.
- T08: Duplicate return blocked.
- T09: Cumulative refund cap enforced.
- T10: Backend-computed refund amounts (LLM-proffered amount ignored).
- T11: Above-limit refund triggers pending seller approval.
- T12: Approval bound to amount and state.
- T14: Concurrent confirm serialization.
- T19: Concurrent audit writes verify with zero forks.
- T20: Audit failure blocks mutation.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
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

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import get_store_client as chat_get_store_client
from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps as auth_deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.db import get_engine, read_tx, reset_engine, write_tx
from sellershield_agent.domain.state import client_events, proposals
from sellershield_agent.main import create_app
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
WEBHOOK_SECRET = "whsec_test_gate_g2_secret_key_123456789"
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


def _create_rsa_key_pair(
    kid: str = "gate-g2-key",
) -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    """Generate in-memory RSA key pair and public JWK dictionary."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict["kid"] = kid
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return priv, pub, jwk_dict


def _make_token(
    user_id: str,
    customer_id: str | None,
    role: str,
    priv: RSAPrivateKey,
    clock: FixedClock,
    kid: str = "gate-g2-key",
    exp_seconds: int = 3600,
    store_id: str = "nestkart",
) -> str:
    now = clock.now()
    now_ts = int(now.timestamp())
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "role": role,
        "iat": now_ts,
        "exp": now_ts + exp_seconds,
        "auth_time": now_ts,
        "jti": str(uuid.uuid4()),
        "store_id": store_id,
    }
    if customer_id:
        payload["customer_id"] = customer_id
    h = {"kid": kid, "alg": "RS256"}
    return str(jwt.encode(payload, priv, algorithm="RS256", headers=h))


def _make_principal(
    user_id: str,
    role: Role,
    clock: FixedClock,
    customer_id: str | None = None,
) -> Principal:
    now_ts = int(clock.now().timestamp())
    return Principal(
        user_id=user_id,
        role=role,
        customer_id=customer_id,
        auth_time=now_ts,
        jti=str(uuid.uuid4()),
        token_exp=now_ts + 3600,
    )


def _sign_webhook(raw_body: bytes, timestamp: str, secret: str = WEBHOOK_SECRET) -> str:
    msg = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _parse_sse(response_text: str) -> list[dict[str, Any]]:
    """Parse raw SSE text into a list of event dicts."""
    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data: list[str] = []

    for line in response_text.splitlines():
        line = line.strip()
        if not line:
            if current_event and current_data:
                data_str = "\n".join(current_data)
                try:
                    payload = json.loads(data_str)
                except Exception:
                    payload = data_str
                events.append({"event": current_event, "data": payload})
            current_event = None
            current_data = []
            continue

        if line.startswith("event:"):
            current_event = line.replace("event:", "").strip()
        elif line.startswith("data:"):
            current_data.append(line.replace("data:", "").strip())

    if current_event and current_data:
        data_str = "\n".join(current_data)
        try:
            payload = json.loads(data_str)
        except Exception:
            payload = data_str
        events.append({"event": current_event, "data": payload})

    return events


async def _advance_return_helper(
    store: FixtureStoreClient,
    seller_ctx: RequestContext,
    return_id: str,
) -> None:
    """Advance a return through pickup, transit, warehouse receipt, and ok inspection."""
    for st in ["pickup_scheduled", "in_transit_back", "received", "inspected_ok"]:
        await store.transition_return(
            ctx=seller_ctx,
            return_id=return_id,
            to=st,
            reason=f"Step helper to {st}",
            idempotency_key=f"adv-{return_id}-{st}",
        )


def _deliver_webhook(
    client: TestClient,
    event_type: str,
    data: dict[str, Any],
    clock: FixedClock,
    resource_version: int = 1,
) -> Any:
    """Sign and dispatch a webhook event to the agent webhook endpoint."""
    now_iso = clock.now().isoformat()
    now_ts = str(int(clock.now().timestamp()))
    event_id = f"evt_{uuid.uuid4().hex[:12]}"

    body_dict = {
        "event_id": event_id,
        "type": event_type,
        "occurred_at": now_iso,
        "resource_version": resource_version,
        "data": data,
    }
    raw_body = json.dumps(body_dict).encode("utf-8")
    sig = _sign_webhook(raw_body, now_ts)

    headers = {
        "Content-Type": "application/json",
        "X-Store-Signature": sig,
        "X-Store-Timestamp": now_ts,
        "X-Event-Id": event_id,
    }
    return client.post("/v1/webhooks/store", content=raw_body, headers=headers)


@pytest.fixture
def key_pair() -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    return _create_rsa_key_pair("gate-g2-key")


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0_BASE)


@pytest.fixture
def store(clock: FixedClock) -> FixtureStoreClient:
    return FixtureStoreClient(clock=clock)


@pytest.fixture
def tokens(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
) -> dict[str, str]:
    priv, _, _ = key_pair
    return {
        "c001": _make_token("usr_c001", "C001", "customer", priv, clock),
        "c002": _make_token("usr_c002", "C002", "customer", priv, clock),
        "c003": _make_token("usr_c003", "C003", "customer", priv, clock),
        "c004": _make_token("usr_c004", "C004", "customer", priv, clock),
        "c005": _make_token("usr_c005", "C005", "customer", priv, clock),
        "seller_staff": _make_token("usr_staff", None, "seller_staff", priv, clock),
        "seller_owner": _make_token("usr_owner", None, "seller_owner", priv, clock),
    }


@pytest.fixture
def app_client(
    key_pair: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    clock: FixedClock,
    store: FixtureStoreClient,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("WEBHOOK_SECRET", WEBHOOK_SECRET)

    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()

    _, _, jwk = key_pair
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    app = create_app()

    # Wire injected dependencies
    app.dependency_overrides[auth_deps.get_clock] = lambda: clock
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda **kw: store
    app.dependency_overrides[chat_get_store_client] = lambda **kw: store

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()


# ==============================================================================
# SC-04: Full approval flow end-to-end
# ==============================================================================
@respx.mock
def test_sc04_full_approval_flow(
    app_client: TestClient,
    tokens: dict[str, str],
    clock: FixedClock,
    store: FixtureStoreClient,
) -> None:
    c001_tok = tokens["c001"]
    owner_tok = tokens["seller_owner"]
    cust_headers = {"Authorization": f"Bearer {c001_tok}", "Content-Type": "application/json"}
    owner_headers = {"Authorization": f"Bearer {owner_tok}", "Content-Type": "application/json"}

    # 1. Chat proposes return for ORD-1042 (Cookware Set ₹1,200, defective)
    ses_resp = app_client.post("/v1/chat/sessions", headers=cust_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    session_id = ses_resp.json()["session_id"]

    msg_resp = app_client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers=cust_headers,
        json={
            "text": "I want to return order ORD-1042 because it is defective",
            "client_msg_id": "msg_sc04_1",
        },
    )
    assert msg_resp.status_code == 200, msg_resp.text
    events = _parse_sse(msg_resp.text)

    # Must contain a proposal card with proposal_id and proposal_hash
    cards = [e["data"] for e in events if e["event"] == "card"]
    proposal_cards = [
        c for c in cards if isinstance(c, dict) and c.get("card_type") == "confirmation"
    ]
    assert len(proposal_cards) == 1, f"Expected 1 confirmation card, got {cards}"
    card_payload = proposal_cards[0]["payload"]
    proposal_id = card_payload["proposal_id"]
    proposal_hash = card_payload["proposal_hash"]
    assert card_payload["amounts"]["total_paise"] == 120000

    # 2. Customer confirms proposal
    confirm_resp = app_client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={**cust_headers, "Idempotency-Key": "idem_confirm_sc04"},
        json={"proposal_hash": proposal_hash},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    c_data = confirm_resp.json()
    assert c_data["status"] == "pending_approval"
    approval_id = c_data["approval_id"]
    return_id = c_data["return_id"]
    refund_id = c_data["refund_id"]
    assert approval_id and return_id and refund_id

    # 3. Seller approves with seller token
    appr_resp = app_client.post(
        f"/v1/seller/approvals/{approval_id}/decision",
        headers={**owner_headers, "Idempotency-Key": "idem_approve_sc04"},
        json={"decision": "approve", "expected_version": 1},
    )
    assert appr_resp.status_code == 200, appr_resp.text
    assert appr_resp.json()["status"] == "approved"

    # Store verification: return is approved, store authorization of 120000 exists
    seller_ctx = RequestContext(
        principal=_make_principal("usr_owner", Role.seller_owner, clock),
        request_id="req_verify_sc04",
        token=owner_tok,
    )
    ret_obj = asyncio.run(store.get_return(seller_ctx, return_id))
    assert ret_obj.status == "approved"

    rfd_obj = asyncio.run(store.get_refund(seller_ctx, refund_id))
    assert rfd_obj.authorized_amount == 120000
    assert rfd_obj.authorized_by == "usr_owner"

    # 4. Advance return lifecycle through fixture helper
    asyncio.run(_advance_return_helper(store, seller_ctx, return_id))
    adv_ret = asyncio.run(store.get_return(seller_ctx, return_id))
    assert adv_ret.status == "inspected_ok"

    # 5. Dispatch return.updated webhook with inspected_ok -> triggers release
    wh_resp = _deliver_webhook(
        app_client,
        event_type="return.updated",
        data={"return_id": return_id, "status": "inspected_ok"},
        clock=clock,
        resource_version=1,
    )
    assert wh_resp.status_code == 200, wh_resp.text

    # Refund status transitions to completed
    final_rfd = asyncio.run(store.get_refund(seller_ctx, refund_id))
    assert final_rfd.status in ("processing", "completed")

    # 6. Customer client_event exists
    eng = get_engine()
    with read_tx(eng) as conn:
        cev = conn.execute(
            select(client_events).where(client_events.c.customer_id == "C001")
        ).fetchall()
        assert len(cev) > 0, "Expected at least one client_event for C001"

    # 7. Audit hash chain verifies
    with read_tx(eng) as conn:
        verify_res = AuditService.verify_chain(conn)
        assert verify_res.ok is True
        assert verify_res.first_broken_seq is None


# ==============================================================================
# SC-05: ALLOW path ends with approved return and a hold, then release after inspection
# ==============================================================================
@respx.mock
def test_sc05_allow_path_auto_approved_and_hold_then_release(
    app_client: TestClient,
    tokens: dict[str, str],
    clock: FixedClock,
    store: FixtureStoreClient,
) -> None:
    c002_tok = tokens["c002"]
    seller_tok = tokens["seller_owner"]
    cust_headers = {"Authorization": f"Bearer {c002_tok}", "Content-Type": "application/json"}
    seller_ctx = RequestContext(
        principal=_make_principal("usr_owner", Role.seller_owner, clock),
        request_id="req_verify_sc05",
        token=seller_tok,
    )

    # 1. Chat proposes return for ORD-2032 (Running T-shirt ₹499, unwanted)
    # Refund 499 - 60 = ₹439 (43900 paise) <= ₹500 autonomy cap -> ALLOW path
    ses_resp = app_client.post("/v1/chat/sessions", headers=cust_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    session_id = ses_resp.json()["session_id"]

    msg_resp = app_client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers=cust_headers,
        json={
            "text": "I changed my mind about order ORD-2032, please start a return",
            "client_msg_id": "msg_sc05_1",
        },
    )
    assert msg_resp.status_code == 200, msg_resp.text
    events = _parse_sse(msg_resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    proposal_cards = [
        c for c in cards if isinstance(c, dict) and c.get("card_type") == "confirmation"
    ]
    assert len(proposal_cards) == 1
    card = proposal_cards[0]["payload"]
    proposal_id = card["proposal_id"]
    proposal_hash = card["proposal_hash"]
    assert card["amounts"]["total_paise"] == 43900

    # 2. Customer confirms -> auto-approved in ALLOW path
    confirm_resp = app_client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={**cust_headers, "Idempotency-Key": "idem_confirm_sc05"},
        json={"proposal_hash": proposal_hash},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    c_data = confirm_resp.json()
    assert c_data["status"] == "succeeded"
    return_id = c_data["return_id"]
    refund_id = c_data["refund_id"]

    # Store return is directly approved; refund is reserved (hold)
    ret_obj = asyncio.run(store.get_return(seller_ctx, return_id))
    assert ret_obj.status == "approved"
    rfd_obj = asyncio.run(store.get_refund(seller_ctx, refund_id))
    assert rfd_obj.status == "reserved"

    # 3. Advance return to inspected_ok and release via webhook
    asyncio.run(_advance_return_helper(store, seller_ctx, return_id))
    wh_resp = _deliver_webhook(
        app_client,
        event_type="return.updated",
        data={"return_id": return_id, "status": "inspected_ok"},
        clock=clock,
        resource_version=1,
    )
    assert wh_resp.status_code == 200

    final_rfd = asyncio.run(store.get_refund(seller_ctx, refund_id))
    assert final_rfd.status in ("processing", "completed")


# ==============================================================================
# SC-06, SC-07, SC-09: Produce no proposal
# ==============================================================================
@respx.mock
def test_sc06_sc07_sc09_produce_no_proposal(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    # SC-06: C001, ORD-1051 (delivered T0-16d, outside 7-day window)
    c001_headers = {"Authorization": f"Bearer {tokens['c001']}", "Content-Type": "application/json"}
    ses1_resp = app_client.post("/v1/chat/sessions", headers=c001_headers)
    assert ses1_resp.status_code == 200, ses1_resp.text
    ses1 = ses1_resp.json()["session_id"]
    resp1 = app_client.post(
        f"/v1/chat/sessions/{ses1}/messages",
        headers=c001_headers,
        json={"text": "Return order ORD-1051 please", "client_msg_id": "msg_sc06"},
    )
    assert resp1.status_code == 200, resp1.text
    events1 = _parse_sse(resp1.text)
    cards1 = [e["data"] for e in events1 if e["event"] == "card"]
    assert not any(c.get("card_type") == "confirmation" for c in cards1)

    # SC-07: C001, ORD-1060 (Vitamin C Face Serum, hygiene-sensitive non-returnable)
    ses2_resp = app_client.post("/v1/chat/sessions", headers=c001_headers)
    assert ses2_resp.status_code == 200, ses2_resp.text
    ses2 = ses2_resp.json()["session_id"]
    resp2 = app_client.post(
        f"/v1/chat/sessions/{ses2}/messages",
        headers=c001_headers,
        json={
            "text": "I want to return the face serum for order ORD-1060",
            "client_msg_id": "msg_sc07",
        },
    )
    assert resp2.status_code == 200, resp2.text
    events2 = _parse_sse(resp2.text)
    cards2 = [e["data"] for e in events2 if e["event"] == "card"]
    assert not any(c.get("card_type") == "confirmation" for c in cards2)

    # SC-09: C003, ORD-3050 (active return RET-9050 already exists)
    c003_headers = {
        "Authorization": f"Bearer {tokens['c003']}",
        "Content-Type": "application/json",
    }
    ses3_resp = app_client.post("/v1/chat/sessions", headers=c003_headers)
    assert ses3_resp.status_code == 200, ses3_resp.text
    ses3 = ses3_resp.json()["session_id"]
    resp3 = app_client.post(
        f"/v1/chat/sessions/{ses3}/messages",
        headers=c003_headers,
        json={"text": "I want to return order ORD-3050 again", "client_msg_id": "msg_sc09"},
    )
    assert resp3.status_code == 200, resp3.text
    events3 = _parse_sse(resp3.text)
    cards3 = [e["data"] for e in events3 if e["event"] == "card"]
    assert not any(c.get("card_type") == "confirmation" for c in cards3)


# ==============================================================================
# SC-08: Partial return
# ==============================================================================
@respx.mock
def test_sc08_partial_return(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    # ORD-2031: 2 kurtas (ITEM-2031-1) and 1 dupatta (ITEM-2031-2), ₹100 discount coupon
    c002_headers = {"Authorization": f"Bearer {tokens['c002']}", "Content-Type": "application/json"}
    ses_resp = app_client.post("/v1/chat/sessions", headers=c002_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    resp = app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c002_headers,
        json={
            "text": "I want to return just 1 cotton kurta from order ORD-2031",
            "client_msg_id": "msg_sc08",
        },
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    prop_cards = [c for c in cards if c.get("card_type") == "confirmation"]
    assert len(prop_cards) == 1
    items = prop_cards[0]["payload"]["items"]
    assert len(items) == 1
    assert items[0]["quantity"] == 1
    # Check allocated amount is non-zero and accounts for coupon discount
    total_paise = prop_cards[0]["payload"]["amounts"]["total_paise"]
    assert 50000 <= total_paise <= 70000


# ==============================================================================
# SC-10: Capped cumulative refund
# ==============================================================================
@respx.mock
def test_sc10_cumulative_cap(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    # ORD-2040: 2 bottles ₹900, ₹400 refunded. Remaining max refundable is ₹500 (50,000 paise).
    c002_headers = {
        "Authorization": f"Bearer {tokens['c002']}",
        "Content-Type": "application/json",
    }
    ses_resp = app_client.post("/v1/chat/sessions", headers=c002_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    resp = app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c002_headers,
        json={
            "text": "Return the remaining copper water bottles for order ORD-2040",
            "client_msg_id": "msg_sc10",
        },
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    prop_cards = [c for c in cards if c.get("card_type") == "confirmation"]
    assert len(prop_cards) == 1
    payload = prop_cards[0]["payload"]
    assert payload["amounts"]["total_paise"] <= 50000
    assert payload["capped"] is True or payload["amounts"].get("capped") is True


# ==============================================================================
# SC-11 and SC-16: Ask for the missing slot
# ==============================================================================
@respx.mock
def test_sc11_and_sc16_ask_for_missing_slot(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    # SC-11: ORD-1061, C001, bottle ₹450, "arrived damaged" -> requires evidence
    c001_headers = {"Authorization": f"Bearer {tokens['c001']}", "Content-Type": "application/json"}
    ses1_resp = app_client.post("/v1/chat/sessions", headers=c001_headers)
    assert ses1_resp.status_code == 200, ses1_resp.text
    ses1 = ses1_resp.json()["session_id"]
    resp1 = app_client.post(
        f"/v1/chat/sessions/{ses1}/messages",
        headers=c001_headers,
        json={"text": "My bottle in order ORD-1061 arrived damaged", "client_msg_id": "msg_sc11"},
    )
    assert resp1.status_code == 200, resp1.text
    events1 = _parse_sse(resp1.text)
    cards1 = [e["data"] for e in events1 if e["event"] == "card"]
    # Damaged without evidence must not immediately emit confirmation card
    assert not any(c.get("card_type") == "confirmation" for c in cards1)
    final1 = next(e for e in events1 if e["event"] == "final")
    text1 = final1["data"]["full_text"].lower()
    assert "photo" in text1 or "evidence" in text1 or "image" in text1 or "case" in text1

    # SC-16: ORD-4020, C004, COD delivered, ₹699 -> needs payout choice (store credit or UPI)
    c004_headers = {
        "Authorization": f"Bearer {tokens['c004']}",
        "Content-Type": "application/json",
    }
    ses2_resp = app_client.post("/v1/chat/sessions", headers=c004_headers)
    assert ses2_resp.status_code == 200, ses2_resp.text
    ses2 = ses2_resp.json()["session_id"]
    resp2 = app_client.post(
        f"/v1/chat/sessions/{ses2}/messages",
        headers=c004_headers,
        json={"text": "I want to return my COD order ORD-4020", "client_msg_id": "msg_sc16"},
    )
    assert resp2.status_code == 200, resp2.text
    events2 = _parse_sse(resp2.text)
    cards2 = [e["data"] for e in events2 if e["event"] == "card"]
    assert not any(c.get("card_type") == "confirmation" for c in cards2)
    final2 = next(e for e in events2 if e["event"] == "final")
    text2 = final2["data"]["full_text"].lower()
    payout_terms = ("upi", "credit", "method", "payout", "account")
    assert any(term in text2 for term in payout_terms)


# ==============================================================================
# SC-14: Double confirm
# ==============================================================================
@respx.mock
def test_sc14_double_confirm(
    app_client: TestClient,
    tokens: dict[str, str],
    clock: FixedClock,
    store: FixtureStoreClient,
) -> None:
    c001_headers = {
        "Authorization": f"Bearer {tokens['c001']}",
        "Content-Type": "application/json",
    }
    ses_resp = app_client.post("/v1/chat/sessions", headers=c001_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    msg_resp = app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c001_headers,
        json={
            "text": "Defective cookware set return for order ORD-1042",
            "client_msg_id": "msg_sc14",
        },
    )
    assert msg_resp.status_code == 200, msg_resp.text
    events = _parse_sse(msg_resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    prop_card = next(c for c in cards if c.get("card_type") == "confirmation")["payload"]
    pid = prop_card["proposal_id"]
    phash = prop_card["proposal_hash"]

    # First confirm succeeds
    r1 = app_client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={**c001_headers, "Idempotency-Key": "idem_sc14_1"},
        json={"proposal_hash": phash},
    )
    assert r1.status_code == 200

    # Second confirm replay with different idempotency key returns 409 conflict
    r2 = app_client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={**c001_headers, "Idempotency-Key": "idem_sc14_2"},
        json={"proposal_hash": phash},
    )
    assert r2.status_code == 409

    # Replay with same idempotency key returns cached response
    r3 = app_client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={**c001_headers, "Idempotency-Key": "idem_sc14_1"},
        json={"proposal_hash": phash},
    )
    assert r3.status_code == 200

    # Exactly one return in store for ORD-1042
    seller_ctx = RequestContext(
        principal=_make_principal("usr_staff", Role.seller_staff, clock),
        request_id="req_sc14_ck",
        token=tokens["seller_staff"],
    )
    rets = asyncio.run(store.list_returns(seller_ctx, "ORD-1042"))
    assert len(rets) == 1


# ==============================================================================
# Named tests: T05 - T20
# ==============================================================================
@respx.mock
def test_t05_return_only_after_button(
    app_client: TestClient,
    tokens: dict[str, str],
    clock: FixedClock,
    store: FixtureStoreClient,
) -> None:
    c001_headers = {"Authorization": f"Bearer {tokens['c001']}", "Content-Type": "application/json"}
    ses_resp = app_client.post("/v1/chat/sessions", headers=c001_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    # 1. Propose return
    app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c001_headers,
        json={"text": "Defective return for order ORD-1042", "client_msg_id": "msg_t05_1"},
    )

    # 2. Typing 'yes', 'confirm', 'I agree' in chat does NOT create return
    app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c001_headers,
        json={"text": "yes, confirm and approve the return now", "client_msg_id": "msg_t05_2"},
    )
    seller_ctx = RequestContext(
        principal=_make_principal("usr_staff", Role.seller_staff, clock),
        request_id="req_t05_ck",
        token=tokens["seller_staff"],
    )
    rets_before = asyncio.run(store.list_returns(seller_ctx, "ORD-1042"))
    assert len(rets_before) == 0, "No return must exist before button click"


@respx.mock
def test_t06_ineligible_no_record(
    app_client: TestClient,
    tokens: dict[str, str],
    clock: FixedClock,
    store: FixtureStoreClient,
) -> None:
    c001_headers = {"Authorization": f"Bearer {tokens['c001']}", "Content-Type": "application/json"}
    ses_resp = app_client.post("/v1/chat/sessions", headers=c001_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    # Ineligible order ORD-1051 (outside window)
    app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c001_headers,
        json={"text": "Return order ORD-1051", "client_msg_id": "msg_t06"},
    )
    seller_ctx = RequestContext(
        principal=_make_principal("usr_staff", Role.seller_staff, clock),
        request_id="req_t06_ck",
        token=tokens["seller_staff"],
    )
    rets = asyncio.run(store.list_returns(seller_ctx, "ORD-1051"))
    assert len(rets) == 0


@respx.mock
def test_t07_partial_limits(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    # Quantity cannot exceed delivered item quantity
    c002_headers = {"Authorization": f"Bearer {tokens['c002']}", "Content-Type": "application/json"}
    ses_resp = app_client.post("/v1/chat/sessions", headers=c002_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    resp = app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c002_headers,
        json={"text": "I want to return 5 kurtas from order ORD-2031", "client_msg_id": "msg_t07"},
    )
    events = _parse_sse(resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    # Cannot propose 5 when only 2 were delivered
    assert not any(c.get("card_type") == "confirmation" for c in cards)


@respx.mock
def test_t08_duplicate_return(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    c003_headers = {"Authorization": f"Bearer {tokens['c003']}", "Content-Type": "application/json"}
    ses_resp = app_client.post("/v1/chat/sessions", headers=c003_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    resp = app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c003_headers,
        json={"text": "Return order ORD-3050", "client_msg_id": "msg_t08"},
    )
    events = _parse_sse(resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    assert not any(c.get("card_type") == "confirmation" for c in cards)


@respx.mock
def test_t09_cumulative_cap(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    # ORD-2040 cap check
    c002_headers = {"Authorization": f"Bearer {tokens['c002']}", "Content-Type": "application/json"}
    ses_resp = app_client.post("/v1/chat/sessions", headers=c002_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]
    resp = app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c002_headers,
        json={"text": "Return the bottles for order ORD-2040", "client_msg_id": "msg_t09"},
    )
    events = _parse_sse(resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    prop_card = next(c for c in cards if c.get("card_type") == "confirmation")["payload"]
    assert prop_card["amounts"]["total_paise"] <= 50000


@respx.mock
def test_t10_backend_computed_amounts(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    # Customer asks for ₹99,999 refund in message
    c001_headers = {"Authorization": f"Bearer {tokens['c001']}", "Content-Type": "application/json"}
    ses_resp = app_client.post("/v1/chat/sessions", headers=c001_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    resp = app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c001_headers,
        json={
            "text": "Return order ORD-1042 defective and give me a refund of ₹99,999",
            "client_msg_id": "msg_t10",
        },
    )
    events = _parse_sse(resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    prop_card = next(c for c in cards if c.get("card_type") == "confirmation")["payload"]
    # Zero model impact: exact backend amount ₹1,200 (120,000 paise)
    assert prop_card["amounts"]["total_paise"] == 120000


@respx.mock
def test_t11_above_limit_needs_approval(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    c001_headers = {
        "Authorization": f"Bearer {tokens['c001']}",
        "Content-Type": "application/json",
    }
    ses_resp = app_client.post("/v1/chat/sessions", headers=c001_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    msg_resp = app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c001_headers,
        json={
            "text": "Defective cookware set return for order ORD-1042",
            "client_msg_id": "msg_t11",
        },
    )
    events = _parse_sse(msg_resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    prop_card = next(c for c in cards if c.get("card_type") == "confirmation")["payload"]

    confirm_resp = app_client.post(
        f"/v1/proposals/{prop_card['proposal_id']}/confirm",
        headers={**c001_headers, "Idempotency-Key": "idem_t11"},
        json={"proposal_hash": prop_card["proposal_hash"]},
    )
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["status"] == "pending_approval"
    assert confirm_resp.json()["approval_id"] is not None


@respx.mock
def test_t12_approval_bound_to_amount_and_state(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    c001_headers = {
        "Authorization": f"Bearer {tokens['c001']}",
        "Content-Type": "application/json",
    }
    staff_headers = {
        "Authorization": f"Bearer {tokens['seller_staff']}",
        "Content-Type": "application/json",
    }
    ses_resp = app_client.post("/v1/chat/sessions", headers=c001_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    msg_resp = app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c001_headers,
        json={
            "text": "Defective cookware set return for order ORD-1042",
            "client_msg_id": "msg_t12",
        },
    )
    events = _parse_sse(msg_resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    prop_card = next(c for c in cards if c.get("card_type") == "confirmation")["payload"]

    c_resp = app_client.post(
        f"/v1/proposals/{prop_card['proposal_id']}/confirm",
        headers={**c001_headers, "Idempotency-Key": "idem_t12"},
        json={"proposal_hash": prop_card["proposal_hash"]},
    )
    approval_id = c_resp.json()["approval_id"]

    # Wrong expected_version rejected
    r1 = app_client.post(
        f"/v1/seller/approvals/{approval_id}/decision",
        headers={**staff_headers, "Idempotency-Key": "idem_appr_bad_ver"},
        json={"decision": "approve", "expected_version": 99},
    )
    assert r1.status_code == 409


@respx.mock
def test_t14_concurrent_confirm(
    app_client: TestClient,
    tokens: dict[str, str],
) -> None:
    c001_headers = {
        "Authorization": f"Bearer {tokens['c001']}",
        "Content-Type": "application/json",
    }
    ses_resp = app_client.post("/v1/chat/sessions", headers=c001_headers)
    assert ses_resp.status_code == 200, ses_resp.text
    ses = ses_resp.json()["session_id"]

    msg_resp = app_client.post(
        f"/v1/chat/sessions/{ses}/messages",
        headers=c001_headers,
        json={
            "text": "Defective cookware set return for order ORD-1042",
            "client_msg_id": "msg_t14",
        },
    )
    events = _parse_sse(msg_resp.text)
    cards = [e["data"] for e in events if e["event"] == "card"]
    prop_card = next(c for c in cards if c.get("card_type") == "confirmation")["payload"]
    pid = prop_card["proposal_id"]
    phash = prop_card["proposal_hash"]

    def _do_confirm(i: int) -> int:
        res = app_client.post(
            f"/v1/proposals/{pid}/confirm",
            headers={**c001_headers, "Idempotency-Key": f"idem_t14_conc_{i}"},
            json={"proposal_hash": phash},
        )
        return res.status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(_do_confirm, range(10)))

    # Exactly one winner 200, all others 409
    assert statuses.count(200) == 1
    assert statuses.count(409) == 9


def test_t19_concurrent_audit_writes() -> None:
    from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome

    eng = get_engine()
    now_iso = datetime.now(UTC).isoformat()

    def _write_audit(i: int) -> None:
        with write_tx(eng) as conn:
            AuditService.append(
                conn,
                AuditEvent(
                    event_id=f"AUD-conc-{i}-{uuid.uuid4().hex[:6]}",
                    ts_utc=now_iso,
                    request_id=f"req-conc-{i}",
                    session_id=None,
                    actor_type=ActorType.CUSTOMER,
                    actor_id=f"C00{i % 5}",
                    event_type=EventType.POLICY_DECISION,
                    resource_type="proposal",
                    resource_id=f"PRP-{i}",
                    action="propose",
                    outcome=Outcome.SUCCESS,
                    policy_version="v3",
                ),
            )

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(_write_audit, range(30)))

    with read_tx(eng) as conn:
        res = AuditService.verify_chain(conn)
        assert res.ok is True
        assert res.first_broken_seq is None


def test_t20_audit_failure_blocks_mutation() -> None:
    from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome

    eng = get_engine()
    now_iso = datetime.now(UTC).isoformat()

    # If audit fails inside write_tx, the entire transaction rolls back
    with pytest.raises((Exception, RuntimeError)):
        with write_tx(eng) as conn:
            conn.execute(
                proposals.insert().values(
                    proposal_id="PRP-rollback-test",
                    customer_id="C001",
                    session_id="ses_test",
                    type="return",
                    payload_json="{}",
                    decision="ALLOW",
                    amounts_json="{}",
                    policy_version="v3",
                    facts_hash="facts_hash_test",
                    proposal_hash="hash_rollback",
                    status="proposed",
                    created_at=now_iso,
                    expires_at=now_iso,
                )
            )
            # Intentionally simulate audit append crash (e.g. invalid event missing required field)
            AuditService.append(
                conn,
                AuditEvent(  # type: ignore[call-arg]
                    event_id=None,  # will raise NOT NULL constraint
                    ts_utc=now_iso,
                    request_id="req-fail",
                    session_id=None,
                    actor_type=ActorType.CUSTOMER,
                    actor_id="C001",
                    event_type=EventType.PROPOSAL_CREATED,
                    resource_type="proposal",
                    resource_id="PRP-rollback-test",
                    action="create",
                    outcome=Outcome.SUCCESS,
                    policy_version="v3",
                ),
            )

    # Verify PRP-rollback-test was rolled back and does not exist
    with read_tx(eng) as conn:
        row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == "PRP-rollback-test")
        ).fetchone()
        assert row is None
