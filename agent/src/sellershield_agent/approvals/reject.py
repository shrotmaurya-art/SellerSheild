"""Seller rejection execution bound to the verified seller JWT."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import Engine

from sellershield_agent.approvals.card import build_approval_card
from sellershield_agent.approvals.common import raise_mapped_store_error
from sellershield_agent.approvals.common_db import save_idempotency_sync
from sellershield_agent.approvals.decide_db import (
    finalize_rejected_sync,
    record_reject_error_sync,
)
from sellershield_agent.approvals.helpers import sanitize_seller_note
from sellershield_agent.clock import Clock
from sellershield_agent.domain.models import RefundStatus, ReturnStatus
from sellershield_agent.domain.state import InvalidTransition, VersionMismatch
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.store.client import (
    RequestContext,
    StoreClient,
    StoreClientError,
    StoreInvalidState,
)


async def execute_reject_decision(
    *,
    engine: Engine,
    store_client: StoreClient,
    clock: Clock,
    seller_ctx: RequestContext,
    exec_seller_ctx: RequestContext,
    approval_id: str,
    proposal_id: str,
    order_id: str,
    customer_id: str,
    return_id: str | None,
    refund_id: str | None,
    expected_version: int,
    reason: str | None,
    policy_version: str,
    session_id: str,
    caller_store_id: str,
    actor_key: str,
    idempotency_key: str,
    req_hash: str,
) -> dict[str, Any]:
    """Reject return/cancel reservation with seller JWT, finalize DB, and return updated card."""
    clean_reason = sanitize_seller_note(reason)
    if not clean_reason:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="A non-empty reason is required when rejecting an approval",
        )

    actor_id = seller_ctx.principal.user_id
    try:
        if return_id is not None:
            try:
                await store_client.transition_return(
                    exec_seller_ctx,
                    return_id,
                    "rejected",
                    clean_reason,
                    f"{approval_id}:reject_return",
                )
            except StoreInvalidState:
                live_ret = await store_client.get_return(
                    build_service_ctx(clock, seller_ctx.request_id), return_id
                )
                if live_ret.status != ReturnStatus.rejected:
                    raise

        if refund_id is not None:
            try:
                await store_client.cancel_reservation(
                    exec_seller_ctx,
                    refund_id,
                    f"{approval_id}:cancel_reservation",
                )
            except StoreInvalidState:
                live_rfd = await store_client.get_refund(
                    build_service_ctx(clock, seller_ctx.request_id), refund_id
                )
                if live_rfd.status != RefundStatus.released:
                    raise
    except (StoreClientError, TimeoutError) as exc:
        err_summary = f"{type(exc).__name__}: {exc}"[:300]
        await asyncio.to_thread(
            record_reject_error_sync,
            engine,
            approval_id=approval_id,
            actor_id=actor_id,
            error_summary=err_summary,
            policy_version=policy_version,
            session_id=session_id,
            clock=clock,
            request_id=seller_ctx.request_id,
        )
        raise_mapped_store_error(exc)

    try:
        await asyncio.to_thread(
            finalize_rejected_sync,
            engine,
            approval_id=approval_id,
            expected_version=expected_version,
            proposal_id=proposal_id,
            order_id=order_id,
            customer_id=customer_id,
            return_id=return_id,
            refund_id=refund_id,
            reason=clean_reason,
            actor_id=actor_id,
            policy_version=policy_version,
            session_id=session_id,
            clock=clock,
            request_id=seller_ctx.request_id,
        )
    except (InvalidTransition, VersionMismatch):
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message="Approval state changed concurrently",
        ) from None

    card = await build_approval_card(
        engine,
        store_client,
        clock,
        approval_id,
        store_id=caller_store_id,
        request_id=seller_ctx.request_id,
    )
    await asyncio.to_thread(
        save_idempotency_sync,
        engine,
        actor_key=actor_key,
        idempotency_key=idempotency_key,
        operation="seller_decision",
        target_id=approval_id,
        request_hash=req_hash,
        response_dict=card,
        clock=clock,
    )
    return card
