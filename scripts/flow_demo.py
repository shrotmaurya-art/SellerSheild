"""Dev tool: SC-04 Full Approval Flow demonstration script (T2.12).

Executes the SC-04 approval flow step by step through the Agent application,
printing every state transition from chat proposal to refund completion and
cryptographic audit verification.

Flow steps:
1. Customer initiates chat session and requests return for ORD-1042 (defective).
2. Agent proposes return; emits confirmation card with deterministic proposal_hash.
3. Customer confirms proposal -> pending seller approval (₹1,200 > ₹500 autonomy cap).
4. Seller owner approves -> return approved, refund authorized (120,000 paise).
5. Return lifecycle advances: pickup_scheduled -> in_transit_back -> received -> inspected_ok.
6. Webhook triggers refund release -> refund completed via SimPay.
7. Verification: customer client_event exists and SHA-256 audit chain verified.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import hmac
import json
import secrets
import sys
import uuid
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
import jwt
from jwt.algorithms import RSAAlgorithm
import respx
from sqlalchemy import select
from starlette.testclient import TestClient

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import get_store_client as chat_get_store_client, reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps as auth_deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.db import get_engine, read_tx, reset_engine
from sellershield_agent.domain.state import client_events
from sellershield_agent.main import create_app
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
WEBHOOK_SECRET = "whsec_flow_demo_secret_key_123456789"
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


def _create_rsa_key_pair(kid: str = "demo-key") -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
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
    kid: str = "demo-key",
) -> str:
    now_ts = int(clock.now().timestamp())
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "role": role,
        "iat": now_ts,
        "exp": now_ts + 3600,
        "auth_time": now_ts,
        "jti": str(uuid.uuid4()),
        "store_id": "nestkart",
    }
    if customer_id:
        payload["customer_id"] = customer_id
    return str(jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid, "alg": "RS256"}))


def _make_principal(user_id: str, role: Role, clock: FixedClock, customer_id: str | None = None) -> Principal:
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


def run_demo() -> None:
    print("=" * 70)
    print("  SELLER SHIELD AI - GATE G2 FLOW DEMO (SC-04)")
    print("=" * 70)

    import os
    os.environ["STORE_MODE"] = "fixtures"
    os.environ["DEV_MODE"] = "true"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ["STORE_JWKS_URL"] = JWKS_URL
    os.environ["WEBHOOK_SECRET"] = WEBHOOK_SECRET

    clock = FixedClock(T0_BASE)
    priv, _, jwk_dict = _create_rsa_key_pair("demo-key")
    c001_tok = _make_token("usr_c001", "C001", "customer", priv, clock)
    owner_tok = _make_token("usr_owner", None, "seller_owner", priv, clock)

    cust_headers = {"Authorization": f"Bearer {c001_tok}", "Content-Type": "application/json"}
    owner_headers = {"Authorization": f"Bearer {owner_tok}", "Content-Type": "application/json"}

    store = FixtureStoreClient(clock)

    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()

    app = create_app()
    app.dependency_overrides[auth_deps.get_clock] = lambda: clock
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda **kw: store
    app.dependency_overrides[chat_get_store_client] = lambda **kw: store

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.get(JWKS_URL).respond(200, json={"keys": [jwk_dict]})

        with TestClient(app, raise_server_exceptions=False) as client:
            # ── 1. Create chat session ────────────────────────────────────────
            print("\n[STEP 1] Customer C001 starts chat session...")
            ses_resp = client.post("/v1/chat/sessions", headers=cust_headers)
            if ses_resp.status_code != 200:
                print(f"FAILED to create session: {ses_resp.text}")
                sys.exit(1)
            session_id = ses_resp.json()["session_id"]
            print(f"  -> Session created: {session_id}")

            # ── 2. Request return ─────────────────────────────────────────────
            msg_text = "I want to return order ORD-1042 because it is defective"
            print(f"\n[STEP 2] Customer sends message: '{msg_text}'")
            msg_resp = client.post(
                f"/v1/chat/sessions/{session_id}/messages",
                headers=cust_headers,
                json={"text": msg_text, "client_msg_id": "demo_msg_1"},
            )
            if msg_resp.status_code != 200:
                print(f"FAILED to post message: {msg_resp.text}")
                sys.exit(1)

            events = _parse_sse(msg_resp.text)
            cards = [e["data"] for e in events if e["event"] == "card"]
            prop_card = next(
                (c["payload"] for c in cards if isinstance(c, dict) and c.get("card_type") == "confirmation"),
                None,
            )
            if not prop_card:
                print(f"FAILED: No proposal confirmation card emitted! Cards: {cards}")
                sys.exit(1)

            proposal_id = prop_card["proposal_id"]
            proposal_hash = prop_card["proposal_hash"]
            total_paise = prop_card["amounts"]["total_paise"]
            display_amt = prop_card["amounts"]["display"]
            decision = prop_card["decision"]

            print(f"  -> Proposal generated: {proposal_id}")
            print(f"  -> Decision: {decision.upper()} (Refund amount: {display_amt} / {total_paise} paise)")
            print(f"  -> Proposal hash: {proposal_hash}")

            # ── 3. Customer confirms proposal ─────────────────────────────────
            print(f"\n[STEP 3] Customer confirms proposal {proposal_id}...")
            confirm_resp = client.post(
                f"/v1/proposals/{proposal_id}/confirm",
                headers={**cust_headers, "Idempotency-Key": "demo_idem_confirm"},
                json={"proposal_hash": proposal_hash},
            )
            if confirm_resp.status_code != 200:
                print(f"FAILED to confirm proposal: {confirm_resp.text}")
                sys.exit(1)

            c_data = confirm_resp.json()
            c_status = c_data["status"]
            approval_id = c_data.get("approval_id")
            return_id = c_data.get("return_id")
            refund_id = c_data.get("refund_id")

            print(f"  -> Proposal status: {c_status}")
            print(f"  -> Store Return ID: {return_id}")
            print(f"  -> Store Refund ID: {refund_id}")
            print(f"  -> Pending Approval ID: {approval_id}")

            # ── 4. Seller approves request ────────────────────────────────────
            print(f"\n[STEP 4] Seller owner reviews and approves {approval_id}...")
            appr_resp = client.post(
                f"/v1/seller/approvals/{approval_id}/decision",
                headers={**owner_headers, "Idempotency-Key": "demo_idem_appr"},
                json={"decision": "approve", "expected_version": 1},
            )
            if appr_resp.status_code != 200:
                print(f"FAILED to approve: {appr_resp.text}")
                sys.exit(1)

            print(f"  -> Approval decision recorded: {appr_resp.json()['status']}")

            seller_ctx = RequestContext(
                principal=_make_principal("usr_owner", Role.seller_owner, clock),
                request_id="req_demo_verify",
                token=owner_tok,
            )
            ret_obj = asyncio.run(store.get_return(seller_ctx, return_id))
            rfd_obj = asyncio.run(store.get_refund(seller_ctx, refund_id))
            print(f"  -> Return status in store: {ret_obj.status}")
            print(f"  -> Authorized amount in store: {rfd_obj.authorized_amount_paise} paise (by {rfd_obj.authorized_by})")

            # ── 5. Advance return lifecycle ───────────────────────────────────
            print("\n[STEP 5] Advancing physical return lifecycle...")
            for st in ["pickup_scheduled", "in_transit_back", "received", "inspected_ok"]:
                asyncio.run(
                    store.transition_return(
                        ctx=seller_ctx,
                        return_id=return_id,
                        to=st,
                        reason=f"Demo advance to {st}",
                        idempotency_key=f"demo-adv-{return_id}-{st}",
                    )
                )
                print(f"  -> Return lifecycle: {st}")

            # ── 6. Webhook release ────────────────────────────────────────────
            print("\n[STEP 6] Store dispatches return.updated webhook with inspected_ok...")
            now_iso = clock.now().isoformat()
            now_ts = str(int(clock.now().timestamp()))
            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            body_dict = {
                "event_id": event_id,
                "type": "return.updated",
                "occurred_at": now_iso,
                "resource_version": 1,
                "data": {"return_id": return_id, "status": "inspected_ok"},
            }
            raw_body = json.dumps(body_dict).encode("utf-8")
            sig = _sign_webhook(raw_body, now_ts)
            wh_headers = {
                "Content-Type": "application/json",
                "X-Store-Signature": sig,
                "X-Store-Timestamp": now_ts,
                "X-Event-Id": event_id,
            }
            wh_resp = client.post("/v1/webhooks/store", content=raw_body, headers=wh_headers)
            if wh_resp.status_code != 200:
                print(f"FAILED webhook dispatch: {wh_resp.text}")
                sys.exit(1)

            final_rfd = asyncio.run(store.get_refund(seller_ctx, refund_id))
            print(f"  -> Refund payout status: {final_rfd.status.upper()}")

            # ── 7. Client Event and Audit Verification ────────────────────────
            print("\n[STEP 7] Verifying customer event and cryptographic audit chain...")
            eng = get_engine()
            with read_tx(eng) as conn:
                cev = conn.execute(
                    select(client_events).where(client_events.c.customer_id == "C001")
                ).fetchall()
                print(f"  -> Client events logged for C001: {len(cev)}")
                verify_res = AuditService.verify_chain(conn)
                print(f"  -> Audit chain integrity verified: ok={verify_res.ok} (broken_seq={verify_res.first_broken_seq})")
                assert verify_res.ok is True
                assert verify_res.first_broken_seq is None

            print("\n" + "=" * 70)
            print("  GATE G2 SC-04 FLOW SUCCESSFULLY COMPLETED!")
            print("=" * 70)


if __name__ == "__main__":
    run_demo()
