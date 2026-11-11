import asyncio
import base64
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

import db
from app.audit import log_audit
from app.clock import Clock
from app.deps import require_auth
from app.errors import AppException, NotFoundException, UpstreamUnavailableException
from app.faults import is_fault_enabled
from app.models import Order, OrderItem, Product, Refund, ReturnItem, Shipment
from app.schemas import (
    OrderItemResponse,
    OrderListResponse,
    OrderResponse,
    ProductResponse,
    RefundableItemResponse,
    RefundableResponse,
    RefundResponse,
    ShipmentEventResponse,
    ShipmentResponse,
)

router = APIRouter(prefix="/api/v1", tags=["Orders"])


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _check_order_ownership_or_deny(
    order: Optional[Order],
    order_id: str,
    principal: dict[str, Any],
    session: Session,
    request: Request,
) -> Order:
    """
    Enforce uniform 404: if the order does not exist OR is not owned by the customer,
    return the exact same 404 body. If access is denied, record a row in store_audit.
    Seller tokens and service tokens with orders:read_all can view any order.
    """
    request_id = (
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID", "unknown")
    )
    actor = principal.get("sub") or principal.get("customer_id") or "anonymous"

    if order is None:
        raise NotFoundException(message="Order not found")

    # Sellers and agent-monitor (orders:read_all) can read any order
    is_seller = (
        principal.get("type") == "user"
        and principal.get("role") in ("seller_owner", "seller_staff")
    )
    is_monitor = (
        principal.get("type") == "service"
        and "orders:read_all" in principal.get("scopes", [])
    )

    if is_seller or is_monitor:
        return order

    # Otherwise, caller must be a customer and own the order
    customer_id = principal.get("customer_id")
    if principal.get("role") != "customer" or order.customer_id != customer_id:
        log_audit(
            session=session,
            request_id=request_id,
            actor=actor,
            action="read",
            resource=f"orders/{order_id}",
            outcome="denied",
        )
        raise NotFoundException(message="Order not found")

    return order


# Public Products Endpoint
@router.get(
    "/products",
    response_model=list[ProductResponse],
    summary="List products",
    tags=["Products"],
)
def list_products() -> list[ProductResponse]:
    with Session(db.engine) as session:
        products = session.scalars(select(Product).order_by(Product.sku.asc())).all()
        return [ProductResponse.model_validate(p) for p in products]


# Customer Orders List
@router.get(
    "/orders",
    response_model=OrderListResponse,
    summary="List customer orders",
)
def list_orders(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    cursor: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    principal: dict[str, Any] = Depends(require_auth),
) -> OrderListResponse:
    # Own orders only: identity strictly from token
    if principal.get("role") != "customer" or not principal.get("customer_id"):
        request_id = (
            getattr(request.state, "request_id", None)
            or request.headers.get("X-Request-ID", "unknown")
        )
        actor = principal.get("sub") or "unknown"
        with Session(db.engine) as session:
            log_audit(
                session=session,
                request_id=request_id,
                actor=actor,
                action="read",
                resource=request.url.path,
                outcome="denied",
            )
        raise AppException(
            code="forbidden",
            message="Customer role required",
            status_code=403,
        )

    customer_id = principal["customer_id"]

    with Session(db.engine) as session:
        query = (
            select(Order)
            .options(joinedload(Order.items))
            .where(Order.customer_id == customer_id)
        )

        if status:
            query = query.where(Order.status == status)

        if search:
            search_pattern = f"%{search.strip()}%"
            query = query.where(
                or_(
                    Order.order_id.ilike(search_pattern),
                    Order.items.any(OrderItem.name_snapshot.ilike(search_pattern)),
                    Order.items.any(OrderItem.sku.ilike(search_pattern)),
                )
            )

        # Cursor pagination: ordered by placed_at desc, order_id desc
        if cursor:
            try:
                decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
                cursor_dt_str, cursor_order_id = decoded.split("|", 1)
                cursor_dt = datetime.fromisoformat(cursor_dt_str)
                query = query.where(
                    or_(
                        Order.placed_at < cursor_dt,
                        (Order.placed_at == cursor_dt) & (Order.order_id < cursor_order_id),
                    )
                )
            except Exception:
                raise AppException(
                    code="validation_error",
                    message="Invalid pagination cursor",
                    status_code=400,
                )

        query = query.order_by(Order.placed_at.desc(), Order.order_id.desc()).limit(limit + 1)
        orders = session.scalars(query).unique().all()

        has_more = len(orders) > limit
        if has_more:
            visible_orders = orders[:limit]
            last_order = visible_orders[-1]
            next_cursor = base64.urlsafe_b64encode(
                f"{last_order.placed_at.isoformat()}|{last_order.order_id}".encode()
            ).decode()
        else:
            visible_orders = orders
            next_cursor = None

        items_resp = [
            OrderResponse(
                order_id=o.order_id,
                customer_id=o.customer_id,
                status=o.status,
                payment_method=o.payment_method,
                payment_status=o.payment_status,
                subtotal_paise=o.subtotal_paise,
                discount_paise=o.discount_paise,
                shipping_fee_paise=o.shipping_fee_paise,
                total_paise=o.total_paise,
                placed_at=o.placed_at,
                shipped_at=o.shipped_at,
                delivered_at=o.delivered_at,
                cancelled_at=o.cancelled_at,
                ship_to_json=o.ship_to_json,
                version=o.version,
                items=[OrderItemResponse.model_validate(it) for it in o.items],
            )
            for o in visible_orders
        ]

        return OrderListResponse(items=items_resp, next_cursor=next_cursor, has_more=has_more)


# Single Order Detail
@router.get(
    "/orders/{order_id}",
    response_model=OrderResponse,
    summary="Get order details",
)
def get_order(
    order_id: str,
    request: Request,
    principal: dict[str, Any] = Depends(require_auth),
) -> OrderResponse:
    with Session(db.engine) as session:
        order = (
            session.query(Order)
            .options(joinedload(Order.items))
            .filter(Order.order_id == order_id)
            .one_or_none()
        )
        order = _check_order_ownership_or_deny(order, order_id, principal, session, request)

        return OrderResponse(
            order_id=order.order_id,
            customer_id=order.customer_id,
            status=order.status,
            payment_method=order.payment_method,
            payment_status=order.payment_status,
            subtotal_paise=order.subtotal_paise,
            discount_paise=order.discount_paise,
            shipping_fee_paise=order.shipping_fee_paise,
            total_paise=order.total_paise,
            placed_at=order.placed_at,
            shipped_at=order.shipped_at,
            delivered_at=order.delivered_at,
            cancelled_at=order.cancelled_at,
            ship_to_json=order.ship_to_json,
            version=order.version,
            items=[OrderItemResponse.model_validate(it) for it in order.items],
        )


# Order Shipment Detail with derived delayed flag
@router.get(
    "/orders/{order_id}/shipment",
    response_model=ShipmentResponse,
    summary="Get order shipment details",
)
async def get_order_shipment(
    order_id: str,
    request: Request,
    principal: dict[str, Any] = Depends(require_auth),
) -> ShipmentResponse:
    if is_fault_enabled("carrier.down"):
        raise UpstreamUnavailableException(message="Carrier service unavailable (carrier.down)")
    if is_fault_enabled("carrier.slow"):
        await asyncio.sleep(8)

    with Session(db.engine) as session:
        order = session.get(Order, order_id)
        _check_order_ownership_or_deny(order, order_id, principal, session, request)

        shipment = (
            session.query(Shipment)
            .options(joinedload(Shipment.events))
            .filter(Shipment.order_id == order_id)
            .one_or_none()
        )
        if not shipment:
            raise NotFoundException(message="Shipment not found")

        # Derived delayed flag: eta earlier than Clock.now and not delivered
        now = Clock.now()
        eta_utc = _to_utc(shipment.eta_at)
        is_delayed = bool(
            shipment.status != "delivered"
            and eta_utc is not None
            and eta_utc < now
        )

        sorted_events = sorted(shipment.events, key=lambda ev: ev.ts)

        # Fault: carrier.stale returns stale tracking (no new events after first)
        if is_fault_enabled("carrier.stale", session=session) and len(sorted_events) > 1:
            sorted_events = sorted_events[:1]
            status = sorted_events[0].status
            last_event_at = sorted_events[0].ts
        else:
            status = shipment.status
            last_event_at = shipment.last_event_at

        return ShipmentResponse(
            shipment_id=shipment.shipment_id,
            order_id=shipment.order_id,
            carrier=shipment.carrier,
            tracking_no=shipment.tracking_no,
            status=status,
            eta_at=shipment.eta_at,
            last_event_at=last_event_at,
            script_id=shipment.script_id,
            delayed=is_delayed,
            events=[ShipmentEventResponse.model_validate(ev) for ev in sorted_events],
        )


# Refundable amounts check
@router.get(
    "/orders/{order_id}/refundable",
    response_model=RefundableResponse,
    summary="Get refundable amount calculation",
)
def get_order_refundable(
    order_id: str,
    request: Request,
    items: Optional[str] = Query(
        default=None,
        description="Comma-separated ITEM_ID:QTY pairs, e.g. ITEM-1048-1:1,ITEM-1048-2:2",
    ),
    principal: dict[str, Any] = Depends(require_auth),
) -> RefundableResponse:
    with Session(db.engine) as session:
        order = (
            session.query(Order)
            .options(joinedload(Order.items))
            .filter(Order.order_id == order_id)
            .one_or_none()
        )
        _check_order_ownership_or_deny(order, order_id, principal, session, request)

        # 1. Total active refunds on this order (reserved, processing, completed)
        active_refunds = session.scalars(
            select(Refund).where(
                Refund.order_id == order_id,
                Refund.status.in_(["reserved", "processing", "completed"]),
            )
        ).all()
        total_active_refunds_paise = sum(r.amount_paise for r in active_refunds)
        order_remaining_refundable_paise = max(0, order.total_paise - total_active_refunds_paise)

        # 2. For each item in order, calculate remaining quantity net of active/completed returns
        order_items_map = {it.item_id: it for it in order.items}
        return_items = session.scalars(
            select(ReturnItem).where(
                ReturnItem.item_id.in_(list(order_items_map.keys())),
                ReturnItem.status.not_in(["cancelled", "rejected"]),
            )
        ).all()
        returned_qty_by_item: dict[str, int] = {}
        for ri in return_items:
            returned_qty_by_item[ri.item_id] = returned_qty_by_item.get(ri.item_id, 0) + ri.qty

        remaining_qty_by_item: dict[str, int] = {}
        for it in order.items:
            ret_qty = returned_qty_by_item.get(it.item_id, 0)
            remaining_qty_by_item[it.item_id] = max(0, it.qty - ret_qty)

        # 3. Parse requested items
        requested_qty_by_item: dict[str, int] = {}
        if items:
            for part in items.split(","):
                part = part.strip()
                if not part:
                    continue
                if ":" not in part:
                    raise AppException(
                        code="validation_error",
                        message=f"Invalid item format '{part}'. Expected ITEM_ID:QTY",
                        status_code=400,
                    )
                item_id, qty_str = part.split(":", 1)
                item_id = item_id.strip()
                try:
                    qty = int(qty_str.strip())
                except ValueError:
                    raise AppException(
                        code="validation_error",
                        message=f"Invalid quantity '{qty_str}' for item '{item_id}'",
                        status_code=400,
                    )
                if qty <= 0:
                    raise AppException(
                        code="validation_error",
                        message=f"Quantity for item '{item_id}' must be positive",
                        status_code=400,
                    )
                if item_id not in order_items_map:
                    raise AppException(
                        code="validation_error",
                        message=f"Item '{item_id}' does not belong to order '{order_id}'",
                        status_code=400,
                    )
                if qty > remaining_qty_by_item[item_id]:
                    raise AppException(
                        code="validation_error",
                        message=(
                            f"Requested quantity {qty} for item '{item_id}' exceeds "
                            f"remaining refundable quantity {remaining_qty_by_item[item_id]}"
                        ),
                        status_code=400,
                    )
                requested_qty_by_item[item_id] = qty
        else:
            # If no items parameter, evaluate all items with their remaining quantity
            for it in order.items:
                rem = remaining_qty_by_item[it.item_id]
                if rem > 0:
                    requested_qty_by_item[it.item_id] = rem

        # 4. Calculate refundable values based on line_paid_paise
        items_result: list[RefundableItemResponse] = []
        gross_requested_paise = 0

        # We first evaluate each requested item
        item_evals = []
        for it in order.items:
            if it.item_id not in requested_qty_by_item:
                continue
            req_qty = requested_qty_by_item[it.item_id]
            rem_qty = remaining_qty_by_item[it.item_id]
            if it.qty == 0:
                item_line_paid = 0
            elif req_qty == it.qty:
                item_line_paid = it.line_paid_paise
            else:
                item_line_paid = (it.line_paid_paise * req_qty) // it.qty

            gross_requested_paise += item_line_paid
            item_evals.append((it, req_qty, rem_qty, item_line_paid))

        # Overall max refundable is capped by order remaining refundable paise
        overall_max_refundable_paise = min(gross_requested_paise, order_remaining_refundable_paise)

        # Distribute / assign per-item refundable capped by order maximum
        for it, req_qty, rem_qty, item_line_paid in item_evals:
            item_max_paise = min(item_line_paid, overall_max_refundable_paise)
            items_result.append(
                RefundableItemResponse(
                    item_id=it.item_id,
                    sku=it.sku,
                    name=it.name_snapshot,
                    requested_qty=req_qty,
                    remaining_qty=rem_qty,
                    unit_price_paise=it.unit_price_paise,
                    line_paid_paise=it.line_paid_paise,
                    max_refundable_paise=item_max_paise,
                )
            )

        return RefundableResponse(
            order_id=order_id,
            items=items_result,
            max_refundable_paise=overall_max_refundable_paise,
        )


# Order Refunds List
@router.get(
    "/orders/{order_id}/refunds",
    response_model=list[RefundResponse],
    summary="Get refunds for an order",
)
def get_order_refunds(
    order_id: str,
    request: Request,
    principal: dict[str, Any] = Depends(require_auth),
) -> list[RefundResponse]:
    with Session(db.engine) as session:
        order = session.get(Order, order_id)
        _check_order_ownership_or_deny(order, order_id, principal, session, request)

        refunds = session.scalars(
            select(Refund)
            .where(Refund.order_id == order_id)
            .order_by(Refund.created_at.desc())
        ).all()

        return [RefundResponse.model_validate(r) for r in refunds]
