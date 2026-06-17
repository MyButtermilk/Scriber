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

function Convert-ToJsonStringContent {
    param([string]$Value)

    $json = $Value | ConvertTo-Json -Compress
    return $json.Substring(1, $json.Length - 2)
}

function Add-TauriBeforeBundleCommandValueSwitch {
    param(
        [string]$ConfigText,
        [string]$SwitchName,
        [string]$Value
    )

    if ($ConfigText.Contains($SwitchName)) {
        return $ConfigText
    }

    $copySwitch = " -CopyToTauriRelease"
    if (-not $ConfigText.Contains($copySwitch)) {
        throw "Cannot enable $SwitchName because beforeBundleCommand does not contain '$copySwitch'."
    }

    $commandArgument = if ($Value -match '\s') { '"' + $Value + '"' } else { $Value }
    $escapedCommandArgument = Convert-ToJsonStringContent -Value $commandArgument
    return $ConfigText.Replace($copySwitch, " $SwitchName $escapedCommandArgument$copySwitch")
}

function Set-TauriNsisCompression {
    param(
        [string]$ConfigText,
        [string]$Compression
    )

    if (-not $Compression) {
        return $ConfigText
    }

    $config = $ConfigText | ConvertFrom-Json
    if (-not $config.bundle) {
        $config | Add-Member -NotePropertyName "bundle" -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    if (-not $config.bundle.windows) {
        $config.bundle | Add-Member -NotePropertyName "windows" -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    if (-not $config.bundle.windows.nsis) {
        $config.bundle.windows | Add-Member -NotePropertyName "nsis" -NotePropertyValue ([pscustomobject]@{}) -Force
    }

    $config.bundle.windows.nsis | Add-Member -NotePropertyName "compression" -NotePropertyValue $Compression -Force
    return ($config | ConvertTo-Json -Depth 100)
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

$RepoRoot = (Resolve-Path $RepoRoot).Path
$frontendRoot = Join-Path $RepoRoot "Frontend"
$bundleArg = ($Bundles -join ",")
$tauriConfigPath = Join-Path $RepoRoot "Frontend\src-tauri\tauri.conf.json"
$tauriConfigOriginal = $null
if ($MediaToolsDir) {
    $MediaToolsDir = (Resolve-Path $MediaToolsDir).Path
}
if ($UseProfileBFfmpeg) {
    $ValidateSlimMediaTools = $true
}

if ($FastLocalInstaller -and $FastLocalStagedApp) {
    throw "Use either -FastLocalInstaller or -FastLocalStagedApp, not both."
}
if ($NsisCompression -and -not $FastLocalInstaller) {
    throw "-NsisCompression is a dev-only FastLocalInstaller option."
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
        $NsisCompression = "zlib"
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
        $MaxMediaToolsRuntimeDependencyMB = if ($UseProfileBFfmpeg) { 10 } else { 210 }
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
        $MaxMediaToolsRuntimeDependencyMB = if ($UseProfileBFfmpeg) { 10 } else { 210 }
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
        python scripts\sync_version.py
    } finally {
        Pop-Location
    }
}

if (-not $SkipChecks -and -not $SkipPythonTests) {
    Invoke-Checked -Label "Python tests" -Command {
        Push-Location $RepoRoot
        try {
            python -m pytest -q
        } finally {
            Pop-Location
        }
    }
}

if (-not $SkipChecks -and -not $SkipFrontendTypeCheck) {
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

    if ((-not $FastLocalStagedApp) -and ($SkipBundledFfprobe -or $ValidateSlimMediaTools -or $MediaToolsDir -or $UseProfileBFfmpeg -or $ReuseSidecarIfUnchanged -or $LocalPyInstallerNoClean -or $RustAudioIsolatedTarget -or $NsisCompression)) {
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
        if ($UseProfileBFfmpeg) {
            $updatedTauriConfig = Add-TauriBeforeBundleCommandSwitch -ConfigText $updatedTauriConfig -SwitchName "-UseProfileBFfmpeg"
        }
        if ($MediaToolsDir) {
            $updatedTauriConfig = Add-TauriBeforeBundleCommandValueSwitch -ConfigText $updatedTauriConfig -SwitchName "-MediaToolsDir" -Value $MediaToolsDir
        }
        if ($ReuseSidecarIfUnchanged) {
            $updatedTauriConfig = Add-TauriBeforeBundleCommandSwitch -ConfigText $updatedTauriConfig -SwitchName "-ReuseSidecarIfUnchanged"
        }
        if ($LocalPyInstallerNoClean) {
            $updatedTauriConfig = Add-TauriBeforeBundleCommandSwitch -ConfigText $updatedTauriConfig -SwitchName "-LocalPyInstallerNoClean"
        }
        if ($RustAudioIsolatedTarget) {
            $updatedTauriConfig = Add-TauriBeforeBundleCommandSwitch -ConfigText $updatedTauriConfig -SwitchName "-RustAudioIsolatedTarget"
        }
        if ($NsisCompression) {
            $updatedTauriConfig = Set-TauriNsisCompression -ConfigText $updatedTauriConfig -Compression $NsisCompression
        }
        if ($updatedTauriConfig -ne $currentTauriConfig) {
            $currentTauriConfig = $updatedTauriConfig
            Set-Utf8NoBomContent -Path $tauriConfigPath -Value $currentTauriConfig
        }
    }

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
        Invoke-Checked -Label "Tauri Windows bundle" -Command {
            Push-Location $frontendRoot
            try {
                npm run tauri:build -- --bundles $bundleArg
            } finally {
                Pop-Location
            }
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
    $installedPackageSmokeTempPath = Join-Path $RepoRoot "tmp\installer-smoke\installed-package-smoke.json"
    $buildTimingPath = Join-Path $metadataDir "build-timing.json"
    foreach ($staleReport in @($mediaPreparationSmokePath, $runtimeDependencyFootprintPath, $installedPackageSmokePath, $installedPackageSmokeTempPath)) {
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
    $artifacts = @()
    if ((-not $FastLocalStagedApp) -and (Test-Path $bundleRoot)) {
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
                python @footprintArgs
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

    }

    if ($RunInstallerSmoke -or $RunInstallerCrashSmoke -or $RunInstallerPortConflictSmoke -or $RunInstallerControlledShutdownSmoke -or $RunInstallerExternalBackendSmoke -or $RunInstallerStartupTimeoutSmoke -or $RunInstallerGlobalHotkeyRegistrationSmoke -or $RunInstallerGlobalHotkeySmoke -or $RunInstallerManualGlobalHotkeySmoke -or $RunInstallerSupportBundleSmoke -or $RunInstallerFrontendSmoke -or $RunInstallerMediaPreparationSmoke -or $RunInstallerRealMediaWorkflowSmoke -or $RunInstallerStabilitySmoke -or $RunInstallerLiveRecordingSmoke -or $RunInstallerLegacyDataSmoke -or $RunInstallerUpgradeSmoke -or $RunInstallerUninstallSmoke) {
        Invoke-Checked -Label "Installed package smoke" -Command {
            Push-Location $RepoRoot
            try {
                $installerSmokeArgs = @(
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    "scripts\smoke_windows_installer.ps1",
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
                python @sizeReportArgs
            } finally {
                Pop-Location
            }
        }
    }

    $buildMode = [ordered]@{
        artifactKind = if ($FastLocalStagedApp) { "staged-app" } else { "installer" }
        devOnly = [bool]($FastLocalInstaller -or $FastLocalStagedApp -or ($NsisCompression -and $NsisCompression -ne "lzma") -or $LocalPyInstallerNoClean)
        fastLocalInstaller = [bool]$FastLocalInstaller
        fastLocalStagedApp = [bool]$FastLocalStagedApp
        installerBuilt = [bool]($artifacts.Count -gt 0)
        installerSmokeValidated = [bool]$installedPackageSmoke["ran"]
        nsisCompression = if ($NsisCompression) { $NsisCompression } else { "tauri-default" }
        localPyInstallerNoClean = [bool]$LocalPyInstallerNoClean
        rustAudioIsolatedTarget = [bool]$RustAudioIsolatedTarget
    }
    $sidecarMetadataPath = Join-Path $targetRelease "backend\sidecar-build-metadata.json"
    $buildTimingPath = Write-BuildTimingReport -MetadataDir $metadataDir -SidecarMetadataPath $sidecarMetadataPath -BuildMode $buildMode

    [pscustomobject]@{
        ok = $true
        bundles = $Bundles
        updaterEnabled = [bool]$EnableTauriUpdater
        releaseExe = $releaseExe
        artifacts = $artifacts
        metadataDir = $metadataDir
        sizeReport = Join-Path $metadataDir "size-report.json"
        buildTiming = $buildTimingPath
        buildMode = $buildMode
        mediaPreparationSmoke = $mediaPreparationSmoke
        runtimeDependencyFootprint = $runtimeDependencyFootprint
        installedPackageSmoke = $installedPackageSmoke
    } | ConvertTo-Json -Compress
} finally {
    if ($null -ne $tauriConfigOriginal) {
        Set-Utf8NoBomContent -Path $tauriConfigPath -Value $tauriConfigOriginal
    }
}
