# ARCHITECTURE (one page; the full spec is in docs/spec/)
Three parts: store-api and store-web (NestKart mock store, system of record) and agent (SellerShield service).
Flow: customer chat -> agent (understand -> resolve with read tools -> grounded draft -> verifier -> stream).
Writes: LLM only proposes. Customer taps Confirm -> executor saga -> store. Seller approves with the seller's own token -> store records authorization.
Store enforces ownership, state machines, refund balance, autonomy cap and daily cap.
Agent owns policy, proposals, approvals, cases, memory, audit chain, notification outbox, monitor.
Trust: JWT from the store (RS256, JWKS). Customer token only inside its request; service tokens for background work.
Data: SQLite per service. Money int paise. UTC storage, IST policy windows, one clock from the store.
Full detail: docs/spec/02-agent.md (agent) and docs/spec/01-playground.md (store).