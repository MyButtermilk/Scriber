<#
.SYNOPSIS
Builds a Windows desktop release bundle for Scriber.

.DESCRIPTION
Runs the frontend type check, builds the Tauri Windows bundle, and optionally
runs the release smoke test. The Tauri `beforeBundleCommand` builds and copies
the Python backend sidecar with bundled ffmpeg/ffprobe before NSIS packaging.

Typical flow:
  powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string[]]$Bundles = @("nsis"),
    [string]$ReleaseBaseUrl = "",
    [switch]$EnableTauriUpdater,
    [string]$UpdaterEndpoint = "",
    [string]$UpdaterPublicKey = "",
    [switch]$RequireUpdaterSignatures,
    [switch]$RequireAuthenticodeSignature,
    [string]$ExpectedAuthenticodePublisher = "",
    [switch]$RequireAuthenticodeTimestamp,
    [double]$MaxInstallerSizeMB = 220,
    [double]$InstallerMaxInstalledSizeMB = 0,
    [switch]$SkipBundledFfprobe,
    [switch]$ValidateSlimMediaTools,
    [switch]$SkipChecks,
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
    [switch]$RunInstallerMediaPreparationSmoke,
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
    [switch]$InstallerDisableLiveTextInjection,
    [switch]$RunInstallerLegacyDataSmoke,
    [switch]$RunInstallerUpgradeSmoke,
    [switch]$RunInstallerUninstallSmoke,
    [switch]$RunMediaPreparationSmoke
)

$ErrorActionPreference = "Stop"

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

function Set-Utf8NoBomContent {
    param(
        [string]$Path,
        [string]$Value
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Add-TauriBeforeBundleCommandSwitch {
    param(
        [string]$ConfigText,
        [string]$SwitchName
    )

    if ($ConfigText.Contains($SwitchName)) {
        return $ConfigText
    }

    $copySwitch = " -CopyToTauriRelease"
    if (-not $ConfigText.Contains($copySwitch)) {
        throw "Cannot enable $SwitchName because beforeBundleCommand does not contain '$copySwitch'."
    }

    return $ConfigText.Replace($copySwitch, " $SwitchName$copySwitch")
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
$frontendRoot = Join-Path $RepoRoot "Frontend"
$bundleArg = ($Bundles -join ",")
$tauriConfigPath = Join-Path $RepoRoot "Frontend\src-tauri\tauri.conf.json"
$tauriConfigOriginal = $null

if (-not (Test-Path (Join-Path $frontendRoot "package.json"))) {
    throw "Frontend package.json was not found under $frontendRoot."
}

Invoke-Checked -Label "Version sync" -Command {
    Push-Location $RepoRoot
    try {
        python scripts\sync_version.py
    } finally {
        Pop-Location
    }
}

if (-not $SkipChecks) {
    Invoke-Checked -Label "Python tests" -Command {
        Push-Location $RepoRoot
        try {
            python -m pytest -q
        } finally {
            Pop-Location
        }
    }

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
        $tauriConfigOriginal = Get-Content -Raw $tauriConfigPath
        Invoke-Checked -Label "Prepare Tauri updater config" -Command {
            Push-Location $RepoRoot
            try {
                $updaterArgs = @(
                    "scripts\prepare_tauri_updater_config.py",
                    "--write"
                )
                if ($UpdaterEndpoint) {
                    $updaterArgs += @("--endpoint", $UpdaterEndpoint)
                }
                if ($UpdaterPublicKey) {
                    $updaterArgs += @("--public-key", $UpdaterPublicKey)
                }
                python @updaterArgs
            } finally {
                Pop-Location
            }
        }
        $RequireUpdaterSignatures = $true
    }

    if ($SkipBundledFfprobe -or $ValidateSlimMediaTools) {
        if ($null -eq $tauriConfigOriginal) {
            $tauriConfigOriginal = Get-Content -Raw $tauriConfigPath
        }
        $currentTauriConfig = Get-Content -Raw $tauriConfigPath
        $updatedTauriConfig = $currentTauriConfig
        if ($SkipBundledFfprobe) {
            $updatedTauriConfig = Add-TauriBeforeBundleCommandSwitch -ConfigText $updatedTauriConfig -SwitchName "-SkipBundledFfprobe"
        }
        if ($ValidateSlimMediaTools) {
            $updatedTauriConfig = Add-TauriBeforeBundleCommandSwitch -ConfigText $updatedTauriConfig -SwitchName "-ValidateSlimMediaTools"
        }
        if ($updatedTauriConfig -ne $currentTauriConfig) {
            $currentTauriConfig = $updatedTauriConfig
            Set-Utf8NoBomContent -Path $tauriConfigPath -Value $currentTauriConfig
        }
    }

    Invoke-Checked -Label "Tauri Windows bundle" -Command {
        Push-Location $frontendRoot
        try {
            npm run tauri:build -- --bundles $bundleArg
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
    $artifacts = @()
    if (Test-Path $bundleRoot) {
        $artifacts = @(
            Get-ChildItem -Path $bundleRoot -Recurse -File -Include *.exe,*.msi |
                Select-Object -ExpandProperty FullName
        )
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
                python @mediaSmokeArgs
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
                python @metadataArgs
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
                python @validationArgs
            } finally {
                Pop-Location
            }
        }

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
                $backendReleaseDir = Join-Path $targetRelease "backend"
                if (Test-Path -LiteralPath $backendReleaseDir -PathType Container) {
                    $sizeReportArgs += @("--top-root", $backendReleaseDir)
                }
                foreach ($artifact in $artifacts) {
                    $sizeReportArgs += @("--artifact", $artifact)
                }
                python @sizeReportArgs
            } finally {
                Pop-Location
            }
        }
    }

    if ($RunInstallerSmoke -or $RunInstallerCrashSmoke -or $RunInstallerPortConflictSmoke -or $RunInstallerControlledShutdownSmoke -or $RunInstallerExternalBackendSmoke -or $RunInstallerStartupTimeoutSmoke -or $RunInstallerGlobalHotkeyRegistrationSmoke -or $RunInstallerGlobalHotkeySmoke -or $RunInstallerManualGlobalHotkeySmoke -or $RunInstallerSupportBundleSmoke -or $RunInstallerFrontendSmoke -or $RunInstallerMediaPreparationSmoke -or $RunInstallerStabilitySmoke -or $RunInstallerLiveRecordingSmoke -or $RunInstallerLegacyDataSmoke -or $RunInstallerUpgradeSmoke -or $RunInstallerUninstallSmoke) {
        Invoke-Checked -Label "Installed package smoke" -Command {
            Push-Location $RepoRoot
            try {
                $installerSmokeArgs = @(
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    "scripts\smoke_windows_installer.ps1"
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
                if ($RunInstallerMediaPreparationSmoke) {
                    $installerSmokeArgs += "-VerifyMediaPreparation"
                    if ($SkipBundledFfprobe) {
                        $installerSmokeArgs += "-AllowMissingFfprobeForMediaPreparation"
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
                    if ($InstallerDisableLiveTextInjection) {
                        $installerSmokeArgs += "-DisableLiveTextInjection"
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
            } finally {
                Pop-Location
            }
        }
    }

    [pscustomobject]@{
        ok = $true
        bundles = $Bundles
        updaterEnabled = [bool]$EnableTauriUpdater
        releaseExe = $releaseExe
        artifacts = $artifacts
        metadataDir = $metadataDir
        sizeReport = Join-Path $metadataDir "size-report.json"
        mediaPreparationSmoke = $mediaPreparationSmokePath
    } | ConvertTo-Json -Compress
} finally {
    if ($null -ne $tauriConfigOriginal) {
        Set-Utf8NoBomContent -Path $tauriConfigPath -Value $tauriConfigOriginal
    }
}
