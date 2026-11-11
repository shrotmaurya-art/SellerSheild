"""Short-transaction database helpers for store webhook ingestion and deduplication."""

from __future__ import annotations

import secrets

from sqlalchemy import Engine, select, update

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.clock import Clock
from sellershield_agent.db import write_tx
from sellershield_agent.domain.state import webhook_events_seen, webhook_resource_versions
from sellershield_agent.execution.common import format_utc_iso
from sellershield_agent.execution.db_ops import append_audit_in_tx


def record_webhook_auth_denied_sync(
    engine: Engine,
    *,
    request_id: str,
    clock: Clock,
    event_id_hint: str | None,
    reason: str,
    path: str,
) -> None:
    """Record generic webhook 401 authentication denial without logging secrets or signatures."""
    res_id = event_id_hint[:64] if event_id_hint else "store_webhook"
    with write_tx(engine) as tx:
        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=format_utc_iso(clock.now()),
                request_id=request_id,
                session_id=None,
                actor_type=ActorType.SYSTEM,
                actor_id="webhook_verifier",
                event_type=EventType.AUTH_DENIED,
                resource_type="webhooks",
                resource_id=res_id,
                action="webhook_auth_denied",
                outcome=Outcome.DENIED,
                policy_version=None,
                details={"reason": reason, "status_code": 401, "path": path},
            ),
        )


def ingest_webhook_event_sync(
    engine: Engine,
    *,
    event_id: str,
    event_type: str,
    resource_type: str,
    resource_id: str,
    resource_version: int,
    occurred_at_iso: str,
    request_id: str,
    clock: Clock,
) -> str:
    """Deduplicate by X-Event-Id, check resource_version monotonicity, and audit in one write_tx.

    Returns ``"duplicate"``, ``"stale_version"``, or ``"accepted"``.
    """
    now_iso = format_utc_iso(clock.now())
    res_type_trim = resource_type[:32]
    res_id_trim = resource_id[:64]
    eid_trim = event_id[:64]

    with write_tx(engine) as tx:
        seen = tx.execute(
            select(webhook_events_seen.c.event_id).where(
                webhook_events_seen.c.event_id == eid_trim
            )
        ).fetchone()
        if seen is not None:
            append_audit_in_tx(
                tx,
                clock=clock,
                request_id=request_id,
                session_id=None,
                actor_type=ActorType.SYSTEM,
                actor_id="webhook_receiver",
                event_type=EventType.WEBHOOK_RECEIVED,
                resource_type="webhooks",
                resource_id=eid_trim,
                action="webhook_duplicate_ignored",
                outcome=Outcome.DENIED,
                policy_version=None,
                details={
                    "event_type": event_type,
                    "target_type": res_type_trim,
                    "target_id": res_id_trim,
                    "resource_version": resource_version,
                },
            )
            return "duplicate"

        tx.execute(
            webhook_events_seen.insert().values(event_id=eid_trim, received_at=now_iso)
        )

        ver_row = tx.execute(
            select(webhook_resource_versions.c.resource_version).where(
                webhook_resource_versions.c.resource_type == res_type_trim,
                webhook_resource_versions.c.resource_id == res_id_trim,
            )
        ).fetchone()

        if ver_row is not None and resource_version < int(ver_row.resource_version):
            append_audit_in_tx(
                tx,
                clock=clock,
                request_id=request_id,
                session_id=None,
                actor_type=ActorType.SYSTEM,
                actor_id="webhook_receiver",
                event_type=EventType.WEBHOOK_RECEIVED,
                resource_type="webhooks",
                resource_id=eid_trim,
                action="webhook_stale_version",
                outcome=Outcome.DENIED,
                policy_version=None,
                details={
                    "event_type": event_type,
                    "target_type": res_type_trim,
                    "target_id": res_id_trim,
                    "resource_version": resource_version,
                    "seen_version": int(ver_row.resource_version),
                },
            )
            return "stale_version"

        if ver_row is None:
            tx.execute(
                webhook_resource_versions.insert().values(
                    resource_type=res_type_trim,
                    resource_id=res_id_trim,
                    resource_version=resource_version,
                    updated_at=now_iso,
                )
            )
        elif resource_version > int(ver_row.resource_version):
            tx.execute(
                update(webhook_resource_versions)
                .where(
                    webhook_resource_versions.c.resource_type == res_type_trim,
                    webhook_resource_versions.c.resource_id == res_id_trim,
                )
                .values(resource_version=resource_version, updated_at=now_iso)
            )

        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=None,
            actor_type=ActorType.SYSTEM,
            actor_id="webhook_receiver",
            event_type=EventType.WEBHOOK_RECEIVED,
            resource_type="webhooks",
            resource_id=eid_trim,
            action="webhook_received",
            outcome=Outcome.SUCCESS,
            policy_version=None,
            details={
                "event_type": event_type,
                "target_type": res_type_trim,
                "target_id": res_id_trim,
                "resource_version": resource_version,
                "occurred_at": occurred_at_iso,
            },
        )
        return "accepted"
