"""Step A tests for T2.8 Decision Trace API, telemetry persistence, and trace security.

Covers:
- Persisting llm_calls and tool_calls rows across SC-04 request lifecycle.
- GET /v1/seller/traces/{request_id} ordered 10-stage timeline: understanding, plan,
  tool_call (redacted args), rule_table, proposal, confirmation, execution_step,
  approval, final_reply, verifier_outcome.
- Secret/PII redaction, HTML escaping of customer free text, and cross-customer isolation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr
from sqlalchemy import select
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
from sellershield_agent.db import get_engine, get_metadata, read_tx, reset_engine
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.llm.chain import LLMChain
from sellershield_agent.llm.client import LLMResult
from sellershield_agent.main import create_app
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway
from tests.test_audit_views import FIXTURES_PATH, JWKS_URL, T0_BASE, make_audit_token

CANONICAL_TRACE_STAGES = [
    "understanding",
    "plan",
    "tool_call",
    "rule_table",
    "proposal",
    "confirmation",
    "execution_step",
    "approval",
    "final_reply",
    "verifier_outcome",
]


def _make_fake_chat(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    item_mentions: list[str],
    reason_text: str,
) -> Any:
    async def _fake_chat(
        _self: LLMChain,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> LLMResult:
        del _self, messages
        payload = {
            "intent": "return_request",
            "confidence": 0.95,
            "order_ids": ["ORD-1042"],
            "item_mentions": item_mentions,
            "reason_code": "defective",
            "reason_text": reason_text,
            "language": "en",
            "sentiment": "calm",
            "asks_for_human": False,
            "secondary_intents": [],
            "needs_clarification": False,
            "clarification_slot": "none",
            "safety_flags": [],
        }
        return LLMResult(
            text=json.dumps(payload),
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            provider="openai",
            model="gpt-4o-mini",
            latency_ms=latency_ms,
            cached=False,
        )

    return _fake_chat


def _new_gateway(trace_env: dict[str, Any]) -> ToolGateway:
    clock: FixedClock = trace_env["clock"]
    return ToolGateway(
        store_client=trace_env["store"],
        engine=trace_env["engine"],
        clock=clock,
        policy=load_policy("v3"),
        service_ctx_factory=lambda rid: build_service_ctx(clock, rid),
    )


@pytest.fixture
def trace_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "decision_trace.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v3")

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(priv.public_key()))
    jwk_dict.update({"kid": "audit-view-key", "alg": "RS256", "use": "sig"})
    respx.get(JWKS_URL).respond(json={"keys": [jwk_dict]})

    clock = FixedClock(T0_BASE)
    store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=clock)
    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: clock
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: store
    monkeypatch.setattr(chat_api, "get_store_client", lambda clock=None, settings=None: store)

    tokens = {
        "c001": make_audit_token(
            user_id="usr_c001", customer_id="C001", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "c002": make_audit_token(
            user_id="usr_c002", customer_id="C002", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "seller_staff": make_audit_token(
            user_id="usr_staff", customer_id=None, role="seller_staff", priv=priv, now_dt=T0_BASE
        ),
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "client": client,
            "engine": get_engine(),
            "clock": clock,
            "store": store,
            "tokens": tokens,
        }

    app.dependency_overrides.clear()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()
    reset_engine()
    get_settings.cache_clear()


@respx.mock
def test_sc04_decision_trace_persists_llm_tool_calls_and_ordered_stages(
    trace_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC-04 full lifecycle persists llm_calls/tool_calls and renders all 10 stages in order."""
    client: TestClient = trace_env["client"]
    engine = trace_env["engine"]
    clock: FixedClock = trace_env["clock"]
    req_trace = "req-sc04-trace-001"
    c001_tok, seller_tok = trace_env["tokens"]["c001"], trace_env["tokens"]["seller_staff"]

    fake_chat = _make_fake_chat(
        prompt_tokens=120,
        completion_tokens=35,
        latency_ms=37,
        item_mentions=["cookware"],
        reason_text="stopped heating",
    )
    monkeypatch.setattr(LLMChain, "chat", fake_chat)

    s_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {c001_tok}"})
    assert s_resp.status_code == 200
    ses_id = s_resp.json()["session_id"]

    m_resp = client.post(
        f"/v1/chat/sessions/{ses_id}/messages",
        headers={"Authorization": f"Bearer {c001_tok}", "X-Request-ID": req_trace},
        json={"text": "My cookware set in ORD-1042 stopped heating", "client_msg_id": "sc04_msg_1"},
    )
    assert m_resp.status_code == 200

    llm_tbl = get_metadata().tables["llm_calls"]
    tool_tbl = get_metadata().tables["tool_calls"]
    with read_tx(engine) as conn:
        l_rows = conn.execute(select(llm_tbl).where(llm_tbl.c.request_id == req_trace)).fetchall()
        t_rows = conn.execute(select(tool_tbl).where(tool_tbl.c.request_id == req_trace)).fetchall()
    assert len(l_rows) >= 1
    assert l_rows[0].stage == "understanding"
    assert l_rows[0].provider == "openai" and l_rows[0].model == "gpt-4o-mini"
    assert len(str(l_rows[0].prompt_hash)) == 64
    assert l_rows[0].tokens_in == 120 and l_rows[0].tokens_out == 35
    assert l_rows[0].latency_ms == 37 and l_rows[0].status == "success"
    assert len(t_rows) >= 1

    gw = _new_gateway(trace_env)
    c_ctx = RequestContext(
        principal=Principal(
            user_id="usr_c001",
            role=Role.customer,
            customer_id="C001",
            auth_time=int(clock.now().timestamp()),
            jti="jti-sc04-prop",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id=req_trace,
        token=SecretStr(c001_tok),
    )
    prop_res = asyncio.run(
        gw.call(
            c_ctx, "propose_return",
            {
                "order_id": "ORD-1042",
                "items": [{"item_id": "ITEM-1042-1", "qty": 1}],
                "reason_code": "defective",
                "reason_text": "Heating coil failed",
            },
            allowed=["propose_return"], session_id=ses_id,
        )
    )
    assert prop_res.ok and isinstance(prop_res.data, dict)
    pid, phash = str(prop_res.data["proposal_id"]), str(prop_res.data["proposal_hash"])

    conf = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={
            "Authorization": f"Bearer {c001_tok}", "X-Request-ID": "req-sc04-confirm-01",
            "Idempotency-Key": "idem-sc04-conf-01",
        },
        json={"proposal_hash": phash},
    )
    assert conf.status_code == 200 and conf.json()["status"] == "pending_approval"
    aid = str(conf.json()["approval_id"])

    aprv = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers={
            "Authorization": f"Bearer {seller_tok}", "X-Request-ID": "req-sc04-approve-01",
            "Idempotency-Key": "idem-sc04-aprv-01",
        },
        json={"decision": "approve", "reason": "Verified", "expected_version": 1},
    )
    assert aprv.status_code == 200 and aprv.json()["status"] == "approved"

    tr = client.get(
        f"/v1/seller/traces/{req_trace}", headers={"Authorization": f"Bearer {seller_tok}"}
    )
    assert tr.status_code == 200, tr.text
    tbody = tr.json()
    assert set(tbody.keys()) == {
        "request_id", "timestamp", "llm_calls",
        "tool_calls", "policy_decisions", "audit_events",
    }
    assert tbody["request_id"] == req_trace
    assert len(tbody["llm_calls"]) >= 1
    assert any(
        tc["tool"] == "propose_return" and "args_redacted" in tc for tc in tbody["tool_calls"]
    )
    assert len(tbody["policy_decisions"]) >= 1
    assert tbody["policy_decisions"][0]["order_id"] == "ORD-1042"

    timeline_stages = [str(item["stage"]) for item in tbody["audit_events"]]
    first_positions = [timeline_stages.index(s) for s in CANONICAL_TRACE_STAGES]
    assert first_positions == sorted(first_positions), (
        f"Expected strictly ordered 10 stages, got {timeline_stages}"
    )


@respx.mock
def test_decision_trace_redacts_secrets_escapes_text_and_isolates_customers(
    trace_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Traces never leak tokens, PII, raw HTML/XSS, or cross-customer order details."""
    client: TestClient = trace_env["client"]
    c001_tok = trace_env["tokens"]["c001"]
    c002_tok = trace_env["tokens"]["c002"]
    seller_tok = trace_env["tokens"]["seller_staff"]

    fake_chat = _make_fake_chat(
        prompt_tokens=40,
        completion_tokens=12,
        latency_ms=11,
        item_mentions=[],
        reason_text="",
    )
    monkeypatch.setattr(LLMChain, "chat", fake_chat)

    s1 = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {c001_tok}"}).json()[
        "session_id"
    ]
    req_c1 = "req-sec-trace-c001"
    msg_resp = client.post(
        f"/v1/chat/sessions/{s1}/messages",
        headers={"Authorization": f"Bearer {c001_tok}", "X-Request-ID": req_c1},
        json={
            "text": (
                "ORD-1042 <script>alert('xss')</script> test@secret.example "
                "+919876543210 eyJhbGciOiJIUzI1NiJ9.secret.sig"
            ),
            "client_msg_id": "sec_msg_1",
        },
    )
    assert msg_resp.status_code == 200
    gw = _new_gateway(trace_env)
    c1_ctx = RequestContext(
        principal=Principal(
            user_id="usr_c001",
            role=Role.customer,
            customer_id="C001",
            jti="jti-sec-trace-c001",
            token_exp=int(trace_env["clock"].now().timestamp()) + 3600,
        ),
        request_id=req_c1,
        token=SecretStr(c001_tok),
    )
    prop_res = asyncio.run(
        gw.call(
            c1_ctx, "propose_return",
            {
                "order_id": "ORD-1042",
                "items": [{"item_id": "ITEM-1042-1", "qty": 1}],
                "reason_code": "defective",
                "reason_text": "<img src=x> coil & wire Bearer tok_secret_123 whsec_abc123",
            },
            allowed=["propose_return"], session_id=s1,
        )
    )
    assert prop_res.ok and isinstance(prop_res.data, dict)
    pid = str(prop_res.data["proposal_id"])

    req_idor = "req-sec-idor-c002"
    idor_resp = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={
            "Authorization": f"Bearer {c002_tok}", "X-Request-ID": req_idor,
            "Idempotency-Key": "idem-sec-idor-c002",
        },
        json={"proposal_hash": str(prop_res.data["proposal_hash"])},
    )
    assert idor_resp.status_code == 404

    tr_c1 = client.get(
        f"/v1/seller/traces/{req_c1}", headers={"Authorization": f"Bearer {seller_tok}"}
    )
    assert tr_c1.status_code == 200
    raw_c1 = json.dumps(tr_c1.json())
    for forbidden in (
        "eyJhbGciOi",
        "Bearer ",
        "tok_secret_123",
        "whsec_abc123",
        "test@secret.example",
        "+919876543210",
        "authorization",
        "<script>",
        "<img",
        c001_tok,
        c002_tok,
    ):
        assert forbidden not in raw_c1, f"Forbidden text leaked in C001 trace: {forbidden}"
    assert "&lt;script&gt;" in raw_c1
    assert "&lt;img" in raw_c1 and "coil &amp; wire" in raw_c1
    assert "C002" not in raw_c1 and "ORD-2032" not in raw_c1

    tr_idor = client.get(
        f"/v1/seller/traces/{req_idor}", headers={"Authorization": f"Bearer {seller_tok}"}
    )
    assert tr_idor.status_code == 200
    raw_idor = json.dumps(tr_idor.json())
    assert "C001" not in raw_idor
    assert "ORD-1042" not in raw_idor
    assert "cookware" not in raw_idor.lower()
    assert c002_tok not in raw_idor and c001_tok not in raw_idor
