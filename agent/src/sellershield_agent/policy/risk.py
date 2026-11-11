"""Deterministic explainable risk indicator scoring for SellerShield AI.

Spec §7.5:
- 3 or more returns in 30 days (+30; 5 or more +50)
- Refund ratio over 40% (4,000 bp) in 90 days (+20)
- Order value at least ₹2,000 (200,000 paise) (+20)
- Account younger than 14 days (+15)
- Suspected injection logged (+10)
Bands: low (< 30), medium (30 to 59), high (60+).
Invariant: Risk never causes DENY; high risk forces seller review.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from sellershield_agent.domain.models import CustomerRiskFacts
from sellershield_agent.policy.models import PolicyDocument


class RiskBand(StrEnum):
    """Explainable risk bands."""

    low = "low"
    medium = "medium"
    high = "high"


class RiskSignal(StrEnum):
    """Named deterministic risk signals."""

    returns_30d_ge_5 = "returns_30d_ge_5"
    returns_30d_ge_3 = "returns_30d_ge_3"
    refund_ratio_gt_40pct = "refund_ratio_gt_40pct"
    order_value_ge_2000 = "order_value_ge_2000"
    account_age_lt_14d = "account_age_lt_14d"
    suspected_injection = "suspected_injection"


class RiskAssessment(BaseModel):
    """Explainable customer risk assessment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: int = Field(ge=0)
    band: RiskBand
    signals: list[str] = Field(default_factory=list)


def assess_risk(
    customer_risk: CustomerRiskFacts,
    order_value_paise: int,
    policy: PolicyDocument,
    suspected_injection: bool = False,
) -> RiskAssessment:
    """Calculate deterministic risk score and band from structured signals."""
    score = 0
    signals: list[str] = []
    cfg = policy.risk

    # 1. 30-day return frequency
    if customer_risk.returns_30d >= cfg.returns_30d_high_threshold:
        score += cfg.returns_30d_high_weight
        signals.append(RiskSignal.returns_30d_ge_5.value)
    elif customer_risk.returns_30d >= cfg.returns_30d_medium_threshold:
        score += cfg.returns_30d_medium_weight
        signals.append(RiskSignal.returns_30d_ge_3.value)

    # 2. 90-day refund ratio
    if customer_risk.refund_ratio_90d_bp > cfg.refund_ratio_90d_threshold_bp:
        score += cfg.refund_ratio_90d_weight
        signals.append(RiskSignal.refund_ratio_gt_40pct.value)

    # 3. Order value
    if order_value_paise >= cfg.order_value_min_paise:
        score += cfg.order_value_weight
        signals.append(RiskSignal.order_value_ge_2000.value)

    # 4. Account age
    if customer_risk.account_age_days < cfg.account_age_days_max:
        score += cfg.account_age_weight
        signals.append(RiskSignal.account_age_lt_14d.value)

    # 5. Suspected injection
    if suspected_injection:
        score += cfg.suspected_injection_weight
        signals.append(RiskSignal.suspected_injection.value)

    # Band assignment
    if score >= cfg.band_high_min:
        band = RiskBand.high
    elif score >= cfg.band_medium_min:
        band = RiskBand.medium
    else:
        band = RiskBand.low

    return RiskAssessment(score=score, band=band, signals=signals)
