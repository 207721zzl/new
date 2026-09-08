param([switch]$Build)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

if ([System.IO.Path]::GetPathRoot($projectRoot) -ne 'D:\') {
    throw "Project directory must be located on D: but resolved to $projectRoot"
}

# The LAN launcher also verifies that Docker Desktop's data disk and both model
# directories are on D:, and creates the ASCII Compose junction when required.
& (Join-Path $PSScriptRoot 'start_lan_microservices.ps1') -ValidateOnly

$composeProjectRoot = if ($projectRoot -match '[^\x00-\x7F]') {
    'D:\EvidenceRAG-LAN'
} else {
    $projectRoot
}
$runtimeRoot = Join-Path $projectRoot 'volumes\public-tunnel'
$runtimeTemp = Join-Path $runtimeRoot 'tmp'
New-Item -ItemType Directory -Force -Path $runtimeRoot, $runtimeTemp | Out-Null
$env:TEMP = $runtimeTemp
$env:TMP = $runtimeTemp

$composeArgs = @(
    'compose',
    '--profile', 'public',
    '--env-file', '.env',
    '--env-file', '.env.microservices',
    '-f', 'deploy/compose.yml',
    '-f', 'deploy/compose.public.yml'
)

Push-Location $composeProjectRoot
try {
    & docker @composeArgs config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw 'Public Compose configuration validation failed.'
    }
    if ($Build) {
        & docker @composeArgs up -d --build
    } else {
        & docker @composeArgs up -d
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'Public tunnel startup failed.'
    }

    $publicUrl = $null
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline -and -not $publicUrl) {
        $logs = (& docker @composeArgs logs --no-color --tail 120 public-tunnel 2>&1) |
            Out-String
        $match = [regex]::Match(
            $logs,
            'https://[a-z0-9-]+\.trycloudflare\.com',
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
        if ($match.Success) {
            $publicUrl = $match.Value
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $publicUrl) {
        throw 'The public tunnel did not publish an HTTPS address within 45 seconds.'
    }

    $health = Invoke-RestMethod -Uri "$publicUrl/api/v1/health" -TimeoutSec 30
    if ($health.status -ne 'ok' -or $health.service -ne 'gateway') {
        throw 'The public gateway health check failed.'
    }
    Set-Content -LiteralPath (Join-Path $runtimeRoot 'public-url.txt') `
        -Value $publicUrl -Encoding utf8
    Write-Host "Public URL: $publicUrl"
    Write-Host 'Public gateway health check: OK'
} finally {
    Pop-Location
}
