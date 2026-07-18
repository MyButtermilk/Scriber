<#
.SYNOPSIS
Builds a Windows desktop release bundle for Scriber.

.DESCRIPTION
Runs the frontend type check, prepares the Python backend sidecar, builds the
Tauri Windows bundle, and optionally runs the release smoke test. The checked-in
Tauri `beforeBundleCommand` still supports direct `npm run tauri:build`, while
this release orchestrator prepares the sidecar before Tauri validates bundle
resources.

Typical flow:
  powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string[]]$Bundles = @("nsis"),
    [string]$ReleaseBaseUrl = "",
    [switch]$EnableTauriUpdater,
    [switch]$ConfigureTauriUpdaterRuntime,
    [switch]$UsePrebuiltTauriApp,
    [string]$UpdaterEndpoint = "",
    [string]$UpdaterPublicKey = "",
    [switch]$RequireUpdaterSignatures,
    [switch]$RequireAuthenticodeSignature,
    [string]$ExpectedAuthenticodePublisher = "",
    [switch]$RequireAuthenticodeTimestamp,
    [double]$MaxInstallerSizeMB = 220,
    [double]$InstallerMaxInstalledSizeMB = 0,
    [string]$MediaToolsDir = "",
    [switch]$UseProfileBFfmpeg,
    [switch]$SkipBundledFfprobe,
    [switch]$ValidateSlimMediaTools,
    [switch]$ReuseSidecarIfUnchanged,
    [switch]$FastLocalInstaller,
    [switch]$FastLocalStagedApp,
    [ValidateSet("", "lzma", "zlib", "bzip2", "none")]
    [string]$NsisCompression = "",
    [switch]$LocalPyInstallerNoClean,
    [switch]$RustAudioIsolatedTarget,
    [switch]$ParallelizeIndependentBuilds,
    [switch]$RunRuntimeDependencyFootprint,
    [double]$MaxScipyRuntimeDependencyMB = 0,
    [double]$MaxOnnxRuntimeDependencyMB = 0,
    [double]$MaxPythonRuntimeDependencyMB = 0,
    [double]$MaxBackendRuntimeDependencyMB = 0,
    [double]$MaxInternalRuntimeDependencyMB = 0,
    [double]$MaxMediaToolsRuntimeDependencyMB = 0,
    [double]$MaxPySide6RuntimeDependencyMB = 0,
    [double]$MaxGoogleGrpcRuntimeDependencyMB = 0,
    [double]$MaxPillowRuntimeDependencyMB = 0,
    [switch]$SkipChecks,
    [switch]$SkipPythonTests,
    [switch]$SkipFrontendTypeCheck,
    [switch]$SkipSmoke,
    [switch]$RunInstallerSmoke,
    [switch]$RunInstallerCrashSmoke,
    [switch]$RunInstallerPortConflictSmoke,
    [switch]$RunInstallerControlledShutdownSmoke,
    [switch]$RunInstallerExternalBackendSmoke,
    [switch]$RunInstallerStartupTimeoutSmoke,
    [switch]$RunInstallerGlobalHotkeyRegistrationSmoke,
    [switch]$RunInstallerGlobalHotkeySmoke,
    [switch]$RunInstallerManualGlobalHotkeySmoke,
    [switch]$RunInstallerSupportBundleSmoke,
    [switch]$RunInstallerFrontendSmoke,
    [switch]$RunInstallerMeetingAudioDeviceTestSmoke,
    [switch]$RunInstallerMediaPreparationSmoke,
    [switch]$RunInstallerRealMediaWorkflowSmoke,
    [string]$InstallerRealWorkflowYoutubeUrl = "https://www.youtube.com/watch?v=0wEjbSYNUM8",
    [int]$InstallerRealWorkflowFileTimeoutSec = 240,
    [int]$InstallerRealWorkflowYoutubeTimeoutSec = 420,
    [int]$InstallerRealWorkflowPollSec = 3,
    [switch]$InstallerRealWorkflowSkipFile,
    [switch]$InstallerRealWorkflowSkipYoutube,
    [switch]$InstallerRealWorkflowNoSummary,
    [string]$InstallerGlobalHotkeySmokeHotkey = "ctrl+alt+shift+f12",
    [int]$InstallerGlobalHotkeyDispatchTimeoutSec = 20,
    [switch]$RunInstallerStabilitySmoke,
    [int]$InstallerStabilityDurationSec = 15,
    [int]$InstallerStabilityProbeIntervalSec = 5,
    [double]$InstallerMaxBackendWorkingSetGrowthMB = 0,
    [double]$InstallerMaxIdleCpuPercent = 0,
    [switch]$RunInstallerLiveRecordingSmoke,
    [int]$InstallerLiveRecordingDurationSec = 0,
    [int]$InstallerLiveRecordingProbeIntervalSec = 5,
    [double]$InstallerMaxLiveBackendWorkingSetGrowthMB = 0,
    [double]$InstallerMaxLiveCpuPercent = 0,
    [int]$InstallerLiveRecordingStartTimeoutSec = 60,
    [int]$InstallerLiveRecordingStopTimeoutSec = 60,
    [string]$InstallerLiveRecordingEnvFile = "",
    [string]$InstallerLiveRecordingDefaultStt = "",
    [string]$InstallerLiveRecordingSonioxMode = "",
    [switch]$InstallerDisableLiveTextInjection,
    [ValidateSet("", "rust-wasapi")]
    [string]$InstallerLiveRecordingAudioEngine = "",
    [ValidateSet("", "synthetic", "wasapi")]
    [string]$InstallerLiveRecordingRustAudioCaptureMode = "",
    [switch]$InstallerLiveRecordingMicAlwaysOn,
    [switch]$RunInstallerLegacyDataSmoke,
    [switch]$RunInstallerUpgradeSmoke,
    [switch]$RunInstallerUninstallSmoke,
    [switch]$RunMediaPreparationSmoke
)

$ErrorActionPreference = "Stop"
$releasePython = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $releasePython -PathType Leaf)) {
    $releasePython = (Get-Command python -ErrorAction Stop).Source
}
$script:BuildTimingStarted = [System.Diagnostics.Stopwatch]::StartNew()
$script:BuildTimingPhases = [System.Collections.Generic.List[object]]::new()

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command
    )

    $stepWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $ok = $false
    try {
        Write-Host "==> $Label"
        & $Command
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE."
        }
        $ok = $true
    } finally {
        $stepWatch.Stop()
        $script:BuildTimingPhases.Add([ordered]@{
            label = $Label
            durationMs = [int64]$stepWatch.ElapsedMilliseconds
            ok = $ok
        }) | Out-Null
    }
}

function ConvertTo-NativeProcessArgument {
    param([AllowEmptyString()][string]$Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }

    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append([char]34)
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes += 1
            continue
        }
        if ($character -eq [char]34) {
            if ($backslashes -gt 0) {
                [void]$builder.Append([char]92, (2 * $backslashes))
            }
            [void]$builder.Append([char]92)
            [void]$builder.Append([char]34)
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append([char]92, $backslashes)
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append([char]92, (2 * $backslashes))
    }
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function Start-TrackedReleaseProcess {
    param(
        [string]$Label,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )

    $safeLabel = $Label.ToLowerInvariant() -replace '[^a-z0-9]+', '-'
    $logRoot = Join-Path $RepoRoot "build\parallel-release-tasks"
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    $stdoutPath = Join-Path $logRoot "$safeLabel.stdout.log"
    $stderrPath = Join-Path $logRoot "$safeLabel.stderr.log"
    foreach ($path in @($stdoutPath, $stderrPath)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force
        }
    }
    $argumentLine = @($Arguments | ForEach-Object { ConvertTo-NativeProcessArgument -Value ([string]$_) }) -join " "
    Write-Host "==> $Label (parallel)"
    $startedAt = (Get-Date).ToUniversalTime()
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $argumentLine `
        -WorkingDirectory $WorkingDirectory `
        -NoNewWindow `
        -PassThru `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath
    # Windows PowerShell 5.1 can return a Process object from Start-Process
    # without retaining its native process handle. Once that child exits,
    # ExitCode and ExitTime then resolve to $null even after WaitForExit().
    # Acquire the handle while the child is alive so the later parallel join
    # can reliably inspect its result.
    try {
        $null = $process.Handle
    } catch {
        $handleError = $_.Exception.Message
        try {
            if (-not $process.HasExited) {
                $process.Kill()
                $process.WaitForExit()
            }
        } catch {
            # Preserve the original handle-acquisition failure.
        } finally {
            $process.Dispose()
        }
        throw "Failed to retain the process handle for '$Label': $handleError"
    }
    return [pscustomobject]@{
        Label = $Label
        Process = $process
        StartedAt = $startedAt
        CompletedAt = $null
        Disposed = $false
        StdoutPath = $stdoutPath
        StderrPath = $stderrPath
    }
}

function Complete-TrackedReleaseProcesses {
    param([object[]]$Tasks)

    $nextProgressAt = (Get-Date).ToUniversalTime().AddSeconds(20)
    $lastProgressStatus = ""
    $lastProgressAt = [DateTime]::MinValue
    while ($true) {
        $now = (Get-Date).ToUniversalTime()
        foreach ($task in $Tasks) {
            if (-not $task.CompletedAt -and $task.Process.HasExited) {
                $task.CompletedAt = $now
            }
        }
        if (@($Tasks | Where-Object { -not $_.CompletedAt }).Count -eq 0) {
            break
        }
        if ($now -ge $nextProgressAt) {
            $status = @(
                $Tasks | ForEach-Object {
                    $state = if ($_.Process.HasExited) { "done" } else { "running" }
                    "$($_.Label)=$state"
                }
            ) -join "; "
            if ($status -ne $lastProgressStatus -or ($now - $lastProgressAt).TotalSeconds -ge 60) {
                Write-Host "Parallel release preparation: $status"
                $lastProgressStatus = $status
                $lastProgressAt = $now
            }
            $nextProgressAt = $now.AddSeconds(20)
        }
        Start-Sleep -Seconds 2
    }

    $failures = [System.Collections.Generic.List[string]]::new()
    foreach ($task in $Tasks) {
        $task.Process.WaitForExit()
        if (Test-Path -LiteralPath $task.StdoutPath -PathType Leaf) {
            Get-Content -LiteralPath $task.StdoutPath | Out-Host
        }
        if (Test-Path -LiteralPath $task.StderrPath -PathType Leaf) {
            Get-Content -LiteralPath $task.StderrPath | Out-Host
        }
        $finishedAt = $task.CompletedAt
        $ok = $task.Process.ExitCode -eq 0
        $script:BuildTimingPhases.Add([ordered]@{
            label = $task.Label
            durationMs = [Math]::Max(0, [int64](($finishedAt - $task.StartedAt).TotalMilliseconds))
            ok = $ok
            parallel = $true
        }) | Out-Null
        if (-not $ok) {
            $failures.Add("$($task.Label) failed with exit code $($task.Process.ExitCode).") | Out-Null
        }
        $task.Process.Dispose()
        $task.Disposed = $true
    }

    if ($failures.Count -gt 0) {
        throw ($failures -join " ")
    }
}

function Stop-TrackedReleaseProcesses {
    param([object[]]$Tasks)

    foreach ($task in $Tasks) {
        if (-not $task.Process -or $task.Disposed) {
            continue
        }
        try {
            if (-not $task.Process.HasExited) {
                $task.Process.Kill()
                $task.Process.WaitForExit()
            }
        } catch {
            Write-Warning "Could not stop parallel release task '$($task.Label)': $($_.Exception.Message)"
        } finally {
            $task.Process.Dispose()
            $task.Disposed = $true
        }
    }
}

function New-SidecarBuildScriptArguments {
    $sidecarArgs = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "scripts\build_tauri_backend_sidecar.ps1",
        "-SkipFrontendBuild",
        "-InstallPyInstaller",
        "-BundleMediaTools",
        "-BundleRustAudioSidecar",
        "-BundleRustDiarizationSidecar",
        "-CopyToTauriRelease"
    )
    if ($SkipBundledFfprobe) {
        $sidecarArgs += "-SkipBundledFfprobe"
    }
    if ($ValidateSlimMediaTools) {
        $sidecarArgs += "-ValidateSlimMediaTools"
    }
    if ($UseProfileBFfmpeg) {
        $sidecarArgs += "-UseProfileBFfmpeg"
    }
    if ($MediaToolsDir) {
        $sidecarArgs += @("-MediaToolsDir", $MediaToolsDir)
    }
    if ($ReuseSidecarIfUnchanged) {
        $sidecarArgs += "-ReuseSidecarIfUnchanged"
    }
    if ($LocalPyInstallerNoClean) {
        $sidecarArgs += "-LocalPyInstallerNoClean"
    }
    if ($ParallelizeIndependentBuilds) {
        # Prestage only the independent diarization cache beside PyInstaller.
        # Do not forward the broader sidecar parallel switch: it would also
        # move Rust audio back to a cold isolated Cargo target.
        $sidecarArgs += "-ParallelizeRustDiarizationBuild"
    }
    # The outer release DAG already overlaps PyInstaller with the Tauri app
    # compile. Keep Rust audio on the shared, restored Tauri target by default:
    # once the app compile has populated that target, an audio cache miss can
    # reuse its dependencies instead of recompiling them in a cold 2+ GiB
    # isolated target. Cargo's target lock safely bounds the rare overlap.
    if ($RustAudioIsolatedTarget) {
        $sidecarArgs += "-RustAudioIsolatedTarget"
    }
    return $sidecarArgs
}

function Write-BuildTimingReport {
    param(
        [string]$MetadataDir,
        [string]$SidecarMetadataPath,
        [object]$BuildMode
    )

    New-Item -ItemType Directory -Force -Path $MetadataDir | Out-Null
    $script:BuildTimingStarted.Stop()
    $sidecarMetadata = $null
    if ($SidecarMetadataPath -and (Test-Path -LiteralPath $SidecarMetadataPath -PathType Leaf)) {
        $sidecarMetadata = Get-Content -LiteralPath $SidecarMetadataPath -Raw | ConvertFrom-Json
    }
    $payload = [ordered]@{
        apiVersion = "1"
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        totalDurationMs = [int64]$script:BuildTimingStarted.ElapsedMilliseconds
        phases = @($script:BuildTimingPhases)
        sidecar = $sidecarMetadata
        buildMode = $BuildMode
    }
    $path = Join-Path $MetadataDir "build-timing.json"
    $payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $path -Encoding utf8
    return $path
}

function Get-LogMatchCount {
    param(
        [string[]]$Lines,
        [string]$Pattern
    )

    return @($Lines | Select-String -Pattern $Pattern).Count
}

function Remove-AnsiEscapeSequences {
    param([string]$Value)

    if ($null -eq $Value) {
        return ""
    }
    $withoutEsc = [regex]::Replace($Value, "\x1B\[[0-?]*[ -/]*[@-~]", "")
    return [regex]::Replace($withoutEsc, "\^\[\[[0-?]*[ -/]*[@-~]", "")
}

function Get-TauriLogRecords {
    param([string[]]$Lines)

    $records = [System.Collections.Generic.List[object]]::new()
    foreach ($line in $Lines) {
        $timestamp = $null
        $message = $line
        if ($line -match '^(?<timestamp>\d{4}-\d{2}-\d{2}T[^\t]+)\t(?<message>.*)$') {
            $timestamp = [DateTimeOffset]::Parse($Matches.timestamp)
            $message = $Matches.message
        }
        $records.Add([pscustomobject]@{
            timestamp = $timestamp
            message = $message
            cleanMessage = Remove-AnsiEscapeSequences -Value $message
        }) | Out-Null
    }
    return @($records)
}

function Get-FirstTauriLogRecord {
    param(
        [object[]]$Records,
        [string]$Pattern
    )

    foreach ($record in $Records) {
        if ($record.cleanMessage -match $Pattern) {
            return $record
        }
    }
    return $null
}

function Format-TauriLogTimestamp {
    param([object]$Record)

    if ($null -eq $Record -or $null -eq $Record.timestamp) {
        return $null
    }
    return $Record.timestamp.ToUniversalTime().ToString("o")
}

function Get-TauriLogDurationSeconds {
    param(
        [object]$Start,
        [object]$End
    )

    if ($null -eq $Start -or $null -eq $End -or $null -eq $Start.timestamp -or $null -eq $End.timestamp) {
        return $null
    }
    return [Math]::Round(($End.timestamp - $Start.timestamp).TotalSeconds, 3)
}

function New-TauriBundleLogSummary {
    param(
        [string]$Path
    )

    $payload = [ordered]@{
        apiVersion = "1"
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        path = $Path
        exists = $false
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $payload
    }

    $item = Get-Item -LiteralPath $Path
    $lines = @(Get-Content -LiteralPath $Path)
    $records = @(Get-TauriLogRecords -Lines $lines)
    $messageLines = @($records | ForEach-Object { $_.cleanMessage })
    $counts = [ordered]@{
        cargoUpdatingIndex = Get-LogMatchCount -Lines $messageLines -Pattern "Updating crates.io index"
        cargoDownloaded = Get-LogMatchCount -Lines $messageLines -Pattern "^\s*Downloaded\s+"
        cargoDownloading = Get-LogMatchCount -Lines $messageLines -Pattern "^\s*Downloading\s+"
        cargoCompiling = Get-LogMatchCount -Lines $messageLines -Pattern "^\s*Compiling\s+"
        cargoFinished = Get-LogMatchCount -Lines $messageLines -Pattern "(?i)^\s*Finished\s+.*profile.*target\(s\)"
        tauriBundling = Get-LogMatchCount -Lines $messageLines -Pattern "(?i)\bbundl(?:e|ing)\b"
        nsis = Get-LogMatchCount -Lines $messageLines -Pattern "(?i)\bnsis\b|makensis"
        signing = Get-LogMatchCount -Lines $messageLines -Pattern "(?i)\bsign(?:ing|ed|ature)?\b"
        updater = Get-LogMatchCount -Lines $messageLines -Pattern "(?i)\bupdater\b|latest\.json|\.sig"
        warnings = Get-LogMatchCount -Lines $messageLines -Pattern "(?i)\bwarning:"
        errors = Get-LogMatchCount -Lines $messageLines -Pattern "(?i)\berror:"
    }
    $firstCargoCompileLines = @(
        $messageLines |
            Select-String -Pattern "^\s*Compiling\s+" |
            Select-Object -First 12 |
            ForEach-Object { $_.Line.Trim() }
    )
    $firstRecord = $records | Where-Object { $null -ne $_.timestamp } | Select-Object -First 1
    $lastRecord = $records | Where-Object { $null -ne $_.timestamp } | Select-Object -Last 1
    $cargoIndexRecord = Get-FirstTauriLogRecord -Records $records -Pattern "Updating crates.io index"
    $firstCompileRecord = Get-FirstTauriLogRecord -Records $records -Pattern "^\s*Compiling\s+"
    $cargoFinishedRecord = Get-FirstTauriLogRecord -Records $records -Pattern "(?i)^\s*Finished\s+.*profile.*target\(s\)"
    $makensisRecord = Get-FirstTauriLogRecord -Records $records -Pattern "(?i)makensis"
    $updaterSignatureRecord = Get-FirstTauriLogRecord -Records $records -Pattern "(?i)Finished\s+\d+\s+updater signature"

    $payload["exists"] = $true
    $payload["sizeBytes"] = [int64]$item.Length
    $payload["lineCount"] = [int64]$lines.Count
    $payload["counts"] = $counts
    $payload["signals"] = [ordered]@{
        crateIndexUpdateDetected = [bool]($counts.cargoUpdatingIndex -gt 0)
        crateDownloadsDetected = [bool](($counts.cargoDownloaded + $counts.cargoDownloading) -gt 0)
        cargoCompileDetected = [bool]($counts.cargoCompiling -gt 0)
        nsisDetected = [bool]($counts.nsis -gt 0)
        signingDetected = [bool]($counts.signing -gt 0)
        updaterArtifactDetected = [bool]($counts.updater -gt 0)
    }
    $payload["milestones"] = [ordered]@{
        firstLineAt = Format-TauriLogTimestamp -Record $firstRecord
        cargoIndexAt = Format-TauriLogTimestamp -Record $cargoIndexRecord
        firstCargoCompileAt = Format-TauriLogTimestamp -Record $firstCompileRecord
        cargoFinishedAt = Format-TauriLogTimestamp -Record $cargoFinishedRecord
        makensisAt = Format-TauriLogTimestamp -Record $makensisRecord
        updaterSignatureAt = Format-TauriLogTimestamp -Record $updaterSignatureRecord
        lastLineAt = Format-TauriLogTimestamp -Record $lastRecord
    }
    $payload["durations"] = [ordered]@{
        firstLineToMakensisSeconds = Get-TauriLogDurationSeconds -Start $firstRecord -End $makensisRecord
        makensisToUpdaterSignatureSeconds = Get-TauriLogDurationSeconds -Start $makensisRecord -End $updaterSignatureRecord
        firstLineToUpdaterSignatureSeconds = Get-TauriLogDurationSeconds -Start $firstRecord -End $updaterSignatureRecord
        firstLineToLastLineSeconds = Get-TauriLogDurationSeconds -Start $firstRecord -End $lastRecord
    }
    $payload["firstCargoCompileLines"] = $firstCargoCompileLines
    $payload["tail"] = @($messageLines | Select-Object -Last 20)
    return $payload
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
$frontendRoot = Join-Path $RepoRoot "Frontend"
$bundleArg = ($Bundles -join ",")
$tauriConfigPath = Join-Path $RepoRoot "Frontend\src-tauri\tauri.conf.json"
$tauriBundleLogPath = Join-Path $RepoRoot "build\tauri-release-config\tauri-windows-bundle.log"
if ($MediaToolsDir) {
    $MediaToolsDir = (Resolve-Path $MediaToolsDir).Path
}
if ($UseProfileBFfmpeg) {
    $ValidateSlimMediaTools = $true
}

if ($FastLocalInstaller -and $FastLocalStagedApp) {
    throw "Use either -FastLocalInstaller or -FastLocalStagedApp, not both."
}
if ($EnableTauriUpdater -and $ConfigureTauriUpdaterRuntime) {
    throw "Use either -EnableTauriUpdater or -ConfigureTauriUpdaterRuntime, not both."
}
if ($UsePrebuiltTauriApp -and $FastLocalStagedApp) {
    throw "-UsePrebuiltTauriApp cannot be combined with -FastLocalStagedApp."
}
if ($ParallelizeIndependentBuilds -and $FastLocalStagedApp) {
    throw "-ParallelizeIndependentBuilds cannot be combined with -FastLocalStagedApp."
}
if ($FastLocalStagedApp -and $NsisCompression) {
    throw "-NsisCompression only applies to installer builds, not -FastLocalStagedApp."
}
if ($LocalPyInstallerNoClean -and -not ($FastLocalInstaller -or $FastLocalStagedApp)) {
    throw "-LocalPyInstallerNoClean is only allowed with -FastLocalInstaller or -FastLocalStagedApp."
}

if ($FastLocalInstaller) {
    $ReuseSidecarIfUnchanged = $true
    $SkipPythonTests = $true
    $SkipSmoke = $true
    $RunMediaPreparationSmoke = $true
    $RunRuntimeDependencyFootprint = $true
    if (-not $MediaToolsDir) {
        $UseProfileBFfmpeg = $true
        $ValidateSlimMediaTools = $true
    }
    if (-not $NsisCompression) {
        $NsisCompression = "lzma"
    }

    if ($MaxScipyRuntimeDependencyMB -le 0) {
        $MaxScipyRuntimeDependencyMB = 0.001
    }
    if ($MaxOnnxRuntimeDependencyMB -le 0) {
        $MaxOnnxRuntimeDependencyMB = 40
    }
    if ($MaxBackendRuntimeDependencyMB -le 0) {
        $MaxBackendRuntimeDependencyMB = if ($UseProfileBFfmpeg) { 325 } else { 500 }
    }
    if ($MaxPythonRuntimeDependencyMB -le 0) {
        $MaxPythonRuntimeDependencyMB = $MaxBackendRuntimeDependencyMB
    }
    if ($MaxInternalRuntimeDependencyMB -le 0) {
        $MaxInternalRuntimeDependencyMB = 250
    }
    if ($MaxMediaToolsRuntimeDependencyMB -le 0) {
        $MaxMediaToolsRuntimeDependencyMB = if ($UseProfileBFfmpeg) { 115 } else { 315 }
    }
    if ($MaxGoogleGrpcRuntimeDependencyMB -le 0) {
        $MaxGoogleGrpcRuntimeDependencyMB = 15
    }
    if ($MaxPillowRuntimeDependencyMB -le 0) {
        $MaxPillowRuntimeDependencyMB = 6
    }
}

if ($FastLocalStagedApp) {
    $ReuseSidecarIfUnchanged = $true
    $SkipPythonTests = $true
    $RunMediaPreparationSmoke = $true
    $RunRuntimeDependencyFootprint = $true
    if (-not $MediaToolsDir) {
        $UseProfileBFfmpeg = $true
        $ValidateSlimMediaTools = $true
    }

    if ($MaxScipyRuntimeDependencyMB -le 0) {
        $MaxScipyRuntimeDependencyMB = 0.001
    }
    if ($MaxOnnxRuntimeDependencyMB -le 0) {
        $MaxOnnxRuntimeDependencyMB = 40
    }
    if ($MaxBackendRuntimeDependencyMB -le 0) {
        $MaxBackendRuntimeDependencyMB = if ($UseProfileBFfmpeg) { 325 } else { 500 }
    }
    if ($MaxPythonRuntimeDependencyMB -le 0) {
        $MaxPythonRuntimeDependencyMB = $MaxBackendRuntimeDependencyMB
    }
    if ($MaxInternalRuntimeDependencyMB -le 0) {
        $MaxInternalRuntimeDependencyMB = 250
    }
    if ($MaxMediaToolsRuntimeDependencyMB -le 0) {
        $MaxMediaToolsRuntimeDependencyMB = if ($UseProfileBFfmpeg) { 115 } else { 315 }
    }
    if ($MaxGoogleGrpcRuntimeDependencyMB -le 0) {
        $MaxGoogleGrpcRuntimeDependencyMB = 15
    }
    if ($MaxPillowRuntimeDependencyMB -le 0) {
        $MaxPillowRuntimeDependencyMB = 6
    }
}

if (-not (Test-Path (Join-Path $frontendRoot "package.json"))) {
    throw "Frontend package.json was not found under $frontendRoot."
}

Invoke-Checked -Label "Version sync" -Command {
    Push-Location $RepoRoot
    try {
        & $releasePython scripts\sync_version.py
    } finally {
        Pop-Location
    }
}

$currentVersion = (& $releasePython -c "from scripts.create_release_metadata import read_version; print(read_version())").Trim()

if (-not $SkipChecks -and -not $SkipPythonTests) {
    Invoke-Checked -Label "Python tests" -Command {
        Push-Location $RepoRoot
        try {
            & $releasePython -m pytest -q
        } finally {
            Pop-Location
        }
    }
}

$runFrontendTypeCheck = -not $SkipChecks -and -not $SkipFrontendTypeCheck
if ($runFrontendTypeCheck -and -not $ParallelizeIndependentBuilds) {
    Invoke-Checked -Label "Frontend type check" -Command {
        Push-Location $frontendRoot
        try {
            npm run check
        } finally {
            Pop-Location
        }
    }
}

try {
    if ($EnableTauriUpdater) {
        if (-not $env:TAURI_SIGNING_PRIVATE_KEY -and $env:TAURI_SIGNING_PRIVATE_KEY_PATH) {
            if (-not (Test-Path -LiteralPath $env:TAURI_SIGNING_PRIVATE_KEY_PATH -PathType Leaf)) {
                throw "TAURI_SIGNING_PRIVATE_KEY_PATH does not point to a file."
            }
            $env:TAURI_SIGNING_PRIVATE_KEY = Get-Content -LiteralPath $env:TAURI_SIGNING_PRIVATE_KEY_PATH -Raw
        }
        $RequireUpdaterSignatures = $true
    }

    $tauriBuildConfigPath = Join-Path $RepoRoot "build\tauri-release-config\tauri.generated.conf.json"
    if (-not $FastLocalStagedApp) {
        Invoke-Checked -Label "Prepare Tauri build config" -Command {
            Push-Location $RepoRoot
            try {
                $configArgs = @(
                    "scripts\prepare_tauri_updater_config.py",
                    "--config",
                    $tauriConfigPath,
                    "--output",
                    $tauriBuildConfigPath,
                    "--version",
                    $currentVersion,
                    "--remove-before-bundle-command"
                )
                if ($NsisCompression) {
                    $configArgs += @("--nsis-compression", $NsisCompression)
                }
                if ($EnableTauriUpdater -or $ConfigureTauriUpdaterRuntime) {
                    if ($UpdaterEndpoint) {
                        $configArgs += @("--endpoint", $UpdaterEndpoint)
                    }
                    if ($UpdaterPublicKey) {
                        $configArgs += @("--public-key", $UpdaterPublicKey)
                    }
                    if ($ConfigureTauriUpdaterRuntime) {
                        $configArgs += @("--skip-signing-key-check", "--skip-updater-artifacts")
                    }
                } else {
                    $configArgs += "--skip-updater-config"
                }
                & $releasePython @configArgs
            } finally {
                Pop-Location
            }
        }
    }

    $tauriAppBuiltBeforeBundle = $false
    $tauriAppBuiltInParallel = $false
    if ($FastLocalStagedApp) {
        Invoke-Checked -Label "Tauri staged sidecar preparation" -Command {
            Push-Location $RepoRoot
            try {
                $sidecarArgs = New-SidecarBuildScriptArguments
                powershell @sidecarArgs
            } finally {
                Pop-Location
            }
        }

        Invoke-Checked -Label "Tauri staged app build" -Command {
            Push-Location $frontendRoot
            try {
                npm run tauri:build -- --no-bundle
            } finally {
                Pop-Location
            }
        }
    } else {
        $parallelTasks = @()
        if ($ParallelizeIndependentBuilds) {
            try {
                $powershellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
                $prepareTauriScript = Join-Path $RepoRoot "scripts\ci\prepare_tauri_app.ps1"
                if (-not (Test-Path -LiteralPath $prepareTauriScript -PathType Leaf)) {
                    throw "Parallel Tauri preparation helper was not found: $prepareTauriScript"
                }
                if ($runFrontendTypeCheck) {
                    $parallelTasks += Start-TrackedReleaseProcess `
                        -Label "Frontend type check" `
                        -FilePath $powershellExe `
                        -Arguments @(
                            "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", $prepareTauriScript,
                            "-Mode", "TypeCheck",
                            "-RepoRoot", $RepoRoot
                        ) `
                        -WorkingDirectory $RepoRoot
                }

                $sidecarArgs = New-SidecarBuildScriptArguments
                $parallelTasks += Start-TrackedReleaseProcess `
                    -Label "Tauri sidecar preparation" `
                    -FilePath $powershellExe `
                    -Arguments $sidecarArgs `
                    -WorkingDirectory $RepoRoot

                if (-not $UsePrebuiltTauriApp) {
                    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tauriBundleLogPath) | Out-Null
                    if (Test-Path -LiteralPath $tauriBundleLogPath -PathType Leaf) {
                        Remove-Item -LiteralPath $tauriBundleLogPath -Force
                    }
                    $tauriAppBuildArgs = @(
                        "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", $prepareTauriScript,
                        "-Mode", "BuildBinary",
                        "-RepoRoot", $RepoRoot,
                        "-ConfigPath", $tauriBuildConfigPath,
                        "-TauriLogPath", $tauriBundleLogPath
                    )
                    $parallelTasks += Start-TrackedReleaseProcess `
                        -Label "Tauri app binary build" `
                        -FilePath $powershellExe `
                        -Arguments $tauriAppBuildArgs `
                        -WorkingDirectory $RepoRoot
                    $tauriAppBuiltBeforeBundle = $true
                    $tauriAppBuiltInParallel = $true
                }

                Complete-TrackedReleaseProcesses -Tasks $parallelTasks
                $parallelTasks = @()
            } catch {
                Stop-TrackedReleaseProcesses -Tasks $parallelTasks
                throw
            }
        } else {
            Invoke-Checked -Label "Tauri sidecar preparation" -Command {
                Push-Location $RepoRoot
                try {
                    $sidecarArgs = New-SidecarBuildScriptArguments
                    powershell @sidecarArgs
                } finally {
                    Pop-Location
                }
            }
        }

        Invoke-Checked -Label "Tauri Windows bundle" -Command {
            Push-Location $frontendRoot
            try {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tauriBundleLogPath) | Out-Null
                $bundleExistingTauriApp = $UsePrebuiltTauriApp -or $tauriAppBuiltBeforeBundle
                if (-not $tauriAppBuiltBeforeBundle -and (Test-Path -LiteralPath $tauriBundleLogPath -PathType Leaf)) {
                    Remove-Item -LiteralPath $tauriBundleLogPath -Force
                }
                $tauriLogEncoding = New-Object System.Text.UTF8Encoding($false)
                $tauriLogWriter = [System.IO.StreamWriter]::new($tauriBundleLogPath, $tauriAppBuiltBeforeBundle, $tauriLogEncoding)
                $quotedConfigPath = $tauriBuildConfigPath.Replace('"', '\"')
                $quotedBundleArg = $bundleArg.Replace('"', '\"')
                if ($bundleExistingTauriApp) {
                    $prebuiltExe = Join-Path $RepoRoot "Frontend\src-tauri\target\release\scriber-desktop.exe"
                    if (-not (Test-Path -LiteralPath $prebuiltExe -PathType Leaf)) {
                        throw "Prebuilt Tauri app executable was not found: $prebuiltExe"
                    }
                    $tauriCommand = 'npm run tauri:bundle -- --bundles "{0}" --config "{1}" --ci 2>&1' -f $quotedBundleArg, $quotedConfigPath
                } else {
                    $tauriCommand = 'npm run tauri:build -- --bundles "{0}" --config "{1}" --ci 2>&1' -f $quotedBundleArg, $quotedConfigPath
                }
                try {
                    cmd.exe /d /s /c $tauriCommand |
                        ForEach-Object {
                            $line = $_.ToString()
                            $tauriLogWriter.WriteLine(("{0}`t{1}" -f (Get-Date).ToUniversalTime().ToString("o"), $line))
                            Write-Host $line
                        }
                } finally {
                    $tauriLogWriter.Dispose()
                }
                $tauriExitCode = $LASTEXITCODE
                if ($tauriExitCode -ne 0) {
                    throw "Tauri Windows bundle failed with exit code $tauriExitCode."
                }
            } finally {
                Pop-Location
            }
        }
    }

    $stagedDiarizationWorkerSmokePath = Join-Path $RepoRoot "build\tauri-release-config\diarization-worker-staged-smoke.json"
    Invoke-Checked -Label "Diarization worker staged resource smoke" -Command {
        Push-Location $RepoRoot
        try {
            if (Test-Path -LiteralPath $stagedDiarizationWorkerSmokePath -PathType Leaf) {
                Remove-Item -LiteralPath $stagedDiarizationWorkerSmokePath -Force
            }
            & $releasePython scripts\smoke_diarization_worker_resource.py `
                --root Frontend\src-tauri\target\release\backend `
                --output $stagedDiarizationWorkerSmokePath | Out-Host
        } finally {
            Pop-Location
        }
    }

    if (-not $SkipSmoke) {
        Invoke-Checked -Label "Tauri release smoke" -Command {
            Push-Location $RepoRoot
            try {
                powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1
            } finally {
                Pop-Location
            }
        }
    }

    $targetRelease = Join-Path $RepoRoot "Frontend\src-tauri\target\release"
    $releaseExe = Join-Path $targetRelease "scriber-desktop.exe"
    $bundleRoot = Join-Path $targetRelease "bundle"
    $metadataDir = Join-Path $targetRelease "release-metadata"
    $mediaPreparationSmokePath = Join-Path $metadataDir "media-preparation-smoke.json"
    $runtimeDependencyFootprintPath = Join-Path $metadataDir "runtime-dependency-footprint.json"
    $installedPackageSmokePath = Join-Path $metadataDir "installed-package-smoke.json"
    $diarizationWorkerStagedSmokePath = Join-Path $metadataDir "diarization-worker-staged-smoke.json"
    $tauriBundleLogMetadataPath = Join-Path $metadataDir "tauri-windows-bundle.log"
    $tauriBundleLogSummaryPath = Join-Path $metadataDir "tauri-bundle-log-summary.json"
    $installedPackageSmokeTempPath = Join-Path $RepoRoot "tmp\installer-smoke\installed-package-smoke.json"
    $buildTimingPath = Join-Path $metadataDir "build-timing.json"
    foreach ($staleReport in @($mediaPreparationSmokePath, $runtimeDependencyFootprintPath, $installedPackageSmokePath, $diarizationWorkerStagedSmokePath, $tauriBundleLogSummaryPath, $tauriBundleLogMetadataPath, $installedPackageSmokeTempPath)) {
        if (Test-Path -LiteralPath $staleReport -PathType Leaf) {
            Remove-Item -LiteralPath $staleReport -Force
        }
    }
    $mediaPreparationSmoke = [ordered]@{
        ran = [bool]$RunMediaPreparationSmoke
        path = $null
        generatedAt = $null
    }
    $runtimeDependencyFootprint = [ordered]@{
        ran = [bool]$RunRuntimeDependencyFootprint
        path = $null
        generatedAt = $null
    }
    $installedPackageSmoke = [ordered]@{
        ran = $false
        path = $null
        generatedAt = $null
    }
    if (-not (Test-Path -LiteralPath $stagedDiarizationWorkerSmokePath -PathType Leaf)) {
        throw "Diarization worker staged smoke did not write expected report: $stagedDiarizationWorkerSmokePath"
    }
    New-Item -ItemType Directory -Force -Path $metadataDir | Out-Null
    Copy-Item -LiteralPath $stagedDiarizationWorkerSmokePath -Destination $diarizationWorkerStagedSmokePath -Force
    if (Test-Path -LiteralPath $tauriBundleLogPath -PathType Leaf) {
        New-Item -ItemType Directory -Force -Path $metadataDir | Out-Null
        Copy-Item -LiteralPath $tauriBundleLogPath -Destination $tauriBundleLogMetadataPath -Force
        New-TauriBundleLogSummary -Path $tauriBundleLogMetadataPath |
            ConvertTo-Json -Depth 8 |
            Set-Content -LiteralPath $tauriBundleLogSummaryPath -Encoding utf8
    }
    $artifacts = @()
    if ((-not $FastLocalStagedApp) -and (Test-Path $bundleRoot)) {
        $allArtifacts = @(
            Get-ChildItem -Path $bundleRoot -Recurse -File -Include *.exe,*.msi |
                Sort-Object FullName
        )
        $currentArtifacts = @(
            $allArtifacts | Where-Object { $_.Name -like "*$currentVersion*" }
        )
        if ($currentArtifacts.Count -gt 0) {
            $artifacts = @($currentArtifacts | Select-Object -ExpandProperty FullName)
        } elseif ($allArtifacts.Count -gt 0) {
            throw (
                "Windows release artifacts were found, but none match current version " +
                "${currentVersion}: " +
                (($allArtifacts | ForEach-Object { $_.Name }) -join ", ")
            )
        }
        if ($artifacts.Count -gt 0) {
            Write-Host (
                "Release artifacts selected for metadata: " +
                (($artifacts | ForEach-Object { Split-Path -Leaf $_ }) -join ", ")
            )
        }
    }

    if ($RunMediaPreparationSmoke) {
        Invoke-Checked -Label "Media preparation smoke" -Command {
            Push-Location $RepoRoot
            try {
                New-Item -ItemType Directory -Force -Path $metadataDir | Out-Null
                $backendMediaToolsDir = Join-Path $targetRelease "backend\tools\ffmpeg"
                if (-not (Test-Path -LiteralPath $backendMediaToolsDir -PathType Container)) {
                    throw "Bundled backend media tools directory was not found: $backendMediaToolsDir"
                }
                $mediaSmokeArgs = @(
                    "scripts\smoke_media_preparation.py",
                    "--output",
                    $mediaPreparationSmokePath,
                    "--media-tools-dir",
                    $backendMediaToolsDir
                )
                if (-not $SkipBundledFfprobe) {
                    $mediaSmokeArgs += "--require-ffprobe"
                }
                & $releasePython @mediaSmokeArgs
                if (-not (Test-Path -LiteralPath $mediaPreparationSmokePath -PathType Leaf)) {
                    throw "Media preparation smoke did not write expected report: $mediaPreparationSmokePath"
                }
                $mediaPreparationSmoke["path"] = $mediaPreparationSmokePath
                $mediaPreparationSmoke["generatedAt"] = (Get-Item -LiteralPath $mediaPreparationSmokePath).LastWriteTimeUtc.ToString("o")
            } finally {
                Pop-Location
            }
        }
    }

    if ($RunRuntimeDependencyFootprint) {
        Invoke-Checked -Label "Runtime dependency footprint" -Command {
            Push-Location $RepoRoot
            try {
                New-Item -ItemType Directory -Force -Path $metadataDir | Out-Null
                $backendReleaseDir = Join-Path $targetRelease "backend"
                if (-not (Test-Path -LiteralPath $backendReleaseDir -PathType Container)) {
                    throw "Bundled backend directory was not found: $backendReleaseDir"
                }
                $footprintArgs = @(
                    "scripts\analyze_backend_runtime_dependencies.py",
                    "--sidecar-dir",
                    $backendReleaseDir,
                    "--output",
                    $runtimeDependencyFootprintPath
                )
                if ($MaxScipyRuntimeDependencyMB -gt 0) {
                    $footprintArgs += @("--max-scipy-mb", $MaxScipyRuntimeDependencyMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                if ($MaxOnnxRuntimeDependencyMB -gt 0) {
                    $footprintArgs += @("--max-onnxruntime-mb", $MaxOnnxRuntimeDependencyMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                if ($MaxPythonRuntimeDependencyMB -gt 0) {
                    $footprintArgs += @("--max-total-mb", $MaxPythonRuntimeDependencyMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                if ($MaxBackendRuntimeDependencyMB -gt 0) {
                    $footprintArgs += @("--max-backend-mb", $MaxBackendRuntimeDependencyMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                if ($MaxInternalRuntimeDependencyMB -gt 0) {
                    $footprintArgs += @("--max-internal-mb", $MaxInternalRuntimeDependencyMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                if ($MaxMediaToolsRuntimeDependencyMB -gt 0) {
                    $footprintArgs += @("--max-media-tools-mb", $MaxMediaToolsRuntimeDependencyMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                if ($MaxPySide6RuntimeDependencyMB -gt 0) {
                    $footprintArgs += @("--max-pyside6-mb", $MaxPySide6RuntimeDependencyMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                if ($MaxGoogleGrpcRuntimeDependencyMB -gt 0) {
                    $footprintArgs += @("--max-google-grpc-mb", $MaxGoogleGrpcRuntimeDependencyMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                if ($MaxPillowRuntimeDependencyMB -gt 0) {
                    $footprintArgs += @("--max-pillow-mb", $MaxPillowRuntimeDependencyMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                & $releasePython @footprintArgs
                if (-not (Test-Path -LiteralPath $runtimeDependencyFootprintPath -PathType Leaf)) {
                    throw "Runtime dependency footprint did not write expected report: $runtimeDependencyFootprintPath"
                }
                $runtimeDependencyFootprint["path"] = $runtimeDependencyFootprintPath
                $runtimeDependencyFootprint["generatedAt"] = (Get-Item -LiteralPath $runtimeDependencyFootprintPath).LastWriteTimeUtc.ToString("o")
            } finally {
                Pop-Location
            }
        }
    }

    if ($RequireAuthenticodeSignature) {
        $authenticodeTargets = @()
        if (Test-Path -LiteralPath $releaseExe) {
            $authenticodeTargets += $releaseExe
        }
        foreach ($artifact in $artifacts) {
            $authenticodeTargets += $artifact
        }

        if ($authenticodeTargets.Count -eq 0) {
            throw "No Windows release artifacts were found for Authenticode validation."
        }
        New-Item -ItemType Directory -Force -Path $metadataDir | Out-Null
        $authenticodeReportPath = Join-Path $metadataDir "authenticode.json"

        Invoke-Checked -Label "Authenticode signature validation" -Command {
            $authenticodeArgs = @(
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                (Join-Path $RepoRoot "scripts\validate_windows_authenticode.ps1"),
                "-Path"
            )
            foreach ($artifact in $authenticodeTargets) {
                $authenticodeArgs += $artifact
            }
            if ($ExpectedAuthenticodePublisher) {
                $authenticodeArgs += @("-ExpectedPublisher", $ExpectedAuthenticodePublisher)
            }
            if ($RequireAuthenticodeTimestamp) {
                $authenticodeArgs += "-RequireTimestamp"
            }
            $authenticodeArgs += @("-OutputPath", $authenticodeReportPath)
            powershell @authenticodeArgs
        }
    }

    if ($artifacts.Count -gt 0) {
        Invoke-Checked -Label "Release metadata" -Command {
            Push-Location $RepoRoot
            try {
                $metadataArgs = @(
                    "scripts\create_release_metadata.py",
                    "--output-dir",
                    $metadataDir
                )
                if ($ReleaseBaseUrl) {
                    $metadataArgs += @("--base-url", $ReleaseBaseUrl)
                }
                foreach ($artifact in $artifacts) {
                    $metadataArgs += @("--artifact", $artifact)
                }
                & $releasePython @metadataArgs
            } finally {
                Pop-Location
            }
        }

        Invoke-Checked -Label "Tauri updater metadata validation" -Command {
            Push-Location $RepoRoot
            try {
                $validationArgs = @(
                    "scripts\validate_tauri_updater_metadata.py",
                    "--metadata",
                    (Join-Path $metadataDir "latest.json"),
                    "--artifact-dir",
                    $bundleRoot,
                    "--sha256sums",
                    (Join-Path $metadataDir "SHA256SUMS.txt")
                )
                if ($RequireUpdaterSignatures) {
                    $validationArgs += "--require-signatures"
                } else {
                    $validationArgs += "--allow-local-urls"
                }
                & $releasePython @validationArgs
            } finally {
                Pop-Location
            }
        }

    }

    if ($RunInstallerSmoke -or $RunInstallerCrashSmoke -or $RunInstallerPortConflictSmoke -or $RunInstallerControlledShutdownSmoke -or $RunInstallerExternalBackendSmoke -or $RunInstallerStartupTimeoutSmoke -or $RunInstallerGlobalHotkeyRegistrationSmoke -or $RunInstallerGlobalHotkeySmoke -or $RunInstallerManualGlobalHotkeySmoke -or $RunInstallerSupportBundleSmoke -or $RunInstallerFrontendSmoke -or $RunInstallerMeetingAudioDeviceTestSmoke -or $RunInstallerMediaPreparationSmoke -or $RunInstallerRealMediaWorkflowSmoke -or $RunInstallerStabilitySmoke -or $RunInstallerLiveRecordingSmoke -or $RunInstallerLegacyDataSmoke -or $RunInstallerUpgradeSmoke -or $RunInstallerUninstallSmoke) {
        Invoke-Checked -Label "Installed package smoke" -Command {
            Push-Location $RepoRoot
            try {
                $installerSmokeArgs = @(
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    "scripts\smoke_windows_installer.ps1",
                    "-PythonExecutable",
                    $releasePython,
                    "-InstallerPath",
                    $artifacts[0],
                    "-OutputPath",
                    $installedPackageSmokeTempPath
                )
                if ($RunInstallerCrashSmoke) {
                    $installerSmokeArgs += "-SimulateBackendCrash"
                }
                if ($RunInstallerPortConflictSmoke) {
                    $installerSmokeArgs += "-OccupyDefaultPort"
                }
                if ($RunInstallerControlledShutdownSmoke) {
                    $installerSmokeArgs += "-SimulateBackendShutdown"
                }
                if ($RunInstallerExternalBackendSmoke) {
                    $installerSmokeArgs += "-AttachExternalBackend"
                }
                if ($RunInstallerStartupTimeoutSmoke) {
                    $installerSmokeArgs += "-SimulateBackendStartupTimeout"
                }
                if ($RunInstallerGlobalHotkeyRegistrationSmoke) {
                    $installerSmokeArgs += "-VerifyGlobalHotkeyRegistration"
                    $installerSmokeArgs += @("-GlobalHotkeySmokeHotkey", $InstallerGlobalHotkeySmokeHotkey)
                    $installerSmokeArgs += @("-GlobalHotkeyDispatchTimeoutSec", $InstallerGlobalHotkeyDispatchTimeoutSec.ToString())
                }
                if ($RunInstallerGlobalHotkeySmoke) {
                    $installerSmokeArgs += "-SimulateGlobalHotkey"
                    $installerSmokeArgs += @("-GlobalHotkeySmokeHotkey", $InstallerGlobalHotkeySmokeHotkey)
                    $installerSmokeArgs += @("-GlobalHotkeyDispatchTimeoutSec", $InstallerGlobalHotkeyDispatchTimeoutSec.ToString())
                }
                if ($RunInstallerManualGlobalHotkeySmoke) {
                    $installerSmokeArgs += "-WaitForManualGlobalHotkey"
                    $installerSmokeArgs += @("-GlobalHotkeySmokeHotkey", $InstallerGlobalHotkeySmokeHotkey)
                    $installerSmokeArgs += @("-GlobalHotkeyDispatchTimeoutSec", $InstallerGlobalHotkeyDispatchTimeoutSec.ToString())
                }
                if ($RunInstallerSupportBundleSmoke) {
                    $installerSmokeArgs += "-VerifySupportBundle"
                }
                if ($RunInstallerFrontendSmoke) {
                    $installerSmokeArgs += "-VerifyFrontend"
                }
                if ($RunInstallerMeetingAudioDeviceTestSmoke) {
                    $installerSmokeArgs += "-VerifyMeetingAudioDeviceTest"
                }
                if ($RunInstallerMediaPreparationSmoke) {
                    $installerSmokeArgs += "-VerifyMediaPreparation"
                    if ($SkipBundledFfprobe) {
                        $installerSmokeArgs += "-AllowMissingFfprobeForMediaPreparation"
                    }
                }
                if ($RunInstallerRealMediaWorkflowSmoke) {
                    $installerSmokeArgs += "-VerifyRealMediaWorkflows"
                    $installerSmokeArgs += @("-RealWorkflowYoutubeUrl", $InstallerRealWorkflowYoutubeUrl)
                    $installerSmokeArgs += @("-RealWorkflowFileTimeoutSec", $InstallerRealWorkflowFileTimeoutSec.ToString())
                    $installerSmokeArgs += @("-RealWorkflowYoutubeTimeoutSec", $InstallerRealWorkflowYoutubeTimeoutSec.ToString())
                    $installerSmokeArgs += @("-RealWorkflowPollSec", $InstallerRealWorkflowPollSec.ToString())
                    if ($InstallerRealWorkflowSkipFile) {
                        $installerSmokeArgs += "-RealWorkflowSkipFile"
                    }
                    if ($InstallerRealWorkflowSkipYoutube) {
                        $installerSmokeArgs += "-RealWorkflowSkipYoutube"
                    }
                    if ($InstallerRealWorkflowNoSummary) {
                        $installerSmokeArgs += "-RealWorkflowNoSummary"
                    }
                }
                if ($RunInstallerStabilitySmoke) {
                    $installerSmokeArgs += @("-StabilityDurationSec", $InstallerStabilityDurationSec.ToString())
                    $installerSmokeArgs += @("-StabilityProbeIntervalSec", $InstallerStabilityProbeIntervalSec.ToString())
                    if ($InstallerMaxBackendWorkingSetGrowthMB -gt 0) {
                        $installerSmokeArgs += @("-MaxBackendWorkingSetGrowthMB", $InstallerMaxBackendWorkingSetGrowthMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                    }
                    if ($InstallerMaxIdleCpuPercent -gt 0) {
                        $installerSmokeArgs += @("-MaxIdleCpuPercent", $InstallerMaxIdleCpuPercent.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                    }
                }
                if ($InstallerMaxInstalledSizeMB -gt 0) {
                    $installerSmokeArgs += @("-MaxInstalledSizeMB", $InstallerMaxInstalledSizeMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                if ($RunInstallerLiveRecordingSmoke) {
                    $liveDuration = if ($InstallerLiveRecordingDurationSec -gt 0) { $InstallerLiveRecordingDurationSec } else { 1800 }
                    $installerSmokeArgs += @("-LiveRecordingDurationSec", $liveDuration.ToString())
                    $installerSmokeArgs += @("-LiveRecordingProbeIntervalSec", $InstallerLiveRecordingProbeIntervalSec.ToString())
                    $installerSmokeArgs += @("-LiveRecordingStartTimeoutSec", $InstallerLiveRecordingStartTimeoutSec.ToString())
                    $installerSmokeArgs += @("-LiveRecordingStopTimeoutSec", $InstallerLiveRecordingStopTimeoutSec.ToString())
                    if ($InstallerLiveRecordingEnvFile) {
                        $installerSmokeArgs += @("-LiveRecordingEnvFile", $InstallerLiveRecordingEnvFile)
                    }
                    if ($InstallerLiveRecordingDefaultStt) {
                        $installerSmokeArgs += @("-LiveRecordingDefaultStt", $InstallerLiveRecordingDefaultStt)
                    }
                    if ($InstallerLiveRecordingSonioxMode) {
                        $installerSmokeArgs += @("-LiveRecordingSonioxMode", $InstallerLiveRecordingSonioxMode)
                    }
                    if ($InstallerDisableLiveTextInjection) {
                        $installerSmokeArgs += "-DisableLiveTextInjection"
                    }
                    if ($InstallerLiveRecordingAudioEngine) {
                        $installerSmokeArgs += @("-LiveRecordingAudioEngine", $InstallerLiveRecordingAudioEngine)
                    }
                    if ($InstallerLiveRecordingRustAudioCaptureMode) {
                        $installerSmokeArgs += @("-LiveRecordingRustAudioCaptureMode", $InstallerLiveRecordingRustAudioCaptureMode)
                    }
                    if ($InstallerLiveRecordingMicAlwaysOn) {
                        $installerSmokeArgs += "-LiveRecordingMicAlwaysOn"
                    }
                    if ($InstallerMaxLiveBackendWorkingSetGrowthMB -gt 0) {
                        $installerSmokeArgs += @("-MaxLiveBackendWorkingSetGrowthMB", $InstallerMaxLiveBackendWorkingSetGrowthMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                    }
                    if ($InstallerMaxLiveCpuPercent -gt 0) {
                        $installerSmokeArgs += @("-MaxLiveCpuPercent", $InstallerMaxLiveCpuPercent.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                    }
                }
                if ($RunInstallerLegacyDataSmoke) {
                    $installerSmokeArgs += @("-LegacyDataDir", $RepoRoot, "-VerifyLegacyDataMigration")
                }
                if ($RunInstallerUpgradeSmoke) {
                    $installerSmokeArgs += "-SimulateUpgrade"
                }
                if ($RunInstallerUninstallSmoke) {
                    $installerSmokeArgs += "-VerifyUninstall"
                }
                powershell @installerSmokeArgs
                if (-not (Test-Path -LiteralPath $installedPackageSmokeTempPath -PathType Leaf)) {
                    throw "Installed package smoke did not write expected report: $installedPackageSmokeTempPath"
                }
                New-Item -ItemType Directory -Force -Path $metadataDir | Out-Null
                Copy-Item -LiteralPath $installedPackageSmokeTempPath -Destination $installedPackageSmokePath -Force
                $installedPackageSmoke["ran"] = $true
                $installedPackageSmoke["path"] = $installedPackageSmokePath
                $installedPackageSmoke["generatedAt"] = (Get-Item -LiteralPath $installedPackageSmokePath).LastWriteTimeUtc.ToString("o")
            } finally {
                Pop-Location
            }
        }
    }

    if ($artifacts.Count -gt 0) {
        Invoke-Checked -Label "Release size report" -Command {
            Push-Location $RepoRoot
            try {
                $sizeReportArgs = @(
                    "scripts\create_release_size_report.py",
                    "--output",
                    (Join-Path $metadataDir "size-report.json"),
                    "--max-installer-mb",
                    $MaxInstallerSizeMB.ToString([System.Globalization.CultureInfo]::InvariantCulture),
                    "--top-root",
                    $bundleRoot
                )
                if ($InstallerMaxInstalledSizeMB -gt 0) {
                    $sizeReportArgs += @("--max-installed-mb", $InstallerMaxInstalledSizeMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
                }
                if (Test-Path -LiteralPath $installedPackageSmokePath -PathType Leaf) {
                    $sizeReportArgs += @("--installed-smoke-report", $installedPackageSmokePath)
                }
                $backendReleaseDir = Join-Path $targetRelease "backend"
                if (Test-Path -LiteralPath $backendReleaseDir -PathType Container) {
                    $sizeReportArgs += @("--top-root", $backendReleaseDir)
                }
                foreach ($artifact in $artifacts) {
                    $sizeReportArgs += @("--artifact", $artifact)
                }
                & $releasePython @sizeReportArgs
            } finally {
                Pop-Location
            }
        }
    }

    $runtimeAttestationPath = $null
    if ($FastLocalStagedApp) {
        Invoke-Checked -Label "FastLocal runtime attestation" -Command {
            Push-Location $RepoRoot
            try {
                & $releasePython scripts\perf\runtime_attestation.py write `
                    --repo-root $RepoRoot `
                    --install-root $targetRelease
                if ($LASTEXITCODE -ne 0) {
                    throw "FastLocal runtime attestation failed."
                }
            } finally {
                Pop-Location
            }
        }
        $runtimeAttestationPath = Join-Path $targetRelease "scriber-autoresearch-runtime-attestation.json"
        if (-not (Test-Path -LiteralPath $runtimeAttestationPath -PathType Leaf)) {
            throw "FastLocal runtime attestation did not write its expected manifest."
        }
    }

    $buildMode = [ordered]@{
        artifactKind = if ($FastLocalStagedApp) { "staged-app" } else { "installer" }
        devOnly = [bool]($FastLocalInstaller -or $FastLocalStagedApp -or $LocalPyInstallerNoClean)
        fastLocalInstaller = [bool]$FastLocalInstaller
        fastLocalStagedApp = [bool]$FastLocalStagedApp
        prebuiltTauriApp = [bool]$UsePrebuiltTauriApp
        parallelizeIndependentBuilds = [bool]$ParallelizeIndependentBuilds
        tauriAppBuiltBeforeBundle = [bool]$tauriAppBuiltBeforeBundle
        tauriAppBuiltInParallel = [bool]$tauriAppBuiltInParallel
        updaterRuntimeConfigured = [bool]($EnableTauriUpdater -or $ConfigureTauriUpdaterRuntime)
        installerBuilt = [bool]($artifacts.Count -gt 0)
        installerSmokeValidated = [bool]$installedPackageSmoke["ran"]
        diarizationWorkerStagedSmokeValidated = $true
        nsisCompression = if ($NsisCompression) { $NsisCompression } else { "tauri-default" }
        localPyInstallerNoClean = [bool]$LocalPyInstallerNoClean
        rustAudioIsolatedTarget = [bool]$RustAudioIsolatedTarget
        runtimeAttested = [bool]$runtimeAttestationPath
    }
    $sidecarMetadataPath = Join-Path $targetRelease "backend\sidecar-build-metadata.json"
    $buildTimingPath = Write-BuildTimingReport -MetadataDir $metadataDir -SidecarMetadataPath $sidecarMetadataPath -BuildMode $buildMode

    [pscustomobject]@{
        ok = $true
        bundles = $Bundles
        updaterEnabled = [bool]$EnableTauriUpdater
        updaterRuntimeConfigured = [bool]($EnableTauriUpdater -or $ConfigureTauriUpdaterRuntime)
        releaseExe = $releaseExe
        artifacts = $artifacts
        metadataDir = $metadataDir
        sizeReport = Join-Path $metadataDir "size-report.json"
        buildTiming = $buildTimingPath
        buildMode = $buildMode
        runtimeAttestation = $runtimeAttestationPath
        mediaPreparationSmoke = $mediaPreparationSmoke
        runtimeDependencyFootprint = $runtimeDependencyFootprint
        diarizationWorkerStagedSmoke = $diarizationWorkerStagedSmokePath
        installedPackageSmoke = $installedPackageSmoke
    } | ConvertTo-Json -Compress
} catch {
    $targetRelease = Join-Path $RepoRoot "Frontend\src-tauri\target\release"
    $metadataDir = Join-Path $targetRelease "release-metadata"
    $tauriBundleLogMetadataPath = Join-Path $metadataDir "tauri-windows-bundle.log"
    $tauriBundleLogSummaryPath = Join-Path $metadataDir "tauri-bundle-log-summary.json"
    New-Item -ItemType Directory -Force -Path $metadataDir | Out-Null
    if (Test-Path -LiteralPath $tauriBundleLogPath -PathType Leaf) {
        Copy-Item -LiteralPath $tauriBundleLogPath -Destination $tauriBundleLogMetadataPath -Force
        New-TauriBundleLogSummary -Path $tauriBundleLogMetadataPath |
            ConvertTo-Json -Depth 8 |
            Set-Content -LiteralPath $tauriBundleLogSummaryPath -Encoding utf8
    }
    $failureBuildMode = [ordered]@{
        artifactKind = if ($FastLocalStagedApp) { "staged-app" } else { "installer" }
        devOnly = [bool]($FastLocalInstaller -or $FastLocalStagedApp -or $LocalPyInstallerNoClean)
        fastLocalInstaller = [bool]$FastLocalInstaller
        fastLocalStagedApp = [bool]$FastLocalStagedApp
        prebuiltTauriApp = [bool]$UsePrebuiltTauriApp
        parallelizeIndependentBuilds = [bool]$ParallelizeIndependentBuilds
        tauriAppBuiltBeforeBundle = [bool]$tauriAppBuiltBeforeBundle
        tauriAppBuiltInParallel = [bool]$tauriAppBuiltInParallel
        updaterRuntimeConfigured = [bool]($EnableTauriUpdater -or $ConfigureTauriUpdaterRuntime)
        installerBuilt = $false
        installerSmokeValidated = $false
        nsisCompression = if ($NsisCompression) { $NsisCompression } else { "tauri-default" }
        localPyInstallerNoClean = [bool]$LocalPyInstallerNoClean
        rustAudioIsolatedTarget = [bool]$RustAudioIsolatedTarget
        failed = $true
    }
    $sidecarMetadataPath = Join-Path $targetRelease "backend\sidecar-build-metadata.json"
    Write-BuildTimingReport -MetadataDir $metadataDir -SidecarMetadataPath $sidecarMetadataPath -BuildMode $failureBuildMode | Out-Null
    throw
} finally {
}
