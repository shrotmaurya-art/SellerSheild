"""Policy, refund, and deterministic decision engine package for SellerShield AI."""

from __future__ import annotations

from sellershield_agent.policy.decide import (
    Decision,
    DecisionFlags,
    decide,
)
from sellershield_agent.policy.delay import (
    DelayAssessment,
    DelayAssessmentKind,
    assess_delay,
)
from sellershield_agent.policy.facts import (
    DecisionAction,
    DecisionFacts,
    compute_facts_hash,
)
from sellershield_agent.policy.loader import (
    load_policy,
    load_policy_from_dict,
    load_policy_from_json,
)
from sellershield_agent.policy.models import (
    ItemAmounts,
    PolicyDocument,
    RefundBreakdown,
    ReturnShippingPolicy,
    RiskPolicy,
)
from sellershield_agent.policy.refund import compute_refund
from sellershield_agent.policy.risk import (
    RiskAssessment,
    RiskBand,
    RiskSignal,
    assess_risk,
)
from sellershield_agent.policy.rules import (
    ApproverRole,
    DecisionOutcome,
    RuleResult,
    rule_r01_order_ownership,
    rule_r02_order_state,
    rule_r03_category_window,
    rule_r04_item_returnable,
    rule_r05_quantity_available,
    rule_r06_active_return,
    rule_r07_evidence_required,
    rule_r08_refund_formula,
    rule_r09_per_refund_auto_limit,
    rule_r10_cumulative_limit_30d,
    rule_r11_refundable_balance_cap,
    rule_r12_staff_limit_and_hard_ceiling,
    rule_r13_risk_signals,
    rule_r14_cod_refund_method,
    rule_r15_cancellation_pre_dispatch,
)
from sellershield_agent.policy.summary import CustomerSafeSummary, customer_safe_summary

__all__ = [
    "ApproverRole",
    "CustomerSafeSummary",
    "Decision",
    "DecisionAction",
    "DecisionFacts",
    "DecisionFlags",
    "DecisionOutcome",
    "DelayAssessment",
    "DelayAssessmentKind",
    "ItemAmounts",
    "PolicyDocument",
    "RefundBreakdown",
    "ReturnShippingPolicy",
    "RiskAssessment",
    "RiskBand",
    "RiskPolicy",
    "RiskSignal",
    "RuleResult",
    "assess_delay",
    "assess_risk",
    "compute_facts_hash",
    "compute_refund",
    "customer_safe_summary",
    "decide",
    "load_policy",
    "load_policy_from_dict",
    "load_policy_from_json",
    "rule_r01_order_ownership",
    "rule_r02_order_state",
    "rule_r03_category_window",
    "rule_r04_item_returnable",
    "rule_r05_quantity_available",
    "rule_r06_active_return",
    "rule_r07_evidence_required",
    "rule_r08_refund_formula",
    "rule_r09_per_refund_auto_limit",
    "rule_r10_cumulative_limit_30d",
    "rule_r11_refundable_balance_cap",
    "rule_r12_staff_limit_and_hard_ceiling",
    "rule_r13_risk_signals",
    "rule_r14_cod_refund_method",
    "rule_r15_cancellation_pre_dispatch",
]
