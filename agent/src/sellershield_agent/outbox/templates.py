"""Fixed-English email template loading, escaping, and ID-only subject sanitization."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

ALLOWED_TEMPLATES: frozenset[str] = frozenset(
    {
        "approval.decided",
        "refund.updated",
        "case.created",
        "case.message",
        "delay.notice",
    }
)

TEMPLATE_ALIASES: dict[str, str] = {
    "order_delayed": "delay.notice",
    "case_reply": "case.message",
    "case_status_updated": "case.message",
}

_SAFE_ID_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}")
_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def _sanitize_subject_id(val: Any) -> str:
    if val is None:
        return ""
    first_line = re.split(r"[\r\n]", str(val), maxsplit=1)[0].strip(" \t")
    match = _SAFE_ID_PREFIX_RE.match(first_line)
    return match.group(0) if match else ""


def _escape_body_value(val: Any) -> str:
    if val is None:
        return ""
    return html.escape(str(val), quote=True)


def render_template(template_name: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Render subject and body for a template, escaping values and restricting subjects."""
    canonical = TEMPLATE_ALIASES.get(template_name, template_name)
    if not canonical or ".." in canonical or not _TEMPLATE_NAME_RE.fullmatch(canonical):
        raise ValueError(f"Invalid notification template name: {template_name!r}")

    if canonical.startswith("seller."):
        order_id = _sanitize_subject_id(payload.get("order_id"))
        approval_id = _sanitize_subject_id(payload.get("approval_id"))
        if canonical == "seller.approval_needed":
            subject = f"Approval requested for order {order_id}".strip()
            body = f"Approval {approval_id} requested for order {order_id}.".strip()
        else:
            subject = f"Seller notice for order {order_id}".strip()
            body = f"Notice for order {order_id}.".strip()
        return subject, body

    if canonical not in ALLOWED_TEMPLATES:
        raise ValueError(f"Unsupported notification template: {template_name!r}")

    templates_root = TEMPLATES_DIR.resolve()
    template_path = (templates_root / f"{canonical}.txt").resolve()
    if not template_path.is_relative_to(templates_root) or not template_path.is_file():
        raise ValueError(f"Template file missing: {canonical!r}")

    content = template_path.read_text(encoding="utf-8")
    lines = content.strip().splitlines()
    subject_raw = lines[0].replace("Subject:", "", 1).strip() if lines else "Notification"
    body_raw = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

    safe_subject_vars: dict[str, str] = {
        "order_id": _sanitize_subject_id(payload.get("order_id")),
        "case_id": _sanitize_subject_id(payload.get("case_id")),
        "refund_id": _sanitize_subject_id(payload.get("refund_id")),
        "approval_id": _sanitize_subject_id(payload.get("approval_id")),
    }
    subject = _PLACEHOLDER_RE.sub(
        lambda m: safe_subject_vars.get(m.group(1), ""),
        subject_raw,
    )
    subject = subject.replace("\r", " ").replace("\n", " ").strip()

    escaped_vars: dict[str, str] = {
        str(k): _escape_body_value(v) for k, v in payload.items()
    }
    if not escaped_vars.get("status"):
        escaped_vars["status"] = "updated"
    if not escaped_vars.get("order_id"):
        escaped_vars["order_id"] = ""
    if not escaped_vars.get("case_id"):
        escaped_vars["case_id"] = ""

    msg_fallback = payload.get("message")
    if msg_fallback is None or str(msg_fallback) == "":
        if payload.get("text") is not None and str(payload.get("text")) != "":
            msg_fallback = payload.get("text")
        elif canonical == "case.message" and payload.get("status"):
            msg_fallback = f"Case status updated to {payload['status']}."
        elif canonical == "delay.notice":
            eta_val = payload.get("new_eta") or payload.get("eta_at") or payload.get("eta")
            if eta_val:
                msg_fallback = f"Updated estimated delivery time: {eta_val}."
    escaped_vars["message"] = _escape_body_value(msg_fallback)

    body = _PLACEHOLDER_RE.sub(
        lambda m: escaped_vars.get(m.group(1), ""),
        body_raw,
    )
    return subject, body

