"""Shared types, context builders, formatting, and error helpers for execution saga."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import Clock
from sellershield_agent.domain.models import ReturnItem, ReturnStatus
from sellershield_agent.proposals.integrity import PreparedProposal
from sellershield_agent.store.client import (
    ServiceContext,
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

RECONCILE_DELAYS_SECONDS: Final[tuple[int, ...]] = (5, 30, 120)

EXECUTOR_SCOPES: Final[list[str]] = [
    "orders:read_all",
    "returns:transition",
    "refunds:reserve",
    "refunds:release",
    "mail:send",
]

MESSAGE_KEYS: Final[dict[str, str]] = {
    "executing": "proposal.executing",
    "succeeded": "proposal.succeeded",
    "pending_approval": "proposal.pending_approval",
    "unknown": "proposal.unknown",
    "failed": "proposal.failed",
}


@dataclass(frozen=True)
class SagaPlan:
    """Normalized immutable execution plan derived from a confirmed proposal."""

    proposal_id: str
    customer_id: str
    session_id: str
    proposal_type: str
    decision: str
    order_id: str
    items: list[dict[str, Any]]
    amount_paise: int
    refund_method: str
    resolution: str
    reason_code: str
    reason_text: str
    policy_version: str
    facts_hash: str
    request_id: str


def now_utc_iso(clock: Clock) -> str:
    """Format Clock timestamp as canonical Z-suffixed UTC ISO-8601 string."""
    return format_utc_iso(clock.now())


def format_utc_iso(dt: datetime) -> str:
    """Format datetime as canonical Z-suffixed UTC ISO-8601 string."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(ts_str: str) -> datetime:
    """Parse an ISO-8601 string to timezone-aware UTC datetime."""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def step_idempotency_key(proposal_id: str, step: str) -> str:
    """Build deterministic idempotency key per Spec §6.3: proposal_id + ':' + step."""
    return f"{proposal_id}:{step}"


def build_service_ctx(clock: Clock, request_id: str | None = None) -> ServiceContext:
    """Create a short-lived ServiceContext with zero customer credentials."""
    now = clock.now()
    return ServiceContext(
        principal=Principal(
            user_id="svc_executor",
            role=Role.service,
            customer_id=None,
            auth_time=int(now.timestamp()),
            jti=f"jti-svc-{secrets.token_hex(6)}",
            token_exp=int((now + timedelta(minutes=15)).timestamp()),
        ),
        request_id=request_id or f"req-exec-{secrets.token_hex(6)}",
        token=None,
        scopes=list(EXECUTOR_SCOPES),
    )


def classify_store_error(exc: BaseException) -> tuple[bool, str]:
    """Classify StoreClient errors into (is_transient, error_code)."""
    if isinstance(exc, (StoreTimeout, TimeoutError)):
        return True, "timeout"
    if isinstance(exc, StoreUnavailable):
        return True, "unavailable"
    if isinstance(exc, StoreDuplicate):
        return False, "duplicate"
    if isinstance(exc, StoreLimitExceeded):
        return False, "limit_exceeded"
    if isinstance(exc, StoreConflict):
        return False, "conflict"
    if isinstance(exc, StoreInvalidState):
        return False, "invalid_state"
    if isinstance(exc, StoreIdempotencyMismatch):
        return False, "idempotency_mismatch"
    if isinstance(exc, StoreForbidden):
        return False, "forbidden"
    if isinstance(exc, StoreNotFound):
        return False, "not_found"
    if isinstance(exc, StoreValidationError):
        return False, "validation_error"
    if isinstance(exc, StoreClientError):
        return False, "store_error"
    raise exc


def format_execution_status_message(
    status: str,
    *,
    message_key: str | None = None,
    return_id: str | None = None,
    approval_id: str | None = None,
) -> str:
    """Return a truthful, customer-safe status message without internal limit leaks."""
    if status == "succeeded":
        if return_id:
            return f"Your return request {return_id} has been created and approved."
        return "Your request has been confirmed and completed."

    if status == "pending_approval":
        if return_id and approval_id:
            return f"Your return request {return_id} is pending seller review ({approval_id})."
        if approval_id:
            return f"Your refund request is pending seller review ({approval_id})."
        return "Your request is pending seller review."

    if status in ("unknown", "executing"):
        return "We are verifying this request with the store. Please wait for confirmation."

    if message_key == "proposal.failed_duplicate":
        if return_id:
            return (
                f"An active return request ({return_id}) already exists for this item, "
                "so no duplicate request was placed."
            )
        return (
            "An active return request already exists for this item, "
            "so no duplicate request was placed."
        )

    return (
        "We could not complete your request with the store right now, "
        "and no changes were finalized."
    )


def plan_from_prepared(prepared: PreparedProposal, *, request_id: str) -> SagaPlan:
    """Convert an in-memory PreparedProposal into a SagaPlan."""
    return SagaPlan(
        proposal_id=prepared.proposal_id,
        customer_id=prepared.customer_id,
        session_id=prepared.session_id,
        proposal_type=prepared.proposal_type,
        decision=prepared.decision.lower(),
        order_id=prepared.order_id,
        items=[dict(it) for it in prepared.items],
        amount_paise=int(prepared.canonical_amounts["total_paise"]),
        refund_method=prepared.method,
        resolution=prepared.resolution,
        reason_code=prepared.reason_code,
        reason_text=prepared.sanitized_reason_text,
        policy_version=prepared.policy_version,
        facts_hash=prepared.stored_facts_hash,
        request_id=request_id,
    )


def _extract_item_qty(item: dict[str, Any]) -> int:
    raw = item.get("quantity")
    if raw is None:
        raw = item.get("qty", 1)
    return int(raw) if raw is not None else 1


def load_saga_plan_from_row(row: Any, *, request_id: str | None = None) -> SagaPlan:
    """Reconstruct a SagaPlan from a persisted proposals DB row."""
    payload = json.loads(str(row.payload_json))
    amounts = json.loads(str(row.amounts_json))
    raw_items = payload.get("items", [])
    norm_items: list[dict[str, Any]] = [
        {"item_id": str(it["item_id"]), "quantity": _extract_item_qty(it)}
        for it in raw_items
        if isinstance(it, dict) and "item_id" in it
    ]
    return SagaPlan(
        proposal_id=str(row.proposal_id),
        customer_id=str(row.customer_id),
        session_id=str(getattr(row, "session_id", None) or "default"),
        proposal_type=str(row.type),
        decision=str(row.decision).lower(),
        order_id=str(payload["order_id"]),
        items=norm_items,
        amount_paise=int(amounts["total_paise"]),
        refund_method=str(payload.get("refund_method", "original")),
        resolution=str(payload.get("resolution", "refund")),
        reason_code=str(payload.get("reason_code", "changed_mind")),
        reason_text=str(payload.get("reason_text", "")),
        policy_version=str(row.policy_version),
        facts_hash=str(getattr(row, "facts_hash", "") or ""),
        request_id=request_id or f"req-rec-{secrets.token_hex(6)}",
    )


ACTIVE_RETURN_STATUSES: Final[frozenset[ReturnStatus]] = frozenset(
    {
        ReturnStatus.requested,
        ReturnStatus.approved,
        ReturnStatus.pickup_scheduled,
        ReturnStatus.in_transit_back,
        ReturnStatus.received,
        ReturnStatus.inspected_ok,
        ReturnStatus.inspection_failed,
        ReturnStatus.completed,
    }
)


def to_return_items(raw_items: list[dict[str, Any]]) -> list[ReturnItem]:
    """Convert normalized proposal items ('quantity' or 'qty') into Store ReturnItem models."""
    return [
        ReturnItem(
            item_id=str(it["item_id"]),
            qty=int(it.get("qty", it.get("quantity", 1))),
        )
        for it in raw_items
    ]
