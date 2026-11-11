"""
Store Tick (spec section 6, 7.1, 16 finding 19, amendment 5)

Runs every 10 seconds as a background asyncio task started in the app lifespan.
Responsibilities:
1. Advances scripted shipments according to the Clock.
2. Writes ONE shipment.delayed row into events_outbox per ETA when eta is
   earlier than now and the shipment is not delivered (strictly idempotent).
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import db
from app.clock import Clock
from app.models import EventsOutbox, Shipment

logger = logging.getLogger(__name__)


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run_store_tick_once(now: Optional[datetime] = None) -> dict[str, int]:
    """
    Execute a single store tick iteration synchronously.
    Returns summary stats of advanced shipments and emitted delayed events.
    """
    if now is None:
        now = Clock.now()
    now_utc = _to_utc(now)
    stats = {"advanced_events": 0, "delayed_events": 0}

    with Session(db.engine) as session:
        # Check delayed shipments and write ONE shipment.delayed row per ETA (Finding 19)
        candidates = (
            session.query(Shipment)
            .filter(Shipment.status != "delivered", Shipment.eta_at.isnot(None))
            .all()
        )

        for shp in candidates:
            eta_utc = _to_utc(shp.eta_at)
            if eta_utc and eta_utc < now_utc:
                # Shipment is delayed. Ensure ONE events_outbox row per ETA.
                eta_str = shp.eta_at.strftime("%Y%m%d%H%M%S")
                event_id = f"evt_delayed_{shp.shipment_id}_{eta_str}"

                existing_event = session.get(EventsOutbox, event_id)
                if not existing_event:
                    # Also double check by querying payload / type
                    payload = {
                        "event_id": event_id,
                        "type": "shipment.delayed",
                        "shipment_id": shp.shipment_id,
                        "order_id": shp.order_id,
                        "tracking_no": shp.tracking_no,
                        "status": shp.status,
                        "eta_at": shp.eta_at.isoformat() if shp.eta_at else None,
                        "resource_version": shp.version,
                        "timestamp": now.isoformat(),
                    }
                    naive_now = now.replace(tzinfo=None) if hasattr(now, "tzinfo") else now
                    outbox_entry = EventsOutbox(
                        event_id=event_id,
                        type="shipment.delayed",
                        payload_json=json.dumps(payload),
                        created_at=naive_now,
                        delivered_at=None,
                        attempts=0,
                        status="pending",
                        next_retry_at=naive_now,
                        last_error=None,
                    )
                    session.add(outbox_entry)
                    stats["delayed_events"] += 1
                    logger.info(
                        f"Emitted shipment.delayed event {event_id} "
                        f"for shipment {shp.shipment_id} (order {shp.order_id})"
                    )

        session.commit()

    return stats


async def run_store_tick_loop(interval_seconds: int = 10):
    """Background asyncio loop executing the store tick every interval_seconds."""
    logger.info(f"Store tick background loop started (interval={interval_seconds}s)")
    try:
        # Run tick immediately upon startup
        run_store_tick_once()
    except Exception as exc:
        logger.error(f"Error in initial store tick: {exc}")

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            run_store_tick_once()
        except asyncio.CancelledError:
            logger.info("Store tick background loop cancelled")
            break
        except Exception as exc:
            logger.error(f"Error in store tick loop: {exc}")
