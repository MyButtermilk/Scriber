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
With -MaxBackendWorkingSetGrowthMB, the stability smoke also fails when backend
working-set peak growth exceeds the configured threshold. With
-MaxIdleCpuPercent, the stability smoke fails when normalized average idle CPU
for the Tauri app plus backend exceeds the configured threshold. With
-WaitForManualGlobalHotkey, it waits for a physical OS hotkey press and verifies
that the Tauri global shortcut dispatch reaches the existing backend endpoints.
With -LiveRecordingDurationSec, it explicitly starts live microphone recording,
keeps it running while sampling health/state, CPU, and memory, then stops it.
With -VerifySupportBundle, it downloads the token-protected support bundle and
verifies that injected dummy secrets are redacted from the ZIP contents.

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
    [string]$OutputPath = "",
    [string]$SessionToken = "",
    [int]$TimeoutSec = 60,
    [int]$BackendHealthTimeoutSec = 20,
    [int]$CleanupTimeoutSec = 20,
    [int]$StabilityDurationSec = 0,
    [int]$StabilityProbeIntervalSec = 5,
    [double]$MaxBackendWorkingSetGrowthMB = 0,
    [double]$MaxIdleCpuPercent = 0,
    [int]$LiveRecordingDurationSec = 0,
    [int]$LiveRecordingProbeIntervalSec = 5,
    [double]$MaxLiveBackendWorkingSetGrowthMB = 0,
    [double]$MaxLiveCpuPercent = 0,
    [int]$LiveRecordingStartTimeoutSec = 60,
    [int]$LiveRecordingStopTimeoutSec = 60,
    [switch]$KeepAppOpen,
    [switch]$EnableHotkeys,
    [switch]$EnableDeviceMonitor,
    [switch]$OccupyDefaultPort,
    [switch]$SimulateBackendCrash,
    [switch]$SimulateBackendShutdown,
    [switch]$AttachExternalBackend,
    [switch]$SimulateBackendStartupTimeout,
    [switch]$VerifyGlobalHotkeyRegistration,
    [switch]$SimulateGlobalHotkey,
    [switch]$WaitForManualGlobalHotkey,
    [switch]$VerifySupportBundle,
    [string]$GlobalHotkeySmokeHotkey = "ctrl+alt+shift+f12",
    [int]$GlobalHotkeyDispatchTimeoutSec = 20,
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

function Invoke-LiveMicStart {
    param(
        [int]$Port,
        [string]$Token
    )

    $headers = @{}
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }
    return Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/live-mic/start" -Headers $headers -TimeoutSec 10
}

function Invoke-LiveMicStop {
    param(
        [int]$Port,
        [string]$Token
    )

    $headers = @{}
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }
    return Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/live-mic/stop" -Headers $headers -TimeoutSec 10
}

function Wait-BackendState {
    param(
        [int]$Port,
        [string]$Token,
        [scriptblock]$Predicate,
        [int]$DeadlineSec,
        [string]$Label
    )

    $headers = @{}
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }
    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    $lastState = $null
    do {
        try {
            $state = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/state" -Headers $headers -TimeoutSec 5
            $lastState = $state
            if (& $Predicate $state) {
                return $state
            }
        } catch {
            $lastState = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    $lastStateJson = try { $lastState | ConvertTo-Json -Compress -Depth 4 } catch { [string]$lastState }
    throw "Timed out waiting for backend state '$Label' within ${DeadlineSec}s. Last state: $lastStateJson"
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

function Get-ProcessTotalCpuSeconds {
    param([int[]]$ProcessIds)

    $cpuByPid = @{}
    foreach ($processId in $ProcessIds) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($process) {
            $cpuByPid[[int]$processId] = [double]$process.TotalProcessorTime.TotalSeconds
        }
    }
    return $cpuByPid
}

function Get-DeltaCpuPercent {
    param(
        [object]$PreviousSeconds,
        [object]$CurrentSeconds,
        [double]$ElapsedSeconds,
        [int]$LogicalProcessorCount
    )

    if ($null -eq $PreviousSeconds -or $null -eq $CurrentSeconds -or $ElapsedSeconds -le 0) {
        return $null
    }
    $deltaSeconds = [double]$CurrentSeconds - [double]$PreviousSeconds
    if ($deltaSeconds -lt 0) {
        return $null
    }
    $normalized = ($deltaSeconds / ($ElapsedSeconds * [Math]::Max(1, $LogicalProcessorCount))) * 100
    return [Math]::Round($normalized, 2)
}

function Test-RuntimeStability {
    param(
        [System.Diagnostics.Process]$AppProcess,
        [int]$BackendPid,
        [int]$Port,
        [string]$Token,
        [string]$ExpectedRuntimeMode,
        [int]$DurationSec,
        [int]$ProbeIntervalSec,
        [double]$MaxWorkingSetGrowthMB = 0,
        [double]$MaxIdleCpuPercent = 0
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
    $logicalProcessorCount = [Math]::Max(1, [Environment]::ProcessorCount)
    $lastCpuSampleAt = Get-Date
    $lastCpuTotals = Get-ProcessTotalCpuSeconds -ProcessIds @([int]$AppProcess.Id, $BackendPid)
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

        $currentCpuSampleAt = Get-Date
        $currentCpuTotals = Get-ProcessTotalCpuSeconds -ProcessIds @([int]$AppProcess.Id, $BackendPid)
        $cpuElapsedSec = ($currentCpuSampleAt - $lastCpuSampleAt).TotalSeconds
        $appCpuPercent = Get-DeltaCpuPercent `
            -PreviousSeconds $lastCpuTotals[[int]$AppProcess.Id] `
            -CurrentSeconds $currentCpuTotals[[int]$AppProcess.Id] `
            -ElapsedSeconds $cpuElapsedSec `
            -LogicalProcessorCount $logicalProcessorCount
        $backendCpuPercent = Get-DeltaCpuPercent `
            -PreviousSeconds $lastCpuTotals[$BackendPid] `
            -CurrentSeconds $currentCpuTotals[$BackendPid] `
            -ElapsedSeconds $cpuElapsedSec `
            -LogicalProcessorCount $logicalProcessorCount
        $combinedCpuPercent = $null
        if ($null -ne $appCpuPercent -or $null -ne $backendCpuPercent) {
            $appCpuValue = if ($null -ne $appCpuPercent) { [double]$appCpuPercent } else { 0.0 }
            $backendCpuValue = if ($null -ne $backendCpuPercent) { [double]$backendCpuPercent } else { 0.0 }
            $combinedCpuPercent = [Math]::Round($appCpuValue + $backendCpuValue, 2)
        }
        $lastCpuSampleAt = $currentCpuSampleAt
        $lastCpuTotals = $currentCpuTotals

        $processSnapshot = Get-Process -Id $BackendPid -ErrorAction Stop
        $samples += [pscustomobject]@{
            index = $samples.Count + 1
            elapsedSec = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 2)
            backendPid = $BackendPid
            backendWorkingSetMb = [Math]::Round($processSnapshot.WorkingSet64 / 1MB, 2)
            appCpuPercent = $appCpuPercent
            backendCpuPercent = $backendCpuPercent
            combinedCpuPercent = $combinedCpuPercent
            healthMs = $healthProbe.elapsedMs
            stateMs = $stateProbe.elapsedMs
            healthReady = [bool]$health.ready
            status = [string]$state.status
            recordingState = [string]$state.recordingState
            listening = [bool]$state.listening
        }

        if ((Get-Date) -ge $deadline) {
            break
        }
        Start-Sleep -Seconds ([Math]::Max(1, $ProbeIntervalSec))
    } while ((Get-Date) -lt $deadline)

    $workingSetValues = @($samples | ForEach-Object { [double]$_.backendWorkingSetMb })
    $workingSetStart = if ($workingSetValues.Count) { [double]$workingSetValues[0] } else { $null }
    $workingSetEnd = if ($workingSetValues.Count) { [double]$workingSetValues[-1] } else { $null }
    $workingSetMax = if ($workingSetValues.Count) { [double](($workingSetValues | Measure-Object -Maximum).Maximum) } else { $null }
    $workingSetGrowth = if ($null -ne $workingSetStart -and $null -ne $workingSetEnd) { [Math]::Round($workingSetEnd - $workingSetStart, 2) } else { $null }
    $workingSetPeakGrowth = if ($null -ne $workingSetStart -and $null -ne $workingSetMax) { [Math]::Round($workingSetMax - $workingSetStart, 2) } else { $null }
    if ($MaxWorkingSetGrowthMB -gt 0 -and $null -ne $workingSetPeakGrowth -and $workingSetPeakGrowth -gt $MaxWorkingSetGrowthMB) {
        throw "Stability smoke backend working-set peak growth ${workingSetPeakGrowth}MB exceeded ${MaxWorkingSetGrowthMB}MB."
    }
    $appCpuValues = @($samples | Where-Object { $null -ne $_.appCpuPercent } | ForEach-Object { [double]$_.appCpuPercent })
    $backendCpuValues = @($samples | Where-Object { $null -ne $_.backendCpuPercent } | ForEach-Object { [double]$_.backendCpuPercent })
    $combinedCpuValues = @($samples | Where-Object { $null -ne $_.combinedCpuPercent } | ForEach-Object { [double]$_.combinedCpuPercent })
    $appCpuMax = if ($appCpuValues.Count) { [double](($appCpuValues | Measure-Object -Maximum).Maximum) } else { $null }
    $backendCpuMax = if ($backendCpuValues.Count) { [double](($backendCpuValues | Measure-Object -Maximum).Maximum) } else { $null }
    $combinedCpuMax = if ($combinedCpuValues.Count) { [double](($combinedCpuValues | Measure-Object -Maximum).Maximum) } else { $null }
    $combinedCpuAvg = if ($combinedCpuValues.Count) { [Math]::Round([double](($combinedCpuValues | Measure-Object -Average).Average), 2) } else { $null }
    if ($MaxIdleCpuPercent -gt 0 -and -not $combinedCpuValues.Count) {
        throw "Stability smoke could not collect idle CPU samples."
    }
    if ($MaxIdleCpuPercent -gt 0 -and $null -ne $combinedCpuAvg -and $combinedCpuAvg -gt $MaxIdleCpuPercent) {
        throw "Stability smoke average idle CPU ${combinedCpuAvg}% exceeded ${MaxIdleCpuPercent}%."
    }
    return [pscustomobject]@{
        verified = $true
        durationSec = $DurationSec
        probeIntervalSec = [Math]::Max(1, $ProbeIntervalSec)
        sampleCount = $samples.Count
        backendPid = $BackendPid
        logicalProcessorCount = $logicalProcessorCount
        backendWorkingSetStartMb = $workingSetStart
        backendWorkingSetEndMb = $workingSetEnd
        backendWorkingSetMaxMb = $workingSetMax
        backendWorkingSetGrowthMb = $workingSetGrowth
        backendWorkingSetPeakGrowthMb = $workingSetPeakGrowth
        maxBackendWorkingSetGrowthMb = if ($MaxWorkingSetGrowthMB -gt 0) { $MaxWorkingSetGrowthMB } else { $null }
        appCpuMaxPercent = $appCpuMax
        backendCpuMaxPercent = $backendCpuMax
        combinedCpuMaxPercent = $combinedCpuMax
        combinedCpuAvgPercent = $combinedCpuAvg
        maxIdleCpuPercent = if ($MaxIdleCpuPercent -gt 0) { $MaxIdleCpuPercent } else { $null }
        samples = $samples
    }
}

function Test-LiveRecordingStability {
    param(
        [System.Diagnostics.Process]$AppProcess,
        [int]$BackendPid,
        [int]$Port,
        [string]$Token,
        [string]$ExpectedRuntimeMode,
        [int]$DurationSec,
        [int]$ProbeIntervalSec,
        [double]$MaxWorkingSetGrowthMB = 0,
        [double]$MaxCpuPercent = 0,
        [int]$StartTimeoutSec = 60,
        [int]$StopTimeoutSec = 60
    )

    if ($DurationSec -le 0) {
        return $null
    }

    $startedState = $null
    $stability = $null
    $stopResponse = $null
    $stoppedState = $null
    try {
        $startResponse = Invoke-LiveMicStart -Port $Port -Token $Token
        $startedState = Wait-BackendState `
            -Port $Port `
            -Token $Token `
            -Predicate { param($state) ([string]$state.recordingState -eq "recording") -or ([bool]$state.listening) } `
            -DeadlineSec $StartTimeoutSec `
            -Label "live recording started"
        $stability = Test-RuntimeStability `
            -AppProcess $AppProcess `
            -BackendPid $BackendPid `
            -Port $Port `
            -Token $Token `
            -ExpectedRuntimeMode $ExpectedRuntimeMode `
            -DurationSec $DurationSec `
            -ProbeIntervalSec $ProbeIntervalSec `
            -MaxWorkingSetGrowthMB $MaxWorkingSetGrowthMB `
            -MaxIdleCpuPercent $MaxCpuPercent
        $nonRecordingSamples = @(
            $stability.samples |
                Where-Object { ([string]$_.recordingState -ne "recording") -and -not ([bool]$_.listening) }
        )
        if ($nonRecordingSamples.Count -gt 0) {
            throw "Live recording smoke observed non-recording state during stability sampling."
        }
        $stopResponse = Invoke-LiveMicStop -Port $Port -Token $Token
        $stoppedState = Wait-BackendState `
            -Port $Port `
            -Token $Token `
            -Predicate { param($state) ([string]$state.recordingState -eq "idle") -and -not ([bool]$state.listening) } `
            -DeadlineSec $StopTimeoutSec `
            -Label "live recording stopped"
        return [pscustomobject]@{
            verified = $true
            durationSec = $DurationSec
            probeIntervalSec = [Math]::Max(1, $ProbeIntervalSec)
            startResponseOk = [bool]$startResponse.ok
            startedRecordingState = [string]$startedState.recordingState
            startedListening = [bool]$startedState.listening
            stopResponseOk = [bool]$stopResponse.ok
            stoppedRecordingState = [string]$stoppedState.recordingState
            stoppedListening = [bool]$stoppedState.listening
            nonRecordingSampleCount = $nonRecordingSamples.Count
            stability = $stability
        }
    } catch {
        try {
            Invoke-LiveMicStop -Port $Port -Token $Token | Out-Null
        } catch {
            Write-Warning "Live recording smoke cleanup stop failed: $_"
        }
        throw
    }
}

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
        throw "$Label must stay under repo root. Got: $pathFull"
    }
}

function Write-SmokeJson {
    param(
        [object]$Payload,
        [string]$Path,
        [string]$Root
    )

    $json = $Payload | ConvertTo-Json -Compress -Depth 8
    if ($Path) {
        $outputFull = Convert-ToFullPath -Path $Path
        Assert-UnderRoot -Root (Join-Path $Root "tmp") -Path $outputFull -Label "Smoke output"
        New-Item -ItemType Directory -Force -Path (Split-Path $outputFull) | Out-Null
        Set-Content -LiteralPath $outputFull -Value $json -Encoding UTF8
    }
    return $json
}

function Read-ZipEntryText {
    param([string]$Path)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entries = @()
        $combined = New-Object System.Text.StringBuilder
        foreach ($entry in $archive.Entries) {
            $entries += $entry.FullName
            if ($entry.Length -le 0 -or $entry.Length -gt 2000000) {
                continue
            }
            $reader = New-Object System.IO.StreamReader($entry.Open(), [System.Text.Encoding]::UTF8, $true)
            try {
                [void]$combined.AppendLine($reader.ReadToEnd())
            } finally {
                $reader.Dispose()
            }
        }
        return [pscustomobject]@{
            entries = $entries
            combinedText = $combined.ToString()
        }
    } finally {
        $archive.Dispose()
    }
}

function Test-SupportBundle {
    param(
        [int]$Port,
        [string]$Token,
        [string]$RuntimeDataDir
    )

    $secretValues = @(
        "support-bundle-env-secret",
        "support-bundle-settings-secret",
        "support-bundle-log-secret",
        "support-bundle-bearer-secret"
    )

    $logsPath = Join-Path $RuntimeDataDir "logs"
    New-Item -ItemType Directory -Force -Path $logsPath | Out-Null
    Set-Content `
        -LiteralPath (Join-Path $RuntimeDataDir ".env") `
        -Value "OPENAI_API_KEY=$($secretValues[0])`nSCRIBER_MODE=toggle" `
        -Encoding UTF8
    Set-Content `
        -LiteralPath (Join-Path $RuntimeDataDir "settings.json") `
        -Value "{`"language`":`"en`",`"apiKeys`":{`"openaiApiKey`":`"$($secretValues[1])`"}}" `
        -Encoding UTF8
    Set-Content `
        -LiteralPath (Join-Path $logsPath "support-bundle-secret-smoke.log") `
        -Value "OPENAI_API_KEY=$($secretValues[2]) Authorization: Bearer $($secretValues[3])" `
        -Encoding UTF8

    $uri = "http://127.0.0.1:$Port/api/runtime/support-bundle"
    $unauthorizedStatus = $null
    try {
        Invoke-WebRequest -Method Post -Uri $uri -TimeoutSec 5 | Out-Null
        throw "Support bundle endpoint allowed an unauthenticated request."
    } catch {
        $response = $_.Exception.Response
        if ($response -and $response.StatusCode) {
            $unauthorizedStatus = [int]$response.StatusCode
        }
        if ($unauthorizedStatus -ne 401) {
            throw "Support bundle endpoint should reject unauthenticated requests with 401, got $unauthorizedStatus."
        }
    }

    $downloadDir = Join-Path $RuntimeDataDir "support-bundle-smoke"
    New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
    $downloadPath = Join-Path $downloadDir "support-bundle.zip"
    $headers = @{"X-Scriber-Token" = $Token}
    Invoke-WebRequest -Method Post -Uri $uri -Headers $headers -OutFile $downloadPath -TimeoutSec 20
    if (-not (Test-Path -LiteralPath $downloadPath -PathType Leaf)) {
        throw "Support bundle download did not create $downloadPath."
    }

    $downloadItem = Get-Item -LiteralPath $downloadPath
    if ($downloadItem.Length -le 0) {
        throw "Support bundle download was empty."
    }

    $zip = Read-ZipEntryText -Path $downloadPath
    $entrySet = @($zip.entries)
    foreach ($required in @("manifest.json", "runtime.json", "state.redacted.json", "environment.redacted.json", "config/env.redacted.txt", "logs/support-bundle-secret-smoke.log")) {
        if ($entrySet -notcontains $required) {
            throw "Support bundle is missing required entry: $required"
        }
    }
    $settingsEntry = if ($entrySet -contains "config/settings.redacted.json") {
        "config/settings.redacted.json"
    } elseif ($entrySet -contains "config/settings.redacted.txt") {
        "config/settings.redacted.txt"
    } else {
        throw "Support bundle is missing redacted settings entry."
    }

    $combined = [string]$zip.combinedText
    foreach ($secret in @($secretValues + @($Token))) {
        if ($secret -and $combined.Contains($secret)) {
            throw "Support bundle leaked a secret value."
        }
    }
    if (-not $combined.Contains("[REDACTED]")) {
        throw "Support bundle did not contain any redaction marker."
    }

    return [pscustomobject]@{
        verified = $true
        tokenProtected = $true
        unauthorizedStatus = $unauthorizedStatus
        downloadPath = $downloadPath
        downloadBytes = [int64]$downloadItem.Length
        entryCount = [int]$entrySet.Count
        redactionVerified = $true
        requiredEntries = @(
            "manifest.json",
            "runtime.json",
            "state.redacted.json",
            "environment.redacted.json",
            "config/env.redacted.txt",
            $settingsEntry,
            "logs/support-bundle-secret-smoke.log"
        )
    }
}

function Initialize-WindowsKeyboardInput {
    if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
        throw "Global hotkey simulation is only available on Windows."
    }
    if ([System.Type]::GetType("ScriberSmoke.KeyboardInput", $false)) {
        return
    }
    Add-Type -TypeDefinition @"
namespace ScriberSmoke {
    using System;
    using System.Runtime.InteropServices;

    public static class KeyboardInput {
        [StructLayout(LayoutKind.Sequential)]
        public struct INPUT {
            public uint type;
            public InputUnion u;
        }

        [StructLayout(LayoutKind.Explicit)]
        public struct InputUnion {
            [FieldOffset(0)]
            public MOUSEINPUT mi;
            [FieldOffset(0)]
            public KEYBDINPUT ki;
            [FieldOffset(0)]
            public HARDWAREINPUT hi;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct MOUSEINPUT {
            public int dx;
            public int dy;
            public uint mouseData;
            public uint dwFlags;
            public uint time;
            public UIntPtr dwExtraInfo;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct KEYBDINPUT {
            public ushort wVk;
            public ushort wScan;
            public uint dwFlags;
            public uint time;
            public UIntPtr dwExtraInfo;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct HARDWAREINPUT {
            public uint uMsg;
            public ushort wParamL;
            public ushort wParamH;
        }

        [DllImport("user32.dll", SetLastError = true)]
        public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

        [DllImport("user32.dll")]
        public static extern uint MapVirtualKey(uint uCode, uint uMapType);

        public static void SendKey(ushort virtualKey, bool keyUp) {
            INPUT input = new INPUT();
            input.type = 1;
            input.u.ki.wVk = virtualKey;
            input.u.ki.wScan = (ushort)MapVirtualKey(virtualKey, 0);
            input.u.ki.dwFlags = keyUp ? 0x0002u : 0u;
            input.u.ki.time = 0;
            input.u.ki.dwExtraInfo = UIntPtr.Zero;
            INPUT[] inputs = new INPUT[] { input };
            uint sent = SendInput(1, inputs, Marshal.SizeOf(typeof(INPUT)));
            if (sent != 1) {
                int error = Marshal.GetLastWin32Error();
                throw new InvalidOperationException("SendInput failed for virtual key " + virtualKey + " error=" + error);
            }
        }
    }
}
"@
}

function Convert-HotkeyPartToVirtualKey {
    param([string]$Part)

    $key = if ($Part) { $Part.Trim().ToLowerInvariant() } else { "" }
    switch ($key) {
        "ctrl" { return 0x11 }
        "control" { return 0x11 }
        "alt" { return 0x12 }
        "shift" { return 0x10 }
        "win" { return 0x5B }
        "meta" { return 0x5B }
    }
    if ($key -match "^f([1-9]|1[0-9]|2[0-4])$") {
        return 0x70 + ([int]$Matches[1]) - 1
    }
    if ($key -match "^[a-z]$") {
        return [byte][char]$key.ToUpperInvariant()
    }
    if ($key -match "^[0-9]$") {
        return [byte][char]$key
    }
    throw "Unsupported hotkey part for smoke simulation: '$Part'"
}

function Convert-HotkeyToKeyChord {
    param([string]$Hotkey)

    $parts = @($Hotkey.Split("+") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($parts.Count -lt 1) {
        throw "Global hotkey smoke requires a non-empty hotkey."
    }
    $primary = $parts[-1]
    $modifierParts = @()
    if ($parts.Count -gt 1) {
        $modifierParts = @($parts[0..($parts.Count - 2)])
    }
    return [pscustomobject]@{
        modifiers = @($modifierParts | ForEach-Object { Convert-HotkeyPartToVirtualKey -Part $_ })
        key = Convert-HotkeyPartToVirtualKey -Part $primary
    }
}

function Invoke-GlobalHotkeyChord {
    param([string]$Hotkey)

    Initialize-WindowsKeyboardInput
    $chord = Convert-HotkeyToKeyChord -Hotkey $Hotkey

    foreach ($modifier in $chord.modifiers) {
        [ScriberSmoke.KeyboardInput]::SendKey([UInt16]$modifier, $false)
        Start-Sleep -Milliseconds 25
    }
    [ScriberSmoke.KeyboardInput]::SendKey([UInt16]$chord.key, $false)
    Start-Sleep -Milliseconds 50
    [ScriberSmoke.KeyboardInput]::SendKey([UInt16]$chord.key, $true)
    $releaseModifiers = @($chord.modifiers)
    [array]::Reverse($releaseModifiers)
    foreach ($modifier in $releaseModifiers) {
        Start-Sleep -Milliseconds 25
        [ScriberSmoke.KeyboardInput]::SendKey([UInt16]$modifier, $true)
    }
}

function Wait-TextFileContains {
    param(
        [string]$Path,
        [string]$Pattern,
        [int]$DeadlineSec
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    do {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $content = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
            if ($content -and $content.Contains($Pattern)) {
                return $true
            }
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Get-MicTranscriptSummary {
    param(
        [int]$Port,
        [hashtable]$Headers = @{}
    )

    $payload = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/transcripts?type=mic&limit=5" -Headers $Headers -TimeoutSec 5
    $items = @($payload.items)
    $latest = if ($items.Count -gt 0) { $items[0] } else { $null }
    $preview = ""
    if ($latest) {
        if ($latest.PSObject.Properties.Name -contains "previewText") {
            $preview = [string]$latest.previewText
        } elseif ($latest.PSObject.Properties.Name -contains "_previewText") {
            $preview = [string]$latest._previewText
        }
    }
    return [pscustomobject]@{
        total = [int]$payload.total
        latestId = if ($latest) { $latest.id } else { "" }
        latestStatus = if ($latest) { $latest.status } else { "" }
        latestType = if ($latest) { $latest.type } else { "" }
        latestPreview = $preview
    }
}

function Initialize-GlobalHotkeySmokeData {
    param(
        [string]$RuntimeDataDir,
        [string]$Hotkey,
        [string]$Root
    )

    Assert-UnderRoot -Root (Join-Path $Root "tmp") -Path $RuntimeDataDir -Label "Global hotkey smoke DataDir"
    New-Item -ItemType Directory -Force -Path $RuntimeDataDir | Out-Null
    $envPath = Join-Path $RuntimeDataDir ".env"
    $invalidProvider = "__hotkey_smoke_invalid__"
    $lines = @(
        "SCRIBER_HOTKEY=$Hotkey",
        "SCRIBER_MODE=toggle",
        "SCRIBER_DEFAULT_STT=$invalidProvider",
        "SCRIBER_INJECT_METHOD=type",
        "SCRIBER_AUTO_SUMMARIZE=0"
    )
    Set-Content -LiteralPath $envPath -Value $lines -Encoding UTF8
    return [pscustomobject]@{
        envPath = $envPath
        hotkey = $Hotkey
        invalidProvider = $invalidProvider
    }
}

function Test-GlobalHotkeyRegistration {
    param(
        [string]$RuntimeDataDir,
        [string]$Hotkey,
        [string]$InvalidProvider,
        [int]$DeadlineSec
    )

    $shellLogPath = Join-Path $RuntimeDataDir "logs\tauri-shell.log"
    $expectedRegistration = "Global hotkey registered: $Hotkey (toggle)"
    if (-not (Wait-TextFileContains -Path $shellLogPath -Pattern $expectedRegistration -DeadlineSec $DeadlineSec)) {
        throw "Global hotkey registration was not observed in $shellLogPath."
    }
    return [pscustomobject]@{
        verified = $true
        hotkey = $Hotkey
        mode = "toggle"
        invalidProvider = $InvalidProvider
        shellLogPath = $shellLogPath
        registrationObserved = $true
        dispatchVerified = $false
    }
}

function Test-GlobalHotkeyDispatch {
    param(
        [int]$Port,
        [string]$Token,
        [string]$RuntimeDataDir,
        [string]$Hotkey,
        [string]$InvalidProvider,
        [int]$DeadlineSec,
        [ValidateSet("synthetic", "manual")]
        [string]$DispatchMethod = "synthetic"
    )

    $headers = @{}
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }

    $registration = Test-GlobalHotkeyRegistration `
        -RuntimeDataDir $RuntimeDataDir `
        -Hotkey $Hotkey `
        -InvalidProvider $InvalidProvider `
        -DeadlineSec $DeadlineSec

    $initialStateProbe = Invoke-TimedRestGet -Uri "http://127.0.0.1:$Port/api/state" -Headers $headers
    $initialTranscripts = Get-MicTranscriptSummary -Port $Port -Headers $headers
    if ($DispatchMethod -eq "manual") {
        Write-Warning "Manual global hotkey smoke: press '$Hotkey' within ${DeadlineSec}s."
    } else {
        Invoke-GlobalHotkeyChord -Hotkey $Hotkey
    }

    $observedStates = @()
    $finalState = $null
    $finalTranscripts = $initialTranscripts
    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    do {
        Start-Sleep -Milliseconds 500
        $stateProbe = Invoke-TimedRestGet -Uri "http://127.0.0.1:$Port/api/state" -Headers $headers
        $state = $stateProbe.payload
        $finalState = $state
        $observedStates += [pscustomobject]@{
            elapsedMs = $stateProbe.elapsedMs
            status = [string]$state.status
            recordingState = [string]$state.recordingState
            listening = [bool]$state.listening
            sessionId = if ($state.sessionId) { [string]$state.sessionId } else { "" }
        }
        $finalTranscripts = Get-MicTranscriptSummary -Port $Port -Headers $headers

        $stateChanged = (
            [string]$state.status -ne [string]$initialStateProbe.payload.status -or
            [string]$state.recordingState -ne [string]$initialStateProbe.payload.recordingState -or
            [bool]$state.listening -ne [bool]$initialStateProbe.payload.listening
        )
        $transcriptAdded = [int]$finalTranscripts.total -gt [int]$initialTranscripts.total
        if ($stateChanged -or $transcriptAdded) {
            break
        }
    } while ((Get-Date) -lt $deadline)

    if (-not $finalState) {
        throw "Global hotkey smoke did not collect any state after dispatch."
    }

    $addedTranscript = [int]$finalTranscripts.total -gt [int]$initialTranscripts.total
    $stateChangedFromInitial = (
        [string]$finalState.status -ne [string]$initialStateProbe.payload.status -or
        [string]$finalState.recordingState -ne [string]$initialStateProbe.payload.recordingState -or
        [bool]$finalState.listening -ne [bool]$initialStateProbe.payload.listening
    )
    if (-not ($addedTranscript -or $stateChangedFromInitial)) {
        throw "Global hotkey did not change backend state or create a mic transcript within ${DeadlineSec}s."
    }

    if ([bool]$finalState.listening) {
        try {
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/live-mic/stop" -Headers $headers -TimeoutSec 5 | Out-Null
        } catch {
            Write-Warning "Global hotkey smoke cleanup stop failed: $_"
        }
    }

    return [pscustomobject]@{
        verified = $true
        hotkey = $Hotkey
        mode = "toggle"
        invalidProvider = $InvalidProvider
        shellLogPath = $registration.shellLogPath
        registrationObserved = $true
        dispatchVerified = $true
        dispatchMethod = $DispatchMethod
        initialState = [pscustomobject]@{
            status = [string]$initialStateProbe.payload.status
            recordingState = [string]$initialStateProbe.payload.recordingState
            listening = [bool]$initialStateProbe.payload.listening
        }
        finalState = [pscustomobject]@{
            status = [string]$finalState.status
            recordingState = [string]$finalState.recordingState
            listening = [bool]$finalState.listening
        }
        initialTranscriptTotal = [int]$initialTranscripts.total
        finalTranscriptTotal = [int]$finalTranscripts.total
        transcriptAdded = $addedTranscript
        latestTranscriptStatus = [string]$finalTranscripts.latestStatus
        observedStates = $observedStates
    }
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
if ($LiveRecordingDurationSec -gt 0 -and ($SimulateBackendCrash -or $SimulateBackendShutdown -or $SimulateBackendStartupTimeout -or $AttachExternalBackend)) {
    throw "-LiveRecordingDurationSec cannot be combined with -SimulateBackendCrash, -SimulateBackendShutdown, -SimulateBackendStartupTimeout, or -AttachExternalBackend."
}
if ($LiveRecordingDurationSec -gt 0 -and ($VerifyGlobalHotkeyRegistration -or $SimulateGlobalHotkey -or $WaitForManualGlobalHotkey)) {
    throw "-LiveRecordingDurationSec cannot be combined with global hotkey smoke options because hotkey smokes override STT settings."
}
if (($SimulateGlobalHotkey -or $WaitForManualGlobalHotkey) -and ($SimulateBackendCrash -or $SimulateBackendShutdown -or $SimulateBackendStartupTimeout)) {
    throw "-SimulateGlobalHotkey and -WaitForManualGlobalHotkey cannot be combined with -SimulateBackendCrash, -SimulateBackendShutdown, or -SimulateBackendStartupTimeout."
}
if ($SimulateGlobalHotkey -and $WaitForManualGlobalHotkey) {
    throw "-SimulateGlobalHotkey cannot be combined with -WaitForManualGlobalHotkey."
}
if (-not $DataDir) {
    $DataDir = Join-Path $RepoRoot ("tmp\tauri-smoke-data\" + [System.Guid]::NewGuid().ToString("N"))
}
$DataDir = Convert-ToFullPath -Path $DataDir
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$globalHotkeySmokeConfig = $null
if ($VerifyGlobalHotkeyRegistration -or $SimulateGlobalHotkey -or $WaitForManualGlobalHotkey) {
    $globalHotkeySmokeConfig = Initialize-GlobalHotkeySmokeData -RuntimeDataDir $DataDir -Hotkey $GlobalHotkeySmokeHotkey -Root $RepoRoot
}
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
$oldTauriGlobalHotkey = $env:SCRIBER_TAURI_GLOBAL_HOTKEY
$oldScriberHotkey = $env:SCRIBER_HOTKEY
$oldScriberMode = $env:SCRIBER_MODE
$oldScriberDefaultStt = $env:SCRIBER_DEFAULT_STT
$oldScriberInjectMethod = $env:SCRIBER_INJECT_METHOD
$oldScriberAutoSummarize = $env:SCRIBER_AUTO_SUMMARIZE
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
if ($VerifyGlobalHotkeyRegistration -or $SimulateGlobalHotkey -or $WaitForManualGlobalHotkey) {
    $env:SCRIBER_TAURI_GLOBAL_HOTKEY = "1"
    $env:SCRIBER_HOTKEY = $globalHotkeySmokeConfig.hotkey
    $env:SCRIBER_MODE = "toggle"
    $env:SCRIBER_DEFAULT_STT = $globalHotkeySmokeConfig.invalidProvider
    $env:SCRIBER_INJECT_METHOD = "type"
    $env:SCRIBER_AUTO_SUMMARIZE = "0"
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
    $supportBundle = $null
    if ($VerifySupportBundle) {
        $supportBundle = Test-SupportBundle `
            -Port ([int]$listener.Port) `
            -Token $SessionToken `
            -RuntimeDataDir $DataDir
    }
    $globalHotkey = $null
    if ($SimulateGlobalHotkey) {
        $globalHotkey = Test-GlobalHotkeyDispatch `
            -Port ([int]$listener.Port) `
            -Token $SessionToken `
            -RuntimeDataDir $DataDir `
            -Hotkey $globalHotkeySmokeConfig.hotkey `
            -InvalidProvider $globalHotkeySmokeConfig.invalidProvider `
            -DeadlineSec $GlobalHotkeyDispatchTimeoutSec `
            -DispatchMethod "synthetic"
    } elseif ($WaitForManualGlobalHotkey) {
        $globalHotkey = Test-GlobalHotkeyDispatch `
            -Port ([int]$listener.Port) `
            -Token $SessionToken `
            -RuntimeDataDir $DataDir `
            -Hotkey $globalHotkeySmokeConfig.hotkey `
            -InvalidProvider $globalHotkeySmokeConfig.invalidProvider `
            -DeadlineSec $GlobalHotkeyDispatchTimeoutSec `
            -DispatchMethod "manual"
    } elseif ($VerifyGlobalHotkeyRegistration) {
        $globalHotkey = Test-GlobalHotkeyRegistration `
            -RuntimeDataDir $DataDir `
            -Hotkey $globalHotkeySmokeConfig.hotkey `
            -InvalidProvider $globalHotkeySmokeConfig.invalidProvider `
            -DeadlineSec $GlobalHotkeyDispatchTimeoutSec
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
    $liveRecording = Test-LiveRecordingStability `
        -AppProcess $app `
        -BackendPid ([int]$listener.BackendPid) `
        -Port ([int]$listener.Port) `
        -Token $SessionToken `
        -ExpectedRuntimeMode $expectedRuntimeMode `
        -DurationSec $LiveRecordingDurationSec `
        -ProbeIntervalSec $LiveRecordingProbeIntervalSec `
        -MaxWorkingSetGrowthMB $MaxLiveBackendWorkingSetGrowthMB `
        -MaxCpuPercent $MaxLiveCpuPercent `
        -StartTimeoutSec $LiveRecordingStartTimeoutSec `
        -StopTimeoutSec $LiveRecordingStopTimeoutSec
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
        -ProbeIntervalSec $StabilityProbeIntervalSec `
        -MaxWorkingSetGrowthMB $MaxBackendWorkingSetGrowthMB `
        -MaxIdleCpuPercent $MaxIdleCpuPercent
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
        supportBundle = $supportBundle
        globalHotkey = $globalHotkey
        startupTimeout = $startupTimeout
        crashRecovery = $crashRecovery
        controlledShutdown = $controlledShutdown
        liveRecording = $liveRecording
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
    $env:SCRIBER_TAURI_GLOBAL_HOTKEY = $oldTauriGlobalHotkey
    $env:SCRIBER_HOTKEY = $oldScriberHotkey
    $env:SCRIBER_MODE = $oldScriberMode
    $env:SCRIBER_DEFAULT_STT = $oldScriberDefaultStt
    $env:SCRIBER_INJECT_METHOD = $oldScriberInjectMethod
    $env:SCRIBER_AUTO_SUMMARIZE = $oldScriberAutoSummarize
    $env:SCRIBER_BACKEND_START_TIMEOUT_MS = $oldBackendStartTimeout
    $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_ONCE = $oldSimulateStartupTimeout
    $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER = $oldSimulateStartupTimeoutMarker

    if ($cleanupFailure) {
        throw $cleanupFailure
    }
}

Write-SmokeJson -Payload $result -Path $OutputPath -Root $RepoRoot
