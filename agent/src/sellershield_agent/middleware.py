import re
import time
import uuid
from collections.abc import Awaitable, Callable

import orjson
import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sellershield_agent.logging import get_logger

REQUEST_ID_REGEX = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_BODY_BYTES = 1024 * 1024  # 1 MB

logger = get_logger("sellershield_agent.access")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Enforces valid X-Request-ID, binds it to logging context, and echoes it in responses."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming_id = request.headers.get("X-Request-ID")
        if incoming_id and REQUEST_ID_REGEX.match(incoming_id):
            request_id = incoming_id
        else:
            request_id = f"req-{uuid.uuid4().hex[:16]}"

        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        response.headers["X-Request-ID"] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emits exactly one structured access log entry per request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start_time = time.perf_counter()
        response = await call_next(request)
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            latency_ms=round(latency_ms, 2),
        )
        return response


class BodySizeLimitExceeded(Exception):
    def __init__(self, received_bytes: int):
        self.received_bytes = received_bytes
        super().__init__(f"Body size {received_bytes} exceeded {MAX_BODY_BYTES}")


class BodyLimitMiddleware:
    """Pure ASGI middleware that enforces 1 MB body limit, returning 413 in standard envelope."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length.decode("latin1")) > self.max_bytes:
                    await self._send_413(scope, send)
                    return
            except ValueError:
                pass

        received_bytes = 0

        async def custom_receive() -> Message:
            nonlocal received_bytes
            msg = await receive()
            if msg["type"] == "http.request":
                chunk = msg.get("body", b"")
                received_bytes += len(chunk)
                if received_bytes > self.max_bytes:
                    raise BodySizeLimitExceeded(received_bytes)
            return msg

        try:
            await self.app(scope, custom_receive, send)
        except BodySizeLimitExceeded:
            await self._send_413(scope, send)

    async def _send_413(self, scope: Scope, send: Send) -> None:
        headers = dict(scope.get("headers", []))
        req_id_raw = headers.get(b"x-request-id", b"")
        req_id = req_id_raw.decode("latin1") if req_id_raw else f"req-{uuid.uuid4().hex[:16]}"
        payload = orjson.dumps({
            "error": {
                "code": "limit_exceeded",
                "message": "Request body exceeds maximum allowed size (1 MB)",
                "request_id": req_id,
                "details": {"max_bytes": self.max_bytes},
            }
        })
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("latin1")),
                (b"x-request-id", req_id.encode("latin1")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": payload,
        })
