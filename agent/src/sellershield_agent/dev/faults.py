"""In-memory developer fault injection engine (DEV_MODE only).

Supported faults:
- llm_down: forces chat into guided mode (Spec §14.7, T16)
- audit_fail: forces AuditService.append() to fail (T20)
- mail_down: forces outbox mail sending to fail and retry without rolling back business state
- slow_llm: adds latency to LLM calls
- off: clears all active faults
"""

from __future__ import annotations

import os
import threading
from typing import Any

from sellershield_agent.config import get_settings


class DevFaultAuditError(RuntimeError):
    """Raised when an audit write is blocked by the audit_fail fault injection."""

    pass


KNOWN_FAULTS: tuple[str, ...] = ("llm_down", "audit_fail", "mail_down", "slow_llm")

_faults_lock = threading.Lock()
_active_faults: set[str] = set()


def set_fault(name: str, enabled: bool = True) -> None:
    """Set or toggle an in-memory fault flag.

    If name == 'off', all faults are disabled.
    """
    clean_name = name.strip().lower()
    with _faults_lock:
        if clean_name == "off":
            _active_faults.clear()
            return
        if clean_name in KNOWN_FAULTS:
            if enabled:
                _active_faults.add(clean_name)
            else:
                _active_faults.discard(clean_name)


def clear_faults() -> None:
    """Clear all in-memory developer faults."""
    with _faults_lock:
        _active_faults.clear()


def is_fault_active(name: str) -> bool:
    """Check if a specific fault is active. In-memory faults are only active in DEV_MODE."""
    try:
        settings = get_settings()
        if not settings.DEV_MODE:
            return False
    except Exception:
        if os.getenv("DEV_MODE", "false").lower() not in ("true", "1"):
            return False

    clean_name = name.strip().lower()
    with _faults_lock:
        if clean_name in _active_faults:
            return True

    # Check env var fallback for llm_down (used by tests like test_monitor)
    if clean_name == "llm_down":
        try:
            settings = get_settings()
            if getattr(settings, "DEV_FAULT_LLM_DOWN", False):
                return True
        except Exception:
            pass
        if os.getenv("DEV_FAULT_LLM_DOWN", "false").lower() in ("true", "1"):
            return True

    return False


def get_faults_map() -> dict[str, bool]:
    """Return map of all known faults to their active boolean states."""
    return {f: is_fault_active(f) for f in KNOWN_FAULTS}


def list_faults() -> list[dict[str, Any]]:
    """Return fault list matching contracts/openapi-agent.yaml DevFaultsResponse."""
    return [
        {"name": f, "enabled": is_fault_active(f)}
        for f in ("llm_down", "audit_fail", "slow_llm")
    ]

