"""Signed store webhook ingestion endpoint (POST /v1/webhooks/store)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import re
from datetime import datetime
from typing import Annotated, Any

import orjson
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator
from sqlalchemy import Engine

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.auth.deps import get_clock
from sellershield_agent.clock import Clock
from sellershield_agent.config import Settings, get_settings
from sellershield_agent.db import get_engine
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.execution.refund_webhook_db import apply_refund_updated_sync
from sellershield_agent.execution.release import handle_return_inspected_ok
from sellershield_agent.execution.webhook_db import (
    ingest_webhook_event_sync,
    record_webhook_auth_denied_sync,
)
from sellershield_agent.middleware import MAX_BODY_BYTES
from sellershield_agent.store import StoreClient, get_store_client

router = APIRouter(tags=["webhooks"])

_TIMESTAMP_RE = re.compile(r"^(?:0|[1-9][0-9]{0,14})$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_PAST_SECONDS = 300
_MAX_FUTURE_SECONDS = 60


class WebhookEnvelope(BaseModel):
    """Strict webhook envelope matching OpenAPI WebhookEnvelope."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    resource_version: StrictInt = Field(ge=0)
    data: dict[str, Any]

    @field_validator("occurred_at", mode="after")
    @classmethod
    def ensure_aware_utc(cls, v: datetime) -> datetime:
        """Reject timezone-naive webhook timestamps."""
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return v


class WebhookAckResponse(BaseModel):
    """Minimal acknowledgment payload for store webhooks."""

    model_config = ConfigDict(extra="forbid")

    received: bool = True
    event_id: str


def _single_raw_header(request: Request, name_lower: bytes) -> str | None:
    """Extract an exact single non-empty header value; reject duplicates (smuggling protection)."""
    vals = [v for k, v in request.headers.raw if k.lower() == name_lower]
    if len(vals) != 1:
        return None
    try:
        decoded = vals[0].decode("latin-1")
    except UnicodeDecodeError:
        return None
    return decoded if decoded else None


async def _fail_auth(
    *,
    engine: Engine,
    request_id: str,
    clock: Clock,
    event_id_hint: str | None,
    reason: str,
    path: str,
) -> AppHTTPException:
    await asyncio.to_thread(
        record_webhook_auth_denied_sync,
        engine,
        request_id=request_id,
        clock=clock,
        event_id_hint=event_id_hint,
        reason=reason,
        path=path,
    )
    return AppHTTPException(
        status_code=401,
        code="unauthenticated",
        message="Authentication failed",
        details={},
    )


def _extract_resource_key(envelope: WebhookEnvelope) -> tuple[str, str]:
    ev_type = envelope.type
    data = envelope.data
    if ev_type.startswith("return."):
        res_id = data.get("return_id") or data.get("order_id") or envelope.event_id
        return "return", str(res_id)[:64]
    if ev_type.startswith("refund."):
        res_id = data.get("refund_id") or data.get("return_id") or envelope.event_id
        return "refund", str(res_id)[:64]
    if ev_type.startswith("order."):
        res_id = data.get("order_id") or envelope.event_id
        return "order", str(res_id)[:64]
    if ev_type.startswith("shipment."):
        res_id = data.get("shipment_id") or data.get("order_id") or envelope.event_id
        return "shipment", str(res_id)[:64]
    if ev_type.startswith("mail."):
        res_id = data.get("message_id") or data.get("thread_id") or envelope.event_id
        return "mail", str(res_id)[:64]
    res_id = (
        data.get("return_id")
        or data.get("refund_id")
        or data.get("order_id")
        or envelope.event_id
    )
    return "generic", str(res_id)[:64]


def _resolve_webhook_store_client(
    request: Request,
    *,
    clock: Clock,
    settings: Settings,
) -> StoreClient:
    """Resolve StoreClient lazily only when an event requires a store call."""
    provider = request.app.dependency_overrides.get(proposals_api.get_proposal_store_client)
    if callable(provider):
        params = inspect.signature(provider).parameters
        required = [
            p
            for p in params.values()
            if p.default is inspect.Parameter.empty
            and p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if not required:
            return provider()  # type: ignore[no-any-return]
        return provider(clock=clock, settings=settings)  # type: ignore[no-any-return]
    return get_store_client(clock=clock, settings=settings)


@router.post("/webhooks/store", response_model=WebhookAckResponse)
async def receive_store_webhook(
    request: Request,
    clock: Annotated[Clock, Depends(get_clock)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> WebhookAckResponse:
    """Verify HMAC-SHA256 webhook signature, replay window, dedupe/ordering, and dispatch."""
    request_id = str(getattr(request.state, "request_id", "req-webhook"))
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        raise AppHTTPException(
            status_code=413,
            code="limit_exceeded",
            message="Request body exceeds maximum allowed size (1 MB)",
            details={"max_bytes": MAX_BODY_BYTES},
        )

    x_event_id = _single_raw_header(request, b"x-event-id")
    x_ts = _single_raw_header(request, b"x-store-timestamp")
    x_sig = _single_raw_header(request, b"x-store-signature")
    settings = get_settings()
    secret = settings.WEBHOOK_SECRET.get_secret_value() if settings.WEBHOOK_SECRET else ""

    if not secret:
        raise await _fail_auth(
            engine=engine,
            request_id=request_id,
            clock=clock,
            event_id_hint=x_event_id,
            reason="webhook_secret_unset",
            path=request.url.path,
        )

    if not x_event_id or not x_ts or not x_sig or len(x_event_id) > 64:
        raise await _fail_auth(
            engine=engine,
            request_id=request_id,
            clock=clock,
            event_id_hint=x_event_id,
            reason="missing_or_duplicate_header",
            path=request.url.path,
        )

    if not _TIMESTAMP_RE.match(x_ts):
        raise await _fail_auth(
            engine=engine,
            request_id=request_id,
            clock=clock,
            event_id_hint=x_event_id,
            reason="malformed_timestamp",
            path=request.url.path,
        )

    if not _HEX64_RE.match(x_sig):
        raise await _fail_auth(
            engine=engine,
            request_id=request_id,
            clock=clock,
            event_id_hint=x_event_id,
            reason="malformed_signature",
            path=request.url.path,
        )

    signed_payload = f"{x_ts}.".encode() + raw_body
    expected_hex = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hex.encode("ascii"), x_sig.lower().encode("ascii")):
        raise await _fail_auth(
            engine=engine,
            request_id=request_id,
            clock=clock,
            event_id_hint=x_event_id,
            reason="invalid_signature",
            path=request.url.path,
        )

    ts_int = int(x_ts)
    now_ts = int(clock.now().timestamp())
    if ts_int < now_ts - _MAX_PAST_SECONDS or ts_int > now_ts + _MAX_FUTURE_SECONDS:
        raise await _fail_auth(
            engine=engine,
            request_id=request_id,
            clock=clock,
            event_id_hint=x_event_id,
            reason="timestamp_out_of_window",
            path=request.url.path,
        )

    try:
        parsed_body = orjson.loads(raw_body)
    except orjson.JSONDecodeError:
        raise AppHTTPException(
            status_code=400,
            code="validation_error",
            message="Malformed JSON webhook payload",
            details={},
        ) from None

    try:
        envelope = WebhookEnvelope.model_validate(parsed_body)
    except ValidationError:
        raise AppHTTPException(
            status_code=400,
            code="validation_error",
            message="Webhook payload does not match schema",
            details={},
        ) from None

    if envelope.event_id != x_event_id:
        raise AppHTTPException(
            status_code=400,
            code="validation_error",
            message="Header X-Event-Id does not match body event_id",
            details={},
        )

    res_type, res_id = _extract_resource_key(envelope)
    ingest_status = await asyncio.to_thread(
        ingest_webhook_event_sync,
        engine,
        event_id=envelope.event_id,
        event_type=envelope.type,
        resource_type=res_type,
        resource_id=res_id,
        resource_version=envelope.resource_version,
        occurred_at_iso=envelope.occurred_at.isoformat(),
        request_id=request_id,
        clock=clock,
    )
    if ingest_status != "accepted":
        return WebhookAckResponse(received=True, event_id=envelope.event_id)

    if envelope.type == "return.updated":
        if str(envelope.data.get("status", "")) == "inspected_ok":
            ret_id = str(envelope.data.get("return_id") or "")
            if ret_id:
                store_client = _resolve_webhook_store_client(
                    request, clock=clock, settings=settings
                )
                await handle_return_inspected_ok(
                    engine=engine,
                    store_client=store_client,
                    clock=clock,
                    return_id=ret_id,
                    request_id=request_id,
                )
    elif envelope.type == "refund.updated":
        rfd_id = str(envelope.data.get("refund_id") or "")
        rfd_status = str(envelope.data.get("status") or "")
        if rfd_id and rfd_status:
            ord_id = str(envelope.data["order_id"]) if envelope.data.get("order_id") else None
            rfd_ret_id = str(envelope.data["return_id"]) if envelope.data.get("return_id") else None
            await asyncio.to_thread(
                apply_refund_updated_sync,
                engine,
                refund_id=rfd_id,
                status_val=rfd_status,
                order_id=ord_id,
                return_id=rfd_ret_id,
                clock=clock,
                request_id=request_id,
            )

    return WebhookAckResponse(received=True, event_id=envelope.event_id)
