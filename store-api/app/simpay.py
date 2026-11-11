"""
SimPay Payment Simulator (spec section 7.2)

- Releasing a refund moves it to 'processing' and completes it after about 3s
  of real time in a background asyncio task.
- gateway_ref format: SIMPAY-xxxxxx.
- Every UI and API response carries simulated=true.
- Faults:
  - payment.fail_once (first attempt fails; retry with same key succeeds)
  - payment.timeout_after_commit (refund applied in DB but response is lost/timed out)
  - payment.down (SimPay 503 upstream unavailable)
"""

import asyncio
import logging
import secrets

from sqlalchemy.orm import Session

import db
from app.clock import Clock
from app.faults import clear_faults, is_fault_enabled, set_fault
from app.models import Refund, Return
from app.outbox import emit_outbox_event

logger = logging.getLogger(__name__)

__all__ = [
    "clear_faults",
    "is_fault_enabled",
    "set_fault",
    "generate_gateway_ref",
    "complete_simpay_refund",
]


def generate_gateway_ref() -> str:
    """Generate SIMPAY-xxxxxx reference."""
    token = secrets.token_hex(3).upper()  # 6 hex chars
    return f"SIMPAY-{token}"


async def complete_simpay_refund(refund_id: str, delay_seconds: float = 3.0) -> None:
    """
    Background asyncio task to complete refund after real-time delay.
    Never holds a write transaction across await asyncio.sleep.
    """
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)

    with Session(db.engine) as session:
        refund = session.get(Refund, refund_id)
        if refund is None or refund.status != "processing":
            return

        now = Clock.now()
        refund.status = "completed"
        if not refund.gateway_ref:
            refund.gateway_ref = generate_gateway_ref()
        refund.updated_at = now
        refund.version += 1

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
                "gateway_ref": refund.gateway_ref,
            },
            resource_version=refund.version,
        )

        # If linked to a return that was inspected_ok, advance to completed
        if refund.return_id:
            ret = session.get(Return, refund.return_id)
            if ret and ret.status == "inspected_ok":
                ret.status = "completed"
                ret.updated_at = now
                ret.version += 1
                for item in ret.items:
                    item.status = "completed"
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

        session.commit()
        logger.info(f"SimPay completed refund {refund_id} with gateway_ref {refund.gateway_ref}")
