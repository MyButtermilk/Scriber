<#
.SYNOPSIS
Starts the official installed-installer smoke check in a background PowerShell
process so it overlaps GitHub draft release asset upload and roundtrip sync.

.DESCRIPTION
Runs the checked-in `scripts\smoke_windows_installer.ps1` with fixed typed
parameters against the freshly built installer under `release-artifacts`.
Before publication, `scripts\ci\join_installer_smoke_background.ps1` waits for
this process and `scripts\ci\validate_installer_smoke_report.py` binds the
smoked installer's SHA-256 cryptographically to the downloaded draft candidate.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,
    [string]$ArtifactsDir = "release-artifacts",
    [string]$OutputPath = "tmp\installer-smoke\downloaded-release-candidate.json",
    [string]$StatePath = "tmp\installer-smoke\background-smoke-state.json"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedPython = (Resolve-Path -LiteralPath $PythonExecutable -ErrorAction Stop).Path
& $resolvedPython -c "import aiohttp"
if ($LASTEXITCODE -ne 0) {
    throw "The exact release Python environment cannot import aiohttp."
}

$resolvedArtifacts = (Resolve-Path -LiteralPath (Join-Path $repoRoot $ArtifactsDir) -ErrorAction Stop).Path
$metadataPath = Join-Path $resolvedArtifacts "latest.json"
if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
    throw "Missing release metadata for background installer smoke: $metadataPath"
}
$metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
$installers = @(
    $metadata.artifacts |
        Where-Object { ([string]$_.name).EndsWith(".exe", [StringComparison]::OrdinalIgnoreCase) }
)
if ($installers.Count -ne 1) {
    throw "Expected exactly one built Windows installer candidate, found $($installers.Count)."
}
$installerPath = (Resolve-Path -LiteralPath (Join-Path $resolvedArtifacts ([string]$installers[0].name)) -ErrorAction Stop).Path
$installerSha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()

$fullOutputPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputPath))
$fullStatePath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $StatePath))
$stateDir = Split-Path -Parent $fullStatePath
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $fullOutputPath) | Out-Null

if (Test-Path -LiteralPath $fullOutputPath) {
    Remove-Item -LiteralPath $fullOutputPath -Force
}

$stdoutLog = Join-Path $stateDir "background-smoke.stdout.log"
$stderrLog = Join-Path $stateDir "background-smoke.stderr.log"
foreach ($log in @($stdoutLog, $stderrLog)) {
    if (Test-Path -LiteralPath $log) {
        Remove-Item -LiteralPath $log -Force
    }
}

$smokeScript = Join-Path $repoRoot "scripts\smoke_windows_installer.ps1"
$pwshExe = (Get-Command pwsh -ErrorAction Stop).Source
$argumentList = @(
    "-NoProfile",
    "-File", $smokeScript,
    "-PythonExecutable", $resolvedPython,
    "-InstallerPath", $installerPath,
    "-OutputPath", $fullOutputPath,
    "-SimulateUpgrade",
    "-VerifyFrontend",
    "-VerifyMediaPreparation",
    "-VerifySupportBundle",
    "-VerifyUninstall"
)

$process = Start-Process `
    -FilePath $pwshExe `
    -ArgumentList $argumentList `
    -WorkingDirectory $repoRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -NoNewWindow `
    -PassThru

$state = [ordered]@{
    schemaVersion = 1
    pid = [int]$process.Id
    startedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    installerPath = $installerPath
    installerSha256 = $installerSha256
    outputPath = $fullOutputPath
    stdoutLog = $stdoutLog
    stderrLog = $stderrLog
}
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $fullStatePath -Encoding utf8
Write-Host "Started background installer smoke (PID $($process.Id)) for $($installers[0].name) [sha256:$($installerSha256.Substring(0, 12))]."
