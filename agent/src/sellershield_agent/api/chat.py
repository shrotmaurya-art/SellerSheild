"""Chat API endpoints for session management and SSE response streaming."""

from __future__ import annotations

import inspect
import json
import secrets
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse, ServerSentEvent  # type: ignore[attr-defined]

from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth.deps import get_clock, require_customer
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.config import Settings, get_settings
from sellershield_agent.db import get_engine, write_tx
from sellershield_agent.domain.session import (
    ChatMessageInput,
    messages,
    sessions,
)
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.logging import get_logger
from sellershield_agent.orchestrator.pipeline import ConversationPipeline
from sellershield_agent.store.client import StoreClient

logger = get_logger("sellershield_agent.api.chat")

router = APIRouter(prefix="/v1/chat", tags=["chat"])

_pipeline_instance: ConversationPipeline | None = None


def reset_chat_pipeline() -> None:
    """Reset the cached pipeline instance (for test isolation)."""
    global _pipeline_instance
    _pipeline_instance = None


def get_store_client(clock: Clock, settings: Settings | None = None) -> StoreClient:
    from sellershield_agent.store import get_store_client as factory_get_store_client

    return factory_get_store_client(clock=clock, settings=settings)


def _resolve_chat_store_client(
    request: Request,
    *,
    clock: Clock,
    settings: Settings,
) -> StoreClient:
    """Resolve StoreClient honoring FastAPI dependency overrides on get_store_client."""
    provider = request.app.dependency_overrides.get(get_store_client)
    if callable(provider):
        params = inspect.signature(provider).parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if has_var_kw:
            return provider(clock=clock, settings=settings)  # type: ignore[no-any-return]
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


def get_pipeline(
    request: Request,
    clock: Annotated[Clock, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConversationPipeline:
    global _pipeline_instance
    store_provider = request.app.dependency_overrides.get(get_store_client)
    if callable(store_provider):
        store_client = _resolve_chat_store_client(request, clock=clock, settings=settings)
        return ConversationPipeline(
            engine=get_engine(),
            clock=clock,
            store_client=store_client,
            settings=settings,
        )
    if _pipeline_instance is None:
        engine = get_engine()
        store_client = get_store_client(clock=clock, settings=settings)
        _pipeline_instance = ConversationPipeline(
            engine=engine,
            clock=clock,
            store_client=store_client,
            settings=settings,
        )
    return _pipeline_instance


@router.post("/sessions", status_code=status.HTTP_200_OK)
def create_chat_session(
    principal: Annotated[Principal, Depends(require_customer)],
    clock: Annotated[Clock, Depends(get_clock)],
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Create a new chat session for the authenticated customer."""
    customer_id = principal.customer_id
    if not customer_id:
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Customer identity required",
        )

    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    now_str = clock.now().isoformat()
    engine = get_engine()

    with write_tx(engine) as conn:
        conn.execute(
            sessions.insert().values(
                id=session_id,
                customer_id=customer_id,
                channel="web",
                handler="agent",
                language="en",
                state_json="{}",
                started_at=now_str,
                last_active_at=now_str,
            )
        )
        req_id = request.headers.get("x-request-id") if request is not None else None
        AuditService.append(
            conn,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=now_str,
                request_id=req_id,
                session_id=session_id,
                actor_type=ActorType.CUSTOMER,
                actor_id=customer_id,
                event_type=EventType.MESSAGE_RECEIVED,
                resource_type="sessions",
                resource_id=session_id,
                action="create_chat_session",
                outcome=Outcome.SUCCESS,
                policy_version=None,
                details={"channel": "web"},
            ),
        )

    from sellershield_agent.memory.snapshot import get_memory_snapshot

    dynamic_chips: list[dict[str, Any]] = []
    try:
        snap = get_memory_snapshot(engine, customer_id)
        dynamic_chips = snap.get("greeting_chips", [])
    except Exception:
        dynamic_chips = []

    default_chips = [
        {"label": "Track Order", "action": "track_order"},
        {"label": "Return an Item", "action": "start_return"},
        {"label": "Refund Status", "action": "refund_status"},
        {"label": "Store Policy", "action": "policy_info"},
        {"label": "Talk to Human", "action": "talk_to_human"},
    ]
    all_chips = [*dynamic_chips, *default_chips]

    return {
        "session_id": session_id,
        "chips": all_chips,
        "greeting_chips": all_chips,
    }


@router.get("/sessions/{session_id}", status_code=status.HTTP_200_OK)
def get_chat_session_history(
    session_id: str,
    principal: Annotated[Principal, Depends(require_customer)],
) -> dict[str, Any]:
    """Retrieve customer-visible message history for a session."""
    customer_id = principal.customer_id
    engine = get_engine()

    with engine.connect() as conn:
        # Uniform 404 query binding customer_id
        session_row = conn.execute(
            select(sessions).where(
                sessions.c.id == session_id,
                sessions.c.customer_id == customer_id,
            )
        ).fetchone()

        if session_row is None:
            raise AppHTTPException(
                status_code=404,
                code="not_found",
                message="Session not found",
            )

        msg_rows = conn.execute(
            select(messages)
            .where(messages.c.session_id == session_id)
            .order_by(messages.c.created_at.asc())
        ).fetchall()

    history = [
        {
            "message_id": m.id,
            "sender": m.role,
            "text": m.text,
            "created_at": m.created_at,
        }
        for m in msg_rows
    ]

    return {
        "session_id": session_id,
        "messages": history,
    }


@router.post("/sessions/{session_id}/messages")
async def send_chat_message(
    session_id: str,
    raw_body: dict[str, Any],
    request: Request,
    principal: Annotated[Principal, Depends(require_customer)],
    pipeline: Annotated[ConversationPipeline, Depends(get_pipeline)],
    authorization: Annotated[str | None, Header()] = None,
) -> EventSourceResponse:
    """Send a message/action to a chat session and stream response via SSE."""
    customer_id = principal.customer_id
    if not customer_id:
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Customer identity required",
        )

    engine = get_engine()

    # Uniform 404 check for session existence and ownership
    with engine.connect() as conn:
        session_row = conn.execute(
            select(sessions).where(
                sessions.c.id == session_id,
                sessions.c.customer_id == customer_id,
            )
        ).fetchone()

    if session_row is None:
        raise AppHTTPException(
            status_code=404,
            code="not_found",
            message="Session not found",
        )

    # Rate limiting: 20 messages per minute per customer
    rate_limit_key = customer_id
    if not user_message_limiter.is_allowed(rate_limit_key, limit=20, window_seconds=60.0):
        raise AppHTTPException(
            status_code=429,
            code="rate_limited",
            message="Rate limit exceeded. Maximum 20 messages per minute.",
        )

    # Validate body
    text_val = raw_body.get("text")
    action_val = raw_body.get("action")
    client_msg_id = raw_body.get("client_msg_id")

    if not client_msg_id or not isinstance(client_msg_id, str):
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="client_msg_id is required",
        )

    if text_val is None and action_val is None:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="Either text or action must be provided",
        )

    if text_val is not None:
        if not isinstance(text_val, str) or len(text_val) > 1000:
            raise AppHTTPException(
                status_code=422,
                code="validation_error",
                message="Message text cannot exceed 1000 characters",
            )

    try:
        input_data = ChatMessageInput.model_validate(raw_body)
    except Exception as e:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message=f"Invalid chat message payload: {e}",
        ) from e

    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    request_id = getattr(request.state, "request_id", f"req-{uuid.uuid4().hex[:12]}")

    # Log metadata only, never raw customer message text
    logger.info(
        "chat_message_received",
        session_id=session_id,
        client_msg_id=client_msg_id,
        has_action=action_val is not None,
        char_count=len(text_val) if text_val else 0,
        request_id=request_id,
    )

    async def sse_event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        async for event_name, payload in pipeline.execute_stream(
            session_id=session_id,
            principal=principal,
            token=token,
            input_data=input_data,
            request_id=request_id,
        ):
            yield ServerSentEvent(
                event=event_name,
                data=json.dumps(payload),
            )

    return EventSourceResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
