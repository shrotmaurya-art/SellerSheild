"""Session and message database tables and domain models."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Column,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from sellershield_agent.db import get_metadata

# ── Database Tables ─────────────────────────────────────────────────────────

sessions = Table(
    "sessions",
    get_metadata(),
    Column("id", String(64), primary_key=True),
    Column("customer_id", String(64), nullable=False, index=True),
    Column("channel", String(32), nullable=False, default="web"),
    Column("handler", String(32), nullable=False, default="agent"),
    Column("language", String(16), nullable=False, default="en"),
    Column("state_json", Text, nullable=False, default="{}"),
    Column("started_at", String(32), nullable=False),
    Column("last_active_at", String(32), nullable=False),
)

messages = Table(
    "messages",
    get_metadata(),
    Column("id", String(64), primary_key=True),
    Column("session_id", String(64), ForeignKey("sessions.id"), nullable=False, index=True),
    Column("role", String(32), nullable=False),
    Column("text", Text, nullable=False),
    Column("cards_json", Text, nullable=True),
    Column("client_msg_id", String(64), nullable=False),
    Column("created_at", String(32), nullable=False),
    UniqueConstraint("session_id", "client_msg_id", name="uq_messages_session_client_msg"),
)


# ── Pydantic Models ─────────────────────────────────────────────────────────


class SessionState(BaseModel):
    """Structured session state per Spec §4.9."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    customer_id: str
    handler: str = "agent"
    language: str = "en"
    current_workflow: str | None = None
    selected_order_id: str | None = None
    selected_items: list[dict[str, Any]] = Field(default_factory=list)
    reason_code: str | None = None
    pending_proposal_ids: list[str] = Field(default_factory=list)
    clarification_count: int = 0
    turn_count: int = 0
    last_fact_ids: list[str] = Field(default_factory=list)
    suspected_injection_count: int = 0
    last_active_at: str

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data_str: str) -> SessionState:
        data = json.loads(data_str) if data_str else {}
        return cls(**data)


class ChatMessageRecord(BaseModel):
    """Stored chat message."""

    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    role: str
    text: str
    cards_json: str | None = None
    client_msg_id: str
    created_at: str


class ChatActionName(StrEnum):
    """Permitted guided actions per contracts/openapi-agent.yaml."""

    track_order = "track_order"
    return_status = "return_status"
    refund_status = "refund_status"
    policy_info = "policy_info"
    start_return = "start_return"
    talk_to_human = "talk_to_human"


class ChatAction(BaseModel):
    """Guided action carried in chat request."""

    model_config = ConfigDict(extra="forbid")

    name: ChatActionName
    order_id: str | None = None


class ChatMessageInput(BaseModel):
    """Input payload for POST /v1/chat/sessions/{id}/messages."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=1000)
    action: ChatAction | None = None
    client_msg_id: str
    evidence_ids: list[str] | None = None
