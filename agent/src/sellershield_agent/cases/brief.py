"""Deterministic case brief builder per Spec §9.3 and §21 amendment 6(c)."""

from __future__ import annotations

import html
import re
from typing import Any

# Secret token redactor patterns
TOKEN_PATTERN = re.compile(
    r"(?i)\bBearer\s+[^\s]+|\b(?:secret[_-]?token|api[_-]?key|password|token)[^\s]*"
)


def _sanitize_untrusted_text(text: str | None) -> str:
    """Scrub tokens/secrets and HTML-escape untrusted customer text."""
    if not text:
        return ""
    # Redact tokens/secrets
    scrubbed = TOKEN_PATTERN.sub("[REDACTED]", text)
    # HTML escape
    return html.escape(scrubbed)


def resolve_recommended_next_step(category: str, customer_sentiment: str | None = None) -> str:
    """Deterministic recommendation from hardcoded rule table.

    Customer text cannot steer this recommendation (Spec §9.2).
    """
    cat = (category or "").lower()
    sent = (customer_sentiment or "").lower()

    if cat in ("lost_shipment", "lost"):
        return "reship or refund with approval"
    if cat in ("delivery_delay", "delay"):
        return "wait or cancel"
    if cat in ("damaged_item", "wrong_item", "damaged", "wrong"):
        return "review"
    if sent == "angry" or cat == "angry":
        return "reply within the SLA"

    return "review details and reply to customer"


def build_deterministic_brief(
    *,
    customer_id: str,
    order_id: str | None = None,
    category: str,
    customer_language: str = "en",
    customer_sentiment: str = "calm",
    customer_message: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    order_facts: dict[str, Any] | None = None,
    risk_signals: dict[str, Any] | None = None,
    policy_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate deterministic case brief without model generation.

    Invariants:
    1. No tokens or Authorization headers.
    2. No cross-customer data.
    3. Untrusted customer message is HTML-escaped and tagged with verified=False.
    4. Next step recommendation is derived solely from the category/facts rule table.
    """
    escaped_request = _sanitize_untrusted_text(customer_message)

    sanitized_tools: list[dict[str, Any]] = []
    if tool_calls:
        for tc in tool_calls:
            sanitized_tools.append(
                {
                    "tool": tc.get("tool", "unknown"),
                    "status": tc.get("status", "unknown"),
                    "latency_ms": tc.get("latency_ms", 0),
                }
            )

    rec_step = resolve_recommended_next_step(category, customer_sentiment)

    return {
        "summary": f"Support escalation brief for {category}",
        "customer_id": customer_id,
        "order_id": order_id,
        "category": category,
        "customer_language": customer_language,
        "customer_sentiment": customer_sentiment,
        "customer_request": {
            "text": escaped_request,
            "verified": False,
        },
        "tool_calls": sanitized_tools,
        "order_facts": order_facts or {},
        "risk_signals": risk_signals or {},
        "policy_rules": policy_rules or {
            "return_window_apparel_days": 10,
            "return_window_other_days": 7,
            "approval_sla_hours": 24,
            "free_shipping_reasons": ["damaged", "wrong_item", "defective"],
        },
        "recommended_next_step": rec_step,
        "options": ["reship", "refund", "store_credit"],
    }

