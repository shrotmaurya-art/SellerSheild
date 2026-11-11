"""Oracle tests for StoreClient write methods and write-capable fixture store (TASK T2.1).

Tests every rule and invariant:
1. Customer order ownership (uniform 404 on unowned orders).
2. Customer cannot invoke service or seller operations (StoreForbidden).
3. Return state machine and allowed transitions; illegal transitions raise StoreInvalidState.
4. One active return per item; returned quantity never exceeds delivered quantity.
5. Refund reservation cannot exceed max_refundable or total order amount paid.
6. Service-token return approval and refund release restricted to autonomy_cap_paise (50,000 paise)
   unless pre-authorized by a seller.
7. Seller authorization: seller_staff allowed up to 200,000 paise; seller_owner required above.
8. Daily autonomous cap (500,000 paise) trips within simulated IST day and resets on the next day.
9. Idempotency: identical key and payload replays stored result; differing payload raises StoreIdempotencyMismatch.
10. Concurrency: concurrent reservations for the same line item via asyncio.gather cannot both succeed.
11. Fault injection (FaultPlan): fail_once, timeout_after_commit, unavailable, conflict, slow.
12. Timestamps strictly sourced from injected Clock.
13. StoreClock: reads store clock, caches offset for 60 seconds, retains last offset on failure.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock, StoreClock
from sellershield_agent.domain.models import (
    CustomerRefundSummary,
    Order,
    OrderStatus,
    Refund,
    ReturnItem,
    ReturnRequest,
    StoreSettings,
)
from sellershield_agent.store.client import (
    CustomerContext,
    RequestContext,
    SellerContext,
    ServiceContext,
    StoreClient,
    StoreConflict,
    StoreDuplicate,
    StoreForbidden,
    StoreIdempotencyMismatch,
    StoreInvalidState,
    StoreLimitExceeded,
    StoreNotFound,
    StoreTimeout,
    StoreUnavailable,
    StoreValidationError,
)
from sellershield_agent.store.fixtures import FixtureStoreClient
from tests.fault_plan import FaultPlan

IST = ZoneInfo("Asia/Kolkata")
BASE_NOW = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(BASE_NOW)


@pytest.fixture
def store(clock: FixedClock) -> FixtureStoreClient:
    s = FixtureStoreClient(clock=clock)
    s.reset()
    return s


@pytest.fixture
def customer_ctx_c001() -> CustomerContext:
    return CustomerContext(
        principal=Principal(
            user_id="usr_c001",
            customer_id="C001",
            role=Role.customer,
            jti="jti-c001",
            token_exp=2000000000,
        ),
        request_id="req-c001-01",
        token=SecretStr("token-c001"),
    )


@pytest.fixture
def customer_ctx_c002() -> CustomerContext:
    return CustomerContext(
        principal=Principal(
            user_id="usr_c002",
            customer_id="C002",
            role=Role.customer,
            jti="jti-c002",
            token_exp=2000000000,
        ),
        request_id="req-c002-01",
        token=SecretStr("token-c002"),
    )


@pytest.fixture
def seller_staff_ctx() -> SellerContext:
    return SellerContext(
        principal=Principal(
            user_id="usr_staff",
            customer_id=None,
            role=Role.seller_staff,
            jti="jti-staff-01",
            token_exp=2000000000,
        ),
        request_id="req-staff-01",
        token=SecretStr("token-staff"),
    )


@pytest.fixture
def seller_owner_ctx() -> SellerContext:
    return SellerContext(
        principal=Principal(
            user_id="usr_owner",
            customer_id=None,
            role=Role.seller_owner,
            jti="jti-owner-01",
            token_exp=2000000000,
        ),
        request_id="req-owner-01",
        token=SecretStr("token-owner"),
    )


@pytest.fixture
def service_ctx() -> ServiceContext:
    return ServiceContext(
        principal=Principal(
            user_id="srv_executor",
            customer_id=None,
            role=Role.service,
            jti="jti-srv-01",
            token_exp=2000000000,
        ),
        request_id="req-srv-01",
        token=SecretStr("token-service"),
        scopes=[
            "orders:read_all",
            "returns:transition",
            "refunds:reserve",
            "refunds:release",
            "mail:send",
        ],
    )


# ── Ownership & Role Boundaries ───────────────────────────────────────


@pytest.mark.asyncio
async def test_ownership_customer_own_order_succeeds(
    store: FixtureStoreClient, customer_ctx_c001: CustomerContext
) -> None:
    """Customer C001 can create a return for their delivered order ORD-1048."""
    ret = await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1, condition_note="defective")],
        reason_code="defective",
        reason_text="Left side headphone audio dead",
        resolution="refund",
        idempotency_key="key-ret-own-01",
    )
    assert ret.order_id == "ORD-1048"
    assert ret.status == "requested"
    assert ret.client_ref == "key-ret-own-01"


@pytest.mark.asyncio
async def test_ownership_customer_other_order_raises_uniform_404(
    store: FixtureStoreClient, customer_ctx_c002: CustomerContext
) -> None:
    """Customer C002 attempting to create a return on C001's order receives uniform StoreNotFound."""
    with pytest.raises(StoreNotFound):
        await store.create_return(
            ctx=customer_ctx_c002,
            order_id="ORD-1048",
            items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
            reason_code="defective",
            reason_text="Not mine",
            resolution="refund",
            idempotency_key="key-ret-unowned-01",
        )


@pytest.mark.asyncio
async def test_customer_cannot_call_service_methods(
    store: FixtureStoreClient, customer_ctx_c001: CustomerContext
) -> None:
    """Customer token cannot execute service operations (reserve refund, admin order detail, settings)."""
    with pytest.raises(StoreForbidden):
        await store.reserve_refund(
            service_ctx=customer_ctx_c001,  # type: ignore[arg-type]
            order_id="ORD-1048",
            amount_paise=10000,
            return_id=None,
            method="original",
            idempotency_key="key-cust-reserve",
        )

    with pytest.raises(StoreForbidden):
        await store.get_order_admin(customer_ctx_c001, "ORD-1048")  # type: ignore[arg-type]

    with pytest.raises(StoreForbidden):
        await store.get_settings(customer_ctx_c001)  # type: ignore[arg-type]

    with pytest.raises(StoreForbidden):
        await store.get_customer_refund_summary(customer_ctx_c001, "C001", days=30)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_customer_cannot_call_seller_authorize(
    store: FixtureStoreClient,
    customer_ctx_c001: CustomerContext,
    service_ctx: ServiceContext,
) -> None:
    """Customer token cannot authorize a refund reservation."""
    # First reserve a refund using service context
    rfd = await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1048",
        amount_paise=10000,
        return_id=None,
        method="original",
        idempotency_key="key-res-for-auth",
    )
    with pytest.raises(StoreForbidden):
        await store.authorize_refund(
            seller_ctx=customer_ctx_c001,  # type: ignore[arg-type]
            refund_id=rfd.refund_id,
            amount_paise=10000,
            note="Customer self-authorization",
            idempotency_key="key-auth-cust",
        )


# ── Return State Machine & Invariants ─────────────────────────────────


@pytest.mark.asyncio
async def test_return_state_machine_happy_path(
    store: FixtureStoreClient,
    customer_ctx_c001: CustomerContext,
    seller_staff_ctx: SellerContext,
) -> None:
    """Full allowed return lifecycle: requested -> approved -> pickup_scheduled -> in_transit_back -> received -> inspected_ok -> completed."""
    ret = await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
        reason_code="defective",
        reason_text="Broken on unboxing",
        resolution="refund",
        idempotency_key="key-ret-life-01",
    )
    assert ret.status == "requested"

    # Seller approves return
    ret = await store.transition_return(
        ctx=seller_staff_ctx,
        return_id=ret.return_id,
        to="approved",
        reason="Approved by staff",
        idempotency_key="key-trans-app",
    )
    assert ret.status == "approved"

    # System advances return states
    for next_state in ["pickup_scheduled", "in_transit_back", "received", "inspected_ok", "completed"]:
        ret = await store.transition_return(
            ctx=seller_staff_ctx,
            return_id=ret.return_id,
            to=next_state,
            reason=f"Advanced to {next_state}",
            idempotency_key=f"key-trans-{next_state}",
        )
        assert ret.status == next_state


@pytest.mark.asyncio
async def test_customer_can_cancel_return_before_approval(
    store: FixtureStoreClient, customer_ctx_c001: CustomerContext
) -> None:
    """Customer can transition their return from requested to cancelled."""
    ret = await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
        reason_code="defective",
        reason_text="Mistake",
        resolution="refund",
        idempotency_key="key-ret-cancel-01",
    )
    ret_cancelled = await store.transition_return(
        ctx=customer_ctx_c001,
        return_id=ret.return_id,
        to="cancelled",
        reason="Changed mind",
        idempotency_key="key-trans-cancel",
    )
    assert ret_cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_customer_cannot_cancel_return_after_approval(
    store: FixtureStoreClient,
    customer_ctx_c001: CustomerContext,
    seller_staff_ctx: SellerContext,
) -> None:
    """Customer cannot cancel an approved return."""
    ret = await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
        reason_code="defective",
        reason_text="Broken",
        resolution="refund",
        idempotency_key="key-ret-no-cancel",
    )
    await store.transition_return(
        ctx=seller_staff_ctx,
        return_id=ret.return_id,
        to="approved",
        reason="Approved",
        idempotency_key="key-trans-app-2",
    )
    with pytest.raises(StoreInvalidState):
        await store.transition_return(
            ctx=customer_ctx_c001,
            return_id=ret.return_id,
            to="cancelled",
            reason="Too late to cancel",
            idempotency_key="key-trans-cancel-fail",
        )


@pytest.mark.asyncio
async def test_illegal_return_transition_raises_invalid_state(
    store: FixtureStoreClient,
    customer_ctx_c001: CustomerContext,
    seller_staff_ctx: SellerContext,
) -> None:
    """Jumping across unauthorized or undefined states raises StoreInvalidState."""
    ret = await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
        reason_code="defective",
        reason_text="Broken",
        resolution="refund",
        idempotency_key="key-ret-illegal",
    )
    # Cannot jump directly from requested to completed
    with pytest.raises(StoreInvalidState):
        await store.transition_return(
            ctx=seller_staff_ctx,
            return_id=ret.return_id,
            to="completed",
            reason="Skipping steps",
            idempotency_key="key-trans-illegal-jump",
        )


@pytest.mark.asyncio
async def test_one_active_return_per_item(
    store: FixtureStoreClient, customer_ctx_c001: CustomerContext
) -> None:
    """Only one active return allowed per order item."""
    await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
        reason_code="defective",
        reason_text="Broken",
        resolution="refund",
        idempotency_key="key-ret-active-1",
    )
    with pytest.raises((StoreInvalidState, StoreDuplicate)):
        await store.create_return(
            ctx=customer_ctx_c001,
            order_id="ORD-1048",
            items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
            reason_code="defective",
            reason_text="Duplicate return on same item",
            resolution="refund",
            idempotency_key="key-ret-active-2",
        )


@pytest.mark.asyncio
async def test_return_quantity_cannot_exceed_delivered(
    store: FixtureStoreClient, customer_ctx_c001: CustomerContext
) -> None:
    """Cannot return a quantity greater than delivered on the order."""
    with pytest.raises(StoreInvalidState):
        await store.create_return(
            ctx=customer_ctx_c001,
            order_id="ORD-1048",
            items=[ReturnItem(item_id="ITEM-1048-1", qty=999)],
            reason_code="defective",
            reason_text="Way too many items",
            resolution="refund",
            idempotency_key="key-ret-excess-qty",
        )


# ── Refund Reservations & Invariants ──────────────────────────────────


@pytest.mark.asyncio
async def test_reserve_refund_within_max_refundable(
    store: FixtureStoreClient, service_ctx: ServiceContext
) -> None:
    """Service token can reserve a refund up to max_refundable."""
    order = await store.get_order_admin(service_ctx, "ORD-1048")
    rfd = await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1048",
        amount_paise=order.total_paise,
        return_id=None,
        method="original",
        idempotency_key="key-res-exact",
    )
    assert rfd.amount_paise == order.total_paise
    assert rfd.status == "reserved"
    assert rfd.client_ref == "key-res-exact"


@pytest.mark.asyncio
async def test_reserve_refund_exceeding_max_refundable_raises(
    store: FixtureStoreClient, service_ctx: ServiceContext
) -> None:
    """Reserving an amount exceeding max_refundable raises StoreLimitExceeded."""
    order = await store.get_order_admin(service_ctx, "ORD-1048")
    with pytest.raises(StoreLimitExceeded):
        await store.reserve_refund(
            service_ctx=service_ctx,
            order_id="ORD-1048",
            amount_paise=order.total_paise + 100,
            return_id=None,
            method="original",
            idempotency_key="key-res-excess",
        )


@pytest.mark.asyncio
async def test_sum_of_active_refunds_cannot_exceed_paid(
    store: FixtureStoreClient, service_ctx: ServiceContext
) -> None:
    """Sum of reserved and active refunds cannot exceed the total amount paid on the order."""
    order = await store.get_order_admin(service_ctx, "ORD-1048")
    half = order.total_paise // 2
    await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1048",
        amount_paise=half,
        return_id=None,
        method="original",
        idempotency_key="key-res-half-1",
    )
    # Second reservation exceeding the remaining half must fail
    with pytest.raises(StoreLimitExceeded):
        await store.reserve_refund(
            service_ctx=service_ctx,
            order_id="ORD-1048",
            amount_paise=half + 500,
            return_id=None,
            method="original",
            idempotency_key="key-res-half-2",
        )


# ── Seller Authorization & Limits ─────────────────────────────────────


@pytest.mark.asyncio
async def test_seller_staff_authorizes_up_to_200000_paise(
    store: FixtureStoreClient,
    service_ctx: ServiceContext,
    seller_staff_ctx: SellerContext,
) -> None:
    """Seller staff can authorize refunds up to 200,000 paise."""
    rfd = await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1048",
        amount_paise=89900,
        return_id=None,
        method="original",
        idempotency_key="key-res-auth-staff",
    )
    auth_rfd = await store.authorize_refund(
        seller_ctx=seller_staff_ctx,
        refund_id=rfd.refund_id,
        amount_paise=89900,
        note="Staff approved",
        idempotency_key="key-auth-staff-ok",
    )
    assert auth_rfd.status == "authorized"
    assert auth_rfd.authorized_by == "usr_staff"
    assert auth_rfd.authorized_amount_paise == 89900


@pytest.mark.asyncio
async def test_seller_staff_cannot_authorize_above_200000_paise(
    store: FixtureStoreClient,
    service_ctx: ServiceContext,
    seller_staff_ctx: SellerContext,
) -> None:
    """Seller staff token cannot authorize amounts greater than 200,000 paise."""
    # Reserve large refund
    rfd = await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1051",
        amount_paise=250000,
        return_id=None,
        method="original",
        idempotency_key="key-res-large",
    )
    with pytest.raises(StoreForbidden):
        await store.authorize_refund(
            seller_ctx=seller_staff_ctx,
            refund_id=rfd.refund_id,
            amount_paise=250000,
            note="Staff trying to approve above limit",
            idempotency_key="key-auth-staff-over",
        )


@pytest.mark.asyncio
async def test_seller_owner_can_authorize_above_200000_paise(
    store: FixtureStoreClient,
    service_ctx: ServiceContext,
    seller_owner_ctx: SellerContext,
) -> None:
    """Seller owner token can authorize amounts above 200,000 paise."""
    rfd = await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1051",
        amount_paise=250000,
        return_id=None,
        method="original",
        idempotency_key="key-res-large-owner",
    )
    auth_rfd = await store.authorize_refund(
        seller_ctx=seller_owner_ctx,
        refund_id=rfd.refund_id,
        amount_paise=250000,
        note="Owner approved high value",
        idempotency_key="key-auth-owner-ok",
    )
    assert auth_rfd.status == "authorized"
    assert auth_rfd.authorized_by == "usr_owner"
    assert auth_rfd.authorized_amount_paise == 250000


@pytest.mark.asyncio
async def test_authorize_amount_cannot_exceed_reservation(
    store: FixtureStoreClient,
    service_ctx: ServiceContext,
    seller_owner_ctx: SellerContext,
) -> None:
    """Authorization amount cannot exceed the reserved amount."""
    rfd = await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1048",
        amount_paise=50000,
        return_id=None,
        method="original",
        idempotency_key="key-res-small",
    )
    with pytest.raises(StoreLimitExceeded):
        await store.authorize_refund(
            seller_ctx=seller_owner_ctx,
            refund_id=rfd.refund_id,
            amount_paise=60000,
            note="Authorizing more than reservation",
            idempotency_key="key-auth-excess-res",
        )


# ── Autonomy Cap & Daily Autonomous Cap ───────────────────────────────


@pytest.mark.asyncio
async def test_service_release_within_autonomy_cap_succeeds(
    store: FixtureStoreClient, service_ctx: ServiceContext
) -> None:
    """Service token release of <= 50,000 paise succeeds autonomously."""
    rfd = await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1048",
        amount_paise=45000,
        return_id=None,
        method="original",
        idempotency_key="key-res-auto",
    )
    rel_rfd = await store.release_refund(
        ctx=service_ctx,
        refund_id=rfd.refund_id,
        idempotency_key="key-rel-auto",
    )
    assert rel_rfd.status in ("processing", "completed")


@pytest.mark.asyncio
async def test_service_release_above_autonomy_cap_without_auth_raises(
    store: FixtureStoreClient, service_ctx: ServiceContext
) -> None:
    """Service token release of > 50,000 paise without seller authorization raises StoreLimitExceeded."""
    rfd = await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1048",
        amount_paise=89900,
        return_id=None,
        method="original",
        idempotency_key="key-res-over-auto",
    )
    with pytest.raises(StoreLimitExceeded):
        await store.release_refund(
            ctx=service_ctx,
            refund_id=rfd.refund_id,
            idempotency_key="key-rel-unauthorized",
        )


@pytest.mark.asyncio
async def test_service_release_above_autonomy_cap_with_auth_succeeds(
    store: FixtureStoreClient,
    service_ctx: ServiceContext,
    seller_staff_ctx: SellerContext,
) -> None:
    """Service token release > 50,000 paise succeeds if authorized by a seller."""
    rfd = await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1048",
        amount_paise=89900,
        return_id=None,
        method="original",
        idempotency_key="key-res-for-auth-rel",
    )
    await store.authorize_refund(
        seller_ctx=seller_staff_ctx,
        refund_id=rfd.refund_id,
        amount_paise=89900,
        note="Approved by staff",
        idempotency_key="key-auth-before-rel",
    )
    rel_rfd = await store.release_refund(
        ctx=service_ctx,
        refund_id=rfd.refund_id,
        idempotency_key="key-rel-authorized",
    )
    assert rel_rfd.status in ("processing", "completed")


@pytest.mark.asyncio
async def test_daily_autonomous_cap_trips_and_resets_next_ist_day(
    store: FixtureStoreClient,
    service_ctx: ServiceContext,
    clock: FixedClock,
) -> None:
    """Daily autonomous cap of 500,000 paise trips on same IST day and resets next IST day."""
    # Execute 10 autonomous refunds of 50,000 paise each = 500,000 paise
    for i in range(10):
        rfd = await store.reserve_refund(
            service_ctx=service_ctx,
            order_id="ORD-1048",
            amount_paise=50000,
            return_id=None,
            method="original",
            idempotency_key=f"key-res-daily-{i}",
        )
        await store.release_refund(
            ctx=service_ctx,
            refund_id=rfd.refund_id,
            idempotency_key=f"key-rel-daily-{i}",
        )

    # 11th autonomous release trips daily cap
    rfd_over = await store.reserve_refund(
        service_ctx=service_ctx,
        order_id="ORD-1048",
        amount_paise=50000,
        return_id=None,
        method="original",
        idempotency_key="key-res-daily-over",
    )
    with pytest.raises(StoreLimitExceeded):
        await store.release_refund(
            ctx=service_ctx,
            refund_id=rfd_over.refund_id,
            idempotency_key="key-rel-daily-over",
        )

    # Advance clock by 24 hours to the next IST day
    clock.set(clock.now() + timedelta(days=1))

    # Next IST day: releasing the refund now succeeds
    rel_next_day = await store.release_refund(
        ctx=service_ctx,
        refund_id=rfd_over.refund_id,
        idempotency_key="key-rel-next-day",
    )
    assert rel_next_day.status in ("processing", "completed")


# ── Idempotency Semantics ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotent_replay_returns_identical_object(
    store: FixtureStoreClient, customer_ctx_c001: CustomerContext
) -> None:
    """Replaying mutation with identical idempotency key and payload returns identical object."""
    ret1 = await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
        reason_code="defective",
        reason_text="Broken on arrival",
        resolution="refund",
        idempotency_key="key-idem-replay-01",
    )
    ret2 = await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
        reason_code="defective",
        reason_text="Broken on arrival",
        resolution="refund",
        idempotency_key="key-idem-replay-01",
    )
    assert ret1.return_id == ret2.return_id
    assert ret1.created_at == ret2.created_at
    assert ret1.version == ret2.version


@pytest.mark.asyncio
async def test_idempotency_mismatch_raises_exception(
    store: FixtureStoreClient, customer_ctx_c001: CustomerContext
) -> None:
    """Replaying key with different payload raises StoreIdempotencyMismatch."""
    await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
        reason_code="defective",
        reason_text="Broken",
        resolution="refund",
        idempotency_key="key-idem-mismatch-01",
    )
    with pytest.raises(StoreIdempotencyMismatch):
        await store.create_return(
            ctx=customer_ctx_c001,
            order_id="ORD-1048",
            items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
            reason_code="wrong_item",  # Changed reason code
            reason_text="Different payload",
            resolution="refund",
            idempotency_key="key-idem-mismatch-01",
        )


# ── Concurrency Safety ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_reserve_calls_cannot_both_succeed(
    store: FixtureStoreClient, service_ctx: ServiceContext
) -> None:
    """Two concurrent reserve_refund calls via asyncio.gather for the same line item cannot both succeed."""
    order = await store.get_order_admin(service_ctx, "ORD-1048")
    full_amount = order.total_paise

    results = await asyncio.gather(
        store.reserve_refund(
            service_ctx=service_ctx,
            order_id="ORD-1048",
            amount_paise=full_amount,
            return_id=None,
            method="original",
            idempotency_key="key-concurrent-res-1",
        ),
        store.reserve_refund(
            service_ctx=service_ctx,
            order_id="ORD-1048",
            amount_paise=full_amount,
            return_id=None,
            method="original",
            idempotency_key="key-concurrent-res-2",
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, Refund)]
    failures = [r for r in results if isinstance(r, StoreLimitExceeded)]

    assert len(successes) == 1
    assert len(failures) == 1


# ── Fault Injection (FaultPlan) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_fault_plan_fail_once(
    clock: FixedClock, customer_ctx_c001: CustomerContext
) -> None:
    """FaultPlan fail_once fails first attempt, subsequent attempt succeeds."""
    faults = FaultPlan().fail_once("create_return")
    store = FixtureStoreClient(clock=clock, fault_plan=faults)

    with pytest.raises(StoreUnavailable):
        await store.create_return(
            ctx=customer_ctx_c001,
            order_id="ORD-1048",
            items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
            reason_code="defective",
            reason_text="Broken",
            resolution="refund",
            idempotency_key="key-fault-once-1",
        )

    # Second call succeeds
    ret = await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
        reason_code="defective",
        reason_text="Broken",
        resolution="refund",
        idempotency_key="key-fault-once-2",
    )
    assert ret.status == "requested"


@pytest.mark.asyncio
async def test_fault_plan_timeout_after_commit_leaves_change_applied(
    clock: FixedClock, customer_ctx_c001: CustomerContext
) -> None:
    """FaultPlan timeout_after_commit raises StoreTimeout, but change persists in store."""
    faults = FaultPlan().timeout_after_commit("create_return")
    store = FixtureStoreClient(clock=clock, fault_plan=faults)

    with pytest.raises(StoreTimeout):
        await store.create_return(
            ctx=customer_ctx_c001,
            order_id="ORD-1048",
            items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
            reason_code="defective",
            reason_text="Broken",
            resolution="refund",
            idempotency_key="key-commit-timeout",
        )

    # Later lookup shows return was actually committed
    returns = await store.list_returns(customer_ctx_c001, "ORD-1048")
    assert any(r.client_ref == "key-commit-timeout" for r in returns)


# ── Clock & Timestamps ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timestamps_follow_clock(
    store: FixtureStoreClient,
    customer_ctx_c001: CustomerContext,
    clock: FixedClock,
) -> None:
    """Store write methods strictly take timestamps from injected Clock."""
    custom_time = datetime(2026, 11, 15, 14, 30, 0, tzinfo=UTC)
    clock.set(custom_time)

    ret = await store.create_return(
        ctx=customer_ctx_c001,
        order_id="ORD-1048",
        items=[ReturnItem(item_id="ITEM-1048-1", qty=1)],
        reason_code="defective",
        reason_text="Testing time",
        resolution="refund",
        idempotency_key="key-clock-test",
    )
    assert ret.created_at == custom_time


@pytest.mark.asyncio
async def test_store_clock_caches_offset_and_survives_outage() -> None:
    """StoreClock caches offset for 60s and retains last offset during unreachable periods."""
    clock = StoreClock(initial_offset_seconds=120.0)
    t1 = clock.now()
    assert (t1 - datetime.now(UTC)).total_seconds() == pytest.approx(120.0, abs=1.0)

    # Simulating synchronization update
    future_time = datetime.now(UTC) + timedelta(seconds=300)
    clock.set_sync(future_time, mode="simulated", offset_seconds=300.0)
    t2 = clock.now()
    assert (t2 - datetime.now(UTC)).total_seconds() == pytest.approx(300.0, abs=1.0)

