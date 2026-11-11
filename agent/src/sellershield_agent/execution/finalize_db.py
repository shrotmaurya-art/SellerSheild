"""Synchronous DB finalization helpers for approvals, client events, and proposals."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import Connection, Engine, select, update

from sellershield_agent.audit.models import ActorType, EventType, Outcome
from sellershield_agent.clock import Clock
from sellershield_agent.db import write_tx
from sellershield_agent.domain.state import (
    PROPOSAL_TRANSITIONS,
    ApprovalStatus,
    EntityNotFound,
    ProposalStatus,
    approvals,
    client_events,
    generate_approval_id,
    http_idempotency_keys,
    proposals,
    transition_proposal,
)
from sellershield_agent.events.emit import emit_client_event
from sellershield_agent.execution.common import (
    MESSAGE_KEYS,
    SagaPlan,
    format_execution_status_message,
    format_utc_iso,
    now_utc_iso,
)
from sellershield_agent.execution.db_ops import (
    append_audit_in_tx,
    create_approval_in_tx,
    enqueue_outbox_in_tx,
)
from sellershield_agent.memory.writer import record_memory_event_in_tx
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.policy.rules import ApproverRole
from sellershield_agent.proposals.executor import ExecutionResult


def _emit_client_event_in_tx(
    tx: Connection,
    *,
    customer_id: str,
    proposal_id: str,
    status: str,
    return_id: str | None,
    approval_id: str | None,
    message_key: str,
    now_iso: str,
    proposal_type: str | None = None,
) -> None:
    existing = tx.execute(
        select(client_events.c.payload_json).where(
            client_events.c.customer_id == customer_id,
            client_events.c.type == "notification.new",
        )
    ).fetchall()
    for row in existing:
        try:
            parsed = json.loads(str(row.payload_json))
        except Exception:
            continue
        if (
            isinstance(parsed, dict)
            and parsed.get("proposal_id") == proposal_id
            and parsed.get("status") == status
        ):
            return

    msg = format_execution_status_message(
        status,
        message_key=message_key,
        return_id=return_id,
        approval_id=approval_id,
        proposal_type=proposal_type,
    )
    payload: dict[str, Any] = {
        "proposal_id": proposal_id,
        "status": status,
        "return_id": return_id,
        "approval_id": approval_id,
        "message_key": message_key,
        "message": msg,
    }
    emit_client_event(tx, customer_id, "notification.new", payload, now_iso)


def _update_confirm_idempotency_in_tx(
    tx: Connection,
    *,
    proposal_id: str,
    result: ExecutionResult,
) -> None:
    msg_key = result.message_key or MESSAGE_KEYS[result.status]
    resp = {
        "proposal_id": proposal_id,
        "status": result.status,
        "return_id": result.return_id,
        "approval_id": result.approval_id,
        "message_key": msg_key,
    }
    tx.execute(
        update(http_idempotency_keys)
        .where(
            http_idempotency_keys.c.proposal_id == proposal_id,
            http_idempotency_keys.c.operation == "confirm",
        )
        .values(response_json=json.dumps(resp, sort_keys=True, separators=(",", ":")))
    )


def finalize_approval_sync(
    engine: Engine,
    *,
    plan: SagaPlan,
    return_id: str | None,
    refund_id: str | None,
    clock: Clock,
    request_id: str,
    actor_type: ActorType | str,
    actor_id: str,
    update_unknown_to_executing: bool = False,
) -> str:
    """Create approval row, client event, seller outbox stub, and audit in one write_tx."""
    now_dt = clock.now()
    now_iso = format_utc_iso(now_dt)
    with write_tx(engine) as tx:
        prop_row = tx.execute(
            select(proposals).where(proposals.c.proposal_id == plan.proposal_id)
        ).fetchone()
        if prop_row is None:
            raise EntityNotFound(f"Proposal '{plan.proposal_id}' not found")

        if update_unknown_to_executing and str(prop_row.status) == ProposalStatus.unknown.value:
            transition_proposal(
                tx,
                proposal_id=plan.proposal_id,
                to_status=ProposalStatus.executing,
                expected_version=int(prop_row.version),
                clock=clock,
                request_id=request_id,
                actor_id="reconciler",
            )

        existing_apr = tx.execute(
            select(approvals).where(approvals.c.proposal_id == plan.proposal_id)
        ).fetchone()
        if existing_apr is not None:
            approval_id = str(existing_apr.approval_id)
            append_audit_in_tx(
                tx,
                clock=clock,
                request_id=request_id,
                session_id=plan.session_id,
                actor_type=actor_type,
                actor_id=actor_id,
                event_type=EventType.APPROVAL_CREATED,
                resource_type="approvals",
                resource_id=approval_id,
                action="existing_approval_reused",
                outcome=Outcome.SUCCESS,
                policy_version=plan.policy_version,
                details={"proposal_id": plan.proposal_id, "approval_id": approval_id},
            )
            _emit_client_event_in_tx(
                tx,
                customer_id=plan.customer_id,
                proposal_id=plan.proposal_id,
                status="pending_approval",
                return_id=return_id,
                approval_id=approval_id,
                message_key="proposal.pending_approval",
                now_iso=now_iso,
                proposal_type=plan.proposal_type,
            )
            return approval_id

        policy = load_policy(plan.policy_version)
        required_role = (
            ApproverRole.seller_owner.value
            if plan.amount_paise > policy.staff_approval_limit_paise
            else ApproverRole.seller_staff.value
        )
        due_at = format_utc_iso(now_dt + timedelta(hours=policy.approval_sla_hours))
        expires_at = format_utc_iso(now_dt + timedelta(days=policy.approval_expiry_days))
        approval_id = generate_approval_id()
        facts_hash = plan.facts_hash or str(prop_row.facts_hash)

        create_approval_in_tx(
            tx,
            approval_id=approval_id,
            proposal_id=plan.proposal_id,
            store_id="nestkart",
            order_id=plan.order_id,
            return_id=return_id,
            refund_id=refund_id,
            amount_paise=plan.amount_paise,
            required_role=required_role,
            status=ApprovalStatus.pending.value,
            policy_version=plan.policy_version,
            facts_hash=facts_hash,
            due_at=due_at,
            expires_at=expires_at,
            decided_by=None,
            decided_at=None,
            decision_reason=None,
            store_authorization_ref=None,
            last_error=None,
            revalidation_diff_json=None,
            created_at=now_iso,
            version=1,
        )

        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=plan.session_id,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type=EventType.APPROVAL_CREATED,
            resource_type="approvals",
            resource_id=approval_id,
            action="approval_created",
            outcome=Outcome.SUCCESS,
            policy_version=plan.policy_version,
            details={
                "proposal_id": plan.proposal_id,
                "order_id": plan.order_id,
                "return_id": return_id,
                "refund_id": refund_id,
                "amount_paise": plan.amount_paise,
                "required_role": required_role,
                "resolution": plan.resolution,
                "refund_method": plan.refund_method,
            },
        )

        _emit_client_event_in_tx(
            tx,
            customer_id=plan.customer_id,
            proposal_id=plan.proposal_id,
            status="pending_approval",
            return_id=return_id,
            approval_id=approval_id,
            message_key="proposal.pending_approval",
            now_iso=now_iso,
            proposal_type=plan.proposal_type,
        )

        enqueue_outbox_in_tx(
            tx,
            dedupe_key=f"seller_approval_needed:{plan.proposal_id}",
            template="seller.approval_needed",
            payload={
                "approval_id": approval_id,
                "proposal_id": plan.proposal_id,
                "order_id": plan.order_id,
                "return_id": return_id,
                "refund_id": refund_id,
                "amount_paise": plan.amount_paise,
                "required_role": required_role,
                "resolution": plan.resolution,
                "refund_method": plan.refund_method,
                "due_at": due_at,
                "expires_at": expires_at,
            },
            now_iso=now_iso,
            customer_id=None,
        )
        return approval_id


def finalize_proposal_sync(
    engine: Engine,
    *,
    plan: SagaPlan,
    result: ExecutionResult,
    clock: Clock,
    request_id: str,
    actor_id: str,
    emit_client_event: bool = True,
    update_proposal_status: bool = True,
    update_idempotency: bool = False,
) -> None:
    """Optionally transition proposal state, emit client_event, and update HTTP idempotency."""
    now_iso = now_utc_iso(clock)
    msg_key = result.message_key or MESSAGE_KEYS[result.status]
    with write_tx(engine) as tx:
        prop_row = tx.execute(
            select(proposals).where(proposals.c.proposal_id == plan.proposal_id)
        ).fetchone()
        if prop_row is None:
            return

        if update_proposal_status:
            curr_status = str(prop_row.status)
            target_status = (
                result.status
                if result.status
                in (
                    ProposalStatus.succeeded.value,
                    ProposalStatus.failed.value,
                    ProposalStatus.unknown.value,
                )
                else ProposalStatus.executing.value
            )
            if curr_status != target_status and target_status in PROPOSAL_TRANSITIONS.get(
                curr_status, set()
            ):
                transition_proposal(
                    tx,
                    proposal_id=plan.proposal_id,
                    to_status=target_status,
                    expected_version=int(prop_row.version),
                    clock=clock,
                    request_id=request_id,
                    actor_id=actor_id,
                )
        else:
            append_audit_in_tx(
                tx,
                clock=clock,
                request_id=request_id,
                session_id=plan.session_id,
                actor_type=ActorType.CUSTOMER,
                actor_id=actor_id,
                event_type=EventType.EXECUTION_STEP,
                resource_type="proposals",
                resource_id=plan.proposal_id,
                action="finalize_proposal_idempotency",
                outcome=Outcome.SUCCESS,
                policy_version=plan.policy_version,
                details={"status": result.status},
            )

        if emit_client_event and result.status in (
            "succeeded",
            "pending_approval",
            "unknown",
            "failed",
        ):
            _emit_client_event_in_tx(
                tx,
                customer_id=plan.customer_id,
                proposal_id=plan.proposal_id,
                status=result.status,
                return_id=result.return_id,
                approval_id=result.approval_id,
                message_key=msg_key,
                now_iso=now_iso,
                proposal_type=plan.proposal_type,
            )

        if result.status in ("succeeded", "failed"):
            record_memory_event_in_tx(
                tx,
                customer_id=plan.customer_id,
                event_type=f"proposal.{result.status}",
                ref_id=result.return_id or result.refund_id or plan.proposal_id,
                outcome=result.status,
                occurred_at=now_iso,
                order_id=plan.order_id,
            )

        if update_idempotency:
            _update_confirm_idempotency_in_tx(tx, proposal_id=plan.proposal_id, result=result)

