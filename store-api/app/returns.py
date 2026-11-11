import json
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

import db
from app.audit import log_audit
from app.clock import Clock
from app.deps import require_auth, require_customer
from app.errors import (
    DuplicateException,
    ForbiddenException,
    InvalidStateException,
    LimitExceededException,
    NotFoundException,
    ValidationErrorException,
)
from app.idempotency import (
    check_idempotency_replay,
    compute_payload_hash,
    require_idempotency_key,
    save_idempotency_record,
)
from app.models import (
    Order,
    OrderItem,
    Return,
    ReturnItem,
    StoreSettings,
)
from app.outbox import emit_outbox_event
from app.schemas import (
    ReturnCreateRequest,
    ReturnResponse,
    ReturnTransitionRequest,
)

router = APIRouter(prefix="/api/v1", tags=["Returns"])

# ---------------------------------------------------------------------------
# Single source-of-truth state machine for Returns (spec section 5)
# ---------------------------------------------------------------------------
RETURN_STATE_TRANSITIONS: dict[str, set[str]] = {
    "requested": {"approved", "rejected", "cancelled"},
    "approved": {"pickup_scheduled"},
    "pickup_scheduled": {"in_transit_back"},
    "in_transit_back": {"received"},
    "received": {"inspected_ok", "inspection_failed"},
    "inspected_ok": {"completed"},
    # Terminal states
    "rejected": set(),
    "cancelled": set(),
    "inspection_failed": set(),
    "completed": set(),
}


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


def _generate_return_id(session: Session, order_id: str) -> str:
    """Generate return ID like RET-####."""
    clean_suffix = order_id.replace("ORD-", "")
    candidate = f"RET-{clean_suffix}"
    if session.get(Return, candidate) is None:
        return candidate

    # Find highest numeric suffix among existing returns
    all_ids = session.scalars(select(Return.return_id)).all()
    max_num = 1000
    for r_id in all_ids:
        if r_id.startswith("RET-"):
            try:
                num = int(r_id.replace("RET-", ""))
                if num > max_num:
                    max_num = num
            except ValueError:
                continue
    return f"RET-{max_num + 1}"


# -----------------------------------------------------------------------
# POST /orders/{id}/returns  –  create a return (customer only)
# -----------------------------------------------------------------------
@router.post(
    "/orders/{order_id}/returns",
    response_model=ReturnResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a return for an order",
)
async def create_return(
    order_id: str,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_customer),
) -> Any:
    """
    Customer creates a return on their own delivered order.

    Checks: ownership (uniform 404), delivered status, qty limits,
    one active return per item, idempotency replay / mismatch.
    reason_text is stored as plain data and never executed.
    """
    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    req_id = _request_id(request)
    act = _actor(principal)
    customer_id = principal["customer_id"]

    try:
        body_json = json.loads(raw_body) if raw_body else {}
        req_data = ReturnCreateRequest.model_validate(body_json)
    except Exception as e:
        raise ValidationErrorException(message=f"Validation failed: {e}")

    if not req_data.items:
        raise ValidationErrorException(message="At least one item must be returned")

    with Session(db.engine) as session:
        # 1. Idempotency replay
        cached = check_idempotency_replay(session, idempotency_key, payload_hash)
        if cached is not None:
            sc, data = cached
            return JSONResponse(status_code=sc, content=data)

        # 2. Ownership – uniform 404
        order = session.scalar(
            select(Order).options(joinedload(Order.items)).where(Order.order_id == order_id)
        )
        if order is None or order.customer_id != customer_id:
            if order is not None and order.customer_id != customer_id:
                log_audit(
                    session=session,
                    request_id=req_id,
                    actor=act,
                    action="create_return",
                    resource=f"orders/{order_id}/returns",
                    outcome="denied",
                )
            raise NotFoundException(message="Order not found")

        # 3. Delivery check
        if order.status != "delivered":
            raise InvalidStateException(
                message=(
                    f"Order {order_id} has status '{order.status}', "
                    "only delivered orders can be returned"
                )
            )

        # 4. Item validation
        items_by_id = {i.item_id: i for i in order.items}
        for ri in req_data.items:
            if ri.item_id not in items_by_id:
                raise NotFoundException(
                    message=(f"Item {ri.item_id} does not belong " f"to order {order_id}")
                )

            oi = items_by_id[ri.item_id]

            existing = session.scalars(
                select(ReturnItem).where(ReturnItem.item_id == ri.item_id)
            ).all()

            # Active return check
            active = [r for r in existing if r.status not in ("completed", "cancelled", "rejected")]
            if active:
                raise DuplicateException(
                    message=(f"An active return already exists " f"for item {ri.item_id}")
                )

            # Cumulative qty check
            past_qty = sum(r.qty for r in existing if r.status not in ("cancelled", "rejected"))
            avail = oi.qty - past_qty
            if ri.qty <= 0 or ri.qty > avail:
                raise LimitExceededException(
                    message=(
                        f"Return qty {ri.qty} exceeds eligible "
                        f"qty {avail} for item {ri.item_id}"
                    )
                )

        # 5. Create the Return
        ret_id = _generate_return_id(session, order_id)
        now = Clock.now()

        new_ret = Return(
            return_id=ret_id,
            order_id=order_id,
            customer_id=customer_id,
            status="requested",
            reason_code=req_data.reason_code,
            reason_text=req_data.reason_text,
            resolution=req_data.resolution,
            requested_via="chat",
            approved_by=None,
            client_ref=idempotency_key,
            created_at=now,
            updated_at=now,
            version=1,
        )
        session.add(new_ret)
        session.flush()

        for ri in req_data.items:
            session.add(
                ReturnItem(
                    return_id=ret_id,
                    item_id=ri.item_id,
                    qty=ri.qty,
                    condition_note=ri.condition_note or req_data.reason_text,
                    status="requested",
                )
            )

        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            raise DuplicateException(
                message=("An active return already exists " "for one or more requested items")
            )

        resp = ReturnResponse.model_validate(new_ret)
        resp_dict = resp.model_dump(mode="json")

        emit_outbox_event(
            session=session,
            event_type="return.updated",
            payload={
                "return_id": new_ret.return_id,
                "order_id": new_ret.order_id,
                "customer_id": new_ret.customer_id,
                "status": new_ret.status,
                "reason_code": new_ret.reason_code,
                "resolution": new_ret.resolution,
            },
            resource_version=new_ret.version,
        )

        save_idempotency_record(
            session=session,
            key=idempotency_key,
            payload_hash=payload_hash,
            status_code=status.HTTP_201_CREATED,
            response_data=resp_dict,
            client_id=act,
        )

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            recheck = check_idempotency_replay(session, idempotency_key, payload_hash)
            if recheck is not None:
                sc, data = recheck
                return JSONResponse(status_code=sc, content=data)
            raise DuplicateException(
                message=("An active return already exists " "for one or more requested items")
            )

        session.refresh(new_ret)
        return ReturnResponse.model_validate(new_ret)


# -----------------------------------------------------------------------
# GET /returns  –  list returns
# -----------------------------------------------------------------------
@router.get(
    "/returns",
    response_model=list[ReturnResponse],
    summary="List returns",
)
def list_returns(
    principal: dict[str, Any] = Depends(require_auth),
) -> list[ReturnResponse]:
    """Customer: own only.  Seller / agent-monitor: all."""
    with Session(db.engine) as session:
        stmt = select(Return).options(joinedload(Return.items)).order_by(Return.created_at.desc())

        is_seller = principal.get("type") == "user" and principal.get("role") in (
            "seller_owner",
            "seller_staff",
        )
        is_monitor = principal.get("type") == "service" and "orders:read_all" in principal.get(
            "scopes", []
        )

        if not (is_seller or is_monitor):
            cid = principal.get("customer_id")
            stmt = stmt.where(Return.customer_id == cid)

        rows = session.scalars(stmt).unique().all()
        return [ReturnResponse.model_validate(r) for r in rows]


# -----------------------------------------------------------------------
# GET /returns/{id}  –  return detail
# -----------------------------------------------------------------------
@router.get(
    "/returns/{return_id}",
    response_model=ReturnResponse,
    summary="Get return details",
)
def get_return(
    return_id: str,
    request: Request,
    principal: dict[str, Any] = Depends(require_auth),
) -> ReturnResponse:
    """Customer: own only (uniform 404 + audit).  Seller: any."""
    req_id = _request_id(request)
    act = _actor(principal)

    with Session(db.engine) as session:
        ret = session.scalar(
            select(Return).options(joinedload(Return.items)).where(Return.return_id == return_id)
        )
        if ret is None:
            raise NotFoundException(message="Return not found")

        is_seller = principal.get("type") == "user" and principal.get("role") in (
            "seller_owner",
            "seller_staff",
        )
        is_monitor = principal.get("type") == "service" and "orders:read_all" in principal.get(
            "scopes", []
        )

        if not (is_seller or is_monitor):
            cid = principal.get("customer_id")
            if principal.get("role") != "customer" or ret.customer_id != cid:
                log_audit(
                    session=session,
                    request_id=req_id,
                    actor=act,
                    action="read",
                    resource=f"returns/{return_id}",
                    outcome="denied",
                )
                raise NotFoundException(message="Return not found")

        return ReturnResponse.model_validate(ret)


# -----------------------------------------------------------------------
# POST /returns/{id}/cancel  –  customer cancels before approval
# -----------------------------------------------------------------------
@router.post(
    "/returns/{return_id}/cancel",
    response_model=ReturnResponse,
    summary="Cancel a return request (customer, before approval)",
)
async def cancel_return(
    return_id: str,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_customer),
) -> Any:
    """Customer cancels own return; only while status is 'requested'."""
    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    req_id = _request_id(request)
    act = _actor(principal)
    customer_id = principal["customer_id"]

    with Session(db.engine) as session:
        cached = check_idempotency_replay(session, idempotency_key, payload_hash)
        if cached is not None:
            sc, data = cached
            return JSONResponse(status_code=sc, content=data)

        ret = session.scalar(
            select(Return).options(joinedload(Return.items)).where(Return.return_id == return_id)
        )
        if ret is None or ret.customer_id != customer_id:
            if ret is not None and ret.customer_id != customer_id:
                log_audit(
                    session=session,
                    request_id=req_id,
                    actor=act,
                    action="cancel",
                    resource=f"returns/{return_id}",
                    outcome="denied",
                )
            raise NotFoundException(message="Return not found")

        if ret.status != "requested":
            raise InvalidStateException(
                message=(f"Return {return_id} in state " f"'{ret.status}' cannot be cancelled")
            )

        now = Clock.now()
        ret.status = "cancelled"
        ret.updated_at = now
        ret.version += 1
        for item in ret.items:
            item.status = "cancelled"

        log_audit(
            session=session,
            request_id=req_id,
            actor=act,
            action="return_cancel",
            resource=f"returns/{return_id}",
            outcome="cancelled",
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
            client_id=act,
        )
        session.commit()
        session.refresh(ret)
        return ReturnResponse.model_validate(ret)


# -----------------------------------------------------------------------
# POST /returns/{id}/transition  –  state-machine driven
# -----------------------------------------------------------------------
@router.post(
    "/returns/{return_id}/transition",
    response_model=ReturnResponse,
    summary="Transition return state",
)
async def transition_return(
    return_id: str,
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    principal: dict[str, Any] = Depends(require_auth),
) -> Any:
    """
    Drive return state along the state-machine table.

    Seller token: any allowed edge.
    Agent-executor: only requested -> approved/rejected,
                    within the autonomy cap from store_settings.
    """
    raw_body = await request.body()
    payload_hash = compute_payload_hash(raw_body)
    req_id = _request_id(request)
    act = _actor(principal)

    try:
        body_json = json.loads(raw_body) if raw_body else {}
        req_data = ReturnTransitionRequest.model_validate(body_json)
    except Exception as e:
        raise ValidationErrorException(message=f"Validation failed: {e}")

    with Session(db.engine) as session:
        cached = check_idempotency_replay(session, idempotency_key, payload_hash)
        if cached is not None:
            sc, data = cached
            return JSONResponse(status_code=sc, content=data)

        ret = session.scalar(
            select(Return).options(joinedload(Return.items)).where(Return.return_id == return_id)
        )
        if ret is None:
            raise NotFoundException(message="Return not found")

        # 1. State machine check
        allowed = RETURN_STATE_TRANSITIONS.get(ret.status, set())
        if req_data.to not in allowed:
            raise InvalidStateException(
                message=(
                    f"Cannot transition from '{ret.status}' "
                    f"to '{req_data.to}'. "
                    f"Allowed: {sorted(allowed)}"
                )
            )

        # 2. Actor permission
        is_seller = principal.get("type") == "user" and principal.get("role") in (
            "seller_owner",
            "seller_staff",
        )
        is_executor = (
            principal.get("type") == "service" and principal.get("client_id") == "agent-executor"
        )

        if not (is_seller or is_executor):
            raise ForbiddenException(
                message=("Only sellers or the agent executor " "can transition returns")
            )

        if is_executor:
            if ret.status != "requested" or req_data.to not in (
                "approved",
                "rejected",
                "cancelled",
            ):
                raise ForbiddenException(
                    message=("Executor may only do requested -> approved/rejected/cancelled")
                )

            if req_data.to == "approved":
                # Autonomy cap check applies to autonomous approvals, not compensation cancels
                total_paise = 0
                for r_item in ret.items:
                    oi = session.get(OrderItem, r_item.item_id)
                    if oi:
                        total_paise += (oi.line_paid_paise * r_item.qty) // oi.qty

                settings_row = session.get(StoreSettings, 1)
                cap = settings_row.autonomy_cap_paise if settings_row else 50000

                if total_paise > cap:
                    raise LimitExceededException(
                        message=(
                            f"Return value {total_paise} paise "
                            f"exceeds autonomy cap {cap} paise"
                        )
                    )

        # 3. Apply transition
        now = Clock.now()
        ret.status = req_data.to
        ret.updated_at = now
        ret.version += 1

        if req_data.to in ("approved", "rejected"):
            ret.approved_by = act

        for item in ret.items:
            item.status = req_data.to

        log_audit(
            session=session,
            request_id=req_id,
            actor=act,
            action=f"return_transition_{req_data.to}",
            resource=f"returns/{return_id}",
            outcome=req_data.to,
        )

        emit_outbox_event(
            session=session,
            event_type="return.updated",
            payload={
                "return_id": ret.return_id,
                "order_id": ret.order_id,
                "customer_id": ret.customer_id,
                "status": ret.status,
                "transition_to": req_data.to,
                "reason": req_data.reason,
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
            client_id=act,
        )
        session.commit()
        session.refresh(ret)
        return ReturnResponse.model_validate(ret)
