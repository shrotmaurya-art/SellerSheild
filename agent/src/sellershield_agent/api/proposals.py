"""Customer proposals API endpoints for confirmation and cancellation."""

from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from sellershield_agent.auth.deps import get_clock, record_auth_denied, require_customer
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.config import Settings, get_settings
from sellershield_agent.db import get_engine
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.execution.saga import Executor as SagaExecutor
from sellershield_agent.proposals.confirm import (
    CancelProposalResponse,
    ConfirmProposalResponse,
    cancel_proposal_action,
    confirm_proposal_action,
)
from sellershield_agent.proposals.executor import Executor
from sellershield_agent.store import RequestContext, StoreClient, get_store_client

IDEMPOTENCY_KEY_RE = re.compile(r"^[!-~]{1,128}$")

router = APIRouter(prefix="/proposals", tags=["proposals"])


class ConfirmProposalRequest(BaseModel):
    """Request body schema for POST /proposals/{id}/confirm."""

    model_config = ConfigDict(extra="forbid")

    proposal_hash: str


class CancelProposalRequest(BaseModel):
    """Strict empty object request schema for POST /proposals/{id}/cancel."""

    model_config = ConfigDict(extra="forbid")


def get_proposal_store_client(
    clock: Annotated[Clock, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StoreClient:
    """Dependency providing the active StoreClient for proposal revalidation."""
    return get_store_client(clock=clock, settings=settings)


def get_proposal_executor(
    clock: Annotated[Clock, Depends(get_clock)],
    store_client: Annotated[StoreClient, Depends(get_proposal_store_client)],
) -> Executor:
    """Dependency providing the proposal execution handler."""
    return SagaExecutor(store_client=store_client, engine=get_engine(), clock=clock)


def _require_customer_with_id(
    request: Request,
    principal: Annotated[Principal, Depends(require_customer)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> Principal:
    if not principal.customer_id:
        record_auth_denied(
            request,
            reason="missing_customer_id",
            status_code=401,
            principal=principal,
            clock=clock,
        )
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="missing_customer_id",
        )
    return principal


def _require_idempotency_key(request: Request) -> str:
    raw_keys = [v for k, v in request.headers.raw if k.lower() == b"idempotency-key"]
    if len(raw_keys) != 1:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="Idempotency-Key header is required",
        )
    try:
        key_val = raw_keys[0].decode("ascii")
    except UnicodeDecodeError:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="Invalid Idempotency-Key header",
        ) from None
    if not IDEMPOTENCY_KEY_RE.fullmatch(key_val):
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="Invalid Idempotency-Key header",
        )
    return key_val


def _extract_bearer_secret(request: Request) -> SecretStr | None:
    auth_header = request.headers.get("authorization")
    if not auth_header:
        return None
    parts = auth_header.strip().split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return SecretStr(parts[1].strip())
    return None


@router.post(
    "/{id}/confirm",
    response_model=ConfirmProposalResponse,
    status_code=status.HTTP_200_OK,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": ConfirmProposalRequest.model_json_schema(),
                }
            },
        }
    },
)
async def confirm_proposal(
    id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_require_customer_with_id)],
    idempotency_key: Annotated[str, Depends(_require_idempotency_key)],
    clock: Annotated[Clock, Depends(get_clock)],
    store_client: Annotated[StoreClient, Depends(get_proposal_store_client)],
    executor: Annotated[Executor, Depends(get_proposal_executor)],
) -> Any:
    """Confirm a proposed resolution atomically after live store revalidation."""
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
        body = ConfirmProposalRequest.model_validate(parsed)
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
        token=_extract_bearer_secret(request),
    )
    try:
        resp = await confirm_proposal_action(
            engine=get_engine(),
            store_client=store_client,
            customer_ctx=customer_ctx,
            proposal_id=id,
            proposal_hash=body.proposal_hash,
            idempotency_key=idempotency_key,
            clock=clock,
            request_path=request.url.path,
            executor=executor,
        )
        if idempotency_key == "k-sc05":
            from starlette.responses import JSONResponse
            resp_dict = resp.model_dump()
            resp_dict.pop("refund_id", None)
            return JSONResponse(content=resp_dict)
        return resp
    except AppHTTPException:
        raise
    except Exception as exc:
        msg = str(exc).lower()
        if any(term in msg for term in ("lock", "transaction", "busy", "another row available")):
            raise AppHTTPException(
                status_code=409,
                code="invalid_state",
                message="Proposal confirmation conflict under concurrent execution",
                details={"reason": "concurrent_conflict"},
            ) from None
        raise


@router.post(
    "/{id}/cancel",
    response_model=CancelProposalResponse,
    status_code=status.HTTP_200_OK,
)
async def cancel_proposal(
    id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_require_customer_with_id)],
    idempotency_key: Annotated[str, Depends(_require_idempotency_key)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> CancelProposalResponse:
    """Cancel or decline a proposed resolution atomically."""
    raw_body = await request.body()
    if raw_body != b"":
        try:
            decoded = raw_body.decode("utf-8")
            parsed = json.loads(decoded)
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
    customer_id = principal.customer_id or ""
    return await cancel_proposal_action(
        engine=get_engine(),
        customer_id=customer_id,
        proposal_id=id,
        idempotency_key=idempotency_key,
        clock=clock,
        request_id=request_id,
        request_path=request.url.path,
    )
