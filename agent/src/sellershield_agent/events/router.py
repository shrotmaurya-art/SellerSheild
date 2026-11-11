"""Events APIRouter for SSE push streams and event polling."""

from __future__ import annotations

import asyncio
import json
import threading
import weakref
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, select

from sellershield_agent.auth import deps
from sellershield_agent.auth.models import Principal
from sellershield_agent.db import get_engine
from sellershield_agent.domain.state import client_events
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.events.emit import client_event_notifier

router = APIRouter(tags=["events"])

MAX_STREAMS_PER_CUSTOMER = 2
DB_FALLBACK_INTERVAL_SECONDS = 2.0
HEARTBEAT_INTERVAL_SECONDS = 20.0
IDLE_TIMEOUT_SECONDS = 900.0
_MAX_SQLITE_INT = (1 << 63) - 1

# Active stream tracking per customer (process-wide, loop-independent lock)
_stream_lock = threading.Lock()
_active_customer_streams: dict[str, int] = defaultdict(int)


class CustomerEventModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    type: str
    payload: dict[str, Any]
    created_at: str


class CustomerEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[CustomerEventModel]
    next_cursor: str | None = None


def _parse_cursor(cursor_str: str | None, param_name: str = "since") -> int | None:
    if cursor_str is None or cursor_str == "":
        return None
    if not (cursor_str.isascii() and cursor_str.isdigit()):
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message=f"{param_name} must be a non-negative integer",
        )
    value = int(cursor_str)
    if value > _MAX_SQLITE_INT:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message=f"{param_name} cursor out of valid range",
        )
    return value


def _acquire_stream_slot(customer_id: str) -> dict[str, bool]:
    with _stream_lock:
        if _active_customer_streams[customer_id] >= MAX_STREAMS_PER_CUSTOMER:
            raise AppHTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="rate_limited",
                message="Too many open streams: maximum 2 streams per customer.",
            )
        _active_customer_streams[customer_id] += 1
        return {"released": False}


def _release_stream_slot(customer_id: str, slot_state: dict[str, bool]) -> None:
    with _stream_lock:
        if slot_state["released"]:
            return
        slot_state["released"] = True
        current = _active_customer_streams.get(customer_id, 0)
        if current <= 1:
            _active_customer_streams.pop(customer_id, None)
        else:
            _active_customer_streams[customer_id] = current - 1


def _fetch_customer_events_sync(
    engine: Engine,
    customer_id: str,
    since_cursor: int,
    limit: int | None = None,
) -> list[tuple[int, str, str, str, str]]:
    with engine.connect() as conn:
        stmt = (
            select(
                client_events.c.id,
                client_events.c.event_id,
                client_events.c.type,
                client_events.c.payload_json,
                client_events.c.created_at,
            )
            .where(
                client_events.c.customer_id == customer_id,
                client_events.c.id > since_cursor,
            )
            .order_by(client_events.c.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = conn.execute(stmt).fetchall()
    return [
        (int(r.id), str(r.event_id), str(r.type), str(r.payload_json), str(r.created_at))
        for r in rows
    ]


def _format_sse_frame(row: tuple[int, str, str, str, str]) -> str:
    row_id, event_id, event_type, payload_json, created_at = row
    payload_data = json.loads(payload_json)
    event_data = {
        "event_id": event_id,
        "type": event_type,
        "payload": payload_data,
        "created_at": created_at,
    }
    data_json = json.dumps(event_data, separators=(",", ":"))
    return f"id: {row_id}\nevent: {event_type}\ndata: {data_json}\n\n"


async def _is_client_disconnected(request: Request) -> bool:
    try:
        return await request.is_disconnected()
    except RuntimeError:
        return False


@router.get(
    "/chat/stream",
    summary="Real-time customer push notification SSE stream",
    response_class=StreamingResponse,
)
async def stream_chat_push_events(
    request: Request,
    principal: Annotated[Principal, Depends(deps.require_customer)],
    since: Annotated[str | None, Query(description="Cursor integer to resume from")] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    customer_id = principal.customer_id
    if not customer_id:
        raise AppHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message="Customer identity missing",
        )

    # Validate cursors before consuming a stream slot so invalid inputs cannot leak capacity
    since_int = _parse_cursor(since, "since")
    header_int = _parse_cursor(last_event_id, "Last-Event-ID")
    start_cursor = (
        header_int if header_int is not None else (since_int if since_int is not None else 0)
    )

    slot_state = _acquire_stream_slot(customer_id)
    engine = get_engine()

    async def _event_generator() -> AsyncIterator[str]:
        current_cursor = start_cursor
        wake_event = asyncio.Event()
        client_event_notifier.subscribe(customer_id, wake_event)
        loop = asyncio.get_running_loop()
        now = loop.time()
        last_heartbeat = now
        last_activity = now

        try:
            # Initial drain from current cursor
            wake_event.clear()
            initial_rows = await asyncio.to_thread(
                _fetch_customer_events_sync, engine, customer_id, current_cursor
            )
            last_db_check = loop.time()
            for row in initial_rows:
                current_cursor = max(current_cursor, row[0])
                last_activity = last_db_check
                yield _format_sse_frame(row)

            while True:
                if await _is_client_disconnected(request):
                    break

                now = loop.time()
                idle_rem = IDLE_TIMEOUT_SECONDS - (now - last_activity)
                if idle_rem <= 0:
                    break
                db_rem = DB_FALLBACK_INTERVAL_SECONDS - (now - last_db_check)
                hb_rem = HEARTBEAT_INTERVAL_SECONDS - (now - last_heartbeat)
                wait_timeout = max(0.01, min(max(db_rem, 0.01), max(hb_rem, 0.01), idle_rem))

                woken = False
                if wake_event.is_set():
                    woken = True
                else:
                    try:
                        await asyncio.wait_for(wake_event.wait(), timeout=wait_timeout)
                        woken = True
                    except TimeoutError:
                        pass

                if await _is_client_disconnected(request):
                    break

                now = loop.time()
                if now - last_activity >= IDLE_TIMEOUT_SECONDS:
                    break

                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now

                if woken or (now - last_db_check >= DB_FALLBACK_INTERVAL_SECONDS):
                    wake_event.clear()
                    new_rows = await asyncio.to_thread(
                        _fetch_customer_events_sync, engine, customer_id, current_cursor
                    )
                    last_db_check = loop.time()
                    for row in new_rows:
                        current_cursor = max(current_cursor, row[0])
                        last_activity = last_db_check
                        yield _format_sse_frame(row)
        except asyncio.CancelledError:
            raise
        finally:
            client_event_notifier.unsubscribe(customer_id, wake_event)
            _release_stream_slot(customer_id, slot_state)

    gen = _event_generator()
    # Guard against generator garbage collection before initial iteration enters try/finally
    weakref.finalize(gen, _release_stream_slot, customer_id, slot_state)

    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/me/events",
    summary="Poll customer events when SSE stream is unavailable",
    response_model=CustomerEventListResponse,
)
async def poll_customer_events(
    principal: Annotated[Principal, Depends(deps.require_customer)],
    since: Annotated[str | None, Query(description="Cursor integer to poll after")] = None,
    limit: Annotated[
        int, Query(ge=1, le=100, description="Maximum number of events to return (1..100)")
    ] = 50,
) -> CustomerEventListResponse:
    customer_id = principal.customer_id
    if not customer_id:
        raise AppHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message="Customer identity missing",
        )

    since_int = _parse_cursor(since, "since") or 0
    engine = get_engine()
    rows = await asyncio.to_thread(
        _fetch_customer_events_sync, engine, customer_id, since_int, limit
    )

    events_list: list[CustomerEventModel] = []
    max_id = since_int
    for row_id, event_id, event_type, payload_json, created_at in rows:
        max_id = max(max_id, row_id)
        events_list.append(
            CustomerEventModel(
                event_id=event_id,
                type=event_type,
                payload=json.loads(payload_json),
                created_at=created_at,
            )
        )

    next_cursor = str(max_id) if events_list else (str(since_int) if since is not None else None)
    return CustomerEventListResponse(events=events_list, next_cursor=next_cursor)
