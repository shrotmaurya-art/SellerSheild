"""Pydantic models for case management, threads, and briefs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CaseMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    sender_role: str
    sender_id: str
    text: str
    created_at: str


class CaseBrief(BaseModel):
    model_config = ConfigDict(extra="allow")

    case_id: str
    customer_id: str
    order_id: str | None = None
    reason: str
    facts: dict[str, Any] = Field(default_factory=dict)
    llm_summary: str | None = None
    created_at: str


class CustomerCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    customer_id: str
    order_id: str | None = None
    status: str
    reason: str
    created_at: str
    updated_at: str | None = None
    messages: list[CaseMessage] = Field(default_factory=list)


class CustomerCaseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[CustomerCase]


class SellerCase(BaseModel):
    model_config = ConfigDict(extra="allow")

    case_id: str
    customer_id: str
    order_id: str | None = None
    status: str
    reason: str
    priority: str
    assigned_to: str | None = None
    evidence_ids: list[str] | None = None
    brief: dict[str, Any] | None = None
    messages: list[CaseMessage] = Field(default_factory=list)
    created_at: str
    updated_at: str | None = None


class SellerCaseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[SellerCase]


class SendCaseMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=2000)


class UpdateCaseStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    resolution_note: str | None = None


class AssignCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assigned_to: str


class HandbackCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instructions: str | None = None

