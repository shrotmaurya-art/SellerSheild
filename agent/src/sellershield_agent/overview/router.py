"""Seller overview endpoint exposing operational KPIs, queues, and activity counts.

Invariants:
- Seller role required (seller_staff or seller_owner); 401 unauthenticated, 403 customer/service.
- IDs only: zero customer names, emails, phone numbers, or internal limits in values.
- Zeros, empty lists, and null last_monitor_run on an empty database.
- IST (Asia/Kolkata) date partition for today metrics using injected Clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from sellershield_agent.auth import deps as auth_deps
from sellershield_agent.auth.deps import require_seller
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine, read_tx
from sellershield_agent.domain.session import sessions
from sellershield_agent.domain.state import (
    approvals,
    cases,
    executions,
    monitor_runs,
    notifications_outbox,
    policies,
    proposals,
)

router = APIRouter(tags=["seller"])

try:
    IST_TZ = ZoneInfo("Asia/Kolkata")
except Exception:
    IST_TZ = timezone(timedelta(hours=5, minutes=30))  # type: ignore[assignment]


def _parse_to_utc(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        clean = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def _is_today_ist(ts_str: str | None, clock: Clock) -> bool:
    dt_utc = _parse_to_utc(ts_str)
    if dt_utc is None:
        return False
    target_date = dt_utc.astimezone(IST_TZ).date()
    current_date = clock.now().astimezone(IST_TZ).date()
    return target_date == current_date


class SellerOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending_approvals_count: int
    pending_approval_ids: list[str]
    overdue_approvals_count: int
    overdue_approval_ids: list[str]
    open_cases_count: int
    open_cases_by_priority: dict[str, int]
    open_case_ids_by_priority: dict[str, list[str]]
    failed_notifications_count: int
    failed_notification_ids: list[str]
    last_monitor_run: dict[str, Any] | None
    today_conversations_count: int
    today_conversation_ids: list[str]
    today_proposals_count: int
    today_proposal_ids: list[str]
    today_executions_count: int
    today_execution_ids: list[str]
    auto_resolved_rate: float
    active_policy_version: str


@router.get("/seller/overview", response_model=SellerOverviewResponse)
def get_seller_overview(
    principal: Annotated[Principal, Depends(require_seller)],
    clock: Annotated[Clock, Depends(auth_deps.get_clock)],
) -> SellerOverviewResponse:
    """Return dashboard KPI tiles, queue lengths, and ID-only lists for sellers."""
    engine = get_engine()
    now_dt = clock.now()

    with read_tx(engine) as conn:
        # 1. Approvals: pending and overdue
        apr_rows = conn.execute(
            select(approvals.c.approval_id, approvals.c.due_at, approvals.c.status).where(
                approvals.c.status == "pending"
            )
        ).fetchall()

        pending_ids: list[str] = []
        overdue_ids: list[str] = []
        for r in apr_rows:
            aid = str(r[0])
            pending_ids.append(aid)
            due_utc = _parse_to_utc(str(r[1]) if r[1] is not None else None)
            if due_utc is not None and due_utc <= now_dt:
                overdue_ids.append(aid)

        pending_ids.sort()
        overdue_ids.sort()

        # 2. Cases: open (excluding resolved and closed)
        case_rows = conn.execute(
            select(cases.c.case_id, cases.c.priority, cases.c.status).where(
                cases.c.status.not_in(["resolved", "closed"])
            )
        ).fetchall()

        open_high_ids: list[str] = []
        open_normal_ids: list[str] = []
        for r in case_rows:
            cid = str(r[0])
            prio = str(r[1]).lower()
            if prio == "high":
                open_high_ids.append(cid)
            else:
                open_normal_ids.append(cid)

        open_high_ids.sort()
        open_normal_ids.sort()
        open_cases_count = len(open_high_ids) + len(open_normal_ids)

        # 3. Failed notifications: failed or exhausted
        notif_rows = conn.execute(
            select(notifications_outbox.c.notification_id).where(
                notifications_outbox.c.status.in_(["failed", "exhausted"])
            )
        ).fetchall()
        failed_notif_ids = sorted(str(r[0]) for r in notif_rows)

        # 4. Last monitor run
        last_run_data: dict[str, Any] | None = None
        try:
            mrun_row = conn.execute(
                select(
                    monitor_runs.c.run_id,
                    monitor_runs.c.scanned,
                    monitor_runs.c.flagged,
                    monitor_runs.c.started_at,
                    monitor_runs.c.summary_json,
                ).order_by(monitor_runs.c.started_at.desc(), monitor_runs.c.run_id.desc()).limit(1)
            ).fetchone()
            if mrun_row is not None:
                actions_taken: list[str] = []
                if mrun_row[4]:
                    try:
                        pld = json.loads(str(mrun_row[4]))
                        if isinstance(pld, dict) and "actions_taken" in pld:
                            actions_taken = pld["actions_taken"]
                    except Exception:
                        pass
                last_run_data = {
                    "run_id": str(mrun_row[0]),
                    "checked_orders_count": int(mrun_row[1] or 0),
                    "anomalies_detected": int(mrun_row[2] or 0),
                    "actions_taken": actions_taken,
                    "created_at": str(mrun_row[3]),
                }
        except Exception:
            last_run_data = None

        # 5. Today metrics (IST calendar date partition)
        # Sessions / Conversations
        ses_rows = conn.execute(
            select(sessions.c.id, sessions.c.started_at)
        ).fetchall()
        today_conv_ids = sorted(
            str(r[0])
            for r in ses_rows
            if _is_today_ist(
                str(r[1]) if r[1] is not None else None, clock
            )
        )

        # Proposals
        prop_rows = conn.execute(
            select(
                proposals.c.proposal_id,
                proposals.c.created_at,
                proposals.c.decision,
            )
        ).fetchall()
        today_prop_ids = sorted(
            str(r[0])
            for r in prop_rows
            if _is_today_ist(
                str(r[1]) if r[1] is not None else None, clock
            )
        )

        # Executions
        exec_rows = conn.execute(
            select(
                executions.c.execution_id,
                executions.c.started_at,
            )
        ).fetchall()
        today_exec_ids = sorted(
            str(r[0])
            for r in exec_rows
            if _is_today_ist(
                str(r[1]) if r[1] is not None else None, clock
            )
        )

        # 6. Auto-resolved rate
        # Auto-resolved: proposals with decision 'allow'
        auto_resolved_count = sum(
            1
            for r in prop_rows
            if str(r[2]).lower() == "allow"
            and _is_today_ist(str(r[1]), clock)
        )
        total_today = len(today_prop_ids)
        if total_today > 0:
            auto_rate = round(
                float(auto_resolved_count) / float(total_today),
                4,
            )
        else:
            auto_rate = 0.0

        # 7. Active policy version
        active_ver = "v3"
        try:
            pol_row = conn.execute(
                select(policies.c.version)
                .where(policies.c.status == "active")
                .order_by(policies.c.activated_at.desc())
                .limit(1)
            ).fetchone()
            if pol_row is not None and pol_row[0]:
                active_ver = str(pol_row[0])
        except Exception:
            active_ver = "v3"

    return SellerOverviewResponse(
        pending_approvals_count=len(pending_ids),
        pending_approval_ids=pending_ids,
        overdue_approvals_count=len(overdue_ids),
        overdue_approval_ids=overdue_ids,
        open_cases_count=open_cases_count,
        open_cases_by_priority={"high": len(open_high_ids), "normal": len(open_normal_ids)},
        open_case_ids_by_priority={"high": open_high_ids, "normal": open_normal_ids},
        failed_notifications_count=len(failed_notif_ids),
        failed_notification_ids=failed_notif_ids,
        last_monitor_run=last_run_data,
        today_conversations_count=len(today_conv_ids),
        today_conversation_ids=today_conv_ids,
        today_proposals_count=len(today_prop_ids),
        today_proposal_ids=today_prop_ids,
        today_executions_count=len(today_exec_ids),
        today_execution_ids=today_exec_ids,
        auto_resolved_rate=auto_rate,
        active_policy_version=active_ver,
    )

