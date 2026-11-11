"""Step A tests for T2.4 confirm and cancel handlers.

Covers TASK T2.4 and Spec docs/spec/02-agent.md sections 2, 11, 13, 21:
- Confirm binds customer click to exact proposal_hash via constant-time compare (hash_mismatch).
- Expired confirm & cancel mark proposal expired and emit proposal_confirmed/cancelled audit.
- Atomic status transition proposed -> confirmed -> executing with version/status WHERE guard.
- Revalidation fetches order & refundable with customer token under current active policy,
  detects order/refundable drift and v1->v3 policy shifts, writes zero store mutations, and
  returns 409 stale_state with sanitized fresh_preview.
- Executor protocol invoked at most once; supports executing, succeeded, pending_approval,
  unknown (including exception -> unknown), and failed, plus return_id/approval_id.
- 20 parallel confirms (SC-14) yield exactly one winner and one execution.
- Chat messages ('yes', 'confirm', 'I agree', 'ok') never confirm proposals.
- proposal_confirmed audit rows exist with valid hash chain for success, denied, and stale.
- Customer-facing responses and fresh_preview payloads never contain forbidden wording.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
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
from pydantic import SecretStr
from sqlalchemy import func, select
from starlette.testclient import TestClient

from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.audit.models import audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, read_tx, reset_engine
from sellershield_agent.domain.models import ReturnItem, ReturnReasonCode, ReturnResolution
from sellershield_agent.domain.state import ProposalStatus, proposals
from sellershield_agent.main import create_app
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scenarios" / "fixtures" / "store.json"
)
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
FORBIDDEN_CUSTOMER_RE = re.compile(
    r"\b(?:limits?|thresholds?|risks?|scores?|staff|owner|auto(?:matic|nomy)?)\b",
    re.IGNORECASE,
)


def _create_rsa_key_pair(
    kid: str = "confirm-t24-key",
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
    kid: str = "confirm-t24-key",
    exp_seconds: int = 3600,
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
    if customer_id is not None:
        payload["customer_id"] = customer_id
    return str(jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid, "alg": "RS256"}))


class _FakeExecutor:
    """Thread-safe configurable fake Executor for T2.4 tests."""

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.observed_row_states: list[tuple[str | None, str | None]] = []
        self.status: str = "succeeded"
        self.return_id: str | None = "RET-7001"
        self.approval_id: str | None = None
        self.message_key: str | None = None
        self.raise_exc: Exception | None = None
        self.delay_seconds: float = 0.0

    def reset(
        self,
        *,
        status: str = "succeeded",
        return_id: str | None = "RET-7001",
        approval_id: str | None = None,
        message_key: str | None = None,
        raise_exc: Exception | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        with self._lock:
            self.calls.clear()
            self.observed_row_states.clear()
            self.status = status
            self.return_id = return_id
            self.approval_id = approval_id
            self.message_key = message_key
            self.raise_exc = raise_exc
            self.delay_seconds = delay_seconds

    async def execute(
        self,
        customer_ctx: RequestContext,
        proposal: Any,
        *,
        clock: Any,
        idempotency_key: str,
    ) -> Any:
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        obs_status: str | None = None
        obs_confirmed_at: str | None = None
        if self._engine is not None:
            with read_tx(self._engine) as conn:
                row = conn.execute(
                    select(proposals.c.status, proposals.c.confirmed_at).where(
                        proposals.c.proposal_id == proposal.proposal_id
                    )
                ).fetchone()
                if row is not None:
                    obs_status = str(row.status)
                    obs_confirmed_at = (
                        str(row.confirmed_at) if row.confirmed_at is not None else None
                    )
        token_secret = customer_ctx.token.get_secret_value() if customer_ctx.token else None
        with self._lock:
            self.observed_row_states.append((obs_status, obs_confirmed_at))
            self.calls.append(
                {
                    "customer_id": customer_ctx.principal.customer_id,
                    "has_token": token_secret is not None,
                    "proposal_id": proposal.proposal_id,
                    "order_id": proposal.order_id,
                    "policy_version": proposal.policy_version,
                    "idempotency_key": idempotency_key,
                    "clock_iso": clock.now().isoformat(),
                }
            )
            exc = self.raise_exc
            status = self.status
            ret_id = self.return_id
            apr_id = self.approval_id
            msg_key = self.message_key

        if exc is not None:
            raise exc

        from sellershield_agent.proposals.executor import ExecutionResult

        return ExecutionResult(
            status=status,
            return_id=ret_id,
            approval_id=apr_id,
            message_key=msg_key,
        )


class _SpyStoreClient:
    """Delegating StoreClient wrapper recording reads, customer token forwarding, and writes."""

    def __init__(self, inner: FixtureStoreClient) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.read_calls: list[str] = []
        self.write_calls: list[str] = []
        self.token_presence: list[bool] = []

    def reset_counts(self) -> None:
        with self._lock:
            self.read_calls.clear()
            self.write_calls.clear()
            self.token_presence.clear()

    def _record_read(self, name: str, ctx: RequestContext) -> None:
        with self._lock:
            self.read_calls.append(name)
            self.token_presence.append(
                ctx.token is not None and bool(ctx.token.get_secret_value())
            )

    def _record_write(self, name: str) -> None:
        with self._lock:
            self.write_calls.append(name)

    async def list_orders(self, ctx: RequestContext) -> Any:
        self._record_read("list_orders", ctx)
        return await self._inner.list_orders(ctx)

    async def get_order(self, ctx: RequestContext, order_id: str) -> Any:
        self._record_read("get_order", ctx)
        return await self._inner.get_order(ctx, order_id)

    async def get_shipment(self, ctx: RequestContext, order_id: str) -> Any:
        self._record_read("get_shipment", ctx)
        return await self._inner.get_shipment(ctx, order_id)

    async def get_refundable(
        self,
        ctx: RequestContext,
        order_id: str,
        items: list[tuple[str, int]] | None = None,
    ) -> Any:
        self._record_read("get_refundable", ctx)
        return await self._inner.get_refundable(ctx, order_id, items=items)

    async def list_returns(self, ctx: RequestContext, order_id: str) -> Any:
        self._record_read("list_returns", ctx)
        return await self._inner.list_returns(ctx, order_id)

    async def list_refunds(self, ctx: RequestContext, order_id: str) -> Any:
        self._record_read("list_refunds", ctx)
        return await self._inner.list_refunds(ctx, order_id)

    async def get_customer_risk_facts(self, service_ctx: RequestContext, customer_id: str) -> Any:
        self._record_read("get_customer_risk_facts", service_ctx)
        return await self._inner.get_customer_risk_facts(service_ctx, customer_id)

    async def create_return(self, *args: Any, **kwargs: Any) -> Any:
        self._record_write("create_return")
        return await self._inner.create_return(*args, **kwargs)

    async def get_return(self, ctx: RequestContext, return_id: str) -> Any:
        self._record_read("get_return", ctx)
        return await self._inner.get_return(ctx, return_id)

    async def transition_return(self, *args: Any, **kwargs: Any) -> Any:
        self._record_write("transition_return")
        return await self._inner.transition_return(*args, **kwargs)

    async def reserve_refund(self, *args: Any, **kwargs: Any) -> Any:
        self._record_write("reserve_refund")
        return await self._inner.reserve_refund(*args, **kwargs)

    async def authorize_refund(self, *args: Any, **kwargs: Any) -> Any:
        self._record_write("authorize_refund")
        return await self._inner.authorize_refund(*args, **kwargs)

    async def release_refund(self, *args: Any, **kwargs: Any) -> Any:
        self._record_write("release_refund")
        return await self._inner.release_refund(*args, **kwargs)

    async def cancel_reservation(self, *args: Any, **kwargs: Any) -> Any:
        self._record_write("cancel_reservation")
        return await self._inner.cancel_reservation(*args, **kwargs)

    async def get_refund(self, ctx: RequestContext, refund_id: str) -> Any:
        self._record_read("get_refund", ctx)
        return await self._inner.get_refund(ctx, refund_id)

    async def get_order_admin(self, service_ctx: RequestContext, order_id: str) -> Any:
        self._record_read("get_order_admin", service_ctx)
        return await self._inner.get_order_admin(service_ctx, order_id)

    async def get_settings(self, service_ctx: RequestContext) -> Any:
        self._record_read("get_settings", service_ctx)
        return await self._inner.get_settings(service_ctx)

    async def get_customer_refund_summary(
        self,
        service_ctx: RequestContext,
        customer_id: str,
        days: int = 30,
    ) -> Any:
        self._record_read("get_customer_refund_summary", service_ctx)
        return await self._inner.get_customer_refund_summary(service_ctx, customer_id, days=days)

    def get_clock(self) -> Any:
        return self._inner.get_clock()


@pytest.fixture
def confirm_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "confirm_handler_t24.db"
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

    priv, _, jwk = _create_rsa_key_pair()
    respx.get(JWKS_URL).respond(json={"keys": [jwk]})

    fixed_clock = FixedClock(T0_BASE)
    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: fixed_clock

    raw_store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=fixed_clock)
    spy_store = _SpyStoreClient(raw_store)
    fake_executor = _FakeExecutor(engine=get_engine())

    from sellershield_agent.api import proposals as proposals_api

    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: spy_store
    if hasattr(proposals_api, "get_proposal_executor"):
        app.dependency_overrides[proposals_api.get_proposal_executor] = lambda: fake_executor

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
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        yield {
            "app": app,
            "client": client,
            "clock": fixed_clock,
            "raw_store": raw_store,
            "store": spy_store,
            "executor": fake_executor,
            "engine": get_engine(),
            "tokens": tokens,
        }

    app.dependency_overrides.clear()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()
    reset_engine()
    get_settings.cache_clear()


async def _propose_return(
    *,
    engine: Any,
    store: Any,
    clock: FixedClock,
    customer_id: str,
    user_id: str,
    order_id: str,
    items: list[dict[str, Any]],
    reason_code: str = "changed_mind",
    policy_version: str = "v3",
    with_risk_service_ctx: bool = True,
) -> dict[str, Any]:
    policy = load_policy(policy_version)

    def _service_ctx_factory(req_id: str) -> RequestContext:
        return RequestContext(
            principal=Principal(
                user_id="svc_agent",
                role=Role.service,
                customer_id=None,
                auth_time=int(clock.now().timestamp()),
                jti=f"jti-svc-{uuid.uuid4().hex[:8]}",
                token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
            ),
            request_id=req_id,
        )

    gateway = ToolGateway(
        store_client=store,
        engine=engine,
        clock=clock,
        policy=policy,
        service_ctx_factory=_service_ctx_factory if with_risk_service_ctx else None,
    )
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
            "reason_text": "Need return",
            "resolution": "refund",
        },
        allowed={"propose_return", "propose_refund"},
        session_id="ses_t24_test",
    )
    assert res.ok is True and isinstance(res.data, dict)
    assert res.data.get("type") == "confirmation"
    return res.data


async def _propose_refund(
    *,
    engine: Any,
    store: Any,
    clock: FixedClock,
    customer_id: str,
    user_id: str,
    order_id: str,
    items: list[dict[str, Any]],
    cause: str = "lost",
    policy_version: str = "v3",
) -> dict[str, Any]:
    policy = load_policy(policy_version)

    def _service_ctx_factory(req_id: str) -> RequestContext:
        return RequestContext(
            principal=Principal(
                user_id="svc_agent",
                role=Role.service,
                customer_id=None,
                auth_time=int(clock.now().timestamp()),
                jti=f"jti-svc-{uuid.uuid4().hex[:8]}",
                token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
            ),
            request_id=req_id,
        )

    gateway = ToolGateway(
        store_client=store,
        engine=engine,
        clock=clock,
        policy=policy,
        service_ctx_factory=_service_ctx_factory,
    )
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
        name="propose_refund",
        raw_args={
            "order_id": order_id,
            "items": items,
            "cause": cause,
        },
        allowed={"propose_return", "propose_refund"},
        session_id="ses_t24_refund_test",
    )
    assert res.ok is True and isinstance(res.data, dict)
    assert res.data.get("type") == "confirmation"
    return res.data


def _get_proposal_row(engine: Any, proposal_id: str) -> Any:
    with read_tx(engine) as conn:
        return conn.execute(
            select(proposals).where(proposals.c.proposal_id == proposal_id)
        ).fetchone()


def _get_proposal_confirm_audits(engine: Any, proposal_id: str) -> list[Any]:
    with read_tx(engine) as conn:
        rows = conn.execute(
            select(audit_events)
            .where(
                audit_events.c.resource_type == "proposals",
                audit_events.c.resource_id == proposal_id,
                audit_events.c.action == "proposal_confirmed",
            )
            .order_by(audit_events.c.seq.asc())
        ).fetchall()
    return list(rows)


def _assert_no_forbidden_customer_words(payload: Any) -> None:
    dumped = json.dumps(payload)
    match = FORBIDDEN_CUSTOMER_RE.search(dumped)
    assert match is None, f"Forbidden customer word '{match.group(0)}' leaked in {dumped}"


# ── 1. Executor Invocation & Return/Approval Status Mapping ───────────────────


@pytest.mark.asyncio
@respx.mock
async def test_valid_confirm_calls_executor_once_and_maps_all_executor_statuses(
    confirm_env: dict[str, Any],
) -> None:
    from sellershield_agent.api import proposals as proposals_api
    from sellershield_agent.proposals.confirm import confirm_proposal

    assert hasattr(proposals_api, "get_proposal_executor")

    from sellershield_agent.proposals.executor import Executor

    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    store: _SpyStoreClient = confirm_env["store"]
    executor: _FakeExecutor = confirm_env["executor"]
    c002_tok = confirm_env["tokens"]["c002"]

    assert isinstance(executor, Executor)
    sig_params = list(inspect.signature(confirm_proposal).parameters.values())
    assert len(sig_params) >= 3
    assert all(p.default is not inspect.Parameter.empty for p in sig_params[3:])

    # Case 1: succeeded + return_id via HTTP POST /v1/proposals/{id}/confirm
    card_ok = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    executor.reset(status="succeeded", return_id="RET-7001", approval_id=None)
    store.reset_counts()

    resp = client.post(
        f"/v1/proposals/{card_ok['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-exec-ok"},
        json={"proposal_hash": card_ok["proposal_hash"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_no_forbidden_customer_words(body)
    assert body["proposal_id"] == card_ok["proposal_id"]
    assert body["status"] == "succeeded"
    assert body["return_id"] == "RET-7001"
    assert body["approval_id"] is None
    assert len(executor.calls) == 1
    assert executor.calls[0]["has_token"] is True
    assert executor.observed_row_states[0][0] == ProposalStatus.executing.value
    assert executor.observed_row_states[0][1] is not None
    assert "get_order" in store.read_calls
    assert "get_refundable" in store.read_calls
    assert all(store.token_presence)
    assert len(store.write_calls) == 0

    # Same Idempotency-Key replays finalized response WITHOUT calling executor again
    replay_resp = client.post(
        f"/v1/proposals/{card_ok['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-exec-ok"},
        json={"proposal_hash": card_ok["proposal_hash"]},
    )
    assert replay_resp.status_code == 200
    assert replay_resp.json() == body
    assert len(executor.calls) == 1

    row_ok = _get_proposal_row(engine, card_ok["proposal_id"])
    assert row_ok is not None and row_ok.status == ProposalStatus.succeeded.value

    # Case 2: pending_approval + approval_id via direct confirm_proposal(ctx, id, hash)
    card_appr = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    clock.set(T0_BASE + timedelta(minutes=2))
    executor.reset(status="pending_approval", return_id=None, approval_id="APR-12345678")
    c001_ctx = RequestContext(
        principal=Principal(
            user_id="usr_c001",
            role=Role.customer,
            customer_id="C001",
            auth_time=int(clock.now().timestamp()),
            jti="jti-direct-c001",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-direct-appr",
        token=SecretStr("tok-direct-c001"),
    )
    direct_res = await confirm_proposal(
        c001_ctx,
        card_appr["proposal_id"],
        card_appr["proposal_hash"],
        engine=engine,
        store_client=store,
        executor=executor,
        clock=clock,
        idempotency_key="k-direct-appr",
    )
    assert direct_res.status == "pending_approval"
    assert direct_res.approval_id == "APR-12345678"
    assert direct_res.return_id is None
    assert len(executor.calls) == 1
    assert executor.calls[0]["has_token"] is True
    assert executor.observed_row_states[0][0] == ProposalStatus.executing.value
    assert executor.observed_row_states[0][1] is not None
    row_appr = _get_proposal_row(engine, card_appr["proposal_id"])
    assert row_appr is not None and row_appr.status == ProposalStatus.executing.value

    # Case 3..6: executing, failed, unknown (returned and via unhandled timeout)
    status_cases: list[tuple[str, str, str | None, str | None, Exception | None]] = [
        ("executing", "executing", None, None, None),
        ("failed", "failed", None, None, None),
        ("unknown", "unknown", None, None, None),
        ("timeout_exc", "unknown", None, None, TimeoutError("simulated gateway timeout")),
    ]
    test_targets = [
        ("C002", "usr_c002", confirm_env["tokens"]["c002"], "ORD-2031", "ITEM-2031-2"),
        ("C005", "usr_c005", None, "ORD-5001", "ITEM-5001-1"),
        ("C003", "usr_c003", None, "ORD-3011", "ITEM-3011-1"),
        ("C003", "usr_c003", None, "ORD-3011", "ITEM-3011-2"),
    ]
    for idx, (cid, uid, _tok, oid, iid) in enumerate(test_targets):
        label, exp_status, ret_id, apr_id, exc = status_cases[idx]
        card = await _propose_return(
            engine=engine,
            store=confirm_env["raw_store"],
            clock=clock,
            customer_id=cid,
            user_id=uid,
            order_id=oid,
            items=[{"item_id": iid, "qty": 1}],
            reason_code="changed_mind",
            with_risk_service_ctx=False,
        )
        executor.reset(status=exp_status, return_id=ret_id, approval_id=apr_id, raise_exc=exc)
        ctx = RequestContext(
            principal=Principal(
                user_id=uid,
                role=Role.customer,
                customer_id=cid,
                auth_time=int(clock.now().timestamp()),
                jti=f"jti-{label}",
                token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
            ),
            request_id=f"req-{label}",
            token=SecretStr(f"tok-direct-{label}"),
        )
        confirm_kwargs: dict[str, Any] = {
            "engine": engine,
            "store_client": store,
            "executor": executor,
            "clock": clock,
        }
        if idx > 0:
            confirm_kwargs["idempotency_key"] = f"k-{label}"
        res = await confirm_proposal(
            ctx,
            card["proposal_id"],
            card["proposal_hash"],
            **confirm_kwargs,
        )
        assert res.status == exp_status
        row = _get_proposal_row(engine, card["proposal_id"])
        assert row is not None
        assert row.status == exp_status
        assert executor.observed_row_states[0][0] == ProposalStatus.executing.value
        assert executor.observed_row_states[0][1] is not None

    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True


# ── 2. Wrong Hash / Hash Copied From Another Proposal ─────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_confirm_wrong_hash_and_copied_hash_409_hash_mismatch_and_audit(
    confirm_env: dict[str, Any],
) -> None:
    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    executor: _FakeExecutor = confirm_env["executor"]
    c002_tok = confirm_env["tokens"]["c002"]

    card_a = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    card_b = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-1", "qty": 1}],
    )
    pid = card_a["proposal_id"]
    wrong_hash = ("0" if card_a["proposal_hash"][0] != "0" else "1") + card_a["proposal_hash"][1:]

    # 1. Wrong hash
    r_wrong = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-bad-hash-1"},
        json={"proposal_hash": wrong_hash},
    )
    assert r_wrong.status_code == 409
    err_w = r_wrong.json()["error"]
    assert err_w["code"] == "invalid_state"
    assert err_w["details"]["reason"] == "hash_mismatch"
    _assert_no_forbidden_customer_words(r_wrong.json())

    # 2. Hash copied from another proposal owned by the same customer
    r_copied = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-bad-hash-2"},
        json={"proposal_hash": card_b["proposal_hash"]},
    )
    assert r_copied.status_code == 409
    assert r_copied.json()["error"]["code"] == "invalid_state"
    assert r_copied.json()["error"]["details"]["reason"] == "hash_mismatch"

    # 3. Non-ASCII Unicode hash rejected cleanly as 409 hash_mismatch (no TypeError)
    r_unicode = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-bad-hash-3"},
        json={"proposal_hash": "\u00e9" * 64},
    )
    assert r_unicode.status_code == 409
    assert r_unicode.json()["error"]["code"] == "invalid_state"
    assert r_unicode.json()["error"]["details"]["reason"] == "hash_mismatch"

    assert len(executor.calls) == 0
    row = _get_proposal_row(engine, pid)
    assert row is not None and row.status == ProposalStatus.proposed.value

    audits = _get_proposal_confirm_audits(engine, pid)
    assert len(audits) == 3
    assert all(a.outcome == "denied" for a in audits)
    assert all(json.loads(a.details_json).get("reason") == "hash_mismatch" for a in audits)
    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True


# ── 3. Expiry Enforcement & Replay After Expiry ───────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_confirm_expired_proposal_marks_expired_409_and_writes_audit(
    confirm_env: dict[str, Any],
) -> None:
    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    executor: _FakeExecutor = confirm_env["executor"]
    c002_tok = confirm_env["tokens"]["c002"]

    card = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    pid = card["proposal_id"]

    clock.set(T0_BASE + timedelta(minutes=10))
    r1 = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-exp-1"},
        json={"proposal_hash": card["proposal_hash"]},
    )
    assert r1.status_code == 409
    err1 = r1.json()["error"]
    assert err1["code"] == "invalid_state"
    assert err1["details"]["reason"] == "expired"
    _assert_no_forbidden_customer_words(r1.json())

    row = _get_proposal_row(engine, pid)
    assert row is not None
    assert row.status == ProposalStatus.expired.value
    assert row.confirmed_at is None
    assert len(executor.calls) == 0

    # Replay an old confirm later after expiry -> still rejected as expired
    clock.set(T0_BASE + timedelta(minutes=25))
    r2 = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-exp-2"},
        json={"proposal_hash": card["proposal_hash"]},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "invalid_state"
    assert r2.json()["error"]["details"]["reason"] == "expired"

    audits = _get_proposal_confirm_audits(engine, pid)
    assert len(audits) >= 2
    assert all(a.outcome == "denied" for a in audits)
    assert all(json.loads(a.details_json).get("reason") == "expired" for a in audits)
    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True


# ── 4. Second Confirm Rejected Without Second Execution ───────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_second_confirm_returns_409_already_confirmed_and_single_execution(
    confirm_env: dict[str, Any],
) -> None:
    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    executor: _FakeExecutor = confirm_env["executor"]
    c002_tok = confirm_env["tokens"]["c002"]

    card = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    pid = card["proposal_id"]

    r1 = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-double-1"},
        json={"proposal_hash": card["proposal_hash"]},
    )
    assert r1.status_code == 200
    assert len(executor.calls) == 1

    r2 = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-double-2"},
        json={"proposal_hash": card["proposal_hash"]},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "invalid_state"
    assert r2.json()["error"]["details"]["reason"] == "already_confirmed"
    _assert_no_forbidden_customer_words(r2.json())
    assert len(executor.calls) == 1

    audits = _get_proposal_confirm_audits(engine, pid)
    outcomes = [a.outcome for a in audits]
    assert "success" in outcomes
    assert "denied" in outcomes
    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True


# ── 5. Cross-Customer 404 & Seller/Service 403 ────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_cross_customer_proposal_uniform_404_and_seller_token_403(
    confirm_env: dict[str, Any],
) -> None:
    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    executor: _FakeExecutor = confirm_env["executor"]

    card_c002 = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    pid = card_c002["proposal_id"]

    c001_tok = confirm_env["tokens"]["c001"]
    r_other = client.post(
        f"/v1/proposals/{pid}/confirm",
        headers={"Authorization": f"Bearer {c001_tok}", "Idempotency-Key": "k-x"},
        json={"proposal_hash": card_c002["proposal_hash"]},
    )
    r_missing = client.post(
        "/v1/proposals/PRP-00000000/confirm",
        headers={"Authorization": f"Bearer {c001_tok}", "Idempotency-Key": "k-y"},
        json={"proposal_hash": card_c002["proposal_hash"]},
    )
    assert r_other.status_code == 404
    assert r_missing.status_code == 404
    assert r_other.json()["error"]["code"] == "not_found"
    assert r_other.json()["error"]["message"] == r_missing.json()["error"]["message"]
    assert "C002" not in r_other.text

    for role_key in ("seller_staff", "seller_owner", "service"):
        r_role = client.post(
            f"/v1/proposals/{pid}/confirm",
            headers={
                "Authorization": f"Bearer {confirm_env['tokens'][role_key]}",
                "Idempotency-Key": f"k-role-{role_key}",
            },
            json={"proposal_hash": card_c002["proposal_hash"]},
        )
        assert r_role.status_code == 403
        assert r_role.json()["error"]["code"] == "forbidden"

    r_cxl_other = client.post(
        f"/v1/proposals/{pid}/cancel",
        headers={"Authorization": f"Bearer {c001_tok}", "Idempotency-Key": "k-cx"},
        json={},
    )
    r_cxl_missing = client.post(
        "/v1/proposals/PRP-00000000/cancel",
        headers={"Authorization": f"Bearer {c001_tok}", "Idempotency-Key": "k-cy"},
        json={},
    )
    assert r_cxl_other.status_code == 404
    assert r_cxl_missing.status_code == 404
    assert r_cxl_other.json()["error"]["code"] == "not_found"
    assert r_cxl_other.json()["error"]["message"] == r_cxl_missing.json()["error"]["message"]
    assert "C002" not in r_cxl_other.text

    for role_key in ("seller_staff", "seller_owner", "service"):
        r_cxl_role = client.post(
            f"/v1/proposals/{pid}/cancel",
            headers={
                "Authorization": f"Bearer {confirm_env['tokens'][role_key]}",
                "Idempotency-Key": f"k-cxl-role-{role_key}",
            },
            json={},
        )
        assert r_cxl_role.status_code == 403
        assert r_cxl_role.json()["error"]["code"] == "forbidden"

    assert len(executor.calls) == 0


# ── 6. 20 Parallel Confirms (SC-14) ───────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_20_parallel_confirms_sc14_single_winner_and_single_execution(
    confirm_env: dict[str, Any],
) -> None:
    from sellershield_agent.api import proposals as proposals_api

    assert hasattr(proposals_api, "get_proposal_executor")

    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    executor: _FakeExecutor = confirm_env["executor"]
    c002_tok = confirm_env["tokens"]["c002"]

    # Warm JWKS cache before launching concurrent worker threads
    assert client.get("/readyz").status_code == 200

    # Subcase A: 20 parallel confirms with distinct Idempotency-Keys
    card = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    executor.reset(status="succeeded", return_id="RET-RACE-1", delay_seconds=0.01)

    def _post_confirm(key: str) -> Any:
        return client.post(
            f"/v1/proposals/{card['proposal_id']}/confirm",
            headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": key},
            json={"proposal_hash": card["proposal_hash"]},
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(_post_confirm, f"sc14-distinct-{i}") for i in range(20)]
        resps = [f.result() for f in futs]

    ok_resps = [r for r in resps if r.status_code == 200]
    conflict_resps = [r for r in resps if r.status_code == 409]
    assert len(ok_resps) == 1, [(r.status_code, r.text) for r in resps]
    assert len(conflict_resps) == 19
    assert ok_resps[0].json()["status"] == "succeeded"
    assert ok_resps[0].json()["return_id"] == "RET-RACE-1"
    assert all(r.json()["error"]["code"] == "invalid_state" for r in conflict_resps)
    assert all(
        r.json()["error"]["details"]["reason"] == "already_confirmed" for r in conflict_resps
    )
    assert len(executor.calls) == 1
    row_race_a = _get_proposal_row(engine, card["proposal_id"])
    assert row_race_a is not None and row_race_a.status == ProposalStatus.succeeded.value
    audits_race_a = _get_proposal_confirm_audits(engine, card["proposal_id"])
    assert sum(1 for a in audits_race_a if a.outcome == "success") == 1
    assert sum(1 for a in audits_race_a if a.outcome == "denied") == 19

    # Subcase B: 20 parallel confirms sharing the SAME Idempotency-Key -> single execution
    card_same = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-2", "qty": 1}],
    )
    executor.reset(status="succeeded", return_id="RET-RACE-2", delay_seconds=0.01)

    def _post_same_key(_: int) -> Any:
        return client.post(
            f"/v1/proposals/{card_same['proposal_id']}/confirm",
            headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "sc14-shared-key"},
            json={"proposal_hash": card_same["proposal_hash"]},
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        same_resps = [f.result() for f in [pool.submit(_post_same_key, i) for i in range(20)]]

    assert all(r.status_code == 200 for r in same_resps)
    assert {r.json()["status"] for r in same_resps}.issubset({"executing", "succeeded"})
    assert len(executor.calls) == 1
    row_same = _get_proposal_row(engine, card_same["proposal_id"])
    assert row_same is not None and row_same.status == ProposalStatus.succeeded.value
    assert row_same.confirmed_at is not None

    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True


# ── 7. Stale Store State -> 409 stale_state + Fresh Preview + 0 Store Writes ──


@pytest.mark.asyncio
@respx.mock
async def test_order_changed_between_propose_and_confirm_stale_state_and_fresh_preview(
    confirm_env: dict[str, Any],
) -> None:
    from sellershield_agent.api import proposals as proposals_api

    assert hasattr(proposals_api, "get_proposal_executor")

    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    raw_store: FixtureStoreClient = confirm_env["raw_store"]
    store: _SpyStoreClient = confirm_env["store"]
    executor: _FakeExecutor = confirm_env["executor"]
    c002_tok = confirm_env["tokens"]["c002"]

    # Subcase A: Another tab consumes most of ORD-2031's refundable balance -> capped lower amount
    card = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-1", "qty": 1}],
        reason_code="changed_mind",
    )
    old_pid = card["proposal_id"]

    svc_ctx = RequestContext(
        principal=Principal(
            user_id="svc_tab",
            role=Role.service,
            customer_id=None,
            auth_time=int(clock.now().timestamp()),
            jti="jti-svc-tab",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-other-tab-refund",
    )
    await raw_store.reserve_refund(
        svc_ctx,
        order_id="ORD-2031",
        amount_paise=130000,
        return_id=None,
        method="original",
        idempotency_key="other-tab-reserve-1",
    )
    store.reset_counts()
    executor.reset(status="succeeded", return_id="RET-FRESH-1")

    r_stale = client.post(
        f"/v1/proposals/{old_pid}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-stale-cap"},
        json={"proposal_hash": card["proposal_hash"]},
    )
    assert r_stale.status_code == 409, r_stale.text
    err = r_stale.json()["error"]
    assert err["code"] == "invalid_state"
    assert err["details"]["reason"] == "stale_state"
    fresh = err["details"]["fresh_preview"]
    assert isinstance(fresh, dict)
    assert fresh["type"] == "confirmation"
    assert fresh["proposal_id"] != old_pid
    assert fresh["proposal_hash"] != card["proposal_hash"]
    assert fresh["amounts"]["total_paise"] == 34700
    assert fresh["capped"] is True
    _assert_no_forbidden_customer_words(r_stale.json())
    assert len(store.write_calls) == 0
    assert len(executor.calls) == 0

    stale_audits = _get_proposal_confirm_audits(engine, old_pid)
    assert any(
        json.loads(a.details_json).get("reason") == "stale_state" for a in stale_audits
    )

    # Confirming the fresh preview proposal succeeds cleanly
    r_fresh = client.post(
        f"/v1/proposals/{fresh['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-confirm-fresh"},
        json={"proposal_hash": fresh["proposal_hash"]},
    )
    assert r_fresh.status_code == 200, r_fresh.text
    assert r_fresh.json()["status"] == "succeeded"
    assert len(executor.calls) == 1

    # Subcase B: External return makes ORD-2032 ineligible (DENY) -> non-proposal fresh_preview
    card_deny = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
    )
    c002_ctx = RequestContext(
        principal=Principal(
            user_id="usr_c002",
            role=Role.customer,
            customer_id="C002",
            auth_time=int(clock.now().timestamp()),
            jti="jti-ext-ret",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-ext-ret",
    )
    await raw_store.create_return(
        c002_ctx,
        order_id="ORD-2032",
        items=[ReturnItem(item_id="ITEM-2032-1", qty=1)],
        reason_code=ReturnReasonCode.changed_mind.value,
        reason_text="Another tab submitted return",
        resolution=ReturnResolution.refund.value,
        idempotency_key="ext-tab-ret-1",
    )

    with read_tx(engine) as conn:
        prop_count_before = int(
            conn.execute(select(func.count()).select_from(proposals)).scalar_one()
        )
    store.reset_counts()
    executor.reset()

    r_deny_stale = client.post(
        f"/v1/proposals/{card_deny['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-stale-deny"},
        json={"proposal_hash": card_deny["proposal_hash"]},
    )
    assert r_deny_stale.status_code == 409
    deny_err = r_deny_stale.json()["error"]
    assert deny_err["code"] == "invalid_state"
    assert deny_err["details"]["reason"] == "stale_state"
    deny_preview = deny_err["details"]["fresh_preview"]
    assert deny_preview["proposal"] is None
    assert deny_preview["decision"] == "deny"
    _assert_no_forbidden_customer_words(r_deny_stale.json())
    assert len(store.write_calls) == 0
    assert len(executor.calls) == 0

    with read_tx(engine) as conn:
        prop_count_after = int(
            conn.execute(select(func.count()).select_from(proposals)).scalar_one()
        )
        assert prop_count_after == prop_count_before
        assert AuditService.verify_chain(conn).ok is True

    deny_audits = _get_proposal_confirm_audits(engine, card_deny["proposal_id"])
    assert any(
        a.outcome == "failed" and json.loads(a.details_json).get("reason") == "stale_state"
        for a in deny_audits
    )

    # Subcase C: Refund proposal at MAX_OPEN_PROPOSALS=3 cap + dedup replacement preview
    c001_tok = confirm_env["tokens"]["c001"]
    await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1061",
        items=[{"item_id": "ITEM-1061-1", "qty": 1}],
        reason_code="changed_mind",
    )
    card_ref = await _propose_refund(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1070",
        items=[{"item_id": "ITEM-1070-1", "qty": 2}],
        cause="lost",
    )
    await raw_store.reserve_refund(
        svc_ctx,
        order_id="ORD-1070",
        amount_paise=50000,
        return_id=None,
        method="original",
        idempotency_key="other-tab-reserve-1070",
    )
    store.reset_counts()
    executor.reset(status="pending_approval", return_id=None, approval_id="APR-REF-1")

    r_ref_stale1 = client.post(
        f"/v1/proposals/{card_ref['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c001_tok}", "Idempotency-Key": "k-stale-ref-1"},
        json={"proposal_hash": card_ref["proposal_hash"]},
    )
    assert r_ref_stale1.status_code == 409, r_ref_stale1.text
    ref_err = r_ref_stale1.json()["error"]
    assert ref_err["code"] == "invalid_state"
    assert ref_err["details"]["reason"] == "stale_state"
    fresh_ref = ref_err["details"]["fresh_preview"]
    assert fresh_ref["type"] == "confirmation"
    assert fresh_ref["proposal_type"] == "refund"
    assert fresh_ref["decision"] == "needs_approval"
    assert fresh_ref["amounts"]["total_paise"] == 49800
    assert fresh_ref["capped"] is True
    assert fresh_ref["proposal_id"] != card_ref["proposal_id"]
    _assert_no_forbidden_customer_words(r_ref_stale1.json())
    assert len(store.write_calls) == 0
    assert len(executor.calls) == 0

    # Re-confirming the stale old proposal deduplicates onto the existing fresh_ref
    r_ref_stale2 = client.post(
        f"/v1/proposals/{card_ref['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c001_tok}", "Idempotency-Key": "k-stale-ref-2"},
        json={"proposal_hash": card_ref["proposal_hash"]},
    )
    assert r_ref_stale2.status_code == 409
    assert (
        r_ref_stale2.json()["error"]["details"]["fresh_preview"]["proposal_id"]
        == fresh_ref["proposal_id"]
    )

    r_ref_ok = client.post(
        f"/v1/proposals/{fresh_ref['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c001_tok}", "Idempotency-Key": "k-confirm-ref-fresh"},
        json={"proposal_hash": fresh_ref["proposal_hash"]},
    )
    assert r_ref_ok.status_code == 200, r_ref_ok.text
    assert r_ref_ok.json()["status"] == "pending_approval"
    assert r_ref_ok.json()["approval_id"] == "APR-REF-1"
    assert len(executor.calls) == 1

    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True


# ── 8. Policy Flip v1 -> v3 Turns ALLOW Into stale_state With Fresh Preview ───


@pytest.mark.asyncio
@respx.mock
async def test_policy_switch_v1_to_v3_turns_allow_into_stale_state_with_fresh_preview(
    confirm_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sellershield_agent.api import proposals as proposals_api

    assert hasattr(proposals_api, "get_proposal_executor")

    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    executor: _FakeExecutor = confirm_env["executor"]
    store: _SpyStoreClient = confirm_env["store"]
    c002_tok = confirm_env["tokens"]["c002"]

    # ORD-2031 ITEM-2031-1 qty=1 changed_mind = 59899 paise: ALLOW under v1 (limit 100000)
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v1")
    card_v1 = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-1", "qty": 1}],
        reason_code="changed_mind",
        policy_version="v1",
    )
    assert card_v1["decision"] == "allow"
    assert card_v1["policy_version"] == "v1"

    # Flip policy mid-flight to v3 (auto_limit_paise = 50000) -> same item becomes NEEDS_APPROVAL
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v3")
    store.reset_counts()
    executor.reset(status="pending_approval", return_id=None, approval_id="APR-99887766")

    r_flip = client.post(
        f"/v1/proposals/{card_v1['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-v1-to-v3"},
        json={"proposal_hash": card_v1["proposal_hash"]},
    )
    assert r_flip.status_code == 409, r_flip.text
    err = r_flip.json()["error"]
    assert err["code"] == "invalid_state"
    assert err["details"]["reason"] == "stale_state"

    fresh = err["details"]["fresh_preview"]
    assert fresh["type"] == "confirmation"
    assert fresh["proposal_id"] != card_v1["proposal_id"]
    assert fresh["proposal_hash"] != card_v1["proposal_hash"]
    assert fresh["decision"] == "needs_approval"
    assert fresh["policy_version"] == "v3"
    assert fresh["amounts"]["total_paise"] == card_v1["amounts"]["total_paise"]
    _assert_no_forbidden_customer_words(r_flip.json())
    assert len(store.write_calls) == 0
    assert len(executor.calls) == 0

    stale_audits = _get_proposal_confirm_audits(engine, card_v1["proposal_id"])
    assert any(
        json.loads(a.details_json).get("reason") == "stale_state" for a in stale_audits
    )

    # Confirming the new v3 preview succeeds cleanly with pending_approval
    r_fresh = client.post(
        f"/v1/proposals/{fresh['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-v3-confirm"},
        json={"proposal_hash": fresh["proposal_hash"]},
    )
    assert r_fresh.status_code == 200, r_fresh.text
    assert r_fresh.json()["status"] == "pending_approval"
    assert r_fresh.json()["approval_id"] == "APR-99887766"
    assert len(executor.calls) == 1


# ── 9. Cancel Lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_cancel_works_once_and_cancel_after_confirm_returns_409(
    confirm_env: dict[str, Any],
) -> None:
    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    executor: _FakeExecutor = confirm_env["executor"]
    c002_tok = confirm_env["tokens"]["c002"]

    card_cancel = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    r_c1 = client.post(
        f"/v1/proposals/{card_cancel['proposal_id']}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-cxl-1"},
        json={},
    )
    assert r_c1.status_code == 200
    assert r_c1.json() == {"proposal_id": card_cancel["proposal_id"], "status": "cancelled"}
    row_cxl = _get_proposal_row(engine, card_cancel["proposal_id"])
    assert row_cxl is not None
    assert row_cxl.status == ProposalStatus.cancelled.value
    assert row_cxl.confirmed_at is None

    # Second cancel with different key -> 409 invalid_state
    r_c2 = client.post(
        f"/v1/proposals/{card_cancel['proposal_id']}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-cxl-2"},
        json={},
    )
    assert r_c2.status_code == 409
    assert r_c2.json()["error"]["code"] == "invalid_state"

    # Confirm then cancel -> 409 invalid_state (already_confirmed)
    card_conf = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-1", "qty": 1}],
    )
    executor.reset(status="succeeded", return_id="RET-CXL-CHK")
    r_ok = client.post(
        f"/v1/proposals/{card_conf['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-conf-first"},
        json={"proposal_hash": card_conf["proposal_hash"]},
    )
    assert r_ok.status_code == 200

    r_cxl_after = client.post(
        f"/v1/proposals/{card_conf['proposal_id']}/cancel",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-cxl-after"},
        json={},
    )
    assert r_cxl_after.status_code == 409
    assert r_cxl_after.json()["error"]["code"] == "invalid_state"
    assert r_cxl_after.json()["error"]["details"]["reason"] == "already_confirmed"
    assert len(executor.calls) == 1
    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True


# ── 10. Chat Text Never Confirms Any Proposal ─────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_chat_text_yes_confirm_i_agree_ok_never_confirms(
    confirm_env: dict[str, Any],
) -> None:
    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    executor: _FakeExecutor = confirm_env["executor"]
    c002_tok = confirm_env["tokens"]["c002"]

    card = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    pid = card["proposal_id"]

    sess_resp = client.post("/v1/chat/sessions", headers={"Authorization": f"Bearer {c002_tok}"})
    assert sess_resp.status_code == 200
    session_id = sess_resp.json()["session_id"]

    for idx, phrase in enumerate(["yes", "confirm", "I agree", "ok"]):
        r_msg = client.post(
            f"/v1/chat/sessions/{session_id}/messages",
            headers={"Authorization": f"Bearer {c002_tok}"},
            json={"text": phrase, "client_msg_id": f"chat_conf_{idx}"},
        )
        assert r_msg.status_code == 200

    row = _get_proposal_row(engine, pid)
    assert row is not None
    assert row.status == ProposalStatus.proposed.value
    assert row.confirmed_at is None
    assert row.version == 1
    assert len(executor.calls) == 0
    assert len(_get_proposal_confirm_audits(engine, pid)) == 0


# ── 11. Complete Audit Coverage & Integrity Across Every Outcome ──────────────


@pytest.mark.asyncio
@respx.mock
async def test_proposal_confirmed_audit_exists_for_all_outcomes(
    confirm_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client: TestClient = confirm_env["client"]
    engine = confirm_env["engine"]
    clock: FixedClock = confirm_env["clock"]
    c002_tok = confirm_env["tokens"]["c002"]

    # 1. Denied (wrong hash) + Success
    card_s = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
    )
    wrong_h = ("f" if card_s["proposal_hash"][0] != "f" else "0") + card_s["proposal_hash"][1:]
    r_d = client.post(
        f"/v1/proposals/{card_s['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-aud-deny"},
        json={"proposal_hash": wrong_h},
    )
    assert r_d.status_code == 409
    r_ok = client.post(
        f"/v1/proposals/{card_s['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-aud-ok"},
        json={"proposal_hash": card_s["proposal_hash"]},
    )
    assert r_ok.status_code == 200

    # 2. Stale (v1 -> v3)
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v1")
    card_st = await _propose_return(
        engine=engine,
        store=confirm_env["raw_store"],
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2031",
        items=[{"item_id": "ITEM-2031-1", "qty": 1}],
        policy_version="v1",
    )
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v3")
    r_st = client.post(
        f"/v1/proposals/{card_st['proposal_id']}/confirm",
        headers={"Authorization": f"Bearer {c002_tok}", "Idempotency-Key": "k-aud-stale"},
        json={"proposal_hash": card_st["proposal_hash"]},
    )
    assert r_st.status_code == 409

    audits_s = _get_proposal_confirm_audits(engine, card_s["proposal_id"])
    assert [a.outcome for a in audits_s] == ["denied", "success"]

    audits_st = _get_proposal_confirm_audits(engine, card_st["proposal_id"])
    assert len(audits_st) == 1
    assert audits_st[0].outcome == "failed"
    assert json.loads(audits_st[0].details_json).get("reason") == "stale_state"

    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True
