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

$plan = [pscustomobject]@{
    ok = $true
    planOnly = [bool]$PlanOnly
    hardwareInputDir = $HardwareInputDir
    matrixValidationOutput = $MatrixValidationOutput
    updaterPublicationReport = $UpdaterPublicationReport
    authenticodeReport = $AuthenticodeReport
    outputPath = $OutputPath
    useExistingAuthenticodeReport = [bool]$UseExistingAuthenticodeReport
    useExistingUpdaterPublicationReport = [bool]$UseExistingUpdaterPublicationReport
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
Set-Content -LiteralPath $planPath -Value $planJson -Encoding UTF8

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
