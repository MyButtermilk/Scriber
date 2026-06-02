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
        Set-Content -LiteralPath $outputFull -Value $json -Encoding UTF8
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
        $smokeArgs += @("-GlobalHotkeySmokeHotkey", $GlobalHotkeySmokeHotkey)
        $smokeArgs += @("-GlobalHotkeyDispatchTimeoutSec", $GlobalHotkeyDispatchTimeoutSec.ToString())
    }
    if ($SimulateGlobalHotkey) {
        $smokeArgs += "-SimulateGlobalHotkey"
        $smokeArgs += @("-GlobalHotkeySmokeHotkey", $GlobalHotkeySmokeHotkey)
        $smokeArgs += @("-GlobalHotkeyDispatchTimeoutSec", $GlobalHotkeyDispatchTimeoutSec.ToString())
    }
    if ($WaitForManualGlobalHotkey) {
        $smokeArgs += "-WaitForManualGlobalHotkey"
        $smokeArgs += @("-GlobalHotkeySmokeHotkey", $GlobalHotkeySmokeHotkey)
        $smokeArgs += @("-GlobalHotkeyDispatchTimeoutSec", $GlobalHotkeyDispatchTimeoutSec.ToString())
    }
    if ($VerifySupportBundle) {
        $smokeArgs += "-VerifySupportBundle"
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
    if ($LASTEXITCODE -ne 0) {
        throw "Installed app smoke test failed."
    }
    return ($smokeJson | ConvertFrom-Json)
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
if ($VerifyUninstall -and $KeepInstalled) {
    throw "-VerifyUninstall cannot be combined with -KeepInstalled."
}
if (-not $InstallerPath) {
    $InstallerPath = Join-Path $RepoRoot "Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe"
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
$cleanupCompleted = $false
try {
    Invoke-ProcessChecked -FilePath $InstallerPath -ArgumentList @("/S", "/D=$InstallDir") -Label "Silent installer"
    $appExe = Resolve-InstalledAppExe -Root $InstallDir

    $smoke = Invoke-InstalledDesktopSmoke -AppExe $appExe -RuntimeDataDir $DataDir

    if ($SimulateUpgrade) {
        $sentinelPath = Join-Path $DataDir "upgrade-sentinel.txt"
        Set-Content -LiteralPath $sentinelPath -Value "preserve across installer rerun" -Encoding UTF8

        Invoke-ProcessChecked -FilePath $InstallerPath -ArgumentList @("/S", "/D=$InstallDir") -Label "Silent installer upgrade"
        $appExe = Resolve-InstalledAppExe -Root $InstallDir
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
            liveRecording = $secondSmoke.liveRecording
            stability = $secondSmoke.stability
            legacyDataMigration = $secondSmoke.legacyDataMigration
        }
        $smoke = $secondSmoke
    }

    $result = [ordered]@{
        ok = $true
        installer = $InstallerPath
        installDir = $InstallDir
        appExe = $appExe
        dataDir = $DataDir
        runtimeMode = $smoke.runtimeMode
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
        liveRecording = $smoke.liveRecording
        stability = $smoke.stability
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
