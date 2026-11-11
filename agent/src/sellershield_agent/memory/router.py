"""Customer Memory APIRouter supporting memory erasure and management."""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth.deps import get_clock, require_customer
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine, write_tx
from sellershield_agent.domain.state import customer_memory, memory_events
from sellershield_agent.errors import AppHTTPException

router = APIRouter(tags=["memory"])


@router.delete("/me/memory")
def delete_customer_memory(
    principal: Annotated[Principal, Depends(require_customer)],
    clock: Annotated[Clock, Depends(get_clock)],
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Erase long-term customer memory and episodic events for the authenticated caller."""
    customer_id = principal.customer_id
    if not customer_id:
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Customer identity required",
        )

    engine = get_engine()
    now_str = clock.now().isoformat()
    req_id = request.headers.get("x-request-id") if request is not None else None

    with write_tx(engine) as tx:
        tx.execute(
            customer_memory.delete().where(
                customer_memory.c.customer_id == customer_id
            )
        )
        tx.execute(
            memory_events.delete().where(
                memory_events.c.customer_id == customer_id
            )
        )
        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=now_str,
                request_id=req_id,
                session_id=None,
                actor_type=ActorType.CUSTOMER,
                actor_id=customer_id,
                event_type=EventType.EXECUTION_STEP,
                resource_type="customer_memory",
                resource_id=customer_id,
                action="erase_memory",
                outcome=Outcome.SUCCESS,
                policy_version=None,
                details={"deleted": True},
            ),
        )

    return {"erased": True}


__all__ = ["router"]
