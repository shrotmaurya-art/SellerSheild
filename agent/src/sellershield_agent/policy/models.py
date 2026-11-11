"""Policy models for SellerShield AI.

All models are frozen Pydantic v2 models (extra='forbid').
Money is strictly integer paise (_paise).
Ratios are integer basis points (_bp).
Datetimes are timezone-aware UTC.
Zero float fields exist on any model (KP-05).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_aware_utc(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError(f"Datetime must be timezone-aware UTC, got naive: {v!r}")
    return v


class ReturnShippingPolicy(BaseModel):
    """Return shipping responsibility rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    free_reasons: list[str]
    customer_fee_paise: int = Field(ge=0)


class RiskPolicy(BaseModel):
    """Deterministic risk weights, signals, and band thresholds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    returns_30d_medium_threshold: int = Field(ge=0)
    returns_30d_medium_weight: int = Field(ge=0)
    returns_30d_high_threshold: int = Field(ge=0)
    returns_30d_high_weight: int = Field(ge=0)
    refund_ratio_90d_threshold_bp: int = Field(ge=0)
    refund_ratio_90d_weight: int = Field(ge=0)
    order_value_min_paise: int = Field(ge=0)
    order_value_weight: int = Field(ge=0)
    account_age_days_max: int = Field(ge=0)
    account_age_weight: int = Field(ge=0)
    suspected_injection_weight: int = Field(ge=0)
    band_medium_min: int = Field(ge=0)
    band_high_min: int = Field(ge=0)


class CustomerFacingText(BaseModel):
    """Customer-safe policy text templates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    return_window_description: str
    free_return_reasons: str
    customer_fee_description: str
    refund_timing: str
    non_returnable_description: str


class PolicyDocument(BaseModel):
    """Versioned immutable store policy configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    effective_from: datetime
    currency: str
    timezone: str
    return_windows_days: dict[str, int]
    non_returnable_flags: list[str]
    auto_limit_paise: int = Field(ge=0)
    customer_cumulative_auto_limit_30d_paise: int = Field(ge=0)
    staff_approval_limit_paise: int = Field(ge=0)
    hard_ceiling_paise: int = Field(ge=0)
    pre_dispatch_cancel_auto_limit_paise: int = Field(ge=0)
    return_shipping: ReturnShippingPolicy
    restocking_fee_bp: dict[str, int]
    risk: RiskPolicy
    approval_sla_hours: int = Field(ge=0)
    approval_expiry_days: int = Field(ge=0)
    case_slas: dict[str, int]
    escalation_keywords: list[str]
    cod_refund_methods: list[str]
    customer_facing_text: CustomerFacingText

    @field_validator("effective_from", mode="after")
    @classmethod
    def ensure_aware_utc(cls, v: datetime | None) -> datetime | None:
        return _validate_aware_utc(v)

    @field_validator("return_windows_days", "restocking_fee_bp", "case_slas", mode="after")
    @classmethod
    def ensure_non_negative_values(cls, v: dict[str, int]) -> dict[str, int]:
        for key, val in v.items():
            if val < 0:
                raise ValueError(f"Value for {key} cannot be negative: {val}")
        return v


Policy = PolicyDocument


class ItemAmounts(dict[str, int]):
    """Dict mapping item_id to amount_paise with support for positional indexing."""

    def __getitem__(self, key: Any) -> int:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class RefundBreakdown(BaseModel):
    """Computed refund breakdown net of fees and caps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_amounts: dict[str, int]
    items_paise: int = Field(ge=0)
    shipping_refund_paise: int = Field(ge=0)
    fee_paise: int = Field(ge=0)
    total_paise: int = Field(ge=0)
    capped: bool
    cap_amount_paise: int | None = Field(default=None, ge=0)

    def __init__(
        self,
        item_amounts: dict[str, int] | None = None,
        items_paise: int | None = None,
        shipping_refund_paise: int | None = None,
        fee_paise: int | None = None,
        total_paise: int | None = None,
        capped: bool | None = None,
        cap_amount_paise: int | None = None,
        **data: Any,
    ) -> None:
        if item_amounts is not None and "item_amounts" not in data:
            data["item_amounts"] = ItemAmounts(item_amounts)
        elif "item_amounts" in data and isinstance(data["item_amounts"], dict):
            data["item_amounts"] = ItemAmounts(data["item_amounts"])
        if items_paise is not None and "items_paise" not in data:
            data["items_paise"] = items_paise
        if shipping_refund_paise is not None and "shipping_refund_paise" not in data:
            data["shipping_refund_paise"] = shipping_refund_paise
        if fee_paise is not None and "fee_paise" not in data:
            data["fee_paise"] = fee_paise
        if total_paise is not None and "total_paise" not in data:
            data["total_paise"] = total_paise
        if capped is not None and "capped" not in data:
            data["capped"] = capped
        if cap_amount_paise is not None and "cap_amount_paise" not in data:
            data["cap_amount_paise"] = cap_amount_paise
        super().__init__(**data)
