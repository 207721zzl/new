param(
    [switch]$Build,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

function Assert-OnDDrive {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if ([System.IO.Path]::GetPathRoot($resolved) -ne 'D:\') {
        throw "$Label must be located on D: but resolved to $resolved"
    }
    return $resolved
}

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $escapedName = [regex]::Escape($Name)
    $line = Get-Content -LiteralPath $Path |
        Where-Object { $_ -match "^$escapedName=" } |
        Select-Object -Last 1
    if (-not $line) {
        throw "$Name is missing from $Path"
    }
    return ($line -split '=', 2)[1].Trim()
}

Assert-OnDDrive -Path $projectRoot -Label 'Project directory' | Out-Null

$composeProjectRoot = $projectRoot
if ($projectRoot -match '[^\x00-\x7F]') {
    # BuildKit rejects non-ASCII session-header values on some Docker Desktop
    # releases. A junction keeps every byte on D: while giving Compose a stable
    # ASCII build-context path.
    $asciiProjectRoot = 'D:\EvidenceRAG-LAN'
    if (Test-Path -LiteralPath $asciiProjectRoot) {
        $item = Get-Item -LiteralPath $asciiProjectRoot -Force
        $targets = @($item.Target) | ForEach-Object {
            [System.IO.Path]::GetFullPath([string]$_).TrimEnd('\')
        }
        if ($item.LinkType -ne 'Junction' -or
            $targets -notcontains $projectRoot.TrimEnd('\')) {
            throw "$asciiProjectRoot already exists and is not a junction to $projectRoot"
        }
    } else {
        New-Item -ItemType Junction -Path $asciiProjectRoot -Target $projectRoot |
            Out-Null
    }
    $composeProjectRoot = $asciiProjectRoot
}

$runtimeRoot = Join-Path $projectRoot 'volumes\microservices-runtime'
$runtimeTemp = Join-Path $runtimeRoot 'tmp'
New-Item -ItemType Directory -Force -Path $runtimeTemp | Out-Null
$env:TEMP = $runtimeTemp
$env:TMP = $runtimeTemp
# Docker Compose Bake currently cannot encode this workspace's non-ASCII path in
# its BuildKit session header. The regular Compose builder handles the path.
$env:COMPOSE_BAKE = 'false'

$dockerSettingsPath = Join-Path $env:APPDATA 'Docker\settings-store.json'
if (-not (Test-Path -LiteralPath $dockerSettingsPath)) {
    throw 'Docker Desktop settings were not found; cannot prove its data disk is on D:.'
}
$dockerSettings = Get-Content -Raw -LiteralPath $dockerSettingsPath | ConvertFrom-Json
$dockerDataRoot = [string]$dockerSettings.CustomWslDistroDir
if (-not $dockerDataRoot) {
    throw 'Docker Desktop CustomWslDistroDir is not configured; move the Docker data disk to D: first.'
}
Assert-OnDDrive -Path $dockerDataRoot -Label 'Docker Desktop data directory' | Out-Null
$dockerDataDisk = Join-Path $dockerDataRoot 'disk\docker_data.vhdx'
Assert-OnDDrive -Path $dockerDataDisk -Label 'Docker Desktop data disk' | Out-Null

$projectEnv = Join-Path $projectRoot '.env'
foreach ($entry in @(
    @{ Name = 'BGE_M3_HOST_PATH'; Label = 'Embedding model directory' },
    @{ Name = 'RERANKER_HOST_PATH'; Label = 'Reranker model directory' }
)) {
    $modelPath = Get-DotEnvValue -Path $projectEnv -Name $entry.Name
    Assert-OnDDrive -Path $modelPath -Label $entry.Label | Out-Null
}

$env:MICRO_BIND_HOST = '0.0.0.0'
if ($ValidateOnly) {
    return
}
Push-Location $composeProjectRoot
try {
    & (Join-Path $composeProjectRoot 'scripts\start_microservices.ps1') -Build:$Build
    if ($LASTEXITCODE -ne 0) {
        throw 'LAN microservice startup failed.'
    }
} finally {
    Pop-Location
}
