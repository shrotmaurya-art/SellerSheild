"""Core audit service implementing the tamper-evident SHA-256 hash chain."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import Connection, text

from sellershield_agent.audit.models import (
    AuditEvent,
    ChainStatus,
    EventType,
    Outcome,
    audit_events,
)
from sellershield_agent.audit.sanitize import sanitize_details
from sellershield_agent.dev.faults import DevFaultAuditError, is_fault_active


class AuditService:
    """Tamper-evident audit service that writes and verifies sequential hash chains."""

    GENESIS_HASH: str = "0" * 64

    @classmethod
    def compute_hash(cls, event_fields: dict[str, Any], prev_hash: str) -> str:
        """Compute deterministic SHA-256 hash of event fields plus prev_hash."""
        payload = {**event_fields, "prev_hash": prev_hash}
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def append(cls, tx: Connection, event: AuditEvent) -> int:
        """Append an audit event inside the caller's write_tx transaction.

        Must run inside the caller's transaction so business changes and the
        audit row commit atomically. It reads the previous hash inside the same
        BEGIN IMMEDIATE transaction.
        """
        if is_fault_active("audit_fail"):
            raise DevFaultAuditError("Dev fault injected: audit write failed (audit_fail active)")

        is_denial = (
            event.outcome == Outcome.DENIED
            or event.event_type == EventType.AUTH_DENIED
            or str(event.outcome).lower() == "denied"
            or str(event.event_type).lower() == "auth_denied"
        )
        details_json = sanitize_details(event.details, actor_id=event.actor_id, is_denial=is_denial)

        last_row = tx.execute(
            text("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1")
        ).fetchone()

        prev_hash = cls.GENESIS_HASH if last_row is None else str(last_row[0])

        actor_type_str = (
            event.actor_type.value if hasattr(event.actor_type, "value") else str(event.actor_type)
        )
        event_type_str = (
            event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
        )
        outcome_str = event.outcome.value if hasattr(event.outcome, "value") else str(event.outcome)

        event_fields: dict[str, Any] = {
            "action": event.action,
            "actor_id": event.actor_id,
            "actor_type": actor_type_str,
            "details_json": details_json,
            "event_id": event.event_id,
            "event_type": event_type_str,
            "outcome": outcome_str,
            "policy_version": event.policy_version,
            "request_id": event.request_id,
            "resource_id": event.resource_id,
            "resource_type": event.resource_type,
            "session_id": event.session_id,
            "ts_utc": event.ts_utc,
        }

        curr_hash = cls.compute_hash(event_fields, prev_hash)

        result = tx.execute(
            audit_events.insert().values(
                event_id=event.event_id,
                ts_utc=event.ts_utc,
                request_id=event.request_id,
                session_id=event.session_id,
                actor_type=actor_type_str,
                actor_id=event.actor_id,
                event_type=event_type_str,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                action=event.action,
                outcome=outcome_str,
                policy_version=event.policy_version,
                details_json=details_json,
                prev_hash=prev_hash,
                hash=curr_hash,
            )
        )

        pk = result.inserted_primary_key
        if pk is not None and len(pk) > 0:
            return int(pk[0])
        raise RuntimeError("Failed to retrieve primary key for inserted audit event")

    @classmethod
    def verify_chain(cls, conn: Connection) -> ChainStatus:
        """Verify the cryptographic integrity of the audit events table.

        Recomputes hashes and verifies that every row points to the preceding
        hash, starting from genesis. Returns ChainStatus indicating whether the
        chain is intact, how many rows were checked, and the first broken seq if corrupted.
        """
        rows = conn.execute(
            text(
                "SELECT seq, event_id, ts_utc, request_id, session_id, "
                "actor_type, actor_id, event_type, resource_type, resource_id, "
                "action, outcome, policy_version, details_json, prev_hash, hash "
                "FROM audit_events ORDER BY seq ASC"
            )
        ).fetchall()

        if not rows:
            return ChainStatus(ok=True, checked=0, first_broken_seq=None)

        prev_hash = cls.GENESIS_HASH
        expected_seq = 1

        for idx, row in enumerate(rows):
            seq = row[0]
            if seq != expected_seq:
                return ChainStatus(ok=False, checked=idx, first_broken_seq=seq)

            row_prev_hash = row[14]
            row_hash = row[15]

            if row_prev_hash != prev_hash:
                return ChainStatus(ok=False, checked=idx, first_broken_seq=seq)

            event_fields = {
                "action": row[10],
                "actor_id": row[6],
                "actor_type": row[5],
                "details_json": row[13],
                "event_id": row[1],
                "event_type": row[7],
                "outcome": row[11],
                "policy_version": row[12],
                "request_id": row[3],
                "resource_id": row[9],
                "resource_type": row[8],
                "session_id": row[4],
                "ts_utc": row[2],
            }

            expected_hash = cls.compute_hash(event_fields, row_prev_hash)
            if row_hash != expected_hash:
                return ChainStatus(ok=False, checked=idx, first_broken_seq=seq)

            prev_hash = row_hash
            expected_seq += 1

        return ChainStatus(ok=True, checked=len(rows), first_broken_seq=None)

    @classmethod
    def reset_chain(cls, tx: Connection, now_iso: str | None = None) -> None:
        """Reset the audit chain and insert a verified seq=1 genesis reset event."""
        tx.execute(text("DELETE FROM audit_events"))
        try:
            tx.execute(text("DELETE FROM sqlite_sequence WHERE name='audit_events'"))
        except Exception:
            pass

        ts_utc = now_iso or "2026-11-09T00:00:00Z"
        event_id = f"evt_reset_{uuid.uuid4().hex[:12]}"
        details_json = json.dumps({"action": "dev_reset", "reason": "system_reset"}, sort_keys=True)
        event_fields: dict[str, Any] = {
            "action": "dev_reset",
            "actor_id": "seller_owner",
            "actor_type": "system",
            "details_json": details_json,
            "event_id": event_id,
            "event_type": "genesis",
            "outcome": "success",
            "policy_version": "v3",
            "request_id": "req-dev-reset",
            "resource_id": "audit_chain",
            "resource_type": "system",
            "session_id": None,
            "ts_utc": ts_utc,
        }
        genesis_hash = cls.compute_hash(event_fields, cls.GENESIS_HASH)

        tx.execute(
            audit_events.insert().values(
                seq=1,
                event_id=event_id,
                ts_utc=ts_utc,
                request_id="req-dev-reset",
                session_id=None,
                actor_type="system",
                actor_id="seller_owner",
                event_type="genesis",
                resource_type="system",
                resource_id="audit_chain",
                action="dev_reset",
                outcome="success",
                policy_version="v3",
                details_json=details_json,
                prev_hash=cls.GENESIS_HASH,
                hash=genesis_hash,
            )
        )

