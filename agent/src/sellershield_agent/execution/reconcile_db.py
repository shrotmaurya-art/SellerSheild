"""Read-only candidate scanning queries for the background reconciler."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select

from sellershield_agent.db import read_tx
from sellershield_agent.domain.state import ProposalStatus, approvals, executions, proposals
from sellershield_agent.execution.common import SagaPlan, load_saga_plan_from_row, parse_utc


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def scan_reconcile_candidates_sync(
    engine: Engine,
    *,
    now: datetime,
    min_age_seconds: int,
) -> tuple[list[dict[str, Any]], list[tuple[SagaPlan, list[dict[str, Any]]]]]:
    """Read-only scan for eligible executing/unknown steps and orphaned in-flight proposals."""
    with read_tx(engine) as conn:
        pending_rows = conn.execute(
            select(executions).where(
                executions.c.status.in_(("executing", "unknown")),
                executions.c.stuck.is_(False),
            )
        ).fetchall()

        eligible_execs: list[dict[str, Any]] = []
        active_or_stuck_pids: set[str] = set()

        all_stuck = conn.execute(
            select(executions.c.proposal_id).where(executions.c.stuck.is_(True))
        ).fetchall()
        for s_row in all_stuck:
            active_or_stuck_pids.add(str(s_row.proposal_id))

        for row in pending_rows:
            pid = str(row.proposal_id)
            active_or_stuck_pids.add(pid)
            if min_age_seconds <= 0:
                eligible_execs.append(_row_to_dict(row))
                continue
            if row.next_retry_at is not None:
                if now >= parse_utc(str(row.next_retry_at)):
                    eligible_execs.append(_row_to_dict(row))
            else:
                age = (now - parse_utc(str(row.started_at))).total_seconds()
                if age >= min_age_seconds:
                    eligible_execs.append(_row_to_dict(row))

        plans_cache: dict[str, SagaPlan] = {}
        enriched_execs: list[dict[str, Any]] = []
        for ex in eligible_execs:
            pid = str(ex["proposal_id"])
            if pid not in plans_cache:
                p_row = conn.execute(
                    select(proposals).where(proposals.c.proposal_id == pid)
                ).fetchone()
                if p_row is None:
                    continue
                plans_cache[pid] = load_saga_plan_from_row(p_row)
            ex["plan"] = plans_cache[pid]
            enriched_execs.append(ex)

        orphan_rows = conn.execute(
            select(proposals).where(
                proposals.c.status.in_(
                    (ProposalStatus.executing.value, ProposalStatus.unknown.value)
                )
            )
        ).fetchall()

        orphans: list[tuple[SagaPlan, list[dict[str, Any]]]] = []
        for p_row in orphan_rows:
            pid = str(p_row.proposal_id)
            if pid in active_or_stuck_pids:
                continue
            has_apr = (
                conn.execute(
                    select(approvals.c.approval_id).where(approvals.c.proposal_id == pid)
                ).fetchone()
                is not None
            )
            if has_apr:
                continue

            pid_execs = [
                _row_to_dict(r)
                for r in conn.execute(
                    select(executions).where(executions.c.proposal_id == pid)
                ).fetchall()
            ]
            if any(str(ex.get("step")) == "release_refund" for ex in pid_execs):
                continue
            if min_age_seconds > 0:
                latest_ts: datetime | None = None
                for candidate_str in (p_row.confirmed_at, p_row.created_at):
                    if candidate_str:
                        dt = parse_utc(str(candidate_str))
                        if latest_ts is None or dt > latest_ts:
                            latest_ts = dt
                for ex in pid_execs:
                    ts_val = ex.get("finished_at") or ex.get("started_at")
                    if ts_val:
                        dt = parse_utc(str(ts_val))
                        if latest_ts is None or dt > latest_ts:
                            latest_ts = dt
                if latest_ts is not None and (now - latest_ts).total_seconds() < min_age_seconds:
                    continue

            orphans.append((load_saga_plan_from_row(p_row), pid_execs))

        return enriched_execs, orphans

