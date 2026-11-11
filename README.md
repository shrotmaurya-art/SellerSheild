# SellerShield AI & NestKart Playground

[![Live Demo](https://img.shields.io/badge/Live%20Demo-Railway-0B0D0E?style=for-the-badge&logo=railway)](https://sellershield-ai-production.up.railway.app)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React 18](https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://react.dev)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.4-3178C6?style=for-the-badge&logo=typescript&logoColor=white)](https://www.typescriptlang.org)

**SellerShield AI** is an autonomous, policy-governed customer support agent designed specifically for e-commerce sellers. It resolves routine post-purchase inquiries (returns, refunds, cancellations, tracking delays) end-to-end through deterministic policy evaluation, strict fact verification, and human-in-the-loop approvals—ensuring money safety, auditability, and verifiable truth across every customer interaction.

---

## 🌟 Hosted Live Demo

The complete stack is deployed live on a high-availability single-container architecture reverse-proxied by Caddy:

🔗 **Public Application URL**: **[https://sellershield-ai-production.up.railway.app](https://sellershield-ai-production.up.railway.app)**

### 1-Click Demo Login
The live login page features a **One-Click Persona Switcher** (no typing required). Simply click any demo persona card to sign in:

| Role | Persona Name | Email | Password | What You Can Test |
| :--- | :--- | :--- | :--- | :--- |
| **Customer** | Aarav Mehta | `aarav.mehta@example.test` | `Demo@12345` | Recent delivered orders, active return requests, open chat with SellerShield AI |
| **Customer** | Diya Nair | `diya.nair@example.test` | `Demo@12345` | In-transit orders, carrier tracking delays, delivery inquiries |
| **Customer** | Rohan Das | `rohan.das@example.test` | `Demo@12345` | Non-returnable electronics policy, sealed hygiene items |
| **Customer** | Meera Iyer | `meera.iyer@example.test` | `Demo@12345` | Multi-item orders, partial item return, refund calculation |
| **Customer** | Kabir Joshi | `kabir.joshi@example.test` | `Demo@12345` | Order cancellations, window expiration scenarios |
| **Seller Owner** | Priya Sharma | `priya.sharma@nestkart.test` | `Demo@12345` | Full Seller Dashboard, Pending Approvals queue, Audit Log, and **Demo Control Center** |
| **Seller Staff** | Kiran Rao | `kiran.rao@nestkart.test` | `Demo@12345` | Store operations, order status inspection, customer lookup |

> 💡 **Demo Control Center (`/seller/demo`)**: Log in as **Priya Sharma** (Seller Owner) and open the **Demo Control Center** from the sidebar to warp simulated time forward, simulate carrier delay notifications, trigger test webhooks, or reset demo database state back to pristine.

---

## 🏗️ System Architecture

```
                  +-------------------------------------------------------------+
                  |                     Public Port (:8080)                     |
                  |                Caddy 2 Reverse Proxy & Static SPA           |
                  +------------------------------+------------------------------+
                                                 |
         +---------------------------------------+---------------------------------------+
         |                                                                               |
         v                                                                               v
+-------------------------------+                               +-------------------------------+
|    Store API (Port 8000)      |                               |     Agent API (Port 8001)     |
|  FastAPI + SQLAlchemy + SQLite|                               |  FastAPI + Policy + LLM Engine|
|-------------------------------|                               |-------------------------------|
| - /api/v1/auth & /orders      |                               | - /agent/v1/chat (SSE stream) |
| - /api/v1/returns & /refunds  |  Webhooks & Service JWT Auth  | - Deterministic Policy Engine |
| - /demo/* control endpoints   | <===========================> | - LLM FactSheet Verifier      |
| - JWKS endpoint (/.well-known)|                               | - Cryptographic Audit Chain   |
+-------------------------------+                               +-------------------------------+
```

### Core Invariants
1. **Zero Hallucination / FactSheet Verifier**: LLM responses pass through an offline deterministic verifier before being sent to customers. Any ungrounded claims (unverified dates, inflated refund amounts, invalid order IDs) are caught and rejected.
2. **Deterministic Money Math**: All financial computations are performed in exact integer **paise** (`₹1.00 = 100 paise`), eliminating floating-point rounding errors.
3. **Timezone Accuracy**: Return windows and cancellation deadlines are strictly evaluated against calendar day boundaries in **Asia/Kolkata (IST)**.
4. **Cryptographic Audit Chain**: Every policy decision, customer action, and seller approval is appended to a SHA-256 tamper-evident hash chain.

---

## 🚀 Local Setup Guide

If you clone or pull the repository to run locally, follow these steps:

### Prerequisites
- **Python**: 3.12+
- **Node.js**: 20+ (with `npm` or `pnpm`)
- **uv**: Fast Python package manager (`pip install uv` or via standalone installer)
- **Just**: (Optional) Task runner (`cargo install just` or via package manager)

---

### Step 1: Clone Repository
```bash
git clone https://github.com/shrotmaurya-art/sellershield-ai.git
cd sellershield-ai
```

---

### Step 2: Configure Environment Variables

Create `.env` files for both backend services using the provided examples:

```bash
# In store-api directory
cp store-api/.env.example store-api/.env

# In agent directory
cp agent/.env.example agent/.env
```

> **Note on LLM Keys**:
> - For local development and automated testing, mock fixtures work out-of-the-box (`DEV_MODE=true`).
> - To test live LLM chat interactions, populate `LLM_PRIMARY_API_KEY` (Gemini API Key) in `agent/.env`.

---

### Step 3: Install Dependencies

Using `uv` and `npm`:

```bash
# 1. Install Store API dependencies
cd store-api
uv sync
cd ..

# 2. Install Agent Service dependencies
cd agent
uv sync
cd ..

# 3. Install Frontend Web dependencies
cd store-web
npm install
cd ..
```

---

### Step 4: Initialize and Seed Database

Run the database seed script to populate demo products, customers, orders, and return policies:

```bash
uv run python scripts/seed.py
```

This creates a SQLite database in `store-api/data/store.db` pre-loaded with simulated scenarios.

---

### Step 5: Start Local Services

Open three terminal tabs to run the services:

#### Terminal 1 — Store API (Port 8000)
```bash
cd store-api
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

#### Terminal 2 — Agent Service (Port 8001)
```bash
cd agent
uv run uvicorn sellershield_agent.main:create_app --factory --host 127.0.0.1 --port 8001 --reload
```

#### Terminal 3 — Store Web Frontend (Port 5173)
```bash
cd store-web
npm run dev
```

The web application will open at **`http://localhost:5173`**. Vite automatically proxies `/api`, `/demo`, `/mail`, and `/agent` requests to the local backend services.

---

## 🧪 Testing & Verification

Run the comprehensive test suite to verify types, linting, policy logic, and regression guards:

```bash
# Run agent verification suite (ruff, mypy, pytest, test-guard)
just agent-check

# Run store-api tests
cd store-api && uv run pytest

# Run live deployment smoke test (against hosted URL)
powershell -ExecutionPolicy Bypass -File deploy/smoke.ps1 -BaseUrl "https://sellershield-ai-production.up.railway.app"
```

---

## 🔒 Security & Privacy Notice
- Never commit `.env` files or API keys.
- Production deployments run with `DEV_MODE=false`, disabling internal dev routes, Swagger documentation, and OpenAPI schemas.
- In production, static asset serving enforces `nosniff`, `DENY` frame options, strict CSP, and double-submit CSRF cookie validation.
