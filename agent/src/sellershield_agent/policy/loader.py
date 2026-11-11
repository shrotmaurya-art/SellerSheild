"""Policy document loader and validator.

Loads versioned policy documents from disk, validates against PolicyDocument
schemas (rejecting unknown fields and negative values), and falls back to
the active version configured by POLICY_ACTIVE_VERSION (default 'v3').
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sellershield_agent.policy.models import PolicyDocument

VERSIONS_DIR = Path(__file__).resolve().parent / "versions"
DEFAULT_ACTIVE_VERSION = "v3"


def load_policy_from_dict(data: dict[str, Any]) -> PolicyDocument:
    """Validate and instantiate a PolicyDocument from a dictionary.

    Raises :class:`pydantic.ValidationError` if any unknown field, negative value,
    or schema violation is encountered.
    """
    return PolicyDocument.model_validate(data)


def load_policy_from_json(json_str: str) -> PolicyDocument:
    """Validate and instantiate a PolicyDocument from raw JSON string."""
    return PolicyDocument.model_validate_json(json_str)


def load_policy(version: str | None = None) -> PolicyDocument:
    """Load and validate a policy configuration by version.

    If *version* is omitted, the version specified by the ``POLICY_ACTIVE_VERSION``
    environment variable is used, defaulting to ``"v3"``.
    """
    if version is None:
        version = os.environ.get("POLICY_ACTIVE_VERSION", DEFAULT_ACTIVE_VERSION)

    version_path = VERSIONS_DIR / f"{version}.json"
    if not version_path.is_file():
        raise FileNotFoundError(
            f"Policy configuration file not found for version {version!r} at {version_path}"
        )

    raw_text = version_path.read_text(encoding="utf-8")
    data = json.loads(raw_text)
    return load_policy_from_dict(data)


get_policy = load_policy
