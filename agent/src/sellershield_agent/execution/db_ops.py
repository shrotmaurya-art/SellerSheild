"""Synchronous short-transaction database helpers for the execution saga.

Every helper opens either read_tx or write_tx briefly and commits/rolls back before
returning, ensuring no database transaction or write lock is ever held across an await
or store call (AGENTS.md §2.7, KP-01).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, select, update

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.clock import Clock
from sellershield_agent.db import read_tx, write_tx
from sellershield_agent.domain.state import (
    EntityNotFound,
    InvalidTransition,
    ProposalStatus,
    approvals,
    executions,
    generate_execution_id,
    generate_notification_id,
    notifications_outbox,
    proposals,
    transition_proposal,
)
from sellershield_agent.execution.common import SagaPlan, now_utc_iso
from sellershield_agent.proposals.executor import ExecutionResult


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def append_audit_in_tx(
    tx: Connection,
    *,
    clock: Clock | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    actor_type: ActorType | str = ActorType.SYSTEM,
    actor_id: str | None = None,
    event_type: EventType | str = EventType.EXECUTION_STEP,
    resource_type: str = "executions",
    resource_id: str,
    action: str,
    outcome: Outcome | str = Outcome.SUCCESS,
    policy_version: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append an audit event inside the caller's open write transaction."""
    ts_utc = (
        now_utc_iso(clock)
        if clock is not None
        else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    AuditService.append(
        tx,
        AuditEvent(
            event_id=f"AUD-{secrets.token_hex(8)}",
            ts_utc=ts_utc,
            request_id=request_id,
            session_id=session_id,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            outcome=outcome,
            policy_version=policy_version,
            details=details or {},
        ),
    )


def enqueue_outbox_in_tx(
    tx: Connection,
    *,
    dedupe_key: str,
    template: str,
    payload: dict[str, Any],
    now_iso: str,
    customer_id: str | None = None,
    clock: Clock | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    actor_id: str | None = None,
    policy_version: str | None = None,
) -> str | None:
    """Insert an idempotent outbox notification stub inside an open write transaction."""
    import json

    exists = tx.execute(
        select(notifications_outbox.c.notification_id).where(
            notifications_outbox.c.dedupe_key == dedupe_key
        )
    ).fetchone()
    if exists is not None:
        return None
    notif_id = generate_notification_id()
    tx.execute(
        notifications_outbox.insert().values(
            notification_id=notif_id,
            channel="seller_stub",
            customer_id=customer_id,
            template=template,
            payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            dedupe_key=dedupe_key,
            status="queued",
            attempts=0,
            next_attempt_at=None,
            last_error=None,
            created_at=now_iso,
        )
    )
    audit_evt = AuditEvent(
        event_id=f"AUD-{secrets.token_hex(8)}",
        ts_utc=now_iso,
        request_id=request_id,
        session_id=session_id,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        event_type=EventType.NOTIFICATION_STUB,
        resource_type="notifications_outbox",
        resource_id=notif_id,
        action="enqueue_notification_stub",
        outcome=Outcome.SUCCESS,
        policy_version=policy_version,
        details={"template": template, "dedupe_key": dedupe_key},
    )
    AuditService.append(tx, audit_evt)
    return notif_id


def create_approval_in_tx(tx: Connection, **values: Any) -> None:
    """Execution writer helper: insert approval row atomically."""
    tx.execute(approvals.insert().values(**values))


def update_approval_metadata_in_tx(
    tx: Connection,
    *,
    approval_id: str | None = None,
    proposal_id: str | None = None,
    values: dict[str, Any],
) -> None:
    """Execution writer helper: update approval metadata."""
    if approval_id is not None:
        tx.execute(
            update(approvals).where(approvals.c.approval_id == approval_id).values(**values)
        )
    elif proposal_id is not None:
        tx.execute(
            update(approvals).where(approvals.c.proposal_id == proposal_id).values(**values)
        )


def update_execution_step_in_tx(
    tx: Connection,
    execution_id: str,
    values: dict[str, Any],
) -> None:
    """Execution writer helper: update executions step row."""
    tx.execute(
        update(executions).where(executions.c.execution_id == execution_id).values(**values)
    )


def insert_execution_step_in_tx(tx: Connection, values: dict[str, Any]) -> None:
    """Execution writer helper: insert execution step row."""
    tx.execute(executions.insert().values(**values))


def prepare_run_sync(
    engine: Engine,
    plan: SagaPlan,
    *,
    clock: Clock,
    request_id: str,
) -> ExecutionResult | None:
    """Prepare proposal for direct Executor.run() or return an already-terminal result."""
    with write_tx(engine) as tx:
        prop = tx.execute(
            select(proposals).where(proposals.c.proposal_id == plan.proposal_id)
        ).fetchone()
        if prop is None:
            raise EntityNotFound(f"Proposal '{plan.proposal_id}' not found")

        status = str(prop.status)
        version = int(prop.version)
        apr_row = tx.execute(
            select(approvals).where(approvals.c.proposal_id == plan.proposal_id)
        ).fetchone()
        exec_rows = tx.execute(
            select(executions).where(executions.c.proposal_id == plan.proposal_id)
        ).fetchall()
        ret_ref = next(
            (
                str(r.store_ref)
                for r in exec_rows
                if str(r.step) == "create_return"
                and str(r.status) == "succeeded"
                and r.store_ref is not None
            ),
            None,
        )

        rfd_ref = next(
            (
                str(r.store_ref)
                for r in exec_rows
                if str(r.step) == "reserve_refund"
                and str(r.status) == "succeeded"
                and r.store_ref is not None
            ),
            None,
        )

        if status == ProposalStatus.succeeded.value:
            return ExecutionResult(
                status="succeeded",
                return_id=ret_ref,
                approval_id=None,
                refund_id=rfd_ref,
                message_key="proposal.succeeded",
            )
        if status == ProposalStatus.failed.value:
            is_dup = any(
                str(r.step) == "create_return" and str(r.error_code or "") == "duplicate"
                for r in exec_rows
            )
            return ExecutionResult(
                status="failed",
                return_id=None,
                approval_id=None,
                message_key="proposal.failed_duplicate" if is_dup else "proposal.failed",
            )
        if apr_row is not None and status == ProposalStatus.executing.value:
            return ExecutionResult(
                status="pending_approval",
                return_id=str(apr_row.return_id) if apr_row.return_id else ret_ref,
                approval_id=str(apr_row.approval_id),
                refund_id=str(apr_row.refund_id) if apr_row.refund_id else None,
                message_key="proposal.pending_approval",
            )

        now_iso = now_utc_iso(clock)
        if status == ProposalStatus.proposed.value:
            v_conf = transition_proposal(
                tx,
                proposal_id=plan.proposal_id,
                to_status=ProposalStatus.confirmed,
                expected_version=version,
                clock=clock,
                request_id=request_id,
                actor_id=plan.customer_id,
                extra_values={"confirmed_at": now_iso},
            )
            transition_proposal(
                tx,
                proposal_id=plan.proposal_id,
                to_status=ProposalStatus.executing,
                expected_version=v_conf,
                clock=clock,
                request_id=request_id,
                actor_id=plan.customer_id,
            )
        elif status == ProposalStatus.confirmed.value:
            transition_proposal(
                tx,
                proposal_id=plan.proposal_id,
                to_status=ProposalStatus.executing,
                expected_version=version,
                clock=clock,
                request_id=request_id,
                actor_id=plan.customer_id,
            )
        elif status not in (ProposalStatus.executing.value, ProposalStatus.unknown.value):
            raise InvalidTransition(f"Cannot execute proposal in status '{status}'")

        return None


def fetch_proposal_exec_state_sync(
    engine: Engine, proposal_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None, str | None]:
    """Fetch existing executions keyed by step, optional approval row, and proposal status."""
    with read_tx(engine) as conn:
        exec_rows = conn.execute(
            select(executions).where(executions.c.proposal_id == proposal_id)
        ).fetchall()
        apr_row = conn.execute(
            select(approvals).where(approvals.c.proposal_id == proposal_id)
        ).fetchone()
        prop_row = conn.execute(
            select(proposals.c.status).where(proposals.c.proposal_id == proposal_id)
        ).fetchone()
        by_step = {str(r.step): _row_to_dict(r) for r in exec_rows}
        apr_dict = _row_to_dict(apr_row) if apr_row is not None else None
        prop_status = str(prop_row.status) if prop_row is not None else None
        return by_step, apr_dict, prop_status


def start_execution_step_sync(
    engine: Engine,
    *,
    proposal_id: str,
    step: str,
    idempotency_key: str,
    actor_type: ActorType | str,
    actor_id: str,
    policy_version: str,
    session_id: str,
    request_id: str,
    clock: Clock,
) -> tuple[str, str]:
    """Phase (a): insert executing step row + audit atomically, returning (exec_id, started_at)."""
    now_iso = now_utc_iso(clock)
    with write_tx(engine) as tx:
        existing = tx.execute(
            select(executions).where(executions.c.idempotency_key == idempotency_key)
        ).fetchone()
        if existing is None:
            exec_id = generate_execution_id()
            started_at = now_iso
            tx.execute(
                executions.insert().values(
                    execution_id=exec_id,
                    proposal_id=proposal_id,
                    step=step,
                    idempotency_key=idempotency_key,
                    status="executing",
                    store_ref=None,
                    error_code=None,
                    started_at=started_at,
                    finished_at=None,
                    attempts=1,
                    next_retry_at=None,
                    stuck=False,
                )
            )
        else:
            exec_id = str(existing.execution_id)
            started_at = str(existing.started_at)

        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=session_id,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type=EventType.EXECUTION_STEP,
            resource_type="proposals",
            resource_id=proposal_id,
            action="execution_step_started",
            outcome=Outcome.SUCCESS,
            policy_version=policy_version,
            details={
                "execution_id": exec_id,
                "step": step,
                "idempotency_key": idempotency_key,
            },
        )
        return exec_id, started_at


def finish_execution_step_sync(
    engine: Engine,
    *,
    proposal_id: str,
    execution_id: str,
    step: str,
    new_status: str,
    store_ref: str | None,
    error_code: str | None,
    actor_type: ActorType | str,
    actor_id: str,
    policy_version: str,
    session_id: str,
    request_id: str,
    clock: Clock,
    next_retry_at: str | None = None,
    attempts: int | None = None,
    stuck: bool | None = None,
    reconcile_action: str | None = None,
    outbox_stub: dict[str, Any] | None = None,
) -> None:
    """Phase (d): update step result and write audit (plus optional outbox stub) atomically."""
    now_iso = now_utc_iso(clock)
    with write_tx(engine) as tx:
        values: dict[str, Any] = {
            "status": new_status,
            "error_code": error_code,
            "finished_at": now_iso if new_status in ("succeeded", "failed") else None,
        }
        if store_ref is not None:
            values["store_ref"] = store_ref
        if attempts is not None:
            values["attempts"] = attempts
        if stuck is not None:
            values["stuck"] = stuck
        if stuck is True or new_status in ("succeeded", "failed"):
            values["next_retry_at"] = None
        elif next_retry_at is not None:
            values["next_retry_at"] = next_retry_at

        tx.execute(
            update(executions).where(executions.c.execution_id == execution_id).values(**values)
        )

        if outbox_stub is not None:
            enqueue_outbox_in_tx(
                tx,
                dedupe_key=str(outbox_stub["dedupe_key"]),
                template=str(outbox_stub["template"]),
                payload=dict(outbox_stub["payload"]),
                now_iso=now_iso,
                customer_id=outbox_stub.get("customer_id"),
            )

        ev_type = EventType.RECONCILE if reconcile_action else EventType.EXECUTION_STEP
        action = reconcile_action or f"execution_step_{new_status}"
        outcome = Outcome.SUCCESS if new_status == "succeeded" else Outcome.FAILED
        details: dict[str, Any] = {"execution_id": execution_id, "step": step}
        if store_ref is not None:
            details["store_ref"] = store_ref
        if error_code is not None:
            details["error_code"] = error_code
        if stuck is True:
            details["stuck"] = True

        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=session_id,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type=ev_type,
            resource_type="proposals",
            resource_id=proposal_id,
            action=action,
            outcome=outcome,
            policy_version=policy_version,
            details=details,
        )
