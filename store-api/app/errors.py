from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppException(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: Any = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


class NotFoundException(AppException):
    def __init__(self, message: str = "Resource not found", details: Any = None) -> None:
        super().__init__(code="not_found", message=message, status_code=404, details=details)


class ForbiddenException(AppException):
    def __init__(self, message: str = "Access forbidden", details: Any = None) -> None:
        super().__init__(code="forbidden", message=message, status_code=403, details=details)


class InvalidStateException(AppException):
    def __init__(self, message: str = "Invalid transition or state", details: Any = None) -> None:
        super().__init__(code="invalid_state", message=message, status_code=409, details=details)


class LimitExceededException(AppException):
    def __init__(self, message: str = "Limit exceeded", details: Any = None) -> None:
        super().__init__(code="limit_exceeded", message=message, status_code=409, details=details)


class DuplicateException(AppException):
    def __init__(self, message: str = "Duplicate resource", details: Any = None) -> None:
        super().__init__(code="duplicate", message=message, status_code=409, details=details)


class ValidationErrorException(AppException):
    def __init__(self, message: str = "Validation error", details: Any = None) -> None:
        super().__init__(code="validation_error", message=message, status_code=422, details=details)


class UpstreamUnavailableException(AppException):
    def __init__(self, message: str = "Upstream service unavailable", details: Any = None) -> None:
        super().__init__(
            code="upstream_unavailable", message=message, status_code=503, details=details
        )


class IdempotencyMismatchException(AppException):
    def __init__(
        self, message: str = "Idempotency key payload mismatch", details: Any = None
    ) -> None:
        super().__init__(
            code="idempotency_mismatch", message=message, status_code=409, details=details
        )


def _get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID", "")


def make_error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    details: Any = None,
) -> JSONResponse:
    payload = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details,
        }
    }
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers={"X-Request-ID": request_id} if request_id else {},
    )


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    request_id = _get_request_id(request)
    return make_error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        request_id=request_id,
        details=exc.details,
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    request_id = _get_request_id(request)
    code_map = {
        404: "not_found",
        403: "forbidden",
        401: "unauthorized",
        400: "bad_request",
        409: "invalid_state",
        422: "validation_error",
        503: "upstream_unavailable",
    }
    code = code_map.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else "An error occurred"
    return make_error_response(
        status_code=exc.status_code,
        code=code,
        message=message,
        request_id=request_id,
        details=exc.detail if not isinstance(exc.detail, str) else None,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    request_id = _get_request_id(request)
    return make_error_response(
        status_code=422,
        code="validation_error",
        message="Request validation failed",
        request_id=request_id,
        details=exc.errors(),
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = _get_request_id(request)
    # Do not leak internal exception details or stack traces
    return make_error_response(
        status_code=500,
        code="internal_error",
        message="An unexpected server error occurred",
        request_id=request_id,
        details=None,
    )
