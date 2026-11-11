import pytest
from argon2 import PasswordHasher
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.discount import allocate_discount_largest_remainder
from app.models import (
    Customer,
    Order,
    OrderItem,
    Product,
    Refund,
    Return,
    ReturnItem,
    StoreSettings,
    User,
)
from db import engine


def test_seed_loads() -> None:
    """Verifies that seed data is loaded with correct entities and attributes."""
    with Session(engine) as session:
        # Check core table counts
        assert session.scalar(select(func.count(User.user_id))) == 7
        assert session.scalar(select(func.count(Customer.customer_id))) == 5
        seller_count = session.scalar(
            select(func.count(User.user_id)).where(User.role.in_(["seller_owner", "seller_staff"]))
        )
        assert seller_count == 2
        assert session.scalar(select(func.count(Product.sku))) == 10
        assert session.scalar(select(func.count(Order.order_id))) == 20
        assert session.scalar(select(func.count(OrderItem.item_id))) == 22
        assert session.scalar(select(func.count(Return.return_id))) == 5
        assert session.scalar(select(func.count(Refund.refund_id))) == 5

        # Check key scenario amounts (in integer paise)
        ord_1048 = session.get(Order, "ORD-1048")
        assert ord_1048 is not None
        assert ord_1048.total_paise == 89900

        ord_2031 = session.get(Order, "ORD-2031")
        assert ord_2031 is not None
        assert ord_2031.total_paise == 164700

        # Check store settings autonomy defaults
        settings = session.get(StoreSettings, 1)
        assert settings is not None
        assert settings.autonomy_cap_paise == 50000
        assert settings.autonomy_daily_cap_paise == 500000

        # Check password hash format: must start with $argon2id$ and verify Demo@12345
        ph = PasswordHasher()
        sample_user = session.get(User, "C001")
        assert sample_user is not None
        assert sample_user.password_hash.startswith("$argon2id$")
        assert ph.verify(sample_user.password_hash, "Demo@12345")


def test_second_active_return_rejected_by_partial_unique_index() -> None:
    """
    Invariants & Guardrail:
    At most one ACTIVE return can exist per order item (partial unique index).
    Inserting a second active return for ITEM-3050-1 must be rejected by the database.
    """
    with Session(engine) as session:
        # ITEM-3050-1 already has an active return RET-9050 with status 'requested'
        ret_item = session.scalar(select(ReturnItem).where(ReturnItem.item_id == "ITEM-3050-1"))
        assert ret_item is not None
        assert ret_item.status == "requested"

        # Attempting to add another active return item for the same order item must fail
        dup_active_item = ReturnItem(
            return_id="RET-9050",
            item_id="ITEM-3050-1",
            qty=1,
            condition_note="Duplicate active return attempt",
            status="requested",
        )
        session.add(dup_active_item)
        with pytest.raises(IntegrityError) as exc_info:
            session.flush()

        assert "UNIQUE constraint failed" in str(exc_info.value) or "uq_active_return_item" in str(
            exc_info.value
        )
        session.rollback()


def test_foreign_keys_enforced() -> None:
    """Verifies that SQLite PRAGMA foreign_keys=ON rejects orphaned child rows."""
    with Session(engine) as session:
        # Order with non-existent customer
        invalid_order = Order(
            order_id="ORD-INVALID",
            customer_id="C999",  # Does not exist
            status="placed",
            payment_method="prepaid",
            payment_status="paid",
            subtotal_paise=10000,
            discount_paise=0,
            shipping_fee_paise=0,
            total_paise=10000,
            placed_at=session.get(Order, "ORD-1048").placed_at,
            ship_to_json="{}",
            version=1,
        )
        session.add(invalid_order)
        with pytest.raises(IntegrityError) as exc_info:
            session.flush()

        assert "FOREIGN KEY constraint failed" in str(exc_info.value)
        session.rollback()


def test_discount_allocation_sums_exactly() -> None:
    """
    Verifies that discount allocation per order item uses integer paise
    with largest-remainder rounding and sums EXACTLY to total discount paise.
    """
    # 1. SC-08 exact test case: 2 kurtas (₹1,398) and 1 dupatta (₹349), discount ₹100
    items = [139800, 34900]
    discount = 10000
    allocated = allocate_discount_largest_remainder(items, discount)
    assert allocated == [8002, 1998]
    assert sum(allocated) == discount
    assert sum(items) - sum(allocated) == 164700

    # 2. Multi-item distribution with fractional remainders
    items_3 = [10000, 10000, 10000]
    discount_1 = 100
    alloc_3 = allocate_discount_largest_remainder(items_3, discount_1)
    # 100 / 3 = 33.33 -> 34, 33, 33
    assert sum(alloc_3) == 100
    assert alloc_3 == [34, 33, 33]

    # 3. 7 items with complex fractional amounts
    items_complex = [12345, 67890, 54321, 98765, 11111, 22222, 33333]
    discount_complex = 7777
    alloc_complex = allocate_discount_largest_remainder(items_complex, discount_complex)
    assert sum(alloc_complex) == discount_complex
    assert all(isinstance(x, int) for x in alloc_complex)

    # 4. Zero discount
    assert allocate_discount_largest_remainder([10000, 20000], 0) == [0, 0]


def test_refund_idempotency_key_unique() -> None:
    """Verifies that refunds.idempotency_key is unique in the database."""
    with Session(engine) as session:
        dup_refund = Refund(
            refund_id="RFD-DUPLICATE",
            order_id="ORD-2040",
            customer_id="C002",
            amount_paise=10000,
            method="original",
            status="reserved",
            idempotency_key="IDEMP-RFD-2040",  # Already used in seeded RFD-2040
            created_at=session.get(Refund, "RFD-2040").created_at,
            updated_at=session.get(Refund, "RFD-2040").updated_at,
            version=1,
        )
        session.add(dup_refund)
        with pytest.raises(IntegrityError) as exc_info:
            session.flush()

        assert "UNIQUE constraint failed" in str(exc_info.value)
        session.rollback()
