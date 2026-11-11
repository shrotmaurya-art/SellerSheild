"""Pydantic schemas and table exports for structured customer memory.

Enforces zero free text: only enums, identifiers, counts, and dates (all strings <= 64 chars).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sellershield_agent.domain.state import (
    customer_memory,
    generate_memory_event_id,
    memory_events,
)

_ORDER_ID_RE = re.compile(r"^ORD-[0-9A-Za-z]+$")
_ITEM_ID_RE = re.compile(r"^(RET|RFD|CASE)-[0-9A-Za-z]+$")
_ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_ALLOWED_LANGUAGES = frozenset({"en", "hi", "hinglish", "other"})
_MAX_STRING_LEN = 64
_MAX_OPEN_ITEMS = 20


def _validate_iso_ts(v: str) -> str:
    if len(v) > _MAX_STRING_LEN or not _ISO_TS_RE.match(v):
        raise ValueError(f"Invalid ISO-8601 timestamp format: '{v}'")
    datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v


class OrderDiscussed(BaseModel):
    """Structured record of the last order discussed (no customer text)."""

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(max_length=_MAX_STRING_LEN)
    timestamp: str = Field(max_length=_MAX_STRING_LEN)

    @field_validator("order_id")
    @classmethod
    def validate_order_id(cls, v: str) -> str:
        if not _ORDER_ID_RE.match(v):
            raise ValueError(f"Invalid order_id format: '{v}'")
        return v

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        return _validate_iso_ts(v)


class MemoryCounts(BaseModel):
    """Deterministic integer counts for profile memory."""

    model_config = ConfigDict(extra="forbid")

    returns: int = Field(default=0, ge=0)
    refunds: int = Field(default=0, ge=0)
    cases: int = Field(default=0, ge=0)


class CustomerProfile(BaseModel):
    """Customer profile memory holding only structured enums, IDs, counts, and dates.

    Customer or model free text is never stored.
    """

    model_config = ConfigDict(extra="forbid")

    preferred_language: Literal["en", "hi", "hinglish", "other"] = "en"
    last_order_discussed: OrderDiscussed | None = None
    open_item_ids: list[str] = Field(default_factory=list)
    counts: MemoryCounts = Field(default_factory=MemoryCounts)
    last_seen_at: str | None = Field(default=None, max_length=_MAX_STRING_LEN)

    @field_validator("last_seen_at")
    @classmethod
    def validate_last_seen_at(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_iso_ts(v)

    @field_validator("open_item_ids")
    @classmethod
    def validate_open_items(cls, items: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in items[-_MAX_OPEN_ITEMS:]:
            if len(item) > _MAX_STRING_LEN:
                raise ValueError(f"Item ID exceeds {_MAX_STRING_LEN} chars: '{item}'")
            if not _ITEM_ID_RE.match(item):
                raise ValueError(f"Invalid item ID format: '{item}'")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


__all__ = [
    "CustomerProfile",
    "MemoryCounts",
    "OrderDiscussed",
    "customer_memory",
    "generate_memory_event_id",
    "memory_events",
]
