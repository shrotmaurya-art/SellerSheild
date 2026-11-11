"""
Evidence upload endpoints.

POST /api/v1/orders/{order_id}/evidence  — upload a damage photo (multipart)
GET  /api/v1/orders/{order_id}/evidence  — list evidence for an order
GET  /api/v1/evidence/{evidence_id}      — download a single evidence file

Security:
  - Only the owning customer or a seller can upload/view.
  - Non-owners get a uniform 404 (same as missing).
  - Real file type validated via Pillow (not extension or Content-Type).
  - Re-encoded with Pillow to strip EXIF and hidden payloads.
  - Random file names; originals never stored on disk.
  - Content-Disposition: attachment and X-Content-Type-Options: nosniff on download.
  - Idempotency-Key honoured on upload.
"""

import hashlib
import io
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from sqlalchemy.orm import Session

import db
from app.audit import log_audit
from app.clock import Clock
from app.config import settings
from app.deps import require_auth
from app.errors import (
    LimitExceededException,
    NotFoundException,
    ValidationErrorException,
)
from app.idempotency import (
    check_idempotency_replay,
    require_idempotency_key,
    save_idempotency_record,
)
from app.models import Evidence, Order
from app.schemas import EvidenceListResponse, EvidenceResponse

router = APIRouter(tags=["Evidence"])

# Allowed MIME types after Pillow detection
_PILLOW_FORMAT_TO_MIME: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_ALLOWED_FORMATS = set(_PILLOW_FORMAT_TO_MIME.keys())

# File extension for the re-encoded output
_PILLOW_FORMAT_TO_EXT: dict[str, str] = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}

MAX_EVIDENCE_PER_ORDER = 3


def _get_upload_dir() -> Path:
    """Resolve the upload directory relative to store-api/, create if needed."""
    upload_dir = Path(settings.UPLOAD_DIR)
    if not upload_dir.is_absolute():
        upload_dir = Path(__file__).resolve().parent.parent / upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def _is_owner_or_seller(
    principal: dict[str, Any],
    order: Order,
) -> bool:
    """Check if the principal owns the order or is a seller."""
    ptype = principal.get("type")
    role = principal.get("role", "")

    if ptype == "user" and role in ("seller_owner", "seller_staff"):
        return True
    if ptype == "user" and role == "customer":
        return principal.get("customer_id") == order.customer_id
    # Service tokens (executor/monitor) can also access evidence
    if ptype == "service":
        return True
    return False


def _make_evidence_response(ev: Evidence) -> dict[str, Any]:
    """Build a dict matching EvidenceResponse fields."""
    return {
        "evidence_id": ev.evidence_id,
        "order_id": ev.order_id,
        "customer_id": ev.customer_id,
        "file_name": ev.file_name,
        "file_size_bytes": ev.file_size_bytes,
        "mime_type": ev.mime_type,
        "created_at": ev.created_at.isoformat(),
        "download_url": f"/api/v1/evidence/{ev.evidence_id}",
    }


@router.post(
    "/api/v1/orders/{order_id}/evidence",
    summary="Upload evidence photo for an order",
    status_code=201,
)
async def upload_evidence(
    order_id: str,
    request: Request,
    file: UploadFile,
    principal: dict[str, Any] = Depends(require_auth),
    idempotency_key: str = Depends(require_idempotency_key),
) -> JSONResponse:
    """Upload a damage photo (jpg, png, webp; max 2 MB; max 3 per order)."""
    request_id = getattr(request.state, "request_id", "unknown")
    actor = principal.get("sub") or principal.get("client_id") or "unknown"

    # Read the file bytes first (needed for idempotency hash)
    raw_bytes = await file.read()

    # Compute idempotency hash from the file content + order_id
    hash_input = order_id.encode("utf-8") + raw_bytes
    payload_hash = hashlib.sha256(hash_input).hexdigest()

    with Session(db.engine) as session:
        # Check idempotency replay
        replay = check_idempotency_replay(session, idempotency_key, payload_hash)
        if replay is not None:
            status_code, response_data = replay
            return JSONResponse(status_code=status_code, content=response_data)

        # Look up the order
        order = session.get(Order, order_id)
        if order is None or not _is_owner_or_seller(principal, order):
            # Uniform 404 — log denial
            log_audit(
                session=session,
                request_id=request_id,
                actor=actor,
                action="upload_evidence",
                resource=f"orders/{order_id}",
                outcome="denied",
            )
            raise NotFoundException(message="Order not found")

        # Check evidence count limit
        existing_count = session.query(Evidence).filter(Evidence.order_id == order_id).count()
        if existing_count >= MAX_EVIDENCE_PER_ORDER:
            raise LimitExceededException(
                message=f"Maximum {MAX_EVIDENCE_PER_ORDER} evidence files per order"
            )

        # Validate file size (2 MB)
        if len(raw_bytes) > settings.MAX_UPLOAD_BYTES:
            max_mb = settings.MAX_UPLOAD_BYTES / (1024 * 1024)
            raise ValidationErrorException(message=f"File size exceeds {max_mb:.0f} MB limit")

        if len(raw_bytes) == 0:
            raise ValidationErrorException(message="Empty file")

        # Validate REAL file type using Pillow (not extension or Content-Type)
        try:
            img = Image.open(io.BytesIO(raw_bytes))
            img.verify()  # Verify it's a valid image
            # Re-open after verify (verify exhausts the stream)
            img = Image.open(io.BytesIO(raw_bytes))
            detected_format = img.format
        except Exception:
            raise ValidationErrorException(
                message="File is not a valid image (jpg, png, or webp required)"
            )

        if detected_format not in _ALLOWED_FORMATS:
            raise ValidationErrorException(
                message=(
                    f"Unsupported image format: {detected_format}. "
                    "Only jpg, png, and webp are allowed"
                )
            )

        # Re-encode with Pillow to strip EXIF and hidden payloads
        output_buf = io.BytesIO()
        # Convert to RGB if needed (e.g., RGBA PNGs stay RGBA, CMYK -> RGB)
        if detected_format == "JPEG" and img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif detected_format == "PNG" and img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            img = img.convert("RGBA")

        save_kwargs: dict[str, Any] = {"format": detected_format}
        if detected_format == "JPEG":
            save_kwargs["quality"] = 90
            save_kwargs["exif"] = b""  # Strip EXIF explicitly
        elif detected_format == "PNG":
            # PNG re-encode strips text chunks and metadata
            pass
        elif detected_format == "WEBP":
            save_kwargs["quality"] = 90

        img.save(output_buf, **save_kwargs)
        clean_bytes = output_buf.getvalue()

        # Generate random file name (never the user's original)
        random_name = uuid.uuid4().hex
        ext = _PILLOW_FORMAT_TO_EXT[detected_format]
        disk_filename = f"{random_name}{ext}"
        mime_type = _PILLOW_FORMAT_TO_MIME[detected_format]

        # Compute SHA-256 of the clean file
        file_hash = hashlib.sha256(clean_bytes).hexdigest()

        # Write to disk under UPLOAD_DIR
        upload_dir = _get_upload_dir()
        file_path = upload_dir / disk_filename
        file_path.write_bytes(clean_bytes)

        # Create evidence record
        evidence_id = f"EV-{uuid.uuid4().hex[:8].upper()}"
        evidence = Evidence(
            evidence_id=evidence_id,
            order_id=order_id,
            customer_id=order.customer_id,
            file_path=str(file_path),
            file_name=disk_filename,
            file_size_bytes=len(clean_bytes),
            mime_type=mime_type,
            sha256_hash=file_hash,
            created_at=Clock.now(),
        )
        session.add(evidence)

        response_data = _make_evidence_response(evidence)

        # Save idempotency record
        save_idempotency_record(
            session=session,
            key=idempotency_key,
            payload_hash=payload_hash,
            status_code=201,
            response_data=response_data,
            client_id=actor,
        )

        # Log success
        log_audit(
            session=session,
            request_id=request_id,
            actor=actor,
            action="upload_evidence",
            resource=f"evidence/{evidence_id}",
            outcome="success",
        )

        session.commit()

    return JSONResponse(status_code=201, content=response_data)


@router.get(
    "/api/v1/orders/{order_id}/evidence",
    summary="List evidence for an order",
    response_model=EvidenceListResponse,
)
def list_evidence(
    order_id: str,
    request: Request,
    principal: dict[str, Any] = Depends(require_auth),
) -> EvidenceListResponse:
    """List all evidence files attached to an order."""
    request_id = getattr(request.state, "request_id", "unknown")
    actor = principal.get("sub") or principal.get("client_id") or "unknown"

    with Session(db.engine) as session:
        order = session.get(Order, order_id)
        if order is None or not _is_owner_or_seller(principal, order):
            log_audit(
                session=session,
                request_id=request_id,
                actor=actor,
                action="list_evidence",
                resource=f"orders/{order_id}",
                outcome="denied",
            )
            raise NotFoundException(message="Order not found")

        evidence_list = (
            session.query(Evidence)
            .filter(Evidence.order_id == order_id)
            .order_by(Evidence.created_at)
            .all()
        )

        items = []
        for ev in evidence_list:
            items.append(
                EvidenceResponse(
                    evidence_id=ev.evidence_id,
                    order_id=ev.order_id,
                    customer_id=ev.customer_id,
                    file_name=ev.file_name,
                    file_size_bytes=ev.file_size_bytes,
                    mime_type=ev.mime_type,
                    created_at=ev.created_at,
                    download_url=f"/api/v1/evidence/{ev.evidence_id}",
                )
            )

        return EvidenceListResponse(
            order_id=order_id,
            items=items,
            count=len(items),
        )


@router.get(
    "/api/v1/evidence/{evidence_id}",
    summary="Download an evidence file",
)
def download_evidence(
    evidence_id: str,
    request: Request,
    principal: dict[str, Any] = Depends(require_auth),
) -> FileResponse:
    """Download a single evidence file by ID."""
    request_id = getattr(request.state, "request_id", "unknown")
    actor = principal.get("sub") or principal.get("client_id") or "unknown"

    with Session(db.engine) as session:
        evidence = session.get(Evidence, evidence_id)
        if evidence is None:
            raise NotFoundException(message="Evidence not found")

        # Ownership check: load the order for the evidence
        order = session.get(Order, evidence.order_id)
        if order is None or not _is_owner_or_seller(principal, order):
            log_audit(
                session=session,
                request_id=request_id,
                actor=actor,
                action="download_evidence",
                resource=f"evidence/{evidence_id}",
                outcome="denied",
            )
            raise NotFoundException(message="Evidence not found")

        file_path = Path(evidence.file_path)
        if not file_path.exists():
            raise NotFoundException(message="Evidence not found")

        # Serve with security headers
        return FileResponse(
            path=str(file_path),
            media_type=evidence.mime_type,
            filename=evidence.file_name,
            headers={
                "Content-Disposition": f'attachment; filename="{evidence.file_name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )
