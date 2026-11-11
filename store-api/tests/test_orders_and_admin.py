"""
Stage 5 test suite: Read-only store endpoints.

Tests:
1. SC-01: Aarav reading ORD-1048 (in transit, status, items, shipment delayed=false)
2. SC-02: Diya asking for ORD-1042 (Aarav's order) gets uniform 404 (identical to ORD-9999)
          and writes a store_audit row
3. SC-03: Caller identity comes from token only (cannot pass query param to see other orders)
4. Pagination: cursor pagination with limit and next_cursor
5. Delayed flag: ORD-2037 shipment has delayed = True (eta earlier than Clock.now)
6. Refundable: ORD-2040 with bottle item_id:2 returns max_refundable_paise = 50000
7. Seller access: Priya (seller_owner), Kiran (seller_staff), and agent-monitor (orders:read_all)
   can read any order via /admin/orders
8. Role protection: Customer calling /admin routes gets 403 forbidden and store_audit row written
9. Admin customer view: Priya reading C003 shows returns_30d = 4
10. Public products: Unauthenticated GET /api/v1/products returns 200 with products
"""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import db
from app.auth import _mint_access_token, _mint_service_token
from app.main import app
from app.models import StoreAudit, User

client = TestClient(app)


def _get_user_token(email: str) -> str:
    """Helper to mint a valid access token for a seeded user."""
    with Session(db.engine) as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None, f"Seeded user not found: {email}"
        customer_id = None
        if user.role == "customer" and user.customer:
            customer_id = user.customer.customer_id
        return _mint_access_token(user, customer_id)


def test_public_products_endpoint():
    """Public GET /api/v1/products requires no auth."""
    res = client.get("/api/v1/products")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) >= 10
    first = data[0]
    assert "sku" in first
    assert "name" in first
    assert "price_paise" in first
    assert isinstance(first["price_paise"], int)


def test_sc01_aarav_orders_list():
    """
    SC-01: Aarav's order list includes ORD-1048, ORD-1042, ORD-1051, ORD-1060, ORD-1061, ORD-1070.
    All money fields end in _paise and are integers.
    """
    token = _get_user_token("aarav.mehta@example.test")
    res = client.get("/api/v1/orders", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.json()
    order_ids = [o["order_id"] for o in data["items"]]
    for expected_id in ["ORD-1048", "ORD-1042", "ORD-1051", "ORD-1060", "ORD-1061", "ORD-1070"]:
        assert expected_id in order_ids, f"Expected {expected_id} in Aarav's orders"

    # Verify money fields are integer paise
    for order in data["items"]:
        assert isinstance(order["total_paise"], int)
        assert isinstance(order["subtotal_paise"], int)
        assert isinstance(order["discount_paise"], int)
        assert isinstance(order["shipping_fee_paise"], int)
        for item in order["items"]:
            assert isinstance(item["unit_price_paise"], int)
            assert isinstance(item["line_paid_paise"], int)
            assert isinstance(item["discount_alloc_paise"], int)


def test_sc01_ord1048_order_and_shipment():
    """ORD-1048: total_paise 89900, status shipped / in_transit with delayed=False."""
    token = _get_user_token("aarav.mehta@example.test")

    # Order detail
    res = client.get("/api/v1/orders/ORD-1048", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    order = res.json()
    assert order["order_id"] == "ORD-1048"
    assert order["total_paise"] == 89900
    assert order["status"] == "shipped"

    # Shipment detail
    res_shp = client.get(
        "/api/v1/orders/ORD-1048/shipment", headers={"Authorization": f"Bearer {token}"}
    )
    assert res_shp.status_code == 200
    shipment = res_shp.json()
    assert shipment["order_id"] == "ORD-1048"
    assert shipment["status"] == "in_transit"
    assert shipment["delayed"] is False
    assert len(shipment["events"]) > 0


def test_sc02_diya_denied_ord1042_uniform_404_and_audit():
    """
    SC-02: Diya asking for ORD-1042 (Aarav's order) → 404 not_found.
    Diya asking for ORD-9999 (does not exist) → identical body (except request_id).
    Denied access writes a row to store_audit.
    """
    diya_token = _get_user_token("diya.nair@example.test")

    # 1. Diya asks for Aarav's order
    res_denied = client.get(
        "/api/v1/orders/ORD-1042",
        headers={"Authorization": f"Bearer {diya_token}", "X-Request-ID": "req-sc02-denied"},
    )
    assert res_denied.status_code == 404
    denied_body = res_denied.json()

    # 2. Diya asks for non-existent order
    res_missing = client.get(
        "/api/v1/orders/ORD-9999",
        headers={"Authorization": f"Bearer {diya_token}", "X-Request-ID": "req-sc02-missing"},
    )
    assert res_missing.status_code == 404
    missing_body = res_missing.json()

    # Error code and message must be exactly identical
    assert denied_body["error"]["code"] == missing_body["error"]["code"] == "not_found"
    assert denied_body["error"]["message"] == missing_body["error"]["message"] == "Order not found"
    assert denied_body["error"]["details"] == missing_body["error"]["details"] is None

    # Assert store_audit row was created for the denied request
    with Session(db.engine) as session:
        audit = session.scalar(
            select(StoreAudit)
            .where(StoreAudit.request_id == "req-sc02-denied")
            .order_by(StoreAudit.id.desc())
        )
        assert audit is not None, "store_audit row missing for denied order access"
        assert audit.outcome == "denied"
        assert audit.resource == "orders/ORD-1042"
        assert audit.action == "read"


def test_sc03_identity_from_token_only():
    """SC-03: Query parameters attempting to spoof customer_id are ignored."""
    diya_token = _get_user_token("diya.nair@example.test")
    res = client.get(
        "/api/v1/orders?customer_id=C001",
        headers={"Authorization": f"Bearer {diya_token}"},
    )
    assert res.status_code == 200
    data = res.json()
    for order in data["items"]:
        assert order["customer_id"] == "C002"
        assert order["customer_id"] != "C001"


def test_orders_cursor_pagination():
    """Verify limit and cursor pagination on /orders."""
    token = _get_user_token("aarav.mehta@example.test")

    # Fetch page 1 (limit 2)
    res1 = client.get("/api/v1/orders?limit=2", headers={"Authorization": f"Bearer {token}"})
    assert res1.status_code == 200
    page1 = res1.json()
    assert len(page1["items"]) == 2
    assert page1["has_more"] is True
    assert page1["next_cursor"] is not None

    # Fetch page 2 using next_cursor
    res2 = client.get(
        f"/api/v1/orders?limit=2&cursor={page1['next_cursor']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res2.status_code == 200
    page2 = res2.json()
    assert len(page2["items"]) == 2

    # Assert items on page 1 and page 2 are disjoint
    page1_ids = {o["order_id"] for o in page1["items"]}
    page2_ids = {o["order_id"] for o in page2["items"]}
    assert page1_ids.isdisjoint(page2_ids)


def test_ord2037_shipment_delayed_flag():
    """ORD-2037 shipment has eta earlier than Clock.now and status exception -> delayed = True."""
    diya_token = _get_user_token("diya.nair@example.test")
    res = client.get(
        "/api/v1/orders/ORD-2037/shipment",
        headers={"Authorization": f"Bearer {diya_token}"},
    )
    assert res.status_code == 200
    shipment = res.json()
    assert shipment["order_id"] == "ORD-2037"
    assert shipment["delayed"] is True


def test_ord2040_refundable_calculation():
    """
    ORD-2040: 2 bottles of ₹450 each (total ₹900). ₹400 already refunded.
    Checking refundable for 2 bottles yields max_refundable_paise = 50000.
    """
    diya_token = _get_user_token("diya.nair@example.test")

    # Get Diya's ORD-2040 item_id
    res_ord = client.get(
        "/api/v1/orders/ORD-2040",
        headers={"Authorization": f"Bearer {diya_token}"},
    )
    assert res_ord.status_code == 200
    order_data = res_ord.json()
    item_id = order_data["items"][0]["item_id"]

    # Check refundable for 2 units
    res_ref = client.get(
        f"/api/v1/orders/ORD-2040/refundable?items={item_id}:2",
        headers={"Authorization": f"Bearer {diya_token}"},
    )
    assert res_ref.status_code == 200
    ref_data = res_ref.json()
    assert ref_data["order_id"] == "ORD-2040"
    assert ref_data["max_refundable_paise"] == 50000
    assert len(ref_data["items"]) == 1
    assert ref_data["items"][0]["remaining_qty"] == 2
    assert ref_data["items"][0]["requested_qty"] == 2


def test_seller_can_read_any_order():
    """Priya (owner), Kiran (staff), and agent-monitor can read any order via /admin/orders."""
    priya_token = _get_user_token("priya.sharma@nestkart.test")
    kiran_token = _get_user_token("kiran.rao@nestkart.test")
    monitor_token = _mint_service_token("agent-monitor", ["orders:read_all"])

    # Priya reads list and specific order
    res = client.get("/api/v1/admin/orders", headers={"Authorization": f"Bearer {priya_token}"})
    assert res.status_code == 200
    assert len(res.json()["items"]) >= 15

    res_single = client.get(
        "/api/v1/admin/orders/ORD-1048", headers={"Authorization": f"Bearer {priya_token}"}
    )
    assert res_single.status_code == 200
    assert res_single.json()["order_id"] == "ORD-1048"

    # Kiran (staff) reads Aarav's order
    res_kiran = client.get(
        "/api/v1/admin/orders/ORD-1048", headers={"Authorization": f"Bearer {kiran_token}"}
    )
    assert res_kiran.status_code == 200

    # Agent-monitor reads Aarav's order
    res_monitor = client.get(
        "/api/v1/admin/orders/ORD-1048", headers={"Authorization": f"Bearer {monitor_token}"}
    )
    assert res_monitor.status_code == 200


def test_customer_forbidden_on_admin_routes_and_audit():
    """Customer calling /admin routes gets 403 forbidden and store_audit row."""
    token = _get_user_token("aarav.mehta@example.test")
    res = client.get(
        "/api/v1/admin/orders",
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": "req-admin-denied"},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"

    # Verify audit row
    with Session(db.engine) as session:
        audit = session.scalar(
            select(StoreAudit)
            .where(StoreAudit.request_id == "req-admin-denied")
            .order_by(StoreAudit.id.desc())
        )
        assert audit is not None
        assert audit.outcome == "denied"
        assert audit.resource == "/api/v1/admin/orders"


def test_admin_customer_summary_c003():
    """Priya's view of customer C003 shows returns_30d = 4 and account age."""
    priya_token = _get_user_token("priya.sharma@nestkart.test")
    res = client.get(
        "/api/v1/admin/customers/C003",
        headers={"Authorization": f"Bearer {priya_token}"},
    )
    assert res.status_code == 200
    summary = res.json()
    assert summary["customer_id"] == "C003"
    assert summary["returns_30d"] == 4
    assert summary["account_age_days"] == 30
    assert summary["refund_ratio_90d"] > 0
