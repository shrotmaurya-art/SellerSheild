"""Step-level reconciliation logic for executing and unknown saga rows."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Engine

from sellershield_agent.audit.models import ActorType
from sellershield_agent.clock import Clock
from sellershield_agent.domain.models import Order, RefundStatus, ReturnStatus
from sellershield_agent.execution.common import (
    RECONCILE_DELAYS_SECONDS,
    SagaPlan,
    classify_store_error,
    format_utc_iso,
    parse_utc,
    step_idempotency_key,
)
from sellershield_agent.execution.db_ops import finish_execution_step_sync
from sellershield_agent.execution.release import prepare_release_reconcile
from sellershield_agent.execution.release_db import sync_approval_refund_status_sync
from sellershield_agent.store import StoreClient
from sellershield_agent.store.client import ServiceContext, StoreClientError

_logger = structlog.get_logger("sellershield_agent.execution.reconcile")


def compute_next_retry(
    *,
    started_at_str: str,
    prior_attempts: int,
    step_status: str,
    had_next_retry: bool,
    now: datetime,
) -> tuple[bool, int, str | None]:
    """Compute (is_stuck, new_attempts, next_retry_at_iso) for 5s -> 30s -> 2m -> stuck."""
    if step_status == "executing" and prior_attempts <= 1 and not had_next_retry:
        delay_idx = 0
        new_attempts = 1
    else:
        delay_idx = prior_attempts
        new_attempts = prior_attempts + 1

    if delay_idx >= len(RECONCILE_DELAYS_SECONDS):
        return True, new_attempts, None

    started_dt = parse_utc(started_at_str)
    delay_s = RECONCILE_DELAYS_SECONDS[delay_idx]
    candidate = started_dt + timedelta(seconds=delay_s)
    if candidate > now:
        next_dt = candidate
    else:
        prev_s = RECONCILE_DELAYS_SECONDS[delay_idx - 1] if delay_idx > 0 else 0
        next_dt = now + timedelta(seconds=max(5, delay_s - prev_s))
    return False, new_attempts, format_utc_iso(next_dt)


def _find_return_id(by_step: dict[str, dict[str, Any]], order: Order, pid: str) -> str | None:
    cr = by_step.get("create_return")
    if cr and cr.get("store_ref"):
        return str(cr["store_ref"])
    cr_key = step_idempotency_key(pid, "create_return")
    for ret in order.returns:
        if ret.client_ref == cr_key:
            return ret.return_id
    return None


def _find_refund_id(by_step: dict[str, dict[str, Any]], order: Order, pid: str) -> str | None:
    rr = by_step.get("reserve_refund")
    if rr and rr.get("store_ref"):
        return str(rr["store_ref"])
    rr_key = step_idempotency_key(pid, "reserve_refund")
    for rfd in order.refunds:
        if rfd.client_ref == rr_key:
            return rfd.refund_id
    return None


async def record_transient(
    engine: Engine,
    plan: SagaPlan,
    step_row: dict[str, Any],
    *,
    err_code: str,
    clock: Clock,
) -> str:
    """Advance retry backoff or mark the step stuck and alert seller outbox."""
    exec_id = str(step_row["execution_id"])
    step = str(step_row["step"])
    is_stuck, new_attempts, next_retry_iso = compute_next_retry(
        started_at_str=str(step_row["started_at"]),
        prior_attempts=int(step_row.get("attempts") or 1),
        step_status=str(step_row["status"]),
        had_next_retry=step_row.get("next_retry_at") is not None,
        now=clock.now(),
    )
    outbox_stub: dict[str, Any] | None = None
    if is_stuck:
        outbox_stub = {
            "dedupe_key": f"execution_stuck:{exec_id}",
            "template": "seller.execution_stuck",
            "payload": {
                "execution_id": exec_id,
                "proposal_id": plan.proposal_id,
                "order_id": plan.order_id,
                "step": step,
                "error_code": err_code,
                "attempts": new_attempts,
            },
            "customer_id": None,
        }
    await asyncio.to_thread(
        finish_execution_step_sync,
        engine,
        proposal_id=plan.proposal_id,
        execution_id=exec_id,
        step=step,
        new_status="unknown",
        store_ref=None,
        error_code=err_code,
        actor_type=ActorType.SYSTEM,
        actor_id="reconciler",
        policy_version=plan.policy_version,
        session_id=plan.session_id,
        request_id=plan.request_id,
        clock=clock,
        next_retry_at=next_retry_iso,
        attempts=new_attempts,
        stuck=is_stuck,
        reconcile_action="reconcile_step_stuck" if is_stuck else "reconcile_step_retry_scheduled",
        outbox_stub=outbox_stub,
    )
    _logger.warning(
        "reconcile_step_stuck" if is_stuck else "reconcile_step_deferred",
        proposal_id=plan.proposal_id,
        execution_id=exec_id,
        step=step,
        error_code=err_code,
        attempts=new_attempts,
    )
    return "stuck" if is_stuck else "unknown"


async def record_terminal_fail(
    engine: Engine,
    plan: SagaPlan,
    step_row: dict[str, Any],
    *,
    err_code: str,
    clock: Clock,
) -> str:
    """Mark an execution step failed during reconciliation, alerting if compensation."""
    exec_id = str(step_row["execution_id"])
    step = str(step_row["step"])
    outbox_stub: dict[str, Any] | None = None
    if step in ("cancel_reservation", "cancel_return"):
        outbox_stub = {
            "dedupe_key": f"compensation_failed:{exec_id}",
            "template": "seller.compensation_failed",
            "payload": {
                "execution_id": exec_id,
                "proposal_id": plan.proposal_id,
                "order_id": plan.order_id,
                "step": step,
                "error_code": err_code,
            },
            "customer_id": None,
        }
    await asyncio.to_thread(
        finish_execution_step_sync,
        engine,
        proposal_id=plan.proposal_id,
        execution_id=exec_id,
        step=step,
        new_status="failed",
        store_ref=None,
        error_code=err_code,
        actor_type=ActorType.SYSTEM,
        actor_id="reconciler",
        policy_version=plan.policy_version,
        session_id=plan.session_id,
        request_id=plan.request_id,
        clock=clock,
        reconcile_action="reconcile_step_failed",
        outbox_stub=outbox_stub,
    )
    return "failed"


async def record_reconciled_success(
    engine: Engine,
    plan: SagaPlan,
    step_row: dict[str, Any],
    *,
    store_ref: str,
    clock: Clock,
) -> str:
    """Persist reconciled step success with store reference."""
    await asyncio.to_thread(
        finish_execution_step_sync,
        engine,
        proposal_id=plan.proposal_id,
        execution_id=str(step_row["execution_id"]),
        step=str(step_row["step"]),
        new_status="succeeded",
        store_ref=store_ref,
        error_code=None,
        actor_type=ActorType.SYSTEM,
        actor_id="reconciler",
        policy_version=plan.policy_version,
        session_id=plan.session_id,
        request_id=plan.request_id,
        clock=clock,
        reconcile_action="reconcile_step_succeeded",
    )
    return "succeeded"


async def _handle_step_exc(
    engine: Engine,
    plan: SagaPlan,
    step_row: dict[str, Any],
    exc: BaseException,
    clock: Clock,
) -> str:
    is_transient, err_code = classify_store_error(exc)
    if is_transient:
        return await record_transient(engine, plan, step_row, err_code=err_code, clock=clock)
    return await record_terminal_fail(engine, plan, step_row, err_code=err_code, clock=clock)


async def reconcile_step(
    *,
    engine: Engine,
    store: StoreClient,
    plan: SagaPlan,
    step_row: dict[str, Any],
    by_step: dict[str, dict[str, Any]],
    order: Order,
    svc_ctx: ServiceContext,
    clock: Clock,
) -> str:
    """Check store admin truth by client_ref and retry idempotently with ServiceContext."""
    step = str(step_row["step"])
    idem_key = str(step_row["idempotency_key"])
    pid = plan.proposal_id

    if step == "create_return":
        matched = next((r for r in order.returns if r.client_ref == idem_key), None)
        if matched is not None:
            return await record_reconciled_success(
                engine, plan, step_row, store_ref=matched.return_id, clock=clock
            )
        err = str(step_row.get("error_code") or "not_committed")
        return await record_terminal_fail(engine, plan, step_row, err_code=err, clock=clock)

    if step == "transition_return":
        ret_id = _find_return_id(by_step, order, pid)
        if not ret_id:
            return await record_terminal_fail(
                engine, plan, step_row, err_code="missing_return", clock=clock
            )
        matched_ret = next((r for r in order.returns if r.return_id == ret_id), None)
        if matched_ret is not None and matched_ret.status == ReturnStatus.approved:
            return await record_reconciled_success(
                engine, plan, step_row, store_ref=matched_ret.return_id, clock=clock
            )
        try:
            ret = await store.transition_return(
                svc_ctx, ret_id, "approved", plan.reason_text or "Policy auto-approved", idem_key
            )
            return await record_reconciled_success(
                engine, plan, step_row, store_ref=ret.return_id, clock=clock
            )
        except (TimeoutError, StoreClientError) as exc:
            return await _handle_step_exc(engine, plan, step_row, exc, clock)

    if step == "reserve_refund":
        matched_rfd = next(
            (
                r
                for r in order.refunds
                if r.client_ref == idem_key
                and r.status
                in (
                    RefundStatus.reserved,
                    RefundStatus.authorized,
                    RefundStatus.processing,
                    RefundStatus.completed,
                )
            ),
            None,
        )
        if matched_rfd is not None:
            return await record_reconciled_success(
                engine, plan, step_row, store_ref=matched_rfd.refund_id, clock=clock
            )
        ret_arg = _find_return_id(by_step, order, pid) if plan.proposal_type == "return" else None
        try:
            rfd = await store.reserve_refund(
                svc_ctx, plan.order_id, plan.amount_paise, ret_arg, plan.refund_method, idem_key
            )
            return await record_reconciled_success(
                engine, plan, step_row, store_ref=rfd.refund_id, clock=clock
            )
        except (TimeoutError, StoreClientError) as exc:
            return await _handle_step_exc(engine, plan, step_row, exc, clock)

    if step == "cancel_reservation":
        refund_id = _find_refund_id(by_step, order, pid)
        if not refund_id:
            return await record_terminal_fail(
                engine, plan, step_row, err_code="missing_refund", clock=clock
            )
        matched_rfd = next((r for r in order.refunds if r.refund_id == refund_id), None)
        if matched_rfd is not None and matched_rfd.status == RefundStatus.released:
            return await record_reconciled_success(
                engine, plan, step_row, store_ref=matched_rfd.refund_id, clock=clock
            )
        try:
            rfd = await store.cancel_reservation(svc_ctx, refund_id, idem_key)
            return await record_reconciled_success(
                engine, plan, step_row, store_ref=rfd.refund_id, clock=clock
            )
        except (TimeoutError, StoreClientError) as exc:
            return await _handle_step_exc(engine, plan, step_row, exc, clock)

    if step == "cancel_return":
        ret_id = _find_return_id(by_step, order, pid)
        if not ret_id:
            return await record_terminal_fail(
                engine, plan, step_row, err_code="missing_return", clock=clock
            )
        matched_ret = next((r for r in order.returns if r.return_id == ret_id), None)
        if matched_ret is not None and matched_ret.status == ReturnStatus.cancelled:
            return await record_reconciled_success(
                engine, plan, step_row, store_ref=matched_ret.return_id, clock=clock
            )
        try:
            ret = await store.transition_return(
                svc_ctx, ret_id, "cancelled", "Compensating failed proposal execution", idem_key
            )
            return await record_reconciled_success(
                engine, plan, step_row, store_ref=ret.return_id, clock=clock
            )
        except (TimeoutError, StoreClientError) as exc:
            return await _handle_step_exc(engine, plan, step_row, exc, clock)

    if step == "release_refund":
        refund_id = _find_refund_id(by_step, order, pid)
        if not refund_id:
            return await record_terminal_fail(
                engine, plan, step_row, err_code="missing_refund", clock=clock
            )
        prep = await prepare_release_reconcile(
            engine=engine,
            store=store,
            plan=plan,
            refund_id=refund_id,
            order=order,
            svc_ctx=svc_ctx,
            clock=clock,
        )
        if prep.status == "succeeded" and prep.store_ref is not None:
            return await record_reconciled_success(
                engine, plan, step_row, store_ref=prep.store_ref, clock=clock
            )
        if prep.status == "failed":
            return await record_terminal_fail(
                engine,
                plan,
                step_row,
                err_code=prep.error_code or "release_failed",
                clock=clock,
            )
        if prep.status == "exc" and prep.exc is not None:
            return await _handle_step_exc(engine, plan, step_row, prep.exc, clock)
        try:
            rfd = await store.release_refund(svc_ctx, refund_id, idem_key)
            res = await record_reconciled_success(
                engine, plan, step_row, store_ref=rfd.refund_id, clock=clock
            )
            await asyncio.to_thread(
                sync_approval_refund_status_sync,
                engine,
                proposal_id=pid,
                refund_status=rfd.status.value,
            )
            return res
        except (TimeoutError, StoreClientError) as exc:
            return await _handle_step_exc(engine, plan, step_row, exc, clock)

    return await record_terminal_fail(engine, plan, step_row, err_code="unknown_step", clock=clock)
