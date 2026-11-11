"""Synchronous database transaction helpers for seller approve and reject mutations."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine

from sellershield_agent.approvals.common_db import (
    emit_approval_decided_event_in_tx,
    transition_parent_proposal_if_possible,
)
from sellershield_agent.audit.models import ActorType, EventType, Outcome
from sellershield_agent.clock import Clock
from sellershield_agent.db import write_tx
from sellershield_agent.domain.state import (
    ApprovalStatus,
    ProposalStatus,
    transition_approval,
)
from sellershield_agent.execution.common import format_utc_iso
from sellershield_agent.execution.db_ops import (
    append_audit_in_tx,
    update_approval_metadata_in_tx,
)
from sellershield_agent.memory.writer import record_memory_event_in_tx


def record_revalidation_needed_sync(
    engine: Engine,
    *,
    approval_id: str,
    expected_version: int,
    diff: list[dict[str, Any]],
    actor_id: str,
    policy_version: str,
    session_id: str | None,
    clock: Clock,
    request_id: str,
) -> int:
    """Atomically set approval to needs_revalidation, store sanitized diff, and audit."""
    diff_json = json.dumps(diff, sort_keys=True, separators=(",", ":"))
    with write_tx(engine) as tx:
        new_ver = transition_approval(
            tx,
            approval_id=approval_id,
            to_status=ApprovalStatus.needs_revalidation,
            expected_version=expected_version,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            actor_type=ActorType.SELLER.value,
            extra_values={"revalidation_diff_json": diff_json},
        )
        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.APPROVAL_DECIDED,
            resource_type="approvals",
            resource_id=approval_id,
            action="needs_revalidation",
            outcome=Outcome.DENIED,
            policy_version=policy_version,
            details={"reason": "needs_revalidation", "diff_count": len(diff)},
        )
        return new_ver


def claim_approving_sync(
    engine: Engine,
    *,
    approval_id: str,
    expected_version: int,
    actor_id: str,
    clock: Clock,
    request_id: str,
) -> int:
    """Atomically claim approval into 'approving' state before calling StoreClient."""
    with write_tx(engine) as tx:
        return transition_approval(
            tx,
            approval_id=approval_id,
            to_status=ApprovalStatus.approving,
            expected_version=expected_version,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            actor_type=ActorType.SELLER.value,
            extra_values={"last_error": None},
        )


def rollback_approving_to_pending_sync(
    engine: Engine,
    *,
    approval_id: str,
    approving_version: int,
    actor_id: str,
    error_summary: str,
    store_auth_ref: str | None,
    policy_version: str,
    session_id: str | None,
    clock: Clock,
    request_id: str,
) -> int:
    """Return approving -> pending after StoreClient failure, preserving partial store_auth_ref."""
    with write_tx(engine) as tx:
        extra: dict[str, Any] = {"last_error": error_summary}
        if store_auth_ref is not None:
            extra["store_authorization_ref"] = store_auth_ref
        new_ver = transition_approval(
            tx,
            approval_id=approval_id,
            to_status=ApprovalStatus.pending,
            expected_version=approving_version,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            actor_type=ActorType.SELLER.value,
            extra_values=extra,
        )
        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.APPROVAL_DECIDED,
            resource_type="approvals",
            resource_id=approval_id,
            action="approve",
            outcome=Outcome.FAILED,
            policy_version=policy_version,
            details={
                "decision": "approve",
                "error": error_summary,
                "store_auth_ref": store_auth_ref,
            },
        )
        return new_ver


def record_reject_error_sync(
    engine: Engine,
    *,
    approval_id: str,
    actor_id: str,
    error_summary: str,
    policy_version: str,
    session_id: str | None,
    clock: Clock,
    request_id: str,
) -> None:
    """Record StoreClient failure during reject while keeping approval state pending/reval."""
    with write_tx(engine) as tx:
        update_approval_metadata_in_tx(
            tx, approval_id=approval_id, values={"last_error": error_summary}
        )
        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.APPROVAL_DECIDED,
            resource_type="approvals",
            resource_id=approval_id,
            action="reject",
            outcome=Outcome.FAILED,
            policy_version=policy_version,
            details={"decision": "reject", "error": error_summary},
        )


def finalize_approved_sync(
    engine: Engine,
    *,
    approval_id: str,
    approving_version: int,
    proposal_id: str,
    order_id: str,
    customer_id: str,
    return_id: str | None,
    refund_id: str | None,
    amount_paise: int,
    store_auth_ref: str,
    reason: str | None,
    actor_id: str,
    policy_version: str,
    session_id: str | None,
    clock: Clock,
    request_id: str,
) -> int:
    """Finalize approving -> approved, succeed proposal, emit event, and audit."""
    now_iso = format_utc_iso(clock.now())
    with write_tx(engine) as tx:
        new_ver = transition_approval(
            tx,
            approval_id=approval_id,
            to_status=ApprovalStatus.approved,
            expected_version=approving_version,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            actor_type=ActorType.SELLER.value,
            extra_values={
                "decided_by": actor_id,
                "decided_at": now_iso,
                "decision_reason": reason,
                "store_authorization_ref": store_auth_ref,
                "last_error": None,
            },
        )
        transition_parent_proposal_if_possible(
            tx,
            proposal_id=proposal_id,
            to_status=ProposalStatus.succeeded,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            actor_type=ActorType.SELLER.value,
        )
        emit_approval_decided_event_in_tx(
            tx,
            customer_id=customer_id,
            approval_id=approval_id,
            proposal_id=proposal_id,
            order_id=order_id,
            return_id=return_id,
            refund_id=refund_id,
            status="approved",
            message=f"Your request for order {order_id} has been approved.",
            now_iso=now_iso,
        )
        record_memory_event_in_tx(
            tx,
            customer_id=customer_id,
            event_type="approval.decided",
            ref_id=return_id or refund_id or approval_id,
            outcome="approved",
            occurred_at=now_iso,
            order_id=order_id,
        )
        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.APPROVAL_DECIDED,
            resource_type="approvals",
            resource_id=approval_id,
            action="approve",
            outcome=Outcome.SUCCESS,
            policy_version=policy_version,
            details={
                "decision": "approved",
                "proposal_id": proposal_id,
                "order_id": order_id,
                "return_id": return_id,
                "refund_id": refund_id,
                "amount_paise": amount_paise,
                "store_auth_ref": store_auth_ref,
            },
        )
        return new_ver


def finalize_rejected_sync(
    engine: Engine,
    *,
    approval_id: str,
    expected_version: int,
    proposal_id: str,
    order_id: str,
    customer_id: str,
    return_id: str | None,
    refund_id: str | None,
    reason: str,
    actor_id: str,
    policy_version: str,
    session_id: str | None,
    clock: Clock,
    request_id: str,
) -> int:
    """Finalize pending/needs_revalidation -> rejected, fail proposal, neutral event, audit."""
    now_iso = format_utc_iso(clock.now())
    with write_tx(engine) as tx:
        new_ver = transition_approval(
            tx,
            approval_id=approval_id,
            to_status=ApprovalStatus.rejected,
            expected_version=expected_version,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            actor_type=ActorType.SELLER.value,
            extra_values={
                "decided_by": actor_id,
                "decided_at": now_iso,
                "decision_reason": reason,
                "last_error": None,
            },
        )
        transition_parent_proposal_if_possible(
            tx,
            proposal_id=proposal_id,
            to_status=ProposalStatus.failed,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            actor_type=ActorType.SELLER.value,
        )
        emit_approval_decided_event_in_tx(
            tx,
            customer_id=customer_id,
            approval_id=approval_id,
            proposal_id=proposal_id,
            order_id=order_id,
            return_id=return_id,
            refund_id=refund_id,
            status="rejected",
            message="Your request was declined after review and no refund was processed.",
            now_iso=now_iso,
        )
        record_memory_event_in_tx(
            tx,
            customer_id=customer_id,
            event_type="approval.decided",
            ref_id=return_id or refund_id or approval_id,
            outcome="rejected",
            occurred_at=now_iso,
            order_id=order_id,
        )
        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=session_id,
            actor_type=ActorType.SELLER,
            actor_id=actor_id,
            event_type=EventType.APPROVAL_DECIDED,
            resource_type="approvals",
            resource_id=approval_id,
            action="reject",
            outcome=Outcome.SUCCESS,
            policy_version=policy_version,
            details={
                "decision": "rejected",
                "proposal_id": proposal_id,
                "order_id": order_id,
                "return_id": return_id,
                "refund_id": refund_id,
            },
        )
        return new_ver
