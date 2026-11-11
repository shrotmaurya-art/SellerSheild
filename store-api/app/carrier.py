"""
Carrier Simulator (SwiftShip) — spec section 7.1, 6, 8, 16

Features:
- Scripts: normal_delivery, delayed_exception, lost_in_transit, rto.
- Tracking numbers: SS plus 10 digits (e.g. SS1048000001).
- Advance shipment: POST /sim/carrier/shipments/{id}/advance.
- Play scenario: POST /sim/carrier/scenarios/{id}/play.
- Seller-only routes, gated by DEMO_MODE=true (404 otherwise).
- Fault switches: carrier.down (503), carrier.slow (8s), carrier.stale (no new events).
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

import db
from app.clock import Clock
from app.config import settings
from app.deps import require_role
from app.errors import NotFoundException, UpstreamUnavailableException
from app.faults import is_fault_enabled
from app.models import Order, Shipment, ShipmentEvent
from app.outbox import emit_outbox_event
from app.schemas import (
    CarrierAdvanceResponse,
    PlayScenarioRequest,
    ShipmentEventResponse,
    ShipmentResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Carrier Simulator"])


@dataclass(frozen=True)
class CarrierScriptStep:
    hours_after_shipping: int
    status: str
    location: str
    note: str
    eta_delta_hours: Optional[int] = None


CARRIER_SCRIPTS: dict[str, list[CarrierScriptStep]] = {
    "normal_delivery": [
        CarrierScriptStep(
            hours_after_shipping=0,
            status="in_transit",
            location="Bengaluru Hub",
            note="Package received at carrier facility",
        ),
        CarrierScriptStep(
            hours_after_shipping=24,
            status="in_transit",
            location="Regional Sorting Facility",
            note="Package departed origin facility",
        ),
        CarrierScriptStep(
            hours_after_shipping=48,
            status="out_for_delivery",
            location="Local Delivery Center",
            note="Package out for delivery",
        ),
        CarrierScriptStep(
            hours_after_shipping=72,
            status="delivered",
            location="Customer Address",
            note="Package delivered to recipient",
        ),
    ],
    "delayed_exception": [
        CarrierScriptStep(
            hours_after_shipping=0,
            status="picked_up",
            location="Mumbai Hub",
            note="Shipment picked up from seller",
        ),
        CarrierScriptStep(
            hours_after_shipping=24,
            status="exception",
            location="Mumbai Hub",
            note="address verification pending",
        ),
    ],
    "lost_in_transit": [
        CarrierScriptStep(
            hours_after_shipping=0,
            status="in_transit",
            location="Delhi Sorting Center",
            note="Package departed facility",
        ),
        CarrierScriptStep(
            hours_after_shipping=144,  # 6 days
            status="lost",
            location="Delhi Sorting Center",
            note="Package lost in transit",
        ),
    ],
    "rto": [
        CarrierScriptStep(
            hours_after_shipping=0,
            status="in_transit",
            location="Origin Warehouse",
            note="Package shipped",
        ),
        CarrierScriptStep(
            hours_after_shipping=24,
            status="out_for_delivery",
            location="Local Hub",
            note="Package out for delivery",
        ),
        CarrierScriptStep(
            hours_after_shipping=48,
            status="undelivered",
            location="Local Hub",
            note="Delivery attempted - recipient unavailable",
        ),
        CarrierScriptStep(
            hours_after_shipping=72,
            status="rto",
            location="Origin Facility",
            note="Package returned to origin",
        ),
    ],
}


def generate_tracking_number() -> str:
    """Generate SwiftShip tracking number formatted as SS + 10 digits."""
    digits = f"{secrets.randbelow(10**10):010d}"
    return f"SS{digits}"


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ensure_demo_mode():
    if not settings.DEMO_MODE:
        raise NotFoundException(message="Endpoint not found")


async def check_carrier_faults():
    """Apply carrier fault switches."""
    if is_fault_enabled("carrier.down"):
        raise UpstreamUnavailableException(message="Carrier service unavailable (carrier.down)")
    if is_fault_enabled("carrier.slow"):
        await asyncio.sleep(8)


def _build_shipment_response(shipment: Shipment) -> ShipmentResponse:
    now = Clock.now()
    eta_utc = _to_utc(shipment.eta_at)
    is_delayed = bool(shipment.status != "delivered" and eta_utc is not None and eta_utc < now)
    sorted_events = sorted(shipment.events, key=lambda ev: ev.ts)
    return ShipmentResponse(
        shipment_id=shipment.shipment_id,
        order_id=shipment.order_id,
        carrier=shipment.carrier,
        tracking_no=shipment.tracking_no,
        status=shipment.status,
        eta_at=shipment.eta_at,
        last_event_at=shipment.last_event_at,
        script_id=shipment.script_id,
        delayed=is_delayed,
        events=[ShipmentEventResponse.model_validate(ev) for ev in sorted_events],
    )


def advance_shipment_in_session(
    session: Session,
    shipment: Shipment,
    now: Optional[datetime] = None,
) -> Optional[ShipmentEvent]:
    """
    Advance shipment to the next event in its script.
    Returns the created ShipmentEvent, or None if script is already completed.
    """
    if now is None:
        now = Clock.now()

    script = CARRIER_SCRIPTS.get(shipment.script_id, CARRIER_SCRIPTS["normal_delivery"])
    current_events = shipment.events
    next_index = len(current_events)

    if next_index >= len(script):
        # Script already exhausted
        return None

    step = script[next_index]
    order_id_num = shipment.order_id.replace("ORD-", "")
    event_id = f"EVT-{order_id_num}-{next_index + 1}"

    # Check if this event already exists
    existing_evt = session.get(ShipmentEvent, event_id)
    if existing_evt:
        return None

    # Handle optional ETA change recorded as part of step
    if step.eta_delta_hours:
        from datetime import timedelta

        new_eta = now + timedelta(hours=step.eta_delta_hours)
        shipment.eta_at = new_eta
        # Record ETA change as an event or note
        step_note = f"{step.note} (ETA revised to {new_eta.strftime('%Y-%m-%d %H:%M UTC')})"
    else:
        step_note = step.note

    event = ShipmentEvent(
        event_id=event_id,
        shipment_id=shipment.shipment_id,
        ts=now.replace(tzinfo=None) if hasattr(now, "tzinfo") else now,
        status=step.status,
        location=step.location,
        note=step_note,
    )
    session.add(event)
    if event not in shipment.events:
        shipment.events.append(event)

    shipment.status = step.status
    shipment.last_event_at = now.replace(tzinfo=None) if hasattr(now, "tzinfo") else now
    shipment.version += 1

    emit_outbox_event(
        session=session,
        event_type="shipment.updated",
        payload={
            "shipment_id": shipment.shipment_id,
            "order_id": shipment.order_id,
            "carrier": shipment.carrier,
            "tracking_no": shipment.tracking_no,
            "status": shipment.status,
            "eta_at": shipment.eta_at.isoformat() if shipment.eta_at else None,
            "last_event_at": shipment.last_event_at.isoformat() if shipment.last_event_at else None,
        },
        resource_version=shipment.version,
    )

    # If status becomes delivered, update Order
    if step.status == "delivered":
        order = session.get(Order, shipment.order_id)
        if order:
            order.status = "delivered"
            order.delivered_at = now.replace(tzinfo=None) if hasattr(now, "tzinfo") else now
            order.version += 1
            emit_outbox_event(
                session=session,
                event_type="order.updated",
                payload={
                    "order_id": order.order_id,
                    "customer_id": order.customer_id,
                    "status": order.status,
                    "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
                },
                resource_version=order.version,
            )

    session.flush()
    return event


@router.post(
    "/sim/carrier/shipments/{id}/advance",
    response_model=CarrierAdvanceResponse,
    summary="Advance a shipment along its carrier script (seller, demo only)",
)
@router.post(
    "/api/v1/sim/carrier/shipments/{id}/advance",
    response_model=CarrierAdvanceResponse,
    include_in_schema=False,
)
async def advance_shipment(
    id: str,
    principal: dict = Depends(require_role("seller_owner", "seller_staff")),
) -> CarrierAdvanceResponse:
    _ensure_demo_mode()
    await check_carrier_faults()

    # Fault check: carrier.stale means no new events
    if is_fault_enabled("carrier.stale"):
        with Session(db.engine) as session:
            shipment = (
                session.query(Shipment)
                .options(joinedload(Shipment.events))
                .filter((Shipment.shipment_id == id) | (Shipment.order_id == id))
                .one_or_none()
            )
            if not shipment:
                raise NotFoundException(message="Shipment not found")
            return CarrierAdvanceResponse(
                shipment=_build_shipment_response(shipment),
                new_event=None,
                message="Carrier stale fault active: no new events",
            )

    now = Clock.now()
    with Session(db.engine) as session:
        shipment = (
            session.query(Shipment)
            .options(joinedload(Shipment.events))
            .filter((Shipment.shipment_id == id) | (Shipment.order_id == id))
            .one_or_none()
        )
        if not shipment:
            raise NotFoundException(message="Shipment not found")

        new_event = advance_shipment_in_session(session, shipment, now=now)
        session.commit()
        session.refresh(shipment)

        return CarrierAdvanceResponse(
            shipment=_build_shipment_response(shipment),
            new_event=ShipmentEventResponse.model_validate(new_event) if new_event else None,
            message="Shipment advanced" if new_event else "Shipment script already at last step",
        )


@router.post(
    "/sim/carrier/scenarios/{id}/play",
    response_model=CarrierAdvanceResponse,
    summary="Play out a carrier script scenario (seller, demo only)",
)
@router.post(
    "/api/v1/sim/carrier/scenarios/{id}/play",
    response_model=CarrierAdvanceResponse,
    include_in_schema=False,
)
async def play_carrier_scenario(
    id: str,
    body: Optional[PlayScenarioRequest] = None,
    principal: dict = Depends(require_role("seller_owner", "seller_staff")),
) -> CarrierAdvanceResponse:
    _ensure_demo_mode()
    await check_carrier_faults()

    with Session(db.engine) as session:
        # Determine target shipment and script
        target_shipment_id = body.shipment_id if body and body.shipment_id else None
        target_script = body.script_id if body and body.script_id else None

        if id in CARRIER_SCRIPTS:
            target_script = id
        elif not target_shipment_id:
            target_shipment_id = id

        query = session.query(Shipment).options(joinedload(Shipment.events))
        if target_shipment_id:
            shipment = query.filter(
                (Shipment.shipment_id == target_shipment_id)
                | (Shipment.order_id == target_shipment_id)
            ).one_or_none()
        elif target_script:
            shipment = query.filter(Shipment.script_id == target_script).first()
        else:
            shipment = query.first()

        if not shipment:
            raise NotFoundException(message="Shipment not found")

        if target_script and target_script in CARRIER_SCRIPTS:
            shipment.script_id = target_script

        now = Clock.now()
        last_event = None
        while True:
            evt = advance_shipment_in_session(session, shipment, now=now)
            if evt is None:
                break
            last_event = evt

        session.commit()
        session.refresh(shipment)

        return CarrierAdvanceResponse(
            shipment=_build_shipment_response(shipment),
            new_event=ShipmentEventResponse.model_validate(last_event) if last_event else None,
            message="Scenario played",
        )
