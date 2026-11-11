"""Shared authorization, store error mapping, and actor-key helpers for approvals."""

from __future__ import annotations

import asyncio
import hashlib
from typing import NoReturn

from sqlalchemy import Engine

from sellershield_agent.approvals.common_db import append_audit_sync
from sellershield_agent.approvals.helpers import (
    STEP_UP_FUTURE_SKEW_SECONDS,
    STEP_UP_MAX_AGE_SECONDS,
)
from sellershield_agent.audit.models import ActorType, EventType, Outcome
from sellershield_agent.auth.models import Role
from sellershield_agent.clock import Clock
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.policy import loader as policy_loader
from sellershield_agent.store.client import (
    RequestContext,
    StoreClientError,
    StoreConflict,
    StoreDuplicate,
    StoreForbidden,
    StoreIdempotencyMismatch,
    StoreInvalidState,
    StoreLimitExceeded,
    StoreNotFound,
    StoreTimeout,
    StoreUnavailable,
    StoreValidationError,
)


def seller_actor_key(store_id: str, user_id: str) -> str:
    """Derive a hashed, store-scoped idempotency partition key for a seller principal."""
    digest = hashlib.sha256(f"{store_id}:{user_id}".encode()).hexdigest()[:48]
    return f"slr:{digest}"


def raise_mapped_store_error(exc: BaseException) -> NoReturn:
    """Translate StoreClient exceptions into standard HTTP error envelopes."""
    if isinstance(exc, (StoreTimeout, TimeoutError, StoreUnavailable)):
        raise AppHTTPException(
            status_code=503,
            code="upstream_unavailable",
            message="Store service temporarily unavailable",
        ) from None
    if isinstance(exc, StoreIdempotencyMismatch):
        raise AppHTTPException(
            status_code=409,
            code="idempotency_mismatch",
            message="Idempotency conflict in store service",
        ) from None
    if isinstance(exc, StoreForbidden):
        raise AppHTTPException(
            status_code=403,
            code="forbidden",
            message="Store authorization rejected",
        ) from None
    if isinstance(exc, (StoreInvalidState, StoreConflict, StoreDuplicate, StoreLimitExceeded)):
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message="Store state conflict prevented this action",
        ) from None
    if isinstance(exc, StoreNotFound):
        raise AppHTTPException(
            status_code=404,
            code="not_found",
            message="Referenced order, return, or refund was not found in store",
        ) from None
    if isinstance(exc, StoreValidationError):
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="Store rejected request parameters",
        ) from None
    if isinstance(exc, StoreClientError):
        raise AppHTTPException(
            status_code=502,
            code="upstream_unavailable",
            message="Store service returned an unexpected error",
        ) from None
    raise exc


def resolve_staff_limit(approval_policy_version: str) -> int:
    """Return the stricter staff approval cap across approval and active policies."""
    limits: list[int] = []
    for ver in (approval_policy_version, None):
        try:
            pol = policy_loader.load_policy(ver)
            limits.append(int(pol.staff_approval_limit_paise))
        except (FileNotFoundError, ValueError):
            continue
    return min(limits) if limits else 200000


async def enforce_role_and_step_up(
    *,
    engine: Engine,
    seller_ctx: RequestContext,
    approval_id: str,
    amount_paise: int,
    required_role: str,
    policy_version: str,
    session_id: str | None,
    clock: Clock,
) -> None:
    """Verify seller_owner requirement above staff limit and enforce 15-min fresh auth."""
    staff_limit = resolve_staff_limit(policy_version)
    needs_owner = amount_paise > staff_limit or required_role == Role.seller_owner.value
    if not needs_owner:
        return

    actor_id = seller_ctx.principal.user_id
    if seller_ctx.principal.role != Role.seller_owner:
        await asyncio.to_thread(
            append_audit_sync,
            engine,
            clock=clock,
            request_id=seller_ctx.request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.AUTH_DENIED,
            resource_type="approvals",
            resource_id=approval_id,
            action="auth_denied",
            outcome=Outcome.DENIED,
            policy_version=policy_version,
            details={
                "reason": "staff_limit_exceeded",
                "amount_paise": amount_paise,
                "staff_limit_paise": staff_limit,
                "required_role": Role.seller_owner.value,
            },
        )
        raise AppHTTPException(
            status_code=403,
            code="forbidden",
            message="Seller owner role is required for approvals above the staff limit",
            reason="staff_limit_exceeded",
        )

    auth_time = seller_ctx.principal.auth_time
    now_ts = int(clock.now().timestamp())
    age = (now_ts - auth_time) if auth_time is not None else None
    if age is None or age > STEP_UP_MAX_AGE_SECONDS or age < -STEP_UP_FUTURE_SKEW_SECONDS:
        await asyncio.to_thread(
            append_audit_sync,
            engine,
            clock=clock,
            request_id=seller_ctx.request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.AUTH_DENIED,
            resource_type="approvals",
            resource_id=approval_id,
            action="auth_denied",
            outcome=Outcome.DENIED,
            policy_version=policy_version,
            details={
                "reason": "authz_step_up_required",
                "auth_time_required_seconds": STEP_UP_MAX_AGE_SECONDS,
            },
        )
        raise AppHTTPException(
            status_code=401,
            code="step_up_required",
            message="Action requires recent authentication within 15 minutes",
            details={"auth_time_required_seconds": STEP_UP_MAX_AGE_SECONDS},
            reason="authz_step_up_required",
        )
