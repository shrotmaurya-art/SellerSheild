"""Tests for Policy documents and refund calculator.

Enforces:
- Spec: docs/spec/02-agent.md sections 7.1, 7.2, 7.4, 7.6, and amendment 6
- Invariants: KP-05 (integer paise, no float), KP-23 (no forbidden words in customer text)
- Worked examples: ORD-1042, ORD-2032, ORD-2031, ORD-2040
- Loader validation, immutability, and version differentiation
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from sellershield_agent.domain.models import (
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    ReturnItem,
    ReturnReasonCode,
    ReturnResolution,
)
from sellershield_agent.policy.loader import load_policy, load_policy_from_dict
from sellershield_agent.policy.models import PolicyDocument
from sellershield_agent.policy.refund import compute_refund
from sellershield_agent.policy.summary import customer_safe_summary

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "scenarios" / "fixtures"
FIXTURES_PATH = FIXTURES_DIR / "store.json"
NOW_UTC = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


def _load_fixture_orders() -> dict[str, Order]:
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    orders_map: dict[str, Order] = {}
    for raw in data.get("orders", []):
        raw_copy = dict(raw)
        raw_copy.pop("placed_at_offset", None)
        raw_copy.pop("shipped_at_offset", None)
        raw_copy.pop("delivered_at_offset", None)
        raw_copy.pop("cancelled_at_offset", None)
        raw_copy["placed_at"] = NOW_UTC
        if raw.get("status") in ("shipped", "delivered"):
            raw_copy["shipped_at"] = NOW_UTC
        if raw.get("status") == "delivered":
            raw_copy["delivered_at"] = NOW_UTC
        order = Order.model_validate(raw_copy)
        orders_map[order.order_id] = order
    return orders_map


@pytest.fixture(scope="module")
def fixture_orders() -> dict[str, Order]:
    return _load_fixture_orders()


@pytest.fixture(scope="module")
def default_policy() -> PolicyDocument:
    return load_policy("v3")


# ── 1. Worked Examples (§7.4) ──────────────────────────────────────


def test_ord_1042_defective_gives_120000(
    fixture_orders: dict[str, Order],
    default_policy: PolicyDocument,
) -> None:
    order = fixture_orders["ORD-1042"]
    requested = [ReturnItem(item_id="ITEM-1042-1", qty=1)]
    breakdown = compute_refund(
        order=order,
        requested_items=requested,
        reason_code=ReturnReasonCode.defective,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=True,
        shipment_lost=False,
        max_refundable_paise=120000,
    )

    assert breakdown.total_paise == 120000
    assert breakdown.fee_paise == 0
    assert breakdown.shipping_refund_paise == 0
    assert breakdown.items_paise == 120000
    assert breakdown.capped is False
    assert breakdown.cap_amount_paise is None


def test_ord_2032_changed_mind_gives_43900(
    fixture_orders: dict[str, Order],
    default_policy: PolicyDocument,
) -> None:
    order = fixture_orders["ORD-2032"]
    requested = [ReturnItem(item_id="ITEM-2032-1", qty=1)]
    breakdown = compute_refund(
        order=order,
        requested_items=requested,
        reason_code=ReturnReasonCode.changed_mind,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=True,
        shipment_lost=False,
        max_refundable_paise=49900,
    )

    assert breakdown.total_paise == 43900
    assert breakdown.fee_paise == 6000
    assert breakdown.shipping_refund_paise == 0
    assert breakdown.items_paise == 49900
    assert breakdown.capped is False


def test_ord_2031_one_kurta_changed_mind_gives_59899(
    fixture_orders: dict[str, Order],
    default_policy: PolicyDocument,
) -> None:
    order = fixture_orders["ORD-2031"]
    requested = [ReturnItem(item_id="ITEM-2031-1", qty=1)]
    breakdown = compute_refund(
        order=order,
        requested_items=requested,
        reason_code=ReturnReasonCode.changed_mind,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=False,
        shipment_lost=False,
        max_refundable_paise=164700,
    )

    # 139800 - 8002 = 131798 / 2 = 65899. 65899 - 6000 fee = 59899
    assert breakdown.items_paise == 65899
    assert breakdown.fee_paise == 6000
    assert breakdown.shipping_refund_paise == 0
    assert breakdown.total_paise == 59899
    assert breakdown.capped is False


def test_ord_2040_request_90000_with_max_50000_gives_50000_capped(
    fixture_orders: dict[str, Order],
    default_policy: PolicyDocument,
) -> None:
    order = fixture_orders["ORD-2040"]
    requested = [ReturnItem(item_id="ITEM-2040-1", qty=2)]
    breakdown = compute_refund(
        order=order,
        requested_items=requested,
        reason_code=ReturnReasonCode.defective,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=True,
        shipment_lost=False,
        max_refundable_paise=50000,
    )

    assert breakdown.items_paise == 90000
    assert breakdown.total_paise == 50000
    assert breakdown.capped is True
    assert breakdown.cap_amount_paise == 50000


# ── 2. Remainder Allocation & Fee Invariants ───────────────────────


def test_odd_line_paid_puts_extra_paisa_on_first_unit(
    default_policy: PolicyDocument,
) -> None:
    # 2 units with odd line_paid of 131799 -> unit 1 has 65900, unit 2 has 65899
    order = Order(
        order_id="ORD-ODD-1",
        customer_id="C001",
        status=OrderStatus.delivered,
        payment_method=PaymentMethod.prepaid,
        payment_status=PaymentStatus.paid,
        subtotal_paise=131799,
        discount_paise=0,
        shipping_fee_paise=0,
        total_paise=131799,
        items=[
            OrderItem(
                item_id="ITEM-ODD-1",
                sku="SKU-ODD",
                name="Odd Item",
                category="apparel",
                qty=2,
                unit_price_paise=65900,
                discount_alloc_paise=1,
                line_paid_paise=131799,
            )
        ],
        placed_at=NOW_UTC,
    )

    # Returning 1 unit takes the first unit (extra paisa)
    breakdown_1 = compute_refund(
        order=order,
        requested_items=[ReturnItem(item_id="ITEM-ODD-1", qty=1)],
        reason_code=ReturnReasonCode.defective,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=False,
        shipment_lost=False,
        max_refundable_paise=131799,
    )
    assert breakdown_1.items_paise == 65900
    assert breakdown_1.total_paise == 65900

    # Returning both units takes 131799
    breakdown_2 = compute_refund(
        order=order,
        requested_items=[ReturnItem(item_id="ITEM-ODD-1", qty=2)],
        reason_code=ReturnReasonCode.defective,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=True,
        shipment_lost=False,
        max_refundable_paise=131799,
    )
    assert breakdown_2.items_paise == 131799
    assert breakdown_2.total_paise == 131799


def test_fee_never_makes_total_negative(default_policy: PolicyDocument) -> None:
    order = Order(
        order_id="ORD-TINY-1",
        customer_id="C001",
        status=OrderStatus.delivered,
        payment_method=PaymentMethod.prepaid,
        payment_status=PaymentStatus.paid,
        subtotal_paise=4000,
        discount_paise=0,
        shipping_fee_paise=0,
        total_paise=4000,
        items=[
            OrderItem(
                item_id="ITEM-TINY-1",
                sku="SKU-TINY",
                name="Tiny Item",
                category="apparel",
                qty=1,
                unit_price_paise=4000,
                line_paid_paise=4000,
            )
        ],
        placed_at=NOW_UTC,
    )

    # 4000 - 6000 fee would be -2000, clamped to 0
    breakdown = compute_refund(
        order=order,
        requested_items=[ReturnItem(item_id="ITEM-TINY-1", qty=1)],
        reason_code=ReturnReasonCode.changed_mind,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=True,
        shipment_lost=False,
        max_refundable_paise=4000,
    )
    assert breakdown.items_paise == 4000
    assert breakdown.fee_paise == 6000
    assert breakdown.total_paise == 0


def test_whole_order_defective_return_refunds_shipping(
    default_policy: PolicyDocument,
) -> None:
    order = Order(
        order_id="ORD-SHIP-1",
        customer_id="C001",
        status=OrderStatus.delivered,
        payment_method=PaymentMethod.prepaid,
        payment_status=PaymentStatus.paid,
        subtotal_paise=50000,
        discount_paise=0,
        shipping_fee_paise=7500,
        total_paise=57500,
        items=[
            OrderItem(
                item_id="ITEM-SHIP-1",
                sku="SKU-SHIP",
                name="Shipped Item",
                category="electronics",
                qty=1,
                unit_price_paise=50000,
                line_paid_paise=50000,
            )
        ],
        placed_at=NOW_UTC,
    )

    # Whole order defective refunds shipping fee
    breakdown = compute_refund(
        order=order,
        requested_items=[ReturnItem(item_id="ITEM-SHIP-1", qty=1)],
        reason_code=ReturnReasonCode.defective,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=True,
        shipment_lost=False,
        max_refundable_paise=57500,
    )
    assert breakdown.shipping_refund_paise == 7500
    assert breakdown.fee_paise == 0
    assert breakdown.total_paise == 57500


def test_partial_defective_return_does_not_refund_shipping(
    default_policy: PolicyDocument,
) -> None:
    order = Order(
        order_id="ORD-SHIP-2",
        customer_id="C001",
        status=OrderStatus.delivered,
        payment_method=PaymentMethod.prepaid,
        payment_status=PaymentStatus.paid,
        subtotal_paise=100000,
        discount_paise=0,
        shipping_fee_paise=7500,
        total_paise=107500,
        items=[
            OrderItem(
                item_id="ITEM-SHIP-A",
                sku="SKU-A",
                name="Item A",
                category="electronics",
                qty=1,
                unit_price_paise=50000,
                line_paid_paise=50000,
            ),
            OrderItem(
                item_id="ITEM-SHIP-B",
                sku="SKU-B",
                name="Item B",
                category="electronics",
                qty=1,
                unit_price_paise=50000,
                line_paid_paise=50000,
            ),
        ],
        placed_at=NOW_UTC,
    )

    # Partial return does not refund shipping even if defective
    breakdown = compute_refund(
        order=order,
        requested_items=[ReturnItem(item_id="ITEM-SHIP-A", qty=1)],
        reason_code=ReturnReasonCode.defective,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=False,
        shipment_lost=False,
        max_refundable_paise=107500,
    )
    assert breakdown.shipping_refund_paise == 0
    assert breakdown.total_paise == 50000


def test_shipment_lost_refunds_shipping(default_policy: PolicyDocument) -> None:
    order = Order(
        order_id="ORD-LOST-1",
        customer_id="C001",
        status=OrderStatus.shipped,
        payment_method=PaymentMethod.prepaid,
        payment_status=PaymentStatus.paid,
        subtotal_paise=50000,
        discount_paise=0,
        shipping_fee_paise=5000,
        total_paise=55000,
        items=[
            OrderItem(
                item_id="ITEM-LOST-1",
                sku="SKU-LOST",
                name="Lost Item",
                category="electronics",
                qty=1,
                unit_price_paise=50000,
                line_paid_paise=50000,
            )
        ],
        placed_at=NOW_UTC,
    )

    breakdown = compute_refund(
        order=order,
        requested_items=[ReturnItem(item_id="ITEM-LOST-1", qty=1)],
        reason_code=ReturnReasonCode.defective,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=True,
        shipment_lost=True,
        max_refundable_paise=55000,
    )
    assert breakdown.shipping_refund_paise == 5000
    assert breakdown.total_paise == 55000


def test_store_credit_resolution_charges_no_fee(default_policy: PolicyDocument) -> None:
    order = Order(
        order_id="ORD-CREDIT-1",
        customer_id="C001",
        status=OrderStatus.delivered,
        payment_method=PaymentMethod.prepaid,
        payment_status=PaymentStatus.paid,
        subtotal_paise=50000,
        discount_paise=0,
        shipping_fee_paise=0,
        total_paise=50000,
        items=[
            OrderItem(
                item_id="ITEM-CREDIT-1",
                sku="SKU-CREDIT",
                name="Credit Item",
                category="apparel",
                qty=1,
                unit_price_paise=50000,
                line_paid_paise=50000,
            )
        ],
        placed_at=NOW_UTC,
    )

    # Changed mind with resolution store_credit does not charge fee
    breakdown = compute_refund(
        order=order,
        requested_items=[ReturnItem(item_id="ITEM-CREDIT-1", qty=1)],
        reason_code=ReturnReasonCode.changed_mind,
        resolution=ReturnResolution.store_credit,
        policy=default_policy,
        whole_order_returned=True,
        shipment_lost=False,
        max_refundable_paise=50000,
    )
    assert breakdown.fee_paise == 0
    assert breakdown.total_paise == 50000


# ── 3. Hypothesis Property Tests ────────────────────────────────────


@given(
    qty_ordered=st.integers(min_value=1, max_value=10),
    line_paid=st.integers(min_value=0, max_value=500000),
    qty_returned=st.integers(min_value=1, max_value=10),
    shipping_fee=st.integers(min_value=0, max_value=20000),
    max_refundable=st.integers(min_value=0, max_value=1000000),
    whole_order=st.booleans(),
    reason_code=st.sampled_from(list(ReturnReasonCode)),
    resolution=st.sampled_from(list(ReturnResolution)),
)
@settings(max_examples=100)
def test_hypothesis_refund_properties(
    qty_ordered: int,
    line_paid: int,
    qty_returned: int,
    shipping_fee: int,
    max_refundable: int,
    whole_order: bool,
    reason_code: ReturnReasonCode,
    resolution: ReturnResolution,
) -> None:
    policy = load_policy("v3")
    # Clamp qty_returned to not exceed qty_ordered
    actual_qty_ret = min(qty_returned, qty_ordered)

    order = Order(
        order_id="ORD-PROP",
        customer_id="C001",
        status=OrderStatus.delivered,
        payment_method=PaymentMethod.prepaid,
        payment_status=PaymentStatus.paid,
        subtotal_paise=line_paid,
        discount_paise=0,
        shipping_fee_paise=shipping_fee,
        total_paise=line_paid + shipping_fee,
        items=[
            OrderItem(
                item_id="ITEM-PROP",
                sku="SKU-PROP",
                name="Prop Item",
                category="apparel",
                qty=qty_ordered,
                unit_price_paise=line_paid // qty_ordered,
                line_paid_paise=line_paid,
            )
        ],
        placed_at=NOW_UTC,
    )

    breakdown = compute_refund(
        order=order,
        requested_items=[ReturnItem(item_id="ITEM-PROP", qty=actual_qty_ret)],
        reason_code=reason_code,
        resolution=resolution,
        policy=policy,
        whole_order_returned=whole_order,
        shipment_lost=False,
        max_refundable_paise=max_refundable,
    )

    # 1. Total is always an int >= 0
    assert isinstance(breakdown.total_paise, int)
    assert breakdown.total_paise >= 0

    # 2. Total never exceeds line_paid sum plus shipping
    assert breakdown.total_paise <= line_paid + shipping_fee

    # 3. Total never exceeds max_refundable
    assert breakdown.total_paise <= max_refundable

    # 4. Capped flag is consistent with cap_amount_paise
    if breakdown.capped:
        assert breakdown.cap_amount_paise == max_refundable
        assert breakdown.total_paise == max_refundable


# ── 4. Edge Cases & Boundary Handling ──────────────────────────────


def test_oversized_quantity_rejected(default_policy: PolicyDocument) -> None:
    order = Order(
        order_id="ORD-OVER-1",
        customer_id="C001",
        status=OrderStatus.delivered,
        payment_method=PaymentMethod.prepaid,
        payment_status=PaymentStatus.paid,
        subtotal_paise=10000,
        total_paise=10000,
        items=[
            OrderItem(
                item_id="ITEM-1",
                sku="SKU-1",
                name="Item",
                qty=2,
                unit_price_paise=5000,
                line_paid_paise=10000,
            )
        ],
        placed_at=NOW_UTC,
    )

    with pytest.raises(ValueError, match="exceeds ordered quantity"):
        compute_refund(
            order=order,
            requested_items=[ReturnItem(item_id="ITEM-1", qty=3)],
            reason_code=ReturnReasonCode.defective,
            resolution=ReturnResolution.refund,
            policy=default_policy,
            whole_order_returned=True,
            shipment_lost=False,
            max_refundable_paise=10000,
        )


def test_item_from_different_order_rejected(default_policy: PolicyDocument) -> None:
    order = Order(
        order_id="ORD-DIFF-1",
        customer_id="C001",
        status=OrderStatus.delivered,
        payment_method=PaymentMethod.prepaid,
        payment_status=PaymentStatus.paid,
        subtotal_paise=10000,
        total_paise=10000,
        items=[
            OrderItem(
                item_id="ITEM-1",
                sku="SKU-1",
                name="Item",
                qty=1,
                unit_price_paise=10000,
                line_paid_paise=10000,
            )
        ],
        placed_at=NOW_UTC,
    )

    with pytest.raises(ValueError, match="not found in order"):
        compute_refund(
            order=order,
            requested_items=[ReturnItem(item_id="ITEM-FOREIGN", qty=1)],
            reason_code=ReturnReasonCode.defective,
            resolution=ReturnResolution.refund,
            policy=default_policy,
            whole_order_returned=True,
            shipment_lost=False,
            max_refundable_paise=10000,
        )


def test_zero_priced_items_handled(default_policy: PolicyDocument) -> None:
    order = Order(
        order_id="ORD-ZERO-1",
        customer_id="C001",
        status=OrderStatus.delivered,
        payment_method=PaymentMethod.prepaid,
        payment_status=PaymentStatus.paid,
        subtotal_paise=0,
        total_paise=0,
        items=[
            OrderItem(
                item_id="ITEM-ZERO",
                sku="SKU-ZERO",
                name="Free Promo Item",
                qty=2,
                unit_price_paise=0,
                line_paid_paise=0,
            )
        ],
        placed_at=NOW_UTC,
    )

    breakdown = compute_refund(
        order=order,
        requested_items=[ReturnItem(item_id="ITEM-ZERO", qty=1)],
        reason_code=ReturnReasonCode.defective,
        resolution=ReturnResolution.refund,
        policy=default_policy,
        whole_order_returned=True,
        shipment_lost=False,
        max_refundable_paise=0,
    )
    assert breakdown.items_paise == 0
    assert breakdown.total_paise == 0


# ── 5. Policy Versions & Loader Validation ─────────────────────────


def test_v1_v2_and_v3_differ_only_in_auto_limit_paise() -> None:
    v1 = load_policy("v1")
    v2 = load_policy("v2")
    v3 = load_policy("v3")

    assert v1.auto_limit_paise == 100000
    assert v2.auto_limit_paise == 75000
    assert v3.auto_limit_paise == 50000

    d1 = v1.model_dump()
    d2 = v2.model_dump()
    d3 = v3.model_dump()

    # Remove version and auto_limit_paise
    for d in (d1, d2, d3):
        d.pop("version")
        d.pop("auto_limit_paise")

    assert d1 == d2
    assert d2 == d3


def test_loader_rejects_v3_file_with_extra_field() -> None:
    v3 = load_policy("v3")
    data = v3.model_dump()
    data["unauthorized_backdoor"] = True

    with pytest.raises(ValidationError):
        load_policy_from_dict(data)


def test_loader_rejects_negative_values() -> None:
    v3 = load_policy("v3")
    data = v3.model_dump()
    data["auto_limit_paise"] = -500

    with pytest.raises(ValidationError):
        load_policy_from_dict(data)


def test_loader_picks_active_version_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v1")
    policy = load_policy()
    assert policy.version == "v1"
    assert policy.auto_limit_paise == 100000


# ── 6. Customer-Safe Summary (KP-23 & Amendment 6) ──────────────────


FORBIDDEN_WORDS = [
    "limit",
    "threshold",
    "risk",
    "approval",
    "score",
    "staff",
    "owner",
    "auto",
]


def test_summary_has_no_forbidden_words(default_policy: PolicyDocument) -> None:
    summary_text = str(customer_safe_summary(default_policy)).lower()

    for forbidden in FORBIDDEN_WORDS:
        assert forbidden not in summary_text, f"Forbidden word '{forbidden}' leaked in summary"


def test_summary_contains_required_sections_only(default_policy: PolicyDocument) -> None:
    summary = customer_safe_summary(default_policy)
    text = str(summary).lower()

    # Required: return windows, return shipping, refund timing
    assert "apparel" in text or "days" in text
    assert "shipping" in text or "fee" in text or "pickup" in text
    assert "business days" in text or "refund" in text
