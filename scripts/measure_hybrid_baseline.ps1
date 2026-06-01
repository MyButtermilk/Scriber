<#
.SYNOPSIS
Collects Phase 0 baseline measurements for the hybrid Tauri + Python runtime.

.DESCRIPTION
Starts the release Tauri executable for one or more iterations, waits for the
main window and managed Python backend, reads runtime/hot-path metrics, verifies
managed backend cleanup, and writes a JSON baseline artifact.

Build the executable and sidecar first, for example:
  cd Frontend
  npm run tauri:build

Typical quick run from repo root:
  powershell -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 -Iterations 3 -DisableDevFallback
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$ExePath = "",
    [string]$PythonPath = "",
    [string]$BackendExePath = "",
    [string]$OutputPath = "",
    [int]$Iterations = 3,
    [int]$TimeoutSec = 60,
    [int]$BackendHealthTimeoutSec = 20,
    [int]$UiVisibleTimeoutSec = 20,
    [int]$WsIterations = 2000,
    [int]$WsWarmup = 100,
    [string]$WsClientCounts = "1,5",
    [switch]$Hidden,
    [switch]$SkipUiVisibleWait,
    [switch]$SkipWsBenchmark,
    [switch]$KeepArtifacts,
    [switch]$EnableHotkeys,
    [switch]$EnableDeviceMonitor,
    [switch]$DisableDevFallback,
    [switch]$FailOnIncompleteGate
)

$ErrorActionPreference = "Stop"

function Convert-ToFullPath {
    param([string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Assert-UnderRoot {
    param(
        [string]$Root,
        [string]$Path,
        [string]$Label
    )

    $rootFull = Convert-ToFullPath -Path $Root
    $pathFull = Convert-ToFullPath -Path $Path
    if (-not $pathFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under $rootFull. Got: $pathFull"
    }
}

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

function Wait-AppMainWindow {
    param(
        [System.Diagnostics.Process]$Process,
        [System.Diagnostics.Stopwatch]$Stopwatch,
        [int]$DeadlineSec
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 100
        $current = Get-Process -Id $Process.Id -ErrorAction SilentlyContinue
        if (-not $current) {
            return $null
        }
        if ([int64]$current.MainWindowHandle -ne 0) {
            return [math]::Round($Stopwatch.Elapsed.TotalMilliseconds, 2)
        }
    }
    return $null
}

function Wait-NewBackendListener {
    param(
        [int[]]$BaselinePids,
        [System.Diagnostics.Stopwatch]$Stopwatch,
        [int]$DeadlineSec
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
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
                    backendPid = [int]$process.ProcessId
                    backendPort = [int]($ports | Select-Object -First 1)
                    backendListenerMs = [math]::Round($Stopwatch.Elapsed.TotalMilliseconds, 2)
                }
            }
        }
    }
    throw "No managed backend listener appeared within ${DeadlineSec}s."
}

function Wait-BackendHealth {
    param(
        [int]$Port,
        [System.Diagnostics.Stopwatch]$Stopwatch,
        [int]$DeadlineSec
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            if ($health.ok -and $health.runtimeMode -eq "tauri-supervised" -and $health.apiVersion) {
                return [pscustomobject]@{
                    backendReadyMs = [math]::Round($Stopwatch.Elapsed.TotalMilliseconds, 2)
                    health = $health
                }
            }
            Start-Sleep -Milliseconds 250
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    throw "Managed backend on port $Port did not return tauri-supervised health."
}

function Invoke-BackendJson {
    param(
        [int]$Port,
        [string]$Path,
        [string]$Token
    )

    $headers = @{}
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }
    return Invoke-RestMethod -Uri "http://127.0.0.1:$Port$Path" -Headers $headers -TimeoutSec 5
}

function Wait-ManagedBackendCleanup {
    param(
        [int[]]$BaselinePids,
        [int]$DeadlineSec
    )

    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        $remaining = @(
            Get-ManagedBackendProcesses |
                Where-Object { $BaselinePids -notcontains [int]$_.ProcessId } |
                ForEach-Object { [int]$_.ProcessId }
        )
        if ($remaining.Count -eq 0) {
            return [pscustomobject]@{
                cleanupVerified = $true
                cleanupMs = [math]::Round($watch.Elapsed.TotalMilliseconds, 2)
                remainingBackendPids = @()
            }
        }
        Start-Sleep -Milliseconds 250
    }

    $remainingFinal = @(
        Get-ManagedBackendProcesses |
            Where-Object { $BaselinePids -notcontains [int]$_.ProcessId } |
            ForEach-Object { [int]$_.ProcessId }
    )
    return [pscustomobject]@{
        cleanupVerified = $false
        cleanupMs = [math]::Round($watch.Elapsed.TotalMilliseconds, 2)
        remainingBackendPids = $remainingFinal
    }
}

function Get-Percentile {
    param(
        [double[]]$Values,
        [double]$Percentile
    )

    $clean = @($Values | Where-Object { $null -ne $_ } | Sort-Object)
    if ($clean.Count -eq 0) {
        return $null
    }
    $idx = [Math]::Ceiling(($Percentile / 100.0) * $clean.Count) - 1
    $idx = [Math]::Max(0, [Math]::Min($clean.Count - 1, $idx))
    return [math]::Round([double]$clean[$idx], 2)
}

function New-SampleSummary {
    param(
        [object[]]$Samples,
        [string]$PropertyName
    )

    $values = @(
        $Samples |
            ForEach-Object {
                $value = $_.$PropertyName
                if ($null -ne $value) {
                    [double]$value
                }
            }
    )
    if ($values.Count -eq 0) {
        return [pscustomobject]@{
            count = 0
            minMs = $null
            p50Ms = $null
            p95Ms = $null
            maxMs = $null
        }
    }

    return [pscustomobject]@{
        count = $values.Count
        minMs = [math]::Round(($values | Measure-Object -Minimum).Minimum, 2)
        p50Ms = Get-Percentile -Values $values -Percentile 50.0
        p95Ms = Get-Percentile -Values $values -Percentile 95.0
        maxMs = [math]::Round(($values | Measure-Object -Maximum).Maximum, 2)
    }
}

function Get-HotPathSegmentNames {
    param([object[]]$Samples)

    $names = New-Object System.Collections.Generic.HashSet[string]
    foreach ($sample in $Samples) {
        $items = @()
        if ($sample.hotPathMetrics -and $sample.hotPathMetrics.items) {
            $items = @($sample.hotPathMetrics.items)
        }
        foreach ($item in $items) {
            if (-not $item.segments) {
                continue
            }
            foreach ($property in $item.segments.PSObject.Properties) {
                [void]$names.Add($property.Name)
            }
        }
    }
    $result = @()
    foreach ($name in $names) {
        $result += $name
    }
    return @($result | Sort-Object)
}

function New-Requirement {
    param(
        [string]$Name,
        [string]$Status,
        [string]$Evidence,
        [string]$Notes = ""
    )

    return [pscustomobject]@{
        name = $Name
        status = $Status
        evidence = $Evidence
        notes = $Notes
    }
}

function Invoke-WebSocketBroadcastBenchmark {
    param(
        [string]$BaselineOutputPath
    )

    $scriptPath = Join-Path $RepoRoot "scripts\measure_ws_broadcast_baseline.py"
    if (-not (Test-Path $scriptPath)) {
        throw "Missing WebSocket baseline benchmark script: $scriptPath"
    }

    $outputDir = Split-Path $BaselineOutputPath
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($BaselineOutputPath)
    $wsOutputPath = Join-Path $outputDir "$baseName-websocket.json"
    Assert-UnderRoot -Root (Join-Path $RepoRoot "tmp") -Path $wsOutputPath -Label "WebSocket baseline output"

    $stdoutPath = Join-Path $outputDir "$baseName-websocket.out"
    $stderrPath = Join-Path $outputDir "$baseName-websocket.err"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

    $process = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @(
            $scriptPath,
            "--iterations", [string]$WsIterations,
            "--warmup", [string]$WsWarmup,
            "--clients", $WsClientCounts,
            "--output", $wsOutputPath
        ) `
        -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -Wait `
        -PassThru

    if ($process.ExitCode -ne 0) {
        $stdout = if (Test-Path $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        throw "WebSocket baseline benchmark failed with exit code $($process.ExitCode). stdout: $stdout stderr: $stderr"
    }
    if (-not (Test-Path $wsOutputPath)) {
        throw "WebSocket baseline benchmark did not write output: $wsOutputPath"
    }

    return Get-Content -LiteralPath $wsOutputPath -Raw | ConvertFrom-Json
}

function Invoke-BaselineIteration {
    param(
        [int]$Index
    )

    $dataDir = Join-Path $RepoRoot ("tmp\hybrid-baseline-data\" + [System.Guid]::NewGuid().ToString("N"))
    $dataDir = Convert-ToFullPath -Path $dataDir
    Assert-UnderRoot -Root (Join-Path $RepoRoot "tmp") -Path $dataDir -Label "Baseline data dir"
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

    $sessionToken = [System.Guid]::NewGuid().ToString("N")
    $baseline = @(Get-ManagedBackendProcesses | ForEach-Object { [int]$_.ProcessId })

    $oldRoot = $env:SCRIBER_REPO_ROOT
    $oldPython = $env:SCRIBER_PYTHON
    $oldBackendExe = $env:SCRIBER_BACKEND_EXE
    $oldDataDir = $env:SCRIBER_DATA_DIR
    $oldForceManaged = $env:SCRIBER_FORCE_MANAGED_BACKEND
    $oldSessionToken = $env:SCRIBER_SESSION_TOKEN
    $oldHotkeys = $env:SCRIBER_DISABLE_HOTKEYS
    $oldMonitor = $env:SCRIBER_DISABLE_DEVICE_MONITOR

    if ($DisableDevFallback) {
        $env:SCRIBER_REPO_ROOT = $null
        $env:SCRIBER_PYTHON = $null
    } else {
        $env:SCRIBER_REPO_ROOT = $RepoRoot
        $env:SCRIBER_PYTHON = $PythonPath
    }
    $env:SCRIBER_DATA_DIR = $dataDir
    $env:SCRIBER_FORCE_MANAGED_BACKEND = "1"
    $env:SCRIBER_SESSION_TOKEN = $sessionToken
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
    $sample = $null
    try {
        $watch = [System.Diagnostics.Stopwatch]::StartNew()
        $startArgs = @{
            FilePath = $ExePath
            WorkingDirectory = (Split-Path $ExePath)
            PassThru = $true
        }
        if ($Hidden) {
            $startArgs.WindowStyle = "Hidden"
        }
        $app = Start-Process @startArgs
        $uiVisibleMs = $null
        if (-not $Hidden -and -not $SkipUiVisibleWait) {
            $uiVisibleMs = Wait-AppMainWindow -Process $app -Stopwatch $watch -DeadlineSec $UiVisibleTimeoutSec
        }

        $listener = Wait-NewBackendListener -BaselinePids $baseline -Stopwatch $watch -DeadlineSec $TimeoutSec
        if ($app.HasExited) {
            throw "Tauri process exited early with code $($app.ExitCode)."
        }
        $healthResult = Wait-BackendHealth -Port $listener.backendPort -Stopwatch $watch -DeadlineSec $BackendHealthTimeoutSec
        $runtimeFetchStart = $watch.Elapsed.TotalMilliseconds
        $runtime = Invoke-BackendJson -Port $listener.backendPort -Path "/api/runtime" -Token $sessionToken
        $runtimeFetchMs = [math]::Round($watch.Elapsed.TotalMilliseconds - $runtimeFetchStart, 2)

        $hotPathMetrics = $null
        try {
            $hotPathMetrics = Invoke-BackendJson -Port $listener.backendPort -Path "/api/metrics/hot-path?limit=200" -Token $sessionToken
        } catch {
            $hotPathMetrics = [pscustomobject]@{
                summary = [pscustomobject]@{ count = 0 }
                items = @()
                error = $_.Exception.Message
            }
        }

        $sample = [pscustomobject]@{
            iteration = $Index
            ok = $true
            appPid = $app.Id
            backendPid = $listener.backendPid
            backendPort = $listener.backendPort
            coldStartToUiVisibleMs = $uiVisibleMs
            backendListenerMs = $listener.backendListenerMs
            backendReadyMs = $healthResult.backendReadyMs
            runtimeFetchMs = $runtimeFetchMs
            runtimeMode = $healthResult.health.runtimeMode
            apiVersion = $healthResult.health.apiVersion
            ready = $healthResult.health.ready
            dataDir = $runtime.dataDir
            downloadsDir = $runtime.downloadsDir
            launchKind = $runtime.launchKind
            hotPathMetrics = $hotPathMetrics
            cleanupVerified = $false
            cleanupMs = $null
            remainingBackendPids = @()
        }
    } finally {
        if ($app -and -not $app.HasExited) {
            Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $app.Id -Timeout 10 -ErrorAction SilentlyContinue
        }
        $cleanup = Wait-ManagedBackendCleanup -BaselinePids $baseline -DeadlineSec 10
        if ($sample) {
            $sample.cleanupVerified = $cleanup.cleanupVerified
            $sample.cleanupMs = $cleanup.cleanupMs
            $sample.remainingBackendPids = $cleanup.remainingBackendPids
        }
        if (-not $cleanup.cleanupVerified) {
            foreach ($processId in $cleanup.remainingBackendPids) {
                Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            }
        }
        if (-not $KeepArtifacts -and (Test-Path $dataDir)) {
            Assert-UnderRoot -Root (Join-Path $RepoRoot "tmp") -Path $dataDir -Label "Baseline data dir cleanup"
            Remove-Item -LiteralPath $dataDir -Recurse -Force
        }

        $env:SCRIBER_REPO_ROOT = $oldRoot
        $env:SCRIBER_PYTHON = $oldPython
        $env:SCRIBER_BACKEND_EXE = $oldBackendExe
        $env:SCRIBER_DATA_DIR = $oldDataDir
        $env:SCRIBER_FORCE_MANAGED_BACKEND = $oldForceManaged
        $env:SCRIBER_SESSION_TOKEN = $oldSessionToken
        $env:SCRIBER_DISABLE_HOTKEYS = $oldHotkeys
        $env:SCRIBER_DISABLE_DEVICE_MONITOR = $oldMonitor
    }

    if (-not $sample) {
        throw "Baseline iteration $Index did not produce a sample."
    }
    if (-not $sample.cleanupVerified) {
        throw "Managed backend cleanup failed for iteration ${Index}: $($sample.remainingBackendPids -join ', ')"
    }
    return $sample
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
if ($Iterations -lt 1) {
    throw "Iterations must be >= 1."
}
if ($WsIterations -lt 1) {
    throw "WsIterations must be >= 1."
}
if ($WsWarmup -lt 0) {
    throw "WsWarmup must be >= 0."
}
if (-not $OutputPath) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $OutputPath = Join-Path $RepoRoot "tmp\hybrid-baseline\hybrid-baseline-$stamp.json"
}
$OutputPath = Convert-ToFullPath -Path $OutputPath
Assert-UnderRoot -Root (Join-Path $RepoRoot "tmp") -Path $OutputPath -Label "Baseline output"
New-Item -ItemType Directory -Force -Path (Split-Path $OutputPath) | Out-Null

$samples = @()
for ($i = 1; $i -le $Iterations; $i++) {
    $samples += Invoke-BaselineIteration -Index $i
}

$webSocketBenchmark = $null
if (-not $SkipWsBenchmark) {
    $webSocketBenchmark = Invoke-WebSocketBroadcastBenchmark -BaselineOutputPath $OutputPath
}

$segmentNames = @(Get-HotPathSegmentNames -Samples $samples)
$hasHotPathSamples = @($samples | Where-Object { $_.hotPathMetrics -and $_.hotPathMetrics.summary -and [int]$_.hotPathMetrics.summary.count -gt 0 }).Count -gt 0

$requirements = @(
    New-Requirement `
        -Name "cold_start_to_ui_visible" `
        -Status $(if ($Hidden -or $SkipUiVisibleWait) { "skipped" } elseif ((New-SampleSummary -Samples $samples -PropertyName "coldStartToUiVisibleMs").count -gt 0) { "measured" } else { "missing" }) `
        -Evidence "Tauri process MainWindowHandle polling" `
        -Notes $(if ($Hidden -or $SkipUiVisibleWait) { "Run without -Hidden/-SkipUiVisibleWait to collect UI-visible timing." } else { "" })
    New-Requirement `
        -Name "backend_ready_time" `
        -Status "measured" `
        -Evidence "Tauri process start to /api/health ready"
    New-Requirement `
        -Name "hotkey_to_recording_state" `
        -Status $(if ($segmentNames -contains "hotkey_received_to_mic_ready_ms") { "measured" } elseif ($hasHotPathSamples) { "partial" } else { "missing_samples" }) `
        -Evidence "/api/metrics/hot-path segment hotkey_received_to_mic_ready_ms"
    New-Requirement `
        -Name "hotkey_to_first_audio_frame" `
        -Status $(if ($segmentNames -contains "hotkey_received_to_first_audio_frame_ms") { "measured" } elseif ($hasHotPathSamples) { "partial" } else { "missing_samples" }) `
        -Evidence "/api/metrics/hot-path segment hotkey_received_to_first_audio_frame_ms"
    New-Requirement `
        -Name "stop_to_text_injection" `
        -Status $(if ($segmentNames -contains "stop_requested_to_first_paste_ms") { "measured" } elseif ($hasHotPathSamples) { "partial" } else { "missing_samples" }) `
        -Evidence "/api/metrics/hot-path segment stop_requested_to_first_paste_ms"
    New-Requirement `
        -Name "upload_export_under_load" `
        -Status "not_automated_yet" `
        -Evidence "No load runner wired into this baseline script yet."
    New-Requirement `
        -Name "websocket_events_and_json_serialize_cost" `
        -Status $(if ($SkipWsBenchmark) { "skipped" } elseif ($webSocketBenchmark -and $webSocketBenchmark.ok) { "measured" } else { "missing" }) `
        -Evidence "scripts/measure_ws_broadcast_baseline.py measures JSON serialization, no-client fast path, and broadcast throughput with synthetic WebSocket clients." `
        -Notes $(if ($SkipWsBenchmark) { "Run without -SkipWsBenchmark to collect WebSocket throughput and serialization baseline." } else { "" })
    New-Requirement `
        -Name "history_scroll_many_transcripts" `
        -Status "not_automated_yet" `
        -Evidence "No browser scroll benchmark wired into this baseline script yet."
)

$incomplete = @(
    $requirements |
        Where-Object { $_.status -notin @("measured") }
)

$gitCommit = ""
$gitBranch = ""
try {
    $gitCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null)
    $gitBranch = (& git -C $RepoRoot rev-parse --abbrev-ref HEAD 2>$null)
} catch {
    $gitCommit = ""
    $gitBranch = ""
}

$result = [pscustomobject]@{
    schemaVersion = 1
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    repoRoot = $RepoRoot
    gitBranch = $gitBranch
    gitCommit = $gitCommit
    exePath = $ExePath
    backendExePath = $BackendExePath
    iterations = $Iterations
    options = [pscustomobject]@{
        hidden = [bool]$Hidden
        skipUiVisibleWait = [bool]$SkipUiVisibleWait
        disableDevFallback = [bool]$DisableDevFallback
        enableHotkeys = [bool]$EnableHotkeys
        enableDeviceMonitor = [bool]$EnableDeviceMonitor
        skipWsBenchmark = [bool]$SkipWsBenchmark
        wsIterations = $WsIterations
        wsWarmup = $WsWarmup
        wsClientCounts = $WsClientCounts
    }
    summary = [pscustomobject]@{
        coldStartToUiVisibleMs = New-SampleSummary -Samples $samples -PropertyName "coldStartToUiVisibleMs"
        backendListenerMs = New-SampleSummary -Samples $samples -PropertyName "backendListenerMs"
        backendReadyMs = New-SampleSummary -Samples $samples -PropertyName "backendReadyMs"
        runtimeFetchMs = New-SampleSummary -Samples $samples -PropertyName "runtimeFetchMs"
        cleanupMs = New-SampleSummary -Samples $samples -PropertyName "cleanupMs"
        hotPathSegmentNames = @($segmentNames)
        webSocketBenchmark = $(if ($webSocketBenchmark) { $webSocketBenchmark.summary } else { $null })
    }
    phase0Gate = [pscustomobject]@{
        complete = $incomplete.Count -eq 0
        incompleteRequirements = @($incomplete | ForEach-Object { $_.name })
        requirements = $requirements
    }
    webSocketBenchmark = $webSocketBenchmark
    samples = $samples
}

$json = $result | ConvertTo-Json -Depth 12
Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8
Write-Output $json

if ($FailOnIncompleteGate -and -not $result.phase0Gate.complete) {
    exit 1
}
