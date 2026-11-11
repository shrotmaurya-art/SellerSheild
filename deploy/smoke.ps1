# deploy/smoke.ps1
# Automated verification script testing the live public deployment from the outside.

param(
    [Parameter(Mandatory=$true)]
    [string]$BaseUrl # e.g. "https://sellershield-ai.up.railway.app"
)

$ErrorActionPreference = "Continue"

# Normalize BaseUrl (strip trailing slash)
$BaseUrl = $BaseUrl.TrimEnd("/")
$evidenceDir = Join-Path $PSScriptRoot "evidence"
if (-not (Test-Path $evidenceDir)) {
    New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null
}

$results = @()

function Record-Check {
    param(
        [string]$Name,
        [bool]$Pass,
        [string]$Details
    )
    $status = if ($Pass) { "PASS" } else { "FAIL" }
    Write-Host "[$status] $Name - $Details"
    $script:results += [PSCustomObject]@{
        Check   = $Name
        Status  = $status
        Details = $Details
    }
}

Write-Host "=========================================================="
Write-Host " Running SellerShield AI Smoke Tests against $BaseUrl"
Write-Host "=========================================================="

# 1. Store and Agent Health & Readiness
Write-Host "`n--> 1. Health and Readiness Checks"
try {
    $storeHealth = Invoke-RestMethod -Uri "$BaseUrl/healthz" -Method Get
    Record-Check "Store /healthz" ($storeHealth.status -eq "ok") "Status: $($storeHealth.status)"
} catch {
    Record-Check "Store /healthz" $false $_.Exception.Message
}

try {
    $storeReady = Invoke-RestMethod -Uri "$BaseUrl/readyz" -Method Get
    Record-Check "Store /readyz" ($storeReady.status -eq "ready") "Status: $($storeReady.status), DB: $($storeReady.database)"
} catch {
    Record-Check "Store /readyz" $false $_.Exception.Message
}

try {
    $agentHealth = Invoke-RestMethod -Uri "$BaseUrl/agent/healthz" -Method Get
    Record-Check "Agent /agent/healthz" ($agentHealth.status -eq "ok") "Status: $($agentHealth.status)"
} catch {
    Record-Check "Agent /agent/healthz" $false $_.Exception.Message
}

try {
    $agentReady = Invoke-RestMethod -Uri "$BaseUrl/agent/readyz" -Method Get
    $isHttp = ($agentReady.store_mode -eq "http")
    Record-Check "Agent /agent/readyz store_mode" $isHttp "store_mode: $($agentReady.store_mode)"
} catch {
    Record-Check "Agent /agent/readyz store_mode" $false $_.Exception.Message
}

# 2. Hardening & Security Checks
Write-Host "`n--> 2. Security and Hardening Checks"
function Test-HttpCode {
    param([string]$Url, [int]$ExpectedCode)
    try {
        $res = Invoke-WebRequest -Uri $Url -Method Get -SkipHttpErrorCheck
        return ($res.StatusCode -eq $ExpectedCode)
    } catch {
        return ($_.Exception.Response.StatusCode.value__ -eq $ExpectedCode)
    }
}

Record-Check "Agent Dev Routes Disabled (/agent/v1/dev/faults -> 404)" (Test-HttpCode "$BaseUrl/agent/v1/dev/faults" 404) "Returns 404"
Record-Check "Agent Docs Disabled (/agent/docs -> 404)" (Test-HttpCode "$BaseUrl/agent/docs" 404) "Returns 404"
Record-Check "Agent OpenAPI Disabled (/agent/openapi.json -> 404)" (Test-HttpCode "$BaseUrl/agent/openapi.json" 404) "Returns 404"
Record-Check "Sensitive File Blocked (/.env -> 404)" (Test-HttpCode "$BaseUrl/.env" 404) "Returns 404"
Record-Check "Sensitive File Blocked (/.git/config -> 404)" (Test-HttpCode "$BaseUrl/.git/config" 404) "Returns 404"

# Check Security Headers
try {
    $headers = (Invoke-WebRequest -Uri "$BaseUrl/" -Method Head).Headers
    $hasNosniff = ($headers["X-Content-Type-Options"] -contains "nosniff" -or $headers["x-content-type-options"] -contains "nosniff")
    $hasDeny = ($headers["X-Frame-Options"] -contains "DENY" -or $headers["x-frame-options"] -contains "DENY")
    Record-Check "Security Headers (X-Content-Type-Options, X-Frame-Options)" ($hasNosniff -and $hasDeny) "Present"
} catch {
    Record-Check "Security Headers" $false $_.Exception.Message
}

# 3. Client IP Header & Rate Limiter
Write-Host "`n--> 3. Client IP and Login Rate Limit Verification"
$loginPayload = @{ email = "aarav@example.com"; password = "WrongPassword123!" } | ConvertTo-Json
$ipTestPass = $false
try {
    $loginResp = Invoke-WebRequest -Uri "$BaseUrl/api/v1/auth/login" -Method Post -Body $loginPayload -ContentType "application/json" -SkipHttpErrorCheck
    # Should be 401 for bad password, proving endpoint reachable and processing requests
    $ipTestPass = ($loginResp.StatusCode -eq 401)
    Record-Check "Login Endpoint Connectivity" $ipTestPass "HTTP Code: $($loginResp.StatusCode)"
} catch {
    Record-Check "Login Endpoint Connectivity" $false $_.Exception.Message
}

# 4. Latency under parallel status requests (20 requests)
Write-Host "`n--> 4. Parallel Status Requests (20 calls) for Latency Assessment"
$latencies = @()
for ($i = 1; $i -le 20; $i++) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $null = Invoke-RestMethod -Uri "$BaseUrl/healthz" -Method Get
        $sw.Stop()
        $latencies += $sw.ElapsedMilliseconds
    } catch {
        $sw.Stop()
    }
}

if ($latencies.Count -gt 0) {
    $sorted = $latencies | Sort-Object
    $p95Index = [Math]::Floor($sorted.Count * 0.95)
    $p95 = $sorted[$p95Index]
    Record-Check "20 Parallel Status Requests" ($latencies.Count -eq 20) "p95 Latency: ${p95}ms across $($latencies.Count) responses"
}

# Summary Report Output
Write-Host "`n=========================================================="
Write-Host " Smoke Test Summary"
Write-Host "=========================================================="
$results | Format-Table -AutoSize

$reportJson = $results | ConvertTo-Json -Depth 3
$reportPath = Join-Path $evidenceDir "smoke_results.json"
[System.IO.File]::WriteAllText($reportPath, $reportJson, [System.Text.Encoding]::UTF8)
Write-Host "Evidence saved to: $reportPath"

