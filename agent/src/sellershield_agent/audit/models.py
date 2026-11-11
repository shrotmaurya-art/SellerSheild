"""Data models and database schema for the audit hash chain."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, Index, Integer, String, Table, Text

from sellershield_agent.db import get_metadata


class ActorType(StrEnum):
    CUSTOMER = "customer"
    SELLER = "seller"
    AGENT = "agent"
    SYSTEM = "system"


class EventType(StrEnum):
    MESSAGE_RECEIVED = "message_received"
    TOOL_CALL = "tool_call"
    AUTH_DENIED = "auth_denied"
    INJECTION_FLAG = "injection_flag"
    VERIFIER_FAILURE = "verifier_failure"
    LLM_FALLBACK = "llm_fallback"
    EXECUTION_STEP = "execution_step"
    APPROVAL_CREATED = "approval_created"
    APPROVAL_DECIDED = "approval_decided"
    RECONCILE = "reconcile"
    WEBHOOK_RECEIVED = "webhook_received"
    POLICY_DECISION = "policy_decision"
    PROPOSAL_CREATED = "proposal_created"
    PROPOSAL_CONFIRMED = "proposal_confirmed"
    NOTIFICATION = "notification"
    NOTIFICATION_STUB = "notification_stub"


class Outcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILED = "failed"


@dataclass(frozen=True)
class ChainStatus:
    ok: bool
    checked: int
    first_broken_seq: int | None = None


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    ts_utc: str
    request_id: str | None = None
    session_id: str | None = None
    actor_type: ActorType | str
    actor_id: str | None = None
    event_type: EventType | str
    resource_type: str | None = None
    resource_id: str | None = None
    action: str
    outcome: Outcome | str
    policy_version: str | None = None
    details: Any = Field(default_factory=dict)

    @property
    def details_json(self) -> str:
        """Serialize event details deterministically for audit/test inspection."""
        try:
            return json.dumps(self.details, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(self.details)


# Table definition attached to shared metadata
audit_events = Table(
    "audit_events",
    get_metadata(),
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(64), nullable=False),
    Column("ts_utc", String(32), nullable=False),
    Column("request_id", String(64), nullable=True),
    Column("session_id", String(64), nullable=True),
    Column("actor_type", String(32), nullable=False),
    Column("actor_id", String(64), nullable=True),
    Column("event_type", String(64), nullable=False),
    Column("resource_type", String(64), nullable=True),
    Column("resource_id", String(64), nullable=True),
    Column("action", String(64), nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("policy_version", String(32), nullable=True),
    Column("details_json", Text, nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
    Index("ix_audit_events_prev_hash", "prev_hash", unique=True),
)
