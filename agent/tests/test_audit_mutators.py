"""Step A tests for T2.8 mutator atomicity (T19/T20), EventTypes, tables, and single-writer rules.

Covers:
- Required EventType values and llm_calls / tool_calls metadata tables.
- Unified domain.state.transition() function and AST enforcement of single writer rules.
- T19: 8-thread concurrent saga writes produce a fork-free valid hash chain.
- T20: Injected audit failure on saga steps rolls back business and execution state cleanly.
"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text

import sellershield_agent.domain.state as state_mod
from sellershield_agent.approvals.decide_db import claim_approving_sync, finalize_approved_sync
from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.clock import FixedClock
from sellershield_agent.db import create_engine_from_url, get_metadata, init_db, read_tx
from sellershield_agent.domain.state import (
    ApprovalStatus,
    InvalidTransition,
    ProposalStatus,
    approvals,
    client_events,
    executions,
    notifications_outbox,
    proposals,
)
from sellershield_agent.execution.db_ops import (
    finish_execution_step_sync,
    prepare_run_sync,
    start_execution_step_sync,
)
from sellershield_agent.execution.finalize_db import finalize_approval_sync
from tests.audit_test_helpers import (
    SRC_ROOT,
    T0_BASE,
    build_facts,
    build_ord1042_bundle,
    make_saga_plan,
    seed_ord1042_proposal,
)


def _check_single_writer_ast() -> list[str]:
    """Ensure DB mutations to executions/proposals/approvals/audit_events stay centralized."""
    forbidden: list[str] = []
    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        rel = py_file.relative_to(SRC_ROOT).as_posix()
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_src = ast.unparse(node.func)
            arg0 = ast.unparse(node.args[0]) if node.args else ""
            if (func_src == "executions.insert" or (func_src == "insert" and arg0 == "executions")):
                if rel != "execution/db_ops.py":
                    forbidden.append(f"{rel}:{node.lineno}: executions insert")
            if (func_src == "approvals.insert" or (func_src == "insert" and arg0 == "approvals")):
                if rel != "execution/db_ops.py":
                    forbidden.append(f"{rel}:{node.lineno}: approvals insert")
            if func_src == "audit_events.insert" or (
                func_src == "insert" and arg0 == "audit_events"
            ):
                if rel != "audit/service.py":
                    forbidden.append(f"{rel}:{node.lineno}: audit_events insert")
            if func_src in ("update", "executions.update", "proposals.update", "approvals.update"):
                target = arg0 if func_src == "update" else func_src.split(".")[0]
                if target == "executions" and rel != "execution/db_ops.py":
                    forbidden.append(f"{rel}:{node.lineno}: update(executions)")
                if target == "proposals" and rel != "domain/state.py":
                    forbidden.append(f"{rel}:{node.lineno}: update(proposals)")
                if target == "approvals" and rel not in ("domain/state.py", "execution/db_ops.py"):
                    forbidden.append(f"{rel}:{node.lineno}: update(approvals)")
            if (
                func_src in ("update", "delete", "audit_events.update", "audit_events.delete")
                and (func_src.startswith("audit_events.") or arg0 == "audit_events")
            ):
                forbidden.append(f"{rel}:{node.lineno}: mutate(audit_events)")
    return forbidden


def test_required_event_types_tables_and_transition_exist() -> None:
    """Required EventTypes, telemetry tables, unified transition(), and single-writer rules."""
    for ev_name in (
        "policy_decision",
        "proposal_created",
        "proposal_confirmed",
        "execution_step",
        "approval_created",
        "approval_decided",
        "reconcile",
        "webhook_received",
        "notification_stub",
    ):
        assert EventType(ev_name).value == ev_name

    md = get_metadata()
    assert "llm_calls" in md.tables and "tool_calls" in md.tables
    llm_cols = set(md.tables["llm_calls"].c.keys())
    tool_cols = set(md.tables["tool_calls"].c.keys())
    assert {
        "call_id", "request_id", "session_id", "stage", "provider", "model",
        "prompt_version", "prompt_hash", "tokens_in", "tokens_out",
        "latency_ms", "cached", "status", "created_at",
    }.issubset(llm_cols)
    assert {
        "call_id", "request_id", "session_id", "tool", "args_hash",
        "args_redacted_json", "status", "error_code", "latency_ms", "created_at",
    }.issubset(tool_cols)

    forbidden = _check_single_writer_ast()
    assert not forbidden, f"Bypassing mutators found: {forbidden}"

    transition_fn = getattr(state_mod, "transition", None)
    assert callable(transition_fn), "domain.state.transition must exist"

    engine = create_engine_from_url("sqlite:///:memory:")
    try:
        clock = FixedClock(T0_BASE)
        init_db(engine, get_metadata())
        order, policy, facts, dec = build_ord1042_bundle(clock)
        pid = seed_ord1042_proposal(
            engine,
            clock=clock,
            policy=policy,
            order=order,
            facts=facts,
            dec=dec,
            customer_id="C001",
            request_id="req-trans-check",
            session_id="ses-trans",
        )
        v2 = transition_fn(
            engine,
            entity_type="proposals",
            entity_id=pid,
            to_status=ProposalStatus.confirmed,
            expected_version=1,
            clock=clock,
            request_id="req-trans-check",
            actor_id="C001",
        )
        assert v2 == 2
        with pytest.raises(InvalidTransition):
            transition_fn(
                engine,
                entity_type="proposals",
                entity_id=pid,
                to_status=ProposalStatus.cancelled,
                expected_version=2,
                clock=clock,
                request_id="req-trans-check",
                actor_id="C001",
            )
        plan = make_saga_plan(
            pid, "C001", dec.facts_hash, session_id="ses-trans", request_id="req-trans-check"
        )
        prepare_run_sync(engine, plan, clock=clock, request_id="req-trans-check")
        aid = finalize_approval_sync(
            engine,
            plan=plan,
            return_id="RET-TRANS",
            refund_id="RFD-TRANS",
            clock=clock,
            request_id="req-trans-check",
            actor_type=ActorType.CUSTOMER,
            actor_id="C001",
        )
        av2 = transition_fn(
            engine,
            entity_type="approvals",
            entity_id=aid,
            to_status=ApprovalStatus.approving,
            expected_version=1,
            clock=clock,
            request_id="req-trans-check",
            actor_id="seller-1",
            actor_type="seller",
        )
        assert av2 == 2
    finally:
        engine.dispose()


def test_t19_concurrent_saga_writes_8_threads_valid_chain(tmp_path: Path) -> None:
    """T19: 8 concurrent threads executing full saga steps keep hash chain intact."""
    db_file = tmp_path / "t19_concurrent_saga.db"
    engine = create_engine_from_url(f"sqlite:///{db_file.as_posix()}")
    clock = FixedClock(T0_BASE)
    try:
        init_db(engine, get_metadata())
        base_order, policy, _, base_dec = build_ord1042_bundle(clock)

        def worker(idx: int) -> str:
            cust_id, req_id, sess_id = f"C-T19-{idx}", f"req-t19-{idx}", f"ses-t19-{idx}"
            ord_obj = base_order.model_copy(update={"customer_id": cust_id})
            w_facts = build_facts(clock, ord_obj, cust_id)
            pid = seed_ord1042_proposal(
                engine, clock=clock, policy=policy, order=ord_obj, facts=w_facts, dec=base_dec,
                customer_id=cust_id, request_id=req_id, session_id=sess_id, reason_text="defective",
            )
            plan = make_saga_plan(
                pid, cust_id, base_dec.facts_hash, session_id=sess_id, request_id=req_id
            )
            assert prepare_run_sync(engine, plan, clock=clock, request_id=req_id) is None
            exec_id, _ = start_execution_step_sync(
                engine, proposal_id=pid, step="create_return",
                idempotency_key=f"{pid}:create_return", actor_type=ActorType.CUSTOMER,
                actor_id=cust_id, policy_version="v3", session_id=sess_id,
                request_id=req_id, clock=clock,
            )
            ret_id, rfd_id = f"RET-T19-{idx}", f"RFD-T19-{idx}"
            finish_execution_step_sync(
                engine, proposal_id=pid, execution_id=exec_id, step="create_return",
                new_status="succeeded", store_ref=ret_id, error_code=None,
                actor_type=ActorType.CUSTOMER, actor_id=cust_id, policy_version="v3",
                session_id=sess_id, request_id=req_id, clock=clock,
            )
            aid = finalize_approval_sync(
                engine, plan=plan, return_id=ret_id, refund_id=rfd_id, clock=clock,
                request_id=req_id, actor_type=ActorType.CUSTOMER, actor_id=cust_id,
            )
            v2 = claim_approving_sync(
                engine, approval_id=aid, expected_version=1, actor_id="seller-1",
                clock=clock, request_id=req_id,
            )
            finalize_approved_sync(
                engine, approval_id=aid, approving_version=v2, proposal_id=pid,
                order_id="ORD-1042", customer_id=cust_id, return_id=ret_id, refund_id=rfd_id,
                amount_paise=120000, store_auth_ref=f"AUTH-{idx}", reason="ok",
                actor_id="seller-1", policy_version="v3", session_id=sess_id,
                clock=clock, request_id=req_id,
            )
            return pid

        with ThreadPoolExecutor(max_workers=8) as pool:
            pids = [f.result() for f in as_completed([pool.submit(worker, i) for i in range(8)])]
        assert len(pids) == 8

        with read_tx(engine) as conn:
            prop_ok = conn.execute(
                text("SELECT count(*) FROM proposals WHERE status='succeeded'")
            ).scalar()
            apr_ok = conn.execute(
                text("SELECT count(*) FROM approvals WHERE status='approved'")
            ).scalar()
            exec_ok = conn.execute(
                text("SELECT count(*) FROM executions WHERE status='succeeded'")
            ).scalar()
            obx_stub_ok = conn.execute(
                text("SELECT count(*) FROM notifications_outbox WHERE channel='seller_stub'")
            ).scalar()
            obx_email_ok = conn.execute(
                text(
                    "SELECT count(*) FROM notifications_outbox "
                    "WHERE channel='email' AND template='approval.decided'"
                )
            ).scalar()
            assert (
                prop_ok == 8
                and apr_ok == 8
                and exec_ok == 8
                and obx_stub_ok == 8
                and obx_email_ok == 8
            )
            prev_hashes = conn.execute(select(audit_events.c.prev_hash)).scalars().all()
            total = len(prev_hashes)
            assert total >= 56 and len(set(prev_hashes)) == total
            ev_types = set(conn.execute(select(audit_events.c.event_type)).scalars().all())
            for needed in (
                "policy_decision",
                "proposal_created",
                "execution_step",
                "approval_created",
                "notification_stub",
                "approval_decided",
            ):
                assert needed in ev_types
            status = AuditService.verify_chain(conn)
            assert status.ok is True and status.checked == total and status.first_broken_seq is None
    finally:
        engine.dispose()


def test_t20_audit_failure_on_saga_step_leaves_business_and_exec_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T20: Audit failure during saga step keeps business row unchanged & execution 'executing'."""
    db_file = tmp_path / "t20_saga_step.db"
    engine = create_engine_from_url(f"sqlite:///{db_file.as_posix()}")
    clock = FixedClock(T0_BASE)
    try:
        init_db(engine, get_metadata())
        order, policy, facts, dec = build_ord1042_bundle(clock)
        pid = seed_ord1042_proposal(
            engine,
            clock=clock,
            policy=policy,
            order=order,
            facts=facts,
            dec=dec,
            customer_id="C001",
            request_id="req-t20-s1",
            session_id="ses-t20",
        )
        plan = make_saga_plan(
            pid, "C001", dec.facts_hash, session_id="ses-t20", request_id="req-t20-s1"
        )
        prepare_run_sync(engine, plan, clock=clock, request_id="req-t20-s1")
        exec_id, _ = start_execution_step_sync(
            engine,
            proposal_id=pid,
            step="create_return",
            idempotency_key=f"{pid}:create_return",
            actor_type=ActorType.CUSTOMER,
            actor_id="C001",
            policy_version="v3",
            session_id="ses-t20",
            request_id="req-t20-s1",
            clock=clock,
        )

        def _boom(_cls: Any, _tx: Any, _event: AuditEvent) -> int:
            raise RuntimeError("injected_audit_failure")

        monkeypatch.setattr(AuditService, "append", classmethod(_boom))

        with pytest.raises(RuntimeError, match="injected_audit_failure"):
            finish_execution_step_sync(
                engine,
                proposal_id=pid,
                execution_id=exec_id,
                step="create_return",
                new_status="succeeded",
                store_ref="RET-999",
                error_code=None,
                actor_type=ActorType.CUSTOMER,
                actor_id="C001",
                policy_version="v3",
                session_id="ses-t20",
                request_id="req-t20-s1",
                clock=clock,
                outbox_stub={"dedupe_key": "k1", "template": "t", "payload": {"x": 1}},
            )
        with pytest.raises(RuntimeError, match="injected_audit_failure"):
            finalize_approval_sync(
                engine,
                plan=plan,
                return_id="RET-999",
                refund_id="RFD-999",
                clock=clock,
                request_id="req-t20-s1",
                actor_type=ActorType.CUSTOMER,
                actor_id="C001",
            )

        with read_tx(engine) as conn:
            erow = conn.execute(
                select(executions).where(executions.c.execution_id == exec_id)
            ).fetchone()
            assert erow is not None and erow.status == "executing" and erow.store_ref is None
            prow = conn.execute(
                select(proposals).where(proposals.c.proposal_id == pid)
            ).fetchone()
            assert prow is not None and prow.status == ProposalStatus.executing.value
            assert conn.execute(select(approvals)).fetchall() == []
            assert conn.execute(select(notifications_outbox)).fetchall() == []
            assert conn.execute(select(client_events)).fetchall() == []
    finally:
        engine.dispose()
