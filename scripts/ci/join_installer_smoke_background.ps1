<#
.SYNOPSIS
Joins the background installed-installer smoke started inside the draft-sync
step, or verifies its recorded completion / falls back to running the smoke
synchronously on the downloaded candidate.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,
    [string]$DownloadedInstallerPath = "",
    [string]$OutputPath = "tmp\installer-smoke\downloaded-release-candidate.json",
    [string]$StatePath = "tmp\installer-smoke\background-smoke-state.json",
    [int]$TimeoutSeconds = 600,
    [switch]$JoinOnly,
    [switch]$DeferFailure,
    [switch]$AbortIfRunning
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedPython = (Resolve-Path -LiteralPath $PythonExecutable -ErrorAction Stop).Path
$fullOutputPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputPath))
$fullStatePath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $StatePath))

function Write-SmokeLogs {
    param([object]$State)
    foreach ($logField in @("stdoutLog", "stderrLog")) {
        $logPath = [string]$State.$logField
        if ($logPath -and (Test-Path -LiteralPath $logPath -PathType Leaf)) {
            $content = Get-Content -LiteralPath $logPath -Raw
            if ($content) {
                Write-Host $content.TrimEnd()
            }
        }
    }
}

if (Test-Path -LiteralPath $fullStatePath -PathType Leaf) {
    $state = Get-Content -LiteralPath $fullStatePath -Raw | ConvertFrom-Json
    $alreadyCompleted = [bool]($state.PSObject.Properties["completed"] -and $state.completed)

    if ($AbortIfRunning) {
        if (-not $alreadyCompleted) {
            $pidValue = [int]$state.pid
            try {
                $process = [System.Diagnostics.Process]::GetProcessById($pidValue)
                try {
                    if (-not $process.HasExited) {
                        $process.Kill($true)
                    }
                } finally {
                    $process.Dispose()
                }
            } catch {
            }
        }
        return
    }

    if ($alreadyCompleted) {
        $exitCode = [int]$state.exitCode
        Write-Host "Using completed background installer smoke result (exit code $exitCode)."
        Write-SmokeLogs -State $state
        if ($exitCode -ne 0) {
            if ($DeferFailure) { return }
            throw "Background installer smoke failed with exit code $exitCode."
        }
        if (-not (Test-Path -LiteralPath $fullOutputPath -PathType Leaf)) {
            if ($DeferFailure) { return }
            throw "Background installer smoke completed without writing report: $fullOutputPath"
        }
        return
    }

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

    $updatedState = [ordered]@{
        schemaVersion = [int]$state.schemaVersion
        pid = $pidValue
        startedAtUtc = [string]$state.startedAtUtc
        completedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
        completed = $true
        exitCode = [int]$exitCode
        installerPath = [string]$state.installerPath
        installerSha256 = [string]$state.installerSha256
        outputPath = [string]$state.outputPath
        stdoutLog = [string]$state.stdoutLog
        stderrLog = [string]$state.stderrLog
    }
    $updatedState | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $fullStatePath -Encoding utf8

    if (-not $JoinOnly) {
        Write-SmokeLogs -State $state
    } else {
        Write-Host "Background installer smoke exited with code $exitCode."
    }

    if ($exitCode -ne 0) {
        if ($DeferFailure) { return }
        throw "Background installer smoke failed with exit code $exitCode."
    }
    if (-not (Test-Path -LiteralPath $fullOutputPath -PathType Leaf)) {
        if ($DeferFailure) { return }
        throw "Background installer smoke completed without writing report: $fullOutputPath"
    }
    return
}

if ($AbortIfRunning -or $JoinOnly) {
    return
}

if ([string]::IsNullOrWhiteSpace($DownloadedInstallerPath)) {
    throw "DownloadedInstallerPath is required when running installer smoke synchronously."
}
$resolvedDownloaded = (Resolve-Path -LiteralPath $DownloadedInstallerPath -ErrorAction Stop).Path

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
