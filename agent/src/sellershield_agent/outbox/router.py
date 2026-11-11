"""Outbox APIRouter for seller notifications list and retry endpoints."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, select

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine, write_tx
from sellershield_agent.domain.state import notifications_outbox
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.outbox.worker import render_template, resolve_recipient_email

router = APIRouter(tags=["outbox"])


class NotificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification_id: str
    channel: str
    recipient: str
    subject: str | None = None
    status: str
    attempts: int
    created_at: str


class NotificationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notifications: list[NotificationItem]


def _safe_parse_payload(payload_json: str) -> dict[str, object]:
    try:
        parsed = json.loads(payload_json)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _list_notifications_sync(engine: Engine, status_filter: str | None) -> list[NotificationItem]:
    items: list[NotificationItem] = []
    with engine.connect() as conn:
        stmt = select(notifications_outbox)
        if status_filter:
            stmt = stmt.where(notifications_outbox.c.status == status_filter)
        stmt = stmt.order_by(notifications_outbox.c.created_at.desc())
        rows = conn.execute(stmt).fetchall()

        for r in rows:
            pld = _safe_parse_payload(str(r.payload_json))
            try:
                subject, _ = render_template(str(r.template), pld)
            except ValueError:
                subject = None
            recipient = resolve_recipient_email(r.customer_id, pld, engine, None, conn=conn)
            items.append(
                NotificationItem(
                    notification_id=str(r.notification_id),
                    channel=str(r.channel),
                    recipient=recipient,
                    subject=subject,
                    status=str(r.status),
                    attempts=int(r.attempts),
                    created_at=str(r.created_at),
                )
            )
    return items


def _retry_notification_sync(
    engine: Engine,
    *,
    notif_id: str,
    seller_user_id: str,
    request_id: str,
    clock: Clock,
) -> NotificationItem:
    now_iso = clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with write_tx(engine) as tx:
        row = tx.execute(
            select(notifications_outbox).where(notifications_outbox.c.notification_id == notif_id)
        ).fetchone()
        if row is None:
            raise AppHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                message=f"Notification '{notif_id}' not found",
            )

        if row.status not in ("failed", "exhausted"):
            raise AppHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                code="invalid_state",
                message=(
                    f"Notification '{notif_id}' has status '{row.status}'; "
                    "only failed or exhausted notifications can be retried."
                ),
            )

        res = tx.execute(
            notifications_outbox.update()
            .where(
                notifications_outbox.c.notification_id == notif_id,
                notifications_outbox.c.status.in_(["failed", "exhausted"]),
            )
            .values(
                status="queued",
                attempts=0,
                next_attempt_at=None,
                last_error=None,
            )
        )
        if res.rowcount != 1:
            raise AppHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                code="invalid_state",
                message=f"Notification '{notif_id}' changed concurrently",
            )

        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=now_iso,
                request_id=request_id,
                actor_type=ActorType.SELLER,
                actor_id=seller_user_id,
                event_type=EventType.NOTIFICATION,
                resource_type="notifications_outbox",
                resource_id=notif_id,
                action="retry_notification",
                outcome=Outcome.SUCCESS,
                policy_version="v3",
                details={
                    "previous_status": str(row.status),
                    "previous_attempts": int(row.attempts),
                    "status": "queued",
                    "attempts": 0,
                },
            ),
        )

        pld = _safe_parse_payload(str(row.payload_json))
        try:
            subject, _ = render_template(str(row.template), pld)
        except ValueError:
            subject = None
        recipient = resolve_recipient_email(row.customer_id, pld, engine, None, conn=tx)

        return NotificationItem(
            notification_id=str(row.notification_id),
            channel=str(row.channel),
            recipient=recipient,
            subject=subject,
            status="queued",
            attempts=0,
            created_at=str(row.created_at),
        )


@router.get(
    "/seller/notifications",
    summary="List notification outbox messages",
    response_model=NotificationListResponse,
)
async def list_seller_notifications(
    principal: Annotated[Principal, Depends(deps.require_seller)],
    status_filter: Annotated[
        str | None, Query(alias="status", description="Filter by status")
    ] = None,
) -> NotificationListResponse:
    engine = get_engine()
    items = await asyncio.to_thread(_list_notifications_sync, engine, status_filter)
    return NotificationListResponse(notifications=items)


@router.post(
    "/seller/notifications/{id}/retry",
    summary="Retry failed outbox notification delivery",
    response_model=NotificationItem,
)
async def retry_seller_notification(
    id: str,
    request: Request,
    principal: Annotated[Principal, Depends(deps.require_seller)],
    clock: Annotated[Clock, Depends(deps.get_clock)],
) -> NotificationItem:
    engine = get_engine()
    req_id = getattr(request.state, "request_id", None) or f"retry-{id}"
    return await asyncio.to_thread(
        _retry_notification_sync,
        engine,
        notif_id=id,
        seller_user_id=principal.user_id,
        request_id=req_id,
        clock=clock,
    )
