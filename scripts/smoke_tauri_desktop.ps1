<#
.SYNOPSIS
Runs a Windows smoke test for the hybrid Tauri desktop runtime.

.DESCRIPTION
The script starts the release Tauri executable, waits for a newly managed
backend process, verifies the Scriber health contract, hard-stops the Tauri
process, and checks that the managed backend process exits with it. With
-SimulateBackendCrash, it also kills the managed worker, waits for the
desktop frontend/supervisor recovery path to start a replacement, and verifies
crash metadata was written. With -OccupyDefaultPort, it binds the default
backend port before launch and verifies that the supervisor selects a different
loopback port. With -SimulateBackendShutdown, it posts the token-protected
runtime shutdown endpoint, waits for the worker to exit cleanly, and verifies
supervisor recovery. With -AttachExternalBackend, it starts an external Python
backend on the default port, starts Tauri without force-managed mode, and
verifies that no managed sidecar is spawned. With -SimulateBackendStartupTimeout,
it forces the first backend worker launch to block before readiness and verifies
that the supervisor replaces it. With -LegacyDataDir and -VerifyLegacyDataMigration,
it verifies first-run migration into SCRIBER_DATA_DIR without printing secret
values. With -StabilityDurationSec, it keeps the app running for repeated
health/state probes and verifies that the backend process remains stable.

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
    [string]$SessionToken = "",
    [int]$TimeoutSec = 60,
    [int]$BackendHealthTimeoutSec = 20,
    [int]$CleanupTimeoutSec = 20,
    [int]$StabilityDurationSec = 0,
    [int]$StabilityProbeIntervalSec = 5,
    [switch]$KeepAppOpen,
    [switch]$EnableHotkeys,
    [switch]$EnableDeviceMonitor,
    [switch]$OccupyDefaultPort,
    [switch]$SimulateBackendCrash,
    [switch]$SimulateBackendShutdown,
    [switch]$AttachExternalBackend,
    [switch]$SimulateBackendStartupTimeout,
    [int]$BackendStartupTimeoutMs = 3000,
    [int]$CrashRecoveryTimeoutSec = 75,
    [string]$LegacyDataDir = "",
    [switch]$VerifyLegacyDataMigration,
    [switch]$DisableDevFallback
)

$ErrorActionPreference = "Stop"
$DefaultBackendPort = 8765

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

function Wait-NewBackendProcess {
    param(
        [int[]]$BaselinePids,
        [int]$DeadlineSec
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $newProcess = Get-ManagedBackendProcesses |
            Where-Object { $BaselinePids -notcontains [int]$_.ProcessId } |
            Select-Object -First 1
        if ($newProcess) {
            return [pscustomobject]@{
                BackendPid = [int]$newProcess.ProcessId
                Name = $newProcess.Name
            }
        }
    }
    throw "No managed backend process appeared within ${DeadlineSec}s."
}

function Wait-BackendHealth {
    param(
        [int]$Port,
        [int]$DeadlineSec,
        [string]$ExpectedRuntimeMode = "tauri-supervised"
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            if ($health.ok -and $health.runtimeMode -eq $ExpectedRuntimeMode -and $health.apiVersion) {
                return $health
            }
            Start-Sleep -Milliseconds 500
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "Backend on port $Port did not return $ExpectedRuntimeMode health."
}

function Get-BackendRuntime {
    param(
        [int]$Port,
        [string]$Token
    )

    $headers = @{}
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }
    $runtime = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/runtime" -Headers $headers -TimeoutSec 5
    if (-not $runtime.dataDir) {
        throw "Managed backend runtime did not report dataDir."
    }
    if (-not $runtime.downloadsDir) {
        throw "Managed backend runtime did not report downloadsDir."
    }
    return $runtime
}

function Invoke-BackendShutdown {
    param(
        [int]$Port,
        [string]$Token
    )

    $headers = @{}
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }
    $response = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/runtime/shutdown" -Headers $headers -TimeoutSec 5
    if (-not $response.ok) {
        throw "Runtime shutdown endpoint on port $Port did not return ok=true."
    }
    return $response
}

function Invoke-TimedRestGet {
    param(
        [string]$Uri,
        [hashtable]$Headers = @{}
    )

    $started = [System.Diagnostics.Stopwatch]::StartNew()
    $payload = Invoke-RestMethod -Uri $Uri -Headers $Headers -TimeoutSec 5
    $started.Stop()
    return [pscustomobject]@{
        payload = $payload
        elapsedMs = [Math]::Round($started.Elapsed.TotalMilliseconds, 2)
    }
}

function Test-RuntimeStability {
    param(
        [System.Diagnostics.Process]$AppProcess,
        [int]$BackendPid,
        [int]$Port,
        [string]$Token,
        [string]$ExpectedRuntimeMode,
        [int]$DurationSec,
        [int]$ProbeIntervalSec
    )

    if ($DurationSec -le 0) {
        return $null
    }

    $headers = @{}
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }

    $samples = @()
    $startedAt = Get-Date
    $deadline = $startedAt.AddSeconds($DurationSec)
    do {
        if ($AppProcess.HasExited) {
            throw "Tauri process exited during stability smoke with code $($AppProcess.ExitCode)."
        }
        $backendProcess = Get-Process -Id $BackendPid -ErrorAction SilentlyContinue
        if (-not $backendProcess) {
            throw "Backend process $BackendPid exited during stability smoke."
        }

        $healthProbe = Invoke-TimedRestGet -Uri "http://127.0.0.1:$Port/api/health"
        $health = $healthProbe.payload
        if (-not ($health.ok -and $health.runtimeMode -eq $ExpectedRuntimeMode -and $health.apiVersion)) {
            throw "Stability smoke health probe returned unexpected payload."
        }
        if ($health.pid -and [int]$health.pid -ne $BackendPid) {
            throw "Stability smoke backend pid changed from $BackendPid to $($health.pid)."
        }

        $stateProbe = Invoke-TimedRestGet -Uri "http://127.0.0.1:$Port/api/state" -Headers $headers
        $state = $stateProbe.payload
        if (-not ($state.recordingState -and $state.status)) {
            throw "Stability smoke state probe returned unexpected payload."
        }

        $processSnapshot = Get-Process -Id $BackendPid -ErrorAction Stop
        $samples += [pscustomobject]@{
            index = $samples.Count + 1
            elapsedSec = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 2)
            backendPid = $BackendPid
            backendWorkingSetMb = [Math]::Round($processSnapshot.WorkingSet64 / 1MB, 2)
            healthMs = $healthProbe.elapsedMs
            stateMs = $stateProbe.elapsedMs
            healthReady = [bool]$health.ready
            recordingState = [string]$state.recordingState
        }

        if ((Get-Date) -ge $deadline) {
            break
        }
        Start-Sleep -Seconds ([Math]::Max(1, $ProbeIntervalSec))
    } while ((Get-Date) -lt $deadline)

    $workingSetValues = @($samples | ForEach-Object { [double]$_.backendWorkingSetMb })
    return [pscustomobject]@{
        verified = $true
        durationSec = $DurationSec
        probeIntervalSec = [Math]::Max(1, $ProbeIntervalSec)
        sampleCount = $samples.Count
        backendPid = $BackendPid
        backendWorkingSetStartMb = if ($workingSetValues.Count) { $workingSetValues[0] } else { $null }
        backendWorkingSetEndMb = if ($workingSetValues.Count) { $workingSetValues[-1] } else { $null }
        backendWorkingSetMaxMb = if ($workingSetValues.Count) { ($workingSetValues | Measure-Object -Maximum).Maximum } else { $null }
        samples = $samples
    }
}

function Convert-ToFullPath {
    param([string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Wait-ProcessExit {
    param(
        [int]$ProcessId,
        [int]$DeadlineSec
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $process) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Process $ProcessId did not exit within ${DeadlineSec}s."
}

function Wait-ManagedBackendProcessesExit {
    param(
        [int[]]$BaselinePids,
        [int]$DeadlineSec
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    do {
        $remaining = @(
            Get-ManagedBackendProcesses |
                Where-Object { $BaselinePids -notcontains [int]$_.ProcessId } |
                ForEach-Object { [int]$_.ProcessId }
        )
        if ($remaining.Count -eq 0) {
            return @()
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    return $remaining
}

function Test-LoopbackPortFree {
    param([int]$Port)

    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Parse("127.0.0.1"),
            $Port
        )
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) {
            $listener.Stop()
        }
    }
}

function Get-LoopbackListenerPid {
    param([int]$Port)

    $connection = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @("127.0.0.1", "0.0.0.0", "::1", "::") } |
        Select-Object -First 1
    if (-not $connection) {
        return $null
    }
    return [int]$connection.OwningProcess
}

function Assert-NoNewBackendListeners {
    param(
        [int[]]$BaselinePids,
        [int]$WaitSec
    )

    Start-Sleep -Seconds $WaitSec
    $newProcesses = @(
        Get-ManagedBackendProcesses |
            Where-Object { $BaselinePids -notcontains [int]$_.ProcessId }
    )
    $newListeners = @()
    foreach ($process in $newProcesses) {
        $ports = @(
            Get-NetTCPConnection -State Listen -OwningProcess ([int]$process.ProcessId) -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty LocalPort -Unique
        )
        if ($ports.Count -gt 0) {
            $newListeners += [pscustomobject]@{
                ProcessId = [int]$process.ProcessId
                Name = $process.Name
                Ports = @($ports)
            }
        }
    }
    if ($newListeners.Count -gt 0) {
        $details = @($newListeners | ForEach-Object { "$($_.ProcessId):$($_.Name):$($_.Ports -join ',')" })
        throw "Unexpected managed backend listener appeared: $($details -join '; ')"
    }
}

function Wait-BackendCrashMetadata {
    param(
        [string]$DataDir,
        [int]$BackendPid,
        [int]$DeadlineSec
    )

    $metadataPath = Join-Path $DataDir "logs\backend-crash-metadata.jsonl"
    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $metadataPath) {
            $content = Get-Content -Raw -Path $metadataPath
            if ($content -match '"event"\s*:\s*"managed_backend_exit"' -and $content -match "`"pid`"\s*:\s*$BackendPid") {
                return $metadataPath
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Backend crash metadata for pid $BackendPid was not written under $metadataPath."
}

function Get-FileSha256 {
    param([string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-LegacyDataMigration {
    param(
        [string]$SourceDir,
        [string]$TargetDir
    )

    if (-not $SourceDir) {
        throw "-VerifyLegacyDataMigration requires -LegacyDataDir."
    }
    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
        throw "LegacyDataDir does not exist: $SourceDir"
    }

    $sourceFull = Convert-ToFullPath -Path (Resolve-Path $SourceDir).Path
    $targetFull = Convert-ToFullPath -Path $TargetDir
    $fileResults = @()

    foreach ($name in @(".env", "settings.json", "transcripts.db", "transcripts.db-wal", "transcripts.db-shm")) {
        $sourcePath = Join-Path $sourceFull $name
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            continue
        }

        $targetPath = Join-Path $targetFull $name
        if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
            throw "Legacy migration did not copy $name to $targetFull."
        }

        $sourceItem = Get-Item -LiteralPath $sourcePath
        $targetItem = Get-Item -LiteralPath $targetPath
        if ($sourceItem.Length -gt 0 -and $targetItem.Length -le 0) {
            throw "Legacy migration created an empty target for $name."
        }

        $entry = [ordered]@{
            path = $name
            sourceBytes = [int64]$sourceItem.Length
            targetBytes = [int64]$targetItem.Length
            hashVerified = $false
        }

        if ($name -in @(".env", "settings.json")) {
            $entry.hashVerified = $true
            $entry.hashMatches = (Get-FileSha256 -Path $sourcePath) -eq (Get-FileSha256 -Path $targetPath)
            if (-not $entry.hashMatches) {
                throw "Legacy migration changed $name while copying it."
            }
        }

        $fileResults += [pscustomobject]$entry
    }

    $dirResults = @()
    foreach ($name in @("downloads", "models")) {
        $sourceDirPath = Join-Path $sourceFull $name
        if (-not (Test-Path -LiteralPath $sourceDirPath -PathType Container)) {
            continue
        }

        $targetDirPath = Join-Path $targetFull $name
        $sourceDirFull = Convert-ToFullPath -Path $sourceDirPath
        $sourceFiles = @(Get-ChildItem -LiteralPath $sourceDirFull -Recurse -File)
        $missing = @()
        foreach ($sourceFile in $sourceFiles) {
            $relative = $sourceFile.FullName.Substring($sourceDirFull.Length).TrimStart('\', '/')
            $targetFile = Join-Path $targetDirPath $relative
            if (-not (Test-Path -LiteralPath $targetFile -PathType Leaf)) {
                $missing += $relative
            }
        }
        if ($missing.Count -gt 0) {
            throw "Legacy migration missed $name files: $($missing -join ', ')"
        }

        $dirResults += [pscustomobject]@{
            path = $name
            sourceFiles = [int]$sourceFiles.Count
            verifiedFiles = [int]$sourceFiles.Count
        }
    }

    if ($fileResults.Count -eq 0 -and $dirResults.Count -eq 0) {
        throw "LegacyDataDir did not contain migratable Scriber runtime data: $sourceFull"
    }

    return [pscustomobject]@{
        verified = $true
        source = $sourceFull
        target = $targetFull
        files = $fileResults
        directories = $dirResults
    }
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
if ($LegacyDataDir) {
    if (-not (Test-Path -LiteralPath $LegacyDataDir -PathType Container)) {
        throw "Missing LegacyDataDir: $LegacyDataDir"
    }
    $LegacyDataDir = (Resolve-Path $LegacyDataDir).Path
} elseif ($VerifyLegacyDataMigration) {
    throw "-VerifyLegacyDataMigration requires -LegacyDataDir."
}
if ($AttachExternalBackend -and ($OccupyDefaultPort -or $SimulateBackendCrash -or $SimulateBackendShutdown -or $SimulateBackendStartupTimeout -or $BackendExePath)) {
    throw "-AttachExternalBackend cannot be combined with -OccupyDefaultPort, -SimulateBackendCrash, -SimulateBackendShutdown, -SimulateBackendStartupTimeout, or -BackendExePath."
}
if ($AttachExternalBackend -and $KeepAppOpen) {
    throw "-AttachExternalBackend cannot be combined with -KeepAppOpen because the smoke owns the external backend process."
}
if ($SimulateBackendStartupTimeout -and ($SimulateBackendCrash -or $SimulateBackendShutdown)) {
    throw "-SimulateBackendStartupTimeout cannot be combined with -SimulateBackendCrash or -SimulateBackendShutdown."
}
if (-not $DataDir) {
    $DataDir = Join-Path $RepoRoot ("tmp\tauri-smoke-data\" + [System.Guid]::NewGuid().ToString("N"))
}
$DataDir = Convert-ToFullPath -Path $DataDir
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
if (-not $SessionToken) {
    $SessionToken = [System.Guid]::NewGuid().ToString("N")
}

$baseline = @(Get-ManagedBackendProcesses | ForEach-Object { [int]$_.ProcessId })
$oldRoot = $env:SCRIBER_REPO_ROOT
$oldPython = $env:SCRIBER_PYTHON
$oldBackendExe = $env:SCRIBER_BACKEND_EXE
$oldDataDir = $env:SCRIBER_DATA_DIR
$oldForceManaged = $env:SCRIBER_FORCE_MANAGED_BACKEND
$oldSessionToken = $env:SCRIBER_SESSION_TOKEN
$oldHotkeys = $env:SCRIBER_DISABLE_HOTKEYS
$oldMonitor = $env:SCRIBER_DISABLE_DEVICE_MONITOR
$oldLegacyDataDir = $env:SCRIBER_LEGACY_DATA_DIR
$oldWebHost = $env:SCRIBER_WEB_HOST
$oldWebPort = $env:SCRIBER_WEB_PORT
$oldRuntimeMode = $env:SCRIBER_RUNTIME_MODE
$oldLaunchKind = $env:SCRIBER_BACKEND_LAUNCH_KIND
$oldBackendStartTimeout = $env:SCRIBER_BACKEND_START_TIMEOUT_MS
$oldSimulateStartupTimeout = $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_ONCE
$oldSimulateStartupTimeoutMarker = $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER

if ($DisableDevFallback) {
    $env:SCRIBER_REPO_ROOT = $null
    $env:SCRIBER_PYTHON = $null
} else {
    $env:SCRIBER_REPO_ROOT = $RepoRoot
    $env:SCRIBER_PYTHON = $PythonPath
}
$env:SCRIBER_DATA_DIR = $DataDir
if ($AttachExternalBackend) {
    $env:SCRIBER_FORCE_MANAGED_BACKEND = $null
    $env:SCRIBER_WEB_HOST = "127.0.0.1"
    $env:SCRIBER_WEB_PORT = $DefaultBackendPort.ToString()
    $env:SCRIBER_RUNTIME_MODE = "external-python"
    $env:SCRIBER_BACKEND_LAUNCH_KIND = "external-python"
} else {
    $env:SCRIBER_FORCE_MANAGED_BACKEND = "1"
}
$env:SCRIBER_SESSION_TOKEN = $SessionToken
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
if ($LegacyDataDir) {
    $env:SCRIBER_LEGACY_DATA_DIR = $LegacyDataDir
}
if ($SimulateBackendStartupTimeout) {
    $env:SCRIBER_BACKEND_START_TIMEOUT_MS = $BackendStartupTimeoutMs.ToString()
    $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_ONCE = "1"
    $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER = Join-Path $DataDir "startup-timeout-once.marker"
}

$app = $null
$result = $null
$defaultPortBlocker = $null
$externalBackend = $null
$externalBackendPid = $null
try {
    $backendBaselineForApp = $baseline
    if ($AttachExternalBackend) {
        if (-not (Test-LoopbackPortFree -Port $DefaultBackendPort)) {
            throw "Default backend port $DefaultBackendPort is already occupied; cannot run external attach smoke."
        }
        $externalBackend = Start-Process -FilePath $PythonPath -ArgumentList @("-m", "src.web_api") -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
        Wait-BackendHealth -Port $DefaultBackendPort -DeadlineSec $BackendHealthTimeoutSec -ExpectedRuntimeMode "external-python" | Out-Null
        $externalBackendPid = Get-LoopbackListenerPid -Port $DefaultBackendPort
        if (-not $externalBackendPid) {
            throw "External backend became healthy, but no listener owner was found for port $DefaultBackendPort."
        }
        $backendBaselineForApp = @($baseline + [int]$externalBackend.Id + [int]$externalBackendPid | Select-Object -Unique)
    }

    if ($OccupyDefaultPort) {
        $defaultPortBlocker = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Parse("127.0.0.1"),
            $DefaultBackendPort
        )
        $defaultPortBlocker.Start()
    }

    $app = Start-Process -FilePath $ExePath -WorkingDirectory (Split-Path $ExePath) -WindowStyle Hidden -PassThru
    $startupTimeout = $null
    if ($AttachExternalBackend) {
        Start-Sleep -Seconds 3
        if ($app.HasExited) {
            throw "Tauri process exited early with code $($app.ExitCode)."
        }
        Assert-NoNewBackendListeners -BaselinePids $backendBaselineForApp -WaitSec 3
        $listener = [pscustomobject]@{
            BackendPid = [int]$externalBackendPid
            Port = $DefaultBackendPort
        }
    } else {
        if ($SimulateBackendStartupTimeout) {
            $initialBackend = Wait-NewBackendProcess -BaselinePids $baseline -DeadlineSec $TimeoutSec
            Wait-ProcessExit -ProcessId $initialBackend.BackendPid -DeadlineSec ([Math]::Max(10, [Math]::Ceiling($BackendStartupTimeoutMs / 1000.0) + 10))
            $startupTimeoutBaseline = @($baseline + [int]$initialBackend.BackendPid)
            $listener = Wait-NewBackendListener -BaselinePids $startupTimeoutBaseline -DeadlineSec $CrashRecoveryTimeoutSec
            if ($listener.BackendPid -eq $initialBackend.BackendPid) {
                throw "Backend startup-timeout recovery reused timed-out backend pid $($initialBackend.BackendPid)."
            }
            $startupTimeout = [pscustomobject]@{
                verified = $true
                timedOutBackendPid = [int]$initialBackend.BackendPid
                replacementBackendPid = [int]$listener.BackendPid
                backendStartupTimeoutMs = $BackendStartupTimeoutMs
                markerPath = $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER
            }
        } else {
            $listener = Wait-NewBackendListener -BaselinePids $baseline -DeadlineSec $TimeoutSec
        }
    }
    if ($app.HasExited) {
        throw "Tauri process exited early with code $($app.ExitCode)."
    }
    $expectedRuntimeMode = if ($AttachExternalBackend) { "external-python" } else { "tauri-supervised" }
    $health = Wait-BackendHealth -Port $listener.Port -DeadlineSec $BackendHealthTimeoutSec -ExpectedRuntimeMode $expectedRuntimeMode
    $runtime = Get-BackendRuntime -Port $listener.Port -Token $SessionToken
    if ((Convert-ToFullPath -Path $runtime.dataDir) -ne $DataDir) {
        throw "Managed backend used unexpected dataDir: $($runtime.dataDir)"
    }
    if (-not (Convert-ToFullPath -Path $runtime.downloadsDir).StartsWith($DataDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Managed backend downloadsDir is not under dataDir: $($runtime.downloadsDir)"
    }
    $externalAttach = $null
    if ($AttachExternalBackend) {
        if ($runtime.launchKind -ne "external-python") {
            throw "External attach smoke expected launchKind external-python, got $($runtime.launchKind)."
        }
        $externalAttach = [pscustomobject]@{
            verified = $true
            externalBackendPid = [int]$externalBackendPid
            port = $DefaultBackendPort
            runtimeMode = $health.runtimeMode
            launchKind = $runtime.launchKind
            managedBackendSpawned = $false
        }
    }
    $portConflict = $null
    if ($OccupyDefaultPort) {
        if ([int]$listener.Port -eq $DefaultBackendPort) {
            throw "Managed backend used default port $DefaultBackendPort even though it was occupied."
        }
        $portConflict = [ordered]@{
            verified = $true
            occupiedPort = $DefaultBackendPort
            initialBackendPort = [int]$listener.Port
            recoveredBackendPort = $null
        }
    }
    $legacyDataMigration = $null
    if ($VerifyLegacyDataMigration) {
        $legacyDataMigration = Test-LegacyDataMigration -SourceDir $LegacyDataDir -TargetDir $DataDir
    }
    $crashRecovery = $null
    if ($SimulateBackendCrash) {
        $initialBackendPid = [int]$listener.BackendPid
        $initialPort = [int]$listener.Port
        Stop-Process -Id $initialBackendPid -Force -ErrorAction Stop
        Wait-ProcessExit -ProcessId $initialBackendPid -DeadlineSec 10

        $crashBaseline = @($baseline + $initialBackendPid)
        $replacement = Wait-NewBackendListener -BaselinePids $crashBaseline -DeadlineSec $CrashRecoveryTimeoutSec
        if ($replacement.BackendPid -eq $initialBackendPid) {
            throw "Backend crash recovery reused the killed backend pid $initialBackendPid."
        }
        if ($OccupyDefaultPort -and [int]$replacement.Port -eq $DefaultBackendPort) {
            throw "Recovered backend used default port $DefaultBackendPort even though it was occupied."
        }
        $replacementHealth = Wait-BackendHealth -Port $replacement.Port -DeadlineSec $BackendHealthTimeoutSec
        $replacementRuntime = Get-BackendRuntime -Port $replacement.Port -Token $SessionToken
        if ((Convert-ToFullPath -Path $replacementRuntime.dataDir) -ne $DataDir) {
            throw "Recovered backend used unexpected dataDir: $($replacementRuntime.dataDir)"
        }
        $metadataPath = Wait-BackendCrashMetadata -DataDir $DataDir -BackendPid $initialBackendPid -DeadlineSec 10

        $listener = $replacement
        $health = $replacementHealth
        $runtime = $replacementRuntime
        if ($portConflict) {
            $portConflict.recoveredBackendPort = [int]$replacement.Port
        }
        $crashRecovery = [pscustomobject]@{
            verified = $true
            killedBackendPid = $initialBackendPid
            replacementBackendPid = [int]$replacement.BackendPid
            initialPort = $initialPort
            replacementPort = [int]$replacement.Port
            metadataPath = $metadataPath
        }
    }
    $controlledShutdown = $null
    if ($SimulateBackendShutdown) {
        $shutdownBackendPid = [int]$listener.BackendPid
        $shutdownPort = [int]$listener.Port
        $shutdownBaseline = @(Get-ManagedBackendProcesses | ForEach-Object { [int]$_.ProcessId })
        $shutdownResponse = Invoke-BackendShutdown -Port $shutdownPort -Token $SessionToken
        Wait-ProcessExit -ProcessId $shutdownBackendPid -DeadlineSec 15

        $replacement = Wait-NewBackendListener -BaselinePids $shutdownBaseline -DeadlineSec $CrashRecoveryTimeoutSec
        if ($replacement.BackendPid -eq $shutdownBackendPid) {
            throw "Backend shutdown recovery reused the stopped backend pid $shutdownBackendPid."
        }
        if ($OccupyDefaultPort -and [int]$replacement.Port -eq $DefaultBackendPort) {
            throw "Recovered backend used default port $DefaultBackendPort even though it was occupied."
        }
        $replacementHealth = Wait-BackendHealth -Port $replacement.Port -DeadlineSec $BackendHealthTimeoutSec
        $replacementRuntime = Get-BackendRuntime -Port $replacement.Port -Token $SessionToken
        if ((Convert-ToFullPath -Path $replacementRuntime.dataDir) -ne $DataDir) {
            throw "Recovered backend used unexpected dataDir: $($replacementRuntime.dataDir)"
        }
        $metadataPath = Wait-BackendCrashMetadata -DataDir $DataDir -BackendPid $shutdownBackendPid -DeadlineSec 10

        $listener = $replacement
        $health = $replacementHealth
        $runtime = $replacementRuntime
        if ($portConflict) {
            $portConflict.recoveredBackendPort = [int]$replacement.Port
        }
        $controlledShutdown = [pscustomobject]@{
            verified = $true
            shutdownBackendPid = $shutdownBackendPid
            replacementBackendPid = [int]$replacement.BackendPid
            initialPort = $shutdownPort
            replacementPort = [int]$replacement.Port
            responseMessage = $shutdownResponse.message
            metadataPath = $metadataPath
        }
    }
    $portConflictResult = $null
    if ($portConflict) {
        $portConflictResult = [pscustomobject]$portConflict
    }
    $stability = Test-RuntimeStability `
        -AppProcess $app `
        -BackendPid ([int]$listener.BackendPid) `
        -Port ([int]$listener.Port) `
        -Token $SessionToken `
        -ExpectedRuntimeMode $expectedRuntimeMode `
        -DurationSec $StabilityDurationSec `
        -ProbeIntervalSec $StabilityProbeIntervalSec
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
        externalAttach = $externalAttach
        portConflict = $portConflictResult
        legacyDataMigration = $legacyDataMigration
        startupTimeout = $startupTimeout
        crashRecovery = $crashRecovery
        controlledShutdown = $controlledShutdown
        stability = $stability
        cleanupVerified = $false
    }
} finally {
    $cleanupFailure = $null
    if (-not $KeepAppOpen -and $app -and -not $app.HasExited) {
        Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $app.Id -Timeout 10 -ErrorAction SilentlyContinue
    }

    if ($externalBackendPid) {
        try {
            Invoke-BackendShutdown -Port $DefaultBackendPort -Token $SessionToken | Out-Null
            Wait-ProcessExit -ProcessId $externalBackendPid -DeadlineSec 15
        } catch {
            Stop-Process -Id $externalBackendPid -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $externalBackendPid -Timeout 10 -ErrorAction SilentlyContinue
        }
    }
    if ($externalBackend -and -not $externalBackend.HasExited) {
        Stop-Process -Id $externalBackend.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $externalBackend.Id -Timeout 10 -ErrorAction SilentlyContinue
    }

    if (-not $KeepAppOpen) {
        $remaining = Wait-ManagedBackendProcessesExit -BaselinePids $baseline -DeadlineSec $CleanupTimeoutSec
        if ($remaining.Count -gt 0) {
            foreach ($processId in $remaining) {
                Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            }
            $cleanupFailure = "Managed backend process remained after Tauri exit for ${CleanupTimeoutSec}s: $($remaining -join ', ')"
        } elseif ($result) {
            $result.cleanupVerified = $true
        }
    }

    if ($defaultPortBlocker) {
        $defaultPortBlocker.Stop()
        $defaultPortBlocker = $null
    }

    $env:SCRIBER_REPO_ROOT = $oldRoot
    $env:SCRIBER_PYTHON = $oldPython
    $env:SCRIBER_BACKEND_EXE = $oldBackendExe
    $env:SCRIBER_DATA_DIR = $oldDataDir
    $env:SCRIBER_FORCE_MANAGED_BACKEND = $oldForceManaged
    $env:SCRIBER_SESSION_TOKEN = $oldSessionToken
    $env:SCRIBER_DISABLE_HOTKEYS = $oldHotkeys
    $env:SCRIBER_DISABLE_DEVICE_MONITOR = $oldMonitor
    $env:SCRIBER_LEGACY_DATA_DIR = $oldLegacyDataDir
    $env:SCRIBER_WEB_HOST = $oldWebHost
    $env:SCRIBER_WEB_PORT = $oldWebPort
    $env:SCRIBER_RUNTIME_MODE = $oldRuntimeMode
    $env:SCRIBER_BACKEND_LAUNCH_KIND = $oldLaunchKind
    $env:SCRIBER_BACKEND_START_TIMEOUT_MS = $oldBackendStartTimeout
    $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_ONCE = $oldSimulateStartupTimeout
    $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER = $oldSimulateStartupTimeoutMarker

    if ($cleanupFailure) {
        throw $cleanupFailure
    }
}

$result | ConvertTo-Json -Compress -Depth 8
