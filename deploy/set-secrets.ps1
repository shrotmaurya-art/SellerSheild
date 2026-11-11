# deploy/set-secrets.ps1
# Generates cryptographic tokens, securely prompts for LLM keys, and provisions platform secrets.
# Invariant: Never writes, logs, prints, or echoes secrets to screen, files, or git.

param(
    [string]$Platform = "railway" # "railway" or "fly"
)

$ErrorActionPreference = "Stop"

Write-Host "==> [1/3] Generating random secrets with .NET RNGCryptoServiceProvider..."
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()

function New-SecureToken([int]$bytes = 32) {
    $buffer = New-Object byte[] $bytes
    $rng.GetBytes($buffer)
    return ($buffer | ForEach-Object { "{0:x2}" -f $_ }) -join ""
}

$webhookSecret = New-SecureToken 32
$executorSecret = New-SecureToken 32
$monitorSecret = New-SecureToken 32
$serviceSecretsJson = "{`"agent-executor`":`"$executorSecret`",`"agent-monitor`":`"$monitorSecret`"}"

Write-Host "==> [2/3] Secure prompt for LLM Keys (input is masked and unlogged)..."
$primaryKeySecure = Read-Host -Prompt "Enter Primary LLM API Key (Gemini) [LLM_PRIMARY_API_KEY]" -AsSecureString
if (-not $primaryKeySecure) {
    Write-Error "Primary LLM key cannot be empty."
    exit 1
}

$secondaryKeySecure = Read-Host -Prompt "Enter Secondary LLM API Key (optional - press Enter to skip)" -AsSecureString

function ConvertFrom-SecureToPlain($sec) {
    if (-not $sec) { return "" }
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try {
        return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

$primaryKey = ConvertFrom-SecureToPlain $primaryKeySecure
$secondaryKey = ConvertFrom-SecureToPlain $secondaryKeySecure

Write-Host "==> [3/3] Setting secrets on platform ($Platform)..."

if ($Platform -eq "railway") {
    if (-not (Get-Command railway -ErrorAction SilentlyContinue)) {
        Write-Error "Railway CLI is not installed or not in PATH."
        exit 1
    }

    # Set variables in Railway on the linked service
    $varArgs = @(
        "variable", "set",
        "WEBHOOK_SECRET=$webhookSecret",
        "STORE_EXECUTOR_CLIENT_ID=agent-executor",
        "STORE_EXECUTOR_CLIENT_SECRET=$executorSecret",
        "STORE_MONITOR_CLIENT_ID=agent-monitor",
        "STORE_MONITOR_CLIENT_SECRET=$monitorSecret",
        "SERVICE_CLIENT_SECRETS=$serviceSecretsJson",
        "LLM_PRIMARY_API_KEY=$primaryKey",
        "DEV_MODE=false",
        "DEMO_MODE=true",
        "STORE_MODE=http",
        "--skip-deploys"
    )
    if ($secondaryKey) {
        $varArgs += "LLM_SECONDARY_API_KEY=$secondaryKey"
    }

    & railway @varArgs | Out-Null

    Write-Host "SUCCESS: Secrets and production variables securely configured on Railway."
} elseif ($Platform -eq "fly") {
    $flyCmd = if (Get-Command fly -ErrorAction SilentlyContinue) { "fly" } else { "flyctl" }
    if (-not (Get-Command $flyCmd -ErrorAction SilentlyContinue)) {
        Write-Error "Fly CLI is not installed or not in PATH."
        exit 1
    }

    $flySecrets = @(
        "WEBHOOK_SECRET=$webhookSecret",
        "STORE_EXECUTOR_CLIENT_ID=agent-executor",
        "STORE_EXECUTOR_CLIENT_SECRET=$executorSecret",
        "STORE_MONITOR_CLIENT_ID=agent-monitor",
        "STORE_MONITOR_CLIENT_SECRET=$monitorSecret",
        "SERVICE_CLIENT_SECRETS=$serviceSecretsJson",
        "LLM_PRIMARY_API_KEY=$primaryKey",
        "DEV_MODE=false",
        "DEMO_MODE=true",
        "STORE_MODE=http"
    )
    if ($secondaryKey) {
        $flySecrets += "LLM_SECONDARY_API_KEY=$secondaryKey"
    }

    & $flyCmd secrets set @flySecrets --config deploy/fly.toml
    Write-Host "SUCCESS: Secrets securely configured on Fly.io."
} else {
    Write-Error "Unsupported platform: $Platform."
    exit 1
}

# In-memory cleanup
$webhookSecret = $null
$executorSecret = $null
$monitorSecret = $null
$serviceSecretsJson = $null
$primaryKey = $null
$secondaryKey = $null
$primaryKeySecure = $null
$secondaryKeySecure = $null

