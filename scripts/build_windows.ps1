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
    [switch]$SkipChecks,
    [switch]$SkipSmoke,
    [switch]$RunInstallerSmoke,
    [switch]$RunInstallerCrashSmoke,
    [switch]$RunInstallerPortConflictSmoke,
    [switch]$RunInstallerControlledShutdownSmoke,
    [switch]$RunInstallerLegacyDataSmoke,
    [switch]$RunInstallerUpgradeSmoke
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
    $bundleRoot = Join-Path $targetRelease "bundle"
    $artifacts = @()
    if (Test-Path $bundleRoot) {
        $artifacts = @(
            Get-ChildItem -Path $bundleRoot -Recurse -File -Include *.exe,*.msi |
                Select-Object -ExpandProperty FullName
        )
    }

    $metadataDir = Join-Path $targetRelease "release-metadata"
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
                    (Join-Path $metadataDir "latest.json")
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

    if ($RunInstallerSmoke -or $RunInstallerCrashSmoke -or $RunInstallerPortConflictSmoke -or $RunInstallerControlledShutdownSmoke -or $RunInstallerLegacyDataSmoke -or $RunInstallerUpgradeSmoke) {
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
                if ($RunInstallerLegacyDataSmoke) {
                    $installerSmokeArgs += @("-LegacyDataDir", $RepoRoot, "-VerifyLegacyDataMigration")
                }
                if ($RunInstallerUpgradeSmoke) {
                    $installerSmokeArgs += "-SimulateUpgrade"
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
        releaseExe = Join-Path $targetRelease "scriber-desktop.exe"
        artifacts = $artifacts
        metadataDir = $metadataDir
    } | ConvertTo-Json -Compress
} finally {
    if ($null -ne $tauriConfigOriginal) {
        Set-Content -Path $tauriConfigPath -Value $tauriConfigOriginal -NoNewline -Encoding UTF8
    }
}
