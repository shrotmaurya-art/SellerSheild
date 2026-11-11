"""
Transactional Outbox Delivery Worker (spec section 7.4, 6, 8, 16)

- Sends events from events_outbox to AGENT_WEBHOOK_URL with headers:
    X-Store-Signature: HMAC-SHA256 of 'timestamp.body' using WEBHOOK_SECRET
    X-Store-Timestamp: UTC timestamp string
    X-Event-Id: event_id
- Every payload carries resource_version.
- Events:
    order.created, order.updated,
    shipment.updated, shipment.delayed,
    return.updated, refund.updated,
    mail.inbound.
- Retry delays: 1 s, 5 s, 30 s, 2 min, 10 min; after 5 failures mark the event exhausted.
- At-least-once delivery.
- If the agent is down, nothing else in the store may break.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

import db
from app.clock import Clock
from app.config import settings
from app.models import EventsOutbox

logger = logging.getLogger(__name__)

# Retry delays in seconds: 1 s, 5 s, 30 s, 2 min (120 s), 10 min (600 s)
RETRY_DELAYS = [1, 5, 30, 120, 600]
MAX_FAILURES = 5


def compute_signature(timestamp: str, body: str, secret: Optional[str] = None) -> str:
    """Compute HMAC-SHA256 signature of 'timestamp.body'."""
    sec = secret if secret is not None else settings.WEBHOOK_SECRET
    message = f"{timestamp}.{body}".encode("utf-8")
    return hmac.new(sec.encode("utf-8"), message, hashlib.sha256).hexdigest()


def get_retry_delay(attempts: int) -> Optional[int]:
    """
    Return delay in seconds for the given failure attempt number (1-indexed).
    attempts 1 -> 1s
    attempts 2 -> 5s
    attempts 3 -> 30s
    attempts 4 -> 120s (2 min)
    attempts 5 -> 600s (10 min)
    attempts > 5 -> None (exhausted)
    """
    idx = attempts - 1
    if 0 <= idx < len(RETRY_DELAYS):
        return RETRY_DELAYS[idx]
    return None


def emit_outbox_event(
    session: Session,
    event_type: str,
    payload: dict[str, Any],
    resource_version: int,
    event_id: Optional[str] = None,
) -> EventsOutbox:
    """
    Write an outbox event in the SAME transaction as the business change.
    Guarantees resource_version, type, and timestamp are included in payload.
    """
    if event_id is None:
        event_id = f"evt_{uuid.uuid4().hex[:12]}"

    now = Clock.now()
    naive_now = now.replace(tzinfo=None) if hasattr(now, "tzinfo") else now

    # Ensure required envelope fields
    payload["event_id"] = event_id
    payload["type"] = event_type
    payload["resource_version"] = resource_version
    payload["timestamp"] = now.isoformat()

    payload_json = json.dumps(payload, default=str)

    entry = EventsOutbox(
        event_id=event_id,
        type=event_type,
        payload_json=payload_json,
        created_at=naive_now,
        delivered_at=None,
        attempts=0,
        status="pending",
        next_retry_at=naive_now,
        last_error=None,
    )
    session.add(entry)
    logger.info(f"Emitted {event_type} event {event_id} (version {resource_version}) to outbox")
    return entry


def build_webhook_wire_body(event: EventsOutbox) -> str:
    """Serialize an outbox row into a WebhookEnvelope JSON string for HTTP delivery."""
    raw: dict[str, Any]
    try:
        parsed = json.loads(event.payload_json)
        raw = parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        raw = {}

    event_id = str(raw.get("event_id") or event.event_id)
    ev_type = str(raw.get("type") or event.type or "unknown")
    try:
        resource_version = int(raw.get("resource_version", 1))
    except (TypeError, ValueError):
        resource_version = 1

    occ_raw = raw.get("occurred_at") or raw.get("timestamp")
    occ_dt: Optional[datetime] = None
    if isinstance(occ_raw, str) and occ_raw:
        try:
            occ_dt = datetime.fromisoformat(occ_raw.replace("Z", "+00:00"))
        except ValueError:
            occ_dt = None
    if occ_dt is None:
        occ_dt = event.created_at if event.created_at is not None else Clock.now()
    if occ_dt.tzinfo is None:
        occ_dt = occ_dt.replace(tzinfo=timezone.utc)
    else:
        occ_dt = occ_dt.astimezone(timezone.utc)

    data = {
        k: v
        for k, v in raw.items()
        if k not in ("event_id", "type", "resource_version", "timestamp", "occurred_at")
    }
    envelope = {
        "event_id": event_id,
        "type": ev_type,
        "occurred_at": occ_dt.isoformat().replace("+00:00", "Z"),
        "resource_version": resource_version,
        "data": data,
    }
    return json.dumps(envelope, default=str)


async def send_event_http(
    event: EventsOutbox,
    webhook_url: Optional[str] = None,
    secret: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 3.0,
) -> tuple[bool, Optional[str]]:
    """
    Send an outbox event via HTTP POST to the agent webhook endpoint.
    Returns (success: bool, error_message: Optional[str]).
    """
    url = webhook_url or settings.AGENT_WEBHOOK_URL
    ts_str = str(int(Clock.now().timestamp()))
    wire_body = build_webhook_wire_body(event)
    sig = compute_signature(ts_str, wire_body, secret=secret)

    headers = {
        "X-Store-Signature": sig,
        "X-Store-Timestamp": ts_str,
        "X-Event-Id": event.event_id,
        "Content-Type": "application/json",
    }

    try:
        if client is not None:
            resp = await client.post(
                url, content=wire_body, headers=headers, timeout=timeout
            )
        else:
            async with httpx.AsyncClient() as c:
                resp = await c.post(
                    url, content=wire_body, headers=headers, timeout=timeout
                )

        if 200 <= resp.status_code < 300:
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
    except Exception as exc:
        return False, str(exc)


async def process_outbox_once(
    webhook_url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> int:
    """
    Process all eligible pending events in events_outbox.
    Returns number of events processed.
    Safe against agent outages: errors never propagate outside outbox worker.
    """
    now = Clock.now()
    naive_now = now.replace(tzinfo=None) if hasattr(now, "tzinfo") else now

    events_to_process: list[tuple[str, str, int]] = []
    with Session(db.engine) as session:
        # Select pending events where next_retry_at <= now
        stmt = (
            select(EventsOutbox)
            .where(
                EventsOutbox.delivered_at.is_(None),
                EventsOutbox.status == "pending",
                (EventsOutbox.next_retry_at.is_(None) | (EventsOutbox.next_retry_at <= naive_now)),
            )
            .order_by(EventsOutbox.created_at.asc())
        )
        rows = session.scalars(stmt).all()
        for r in rows:
            events_to_process.append((r.event_id, r.payload_json, r.attempts))

    if not events_to_process:
        return 0

    processed_count = 0
    for event_id, payload_json, attempts in events_to_process:
        # Create a mock/stub object for sending
        dummy = EventsOutbox(event_id=event_id, payload_json=payload_json, attempts=attempts)
        success, err = await send_event_http(dummy, webhook_url=webhook_url, client=client)

        curr_now = Clock.now()
        curr_naive = curr_now.replace(tzinfo=None) if hasattr(curr_now, "tzinfo") else curr_now

        with Session(db.engine) as session:
            row = session.get(EventsOutbox, event_id)
            if not row or row.delivered_at is not None:
                continue

            if success:
                row.delivered_at = curr_naive
                row.status = "delivered"
                row.attempts += 1
                row.last_error = None
                row.next_retry_at = None
                logger.info(f"Delivered webhook event {event_id} (attempts: {row.attempts})")
            else:
                row.attempts += 1
                row.last_error = err
                delay = get_retry_delay(row.attempts)
                if delay is not None:
                    row.next_retry_at = curr_naive + timedelta(seconds=delay)
                    logger.warning(
                        f"Failed webhook delivery for {event_id} (attempt {row.attempts}): {err}. "
                        f"Next retry in {delay}s"
                    )
                else:
                    # After 5 failures mark the event exhausted
                    row.status = "exhausted"
                    row.next_retry_at = None
                    logger.error(
                        f"Webhook event {event_id} EXHAUSTED after {row.attempts} attempts: {err}"
                    )

            session.commit()
            processed_count += 1

    return processed_count


async def run_outbox_worker_loop(interval_seconds: float = 1.0) -> None:
    """Background asyncio worker loop for delivering outbox events."""
    logger.info("Starting transactional outbox worker loop")
    while True:
        try:
            await process_outbox_once()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"Outbox worker encountered error: {exc}")
        await asyncio.sleep(interval_seconds)
