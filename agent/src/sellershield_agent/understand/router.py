"""Deterministic keyword and phrase router (Spec §4.2 fallback)."""

from __future__ import annotations

import re
from typing import Any

from sellershield_agent.policy.loader import load_policy
from sellershield_agent.understand.injection import detect_injection_heuristics
from sellershield_agent.understand.schema import (
    ClarificationSlot,
    Intent,
    Language,
    ReasonCode,
    SafetyFlag,
    Sentiment,
    Understanding,
)

ORD_REGEX = re.compile(r"\bORD-\d+\b", re.IGNORECASE)
ORD_CANONICAL_REGEX = re.compile(r"\bORD-\d{3,6}\b", re.IGNORECASE)
ORD_FLEXIBLE_REGEX = re.compile(r"\b(?:ORD|order)\s*(?:-|#|\s)\s*(\d{3,6})\b", re.IGNORECASE)
DEVANAGARI_REGEX = re.compile(r"[\u0900-\u097F]")


def normalize_order_id(raw: str | None) -> str | None:
    """Normalize order ID variants (e.g. '1070', 'ord 1070', 'ORD-1070') to canonical 'ORD-XXXX'."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().upper()
    if re.match(r"^ORD-\d{3,6}$", cleaned):
        return cleaned
    if re.match(r"^\d{3,6}$", cleaned):
        return f"ORD-{cleaned}"
    m = re.match(r"^(?:ORD|ORDER)\s*(?:-|#|\s)\s*(\d{3,6})$", cleaned, re.IGNORECASE)
    if m:
        return f"ORD-{m.group(1)}"
    return None


def extract_order_ids(text: str) -> list[str]:
    """Extract and normalize all order IDs found in text."""
    if not text:
        return []
    results: list[str] = []
    # 1. Canonical ORD-XXXX
    for match in ORD_CANONICAL_REGEX.findall(text):
        norm = match.upper()
        if norm not in results:
            results.append(norm)
    # 2. Flexible variants: "ord 1070", "order #1070", "order-1070"
    for match in ORD_FLEXIBLE_REGEX.finditer(text):
        digits = match.group(1)
        norm = f"ORD-{digits}"
        if norm not in results:
            results.append(norm)
    return results

HINGLISH_WORDS = {
    "kab",
    "kahan",
    "mera",
    "meri",
    "mere",
    "aayega",
    "pahuncha",
    "hai",
    "hain",
    "wapas",
    "karo",
    "kardo",
    "karna",
    "chahiye",
    "nahi",
    "toota",
    "kharab",
    "paise",
    "paisa",
    "bhai",
    "bhaiya",
    "samaan",
    "galat",
    "alag",
    "kripya",
    "kya",
    "kaise",
    "ho",
    "aap",
    "tumhari",
    "chor",
    "bakwas",
    "kitne",
    "din",
    "me",
    "mein",
    "bhejo",
    "bhej",
    "diya",
    "baat",
    "karni",
    "se",
    "nikla",
    "ka",
    "ki",
    "ke",
    "kisi",
    "bot",
}

ABUSE_WORDS = {
    "stupid",
    "idiot",
    "useless",
    "garbage",
    "bakwas",
    "chor",
    "scam",
    "cheat",
    "scammers",
    "cheater",
    "harami",
    "kutta",
    "fraudster",
}

HUMAN_PHRASES = [
    r"\bhuman\b",
    r"\bagent\b",
    r"\bmanager\b",
    r"\binsaan\b",
    r"\bperson\b",
    r"\brepresentative\b",
    r"\bexecutive\b",
    r"\btalk\s+to\s+(?:a\s+)?(?:human|person|agent|manager|someone)\b",
    r"\bspeak\s+to\s+(?:a\s+)?(?:human|person|agent|manager|someone)\b",
    r"\bbaat\s+karao\b",
]

SHIPPING_PHRASES = [
    r"\btrack(?:ing)?\b",
    r"\bpackage\b",
    r"\bshipment\b",
    r"\bcourier\b",
    r"\bin\s+transit\b",
    r"\bkahan\s+tak\s+pahuncha\b",
]

POLICY_PHRASES = [
    r"\breturn\s+policy\b",
    r"\bpolicy\b",
    r"\bkitne\s+din\b",
    r"\bhow\s+many\s+days\b",
    r"\b(?:return|refund|store|cancellation)\s+rules?\b",
    r"\brules?\s+(?:for|of)\b",
    r"\bwhat\s+(?:are\s+the|is\s+the)\s+rules?\b",
]

SMALL_TALK_WORDS = [
    r"^(?:hi|hello|hey|namaste|good\s+morning|good\s+afternoon|good\s+evening)\b",
    r"\bkaise\s+ho\b",
    r"\bhow\s+are\s+you\b",
]

OFF_TOPIC_WORDS = [
    r"\bpython\b",
    r"\bcricket\b",
    r"\bprimes?\b",
    r"\bweather\b",
    r"\bmaintenance\s+mode\b",
    r"\btoken(?:s)?\b",
    r"\bwho\s+won\b",
]

ORDER_STATUS_PHRASES = [
    r"\bwhere\s+is\b",
    r"\bwher\s+is\b",
    r"\border\s+status\b",
    r"\bkab\s+aayega\b",
    r"\bdelivery\b",
    r"\barrive\b",
    r"\blate\b",
    r"\blayte\b",
    r"\bordr\b",
    r"\bstatus\b",
]


class DeterministicRouter:
    """Deterministic fallback router implementing regex and keyword classification.

    Spec §4.2:
    Regex for ORD ids; keyword/phrase rules in English and Hinglish;
    reason codes; language detection; human/legal escalation detection;
    confidence capped at 0.5.
    """

    def __init__(self) -> None:
        try:
            policy = load_policy()
            self._escalation_keywords = [k.lower() for k in policy.escalation_keywords]
        except Exception:
            self._escalation_keywords = [
                "lawyer",
                "legal",
                "court",
                "consumer forum",
                "consumer court",
                "fraud",
                "sue",
                "police",
                "chargeback",
            ]
        if "consumer court" not in self._escalation_keywords:
            self._escalation_keywords.append("consumer court")
        if "chargeback" not in self._escalation_keywords:
            self._escalation_keywords.append("chargeback")

    def route(self, message: str, context: Any = None) -> Understanding:
        """Route customer message to an Understanding object with confidence <= 0.5."""
        text = message.strip()
        lower_text = text.lower()

        # 1. Extract ORD IDs
        order_ids = extract_order_ids(text)

        # 2. Language Detection
        if DEVANAGARI_REGEX.search(text):
            language = Language.HI
        else:
            words = set(re.findall(r"\b\w+\b", lower_text))
            hinglish_overlap = words & HINGLISH_WORDS
            hinglish_phrases = [
                "kab aayega",
                "wapas karo",
                "kahan tak",
                "toota hua",
                "paisa wapas",
            ]
            if len(hinglish_overlap) >= 2 or any(p in lower_text for p in hinglish_phrases):
                language = Language.HINGLISH
            elif any(w.isalpha() for w in words):
                language = Language.EN
            else:
                language = Language.OTHER

        # 3. Detect Injection
        safety_flags = detect_injection_heuristics(text)

        # 4. Human & Legal Escalation Detection
        asks_for_human = False
        has_legal_keyword = any(kw in lower_text for kw in self._escalation_keywords)
        has_human_phrase = any(re.search(pat, lower_text) for pat in HUMAN_PHRASES)

        if has_legal_keyword or has_human_phrase:
            asks_for_human = True

        # 5. Reason Code & Text Extraction
        reason_code = ReasonCode.OTHER
        if any(w in lower_text for w in ["defective", "not working", "kharab", "faulty"]):
            reason_code = ReasonCode.DEFECTIVE
        elif any(w in lower_text for w in ["damaged", "toota", "broken", "cracked", "shattered"]):
            reason_code = ReasonCode.DAMAGED
        elif any(w in lower_text for w in ["wrong", "galat", "incorrect"]):
            reason_code = ReasonCode.WRONG_ITEM
        elif any(w in lower_text for w in ["not as described", "different from picture"]):
            reason_code = ReasonCode.NOT_AS_DESCRIBED
        elif any(
            w in lower_text
            for w in ["changed mind", "nahi chahiye", "wrong model", "ordered wrong"]
        ):
            reason_code = ReasonCode.CHANGED_MIND
        elif any(
            re.search(rf"\b{re.escape(w)}\b", lower_text)
            for w in ["late", "delayed", "der", "layte"]
        ):
            reason_code = ReasonCode.LATE

        reason_text = text[:200] if reason_code != ReasonCode.OTHER else ""

        # 6. Sentiment Detection
        sentiment = Sentiment.CALM
        issue_codes = (
            ReasonCode.DEFECTIVE,
            ReasonCode.DAMAGED,
            ReasonCode.LATE,
            ReasonCode.WRONG_ITEM,
        )
        if any(w in lower_text for w in ABUSE_WORDS) or has_legal_keyword:
            sentiment = Sentiment.ANGRY
        elif reason_code in issue_codes or asks_for_human:
            sentiment = Sentiment.FRUSTRATED

        # 7. Intent Classification
        intent = self._classify_intent(
            lower_text=lower_text,
            has_legal_keyword=has_legal_keyword,
            has_human_phrase=has_human_phrase,
            reason_code=reason_code,
            order_ids=order_ids,
            safety_flags=safety_flags,
        )

        # Secondary intents
        secondary_intents: list[Intent] = []
        if intent == Intent.RETURN_REQUEST and reason_code == ReasonCode.DAMAGED:
            secondary_intents.append(Intent.DAMAGED_ITEM)
        elif intent == Intent.RETURN_REQUEST and reason_code == ReasonCode.DEFECTIVE:
            secondary_intents.append(Intent.DAMAGED_ITEM)
        elif intent == Intent.ORDER_STATUS and any(
            re.search(pat, lower_text) for pat in SHIPPING_PHRASES
        ):
            secondary_intents.append(Intent.SHIPPING_STATUS)

        # Router confidence is strictly at most 0.5
        confidence = 0.48

        # Clarification slot
        needs_clarification = True
        clarification_slot = ClarificationSlot.NONE
        if not order_ids and intent in (
            Intent.ORDER_STATUS,
            Intent.SHIPPING_STATUS,
            Intent.RETURN_REQUEST,
            Intent.REFUND_STATUS,
            Intent.REFUND_REQUEST,
            Intent.CANCELLATION,
            Intent.DAMAGED_ITEM,
            Intent.WRONG_ITEM,
        ):
            clarification_slot = ClarificationSlot.ORDER_ID

        return Understanding(
            intent=intent,
            confidence=confidence,
            order_ids=order_ids,
            item_mentions=[],
            reason_code=reason_code,
            reason_text=reason_text,
            language=language,
            sentiment=sentiment,
            asks_for_human=asks_for_human,
            secondary_intents=secondary_intents,
            needs_clarification=needs_clarification,
            clarification_slot=clarification_slot,
            safety_flags=safety_flags,
        )

    def _classify_intent(
        self,
        lower_text: str,
        has_legal_keyword: bool,
        has_human_phrase: bool,
        reason_code: ReasonCode,
        order_ids: list[str],
        safety_flags: list[SafetyFlag] | None = None,
    ) -> Intent:
        # Injection check: message attempting prompt injection without legitimate
        # order routes to off-topic
        if safety_flags and not order_ids:
            return Intent.OFF_TOPIC

        # Abuse check
        if any(w in lower_text for w in ABUSE_WORDS):
            return Intent.ABUSE

        # Legal threat / Human agent request
        if has_legal_keyword or has_human_phrase:
            if not any(k in lower_text for k in ["cancel", "return", "refund", "track", "status"]):
                return Intent.HUMAN_REQUEST

        # Cancellation
        if any(w in lower_text for w in ["cancel", "cancellation", "cancel kardo"]):
            return Intent.CANCELLATION

        # Policy question (check before generic return)
        if (
            any(re.search(pat, lower_text) for pat in POLICY_PHRASES)
            or "how to return" in lower_text
        ):
            return Intent.POLICY_QUESTION

        # Return request vs Damaged/Wrong item
        has_return = any(
            w in lower_text for w in ["return", "wapas", "replace", "send back", "exchange"]
        )
        if (
            has_return
            or (order_ids and reason_code in (ReasonCode.DAMAGED, ReasonCode.DEFECTIVE))
            or (
                order_ids
                and any(
                    w in lower_text
                    for w in [
                        "damaged",
                        "kharab",
                        "defective",
                        "broken",
                        "toota",
                        "cracked",
                        "shattered",
                    ]
                )
            )
        ):
            return Intent.RETURN_REQUEST

        # Refund status vs Refund request
        has_refund = any(k in lower_text for k in ["refund", "paisa wapas", "paise wapas"])
        if has_refund:
            status_tokens = ["when will", "status", "credited", "kab milega", "where is my refund"]
            if any(w in lower_text for w in status_tokens):
                return Intent.REFUND_STATUS
            return Intent.REFUND_REQUEST

        # If reported damaged or cracked without explicit return keyword
        damaged_tokens = [
            "cracked",
            "shattered",
            "broken",
            "toota",
            "damaged screen",
            "screen",
        ]
        if reason_code == ReasonCode.DAMAGED and any(w in lower_text for w in damaged_tokens):
            return Intent.DAMAGED_ITEM

        # If wrong item reported without explicit return keyword
        if reason_code == ReasonCode.WRONG_ITEM or "wrong size" in lower_text:
            return Intent.WRONG_ITEM

        # Off topic (e.g. general questions without order ids)
        if any(re.search(pat, lower_text) for pat in OFF_TOPIC_WORDS):
            return Intent.OFF_TOPIC

        # Shipping status vs Order status
        if any(re.search(pat, lower_text) for pat in SHIPPING_PHRASES):
            return Intent.SHIPPING_STATUS

        if any(re.search(pat, lower_text) for pat in ORDER_STATUS_PHRASES) or (
            order_ids and "?" in lower_text
        ):
            return Intent.ORDER_STATUS

        # Small talk
        if any(re.search(pat, lower_text) for pat in SMALL_TALK_WORDS):
            return Intent.SMALL_TALK

        # Default fallback
        if order_ids:
            return Intent.ORDER_STATUS
        return Intent.OFF_TOPIC
