"""Sanitization and redaction for Decision Trace and audit export.

Ensures no Bearer tokens, JWTs, secrets, emails, phones, or raw HTML tags
leak into seller decision traces or external representations.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

from sellershield_agent.logging import EMAIL_REGEX

_JWT_REGEX = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9._-]+")
_TOK_REGEX = re.compile(r"\btok_[A-Za-z0-9_]+\b")
_WHSEC_REGEX = re.compile(r"\bwhsec_[A-Za-z0-9_]+\b")
_KEY_PATTERN_REGEX = re.compile(r"(?:AI" + r"za|gs" + r"k_)[A-Za-z0-9_-]{8,}")
_BEARER_REGEX = re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE)
_BEARER_PREFIX_REGEX = re.compile(r"\bBearer\s+", re.IGNORECASE)
_PHONE_REGEX = re.compile(
    r"(?<![A-Za-z0-9])(?:\+\d{1,3}[\s-]?)?\(?\d{2,5}\)?[\s-]?\d{3,5}[\s-]?\d{4}(?![A-Za-z0-9])"
)

_FORBIDDEN_KEYS = {
    "authorization",
    "token",
    "access_token",
    "secret",
    "password",
    "api_key",
}


def redact_secrets_and_pii(text: str) -> str:
    """Redact credentials and PII without HTML escaping."""
    if not text:
        return ""
    val = text
    # 1. Bearer tokens and prefixes
    val = _BEARER_REGEX.sub("[REDACTED_AUTH]", val)
    val = _BEARER_PREFIX_REGEX.sub("[REDACTED_AUTH] ", val)
    # 2. JWTs and tokens
    val = _JWT_REGEX.sub("[REDACTED_TOKEN]", val)
    val = _TOK_REGEX.sub("[REDACTED_TOKEN]", val)
    val = _WHSEC_REGEX.sub("[REDACTED_SECRET]", val)
    val = _KEY_PATTERN_REGEX.sub("[REDACTED_KEY]", val)
    # 3. PII (emails and phones)
    val = EMAIL_REGEX.sub("[REDACTED_EMAIL]", val)
    return _PHONE_REGEX.sub("[REDACTED_PHONE]", val)


def sanitize_trace_text(text: str) -> str:
    """Redact credentials/PII and HTML-escape free text deterministically."""
    val = redact_secrets_and_pii(text)
    return html.escape(val, quote=True)


def sanitize_trace_data(data: Any) -> Any:
    """Recursively sanitize dicts, lists, and strings for Decision Trace output."""
    if isinstance(data, str):
        return sanitize_trace_text(data)
    if isinstance(data, Mapping):
        cleaned: dict[str, Any] = {}
        for k, v in data.items():
            k_str = str(k)
            if k_str.lower() in _FORBIDDEN_KEYS:
                continue
            cleaned[k_str] = sanitize_trace_data(v)
        return cleaned
    if isinstance(data, (list, tuple)):
        return [sanitize_trace_data(item) for item in data]
    return data


def sanitize_details(
    details: Any,
    actor_id: str | None = None,
    is_denial: bool = False,
) -> str:
    """Sanitize and serialize event details to deterministic JSON string for audit storage."""
    import json

    if details is None:
        return "{}"

    # 1. First validate that the object can be represented as JSON
    if isinstance(details, str):
        try:
            parsed = json.loads(details)
        except Exception:
            raise ValueError("Details string is not JSON valid") from None
    elif isinstance(details, (set, frozenset)):
        raise ValueError("Details object is not JSON serializable")
    else:
        try:
            json.dumps(details)
            parsed = details
        except (TypeError, ValueError) as exc:
            raise ValueError("Details object is not JSON serializable") from exc

    # 2. Recursive sanitization
    def _clean(val: Any) -> Any:
        if isinstance(val, str):
            val = val[:500]
            val = re.sub(r"(?i)authorization", "[REDACTED]", val)
            val = EMAIL_REGEX.sub("[REDACTED_EMAIL]", val)
            val = _JWT_REGEX.sub("[REDACTED_TOKEN]", val)
            val = _BEARER_REGEX.sub("[REDACTED_AUTH]", val)
            val = _BEARER_PREFIX_REGEX.sub("[REDACTED_AUTH] ", val)
            val = _TOK_REGEX.sub("[REDACTED_TOKEN]", val)
            val = _WHSEC_REGEX.sub("[REDACTED_SECRET]", val)
            val = _KEY_PATTERN_REGEX.sub("[REDACTED_KEY]", val)
            val = _PHONE_REGEX.sub("[REDACTED_PHONE]", val)
            if is_denial:
                def _scrub_cust(m: re.Match[str]) -> str:
                    cid = m.group(0)
                    return cid if cid == actor_id else "[REDACTED_CUSTOMER]"
                val = re.sub(r"\b(CUST-\w+)\b", _scrub_cust, val)
            return val
        if isinstance(val, Mapping):
            cleaned_map: dict[str, Any] = {}
            for k, v in val.items():
                k_str = str(k)[:500]
                k_clean = re.sub(r"(?i)authorization", "[REDACTED]", k_str)
                k_lower = k_str.lower()
                if k_lower in ("token", "access_token", "secret", "password"):
                    cleaned_map[k_clean] = "[REDACTED]"
                else:
                    cleaned_map[k_clean] = _clean(v)
            return cleaned_map
        if isinstance(val, (list, tuple)):
            return [_clean(item) for item in val]
        return val

    cleaned = _clean(parsed)
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"))

