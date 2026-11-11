"""Step A tests for T2.5 Executor saga and reconciler.

Covers TASK T2.5 and Spec docs/spec/02-agent.md sections 2, 6.3, 9.1, 21 (amendments 1 & 2):
- ALLOW happy path: create_return (customer token), transition_return to approved (service token),
  reserve_refund hold (service token) -> 1 approved return, 1 hold, 3 execution rows + audit events.
- NEEDS_APPROVAL path (return & refund without return): create_return (customer token),
  reserve_refund hold (service token), approval row with due_at & expires_at,
  client_event, seller notification stub, carrying chosen resolution & refund method.
- Terminal failure compensation in reverse order (cancel_reservation -> cancel_return) with
  service tokens, plus compensation failure recording for reconciler.
- StoreTimeout / StoreUnavailable -> status 'unknown' (never 'failed'), customer sees verifying.
- timeout_after_commit on create_return -> unknown -> reconcile_once matches client_ref via
  get_order_admin with service token and resumes to success without creating a second return.
- unavailable on step -> unknown -> reconcile_once retries with same idempotency_key (at 5s, 30s,
  2m, then stuck) using ServiceContext only.
- Crash simulated after store call and before result write -> startup lifespan reconcile_once
  repairs before serving; background loop reconciles every 5s cleanly.
- Double confirmation of same proposal never creates a second return or second hold.
- Audit failure on any step aborts that step's DB write and leaves executing for reconciler.
- Duplicate active return error -> failed with existing return_id and compensation of any hold.
- Daily-cap error -> failed with neutral customer message (zero forbidden words).
- Connection/transaction spy proves zero store calls happen inside an open transaction.
- Customer token isolation proves reconciler & compensator never use customer tokens and zero
  tokens are persisted in DB rows, audit details, client_events, notifications, or logs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, event, select
from starlette.testclient import TestClient

from sellershield_agent.audit.models import EventType, audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import (
    _db_write_lock,
    create_engine_from_url,
    get_engine,
    init_db,
    read_tx,
    reset_engine,
    write_tx,
)
from sellershield_agent.domain.models import (
    RefundMethod,
    RefundStatus,
    ReturnItem,
    ReturnReasonCode,
    ReturnResolution,
    ReturnStatus,
)
from sellershield_agent.domain.state import (
    ApprovalStatus,
    ProposalStatus,
    approvals,
    client_events,
    executions,
    proposals,
)
from sellershield_agent.logging import configure_logging
from sellershield_agent.main import create_app
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.proposals.confirm import confirm_proposal
from sellershield_agent.proposals.integrity import (
    PreparedProposal,
    build_prepared_proposal,
    validate_stored_proposal,
)
from sellershield_agent.store.client import (
    RequestContext,
    StoreLimitExceeded,
)
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway
from tests.fault_plan import FaultPlan

FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scenarios" / "fixtures" / "store.json"
)
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
FORBIDDEN_CUSTOMER_RE = re.compile(
    r"\b(?:limits?|thresholds?|risks?|scores?|staff|owner|auto(?:matic|nomy)?|caps?)\b",
    re.IGNORECASE,
)


class _SimulatedCrash(BaseException):
    """Uncatchable error simulating sudden process death after store commit."""


class _TransactionSpyStoreClient:
    """StoreClient wrapper recording every call's principal/token and asserting zero open tx."""

    def __init__(self, inner: FixtureStoreClient, engine: Engine) -> None:
        self._inner = inner
        self._engine = engine
        self._lock = threading.Lock()
        self._active_tx_depth = 0
        self.calls: list[dict[str, Any]] = []
        self.tx_violations: list[str] = []
        self.on_after_store_write: Any = None
        self.raise_reserve_error: Exception | None = None

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

    def _record(self, op: str, ctx: RequestContext, *, idempotency_key: str | None = None) -> None:
        with self._lock:
            tx_open = self._active_tx_depth > 0 or _db_write_lock.locked()
            if tx_open:
                self.tx_violations.append(
                    f"{op} called with open transaction (depth={self._active_tx_depth}, "
                    f"write_lock={_db_write_lock.locked()})"
                )
            token_val = ctx.token.get_secret_value() if ctx.token is not None else None
            self.calls.append(
                {
                    "op": op,
                    "role": ctx.principal.role,
                    "customer_id": ctx.principal.customer_id,
                    "user_id": ctx.principal.user_id,
                    "has_token": token_val is not None and len(token_val) > 0,
                    "token_value": token_val,
                    "idempotency_key": idempotency_key,
                    "scopes": list(getattr(ctx, "scopes", [])),
                }
            )

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
        self._record("get_order", ctx)
        return await self._inner.get_order(ctx, order_id)

    async def get_shipment(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("get_shipment", ctx)
        return await self._inner.get_shipment(ctx, order_id)

    async def get_refundable(
        self,
        ctx: RequestContext,
        order_id: str,
        items: list[tuple[str, int]] | None = None,
    ) -> Any:
        self._record("get_refundable", ctx)
        return await self._inner.get_refundable(ctx, order_id, items=items)

    async def list_returns(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("list_returns", ctx)
        return await self._inner.list_returns(ctx, order_id)

    async def list_refunds(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("list_refunds", ctx)
        return await self._inner.list_refunds(ctx, order_id)

    async def get_customer_risk_facts(self, service_ctx: RequestContext, customer_id: str) -> Any:
        self._record("get_customer_risk_facts", service_ctx)
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
        self._record("create_return", ctx, idempotency_key=idempotency_key)
        res = await self._inner.create_return(
            ctx, order_id, items, reason_code, reason_text, resolution, idempotency_key
        )
        if callable(self.on_after_store_write):
            self.on_after_store_write("create_return", res)
        return res

    async def get_return(self, ctx: RequestContext, return_id: str) -> Any:
        self._record("get_return", ctx)
        return await self._inner.get_return(ctx, return_id)

    async def transition_return(
        self,
        ctx: RequestContext,
        return_id: str,
        to: str,
        reason: str | None,
        idempotency_key: str,
    ) -> Any:
        self._record("transition_return", ctx, idempotency_key=idempotency_key)
        res = await self._inner.transition_return(ctx, return_id, to, reason, idempotency_key)
        if callable(self.on_after_store_write):
            self.on_after_store_write("transition_return", res)
        return res

    async def reserve_refund(
        self,
        service_ctx: RequestContext,
        order_id: str,
        amount_paise: int,
        return_id: str | None,
        method: str,
        idempotency_key: str,
    ) -> Any:
        self._record("reserve_refund", service_ctx, idempotency_key=idempotency_key)
        if self.raise_reserve_error is not None:
            raise self.raise_reserve_error
        res = await self._inner.reserve_refund(
            service_ctx, order_id, amount_paise, return_id, method, idempotency_key
        )
        if callable(self.on_after_store_write):
            self.on_after_store_write("reserve_refund", res)
        return res

    async def authorize_refund(
        self,
        seller_ctx: RequestContext,
        refund_id: str,
        amount_paise: int,
        note: str | None,
        idempotency_key: str,
    ) -> Any:
        self._record("authorize_refund", seller_ctx, idempotency_key=idempotency_key)
        return await self._inner.authorize_refund(
            seller_ctx, refund_id, amount_paise, note, idempotency_key
        )

    async def release_refund(
        self, ctx: RequestContext, refund_id: str, idempotency_key: str
    ) -> Any:
        self._record("release_refund", ctx, idempotency_key=idempotency_key)
        return await self._inner.release_refund(ctx, refund_id, idempotency_key)

    async def cancel_reservation(
        self, ctx: RequestContext, refund_id: str, idempotency_key: str
    ) -> Any:
        self._record("cancel_reservation", ctx, idempotency_key=idempotency_key)
        return await self._inner.cancel_reservation(ctx, refund_id, idempotency_key)

    async def get_refund(self, ctx: RequestContext, refund_id: str) -> Any:
        self._record("get_refund", ctx)
        return await self._inner.get_refund(ctx, refund_id)

    async def get_order_admin(self, service_ctx: RequestContext, order_id: str) -> Any:
        self._record("get_order_admin", service_ctx)
        return await self._inner.get_order_admin(service_ctx, order_id)

    async def get_settings(self, service_ctx: RequestContext) -> Any:
        self._record("get_settings", service_ctx)
        return await self._inner.get_settings(service_ctx)

    async def get_customer_refund_summary(
        self, service_ctx: RequestContext, customer_id: str, days: int = 30
    ) -> Any:
        self._record("get_customer_refund_summary", service_ctx)
        return await self._inner.get_customer_refund_summary(service_ctx, customer_id, days=days)


def _make_customer_ctx(
    *,
    customer_id: str,
    user_id: str,
    clock: FixedClock,
    token_value: str = "cust-jwt-canary-token",
    expired: bool = False,
    exp_seconds: int = 3600,
) -> RequestContext:
    exp_delta = timedelta(minutes=-5) if expired else timedelta(seconds=exp_seconds)
    return RequestContext(
        principal=Principal(
            user_id=user_id,
            role=Role.customer,
            customer_id=customer_id,
            auth_time=int(clock.now().timestamp()),
            jti=f"jti-{uuid.uuid4().hex[:8]}",
            token_exp=int((clock.now() + exp_delta).timestamp()),
        ),
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        token=SecretStr(token_value),
    )


def _make_service_ctx(clock: FixedClock) -> RequestContext:
    return RequestContext(
        principal=Principal(
            user_id="svc_test",
            role=Role.service,
            customer_id=None,
            auth_time=int(clock.now().timestamp()),
            jti=f"jti-svc-{uuid.uuid4().hex[:8]}",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id=f"req-svc-{uuid.uuid4().hex[:8]}",
        token=None,
    )


async def _propose_return(
    *,
    engine: Engine,
    store: FixtureStoreClient,
    clock: FixedClock,
    customer_id: str,
    user_id: str,
    order_id: str,
    items: list[dict[str, Any]],
    reason_code: str = "changed_mind",
    resolution: str = "refund",
    refund_method: str | None = None,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    policy = load_policy("v3")
    gateway = ToolGateway(store_client=store, engine=engine, clock=clock, policy=policy)
    ctx = _make_customer_ctx(customer_id=customer_id, user_id=user_id, clock=clock)
    args: dict[str, Any] = {
        "order_id": order_id,
        "items": items,
        "reason_code": reason_code,
        "reason_text": "Test return request",
        "resolution": resolution,
    }
    if refund_method is not None:
        args["refund_method"] = refund_method
    if evidence_ids is not None:
        args["evidence_ids"] = evidence_ids
    res = await gateway.call(
        ctx=ctx,
        name="propose_return",
        raw_args=args,
        allowed={"propose_return", "propose_refund"},
        session_id=f"ses_{customer_id}_{order_id}",
    )
    assert res.ok is True and isinstance(res.data, dict)
    assert res.data.get("type") == "confirmation"
    return res.data


async def _propose_refund(
    *,
    engine: Engine,
    store: FixtureStoreClient,
    clock: FixedClock,
    customer_id: str,
    user_id: str,
    order_id: str,
    cause: str = "lost",
    refund_method: str | None = None,
) -> dict[str, Any]:
    policy = load_policy("v3")
    gateway = ToolGateway(store_client=store, engine=engine, clock=clock, policy=policy)
    ctx = _make_customer_ctx(customer_id=customer_id, user_id=user_id, clock=clock)
    args: dict[str, Any] = {"order_id": order_id, "cause": cause}
    if refund_method is not None:
        args["refund_method"] = refund_method
    res = await gateway.call(
        ctx=ctx,
        name="propose_refund",
        raw_args=args,
        allowed={"propose_return", "propose_refund"},
        session_id=f"ses_{customer_id}_{order_id}",
    )
    assert res.ok is True and isinstance(res.data, dict)
    assert res.data.get("type") == "confirmation"
    return res.data


def _load_prepared(engine: Engine, proposal_id: str, clock: FixedClock) -> PreparedProposal:
    with read_tx(engine) as conn:
        row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == proposal_id)
        ).fetchone()
        assert row is not None
        payload, order_id, norm_items, canonical_amounts, method = validate_stored_proposal(
            row, clock=clock, expected_hash=str(row.proposal_hash)
        )
        return build_prepared_proposal(
            conn,
            row,
            customer_id=str(row.customer_id),
            order_id=order_id,
            norm_items=norm_items,
            canonical_amounts=canonical_amounts,
            method=method,
            payload=payload,
        )


def _assert_customer_safe(text: str) -> None:
    match = FORBIDDEN_CUSTOMER_RE.search(text)
    assert match is None, f"Forbidden internal term '{match.group(0)}' leaked in: {text!r}"


@pytest.fixture
def saga_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "saga_test.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    get_settings.cache_clear()
    reset_engine()
    reset_default_jwks_client()

    clock = FixedClock(T0_BASE)
    fault_plan = FaultPlan()
    raw_store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=clock, fault_plan=fault_plan)
    engine = create_engine_from_url(f"sqlite:///{db_file.as_posix()}")
    init_db(engine)
    spy_store = _TransactionSpyStoreClient(raw_store, engine)

    yield {
        "db_file": db_file,
        "engine": engine,
        "clock": clock,
        "fault_plan": fault_plan,
        "raw_store": raw_store,
        "store": spy_store,
    }

    engine.dispose()
    reset_engine()
    reset_default_jwks_client()
    get_settings.cache_clear()


# ── 1. ALLOW Happy Path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allow_happy_path_creates_approved_return_hold_3_executions_and_audits(
    saga_env: dict[str, Any],
) -> None:
    from sellershield_agent.execution.saga import Executor, format_execution_status_message

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]

    # Chosen non-default resolution='store_credit' and method='store_credit'
    card = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
        resolution="store_credit",
    )
    pid = card["proposal_id"]
    ctx = _make_customer_ctx(customer_id="C002", user_id="usr_c002", clock=clock)
    spy_store.reset_spy()

    executor = Executor(store_client=spy_store, engine=engine, clock=clock)
    res = await confirm_proposal(
        ctx,
        pid,
        card["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-allow-1",
    )

    assert res.status == "succeeded"
    assert res.return_id is not None and res.return_id.startswith("RET-")
    assert res.approval_id is None
    assert res.message_key == "proposal.succeeded"
    msg = format_execution_status_message(
        res.status, message_key=res.message_key, return_id=res.return_id
    )
    _assert_customer_safe(msg)
    assert res.return_id in msg

    # Verify Store state via admin view
    svc_ctx = _make_service_ctx(clock)
    admin_order = await raw_store.get_order_admin(svc_ctx, "ORD-2032")
    assert len(admin_order.returns) == 1
    ret = admin_order.returns[0]
    assert ret.return_id == res.return_id
    assert ret.status == ReturnStatus.approved
    assert ret.resolution == ReturnResolution.store_credit
    assert ret.client_ref == f"{pid}:create_return"

    assert len(admin_order.refunds) == 1
    rfd = admin_order.refunds[0]
    assert rfd.status == RefundStatus.reserved
    assert rfd.return_id == ret.return_id
    assert rfd.method == RefundMethod.store_credit
    assert rfd.amount_paise == card["amounts"]["total_paise"]
    assert rfd.client_ref == f"{pid}:reserve_refund"

    # Verify exact token boundaries on write calls
    write_calls = [
        c
        for c in spy_store.calls
        if c["op"] in ("create_return", "transition_return", "reserve_refund")
    ]
    assert [c["op"] for c in write_calls] == [
        "create_return",
        "transition_return",
        "reserve_refund",
    ]
    assert write_calls[0]["role"] == Role.customer and write_calls[0]["has_token"] is True
    assert write_calls[0]["idempotency_key"] == f"{pid}:create_return"
    assert write_calls[1]["role"] == Role.service and write_calls[1]["has_token"] is False
    assert write_calls[1]["idempotency_key"] == f"{pid}:transition_return"
    assert write_calls[2]["role"] == Role.service and write_calls[2]["has_token"] is False
    assert write_calls[2]["idempotency_key"] == f"{pid}:reserve_refund"
    assert spy_store.tx_violations == []

    # Verify DB tables
    with read_tx(engine) as conn:
        prop_row = conn.execute(select(proposals).where(proposals.c.proposal_id == pid)).fetchone()
        assert prop_row is not None and prop_row.status == ProposalStatus.succeeded.value

        exec_rows = conn.execute(
            select(executions).where(executions.c.proposal_id == pid)
        ).fetchall()
        assert len(exec_rows) == 3
        assert {r.step for r in exec_rows} == {
            "create_return",
            "transition_return",
            "reserve_refund",
        }
        for r in exec_rows:
            assert r.status == "succeeded"
            assert r.idempotency_key == f"{pid}:{r.step}"
            assert r.store_ref is not None
            assert r.finished_at is not None

        apr_rows = conn.execute(select(approvals).where(approvals.c.proposal_id == pid)).fetchall()
        assert len(apr_rows) == 0

        cev_rows = conn.execute(
            select(client_events).where(client_events.c.customer_id == "C002")
        ).fetchall()
        assert len(cev_rows) >= 1

        step_audits = conn.execute(
            select(audit_events).where(
                audit_events.c.resource_id == pid,
                audit_events.c.event_type == EventType.EXECUTION_STEP.value,
            )
        ).fetchall()
        started_steps = {
            r.action for r in step_audits if str(r.action).startswith("execution_step_started")
        }
        assert len(step_audits) >= 6  # 2 events (started + succeeded) x 3 steps
        assert len(started_steps) >= 1
        assert AuditService.verify_chain(conn).ok is True

    # Also verify direct 2-argument Executor.run(ctx, proposal) on another ALLOW proposal
    raw_store.reset()
    card_direct = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C005",
        user_id="usr_c005",
        order_id="ORD-5001",
        items=[{"item_id": "ITEM-5001-1", "qty": 1}],
        reason_code="changed_mind",
        resolution="refund",
    )
    pid_direct = card_direct["proposal_id"]
    prepared_direct = _load_prepared(engine, pid_direct, clock)
    ctx_c005 = _make_customer_ctx(customer_id="C005", user_id="usr_c005", clock=clock)
    direct_res = await executor.run(ctx_c005, prepared_direct)
    assert direct_res.status == "succeeded"
    assert direct_res.return_id is not None and direct_res.return_id.startswith("RET-")
    with read_tx(engine) as conn:
        prop_direct = conn.execute(
            select(proposals).where(proposals.c.proposal_id == pid_direct)
        ).fetchone()
        assert prop_direct is not None and prop_direct.status == ProposalStatus.succeeded.value
        assert AuditService.verify_chain(conn).ok is True


# ── 2. NEEDS_APPROVAL Path (Return & Refund Without Return) ──────────────────


@pytest.mark.asyncio
async def test_needs_approval_return_and_refund_create_approval_client_event_and_seller_stub(
    saga_env: dict[str, Any],
) -> None:
    from sellershield_agent.execution.saga import Executor, format_execution_status_message

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]

    from sellershield_agent.domain.state import notifications_outbox

    executor = Executor(store_client=spy_store, engine=engine, clock=clock)

    # A: Return proposal above auto limit (ORD-1042 = 120,000 <= 200,000 staff limit)
    card_ret = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
        resolution="refund",
        evidence_ids=["EV-1042"],
    )
    pid_ret = card_ret["proposal_id"]
    ctx_c001 = _make_customer_ctx(customer_id="C001", user_id="usr_c001", clock=clock)
    spy_store.reset_spy()

    res_ret = await confirm_proposal(
        ctx_c001,
        pid_ret,
        card_ret["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-appr-ret",
    )
    assert res_ret.status == "pending_approval"
    assert res_ret.return_id is not None and res_ret.return_id.startswith("RET-")
    assert res_ret.approval_id is not None and res_ret.approval_id.startswith("APR-")
    assert res_ret.message_key == "proposal.pending_approval"
    msg = format_execution_status_message(
        res_ret.status,
        message_key=res_ret.message_key,
        return_id=res_ret.return_id,
        approval_id=res_ret.approval_id,
    )
    _assert_customer_safe(msg)

    svc_ctx = _make_service_ctx(clock)
    admin_ord = await raw_store.get_order_admin(svc_ctx, "ORD-1042")
    assert len(admin_ord.returns) == 1
    assert admin_ord.returns[0].status == ReturnStatus.requested
    assert len(admin_ord.refunds) == 1
    assert admin_ord.refunds[0].status == RefundStatus.reserved
    assert admin_ord.refunds[0].return_id == admin_ord.returns[0].return_id
    assert spy_store.tx_violations == []

    with read_tx(engine) as conn:
        prop = conn.execute(select(proposals).where(proposals.c.proposal_id == pid_ret)).fetchone()
        assert prop is not None and prop.status == ProposalStatus.executing.value

        exec_rows = conn.execute(
            select(executions).where(executions.c.proposal_id == pid_ret)
        ).fetchall()
        assert {r.step for r in exec_rows} == {"create_return", "reserve_refund"}
        assert all(r.status == "succeeded" for r in exec_rows)

        apr = conn.execute(select(approvals).where(approvals.c.proposal_id == pid_ret)).fetchone()
        assert apr is not None
        assert apr.approval_id == res_ret.approval_id
        assert apr.status == ApprovalStatus.pending.value
        assert apr.return_id == res_ret.return_id
        assert apr.refund_id == admin_ord.refunds[0].refund_id
        assert apr.amount_paise == 120000
        assert apr.required_role == "seller_staff"
        expected_due = (
            (T0_BASE + timedelta(hours=24)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        expected_exp = (T0_BASE + timedelta(days=7)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert apr.due_at == expected_due
        assert apr.expires_at == expected_exp

        cevs = conn.execute(
            select(client_events).where(client_events.c.customer_id == "C001")
        ).fetchall()
        assert len(cevs) == 1
        assert cevs[0].type == "notification.new"
        _assert_customer_safe(cevs[0].payload_json)
        assert pid_ret in cevs[0].payload_json

        stubs = conn.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.dedupe_key == f"seller_approval_needed:{pid_ret}"
            )
        ).fetchall()
        assert len(stubs) == 1
        assert res_ret.approval_id in stubs[0].payload_json
        assert "refund" in stubs[0].payload_json
        assert "original" in stubs[0].payload_json

        apr_audits = conn.execute(
            select(audit_events).where(
                audit_events.c.event_type == EventType.APPROVAL_CREATED.value,
                audit_events.c.resource_id == res_ret.approval_id,
            )
        ).fetchall()
        assert len(apr_audits) == 1

    # B: propose_refund without return (ORD-1070 lost shipment -> 1 store step + approval)
    card_rfd = await _propose_refund(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1070",
        cause="lost",
    )
    res_rfd = await confirm_proposal(
        ctx_c001,
        card_rfd["proposal_id"],
        card_rfd["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-appr-rfd",
    )
    assert res_rfd.status == "pending_approval"
    assert res_rfd.return_id is None
    assert res_rfd.approval_id is not None

    with read_tx(engine) as conn:
        exec_rfd = conn.execute(
            select(executions).where(executions.c.proposal_id == card_rfd["proposal_id"])
        ).fetchall()
        assert [r.step for r in exec_rfd] == ["reserve_refund"]
        assert exec_rfd[0].status == "succeeded"
        apr_rfd = conn.execute(
            select(approvals).where(approvals.c.proposal_id == card_rfd["proposal_id"])
        ).fetchone()
        assert apr_rfd is not None and apr_rfd.return_id is None and apr_rfd.refund_id is not None
        cev_rfd = conn.execute(
            select(client_events).where(client_events.c.customer_id == "C001")
        ).fetchall()
        assert any(card_rfd["proposal_id"] in c.payload_json for c in cev_rfd)
        stub_rfd = conn.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.dedupe_key
                == f"seller_approval_needed:{card_rfd['proposal_id']}"
            )
        ).fetchone()
        assert stub_rfd is not None and res_rfd.approval_id in stub_rfd.payload_json
        assert AuditService.verify_chain(conn).ok is True

    # C: Above seller_staff limit (ORD-3011 = 245,000 > 200,000) with non-default store_credit
    raw_store.reset()
    card_owner = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[
            {"item_id": "ITEM-3011-1", "qty": 1},
            {"item_id": "ITEM-3011-2", "qty": 1},
        ],
        reason_code="defective",
        resolution="store_credit",
        refund_method="store_credit",
    )
    ctx_c003 = _make_customer_ctx(customer_id="C003", user_id="usr_c003", clock=clock)
    res_owner = await confirm_proposal(
        ctx_c003,
        card_owner["proposal_id"],
        card_owner["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-appr-owner",
    )
    assert res_owner.status == "pending_approval"
    admin_owner = await raw_store.get_order_admin(svc_ctx, "ORD-3011")
    assert len(admin_owner.returns) == 1
    assert admin_owner.returns[0].resolution == ReturnResolution.store_credit
    assert len(admin_owner.refunds) == 1
    assert admin_owner.refunds[0].method == RefundMethod.store_credit
    with read_tx(engine) as conn:
        apr_owner = conn.execute(
            select(approvals).where(approvals.c.proposal_id == card_owner["proposal_id"])
        ).fetchone()
        assert apr_owner is not None
        assert apr_owner.amount_paise == 245000
        assert apr_owner.required_role == "seller_owner"
        stub_owner = conn.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.dedupe_key
                == f"seller_approval_needed:{card_owner['proposal_id']}"
            )
        ).fetchone()
        assert stub_owner is not None and "store_credit" in stub_owner.payload_json
        assert AuditService.verify_chain(conn).ok is True


# ── 3. Reverse Compensation & Compensation Failures ─────────────────────────


@pytest.mark.asyncio
async def test_reserve_failure_compensates_return_and_handles_compensation_errors(
    saga_env: dict[str, Any],
) -> None:
    from sellershield_agent.execution.reconcile import reconcile_once
    from sellershield_agent.execution.saga import Executor, format_execution_status_message

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    fault_plan: FaultPlan = saga_env["fault_plan"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]

    from sellershield_agent.domain.state import notifications_outbox

    executor = Executor(store_client=spy_store, engine=engine, clock=clock)
    svc_ctx = _make_service_ctx(clock)

    # Case A: terminal conflict on reserve_refund for NEEDS_APPROVAL -> cancel_return succeeds
    card_a = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    fault_plan.conflict("reserve_refund")
    ctx_a = _make_customer_ctx(customer_id="C001", user_id="usr_c001", clock=clock)
    spy_store.reset_spy()

    res_a = await confirm_proposal(
        ctx_a,
        card_a["proposal_id"],
        card_a["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-comp-a",
    )
    assert res_a.status == "failed"
    assert res_a.return_id is None
    assert res_a.approval_id is None
    msg_a = format_execution_status_message(res_a.status, message_key=res_a.message_key)
    _assert_customer_safe(msg_a)
    assert "approved" not in msg_a.lower()

    admin_a = await raw_store.get_order_admin(svc_ctx, "ORD-1042")
    assert len(admin_a.returns) == 1
    assert admin_a.returns[0].status == ReturnStatus.cancelled
    assert len(admin_a.refunds) == 0

    cancel_calls = [c for c in spy_store.calls if c["op"] == "transition_return"]
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["role"] == Role.service and cancel_calls[0]["has_token"] is False
    assert cancel_calls[0]["idempotency_key"] == f"{card_a['proposal_id']}:cancel_return"
    assert spy_store.tx_violations == []

    with read_tx(engine) as conn:
        prop_a = conn.execute(
            select(proposals).where(proposals.c.proposal_id == card_a["proposal_id"])
        ).fetchone()
        assert prop_a is not None and prop_a.status == ProposalStatus.failed.value

    # Case B: ALLOW path conflict on reserve_refund after transition_return approved ->
    # cancel_return fails StoreInvalidState and records failure + seller alert stub cleanly.
    raw_store.reset()
    fault_plan._conflict_steps.clear()
    card_b = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
    )
    fault_plan.conflict("reserve_refund")
    ctx_b = _make_customer_ctx(customer_id="C002", user_id="usr_c002", clock=clock)
    res_b = await confirm_proposal(
        ctx_b,
        card_b["proposal_id"],
        card_b["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-comp-b",
    )
    assert res_b.status == "failed"
    with read_tx(engine) as conn:
        comp_rows = conn.execute(
            select(executions).where(
                executions.c.proposal_id == card_b["proposal_id"],
                executions.c.step == "cancel_return",
            )
        ).fetchall()
        assert len(comp_rows) == 1
        assert comp_rows[0].status == "failed"
        stubs = conn.execute(select(notifications_outbox)).fetchall()
        assert any("compensation" in s.template for s in stubs)

    # Case C: transient timeout on cancel_return leaves unknown -> reconciler retries
    raw_store.reset()
    fault_plan._conflict_steps.clear()
    card_c = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    fault_plan.conflict("reserve_refund")
    fault_plan.unavailable("transition_return")
    await confirm_proposal(
        ctx_a,
        card_c["proposal_id"],
        card_c["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-comp-c",
    )
    with read_tx(engine) as conn:
        c_row = conn.execute(
            select(executions).where(
                executions.c.proposal_id == card_c["proposal_id"],
                executions.c.step == "cancel_return",
            )
        ).fetchone()
        assert c_row is not None and c_row.status == "unknown"

    fault_plan._unavailable_steps.clear()
    fault_plan._conflict_steps.clear()
    clock.set(T0_BASE + timedelta(seconds=6))
    await reconcile_once(clock, store_client=spy_store, engine=engine)
    admin_c = await raw_store.get_order_admin(svc_ctx, "ORD-1042")
    assert admin_c.returns[0].status == ReturnStatus.cancelled

    # Case D: reverse-order compensation (cancel_reservation FIRST, then cancel_return)
    # when both create_return (requested) and reserve_refund (reserved) succeeded prior to failure.
    raw_store.reset()
    clock.set(T0_BASE + timedelta(minutes=10))
    card_d = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    pid_d = card_d["proposal_id"]
    ctx_d = _make_customer_ctx(customer_id="C001", user_id="usr_c001", clock=clock)
    ret_d = await raw_store.create_return(
        ctx_d,
        order_id="ORD-1042",
        items=[ReturnItem(item_id="ITEM-1042-1", qty=1)],
        reason_code="defective",
        reason_text="Broken",
        resolution="refund",
        idempotency_key=f"{pid_d}:create_return",
    )
    rfd_d = await raw_store.reserve_refund(
        svc_ctx,
        order_id="ORD-1042",
        amount_paise=120000,
        return_id=ret_d.return_id,
        method="original",
        idempotency_key=f"{pid_d}:reserve_refund",
    )
    with write_tx(engine) as conn:
        conn.execute(
            proposals.update()
            .where(proposals.c.proposal_id == pid_d)
            .values(status=ProposalStatus.executing.value, version=2)
        )
        t_start = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        for idx, (st_name, s_ref, st_val) in enumerate(
            [
                ("create_return", ret_d.return_id, "succeeded"),
                ("reserve_refund", rfd_d.refund_id, "succeeded"),
                ("transition_return", None, "failed"),
            ]
        ):
            conn.execute(
                executions.insert().values(
                    execution_id=f"EXEC-REV-{idx}",
                    proposal_id=pid_d,
                    step=st_name,
                    idempotency_key=f"{pid_d}:{st_name}",
                    status=st_val,
                    store_ref=s_ref,
                    error_code="conflict" if st_val == "failed" else None,
                    started_at=t_start,
                    finished_at=t_start,
                    attempts=1,
                    next_retry_at=None,
                    stuck=False,
                )
            )
    spy_store.reset_spy()
    await reconcile_once(clock, store_client=spy_store, engine=engine, min_age_seconds=0)
    comp_ops = [
        c["op"] for c in spy_store.calls if c["op"] in ("cancel_reservation", "transition_return")
    ]
    assert comp_ops == ["cancel_reservation", "transition_return"]
    assert all(
        c["role"] == Role.service and not c["has_token"]
        for c in spy_store.calls
        if c["op"] in ("cancel_reservation", "transition_return")
    )
    admin_d = await raw_store.get_order_admin(svc_ctx, "ORD-1042")
    assert admin_d.refunds[0].status == RefundStatus.released
    assert admin_d.returns[0].status == ReturnStatus.cancelled


# ── 4. timeout_after_commit on create_return -> Reconcile Resumes ───────────


@pytest.mark.asyncio
async def test_timeout_after_commit_on_create_return_unknown_then_reconcile_resumes_no_duplicate(
    saga_env: dict[str, Any],
) -> None:
    from sellershield_agent.execution.reconcile import reconcile_once
    from sellershield_agent.execution.saga import Executor, format_execution_status_message

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    fault_plan: FaultPlan = saga_env["fault_plan"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]
    executor = Executor(store_client=spy_store, engine=engine, clock=clock)

    card = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
    )
    pid = card["proposal_id"]
    fault_plan.timeout_after_commit("create_return")
    ctx = _make_customer_ctx(customer_id="C002", user_id="usr_c002", clock=clock)

    res = await confirm_proposal(
        ctx,
        pid,
        card["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-to-1",
    )
    assert res.status == "unknown"
    assert res.return_id is None
    assert res.message_key == "proposal.unknown"
    unknown_msg = format_execution_status_message(res.status, message_key=res.message_key)
    assert "verifying" in unknown_msg.lower()
    assert "created" not in unknown_msg.lower()
    assert "approved" not in unknown_msg.lower()
    assert spy_store.tx_violations == []

    with read_tx(engine) as conn:
        exec_to = conn.execute(select(executions).where(executions.c.proposal_id == pid)).fetchall()
        assert len(exec_to) == 1
        assert exec_to[0].step == "create_return"
        assert exec_to[0].status == "unknown"
        assert exec_to[0].store_ref is None
        assert exec_to[0].error_code == "timeout"
        prop_to = conn.execute(select(proposals).where(proposals.c.proposal_id == pid)).fetchone()
        assert prop_to is not None and prop_to.status == ProposalStatus.unknown.value

    svc_ctx = _make_service_ctx(clock)
    admin_mid = await raw_store.get_order_admin(svc_ctx, "ORD-2032")
    assert len(admin_mid.returns) == 1
    created_ret_id = admin_mid.returns[0].return_id
    assert admin_mid.returns[0].client_ref == f"{pid}:create_return"

    # Too early (< 5s): reconcile_once must not run
    fault_plan._timeout_after_commit_steps.clear()
    spy_store.reset_spy()
    clock.set(T0_BASE + timedelta(seconds=3))
    summary_early = await reconcile_once(clock, store_client=spy_store, engine=engine)
    assert summary_early["resolved"] == 0
    assert len(spy_store.calls) == 0

    # At >= 5s: reconcile_once finds return by client_ref and resumes to success
    clock.set(T0_BASE + timedelta(seconds=5))
    summary = await reconcile_once(clock, store_client=spy_store, engine=engine)
    assert summary["resolved"] >= 1
    assert spy_store.tx_violations == []
    assert all(c["role"] == Role.service and not c["has_token"] for c in spy_store.calls)
    assert any(c["op"] == "get_order_admin" for c in spy_store.calls)
    assert not any(c["op"] == "create_return" for c in spy_store.calls)

    admin_after = await raw_store.get_order_admin(svc_ctx, "ORD-2032")
    assert len(admin_after.returns) == 1
    assert admin_after.returns[0].return_id == created_ret_id
    assert admin_after.returns[0].status == ReturnStatus.approved
    assert len(admin_after.refunds) == 1
    assert admin_after.refunds[0].status == RefundStatus.reserved

    with read_tx(engine) as conn:
        prop_row = conn.execute(select(proposals).where(proposals.c.proposal_id == pid)).fetchone()
        assert prop_row is not None and prop_row.status == ProposalStatus.succeeded.value
        assert AuditService.verify_chain(conn).ok is True

    # Part B: timeout_after_commit on transition_return (ALLOW) -> reconciler detects approved
    raw_store.reset()
    clock.set(T0_BASE + timedelta(hours=1))
    card_tr = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C005",
        user_id="usr_c005",
        order_id="ORD-5001",
        items=[{"item_id": "ITEM-5001-1", "qty": 1}],
        reason_code="changed_mind",
    )
    pid_tr = card_tr["proposal_id"]
    fault_plan.timeout_after_commit("transition_return")
    ctx_c005 = _make_customer_ctx(customer_id="C005", user_id="usr_c005", clock=clock)
    res_tr = await confirm_proposal(
        ctx_c005,
        pid_tr,
        card_tr["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-to-tr",
    )
    assert res_tr.status == "unknown"

    fault_plan._timeout_after_commit_steps.clear()
    spy_store.reset_spy()
    clock.set(T0_BASE + timedelta(hours=1, seconds=5))
    await reconcile_once(clock, store_client=spy_store, engine=engine)
    assert not any(c["op"] == "transition_return" for c in spy_store.calls)
    assert any(c["op"] == "reserve_refund" for c in spy_store.calls)
    admin_tr = await raw_store.get_order_admin(svc_ctx, "ORD-5001")
    assert len(admin_tr.returns) == 1 and admin_tr.returns[0].status == ReturnStatus.approved
    assert len(admin_tr.refunds) == 1 and admin_tr.refunds[0].status == RefundStatus.reserved

    # Part C: timeout_after_commit on reserve_refund (NEEDS_APPROVAL) -> creates approval, 1 hold
    raw_store.reset()
    clock.set(T0_BASE + timedelta(hours=1, minutes=10))
    card_rr = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    pid_rr = card_rr["proposal_id"]
    fault_plan.timeout_after_commit("reserve_refund")
    ctx_c001 = _make_customer_ctx(customer_id="C001", user_id="usr_c001", clock=clock)
    res_rr = await confirm_proposal(
        ctx_c001,
        pid_rr,
        card_rr["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-to-rr",
    )
    assert res_rr.status == "unknown"

    fault_plan._timeout_after_commit_steps.clear()
    spy_store.reset_spy()
    clock.set(T0_BASE + timedelta(hours=1, minutes=10, seconds=5))
    await reconcile_once(clock, store_client=spy_store, engine=engine)
    assert not any(c["op"] == "reserve_refund" for c in spy_store.calls)
    admin_rr = await raw_store.get_order_admin(svc_ctx, "ORD-1042")
    assert len(admin_rr.refunds) == 1
    with read_tx(engine) as conn:
        apr_rr = conn.execute(select(approvals).where(approvals.c.proposal_id == pid_rr)).fetchone()
        assert apr_rr is not None and apr_rr.status == ApprovalStatus.pending.value
        assert AuditService.verify_chain(conn).ok is True


# ── 5. Unavailable, Same-Key Retry, and 5s/30s/2m -> Stuck Backoff ──────────


@pytest.mark.asyncio
async def test_unavailable_retries_same_key_at_5s_and_flags_stuck_after_30s_and_2m(
    saga_env: dict[str, Any],
) -> None:
    from sellershield_agent.execution.reconcile import reconcile_once
    from sellershield_agent.execution.saga import Executor

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    fault_plan: FaultPlan = saga_env["fault_plan"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]

    from sellershield_agent.domain.state import notifications_outbox

    executor = Executor(store_client=spy_store, engine=engine, clock=clock)
    svc_ctx = _make_service_ctx(clock)

    # Part A: fail_once (StoreUnavailable) on reserve_refund gives unknown, then reconciles at 5s
    card_a = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    fault_plan.fail_once("reserve_refund")
    ctx_a = _make_customer_ctx(customer_id="C001", user_id="usr_c001", clock=clock)
    res_a = await confirm_proposal(
        ctx_a,
        card_a["proposal_id"],
        card_a["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-unavail-a",
    )
    assert res_a.status == "unknown"

    spy_store.reset_spy()
    clock.set(T0_BASE + timedelta(seconds=5))
    await reconcile_once(clock, store_client=spy_store, engine=engine)

    res_calls = [c for c in spy_store.calls if c["op"] == "reserve_refund"]
    assert len(res_calls) == 1
    assert res_calls[0]["idempotency_key"] == f"{card_a['proposal_id']}:reserve_refund"
    assert res_calls[0]["role"] == Role.service and res_calls[0]["has_token"] is False
    assert spy_store.tx_violations == []

    admin_a = await raw_store.get_order_admin(svc_ctx, "ORD-1042")
    assert len(admin_a.refunds) == 1
    with read_tx(engine) as conn:
        apr = conn.execute(
            select(approvals).where(approvals.c.proposal_id == card_a["proposal_id"])
        ).fetchone()
        assert apr is not None and apr.status == ApprovalStatus.pending.value

    # Part B: sustained unavailable hits 5s, 30s, 2m schedule and flags stuck
    raw_store.reset()
    clock.set(T0_BASE + timedelta(hours=2))
    t_b0 = clock.now()
    card_b = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
    )
    fault_plan.unavailable("reserve_refund")
    ctx_b = _make_customer_ctx(customer_id="C002", user_id="usr_c002", clock=clock)
    res_b = await confirm_proposal(
        ctx_b,
        card_b["proposal_id"],
        card_b["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-unavail-b",
    )
    assert res_b.status == "unknown"

    # Retry 1 at 5s
    clock.set(t_b0 + timedelta(seconds=5))
    await reconcile_once(clock, store_client=spy_store, engine=engine)
    # Too soon before 30s
    spy_store.reset_spy()
    clock.set(t_b0 + timedelta(seconds=15))
    await reconcile_once(clock, store_client=spy_store, engine=engine)
    assert len(spy_store.calls) == 0

    # Retry 2 at 30s
    clock.set(t_b0 + timedelta(seconds=30))
    await reconcile_once(clock, store_client=spy_store, engine=engine)
    # Too soon before 2m
    spy_store.reset_spy()
    clock.set(t_b0 + timedelta(seconds=60))
    await reconcile_once(clock, store_client=spy_store, engine=engine)
    assert len(spy_store.calls) == 0

    # Retry 3 at 2m -> flagged stuck
    clock.set(t_b0 + timedelta(seconds=120))
    summary_stuck = await reconcile_once(clock, store_client=spy_store, engine=engine)
    assert summary_stuck["stuck"] == 1

    with read_tx(engine) as conn:
        rfd_exec = conn.execute(
            select(executions).where(
                executions.c.proposal_id == card_b["proposal_id"],
                executions.c.step == "reserve_refund",
            )
        ).fetchone()
        assert rfd_exec is not None and bool(rfd_exec.stuck) is True
        stuck_stubs = conn.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.template == "seller.execution_stuck"
            )
        ).fetchall()
        assert len(stuck_stubs) == 1

    # Recover store at 5m: stuck execution is skipped cleanly
    fault_plan._unavailable_steps.clear()
    spy_store.reset_spy()
    clock.set(t_b0 + timedelta(minutes=5))
    await reconcile_once(clock, store_client=spy_store, engine=engine)
    assert len(spy_store.calls) == 0

    # Part C: pre-commit unavailable on create_return resolves failed when admin confirms absent
    raw_store.reset()
    clock.set(T0_BASE + timedelta(hours=3))
    t_c0 = clock.now()
    card_c = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C005",
        user_id="usr_c005",
        order_id="ORD-5001",
        items=[{"item_id": "ITEM-5001-1", "qty": 1}],
        reason_code="changed_mind",
    )
    fault_plan.unavailable("create_return")
    ctx_c = _make_customer_ctx(customer_id="C005", user_id="usr_c005", clock=clock)
    res_c = await confirm_proposal(
        ctx_c,
        card_c["proposal_id"],
        card_c["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-unavail-c",
    )
    assert res_c.status == "unknown"

    fault_plan._unavailable_steps.clear()
    clock.set(t_c0 + timedelta(seconds=5))
    await reconcile_once(clock, store_client=spy_store, engine=engine)
    with read_tx(engine) as conn:
        prop_c = conn.execute(
            select(proposals).where(proposals.c.proposal_id == card_c["proposal_id"])
        ).fetchone()
        assert prop_c is not None and prop_c.status == ProposalStatus.failed.value
        assert AuditService.verify_chain(conn).ok is True


# ── 6. Crash Recovery on Startup Reconciler Before Serving ───────────────────


@pytest.mark.asyncio
async def test_crash_after_store_call_before_result_write_repaired_on_startup_before_serving(
    saga_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sellershield_agent.api import proposals as proposals_api
    from sellershield_agent.execution.saga import Executor

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]
    db_file: Path = saga_env["db_file"]

    card = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
    )
    pid = card["proposal_id"]

    # Crash immediately after create_return commits on store before result write_tx runs
    def _crash_after_store(step: str, _res: Any) -> None:
        if step == "create_return":
            raise _SimulatedCrash("Simulated SIGKILL after store commit")

    spy_store.on_after_store_write = _crash_after_store
    executor = Executor(store_client=spy_store, engine=engine, clock=clock)
    ctx = _make_customer_ctx(customer_id="C002", user_id="usr_c002", clock=clock)

    with pytest.raises(_SimulatedCrash):
        await confirm_proposal(
            ctx,
            pid,
            card["proposal_hash"],
            engine=engine,
            store_client=spy_store,
            executor=executor,
            clock=clock,
            idempotency_key="idem-crash-1",
        )

    spy_store.on_after_store_write = None
    with read_tx(engine) as conn:
        exec_row = conn.execute(
            select(executions).where(executions.c.proposal_id == pid)
        ).fetchone()
        assert exec_row is not None and exec_row.status == "executing"
        assert exec_row.store_ref is None
        prop_row = conn.execute(select(proposals).where(proposals.c.proposal_id == pid)).fetchone()
        assert prop_row is not None and prop_row.status == ProposalStatus.executing.value

    # Dispose crashed process engine and restart FastAPI app against same DB & Store
    engine.dispose()
    reset_engine()
    get_settings.cache_clear()

    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: clock
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: spy_store

    with TestClient(app, raise_server_exceptions=False) as client:
        # Must already be repaired on startup before any request completes
        restarted_engine = get_engine()
        with read_tx(restarted_engine) as conn:
            prop_repaired = conn.execute(
                select(proposals).where(proposals.c.proposal_id == pid)
            ).fetchone()
            assert prop_repaired is not None
            assert prop_repaired.status == ProposalStatus.succeeded.value

            execs = conn.execute(
                select(executions).where(executions.c.proposal_id == pid)
            ).fetchall()
            assert len(execs) == 3
            assert all(r.status == "succeeded" for r in execs)
            assert AuditService.verify_chain(conn).ok is True

        svc_ctx = _make_service_ctx(clock)
        admin_ord = await raw_store.get_order_admin(svc_ctx, "ORD-2032")
        assert len(admin_ord.returns) == 1
        assert admin_ord.returns[0].status == ReturnStatus.approved
        assert len(admin_ord.refunds) == 1

        health = client.get("/healthz")
        assert health.status_code == 200
        assert spy_store.tx_violations == []

    app.dependency_overrides.clear()
    restarted = get_engine()
    if restarted is not engine:
        restarted.dispose()
    reset_engine()

    # Part B: Crash strictly BETWEEN step 1 commit and step 2 start (0 pending executions)
    db_file_b = db_file.parent / "saga_crash_between.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file_b.as_posix()}")
    get_settings.cache_clear()
    engine_b = create_engine_from_url(f"sqlite:///{db_file_b.as_posix()}")
    init_db(engine_b)
    raw_store.reset()
    spy_b = _TransactionSpyStoreClient(raw_store, engine_b)

    card_b = await _propose_return(
        engine=engine_b,
        store=raw_store,
        clock=clock,
        customer_id="C005",
        user_id="usr_c005",
        order_id="ORD-5001",
        items=[{"item_id": "ITEM-5001-1", "qty": 1}],
        reason_code="changed_mind",
    )
    pid_b = card_b["proposal_id"]
    orig_append = AuditService.append

    def _crash_between_steps(cls: type[AuditService], tx: Any, event_obj: Any) -> int:
        details_str = str(getattr(event_obj, "details_json", "") or "")
        if (
            getattr(event_obj, "action", "") == "execution_step_started"
            and "transition_return" in details_str
        ):
            raise _SimulatedCrash("Simulated SIGKILL between step 1 commit and step 2")
        return orig_append(tx, event_obj)

    monkeypatch.setattr(AuditService, "append", classmethod(_crash_between_steps))
    executor_b = Executor(store_client=spy_b, engine=engine_b, clock=clock)
    ctx_b = _make_customer_ctx(customer_id="C005", user_id="usr_c005", clock=clock)

    with pytest.raises(_SimulatedCrash):
        await confirm_proposal(
            ctx_b,
            pid_b,
            card_b["proposal_hash"],
            engine=engine_b,
            store_client=spy_b,
            executor=executor_b,
            clock=clock,
            idempotency_key="idem-crash-between",
        )

    monkeypatch.undo()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file_b.as_posix()}")
    get_settings.cache_clear()
    with read_tx(engine_b) as conn:
        b_execs = conn.execute(
            select(executions).where(executions.c.proposal_id == pid_b)
        ).fetchall()
        assert len(b_execs) == 1 and b_execs[0].step == "create_return"
        assert b_execs[0].status == "succeeded"
    engine_b.dispose()
    reset_engine()

    app_b = create_app()
    app_b.dependency_overrides[deps.get_clock] = lambda: clock
    app_b.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: spy_b
    with TestClient(app_b, raise_server_exceptions=False) as client_b:
        assert client_b.get("/healthz").status_code == 200
        with read_tx(get_engine()) as conn:
            prop_b = conn.execute(
                select(proposals).where(proposals.c.proposal_id == pid_b)
            ).fetchone()
            assert prop_b is not None and prop_b.status == ProposalStatus.succeeded.value
            assert AuditService.verify_chain(conn).ok is True
        assert spy_b.tx_violations == []

    app_b.dependency_overrides.clear()
    reset_engine()
    saga_env["engine"] = create_engine_from_url(f"sqlite:///{db_file.as_posix()}")


@pytest.mark.asyncio
async def test_lifespan_background_reconcile_loop_runs_periodically_and_cancels_cleanly(
    saga_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sellershield_agent.main as main_mod
    from sellershield_agent.api import proposals as proposals_api

    clock: FixedClock = saga_env["clock"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]
    call_count = 0

    async def _fake_reconcile_once(*args: Any, **kwargs: Any) -> dict[str, int]:
        nonlocal call_count
        call_count += 1
        return {"scanned": 0, "resolved": 0, "stuck": 0}

    monkeypatch.setattr(main_mod, "RECONCILE_INTERVAL_SECONDS", 0.02, raising=False)
    monkeypatch.setattr(main_mod, "reconcile_once", _fake_reconcile_once, raising=False)

    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: clock
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: spy_store

    with TestClient(app, raise_server_exceptions=True) as client:
        assert client.get("/healthz").status_code == 200
        deadline = time.monotonic() + 1.0
        while call_count < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    assert call_count >= 2
    app.dependency_overrides.clear()


# ── 7. Double Confirmation Never Creates Second Return ──────────────────────


@pytest.mark.asyncio
async def test_confirming_same_proposal_twice_never_creates_second_return(
    saga_env: dict[str, Any],
) -> None:
    from sellershield_agent.errors import AppHTTPException
    from sellershield_agent.execution.saga import Executor

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]
    executor = Executor(store_client=spy_store, engine=engine, clock=clock)

    card = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
    )
    pid = card["proposal_id"]
    prepared = _load_prepared(engine, pid, clock)
    ctx = _make_customer_ctx(customer_id="C002", user_id="usr_c002", clock=clock)

    r1 = await confirm_proposal(
        ctx,
        pid,
        card["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-double-1",
    )
    r2_replay = await confirm_proposal(
        ctx,
        pid,
        card["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-double-1",
    )
    assert r2_replay == r1

    with pytest.raises(AppHTTPException) as exc_info:
        await confirm_proposal(
            ctx,
            pid,
            card["proposal_hash"],
            engine=engine,
            store_client=spy_store,
            executor=executor,
            clock=clock,
            idempotency_key="idem-double-new-key",
        )
    assert exc_info.value.status_code == 409

    # Even direct second Executor.run on same PreparedProposal stays idempotent
    r3_direct = await executor.run(ctx, prepared, clock=clock)
    assert r3_direct.status == "succeeded"
    assert r3_direct.return_id == r1.return_id

    svc_ctx = _make_service_ctx(clock)
    admin_ord = await raw_store.get_order_admin(svc_ctx, "ORD-2032")
    assert len(admin_ord.returns) == 1
    assert len(admin_ord.refunds) == 1


# ── 8. Audit Failure Aborts Step Write & Leaves Executing for Reconciler ─────


@pytest.mark.asyncio
async def test_audit_failure_at_step_aborts_step_write_and_leaves_executing_for_reconciler(
    saga_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sellershield_agent.execution.reconcile import reconcile_once
    from sellershield_agent.execution.saga import Executor

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]

    card = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
    )
    pid = card["proposal_id"]
    prepared = _load_prepared(engine, pid, clock)
    ctx = _make_customer_ctx(customer_id="C002", user_id="usr_c002", clock=clock)

    orig_append = AuditService.append

    def _fail_post_store_step_audit(cls: type[AuditService], tx: Any, event_obj: Any) -> int:
        if getattr(event_obj, "action", "") == "execution_step_succeeded":
            raise RuntimeError("Simulated audit write failure on post-store step")
        return orig_append(tx, event_obj)

    monkeypatch.setattr(AuditService, "append", classmethod(_fail_post_store_step_audit))
    executor = Executor(store_client=spy_store, engine=engine, clock=clock)

    with pytest.raises(RuntimeError, match="audit write failure"):
        await executor.run(ctx, prepared, clock=clock)

    # Restore audit before verifying & reconciling
    monkeypatch.undo()

    with read_tx(engine) as conn:
        exec_rows = conn.execute(
            select(executions).where(executions.c.proposal_id == pid)
        ).fetchall()
        assert len(exec_rows) == 1
        assert exec_rows[0].step == "create_return"
        assert exec_rows[0].status == "executing"
        assert exec_rows[0].store_ref is None
        assert exec_rows[0].finished_at is None
        assert AuditService.verify_chain(conn).ok is True

    # Reconcile repairs the aborted executing step without duplicate return
    clock.set(T0_BASE + timedelta(seconds=6))
    await reconcile_once(clock, store_client=spy_store, engine=engine)

    svc_ctx = _make_service_ctx(clock)
    admin_ord = await raw_store.get_order_admin(svc_ctx, "ORD-2032")
    assert len(admin_ord.returns) == 1
    assert admin_ord.returns[0].status == ReturnStatus.approved
    assert len(admin_ord.refunds) == 1

    with read_tx(engine) as conn:
        prop_row = conn.execute(select(proposals).where(proposals.c.proposal_id == pid)).fetchone()
        assert prop_row is not None and prop_row.status == ProposalStatus.succeeded.value
        assert AuditService.verify_chain(conn).ok is True


# ── 9. Duplicate Active Return & Hold Compensation ──────────────────────────


@pytest.mark.asyncio
async def test_duplicate_active_return_becomes_failed_with_existing_id_and_compensates_hold(
    saga_env: dict[str, Any],
) -> None:
    from sellershield_agent.execution.saga import Executor

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]
    svc_ctx = _make_service_ctx(clock)

    # Subcase A: active external return created after propose_return
    card_a = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        items=[{"item_id": "ITEM-2032-1", "qty": 1}],
        reason_code="changed_mind",
    )
    pid_a = card_a["proposal_id"]
    ctx_c002 = _make_customer_ctx(customer_id="C002", user_id="usr_c002", clock=clock)
    ext_ret = await raw_store.create_return(
        ctx_c002,
        order_id="ORD-2032",
        items=[ReturnItem(item_id="ITEM-2032-1", qty=1)],
        reason_code=ReturnReasonCode.changed_mind.value,
        reason_text="External return created separately",
        resolution=ReturnResolution.refund.value,
        idempotency_key="ext-ret-dup-1",
    )

    prepared_a = _load_prepared(engine, pid_a, clock)
    executor = Executor(store_client=spy_store, engine=engine, clock=clock)
    res_a = await executor.run(ctx_c002, prepared_a, clock=clock)

    assert res_a.status == "failed"
    assert res_a.return_id == ext_ret.return_id
    assert res_a.message_key == "proposal.failed_duplicate"

    # External return must remain requested (never compensated away!)
    admin_a = await raw_store.get_order_admin(svc_ctx, "ORD-2032")
    assert len(admin_a.returns) == 1
    assert admin_a.returns[0].status == ReturnStatus.requested

    # Subcase B: duplicate error when a saga hold already existed -> hold compensated
    raw_store.reset()
    card_b = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    pid_b = card_b["proposal_id"]
    ctx_c001 = _make_customer_ctx(customer_id="C001", user_id="usr_c001", clock=clock)
    ext_ret_b = await raw_store.create_return(
        ctx_c001,
        order_id="ORD-1042",
        items=[ReturnItem(item_id="ITEM-1042-1", qty=1)],
        reason_code=ReturnReasonCode.defective.value,
        reason_text="External active return",
        resolution=ReturnResolution.refund.value,
        idempotency_key="ext-ret-dup-2",
    )
    prior_hold = await raw_store.reserve_refund(
        svc_ctx,
        order_id="ORD-1042",
        amount_paise=120000,
        return_id=None,
        method="original",
        idempotency_key=f"{pid_b}:reserve_refund",
    )
    with write_tx(engine) as conn:
        conn.execute(
            executions.insert().values(
                execution_id=f"EXEC-{uuid.uuid4().hex[:8]}",
                proposal_id=pid_b,
                step="reserve_refund",
                idempotency_key=f"{pid_b}:reserve_refund",
                status="succeeded",
                store_ref=prior_hold.refund_id,
                error_code=None,
                started_at=T0_BASE.isoformat(),
                finished_at=T0_BASE.isoformat(),
                attempts=1,
                next_retry_at=None,
                stuck=False,
            )
        )

    prepared_b = _load_prepared(engine, pid_b, clock)
    res_b = await executor.run(ctx_c001, prepared_b, clock=clock)
    assert res_b.status == "failed"
    assert res_b.return_id == ext_ret_b.return_id

    admin_b = await raw_store.get_order_admin(svc_ctx, "ORD-1042")
    assert admin_b.returns[0].status == ReturnStatus.requested
    assert admin_b.refunds[0].status == RefundStatus.released
    assert spy_store.tx_violations == []


# ── 10. Daily-Cap Error Fails Safely Without Internal Limit Leaks ────────────


@pytest.mark.asyncio
async def test_daily_cap_error_compensates_and_emits_neutral_customer_message(
    saga_env: dict[str, Any],
) -> None:
    from sellershield_agent.execution.saga import Executor, format_execution_status_message

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]

    card = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    pid = card["proposal_id"]
    ctx = _make_customer_ctx(customer_id="C001", user_id="usr_c001", clock=clock)

    spy_store.raise_reserve_error = StoreLimitExceeded(
        "Daily autonomous refund cap exceeded: 550000 > 500000"
    )
    executor = Executor(store_client=spy_store, engine=engine, clock=clock)

    res = await confirm_proposal(
        ctx,
        pid,
        card["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-daily-cap",
    )

    assert res.status == "failed"
    assert res.return_id is None
    assert res.approval_id is None
    msg = format_execution_status_message(res.status, message_key=res.message_key)
    _assert_customer_safe(msg)
    assert "500000" not in msg and "exceeded" not in msg.lower()

    svc_ctx = _make_service_ctx(clock)
    admin_ord = await raw_store.get_order_admin(svc_ctx, "ORD-1042")
    assert admin_ord.returns[0].status == ReturnStatus.cancelled
    assert len(admin_ord.refunds) == 0
    assert spy_store.tx_violations == []

    with read_tx(engine) as conn:
        cevs = conn.execute(
            select(client_events).where(client_events.c.customer_id == "C001")
        ).fetchall()
        for c in cevs:
            _assert_customer_safe(c.payload_json)
            assert "500000" not in c.payload_json
            assert "cap" not in c.payload_json.lower()


# ── 11. Token Isolation, Redaction & No Tokens Stored Anywhere ───────────────


@pytest.mark.asyncio
async def test_customer_token_never_stored_logged_or_used_by_reconciler_or_compensation(
    saga_env: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sellershield_agent.execution.reconcile import reconcile_once
    from sellershield_agent.execution.saga import Executor

    configure_logging()
    caplog.set_level(logging.DEBUG)

    engine: Engine = saga_env["engine"]
    clock: FixedClock = saga_env["clock"]
    fault_plan: FaultPlan = saga_env["fault_plan"]
    raw_store: FixtureStoreClient = saga_env["raw_store"]
    spy_store: _TransactionSpyStoreClient = saga_env["store"]

    from sellershield_agent.domain.state import notifications_outbox

    executor = Executor(store_client=spy_store, engine=engine, clock=clock)

    canary_jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c3JfYzAwMSJ9.SIGCANARY_NEVER_PERSIST"
    ctx = _make_customer_ctx(
        customer_id="C001",
        user_id="usr_c001",
        clock=clock,
        token_value=canary_jwt,
        exp_seconds=30,
    )

    # A: Reconciler isolation after customer token has already expired + token redaction check
    card = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "qty": 1}],
        reason_code="defective",
    )
    fault_plan.timeout_after_commit("create_return")
    await confirm_proposal(
        ctx,
        card["proposal_id"],
        card["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-tok-1",
    )

    fault_plan._timeout_after_commit_steps.clear()
    spy_store.reset_spy()
    # Advance clock past 30s JWT lifetime -> reconciler still succeeds via ServiceContext
    clock.set(T0_BASE + timedelta(minutes=2))
    await reconcile_once(clock, store_client=spy_store, engine=engine)

    # Every call made during reconcile used ServiceContext with zero customer token
    assert len(spy_store.calls) >= 2
    for call in spy_store.calls:
        assert call["role"] == Role.service
        assert call["customer_id"] is None
        assert call["token_value"] is None

    # B: Compensation isolation (conflict on reserve_refund uses ServiceContext only)
    raw_store.reset()
    clock.set(T0_BASE + timedelta(minutes=20))
    card_comp = await _propose_return(
        engine=engine,
        store=raw_store,
        clock=clock,
        customer_id="C003",
        user_id="usr_c003",
        order_id="ORD-3011",
        items=[
            {"item_id": "ITEM-3011-1", "qty": 1},
            {"item_id": "ITEM-3011-2", "qty": 1},
        ],
        reason_code="defective",
    )
    fault_plan.conflict("reserve_refund")
    ctx_comp = _make_customer_ctx(
        customer_id="C003", user_id="usr_c003", clock=clock, token_value=canary_jwt
    )
    spy_store.reset_spy()
    await confirm_proposal(
        ctx_comp,
        card_comp["proposal_id"],
        card_comp["proposal_hash"],
        engine=engine,
        store_client=spy_store,
        executor=executor,
        clock=clock,
        idempotency_key="idem-tok-comp",
    )
    comp_calls = [c for c in spy_store.calls if c["op"] == "transition_return"]
    assert len(comp_calls) == 1
    assert comp_calls[0]["role"] == Role.service
    assert comp_calls[0]["customer_id"] is None
    assert comp_calls[0]["token_value"] is None

    # No DB row in any table may ever contain the customer JWT
    with read_tx(engine) as conn:
        for tbl in (
            proposals,
            executions,
            approvals,
            client_events,
            notifications_outbox,
            audit_events,
        ):
            rows = conn.execute(select(tbl)).fetchall()
            blob = "\n".join(str(tuple(r)) for r in rows)
            assert canary_jwt not in blob
            assert "SIGCANARY_NEVER_PERSIST" not in blob

    captured = capsys.readouterr()
    all_logs = f"{caplog.text}\n{captured.out}\n{captured.err}"
    assert canary_jwt not in all_logs
    assert "SIGCANARY_NEVER_PERSIST" not in all_logs
