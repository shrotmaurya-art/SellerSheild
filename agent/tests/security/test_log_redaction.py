"""Security log redaction, trace scrubbing, and audit detail sanitization tests.

Verifies:
- Running the full SC-04 flow with log capture at DEBUG
- Asserts zero JWT-shaped strings
- Asserts zero key patterns (AIza, gsk_)
- Asserts zero Authorization header values
- Asserts zero email addresses
- Asserts zero phone numbers
across any log line, Decision Trace field, or audit event details.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sys
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
from pydantic import SecretStr
import pytest
import respx
import structlog
from starlette.testclient import TestClient

from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, get_metadata, read_tx, reset_engine
from sellershield_agent.llm.chain import LLMChain
from sellershield_agent.llm.client import LLMResult
from sellershield_agent.main import create_app
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway
from tests.security.test_route_sweep import _mint_jwt

JWKS_URL = "http://test-store:8000/.well-known/jwks.json"
JWT_REGEX = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9._-]{10,}")
PHONE_REGEX = re.compile(r"(?<![A-Za-z0-9])(?:\+\d{1,3}[\s-]?)?\(?\d{2,5}\)?[\s-]?\d{3,5}[\s-]?\d{4}(?![A-Za-z0-9])")
EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")


def _assert_zero_sensitive_data(
    target: Any,
    label: str,
    forbidden_strings: list[str],
) -> None:
    """Recursively asserts that target contains no JWTs, key patterns, auth values, emails, or phones."""
    if isinstance(target, str):
        # 1. JWT assertion
        assert not JWT_REGEX.search(target), (
            f"Found JWT-shaped string in {label}: {target[:100]}..."
        )
        # 2. Key pattern assertions (AIza, gsk_)
        prefix_aiza = "AI" + "za"
        prefix_gsk = "gs" + "k_"
        assert prefix_aiza not in target, f"Found AIza key pattern in {label}: {target}"
        assert prefix_gsk not in target, f"Found gsk_ key pattern in {label}: {target}"
        # 3. Specific forbidden credentials
        for secret_str in forbidden_strings:
            if secret_str:
                assert secret_str not in target, f"Found forbidden secret in {label}"
        # 4. Email assertion
        assert not EMAIL_REGEX.search(target), f"Found email in {label}: {target}"
        # 5. Phone number assertion
        phone_matches = PHONE_REGEX.findall(target)
        assert len(phone_matches) == 0, f"Found phone numbers {phone_matches} in {label}: {target}"

    elif isinstance(target, dict):
        for k, v in target.items():
            k_lower = str(k).lower()
            assert "authorization" not in k_lower or v == "[REDACTED]", (
                f"Unredacted authorization key '{k}' in {label}"
            )
            _assert_zero_sensitive_data(v, f"{label}.{k}", forbidden_strings)
    elif isinstance(target, (list, tuple, set)):
        for idx, item in enumerate(target):
            _assert_zero_sensitive_data(item, f"{label}[{idx}]", forbidden_strings)


@respx.mock
def test_sc04_flow_debug_logs_trace_and_audit_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run SC-04 flow with DEBUG log capture; assert no secrets, JWTs, keys, or PII in logs, trace, or audit."""
    db_file = tmp_path / "log_redaction.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v3")

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()

    # Configure stdout and stdlib log capturing at DEBUG
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.DEBUG)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)

    # Intercept sys.stdout for structlog PrintLoggerFactory
    old_stdout = sys.stdout
    sys.stdout = stdout_capture = io.StringIO()

    try:
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key())  # type: ignore[no-untyped-call]
        )
        pub_jwk.update({"kid": "redact-key-01", "alg": "RS256", "use": "sig"})
        respx.get(JWKS_URL).respond(json={"keys": [pub_jwk]})

        clock = FixedClock(datetime(2026, 11, 11, 10, 0, 0, tzinfo=UTC))
        store_client = FixtureStoreClient(clock=clock)
        app = create_app()

        from sellershield_agent.auth import deps as auth_deps
        from sellershield_agent.api import proposals as proposals_api
        from sellershield_agent.api import chat as chat_api

        app.dependency_overrides[auth_deps.get_clock] = lambda: clock
        app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: store_client
        monkeypatch.setattr(chat_api, "get_store_client", lambda clock=None, settings=None: store_client)

        client = TestClient(app, raise_server_exceptions=False)

        c001_tok = _mint_jwt(
            priv, role="customer", sub="usr_c001", customer_id="C001", kid="redact-key-01", now_dt=clock.now()
        )
        seller_tok = _mint_jwt(
            priv, role="seller_staff", sub="usr_s001", store_id="nestkart", kid="redact-key-01", now_dt=clock.now()
        )

        req_sc04 = "req-sec-sc04-redaction-01"
        test_email = "aarav.customer@example.com"
        test_phone = "+91 9876543210"
        test_aiza_key = "AI" + "zaSyD-TestFakeGoogleApiKey35Chars"
        test_gsk_key = "gs" + "k_TestFakeGroqApiKey25CharsLong"

        # Mock LLM to return understanding stage output without calling real model
        async def _mock_chat(self: Any, messages: list[Any], **kwargs: Any) -> LLMResult:
            del self, messages, kwargs
            payload = {
                "intent": "return_request",
                "confidence": 0.95,
                "order_ids": ["ORD-1042"],
                "item_mentions": ["cookware"],
                "reason_code": "defective",
                "reason_text": "stopped heating",
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
                usage={"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                provider="openai",
                model="gpt-4o-mini",
                latency_ms=25,
                cached=False,
            )

        monkeypatch.setattr(LLMChain, "chat", _mock_chat)

        # 1. Start Chat Session
        s_res = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {c001_tok}"})
        assert s_res.status_code == 200
        ses_id = s_res.json()["session_id"]

        # 2. Send Message with PII, sensitive keys, and defect description
        m_res = client.post(
            f"/v1/chat/sessions/{ses_id}/messages",
            headers={"Authorization": f"Bearer {c001_tok}", "X-Request-ID": req_sc04},
            json={
                "text": (
                    f"Customer Aarav here (email: {test_email}, phone: {test_phone}). "
                    f"Defective cookware in ORD-1042 stopped heating. "
                    f"Keys: {test_aiza_key} and {test_gsk_key}"
                ),
                "client_msg_id": "msg-redact-01",
            },
        )
        assert m_res.status_code == 200

        # 3. Create Proposal via Gateway
        engine = get_engine()
        gw = ToolGateway(
            store_client=store_client,
            engine=engine,
            clock=clock,
            policy=load_policy("v3"),
        )
        c_ctx = RequestContext(
            principal=Principal(
                user_id="usr_c001",
                role=Role.customer,
                customer_id="C001",
                auth_time=int(clock.now().timestamp()),
                jti="jti-sc04-redact",
                token_exp=int(clock.now().timestamp()) + 3600,
            ),
            request_id=req_sc04,
            token=SecretStr(c001_tok),
        )

        import asyncio
        prop_res = asyncio.run(
            gw.call(
                c_ctx,
                "propose_return",
                {
                    "order_id": "ORD-1042",
                    "items": [{"item_id": "ITEM-1042-1", "qty": 1}],
                    "reason_code": "defective",
                    "reason_text": f"Heating failed. Email {test_email} Phone {test_phone}",
                },
                allowed=["propose_return"],
                session_id=ses_id,
            )
        )
        assert prop_res.ok and isinstance(prop_res.data, dict)
        pid = str(prop_res.data["proposal_id"])
        phash = str(prop_res.data["proposal_hash"])

        # 4. Confirm Proposal (creates pending approval)
        c_res = client.post(
            f"/v1/proposals/{pid}/confirm",
            headers={
                "Authorization": f"Bearer {c001_tok}",
                "X-Request-ID": req_sc04,
                "Idempotency-Key": "idem-sc04-redact-confirm",
            },
            json={"proposal_hash": phash},
        )
        assert c_res.status_code == 200
        aid = str(c_res.json()["approval_id"])

        # 5. Seller Approves
        a_res = client.post(
            f"/v1/seller/approvals/{aid}/decision",
            headers={
                "Authorization": f"Bearer {seller_tok}",
                "X-Request-ID": req_sc04,
                "Idempotency-Key": "idem-sc04-redact-approve",
            },
            json={"decision": "approve", "reason": "Verified defective item", "expected_version": 1},
        )
        assert a_res.status_code == 200

        # 6. Fetch Decision Trace
        tr_res = client.get(
            f"/v1/seller/traces/{req_sc04}",
            headers={"Authorization": f"Bearer {seller_tok}"},
        )
        assert tr_res.status_code == 200
        trace_data = tr_res.json()

        # 7. Query Audit Events
        meta = get_metadata()
        audit_tbl = meta.tables["audit_events"]
        with read_tx(engine) as conn:
            audit_rows = conn.execute(
                audit_tbl.select().where(audit_tbl.c.request_id == req_sc04)
            ).fetchall()
        assert len(audit_rows) >= 1

    finally:
        root_logger.removeHandler(handler)
        sys.stdout = old_stdout

    # Combine all captured logs
    all_logs = log_stream.getvalue() + "\n" + stdout_capture.getvalue()
    forbidden_tokens = [c001_tok, seller_tok, test_email, test_aiza_key, test_gsk_key]

    # Verification A: No sensitive data in any log line
    _assert_zero_sensitive_data(all_logs, "Captured Logs", forbidden_tokens)

    # Verification B: No sensitive data in any Decision Trace field
    _assert_zero_sensitive_data(trace_data, "Decision Trace", forbidden_tokens)

    # Verification C: No sensitive data in audit event details
    for row in audit_rows:
        row_dict = dict(row._mapping)
        details = row_dict.get("details_json", "")
        _assert_zero_sensitive_data(details, f"Audit Row {row.event_id}", forbidden_tokens)
