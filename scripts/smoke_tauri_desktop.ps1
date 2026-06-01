<#
.SYNOPSIS
Runs a Windows smoke test for the hybrid Tauri desktop runtime.

.DESCRIPTION
The script starts the release Tauri executable, waits for a newly managed
backend process, verifies the Scriber health contract, hard-stops the Tauri
process, and checks that the managed backend process exits with it.

Build the executable first with:
  cd Frontend
  npm run tauri:build
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$ExePath = "",
    [string]$PythonPath = "",
    [string]$BackendExePath = "",
    [string]$DataDir = "",
    [int]$TimeoutSec = 60,
    [int]$BackendHealthTimeoutSec = 20,
    [switch]$KeepAppOpen,
    [switch]$EnableHotkeys,
    [switch]$EnableDeviceMonitor
)

$ErrorActionPreference = "Stop"

function Get-ManagedBackendProcesses {
    Get-CimInstance Win32_Process |
        Where-Object {
            ($_.CommandLine -match "python.*-m\s+src\.web_api") -or
            ($_.CommandLine -match "scriber-backend") -or
            ($_.Name -match "^scriber-backend")
        }
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
            Get-ManagedBackendProcesses |
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

function Get-BackendRuntime {
    param([int]$Port)

    $runtime = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/runtime" -TimeoutSec 5
    if (-not $runtime.dataDir) {
        throw "Managed backend runtime did not report dataDir."
    }
    if (-not $runtime.downloadsDir) {
        throw "Managed backend runtime did not report downloadsDir."
    }
    return $runtime
}

function Convert-ToFullPath {
    param([string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
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
if ($BackendExePath) {
    if (-not (Test-Path $BackendExePath)) {
        throw "Missing backend sidecar executable: $BackendExePath"
    }
    $BackendExePath = (Resolve-Path $BackendExePath).Path
}
if (-not $DataDir) {
    $DataDir = Join-Path $RepoRoot ("tmp\tauri-smoke-data\" + [System.Guid]::NewGuid().ToString("N"))
}
$DataDir = Convert-ToFullPath -Path $DataDir
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

$baseline = @(Get-ManagedBackendProcesses | ForEach-Object { [int]$_.ProcessId })
$oldRoot = $env:SCRIBER_REPO_ROOT
$oldPython = $env:SCRIBER_PYTHON
$oldBackendExe = $env:SCRIBER_BACKEND_EXE
$oldDataDir = $env:SCRIBER_DATA_DIR
$oldForceManaged = $env:SCRIBER_FORCE_MANAGED_BACKEND
$oldHotkeys = $env:SCRIBER_DISABLE_HOTKEYS
$oldMonitor = $env:SCRIBER_DISABLE_DEVICE_MONITOR

$env:SCRIBER_REPO_ROOT = $RepoRoot
$env:SCRIBER_PYTHON = $PythonPath
$env:SCRIBER_DATA_DIR = $DataDir
$env:SCRIBER_FORCE_MANAGED_BACKEND = "1"
if ($BackendExePath) {
    $env:SCRIBER_BACKEND_EXE = $BackendExePath
} else {
    $env:SCRIBER_BACKEND_EXE = $oldBackendExe
}
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
    $runtime = Get-BackendRuntime -Port $listener.Port
    if ((Convert-ToFullPath -Path $runtime.dataDir) -ne $DataDir) {
        throw "Managed backend used unexpected dataDir: $($runtime.dataDir)"
    }
    if (-not (Convert-ToFullPath -Path $runtime.downloadsDir).StartsWith($DataDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Managed backend downloadsDir is not under dataDir: $($runtime.downloadsDir)"
    }
    $result = [pscustomobject]@{
        ok = $true
        appPid = $app.Id
        backendPid = $listener.BackendPid
        backendPort = $listener.Port
        runtimeMode = $health.runtimeMode
        apiVersion = $health.apiVersion
        ready = $health.ready
        dataDir = $runtime.dataDir
        downloadsDir = $runtime.downloadsDir
        launchKind = $runtime.launchKind
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
            Get-ManagedBackendProcesses |
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
    $env:SCRIBER_BACKEND_EXE = $oldBackendExe
    $env:SCRIBER_DATA_DIR = $oldDataDir
    $env:SCRIBER_FORCE_MANAGED_BACKEND = $oldForceManaged
    $env:SCRIBER_DISABLE_HOTKEYS = $oldHotkeys
    $env:SCRIBER_DISABLE_DEVICE_MONITOR = $oldMonitor
}

$result | ConvertTo-Json -Compress
