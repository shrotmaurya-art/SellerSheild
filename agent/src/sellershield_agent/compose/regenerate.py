"""Correction prompt builder for LLM draft regeneration attempts.

Spec §4.6, §21 (Amendment 6).
Invariants:
- Exactly one regeneration attempt is permitted before falling back to templates.
- Explicitly lists deterministic verification violations.
- Injects verified allowed facts (IDs, formatted amounts, dates, claims).
"""

from __future__ import annotations

from sellershield_agent.compose.factsheet import FactSheet
from sellershield_agent.money import format_inr


def build_regeneration_prompt(
    draft: str,
    violations: list[str],
    factsheet: FactSheet,
    language: str = "en",
) -> str:
    """Build the correction instruction message for one regeneration attempt."""
    is_hi = language.lower().startswith("hi")

    allowed_ids_str = ", ".join(sorted(factsheet.allowed_ids)) if factsheet.allowed_ids else "None"
    formatted_amounts = (
        ", ".join(format_inr(a) for a in sorted(factsheet.amounts_paise, reverse=True) if a > 0)
        or "₹0"
    )
    allowed_dates_str = (
        ", ".join(d.isoformat() for d in sorted(factsheet.dates)) if factsheet.dates else "None"
    )
    supported_claims_str = (
        ", ".join(sorted(factsheet.supported_claims)) if factsheet.supported_claims else "None"
    )

    violation_lines = "\n".join(f"- {v}" for v in violations)

    if is_hi:
        header_hi = (
            "आपका पिछला उत्तर सत्यापन जाँच में विफल रहा। "
            "कृपया निम्नलिखित त्रुटियों को सुधारते हुए पुनः उत्तर दें:\n\n"
        )
        return (
            f"{header_hi}"
            f"त्रुटियाँ (Violations):\n{violation_lines}\n\n"
            "मान्य तथ्य (Allowed Facts):\n"
            f"- स्वीकृत आईडी (Allowed IDs): {allowed_ids_str}\n"
            f"- स्वीकृत राशियाँ (Allowed Amounts): {formatted_amounts}\n"
            f"- स्वीकृत तिथियाँ (Allowed Dates): {allowed_dates_str}\n"
            f"- समर्थित दावे (Supported Claims): {supported_claims_str}\n\n"
            "नियम (Rules):\n"
            "1. केवल ऊपर दी गई स्वीकृत आईडी और राशियों का ही उल्लेख करें।\n"
            "2. बिना तथ्य समर्थन के कोई वादा या दावा न करें।\n"
            "3. उत्तर देवनागरी लिपि (हिंदी) में होना चाहिए और 1200 वर्णों से कम होना चाहिए।\n"
            "4. किसी भी आंतरिक टूल, सीमा या जोखिम स्कोर का उल्लेख न करें।"
        )

    return (
        "Your previous response draft failed deterministic verification. "
        "Please rewrite your response correcting the following violations:\n\n"
        f"Violations:\n{violation_lines}\n\n"
        "Ground Facts (Use ONLY these):\n"
        f"- Allowed Entity IDs: {allowed_ids_str}\n"
        f"- Allowed Amounts: {formatted_amounts}\n"
        f"- Allowed Dates: {allowed_dates_str}\n"
        f"- Supported Claims: {supported_claims_str}\n\n"
        "Strict Instructions:\n"
        "1. Mention ONLY the allowed IDs and amounts listed above.\n"
        "2. Do NOT promise actions (refunds, deliveries) unless supported by the claims above.\n"
        "3. Reply in plain English and stay under 1200 characters.\n"
        "4. Do NOT mention internal tool names, limits, risk scores, or URLs."
    )
