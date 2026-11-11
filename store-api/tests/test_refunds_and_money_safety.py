"""
Stage 8: Refunds, SimPay, and Money Limits Test Suite

Covers:
- SC-10: Capped at remaining Rs 500
- Over-reservation refused
- SC-16: COD refund rule (store_credit or upi_payout required)
- Parallel reservations race condition (Invariant 2: exactly one wins)
- Autonomy cap with and without stored authorization
- Role limits on authorize (staff <= Rs 2,000, owner above)
- Daily cap enforcement (simulated IST day)
- payment.fail_once retry with same idempotency key succeeds
- payment.timeout_after_commit reconciliation truthful reporting
- Settings GET/PUT RBAC (seller_owner only)
- Customer refund summary endpoint
"""

import concurrent.futures
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import db
from app.auth import _mint_access_token, _mint_service_token
from app.clock import Clock
from app.main import app
from app.models import Order, OrderItem, Refund, StoreSettings, User
from app.simpay import clear_faults, set_fault

client = TestClient(app)

# Track test-created IDs for cleanup
_TEST_ORDER_IDS: list[str] = []
_TEST_REFUND_IDS: list[str] = []


@pytest.fixture(autouse=True)
def clean_faults():
    clear_faults()
    yield
    clear_faults()


@pytest.fixture(scope="module", autouse=True)
def cleanup_test_rows():
    """Remove orders, items, and refunds created during this module."""
    _TEST_ORDER_IDS.clear()
    _TEST_REFUND_IDS.clear()
    yield
    with Session(db.engine) as session:
        if _TEST_REFUND_IDS:
            session.execute(delete(Refund).where(Refund.refund_id.in_(_TEST_REFUND_IDS)))
        if _TEST_ORDER_IDS:
            session.execute(delete(Refund).where(Refund.order_id.in_(_TEST_ORDER_IDS)))
            session.execute(delete(OrderItem).where(OrderItem.order_id.in_(_TEST_ORDER_IDS)))
            session.execute(delete(Order).where(Order.order_id.in_(_TEST_ORDER_IDS)))
        # Restore settings to seed defaults
        settings = session.get(StoreSettings, 1)
        if settings:
            settings.autonomy_cap_paise = 50000
            settings.autonomy_daily_cap_paise = 500000
        session.commit()
    _TEST_ORDER_IDS.clear()
    _TEST_REFUND_IDS.clear()


def _get_user(email: str) -> tuple[User, str]:
    with Session(db.engine) as session:
        user = session.query(User).filter(User.email == email).first()
        assert user is not None
        cust_id = user.customer.customer_id if user.customer else None
        token = _mint_access_token(user, cust_id)
        return user, token


def _get_executor_token() -> str:
    return _mint_service_token("agent-executor", ["refunds:reserve", "refunds:release"])


# ---------------------------------------------------------------------------
# Settings & RBAC Tests
# ---------------------------------------------------------------------------
def test_admin_settings_rbac():
    _, priya_tok = _get_user("priya.sharma@nestkart.test")  # owner
    _, kiran_tok = _get_user("kiran.rao@nestkart.test")  # staff
    _, aarav_tok = _get_user("aarav.mehta@example.test")  # customer

    # Customer gets 403
    r = client.get("/api/v1/admin/settings", headers={"Authorization": f"Bearer {aarav_tok}"})
    assert r.status_code == 403

    # Staff gets 403 on GET and PUT
    r = client.get("/api/v1/admin/settings", headers={"Authorization": f"Bearer {kiran_tok}"})
    assert r.status_code == 403
    r = client.put(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {kiran_tok}"},
        json={"autonomy_cap_paise": 60000},
    )
    assert r.status_code == 403

    # Owner gets 200 on GET
    r = client.get("/api/v1/admin/settings", headers={"Authorization": f"Bearer {priya_tok}"})
    assert r.status_code == 200
    data = r.json()
    assert data["autonomy_cap_paise"] == 50000
    assert data["autonomy_daily_cap_paise"] == 500000

    # Owner updates settings
    r = client.put(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {priya_tok}"},
        json={"autonomy_cap_paise": 75000},
    )
    assert r.status_code == 200
    assert r.json()["autonomy_cap_paise"] == 75000
    assert r.json()["updated_by"] is not None

    # Reset back to 50000
    client.put(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {priya_tok}"},
        json={"autonomy_cap_paise": 50000},
    )


# ---------------------------------------------------------------------------
# SC-10: Capped at remaining Rs 500
# ---------------------------------------------------------------------------
def test_sc10_remaining_refund_cap():
    executor_tok = _get_executor_token()

    over_key = f"IDEMP-SC10-OVER-{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/api/v1/orders/ORD-2040/refund-reservations",
        headers={"Authorization": f"Bearer {executor_tok}", "Idempotency-Key": over_key},
        json={"amount_paise": 50001, "method": "original"},
    )
    assert r.status_code in (400, 409)

    ok_key = f"IDEMP-SC10-OK-{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/api/v1/orders/ORD-2040/refund-reservations",
        headers={"Authorization": f"Bearer {executor_tok}", "Idempotency-Key": ok_key},
        json={"amount_paise": 50000, "method": "original"},
    )
    assert r.status_code in (200, 201)
    res_data = r.json()
    _TEST_REFUND_IDS.append(res_data["refund_id"])
    assert res_data["amount_paise"] == 50000
    assert res_data["status"] == "reserved"

    cancel_key = f"IDEMP-CANCEL-{uuid.uuid4().hex[:8]}"
    client.post(
        f"/api/v1/refunds/{res_data['refund_id']}/cancel-reservation",
        headers={"Authorization": f"Bearer {executor_tok}", "Idempotency-Key": cancel_key},
        json={},
    )


# ---------------------------------------------------------------------------
# SC-16: COD Refund Rule
# ---------------------------------------------------------------------------
def test_sc16_cod_refund_rule():
    executor_tok = _get_executor_token()

    r = client.post(
        "/api/v1/orders/ORD-4020/refund-reservations",
        headers={
            "Authorization": f"Bearer {executor_tok}",
            "Idempotency-Key": f"IDEMP-COD-FAIL-{uuid.uuid4().hex[:8]}",
        },
        json={"amount_paise": 10000, "method": "original"},
    )
    assert r.status_code in (400, 422)
    assert "store_credit or upi_payout" in r.json()["error"]["message"]

    r = client.post(
        "/api/v1/orders/ORD-4020/refund-reservations",
        headers={
            "Authorization": f"Bearer {executor_tok}",
            "Idempotency-Key": f"IDEMP-COD-OK-{uuid.uuid4().hex[:8]}",
        },
        json={"amount_paise": 10000, "method": "store_credit"},
    )
    assert r.status_code in (200, 201)
    _TEST_REFUND_IDS.append(r.json()["refund_id"])
    assert r.json()["method"] == "store_credit"

    cancel_key = f"IDEMP-CANCEL-{uuid.uuid4().hex[:8]}"
    client.post(
        f"/api/v1/refunds/{r.json()['refund_id']}/cancel-reservation",
        headers={"Authorization": f"Bearer {executor_tok}", "Idempotency-Key": cancel_key},
        json={},
    )


# ---------------------------------------------------------------------------
# Race Condition: Two Parallel Reservations (Exactly One Wins; Invariant 2 Holds)
# ---------------------------------------------------------------------------
def test_race_parallel_reservations_exactly_one_wins():
    """
    Two parallel reservation requests for the full remaining amount on an order.
    Exactly one must win, and Invariant 2 (active refunds <= order total) holds.
    """
    executor_tok = _get_executor_token()

    order_id = f"ORD-RACE-{uuid.uuid4().hex[:6]}"
    _TEST_ORDER_IDS.append(order_id)
    with Session(db.engine) as session:
        order = Order(
            order_id=order_id,
            customer_id="C001",
            status="delivered",
            payment_method="prepaid",
            payment_status="paid",
            subtotal_paise=50000,
            discount_paise=0,
            shipping_fee_paise=0,
            total_paise=50000,
            placed_at=Clock.now(),
            delivered_at=Clock.now(),
        )
        item = OrderItem(
            item_id=f"ITEM-{order_id}-1",
            order_id=order_id,
            sku="P-101",
            name_snapshot="Race Test Product",
            category_snapshot="test",
            qty=1,
            unit_price_paise=50000,
            discount_alloc_paise=0,
            line_paid_paise=50000,
        )
        session.add(order)
        session.add(item)
        session.commit()

    def make_reservation(key: str):
        local_client = TestClient(app)
        return local_client.post(
            f"/api/v1/orders/{order_id}/refund-reservations",
            headers={"Authorization": f"Bearer {executor_tok}", "Idempotency-Key": key},
            json={"amount_paise": 50000, "method": "original"},
        )

    key1 = f"IDEMP-RACE-1-{uuid.uuid4().hex[:8]}"
    key2 = f"IDEMP-RACE-2-{uuid.uuid4().hex[:8]}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(make_reservation, key1)
        f2 = pool.submit(make_reservation, key2)
        r1 = f1.result()
        r2 = f2.result()

    statuses = [r1.status_code, r2.status_code]
    success_count = sum(1 for s in statuses if s in (200, 201))
    fail_count = sum(1 for s in statuses if s in (400, 409))

    assert success_count == 1, f"Expected exactly 1 success, got {success_count} ({statuses})"
    assert fail_count == 1, f"Expected exactly 1 failure, got {fail_count} ({statuses})"

    with Session(db.engine) as session:
        active = (
            session.query(Refund)
            .filter(
                Refund.order_id == order_id,
                Refund.status.in_(["reserved", "processing", "completed"]),
            )
            .all()
        )
        assert sum(r.amount_paise for r in active) == 50000


# ---------------------------------------------------------------------------
# Autonomy Cap and Role Limits
# ---------------------------------------------------------------------------
def test_autonomy_cap_and_role_limits():
    executor_tok = _get_executor_token()
    _, priya_tok = _get_user("priya.sharma@nestkart.test")  # owner
    _, kiran_tok = _get_user("kiran.rao@nestkart.test")  # staff

    order_id = f"ORD-CAP-{uuid.uuid4().hex[:6]}"
    _TEST_ORDER_IDS.append(order_id)
    with Session(db.engine) as session:
        order = Order(
            order_id=order_id,
            customer_id="C001",
            status="delivered",
            payment_method="prepaid",
            payment_status="paid",
            subtotal_paise=250000,
            discount_paise=0,
            shipping_fee_paise=0,
            total_paise=250000,
            placed_at=Clock.now(),
            delivered_at=Clock.now(),
        )
        item = OrderItem(
            item_id=f"ITEM-{order_id}-1",
            order_id=order_id,
            sku="P-101",
            name_snapshot="Cap Test Item",
            category_snapshot="test",
            qty=1,
            unit_price_paise=250000,
            discount_alloc_paise=0,
            line_paid_paise=250000,
        )
        session.add(order)
        session.add(item)
        session.commit()

    # 1. Reserve 250,000 paise
    r = client.post(
        f"/api/v1/orders/{order_id}/refund-reservations",
        headers={
            "Authorization": f"Bearer {executor_tok}",
            "Idempotency-Key": f"IDEMP-RES-{uuid.uuid4().hex[:8]}",
        },
        json={"amount_paise": 250000, "method": "original"},
    )
    assert r.status_code in (200, 201)
    refund_id = r.json()["refund_id"]

    # 2. Release without authorization fails (250,000 > 50,000 autonomy cap)
    r = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={
            "Authorization": f"Bearer {executor_tok}",
            "Idempotency-Key": f"IDEMP-REL-{uuid.uuid4().hex[:8]}",
        },
        json={},
    )
    assert r.status_code == 409
    assert "exceeds autonomy cap" in r.json()["error"]["message"]

    # 3. Staff (Kiran) tries to authorize 250,000 paise (> Rs 2,000 limit) -> 403 forbidden
    r = client.post(
        f"/api/v1/refunds/{refund_id}/authorize",
        headers={
            "Authorization": f"Bearer {kiran_tok}",
            "Idempotency-Key": f"IDEMP-AUTH-STAFF-{uuid.uuid4().hex[:8]}",
        },
        json={"amount_paise": 250000},
    )
    assert r.status_code == 403
    assert "up to Rs 2,000" in r.json()["error"]["message"]

    # 4. Owner (Priya) authorizes 250,000 paise -> succeeds
    r = client.post(
        f"/api/v1/refunds/{refund_id}/authorize",
        headers={
            "Authorization": f"Bearer {priya_tok}",
            "Idempotency-Key": f"IDEMP-AUTH-OWNER-{uuid.uuid4().hex[:8]}",
        },
        json={"amount_paise": 250000},
    )
    assert r.status_code == 200
    assert r.json()["authorized_amount_paise"] == 250000

    # 5. Service token release now succeeds because owner authorization exists
    r = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={
            "Authorization": f"Bearer {executor_tok}",
            "Idempotency-Key": f"IDEMP-REL-OK-{uuid.uuid4().hex[:8]}",
        },
        json={},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "processing"


# ---------------------------------------------------------------------------
# Daily Cap on Service Tokens
# ---------------------------------------------------------------------------
def test_daily_cap_enforcement():
    """
    Verify that service-token releases within one IST day are capped.

    Strategy: set daily cap very high for the first release (guaranteed to
    succeed regardless of prior test releases), then drop it to 1 paise so
    the second release is guaranteed to fail.  Uses try/finally to restore.
    """
    executor_tok = _get_executor_token()
    _, priya_tok = _get_user("priya.sharma@nestkart.test")

    ORDER_AMT = 40000  # ₹400 — small enough to stay under default 500,000 total

    # Phase 1: raise caps very high so first release succeeds no matter what
    client.put(
        "/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {priya_tok}"},
        json={"autonomy_daily_cap_paise": 99999999, "autonomy_cap_paise": 99999999},
    )

    try:
        ord1 = f"ORD-DAY1-{uuid.uuid4().hex[:6]}"
        _TEST_ORDER_IDS.append(ord1)
        with Session(db.engine) as session:
            session.add(
                Order(
                    order_id=ord1,
                    customer_id="C001",
                    status="delivered",
                    payment_method="prepaid",
                    payment_status="paid",
                    subtotal_paise=ORDER_AMT,
                    total_paise=ORDER_AMT,
                    placed_at=Clock.now(),
                )
            )
            session.add(
                OrderItem(
                    item_id=f"ITEM-{ord1}-1",
                    order_id=ord1,
                    sku="P-101",
                    name_snapshot="Day 1",
                    category_snapshot="t",
                    qty=1,
                    unit_price_paise=ORDER_AMT,
                    line_paid_paise=ORDER_AMT,
                )
            )
            session.commit()

        r1 = client.post(
            f"/api/v1/orders/{ord1}/refund-reservations",
            headers={
                "Authorization": f"Bearer {executor_tok}",
                "Idempotency-Key": f"IDEMP-D1-{uuid.uuid4().hex[:8]}",
            },
            json={"amount_paise": ORDER_AMT, "method": "original"},
        )
        assert r1.status_code in (200, 201)
        ref1_id = r1.json()["refund_id"]

        r_rel1 = client.post(
            f"/api/v1/refunds/{ref1_id}/release",
            headers={
                "Authorization": f"Bearer {executor_tok}",
                "Idempotency-Key": f"IDEMP-D1-REL-{uuid.uuid4().hex[:8]}",
            },
            json={},
        )
        assert r_rel1.status_code == 200

        # Phase 2: drop daily cap to 1 paise so next release is blocked
        client.put(
            "/api/v1/admin/settings",
            headers={"Authorization": f"Bearer {priya_tok}"},
            json={"autonomy_daily_cap_paise": 1},
        )

        ord2 = f"ORD-DAY2-{uuid.uuid4().hex[:6]}"
        _TEST_ORDER_IDS.append(ord2)
        with Session(db.engine) as session:
            session.add(
                Order(
                    order_id=ord2,
                    customer_id="C001",
                    status="delivered",
                    payment_method="prepaid",
                    payment_status="paid",
                    subtotal_paise=ORDER_AMT,
                    total_paise=ORDER_AMT,
                    placed_at=Clock.now(),
                )
            )
            session.add(
                OrderItem(
                    item_id=f"ITEM-{ord2}-1",
                    order_id=ord2,
                    sku="P-101",
                    name_snapshot="Day 2",
                    category_snapshot="t",
                    qty=1,
                    unit_price_paise=ORDER_AMT,
                    line_paid_paise=ORDER_AMT,
                )
            )
            session.commit()

        r2 = client.post(
            f"/api/v1/orders/{ord2}/refund-reservations",
            headers={
                "Authorization": f"Bearer {executor_tok}",
                "Idempotency-Key": f"IDEMP-D2-{uuid.uuid4().hex[:8]}",
            },
            json={"amount_paise": ORDER_AMT, "method": "original"},
        )
        assert r2.status_code in (200, 201)
        ref2_id = r2.json()["refund_id"]

        r_rel2 = client.post(
            f"/api/v1/refunds/{ref2_id}/release",
            headers={
                "Authorization": f"Bearer {executor_tok}",
                "Idempotency-Key": f"IDEMP-D2-REL-{uuid.uuid4().hex[:8]}",
            },
            json={},
        )
        assert r_rel2.status_code == 409
        assert "Daily service-token release cap" in r_rel2.json()["error"]["message"]
    finally:
        # Always restore defaults so subsequent tests are not affected
        client.put(
            "/api/v1/admin/settings",
            headers={"Authorization": f"Bearer {priya_tok}"},
            json={"autonomy_daily_cap_paise": 500000, "autonomy_cap_paise": 50000},
        )


# ---------------------------------------------------------------------------
# Fault Switches: payment.fail_once and payment.timeout_after_commit
# ---------------------------------------------------------------------------
def test_fault_fail_once_retry_with_same_key_succeeds():
    executor_tok = _get_executor_token()

    ord_id = f"ORD-FAIL1-{uuid.uuid4().hex[:6]}"
    _TEST_ORDER_IDS.append(ord_id)
    with Session(db.engine) as session:
        session.add(
            Order(
                order_id=ord_id,
                customer_id="C001",
                status="delivered",
                payment_method="prepaid",
                payment_status="paid",
                subtotal_paise=20000,
                total_paise=20000,
                placed_at=Clock.now(),
            )
        )
        session.add(
            OrderItem(
                item_id=f"ITEM-{ord_id}-1",
                order_id=ord_id,
                sku="P-101",
                name_snapshot="Item",
                category_snapshot="t",
                qty=1,
                unit_price_paise=20000,
                line_paid_paise=20000,
            )
        )
        session.commit()

    r = client.post(
        f"/api/v1/orders/{ord_id}/refund-reservations",
        headers={
            "Authorization": f"Bearer {executor_tok}",
            "Idempotency-Key": f"IDEMP-R-{uuid.uuid4().hex[:8]}",
        },
        json={"amount_paise": 20000, "method": "original"},
    )
    refund_id = r.json()["refund_id"]

    set_fault("payment.fail_once", True)

    rel_key = f"IDEMP-REL-FAILONCE-{uuid.uuid4().hex[:8]}"
    r_fail = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={"Authorization": f"Bearer {executor_tok}", "Idempotency-Key": rel_key},
        json={},
    )
    assert r_fail.status_code in (502, 503)

    r_chk = client.get(
        f"/api/v1/refunds/{refund_id}",
        headers={"Authorization": f"Bearer {executor_tok}"},
    )
    assert r_chk.json()["status"] == "failed"

    r_retry = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={"Authorization": f"Bearer {executor_tok}", "Idempotency-Key": rel_key},
        json={},
    )
    assert r_retry.status_code == 200
    assert r_retry.json()["status"] == "processing"


def test_fault_timeout_after_commit_truthfully_reported():
    executor_tok = _get_executor_token()

    ord_id = f"ORD-TIMEOUT-{uuid.uuid4().hex[:6]}"
    _TEST_ORDER_IDS.append(ord_id)
    with Session(db.engine) as session:
        session.add(
            Order(
                order_id=ord_id,
                customer_id="C001",
                status="delivered",
                payment_method="prepaid",
                payment_status="paid",
                subtotal_paise=20000,
                total_paise=20000,
                placed_at=Clock.now(),
            )
        )
        session.add(
            OrderItem(
                item_id=f"ITEM-{ord_id}-1",
                order_id=ord_id,
                sku="P-101",
                name_snapshot="Item",
                category_snapshot="t",
                qty=1,
                unit_price_paise=20000,
                line_paid_paise=20000,
            )
        )
        session.commit()

    r = client.post(
        f"/api/v1/orders/{ord_id}/refund-reservations",
        headers={
            "Authorization": f"Bearer {executor_tok}",
            "Idempotency-Key": f"IDEMP-R-{uuid.uuid4().hex[:8]}",
        },
        json={"amount_paise": 20000, "method": "original"},
    )
    refund_id = r.json()["refund_id"]

    set_fault("payment.timeout_after_commit", True)

    rel_key = f"IDEMP-REL-TIMEOUT-{uuid.uuid4().hex[:8]}"
    r_to = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={"Authorization": f"Bearer {executor_tok}", "Idempotency-Key": rel_key},
        json={},
    )
    assert r_to.status_code in (503, 504)

    r_chk = client.get(
        f"/api/v1/refunds/{refund_id}",
        headers={"Authorization": f"Bearer {executor_tok}"},
    )
    assert r_chk.status_code == 200
    assert r_chk.json()["status"] in ("processing", "completed")
    assert r_chk.json()["simulated"] is True


# ---------------------------------------------------------------------------
# Customer Refund Summary
# ---------------------------------------------------------------------------
def test_customer_refund_summary():
    executor_tok = _get_executor_token()
    _, aarav_tok = _get_user("aarav.mehta@example.test")

    r = client.get(
        "/api/v1/customers/C003/refund-summary",
        headers={"Authorization": f"Bearer {aarav_tok}"},
    )
    assert r.status_code == 403

    r = client.get(
        "/api/v1/customers/C003/refund-summary?days=30",
        headers={"Authorization": f"Bearer {executor_tok}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["customer_id"] == "C003"
    assert data["days"] == 30
    assert data["total_refunds_paise"] >= 0
    assert data["refund_count"] >= 0
