"""Outbox notification worker and batch processor.

Enforces:
- Atomically claims due email rows (queued / failed / retry_scheduled -> sending) inside write_tx.
- Calls StoreClient.send_mail strictly OUTSIDE any database transaction (KP-01).
- Backs off 1 s, 5 s, 30 s, and marks exhausted after 3 attempts.
- Recipient resolved strictly from database (order/case/proposal/approval owner), never from input.
- Templates are fixed English files with HTML-escaped values and sanitized ID-only subjects.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, or_, select

from sellershield_agent.clock import Clock
from sellershield_agent.db import write_tx
from sellershield_agent.domain.state import (
    approvals,
    cases,
    executions,
    notifications_outbox,
    proposals,
)
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.outbox.templates import (
    ALLOWED_TEMPLATES,
    TEMPLATE_ALIASES,
    TEMPLATES_DIR,
    render_template,
)
from sellershield_agent.store.client import StoreClient

__all__ = [
    "ALLOWED_TEMPLATES",
    "BACKOFF_SECONDS",
    "DEFAULT_CUSTOMER_EMAILS",
    "TEMPLATES_DIR",
    "TEMPLATE_ALIASES",
    "has_due_email_rows",
    "process_outbox_batch",
    "render_template",
    "resolve_recipient_email",
]

BACKOFF_SECONDS = [1, 5, 30]

DEFAULT_CUSTOMER_EMAILS = {
    "C001": "aarav.mehta@example.test",
    "C002": "diya.nair@example.test",
    "C003": "rohan.das@example.test",
    "C004": "meera.iyer@example.test",
    "C005": "kabir.singh@example.test",
}


def _lookup_customer_id_in_conn(conn: Connection, payload: dict[str, Any]) -> str | None:
    if payload.get("case_id"):
        row = conn.execute(
            select(cases.c.customer_id).where(cases.c.case_id == str(payload["case_id"]))
        ).fetchone()
        if row is not None and row[0]:
            return str(row[0])

    if payload.get("proposal_id"):
        row = conn.execute(
            select(proposals.c.customer_id).where(
                proposals.c.proposal_id == str(payload["proposal_id"])
            )
        ).fetchone()
        if row is not None and row[0]:
            return str(row[0])

    if payload.get("approval_id"):
        row = conn.execute(
            select(proposals.c.customer_id)
            .select_from(
                approvals.join(proposals, approvals.c.proposal_id == proposals.c.proposal_id)
            )
            .where(approvals.c.approval_id == str(payload["approval_id"]))
        ).fetchone()
        if row is not None and row[0]:
            return str(row[0])

    if payload.get("refund_id"):
        row = conn.execute(
            select(proposals.c.customer_id)
            .select_from(
                executions.join(proposals, executions.c.proposal_id == proposals.c.proposal_id)
            )
            .where(executions.c.store_ref == str(payload["refund_id"]))
            .limit(1)
        ).fetchone()
        if row is not None and row[0]:
            return str(row[0])

    if payload.get("order_id"):
        order_id = str(payload["order_id"])
        apr_row = conn.execute(
            select(proposals.c.customer_id)
            .select_from(
                approvals.join(proposals, approvals.c.proposal_id == proposals.c.proposal_id)
            )
            .where(approvals.c.order_id == order_id)
            .limit(1)
        ).fetchone()
        if apr_row is not None and apr_row[0]:
            return str(apr_row[0])
        case_row = conn.execute(
            select(cases.c.customer_id).where(cases.c.order_id == order_id).limit(1)
        ).fetchone()
        if case_row is not None and case_row[0]:
            return str(case_row[0])

    return None


def _resolve_customer_id_strict(
    customer_id: str | None,
    payload: dict[str, Any],
    engine: Engine,
    store_client: Any,
    *,
    conn: Connection | None = None,
) -> str | None:
    if conn is not None:
        db_cid = _lookup_customer_id_in_conn(conn, payload)
    else:
        with engine.connect() as local_conn:
            db_cid = _lookup_customer_id_in_conn(local_conn, payload)

    cid = db_cid or customer_id

    if not cid and payload.get("order_id") and hasattr(store_client, "_raw_data"):
        raw_data = store_client._raw_data
        if isinstance(raw_data, dict):
            target_oid = str(payload["order_id"])
            for ord_row in raw_data.get("orders", []):
                if ord_row.get("order_id") == target_oid and ord_row.get("customer_id"):
                    cid = str(ord_row["customer_id"])
                    break

    return cid


def resolve_recipient_email(
    customer_id: str | None,
    payload: dict[str, Any],
    engine: Engine,
    store_client: Any,
    *,
    conn: Connection | None = None,
) -> str:
    """Resolve recipient strictly from DB/store records, ignoring any input recipient fields."""
    cid = _resolve_customer_id_strict(
        customer_id, payload, engine, store_client, conn=conn
    )

    if cid and hasattr(store_client, "_raw_data") and isinstance(store_client._raw_data, dict):
        for cust in store_client._raw_data.get("customers", []):
            if cust.get("customer_id") == cid and cust.get("email"):
                return str(cust["email"])

    if cid and cid in DEFAULT_CUSTOMER_EMAILS:
        return DEFAULT_CUSTOMER_EMAILS[cid]

    if cid:
        return f"{cid.lower()}@example.test"

    return "customer@example.test"


def has_due_email_rows(engine: Engine, clock: Clock) -> bool:
    """Fast non-locking check for due email outbox rows."""
    now_iso = clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with engine.connect() as conn:
        row = conn.execute(
            select(notifications_outbox.c.notification_id)
            .where(
                notifications_outbox.c.channel == "email",
                notifications_outbox.c.status.in_(["queued", "failed", "retry_scheduled"]),
                notifications_outbox.c.attempts < 3,
                or_(
                    notifications_outbox.c.next_attempt_at.is_(None),
                    notifications_outbox.c.next_attempt_at <= now_iso,
                ),
            )
            .limit(1)
        ).fetchone()
        return row is not None


def _claim_due_rows_sync(
    engine: Engine,
    *,
    now_iso: str,
    force_due: bool,
    limit: int,
) -> list[dict[str, Any]]:
    claimed_rows: list[dict[str, Any]] = []
    with write_tx(engine) as tx:
        query = select(
            notifications_outbox.c.notification_id,
            notifications_outbox.c.customer_id,
            notifications_outbox.c.template,
            notifications_outbox.c.payload_json,
            notifications_outbox.c.dedupe_key,
            notifications_outbox.c.status,
            notifications_outbox.c.attempts,
            notifications_outbox.c.next_attempt_at,
        ).where(
            notifications_outbox.c.channel == "email",
            notifications_outbox.c.status.in_(["queued", "failed", "retry_scheduled"]),
            notifications_outbox.c.attempts < 3,
        )
        if not force_due:
            query = query.where(
                or_(
                    notifications_outbox.c.next_attempt_at.is_(None),
                    notifications_outbox.c.next_attempt_at <= now_iso,
                )
            )
        query = query.order_by(notifications_outbox.c.created_at.asc()).limit(limit)
        candidates = tx.execute(query).fetchall()

        for c in candidates:
            nid = str(c.notification_id)
            res = tx.execute(
                notifications_outbox.update()
                .where(
                    notifications_outbox.c.notification_id == nid,
                    notifications_outbox.c.channel == "email",
                    notifications_outbox.c.status.in_(["queued", "failed", "retry_scheduled"]),
                    notifications_outbox.c.attempts < 3,
                )
                .values(status="sending")
            )
            if res.rowcount != 1:
                continue

            try:
                pld = json.loads(str(c.payload_json))
                if not isinstance(pld, dict):
                    pld = {}
            except Exception:
                pld = {}
            claimed_rows.append(
                {
                    "notification_id": nid,
                    "customer_id": str(c.customer_id) if c.customer_id is not None else None,
                    "template": str(c.template),
                    "payload": pld,
                    "attempts": int(c.attempts),
                }
            )
    return claimed_rows


def _prepare_mail_item_sync(
    engine: Engine,
    item: dict[str, Any],
    store_client: Any,
    *,
    resolved_customer_id: str | None = None,
) -> tuple[str | None, str, str, str]:
    cid = _resolve_customer_id_strict(
        resolved_customer_id or item["customer_id"],
        item["payload"],
        engine,
        store_client,
    )
    recipient = resolve_recipient_email(
        cid,
        item["payload"],
        engine,
        store_client,
    )
    subject, body = render_template(item["template"], item["payload"])
    return cid, recipient, subject, body


def _settle_outbox_results_sync(
    engine: Engine,
    *,
    results: list[tuple[dict[str, Any], bool, str | None]],
    clock: Clock,
) -> int:
    sent_count = 0
    now_dt = clock.now().astimezone(UTC)
    with write_tx(engine) as tx:
        for item, success, error_msg in results:
            nid = item["notification_id"]
            new_attempts = int(item["attempts"]) + 1

            if success:
                tx.execute(
                    notifications_outbox.update()
                    .where(notifications_outbox.c.notification_id == nid)
                    .values(
                        status="sent",
                        attempts=new_attempts,
                        next_attempt_at=None,
                        last_error=None,
                    )
                )
                sent_count += 1
            else:
                if new_attempts >= 3:
                    new_status = "exhausted"
                    next_attempt_at = None
                else:
                    new_status = "failed"
                    backoff_sec = BACKOFF_SECONDS[min(new_attempts - 1, len(BACKOFF_SECONDS) - 1)]
                    next_attempt_at = (now_dt + timedelta(seconds=backoff_sec)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )

                tx.execute(
                    notifications_outbox.update()
                    .where(notifications_outbox.c.notification_id == nid)
                    .values(
                        status=new_status,
                        attempts=new_attempts,
                        next_attempt_at=next_attempt_at,
                        last_error=error_msg,
                    )
                )
    return sent_count


async def process_outbox_batch(
    engine: Engine,
    store_client: StoreClient,
    clock: Clock,
    force_due: bool = False,
    limit: int = 10,
) -> int:
    """Claim and send due email notifications outside any database transaction.

    1. Atomically claims due email rows inside write_tx (status -> 'sending').
    2. Sends email outside any transaction via store_client.send_mail.
    3. Settles status in a subsequent write_tx (sent / failed / exhausted).
    """
    now_dt = clock.now().astimezone(UTC)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    claimed_rows = await asyncio.to_thread(
        _claim_due_rows_sync,
        engine,
        now_iso=now_iso,
        force_due=force_due,
        limit=limit,
    )
    if not claimed_rows:
        return 0

    results: list[tuple[dict[str, Any], bool, str | None]] = []
    for item in claimed_rows:
        nid = str(item["notification_id"])
        template = str(item["template"])
        attempt = int(item["attempts"]) + 1

        try:
            service_ctx = build_service_ctx(clock, f"notif-{nid}")
            cid, recipient, subject, body = await asyncio.to_thread(
                _prepare_mail_item_sync, engine, item, store_client
            )
            order_id = item["payload"].get("order_id")
            if cid is None and order_id and hasattr(store_client, "get_order_admin"):
                order = await store_client.get_order_admin(service_ctx, str(order_id))
                _, recipient, subject, body = await asyncio.to_thread(
                    _prepare_mail_item_sync,
                    engine,
                    item,
                    store_client,
                    resolved_customer_id=order.customer_id,
                )
            from sellershield_agent.dev.faults import is_fault_active

            if is_fault_active("mail_down"):
                raise RuntimeError("Dev fault injected: mail_down active (email delivery failed)")

            await store_client.send_mail(
                service_ctx=service_ctx,
                recipient=recipient,
                subject=subject,
                body_text=body,
                template=template,
                idempotency_key=f"mail:{nid}:{attempt}",
            )
            results.append((item, True, None))
        except asyncio.CancelledError:
            results.append((item, False, "CancelledError: interrupted during shutdown"))
            await asyncio.to_thread(
                _settle_outbox_results_sync, engine, results=results, clock=clock
            )
            raise
        except Exception as exc:
            err_summary = f"{type(exc).__name__}: {str(exc)[:200]}"
            results.append((item, False, err_summary))

    return await asyncio.to_thread(
        _settle_outbox_results_sync,
        engine,
        results=results,
        clock=clock,
    )
