import logging
import re
import sys
from collections.abc import Mapping
from typing import Any, cast

import structlog
from structlog.types import EventDict, WrappedLogger

SENSITIVE_KEY_PATTERNS = {
    "authorization",
    "api_key",
    "token",
    "access_token",
    "password",
    "secret",
    "cookie",
}

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
JWT_REGEX = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9._-]{10,}")
KEY_PATTERN_REGEX = re.compile(r"(?:AI" + r"za|gs" + r"k_)[A-Za-z0-9_-]{8,}")
BEARER_REGEX = re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE)
PHONE_REGEX = re.compile(
    r"(?<![A-Za-z0-9])(?:\+\d{1,3}[\s-]?)?\(?\d{2,5}\)?[\s-]?\d{3,5}[\s-]?\d{4}(?![A-Za-z0-9])"
)


def _sanitize_string(val: str) -> str:
    if not val:
        return ""
    val = BEARER_REGEX.sub("[REDACTED_AUTH]", val)
    val = JWT_REGEX.sub("[REDACTED_TOKEN]", val)
    val = KEY_PATTERN_REGEX.sub("[REDACTED_KEY]", val)
    val = EMAIL_REGEX.sub("[REDACTED_EMAIL]", val)
    val = PHONE_REGEX.sub("[REDACTED_PHONE]", val)
    return val


def _sanitize_value(val: Any) -> Any:
    if isinstance(val, str):
        return _sanitize_string(val)
    elif isinstance(val, Mapping):
        return _sanitize_dict(val)
    elif isinstance(val, list):
        return [_sanitize_value(v) for v in val]
    return val


def _sanitize_dict(d: Mapping[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for k, v in d.items():
        k_lower = str(k).lower()
        if any(pat in k_lower for pat in SENSITIVE_KEY_PATTERNS):
            cleaned[k] = "[REDACTED]"
        else:
            cleaned[k] = _sanitize_value(v)
    return cleaned


def redact_sensitive_processor(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Processor to redact passwords, tokens, API keys, cookies, and email addresses."""
    return _sanitize_dict(event_dict)


class SensitiveRedactionFilter(logging.Filter):
    """Filters standard logging records to prevent token, key, email, or phone leaks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _sanitize_string(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _sanitize_string(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    _sanitize_string(v) if isinstance(v, str) else v for v in record.args
                )
        return True


def configure_logging() -> None:
    """Configure structured JSON logging with safe redaction and request_id binding."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )
    logging.getLogger().addFilter(SensitiveRedactionFilter())

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_sensitive_processor,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "sellershield_agent") -> structlog.BoundLogger:
    return cast(structlog.BoundLogger, structlog.get_logger(name))
