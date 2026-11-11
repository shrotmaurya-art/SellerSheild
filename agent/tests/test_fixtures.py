"""Tests for StoreClient interface, domain models, and FixtureStoreClient.

Enforces:
- Spec: docs/spec/01-playground.md sections 5, 10, 16
- Invariants: KP-05 (money int paise), KP-06 (injected Clock), KP-15 (uniform not-found)
- Pydantic v2 frozen models with extra='forbid' and zero float fields
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest
from pydantic import SecretStr, ValidationError

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.domain.models import (
    CustomerRiskFacts,
    Order,
    OrderItem,
    OrderStatus,
    Refund,
    Refundable,
    ReturnRequest,
    Shipment,
)
from sellershield_agent.store.client import (
    RequestContext,
    StoreClient,
    StoreForbidden,
    StoreNotFound,
    StoreValidationError,
)
from sellershield_agent.store.fixtures import FixtureStoreClient

T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "scenarios" / "fixtures"
FIXTURES_PATH = FIXTURES_DIR / "store.json"


def _make_context(
    role: Role = Role.customer,
    user_id: str = "USR-001",
    customer_id: str | None = "C001",
    request_id: str = "req-test-1",
) -> RequestContext:
    principal = Principal(
        user_id=user_id,
        role=role,
        customer_id=customer_id,
        scopes=["order:read"],
        jti="jti-test-1",
        token_exp=int((T0_BASE + timedelta(hours=1)).timestamp()),
    )
    return RequestContext(
        principal=principal,
        request_id=request_id,
        token=SecretStr("mock.token.secret"),
    )


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0_BASE)


@pytest.fixture
def store(clock: FixedClock) -> StoreClient:
    return FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=clock)


# ── 1. Scenario Orders Load and Validate ────────────────────────────


@pytest.mark.asyncio
async def test_all_scenario_orders_load_and_validate(store: StoreClient) -> None:
    """Every scenario order (SC-01 to SC-17) loads and validates against domain models."""
    scenario_order_ids = [
        "ORD-1048",  # SC-01
        "ORD-1042",  # SC-02, SC-04, SC-14
        "ORD-2032",  # SC-05
        "ORD-1051",  # SC-06
        "ORD-1060",  # SC-07
        "ORD-2031",  # SC-08
        "ORD-3050",  # SC-09
        "ORD-2040",  # SC-10
        "ORD-1061",  # SC-11
        "ORD-2037",  # SC-12
        "ORD-5001",  # SC-13
        "ORD-3010",  # SC-15
        "ORD-3011",  # SC-15
        "ORD-4020",  # SC-16
        "ORD-4021",  # SC-16
        "ORD-1070",  # SC-17
    ]
    service_ctx = _make_context(role=Role.service, user_id="USR-SRV", customer_id=None)

    for order_id in scenario_order_ids:
        order = await store.get_order(service_ctx, order_id)
        assert isinstance(order, Order)
        assert order.order_id == order_id
        assert len(order.items) >= 1
        assert order.placed_at.tzinfo is not None
        assert order.total_paise >= 0


# ── 2. Ownership Uniformity (KP-15) ─────────────────────────────────


@pytest.mark.asyncio
async def test_ownership_uniformity_not_found(store: StoreClient) -> None:
    """An unowned order raises StoreNotFound with identical type and message to a nonexistent id."""
    ctx_c002 = _make_context(role=Role.customer, user_id="USR-002", customer_id="C002")

    # ORD-1042 exists in store but belongs to C001
    with pytest.raises(StoreNotFound) as exc_unowned:
        await store.get_order(ctx_c002, "ORD-1042")

    # ORD-9999 does not exist in store at all
    with pytest.raises(StoreNotFound) as exc_nonexistent:
        await store.get_order(ctx_c002, "ORD-9999")

    # Exact type and error message uniformity
    assert type(exc_unowned.value) is type(exc_nonexistent.value)
    assert str(exc_unowned.value) == "Order 'ORD-1042' not found"
    assert str(exc_nonexistent.value) == "Order 'ORD-9999' not found"


# ── 3. Money Invariants & Internal Consistency (KP-05) ───────────────


@pytest.mark.asyncio
async def test_fixtures_money_invariants(store: StoreClient) -> None:
    """Property over fixtures: line items, coupons and totals sum to exact paise."""
    service_ctx = _make_context(role=Role.service, user_id="USR-SRV", customer_id=None)
    all_orders = await store.list_orders(service_ctx)
    assert len(all_orders) >= 17

    for order in all_orders:
        item_sum = 0
        discount_alloc_sum = 0
        line_paid_sum = 0

        for item in order.items:
            expected_line_paid = (item.qty * item.unit_price_paise) - item.discount_alloc_paise
            assert item.line_paid_paise == expected_line_paid
            item_sum += item.qty * item.unit_price_paise
            discount_alloc_sum += item.discount_alloc_paise
            line_paid_sum += item.line_paid_paise

        assert order.subtotal_paise == item_sum
        assert discount_alloc_sum == order.discount_paise
        expected_total = (
            order.subtotal_paise - order.discount_paise + order.shipping_fee_paise
        )
        assert order.total_paise == expected_total
        assert order.total_paise == line_paid_sum + order.shipping_fee_paise

    # Specific check for ORD-2031 coupon allocation
    ord_2031 = await store.get_order(service_ctx, "ORD-2031")
    assert ord_2031.total_paise == 164700
    assert ord_2031.discount_paise == 10000
    allocations = sorted([item.discount_alloc_paise for item in ord_2031.items])
    assert allocations == [1998, 8002]


# ── 4. Relative Times Resolution (KP-06) ─────────────────────────────


@pytest.mark.asyncio
async def test_relative_times_resolve_under_different_clocks() -> None:
    """Relative offsets from T0 resolve deterministically under different FixedClock values."""
    clock_a = FixedClock(T0_BASE)
    store_a = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=clock_a)

    clock_b = FixedClock(T0_BASE + timedelta(days=10))
    store_b = FixtureStoreClient(fixtures_path=FIXTURES_PATH, clock=clock_b)

    service_ctx = _make_context(role=Role.service, user_id="USR-SRV", customer_id=None)

    order_a = await store_a.get_order(service_ctx, "ORD-1042")
    order_b = await store_b.get_order(service_ctx, "ORD-1042")

    # ORD-1042 is placed T0-5d, shipped T0-4d, delivered T0-3d
    assert order_a.placed_at == T0_BASE - timedelta(days=5)
    assert order_a.shipped_at == T0_BASE - timedelta(days=4)
    assert order_a.delivered_at == T0_BASE - timedelta(days=3)

    assert order_b.placed_at == (T0_BASE + timedelta(days=10)) - timedelta(days=5)
    assert order_b.shipped_at == (T0_BASE + timedelta(days=10)) - timedelta(days=4)
    assert order_b.delivered_at == (T0_BASE + timedelta(days=10)) - timedelta(days=3)

    assert order_b.delivered_at - order_a.delivered_at == timedelta(days=10)


# ── 5. Zero Float Fields on Domain Models ────────────────────────────


def _check_no_floats(annotation: Any) -> None:
    if annotation is float:
        raise AssertionError("Found float field in domain models!")
    origin = get_origin(annotation)
    if origin is not None:
        for arg in get_args(annotation):
            _check_no_floats(arg)


def test_no_float_fields_on_domain_models() -> None:
    """Domain models must not have any float fields (money is int paise)."""
    models = [
        Order,
        OrderItem,
        Shipment,
        Refund,
        Refundable,
        ReturnRequest,
        CustomerRiskFacts,
    ]
    for model in models:
        for _field_name, field_info in model.model_fields.items():
            _check_no_floats(field_info.annotation)

    # Validate CustomerRiskFacts field types specifically
    facts = CustomerRiskFacts(
        customer_id="C002",
        returns_30d=1,
        refund_ratio_90d_bp=792,
        account_age_days=120,
        refunded_30d_paise=40000,
    )
    assert isinstance(facts.refund_ratio_90d_bp, int)
    assert isinstance(facts.refunded_30d_paise, int)


# ── 6. Customer Risk Facts Authorization Guard ───────────────────────


@pytest.mark.asyncio
async def test_customer_risk_facts_forbidden_for_customer(store: StoreClient) -> None:
    """Customer role contexts cannot access customer risk facts."""
    cust_ctx = _make_context(role=Role.customer, customer_id="C001")
    srv_ctx = _make_context(role=Role.service, customer_id=None)

    with pytest.raises(StoreForbidden):
        await store.get_customer_risk_facts(cust_ctx, "C001")

    facts = await store.get_customer_risk_facts(srv_ctx, "C001")
    assert isinstance(facts, CustomerRiskFacts)
    assert facts.customer_id == "C001"
    assert facts.returns_30d == 0


# ── 7. Order ID Format Validation ───────────────────────────────────


@pytest.mark.asyncio
async def test_order_id_format_rejection(store: StoreClient) -> None:
    """Malformed order IDs raise StoreValidationError and never touch data or return 404."""
    service_ctx = _make_context(role=Role.service, user_id="USR-SRV", customer_id=None)

    malformed_ids = [
        "ord-1042",        # lowercase prefix
        " ORD-1042",       # leading whitespace
        "ORD-1042 ",       # trailing whitespace
        "ORD-12",          # too short (< 3 digits)
        "ORD-1234567",     # too long (> 6 digits)
        "ORD-1042\n",      # newline injection
        "ОRD-1042",        # cyrillic O lookalike
        "ORD-١٢٣",         # arabic unicode digits
        "ORD-104a",        # non-digit character
        "ORD- 1042",       # space inside
        "ORD1042",         # missing hyphen
    ]

    for bad_id in malformed_ids:
        with pytest.raises(StoreValidationError):
            await store.get_order(service_ctx, bad_id)


# ── 8. Models Are Frozen ────────────────────────────────────────────


def test_models_are_frozen() -> None:
    """Models are immutable (ConfigDict frozen=True, extra='forbid')."""
    item = OrderItem(
        item_id="ITEM-1",
        sku="P-101",
        name="Test Item",
        category="general",
        qty=1,
        unit_price_paise=1000,
        discount_alloc_paise=0,
        line_paid_paise=1000,
        flags={},
    )
    with pytest.raises(ValidationError):
        item.qty = 2  # type: ignore[misc]

    with pytest.raises(ValidationError):
        item.unit_price_paise = 2000  # type: ignore[misc]


# ── 9. Delivered At Exists Only for Delivered Orders ─────────────────


@pytest.mark.asyncio
async def test_delivered_at_only_on_delivered_orders(store: StoreClient) -> None:
    """delivered_at exists only on delivered orders; shipped_at only on shipped/delivered."""
    service_ctx = _make_context(role=Role.service, user_id="USR-SRV", customer_id=None)
    all_orders = await store.list_orders(service_ctx)

    for order in all_orders:
        if order.status == OrderStatus.delivered:
            assert order.delivered_at is not None
            assert order.shipped_at is not None
            assert order.placed_at <= order.shipped_at <= order.delivered_at
        elif order.status == OrderStatus.shipped:
            assert order.delivered_at is None
            assert order.shipped_at is not None
        elif order.status in (OrderStatus.placed, OrderStatus.packed):
            assert order.delivered_at is None
            assert order.shipped_at is None


# ── 10. ORD-2040 Earlier Refund and Refundable ──────────────────────


@pytest.mark.asyncio
async def test_ord_2040_earlier_refund_and_refundable(store: StoreClient) -> None:
    """ORD-2040 has a prior refund of 40000 paise and refundable remaining is 50000 paise."""
    service_ctx = _make_context(role=Role.service, user_id="USR-SRV", customer_id=None)

    refunds = await store.list_refunds(service_ctx, "ORD-2040")
    assert len(refunds) == 1
    assert refunds[0].amount_paise == 40000
    assert refunds[0].status.value == "completed"

    refundable = await store.get_refundable(service_ctx, "ORD-2040")
    assert isinstance(refundable, Refundable)
    assert refundable.order_id == "ORD-2040"
    assert refundable.max_refundable_paise == 50000
