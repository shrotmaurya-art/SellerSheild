"""
Tests for evidence upload endpoints (Stage 9).

Covers:
  - Valid jpg, png, webp upload → 201
  - .exe renamed to .jpg → rejected (validation_error)
  - 3 MB oversized file → rejected (validation_error)
  - 4th upload on same order → rejected (limit_exceeded)
  - Path traversal filename ../../x.jpg → random name on disk
  - Another customer's access → uniform 404
  - Content-Disposition: attachment and X-Content-Type-Options: nosniff on download
  - Idempotency replay and mismatch
  - Seller can upload and view evidence
  - Service token (executor) can view evidence
"""

import io
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from app.auth import _mint_access_token, _mint_service_token  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Evidence, IdempotencyKey, User  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

# ---------- helpers ----------


def _token(email: str) -> str:
    """Mint a valid access token for a seeded user (bypasses rate limiter)."""
    with Session(db.engine) as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None, f"Seeded user not found: {email}"
        cid = None
        if user.role == "customer" and user.customer:
            cid = user.customer.customer_id
        return _mint_access_token(user, cid)


def _service_token(client_id: str = "agent-executor") -> str:
    """Get a service token by minting directly."""
    scopes_map = {
        "agent-executor": ["orders:read_all", "returns:write", "refunds:write"],
        "agent-monitor": ["orders:read_all"],
    }
    return _mint_service_token(client_id, scopes_map.get(client_id, []))


def _make_jpeg(width: int = 10, height: int = 10) -> bytes:
    """Create a small valid JPEG in memory."""
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _make_png(width: int = 10, height: int = 10) -> bytes:
    """Create a small valid PNG in memory."""
    img = Image.new("RGBA", (width, height), color=(0, 255, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_webp(width: int = 10, height: int = 10) -> bytes:
    """Create a small valid WebP in memory."""
    img = Image.new("RGB", (width, height), color=(0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=85)
    return buf.getvalue()


def _upload(
    order_id: str,
    token: str,
    file_bytes: bytes,
    filename: str = "photo.jpg",
    idempotency_key: str | None = None,
):
    """Upload evidence and return the response."""
    key = idempotency_key or f"ev-{uuid.uuid4().hex[:8]}"
    return client.post(
        f"/api/v1/orders/{order_id}/evidence",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key,
        },
        files={"file": (filename, io.BytesIO(file_bytes), "application/octet-stream")},
    )


# Track created evidence for cleanup
_created_evidence_ids: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup_evidence():
    """Clean up any evidence created during tests."""
    _created_evidence_ids.clear()
    yield
    # Clean up evidence records and files from the database
    with Session(db.engine) as session:
        for ev_id in _created_evidence_ids:
            ev = session.get(Evidence, ev_id)
            if ev:
                # Delete the file from disk
                try:
                    Path(ev.file_path).unlink(missing_ok=True)
                except Exception:
                    pass
                session.delete(ev)
        # Also clean up idempotency keys we created
        for ik in session.query(IdempotencyKey).filter(IdempotencyKey.key.like("ev-%")).all():
            session.delete(ik)
        session.commit()


def _track(resp) -> None:
    """Track evidence IDs for cleanup."""
    if resp.status_code == 201:
        data = resp.json()
        if "evidence_id" in data:
            _created_evidence_ids.append(data["evidence_id"])


# ---------- SC-11: Damage photo upload ----------

# ORD-1061 is Aarav's (C001) damaged bottle claim scenario
ORDER_ID = "ORD-1061"
AARAV_EMAIL = "aarav.mehta@example.test"
DIYA_EMAIL = "diya.nair@example.test"
SELLER_EMAIL = "priya.sharma@nestkart.test"


class TestEvidenceUploadValid:
    """Valid uploads: jpg, png, webp → 201 with evidence_id."""

    def test_upload_jpeg(self):
        token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, token, _make_jpeg(), "damage_photo.jpg")
        _track(resp)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["evidence_id"].startswith("EV-")
        assert data["order_id"] == ORDER_ID
        assert data["mime_type"] == "image/jpeg"
        assert data["download_url"].startswith("/api/v1/evidence/")

    def test_upload_png(self):
        token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, token, _make_png(), "screenshot.png")
        _track(resp)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["mime_type"] == "image/png"

    def test_upload_webp(self):
        token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, token, _make_webp(), "photo.webp")
        _track(resp)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["mime_type"] == "image/webp"


class TestEvidenceUploadRejections:
    """Rejected uploads: wrong type, oversized, over limit, traversal."""

    def test_exe_renamed_to_jpg_rejected(self):
        """An .exe file renamed to .jpg is detected and rejected."""
        token = _token(AARAV_EMAIL)
        # Create fake binary content (not a valid image)
        fake_exe = b"MZ" + b"\x00" * 1024  # PE header magic bytes
        resp = _upload(ORDER_ID, token, fake_exe, "malware.jpg")
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"
        assert "not a valid image" in resp.json()["error"]["message"]

    def test_text_file_renamed_to_jpg_rejected(self):
        """Plain text renamed to .jpg is rejected."""
        token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, token, b"not an image at all", "fake.jpg")
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    def test_oversized_file_rejected(self):
        """A file exceeding 2 MB is rejected with validation_error."""
        token = _token(AARAV_EMAIL)
        # Create a valid JPEG that is ~2.7 MB (> 2 MB limit)
        large_img = Image.new("RGB", (1800, 1800))
        pixels = [(x % 256, (x * 3) % 256, (x * 7) % 256) for x in range(1800 * 1800)]
        large_img.putdata(pixels)
        buf = io.BytesIO()
        large_img.save(buf, format="JPEG", quality=95)
        large_bytes = buf.getvalue()

        assert len(large_bytes) > 2 * 1024 * 1024, f"File too small: {len(large_bytes)}"
        assert len(large_bytes) < 5 * 1024 * 1024, f"File too large: {len(large_bytes)}"

        resp = _upload(ORDER_ID, token, large_bytes, "huge.jpg")
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "validation_error"
        assert "size" in resp.json()["error"]["message"].lower()

    def test_empty_file_rejected(self):
        """An empty file is rejected."""
        token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, token, b"", "empty.jpg")
        assert resp.status_code == 422, resp.text

    def test_fourth_upload_rejected(self):
        """The 4th upload on the same order is rejected (max 3)."""
        token = _token(AARAV_EMAIL)
        for i in range(3):
            resp = _upload(ORDER_ID, token, _make_jpeg(), f"photo{i}.jpg")
            _track(resp)
            assert resp.status_code == 201, f"Upload {i+1} failed: {resp.text}"

        # The 4th should fail
        resp = _upload(ORDER_ID, token, _make_jpeg(), "photo3.jpg")
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "limit_exceeded"

    def test_path_traversal_filename_sanitized(self):
        """A filename like ../../x.jpg results in a random name on disk."""
        token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, token, _make_jpeg(), "../../x.jpg")
        _track(resp)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        # The stored file_name should NOT contain path traversal
        assert ".." not in data["file_name"]
        assert "/" not in data["file_name"]
        assert "\\" not in data["file_name"]
        # And should not be the original filename
        assert data["file_name"] != "../../x.jpg"
        assert data["file_name"] != "x.jpg"


class TestEvidenceOwnership:
    """Ownership checks: uniform 404 for non-owners."""

    def test_other_customer_gets_404_on_upload(self):
        """Diya cannot upload evidence to Aarav's order."""
        diya_token = _token(DIYA_EMAIL)
        resp = _upload(ORDER_ID, diya_token, _make_jpeg(), "photo.jpg")
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    def test_other_customer_gets_404_on_list(self):
        """Diya cannot list evidence on Aarav's order."""
        diya_token = _token(DIYA_EMAIL)
        resp = client.get(
            f"/api/v1/orders/{ORDER_ID}/evidence",
            headers={"Authorization": f"Bearer {diya_token}"},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    def test_other_customer_gets_404_on_download(self):
        """Diya cannot download evidence from Aarav's order."""
        # First, Aarav uploads
        aarav_token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, aarav_token, _make_jpeg(), "photo.jpg")
        _track(resp)
        assert resp.status_code == 201
        ev_id = resp.json()["evidence_id"]

        # Diya tries to download
        diya_token = _token(DIYA_EMAIL)
        resp2 = client.get(
            f"/api/v1/evidence/{ev_id}",
            headers={"Authorization": f"Bearer {diya_token}"},
        )
        assert resp2.status_code == 404, resp2.text
        assert resp2.json()["error"]["code"] == "not_found"

    def test_nonexistent_order_upload_404(self):
        """Upload to a nonexistent order returns 404."""
        token = _token(AARAV_EMAIL)
        resp = _upload("ORD-9999", token, _make_jpeg(), "photo.jpg")
        assert resp.status_code == 404, resp.text

    def test_nonexistent_evidence_download_404(self):
        """Download a nonexistent evidence ID returns 404."""
        token = _token(AARAV_EMAIL)
        resp = client.get(
            "/api/v1/evidence/EV-NONEXIST",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, resp.text


class TestEvidenceDownloadHeaders:
    """Download response has correct security headers."""

    def test_download_headers(self):
        """Evidence download includes Content-Disposition: attachment and nosniff."""
        token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, token, _make_jpeg(), "photo.jpg")
        _track(resp)
        assert resp.status_code == 201
        ev_id = resp.json()["evidence_id"]

        resp2 = client.get(
            f"/api/v1/evidence/{ev_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        assert "attachment" in resp2.headers.get("content-disposition", "").lower()
        assert resp2.headers.get("x-content-type-options") == "nosniff"


class TestEvidenceList:
    """Listing evidence for an order."""

    def test_list_empty(self):
        """An order with no evidence returns empty list."""
        token = _token(AARAV_EMAIL)
        resp = client.get(
            f"/api/v1/orders/{ORDER_ID}/evidence",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"] == ORDER_ID
        assert data["count"] == 0
        assert data["items"] == []

    def test_list_after_uploads(self):
        """After uploading 2 files, listing shows 2."""
        token = _token(AARAV_EMAIL)
        for i in range(2):
            resp = _upload(ORDER_ID, token, _make_jpeg(), f"photo{i}.jpg")
            _track(resp)
            assert resp.status_code == 201

        resp2 = client.get(
            f"/api/v1/orders/{ORDER_ID}/evidence",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["count"] == 2
        assert len(data["items"]) == 2


class TestEvidenceRandomFilenames:
    """File names on disk are random, never the user's original."""

    def test_random_filename_on_disk(self):
        """Uploaded file is stored with a random UUID name."""
        token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, token, _make_jpeg(), "my_secret_photo.jpg")
        _track(resp)
        assert resp.status_code == 201
        data = resp.json()

        # file_name should be a random hex string, not the original
        assert data["file_name"] != "my_secret_photo.jpg"
        assert data["file_name"].endswith(".jpg")
        # Should be hex (UUID) + .jpg
        stem = data["file_name"].rsplit(".", 1)[0]
        assert len(stem) == 32  # uuid4().hex is 32 chars


class TestEvidenceSellerAccess:
    """Sellers can upload and view evidence on any order."""

    def test_seller_can_upload(self):
        """A seller can upload evidence to any order."""
        seller_token = _token(SELLER_EMAIL)
        resp = _upload(ORDER_ID, seller_token, _make_jpeg(), "seller_photo.jpg")
        _track(resp)
        assert resp.status_code == 201

    def test_seller_can_list(self):
        """A seller can list evidence on any order."""
        seller_token = _token(SELLER_EMAIL)
        resp = client.get(
            f"/api/v1/orders/{ORDER_ID}/evidence",
            headers={"Authorization": f"Bearer {seller_token}"},
        )
        assert resp.status_code == 200

    def test_seller_can_download(self):
        """A seller can download evidence from any order."""
        aarav_token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, aarav_token, _make_jpeg(), "photo.jpg")
        _track(resp)
        assert resp.status_code == 201
        ev_id = resp.json()["evidence_id"]

        seller_token = _token(SELLER_EMAIL)
        resp2 = client.get(
            f"/api/v1/evidence/{ev_id}",
            headers={"Authorization": f"Bearer {seller_token}"},
        )
        assert resp2.status_code == 200


class TestEvidenceIdempotency:
    """Idempotency-Key honoured on uploads."""

    def test_replay_same_key_same_file(self):
        """Same Idempotency-Key + same file = replay (same evidence_id)."""
        token = _token(AARAV_EMAIL)
        jpeg_bytes = _make_jpeg()
        key = "ev-idem-same-001"

        resp1 = _upload(ORDER_ID, token, jpeg_bytes, "photo.jpg", idempotency_key=key)
        _track(resp1)
        assert resp1.status_code == 201

        resp2 = _upload(ORDER_ID, token, jpeg_bytes, "photo.jpg", idempotency_key=key)
        assert resp2.status_code == 201
        assert resp2.json()["evidence_id"] == resp1.json()["evidence_id"]

    def test_mismatch_same_key_different_file(self):
        """Same Idempotency-Key + different file = 409 mismatch."""
        token = _token(AARAV_EMAIL)
        key = "ev-idem-diff-001"

        resp1 = _upload(ORDER_ID, token, _make_jpeg(), "photo1.jpg", idempotency_key=key)
        _track(resp1)
        assert resp1.status_code == 201

        resp2 = _upload(ORDER_ID, token, _make_png(), "photo2.png", idempotency_key=key)
        assert resp2.status_code == 409
        assert resp2.json()["error"]["code"] == "idempotency_mismatch"

    def test_missing_idempotency_key_rejected(self):
        """POST without Idempotency-Key → 422."""
        token = _token(AARAV_EMAIL)
        resp = client.post(
            f"/api/v1/orders/{ORDER_ID}/evidence",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("photo.jpg", io.BytesIO(_make_jpeg()), "image/jpeg")},
        )
        assert resp.status_code == 422


class TestEvidenceExifStripping:
    """Re-encoding strips EXIF metadata."""

    def test_exif_stripped_from_jpeg(self):
        """EXIF data in a JPEG is stripped after re-encoding."""
        # Create a JPEG with EXIF data
        img = Image.new("RGB", (10, 10), color=(255, 0, 0))
        buf = io.BytesIO()
        # Add fake EXIF data
        # Minimal EXIF: Exif header + IFD with GPS tag
        exif_data = b"Exif\x00\x00MM\x00\x2a\x00\x00\x00\x08"  # TIFF header
        exif_data += b"\x00\x01"  # 1 IFD entry
        exif_data += b"\x01\x0e"  # ImageDescription tag
        exif_data += b"\x00\x02"  # ASCII type
        exif_data += b"\x00\x00\x00\x0c"  # 12 chars
        exif_data += b"Secret data\x00"  # Value
        exif_data += b"\x00\x00\x00\x00"  # Next IFD offset (none)

        img.save(buf, format="JPEG", quality=85, exif=exif_data)
        jpeg_with_exif = buf.getvalue()

        token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, token, jpeg_with_exif, "exif_photo.jpg")
        _track(resp)
        assert resp.status_code == 201

        # Download and check that EXIF is stripped
        ev_id = resp.json()["evidence_id"]
        resp2 = client.get(
            f"/api/v1/evidence/{ev_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp2.status_code == 200

        # Parse the downloaded image and check for EXIF
        downloaded = Image.open(io.BytesIO(resp2.content))
        exif = downloaded.getexif()
        # Should have no EXIF entries (or very minimal)
        assert len(exif) == 0, f"EXIF not stripped: {dict(exif)}"


class TestEvidenceServiceToken:
    """Service tokens (executor/monitor) can view evidence."""

    def test_executor_can_list(self):
        """Agent-executor can list evidence on any order."""
        svc_token = _service_token("agent-executor")
        resp = client.get(
            f"/api/v1/orders/{ORDER_ID}/evidence",
            headers={"Authorization": f"Bearer {svc_token}"},
        )
        assert resp.status_code == 200

    def test_executor_can_download(self):
        """Agent-executor can download evidence."""
        aarav_token = _token(AARAV_EMAIL)
        resp = _upload(ORDER_ID, aarav_token, _make_jpeg(), "photo.jpg")
        _track(resp)
        assert resp.status_code == 201
        ev_id = resp.json()["evidence_id"]

        svc_token = _service_token("agent-executor")
        resp2 = client.get(
            f"/api/v1/evidence/{ev_id}",
            headers={"Authorization": f"Bearer {svc_token}"},
        )
        assert resp2.status_code == 200
