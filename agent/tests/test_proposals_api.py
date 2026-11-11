"""Step A tests for proposal confirmation/cancel API, approval cards, and loophole defenses.

Enforces TASK T2.3b and Spec docs/spec/02-agent.md sections 2, 5 (W3/W4), 11, 13, 15 (T05), 21:
- Authentication & role boundaries (401 unauthenticated, 403 seller/service tokens, clock-checked).
- Uniform 404 IDOR defense on confirm/cancel (never reveals existence or leaks customer data).
- Idempotency-Key validation, replay, and 409 idempotency_mismatch across bodies/endpoints/ids.
- Proposal hash binding (hmac compare_digest, DB payload tampering detection, extra='forbid').
- Expiry boundary enforcement under FixedClock (clock.now() >= expires_at -> 409 invalid_state).
- Store staleness revalidation without false failure on normal clock advance (facts.now trap) and
  without holding write transactions across await/HTTP.
- Overlap guard preventing concurrent in-flight proposals for the same order/item from confirming.
- Typed 'yes'/'confirm' chat text and unauthorized ChatAction bypass prevention (T05).
- Single-winner concurrent confirm serialization and audit fail-closed atomicity (T14, T20).
- Confirmation & return_preview card builders, XSS stripping, FactSheet/Verifier compliance, and
  ConversationPipeline SSE confirmation card emission.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select, update
from starlette.testclient import TestClient

from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.audit.models import audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.compose.factsheet import build_factsheet
from sellershield_agent.compose.verifier import verify
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, read_tx, reset_engine, write_tx
from sellershield_agent.domain.models import ReturnItem, ReturnReasonCode, ReturnResolution
from sellershield_agent.domain.session import ChatMessageInput
from sellershield_agent.domain.state import ProposalStatus, proposals
from sellershield_agent.main import create_app
from sellershield_agent.orchestrator.pipeline import ConversationPipeline
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.base import ToolResult
from sellershield_agent.tools.gateway import ToolGateway

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scenarios" / "fixtures" / "store.json"
)
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
FORBIDDEN_CUSTOMER_WORDS = ("limit", "threshold", "risk", "score", "staff", "owner")


def _create_rsa_key_pair(
    kid: str = "prop-key-1",
) -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict["kid"] = kid
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return priv, pub, jwk_dict


def _make_token(
    *,
    user_id: str,
    customer_id: str | None,
    role: str,
    priv: RSAPrivateKey,
    now_dt: datetime,
    kid: str = "prop-key-1",
    exp_seconds: int = 3600,
    include_customer_id_claim: bool = True,
) -> str:
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "role": role,
        "iat": int(now_dt.timestamp()),
        "exp": int(now_dt.timestamp()) + exp_seconds,
        "auth_time": int(now_dt.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    if include_customer_id_claim and customer_id is not None:
        payload["customer_id"] = customer_id
    return str(jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid, "alg": "RS256"}))


def _parse_sse(response_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data: list[str] = []

    for raw_line in response_text.splitlines():
        line = raw_line.strip()
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


def _assert_error_envelope(resp: Any, expected_status: int, expected_code: str) -> dict[str, Any]:
    assert resp.status_code == expected_status, (
        f"Expected {expected_status}, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert isinstance(body, dict)
    assert "error" in body
    err = body["error"]
    assert err["code"] == expected_code
    assert isinstance(err["message"], str) and len(err["message"]) > 0
    assert isinstance(err["request_id"], str) and len(err["request_id"]) > 0
    assert isinstance(err.get("details"), dict)
    return dict(body)


@pytest.fixture
def rsa_keys() -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    return _create_rsa_key_pair()


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(T0_BASE)


@pytest.fixture
def app_env(
    tmp_path: Path,
    rsa_keys: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "proposals_api_test.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()

    priv, _, jwk = rsa_keys
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: fixed_clock

    fixture_store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=fixed_clock)

    try:
        from sellershield_agent.api import proposals as proposals_api
    except ImportError:
        proposals_api = None  # type: ignore[assignment]

    if proposals_api is not None and hasattr(proposals_api, "get_proposal_store_client"):
        app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: fixture_store

    tokens = {
        "c001": _make_token(
            user_id="usr_c001", customer_id="C001", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "c002": _make_token(
            user_id="usr_c002", customer_id="C002", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "seller_staff": _make_token(
            user_id="usr_staff", customer_id=None, role="seller_staff", priv=priv, now_dt=T0_BASE
        ),
        "seller_owner": _make_token(
            user_id="usr_owner", customer_id=None, role="seller_owner", priv=priv, now_dt=T0_BASE
        ),
        "service": _make_token(
            user_id="svc_exec", customer_id=None, role="service", priv=priv, now_dt=T0_BASE
        ),
        "expired": _make_token(
            user_id="usr_c001",
            customer_id="C001",
            role="customer",
            priv=priv,
            now_dt=T0_BASE,
            exp_seconds=-120,
        ),
        "customer_no_cid": _make_token(
            user_id="usr_bad",
            customer_id=None,
            role="customer",
            priv=priv,
            now_dt=T0_BASE,
            include_customer_id_claim=False,
        ),
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "app": app,
            "client": client,
            "clock": fixed_clock,
            "store": fixture_store,
            "engine": get_engine(),
            "tokens": tokens,
            "priv": priv,
        }

    app.dependency_overrides.clear()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()
    reset_engine()
    get_settings.cache_clear()


async def _create_proposal_via_gateway(
    *,
    engine: Any,
    store: FixtureStoreClient,
    clock: FixedClock,
    customer_id: str,
    user_id: str,
    order_id: str,
    items: list[dict[str, Any]],
    reason_code: str = "changed_mind",
    reason_text: str = "Customer requested return",
    resolution: str = "refund",
    session_id: str = "ses_test_prop",
) -> dict[str, Any]:
    policy = load_policy("v3")
    gateway = ToolGateway(store_client=store, engine=engine, clock=clock, policy=policy)
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
            "resolution": resolution,
        },
        allowed={"propose_return", "propose_refund"},
        session_id=session_id,
    )
    assert res.ok is True and isinstance(res.data, dict)
    assert res.data.get("type") == "confirmation"
    return res.data


def _get_proposal_row(engine: Any, proposal_id: str) -> Any:
    with read_tx(engine) as conn:
        return conn.execute(
            select(proposals).where(proposals.c.proposal_id == proposal_id)
        ).fetchone()


# ── 1. Auth & Role Boundaries ────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_confirm_requires_auth_and_rejects_expired_or_malformed_jwt(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    card = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    pid = card["proposal_id"]
    phash = card["proposal_hash"]

    resp_no_auth = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Idempotency-Key": "idem-noauth"},
        json={"proposal_hash": phash},
    )
    _assert_error_envelope(resp_no_auth, 401, "unauthenticated")

    resp_expired = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={
            "Authorization": f"Bearer {app_env['tokens']['expired']}",
            "Idempotency-Key": "idem-expjwt",
        },
        json={"proposal_hash": phash},
    )
    _assert_error_envelope(resp_expired, 401, "unauthenticated")

    resp_no_cid = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={
            "Authorization": f"Bearer {app_env['tokens']['customer_no_cid']}",
            "Idempotency-Key": "idem-nocid",
        },
        json={"proposal_hash": phash},
    )
    _assert_error_envelope(resp_no_cid, 401, "unauthenticated")

    row = _get_proposal_row(engine, pid)
    assert row is not None and row.status == ProposalStatus.proposed.value


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("role_key", ["seller_staff", "seller_owner", "service"])
async def test_confirm_and_cancel_reject_non_customer_roles_403_and_audit(
    app_env: dict[str, Any],
    role_key: str,
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    card = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    pid = card["proposal_id"]
    token = app_env["tokens"][role_key]

    r_confirm = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"idem-role-c-{role_key}"},
        json={"proposal_hash": card["proposal_hash"]},
    )
    _assert_error_envelope(r_confirm, 403, "forbidden")

    r_cancel = client.post(
        f"/v1/proposals/{pid}/cancel",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"idem-role-x-{role_key}"},
        json={},
    )
    _assert_error_envelope(r_cancel, 403, "forbidden")

    with read_tx(engine) as conn:
        denials = conn.execute(
            select(audit_events).where(audit_events.c.event_type == "auth_denied")
        ).fetchall()
        assert len(denials) >= 2
        assert AuditService.verify_chain(conn).ok is True


# ── 2. IDOR & Uniform 404 (No Existence/Idempotency Oracle) ─────────────────


@pytest.mark.asyncio
@respx.mock
async def test_idor_confirm_and_cancel_uniform_404_and_no_idempotency_oracle(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]

    c001_card = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    c002_card = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    c001_pid = c001_card["proposal_id"]
    c002_pid = c002_card["proposal_id"]
    c002_tok = app_env["tokens"]["c002"]

    # First let C002 consume a legitimate idempotency key on C002's own proposal
    ok_resp = client.post(
        f"/v1/proposals/{c002_pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "c002-shared-key"},
        json={"proposal_hash": c002_card["proposal_hash"]},
    )
    assert ok_resp.status_code == 200

    # C002 tries to confirm nonexistent PRP and C001's PRP (with wrong hash, right hash,
    # and even reusing c002-shared-key). All must answer byte-identical 404 not_found.
    resp_missing = client.post(
        "/v1/proposals/PRP-00000000/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "idor-k-miss"},
        json={"proposal_hash": "a" * 64},
    )
    resp_foreign = client.post(
        f"/v1/proposals/{c001_pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "c002-shared-key"},
        json={"proposal_hash": c001_card["proposal_hash"]},
    )
    resp_cancel_missing = client.post(
        "/v1/proposals/PRP-00000000/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "idor-k-cmiss"},
        json={},
    )
    resp_cancel_foreign = client.post(
        f"/v1/proposals/{c001_pid}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "idor-k-cfor"},
        json={},
    )

    err_miss = _assert_error_envelope(resp_missing, 404, "not_found")
    err_for = _assert_error_envelope(resp_foreign, 404, "not_found")
    err_cmiss = _assert_error_envelope(resp_cancel_missing, 404, "not_found")
    err_cfor = _assert_error_envelope(resp_cancel_foreign, 404, "not_found")

    assert err_for["error"]["message"] == err_miss["error"]["message"]
    assert err_cfor["error"]["message"] == err_cmiss["error"]["message"]
    assert "C001" not in resp_foreign.text
    assert "C001" not in resp_cancel_foreign.text

    c001_row = _get_proposal_row(engine, c001_pid)
    assert c001_row is not None and c001_row.status == ProposalStatus.proposed.value
    assert c001_row.version == 1

    with read_tx(engine) as conn:
        denial_rows = conn.execute(
            select(audit_events).where(audit_events.c.event_type == "auth_denied")
        ).fetchall()
        assert len(denial_rows) >= 2
        for d in denial_rows:
            assert "C001" not in d.details_json
            assert c001_card["proposal_hash"] not in d.details_json
        assert AuditService.verify_chain(conn).ok is True


# ── 3. T05: Typed 'yes' & Chat Action Bypass Must Never Confirm ─────────────


@pytest.mark.asyncio
@respx.mock
async def test_typed_yes_and_confirm_text_in_chat_never_confirms_proposal(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    c002_tok = app_env["tokens"]["c002"]

    card = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    pid = card["proposal_id"]

    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {c002_tok}"})
    assert sess_resp.status_code == 200
    session_id = sess_resp.json()["session_id"]

    for idx, text_msg in enumerate(
        ["yes", "confirm", f"yes please confirm {pid}", "I confirm and approve this return"]
    ):
        chat_resp = client.post(
            f"/v1/chat/sessions/{session_id}/messages",
            headers={"Authorization": f"Bearer {c002_tok}"},
            json={"text": text_msg, "client_msg_id": f"typed_yes_{idx}"},
        )
        assert chat_resp.status_code == 200

    row = _get_proposal_row(engine, pid)
    assert row is not None
    assert row.status == ProposalStatus.proposed.value
    assert row.confirmed_at is None
    assert row.version == 1

    # Invalid guided action trying to invoke confirm_proposal through chat is rejected
    bad_action = client.post(
        f"/v1/chat/sessions/{session_id}/messages",
        headers={"Authorization": f"Bearer {c002_tok}"},
        json={
            "action": {"name": "confirm_proposal", "order_id": "ORD-2032"},
            "client_msg_id": "bad_action_confirm",
        },
    )
    _assert_error_envelope(bad_action, 422, "validation_error")


# ── 4. Hash Binding, DB Tampering Defense & Schema Strictness ───────────────


@pytest.mark.asyncio
@respx.mock
async def test_confirm_rejects_wrong_hash_tampered_db_and_extra_fields(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    c002_tok = app_env["tokens"]["c002"]

    card = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    pid = card["proposal_id"]
    real_hash = card["proposal_hash"]
    wrong_hash = ("0" if real_hash[0] != "0" else "1") + real_hash[1:]

    # Wrong 64-hex hash rejected
    resp_wrong = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "idem-wrong-hash"},
        json={"proposal_hash": wrong_hash},
    )
    _assert_error_envelope(resp_wrong, 409, "invalid_state")

    # Uppercase or non-hex hash rejected without confirming
    resp_upper = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "idem-upper-hash"},
        json={"proposal_hash": real_hash.upper()},
    )
    assert resp_upper.status_code in (400, 409, 422)

    # Extra forbidden body fields rejected (extra='forbid')
    resp_extra_c = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "idem-extra-c"},
        json={"proposal_hash": real_hash, "amount_paise": 999999},
    )
    _assert_error_envelope(resp_extra_c, 422, "validation_error")

    resp_extra_x = client.post(
        f"/v1/proposals/{pid}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "idem-extra-x"},
        json={"status": "succeeded"},
    )
    _assert_error_envelope(resp_extra_x, 422, "validation_error")

    # Stored-row amount tampering detection: altering amounts_json breaks canonical hash parity
    with write_tx(engine) as conn:
        tampered_amounts = json.dumps(
            {
                "fee_paise": 0,
                "items_paise": 99900,
                "shipping_refund_paise": 0,
                "total_paise": 99900,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            update(proposals)
            .where(proposals.c.proposal_id == pid)
            .values(amounts_json=tampered_amounts)
        )

    resp_tampered = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "idem-tampered-db"},
        json={"proposal_hash": real_hash},
    )
    _assert_error_envelope(resp_tampered, 409, "invalid_state")

    row = _get_proposal_row(engine, pid)
    assert row is not None and row.status == ProposalStatus.proposed.value


# ── 5. Expiry Boundary Under FixedClock ──────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_confirm_and_cancel_reject_at_exact_expiry_boundary(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]
    c002_tok = app_env["tokens"]["c002"]

    card_a = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    card_b = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-1", "qty": 1}],
    )

    # Advance simulated clock to exact 10-minute expiry boundary (still within 1h JWT expiry)
    clock.set(T0_BASE + timedelta(minutes=10))

    r_exp_confirm = client.post(
        f"/v1/proposals/{card_a['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "idem-exp-c"},
        json={"proposal_hash": card_a["proposal_hash"]},
    )
    _assert_error_envelope(r_exp_confirm, 409, "invalid_state")

    r_exp_cancel = client.post(
        f"/v1/proposals/{card_b['proposal_id']}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "idem-exp-x"},
        json={},
    )
    _assert_error_envelope(r_exp_cancel, 409, "invalid_state")

    row_a = _get_proposal_row(engine, card_a["proposal_id"])
    assert row_a is not None
    assert row_a.confirmed_at is None
    assert row_a.status in (ProposalStatus.proposed.value, ProposalStatus.expired.value)


# ── 6. Happy Path Confirm, Root Alias, Clock Advance, Revalidation & Overlap ─


@pytest.mark.asyncio
@respx.mock
async def test_confirm_valid_proposals_allow_and_needs_approval_after_clock_advance(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]

    card_allow = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
    )
    card_appr = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )

    # Normal clock advance within 10m window must succeed despite facts.now inside facts_hash
    clock.set(T0_BASE + timedelta(minutes=2))

    resp_allow = client.post(
        f"/v1/proposals/{card_allow['proposal_id']}/confirm",
        headers={
            "Authorization": f"Bearer {app_env['tokens']['c002']}",
            "Idempotency-Key": "k-sc05",
        },
        json={"proposal_hash": card_allow["proposal_hash"]},
    )
    assert resp_allow.status_code == 200, resp_allow.text
    body_allow = resp_allow.json()
    assert set(body_allow.keys()) == {
        "proposal_id",
        "status",
        "return_id",
        "approval_id",
        "message_key",
    }
    assert body_allow["proposal_id"] == card_allow["proposal_id"]
    assert body_allow["status"] in ("executing", "succeeded")
    assert isinstance(body_allow["message_key"], str) and len(body_allow["message_key"]) > 0

    # Also verify unprefixed root mount parity (/proposals/{id}/confirm)
    resp_appr = client.post(
        f"/proposals/{card_appr['proposal_id']}/confirm",
        headers={
            "Authorization": f"Bearer {app_env['tokens']['c001']}",
            "Idempotency-Key": "k-sc04",
        },
        json={"proposal_hash": card_appr["proposal_hash"]},
    )
    assert resp_appr.status_code == 200, resp_appr.text
    body_appr = resp_appr.json()
    assert body_appr["proposal_id"] == card_appr["proposal_id"]
    assert body_appr["status"] in ("executing", "pending_approval")

    row_allow = _get_proposal_row(engine, card_allow["proposal_id"])
    row_appr = _get_proposal_row(engine, card_appr["proposal_id"])
    assert row_allow is not None and row_allow.confirmed_at is not None and row_allow.version >= 2
    assert row_appr is not None and row_appr.confirmed_at is not None and row_appr.version >= 2

    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True


@pytest.mark.asyncio
@respx.mock
async def test_confirm_rejects_stale_proposal_when_store_changes_before_click(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]
    store: FixtureStoreClient = app_env["store"]

    card = await _create_proposal_via_gateway(
        engine=engine,
        store=store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
    )

    # Simulate another process/action creating an active return in the store before customer clicks
    c002_ctx = RequestContext(
        principal=Principal(
            user_id="usr_c002",
            role=Role.customer,
            customer_id="C002",
            auth_time=int(clock.now().timestamp()),
            jti="jti-store-race",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-store-race",
    )
    await store.create_return(
        c002_ctx,
        order_id="ORD-2032",
        items=[ReturnItem(item_id="ITEM-2032-1", qty=1)],
        reason_code=ReturnReasonCode.changed_mind.value,
        reason_text="Created externally before click",
        resolution=ReturnResolution.refund.value,
        idempotency_key="store-race-ret-1",
    )

    resp = client.post(
        f"/v1/proposals/{card['proposal_id']}/confirm",
        headers={
            "Authorization": f"Bearer {app_env['tokens']['c002']}",
            "Idempotency-Key": "k-stale",
        },
        json={"proposal_hash": card["proposal_hash"]},
    )
    _assert_error_envelope(resp, 409, "invalid_state")

    row = _get_proposal_row(engine, card["proposal_id"])
    assert row is not None and row.status == ProposalStatus.proposed.value
    assert row.confirmed_at is None


@pytest.mark.asyncio
@respx.mock
async def test_confirm_rejects_overlapping_second_proposal_for_same_order_item(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    clock: FixedClock = app_env["clock"]
    c002_tok = app_env["tokens"]["c002"]

    card_qty1 = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-1", "qty": 1}],
        reason_code="changed_mind",
    )
    card_qty2 = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-1", "qty": 2}],
        reason_code="changed_mind",
    )
    assert card_qty1["proposal_id"] != card_qty2["proposal_id"]

    r1 = client.post(
        f"/v1/proposals/{card_qty1['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-ov-1"},
        json={"proposal_hash": card_qty1["proposal_hash"]},
    )
    assert r1.status_code == 200

    # Confirming second overlapping proposal on same order/item must be rejected
    r2 = client.post(
        f"/v1/proposals/{card_qty2['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-ov-2"},
        json={"proposal_hash": card_qty2["proposal_hash"]},
    )
    _assert_error_envelope(r2, 409, "invalid_state")


# ── 7. Cancel Lifecycle & State Conflicts ────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_cancel_lifecycle_empty_or_json_body_and_state_conflicts(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    c002_tok = app_env["tokens"]["c002"]

    card_cancel_empty = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    card_confirmed = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-1", "qty": 1}],
    )

    # Cancel with completely empty body works (OpenAPI requestBody required: false)
    r_cancel = client.post(
        f"/v1/proposals/{card_cancel_empty['proposal_id']}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-cancel-empty"},
    )
    assert r_cancel.status_code == 200, r_cancel.text
    assert r_cancel.json() == {
        "proposal_id": card_cancel_empty["proposal_id"],
        "status": "cancelled",
    }

    # Same Idempotency-Key on cancel replays 200
    r_cancel_replay = client.post(
        f"/v1/proposals/{card_cancel_empty['proposal_id']}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-cancel-empty"},
        json={},
    )
    assert r_cancel_replay.status_code == 200
    assert r_cancel_replay.json() == r_cancel.json()

    # Confirm after cancel -> 409 invalid_state
    r_conf_after = client.post(
        f"/v1/proposals/{card_cancel_empty['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-conf-after-x"},
        json={"proposal_hash": card_cancel_empty["proposal_hash"]},
    )
    _assert_error_envelope(r_conf_after, 409, "invalid_state")

    # Confirm then cancel with a new key -> 409 invalid_state
    r_conf = client.post(
        f"/v1/proposals/{card_confirmed['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-conf-then-x-1"},
        json={"proposal_hash": card_confirmed["proposal_hash"]},
    )
    assert r_conf.status_code == 200

    r_cancel_after = client.post(
        f"/v1/proposals/{card_confirmed['proposal_id']}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-conf-then-x-2"},
        json={},
    )
    _assert_error_envelope(r_cancel_after, 409, "invalid_state")


# ── 8. Idempotency-Key Header Validation, Replay & Mismatch ─────────────────


@pytest.mark.asyncio
@respx.mock
async def test_idempotency_key_validation_replay_and_mismatch(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    c002_tok = app_env["tokens"]["c002"]

    card1 = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    card2 = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-1", "qty": 1}],
    )

    # Missing, blank, overlong, and duplicate Idempotency-Key headers rejected
    r_missing = client.post(
        f"/v1/proposals/{card1['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}"},
        json={"proposal_hash": card1["proposal_hash"]},
    )
    assert r_missing.status_code in (400, 422)
    assert r_missing.json()["error"]["code"] == "validation_error"

    r_blank = client.post(
        f"/v1/proposals/{card1['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "   "},
        json={"proposal_hash": card1["proposal_hash"]},
    )
    assert r_blank.status_code in (400, 422)
    assert r_blank.json()["error"]["code"] == "validation_error"

    r_long = client.post(
        f"/v1/proposals/{card1['proposal_id']}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "x" * 129},
        json={},
    )
    assert r_long.status_code in (400, 422)
    assert r_long.json()["error"]["code"] == "validation_error"

    # Confirm card1 with key "shared-idem"
    r1 = client.post(
        f"/v1/proposals/{card1['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "shared-idem"},
        json={"proposal_hash": card1["proposal_hash"]},
    )
    assert r1.status_code == 200

    # Replaying exact same request replays identical 200 response without version bump
    v_after_first = _get_proposal_row(engine, card1["proposal_id"]).version
    r1_retry = client.post(
        f"/v1/proposals/{card1['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "shared-idem"},
        json={"proposal_hash": card1["proposal_hash"]},
    )
    assert r1_retry.status_code == 200
    assert r1_retry.json() == r1.json()
    assert _get_proposal_row(engine, card1["proposal_id"]).version == v_after_first

    # Reusing "shared-idem" with different hash -> 409 idempotency_mismatch
    bad_hash = ("f" if card1["proposal_hash"][0] != "f" else "e") + card1["proposal_hash"][1:]
    r_diff_hash = client.post(
        f"/v1/proposals/{card1['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "shared-idem"},
        json={"proposal_hash": bad_hash},
    )
    _assert_error_envelope(r_diff_hash, 409, "idempotency_mismatch")

    # Reusing "shared-idem" on another owned proposal -> 409 idempotency_mismatch
    r_diff_prop = client.post(
        f"/v1/proposals/{card2['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "shared-idem"},
        json={"proposal_hash": card2["proposal_hash"]},
    )
    _assert_error_envelope(r_diff_prop, 409, "idempotency_mismatch")

    # Reusing "shared-idem" on /cancel -> 409 idempotency_mismatch
    r_diff_endpoint = client.post(
        f"/v1/proposals/{card1['proposal_id']}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "shared-idem"},
        json={},
    )
    _assert_error_envelope(r_diff_endpoint, 409, "idempotency_mismatch")

    # Duplicate Idempotency-Key headers rejected cleanly
    r_dup_hdr = client.post(
        f"/v1/proposals/{card2['proposal_id']}/cancel",
        headers=[
            ("Authorization", f"Bearer {c002_tok}"),
            ("Idempotency-Key", "dup-hdr-1"),
            ("Idempotency-Key", "dup-hdr-2"),
        ],
        json={},
    )
    assert r_dup_hdr.status_code in (400, 422)
    assert r_dup_hdr.json()["error"]["code"] == "validation_error"

    # Tenant isolation: C001 can independently use the exact same key string "shared-idem"
    c001_card = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    r_c001_same_key = client.post(
        f"/v1/proposals/{c001_card['proposal_id']}/confirm",
        headers={
            "Authorization": f"Bearer {app_env['tokens']['c001']}",
            "Idempotency-Key": "shared-idem",
        },
        json={"proposal_hash": c001_card["proposal_hash"]},
    )
    assert r_c001_same_key.status_code == 200, r_c001_same_key.text


# ── 9. Concurrency Race & Audit Fail-Closed Atomicity (T14, T20) ─────────────


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_confirm_distinct_keys_single_winner(
    app_env: dict[str, Any],
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    c002_tok = app_env["tokens"]["c002"]

    card = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    pid = card["proposal_id"]
    phash = card["proposal_hash"]

    def _fire(key: str) -> Any:
        return client.post(
            f"/v1/proposals/{pid}/confirm",
            headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": key},
            json={"proposal_hash": phash},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(_fire, "race-key-1")
        fut2 = pool.submit(_fire, "race-key-2")
        resps = [fut1.result(), fut2.result()]

    statuses = sorted(r.status_code for r in resps)
    assert statuses == [200, 409], [(r.status_code, r.text) for r in resps]
    loser = next(r for r in resps if r.status_code == 409)
    assert loser.json()["error"]["code"] == "invalid_state"

    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True


@pytest.mark.asyncio
@respx.mock
async def test_audit_failure_rolls_back_proposal_confirmation(
    app_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client: TestClient = app_env["client"]
    engine = app_env["engine"]
    c002_tok = app_env["tokens"]["c002"]

    card = await _create_proposal_via_gateway(
        engine=engine,
        store=app_env["store"],
        clock=app_env["clock"],
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    pid = card["proposal_id"]

    def _boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("simulated audit_fail")

    monkeypatch.setattr(AuditService, "append", classmethod(_boom))

    resp = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-audit-fail"},
        json={"proposal_hash": card["proposal_hash"]},
    )
    assert resp.status_code >= 500

    row = _get_proposal_row(engine, pid)
    assert row is not None
    assert row.status == ProposalStatus.proposed.value
    assert row.confirmed_at is None
    assert row.version == 1


# ── 10. Card Builders, XSS Stripping & Pipeline SSE / Verifier Integration ───


def test_confirmation_and_return_preview_card_builders_xss_and_hash_parity() -> None:
    from sellershield_agent.orchestrator.cards import (
        build_confirmation_card,
        build_return_preview_card,
    )

    sample_hash = "ab" * 32
    raw_confirmation = {
        "type": "confirmation",
        "proposal_id": "PRP-deadbeef",
        "proposal_hash": sample_hash,
        "proposal_type": "return",
        "order_id": "ORD-1042",
        "items": [
            {
                "item_id": "ITEM-1042-1",
                "quantity": 1,
                "name": "<script>alert(1)</script>Cookware Set",
            }
        ],
        "amounts": {
            "items_paise": 120000,
            "shipping_refund_paise": 0,
            "fee_paise": 0,
            "total_paise": 120000,
            "display": "₹1,200",
        },
        "method": "original",
        "resolution": "refund",
        "decision": "needs_approval",
        "policy_version": "v3",
        "capped": False,
        "expires_at": "2026-11-08T12:10:00Z",
        "review_statement": (
            "The seller will review your request. A decision is expected within 24 hours."
        ),
    }

    conf_card = build_confirmation_card(raw_confirmation)
    assert conf_card["card_type"] == "confirmation"
    payload = conf_card["payload"]
    assert payload["proposal_id"] == "PRP-deadbeef"
    assert payload["proposal_hash"] == sample_hash
    assert payload["expires_at"] == "2026-11-08T12:10:00Z"
    assert payload["amounts"]["total_paise"] == 120000
    assert "<script>" not in json.dumps(conf_card).lower()
    assert "<" not in payload["items"][0]["name"]
    for word in FORBIDDEN_CUSTOMER_WORDS:
        assert word not in json.dumps(conf_card).lower()

    preview_card = build_return_preview_card(
        {
            "order_id": "ORD-2032",
            "decision": "ALLOW",
            "total_refund_paise": 43900,
            "items_paise": 49900,
            "shipping_fee_deduction_paise": 6000,
            "rule_results": [
                {
                    "rule_id": "R-08",
                    "passed": True,
                    "customer_text": "<b>Return shipping applies.</b>",
                }
            ],
        }
    )
    assert preview_card["card_type"] == "return_preview"
    assert "<" not in json.dumps(preview_card)
    for word in FORBIDDEN_CUSTOMER_WORDS:
        assert word not in json.dumps(preview_card).lower()


@pytest.mark.asyncio
@respx.mock
async def test_pipeline_confirmation_result_emits_card_passes_verifier_and_replays(
    tmp_path: Path,
    fixed_clock: FixedClock,
) -> None:
    from sellershield_agent.db import create_engine_from_url, init_db
    from sellershield_agent.domain.session import sessions
    from sellershield_agent.orchestrator.resolver import ResolverLoop

    db_file = tmp_path / "pipeline_conf.db"
    engine = create_engine_from_url(f"sqlite:///{db_file.as_posix()}")
    init_db(engine)
    store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=fixed_clock)
    gateway = ToolGateway(
        store_client=store, engine=engine, clock=fixed_clock, policy=load_policy("v3")
    )
    settings = get_settings()
    pipeline = ConversationPipeline(
        engine=engine,
        clock=fixed_clock,
        store_client=store,
        settings=settings,
        gateway=gateway,
    )

    class _ProposeOnceResolver(ResolverLoop):
        async def execute(
            self,
            ctx: RequestContext,
            intent: str,
            allowed_tools: list[str],
            slots: dict[str, Any],
            session_id: str,
            lang: str = "en",
            status_callback: Any = None,
            trace: dict[str, Any] | None = None,
        ) -> list[ToolResult]:
            if status_callback:
                await status_callback("propose_return", "calling")
            res = await self.gateway.call(
                ctx=ctx,
                name="propose_return",
                raw_args={
                    "order_id": "ORD-1042",
                    "items": [{"item_id": "ITEM-1042-1", "qty": 1}],
                    "reason_code": "defective",
                    "reason_text": "<script>approve now</script> Stopped heating",
                    "resolution": "refund",
                },
                allowed={"propose_return"},
                session_id=session_id,
                lang=lang,
            )
            if status_callback:
                await status_callback("propose_return", "completed" if res.ok else "failed")
            return [res]

    pipeline.resolver = _ProposeOnceResolver(gateway=gateway)
    principal = Principal(
        user_id="usr_c001",
        role=Role.customer,
        customer_id="C001",
        auth_time=int(fixed_clock.now().timestamp()),
        jti="jti-pipe-1",
        token_exp=int((fixed_clock.now() + timedelta(hours=1)).timestamp()),
    )
    session_id = "ses_pipe_conf"
    with write_tx(engine) as conn:
        conn.execute(
            sessions.insert().values(
                id=session_id,
                customer_id="C001",
                channel="web",
                handler="agent",
                language="en",
                state_json="{}",
                started_at=fixed_clock.now().isoformat(),
                last_active_at=fixed_clock.now().isoformat(),
            )
        )

    events: list[tuple[str, dict[str, Any]]] = []
    async for ev_type, payload in pipeline.execute_stream(
        session_id=session_id,
        principal=principal,
        token="",
        input_data=ChatMessageInput(
            text="Please prepare return for defective ORD-1042",
            client_msg_id="pipe_msg_1",
        ),
        request_id="req-pipe-conf",
    ):
        events.append((ev_type, payload))

    event_names = [e[0] for e in events]
    assert "card" in event_names
    assert "final" in event_names

    card_ev = next(
        p for name, p in events if name == "card" and p.get("card_type") == "confirmation"
    )
    row = _get_proposal_row(engine, card_ev["payload"]["proposal_id"])
    assert row is not None
    assert card_ev["payload"]["proposal_hash"] == row.proposal_hash
    assert card_ev["payload"]["decision"] == "needs_approval"
    assert "seller" in card_ev["payload"]["review_statement"].lower()

    full_text = next(p["full_text"] for name, p in events if name == "final")
    sentence_count = len([s for s in full_text.split(".") if s.strip()])
    assert sentence_count <= 2
    assert "created" not in full_text.lower()
    assert "approved" not in full_text.lower()
    assert "refunded" not in full_text.lower()
    for word in FORBIDDEN_CUSTOMER_WORDS:
        assert word not in full_text.lower()

    # Duplicate client_msg_id replays the exact same confirmation card without re-running tools
    replay_events: list[tuple[str, dict[str, Any]]] = []
    async for ev_type, payload in pipeline.execute_stream(
        session_id=session_id,
        principal=principal,
        token="",
        input_data=ChatMessageInput(
            text="Please prepare return for defective ORD-1042",
            client_msg_id="pipe_msg_1",
        ),
        request_id="req-pipe-conf-replay",
    ):
        replay_events.append((ev_type, payload))

    replay_cards = [p for name, p in replay_events if name == "card"]
    assert len(replay_cards) == 1
    assert replay_cards[0]["card_type"] == "confirmation"
    assert replay_cards[0]["payload"]["proposal_hash"] == row.proposal_hash

    # Unbacked claim check on FactSheet built from propose_return ToolResult
    propose_res = await gateway.call(
        ctx=RequestContext(principal=principal, request_id="req-sheet"),
        name="propose_return",
        raw_args={
            "order_id": "ORD-1042",
            "items": [{"item_id": "ITEM-1042-1", "qty": 1}],
            "reason_code": "defective",
            "resolution": "refund",
        },
        allowed={"propose_return"},
        session_id=session_id,
    )
    sheet = build_factsheet([propose_res], language="en", clock=fixed_clock)
    assert verify(full_text, sheet, "en").ok is True
    assert verify("Done. Your return is created and approved for ₹1,200.", sheet, "en").ok is False

    engine.dispose()
