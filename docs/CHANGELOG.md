# CHANGELOG (what was built; newest first)

Entry format:
## YYYY-MM-DD T0.x Title
Files: created/changed list
What changed:
Verification run (commands and result summary):
Follow-ups:

## 2026-11-11 T0.10 LLM adapter and provider chain
Files: agent/src/sellershield_agent/llm/__init__.py, agent/src/sellershield_agent/llm/config.py, agent/src/sellershield_agent/llm/client.py, agent/src/sellershield_agent/llm/cache.py, agent/src/sellershield_agent/llm/chain.py, agent/src/sellershield_agent/llm/structured.py, agent/scripts/llm_smoke.py, agent/tests/test_llm.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/PITFALLS.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `llm/config.py`: `ProviderConfig` and `ProviderCapabilities` (`supports_json_schema`, `supports_tools`, `parallel_tools`, all defaulting to `False` for unknown providers) with automatic capability detection for Gemini, Groq, OpenRouter, and OpenAI, and `load_providers_from_settings()` building providers from environment (`LLM_PRIMARY_*`, `LLM_SECONDARY_*`, `LLM_TERTIARY_*`).
- Implemented `llm/client.py`: Async wrapper over `openai.AsyncOpenAI(base_url, api_key, timeout=12, max_retries=0)` supporting explicit `httpx.AsyncClient` injection for zero-network testing with respx. Maps 401/403 to `ConfigError` naming provider (never the key) and 404 to clear model-not-found `ConfigError`.
- Implemented `llm/cache.py`: Canonical SHA-256 JSON key hashing across `(provider, model, messages, tools, schema)` with persistent storage under `agent/.llm_cache/` (gitignored), supporting `off`, `record`, and `replay` modes. Atomic file writes and strict `CacheMiss` exception on replay misses.
- Implemented `llm/chain.py`: Multi-provider failover chain trying primary, secondary, and tertiary on timeout, 429, 5xx, or connection errors. In-memory `CircuitBreaker` using domain `Clock` (KP-06) that trips after 3 consecutive failures within 60s, skips provider for 60s cooldown, then enters half-open state. Safe structlog logging emitting `provider`, `model`, `latency_ms`, `cached`, `attempt` without prompts or keys.
- Implemented `llm/structured.py`: `generate_structured[T: BaseModel]` leveraging JSON schema mode if supported or fallback to JSON object mode, Pydantic validation, single automatic retry with validation error feedback, and `StructuredOutputError` on double failure.
- Implemented `scripts/llm_smoke.py`: Executable verification script requesting flat Pydantic model `{answer: str, confidence: float}` for "Reply with the word ready", printing latencies and results without printing secrets, exiting 1 if no provider works.
- Authored 12 offline tests in `agent/tests/test_llm.py` mocking providers with respx (0 network calls).
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run pytest -q`: 97 passed in 3.50s.
  - `cd agent; uv run ruff check .`: All checks passed!
  - `cd agent; uv run mypy src`: Success: no issues found in 23 source files.
- Accidental secrets check: `git grep -n -E 'AIza|gsk_'` returned 0 results.
Follow-ups:
- Human verification of `scripts/llm_smoke.py` with real keys in `agent/.env`.
- Proceed to T0.11 (Gate G0, local commit and tag).

## 2026-11-11 T0.9 JWT and JWKS verification
Files: agent/src/sellershield_agent/auth/__init__.py, agent/src/sellershield_agent/auth/models.py, agent/src/sellershield_agent/auth/jwks.py, agent/src/sellershield_agent/auth/verify.py, agent/src/sellershield_agent/auth/deps.py, agent/src/sellershield_agent/errors.py, agent/src/sellershield_agent/config.py, agent/src/sellershield_agent/api/health.py, agent/tests/test_auth.py, agent/tests/test_health.py, agent/tests/test_money.py, agent/tests/conftest.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/PITFALLS.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `auth/models.py` with `Role` StrEnum (`customer`, `seller_staff`, `seller_owner`, `service`) and immutable `Principal` model (`user_id`, `role`, `customer_id`, `scopes`, `auth_time`, `jti`, `token_exp`).
- Implemented `auth/jwks.py` `JWKSClient` with lazy fetch on first use, 1-hour in-memory cache, unknown `kid` refresh rate limit of at most once per 30 seconds, 3 retries with backoff, retention of last known good keys if a refetch fails, `follow_redirects=False` with strict timeouts, and rejection of plain HTTP unless `DEV_MODE=True`.
- Implemented `auth/verify.py` `verify_token` enforcing RS256 only (rejecting `none`, `HS256`, algorithm confusion), non-empty `kid`, claims validation (`sub`, `role`, `iss`, `exp`, `iat`, `jti`, `aud` containing `sellershield-agent`), evaluation of time claims against domain `Clock` with 30s leeway (KP-06), mandatory `customer_id` for customer role, and redacting raw tokens from all logs and error envelopes.
- Implemented `auth/deps.py` with FastAPI dependencies `get_principal`, `require_customer`, `require_seller`, `require_owner`, and `require_fresh_auth(max_age_seconds=900)` answering 401 with `step_up_required` for stale authentication.
- Updated `errors.py` with `AppHTTPException` carrying explicit error code and details, and default 401 mapping to `unauthenticated`.
- Updated `config.py` with `JWT_ISSUER: str = "nestkart-store"`.
- Updated `api/health.py` `/readyz` probe to check JWKS readiness and return 200 `status="degraded"` when store JWKS is down, preventing startup crashes.
- Authored 24 comprehensive security, rotation, outage, and adversarial break-it tests in `agent/tests/test_auth.py` with all RSA keys generated exclusively in memory.
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run pytest -q`: 85 passed in 2.92s.
  - `cd agent; uv run ruff check .`: All checks passed!
  - `cd agent; uv run mypy src`: Success: no issues found in 17 source files.
- `cd agent; uv run pytest tests/test_auth.py -q`: 24 passed in 1.69s.
- Accidental secrets check: `git grep -n -E 'BEGIN (RSA )?PRIVATE KEY'` returned 0 results.
Follow-ups:
- Proceed to T0.10 (LLM adapter and provider chain).

## 2026-11-11 T0.8 Database, clock, money and time helpers
Files: agent/src/sellershield_agent/db.py, agent/src/sellershield_agent/clock.py, agent/src/sellershield_agent/money.py, agent/src/sellershield_agent/timeutil.py, agent/tests/test_db.py, agent/tests/test_money.py, agent/tests/test_timeutil.py, README.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `db.py` SQLite engine factory with Section 16 recipe: `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `isolation_level=None` on connect, and `BEGIN IMMEDIATE` on begin. Added `write_tx()` and `read_tx()` context managers (with docstring prohibiting transaction holds across awaits/HTTP) and idempotent `init_db()` creating `_meta` table.
- Implemented `clock.py` with `Clock` protocol, `SystemClock` (aware UTC), `FixedClock` (settable, rejecting naive datetimes), and `StoreClock` stub.
- Implemented `money.py` with `Paise = int`, `format_inr()` (integer math only: whole rupees without decimals, fractional with two decimals and zero padding), `parse_rupees()` (Decimal-based, rejecting float and >2 decimals), and `allocate_largest_remainder()` (Hamilton method with stable tie-breaking).
- Implemented `timeutil.py` with `to_ist()`, `ist_day_end()` (calculates 23:59:59.999999 Asia/Kolkata date + days as aware UTC), and `is_within_window()` with strict rejection of naive datetimes.
- Authored 41 comprehensive unit, concurrency, and property tests (Hypothesis) covering WAL pragmas, thread concurrency under busy_timeout, transaction rollback, uncommitted write isolation, exact rupee allocation, leap day / midnight boundaries, and tzdata loading.
- Documented integer paise and IST boundary guarantees in root `README.md`.
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run pytest -q`: 60 passed in 1.15s.
  - `cd agent; uv run ruff check .`: All checks passed.
  - `cd agent; uv run mypy src`: Success: no issues found in 12 source files.
- `cd agent; uv run pytest tests/test_db.py tests/test_money.py tests/test_timeutil.py -v`: 41 passed in 1.52s.
Follow-ups:
- Proceed to T0.9 (JWT and JWKS verification).

## 2026-11-11 T0.7 Agent service skeleton
Files: agent/pyproject.toml, agent/.env.example, agent/src/sellershield_agent/__init__.py, agent/src/sellershield_agent/config.py, agent/src/sellershield_agent/logging.py, agent/src/sellershield_agent/middleware.py, agent/src/sellershield_agent/errors.py, agent/src/sellershield_agent/api/__init__.py, agent/src/sellershield_agent/api/health.py, agent/src/sellershield_agent/main.py, agent/tests/__init__.py, agent/tests/conftest.py, agent/tests/test_config.py, agent/tests/test_cors.py, agent/tests/test_errors.py, agent/tests/test_health.py, agent/tests/test_logging.py, agent/tests/test_request_id.py, docs/FEATURES.md, docs/DECISIONS.md, docs/CHANGELOG.md, docs/TASKS.md, docs/STATE.md
What changed:
- Initialized `agent/` as a uv package with Python >=3.12, pinned dependencies from 02-agent.md Section 0A, and configured strict tooling (`mypy strict`, `ruff`, `pytest-asyncio`).
- Implemented `config.py` with Pydantic Settings reading `agent/.env`, using `SecretStr` for credentials, and a fail-fast loader that reports missing/invalid variable names only without leaking secret values or inputs.
- Implemented `logging.py` with Structlog JSON output, recursive redaction of sensitive keys (`authorization`, `api_key`, `token`, `access_token`, `password`, `secret`, `cookie`), email pattern masking (`[REDACTED_EMAIL]`), and request_id context binding.
- Implemented `middleware.py` with `RequestIDMiddleware` (enforces `^[A-Za-z0-9-]{8,64}$`, echoes header), `AccessLogMiddleware` (single structured access line with latency_ms), and `BodyLimitMiddleware` (pure ASGI middleware enforcing 1 MB limit returning 413 envelope).
- Implemented `errors.py` with standard error envelopes (`{error: {code, message, request_id, details}}`) for HTTP exceptions, sanitized request validation errors, and safe 500 internal errors suppressing tracebacks.
- Implemented `api/health.py` with GET `/healthz` (always 200 ok) and GET `/readyz` (checks settings availability).
- Implemented `main.py` with `create_app()` factory, CORS disabled by default, and interactive OpenAPI documentation gated behind `DEV_MODE=True`.
- Authored test suite in `agent/tests/` covering all 9 required specs and break-it cases (19 tests).
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run pytest -q`: 19 passed in 0.31s.
  - `cd agent; uv run ruff check .`: All checks passed.
  - `cd agent; uv run mypy src`: Success: no issues found in 8 source files.
- Live server test on port 8001:
  - `Invoke-RestMethod http://localhost:8001/healthz`: `{"status":"ok","timestamp":"2026-11-07T11:18:07.581236+00:00"}`
  - `Invoke-RestMethod http://localhost:8001/nope`: `{"error":{"code":"not_found","message":"Not Found","request_id":"req-9005c5e6c63940d3","details":{}}}`
Follow-ups:
- Proceed to T0.8 (Database, clock, money and time helpers).

## 2026-11-11 T0.6b Agent OpenAPI contract (milestone M0)
Files: contracts/openapi-agent.yaml, docs/spec/CONTRACT_COVERAGE.md, scripts/contract_check.py, docs/FEATURES.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md, docs/CHANGELOG.md
What changed:
- Authored contracts/openapi-agent.yaml (OpenAPI 3.1) with 48 operations across 8 tags.
- Defined chat SSE stream (x-sse-events) covering message.delta, tool.status, cards (order_summary, shipment_timeline, return_preview, confirmation, case_created, chips), final, and error.
- Defined push notifications stream GET /v1/chat/stream and polling fallback GET /v1/me/events?since=.
- Defined seller approvals workflow with 401 step_up_required (15-min auth_time check).
- Defined case brief retrieval, seller inbox management, and agent handbacks.
- Enforced money safety: all money fields end in _paise and are type integer.
- Enforced idempotency: all mutating POST operations require Idempotency-Key header.
- Updated docs/spec/CONTRACT_COVERAGE.md mapping all Section 11 and Section 21 amendments (0 missing).
- Milestone M0 contract freeze complete across Store and Agent services.
Verification run (commands and result summary):
- `uvx openapi-spec-validator contracts/openapi-agent.yaml`: OK.
- `uv run --with pyyaml python scripts/contract_check.py contracts/openapi-agent.yaml`: Total operations 48, ALL CHECKS PASSED.
- `(Select-String -Path contracts/openapi-agent.yaml -Pattern 'operationId:').Count`: 48.
- `Select-String -Path docs/spec/CONTRACT_COVERAGE.md -Pattern 'MISSING'`: 0 occurrences.
Follow-ups:
- Notify W1 and W2 that contracts/ is frozen (Milestone M0).
- Proceed to T0.7 (Agent service skeleton).

## 2026-11-11 T0.6a Store OpenAPI contract (milestone M0)
Files: contracts/openapi-store.yaml, docs/spec/CONTRACT_COVERAGE.md, scripts/contract_check.py, docs/FEATURES.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md, docs/CHANGELOG.md
What changed:
- Authored contracts/openapi-store.yaml (OpenAPI 3.1) with 49 operations across 8 tags and 7 webhook outbox definitions.
- Enforced money safety: all money attributes end in _paise and are type integer.
- Enforced idempotency: all mutating POST operations require Idempotency-Key header.
- Enforced data isolation: uniform 404 for unowned orders; additionalProperties: false on all request bodies.
- Enforced concurrency control: If-Match version tag on admin settings updates.
- Created docs/spec/CONTRACT_COVERAGE.md mapping all Section 6 and Section 16 amendments (0 missing).
- Created scripts/contract_check.py verifying AST invariants, operationIds, security, idempotency, and error envelopes.
Verification run (commands and result summary):
- `uvx openapi-spec-validator contracts/openapi-store.yaml`: OK (schema valid OpenAPI 3.1).
- `uv run --with pyyaml python scripts/contract_check.py contracts/openapi-store.yaml`: Total operations 49, ALL CHECKS PASSED.
- `(Select-String -Path contracts/openapi-store.yaml -Pattern 'operationId:').Count`: 49.
- `Select-String -Path docs/spec/CONTRACT_COVERAGE.md -Pattern 'MISSING'`: 0 occurrences.
Follow-ups:
- Implement Prompt 6b (TASK T0.6b - Agent OpenAPI contract).

## 2026-11-11 T0.4 Repository skeleton and tooling
Files: .gitignore, .gitattributes, .editorconfig, justfile, .env.example, README.md, agent/README.md, store-api/README.md, store-web/README.md, contracts/README.md, scenarios/golden/README.md, scripts/README.md, docs/spec/README.md, docs/TASKS.md, docs/STATE.md, docs/CHANGELOG.md
What changed:
- Created .gitignore with protections for .env, sqlite DBs, caches, virtual environments, and node_modules.
- Created .gitattributes with `* text=auto` and .editorconfig with multi-language spacing rules.
- Created justfile with powershell windows-shell and recipes for agent checks, secrets scanning, and workspace stubs.
- Created .env.example listing all 21 agent configuration variables from docs/spec/02-agent.md Section 0A with 'replace-me'.
- Created root README.md with project summary, status, quickstart stubs, and build timeline disclosure.
- Created skeleton folders with ownership READMEs: agent/ (A), store-api/ (W1), store-web/ (W2), contracts/ (A), scenarios/golden/ (A & team), scripts/ (All), docs/spec/ (All).
Verification run (commands and result summary):
- `(Get-Item AGENTS.md).Length`: 6545 bytes (under 12000 character limit).
- `git status`: Successfully lists all newly added skeleton files as untracked.
- `Set-Content -Path .env -Value 'TEST=1' ; git status ; Remove-Item .env`: Verified that .env is ignored and excluded by git status.
Follow-ups:
- Human toolchain installation for `just` and `gitleaks` (T0.1).
