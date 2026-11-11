#!/usr/bin/env bash
set -eo pipefail

echo "=========================================================="
echo " Starting SellerShield AI Stack (Production Mode)"
echo "=========================================================="

# 1. Ensure required runtime data and keys directories exist
mkdir -p /app/store-api/data /app/store-api/uploads /app/store-api/keys /app/agent/data /app/data

# 2. Fresh JWT RS256 keypair generation (never reuse dev keys)
if [ ! -f /app/store-api/keys/jwt_private.pem ]; then
    echo "==> Generating fresh RS256 keypair for deployment..."
    openssl genpkey -algorithm RSA -out /app/store-api/keys/jwt_private.pem -pkeyopt rsa_keygen_bits:2048
    openssl rsa -pubout -in /app/store-api/keys/jwt_private.pem -out /app/store-api/keys/jwt_public.pem
    echo "==> Fresh JWT RS256 keys generated."
fi

# 3. Seed store database
echo "==> Initializing and seeding store database..."
cd /app
/app/store-api/.venv/bin/python scripts/seed.py
echo "==> Store database seeded successfully."

# 4. Start Store API on 127.0.0.1:8000
echo "==> Starting Store API daemon on 127.0.0.1:8000..."
cd /app/store-api
/app/store-api/.venv/bin/python -m uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --proxy-headers \
    --forwarded-allow-ips 127.0.0.1 &
STORE_PID=$!

# 5. Start Agent API on 127.0.0.1:8001
echo "==> Starting Agent API daemon on 127.0.0.1:8001..."
cd /app/agent
/app/agent/.venv/bin/python -m uvicorn sellershield_agent.main:create_app \
    --factory \
    --app-dir /app/agent/src \
    --host 127.0.0.1 \
    --port 8001 \
    --proxy-headers \
    --forwarded-allow-ips 127.0.0.1 &
AGENT_PID=$!

# 6. Wait for internal endpoints to become healthy
echo "==> Waiting for Store and Agent services to become healthy..."
READY=0
for i in $(seq 1 30); do
    if curl -s -f http://127.0.0.1:8000/healthz >/dev/null 2>&1 && curl -s -f http://127.0.0.1:8001/healthz >/dev/null 2>&1; then
        echo "==> Store API and Agent API are healthy!"
        READY=1
        break
    fi
    sleep 1
done

if [ "$READY" -ne 1 ]; then
    echo "ERROR: Timed out waiting for Store API and Agent API to become ready."
    kill -TERM $STORE_PID $AGENT_PID 2>/dev/null || true
    exit 1
fi

# 7. Start Caddy reverse proxy
PORT="${PORT:-8080}"
echo "==> Launching Caddy reverse proxy on port $PORT..."
caddy run --config /app/deploy/Caddyfile --adapter caddyfile &
CADDY_PID=$!

# Supervisor trap: if any process dies, terminate the container so platform restarts
cleanup() {
    echo "Supervisor: Received shutdown signal or process termination. Cleaning up..."
    kill -TERM $STORE_PID $AGENT_PID $CADDY_PID 2>/dev/null || true
    exit 1
}

trap cleanup SIGINT SIGTERM CHLD

echo "==> All services running. Monitoring processes..."
wait -n $STORE_PID $AGENT_PID $CADDY_PID
EXIT_CODE=$?
echo "Supervisor: A core process terminated with status $EXIT_CODE. Exiting to trigger platform restart."
kill -TERM $STORE_PID $AGENT_PID $CADDY_PID 2>/dev/null || true
exit $EXIT_CODE

