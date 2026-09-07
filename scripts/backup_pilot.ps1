param(
    [string]$Destination = "backups",
    [switch]$IncludeVectorVolumes
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$destinationRoot = if ([System.IO.Path]::IsPathRooted($Destination)) {
    [System.IO.Path]::GetFullPath($Destination)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Destination))
}
if ($destinationRoot -eq $projectRoot) {
    throw "Backup destination must not be the project root."
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupDirectory = Join-Path $destinationRoot $timestamp
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null

Push-Location $projectRoot
try {
    $databaseDump = Join-Path $backupDirectory "mysql.sql"
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    $dumpWriter = New-Object System.IO.StreamWriter($databaseDump, $false, $utf8WithoutBom)
    try {
        docker compose exec -T mysql sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --events --default-character-set=utf8mb4 "$MYSQL_DATABASE"' |
            ForEach-Object { $dumpWriter.WriteLine($_) }
        if ($LASTEXITCODE -ne 0) { throw "MySQL backup failed." }
    } finally {
        $dumpWriter.Dispose()
    }

    $uploads = Join-Path $projectRoot "data\knowledge\uploads"
    if (Test-Path -LiteralPath $uploads) {
        Compress-Archive -LiteralPath $uploads -DestinationPath (Join-Path $backupDirectory "uploads.zip") -CompressionLevel Optimal
    }

    if ($IncludeVectorVolumes) {
        docker compose stop api standalone etcd minio
        try {
            foreach ($name in @("milvus", "etcd", "minio")) {
                $source = Join-Path $projectRoot "volumes\$name"
                if (Test-Path -LiteralPath $source) {
                    Compress-Archive -LiteralPath $source -DestinationPath (Join-Path $backupDirectory "$name.zip") -CompressionLevel Optimal
                }
            }
        } finally {
            docker compose up -d
        }
    }

    $files = Get-ChildItem -LiteralPath $backupDirectory -File
    $manifest = [ordered]@{
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        includes_vector_volumes = [bool]$IncludeVectorVolumes
        files = @($files | ForEach-Object {
            [ordered]@{
                name = $_.Name
                bytes = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        })
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText(
        (Join-Path $backupDirectory "manifest.json"),
        $manifestJson,
        $utf8WithoutBom
    )
    Write-Output $backupDirectory
} finally {
    Pop-Location
}
