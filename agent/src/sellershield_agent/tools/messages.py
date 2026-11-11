"""Customer-safe error messages in English and Hindi.

KP-23 & Amendment 6:
Customer-facing error messages must never mention internal limits,
thresholds, risk scores, staff roles, stack traces, or technical internals.
"""

from __future__ import annotations

from sellershield_agent.tools.base import ToolErrorCode

MESSAGES: dict[ToolErrorCode, dict[str, str]] = {
    ToolErrorCode.not_found: {
        "en": "We could not find the requested order or item. Please verify your details.",
        "hi": "हमें अनुरोधित ऑर्डर या वस्तु नहीं मिली। कृपया अपने विवरण की जाँच करें।",
    },
    ToolErrorCode.not_permitted: {
        "en": "This action is not available right now.",
        "hi": "यह कार्रवाई अभी उपलब्ध नहीं है।",
    },
    ToolErrorCode.invalid_args: {
        "en": "The provided details are invalid. Please check and try again.",
        "hi": "प्रदान किए गए विवरण अमान्य हैं। कृपया जाँचें और पुनः प्रयास करें।",
    },
    ToolErrorCode.policy_denied: {
        "en": "This request cannot be approved based on our store policy.",
        "hi": "स्टोर नीति के अनुसार इस अनुरोध को स्वीकृत नहीं किया जा सकता है।",
    },
    ToolErrorCode.stale_state: {
        "en": "The order details have changed. Please refresh and try again.",
        "hi": "ऑर्डर के विवरण बदल गए हैं। कृपया रीफ़्रेश करें और पुनः प्रयास करें।",
    },
    ToolErrorCode.rate_limited: {
        "en": "Too many requests. Please wait a moment and try again.",
        "hi": "बहुत सारे अनुरोध प्राप्त हुए। कृपया थोड़ी देर प्रतीक्षा करें और पुनः प्रयास करें।",
    },
    ToolErrorCode.upstream_unavailable: {
        "en": "The service is temporarily unavailable. Please try again shortly.",
        "hi": "सेवा अस्थायी रूप से अनुपलब्ध है। कृपया थोड़ी देर बाद पुनः प्रयास करें।",
    },
    ToolErrorCode.timeout: {
        "en": "The request timed out. Please try again shortly.",
        "hi": "अनुरोध का समय समाप्त हो गया। कृपया थोड़ी देर बाद पुनः प्रयास करें।",
    },
    ToolErrorCode.internal: {
        "en": "An unexpected error occurred. Please try again.",
        "hi": "एक अप्रत्याशित त्रुटि हुई। कृपया पुनः प्रयास करें।",
    },
}


def get_customer_safe_message(code: ToolErrorCode, lang: str = "en") -> str:
    """Retrieve customer-safe localized message for a tool error code."""
    lang_key = "hi" if lang.lower().startswith("hi") else "en"
    code_dict = MESSAGES.get(code, MESSAGES[ToolErrorCode.internal])
    return code_dict.get(lang_key, code_dict["en"])
