param()

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "autoresearch.ps1") -Suite Smoke
exit $LASTEXITCODE
