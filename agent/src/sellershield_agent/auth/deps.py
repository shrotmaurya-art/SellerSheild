"""FastAPI dependency injection providers for authentication and authorization."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Request

from sellershield_agent.api.ratelimit import ip_auth_failure_limiter
from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth.jwks import JWKSClient
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.auth.verify import verify_token
from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, write_tx
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.logging import get_logger

logger = get_logger("sellershield_agent.auth.deps")

_default_jwks_client: JWKSClient | None = None


def record_auth_denied(
    request: Request | None = None,
    reason: str = "denied",
    status_code: int = 401,
    principal: Principal | None = None,
    clock: Clock | None = None,
    request_id: str | None = None,
    path: str | None = None,
) -> None:
    """Record an immutable auth_denied audit event inside write_tx."""
    resolved_path = path or (request.url.path if request is not None else "/unknown")
    if request is not None and status_code == 401:
        client_ip = request.client.host if request.client else "127.0.0.1"
        ip_auth_failure_limiter.record_attempt(client_ip)

    engine = get_engine()
    resolved_req_id = (
        request_id
        or (getattr(request.state, "request_id", None) if request is not None else None)
        or f"req-{uuid.uuid4().hex[:12]}"
    )
    now_clock = clock or SystemClock()
    now_ts = now_clock.now().isoformat()

    actor_type = ActorType.SYSTEM
    actor_id: str | None = None
    if principal is not None:
        actor_id = principal.user_id or principal.customer_id
        if principal.role == Role.customer:
            actor_type = ActorType.CUSTOMER
        elif principal.role in (Role.seller_staff, Role.seller_owner):
            actor_type = ActorType.SELLER
    elif "/seller/" in resolved_path:
        actor_type = ActorType.SELLER
    elif "/chat/" in resolved_path or "/me/" in resolved_path:
        actor_type = ActorType.CUSTOMER

    details = {
        "reason": reason,
        "status_code": status_code,
        "path": resolved_path,
    }

    event = AuditEvent(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        ts_utc=now_ts,
        request_id=resolved_req_id,
        session_id=None,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=EventType.AUTH_DENIED,
        resource_type="endpoint",
        resource_id=resolved_path,
        action="auth_denied",
        outcome=Outcome.DENIED,
        details=details,
    )

    with write_tx(engine) as conn:
        try:
            AuditService.append(conn, event)
        except Exception as exc:
            from sellershield_agent.dev.faults import DevFaultAuditError

            if isinstance(exc, DevFaultAuditError):
                logger.warning("auth_denied_audit_skipped_under_fault", reason=reason)
            else:
                raise


def reset_default_jwks_client() -> None:
    """Reset the singleton JWKS client (primarily for testing)."""
    global _default_jwks_client
    if _default_jwks_client is not None:
        _default_jwks_client.close()
    _default_jwks_client = None


def get_clock() -> Clock:
    """Dependency providing the active application Clock."""
    return SystemClock()


def get_default_jwks_client() -> JWKSClient:
    """Singleton getter for JWKSClient."""
    global _default_jwks_client
    if _default_jwks_client is None:
        settings = get_settings()
        _default_jwks_client = JWKSClient(
            url=settings.STORE_JWKS_URL,
            dev_mode=settings.DEV_MODE,
        )
    return _default_jwks_client


def get_jwks_client() -> JWKSClient:
    """Dependency providing JWKSClient instance."""
    return get_default_jwks_client()


def get_principal(
    request: Request,
    jwks_client: Annotated[JWKSClient, Depends(get_jwks_client)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> Principal:
    """Extract, validate, and verify the Bearer token from Authorization header."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    if ip_auth_failure_limiter.is_exceeded(client_ip, limit=20, window_seconds=60.0):
        logger.warning("auth_rate_limited", client_ip=client_ip)
        raise AppHTTPException(
            status_code=429,
            code="rate_limited",
            message="Too many failed authentication attempts from this IP",
            details={"retry_after_seconds": 60},
        )

    raw_auth_headers = [v for k, v in request.headers.raw if k.lower() == b"authorization"]
    if len(raw_auth_headers) > 1:
        logger.warning("auth_failed", reason="duplicate_authorization_headers")
        record_auth_denied(
            request, reason="duplicate_authorization_headers", status_code=401, clock=clock
        )
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="duplicate_authorization_headers",
        )

    auth_header = request.headers.get("authorization")
    if not auth_header or not auth_header.strip():
        logger.warning("auth_failed", reason="missing_authorization_header")
        record_auth_denied(
            request, reason="missing_authorization_header", status_code=401, clock=clock
        )
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="missing_authorization_header",
        )

    try:
        auth_header.encode("ascii")
    except UnicodeEncodeError:
        logger.warning("auth_failed", reason="non_ascii_authorization_header")
        record_auth_denied(
            request, reason="non_ascii_authorization_header", status_code=401, clock=clock
        )
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="non_ascii_authorization_header",
        ) from None

    parts = auth_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        logger.warning("auth_failed", reason="malformed_authorization_header")
        record_auth_denied(
            request, reason="malformed_authorization_header", status_code=401, clock=clock
        )
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="malformed_authorization_header",
        )

    token = parts[1].strip()
    if not token:
        logger.warning("auth_failed", reason="empty_bearer_token")
        record_auth_denied(request, reason="empty_bearer_token", status_code=401, clock=clock)
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="empty_bearer_token",
        )

    try:
        principal = verify_token(token, jwks_client=jwks_client, clock=clock)
    except AppHTTPException as exc:
        reason = getattr(exc, "reason", None) or "token_verification_failed"
        record_auth_denied(request, reason=reason, status_code=exc.status_code, clock=clock)
        raise

    request.state.principal = principal
    return principal


def require_customer(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Require caller to have customer role."""
    if principal.role != Role.customer:
        logger.warning(
            "authz_failed",
            reason="role_mismatch",
            required="customer",
            actual=principal.role,
        )
        record_auth_denied(request, reason="role_mismatch", status_code=403, principal=principal)
        raise AppHTTPException(
            status_code=403,
            code="forbidden",
            message="Access forbidden",
            reason="role_mismatch",
        )
    return principal


def require_seller(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Require caller to have seller_staff or seller_owner role."""
    if principal.role not in (Role.seller_staff, Role.seller_owner):
        logger.warning(
            "authz_failed",
            reason="role_mismatch",
            required="seller",
            actual=principal.role,
        )
        record_auth_denied(request, reason="role_mismatch", status_code=403, principal=principal)
        raise AppHTTPException(
            status_code=403,
            code="forbidden",
            message="Access forbidden",
            reason="role_mismatch",
        )
    return principal


def require_owner(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Require caller to have seller_owner role."""
    if principal.role != Role.seller_owner:
        logger.warning(
            "authz_failed",
            reason="role_mismatch",
            required="seller_owner",
            actual=principal.role,
        )
        record_auth_denied(request, reason="role_mismatch", status_code=403, principal=principal)
        raise AppHTTPException(
            status_code=403,
            code="forbidden",
            message="Access forbidden",
            reason="role_mismatch",
        )
    return principal


class RequireFreshAuth:
    """Callable dependency enforcing auth_time within max_age_seconds (step-up)."""

    def __init__(self, max_age_seconds: int = 900) -> None:
        self.max_age_seconds = max_age_seconds

    def __call__(
        self,
        request: Request,
        principal: Annotated[Principal, Depends(get_principal)],
        clock: Annotated[Clock, Depends(get_clock)],
    ) -> Principal:
        if principal.auth_time is None:
            logger.warning("authz_step_up_missing_auth_time", user_id=principal.user_id)
            record_auth_denied(
                request,
                reason="authz_step_up_missing_auth_time",
                status_code=401,
                principal=principal,
                clock=clock,
            )
            raise AppHTTPException(
                status_code=401,
                code="step_up_required",
                message="Action requires recent authentication within 15 minutes",
                details={"auth_time_required_seconds": self.max_age_seconds},
                reason="authz_step_up_missing_auth_time",
            )

        now_ts = int(clock.now().timestamp())
        age = now_ts - principal.auth_time
        if age > self.max_age_seconds or age < -30:
            logger.warning(
                "authz_step_up_stale",
                user_id=principal.user_id,
                age_seconds=age,
                max_age_seconds=self.max_age_seconds,
            )
            record_auth_denied(
                request,
                reason="authz_step_up_stale",
                status_code=401,
                principal=principal,
                clock=clock,
            )
            raise AppHTTPException(
                status_code=401,
                code="step_up_required",
                message="Action requires recent authentication within 15 minutes",
                details={"auth_time_required_seconds": self.max_age_seconds},
                reason="authz_step_up_stale",
            )

        return principal


def require_fresh_auth(max_age_seconds: int = 900) -> RequireFreshAuth:
    """Factory returning a RequireFreshAuth dependency callable."""
    return RequireFreshAuth(max_age_seconds=max_age_seconds)


def require_dev_mode(request: Request) -> None:
    """Require DEV_MODE to be true, otherwise return 404 before auth/JWKS."""
    settings = get_settings()
    if not settings.DEV_MODE:
        raise AppHTTPException(
            status_code=404,
            code="not_found",
            message="Endpoint not found",
            reason="not_found",
        )
