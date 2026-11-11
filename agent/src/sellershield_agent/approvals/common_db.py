"""Shared synchronous DB transaction helpers for approval decisions and withdraw."""

from __future__ import annotations

import json
import secrets
from typing import Any

from sqlalchemy import Connection, Engine, select

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.clock import Clock
from sellershield_agent.db import read_tx, write_tx
from sellershield_agent.domain.models import CustomerRiskFacts
from sellershield_agent.domain.state import (
    PROPOSAL_TRANSITIONS,
    ProposalStatus,
    approvals,
    client_events,
    http_idempotency_keys,
    proposals,
    transition_proposal,
)
from sellershield_agent.events.emit import emit_client_event
from sellershield_agent.execution.common import format_utc_iso
from sellershield_agent.outbox.enqueue import enqueue_notification
from sellershield_agent.proposals.integrity import (
    check_idempotency_row,
    load_proposal_decision_snapshot,
)


def emit_approval_decided_event_in_tx(
    tx: Connection,
    *,
    customer_id: str,
    approval_id: str,
    proposal_id: str,
    order_id: str,
    return_id: str | None,
    refund_id: str | None,
    status: str,
    message: str,
    now_iso: str,
) -> None:
    """Emit idempotent approval.decided client_event inside an open write transaction."""
    if not customer_id:
        return
    existing = tx.execute(
        select(client_events.c.payload_json).where(
            client_events.c.customer_id == customer_id,
            client_events.c.type == "approval.decided",
        )
    ).fetchall()
    for row in existing:
        try:
            parsed = json.loads(str(row.payload_json))
        except Exception:
            continue
        if isinstance(parsed, dict) and parsed.get("approval_id") == approval_id:
            return

    payload: dict[str, Any] = {
        "approval_id": approval_id,
        "proposal_id": proposal_id,
        "order_id": order_id,
        "return_id": return_id,
        "refund_id": refund_id,
        "status": status,
        "message_key": f"approval.{status}",
        "message": message,
    }
    emit_client_event(tx, customer_id, "approval.decided", payload, now_iso)
    enqueue_notification(
        tx,
        customer_id=customer_id,
        template="approval.decided",
        payload=payload,
        dedupe_key=f"approval.decided:{approval_id}",
        now_iso=now_iso,
    )


def transition_parent_proposal_if_possible(
    tx: Connection,
    *,
    proposal_id: str,
    to_status: ProposalStatus,
    clock: Clock,
    request_id: str,
    actor_id: str,
    actor_type: str,
) -> None:
    """Advance parent proposal to terminal status when valid under the state machine."""
    prop_row = tx.execute(
        select(proposals).where(proposals.c.proposal_id == proposal_id)
    ).fetchone()
    if prop_row is None:
        return
    curr_status = str(prop_row.status)
    if to_status.value in PROPOSAL_TRANSITIONS.get(curr_status, set()):
        transition_proposal(
            tx,
            proposal_id=proposal_id,
            to_status=to_status,
            expected_version=int(prop_row.version),
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            actor_type=actor_type,
        )


def load_approval_for_decision_sync(
    engine: Engine,
    approval_id: str,
) -> tuple[Any | None, Any | None, CustomerRiskFacts | None, int, dict[str, Any] | None]:
    """Read approval, proposal, and stored snapshot in read_tx."""
    with read_tx(engine) as conn:
        apr_row = conn.execute(
            select(approvals).where(approvals.c.approval_id == approval_id)
        ).fetchone()
        if apr_row is None:
            return None, None, None, 0, None
        prop_row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == apr_row.proposal_id)
        ).fetchone()
        if prop_row is None:
            return apr_row, None, None, 0, None
        fallback_risk, prev_refunds, snapshot = load_proposal_decision_snapshot(
            conn,
            customer_id=str(prop_row.customer_id),
            order_id=str(apr_row.order_id),
            proposal_id=str(apr_row.proposal_id),
        )
        return apr_row, prop_row, fallback_risk, prev_refunds, snapshot


def check_idempotency_sync(
    engine: Engine,
    *,
    actor_key: str,
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any] | None:
    """Read HTTP idempotency cache record cleanly in read_tx."""
    with read_tx(engine) as conn:
        return check_idempotency_row(
            conn,
            customer_id=actor_key,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )


def save_idempotency_sync(
    engine: Engine,
    *,
    actor_key: str,
    idempotency_key: str,
    operation: str,
    target_id: str,
    request_hash: str,
    response_dict: dict[str, Any],
    clock: Clock,
) -> None:
    """Persist HTTP idempotency response payload if not already recorded."""
    now_iso = format_utc_iso(clock.now())
    with write_tx(engine) as tx:
        existing = tx.execute(
            select(http_idempotency_keys.c.id).where(
                http_idempotency_keys.c.customer_id == actor_key,
                http_idempotency_keys.c.idempotency_key == idempotency_key,
            )
        ).fetchone()
        if existing is not None:
            return
        tx.execute(
            http_idempotency_keys.insert().values(
                customer_id=actor_key,
                idempotency_key=idempotency_key,
                operation=operation,
                proposal_id=target_id,
                request_hash=request_hash,
                status_code=200,
                response_json=json.dumps(response_dict, sort_keys=True, separators=(",", ":")),
                created_at=now_iso,
            )
        )
        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=now_iso,
                request_id=None,
                session_id=None,
                actor_type=ActorType.SELLER if "seller" in actor_key else ActorType.SYSTEM,
                actor_id=actor_key,
                event_type=(
                    EventType.APPROVAL_DECIDED
                    if "seller" in actor_key
                    else EventType.EXECUTION_STEP
                ),
                resource_type="http_idempotency_keys",
                resource_id=idempotency_key,
                action="save_idempotency",
                outcome=Outcome.SUCCESS,
                policy_version="v3",
                details={"operation": operation, "target_id": target_id},
            ),
        )


def append_audit_sync(
    engine: Engine,
    *,
    clock: Clock,
    request_id: str,
    session_id: str | None,
    actor_type: ActorType,
    actor_id: str | None,
    event_type: EventType,
    resource_type: str,
    resource_id: str,
    action: str,
    outcome: Outcome,
    policy_version: str | None,
    details: dict[str, Any],
) -> None:
    """Append an immutable audit event in a dedicated short write_tx."""
    with write_tx(engine) as tx:
        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=format_utc_iso(clock.now()),
                request_id=request_id,
                session_id=session_id,
                actor_type=actor_type,
                actor_id=actor_id,
                event_type=event_type,
                resource_type=resource_type,
                resource_id=resource_id[:64],
                action=action,
                outcome=outcome,
                policy_version=policy_version,
                details=details,
            ),
        )
