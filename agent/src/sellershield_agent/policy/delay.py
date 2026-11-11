"""Shipment delay assessment for SellerShield AI.

Spec & Amendment:
- Status 'lost' -> kind: lost
- Delayed under 2 days -> kind: minor
- Delayed 2 or more days -> kind: options
- On time / not late -> kind: on_time
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from sellershield_agent.domain.models import Shipment, ShipmentStatus
from sellershield_agent.policy.models import PolicyDocument


class DelayAssessmentKind(StrEnum):
    """Categorized delay assessment."""

    on_time = "on_time"
    minor = "minor"
    options = "options"
    lost = "lost"


class DelayAssessment(BaseModel):
    """Evaluation of carrier shipment timeliness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DelayAssessmentKind
    days_late: int = Field(default=0, ge=0)


def assess_delay(
    shipment: Shipment | None,
    now: datetime,
    policy: PolicyDocument,
) -> DelayAssessment:
    """Assess delay severity and return options tier based on policy timezone."""
    if shipment is None:
        return DelayAssessment(kind=DelayAssessmentKind.on_time, days_late=0)

    if shipment.status == ShipmentStatus.lost:
        return DelayAssessment(kind=DelayAssessmentKind.lost, days_late=0)

    if shipment.status == ShipmentStatus.delivered:
        return DelayAssessment(kind=DelayAssessmentKind.on_time, days_late=0)

    if shipment.eta_at is None:
        return DelayAssessment(kind=DelayAssessmentKind.on_time, days_late=0)

    tz = ZoneInfo(policy.timezone)
    now_local = now.astimezone(tz)
    eta_local = shipment.eta_at.astimezone(tz)

    if now_local > eta_local:
        days_late = max(0, (now_local.date() - eta_local.date()).days)
        if days_late >= 2:
            kind = DelayAssessmentKind.options
        elif days_late > 0 or (now_local - eta_local).total_seconds() > 0:
            kind = DelayAssessmentKind.minor
        else:
            kind = DelayAssessmentKind.on_time
        return DelayAssessment(kind=kind, days_late=days_late)

    return DelayAssessment(kind=DelayAssessmentKind.on_time, days_late=0)
