# deploy/deploy.ps1
# Deploys SellerShield AI via platform CLI remote build (no local Docker required)

param(
    [string]$Platform = "railway" # "railway" or "fly"
)

$ErrorActionPreference = "Stop"

Write-Host "=========================================================="
Write-Host " SellerShield AI - Remote Direct-Upload Deployment"
Write-Host " Platform: $Platform"
Write-Host "=========================================================="

if ($Platform -eq "railway") {
    if (-not (Get-Command railway -ErrorAction SilentlyContinue)) {
        Write-Error "Railway CLI is not installed. Install via: npm install -g @railway/cli"
        exit 1
    }

    Write-Host "==> [1/4] Checking Railway authentication status..."
    $whoami = railway whoami 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Not logged in to Railway. Run 'railway login' first."
        exit 1
    }
    Write-Host "Logged in as: $whoami"

    Write-Host "==> [2/3] Uploading context and initiating cloud build on Railway..."
    railway up --service sellershield-ai --detach
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Railway deployment failed."
        exit 1
    }

    $appUrl = "https://sellershield-ai-production.up.railway.app"

    Write-Host ""
    Write-Host "=========================================================="
    Write-Host " Deployment Triggered!"
    Write-Host " Live URL: $appUrl"
    Write-Host " Check build logs with: railway logs"
    Write-Host "=========================================================="

} elseif ($Platform -eq "fly") {
    $flyCmd = if (Get-Command fly -ErrorAction SilentlyContinue) { "fly" } else { "flyctl" }
    if (-not (Get-Command $flyCmd -ErrorAction SilentlyContinue)) {
        Write-Error "Fly CLI is not installed."
        exit 1
    }

    Write-Host "==> [1/3] Verifying Fly authentication..."
    & $flyCmd auth whoami
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Not logged in to Fly.io. Run 'fly auth login' first."
        exit 1
    }

    Write-Host "==> [2/3] Launching remote build on Fly.io..."
    & $flyCmd deploy --config deploy/fly.toml --remote-only
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Fly deployment failed."
        exit 1
    }

    Write-Host "==> [3/3] Fetching public URL..."
    $appInfo = & $flyCmd status --config deploy/fly.toml --json | ConvertFrom-Json
    $appUrl = "https://$($appInfo.Hostname)"

    Write-Host ""
    Write-Host "=========================================================="
    Write-Host " Deployment Complete!"
    Write-Host " Live URL: $appUrl"
    Write-Host "=========================================================="
} else {
    Write-Error "Unsupported platform: $Platform"
    exit 1
}

