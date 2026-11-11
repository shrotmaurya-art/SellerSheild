"""Lite Evaluation kit package (L4.2)."""

from __future__ import annotations

from sellershield_agent.evalkit.loader import load_all_convos, load_convo_file, load_golden_intents
from sellershield_agent.evalkit.metrics import (
    build_metrics_summary,
    compute_action_correctness,
    compute_intent_accuracy,
    compute_latencies,
    compute_refusal_and_injection_resistance,
    compute_tool_correctness,
    compute_verifier_failures,
    format_metric,
)
from sellershield_agent.evalkit.runner import EvalRunner
from sellershield_agent.evalkit.schema import (
    CaseResult,
    CaseStatus,
    ConversationCase,
    EvalMetrics,
    GoldenIntent,
)

__all__ = [
    "CaseResult",
    "CaseStatus",
    "ConversationCase",
    "EvalMetrics",
    "EvalRunner",
    "GoldenIntent",
    "build_metrics_summary",
    "compute_action_correctness",
    "compute_intent_accuracy",
    "compute_latencies",
    "compute_refusal_and_injection_resistance",
    "compute_tool_correctness",
    "compute_verifier_failures",
    "format_metric",
    "load_all_convos",
    "load_convo_file",
    "load_golden_intents",
]

