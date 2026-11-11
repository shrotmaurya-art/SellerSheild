from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    customer: Mapped[Optional["Customer"]] = relationship(
        "Customer", back_populates="user", uselist=False
    )


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # C001, C002, etc.
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id"), unique=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    default_address_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="customer")
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="customer")


class Product(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)  # P-101, etc.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    hygiene_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    final_sale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # ORD-####
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(32), nullable=False)
    payment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    subtotal_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    shipping_fee_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ship_to_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    customer: Mapped["Customer"] = relationship("Customer", back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan"
    )
    shipment: Mapped[Optional["Shipment"]] = relationship(
        "Shipment", back_populates="order", uselist=False
    )
    returns: Mapped[list["Return"]] = relationship("Return", back_populates="order")
    refunds: Mapped[list["Refund"]] = relationship("Refund", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # ITEM-####-#
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.order_id"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(32), ForeignKey("products.sku"), nullable=False)
    name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    category_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_alloc_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    line_paid_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    flags_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="items")
    product: Mapped["Product"] = relationship("Product")


class Shipment(Base):
    __tablename__ = "shipments"

    shipment_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # SHP-####
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.order_id"), unique=True, nullable=False
    )
    carrier: Mapped[str] = mapped_column(String(64), nullable=False)
    tracking_no: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    eta_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_event_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    script_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="shipment")
    events: Mapped[list["ShipmentEvent"]] = relationship(
        "ShipmentEvent", back_populates="shipment", cascade="all, delete-orphan"
    )


class ShipmentEvent(Base):
    __tablename__ = "shipment_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # EVT-####-#
    shipment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("shipments.shipment_id"), nullable=False, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    location: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)

    shipment: Mapped["Shipment"] = relationship("Shipment", back_populates="events")


class Return(Base):
    __tablename__ = "returns"

    return_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # RET-####
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.order_id"), nullable=False, index=True
    )
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_via: Mapped[str] = mapped_column(String(32), default="chat", nullable=False)
    approved_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    client_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="returns")
    items: Mapped[list["ReturnItem"]] = relationship(
        "ReturnItem", back_populates="return_obj", cascade="all, delete-orphan"
    )


class ReturnItem(Base):
    __tablename__ = "return_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    return_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("returns.return_id"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("order_items.item_id"), nullable=False, index=True
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    condition_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="requested", nullable=False)

    return_obj: Mapped["Return"] = relationship("Return", back_populates="items")
    order_item: Mapped["OrderItem"] = relationship("OrderItem")

    __table_args__ = (
        Index(
            "uq_active_return_item",
            "item_id",
            unique=True,
            sqlite_where=text("status NOT IN ('completed', 'cancelled', 'rejected')"),
        ),
    )


class Refund(Base):
    __tablename__ = "refunds"

    refund_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # RFD-####
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.order_id"), nullable=False, index=True
    )
    return_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("returns.return_id"), nullable=True
    )
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False, index=True
    )
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    gateway_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    authorized_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    authorized_amount_paise: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    authorized_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="refunds")

    @property
    def client_ref(self) -> str:
        return self.idempotency_key


class StoreCreditLedger(Base):
    __tablename__ = "store_credit_ledger"

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False, index=True
    )
    delta_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    ref: Mapped[str] = mapped_column(String(128), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MailMessage(Base):
    __tablename__ = "mail_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    from_addr: Mapped[str] = mapped_column(String(255), nullable=False)
    to_addr: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("users.user_id"), nullable=True
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    verified_sender: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="delivered", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class EventsOutbox(Base):
    __tablename__ = "events_outbox"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ServiceClient(Base):
    __tablename__ = "service_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False)


class StoreAudit(Base):
    __tablename__ = "store_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)


class SimClock(Base):
    __tablename__ = "sim_clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(32), default="realtime", nullable=False)
    offset_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    frozen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SimFault(Base):
    __tablename__ = "sim_faults"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    params_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class StoreSettings(Base):
    __tablename__ = "store_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    autonomy_cap_paise: Mapped[int] = mapped_column(Integer, default=50000, nullable=False)
    autonomy_daily_cap_paise: Mapped[int] = mapped_column(Integer, default=500000, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Evidence(Base):
    __tablename__ = "evidence"

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.order_id"), nullable=False, index=True
    )
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False, index=True
    )
    file_path: Mapped[str] = mapped_column(String(255), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
