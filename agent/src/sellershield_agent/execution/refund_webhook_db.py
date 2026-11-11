"""Short-transaction database handler for store refund.updated webhooks."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, Engine, select

from sellershield_agent.audit.models import ActorType, EventType, Outcome
from sellershield_agent.clock import Clock
from sellershield_agent.db import write_tx
from sellershield_agent.domain.state import (
    PROPOSAL_TRANSITIONS,
    ApprovalStatus,
    ProposalStatus,
    approvals,
    client_events,
    executions,
    generate_execution_id,
    proposals,
    transition_proposal,
)
from sellershield_agent.events.emit import emit_client_event
from sellershield_agent.execution.common import format_utc_iso, step_idempotency_key
from sellershield_agent.execution.db_ops import (
    append_audit_in_tx,
    insert_execution_step_in_tx,
    update_approval_metadata_in_tx,
    update_execution_step_in_tx,
)
from sellershield_agent.memory.writer import record_memory_event_in_tx
from sellershield_agent.outbox.enqueue import enqueue_notification

_SAFE_REFUND_MESSAGES: dict[str, tuple[str, str]] = {
    "processing": ("refund.processing", "Your refund is being processed."),
    "completed": ("refund.completed", "Your refund has been completed."),
    "failed": (
        "refund.failed",
        "Your refund could not be processed. No replacement refund was created; "
        "please ask support for assistance.",
    ),
}


def _emit_refund_client_event_in_tx(
    tx: Connection,
    *,
    customer_id: str,
    refund_id: str,
    order_id: str | None,
    return_id: str | None,
    proposal_id: str | None,
    status_val: str,
    now_iso: str,
) -> None:
    if not customer_id or status_val not in _SAFE_REFUND_MESSAGES:
        return
    existing = tx.execute(
        select(client_events.c.payload_json).where(
            client_events.c.customer_id == customer_id,
            client_events.c.type == "refund.updated",
        )
    ).fetchall()
    for row in existing:
        try:
            parsed = json.loads(str(row.payload_json))
        except Exception:
            continue
        if (
            isinstance(parsed, dict)
            and parsed.get("refund_id") == refund_id
            and parsed.get("status") == status_val
        ):
            return

    msg_key, msg_text = _SAFE_REFUND_MESSAGES[status_val]
    payload: dict[str, Any] = {
        "refund_id": refund_id,
        "order_id": order_id,
        "return_id": return_id,
        "proposal_id": proposal_id,
        "status": status_val,
        "message_key": msg_key,
        "message": msg_text,
    }
    emit_client_event(tx, customer_id, "refund.updated", payload, now_iso)
    enqueue_notification(
        tx,
        customer_id=customer_id,
        template="refund.updated",
        payload=payload,
        dedupe_key=f"refund.updated:{refund_id}:{status_val}",
        now_iso=now_iso,
    )


def _is_regression(curr_refund_status: str | None, rel_status: str | None, new_status: str) -> bool:
    if rel_status == "failed" and new_status in ("processing", "completed"):
        return True
    if curr_refund_status == "failed" and new_status in ("processing", "completed"):
        return True
    if curr_refund_status == "completed" and new_status == "processing":
        return True
    return False


def apply_refund_updated_sync(
    engine: Engine,
    *,
    refund_id: str,
    status_val: str,
    order_id: str | None,
    return_id: str | None,
    clock: Clock,
    request_id: str,
) -> None:
    """Apply store refund.updated webhook to executions, approvals, proposals, and client_events."""
    now_iso = format_utc_iso(clock.now())
    with write_tx(engine) as tx:
        pid: str | None = None
        for step_name in ("release_refund", "reserve_refund"):
            step_match = tx.execute(
                select(executions.c.proposal_id)
                .where(executions.c.step == step_name, executions.c.store_ref == refund_id)
                .limit(1)
            ).fetchone()
            if step_match is not None:
                pid = str(step_match.proposal_id)
                break
        if pid is None:
            apr_match = tx.execute(
                select(approvals.c.proposal_id).where(approvals.c.refund_id == refund_id).limit(1)
            ).fetchone()
            if apr_match is not None:
                pid = str(apr_match.proposal_id)

        if pid is None:
            append_audit_in_tx(
                tx,
                clock=clock,
                request_id=request_id,
                session_id=None,
                actor_type=ActorType.SYSTEM,
                actor_id="webhook_refund",
                event_type=EventType.EXECUTION_STEP,
                resource_type="refunds",
                resource_id=refund_id[:64],
                action=f"refund_webhook_{status_val}_unmatched",
                outcome=Outcome.SUCCESS,
                policy_version=None,
                details={"refund_id": refund_id, "status": status_val},
            )
            return

        prop_row = tx.execute(select(proposals).where(proposals.c.proposal_id == pid)).fetchone()
        apr_row = tx.execute(select(approvals).where(approvals.c.proposal_id == pid)).fetchone()
        rel_row = tx.execute(
            select(executions).where(
                executions.c.proposal_id == pid, executions.c.step == "release_refund"
            )
        ).fetchone()

        curr_rfd_status = (
            str(apr_row.refund_status)
            if apr_row is not None and apr_row.refund_status is not None
            else None
        )
        rel_status = str(rel_row.status) if rel_row is not None else None
        if _is_regression(curr_rfd_status, rel_status, status_val):
            append_audit_in_tx(
                tx,
                clock=clock,
                request_id=request_id,
                session_id=str(prop_row.session_id) if prop_row else None,
                actor_type=ActorType.SYSTEM,
                actor_id="webhook_refund",
                event_type=EventType.EXECUTION_STEP,
                resource_type="proposals",
                resource_id=pid,
                action="refund_webhook_regression_ignored",
                outcome=Outcome.DENIED,
                policy_version=str(prop_row.policy_version) if prop_row else None,
                details={"refund_id": refund_id, "status": status_val},
            )
            return

        customer_id = str(prop_row.customer_id) if prop_row is not None else ""
        session_id = str(prop_row.session_id) if prop_row is not None else None
        policy_version = str(prop_row.policy_version) if prop_row is not None else None
        resolved_ret_id = return_id or (
            str(apr_row.return_id) if apr_row and apr_row.return_id else None
        )
        resolved_ord_id = order_id or (str(apr_row.order_id) if apr_row else None)

        if status_val in ("processing", "completed"):
            approval_ok = apr_row is None or str(apr_row.status) == ApprovalStatus.approved.value
            if not approval_ok or rel_row is None:
                append_audit_in_tx(
                    tx,
                    clock=clock,
                    request_id=request_id,
                    session_id=session_id,
                    actor_type=ActorType.SYSTEM,
                    actor_id="webhook_refund",
                    event_type=EventType.EXECUTION_STEP,
                    resource_type="proposals",
                    resource_id=pid,
                    action=f"refund_webhook_{status_val}_blocked",
                    outcome=Outcome.DENIED,
                    policy_version=policy_version,
                    details={"refund_id": refund_id, "status": status_val},
                )
                return

            if str(rel_row.status) != "succeeded":
                update_execution_step_in_tx(
                    tx,
                    execution_id=str(rel_row.execution_id),
                    values={
                        "status": "succeeded",
                        "store_ref": refund_id,
                        "error_code": None,
                        "finished_at": now_iso,
                        "next_retry_at": None,
                    },
                )
            if apr_row is not None:
                update_approval_metadata_in_tx(
                    tx,
                    proposal_id=pid,
                    values={"refund_status": status_val, "last_error": None},
                )
            if status_val == "completed" and prop_row is not None:
                record_memory_event_in_tx(
                    tx,
                    customer_id=str(prop_row.customer_id),
                    event_type="refund.completed",
                    ref_id=refund_id,
                    outcome="completed",
                    occurred_at=now_iso,
                    order_id=resolved_ord_id,
                )
                curr_st = str(prop_row.status)
                if ProposalStatus.succeeded.value in PROPOSAL_TRANSITIONS.get(curr_st, set()):
                    transition_proposal(
                        tx,
                        proposal_id=pid,
                        to_status=ProposalStatus.succeeded,
                        expected_version=int(prop_row.version),
                        clock=clock,
                        request_id=request_id,
                        actor_id="webhook_refund",
                        actor_type=ActorType.SYSTEM.value,
                    )

        elif status_val == "failed":
            if rel_row is not None:
                update_execution_step_in_tx(
                    tx,
                    execution_id=str(rel_row.execution_id),
                    values={
                        "status": "failed",
                        "store_ref": refund_id,
                        "error_code": "store_refund_failed",
                        "finished_at": now_iso,
                        "next_retry_at": None,
                    },
                )
            else:
                insert_execution_step_in_tx(
                    tx,
                    values={
                        "execution_id": generate_execution_id(),
                        "proposal_id": pid,
                        "step": "release_refund",
                        "idempotency_key": step_idempotency_key(pid, "release_refund"),
                        "status": "failed",
                        "store_ref": refund_id,
                        "error_code": "store_refund_failed",
                        "started_at": now_iso,
                        "finished_at": now_iso,
                        "attempts": 1,
                        "next_retry_at": None,
                        "stuck": False,
                    },
                )
            if apr_row is not None:
                update_approval_metadata_in_tx(
                    tx,
                    proposal_id=pid,
                    values={"refund_status": "failed", "last_error": "store_refund_failed"},
                )
            if prop_row is not None:
                record_memory_event_in_tx(
                    tx,
                    customer_id=str(prop_row.customer_id),
                    event_type="proposal.failed",
                    ref_id=refund_id,
                    outcome="failed",
                    occurred_at=now_iso,
                    order_id=resolved_ord_id,
                )
                curr_st = str(prop_row.status)
                if ProposalStatus.failed.value in PROPOSAL_TRANSITIONS.get(curr_st, set()):
                    transition_proposal(
                        tx,
                        proposal_id=pid,
                        to_status=ProposalStatus.failed,
                        expected_version=int(prop_row.version),
                        clock=clock,
                        request_id=request_id,
                        actor_id="webhook_refund",
                        actor_type=ActorType.SYSTEM.value,
                    )

        _emit_refund_client_event_in_tx(
            tx,
            customer_id=customer_id,
            refund_id=refund_id,
            order_id=resolved_ord_id,
            return_id=resolved_ret_id,
            proposal_id=pid,
            status_val=status_val,
            now_iso=now_iso,
        )

        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=session_id,
            actor_type=ActorType.SYSTEM,
            actor_id="webhook_refund",
            event_type=EventType.EXECUTION_STEP,
            resource_type="proposals",
            resource_id=pid,
            action=f"refund_webhook_{status_val}",
            outcome=Outcome.FAILED if status_val == "failed" else Outcome.SUCCESS,
            policy_version=policy_version,
            details={
                "refund_id": refund_id,
                "return_id": resolved_ret_id,
                "status": status_val,
            },
        )
