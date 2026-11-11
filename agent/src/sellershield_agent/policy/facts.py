"""DecisionFacts and canonical facts hashing for SellerShield AI.

All models are frozen Pydantic v2 models (extra='forbid').
Datetimes are timezone-aware UTC.
Money is strictly integer paise (_paise).
Invariant: reason_text is NEVER read by any rule and is omitted from facts_hash.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sellershield_agent.domain.models import (
    CustomerRiskFacts,
    Order,
    Refundable,
    RefundMethod,
    ReturnItem,
    ReturnReasonCode,
    ReturnRequest,
    ReturnResolution,
    Shipment,
)


def _validate_aware_utc(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError(f"Datetime must be timezone-aware UTC, got naive: {v!r}")
    return v


class DecisionAction(StrEnum):
    """Action requested by the customer or workflow."""

    return_ = "return"
    refund_request = "refund_request"
    cancellation = "cancellation"


class DecisionFacts(BaseModel):
    """Complete factual snapshot used by pure policy evaluation.

    Immutable (frozen=True) with extra='forbid'.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: DecisionAction | str
    order: Order
    items: list[ReturnItem]
    reason_code: ReturnReasonCode | str | None = None
    reason_text: str | None = None
    resolution: ReturnResolution | str | None = None
    refund_method: RefundMethod | str | None = None
    evidence_present: bool = False
    active_returns: list[ReturnRequest] = Field(default_factory=list)
    previous_refunds_30d_paise: int = Field(default=0, ge=0)
    refundable: Refundable
    customer_risk: CustomerRiskFacts
    shipment: Shipment | None = None
    now: datetime

    @field_validator("now", mode="after")
    @classmethod
    def ensure_aware_utc(cls, v: datetime | None) -> datetime | None:
        return _validate_aware_utc(v)


def compute_facts_hash(facts: DecisionFacts) -> str:
    """Compute deterministic SHA-256 hash of facts, strictly omitting reason_text."""
    raw = facts.model_dump(mode="json")
    raw.pop("reason_text", None)
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
