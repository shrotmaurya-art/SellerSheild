"""Two-phase step runner and reverse-order compensation for the execution saga."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import Engine

from sellershield_agent.audit.models import ActorType
from sellershield_agent.clock import Clock
from sellershield_agent.execution.common import (
    RECONCILE_DELAYS_SECONDS,
    SagaPlan,
    classify_store_error,
    format_utc_iso,
    parse_utc,
    step_idempotency_key,
)
from sellershield_agent.execution.db_ops import (
    fetch_proposal_exec_state_sync,
    finish_execution_step_sync,
    start_execution_step_sync,
)
from sellershield_agent.store.client import ServiceContext, StoreClient, StoreClientError

_logger = structlog.get_logger("sellershield_agent.execution.steps")


async def execute_single_step(
    *,
    engine: Engine,
    plan: SagaPlan,
    step: str,
    actor_type: ActorType | str,
    actor_id: str,
    clock: Clock,
    store_call: Callable[[], Awaitable[str]],
    compensation_outbox_on_fail: bool = False,
) -> tuple[bool, str | None, str | None]:
    """Run one 2-phase step: tx(executing+audit) -> store(no tx) -> tx(result+audit)."""
    idem_key = step_idempotency_key(plan.proposal_id, step)
    exec_id, started_at = await asyncio.to_thread(
        start_execution_step_sync,
        engine,
        proposal_id=plan.proposal_id,
        step=step,
        idempotency_key=idem_key,
        actor_type=actor_type,
        actor_id=actor_id,
        policy_version=plan.policy_version,
        session_id=plan.session_id,
        request_id=plan.request_id,
        clock=clock,
    )

    try:
        store_ref = await store_call()
    except (TimeoutError, StoreClientError) as exc:
        is_transient, err_code = classify_store_error(exc)
        if is_transient:
            next_retry = format_utc_iso(
                parse_utc(started_at) + timedelta(seconds=RECONCILE_DELAYS_SECONDS[0])
            )
            await asyncio.to_thread(
                finish_execution_step_sync,
                engine,
                proposal_id=plan.proposal_id,
                execution_id=exec_id,
                step=step,
                new_status="unknown",
                store_ref=None,
                error_code=err_code,
                actor_type=actor_type,
                actor_id=actor_id,
                policy_version=plan.policy_version,
                session_id=plan.session_id,
                request_id=plan.request_id,
                clock=clock,
                next_retry_at=next_retry,
                attempts=1,
            )
            _logger.info(
                "execution_step_unknown",
                proposal_id=plan.proposal_id,
                execution_id=exec_id,
                step=step,
                error_code=err_code,
                request_id=plan.request_id,
            )
            return False, None, err_code

        outbox_stub: dict[str, Any] | None = None
        if compensation_outbox_on_fail:
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
            actor_type=actor_type,
            actor_id=actor_id,
            policy_version=plan.policy_version,
            session_id=plan.session_id,
            request_id=plan.request_id,
            clock=clock,
            outbox_stub=outbox_stub,
        )
        _logger.warning(
            "execution_step_failed",
            proposal_id=plan.proposal_id,
            execution_id=exec_id,
            step=step,
            error_code=err_code,
            request_id=plan.request_id,
        )
        return False, None, err_code

    await asyncio.to_thread(
        finish_execution_step_sync,
        engine,
        proposal_id=plan.proposal_id,
        execution_id=exec_id,
        step=step,
        new_status="succeeded",
        store_ref=store_ref,
        error_code=None,
        actor_type=actor_type,
        actor_id=actor_id,
        policy_version=plan.policy_version,
        session_id=plan.session_id,
        request_id=plan.request_id,
        clock=clock,
    )
    return True, store_ref, None


async def compensate_proposal(
    *,
    engine: Engine,
    store: StoreClient,
    plan: SagaPlan,
    clock: Clock,
    service_ctx: ServiceContext,
    actor_id: str = "executor",
) -> bool:
    """Reverse-order service-token compensation: cancel_reservation, then cancel_return."""
    by_step, _, _ = await asyncio.to_thread(
        fetch_proposal_exec_state_sync, engine, plan.proposal_id
    )
    all_ok = True

    reserve_row = by_step.get("reserve_refund")
    cancel_rfd_row = by_step.get("cancel_reservation")
    if (
        reserve_row is not None
        and reserve_row.get("status") == "succeeded"
        and reserve_row.get("store_ref")
        and (cancel_rfd_row is None or cancel_rfd_row.get("status") not in ("succeeded", "failed"))
    ):
        refund_id = str(reserve_row["store_ref"])
        idem_key = step_idempotency_key(plan.proposal_id, "cancel_reservation")

        async def _cancel_res() -> str:
            rfd = await store.cancel_reservation(service_ctx, refund_id, idem_key)
            return rfd.refund_id

        ok, _, _ = await execute_single_step(
            engine=engine,
            plan=plan,
            step="cancel_reservation",
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            clock=clock,
            store_call=_cancel_res,
            compensation_outbox_on_fail=True,
        )
        if not ok:
            all_ok = False

    create_row = by_step.get("create_return")
    cancel_ret_row = by_step.get("cancel_return")
    if (
        create_row is not None
        and create_row.get("status") == "succeeded"
        and create_row.get("store_ref")
        and (cancel_ret_row is None or cancel_ret_row.get("status") not in ("succeeded", "failed"))
    ):
        return_id = str(create_row["store_ref"])
        idem_key = step_idempotency_key(plan.proposal_id, "cancel_return")

        async def _cancel_ret() -> str:
            ret = await store.transition_return(
                service_ctx,
                return_id,
                "cancelled",
                "Compensating failed proposal execution",
                idem_key,
            )
            return ret.return_id

        ok, _, _ = await execute_single_step(
            engine=engine,
            plan=plan,
            step="cancel_return",
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            clock=clock,
            store_call=_cancel_ret,
            compensation_outbox_on_fail=True,
        )
        if not ok:
            all_ok = False

    return all_ok

