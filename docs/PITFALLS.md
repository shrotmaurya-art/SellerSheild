# PITFALLS (read the ones that match the files you touch)
KP-01 SQLite: use the BEGIN IMMEDIATE recipe from docs/spec/01-playground.md section 16; never hold a write transaction across await or HTTP.
KP-02 Browser EventSource cannot send Authorization; streams use fetch-event-source.
KP-03 Windows needs the tzdata package for zoneinfo; Asia/Kolkata fails without it.
KP-04 JWT: allow RS256 only; reject alg none and HS256 signed with the public key; check aud, iss, exp.
KP-05 Money as float causes paise drift; use int paise and largest-remainder allocation.
KP-06 datetime.now() in domain code breaks the simulated clock; use Clock.
KP-07 Repo inside OneDrive or Dropbox corrupts SQLite; keep it in C:/dev.
KP-08 PowerShell has no && or export; justfile needs windows-shell set to PowerShell.
KP-09 Provider JSON-schema support differs; keep schemas flat; validate with Pydantic; retry once.
KP-10 Agents weaken tests to pass; tests are frozen unless the spec changes.
KP-11 Agents invent library APIs; check installed versions first.
KP-12 Verifier bypass: any code path that sends LLM text to a customer without the verifier is a bug.
KP-13 Tokens or keys in logs; redact Authorization, api_key, token, password, secret, e-mail.
KP-14 .env committed by accident; .gitignore first, gitleaks before every commit.
KP-15 Order not owned must look identical to order not found (uniform 404, same text, similar timing).
KP-16 Retried writes without idempotency keys create duplicates.
KP-17 SSE behind proxies buffers output; send no-cache and X-Accel-Buffering no.
KP-18 Free-tier LLM quota is small; use the replay cache and per-session budgets.
KP-19 Antigravity rule files max 12,000 characters; keep AGENTS.md short.
KP-20 One agent per folder at a time; parallel agents overwrite each other.