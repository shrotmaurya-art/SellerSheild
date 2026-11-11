"""Contract tests for HttpStoreClient write methods and token isolation (TASK T2.9).

Enforces:
1. Real store write methods: create_return, transition_return, reserve_refund,
   authorize_refund, release_refund, cancel_reservation, update_settings.
2. Authorization per context: customer token on customer steps, seller token on
   seller steps, and service token on service steps.
3. Token isolation: proof that customer token is never sent on service steps and
   service token is never sent on seller steps.
4. Service token caching and 60-second pre-expiry refresh.
5. Idempotency-Key and X-Request-ID headers on every write; If-Match on update_settings.
6. Zero automatic retries on writes (saga and reconciler own retries).
7. Error mapping:
   - 409 duplicate -> StoreDuplicate
   - 409 limit_exceeded -> StoreLimitExceeded
   - 409 invalid_state -> StoreInvalidState
   - 409 idempotency_mismatch -> StoreIdempotencyMismatch
   - 404 -> StoreNotFound
   - 401/403 -> StoreForbidden
   - 412 -> StoreInvalidState
   - 5xx and network errors -> StoreUnavailable
   - timeouts -> StoreTimeout
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.domain.models import (
    Refund,
    RefundStatus,
    ReturnItem,
    ReturnRequest,
    ReturnStatus,
    StoreSettings,
)
from sellershield_agent.store.client import (
    CustomerContext,
    SellerContext,
    ServiceContext,
    StoreDuplicate,
    StoreForbidden,
    StoreIdempotencyMismatch,
    StoreInvalidState,
    StoreLimitExceeded,
    StoreNotFound,
    StoreTimeout,
    StoreUnavailable,
)
from sellershield_agent.store.http_client import HttpStoreClient

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def client(clock: FixedClock) -> HttpStoreClient:
    return HttpStoreClient(
        base_url="http://localhost:8000",
        monitor_client_id="agent-monitor",
        monitor_client_secret=SecretStr("mon-secret"),
        executor_client_id="agent-executor",
        executor_client_secret=SecretStr("exec-secret"),
        clock=clock,
    )


@pytest.fixture
def customer_ctx() -> CustomerContext:
    return CustomerContext(
        principal=Principal(
            user_id="USR-C001",
            customer_id="C001",
            role=Role.customer,
            jti="jti-cust-001",
            token_exp=1999999999,
        ),
        request_id="req-write-cust-001",
        token=SecretStr("cust-bearer-token-123"),
    )


@pytest.fixture
def seller_ctx() -> SellerContext:
    return SellerContext(
        principal=Principal(
            user_id="USR-SELLER-01",
            customer_id=None,
            role=Role.seller_owner,
            jti="jti-seller-001",
            token_exp=1999999999,
            store_id="nestkart-default",
            auth_time=1999999900,
        ),
        request_id="req-write-seller-001",
        token=SecretStr("seller-bearer-token-456"),
    )


@pytest.fixture
def service_ctx() -> ServiceContext:
    return ServiceContext(
        principal=Principal(
            user_id="SRV-EXECUTOR",
            customer_id=None,
            role=Role.service,
            jti="jti-service-001",
            token_exp=1999999999,
        ),
        request_id="req-write-srv-001",
        token=None,
        scopes=["returns:transition", "refunds:reserve", "refunds:release"],
    )


RETURN_EXAMPLE: dict[str, Any] = {
    "return_id": "RET-1042-01",
    "order_id": "ORD-1042",
    "customer_id": "C001",
    "status": "requested",
    "reason_code": "defective",
    "reason_text": "Non-stick coating peeling",
    "resolution": "refund",
    "items": [{"item_id": "ITEM-1042-1", "qty": 1, "condition_note": "Defective coating"}],
    "created_at": "2026-11-10T12:00:00+00:00",
    "updated_at": "2026-11-10T12:00:00+00:00",
    "version": 1,
    "client_ref": "idem-ret-001",
}

REFUND_EXAMPLE: dict[str, Any] = {
    "refund_id": "RFD-1042-01",
    "order_id": "ORD-1042",
    "return_id": "RET-1042-01",
    "customer_id": "C001",
    "amount_paise": 120000,
    "method": "original",
    "status": "reserved",
    "created_at": "2026-11-10T12:00:00+00:00",
    "client_ref": "idem-rfd-001",
}

SETTINGS_EXAMPLE: dict[str, Any] = {
    "autonomy_cap_paise": 50000,
    "autonomy_daily_cap_paise": 500000,
    "version": 1,
}


# ── Contract Tests: Write Endpoints & Headers ───────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_create_return_contract_headers_and_payload(
    client: HttpStoreClient, customer_ctx: CustomerContext
) -> None:
    """create_return passes customer bearer token, Idempotency-Key, and X-Request-ID."""
    route = respx.post("http://localhost:8000/api/v1/orders/ORD-1042/returns").respond(
        status_code=201, json=RETURN_EXAMPLE
    )

    items = [ReturnItem(item_id="ITEM-1042-1", qty=1, condition_note="Defective coating")]
    ret = await client.create_return(
        ctx=customer_ctx,
        order_id="ORD-1042",
        items=items,
        reason_code="defective",
        reason_text="Non-stick coating peeling",
        resolution="refund",
        idempotency_key="idem-ret-001",
    )

    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer cust-bearer-token-123"
    assert req.headers["Idempotency-Key"] == "idem-ret-001"
    assert req.headers["X-Request-ID"] == "req-write-cust-001"

    body = json.loads(req.content.decode("utf-8"))
    assert body["reason_code"] == "defective"
    assert body["reason_text"] == "Non-stick coating peeling"
    assert body["resolution"] == "refund"
    assert body["items"] == [
        {"item_id": "ITEM-1042-1", "qty": 1, "condition_note": "Defective coating"}
    ]

    assert isinstance(ret, ReturnRequest)
    assert ret.return_id == "RET-1042-01"
    assert ret.status == ReturnStatus.requested
    assert ret.created_at.tzinfo is not None


@respx.mock
@pytest.mark.asyncio
async def test_transition_return_contract_executor_and_seller(
    client: HttpStoreClient, service_ctx: ServiceContext, seller_ctx: SellerContext
) -> None:
    """transition_return uses executor service token for service, seller token for seller."""
    token_route = respx.post("http://localhost:8000/api/v1/auth/token").respond(
        json={"access_token": "tok-exec-jwt", "expires_in": 600}
    )
    trans_route = respx.post(
        "http://localhost:8000/api/v1/returns/RET-1042-01/transition"
    ).respond(json={**RETURN_EXAMPLE, "status": "approved", "version": 2})

    # 1. Service caller (executor) -> must mint and use executor service token
    ret1 = await client.transition_return(
        ctx=service_ctx,
        return_id="RET-1042-01",
        to="approved",
        reason="policy_auto_approved",
        idempotency_key="idem-trans-001",
    )
    assert token_route.called
    assert trans_route.calls[0].request.headers["Authorization"] == "Bearer tok-exec-jwt"
    assert trans_route.calls[0].request.headers["Idempotency-Key"] == "idem-trans-001"
    assert trans_route.calls[0].request.headers["X-Request-ID"] == "req-write-srv-001"
    assert ret1.status == ReturnStatus.approved

    # 2. Seller caller -> must forward seller token directly
    ret2 = await client.transition_return(
        ctx=seller_ctx,
        return_id="RET-1042-01",
        to="approved",
        reason="seller_manual_approval",
        idempotency_key="idem-trans-002",
    )
    assert trans_route.calls[1].request.headers["Authorization"] == (
        "Bearer seller-bearer-token-456"
    )
    assert trans_route.calls[1].request.headers["Idempotency-Key"] == "idem-trans-002"
    assert trans_route.calls[1].request.headers["X-Request-ID"] == "req-write-seller-001"
    assert ret2.status == ReturnStatus.approved


@respx.mock
@pytest.mark.asyncio
async def test_reserve_refund_contract(
    client: HttpStoreClient, service_ctx: ServiceContext
) -> None:
    """reserve_refund uses executor service token, Idempotency-Key, and post body."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        json={"access_token": "tok-exec-jwt", "expires_in": 600}
    )
    reserve_route = respx.post(
        "http://localhost:8000/api/v1/orders/ORD-1042/refund-reservations"
    ).respond(status_code=201, json=REFUND_EXAMPLE)

    refund = await client.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1042",
        amount_paise=120000,
        return_id="RET-1042-01",
        method="original",
        idempotency_key="idem-rfd-001",
    )

    assert reserve_route.called
    req = reserve_route.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok-exec-jwt"
    assert req.headers["Idempotency-Key"] == "idem-rfd-001"
    assert req.headers["X-Request-ID"] == "req-write-srv-001"

    body = json.loads(req.content.decode("utf-8"))
    assert body == {"amount_paise": 120000, "return_id": "RET-1042-01", "method": "original"}

    assert isinstance(refund, Refund)
    assert refund.refund_id == "RFD-1042-01"
    assert refund.status == RefundStatus.reserved


@respx.mock
@pytest.mark.asyncio
async def test_authorize_refund_contract(
    client: HttpStoreClient, seller_ctx: SellerContext
) -> None:
    """authorize_refund forwards seller token and sends amount_paise + note."""
    auth_route = respx.post(
        "http://localhost:8000/api/v1/refunds/RFD-1042-01/authorize"
    ).respond(
        json={**REFUND_EXAMPLE, "status": "authorized", "authorized_by": "USR-SELLER-01"}
    )

    refund = await client.authorize_refund(
        seller_ctx=seller_ctx,
        refund_id="RFD-1042-01",
        amount_paise=120000,
        note="Approved under customer loyalty policy",
        idempotency_key="idem-auth-001",
    )

    assert auth_route.called
    req = auth_route.calls.last.request
    assert req.headers["Authorization"] == "Bearer seller-bearer-token-456"
    assert req.headers["Idempotency-Key"] == "idem-auth-001"
    assert req.headers["X-Request-ID"] == "req-write-seller-001"

    body = json.loads(req.content.decode("utf-8"))
    assert body == {"amount_paise": 120000, "note": "Approved under customer loyalty policy"}
    assert refund.status == RefundStatus.authorized


@respx.mock
@pytest.mark.asyncio
async def test_release_refund_contract(
    client: HttpStoreClient, service_ctx: ServiceContext
) -> None:
    """release_refund sends executor service token and Idempotency-Key."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        json={"access_token": "tok-exec-jwt", "expires_in": 600}
    )
    rel_route = respx.post(
        "http://localhost:8000/api/v1/refunds/RFD-1042-01/release"
    ).respond(json={**REFUND_EXAMPLE, "status": "processing"})

    refund = await client.release_refund(
        ctx=service_ctx,
        refund_id="RFD-1042-01",
        idempotency_key="idem-rel-001",
    )

    assert rel_route.called
    req = rel_route.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok-exec-jwt"
    assert req.headers["Idempotency-Key"] == "idem-rel-001"
    assert req.headers["X-Request-ID"] == "req-write-srv-001"
    assert refund.status == RefundStatus.processing


@respx.mock
@pytest.mark.asyncio
async def test_cancel_reservation_contract(
    client: HttpStoreClient, service_ctx: ServiceContext
) -> None:
    """cancel_reservation releases refund hold with executor token."""
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        json={"access_token": "tok-exec-jwt", "expires_in": 600}
    )
    cancel_route = respx.post(
        "http://localhost:8000/api/v1/refunds/RFD-1042-01/cancel-reservation"
    ).respond(json={**REFUND_EXAMPLE, "status": "released"})

    refund = await client.cancel_reservation(
        ctx=service_ctx,
        refund_id="RFD-1042-01",
        idempotency_key="idem-can-001",
    )

    assert cancel_route.called
    req = cancel_route.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok-exec-jwt"
    assert req.headers["Idempotency-Key"] == "idem-can-001"
    assert req.headers["X-Request-ID"] == "req-write-srv-001"
    assert refund.status == RefundStatus.released


@respx.mock
@pytest.mark.asyncio
async def test_update_settings_contract_with_if_match(
    client: HttpStoreClient, seller_ctx: SellerContext
) -> None:
    """update_settings requires seller token, If-Match version header, and returns StoreSettings."""
    settings_route = respx.put("http://localhost:8000/api/v1/admin/settings").respond(
        json={"autonomy_cap_paise": 60000, "autonomy_daily_cap_paise": 600000, "version": 2}
    )

    updated = await client.update_settings(
        seller_ctx=seller_ctx,
        autonomy_cap_paise=60000,
        autonomy_daily_cap_paise=600000,
        expected_version=1,
    )

    assert settings_route.called
    req = settings_route.calls.last.request
    assert req.headers["Authorization"] == "Bearer seller-bearer-token-456"
    assert req.headers["If-Match"] == "1"
    assert req.headers["X-Request-ID"] == "req-write-seller-001"

    body = json.loads(req.content.decode("utf-8"))
    assert body == {"autonomy_cap_paise": 60000, "autonomy_daily_cap_paise": 600000}
    assert isinstance(updated, StoreSettings)
    assert updated.autonomy_cap_paise == 60000
    assert updated.version == 2


# ── Token Caching & Refresh Tests ────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_service_token_caching_and_refresh_60s(
    client: HttpStoreClient, service_ctx: ServiceContext, clock: FixedClock
) -> None:
    """Service token cached until 60 seconds before expiry; refreshes automatically."""
    token_calls = 0

    def _token_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        token_calls += 1
        body = json.loads(request.content.decode("utf-8"))
        assert body["client_id"] == "agent-executor"
        assert body["client_secret"] == "exec-secret"
        return httpx.Response(
            200, json={"access_token": f"tok-v{token_calls}", "expires_in": 120}
        )

    respx.post("http://localhost:8000/api/v1/auth/token").mock(side_effect=_token_handler)
    respx.post("http://localhost:8000/api/v1/refunds/RFD-1042-01/release").respond(
        json={**REFUND_EXAMPLE, "status": "processing"}
    )

    # Call 1 at T0: fetches tok-v1 (valid for 120s, refresh window starts at T0 + 60s)
    await client.release_refund(service_ctx, "RFD-1042-01", "idem-t1")
    assert token_calls == 1

    # Advance clock by 30 seconds -> 90s remaining (> 60s before expiry) -> uses cached token
    clock.advance(timedelta(seconds=30))
    await client.release_refund(service_ctx, "RFD-1042-01", "idem-t2")
    assert token_calls == 1

    # Advance clock by 40 more seconds (total 70s elapsed -> 50s remaining < 60s) -> must refresh
    clock.advance(timedelta(seconds=40))
    await client.release_refund(service_ctx, "RFD-1042-01", "idem-t3")
    assert token_calls == 2


# ── Token Isolation Tests ────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_token_isolation_customer_never_sent_on_service_steps(
    client: HttpStoreClient, customer_ctx: CustomerContext, service_ctx: ServiceContext
) -> None:
    """Customer token is strictly forbidden from being sent on service steps."""
    # 1. Customer role caller rejected on service-only operations
    with pytest.raises(StoreForbidden):
        await client.reserve_refund(
            service_ctx=customer_ctx,
            order_id="ORD-1042",
            amount_paise=10000,
            return_id=None,
            method="original",
            idempotency_key="idem-iso-1",
        )

    with pytest.raises(StoreForbidden):
        await client.release_refund(
            ctx=customer_ctx, refund_id="RFD-1042-01", idempotency_key="idem-iso-2"
        )

    with pytest.raises(StoreForbidden):
        await client.cancel_reservation(
            ctx=customer_ctx, refund_id="RFD-1042-01", idempotency_key="idem-iso-3"
        )

    # 2. ServiceContext carrying a customer token must NOT leak customer token
    respx.post("http://localhost:8000/api/v1/auth/token").respond(
        json={"access_token": "tok-exec-safe", "expires_in": 600}
    )
    rel_route = respx.post(
        "http://localhost:8000/api/v1/refunds/RFD-1042-01/release"
    ).respond(json={**REFUND_EXAMPLE, "status": "processing"})

    polluted_service_ctx = ServiceContext(
        principal=service_ctx.principal,
        request_id="req-iso-leak-test",
        token=SecretStr("leak-customer-token"),
        scopes=service_ctx.scopes,
    )
    await client.release_refund(polluted_service_ctx, "RFD-1042-01", "idem-iso-4")

    # Verified: sent executor service token, NOT the customer token!
    sent_auth = rel_route.calls.last.request.headers["Authorization"]
    assert sent_auth == "Bearer tok-exec-safe"
    assert "leak-customer-token" not in sent_auth


@respx.mock
@pytest.mark.asyncio
async def test_token_isolation_service_never_sent_on_seller_steps(
    client: HttpStoreClient, service_ctx: ServiceContext, seller_ctx: SellerContext
) -> None:
    """Service caller or missing seller token is rejected on seller steps."""
    # 1. Service caller attempting seller step is rejected
    with pytest.raises(StoreForbidden):
        await client.authorize_refund(
            seller_ctx=service_ctx,
            refund_id="RFD-1042-01",
            amount_paise=10000,
            note="Service trying to authorize",
            idempotency_key="idem-iso-5",
        )

    # 2. Seller context with None token is rejected
    missing_tok_seller = SellerContext(
        principal=seller_ctx.principal,
        request_id="req-seller-no-tok",
        token=None,
    )
    with pytest.raises(StoreForbidden):
        await client.authorize_refund(
            seller_ctx=missing_tok_seller,
            refund_id="RFD-1042-01",
            amount_paise=10000,
            note="Missing token",
            idempotency_key="idem-iso-6",
        )


@respx.mock
@pytest.mark.asyncio
async def test_token_isolation_customer_step_requires_customer_token(
    client: HttpStoreClient, service_ctx: ServiceContext, customer_ctx: CustomerContext
) -> None:
    """create_return requires customer role and customer token."""
    items = [ReturnItem(item_id="ITEM-1042-1", qty=1)]

    # Service caller rejected
    with pytest.raises(StoreForbidden):
        await client.create_return(
            ctx=service_ctx,
            order_id="ORD-1042",
            items=items,
            reason_code="defective",
            reason_text="None",
            resolution="refund",
            idempotency_key="idem-iso-7",
        )

    # Customer caller without token rejected
    no_tok_cust = CustomerContext(
        principal=customer_ctx.principal,
        request_id="req-no-tok-cust",
        token=None,
    )
    with pytest.raises(StoreForbidden):
        await client.create_return(
            ctx=no_tok_cust,
            order_id="ORD-1042",
            items=items,
            reason_code="defective",
            reason_text="None",
            resolution="refund",
            idempotency_key="idem-iso-8",
        )


# ── Error Mapping Tests ──────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_error_mapping_409_variants(
    client: HttpStoreClient, customer_ctx: CustomerContext
) -> None:
    """409 status code maps to duplicate, limit_exceeded, invalid_state, or mismatch."""
    items = [ReturnItem(item_id="ITEM-1042-1", qty=1)]
    base_url = "http://localhost:8000/api/v1/orders/ORD-1042/returns"

    # 1. Duplicate
    respx.post(base_url).respond(
        status_code=409,
        json={
            "error": {
                "code": "duplicate",
                "message": "Return already exists",
                "request_id": "r1",
            }
        },
    )
    with pytest.raises(StoreDuplicate):
        await client.create_return(
            customer_ctx, "ORD-1042", items, "defective", None, "refund", "k1"
        )

    # 2. Limit exceeded
    respx.post(base_url).respond(
        status_code=409,
        json={
            "error": {
                "code": "limit_exceeded",
                "message": "Daily cap exceeded",
                "request_id": "r2",
            }
        },
    )
    with pytest.raises(StoreLimitExceeded):
        await client.create_return(
            customer_ctx, "ORD-1042", items, "defective", None, "refund", "k2"
        )

    # 3. Invalid state
    respx.post(base_url).respond(
        status_code=409,
        json={
            "error": {
                "code": "invalid_state",
                "message": "Order not delivered",
                "request_id": "r3",
            }
        },
    )
    with pytest.raises(StoreInvalidState):
        await client.create_return(
            customer_ctx, "ORD-1042", items, "defective", None, "refund", "k3"
        )

    # 4. Idempotency mismatch
    respx.post(base_url).respond(
        status_code=409,
        json={
            "error": {
                "code": "idempotency_mismatch",
                "message": "Differing payload",
                "request_id": "r4",
            }
        },
    )
    with pytest.raises(StoreIdempotencyMismatch):
        await client.create_return(
            customer_ctx, "ORD-1042", items, "defective", None, "refund", "k4"
        )


@respx.mock
@pytest.mark.asyncio
async def test_error_mapping_404_401_412_5xx_network_timeout(
    client: HttpStoreClient, customer_ctx: CustomerContext, seller_ctx: SellerContext
) -> None:
    """Status codes and exceptions map to appropriate StoreClientError subtypes."""
    items = [ReturnItem(item_id="ITEM-1042-1", qty=1)]
    url = "http://localhost:8000/api/v1/orders/ORD-1042/returns"

    # 404 Not Found
    respx.post(url).respond(
        status_code=404,
        json={"error": {"code": "not_found", "message": "Order not found"}},
    )
    with pytest.raises(StoreNotFound):
        await client.create_return(
            customer_ctx, "ORD-1042", items, "defective", None, "refund", "k-404"
        )

    # 401/403 Forbidden
    respx.post(url).respond(
        status_code=403,
        json={"error": {"code": "forbidden", "message": "Forbidden"}},
    )
    with pytest.raises(StoreForbidden):
        await client.create_return(
            customer_ctx, "ORD-1042", items, "defective", None, "refund", "k-403"
        )

    # 412 Precondition Failed (version mismatch) on PUT /admin/settings
    respx.put("http://localhost:8000/api/v1/admin/settings").respond(
        status_code=412,
        json={"error": {"code": "invalid_state", "message": "Version mismatch"}},
    )
    with pytest.raises(StoreInvalidState):
        await client.update_settings(seller_ctx, 50000, 500000, expected_version=1)

    # 500/503 Unavailable
    respx.post(url).respond(status_code=503, text="Service Unavailable")
    with pytest.raises(StoreUnavailable):
        await client.create_return(
            customer_ctx, "ORD-1042", items, "defective", None, "refund", "k-503"
        )

    # Network Connection Error -> StoreUnavailable
    respx.post(url).mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(StoreUnavailable):
        await client.create_return(
            customer_ctx, "ORD-1042", items, "defective", None, "refund", "k-net"
        )

    # Read / Connect Timeout -> StoreTimeout
    respx.post(url).mock(side_effect=httpx.ReadTimeout("Socket read timed out"))
    with pytest.raises(StoreTimeout):
        await client.create_return(
            customer_ctx, "ORD-1042", items, "defective", None, "refund", "k-tout"
        )


# ── No Retries on Writes ─────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_no_retries_on_mutating_writes(
    client: HttpStoreClient, customer_ctx: CustomerContext
) -> None:
    """Mutating POST calls must fail immediately on 503 or network failure without retrying."""
    route = respx.post("http://localhost:8000/api/v1/orders/ORD-1042/returns").respond(
        status_code=503, text="Gateway Error"
    )

    items = [ReturnItem(item_id="ITEM-1042-1", qty=1)]
    with pytest.raises(StoreUnavailable):
        await client.create_return(
            customer_ctx, "ORD-1042", items, "defective", None, "refund", "idem-no-retry"
        )

    # Exactly 1 attempt made (no retry loop)
    assert route.call_count == 1
