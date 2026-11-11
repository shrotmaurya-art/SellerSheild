"""Refund release orchestration and reconciler step preparation for return.inspected_ok."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import Engine

from sellershield_agent.approvals.helpers import LiveStoreBundle, fetch_live_store_bundle
from sellershield_agent.audit.models import ActorType
from sellershield_agent.clock import Clock
from sellershield_agent.domain.models import Order, RefundStatus
from sellershield_agent.execution.common import (
    SagaPlan,
    build_service_ctx,
    classify_store_error,
    step_idempotency_key,
)
from sellershield_agent.execution.db_ops import (
    finish_execution_step_sync,
    start_execution_step_sync,
)
from sellershield_agent.execution.release_db import (
    ReleaseContext,
    load_release_context_sync,
    record_release_denied_sync,
    sync_approval_refund_status_sync,
)
from sellershield_agent.execution.release_guards import (
    ReleaseSafetyError,
    verify_release_safety,
)
from sellershield_agent.store import StoreClient
from sellershield_agent.store.client import ServiceContext, StoreClientError

__all__ = [
    "ReleaseReconcilePrep",
    "ReleaseSafetyError",
    "evaluate_release_safety_with_store",
    "handle_return_inspected_ok",
    "prepare_release_reconcile",
    "verify_release_safety",
]


@dataclass(frozen=True)
class ReleaseReconcilePrep:
    """Outcome of checking store state + safety gates for reconciling release_refund."""

    status: str  # "succeeded" | "failed" | "retry" | "exc"
    store_ref: str | None = None
    error_code: str | None = None
    exc: BaseException | None = None


async def evaluate_release_safety_with_store(
    *,
    store: StoreClient,
    rel_ctx: ReleaseContext,
    svc_ctx: ServiceContext,
    clock: Clock,
) -> tuple[ReleaseSafetyError | None, LiveStoreBundle]:
    """Fetch live store state outside any DB lock and run verify_release_safety."""
    plan = rel_ctx.plan
    req_pairs = [
        (str(it["item_id"]), int(it.get("quantity", it.get("qty", 1))))
        for it in plan.items
    ]
    bundle = await fetch_live_store_bundle(
        store_client=store,
        svc_ctx=svc_ctx,
        order_id=plan.order_id,
        customer_id=plan.customer_id,
        req_pairs=req_pairs,
        return_id=rel_ctx.return_id,
        refund_id=rel_ctx.refund_id,
        allow_missing_order=True,
    )
    store_settings = await store.get_settings(svc_ctx)
    refund_summary = await store.get_customer_refund_summary(svc_ctx, plan.customer_id, days=30)
    err = verify_release_safety(
        rel_ctx=rel_ctx,
        bundle=bundle,
        store_settings=store_settings,
        refund_summary=refund_summary,
        clock=clock,
    )
    return err, bundle


async def prepare_release_reconcile(
    *,
    engine: Engine,
    store: StoreClient,
    plan: SagaPlan,
    refund_id: str,
    order: Order,
    svc_ctx: ServiceContext,
    clock: Clock,
) -> ReleaseReconcilePrep:
    """Evaluate already-committed store refund or safety-gate retry for reconcile_step."""
    pid = plan.proposal_id
    matched_rfd = next((r for r in order.refunds if r.refund_id == refund_id), None)
    if matched_rfd is not None and matched_rfd.status in (
        RefundStatus.processing,
        RefundStatus.completed,
    ):
        await asyncio.to_thread(
            sync_approval_refund_status_sync,
            engine,
            proposal_id=pid,
            refund_status=matched_rfd.status.value,
        )
        return ReleaseReconcilePrep(status="succeeded", store_ref=matched_rfd.refund_id)

    if matched_rfd is not None and matched_rfd.status in (
        RefundStatus.failed,
        RefundStatus.released,
    ):
        await asyncio.to_thread(
            sync_approval_refund_status_sync,
            engine,
            proposal_id=pid,
            refund_status=matched_rfd.status.value,
        )
        return ReleaseReconcilePrep(
            status="failed",
            error_code=f"refund_{matched_rfd.status.value}",
        )

    rel_ctx = await asyncio.to_thread(
        load_release_context_sync, engine, proposal_id=pid, request_id=plan.request_id
    )
    if rel_ctx is None:
        return ReleaseReconcilePrep(status="failed", error_code="missing_proposal_context")

    try:
        safety_err, _ = await evaluate_release_safety_with_store(
            store=store, rel_ctx=rel_ctx, svc_ctx=svc_ctx, clock=clock
        )
    except (TimeoutError, StoreClientError) as exc:
        return ReleaseReconcilePrep(status="exc", exc=exc)

    if safety_err is not None:
        await asyncio.to_thread(
            record_release_denied_sync,
            engine,
            resource_type="proposals",
            resource_id=pid,
            session_id=plan.session_id,
            policy_version=plan.policy_version,
            reason=safety_err.reason,
            details=safety_err.details,
            clock=clock,
            request_id=plan.request_id,
            actor_type=ActorType.SYSTEM,
            actor_id="reconciler",
        )
        return ReleaseReconcilePrep(status="failed", error_code="release_safety_denied")

    return ReleaseReconcilePrep(status="retry")


async def handle_return_inspected_ok(
    *,
    engine: Engine,
    store_client: StoreClient,
    clock: Clock,
    return_id: str,
    request_id: str,
) -> None:
    """React safely to return.updated=inspected_ok by gating and releasing the refund."""
    rel_ctx = await asyncio.to_thread(
        load_release_context_sync, engine, return_id=return_id, request_id=request_id
    )
    if rel_ctx is None:
        await asyncio.to_thread(
            record_release_denied_sync,
            engine,
            resource_type="returns",
            resource_id=return_id,
            session_id=None,
            policy_version=None,
            reason="unknown_return_for_release",
            details={"return_id": return_id},
            clock=clock,
            request_id=request_id,
        )
        return

    plan = rel_ctx.plan
    if rel_ctx.release_step is not None:
        step_st = str(rel_ctx.release_step.get("status", ""))
        if step_st in ("succeeded", "executing", "unknown", "failed"):
            return

    res_step = rel_ctx.by_step.get("reserve_refund")
    if (
        rel_ctx.refund_id is None
        or res_step is None
        or str(res_step.get("status", "")) != "succeeded"
    ):
        await asyncio.to_thread(
            record_release_denied_sync,
            engine,
            resource_type="proposals",
            resource_id=plan.proposal_id,
            session_id=plan.session_id,
            policy_version=plan.policy_version,
            reason="refund_not_reserved",
            details={"return_id": return_id, "refund_id": rel_ctx.refund_id},
            clock=clock,
            request_id=request_id,
        )
        return

    svc_ctx = build_service_ctx(clock, request_id)
    try:
        safety_err, _ = await evaluate_release_safety_with_store(
            store=store_client, rel_ctx=rel_ctx, svc_ctx=svc_ctx, clock=clock
        )
    except (TimeoutError, StoreClientError) as exc:
        _, err_code = classify_store_error(exc)
        await asyncio.to_thread(
            record_release_denied_sync,
            engine,
            resource_type="proposals",
            resource_id=plan.proposal_id,
            session_id=plan.session_id,
            policy_version=plan.policy_version,
            reason="store_safety_read_failed",
            details={"return_id": return_id, "error_code": err_code},
            clock=clock,
            request_id=request_id,
        )
        return

    if safety_err is not None:
        await asyncio.to_thread(
            record_release_denied_sync,
            engine,
            resource_type="proposals",
            resource_id=plan.proposal_id,
            session_id=plan.session_id,
            policy_version=plan.policy_version,
            reason=safety_err.reason,
            details=safety_err.details,
            clock=clock,
            request_id=request_id,
        )
        return

    idem_key = step_idempotency_key(plan.proposal_id, "release_refund")
    exec_id, _ = await asyncio.to_thread(
        start_execution_step_sync,
        engine,
        proposal_id=plan.proposal_id,
        step="release_refund",
        idempotency_key=idem_key,
        actor_type=ActorType.SYSTEM,
        actor_id="webhook_release",
        policy_version=plan.policy_version,
        session_id=plan.session_id,
        request_id=request_id,
        clock=clock,
    )

    try:
        released = await store_client.release_refund(svc_ctx, rel_ctx.refund_id, idem_key)
    except (TimeoutError, StoreClientError) as exc:
        is_transient, err_code = classify_store_error(exc)
        new_st = "unknown" if is_transient else "failed"
        await asyncio.to_thread(
            finish_execution_step_sync,
            engine,
            proposal_id=plan.proposal_id,
            execution_id=exec_id,
            step="release_refund",
            new_status=new_st,
            store_ref=None,
            error_code=err_code,
            actor_type=ActorType.SYSTEM,
            actor_id="webhook_release",
            policy_version=plan.policy_version,
            session_id=plan.session_id,
            request_id=request_id,
            clock=clock,
        )
        return

    await asyncio.to_thread(
        finish_execution_step_sync,
        engine,
        proposal_id=plan.proposal_id,
        execution_id=exec_id,
        step="release_refund",
        new_status="succeeded",
        store_ref=released.refund_id,
        error_code=None,
        actor_type=ActorType.SYSTEM,
        actor_id="webhook_release",
        policy_version=plan.policy_version,
        session_id=plan.session_id,
        request_id=request_id,
        clock=clock,
    )
    if rel_ctx.approval is not None:
        await asyncio.to_thread(
            sync_approval_refund_status_sync,
            engine,
            proposal_id=plan.proposal_id,
            refund_status=released.status.value,
        )
