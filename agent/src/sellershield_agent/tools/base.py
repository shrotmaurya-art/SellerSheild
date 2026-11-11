"""Base definitions for tools, errors, and execution results.

Spec §6.1, §6.2, §13, §21 (Amendment 1).
Invariant: Handlers never see raw customer tokens.
Invariant: Extra argument fields are strictly forbidden.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from sellershield_agent.clock import Clock
from sellershield_agent.policy.models import PolicyDocument
from sellershield_agent.store.client import RequestContext, StoreClient


class ToolKind(StrEnum):
    """Permitted tool execution kinds."""

    READ = "read"
    PROPOSE = "propose"


class ToolErrorCode(StrEnum):
    """Taxonomy of tool error codes mapped to customer-safe messages."""

    not_found = "not_found"
    not_permitted = "not_permitted"
    invalid_args = "invalid_args"
    policy_denied = "policy_denied"
    stale_state = "stale_state"
    rate_limited = "rate_limited"
    upstream_unavailable = "upstream_unavailable"
    timeout = "timeout"
    internal = "internal"


class ToolResult(BaseModel):
    """Standard execution result returned by all tools."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    data: Any = None
    error_code: ToolErrorCode | None = None
    error_message: str | None = None
    retryable: bool = False
    fact_ids: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ToolContext:
    """Execution context passed to tool handlers.

    Enforces that handlers cannot inspect raw customer tokens.
    Token handling is encapsulated within the StoreClient calls.
    """

    store_client: StoreClient
    customer_ctx: RequestContext
    clock: Clock
    policy: PolicyDocument
    service_ctx_factory: Callable[[str], RequestContext] | None = None
    session_id: str | None = None
    engine: Engine | None = None


@dataclass(frozen=True)
class Tool:
    """Definition and handler specification for an agent tool."""

    name: str
    description: str
    args_model: type[BaseModel]
    kind: ToolKind
    handler: Callable[[ToolContext, Any], Awaitable[ToolResult]]
