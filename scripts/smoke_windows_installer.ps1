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
preserved.
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$InstallerPath = "",
    [string]$InstallDir = "",
    [string]$DataDir = "",
    [switch]$OccupyDefaultPort,
    [switch]$SimulateBackendCrash,
    [switch]$SimulateBackendShutdown,
    [switch]$AttachExternalBackend,
    [switch]$SimulateBackendStartupTimeout,
    [string]$LegacyDataDir = "",
    [switch]$VerifyLegacyDataMigration,
    [switch]$SimulateUpgrade,
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
    if ($OccupyDefaultPort) {
        $smokeArgs += "-OccupyDefaultPort"
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
            legacyDataMigration = $secondSmoke.legacyDataMigration
        }
        $smoke = $secondSmoke
    }

    [pscustomobject]@{
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
        cleanupVerified = $smoke.cleanupVerified
    } | ConvertTo-Json -Compress -Depth 8
} finally {
    if (-not $KeepInstalled) {
        $uninstaller = Get-ChildItem -LiteralPath $InstallDir -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -in @("uninstall.exe", "Uninstall.exe") -or $_.Name -match "^unins.*\.exe$" } |
            Select-Object -First 1
        if ($uninstaller) {
            try {
                Invoke-ProcessChecked -FilePath $uninstaller.FullName -ArgumentList @("/S") -Label "Silent uninstaller"
            } catch {
                Write-Warning $_
            }
        }
        if (Test-Path $InstallDir) {
            Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path $DataDir) {
            Remove-Item -LiteralPath $DataDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path $tmpRoot -PathType Container -ErrorAction SilentlyContinue) {
            $remaining = @(Get-ChildItem -LiteralPath $tmpRoot -Force -ErrorAction SilentlyContinue)
            if ($remaining.Count -eq 0) {
                Remove-Item -LiteralPath $tmpRoot -Force -ErrorAction SilentlyContinue
            }
        }
    }
}
