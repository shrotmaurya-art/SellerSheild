"""FastAPI dependency injection providers for authentication and authorization."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from sellershield_agent.auth.jwks import JWKSClient
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.auth.verify import verify_token
from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.config import get_settings
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.logging import get_logger

logger = get_logger("sellershield_agent.auth.deps")

_default_jwks_client: JWKSClient | None = None


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
    raw_auth_headers = [v for k, v in request.headers.raw if k.lower() == b"authorization"]
    if len(raw_auth_headers) > 1:
        logger.warning("auth_failed", reason="duplicate_authorization_headers")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
        )

    auth_header = request.headers.get("authorization")
    if not auth_header or not auth_header.strip():
        logger.warning("auth_failed", reason="missing_authorization_header")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
        )

    try:
        auth_header.encode("ascii")
    except UnicodeEncodeError:
        logger.warning("auth_failed", reason="non_ascii_authorization_header")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
        ) from None

    parts = auth_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        logger.warning("auth_failed", reason="malformed_authorization_header")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
        )

    token = parts[1].strip()
    if not token:
        logger.warning("auth_failed", reason="empty_bearer_token")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
        )

    principal = verify_token(token, jwks_client=jwks_client, clock=clock)
    request.state.principal = principal
    return principal


def require_customer(
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
        raise AppHTTPException(
            status_code=403,
            code="forbidden",
            message="Access forbidden",
        )
    return principal


def require_seller(
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
        raise AppHTTPException(
            status_code=403,
            code="forbidden",
            message="Access forbidden",
        )
    return principal


def require_owner(
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
        raise AppHTTPException(
            status_code=403,
            code="forbidden",
            message="Access forbidden",
        )
    return principal


class RequireFreshAuth:
    """Callable dependency enforcing auth_time within max_age_seconds (step-up)."""

    def __init__(self, max_age_seconds: int = 900) -> None:
        self.max_age_seconds = max_age_seconds

    def __call__(
        self,
        principal: Annotated[Principal, Depends(get_principal)],
        clock: Annotated[Clock, Depends(get_clock)],
    ) -> Principal:
        if principal.auth_time is None:
            logger.warning("authz_step_up_missing_auth_time", user_id=principal.user_id)
            raise AppHTTPException(
                status_code=401,
                code="step_up_required",
                message="Action requires recent authentication within 15 minutes",
                details={"auth_time_required_seconds": self.max_age_seconds},
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
            raise AppHTTPException(
                status_code=401,
                code="step_up_required",
                message="Action requires recent authentication within 15 minutes",
                details={"auth_time_required_seconds": self.max_age_seconds},
            )

        return principal


def require_fresh_auth(max_age_seconds: int = 900) -> RequireFreshAuth:
    """Factory returning a RequireFreshAuth dependency callable."""
    return RequireFreshAuth(max_age_seconds=max_age_seconds)
