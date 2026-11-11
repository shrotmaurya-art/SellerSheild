from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sku: str
    name: str
    category: str
    price_paise: int
    hygiene_sensitive: bool
    final_sale: bool


class OrderItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_id: str
    order_id: str
    sku: str
    name_snapshot: str
    category_snapshot: str
    qty: int
    unit_price_paise: int
    discount_alloc_paise: int
    line_paid_paise: int
    flags_json: str = "{}"


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: str
    customer_id: str
    status: str
    payment_method: str
    payment_status: str
    subtotal_paise: int
    discount_paise: int
    shipping_fee_paise: int
    total_paise: int
    placed_at: datetime
    shipped_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    ship_to_json: str = "{}"
    items: list[OrderItemResponse] = []
    version: int = 1


class OrderListResponse(BaseModel):
    items: list[OrderResponse]
    next_cursor: Optional[str] = None
    has_more: bool


class ShipmentEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    shipment_id: str
    ts: datetime
    status: str
    location: str
    note: str


class ShipmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    shipment_id: str
    order_id: str
    carrier: str
    tracking_no: str
    status: str
    eta_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    script_id: str
    delayed: bool
    events: list[ShipmentEventResponse] = []


class RefundableItemResponse(BaseModel):
    item_id: str
    sku: str
    name: str
    requested_qty: int
    remaining_qty: int
    unit_price_paise: int
    line_paid_paise: int
    max_refundable_paise: int


class RefundableResponse(BaseModel):
    order_id: str
    items: list[RefundableItemResponse]
    max_refundable_paise: int


class RefundResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    refund_id: str
    order_id: str
    return_id: Optional[str] = None
    customer_id: str
    amount_paise: int
    method: str
    status: str
    gateway_ref: Optional[str] = None
    simulated: bool = True
    authorized_by: Optional[str] = None
    authorized_amount_paise: Optional[int] = None
    authorized_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    client_ref: Optional[str] = None
    version: int = 1
    created_at: datetime
    updated_at: datetime


class StoreSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    autonomy_cap_paise: int
    autonomy_daily_cap_paise: int
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    version: int = 1


class StoreSettingsUpdateRequest(BaseModel):
    autonomy_cap_paise: Optional[int] = None
    autonomy_daily_cap_paise: Optional[int] = None


class RefundReservationRequest(BaseModel):
    amount_paise: int
    return_id: Optional[str] = None
    method: str = "original"  # "original", "store_credit", "upi_payout"


class RefundAuthorizeRequest(BaseModel):
    amount_paise: Optional[int] = None
    note: Optional[str] = None


class RefundReleaseRequest(BaseModel):
    note: Optional[str] = None


class CustomerRefundSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: str
    days: int
    total_refunds_paise: int
    refund_count: int
    active_holds_paise: int
    active_hold_count: int


class CustomerAdminSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: str
    full_name: str
    joined_at: datetime
    account_age: str
    account_age_days: int
    returns_30d: int
    refund_ratio_90d: float
    total_orders: int
    total_spend_paise: int
    total_refunded_paise: int


class ReturnItemCreate(BaseModel):
    item_id: str
    qty: int = 1
    condition_note: Optional[str] = None


class ReturnCreateRequest(BaseModel):
    items: list[ReturnItemCreate]
    reason_code: str
    reason_text: str
    resolution: str = "refund"  # refund or store_credit


class ReturnItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    return_id: str
    item_id: str
    qty: int
    condition_note: Optional[str] = None
    status: str


class ReturnResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    return_id: str
    order_id: str
    customer_id: str
    status: str
    reason_code: str
    reason_text: str
    resolution: str
    requested_via: str = "chat"
    approved_by: Optional[str] = None
    client_ref: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    version: int = 1
    items: list[ReturnItemResponse] = []


class AdminOrderDetailResponse(OrderResponse):
    returns: list[ReturnResponse] = []
    refunds: list[RefundResponse] = []


class ReturnTransitionRequest(BaseModel):
    to: str
    reason: Optional[str] = None


class ReturnInspectRequest(BaseModel):
    result: Optional[str] = None  # "ok" or "failed"
    to: Optional[str] = None  # "inspected_ok" or "inspection_failed"
    note: Optional[str] = None


class EvidenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evidence_id: str
    order_id: str
    customer_id: str
    file_name: str
    file_size_bytes: int
    mime_type: str
    created_at: datetime
    download_url: str


class EvidenceListResponse(BaseModel):
    order_id: str
    items: list[EvidenceResponse]
    count: int


# Mailbox Schemas (spec section 6, 7.3)
class MailSendRequest(BaseModel):
    thread_id: Optional[str] = None
    channel: str = "email"  # "email" or "whatsapp_sim"
    to_addr: str
    subject: str
    body_text: str
    direction: Optional[str] = None  # inbound or outbound


class MailMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_id: str
    thread_id: str
    direction: str
    channel: str
    from_addr: str
    to_addr: str
    user_id: Optional[str] = None
    subject: str
    body_text: str
    verified_sender: bool = True
    status: str = "delivered"
    created_at: datetime
    read_at: Optional[datetime] = None
    simulated: bool = True


class MailInboxResponse(BaseModel):
    messages: list[MailMessageResponse]
    total: int


class MailThreadResponse(BaseModel):
    thread_id: str
    messages: list[MailMessageResponse]


# Carrier Sim Schemas (spec section 6, 7.1)
class CarrierAdvanceResponse(BaseModel):
    shipment: ShipmentResponse
    new_event: Optional[ShipmentEventResponse] = None
    message: str = "Shipment advanced"


class PlayScenarioRequest(BaseModel):
    shipment_id: Optional[str] = None
    script_id: Optional[str] = None


class EventOutboxResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    type: str
    payload_json: str
    created_at: datetime
    delivered_at: Optional[datetime] = None
    attempts: int = 0
    status: str = "pending"
    next_retry_at: Optional[datetime] = None
    last_error: Optional[str] = None


# Demo Control Schemas (spec section 6, 8, 16)
class DemoResetResponse(BaseModel):
    status: str = "ok"
    message: str = "Database reset and seeded successfully"
    duration_seconds: float


class DemoClockRequest(BaseModel):
    action: Optional[str] = None  # advance, freeze, resume, set, reset
    hours: Optional[float] = 0.0
    days: Optional[float] = 0.0
    seconds: Optional[float] = 0.0
    exact_time: Optional[str] = None
    freeze: Optional[bool] = None


class DemoClockResponse(BaseModel):
    now_utc: str
    mode: str
    offset_seconds: float = 0.0


class DemoFaultRequest(BaseModel):
    fault: Optional[str] = None
    enabled: Optional[bool] = None
    clear_all: Optional[bool] = False
    faults: Optional[dict[str, bool]] = None


class DemoFaultsResponse(BaseModel):
    faults: dict[str, bool]


class OutboxStatusSummary(BaseModel):
    total: int = 0
    pending: int = 0
    delivered: int = 0
    exhausted: int = 0


class DemoStateResponse(BaseModel):
    row_counts: dict[str, int]
    last_20_events: list[EventOutboxResponse]
    outbox_status: OutboxStatusSummary
    clock: dict[str, Any]
    faults: dict[str, bool]


class DemoScenarioLoadResponse(BaseModel):
    status: str = "ok"
    scenario_id: str
    message: str


class SnapshotActionResponse(BaseModel):
    status: str = "ok"
    snapshot: str
    message: str


class SnapshotListResponse(BaseModel):
    snapshots: list[str]
