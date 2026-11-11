"""Decision Trace assembly for seller request lifecycle transparency."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, select

from sellershield_agent.audit.models import audit_events
from sellershield_agent.audit.sanitize import sanitize_trace_data
from sellershield_agent.clock import Clock
from sellershield_agent.db import read_tx
from sellershield_agent.domain.state import (
    approvals,
    llm_calls,
    policy_decisions,
    proposals,
    tool_calls,
)
from sellershield_agent.errors import AppHTTPException

STAGE_ORDER: dict[str, int] = {
    "understanding": 0,
    "plan": 1,
    "tool_call": 2,
    "rule_table": 3,
    "proposal": 4,
    "confirmation": 5,
    "execution_step": 6,
    "approval": 7,
    "final_reply": 8,
    "verifier_outcome": 9,
    "notification": 10,
}


def _event_stage(
    event_type: str, action: str, resource_type: str | None, details: dict[str, Any]
) -> str:
    """Map an audit row or synthetic item to its canonical stage name."""
    if details.get("stage"):
        return str(details["stage"])
    if action == "understanding" or "understanding" in action:
        return "understanding"
    if action == "plan":
        return "plan"
    if event_type == "tool_call" or action == "tool_call":
        return "tool_call"
    if event_type == "policy_decision" or action == "record_policy_decision":
        return "rule_table"
    if event_type == "proposal_created" or action == "proposal_created":
        return "proposal"
    if event_type == "proposal_confirmed" or action == "proposal_confirmed":
        return "confirmation"
    if event_type == "execution_step" or "execution_step" in action:
        return "execution_step"
    if (
        event_type in ("approval_created", "approval_decided")
        or "approval" in action
        or resource_type == "approvals"
    ):
        return "approval"
    if action in ("final_reply", "agent_reply"):
        return "final_reply"
    if action in ("verifier_outcome", "verify_draft"):
        return "verifier_outcome"
    if event_type == "notification_stub" or resource_type == "notifications_outbox":
        return "notification"
    return action or event_type


def build_decision_trace_sync(
    engine: Engine,
    request_id: str,
    clock: Clock,
) -> dict[str, Any]:
    """Assemble complete Decision Trace for a request across telemetry and audit tables."""
    with read_tx(engine) as conn:
        direct_events = conn.execute(
            select(audit_events)
            .where(audit_events.c.request_id == request_id)
            .order_by(audit_events.c.seq.asc())
        ).fetchall()
        direct_llm = conn.execute(
            select(llm_calls)
            .where(llm_calls.c.request_id == request_id)
            .order_by(llm_calls.c.created_at.asc())
        ).fetchall()

        if not direct_events and not direct_llm:
            raise AppHTTPException(
                status_code=404,
                code="not_found",
                message=f"Decision trace for request '{request_id}' not found",
            )

        # IDOR prevention: if request is an auth denial / rejected action,
        # do not link to cross-customer objects
        is_denial = any(
            str(e.event_type) == "auth_denied" or str(e.outcome) == "denied"
            for e in direct_events
        )

        proposal_ids: set[str] = set()
        if not is_denial:
            for ev in direct_events:
                if str(ev.resource_type) == "proposals" and ev.resource_id:
                    proposal_ids.add(str(ev.resource_id))
                if ev.details_json:
                    try:
                        d = json.loads(str(ev.details_json))
                        if isinstance(d, dict) and d.get("proposal_id"):
                            proposal_ids.add(str(d["proposal_id"]))
                    except Exception:
                        pass

        # Expand to related approvals and executions only for authorized proposals
        approval_ids: set[str] = set()
        if proposal_ids:
            apr_rows = conn.execute(
                select(approvals.c.approval_id).where(
                    approvals.c.proposal_id.in_(list(proposal_ids))
                )
            ).fetchall()
            approval_ids = {str(r[0]) for r in apr_rows}

        # Gather all related audit rows
        all_event_rows = list(direct_events)
        if not is_denial and (proposal_ids or approval_ids):
            extra_events = conn.execute(
                select(audit_events)
                .where(
                    audit_events.c.resource_id.in_(list(proposal_ids | approval_ids)),
                    audit_events.c.event_type != "auth_denied",
                    audit_events.c.outcome != "denied",
                )
                .order_by(audit_events.c.seq.asc())
            ).fetchall()
            seen_seqs = {r.seq for r in all_event_rows}
            for ev in extra_events:
                if ev.seq not in seen_seqs:
                    all_event_rows.append(ev)
                    seen_seqs.add(ev.seq)

        # Fetch telemetry: llm_calls, tool_calls, policy_decisions
        l_rows = list(direct_llm)
        t_rows = conn.execute(
            select(tool_calls).where(tool_calls.c.request_id == request_id)
        ).fetchall()

        pdec_rows = list(
            conn.execute(
                select(policy_decisions).where(policy_decisions.c.request_id == request_id)
            ).fetchall()
        )
        if not is_denial and not pdec_rows and proposal_ids:
            # Check if policy_decisions can be matched via proposals
            props = conn.execute(
                select(proposals.c.order_id, proposals.c.customer_id).where(
                    proposals.c.proposal_id.in_(list(proposal_ids))
                )
            ).fetchall()
            for p in props:
                pdecs = conn.execute(
                    select(policy_decisions).where(
                        policy_decisions.c.order_id == p[0],
                        policy_decisions.c.customer_id == p[1],
                    )
                ).fetchall()
                pdec_rows.extend(pdecs)

    # Format telemetry
    llm_out: list[dict[str, Any]] = []
    for lr in l_rows:
        llm_out.append(
            {
                "call_id": lr.call_id,
                "stage": lr.stage,
                "provider": lr.provider,
                "model": lr.model,
                "prompt_hash": lr.prompt_hash,
                "tokens_in": lr.tokens_in,
                "tokens_out": lr.tokens_out,
                "latency_ms": lr.latency_ms,
                "status": lr.status,
                "created_at": lr.created_at,
            }
        )

    tool_out: list[dict[str, Any]] = []
    for tr in t_rows:
        redacted = {}
        if tr.args_redacted_json:
            try:
                redacted = json.loads(str(tr.args_redacted_json))
            except Exception:
                redacted = {"raw": str(tr.args_redacted_json)}
        tool_out.append(
            {
                "call_id": tr.call_id,
                "tool": tr.tool,
                "args_redacted": redacted,
                "args_hash": tr.args_hash,
                "status": tr.status,
                "error_code": tr.error_code,
                "latency_ms": tr.latency_ms,
                "created_at": tr.created_at,
            }
        )

    pdec_out: list[dict[str, Any]] = []
    for pr in pdec_rows:
        f_json = {}
        d_json = {}
        try:
            f_json = json.loads(str(pr.facts_json))
        except Exception:
            f_json = {}
        try:
            d_json = json.loads(str(pr.decision_json))
        except Exception:
            d_json = {}
        pdec_out.append(
            {
                "decision_id": pr.decision_id,
                "order_id": pr.order_id,
                "customer_id": pr.customer_id,
                "policy_version": pr.policy_version,
                "facts": f_json,
                "decision": d_json,
                "created_at": pr.created_at,
            }
        )

    # Format audit timeline events
    timeline: list[dict[str, Any]] = []
    for ev in all_event_rows:
        d_val: dict[str, Any] = {}
        if ev.details_json:
            try:
                d_val = json.loads(str(ev.details_json))
            except Exception:
                d_val = {}
        stage_name = _event_stage(
            str(ev.event_type), str(ev.action), ev.resource_type, d_val
        )
        timeline.append(
            {
                "seq": int(ev.seq),
                "event_id": str(ev.event_id),
                "event_type": str(ev.event_type),
                "stage": stage_name,
                "actor": str(ev.actor_id or ev.actor_type),
                "action": str(ev.action),
                "outcome": str(ev.outcome),
                "resource_type": str(ev.resource_type or ""),
                "resource_id": str(ev.resource_id or ""),
                "details": d_val,
                "created_at": str(ev.ts_utc),
            }
        )

    # Synthesize missing canonical stages if they exist in telemetry but lack an audit row
    present_stages = {str(item["stage"]) for item in timeline}
    for t_item in tool_out:
        if "tool_call" not in present_stages:
            timeline.append(
                {
                    "seq": 0,
                    "event_id": f"syn-tc-{t_item['call_id']}",
                    "event_type": "tool_call",
                    "stage": "tool_call",
                    "actor": "customer",
                    "action": t_item["tool"],
                    "outcome": t_item["status"],
                    "resource_type": "tool",
                    "resource_id": t_item["tool"],
                    "details": t_item["args_redacted"],
                    "created_at": t_item["created_at"],
                }
            )
            present_stages.add("tool_call")

    for p_item in pdec_out:
        if "rule_table" not in present_stages:
            timeline.append(
                {
                    "seq": 0,
                    "event_id": f"syn-dec-{p_item['decision_id']}",
                    "event_type": "policy_decision",
                    "stage": "rule_table",
                    "actor": "system",
                    "action": "evaluate_policy",
                    "outcome": "success",
                    "resource_type": "policy_decisions",
                    "resource_id": p_item["decision_id"],
                    "details": p_item["decision"],
                    "created_at": p_item["created_at"],
                }
            )
            present_stages.add("rule_table")

    # Sort strictly by canonical stage order, breaking ties by sequence number
    timeline.sort(
        key=lambda item: (STAGE_ORDER.get(str(item["stage"]), 99), int(item.get("seq", 0)))
    )

    ts_now = clock.now().isoformat()
    raw_response: dict[str, Any] = {
        "request_id": request_id,
        "timestamp": ts_now,
        "llm_calls": llm_out,
        "tool_calls": tool_out,
        "policy_decisions": pdec_out,
        "audit_events": timeline,
    }

    # Pass through strict PII and token sanitization
    return sanitize_trace_data(raw_response)  # type: ignore[no-any-return]
