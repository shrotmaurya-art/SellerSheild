"""Contract and integration tests for HttpStoreClient (T1.9).

Enforces:
- Full coverage of StoreClient protocol against openapi-store.yaml schemas.
- Passthrough of customer token and X-Request-ID headers (KP-13).
- Tokens are SecretStr and never logged (KP-13).
- Uniform 404 for unowned and nonexistent orders (KP-15).
- Service token caching for client credentials (60 s pre-expiry refresh).
- Error mapping to domain exceptions (404, 401/403, 409, 5xx, timeouts).
- Bounded retry on idempotent GET for 502/503/504 and connection errors.
- Extra unknown response fields are dropped without error.
- STORE_MODE validation and refusal outside DEV_MODE (KP-24).
- readyz reporting active store_mode and reachability.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from pydantic import SecretStr
from starlette.testclient import TestClient

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import ConfigError
from sellershield_agent.domain.models import (
    CustomerRiskFacts,
    Order,
    OrderStatus,
    Refund,
    Refundable,
    ReturnRequest,
    Shipment,
)
from sellershield_agent.main import create_app
from sellershield_agent.store.client import (
    RequestContext,
    StoreClient,
    StoreConflict,
    StoreForbidden,
    StoreNotFound,
    StoreTimeout,
    StoreUnavailable,
    StoreValidationError,
)
from sellershield_agent.store.http_client import HttpStoreClient

# ── Fixtures & Helpers ────────────────────────────────────────────────


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock(fixed_now: datetime) -> FixedClock:
    return FixedClock(fixed_now)


@pytest.fixture
def customer_ctx() -> RequestContext:
    return RequestContext(
        principal=Principal(
            user_id="U001",
            customer_id="C001",
            role=Role.customer,
            jti="jti-test-1",
            token_exp=1799999999,
        ),
        request_id="req-test-123",
        token=SecretStr("customer-access-token-xyz"),
    )


@pytest.fixture
def service_ctx() -> RequestContext:
    return RequestContext(
        principal=Principal(
            user_id="SRV01",
            customer_id=None,
            role=Role.service,
            jti="jti-test-2",
            token_exp=1799999999,
        ),
        request_id="req-srv-456",
        token=None,
    )


@pytest.fixture
def http_store(clock: FixedClock) -> HttpStoreClient:
    return HttpStoreClient(
        base_url="http://localhost:8000",
        monitor_client_id="test-monitor-id",
        monitor_client_secret=SecretStr("test-monitor-secret"),
        clock=clock,
    )


SAMPLE_ORDER_JSON: dict[str, Any] = {
    "order_id": "ORD-1048",
    "customer_id": "C001",
    "status": "in_transit",
    "payment_method": "upi",
    "payment_status": "captured",
    "subtotal_paise": 89900,
    "discount_paise": 0,
    "shipping_fee_paise": 0,
    "total_paise": 89900,
    "items": [
        {
            "item_id": "ITEM_01",
            "sku": "SKU-HEADPHONES",
            "name": "Wireless Noise Cancelling Headphones",
            "category": "electronics",
            "qty": 1,
            "unit_price_paise": 89900,
            "discount_alloc_paise": 0,
            "line_paid_paise": 89900,
            "flags": {},
        }
    ],
    "ship_to": {
        "full_name": "Aarav Mehta",
        "line1": "Flat 4B, Palm Grove",
        "city": "Bengaluru",
        "state": "Karnataka",
        "postal_code": "560001",
        "country": "IN",
    },
    "placed_at": "2026-11-07T08:30:00Z",
    "version": 1,
}

SAMPLE_SHIPMENT_JSON: dict[str, Any] = {
    "order_id": "ORD-1048",
    "shipment": {
        "shipment_id": "SHIP-1048",
        "order_id": "ORD-1048",
        "carrier": "BlueDart",
        "tracking_no": "BD-987654321",
        "status": "in_transit",
        "eta_at": "2026-11-11T18:00:00Z",
        "delayed": False,
        "events": [
            {
                "event_id": "EVT-01",
                "occurred_at": "2026-11-08T09:00:00Z",
                "status": "shipped",
                "location": "Bengaluru Hub",
                "note": "Package received at origin facility",
            }
        ],
    },
}

SAMPLE_REFUNDABLE_JSON: dict[str, Any] = {
    "order_id": "ORD-1048",
    "max_refundable_paise": 89900,
    "items": [
        {
            "item_id": "ITEM_01",
            "original_qty": 1,
            "remaining_qty": 1,
            "line_paid_paise": 89900,
            "refundable_paise": 89900,
        }
    ],
}

SAMPLE_RETURN_JSON: dict[str, Any] = {
    "return_id": "RET-101",
    "order_id": "ORD-1048",
    "customer_id": "C001",
    "status": "requested",
    "reason_code": "defective",
    "reason_text": "One side audio is not working",
    "resolution": "refund",
    "items": [{"item_id": "ITEM_01", "qty": 1, "condition_note": "defective"}],
    "created_at": "2026-11-09T08:00:00Z",
    "version": 1,
}

SAMPLE_REFUND_JSON: dict[str, Any] = {
    "refund_id": "RFD-501",
    "order_id": "ORD-1048",
    "customer_id": "C001",
    "amount_paise": 89900,
    "method": "original_payment",
    "status": "completed",
    "gateway_ref": "SIMULATED-PAY-REF-1048",
    "created_at": "2026-11-09T09:00:00Z",
}

SAMPLE_TOKEN_JSON: dict[str, Any] = {
    "access_token": "service-monitor-jwt-token-abc",
    "token_type": "Bearer",
    "expires_in": 600,
    "scopes": ["orders:read_all", "shipments:read", "mail:send"],
}


# ── Protocol Compliance ───────────────────────────────────────────────


def test_http_store_implements_store_client(http_store: HttpStoreClient) -> None:
    """HttpStoreClient must satisfy StoreClient protocol without modifications."""
    assert isinstance(http_store, StoreClient)


# ── Success & Header Forwarding Tests ─────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_http_store_list_orders_success(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """list_orders forwards token, X-Request-ID, drops extra fields, returns Order models."""
    response_payload = {
        "orders": [
            {**SAMPLE_ORDER_JSON, "unknown_field_123": "ignore_me"},
        ],
        "next_cursor": None,
        "unexpected_metadata": {"foo": "bar"},
    }
    route = respx.get("http://localhost:8000/api/v1/orders").respond(json=response_payload)

    orders = await http_store.list_orders(customer_ctx)

    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer customer-access-token-xyz"
    assert req.headers["X-Request-ID"] == "req-test-123"

    assert len(orders) == 1
    assert isinstance(orders[0], Order)
    assert orders[0].order_id == "ORD-1048"
    assert orders[0].status == OrderStatus.in_transit
    assert orders[0].total_paise == 89900


@respx.mock
@pytest.mark.asyncio
async def test_http_store_get_order_success(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """get_order fetches order details, validates Pydantic model, ignores unknown fields."""
    order_data = {**SAMPLE_ORDER_JSON, "extra_store_tag": "vip"}
    route = respx.get("http://localhost:8000/api/v1/orders/ORD-1048").respond(json=order_data)

    order = await http_store.get_order(customer_ctx, "ORD-1048")

    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer customer-access-token-xyz"
    assert req.headers["X-Request-ID"] == "req-test-123"

    assert isinstance(order, Order)
    assert order.order_id == "ORD-1048"
    assert order.customer_id == "C001"
    assert order.items[0].sku == "SKU-HEADPHONES"


@respx.mock
@pytest.mark.asyncio
async def test_http_store_get_shipment_success(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """get_shipment calls /orders/{id}/shipment and extracts Shipment model."""
    route = respx.get("http://localhost:8000/api/v1/orders/ORD-1048/shipment").respond(
        json=SAMPLE_SHIPMENT_JSON
    )

    shipment = await http_store.get_shipment(customer_ctx, "ORD-1048")

    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer customer-access-token-xyz"
    assert isinstance(shipment, Shipment)
    assert shipment.shipment_id == "SHIP-1048"
    assert shipment.carrier == "BlueDart"
    assert len(shipment.events) == 1


@respx.mock
@pytest.mark.asyncio
async def test_http_store_get_refundable_success(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """get_refundable encodes items query param ITEM_ID:QTY,ITEM_ID:QTY per spec §16."""
    route = respx.get(
        "http://localhost:8000/api/v1/orders/ORD-1048/refundable",
        params={"items": "ITEM_01:1"},
    ).respond(json=SAMPLE_REFUNDABLE_JSON)

    refundable = await http_store.get_refundable(
        customer_ctx, "ORD-1048", items=[("ITEM_01", 1)]
    )

    assert route.called
    assert isinstance(refundable, Refundable)
    assert refundable.order_id == "ORD-1048"
    assert refundable.max_refundable_paise == 89900
    assert len(refundable.items) == 1
    assert refundable.items[0].remaining_qty == 1


@respx.mock
@pytest.mark.asyncio
async def test_http_store_list_returns_success(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """list_returns filters store returns matching the target order_id."""
    payload = {
        "returns": [
            SAMPLE_RETURN_JSON,
            {**SAMPLE_RETURN_JSON, "return_id": "RET-999", "order_id": "ORD-9999"},
        ],
        "next_cursor": None,
    }
    route = respx.get("http://localhost:8000/api/v1/returns").respond(json=payload)

    returns = await http_store.list_returns(customer_ctx, "ORD-1048")

    assert route.called
    assert len(returns) == 1
    assert isinstance(returns[0], ReturnRequest)
    assert returns[0].return_id == "RET-101"
    assert returns[0].order_id == "ORD-1048"


@respx.mock
@pytest.mark.asyncio
async def test_http_store_list_refunds_success(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """list_refunds queries /orders/{id}/refunds and parses Refund list."""
    payload = {"refunds": [SAMPLE_REFUND_JSON]}
    route = respx.get("http://localhost:8000/api/v1/orders/ORD-1048/refunds").respond(json=payload)

    refunds = await http_store.list_refunds(customer_ctx, "ORD-1048")

    assert route.called
    assert len(refunds) == 1
    assert isinstance(refunds[0], Refund)
    assert refunds[0].refund_id == "RFD-501"
    assert refunds[0].amount_paise == 89900


# ── Service Token & Risk Facts Tests ──────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_http_store_customer_risk_facts_forbidden_for_customer_ctx(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """get_customer_risk_facts rejects customer role with StoreForbidden."""
    with pytest.raises(StoreForbidden, match="requires service context"):
        await http_store.get_customer_risk_facts(customer_ctx, "C001")


@respx.mock
@pytest.mark.asyncio
async def test_http_store_get_customer_risk_facts_with_service_token(
    http_store: HttpStoreClient, service_ctx: RequestContext
) -> None:
    """get_customer_risk_facts issues service token and queries admin profile & refund summary."""
    token_route = respx.post("http://localhost:8000/api/v1/auth/token").respond(
        json=SAMPLE_TOKEN_JSON
    )
    admin_route = respx.get("http://localhost:8000/api/v1/admin/customers/C001").respond(
        json={
            "customer_id": "C001",
            "full_name": "Aarav Mehta",
            "email": "aarav.mehta@example.test",
            "joined_at": "2026-05-13T00:00:00Z",
            "returns_30d": 1,
            "refund_ratio_90d": 0.0792,
            "account_age_days": 180,
        }
    )
    summary_route = respx.get(
        "http://localhost:8000/api/v1/customers/C001/refund-summary",
        params={"days": "30"},
    ).respond(
        json={
            "customer_id": "C001",
            "days": 30,
            "total_refunded_paise": 40000,
            "refund_count": 1,
            "return_count": 1,
        }
    )

    facts = await http_store.get_customer_risk_facts(service_ctx, "C001")

    assert token_route.called
    assert admin_route.called
    assert summary_route.called

    assert admin_route.calls.last.request.headers["Authorization"] == (
        "Bearer service-monitor-jwt-token-abc"
    )

    assert isinstance(facts, CustomerRiskFacts)
    assert facts.customer_id == "C001"
    assert facts.returns_30d == 1
    assert facts.refund_ratio_90d_bp == 792  # 0.0792 * 10000 bp
    assert facts.account_age_days == 180
    assert facts.refunded_30d_paise == 40000


@respx.mock
@pytest.mark.asyncio
async def test_http_store_service_token_cached_and_refreshed_before_expiry(
    http_store: HttpStoreClient, service_ctx: RequestContext, clock: FixedClock
) -> None:
    """Service token is cached and refreshed only when within 60 s of expiry."""
    token_route = respx.post("http://localhost:8000/api/v1/auth/token").respond(
        json=SAMPLE_TOKEN_JSON
    )
    respx.get("http://localhost:8000/api/v1/admin/customers/C001").respond(
        json={
            "customer_id": "C001",
            "full_name": "Aarav Mehta",
            "email": "aarav@example.test",
            "joined_at": "2026-05-13T00:00:00Z",
            "returns_30d": 0,
            "refund_ratio_90d": 0.0,
            "account_age_days": 180,
        }
    )
    respx.get(
        "http://localhost:8000/api/v1/customers/C001/refund-summary",
        params={"days": "30"},
    ).respond(
        json={
            "customer_id": "C001",
            "days": 30,
            "total_refunded_paise": 0,
            "refund_count": 0,
            "return_count": 0,
        }
    )

    # First call: issues token
    await http_store.get_customer_risk_facts(service_ctx, "C001")
    assert token_route.call_count == 1

    # Second call at +300 s (well within 600 s lifetime): cached, no new token requested
    clock.set(clock.now() + timedelta(seconds=300))
    await http_store.get_customer_risk_facts(service_ctx, "C001")
    assert token_route.call_count == 1

    # Third call at +541 s (59 s before 600 s expiry): triggers refresh
    clock.set(clock.now() + timedelta(seconds=241))
    await http_store.get_customer_risk_facts(service_ctx, "C001")
    assert token_route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_http_store_get_clock_sync(
    http_store: HttpStoreClient, service_ctx: RequestContext
) -> None:
    """get_clock returns synchronized Clock from store GET /clock."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(json=SAMPLE_TOKEN_JSON)
    respx.get("http://localhost:8000/api/v1/clock").respond(
        json={
            "now_utc": "2026-11-09T12:00:00Z",
            "mode": "simulated",
            "offset_seconds": 7200,
        }
    )

    synced_dt = await http_store.sync_clock()
    assert synced_dt == datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)

    store_clock = http_store.get_clock()
    assert store_clock.now() == datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)


# ── Error Mapping Tests ───────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_http_store_uniform_404_handling(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """Unowned order and missing order both raise StoreNotFound indistinguishably (KP-15)."""
    uniform_404_body = {
        "error": {
            "code": "not_found",
            "message": "Order ORD-9999 was not found",
            "request_id": "req-123",
            "details": {},
        }
    }
    respx.get("http://localhost:8000/api/v1/orders/ORD-9999").respond(
        status_code=404, json=uniform_404_body
    )
    respx.get("http://localhost:8000/api/v1/orders/ORD-1048").respond(
        status_code=404, json=uniform_404_body
    )

    with pytest.raises(StoreNotFound):
        await http_store.get_order(customer_ctx, "ORD-9999")

    with pytest.raises(StoreNotFound):
        await http_store.get_order(customer_ctx, "ORD-1048")


@respx.mock
@pytest.mark.asyncio
async def test_http_store_error_mapping_401_403(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """401 and 403 status codes map to StoreForbidden."""
    respx.get("http://localhost:8000/api/v1/orders/ORD-1048").respond(status_code=401)
    with pytest.raises(StoreForbidden):
        await http_store.get_order(customer_ctx, "ORD-1048")

    respx.get("http://localhost:8000/api/v1/orders/ORD-1048").respond(status_code=403)
    with pytest.raises(StoreForbidden):
        await http_store.get_order(customer_ctx, "ORD-1048")


@respx.mock
@pytest.mark.asyncio
async def test_http_store_error_mapping_409(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """409 status code maps to StoreConflict."""
    respx.get("http://localhost:8000/api/v1/orders/ORD-1048").respond(status_code=409)
    with pytest.raises(StoreConflict):
        await http_store.get_order(customer_ctx, "ORD-1048")


@respx.mock
@pytest.mark.asyncio
async def test_http_store_error_mapping_5xx_and_network_error(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """500 server error and network connection failures map to StoreUnavailable."""
    respx.get("http://localhost:8000/api/v1/orders/ORD-1048").respond(status_code=500)
    with pytest.raises(StoreUnavailable):
        await http_store.get_order(customer_ctx, "ORD-1048")

    respx.get("http://localhost:8000/api/v1/orders/ORD-1048").mock(
        side_effect=httpx.ConnectError("Network is down")
    )
    with pytest.raises(StoreUnavailable):
        await http_store.get_order(customer_ctx, "ORD-1048")


@respx.mock
@pytest.mark.asyncio
async def test_http_store_error_mapping_timeout(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """ReadTimeout maps to StoreTimeout."""
    respx.get("http://localhost:8000/api/v1/orders/ORD-1048").mock(
        side_effect=httpx.ReadTimeout("Read timed out")
    )
    with pytest.raises(StoreTimeout):
        await http_store.get_order(customer_ctx, "ORD-1048")


@pytest.mark.asyncio
async def test_http_store_order_id_validation(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """Malformed order IDs raise StoreValidationError immediately without network call."""
    with pytest.raises(StoreValidationError):
        await http_store.get_order(customer_ctx, "INVALID_ID")

    with pytest.raises(StoreValidationError):
        await http_store.get_order(customer_ctx, "ORD-12")  # Too short

    with pytest.raises(StoreValidationError):
        await http_store.get_order(customer_ctx, "ord-1048")  # Lowercase


# ── Retries & Jitter on Idempotent GET ─────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_http_store_idempotent_get_retry_succeeds(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """Transient 503 is retried once with jitter and succeeds on 2nd attempt."""
    route = respx.get("http://localhost:8000/api/v1/orders/ORD-1048")
    route.side_effect = [
        httpx.Response(
            503, json={"error": {"code": "upstream_unavailable", "message": "retry me"}}
        ),
        httpx.Response(200, json=SAMPLE_ORDER_JSON),
    ]

    order = await http_store.get_order(customer_ctx, "ORD-1048")
    assert order.order_id == "ORD-1048"
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_http_store_idempotent_get_retry_bounded_at_two(
    http_store: HttpStoreClient, customer_ctx: RequestContext
) -> None:
    """Persistent 503 aborts after 2 tries and raises StoreUnavailable."""
    route = respx.get("http://localhost:8000/api/v1/orders/ORD-1048").respond(status_code=503)

    with pytest.raises(StoreUnavailable):
        await http_store.get_order(customer_ctx, "ORD-1048")

    assert route.call_count == 2


# ── Secret Logging & Payload Leakage Tests (KP-13) ────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_http_store_tokens_never_logged(
    http_store: HttpStoreClient,
    customer_ctx: RequestContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Customer token and service secret are strictly never emitted in logs (KP-13)."""
    respx.get("http://localhost:8000/api/v1/orders/ORD-1048").respond(json=SAMPLE_ORDER_JSON)

    with caplog.at_level(logging.DEBUG):
        await http_store.get_order(customer_ctx, "ORD-1048")

    all_logs = " ".join(record.message for record in caplog.records)
    assert "customer-access-token-xyz" not in all_logs
    assert "test-monitor-secret" not in all_logs


@respx.mock
@pytest.mark.asyncio
async def test_http_store_invalid_schema_logged_without_body(
    http_store: HttpStoreClient,
    customer_ctx: RequestContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid JSON response body raises StoreUnavailable and does not log response payload."""
    malformed_payload = '{"secret_customer_pii": "sensitive-data", "items": "corrupt"}'
    respx.get("http://localhost:8000/api/v1/orders/ORD-1048").respond(
        content=malformed_payload, headers={"Content-Type": "application/json"}
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(StoreUnavailable):
            await http_store.get_order(customer_ctx, "ORD-1048")

    all_logs = " ".join(record.message for record in caplog.records)
    assert "sensitive-data" not in all_logs
    assert "secret_customer_pii" not in all_logs


# ── STORE_MODE & readyz Health Tests (KP-24) ──────────────────────────


def test_store_mode_fixtures_refused_outside_dev_mode() -> None:
    """App startup must fail if DEV_MODE=False and STORE_MODE=fixtures (KP-24)."""
    with patch.dict(
        "os.environ",
        {
            "STORE_BASE_URL": "http://localhost:8000",
            "STORE_JWKS_URL": "http://localhost:8000/.well-known/jwks.json",
            "DEV_MODE": "false",
            "STORE_MODE": "fixtures",
        },
        clear=True,
    ):
        with pytest.raises((RuntimeError, ConfigError, ValueError)):
            create_app()


@respx.mock
def test_readyz_reports_store_mode_fixtures() -> None:
    """readyz returns active store_mode when STORE_MODE=fixtures."""
    respx.get("http://localhost:8000/.well-known/jwks.json").respond(json={"keys": []})
    with patch.dict(
        "os.environ",
        {
            "STORE_BASE_URL": "http://localhost:8000",
            "STORE_JWKS_URL": "http://localhost:8000/.well-known/jwks.json",
            "DEV_MODE": "true",
            "STORE_MODE": "fixtures",
        },
        clear=True,
    ):
        app = create_app()
        with TestClient(app) as test_client:
            resp = test_client.get("/readyz")
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("store_mode") == "fixtures"


@respx.mock
def test_readyz_reports_reachability_when_store_mode_http() -> None:
    """readyz checks store reachability when STORE_MODE=http and degrades if store is down."""
    respx.get("http://localhost:8000/.well-known/jwks.json").respond(json={"keys": []})
    respx.get("http://localhost:8000/api/v1/clock").respond(status_code=500)

    with patch.dict(
        "os.environ",
        {
            "STORE_BASE_URL": "http://localhost:8000",
            "STORE_JWKS_URL": "http://localhost:8000/.well-known/jwks.json",
            "DEV_MODE": "true",
            "STORE_MODE": "http",
        },
        clear=True,
    ):
        app = create_app()
        with TestClient(app) as test_client:
            resp = test_client.get("/readyz")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "degraded"
            assert data.get("store_mode") == "http"

