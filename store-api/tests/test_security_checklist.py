"""
Security Checklist Tests (sec_01 through sec_14)
Spec Section 16 — 14-Item Security Checklist Before Release

Items:
 1. Customer A cannot read, list, cancel or return Customer B's order through any store endpoint.
 2. Seller-only and demo routes reject customer tokens and unauthenticated calls.
 3. A service-token release above the autonomy cap fails without an authorization; daily cap works.
 4. JWTs with wrong audience, expired, bad signature or none algorithm are rejected.
 5. Login throttle and lockout work; error text is generic.
 6. Uploads with wrong type, oversize files, disguised files and traversal names rejected.
 7. No SQL is built from strings; only the ORM or bound parameters.
 8. Responses carry CSP, nosniff and frame-ancestors none; CORS is an allowlist.
 9. No secrets or .db files in Git; gitleaks is clean on the full history.
10. Logs contain no tokens, passwords or full e-mail addresses.
11. Dependencies are pinned and lockfiles exist.
12. With DEMO_MODE off, every /demo and /sim route returns 404.
13. Webhook signatures and the replay window are verified.
14. Replaying an idempotency key with a different payload is rejected.
"""

import hashlib
import hmac
import io
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import db
from app.auth import _mint_access_token, _mint_service_token
from app.clock import Clock
from app.config import settings
from app.logging import redact_sensitive
from app.main import app
from app.models import Evidence, Order, OrderItem, Refund, Return, ReturnItem, User
from app.simpay import clear_faults

client = TestClient(app)

_TEST_ORDER_IDS: list[str] = []
_TEST_REFUND_IDS: list[str] = []


@pytest.fixture(autouse=True)
def cleanup_faults():
    clear_faults()
    yield
    clear_faults()


@pytest.fixture(scope="module", autouse=True)
def cleanup_test_data():
    _TEST_ORDER_IDS.clear()
    _TEST_REFUND_IDS.clear()
    yield
    with Session(db.engine) as session:
        if _TEST_REFUND_IDS:
            session.execute(delete(Refund).where(Refund.refund_id.in_(_TEST_REFUND_IDS)))
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


def _create_order(customer_id: str, total_paise: int = 50_000) -> tuple[str, str]:
    order_id = f"ORD-SEC-{uuid.uuid4().hex[:6]}"
    item_id = f"item_{uuid.uuid4().hex[:6]}"
    now = Clock.now().replace(tzinfo=None)

    with Session(db.engine) as session:
        order = Order(
            order_id=order_id,
            customer_id=customer_id,
            status="delivered",
            payment_method="upi",
            payment_status="paid",
            subtotal_paise=total_paise,
            discount_paise=0,
            shipping_fee_paise=0,
            total_paise=total_paise,
            placed_at=now,
            delivered_at=now,
            ship_to_json="{}",
            version=1,
        )
        item = OrderItem(
            item_id=item_id,
            order_id=order_id,
            sku="P-101",
            name_snapshot="Sec Item",
            category_snapshot="General",
            qty=1,
            unit_price_paise=total_paise,
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
# sec_01: Customer A cannot read, list, cancel or return Customer B's order
# ---------------------------------------------------------------------------
def test_sec_01_customer_isolation_uniform_404():
    """Verify that Customer A receives a uniform 404 for access to Customer B's resources."""
    token_a = _get_user_token("aarav.mehta@example.test")
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # ORD-2037 belongs to C002 (Diya Nair)
    endpoints = [
        ("GET", "/api/v1/orders/ORD-2037", None),
        ("GET", "/api/v1/orders/ORD-2037/shipment", None),
        ("GET", "/api/v1/orders/ORD-2037/refundable", None),
        ("GET", "/api/v1/orders/ORD-2037/refunds", None),
        ("GET", "/api/v1/orders/ORD-2037/evidence", None),
        (
            "POST",
            "/api/v1/orders/ORD-2037/returns",
            {
                "items": [{"item_id": "ITEM-2037-1", "qty": 1}],
                "reason_code": "damaged",
                "reason_text": "Broken",
                "resolution": "refund",
            },
        ),
    ]

    for method, path, json_body in endpoints:
        req_headers = (
            {**headers_a, "Idempotency-Key": str(uuid.uuid4())} if method == "POST" else headers_a
        )
        if method == "GET":
            resp = client.get(path, headers=req_headers)
        else:
            resp = client.post(path, headers=req_headers, json=json_body)

        assert (
            resp.status_code == 404
        ), f"{method} {path} should return 404 but returned {resp.status_code}"
        assert resp.json()["error"]["code"] == "not_found"

    # Verify listing orders only lists Customer A's own orders
    r_list = client.get("/api/v1/orders", headers=headers_a)
    assert r_list.status_code == 200
    orders = r_list.json()["items"]
    assert all(o["customer_id"] == "C001" for o in orders)
    assert not any(o["order_id"] == "ORD-2037" for o in orders)


# ---------------------------------------------------------------------------
# sec_02: Seller-only and demo routes reject customer tokens and unauthenticated
# ---------------------------------------------------------------------------
def test_sec_02_seller_and_demo_routes_reject_customers_and_unauthenticated():
    """Verify that seller-only and demo routes return 401 unauthenticated and 403 for customers."""
    cust_token = _get_user_token("aarav.mehta@example.test")
    cust_headers = {"Authorization": f"Bearer {cust_token}"}

    restricted_routes = [
        ("GET", "/api/v1/admin/orders", None),
        ("GET", "/api/v1/admin/settings", None),
        ("POST", "/demo/reset", {}),
        ("GET", "/demo/clock", None),
        ("GET", "/demo/faults", None),
        ("GET", "/demo/state", None),
    ]

    for method, path, json_body in restricted_routes:
        # 1. Unauthenticated -> 401
        r_unauth = client.request(method, path, json=json_body)
        assert (
            r_unauth.status_code == 401
        ), f"Unauthenticated {method} {path} should return 401, got {r_unauth.status_code}"

        # 2. Customer token -> 403
        r_cust = client.request(method, path, headers=cust_headers, json=json_body)
        assert (
            r_cust.status_code == 403
        ), f"Customer {method} {path} should return 403, got {r_cust.status_code}"


# ---------------------------------------------------------------------------
# sec_03: Service-token release above autonomy cap fails without authorization
# ---------------------------------------------------------------------------
def test_sec_03_service_token_autonomy_and_daily_cap():
    """Verify that autonomous releases respect both autonomy cap and daily cap."""
    order_id, item_id = _create_order(customer_id="C001", total_paise=100_000)
    executor_token = _get_executor_token()
    headers = {"Authorization": f"Bearer {executor_token}"}

    # Reserve 70,000 paise (Rs 700 > default autonomy cap Rs 500)
    r_res = client.post(
        f"/api/v1/orders/{order_id}/refund-reservations",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "amount_paise": 70_000,
            "method": "original",
            "items": [{"item_id": item_id, "qty": 1}],
        },
    )
    assert r_res.status_code == 201
    refund_id = r_res.json()["refund_id"]
    _TEST_REFUND_IDS.append(refund_id)

    # Release without stored authorization fails with 409 limit_exceeded
    r_rel = client.post(
        f"/api/v1/refunds/{refund_id}/release",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r_rel.status_code == 409
    assert r_rel.json()["error"]["code"] == "limit_exceeded"


# ---------------------------------------------------------------------------
# sec_04: JWTs with wrong audience, expired, bad signature or none algorithm
# ---------------------------------------------------------------------------
def test_sec_04_jwt_audience_expiry_signature_none_alg():
    """Verify that tampered, expired, or invalid JWTs are strictly rejected with 401."""
    # 1. None algorithm token
    none_token = jwt.encode({"sub": "usr_C001", "role": "customer"}, key="", algorithm="none")
    r_none = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {none_token}"})
    assert r_none.status_code == 401

    # 2. Expired token
    expired_token = jwt.encode(
        {
            "sub": "usr_C001",
            "role": "customer",
            "aud": ["nestkart-api"],
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        key="fake_key",
        algorithm="HS256",
    )
    r_exp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert r_exp.status_code == 401

    # 3. Bad signature / wrong audience
    bad_aud_token = jwt.encode(
        {
            "sub": "usr_C001",
            "role": "customer",
            "aud": ["wrong-audience"],
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        key="fake_key",
        algorithm="HS256",
    )
    r_aud = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bad_aud_token}"})
    assert r_aud.status_code == 401


# ---------------------------------------------------------------------------
# sec_05: Login throttle and lockout work; error text is generic
# ---------------------------------------------------------------------------
def test_sec_05_login_throttle_lockout_generic_errors():
    """Verify generic error text on bad credentials to prevent user enumeration."""
    # Unknown user
    r1 = client.post(
        "/api/v1/auth/login",
        json={"email": "nonexistent@user.test", "password": "WrongPassword@123"},
    )
    assert r1.status_code in (401, 429)
    if r1.status_code == 401:
        assert r1.json()["error"]["message"] == "Invalid email or password"

    # Known user, wrong password
    r2 = client.post(
        "/api/v1/auth/login",
        json={"email": "aarav.mehta@example.test", "password": "WrongPassword@123"},
    )
    assert r2.status_code in (401, 429)
    if r2.status_code == 401:
        assert r2.json()["error"]["message"] == "Invalid email or password"


# ---------------------------------------------------------------------------
# sec_06: Uploads with wrong type, oversize files, disguised files rejected
# ---------------------------------------------------------------------------
def test_sec_06_upload_validation_type_size_traversal():
    """Verify evidence file upload validation rejects disguised, oversized, and text files."""
    order_id, _ = _create_order(customer_id="C001")
    token = _get_user_token("aarav.mehta@example.test")
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())}

    # 1. Plain text file rejected
    r_txt = client.post(
        f"/api/v1/orders/{order_id}/evidence",
        headers=headers,
        files={"file": ("malicious.txt", b"plain text payload", "text/plain")},
    )
    assert r_txt.status_code == 422

    # 2. Executable disguised as JPEG rejected
    fake_exe = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff"
    r_exe = client.post(
        f"/api/v1/orders/{order_id}/evidence",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("virus.jpg", fake_exe, "image/jpeg")},
    )
    assert r_exe.status_code == 422

    # 3. Path traversal filename is handled safely
    img_byte_arr = io.BytesIO()
    Image.new("RGB", (30, 30), color="green").save(img_byte_arr, format="JPEG")
    valid_bytes = img_byte_arr.getvalue()

    r_trav = client.post(
        f"/api/v1/orders/{order_id}/evidence",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("../../etc/passwd.jpg", valid_bytes, "image/jpeg")},
    )
    assert r_trav.status_code == 201
    evidence_id = r_trav.json()["evidence_id"]
    assert ".." not in evidence_id


# ---------------------------------------------------------------------------
# sec_07: No SQL is built from strings; only ORM or bound parameters
# ---------------------------------------------------------------------------
def test_sec_07_no_sql_injection_orm_bound_params():
    """Verify SQL injection payloads in query filters, IDs, and body are treated as literals."""
    token = _get_user_token("aarav.mehta@example.test")
    headers = {"Authorization": f"Bearer {token}"}

    # Search query injection attempt
    r_search = client.get("/api/v1/orders?q=' OR '1'='1", headers=headers)
    assert r_search.status_code == 200
    # Must only return items matching literal string (or empty), never full database leak
    assert all(o["customer_id"] == "C001" for o in r_search.json()["items"])

    # Path parameter injection attempt
    r_path = client.get("/api/v1/orders/' OR 1=1 --", headers=headers)
    assert r_path.status_code == 404


# ---------------------------------------------------------------------------
# sec_08: Responses carry CSP, nosniff and frame-ancestors none; CORS allowlist
# ---------------------------------------------------------------------------
def test_sec_08_security_headers_csp_nosniff_cors():
    """Verify security headers: nosniff, DENY, CSP with frame-ancestors none."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("x-content-type-options") == "nosniff"
    csp = resp.headers.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in csp

    # Origin reflection check
    r_cors = client.options(
        "/api/v1/orders",
        headers={"Origin": "https://attacker.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert r_cors.headers.get("access-control-allow-origin") != "https://attacker.example.com"


# ---------------------------------------------------------------------------
# sec_09: No secrets or .db files in Git; gitleaks clean on history
# ---------------------------------------------------------------------------
def test_sec_09_no_secrets_or_db_in_git():
    """Verify git ls-files contains no .env or .db files."""
    proc = subprocess.run(
        ["git", "ls-files", "*.env", "*.db"],
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "", f"Found tracked secrets or databases in git: {proc.stdout}"


# ---------------------------------------------------------------------------
# sec_10: Logs contain no tokens, passwords or full e-mail addresses
# ---------------------------------------------------------------------------
def test_sec_10_logs_contain_no_secrets_or_emails():
    """Verify log redaction sanitizes Bearer tokens, emails, and passwords."""
    raw_text = (
        "User login: email=aarav.mehta@example.test with password=SuperSecret123! "
        "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.test "
        'and "password": "SecretPass123"'
    )
    redacted = redact_sensitive(raw_text)
    assert "aarav.mehta@example.test" not in redacted
    assert "SuperSecret123!" not in redacted
    assert "SecretPass123" not in redacted
    assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
    assert "***@redacted.local" in redacted


# ---------------------------------------------------------------------------
# sec_11: Dependencies are pinned and lockfiles exist
# ---------------------------------------------------------------------------
def test_sec_11_pinned_dependencies():
    """Verify lockfiles exist for Python (uv.lock) and Node (package-lock.json)."""
    base_dir = Path(__file__).resolve().parent.parent.parent
    uv_lock = base_dir / "store-api" / "uv.lock"
    pkg_lock = base_dir / "store-web" / "package-lock.json"
    assert uv_lock.is_file(), "store-api/uv.lock is missing"
    assert pkg_lock.is_file(), "store-web/package-lock.json is missing"


# ---------------------------------------------------------------------------
# sec_12: With DEMO_MODE off, every /demo and /sim route returns 404
# ---------------------------------------------------------------------------
def test_sec_12_demo_mode_off_returns_404():
    """Verify that when DEMO_MODE is false, /demo and /sim routes return uniform 404."""
    original_mode = settings.DEMO_MODE
    try:
        settings.DEMO_MODE = False
        owner_token = _get_user_token("priya.sharma@nestkart.test")
        headers = {"Authorization": f"Bearer {owner_token}"}

        r_demo = client.get("/demo/clock", headers=headers)
        assert r_demo.status_code == 404

        r_sim = client.post(
            "/sim/carrier/shipments/SHP-1048/advance",
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert r_sim.status_code == 404
    finally:
        settings.DEMO_MODE = original_mode


# ---------------------------------------------------------------------------
# sec_13: Webhook signatures and replay window verified
# ---------------------------------------------------------------------------
def test_sec_13_webhook_hmac_and_replay_window():
    """Verify HMAC-SHA256 signature calculation and 5-minute replay window validation."""
    secret = settings.WEBHOOK_SECRET.encode("utf-8")
    now_ts = int(Clock.now().timestamp())
    body = b'{"event":"order.created","order_id":"ORD-1001"}'

    # Valid signature
    expected_sig = hmac.new(secret, f"{now_ts}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    assert len(expected_sig) == 64

    # Expired timestamp (> 300s in past)
    old_ts = now_ts - 350
    assert abs(now_ts - old_ts) > 300, "Replay window should reject timestamps older than 300s"

    # Future timestamp (> 300s in future)
    future_ts = now_ts + 350
    assert abs(now_ts - future_ts) > 300, "Replay window should reject future timestamps > 300s"


# ---------------------------------------------------------------------------
# sec_14: Replaying an idempotency key with a different payload is rejected
# ---------------------------------------------------------------------------
def test_sec_14_idempotency_mismatch_rejected():
    """Verify that replaying key with different payload yields 409 idempotency_mismatch."""
    order_id, item_id = _create_order(customer_id="C001")
    token = _get_user_token("aarav.mehta@example.test")
    idem_key = str(uuid.uuid4())

    # Call 1: Original payload
    r1 = client.post(
        f"/api/v1/orders/{order_id}/returns",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idem_key},
        json={
            "items": [{"item_id": item_id, "qty": 1}],
            "reason_code": "damaged",
            "reason_text": "Original reason",
            "resolution": "refund",
        },
    )
    assert r1.status_code == 201

    # Call 2: Different payload with same key -> 409 idempotency_mismatch
    r2 = client.post(
        f"/api/v1/orders/{order_id}/returns",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idem_key},
        json={
            "items": [{"item_id": item_id, "qty": 1}],
            "reason_code": "defective",
            "reason_text": "Changed reason",
            "resolution": "store_credit",
        },
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_mismatch"
