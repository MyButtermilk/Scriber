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
With -VerifyFrontend, it verifies that installed frontend ownership stays in the
Tauri WebView bundle rather than the Python backend sidecar, verifies that Tauri
production origins can call /api/health and tokenized /api/runtime, and waits
until the actual Tauri WebView reports a tokenized frontend-ready beacon.
With -VerifyRealMediaWorkflows, it runs real installed backend file and YouTube
transcription workflows through the token-protected REST API and requires
completed transcript plus summary evidence.
With -VerifySingleInstance, it starts a second desktop process and verifies that
the single-instance mutex makes it exit without spawning another backend worker
and signals the primary process to restore its main window.
Use -SingleInstanceSecondArguments and -SingleInstanceForbiddenLogText to verify
that malformed/sensitive second-launch input is blocked without runtime log leaks.
With -VerifyAudioSidecarCleanup, it starts a same-install audio sidecar before
launch and verifies startup cleanup removes it and app exit leaves no same-path
audio sidecar processes behind.
With -VerifyShellMenuSmoke, it triggers the installed shell's smoke-only tray
menu path after startup checks and verifies Open Scriber plus Quit timing and
cleanup from the real Tauri shell process.

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
    [string]$LiveRecordingEnvFile = "",
    [string]$LiveRecordingDefaultStt = "",
    [string]$LiveRecordingSonioxMode = "",
    [switch]$DisableLiveTextInjection,
    [ValidateSet("", "rust-wasapi")]
    [string]$LiveRecordingAudioEngine = "",
    [ValidateSet("", "synthetic", "wasapi")]
    [string]$LiveRecordingRustAudioCaptureMode = "",
    [switch]$LiveRecordingMicAlwaysOn,
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
    [switch]$VerifyFrontend,
    [switch]$VerifyMeetingAudioDeviceTest,
    [switch]$VerifyRealMediaWorkflows,
    [switch]$VerifySingleInstance,
    [string[]]$SingleInstanceSecondArguments = @(),
    [string[]]$SingleInstanceForbiddenLogText = @(),
    [switch]$VerifyAudioSidecarCleanup,
    [switch]$VerifyShellMenuSmoke,
    [string]$ShellMenuSmokeActions = "show-window,quit",
    [int]$ShellMenuSmokeTimeoutSec = 30,
    [int]$ShellMenuSmokeActionDelayMs = 250,
    [string]$FrontendPerformanceGatePath = "",
    [string]$RealWorkflowYoutubeUrl = "https://www.youtube.com/watch?v=0wEjbSYNUM8",
    [int]$RealWorkflowFileTimeoutSec = 240,
    [int]$RealWorkflowYoutubeTimeoutSec = 420,
    [int]$RealWorkflowPollSec = 3,
    [switch]$RealWorkflowSkipFile,
    [switch]$RealWorkflowSkipYoutube,
    [switch]$RealWorkflowNoSummary,
    [string]$GlobalHotkeySmokeHotkey = "ctrl+alt+shift+f12",
    [string]$GlobalHotkeySmokeDefaultStt = "",
    [switch]$GlobalHotkeySkipStopCleanup,
    [int]$GlobalHotkeyDispatchTimeoutSec = 20,
    [int]$GlobalHotkeyPreDispatchSettleSec = 0,
    [int]$BackendStartupTimeoutMs = 3000,
    [int]$CrashRecoveryTimeoutSec = 75,
    [string]$LegacyDataDir = "",
    [switch]$VerifyLegacyDataMigration,
    [switch]$DisableDevFallback
)

$ErrorActionPreference = "Stop"
$DefaultBackendPort = 8765

if ($LiveRecordingRustAudioCaptureMode -and $LiveRecordingAudioEngine -ne "rust-wasapi") {
    throw "-LiveRecordingRustAudioCaptureMode requires -LiveRecordingAudioEngine rust-wasapi."
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

function Get-FrontendPerformanceDiagnostics {
    param(
        [int]$Port,
        [string]$Token,
        [Nullable[int64]]$AfterSequence = $null,
        [string]$SourceInstanceId = ""
    )

    $headers = @{}
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }
    $query = @()
    if ($null -ne $AfterSequence) {
        $query += "afterSequence=$AfterSequence"
    }
    if ($SourceInstanceId) {
        $query += "sourceInstanceId=$([Uri]::EscapeDataString($SourceInstanceId))"
    }
    $suffix = if ($query.Count -gt 0) { "?" + ($query -join "&") } else { "" }
    return Invoke-RestMethod `
        -Uri "http://127.0.0.1:$Port/api/runtime/frontend-performance$suffix" `
        -Headers $headers `
        -TimeoutSec 2
}

function Request-FrontendPerformanceFlush {
    param(
        [int]$Port,
        [string]$Token,
        [string]$SourceInstanceId
    )

    $headers = @{ "Content-Type" = "application/json" }
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }
    $body = @{
        apiVersion = "1"
        sourceInstanceId = $SourceInstanceId
    } | ConvertTo-Json -Compress
    return Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:$Port/api/runtime/frontend-performance/flush-request" `
        -Headers $headers `
        -Body $body `
        -TimeoutSec 2
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

function Test-MeetingAudioDeviceTest {
    param(
        [int]$Port,
        [string]$Token
    )

    $headers = @{ "Content-Type" = "application/json" }
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }
    $body = @{
        durationMs = 1000
        aecEnabled = $true
        playTestTone = $false
    } | ConvertTo-Json -Compress
    $response = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:$Port/api/meetings/device-test" `
        -Headers $headers `
        -Body $body `
        -TimeoutSec 15
    if (-not $response.available -or -not $response.aecActive) {
        throw "Synthetic Meeting audio device test did not report active AEC capture."
    }
    if ($response.audioPersisted -or $response.audioSentToProvider) {
        throw "Meeting audio device test persisted or sent synthetic audio."
    }
    $summaries = [ordered]@{}
    foreach ($source in @("microphone", "system", "mic_clean")) {
        $level = $response.sources.$source
        if (-not $level -or -not $level.active -or [int64]$level.frames -le 0 -or [int64]$level.audioFrames -le 0) {
            throw "Synthetic Meeting audio source '$source' delivered no active frames."
        }
        if ([string]$level.errorCode) {
            throw "Synthetic Meeting audio source '$source' reported $($level.errorCode)."
        }
        $summaries[$source] = [pscustomobject]@{
            frames = [int64]$level.frames
            audioFrames = [int64]$level.audioFrames
            rms = [Math]::Round([double]$level.rms, 6)
            peak = [Math]::Round([double]$level.peak, 6)
            active = [bool]$level.active
        }
    }
    if ([double]$summaries.microphone.peak -le 0 -or [double]$summaries.system.peak -le 0) {
        throw "Synthetic Meeting microphone/system signals were silent."
    }
    return [pscustomobject]@{
        verified = $true
        transport = "tauri-shell-ipc-to-rust-sidecar-frame-pipes"
        durationMs = [int]$response.durationMs
        aecActive = [bool]$response.aecActive
        testTonePlayed = [bool]$response.testTonePlayed
        audioPersisted = [bool]$response.audioPersisted
        audioSentToProvider = [bool]$response.audioSentToProvider
        sources = [pscustomobject]$summaries
    }
}

function Wait-BackendState {
    param(
        [int]$Port,
        [string]$Token,
        [scriptblock]$Predicate,
        [scriptblock]$FailurePredicate = $null,
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
            if ($FailurePredicate -and (& $FailurePredicate $state)) {
                $stateJson = try { $state | ConvertTo-Json -Compress -Depth 4 } catch { [string]$state }
                throw "Backend entered failure state while waiting for '$Label': $stateJson"
            }
            if (& $Predicate $state) {
                return $state
            }
        } catch {
            if ($_.Exception.Message -like "Backend entered failure state while waiting*") {
                throw
            }
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

function Convert-AudioDiagnosticsSummary {
    param([object]$AudioDiagnostics)

    $featureFlags = $AudioDiagnostics.featureFlags
    $microphone = $AudioDiagnostics.microphone
    $activeCapture = if ($microphone) { $microphone.activeCapture } else { $null }
    $source = if ($activeCapture) { $activeCapture.source } else { $null }
    $endpointSelection = if ($source) { $source.endpointSelection } else { $null }
    $fallbackCircuit = if ($microphone) { $microphone.rustAudioFallbackCircuit } else { $null }
    $framePipeFramesRead = if ($activeCapture -and $null -ne $activeCapture.framePipeFramesRead) { $activeCapture.framePipeFramesRead } elseif ($source) { $source.framePipeFramesRead } else { $null }
    $framePipeAudioFramesRead = if ($activeCapture -and $null -ne $activeCapture.framePipeAudioFramesRead) { $activeCapture.framePipeAudioFramesRead } elseif ($source) { $source.framePipeAudioFramesRead } else { $null }
    $framePipeSequenceErrorCount = if ($activeCapture -and $null -ne $activeCapture.framePipeSequenceErrorCount) { $activeCapture.framePipeSequenceErrorCount } elseif ($source) { $source.framePipeSequenceErrorCount } else { $null }
    $framePipeProtocolErrorCount = if ($activeCapture -and $null -ne $activeCapture.framePipeProtocolErrorCount) { $activeCapture.framePipeProtocolErrorCount } elseif ($source) { $source.framePipeProtocolErrorCount } else { $null }
    $framePipePrebufferAfterLiveCount = if ($activeCapture -and $null -ne $activeCapture.framePipePrebufferAfterLiveCount) { $activeCapture.framePipePrebufferAfterLiveCount } elseif ($source) { $source.framePipePrebufferAfterLiveCount } else { $null }
    $framePipeReaderEndReason = if ($activeCapture -and $null -ne $activeCapture.framePipeReaderEndReason) { [string]$activeCapture.framePipeReaderEndReason } elseif ($source) { [string]$source.framePipeReaderEndReason } else { "" }
    $midSessionFailureReason = if ($activeCapture -and $null -ne $activeCapture.midSessionFailureReason) { [string]$activeCapture.midSessionFailureReason } elseif ($source) { [string]$source.midSessionFailureReason } else { "" }

    return [pscustomobject]@{
        apiVersion = [string]$AudioDiagnostics.apiVersion
        runtimeMode = [string]$AudioDiagnostics.runtimeMode
        pid = $AudioDiagnostics.pid
        recordingState = [string]$AudioDiagnostics.recordingState
        featureFlags = [pscustomobject]@{
            audioEngine = [string]$featureFlags.audioEngine
            requestedAudioEngine = [string]$featureFlags.requestedAudioEngine
            rustAudioRequested = [bool]$featureFlags.rustAudioRequested
            rustAudioAvailable = [bool]$featureFlags.rustAudioAvailable
        }
        activeCapture = if ($activeCapture) {
            [pscustomobject]@{
                running = [bool]$activeCapture.running
                engine = [string]$activeCapture.engine
                requestedEngine = [string]$activeCapture.requestedEngine
                frameSource = [string]$activeCapture.frameSource
                engineFallbackReason = [string]$activeCapture.engineFallbackReason
                hasStream = [bool]$activeCapture.hasStream
                streamActive = [bool]$activeCapture.streamActive
                callbackCount = $activeCapture.callbackCount
                droppedFrameCount = $activeCapture.droppedFrameCount
                healthRestartCount = $activeCapture.healthRestartCount
                healthRestartThrottleCount = $activeCapture.healthRestartThrottleCount
                lastHealthFailureReason = [string]$activeCapture.lastHealthFailureReason
                lastHealthRestartReason = [string]$activeCapture.lastHealthRestartReason
                lastHealthRestartError = [string]$activeCapture.lastHealthRestartError
                lastHealthRestartThrottledReason = [string]$activeCapture.lastHealthRestartThrottledReason
                lastHealthRestartThrottleRemainingSeconds = $activeCapture.lastHealthRestartThrottleRemainingSeconds
                lastRustAudioMidSessionFailureReason = [string]$activeCapture.lastRustAudioMidSessionFailureReason
                nativeEndpointIdHash = [string]$activeCapture.nativeEndpointIdHash
                sourceFrameSource = if ($source) { [string]$source.frameSource } else { "" }
                sourceNativeEndpointIdHash = if ($source) { [string]$source.nativeEndpointIdHash } else { "" }
                sourceFramePipeReaderEndReason = if ($source) { [string]$source.framePipeReaderEndReason } else { "" }
                sourceMidSessionFailureReason = if ($source) { [string]$source.midSessionFailureReason } else { "" }
                sourceEndpointSelectionMode = if ($endpointSelection) { [string]$endpointSelection.mode } else { "" }
                sourceEndpointSelectionUsedDefault = if ($endpointSelection) { [bool]$endpointSelection.usedDefaultEndpoint } else { $false }
                sourceEndpointSelectionRequestedHash = if ($endpointSelection) { [string]$endpointSelection.requestedNativeEndpointIdHash } else { "" }
                sourceEndpointSelectionSelectedHash = if ($endpointSelection) { [string]$endpointSelection.selectedNativeEndpointIdHash } else { "" }
                framePipeFramesRead = $framePipeFramesRead
                framePipeAudioFramesRead = $framePipeAudioFramesRead
                framePipeSequenceErrorCount = $framePipeSequenceErrorCount
                framePipeProtocolErrorCount = $framePipeProtocolErrorCount
                framePipePrebufferAfterLiveCount = $framePipePrebufferAfterLiveCount
                framePipeReaderEndReason = $framePipeReaderEndReason
                midSessionFailureReason = $midSessionFailureReason
                rustPrewarmAdoption = if ($activeCapture.rustPrewarmAdoption) {
                    $adoption = $activeCapture.rustPrewarmAdoption
                    $signature = $adoption.signature
                    [pscustomobject]@{
                        adopted = [bool]$adoption.adopted
                        engine = [string]$adoption.engine
                        prewarmIdHash = [string]$adoption.prewarmIdHash
                        prewarm_idHash = [string]$adoption.prewarm_idHash
                        signature = if ($signature) {
                            [pscustomobject]@{
                                device_preference = [string]$signature.device_preference
                                sample_rate = $signature.sample_rate
                                target_channels = $signature.target_channels
                                block_size = $signature.block_size
                            }
                        } else {
                            $null
                        }
                    }
                } else {
                    $null
                }
                sidecarPid = $activeCapture.sidecarPid
                sidecarConnected = $activeCapture.sidecarConnected
            }
        } else {
            $null
        }
        rustAudioFallbackCircuit = if ($fallbackCircuit) {
            [pscustomobject]@{
                available = [bool]$fallbackCircuit.available
                open = [bool]$fallbackCircuit.open
                reason = [string]$fallbackCircuit.reason
                remainingSeconds = $fallbackCircuit.remainingSeconds
                cooldownSeconds = $fallbackCircuit.cooldownSeconds
            }
        } else {
            $null
        }
        microphone = if ($microphone) {
            [pscustomobject]@{
                micAlwaysOn = [bool]$microphone.micAlwaysOn
                idlePrewarmActive = [bool]$microphone.idlePrewarmActive
                prebufferMs = $microphone.prebufferMs
                prewarmEngine = if ($microphone.prewarm) { [string]$microphone.prewarm.engine } else { "" }
                prewarmActive = if ($microphone.prewarm) { [bool]$microphone.prewarm.active } else { $false }
                prewarmActiveCaptureResumeReadyCount = if ($microphone.prewarm) { $microphone.prewarm.activeCaptureResumeReadyCount } else { $null }
                prewarmActiveCaptureResumeFailedCount = if ($microphone.prewarm) { $microphone.prewarm.activeCaptureResumeFailedCount } else { $null }
                prewarmLastActiveCaptureResumeGapMs = if ($microphone.prewarm) { $microphone.prewarm.lastActiveCaptureResumeGapMs } else { $null }
                prewarmLastActiveCaptureStopToReadyMs = if ($microphone.prewarm) { $microphone.prewarm.lastActiveCaptureStopToReadyMs } else { $null }
                prewarmMaxActiveCaptureStopToReadyMs = if ($microphone.prewarm) { $microphone.prewarm.maxActiveCaptureStopToReadyMs } else { $null }
            }
        } else {
            $null
        }
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

function Get-ScriberProcessRole {
    param(
        [int]$ProcessId,
        [int]$AppPid,
        [int]$BackendPid,
        [string]$ProcessName
    )

    $name = ([string]$ProcessName).ToLowerInvariant()
    if ($ProcessId -eq $AppPid) {
        return "tauriShell"
    }
    if ($ProcessId -eq $BackendPid -or $name -eq "scriber-backend") {
        return "backend"
    }
    if ($name -eq "msedgewebview2") {
        return "webview2"
    }
    if ($name -eq "scriber-audio-sidecar") {
        return "audioSidecar"
    }
    if ($name -eq "scriber-diarization-sidecar") {
        return "diarizationSidecar"
    }
    if ($name -like "scriber*") {
        return "scriberChild"
    }
    return "otherDescendant"
}

function Get-ProcessTreeMetrics {
    param(
        [int]$AppPid,
        [int]$BackendPid
    )

    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $childrenByParent = @{}
    foreach ($proc in $allProcesses) {
        $parentId = [int]$proc.ParentProcessId
        if (-not $childrenByParent.ContainsKey($parentId)) {
            $childrenByParent[$parentId] = New-Object System.Collections.Generic.List[int]
        }
        $childrenByParent[$parentId].Add([int]$proc.ProcessId)
    }

    $seen = @{}
    $queue = New-Object System.Collections.Generic.Queue[int]
    foreach ($rootId in @($AppPid, $BackendPid)) {
        if ($rootId -gt 0 -and -not $seen.ContainsKey($rootId)) {
            $seen[$rootId] = $true
            $queue.Enqueue($rootId)
        }
    }
    while ($queue.Count -gt 0) {
        $currentId = $queue.Dequeue()
        if (-not $childrenByParent.ContainsKey($currentId)) {
            continue
        }
        foreach ($childId in $childrenByParent[$currentId]) {
            if (-not $seen.ContainsKey($childId)) {
                $seen[$childId] = $true
                $queue.Enqueue($childId)
            }
        }
    }

    $metrics = @()
    foreach ($processId in @($seen.Keys | ForEach-Object { [int]$_ } | Sort-Object)) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if (-not $process) {
            continue
        }
        $cim = $allProcesses | Where-Object { [int]$_.ProcessId -eq $processId } | Select-Object -First 1
        $parentPid = if ($cim) { [int]$cim.ParentProcessId } else { $null }
        $metrics += [pscustomobject]@{
            pid = [int]$processId
            parentPid = $parentPid
            name = [string]$process.ProcessName
            role = Get-ScriberProcessRole -ProcessId $processId -AppPid $AppPid -BackendPid $BackendPid -ProcessName $process.ProcessName
            cpuTotalSeconds = [Math]::Round([double]$process.TotalProcessorTime.TotalSeconds, 3)
            workingSetMb = [Math]::Round([double]$process.WorkingSet64 / 1MB, 2)
            privateBytesMb = [Math]::Round([double]$process.PrivateMemorySize64 / 1MB, 2)
        }
    }
    return $metrics
}

function Get-ProcessTreeMetricsSummary {
    param([object[]]$Samples)

    $rows = @()
    foreach ($sample in $Samples) {
        foreach ($metric in @($sample.processMetrics)) {
            if ($metric) {
                $rows += $metric
            }
        }
    }
    $summary = @()
    foreach ($role in @($rows | ForEach-Object { [string]$_.role } | Sort-Object -Unique)) {
        $roleRows = @($rows | Where-Object { [string]$_.role -eq $role })
        if (-not $roleRows.Count) {
            continue
        }
        $summary += [pscustomobject]@{
            role = $role
            pidCount = @($roleRows | ForEach-Object { [int]$_.pid } | Sort-Object -Unique).Count
            sampleCount = $roleRows.Count
            maxWorkingSetMb = [Math]::Round([double](($roleRows | Measure-Object -Property workingSetMb -Maximum).Maximum), 2)
            maxPrivateBytesMb = [Math]::Round([double](($roleRows | Measure-Object -Property privateBytesMb -Maximum).Maximum), 2)
            maxCpuTotalSeconds = [Math]::Round([double](($roleRows | Measure-Object -Property cpuTotalSeconds -Maximum).Maximum), 3)
        }
    }
    return $summary
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
        [double]$MaxIdleCpuPercent = 0,
        [bool]$CollectAudioDiagnostics = $false
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
    $lastProcessMetrics = @(Get-ProcessTreeMetrics -AppPid ([int]$AppProcess.Id) -BackendPid $BackendPid)
    $lastCpuTotals = @{}
    foreach ($metric in $lastProcessMetrics) {
        $lastCpuTotals[[int]$metric.pid] = [double]$metric.cpuTotalSeconds
    }
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
        $audioDiagnosticsProbe = $null
        $audioDiagnosticsSummary = $null
        if ($CollectAudioDiagnostics) {
            $audioDiagnosticsProbe = Invoke-TimedRestGet -Uri "http://127.0.0.1:$Port/api/runtime/audio-diagnostics" -Headers $headers
            $audioDiagnosticsSummary = Convert-AudioDiagnosticsSummary -AudioDiagnostics $audioDiagnosticsProbe.payload
        }

        $currentCpuSampleAt = Get-Date
        $currentProcessMetrics = @(Get-ProcessTreeMetrics -AppPid ([int]$AppProcess.Id) -BackendPid $BackendPid)
        $currentCpuTotals = @{}
        foreach ($metric in $currentProcessMetrics) {
            $currentCpuTotals[[int]$metric.pid] = [double]$metric.cpuTotalSeconds
        }
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
        $processTreeCpuTotal = 0.0
        $processTreeCpuMeasured = $false
        foreach ($metric in $currentProcessMetrics) {
            $metricPid = [int]$metric.pid
            $metricCpuPercent = Get-DeltaCpuPercent `
                -PreviousSeconds $lastCpuTotals[$metricPid] `
                -CurrentSeconds $currentCpuTotals[$metricPid] `
                -ElapsedSeconds $cpuElapsedSec `
                -LogicalProcessorCount $logicalProcessorCount
            if ($null -ne $metricCpuPercent) {
                $processTreeCpuTotal += [double]$metricCpuPercent
                $processTreeCpuMeasured = $true
            }
        }
        $processTreeCpuPercent = if ($processTreeCpuMeasured) {
            [Math]::Round($processTreeCpuTotal, 2)
        } else {
            $null
        }
        $totalWorkingSetMb = [Math]::Round(
            [double](($currentProcessMetrics | Measure-Object -Property workingSetMb -Sum).Sum),
            2
        )
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
            processTreeCpuPercent = $processTreeCpuPercent
            totalWorkingSetMb = $totalWorkingSetMb
            healthMs = $healthProbe.elapsedMs
            stateMs = $stateProbe.elapsedMs
            audioDiagnosticsMs = if ($audioDiagnosticsProbe) { $audioDiagnosticsProbe.elapsedMs } else { $null }
            audioDiagnostics = $audioDiagnosticsSummary
            processMetrics = $currentProcessMetrics
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
    $processTreeCpuValues = @($samples | Where-Object { $null -ne $_.processTreeCpuPercent } | ForEach-Object { [double]$_.processTreeCpuPercent })
    $processTreeCpuMax = if ($processTreeCpuValues.Count) { [double](($processTreeCpuValues | Measure-Object -Maximum).Maximum) } else { $null }
    $processTreeCpuAvg = if ($processTreeCpuValues.Count) { [Math]::Round([double](($processTreeCpuValues | Measure-Object -Average).Average), 2) } else { $null }
    $totalWorkingSetValues = @($samples | ForEach-Object { [double]$_.totalWorkingSetMb })
    $totalWorkingSetStart = if ($totalWorkingSetValues.Count) { [double]$totalWorkingSetValues[0] } else { $null }
    $totalWorkingSetEnd = if ($totalWorkingSetValues.Count) { [double]$totalWorkingSetValues[-1] } else { $null }
    $totalWorkingSetMax = if ($totalWorkingSetValues.Count) { [double](($totalWorkingSetValues | Measure-Object -Maximum).Maximum) } else { $null }
    $totalWorkingSetGrowth = if ($null -ne $totalWorkingSetStart -and $null -ne $totalWorkingSetEnd) { [Math]::Round($totalWorkingSetEnd - $totalWorkingSetStart, 2) } else { $null }
    $totalWorkingSetPeakGrowth = if ($null -ne $totalWorkingSetStart -and $null -ne $totalWorkingSetMax) { [Math]::Round($totalWorkingSetMax - $totalWorkingSetStart, 2) } else { $null }
    $processMetricsSummary = @(Get-ProcessTreeMetricsSummary -Samples $samples)
    if ($MaxIdleCpuPercent -gt 0 -and -not $processTreeCpuValues.Count) {
        throw "Stability smoke could not collect idle CPU samples."
    }
    if ($MaxIdleCpuPercent -gt 0 -and $null -ne $processTreeCpuAvg -and $processTreeCpuAvg -gt $MaxIdleCpuPercent) {
        throw "Stability smoke process-tree average idle CPU ${processTreeCpuAvg}% exceeded ${MaxIdleCpuPercent}%."
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
        totalWorkingSetStartMb = $totalWorkingSetStart
        totalWorkingSetEndMb = $totalWorkingSetEnd
        totalWorkingSetMaxMb = $totalWorkingSetMax
        totalWorkingSetGrowthMb = $totalWorkingSetGrowth
        totalWorkingSetPeakGrowthMb = $totalWorkingSetPeakGrowth
        appCpuMaxPercent = $appCpuMax
        backendCpuMaxPercent = $backendCpuMax
        combinedCpuMaxPercent = $combinedCpuMax
        combinedCpuAvgPercent = $combinedCpuAvg
        processTreeCpuMaxPercent = $processTreeCpuMax
        processTreeCpuAvgPercent = $processTreeCpuAvg
        maxIdleCpuPercent = if ($MaxIdleCpuPercent -gt 0) { $MaxIdleCpuPercent } else { $null }
        processMetricsSummary = $processMetricsSummary
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
        [int]$StopTimeoutSec = 60,
        [bool]$TextInjectionDisabled = $false,
        [bool]$MicAlwaysOn = $false
    )

    if ($DurationSec -le 0) {
        return $null
    }

    $startedState = $null
    $stability = $null
    $stopResponse = $null
    $stoppedState = $null
    $postStopAudioDiagnostics = $null
    try {
        $startResponse = Invoke-LiveMicStart -Port $Port -Token $Token
        $startedState = Wait-BackendState `
            -Port $Port `
            -Token $Token `
            -Predicate { param($state) ([string]$state.recordingState -eq "recording") -or ([bool]$state.listening) } `
            -FailurePredicate { param($state) ([string]$state.recordingState -eq "failed") -or ([string]$state.status -eq "Error") } `
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
            -MaxIdleCpuPercent $MaxCpuPercent `
            -CollectAudioDiagnostics $true
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
        if ($MicAlwaysOn) {
            $headers = @{}
            if ($Token) {
                $headers["X-Scriber-Token"] = $Token
            }
            $prewarmDeadline = (Get-Date).AddSeconds([Math]::Min(10, [Math]::Max(2, $StopTimeoutSec)))
            do {
                $postStopProbe = Invoke-TimedRestGet -Uri "http://127.0.0.1:$Port/api/runtime/audio-diagnostics" -Headers $headers
                $postStopSummary = Convert-AudioDiagnosticsSummary -AudioDiagnostics $postStopProbe.payload
                $postStopAudioDiagnostics = [pscustomobject]@{
                    elapsedMs = $postStopProbe.elapsedMs
                    audioDiagnostics = $postStopSummary
                }
                if ($postStopSummary.microphone -and [bool]$postStopSummary.microphone.prewarmActive) {
                    break
                }
                Start-Sleep -Milliseconds 250
            } while ((Get-Date) -lt $prewarmDeadline)
        }
        return [pscustomobject]@{
            verified = $true
            durationSec = $DurationSec
            probeIntervalSec = [Math]::Max(1, $ProbeIntervalSec)
            startResponseOk = [bool]($startResponse.ok -or ([string]$startedState.recordingState -eq "recording") -or [bool]$startedState.listening)
            startedRecordingState = [string]$startedState.recordingState
            startedListening = [bool]$startedState.listening
            stopResponseOk = [bool]($stopResponse.ok -or (([string]$stoppedState.recordingState -eq "idle") -and -not [bool]$stoppedState.listening))
            stoppedRecordingState = [string]$stoppedState.recordingState
            stoppedListening = [bool]$stoppedState.listening
            nonRecordingSampleCount = $nonRecordingSamples.Count
            textInjectionDisabled = $TextInjectionDisabled
            micAlwaysOn = $MicAlwaysOn
            stability = $stability
            postStopAudioDiagnostics = $postStopAudioDiagnostics
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

function Read-EnvFileAssignments {
    param([string]$Path)

    if (-not $Path) {
        return @()
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing LiveRecordingEnvFile: $Path"
    }
    $assignments = @()
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $separator = $trimmed.IndexOf("=")
        if ($separator -le 0) {
            continue
        }
        $name = $trimmed.Substring(0, $separator).Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            continue
        }
        $value = $trimmed.Substring($separator + 1)
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $assignments += [pscustomobject]@{
            Name = $name
            Value = $value
        }
    }
    return $assignments
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

function Write-Utf8NoBomJson {
    param(
        [string]$Path,
        [string]$Json
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Json, $encoding)
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
        Write-Utf8NoBomJson -Path $outputFull -Json $json
    }
    return $json
}

function Get-SmokeFailureDiagnostics {
    param(
        [int]$Port,
        [string]$Token
    )

    $headers = @{}
    if ($Token) {
        $headers["X-Scriber-Token"] = $Token
    }
    $state = $null
    $audioDiagnostics = $null
    $runtimeLogs = $null
    try {
        $state = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/state" -Headers $headers -TimeoutSec 5
    } catch {
        $state = [pscustomobject]@{ error = $_.Exception.Message }
    }
    try {
        $audioDiagnostics = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/runtime/audio-diagnostics" -Headers $headers -TimeoutSec 5
    } catch {
        $audioDiagnostics = [pscustomobject]@{ error = $_.Exception.Message }
    }
    try {
        $runtimeLogs = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/runtime/logs?limit=80" -Headers $headers -TimeoutSec 5
    } catch {
        $runtimeLogs = [pscustomobject]@{ error = $_.Exception.Message }
    }

    return [pscustomobject]@{
        state = $state
        audioDiagnostics = $audioDiagnostics
        runtimeLogs = $runtimeLogs
    }
}

function Read-ZipEntryText {
    param([string]$Path)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entries = @()
        $entryTexts = @{}
        $combined = New-Object System.Text.StringBuilder
        foreach ($entry in $archive.Entries) {
            $entries += $entry.FullName
            if ($entry.Length -le 0 -or $entry.Length -gt 2000000) {
                continue
            }
            $reader = New-Object System.IO.StreamReader($entry.Open(), [System.Text.Encoding]::UTF8, $true)
            try {
                $text = $reader.ReadToEnd()
                $entryTexts[$entry.FullName] = $text
                [void]$combined.AppendLine($text)
            } finally {
                $reader.Dispose()
            }
        }
        return [pscustomobject]@{
            entries = $entries
            entryTexts = $entryTexts
            combinedText = $combined.ToString()
        }
    } finally {
        $archive.Dispose()
    }
}

function Test-NativeDeviceEventDiagnostics {
    param(
        [Parameter(Mandatory = $true)]
        [object]$NativeDeviceEvents
    )

    if ($null -eq $NativeDeviceEvents.shellIpcAvailable) {
        throw "Support bundle native device-event diagnostics are missing shellIpcAvailable."
    }
    if (-not [bool]$NativeDeviceEvents.shellIpcAvailable) {
        return [pscustomobject]@{
            shellIpcAvailable = $false
            registrationVerified = $false
            skippedReason = "shellIpcUnavailable"
        }
    }

    if ($NativeDeviceEvents.monitorKind -ne "wasapi-imm-notification") {
        throw "Support bundle native device-event diagnostics reported unexpected monitorKind: $($NativeDeviceEvents.monitorKind)"
    }

    $platformSupported = $true
    if ($null -ne $NativeDeviceEvents.platformSupported) {
        $platformSupported = [bool]$NativeDeviceEvents.platformSupported
    }
    $requestedMode = [string]$NativeDeviceEvents.requestedMode
    $effectiveMode = [string]$NativeDeviceEvents.effectiveMode
    if ((-not $platformSupported) -or $effectiveMode -eq "unsupported-platform" -or $requestedMode -eq "disabled" -or $effectiveMode -eq "disabled") {
        return [pscustomobject]@{
            shellIpcAvailable = $true
            registrationVerified = $false
            skippedReason = "nativeDeviceEventsDisabledOrUnsupported"
            effectiveMode = $effectiveMode
        }
    }

    foreach ($required in @("available", "running", "registered", "comInitialized", "callbackAlive")) {
        if ($null -eq $NativeDeviceEvents.$required) {
            throw "Support bundle native device-event diagnostics are missing $required."
        }
        if (-not [bool]$NativeDeviceEvents.$required) {
            throw "Support bundle native device-event diagnostics did not prove $required=true."
        }
    }
    foreach ($counter in @("eventCount", "ignoredRenderCount", "debouncedEventCount", "postAttemptCount", "postSuccessCount", "postFailureCount")) {
        if ($null -eq $NativeDeviceEvents.$counter) {
            throw "Support bundle native device-event diagnostics are missing $counter."
        }
    }

    return [pscustomobject]@{
        shellIpcAvailable = $true
        registrationVerified = $true
        effectiveMode = $effectiveMode
        registered = [bool]$NativeDeviceEvents.registered
        comInitialized = [bool]$NativeDeviceEvents.comInitialized
        callbackAlive = [bool]$NativeDeviceEvents.callbackAlive
    }
}

function Test-RustAudioFallbackCircuitDiagnostics {
    param(
        [Parameter(Mandatory = $true)]
        [object]$RustAudioFallbackCircuit
    )

    foreach ($required in @("available", "open", "cooldownSeconds")) {
        if ($null -eq $RustAudioFallbackCircuit.$required) {
            throw "Support bundle Rust audio fallback-circuit diagnostics are missing $required."
        }
    }
    if (-not [bool]$RustAudioFallbackCircuit.available) {
        throw "Support bundle Rust audio fallback-circuit diagnostics did not prove available=true."
    }
    if (-not ($RustAudioFallbackCircuit.open -is [bool])) {
        throw "Support bundle Rust audio fallback-circuit diagnostics open must be boolean."
    }
    if ($null -ne $RustAudioFallbackCircuit.cooldownSeconds -and [double]$RustAudioFallbackCircuit.cooldownSeconds -lt 0) {
        throw "Support bundle Rust audio fallback-circuit diagnostics cooldownSeconds must be non-negative."
    }

    $remainingSeconds = $RustAudioFallbackCircuit.remainingSeconds
    if ($null -ne $remainingSeconds -and [double]$remainingSeconds -lt 0) {
        throw "Support bundle Rust audio fallback-circuit diagnostics remainingSeconds must be non-negative when present."
    }
    if ([bool]$RustAudioFallbackCircuit.open) {
        if (-not [string]$RustAudioFallbackCircuit.reason) {
            throw "Support bundle Rust audio fallback-circuit diagnostics reason is required when open=true."
        }
        if ($null -eq $remainingSeconds) {
            throw "Support bundle Rust audio fallback-circuit diagnostics remainingSeconds is required when open=true."
        }
    }

    return [pscustomobject]@{
        available = [bool]$RustAudioFallbackCircuit.available
        open = [bool]$RustAudioFallbackCircuit.open
        reason = [string]$RustAudioFallbackCircuit.reason
        remainingSeconds = $remainingSeconds
        cooldownSeconds = $RustAudioFallbackCircuit.cooldownSeconds
    }
}

function Wait-FrontendReady {
    param(
        [int]$Port,
        [string]$Token,
        [int]$DeadlineSec = 25
    )

    if (-not $Token) {
        throw "Frontend WebView readiness probe requires a session token."
    }

    $headers = @{ "X-Scriber-Token" = $Token }
    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/runtime/frontend-ready" -Headers $headers -TimeoutSec 2
            if ($status.ready -and $status.lastSeen -and $status.lastSeen.tauriRuntime) {
                return $status
            }
        } catch {
            # Backend may still be waiting for the first WebView request.
        }
        Start-Sleep -Milliseconds 500
    }

    throw "Tauri WebView did not report frontend-ready within ${DeadlineSec}s."
}

function Test-FrontendHttp {
    param(
        [int]$Port,
        [string]$Token = ""
    )

    $baseUrl = "http://127.0.0.1:$Port"
    $rootUrl = "$baseUrl/"
    $backendRootStatusCode = $null
    $backendRootBytes = 0
    $backendStaticFallbackAvailable = $false
    try {
        $rootResponse = Invoke-WebRequest -Uri $rootUrl -TimeoutSec 10 -UseBasicParsing
        $backendRootStatusCode = [int]$rootResponse.StatusCode
        $html = [string]$rootResponse.Content
        $backendRootBytes = [int]$html.Length
        $backendStaticFallbackAvailable = $html.Contains('id="root"') -and ($html -match "<script")
    } catch {
        $response = $_.Exception.Response
        if ($response -and $response.StatusCode) {
            $backendRootStatusCode = [int]$response.StatusCode
        } else {
            throw
        }
    }

    if ($backendStaticFallbackAvailable) {
        throw "Backend static fallback unexpectedly served frontend assets; installed frontend assets must be owned by the Tauri WebView bundle."
    }
    if ($backendRootStatusCode -ne 404) {
        throw "Backend frontend root returned HTTP $backendRootStatusCode; expected 404 because installed frontend assets are not embedded in the Python sidecar."
    }

    $tauriOrigin = "http://tauri.localhost"
    $originHeaders = @{ Origin = $tauriOrigin }
    $preflightHeaders = @{
        Origin = $tauriOrigin
        "Access-Control-Request-Method" = "GET"
        "Access-Control-Request-Private-Network" = "true"
    }
    $preflightResponse = Invoke-WebRequest -Method Options -Uri "$baseUrl/api/health" -Headers $preflightHeaders -TimeoutSec 10 -UseBasicParsing
    if ([int]$preflightResponse.StatusCode -ne 204) {
        throw "Frontend CORS private-network preflight returned HTTP $($preflightResponse.StatusCode)."
    }
    $privateNetworkAllowed = [string]$preflightResponse.Headers["Access-Control-Allow-Private-Network"]
    if ($privateNetworkAllowed -ne "true") {
        throw "Frontend CORS private-network preflight did not allow private network access."
    }

    $healthResponse = Invoke-WebRequest -Uri "$baseUrl/api/health" -Headers $originHeaders -TimeoutSec 10 -UseBasicParsing
    if ([int]$healthResponse.StatusCode -ne 200) {
        throw "Frontend CORS health probe returned HTTP $($healthResponse.StatusCode)."
    }
    $healthAllowedOrigin = [string]$healthResponse.Headers["Access-Control-Allow-Origin"]
    if ($healthAllowedOrigin -ne $tauriOrigin) {
        throw "Frontend CORS health probe returned Access-Control-Allow-Origin '$healthAllowedOrigin' instead of '$tauriOrigin'."
    }

    $runtimeCorsVerified = $false
    if ($Token) {
        $encodedToken = [uri]::EscapeDataString($Token)
        $runtimeResponse = Invoke-WebRequest -Uri "$baseUrl/api/runtime?scriberToken=$encodedToken" -Headers $originHeaders -TimeoutSec 10 -UseBasicParsing
        if ([int]$runtimeResponse.StatusCode -ne 200) {
            throw "Frontend CORS runtime probe returned HTTP $($runtimeResponse.StatusCode)."
        }
        $runtimeAllowedOrigin = [string]$runtimeResponse.Headers["Access-Control-Allow-Origin"]
        if ($runtimeAllowedOrigin -ne $tauriOrigin) {
            throw "Frontend CORS runtime probe returned Access-Control-Allow-Origin '$runtimeAllowedOrigin' instead of '$tauriOrigin'."
        }
        $runtimeCorsVerified = $true
    }

    $frontendReady = Wait-FrontendReady -Port $Port -Token $Token
    $lastSeen = $frontendReady.lastSeen
    if ([string]$lastSeen.backendBaseUrl -ne $baseUrl) {
        throw "Tauri WebView reported backendBaseUrl '$($lastSeen.backendBaseUrl)' instead of '$baseUrl'."
    }
    if ([string]$lastSeen.locationOrigin -ne $tauriOrigin) {
        throw "Tauri WebView reported locationOrigin '$($lastSeen.locationOrigin)' instead of '$tauriOrigin'."
    }
    if ([string]$lastSeen.origin -ne $tauriOrigin) {
        throw "Tauri WebView frontend-ready request used Origin '$($lastSeen.origin)' instead of '$tauriOrigin'."
    }

    return [pscustomobject]@{
        verified = $true
        source = "tauri-webview"
        backendStaticFallbackUrl = $rootUrl
        backendStaticFallbackStatusCode = [int]$backendRootStatusCode
        backendStaticFallbackAvailable = $false
        backendStaticFallbackBytes = [int]$backendRootBytes
        assetCount = 0
        verifiedAssetCount = 0
        tauriOriginCors = $true
        privateNetworkPreflight = $true
        runtimeCorsVerified = $runtimeCorsVerified
        webViewReady = [bool]$frontendReady.ready
        webViewReadyAt = [string]$lastSeen.receivedAt
        webViewTauriRuntime = [bool]$lastSeen.tauriRuntime
        webViewBackendBaseUrl = [string]$lastSeen.backendBaseUrl
        webViewLocationOrigin = [string]$lastSeen.locationOrigin
        webViewRequestOrigin = [string]$lastSeen.origin
        assets = @()
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
    $dummyShellPipe = "\\.\pipe\scriber-shell-support-bundle-smoke"
    $dummyEndpointId = "SWD\MMDEVAPI\{0.0.1.00000000}.{support-bundle-capture-smoke}"
    $shellPipePattern = '(?i)(?:\\\\){1,2}\.(?:\\){1,2}pipe(?:\\){1,2}scriber-shell-[A-Za-z0-9_.-]+'
    $endpointIdPattern = '(?i)SWD(?:\\+|#)+MMDEVAPI(?:\\+|#)+[^\s"'',;<>]+'

    $envPath = Join-Path $RuntimeDataDir ".env"
    $settingsPath = Join-Path $RuntimeDataDir "settings.json"
    $configSnapshots = @(
        [pscustomobject]@{
            Path = $envPath
            Exists = Test-Path -LiteralPath $envPath -PathType Leaf
            Bytes = if (Test-Path -LiteralPath $envPath -PathType Leaf) { [System.IO.File]::ReadAllBytes($envPath) } else { $null }
        },
        [pscustomobject]@{
            Path = $settingsPath
            Exists = Test-Path -LiteralPath $settingsPath -PathType Leaf
            Bytes = if (Test-Path -LiteralPath $settingsPath -PathType Leaf) { [System.IO.File]::ReadAllBytes($settingsPath) } else { $null }
        }
    )

    $logsPath = Join-Path $RuntimeDataDir "logs"
    New-Item -ItemType Directory -Force -Path $logsPath | Out-Null
    try {
        Set-Content `
            -LiteralPath $envPath `
            -Value "OPENAI_API_KEY=$($secretValues[0])`nSCRIBER_MODE=toggle`nSCRIBER_SHELL_IPC_PIPE=$dummyShellPipe" `
            -Encoding UTF8
        Set-Content `
            -LiteralPath $settingsPath `
            -Value "{`"language`":`"en`",`"apiKeys`":{`"openaiApiKey`":`"$($secretValues[1])`"}}" `
            -Encoding UTF8
        Set-Content `
            -LiteralPath (Join-Path $logsPath "support-bundle-secret-smoke.log") `
            -Value "OPENAI_API_KEY=$($secretValues[2]) Authorization: Bearer $($secretValues[3]) pipe=$dummyShellPipe endpoint=$dummyEndpointId" `
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
        $meetingPrivacyFindings = [System.Collections.Generic.List[string]]::new()
        $forbiddenSuffixes = @(".aac", ".bin", ".blob", ".db", ".db-shm", ".db-wal", ".flac", ".m4a", ".mp3", ".npy", ".npz", ".ogg", ".opus", ".pcm", ".sqlite", ".sqlite3", ".wav", ".webm")
        $forbiddenMarkers = @("meeting_audio", "outlook_refresh_token", "speaker_observation", "speaker_profile", "transcript_export", "voice_embedding", "voiceprint", "webhook_secret")
        foreach ($entryName in $entrySet) {
            $normalizedEntry = ([string]$entryName).ToLowerInvariant()
            foreach ($suffix in $forbiddenSuffixes) {
                if ($normalizedEntry.EndsWith($suffix)) {
                    [void]$meetingPrivacyFindings.Add("forbidden-suffix:$suffix")
                }
            }
            foreach ($marker in $forbiddenMarkers) {
                if ($normalizedEntry.Contains($marker)) {
                    [void]$meetingPrivacyFindings.Add("forbidden-marker:$marker")
                }
            }
        }
        if ($meetingPrivacyFindings.Count -gt 0) {
            $categories = @($meetingPrivacyFindings | Sort-Object -Unique)
            throw "Support bundle contains Meeting-sensitive artifact categories: $($categories -join ', ')"
        }
        foreach ($required in @("manifest.json", "runtime.json", "state.redacted.json", "audio-diagnostics.redacted.json", "environment.redacted.json", "config/env.redacted.txt", "logs/support-bundle-secret-smoke.log")) {
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
        if ($combined.Contains($dummyShellPipe) -or ([regex]::IsMatch($combined, $shellPipePattern))) {
            throw "Support bundle leaked a raw Shell IPC pipe name."
        }
        if ($combined.Contains($dummyEndpointId) -or $combined.Contains("support-bundle-capture-smoke") -or ([regex]::IsMatch($combined, $endpointIdPattern))) {
            throw "Support bundle leaked a raw native audio endpoint ID."
        }
        if (-not $combined.Contains("[REDACTED]")) {
            throw "Support bundle did not contain any redaction marker."
        }
        if (-not $combined.Contains("[REDACTED_PIPE]")) {
            throw "Support bundle did not contain the Shell IPC pipe redaction marker."
        }
        if (-not $combined.Contains("[REDACTED_ENDPOINT_ID]")) {
            throw "Support bundle did not contain the native audio endpoint redaction marker."
        }
        $audioDiagnostics = $zip.entryTexts["audio-diagnostics.redacted.json"] | ConvertFrom-Json
        if (-not $audioDiagnostics.microphone.nativeDeviceEvents) {
            throw "Support bundle audio diagnostics are missing microphone.nativeDeviceEvents."
        }
        if (-not $audioDiagnostics.microphone.rustAudioFallbackCircuit) {
            throw "Support bundle audio diagnostics are missing microphone.rustAudioFallbackCircuit."
        }
        $nativeDeviceEvents = Test-NativeDeviceEventDiagnostics -NativeDeviceEvents $audioDiagnostics.microphone.nativeDeviceEvents
        $rustAudioFallbackCircuit = Test-RustAudioFallbackCircuitDiagnostics -RustAudioFallbackCircuit $audioDiagnostics.microphone.rustAudioFallbackCircuit

        return [pscustomobject]@{
            verified = $true
            tokenProtected = $true
            unauthorizedStatus = $unauthorizedStatus
            downloadPath = $downloadPath
            downloadBytes = [int64]$downloadItem.Length
            entryCount = [int]$entrySet.Count
            redactionVerified = $true
            meetingPrivacy = [pscustomobject]@{
                verified = $true
                sensitiveFindingCount = 0
                audioAbsent = $true
                transcriptStoresAbsent = $true
                outlookCredentialArtifactsAbsent = $true
                webhookSecretArtifactsAbsent = $true
                voiceprintArtifactsAbsent = $true
            }
            nativeDeviceEvents = $nativeDeviceEvents
            rustAudioFallbackCircuit = $rustAudioFallbackCircuit
            requiredEntries = @(
                "manifest.json",
                "runtime.json",
                "state.redacted.json",
                "audio-diagnostics.redacted.json",
                "environment.redacted.json",
                "config/env.redacted.txt",
                $settingsEntry,
                "logs/support-bundle-secret-smoke.log"
            )
        }
    } finally {
        foreach ($snapshot in $configSnapshots) {
            if ($snapshot.Exists) {
                [System.IO.File]::WriteAllBytes($snapshot.Path, [byte[]]$snapshot.Bytes)
            } elseif (Test-Path -LiteralPath $snapshot.Path -PathType Leaf) {
                Remove-Item -LiteralPath $snapshot.Path -Force
            }
        }
    }
}

function Test-RealMediaWorkflows {
    param(
        [int]$Port,
        [string]$Token,
        [string]$RuntimeDataDir
    )

    if (-not $Token) {
        throw "Real media workflow smoke requires a session token."
    }

    $outputPath = Join-Path $RuntimeDataDir "installed-real-media-workflows-smoke.json"
    $workflowArgs = @(
        "scripts\smoke_installed_transcription_workflows.py",
        "--base-url",
        "http://127.0.0.1:$Port",
        "--token-env",
        "SCRIBER_SMOKE_SESSION_TOKEN",
        "--output",
        $outputPath,
        "--youtube-url",
        $RealWorkflowYoutubeUrl,
        "--file-timeout-sec",
        $RealWorkflowFileTimeoutSec.ToString(),
        "--youtube-timeout-sec",
        $RealWorkflowYoutubeTimeoutSec.ToString(),
        "--poll-sec",
        $RealWorkflowPollSec.ToString()
    )
    if ($RealWorkflowSkipFile) {
        $workflowArgs += "--skip-file"
    }
    if ($RealWorkflowSkipYoutube) {
        $workflowArgs += "--skip-youtube"
    }
    if ($RealWorkflowNoSummary) {
        $workflowArgs += "--no-require-summary"
    }

    $oldSmokeToken = $env:SCRIBER_SMOKE_SESSION_TOKEN
    $env:SCRIBER_SMOKE_SESSION_TOKEN = $Token
    Push-Location $RepoRoot
    try {
        python @workflowArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Installed real media workflow smoke failed with exit code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
        $env:SCRIBER_SMOKE_SESSION_TOKEN = $oldSmokeToken
    }

    if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
        throw "Installed real media workflow smoke did not write output: $outputPath"
    }
    $report = Get-Content -LiteralPath $outputPath -Raw | ConvertFrom-Json
    if (-not $report.ok) {
        throw "Installed real media workflow smoke wrote ok=false: $outputPath"
    }

    return [pscustomobject]@{
        verified = $true
        reportPath = $outputPath
        report = $report
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
        public struct POINT {
            public int x;
            public int y;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct MSG {
            public IntPtr hwnd;
            public uint message;
            public UIntPtr wParam;
            public IntPtr lParam;
            public uint time;
            public POINT pt;
        }

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

        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);

        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool UnregisterHotKey(IntPtr hWnd, int id);

        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool PeekMessage(out MSG lpMsg, IntPtr hWnd, uint wMsgFilterMin, uint wMsgFilterMax, uint wRemoveMsg);

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

function Convert-VirtualKeyToHotkeyModifier {
    param([int]$VirtualKey)

    switch ($VirtualKey) {
        0x10 { return 0x0004 } # MOD_SHIFT
        0x11 { return 0x0002 } # MOD_CONTROL
        0x12 { return 0x0001 } # MOD_ALT
        0x5B { return 0x0008 } # MOD_WIN
        default { throw "Unsupported modifier virtual key for synthetic hotkey probe: $VirtualKey" }
    }
}

function Test-SyntheticGlobalHotkeyDispatchSupport {
    param(
        [string]$ProbeHotkey = "ctrl+alt+shift+f24",
        [int]$DeadlineMs = 1500
    )

    Initialize-WindowsKeyboardInput
    $chord = Convert-HotkeyToKeyChord -Hotkey $ProbeHotkey
    $modifiers = 0
    foreach ($modifier in $chord.modifiers) {
        $modifiers = $modifiers -bor (Convert-VirtualKeyToHotkeyModifier -VirtualKey ([int]$modifier))
    }

    $probeId = 0x5348
    $registered = [ScriberSmoke.KeyboardInput]::RegisterHotKey(
        [IntPtr]::Zero,
        $probeId,
        [UInt32]$modifiers,
        [UInt32]$chord.key
    )
    if (-not $registered) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw "Synthetic global hotkey support preflight could not register probe hotkey '$ProbeHotkey' (Win32 error $errorCode)."
    }

    try {
        Invoke-GlobalHotkeyChord -Hotkey $ProbeHotkey
        $deadline = (Get-Date).AddMilliseconds($DeadlineMs)
        do {
            $msg = New-Object ScriberSmoke.KeyboardInput+MSG
            if ([ScriberSmoke.KeyboardInput]::PeekMessage([ref]$msg, [IntPtr]::Zero, 0, 0, 0x0001)) {
                $wParam = [int]$msg.wParam.ToUInt64()
                if ($msg.message -eq 0x0312 -and $wParam -eq $probeId) {
                    return $true
                }
            }
            Start-Sleep -Milliseconds 20
        } while ((Get-Date) -lt $deadline)
        return $false
    } finally {
        [void][ScriberSmoke.KeyboardInput]::UnregisterHotKey([IntPtr]::Zero, $probeId)
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

function Get-HotPathSegmentValue {
    param(
        [object]$Segments,
        [string]$Name
    )

    if (-not $Segments) {
        return $null
    }
    $property = $Segments.PSObject.Properties | Where-Object { $_.Name -eq $Name } | Select-Object -First 1
    if (-not $property) {
        return $null
    }
    return $property.Value
}

function Convert-HotPathMilliseconds {
    param([object]$Value)

    if ($null -eq $Value) {
        return $null
    }
    try {
        return [Math]::Round([double]$Value, 3)
    } catch {
        return $null
    }
}

function Select-HotPathMetricItem {
    param(
        [object]$Metrics,
        [string]$SessionId
    )

    if (-not $Metrics) {
        return $null
    }
    $candidates = @()
    if ($Metrics.activeItems) {
        $candidates += @($Metrics.activeItems)
    }
    if ($Metrics.items) {
        $candidates += @($Metrics.items)
    }
    if ($candidates.Count -eq 0) {
        return $null
    }
    if ($SessionId) {
        $match = @($candidates | Where-Object { [string]$_.sessionId -eq $SessionId } | Select-Object -First 1)
        if ($match.Count -gt 0) {
            return $match[0]
        }
    }
    return $candidates[0]
}

function Convert-GlobalHotkeyUserReady {
    param([object]$HotPathItem)

    if (-not $HotPathItem) {
        return [pscustomobject]@{
            verified = $false
            sessionId = ""
            micReadyObserved = $false
            firstAudioFrameObserved = $false
            firstAudibleAudioFrameObserved = $false
            hotkeyToMicReadyMs = $null
            hotkeyToFirstAudioFrameMs = $null
            hotkeyToFirstAudibleAudioFrameMs = $null
            markerNames = @()
            hotPathMetrics = $null
        }
    }

    $segments = $HotPathItem.segments
    $micReadyMs = Convert-HotPathMilliseconds -Value (Get-HotPathSegmentValue -Segments $segments -Name "hotkey_received_to_mic_ready_ms")
    $firstAudioFrameMs = Convert-HotPathMilliseconds -Value (Get-HotPathSegmentValue -Segments $segments -Name "hotkey_received_to_first_audio_frame_ms")
    $firstAudibleAudioFrameMs = Convert-HotPathMilliseconds -Value (Get-HotPathSegmentValue -Segments $segments -Name "hotkey_received_to_first_audible_audio_frame_ms")

    return [pscustomobject]@{
        verified = ($null -ne $micReadyMs -and $null -ne $firstAudioFrameMs)
        sessionId = if ($HotPathItem.sessionId) { [string]$HotPathItem.sessionId } else { "" }
        micReadyObserved = ($null -ne $micReadyMs)
        firstAudioFrameObserved = ($null -ne $firstAudioFrameMs)
        firstAudibleAudioFrameObserved = ($null -ne $firstAudibleAudioFrameMs)
        hotkeyToMicReadyMs = $micReadyMs
        hotkeyToFirstAudioFrameMs = $firstAudioFrameMs
        hotkeyToFirstAudibleAudioFrameMs = $firstAudibleAudioFrameMs
        markerNames = @($HotPathItem.markerNames)
        hotPathMetrics = $HotPathItem
    }
}

function Wait-GlobalHotkeyUserReady {
    param(
        [int]$Port,
        [hashtable]$Headers = @{},
        [string]$SessionId,
        [int]$DeadlineSec
    )

    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $DeadlineSec))
    $lastReady = $null
    do {
        try {
            $metrics = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/metrics/hot-path?limit=10&includeActive=1" -Headers $Headers -TimeoutSec 5
            $item = Select-HotPathMetricItem -Metrics $metrics -SessionId $SessionId
            if ($item) {
                $lastReady = Convert-GlobalHotkeyUserReady -HotPathItem $item
                if ($lastReady.verified) {
                    return $lastReady
                }
            }
        } catch {
            $lastReady = [pscustomobject]@{
                verified = $false
                sessionId = $SessionId
                micReadyObserved = $false
                firstAudioFrameObserved = $false
                firstAudibleAudioFrameObserved = $false
                hotkeyToMicReadyMs = $null
                hotkeyToFirstAudioFrameMs = $null
                hotkeyToFirstAudibleAudioFrameMs = $null
                markerNames = @()
                hotPathMetrics = $null
                error = $_.Exception.Message
            }
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)

    if ($lastReady) {
        return $lastReady
    }
    return Convert-GlobalHotkeyUserReady -HotPathItem $null
}

function Initialize-GlobalHotkeySmokeData {
    param(
        [string]$RuntimeDataDir,
        [string]$Hotkey,
        [string]$DefaultStt,
        [string]$Root
    )

    Assert-UnderRoot -Root (Join-Path $Root "tmp") -Path $RuntimeDataDir -Label "Global hotkey smoke DataDir"
    New-Item -ItemType Directory -Force -Path $RuntimeDataDir | Out-Null
    $envPath = Join-Path $RuntimeDataDir ".env"
    $invalidProvider = "__hotkey_smoke_invalid__"
    $effectiveDefaultStt = if ([string]::IsNullOrWhiteSpace($DefaultStt)) { $invalidProvider } else { $DefaultStt.Trim() }
    $lines = @(
        "SCRIBER_HOTKEY=$Hotkey",
        "SCRIBER_POST_PROCESSING_ENABLED=1",
        "SCRIBER_POST_PROCESSING_HOTKEY=ctrl+shift+f",
        "SCRIBER_MEETING_HOTKEY=ctrl+shift+m",
        "SCRIBER_MODE=toggle",
        "SCRIBER_DEFAULT_STT=$effectiveDefaultStt",
        "SCRIBER_INJECT_METHOD=type",
        "SCRIBER_AUTO_SUMMARIZE=0"
    )
    Set-Content -LiteralPath $envPath -Value $lines -Encoding UTF8
    return [pscustomobject]@{
        envPath = $envPath
        hotkey = $Hotkey
        invalidProvider = $invalidProvider
        defaultStt = $effectiveDefaultStt
    }
}

function Test-GlobalHotkeyRegistration {
    param(
        [string]$RuntimeDataDir,
        [string]$Hotkey,
        [string]$InvalidProvider,
        [string]$DefaultStt,
        [int]$DeadlineSec
    )

    $shellLogPath = Join-Path $RuntimeDataDir "logs\tauri-shell.log"
    $expectedRegistration = "Global hotkey registered: $Hotkey (toggle), post-processing: ctrl+shift+f, meeting: ctrl+shift+m"
    if (-not (Wait-TextFileContains -Path $shellLogPath -Pattern $expectedRegistration -DeadlineSec $DeadlineSec)) {
        throw "Global hotkey registration was not observed in $shellLogPath."
    }
    return [pscustomobject]@{
        verified = $true
        hotkey = $Hotkey
        mode = "toggle"
        invalidProvider = $InvalidProvider
        defaultStt = $DefaultStt
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
        [string]$DefaultStt,
        [int]$DeadlineSec,
        [bool]$SkipStopCleanup = $false,
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
        -DefaultStt $DefaultStt `
        -DeadlineSec $DeadlineSec

    $initialStateProbe = Invoke-TimedRestGet -Uri "http://127.0.0.1:$Port/api/state" -Headers $headers
    $initialTranscripts = Get-MicTranscriptSummary -Port $Port -Headers $headers
    $dispatchStartQpcTicks = $null
    $dispatchQpcFrequency = [System.Diagnostics.Stopwatch]::Frequency
    if ($DispatchMethod -eq "manual") {
        $dispatchStartQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
        Write-Warning "Manual global hotkey smoke: press '$Hotkey' within ${DeadlineSec}s."
    } else {
        if (-not (Test-SyntheticGlobalHotkeyDispatchSupport)) {
            throw "Synthetic global hotkey dispatch is unavailable on this host: Windows SendInput did not trigger a probe RegisterHotKey. Use -WaitForManualGlobalHotkey for physical OS hotkey evidence."
        }
        $dispatchStartQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
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

    $userReadySessionId = if ($finalState.sessionId) { [string]$finalState.sessionId } else { "" }
    if (-not $userReadySessionId) {
        $observedSession = @(
            $observedStates |
                Where-Object { $_.sessionId } |
                Select-Object -Last 1
        )
        if ($observedSession.Count -gt 0) {
            $userReadySessionId = [string]$observedSession[0].sessionId
        }
    }
    $userReady = Wait-GlobalHotkeyUserReady `
        -Port $Port `
        -Headers $headers `
        -SessionId $userReadySessionId `
        -DeadlineSec ([Math]::Max(2, [Math]::Min(10, $DeadlineSec)))

    $stopSkipped = $false
    if ([bool]$finalState.listening -and $SkipStopCleanup) {
        $stopSkipped = $true
    } elseif ([bool]$finalState.listening) {
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
        defaultStt = $DefaultStt
        shellLogPath = $registration.shellLogPath
        registrationObserved = $true
        dispatchVerified = $true
        dispatchMethod = $DispatchMethod
        dispatchStartQpcTicks = $dispatchStartQpcTicks
        qpcFrequency = $dispatchQpcFrequency
        stopSkipped = $stopSkipped
        userReady = $userReady
        hotPathMetrics = if ($userReady) { $userReady.hotPathMetrics } else { $null }
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

function Get-InstalledAudioSidecarProcesses {
    param([string]$InstallRoot)

    if (-not $InstallRoot -or -not (Test-Path -LiteralPath $InstallRoot -PathType Container)) {
        return @()
    }
    $rootFull = (Convert-ToFullPath -Path $InstallRoot).ToLowerInvariant()
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $name = [string]$_.Name
                if ($name -notmatch "^scriber-audio-sidecar") {
                    return $false
                }
                $cmd = if ($_.CommandLine) { $_.CommandLine.ToLowerInvariant() } else { "" }
                $exe = if ($_.ExecutablePath) { $_.ExecutablePath.ToLowerInvariant() } else { "" }
                $exe.StartsWith($rootFull) -or $cmd.Contains($rootFull)
            } |
            ForEach-Object {
                [pscustomobject]@{
                    processId = [int]$_.ProcessId
                    name = [string]$_.Name
                    executablePath = [string]$_.ExecutablePath
                }
            }
    )
}

function Resolve-InstalledAudioSidecarExe {
    param([string]$InstallRoot)

    $candidates = @(
        (Join-Path $InstallRoot "scriber-audio-sidecar.exe"),
        (Join-Path $InstallRoot "audio-sidecar\scriber-audio-sidecar.exe"),
        (Join-Path $InstallRoot "resources\audio-sidecar\scriber-audio-sidecar.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $match = Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -Filter "scriber-audio-sidecar.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($match) {
        return $match.FullName
    }
    throw "Installed audio sidecar executable was not found under $InstallRoot."
}

function Start-InstalledAudioSidecarStray {
    param([string]$AudioSidecarPath)

    $sidecarPath = (Resolve-Path -LiteralPath $AudioSidecarPath).Path
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $sidecarPath
    $startInfo.Arguments = "--stdio"
    $startInfo.WorkingDirectory = Split-Path $sidecarPath
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Failed to start installed audio sidecar cleanup probe."
    }
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) {
        throw "Installed audio sidecar cleanup probe exited before Tauri startup."
    }
    return [pscustomobject]@{
        process = $process
        processId = [int]$process.Id
        executablePath = $sidecarPath
    }
}

function Test-SingleInstanceBlock {
    param(
        [string]$ExePath,
        [string]$RuntimeDataDir,
        [System.Diagnostics.Process]$PrimaryAppProcess,
        [int]$DeadlineSec,
        [string[]]$SecondArgumentList = @(),
        [string[]]$ForbiddenLogText = @()
    )

    $backendBaseline = @(Get-ManagedBackendProcesses | ForEach-Object { [int]$_.ProcessId })
    $startProcessParams = @{
        FilePath = $ExePath
        WorkingDirectory = (Split-Path $ExePath)
        WindowStyle = "Hidden"
        PassThru = $true
    }
    if ($SecondArgumentList -and $SecondArgumentList.Count -gt 0) {
        $startProcessParams.ArgumentList = $SecondArgumentList
    }
    $second = Start-Process @startProcessParams
    try {
        if (-not $second.WaitForExit([Math]::Max(1, $DeadlineSec) * 1000)) {
            Stop-Process -Id $second.Id -Force -ErrorAction SilentlyContinue
            throw "Second Tauri process did not exit within ${DeadlineSec}s."
        }
        Assert-NoNewBackendListeners -BaselinePids $backendBaseline -WaitSec 2
        if ($PrimaryAppProcess.HasExited) {
            throw "Primary Tauri process exited while verifying single-instance behavior."
        }
        $shellLogPath = Join-Path $RuntimeDataDir "logs\tauri-shell.log"
        $logObserved = Wait-TextFileContains `
            -Path $shellLogPath `
            -Pattern "another Scriber desktop instance is already running" `
            -DeadlineSec 5
        if (-not $logObserved) {
            throw "Single-instance mutex message was not observed in $shellLogPath."
        }
        $restoreObserved = Wait-TextFileContains `
            -Path $shellLogPath `
            -Pattern "single-instance main-window restore requested" `
            -DeadlineSec 5
        if (-not $restoreObserved) {
            throw "Single-instance main-window restore signal was not observed in $shellLogPath."
        }
        $forbiddenLogFileCount = 0
        if ($ForbiddenLogText -and $ForbiddenLogText.Count -gt 0) {
            $logPaths = @(
                $shellLogPath,
                (Join-Path $RuntimeDataDir "logs\tauri-backend.log")
            )
            foreach ($logPath in $logPaths) {
                if (-not (Test-Path -LiteralPath $logPath -PathType Leaf)) {
                    continue
                }
                $forbiddenLogFileCount += 1
                $content = Get-Content -Raw -LiteralPath $logPath
                foreach ($probe in $ForbiddenLogText) {
                    if ($probe -and $content.Contains($probe)) {
                        throw "Single-instance forbidden probe text leaked to runtime logs."
                    }
                }
            }
        }
        return [pscustomobject]@{
            verified = $true
            secondProcessId = [int]$second.Id
            secondExitCode = [int]$second.ExitCode
            secondArgumentCount = if ($SecondArgumentList) { [int]$SecondArgumentList.Count } else { 0 }
            noManagedBackendSpawned = $true
            primaryStillRunning = -not $PrimaryAppProcess.HasExited
            shellLogPath = $shellLogPath
            shellLogObserved = $logObserved
            mainWindowRestoreObserved = $restoreObserved
            forbiddenLogTextAbsent = if ($ForbiddenLogText -and $ForbiddenLogText.Count -gt 0) { $true } else { $null }
            forbiddenLogFileCount = $forbiddenLogFileCount
        }
    } finally {
        if ($second -and -not $second.HasExited) {
            Stop-Process -Id $second.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-ShellMenuSmoke {
    param(
        [System.Diagnostics.Process]$AppProcess,
        [string]$RuntimeDataDir,
        [string]$TriggerPath,
        [string]$Actions,
        [int]$DeadlineSec,
        [int]$Port,
        [string]$Token,
        [string]$QuitBarrierPath,
        [string]$MeasurementGatePath
    )

    $shellLogPath = Join-Path $RuntimeDataDir "logs\tauri-shell.log"
    New-Item -ItemType Directory -Force -Path (Split-Path $TriggerPath) | Out-Null
    $frontendPerformanceBaseline = $null
    $frontendPerformanceAfterShow = $null
    $frontendPerformanceAfterShowQpcTicks = $null
    $frontendPerformanceFlushRequest = $null
    $frontendPerformanceMeasurementEndQpcTicks = $null
    $frontendPerformanceHeartbeatAckQpcTicks = $null
    try {
        $frontendPerformanceBaseline = Get-FrontendPerformanceDiagnostics -Port $Port -Token $Token
    } catch {
        $frontendPerformanceBaseline = $null
    }
    $triggerQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
    $triggerQpcFrequency = [System.Diagnostics.Stopwatch]::Frequency
    Set-Content -LiteralPath $TriggerPath -Value "go" -Encoding ASCII

    $showMatch = $null
    $copyRecentMatch = $null
    $hotkeyPressMatch = $null
    $hotkeyReleaseMatch = $null
    $overlayInitializingMatch = $null
    $overlayRecordingMatch = $null
    $overlayTranscribingMatch = $null
    $overlayHideMatch = $null
    $quitMatch = $null
    $copyRecentRequested = [string]::IsNullOrWhiteSpace($Actions) -eq $false -and $Actions -match "(?i)(^|[,;\s])(copy-recent|copy-recent-transcript|recent-copy)([,;\s]|$)"
    $hotkeyPressRequested = [string]::IsNullOrWhiteSpace($Actions) -eq $false -and $Actions -match "(?i)(^|[,;\s])(hotkey-press|push-to-talk-press|ptt-press)([,;\s]|$)"
    $hotkeyReleaseRequested = [string]::IsNullOrWhiteSpace($Actions) -eq $false -and $Actions -match "(?i)(^|[,;\s])(hotkey-release|push-to-talk-release|ptt-release)([,;\s]|$)"
    $overlayInitializingRequested = [string]::IsNullOrWhiteSpace($Actions) -eq $false -and $Actions -match "(?i)(^|[,;\s])(overlay-initializing|overlay-preparing)([,;\s]|$)"
    $overlayRecordingRequested = [string]::IsNullOrWhiteSpace($Actions) -eq $false -and $Actions -match "(?i)(^|[,;\s])(overlay-recording)([,;\s]|$)"
    $overlayTranscribingRequested = [string]::IsNullOrWhiteSpace($Actions) -eq $false -and $Actions -match "(?i)(^|[,;\s])(overlay-transcribing|overlay-finalizing)([,;\s]|$)"
    $overlayHideRequested = [string]::IsNullOrWhiteSpace($Actions) -eq $false -and $Actions -match "(?i)(^|[,;\s])(overlay-hide|hide-overlay)([,;\s]|$)"
    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    do {
        if (Test-Path -LiteralPath $shellLogPath -PathType Leaf) {
            $content = Get-Content -LiteralPath $shellLogPath -Raw -ErrorAction SilentlyContinue
            if (-not $showMatch -and $content) {
                $candidate = [regex]::Match(
                    $content,
                    "shell menu smoke action show-window completed elapsedMs=(\d+) visible=(true|false) hideSucceeded=(true|false)"
                )
                if ($candidate.Success) {
                    $showMatch = $candidate
                    if ($candidate.Groups[2].Value -ne "true") {
                        throw "Shell menu smoke Open Scriber did not leave the main window visible."
                    }
                }
            }
            if ($copyRecentRequested -and -not $copyRecentMatch -and $content) {
                $candidate = [regex]::Match(
                    $content,
                    "shell menu smoke action copy-recent completed elapsedMs=(\d+) copied=(true|false) transcriptId=([A-Za-z0-9_-]+)"
                )
                if ($candidate.Success) {
                    $copyRecentMatch = $candidate
                    if ($candidate.Groups[2].Value -ne "true") {
                        throw "Shell menu smoke Recent Transcript copy did not report copied=true."
                    }
                }
            }
            if ($hotkeyPressRequested -and -not $hotkeyPressMatch -and $content) {
                $candidate = [regex]::Match(
                    $content,
                    "shell menu smoke action hotkey-press completed elapsedMs=(\d+) mode=([A-Za-z0-9_]+) path=([^ ]+) dispatched=(true|false) posted=(true|false) error=([^\r\n]*)"
                )
                if ($candidate.Success) {
                    $hotkeyPressMatch = $candidate
                    if ($candidate.Groups[2].Value -ne "push_to_talk" -or $candidate.Groups[3].Value -ne "/api/live-mic/start" -or $candidate.Groups[4].Value -ne "true") {
                        throw "Shell menu smoke push-to-talk press did not dispatch /api/live-mic/start in push_to_talk mode."
                    }
                }
            }
            if ($hotkeyReleaseRequested -and -not $hotkeyReleaseMatch -and $content) {
                $candidate = [regex]::Match(
                    $content,
                    "shell menu smoke action hotkey-release completed elapsedMs=(\d+) mode=([A-Za-z0-9_]+) path=([^ ]+) dispatched=(true|false) posted=(true|false) error=([^\r\n]*)"
                )
                if ($candidate.Success) {
                    $hotkeyReleaseMatch = $candidate
                    if ($candidate.Groups[2].Value -ne "push_to_talk" -or $candidate.Groups[3].Value -ne "/api/live-mic/stop-request" -or $candidate.Groups[4].Value -ne "true") {
                        throw "Shell menu smoke push-to-talk release did not dispatch /api/live-mic/stop-request in push_to_talk mode."
                    }
                }
            }
            if ($overlayInitializingRequested -and -not $overlayInitializingMatch -and $content) {
                $candidate = [regex]::Match(
                    $content,
                    "shell menu smoke action overlay-initializing completed elapsedMs=(\d+) mode=([A-Za-z0-9_]+) visible=(true|false) available=(true|false)"
                )
                if ($candidate.Success) {
                    $overlayInitializingMatch = $candidate
                    if ($candidate.Groups[2].Value -ne "initializing" -or $candidate.Groups[3].Value -ne "true" -or $candidate.Groups[4].Value -ne "true") {
                        throw "Shell menu smoke overlay-initializing did not report visible initializing state."
                    }
                }
            }
            if ($overlayRecordingRequested -and -not $overlayRecordingMatch -and $content) {
                $candidate = [regex]::Match(
                    $content,
                    "shell menu smoke action overlay-recording completed elapsedMs=(\d+) mode=([A-Za-z0-9_]+) visible=(true|false) available=(true|false)"
                )
                if ($candidate.Success) {
                    $overlayRecordingMatch = $candidate
                    if ($candidate.Groups[2].Value -ne "recording" -or $candidate.Groups[3].Value -ne "true" -or $candidate.Groups[4].Value -ne "true") {
                        throw "Shell menu smoke overlay-recording did not report visible recording state."
                    }
                }
            }
            if ($overlayTranscribingRequested -and -not $overlayTranscribingMatch -and $content) {
                $candidate = [regex]::Match(
                    $content,
                    "shell menu smoke action overlay-transcribing completed elapsedMs=(\d+) mode=([A-Za-z0-9_]+) visible=(true|false) available=(true|false)"
                )
                if ($candidate.Success) {
                    $overlayTranscribingMatch = $candidate
                    if ($candidate.Groups[2].Value -ne "transcribing" -or $candidate.Groups[3].Value -ne "true" -or $candidate.Groups[4].Value -ne "true") {
                        throw "Shell menu smoke overlay-transcribing did not report visible transcribing state."
                    }
                }
            }
            if ($overlayHideRequested -and -not $overlayHideMatch -and $content) {
                $candidate = [regex]::Match(
                    $content,
                    "shell menu smoke action overlay-hide completed elapsedMs=(\d+) mode=([A-Za-z0-9_]+) visible=(true|false) available=(true|false)"
                )
                if ($candidate.Success) {
                    $overlayHideMatch = $candidate
                    if ($candidate.Groups[2].Value -ne "hidden" -or $candidate.Groups[3].Value -ne "false" -or $candidate.Groups[4].Value -ne "true") {
                        throw "Shell menu smoke overlay-hide did not report hidden state."
                    }
                }
            }
            if (-not $quitMatch -and $content) {
                $candidate = [regex]::Match(
                    $content,
                    "shell menu smoke action quit completed elapsedMs=(\d+) stoppedSidecars=(\d+) exitRequested=true"
                )
                if ($candidate.Success) {
                    $quitMatch = $candidate
                }
            }
            if ($showMatch -and -not $quitMatch -and $frontendPerformanceBaseline) {
                try {
                    $baselineWindow = $frontendPerformanceBaseline.window
                    if (
                        $frontendPerformanceBaseline.available -and
                        $frontendPerformanceBaseline.observerSupported -eq $true -and
                        $baselineWindow
                    ) {
                        $measurementGateOpen = (
                            -not $MeasurementGatePath -or
                            (Test-Path -LiteralPath $MeasurementGatePath -PathType Leaf)
                        )
                        if (-not $frontendPerformanceFlushRequest -and $measurementGateOpen) {
                            $frontendPerformanceMeasurementEndQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
                            $frontendPerformanceFlushRequest = Request-FrontendPerformanceFlush `
                                -Port $Port `
                                -Token $Token `
                                -SourceInstanceId ([string]$frontendPerformanceBaseline.sourceInstanceId)
                        }
                        $frontendPerformanceAfterShow = Get-FrontendPerformanceDiagnostics `
                            -Port $Port `
                            -Token $Token `
                            -AfterSequence ([int64]$baselineWindow.lastSequence) `
                            -SourceInstanceId ([string]$frontendPerformanceBaseline.sourceInstanceId)
                        $frontendPerformanceAfterShowQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
                        $afterWindow = $frontendPerformanceAfterShow.window
                        if (
                            $frontendPerformanceFlushRequest -and
                            $afterWindow -and
                            [int64]$afterWindow.heartbeatSequence -ge [int64]$frontendPerformanceFlushRequest.heartbeatSequence -and
                            [double]$afterWindow.heartbeatObservedAtFrontendUptimeMs -ge [double]$frontendPerformanceFlushRequest.requestedAfterFrontendUptimeMs -and
                            [double]$afterWindow.heartbeatReceivedAtUptimeSeconds -ge [double]$frontendPerformanceFlushRequest.requestedAtUptimeSeconds
                        ) {
                            $frontendPerformanceHeartbeatAckQpcTicks = $frontendPerformanceAfterShowQpcTicks
                            if ($QuitBarrierPath -and -not (Test-Path -LiteralPath $QuitBarrierPath)) {
                                Set-Content -LiteralPath $QuitBarrierPath -Value "acked" -Encoding ASCII
                            }
                        }
                    }
                } catch {
                    # Keep polling until the bounded barrier timeout. The
                    # benchmark treats missing acknowledgement as unknown.
                }
            }
            if (
                $showMatch -and
                $quitMatch -and
                (-not $copyRecentRequested -or $copyRecentMatch) -and
                (-not $hotkeyPressRequested -or $hotkeyPressMatch) -and
                (-not $hotkeyReleaseRequested -or $hotkeyReleaseMatch) -and
                (-not $overlayInitializingRequested -or $overlayInitializingMatch) -and
                (-not $overlayRecordingRequested -or $overlayRecordingMatch) -and
                (-not $overlayTranscribingRequested -or $overlayTranscribingMatch) -and
                (-not $overlayHideRequested -or $overlayHideMatch)
            ) {
                break
            }
        }
        Start-Sleep -Milliseconds 50
    } while ((Get-Date) -lt $deadline)

    if (-not $showMatch) {
        throw "Shell menu smoke did not observe the Open Scriber marker in $shellLogPath."
    }
    if (-not $quitMatch) {
        throw "Shell menu smoke did not observe the Quit marker in $shellLogPath."
    }
    if ($copyRecentRequested -and -not $copyRecentMatch) {
        throw "Shell menu smoke did not observe the Recent Transcript copy marker in $shellLogPath."
    }
    if ($hotkeyPressRequested -and -not $hotkeyPressMatch) {
        throw "Shell menu smoke did not observe the push-to-talk press marker in $shellLogPath."
    }
    if ($hotkeyReleaseRequested -and -not $hotkeyReleaseMatch) {
        throw "Shell menu smoke did not observe the push-to-talk release marker in $shellLogPath."
    }
    if ($overlayInitializingRequested -and -not $overlayInitializingMatch) {
        throw "Shell menu smoke did not observe the overlay-initializing marker in $shellLogPath."
    }
    if ($overlayRecordingRequested -and -not $overlayRecordingMatch) {
        throw "Shell menu smoke did not observe the overlay-recording marker in $shellLogPath."
    }
    if ($overlayTranscribingRequested -and -not $overlayTranscribingMatch) {
        throw "Shell menu smoke did not observe the overlay-transcribing marker in $shellLogPath."
    }
    if ($overlayHideRequested -and -not $overlayHideMatch) {
        throw "Shell menu smoke did not observe the overlay-hide marker in $shellLogPath."
    }

    $remaining = [Math]::Max(1, [int][Math]::Ceiling(($deadline - (Get-Date)).TotalSeconds))
    Wait-ProcessExit -ProcessId ([int]$AppProcess.Id) -DeadlineSec $remaining

    return [pscustomobject]@{
        verified = $true
        shellLogPath = $shellLogPath
        triggerPath = $TriggerPath
        triggerQpcTicks = $triggerQpcTicks
        qpcFrequency = $triggerQpcFrequency
        frontendPerformance = [pscustomobject]@{
            baseline = $frontendPerformanceBaseline
            afterShow = $frontendPerformanceAfterShow
            flushRequest = $frontendPerformanceFlushRequest
            heartbeatAcknowledged = [bool]$frontendPerformanceHeartbeatAckQpcTicks
            measurementEndQpcTicks = $frontendPerformanceMeasurementEndQpcTicks
            heartbeatAckQpcTicks = $frontendPerformanceHeartbeatAckQpcTicks
            measurementWindowMs = if ($frontendPerformanceMeasurementEndQpcTicks) {
                [Math]::Round(
                    (($frontendPerformanceMeasurementEndQpcTicks - $triggerQpcTicks) / $triggerQpcFrequency) * 1000.0,
                    3
                )
            } else {
                $null
            }
            heartbeatAckLatencyMs = if (
                $frontendPerformanceHeartbeatAckQpcTicks -and
                $frontendPerformanceMeasurementEndQpcTicks
            ) {
                [Math]::Round(
                    (($frontendPerformanceHeartbeatAckQpcTicks - $frontendPerformanceMeasurementEndQpcTicks) / $triggerQpcFrequency) * 1000.0,
                    3
                )
            } else {
                $null
            }
        }
        showWindow = [pscustomobject]@{
            elapsedMs = [int]$showMatch.Groups[1].Value
            visible = ($showMatch.Groups[2].Value -eq "true")
            hideSucceeded = ($showMatch.Groups[3].Value -eq "true")
        }
        copyRecent = if ($copyRecentMatch) {
            [pscustomobject]@{
                elapsedMs = [int]$copyRecentMatch.Groups[1].Value
                copied = ($copyRecentMatch.Groups[2].Value -eq "true")
                transcriptId = $copyRecentMatch.Groups[3].Value
            }
        } else {
            $null
        }
        hotkeyPress = if ($hotkeyPressMatch) {
            [pscustomobject]@{
                elapsedMs = [int]$hotkeyPressMatch.Groups[1].Value
                mode = $hotkeyPressMatch.Groups[2].Value
                path = $hotkeyPressMatch.Groups[3].Value
                dispatched = ($hotkeyPressMatch.Groups[4].Value -eq "true")
                posted = ($hotkeyPressMatch.Groups[5].Value -eq "true")
                error = $hotkeyPressMatch.Groups[6].Value
            }
        } else {
            $null
        }
        hotkeyRelease = if ($hotkeyReleaseMatch) {
            [pscustomobject]@{
                elapsedMs = [int]$hotkeyReleaseMatch.Groups[1].Value
                mode = $hotkeyReleaseMatch.Groups[2].Value
                path = $hotkeyReleaseMatch.Groups[3].Value
                dispatched = ($hotkeyReleaseMatch.Groups[4].Value -eq "true")
                posted = ($hotkeyReleaseMatch.Groups[5].Value -eq "true")
                error = $hotkeyReleaseMatch.Groups[6].Value
            }
        } else {
            $null
        }
        overlay = [pscustomobject]@{
            initializing = if ($overlayInitializingMatch) {
                [pscustomobject]@{
                    elapsedMs = [int]$overlayInitializingMatch.Groups[1].Value
                    mode = $overlayInitializingMatch.Groups[2].Value
                    visible = ($overlayInitializingMatch.Groups[3].Value -eq "true")
                    available = ($overlayInitializingMatch.Groups[4].Value -eq "true")
                }
            } else {
                $null
            }
            recording = if ($overlayRecordingMatch) {
                [pscustomobject]@{
                    elapsedMs = [int]$overlayRecordingMatch.Groups[1].Value
                    mode = $overlayRecordingMatch.Groups[2].Value
                    visible = ($overlayRecordingMatch.Groups[3].Value -eq "true")
                    available = ($overlayRecordingMatch.Groups[4].Value -eq "true")
                }
            } else {
                $null
            }
            transcribing = if ($overlayTranscribingMatch) {
                [pscustomobject]@{
                    elapsedMs = [int]$overlayTranscribingMatch.Groups[1].Value
                    mode = $overlayTranscribingMatch.Groups[2].Value
                    visible = ($overlayTranscribingMatch.Groups[3].Value -eq "true")
                    available = ($overlayTranscribingMatch.Groups[4].Value -eq "true")
                }
            } else {
                $null
            }
            hide = if ($overlayHideMatch) {
                [pscustomobject]@{
                    elapsedMs = [int]$overlayHideMatch.Groups[1].Value
                    mode = $overlayHideMatch.Groups[2].Value
                    visible = ($overlayHideMatch.Groups[3].Value -eq "true")
                    available = ($overlayHideMatch.Groups[4].Value -eq "true")
                }
            } else {
                $null
            }
        }
        quit = [pscustomobject]@{
            elapsedMs = [int]$quitMatch.Groups[1].Value
            stoppedSidecars = [int]$quitMatch.Groups[2].Value
            exitRequested = $true
        }
        appExited = $true
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
$InstallRoot = Split-Path $ExePath
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
if (($LiveRecordingEnvFile -or $LiveRecordingDefaultStt -or $LiveRecordingSonioxMode) -and $LiveRecordingDurationSec -le 0) {
    throw "Live recording provider overrides require -LiveRecordingDurationSec."
}
if ($LiveRecordingEnvFile) {
    if (-not (Test-Path -LiteralPath $LiveRecordingEnvFile -PathType Leaf)) {
        throw "Missing LiveRecordingEnvFile: $LiveRecordingEnvFile"
    }
    $LiveRecordingEnvFile = (Resolve-Path -LiteralPath $LiveRecordingEnvFile).Path
}
if (($SimulateGlobalHotkey -or $WaitForManualGlobalHotkey) -and ($SimulateBackendCrash -or $SimulateBackendShutdown -or $SimulateBackendStartupTimeout)) {
    throw "-SimulateGlobalHotkey and -WaitForManualGlobalHotkey cannot be combined with -SimulateBackendCrash, -SimulateBackendShutdown, or -SimulateBackendStartupTimeout."
}
if ($SimulateGlobalHotkey -and $WaitForManualGlobalHotkey) {
    throw "-SimulateGlobalHotkey cannot be combined with -WaitForManualGlobalHotkey."
}
if ($VerifyAudioSidecarCleanup -and $KeepAppOpen) {
    throw "-VerifyAudioSidecarCleanup cannot be combined with -KeepAppOpen because app-exit cleanup must be verified."
}
if ($VerifyShellMenuSmoke -and $KeepAppOpen) {
    throw "-VerifyShellMenuSmoke cannot be combined with -KeepAppOpen because shell Quit must be verified."
}
if ($VerifyShellMenuSmoke -and $ShellMenuSmokeActions -notmatch "(?i)(^|[,;\s])(quit|exit)([,;\s]|$)") {
    throw "-VerifyShellMenuSmoke requires ShellMenuSmokeActions to include quit."
}
if ($GlobalHotkeySkipStopCleanup -and -not ($SimulateGlobalHotkey -or $WaitForManualGlobalHotkey)) {
    throw "-GlobalHotkeySkipStopCleanup requires -SimulateGlobalHotkey or -WaitForManualGlobalHotkey."
}
if (-not $DataDir) {
    $DataDir = Join-Path $RepoRoot ("tmp\tauri-smoke-data\" + [System.Guid]::NewGuid().ToString("N"))
}
$DataDir = Convert-ToFullPath -Path $DataDir
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$shellMenuSmokeTriggerPath = $null
$shellMenuSmokeQuitBarrierPath = $null
if ($VerifyShellMenuSmoke) {
    $shellMenuSmokeTriggerPath = Join-Path $DataDir "shell-menu-smoke.trigger"
    $shellMenuSmokeQuitBarrierPath = Join-Path $DataDir "shell-menu-smoke.quit-barrier"
}
$globalHotkeySmokeConfig = $null
if ($VerifyGlobalHotkeyRegistration -or $SimulateGlobalHotkey -or $WaitForManualGlobalHotkey) {
    $globalHotkeySmokeConfig = Initialize-GlobalHotkeySmokeData -RuntimeDataDir $DataDir -Hotkey $GlobalHotkeySmokeHotkey -DefaultStt $GlobalHotkeySmokeDefaultStt -Root $RepoRoot
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
$oldScriberPostProcessingEnabled = $env:SCRIBER_POST_PROCESSING_ENABLED
$oldScriberPostProcessingHotkey = $env:SCRIBER_POST_PROCESSING_HOTKEY
$oldScriberMeetingHotkey = $env:SCRIBER_MEETING_HOTKEY
$oldScriberMode = $env:SCRIBER_MODE
$oldScriberDefaultStt = $env:SCRIBER_DEFAULT_STT
$oldScriberInjectMethod = $env:SCRIBER_INJECT_METHOD
$oldDisableTextInjection = $env:SCRIBER_DISABLE_TEXT_INJECTION
$oldAudioEngine = $env:SCRIBER_AUDIO_ENGINE
$oldRustSyntheticCapture = $env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE
$oldRustSyntheticSignal = $env:SCRIBER_RUST_AUDIO_SYNTHETIC_SIGNAL
$oldRustWasapiCapture = $env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE
$oldMicAlwaysOn = $env:SCRIBER_MIC_ALWAYS_ON
$oldScriberAutoSummarize = $env:SCRIBER_AUTO_SUMMARIZE
$oldAudioSidecarExe = $env:SCRIBER_AUDIO_SIDECAR_EXE
$oldBackendStartTimeout = $env:SCRIBER_BACKEND_START_TIMEOUT_MS
$oldSimulateStartupTimeout = $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_ONCE
$oldSimulateStartupTimeoutMarker = $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER
$oldShellMenuSmokeActions = $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTIONS
$oldShellMenuSmokeTriggerFile = $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_FILE
$oldShellMenuSmokeTriggerTimeout = $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_TIMEOUT_MS
$oldShellMenuSmokeActionDelay = $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTION_DELAY_MS
$oldShellMenuSmokeQuitBarrierFile = $env:SCRIBER_TAURI_SMOKE_QUIT_BARRIER_FILE
$oldShellMenuSmokeQuitBarrierTimeout = $env:SCRIBER_TAURI_SMOKE_QUIT_BARRIER_TIMEOUT_MS
$liveRecordingProviderEnvSnapshot = @{}

function Set-SmokeEnvironmentVariable {
    param(
        [string]$Name,
        [string]$Value
    )

    if (-not $liveRecordingProviderEnvSnapshot.ContainsKey($Name)) {
        $liveRecordingProviderEnvSnapshot[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

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
    $env:SCRIBER_POST_PROCESSING_ENABLED = "1"
    $env:SCRIBER_POST_PROCESSING_HOTKEY = "ctrl+shift+f"
    $env:SCRIBER_MEETING_HOTKEY = "ctrl+shift+m"
    $env:SCRIBER_MODE = "toggle"
    $env:SCRIBER_DEFAULT_STT = $globalHotkeySmokeConfig.defaultStt
    $env:SCRIBER_INJECT_METHOD = "type"
    $env:SCRIBER_AUTO_SUMMARIZE = "0"
}
if ($LiveRecordingDurationSec -gt 0) {
    foreach ($assignment in Read-EnvFileAssignments -Path $LiveRecordingEnvFile) {
        Set-SmokeEnvironmentVariable -Name $assignment.Name -Value $assignment.Value
    }
    if ($LiveRecordingDefaultStt) {
        Set-SmokeEnvironmentVariable -Name "SCRIBER_DEFAULT_STT" -Value $LiveRecordingDefaultStt
    }
    if ($LiveRecordingSonioxMode) {
        Set-SmokeEnvironmentVariable -Name "SCRIBER_SONIOX_MODE" -Value $LiveRecordingSonioxMode
    }
}
if ($DisableLiveTextInjection) {
    $env:SCRIBER_DISABLE_TEXT_INJECTION = "1"
}
if ($LiveRecordingAudioEngine) {
    $env:SCRIBER_AUDIO_ENGINE = $LiveRecordingAudioEngine
}
if ($LiveRecordingRustAudioCaptureMode) {
    if ($LiveRecordingRustAudioCaptureMode -eq "synthetic") {
        $env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = "1"
        $env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE = $null
    } elseif ($LiveRecordingRustAudioCaptureMode -eq "wasapi") {
        $env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE = "1"
        $env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = $null
    }
}
if ($VerifyMeetingAudioDeviceTest) {
    $env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = "1"
    $env:SCRIBER_RUST_AUDIO_SYNTHETIC_SIGNAL = "1"
    $env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE = $null
}
if ($LiveRecordingMicAlwaysOn) {
    $env:SCRIBER_MIC_ALWAYS_ON = "1"
}
if ($LegacyDataDir) {
    $env:SCRIBER_LEGACY_DATA_DIR = $LegacyDataDir
}
if ($SimulateBackendStartupTimeout) {
    $env:SCRIBER_BACKEND_START_TIMEOUT_MS = $BackendStartupTimeoutMs.ToString()
    $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_ONCE = "1"
    $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER = Join-Path $DataDir "startup-timeout-once.marker"
}
if ($VerifyShellMenuSmoke) {
    $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTIONS = $ShellMenuSmokeActions
    $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_FILE = $shellMenuSmokeTriggerPath
    $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_TIMEOUT_MS = ([Math]::Max(1, $ShellMenuSmokeTimeoutSec) * 1000).ToString()
    $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTION_DELAY_MS = ([Math]::Max(0, $ShellMenuSmokeActionDelayMs)).ToString()
    $env:SCRIBER_TAURI_SMOKE_QUIT_BARRIER_FILE = $shellMenuSmokeQuitBarrierPath
    $env:SCRIBER_TAURI_SMOKE_QUIT_BARRIER_TIMEOUT_MS = "10000"
}

$app = $null
$result = $null
$failure = $null
$defaultPortBlocker = $null
$externalBackend = $null
$externalBackendPid = $null
$singleInstance = $null
$audioSidecarCleanup = $null
$strayAudioSidecar = $null
$shellMenuSmoke = $null
$meetingAudioDeviceTest = $null
try {
    if ($VerifyAudioSidecarCleanup) {
        $audioSidecarProbePath = Resolve-InstalledAudioSidecarExe -InstallRoot $InstallRoot
        $env:SCRIBER_AUDIO_SIDECAR_EXE = $audioSidecarProbePath
        $strayAudioSidecar = Start-InstalledAudioSidecarStray -AudioSidecarPath $audioSidecarProbePath
        $audioSidecarCleanup = [pscustomobject]@{
            verified = $false
            startupProbePid = [int]$strayAudioSidecar.processId
            startupProbePath = [string]$strayAudioSidecar.executablePath
            shellAudioSidecarExe = [string]$audioSidecarProbePath
            startupCleanupVerified = $false
            noRemainingInstalledSidecarsAfterExit = $false
            remainingInstalledSidecarsAfterExit = @()
        }
    }
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
    if ($VerifyAudioSidecarCleanup) {
        Wait-ProcessExit -ProcessId ([int]$strayAudioSidecar.processId) -DeadlineSec 10
        $audioSidecarCleanup.startupCleanupVerified = $true
    }
    if ($VerifySingleInstance) {
        $singleInstance = Test-SingleInstanceBlock `
            -ExePath $ExePath `
            -RuntimeDataDir $DataDir `
            -PrimaryAppProcess $app `
            -DeadlineSec 10 `
            -SecondArgumentList $SingleInstanceSecondArguments `
            -ForbiddenLogText $SingleInstanceForbiddenLogText
    }
    if ($VerifyMeetingAudioDeviceTest) {
        $meetingAudioDeviceTest = Test-MeetingAudioDeviceTest `
            -Port ([int]$listener.Port) `
            -Token $SessionToken
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
        if ($GlobalHotkeyPreDispatchSettleSec -gt 0) {
            Start-Sleep -Seconds $GlobalHotkeyPreDispatchSettleSec
        }
        $globalHotkey = Test-GlobalHotkeyDispatch `
            -Port ([int]$listener.Port) `
            -Token $SessionToken `
            -RuntimeDataDir $DataDir `
            -Hotkey $globalHotkeySmokeConfig.hotkey `
            -InvalidProvider $globalHotkeySmokeConfig.invalidProvider `
            -DefaultStt $globalHotkeySmokeConfig.defaultStt `
            -DeadlineSec $GlobalHotkeyDispatchTimeoutSec `
            -SkipStopCleanup ([bool]$GlobalHotkeySkipStopCleanup) `
            -DispatchMethod "synthetic"
    } elseif ($WaitForManualGlobalHotkey) {
        if ($GlobalHotkeyPreDispatchSettleSec -gt 0) {
            Start-Sleep -Seconds $GlobalHotkeyPreDispatchSettleSec
        }
        $globalHotkey = Test-GlobalHotkeyDispatch `
            -Port ([int]$listener.Port) `
            -Token $SessionToken `
            -RuntimeDataDir $DataDir `
            -Hotkey $globalHotkeySmokeConfig.hotkey `
            -InvalidProvider $globalHotkeySmokeConfig.invalidProvider `
            -DefaultStt $globalHotkeySmokeConfig.defaultStt `
            -DeadlineSec $GlobalHotkeyDispatchTimeoutSec `
            -SkipStopCleanup ([bool]$GlobalHotkeySkipStopCleanup) `
            -DispatchMethod "manual"
    } elseif ($VerifyGlobalHotkeyRegistration) {
        $globalHotkey = Test-GlobalHotkeyRegistration `
            -RuntimeDataDir $DataDir `
            -Hotkey $globalHotkeySmokeConfig.hotkey `
            -InvalidProvider $globalHotkeySmokeConfig.invalidProvider `
            -DefaultStt $globalHotkeySmokeConfig.defaultStt `
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
    $frontend = $null
    if ($VerifyFrontend) {
        $frontend = Test-FrontendHttp -Port ([int]$listener.Port) -Token $SessionToken
    }
    $realMediaWorkflows = $null
    if ($VerifyRealMediaWorkflows) {
        $realMediaWorkflows = Test-RealMediaWorkflows `
            -Port ([int]$listener.Port) `
            -Token $SessionToken `
            -RuntimeDataDir $DataDir
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
        -StopTimeoutSec $LiveRecordingStopTimeoutSec `
        -TextInjectionDisabled ([bool]$DisableLiveTextInjection) `
        -MicAlwaysOn ([bool]$LiveRecordingMicAlwaysOn)
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
    if ($VerifyShellMenuSmoke) {
        $shellMenuSmoke = Test-ShellMenuSmoke `
            -AppProcess $app `
            -RuntimeDataDir $DataDir `
            -TriggerPath $shellMenuSmokeTriggerPath `
            -Actions $ShellMenuSmokeActions `
            -DeadlineSec $ShellMenuSmokeTimeoutSec `
            -Port ([int]$listener.Port) `
            -Token $SessionToken `
            -QuitBarrierPath $shellMenuSmokeQuitBarrierPath `
            -MeasurementGatePath $FrontendPerformanceGatePath
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
        externalAttach = $externalAttach
        portConflict = $portConflictResult
        legacyDataMigration = $legacyDataMigration
        frontend = $frontend
        meetingAudioDeviceTest = $meetingAudioDeviceTest
        supportBundle = $supportBundle
        realMediaWorkflows = $realMediaWorkflows
        globalHotkey = $globalHotkey
        singleInstance = $singleInstance
        audioSidecarCleanup = $audioSidecarCleanup
        startupTimeout = $startupTimeout
        crashRecovery = $crashRecovery
        controlledShutdown = $controlledShutdown
        liveRecording = $liveRecording
        stability = $stability
        shellMenuSmoke = $shellMenuSmoke
        failureDiagnostics = $null
        cleanupVerified = $false
    }
} catch {
    $failure = $_
    $failureMessage = $_.Exception.Message
    $failureDiagnostics = $null
    if ($listener) {
        $failureDiagnostics = Get-SmokeFailureDiagnostics -Port ([int]$listener.Port) -Token $SessionToken
    }
    $failureLiveRecording = $null
    if ($LiveRecordingDurationSec -gt 0) {
        $failureLiveRecording = [pscustomobject]@{
            verified = $false
            durationSec = $LiveRecordingDurationSec
            probeIntervalSec = [Math]::Max(1, $LiveRecordingProbeIntervalSec)
            textInjectionDisabled = [bool]$DisableLiveTextInjection
            micAlwaysOn = [bool]$LiveRecordingMicAlwaysOn
            error = $failureMessage
        }
    }
    $result = [pscustomobject]@{
        ok = $false
        error = $failureMessage
        appPid = if ($app) { $app.Id } else { $null }
        backendPid = if ($listener) { $listener.BackendPid } else { $null }
        backendPort = if ($listener) { $listener.Port } else { $null }
        runtimeMode = if ($health) { $health.runtimeMode } else { $null }
        apiVersion = if ($health) { $health.apiVersion } else { $null }
        ready = if ($health) { $health.ready } else { $null }
        dataDir = if ($runtime) { $runtime.dataDir } else { $DataDir }
        downloadsDir = if ($runtime) { $runtime.downloadsDir } else { $null }
        launchKind = if ($runtime) { $runtime.launchKind } else { $null }
        externalAttach = $externalAttach
        portConflict = if ($portConflict) { [pscustomobject]$portConflict } else { $null }
        legacyDataMigration = $legacyDataMigration
        frontend = $frontend
        meetingAudioDeviceTest = $meetingAudioDeviceTest
        supportBundle = $supportBundle
        realMediaWorkflows = $realMediaWorkflows
        globalHotkey = $globalHotkey
        singleInstance = $singleInstance
        audioSidecarCleanup = $audioSidecarCleanup
        startupTimeout = $startupTimeout
        crashRecovery = $crashRecovery
        controlledShutdown = $controlledShutdown
        liveRecording = $failureLiveRecording
        stability = $null
        shellMenuSmoke = $shellMenuSmoke
        failureDiagnostics = $failureDiagnostics
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

    if ($VerifyAudioSidecarCleanup) {
        if ($strayAudioSidecar -and $strayAudioSidecar.process -and -not $strayAudioSidecar.process.HasExited) {
            Stop-Process -Id ([int]$strayAudioSidecar.processId) -Force -ErrorAction SilentlyContinue
            $cleanupFailure = "Installed audio sidecar startup cleanup did not stop probe process $($strayAudioSidecar.processId)."
        }
        $remainingAudioSidecars = @(Get-InstalledAudioSidecarProcesses -InstallRoot $InstallRoot)
        if ($audioSidecarCleanup) {
            $audioSidecarCleanup.remainingInstalledSidecarsAfterExit = @($remainingAudioSidecars)
            $audioSidecarCleanup.noRemainingInstalledSidecarsAfterExit = ($remainingAudioSidecars.Count -eq 0)
            $audioSidecarCleanup.verified = (
                [bool]$audioSidecarCleanup.startupCleanupVerified -and
                [bool]$audioSidecarCleanup.noRemainingInstalledSidecarsAfterExit
            )
        }
        if ($remainingAudioSidecars.Count -gt 0) {
            foreach ($process in $remainingAudioSidecars) {
                Stop-Process -Id ([int]$process.processId) -Force -ErrorAction SilentlyContinue
            }
            $details = @($remainingAudioSidecars | ForEach-Object { "$($_.processId):$($_.name)" })
            $cleanupFailure = "Installed audio sidecar process remained after Tauri exit: $($details -join '; ')"
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
    $env:SCRIBER_POST_PROCESSING_ENABLED = $oldScriberPostProcessingEnabled
    $env:SCRIBER_POST_PROCESSING_HOTKEY = $oldScriberPostProcessingHotkey
    $env:SCRIBER_MEETING_HOTKEY = $oldScriberMeetingHotkey
    $env:SCRIBER_MODE = $oldScriberMode
    $env:SCRIBER_DEFAULT_STT = $oldScriberDefaultStt
    $env:SCRIBER_INJECT_METHOD = $oldScriberInjectMethod
    $env:SCRIBER_DISABLE_TEXT_INJECTION = $oldDisableTextInjection
    $env:SCRIBER_AUDIO_ENGINE = $oldAudioEngine
    $env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = $oldRustSyntheticCapture
    $env:SCRIBER_RUST_AUDIO_SYNTHETIC_SIGNAL = $oldRustSyntheticSignal
    $env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE = $oldRustWasapiCapture
    $env:SCRIBER_MIC_ALWAYS_ON = $oldMicAlwaysOn
    $env:SCRIBER_AUTO_SUMMARIZE = $oldScriberAutoSummarize
    $env:SCRIBER_AUDIO_SIDECAR_EXE = $oldAudioSidecarExe
    $env:SCRIBER_BACKEND_START_TIMEOUT_MS = $oldBackendStartTimeout
    $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_ONCE = $oldSimulateStartupTimeout
    $env:SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER = $oldSimulateStartupTimeoutMarker
    $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTIONS = $oldShellMenuSmokeActions
    $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_FILE = $oldShellMenuSmokeTriggerFile
    $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_TIMEOUT_MS = $oldShellMenuSmokeTriggerTimeout
    $env:SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTION_DELAY_MS = $oldShellMenuSmokeActionDelay
    $env:SCRIBER_TAURI_SMOKE_QUIT_BARRIER_FILE = $oldShellMenuSmokeQuitBarrierFile
    $env:SCRIBER_TAURI_SMOKE_QUIT_BARRIER_TIMEOUT_MS = $oldShellMenuSmokeQuitBarrierTimeout
    foreach ($name in @($liveRecordingProviderEnvSnapshot.Keys)) {
        [Environment]::SetEnvironmentVariable(
            $name,
            $liveRecordingProviderEnvSnapshot[$name],
            "Process"
        )
    }

    if ($cleanupFailure) {
        throw $cleanupFailure
    }
}

Write-SmokeJson -Payload $result -Path $OutputPath -Root $RepoRoot
if ($failure) {
    throw $failure
}
