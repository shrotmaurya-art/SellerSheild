"""Step A tests for T2.6 seller approval workflow, deterministic cards, and withdraw.

Covers TASK T2.6 and Spec docs/spec/02-agent.md §§9.1, 9.2, 11, 13 (rows 6, 8, 16, 17),
§21 (Amendments 1, 2, 9), §§23-24:
- SC-04 end-to-end approval path: card inspection, seller approve, exact store refund
  authorization (120000 paise), return transition to approved, approval.decided event.
- Role, limit, and step-up boundaries: seller_staff <= 200000 paise, seller_owner above
  200000 with 15-minute auth_time step-up (401 step_up_required), customer/service 403.
- Seller-token spy: authorize_refund, transition_return (approve/reject), and reject's
  cancel_reservation MUST receive the verified seller JWT, never a service token; reads
  and background expiry/withdraw holds use ServiceContext; zero open DB write tx across I/O.
- Reject flow: non-empty reason required, neutral customer client_event (no internal reason),
  return rejected and hold released via seller token.
- Idempotency, concurrency/optimistic version checks (expected_version), duplicate approve,
  rejected/withdrawn/expired 409s, strict body validation (extra='forbid', tampered amount).
- Revalidation: order/fixture drift or stricter active policy triggers needs_revalidation
  (409 with zero-PII diff); store errors roll back approving -> pending cleanly.
- Card security: HTML-escaped <= 200-char isolated Unverified customer statement block, zero
  PII, similar past decisions counts, and rule-based recommendation immune to reason text.
- Background expiry (no auto-approve on timeout) and customer withdraw (CHANGE-003).
- Uniform 404 across unknown vs foreign-store approvals and valid SHA-256 audit hash chain.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr
from sqlalchemy import Engine, event, select, update
from starlette.testclient import TestClient

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.audit.models import audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import _db_write_lock, get_engine, read_tx, reset_engine, write_tx
from sellershield_agent.domain.models import ReturnItem
from sellershield_agent.domain.state import (
    ApprovalStatus,
    ProposalStatus,
    approvals,
    client_events,
    proposals,
)
from sellershield_agent.execution.common import build_service_ctx, plan_from_prepared
from sellershield_agent.execution.saga import Executor as SagaExecutor
from sellershield_agent.execution.saga import run_forward_saga
from sellershield_agent.main import create_app
from sellershield_agent.policy import loader as policy_loader
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.proposals.confirm import confirm_proposal
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway
from tests.fault_plan import FaultPlan

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scenarios" / "fixtures" / "store.json"
)
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
FORBIDDEN_CUSTOMER_RE = re.compile(
    r"\b(?:limits?|thresholds?|risks?|scores?|staff|owner|auto(?:matic|nomy)?|caps?)\b",
    re.IGNORECASE,
)


def _create_rsa_key_pair(
    kid: str = "apr-key-1",
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
    kid: str = "apr-key-1",
    exp_seconds: int = 3600,
    auth_time_offset: int = 0,
    include_auth_time: bool = True,
    auth_time_override: Any = None,
    store_id: str | None = "nestkart",
) -> str:
    now_ts = int(now_dt.timestamp())
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "role": role,
        "iat": now_ts,
        "exp": now_ts + exp_seconds,
        "jti": str(uuid.uuid4()),
    }
    if include_auth_time:
        if auth_time_override is not None:
            payload["auth_time"] = auth_time_override
        else:
            payload["auth_time"] = now_ts - auth_time_offset
    if customer_id is not None:
        payload["customer_id"] = customer_id
    if store_id is not None:
        payload["store_id"] = store_id
    return str(jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid, "alg": "RS256"}))


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


class _SellerStoreSpy:
    """Store wrapper recording principal roles/tokens and checking no open DB write tx."""

    def __init__(self, inner: FixtureStoreClient, engine: Engine) -> None:
        self._inner = inner
        self._engine = engine
        self._lock = threading.Lock()
        self._active_tx_depth = 0
        self.calls: list[dict[str, Any]] = []
        self.tx_violations: list[str] = []

        @event.listens_for(engine, "begin")
        def _on_begin(_conn: Any) -> None:
            with self._lock:
                self._active_tx_depth += 1

        @event.listens_for(engine, "commit")
        def _on_commit(_conn: Any) -> None:
            with self._lock:
                self._active_tx_depth = max(0, self._active_tx_depth - 1)

        @event.listens_for(engine, "rollback")
        def _on_rollback(_conn: Any) -> None:
            with self._lock:
                self._active_tx_depth = max(0, self._active_tx_depth - 1)

    @property
    def _raw_data(self) -> dict[str, Any]:
        return self._inner._raw_data

    def _record(
        self,
        op: str,
        ctx: RequestContext,
        **extra: Any,
    ) -> None:
        with self._lock:
            tx_open = self._active_tx_depth > 0 or _db_write_lock.locked()
            if tx_open:
                self.tx_violations.append(
                    f"{op} called inside open tx "
                    f"(depth={self._active_tx_depth}, lock={_db_write_lock.locked()})"
                )
            token_val = ctx.token.get_secret_value() if ctx.token is not None else None
            entry: dict[str, Any] = {
                "op": op,
                "role": ctx.principal.role,
                "user_id": ctx.principal.user_id,
                "customer_id": ctx.principal.customer_id,
                "has_token": token_val is not None and len(token_val) > 0,
                "token_value": token_val,
                "scopes": list(getattr(ctx, "scopes", [])),
            }
            entry.update(extra)
            self.calls.append(entry)

    def reset_spy(self) -> None:
        with self._lock:
            self.calls.clear()
            self.tx_violations.clear()

    def get_clock(self) -> Any:
        return self._inner.get_clock()

    async def list_orders(self, ctx: RequestContext) -> Any:
        self._record("list_orders", ctx)
        return await self._inner.list_orders(ctx)

    async def get_order(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("get_order", ctx, order_id=order_id)
        return await self._inner.get_order(ctx, order_id)

    async def get_shipment(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("get_shipment", ctx, order_id=order_id)
        return await self._inner.get_shipment(ctx, order_id)

    async def get_refundable(
        self,
        ctx: RequestContext,
        order_id: str,
        items: list[tuple[str, int]] | None = None,
    ) -> Any:
        self._record("get_refundable", ctx, order_id=order_id)
        return await self._inner.get_refundable(ctx, order_id, items=items)

    async def list_returns(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("list_returns", ctx, order_id=order_id)
        return await self._inner.list_returns(ctx, order_id)

    async def list_refunds(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("list_refunds", ctx, order_id=order_id)
        return await self._inner.list_refunds(ctx, order_id)

    async def get_customer_risk_facts(self, service_ctx: RequestContext, customer_id: str) -> Any:
        self._record("get_customer_risk_facts", service_ctx, target_customer=customer_id)
        return await self._inner.get_customer_risk_facts(service_ctx, customer_id)

    async def create_return(
        self,
        ctx: RequestContext,
        order_id: str,
        items: list[ReturnItem] | list[dict[str, Any]],
        reason_code: str,
        reason_text: str | None,
        resolution: str,
        idempotency_key: str,
    ) -> Any:
        self._record("create_return", ctx, order_id=order_id, idempotency_key=idempotency_key)
        return await self._inner.create_return(
            ctx, order_id, items, reason_code, reason_text, resolution, idempotency_key
        )

    async def get_return(self, ctx: RequestContext, return_id: str) -> Any:
        self._record("get_return", ctx, return_id=return_id)
        return await self._inner.get_return(ctx, return_id)

    async def transition_return(
        self,
        ctx: RequestContext,
        return_id: str,
        to: str,
        reason: str | None,
        idempotency_key: str,
    ) -> Any:
        self._record(
            "transition_return",
            ctx,
            return_id=return_id,
            to=to,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        return await self._inner.transition_return(ctx, return_id, to, reason, idempotency_key)

    async def reserve_refund(
        self,
        service_ctx: RequestContext,
        order_id: str,
        amount_paise: int,
        return_id: str | None,
        method: str,
        idempotency_key: str,
    ) -> Any:
        self._record(
            "reserve_refund",
            service_ctx,
            order_id=order_id,
            amount_paise=amount_paise,
            idempotency_key=idempotency_key,
        )
        return await self._inner.reserve_refund(
            service_ctx, order_id, amount_paise, return_id, method, idempotency_key
        )

    async def authorize_refund(
        self,
        seller_ctx: RequestContext,
        refund_id: str,
        amount_paise: int,
        note: str | None,
        idempotency_key: str,
    ) -> Any:
        self._record(
            "authorize_refund",
            seller_ctx,
            refund_id=refund_id,
            amount_paise=amount_paise,
            note=note,
            idempotency_key=idempotency_key,
        )
        return await self._inner.authorize_refund(
            seller_ctx, refund_id, amount_paise, note, idempotency_key
        )

    async def release_refund(
        self, ctx: RequestContext, refund_id: str, idempotency_key: str
    ) -> Any:
        self._record("release_refund", ctx, refund_id=refund_id, idempotency_key=idempotency_key)
        return await self._inner.release_refund(ctx, refund_id, idempotency_key)

    async def cancel_reservation(
        self, ctx: RequestContext, refund_id: str, idempotency_key: str
    ) -> Any:
        self._record(
            "cancel_reservation", ctx, refund_id=refund_id, idempotency_key=idempotency_key
        )
        return await self._inner.cancel_reservation(ctx, refund_id, idempotency_key)

    async def get_refund(self, ctx: RequestContext, refund_id: str) -> Any:
        self._record("get_refund", ctx, refund_id=refund_id)
        return await self._inner.get_refund(ctx, refund_id)

    async def get_order_admin(self, service_ctx: RequestContext, order_id: str) -> Any:
        self._record("get_order_admin", service_ctx, order_id=order_id)
        return await self._inner.get_order_admin(service_ctx, order_id)

    async def get_settings(self, service_ctx: RequestContext) -> Any:
        self._record("get_settings", service_ctx)
        return await self._inner.get_settings(service_ctx)

    async def get_customer_refund_summary(
        self, service_ctx: RequestContext, customer_id: str, days: int = 30
    ) -> Any:
        self._record("get_customer_refund_summary", service_ctx, target_customer=customer_id)
        return await self._inner.get_customer_refund_summary(service_ctx, customer_id, days=days)


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
    db_file = tmp_path / "seller_approvals_test.db"
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

    fault_plan = FaultPlan()
    inner_store = FixtureStoreClient(
        fixtures_path=FIXTURES_PATH,
        clock=fixed_clock,
        fault_plan=fault_plan,
    )
    engine = get_engine()
    spy_store = _SellerStoreSpy(inner=inner_store, engine=engine)
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: spy_store

    tokens = {
        "c001": _make_token(
            user_id="usr_c001", customer_id="C001", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "c002": _make_token(
            user_id="usr_c002", customer_id="C002", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "c003": _make_token(
            user_id="usr_c003", customer_id="C003", role="customer", priv=priv, now_dt=T0_BASE
        ),
        "seller_staff": _make_token(
            user_id="usr_staff", customer_id=None, role="seller_staff", priv=priv, now_dt=T0_BASE
        ),
        "seller_staff_stale": _make_token(
            user_id="usr_staff",
            customer_id=None,
            role="seller_staff",
            priv=priv,
            now_dt=T0_BASE,
            auth_time_offset=950,
        ),
        "seller_owner": _make_token(
            user_id="usr_owner", customer_id=None, role="seller_owner", priv=priv, now_dt=T0_BASE
        ),
        "seller_owner_stale": _make_token(
            user_id="usr_owner",
            customer_id=None,
            role="seller_owner",
            priv=priv,
            now_dt=T0_BASE,
            auth_time_offset=905,
        ),
        "seller_owner_other_store": _make_token(
            user_id="usr_other_owner",
            customer_id=None,
            role="seller_owner",
            priv=priv,
            now_dt=T0_BASE,
            store_id="other-store",
        ),
        "service": _make_token(
            user_id="svc_exec", customer_id=None, role="service", priv=priv, now_dt=T0_BASE
        ),
        "expired": _make_token(
            user_id="usr_owner",
            customer_id=None,
            role="seller_owner",
            priv=priv,
            now_dt=T0_BASE,
            exp_seconds=-120,
        ),
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "app": app,
            "client": client,
            "clock": fixed_clock,
            "store": spy_store,
            "inner_store": inner_store,
            "fault_plan": fault_plan,
            "engine": engine,
            "tokens": tokens,
            "priv": priv,
            "tmp_path": tmp_path,
        }

    app.dependency_overrides.clear()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()
    reset_engine()
    get_settings.cache_clear()


async def _create_pending_approval(
    app_env: dict[str, Any],
    *,
    customer_id: str,
    user_id: str,
    order_id: str,
    items: list[dict[str, Any]] | None = None,
    proposal_type: str = "return",
    reason_code: str = "defective",
    reason_text: str = "Defective item requested for return",
    resolution: str = "refund",
    evidence_ids: list[str] | None = None,
    use_service_risk_facts: bool = True,
) -> dict[str, Any]:
    """Create and confirm a NEEDS_APPROVAL proposal through real ToolGateway + SagaExecutor."""
    engine: Engine = app_env["engine"]
    store: _SellerStoreSpy = app_env["store"]
    clock: FixedClock = app_env["clock"]
    policy = load_policy()
    svc_factory = (
        (lambda req_id: build_service_ctx(clock, req_id)) if use_service_risk_facts else None
    )
    gateway = ToolGateway(
        store_client=store,
        engine=engine,
        clock=clock,
        policy=policy,
        service_ctx_factory=svc_factory,
    )
    req_id = f"req-{uuid.uuid4().hex[:8]}"
    ctx = RequestContext(
        principal=Principal(
            user_id=user_id,
            role=Role.customer,
            customer_id=customer_id,
            auth_time=int(clock.now().timestamp()),
            jti=f"jti-{uuid.uuid4().hex[:8]}",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id=req_id,
        token=SecretStr(app_env["tokens"].get(customer_id.lower(), "cust-test-jwt")),
    )
    if proposal_type == "refund":
        args: dict[str, Any] = {"order_id": order_id, "cause": "lost"}
        tool_name = "propose_refund"
    else:
        assert items is not None
        args = {
            "order_id": order_id,
            "items": items,
            "reason_code": reason_code,
            "reason_text": reason_text,
            "resolution": resolution,
        }
        if evidence_ids is not None:
            args["evidence_ids"] = evidence_ids
        tool_name = "propose_return"

    res = await gateway.call(
        ctx=ctx,
        name=tool_name,
        raw_args=args,
        allowed={"propose_return", "propose_refund"},
        session_id=f"ses_{uuid.uuid4().hex[:8]}",
    )
    assert res.ok is True and isinstance(res.data, dict)
    assert res.data.get("type") == "confirmation", f"Expected proposal card, got {res.data}"

    pid = str(res.data["proposal_id"])
    phash = str(res.data["proposal_hash"])

    class _ConfirmExecutorAdapter:
        def __init__(self) -> None:
            self._saga = SagaExecutor(store_client=store, engine=engine, clock=clock)

        async def execute(
            self,
            customer_ctx: RequestContext,
            proposal: Any,
            *,
            clock: Any,
            idempotency_key: str,
        ) -> Any:
            del idempotency_key
            plan = plan_from_prepared(proposal, request_id=customer_ctx.request_id)
            return await run_forward_saga(
                engine=engine,
                store=store,
                plan=plan,
                clock=clock,
                customer_ctx=customer_ctx,
                by_step={},
                update_proposal_status=True,
                update_idempotency=False,
                from_reconcile=False,
            )

    confirm_resp = await confirm_proposal(
        ctx,
        pid,
        phash,
        idempotency_key=f"idem-confirm-{uuid.uuid4().hex[:10]}",
        executor=_ConfirmExecutorAdapter(),
        store_client=store,
        clock=clock,
        engine=engine,
    )
    assert confirm_resp.status == "pending_approval"
    assert confirm_resp.approval_id is not None

    with read_tx(engine) as conn:
        apr_row = conn.execute(
            select(approvals).where(approvals.c.approval_id == confirm_resp.approval_id)
        ).fetchone()
    assert apr_row is not None
    store.reset_spy()
    return {
        "proposal_id": pid,
        "proposal_hash": phash,
        "approval_id": str(apr_row.approval_id),
        "return_id": apr_row.return_id,
        "refund_id": str(apr_row.refund_id),
        "amount_paise": int(apr_row.amount_paise),
        "required_role": str(apr_row.required_role),
        "version": int(apr_row.version),
    }


def _get_approval_row(engine: Engine, approval_id: str) -> Any:
    with read_tx(engine) as conn:
        return conn.execute(
            select(approvals).where(approvals.c.approval_id == approval_id)
        ).fetchone()


def _get_proposal_row(engine: Engine, proposal_id: str) -> Any:
    with read_tx(engine) as conn:
        return conn.execute(
            select(proposals).where(proposals.c.proposal_id == proposal_id)
        ).fetchone()


def _seller_headers(token: str, idem: str | None = None) -> dict[str, str]:
    hdrs = {"Authorization": f"Bearer {token}"}
    if idem is not None:
        hdrs["Idempotency-Key"] = idem
    return hdrs


# ── 1. SC-04 End-to-End Approve Path & Card Contents ────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_sc04_approve_path_authorizes_exact_amount_and_emits_client_event(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
        reason_text="Cookware non-stick coating peeling off",
    )
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    engine: Engine = app_env["engine"]
    store: _SellerStoreSpy = app_env["store"]
    clock: FixedClock = app_env["clock"]

    card_resp = client.get(
        f"/v1/seller/approvals/{aid}",
        headers=_seller_headers(app_env["tokens"]["seller_staff"]),
    )
    assert card_resp.status_code == 200, card_resp.text
    card = card_resp.json()
    assert card["approval_id"] == aid
    assert card["proposal_id"] == created["proposal_id"]
    assert card["status"] == "pending"
    assert card["requested_amount_paise"] == 120000
    assert card["amount_paise"] == 120000
    assert card["policy_version"] == "v3"
    assert card["computed_amount"]["total_paise"] == 120000
    assert card["computed_amount"]["items_paise"] == 120000
    assert card["method"] == "original"
    assert card["recommendation"] == "approve"
    assert isinstance(card["rule_results"], list) and len(card["rule_results"]) >= 10
    assert any(r.get("seller_text") for r in card["rule_results"])
    assert card["risk"]["band"] == "low"
    assert card["unverified_customer_statement"]["label"] == "Unverified customer statement"
    assert "Cookware" in card["unverified_customer_statement"]["text"]
    assert card["due_at"] and card["expires_at"]
    assert "similar_past_decisions" in card

    store.reset_spy()
    dec_resp = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], f"idem-{uuid.uuid4().hex}"),
        json={
            "decision": "approve",
            "reason": "Verified defective cookware photos",
            "expected_version": created["version"],
        },
    )
    assert dec_resp.status_code == 200, dec_resp.text
    dec_body = dec_resp.json()
    assert dec_body["status"] == "approved"
    assert dec_body["store_authorization_ref"]
    assert dec_body["decided_by"] == "usr_staff"
    assert dec_body["decided_at"] is not None
    assert dec_body["expected_version"] > created["version"]

    svc_ctx = build_service_ctx(clock, "req-verify")
    ret = await store.get_return(svc_ctx, str(created["return_id"]))
    rfd = await store.get_refund(svc_ctx, created["refund_id"])
    assert ret.status.value == "approved"
    assert ret.approved_by == "usr_staff"
    assert rfd.status.value == "authorized"
    assert rfd.authorized_amount_paise == 120000
    assert rfd.authorized_by == "usr_staff"

    apr_row = _get_approval_row(engine, aid)
    prop_row = _get_proposal_row(engine, created["proposal_id"])
    assert apr_row.status == ApprovalStatus.approved.value
    assert prop_row.status == ProposalStatus.succeeded.value

    with read_tx(engine) as conn:
        events = conn.execute(
            select(client_events).where(
                client_events.c.customer_id == "C001",
                client_events.c.type == "approval.decided",
            )
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(str(events[0].payload_json))
        assert payload["approval_id"] == aid
        assert payload["proposal_id"] == created["proposal_id"]
        assert payload["status"] == "approved"
        assert not FORBIDDEN_CUSTOMER_RE.search(json.dumps(payload))


# ── 2. Role, Staff Limit, Step-Up Auth, & Route Auth Boundaries ──────────────


@pytest.mark.asyncio
@respx.mock
async def test_staff_limit_blocks_above_200000_and_audits(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[{"item_id": "ITEM-3011-1", "qty": 1}, {"item_id": "ITEM-3011-2", "qty": 1}],
        reason_code="defective",
    )
    assert created["amount_paise"] == 245000
    assert created["required_role"] == "seller_owner"

    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    engine: Engine = app_env["engine"]
    store: _SellerStoreSpy = app_env["store"]
    store.reset_spy()

    r_approve = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-staff-hi-1"),
        json={"decision": "approve", "reason": "Try approve", "expected_version": 1},
    )
    _assert_error_envelope(r_approve, 403, "forbidden")

    r_reject = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-staff-hi-2"),
        json={"decision": "reject", "reason": "Try reject", "expected_version": 1},
    )
    _assert_error_envelope(r_reject, 403, "forbidden")

    # Stale auth_time staff above 200000 still gets 403 (role check before step-up)
    r_stale_staff = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff_stale"], "idem-staff-hi-3"),
        json={"decision": "approve", "reason": "Stale staff", "expected_version": 1},
    )
    _assert_error_envelope(r_stale_staff, 403, "forbidden")

    assert not [c for c in store.calls if c["op"] in ("authorize_refund", "transition_return")]
    apr_row = _get_approval_row(engine, aid)
    assert apr_row.status == ApprovalStatus.pending.value

    with read_tx(engine) as conn:
        denials = conn.execute(
            select(audit_events).where(audit_events.c.event_type == "auth_denied")
        ).fetchall()
        assert len(denials) >= 3
        assert AuditService.verify_chain(conn).ok is True


@pytest.mark.asyncio
@respx.mock
async def test_owner_approves_above_staff_limit_and_below_limit(
    app_env: dict[str, Any],
) -> None:
    high = await _create_pending_approval(
        app_env,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[{"item_id": "ITEM-3011-1", "qty": 1}, {"item_id": "ITEM-3011-2", "qty": 1}],
    )
    low = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _SellerStoreSpy = app_env["store"]

    r_high = client.post(
        f"/v1/seller/approvals/{high['approval_id']}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_owner"], "idem-owner-hi"),
        json={"decision": "approve", "reason": "Owner approved", "expected_version": 1},
    )
    assert r_high.status_code == 200, r_high.text
    assert r_high.json()["status"] == "approved"

    r_low = client.post(
        f"/v1/seller/approvals/{low['approval_id']}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_owner"], "idem-owner-lo"),
        json={"decision": "approve", "reason": "Owner approved", "expected_version": 1},
    )
    assert r_low.status_code == 200, r_low.text
    assert r_low.json()["status"] == "approved"

    svc_ctx = build_service_ctx(clock, "req-chk")
    rfd_hi = await store.get_refund(svc_ctx, high["refund_id"])
    assert rfd_hi.authorized_amount_paise == 245000
    assert rfd_hi.authorized_by == "usr_owner"


@pytest.mark.asyncio
@respx.mock
async def test_staff_token_at_or_below_limit_succeeds(
    app_env: dict[str, Any],
) -> None:
    exact_limit = await _create_pending_approval(
        app_env,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[{"item_id": "ITEM-3011-1", "qty": 1}],
    )
    assert exact_limit["amount_paise"] == 200000
    assert exact_limit["required_role"] == "seller_staff"

    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _SellerStoreSpy = app_env["store"]

    resp = client.post(
        f"/v1/seller/approvals/{exact_limit['approval_id']}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-exact-200k"),
        json={"decision": "approve", "reason": "Exact limit OK", "expected_version": 1},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    svc_ctx = build_service_ctx(clock, "req-chk")
    rfd = await store.get_refund(svc_ctx, exact_limit["refund_id"])
    assert rfd.authorized_amount_paise == 200000

    # Below limit, stale auth_time still succeeds for seller_staff (step-up is above limit only)
    low = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    resp_stale_ok = client.post(
        f"/v1/seller/approvals/{low['approval_id']}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff_stale"], "idem-lo-stale"),
        json={"decision": "approve", "reason": "Below limit", "expected_version": 1},
    )
    assert resp_stale_ok.status_code == 200, resp_stale_ok.text


@pytest.mark.asyncio
@respx.mock
async def test_customer_and_service_tokens_on_seller_routes_return_403(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    engine: Engine = app_env["engine"]

    _assert_error_envelope(client.get("/v1/seller/approvals"), 401, "unauthenticated")
    _assert_error_envelope(
        client.get("/v1/seller/approvals", headers=_seller_headers(app_env["tokens"]["expired"])),
        401,
        "unauthenticated",
    )

    for tok_key in ("c001", "service"):
        tok = app_env["tokens"][tok_key]
        _assert_error_envelope(
            client.get("/v1/seller/approvals", headers=_seller_headers(tok)),
            403,
            "forbidden",
        )
        _assert_error_envelope(
            client.get(f"/v1/seller/approvals/{aid}", headers=_seller_headers(tok)),
            403,
            "forbidden",
        )
        # Send malformed JSON too to confirm auth fires before body validation (no 422 oracle)
        _assert_error_envelope(
            client.post(
                f"/v1/seller/approvals/{aid}/decision",
                headers=_seller_headers(tok, f"idem-{tok_key}"),
                json={"bad": "payload"},
            ),
            403,
            "forbidden",
        )

    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True


# ── 3. Reject Flow, Neutral Client Event & Reason Validation ─────────────────


@pytest.mark.asyncio
@respx.mock
async def test_reject_requires_reason_transitions_return_and_cancels_hold(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    engine: Engine = app_env["engine"]
    store: _SellerStoreSpy = app_env["store"]
    clock: FixedClock = app_env["clock"]

    for bad_reason in (None, "", "   \n\t  "):
        payload: dict[str, Any] = {"decision": "reject", "expected_version": 1}
        if bad_reason is not None:
            payload["reason"] = bad_reason
        r_bad = client.post(
            f"/v1/seller/approvals/{aid}/decision",
            headers=_seller_headers(app_env["tokens"]["seller_staff"], f"idem-bad-{uuid.uuid4()}"),
            json=payload,
        )
        _assert_error_envelope(r_bad, 422, "validation_error")

    internal_note = "Flagged by internal staff risk review threshold check"
    r_ok = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-reject-ok"),
        json={"decision": "reject", "reason": internal_note, "expected_version": 1},
    )
    assert r_ok.status_code == 200, r_ok.text
    body = r_ok.json()
    assert body["status"] == "rejected"
    assert body["decision_reason"] == internal_note
    assert body["store_authorization_ref"] is None

    svc_ctx = build_service_ctx(clock, "req-rej-chk")
    ret = await store.get_return(svc_ctx, str(created["return_id"]))
    rfd = await store.get_refund(svc_ctx, created["refund_id"])
    assert ret.status.value == "rejected"
    assert rfd.status.value == "released"

    assert _get_proposal_row(engine, created["proposal_id"]).status == ProposalStatus.failed.value

    with read_tx(engine) as conn:
        events = conn.execute(
            select(client_events).where(
                client_events.c.customer_id == "C001",
                client_events.c.type == "approval.decided",
            )
        ).fetchall()
        assert len(events) == 1
        ev_payload = json.loads(str(events[0].payload_json))
        assert ev_payload["status"] == "rejected"
        ev_text = json.dumps(ev_payload)
        assert "internal staff" not in ev_text.lower()
        assert not FORBIDDEN_CUSTOMER_RE.search(ev_text)


# ── 4. State Conflicts, Double-Approve, Idempotency & Tampered Body ──────────


@pytest.mark.asyncio
@respx.mock
async def test_approve_twice_returns_409_and_idempotency_replay_works(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    store: _SellerStoreSpy = app_env["store"]
    staff_tok = app_env["tokens"]["seller_staff"]
    first_body = {"decision": "approve", "reason": "All checks pass", "expected_version": 1}

    r1 = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(staff_tok, "idem-once"),
        json=first_body,
    )
    assert r1.status_code == 200, r1.text
    new_version = r1.json()["expected_version"]

    # Identical Idempotency-Key + identical body replays without calling store again
    calls_before = len(store.calls)
    r_replay = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(staff_tok, "idem-once"),
        json=first_body,
    )
    assert r_replay.status_code == 200
    assert r_replay.json() == r1.json()
    assert len(store.calls) == calls_before

    # Same Idempotency-Key + different body -> 409 idempotency_mismatch
    r_mismatch = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(staff_tok, "idem-once"),
        json={"decision": "reject", "reason": "Changed mind", "expected_version": 1},
    )
    _assert_error_envelope(r_mismatch, 409, "idempotency_mismatch")

    # New Idempotency-Key -> 409 invalid_state regardless of expected_version
    for ver in (1, new_version):
        r2 = client.post(
            f"/v1/seller/approvals/{aid}/decision",
            headers=_seller_headers(staff_tok, f"idem-twice-{ver}"),
            json={"decision": "approve", "reason": "Second click", "expected_version": ver},
        )
        _assert_error_envelope(r2, 409, "invalid_state")


@pytest.mark.asyncio
@respx.mock
async def test_approve_rejected_or_expired_returns_409(
    app_env: dict[str, Any],
) -> None:
    rejected = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    priv: RSAPrivateKey = app_env["priv"]

    r_rej = client.post(
        f"/v1/seller/approvals/{rejected['approval_id']}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-rej-1"),
        json={"decision": "reject", "reason": "Not defective", "expected_version": 1},
    )
    assert r_rej.status_code == 200
    r_app_rej = client.post(
        f"/v1/seller/approvals/{rejected['approval_id']}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-rej-2"),
        json={"decision": "approve", "reason": "Try approve", "expected_version": 2},
    )
    _assert_error_envelope(r_app_rej, 409, "invalid_state")

    # Advance past 7-day approval expiry on another approval
    to_expire = await _create_pending_approval(
        app_env,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[{"item_id": "ITEM-3011-1", "qty": 1}],
    )
    clock.set(clock.now() + timedelta(days=7, seconds=60))
    fresh_staff = _make_token(
        user_id="usr_staff",
        customer_id=None,
        role="seller_staff",
        priv=priv,
        now_dt=clock.now(),
    )
    r_exp = client.post(
        f"/v1/seller/approvals/{to_expire['approval_id']}/decision",
        headers=_seller_headers(fresh_staff, "idem-exp-1"),
        json={"decision": "approve", "reason": "Late approve", "expected_version": 1},
    )
    _assert_error_envelope(r_exp, 409, "invalid_state")

    apr_exp = _get_approval_row(app_env["engine"], to_expire["approval_id"])
    assert apr_exp.status == ApprovalStatus.expired.value
    assert apr_exp.store_authorization_ref is None
    svc_ctx = build_service_ctx(clock, "req-exp-chk")
    assert (await app_env["store"].get_refund(svc_ctx, to_expire["refund_id"])).status.value == (
        "released"
    )


@pytest.mark.asyncio
@respx.mock
async def test_wrong_expected_version_returns_409(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    store: _SellerStoreSpy = app_env["store"]
    store.reset_spy()

    r_wrong = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-ver-wrong"),
        json={"decision": "approve", "reason": "Stale UI tab", "expected_version": 99},
    )
    _assert_error_envelope(r_wrong, 409, "invalid_state")
    assert len(store.calls) == 0
    assert _get_approval_row(app_env["engine"], aid).status == ApprovalStatus.pending.value

    # Boolean / 0 version must be rejected cleanly with 422, not coerced to int 1
    for bad_ver in (True, 0, -1):
        r_bad_ver = client.post(
            f"/v1/seller/approvals/{aid}/decision",
            headers=_seller_headers(app_env["tokens"]["seller_staff"], f"idem-bv-{bad_ver}"),
            json={"decision": "approve", "reason": "Bad ver", "expected_version": bad_ver},
        )
        _assert_error_envelope(r_bad_ver, 422, "validation_error")


@pytest.mark.asyncio
@respx.mock
async def test_tampered_amount_in_body_rejected_and_authorized_equals_reserved(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    engine: Engine = app_env["engine"]
    store: _SellerStoreSpy = app_env["store"]

    for extra_key in ("amount", "amount_paise", "required_role"):
        r_extra = client.post(
            f"/v1/seller/approvals/{aid}/decision",
            headers=_seller_headers(app_env["tokens"]["seller_staff"], f"idem-{extra_key}"),
            json={
                "decision": "approve",
                "reason": "Tampered",
                "expected_version": 1,
                extra_key: 500000,
            },
        )
        _assert_error_envelope(r_extra, 422, "validation_error")

    # Even if DB approval row amount was tampered out-of-band, revalidation blocks before authorize
    with write_tx(engine) as conn:
        conn.execute(
            update(approvals).where(approvals.c.approval_id == aid).values(amount_paise=190000)
        )
    r_db_tamper = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-db-tamper"),
        json={"decision": "approve", "reason": "Check mismatch", "expected_version": 1},
    )
    _assert_error_envelope(r_db_tamper, 409, "invalid_state")
    assert not [c for c in store.calls if c["op"] == "authorize_refund"]

    # Normal approval always passes exact reserved amount
    created2 = await _create_pending_approval(
        app_env,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[{"item_id": "ITEM-3011-1", "qty": 1}],
    )
    store.reset_spy()
    r_ok = client.post(
        f"/v1/seller/approvals/{created2['approval_id']}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-reserved-exact"),
        json={"decision": "approve", "reason": "Verified", "expected_version": 1},
    )
    assert r_ok.status_code == 200
    auth_calls = [c for c in store.calls if c["op"] == "authorize_refund"]
    assert len(auth_calls) == 1
    assert auth_calls[0]["amount_paise"] == 200000


# ── 5. Revalidation (Facts Changed & Policy Version Change) ──────────────────


@pytest.mark.asyncio
@respx.mock
async def test_needs_revalidation_when_order_facts_change_returns_diff(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    engine: Engine = app_env["engine"]
    store: _SellerStoreSpy = app_env["store"]

    # Mutate order item price & order total in fixture store after proposal was created
    for raw_order in store._raw_data["orders"]:
        if raw_order["order_id"] == "ORD-1042":
            raw_order["total_paise"] = 100000
            raw_order["subtotal_paise"] = 100000
            raw_order["items"][0]["line_paid_paise"] = 100000
            raw_order["items"][0]["unit_price_paise"] = 100000
            raw_order["version"] = 2

    store.reset_spy()
    resp = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-drift-facts"),
        json={"decision": "approve", "reason": "Check facts", "expected_version": 1},
    )
    err = _assert_error_envelope(resp, 409, "invalid_state")["error"]
    assert err["details"].get("status") == "needs_revalidation"
    diff = err["details"].get("diff")
    assert isinstance(diff, list) and len(diff) > 0
    diff_str = json.dumps(diff)
    assert "Aarav" not in diff_str and "+91" not in diff_str and "560103" not in diff_str

    assert not [c for c in store.calls if c["op"] in ("authorize_refund", "transition_return")]
    assert (
        _get_approval_row(engine, aid).status == ApprovalStatus.needs_revalidation.value
    )

    card_after = client.get(
        f"/v1/seller/approvals/{aid}",
        headers=_seller_headers(app_env["tokens"]["seller_staff"]),
    ).json()
    assert card_after["status"] == "needs_revalidation"
    assert card_after["recommendation"] == "review"
    diff_after = card_after["revalidation_diff"]
    assert isinstance(diff_after, list) and len(diff_after) > 0

    # Approve remains blocked on needs_revalidation, whereas reject cleans up the hold
    r_retry_approve = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-drift-approve"),
        json={
            "decision": "approve",
            "reason": "Retry approve",
            "expected_version": card_after["expected_version"],
        },
    )
    _assert_error_envelope(r_retry_approve, 409, "invalid_state")

    r_reject = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-drift-reject"),
        json={
            "decision": "reject",
            "reason": "Rejected after order facts changed",
            "expected_version": card_after["expected_version"],
        },
    )
    assert r_reject.status_code == 200
    assert r_reject.json()["status"] == "rejected"


@pytest.mark.asyncio
@respx.mock
async def test_needs_revalidation_when_policy_version_change_fails_rule(
    app_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    store: _SellerStoreSpy = app_env["store"]
    tmp_path: Path = app_env["tmp_path"]

    # Build temp policy versions dir copying v3 and adding stricter v4 (2-day home_kitchen window)
    tmp_versions = tmp_path / "policy_versions"
    tmp_versions.mkdir()
    orig_v3 = json.loads((policy_loader.VERSIONS_DIR / "v3.json").read_text(encoding="utf-8"))
    (tmp_versions / "v3.json").write_text(json.dumps(orig_v3), encoding="utf-8")
    v4 = dict(orig_v3)
    v4["version"] = "v4"
    v4["return_windows_days"] = {**orig_v3["return_windows_days"], "home_kitchen": 2}
    (tmp_versions / "v4.json").write_text(json.dumps(v4), encoding="utf-8")

    monkeypatch.setattr(policy_loader, "VERSIONS_DIR", tmp_versions)
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v4")

    store.reset_spy()
    resp = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-pol-change"),
        json={"decision": "approve", "reason": "Policy changed", "expected_version": 1},
    )
    err = _assert_error_envelope(resp, 409, "invalid_state")["error"]
    assert err["details"].get("status") == "needs_revalidation"
    assert isinstance(err["details"].get("diff"), list) and len(err["details"]["diff"]) > 0
    assert not [c for c in store.calls if c["op"] in ("authorize_refund", "transition_return")]
    assert _get_approval_row(app_env["engine"], aid).status == "needs_revalidation"


# ── 6. Store Failures & Seller Token Spy ─────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_store_failure_during_authorize_leaves_pending_with_error_and_audit(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    engine: Engine = app_env["engine"]
    fault_plan: FaultPlan = app_env["fault_plan"]
    clock: FixedClock = app_env["clock"]
    store: _SellerStoreSpy = app_env["store"]

    fault_plan.fail_once("authorize_refund")
    r_fail = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-store-down"),
        json={"decision": "approve", "reason": "First attempt", "expected_version": 1},
    )
    _assert_error_envelope(r_fail, 503, "upstream_unavailable")

    apr_after = _get_approval_row(engine, aid)
    assert apr_after.status == ApprovalStatus.pending.value
    assert apr_after.store_authorization_ref is None
    assert apr_after.version > 1

    card_after = client.get(
        f"/v1/seller/approvals/{aid}",
        headers=_seller_headers(app_env["tokens"]["seller_staff"]),
    ).json()
    assert card_after["last_error"] is not None

    with read_tx(engine) as conn:
        failed_audits = conn.execute(
            select(audit_events).where(
                audit_events.c.resource_id == aid,
                audit_events.c.outcome == "failed",
            )
        ).fetchall()
        assert len(failed_audits) >= 1

    # Retry with SAME Idempotency-Key and updated expected_version after outage clears -> 200!
    r_retry = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-store-down"),
        json={
            "decision": "approve",
            "reason": "First attempt",
            "expected_version": int(apr_after.version),
        },
    )
    assert r_retry.status_code == 200, r_retry.text
    assert r_retry.json()["status"] == "approved"

    # Partial failure: authorize_refund commits, transition_return fails -> retry completes
    created2 = await _create_pending_approval(
        app_env,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[{"item_id": "ITEM-3011-1", "qty": 1}],
    )
    aid2 = created2["approval_id"]
    fault_plan.fail_once("transition_return")
    r_partial = client.post(
        f"/v1/seller/approvals/{aid2}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-partial-1"),
        json={"decision": "approve", "reason": "Approve", "expected_version": 1},
    )
    _assert_error_envelope(r_partial, 503, "upstream_unavailable")
    apr2_mid = _get_approval_row(engine, aid2)
    assert apr2_mid.status == ApprovalStatus.pending.value
    assert apr2_mid.store_authorization_ref is not None

    store.reset_spy()
    r_partial_retry = client.post(
        f"/v1/seller/approvals/{aid2}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-partial-2"),
        json={
            "decision": "approve",
            "reason": "Approve retry",
            "expected_version": int(apr2_mid.version),
        },
    )
    assert r_partial_retry.status_code == 200, r_partial_retry.text
    assert r_partial_retry.json()["status"] == "approved"
    assert not [c for c in store.calls if c["op"] == "authorize_refund"]
    svc_ctx = build_service_ctx(clock, "req-partial-chk")
    assert (await store.get_return(svc_ctx, str(created2["return_id"]))).status.value == (
        "approved"
    )


@pytest.mark.asyncio
@respx.mock
async def test_seller_token_spy_on_authorize_and_return_transitions(
    app_env: dict[str, Any],
) -> None:
    to_approve = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    to_reject = await _create_pending_approval(
        app_env,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[{"item_id": "ITEM-3011-1", "qty": 1}],
    )
    client: TestClient = app_env["client"]
    store: _SellerStoreSpy = app_env["store"]
    staff_tok = app_env["tokens"]["seller_staff"]
    owner_tok = app_env["tokens"]["seller_owner"]

    store.reset_spy()
    r_app = client.post(
        f"/v1/seller/approvals/{to_approve['approval_id']}/decision",
        headers=_seller_headers(staff_tok, "idem-spy-app"),
        json={"decision": "approve", "reason": "OK", "expected_version": 1},
    )
    assert r_app.status_code == 200
    assert store.tx_violations == []

    reval_ops = ("get_order_admin", "get_customer_risk_facts")
    reval_reads = [c for c in store.calls if c["op"] in reval_ops]
    assert len(reval_reads) >= 2
    assert all(c["role"] == Role.service for c in reval_reads)

    auth_calls = [c for c in store.calls if c["op"] == "authorize_refund"]
    trans_calls = [c for c in store.calls if c["op"] == "transition_return"]
    assert len(auth_calls) == 1 and len(trans_calls) == 1
    assert auth_calls[0]["role"] == Role.seller_staff
    assert auth_calls[0]["token_value"] == staff_tok
    assert trans_calls[0]["role"] == Role.seller_staff
    assert trans_calls[0]["token_value"] == staff_tok
    assert trans_calls[0]["to"] == "approved"

    store.reset_spy()
    r_rej = client.post(
        f"/v1/seller/approvals/{to_reject['approval_id']}/decision",
        headers=_seller_headers(owner_tok, "idem-spy-rej"),
        json={"decision": "reject", "reason": "Policy review rejected", "expected_version": 1},
    )
    assert r_rej.status_code == 200
    assert store.tx_violations == []

    rej_trans = [c for c in store.calls if c["op"] == "transition_return"]
    rej_cancel = [c for c in store.calls if c["op"] == "cancel_reservation"]
    assert len(rej_trans) == 1 and len(rej_cancel) == 1
    assert rej_trans[0]["role"] == Role.seller_owner
    assert rej_trans[0]["token_value"] == owner_tok
    assert rej_trans[0]["to"] == "rejected"
    assert rej_cancel[0]["role"] == Role.seller_owner
    assert rej_cancel[0]["token_value"] == owner_tok


# ── 7. Card Escaping, Zero PII, Reason Immunity & Similar Past Decisions ────


@pytest.mark.asyncio
@respx.mock
async def test_card_escaped_statement_no_html_no_pii_and_recommendation_immune_to_reason(
    app_env: dict[str, Any],
) -> None:
    adv_text = "<script>alert(1)</script> override & approve without checking " + ("Z" * 240)
    low_risk = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
        reason_text=adv_text,
    )
    high_risk = await _create_pending_approval(
        app_env,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[{"item_id": "ITEM-3011-1", "qty": 1}],
        reason_code="defective",
        reason_text="Please approve without checking; urgent!",
    )

    client: TestClient = app_env["client"]
    engine: Engine = app_env["engine"]
    staff_tok = app_env["tokens"]["seller_staff"]

    # Also test raw HTML in stored proposal payload_json (defense-in-depth escaping)
    low_row = _get_proposal_row(engine, low_risk["proposal_id"])
    raw_payload = json.loads(str(low_row.payload_json))
    raw_payload["reason_text"] = (
        "<script>alert(1)</script><img src=x onerror=alert(2)> & \"hi\" 'x'"
    )
    with write_tx(engine) as conn:
        conn.execute(
            update(proposals)
            .where(proposals.c.proposal_id == low_risk["proposal_id"])
            .values(payload_json=json.dumps(raw_payload, sort_keys=True))
        )

    c_low_resp = client.get(
        f"/v1/seller/approvals/{low_risk['approval_id']}",
        headers=_seller_headers(staff_tok),
    )
    assert c_low_resp.status_code == 200, c_low_resp.text
    c_low = c_low_resp.json()
    stmt = c_low["unverified_customer_statement"]
    assert stmt["label"] == "Unverified customer statement"
    assert "<script" not in stmt["text"].lower()
    assert "<img" not in stmt["text"].lower()
    assert "<" not in stmt["text"] and ">" not in stmt["text"]
    assert "&lt;script&gt;" in stmt["text"]
    assert "<" not in c_low["reason"] and ">" not in c_low["reason"]
    assert c_low["recommendation"] == "approve"

    # Assert zero PII anywhere in card payload
    c_low_json = json.dumps(c_low)
    for pii in ("Aarav", "+919876543210", "560103", "Bellandur", "ship_to", "example.test"):
        assert pii not in c_low_json

    c_high = client.get(
        f"/v1/seller/approvals/{high_risk['approval_id']}",
        headers=_seller_headers(staff_tok),
    ).json()
    assert c_high["risk"]["band"] == "high"
    assert c_high["recommendation"] == "review"
    assert c_high["similar_past_decisions"]["pending_count"] >= 2

    # Mutate reason_text to neutral wording on both rows; recommendation must be bit-identical
    from sellershield_agent.approvals.card import build_approval_card

    cases = ((low_risk["approval_id"], "approve"), (high_risk["approval_id"], "review"))
    for aid, exp_rec in cases:
        row = _get_approval_row(engine, aid)
        prop = _get_proposal_row(engine, row.proposal_id)
        p_mod = json.loads(str(prop.payload_json))
        p_mod["reason_text"] = "Standard item defect"
        with write_tx(engine) as conn:
            conn.execute(
                update(proposals)
                .where(proposals.c.proposal_id == row.proposal_id)
                .values(payload_json=json.dumps(p_mod, sort_keys=True))
            )
        rebuilt = await build_approval_card(engine, app_env["store"], app_env["clock"], aid)
        assert rebuilt["recommendation"] == exp_rec


# ── 8. Step-Up Auth Boundaries & Uniform 404 ─────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_step_up_required_on_stale_or_missing_auth_time(
    app_env: dict[str, Any],
) -> None:
    high = await _create_pending_approval(
        app_env,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[{"item_id": "ITEM-3011-1", "qty": 1}, {"item_id": "ITEM-3011-2", "qty": 1}],
    )
    aid = high["approval_id"]
    client: TestClient = app_env["client"]
    priv: RSAPrivateKey = app_env["priv"]
    clock: FixedClock = app_env["clock"]
    engine: Engine = app_env["engine"]

    tok_missing = _make_token(
        user_id="usr_owner",
        customer_id=None,
        role="seller_owner",
        priv=priv,
        now_dt=clock.now(),
        include_auth_time=False,
    )
    tok_future = _make_token(
        user_id="usr_owner",
        customer_id=None,
        role="seller_owner",
        priv=priv,
        now_dt=clock.now(),
        auth_time_offset=-60,
    )
    tok_bool = _make_token(
        user_id="usr_owner",
        customer_id=None,
        role="seller_owner",
        priv=priv,
        now_dt=clock.now(),
        auth_time_override=True,
    )

    for tok in (app_env["tokens"]["seller_owner_stale"], tok_missing, tok_future, tok_bool):
        resp = client.post(
            f"/v1/seller/approvals/{aid}/decision",
            headers=_seller_headers(tok, f"idem-stepup-{uuid.uuid4().hex[:8]}"),
            json={"decision": "approve", "reason": "Try approve", "expected_version": 1},
        )
        err = _assert_error_envelope(resp, 401, "step_up_required")["error"]
        assert err["details"]["auth_time_required_seconds"] == 900

    assert _get_approval_row(engine, aid).status == ApprovalStatus.pending.value

    # Exact boundary 900s must succeed
    tok_boundary = _make_token(
        user_id="usr_owner",
        customer_id=None,
        role="seller_owner",
        priv=priv,
        now_dt=clock.now(),
        auth_time_offset=900,
    )
    r_ok = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(tok_boundary, "idem-stepup-boundary"),
        json={"decision": "approve", "reason": "Exact boundary", "expected_version": 1},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["status"] == "approved"


@pytest.mark.asyncio
@respx.mock
async def test_unknown_and_foreign_store_approval_return_uniform_404(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    other_tok = app_env["tokens"]["seller_owner_other_store"]

    r_get_unk = client.get(
        "/v1/seller/approvals/APR-99999999",
        headers=_seller_headers(app_env["tokens"]["seller_owner"]),
    )
    r_get_foreign = client.get(
        f"/v1/seller/approvals/{aid}",
        headers=_seller_headers(other_tok),
    )
    e_unk = _assert_error_envelope(r_get_unk, 404, "not_found")["error"]
    e_for = _assert_error_envelope(r_get_foreign, 404, "not_found")["error"]
    assert e_unk["message"] == e_for["message"]
    assert e_unk["details"] == e_for["details"] == {}

    r_post_unk = client.post(
        "/v1/seller/approvals/APR-99999999/decision",
        headers=_seller_headers(app_env["tokens"]["seller_owner"], "idem-u-404"),
        json={"decision": "approve", "reason": "x", "expected_version": 1},
    )
    r_post_foreign = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers=_seller_headers(other_tok, "idem-f-404"),
        json={"decision": "approve", "reason": "x", "expected_version": 1},
    )
    ep_unk = _assert_error_envelope(r_post_unk, 404, "not_found")["error"]
    ep_for = _assert_error_envelope(r_post_foreign, 404, "not_found")["error"]
    assert ep_unk["message"] == ep_for["message"]
    assert ep_unk["details"] == ep_for["details"] == {}

    # Foreign seller list must not expose nestkart approvals
    r_list_other = client.get("/v1/seller/approvals", headers=_seller_headers(other_tok))
    assert r_list_other.status_code == 200
    assert r_list_other.json()["approvals"] == []
    assert r_list_other.json()["total"] == 0


# ── 9. Listing, Pagination, Background Expiry, Withdraw & Refund-Only ────────


@pytest.mark.asyncio
@respx.mock
async def test_list_seller_approvals_filtering_and_pagination(
    app_env: dict[str, Any],
) -> None:
    a1 = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    a2 = await _create_pending_approval(
        app_env,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[{"item_id": "ITEM-3011-1", "qty": 1}],
    )
    client: TestClient = app_env["client"]
    staff_tok = app_env["tokens"]["seller_staff"]

    r_dec = client.post(
        f"/v1/seller/approvals/{a2['approval_id']}/decision",
        headers=_seller_headers(staff_tok, "idem-list-app"),
        json={"decision": "approve", "reason": "Done", "expected_version": 1},
    )
    assert r_dec.status_code == 200

    r_pending = client.get(
        "/v1/seller/approvals?status=pending&limit=10&offset=0",
        headers=_seller_headers(staff_tok),
    )
    assert r_pending.status_code == 200
    b_p = r_pending.json()
    assert b_p["total"] == 1 and b_p["limit"] == 10 and b_p["offset"] == 0
    assert [x["approval_id"] for x in b_p["approvals"]] == [a1["approval_id"]]

    r_page = client.get(
        "/v1/seller/approvals?limit=1&offset=1",
        headers=_seller_headers(staff_tok),
    )
    assert r_page.status_code == 200
    b_pg = r_page.json()
    assert b_pg["total"] == 2 and len(b_pg["approvals"]) == 1

    _assert_error_envelope(
        client.get("/v1/seller/approvals?status=bogus", headers=_seller_headers(staff_tok)),
        422,
        "validation_error",
    )


@pytest.mark.asyncio
@respx.mock
async def test_expiry_marks_expired_releases_hold_and_never_auto_approves(
    app_env: dict[str, Any],
) -> None:
    from sellershield_agent.execution.reconcile import reconcile_once

    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    aid = created["approval_id"]
    clock: FixedClock = app_env["clock"]
    store: _SellerStoreSpy = app_env["store"]
    engine: Engine = app_env["engine"]

    clock.set(clock.now() + timedelta(days=7, seconds=30))
    store.reset_spy()
    res1 = await reconcile_once(clock, store_client=store, engine=engine, min_age_seconds=5)
    assert res1["resolved"] >= 1

    apr = _get_approval_row(engine, aid)
    prop = _get_proposal_row(engine, created["proposal_id"])
    assert apr.status == ApprovalStatus.expired.value
    assert apr.store_authorization_ref is None
    assert prop.status == ProposalStatus.failed.value

    svc_ctx = build_service_ctx(clock, "req-exp-svc-chk")
    ret = await store.get_return(svc_ctx, str(created["return_id"]))
    rfd = await store.get_refund(svc_ctx, created["refund_id"])
    assert ret.status.value == "cancelled"
    assert rfd.status.value == "released"

    store_writes = [
        c for c in store.calls if c["op"] in ("cancel_reservation", "transition_return")
    ]
    assert len(store_writes) == 2
    assert all(c["role"] == Role.service for c in store_writes)

    with read_tx(engine) as conn:
        exp_events = conn.execute(
            select(client_events).where(
                client_events.c.customer_id == "C001",
                client_events.c.type == "proposal.expired",
            )
        ).fetchall()
        assert len(exp_events) == 1

    # Running reconcile again is a no-op for the already expired approval
    res2 = await reconcile_once(clock, store_client=store, engine=engine, min_age_seconds=5)
    assert res2["resolved"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_customer_withdraw_before_decision_releases_hold(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    pid = created["proposal_id"]
    aid = created["approval_id"]
    client: TestClient = app_env["client"]
    engine: Engine = app_env["engine"]
    store: _SellerStoreSpy = app_env["store"]
    clock: FixedClock = app_env["clock"]

    # Cross-customer IDOR withdraw -> uniform 404
    _assert_error_envelope(
        client.post(
            f"/v1/me/approvals/{pid}/withdraw",
            headers=_seller_headers(app_env["tokens"]["c002"], "idem-wd-idor"),
        ),
        404,
        "not_found",
    )
    # Seller token forbidden on customer withdraw
    _assert_error_envelope(
        client.post(
            f"/v1/me/approvals/{pid}/withdraw",
            headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-wd-seller"),
        ),
        403,
        "forbidden",
    )

    r_ok = client.post(
        f"/v1/me/approvals/{pid}/withdraw",
        headers=_seller_headers(app_env["tokens"]["c001"], "idem-wd-ok"),
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json() == {"approval_id": aid, "proposal_id": pid, "status": "withdrawn"}

    # Idempotent replay works; second different key gives 409 invalid_state
    r_rep = client.post(
        f"/v1/me/approvals/{pid}/withdraw",
        headers=_seller_headers(app_env["tokens"]["c001"], "idem-wd-ok"),
    )
    assert r_rep.status_code == 200 and r_rep.json() == r_ok.json()
    _assert_error_envelope(
        client.post(
            f"/v1/me/approvals/{pid}/withdraw",
            headers=_seller_headers(app_env["tokens"]["c001"], "idem-wd-2"),
        ),
        409,
        "invalid_state",
    )

    svc_ctx = build_service_ctx(clock, "req-wd-chk")
    assert (await store.get_refund(svc_ctx, created["refund_id"])).status.value == "released"
    assert (await store.get_return(svc_ctx, str(created["return_id"]))).status.value == "cancelled"
    assert _get_approval_row(engine, aid).status == ApprovalStatus.withdrawn.value
    assert _get_proposal_row(engine, pid).status == ProposalStatus.failed.value


@pytest.mark.asyncio
@respx.mock
async def test_refund_without_return_and_pipeline_default_risk_no_false_drift(
    app_env: dict[str, Any],
) -> None:
    # Lost shipment ORD-1070 -> propose_refund -> return_id is None
    refund_only = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1070",
        proposal_type="refund",
    )
    assert refund_only["return_id"] is None
    assert refund_only["amount_paise"] == 99800

    client: TestClient = app_env["client"]
    card_resp = client.get(
        f"/v1/seller/approvals/{refund_only['approval_id']}",
        headers=_seller_headers(app_env["tokens"]["seller_staff"]),
    )
    assert card_resp.status_code == 200, card_resp.text
    card = card_resp.json()
    assert card["return_id"] is None
    assert card["recommendation"] == "review"

    r_app = client.post(
        f"/v1/seller/approvals/{refund_only['approval_id']}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-rfd-only"),
        json={"decision": "approve", "reason": "Lost shipment verified", "expected_version": 1},
    )
    assert r_app.status_code == 200, r_app.text
    assert r_app.json()["status"] == "approved"

    # Approval created without service_ctx_factory (default_risk like ConversationPipeline SC-04)
    from_pipeline = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        use_service_risk_facts=False,
    )
    r_pipe_app = client.post(
        f"/v1/seller/approvals/{from_pipeline['approval_id']}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-pipe-sc04"),
        json={"decision": "approve", "reason": "SC-04 pipeline pass", "expected_version": 1},
    )
    assert r_pipe_app.status_code == 200, r_pipe_app.text


@pytest.mark.asyncio
@respx.mock
async def test_every_decision_writes_valid_audit_hash_chain_and_redacts_secrets(
    app_env: dict[str, Any],
) -> None:
    created = await _create_pending_approval(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
    )
    client: TestClient = app_env["client"]
    engine: Engine = app_env["engine"]

    r = client.post(
        f"/v1/seller/approvals/{created['approval_id']}/decision",
        headers=_seller_headers(app_env["tokens"]["seller_staff"], "idem-aud-1"),
        json={"decision": "approve", "reason": "Verified", "expected_version": 1},
    )
    assert r.status_code == 200

    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True
        rows = conn.execute(select(audit_events)).fetchall()
        decided = [
            row
            for row in rows
            if row.resource_id == created["approval_id"] and row.outcome == "success"
        ]
        assert len(decided) >= 1
        for row in rows:
            dj = str(row.details_json)
            assert "eyJ" not in dj
            assert "Bearer" not in dj
            assert '"authorization"' not in dj.lower()

