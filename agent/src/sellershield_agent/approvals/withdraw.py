"""Customer approval withdrawal workflow (CHANGE-003)."""

from __future__ import annotations

import asyncio

from sqlalchemy import Engine

from sellershield_agent.approvals.common import raise_mapped_store_error
from sellershield_agent.approvals.expire import expire_single_approval
from sellershield_agent.approvals.models import WithdrawApprovalResponse
from sellershield_agent.approvals.withdraw_db import (
    finalize_withdrawn_sync,
    load_withdraw_context_sync,
)
from sellershield_agent.auth.models import Role
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine
from sellershield_agent.domain.models import RefundStatus, ReturnStatus
from sellershield_agent.domain.state import (
    ApprovalStatus,
    InvalidTransition,
    VersionMismatch,
    is_approval_expired,
)
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.proposals.integrity import compute_request_hash
from sellershield_agent.store.client import RequestContext, StoreClient, StoreClientError


async def withdraw_customer_approval(
    customer_ctx: RequestContext,
    proposal_id: str,
    *,
    idempotency_key: str,
    clock: Clock,
    store_client: StoreClient,
    engine: Engine | None = None,
) -> WithdrawApprovalResponse:
    """Allow the owning customer to withdraw a pending seller approval before a decision."""
    resolved_engine = engine or get_engine()
    customer_id = customer_ctx.principal.customer_id
    if customer_ctx.principal.role != Role.customer or not customer_id:
        raise AppHTTPException(status_code=403, code="forbidden", message="Access forbidden")

    req_hash = compute_request_hash(
        operation="withdraw",
        proposal_id=proposal_id,
        canonical_body={},
    )
    replay, prop_row, apr_row = await asyncio.to_thread(
        load_withdraw_context_sync,
        resolved_engine,
        proposal_id=proposal_id,
        customer_id=customer_id,
        idempotency_key=idempotency_key,
        request_hash=req_hash,
    )
    if replay is not None:
        return WithdrawApprovalResponse.model_validate(replay)

    if prop_row is None or apr_row is None:
        raise AppHTTPException(
            status_code=404,
            code="not_found",
            message="Pending approval not found",
            details={},
        )

    approval_id = str(apr_row.approval_id)
    curr_status = str(apr_row.status)
    if curr_status not in (
        ApprovalStatus.pending.value,
        ApprovalStatus.needs_revalidation.value,
    ):
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message=f"Approval cannot be withdrawn in status '{curr_status}'",
        )

    if is_approval_expired(str(apr_row.expires_at), clock):
        apr_dict = dict(apr_row._mapping)
        apr_dict["customer_id"] = customer_id
        try:
            await expire_single_approval(
                engine=resolved_engine,
                store_client=store_client,
                apr_item=apr_dict,
                clock=clock,
                request_id=customer_ctx.request_id,
                actor_id="reconciler",
            )
        except (StoreClientError, TimeoutError):
            pass
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message="Approval has already expired",
        )

    svc_ctx = build_service_ctx(clock, customer_ctx.request_id)
    refund_id = str(apr_row.refund_id) if apr_row.refund_id else None
    return_id = str(apr_row.return_id) if apr_row.return_id else None

    try:
        if refund_id is not None:
            rfd = await store_client.get_refund(svc_ctx, refund_id)
            if rfd.status == RefundStatus.reserved:
                await store_client.cancel_reservation(
                    svc_ctx,
                    refund_id,
                    f"{approval_id}:withdraw_cancel_reservation",
                )
        if return_id is not None:
            ret = await store_client.get_return(svc_ctx, return_id)
            if ret.status == ReturnStatus.requested:
                await store_client.transition_return(
                    svc_ctx,
                    return_id,
                    "cancelled",
                    "Withdrawn by customer",
                    f"{approval_id}:withdraw_cancel_return",
                )
    except (StoreClientError, TimeoutError) as exc:
        raise_mapped_store_error(exc)

    response = WithdrawApprovalResponse(
        approval_id=approval_id,
        proposal_id=proposal_id,
        status="withdrawn",
    )
    try:
        await asyncio.to_thread(
            finalize_withdrawn_sync,
            resolved_engine,
            approval_id=approval_id,
            expected_version=int(apr_row.version),
            proposal_id=proposal_id,
            order_id=str(apr_row.order_id),
            customer_id=customer_id,
            policy_version=str(apr_row.policy_version),
            session_id=str(prop_row.session_id),
            idempotency_key=idempotency_key,
            request_hash=req_hash,
            response_dict=response.model_dump(mode="json"),
            clock=clock,
            request_id=customer_ctx.request_id,
        )
    except (InvalidTransition, VersionMismatch):
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message="Approval state changed concurrently",
        ) from None

    return response
