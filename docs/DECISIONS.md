# DECISIONS (choices and reasons; do not reverse without a new entry)
ADR-001 SQLite files for store and agent (no Supabase or Firebase): transactions, zero setup, offline demo.
ADR-002 Hand-written orchestrator, no LangChain: control of loops, allowlists, audit and tests.
ADR-003 OpenAI-compatible client for all LLM providers: one adapter, config switches provider.
ADR-004 LLM tools are read and propose only; writes live in the execution layer after a human click.
ADR-005 uv for Python and just for tasks on Windows (PowerShell shell); no global pip.
ADR-006 Store is the identity provider (RS256 and JWKS); agent only verifies.


Entry format:
## ADR-0xx Title  Date  Status
Context / Options considered / Decision / Consequences