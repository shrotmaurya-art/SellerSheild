"""Advisory prompt injection heuristics (Spec §4.1)."""

from __future__ import annotations

import re

from sellershield_agent.understand.schema import SafetyFlag

INJECTION_PATTERNS = [
    # Direct override and instruction manipulation
    r"\bignore\s+(?:all\s+)?(?:previous|prior|rules|directives|that)\b",
    r"\bdisregard\s+(?:all\s+)?(?:previous|prior|safety|rules|constraints)\b",
    r"\boverride\b",
    r"\bsystem\s+override\b",
    r"\bsystem\s+prompt\b",
    r"\bend\s+of\s+system\s+prompt\b",
    r"\bhidden\s+instructions?\b",
    r"\[\[\s*instruction\s*\]\]",
    r"<\s*system\s*>",
    r"<\s*/\s*system\s*>",
    r"<!--\s*execute_tool",
    r"\bexecute_sql\b",
    r"\badmin_wipe\b",
    # Role-play & jailbreak personas
    r"\byou\s+are\s+now\b",
    r"\bdebug\s+mode\b",
    r"\bdeveloper\s+mode\b",
    r"\bsudo\s+mode\b",
    r"\bas\s+(?:admin|root|developer)\b",
    r"\bstop\s+roleplay\b",
    r"\bDAN\b",
    r"\bdo\s+anything\s+now\b",
    r"\bpretend\s+you\s+are\b",
    r"\bbypass\s+(?:the\s+)?verifier\b",
    r"\bauto_limit\b",
    r"\bopposite\s+day\b",
    r"\bunrestricted\s+shell\b",
    # Data extraction / prompt leak
    r"\bprompt\s+leak\b",
    r"\bdatabase\s+credentials\b",
    r"\benvironment\s+variables\b",
    r"\bjwt_secret\b",
    r"\bstore_base_url\b",
    r"\bsecret_key\b",
    r"\bauth\s+token\b",
    # Financial manipulation / unauthorized action demands
    r"\bapprove\s+(?:my\s+refund|immediate\s+refund|all)\b",
    r"\bautomatically\s+approve\b",
    r"\bwithout\s+inspection\b",
    r"\bdo\s+not\s+verify\s+factsheet\b",
    r"\bassistant\s+response\s+must\s+start\s+with\b",
    r"\bno\s+longer\s+bound\s+by\s+policy\b",
    r"\bconfirm\s+refund\s+of\b",
    r"\breset\s+customer\s+risk\s+score\b",
    r"\bset\s+shipping_fee_paise\b",
    # Multilingual & Hindi injection phrases
    r"निर्देश(?:ों)?\s+को\s+अनदेखा",
    r"pichhle\s+saare\s+nirdesh\s+bhool\s+jao",
    r"व्यवस्थापक\s+अधिकार",
]

_COMPILED_INJECTION_RE = [re.compile(pat, re.IGNORECASE) for pat in INJECTION_PATTERNS]
_LEET_MAP = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s"})


def detect_injection_heuristics(message: str) -> list[SafetyFlag]:
    """Detect suspected prompt injection patterns in customer text.

    Advisory only: no defence depends on detection; returns safety flags
    for audit logging without changing business behavior.
    """
    if not message or not message.strip():
        return []

    # Clean zero-width chars
    clean = re.sub(r"[\u200b-\u200f\ufeff\u202a-\u202e]", "", message)
    # Unhyphenate single letter spans (e.g. S-Y-S-T-E-M)
    unhyphen = re.sub(r"(?<=\b[a-zA-Z])-(?=[a-zA-Z]\b)", "", clean)
    # Leetspeak decode
    leet = unhyphen.translate(_LEET_MAP)

    flags: list[SafetyFlag] = []
    for pattern in _COMPILED_INJECTION_RE:
        if pattern.search(clean) or pattern.search(unhyphen) or pattern.search(leet):
            flags.append(SafetyFlag.INJECTION_ATTEMPT)
            break

    return flags
