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
    [string]$RustAudioPrewarmSidecarReport = "",
    [double]$RustAudioPrewarmSidecarDurationSec = 1,
    [int]$RustAudioPrewarmSidecarPrebufferMs = 400,
    [switch]$RequireRustAudioSidecarSmoke,
    [switch]$RunRustAudioSidecarSmoke,
    [switch]$UseExistingRustAudioSidecarReport,
    [switch]$RequireRustAudioPrewarmSidecarSmoke,
    [switch]$RunRustAudioPrewarmSidecarSmoke,
    [switch]$UseExistingRustAudioPrewarmSidecarReport,
    [switch]$RequireRustEndpointInventory,
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

    return (($CommandArgs | ForEach-Object {
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

$matrixArgs = @(
    "scripts\validate_microphone_hardware_matrix.py",
    "--input-dir",
    $HardwareInputDir,
    "--output",
    $MatrixValidationOutput
)
if ($RequireRustEndpointInventory -or $RequireRustAudioSidecarSmoke) {
    $matrixArgs += "--require-rust-endpoint-inventory"
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
if ($RustAudioSidecarExe) {
    $rustAudioSidecarArgs += @("--sidecar-exe", $RustAudioSidecarExe)
}

$rustAudioPrewarmSidecarArgs = @(
    "scripts\smoke_rust_audio_prewarm_sidecar.py",
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
if ($RequireRustAudioSidecarSmoke -or $RunRustAudioSidecarSmoke -or $UseExistingRustAudioSidecarReport) {
    $readinessArgs += @("--rust-audio-sidecar-report", $RustAudioSidecarReport)
}
if ($RequireRustAudioSidecarSmoke) {
    $readinessArgs += @("--require-rust-audio-sidecar-smoke", "--min-rust-audio-duration-sec", ([string]$RustAudioSidecarDurationSec))
}
if ($RequireRustAudioPrewarmSidecarSmoke -or $RunRustAudioPrewarmSidecarSmoke -or $UseExistingRustAudioPrewarmSidecarReport) {
    $readinessArgs += @("--rust-audio-prewarm-sidecar-report", $RustAudioPrewarmSidecarReport)
}
if ($RequireRustAudioPrewarmSidecarSmoke) {
    $readinessArgs += "--require-rust-audio-prewarm-sidecar-smoke"
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
        external = $true
        producer = "scripts\run_microphone_hardware_matrix.ps1"
        validator = "scripts\validate_microphone_hardware_matrix.py"
        inputDir = $HardwareInputDir
        expectedArtifacts = @($hardwareArtifacts | ForEach-Object { Join-Path $HardwareInputDir $_ })
        output = $MatrixValidationOutput
        requireRustEndpointInventory = [bool]($RequireRustEndpointInventory -or $RequireRustAudioSidecarSmoke)
        notes = "Requires physical USB, dock, Bluetooth, Windows default-device, and favorite-mic fallback actions on the target Windows machine. Rust audio promotion also requires Rust/WASAPI endpoint inventory evidence in each artifact."
    },
    [pscustomobject]@{
        name = "signedTauriUpdaterMetadata"
        required = $true
        external = $true
        producer = "scripts\build_windows.ps1 -EnableTauriUpdater with signing keys"
        metadata = $UpdaterMetadata
        artifactDir = $UpdaterArtifactDir
        sha256Sums = $Sha256Sums
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
        required = [bool]$RequireRustAudioSidecarSmoke
        external = $false
        producer = $(if ($UseExistingRustAudioSidecarReport) { "existing report" } elseif ($RunRustAudioSidecarSmoke) { "scripts\smoke_rust_audio_sidecar.py" } else { "not requested" })
        report = $RustAudioSidecarReport
        durationSec = $RustAudioSidecarDurationSec
        selectedDurationSec = $RustAudioSidecarSelectedDurationSec
        prebufferMs = $RustAudioSidecarPrebufferMs
        notes = "Optional for standard releases. Required when evaluating Rust audio promotion; validates default WASAPI capture, selected native endpoint hash capture, requested prebuffer delivery, frame-pipe metrics, and stop health."
    },
    [pscustomobject]@{
        name = "rustAudioPrewarmSidecarSmoke"
        required = [bool]$RequireRustAudioPrewarmSidecarSmoke
        external = $false
        producer = $(if ($UseExistingRustAudioPrewarmSidecarReport) { "existing report" } elseif ($RunRustAudioPrewarmSidecarSmoke) { "scripts\smoke_rust_audio_prewarm_sidecar.py" } else { "not requested" })
        report = $RustAudioPrewarmSidecarReport
        durationSec = $RustAudioPrewarmSidecarDurationSec
        prebufferMs = $RustAudioPrewarmSidecarPrebufferMs
        notes = "Optional synthetic lifecycle evidence only. It validates prewarmStart/prewarmStop routing, prewarmId matching, buffered-frame counters, and stop health; it is not WASAPI idle prewarm adoption evidence."
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
        expectedPublisher = $ExpectedAuthenticodePublisher
        requireTimestamp = [bool]$RequireAuthenticodeTimestamp
        notes = "The Authenticode report must include the release artifact names from latest.json, not only an unrelated signed executable."
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
    hardwareInputDir = $HardwareInputDir
    matrixValidationOutput = $MatrixValidationOutput
    mediaPreparationReport = $MediaPreparationReport
    mediaToolsDir = $MediaToolsDir
    runtimeDependencyFootprintReport = $RuntimeDependencyFootprintReport
    sidecarDir = $SidecarDir
    rustAudioSidecarReport = $RustAudioSidecarReport
    rustAudioSidecarDurationSec = $RustAudioSidecarDurationSec
    rustAudioSidecarSelectedDurationSec = $RustAudioSidecarSelectedDurationSec
    rustAudioSidecarPrebufferMs = $RustAudioSidecarPrebufferMs
    rustAudioPrewarmSidecarReport = $RustAudioPrewarmSidecarReport
    rustAudioPrewarmSidecarDurationSec = $RustAudioPrewarmSidecarDurationSec
    rustAudioPrewarmSidecarPrebufferMs = $RustAudioPrewarmSidecarPrebufferMs
    updaterPublicationReport = $UpdaterPublicationReport
    authenticodeReport = $AuthenticodeReport
    outputPath = $OutputPath
    useExistingAuthenticodeReport = [bool]$UseExistingAuthenticodeReport
    useExistingMediaPreparationReport = [bool]$UseExistingMediaPreparationReport
    useExistingRuntimeDependencyFootprintReport = [bool]$UseExistingRuntimeDependencyFootprintReport
    useExistingRustAudioSidecarReport = [bool]$UseExistingRustAudioSidecarReport
    useExistingRustAudioPrewarmSidecarReport = [bool]$UseExistingRustAudioPrewarmSidecarReport
    runRustAudioSidecarSmoke = [bool]$RunRustAudioSidecarSmoke
    runRustAudioPrewarmSidecarSmoke = [bool]$RunRustAudioPrewarmSidecarSmoke
    requireRustAudioSidecarSmoke = [bool]$RequireRustAudioSidecarSmoke
    requireRustAudioPrewarmSidecarSmoke = [bool]$RequireRustAudioPrewarmSidecarSmoke
    useExistingUpdaterPublicationReport = [bool]$UseExistingUpdaterPublicationReport
    requiredEvidence = $requiredEvidence
    commands = @(
        [pscustomobject]@{
            name = "microphoneMatrixValidation"
            command = "python " + (Convert-ToDisplayCommand -CommandArgs $matrixArgs)
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
            command = $(if ($UseExistingRustAudioSidecarReport) { "reuse $RustAudioSidecarReport" } elseif ($RunRustAudioSidecarSmoke) { "python " + (Convert-ToDisplayCommand -CommandArgs $rustAudioSidecarArgs) } else { "not requested" })
        },
        [pscustomobject]@{
            name = "rustAudioPrewarmSidecarSmoke"
            command = $(if ($UseExistingRustAudioPrewarmSidecarReport) { "reuse $RustAudioPrewarmSidecarReport" } elseif ($RunRustAudioPrewarmSidecarSmoke) { "python " + (Convert-ToDisplayCommand -CommandArgs $rustAudioPrewarmSidecarArgs) } else { "not requested" })
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

if (-not $UseExistingAuthenticodeReport -and $AuthenticodePath.Count -eq 0) {
    throw "-AuthenticodePath is required unless -UseExistingAuthenticodeReport is passed."
}

Push-Location $RepoRoot
try {
    Invoke-Checked -Label "Physical microphone matrix validation" -Command {
        python @matrixArgs
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
    } elseif ($RequireRustAudioSidecarSmoke) {
        throw "-RequireRustAudioSidecarSmoke requires -RunRustAudioSidecarSmoke or -UseExistingRustAudioSidecarReport."
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
    mediaPreparationReport = $MediaPreparationReport
    runtimeDependencyFootprintReport = $RuntimeDependencyFootprintReport
    rustAudioSidecarReport = $RustAudioSidecarReport
    rustAudioPrewarmSidecarReport = $RustAudioPrewarmSidecarReport
    updaterPublicationReport = $UpdaterPublicationReport
    authenticodeReport = $AuthenticodeReport
    outputPath = $OutputPath
} | ConvertTo-Json -Depth 5 -Compress
