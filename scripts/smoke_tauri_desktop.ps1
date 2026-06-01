<#
.SYNOPSIS
Runs a Windows smoke test for the hybrid Tauri desktop runtime.

.DESCRIPTION
The script starts the release Tauri executable, waits for a newly managed
Python web API process, verifies the Scriber health contract, hard-stops the
Tauri process, and checks that the managed backend process exits with it.

Build the executable first with:
  cd Frontend
  npm run tauri:build
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$ExePath = "",
    [string]$PythonPath = "",
    [int]$TimeoutSec = 60,
    [int]$BackendHealthTimeoutSec = 20,
    [switch]$KeepAppOpen,
    [switch]$EnableHotkeys,
    [switch]$EnableDeviceMonitor
)

$ErrorActionPreference = "Stop"

function Get-WebApiProcesses {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match "python.*-m\s+src\.web_api" }
}

function Resolve-PythonPath {
    param([string]$Root, [string]$Requested)

    if ($Requested -and (Test-Path $Requested)) {
        return (Resolve-Path $Requested).Path
    }

    $candidates = @(
        (Join-Path $Root "venv\Scripts\python.exe"),
        (Join-Path $Root ".venv\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    return "python"
}

function Wait-NewBackendListener {
    param(
        [int[]]$BaselinePids,
        [int]$DeadlineSec
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $newProcesses = @(
            Get-WebApiProcesses |
                Where-Object { $BaselinePids -notcontains [int]$_.ProcessId }
        )
        foreach ($process in $newProcesses) {
            $ports = @(
                Get-NetTCPConnection -State Listen -OwningProcess ([int]$process.ProcessId) -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty LocalPort -Unique
            )
            if ($ports.Count -gt 0) {
                return [pscustomobject]@{
                    BackendPid = [int]$process.ProcessId
                    Port = [int]($ports | Select-Object -First 1)
                }
            }
        }
    }
    throw "No managed backend listener appeared within ${DeadlineSec}s."
}

function Wait-BackendHealth {
    param(
        [int]$Port,
        [int]$DeadlineSec
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            if ($health.ok -and $health.runtimeMode -eq "tauri-supervised" -and $health.apiVersion) {
                return $health
            }
            Start-Sleep -Milliseconds 500
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "Managed backend on port $Port did not return tauri-supervised health."
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
if (-not $ExePath) {
    $ExePath = Join-Path $RepoRoot "Frontend\src-tauri\target\release\scriber-desktop.exe"
}
if (-not (Test-Path $ExePath)) {
    throw "Missing Tauri executable: $ExePath. Run 'cd Frontend; npm run tauri:build' first."
}
$ExePath = (Resolve-Path $ExePath).Path
$PythonPath = Resolve-PythonPath -Root $RepoRoot -Requested $PythonPath

$baseline = @(Get-WebApiProcesses | ForEach-Object { [int]$_.ProcessId })
$oldRoot = $env:SCRIBER_REPO_ROOT
$oldPython = $env:SCRIBER_PYTHON
$oldHotkeys = $env:SCRIBER_DISABLE_HOTKEYS
$oldMonitor = $env:SCRIBER_DISABLE_DEVICE_MONITOR

$env:SCRIBER_REPO_ROOT = $RepoRoot
$env:SCRIBER_PYTHON = $PythonPath
if (-not $EnableHotkeys) {
    $env:SCRIBER_DISABLE_HOTKEYS = "1"
}
if (-not $EnableDeviceMonitor) {
    $env:SCRIBER_DISABLE_DEVICE_MONITOR = "1"
}

$app = $null
$result = $null
try {
    $app = Start-Process -FilePath $ExePath -WorkingDirectory (Split-Path $ExePath) -WindowStyle Hidden -PassThru
    $listener = Wait-NewBackendListener -BaselinePids $baseline -DeadlineSec $TimeoutSec
    if ($app.HasExited) {
        throw "Tauri process exited early with code $($app.ExitCode)."
    }
    $health = Wait-BackendHealth -Port $listener.Port -DeadlineSec $BackendHealthTimeoutSec
    $result = [pscustomobject]@{
        ok = $true
        appPid = $app.Id
        backendPid = $listener.BackendPid
        backendPort = $listener.Port
        runtimeMode = $health.runtimeMode
        apiVersion = $health.apiVersion
        ready = $health.ready
        cleanupVerified = $false
    }
} finally {
    if (-not $KeepAppOpen -and $app -and -not $app.HasExited) {
        Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $app.Id -Timeout 10 -ErrorAction SilentlyContinue
    }

    if (-not $KeepAppOpen) {
        Start-Sleep -Seconds 3
        $remaining = @(
            Get-WebApiProcesses |
                Where-Object { $baseline -notcontains [int]$_.ProcessId } |
                ForEach-Object { [int]$_.ProcessId }
        )
        if ($remaining.Count -gt 0) {
            foreach ($processId in $remaining) {
                Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            }
            throw "Managed backend process remained after Tauri exit: $($remaining -join ', ')"
        }
        if ($result) {
            $result.cleanupVerified = $true
        }
    }

    $env:SCRIBER_REPO_ROOT = $oldRoot
    $env:SCRIBER_PYTHON = $oldPython
    $env:SCRIBER_DISABLE_HOTKEYS = $oldHotkeys
    $env:SCRIBER_DISABLE_DEVICE_MONITOR = $oldMonitor
}

$result | ConvertTo-Json -Compress
