"""Proactive Delay Monitor service (Spec §5 W2/W9, §9.5, §13, §21).

Scans in-transit and exception shipments using simulated clock:
- Flags delayed shipments (ETA < now) or carrier exceptions.
- Deduplicates using unique monitor_events keys.
- Opens high-priority cases for lost shipments (recommended: 'reship or refund with approval')
  and carrier exceptions >= 5 days (recommended: 'wait or cancel').
- Queues notifications in outbox and emits customer client_events with guided action chips.
- Run lock serializes concurrent runs, returning running run_id without duplicate scanning.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from sellershield_agent.audit.models import ActorType, AuditEvent, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth.models import Role
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine, write_tx
from sellershield_agent.domain.state import (
    case_events,
    cases,
    client_events,
    generate_case_event_id,
    generate_case_id,
    generate_client_event_id,
    generate_notification_id,
    monitor_events,
    monitor_runs,
    notifications_outbox,
)
from sellershield_agent.logging import get_logger
from sellershield_agent.store.client import RequestContext, StoreClient, StoreForbidden

logger = get_logger("sellershield_agent.monitor.service")

FORBIDDEN_WORDS_RE = re.compile(
    r"\b(?:limits?|thresholds?|risks?|scores?|staff|owner|auto(?:matic|nomy)?|caps?)\b",
    re.IGNORECASE,
)


@dataclass
class RunMonitorResult:
    run_id: str
    status: str
    scanned: int
    flagged: int
    notified: int
    skipped_duplicates: int
    cases_created: int
    actions_taken: list[str]
    created_at: str


_run_lock = asyncio.Lock()
_active_run_id: str | None = None
_active_run_started_at: str | None = None


def _sanitize_for_notice(val: str | None) -> str:
    """Strip script tags, img tags, and PII from carrier notes and strings."""
    if not val:
        return ""
    clean = re.sub(r"<[^>]+>", "", val)
    return clean.strip()


async def run_monitor(
    service_ctx: RequestContext,
    clock: Clock,
    *,
    store_client: StoreClient,
    engine: Engine | None = None,
) -> RunMonitorResult:
    """Execute proactive monitor scan across store shipments."""
    global _active_run_id, _active_run_started_at

    if service_ctx.principal.role != Role.service or service_ctx.token is not None:
        raise StoreForbidden("Monitor shipment listing requires service context")

    now_dt = clock.now().astimezone(UTC)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Fast-return if another concurrent run is already executing
    if _run_lock.locked() and _active_run_id is not None:
        return RunMonitorResult(
            run_id=_active_run_id,
            status="running",
            scanned=0,
            flagged=0,
            notified=0,
            skipped_duplicates=0,
            cases_created=0,
            actions_taken=[],
            created_at=_active_run_started_at or now_iso,
        )

    async with _run_lock:
        _active_run_id = f"MON-{secrets.token_hex(4).upper()}"
        _active_run_started_at = now_iso
        run_id = _active_run_id

        try:
            resolved_engine = engine or get_engine()

            # 1. Fetch shipments from store client (up to 200 cap)
            all_shipments: list[Any] = []
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for _ in range(10):  # loop guard
                remaining = max(1, 200 - len(all_shipments))
                res = await store_client.list_shipments_for_monitor(
                    service_ctx,
                    statuses=["in_transit", "exception", "lost"],
                    limit=remaining,
                    cursor=cursor,
                )
                shipment_batch = getattr(res, "shipments", [])
                all_shipments.extend(shipment_batch)
                next_cur = getattr(res, "next_cursor", None)
                if (
                    not next_cur
                    or next_cur in seen_cursors
                    or len(all_shipments) >= 200
                    or not shipment_batch
                ):
                    break
                seen_cursors.add(next_cur)
                cursor = next_cur

            scanned_shipments = all_shipments[:200]

            # Pre-resolve customer_id for shipments lacking it before opening write_tx
            customer_by_order: dict[str, str] = {}
            for s in scanned_shipments:
                cid = getattr(s, "customer_id", "")
                oid = getattr(s, "order_id", "")
                if not cid and oid and oid not in customer_by_order:
                    try:
                        ord_obj = await store_client.get_order_admin(service_ctx, oid)
                        if ord_obj and hasattr(ord_obj, "customer_id"):
                            customer_by_order[oid] = str(ord_obj.customer_id)
                    except Exception:
                        pass

            scanned = 0
            flagged = 0
            notified = 0
            skipped_duplicates = 0
            cases_created = 0
            actions_taken: list[str] = []

            with write_tx(resolved_engine) as conn:
                processed_keys_in_run: set[str] = set()
                for shp in scanned_shipments:
                    scanned += 1
                    status = str(getattr(shp, "status", "")).lower()
                    if status in ("delivered", "rto"):
                        continue

                    order_id = str(getattr(shp, "order_id", ""))
                    raw_cid = getattr(shp, "customer_id", "")
                    fallback_cid = customer_by_order.get(order_id, "")
                    customer_id = str(raw_cid or fallback_cid)
                    carrier = _sanitize_for_notice(
                        getattr(shp, "carrier", "SwiftShip")
                    )
                    tracking_no = _sanitize_for_notice(
                        getattr(shp, "tracking_no", "")
                    )
                    eta_raw = getattr(shp, "eta_at", None)

                    if isinstance(eta_raw, datetime):
                        eta_dt = (
                            eta_raw
                            if eta_raw.tzinfo is not None
                            else eta_raw.replace(tzinfo=UTC)
                        )
                    elif isinstance(eta_raw, str):
                        clean_eta = eta_raw.replace("Z", "+00:00")
                        dt_parsed = datetime.fromisoformat(clean_eta)
                        eta_dt = (
                            dt_parsed
                            if dt_parsed.tzinfo is not None
                            else dt_parsed.replace(tzinfo=UTC)
                        )
                    else:
                        eta_dt = None

                    if eta_dt:
                        eta_date_str = eta_dt.strftime("%Y-%m-%d")
                        eta_iso_str = eta_dt.strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        )
                    else:
                        eta_date_str = now_dt.strftime("%Y-%m-%d")
                        eta_iso_str = now_iso

                    # Check delay calculations
                    is_late = bool(eta_dt and eta_dt < now_dt)
                    if eta_dt and is_late:
                        days_late = (
                            (now_dt - eta_dt).total_seconds() / 86400.0
                        )
                    else:
                        days_late = 0.0

                    if status == "lost":
                        flagged += 1
                        kind = "lost"
                        dedupe_key = f"delay:{order_id}:{eta_date_str}:{kind}"
                        event_key = f"{order_id}:{eta_date_str}:{kind}"

                        # Check if already processed in this run or past runs
                        if event_key in processed_keys_in_run:
                            skipped_duplicates += 1
                            continue

                        existing_ev = conn.execute(
                            select(monitor_events.c.key).where(monitor_events.c.key == event_key)
                        ).fetchone()
                        if existing_ev is not None:
                            skipped_duplicates += 1
                            processed_keys_in_run.add(event_key)
                            continue

                        processed_keys_in_run.add(event_key)
                        # Record new lost shipment actions
                        notified += 1
                        cases_created += 1
                        actions_taken.append(f"case:{order_id}")

                        # 1. Outbox email notification
                        conn.execute(
                            notifications_outbox.insert().values(
                                notification_id=generate_notification_id(),
                                channel="email",
                                customer_id=customer_id,
                                template="delay.notice",
                                payload_json=json.dumps(
                                    {
                                        "customer_id": customer_id,
                                        "order_id": order_id,
                                        "carrier": carrier,
                                        "tracking_no": tracking_no,
                                        "eta": eta_date_str,
                                        "language": "en",
                                    }
                                ),
                                dedupe_key=dedupe_key,
                                status="queued",
                                attempts=0,
                                created_at=now_iso,
                            )
                        )

                        # 2. Client event
                        conn.execute(
                            client_events.insert().values(
                                event_id=generate_client_event_id(),
                                customer_id=customer_id,
                                type="monitor.notice",
                                payload_json=json.dumps(
                                    {
                                        "order_id": order_id,
                                        "kind": kind,
                                        "message": f"Update on {order_id}: shipment is delayed.",
                                        "eta_at": eta_iso_str,
                                        "carrier": carrier,
                                        "tracking_number": tracking_no,
                                    }
                                ),
                                created_at=now_iso,
                            )
                        )

                        # 3. High-priority case
                        case_id = generate_case_id()
                        conn.execute(
                            cases.insert().values(
                                case_id=case_id,
                                customer_id=customer_id,
                                order_id=order_id,
                                session_id=None,
                                category="lost_shipment",
                                priority="high",
                                status="open",
                                handler="agent",
                                brief_json=json.dumps(
                                    {
                                        "recommended_next_step": "reship or refund with approval",
                                        "order_id": order_id,
                                        "customer_id": customer_id,
                                        "category": "lost_shipment",
                                        "carrier": carrier,
                                        "tracking_no": tracking_no,
                                    }
                                ),
                                sla_due_at=(
                                    now_dt + datetime.resolution
                                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                created_at=now_iso,
                                version=1,
                            )
                        )
                        conn.execute(
                            case_events.insert().values(
                                event_id=generate_case_event_id(),
                                case_id=case_id,
                                actor_type="system",
                                actor_id="agent_monitor",
                                kind="case_opened",
                                text=(
                                    "Case opened automatically by proactive"
                                    " delay monitor for lost shipment"
                                ),
                                created_at=now_iso,
                            )
                        )

                        # 4. Monitor event deduplication row
                        conn.execute(
                            monitor_events.insert().values(
                                key=event_key,
                                run_id=run_id,
                                order_id=order_id,
                                customer_id=customer_id,
                                kind=kind,
                                created_at=now_iso,
                            )
                        )

                    elif status == "exception" or (
                        status == "in_transit"
                        and is_late
                        and days_late >= 2.0
                    ):
                        flagged += 1
                        kind = "options"
                        dedupe_key = f"delay:{order_id}:{eta_date_str}:{kind}"
                        event_key = f"{order_id}:{eta_date_str}:{kind}"

                        if event_key in processed_keys_in_run:
                            skipped_duplicates += 1
                            continue

                        existing_ev = conn.execute(
                            select(monitor_events.c.key).where(monitor_events.c.key == event_key)
                        ).fetchone()

                        if existing_ev is not None:
                            # Already notified. Check if newly escalated to >= 5 days exception
                            skipped_duplicates += 1
                            processed_keys_in_run.add(event_key)
                            if status == "exception" and days_late >= 5.0:
                                existing_case = conn.execute(
                                    select(cases.c.case_id).where(
                                        cases.c.order_id == order_id,
                                        cases.c.category == "delivery_delay",
                                    )
                                ).fetchone()
                                if existing_case is None:
                                    case_id = generate_case_id()
                                    conn.execute(
                                        cases.insert().values(
                                            case_id=case_id,
                                            customer_id=customer_id,
                                            order_id=order_id,
                                            session_id=None,
                                            category="delivery_delay",
                                            priority="high",
                                            status="open",
                                            handler="agent",
                                            brief_json=json.dumps(
                                                {
                                                    "recommended_next_step": "wait or cancel",
                                                    "order_id": order_id,
                                                    "customer_id": customer_id,
                                                    "category": "delivery_delay",
                                                    "carrier": carrier,
                                                    "tracking_no": tracking_no,
                                                }
                                            ),
                                            created_at=now_iso,
                                            version=1,
                                        )
                                    )
                                    conn.execute(
                                        case_events.insert().values(
                                            event_id=generate_case_event_id(),
                                            case_id=case_id,
                                            actor_type="system",
                                            actor_id="agent_monitor",
                                            kind="case_opened",
                                            text=(
                                                "Case opened automatically by proactive"
                                                " delay monitor for delivery delay"
                                            ),
                                            created_at=now_iso,
                                        )
                                    )
                                    cases_created += 1
                                    actions_taken.append(f"case:{order_id}")
                            continue

                        # First time flagged
                        notified += 1
                        actions_taken.append(f"notice:{order_id}")

                        # Check if exception >= 5 days opens case immediately
                        if status == "exception" and days_late >= 5.0:
                            case_id = generate_case_id()
                            conn.execute(
                                cases.insert().values(
                                    case_id=case_id,
                                    customer_id=customer_id,
                                    order_id=order_id,
                                    session_id=None,
                                    category="delivery_delay",
                                    priority="high",
                                    status="open",
                                    handler="agent",
                                    brief_json=json.dumps(
                                        {
                                            "recommended_next_step": "wait or cancel",
                                            "order_id": order_id,
                                            "customer_id": customer_id,
                                            "category": "delivery_delay",
                                            "carrier": carrier,
                                            "tracking_no": tracking_no,
                                        }
                                    ),
                                    created_at=now_iso,
                                    version=1,
                                )
                            )
                            conn.execute(
                                case_events.insert().values(
                                    event_id=generate_case_event_id(),
                                    case_id=case_id,
                                    actor_type="system",
                                    actor_id="agent_monitor",
                                    kind="case_opened",
                                    text=(
                                        "Case opened automatically by proactive"
                                        " delay monitor for delivery delay"
                                    ),
                                    created_at=now_iso,
                                )
                            )
                            cases_created += 1
                            actions_taken.append(f"case:{order_id}")

                        # Guided action chips
                        chips = [
                            {
                                "label": "Wait",
                                "action": "track_order",
                                "order_id": order_id,
                            },
                            {
                                "label": "Cancel if not delivered",
                                "action": "talk_to_human",
                                "order_id": order_id,
                            },
                            {
                                "label": "Talk to support",
                                "action": "talk_to_human",
                                "order_id": order_id,
                            },
                        ]

                        # 1. Outbox notification
                        conn.execute(
                            notifications_outbox.insert().values(
                                notification_id=generate_notification_id(),
                                channel="email",
                                customer_id=customer_id,
                                template="delay.notice",
                                payload_json=json.dumps(
                                    {
                                        "customer_id": customer_id,
                                        "order_id": order_id,
                                        "carrier": carrier,
                                        "tracking_no": tracking_no,
                                        "eta": eta_date_str,
                                        "language": "en",
                                    }
                                ),
                                dedupe_key=dedupe_key,
                                status="queued",
                                attempts=0,
                                created_at=now_iso,
                            )
                        )

                        # 2. Client event with chips
                        conn.execute(
                            client_events.insert().values(
                                event_id=generate_client_event_id(),
                                customer_id=customer_id,
                                type="monitor.notice",
                                payload_json=json.dumps(
                                    {
                                        "order_id": order_id,
                                        "kind": kind,
                                        "message": f"Update on {order_id}: shipment is delayed.",
                                        "eta_at": eta_iso_str,
                                        "carrier": carrier,
                                        "tracking_number": tracking_no,
                                        "chips": chips,
                                    }
                                ),
                                created_at=now_iso,
                            )
                        )

                        # 3. Monitor event deduplication row
                        conn.execute(
                            monitor_events.insert().values(
                                key=event_key,
                                run_id=run_id,
                                order_id=order_id,
                                customer_id=customer_id,
                                kind=kind,
                                created_at=now_iso,
                            )
                        )
                        processed_keys_in_run.add(event_key)

                # Persist monitor_runs record
                summary_data = {"actions_taken": actions_taken}
                conn.execute(
                    monitor_runs.insert().values(
                        run_id=run_id,
                        status="completed",
                        started_at=now_iso,
                        finished_at=clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        scanned=scanned,
                        flagged=flagged,
                        notified=notified,
                        skipped_duplicates=skipped_duplicates,
                        cases_created=cases_created,
                        summary_json=json.dumps(summary_data),
                    )
                )

                # Append audit record
                AuditService.append(
                    conn,
                    AuditEvent(
                        event_id=f"evt_mon_{uuid.uuid4().hex[:12]}",
                        ts_utc=now_iso,
                        request_id=service_ctx.request_id,
                        session_id=None,
                        actor_type=ActorType.SYSTEM,
                        actor_id="agent_monitor",
                        event_type="monitor_run",
                        resource_type="monitor",
                        resource_id=run_id,
                        action="monitor_run",
                        outcome=Outcome.SUCCESS,
                        policy_version="v3",
                        details={
                            "scanned": scanned,
                            "flagged": flagged,
                            "notified": notified,
                            "cases_created": cases_created,
                        },
                    ),
                )

            return RunMonitorResult(
                run_id=run_id,
                status="completed",
                scanned=scanned,
                flagged=flagged,
                notified=notified,
                skipped_duplicates=skipped_duplicates,
                cases_created=cases_created,
                actions_taken=actions_taken,
                created_at=now_iso,
            )
        finally:
            _active_run_id = None
            _active_run_started_at = None
