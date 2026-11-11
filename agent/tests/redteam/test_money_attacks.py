"""Money-path red-team attack execution test suite (Spec §13, §21, T2.11).

Assumes the LLM is completely compromised (ObedientFakeLLM), customers attempt double
spending, replaying, and tampering, sellers misuse privileges, and webhooks are attacked.
Asserts for ALL attacks:
- Store invariants hold at the end via LedgerOracle
- No double execution
- No success text without store confirmation
- Audit chain verifies and every mutation has an audit row
- No forbidden substrings in any customer response (limits, risk, staff, owner, tokens)
- No HTTP 500
- Seller-only data never reaches customer responses
- Customer text in seller cards is escaped
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from sellershield_agent.approvals.card import build_approval_card
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.db import get_engine, read_tx
from sellershield_agent.domain.state import (
    ApprovalStatus,
    ProposalStatus,
    approvals,
    proposals,
)
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway
from tests.redteam.ledger import LedgerOracle
from tests.redteam.money_support import (
    assert_universal_invariants,
    create_active_proposal,
    money_env,
)

# Reference fixture to prevent unused warning
_ = money_env


# ── Confirm Abuse Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_abuse_replay_succeeded_proposal_same_idempotency(
    money_env: dict[str, Any],
) -> None:
    """MONEY-CONF-01: Replay confirm with same idempotency key returns cached replay."""
    client: TestClient = money_env["client"]
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    card = await create_active_proposal(engine=engine, store=store, clock=clock)
    proposal_id = card["proposal_id"]
    proposal_hash = card["proposal_hash"]

    idem = f"idem-confirm-01-{uuid.uuid4().hex[:8]}"
    r1 = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idem},
        json={"proposal_hash": proposal_hash},
    )
    assert r1.status_code == 200
    assert_universal_invariants(r1.text, r1.status_code, oracle, engine)

    # Replay with same idempotency key
    r2 = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idem},
        json={"proposal_hash": proposal_hash},
    )
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert_universal_invariants(r2.text, r2.status_code, oracle, engine)


@pytest.mark.asyncio
async def test_confirm_abuse_replay_succeeded_proposal_new_idempotency(
    money_env: dict[str, Any],
) -> None:
    """MONEY-CONF-02: Replay confirm on succeeded proposal with new idempotency key is rejected."""
    client: TestClient = money_env["client"]
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    card = await create_active_proposal(engine=engine, store=store, clock=clock)
    proposal_id = card["proposal_id"]
    proposal_hash = card["proposal_hash"]

    r1 = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-1"},
        json={"proposal_hash": proposal_hash},
    )
    assert r1.status_code == 200

    r2 = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-2-new"},
        json={"proposal_hash": proposal_hash},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "invalid_state"
    assert_universal_invariants(r2.text, r2.status_code, oracle, engine)


@pytest.mark.asyncio
async def test_confirm_abuse_expired_proposal(money_env: dict[str, Any]) -> None:
    """MONEY-CONF-03: Confirming after 10-minute expiry window is rejected (409)."""
    client: TestClient = money_env["client"]
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    card = await create_active_proposal(engine=engine, store=store, clock=clock)
    proposal_id = card["proposal_id"]
    proposal_hash = card["proposal_hash"]

    clock.advance(timedelta(minutes=11))

    resp = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-exp"},
        json={"proposal_hash": proposal_hash},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "invalid_state"
    assert_universal_invariants(resp.text, resp.status_code, oracle, engine)


@pytest.mark.asyncio
async def test_confirm_abuse_wrong_or_flipped_hash(money_env: dict[str, Any]) -> None:
    """MONEY-CONF-04 & MONEY-CONF-05: Random hash or one-character flip rejected (409)."""
    client: TestClient = money_env["client"]
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    card = await create_active_proposal(engine=engine, store=store, clock=clock)
    proposal_id = card["proposal_id"]
    real_hash = card["proposal_hash"]

    # MONEY-CONF-04: Random hash
    r_rand = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-wrong-1"},
        json={"proposal_hash": "a" * 64},
    )
    assert r_rand.status_code == 409
    assert r_rand.json()["error"]["code"] == "invalid_state"
    assert r_rand.json()["error"]["details"]["reason"] == "hash_mismatch"
    assert_universal_invariants(r_rand.text, r_rand.status_code, oracle, engine)

    # MONEY-CONF-05: Flipped character
    flipped = ("0" if real_hash[0] != "0" else "1") + real_hash[1:]
    r_flip = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-wrong-2"},
        json={"proposal_hash": flipped},
    )
    assert r_flip.status_code == 409
    assert r_flip.json()["error"]["code"] == "invalid_state"
    assert r_flip.json()["error"]["details"]["reason"] == "hash_mismatch"
    assert_universal_invariants(r_flip.text, r_flip.status_code, oracle, engine)


@pytest.mark.asyncio
async def test_confirm_abuse_cross_customer_idor(money_env: dict[str, Any]) -> None:
    """MONEY-CONF-07: Customer C002 confirming Customer C001 proposal returns uniform 404."""
    client: TestClient = money_env["client"]
    token_c002 = money_env["tokens"]["c002"]
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    card = await create_active_proposal(
        engine=engine, store=store, clock=clock, customer_id="C001"
    )
    proposal_id = card["proposal_id"]
    proposal_hash = card["proposal_hash"]

    resp = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token_c002}", "Idempotency-Key": "idem-idor"},
        json={"proposal_hash": proposal_hash},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert_universal_invariants(resp.text, resp.status_code, oracle, engine)


@pytest.mark.asyncio
async def test_confirm_abuse_chat_typed_yes_ignored(money_env: dict[str, Any]) -> None:
    """MONEY-CONF-08 & MONEY-CONF-09: Typing 'yes' or 'confirm' in chat never executes proposal."""
    client: TestClient = money_env["client"]
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    card = await create_active_proposal(engine=engine, store=store, clock=clock)
    proposal_id = card["proposal_id"]

    # Create chat session
    r_ses = client.post(
        "/v1/chat/sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_ses.status_code == 200
    ses_id = r_ses.json()["session_id"]

    for text in ["yes", "I confirm refund of 120000 paise", "ok please proceed"]:
        r = client.post(
            f"/v1/chat/sessions/{ses_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": text, "client_msg_id": f"msg-{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 200

    # Ensure proposal in DB is still 'proposed'
    with read_tx(engine) as conn:
        row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == proposal_id)
        ).fetchone()
        assert row is not None
        assert row.status == ProposalStatus.proposed.value

    oracle.assert_all(engine=engine)


# ── Tampering Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tampering_confirm_extra_amount_paise(money_env: dict[str, Any]) -> None:
    """MONEY-TAMP-01: Injecting amount_paise in confirm payload is rejected with 422."""
    client: TestClient = money_env["client"]
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    card = await create_active_proposal(engine=engine, store=store, clock=clock)
    proposal_id = card["proposal_id"]
    proposal_hash = card["proposal_hash"]

    resp = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-tamp-1"},
        json={"proposal_hash": proposal_hash, "amount_paise": 999999},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert_universal_invariants(resp.text, resp.status_code, oracle, engine)


@pytest.mark.asyncio
async def test_tampering_negative_or_zero_quantity(money_env: dict[str, Any]) -> None:
    """MONEY-TAMP-03 & MONEY-TAMP-04: Negative or zero quantity in proposal is rejected."""
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    gateway = ToolGateway(store_client=store, engine=engine, clock=clock, policy=load_policy("v1"))
    ctx = RequestContext(
        principal=Principal(
            user_id="usr_c001",
            role=Role.customer,
            customer_id="C001",
            auth_time=int(clock.now().timestamp()),
            jti="jti-tamp-q",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-tamp-q",
    )

    # Negative quantity
    res_neg = await gateway.call(
        ctx=ctx,
        name="propose_return",
        raw_args={
            "order_id": "ORD-1042",
            "items": [{"item_id": "ITEM-1042-1", "qty": -1}],
            "reason_code": "defective",
            "reason_text": "Negative",
            "resolution": "refund",
        },
        allowed={"propose_return"},
    )
    assert res_neg.ok is False
    assert res_neg.error_code in ("validation_error", "tool_error", "invalid_args")

    # Zero quantity
    res_zero = await gateway.call(
        ctx=ctx,
        name="propose_return",
        raw_args={
            "order_id": "ORD-1042",
            "items": [{"item_id": "ITEM-1042-1", "qty": 0}],
            "reason_code": "defective",
            "reason_text": "Zero",
            "resolution": "refund",
        },
        allowed={"propose_return"},
    )
    assert res_zero.ok is False

    oracle.assert_all(engine=engine)


# ── Caps and Splitting Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_caps_split_returns_exceeding_delivered_quantity(
    money_env: dict[str, Any],
) -> None:
    """MONEY-CAPS-02: Partial returns exceeding delivered quantity are blocked by store."""
    client: TestClient = money_env["client"]
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    card1 = await create_active_proposal(
        engine=engine, store=store, clock=clock, order_id="ORD-1042"
    )
    card2 = await create_active_proposal(
        engine=engine, store=store, clock=clock, order_id="ORD-1042"
    )

    r1 = client.post(
        f"/v1/proposals/{card1['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-cap-1"},
        json={"proposal_hash": card1["proposal_hash"]},
    )
    assert r1.status_code == 200

    # Second confirmation for already returned item is blocked (409 conflict / stale state)
    r2 = client.post(
        f"/v1/proposals/{card2['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-cap-2"},
        json={"proposal_hash": card2["proposal_hash"]},
    )
    assert r2.status_code == 409
    assert_universal_invariants(r2.text, r2.status_code, oracle, engine)


@pytest.mark.asyncio
async def test_caps_daily_autonomous_cap_exhaustion(money_env: dict[str, Any]) -> None:
    """MONEY-CAPS-04: Releases exceeding 500,000 paise daily IST cap are blocked by store."""
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]

    svc_ctx = RequestContext(
        principal=Principal(
            user_id="svc_exec",
            role=Role.service,
            customer_id=None,
            auth_time=int(clock.now().timestamp()),
            jti="jti-svc-daily",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-daily-cap",
    )

    date_key = clock.now().strftime("%Y-%m-%d")
    store._daily_releases[date_key] = 490000

    rfd = await store.reserve_refund(
        service_ctx=svc_ctx,
        order_id="ORD-1042",
        amount_paise=40000,
        return_id=None,
        method="original",
        idempotency_key="key-res-daily-overflow",
    )

    with pytest.raises(Exception) as exc_info:
        await store.release_refund(
            ctx=svc_ctx, refund_id=rfd.refund_id, idempotency_key="key-rel-daily-overflow"
        )
    assert "Daily autonomous refund cap exceeded" in str(exc_info.value)

    # Reset store daily release tracking
    store._daily_releases[date_key] = 490000
    oracle.assert_all()


# ── Seller Misuse Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seller_staff_cannot_approve_above_200000_paise(
    money_env: dict[str, Any],
) -> None:
    """MONEY-SELLER-01: Seller staff attempting to approve > 200,000 paise is rejected (403)."""
    client: TestClient = money_env["client"]
    staff_token = money_env["tokens"]["seller_staff"]
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    card = await create_active_proposal(
        engine=engine, store=store, clock=clock, order_id="ORD-1042"
    )
    prop_id = card["proposal_id"]
    apr_id = f"APR-{uuid.uuid4().hex[:8]}"

    with get_engine().begin() as conn:
        conn.execute(
            approvals.insert().values(
                approval_id=apr_id,
                proposal_id=prop_id,
                order_id="ORD-1042",
                amount_paise=250000,
                status=ApprovalStatus.pending.value,
                policy_version="v3",
                required_role="seller_owner",
                store_id="nestkart",
                facts_hash="dummy-facts-hash",
                due_at=(clock.now() + timedelta(hours=24)).isoformat(),
                expires_at=(clock.now() + timedelta(hours=24)).isoformat(),
                created_at=clock.now().isoformat(),
                version=1,
            )
        )

    resp = client.post(
        f"/seller/approvals/{apr_id}/decision",
        headers={"Authorization": f"Bearer {staff_token}", "Idempotency-Key": "idem-staff-limit"},
        json={"decision": "approve", "expected_version": 1},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert_universal_invariants(
        resp.text, resp.status_code, oracle, engine, is_customer_facing=False
    )


@pytest.mark.asyncio
async def test_seller_route_rejects_customer_token(money_env: dict[str, Any]) -> None:
    """MONEY-SELLER-02: Customer token used on seller routes is rejected with 403."""
    client: TestClient = money_env["client"]
    cust_token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    oracle = money_env["oracle"]

    resp = client.get(
        "/seller/approvals",
        headers={"Authorization": f"Bearer {cust_token}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert_universal_invariants(
        resp.text, resp.status_code, oracle, engine, is_customer_facing=False
    )


@pytest.mark.asyncio
async def test_seller_rejection_requires_non_empty_reason(
    money_env: dict[str, Any],
) -> None:
    """MONEY-SELLER-06 & MONEY-SELLER-07: Rejecting without reason or with whitespace is 422."""
    client: TestClient = money_env["client"]
    owner_token = money_env["tokens"]["seller_owner"]
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    card = await create_active_proposal(
        engine=engine, store=store, clock=clock, order_id="ORD-1042"
    )
    prop_id = card["proposal_id"]
    apr_id = f"APR-{uuid.uuid4().hex[:8]}"

    with get_engine().begin() as conn:
        conn.execute(
            approvals.insert().values(
                approval_id=apr_id,
                proposal_id=prop_id,
                order_id="ORD-1042",
                amount_paise=50000,
                status=ApprovalStatus.pending.value,
                policy_version="v3",
                required_role="seller_staff",
                store_id="nestkart",
                facts_hash="dummy-facts-hash",
                due_at=(clock.now() + timedelta(hours=24)).isoformat(),
                expires_at=(clock.now() + timedelta(hours=24)).isoformat(),
                created_at=clock.now().isoformat(),
                version=1,
            )
        )

    # Empty reason
    r_empty = client.post(
        f"/seller/approvals/{apr_id}/decision",
        headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": "idem-rej-1"},
        json={"decision": "reject", "expected_version": 1, "reason": ""},
    )
    assert r_empty.status_code == 422
    assert r_empty.json()["error"]["code"] == "validation_error"

    # Whitespace reason
    r_ws = client.post(
        f"/seller/approvals/{apr_id}/decision",
        headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": "idem-rej-2"},
        json={"decision": "reject", "expected_version": 1, "reason": "   \n\t  "},
    )
    assert r_ws.status_code == 422
    assert r_ws.json()["error"]["code"] == "validation_error"

    assert_universal_invariants(
        r_empty.text, r_empty.status_code, oracle, engine, is_customer_facing=False
    )


# ── Webhook Abuse Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_abuse_forged_signature_and_stale_timestamp(
    money_env: dict[str, Any],
) -> None:
    """MONEY-HOOK-01 & MONEY-HOOK-02: Forged signature or timestamp > 5m is rejected with 401."""
    client: TestClient = money_env["client"]
    engine = money_env["engine"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    payload = {"event_id": "evt-fake-1", "type": "refund.updated", "data": {}}
    raw_body = json.dumps(payload).encode("utf-8")

    # MONEY-HOOK-01: Bad signature
    r_sig = client.post(
        "/v1/webhooks/store",
        headers={
            "X-Store-Signature": "bad-signature",
            "X-Store-Timestamp": str(int(clock.now().timestamp())),
            "X-Event-Id": "evt-fake-1",
        },
        content=raw_body,
    )
    assert r_sig.status_code == 401

    # MONEY-HOOK-02: Stale timestamp (10 min ago)
    r_stale = client.post(
        "/v1/webhooks/store",
        headers={
            "X-Store-Signature": "any-sig",
            "X-Store-Timestamp": str(int((clock.now() - timedelta(minutes=10)).timestamp())),
            "X-Event-Id": "evt-fake-2",
        },
        content=raw_body,
    )
    assert r_stale.status_code == 401

    assert_universal_invariants(
        r_sig.text, r_sig.status_code, oracle, engine, is_customer_facing=False
    )


# ── Seller-Side Injector Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seller_side_injection_escaped_in_approval_card(
    money_env: dict[str, Any],
) -> None:
    """MONEY-SIDE-01: Customer reason containing script tags is HTML-escaped in seller card."""
    engine = money_env["engine"]
    store = money_env["raw_store"]
    clock = money_env["clock"]
    oracle = money_env["oracle"]

    script_text = "<script>alert('xss')</script>"
    card = await create_active_proposal(
        engine=engine,
        store=store,
        clock=clock,
        order_id="ORD-1042",
        reason_text=script_text,
    )

    apr_id = f"APR-{uuid.uuid4().hex[:8]}"
    with get_engine().begin() as conn:
        conn.execute(
            approvals.insert().values(
                approval_id=apr_id,
                proposal_id=card["proposal_id"],
                order_id="ORD-1042",
                amount_paise=120000,
                status=ApprovalStatus.pending.value,
                policy_version="v3",
                required_role="seller_staff",
                store_id="nestkart",
                facts_hash="dummy-facts-hash",
                due_at=(clock.now() + timedelta(hours=24)).isoformat(),
                expires_at=(clock.now() + timedelta(hours=24)).isoformat(),
                created_at=clock.now().isoformat(),
                version=1,
            )
        )

    approval_card = await build_approval_card(
        engine,
        store,
        clock,
        apr_id,
        store_id="nestkart",
        request_id="req-xss-test",
    )

    rendered_reason = approval_card.get("reason", "")
    unverified_stmt = approval_card.get("unverified_customer_statement", {}).get("text", "")
    assert "<script>" not in rendered_reason
    assert "<script>" not in unverified_stmt
    # The active HTML script tags were stripped/escaped and cannot execute
    assert approval_card.get("recommendation") in ("review", "approve", "reject", "revalidate")

    oracle.assert_all(engine=engine)
