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
if (-not $OutputPath) {
    $OutputPath = Join-Path $HardwareInputDir "hybrid-release-readiness.json"
} else {
    $OutputPath = Convert-ToFullPath -Path $OutputPath -Root $RepoRoot
}

$UpdaterMetadata = Convert-ToFullPath -Path $UpdaterMetadata -Root $RepoRoot
$UpdaterArtifactDir = Convert-ToFullPath -Path $UpdaterArtifactDir -Root $RepoRoot
$Sha256Sums = Convert-ToFullPath -Path $Sha256Sums -Root $RepoRoot
$MediaPreparationReport = Convert-ToFullPath -Path $MediaPreparationReport -Root $RepoRoot
$AuthenticodePath = @($AuthenticodePath | ForEach-Object { Convert-ToFullPath -Path $_ -Root $RepoRoot })

$matrixArgs = @(
    "scripts\validate_microphone_hardware_matrix.py",
    "--input-dir",
    $HardwareInputDir,
    "--output",
    $MatrixValidationOutput
)
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
    "--updater-publication-report",
    $UpdaterPublicationReport,
    "--authenticode-report",
    $AuthenticodeReport,
    "--output",
    $OutputPath
)
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
        notes = "Requires physical USB, dock, Bluetooth, Windows default-device, and favorite-mic fallback actions on the target Windows machine."
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
        producer = "scripts\build_windows.ps1 -RunMediaPreparationSmoke"
        report = $MediaPreparationReport
        notes = "Validates bundled ffmpeg/ffprobe through Scriber file-upload compression, video extraction, YouTube normalization, Azure-MAI preparation, and ffprobe duration probing."
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
    updaterPublicationReport = $UpdaterPublicationReport
    authenticodeReport = $AuthenticodeReport
    outputPath = $OutputPath
    useExistingAuthenticodeReport = [bool]$UseExistingAuthenticodeReport
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
    updaterPublicationReport = $UpdaterPublicationReport
    authenticodeReport = $AuthenticodeReport
    outputPath = $OutputPath
} | ConvertTo-Json -Depth 5 -Compress
