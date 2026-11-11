"""Canonical proposal card extraction and deterministic SHA-256 hashing.

Ensures 'what you saw is what you approved' (Spec §2, §6.1, §10).
The canonical card strictly contains only the fields presented to the customer:
type, order_id, items with quantities, amounts breakdown in integer paise,
refund method, policy decision, policy_version, and expires_at.
It never includes model-written text or unverified customer statements.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def canonical_card(
    *,
    type: str,
    order_id: str,
    items: list[dict[str, Any]],
    amounts: dict[str, int],
    method: str,
    decision: str,
    policy_version: str,
    expires_at: str | datetime,
) -> dict[str, Any]:
    """Construct the canonical customer-facing card dictionary.

    - Sorts items deterministically by item_id.
    - Strictly validates and preserves integer paise amounts.
    - Standardizes expires_at to an ISO 8601 UTC string.
    - Omits any model text, greetings, explanations, or commentary.
    """
    if amounts is None or not isinstance(amounts, dict):
        raise TypeError("amounts must be a non-null dictionary of integer paise")

    # Standardize and sort items deterministically
    normalized_items: list[dict[str, Any]] = sorted(
        [{"item_id": str(item["item_id"]), "quantity": int(item["quantity"])} for item in items],
        key=lambda x: str(x["item_id"]),
    )

    # Standardize amounts breakdown (strictly integer paise)
    normalized_amounts: dict[str, int] = {str(k): int(v) for k, v in amounts.items()}

    # Standardize expiry datetime to UTC ISO string
    if isinstance(expires_at, datetime):
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError(
                f"expires_at datetime must be timezone-aware UTC, got naive: {expires_at!r}"
            )
        expires_at_str = expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        expires_at_str = str(expires_at)

    return {
        "amounts": normalized_amounts,
        "decision": str(decision),
        "expires_at": expires_at_str,
        "items": normalized_items,
        "method": str(method),
        "order_id": str(order_id),
        "policy_version": str(policy_version),
        "type": str(type),
    }


def compute_proposal_hash(card: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of canonical JSON with sorted keys and no whitespace."""
    canonical_json = json.dumps(
        card,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
