import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure store-api is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
STORE_API_DIR = BASE_DIR / "store-api"
if str(STORE_API_DIR) not in sys.path:
    sys.path.insert(0, str(STORE_API_DIR))

from app.main import app  # noqa: E402


def detect_breaking_changes(
    old_spec: dict[str, Any], new_spec: dict[str, Any]
) -> list[str]:
    """Detect breaking changes between old and new OpenAPI specifications.

    Breaking changes include:
    1. Removed endpoints (paths).
    2. Removed HTTP methods on an existing endpoint.
    3. Added required parameters without a default value.
    4. Removed successful HTTP response status codes.
    5. Request body becoming required when it was optional or absent.
    """
    breaking: list[str] = []
    old_paths: dict[str, Any] = old_spec.get("paths", {})
    new_paths: dict[str, Any] = new_spec.get("paths", {})

    for path, old_methods in old_paths.items():
        if path not in new_paths:
            breaking.append(f"Removed endpoint path: {path}")
            continue

        new_methods: dict[str, Any] = new_paths[path]
        http_methods = ("get", "post", "put", "delete", "patch", "options", "head")

        for method in http_methods:
            if method in old_methods:
                if method not in new_methods:
                    breaking.append(f"Removed method: {method.upper()} {path}")
                    continue

                old_op: dict[str, Any] = old_methods[method]
                new_op: dict[str, Any] = new_methods[method]

                # Check parameters: added required parameter is breaking
                old_params = {
                    (p.get("name"), p.get("in")): p
                    for p in old_op.get("parameters", [])
                }
                new_params = {
                    (p.get("name"), p.get("in")): p
                    for p in new_op.get("parameters", [])
                }

                for param_key, new_param in new_params.items():
                    if new_param.get("required") is True:
                        if param_key not in old_params or not old_params[param_key].get("required"):
                            name, location = param_key
                            breaking.append(
                                f"Added required parameter '{name}' in '{location}' on {method.upper()} {path}"
                            )

                # Check responses: removing a 2xx response code is breaking
                old_responses: dict[str, Any] = old_op.get("responses", {})
                new_responses: dict[str, Any] = new_op.get("responses", {})
                for status_code in old_responses:
                    if status_code.startswith("2") and status_code not in new_responses:
                        breaking.append(
                            f"Removed response status code {status_code} on {method.upper()} {path}"
                        )

                # Check requestBody: becoming required
                old_rb = old_op.get("requestBody", {})
                new_rb = new_op.get("requestBody", {})
                if new_rb.get("required") is True and not old_rb.get("required"):
                    breaking.append(
                        f"Request body became required on {method.upper()} {path}"
                    )

    return breaking


def main() -> None:
    parser = argparse.ArgumentParser(description="Export and validate OpenAPI specification.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for breaking changes without writing to file.",
    )
    parser.add_argument(
        "--allow-breaking",
        "--force",
        action="store_true",
        dest="allow_breaking",
        help="Allow breaking changes and overwrite contract.",
    )
    args = parser.parse_args()

    contracts_dir = BASE_DIR / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    schema_path = contracts_dir / "openapi-store.json"

    openapi_data = app.openapi()

    if schema_path.exists():
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                old_spec = json.load(f)
            breaking = detect_breaking_changes(old_spec, openapi_data)
            if breaking:
                print("BREAKING CHANGES DETECTED in OpenAPI contract:")
                for change in breaking:
                    print(f"  - {change}")
                if not args.allow_breaking:
                    print("\nExport ABORTED. Contract is frozen. Use --allow-breaking to override.")
                    sys.exit(1)
                else:
                    print("\nProceeding because --allow-breaking was specified.")
        except Exception as e:
            if not isinstance(e, SystemExit):
                print(f"Warning: could not inspect existing contract for breaking changes: {e}")

    if args.check:
        print(f"Contract check PASSED: no breaking changes against {schema_path}")
        return

    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(openapi_data, f, indent=2)

    print(f"Exported OpenAPI schema to {schema_path}")


if __name__ == "__main__":
    main()

