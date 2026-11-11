"""Customer-safe policy summarizer (KP-23 & Amendment 6).

Generates customer-safe summaries containing return windows by category,
return shipping responsibility, and refund timing only.

Strictly excludes internal limits, thresholds, risk bands, score mechanics,
or staff approval details.
"""

from __future__ import annotations

from sellershield_agent.policy.models import PolicyDocument


class CustomerSafeSummary(str):
    """Immutable string summary with structured attribute access."""

    windows: dict[str, int]
    return_shipping: str
    refund_timing: str

    def __new__(
        cls,
        content: str,
        *,
        windows: dict[str, int],
        return_shipping: str,
        refund_timing: str,
    ) -> CustomerSafeSummary:
        obj = super().__new__(cls, content)
        obj.windows = windows
        obj.return_shipping = return_shipping
        obj.refund_timing = refund_timing
        return obj


def customer_safe_summary(policy: PolicyDocument) -> CustomerSafeSummary:
    """Generate customer-safe summary from a PolicyDocument.

    Returns category return windows, shipping deduction/free rules, and refund timing.
    Guaranteed free of internal limits, risk thresholds, approval levels, or scores.
    """
    window_entries: list[str] = []
    for category, days in policy.return_windows_days.items():
        cat_display = category.replace("_", " ").title()
        if days > 0:
            window_entries.append(f"- {cat_display}: {days} days from delivery")
        else:
            window_entries.append(f"- {cat_display}: Non-returnable (0 days)")

    windows_text = "\n".join(window_entries)

    fee_rupees = policy.return_shipping.customer_fee_paise // 100
    shipping_text = (
        "Return pickup is free for items that are defective, damaged, incorrect, "
        "or not as described. "
        f"For other reasons, a return shipping fee of ₹{fee_rupees} is deducted from the refund."
    )

    timing_text = policy.customer_facing_text.refund_timing

    full_text = (
        f"Return Windows:\n{windows_text}\n\n"
        f"Return Shipping:\n{shipping_text}\n\n"
        f"Refund Timing:\n{timing_text}"
    )

    return CustomerSafeSummary(
        full_text,
        windows=policy.return_windows_days,
        return_shipping=shipping_text,
        refund_timing=timing_text,
    )
