"""Outbox notification queueing and deduplication."""

from __future__ import annotations

import json
from datetime import UTC
from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.exc import IntegrityError

from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.domain.state import (
    generate_notification_id,
    notifications_outbox,
)


def enqueue_notification(
    tx: Connection,
    customer_id: str | None,
    template: str,
    payload: dict[str, Any] | str,
    dedupe_key: str,
    channel: str = "email",
    now_iso: str | None = None,
    *,
    clock: Clock | None = None,
) -> str:
    """Enqueue a notification row in notifications_outbox.

    If a notification with dedupe_key already exists, returns the existing notification_id.
    """
    if not dedupe_key or not isinstance(dedupe_key, str):
        raise ValueError("dedupe_key must be a non-empty string")
    if not template or not isinstance(template, str):
        raise ValueError("template must be a non-empty string")

    # Check for existing dedupe_key
    existing = tx.execute(
        select(notifications_outbox.c.notification_id).where(
            notifications_outbox.c.dedupe_key == dedupe_key
        )
    ).fetchone()
    if existing is not None:
        return str(existing[0])

    # Validate and serialize payload
    if isinstance(payload, (dict, list)):
        try:
            payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"Payload could not be JSON serialized: {exc}") from exc
    elif isinstance(payload, str):
        try:
            json.loads(payload)
            payload_json = payload
        except Exception as exc:
            raise ValueError(f"Payload string is not valid JSON: {exc}") from exc
    else:
        type_name = type(payload).__name__
        raise TypeError(f"Payload must be a dict or valid JSON string, got {type_name}")

    notif_id = generate_notification_id()
    resolved_clock = clock or SystemClock()
    created_at = (
        now_iso
        if now_iso is not None
        else resolved_clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    try:
        tx.execute(
            notifications_outbox.insert().values(
                notification_id=notif_id,
                channel=channel,
                customer_id=customer_id,
                template=template,
                payload_json=payload_json,
                dedupe_key=dedupe_key,
                status="queued",
                attempts=0,
                next_attempt_at=None,
                last_error=None,
                created_at=created_at,
            )
        )
    except IntegrityError:
        row = tx.execute(
            select(notifications_outbox.c.notification_id).where(
                notifications_outbox.c.dedupe_key == dedupe_key
            )
        ).fetchone()
        if row is not None:
            return str(row[0])
        raise

    return notif_id
