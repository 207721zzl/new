param([switch]$Build)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Push-Location $projectRoot
try {
    & ./.venv/Scripts/python.exe -m scripts.init_microservices
    if ($LASTEXITCODE -ne 0) { throw 'Failed to initialize local settings.' }
    $composeArgs = @('compose', '--env-file', '.env', '--env-file', '.env.microservices', '-f', 'deploy/compose.yml')
    & docker @composeArgs config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Compose configuration validation failed.' }
    if ($Build) { & docker @composeArgs up -d --build }
    else { & docker @composeArgs up -d }
    if ($LASTEXITCODE -ne 0) { throw 'Service startup failed; original deployment has not been changed.' }
    & docker @composeArgs ps
} finally {
    Pop-Location
}
