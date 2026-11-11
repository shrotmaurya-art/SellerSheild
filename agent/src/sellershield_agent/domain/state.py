"""Proposal, execution, approval, and decision data models and state machines.

Implements durable, tested state management, allowed-transition state machines,
optimistic locking, and atomic audit logging (Spec §2, §6.3, §9.1, §10, §21 amendment 4).
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Connection,
    Engine,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
    update,
)

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_metadata, write_tx
from sellershield_agent.logging import EMAIL_REGEX

# ── Status Enums ───────────────────────────────────────────────────────────


class ProposalStatus(StrEnum):
    """Proposal lifecycle states per Spec §10."""

    proposed = "proposed"
    confirmed = "confirmed"
    cancelled = "cancelled"
    expired = "expired"
    executing = "executing"
    succeeded = "succeeded"
    failed = "failed"
    unknown = "unknown"


class ApprovalStatus(StrEnum):
    """Seller approval lifecycle states per Spec §9.1 and §21 amendment 4."""

    pending = "pending"
    approving = "approving"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    needs_revalidation = "needs_revalidation"
    withdrawn = "withdrawn"


class ExecutionStatus(StrEnum):
    """Saga execution step states per Spec §6.3."""

    executing = "executing"
    succeeded = "succeeded"
    failed = "failed"
    unknown = "unknown"
    reconciled = "reconciled"


# ── Allowed-Transition Tables ──────────────────────────────────────────────

PROPOSAL_TRANSITIONS: dict[str, set[str]] = {
    ProposalStatus.proposed.value: {
        ProposalStatus.confirmed.value,
        ProposalStatus.cancelled.value,
        ProposalStatus.expired.value,
    },
    ProposalStatus.confirmed.value: {
        ProposalStatus.executing.value,
    },
    ProposalStatus.executing.value: {
        ProposalStatus.succeeded.value,
        ProposalStatus.failed.value,
        ProposalStatus.unknown.value,
    },
    ProposalStatus.unknown.value: {
        ProposalStatus.succeeded.value,
        ProposalStatus.failed.value,
        ProposalStatus.executing.value,
    },
    ProposalStatus.cancelled.value: set(),
    ProposalStatus.expired.value: set(),
    ProposalStatus.succeeded.value: {ProposalStatus.failed.value},
    ProposalStatus.failed.value: set(),
}

APPROVAL_TRANSITIONS: dict[str, set[str]] = {
    ApprovalStatus.pending.value: {
        ApprovalStatus.approving.value,
        ApprovalStatus.rejected.value,
        ApprovalStatus.expired.value,
        ApprovalStatus.needs_revalidation.value,
        ApprovalStatus.withdrawn.value,
    },
    ApprovalStatus.approving.value: {
        ApprovalStatus.approved.value,
        ApprovalStatus.pending.value,  # failure returns to pending per amendment 2
    },
    ApprovalStatus.needs_revalidation.value: {
        ApprovalStatus.pending.value,
        ApprovalStatus.rejected.value,
        ApprovalStatus.expired.value,
        ApprovalStatus.withdrawn.value,
    },
    ApprovalStatus.approved.value: set(),
    ApprovalStatus.rejected.value: set(),
    ApprovalStatus.expired.value: set(),
    ApprovalStatus.withdrawn.value: set(),
}


# ── Exceptions ─────────────────────────────────────────────────────────────


class InvalidTransition(Exception):
    """Raised when a state transition is not allowed or violates invariants."""


class VersionMismatch(InvalidTransition):
    """Raised when the expected row version does not match the stored version."""


class EntityNotFound(InvalidTransition):
    """Raised when the target transition entity does not exist."""


# ── Database Tables Attached to Shared Metadata ────────────────────────────

policy_decisions = Table(
    "policy_decisions",
    get_metadata(),
    Column("decision_id", String(64), primary_key=True),
    Column("request_id", String(64), nullable=True),
    Column("customer_id", String(64), nullable=False, index=True),
    Column("order_id", String(64), nullable=True, index=True),
    Column("policy_version", String(32), nullable=False),
    Column("facts_json", Text, nullable=False),
    Column("decision_json", Text, nullable=False),
    Column("created_at", String(32), nullable=False),
)

proposals = Table(
    "proposals",
    get_metadata(),
    Column("proposal_id", String(64), primary_key=True),
    Column("customer_id", String(64), nullable=False, index=True),
    Column("session_id", String(64), nullable=False, index=True),
    Column("type", String(32), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("decision", String(32), nullable=False),
    Column("amounts_json", Text, nullable=False),
    Column("policy_version", String(32), nullable=False),
    Column("facts_hash", String(64), nullable=False),
    Column("proposal_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", String(32), nullable=True),
    Column("expires_at", String(32), nullable=False),
    Column("confirmed_at", String(32), nullable=True),
    Column("version", Integer, nullable=False, default=1),
)

executions = Table(
    "executions",
    get_metadata(),
    Column("execution_id", String(64), primary_key=True),
    Column(
        "proposal_id",
        String(64),
        ForeignKey("proposals.proposal_id"),
        nullable=False,
        index=True,
    ),
    Column("step", String(64), nullable=False),
    Column("idempotency_key", String(128), unique=True, nullable=False),
    Column("status", String(32), nullable=False),
    Column("store_ref", String(64), nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("started_at", String(32), nullable=False),
    Column("finished_at", String(32), nullable=True),
    Column("attempts", Integer, nullable=False, default=1),
    Column("next_retry_at", String(32), nullable=True),
    Column("stuck", Boolean, nullable=False, default=False),
)

approvals = Table(
    "approvals",
    get_metadata(),
    Column("approval_id", String(64), primary_key=True),
    Column(
        "proposal_id",
        String(64),
        ForeignKey("proposals.proposal_id"),
        unique=True,
        nullable=False,
    ),
    Column("store_id", String(64), nullable=False, server_default="nestkart", default="nestkart"),
    Column("order_id", String(64), nullable=False, index=True),
    Column("return_id", String(64), nullable=True),
    Column("refund_id", String(64), nullable=True),
    Column("amount_paise", Integer, nullable=False),
    Column("required_role", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("policy_version", String(32), nullable=False),
    Column("facts_hash", String(64), nullable=False),
    Column("due_at", String(32), nullable=False),
    Column("expires_at", String(32), nullable=False),
    Column("decided_by", String(64), nullable=True),
    Column("decided_at", String(32), nullable=True),
    Column("decision_reason", Text, nullable=True),
    Column("store_authorization_ref", String(64), nullable=True),
    Column("refund_status", String(32), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("revalidation_diff_json", Text, nullable=True),
    Column("created_at", String(32), nullable=True),
    Column("version", Integer, nullable=False, default=1),
)

client_events = Table(
    "client_events",
    get_metadata(),
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(64), unique=True, nullable=False, index=True),
    Column("customer_id", String(64), nullable=False, index=True),
    Column("type", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", String(32), nullable=False),
)

http_idempotency_keys = Table(
    "http_idempotency_keys",
    get_metadata(),
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("customer_id", String(64), nullable=False, index=True),
    Column("idempotency_key", String(128), nullable=False),
    Column("operation", String(32), nullable=False),
    Column("proposal_id", String(64), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("response_json", Text, nullable=False),
    Column("created_at", String(32), nullable=False),
    UniqueConstraint(
        "customer_id",
        "idempotency_key",
        name="uq_http_idempotency_customer_key",
    ),
)

notifications_outbox = Table(
    "notifications_outbox",
    get_metadata(),
    Column("notification_id", String(64), primary_key=True),
    Column("channel", String(32), nullable=False),
    Column("customer_id", String(64), nullable=True, index=True),
    Column("template", String(64), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("dedupe_key", String(128), unique=True, nullable=False),
    Column("status", String(32), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_attempt_at", String(32), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", String(32), nullable=False),
)

webhook_events_seen = Table(
    "webhook_events_seen",
    get_metadata(),
    Column("event_id", String(64), primary_key=True),
    Column("received_at", String(32), nullable=False),
)

webhook_resource_versions = Table(
    "webhook_resource_versions",
    get_metadata(),
    Column("resource_type", String(32), primary_key=True),
    Column("resource_id", String(64), primary_key=True),
    Column("resource_version", Integer, nullable=False),
    Column("updated_at", String(32), nullable=False),
)

llm_calls = Table(
    "llm_calls",
    get_metadata(),
    Column("call_id", String(64), primary_key=True),
    Column("request_id", String(64), nullable=True, index=True),
    Column("session_id", String(64), nullable=True, index=True),
    Column("stage", String(32), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("model", String(64), nullable=False),
    Column("prompt_version", String(32), nullable=True),
    Column("prompt_hash", String(64), nullable=False),
    Column("tokens_in", Integer, nullable=False),
    Column("tokens_out", Integer, nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("cached", Boolean, nullable=False, default=False),
    Column("status", String(32), nullable=False),
    Column("created_at", String(32), nullable=False),
)

tool_calls = Table(
    "tool_calls",
    get_metadata(),
    Column("call_id", String(64), primary_key=True),
    Column("request_id", String(64), nullable=True, index=True),
    Column("session_id", String(64), nullable=True, index=True),
    Column("tool", String(64), nullable=False),
    Column("args_hash", String(64), nullable=False),
    Column("args_redacted_json", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("error_code", String(64), nullable=True),
    Column("latency_ms", Integer, nullable=False),
    Column("created_at", String(32), nullable=False),
)

cases = Table(
    "cases",
    get_metadata(),
    Column("case_id", String(64), primary_key=True),
    Column("customer_id", String(64), nullable=False, index=True),
    Column("order_id", String(64), nullable=True, index=True),
    Column("session_id", String(64), nullable=True, index=True),
    Column("category", String(64), nullable=False),
    Column("priority", String(32), nullable=False, default="normal"),
    Column("status", String(32), nullable=False, default="open"),
    Column("handler", String(32), nullable=False, default="agent"),
    Column("assignee", String(64), nullable=True),
    Column("brief_json", Text, nullable=True),
    Column("sla_due_at", String(32), nullable=True),
    Column("resolution_code", String(64), nullable=True),
    Column("evidence_ids_json", Text, nullable=True),
    Column("created_at", String(32), nullable=False),
    Column("resolved_at", String(32), nullable=True),
    Column("version", Integer, nullable=False, default=1),
)

case_events = Table(
    "case_events",
    get_metadata(),
    Column("event_id", String(64), primary_key=True),
    Column("case_id", String(64), nullable=False, index=True),
    Column("actor_type", String(32), nullable=False),
    Column("actor_id", String(64), nullable=True),
    Column("kind", String(64), nullable=False),
    Column("text", Text, nullable=True),
    Column("created_at", String(32), nullable=False),
)

customer_memory = Table(
    "customer_memory",
    get_metadata(),
    Column("customer_id", String(64), primary_key=True),
    Column("profile_json", Text, nullable=False),
    Column("updated_at", String(32), nullable=False),
)

memory_events = Table(
    "memory_events",
    get_metadata(),
    Column("event_id", String(64), primary_key=True),
    Column("customer_id", String(64), nullable=False, index=True),
    Column("type", String(64), nullable=False),
    Column("ref_id", String(64), nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("occurred_at", String(32), nullable=False),
)

policies = Table(
    "policies",
    get_metadata(),
    Column("version", String(32), primary_key=True),
    Column("status", String(32), nullable=False),  # draft, active, retired
    Column("document_json", Text, nullable=False),
    Column("created_by", String(64), nullable=False),
    Column("created_at", String(32), nullable=False),
    Column("activated_at", String(32), nullable=True),
)

monitor_runs = Table(
    "monitor_runs",
    get_metadata(),
    Column("run_id", String(64), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("started_at", String(32), nullable=False),
    Column("finished_at", String(32), nullable=True),
    Column("scanned", Integer, nullable=False, default=0),
    Column("flagged", Integer, nullable=False, default=0),
    Column("notified", Integer, nullable=False, default=0),
    Column("skipped_duplicates", Integer, nullable=False, default=0),
    Column("cases_created", Integer, nullable=False, default=0),
    Column("summary_json", Text, nullable=False, default="{}"),
)

monitor_events = Table(
    "monitor_events",
    get_metadata(),
    Column("key", String(128), primary_key=True),
    Column("run_id", String(64), nullable=True),
    Column("order_id", String(64), nullable=False, index=True),
    Column("customer_id", String(64), nullable=False, index=True),
    Column("kind", String(64), nullable=False),
    Column("created_at", String(32), nullable=False),
)


# ── Cryptographically Strong ID Generators ─────────────────────────────────


def generate_proposal_id() -> str:
    """Generate PRP- + 8 random lowercase hexadecimal characters from secrets."""
    return f"PRP-{secrets.token_hex(4)}"


def generate_approval_id() -> str:
    """Generate APR- + 8 non-sequential random digits."""
    return f"APR-{secrets.randbelow(90000000) + 10000000}"


def generate_decision_id() -> str:
    """Generate DEC- + 8 random hexadecimal characters."""
    return f"DEC-{secrets.token_hex(4)}"


def generate_client_event_id() -> str:
    """Generate CEV- + 16 random hexadecimal characters."""
    return f"CEV-{secrets.token_hex(8)}"


def generate_execution_id() -> str:
    """Generate EXEC- + 8 random hexadecimal characters."""
    return f"EXEC-{secrets.token_hex(4)}"


def generate_notification_id() -> str:
    """Generate NTF- + 8 random hexadecimal characters."""
    return f"NTF-{secrets.token_hex(4)}"


def generate_llm_call_id() -> str:
    """Generate LLM- + 8 random hexadecimal characters."""
    return f"LLM-{secrets.token_hex(4)}"


def generate_tool_call_id() -> str:
    """Generate TCL- + 8 random hexadecimal characters."""
    return f"TCL-{secrets.token_hex(4)}"


def generate_case_id() -> str:
    """Generate CASE- + 8 random lowercase hexadecimal characters."""
    return f"CASE-{secrets.token_hex(4)}"


def generate_case_event_id() -> str:
    """Generate CEVT- + 12 random lowercase hexadecimal characters."""
    return f"CEVT-{secrets.token_hex(6)}"


def generate_memory_event_id() -> str:
    """Generate MEM- + 12 random lowercase hexadecimal characters."""
    return f"MEM-{secrets.token_hex(6)}"


# ── Expiry & SLA Due Evaluators ────────────────────────────────────────────


def _parse_iso_utc(ts_str: str) -> datetime:
    """Parse ISO 8601 string to timezone-aware UTC datetime."""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_proposal_expired(expires_at: str | datetime, clock: Clock) -> bool:
    """Check if proposal has reached or passed its expiry boundary."""
    dt = expires_at if isinstance(expires_at, datetime) else _parse_iso_utc(expires_at)
    return clock.now() >= dt


def is_approval_expired(expires_at: str | datetime, clock: Clock) -> bool:
    """Check if approval has reached or passed its expiry boundary."""
    dt = expires_at if isinstance(expires_at, datetime) else _parse_iso_utc(expires_at)
    return clock.now() >= dt


def is_approval_due(due_at: str | datetime, clock: Clock) -> bool:
    """Check if approval has reached or exceeded its SLA due deadline."""
    dt = due_at if isinstance(due_at, datetime) else _parse_iso_utc(due_at)
    return clock.now() >= dt


# ── PII Sanitization for Policy Decisions ──────────────────────────────────

_PII_KEYS = {
    "address",
    "shipping_address",
    "billing_address",
    "street",
    "city",
    "pincode",
    "postal_code",
    "phone",
    "phone_number",
    "mobile",
    "contact",
    "email",
    "token",
    "access_token",
    "authorization",
    "secret",
    "password",
    "api_key",
}

_PHONE_REGEX = re.compile(r"(\+?\d[\d -]{7,}\d)")
_JWT_REGEX = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9._-]+")


def sanitize_policy_facts(val: Any) -> Any:
    """Recursively strip customer personal data (address, phone, email, tokens) from facts."""
    if isinstance(val, str):
        if EMAIL_REGEX.search(val):
            val = EMAIL_REGEX.sub("[REDACTED_EMAIL]", val)
        if _JWT_REGEX.search(val):
            val = _JWT_REGEX.sub("[REDACTED_TOKEN]", val)
        if _PHONE_REGEX.search(val):
            val = _PHONE_REGEX.sub("[REDACTED_PHONE]", val)
        return val
    if isinstance(val, Mapping):
        cleaned: dict[str, Any] = {}
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(pii in k_lower for pii in _PII_KEYS):
                continue
            cleaned[str(k)] = sanitize_policy_facts(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [sanitize_policy_facts(item) for item in val]
    return val


def record_policy_decision(
    db_or_tx: Engine | Connection,
    *,
    request_id: str | None,
    customer_id: str,
    order_id: str | None,
    policy_version: str,
    facts: dict[str, Any],
    decision: dict[str, Any],
    clock: Clock,
) -> str:
    """Store a sanitized policy decision row in policy_decisions."""
    clean_facts = sanitize_policy_facts(facts)
    facts_json = json.dumps(clean_facts, sort_keys=True, separators=(",", ":"))
    decision_json = json.dumps(decision, sort_keys=True, separators=(",", ":"))
    dec_id = generate_decision_id()
    created_at = clock.now().isoformat()

    def _insert(tx: Connection) -> str:
        tx.execute(
            policy_decisions.insert().values(
                decision_id=dec_id,
                request_id=request_id,
                customer_id=customer_id,
                order_id=order_id,
                policy_version=policy_version,
                facts_json=facts_json,
                decision_json=decision_json,
                created_at=created_at,
            )
        )
        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=created_at,
                request_id=request_id,
                session_id=None,
                actor_type=ActorType.SYSTEM,
                actor_id=customer_id,
                event_type=EventType.POLICY_DECISION,
                resource_type="policy_decisions",
                resource_id=dec_id,
                action="record_policy_decision",
                outcome=Outcome.SUCCESS,
                policy_version=policy_version,
                details={"order_id": order_id, "customer_id": customer_id},
            ),
        )
        return dec_id

    if isinstance(db_or_tx, Engine):
        with write_tx(db_or_tx) as tx:
            return _insert(tx)
    return _insert(db_or_tx)


# ── Atomic State Transition Engine ─────────────────────────────────────────


def _execute_transition(
    tx: Connection,
    table: Table,
    id_col_name: str,
    entity_id: str,
    allowed_transitions: dict[str, set[str]],
    to_status: str,
    expected_version: int,
    clock: Clock,
    request_id: str | None,
    actor_id: str | None,
    extra_values: dict[str, Any] | None = None,
    actor_type: str | None = None,
) -> int:
    """Execute atomic status update and audit write inside the caller's write_tx.

    Uses one atomic UPDATE with status and version in the WHERE clause, checking
    the row count per Spec Document 6 §0 and KP-01.
    """
    id_col = table.c[id_col_name]

    # Valid statuses that can legally transition to to_status
    valid_from_statuses = [
        from_st for from_st, targets in allowed_transitions.items() if to_status in targets
    ]
    if not valid_from_statuses:
        raise InvalidTransition(f"Target status '{to_status}' is not reachable for {table.name}")

    # 1. Execute direct atomic UPDATE with status and version in WHERE clause
    new_version = expected_version + 1
    values_to_update: dict[str, Any] = {
        "status": to_status,
        "version": new_version,
    }
    if extra_values:
        values_to_update.update(extra_values)

    stmt = (
        update(table)
        .where(
            id_col == entity_id,
            table.c.version == expected_version,
            table.c.status.in_(valid_from_statuses),
        )
        .values(**values_to_update)
    )
    result = tx.execute(stmt)

    if result.rowcount == 1:
        # Atomic transition succeeded! Now fetch row details needed for audit
        row = tx.execute(select(table).where(id_col == entity_id)).fetchone()
        sess_id = str(row.session_id) if row and hasattr(row, "session_id") else None
        pol_ver = str(row.policy_version) if row and hasattr(row, "policy_version") else None
        resolved_actor_type = actor_type or ("seller" if table.name == "approvals" else "customer")

        audit_evt = AuditEvent(
            event_id=f"AUD-{secrets.token_hex(8)}",
            ts_utc=clock.now().isoformat(),
            request_id=request_id,
            session_id=sess_id,
            actor_type=resolved_actor_type,
            actor_id=actor_id,
            event_type=EventType.TOOL_CALL,
            resource_type=table.name,
            resource_id=entity_id,
            action=f"transition_to_{to_status}",
            outcome=Outcome.SUCCESS,
            policy_version=pol_ver,
            details={
                "to_status": to_status,
                "previous_version": expected_version,
                "new_version": new_version,
            },
        )
        AuditService.append(tx, audit_evt)
        return new_version

    # rowcount == 0: transition was rejected or raced. Diagnose exact failure:
    row = tx.execute(select(table).where(id_col == entity_id)).fetchone()
    if row is None:
        raise EntityNotFound(f"{table.name} record '{entity_id}' not found")
    if int(row.version) != expected_version:
        raise VersionMismatch(
            f"Version mismatch for {table.name} '{entity_id}': "
            f"expected {expected_version}, got {row.version}"
        )
    if str(row.status) not in valid_from_statuses:
        raise InvalidTransition(
            f"Illegal transition for {table.name} '{entity_id}': "
            f"cannot move from '{row.status}' to '{to_status}'"
        )
    raise InvalidTransition(f"Transition failed for {table.name} '{entity_id}'")


def transition_proposal(
    db_or_tx: Engine | Connection,
    *,
    proposal_id: str,
    to_status: ProposalStatus | str,
    expected_version: int,
    clock: Clock,
    request_id: str | None = None,
    actor_id: str | None = None,
    extra_values: dict[str, Any] | None = None,
    actor_type: str | None = None,
) -> int:
    """Transition proposal state atomically with optimistic locking and audit logging."""
    to_st = to_status.value if isinstance(to_status, ProposalStatus) else str(to_status)

    def _call(tx: Connection) -> int:
        return _execute_transition(
            tx=tx,
            table=proposals,
            id_col_name="proposal_id",
            entity_id=proposal_id,
            allowed_transitions=PROPOSAL_TRANSITIONS,
            to_status=to_st,
            expected_version=expected_version,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            extra_values=extra_values,
            actor_type=actor_type,
        )

    if isinstance(db_or_tx, Engine):
        with write_tx(db_or_tx) as tx:
            return _call(tx)
    return _call(db_or_tx)


def transition_approval(
    db_or_tx: Engine | Connection,
    *,
    approval_id: str,
    to_status: ApprovalStatus | str,
    expected_version: int,
    clock: Clock,
    request_id: str | None = None,
    actor_id: str | None = None,
    extra_values: dict[str, Any] | None = None,
    actor_type: str | None = None,
) -> int:
    """Transition approval state atomically with optimistic locking and audit logging."""
    to_st = to_status.value if isinstance(to_status, ApprovalStatus) else str(to_status)

    def _call(tx: Connection) -> int:
        return _execute_transition(
            tx=tx,
            table=approvals,
            id_col_name="approval_id",
            entity_id=approval_id,
            allowed_transitions=APPROVAL_TRANSITIONS,
            to_status=to_st,
            expected_version=expected_version,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            extra_values=extra_values,
            actor_type=actor_type,
        )

    if isinstance(db_or_tx, Engine):
        with write_tx(db_or_tx) as tx:
            return _call(tx)
    return _call(db_or_tx)


def transition(
    db_or_tx: Engine | Connection,
    *,
    entity_type: str,
    entity_id: str,
    to_status: ProposalStatus | ApprovalStatus | str,
    expected_version: int,
    clock: Clock,
    request_id: str | None = None,
    actor_id: str | None = None,
    extra_values: dict[str, Any] | None = None,
    actor_type: str | None = None,
) -> int:
    """Unified transition dispatcher supporting proposals and approvals."""
    ent = entity_type.strip().lower()
    if ent in ("proposals", "proposal"):
        return transition_proposal(
            db_or_tx,
            proposal_id=entity_id,
            to_status=to_status,
            expected_version=expected_version,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            extra_values=extra_values,
            actor_type=actor_type,
        )
    if ent in ("approvals", "approval"):
        return transition_approval(
            db_or_tx,
            approval_id=entity_id,
            to_status=to_status,
            expected_version=expected_version,
            clock=clock,
            request_id=request_id,
            actor_id=actor_id,
            extra_values=extra_values,
            actor_type=actor_type,
        )
    raise InvalidTransition(f"Unsupported entity type '{entity_type}'")
