import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure scripts dir is on sys.path to import detect_breaking_changes
BASE_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from export_openapi import detect_breaking_changes  # noqa: E402

from app.main import app  # noqa: E402

CONTRACT_PATH = BASE_DIR / "contracts" / "openapi-store.json"


@pytest.fixture(scope="module")
def stored_contract() -> dict[str, Any]:
    assert CONTRACT_PATH.exists(), f"Contract file missing at {CONTRACT_PATH}"
    with open(CONTRACT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def current_schema() -> dict[str, Any]:
    return app.openapi()


def test_contract_file_exists():
    """Verify that contracts/openapi-store.json exists."""
    assert CONTRACT_PATH.is_file()


def test_all_contract_endpoints_exist_in_application(stored_contract, current_schema):
    """Verify that every endpoint and method in openapi-store.json is in FastAPI."""
    contract_paths = stored_contract.get("paths", {})
    current_paths = current_schema.get("paths", {})

    for path, methods in contract_paths.items():
        assert path in current_paths, f"Path {path} from contract is missing in application routes"
        for method in methods:
            if method.lower() in ("get", "post", "put", "delete", "patch", "options", "head"):
                assert method.lower() in current_paths[path], (
                    f"Method {method.upper()} on {path} is missing"
                )


def test_no_breaking_changes_against_stored_contract(stored_contract, current_schema):
    """Verify that current FastAPI app has zero breaking changes against contract."""
    breaking = detect_breaking_changes(stored_contract, current_schema)
    assert not breaking, f"Breaking changes detected in OpenAPI contract: {breaking}"


def test_breaking_change_detection_logic():
    """Verify that detect_breaking_changes catches all breaking change scenarios."""
    base_spec = {
        "paths": {
            "/api/v1/orders": {
                "get": {
                    "parameters": [{"name": "limit", "in": "query", "required": False}],
                    "responses": {"200": {"description": "OK"}},
                },
                "post": {
                    "requestBody": {"required": False},
                    "responses": {"201": {"description": "Created"}},
                },
            }
        }
    }

    # Case 1: Route removed
    removed_route_spec = {"paths": {}}
    breaking_1 = detect_breaking_changes(base_spec, removed_route_spec)
    assert any("Removed endpoint path" in b for b in breaking_1)

    # Case 2: Method removed
    removed_method_spec = {
        "paths": {"/api/v1/orders": {"get": base_spec["paths"]["/api/v1/orders"]["get"]}}
    }
    breaking_2 = detect_breaking_changes(base_spec, removed_method_spec)
    assert any("Removed method: POST" in b for b in breaking_2)

    # Case 3: Required parameter added
    added_param_spec = {
        "paths": {
            "/api/v1/orders": {
                "get": {
                    "parameters": [
                        {"name": "limit", "in": "query", "required": False},
                        {"name": "mandatory_filter", "in": "query", "required": True},
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
                "post": base_spec["paths"]["/api/v1/orders"]["post"],
            }
        }
    }
    breaking_3 = detect_breaking_changes(base_spec, added_param_spec)
    assert any("Added required parameter" in b for b in breaking_3)

    # Case 4: 2xx response code removed
    removed_status_spec = {
        "paths": {
            "/api/v1/orders": {
                "get": {
                    "parameters": [{"name": "limit", "in": "query", "required": False}],
                    "responses": {"500": {"description": "Error"}},
                },
                "post": base_spec["paths"]["/api/v1/orders"]["post"],
            }
        }
    }
    breaking_4 = detect_breaking_changes(base_spec, removed_status_spec)
    assert any("Removed response status code 200" in b for b in breaking_4)

    # Case 5: Request body became required
    req_body_spec = {
        "paths": {
            "/api/v1/orders": {
                "get": base_spec["paths"]["/api/v1/orders"]["get"],
                "post": {
                    "requestBody": {"required": True},
                    "responses": {"201": {"description": "Created"}},
                },
            }
        }
    }
    breaking_5 = detect_breaking_changes(base_spec, req_body_spec)
    assert any("Request body became required" in b for b in breaking_5)
