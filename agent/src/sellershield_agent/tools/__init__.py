"""Tools layer for SellerShield AI.

Spec §6.1, §6.2, §6.4, §13, §21 (Amendment 1).
Singular entrypoint for all agent tool executions with security,
allowlist enforcement, response minimization, and tamper-evident auditing.
"""

from __future__ import annotations

from sellershield_agent.tools.base import (
    Tool,
    ToolContext,
    ToolErrorCode,
    ToolKind,
    ToolResult,
)
from sellershield_agent.tools.formatting import (
    tool_results_block,
    tools_for_llm,
)
from sellershield_agent.tools.gateway import ToolGateway
from sellershield_agent.tools.propose_tools import (
    ProposeRefundArgs,
    ProposeReturnArgs,
    RefundCause,
    build_propose_tools,
)
from sellershield_agent.tools.read_tools import (
    CheckReturnEligibilityArgs,
    GetOrderArgs,
    GetPolicySummaryArgs,
    GetRefundStatusArgs,
    GetReturnStatusArgs,
    GetShippingStatusArgs,
    ListMyOrdersArgs,
    PreviewRefundArgs,
    build_read_tools,
)


def build_all_tools() -> dict[str, Tool]:
    """Build and return the complete registry of READ and PROPOSE tools."""
    return {**build_read_tools(), **build_propose_tools()}


__all__ = [
    "CheckReturnEligibilityArgs",
    "GetOrderArgs",
    "GetPolicySummaryArgs",
    "GetRefundStatusArgs",
    "GetReturnStatusArgs",
    "GetShippingStatusArgs",
    "ListMyOrdersArgs",
    "PreviewRefundArgs",
    "ProposeRefundArgs",
    "ProposeReturnArgs",
    "RefundCause",
    "Tool",
    "ToolContext",
    "ToolErrorCode",
    "ToolGateway",
    "ToolKind",
    "ToolResult",
    "build_all_tools",
    "build_propose_tools",
    "build_read_tools",
    "tool_results_block",
    "tools_for_llm",
]
