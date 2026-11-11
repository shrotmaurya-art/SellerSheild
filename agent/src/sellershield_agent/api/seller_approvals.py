"""Seller approval queue, deterministic card, and decision endpoints."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import ValidationError

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.approvals.card import build_approval_card, list_seller_approvals_cards
from sellershield_agent.approvals.common_db import append_audit_sync
from sellershield_agent.approvals.decide import decide_approval
from sellershield_agent.approvals.models import (
    ApprovalDecisionRequest,
    SellerApproval,
    SellerApprovalListResponse,
)
from sellershield_agent.audit.models import ActorType, EventType, Outcome
from sellershield_agent.auth.deps import get_clock, require_seller
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine
from sellershield_agent.domain.state import ApprovalStatus
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.store import RequestContext, StoreClient

router = APIRouter(prefix="/seller", tags=["seller_approvals"])


@router.get(
    "/approvals",
    response_model=SellerApprovalListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_seller_approvals(
    principal: Annotated[Principal, Depends(require_seller)],
    clock: Annotated[Clock, Depends(get_clock)],
    store_client: Annotated[StoreClient, Depends(proposals_api.get_proposal_store_client)],
    request: Request,
    status_filter: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SellerApprovalListResponse:
    """List approval cards filtered cleanly by status and authenticated store_id."""
    request_id = getattr(request.state, "request_id", None) or f"req-{uuid.uuid4().hex[:12]}"
    return await list_seller_approvals_cards(
        engine=get_engine(),
        store_client=store_client,
        clock=clock,
        store_id=principal.store_id,
        status_filter=status_filter.value if status_filter is not None else None,
        limit=limit,
        offset=offset,
        request_id=request_id,
    )


@router.get(
    "/approvals/{id}",
    response_model=SellerApproval,
    status_code=status.HTTP_200_OK,
)
async def get_seller_approval_card(
    id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_seller)],
    clock: Annotated[Clock, Depends(get_clock)],
    store_client: Annotated[StoreClient, Depends(proposals_api.get_proposal_store_client)],
) -> SellerApproval:
    """Fetch a single deterministic seller approval card scoped strictly to caller's store."""
    request_id = getattr(request.state, "request_id", None) or f"req-{uuid.uuid4().hex[:12]}"
    engine = get_engine()
    try:
        card = await build_approval_card(
            engine,
            store_client,
            clock,
            id,
            store_id=principal.store_id,
            request_id=request_id,
        )
    except AppHTTPException as exc:
        if exc.status_code == 404:
            await asyncio.to_thread(
                append_audit_sync,
                engine,
                clock=clock,
                request_id=request_id,
                session_id=None,
                actor_type=ActorType.SELLER,
                actor_id=principal.user_id,
                event_type=EventType.AUTH_DENIED,
                resource_type="approvals",
                resource_id=id,
                action="approval_read_denied",
                outcome=Outcome.DENIED,
                policy_version=None,
                details={"reason": "not_found"},
            )
            raise AppHTTPException(
                status_code=404,
                code="not_found",
                message="Approval not found",
                details={},
            ) from None
        raise
    return SellerApproval.model_validate(card)


@router.post(
    "/approvals/{id}/decision",
    response_model=SellerApproval,
    status_code=status.HTTP_200_OK,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": ApprovalDecisionRequest.model_json_schema(),
                }
            },
        }
    },
)
async def decide_seller_approval(
    id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_seller)],
    idempotency_key: Annotated[str, Depends(proposals_api._require_idempotency_key)],
    clock: Annotated[Clock, Depends(get_clock)],
    store_client: Annotated[StoreClient, Depends(proposals_api.get_proposal_store_client)],
) -> SellerApproval:
    """Submit a seller approve or reject decision bound to live store revalidation."""
    raw_body = await request.body()
    if raw_body == b"":
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="Request validation failed",
        )
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="Request validation failed",
        ) from None
    if not isinstance(parsed, dict):
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="Request validation failed",
        )
    try:
        body = ApprovalDecisionRequest.model_validate(parsed)
    except ValidationError:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="Request validation failed",
        ) from None

    request_id = getattr(request.state, "request_id", None) or f"req-{uuid.uuid4().hex[:12]}"
    seller_ctx = RequestContext(
        principal=principal,
        request_id=request_id,
        token=proposals_api._extract_bearer_secret(request),
    )
    card = await decide_approval(
        seller_ctx,
        id,
        body.decision,
        body.reason,
        body.expected_version,
        idempotency_key=idempotency_key,
        clock=clock,
        store_client=store_client,
        engine=get_engine(),
        request_path=request.url.path,
    )
    return SellerApproval.model_validate(card)
