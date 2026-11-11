"""Durable Saga Executor orchestrating confirmed proposals across Agent DB and Store."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import Engine

from sellershield_agent.audit.models import ActorType
from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.db import get_engine
from sellershield_agent.domain.state import ProposalStatus
from sellershield_agent.execution.common import (
    ACTIVE_RETURN_STATUSES,
    SagaPlan,
    build_service_ctx,
    format_execution_status_message,
    plan_from_prepared,
    step_idempotency_key,
    to_return_items,
)
from sellershield_agent.execution.db_ops import fetch_proposal_exec_state_sync, prepare_run_sync
from sellershield_agent.execution.finalize_db import finalize_approval_sync, finalize_proposal_sync
from sellershield_agent.execution.steps import compensate_proposal, execute_single_step
from sellershield_agent.proposals.executor import ExecutionResult
from sellershield_agent.proposals.integrity import PreparedProposal
from sellershield_agent.store.client import RequestContext, StoreClient, StoreClientError


async def _lookup_duplicate_return_id(
    store: StoreClient, plan: SagaPlan, clock: Clock
) -> str | None:
    svc_ctx = build_service_ctx(clock, plan.request_id)
    try:
        order = await store.get_order_admin(svc_ctx, plan.order_id)
    except (TimeoutError, StoreClientError):
        return None
    target_items = {str(it["item_id"]) for it in plan.items}
    for ret in order.returns:
        if ret.status in ACTIVE_RETURN_STATUSES and any(
            ri.item_id in target_items for ri in ret.items
        ):
            return ret.return_id
    for ret in order.returns:
        if ret.status in ACTIVE_RETURN_STATUSES:
            return ret.return_id
    return None


async def _finish_step_error(
    *,
    engine: Engine,
    store: StoreClient,
    plan: SagaPlan,
    clock: Clock,
    actor_id: str,
    err_code: str | None,
    step: str,
    update_proposal_status: bool,
    update_idempotency: bool,
) -> ExecutionResult:
    if err_code in ("timeout", "unavailable"):
        res_u = ExecutionResult(status="unknown", message_key="proposal.unknown")
        await asyncio.to_thread(
            finalize_proposal_sync,
            engine,
            plan=plan,
            result=res_u,
            clock=clock,
            request_id=plan.request_id,
            actor_id=actor_id,
            update_proposal_status=update_proposal_status,
            update_idempotency=update_idempotency,
        )
        return res_u

    dup_ret_id = (
        await _lookup_duplicate_return_id(store, plan, clock)
        if step == "create_return" and err_code == "duplicate"
        else None
    )
    await compensate_proposal(
        engine=engine,
        store=store,
        plan=plan,
        clock=clock,
        service_ctx=build_service_ctx(clock, plan.request_id),
        actor_id=actor_id,
    )
    msg_key = (
        "proposal.failed_duplicate"
        if step == "create_return" and err_code == "duplicate"
        else "proposal.failed"
    )
    res_f = ExecutionResult(status="failed", return_id=dup_ret_id, message_key=msg_key)
    await asyncio.to_thread(
        finalize_proposal_sync,
        engine,
        plan=plan,
        result=res_f,
        clock=clock,
        request_id=plan.request_id,
        actor_id=actor_id,
        update_proposal_status=update_proposal_status,
        update_idempotency=update_idempotency,
    )
    return res_f


async def run_forward_saga(
    *,
    engine: Engine,
    store: StoreClient,
    plan: SagaPlan,
    clock: Clock,
    customer_ctx: RequestContext | None,
    by_step: dict[str, dict[str, Any]],
    update_proposal_status: bool,
    update_idempotency: bool,
    from_reconcile: bool,
) -> ExecutionResult:
    """Execute forward steps from existing state, compensating on terminal errors."""
    actor_id = "reconciler" if from_reconcile else "executor"
    return_id: str | None = None

    if plan.proposal_type == "cancel":
        if plan.decision == "needs_approval":
            approval_id = await asyncio.to_thread(
                finalize_approval_sync,
                engine,
                plan=plan,
                return_id=None,
                refund_id=None,
                clock=clock,
                request_id=plan.request_id,
                actor_type=ActorType.SYSTEM,
                actor_id=actor_id,
                update_unknown_to_executing=from_reconcile,
            )
            res_pa = ExecutionResult(
                status="pending_approval",
                return_id=None,
                approval_id=approval_id,
                refund_id=None,
                message_key="proposal.pending_approval",
            )
            if update_idempotency:
                await asyncio.to_thread(
                    finalize_proposal_sync,
                    engine,
                    plan=plan,
                    result=res_pa,
                    clock=clock,
                    request_id=plan.request_id,
                    actor_id=actor_id,
                    emit_client_event=False,
                    update_proposal_status=False,
                    update_idempotency=True,
                )
            return res_pa

        co_row = by_step.get("cancel_order")
        if co_row is None or co_row.get("status") != "succeeded":
            svc_ctx = build_service_ctx(clock, plan.request_id)
            co_key = step_idempotency_key(plan.proposal_id, "cancel_order")

            async def _cancel_ord() -> str:
                cancelled = await store.cancel_order(
                    svc_ctx,
                    plan.order_id,
                    plan.reason_text or "Customer requested pre-dispatch cancellation",
                    co_key,
                )
                return cancelled.order_id

            ok, _, err_code = await execute_single_step(
                engine=engine,
                plan=plan,
                step="cancel_order",
                actor_type=ActorType.SYSTEM,
                actor_id=actor_id,
                clock=clock,
                store_call=_cancel_ord,
            )
            if not ok:
                return await _finish_step_error(
                    engine=engine,
                    store=store,
                    plan=plan,
                    clock=clock,
                    actor_id=actor_id,
                    err_code=err_code,
                    step="cancel_order",
                    update_proposal_status=update_proposal_status,
                    update_idempotency=update_idempotency,
                )

        res_cancel_ok = ExecutionResult(
            status="succeeded",
            return_id=None,
            approval_id=None,
            refund_id=None,
            message_key="proposal.succeeded",
        )
        await asyncio.to_thread(
            finalize_proposal_sync,
            engine,
            plan=plan,
            result=res_cancel_ok,
            clock=clock,
            request_id=plan.request_id,
            actor_id=actor_id,
            emit_client_event=True,
            update_proposal_status=update_proposal_status,
            update_idempotency=update_idempotency,
        )
        return res_cancel_ok

    if plan.proposal_type == "return":
        cr_row = by_step.get("create_return")
        if cr_row is not None and cr_row.get("status") == "succeeded" and cr_row.get("store_ref"):
            return_id = str(cr_row["store_ref"])
        elif customer_ctx is None:
            return await _finish_step_error(
                engine=engine,
                store=store,
                plan=plan,
                clock=clock,
                actor_id=actor_id,
                err_code="missing_context",
                step="create_return",
                update_proposal_status=update_proposal_status,
                update_idempotency=update_idempotency,
            )
        else:
            norm_items = to_return_items(plan.items)
            cr_key = step_idempotency_key(plan.proposal_id, "create_return")

            async def _create_ret() -> str:
                ret = await store.create_return(
                    customer_ctx,
                    plan.order_id,
                    norm_items,
                    plan.reason_code,
                    plan.reason_text,
                    plan.resolution,
                    cr_key,
                )
                return ret.return_id

            ok, ref, err_code = await execute_single_step(
                engine=engine,
                plan=plan,
                step="create_return",
                actor_type=ActorType.CUSTOMER,
                actor_id=plan.customer_id,
                clock=clock,
                store_call=_create_ret,
            )
            if not ok:
                return await _finish_step_error(
                    engine=engine,
                    store=store,
                    plan=plan,
                    clock=clock,
                    actor_id=actor_id,
                    err_code=err_code,
                    step="create_return",
                    update_proposal_status=update_proposal_status,
                    update_idempotency=update_idempotency,
                )
            return_id = ref

        if plan.decision == "allow":
            tr_row = by_step.get("transition_return")
            if tr_row is None or tr_row.get("status") != "succeeded":
                svc_ctx = build_service_ctx(clock, plan.request_id)
                tr_key = step_idempotency_key(plan.proposal_id, "transition_return")
                assert return_id is not None

                async def _trans_ret() -> str:
                    ret = await store.transition_return(
                        svc_ctx,
                        return_id,
                        "approved",
                        plan.reason_text or "Policy auto-approved",
                        tr_key,
                    )
                    return ret.return_id

                ok, _, err_code = await execute_single_step(
                    engine=engine,
                    plan=plan,
                    step="transition_return",
                    actor_type=ActorType.SYSTEM,
                    actor_id=actor_id,
                    clock=clock,
                    store_call=_trans_ret,
                )
                if not ok:
                    return await _finish_step_error(
                        engine=engine,
                        store=store,
                        plan=plan,
                        clock=clock,
                        actor_id=actor_id,
                        err_code=err_code,
                        step="transition_return",
                        update_proposal_status=update_proposal_status,
                        update_idempotency=update_idempotency,
                    )

    rr_row = by_step.get("reserve_refund")
    refund_id: str | None = None
    if rr_row is not None and rr_row.get("status") == "succeeded" and rr_row.get("store_ref"):
        refund_id = str(rr_row["store_ref"])
    else:
        svc_ctx = build_service_ctx(clock, plan.request_id)
        rr_key = step_idempotency_key(plan.proposal_id, "reserve_refund")
        ret_arg = return_id if plan.proposal_type == "return" else None

        async def _reserve_rfd() -> str:
            rfd = await store.reserve_refund(
                svc_ctx,
                plan.order_id,
                plan.amount_paise,
                ret_arg,
                plan.refund_method,
                rr_key,
            )
            return rfd.refund_id

        ok, ref, err_code = await execute_single_step(
            engine=engine,
            plan=plan,
            step="reserve_refund",
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            clock=clock,
            store_call=_reserve_rfd,
        )
        if not ok:
            return await _finish_step_error(
                engine=engine,
                store=store,
                plan=plan,
                clock=clock,
                actor_id=actor_id,
                err_code=err_code,
                step="reserve_refund",
                update_proposal_status=update_proposal_status,
                update_idempotency=update_idempotency,
            )
        refund_id = ref

    if plan.decision == "needs_approval":
        assert refund_id is not None
        ret_out = return_id if plan.proposal_type == "return" else None
        approval_id = await asyncio.to_thread(
            finalize_approval_sync,
            engine,
            plan=plan,
            return_id=ret_out,
            refund_id=refund_id,
            clock=clock,
            request_id=plan.request_id,
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            update_unknown_to_executing=from_reconcile,
        )
        res_pa = ExecutionResult(
            status="pending_approval",
            return_id=ret_out,
            approval_id=approval_id,
            refund_id=refund_id,
            message_key="proposal.pending_approval",
        )
        if update_idempotency:
            await asyncio.to_thread(
                finalize_proposal_sync,
                engine,
                plan=plan,
                result=res_pa,
                clock=clock,
                request_id=plan.request_id,
                actor_id=actor_id,
                emit_client_event=False,
                update_proposal_status=False,
                update_idempotency=True,
            )
        return res_pa

    res_ok = ExecutionResult(
        status="succeeded",
        return_id=return_id if plan.proposal_type == "return" else None,
        approval_id=None,
        refund_id=refund_id,
        message_key="proposal.succeeded",
    )
    await asyncio.to_thread(
        finalize_proposal_sync,
        engine,
        plan=plan,
        result=res_ok,
        clock=clock,
        request_id=plan.request_id,
        actor_id=actor_id,
        emit_client_event=True,
        update_proposal_status=update_proposal_status,
        update_idempotency=update_idempotency,
    )
    return res_ok


class Executor:
    """Two-phase Saga Executor for ALLOW and NEEDS_APPROVAL proposals."""

    def __init__(
        self,
        store_client: StoreClient,
        engine: Engine | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = store_client
        self._engine_override = engine
        self._clock = clock

    def _resolve_engine(self) -> Engine:
        return self._engine_override or get_engine()

    async def execute(
        self,
        customer_ctx: RequestContext,
        proposal: PreparedProposal,
        *,
        clock: Clock,
        idempotency_key: str,
    ) -> ExecutionResult:
        """Execute an already-claimed proposal from confirm_proposal handler."""
        del idempotency_key
        engine = self._resolve_engine()
        plan = plan_from_prepared(proposal, request_id=customer_ctx.request_id)
        by_step, _, _ = await asyncio.to_thread(
            fetch_proposal_exec_state_sync, engine, plan.proposal_id
        )
        return await run_forward_saga(
            engine=engine,
            store=self._store,
            plan=plan,
            clock=clock,
            customer_ctx=customer_ctx,
            by_step=by_step,
            update_proposal_status=False,
            update_idempotency=False,
            from_reconcile=False,
        )

    async def run(
        self,
        customer_ctx: RequestContext,
        proposal: PreparedProposal,
        *,
        clock: Clock | None = None,
        idempotency_key: str | None = None,
    ) -> ExecutionResult:
        """Directly execute a PreparedProposal idempotently, transitioning proposal state."""
        del idempotency_key
        resolved_clock = clock or self._clock or SystemClock()
        engine = self._resolve_engine()
        plan = plan_from_prepared(proposal, request_id=customer_ctx.request_id)
        existing_res = await asyncio.to_thread(
            prepare_run_sync,
            engine,
            plan,
            clock=resolved_clock,
            request_id=customer_ctx.request_id,
        )
        if existing_res is not None:
            return existing_res

        by_step, _, prop_status = await asyncio.to_thread(
            fetch_proposal_exec_state_sync, engine, plan.proposal_id
        )
        if prop_status == ProposalStatus.succeeded.value:
            ret_ref = by_step.get("create_return", {}).get("store_ref")
            rfd_ref = by_step.get("reserve_refund", {}).get("store_ref")
            return ExecutionResult(
                status="succeeded",
                return_id=str(ret_ref) if ret_ref else None,
                refund_id=str(rfd_ref) if rfd_ref else None,
                message_key="proposal.succeeded",
            )
        return await run_forward_saga(
            engine=engine,
            store=self._store,
            plan=plan,
            clock=resolved_clock,
            customer_ctx=customer_ctx,
            by_step=by_step,
            update_proposal_status=True,
            update_idempotency=False,
            from_reconcile=False,
        )


__all__ = [
    "Executor",
    "compensate_proposal",
    "format_execution_status_message",
    "run_forward_saga",
]
