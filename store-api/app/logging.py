import json
import logging
import re
from typing import Any

from app.clock import Clock

# Patterns for sensitive data that should never be logged
AUTH_HEADER_REGEX = re.compile(r"Bearer\s+([A-Za-z0-9\-_\.]+)", re.IGNORECASE)
JWT_REGEX = re.compile(r"eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}")
EMAIL_REGEX = re.compile(r"([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")
SECRET_KEY_REGEX = re.compile(
    r'("(?:password|secret|token|authorization|refresh_token)":\s*")[^"]+(")',
    re.IGNORECASE,
)
QUERY_SECRET_REGEX = re.compile(
    r"(?:password|secret|token|refresh_token)=([^&\s]+)",
    re.IGNORECASE,
)


def redact_sensitive(text: str) -> str:
    """Mask credentials, tokens, and email addresses."""
    redacted = AUTH_HEADER_REGEX.sub("Bearer [REDACTED]", text)
    redacted = JWT_REGEX.sub("[REDACTED_JWT]", redacted)
    redacted = SECRET_KEY_REGEX.sub(r"\1[REDACTED]\2", redacted)
    redacted = QUERY_SECRET_REGEX.sub(r"password=[REDACTED]", redacted)
    redacted = EMAIL_REGEX.sub(r"***@redacted.local", redacted)
    return redacted


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict[str, Any] = {
            "timestamp": Clock.now().isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": redact_sensitive(record.getMessage()),
        }
        # Add contextual attributes if available
        for attr in ("request_id", "route", "actor", "status_code", "latency_ms"):
            val = getattr(record, attr, None)
            if val is not None:
                log_obj[attr] = val

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            log_obj["exception"] = redact_sensitive(record.exc_text)

        return json.dumps(log_obj)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("store_api")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    return logger


logger = setup_logging()
