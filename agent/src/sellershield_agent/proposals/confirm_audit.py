"""Shared confirmation/cancellation spec, audit event helpers, and idempotency storage."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from sqlalchemy import Connection, Engine, select

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.clock import Clock
from sellershield_agent.db import write_tx
from sellershield_agent.domain.state import (
    ProposalStatus,
    client_events,
    http_idempotency_keys,
    is_proposal_expired,
    transition_proposal,
)
from sellershield_agent.events.emit import emit_client_event
from sellershield_agent.proposals.integrity import ProposalValidationError

FINAL_MESSAGE_KEYS: dict[str, str] = {
    "executing": "proposal.executing",
    "succeeded": "proposal.succeeded",
    "pending_approval": "proposal.pending_approval",
    "unknown": "proposal.unknown",
    "failed": "proposal.failed",
}


@dataclass(frozen=True)
class ActionSpec:
    """Normalized request parameters shared across confirm/cancel transaction steps."""

    operation: str
    proposal_id: str
    customer_id: str
    idempotency_key: str
    request_hash: str
    expected_hash: str | None
    clock: Clock
    request_id: str
    request_path: str


def now_utc_iso(clock: Clock) -> str:
    """Format current Clock timestamp as canonical Z-suffixed UTC ISO-8601 string."""
    return clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_action_audit(
    tx: Connection,
    spec: ActionSpec,
    *,
    outcome: Outcome,
    details: dict[str, Any],
    row: Any = None,
    session_id: str | None = None,
    policy_version: str | None = None,
) -> None:
    """Append a proposal_confirmed or proposal_cancelled audit event inside tx."""
    action = "proposal_confirmed" if spec.operation == "confirm" else "proposal_cancelled"
    sess = session_id or (
        str(row.session_id) if row is not None and getattr(row, "session_id", None) else None
    )
    pol = policy_version or (
        str(row.policy_version)
        if row is not None and getattr(row, "policy_version", None)
        else None
    )
    AuditService.append(
        tx,
        AuditEvent(
            event_id=f"AUD-{secrets.token_hex(8)}",
            ts_utc=now_utc_iso(spec.clock),
            request_id=spec.request_id,
            session_id=sess,
            actor_type=ActorType.CUSTOMER,
            actor_id=spec.customer_id,
            event_type=(
                EventType.PROPOSAL_CONFIRMED
                if spec.operation == "confirm"
                else EventType.TOOL_CALL
            ),
            resource_type="proposals",
            resource_id=spec.proposal_id[:64],
            action=action,
            outcome=outcome,
            policy_version=pol,
            details=details,
        ),
    )


def record_not_found_audit(engine: Engine, spec: ActionSpec) -> None:
    """Persist AUTH_DENIED audit row for unknown or cross-customer proposal access."""
    with write_tx(engine) as tx:
        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=now_utc_iso(spec.clock),
                request_id=spec.request_id,
                session_id=None,
                actor_type=ActorType.CUSTOMER,
                actor_id=spec.customer_id,
                event_type=EventType.AUTH_DENIED,
                resource_type="proposals",
                resource_id=spec.proposal_id[:64],
                action="proposal_access",
                outcome=Outcome.DENIED,
                details={
                    "reason": "proposal_not_found",
                    "status_code": 404,
                    "path": spec.request_path,
                },
            ),
        )


def _emit_proposal_expired_event_in_tx(
    tx: Connection, spec: ActionSpec, row: Any
) -> None:
    if not spec.customer_id:
        return
    existing = tx.execute(
        select(client_events.c.payload_json).where(
            client_events.c.customer_id == spec.customer_id,
            client_events.c.type == "proposal.expired",
        )
    ).fetchall()
    for ev_row in existing:
        try:
            parsed = json.loads(str(ev_row.payload_json))
        except Exception:
            continue
        if isinstance(parsed, dict) and parsed.get("proposal_id") == spec.proposal_id:
            return

    order_id: str | None = None
    try:
        parsed_pld = json.loads(str(getattr(row, "payload_json", "{}")))
        if isinstance(parsed_pld, dict) and parsed_pld.get("order_id"):
            order_id = str(parsed_pld["order_id"])
    except Exception:
        order_id = None

    emit_client_event(
        tx,
        spec.customer_id,
        "proposal.expired",
        {
            "proposal_id": spec.proposal_id,
            "order_id": order_id,
            "status": "expired",
            "message_key": "proposal.expired",
            "message": "Your proposal expired before confirmation and no changes were made.",
        },
        now_utc_iso(spec.clock),
        clock=spec.clock,
    )


def record_rejection_in_tx(
    tx: Connection, spec: ActionSpec, row: Any, err: ProposalValidationError
) -> None:
    """Expire the row if applicable and append a denied confirm/cancel audit event."""
    if (
        err.reason == "expired"
        and row is not None
        and str(row.status) == ProposalStatus.proposed.value
        and is_proposal_expired(str(row.expires_at), spec.clock)
    ):
        transition_proposal(
            tx,
            proposal_id=spec.proposal_id,
            to_status=ProposalStatus.expired,
            expected_version=int(row.version),
            clock=spec.clock,
            request_id=spec.request_id,
            actor_id=spec.customer_id,
        )
        _emit_proposal_expired_event_in_tx(tx, spec, row)
    append_action_audit(
        tx,
        spec,
        outcome=Outcome.DENIED,
        row=row,
        details={"reason": err.reason or "invalid_state"},
    )


def save_idempotency_row(
    tx: Connection, spec: ActionSpec, resp_dict: dict[str, Any], now_iso: str
) -> None:
    """Persist HTTP idempotency replay record inside tx."""
    tx.execute(
        http_idempotency_keys.insert().values(
            customer_id=spec.customer_id,
            idempotency_key=spec.idempotency_key,
            operation=spec.operation,
            proposal_id=spec.proposal_id,
            request_hash=spec.request_hash,
            status_code=200,
            response_json=json.dumps(resp_dict, sort_keys=True, separators=(",", ":")),
            created_at=now_iso,
        )
    )
