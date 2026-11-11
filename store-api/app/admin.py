import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

import db
from app.audit import log_audit
from app.clock import Clock
from app.deps import require_owner_or_monitor, require_role, require_seller_or_monitor
from app.errors import (
    AppException,
    InvalidStateException,
    NotFoundException,
    ValidationErrorException,
)
from app.idempotency import (
    check_idempotency_replay,
    compute_payload_hash,
    require_idempotency_key,
    save_idempotency_record,
)
from app.models import Customer, Order, OrderItem, Refund, Return, StoreSettings
from app.outbox import emit_outbox_event
from app.schemas import (
    AdminOrderDetailResponse,
    CustomerAdminSummaryResponse,
    OrderItemResponse,
    OrderListResponse,
    OrderResponse,
    RefundResponse,
    ReturnInspectRequest,
    ReturnResponse,
    StoreSettingsResponse,
    StoreSettingsUpdateRequest,
)

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


@router.get(
    "/orders",
    response_model=OrderListResponse,
    summary="List all store orders (seller view)",
)
def admin_list_orders(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = None,
    status: Optional[str] = None,
    customer_id: Optional[str] = None,
    search: Optional[str] = None,
    _: dict[str, Any] = Depends(require_seller_or_monitor),
) -> OrderListResponse:
    with Session(db.engine) as session:
        query = select(Order).options(joinedload(Order.items))

        if status:
            query = query.where(Order.status == status)

        if customer_id:
            query = query.where(Order.customer_id == customer_id)

        if search:
            search_pattern = f"%{search.strip()}%"
            query = query.where(
                or_(
                    Order.order_id.ilike(search_pattern),
                    Order.customer_id.ilike(search_pattern),
                    Order.items.any(OrderItem.name_snapshot.ilike(search_pattern)),
                    Order.items.any(OrderItem.sku.ilike(search_pattern)),
                )
            )

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


@router.get(
    "/orders/{order_id}",
    response_model=AdminOrderDetailResponse,
    summary="Get any store order (seller view)",
)
def admin_get_order(
    order_id: str,
    _: dict[str, Any] = Depends(require_seller_or_monitor),
) -> AdminOrderDetailResponse:
    with Session(db.engine) as session:
        order = (
            session.scalars(
                select(Order)
                .options(
                    joinedload(Order.items),
                    joinedload(Order.returns).joinedload(Return.items),
                    joinedload(Order.refunds),
                )
                .where(Order.order_id == order_id)
            )
            .unique()
            .one_or_none()
        )
        if not order:
            raise NotFoundException(message="Order not found")

        return AdminOrderDetailResponse(
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
            returns=[ReturnResponse.model_validate(r) for r in order.returns],
            refunds=[RefundResponse.model_validate(rf) for rf in order.refunds],
        )


@router.get(
    "/customers/{customer_id}",
    response_model=CustomerAdminSummaryResponse,
    summary="Get customer summary (seller view)",
)
def admin_get_customer_summary(
    customer_id: str,
    _: dict[str, Any] = Depends(require_seller_or_monitor),
) -> CustomerAdminSummaryResponse:
    with Session(db.engine) as session:
        customer = session.get(Customer, customer_id)
        if not customer:
            raise NotFoundException(message="Customer not found")

        now = Clock.now()
        joined_utc = (
            customer.joined_at.replace(tzinfo=timezone.utc)
            if customer.joined_at.tzinfo is None
            else customer.joined_at.astimezone(timezone.utc)
        )
        account_age_days = (now - joined_utc).days
        account_age_str = f"{account_age_days} days"

        # SQLite stores naive UTC datetimes
        now_naive = now.replace(tzinfo=None)
        thirty_days_ago = now_naive - timedelta(days=30)
        returns_30d = (
            session.scalar(
                select(func.count(Return.return_id)).where(
                    Return.customer_id == customer_id,
                    Return.status == "completed",
                    Return.created_at >= thirty_days_ago,
                )
            )
            or 0
        )

        # refund ratio 90 days: refunded paise / ordered paise in last 90 days
        ninety_days_ago = now_naive - timedelta(days=90)
        refunds_90d_paise = (
            session.scalar(
                select(func.coalesce(func.sum(Refund.amount_paise), 0)).where(
                    Refund.customer_id == customer_id,
                    Refund.status == "completed",
                    Refund.created_at >= ninety_days_ago,
                )
            )
            or 0
        )
        orders_90d_paise = (
            session.scalar(
                select(func.coalesce(func.sum(Order.total_paise), 0)).where(
                    Order.customer_id == customer_id,
                    Order.placed_at >= ninety_days_ago,
                )
            )
            or 0
        )
        refund_ratio_90d = (
            round(float(refunds_90d_paise) / float(orders_90d_paise), 4)
            if orders_90d_paise > 0
            else 0.0
        )

        total_orders = (
            session.scalar(
                select(func.count(Order.order_id)).where(Order.customer_id == customer_id)
            )
            or 0
        )
        total_spend_paise = (
            session.scalar(
                select(func.coalesce(func.sum(Order.total_paise), 0)).where(
                    Order.customer_id == customer_id
                )
            )
            or 0
        )
        total_refunded_paise = (
            session.scalar(
                select(func.coalesce(func.sum(Refund.amount_paise), 0)).where(
                    Refund.customer_id == customer_id,
                    Refund.status == "completed",
                )
            )
            or 0
        )

        return CustomerAdminSummaryResponse(
            customer_id=customer.customer_id,
            full_name=customer.full_name,
            joined_at=customer.joined_at,
            account_age=account_age_str,
            account_age_days=account_age_days,
            returns_30d=returns_30d,
            refund_ratio_90d=refund_ratio_90d,
            total_orders=total_orders,
            total_spend_paise=total_spend_paise,
            total_refunded_paise=total_refunded_paise,
        )


@router.post(
    "/returns/{return_id}/receive",
    response_model=ReturnResponse,
    summary="Mark returned item received at warehouse (seller only)",
)
async def admin_receive_return(
    return_id: str,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_role("seller_owner", "seller_staff")),
) -> Any:
    """
    Seller receives return at warehouse.
    Transitions return from in_transit_back to received.
    """
    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    req_id = getattr(request.state, "request_id", None) or request.headers.get(
        "X-Request-ID", "unknown"
    )
    actor = principal.get("sub") or "seller"

    with Session(db.engine) as session:
        cached = check_idempotency_replay(session, idempotency_key, payload_hash)
        if cached is not None:
            status_code, resp_data = cached
            return JSONResponse(status_code=status_code, content=resp_data)

        ret = session.scalar(
            select(Return).options(joinedload(Return.items)).where(Return.return_id == return_id)
        )
        if ret is None:
            raise NotFoundException(message="Return not found")

        if ret.status != "in_transit_back":
            raise InvalidStateException(
                message=(
                    f"Cannot receive return in state " f"'{ret.status}'; must be 'in_transit_back'"
                )
            )

        now = Clock.now()
        ret.status = "received"
        ret.updated_at = now
        ret.version += 1

        for item in ret.items:
            item.status = "received"

        log_audit(
            session=session,
            request_id=req_id,
            actor=actor,
            action="return_receive",
            resource=f"returns/{return_id}",
            outcome="received",
        )

        emit_outbox_event(
            session=session,
            event_type="return.updated",
            payload={
                "return_id": ret.return_id,
                "order_id": ret.order_id,
                "customer_id": ret.customer_id,
                "status": ret.status,
            },
            resource_version=ret.version,
        )

        resp_dict = ReturnResponse.model_validate(ret).model_dump(mode="json")
        save_idempotency_record(
            session=session,
            key=idempotency_key,
            payload_hash=payload_hash,
            status_code=status.HTTP_200_OK,
            response_data=resp_dict,
            client_id=actor,
        )
        session.commit()
        session.refresh(ret)
        return ReturnResponse.model_validate(ret)


@router.post(
    "/returns/{return_id}/inspect",
    response_model=ReturnResponse,
    summary="Record inspection result for return (seller only)",
)
async def admin_inspect_return(
    return_id: str,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_role("seller_owner", "seller_staff")),
) -> Any:
    """
    Seller records inspection result for received return.
    Transitions return from received to inspected_ok / inspection_failed.
    """
    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    req_id = getattr(request.state, "request_id", None) or request.headers.get(
        "X-Request-ID", "unknown"
    )
    actor = principal.get("sub") or "seller"

    try:
        body_json = json.loads(raw_body) if raw_body else {}
        req_data = ReturnInspectRequest.model_validate(body_json)
    except Exception as e:
        raise ValidationErrorException(message=f"Validation failed: {e}")

    target_status = None
    if req_data.to in ("inspected_ok", "inspection_failed"):
        target_status = req_data.to
    elif req_data.result == "ok":
        target_status = "inspected_ok"
    elif req_data.result == "failed":
        target_status = "inspection_failed"
    else:
        raise ValidationErrorException(
            message=(
                "Inspection must specify result "
                "('ok'/'failed') or to "
                "('inspected_ok'/'inspection_failed')"
            )
        )

    with Session(db.engine) as session:
        cached = check_idempotency_replay(session, idempotency_key, payload_hash)
        if cached is not None:
            status_code, resp_data = cached
            return JSONResponse(status_code=status_code, content=resp_data)

        ret = session.scalar(
            select(Return).options(joinedload(Return.items)).where(Return.return_id == return_id)
        )
        if ret is None:
            raise NotFoundException(message="Return not found")

        if ret.status != "received":
            raise InvalidStateException(
                message=(f"Cannot inspect return in state " f"'{ret.status}'; must be 'received'")
            )

        now = Clock.now()
        ret.status = target_status
        ret.updated_at = now
        ret.version += 1

        for item in ret.items:
            item.status = target_status
            if req_data.note:
                item.condition_note = req_data.note

        log_audit(
            session=session,
            request_id=req_id,
            actor=actor,
            action=f"return_inspect_{target_status}",
            resource=f"returns/{return_id}",
            outcome=target_status,
        )

        emit_outbox_event(
            session=session,
            event_type="return.updated",
            payload={
                "return_id": ret.return_id,
                "order_id": ret.order_id,
                "customer_id": ret.customer_id,
                "status": ret.status,
                "inspection_result": target_status,
                "note": req_data.note,
            },
            resource_version=ret.version,
        )

        resp_dict = ReturnResponse.model_validate(ret).model_dump(mode="json")
        save_idempotency_record(
            session=session,
            key=idempotency_key,
            payload_hash=payload_hash,
            status_code=status.HTTP_200_OK,
            response_data=resp_dict,
            client_id=actor,
        )
        session.commit()
        session.refresh(ret)
        return ReturnResponse.model_validate(ret)


# ---------------------------------------------------------------------------
# Store Settings (seller_owner only)
# ---------------------------------------------------------------------------
@router.get(
    "/settings",
    response_model=StoreSettingsResponse,
    summary="Get store settings (seller_owner or orders:read_all service monitor)",
)
def get_store_settings(
    _: dict[str, Any] = Depends(require_owner_or_monitor),
) -> StoreSettingsResponse:
    with Session(db.engine) as session:
        settings_row = session.get(StoreSettings, 1)
        if settings_row is None:
            settings_row = StoreSettings(
                id=1,
                autonomy_cap_paise=50000,
                autonomy_daily_cap_paise=500000,
                updated_at=Clock.now(),
            )
            session.add(settings_row)
            session.commit()
            session.refresh(settings_row)
        return StoreSettingsResponse.model_validate(settings_row)


@router.put(
    "/settings",
    response_model=StoreSettingsResponse,
    summary="Update store settings (seller_owner only)",
)
def update_store_settings(
    req: StoreSettingsUpdateRequest,
    request: Request,
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> StoreSettingsResponse:
    if req.autonomy_cap_paise is not None and req.autonomy_cap_paise < 0:
        raise ValidationErrorException(message="autonomy_cap_paise cannot be negative")
    if req.autonomy_daily_cap_paise is not None and req.autonomy_daily_cap_paise < 0:
        raise ValidationErrorException(message="autonomy_daily_cap_paise cannot be negative")

    actor = principal.get("sub") or "unknown"
    req_id = getattr(request.state, "request_id", None) or request.headers.get(
        "X-Request-ID", "unknown"
    )

    with Session(db.engine) as session:
        settings_row = session.get(StoreSettings, 1)
        if settings_row is None:
            settings_row = StoreSettings(
                id=1,
                autonomy_cap_paise=50000,
                autonomy_daily_cap_paise=500000,
            )
            session.add(settings_row)

        if req.autonomy_cap_paise is not None:
            settings_row.autonomy_cap_paise = req.autonomy_cap_paise
        if req.autonomy_daily_cap_paise is not None:
            settings_row.autonomy_daily_cap_paise = req.autonomy_daily_cap_paise

        now = Clock.now()
        settings_row.updated_by = actor
        settings_row.updated_at = now
        settings_row.version += 1

        log_audit(
            session=session,
            request_id=req_id,
            actor=actor,
            action="update_settings",
            resource="admin/settings",
            outcome="success",
        )

        session.commit()
        session.refresh(settings_row)
        return StoreSettingsResponse.model_validate(settings_row)


@router.post(
    "/orders/{order_id}/cancel",
    response_model=OrderResponse,
    summary="Cancel an order before shipped (seller only)",
)
async def admin_cancel_order(
    order_id: str,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_role("seller_owner", "seller_staff")),
) -> Any:
    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    req_id = getattr(request.state, "request_id", None) or request.headers.get(
        "X-Request-ID", "unknown"
    )
    actor = principal.get("sub") or "seller"

    with Session(db.engine) as session:
        cached = check_idempotency_replay(session, idempotency_key, payload_hash)
        if cached is not None:
            status_code, resp_data = cached
            return JSONResponse(status_code=status_code, content=resp_data)

        order = session.scalar(
            select(Order).options(joinedload(Order.items)).where(Order.order_id == order_id)
        )
        if order is None:
            raise NotFoundException(message="Order not found")

        if order.status not in ("placed", "confirmed"):
            raise InvalidStateException(
                message=(
                    f"Order {order_id} in state '{order.status}' "
                    "cannot be cancelled; only before shipped"
                )
            )

        now = Clock.now()
        order.status = "cancelled"
        order.cancelled_at = now.replace(tzinfo=None) if hasattr(now, "tzinfo") else now
        order.version += 1

        log_audit(
            session=session,
            request_id=req_id,
            actor=actor,
            action="order_cancel",
            resource=f"orders/{order_id}",
            outcome="cancelled",
        )

        emit_outbox_event(
            session=session,
            event_type="order.updated",
            payload={
                "order_id": order.order_id,
                "customer_id": order.customer_id,
                "status": order.status,
                "cancelled_at": order.cancelled_at.isoformat() if order.cancelled_at else None,
            },
            resource_version=order.version,
        )

        resp_dict = OrderResponse.model_validate(order).model_dump(mode="json")
        save_idempotency_record(
            session=session,
            key=idempotency_key,
            payload_hash=payload_hash,
            status_code=status.HTTP_200_OK,
            response_data=resp_dict,
            client_id=actor,
        )
        session.commit()
        session.refresh(order)
        return OrderResponse.model_validate(order)
