"""Customer approval withdrawal endpoint (CHANGE-003)."""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from pydantic import ValidationError

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.proposals import CancelProposalRequest
from sellershield_agent.approvals.models import WithdrawApprovalResponse
from sellershield_agent.approvals.withdraw import withdraw_customer_approval
from sellershield_agent.auth.deps import get_clock
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.store import RequestContext, StoreClient

router = APIRouter(prefix="/me", tags=["proposals"])


@router.post(
    "/approvals/{proposal_id}/withdraw",
    response_model=WithdrawApprovalResponse,
    status_code=status.HTTP_200_OK,
)
async def withdraw_approval(
    proposal_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(proposals_api._require_customer_with_id)],
    idempotency_key: Annotated[str, Depends(proposals_api._require_idempotency_key)],
    clock: Annotated[Clock, Depends(get_clock)],
    store_client: Annotated[StoreClient, Depends(proposals_api.get_proposal_store_client)],
) -> WithdrawApprovalResponse:
    """Allow an authenticated customer to withdraw a pending seller approval request."""
    raw_body = await request.body()
    if raw_body != b"":
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
            CancelProposalRequest.model_validate(parsed)
        except ValidationError:
            raise AppHTTPException(
                status_code=422,
                code="validation_error",
                message="Request validation failed",
            ) from None

    request_id = getattr(request.state, "request_id", None) or f"req-{uuid.uuid4().hex[:12]}"
    customer_ctx = RequestContext(
        principal=principal,
        request_id=request_id,
        token=proposals_api._extract_bearer_secret(request),
    )
    return await withdraw_customer_approval(
        customer_ctx,
        proposal_id,
        idempotency_key=idempotency_key,
        clock=clock,
        store_client=store_client,
        engine=get_engine(),
    )
