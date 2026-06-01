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
    [int]$UploadFiles = 4,
    [double]$UploadSizeMb = 4.0,
    [double]$UploadChunkMb = 1.0,
    [int]$ExportIterations = 2,
    [int]$ExportConcurrency = 2,
    [int]$ExportParagraphs = 120,
    [int]$WsIterations = 2000,
    [int]$WsWarmup = 100,
    [string]$WsClientCounts = "1,5",
    [int]$HistoryItems = 2000,
    [string]$HistoryRoutes = "/",
    [string]$HistoryViews = "list,grid",
    [switch]$RecordHotPathSamples,
    [int]$RecordingHotPathIterations = 1,
    [double]$RecordingHotPathSeconds = 2.0,
    [int]$RecordingHotPathTimeoutSec = 60,
    [switch]$Hidden,
    [switch]$SkipUiVisibleWait,
    [switch]$SkipUploadExportBenchmark,
    [switch]$SkipWsBenchmark,
    [switch]$SkipHistoryScrollBenchmark,
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

function Invoke-UploadExportBenchmark {
    param(
        [string]$BaselineOutputPath
    )

    $scriptPath = Join-Path $RepoRoot "scripts\measure_upload_export_baseline.py"
    if (-not (Test-Path $scriptPath)) {
        throw "Missing upload/export baseline benchmark script: $scriptPath"
    }

    $outputDir = Split-Path $BaselineOutputPath
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($BaselineOutputPath)
    $benchmarkOutputPath = Join-Path $outputDir "$baseName-upload-export.json"
    Assert-UnderRoot -Root (Join-Path $RepoRoot "tmp") -Path $benchmarkOutputPath -Label "Upload/export baseline output"

    $stdoutPath = Join-Path $outputDir "$baseName-upload-export.out"
    $stderrPath = Join-Path $outputDir "$baseName-upload-export.err"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

    $process = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @(
            $scriptPath,
            "--upload-files", [string]$UploadFiles,
            "--upload-size-mb", [string]$UploadSizeMb,
            "--upload-chunk-mb", [string]$UploadChunkMb,
            "--export-iterations", [string]$ExportIterations,
            "--export-concurrency", [string]$ExportConcurrency,
            "--export-paragraphs", [string]$ExportParagraphs,
            "--output", $benchmarkOutputPath
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
        throw "Upload/export baseline benchmark failed with exit code $($process.ExitCode). stdout: $stdout stderr: $stderr"
    }
    if (-not (Test-Path $benchmarkOutputPath)) {
        throw "Upload/export baseline benchmark did not write output: $benchmarkOutputPath"
    }

    return Get-Content -LiteralPath $benchmarkOutputPath -Raw | ConvertFrom-Json
}

function Invoke-HistoryScrollBenchmark {
    param(
        [string]$BaselineOutputPath
    )

    $scriptPath = Join-Path $RepoRoot "scripts\measure_history_scroll_baseline.py"
    if (-not (Test-Path $scriptPath)) {
        throw "Missing history scroll baseline benchmark script: $scriptPath"
    }

    $outputDir = Split-Path $BaselineOutputPath
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($BaselineOutputPath)
    $benchmarkOutputPath = Join-Path $outputDir "$baseName-history-scroll.json"
    Assert-UnderRoot -Root (Join-Path $RepoRoot "tmp") -Path $benchmarkOutputPath -Label "History scroll baseline output"

    $stdoutPath = Join-Path $outputDir "$baseName-history-scroll.out"
    $stderrPath = Join-Path $outputDir "$baseName-history-scroll.err"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

    $process = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @(
            $scriptPath,
            "--items", [string]$HistoryItems,
            "--routes", $HistoryRoutes,
            "--views", $HistoryViews,
            "--output", $benchmarkOutputPath
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
        throw "History scroll baseline benchmark failed with exit code $($process.ExitCode). stdout: $stdout stderr: $stderr"
    }
    if (-not (Test-Path $benchmarkOutputPath)) {
        throw "History scroll baseline benchmark did not write output: $benchmarkOutputPath"
    }

    return Get-Content -LiteralPath $benchmarkOutputPath -Raw | ConvertFrom-Json
}

function Invoke-RecordingHotPathBenchmark {
    param(
        [int]$Port,
        [string]$Token,
        [string]$DataDir,
        [int]$Iteration
    )

    $scriptPath = Join-Path $RepoRoot "scripts\measure_recording_hot_path_baseline.py"
    if (-not (Test-Path $scriptPath)) {
        throw "Missing recording hot-path baseline benchmark script: $scriptPath"
    }

    $benchmarkOutputPath = Join-Path $DataDir "recording-hot-path-$Iteration.json"
    Assert-UnderRoot -Root (Join-Path $RepoRoot "tmp") -Path $benchmarkOutputPath -Label "Recording hot-path baseline output"

    $stdoutPath = Join-Path $DataDir "recording-hot-path-$Iteration.out"
    $stderrPath = Join-Path $DataDir "recording-hot-path-$Iteration.err"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

    $process = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @(
            $scriptPath,
            "--base-url", "http://127.0.0.1:$Port",
            "--token", $Token,
            "--iterations", [string]$RecordingHotPathIterations,
            "--record-seconds", [string]$RecordingHotPathSeconds,
            "--start-timeout-sec", [string]$RecordingHotPathTimeoutSec,
            "--stop-timeout-sec", [string]$RecordingHotPathTimeoutSec,
            "--metric-timeout-sec", [string]$RecordingHotPathTimeoutSec,
            "--output", $benchmarkOutputPath
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
        return [pscustomobject]@{
            ok = $false
            error = "Recording hot-path benchmark failed with exit code $($process.ExitCode). stdout: $stdout stderr: $stderr"
            summary = [pscustomobject]@{
                complete = $false
                requirements = [pscustomobject]@{}
            }
        }
    }
    if (-not (Test-Path $benchmarkOutputPath)) {
        return [pscustomobject]@{
            ok = $false
            error = "Recording hot-path benchmark did not write output: $benchmarkOutputPath"
            summary = [pscustomobject]@{
                complete = $false
                requirements = [pscustomobject]@{}
            }
        }
    }

    return Get-Content -LiteralPath $benchmarkOutputPath -Raw | ConvertFrom-Json
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

        $recordingHotPathBenchmark = $null
        if ($RecordHotPathSamples) {
            $recordingHotPathBenchmark = Invoke-RecordingHotPathBenchmark `
                -Port $listener.backendPort `
                -Token $sessionToken `
                -DataDir $dataDir `
                -Iteration $Index
        }

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
            recordingHotPathBenchmark = $recordingHotPathBenchmark
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
if ($UploadFiles -lt 1) {
    throw "UploadFiles must be >= 1."
}
if ($UploadSizeMb -le 0) {
    throw "UploadSizeMb must be > 0."
}
if ($UploadChunkMb -le 0) {
    throw "UploadChunkMb must be > 0."
}
if ($ExportIterations -lt 1) {
    throw "ExportIterations must be >= 1."
}
if ($ExportConcurrency -lt 1) {
    throw "ExportConcurrency must be >= 1."
}
if ($ExportParagraphs -lt 1) {
    throw "ExportParagraphs must be >= 1."
}
if ($WsIterations -lt 1) {
    throw "WsIterations must be >= 1."
}
if ($WsWarmup -lt 0) {
    throw "WsWarmup must be >= 0."
}
if ($HistoryItems -lt 1) {
    throw "HistoryItems must be >= 1."
}
if ($RecordingHotPathIterations -lt 1) {
    throw "RecordingHotPathIterations must be >= 1."
}
if ($RecordingHotPathSeconds -le 0) {
    throw "RecordingHotPathSeconds must be > 0."
}
if ($RecordingHotPathTimeoutSec -lt 1) {
    throw "RecordingHotPathTimeoutSec must be >= 1."
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

$uploadExportBenchmark = $null
if (-not $SkipUploadExportBenchmark) {
    $uploadExportBenchmark = Invoke-UploadExportBenchmark -BaselineOutputPath $OutputPath
}

$webSocketBenchmark = $null
if (-not $SkipWsBenchmark) {
    $webSocketBenchmark = Invoke-WebSocketBroadcastBenchmark -BaselineOutputPath $OutputPath
}

$historyScrollBenchmark = $null
if (-not $SkipHistoryScrollBenchmark) {
    $historyScrollBenchmark = Invoke-HistoryScrollBenchmark -BaselineOutputPath $OutputPath
}

$segmentNames = @(Get-HotPathSegmentNames -Samples $samples)
$hasHotPathSamples = @($samples | Where-Object { $_.hotPathMetrics -and $_.hotPathMetrics.summary -and [int]$_.hotPathMetrics.summary.count -gt 0 }).Count -gt 0
$recordingHotPathBenchmarks = @(
    $samples |
        ForEach-Object { $_.recordingHotPathBenchmark } |
        Where-Object { $null -ne $_ }
)

function Get-RecordingHotPathRequirementStatus {
    param(
        [string]$RequirementName,
        [string]$SegmentName
    )

    $statuses = @()
    foreach ($benchmark in $recordingHotPathBenchmarks) {
        if (-not $benchmark.summary -or -not $benchmark.summary.requirements) {
            continue
        }
        $property = $benchmark.summary.requirements.PSObject.Properties[$RequirementName]
        if ($property -and $property.Value -and $property.Value.status) {
            $statuses += [string]$property.Value.status
        }
    }

    if ($statuses -contains "measured") {
        return "measured"
    }
    if ($RecordHotPathSamples -and $statuses.Count -gt 0) {
        return [string]($statuses | Select-Object -First 1)
    }
    if ($segmentNames -contains $SegmentName) {
        return "measured"
    }
    if ($hasHotPathSamples) {
        return "partial"
    }
    if (-not $RecordHotPathSamples) {
        return "not_requested"
    }
    return "missing_samples"
}

function Get-RecordingHotPathRequirementNotes {
    if (-not $RecordHotPathSamples) {
        return "Run with -RecordHotPathSamples on a machine with microphone/provider access to collect live recording hot-path samples."
    }
    return ""
}

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
        -Status $(Get-RecordingHotPathRequirementStatus -RequirementName "hotkey_to_recording_state" -SegmentName "hotkey_received_to_mic_ready_ms") `
        -Evidence "/api/metrics/hot-path segment hotkey_received_to_mic_ready_ms" `
        -Notes $(Get-RecordingHotPathRequirementNotes)
    New-Requirement `
        -Name "hotkey_to_first_audio_frame" `
        -Status $(Get-RecordingHotPathRequirementStatus -RequirementName "hotkey_to_first_audio_frame" -SegmentName "hotkey_received_to_first_audio_frame_ms") `
        -Evidence "/api/metrics/hot-path segment hotkey_received_to_first_audio_frame_ms" `
        -Notes $(Get-RecordingHotPathRequirementNotes)
    New-Requirement `
        -Name "stop_to_text_injection" `
        -Status $(Get-RecordingHotPathRequirementStatus -RequirementName "stop_to_text_injection" -SegmentName "stop_requested_to_first_paste_ms") `
        -Evidence "/api/metrics/hot-path segment stop_requested_to_first_paste_ms" `
        -Notes $(Get-RecordingHotPathRequirementNotes)
    New-Requirement `
        -Name "upload_export_under_load" `
        -Status $(if ($SkipUploadExportBenchmark) { "skipped" } elseif ($uploadExportBenchmark -and $uploadExportBenchmark.ok) { "measured" } else { "missing" }) `
        -Evidence "scripts/measure_upload_export_baseline.py measures concurrent synthetic upload stream writes, parallel PDF/DOCX export rendering, and /api/health plus /api/state responsiveness under that load." `
        -Notes $(if ($SkipUploadExportBenchmark) { "Run without -SkipUploadExportBenchmark to collect upload/export load baseline." } else { "" })
    New-Requirement `
        -Name "websocket_events_and_json_serialize_cost" `
        -Status $(if ($SkipWsBenchmark) { "skipped" } elseif ($webSocketBenchmark -and $webSocketBenchmark.ok) { "measured" } else { "missing" }) `
        -Evidence "scripts/measure_ws_broadcast_baseline.py measures JSON serialization, no-client fast path, and broadcast throughput with synthetic WebSocket clients." `
        -Notes $(if ($SkipWsBenchmark) { "Run without -SkipWsBenchmark to collect WebSocket throughput and serialization baseline." } else { "" })
    New-Requirement `
        -Name "history_scroll_many_transcripts" `
        -Status $(if ($SkipHistoryScrollBenchmark) { "skipped" } elseif ($historyScrollBenchmark -and $historyScrollBenchmark.ok) { "measured" } else { "missing" }) `
        -Evidence "scripts/measure_history_scroll_baseline.py starts the React UI against a synthetic paginated backend and measures virtualized browser scrolling." `
        -Notes $(if ($SkipHistoryScrollBenchmark) { "Run without -SkipHistoryScrollBenchmark to collect large-history scroll baseline." } else { "" })
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
        skipUploadExportBenchmark = [bool]$SkipUploadExportBenchmark
        uploadFiles = $UploadFiles
        uploadSizeMb = $UploadSizeMb
        uploadChunkMb = $UploadChunkMb
        exportIterations = $ExportIterations
        exportConcurrency = $ExportConcurrency
        exportParagraphs = $ExportParagraphs
        skipWsBenchmark = [bool]$SkipWsBenchmark
        wsIterations = $WsIterations
        wsWarmup = $WsWarmup
        wsClientCounts = $WsClientCounts
        skipHistoryScrollBenchmark = [bool]$SkipHistoryScrollBenchmark
        historyItems = $HistoryItems
        historyRoutes = $HistoryRoutes
        historyViews = $HistoryViews
        recordHotPathSamples = [bool]$RecordHotPathSamples
        recordingHotPathIterations = $RecordingHotPathIterations
        recordingHotPathSeconds = $RecordingHotPathSeconds
        recordingHotPathTimeoutSec = $RecordingHotPathTimeoutSec
    }
    summary = [pscustomobject]@{
        coldStartToUiVisibleMs = New-SampleSummary -Samples $samples -PropertyName "coldStartToUiVisibleMs"
        backendListenerMs = New-SampleSummary -Samples $samples -PropertyName "backendListenerMs"
        backendReadyMs = New-SampleSummary -Samples $samples -PropertyName "backendReadyMs"
        runtimeFetchMs = New-SampleSummary -Samples $samples -PropertyName "runtimeFetchMs"
        cleanupMs = New-SampleSummary -Samples $samples -PropertyName "cleanupMs"
        hotPathSegmentNames = @($segmentNames)
        uploadExportBenchmark = $(if ($uploadExportBenchmark) { $uploadExportBenchmark.summary } else { $null })
        webSocketBenchmark = $(if ($webSocketBenchmark) { $webSocketBenchmark.summary } else { $null })
        historyScrollBenchmark = $(if ($historyScrollBenchmark) { $historyScrollBenchmark.summary } else { $null })
        recordingHotPathBenchmarks = @($recordingHotPathBenchmarks | ForEach-Object { $_.summary })
    }
    phase0Gate = [pscustomobject]@{
        complete = $incomplete.Count -eq 0
        incompleteRequirements = @($incomplete | ForEach-Object { $_.name })
        requirements = $requirements
    }
    uploadExportBenchmark = $uploadExportBenchmark
    webSocketBenchmark = $webSocketBenchmark
    historyScrollBenchmark = $historyScrollBenchmark
    recordingHotPathBenchmarks = @($recordingHotPathBenchmarks)
    samples = $samples
}

$json = $result | ConvertTo-Json -Depth 12
Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8
Write-Output $json

if ($FailOnIncompleteGate -and -not $result.phase0Gate.complete) {
    exit 1
}
