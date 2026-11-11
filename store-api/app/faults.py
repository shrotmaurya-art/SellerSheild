"""
Simulated Fault Switches (spec section 7.1, 7.2, 7.3, 8)

Fault switches in code:
- carrier.down (503 upstream_unavailable)
- carrier.stale (returns stale events / no new events)
- carrier.slow (8 s latency)
- mail.fail_once (first call fails with 503, then clears)
- mail.down (503 upstream_unavailable)
- payment.fail_once
- payment.timeout_after_commit
- payment.down
"""

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import db
from app.models import SimFault

logger = logging.getLogger(__name__)

# In-memory fast path and test overrides
_MEMORY_FAULTS: dict[str, bool] = {}


def set_fault(name: str, enabled: bool, session: Optional[Session] = None) -> None:
    """Enable or disable a simulator fault in memory and database."""
    _MEMORY_FAULTS[name] = enabled
    try:
        if session is not None:
            fault = session.get(SimFault, name)
            if fault:
                fault.enabled = enabled
            else:
                fault = SimFault(
                    name=name,
                    mode="boolean",
                    params_json="{}",
                    enabled=enabled,
                )
                session.add(fault)
        else:
            with Session(db.engine) as s:
                fault = s.get(SimFault, name)
                if fault:
                    fault.enabled = enabled
                else:
                    fault = SimFault(
                        name=name,
                        mode="boolean",
                        params_json="{}",
                        enabled=enabled,
                    )
                    s.add(fault)
                s.commit()
    except Exception as exc:
        logger.warning(f"Could not persist sim fault {name} to DB: {exc}")


def clear_faults() -> None:
    """Clear all simulator fault switches in memory and database."""
    _MEMORY_FAULTS.clear()
    try:
        with Session(db.engine) as session:
            faults = session.scalars(select(SimFault)).all()
            for f in faults:
                f.enabled = False
            session.commit()
    except Exception as exc:
        logger.warning(f"Could not clear sim faults in DB: {exc}")


def is_fault_enabled(name: str, session: Optional[Session] = None) -> bool:
    """Check if a simulator fault is currently enabled."""
    if name in _MEMORY_FAULTS:
        return _MEMORY_FAULTS[name]
    try:
        if session is not None:
            fault = session.get(SimFault, name)
            enabled = bool(fault and fault.enabled)
            _MEMORY_FAULTS[name] = enabled
            return enabled
        else:
            with Session(db.engine) as s:
                fault = s.get(SimFault, name)
                enabled = bool(fault and fault.enabled)
                _MEMORY_FAULTS[name] = enabled
                return enabled
    except Exception:
        return False
