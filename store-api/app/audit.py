from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.clock import Clock
from app.models import StoreAudit


def log_audit(
    session: Session,
    request_id: str,
    actor: str,
    action: str,
    resource: str,
    outcome: str = "denied",
    ts: Optional[datetime] = None,
) -> None:
    """
    Log an entry to store_audit.
    Enforces that business time comes from Clock.now() if ts is not supplied.
    """
    audit = StoreAudit(
        ts=ts or Clock.now(),
        request_id=request_id or "unknown",
        actor=actor or "anonymous",
        action=action,
        resource=resource,
        outcome=outcome,
    )
    session.add(audit)
    session.commit()
