"""Grounded composition, deterministic verifier, and templated replies.

Spec: docs/spec/02-agent.md sections 4.5, 4.6 and 21 (amendments 2 and 6).
Invariants:
- AGENTS.md rule 2.8: Every customer-facing reply passes the verifier.
- KP-12: Verifier bypass is a bug.
- KP-21/KP-30: Never stream unverified model text.
"""

from __future__ import annotations

from sellershield_agent.compose.factsheet import FactSheet, build_factsheet
from sellershield_agent.compose.regenerate import build_regeneration_prompt
from sellershield_agent.compose.templates import (
    delay_options,
    guided_mode_menu,
    human_handoff_unavailable,
    not_found,
    order_status,
    orders_list,
    policy_summary,
    refund_status,
    return_eligibility_preview,
    return_status,
    shipping_status,
    store_unavailable,
)
from sellershield_agent.compose.verifier import VerificationResult, verify

__all__ = [
    "FactSheet",
    "VerificationResult",
    "build_factsheet",
    "build_regeneration_prompt",
    "delay_options",
    "guided_mode_menu",
    "human_handoff_unavailable",
    "not_found",
    "order_status",
    "orders_list",
    "policy_summary",
    "refund_status",
    "return_eligibility_preview",
    "return_status",
    "shipping_status",
    "store_unavailable",
    "verify",
]
