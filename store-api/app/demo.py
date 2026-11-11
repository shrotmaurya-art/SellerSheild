"""
Demo Control Center Endpoints (spec sections 6, 7, 8, 16)

Routes:
- POST /demo/reset (rebuild seed in under 5 seconds)
- GET and POST /demo/clock (advance hours/days, set exact time, freeze, resume)
- GET and POST /demo/faults (carrier, payment, and mail fault toggles)
- GET /demo/state (table row counts, last 20 events, outbox summary)
- POST /demo/scenarios/{id}/load (load seeded scenario)
- POST /demo/snapshots/save (save SQLite snapshot)
- POST /demo/snapshots/restore (restore SQLite snapshot)
- GET /demo/snapshots (list saved snapshots)

Gating:
- seller_owner role only
- DEMO_MODE must be true (404 not_found otherwise)
"""

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import db
from app.clock import Clock
from app.config import settings
from app.deps import require_role
from app.errors import NotFoundException, ValidationErrorException
from app.faults import clear_faults, is_fault_enabled, set_fault
from app.models import (
    Customer,
    EventsOutbox,
    IdempotencyKey,
    MailMessage,
    Order,
    OrderItem,
    Product,
    Refund,
    Return,
    ReturnItem,
    Shipment,
    ShipmentEvent,
    StoreAudit,
    User,
)
from app.schemas import (
    DemoClockRequest,
    DemoClockResponse,
    DemoFaultRequest,
    DemoFaultsResponse,
    DemoResetResponse,
    DemoScenarioLoadResponse,
    DemoStateResponse,
    EventOutboxResponse,
    OutboxStatusSummary,
    SnapshotActionResponse,
    SnapshotListResponse,
)

logger = logging.getLogger(__name__)

# Ensure scripts folder is on sys.path so we can import seed_database
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from seed import seed_database  # noqa: E402

router = APIRouter(tags=["Demo Control"])

ALL_KNOWN_FAULTS = [
    "carrier.down",
    "carrier.slow",
    "carrier.stale",
    "payment.fail_once",
    "payment.timeout_after_commit",
    "payment.down",
    "mail.fail_once",
    "mail.down",
]

SNAPSHOTS_DIR = REPO_ROOT / "store-api" / "data" / "snapshots"


def _ensure_demo_mode() -> None:
    if not settings.DEMO_MODE:
        raise NotFoundException(message="Endpoint not found")


def _get_current_faults_map() -> dict[str, bool]:
    result: dict[str, bool] = {}
    with Session(db.engine) as session:
        for fault_name in ALL_KNOWN_FAULTS:
            result[fault_name] = is_fault_enabled(fault_name, session)
    return result


# ---------------------------------------------------------------------------
# POST /demo/reset
# ---------------------------------------------------------------------------
@router.post(
    "/demo/reset",
    response_model=DemoResetResponse,
    summary="Rebuild database seed and reset demo environment in under 5s",
)
@router.post(
    "/api/v1/demo/reset",
    response_model=DemoResetResponse,
    include_in_schema=False,
)
def demo_reset(
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> DemoResetResponse:
    _ensure_demo_mode()

    start_time = time.perf_counter()

    # Reset in-memory / persisted clock and simulator faults
    Clock.reset()
    clear_faults()

    # Re-run full database seed
    seed_duration = seed_database(reset=True)
    total_duration = time.perf_counter() - start_time

    logger.info(f"Demo reset completed in {total_duration:.3f}s (seed took {seed_duration:.3f}s)")
    return DemoResetResponse(
        status="ok",
        message="Database reset and seeded successfully",
        duration_seconds=round(total_duration, 4),
    )


# ---------------------------------------------------------------------------
# GET and POST /demo/clock
# ---------------------------------------------------------------------------
@router.get(
    "/demo/clock",
    response_model=DemoClockResponse,
    summary="Get demo clock status (seller_owner only)",
)
@router.get(
    "/api/v1/demo/clock",
    response_model=DemoClockResponse,
    include_in_schema=False,
)
def get_demo_clock(
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> DemoClockResponse:
    _ensure_demo_mode()
    return DemoClockResponse(
        now_utc=Clock.now().isoformat(),
        mode=Clock.mode,
        offset_seconds=Clock.offset_seconds,
    )


@router.post(
    "/demo/clock",
    response_model=DemoClockResponse,
    summary="Control demo clock (advance, set, freeze, resume)",
)
@router.post(
    "/api/v1/demo/clock",
    response_model=DemoClockResponse,
    include_in_schema=False,
)
def control_demo_clock(
    req: DemoClockRequest,
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> DemoClockResponse:
    _ensure_demo_mode()

    action = (req.action or "").lower()

    # Freeze handling
    if action == "freeze" or req.freeze is True:
        at_dt = None
        if req.exact_time:
            try:
                at_dt = datetime.fromisoformat(req.exact_time)
            except Exception as exc:
                raise ValidationErrorException(message=f"Invalid exact_time format: {exc}")
        Clock.freeze(at=at_dt)

    # Resume / Unfreeze handling
    elif action == "resume" or req.freeze is False:
        Clock.unfreeze()

    # Reset handling
    elif action == "reset":
        Clock.reset()

    # Set exact time
    elif action == "set" or (req.exact_time and not req.freeze):
        if not req.exact_time:
            raise ValidationErrorException(message="exact_time must be provided for 'set' action")
        try:
            target_dt = datetime.fromisoformat(req.exact_time)
            if target_dt.tzinfo is None:
                target_dt = target_dt.replace(tzinfo=timezone.utc)
            else:
                target_dt = target_dt.astimezone(timezone.utc)
        except Exception as exc:
            raise ValidationErrorException(message=f"Invalid exact_time format: {exc}")

        current_now = Clock.now()
        diff_seconds = (target_dt - current_now).total_seconds()
        Clock.advance(seconds=diff_seconds)

    # Advance handling
    elif action == "advance" or (req.hours or req.days or req.seconds):
        Clock.advance(
            hours=req.hours or 0.0,
            days=req.days or 0.0,
            seconds=req.seconds or 0.0,
        )

    logger.info(f"Demo clock updated: now_utc={Clock.now().isoformat()}, mode={Clock.mode}")
    return DemoClockResponse(
        now_utc=Clock.now().isoformat(),
        mode=Clock.mode,
        offset_seconds=Clock.offset_seconds,
    )


# ---------------------------------------------------------------------------
# GET and POST /demo/faults
# ---------------------------------------------------------------------------
@router.get(
    "/demo/faults",
    response_model=DemoFaultsResponse,
    summary="Get status of all simulator faults",
)
@router.get(
    "/api/v1/demo/faults",
    response_model=DemoFaultsResponse,
    include_in_schema=False,
)
def get_demo_faults(
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> DemoFaultsResponse:
    _ensure_demo_mode()
    return DemoFaultsResponse(faults=_get_current_faults_map())


@router.post(
    "/demo/faults",
    response_model=DemoFaultsResponse,
    summary="Toggle simulator faults",
)
@router.post(
    "/api/v1/demo/faults",
    response_model=DemoFaultsResponse,
    include_in_schema=False,
)
def set_demo_faults(
    req: DemoFaultRequest,
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> DemoFaultsResponse:
    _ensure_demo_mode()

    if req.clear_all:
        clear_faults()

    if req.fault is not None:
        if req.enabled is None:
            raise ValidationErrorException(
                message="enabled must be specified when fault is provided"
            )
        set_fault(req.fault, req.enabled)

    if req.faults:
        for fault_name, enabled in req.faults.items():
            set_fault(fault_name, enabled)

    return DemoFaultsResponse(faults=_get_current_faults_map())


# ---------------------------------------------------------------------------
# GET /demo/state
# ---------------------------------------------------------------------------
@router.get(
    "/demo/state",
    response_model=DemoStateResponse,
    summary="Inspect state (row counts, last 20 events, outbox status, clock, faults)",
)
@router.get(
    "/api/v1/demo/state",
    response_model=DemoStateResponse,
    include_in_schema=False,
)
def get_demo_state(
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> DemoStateResponse:
    _ensure_demo_mode()

    with Session(db.engine) as session:
        # Table row counts
        row_counts = {
            "users": session.scalar(select(func.count(User.user_id))) or 0,
            "customers": session.scalar(select(func.count(Customer.customer_id))) or 0,
            "orders": session.scalar(select(func.count(Order.order_id))) or 0,
            "order_items": session.scalar(select(func.count(OrderItem.item_id))) or 0,
            "products": session.scalar(select(func.count(Product.sku))) or 0,
            "shipments": session.scalar(select(func.count(Shipment.shipment_id))) or 0,
            "shipment_events": session.scalar(select(func.count(ShipmentEvent.event_id))) or 0,
            "returns": session.scalar(select(func.count(Return.return_id))) or 0,
            "return_items": session.scalar(select(func.count(ReturnItem.item_id))) or 0,
            "refunds": session.scalar(select(func.count(Refund.refund_id))) or 0,
            "mail_messages": session.scalar(select(func.count(MailMessage.message_id))) or 0,
            "events_outbox": session.scalar(select(func.count(EventsOutbox.event_id))) or 0,
            "store_audit": session.scalar(select(func.count(StoreAudit.id))) or 0,
            "idempotency_keys": session.scalar(select(func.count(IdempotencyKey.key))) or 0,
        }

        # Outbox status breakdown
        outbox_total = row_counts["events_outbox"]
        pending_count = (
            session.scalar(
                select(func.count(EventsOutbox.event_id)).where(
                    EventsOutbox.delivered_at.is_(None),
                    EventsOutbox.status == "pending",
                )
            )
            or 0
        )
        delivered_count = (
            session.scalar(
                select(func.count(EventsOutbox.event_id)).where(
                    EventsOutbox.delivered_at.isnot(None)
                )
            )
            or 0
        )
        exhausted_count = (
            session.scalar(
                select(func.count(EventsOutbox.event_id)).where(EventsOutbox.status == "exhausted")
            )
            or 0
        )
        outbox_summary = OutboxStatusSummary(
            total=outbox_total,
            pending=pending_count,
            delivered=delivered_count,
            exhausted=exhausted_count,
        )

        # Last 20 events
        last_events_rows = session.scalars(
            select(EventsOutbox).order_by(EventsOutbox.created_at.desc()).limit(20)
        ).all()
        last_20_events = [EventOutboxResponse.model_validate(r) for r in last_events_rows]

    return DemoStateResponse(
        row_counts=row_counts,
        last_20_events=last_20_events,
        outbox_status=outbox_summary,
        clock=Clock.get_info(),
        faults=_get_current_faults_map(),
    )


# ---------------------------------------------------------------------------
# POST /demo/scenarios/{id}/load
# ---------------------------------------------------------------------------
@router.post(
    "/demo/scenarios/{id}/load",
    response_model=DemoScenarioLoadResponse,
    summary="Load a specific demo scenario",
)
@router.post(
    "/api/v1/demo/scenarios/{id}/load",
    response_model=DemoScenarioLoadResponse,
    include_in_schema=False,
)
def load_demo_scenario(
    id: str,
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> DemoScenarioLoadResponse:
    _ensure_demo_mode()

    valid_scenarios = [f"SC-{i:02d}" for i in range(1, 18)] + ["DEFAULT"]
    norm_id = id.strip().upper()
    if norm_id not in valid_scenarios:
        raise NotFoundException(
            message=f"Scenario '{id}' not found. Valid scenarios: SC-01 through SC-17, DEFAULT"
        )

    # Reset seed to provide fresh scenario world
    Clock.reset()
    clear_faults()
    seed_database(reset=True)

    logger.info(f"Loaded scenario {norm_id}")
    return DemoScenarioLoadResponse(
        status="ok",
        scenario_id=norm_id,
        message=f"Scenario {norm_id} loaded successfully",
    )


# ---------------------------------------------------------------------------
# Snapshot Save / Restore (SQLite file copy)
# ---------------------------------------------------------------------------
@router.post(
    "/demo/snapshots/save",
    response_model=SnapshotActionResponse,
    summary="Save current SQLite database to a snapshot file",
)
@router.post(
    "/api/v1/demo/snapshots/save",
    response_model=SnapshotActionResponse,
    include_in_schema=False,
)
def save_demo_snapshot(
    name: Optional[str] = Query(None, description="Snapshot name"),
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> SnapshotActionResponse:
    _ensure_demo_mode()

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    snap_name = name or f"snapshot_{int(Clock.now().timestamp())}"
    # Sanitize name
    safe_name = "".join(c for c in snap_name if c.isalnum() or c in ("-", "_")).strip()
    if not safe_name:
        safe_name = f"snapshot_{int(Clock.now().timestamp())}"

    target_file = SNAPSHOTS_DIR / f"{safe_name}.db"
    db_file = REPO_ROOT / "store-api" / "data" / "store.db"

    if not db_file.exists():
        raise NotFoundException(message="Database file store.db not found to snapshot")

    # Use SQLite online backup API to cleanly capture WAL and DB state
    src_conn = sqlite3.connect(str(db_file))
    try:
        dst_conn = sqlite3.connect(str(target_file))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    logger.info(f"Saved database snapshot to {target_file}")
    return SnapshotActionResponse(
        status="ok",
        snapshot=safe_name,
        message=f"Snapshot '{safe_name}' saved successfully",
    )


@router.post(
    "/demo/snapshots/restore",
    response_model=SnapshotActionResponse,
    summary="Restore SQLite database from a snapshot file",
)
@router.post(
    "/api/v1/demo/snapshots/restore",
    response_model=SnapshotActionResponse,
    include_in_schema=False,
)
def restore_demo_snapshot(
    name: str = Query(..., description="Snapshot name to restore"),
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> SnapshotActionResponse:
    _ensure_demo_mode()

    safe_name = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()
    target_file = SNAPSHOTS_DIR / f"{safe_name}.db"

    if not target_file.exists():
        raise NotFoundException(message=f"Snapshot '{name}' not found")

    db_dir = REPO_ROOT / "store-api" / "data"
    db_file = db_dir / "store.db"

    # Dispose active SQLAlchemy engine connections
    db.engine.dispose()

    # Use SQLite online backup API to cleanly restore into store.db
    src_conn = sqlite3.connect(str(target_file))
    try:
        dst_conn = sqlite3.connect(str(db_file))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    logger.info(f"Restored database from snapshot {target_file}")
    return SnapshotActionResponse(
        status="ok",
        snapshot=safe_name,
        message=f"Snapshot '{safe_name}' restored successfully",
    )


@router.get(
    "/demo/snapshots",
    response_model=SnapshotListResponse,
    summary="List available database snapshots",
)
@router.get(
    "/api/v1/demo/snapshots",
    response_model=SnapshotListResponse,
    include_in_schema=False,
)
def list_demo_snapshots(
    principal: dict[str, Any] = Depends(require_role("seller_owner")),
) -> SnapshotListResponse:
    _ensure_demo_mode()

    if not SNAPSHOTS_DIR.exists():
        return SnapshotListResponse(snapshots=[])

    snapshots = [f.stem for f in SNAPSHOTS_DIR.glob("*.db") if f.is_file()]
    snapshots.sort()
    return SnapshotListResponse(snapshots=snapshots)
