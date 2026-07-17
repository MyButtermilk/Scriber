<#
.SYNOPSIS
Runs the real Tauri Meeting flow through Puppeteer with a local Piper microphone fixture.

.DESCRIPTION
The smoke builds the current debug desktop/audio binaries unless -SkipBuild is used,
starts the Vite dev frontend and the real Tauri shell, connects Puppeteer Core to the
Tauri WebView2 remote-debugging endpoint, and drives the Meeting UI through start,
pause, resume, stop, local final transcription, and transcript validation.

Piper and Puppeteer are installed only below tmp/. Voice models and generated PCM are
never copied into tracked source directories. The result JSON contains counts, hashes,
states, and timings only; it intentionally omits transcript text, session tokens, URLs,
DOM dumps, screenshots, personal paths, and raw runtime logs.
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonPath = "",
    [string]$PiperRuntimePath = "",
    [string]$PiperVoiceModelPath = "",
    [string]$FfmpegPath = "",
    [string]$ExePath = "",
    [string]$AudioSidecarPath = "",
    [string]$ArtifactDir = "",
    [string]$PuppeteerRuntimePath = "",
    [string]$PuppeteerVersion = "25.3.0",
    [string]$OnnxModel = "parakeet-primeline",
    [ValidateSet("int8", "fp16", "fp32")]
    [string]$OnnxQuantization = "int8",
    [string]$ModelCachePath = "",
    [string]$TtsText = (
        "Dies ist ein automatischer Scriber Mikrofontest. " +
        "Heute prüfen wir Aufnahme, Pause, Fortsetzen und Transkription. " +
        "Die eindeutige Testmarke lautet Seestern siebenundvierzig. " +
        "Das Meeting funktioniert vollständig."
    ),
    [string[]]$ExpectedTokens = @("seestern", "siebenundvierzig", "mikrofontest"),
    [int]$PrePauseMs = 3000,
    [int]$PausedMs = 1200,
    [int]$StartupTimeoutSec = 90,
    [int]$FinalizationTimeoutSec = 420,
    [int]$CleanupTimeoutSec = 30,
    [switch]$SkipBuild,
    [switch]$SkipPuppeteerInstall,
    [switch]$ReuseDevServer,
    [switch]$KeepFixture,
    [switch]$KeepRuntimeData
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$DevServerPort = 5000


function Convert-ToFullPath {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [string]$BasePath
    )
    if ([System.IO.Path]::IsPathFullyQualified($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $BasePath $Path))
}


function Resolve-RequiredFile {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing or is not a file."
    }
    return (Resolve-Path -LiteralPath $Path).Path
}


function Resolve-RequiredDirectory {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label is missing or is not a directory."
    }
    return (Resolve-Path -LiteralPath $Path).Path
}


function Resolve-CommandFile {
    param(
        [string]$Requested,
        [Parameter(Mandatory)]
        [string]$FallbackName,
        [Parameter(Mandatory)]
        [string]$Label
    )
    if ($Requested) {
        return Resolve-RequiredFile -Path (Convert-ToFullPath -Path $Requested -BasePath $RepoRoot) -Label $Label
    }
    $command = Get-Command $FallbackName -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "$Label is unavailable."
    }
    return $command.Source
}


function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    try {
        $listener.Start()
        return [int]$listener.LocalEndpoint.Port
    } finally {
        $listener.Stop()
    }
}


function Test-LoopbackPortListening {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        return $task.Wait(300) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}


function Wait-HttpReady {
    param(
        [Parameter(Mandatory)]
        [string]$Uri,
        [Parameter(Mandatory)]
        [System.Diagnostics.Process]$OwnerProcess,
        [int]$TimeoutSec
    )
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSec)
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        $OwnerProcess.Refresh()
        if ($OwnerProcess.HasExited) {
            throw "The owning process exited before its HTTP endpoint became ready."
        }
        try {
            $response = Invoke-WebRequest -Uri $Uri -TimeoutSec 2 -UseBasicParsing
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    throw "HTTP endpoint did not become ready before the startup deadline."
}


function Wait-ProcessExit {
    param(
        [Parameter(Mandatory)]
        [System.Diagnostics.Process]$Process,
        [int]$TimeoutSec
    )
    try {
        $Process.Refresh()
        if ($Process.HasExited) {
            return $true
        }
        return $Process.WaitForExit([Math]::Max(1, $TimeoutSec) * 1000)
    } catch {
        return $true
    }
}


function Get-ProcessDescendantHandles {
    param(
        [Parameter(Mandatory)]
        [System.Diagnostics.Process]$RootProcess
    )
    try {
        $RootProcess.Refresh()
        if ($RootProcess.HasExited) {
            return @()
        }
        $rootProcessId = $RootProcess.Id
        $rootStartTicks = $RootProcess.StartTime.ToUniversalTime().Ticks
        $rootExecutablePath = [System.IO.Path]::GetFullPath([string]$RootProcess.Path)
        $null = $RootProcess.Handle
    } catch {
        return @()
    }
    $processes = @(
        Get-CimInstance Win32_Process |
            Select-Object ProcessId, ParentProcessId, CreationDate, ExecutablePath
    )
    $rootSnapshot = $processes |
        Where-Object { [int]$_.ProcessId -eq $rootProcessId } |
        Select-Object -First 1
    if (-not $rootSnapshot -or -not $rootSnapshot.CreationDate -or -not $rootSnapshot.ExecutablePath) {
        return @()
    }
    $rootSnapshotTicks = ([datetime]$rootSnapshot.CreationDate).ToUniversalTime().Ticks
    $rootSnapshotPath = [System.IO.Path]::GetFullPath([string]$rootSnapshot.ExecutablePath)
    if (
        [math]::Abs($rootSnapshotTicks - $rootStartTicks) -gt 100 `
        -or -not $rootSnapshotPath.Equals(
            $rootExecutablePath,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return @()
    }
    $seen = [System.Collections.Generic.HashSet[int]]::new()
    $frontier = [System.Collections.Generic.Queue[int]]::new()
    $frontier.Enqueue($rootProcessId)
    while ($frontier.Count -gt 0) {
        $parent = $frontier.Dequeue()
        foreach ($process in $processes) {
            $processId = [int]$process.ProcessId
            if ([int]$process.ParentProcessId -eq $parent -and $seen.Add($processId)) {
                $frontier.Enqueue($processId)
            }
        }
    }
    $owned = @()
    foreach ($processId in $seen) {
        $snapshot = $processes | Where-Object { [int]$_.ProcessId -eq $processId } | Select-Object -First 1
        if (-not $snapshot -or -not $snapshot.CreationDate -or -not $snapshot.ExecutablePath) {
            continue
        }
        try {
            $process = Get-Process -Id $processId -ErrorAction Stop
            $snapshotTicks = ([datetime]$snapshot.CreationDate).ToUniversalTime().Ticks
            $processTicks = $process.StartTime.ToUniversalTime().Ticks
            $snapshotPath = [System.IO.Path]::GetFullPath([string]$snapshot.ExecutablePath)
            $processPath = [System.IO.Path]::GetFullPath([string]$process.Path)
            if (
                [math]::Abs($snapshotTicks - $processTicks) -gt 100 `
                -or -not $snapshotPath.Equals(
                    $processPath,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            ) {
                $process.Dispose()
                continue
            }
            # Force the process handle open now. The retained handle remains
            # bound to this exact process even if Windows later reuses its PID.
            $null = $process.Handle
            $owned += $process
        } catch {
            # The child exited between the process-tree snapshot and handle capture.
        }
    }
    return @($owned)
}


function Stop-OwnedProcess {
    param([System.Diagnostics.Process]$Process)
    if (-not $Process) {
        return
    }
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            $Process.Kill()
            $null = $Process.WaitForExit(10000)
        }
    } catch {
        # The process already exited between inspection and cleanup.
    }
}


function Remove-ContainedDirectory {
    param(
        [Parameter(Mandatory)]
        [string]$Target,
        [Parameter(Mandatory)]
        [string]$AllowedRoot
    )
    if (-not (Test-Path -LiteralPath $Target)) {
        return
    }
    $targetFull = [System.IO.Path]::GetFullPath($Target).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $rootFull = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    if ($targetFull -eq $rootFull -or -not $targetFull.StartsWith(
        $prefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to recursively remove a directory outside the verified artifact root."
    }
    Remove-Item -LiteralPath $targetFull -Recurse -Force
}


function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,
        [Parameter(Mandatory)]
        [string[]]$ArgumentList,
        [Parameter(Mandatory)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory)]
        [string]$FailureMessage
    )
    Push-Location $WorkingDirectory
    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw "$FailureMessage (exit code $LASTEXITCODE)."
        }
    } finally {
        Pop-Location
    }
}


$RepoRoot = Resolve-RequiredDirectory -Path $RepoRoot -Label "repository root"
$FrontendRoot = Join-Path $RepoRoot "Frontend"
$TauriRoot = Join-Path $FrontendRoot "src-tauri"
$GeneratorScript = Resolve-RequiredFile -Path (Join-Path $PSScriptRoot "generate_meeting_tts_fixture.py") -Label "Piper fixture generator"
$PuppeteerScript = Resolve-RequiredFile -Path (Join-Path $PSScriptRoot "smoke_meeting_tts_puppeteer.mjs") -Label "Puppeteer Meeting driver"

if (-not $PythonPath) {
    $venvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $PythonPath = $venvPython
    } else {
        $PythonPath = (Get-Command python.exe -ErrorAction Stop).Source
    }
}
$PythonPath = Resolve-RequiredFile -Path (Convert-ToFullPath -Path $PythonPath -BasePath $RepoRoot) -Label "Python executable"
$NodePath = (Get-Command node.exe -ErrorAction Stop).Source
$NpmPath = (Get-Command npm.cmd -ErrorAction Stop).Source
$CargoPath = (Get-Command cargo.exe -ErrorAction Stop).Source
$FfmpegPath = Resolve-CommandFile -Requested $FfmpegPath -FallbackName "ffmpeg.exe" -Label "ffmpeg executable"

if (-not $PiperRuntimePath) {
    $PiperRuntimePath = Join-Path $RepoRoot "tmp\meeting-tts\piper-runtime"
} else {
    $PiperRuntimePath = Convert-ToFullPath -Path $PiperRuntimePath -BasePath $RepoRoot
}
$PiperRuntimePath = Resolve-RequiredDirectory -Path $PiperRuntimePath -Label "isolated Piper runtime"

if (-not $PiperVoiceModelPath) {
    $PiperVoiceModelPath = Join-Path $RepoRoot "tmp\meeting-tts\voices\de_DE-thorsten-medium.onnx"
} else {
    $PiperVoiceModelPath = Convert-ToFullPath -Path $PiperVoiceModelPath -BasePath $RepoRoot
}
$PiperVoiceModelPath = Resolve-RequiredFile -Path $PiperVoiceModelPath -Label "Piper voice model"
$null = Resolve-RequiredFile -Path "$PiperVoiceModelPath.json" -Label "Piper voice configuration"

if ($PrePauseMs -lt 1000 -or $PrePauseMs -gt 30000) {
    throw "PrePauseMs must be between 1,000 and 30,000."
}
if ($PausedMs -lt 500 -or $PausedMs -gt 30000) {
    throw "PausedMs must be between 500 and 30,000."
}
if ($FinalizationTimeoutSec -lt 30 -or $FinalizationTimeoutSec -gt 1800) {
    throw "FinalizationTimeoutSec must be between 30 and 1,800."
}
if (-not $TtsText.Trim()) {
    throw "TtsText must not be empty."
}

$existingDesktop = @(Get-Process scriber-desktop -ErrorAction SilentlyContinue)
if ($existingDesktop.Count -gt 0) {
    throw "A Scriber desktop process is already running. Close it before the isolated Meeting E2E smoke; no existing process was changed."
}
if ((Test-LoopbackPortListening -Port $DevServerPort) -and -not $ReuseDevServer) {
    throw "Port $DevServerPort is already in use. Use -ReuseDevServer only when that listener is the current Scriber Vite frontend."
}

if (-not $ArtifactDir) {
    $runId = [System.Guid]::NewGuid().ToString("N")
    $ArtifactDir = Join-Path $RepoRoot "tmp\meeting-e2e\runs\$runId"
} else {
    $ArtifactDir = Convert-ToFullPath -Path $ArtifactDir -BasePath $RepoRoot
}
[System.IO.Directory]::CreateDirectory($ArtifactDir) | Out-Null
$ArtifactDir = (Resolve-Path -LiteralPath $ArtifactDir).Path
$DataDir = Join-Path $ArtifactDir "runtime-data"
[System.IO.Directory]::CreateDirectory($DataDir) | Out-Null
$PcmPath = Join-Path $ArtifactDir "meeting-mic.pcm"
$FixtureResultPath = Join-Path $ArtifactDir "fixture.json"
$ResultPath = Join-Path $ArtifactDir "result.json"
$TriggerPath = Join-Path $ArtifactDir "shell-quit.trigger"
$ViteStdoutPath = Join-Path $ArtifactDir "vite.stdout.log"
$ViteStderrPath = Join-Path $ArtifactDir "vite.stderr.log"
$TauriStdoutPath = Join-Path $ArtifactDir "tauri.stdout.log"
$TauriStderrPath = Join-Path $ArtifactDir "tauri.stderr.log"

if (-not $PuppeteerRuntimePath) {
    $PuppeteerRuntimePath = Join-Path $RepoRoot "tmp\meeting-e2e\puppeteer-$PuppeteerVersion"
} else {
    $PuppeteerRuntimePath = Convert-ToFullPath -Path $PuppeteerRuntimePath -BasePath $RepoRoot
}
[System.IO.Directory]::CreateDirectory($PuppeteerRuntimePath) | Out-Null
$PuppeteerRuntimePath = (Resolve-Path -LiteralPath $PuppeteerRuntimePath).Path

if (-not $ExePath) {
    $ExePath = Join-Path $TauriRoot "target\debug\scriber-desktop.exe"
} else {
    $ExePath = Convert-ToFullPath -Path $ExePath -BasePath $RepoRoot
}
if (-not $AudioSidecarPath) {
    $AudioSidecarPath = Join-Path $TauriRoot "target\debug\scriber-audio-sidecar.exe"
} else {
    $AudioSidecarPath = Convert-ToFullPath -Path $AudioSidecarPath -BasePath $RepoRoot
}

if (-not $SkipPuppeteerInstall) {
    $installedPackage = Join-Path $PuppeteerRuntimePath "node_modules\puppeteer-core\package.json"
    $installedVersion = ""
    if (Test-Path -LiteralPath $installedPackage -PathType Leaf) {
        try {
            $installedVersion = [string](Get-Content -LiteralPath $installedPackage -Raw | ConvertFrom-Json).version
        } catch {
            $installedVersion = ""
        }
    }
    if ($installedVersion -ne $PuppeteerVersion) {
        Invoke-CheckedCommand `
            -FilePath $NpmPath `
            -ArgumentList @(
                "install",
                "--prefix", $PuppeteerRuntimePath,
                "--no-save",
                "--no-package-lock",
                "--ignore-scripts",
                "puppeteer-core@$PuppeteerVersion"
            ) `
            -WorkingDirectory $RepoRoot `
            -FailureMessage "Could not install isolated Puppeteer Core"
    }
}
$PuppeteerPackagePath = Resolve-RequiredFile `
    -Path (Join-Path $PuppeteerRuntimePath "node_modules\puppeteer-core\package.json") `
    -Label "isolated Puppeteer Core package"
$ActualPuppeteerVersion = [string](Get-Content -LiteralPath $PuppeteerPackagePath -Raw | ConvertFrom-Json).version
if ($ActualPuppeteerVersion -ne $PuppeteerVersion) {
    throw "Isolated Puppeteer Core version does not match the requested pinned version."
}

if (-not $SkipBuild) {
    Invoke-CheckedCommand `
        -FilePath $CargoPath `
        -ArgumentList @(
            "build",
            "--manifest-path", (Join-Path $TauriRoot "Cargo.toml"),
            "--bin", "scriber-desktop",
            "--bin", "scriber-audio-sidecar"
        ) `
        -WorkingDirectory $RepoRoot `
        -FailureMessage "Tauri debug binaries could not be built"
}
$ExePath = Resolve-RequiredFile -Path $ExePath -Label "Tauri debug executable"
$AudioSidecarPath = Resolve-RequiredFile -Path $AudioSidecarPath -Label "Rust audio sidecar"

$fixtureOutput = & $PythonPath $GeneratorScript `
    --runtime-dir $PiperRuntimePath `
    --voice-model $PiperVoiceModelPath `
    --output $PcmPath `
    --result-json $FixtureResultPath `
    --ffmpeg $FfmpegPath `
    --text $TtsText
if ($LASTEXITCODE -ne 0) {
    throw "Piper fixture generation failed."
}
$Fixture = $fixtureOutput | Select-Object -Last 1 | ConvertFrom-Json
if ([int]$Fixture.sampleRate -ne 48000 -or [int]$Fixture.channels -ne 1 -or [int]$Fixture.sampleWidthBytes -ne 2) {
    throw "Piper fixture contract validation failed."
}
if ([int64]$Fixture.byteLength -le 0 -or [double]$Fixture.rms -le 0.0001) {
    throw "Piper fixture signal validation failed."
}

$viteProcess = $null
$viteListenerProcess = $null
$viteDescendantProcesses = @()
$tauriProcess = $null
$tauriDescendantProcesses = @()
$driverSucceeded = $false
$shutdownTriggered = $false
$remoteDebuggingPort = Get-FreeLoopbackPort
$sessionToken = [System.Guid]::NewGuid().ToString("N")
$meetingTitle = "Puppeteer Piper TTS E2E " + [DateTimeOffset]::UtcNow.ToString("yyyyMMddHHmmss")

try {
    if (-not (Test-LoopbackPortListening -Port $DevServerPort)) {
        $viteProcess = Start-Process `
            -FilePath $NpmPath `
            -ArgumentList @("run", "dev:client", "--", "--host", "127.0.0.1", "--strictPort") `
            -WorkingDirectory $FrontendRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $ViteStdoutPath `
            -RedirectStandardError $ViteStderrPath `
            -PassThru
        $viteDescendantProcesses = @(
            Get-ProcessDescendantHandles -RootProcess $viteProcess
        )
        Wait-HttpReady `
            -Uri "http://127.0.0.1:$DevServerPort/" `
            -OwnerProcess $viteProcess `
            -TimeoutSec $StartupTimeoutSec
        $viteDescendantProcesses = @(
            $viteDescendantProcesses
            @(Get-ProcessDescendantHandles -RootProcess $viteProcess)
        )
        $viteListener = Get-NetTCPConnection `
            -State Listen `
            -LocalPort $DevServerPort `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($viteListener) {
            $candidateListenerPid = [int]$viteListener.OwningProcess
            if (
                $candidateListenerPid -ne $viteProcess.Id `
                -and $candidateListenerPid -notin @($viteDescendantProcesses | ForEach-Object { $_.Id })
            ) {
                throw "The Vite listener is not owned by the E2E process tree."
            }
            $viteListenerProcess = if ($candidateListenerPid -eq $viteProcess.Id) {
                $viteProcess
            } else {
                $viteDescendantProcesses |
                    Where-Object { $_.Id -eq $candidateListenerPid } |
                    Select-Object -First 1
            }
            if (-not $viteListenerProcess) {
                throw "The Vite listener identity could not be bound to an owned process handle."
            }
        }
    }

    $appEnvironment = @{
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" = "--remote-debugging-port=$remoteDebuggingPort --remote-debugging-address=127.0.0.1"
        "SCRIBER_REPO_ROOT" = $RepoRoot
        "SCRIBER_PYTHON" = $PythonPath
        "SCRIBER_DATA_DIR" = $DataDir
        "SCRIBER_FORCE_MANAGED_BACKEND" = "1"
        "SCRIBER_SESSION_TOKEN" = $sessionToken
        "SCRIBER_DISABLE_HOTKEYS" = "1"
        "SCRIBER_DISABLE_DEVICE_MONITOR" = "1"
        "SCRIBER_NATIVE_DEVICE_EVENTS" = "0"
        "SCRIBER_DISABLE_TEXT_INJECTION" = "1"
        "SCRIBER_AUDIO_ENGINE" = "rust-wasapi"
        "SCRIBER_AUDIO_SIDECAR_EXE" = $AudioSidecarPath
        "SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE" = "1"
        "SCRIBER_RUST_AUDIO_SYNTHETIC_SIGNAL" = "0"
        "SCRIBER_RUST_AUDIO_WASAPI_CAPTURE" = "0"
        "SCRIBER_RUST_AUDIO_DISABLE_WASAPI_CAPTURE" = "0"
        "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH" = $PcmPath
        "SCRIBER_MEETING_FINAL_PROVIDER" = "onnx_local"
        "SCRIBER_MEETING_TRANSCRIPTION_MODE" = "final_only"
        "SCRIBER_MEETING_AUTO_ANALYZE" = "0"
        "SCRIBER_MEETING_SMART_TURN_ENABLED" = "0"
        "SCRIBER_MEETING_AUDIO_RETENTION_DAYS" = "1"
        "SCRIBER_ONNX_MODEL" = $OnnxModel
        "SCRIBER_ONNX_QUANTIZATION" = $OnnxQuantization
        "SCRIBER_ONNX_USE_GPU" = "0"
        "SCRIBER_AUTO_SUMMARIZE" = "0"
        "SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTIONS" = "quit"
        "SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_FILE" = $TriggerPath
        "SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_TIMEOUT_MS" = (($FinalizationTimeoutSec + $StartupTimeoutSec + 180) * 1000).ToString()
        "SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTION_DELAY_MS" = "0"
    }
    if ($ModelCachePath) {
        $appEnvironment["SCRIBER_MODEL_CACHE"] = Convert-ToFullPath -Path $ModelCachePath -BasePath $RepoRoot
    }

    $tauriProcess = Start-Process `
        -FilePath $ExePath `
        -WorkingDirectory (Split-Path $ExePath) `
        -WindowStyle Hidden `
        -Environment $appEnvironment `
        -RedirectStandardOutput $TauriStdoutPath `
        -RedirectStandardError $TauriStderrPath `
        -PassThru
    Wait-HttpReady `
        -Uri "http://127.0.0.1:$remoteDebuggingPort/json/version" `
        -OwnerProcess $tauriProcess `
        -TimeoutSec $StartupTimeoutSec
    $tauriDescendantProcesses = @(Get-ProcessDescendantHandles -RootProcess $tauriProcess)

    $driverArguments = @(
        $PuppeteerScript,
        "--browser-url", "http://127.0.0.1:$remoteDebuggingPort",
        "--puppeteer-root", $PuppeteerRuntimePath,
        "--output", $ResultPath,
        "--title", $meetingTitle,
        "--fixture-duration-ms", ([string]$Fixture.durationMs),
        "--pre-pause-ms", ([string]$PrePauseMs),
        "--paused-ms", ([string]$PausedMs),
        "--navigation-timeout-ms", ([string]($StartupTimeoutSec * 1000)),
        "--finalization-timeout-ms", ([string]($FinalizationTimeoutSec * 1000))
    )
    foreach ($token in $ExpectedTokens) {
        if ($token.Trim()) {
            $driverArguments += @("--expected-token", $token)
        }
    }
    & $NodePath @driverArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Puppeteer Meeting driver failed."
    }
    $DriverResult = Get-Content -LiteralPath $ResultPath -Raw | ConvertFrom-Json
    if ($DriverResult.ok -ne $true) {
        throw "Puppeteer Meeting result did not pass."
    }
    $tauriDescendantProcesses = @(
        $tauriDescendantProcesses
        @(Get-ProcessDescendantHandles -RootProcess $tauriProcess)
    )

    [System.IO.File]::WriteAllText($TriggerPath, "quit`n", [System.Text.UTF8Encoding]::new($false))
    $shutdownTriggered = $true
    if (-not (Wait-ProcessExit -Process $tauriProcess -TimeoutSec $CleanupTimeoutSec)) {
        throw "Tauri shell did not exit through the bounded smoke Quit path."
    }

    $cleanupDeadline = [DateTimeOffset]::UtcNow.AddSeconds($CleanupTimeoutSec)
    do {
        $remainingDescendants = @($tauriDescendantProcesses | Where-Object {
            try {
                $_.Refresh()
                -not $_.HasExited
            } catch {
                $false
            }
        })
        if ($remainingDescendants.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTimeOffset]::UtcNow -lt $cleanupDeadline)
    if ($remainingDescendants.Count -gt 0) {
        throw "A process owned by the E2E Tauri instance remained after its cleanup deadline."
    }

    $driverSucceeded = $true
    [pscustomobject]@{
        ok = $true
        automation = "puppeteer-core"
        puppeteerVersion = $ActualPuppeteerVersion
        browserTransport = "webview2-remote-debugging"
        fixtureDurationMs = [int]$Fixture.durationMs
        fixtureSha256 = [string]$Fixture.sha256
        observedStates = @($DriverResult.observedStates)
        segmentCount = [int]$DriverResult.segmentCount
        transcriptCharacterCount = [int]$DriverResult.transcriptCharacterCount
        matchedExpectedTokenCount = [int]$DriverResult.matchedExpectedTokenCount
        audioGapCount = [int]$DriverResult.audioGapCount
        cleanupVerified = $true
        resultArtifact = "result.json"
    } | ConvertTo-Json -Depth 5
} finally {
    if ($tauriProcess) {
        if ($tauriDescendantProcesses.Count -eq 0) {
            $tauriDescendantProcesses = @(
                Get-ProcessDescendantHandles -RootProcess $tauriProcess
            )
        }
        try {
            $tauriProcess.Refresh()
            if (-not $tauriProcess.HasExited) {
                $tauriDescendantProcesses = @(
                    $tauriDescendantProcesses
                    @(Get-ProcessDescendantHandles -RootProcess $tauriProcess)
                )
            }
            if (-not $tauriProcess.HasExited -and -not $shutdownTriggered) {
                [System.IO.File]::WriteAllText($TriggerPath, "quit`n", [System.Text.UTF8Encoding]::new($false))
                $shutdownTriggered = $true
                $null = Wait-ProcessExit -Process $tauriProcess -TimeoutSec $CleanupTimeoutSec
            }
        } catch {
            # Continue into the owned-process fallback below.
        }
        Stop-OwnedProcess -Process $tauriProcess
    }

    foreach ($owned in $tauriDescendantProcesses) {
        Stop-OwnedProcess -Process $owned
    }

    if ($viteProcess) {
        try {
            $viteDescendantProcesses = @(
                $viteDescendantProcesses
                @(Get-ProcessDescendantHandles -RootProcess $viteProcess)
            )
        } catch {
            # Continue with the process-tree snapshot already owned by this run.
        }
    }
    if ($viteListenerProcess) {
        Stop-OwnedProcess -Process $viteListenerProcess
    }
    Stop-OwnedProcess -Process $viteProcess
    foreach ($owned in $viteDescendantProcesses) {
        Stop-OwnedProcess -Process $owned
    }

    foreach ($redirectOwner in @(
        $tauriProcess
        $viteProcess
        $viteListenerProcess
        $tauriDescendantProcesses
        $viteDescendantProcesses
    )) {
        if ($redirectOwner) {
            try {
                $redirectOwner.Dispose()
            } catch {
                # Cleanup must continue even if the process handle already closed.
            }
        }
    }

    if ($driverSucceeded) {
        foreach ($logPath in @($ViteStdoutPath, $ViteStderrPath, $TauriStdoutPath, $TauriStderrPath)) {
            Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $TriggerPath -Force -ErrorAction SilentlyContinue
    if (-not $KeepFixture) {
        Remove-Item -LiteralPath $PcmPath -Force -ErrorAction SilentlyContinue
    }
    if (-not $KeepRuntimeData) {
        Remove-ContainedDirectory -Target $DataDir -AllowedRoot $ArtifactDir
    }
}
