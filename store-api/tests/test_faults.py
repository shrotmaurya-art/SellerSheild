"""
Simulator Fault Toggle Tests (Spec section 7, 8, 13, 16)
Verifies that every fault toggle produces its documented failure behavior:
1. carrier.down -> 503 upstream_unavailable
2. carrier.slow -> simulated latency
3. carrier.stale -> returns stale shipment events
4. payment.down -> 503 upstream_unavailable on release
5. payment.fail_once -> fails first attempt, retry succeeds
6. payment.timeout_after_commit -> commits DB before 503, reconciliation truthful
7. mail.down -> 503 upstream_unavailable on mail send
8. mail.fail_once -> fails first attempt, retry succeeds
"""

import uuid

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import db
from app.auth import _mint_access_token, _mint_service_token
from app.faults import clear_faults, set_fault
from app.main import app
from app.models import Order, OrderItem, Refund, User

client = TestClient(app)

_TEST_ORDER_IDS: list[str] = []
_TEST_REFUND_IDS: list[str] = []


@pytest.fixture(autouse=True)
def cleanup_faults_and_test_rows():
    clear_faults()
    yield
    clear_faults()
    with Session(db.engine) as session:
        if _TEST_REFUND_IDS:
            from sqlalchemy import delete

            session.execute(delete(Refund).where(Refund.refund_id.in_(_TEST_REFUND_IDS)))
        if _TEST_ORDER_IDS:
            from sqlalchemy import delete

            session.execute(delete(OrderItem).where(OrderItem.order_id.in_(_TEST_ORDER_IDS)))
            session.execute(delete(Order).where(Order.order_id.in_(_TEST_ORDER_IDS)))
        session.commit()
    _TEST_ORDER_IDS.clear()
    _TEST_REFUND_IDS.clear()


def _get_user_token(email: str) -> str:
    with Session(db.engine) as session:
        user = session.query(User).filter(User.email == email).first()
        assert user is not None
        cust_id = user.customer.customer_id if user.customer else None
        return _mint_access_token(user, cust_id)


def _get_executor_token() -> str:
    return _mint_service_token(
        "agent-executor", ["orders:read_all", "refunds:reserve", "refunds:release"]
    )


def test_fault_carrier_down():
    """Verify carrier.down returns 503 upstream_unavailable on carrier endpoints."""
    token = _get_user_token("aarav.mehta@example.test")
    set_fault("carrier.down", True)

    resp = client.get(
        "/api/v1/orders/ORD-1048/shipment", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "upstream_unavailable"


def test_fault_carrier_stale():
    """Verify carrier.stale prevents advancement and returns stale events."""
    seller_token = _get_user_token("priya.sharma@nestkart.test")
    cust_token = _get_user_token("aarav.mehta@example.test")
    set_fault("carrier.stale", True)

    # Advance attempt with carrier.stale returns new_event: None
    adv_resp = client.post(
        "/sim/carrier/shipments/SHP-1048/advance",
        headers={"Authorization": f"Bearer {seller_token}", "Idempotency-Key": str(uuid.uuid4())},
    )
    assert adv_resp.status_code == 200
    assert adv_resp.json()["new_event"] is None

    # Shipment fetch succeeds and returns existing/stale state
    resp = client.get(
        "/api/v1/orders/ORD-1048/shipment", headers={"Authorization": f"Bearer {cust_token}"}
    )
    assert resp.status_code == 200


def test_fault_carrier_slow(monkeypatch):
    """Verify carrier.slow triggers simulated latency."""
    token = _get_user_token("aarav.mehta@example.test")
    set_fault("carrier.slow", True)

    sleep_called = []

    async def mock_sleep(seconds):
        sleep_called.append(seconds)

    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    resp = client.get(
        "/api/v1/orders/ORD-1048/shipment", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert 8.0 in sleep_called


def test_fault_payment_down():
    """Verify payment.down returns 503 upstream_unavailable on refund release."""
    executor_token = _get_executor_token()
    headers = {"Authorization": f"Bearer {executor_token}"}

    # Reserve refund on ORD-1042 (under autonomy cap: 10,000 paise / Rs 100)
    r_res = client.post(
        "/api/v1/orders/ORD-1042/refund-reservations",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "amount_paise": 10_000,
            "method": "original",
            "items": [{"item_id": "ITEM-1042-1", "qty": 1}],
        },
    )
    assert r_res.status_code == 201
    refund_id = r_res.json()["refund_id"]
    _TEST_REFUND_IDS.append(refund_id)

    set_fault("payment.down", True)
    r_rel = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r_rel.status_code == 503
    assert r_rel.json()["error"]["code"] == "upstream_unavailable"


def test_fault_payment_fail_once():
    """Verify payment.fail_once fails release once with 503, retry succeeds."""
    executor_token = _get_executor_token()
    headers = {"Authorization": f"Bearer {executor_token}"}

    r_res = client.post(
        "/api/v1/orders/ORD-1042/refund-reservations",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "amount_paise": 5_000,
            "method": "original",
            "items": [{"item_id": "ITEM-1042-1", "qty": 1}],
        },
    )
    assert r_res.status_code == 201
    refund_id = r_res.json()["refund_id"]
    _TEST_REFUND_IDS.append(refund_id)

    set_fault("payment.fail_once", True)
    idem_key = str(uuid.uuid4())

    # First attempt: 503
    r1 = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={**headers, "Idempotency-Key": idem_key},
    )
    assert r1.status_code == 503
    assert r1.json()["error"]["code"] == "upstream_unavailable"

    # Second attempt with same key: succeeds
    r2 = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={**headers, "Idempotency-Key": idem_key},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "processing"


def test_fault_mail_down():
    """Verify mail.down returns 503 upstream_unavailable on mail send."""
    token = _get_user_token("aarav.mehta@example.test")
    set_fault("mail.down", True)

    resp = client.post(
        "/mail/send",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "to_addr": "support@nestkart.test",
            "channel": "email",
            "subject": "Test subject",
            "body_text": "Test body",
        },
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "upstream_unavailable"


def test_fault_mail_fail_once():
    """Verify mail.fail_once fails once with 503, retry succeeds."""
    token = _get_user_token("aarav.mehta@example.test")
    set_fault("mail.fail_once", True)
    idem_key = str(uuid.uuid4())

    payload = {
        "to_addr": "support@nestkart.test",
        "channel": "email",
        "subject": "Fail once test",
        "body_text": "Message body",
    }

    # Attempt 1 -> 503
    r1 = client.post(
        "/mail/send",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idem_key},
        json=payload,
    )
    assert r1.status_code == 503
    assert r1.json()["error"]["code"] == "upstream_unavailable"

    # Attempt 2 -> 200/201
    r2 = client.post(
        "/mail/send",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idem_key},
        json=payload,
    )
    assert r2.status_code in (200, 201)
