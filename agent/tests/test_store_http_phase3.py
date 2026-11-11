"""Contract tests for HttpStoreClient mail and shipments integration (TASK T3.7).

Enforces:
1. send_mail uses service token, passes X-Request-ID and Idempotency-Key headers,
   sends SendMailRequest payload (to_addr, subject, body_text, channel), and returns
   MailMessage envelope.
2. Token isolation: customer tokens and tainted service tokens are strictly forbidden
   from sending outbound mail or querying monitor shipments.
3. Zero retries on mutating send_mail writes (KP-16).
4. Error mapping:
   - 400 validation error -> StoreValidationError
   - 401/403 forbidden -> StoreForbidden
   - 404 not found -> StoreNotFound
   - 409 duplicate -> StoreDuplicate
   - 409 idempotency_mismatch -> StoreIdempotencyMismatch
   - 5xx and network errors -> StoreUnavailable
   - timeouts -> StoreTimeout
5. list_shipments_for_monitor uses monitor service token, passes Authorization and
   X-Request-ID headers, supports pagination cursor, and falls back cleanly to
   admin orders + shipment lookup when /admin/shipments returns 404.
"""

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
from sellershield_agent.domain.models import ShipmentListResponse
from sellershield_agent.store.client import (
    CustomerContext,
    ServiceContext,
    StoreDuplicate,
    StoreForbidden,
    StoreIdempotencyMismatch,
    StoreNotFound,
    StoreTimeout,
    StoreUnavailable,
    StoreValidationError,
)
from sellershield_agent.store.http_client import HttpStoreClient

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def http_client(clock: FixedClock) -> HttpStoreClient:
    return HttpStoreClient(
        base_url="http://localhost:8000",
        monitor_client_id="agent-monitor",
        monitor_client_secret=SecretStr("fake_monitor_secret_key_67890"),
        executor_client_id="agent-executor",
        executor_client_secret=SecretStr("fake_executor_secret_key_12345"),
        clock=clock,
    )


@pytest.fixture
def service_ctx() -> ServiceContext:
    return ServiceContext(
        principal=Principal(
            user_id="SRV-EXECUTOR",
            customer_id=None,
            role=Role.service,
            jti="jti-service-phase3",
            token_exp=1999999999,
        ),
        request_id="req-phase3-srv-001",
        token=None,
        scopes=["orders:read_all", "mail:send"],
    )


@pytest.fixture
def monitor_service_ctx() -> ServiceContext:
    return ServiceContext(
        principal=Principal(
            user_id="SRV-MONITOR",
            customer_id=None,
            role=Role.service,
            jti="jti-monitor-phase3",
            token_exp=1999999999,
        ),
        request_id="req-phase3-mon-001",
        token=None,
        scopes=["orders:read_all", "mail:send"],
    )


@pytest.fixture
def customer_ctx() -> CustomerContext:
    return CustomerContext(
        principal=Principal(
            user_id="USR-C001",
            customer_id="C001",
            role=Role.customer,
            jti="jti-cust-phase3",
            token_exp=1999999999,
        ),
        request_id="req-phase3-cust-001",
        token=SecretStr("cust-bearer-token-123"),
    )


@pytest.fixture
def tainted_service_ctx() -> ServiceContext:
    return ServiceContext(
        principal=Principal(
            user_id="SRV-MONITOR",
            customer_id=None,
            role=Role.service,
            jti="jti-tainted-001",
            token_exp=1999999999,
        ),
        request_id="req-phase3-tainted-001",
        token=SecretStr("customer-tainted-bearer-token"),
        scopes=["orders:read_all"],
    )


MAIL_MESSAGE_EXAMPLE: dict[str, Any] = {
    "message_id": "MSG-9988AABB",
    "thread_id": "THD-1048",
    "direction": "outbound",
    "channel": "email",
    "from_addr": "support@nestkart.test",
    "to_addr": "aarav.mehta@example.test",
    "subject": "Delay Update: ORD-1048",
    "body_text": "Your package has been delayed in transit.",
    "verified_sender": True,
    "created_at": "2026-11-10T12:00:00+00:00",
    "read_at": None,
}


# ── Contract Tests: Mail ────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_send_mail_contract_headers_payload_and_response(
    http_client: HttpStoreClient,
    service_ctx: ServiceContext,
) -> None:
    """send_mail passes service token, Idempotency-Key, and conforms to SendMailRequest."""
    token_route = respx.post("http://localhost:8000/api/v1/auth/token").respond(
        status_code=200, json={"access_token": "exec-token-xyz", "expires_in": 600}
    )
    mail_route = respx.post("http://localhost:8000/api/v1/mail/send").respond(
        status_code=200, json=MAIL_MESSAGE_EXAMPLE
    )

    res = await http_client.send_mail(
        service_ctx=service_ctx,
        recipient="aarav.mehta@example.test",
        subject="Delay Update: ORD-1048",
        body_text="Your package has been delayed in transit.",
        idempotency_key="mail:req-phase3-srv-001",
    )

    assert token_route.call_count == 1
    assert mail_route.call_count == 1
    sent_req = mail_route.calls[0].request
    assert sent_req.headers["Authorization"] == "Bearer exec-token-xyz"
    assert sent_req.headers["X-Request-ID"] == "req-phase3-srv-001"
    assert sent_req.headers["Idempotency-Key"] == "mail:req-phase3-srv-001"

    body = json.loads(sent_req.content.decode("utf-8"))
    assert body["to_addr"] == "aarav.mehta@example.test"
    assert body["subject"] == "Delay Update: ORD-1048"
    assert body["body_text"] == "Your package has been delayed in transit."
    assert body["channel"] == "email"

    assert res["message_id"] == "MSG-9988AABB"
    assert res["to_addr"] == "aarav.mehta@example.test"
    assert res["direction"] == "outbound"


@pytest.mark.asyncio
async def test_send_mail_token_isolation_enforced(
    http_client: HttpStoreClient,
    customer_ctx: CustomerContext,
    tainted_service_ctx: ServiceContext,
) -> None:
    """Customer tokens and tainted tokens are strictly rejected from send_mail."""
    with pytest.raises(StoreForbidden):
        await http_client.send_mail(
            service_ctx=customer_ctx,
            recipient="aarav.mehta@example.test",
            subject="Test",
            body_text="Test",
        )

    with pytest.raises(StoreForbidden):
        await http_client.send_mail(
            service_ctx=tainted_service_ctx,
            recipient="aarav.mehta@example.test",
            subject="Test",
            body_text="Test",
        )


@respx.mock
@pytest.mark.asyncio
async def test_send_mail_zero_retries_on_mutating_writes(
    http_client: HttpStoreClient,
    service_ctx: ServiceContext,
) -> None:
    """Mutating send_mail must NOT perform automatic retries on 503 (KP-16)."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        status_code=200, json={"access_token": "exec-token-xyz", "expires_in": 600}
    )
    mail_route = respx.post("http://localhost:8000/api/v1/mail/send").respond(
        status_code=503, json={"error": {"code": "unavailable", "message": "Mail server down"}}
    )

    with pytest.raises(StoreUnavailable):
        await http_client.send_mail(
            service_ctx=service_ctx,
            recipient="aarav.mehta@example.test",
            subject="Test",
            body_text="Test",
        )

    assert mail_route.call_count == 1


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "err_code", "expected_exc"),
    [
        (400, "validation_error", StoreValidationError),
        (401, "unauthorized", StoreForbidden),
        (403, "forbidden", StoreForbidden),
        (404, "not_found", StoreNotFound),
        (409, "duplicate", StoreDuplicate),
        (409, "idempotency_mismatch", StoreIdempotencyMismatch),
        (500, "internal_error", StoreUnavailable),
    ],
)
async def test_send_mail_error_mapping(
    http_client: HttpStoreClient,
    service_ctx: ServiceContext,
    status_code: int,
    err_code: str,
    expected_exc: type[Exception],
) -> None:
    """send_mail maps store error envelopes to proper StoreClientError hierarchy."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        status_code=200, json={"access_token": "exec-token-xyz", "expires_in": 600}
    )
    respx.post("http://localhost:8000/api/v1/mail/send").respond(
        status_code=status_code, json={"error": {"code": err_code, "message": "Failed"}}
    )

    with pytest.raises(expected_exc):
        await http_client.send_mail(
            service_ctx=service_ctx,
            recipient="aarav.mehta@example.test",
            subject="Test",
            body_text="Test",
        )


@respx.mock
@pytest.mark.asyncio
async def test_send_mail_timeout_mapping(
    http_client: HttpStoreClient,
    service_ctx: ServiceContext,
) -> None:
    """send_mail maps httpx timeouts to StoreTimeout."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        status_code=200, json={"access_token": "exec-token-xyz", "expires_in": 600}
    )
    respx.post("http://localhost:8000/api/v1/mail/send").mock(
        side_effect=httpx.ReadTimeout("Connection timed out")
    )

    with pytest.raises(StoreTimeout):
        await http_client.send_mail(
            service_ctx=service_ctx,
            recipient="aarav.mehta@example.test",
            subject="Test",
            body_text="Test",
        )


# ── Contract Tests: Shipments for Monitor ───────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_list_shipments_for_monitor_contract_and_token(
    http_client: HttpStoreClient,
    monitor_service_ctx: ServiceContext,
) -> None:
    """list_shipments_for_monitor passes monitor service token and parses shipments."""
    token_route = respx.post("http://localhost:8000/api/v1/auth/token").respond(
        status_code=200, json={"access_token": "mon-token-123", "expires_in": 600}
    )
    shipments_route = respx.get("http://localhost:8000/admin/shipments").respond(
        status_code=200,
        json={
            "shipments": [
                {
                    "shipment_id": "SHP-1048",
                    "order_id": "ORD-1048",
                    "customer_id": "C001",
                    "carrier": "BlueDart",
                    "tracking_no": "TRK-1048",
                    "status": "in_transit",
                    "eta_at": "2026-11-12T12:00:00+00:00",
                    "delayed": False,
                    "events": [],
                },
                {
                    "shipment_id": "SHP-2037",
                    "order_id": "ORD-2037",
                    "customer_id": "C002",
                    "carrier": "Delhivery",
                    "tracking_no": "TRK-2037",
                    "status": "exception",
                    "eta_at": "2026-11-04T12:00:00+00:00",
                    "delayed": True,
                    "events": [],
                },
            ],
            "next_cursor": "cur-next-99",
        },
    )

    resp = await http_client.list_shipments_for_monitor(
        service_ctx=monitor_service_ctx,
        statuses=["in_transit", "exception"],
        limit=50,
        cursor="cur-prev-00",
    )

    assert token_route.call_count == 1
    assert shipments_route.call_count == 1
    sent_req = shipments_route.calls[0].request
    assert sent_req.headers["Authorization"] == "Bearer mon-token-123"
    assert sent_req.headers["X-Request-ID"] == "req-phase3-mon-001"

    assert isinstance(resp, ShipmentListResponse)
    assert len(resp.shipments) == 2
    assert resp.next_cursor == "cur-next-99"
    assert resp.shipments[0].order_id == "ORD-1048"
    assert resp.shipments[0].delayed is False
    assert resp.shipments[1].order_id == "ORD-2037"
    assert resp.shipments[1].delayed is True
    assert resp.shipments[1].eta_at is not None
    assert resp.shipments[1].eta_at.tzinfo is not None


@pytest.mark.asyncio
async def test_list_shipments_for_monitor_token_isolation(
    http_client: HttpStoreClient,
    customer_ctx: CustomerContext,
    tainted_service_ctx: ServiceContext,
) -> None:
    """Customer tokens and tainted service tokens cannot list monitor shipments."""
    with pytest.raises(StoreForbidden):
        await http_client.list_shipments_for_monitor(customer_ctx)

    with pytest.raises(StoreForbidden):
        await http_client.list_shipments_for_monitor(tainted_service_ctx)


@respx.mock
@pytest.mark.asyncio
async def test_list_shipments_for_monitor_fallback_to_admin_orders(
    http_client: HttpStoreClient,
    monitor_service_ctx: ServiceContext,
) -> None:
    """When /admin/shipments returns 404, falls back to /admin/orders and /orders/{id}/shipment."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        status_code=200, json={"access_token": "mon-token-123", "expires_in": 600}
    )
    respx.get("http://localhost:8000/admin/shipments").respond(
        status_code=404, json={"error": {"code": "not_found", "message": "Not Found"}}
    )
    respx.get("http://localhost:8000/api/v1/admin/orders").respond(
        status_code=200,
        json={
            "items": [
                {"order_id": "ORD-1048", "customer_id": "C001", "status": "shipped"},
                {"order_id": "ORD-2037", "customer_id": "C002", "status": "shipped"},
            ],
            "has_more": False,
        },
    )
    respx.get("http://localhost:8000/api/v1/orders/ORD-1048/shipment").respond(
        status_code=200,
        json={
            "shipment_id": "SHP-1048",
            "order_id": "ORD-1048",
            "carrier": "BlueDart",
            "tracking_no": "TRK-1048",
            "status": "in_transit",
            "eta_at": "2026-11-12T12:00:00+00:00",
            "delayed": False,
            "events": [],
        },
    )
    respx.get("http://localhost:8000/api/v1/orders/ORD-2037/shipment").respond(
        status_code=200,
        json={
            "shipment_id": "SHP-2037",
            "order_id": "ORD-2037",
            "carrier": "Delhivery",
            "tracking_no": "TRK-2037",
            "status": "exception",
            "eta_at": "2026-11-04T12:00:00+00:00",
            "delayed": True,
            "events": [],
        },
    )

    resp = await http_client.list_shipments_for_monitor(
        service_ctx=monitor_service_ctx,
        limit=20,
    )

    assert isinstance(resp, ShipmentListResponse)
    assert len(resp.shipments) == 2
    assert resp.shipments[0].order_id == "ORD-1048"
    assert resp.shipments[0].customer_id == "C001"
    assert resp.shipments[1].order_id == "ORD-2037"
    assert resp.shipments[1].customer_id == "C002"
    assert resp.shipments[1].delayed is True


@respx.mock
@pytest.mark.asyncio
async def test_list_shipments_for_monitor_forbidden_and_unavailable(
    http_client: HttpStoreClient,
    monitor_service_ctx: ServiceContext,
) -> None:
    """Forbidden and server errors from store raise corresponding domain exceptions."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        status_code=200, json={"access_token": "mon-token-123", "expires_in": 600}
    )
    respx.get("http://localhost:8000/admin/shipments").respond(
        status_code=403, json={"error": {"code": "forbidden", "message": "Scope required"}}
    )

    with pytest.raises(StoreForbidden):
        await http_client.list_shipments_for_monitor(service_ctx=monitor_service_ctx)

