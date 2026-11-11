"""Regression tests for BUG-019 (pre-dispatch cancellation), Test 2.5 (no-ID refund scan),
and full Hinglish / Hindi Devanagari understanding and verifier-safe generation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr
from starlette.testclient import TestClient

from sellershield_agent.api import chat as chat_api
from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, init_db, reset_engine
from sellershield_agent.domain.models import ReturnItem, ReturnReasonCode
from sellershield_agent.main import create_app
from sellershield_agent.orchestrator.resolver import _parse_qty_from_text
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.store.client import RequestContext, StoreValidationError
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.understand.router import DeterministicRouter, extract_order_ids
from sellershield_agent.understand.schema import Intent, Language
from sellershield_agent.understand.service import UnderstandContext

T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"


def _create_rsa_key_pair(
    kid: str = "bt1-key",
) -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return priv, pub, jwk_dict


class _FakeJWKSClient:
    def __init__(self, jwk: dict[str, Any]) -> None:
        self._pyjwk = jwt.PyJWK.from_dict(jwk)

    def get_signing_key(self, kid: str) -> Any:
        return self._pyjwk

    def is_healthy(self) -> bool:
        return True


def _make_token(
    user_id: str,
    role: str,
    priv_key: RSAPrivateKey,
    kid: str = "bt1-key",
    customer_id: str | None = None,
    email: str = "test@example.com",
) -> str:
    payload: dict[str, Any] = {
        "jti": f"token-{uuid.uuid4().hex[:8]}",
        "sub": user_id,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "iat": int(T0_BASE.timestamp()),
        "exp": int((T0_BASE + timedelta(hours=2)).timestamp()),
        "auth_time": int(T0_BASE.timestamp()),
        "role": role,
        "email": email,
    }
    if customer_id is not None:
        payload["customer_id"] = customer_id
    return str(jwt.encode(payload, priv_key, algorithm="RS256", headers={"kid": kid}))


def _parse_sse(stream_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cur_ev: str | None = None
    cur_data: list[str] = []
    for raw_line in stream_text.splitlines():
        line = raw_line.strip()
        if not line:
            if cur_ev and cur_data:
                txt = "\n".join(cur_data)
                try:
                    parsed: Any = json.loads(txt)
                except Exception:
                    parsed = txt
                events.append({"event": cur_ev, "data": parsed})
            cur_ev = None
            cur_data = []
            continue
        if line.startswith("event:"):
            cur_ev = line[len("event:") :].strip()
        elif line.startswith("data:"):
            cur_data.append(line[len("data:") :].strip())
    if cur_ev and cur_data:
        txt = "\n".join(cur_data)
        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = txt
        events.append({"event": cur_ev, "data": parsed})
    return events


def _find_confirmation_card(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract inner payload of an emitted confirmation SSE card."""
    for ev in events:
        if ev.get("event") == "card" and isinstance(ev.get("data"), dict):
            data = ev["data"]
            if data.get("card_type") == "confirmation" or data.get("type") == "confirmation":
                payload = data.get("payload")
                return payload if isinstance(payload, dict) else data
    return None


@pytest.fixture
def bt1_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "bt1_test.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("STORE_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("STORE_JWKS_URL", "http://localhost:8000/.well-known/jwks.json")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    for key in (
        "LLM_PRIMARY_API_KEY",
        "LLM_PRIMARY_BASE_URL",
        "LLM_PRIMARY_MODEL",
        "LLM_SECONDARY_API_KEY",
        "LLM_SECONDARY_BASE_URL",
        "LLM_SECONDARY_MODEL",
        "LLM_TERTIARY_API_KEY",
        "LLM_TERTIARY_BASE_URL",
        "LLM_TERTIARY_MODEL",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    user_message_limiter.reset()
    reset_default_jwks_client()

    priv, _, jwk = _create_rsa_key_pair()
    fake_jwks = _FakeJWKSClient(jwk)
    fixed_clock = FixedClock(T0_BASE)
    store = FixtureStoreClient(clock=fixed_clock)

    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: fixed_clock
    app.dependency_overrides[deps.get_jwks_client] = lambda: fake_jwks
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: store
    app.dependency_overrides[chat_api.get_store_client] = lambda: store

    engine = get_engine()
    init_db(engine)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "client": client,
            "priv": priv,
            "store": store,
            "engine": engine,
            "clock": fixed_clock,
        }

    app.dependency_overrides.clear()
    reset_chat_pipeline()
    reset_engine()
    get_settings.cache_clear()


def test_router_hinglish_and_hindi_classification_and_devanagari_ids() -> None:
    """Router accurately handles Hinglish, Devanagari Hindi, and Devanagari order IDs."""
    router = DeterministicRouter()
    ctx = UnderstandContext(session_id="SES-HI", customer_id="C001")

    assert extract_order_ids("मेरा ऑर्डर ORD-१०४८ कहाँ पहुँचा?") == ["ORD-1048"]
    assert extract_order_ids("ऑर्डर १०४८ का स्टेटस बताओ") == ["ORD-1048"]

    # Hinglish order / refund / return / cancel / human / policy
    r_ord = router.route("Mera parcel kab aayega ORD-1048 ka?", ctx)
    assert r_ord.intent == Intent.ORDER_STATUS
    assert r_ord.language == Language.HINGLISH

    r_rfd_st = router.route("mera refund kahan tak pahuncha?", ctx)
    assert r_rfd_st.intent == Intent.REFUND_STATUS
    assert r_rfd_st.language == Language.HINGLISH

    r_paise_st = router.route("mere paise kab tak wapas aayenge?", ctx)
    assert r_paise_st.intent == Intent.REFUND_STATUS
    assert r_paise_st.language == Language.HINGLISH

    r_list = router.route("mere saare orders dikhao", ctx)
    assert r_list.intent == Intent.ORDER_STATUS
    assert r_list.language == Language.HINGLISH

    r_cxl_krdo = router.route("cancel krdo ORD-4021", ctx)
    assert r_cxl_krdo.intent == Intent.CANCELLATION
    assert r_cxl_krdo.order_ids == ["ORD-4021"]

    r_rfd_req = router.route("Mera paisa wapas karo order ORD-8811 deliver nahi hua.", ctx)
    assert r_rfd_req.intent == Intent.REFUND_REQUEST
    assert r_rfd_req.language == Language.HINGLISH

    r_cxl = router.route("Cancel kardo mera order ORD-4021, ab nahi chahiye.", ctx)
    assert r_cxl.intent == Intent.CANCELLATION
    assert r_cxl.order_ids == ["ORD-4021"]
    assert r_cxl.language == Language.HINGLISH

    r_ret = router.route("ek kurta wapas karo ORD-2031 me defect hai", ctx)
    assert r_ret.intent == Intent.RETURN_REQUEST
    assert r_ret.order_ids == ["ORD-2031"]
    assert r_ret.language == Language.HINGLISH

    # Golden INT-12 ("returned order" + "refund") and INT-07 ("cracked and shattered")
    r_int12 = router.route(
        "When will my refund for returned order ORD-1102 be credited to my bank?",
        ctx,
    )
    assert r_int12.intent == Intent.REFUND_STATUS
    r_int07 = router.route(
        "The screen on the phone in ORD-8812 is cracked and completely shattered.",
        ctx,
    )
    assert r_int07.intent == Intent.DAMAGED_ITEM

    # Devanagari Hindi
    hi_track = router.route("मेरा ऑर्डर ORD-१०४८ कहाँ पहुँचा?", ctx)
    assert hi_track.language == Language.HI
    assert hi_track.intent == Intent.ORDER_STATUS
    assert hi_track.order_ids == ["ORD-1048"]

    hi_cancel = router.route("ऑर्डर ORD-4021 रद्द कर दो", ctx)
    assert hi_cancel.language == Language.HI
    assert hi_cancel.intent == Intent.CANCELLATION
    assert hi_cancel.order_ids == ["ORD-4021"]

    hi_rfd = router.route("मेरा रिफंड कब मिलेगा?", ctx)
    assert hi_rfd.language == Language.HI
    assert hi_rfd.intent == Intent.REFUND_STATUS


def test_hinglish_hindi_quantity_parsing_safety() -> None:
    """Partial quantity parser supports Hinglish/Hindi numerals without false triggering on 'do'."""
    assert _parse_qty_from_text("ek kurta return karna hai") == 1
    assert _parse_qty_from_text("do kurta wapas karo") == 2
    assert _parse_qty_from_text("तीन आइटम वापस करें") == 3
    assert _parse_qty_from_text("cancel kar do order please") is None
    assert _parse_qty_from_text("do you have my refund status") is None


def test_bug019_pre_dispatch_cancel_auto_allow_and_confirm(bt1_env: dict[str, Any]) -> None:
    """BUG-019: Meera Iyer cancels placed ORD-4021 end-to-end via chat proposal + Confirm."""
    client: TestClient = bt1_env["client"]
    priv: RSAPrivateKey = bt1_env["priv"]
    store: FixtureStoreClient = bt1_env["store"]
    tok = _make_token(
        "usr_c004",
        "customer",
        priv,
        customer_id="C004",
        email="meera.iyer@example.test",
    )

    sess = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {tok}"}).json()
    sid = sess["session_id"]

    # Force router/deterministic path by disabling external LLM calls if any, using standard flow
    resp = client.post(
        f"/v1/chat/sessions/{sid}/messages",
        headers={"Authorization": f"Bearer {tok}"},
        json={"text": "cancel kardo mera order ORD-4021", "client_msg_id": "msg-cxl-4021"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final_ev = next(e for e in events if e["event"] == "final")
    assert "not available in this build" not in final_ev["data"]["full_text"].lower()

    conf_card = _find_confirmation_card(events)
    assert conf_card is not None
    assert conf_card["proposal_type"] == "cancel"
    pid = conf_card["proposal_id"]
    phash = conf_card["proposal_hash"]

    # Store must still have placed status before customer Confirm
    svc_ctx = RequestContext(
        principal=Principal(
            user_id="svc-test",
            role=Role.service,
            jti="bt1-svc-pre",
            token_exp=int(T0_BASE.timestamp()) + 7200,
        ),
        request_id="req-chk-pre",
        token=SecretStr("x"),
    )
    pre_order = _run_async(store.get_order_admin(svc_ctx, "ORD-4021"))
    assert pre_order.status.value == "placed"

    # Confirm executes single-step cancellation
    conf_resp = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={
            "Authorization": f"Bearer {tok}",
            "Idempotency-Key": f"key-{pid}",
        },
        json={"proposal_hash": phash},
    )
    assert conf_resp.status_code == 200
    conf_body = conf_resp.json()
    assert conf_body["status"] == "succeeded"

    post_order = _run_async(store.get_order_admin(svc_ctx, "ORD-4021"))
    assert post_order.status.value == "cancelled"


def test_bug019_pre_dispatch_cancel_needs_approval_and_seller_approve(
    bt1_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High-value pre-dispatch cancellation needing seller approval works through seller Approve."""
    client: TestClient = bt1_env["client"]
    priv: RSAPrivateKey = bt1_env["priv"]
    store: FixtureStoreClient = bt1_env["store"]

    # Temporarily lower pre_dispatch_cancel_auto_limit_paise across pipeline & revalidation
    strict_policy = load_policy("v3").model_copy(
        update={"pre_dispatch_cancel_auto_limit_paise": 50000}
    )
    monkeypatch.setattr(
        "sellershield_agent.policy.loader.load_policy",
        lambda *_a, **_kw: strict_policy,
    )
    monkeypatch.setattr(
        "sellershield_agent.orchestrator.pipeline.load_policy",
        lambda *_a, **_kw: strict_policy,
    )
    monkeypatch.setattr(
        "sellershield_agent.proposals.revalidate.load_policy",
        lambda *_a, **_kw: strict_policy,
    )
    monkeypatch.setattr(
        "sellershield_agent.approvals.revalidation.policy_loader.load_policy",
        lambda *_a, **_kw: strict_policy,
    )

    c004_tok = _make_token(
        "usr_c004", "customer", priv, customer_id="C004", email="meera@example.test"
    )
    seller_tok = _make_token("S002", "seller_staff", priv, email="kiran.rao@nestkart.test")

    sid = client.post(
        "/v1/chat/sessions", headers={"Authorization": f"Bearer {c004_tok}"}
    ).json()["session_id"]

    msg_resp = client.post(
        f"/v1/chat/sessions/{sid}/messages",
        headers={"Authorization": f"Bearer {c004_tok}"},
        json={"text": "please cancel ORD-4021", "client_msg_id": "msg-cxl-apr"},
    )
    assert msg_resp.status_code == 200
    events = _parse_sse(msg_resp.text)
    conf_card = _find_confirmation_card(events)
    assert conf_card is not None
    assert conf_card["decision"] == "needs_approval"
    pid = conf_card["proposal_id"]

    conf_resp = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={
            "Authorization": f"Bearer {c004_tok}",
            "Idempotency-Key": f"key-apr-{pid}",
        },
        json={"proposal_hash": conf_card["proposal_hash"]},
    )
    assert conf_resp.status_code == 200
    conf_body = conf_resp.json()
    assert conf_body["status"] == "pending_approval"
    aid = conf_body["approval_id"]
    assert aid is not None

    # Fetch seller approval card and approve
    card_resp = client.get(
        f"/v1/seller/approvals/{aid}",
        headers={"Authorization": f"Bearer {seller_tok}"},
    )
    assert card_resp.status_code == 200
    ver = card_resp.json()["expected_version"]

    apr_resp = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers={
            "Authorization": f"Bearer {seller_tok}",
            "Idempotency-Key": f"apr-exec-{aid}",
        },
        json={
            "decision": "approve",
            "expected_version": ver,
            "reason": "Verified pre-dispatch cancel",
        },
    )
    assert apr_resp.status_code == 200
    assert apr_resp.json()["status"] == "approved"

    svc_ctx = RequestContext(
        principal=Principal(
            user_id="svc-test",
            role=Role.service,
            jti="bt1-svc-apr",
            token_exp=int(T0_BASE.timestamp()) + 7200,
        ),
        request_id="req-chk-apr",
        token=SecretStr("x"),
    )
    post_order = _run_async(store.get_order_admin(svc_ctx, "ORD-4021"))
    assert post_order.status.value == "cancelled"


def test_bug019_shipped_order_cancel_denied_and_passes_verifier(bt1_env: dict[str, Any]) -> None:
    """Cancelling already-shipped ORD-1048 returns a truthful verifier-clean denial."""
    client: TestClient = bt1_env["client"]
    priv: RSAPrivateKey = bt1_env["priv"]
    tok = _make_token("usr_c001", "customer", priv, customer_id="C001", email="aarav@example.test")

    sid = client.post(
        "/v1/chat/sessions", headers={"Authorization": f"Bearer {tok}"}
    ).json()["session_id"]
    resp = client.post(
        f"/v1/chat/sessions/{sid}/messages",
        headers={"Authorization": f"Bearer {tok}"},
        json={"text": "cancel ORD-1048", "client_msg_id": "msg-cxl-denied"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    full_text = next(e["data"]["full_text"] for e in events if e["event"] == "final")
    assert "ORD-1048" in full_text
    assert "not eligible for cancellation" in full_text
    assert _find_confirmation_card(events) is None


def test_2_5_no_id_refund_inquiry_scans_all_orders_never_guesses_ord1051(
    bt1_env: dict[str, Any],
) -> None:
    """Test 2.5: Asking about refund without order ID scans orders; never defaults to ORD-1051."""
    client: TestClient = bt1_env["client"]
    priv: RSAPrivateKey = bt1_env["priv"]
    store: FixtureStoreClient = bt1_env["store"]
    tok = _make_token("usr_c001", "customer", priv, customer_id="C001", email="aarav@example.test")

    sid = client.post(
        "/v1/chat/sessions", headers={"Authorization": f"Bearer {tok}"}
    ).json()["session_id"]

    # 1. With no refunds in store, never blames arbitrary ORD-1051
    resp_empty = client.post(
        f"/v1/chat/sessions/{sid}/messages",
        headers={"Authorization": f"Bearer {tok}"},
        json={"text": "Where is my refund?", "client_msg_id": "msg-rfd-none"},
    )
    assert resp_empty.status_code == 200
    ev_empty = _parse_sse(resp_empty.text)
    txt_empty = next(e["data"]["full_text"] for e in ev_empty if e["event"] == "final")
    assert "ORD-1051" not in txt_empty
    assert "No active refunds were found across your recent orders." in txt_empty

    # 2. Seed an active return + refund reservation on ORD-1042, then ask in Hinglish
    c001_ctx = RequestContext(
        principal=Principal(
            user_id="usr_c001",
            role=Role.customer,
            customer_id="C001",
            jti="bt1-c001-seed",
            token_exp=int(T0_BASE.timestamp()) + 7200,
        ),
        request_id="req-seed-rfd",
        token=SecretStr("x"),
    )
    svc_ctx = RequestContext(
        principal=Principal(
            user_id="svc-test",
            role=Role.service,
            jti="bt1-svc-seed",
            token_exp=int(T0_BASE.timestamp()) + 7200,
        ),
        request_id="req-seed-rfd-svc",
        token=SecretStr("x"),
    )
    ord_1042 = _run_async(store.get_order(c001_ctx, "ORD-1042"))
    ret = _run_async(
        store.create_return(
            svc_ctx,
            "ORD-1042",
            [ReturnItem(item_id=ord_1042.items[0].item_id, qty=1)],
            ReturnReasonCode.defective,
            "Defective item",
            "refund",
            idempotency_key="bt1-ret-1042",
        )
    )
    reserved_paise = min(ord_1042.total_paise, 120000)
    _run_async(
        store.reserve_refund(
            svc_ctx,
            "ORD-1042",
            reserved_paise,
            ret.return_id,
            "original",
            idempotency_key="bt1-rfd-1042",
        )
    )

    resp_found = client.post(
        f"/v1/chat/sessions/{sid}/messages",
        headers={"Authorization": f"Bearer {tok}"},
        json={"text": "mere paise kab tak wapas aayenge?", "client_msg_id": "msg-rfd-hi"},
    )
    assert resp_found.status_code == 200
    ev_found = _parse_sse(resp_found.text)
    txt_found = next(e["data"]["full_text"] for e in ev_found if e["event"] == "final")
    assert "ORD-1042" in txt_found
    assert "ORD-1051" not in txt_found
    expected_rupees = f"{reserved_paise // 100:,}"
    assert expected_rupees in txt_found or f"{reserved_paise / 100:.2f}" in txt_found


def test_hindi_devanagari_e2e_pipeline_passes_verifier(bt1_env: dict[str, Any]) -> None:
    """Devanagari Hindi query with Devanagari digits ('ORD-१०४८') translates order and replies."""
    client: TestClient = bt1_env["client"]
    priv: RSAPrivateKey = bt1_env["priv"]
    tok = _make_token("usr_c001", "customer", priv, customer_id="C001", email="aarav@example.test")

    sid = client.post(
        "/v1/chat/sessions", headers={"Authorization": f"Bearer {tok}"}
    ).json()["session_id"]

    resp = client.post(
        f"/v1/chat/sessions/{sid}/messages",
        headers={"Authorization": f"Bearer {tok}"},
        json={"text": "मेरा ऑर्डर ORD-१०४८ कहाँ पहुँचा?", "client_msg_id": "msg-hi-1048"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    full_text = next(e["data"]["full_text"] for e in events if e["event"] == "final")
    assert "ORD-1048" in full_text
    assert any("\u0900" <= ch <= "\u097f" for ch in full_text)


def test_bug020_seller_approval_queue_survives_http_refundable_400(
    bt1_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG-020: Pending approval card & approve succeed when store /refundable returns 400."""
    client: TestClient = bt1_env["client"]
    priv: RSAPrivateKey = bt1_env["priv"]
    store: FixtureStoreClient = bt1_env["store"]
    c001_tok = _make_token(
        "usr_c001", "customer", priv, customer_id="C001", email="aarav@example.test"
    )
    owner_tok = _make_token(
        "S001", "seller_owner", priv, email="priya.sharma@nestkart.test"
    )

    sid = client.post(
        "/v1/chat/sessions", headers={"Authorization": f"Bearer {c001_tok}"}
    ).json()["session_id"]

    msg_resp = client.post(
        f"/v1/chat/sessions/{sid}/messages",
        headers={"Authorization": f"Bearer {c001_tok}"},
        json={
            "text": "My cookware set in ORD-1042 is defective, I want to return it",
            "client_msg_id": "msg-bug020-1042",
        },
    )
    assert msg_resp.status_code == 200
    conf_card = _find_confirmation_card(_parse_sse(msg_resp.text))
    assert conf_card is not None
    pid = conf_card["proposal_id"]

    conf_resp = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={
            "Authorization": f"Bearer {c001_tok}",
            "Idempotency-Key": f"key-bug020-{pid}",
        },
        json={"proposal_hash": conf_card["proposal_hash"]},
    )
    assert conf_resp.status_code == 200
    conf_body = conf_resp.json()
    assert conf_body["status"] == "pending_approval"
    aid = conf_body["approval_id"]
    assert aid is not None

    async def _http_400_refundable(*_args: Any, **_kwargs: Any) -> Any:
        raise StoreValidationError(
            "get_refundable ORD-1042: Requested quantity 1 exceeds remaining refundable quantity 0"
        )

    monkeypatch.setattr(store, "get_refundable", _http_400_refundable)

    queue_resp = client.get(
        "/v1/seller/approvals?status=pending&limit=50",
        headers={"Authorization": f"Bearer {owner_tok}"},
    )
    assert queue_resp.status_code == 200
    cards = queue_resp.json()["approvals"]
    assert len(cards) == 1
    assert cards[0]["approval_id"] == aid
    assert cards[0]["computed_amount"]["total_paise"] == 120000
    ver = cards[0]["expected_version"]

    dec_resp = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers={
            "Authorization": f"Bearer {owner_tok}",
            "Idempotency-Key": f"apr-bug020-{aid}",
        },
        json={
            "decision": "approve",
            "expected_version": ver,
            "reason": "Verified defective cookware set",
        },
    )
    assert dec_resp.status_code == 200
    assert dec_resp.json()["status"] == "approved"


def _run_async(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
