"""Execution protocol and default placeholder executor for confirmed proposals."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from sellershield_agent.clock import Clock
from sellershield_agent.proposals.integrity import PreparedProposal
from sellershield_agent.store.client import RequestContext


class ExecutionResult(BaseModel):
    """Structured outcome returned by an Executor implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["executing", "succeeded", "pending_approval", "unknown", "failed"]
    return_id: str | None = None
    approval_id: str | None = None
    refund_id: str | None = None
    message_key: str | None = None


@runtime_checkable
class Executor(Protocol):
    """Execution layer contract invoked once a proposal is atomically confirmed."""

    async def execute(
        self,
        customer_ctx: RequestContext,
        proposal: PreparedProposal,
        *,
        clock: Clock,
        idempotency_key: str,
    ) -> ExecutionResult: ...


class PendingExecutor:
    """Default pre-T2.5 executor leaving proposals in executing state."""

    async def execute(
        self,
        customer_ctx: RequestContext,
        proposal: PreparedProposal,
        *,
        clock: Clock,
        idempotency_key: str,
    ) -> ExecutionResult:
        return ExecutionResult(
            status="executing",
            return_id=None,
            approval_id=None,
            message_key="proposal.executing",
        )
