import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add store-api to sys.path so we can import app modules and db
REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_API_DIR = REPO_ROOT / "store-api"
if str(STORE_API_DIR) not in sys.path:
    sys.path.insert(0, str(STORE_API_DIR))

from argon2 import PasswordHasher
from db import DATA_DIR, engine
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Customer,
    Evidence,
    EventsOutbox,
    IdempotencyKey,
    MailMessage,
    Order,
    OrderItem,
    Product,
    Refund,
    Return,
    ReturnItem,
    ServiceClient,
    Shipment,
    ShipmentEvent,
    SimClock,
    SimFault,
    StoreAudit,
    StoreCreditLedger,
    StoreSettings,
    User,
)

SEED_DIR = REPO_ROOT / "scenarios" / "seed"


def parse_t0_offset(val: str | None, t0: datetime) -> datetime | None:
    if val is None:
        return None
    val = val.strip()
    if val == "T0":
        return t0

    # Match T0+... or T0-...
    match = re.match(r"^T0([+-])(\d+)([dhms])$", val)
    if match:
        sign, amount_str, unit = match.groups()
        amount = int(amount_str)
        delta_kwargs = {}
        if unit == "d":
            delta_kwargs["days"] = amount
        elif unit == "h":
            delta_kwargs["hours"] = amount
        elif unit == "m":
            delta_kwargs["minutes"] = amount
        elif unit == "s":
            delta_kwargs["seconds"] = amount

        delta = timedelta(**delta_kwargs)
        if sign == "+":
            return t0 + delta
        else:
            return t0 - delta

    # If it's already an ISO timestamp
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None


def seed_database(reset: bool = False) -> float:
    start_time = time.perf_counter()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_file = DATA_DIR / "store.db"

    # Always ensure tables are created
    if reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    # Compute T0 at the moment of seeding (UTC)
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)

    # Compute argon2id password hash once for Demo@12345
    ph = PasswordHasher()
    default_password_hash = ph.hash("Demo@12345")

    with Session(engine) as session:
        # Clear existing seed data in reverse dependency order
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())

        # 1. Users
        with open(SEED_DIR / "users.json", "r", encoding="utf-8") as f:
            users_data = json.load(f)
        for u in users_data:
            session.add(
                User(
                    user_id=u["user_id"],
                    email=u["email"],
                    password_hash=default_password_hash,
                    role=u["role"],
                    display_name=u["display_name"],
                    status=u.get("status", "active"),
                    created_at=parse_t0_offset(u.get("created_at"), t0) or t0,
                    version=1,
                )
            )
        session.flush()

        # 2. Customers
        with open(SEED_DIR / "customers.json", "r", encoding="utf-8") as f:
            customers_data = json.load(f)
        for c in customers_data:
            session.add(
                Customer(
                    customer_id=c["customer_id"],
                    user_id=c["user_id"],
                    full_name=c["full_name"],
                    joined_at=parse_t0_offset(c.get("joined_at"), t0) or t0,
                    default_address_json=c.get("default_address_json", "{}"),
                    version=1,
                )
            )
        session.flush()

        # 3. Products
        with open(SEED_DIR / "products.json", "r", encoding="utf-8") as f:
            products_data = json.load(f)
        for p in products_data:
            session.add(
                Product(
                    sku=p["sku"],
                    name=p["name"],
                    category=p["category"],
                    price_paise=p["price_paise"],
                    hygiene_sensitive=p.get("hygiene_sensitive", False),
                    final_sale=p.get("final_sale", False),
                )
            )
        session.flush()

        # 4. Orders
        with open(SEED_DIR / "orders.json", "r", encoding="utf-8") as f:
            orders_data = json.load(f)
        for o in orders_data:
            session.add(
                Order(
                    order_id=o["order_id"],
                    customer_id=o["customer_id"],
                    status=o["status"],
                    payment_method=o["payment_method"],
                    payment_status=o["payment_status"],
                    subtotal_paise=o["subtotal_paise"],
                    discount_paise=o.get("discount_paise", 0),
                    shipping_fee_paise=o.get("shipping_fee_paise", 0),
                    total_paise=o["total_paise"],
                    placed_at=parse_t0_offset(o.get("placed_at"), t0) or t0,
                    shipped_at=parse_t0_offset(o.get("shipped_at"), t0),
                    delivered_at=parse_t0_offset(o.get("delivered_at"), t0),
                    cancelled_at=parse_t0_offset(o.get("cancelled_at"), t0),
                    ship_to_json=o.get("ship_to_json", "{}"),
                    version=1,
                )
            )
        session.flush()

        # 5. Order Items
        with open(SEED_DIR / "order_items.json", "r", encoding="utf-8") as f:
            items_data = json.load(f)
        for item in items_data:
            session.add(
                OrderItem(
                    item_id=item["item_id"],
                    order_id=item["order_id"],
                    sku=item["sku"],
                    name_snapshot=item["name_snapshot"],
                    category_snapshot=item["category_snapshot"],
                    qty=item["qty"],
                    unit_price_paise=item["unit_price_paise"],
                    discount_alloc_paise=item.get("discount_alloc_paise", 0),
                    line_paid_paise=item["line_paid_paise"],
                    flags_json=item.get("flags_json", "{}"),
                )
            )
        session.flush()

        # 6. Shipments
        with open(SEED_DIR / "shipments.json", "r", encoding="utf-8") as f:
            shipments_data = json.load(f)
        for s in shipments_data:
            session.add(
                Shipment(
                    shipment_id=s["shipment_id"],
                    order_id=s["order_id"],
                    carrier=s["carrier"],
                    tracking_no=s["tracking_no"],
                    status=s["status"],
                    eta_at=parse_t0_offset(s.get("eta_at"), t0),
                    last_event_at=parse_t0_offset(s.get("last_event_at"), t0),
                    script_id=s["script_id"],
                    version=1,
                )
            )
        session.flush()

        # 7. Shipment Events
        with open(SEED_DIR / "shipment_events.json", "r", encoding="utf-8") as f:
            events_data = json.load(f)
        for ev in events_data:
            session.add(
                ShipmentEvent(
                    event_id=ev["event_id"],
                    shipment_id=ev["shipment_id"],
                    ts=parse_t0_offset(ev.get("ts"), t0) or t0,
                    status=ev["status"],
                    location=ev["location"],
                    note=ev["note"],
                )
            )
        session.flush()

        # 8. Returns
        with open(SEED_DIR / "returns.json", "r", encoding="utf-8") as f:
            returns_data = json.load(f)
        for r in returns_data:
            session.add(
                Return(
                    return_id=r["return_id"],
                    order_id=r["order_id"],
                    customer_id=r["customer_id"],
                    status=r["status"],
                    reason_code=r["reason_code"],
                    reason_text=r["reason_text"],
                    resolution=r["resolution"],
                    requested_via=r.get("requested_via", "chat"),
                    approved_by=r.get("approved_by"),
                    created_at=parse_t0_offset(r.get("created_at"), t0) or t0,
                    updated_at=parse_t0_offset(r.get("updated_at"), t0) or t0,
                    version=1,
                )
            )
        session.flush()

        # 9. Return Items
        with open(SEED_DIR / "return_items.json", "r", encoding="utf-8") as f:
            return_items_data = json.load(f)
        for ri in return_items_data:
            session.add(
                ReturnItem(
                    return_id=ri["return_id"],
                    item_id=ri["item_id"],
                    qty=ri["qty"],
                    condition_note=ri.get("condition_note"),
                    status=ri.get("status", "requested"),
                )
            )
        session.flush()

        # 10. Refunds
        with open(SEED_DIR / "refunds.json", "r", encoding="utf-8") as f:
            refunds_data = json.load(f)
        for rf in refunds_data:
            session.add(
                Refund(
                    refund_id=rf["refund_id"],
                    order_id=rf["order_id"],
                    return_id=rf.get("return_id"),
                    customer_id=rf["customer_id"],
                    amount_paise=rf["amount_paise"],
                    method=rf["method"],
                    status=rf["status"],
                    gateway_ref=rf.get("gateway_ref"),
                    idempotency_key=rf["idempotency_key"],
                    authorized_by=rf.get("authorized_by"),
                    authorized_amount_paise=rf.get("authorized_amount_paise"),
                    authorized_at=parse_t0_offset(rf.get("authorized_at"), t0),
                    failure_reason=rf.get("failure_reason"),
                    created_at=parse_t0_offset(rf.get("created_at"), t0) or t0,
                    updated_at=parse_t0_offset(rf.get("updated_at"), t0) or t0,
                    version=1,
                )
            )
        session.flush()

        # 11. Mail Messages
        with open(SEED_DIR / "mail_messages.json", "r", encoding="utf-8") as f:
            messages_data = json.load(f)
        for m in messages_data:
            session.add(
                MailMessage(
                    message_id=m["message_id"],
                    thread_id=m["thread_id"],
                    direction=m["direction"],
                    channel=m["channel"],
                    from_addr=m["from_addr"],
                    to_addr=m["to_addr"],
                    user_id=m.get("user_id"),
                    subject=m["subject"],
                    body_text=m["body_text"],
                    verified_sender=m.get("verified_sender", True),
                    status=m.get("status", "delivered"),
                    created_at=parse_t0_offset(m.get("created_at"), t0) or t0,
                    read_at=parse_t0_offset(m.get("read_at"), t0),
                )
            )
        session.flush()

        # 12. Store Settings
        with open(SEED_DIR / "store_settings.json", "r", encoding="utf-8") as f:
            settings_data = json.load(f)
        session.add(
            StoreSettings(
                id=settings_data.get("id", 1),
                autonomy_cap_paise=settings_data.get("autonomy_cap_paise", 50000),
                autonomy_daily_cap_paise=settings_data.get("autonomy_daily_cap_paise", 500000),
                updated_by=settings_data.get("updated_by"),
                updated_at=parse_t0_offset(settings_data.get("updated_at"), t0) or t0,
                version=1,
            )
        )
        session.flush()

        session.commit()

        # Query counts
        counts = {
            "users": session.scalar(select(func.count(User.user_id))),
            "customers": session.scalar(select(func.count(Customer.customer_id))),
            "products": session.scalar(select(func.count(Product.sku))),
            "orders": session.scalar(select(func.count(Order.order_id))),
            "order_items": session.scalar(select(func.count(OrderItem.item_id))),
            "shipments": session.scalar(select(func.count(Shipment.shipment_id))),
            "shipment_events": session.scalar(select(func.count(ShipmentEvent.event_id))),
            "returns": session.scalar(select(func.count(Return.return_id))),
            "return_items": session.scalar(select(func.count(ReturnItem.id))),
            "refunds": session.scalar(select(func.count(Refund.refund_id))),
            "mail_messages": session.scalar(select(func.count(MailMessage.message_id))),
            "store_settings": session.scalar(select(func.count(StoreSettings.id))),
        }

    elapsed = time.perf_counter() - start_time

    print(f"Database seeded successfully in {elapsed:.3f}s:")
    for table_name, count in counts.items():
        print(f"  - {table_name:16}: {count} rows")

    return elapsed


def reset_and_seed() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_file = DATA_DIR / "store.db"
    # Close existing connections
    engine.dispose()
    try:
        if db_file.exists():
            db_file.unlink()
        for suffix in ["-wal", "-shm"]:
            extra = DATA_DIR / f"store.db{suffix}"
            if extra.exists():
                extra.unlink()
    except PermissionError:
        # On Windows, if file is held open (e.g. DB Browser for SQLite), drop tables cleanly
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    seed_database(reset=True)


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset_and_seed()
    else:
        seed_database()

