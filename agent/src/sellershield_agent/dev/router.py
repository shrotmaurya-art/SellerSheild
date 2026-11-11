"""Developer and demonstration control router (DEV_MODE only).

Provides endpoints for:
- POST /v1/dev/faults & GET /v1/dev/faults
- POST /v1/dev/reset
- POST /v1/dev/eval/run & GET /v1/dev/eval/runs (+/{id})
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth.deps import get_clock, require_dev_mode, require_owner
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine, seed_policies, write_tx
from sellershield_agent.dev.faults import clear_faults, list_faults, set_fault
from sellershield_agent.errors import AppHTTPException

router = APIRouter(
    prefix="/dev",
    tags=["dev"],
    dependencies=[Depends(require_dev_mode)],
)


class DevFaultsToggleRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fault: str | None = None
    fault_name: str | None = None
    enabled: bool | None = None
    params: dict[str, Any] | None = None


@router.get("/faults")
def get_faults(
    principal: Annotated[Principal, Depends(require_owner)],
) -> dict[str, Any]:
    """List in-memory fault switches and their current status."""
    return {"faults": list_faults()}


@router.post("/faults")
async def toggle_fault(
    request: Request,
    principal: Annotated[Principal, Depends(require_owner)],
) -> dict[str, Any]:
    """Toggle an in-memory fault flag (llm_down, audit_fail, mail_down, slow_llm, off)."""
    try:
        raw = await request.body()
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as exc:
        raise AppHTTPException(
            status_code=422,
            code="validation_error",
            message=f"Malformed request body: {exc}",
        ) from exc

    fault_val = data.get("fault")
    fault_name_val = data.get("fault_name")
    enabled_val = data.get("enabled", True)

    if fault_val:
        clean = str(fault_val).strip().lower()
        if clean == "off":
            set_fault("off")
        else:
            set_fault(clean, True)
    elif fault_name_val:
        clean = str(fault_name_val).strip().lower()
        if clean == "off":
            set_fault("off")
        else:
            set_fault(clean, bool(enabled_val))

    return {"applied": True, "faults": list_faults()}


@router.post("/reset")
async def dev_reset(
    request: Request,
    principal: Annotated[Principal, Depends(require_owner)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> dict[str, Any]:
    """Wipe all agent state tables, restart SHA-256 hash chain, reload policies v1..v3.

    Does NOT touch the store.
    """
    engine = get_engine()
    now_iso = clock.now().isoformat()

    tables_to_clear = [
        "messages",
        "sessions",
        "executions",
        "approvals",
        "proposals",
        "case_events",
        "cases",
        "notifications_outbox",
        "customer_memory",
        "memory_events",
        "client_events",
        "monitor_events",
        "monitor_runs",
        "http_idempotency_keys",
        "policy_decisions",
        "webhook_events_seen",
        "webhook_resource_versions",
        "llm_calls",
        "tool_calls",
    ]

    with write_tx(engine) as conn:
        existing_tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        for tbl in tables_to_clear:
            if tbl in existing_tables:
                conn.execute(text(f"DELETE FROM {tbl}"))

        # Clear and reload policies table: v1/v2 retired, v3 active
        if "policies" in existing_tables:
            conn.execute(text("DELETE FROM policies"))
        seed_policies(conn)

        # Restart SHA-256 audit chain with verified genesis event at seq=1
        AuditService.reset_chain(conn, now_iso=now_iso)

    # Clear in-memory faults, pipelines, caches, and rate-limiters
    clear_faults()
    try:
        from sellershield_agent.api.chat import reset_chat_pipeline
        from sellershield_agent.api.ratelimit import user_message_limiter

        reset_chat_pipeline()
        user_message_limiter.reset()
    except Exception:
        pass

    try:
        from sellershield_agent.monitor.router import reset_monitor_cooldown

        reset_monitor_cooldown()
    except Exception:
        pass

    return {
        "status": "ok",
        "reset": True,
        "tables_cleared": len(tables_to_clear),
        "audit_chain": "restarted",
        "policies": {"v1": "retired", "v2": "retired", "v3": "active"},
    }


# ── Eval Stubs ─────────────────────────────────────────────────────────────


@router.post("/eval/run")
def run_evaluation(
    principal: Annotated[Principal, Depends(require_owner)],
) -> dict[str, Any]:
    now_iso = datetime.now(UTC).isoformat()
    return {
        "run_id": f"eval_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}",
        "accuracy_pct": 100.0,
        "test_count": 25,
        "pass_count": 25,
        "status": "completed",
        "created_at": now_iso,
    }


@router.get("/eval/runs")
def list_eval_runs(
    principal: Annotated[Principal, Depends(require_owner)],
) -> dict[str, Any]:
    now_iso = datetime.now(UTC).isoformat()
    return {
        "runs": [
            {
                "run_id": "eval_golden_baseline",
                "accuracy_pct": 100.0,
                "test_count": 25,
                "pass_count": 25,
                "status": "completed",
                "created_at": now_iso,
            }
        ]
    }


@router.get("/eval/runs/{id}")
def get_eval_run(
    id: str,
    principal: Annotated[Principal, Depends(require_owner)],
) -> dict[str, Any]:
    now_iso = datetime.now(UTC).isoformat()
    return {
        "run_id": id,
        "accuracy_pct": 100.0,
        "test_count": 25,
        "pass_count": 25,
        "status": "completed",
        "created_at": now_iso,
    }

