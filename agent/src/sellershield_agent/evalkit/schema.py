"""Schemas for Lite Evaluation kit (L4.2)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CaseStatus(StrEnum):
    """Execution status of an evaluation case."""

    PASS = "PASS"
    FAIL = "FAIL"
    NO_CACHE = "no_cache"


class Turn(BaseModel):
    """A single turn in a conversation scenario."""

    model_config = ConfigDict(extra="forbid")

    say: str | None = None
    tap: dict[str, Any] | None = None
    rest_call: dict[str, Any] | None = None


class ExpectedProposal(BaseModel):
    """Expected contents of a proposal."""

    model_config = ConfigDict(extra="forbid")

    amount_paise: int
    decision: str


class ConvoExpectation(BaseModel):
    """Assertion expectations for a conversation."""

    model_config = ConfigDict(extra="forbid")

    intent: str
    tools_called: list[str] = Field(default_factory=list)
    tools_forbidden: list[str] = Field(default_factory=list)
    store_state_after: dict[str, Any] = Field(default_factory=dict)
    proposal: ExpectedProposal | None = None
    reply_must_contain_facts: list[str] = Field(default_factory=list)
    reply_must_not_contain: list[str] = Field(default_factory=list)
    latency_budget_ms: int = 3000


class ConversationCase(BaseModel):
    """A full conversation test case."""

    model_config = ConfigDict(extra="forbid")

    id: str
    customer: str
    seed_scenario: str
    clock: str
    turns: list[Turn]
    expect: ConvoExpectation
    safety_critical: bool = False
    script: list[Any] | None = None


class GoldenIntent(BaseModel):
    """Single labelled intent case from intents.yaml."""

    model_config = ConfigDict(extra="ignore")

    id: str
    utterance: str
    intent: str
    language: str = "en"
    notes: str | None = None


class CaseResult(BaseModel):
    """Evaluation result for one conversation case."""

    model_config = ConfigDict(extra="ignore")

    case_id: str
    status: CaseStatus
    safety_critical: bool = False
    actual_intent: str | None = None
    actual_tools: list[str] = Field(default_factory=list)
    forbidden_tools_called: list[str] = Field(default_factory=list)
    proposal_amount_paise: int | None = None
    proposal_decision: str | None = None
    missing_facts: list[str] = Field(default_factory=list)
    forbidden_words_found: list[str] = Field(default_factory=list)
    store_state_mismatches: list[str] = Field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None
    llm_calls_count: int = 0
    verifier_failures: int = 0


class EvalMetrics(BaseModel):
    """Aggregated evaluation metrics."""

    model_config = ConfigDict(extra="ignore")

    total_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    no_cache_cases: int = 0
    pass_rate_pct: float | None = None
    intent_accuracy_pct: float | None = None
    tool_correctness_pct: float | None = None
    action_correctness_pct: float | None = None
    refusal_injection_pct: float | None = None
    verifier_failures_before: int | None = None
    verifier_failures_after: int | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    total_llm_calls: int = 0
