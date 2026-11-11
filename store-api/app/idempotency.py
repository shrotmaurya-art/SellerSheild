import hashlib
import json
from typing import Any, Optional

from fastapi import Header
from sqlalchemy.orm import Session

from app.clock import Clock
from app.errors import IdempotencyMismatchException, ValidationErrorException
from app.models import IdempotencyKey


def compute_payload_hash(raw_body: bytes) -> str:
    """Compute deterministic SHA-256 hash of the payload, normalizing JSON."""
    if not raw_body:
        return hashlib.sha256(b"").hexdigest()
    try:
        parsed = json.loads(raw_body)
        normalized = json.dumps(parsed, sort_keys=True).encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()
    except Exception:
        return hashlib.sha256(raw_body).hexdigest()


def require_idempotency_key(
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> str:
    """Validate that Idempotency-Key header is present."""
    if not idempotency_key or not idempotency_key.strip():
        raise ValidationErrorException(message="Missing required Idempotency-Key header")
    return idempotency_key.strip()


def check_idempotency_replay(
    session: Session,
    key: str,
    payload_hash: str,
) -> Optional[tuple[int, Any]]:
    """
    Check if this idempotency key was previously processed.

    Same key + same payload: returns (status_code, response_data).
    Same key + different payload: raises IdempotencyMismatchException.
    Not seen: returns None.
    """
    existing = session.get(IdempotencyKey, key)
    if existing is None:
        return None

    if existing.request_hash != payload_hash:
        raise IdempotencyMismatchException(message="Idempotency key payload mismatch")

    response_data = json.loads(existing.response_json) if existing.response_json else None
    return existing.status_code or 200, response_data


def save_idempotency_record(
    session: Session,
    key: str,
    payload_hash: str,
    status_code: int,
    response_data: Any,
    client_id: Optional[str] = None,
) -> None:
    """Store idempotency record in the database."""
    record = IdempotencyKey(
        key=key,
        client_id=client_id,
        request_hash=payload_hash,
        response_json=json.dumps(response_data, default=str),
        status_code=status_code,
        created_at=Clock.now(),
    )
    session.add(record)
