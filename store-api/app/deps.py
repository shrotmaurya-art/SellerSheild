"""
FastAPI dependencies for authentication and authorization.

- get_current_principal: extracts the principal from the request (returns dict or None)
- require_auth: raises 401 if no principal
- require_customer: raises 403 if principal is not a customer
- require_seller_or_monitor: seller_owner, seller_staff, or agent-monitor with orders:read_all
- require_role(*roles): raises 403 if principal role not in allowed roles
- require_scope(*scopes): raises 403 if service principal lacks required scopes
"""

from typing import Any

from fastapi import Depends, Request
from sqlalchemy.orm import Session

import db
from app.audit import log_audit
from app.auth import get_current_principal
from app.errors import AppException


def require_auth(request: Request) -> dict[str, Any]:
    """Require a valid principal. Raises 401 if missing or invalid."""
    principal = get_current_principal(request)
    if principal is None:
        raise AppException(
            code="unauthorized",
            message="Authentication required",
            status_code=401,
        )
    return principal


def require_customer(request: Request) -> dict[str, Any]:
    """Require an authenticated customer."""
    principal = require_auth(request)
    is_customer = (
        principal.get("type") == "user"
        and principal.get("role") == "customer"
        and bool(principal.get("customer_id"))
    )
    if not is_customer:
        request_id = getattr(request.state, "request_id", None) or request.headers.get(
            "X-Request-ID", "unknown"
        )
        actor = principal.get("sub") or principal.get("customer_id") or "unknown"
        with Session(db.engine) as session:
            log_audit(
                session=session,
                request_id=request_id,
                actor=actor,
                action="read",
                resource=request.url.path,
                outcome="denied",
            )
        raise AppException(
            code="forbidden",
            message="Customer role required",
            status_code=403,
        )
    return principal


def require_seller_or_monitor(request: Request) -> dict[str, Any]:
    """
    Require seller_owner, seller_staff, or service token with orders:read_all scope.
    Logs denied access to store_audit on 403.
    """
    principal = require_auth(request)
    is_seller = principal.get("type") == "user" and principal.get("role") in (
        "seller_owner",
        "seller_staff",
    )
    is_monitor = principal.get("type") == "service" and "orders:read_all" in principal.get(
        "scopes", []
    )

    if not (is_seller or is_monitor):
        request_id = getattr(request.state, "request_id", None) or request.headers.get(
            "X-Request-ID", "unknown"
        )
        actor = principal.get("sub") or principal.get("customer_id") or "unknown"
        with Session(db.engine) as session:
            log_audit(
                session=session,
                request_id=request_id,
                actor=actor,
                action="read",
                resource=request.url.path,
                outcome="denied",
            )
        raise AppException(
            code="forbidden",
            message="Seller access or orders:read_all scope required",
            status_code=403,
        )
    return principal


def require_seller_or_executor(request: Request) -> dict[str, Any]:
    """
    Require seller_owner, seller_staff, or agent-executor service token.
    Logs denied access to store_audit on 403.
    """
    principal = require_auth(request)
    is_seller = principal.get("type") == "user" and principal.get("role") in (
        "seller_owner",
        "seller_staff",
    )
    is_executor = (
        principal.get("type") == "service"
        and principal.get("client_id") == "agent-executor"
    )
    if not (is_seller or is_executor):
        request_id = getattr(request.state, "request_id", None) or request.headers.get(
            "X-Request-ID", "unknown"
        )
        actor = principal.get("sub") or principal.get("customer_id") or "unknown"
        with Session(db.engine) as session:
            log_audit(
                session=session,
                request_id=request_id,
                actor=actor,
                action="write",
                resource=request.url.path,
                outcome="denied",
            )
        raise AppException(
            code="forbidden",
            message="Seller or agent-executor access required",
            status_code=403,
        )
    return principal


def require_owner_or_monitor(request: Request) -> dict[str, Any]:
    """Require seller_owner or a service token with orders:read_all scope (audits 403)."""
    principal = require_auth(request)
    is_owner = principal.get("type") == "user" and principal.get("role") == "seller_owner"
    is_monitor = principal.get("type") == "service" and "orders:read_all" in principal.get(
        "scopes", []
    )
    if not (is_owner or is_monitor):
        request_id = getattr(request.state, "request_id", None) or request.headers.get(
            "X-Request-ID", "unknown"
        )
        actor = principal.get("sub") or principal.get("customer_id") or "unknown"
        with Session(db.engine) as session:
            log_audit(
                session=session,
                request_id=request_id,
                actor=actor,
                action="read",
                resource=request.url.path,
                outcome="denied",
            )
        raise AppException(
            code="forbidden",
            message="Owner access or orders:read_all scope required",
            status_code=403,
        )
    return principal


class RoleChecker:
    """Dependency that checks the principal has one of the allowed roles."""

    def __init__(self, *allowed_roles: str) -> None:
        self.allowed_roles = set(allowed_roles)

    def __call__(self, principal: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        # Service tokens don't have a role
        if principal.get("type") == "service":
            raise AppException(
                code="forbidden",
                message="This endpoint requires a user token",
                status_code=403,
            )
        if principal.get("role") not in self.allowed_roles:
            raise AppException(
                code="forbidden",
                message="Insufficient role",
                status_code=403,
            )
        return principal


class ScopeChecker:
    """Dependency that checks the service principal has all required scopes."""

    def __init__(self, *required_scopes: str) -> None:
        self.required_scopes = set(required_scopes)

    def __call__(self, principal: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
        if principal.get("type") != "service":
            raise AppException(
                code="forbidden",
                message="This endpoint requires a service token",
                status_code=403,
            )
        token_scopes = set(principal.get("scopes", []))
        if not self.required_scopes.issubset(token_scopes):
            missing = self.required_scopes - token_scopes
            raise AppException(
                code="forbidden",
                message=f"Missing required scopes: {', '.join(sorted(missing))}",
                status_code=403,
            )
        return principal


# Convenience factories
def require_role(*roles: str) -> RoleChecker:
    """Create a role-checking dependency."""
    return RoleChecker(*roles)


def require_scope(*scopes: str) -> ScopeChecker:
    """Create a scope-checking dependency."""
    return ScopeChecker(*scopes)
