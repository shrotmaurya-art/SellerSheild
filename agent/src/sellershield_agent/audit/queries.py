"""Database query helpers for immutable audit log and cryptographic verification."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select, text

from sellershield_agent.audit.models import audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.db import read_tx
from sellershield_agent.errors import AppHTTPException


def _parse_filter_iso(ts_str: str, field_name: str) -> datetime:
    """Parse ISO 8601 string to timezone-aware UTC datetime or raise 422."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message=f"Invalid timestamp format for '{field_name}'",
            details={"field": field_name, "value": ts_str},
        ) from None


def query_audit_events_sync(
    engine: Engine,
    *,
    limit: int = 50,
    cursor: str | None = None,
    request_id: str | None = None,
    resource_id: str | None = None,
    event_type: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> dict[str, Any]:
    """Query audit events with validation, filters, and keyset cursor pagination."""
    if limit < 1 or limit > 200:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="Limit must be between 1 and 200",
            details={"field": "limit", "value": limit},
        )

    cursor_seq: int | None = None
    if cursor is not None:
        try:
            cursor_seq = int(cursor)
            if cursor_seq < 0:
                raise ValueError("Cursor must be non-negative")
        except Exception:
            raise AppHTTPException(
                status_code=422,
                code="validation_error",
                message="Invalid cursor format",
                details={"field": "cursor", "value": cursor},
            ) from None

    from_dt: datetime | None = None
    to_dt: datetime | None = None
    if from_ts is not None:
        from_dt = _parse_filter_iso(from_ts, "from")
    if to_ts is not None:
        to_dt = _parse_filter_iso(to_ts, "to")
    if from_dt is not None and to_dt is not None and from_dt > to_dt:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message="'from' timestamp must be <= 'to' timestamp",
            details={"field": "from", "from": from_ts, "to": to_ts},
        )

    stmt = select(audit_events).order_by(audit_events.c.seq.asc())
    if cursor_seq is not None:
        stmt = stmt.where(audit_events.c.seq > cursor_seq)
    if request_id:
        stmt = stmt.where(audit_events.c.request_id == request_id)
    if resource_id:
        stmt = stmt.where(audit_events.c.resource_id == resource_id)
    if event_type:
        stmt = stmt.where(audit_events.c.event_type == event_type)
    if from_dt:
        stmt = stmt.where(audit_events.c.ts_utc >= from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if to_dt:
        stmt = stmt.where(audit_events.c.ts_utc <= to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Fetch limit + 1 to detect next page
    stmt = stmt.limit(limit + 1)

    with read_tx(engine) as conn:
        rows = conn.execute(stmt).fetchall()

    next_cursor: str | None = None
    if len(rows) > limit:
        next_cursor = str(rows[limit - 1].seq)
        rows = rows[:limit]

    events: list[dict[str, Any]] = []
    for r in rows:
        details_val: dict[str, Any] = {}
        if r.details_json:
            try:
                details_val = json.loads(str(r.details_json))
            except Exception:
                details_val = {"raw": str(r.details_json)}

        events.append(
            {
                "seq": int(r.seq),
                "event_id": str(r.event_id),
                "event_type": str(r.event_type),
                "actor": str(r.actor_id or r.actor_type),
                "request_id": str(r.request_id or ""),
                "hash": str(r.hash),
                "prev_hash": str(r.prev_hash),
                "details": details_val,
                "created_at": str(r.ts_utc),
            }
        )

    return {"events": events, "next_cursor": next_cursor}


def verify_audit_chain_report_sync(engine: Engine) -> dict[str, Any]:
    """Verify SHA-256 audit hash chain integrity and return summary report."""
    with read_tx(engine) as conn:
        total_events = conn.execute(text("SELECT count(*) FROM audit_events")).scalar() or 0
        last_hash = conn.execute(
            text("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1")
        ).scalar() or ("0" * 64)
        status = AuditService.verify_chain(conn)

    error_msg: str | None = None
    if not status.ok:
        error_msg = f"Hash chain verification failed at sequence {status.first_broken_seq}"

    return {
        "verified": status.ok,
        "total_events": total_events,
        "head_hash": last_hash,
        "broken_seq": status.first_broken_seq,
        "error": error_msg,
    }

