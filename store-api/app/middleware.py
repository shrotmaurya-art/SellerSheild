import time
import uuid
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.logging import logger

# Maximum request body size: 1 MB (general), 5 MB for evidence uploads
MAX_BODY_BYTES = 1_048_576
MAX_UPLOAD_BODY_BYTES = 5_242_880


class RequestIdLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Create request_id if missing or empty, else preserve provided header
        incoming_id = request.headers.get("X-Request-ID")
        if incoming_id and incoming_id.strip():
            request_id = incoming_id.strip()
        else:
            request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        start_time = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(
                f"HTTP {request.method} {request.url.path} failed",
                extra={
                    "request_id": request_id,
                    "route": request.url.path,
                    "status_code": 500,
                    "latency_ms": round(latency_ms, 2),
                },
            )
            raise

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        response.headers["X-Request-ID"] = request_id

        logger.info(
            f"HTTP {request.method} {request.url.path} {response.status_code}",
            extra={
                "request_id": request_id,
                "route": request.url.path,
                "status_code": response.status_code,
                "latency_ms": round(latency_ms, 2),
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response: CSP, nosniff, frame-ancestors."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "frame-ancestors 'none'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "0"
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than 1 MB (or 3 MB for evidence uploads)."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        content_length = request.headers.get("content-length")
        if content_length:
            # Evidence upload routes get a higher limit for multipart overhead
            is_evidence_upload = request.method == "POST" and "/evidence" in request.url.path
            limit = MAX_UPLOAD_BODY_BYTES if is_evidence_upload else MAX_BODY_BYTES
            if int(content_length) > limit:
                limit_mb = limit / (1024 * 1024)
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "payload_too_large",
                            "message": f"Request body exceeds {limit_mb:.0f} MB limit",
                            "request_id": getattr(request.state, "request_id", ""),
                            "details": None,
                        }
                    },
                )
        return await call_next(request)
