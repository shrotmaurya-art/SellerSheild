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
ORD_FLEXIBLE_REGEX = re.compile(
    r"\b(?:ORD|order|ordr|odr)\s*(?:-|#|:|\s)\s*(\d{3,6})\b", re.IGNORECASE
)
ORD_HINDI_REGEX = re.compile(r"(?:^|[\s,(])(?:ऑर्डर|आर्डर)\s*(?:-|#|:|\s)\s*(\d{3,6})\b")
DEVANAGARI_REGEX = re.compile(r"[\u0900-\u097F]")
_DEVANAGARI_DIGITS_MAP = str.maketrans("०१२३४५६७८९", "0123456789")


def normalize_order_id(raw: str | None) -> str | None:
    """Normalize order ID variants (e.g. '1070', 'ord 1070', 'ORD-१०४८') to canonical 'ORD-XXXX'."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.translate(_DEVANAGARI_DIGITS_MAP).strip().upper()
    if re.match(r"^ORD-\d{3,6}$", cleaned):
        return cleaned
    if re.match(r"^\d{3,6}$", cleaned):
        return f"ORD-{cleaned}"
    m = re.match(
        r"^(?:ORD|ORDER|ORDR|ODR|ऑर्डर|आर्डर)\s*(?:-|#|:|\s)\s*(\d{3,6})$",
        cleaned,
        re.IGNORECASE,
    )
    if m:
        return f"ORD-{m.group(1)}"
    return None


def extract_order_ids(text: str) -> list[str]:
    """Extract and normalize all order IDs found in text (including Devanagari digits)."""
    if not text:
        return []
    norm_text = text.translate(_DEVANAGARI_DIGITS_MAP)
    results: list[str] = []
    # 1. Canonical ORD-XXXX
    for match in ORD_CANONICAL_REGEX.findall(norm_text):
        norm = match.upper()
        if norm not in results:
            results.append(norm)
    # 2. Flexible variants: "ord 1070", "order #1070", "order-1070", "ऑर्डर 1048"
    for match in ORD_FLEXIBLE_REGEX.finditer(norm_text):
        digits = match.group(1)
        norm = f"ORD-{digits}"
        if norm not in results:
            results.append(norm)
    for match in ORD_HINDI_REGEX.finditer(norm_text):
        digits = match.group(1)
        norm = f"ORD-{digits}"
        if norm not in results:
            results.append(norm)
    return results

HINGLISH_WORDS = {
    "kab",
    "kahan",
    "kaha",
    "kidhar",
    "mera",
    "meri",
    "mere",
    "mujhe",
    "humara",
    "aapka",
    "aayega",
    "aayegi",
    "pahuncha",
    "pahunchega",
    "hai",
    "hain",
    "tha",
    "thi",
    "hua",
    "hui",
    "wapas",
    "vapas",
    "vaapas",
    "wapis",
    "vapis",
    "kar",
    "karo",
    "kardo",
    "karna",
    "karni",
    "kariye",
    "kijiye",
    "chahiye",
    "chahie",
    "nahi",
    "nhi",
    "mat",
    "haan",
    "hanji",
    "toota",
    "tuta",
    "kharab",
    "paise",
    "paisa",
    "rupaye",
    "bhai",
    "bhaiya",
    "samaan",
    "saman",
    "galat",
    "alag",
    "kripya",
    "kya",
    "kaise",
    "kyu",
    "kyun",
    "ho",
    "aap",
    "tumhari",
    "chor",
    "bakwas",
    "kitne",
    "kitna",
    "din",
    "tak",
    "abhi",
    "jaldi",
    "me",
    "mein",
    "bhejo",
    "bhej",
    "bhejna",
    "diya",
    "baat",
    "karao",
    "se",
    "nikla",
    "nikli",
    "milega",
    "milegi",
    "mila",
    "mili",
    "radd",
    "pasand",
    "dikhao",
    "dikhaye",
    "batao",
    "bataiye",
    "batana",
    "sab",
    "sabhi",
    "saare",
    "sare",
    "ek",
    "teen",
    "chaar",
    "paanch",
    "ka",
    "ki",
    "ke",
    "kisi",
    "bot",
    "parcel",
    "krdo",
    "karde",
    "krna",
    "dikhado",
    "dikha",
    "bata",
    "batayiye",
    "milenge",
    "milengi",
    "aayenge",
    "aaye",
    "pahunchi",
    "pahunche",
    "pahunchegi",
    "rad",
    "madad",
    "apna",
    "apne",
    "kharaab",
    "sahayata",
    "karke",
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
    r"\bbaat\s+(?:karao|karna|karni)\b",
    r"इंसान",
    r"व्यक्ति",
    r"एजेंट",
    r"मैनेजर",
    r"कस्टमर\s*केयर",
    r"बात\s*(?:करनी|करा)",
]

SHIPPING_PHRASES = [
    r"\btrack(?:ing)?\b",
    r"\bpackage\b",
    r"\bshipment\b",
    r"\bcourier\b",
    r"\bin\s+transit\b",
    r"\bkahan\s+tak\s+(?:pahuncha|aaya)\b",
    r"ट्रैक",
    r"शिपमेंट",
    r"कूरियर",
]

POLICY_PHRASES = [
    r"\breturn\s+policy\b",
    r"\bpolicy\b",
    r"\bkitne\s+din\b",
    r"\bhow\s+many\s+days\b",
    r"\b(?:return|refund|store|cancellation)\s+rules?\b",
    r"\brules?\s+(?:for|of)\b",
    r"\bwhat\s+(?:are\s+the|is\s+the)\s+rules?\b",
    r"नीति",
    r"पॉलिसी",
    r"नियम",
]

SMALL_TALK_WORDS = [
    r"^(?:hi|hello|hey|namaste|good\s+morning|good\s+afternoon|good\s+evening|नमस्ते|नमस्कार)(?:\b|[\s,!।?]|$)",
    r"\bkaise\s+ho\b",
    r"\bhow\s+are\s+you\b",
    r"कैसे\s+हो",
    r"कैसे\s+हैं",
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
    r"\bkab\s+(?:aayega|aayegi|aayenge|pahunchega|pahunchegi|tak|milega)\b",
    r"\b(?:kahan|kaha)\s+(?:hai|pahuncha|pahunchi|pahunche|tak)\b",
    r"\bkidhar\s+hai\b",
    r"\bdelivery\b",
    r"\barrive\b",
    r"\blate\b",
    r"\blayte\b",
    r"\bordr\b",
    r"\bstatus\b",
    r"\b(?:list|show|get|all|my|mere|sabhi|saare|sare|sab)\b.*\b(?:order|orders|product|products|item|items)\b",
    r"\b(?:orders|products)\b",
    r"ऑर्डर",
    r"पार्सल",
    r"कहाँ",
    r"कब\s+आएगा",
    r"डिलीवरी",
    r"स्टेटस",
    r"स्थिति",
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
                "kab aayegi",
                "kab aayenge",
                "kab tak",
                "wapas karo",
                "wapas karna",
                "wapas aayenge",
                "vapas karo",
                "kahan tak",
                "kahan hai",
                "kaha hai",
                "toota hua",
                "tuta hua",
                "paisa wapas",
                "paise wapas",
                "paise kab",
                "paisa kab",
                "cancel kardo",
                "cancel krdo",
                "cancel karna",
                "cancel karo",
                "kab milega",
                "kab milenge",
                "bata do",
                "mere orders",
                "sabhi orders",
                "orders dikhao",
                "order dikhao",
                "baat karao",
                "policy kya",
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
        if any(
            w in lower_text
            for w in ["defective", "not working", "kharab", "faulty", "खराब", "काम नहीं"]
        ):
            reason_code = ReasonCode.DEFECTIVE
        elif any(
            w in lower_text
            for w in ["damaged", "toota", "tuta", "broken", "cracked", "shattered", "टूटा", "फूटा"]
        ):
            reason_code = ReasonCode.DAMAGED
        elif any(w in lower_text for w in ["wrong", "galat", "incorrect", "गलत"]):
            reason_code = ReasonCode.WRONG_ITEM
        elif any(w in lower_text for w in ["not as described", "different from picture"]):
            reason_code = ReasonCode.NOT_AS_DESCRIBED
        elif any(
            w in lower_text
            for w in [
                "changed mind",
                "nahi chahiye",
                "pasand nahi",
                "wrong model",
                "ordered wrong",
                "नहीं चाहिए",
                "मन बदल",
            ]
        ):
            reason_code = ReasonCode.CHANGED_MIND
        elif any(
            re.search(rf"\b{re.escape(w)}\b", lower_text)
            for w in ["late", "delayed", "der", "layte"]
        ) or any(w in lower_text for w in ["देर", "देरी"]):
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
            if not any(
                k in lower_text
                for k in [
                    "cancel",
                    "return",
                    "refund",
                    "track",
                    "status",
                    "रद्द",
                    "कैंसल",
                    "रिटर्न",
                    "रिफंड",
                    "ट्रैक",
                ]
            ):
                return Intent.HUMAN_REQUEST

        # Cancellation
        if re.search(
            r"\bcancel(?:l?(?:ation|ed|ing)?|kardo|krdo|karna|karo)\b|\bradd\b|\brad\s+kar",
            lower_text,
        ) or any(w in lower_text for w in ["रद्द", "कैंसल", "कैंसलेशन", "निरस्त"]):
            return Intent.CANCELLATION

        # Policy question (check before generic return)
        if (
            any(re.search(pat, lower_text) for pat in POLICY_PHRASES)
            or "how to return" in lower_text
        ):
            return Intent.POLICY_QUESTION

        # Refund status vs Refund request (check before return for paisa/returned-order refunds)
        has_refund = bool(
            re.search(
                r"\brefund\b|\b(?:paisa|paise)\b.*\b(?:wapas|vapas|vaapas|wapis|vapis|milega|milegi|milenge|aaya|aayega|aayegi|aayenge)\b",
                lower_text,
            )
        ) or any(w in lower_text for w in ["रिफंड", "पैसा वापस", "पैसे वापस", "पैसे कब"])
        has_active_item_return = bool(
            re.search(
                r"\b(?:replace|exchange|send\s+back)\b|\b(?:samaan|saman|item|product|kurta)\s+(?:wapas|vapas|vaapas|wapis|return)\b|\breturn\b(?!ed\b)",
                lower_text,
            )
        ) or any(w in lower_text for w in ["सामान वापस", "समान वापस", "रिटर्न करना", "बदलना"])
        if has_refund and not has_active_item_return:
            status_tokens = [
                "when will",
                "status",
                "credited",
                "kab milega",
                "kab milenge",
                "kab aayega",
                "kab aayenge",
                "kab tak",
                "where is",
                "what happened",
                "kahan",
                "kaha",
                "kidhar",
                "milega",
                "milenge",
                "aayenge",
                "pahuncha",
                "pahunchi",
                "pahunche",
                "kya hai",
                "aaya kya",
                "स्थिति",
                "स्टेटस",
                "कब",
                "कहाँ",
                "पहुँचा",
            ]
            if any(w in lower_text for w in status_tokens):
                return Intent.REFUND_STATUS
            return Intent.REFUND_REQUEST

        # Return request vs Damaged/Wrong item
        has_return = bool(
            re.search(
                r"\b(?:return(?:ing|ed)?|wapas|vapas|vaapas|wapis|replace|send\s+back|exchange)\b",
                lower_text,
            )
        ) or any(w in lower_text for w in ["रिटर्न", "वापस", "लौटाना", "वापसी", "एक्सचेंज"])
        if has_return:
            return Intent.RETURN_REQUEST

        if has_refund:
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
