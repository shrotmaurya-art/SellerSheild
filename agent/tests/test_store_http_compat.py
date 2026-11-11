"""Integration compatibility tests for HttpStoreClient against store-api payloads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import Settings
from sellershield_agent.domain.models import (
    AdminOrderDetail,
    CustomerRefundSummary,
    CustomerRiskFacts,
    RefundStatus,
    ReturnStatus,
    Shipment,
    ShipmentStatus,
    StoreSettings,
)
from sellershield_agent.store import get_store_client
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.http_client import HttpStoreClient


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def service_ctx() -> RequestContext:
    return RequestContext(
        principal=Principal(
            user_id="SRV-AGENT",
            customer_id=None,
            role=Role.service,
            jti="jti-srv-1",
            token_exp=1999999999,
        ),
        request_id="req-compat-srv",
        token=None,
    )


@pytest.fixture
def customer_ctx() -> RequestContext:
    return RequestContext(
        principal=Principal(
            user_id="USR-C001",
            customer_id="C001",
            role=Role.customer,
            jti="jti-cust-1",
            token_exp=1999999999,
        ),
        request_id="req-compat-cust",
        token=SecretStr("cust-jwt-secret"),
    )


@pytest.fixture
def seller_ctx() -> RequestContext:
    return RequestContext(
        principal=Principal(
            user_id="USR-SELLER",
            customer_id=None,
            role=Role.seller_owner,
            jti="jti-seller-1",
            token_exp=1999999999,
            store_id="nestkart-default",
            auth_time=1999999900,
        ),
        request_id="req-compat-seller",
        token=SecretStr("seller-jwt-secret"),
    )


@pytest.fixture
def dual_client(clock: FixedClock) -> HttpStoreClient:
    return HttpStoreClient(
        base_url="http://localhost:8000",
        monitor_client_id="agent-monitor",
        monitor_client_secret=SecretStr("mon-secret"),
        executor_client_id="agent-executor",
        executor_client_secret=SecretStr("exec-secret"),
        clock=clock,
    )


REAL_STORE_ORDER: dict[str, Any] = {
    "order_id": "ORD-1042",
    "customer_id": "C001",
    "status": "delivered",
    "payment_method": "prepaid",
    "payment_status": "paid",
    "subtotal_paise": 120000,
    "discount_paise": 0,
    "shipping_fee_paise": 0,
    "total_paise": 120000,
    "placed_at": "2026-11-05T08:30:00",
    "shipped_at": "2026-11-06T08:30:00",
    "delivered_at": "2026-11-07T08:30:00",
    "cancelled_at": None,
    "ship_to_json": '{"name": "Aarav Mehta", "city": "Bengaluru", "postal_code": "560001"}',
    "items": [
        {
            "item_id": "ITEM-1042-1",
            "order_id": "ORD-1042",
            "sku": "P-101",
            "name_snapshot": "Non-stick Cookware Set",
            "category_snapshot": "home_kitchen",
            "qty": 1,
            "unit_price_paise": 120000,
            "discount_alloc_paise": 0,
            "line_paid_paise": 120000,
            "flags_json": '{"hygiene_sensitive": false}',
        }
    ],
    "version": 1,
    "returns": [
        {
            "return_id": "RET-1042",
            "order_id": "ORD-1042",
            "customer_id": "C001",
            "status": "requested",
            "reason_code": "defective",
            "reason_text": "Damaged lid",
            "resolution": "refund",
            "requested_via": "chat",
            "approved_by": None,
            "client_ref": "prp-1042:create_return",
            "created_at": "2026-11-09T10:00:00",
            "updated_at": "2026-11-09T10:00:00",
            "version": 1,
            "items": [
                {
                    "id": 7,
                    "return_id": "RET-1042",
                    "item_id": "ITEM-1042-1",
                    "qty": 1,
                    "condition_note": "Damaged lid",
                    "status": "requested",
                }
            ],
        }
    ],
    "refunds": [
        {
            "refund_id": "RFD-1042",
            "order_id": "ORD-1042",
            "return_id": "RET-1042",
            "customer_id": "C001",
            "amount_paise": 120000,
            "method": "original",
            "status": "reserved",
            "gateway_ref": None,
            "simulated": True,
            "authorized_by": "USR-SELLER",
            "authorized_amount_paise": 120000,
            "authorized_at": "2026-11-09T11:00:00",
            "failure_reason": None,
            "client_ref": "prp-1042:reserve_refund",
            "version": 2,
            "created_at": "2026-11-09T10:05:00",
            "updated_at": "2026-11-09T11:00:00",
        }
    ],
}


@respx.mock
@pytest.mark.asyncio
async def test_get_store_client_wires_executor_credentials_and_role_routing(
    clock: FixedClock, service_ctx: RequestContext, seller_ctx: RequestContext
) -> None:
    """get_store_client passes executor creds; writes use executor and seller tokens forward."""
    settings = Settings.load_from_env(
        env_dict={
            "STORE_BASE_URL": "http://localhost:8000",
            "STORE_JWKS_URL": "http://localhost:8000/.well-known/jwks.json",
            "DEV_MODE": "true",
            "STORE_MODE": "http",
            "STORE_MONITOR_CLIENT_ID": "agent-monitor",
            "STORE_MONITOR_CLIENT_SECRET": "mon-secret",
            "STORE_EXECUTOR_CLIENT_ID": "agent-executor",
            "STORE_EXECUTOR_CLIENT_SECRET": "exec-secret",
        }
    )
    client = get_store_client(clock=clock, settings=settings)
    assert isinstance(client, HttpStoreClient)

    def _token_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        cid = body["client_id"]
        if cid == "agent-executor":
            return httpx.Response(200, json={"access_token": "tok-executor", "expires_in": 600})
        return httpx.Response(200, json={"access_token": "tok-monitor", "expires_in": 600})

    respx.post("http://localhost:8000/api/v1/auth/token").mock(side_effect=_token_handler)
    settings_route = respx.get("http://localhost:8000/api/v1/admin/settings").respond(
        json={"autonomy_cap_paise": 50000, "autonomy_daily_cap_paise": 500000, "version": 1}
    )
    trans_route = respx.post("http://localhost:8000/api/v1/returns/RET-1042/transition").respond(
        json=REAL_STORE_ORDER["returns"][0]
    )
    res_route = respx.post(
        "http://localhost:8000/api/v1/orders/ORD-1042/refund-reservations"
    ).respond(status_code=201, json=REAL_STORE_ORDER["refunds"][0])
    rel_route = respx.post("http://localhost:8000/api/v1/refunds/RFD-1042/release").respond(
        json={**REAL_STORE_ORDER["refunds"][0], "status": "processing"}
    )
    auth_route = respx.post("http://localhost:8000/api/v1/refunds/RFD-1042/authorize").respond(
        json=REAL_STORE_ORDER["refunds"][0]
    )

    await client.get_settings(service_ctx)
    assert settings_route.calls.last.request.headers["Authorization"] == "Bearer tok-monitor"

    await client.transition_return(service_ctx, "RET-1042", "cancelled", "comp", "idem-1")
    assert trans_route.calls.last.request.headers["Authorization"] == "Bearer tok-executor"

    await client.reserve_refund(service_ctx, "ORD-1042", 50000, "RET-1042", "original", "idem-2")
    assert res_route.calls.last.request.headers["Authorization"] == "Bearer tok-executor"

    await client.release_refund(service_ctx, "RFD-1042", "idem-3")
    assert rel_route.calls.last.request.headers["Authorization"] == "Bearer tok-executor"

    # Seller token must forward seller JWT directly rather than replacing with executor token
    await client.authorize_refund(seller_ctx, "RFD-1042", 120000, "ok", "idem-4")
    assert auth_route.calls.last.request.headers["Authorization"] == "Bearer seller-jwt-secret"


@respx.mock
@pytest.mark.asyncio
async def test_http_store_list_orders_and_admin_detail_normalize_store_api_shapes(
    dual_client: HttpStoreClient,
    customer_ctx: RequestContext,
    service_ctx: RequestContext,
) -> None:
    """list_orders handles 'items'; get_order_admin parses snapshots, tz, and authorized holds."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        json={"access_token": "tok-monitor", "expires_in": 600}
    )
    respx.get("http://localhost:8000/api/v1/orders").respond(
        json={"items": [REAL_STORE_ORDER], "next_cursor": None, "has_more": False}
    )
    respx.get("http://localhost:8000/api/v1/admin/orders/ORD-1042").respond(json=REAL_STORE_ORDER)

    orders = await dual_client.list_orders(customer_ctx)
    assert len(orders) == 1
    assert orders[0].items[0].name == "Non-stick Cookware Set"
    assert orders[0].items[0].flags == {"hygiene_sensitive": False}
    assert orders[0].ship_to is not None and orders[0].ship_to.full_name == "Aarav Mehta"
    assert orders[0].placed_at.tzinfo is not None

    detail = await dual_client.get_order_admin(service_ctx, "ORD-1042")
    assert isinstance(detail, AdminOrderDetail)
    assert len(detail.returns) == 1
    assert detail.returns[0].status == ReturnStatus.requested
    assert detail.returns[0].client_ref == "prp-1042:create_return"
    assert len(detail.refunds) == 1
    assert detail.refunds[0].status == RefundStatus.authorized
    assert detail.refunds[0].client_ref == "prp-1042:reserve_refund"


@respx.mock
@pytest.mark.asyncio
async def test_http_store_normalizes_shipment_refundable_settings_and_risk_facts(
    dual_client: HttpStoreClient, service_ctx: RequestContext
) -> None:
    """Shipment, Refundable, StoreSettings, RefundSummary, and RiskFacts handle store-api JSON."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        json={"access_token": "tok-monitor", "expires_in": 600}
    )
    respx.get("http://localhost:8000/api/v1/orders/ORD-1042/shipment").respond(
        json={
            "shipment_id": "SHP-1042",
            "order_id": "ORD-1042",
            "carrier": "BlueDart",
            "tracking_no": "BD-1042",
            "status": "delivered",
            "eta_at": "2026-11-08T18:00:00",
            "last_event_at": "2026-11-07T08:30:00",
            "script_id": "script-delivered",
            "delayed": False,
            "events": [
                {
                    "event_id": "EVT-1",
                    "shipment_id": "SHP-1042",
                    "ts": "2026-11-06T09:00:00",
                    "status": "shipped",
                    "location": "Hub",
                    "note": "Dispatched",
                }
            ],
        }
    )
    respx.get("http://localhost:8000/api/v1/orders/ORD-1042/refundable").respond(
        json={
            "order_id": "ORD-1042",
            "max_refundable_paise": 120000,
            "items": [
                {
                    "item_id": "ITEM-1042-1",
                    "sku": "P-101",
                    "name": "Cookware",
                    "requested_qty": 1,
                    "remaining_qty": 1,
                    "unit_price_paise": 120000,
                    "line_paid_paise": 120000,
                    "max_refundable_paise": 120000,
                }
            ],
        }
    )
    respx.get("http://localhost:8000/api/v1/admin/settings").respond(
        json={
            "autonomy_cap_paise": 50000,
            "autonomy_daily_cap_paise": 500000,
            "updated_by": "USR-PRIYA",
            "updated_at": "2026-11-01T00:00:00",
            "version": 3,
        }
    )
    respx.get("http://localhost:8000/api/v1/admin/customers/C001").respond(
        json={
            "customer_id": "C001",
            "full_name": "Aarav Mehta",
            "joined_at": "2026-05-01T00:00:00",
            "account_age": "180 days",
            "account_age_days": 180,
            "returns_30d": 1,
            "refund_ratio_90d": 0.125,
            "total_orders": 5,
            "total_spend_paise": 300000,
            "total_refunded_paise": 37500,
        }
    )
    respx.get(
        "http://localhost:8000/api/v1/customers/C001/refund-summary",
        params={"days": "30"},
    ).respond(
        json={
            "customer_id": "C001",
            "days": 30,
            "total_refunds_paise": 25000,
            "refund_count": 2,
            "active_holds_paise": 10000,
            "active_hold_count": 1,
        }
    )

    shipment = await dual_client.get_shipment(service_ctx, "ORD-1042")
    assert isinstance(shipment, Shipment)
    assert shipment.events[0].status == ShipmentStatus.in_transit
    assert shipment.events[0].occurred_at.tzinfo is not None

    refundable = await dual_client.get_refundable(service_ctx, "ORD-1042")
    assert refundable.items[0].original_qty == 1
    assert refundable.items[0].refundable_paise == 120000

    st = await dual_client.get_settings(service_ctx)
    assert isinstance(st, StoreSettings)
    assert st.version == 3

    summary = await dual_client.get_customer_refund_summary(service_ctx, "C001", days=30)
    assert isinstance(summary, CustomerRefundSummary)
    assert summary.total_refunded_paise == 25000
    assert summary.return_count == 0

    facts = await dual_client.get_customer_risk_facts(service_ctx, "C001")
    assert isinstance(facts, CustomerRiskFacts)
    assert facts.refunded_30d_paise == 25000
    assert facts.refund_ratio_90d_bp == 1250

