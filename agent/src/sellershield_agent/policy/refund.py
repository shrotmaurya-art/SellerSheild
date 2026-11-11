"""Pure refund calculator implementing Spec §7.4 integer paise rules.

Invariants:
- All money in integer paise (KP-05: zero float, no round(), no Decimal division).
- Remainder paise allocated to the first unit of each returned item line.
- Original shipping fee refunded only on whole-order seller-fault returns or lost shipments.
- Return shipping fee deducted only when reason is not in free_reasons and resolution is refund.
- Total never negative; capped by max_refundable_paise.
- Pure deterministic function: no LLM, no I/O, no Clock reading.
"""

from __future__ import annotations

from collections.abc import Sequence

from sellershield_agent.domain.models import (
    Order,
    OrderItem,
    ReturnItem,
    ReturnReasonCode,
    ReturnResolution,
)
from sellershield_agent.policy.models import ItemAmounts, PolicyDocument, RefundBreakdown


def compute_refund(
    order: Order,
    requested_items: Sequence[ReturnItem],
    reason_code: ReturnReasonCode | str,
    resolution: ReturnResolution | str,
    policy: PolicyDocument,
    whole_order_returned: bool,
    shipment_lost: bool,
    max_refundable_paise: int | None = None,
) -> RefundBreakdown:
    """Calculate exact integer paise refund breakdown for a customer return or claim.

    Raises :class:`ValueError` on invalid items, negative quantities, or requested
    quantities exceeding ordered quantities.
    """
    if isinstance(reason_code, ReturnReasonCode):
        reason_str = reason_code.value
    else:
        reason_str = str(reason_code)

    if isinstance(resolution, ReturnResolution):
        resolution_str = resolution.value
    else:
        resolution_str = str(resolution)

    order_items_by_id: dict[str, OrderItem] = {item.item_id: item for item in order.items}

    item_amounts: dict[str, int] = {}
    for req in requested_items:
        if req.item_id not in order_items_by_id:
            raise ValueError(
                f"Requested item {req.item_id!r} not found in order {order.order_id!r}"
            )

        order_item = order_items_by_id[req.item_id]
        if req.qty < 0:
            raise ValueError(
                f"Requested quantity cannot be negative for item {req.item_id!r}: {req.qty}"
            )
        if req.qty > order_item.qty:
            raise ValueError(
                f"Requested quantity {req.qty} exceeds ordered quantity "
                f"{order_item.qty} for item {req.item_id!r}"
            )

        if req.qty == 0:
            item_amounts[req.item_id] = 0
            continue

        line_paid = order_item.line_paid_paise
        base_per_unit = line_paid // order_item.qty
        remainder_paise = line_paid % order_item.qty

        # Spec §7.4: per_unit = line_paid ÷ qty ordered (remainder paise go to the first unit)
        # Unit 1 gets base_per_unit + remainder_paise; subsequent units get base_per_unit
        amount = (base_per_unit * req.qty) + remainder_paise
        item_amounts[req.item_id] = amount

    items_paise = sum(item_amounts.values())

    # Shipping refund: original shipping fee only for whole-order seller-fault or lost shipment
    is_seller_fault = reason_str in policy.return_shipping.free_reasons
    if shipment_lost or (whole_order_returned and is_seller_fault):
        shipping_refund_paise = order.shipping_fee_paise
    else:
        shipping_refund_paise = 0

    # Return shipping fee: deducted when reason is not free and resolution is refund
    if (not is_seller_fault) and resolution_str == ReturnResolution.refund.value:
        fee_paise = policy.return_shipping.customer_fee_paise
    else:
        fee_paise = 0

    # Total never below zero
    raw_total = items_paise + shipping_refund_paise - fee_paise
    uncapped_total = max(0, raw_total)

    # Cap by max_refundable_paise if specified
    if max_refundable_paise is not None and uncapped_total > max_refundable_paise:
        total_paise = max_refundable_paise
        capped = True
        cap_amount_paise = max_refundable_paise
    else:
        total_paise = uncapped_total
        capped = False
        cap_amount_paise = None

    return RefundBreakdown(
        item_amounts=ItemAmounts(item_amounts),
        items_paise=items_paise,
        shipping_refund_paise=shipping_refund_paise,
        fee_paise=fee_paise,
        total_paise=total_paise,
        capped=capped,
        cap_amount_paise=cap_amount_paise,
    )
