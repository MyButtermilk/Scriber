<#
.SYNOPSIS
Runs the final hybrid architecture release-readiness evidence gate.

.DESCRIPTION
This script orchestrates the final external-evidence checks after a release
candidate has been built, signed, published, and physically tested. It does not
replace the real hardware/signing/publication steps; it standardizes how their
evidence is validated and assembled into the final readiness verdict.
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$HardwareInputDir = "tmp\hybrid-baseline",
    [string]$MatrixValidationOutput = "",
    [string]$MeetingReleaseMatrixDir = "",
    [string]$MeetingMatrixValidationOutput = "",
    [string]$MeetingExpectedAppVersion = "",
    [switch]$RequireMeetingReleaseMatrix,
    [switch]$RunReleaseBuild,
    [string[]]$ReleaseBuildBundles = @("nsis"),
    [string]$ReleaseBuildReleaseBaseUrl = "",
    [switch]$ReleaseBuildEnableTauriUpdater,
    [string]$ReleaseBuildUpdaterEndpoint = "",
    [string]$ReleaseBuildUpdaterPublicKey = "",
    [switch]$ReleaseBuildRequireUpdaterSignatures,
    [switch]$ReleaseBuildRequireAuthenticodeSignature,
    [switch]$ReleaseBuildUseProfileBFfmpeg,
    [switch]$ReleaseBuildValidateSlimMediaTools,
    [switch]$ReleaseBuildReuseSidecarIfUnchanged,
    [switch]$ReleaseBuildRunMediaPreparationSmoke,
    [switch]$ReleaseBuildRunRuntimeDependencyFootprint,
    [switch]$RunMicrophoneHardwareMatrix,
    [string]$MicrophoneMatrixBaseUrl = "http://127.0.0.1:8765",
    [string]$MicrophoneMatrixToken = "",
    [double]$MicrophoneMatrixWaitSec = 60,
    [double]$MicrophoneMatrixPollSec = 1,
    [string]$MicrophoneMatrixUsbLabel = "",
    [string]$MicrophoneMatrixDockLabel = "",
    [string]$MicrophoneMatrixBluetoothLabel = "",
    [string]$MicrophoneMatrixFavoriteLabel = "",
    [switch]$MicrophoneMatrixAssumeCompleted,
    [switch]$MicrophoneMatrixForceRefreshEachPoll,
    [string]$UpdaterMetadata = "Frontend\src-tauri\target\release\release-metadata\latest.json",
    [string]$UpdaterArtifactDir = "Frontend\src-tauri\target\release\bundle\nsis",
    [string]$Sha256Sums = "Frontend\src-tauri\target\release\release-metadata\SHA256SUMS.txt",
    [string]$MediaPreparationReport = "Frontend\src-tauri\target\release\release-metadata\media-preparation-smoke.json",
    [string]$MediaToolsDir = "Frontend\src-tauri\target\release\backend\tools\ffmpeg",
    [string]$RuntimeDependencyFootprintReport = "Frontend\src-tauri\target\release\release-metadata\runtime-dependency-footprint.json",
    [string]$SidecarDir = "Frontend\src-tauri\target\release\backend",
    [string]$RustAudioSidecarReport = "",
    [string]$RustAudioSidecarExe = "",
    [double]$RustAudioSidecarDurationSec = 600,
    [double]$RustAudioSidecarSelectedDurationSec = 10,
    [int]$RustAudioSidecarPrebufferMs = 400,
    [switch]$RustAudioSidecarPrewarmBeforeCapture,
    [double]$RustAudioSidecarPrewarmDurationSec = 0.5,
    [string]$RustAudioPrewarmSidecarReport = "",
    [ValidateSet("synthetic", "wasapi")]
    [string]$RustAudioPrewarmSidecarMode = "synthetic",
    [double]$RustAudioPrewarmSidecarDurationSec = 1,
    [int]$RustAudioPrewarmSidecarPrebufferMs = 400,
    [string]$RustAudioAppPrewarmReport = "",
    [ValidateSet("synthetic", "wasapi")]
    [string]$RustAudioAppPrewarmMode = "wasapi",
    [double]$RustAudioAppPrewarmDurationSec = 1,
    [double]$RustAudioAppPrewarmPrewarmDurationSec = 1,
    [int]$RustAudioAppPrewarmPrebufferMs = 400,
    [int]$RustAudioAppPrewarmCaptureCycles = 1,
    [double]$MinRustAudioAppPrewarmDurationSec = 0,
    [double]$MinRustAudioAppPrewarmPrewarmDurationSec = 0,
    [int]$MinRustAudioAppPrewarmCaptureCycles = 0,
    [switch]$RustAudioAppPrewarmHonorFavoriteMic,
    [string]$InstalledLiveRecordingSmokeReport = "",
    [string]$InstalledLiveRecordingInstallerPath = "",
    [switch]$RunInstalledLiveRecordingSmoke,
    [switch]$UseExistingInstalledLiveRecordingSmokeReport,
    [switch]$RequireInstalledLiveRecordingSmoke,
    [switch]$RequireInstalledLiveRecordingRustAudio,
    [int]$InstalledLiveRecordingDurationSec = 0,
    [int]$InstalledLiveRecordingProbeIntervalSec = 5,
    [int]$InstalledLiveRecordingStartTimeoutSec = 60,
    [int]$InstalledLiveRecordingStopTimeoutSec = 90,
    [string]$InstalledLiveRecordingEnvFile = "",
    [string]$InstalledLiveRecordingDefaultStt = "",
    [string]$InstalledLiveRecordingSonioxMode = "",
    [switch]$InstalledLiveRecordingDisableTextInjection,
    [ValidateSet("", "rust-wasapi")]
    [string]$InstalledLiveRecordingAudioEngine = "",
    [ValidateSet("", "synthetic", "wasapi")]
    [string]$InstalledLiveRecordingRustAudioCaptureMode = "",
    [switch]$InstalledLiveRecordingMicAlwaysOn,
    [double]$MinInstalledLiveRecordingDurationSec = 0,
    [string]$TauriTextInjectionSmokeReport = "",
    [switch]$RequireTauriTextInjectionSmoke,
    [switch]$RunTauriTextInjectionSmoke,
    [switch]$UseExistingTauriTextInjectionSmokeReport,
    [string]$TauriTextInjectionSmokeText = "Scriber Tauri text injection readiness smoke.",
    [string]$TauriTextInjectionTargetTitle = "Scriber Tauri Injection Readiness Target",
    [double]$TauriTextInjectionSettleSec = 1,
    [double]$TauriTextInjectionTimeoutSec = 5,
    [switch]$TauriTextInjectionSkipTargetClick,
    [int]$TauriTextInjectionPasteRestoreDelayMs = 1500,
    [string]$TauriTextInjectionMatrixReport = "",
    [switch]$RequireTauriTextInjectionMatrix,
    [switch]$RunTauriTextInjectionMatrixBuilder,
    [switch]$UseExistingTauriTextInjectionMatrixReport,
    [string]$TauriTextInjectionMatrixInputDir = "",
    [string[]]$TauriTextInjectionMatrixScenario = @(),
    [string[]]$TauriTextInjectionMatrixUnsupportedOptional = @(),
    [switch]$RequireRustAudioSidecarSmoke,
    [switch]$RunRustAudioSidecarSmoke,
    [switch]$UseExistingRustAudioSidecarReport,
    [switch]$RequireRustAudioPrewarmSidecarSmoke,
    [switch]$RunRustAudioPrewarmSidecarSmoke,
    [switch]$UseExistingRustAudioPrewarmSidecarReport,
    [switch]$RequireRustAudioAppPrewarmSmoke,
    [switch]$RunRustAudioAppPrewarmSmoke,
    [switch]$UseExistingRustAudioAppPrewarmReport,
    [string]$RecordingHotPathComparisonReport = "",
    [switch]$RunRecordingHotPathComparison,
    [switch]$UseExistingRecordingHotPathComparisonReport,
    [switch]$RequireRecordingHotPathComparison,
    [int]$RecordingHotPathIterations = 3,
    [double]$RecordingHotPathSeconds = 2,
    [int]$RecordingHotPathTimeoutSec = 60,
    [string]$RecordingHotPathSpeechPrompt = "Scriber provider-backed Rust audio validation",
    [double]$RecordingHotPathSpeechDelaySec = 0.5,
    [string]$RecordingHotPathEnvFile = "",
    [string]$RecordingHotPathDefaultStt = "",
    [string]$RecordingHotPathSonioxMode = "",
    [double]$RecordingHotPathMaxAudioOwnedP95RegressionMs = 50,
    [ValidateSet("wasapi", "synthetic")]
    [string]$RecordingHotPathRustCaptureMode = "wasapi",
    [string]$RecordingHotPathExePath = "",
    [string]$RecordingHotPathPythonPath = "",
    [string]$RecordingHotPathBackendExePath = "",
    [string]$RecordingHotPathLegacyDataDir = "",
    [switch]$RecordingHotPathHidden,
    [switch]$RecordingHotPathSkipUiVisibleWait,
    [switch]$RecordingHotPathDisableDevFallback,
    [switch]$RecordingHotPathKeepArtifacts,
    [switch]$RequireRustAudioPromotionReadiness,
    [switch]$RequireRustEndpointInventory,
    [switch]$RequireDeviceRefreshEvidence,
    [string]$UpdaterPublicationUrl = "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
    [string]$UpdaterPublicationReport = "",
    [int]$UpdaterPublicationAttempts = 6,
    [double]$UpdaterPublicationRetryDelaySec = 10,
    [string[]]$AuthenticodePath = @(),
    [string]$AuthenticodeReport = "",
    [string]$ExpectedAuthenticodePublisher = "",
    [switch]$RequireAuthenticodeTimestamp,
    [string]$OutputPath = "",
    [switch]$UseExistingAuthenticodeReport,
    [switch]$UseExistingMediaPreparationReport,
    [switch]$UseExistingRuntimeDependencyFootprintReport,
    [switch]$UseExistingUpdaterPublicationReport,
    [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"

function Convert-ToFullPath {
    param(
        [string]$Path,
        [string]$Root
    )

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Root $Path))
}

function Convert-ToDisplayCommand {
    param([string[]]$CommandArgs)

    $displayArgs = @()
    for ($i = 0; $i -lt $CommandArgs.Count; $i++) {
        $arg = $CommandArgs[$i]
        if (($arg -eq "-Token" -or $arg -eq "--token") -and ($i + 1) -lt $CommandArgs.Count) {
            $displayArgs += $arg
            $displayArgs += "<session token>"
            $i += 1
            continue
        }
        $displayArgs += $arg
    }
    return (($displayArgs | ForEach-Object {
        if ($_ -match "\s" -or $_ -eq "") {
            '"' + ($_ -replace '"', '\"') + '"'
        } else {
            $_
        }
    }) -join " ")
}

function Write-Utf8NoBomJson {
    param(
        [string]$Path,
        [string]$Json
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Json, $encoding)
}

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command
    )

    Write-Host "==> $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Assert-ExistingFile {
    param(
        [string]$Path,
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not found: $Path"
    }
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
$HardwareInputDir = Convert-ToFullPath -Path $HardwareInputDir -Root $RepoRoot
New-Item -ItemType Directory -Force -Path $HardwareInputDir | Out-Null

if (-not $MatrixValidationOutput) {
    $MatrixValidationOutput = Join-Path $HardwareInputDir "microphone-hardware-matrix-validation.json"
} else {
    $MatrixValidationOutput = Convert-ToFullPath -Path $MatrixValidationOutput -Root $RepoRoot
}
if (-not $MeetingReleaseMatrixDir) {
    $MeetingReleaseMatrixDir = Join-Path $HardwareInputDir "meeting-release-matrix"
} else {
    $MeetingReleaseMatrixDir = Convert-ToFullPath -Path $MeetingReleaseMatrixDir -Root $RepoRoot
}
if (-not $MeetingMatrixValidationOutput) {
    $MeetingMatrixValidationOutput = Join-Path $MeetingReleaseMatrixDir "meeting-release-matrix-validation.json"
} else {
    $MeetingMatrixValidationOutput = Convert-ToFullPath -Path $MeetingMatrixValidationOutput -Root $RepoRoot
}
if (-not $MeetingExpectedAppVersion) {
    $versionSource = Get-Content (Join-Path $RepoRoot "src\version.py") -Raw
    $versionMatch = [regex]::Match($versionSource, '__version__\s*=\s*"([^"]+)"')
    if (-not $versionMatch.Success) {
        throw "Could not read Meeting expected app version from src\version.py."
    }
    $MeetingExpectedAppVersion = $versionMatch.Groups[1].Value
}
if (-not $UpdaterPublicationReport) {
    $UpdaterPublicationReport = Join-Path $HardwareInputDir "updater-publication.json"
} else {
    $UpdaterPublicationReport = Convert-ToFullPath -Path $UpdaterPublicationReport -Root $RepoRoot
}
if (-not $AuthenticodeReport) {
    $AuthenticodeReport = Join-Path $HardwareInputDir "authenticode.json"
} else {
    $AuthenticodeReport = Convert-ToFullPath -Path $AuthenticodeReport -Root $RepoRoot
}
if (-not $RustAudioSidecarReport) {
    $RustAudioSidecarReport = Join-Path $HardwareInputDir "rust-audio-sidecar-smoke.json"
} else {
    $RustAudioSidecarReport = Convert-ToFullPath -Path $RustAudioSidecarReport -Root $RepoRoot
}
if (-not $RustAudioPrewarmSidecarReport) {
    $RustAudioPrewarmSidecarReport = Join-Path $HardwareInputDir "rust-audio-prewarm-sidecar-smoke.json"
} else {
    $RustAudioPrewarmSidecarReport = Convert-ToFullPath -Path $RustAudioPrewarmSidecarReport -Root $RepoRoot
}
if (-not $RustAudioAppPrewarmReport) {
    $RustAudioAppPrewarmReport = Join-Path $HardwareInputDir "rust-audio-app-prewarm-smoke.json"
} else {
    $RustAudioAppPrewarmReport = Convert-ToFullPath -Path $RustAudioAppPrewarmReport -Root $RepoRoot
}
if (-not $RecordingHotPathComparisonReport) {
    $RecordingHotPathComparisonReport = Join-Path $HardwareInputDir "recording-hot-path-python-rust-comparison.json"
} else {
    $RecordingHotPathComparisonReport = Convert-ToFullPath -Path $RecordingHotPathComparisonReport -Root $RepoRoot
}
if ($RecordingHotPathExePath) {
    $RecordingHotPathExePath = Convert-ToFullPath -Path $RecordingHotPathExePath -Root $RepoRoot
}
if ($RecordingHotPathPythonPath) {
    $RecordingHotPathPythonPath = Convert-ToFullPath -Path $RecordingHotPathPythonPath -Root $RepoRoot
}
if ($RecordingHotPathBackendExePath) {
    $RecordingHotPathBackendExePath = Convert-ToFullPath -Path $RecordingHotPathBackendExePath -Root $RepoRoot
}
if ($RecordingHotPathLegacyDataDir) {
    $RecordingHotPathLegacyDataDir = Convert-ToFullPath -Path $RecordingHotPathLegacyDataDir -Root $RepoRoot
}
if ($RecordingHotPathEnvFile) {
    $RecordingHotPathEnvFile = Convert-ToFullPath -Path $RecordingHotPathEnvFile -Root $RepoRoot
}
if (-not $InstalledLiveRecordingSmokeReport) {
    $InstalledLiveRecordingSmokeReport = Join-Path $HardwareInputDir "installed-live-recording-smoke.json"
} else {
    $InstalledLiveRecordingSmokeReport = Convert-ToFullPath -Path $InstalledLiveRecordingSmokeReport -Root $RepoRoot
}
if ($InstalledLiveRecordingInstallerPath) {
    $InstalledLiveRecordingInstallerPath = Convert-ToFullPath -Path $InstalledLiveRecordingInstallerPath -Root $RepoRoot
}
if ($InstalledLiveRecordingEnvFile) {
    $InstalledLiveRecordingEnvFile = Convert-ToFullPath -Path $InstalledLiveRecordingEnvFile -Root $RepoRoot
}
if (-not $TauriTextInjectionSmokeReport) {
    $TauriTextInjectionSmokeReport = Join-Path $HardwareInputDir "tauri-text-injection-smoke.json"
} else {
    $TauriTextInjectionSmokeReport = Convert-ToFullPath -Path $TauriTextInjectionSmokeReport -Root $RepoRoot
}
if (-not $TauriTextInjectionMatrixReport) {
    $TauriTextInjectionMatrixReport = Join-Path $HardwareInputDir "tauri-text-injection-matrix.json"
} else {
    $TauriTextInjectionMatrixReport = Convert-ToFullPath -Path $TauriTextInjectionMatrixReport -Root $RepoRoot
}
if (-not $TauriTextInjectionMatrixInputDir) {
    $TauriTextInjectionMatrixInputDir = Join-Path $HardwareInputDir "tauri-text-injection"
} else {
    $TauriTextInjectionMatrixInputDir = Convert-ToFullPath -Path $TauriTextInjectionMatrixInputDir -Root $RepoRoot
}
if (-not $OutputPath) {
    $OutputPath = Join-Path $HardwareInputDir "hybrid-release-readiness.json"
} else {
    $OutputPath = Convert-ToFullPath -Path $OutputPath -Root $RepoRoot
}

$UpdaterMetadata = Convert-ToFullPath -Path $UpdaterMetadata -Root $RepoRoot
$UpdaterArtifactDir = Convert-ToFullPath -Path $UpdaterArtifactDir -Root $RepoRoot
$Sha256Sums = Convert-ToFullPath -Path $Sha256Sums -Root $RepoRoot
$MediaPreparationReport = Convert-ToFullPath -Path $MediaPreparationReport -Root $RepoRoot
$MediaToolsDir = Convert-ToFullPath -Path $MediaToolsDir -Root $RepoRoot
$RuntimeDependencyFootprintReport = Convert-ToFullPath -Path $RuntimeDependencyFootprintReport -Root $RepoRoot
$SidecarDir = Convert-ToFullPath -Path $SidecarDir -Root $RepoRoot
if ($RustAudioSidecarExe) {
    $RustAudioSidecarExe = Convert-ToFullPath -Path $RustAudioSidecarExe -Root $RepoRoot
}
$AuthenticodePath = @($AuthenticodePath | ForEach-Object { Convert-ToFullPath -Path $_ -Root $RepoRoot })
$releaseBuildGeneratedAuthenticodeReport = Join-Path $RepoRoot "Frontend\src-tauri\target\release\release-metadata\authenticode.json"

if ($RequireRustAudioPromotionReadiness) {
    $RequireRustAudioSidecarSmoke = $true
    $RustAudioSidecarPrewarmBeforeCapture = $true
    $RequireRustAudioAppPrewarmSmoke = $true
    $RequireInstalledLiveRecordingSmoke = $true
    $RequireInstalledLiveRecordingRustAudio = $true
    $InstalledLiveRecordingMicAlwaysOn = $true
    $RequireRecordingHotPathComparison = $true
    $RequireRustEndpointInventory = $true
    $RequireDeviceRefreshEvidence = $true
    if ($RustAudioSidecarDurationSec -lt 600) {
        $RustAudioSidecarDurationSec = 600
    }
    if ($RustAudioAppPrewarmDurationSec -lt 600) {
        $RustAudioAppPrewarmDurationSec = 600
    }
    if ($RustAudioAppPrewarmPrewarmDurationSec -lt 1800) {
        $RustAudioAppPrewarmPrewarmDurationSec = 1800
    }
    if ($MinRustAudioAppPrewarmDurationSec -lt 600) {
        $MinRustAudioAppPrewarmDurationSec = 600
    }
    if ($MinRustAudioAppPrewarmPrewarmDurationSec -lt 1800) {
        $MinRustAudioAppPrewarmPrewarmDurationSec = 1800
    }
    if ($RustAudioAppPrewarmCaptureCycles -lt 2) {
        $RustAudioAppPrewarmCaptureCycles = 2
    }
    if ($MinRustAudioAppPrewarmCaptureCycles -lt 2) {
        $MinRustAudioAppPrewarmCaptureCycles = 2
    }
    if ($MinInstalledLiveRecordingDurationSec -lt 600) {
        $MinInstalledLiveRecordingDurationSec = 600
    }
}

$effectiveRequireRustAudioSidecarSmoke = [bool]($RequireRustAudioSidecarSmoke -or $RustAudioSidecarPrewarmBeforeCapture)
$effectiveRequireRustAudioAppPrewarmSmoke = [bool]($RequireRustAudioAppPrewarmSmoke -or ($MinRustAudioAppPrewarmDurationSec -gt 0) -or ($MinRustAudioAppPrewarmPrewarmDurationSec -gt 0) -or ($MinRustAudioAppPrewarmCaptureCycles -gt 0))
$effectiveRequireInstalledLiveRecordingSmoke = [bool]($RequireInstalledLiveRecordingSmoke -or $RequireInstalledLiveRecordingRustAudio -or ($MinInstalledLiveRecordingDurationSec -gt 0))
if ($RequireInstalledLiveRecordingRustAudio -and -not $InstalledLiveRecordingAudioEngine) {
    $InstalledLiveRecordingAudioEngine = "rust-wasapi"
}
if ($RequireInstalledLiveRecordingRustAudio -and -not $InstalledLiveRecordingRustAudioCaptureMode) {
    $InstalledLiveRecordingRustAudioCaptureMode = "wasapi"
}
if ($RequireInstalledLiveRecordingRustAudio) {
    $InstalledLiveRecordingMicAlwaysOn = $true
}
if ($InstalledLiveRecordingRustAudioCaptureMode -and $InstalledLiveRecordingAudioEngine -ne "rust-wasapi") {
    throw "-InstalledLiveRecordingRustAudioCaptureMode requires -InstalledLiveRecordingAudioEngine rust-wasapi."
}
$effectiveInstalledLiveRecordingDurationSec = $InstalledLiveRecordingDurationSec
if ($effectiveInstalledLiveRecordingDurationSec -le 0) {
    if ($MinInstalledLiveRecordingDurationSec -gt 0) {
        $effectiveInstalledLiveRecordingDurationSec = [int][Math]::Ceiling($MinInstalledLiveRecordingDurationSec)
    } elseif ($RequireInstalledLiveRecordingRustAudio) {
        $effectiveInstalledLiveRecordingDurationSec = 600
    } else {
        $effectiveInstalledLiveRecordingDurationSec = 1800
    }
}

$matrixArgs = @(
    "scripts\validate_microphone_hardware_matrix.py",
    "--input-dir",
    $HardwareInputDir,
    "--output",
    $MatrixValidationOutput
)
$meetingMatrixArgs = @(
    "scripts\validate_meeting_release_matrix.py",
    "--input-dir", $MeetingReleaseMatrixDir,
    "--expected-app-version", $MeetingExpectedAppVersion,
    "--output", $MeetingMatrixValidationOutput
)
$effectiveRequireRustEndpointInventory = [bool]($RequireRustEndpointInventory -or $effectiveRequireRustAudioSidecarSmoke -or $effectiveRequireRustAudioAppPrewarmSmoke)
if ($effectiveRequireRustEndpointInventory) {
    $matrixArgs += "--require-rust-endpoint-inventory"
}
$effectiveRequireDeviceRefreshEvidence = [bool]($RequireDeviceRefreshEvidence -or $effectiveRequireRustAudioSidecarSmoke -or $effectiveRequireRustAudioAppPrewarmSmoke)
if ($effectiveRequireDeviceRefreshEvidence) {
    $matrixArgs += "--require-device-refresh-evidence"
}
if ($MicrophoneMatrixForceRefreshEachPoll -and $effectiveRequireDeviceRefreshEvidence) {
    throw "-MicrophoneMatrixForceRefreshEachPoll cannot be used when native device-refresh evidence is required. Rust-promotion evidence must prove native event-driven refreshes, not forced poll refreshes."
}
$microphoneMatrixRunnerArgs = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $RepoRoot "scripts\run_microphone_hardware_matrix.ps1"),
    "-RepoRoot",
    $RepoRoot,
    "-BaseUrl",
    $MicrophoneMatrixBaseUrl,
    "-OutputDir",
    $HardwareInputDir,
    "-WaitSec",
    ([string]$MicrophoneMatrixWaitSec),
    "-PollSec",
    ([string]$MicrophoneMatrixPollSec)
)
if ($MicrophoneMatrixToken) {
    $microphoneMatrixRunnerArgs += @("-Token", $MicrophoneMatrixToken)
}
if ($MicrophoneMatrixUsbLabel) {
    $microphoneMatrixRunnerArgs += @("-UsbLabel", $MicrophoneMatrixUsbLabel)
}
if ($MicrophoneMatrixDockLabel) {
    $microphoneMatrixRunnerArgs += @("-DockLabel", $MicrophoneMatrixDockLabel)
}
if ($MicrophoneMatrixBluetoothLabel) {
    $microphoneMatrixRunnerArgs += @("-BluetoothLabel", $MicrophoneMatrixBluetoothLabel)
}
if ($MicrophoneMatrixFavoriteLabel) {
    $microphoneMatrixRunnerArgs += @("-FavoriteLabel", $MicrophoneMatrixFavoriteLabel)
}
if ($MicrophoneMatrixAssumeCompleted) {
    $microphoneMatrixRunnerArgs += "-AssumeCompleted"
}
if ($MicrophoneMatrixForceRefreshEachPoll) {
    $microphoneMatrixRunnerArgs += "-ForceRefreshEachPoll"
}
if ($effectiveRequireRustEndpointInventory) {
    $microphoneMatrixRunnerArgs += "-RequireRustEndpointInventory"
}
if ($effectiveRequireDeviceRefreshEvidence) {
    $microphoneMatrixRunnerArgs += "-RequireDeviceRefreshEvidence"
}
$releaseBuildArgs = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $RepoRoot "scripts\build_windows.ps1"),
    "-RepoRoot",
    $RepoRoot,
    "-Bundles"
)
$releaseBuildArgs += $ReleaseBuildBundles
if ($ReleaseBuildReleaseBaseUrl) {
    $releaseBuildArgs += @("-ReleaseBaseUrl", $ReleaseBuildReleaseBaseUrl)
}
if ($ReleaseBuildEnableTauriUpdater) {
    $releaseBuildArgs += "-EnableTauriUpdater"
}
if ($ReleaseBuildUpdaterEndpoint) {
    $releaseBuildArgs += @("-UpdaterEndpoint", $ReleaseBuildUpdaterEndpoint)
}
if ($ReleaseBuildUpdaterPublicKey) {
    $releaseBuildArgs += @("-UpdaterPublicKey", $ReleaseBuildUpdaterPublicKey)
}
if ($ReleaseBuildRequireUpdaterSignatures) {
    $releaseBuildArgs += "-RequireUpdaterSignatures"
}
if ($ReleaseBuildRequireAuthenticodeSignature) {
    $releaseBuildArgs += "-RequireAuthenticodeSignature"
}
if ($ExpectedAuthenticodePublisher) {
    $releaseBuildArgs += @("-ExpectedAuthenticodePublisher", $ExpectedAuthenticodePublisher)
}
if ($RequireAuthenticodeTimestamp) {
    $releaseBuildArgs += "-RequireAuthenticodeTimestamp"
}
if ($ReleaseBuildUseProfileBFfmpeg) {
    $releaseBuildArgs += "-UseProfileBFfmpeg"
}
if ($ReleaseBuildValidateSlimMediaTools) {
    $releaseBuildArgs += "-ValidateSlimMediaTools"
}
if ($ReleaseBuildReuseSidecarIfUnchanged) {
    $releaseBuildArgs += "-ReuseSidecarIfUnchanged"
}
if ($ReleaseBuildRunMediaPreparationSmoke) {
    $releaseBuildArgs += "-RunMediaPreparationSmoke"
}
if ($ReleaseBuildRunRuntimeDependencyFootprint) {
    $releaseBuildArgs += "-RunRuntimeDependencyFootprint"
}
$updaterArgs = @(
    "scripts\verify_tauri_updater_publication.py",
    "--url",
    $UpdaterPublicationUrl,
    "--metadata",
    $UpdaterMetadata,
    "--attempts",
    ([string]$UpdaterPublicationAttempts),
    "--retry-delay-sec",
    ([string]$UpdaterPublicationRetryDelaySec),
    "--output",
    $UpdaterPublicationReport
)
$authenticodeArgs = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $RepoRoot "scripts\validate_windows_authenticode.ps1"),
    "-Path"
)
$authenticodeArgs += $AuthenticodePath
if ($ExpectedAuthenticodePublisher) {
    $authenticodeArgs += @("-ExpectedPublisher", $ExpectedAuthenticodePublisher)
}
if ($RequireAuthenticodeTimestamp) {
    $authenticodeArgs += "-RequireTimestamp"
}
$authenticodeArgs += @("-OutputPath", $AuthenticodeReport)

$mediaPreparationArgs = @(
    "scripts\smoke_media_preparation.py",
    "--output",
    $MediaPreparationReport,
    "--media-tools-dir",
    $MediaToolsDir,
    "--require-ffprobe"
)

$runtimeDependencyFootprintArgs = @(
    "scripts\analyze_backend_runtime_dependencies.py",
    "--sidecar-dir",
    $SidecarDir,
    "--output",
    $RuntimeDependencyFootprintReport
)

$rustAudioSidecarArgs = @(
    "scripts\smoke_rust_audio_sidecar.py",
    "--mode",
    "wasapi",
    "--duration-sec",
    ([string]$RustAudioSidecarDurationSec),
    "--selected-duration-sec",
    ([string]$RustAudioSidecarSelectedDurationSec),
    "--prebuffer-ms",
    ([string]$RustAudioSidecarPrebufferMs),
    "--output",
    $RustAudioSidecarReport
)
if ($RustAudioSidecarPrewarmBeforeCapture) {
    $rustAudioSidecarArgs += @("--prewarm-before-capture", "--prewarm-duration-sec", ([string]$RustAudioSidecarPrewarmDurationSec))
}
if ($RustAudioSidecarExe) {
    $rustAudioSidecarArgs += @("--sidecar-exe", $RustAudioSidecarExe)
}

$rustAudioPrewarmSidecarArgs = @(
    "scripts\smoke_rust_audio_prewarm_sidecar.py",
    "--mode",
    $RustAudioPrewarmSidecarMode,
    "--duration-sec",
    ([string]$RustAudioPrewarmSidecarDurationSec),
    "--prebuffer-ms",
    ([string]$RustAudioPrewarmSidecarPrebufferMs),
    "--output",
    $RustAudioPrewarmSidecarReport
)
if ($RustAudioSidecarExe) {
    $rustAudioPrewarmSidecarArgs += @("--sidecar-exe", $RustAudioSidecarExe)
}

$rustAudioAppPrewarmArgs = @(
    "scripts\smoke_rust_audio_app_prewarm.py",
    "--mode",
    $RustAudioAppPrewarmMode,
    "--duration-sec",
    ([string]$RustAudioAppPrewarmDurationSec),
    "--prewarm-duration-sec",
    ([string]$RustAudioAppPrewarmPrewarmDurationSec),
    "--capture-cycles",
    ([string]$RustAudioAppPrewarmCaptureCycles),
    "--prebuffer-ms",
    ([string]$RustAudioAppPrewarmPrebufferMs),
    "--output",
    $RustAudioAppPrewarmReport
)
if ($RustAudioAppPrewarmHonorFavoriteMic) {
    $rustAudioAppPrewarmArgs += "--honor-favorite-mic"
}
if ($RustAudioSidecarExe) {
    $rustAudioAppPrewarmArgs += @("--sidecar-exe", $RustAudioSidecarExe)
}

$installedLiveRecordingArgs = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $RepoRoot "scripts\smoke_windows_installer.ps1"),
    "-OutputPath",
    $InstalledLiveRecordingSmokeReport,
    "-LiveRecordingDurationSec",
    ([string]$effectiveInstalledLiveRecordingDurationSec),
    "-LiveRecordingProbeIntervalSec",
    ([string]$InstalledLiveRecordingProbeIntervalSec),
    "-LiveRecordingStartTimeoutSec",
    ([string]$InstalledLiveRecordingStartTimeoutSec),
    "-LiveRecordingStopTimeoutSec",
    ([string]$InstalledLiveRecordingStopTimeoutSec)
)
if ($InstalledLiveRecordingInstallerPath) {
    $installedLiveRecordingArgs += @("-InstallerPath", $InstalledLiveRecordingInstallerPath)
}
if ($InstalledLiveRecordingDisableTextInjection) {
    $installedLiveRecordingArgs += "-DisableLiveTextInjection"
}
if ($InstalledLiveRecordingEnvFile) {
    $installedLiveRecordingArgs += @("-LiveRecordingEnvFile", $InstalledLiveRecordingEnvFile)
}
if ($InstalledLiveRecordingDefaultStt) {
    $installedLiveRecordingArgs += @("-LiveRecordingDefaultStt", $InstalledLiveRecordingDefaultStt)
}
if ($InstalledLiveRecordingSonioxMode) {
    $installedLiveRecordingArgs += @("-LiveRecordingSonioxMode", $InstalledLiveRecordingSonioxMode)
}
if ($InstalledLiveRecordingAudioEngine) {
    $installedLiveRecordingArgs += @("-LiveRecordingAudioEngine", $InstalledLiveRecordingAudioEngine)
}
if ($InstalledLiveRecordingRustAudioCaptureMode) {
    $installedLiveRecordingArgs += @("-LiveRecordingRustAudioCaptureMode", $InstalledLiveRecordingRustAudioCaptureMode)
}
if ($InstalledLiveRecordingMicAlwaysOn) {
    $installedLiveRecordingArgs += "-LiveRecordingMicAlwaysOn"
}

$tauriTextInjectionSmokeArgs = @(
    "scripts\smoke_text_injection_target.py",
    "--method",
    "tauri",
    "--text",
    $TauriTextInjectionSmokeText,
    "--output",
    $TauriTextInjectionSmokeReport,
    "--target-title",
    $TauriTextInjectionTargetTitle,
    "--settle-sec",
    ([string]$TauriTextInjectionSettleSec),
    "--timeout-sec",
    ([string]$TauriTextInjectionTimeoutSec),
    "--paste-restore-delay-ms",
    ([string]$TauriTextInjectionPasteRestoreDelayMs)
)
if ($TauriTextInjectionSkipTargetClick) {
    $tauriTextInjectionSmokeArgs += "--skip-target-click"
}

$tauriTextInjectionMatrixArgs = @(
    "scripts\build_tauri_text_injection_matrix.py",
    "--input-dir",
    $TauriTextInjectionMatrixInputDir,
    "--output",
    $TauriTextInjectionMatrixReport
)
foreach ($scenario in $TauriTextInjectionMatrixScenario) {
    $tauriTextInjectionMatrixArgs += @("--scenario", $scenario)
}
foreach ($unsupported in $TauriTextInjectionMatrixUnsupportedOptional) {
    $tauriTextInjectionMatrixArgs += @("--unsupported-optional", $unsupported)
}

$recordingHotPathComparisonArgs = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $RepoRoot "scripts\run_recording_hot_path_comparison.ps1"),
    "-RepoRoot",
    $RepoRoot,
    "-OutputDir",
    $HardwareInputDir,
    "-ComparisonOutput",
    $RecordingHotPathComparisonReport,
    "-RecordingHotPathIterations",
    ([string]$RecordingHotPathIterations),
    "-RecordingHotPathSeconds",
    ([string]$RecordingHotPathSeconds),
    "-RecordingHotPathTimeoutSec",
    ([string]$RecordingHotPathTimeoutSec),
    "-RecordingHotPathSpeechPrompt",
    $RecordingHotPathSpeechPrompt,
    "-RecordingHotPathSpeechDelaySec",
    ([string]$RecordingHotPathSpeechDelaySec),
    "-MaxAudioOwnedP95RegressionMs",
    ([string]$RecordingHotPathMaxAudioOwnedP95RegressionMs),
    "-RustCaptureMode",
    $RecordingHotPathRustCaptureMode,
    "-RustAlwaysOnMic"
)
if ($RecordingHotPathExePath) {
    $recordingHotPathComparisonArgs += @("-ExePath", $RecordingHotPathExePath)
}
if ($RecordingHotPathPythonPath) {
    $recordingHotPathComparisonArgs += @("-PythonPath", $RecordingHotPathPythonPath)
}
if ($RecordingHotPathBackendExePath) {
    $recordingHotPathComparisonArgs += @("-BackendExePath", $RecordingHotPathBackendExePath)
}
if ($RecordingHotPathLegacyDataDir) {
    $recordingHotPathComparisonArgs += @("-LegacyDataDir", $RecordingHotPathLegacyDataDir)
}
if ($RecordingHotPathEnvFile) {
    $recordingHotPathComparisonArgs += @("-RecordingHotPathEnvFile", $RecordingHotPathEnvFile)
}
if ($RecordingHotPathDefaultStt) {
    $recordingHotPathComparisonArgs += @("-RecordingHotPathDefaultStt", $RecordingHotPathDefaultStt)
}
if ($RecordingHotPathSonioxMode) {
    $recordingHotPathComparisonArgs += @("-RecordingHotPathSonioxMode", $RecordingHotPathSonioxMode)
}
if ($RecordingHotPathHidden) {
    $recordingHotPathComparisonArgs += "-Hidden"
}
if ($RecordingHotPathSkipUiVisibleWait) {
    $recordingHotPathComparisonArgs += "-SkipUiVisibleWait"
}
if ($RecordingHotPathDisableDevFallback) {
    $recordingHotPathComparisonArgs += "-DisableDevFallback"
}
if ($RecordingHotPathKeepArtifacts) {
    $recordingHotPathComparisonArgs += "-KeepArtifacts"
}

$readinessArgs = @(
    "scripts\validate_hybrid_release_readiness.py",
    "--hardware-input-dir",
    $HardwareInputDir,
    "--updater-metadata",
    $UpdaterMetadata,
    "--updater-artifact-dir",
    $UpdaterArtifactDir,
    "--sha256sums",
    $Sha256Sums,
    "--media-preparation-report",
    $MediaPreparationReport,
    "--runtime-dependency-footprint-report",
    $RuntimeDependencyFootprintReport,
    "--updater-publication-report",
    $UpdaterPublicationReport,
    "--authenticode-report",
    $AuthenticodeReport,
    "--output",
    $OutputPath
)
if ($RequireMeetingReleaseMatrix -or (Test-Path -LiteralPath $MeetingReleaseMatrixDir -PathType Container)) {
    $readinessArgs += @(
        "--meeting-release-matrix-dir", $MeetingReleaseMatrixDir,
        "--expected-app-version", $MeetingExpectedAppVersion
    )
}
if ($RequireMeetingReleaseMatrix) {
    $readinessArgs += "--require-meeting-release-matrix"
}
if ($effectiveRequireRustAudioSidecarSmoke -or $RunRustAudioSidecarSmoke -or $UseExistingRustAudioSidecarReport) {
    $readinessArgs += @("--rust-audio-sidecar-report", $RustAudioSidecarReport)
}
if ($RequireRustAudioSidecarSmoke) {
    $readinessArgs += @("--require-rust-audio-sidecar-smoke", "--min-rust-audio-duration-sec", ([string]$RustAudioSidecarDurationSec))
}
if ($RustAudioSidecarPrewarmBeforeCapture) {
    $readinessArgs += "--require-rust-audio-sidecar-prewarm-adoption"
}
if ($RequireRustAudioPrewarmSidecarSmoke -or $RunRustAudioPrewarmSidecarSmoke -or $UseExistingRustAudioPrewarmSidecarReport) {
    $readinessArgs += @("--rust-audio-prewarm-sidecar-report", $RustAudioPrewarmSidecarReport)
}
if ($RequireRustAudioPrewarmSidecarSmoke) {
    $readinessArgs += "--require-rust-audio-prewarm-sidecar-smoke"
}
if ($effectiveRequireRustAudioAppPrewarmSmoke -or $RunRustAudioAppPrewarmSmoke -or $UseExistingRustAudioAppPrewarmReport) {
    $readinessArgs += @("--rust-audio-app-prewarm-report", $RustAudioAppPrewarmReport)
}
if ($RequireRustAudioAppPrewarmSmoke) {
    $readinessArgs += "--require-rust-audio-app-prewarm-smoke"
}
if ($MinRustAudioAppPrewarmDurationSec -gt 0) {
    $readinessArgs += @("--min-rust-audio-app-prewarm-duration-sec", ([string]$MinRustAudioAppPrewarmDurationSec))
}
if ($MinRustAudioAppPrewarmPrewarmDurationSec -gt 0) {
    $readinessArgs += @("--min-rust-audio-app-prewarm-prewarm-duration-sec", ([string]$MinRustAudioAppPrewarmPrewarmDurationSec))
}
if ($MinRustAudioAppPrewarmCaptureCycles -gt 0) {
    $readinessArgs += @("--min-rust-audio-app-prewarm-cycles", ([string]$MinRustAudioAppPrewarmCaptureCycles))
}
if ($RequireInstalledLiveRecordingSmoke -or $RequireInstalledLiveRecordingRustAudio -or $MinInstalledLiveRecordingDurationSec -gt 0 -or (Test-Path -LiteralPath $InstalledLiveRecordingSmokeReport -PathType Leaf)) {
    $readinessArgs += @("--installed-live-recording-smoke-report", $InstalledLiveRecordingSmokeReport)
}
if ($RequireInstalledLiveRecordingSmoke) {
    $readinessArgs += "--require-installed-live-recording-smoke"
}
if ($RequireInstalledLiveRecordingRustAudio) {
    $readinessArgs += "--require-installed-live-recording-rust-audio"
}
if ($MinInstalledLiveRecordingDurationSec -gt 0) {
    $readinessArgs += @("--min-installed-live-recording-duration-sec", ([string]$MinInstalledLiveRecordingDurationSec))
}
if ($RequireTauriTextInjectionSmoke -or $RunTauriTextInjectionSmoke -or $UseExistingTauriTextInjectionSmokeReport -or (Test-Path -LiteralPath $TauriTextInjectionSmokeReport -PathType Leaf)) {
    $readinessArgs += @("--tauri-text-injection-smoke-report", $TauriTextInjectionSmokeReport)
}
if ($RequireTauriTextInjectionSmoke) {
    $readinessArgs += "--require-tauri-text-injection-smoke"
}
if ($RequireTauriTextInjectionMatrix -or $RunTauriTextInjectionMatrixBuilder -or $UseExistingTauriTextInjectionMatrixReport -or (Test-Path -LiteralPath $TauriTextInjectionMatrixReport -PathType Leaf)) {
    $readinessArgs += @("--tauri-text-injection-matrix-report", $TauriTextInjectionMatrixReport)
}
if ($RequireTauriTextInjectionMatrix) {
    $readinessArgs += "--require-tauri-text-injection-matrix"
}
if ($RequireRecordingHotPathComparison -or (Test-Path -LiteralPath $RecordingHotPathComparisonReport -PathType Leaf)) {
    $readinessArgs += @("--recording-hot-path-comparison-report", $RecordingHotPathComparisonReport)
}
if ($RequireRecordingHotPathComparison) {
    $readinessArgs += "--require-recording-hot-path-comparison"
}
if ($effectiveRequireRustEndpointInventory) {
    $readinessArgs += "--require-rust-endpoint-inventory"
}
if ($effectiveRequireDeviceRefreshEvidence) {
    $readinessArgs += "--require-device-refresh-evidence"
}
if ($ExpectedAuthenticodePublisher) {
    $readinessArgs += @("--expected-authenticode-publisher", $ExpectedAuthenticodePublisher)
}
if ($RequireAuthenticodeTimestamp) {
    $readinessArgs += "--require-authenticode-timestamp"
}

$hardwareArtifacts = @(
    "microphone-hardware-usb-add.json",
    "microphone-hardware-usb-remove.json",
    "microphone-hardware-dock-disconnect.json",
    "microphone-hardware-dock-connect.json",
    "microphone-hardware-bluetooth-add.json",
    "microphone-hardware-bluetooth-remove.json",
    "microphone-hardware-default-mic-change.json",
    "microphone-hardware-favorite-fallback.json"
)
$requiredEvidence = @(
    [pscustomobject]@{
        name = "physicalMicrophoneMatrix"
        required = $true
        external = [bool](-not $RunMicrophoneHardwareMatrix)
        producer = $(if ($RunMicrophoneHardwareMatrix) { "scripts\run_microphone_hardware_matrix.ps1" } else { "external artifacts or scripts\run_microphone_hardware_matrix.ps1" })
        validator = "scripts\validate_microphone_hardware_matrix.py"
        inputDir = $HardwareInputDir
        expectedArtifacts = @($hardwareArtifacts | ForEach-Object { Join-Path $HardwareInputDir $_ })
        output = $MatrixValidationOutput
        waitSec = $MicrophoneMatrixWaitSec
        pollSec = $MicrophoneMatrixPollSec
        forceRefreshEachPoll = [bool]$MicrophoneMatrixForceRefreshEachPoll
        labelsConfigured = [pscustomobject]@{
            usb = [bool]$MicrophoneMatrixUsbLabel
            dock = [bool]$MicrophoneMatrixDockLabel
            bluetooth = [bool]$MicrophoneMatrixBluetoothLabel
            favorite = [bool]$MicrophoneMatrixFavoriteLabel
        }
        requireRustEndpointInventory = $effectiveRequireRustEndpointInventory
        requireDeviceRefreshEvidence = $effectiveRequireDeviceRefreshEvidence
        notes = "Requires physical USB, dock, Bluetooth, Windows default-device, and favorite-mic fallback actions on the target Windows machine. Rust audio promotion also requires Rust/WASAPI endpoint inventory evidence and native-event device-refresh evidence in each artifact without forced per-poll refreshes."
    },
    [pscustomobject]@{
        name = "signedTauriUpdaterMetadata"
        required = $true
        external = $true
        producer = $(if ($RunReleaseBuild) { "scripts\build_windows.ps1" } else { "scripts\build_windows.ps1 -EnableTauriUpdater with signing keys" })
        metadata = $UpdaterMetadata
        artifactDir = $UpdaterArtifactDir
        sha256Sums = $Sha256Sums
        releaseBuild = [pscustomobject]@{
            run = [bool]$RunReleaseBuild
            enableTauriUpdater = [bool]$ReleaseBuildEnableTauriUpdater
            requireUpdaterSignatures = [bool]$ReleaseBuildRequireUpdaterSignatures
            releaseBaseUrlConfigured = [bool]$ReleaseBuildReleaseBaseUrl
        }
        notes = "latest.json must use absolute HTTPS release URLs and non-empty Tauri updater signatures."
    },
    [pscustomobject]@{
        name = "mediaPreparationSmoke"
        required = $true
        external = $false
        producer = $(if ($UseExistingMediaPreparationReport) { "existing report" } else { "scripts\smoke_media_preparation.py" })
        report = $MediaPreparationReport
        mediaToolsDir = $MediaToolsDir
        notes = "Validates bundled ffmpeg/ffprobe through Scriber file-upload compression, video extraction, YouTube normalization, Azure-MAI preparation, and ffprobe duration probing."
    },
    [pscustomobject]@{
        name = "runtimeDependencyFootprint"
        required = $true
        external = $false
        producer = $(if ($UseExistingRuntimeDependencyFootprintReport) { "existing report" } else { "scripts\analyze_backend_runtime_dependencies.py" })
        report = $RuntimeDependencyFootprintReport
        sidecarDir = $SidecarDir
        notes = "Validates that the frozen backend sidecar keeps SciPy absent, contains required ONNXRuntime/Silero-VAD runtime paths, and records the tracked dependency footprint."
    },
    [pscustomobject]@{
        name = "rustAudioSidecarSmoke"
        required = $effectiveRequireRustAudioSidecarSmoke
        external = $false
        producer = $(if ($UseExistingRustAudioSidecarReport) { "existing report" } elseif ($RunRustAudioSidecarSmoke) { "scripts\smoke_rust_audio_sidecar.py" } elseif ($effectiveRequireRustAudioSidecarSmoke) { "required external report" } else { "not requested" })
        report = $RustAudioSidecarReport
        durationSec = $RustAudioSidecarDurationSec
        selectedDurationSec = $RustAudioSidecarSelectedDurationSec
        prebufferMs = $RustAudioSidecarPrebufferMs
        prewarmBeforeCapture = [bool]$RustAudioSidecarPrewarmBeforeCapture
        requirePrewarmAdoption = [bool]$RustAudioSidecarPrewarmBeforeCapture
        prewarmDurationSec = $RustAudioSidecarPrewarmDurationSec
        notes = "Optional for standard releases. Required when evaluating Rust audio promotion; validates default WASAPI capture, selected native endpoint hash capture, requested prebuffer delivery, optional prewarm adoption, frame-pipe metrics, and stop health."
    },
    [pscustomobject]@{
        name = "rustAudioPrewarmSidecarSmoke"
        required = [bool]$RequireRustAudioPrewarmSidecarSmoke
        external = $false
        producer = $(if ($UseExistingRustAudioPrewarmSidecarReport) { "existing report" } elseif ($RunRustAudioPrewarmSidecarSmoke) { "scripts\smoke_rust_audio_prewarm_sidecar.py" } elseif ($RequireRustAudioPrewarmSidecarSmoke) { "required external report" } else { "not requested" })
        report = $RustAudioPrewarmSidecarReport
        mode = $RustAudioPrewarmSidecarMode
        durationSec = $RustAudioPrewarmSidecarDurationSec
        prebufferMs = $RustAudioPrewarmSidecarPrebufferMs
        notes = "Optional lifecycle evidence only. Synthetic mode validates plumbing; WASAPI mode starts a real passive idle stream. This still is not buffered audio adoption into active capture."
    },
    [pscustomobject]@{
        name = "rustAudioAppPrewarmSmoke"
        required = $effectiveRequireRustAudioAppPrewarmSmoke
        external = $false
        producer = $(if ($UseExistingRustAudioAppPrewarmReport) { "existing report" } elseif ($RunRustAudioAppPrewarmSmoke) { "scripts\smoke_rust_audio_app_prewarm.py" } elseif ($effectiveRequireRustAudioAppPrewarmSmoke) { "required external report" } else { "not requested" })
        report = $RustAudioAppPrewarmReport
        mode = $RustAudioAppPrewarmMode
        durationSec = $RustAudioAppPrewarmDurationSec
        prewarmDurationSec = $RustAudioAppPrewarmPrewarmDurationSec
        captureCycles = $RustAudioAppPrewarmCaptureCycles
        prebufferMs = $RustAudioAppPrewarmPrebufferMs
        minDurationSec = $MinRustAudioAppPrewarmDurationSec
        minPrewarmDurationSec = $MinRustAudioAppPrewarmPrewarmDurationSec
        minCaptureCycles = $MinRustAudioAppPrewarmCaptureCycles
        honorFavoriteMic = [bool]$RustAudioAppPrewarmHonorFavoriteMic
        notes = "Optional Rust promotion evidence. WASAPI mode validates the app-level RustAudioPrewarmManager to RustPrototypeFrameSource handoff, adopted prebuffer frames before live frames, and idle-prewarm resume after capture. Rust promotion requires repeated stop/resume capture cycles. Default release evidence should keep honorFavoriteMic=false."
    },
    [pscustomobject]@{
        name = "recordingHotPathPythonRustComparison"
        required = [bool]$RequireRecordingHotPathComparison
        external = [bool](-not $RunRecordingHotPathComparison)
        producer = $(if ($UseExistingRecordingHotPathComparisonReport) { "existing report" } elseif ($RunRecordingHotPathComparison) { "scripts\run_recording_hot_path_comparison.ps1 -RustAlwaysOnMic" } elseif ($RequireRecordingHotPathComparison) { "required external report: scripts\run_recording_hot_path_comparison.ps1 -RustAlwaysOnMic, or scripts\validate_recording_hot_path_comparison.py over existing provider-backed Python and Rust reports with Always-On-Mic evidence" } else { "not requested" })
        report = $RecordingHotPathComparisonReport
        rustAlwaysOnMicRequired = $true
        rustPrewarmAdoptionRequired = $true
        iterations = $RecordingHotPathIterations
        recordSeconds = $RecordingHotPathSeconds
        timeoutSec = $RecordingHotPathTimeoutSec
        rustCaptureMode = $RecordingHotPathRustCaptureMode
        envFile = $(if ($RecordingHotPathEnvFile) { $RecordingHotPathEnvFile } else { "" })
        defaultStt = $RecordingHotPathDefaultStt
        sonioxMode = $RecordingHotPathSonioxMode
        notes = "Required for Rust audio promotion. Compares provider-backed Python and rust-wasapi recording hot-path reports, rejects validate-only artifacts, requires passing inputReportRedaction, sameRecordingConfig, rustAlwaysOnMic, rustMidSessionClean, rustFramePipeFlow, rustNoDroppedFrames, rustActiveCaptureStable, and rustPrewarmAdoption checks, requires at least three samples per engine, requires provider transcript evidence with the same STT provider in both reports, requires active rust-frame-pipe capture with positive callback/frame/audio-frame counters, zero dropped frames, and adopted Rust prewarm evidence in the Rust report, rejects open Rust fallback-circuit, mid-session frame-pipe failure, and active-capture watchdog restart evidence, and rejects clear P95 regressions in local audio-owned hot-path segments."
    },
    [pscustomobject]@{
        name = "installedLiveRecordingSmoke"
        required = $effectiveRequireInstalledLiveRecordingSmoke
        external = [bool](-not $RunInstalledLiveRecordingSmoke)
        producer = $(if ($UseExistingInstalledLiveRecordingSmokeReport) { "existing report" } elseif ($RunInstalledLiveRecordingSmoke) { "scripts\smoke_windows_installer.ps1" } elseif ($RequireInstalledLiveRecordingRustAudio) { "scripts\build_windows.ps1 -RunInstallerLiveRecordingSmoke -InstallerLiveRecordingAudioEngine rust-wasapi -InstallerLiveRecordingRustAudioCaptureMode wasapi -InstallerLiveRecordingMicAlwaysOn, scripts\smoke_windows_installer.ps1 -LiveRecordingDurationSec -LiveRecordingAudioEngine rust-wasapi -LiveRecordingRustAudioCaptureMode wasapi -LiveRecordingMicAlwaysOn, or scripts\smoke_tauri_desktop.ps1 -LiveRecordingDurationSec -LiveRecordingAudioEngine rust-wasapi -LiveRecordingRustAudioCaptureMode wasapi -LiveRecordingMicAlwaysOn over an installed app" } elseif ($effectiveRequireInstalledLiveRecordingSmoke) { "required external report" } else { "not requested" })
        report = $InstalledLiveRecordingSmokeReport
        minDurationSec = $MinInstalledLiveRecordingDurationSec
        durationSec = $effectiveInstalledLiveRecordingDurationSec
        probeIntervalSec = $InstalledLiveRecordingProbeIntervalSec
        installerPath = $InstalledLiveRecordingInstallerPath
        envFile = $(if ($InstalledLiveRecordingEnvFile) { $InstalledLiveRecordingEnvFile } else { "" })
        defaultStt = $InstalledLiveRecordingDefaultStt
        sonioxMode = $InstalledLiveRecordingSonioxMode
        audioEngine = $InstalledLiveRecordingAudioEngine
        rustAudioCaptureMode = $InstalledLiveRecordingRustAudioCaptureMode
        requireRustAudio = [bool]$RequireInstalledLiveRecordingRustAudio
        rustPrewarmAdoptionRequired = [bool]$RequireInstalledLiveRecordingRustAudio
        micAlwaysOn = [bool]$InstalledLiveRecordingMicAlwaysOn
        disableTextInjection = [bool]$InstalledLiveRecordingDisableTextInjection
        notes = "Required for Rust/WASAPI live-mic release evidence. Validates installed app live recording start/stop state, non-recording sample leakage, stability samples, cleanup, and, when requireRustAudio=true, sampled rust-wasapi/rust-frame-pipe capture with adopted Rust prewarm evidence and a closed fallback circuit; provider-backed transcription quality remains covered by recordingHotPathPythonRustComparison."
    },
    [pscustomobject]@{
        name = "tauriTextInjectionSmoke"
        required = [bool]$RequireTauriTextInjectionSmoke
        external = [bool](-not $RunTauriTextInjectionSmoke)
        producer = $(if ($UseExistingTauriTextInjectionSmokeReport) { "existing report" } elseif ($RunTauriTextInjectionSmoke) { "scripts\smoke_text_injection_target.py --method tauri" } elseif ($RequireTauriTextInjectionSmoke) { "required external report: scripts\smoke_text_injection_target.py --method tauri in a Tauri-managed backend environment with shell IPC variables" } else { "not requested" })
        report = $TauriTextInjectionSmokeReport
        textChars = $TauriTextInjectionSmokeText.Length
        targetTitle = $TauriTextInjectionTargetTitle
        timeoutSec = $TauriTextInjectionTimeoutSec
        skipTargetClick = [bool]$TauriTextInjectionSkipTargetClick
        notes = "Required before considering Rust/Tauri text injection as a default path. Validates safe target-window injection, strict SCRIBER_INJECT_METHOD=tauri, Shell IPC injectText success, clipboard_set and paste markers, structured restore evidence, and hashed/redacted foreground diagnostics. Manual target-app matrix evidence is still required before default promotion."
    },
    [pscustomobject]@{
        name = "tauriTextInjectionMatrix"
        required = [bool]$RequireTauriTextInjectionMatrix
        external = [bool](-not $RunTauriTextInjectionMatrixBuilder)
        producer = $(if ($UseExistingTauriTextInjectionMatrixReport) { "existing report" } elseif ($RunTauriTextInjectionMatrixBuilder) { "scripts\build_tauri_text_injection_matrix.py" } elseif ($RequireTauriTextInjectionMatrix) { "required external report: scripts\build_tauri_text_injection_matrix.py over real installed target-app matrix reports from scripts\smoke_text_injection_target.py --method tauri and manual target-app runs" } else { "not requested" })
        report = $TauriTextInjectionMatrixReport
        inputDir = $TauriTextInjectionMatrixInputDir
        scenarioOverrides = $TauriTextInjectionMatrixScenario
        unsupportedOptional = $TauriTextInjectionMatrixUnsupportedOptional
        inputReportsExternal = $true
        notes = "Required before changing text-injection defaults. The matrix must cover Notepad, Word, Outlook, browser input, browser contenteditable, Electron, elevated-target, elevated-Scriber, clipboard text/non-text/locked, restore user-copy, and same-text restore scenarios; Remote Desktop is optional when unavailable. Every scenario must prove preDelayMode=auto, structured restore evidence, and hashed/redacted foreground diagnostics, and Word/Outlook must show a positive applied pre-delay from Rust foreground policy."
    },
    [pscustomobject]@{
        name = "publishedUpdaterManifest"
        required = $true
        external = $true
        producer = $(if ($UseExistingUpdaterPublicationReport) { "existing report" } else { "scripts\verify_tauri_updater_publication.py" })
        url = $UpdaterPublicationUrl
        report = $UpdaterPublicationReport
        notes = "The signed latest.json must be publicly reachable, keep its final redirect URL on HTTPS, and match the local release metadata SHA256."
    },
    [pscustomobject]@{
        name = "authenticodeSignatures"
        required = $true
        external = $true
        producer = $(if ($UseExistingAuthenticodeReport) { "existing report" } else { "scripts\validate_windows_authenticode.ps1" })
        inputPaths = $AuthenticodePath
        report = $AuthenticodeReport
        releaseBuildReport = $releaseBuildGeneratedAuthenticodeReport
        generatedByReleaseBuild = [bool]($RunReleaseBuild -and $ReleaseBuildRequireAuthenticodeSignature)
        expectedPublisher = $ExpectedAuthenticodePublisher
        requireTimestamp = [bool]$RequireAuthenticodeTimestamp
        notes = "The Authenticode report must include the release artifact names from latest.json, not only an unrelated signed executable."
    },
    [pscustomobject]@{
        name = "meetingReleaseMatrix"
        required = [bool]$RequireMeetingReleaseMatrix
        external = $true
        producer = "scripts\run_meeting_release_matrix.ps1 over real installed Teams, Zoom, Meet, hardware, Outlook, failure, privacy, and soak evidence"
        validator = "scripts\validate_meeting_release_matrix.py"
        inputDir = $MeetingReleaseMatrixDir
        output = $MeetingMatrixValidationOutput
        expectedAppVersion = $MeetingExpectedAppVersion
        notes = "Full release evidence must be bound to one signed installer SHA-256 and include Authenticode, Tauri updater signature, legal/privacy review, 60-minute recording, two-hour soak, and all real-world Meeting scenarios. Drafts never count."
    },
    [pscustomobject]@{
        name = "hybridReleaseReadinessAggregate"
        required = $true
        external = $false
        producer = "scripts\validate_hybrid_release_readiness.py"
        output = $OutputPath
        notes = "Final aggregate verdict remains red until every required external evidence item above is present and valid."
    }
)

$plan = [pscustomobject]@{
    ok = $true
    planOnly = [bool]$PlanOnly
    runReleaseBuild = [bool]$RunReleaseBuild
    releaseBuildCommand = $(if ($RunReleaseBuild) { "powershell " + (Convert-ToDisplayCommand -CommandArgs $releaseBuildArgs) } else { "not requested" })
    releaseBuildGeneratedAuthenticodeReport = $releaseBuildGeneratedAuthenticodeReport
    releaseBuildEnableTauriUpdater = [bool]$ReleaseBuildEnableTauriUpdater
    releaseBuildRequireUpdaterSignatures = [bool]$ReleaseBuildRequireUpdaterSignatures
    releaseBuildRequireAuthenticodeSignature = [bool]$ReleaseBuildRequireAuthenticodeSignature
    hardwareInputDir = $HardwareInputDir
    matrixValidationOutput = $MatrixValidationOutput
    meetingReleaseMatrixDir = $MeetingReleaseMatrixDir
    meetingMatrixValidationOutput = $MeetingMatrixValidationOutput
    meetingExpectedAppVersion = $MeetingExpectedAppVersion
    requireMeetingReleaseMatrix = [bool]$RequireMeetingReleaseMatrix
    mediaPreparationReport = $MediaPreparationReport
    mediaToolsDir = $MediaToolsDir
    runtimeDependencyFootprintReport = $RuntimeDependencyFootprintReport
    sidecarDir = $SidecarDir
    rustAudioSidecarReport = $RustAudioSidecarReport
    rustAudioSidecarDurationSec = $RustAudioSidecarDurationSec
    rustAudioSidecarSelectedDurationSec = $RustAudioSidecarSelectedDurationSec
    rustAudioSidecarPrebufferMs = $RustAudioSidecarPrebufferMs
    rustAudioSidecarPrewarmBeforeCapture = [bool]$RustAudioSidecarPrewarmBeforeCapture
    rustAudioSidecarPrewarmDurationSec = $RustAudioSidecarPrewarmDurationSec
    rustAudioPrewarmSidecarReport = $RustAudioPrewarmSidecarReport
    rustAudioPrewarmSidecarMode = $RustAudioPrewarmSidecarMode
    rustAudioPrewarmSidecarDurationSec = $RustAudioPrewarmSidecarDurationSec
    rustAudioPrewarmSidecarPrebufferMs = $RustAudioPrewarmSidecarPrebufferMs
    rustAudioAppPrewarmReport = $RustAudioAppPrewarmReport
    rustAudioAppPrewarmMode = $RustAudioAppPrewarmMode
    rustAudioAppPrewarmDurationSec = $RustAudioAppPrewarmDurationSec
    rustAudioAppPrewarmPrewarmDurationSec = $RustAudioAppPrewarmPrewarmDurationSec
    rustAudioAppPrewarmPrebufferMs = $RustAudioAppPrewarmPrebufferMs
    rustAudioAppPrewarmCaptureCycles = $RustAudioAppPrewarmCaptureCycles
    minRustAudioAppPrewarmDurationSec = $MinRustAudioAppPrewarmDurationSec
    minRustAudioAppPrewarmPrewarmDurationSec = $MinRustAudioAppPrewarmPrewarmDurationSec
    minRustAudioAppPrewarmCaptureCycles = $MinRustAudioAppPrewarmCaptureCycles
    rustAudioAppPrewarmHonorFavoriteMic = [bool]$RustAudioAppPrewarmHonorFavoriteMic
    installedLiveRecordingSmokeReport = $InstalledLiveRecordingSmokeReport
    installedLiveRecordingInstallerPath = $InstalledLiveRecordingInstallerPath
    installedLiveRecordingDurationSec = $effectiveInstalledLiveRecordingDurationSec
    installedLiveRecordingProbeIntervalSec = $InstalledLiveRecordingProbeIntervalSec
    installedLiveRecordingEnvFile = $InstalledLiveRecordingEnvFile
    installedLiveRecordingDefaultStt = $InstalledLiveRecordingDefaultStt
    installedLiveRecordingSonioxMode = $InstalledLiveRecordingSonioxMode
    installedLiveRecordingAudioEngine = $InstalledLiveRecordingAudioEngine
    installedLiveRecordingRustAudioCaptureMode = $InstalledLiveRecordingRustAudioCaptureMode
    installedLiveRecordingMicAlwaysOn = [bool]$InstalledLiveRecordingMicAlwaysOn
    minInstalledLiveRecordingDurationSec = $MinInstalledLiveRecordingDurationSec
    tauriTextInjectionSmokeReport = $TauriTextInjectionSmokeReport
    tauriTextInjectionMatrixReport = $TauriTextInjectionMatrixReport
    tauriTextInjectionMatrixInputDir = $TauriTextInjectionMatrixInputDir
    recordingHotPathComparisonReport = $RecordingHotPathComparisonReport
    recordingHotPathEnvFile = $RecordingHotPathEnvFile
    recordingHotPathDefaultStt = $RecordingHotPathDefaultStt
    recordingHotPathSonioxMode = $RecordingHotPathSonioxMode
    updaterPublicationReport = $UpdaterPublicationReport
    authenticodeReport = $AuthenticodeReport
    outputPath = $OutputPath
    runMicrophoneHardwareMatrix = [bool]$RunMicrophoneHardwareMatrix
    microphoneMatrixBaseUrl = $MicrophoneMatrixBaseUrl
    microphoneMatrixWaitSec = $MicrophoneMatrixWaitSec
    microphoneMatrixPollSec = $MicrophoneMatrixPollSec
    microphoneMatrixForceRefreshEachPoll = [bool]$MicrophoneMatrixForceRefreshEachPoll
    useExistingAuthenticodeReport = [bool]$UseExistingAuthenticodeReport
    useExistingMediaPreparationReport = [bool]$UseExistingMediaPreparationReport
    useExistingRuntimeDependencyFootprintReport = [bool]$UseExistingRuntimeDependencyFootprintReport
    useExistingRustAudioSidecarReport = [bool]$UseExistingRustAudioSidecarReport
    useExistingRustAudioPrewarmSidecarReport = [bool]$UseExistingRustAudioPrewarmSidecarReport
    useExistingRustAudioAppPrewarmReport = [bool]$UseExistingRustAudioAppPrewarmReport
    useExistingRecordingHotPathComparisonReport = [bool]$UseExistingRecordingHotPathComparisonReport
    useExistingInstalledLiveRecordingSmokeReport = [bool]$UseExistingInstalledLiveRecordingSmokeReport
    useExistingTauriTextInjectionSmokeReport = [bool]$UseExistingTauriTextInjectionSmokeReport
    useExistingTauriTextInjectionMatrixReport = [bool]$UseExistingTauriTextInjectionMatrixReport
    runRustAudioSidecarSmoke = [bool]$RunRustAudioSidecarSmoke
    runRustAudioPrewarmSidecarSmoke = [bool]$RunRustAudioPrewarmSidecarSmoke
    runRustAudioAppPrewarmSmoke = [bool]$RunRustAudioAppPrewarmSmoke
    runRecordingHotPathComparison = [bool]$RunRecordingHotPathComparison
    runInstalledLiveRecordingSmoke = [bool]$RunInstalledLiveRecordingSmoke
    runTauriTextInjectionSmoke = [bool]$RunTauriTextInjectionSmoke
    runTauriTextInjectionMatrixBuilder = [bool]$RunTauriTextInjectionMatrixBuilder
    requireRustAudioSidecarSmoke = [bool]$RequireRustAudioSidecarSmoke
    requireRustAudioPrewarmSidecarSmoke = [bool]$RequireRustAudioPrewarmSidecarSmoke
    requireRustAudioAppPrewarmSmoke = [bool]$RequireRustAudioAppPrewarmSmoke
    requireRustAudioPromotionReadiness = [bool]$RequireRustAudioPromotionReadiness
    requireInstalledLiveRecordingSmoke = [bool]$RequireInstalledLiveRecordingSmoke
    requireInstalledLiveRecordingRustAudio = [bool]$RequireInstalledLiveRecordingRustAudio
    requireTauriTextInjectionSmoke = [bool]$RequireTauriTextInjectionSmoke
    requireTauriTextInjectionMatrix = [bool]$RequireTauriTextInjectionMatrix
    requireRecordingHotPathComparison = [bool]$RequireRecordingHotPathComparison
    requireRustEndpointInventory = $effectiveRequireRustEndpointInventory
    requireDeviceRefreshEvidence = $effectiveRequireDeviceRefreshEvidence
    useExistingUpdaterPublicationReport = [bool]$UseExistingUpdaterPublicationReport
    requiredEvidence = $requiredEvidence
    commands = @(
        [pscustomobject]@{
            name = "microphoneMatrixValidation"
            command = $(if ($RunMicrophoneHardwareMatrix) { "powershell " + (Convert-ToDisplayCommand -CommandArgs $microphoneMatrixRunnerArgs) + "; python " + (Convert-ToDisplayCommand -CommandArgs $matrixArgs) } else { "python " + (Convert-ToDisplayCommand -CommandArgs $matrixArgs) })
        },
        [pscustomobject]@{
            name = "meetingReleaseMatrixValidation"
            command = $(if ($RequireMeetingReleaseMatrix -or (Test-Path -LiteralPath $MeetingReleaseMatrixDir -PathType Container)) { "python " + (Convert-ToDisplayCommand -CommandArgs $meetingMatrixArgs) } else { "not requested" })
        },
        [pscustomobject]@{
            name = "updaterPublicationVerification"
            command = $(if ($UseExistingUpdaterPublicationReport) { "reuse $UpdaterPublicationReport" } else { "python " + (Convert-ToDisplayCommand -CommandArgs $updaterArgs) })
        },
        [pscustomobject]@{
            name = "mediaPreparationSmoke"
            command = $(if ($UseExistingMediaPreparationReport) { "reuse $MediaPreparationReport" } else { "python " + (Convert-ToDisplayCommand -CommandArgs $mediaPreparationArgs) })
        },
        [pscustomobject]@{
            name = "runtimeDependencyFootprint"
            command = $(if ($UseExistingRuntimeDependencyFootprintReport) { "reuse $RuntimeDependencyFootprintReport" } else { "python " + (Convert-ToDisplayCommand -CommandArgs $runtimeDependencyFootprintArgs) })
        },
        [pscustomobject]@{
            name = "rustAudioSidecarSmoke"
            command = $(if ($UseExistingRustAudioSidecarReport) { "reuse $RustAudioSidecarReport" } elseif ($RunRustAudioSidecarSmoke) { "python " + (Convert-ToDisplayCommand -CommandArgs $rustAudioSidecarArgs) } elseif ($effectiveRequireRustAudioSidecarSmoke) { "required external report: produce with scripts\smoke_rust_audio_sidecar.py or pass -RunRustAudioSidecarSmoke / -UseExistingRustAudioSidecarReport" } else { "not requested" })
        },
        [pscustomobject]@{
            name = "rustAudioPrewarmSidecarSmoke"
            command = $(if ($UseExistingRustAudioPrewarmSidecarReport) { "reuse $RustAudioPrewarmSidecarReport" } elseif ($RunRustAudioPrewarmSidecarSmoke) { "python " + (Convert-ToDisplayCommand -CommandArgs $rustAudioPrewarmSidecarArgs) } elseif ($RequireRustAudioPrewarmSidecarSmoke) { "required external report: produce with scripts\smoke_rust_audio_prewarm_sidecar.py or pass -RunRustAudioPrewarmSidecarSmoke / -UseExistingRustAudioPrewarmSidecarReport" } else { "not requested" })
        },
        [pscustomobject]@{
            name = "rustAudioAppPrewarmSmoke"
            command = $(if ($UseExistingRustAudioAppPrewarmReport) { "reuse $RustAudioAppPrewarmReport" } elseif ($RunRustAudioAppPrewarmSmoke) { "python " + (Convert-ToDisplayCommand -CommandArgs $rustAudioAppPrewarmArgs) } elseif ($effectiveRequireRustAudioAppPrewarmSmoke) { "required external report: produce with scripts\smoke_rust_audio_app_prewarm.py or pass -RunRustAudioAppPrewarmSmoke / -UseExistingRustAudioAppPrewarmReport" } else { "not requested" })
        },
        [pscustomobject]@{
            name = "recordingHotPathPythonRustComparison"
            command = $(if ($UseExistingRecordingHotPathComparisonReport) { "reuse $RecordingHotPathComparisonReport" } elseif ($RunRecordingHotPathComparison) { "powershell " + (Convert-ToDisplayCommand -CommandArgs $recordingHotPathComparisonArgs) } elseif (Test-Path -LiteralPath $RecordingHotPathComparisonReport -PathType Leaf) { "reuse $RecordingHotPathComparisonReport" } elseif ($RequireRecordingHotPathComparison) { "required external report: produce with scripts\run_recording_hot_path_comparison.ps1 -RustAlwaysOnMic or validate existing Python/Rust Always-On-Mic reports with scripts\validate_recording_hot_path_comparison.py" } else { "not requested" })
        },
        [pscustomobject]@{
            name = "installedLiveRecordingSmoke"
            command = $(if ($UseExistingInstalledLiveRecordingSmokeReport) { "reuse $InstalledLiveRecordingSmokeReport" } elseif ($RunInstalledLiveRecordingSmoke) { "powershell " + (Convert-ToDisplayCommand -CommandArgs $installedLiveRecordingArgs) } elseif (Test-Path -LiteralPath $InstalledLiveRecordingSmokeReport -PathType Leaf) { "reuse $InstalledLiveRecordingSmokeReport" } elseif ($RequireInstalledLiveRecordingRustAudio) { "required external report: produce with scripts\build_windows.ps1 -RunInstallerLiveRecordingSmoke -InstallerLiveRecordingAudioEngine rust-wasapi -InstallerLiveRecordingRustAudioCaptureMode wasapi -InstallerLiveRecordingMicAlwaysOn or scripts\smoke_windows_installer.ps1 -LiveRecordingDurationSec -LiveRecordingAudioEngine rust-wasapi -LiveRecordingRustAudioCaptureMode wasapi -LiveRecordingMicAlwaysOn" } elseif ($RequireInstalledLiveRecordingSmoke -or $MinInstalledLiveRecordingDurationSec -gt 0) { "required external report: produce with scripts\build_windows.ps1 -RunInstallerLiveRecordingSmoke or scripts\smoke_windows_installer.ps1 -LiveRecordingDurationSec" } else { "not requested" })
        },
        [pscustomobject]@{
            name = "tauriTextInjectionSmoke"
            command = $(if ($UseExistingTauriTextInjectionSmokeReport) { "reuse $TauriTextInjectionSmokeReport" } elseif ($RunTauriTextInjectionSmoke) { "python " + (Convert-ToDisplayCommand -CommandArgs $tauriTextInjectionSmokeArgs) } elseif (Test-Path -LiteralPath $TauriTextInjectionSmokeReport -PathType Leaf) { "reuse $TauriTextInjectionSmokeReport" } elseif ($RequireTauriTextInjectionSmoke) { "required external report: produce with scripts\smoke_text_injection_target.py --method tauri from a Tauri-managed backend environment or pass -RunTauriTextInjectionSmoke / -UseExistingTauriTextInjectionSmokeReport" } else { "not requested" })
        },
        [pscustomobject]@{
            name = "tauriTextInjectionMatrix"
            command = $(if ($UseExistingTauriTextInjectionMatrixReport) { "reuse $TauriTextInjectionMatrixReport" } elseif ($RunTauriTextInjectionMatrixBuilder) { "python " + (Convert-ToDisplayCommand -CommandArgs $tauriTextInjectionMatrixArgs) } elseif (Test-Path -LiteralPath $TauriTextInjectionMatrixReport -PathType Leaf) { "reuse $TauriTextInjectionMatrixReport" } elseif ($RequireTauriTextInjectionMatrix) { "required external report: produce with scripts\build_tauri_text_injection_matrix.py over installed target-app matrix evidence or pass -RunTauriTextInjectionMatrixBuilder / -UseExistingTauriTextInjectionMatrixReport" } else { "not requested" })
        },
        [pscustomobject]@{
            name = "authenticodeValidation"
            command = $(if ($UseExistingAuthenticodeReport) { "reuse $AuthenticodeReport" } else { "powershell " + (Convert-ToDisplayCommand -CommandArgs $authenticodeArgs) })
        },
        [pscustomobject]@{
            name = "hybridReleaseReadiness"
            command = "python " + (Convert-ToDisplayCommand -CommandArgs $readinessArgs)
        }
    )
}
$planPath = Join-Path $HardwareInputDir "hybrid-release-readiness-runner-plan.json"
$planJson = $plan | ConvertTo-Json -Depth 8 -Compress
Write-Utf8NoBomJson -Path $planPath -Json $planJson

if ($PlanOnly) {
    $planJson
    exit 0
}

if (-not $UseExistingAuthenticodeReport -and $AuthenticodePath.Count -eq 0 -and -not ($RunReleaseBuild -and $ReleaseBuildRequireAuthenticodeSignature)) {
    throw "-AuthenticodePath is required unless -UseExistingAuthenticodeReport is passed."
}

Push-Location $RepoRoot
try {
    if ($RunReleaseBuild) {
        Invoke-Checked -Label "Windows release build" -Command {
            powershell @releaseBuildArgs
        }
        if ($ReleaseBuildRequireAuthenticodeSignature) {
            Assert-ExistingFile -Path $releaseBuildGeneratedAuthenticodeReport -Label "Release-build Authenticode report"
            if ($releaseBuildGeneratedAuthenticodeReport -ne $AuthenticodeReport) {
                New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($AuthenticodeReport)) | Out-Null
                Copy-Item -LiteralPath $releaseBuildGeneratedAuthenticodeReport -Destination $AuthenticodeReport -Force
            }
            $UseExistingAuthenticodeReport = $true
        }
    }

    if ($RunMicrophoneHardwareMatrix) {
        Invoke-Checked -Label "Physical microphone matrix" -Command {
            powershell @microphoneMatrixRunnerArgs
        }
    }

    Invoke-Checked -Label "Physical microphone matrix validation" -Command {
        python @matrixArgs
    }

    if ($RequireMeetingReleaseMatrix -or (Test-Path -LiteralPath $MeetingReleaseMatrixDir -PathType Container)) {
        Invoke-Checked -Label "Meeting release matrix validation" -Command {
            python @meetingMatrixArgs
        }
    }

    if ($UseExistingUpdaterPublicationReport) {
        Assert-ExistingFile -Path $UpdaterPublicationReport -Label "Updater publication report"
    } else {
        Invoke-Checked -Label "Published updater metadata verification" -Command {
            python @updaterArgs
        }
    }

    if ($UseExistingMediaPreparationReport) {
        Assert-ExistingFile -Path $MediaPreparationReport -Label "Media preparation smoke report"
    } else {
        Invoke-Checked -Label "Media preparation smoke" -Command {
            python @mediaPreparationArgs
        }
    }

    if ($UseExistingRuntimeDependencyFootprintReport) {
        Assert-ExistingFile -Path $RuntimeDependencyFootprintReport -Label "Runtime dependency footprint report"
    } else {
        Invoke-Checked -Label "Runtime dependency footprint" -Command {
            python @runtimeDependencyFootprintArgs
        }
    }

    if ($UseExistingRustAudioSidecarReport) {
        Assert-ExistingFile -Path $RustAudioSidecarReport -Label "Rust audio sidecar smoke report"
    } elseif ($RunRustAudioSidecarSmoke) {
        Invoke-Checked -Label "Rust audio sidecar smoke" -Command {
            python @rustAudioSidecarArgs
        }
    } elseif ($effectiveRequireRustAudioSidecarSmoke) {
        throw "Rust audio sidecar evidence is required; pass -RunRustAudioSidecarSmoke or -UseExistingRustAudioSidecarReport."
    }

    if ($UseExistingRustAudioPrewarmSidecarReport) {
        Assert-ExistingFile -Path $RustAudioPrewarmSidecarReport -Label "Rust audio prewarm sidecar smoke report"
    } elseif ($RunRustAudioPrewarmSidecarSmoke) {
        Invoke-Checked -Label "Rust audio prewarm sidecar smoke" -Command {
            python @rustAudioPrewarmSidecarArgs
        }
    } elseif ($RequireRustAudioPrewarmSidecarSmoke) {
        throw "-RequireRustAudioPrewarmSidecarSmoke requires -RunRustAudioPrewarmSidecarSmoke or -UseExistingRustAudioPrewarmSidecarReport."
    }

    if ($UseExistingRustAudioAppPrewarmReport) {
        Assert-ExistingFile -Path $RustAudioAppPrewarmReport -Label "Rust audio app prewarm smoke report"
    } elseif ($RunRustAudioAppPrewarmSmoke) {
        Invoke-Checked -Label "Rust audio app prewarm smoke" -Command {
            python @rustAudioAppPrewarmArgs
        }
    } elseif ($effectiveRequireRustAudioAppPrewarmSmoke) {
        throw "Rust audio app prewarm evidence is required; pass -RunRustAudioAppPrewarmSmoke or -UseExistingRustAudioAppPrewarmReport."
    }

    if ($UseExistingRecordingHotPathComparisonReport) {
        Assert-ExistingFile -Path $RecordingHotPathComparisonReport -Label "Recording hot-path Python/Rust comparison report"
    } elseif ($RunRecordingHotPathComparison) {
        Invoke-Checked -Label "Recording hot-path Python/Rust comparison" -Command {
            powershell @recordingHotPathComparisonArgs
        }
    } elseif (Test-Path -LiteralPath $RecordingHotPathComparisonReport -PathType Leaf) {
        Assert-ExistingFile -Path $RecordingHotPathComparisonReport -Label "Recording hot-path Python/Rust comparison report"
    } elseif ($RequireRecordingHotPathComparison) {
        throw "Recording hot-path Python/Rust comparison evidence is required; pass -RunRecordingHotPathComparison or -UseExistingRecordingHotPathComparisonReport."
    }

    if ($UseExistingInstalledLiveRecordingSmokeReport) {
        Assert-ExistingFile -Path $InstalledLiveRecordingSmokeReport -Label "Installed live recording smoke report"
    } elseif ($RunInstalledLiveRecordingSmoke) {
        if (-not $InstalledLiveRecordingInstallerPath) {
            throw "-RunInstalledLiveRecordingSmoke requires -InstalledLiveRecordingInstallerPath."
        }
        Assert-ExistingFile -Path $InstalledLiveRecordingInstallerPath -Label "Installed live recording installer"
        Invoke-Checked -Label "Installed live recording smoke" -Command {
            powershell @installedLiveRecordingArgs
        }
    } elseif (Test-Path -LiteralPath $InstalledLiveRecordingSmokeReport -PathType Leaf) {
        Assert-ExistingFile -Path $InstalledLiveRecordingSmokeReport -Label "Installed live recording smoke report"
    } elseif ($effectiveRequireInstalledLiveRecordingSmoke) {
        throw "Installed live recording evidence is required; pass -RunInstalledLiveRecordingSmoke or -UseExistingInstalledLiveRecordingSmokeReport."
    }

    if ($UseExistingTauriTextInjectionSmokeReport) {
        Assert-ExistingFile -Path $TauriTextInjectionSmokeReport -Label "Tauri text injection smoke report"
    } elseif ($RunTauriTextInjectionSmoke) {
        Invoke-Checked -Label "Tauri text injection safe-target smoke" -Command {
            python @tauriTextInjectionSmokeArgs
        }
    } elseif (Test-Path -LiteralPath $TauriTextInjectionSmokeReport -PathType Leaf) {
        Assert-ExistingFile -Path $TauriTextInjectionSmokeReport -Label "Tauri text injection smoke report"
    } elseif ($RequireTauriTextInjectionSmoke) {
        throw "Tauri text injection smoke evidence is required; pass -RunTauriTextInjectionSmoke or -UseExistingTauriTextInjectionSmokeReport."
    }

    if ($UseExistingTauriTextInjectionMatrixReport) {
        Assert-ExistingFile -Path $TauriTextInjectionMatrixReport -Label "Tauri text injection matrix report"
    } elseif ($RunTauriTextInjectionMatrixBuilder) {
        Invoke-Checked -Label "Tauri text injection matrix builder" -Command {
            python @tauriTextInjectionMatrixArgs
        }
    } elseif (Test-Path -LiteralPath $TauriTextInjectionMatrixReport -PathType Leaf) {
        Assert-ExistingFile -Path $TauriTextInjectionMatrixReport -Label "Tauri text injection matrix report"
    } elseif ($RequireTauriTextInjectionMatrix) {
        throw "Tauri text injection matrix evidence is required; pass -RunTauriTextInjectionMatrixBuilder or -UseExistingTauriTextInjectionMatrixReport."
    }

    if ($UseExistingAuthenticodeReport) {
        Assert-ExistingFile -Path $AuthenticodeReport -Label "Authenticode report"
    } else {
        Invoke-Checked -Label "Authenticode signature validation" -Command {
            powershell @authenticodeArgs
        }
    }

    Invoke-Checked -Label "Hybrid release readiness validation" -Command {
        python @readinessArgs
    }
} finally {
    Pop-Location
}

[pscustomobject]@{
    ok = $true
    planPath = $planPath
    matrixValidationOutput = $MatrixValidationOutput
    meetingMatrixValidationOutput = $MeetingMatrixValidationOutput
    mediaPreparationReport = $MediaPreparationReport
    runtimeDependencyFootprintReport = $RuntimeDependencyFootprintReport
    rustAudioSidecarReport = $RustAudioSidecarReport
    rustAudioPrewarmSidecarReport = $RustAudioPrewarmSidecarReport
    rustAudioAppPrewarmReport = $RustAudioAppPrewarmReport
    installedLiveRecordingSmokeReport = $InstalledLiveRecordingSmokeReport
    tauriTextInjectionSmokeReport = $TauriTextInjectionSmokeReport
    recordingHotPathComparisonReport = $RecordingHotPathComparisonReport
    updaterPublicationReport = $UpdaterPublicationReport
    authenticodeReport = $AuthenticodeReport
    outputPath = $OutputPath
} | ConvertTo-Json -Depth 5 -Compress
