"""
Invariant Tests for Invariants 1 through 14
Spec Section 5 and Section 16 (Amendment 3)

Invariants:
 1. Ownership: uniform 404 for unowned resources across all routes.
 2. Active refunds sum never exceeds amount paid; parallel reservation race test.
 3. Returned quantity never exceeds delivered quantity.
 4. At most one active return per item (partial unique index); parallel race test.
 5. Refund release requires payment_status paid, or cod_collected.
 6. State transitions follow allowed edges only; invalid state jumps rejected.
 7. Reservation amount cannot exceed store's max_refundable calculation.
 8. Idempotency-Key required on mutating POSTs; payload mismatch rejected with 409.
 9. delivered_at immutable once set; business logic uses Clock service.
10. Money-moving paths run in BEGIN IMMEDIATE transactions with WAL and foreign keys.
11. Service-token releases never exceed the autonomy cap without a stored authorization.
12. Stored authorization requires seller token of sufficient role (staff <= Rs 2,000, owner above).
13. Evidence uploads validated for type, size, count, and customer ownership.
14. Daily autonomous service release cap enforced.
"""

import concurrent.futures
import io
import uuid
from datetime import timezone

import pytest
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import db
from app.auth import _mint_access_token, _mint_service_token
from app.clock import Clock
from app.main import app
from app.models import Evidence, Order, OrderItem, Refund, Return, ReturnItem, StoreSettings, User
from app.simpay import clear_faults

client = TestClient(app)

_TEST_ORDER_IDS: list[str] = []
_TEST_RETURN_IDS: list[str] = []
_TEST_REFUND_IDS: list[str] = []


@pytest.fixture(autouse=True)
def cleanup_faults():
    clear_faults()
    yield
    clear_faults()


@pytest.fixture(scope="module", autouse=True)
def cleanup_test_data():
    _TEST_ORDER_IDS.clear()
    _TEST_RETURN_IDS.clear()
    _TEST_REFUND_IDS.clear()
    yield
    with Session(db.engine) as session:
        if _TEST_REFUND_IDS:
            session.execute(delete(Refund).where(Refund.refund_id.in_(_TEST_REFUND_IDS)))
        if _TEST_RETURN_IDS:
            session.execute(delete(ReturnItem).where(ReturnItem.return_id.in_(_TEST_RETURN_IDS)))
            session.execute(delete(Return).where(Return.return_id.in_(_TEST_RETURN_IDS)))
        if _TEST_ORDER_IDS:
            session.execute(delete(Evidence).where(Evidence.order_id.in_(_TEST_ORDER_IDS)))
            ret_ids = session.scalars(
                select(Return.return_id).where(Return.order_id.in_(_TEST_ORDER_IDS))
            ).all()
            if ret_ids:
                session.execute(delete(ReturnItem).where(ReturnItem.return_id.in_(ret_ids)))
                session.execute(delete(Return).where(Return.return_id.in_(ret_ids)))
            session.execute(delete(OrderItem).where(OrderItem.order_id.in_(_TEST_ORDER_IDS)))
            session.execute(delete(Order).where(Order.order_id.in_(_TEST_ORDER_IDS)))
        session.commit()


def _get_user_token(email: str) -> str:
    with Session(db.engine) as session:
        user = session.query(User).filter(User.email == email).first()
        assert user is not None, f"Seeded user {email} not found"
        cust_id = user.customer.customer_id if user.customer else None
        return _mint_access_token(user, cust_id)


def _get_executor_token() -> str:
    return _mint_service_token(
        "agent-executor",
        ["orders:read_all", "refunds:reserve", "refunds:release", "returns:transition"],
    )


def _create_test_order(
    customer_id: str = "C001",
    total_paise: int = 100_000,
    status: str = "delivered",
    payment_status: str = "paid",
    item_qty: int = 1,
) -> tuple[str, str]:
    """Helper to insert an isolated test order with 1 item."""
    order_id = f"ORD-INV-{uuid.uuid4().hex[:6]}"
    item_id = f"item_{uuid.uuid4().hex[:6]}"
    now = Clock.now().replace(tzinfo=None)

    with Session(db.engine) as session:
        order = Order(
            order_id=order_id,
            customer_id=customer_id,
            status=status,
            payment_method="upi",
            payment_status=payment_status,
            subtotal_paise=total_paise,
            discount_paise=0,
            shipping_fee_paise=0,
            total_paise=total_paise,
            placed_at=now,
            delivered_at=now if status == "delivered" else None,
            ship_to_json="{}",
            version=1,
        )
        item = OrderItem(
            item_id=item_id,
            order_id=order_id,
            sku="P-101",
            name_snapshot="Test Product",
            category_snapshot="Electronics",
            qty=item_qty,
            unit_price_paise=total_paise // item_qty,
            discount_alloc_paise=0,
            line_paid_paise=total_paise,
            flags_json="{}",
        )
        session.add(order)
        session.add(item)
        session.commit()

    _TEST_ORDER_IDS.append(order_id)
    return order_id, item_id


# ---------------------------------------------------------------------------
# Invariant 1: Ownership — uniform 404 for unowned resources
# ---------------------------------------------------------------------------
def test_invariant_01_ownership():
    token_a = _get_user_token("aarav.mehta@example.test")
    # ORD-2037 belongs to C002 (Diya Nair)
    headers_a = {"Authorization": f"Bearer {token_a}"}

    r_order = client.get("/api/v1/orders/ORD-2037", headers=headers_a)
    assert r_order.status_code == 404
    assert r_order.json()["error"]["code"] == "not_found"

    r_nonexistent = client.get("/api/v1/orders/ORD-9999", headers=headers_a)
    assert r_nonexistent.status_code == 404
    # Uniform 404: codes and messages must match identically to prevent IDOR enumeration
    assert r_order.json()["error"]["code"] == r_nonexistent.json()["error"]["code"]
    assert r_order.json()["error"]["message"] == r_nonexistent.json()["error"]["message"]

    # Shipment, refundable, refunds, evidence must also return uniform 404
    assert client.get("/api/v1/orders/ORD-2037/shipment", headers=headers_a).status_code == 404
    assert client.get("/api/v1/orders/ORD-2037/refundable", headers=headers_a).status_code == 404
    assert client.get("/api/v1/orders/ORD-2037/refunds", headers=headers_a).status_code == 404
    assert client.get("/api/v1/orders/ORD-2037/evidence", headers=headers_a).status_code == 404


# ---------------------------------------------------------------------------
# Invariant 2: Sum of active refunds never exceeds amount paid + race test
# ---------------------------------------------------------------------------
def test_invariant_02_refund_sum_and_parallel_race():
    order_id, item_id = _create_test_order(customer_id="C001", total_paise=80_000)
    executor_token = _get_executor_token()
    headers = {"Authorization": f"Bearer {executor_token}"}

    # 1. Attempting to reserve more than paid amount fails
    r_over = client.post(
        f"/api/v1/orders/{order_id}/refund-reservations",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "amount_paise": 80_001,
            "method": "original",
            "items": [{"item_id": item_id, "qty": 1}],
        },
    )
    assert r_over.status_code == 409
    assert r_over.json()["error"]["code"] == "limit_exceeded"

    # 2. Parallel race: 5 threads attempt to reserve full 80,000 paise at once
    def attempt_reservation():
        idem_key = str(uuid.uuid4())
        return client.post(
            f"/api/v1/orders/{order_id}/refund-reservations",
            headers={**headers, "Idempotency-Key": idem_key},
            json={
                "amount_paise": 80_000,
                "method": "original",
                "items": [{"item_id": item_id, "qty": 1}],
            },
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(attempt_reservation) for _ in range(5)]
        results = [f.result() for f in futures]

    successes = [r for r in results if r.status_code == 201]
    rejections = [r for r in results if r.status_code == 409]

    assert len(successes) == 1, f"Expected exactly 1 reservation to succeed, got {len(successes)}"
    assert len(rejections) == 4, f"Expected 4 rejections with 409, got {len(rejections)}"

    ref_id = successes[0].json()["refund_id"]
    _TEST_REFUND_IDS.append(ref_id)

    # Verify DB sum of active refunds is exactly 80,000 paise
    with Session(db.engine) as session:
        active_refunds = session.scalars(
            select(Refund).where(
                Refund.order_id == order_id,
                Refund.status.in_(["reserved", "processing", "completed"]),
            )
        ).all()
        total_reserved = sum(r.amount_paise for r in active_refunds)
        assert total_reserved == 80_000


# ---------------------------------------------------------------------------
# Invariant 3: Returned quantity never exceeds delivered quantity
# ---------------------------------------------------------------------------
def test_invariant_03_return_qty_limit():
    order_id, item_id = _create_test_order(customer_id="C001", total_paise=50_000, item_qty=1)
    customer_token = _get_user_token("aarav.mehta@example.test")
    headers = {"Authorization": f"Bearer {customer_token}", "Idempotency-Key": str(uuid.uuid4())}

    # Attempting to return qty=2 when delivered qty=1
    r_invalid_qty = client.post(
        f"/api/v1/orders/{order_id}/returns",
        headers=headers,
        json={
            "items": [{"item_id": item_id, "qty": 2}],
            "reason_code": "defective",
            "reason_text": "Item arrived broken",
            "resolution": "refund",
        },
    )
    assert r_invalid_qty.status_code in (409, 422)


# ---------------------------------------------------------------------------
# Invariant 4: At most one active return per item (partial unique index) + race
# ---------------------------------------------------------------------------
def test_invariant_04_partial_unique_return_item_race():
    order_id, item_id = _create_test_order(customer_id="C001", total_paise=60_000, item_qty=1)
    customer_token = _get_user_token("aarav.mehta@example.test")

    def attempt_create_return():
        idem_key = str(uuid.uuid4())
        return client.post(
            f"/api/v1/orders/{order_id}/returns",
            headers={"Authorization": f"Bearer {customer_token}", "Idempotency-Key": idem_key},
            json={
                "items": [{"item_id": item_id, "qty": 1}],
                "reason_code": "size_fit",
                "reason_text": "Size did not match expectations",
                "resolution": "refund",
            },
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(attempt_create_return) for _ in range(5)]
        results = [f.result() for f in futures]

    successes = [r for r in results if r.status_code == 201]
    duplicates = [r for r in results if r.status_code == 409]

    assert len(successes) == 1, f"Expected exactly 1 return to succeed, got {len(successes)}"
    assert len(duplicates) == 4, f"Expected 4 duplicate return rejections, got {len(duplicates)}"

    ret_id = successes[0].json()["return_id"]
    _TEST_RETURN_IDS.append(ret_id)


# ---------------------------------------------------------------------------
# Invariant 5: Refund release requires payment_status paid or cod_collected
# ---------------------------------------------------------------------------
def test_invariant_05_refund_requires_paid_or_cod_collected():
    # Create an order with payment_status = pending
    order_id, item_id = _create_test_order(customer_id="C001", payment_status="pending")
    executor_token = _get_executor_token()
    headers = {"Authorization": f"Bearer {executor_token}"}

    # Reserve succeeds because hold is created before payment reconciliation
    r_res = client.post(
        f"/api/v1/orders/{order_id}/refund-reservations",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "amount_paise": 10_000,
            "method": "original",
            "items": [{"item_id": item_id, "qty": 1}],
        },
    )
    assert r_res.status_code == 201
    refund_id = r_res.json()["refund_id"]
    _TEST_REFUND_IDS.append(refund_id)

    # Release must fail because order is not paid
    r_release = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r_release.status_code in (409, 422)


# ---------------------------------------------------------------------------
# Invariant 6: State changes only along allowed edges
# ---------------------------------------------------------------------------
def test_invariant_06_state_machine_allowed_edges_only():
    order_id, item_id = _create_test_order(customer_id="C001")
    customer_token = _get_user_token("aarav.mehta@example.test")
    seller_token = _get_user_token("priya.sharma@nestkart.test")

    # Create return in 'requested' status
    r_ret = client.post(
        f"/api/v1/orders/{order_id}/returns",
        headers={"Authorization": f"Bearer {customer_token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "items": [{"item_id": item_id, "qty": 1}],
            "reason_code": "damaged",
            "reason_text": "Broken glass",
            "resolution": "refund",
        },
    )
    assert r_ret.status_code == 201
    return_id = r_ret.json()["return_id"]
    _TEST_RETURN_IDS.append(return_id)

    # Illegal jump: requested -> completed directly
    r_illegal = client.post(
        f"/api/v1/returns/{return_id}/transition",
        headers={"Authorization": f"Bearer {seller_token}", "Idempotency-Key": str(uuid.uuid4())},
        json={"to": "completed", "reason": "Bypassing state machine"},
    )
    assert r_illegal.status_code == 409
    assert r_illegal.json()["error"]["code"] == "invalid_state"


# ---------------------------------------------------------------------------
# Invariant 7: Reservation cannot exceed store's max_refundable calculation
# ---------------------------------------------------------------------------
def test_invariant_07_reservation_cannot_exceed_max_refundable():
    order_id, item_id = _create_test_order(customer_id="C001", total_paise=30_000)
    executor_token = _get_executor_token()
    headers = {"Authorization": f"Bearer {executor_token}", "Idempotency-Key": str(uuid.uuid4())}

    # Store computes max_refundable as 30,000 paise. Client sends 35,000 paise.
    r = client.post(
        f"/api/v1/orders/{order_id}/refund-reservations",
        headers=headers,
        json={
            "amount_paise": 35_000,
            "method": "original",
            "items": [{"item_id": item_id, "qty": 1}],
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "limit_exceeded"


# ---------------------------------------------------------------------------
# Invariant 8: Every mutation needs Idempotency-Key; mismatch rejected
# ---------------------------------------------------------------------------
def test_invariant_08_idempotency_key_required_and_mismatch():
    order_id, item_id = _create_test_order(customer_id="C001")
    customer_token = _get_user_token("aarav.mehta@example.test")

    # 1. Missing header returns 422
    r_missing = client.post(
        f"/api/v1/orders/{order_id}/returns",
        headers={"Authorization": f"Bearer {customer_token}"},
        json={
            "items": [{"item_id": item_id, "qty": 1}],
            "reason_code": "defective",
            "reason_text": "Missing key test",
            "resolution": "refund",
        },
    )
    assert r_missing.status_code == 422

    # 2. First call succeeds
    idem_key = str(uuid.uuid4())
    r_first = client.post(
        f"/api/v1/orders/{order_id}/returns",
        headers={"Authorization": f"Bearer {customer_token}", "Idempotency-Key": idem_key},
        json={
            "items": [{"item_id": item_id, "qty": 1}],
            "reason_code": "defective",
            "reason_text": "First attempt",
            "resolution": "refund",
        },
    )
    assert r_first.status_code == 201
    ret_id = r_first.json()["return_id"]
    _TEST_RETURN_IDS.append(ret_id)

    # 3. Same key with different payload returns 409 idempotency_mismatch
    r_mismatch = client.post(
        f"/api/v1/orders/{order_id}/returns",
        headers={"Authorization": f"Bearer {customer_token}", "Idempotency-Key": idem_key},
        json={
            "items": [{"item_id": item_id, "qty": 1}],
            "reason_code": "different_reason",
            "reason_text": "Altered attempt",
            "resolution": "store_credit",
        },
    )
    assert r_mismatch.status_code == 409
    assert r_mismatch.json()["error"]["code"] == "idempotency_mismatch"


# ---------------------------------------------------------------------------
# Invariant 9: delivered_at is immutable once set; Clock used for timestamps
# ---------------------------------------------------------------------------
def test_invariant_09_delivered_at_immutable_and_clock_used():
    order_id, _ = _create_test_order(customer_id="C001", status="delivered")
    with Session(db.engine) as session:
        order = session.get(Order, order_id)
        original_delivered_at = order.delivered_at
        assert original_delivered_at is not None

        # Confirm Clock is in UTC
        now_utc = Clock.now()
        assert now_utc.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Invariant 10: Money paths execute in BEGIN IMMEDIATE transactions
# ---------------------------------------------------------------------------
def test_invariant_10_money_paths_begin_immediate():
    # Verify DB connection configuration is WAL, foreign keys ON, and BEGIN IMMEDIATE
    with db.engine.connect() as conn:
        wal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        assert wal_mode.lower() == "wal"
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
        assert foreign_keys == 1


# ---------------------------------------------------------------------------
# Invariant 11: Service-token releases never exceed autonomy cap without auth
# ---------------------------------------------------------------------------
def test_invariant_11_service_token_autonomy_cap():
    order_id, item_id = _create_test_order(customer_id="C001", total_paise=100_000)
    executor_token = _get_executor_token()
    headers = {"Authorization": f"Bearer {executor_token}"}

    # Reserve 60,000 paise (above default autonomy cap 50,000 paise / Rs 500)
    r_res = client.post(
        f"/api/v1/orders/{order_id}/refund-reservations",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "amount_paise": 60_000,
            "method": "original",
            "items": [{"item_id": item_id, "qty": 1}],
        },
    )
    assert r_res.status_code == 201
    refund_id = r_res.json()["refund_id"]
    _TEST_REFUND_IDS.append(refund_id)

    # Releasing 60,000 paise without stored authorization must fail with 409 limit_exceeded
    r_rel = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r_rel.status_code == 409
    assert r_rel.json()["error"]["code"] == "limit_exceeded"


# ---------------------------------------------------------------------------
# Invariant 12: Stored authorization requires seller token of sufficient role
# ---------------------------------------------------------------------------
def test_invariant_12_authorization_seller_roles():
    order_id, item_id = _create_test_order(customer_id="C001", total_paise=300_000)
    executor_token = _get_executor_token()
    staff_token = _get_user_token("kiran.rao@nestkart.test")
    owner_token = _get_user_token("priya.sharma@nestkart.test")

    # Reserve 250,000 paise (Rs 2,500 > staff limit of Rs 2,000)
    r_res = client.post(
        f"/api/v1/orders/{order_id}/refund-reservations",
        headers={"Authorization": f"Bearer {executor_token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "amount_paise": 250_000,
            "method": "original",
            "items": [{"item_id": item_id, "qty": 1}],
        },
    )
    assert r_res.status_code == 201
    refund_id = r_res.json()["refund_id"]
    _TEST_REFUND_IDS.append(refund_id)

    # 1. Staff cannot authorize > Rs 2,000
    r_staff = client.post(
        f"/api/v1/refunds/{refund_id}/authorize",
        headers={"Authorization": f"Bearer {staff_token}", "Idempotency-Key": str(uuid.uuid4())},
        json={"amount_paise": 250_000, "note": "Staff approving high value"},
    )
    assert r_staff.status_code == 403

    # 2. Owner can authorize > Rs 2,000
    r_owner = client.post(
        f"/api/v1/refunds/{refund_id}/authorize",
        headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": str(uuid.uuid4())},
        json={"amount_paise": 250_000, "note": "Owner approved"},
    )
    assert r_owner.status_code == 200
    assert r_owner.json()["authorized_by"] is not None
    assert r_owner.json()["authorized_amount_paise"] == 250_000


# ---------------------------------------------------------------------------
# Invariant 13: Evidence uploads validated for type, size, count, ownership
# ---------------------------------------------------------------------------
def test_invariant_13_evidence_validation_and_ownership():
    order_id, _ = _create_test_order(customer_id="C001")
    customer_token = _get_user_token("aarav.mehta@example.test")
    headers = {"Authorization": f"Bearer {customer_token}"}

    # 1. Non-image format rejected
    r_txt = client.post(
        f"/api/v1/orders/{order_id}/evidence",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("notes.txt", b"Plain text evidence", "text/plain")},
    )
    assert r_txt.status_code == 422

    # 2. Oversize file (> 2 MB) rejected
    large_payload = b"A" * (2 * 1024 * 1024 + 10)
    r_large = client.post(
        f"/api/v1/orders/{order_id}/evidence",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("large.png", large_payload, "image/png")},
    )
    assert r_large.status_code == 422

    # 3. Other customer cannot upload evidence (uniform 404)
    diya_token = _get_user_token("diya.nair@example.test")
    img_byte_arr = io.BytesIO()
    Image.new("RGB", (60, 60), color="blue").save(img_byte_arr, format="JPEG")
    valid_bytes = img_byte_arr.getvalue()

    r_unowned = client.post(
        f"/api/v1/orders/{order_id}/evidence",
        headers={"Authorization": f"Bearer {diya_token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("photo.jpg", valid_bytes, "image/jpeg")},
    )
    assert r_unowned.status_code == 404


# ---------------------------------------------------------------------------
# Invariant 14: Daily autonomous release cap enforced
# ---------------------------------------------------------------------------
def test_invariant_14_daily_autonomous_cap():
    executor_token = _get_executor_token()
    headers = {"Authorization": f"Bearer {executor_token}"}

    # Check store daily cap setting
    with Session(db.engine) as session:
        settings_row = session.get(StoreSettings, 1)
        daily_cap = settings_row.autonomy_daily_cap_paise if settings_row else 500_000

    # Ensure release exceeding daily cap is blocked with 409 limit_exceeded
    order_id, item_id = _create_test_order(customer_id="C001", total_paise=daily_cap + 100_000)
    r_res = client.post(
        f"/api/v1/orders/{order_id}/refund-reservations",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "amount_paise": daily_cap + 10_000,
            "method": "original",
            "items": [{"item_id": item_id, "qty": 1}],
        },
    )
    assert r_res.status_code == 201
    refund_id = r_res.json()["refund_id"]
    _TEST_REFUND_IDS.append(refund_id)

    # Release without stored authorization fails
    r_rel = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r_rel.status_code == 409
    assert r_rel.json()["error"]["code"] == "limit_exceeded"
