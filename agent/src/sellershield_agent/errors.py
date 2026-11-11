from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sellershield_agent.logging import get_logger

logger = get_logger("sellershield_agent.errors")

STATUS_CODE_TO_ERROR_CODE: dict[int, str] = {
    400: "validation_error",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "not_found",
    409: "duplicate",
    413: "limit_exceeded",
    422: "validation_error",
    500: "internal",
    502: "upstream_unavailable",
    503: "upstream_unavailable",
    504: "upstream_unavailable",
}

SENSITIVE_FIELD_NAMES = {
    "password",
    "secret",
    "token",
    "key",
    "auth",
    "authorization",
    "cookie",
    "credential",
}


def build_error_envelope(
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build standard API error envelope conforming to contracts/openapi-agent.yaml."""
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details or {},
        }
    }


def _sanitize_validation_error(err: dict[str, Any]) -> dict[str, Any]:
    """Sanitize validation error element to prevent echoing secret inputs."""
    loc = err.get("loc", ())
    loc_str = ".".join(str(item).lower() for item in loc)
    is_sensitive = any(term in loc_str for term in SENSITIVE_FIELD_NAMES)

    cleaned = {
        "loc": list(loc),
        "msg": err.get("msg", ""),
        "type": err.get("type", ""),
    }

    if "input" in err:
        if is_sensitive:
            cleaned["input"] = "[REDACTED]"
        else:
            raw_input = err["input"]
            if isinstance(raw_input, bytes):
                try:
                    cleaned["input"] = raw_input.decode("utf-8", errors="replace")[:100]
                except Exception:
                    cleaned["input"] = str(raw_input)[:100]
            elif isinstance(raw_input, str) and len(raw_input) > 100:
                cleaned["input"] = raw_input[:100] + "... (truncated)"
            else:
                cleaned["input"] = raw_input

    return cleaned


class AppHTTPException(HTTPException):
    """Application HTTP exception carrying an explicit error code and optional details."""

    def __init__(
        self,
        status_code: int,
        code: str | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        reason: str | None = None,
    ) -> None:
        detail = message or "Request failed"
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code or STATUS_CODE_TO_ERROR_CODE.get(status_code, "internal")
        self.message = detail
        self.details = details or {}
        self.reason = reason or self.details.get("reason")


async def http_exception_handler(
    request: Request, exc: HTTPException | StarletteHTTPException
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    code = getattr(exc, "code", None) or STATUS_CODE_TO_ERROR_CODE.get(exc.status_code, "internal")
    message = getattr(exc, "message", None) or (str(exc.detail) if exc.detail else "Request failed")
    details = getattr(exc, "details", None) or {}

    return JSONResponse(
        status_code=exc.status_code,
        content=build_error_envelope(
            code=code,
            message=message,
            request_id=request_id,
            details=details,
        ),
        headers={"X-Request-ID": request_id, **(exc.headers or {})},
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    sanitized_errors = [_sanitize_validation_error(e) for e in exc.errors()]

    return JSONResponse(
        status_code=422,
        content=build_error_envelope(
            code="validation_error",
            message="Request validation failed",
            request_id=request_id,
            details={"errors": sanitized_errors},
        ),
        headers={"X-Request-ID": request_id},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error("unhandled_exception", error=str(exc), request_id=request_id)

    return JSONResponse(
        status_code=500,
        content=build_error_envelope(
            code="internal",
            message="Internal server error",
            request_id=request_id,
        ),
        headers={"X-Request-ID": request_id},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register all standard error handlers on the FastAPI application."""
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
