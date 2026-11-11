"""Domain models mirroring the Store API contract for SellerShield AI.

All models are frozen Pydantic v2 models (extra='forbid').
Money is strictly integer paise (_paise).
Datetimes are timezone-aware UTC.
Ratios are integer basis points (_bp).
Zero float fields exist on any model (KP-05).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── Status Enums (Spec 01 section 5 & openapi-store.yaml) ───────────


class OrderStatus(StrEnum):
    placed = "placed"
    packed = "packed"
    shipped = "shipped"
    in_transit = "in_transit"
    out_for_delivery = "out_for_delivery"
    delivered = "delivered"
    cancelled = "cancelled"


class PaymentMethod(StrEnum):
    prepaid = "prepaid"
    cod = "cod"


class PaymentStatus(StrEnum):
    paid = "paid"
    pending = "pending"
    cod_pending = "cod_pending"
    cod_collected = "cod_collected"
    refunded = "refunded"
    partially_refunded = "partially_refunded"


class ShipmentStatus(StrEnum):
    label_created = "label_created"
    picked_up = "picked_up"
    in_transit = "in_transit"
    out_for_delivery = "out_for_delivery"
    delivered = "delivered"
    exception = "exception"
    lost = "lost"
    rto = "rto"


class ReturnStatus(StrEnum):
    requested = "requested"
    approved = "approved"
    rejected = "rejected"
    cancelled = "cancelled"
    pickup_scheduled = "pickup_scheduled"
    in_transit_back = "in_transit_back"
    received = "received"
    inspected_ok = "inspected_ok"
    inspection_failed = "inspection_failed"
    completed = "completed"


class ReturnReasonCode(StrEnum):
    defective = "defective"
    damaged = "damaged"
    wrong_item = "wrong_item"
    not_as_described = "not_as_described"
    changed_mind = "changed_mind"
    arrived_late = "arrived_late"


class ReturnResolution(StrEnum):
    refund = "refund"
    store_credit = "store_credit"


class RefundStatus(StrEnum):
    reserved = "reserved"
    authorized = "authorized"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    released = "released"


class RefundMethod(StrEnum):
    original = "original"
    store_credit = "store_credit"
    upi_payout = "upi_payout"


# ── Helper for Aware UTC Datetime Validation ────────────────────────


def _validate_aware_utc(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError(f"Datetime must be timezone-aware UTC, got naive: {v!r}")
    return v


# ── Domain Models ───────────────────────────────────────────────────


class Address(BaseModel):
    """Customer shipping or delivery address."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    full_name: str
    line1: str
    line2: str | None = None
    city: str
    state: str
    postal_code: str
    country: str
    phone: str | None = None


class OrderItem(BaseModel):
    """Line item belonging to an order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str
    sku: str
    name: str
    category: str | None = None
    qty: int = Field(ge=1)
    unit_price_paise: int = Field(ge=0)
    discount_alloc_paise: int = Field(default=0, ge=0)
    line_paid_paise: int = Field(ge=0)
    flags: dict[str, Any] = Field(default_factory=dict)


class Order(BaseModel):
    """Customer order mirroring store contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    customer_id: str
    status: OrderStatus
    payment_method: PaymentMethod
    payment_status: PaymentStatus
    subtotal_paise: int = Field(ge=0)
    discount_paise: int = Field(default=0, ge=0)
    shipping_fee_paise: int = Field(default=0, ge=0)
    total_paise: int = Field(ge=0)
    items: list[OrderItem]
    ship_to: Address | None = None
    placed_at: datetime
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    cancelled_at: datetime | None = None
    version: int = 1
    returns: list[ReturnRequest] = Field(default_factory=list)
    refunds: list[Refund] = Field(default_factory=list)
    client_ref: str | None = None

    @field_validator("placed_at", "shipped_at", "delivered_at", "cancelled_at", mode="after")
    @classmethod
    def ensure_aware_utc(cls, v: datetime | None) -> datetime | None:
        return _validate_aware_utc(v)


class ShipmentEvent(BaseModel):
    """Tracking event recorded against a shipment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    occurred_at: datetime
    status: ShipmentStatus
    location: str
    note: str | None = None

    @field_validator("occurred_at", mode="after")
    @classmethod
    def ensure_aware_utc(cls, v: datetime | None) -> datetime | None:
        return _validate_aware_utc(v)


class Shipment(BaseModel):
    """Carrier tracking information for an order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    shipment_id: str
    order_id: str
    carrier: str
    tracking_no: str
    status: ShipmentStatus
    eta_at: datetime | None = None
    delayed: bool = False
    events: list[ShipmentEvent] = Field(default_factory=list)

    @field_validator("eta_at", mode="after")
    @classmethod
    def ensure_aware_utc(cls, v: datetime | None) -> datetime | None:
        return _validate_aware_utc(v)


class ReturnItem(BaseModel):
    """Specific line item and quantity requested for return."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str
    qty: int = Field(ge=1)
    condition_note: str | None = None


class ReturnRequest(BaseModel):
    """Customer return request lifecycle entity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    return_id: str
    order_id: str
    customer_id: str
    status: ReturnStatus
    reason_code: ReturnReasonCode
    reason_text: str | None = None
    resolution: ReturnResolution
    items: list[ReturnItem]
    approved_by: str | None = None
    client_ref: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    version: int = 1

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def ensure_aware_utc(cls, v: datetime | None) -> datetime | None:
        return _validate_aware_utc(v)


# Alias Return to ReturnRequest to mirror both spec terminology and contract schema
Return = ReturnRequest


class Refund(BaseModel):
    """Refund entity representing autonomous or authorized payout/credit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    refund_id: str
    order_id: str
    return_id: str | None = None
    customer_id: str
    amount_paise: int = Field(ge=0)
    method: RefundMethod
    status: RefundStatus
    gateway_ref: str | None = None
    authorized_by: str | None = None
    authorized_amount_paise: int | None = Field(default=None, ge=0)
    authorized_at: datetime | None = None
    failure_reason: str | None = None
    client_ref: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    @property
    def authorized_amount(self) -> int | None:
        return self.authorized_amount_paise

    @field_validator("authorized_at", "created_at", "updated_at", mode="after")
    @classmethod
    def ensure_aware_utc(cls, v: datetime | None) -> datetime | None:
        return _validate_aware_utc(v)


class RefundableItem(BaseModel):
    """Remaining quantity and refundable amount for a single order item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str
    original_qty: int = Field(ge=0)
    remaining_qty: int = Field(ge=0)
    line_paid_paise: int = Field(ge=0)
    refundable_paise: int = Field(ge=0)


class Refundable(BaseModel):
    """Calculated refundable status for an order net of holds and past refunds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    max_refundable_paise: int = Field(ge=0)
    items: list[RefundableItem]


class CustomerRiskFacts(BaseModel):
    """Deterministic customer risk facts for policy evaluation.

    All ratios expressed as integer basis points (_bp, 100 bp = 1%).
    Zero float fields (KP-05).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    customer_id: str
    returns_30d: int = Field(ge=0)
    refund_ratio_90d_bp: int = Field(ge=0)
    account_age_days: int = Field(ge=0)
    refunded_30d_paise: int = Field(ge=0)


class AdminOrderDetail(Order):
    """Detailed order representation for seller and admin queries with returns and refunds."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StoreSettings(BaseModel):
    """Store autonomy limits and configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    autonomy_cap_paise: int = Field(default=50000, ge=0)
    autonomy_daily_cap_paise: int = Field(default=500000, ge=0)
    version: int = 1


class CustomerRefundSummary(BaseModel):
    """Summary of historical refunds and returns for a customer over N days."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    customer_id: str
    days: int = Field(ge=1)
    total_refunded_paise: int = Field(ge=0)
    refund_count: int = Field(ge=0)
    return_count: int = Field(ge=0)


Order.model_rebuild()
AdminOrderDetail.model_rebuild()


class MonitorShipment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shipment_id: str
    order_id: str
    customer_id: str = ""
    carrier: str
    tracking_no: str
    status: str
    eta_at: datetime | None = None
    delayed: bool = False
    events: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("eta_at", mode="before")
    @classmethod
    def _normalize_eta(cls, v: Any) -> datetime | None:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v if v.tzinfo is not None else v.replace(tzinfo=UTC)
        if isinstance(v, str):
            clean = v.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        return None


class ShipmentListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shipments: list[MonitorShipment]
    next_cursor: str | None = None
