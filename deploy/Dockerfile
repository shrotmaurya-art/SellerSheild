# Multi-stage Dockerfile for SellerShield AI
# Stage 1: Build the React web frontend
FROM node:20-alpine AS web-builder
WORKDIR /build

# Copy frontend package manifests and install dependencies
COPY store-web/package.json store-web/package-lock.json ./
RUN npm ci

# Copy frontend source and build production bundle
COPY store-web/ ./
RUN npm run build

# Stage 2: Runtime image with Python 3.12, Caddy, uv, and dependencies
FROM python:3.12-slim AS runtime

# Install system utilities: curl, openssl, ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    openssl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy Caddy binary from official Caddy image
COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy

# Copy uv binary
COPY --from=ghcr.io/astral-sh/uv:0.5.26 /uv /uvx /bin/

WORKDIR /app

# Copy dependency manifests and lockfiles for store-api and agent
COPY store-api/pyproject.toml store-api/uv.lock store-api/README.md /app/store-api/
COPY agent/pyproject.toml agent/uv.lock agent/README.md /app/agent/

# Install store-api dependencies into /app/store-api/.venv
RUN cd /app/store-api && uv sync --frozen --no-dev

# Install agent dependencies into /app/agent/.venv
RUN cd /app/agent && uv sync --frozen --no-dev

# Copy application source code and scenarios
COPY store-api/ /app/store-api/
COPY agent/ /app/agent/
COPY scripts/ /app/scripts/
COPY scenarios/ /app/scenarios/
COPY contracts/ /app/contracts/

# Copy built frontend assets from web-builder
COPY --from=web-builder /build/dist /app/store-web/dist

# Copy deployment configurations
COPY deploy/Caddyfile /app/deploy/Caddyfile
COPY deploy/start.sh /app/deploy/start.sh
RUN chmod +x /app/deploy/start.sh

# Default environment configuration
ENV PORT=8080 \
    DEMO_MODE=true \
    DEV_MODE=false \
    STORE_MODE=http \
    STORE_BASE_URL=http://127.0.0.1:8000 \
    STORE_JWKS_URL=http://127.0.0.1:8000/.well-known/jwks.json \
    AGENT_WEBHOOK_URL=http://127.0.0.1:8001/v1/webhooks/store \
    DATABASE_URL=sqlite:////app/agent/data/agent.db \
    JWT_ISSUER=nestkart-store \
    JWT_PRIVATE_KEY_PATH=/app/store-api/keys/jwt_private.pem \
    JWT_PUBLIC_KEY_PATH=/app/store-api/keys/jwt_public.pem \
    STORE_EXECUTOR_CLIENT_ID=agent-executor \
    STORE_MONITOR_CLIENT_ID=agent-monitor \
    LLM_PRIMARY_MODEL=gemini-2.5-flash \
    LLM_PRIMARY_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/ \
    LLM_TIMEOUT_S=15 \
    LLM_CACHE_MODE=off \
    PROMPT_VERSION=v1

EXPOSE 8080

CMD ["/app/deploy/start.sh"]

