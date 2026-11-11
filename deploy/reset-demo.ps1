# deploy/reset-demo.ps1
# Resets the deployed demo environment by logging in as seller owner and triggering /demo/reset

param(
    [Parameter(Mandatory=$true)]
    [string]$BaseUrl # e.g. "https://sellershield-ai.up.railway.app"
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")

Write-Host "==> Logging in as seller owner (priya@example.com) to obtain access token..."
$loginBody = @{
    email    = "priya@example.com"
    password = "SellerOwnerPass123!"
} | ConvertTo-Json

$loginRes = Invoke-RestMethod -Uri "$BaseUrl/api/v1/auth/login" -Method Post -Body $loginBody -ContentType "application/json"
$token = $loginRes.access_token

Write-Host "==> Triggering demo reset via POST /demo/reset..."
$headers = @{
    "Authorization" = "Bearer $token"
}

$resetRes = Invoke-RestMethod -Uri "$BaseUrl/demo/reset" -Method Post -Headers $headers

Write-Host "==> Demo reset complete!"
Write-Host "Status: $($resetRes.status)"
Write-Host "Message: $($resetRes.message)"
Write-Host "Duration: $($resetRes.duration_seconds)s"

