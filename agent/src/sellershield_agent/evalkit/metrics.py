"""Pure metric calculation functions for Lite Evaluation kit (L4.2)."""

from __future__ import annotations

import math
from typing import Any

from sellershield_agent.evalkit.schema import CaseResult, CaseStatus, EvalMetrics


def compute_intent_accuracy(intent_evaluations: list[tuple[str, str]]) -> float | None:
    """Compute intent accuracy percentage across evaluated utterances.

    Args:
        intent_evaluations: List of (predicted_intent, ground_truth_intent).

    Returns:
        Accuracy percentage (0.0 to 100.0) or None if no evaluations exist.
    """
    if not intent_evaluations:
        return None
    matches = sum(1 for pred, truth in intent_evaluations if pred.strip() == truth.strip())
    return (matches / len(intent_evaluations)) * 100.0


def compute_tool_correctness(results: list[CaseResult]) -> float | None:
    """Compute tool correctness percentage across cases.

    A case is tool-correct if expected tools were called in the expected order
    and no forbidden tools were called.
    """
    if not results:
        return None
    correct = sum(
        1
        for r in results
        if not r.forbidden_tools_called and not any("tool" in (r.error or "").lower() for _ in [1])
    )
    return (correct / len(results)) * 100.0


def compute_action_correctness(results: list[CaseResult]) -> float | None:
    """Compute action correctness percentage (store state assertions satisfied)."""
    if not results:
        return None
    correct = sum(1 for r in results if not r.store_state_mismatches)
    return (correct / len(results)) * 100.0


def compute_refusal_and_injection_resistance(results: list[CaseResult]) -> float | None:
    """Compute refusal and injection resistance across safety-critical cases.

    Returns:
        Percentage of safety-critical cases that passed cleanly without
        forbidden outputs or illegal mutations, or None if no safety-critical cases.
    """
    safety_cases = [r for r in results if r.safety_critical]
    if not safety_cases:
        return None
    passed = sum(1 for r in safety_cases if r.status == CaseStatus.PASS)
    return (passed / len(safety_cases)) * 100.0


def compute_verifier_failures(
    audit_events: list[dict[str, Any]],
) -> tuple[int | None, int | None]:
    """Compute verifier failures before and after regeneration from audit logs.

    Returns:
        (failures_before_regen, failures_after_regen) or (None, None) if no data.
    """
    if not audit_events:
        return None, None

    before_count = 0
    after_count = 0

    for ev in audit_events:
        action = ev.get("action")
        event_type = ev.get("event_type")
        details = ev.get("details") or {}
        stage = details.get("stage", "")

        # Verifier failure on initial draft
        if event_type == "verifier_failure" or action == "verify_draft":
            if "regen" in str(stage).lower() or details.get("regeneration"):
                after_count += 1
            else:
                before_count += 1
        elif action == "verifier_outcome" and ev.get("outcome") == "failed":
            before_count += 1

    return before_count, after_count


def compute_latencies(latencies: list[int | float]) -> tuple[float | None, float | None]:
    """Compute p50 and p95 latency values.

    Returns:
        (p50, p95) in milliseconds, or (None, None) if latencies is empty.
    """
    if not latencies:
        return None, None

    sorted_vals = sorted(latencies)
    n = len(sorted_vals)

    def _percentile(p: float) -> float:
        k = (n - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return float(sorted_vals[int(k)])
        d0 = float(sorted_vals[int(f)]) * (c - k)
        d1 = float(sorted_vals[int(c)]) * (k - f)
        return d0 + d1

    return _percentile(0.50), _percentile(0.95)


def format_metric(val: float | int | None, unit: str = "%") -> str:
    """Format a metric value, printing 'not measured' when data is missing.

    A metric with no data prints 'not measured', never 0 or 100 percent.
    """
    if val is None:
        return "not measured"
    if unit == "%":
        return f"{val:.1f}%"
    if unit == "ms":
        return f"{val:.0f} ms"
    return str(val)


def build_metrics_summary(
    results: list[CaseResult],
    intent_evaluations: list[tuple[str, str]] | None = None,
    audit_events: list[dict[str, Any]] | None = None,
    latencies: list[int | float] | None = None,
    total_calls: int = 0,
) -> EvalMetrics:
    """Aggregate all evaluation metrics into an EvalMetrics model."""
    total = len(results)
    passed = sum(1 for r in results if r.status == CaseStatus.PASS)
    failed = sum(1 for r in results if r.status == CaseStatus.FAIL)
    no_cache = sum(1 for r in results if r.status == CaseStatus.NO_CACHE)

    pass_rate = (passed / total * 100.0) if total > 0 else None
    intent_acc = compute_intent_accuracy(intent_evaluations or [])
    tool_corr = compute_tool_correctness(results)
    action_corr = compute_action_correctness(results)
    refusal_inj = compute_refusal_and_injection_resistance(results)

    v_before, v_after = compute_verifier_failures(audit_events or [])
    p50, p95 = compute_latencies(latencies or [r.latency_ms for r in results if r.latency_ms > 0])

    return EvalMetrics(
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        no_cache_cases=no_cache,
        pass_rate_pct=pass_rate,
        intent_accuracy_pct=intent_acc,
        tool_correctness_pct=tool_corr,
        action_correctness_pct=action_corr,
        refusal_injection_pct=refusal_inj,
        verifier_failures_before=v_before,
        verifier_failures_after=v_after,
        latency_p50_ms=p50,
        latency_p95_ms=p95,
        total_llm_calls=total_calls,
    )
