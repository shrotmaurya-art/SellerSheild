"""Shared Step A test helpers for T2.8 mutator atomicity and coverage tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select, update

from sellershield_agent.approvals.decide_db import claim_approving_sync
from sellershield_agent.audit.models import ActorType
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, get_metadata, init_db, read_tx, reset_engine, write_tx
from sellershield_agent.domain.models import (
    CustomerRiskFacts,
    Refundable,
    RefundableItem,
    RefundMethod,
    ReturnItem,
    ReturnReasonCode,
    ReturnResolution,
)
from sellershield_agent.domain.session import sessions
from sellershield_agent.domain.state import http_idempotency_keys, proposals
from sellershield_agent.execution.common import SagaPlan
from sellershield_agent.execution.db_ops import (
    finish_execution_step_sync,
    prepare_run_sync,
    start_execution_step_sync,
)
from sellershield_agent.execution.finalize_db import finalize_approval_sync
from sellershield_agent.orchestrator.pipeline import ConversationPipeline
from sellershield_agent.policy.decide import decide
from sellershield_agent.policy.facts import DecisionAction, DecisionFacts
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.policy.rules import DecisionOutcome
from sellershield_agent.proposals.confirm_audit import ActionSpec
from sellershield_agent.proposals.executor import ExecutionResult
from sellershield_agent.proposals.integrity import PreparedProposal
from sellershield_agent.proposals.revalidate import RevalidationOutcome
from sellershield_agent.proposals.service import persist_proposal_and_build_card
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway

FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scenarios" / "fixtures" / "store.json"
)
SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "sellershield_agent"
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


def snapshot_db(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    """Capture a deterministic sorted snapshot of every table in shared MetaData."""
    snap: dict[str, list[tuple[Any, ...]]] = {}
    with read_tx(engine) as conn:
        for name, tbl in sorted(get_metadata().tables.items()):
            rows = [tuple(r) for r in conn.execute(select(tbl)).fetchall()]
            rows.sort(key=lambda row: tuple(str(col) for col in row))
            snap[name] = rows
    return snap


def build_facts(clock: FixedClock, order: Any, customer_id: str) -> DecisionFacts:
    """Construct clean DecisionFacts for ORD-1042 style fixtures."""
    return DecisionFacts(
        action=DecisionAction.return_,
        order=order,
        items=[ReturnItem(item_id="ITEM-1042-1", qty=1)],
        reason_code=ReturnReasonCode.defective,
        reason_text=None,
        resolution=ReturnResolution.refund,
        refund_method=RefundMethod.original,
        evidence_present=False,
        active_returns=[],
        previous_refunds_30d_paise=0,
        refundable=Refundable(
            order_id=order.order_id,
            max_refundable_paise=120000,
            items=[
                RefundableItem(
                    item_id="ITEM-1042-1", original_qty=1, remaining_qty=1,
                    line_paid_paise=120000, refundable_paise=120000,
                )
            ],
        ),
        customer_risk=CustomerRiskFacts(
            customer_id=customer_id,
            returns_30d=0,
            refund_ratio_90d_bp=0,
            account_age_days=180,
            refunded_30d_paise=0,
        ),
        shipment=None,
        now=clock.now(),
    )


def build_ord1042_bundle(
    clock: FixedClock, customer_id: str = "C001"
) -> tuple[Any, Any, DecisionFacts, Any]:
    """Build Order, Policy, DecisionFacts, and Decision from fixtures for ORD-1042."""
    store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=clock)
    policy = load_policy("v3")
    now_ts = int(clock.now().timestamp())
    req_ctx = RequestContext(
        principal=Principal(
            user_id="usr_c001", role=Role.customer, customer_id="C001",
            jti="jti-helper-ord1042", token_exp=now_ts + 3600,
        ),
        request_id="req-helper-ord1042", token=SecretStr("tok-helper"),
    )
    order = asyncio.run(store.get_order(req_ctx, "ORD-1042")).model_copy(
        update={"customer_id": customer_id}
    )
    facts = build_facts(clock, order, customer_id)
    dec = decide(facts, policy, clock.now())
    return order, policy, facts, dec


def make_saga_plan(
    proposal_id: str,
    customer_id: str,
    facts_hash: str,
    *,
    session_id: str = "ses-t20",
    request_id: str = "req-t20",
) -> SagaPlan:
    """Create a deterministic SagaPlan for ORD-1042 single-item return."""
    return SagaPlan(
        proposal_id=proposal_id,
        customer_id=customer_id,
        session_id=session_id,
        proposal_type="return",
        decision="needs_approval",
        order_id="ORD-1042",
        items=[{"item_id": "ITEM-1042-1", "quantity": 1}],
        amount_paise=120000,
        refund_method="original",
        resolution="refund",
        reason_code="defective",
        reason_text="broken",
        policy_version="v3",
        facts_hash=facts_hash,
        request_id=request_id,
    )


def seed_ord1042_proposal(
    engine: Engine,
    *,
    clock: FixedClock,
    policy: Any,
    order: Any,
    facts: DecisionFacts,
    dec: Any,
    customer_id: str,
    request_id: str,
    session_id: str,
    reason_text: str = "broken",
) -> str:
    """Persist an ORD-1042 needs_approval proposal and return its proposal_id."""
    _, pid, limited = persist_proposal_and_build_card(
        engine,
        clock=clock,
        policy=policy,
        request_id=request_id,
        session_id=session_id,
        customer_id=customer_id,
        order=order.model_copy(update={"customer_id": customer_id}),
        proposal_type="return",
        requested_items=[ReturnItem(item_id="ITEM-1042-1", qty=1)],
        facts=facts,
        decision=dec,
        effective_outcome=DecisionOutcome.needs_approval,
        method_str="original",
        resolution_str="refund",
        sanitized_reason_text=reason_text,
        extra_payload={"reason_code": "defective"},
    )
    assert not limited and pid is not None
    return pid


def setup_t20_coverage_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Seed isolated SQLite DB containing realistic rows for every public mutator."""
    db_file = tmp_path / "t20_coverage.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    get_settings.cache_clear()
    reset_engine()

    clock = FixedClock(T0_BASE)
    now_iso = "2026-11-08T12:00:00Z"
    store = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=clock)
    settings = get_settings()
    engine = get_engine()
    init_db(engine, get_metadata())

    order, policy, facts, dec = build_ord1042_bundle(clock, customer_id="C001")
    cust_principal = Principal(
        user_id="usr_c001",
        role=Role.customer,
        customer_id="C001",
        jti="jti-t20-cov",
        token_exp=int(clock.now().timestamp()) + 3600,
    )
    cust_ctx = RequestContext(
        principal=cust_principal, request_id="req-t20-cov", token=SecretStr("tok-cust")
    )
    pipeline = ConversationPipeline(
        engine=engine, clock=clock, store_client=store, settings=settings
    )
    gateway = ToolGateway(store_client=store, engine=engine, clock=clock, policy=policy)

    ses_seed = "ses_t20_seed"
    with write_tx(engine) as tx:
        tx.execute(
            sessions.insert().values(
                id=ses_seed, customer_id="C001", channel="web", handler="agent",
                language="en", state_json="{}", started_at=now_iso, last_active_at=now_iso,
            )
        )

    def _seed(cust: str, suffix: str) -> str:
        return seed_ord1042_proposal(
            engine, clock=clock, policy=policy, order=order, facts=facts, dec=dec,
            customer_id=cust, request_id=f"req-seed-{suffix}", session_id=ses_seed,
        )

    pid_prop, pid_exp = _seed("C-T20-PROP", "prop"), _seed("C-T20-EXP", "exp")
    pid_exec, pid_with_apr, pid_aprv = (
        _seed("C-T20-EXEC", "exec"), _seed("C-T20-APR1", "apr1"), _seed("C-T20-APR2", "apr2")
    )

    with write_tx(engine) as tx:
        tx.execute(
            update(proposals).where(proposals.c.proposal_id == pid_exp).values(
                expires_at="2026-11-08T11:00:00Z"
            )
        )

    plan_prop = make_saga_plan(pid_prop, "C-T20-PROP", dec.facts_hash, session_id=ses_seed)
    plan_exec = make_saga_plan(pid_exec, "C-T20-EXEC", dec.facts_hash, session_id=ses_seed)
    plan_apr1 = make_saga_plan(pid_with_apr, "C-T20-APR1", dec.facts_hash, session_id=ses_seed)
    plan_apr2 = make_saga_plan(pid_aprv, "C-T20-APR2", dec.facts_hash, session_id=ses_seed)

    prepare_run_sync(engine, plan_exec, clock=clock, request_id="req-t20-seed")
    exec_id_exec, _ = start_execution_step_sync(
        engine, proposal_id=pid_exec, step="create_return",
        idempotency_key=f"{pid_exec}:create_return", actor_type=ActorType.CUSTOMER,
        actor_id="C-T20-EXEC", policy_version="v3", session_id=ses_seed,
        request_id="req-t20-seed", clock=clock,
    )
    with write_tx(engine) as tx:
        tx.execute(
            http_idempotency_keys.insert().values(
                customer_id="C-T20-EXEC", idempotency_key="idem-seed-exec", operation="confirm",
                proposal_id=pid_exec, request_hash="h-exec", status_code=200,
                response_json="{}", created_at=now_iso,
            )
        )

    prepare_run_sync(engine, plan_apr1, clock=clock, request_id="req-t20-seed")
    aid_pend = finalize_approval_sync(
        engine, plan=plan_apr1, return_id="RET-APR-1", refund_id="RFD-APR-1", clock=clock,
        request_id="req-t20-seed", actor_type=ActorType.CUSTOMER, actor_id="C-T20-APR1",
    )
    rel_step, _ = start_execution_step_sync(
        engine, proposal_id=pid_with_apr, step="release_refund",
        idempotency_key=f"{pid_with_apr}:release_refund", actor_type=ActorType.SYSTEM,
        actor_id="svc", policy_version="v3", session_id=ses_seed,
        request_id="req-t20-seed", clock=clock,
    )
    finish_execution_step_sync(
        engine, proposal_id=pid_with_apr, execution_id=rel_step, step="release_refund",
        new_status="unknown", store_ref="RFD-APR-1", error_code="timeout",
        actor_type=ActorType.SYSTEM, actor_id="svc", policy_version="v3",
        session_id=ses_seed, request_id="req-t20-seed", clock=clock,
    )
    prepare_run_sync(engine, plan_apr2, clock=clock, request_id="req-t20-seed")
    aid_aprv = finalize_approval_sync(
        engine, plan=plan_apr2, return_id="RET-APR-2", refund_id="RFD-APR-2", clock=clock,
        request_id="req-t20-seed", actor_type=ActorType.CUSTOMER, actor_id="C-T20-APR2",
    )
    v_aprv = claim_approving_sync(
        engine, approval_id=aid_aprv, expected_version=1, actor_id="seller-1",
        clock=clock, request_id="req-t20-seed",
    )

    with read_tx(engine) as conn:
        prop_row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == pid_prop)
        ).fetchone()
        exp_row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == pid_exp)
        ).fetchone()
        exec_row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == pid_exec)
        ).fetchone()
    assert prop_row is not None and exp_row is not None and exec_row is not None
    prop_hash = str(prop_row.proposal_hash)
    exp_hash = str(exp_row.proposal_hash)

    def _spec(op: str, pid: str, cust: str, key: str, h: str | None) -> ActionSpec:
        return ActionSpec(
            operation=op,
            proposal_id=pid,
            customer_id=cust,
            idempotency_key=f"idem-t20-{key}",
            request_hash=f"h-{key}",
            expected_hash=h,
            clock=clock,
            request_id="req-t20",
            request_path=f"/v1/proposals/{pid}/{op}",
        )

    return {
        "engine": engine, "clock": clock, "now_iso": now_iso, "store": store,
        "policy": policy, "order": order, "facts": facts, "dec": dec,
        "cust_principal": cust_principal, "cust_ctx": cust_ctx,
        "pipeline": pipeline, "gateway": gateway, "ses_seed": ses_seed,
        "pid_prop": pid_prop, "pid_with_apr": pid_with_apr, "pid_exec": pid_exec,
        "pid_aprv": pid_aprv, "exec_id_exec": exec_id_exec,
        "exec_prop_ver": int(exec_row.version), "aid_pend": aid_pend,
        "aid_aprv": aid_aprv, "v_aprv": v_aprv, "plan_prop": plan_prop,
        "plan_exec": plan_exec, "plan_apr1": plan_apr1,
        "spec_prop": _spec("confirm", pid_prop, "C-T20-PROP", "c", prop_hash),
        "spec_missing": _spec("confirm", "PRP-MISSING", "C-T20-PROP", "m", prop_hash),
        "spec_expired": _spec("confirm", pid_exp, "C-T20-EXP", "e", exp_hash),
        "spec_cancel": _spec("cancel", pid_prop, "C-T20-PROP", "x", None),
        "spec_exec": ActionSpec(
            operation="confirm", proposal_id=pid_exec, customer_id="C-T20-EXEC",
            idempotency_key="idem-seed-exec", request_hash="h-exec", expected_hash="h-exec",
            clock=clock, request_id="req-t20", request_path=f"/v1/proposals/{pid_exec}/confirm",
        ),
        "prep_prop": PreparedProposal(
            proposal_id=pid_prop,
            customer_id="C-T20-PROP",
            proposal_type="return",
            order_id="ORD-1042",
            items=[{"item_id": "ITEM-1042-1", "quantity": 1}],
            canonical_amounts={
                "items_paise": 120000,
                "shipping_refund_paise": 0,
                "fee_paise": 0,
                "total_paise": 120000,
            },
            method="original",
            resolution="refund",
            reason_code="defective",
            evidence_ids=[],
            decision="needs_approval",
            policy_version="v3",
            proposal_hash=prop_hash,
            expires_at="2026-11-08T12:10:00Z",
            customer_risk=facts.customer_risk.model_copy(update={"customer_id": "C-T20-PROP"}),
            previous_refunds_30d_paise=0,
            session_id=ses_seed,
        ),
        "reval_stale": RevalidationOutcome(
            is_stale=True,
            reason="stale",
            active_policy=policy,
            order=order,
            live_facts=facts,
            live_decision=dec,
            effective_outcome=DecisionOutcome.deny,
            requested_items=[ReturnItem(item_id="ITEM-1042-1", qty=1)],
            method_str="original",
            resolution_str="refund",
            live_amounts=None,
            rule_text="stale",
        ),
        "res_pend": ExecutionResult(
            status="pending_approval", return_id="R1", approval_id=aid_pend
        ),
        "res_succ": ExecutionResult(status="succeeded", return_id="R1"),
    }
