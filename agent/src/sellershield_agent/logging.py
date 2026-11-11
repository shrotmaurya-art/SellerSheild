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


def _sanitize_value(val: Any) -> Any:
    if isinstance(val, str):
        if EMAIL_REGEX.search(val):
            return EMAIL_REGEX.sub("[REDACTED_EMAIL]", val)
        return val
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


def configure_logging() -> None:
    """Configure structured JSON logging with safe redaction and request_id binding."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

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
