"""Synchronous database helpers for customer approval withdrawal."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, select

from sellershield_agent.approvals.common_db import transition_parent_proposal_if_possible
from sellershield_agent.audit.models import ActorType, EventType, Outcome
from sellershield_agent.clock import Clock
from sellershield_agent.db import read_tx, write_tx
from sellershield_agent.domain.state import (
    ApprovalStatus,
    ProposalStatus,
    approvals,
    http_idempotency_keys,
    proposals,
    transition_approval,
)
from sellershield_agent.execution.common import format_utc_iso
from sellershield_agent.execution.db_ops import append_audit_in_tx
from sellershield_agent.memory.writer import record_memory_event_in_tx
from sellershield_agent.proposals.integrity import check_idempotency_row


def load_withdraw_context_sync(
    engine: Engine,
    *,
    proposal_id: str,
    customer_id: str,
    idempotency_key: str,
    request_hash: str,
) -> tuple[dict[str, Any] | None, Any | None, Any | None]:
    """Check customer idempotency and load proposal + approval in read_tx."""
    with read_tx(engine) as conn:
        replay = check_idempotency_row(
            conn,
            customer_id=customer_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay, None, None
        prop_row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == proposal_id)
        ).fetchone()
        if prop_row is None or str(prop_row.customer_id) != customer_id:
            return None, None, None
        apr_row = conn.execute(
            select(approvals).where(approvals.c.proposal_id == proposal_id)
        ).fetchone()
        return None, prop_row, apr_row


def finalize_withdrawn_sync(
    engine: Engine,
    *,
    approval_id: str,
    expected_version: int,
    proposal_id: str,
    order_id: str,
    customer_id: str,
    policy_version: str,
    session_id: str | None,
    idempotency_key: str,
    request_hash: str,
    response_dict: dict[str, Any],
    clock: Clock,
    request_id: str,
) -> int:
    """Finalize pending/needs_revalidation -> withdrawn, fail proposal, audit, idempotency."""
    now_iso = format_utc_iso(clock.now())
    with write_tx(engine) as tx:
        new_ver = transition_approval(
            tx,
            approval_id=approval_id,
            to_status=ApprovalStatus.withdrawn,
            expected_version=expected_version,
            clock=clock,
            request_id=request_id,
            actor_id=customer_id,
            actor_type=ActorType.CUSTOMER.value,
            extra_values={"last_error": None},
        )
        transition_parent_proposal_if_possible(
            tx,
            proposal_id=proposal_id,
            to_status=ProposalStatus.failed,
            clock=clock,
            request_id=request_id,
            actor_id=customer_id,
            actor_type=ActorType.CUSTOMER.value,
        )
        record_memory_event_in_tx(
            tx,
            customer_id=customer_id,
            event_type="proposal.failed",
            ref_id=proposal_id,
            outcome="failed",
            occurred_at=now_iso,
            order_id=order_id,
        )
        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=session_id,
            actor_type=ActorType.CUSTOMER,
            actor_id=customer_id,
            event_type=EventType.APPROVAL_DECIDED,
            resource_type="approvals",
            resource_id=approval_id,
            action="withdraw",
            outcome=Outcome.SUCCESS,
            policy_version=policy_version,
            details={"decision": "withdrawn", "proposal_id": proposal_id, "order_id": order_id},
        )
        existing = tx.execute(
            select(http_idempotency_keys.c.id).where(
                http_idempotency_keys.c.customer_id == customer_id,
                http_idempotency_keys.c.idempotency_key == idempotency_key,
            )
        ).fetchone()
        if existing is None:
            tx.execute(
                http_idempotency_keys.insert().values(
                    customer_id=customer_id,
                    idempotency_key=idempotency_key,
                    operation="withdraw",
                    proposal_id=proposal_id,
                    request_hash=request_hash,
                    status_code=200,
                    response_json=json.dumps(response_dict, sort_keys=True, separators=(",", ":")),
                    created_at=now_iso,
                )
            )
        return new_ver
