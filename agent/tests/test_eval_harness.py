"""Tests for Lite Evaluation harness, loader, and pure metrics (TASK L4.2).

Verifies that the evaluation harness enforces strict test integrity:
- Correct conversations pass
- Wrong tools, forbidden tools, wrong store states, forbidden substrings fail
- Replay CacheMiss yields status no_cache (never PASS or FAIL)
- Metrics on hand-made synthetic data equal hand-computed values
- Empty metrics format as 'not measured', never 0% or 100%
- --max-calls stops cleanly at the limit
- Loader validates all 12 conversation files
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from sellershield_agent.evalkit.loader import load_all_convos, load_convo_file, load_golden_intents
from sellershield_agent.evalkit.metrics import (
    build_metrics_summary,
    format_metric,
)
from sellershield_agent.evalkit.runner import CallBudgetTracker, EvalRunner, MaxCallsReached
from sellershield_agent.evalkit.schema import (
    CaseResult,
    CaseStatus,
)
from sellershield_agent.llm.cache import CacheMiss
from sellershield_agent.understand.schema import Intent

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
CONVOS_DIR = ROOT_DIR / "scenarios" / "golden" / "convos"
INTENTS_FILE = ROOT_DIR / "scenarios" / "golden" / "intents.yaml"
FIXTURES_PATH = ROOT_DIR / "scenarios" / "fixtures" / "store.json"


# ── Loader Tests ─────────────────────────────────────────────────────────────


def test_loader_validates_all_12_files() -> None:
    """Validate all 12 conversation files: unique IDs L01-L12, valid enums, amounts in proposals."""
    cases = load_all_convos(CONVOS_DIR)
    assert len(cases) == 12, f"Expected exactly 12 conversation files, found {len(cases)}"

    # Unique IDs
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)), f"Duplicate IDs detected: {ids}"
    expected_ids = [f"L{i:02d}" for i in range(1, 13)]
    assert sorted(ids) == expected_ids

    # Valid intent enums
    valid_intents = {i.value for i in Intent}
    for c in cases:
        assert c.expect.intent in valid_intents, (
            f"Case {c.id} specifies unknown intent: {c.expect.intent}"
        )
        assert c.turns, f"Case {c.id} has no turns"
        assert c.clock, f"Case {c.id} has no clock"

        # Every money proposal must name an amount (int paise >= 0)
        if c.expect.proposal is not None:
            assert isinstance(c.expect.proposal.amount_paise, int)
            assert c.expect.proposal.amount_paise >= 0
            assert c.expect.proposal.decision in ("allow", "needs_approval", "deny")


def test_loader_rejects_duplicate_or_invalid_cases(tmp_path: Path) -> None:
    """Ensure loader fails when invalid schemas or duplicate IDs are provided."""
    dup_1 = tmp_path / "L01.yaml"
    dup_1.write_text(
        """
id: L01
customer: C001
seed_scenario: SC-01
clock: "2026-11-08T12:00:00+05:30"
turns: [{say: "Hello"}]
expect: {intent: "order_status"}
""",
        encoding="utf-8",
    )
    dup_2 = tmp_path / "L02.yaml"
    dup_2.write_text(
        """
id: L01
customer: C001
seed_scenario: SC-01
clock: "2026-11-08T12:00:00+05:30"
turns: [{say: "Duplicate id"}]
expect: {intent: "order_status"}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate conversation case ID"):
        load_all_convos(tmp_path)


# ── Harness Execution Tests ──────────────────────────────────────────────────


def test_correct_conversation_passes() -> None:
    """A correct conversation matching all expectations passes with status PASS."""
    runner = EvalRunner(mode="scripted", fixtures_path=FIXTURES_PATH)
    l01 = load_convo_file(CONVOS_DIR / "L01.yaml")

    result = runner.run_conversation(l01)
    assert result.status == CaseStatus.PASS
    assert result.case_id == "L01"
    assert "get_order" in result.actual_tools
    assert result.error is None


def test_wrong_tool_fails_with_clear_reason() -> None:
    """Missing an expected tool fails with a clear, descriptive reason."""
    runner = EvalRunner(mode="scripted", fixtures_path=FIXTURES_PATH)
    l01 = load_convo_file(CONVOS_DIR / "L01.yaml")

    # Tamper with expectation: require a tool that is never called
    tampered = copy.deepcopy(l01)
    tampered.expect.tools_called.append("uncalled_admin_tool")

    result = runner.run_conversation(tampered)
    assert result.status == CaseStatus.FAIL
    assert "Expected tool 'uncalled_admin_tool' was not called" in (result.error or "")


def test_forbidden_tool_fails_with_clear_reason() -> None:
    """Invoking a forbidden tool fails with a clear, descriptive reason."""
    runner = EvalRunner(mode="scripted", fixtures_path=FIXTURES_PATH)
    l01 = load_convo_file(CONVOS_DIR / "L01.yaml")

    # Tamper with expectation: forbid get_order which L01 calls
    tampered = copy.deepcopy(l01)
    tampered.expect.tools_forbidden.append("get_order")

    result = runner.run_conversation(tampered)
    assert result.status == CaseStatus.FAIL
    assert "Forbidden tool 'get_order' was called" in (result.error or "")
    assert "get_order" in result.forbidden_tools_called


def test_wrong_store_state_fails_with_clear_reason() -> None:
    """A store state assertion mismatch fails with clear reason."""
    runner = EvalRunner(mode="scripted", fixtures_path=FIXTURES_PATH)
    l01 = load_convo_file(CONVOS_DIR / "L01.yaml")

    tampered = copy.deepcopy(l01)
    tampered.expect.store_state_after["returns_count"] = 99

    result = runner.run_conversation(tampered)
    assert result.status == CaseStatus.FAIL
    assert "Store returns count mismatch: expected 99" in (result.error or "")
    assert len(result.store_state_mismatches) > 0


def test_forbidden_substring_fails_with_clear_reason() -> None:
    """A reply containing a forbidden substring fails with clear reason."""
    runner = EvalRunner(mode="scripted", fixtures_path=FIXTURES_PATH)
    l01 = load_convo_file(CONVOS_DIR / "L01.yaml")

    # L01 response contains 'ORD-1048'
    tampered = copy.deepcopy(l01)
    tampered.expect.reply_must_not_contain.append("ORD-1048")

    result = runner.run_conversation(tampered)
    assert result.status == CaseStatus.FAIL
    assert "Reply contains forbidden term 'ORD-1048'" in (result.error or "")
    assert "ORD-1048" in result.forbidden_words_found


def test_missing_fact_fails_with_clear_reason() -> None:
    """A reply missing an expected fact fails with clear reason."""
    runner = EvalRunner(mode="scripted", fixtures_path=FIXTURES_PATH)
    l01 = load_convo_file(CONVOS_DIR / "L01.yaml")

    tampered = copy.deepcopy(l01)
    tampered.expect.reply_must_contain_facts.append("NON_EXISTENT_FACT_XYZ_999")

    result = runner.run_conversation(tampered)
    assert result.status == CaseStatus.FAIL
    assert "Reply missing expected fact 'NON_EXISTENT_FACT_XYZ_999'" in (result.error or "")
    assert "NON_EXISTENT_FACT_XYZ_999" in result.missing_facts


def test_replay_cache_miss_gives_no_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """In replay mode, a CacheMiss yields status no_cache, never pass or fail."""
    runner = EvalRunner(mode="replay", fixtures_path=FIXTURES_PATH)
    l01 = load_convo_file(CONVOS_DIR / "L01.yaml")

    # Simulate CacheMiss during chat call
    async def mock_chat(*args: Any, **kwargs: Any) -> Any:
        raise CacheMiss("Simulated replay cache miss for unit test")

    from sellershield_agent.llm.chain import LLMChain

    monkeypatch.setattr(LLMChain, "chat", mock_chat)

    result = runner.run_conversation(l01)
    assert result.status == CaseStatus.NO_CACHE
    assert result.status != CaseStatus.PASS
    assert result.status != CaseStatus.FAIL
    assert "Replay cache miss" in (result.error or "")


# ── Metrics Pure Function Tests ──────────────────────────────────────────────


def test_metrics_hand_computed_values() -> None:
    """Metrics computed on hand-crafted synthetic data must equal exact hand-calculated values."""
    mock_results = [
        CaseResult(case_id="L01", status=CaseStatus.PASS, safety_critical=False, latency_ms=100),
        CaseResult(case_id="L02", status=CaseStatus.PASS, safety_critical=True, latency_ms=200),
        CaseResult(case_id="L03", status=CaseStatus.PASS, safety_critical=True, latency_ms=300),
        CaseResult(
            case_id="L04",
            status=CaseStatus.FAIL,
            safety_critical=True,
            error="Tampered tool mismatch",
            store_state_mismatches=["mismatch"],
            forbidden_tools_called=["exec_sql"],
            latency_ms=400,
        ),
    ]

    intents_eval = [
        ("order_status", "order_status"),
        ("return_request", "return_request"),
        ("shipping_status", "shipping_status"),
        ("human_request", "order_status"),
    ]

    metrics = build_metrics_summary(
        results=mock_results,
        intent_evaluations=intents_eval,
        latencies=[100, 200, 300, 400],
        total_calls=12,
    )

    assert metrics.total_cases == 4
    assert metrics.passed_cases == 3
    assert metrics.failed_cases == 1
    assert metrics.pass_rate_pct == 75.0
    assert metrics.tool_correctness_pct == 75.0
    assert metrics.action_correctness_pct == 75.0
    assert metrics.refusal_injection_pct is not None
    assert round(metrics.refusal_injection_pct, 1) == 66.7
    assert metrics.intent_accuracy_pct == 75.0
    assert metrics.latency_p50_ms == 250.0
    assert metrics.total_llm_calls == 12


def test_empty_metrics_format_not_measured() -> None:
    """A metric with no data formats as 'not measured', never '0%' or '100%'."""
    assert format_metric(None, "%") == "not measured"
    assert format_metric(None, "ms") == "not measured"
    assert format_metric(None) == "not measured"

    empty_metrics = build_metrics_summary(results=[])
    assert empty_metrics.pass_rate_pct is None
    assert empty_metrics.intent_accuracy_pct is None
    assert empty_metrics.tool_correctness_pct is None
    assert empty_metrics.action_correctness_pct is None
    assert empty_metrics.refusal_injection_pct is None
    assert empty_metrics.latency_p50_ms is None

    assert format_metric(empty_metrics.pass_rate_pct) == "not measured"
    assert format_metric(empty_metrics.latency_p50_ms, "ms") == "not measured"


def test_max_calls_stops_at_limit() -> None:
    """Enforce that CallBudgetTracker cleanly halts at the --max-calls limit."""
    tracker = CallBudgetTracker(max_calls=3)
    tracker.increment()
    tracker.increment()
    tracker.increment()
    assert tracker.call_count == 3

    with pytest.raises(MaxCallsReached, match="Reached --max-calls limit of 3"):
        tracker.increment()


def test_golden_intents_loader() -> None:
    """Ensure all 30 golden utterances are loaded properly from intents.yaml."""
    intents = load_golden_intents(INTENTS_FILE)
    assert len(intents) == 30
    assert intents[0].id == "INT-01"
    assert intents[-1].id == "INT-30"
    for item in intents:
        assert item.utterance
        assert item.intent
