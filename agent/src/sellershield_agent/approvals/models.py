"""Pydantic schemas for seller approval cards, queues, decisions, and withdrawal."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ComputedAmountSchema(BaseModel):
    """Canonical refund calculation breakdown in integer paise."""

    model_config = ConfigDict(extra="forbid")

    items_paise: int = Field(ge=0)
    shipping_refund_paise: int = Field(ge=0)
    fee_paise: int = Field(ge=0)
    total_paise: int = Field(ge=0)
    capped: bool


class UnverifiedCustomerStatementSchema(BaseModel):
    """Isolated, escaped customer free-text block shown on the approval card."""

    model_config = ConfigDict(extra="forbid")

    label: str
    text: str
    truncated: bool


class SimilarPastDecisionsSchema(BaseModel):
    """Counts of past and current approval outcomes from the approvals table."""

    model_config = ConfigDict(extra="forbid")

    approved_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    expired_count: int = Field(ge=0)
    withdrawn_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    same_order_count: int = Field(ge=0)


class SellerApproval(BaseModel):
    """Deterministic seller approval card matching contracts/openapi-agent.yaml."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    proposal_id: str
    store_id: str = "nestkart"
    customer_id: str
    order_id: str
    return_id: str | None = None
    refund_id: str | None = None
    requested_amount_paise: int = Field(ge=0)
    amount_paise: int = Field(ge=0)
    required_role: str
    policy_version: str
    reason: str
    status: Literal[
        "pending",
        "approving",
        "approved",
        "rejected",
        "expired",
        "needs_revalidation",
        "withdrawn",
    ]
    expected_version: int
    risk_flags: list[str] = Field(default_factory=list)
    store_authorization_ref: str | None = None
    created_at: str
    due_at: str
    expires_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    last_error: str | None = None
    method: str
    resolution: str
    reason_code: str | None = None
    order: dict[str, Any] | None = None
    computed_amount: ComputedAmountSchema | None = None
    rule_results: list[dict[str, Any]] = Field(default_factory=list)
    risk: dict[str, Any] | None = None
    unverified_customer_statement: UnverifiedCustomerStatementSchema | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    recommendation: Literal["approve", "review"]
    recommendation_reason: str | None = None
    similar_past_decisions: SimilarPastDecisionsSchema | None = None
    revalidation_diff: list[dict[str, Any]] | None = None


class SellerApprovalListResponse(BaseModel):
    """Paginated response wrapper for GET /seller/approvals."""

    model_config = ConfigDict(extra="forbid")

    approvals: list[SellerApproval]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ApprovalDecisionRequest(BaseModel):
    """Strict seller decision body for POST /seller/approvals/{id}/decision."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=1000)
    expected_version: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def _require_non_empty_reason_on_reject(self) -> ApprovalDecisionRequest:
        if self.decision == "reject":
            if self.reason is None or not self.reason.strip():
                raise ValueError("A non-empty reason is required when rejecting an approval")
        return self


class WithdrawApprovalResponse(BaseModel):
    """Response schema for POST /me/approvals/{proposal_id}/withdraw."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    proposal_id: str
    status: Literal["withdrawn"]
