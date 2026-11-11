"""Security verification of DEV gating and production mode isolation.

Verifies:
- With DEV_MODE=false, every /v1/dev route gives 404 even to authenticated owner
- STORE_MODE=fixtures unconditionally refuses to start outside DEV_MODE with a clear message
- FastAPI /docs, /redoc, and /openapi.json routes are completely disabled (404)
- No debug route exists in the application
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
import pytest
import respx
from starlette.testclient import TestClient

from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.config import get_settings
from sellershield_agent.db import reset_engine
from sellershield_agent.main import create_app
from tests.security.test_route_sweep import _mint_jwt

JWKS_URL = "http://test-store:8000/.well-known/jwks.json"


def test_dev_mode_false_every_dev_route_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With DEV_MODE=false, every /v1/dev and /dev route returns 404 even to seller_owner."""
    db_file = tmp_path / "dev_gating.db"
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("STORE_MODE", "http")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    owner_tok = _mint_jwt(priv, role="seller_owner", sub="usr_owner", store_id="nestkart")

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {owner_tok}"}

    dev_endpoints = [
        ("GET", "/v1/dev/faults", None),
        ("POST", "/v1/dev/faults", {"fault": "llm_down", "enabled": True}),
        ("POST", "/v1/dev/reset", {}),
        ("POST", "/v1/dev/eval/run", {}),
        ("GET", "/v1/dev/eval/runs", None),
        ("GET", "/v1/dev/eval/runs/run-123", None),
        ("GET", "/dev/faults", None),
        ("POST", "/dev/faults", {"fault": "llm_down", "enabled": True}),
        ("POST", "/dev/reset", {}),
        ("POST", "/dev/eval/run", {}),
        ("GET", "/dev/eval/runs", None),
        ("GET", "/dev/eval/runs/run-123", None),
    ]

    for method, path, body in dev_endpoints:
        res = client.request(method, path, headers=headers, json=body)
        assert res.status_code == 404, (
            f"DEV route {method} {path} returned {res.status_code} with DEV_MODE=false, expected 404"
        )
        assert res.json().get("error", {}).get("code") == "not_found"

    get_settings.cache_clear()
    reset_engine()


def test_store_mode_fixtures_refuses_to_start_outside_dev_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STORE_MODE=fixtures refuses to start with clear message when DEV_MODE=false (KP-24)."""
    db_file = tmp_path / "fixture_refusal.db"
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()

    with pytest.raises(RuntimeError) as exc_info:
        create_app()

    assert "STORE_MODE=fixtures is prohibited outside DEV_MODE" in str(exc_info.value)
    assert "KP-24" in str(exc_info.value)

    get_settings.cache_clear()
    reset_engine()


def test_fastapi_docs_and_openapi_off_outside_dev_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FastAPI /docs, /redoc, and /openapi.json are completely turned off when DEV_MODE=false."""
    db_file = tmp_path / "docs_off.db"
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("STORE_MODE", "http")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    for doc_path in ("/docs", "/redoc", "/openapi.json"):
        res = client.get(doc_path)
        assert res.status_code == 404, (
            f"Documentation endpoint {doc_path} returned {res.status_code} outside DEV_MODE, expected 404"
        )

    get_settings.cache_clear()
    reset_engine()


def test_no_debug_routes_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert no debug route exists anywhere in the application routes."""
    db_file = tmp_path / "no_debug.db"
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    route_paths = [r.path for r in app.routes if hasattr(r, "path")]
    for path in route_paths:
        assert "debug" not in path.lower(), f"Unexpected debug route found in app: {path}"

    for probe in ("/debug", "/v1/debug", "/api/debug", "/_debug"):
        res = client.get(probe)
        assert res.status_code == 404

    get_settings.cache_clear()
    reset_engine()

