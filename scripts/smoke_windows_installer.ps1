<#
.SYNOPSIS
Runs a smoke test against the installed Windows NSIS package.

.DESCRIPTION
Installs the generated NSIS setup into a temporary per-repo directory, starts
the installed app without development fallback, verifies that the packaged
backend sidecar becomes healthy, then uninstalls the app unless -KeepInstalled
is passed. With -SimulateBackendCrash, it also verifies that the installed
desktop shell restarts a killed backend worker and writes crash metadata. With
-OccupyDefaultPort, it verifies that the installed supervisor avoids the
occupied default backend port. With -SimulateBackendShutdown, it verifies the
token-protected controlled worker shutdown and supervisor recovery path. With
-AttachExternalBackend, it starts an external Python backend and verifies that
the installed Tauri shell attaches without spawning a managed sidecar. With
-SimulateBackendStartupTimeout, it verifies that the installed supervisor
replaces a backend worker that never becomes ready. With -VerifyLegacyDataMigration,
it verifies that first-run legacy runtime data is copied into the installed app
data directory. With -SimulateUpgrade, it runs the installer a second time
against the same install/data directories and verifies that existing app data is
preserved. With -StabilityDurationSec, the installed desktop smoke keeps the app
running for repeated health/state probes before cleanup. With
-MaxBackendWorkingSetGrowthMB, stability also fails on excessive backend
working-set peak growth. With -MaxIdleCpuPercent, stability fails when
normalized average idle CPU for the Tauri app plus backend exceeds the
configured threshold. With -VerifyUninstall, the silent uninstaller becomes a
strict release gate: it must remove installed app artifacts while preserving
runtime data before the script removes temporary smoke-test directories.
With -WaitForManualGlobalHotkey, the installed desktop smoke waits for a
physical OS hotkey press and verifies the dispatch against the installed app.
With -LiveRecordingDurationSec, the installed desktop smoke explicitly records
from the live microphone path for the configured duration and samples CPU,
memory, health, and state while recording is active.
With -VerifySupportBundle, the installed desktop smoke downloads the
token-protected support bundle and verifies dummy secret redaction.
With -VerifyFrontend, the installed desktop smoke verifies that the installed
frontend is owned by the Tauri WebView bundle instead of the Python backend
sidecar, verifies that Tauri production origins can call /api/health and
tokenized /api/runtime, then waits for the actual Tauri WebView frontend-ready
beacon.
With -VerifyMediaPreparation, the installed desktop smoke runs the media
preparation helper smoke against the ffmpeg/ffprobe binaries that were actually
installed with the packaged app.
With -VerifyRealMediaWorkflows, the installed desktop smoke runs real file and
YouTube transcription workflows through the installed backend and requires
completed transcript plus summary evidence.
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$InstallerPath = "",
    [string]$InstallDir = "",
    [string]$DataDir = "",
    [string]$OutputPath = "",
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
    [switch]$VerifyMediaPreparation,
    [switch]$VerifyRealMediaWorkflows,
    [string]$RealWorkflowYoutubeUrl = "https://www.youtube.com/watch?v=0wEjbSYNUM8",
    [int]$RealWorkflowFileTimeoutSec = 240,
    [int]$RealWorkflowYoutubeTimeoutSec = 420,
    [int]$RealWorkflowPollSec = 3,
    [switch]$RealWorkflowSkipFile,
    [switch]$RealWorkflowSkipYoutube,
    [switch]$RealWorkflowNoSummary,
    [switch]$AllowMissingFfprobeForMediaPreparation,
    [string]$GlobalHotkeySmokeHotkey = "ctrl+alt+shift+f12",
    [int]$GlobalHotkeyDispatchTimeoutSec = 20,
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
    [double]$MaxInstalledSizeMB = 0,
    [string]$LegacyDataDir = "",
    [switch]$VerifyLegacyDataMigration,
    [switch]$SimulateUpgrade,
    [switch]$VerifyUninstall,
    [switch]$KeepInstalled
)

$ErrorActionPreference = "Stop"

function Convert-ToFullPath {
    param([string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
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

function Invoke-ProcessChecked {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$Label
    )

    $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "$Label failed with exit code $($process.ExitCode)."
    }
}

function Stop-ProcessesUnderPath {
    param(
        [string]$Root,
        [string]$Label
    )

    if (-not (Test-Path $Root)) {
        return
    }
    $rootFull = (Convert-ToFullPath -Path $Root).ToLowerInvariant()
    $processes = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $cmd = if ($_.CommandLine) { $_.CommandLine.ToLowerInvariant() } else { "" }
                $exe = if ($_.ExecutablePath) { $_.ExecutablePath.ToLowerInvariant() } else { "" }
                $cmd.Contains($rootFull) -or $exe.StartsWith($rootFull)
            }
    )
    foreach ($process in $processes) {
        try {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
            Write-Host "Stopped stale $Label process $($process.ProcessId) ($($process.Name))."
        } catch {
            Write-Warning "Could not stop stale $Label process $($process.ProcessId): $_"
        }
    }
    if ($processes.Count -gt 0) {
        Start-Sleep -Seconds 2
    }
}

function Resolve-InstalledAppExe {
    param([string]$Root)

    $candidates = @(
        (Join-Path $Root "Scriber.exe"),
        (Join-Path $Root "scriber-desktop.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    $exe = Get-ChildItem -LiteralPath $Root -Recurse -File -Include "Scriber.exe", "scriber-desktop.exe" |
        Select-Object -First 1
    if ($exe) {
        return $exe.FullName
    }
    throw "Installed Scriber executable was not found under $Root."
}

function Resolve-InstalledUninstaller {
    param([string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return $null
    }
    $uninstaller = Get-ChildItem -LiteralPath $Root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @("uninstall.exe", "Uninstall.exe") -or $_.Name -match "^unins.*\.exe$" } |
        Select-Object -First 1
    if ($uninstaller) {
        return $uninstaller.FullName
    }
    return $null
}

function Resolve-InstalledMediaToolsDir {
    param([string]$Root)

    $candidates = @(
        (Join-Path $Root "backend\tools\ffmpeg"),
        (Join-Path $Root "resources\backend\tools\ffmpeg")
    )
    foreach ($candidate in $candidates) {
        $ffmpeg = Join-Path $candidate "ffmpeg.exe"
        if (Test-Path -LiteralPath $ffmpeg -PathType Leaf) {
            return (Resolve-Path $candidate).Path
        }
    }

    $ffmpegExe = Get-ChildItem -LiteralPath $Root -Recurse -File -Filter "ffmpeg.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\tools\\ffmpeg\\ffmpeg\.exe$" } |
        Select-Object -First 1
    if ($ffmpegExe) {
        return $ffmpegExe.Directory.FullName
    }

    throw "Installed ffmpeg.exe was not found under $Root."
}

function Resolve-InstalledAudioSidecarExe {
    param([string]$Root)

    $candidates = @(
        (Join-Path $Root "scriber-audio-sidecar.exe"),
        (Join-Path $Root "audio-sidecar\scriber-audio-sidecar.exe"),
        (Join-Path $Root "resources\audio-sidecar\scriber-audio-sidecar.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path $candidate).Path
        }
    }

    $audioSidecarExe = Get-ChildItem -LiteralPath $Root -Recurse -File -Filter "scriber-audio-sidecar.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\scriber-audio-sidecar\.exe$" } |
        Select-Object -First 1
    if ($audioSidecarExe) {
        return $audioSidecarExe.FullName
    }

    throw "Installed scriber-audio-sidecar.exe was not found under $Root."
}

function Test-InstalledFrontendAssetOwnership {
    param([string]$Root)

    $legacyBackendAssetTrees = @(
        (Join-Path $Root "backend\Frontend\dist\public"),
        (Join-Path $Root "resources\backend\Frontend\dist\public")
    )
    $found = @(
        $legacyBackendAssetTrees |
            Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
            ForEach-Object { Convert-ToRelativePath -Root $Root -Path $_ }
    )
    if ($found.Count -gt 0) {
        throw "Installed Python backend sidecar still contains frontend asset tree(s): $($found -join ', ')"
    }

    return [pscustomobject]@{
        verified = $true
        source = "tauri-webview"
        backendFrontendAssetTreesAbsent = $true
        checkedPaths = @(
            $legacyBackendAssetTrees |
                ForEach-Object { Convert-ToRelativePath -Root $Root -Path $_ }
        )
    }
}

function Convert-ToRelativePath {
    param(
        [string]$Root,
        [string]$Path
    )

    $rootFull = Convert-ToFullPath -Path $Root
    $pathFull = Convert-ToFullPath -Path $Path
    if ($pathFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return "."
    }
    $rootPrefix = $rootFull.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if ($pathFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $pathFull.Substring($rootPrefix.Length)
    }
    return $pathFull
}

function Get-RemainingInstallArtifacts {
    param([string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return @()
    }
    return @(
        Get-ChildItem -LiteralPath $Root -Recurse -Force -ErrorAction SilentlyContinue |
            ForEach-Object { Convert-ToRelativePath -Root $Root -Path $_.FullName }
    )
}

function Get-DirectorySizeReport {
    param(
        [string]$Root,
        [double]$MaxSizeMB = 0,
        [int]$TopFilesLimit = 20
    )

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Directory size report root not found: $Root"
    }

    $files = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue)
    $totalBytes = [int64]0
    foreach ($file in $files) {
        $totalBytes += [int64]$file.Length
    }
    $totalMb = [Math]::Round($totalBytes / 1MB, 2)
    $topFiles = @(
        $files |
            Sort-Object Length -Descending |
            Select-Object -First $TopFilesLimit |
            ForEach-Object {
                [pscustomobject]@{
                    path = Convert-ToRelativePath -Root $Root -Path $_.FullName
                    sizeBytes = [int64]$_.Length
                    sizeMb = [Math]::Round(([int64]$_.Length) / 1MB, 2)
                }
            }
    )
    $withinBudget = $null
    if ($MaxSizeMB -gt 0) {
        $withinBudget = ($totalMb -le $MaxSizeMB)
        if (-not $withinBudget) {
            throw "Installed app size ${totalMb} MB exceeds budget ${MaxSizeMB} MB."
        }
    }

    return [pscustomobject]@{
        path = $Root
        fileCount = $files.Count
        totalBytes = $totalBytes
        totalMb = $totalMb
        maxInstalledSizeMb = if ($MaxSizeMB -gt 0) { $MaxSizeMB } else { $null }
        withinBudget = $withinBudget
        topFiles = $topFiles
    }
}

function Wait-InstallArtifactsRemoved {
    param(
        [string]$Root,
        [int]$TimeoutSec = 15
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        $remaining = @(Get-RemainingInstallArtifacts -Root $Root)
        if ($remaining.Count -eq 0) {
            return @()
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    return @(Get-RemainingInstallArtifacts -Root $Root)
}

function Invoke-InstalledUninstallCheck {
    param(
        [string]$InstallRoot,
        [string]$RuntimeDataDir,
        [switch]$Strict
    )

    $sentinelPath = Join-Path $RuntimeDataDir "uninstall-preserve-sentinel.txt"
    New-Item -ItemType Directory -Force -Path $RuntimeDataDir | Out-Null
    Set-Content -LiteralPath $sentinelPath -Value "preserve across silent uninstall" -Encoding UTF8

    $result = [ordered]@{
        attempted = $false
        verified = $false
        uninstallerPath = $null
        installArtifactsRemoved = $false
        dataDirPreserved = $false
        dataSentinelPath = $sentinelPath
        remainingInstallArtifacts = @()
    }

    $uninstaller = Resolve-InstalledUninstaller -Root $InstallRoot
    if (-not $uninstaller) {
        if ($Strict) {
            throw "Silent uninstall verification failed: no uninstaller was found under $InstallRoot."
        }
        Write-Warning "No uninstaller was found under $InstallRoot."
        return [pscustomobject]$result
    }

    $result.attempted = $true
    $result.uninstallerPath = $uninstaller
    try {
        Invoke-ProcessChecked -FilePath $uninstaller -ArgumentList @("/S") -Label "Silent uninstaller"
    } catch {
        if ($Strict) {
            throw
        }
        Write-Warning $_
    }

    $remaining = @(Wait-InstallArtifactsRemoved -Root $InstallRoot)
    $result.remainingInstallArtifacts = $remaining
    $result.installArtifactsRemoved = ($remaining.Count -eq 0)
    $result.dataDirPreserved = Test-Path -LiteralPath $sentinelPath -PathType Leaf

    if ($Strict -and -not $result.installArtifactsRemoved) {
        throw "Silent uninstall verification failed: install artifacts remain under ${InstallRoot}: $($remaining -join ', ')"
    }
    if ($Strict -and -not $result.dataDirPreserved) {
        throw "Silent uninstall verification failed: runtime data sentinel was removed: $sentinelPath"
    }

    $result.verified = ($result.attempted -and $result.installArtifactsRemoved -and $result.dataDirPreserved)
    return [pscustomobject]$result
}

function Remove-InstallerSmokeArtifacts {
    param(
        [string]$InstallRoot,
        [string]$RuntimeDataDir,
        [string]$TempRoot
    )

    if (Test-Path -LiteralPath $InstallRoot -PathType Container) {
        Stop-ProcessesUnderPath -Root $InstallRoot -Label "installer-smoke"
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $RuntimeDataDir) {
        Remove-Item -LiteralPath $RuntimeDataDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $TempRoot -PathType Container -ErrorAction SilentlyContinue) {
        $remaining = @(Get-ChildItem -LiteralPath $TempRoot -Force -ErrorAction SilentlyContinue)
        if ($remaining.Count -eq 0) {
            Remove-Item -LiteralPath $TempRoot -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-InstalledDesktopSmoke {
    param(
        [string]$AppExe,
        [string]$RuntimeDataDir
    )

    $desktopSmokeOutputPath = Join-Path $RuntimeDataDir "installed-desktop-smoke.json"
    $smokeArgs = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        (Join-Path $RepoRoot "scripts\smoke_tauri_desktop.ps1"),
        "-RepoRoot",
        $RepoRoot,
        "-ExePath",
        $AppExe,
        "-DataDir",
        $RuntimeDataDir,
        "-OutputPath",
        $desktopSmokeOutputPath,
        "-DisableDevFallback"
    )
    if ($SimulateBackendCrash) {
        $smokeArgs += "-SimulateBackendCrash"
    }
    if ($SimulateBackendShutdown) {
        $smokeArgs += "-SimulateBackendShutdown"
    }
    if ($AttachExternalBackend) {
        $smokeArgs += "-AttachExternalBackend"
    }
    if ($SimulateBackendStartupTimeout) {
        $smokeArgs += "-SimulateBackendStartupTimeout"
    }
    if ($VerifyGlobalHotkeyRegistration) {
        $smokeArgs += "-VerifyGlobalHotkeyRegistration"
    }
    if ($SimulateGlobalHotkey) {
        $smokeArgs += "-SimulateGlobalHotkey"
    }
    if ($WaitForManualGlobalHotkey) {
        $smokeArgs += "-WaitForManualGlobalHotkey"
    }
    if ($VerifyGlobalHotkeyRegistration -or $SimulateGlobalHotkey -or $WaitForManualGlobalHotkey) {
        $smokeArgs += @("-GlobalHotkeySmokeHotkey", $GlobalHotkeySmokeHotkey)
        $smokeArgs += @("-GlobalHotkeyDispatchTimeoutSec", $GlobalHotkeyDispatchTimeoutSec.ToString())
    }
    if ($VerifySupportBundle) {
        $smokeArgs += "-VerifySupportBundle"
    }
    if ($VerifyFrontend) {
        $smokeArgs += "-VerifyFrontend"
    }
    if ($VerifyRealMediaWorkflows) {
        $smokeArgs += "-VerifyRealMediaWorkflows"
        $smokeArgs += @("-RealWorkflowYoutubeUrl", $RealWorkflowYoutubeUrl)
        $smokeArgs += @("-RealWorkflowFileTimeoutSec", $RealWorkflowFileTimeoutSec.ToString())
        $smokeArgs += @("-RealWorkflowYoutubeTimeoutSec", $RealWorkflowYoutubeTimeoutSec.ToString())
        $smokeArgs += @("-RealWorkflowPollSec", $RealWorkflowPollSec.ToString())
        if ($RealWorkflowSkipFile) {
            $smokeArgs += "-RealWorkflowSkipFile"
        }
        if ($RealWorkflowSkipYoutube) {
            $smokeArgs += "-RealWorkflowSkipYoutube"
        }
        if ($RealWorkflowNoSummary) {
            $smokeArgs += "-RealWorkflowNoSummary"
        }
    }
    if ($OccupyDefaultPort) {
        $smokeArgs += "-OccupyDefaultPort"
    }
    if ($StabilityDurationSec -gt 0) {
        $smokeArgs += @("-StabilityDurationSec", $StabilityDurationSec.ToString())
        $smokeArgs += @("-StabilityProbeIntervalSec", $StabilityProbeIntervalSec.ToString())
        if ($MaxBackendWorkingSetGrowthMB -gt 0) {
            $smokeArgs += @("-MaxBackendWorkingSetGrowthMB", $MaxBackendWorkingSetGrowthMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
        }
        if ($MaxIdleCpuPercent -gt 0) {
            $smokeArgs += @("-MaxIdleCpuPercent", $MaxIdleCpuPercent.ToString([System.Globalization.CultureInfo]::InvariantCulture))
        }
    }
    if ($LiveRecordingDurationSec -gt 0) {
        $smokeArgs += @("-LiveRecordingDurationSec", $LiveRecordingDurationSec.ToString())
        $smokeArgs += @("-LiveRecordingProbeIntervalSec", $LiveRecordingProbeIntervalSec.ToString())
        $smokeArgs += @("-LiveRecordingStartTimeoutSec", $LiveRecordingStartTimeoutSec.ToString())
        $smokeArgs += @("-LiveRecordingStopTimeoutSec", $LiveRecordingStopTimeoutSec.ToString())
        if ($LiveRecordingEnvFile) {
            $smokeArgs += @("-LiveRecordingEnvFile", $LiveRecordingEnvFile)
        }
        if ($LiveRecordingDefaultStt) {
            $smokeArgs += @("-LiveRecordingDefaultStt", $LiveRecordingDefaultStt)
        }
        if ($LiveRecordingSonioxMode) {
            $smokeArgs += @("-LiveRecordingSonioxMode", $LiveRecordingSonioxMode)
        }
        if ($DisableLiveTextInjection) {
            $smokeArgs += "-DisableLiveTextInjection"
        }
        if ($LiveRecordingAudioEngine) {
            $smokeArgs += @("-LiveRecordingAudioEngine", $LiveRecordingAudioEngine)
        }
        if ($LiveRecordingRustAudioCaptureMode) {
            $smokeArgs += @("-LiveRecordingRustAudioCaptureMode", $LiveRecordingRustAudioCaptureMode)
        }
        if ($LiveRecordingMicAlwaysOn) {
            $smokeArgs += "-LiveRecordingMicAlwaysOn"
        }
        if ($MaxLiveBackendWorkingSetGrowthMB -gt 0) {
            $smokeArgs += @("-MaxLiveBackendWorkingSetGrowthMB", $MaxLiveBackendWorkingSetGrowthMB.ToString([System.Globalization.CultureInfo]::InvariantCulture))
        }
        if ($MaxLiveCpuPercent -gt 0) {
            $smokeArgs += @("-MaxLiveCpuPercent", $MaxLiveCpuPercent.ToString([System.Globalization.CultureInfo]::InvariantCulture))
        }
    }
    if ($LegacyDataDir) {
        $smokeArgs += @("-LegacyDataDir", $LegacyDataDir)
    }
    if ($VerifyLegacyDataMigration) {
        $smokeArgs += "-VerifyLegacyDataMigration"
    }

    $smokeJson = powershell @smokeArgs
    $smokeExitCode = $LASTEXITCODE
    $smokeReport = $null
    if (Test-Path -LiteralPath $desktopSmokeOutputPath -PathType Leaf) {
        $smokeReport = Get-Content -LiteralPath $desktopSmokeOutputPath -Raw | ConvertFrom-Json
    } elseif ($smokeJson) {
        $smokeReport = $smokeJson | ConvertFrom-Json
    }
    if ($smokeExitCode -ne 0 -and -not $smokeReport) {
        throw "Installed app smoke test failed."
    }
    return $smokeReport
}

function Invoke-InstalledMediaPreparationSmoke {
    param(
        [string]$InstallRoot,
        [string]$RuntimeDataDir
    )

    $mediaToolsDir = Resolve-InstalledMediaToolsDir -Root $InstallRoot
    $outputPath = Join-Path $RuntimeDataDir "installed-media-preparation-smoke.json"
    New-Item -ItemType Directory -Force -Path (Split-Path $outputPath) | Out-Null

    $mediaSmokeArgs = @(
        "scripts\smoke_media_preparation.py",
        "--output",
        $outputPath,
        "--media-tools-dir",
        $mediaToolsDir
    )
    if (-not $AllowMissingFfprobeForMediaPreparation) {
        $mediaSmokeArgs += "--require-ffprobe"
    }

    Push-Location $RepoRoot
    try {
        python @mediaSmokeArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Installed media preparation smoke failed with exit code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
    }

    if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
        throw "Installed media preparation smoke did not write output: $outputPath"
    }
    $report = Get-Content -LiteralPath $outputPath -Raw | ConvertFrom-Json
    if (-not $report.ok) {
        throw "Installed media preparation smoke wrote ok=false: $outputPath"
    }

    return [pscustomobject]@{
        verified = $true
        mediaToolsDir = $mediaToolsDir
        requireFfprobe = -not [bool]$AllowMissingFfprobeForMediaPreparation
        report = $report
    }
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
if ($VerifyUninstall -and $KeepInstalled) {
    throw "-VerifyUninstall cannot be combined with -KeepInstalled."
}
if (-not $InstallerPath) {
    $InstallerPath = Join-Path $RepoRoot "Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.2.0_x64-setup.exe"
}
if (-not (Test-Path $InstallerPath)) {
    throw "Missing installer: $InstallerPath"
}
$InstallerPath = (Resolve-Path $InstallerPath).Path
if ($LegacyDataDir) {
    if (-not (Test-Path -LiteralPath $LegacyDataDir -PathType Container)) {
        throw "Missing LegacyDataDir: $LegacyDataDir"
    }
    $LegacyDataDir = (Resolve-Path $LegacyDataDir).Path
} elseif ($VerifyLegacyDataMigration) {
    throw "-VerifyLegacyDataMigration requires -LegacyDataDir."
}

$tmpRoot = Join-Path $RepoRoot "tmp\installer-smoke"
Assert-UnderRoot -Root (Join-Path $RepoRoot "tmp") -Path $tmpRoot -Label "Installer smoke temp root"
if (-not $InstallDir) {
    $InstallDir = Join-Path $tmpRoot "Scriber"
}
if (-not $DataDir) {
    $DataDir = Join-Path $tmpRoot ("data-" + [System.Guid]::NewGuid().ToString("N"))
}
$InstallDir = Convert-ToFullPath -Path $InstallDir
$DataDir = Convert-ToFullPath -Path $DataDir
Assert-UnderRoot -Root $tmpRoot -Path $InstallDir -Label "InstallDir"
Assert-UnderRoot -Root $tmpRoot -Path $DataDir -Label "DataDir"

if (Test-Path $InstallDir) {
    Stop-ProcessesUnderPath -Root $InstallDir -Label "installer-smoke"
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $tmpRoot | Out-Null

$smoke = $null
$upgrade = $null
$mediaPreparation = $null
$audioSidecarExe = $null
$frontendAssetOwnership = $null
$cleanupCompleted = $false
try {
    Invoke-ProcessChecked -FilePath $InstallerPath -ArgumentList @("/S", "/D=$InstallDir") -Label "Silent installer"
    $appExe = Resolve-InstalledAppExe -Root $InstallDir
    $audioSidecarExe = Resolve-InstalledAudioSidecarExe -Root $InstallDir
    $frontendAssetOwnership = Test-InstalledFrontendAssetOwnership -Root $InstallDir
    $installSize = Get-DirectorySizeReport -Root $InstallDir -MaxSizeMB $MaxInstalledSizeMB

    $smoke = Invoke-InstalledDesktopSmoke -AppExe $appExe -RuntimeDataDir $DataDir

    if ($SimulateUpgrade) {
        $sentinelPath = Join-Path $DataDir "upgrade-sentinel.txt"
        Set-Content -LiteralPath $sentinelPath -Value "preserve across installer rerun" -Encoding UTF8

        Invoke-ProcessChecked -FilePath $InstallerPath -ArgumentList @("/S", "/D=$InstallDir") -Label "Silent installer upgrade"
        $appExe = Resolve-InstalledAppExe -Root $InstallDir
        $audioSidecarExe = Resolve-InstalledAudioSidecarExe -Root $InstallDir
        $frontendAssetOwnership = Test-InstalledFrontendAssetOwnership -Root $InstallDir
        $installSize = Get-DirectorySizeReport -Root $InstallDir -MaxSizeMB $MaxInstalledSizeMB
        $secondSmoke = Invoke-InstalledDesktopSmoke -AppExe $appExe -RuntimeDataDir $DataDir
        if (-not (Test-Path -LiteralPath $sentinelPath -PathType Leaf)) {
            throw "Installer upgrade smoke did not preserve existing data sentinel: $sentinelPath"
        }

        $upgrade = [pscustomobject]@{
            verified = $true
            sentinelPreserved = $true
            secondRuntimeMode = $secondSmoke.runtimeMode
            secondLaunchKind = $secondSmoke.launchKind
            secondCleanupVerified = $secondSmoke.cleanupVerified
            externalAttach = $secondSmoke.externalAttach
            portConflict = $secondSmoke.portConflict
            controlledShutdown = $secondSmoke.controlledShutdown
            startupTimeout = $secondSmoke.startupTimeout
            globalHotkey = $secondSmoke.globalHotkey
            supportBundle = $secondSmoke.supportBundle
            frontend = $secondSmoke.frontend
            frontendAssetOwnership = $frontendAssetOwnership
            liveRecording = $secondSmoke.liveRecording
            stability = $secondSmoke.stability
            legacyDataMigration = $secondSmoke.legacyDataMigration
            installSize = $installSize
        }
        $smoke = $secondSmoke
    }

    if ($VerifyMediaPreparation) {
        $mediaPreparation = Invoke-InstalledMediaPreparationSmoke -InstallRoot $InstallDir -RuntimeDataDir $DataDir
    }
    $smokeOk = [bool]($smoke -and ($smoke.ok -ne $false))
    $desktopSmokeFailure = if (-not $smokeOk) { $smoke } else { $null }

    $result = [ordered]@{
        ok = $smokeOk
        installer = $InstallerPath
        installDir = $InstallDir
        appExe = $appExe
        audioSidecarExe = $audioSidecarExe
        dataDir = $DataDir
        installSize = $installSize
        appPid = $smoke.appPid
        backendPid = $smoke.backendPid
        backendPort = $smoke.backendPort
        runtimeMode = $smoke.runtimeMode
        apiVersion = $smoke.apiVersion
        ready = $smoke.ready
        launchKind = $smoke.launchKind
        externalAttach = $smoke.externalAttach
        portConflict = $smoke.portConflict
        legacyDataMigration = $smoke.legacyDataMigration
        upgrade = $upgrade
        crashRecovery = $smoke.crashRecovery
        controlledShutdown = $smoke.controlledShutdown
        startupTimeout = $smoke.startupTimeout
        globalHotkey = $smoke.globalHotkey
        supportBundle = $smoke.supportBundle
        frontend = $smoke.frontend
        frontendAssetOwnership = $frontendAssetOwnership
        realMediaWorkflows = $smoke.realMediaWorkflows
        mediaPreparation = $mediaPreparation
        liveRecording = $smoke.liveRecording
        stability = $smoke.stability
        failureDiagnostics = $smoke.failureDiagnostics
        desktopSmokeFailure = $desktopSmokeFailure
        cleanupVerified = $smoke.cleanupVerified
        uninstall = $null
    }

    if ($KeepInstalled) {
        $result.uninstall = [pscustomobject]@{
            attempted = $false
            verified = $false
            reason = "KeepInstalled"
        }
    } else {
        $result.uninstall = Invoke-InstalledUninstallCheck -InstallRoot $InstallDir -RuntimeDataDir $DataDir -Strict:$VerifyUninstall
        Remove-InstallerSmokeArtifacts -InstallRoot $InstallDir -RuntimeDataDir $DataDir -TempRoot $tmpRoot
        $cleanupCompleted = $true
    }

    Write-SmokeJson -Payload ([pscustomobject]$result) -Path $OutputPath -Root $RepoRoot
    if (-not $result.ok) {
        throw "Installed app smoke test failed."
    }
} finally {
    if (-not $KeepInstalled -and -not $cleanupCompleted) {
        $uninstaller = Resolve-InstalledUninstaller -Root $InstallDir
        if ($uninstaller) {
            try {
                Invoke-ProcessChecked -FilePath $uninstaller -ArgumentList @("/S") -Label "Silent uninstaller"
            } catch {
                Write-Warning $_
            }
        }
        Remove-InstallerSmokeArtifacts -InstallRoot $InstallDir -RuntimeDataDir $DataDir -TempRoot $tmpRoot
    }
}
