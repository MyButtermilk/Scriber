<#
.SYNOPSIS
Joins the background installed-installer smoke started before draft sync, or
runs the smoke synchronously on the downloaded candidate if no background run
exists.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,
    [Parameter(Mandatory = $true)]
    [string]$DownloadedInstallerPath,
    [string]$OutputPath = "tmp\installer-smoke\downloaded-release-candidate.json",
    [string]$StatePath = "tmp\installer-smoke\background-smoke-state.json",
    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedPython = (Resolve-Path -LiteralPath $PythonExecutable -ErrorAction Stop).Path
$resolvedDownloaded = (Resolve-Path -LiteralPath $DownloadedInstallerPath -ErrorAction Stop).Path
$fullOutputPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputPath))
$fullStatePath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $StatePath))

if (Test-Path -LiteralPath $fullStatePath -PathType Leaf) {
    $state = Get-Content -LiteralPath $fullStatePath -Raw | ConvertFrom-Json
    $pidValue = [int]$state.pid
    Write-Host "Joining background installer smoke (PID $pidValue)..."
    $process = $null
    try {
        $process = [System.Diagnostics.Process]::GetProcessById($pidValue)
    } catch {
        $process = $null
    }
    if ($null -ne $process) {
        try {
            if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
                try { $process.Kill($true) } catch {}
                throw "Background installer smoke (PID $pidValue) exceeded timeout of $TimeoutSeconds seconds."
            }
            $exitCode = $process.ExitCode
        } finally {
            $process.Dispose()
        }
    } else {
        $exitCode = if (Test-Path -LiteralPath $fullOutputPath -PathType Leaf) { 0 } else { 1 }
    }

    foreach ($logField in @("stdoutLog", "stderrLog")) {
        $logPath = [string]$state.$logField
        if ($logPath -and (Test-Path -LiteralPath $logPath -PathType Leaf)) {
            $content = Get-Content -LiteralPath $logPath -Raw
            if ($content) {
                Write-Host $content.TrimEnd()
            }
        }
    }

    if ($exitCode -ne 0) {
        throw "Background installer smoke failed with exit code $exitCode."
    }
    if (-not (Test-Path -LiteralPath $fullOutputPath -PathType Leaf)) {
        throw "Background installer smoke completed without writing report: $fullOutputPath"
    }
    return
}

Write-Host "No background installer smoke state found; running installed smoke synchronously on downloaded candidate."
& (Join-Path $repoRoot "scripts\smoke_windows_installer.ps1") `
    -PythonExecutable $resolvedPython `
    -InstallerPath $resolvedDownloaded `
    -OutputPath $fullOutputPath `
    -SimulateUpgrade `
    -VerifyFrontend `
    -VerifyMediaPreparation `
    -VerifySupportBundle `
    -VerifyUninstall
if ($LASTEXITCODE -ne 0) {
    throw "Synchronous downloaded installer smoke failed with exit code $LASTEXITCODE."
}
