"""Deterministic verifier for customer-facing draft responses.

Spec §4.6, §21 (Amendments 2 & 6).
Invariants:
- AGENTS.md rule 2.8: Every customer-facing reply passes the verifier.
- KP-12: Verifier bypass is a bug.
- Pure deterministic Python code; no model or network calls.
- Integer arithmetic only for money; no float drift (KP-05).
- Character length <= 1200 characters.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from sellershield_agent.compose.factsheet import FactSheet

# ── Regex & Lookup Constants ────────────────────────────────────────

_DEVANAGARI_DIGITS: Final[str] = "०१२३४५६७८९"
_ASCII_DIGITS: Final[str] = "0123456789"
_DIGIT_TRANS: Final[dict[int, int]] = str.maketrans(_DEVANAGARI_DIGITS, _ASCII_DIGITS)

_MONTHS: Final[dict[str, int]] = {
    "jan": 1,
    "january": 1,
    "जनवरी": 1,
    "feb": 2,
    "february": 2,
    "फ़रवरी": 2,
    "फरवरी": 2,
    "mar": 3,
    "march": 3,
    "मार्च": 3,
    "apr": 4,
    "april": 4,
    "अप्रैल": 4,
    "may": 5,
    "मई": 5,
    "jun": 6,
    "june": 6,
    "जून": 6,
    "jul": 7,
    "july": 7,
    "जुलाई": 7,
    "aug": 8,
    "august": 8,
    "अगस्त": 8,
    "sep": 9,
    "september": 9,
    "सितंबर": 9,
    "oct": 10,
    "october": 10,
    "अक्टूबर": 10,
    "nov": 11,
    "november": 11,
    "नवंबर": 11,
    "dec": 12,
    "december": 12,
    "दिसंबर": 12,
}

_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "list_my_orders",
        "get_order",
        "get_shipping_status",
        "get_return_status",
        "get_refund_status",
        "get_policy_summary",
        "check_return_eligibility",
        "preview_refund",
        "propose_return",
        "propose_refund",
        "propose_cancellation",
        "request_human_escalation",
    }
)

_PROMPT_PHRASES: Final[tuple[str, ...]] = (
    "system prompt",
    "developer prompt",
    "internal instructions",
    "instructions:",
    "you are an ai",
    "you are a customer-support agent",
    "untrusted data",
    "<untrusted_tool_results>",
    "<context>",
)

_INTERNAL_LIMITS_PHRASES: Final[tuple[str, ...]] = (
    "risk score",
    "risk_score",
    "risk level",
    "customer risk",
    "risk_facts",
    "auto-limit",
    "auto_limit",
    "auto limit",
    "autonomy cap",
    "staff limit",
    "owner limit",
    "hard ceiling",
    "threshold",
    "rule_results",
)

_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"https?://\S+|www\.\S+|ftp://\S+", re.IGNORECASE)
_MARKDOWN_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[.*?\]\([^\)]+\)")
_MARKDOWN_IMAGE_PATTERN: Final[re.Pattern[str]] = re.compile(r"!\[.*?\]\([^\)]+\)")
_HTML_PATTERN: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_CUSTOMER_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(C[0-9]{3,}|cust_[a-zA-Z0-9]+)\b", re.IGNORECASE
)
_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\b"
    r"|\bBearer\s+[A-Za-z0-9_.-]{10,}\b"
    r"|\b(?:sk|pk|api|key|secret)[_-][a-zA-Z0-9]{10,}\b",
    re.IGNORECASE,
)

_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(ORD|RET|RFD|CASE|APR|PRP)-[A-Za-z0-9]+\b",
    re.IGNORECASE,
)

_SPELLED_OUT_MAGNITUDES: Final[tuple[str, ...]] = (
    "hundred",
    "thousand",
    "lakh",
    "crore",
    "sau",
    "hazaar",
    "सौ",
    "हज़ार",
    "लाख",
    "करोड़",
)

_SPELLED_OUT_AMOUNT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?:twelve|one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|do|teen|chaar|paanch|chhah|saat|aath|nau|das|[0-9०-९]+)\s+"
    r"(?:hundred|thousand|lakh|crore|sau|hazaar|सौ|हज़ार|लाख|करोड़)\s*"
    r"(?:rupees?|rs\.?|inr|rupaye?|rupaya|रुपये|रुपया|रु)?\b"
    r"|\b(?:hundred|thousand|lakh|crore|sau|hazaar|सौ|हज़ार|लाख|करोड़)\s*(?:rupees?|rs\.?|inr|rupaye?|rupaya|रुपये|रुपया|रु)\b"
)

_CURRENCY_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:₹|Rs\.?|INR|रु\.?)\s*[\r\n]*\s*([०-९0-9]+(?:,[०-९0-9]+)*(?:\.[०-९0-9]+)?)",
    re.IGNORECASE,
)

_CURRENCY_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"([०-९0-9]+(?:,[०-९0-9]+)*(?:\.[०-९0-9]+)?)\s*[\r\n]*\s*(?:rupees?|rupaye?|rupaya|रुपये|रुपया|रु\.?)",
    re.IGNORECASE,
)

_ISO_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_TEXT_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(\d{1,2}|[०-९]{1,2})(?:st|nd|rd|th)?\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|जनवरी|फ़रवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|अक्टूबर|नवंबर|दिसंबर)"
    r"(?:\s+(\d{4}|[०-९]{4}))?\b"
)

_PROMISE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?:guaranteed|guarantee|definitely|100%|promise)\b"
    r"|\bwill\s+be\s+refunded\b"
    r"|\b(?:गारंटी|निश्चित\s+रूप\s+से|पक्का|वादा)\b"
    r"|\bरिफंड\s+किया\s+जाएगा\b"
)


class VerificationResult(BaseModel):
    """Result of deterministic verification of a draft response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    violations: list[str] = Field(default_factory=list)


def _to_standard_digits(text: str) -> str:
    return text.translate(_DIGIT_TRANS)


def _parse_currency_amount(raw_num: str) -> tuple[int | None, str | None]:
    clean = _to_standard_digits(raw_num).replace(",", "").strip()
    if "." in clean:
        parts = clean.split(".")
        if len(parts) > 2 or len(parts[1]) > 2:
            return None, f"Amount '{raw_num}' has invalid decimal precision"
        try:
            rupees = int(parts[0]) if parts[0] else 0
            paise_cents = int(parts[1].ljust(2, "0"))
            return rupees * 100 + paise_cents, None
        except ValueError:
            return None, f"Cannot parse amount '{raw_num}'"
    try:
        rupees = int(clean)
        return rupees * 100, None
    except ValueError:
        return None, f"Cannot parse amount '{raw_num}'"


def verify(draft: str, factsheet: FactSheet, language: str) -> VerificationResult:
    """Deterministically verify a customer-facing response draft against a FactSheet."""
    violations: list[str] = []

    # 1. Length check
    if len(draft) > 1200:
        violations.append(f"Draft length {len(draft)} exceeds maximum allowed 1200 characters")

    # Unicode normalization and sanitization
    normalized = unicodedata.normalize("NFKC", draft)
    cleaned = re.sub(r"[\u200b\u200c\u200d\ufeff\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", normalized)
    cleaned_dashes = re.sub(r"[–—−]", "-", cleaned)

    # 2. Language & Script check
    has_devanagari = bool(re.search(r"[\u0900-\u097F]", cleaned))
    lang_lower = language.lower()
    if lang_lower.startswith("hi"):
        if not has_devanagari:
            violations.append(
                "Customer language is Hindi (hi) but draft contains no Devanagari script"
            )
    elif lang_lower.startswith("en"):
        if has_devanagari:
            violations.append(
                "Customer language is English (en) but draft contains Devanagari script"
            )

    # 3. Forbidden content checks
    if _URL_PATTERN.search(cleaned):
        violations.append("Draft contains forbidden URL")
    if _MARKDOWN_LINK_PATTERN.search(cleaned):
        violations.append("Draft contains forbidden Markdown link")
    if _MARKDOWN_IMAGE_PATTERN.search(cleaned):
        violations.append("Draft contains forbidden Markdown image")
    if _HTML_PATTERN.search(cleaned):
        violations.append("Draft contains forbidden HTML tag")

    for tool in _TOOL_NAMES:
        if re.search(rf"\b{tool}\b", cleaned, re.IGNORECASE):
            violations.append(f"Draft mentions internal tool name: '{tool}'")

    for phrase in _PROMPT_PHRASES:
        if phrase in cleaned.lower():
            violations.append(f"Draft contains internal prompt/instruction text: '{phrase}'")

    for phrase in _INTERNAL_LIMITS_PHRASES:
        if phrase in cleaned.lower():
            violations.append(f"Draft mentions internal limit/risk metric: '{phrase}'")

    cust_matches = _CUSTOMER_ID_PATTERN.findall(cleaned)
    if cust_matches:
        for cid in cust_matches:
            violations.append(f"Draft contains customer ID: '{cid}'")

    if _TOKEN_PATTERN.search(cleaned):
        violations.append("Draft contains token-like or secret key string")

    # 4. Spelled-out amounts check
    if _SPELLED_OUT_AMOUNT_PATTERN.search(cleaned):
        violations.append("Draft contains spelled-out currency amount")

    # 5. Currency amounts check
    amount_matches: list[str] = []
    for m in _CURRENCY_PREFIX_PATTERN.finditer(cleaned_dashes):
        amount_matches.append(m.group(1))
    for m in _CURRENCY_SUFFIX_PATTERN.finditer(cleaned_dashes):
        amount_matches.append(m.group(1))

    for raw_amt in amount_matches:
        paise, err = _parse_currency_amount(raw_amt)
        if err or paise is None:
            violations.append(err or f"Invalid currency amount format: '{raw_amt}'")
        elif paise not in factsheet.amounts_paise:
            sorted_amts = sorted(factsheet.amounts_paise)
            violations.append(
                f"Currency amount ₹{paise // 100} ({paise} paise) is not backed "
                f"by FactSheet amounts: {sorted_amts}"
            )

    # 6. Entity IDs check
    for m in _ID_PATTERN.finditer(cleaned_dashes):
        raw_id = m.group(0)
        norm_id = raw_id.upper()
        if norm_id not in factsheet.allowed_ids:
            sorted_ids = sorted(factsheet.allowed_ids)
            violations.append(
                f"Identifier '{raw_id}' does not exist in FactSheet allowed IDs: {sorted_ids}"
            )

    # 7. Dates check
    detected_dates: list[date] = []
    for m in _ISO_DATE_PATTERN.finditer(cleaned_dashes):
        try:
            detected_dates.append(date.fromisoformat(m.group(1)))
        except ValueError:
            violations.append(f"Invalid date format: '{m.group(1)}'")

    default_year = 2026
    if factsheet.dates:
        default_year = next(iter(factsheet.dates)).year

    for m in _TEXT_DATE_PATTERN.finditer(cleaned_dashes):
        day_str = _to_standard_digits(m.group(1))
        month_str = m.group(2).lower()
        year_str = _to_standard_digits(m.group(3)) if m.group(3) else str(default_year)
        month_num = _MONTHS.get(month_str)
        if month_num is not None:
            try:
                d = date(int(year_str), month_num, int(day_str))
                detected_dates.append(d)
            except ValueError:
                violations.append(f"Invalid date: '{m.group(0)}'")

    for d in detected_dates:
        if d not in factsheet.dates:
            sorted_dates = sorted(factsheet.dates)
            violations.append(
                f"Date '{d.isoformat()}' is not backed by FactSheet dates: {sorted_dates}"
            )

    # 8. Relative-day facts check
    if re.search(r"\btomorrow\b", cleaned, re.IGNORECASE):
        if factsheet.relative_facts.get("eta_in_days") != 1:
            violations.append(
                "Draft claims delivery 'tomorrow' but FactSheet relative fact eta_in_days != 1"
            )

    if re.search(r"\byesterday\b", cleaned, re.IGNORECASE):
        if factsheet.relative_facts.get("delivered_days_ago") != 1:
            violations.append(
                "Draft claims delivery 'yesterday' but FactSheet "
                "relative fact delivered_days_ago != 1"
            )

    m_in_days = re.search(r"\bin\s+(\d+|[०-९]+)\s+days?\b", cleaned, re.IGNORECASE)
    if m_in_days:
        n = int(_to_standard_digits(m_in_days.group(1)))
        actual_eta = factsheet.relative_facts.get("eta_in_days")
        if actual_eta != n:
            violations.append(
                f"Draft claims delivery in {n} days but FactSheet "
                f"relative fact eta_in_days is {actual_eta}"
            )

    m_days_ago = re.search(r"\b(\d+|[०-९]+)\s+days?\s+ago\b", cleaned, re.IGNORECASE)
    if m_days_ago:
        n = int(_to_standard_digits(m_days_ago.group(1)))
        actual_ago = factsheet.relative_facts.get("delivered_days_ago")
        if actual_ago != n:
            violations.append(
                f"Draft claims delivery {n} days ago but FactSheet "
                f"relative fact delivered_days_ago is {actual_ago}"
            )

    # 9. Action claims check
    if re.search(r"\brefunded\b", cleaned, re.IGNORECASE):
        if "refunded" not in factsheet.supported_claims:
            violations.append(
                "Draft claims 'refunded' but FactSheet has no supporting refund claim"
            )

    if re.search(r"\bprocessed\b", cleaned, re.IGNORECASE):
        if "processed" not in factsheet.supported_claims:
            violations.append("Draft claims 'processed' but FactSheet has no supporting claim")

    if re.search(r"\bapproved\b", cleaned, re.IGNORECASE):
        if "approved" not in factsheet.supported_claims:
            violations.append("Draft claims 'approved' but FactSheet has no supporting claim")

    if re.search(r"\bcreated\b", cleaned, re.IGNORECASE):
        if "created" not in factsheet.supported_claims:
            violations.append("Draft claims 'created' but FactSheet has no supporting claim")

    if re.search(r"\b(?:cancelled|canceled)\b", cleaned, re.IGNORECASE):
        if "cancelled" not in factsheet.supported_claims:
            violations.append("Draft claims 'cancelled' but FactSheet has no supporting claim")

    if re.search(r"\b(?:shipped|dispatched)\b", cleaned, re.IGNORECASE):
        if "shipped" not in factsheet.supported_claims:
            violations.append(
                "Draft claims 'shipped/dispatched' but FactSheet has no supporting claim"
            )

    if re.search(r"\bdelivered\b", cleaned, re.IGNORECASE):
        if "delivered" not in factsheet.supported_claims:
            violations.append("Draft claims 'delivered' but FactSheet has no supporting claim")

    # 10. Promise words check
    for m in _PROMISE_PATTERN.finditer(cleaned):
        word = m.group(0)
        if word.lower() not in factsheet.promise_facts:
            violations.append(f"Draft contains promise language '{word}' without supporting fact")

    return VerificationResult(ok=len(violations) == 0, violations=violations)
