# SellerShield AI Deployment Guide

This directory contains the production container build, reverse-proxy configurations, operational automation, and verification scripts to deploy the complete SellerShield AI stack (Web Frontend, Store API, and Agent Service) to a single public HTTPS URL.

---

## Architecture Overview

```
[Browser / Judge] ──> HTTPS :443 ──> [Caddy :$PORT]
                                           │
         ┌─────────────────────────────────┼──────────────────────────────┐
         ▼                                 ▼                              ▼
[/app/store-web/dist]           [Store API :8000]               [Agent API :8001]
(Built React Web SPA)       (/api, /demo, /sim, /mail)               (/agent)
                                    │                                      │
                             [data/store.db]                        [data/agent.db]
```

- **Single Origin**: All traffic flows through Caddy on `$PORT`. No CORS complexity or mixed content.
- **SSE Streaming**: Caddy uses `flush_interval -1` and unbuffered reverse proxying for `/agent/*`.
- **Client IP Preservation**: Uvicorn runs with `--proxy-headers --forwarded-allow-ips 127.0.0.1` and Caddy forwards `X-Forwarded-For {remote_host}`.
- **Fresh Keys**: Fresh RS256 JWT keys are generated automatically on container start.
- **Deterministic State**: Database automatically seeds with all demo scenarios on startup.

---

## Step-by-Step Deployment Guide

### 1. Authenticate with Platform CLI

#### Recommended: Railway
```powershell
# Check login status
railway whoami

# If not logged in:
railway login
```

#### Alternative: Fly.io
```powershell
fly auth login
```

---

### 2. Configure Production Secrets

Run `deploy/set-secrets.ps1`. This script generates cryptographically random tokens using the .NET RNG, prompts for your Gemini API key via a masked prompt (`Read-Host -AsSecureString`), provisions the platform variables directly via the platform CLI, and never logs or prints secrets.

```powershell
# For Railway:
powershell -ExecutionPolicy Bypass -File deploy/set-secrets.ps1 -Platform railway

# For Fly.io:
powershell -ExecutionPolicy Bypass -File deploy/set-secrets.ps1 -Platform fly
```

---

### 3. Deploy the Stack

Trigger the remote direct-upload build:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/deploy.ps1 -Platform railway
```

The CLI builds the multi-stage image in the cloud (no local Docker required) and prints the public HTTPS URL.

---

### 4. Verify the Deployment

Execute the automated smoke verification against your live domain:

```powershell
powershell -ExecutionPolicy Bypass -File deploy/smoke.ps1 -BaseUrl "https://<your-app-domain>"
```

---

### 5. Day-to-Day Operations

#### View Live Application Logs
```powershell
# Railway
railway logs

# Fly.io
fly logs --config deploy/fly.toml
```

#### Reset Demo State to Initial Seed
```powershell
powershell -ExecutionPolicy Bypass -File deploy/reset-demo.ps1 -BaseUrl "https://<your-app-domain>"
```

#### Restart Instance
```powershell
# Railway
railway restart

# Fly.io
fly machine restart --config deploy/fly.toml
```

#### Rollback
```powershell
# Railway: Open Railway dashboard -> Deployments -> Click previous deployment -> Redeploy
# Fly.io:
fly releases --config deploy/fly.toml
# Deploy previous image:
fly deploy --image <previous-image-tag>
```

#### Teardown / Destroy
```powershell
# Railway
railway down

# Fly.io
fly apps destroy sellershield-ai
```

---

## Plan B: Local Quick Tunnel Fallback

If cloud deployment is blocked, run the stack locally and publish via Cloudflare Quick Tunnel:

```powershell
# Download cloudflared.exe (no login or card required) and publish:
cloudflared tunnel --url http://127.0.0.1:8080
```

