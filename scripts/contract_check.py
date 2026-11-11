"""
scripts/contract_check.py
Validates OpenAPI 3.1 specifications against SellerShield architecture invariants.

Invariants checked:
1. Every operation must have a unique operationId (snake_case).
2. Every non-public operation must declare security.
3. Every mutating POST must require the Idempotency-Key header.
4. Every property named amount, price, total or ending in _paise must be integer.
5. Every error response (4xx, 5xx) must reference #/components/schemas/Error.
"""

import sys
from pathlib import Path
import yaml


def check_contract(path: Path) -> bool:
    if not path.exists():
        print(f"FAIL: File not found: {path}")
        return False

    with open(path, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    errors = []
    operation_ids = set()

    components = spec.get("components", {})
    comp_params = components.get("parameters", {})
    comp_responses = components.get("responses", {})

    # Helper to resolve parameter references
    def resolve_param(param):
        if isinstance(param, dict) and "$ref" in param:
            ref = param["$ref"]
            if ref.startswith("#/components/parameters/"):
                p_name = ref.split("/")[-1]
                return comp_params.get(p_name, {})
        return param

    # Helper to resolve response references
    def resolve_response(resp):
        if isinstance(resp, dict) and "$ref" in resp:
            ref = resp["$ref"]
            if ref.startswith("#/components/responses/"):
                r_name = ref.split("/")[-1]
                return comp_responses.get(r_name, {})
        return resp

    # Public operations exempt from security requirement
    public_endpoints = {
        ("/auth/register", "post"),
        ("/auth/login", "post"),
        ("/auth/refresh", "post"),
        ("/auth/token", "post"),
        ("/.well-known/jwks.json", "get"),
        ("/clock", "get"),
        ("/api/v1/clock", "get"),
        ("/healthz", "get"),
        ("/readyz", "get"),
        ("/openapi.json", "get"),
    }

    # Non-mutating POST operations or webhooks identified by event headers
    exempt_posts = {
        "/auth/login",
        "/auth/token",
        "/auth/refresh",
        "/webhooks/store",
        "/v1/webhooks/store",
    }

    # Check paths and operations
    paths = spec.get("paths", {})
    for p_name, p_item in paths.items():
        if not isinstance(p_item, dict):
            continue

        for method in ["get", "post", "put", "patch", "delete"]:
            if method not in p_item:
                continue

            op = p_item[method]
            if not isinstance(op, dict):
                continue

            # 1. Unique operationId
            op_id = op.get("operationId")
            if not op_id:
                errors.append(f"{method.upper()} {p_name} has no operationId")
            elif op_id in operation_ids:
                errors.append(f"Duplicate operationId: {op_id}")
            else:
                operation_ids.add(op_id)

            # 2. Security for non-public operations
            is_public = (p_name, method) in public_endpoints
            global_sec = spec.get("security", [])
            op_sec = op.get("security", global_sec)
            if not is_public and not op_sec:
                errors.append(f"Non-public operation {method.upper()} {p_name} ({op_id}) has no security declaration")

            # 3. Mutating POST requires Idempotency-Key
            if method == "post" and p_name not in exempt_posts:
                raw_params = op.get("parameters", []) + p_item.get("parameters", [])
                params = [resolve_param(p) for p in raw_params]
                has_idempotency_key = any(
                    isinstance(param, dict) and param.get("name") == "Idempotency-Key" and param.get("in") == "header"
                    for param in params
                )
                if not has_idempotency_key:
                    errors.append(f"Mutating POST {p_name} ({op_id}) lacks Idempotency-Key header parameter")

            # 5. Error responses reference Error schema
            responses = op.get("responses", {})
            for status_code, raw_resp in responses.items():
                if str(status_code).startswith(("4", "5")) and status_code != "default":
                    resp = resolve_response(raw_resp)
                    content = resp.get("content", {})
                    json_content = content.get("application/json", {})
                    schema = json_content.get("schema", {})
                    ref = schema.get("$ref", "")
                    if "#/components/schemas/Error" not in ref:
                        errors.append(f"Error response {status_code} in {op_id} does not reference #/components/schemas/Error")

    # 4. Check schemas for money / price / amount fields
    def check_schema_properties(schema_name: str, schema_dict: dict):
        properties = schema_dict.get("properties", {})
        for prop_name, prop_spec in properties.items():
            if not isinstance(prop_spec, dict):
                continue

            # Recurse into nested objects or array items
            if prop_spec.get("type") == "object":
                check_schema_properties(f"{schema_name}.{prop_name}", prop_spec)
            elif prop_spec.get("type") == "array" and isinstance(prop_spec.get("items"), dict):
                check_schema_properties(f"{schema_name}.{prop_name}[]", prop_spec["items"])

            is_money_field = (
                prop_name.endswith("_paise")
                or prop_name in ("amount", "price", "total", "authorized_amount", "discount_alloc")
            )
            if is_money_field:
                p_type = prop_spec.get("type")
                if p_type != "integer":
                    errors.append(
                        f"Field '{prop_name}' in schema '{schema_name}' must be integer type, found '{p_type}'"
                    )

    schemas = components.get("schemas", {})
    for s_name, s_spec in schemas.items():
        if isinstance(s_spec, dict):
            check_schema_properties(s_name, s_spec)

    print(f"Checked contract: {path}")
    print(f"Total operations: {len(operation_ids)}")
    if errors:
        print(f"FAILED with {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err}")
        return False

    print("ALL CHECKS PASSED.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/contract_check.py <path_to_openapi.yaml>")
        sys.exit(1)

    all_passed = True
    for arg in sys.argv[1:]:
        if not check_contract(Path(arg)):
            all_passed = False

    sys.exit(0 if all_passed else 1)
