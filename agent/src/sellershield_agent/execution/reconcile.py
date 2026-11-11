"""Idempotent reconciler entrypoint and orphan proposal sweep."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import Engine

from sellershield_agent.approvals.expire import expire_stale_approvals
from sellershield_agent.clock import Clock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine
from sellershield_agent.domain.state import ProposalStatus
from sellershield_agent.execution.common import SagaPlan, build_service_ctx, classify_store_error
from sellershield_agent.execution.db_ops import fetch_proposal_exec_state_sync
from sellershield_agent.execution.finalize_db import finalize_proposal_sync
from sellershield_agent.execution.reconcile_db import scan_reconcile_candidates_sync
from sellershield_agent.execution.reconcile_steps import (
    reconcile_step,
    record_terminal_fail,
    record_transient,
)
from sellershield_agent.execution.saga import run_forward_saga
from sellershield_agent.execution.steps import compensate_proposal
from sellershield_agent.proposals.executor import ExecutionResult
from sellershield_agent.store import StoreClient, get_store_client
from sellershield_agent.store.client import StoreClientError

_STEP_ORDER = {
    "create_return": 0,
    "transition_return": 1,
    "reserve_refund": 2,
    "cancel_reservation": 3,
    "cancel_return": 4,
    "release_refund": 5,
}


async def _finalize_reconciled_proposal(
    *,
    engine: Engine,
    store: StoreClient,
    plan: SagaPlan,
    clock: Clock,
) -> None:
    by_step, _, prop_status = await asyncio.to_thread(
        fetch_proposal_exec_state_sync, engine, plan.proposal_id
    )
    if any(
        bool(r.get("stuck")) or str(r.get("status")) in ("executing", "unknown")
        for r in by_step.values()
    ):
        return

    if "release_refund" in by_step:
        if (
            str(by_step["release_refund"].get("status")) == "failed"
            and prop_status != ProposalStatus.failed.value
        ):
            res_f = ExecutionResult(status="failed", message_key="proposal.failed")
            await asyncio.to_thread(
                finalize_proposal_sync,
                engine,
                plan=plan,
                result=res_f,
                clock=clock,
                request_id=plan.request_id,
                actor_id="reconciler",
                update_proposal_status=True,
                update_idempotency=False,
            )
        return

    any_forward_failed = any(
        str(by_step.get(s, {}).get("status")) == "failed"
        for s in ("create_return", "transition_return", "reserve_refund")
    )
    if any_forward_failed:
        await compensate_proposal(
            engine=engine,
            store=store,
            plan=plan,
            clock=clock,
            service_ctx=build_service_ctx(clock, plan.request_id),
            actor_id="reconciler",
        )
        if prop_status != ProposalStatus.failed.value:
            res_f = ExecutionResult(status="failed", message_key="proposal.failed")
            await asyncio.to_thread(
                finalize_proposal_sync,
                engine,
                plan=plan,
                result=res_f,
                clock=clock,
                request_id=plan.request_id,
                actor_id="reconciler",
                update_proposal_status=True,
                update_idempotency=True,
            )
    elif prop_status != ProposalStatus.failed.value:
        await run_forward_saga(
            engine=engine,
            store=store,
            plan=plan,
            clock=clock,
            customer_ctx=None,
            by_step=by_step,
            update_proposal_status=True,
            update_idempotency=True,
            from_reconcile=True,
        )


async def reconcile_once(
    clock: Clock,
    *,
    store_client: StoreClient | None = None,
    engine: Engine | None = None,
    min_age_seconds: int = 5,
) -> dict[str, int]:
    """Reconcile stalled/unknown steps, crashed proposals, and expired approvals."""
    resolved_engine = engine or get_engine()
    eligible_execs, orphans = await asyncio.to_thread(
        scan_reconcile_candidates_sync,
        resolved_engine,
        now=clock.now(),
        min_age_seconds=min_age_seconds,
    )
    if not eligible_execs and not orphans:
        return await expire_stale_approvals(
            clock=clock,
            store_client=store_client,
            engine=resolved_engine,
        )

    resolved_store = store_client or get_store_client(clock=clock, settings=get_settings())
    scanned = len(eligible_execs) + len(orphans)
    resolved = 0
    stuck = 0

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in eligible_execs:
        grouped.setdefault(str(row["proposal_id"]), []).append(row)

    for pid, rows in grouped.items():
        plan: SagaPlan = rows[0]["plan"]
        for step_row in sorted(rows, key=lambda r: _STEP_ORDER.get(str(r["step"]), 99)):
            by_step, _, _ = await asyncio.to_thread(
                fetch_proposal_exec_state_sync, resolved_engine, pid
            )
            svc_ctx = build_service_ctx(clock, plan.request_id)
            try:
                order = await resolved_store.get_order_admin(svc_ctx, plan.order_id)
            except (TimeoutError, StoreClientError) as exc:
                is_transient, err_code = classify_store_error(exc)
                if is_transient:
                    outcome = await record_transient(
                        resolved_engine, plan, step_row, err_code=err_code, clock=clock
                    )
                    if outcome == "stuck":
                        stuck += 1
                    break
                await record_terminal_fail(
                    resolved_engine, plan, step_row, err_code=err_code, clock=clock
                )
                resolved += 1
                break

            outcome = await reconcile_step(
                engine=resolved_engine,
                store=resolved_store,
                plan=plan,
                step_row=step_row,
                by_step=by_step,
                order=order,
                svc_ctx=svc_ctx,
                clock=clock,
            )
            if outcome == "stuck":
                stuck += 1
                break
            if outcome == "unknown":
                break
            resolved += 1
            if outcome == "failed":
                break

        await _finalize_reconciled_proposal(
            engine=resolved_engine,
            store=resolved_store,
            plan=plan,
            clock=clock,
        )

    for plan, pid_execs in orphans:
        by_step = {str(r["step"]): r for r in pid_execs}
        any_forward_failed = any(
            str(by_step.get(s, {}).get("status")) == "failed"
            for s in ("create_return", "transition_return", "reserve_refund")
        )
        if any_forward_failed:
            await compensate_proposal(
                engine=resolved_engine,
                store=resolved_store,
                plan=plan,
                clock=clock,
                service_ctx=build_service_ctx(clock, plan.request_id),
                actor_id="reconciler",
            )
            res_f = ExecutionResult(status="failed", message_key="proposal.failed")
            await asyncio.to_thread(
                finalize_proposal_sync,
                resolved_engine,
                plan=plan,
                result=res_f,
                clock=clock,
                request_id=plan.request_id,
                actor_id="reconciler",
                update_proposal_status=True,
                update_idempotency=True,
            )
            resolved += 1
        else:
            res = await run_forward_saga(
                engine=resolved_engine,
                store=resolved_store,
                plan=plan,
                clock=clock,
                customer_ctx=None,
                by_step=by_step,
                update_proposal_status=True,
                update_idempotency=True,
                from_reconcile=True,
            )
            if res.status != "unknown":
                resolved += 1

    exp_summary = await expire_stale_approvals(
        clock=clock,
        store_client=resolved_store,
        engine=resolved_engine,
    )
    scanned += exp_summary["scanned"]
    resolved += exp_summary["resolved"]
    stuck += exp_summary["stuck"]
    return {"scanned": scanned, "resolved": resolved, "stuck": stuck}
