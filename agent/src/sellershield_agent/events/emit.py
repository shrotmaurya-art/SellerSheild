"""Client event emission and per-type schema validation."""

from __future__ import annotations

import asyncio
import functools
import json
import threading
from collections import defaultdict
from datetime import UTC
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection

from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.domain.state import client_events, generate_client_event_id


class ApprovalDecidedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    proposal_id: str
    order_id: str
    status: str
    return_id: str | None = None
    refund_id: str | None = None
    message_key: str | None = None
    message: str | None = None


class CaseMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    text: str
    message_id: str | None = None
    actor_type: str = "seller"
    created_at: str | None = None


class CaseStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    status: str
    previous_status: str | None = None
    resolution_code: str | None = None
    message: str | None = None


class NotificationNewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str | None = None
    status: str | None = None
    return_id: str | None = None
    approval_id: str | None = None
    notification_id: str | None = None
    message_key: str | None = None
    message: str | None = None
    title: str | None = None


class ProposalExpiredPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    approval_id: str | None = None
    order_id: str | None = None
    return_id: str | None = None
    status: str = "expired"
    message_key: str | None = None
    message: str | None = None


class MonitorNoticePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    kind: str
    message: str
    eta_at: str | None = None
    new_eta: str | None = None
    carrier: str | None = None
    tracking_number: str | None = None
    chips: list[dict[str, Any]] | None = None


class RefundUpdatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refund_id: str
    status: str
    order_id: str | None = None
    return_id: str | None = None
    proposal_id: str | None = None
    message_key: str | None = None
    message: str | None = None


class ClientEventNotifier:
    """Thread-safe and asyncio-safe pub-sub dispatcher for customer client events."""

    def __init__(self) -> None:
        self._listeners: dict[str, dict[asyncio.Event, asyncio.AbstractEventLoop | None]] = (
            defaultdict(dict)
        )
        self._lock = threading.Lock()

    def subscribe(self, customer_id: str, event: asyncio.Event) -> None:
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._lock:
            self._listeners[customer_id][event] = loop

    def unsubscribe(self, customer_id: str, event: asyncio.Event) -> None:
        with self._lock:
            listeners = self._listeners.get(customer_id)
            if listeners is not None:
                listeners.pop(event, None)
                if not listeners:
                    del self._listeners[customer_id]

    def notify(self, customer_id: str) -> None:
        with self._lock:
            listeners = list(self._listeners.get(customer_id, {}).items())
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for ev, loop in listeners:
            if loop is None or loop is current_loop:
                ev.set()
            elif not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(ev.set)
                except RuntimeError:
                    pass


client_event_notifier = ClientEventNotifier()


EVENT_TYPE_MODELS: dict[str, type[BaseModel]] = {
    "approval.decided": ApprovalDecidedPayload,
    "case.message": CaseMessagePayload,
    "case.status": CaseStatusPayload,
    "notification.new": NotificationNewPayload,
    "proposal.expired": ProposalExpiredPayload,
    "monitor.notice": MonitorNoticePayload,
    "refund.updated": RefundUpdatedPayload,
}


def emit_client_event(
    tx: Connection,
    customer_id: str,
    type: str,
    payload: dict[str, Any] | BaseModel,
    now_iso: str | None = None,
    *,
    clock: Clock | None = None,
) -> str:
    """Validate client event payload and insert into client_events, returning cursor (event_id).

    Validates payload against the per-type Pydantic schema with extra='forbid'.
    Schedules notifier wakeup after commit when executed inside write_tx.
    """
    if not customer_id or not isinstance(customer_id, str):
        raise ValueError("customer_id must be a non-empty string")

    model_cls = EVENT_TYPE_MODELS.get(type)
    if model_cls is None:
        raise ValueError(
            f"Unsupported event type '{type}'. Allowed types: {sorted(EVENT_TYPE_MODELS.keys())}"
        )

    if isinstance(payload, BaseModel):
        validated = model_cls.model_validate(payload.model_dump())
    elif isinstance(payload, dict):
        validated = model_cls.model_validate(payload)
    else:
        raise TypeError(f"Payload must be a dict or BaseModel, got {type(payload).__name__}")

    event_id = generate_client_event_id()
    resolved_clock = clock or SystemClock()
    created_at = (
        now_iso
        if now_iso is not None
        else resolved_clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    payload_json = json.dumps(
        validated.model_dump(),
        sort_keys=True,
        separators=(",", ":"),
    )

    tx.execute(
        client_events.insert().values(
            event_id=event_id,
            customer_id=customer_id,
            type=type,
            payload_json=payload_json,
            created_at=created_at,
        )
    )

    after_commit = tx.info.get("_after_commit")
    if isinstance(after_commit, list):
        after_commit.append(functools.partial(client_event_notifier.notify, customer_id))
    else:
        client_event_notifier.notify(customer_id)

    return event_id
