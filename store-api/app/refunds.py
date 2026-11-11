import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

import db
from app.audit import log_audit
from app.clock import Clock
from app.deps import require_auth
from app.errors import (
    ForbiddenException,
    InvalidStateException,
    LimitExceededException,
    NotFoundException,
    UpstreamUnavailableException,
    ValidationErrorException,
)
from app.idempotency import (
    check_idempotency_replay,
    compute_payload_hash,
    require_idempotency_key,
    save_idempotency_record,
)
from app.models import (
    Customer,
    Order,
    OrderItem,
    Refund,
    Return,
    StoreAudit,
    StoreSettings,
    User,
)
from app.outbox import emit_outbox_event
from app.schemas import (
    CustomerRefundSummaryResponse,
    RefundAuthorizeRequest,
    RefundReleaseRequest,
    RefundReservationRequest,
    RefundResponse,
)
from app.simpay import complete_simpay_refund, is_fault_enabled, set_fault

router = APIRouter(prefix="/api/v1", tags=["Refunds"])


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or request.headers.get(
        "X-Request-ID", "unknown"
    )


def _actor(principal: dict[str, Any]) -> str:
    return (
        principal.get("sub")
        or principal.get("client_id")
        or principal.get("customer_id")
        or "unknown"
    )


def _generate_refund_id(session: Session, order_id: str) -> str:
    """Generate refund ID like RFD-####."""
    clean_suffix = order_id.replace("ORD-", "")
    candidate = f"RFD-{clean_suffix}"
    if session.get(Refund, candidate) is None:
        return candidate

    all_ids = session.scalars(select(Refund.refund_id)).all()
    max_num = 1000
    for r_id in all_ids:
        if r_id.startswith("RFD-"):
            try:
                num = int(r_id.replace("RFD-", ""))
                if num > max_num:
                    max_num = num
            except ValueError:
                continue
    return f"RFD-{max_num + 1}"


# ---------------------------------------------------------------------------
# POST /orders/{id}/refund-reservations
# ---------------------------------------------------------------------------
@router.post(
    "/orders/{order_id}/refund-reservations",
    response_model=RefundResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an atomic refund reservation hold",
)
async def create_refund_reservation(
    order_id: str,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_auth),
) -> Any:
    """
    Executor or seller creates a 'reserved' hold atomically.
    Amount must not exceed store's own max_refundable calculation.
    Cash-on-delivery orders require store_credit or upi_payout (SC-16).
    """
    is_seller = principal.get("type") == "user" and principal.get("role") in (
        "seller_owner",
        "seller_staff",
    )
    is_executor = principal.get("type") == "service" and (
        principal.get("client_id") == "agent-executor"
        or "refunds:reserve" in principal.get("scopes", [])
    )
    if not (is_seller or is_executor):
        raise ForbiddenException(message="Only seller or agent executor can reserve refunds")

    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    req_id = _request_id(request)
    act = _actor(principal)

    try:
        body_json = json.loads(raw_body) if raw_body else {}
        req_data = RefundReservationRequest.model_validate(body_json)
    except Exception as e:
        raise ValidationErrorException(message=f"Validation failed: {e}")

    if req_data.amount_paise <= 0:
        raise ValidationErrorException(message="amount_paise must be positive")

    valid_methods = {"original", "store_credit", "upi_payout"}
    if req_data.method not in valid_methods:
        raise ValidationErrorException(
            message=(
                f"Invalid refund method '{req_data.method}'. "
                f"Must be one of {sorted(valid_methods)}"
            )
        )

    with Session(db.engine) as session:
        cached = check_idempotency_replay(session, idempotency_key, payload_hash)
        if cached is not None:
            sc, data = cached
            return JSONResponse(status_code=sc, content=data)

        order = session.scalar(
            select(Order).options(joinedload(Order.items)).where(Order.order_id == order_id)
        )
        if order is None:
            raise NotFoundException(message=f"Order {order_id} not found")

        if order.status == "cancelled":
            raise InvalidStateException(message=f"Cannot refund cancelled order {order_id}")

        # COD method check (SC-16)
        is_cod = order.payment_method in ("cod", "cash_on_delivery")
        if is_cod and req_data.method == "original":
            raise ValidationErrorException(
                message=(
                    "Cash-on-delivery orders cannot be refunded to original "
                    "payment method; must use store_credit or upi_payout"
                )
            )

        # Store's OWN max_refundable calculation (Invariant 2: active refunds <= paid)
        active_refunds = session.scalars(
            select(Refund).where(
                Refund.order_id == order_id,
                Refund.status.in_(["reserved", "processing", "completed"]),
            )
        ).all()
        total_active_paise = sum(r.amount_paise for r in active_refunds)
        order_remaining_refundable_paise = max(0, order.total_paise - total_active_paise)

        max_allowed_paise = order_remaining_refundable_paise

        if req_data.return_id:
            ret = session.scalar(
                select(Return)
                .options(joinedload(Return.items))
                .where(Return.return_id == req_data.return_id)
            )
            if ret is None:
                raise NotFoundException(message=f"Return {req_data.return_id} not found")
            if ret.order_id != order_id:
                raise ValidationErrorException(
                    message=f"Return {req_data.return_id} does not belong to order {order_id}"
                )

            total_return_items_max = 0
            for r_item in ret.items:
                oi = session.get(OrderItem, r_item.item_id)
                if oi and oi.qty > 0:
                    total_return_items_max += (oi.line_paid_paise * r_item.qty) // oi.qty

            return_active_paise = sum(
                r.amount_paise for r in active_refunds if r.return_id == req_data.return_id
            )
            return_remaining_paise = max(0, total_return_items_max - return_active_paise)
            max_allowed_paise = min(order_remaining_refundable_paise, return_remaining_paise)

        if req_data.amount_paise > max_allowed_paise:
            raise LimitExceededException(
                message=(
                    f"Requested refund amount {req_data.amount_paise} paise exceeds "
                    f"maximum refundable {max_allowed_paise} paise"
                )
            )

        now = Clock.now()
        refund_id = _generate_refund_id(session, order_id)
        refund = Refund(
            refund_id=refund_id,
            order_id=order_id,
            return_id=req_data.return_id,
            customer_id=order.customer_id,
            amount_paise=req_data.amount_paise,
            method=req_data.method,
            status="reserved",
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        session.add(refund)

        log_audit(
            session=session,
            request_id=req_id,
            actor=act,
            action="refund_reserve",
            resource=f"orders/{order_id}/refund-reservations",
            outcome="reserved",
        )

        emit_outbox_event(
            session=session,
            event_type="refund.updated",
            payload={
                "refund_id": refund.refund_id,
                "order_id": refund.order_id,
                "return_id": refund.return_id,
                "customer_id": refund.customer_id,
                "status": refund.status,
                "amount_paise": refund.amount_paise,
                "method": refund.method,
            },
            resource_version=refund.version,
        )

        resp_dict = RefundResponse.model_validate(refund).model_dump(mode="json")
        save_idempotency_record(
            session=session,
            key=idempotency_key,
            payload_hash=payload_hash,
            status_code=status.HTTP_201_CREATED,
            response_data=resp_dict,
            client_id=act,
        )
        session.commit()
        session.refresh(refund)
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=resp_dict)


# ---------------------------------------------------------------------------
# POST /refunds/{id}/authorize
# ---------------------------------------------------------------------------
@router.post(
    "/refunds/{refund_id}/authorize",
    response_model=RefundResponse,
    summary="Authorize a reserved refund (seller only)",
)
async def authorize_refund(
    refund_id: str,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_auth),
) -> Any:
    """
    Seller token only. Staff up to Rs 2,000 (200,000 paise), owner above.
    Stores authorized_by, authorized_amount_paise, authorized_at.
    """
    if principal.get("type") != "user" or principal.get("role") not in (
        "seller_owner",
        "seller_staff",
    ):
        raise ForbiddenException(message="Only seller tokens can authorize refunds")

    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    req_id = _request_id(request)
    act = _actor(principal)

    try:
        body_json = json.loads(raw_body) if raw_body else {}
        req_data = RefundAuthorizeRequest.model_validate(body_json)
    except Exception as e:
        raise ValidationErrorException(message=f"Validation failed: {e}")

    with Session(db.engine) as session:
        cached = check_idempotency_replay(session, idempotency_key, payload_hash)
        if cached is not None:
            sc, data = cached
            return JSONResponse(status_code=sc, content=data)

        refund = session.get(Refund, refund_id)
        if refund is None:
            raise NotFoundException(message=f"Refund {refund_id} not found")

        if refund.status != "reserved":
            raise InvalidStateException(
                message=f"Cannot authorize refund in status '{refund.status}'; must be 'reserved'"
            )

        auth_amount = (
            req_data.amount_paise if req_data.amount_paise is not None else refund.amount_paise
        )
        if auth_amount <= 0 or auth_amount > refund.amount_paise:
            raise ValidationErrorException(
                message=(
                    f"Authorized amount {auth_amount} paise cannot exceed "
                    f"reserved amount {refund.amount_paise} paise"
                )
            )

        if principal.get("role") == "seller_staff" and auth_amount > 200000:
            raise ForbiddenException(
                message=(
                    "Seller staff can only authorize refunds up to Rs 2,000 "
                    "(200,000 paise); owner required above"
                )
            )

        now = Clock.now()
        refund.authorized_by = act
        refund.authorized_amount_paise = auth_amount
        refund.authorized_at = now
        refund.updated_at = now
        refund.version += 1

        log_audit(
            session=session,
            request_id=req_id,
            actor=act,
            action="refund_authorize",
            resource=f"refunds/{refund_id}",
            outcome="authorized",
        )

        emit_outbox_event(
            session=session,
            event_type="refund.updated",
            payload={
                "refund_id": refund.refund_id,
                "order_id": refund.order_id,
                "return_id": refund.return_id,
                "customer_id": refund.customer_id,
                "status": refund.status,
                "amount_paise": refund.amount_paise,
                "method": refund.method,
            },
            resource_version=refund.version,
        )

        resp_dict = RefundResponse.model_validate(refund).model_dump(mode="json")
        save_idempotency_record(
            session=session,
            key=idempotency_key,
            payload_hash=payload_hash,
            status_code=status.HTTP_200_OK,
            response_data=resp_dict,
            client_id=act,
        )
        session.commit()
        session.refresh(refund)
        return RefundResponse.model_validate(refund)


# ---------------------------------------------------------------------------
# POST /refunds/{id}/release
# ---------------------------------------------------------------------------
@router.post(
    "/refunds/{refund_id}/release",
    response_model=RefundResponse,
    summary="Release refund to SimPay for processing and completion",
)
async def release_refund(
    refund_id: str,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_auth),
) -> Any:
    """
    Executor or seller releases a refund.
    - Service-token release accepted ONLY if total <= autonomy cap OR stored
      authorization of sufficient role exists, AND simulated-IST-day total
      of service-token releases stays <= daily cap; otherwise 409 limit_exceeded.
    - Requires payment_status paid (or cod_collected).
    - Moves to 'processing'; SimPay completes after ~3s real time.
    """
    is_seller = principal.get("type") == "user" and principal.get("role") in (
        "seller_owner",
        "seller_staff",
    )
    is_executor = principal.get("type") == "service" and (
        principal.get("client_id") == "agent-executor"
        or "refunds:release" in principal.get("scopes", [])
    )
    if not (is_seller or is_executor):
        raise ForbiddenException(message="Only seller or agent executor can release refunds")

    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    req_id = _request_id(request)
    act = _actor(principal)

    try:
        body_json = json.loads(raw_body) if raw_body else {}
        _ = RefundReleaseRequest.model_validate(body_json)
    except Exception as e:
        raise ValidationErrorException(message=f"Validation failed: {e}")

    with Session(db.engine) as session:
        refund = session.get(Refund, refund_id)
        if refund is None:
            raise NotFoundException(message=f"Refund {refund_id} not found")

        cached = check_idempotency_replay(session, idempotency_key, payload_hash)
        if cached is not None and refund.status != "failed":
            sc, data = cached
            return JSONResponse(status_code=sc, content=data)

        if refund.status not in ("reserved", "failed"):
            raise InvalidStateException(
                message=(
                    f"Cannot release refund in status '{refund.status}'; "
                    "must be 'reserved' or 'failed'"
                )
            )

        order = session.get(Order, refund.order_id)
        if order is None:
            raise NotFoundException(message=f"Order {refund.order_id} not found")

        if order.payment_status not in ("paid", "cod_collected"):
            raise InvalidStateException(
                message=(
                    f"Order payment status '{order.payment_status}' does not allow refund release; "
                    "requires 'paid' or 'cod_collected'"
                )
            )

        if is_executor:
            settings_row = session.get(StoreSettings, 1)
            cap = settings_row.autonomy_cap_paise if settings_row else 50000
            daily_cap = settings_row.autonomy_daily_cap_paise if settings_row else 500000

            if refund.amount_paise > cap:
                has_auth = bool(
                    refund.authorized_by
                    and (refund.authorized_amount_paise or 0) >= refund.amount_paise
                )
                if not has_auth:
                    raise LimitExceededException(
                        message=(
                            f"Release value {refund.amount_paise} paise exceeds autonomy cap "
                            f"{cap} paise without stored authorization"
                        )
                    )
                authorizer = session.get(User, refund.authorized_by)
                if authorizer is None:
                    raise LimitExceededException(message="Authorizer on refund does not exist")
                if refund.amount_paise > 200000 and authorizer.role != "seller_owner":
                    raise LimitExceededException(
                        message="Amounts above Rs 2,000 require authorization by seller_owner"
                    )
                if refund.amount_paise <= 200000 and authorizer.role not in (
                    "seller_owner",
                    "seller_staff",
                ):
                    raise LimitExceededException(message="Stored authorization by seller required")

            now_utc = Clock.now()
            ist_offset = timedelta(hours=5, minutes=30)
            now_ist = now_utc + ist_offset
            start_of_day_ist = datetime(now_ist.year, now_ist.month, now_ist.day, 0, 0, 0)
            start_of_day_utc = start_of_day_ist - ist_offset
            end_of_day_utc = start_of_day_utc + timedelta(days=1)

            audit_query = select(StoreAudit).where(
                StoreAudit.action.in_(["refund_release", "refund_release_timeout"]),
                StoreAudit.actor.in_(["agent-executor", "service"]),
                StoreAudit.ts >= start_of_day_utc,
                StoreAudit.ts < end_of_day_utc,
            )
            service_audits = session.scalars(audit_query).all()
            released_ids = {
                a.resource.replace("refunds/", "")
                for a in service_audits
                if a.resource.startswith("refunds/")
            }
            released_ids.discard(refund.refund_id)

            today_total = 0
            if released_ids:
                service_today = session.scalars(
                    select(Refund).where(
                        Refund.refund_id.in_(list(released_ids)),
                        Refund.status.in_(["processing", "completed"]),
                    )
                ).all()
                today_total = sum(r.amount_paise for r in service_today)

            if today_total + refund.amount_paise > daily_cap:
                raise LimitExceededException(
                    message=(
                        f"Daily service-token release cap of {daily_cap} paise exceeded "
                        f"(today: {today_total} paise, requested: {refund.amount_paise} paise)"
                    )
                )

        if is_fault_enabled("payment.down", session):
            raise UpstreamUnavailableException(
                message="SimPay gateway is currently down (payment.down)"
            )

        if is_fault_enabled("payment.fail_once", session):
            set_fault("payment.fail_once", False, session=session)
            refund.status = "failed"
            refund.failure_reason = "SimPay simulated error (payment.fail_once)"
            refund.updated_at = Clock.now()
            refund.version += 1
            log_audit(
                session=session,
                request_id=req_id,
                actor=act,
                action="refund_release_failed",
                resource=f"refunds/{refund_id}",
                outcome="failed",
            )
            session.commit()
            raise UpstreamUnavailableException(message="SimPay payment failed (payment.fail_once)")

        if is_fault_enabled("payment.timeout_after_commit", session):
            set_fault("payment.timeout_after_commit", False, session=session)
            refund.status = "processing"
            refund.updated_at = Clock.now()
            refund.version += 1
            log_audit(
                session=session,
                request_id=req_id,
                actor=act,
                action="refund_release_timeout",
                resource=f"refunds/{refund_id}",
                outcome="processing",
            )
            session.commit()
            asyncio.create_task(complete_simpay_refund(refund.refund_id))
            raise UpstreamUnavailableException(
                message="SimPay gateway timeout after commit (payment.timeout_after_commit)"
            )

        now = Clock.now()
        refund.status = "processing"
        refund.failure_reason = None
        refund.updated_at = now
        refund.version += 1

        log_audit(
            session=session,
            request_id=req_id,
            actor=act,
            action="refund_release",
            resource=f"refunds/{refund_id}",
            outcome="processing",
        )

        emit_outbox_event(
            session=session,
            event_type="refund.updated",
            payload={
                "refund_id": refund.refund_id,
                "order_id": refund.order_id,
                "return_id": refund.return_id,
                "customer_id": refund.customer_id,
                "status": refund.status,
                "amount_paise": refund.amount_paise,
                "method": refund.method,
            },
            resource_version=refund.version,
        )

        resp_dict = RefundResponse.model_validate(refund).model_dump(mode="json")
        save_idempotency_record(
            session=session,
            key=idempotency_key,
            payload_hash=payload_hash,
            status_code=status.HTTP_200_OK,
            response_data=resp_dict,
            client_id=act,
        )
        session.commit()
        session.refresh(refund)

        asyncio.create_task(complete_simpay_refund(refund.refund_id))
        return RefundResponse.model_validate(refund)


# ---------------------------------------------------------------------------
# POST /refunds/{id}/cancel-reservation
# ---------------------------------------------------------------------------
@router.post(
    "/refunds/{refund_id}/cancel-reservation",
    response_model=RefundResponse,
    summary="Cancel a refund reservation hold",
)
async def cancel_refund_reservation(
    refund_id: str,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Executor or seller releases a 'reserved' hold back to cancelled."""
    is_seller = principal.get("type") == "user" and principal.get("role") in (
        "seller_owner",
        "seller_staff",
    )
    is_executor = principal.get("type") == "service" and (
        principal.get("client_id") == "agent-executor"
        or "refunds:reserve" in principal.get("scopes", [])
    )
    if not (is_seller or is_executor):
        raise ForbiddenException(message="Only seller or agent executor can cancel reservations")

    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    req_id = _request_id(request)
    act = _actor(principal)

    with Session(db.engine) as session:
        cached = check_idempotency_replay(session, idempotency_key, payload_hash)
        if cached is not None:
            sc, data = cached
            return JSONResponse(status_code=sc, content=data)

        refund = session.get(Refund, refund_id)
        if refund is None:
            raise NotFoundException(message=f"Refund {refund_id} not found")

        if refund.status != "reserved":
            raise InvalidStateException(
                message=f"Cannot cancel reservation in status '{refund.status}'; must be 'reserved'"
            )

        now = Clock.now()
        refund.status = "cancelled"
        refund.failure_reason = "hold_cancelled"
        refund.updated_at = now
        refund.version += 1

        log_audit(
            session=session,
            request_id=req_id,
            actor=act,
            action="refund_cancel_reservation",
            resource=f"refunds/{refund_id}",
            outcome="cancelled",
        )

        emit_outbox_event(
            session=session,
            event_type="refund.updated",
            payload={
                "refund_id": refund.refund_id,
                "order_id": refund.order_id,
                "return_id": refund.return_id,
                "customer_id": refund.customer_id,
                "status": refund.status,
                "amount_paise": refund.amount_paise,
                "method": refund.method,
            },
            resource_version=refund.version,
        )

        resp_dict = RefundResponse.model_validate(refund).model_dump(mode="json")
        save_idempotency_record(
            session=session,
            key=idempotency_key,
            payload_hash=payload_hash,
            status_code=status.HTTP_200_OK,
            response_data=resp_dict,
            client_id=act,
        )
        session.commit()
        session.refresh(refund)
        return RefundResponse.model_validate(refund)


# ---------------------------------------------------------------------------
# GET /refunds/{id}
# ---------------------------------------------------------------------------
@router.get(
    "/refunds/{refund_id}",
    response_model=RefundResponse,
    summary="Get refund details",
)
def get_refund(
    refund_id: str,
    request: Request,
    principal: dict[str, Any] = Depends(require_auth),
) -> RefundResponse:
    """Customer: own only (uniform 404 + audit denial). Seller/Executor: any."""
    req_id = _request_id(request)
    act = _actor(principal)

    with Session(db.engine) as session:
        refund = session.get(Refund, refund_id)
        if refund is None:
            raise NotFoundException(message=f"Refund {refund_id} not found")

        if principal.get("type") == "user" and principal.get("role") == "customer":
            cust_id = principal.get("customer_id")
            if refund.customer_id != cust_id:
                log_audit(
                    session=session,
                    request_id=req_id,
                    actor=act,
                    action="read",
                    resource=f"refunds/{refund_id}",
                    outcome="denied",
                )
                raise NotFoundException(message=f"Refund {refund_id} not found")

        return RefundResponse.model_validate(refund)


# ---------------------------------------------------------------------------
# GET /customers/{id}/refund-summary?days=30
# ---------------------------------------------------------------------------
@router.get(
    "/customers/{customer_id}/refund-summary",
    response_model=CustomerRefundSummaryResponse,
    summary="Get customer refund summary over days window",
)
def get_customer_refund_summary(
    customer_id: str,
    days: int = Query(default=30, ge=1, le=365),
    principal: dict[str, Any] = Depends(require_auth),
) -> CustomerRefundSummaryResponse:
    """Executor or seller only. Customer gets 403."""
    is_seller = principal.get("type") == "user" and principal.get("role") in (
        "seller_owner",
        "seller_staff",
    )
    is_executor = principal.get("type") == "service" and (
        principal.get("client_id") in ("agent-executor", "agent-monitor")
        or "orders:read_all" in principal.get("scopes", [])
        or "refunds:reserve" in principal.get("scopes", [])
    )
    if not (is_seller or is_executor):
        raise ForbiddenException(message="Seller or agent access required for refund summary")

    with Session(db.engine) as session:
        customer = session.get(Customer, customer_id)
        if customer is None:
            raise NotFoundException(message=f"Customer {customer_id} not found")

        now = Clock.now()
        since = now - timedelta(days=days)
        # SQLite stores naive datetimes; strip tz for comparison
        since_naive = since.replace(tzinfo=None)

        refunds = session.scalars(select(Refund).where(Refund.customer_id == customer_id)).all()

        completed_in_window = [
            r
            for r in refunds
            if r.status == "completed"
            and (r.created_at.replace(tzinfo=None) if r.created_at else r.created_at) >= since_naive
        ]
        total_completed_paise = sum(r.amount_paise for r in completed_in_window)

        active_holds = [r for r in refunds if r.status in ("reserved", "processing")]
        total_holds_paise = sum(r.amount_paise for r in active_holds)

        return CustomerRefundSummaryResponse(
            customer_id=customer_id,
            days=days,
            total_refunds_paise=total_completed_paise,
            refund_count=len(completed_in_window),
            active_holds_paise=total_holds_paise,
            active_hold_count=len(active_holds),
        )
