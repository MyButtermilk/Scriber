<#
.SYNOPSIS
Builds and evaluates one immutable installer-size AutoResearch packet.

.DESCRIPTION
This is the only product-producing entry point for the installer-size profile.
All inputs except RunId, the fixed mode, and the fixed timing switch come from
the immutable packet and run state.  It never signs, publishes, or configures
an updater.  Every scratch path is derived below the canonical run namespace or
the installer-smoke scratch namespace.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [ValidateSet("baseline-1", "candidate", "final-1", "final-2")]
    [string]$Mode,

    [switch]$RunTiming
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$ResearchNamespace = Join-Path $RepoRoot "autoresearch-results\installer-size"
$InstallerSmokeNamespace = Join-Path $RepoRoot "tmp\installer-smoke"
$ComponentMap = Join-Path $RepoRoot "packaging\installer-component-map-v1.json"
$ProfileConfig = Join-Path $RepoRoot "scripts\perf\profiles\installer-size\config.json"
$SigningEnvironmentNames = @(
    "TAURI_SIGNING_PRIVATE_KEY",
    "TAURI_SIGNING_PRIVATE_KEY_PATH",
    "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
    "SCRIBER_TAURI_UPDATER_PUBLIC_KEY",
    "SCRIBER_TAURI_UPDATER_ENDPOINT",
    "SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE",
    "SCRIBER_AUTHENTICODE_PUBLISHER",
    "SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP",
    "CSC_LINK",
    "CSC_KEY_PASSWORD"
)

function Resolve-CanonicalRunId {
    param([Parameter(Mandatory = $true)][string]$Value)

    $parsed = [guid]::Empty
    if (-not [guid]::TryParseExact($Value, "D", [ref]$parsed)) {
        throw "invalid_run_id"
    }
    $canonical = $parsed.ToString("D")
    if (
        $canonical -ne $Value -or
        $parsed -eq [guid]::Empty -or
        $canonical[19] -notin @('8', '9', 'a', 'b')
    ) {
        throw "invalid_run_id"
    }
    return $canonical
}

function Assert-SafePacketId {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$') {
        throw "invalid_packet_id"
    }
    return $Value
}

function Convert-ToFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Assert-StrictDescendant {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Code
    )

    $rootFull = Convert-ToFullPath -Path $Root
    $pathFull = Convert-ToFullPath -Path $Path
    $prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $pathFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw $Code
    }
    return $pathFull
}

function Assert-NoReparsePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Code,
        [switch]$Recurse
    )

    $rootFull = Convert-ToFullPath -Path $Root
    $pathFull = Assert-StrictDescendant -Root $rootFull -Path $Path -Code $Code
    if (Test-Path -LiteralPath $rootFull) {
        $rootItem = Get-Item -LiteralPath $rootFull -Force
        if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw $Code
        }
    }
    $relative = $pathFull.Substring($rootFull.Length).TrimStart('\', '/')
    $current = $rootFull
    $separators = [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    foreach ($part in $relative.Split($separators, [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        if (-not (Test-Path -LiteralPath $current)) {
            break
        }
        $item = Get-Item -LiteralPath $current -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw $Code
        }
    }
    if ($Recurse -and (Test-Path -LiteralPath $pathFull -PathType Container)) {
        $reparse = Get-ChildItem -LiteralPath $pathFull -Recurse -Force -ErrorAction Stop |
            Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 } |
            Select-Object -First 1
        if ($reparse) {
            throw $Code
        }
    }
    return $pathFull
}

function Get-Sha256File {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-Sha256Text {
    param([Parameter(Mandatory = $true)][string]$Value)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function Get-BuildRootIdentitySha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    # Matches os.path.normcase(Path.resolve()) in installer_research.inventory
    # on the Windows-only product runtime without persisting the local path.
    $normalized = (Convert-ToFullPath -Path $Path).ToLowerInvariant().Replace('\', '/').TrimEnd('/')
    return Get-Sha256Text -Value $normalized
}

function Read-JsonObject {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Code
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw $Code
    }
    try {
        $value = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    } catch {
        throw $Code
    }
    if ($null -eq $value) {
        throw $Code
    }
    return $value
}

function Write-JsonAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Payload
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = "$Path.$PID.tmp"
    $encoding = [System.Text.UTF8Encoding]::new($false)
    try {
        $json = $Payload | ConvertTo-Json -Depth 20
        [System.IO.File]::WriteAllText($temporary, $json + "`n", $encoding)
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Invoke-CapturedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][scriptblock]$Command,
        [switch]$PowerShellScript
    )

    $parent = Split-Path -Parent $LogPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $exitCode = 0
    try {
        # A successful PowerShell-only command must not inherit a stale native
        # exit code from an earlier tool invocation in this process.
        $global:LASTEXITCODE = $null
        # Redirect only stdout. Windows PowerShell 5.1 turns redirected native
        # stderr into PowerShell ErrorRecords, which can make a nested script
        # using ErrorActionPreference=Stop fail even when the process exits 0.
        # The parent dispatcher drains and bounds stderr independently.
        & $Command > $LogPath
        $commandSucceeded = $?
        $nativeExitCode = $LASTEXITCODE
        # A successful allowlisted .ps1 orchestrator can intentionally handle a
        # native failure and leave that historical code behind. Its
        # ErrorActionPreference=Stop contract plus the caller's subsequent
        # artifact/evidence validation are authoritative. Direct native
        # commands retain exact exit codes and must not use this switch.
        if ($PowerShellScript) {
            # Reaching this point means the allowlisted script returned without
            # a terminating error. Windows PowerShell 5.1 can still expose a
            # false `$?` from an internal, already-handled native invocation;
            # the caller must validate the script's immutable evidence next.
            $exitCode = 0
        } elseif ($null -ne $nativeExitCode) {
            $exitCode = [int]$nativeExitCode
        } elseif (-not $commandSucceeded) {
            $exitCode = 1
        }
    } catch {
        $exitCode = 2
        $safeType = $_.Exception.GetType().Name
        [System.IO.File]::WriteAllText(
            $LogPath,
            "captured-command-error:$safeType`n",
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    $sha = if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
        Get-Sha256File -Path $LogPath
    } else {
        Get-Sha256Text -Value "empty-captured-command"
    }
    return [ordered]@{ exitCode = $exitCode; evidenceSha256 = $sha }
}

function Remove-ScopedTree {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Code
    )

    $verifiedRoot = Assert-NoReparsePath -Root $RepoRoot -Path $Root -Code $Code
    $verified = Assert-NoReparsePath -Root $verifiedRoot -Path $Path -Code $Code -Recurse
    if (Test-Path -LiteralPath $verified) {
        Remove-Item -LiteralPath $verified -Recurse -Force -ErrorAction Stop
    }
    if (Test-Path -LiteralPath $verified) {
        throw $Code
    }
}

function Get-ExactInstalledProcesses {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) {
        return @()
    }
    $prefix = (Convert-ToFullPath -Path $InstallRoot) + [System.IO.Path]::DirectorySeparatorChar
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $exe = [string]$_.ExecutablePath
                $exe -and $exe.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
            }
    )
}

function Test-IsScriberUpdaterCommand {
    param([string]$CommandLine)

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    return (
        $CommandLine -match '(?i)Scriber' -and
        $CommandLine -match '(?i)(?:^|\s)/(?:UPDATE|P|R|ARGS)(?:\s|$)'
    )
}

function Get-RelatedInstallerProcesses {
    param(
        [Parameter(Mandatory = $true)][System.Collections.Generic.HashSet[int]]$KnownProcessIds,
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string[]]$InstallerPaths
    )

    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($process in $processes) {
            $processId = [int]$process.ProcessId
            $parentProcessId = [int]$process.ParentProcessId
            if (
                $processId -ne $PID -and
                $KnownProcessIds.Contains($parentProcessId) -and
                -not $KnownProcessIds.Contains($processId)
            ) {
                [void]$KnownProcessIds.Add($processId)
                $changed = $true
            }
        }
    }

    $targetNeedle = (Convert-ToFullPath -Path $TargetRoot).ToLowerInvariant()
    $installerPathSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($installerPath in $InstallerPaths) {
        if (-not [string]::IsNullOrWhiteSpace($installerPath)) {
            [void]$installerPathSet.Add([System.IO.Path]::GetFullPath($installerPath))
        }
    }

    return @(
        $processes | Where-Object {
            $processId = [int]$_.ProcessId
            if ($processId -eq $PID) {
                return $false
            }
            $commandLine = [string]$_.CommandLine
            $executablePath = [string]$_.ExecutablePath
            $exactInstaller = $false
            if (-not [string]::IsNullOrWhiteSpace($executablePath)) {
                try {
                    $exactInstaller = $installerPathSet.Contains([System.IO.Path]::GetFullPath($executablePath))
                } catch {
                    $exactInstaller = $false
                }
            }
            if (-not $exactInstaller -and -not [string]::IsNullOrWhiteSpace($commandLine)) {
                foreach ($installerPath in $installerPathSet) {
                    if ($commandLine.IndexOf($installerPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                        $exactInstaller = $true
                        break
                    }
                }
            }
            $executableUnderTarget = (
                -not [string]::IsNullOrWhiteSpace($executablePath) -and
                $executablePath.ToLowerInvariant().StartsWith($targetNeedle + '\')
            )
            $commandTargetsRoot = (
                -not [string]::IsNullOrWhiteSpace($commandLine) -and
                $commandLine.ToLowerInvariant().Contains($targetNeedle)
            )
            $scopedUpdater = (
                $commandTargetsRoot -and
                (Test-IsScriberUpdaterCommand -CommandLine $commandLine)
            )
            return (
                $KnownProcessIds.Contains($processId) -or
                $executableUnderTarget -or
                $commandTargetsRoot -or
                $scopedUpdater -or
                $exactInstaller
            )
        }
    )
}

function Get-RemainingDeadlineMilliseconds {
    param([Parameter(Mandatory = $true)][DateTimeOffset]$Deadline)

    $remaining = [Math]::Ceiling(($Deadline - [DateTimeOffset]::UtcNow).TotalMilliseconds)
    if ($remaining -le 0) {
        return 0
    }
    return [int][Math]::Min([double][int]::MaxValue, [double]$remaining)
}

function Stop-ScopedProcessTree {
    param(
        [Parameter(Mandatory = $true)][System.Collections.Generic.HashSet[int]]$KnownProcessIds,
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string[]]$InstallerPaths
    )

    $related = @(
        Get-RelatedInstallerProcesses `
            -KnownProcessIds $KnownProcessIds `
            -TargetRoot $TargetRoot `
            -InstallerPaths $InstallerPaths |
            Sort-Object { [int]$_.ProcessId } -Descending
    )
    foreach ($process in $related) {
        $processId = [int]$process.ProcessId
        if ($processId -ne $PID) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Wait-ScopedProcessTreeExit {
    param(
        [Parameter(Mandatory = $true)][System.Collections.Generic.HashSet[int]]$KnownProcessIds,
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string[]]$InstallerPaths,
        [Parameter(Mandatory = $true)][DateTimeOffset]$Deadline
    )

    while ([DateTimeOffset]::UtcNow -lt $Deadline) {
        $related = @(
            Get-RelatedInstallerProcesses `
                -KnownProcessIds $KnownProcessIds `
                -TargetRoot $TargetRoot `
                -InstallerPaths $InstallerPaths
        )
        if ($related.Count -eq 0) {
            return
        }
        # Re-issue the bounded force-stop for a child discovered after the
        # initial process-tree snapshot.
        foreach ($process in ($related | Sort-Object { [int]$_.ProcessId } -Descending)) {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
        $remainingMs = Get-RemainingDeadlineMilliseconds -Deadline $Deadline
        if ($remainingMs -le 0) {
            break
        }
        Start-Sleep -Milliseconds ([Math]::Min(250, $remainingMs))
    }
    throw "upgrade_scoped_process_cleanup_timeout"
}

function Invoke-ScopedInstallerProcessCleanup {
    param(
        [Parameter(Mandatory = $true)][System.Collections.Generic.HashSet[int]]$KnownProcessIds,
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string[]]$InstallerPaths,
        [ValidateRange(1, 60)][int]$TimeoutSec = 10
    )

    Stop-ScopedProcessTree `
        -KnownProcessIds $KnownProcessIds `
        -TargetRoot $TargetRoot `
        -InstallerPaths $InstallerPaths
    Wait-ScopedProcessTreeExit `
        -KnownProcessIds $KnownProcessIds `
        -TargetRoot $TargetRoot `
        -InstallerPaths $InstallerPaths `
        -Deadline ([DateTimeOffset]::UtcNow.AddSeconds($TimeoutSec))
}

function Get-ExclusiveInstalledIdentity {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::None
    )
    try {
        $length = [int64]$stream.Length
        $algorithm = [System.Security.Cryptography.SHA256]::Create()
        try {
            $sha256 = ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
        } finally {
            $algorithm.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
    return [ordered]@{
        length = $length
        sha256 = $sha256
        key = "$length|$sha256"
    }
}

function Get-CandidateInstalledAppIdentity {
    param([Parameter(Mandatory = $true)][object]$Inventory)

    $matches = @(
        @($Inventory.payload.installed.files) | Where-Object {
            [string]$_.path -in @("Scriber.exe", "scriber-desktop.exe")
        }
    )
    if (
        $matches.Count -ne 1 -or
        [int64]$matches[0].length -le 0 -or
        [string]$matches[0].sha256 -notmatch '^[0-9a-f]{64}$'
    ) {
        throw "candidate_inventory_app_identity_invalid"
    }
    return $matches[0]
}

function Wait-CandidateUpgradeInstallStable {
    param(
        [Parameter(Mandatory = $true)][System.Collections.Generic.HashSet[int]]$KnownProcessIds,
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string[]]$InstallerPaths,
        [Parameter(Mandatory = $true)][object]$CandidateInventory,
        [Parameter(Mandatory = $true)][DateTimeOffset]$Deadline,
        [ValidateRange(2, 10)][int]$RequiredStableSamples = 3,
        [ValidateRange(50, 2000)][int]$ObservationIntervalMs = 250
    )

    $expectedIdentity = Get-CandidateInstalledAppIdentity -Inventory $CandidateInventory
    $stableCount = 0
    $lastIdentityKey = ""
    while ([DateTimeOffset]::UtcNow -lt $Deadline) {
        $related = @(
            Get-RelatedInstallerProcesses `
                -KnownProcessIds $KnownProcessIds `
                -TargetRoot $InstallRoot `
                -InstallerPaths $InstallerPaths
        )
        if ($related.Count -eq 0) {
            try {
                $installedExe = Resolve-InstalledAppExecutable -InstallRoot $InstallRoot
                $identity = Get-ExclusiveInstalledIdentity -Path $installedExe
                if (
                    [int64]$identity.length -eq [int64]$expectedIdentity.length -and
                    [string]$identity.sha256 -eq [string]$expectedIdentity.sha256
                ) {
                    # The desktop EXE can be byte-identical across a backend-only
                    # optimization. Treat it only as a cheap readiness prefilter:
                    # every stable sample must prove the complete candidate tree.
                    Assert-InstalledPayloadMatchesInventory `
                        -InstallRoot $InstallRoot `
                        -Inventory $CandidateInventory
                    if ([DateTimeOffset]::UtcNow -ge $Deadline) {
                        break
                    }
                    if ([string]$identity.key -eq $lastIdentityKey) {
                        $stableCount += 1
                    } else {
                        $lastIdentityKey = [string]$identity.key
                        $stableCount = 1
                    }
                    if ($stableCount -ge $RequiredStableSamples) {
                        return
                    }
                } else {
                    $stableCount = 0
                    $lastIdentityKey = ""
                }
            } catch {
                $stableCount = 0
                $lastIdentityKey = ""
            }
        } else {
            $stableCount = 0
            $lastIdentityKey = ""
        }
        $remainingMs = Get-RemainingDeadlineMilliseconds -Deadline $Deadline
        if ($remainingMs -le 0) {
            break
        }
        Start-Sleep -Milliseconds ([Math]::Min($ObservationIntervalMs, $remainingMs))
    }
    throw "candidate_upgrade_completion_timeout"
}

function Stop-ExactInstalledProcesses {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $matches = @(Get-ExactInstalledProcesses -InstallRoot $InstallRoot)
    foreach ($process in $matches) {
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    if ($matches.Count -gt 0) {
        Start-Sleep -Seconds 2
    }
}

function Invoke-ExactUninstaller {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [switch]$RequireUninstaller,
        [ValidateRange(1, 300)][int]$TimeoutSec = 60
    )

    if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) {
        return
    }
    $verifiedSmokeNamespace = Assert-NoReparsePath -Root $RepoRoot -Path $InstallerSmokeNamespace -Code "unsafe_install_cleanup"
    $null = Assert-NoReparsePath -Root $verifiedSmokeNamespace -Path $InstallRoot -Code "unsafe_install_cleanup" -Recurse
    $uninstallers = @(
        Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -in @("uninstall.exe", "Uninstall.exe") -or $_.Name -match '^unins.*\.exe$'
            }
    )
    if ($uninstallers.Count -gt 1) {
        throw "ambiguous_uninstaller"
    }
    if ($RequireUninstaller -and $uninstallers.Count -ne 1) {
        throw "uninstaller_missing"
    }
    if ($uninstallers.Count -eq 1) {
        $uninstaller = $uninstallers[0]
        if (($uninstaller.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "unsafe_uninstaller"
        }
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSec)
        $process = $null
        try {
            $process = Start-Process `
                -FilePath $uninstaller.FullName `
                -ArgumentList @("/S") `
                -PassThru `
                -WindowStyle Hidden
            $null = $process.Handle
            $remainingMs = Get-RemainingDeadlineMilliseconds -Deadline $deadline
            if ($remainingMs -le 0 -or -not $process.WaitForExit($remainingMs)) {
                throw "uninstaller_timeout"
            }
            if ([int]$process.ExitCode -ne 0) {
                throw "uninstaller_failed"
            }
        } finally {
            if ($null -ne $process) {
                if (-not $process.HasExited) {
                    Stop-Process -Id ([int]$process.Id) -Force -ErrorAction SilentlyContinue
                }
                $process.Dispose()
            }
        }
    }
    Stop-ExactInstalledProcesses -InstallRoot $InstallRoot
}

function Get-ExactUninstallRegistryEntries {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $target = Convert-ToFullPath -Path $InstallRoot
    $registryRoot = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall"
    if (-not (Test-Path -LiteralPath $registryRoot)) {
        return @()
    }
    return @(
        foreach ($key in @(Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue)) {
            $entry = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
            if ($null -eq $entry) {
                continue
            }
            $locationProperty = $entry.PSObject.Properties["InstallLocation"]
            if ($null -eq $locationProperty) {
                continue
            }
            $location = ([string]$locationProperty.Value).Trim().Trim('"').TrimEnd('\', '/')
            if (-not $location) {
                continue
            }
            try {
                $locationFull = Convert-ToFullPath -Path $location
            } catch {
                continue
            }
            if ($locationFull.Equals($target, [System.StringComparison]::OrdinalIgnoreCase)) {
                $key
            }
        }
    )
}

function Remove-ExactUninstallRegistryEntries {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    foreach ($key in @(Get-ExactUninstallRegistryEntries -InstallRoot $InstallRoot)) {
        # The NSIS cleanup process can remove its key after enumeration but
        # before this fallback executes. Treat that disappearance as success,
        # then fail closed if an exact entry is still present.
        Remove-Item -LiteralPath $key.PSPath -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (@(Get-ExactUninstallRegistryEntries -InstallRoot $InstallRoot).Count -ne 0) {
        throw "uninstall_registry_cleanup_failed"
    }
}

function Test-NestedTrue {
    param(
        [object]$Value,
        [Parameter(Mandatory = $true)][string[]]$PropertyPath
    )

    $current = $Value
    foreach ($name in $PropertyPath) {
        if ($null -eq $current) {
            return $false
        }
        $property = $current.PSObject.Properties[$name]
        if ($null -eq $property) {
            return $false
        }
        $current = $property.Value
    }
    return $current -eq $true
}

function Resolve-InstalledAppExecutable {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $matches = @(
        foreach ($name in @("Scriber.exe", "scriber-desktop.exe")) {
            $path = Join-Path $InstallRoot $name
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                Get-Item -LiteralPath $path -Force
            }
        }
    )
    if ($matches.Count -ne 1 -or ($matches[0].Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "installed_app_executable_invalid"
    }
    return $matches[0].FullName
}

function Get-ProductVersion {
    $source = Get-Content -LiteralPath (Join-Path $RepoRoot "src\version.py") -Raw
    $match = [regex]::Match($source, '(?m)^__version__\s*=\s*"([^"]+)"')
    if (-not $match.Success -or $match.Groups[1].Value -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
        throw "invalid_product_version"
    }
    return $match.Groups[1].Value
}

function Assert-FileIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Identity,
        [Parameter(Mandatory = $true)][string]$Code
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw $Code
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        [int64]$item.Length -ne [int64]$Identity.length -or
        (Get-Sha256File -Path $item.FullName) -ne [string]$Identity.sha256
    ) {
        throw $Code
    }
    return $item.FullName
}

function Get-PlainTreeIdentity {
    param([Parameter(Mandatory = $true)][string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "tree_identity_root_missing"
    }
    $rootItem = Get-Item -LiteralPath $Root -Force
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "tree_identity_reparse_point"
    }
    $rootFull = Convert-ToFullPath -Path $rootItem.FullName
    $entries = [System.Collections.Generic.List[string]]::new()
    $fileCount = 0
    $totalBytes = [int64]0
    foreach ($item in @(Get-ChildItem -LiteralPath $rootFull -Recurse -Force -ErrorAction Stop)) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "tree_identity_reparse_point"
        }
        $relative = $item.FullName.Substring($rootFull.Length).TrimStart('\', '/').Replace('\', '/')
        if ($item.PSIsContainer) {
            $entries.Add("D|$relative")
            continue
        }
        $entries.Add("F|$relative|$([int64]$item.Length)|$(Get-Sha256File -Path $item.FullName)")
        $fileCount += 1
        $totalBytes += [int64]$item.Length
    }
    $entries.Sort([System.StringComparer]::Ordinal)
    return [ordered]@{
        fileCount = [int]$fileCount
        totalBytes = [int64]$totalBytes
        treeSha256 = Get-Sha256Text -Value ($entries -join "`n")
    }
}

function Get-PlainTreeIdentitySha256 {
    param([Parameter(Mandatory = $true)][string]$Root)
    return [string](Get-PlainTreeIdentity -Root $Root).treeSha256
}

function Assert-NsisTreeIdentity {
    param(
        [Parameter(Mandatory = $true)][object]$Manifest,
        [Parameter(Mandatory = $true)][string]$Code
    )

    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw $Code
    }
    $nsisRoot = Join-Path $env:LOCALAPPDATA "tauri\NSIS"
    $nsisRoot = Assert-NoReparsePath -Root $env:LOCALAPPDATA -Path $nsisRoot -Code $Code -Recurse
    $makensisRelativePath = ([string]$Manifest.nsis.relativePath).Replace('/', '\')
    if ($makensisRelativePath -notin @("Bin\makensis.exe", "makensis.exe")) {
        throw $Code
    }
    $null = Assert-FileIdentity `
        -Path (Join-Path $nsisRoot $makensisRelativePath) `
        -Identity $Manifest.nsis `
        -Code $Code
    $expected = $Manifest.nsisTree
    $actual = Get-PlainTreeIdentity -Root $nsisRoot
    if (
        [int]$expected.fileCount -ne [int]$actual.fileCount -or
        [int64]$expected.totalBytes -ne [int64]$actual.totalBytes -or
        [string]$expected.treeSha256 -ne [string]$actual.treeSha256
    ) {
        throw $Code
    }
    return $nsisRoot
}

function Ensure-HermeticEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$RunRoot,
        [Parameter(Mandatory = $true)][string]$SourceCommit,
        [Parameter(Mandatory = $true)][bool]$Baseline,
        [switch]$VerifyOnly
    )

    $environmentName = if ($Baseline) { "baseline" } else { "source-$($SourceCommit.Substring(0, 16))" }
    $environmentRoot = Join-Path $RunRoot "environments\$environmentName"
    $manifestPath = Join-Path $environmentRoot "environment-manifest.json"
    $pythonPath = Join-Path $environmentRoot ".venv\Scripts\python.exe"
    $requirementsRoot = if ($Baseline) { Join-Path $RunRoot "snapshots" } else { $RepoRoot }
    $requirementsBasePath = Join-Path $requirementsRoot "requirements-base.txt"
    $requirementsBuildPath = Join-Path $requirementsRoot "requirements-build.txt"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        if ($VerifyOnly) {
            throw "environment_missing_after_build"
        }
        if ($Baseline) {
            throw "baseline_environment_missing"
        }
        $basePython = Join-Path $RunRoot "environments\baseline\.venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $basePython -PathType Leaf)) {
            throw "baseline_python_missing"
        }
        $prepareLog = Join-Path $RunRoot "scratch\environment-$environmentName.log"
        $prepared = Invoke-CapturedCommand -LogPath $prepareLog -PowerShellScript -Command {
            & (Join-Path $RepoRoot "scripts\prepare_installer_research_environment.ps1") `
                -RunId $RunId `
                -BasePython $basePython `
                -EnvironmentName $environmentName
        }
        if ($prepared.exitCode -ne 0) {
            throw "candidate_environment_prepare_failed"
        }
    }
    $null = Assert-NoReparsePath -Root $RunRoot -Path $environmentRoot -Code "unsafe_environment" -Recurse
    $manifest = Read-JsonObject -Path $manifestPath -Code "invalid_environment_manifest"
    if (
        [int]$manifest.schemaVersion -ne 1 -or
        [string]$manifest.kind -ne "scriber-installer-research-python-environment" -or
        [string]$manifest.runId -ne $RunId -or
        [string]$manifest.environmentName -ne $environmentName
    ) {
        throw "environment_identity_mismatch"
    }
    $verifyLog = Join-Path $RunRoot "scratch\environment-verify-$environmentName.log"
    $verified = Invoke-CapturedCommand -LogPath $verifyLog -Command {
        & $pythonPath (Join-Path $RepoRoot "scripts\write_installer_research_environment_manifest.py") `
            --run-id $RunId `
            --environment-name $environmentName `
            --wheelhouse (Join-Path $RunRoot "wheelhouse") `
            --requirements $requirementsBasePath `
            --requirements $requirementsBuildPath `
            --verify $manifestPath
    }
    if ($verified.exitCode -ne 0) {
        throw "environment_manifest_drift"
    }
    $null = Assert-FileIdentity -Path $pythonPath -Identity $manifest.python -Code "environment_python_drift"
    $requirements = @($manifest.requirements)
    foreach ($name in @("requirements-base.txt", "requirements-build.txt")) {
        $identity = @($requirements | Where-Object { [string]$_.name -eq $name })
        $path = Join-Path $requirementsRoot $name
        if (
            $identity.Count -ne 1 -or
            [int64](Get-Item -LiteralPath $path).Length -ne [int64]$identity[0].length -or
            (Get-Sha256File -Path $path) -ne [string]$identity[0].sha256
        ) {
            throw "environment_requirements_drift"
        }
    }
    $pipLog = Join-Path $RunRoot "scratch\pip-check-$environmentName.log"
    $pipCheck = Invoke-CapturedCommand -LogPath $pipLog -Command { & $pythonPath -m pip check }
    if ($pipCheck.exitCode -ne 0) {
        throw "environment_pip_check_failed"
    }
    return [ordered]@{
        name = $environmentName
        root = $environmentRoot
        python = $pythonPath
        manifest = $manifestPath
        manifestSha256 = Get-Sha256File -Path $manifestPath
        productDependenciesSha256 = [string]$manifest.productDependenciesSha256
    }
}

function Assert-ToolchainManifest {
    param([Parameter(Mandatory = $true)][string]$RunRoot)

    $path = Join-Path $RunRoot "toolchain\toolchain-manifest.json"
    $null = Assert-NoReparsePath -Root $RunRoot -Path $path -Code "unsafe_toolchain_manifest"
    $manifest = Read-JsonObject -Path $path -Code "invalid_toolchain_manifest"
    if (
        [int]$manifest.schemaVersion -ne 1 -or
        [string]$manifest.kind -ne "scriber-installer-research-toolchain" -or
        [string]$manifest.runId -ne $RunId -or
        [string]$manifest.rustToolchain -ne "1.97.0"
    ) {
        throw "toolchain_identity_mismatch"
    }
    $toolchainRoot = Split-Path -Parent $path
    $null = Assert-FileIdentity -Path (Join-Path $toolchainRoot "node\node.exe") -Identity $manifest.node -Code "toolchain_node_drift"
    $null = Assert-FileIdentity -Path (Join-Path $toolchainRoot "node\node_modules\npm\bin\npm-cli.js") -Identity $manifest.npm -Code "toolchain_npm_drift"
    $null = Assert-FileIdentity -Path (Join-Path $RepoRoot "Frontend\node_modules\@tauri-apps\cli\tauri.js") -Identity $manifest.tauri -Code "toolchain_tauri_js_drift"
    $null = Assert-FileIdentity `
        -Path (Join-Path $RepoRoot "Frontend\node_modules\@tauri-apps\cli-win32-x64-msvc\cli.win32-x64-msvc.node") `
        -Identity $manifest.nativeTauriCli `
        -Code "toolchain_native_tauri_drift"
    $null = Assert-FileIdentity -Path (Join-Path $RepoRoot "Frontend\package-lock.json") -Identity $manifest.frontendPackageLock -Code "toolchain_frontend_lock_drift"
    $expectedNodeModules = $manifest.frontendNodeModules
    $actualNodeModules = Get-PlainTreeIdentity -Root (Join-Path $RepoRoot "Frontend\node_modules")
    if (
        [int]$expectedNodeModules.fileCount -ne [int]$actualNodeModules.fileCount -or
        [int64]$expectedNodeModules.totalBytes -ne [int64]$actualNodeModules.totalBytes -or
        [string]$expectedNodeModules.treeSha256 -ne [string]$actualNodeModules.treeSha256
    ) {
        throw "toolchain_frontend_node_modules_drift"
    }
    $rustupCommand = Get-Command rustup.exe -ErrorAction Stop
    foreach ($binding in @(
        [ordered]@{ executable = "rustc"; identity = $manifest.rustc; code = "toolchain_rustc_drift" },
        [ordered]@{ executable = "cargo"; identity = $manifest.cargo; code = "toolchain_cargo_drift" },
        [ordered]@{ executable = "rustfmt"; identity = $manifest.rustfmt; code = "toolchain_rustfmt_drift" },
        [ordered]@{ executable = "clippy-driver"; identity = $manifest.clippyDriver; code = "toolchain_clippy_drift" }
    )) {
        $resolved = (& $rustupCommand.Source which --toolchain ([string]$manifest.rustToolchain) ([string]$binding.executable)).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw ([string]$binding.code)
        }
        $null = Assert-FileIdentity -Path $resolved -Identity $binding.identity -Code ([string]$binding.code)
    }
    $null = Assert-NsisTreeIdentity -Manifest $manifest -Code "toolchain_nsis_tree_drift"
    return [ordered]@{ path = $path; manifest = $manifest; sha256 = Get-Sha256File -Path $path }
}

function Assert-PayloadMatchesInventory {
    param(
        [Parameter(Mandatory = $true)][string]$PayloadRoot,
        [Parameter(Mandatory = $true)][object]$Inventory
    )

    $expected = @($Inventory.payload.staged.files)
    $actual = @(Get-ChildItem -LiteralPath $PayloadRoot -Recurse -File -Force -ErrorAction Stop)
    if ($actual.Count -ne $expected.Count) {
        throw "payload_file_count_drift"
    }
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $expected) {
        $relative = [string]$entry.path
        if (
            -not $relative -or
            $relative.Contains("..") -or
            $relative.Contains("\") -or
            $relative.StartsWith("/") -or
            -not $seen.Add($relative)
        ) {
            throw "unsafe_payload_inventory_path"
        }
        $candidate = Join-Path $PayloadRoot ($relative.Replace('/', '\'))
        $null = Assert-FileIdentity -Path $candidate -Identity $entry -Code "payload_inventory_drift"
    }
    foreach ($file in $actual) {
        $relative = $file.FullName.Substring((Convert-ToFullPath -Path $PayloadRoot).Length).TrimStart('\', '/').Replace('\', '/')
        if (-not $seen.Contains($relative)) {
            throw "payload_inventory_extra_file"
        }
    }
}

function Assert-InstalledPayloadMatchesInventory {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][object]$Inventory
    )

    $expected = @($Inventory.payload.installed.files)
    $actual = @(Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -Force -ErrorAction Stop)
    if ($actual.Count -ne $expected.Count) {
        throw "installed_payload_file_count_drift"
    }
    $rootFull = Convert-ToFullPath -Path $InstallRoot
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $expected) {
        $relative = [string]$entry.path
        if (
            -not $relative -or
            $relative.Contains("..") -or
            $relative.Contains("\") -or
            $relative.StartsWith("/") -or
            -not $seen.Add($relative)
        ) {
            throw "unsafe_installed_inventory_path"
        }
        $candidate = Join-Path $InstallRoot ($relative.Replace('/', '\'))
        $null = Assert-FileIdentity -Path $candidate -Identity $entry -Code "installed_payload_inventory_drift"
    }
    foreach ($file in $actual) {
        $relative = $file.FullName.Substring($rootFull.Length).TrimStart('\', '/').Replace('\', '/')
        if (-not $seen.Contains($relative)) {
            throw "installed_payload_inventory_extra_file"
        }
    }
}

function New-RedactedInstalledPayloadDifference {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("missing", "unexpected", "identity")][string]$Kind,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [int64]$ExpectedLength = -1,
        [int64]$ActualLength = -1,
        [string]$ExpectedSha256 = "",
        [string]$ActualSha256 = ""
    )

    $safeRelativePath = $RelativePath.Replace('\', '/')
    if (
        $safeRelativePath -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,255}$' -or
        $safeRelativePath -match '(^|/)\.\.(/|$)'
    ) {
        $safeRelativePath = "redacted"
    }
    foreach ($sha256 in @($ExpectedSha256, $ActualSha256)) {
        if ($sha256 -and $sha256 -notmatch '^[0-9a-f]{64}$') {
            throw "installed_payload_difference_hash_invalid"
        }
    }
    return [ordered]@{
        kind = $Kind
        relativePath = $safeRelativePath
        expectedLength = $ExpectedLength
        actualLength = $ActualLength
        expectedSha256 = $ExpectedSha256
        actualSha256 = $ActualSha256
    }
}

function Get-InstalledPayloadFirstDifference {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][object]$Inventory
    )

    $expectedEntries = @($Inventory.payload.installed.files)
    $actualFiles = @(Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -Force -ErrorAction Stop)
    $rootFull = Convert-ToFullPath -Path $InstallRoot
    $expectedByPath = [System.Collections.Generic.SortedDictionary[string, object]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($entry in $expectedEntries) {
        $relative = [string]$entry.path
        if (
            -not $relative -or
            $relative.Contains("..") -or
            $relative.Contains("\") -or
            $relative.StartsWith("/") -or
            $expectedByPath.ContainsKey($relative)
        ) {
            throw "unsafe_installed_inventory_path"
        }
        $expectedByPath.Add($relative, $entry)
    }

    $actualByPath = [System.Collections.Generic.SortedDictionary[string, object]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($file in $actualFiles) {
        $relative = $file.FullName.Substring($rootFull.Length).TrimStart('\', '/').Replace('\', '/')
        if ($actualByPath.ContainsKey($relative)) {
            throw "installed_payload_actual_path_ambiguous"
        }
        $actualByPath.Add($relative, $file)
    }

    $allPaths = [System.Collections.Generic.SortedSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($relative in $expectedByPath.Keys) {
        [void]$allPaths.Add($relative)
    }
    foreach ($relative in $actualByPath.Keys) {
        [void]$allPaths.Add($relative)
    }
    $countReasonCode = if ($actualFiles.Count -ne $expectedEntries.Count) {
        "installed_payload_file_count_drift"
    } else {
        ""
    }
    foreach ($relative in $allPaths) {
        $hasExpected = $expectedByPath.ContainsKey($relative)
        $hasActual = $actualByPath.ContainsKey($relative)
        if (-not $hasExpected) {
            $actual = $actualByPath[$relative]
            $actualIsReparse = ($actual.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
            $actualLength = if ($actualIsReparse) { [int64]-1 } else { [int64]$actual.Length }
            $actualSha256 = if ($actualIsReparse) { "" } else { Get-Sha256File -Path $actual.FullName }
            return [ordered]@{
                reasonCode = if ($countReasonCode) { $countReasonCode } else { "installed_payload_inventory_extra_file" }
                firstDifference = New-RedactedInstalledPayloadDifference `
                    -Kind "unexpected" `
                    -RelativePath $relative `
                    -ActualLength $actualLength `
                    -ActualSha256 $actualSha256
            }
        }
        if (-not $hasActual) {
            $expected = $expectedByPath[$relative]
            return [ordered]@{
                reasonCode = if ($countReasonCode) { $countReasonCode } else { "installed_payload_inventory_drift" }
                firstDifference = New-RedactedInstalledPayloadDifference `
                    -Kind "missing" `
                    -RelativePath $relative `
                    -ExpectedLength ([int64]$expected.length) `
                    -ExpectedSha256 ([string]$expected.sha256)
            }
        }

        $expected = $expectedByPath[$relative]
        $actual = $actualByPath[$relative]
        $actualSha256 = if (($actual.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            ""
        } else {
            Get-Sha256File -Path $actual.FullName
        }
        if (
            ($actual.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            [int64]$actual.Length -ne [int64]$expected.length -or
            $actualSha256 -ne [string]$expected.sha256
        ) {
            return [ordered]@{
                reasonCode = if ($countReasonCode) { $countReasonCode } else { "installed_payload_inventory_drift" }
                firstDifference = New-RedactedInstalledPayloadDifference `
                    -Kind "identity" `
                    -RelativePath $relative `
                    -ExpectedLength ([int64]$expected.length) `
                    -ActualLength ([int64]$actual.Length) `
                    -ExpectedSha256 ([string]$expected.sha256) `
                    -ActualSha256 $actualSha256
            }
        }
    }
    return $null
}

function Get-RedactedUpgradeGateReasonCode {
    param(
        [Parameter(Mandatory = $true)][System.Exception]$Exception,
        [Parameter(Mandatory = $true)][string]$ActiveCheck
    )

    $allowed = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    foreach ($reasonCode in @(
        "baseline_upgrade_source_install_failed",
        "baseline_upgrade_source_smoke_failed",
        "candidate_upgrade_launcher_timeout",
        "candidate_upgrade_installer_failed",
        "candidate_upgrade_completion_timeout",
        "candidate_upgrade_sentinel_drift",
        "installed_payload_file_count_drift",
        "installed_payload_inventory_drift",
        "installed_payload_inventory_extra_file",
        "candidate_upgrade_desktop_failed",
        "candidate_upgrade_frontend_failed",
        "candidate_upgrade_meeting_failed",
        "candidate_upgrade_runtime_cleanup_failed",
        "candidate_upgrade_strict_uninstall_failed",
        "upgrade_gate_cleanup_failed"
    )) {
        [void]$allowed.Add($reasonCode)
    }
    $message = [string]$Exception.Message
    if ($allowed.Contains($message)) {
        return $message
    }
    switch ($ActiveCheck) {
        "baseline-install" { return "baseline_upgrade_source_failed" }
        "data-sentinel" { return "upgrade_data_sentinel_failed" }
        "candidate-upgrade-install" { return "candidate_upgrade_install_failed" }
        "candidate-installed-payload" { return "candidate_installed_payload_failed" }
        "candidate-desktop-frontend" { return "candidate_upgrade_desktop_failed" }
        "candidate-meeting-capture" { return "candidate_upgrade_meeting_failed" }
        "candidate-runtime-cleanup" { return "candidate_upgrade_runtime_cleanup_failed" }
        "strict-uninstall" { return "candidate_upgrade_strict_uninstall_failed" }
        "gate-cleanup" { return "upgrade_gate_cleanup_failed" }
        default { return "upgrade_gate_failed" }
    }
}

function Write-UpgradeGateDetailArtifact {
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][string]$PacketId,
        [Parameter(Mandatory = $true)][string]$ParentChampionId,
        [Parameter(Mandatory = $true)][string]$SourceCommit,
        [Parameter(Mandatory = $true)][ValidateSet("pass", "fail")][string]$Status,
        [string]$FailedCheck = "",
        [string]$ReasonCode = "",
        [int]$InstallerExitCode = -1,
        [AllowNull()][object]$FirstDifference = $null
    )

    if (
        $InstallerExitCode -lt -1 -or
        ($Status -eq "pass" -and ($FailedCheck -or $ReasonCode -or $null -ne $FirstDifference)) -or
        ($Status -eq "fail" -and (
            $FailedCheck -notmatch '^[a-z0-9][a-z0-9._-]{0,95}$' -or
            $ReasonCode -notmatch '^[a-z0-9][a-z0-9._-]{0,95}$'
        ))
    ) {
        throw "invalid_upgrade_gate_detail"
    }
    if ($null -ne $FirstDifference) {
        if (
            [string]$FirstDifference.kind -notin @("missing", "unexpected", "identity") -or
            [string]$FirstDifference.relativePath -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,255}$' -or
            [string]$FirstDifference.relativePath -match '(^|/)\.\.(/|$)' -or
            [int64]$FirstDifference.expectedLength -lt -1 -or
            [int64]$FirstDifference.actualLength -lt -1 -or
            (([string]$FirstDifference.expectedSha256) -and [string]$FirstDifference.expectedSha256 -notmatch '^[0-9a-f]{64}$') -or
            (([string]$FirstDifference.actualSha256) -and [string]$FirstDifference.actualSha256 -notmatch '^[0-9a-f]{64}$')
        ) {
            throw "invalid_upgrade_gate_difference"
        }
    }

    $path = Join-Path $EvidenceRoot "upgrade-installed-payload.json"
    $artifact = [ordered]@{
        upgradeGateDetailContract = "InstallerResearchUpgradeGateDetailV1"
        schemaVersion = 1
        runId = $RunId
        packetId = $PacketId
        parentChampionId = $ParentChampionId
        sourceCommit = $SourceCommit
        status = $Status
        failedCheck = $FailedCheck
        reasonCode = $ReasonCode
        installerExitCode = $InstallerExitCode
        firstDifference = $FirstDifference
    }
    Write-JsonAtomic -Path $path -Payload $artifact
    $raw = Get-Content -LiteralPath $path -Raw
    if (
        ([System.Text.Encoding]::UTF8.GetByteCount($raw)) -gt 65536 -or
        $raw -match '(?i)file://' -or
        $raw -match '(?i)[a-z]:[\\/]' -or
        $raw -match '\\\\[^\\]'
    ) {
        throw "unsafe_upgrade_gate_detail"
    }
    return [ordered]@{
        kind = "same-version-upgrade-tree"
        relativePath = "packet-evidence/$PacketId/upgrade-installed-payload.json"
        sha256 = Get-Sha256File -Path $path
    }
}

function Copy-PlainPayload {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$RunRoot
    )

    $null = Assert-NoReparsePath -Root $RunRoot -Path $Source -Code "unsafe_source_payload" -Recurse
    $null = Assert-NoReparsePath -Root $RunRoot -Path $Destination -Code "unsafe_destination_payload"
    if (Test-Path -LiteralPath $Destination) {
        throw "payload_destination_exists"
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
    $null = Assert-NoReparsePath -Root $RunRoot -Path $Destination -Code "unsafe_destination_payload" -Recurse
}

function Invoke-FullInstallerBuild {
    param(
        [Parameter(Mandatory = $true)][string]$BuildRoot,
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$ToolchainManifest,
        [Parameter(Mandatory = $true)][string]$ExpectedInstaller
    )

    if (Test-Path -LiteralPath $BuildRoot) {
        throw "fresh_build_root_required"
    }
    New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
    $bundleRoot = Join-Path $RepoRoot "Frontend\src-tauri\target\release\bundle"
    if (Test-Path -LiteralPath $ExpectedInstaller -PathType Leaf) {
        $null = Assert-NoReparsePath -Root $bundleRoot -Path $ExpectedInstaller -Code "unsafe_stale_installer"
        Remove-Item -LiteralPath $ExpectedInstaller -Force
    }
    $log = Join-Path $BuildRoot "build.log"
    $built = Invoke-CapturedCommand -LogPath $log -PowerShellScript -Command {
        & (Join-Path $RepoRoot "scripts\build_windows.ps1") `
            -RepoRoot $RepoRoot `
            -Bundles @("nsis") `
            -NsisCompression "bzip2" `
            -UseProfileBFfmpeg `
            -ValidateSlimMediaTools `
            -SkipPythonTests `
            -SkipFrontendTypeCheck `
            -SkipSmoke `
            -RunRuntimeDependencyFootprint `
            -RunMediaPreparationSmoke `
            -ParallelizeIndependentBuilds `
            -PythonExecutable $Python `
            -ResearchBuildRoot $BuildRoot `
            -ResearchToolchainManifest $ToolchainManifest
    }
    if ($built.exitCode -ne 0 -or -not (Test-Path -LiteralPath $ExpectedInstaller -PathType Leaf)) {
        throw "installer_build_failed"
    }
    return $built.evidenceSha256
}

function Invoke-CompressionRepack {
    param(
        [Parameter(Mandatory = $true)][string]$BuildRoot,
        [Parameter(Mandatory = $true)][string]$PayloadRoot,
        [Parameter(Mandatory = $true)][string]$Compression,
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][object]$Toolchain,
        [Parameter(Mandatory = $true)][string]$ExpectedInstallerName
    )

    if ($Compression -notin @("bzip2", "zlib", "lzma")) {
        throw "invalid_repack_compression"
    }
    if (Test-Path -LiteralPath $BuildRoot) {
        throw "fresh_repack_root_required"
    }
    New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
    $isolatedTarget = Join-Path $BuildRoot "cargo-target"
    $releaseRoot = Join-Path $isolatedTarget "release"
    New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
    $null = Assert-NoReparsePath -Root $BuildRoot -Path $isolatedTarget -Code "unsafe_isolated_repack_target"
    Copy-Item -LiteralPath (Join-Path $PayloadRoot "backend") -Destination (Join-Path $releaseRoot "backend") -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $PayloadRoot "scriber-desktop.exe") -Destination (Join-Path $releaseRoot "scriber-desktop.exe") -Force
    Copy-Item -LiteralPath (Join-Path $PayloadRoot "scriber-audio-sidecar.exe") -Destination (Join-Path $releaseRoot "scriber-audio-sidecar.exe") -Force
    $null = Assert-NoReparsePath -Root $BuildRoot -Path $releaseRoot -Code "unsafe_isolated_repack_payload" -Recurse
    if ((Get-Sha256File -Path (Join-Path $PayloadRoot "THIRD_PARTY_NOTICES.md")) -ne (Get-Sha256File -Path (Join-Path $RepoRoot "THIRD_PARTY_NOTICES.md"))) {
        throw "repack_notice_drift"
    }
    if (
        (Get-PlainTreeIdentitySha256 -Root (Join-Path $releaseRoot "backend")) -ne
        (Get-PlainTreeIdentitySha256 -Root (Join-Path $PayloadRoot "backend")) -or
        (Get-Sha256File -Path (Join-Path $releaseRoot "scriber-desktop.exe")) -ne
        (Get-Sha256File -Path (Join-Path $PayloadRoot "scriber-desktop.exe")) -or
        (Get-Sha256File -Path (Join-Path $releaseRoot "scriber-audio-sidecar.exe")) -ne
        (Get-Sha256File -Path (Join-Path $PayloadRoot "scriber-audio-sidecar.exe"))
    ) {
        throw "isolated_repack_payload_drift"
    }
    $expectedInstaller = Join-Path $releaseRoot "bundle\nsis\$ExpectedInstallerName"
    $configPath = Join-Path $BuildRoot "tauri-repack.conf.json"
    $configLog = Join-Path $BuildRoot "prepare-config.log"
    $version = Get-ProductVersion
    $configured = Invoke-CapturedCommand -LogPath $configLog -Command {
        & $Python (Join-Path $RepoRoot "scripts\prepare_tauri_updater_config.py") `
            --config (Join-Path $RepoRoot "Frontend\src-tauri\tauri.conf.json") `
            --output $configPath `
            --version $version `
            --nsis-compression $Compression `
            --remove-before-bundle-command `
            --skip-updater-config
    }
    if ($configured.exitCode -ne 0) {
        throw "repack_config_failed"
    }
    $repackConfig = Read-JsonObject -Path $configPath -Code "repack_config_invalid"
    $isolatedBackendSource = (Convert-ToFullPath -Path (Join-Path $releaseRoot "backend")).Replace('\', '/') + "/"
    $noticesSource = (Convert-ToFullPath -Path (Join-Path $RepoRoot "THIRD_PARTY_NOTICES.md")).Replace('\', '/')
    $isolatedResources = [ordered]@{}
    $isolatedResources[$isolatedBackendSource] = "backend/"
    $isolatedResources[$noticesSource] = "THIRD_PARTY_NOTICES.md"
    $repackConfig.bundle.resources = [pscustomobject]$isolatedResources
    Write-JsonAtomic -Path $configPath -Payload $repackConfig
    $verifiedRepackConfig = Read-JsonObject -Path $configPath -Code "repack_config_invalid"
    $resourceProperties = @($verifiedRepackConfig.bundle.resources.PSObject.Properties)
    if (
        $resourceProperties.Count -ne 2 -or
        [string]$verifiedRepackConfig.bundle.resources.$isolatedBackendSource -ne "backend/" -or
        [string]$verifiedRepackConfig.bundle.resources.$noticesSource -ne "THIRD_PARTY_NOTICES.md"
    ) {
        throw "repack_resource_binding_failed"
    }
    $toolchainRoot = Split-Path -Parent $Toolchain.path
    $node = Assert-FileIdentity -Path (Join-Path $toolchainRoot "node\node.exe") -Identity $Toolchain.manifest.node -Code "pinned_node_drift"
    $tauri = Assert-FileIdentity -Path (Join-Path $RepoRoot "Frontend\node_modules\@tauri-apps\cli\tauri.js") -Identity $Toolchain.manifest.tauri -Code "pinned_tauri_drift"
    $priorRust = $env:RUSTUP_TOOLCHAIN
    $priorCargoTarget = $env:CARGO_TARGET_DIR
    $env:RUSTUP_TOOLCHAIN = [string]$Toolchain.manifest.rustToolchain
    $env:CARGO_TARGET_DIR = $isolatedTarget
    try {
        $bundleLog = Join-Path $BuildRoot "tauri-bundle.log"
        $bundled = Invoke-CapturedCommand -LogPath $bundleLog -Command {
            Push-Location (Join-Path $RepoRoot "Frontend")
            try {
                $null = Assert-NsisTreeIdentity -Manifest $Toolchain.manifest -Code "repack_nsis_tree_drift"
                try {
                    & $node $tauri bundle --bundles "nsis" --config $configPath --ci
                } finally {
                    $null = Assert-NsisTreeIdentity -Manifest $Toolchain.manifest -Code "repack_nsis_tree_drift"
                }
            } finally {
                Pop-Location
            }
        }
        if ($bundled.exitCode -ne 0 -or -not (Test-Path -LiteralPath $expectedInstaller -PathType Leaf)) {
            throw "repack_bundle_failed"
        }
        return [ordered]@{
            evidenceSha256 = $bundled.evidenceSha256
            installerPath = $expectedInstaller
        }
    } finally {
        if ($null -eq $priorRust) {
            Remove-Item Env:RUSTUP_TOOLCHAIN -ErrorAction SilentlyContinue
        } else {
            $env:RUSTUP_TOOLCHAIN = $priorRust
        }
        if ($null -eq $priorCargoTarget) {
            Remove-Item Env:CARGO_TARGET_DIR -ErrorAction SilentlyContinue
        } else {
            $env:CARGO_TARGET_DIR = $priorCargoTarget
        }
    }
}

function Get-ParentInventory {
    param(
        [Parameter(Mandatory = $true)][string]$RunRoot,
        [Parameter(Mandatory = $true)][string]$ParentId
    )

    if ($ParentId -eq "baseline") {
        $baseline = Read-JsonObject -Path (Join-Path $RunRoot "baseline.json") -Code "baseline_missing"
        if ($baseline.accepted -ne $true -or $null -eq $baseline.inventory) {
            throw "baseline_not_accepted"
        }
        return $baseline.inventory
    }
    $safe = Assert-SafePacketId -Value $ParentId
    $champion = Read-JsonObject -Path (Join-Path $RunRoot "champion.json") -Code "champion_missing"
    if ([string]$champion.packetId -ne $safe -or [string]$champion.decision -ne "keep") {
        throw "parent_champion_mismatch"
    }
    return Read-JsonObject -Path (Join-Path $RunRoot "packet-evidence\$safe\inventory.json") -Code "parent_inventory_missing"
}

function Get-InstallerArchivePath {
    param(
        [Parameter(Mandatory = $true)][string]$RunRoot,
        [Parameter(Mandatory = $true)][string]$PacketId,
        [Parameter(Mandatory = $true)][object]$Inventory
    )

    $path = Join-Path $RunRoot "artifacts\$PacketId\$([string]$Inventory.installer.name)"
    $null = Assert-FileIdentity -Path $path -Identity $Inventory.installer -Code "archived_installer_drift"
    return $path
}

function Test-HoldoutSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedContract,
        [Parameter(Mandatory = $true)][string]$ExpectedRunId
    )

    try {
        $payload = Read-JsonObject -Path $Path -Code "holdout_missing"
        if (
            [string]$payload.holdoutSnapshotContract -ne $ExpectedContract -or
            [int]$payload.schemaVersion -ne 1 -or
            [string]$payload.runId -ne $ExpectedRunId -or
            @($payload.cases).Count -ne 6
        ) {
            return $false
        }
        foreach ($case in @($payload.cases)) {
            if ([string]$case.status -notin @("validated", "pass")) {
                return $false
            }
        }
        return $true
    } catch {
        return $false
    }
}

function Test-HoldoutInventoryBinding {
    param(
        [Parameter(Mandatory = $true)][object]$Binding,
        [Parameter(Mandatory = $true)][object]$Inventory,
        [Parameter(Mandatory = $true)][string]$InventoryPath
    )

    try {
        return (
            [string]$Binding.inventorySha256 -eq (Get-Sha256File -Path $InventoryPath) -and
            [string]$Binding.sourceCommit -eq [string]$Inventory.sourceCommit -and
            [string]$Binding.replicaId -eq [string]$Inventory.buildProvenance.replicaId -and
            [string]$Binding.stagedSemanticTreeSha256 -eq [string]$Inventory.payload.staged.semanticTreeSha256 -and
            [string]$Binding.stagedFileListSha256 -eq [string]$Inventory.payload.staged.fileListSha256 -and
            [string]$Binding.installedSemanticTreeSha256 -eq [string]$Inventory.payload.installed.semanticTreeSha256 -and
            [string]$Binding.installedFileListSha256 -eq [string]$Inventory.payload.installed.fileListSha256
        )
    } catch {
        return $false
    }
}

function Test-CandidateHoldoutEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedRunId,
        [Parameter(Mandatory = $true)][string]$ExpectedPacketId,
        [Parameter(Mandatory = $true)][string]$ExpectedParentChampionId,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceCommit,
        [Parameter(Mandatory = $true)][string]$BaselineInventoryPath,
        [Parameter(Mandatory = $true)][string]$ParentInventoryPath,
        [Parameter(Mandatory = $true)][string]$CandidateInventoryPath
    )

    try {
        $hasExactFields = {
            param($Object, [string[]]$Expected)
            if ($null -eq $Object) {
                return $false
            }
            $actual = @($Object.PSObject.Properties.Name)
            if ($actual.Count -ne $Expected.Count) {
                return $false
            }
            $expectedSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
            foreach ($name in $Expected) {
                [void]$expectedSet.Add($name)
            }
            foreach ($name in $actual) {
                if (-not $expectedSet.Contains([string]$name)) {
                    return $false
                }
            }
            return $true
        }
        $isInteger = {
            param($Value)
            return (
                $Value -is [byte] -or $Value -is [sbyte] -or
                $Value -is [int16] -or $Value -is [uint16] -or
                $Value -is [int32] -or $Value -is [uint32] -or
                $Value -is [int64] -or $Value -is [uint64]
            )
        }
        $isNonnegativeInteger = {
            param($Value)
            return ((& $isInteger $Value) -and [decimal]$Value -ge 0)
        }
        $arraysEqual = {
            param($Left, $Right)
            if ($Left -isnot [System.Array] -or $Right -isnot [System.Array]) {
                return $false
            }
            $leftItems = @($Left)
            $rightItems = @($Right)
            if ($leftItems.Count -ne $rightItems.Count) {
                return $false
            }
            for ($index = 0; $index -lt $leftItems.Count; $index++) {
                if (-not [string]::Equals([string]$leftItems[$index], [string]$rightItems[$index], [System.StringComparison]::Ordinal)) {
                    return $false
                }
            }
            return $true
        }
        $isCapabilityArray = {
            param($Value, [bool]$RequireNonempty = $false)
            if ($Value -isnot [System.Array]) {
                return $false
            }
            $items = @($Value)
            if ($RequireNonempty -and $items.Count -eq 0) {
                return $false
            }
            $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
            $previous = $null
            foreach ($item in $items) {
                if ($item -isnot [string] -or [string]$item -notmatch '^[a-z0-9][a-z0-9-]{0,63}$') {
                    return $false
                }
                if (-not $seen.Add([string]$item)) {
                    return $false
                }
                if ($null -ne $previous -and [string]::CompareOrdinal([string]$previous, [string]$item) -ge 0) {
                    return $false
                }
                $previous = [string]$item
            }
            return $true
        }
        $diagnosticFields = @(
            "comparisonPolicy", "requiredCapabilities",
            "baselineObservedCapabilities", "candidateObservedCapabilities",
            "baselineMissingRequiredCapabilities", "candidateMissingRequiredCapabilities",
            "optionalOnlyInBaseline", "optionalOnlyInCandidate", "optionalParity"
        )
        $testDiagnostics = {
            param($Diagnostics)
            if (
                -not (& $hasExactFields $Diagnostics $diagnosticFields) -or
                [string]$Diagnostics.comparisonPolicy -ne "required-capabilities-v2" -or
                $Diagnostics.optionalParity -isnot [bool] -or
                -not (& $isCapabilityArray $Diagnostics.requiredCapabilities $true) -or
                -not (& $isCapabilityArray $Diagnostics.baselineObservedCapabilities $false) -or
                -not (& $isCapabilityArray $Diagnostics.candidateObservedCapabilities $false) -or
                -not (& $isCapabilityArray $Diagnostics.baselineMissingRequiredCapabilities $false) -or
                -not (& $isCapabilityArray $Diagnostics.candidateMissingRequiredCapabilities $false) -or
                -not (& $isCapabilityArray $Diagnostics.optionalOnlyInBaseline $false) -or
                -not (& $isCapabilityArray $Diagnostics.optionalOnlyInCandidate $false)
            ) {
                return $false
            }
            return $true
        }
        $failureCodes = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
        foreach ($failureCode in @(
            "http_429", "http_403", "login_required", "geo_restricted",
            "media_unavailable", "network_timeout", "tls_failure", "dns_failure",
            "extractor_error", "unknown_failure", "probe_boundary_invalid",
            "probe_response_limit", "timeout", "cancelled", "output_limit",
            "invalid_json", "probe_contract_invalid", "unclassified_failure"
        )) {
            [void]$failureCodes.Add($failureCode)
        }
        $pairAttemptFields = @(
            "attemptIndex", "attemptKind", "logicalSampleId", "order", "status",
            "reasonCode", "baselineDurationNs", "candidateDurationNs",
            "semanticCapabilities", "capabilityDiagnostics", "baselineFailureCode",
            "candidateFailureCode", "cleanupVerified"
        )
        $testPairAttempt = {
            param($Attempt, [string]$LogicalSampleId, [int]$AttemptIndex, [string]$AttemptKind, [string]$ExpectedStatus)
            if (
                -not (& $hasExactFields $Attempt $pairAttemptFields) -or
                -not (& $isInteger $Attempt.attemptIndex) -or
                [int]$Attempt.attemptIndex -ne $AttemptIndex -or
                [string]$Attempt.attemptKind -ne $AttemptKind -or
                [string]$Attempt.logicalSampleId -ne $LogicalSampleId -or
                $Attempt.order -isnot [System.Array] -or
                @($Attempt.order).Count -ne 2 -or
                [string]$Attempt.order[0] -ne "baseline" -or
                [string]$Attempt.order[1] -ne "candidate" -or
                [string]$Attempt.status -ne $ExpectedStatus -or
                -not (& $isNonnegativeInteger $Attempt.baselineDurationNs) -or
                -not (& $isNonnegativeInteger $Attempt.candidateDurationNs) -or
                -not (& $isCapabilityArray $Attempt.semanticCapabilities $false) -or
                -not (& $testDiagnostics $Attempt.capabilityDiagnostics) -or
                $Attempt.cleanupVerified -isnot [bool]
            ) {
                return $false
            }
            if ($ExpectedStatus -eq "pass") {
                return (
                    $null -eq $Attempt.reasonCode -and
                    $null -eq $Attempt.baselineFailureCode -and
                    $null -eq $Attempt.candidateFailureCode -and
                    $Attempt.cleanupVerified -eq $true -and
                    @($Attempt.capabilityDiagnostics.baselineMissingRequiredCapabilities).Count -eq 0 -and
                    @($Attempt.capabilityDiagnostics.candidateMissingRequiredCapabilities).Count -eq 0 -and
                    (& $arraysEqual $Attempt.semanticCapabilities $Attempt.capabilityDiagnostics.baselineObservedCapabilities)
                )
            }
            return (
                [string]$Attempt.reasonCode -eq "candidate_probe_failed" -and
                $null -eq $Attempt.baselineFailureCode -and
                $Attempt.candidateFailureCode -is [string] -and
                $failureCodes.Contains([string]$Attempt.candidateFailureCode) -and
                @($Attempt.capabilityDiagnostics.baselineMissingRequiredCapabilities).Count -eq 0 -and
                @($Attempt.capabilityDiagnostics.candidateObservedCapabilities).Count -eq 0 -and
                (& $arraysEqual $Attempt.capabilityDiagnostics.candidateMissingRequiredCapabilities $Attempt.capabilityDiagnostics.requiredCapabilities) -and
                @($Attempt.semanticCapabilities).Count -eq 0
            )
        }
        $recoveryFields = @(
            "eligible", "attempted", "accepted", "budgetOrdinal", "budgetExhausted",
            "triggerReasonCode", "confirmationReasonCode"
        )
        $testRecovery = {
            param($Recovery, [bool]$Recovered, [string]$TriggerReason)
            if (-not (& $hasExactFields $Recovery $recoveryFields)) {
                return $false
            }
            if ($Recovered) {
                return (
                    $Recovery.eligible -is [bool] -and $Recovery.eligible -eq $true -and
                    $Recovery.attempted -is [bool] -and $Recovery.attempted -eq $true -and
                    $Recovery.accepted -is [bool] -and $Recovery.accepted -eq $true -and
                    (& $isInteger $Recovery.budgetOrdinal) -and [int]$Recovery.budgetOrdinal -eq 1 -and
                    $Recovery.budgetExhausted -is [bool] -and $Recovery.budgetExhausted -eq $false -and
                    [string]$Recovery.triggerReasonCode -eq $TriggerReason -and
                    $null -eq $Recovery.confirmationReasonCode
                )
            }
            return (
                $Recovery.eligible -is [bool] -and $Recovery.eligible -eq $false -and
                $Recovery.attempted -is [bool] -and $Recovery.attempted -eq $false -and
                $Recovery.accepted -is [bool] -and $Recovery.accepted -eq $false -and
                $null -eq $Recovery.budgetOrdinal -and
                $Recovery.budgetExhausted -is [bool] -and $Recovery.budgetExhausted -eq $false -and
                $null -eq $Recovery.triggerReasonCode -and
                $null -eq $Recovery.confirmationReasonCode
            )
        }
        $testPairSample = {
            param($Sample, [string]$LogicalSampleId, [bool]$IncludeTimings)
            if ($Sample.attempts -isnot [System.Array]) {
                return $false
            }
            $attempts = @($Sample.attempts)
            if ($attempts.Count -notin @(1, 2)) {
                return $false
            }
            $recovered = $attempts.Count -eq 2
            if ($recovered) {
                if (
                    -not (& $testPairAttempt $attempts[0] $LogicalSampleId 1 "original" "fail") -or
                    -not (& $testPairAttempt $attempts[1] $LogicalSampleId 2 "confirmation" "pass") -or
                    -not (& $arraysEqual $attempts[0].capabilityDiagnostics.requiredCapabilities $attempts[1].capabilityDiagnostics.requiredCapabilities)
                ) {
                    return $false
                }
                $selected = $attempts[1]
                $selectedName = "confirmation"
            } else {
                if (-not (& $testPairAttempt $attempts[0] $LogicalSampleId 1 "original" "pass")) {
                    return $false
                }
                $selected = $attempts[0]
                $selectedName = "original"
            }
            if (
                [string]$Sample.status -ne "pass" -or
                $null -ne $Sample.reasonCode -or
                [string]$Sample.selectedAttempt -ne $selectedName -or
                $null -ne $Sample.baselineFailureCode -or
                $null -ne $Sample.candidateFailureCode -or
                $Sample.cleanupVerified -isnot [bool] -or
                $Sample.cleanupVerified -ne $true -or
                -not (& $testDiagnostics $Sample.capabilityDiagnostics) -or
                -not (& $arraysEqual $Sample.capabilityDiagnostics.requiredCapabilities $selected.capabilityDiagnostics.requiredCapabilities) -or
                -not (& $arraysEqual $Sample.capabilityDiagnostics.baselineObservedCapabilities $selected.capabilityDiagnostics.baselineObservedCapabilities) -or
                -not (& $arraysEqual $Sample.capabilityDiagnostics.candidateObservedCapabilities $selected.capabilityDiagnostics.candidateObservedCapabilities) -or
                -not (& $testRecovery $Sample.recovery $recovered "candidate_probe_failed")
            ) {
                return $false
            }
            if ($IncludeTimings) {
                if (
                    $Sample.order -isnot [System.Array] -or
                    @($Sample.order).Count -ne 2 -or
                    [string]$Sample.order[0] -ne "baseline" -or
                    [string]$Sample.order[1] -ne "candidate" -or
                    -not (& $isNonnegativeInteger $Sample.baselineDurationNs) -or
                    -not (& $isNonnegativeInteger $Sample.candidateDurationNs) -or
                    [int64]$Sample.baselineDurationNs -ne [int64]$selected.baselineDurationNs -or
                    [int64]$Sample.candidateDurationNs -ne [int64]$selected.candidateDurationNs -or
                    -not (& $arraysEqual $Sample.semanticCapabilities $selected.semanticCapabilities)
                ) {
                    return $false
                }
            }
            return $true
        }
        $parallelAttemptFields = @(
            "attemptIndex", "attemptKind", "logicalSampleId", "status", "reasonCode",
            "workerCount", "workers", "capabilityParity", "capabilityDiagnostics",
            "cleanupVerified"
        )
        $parallelWorkerFields = @(
            "workerIndex", "status", "durationNs", "semanticCapabilities",
            "missingRequiredCapabilities", "failureCode", "cleanupVerified"
        )
        $testParallelAttempt = {
            param($Attempt, [int]$AttemptIndex, [string]$AttemptKind, [string]$ExpectedStatus)
            if (
                -not (& $hasExactFields $Attempt $parallelAttemptFields) -or
                -not (& $isInteger $Attempt.attemptIndex) -or
                [int]$Attempt.attemptIndex -ne $AttemptIndex -or
                [string]$Attempt.attemptKind -ne $AttemptKind -or
                [string]$Attempt.logicalSampleId -ne "parallel:two-worker" -or
                [string]$Attempt.status -ne $ExpectedStatus -or
                -not (& $isInteger $Attempt.workerCount) -or
                [int]$Attempt.workerCount -ne 2 -or
                $Attempt.workers -isnot [System.Array] -or
                @($Attempt.workers).Count -ne 2 -or
                $Attempt.capabilityParity -isnot [bool] -or
                $Attempt.cleanupVerified -isnot [bool] -or
                -not (& $testDiagnostics $Attempt.capabilityDiagnostics)
            ) {
                return $false
            }
            $allWorkersPass = $true
            $allRequired = $true
            $allCleanup = $true
            for ($workerIndex = 0; $workerIndex -lt 2; $workerIndex++) {
                $worker = @($Attempt.workers)[$workerIndex]
                if (
                    -not (& $hasExactFields $worker $parallelWorkerFields) -or
                    -not (& $isInteger $worker.workerIndex) -or
                    [int]$worker.workerIndex -ne ($workerIndex + 1) -or
                    [string]$worker.status -notin @("pass", "fail") -or
                    -not (& $isNonnegativeInteger $worker.durationNs) -or
                    -not (& $isCapabilityArray $worker.semanticCapabilities $false) -or
                    -not (& $isCapabilityArray $worker.missingRequiredCapabilities $false) -or
                    $worker.cleanupVerified -isnot [bool]
                ) {
                    return $false
                }
                if ([string]$worker.status -eq "pass") {
                    if ($null -ne $worker.failureCode) {
                        return $false
                    }
                } elseif ($worker.failureCode -isnot [string] -or -not $failureCodes.Contains([string]$worker.failureCode)) {
                    return $false
                }
                $allWorkersPass = $allWorkersPass -and [string]$worker.status -eq "pass"
                $allRequired = $allRequired -and @($worker.missingRequiredCapabilities).Count -eq 0
                $allCleanup = $allCleanup -and $worker.cleanupVerified -eq $true
            }
            $workers = @($Attempt.workers)
            if (
                -not (& $arraysEqual $Attempt.capabilityDiagnostics.baselineObservedCapabilities $workers[0].semanticCapabilities) -or
                -not (& $arraysEqual $Attempt.capabilityDiagnostics.candidateObservedCapabilities $workers[1].semanticCapabilities) -or
                -not (& $arraysEqual $Attempt.capabilityDiagnostics.baselineMissingRequiredCapabilities $workers[0].missingRequiredCapabilities) -or
                -not (& $arraysEqual $Attempt.capabilityDiagnostics.candidateMissingRequiredCapabilities $workers[1].missingRequiredCapabilities)
            ) {
                return $false
            }
            if ($ExpectedStatus -eq "pass") {
                return (
                    $null -eq $Attempt.reasonCode -and $allWorkersPass -and $allRequired -and
                    $allCleanup -and $Attempt.capabilityParity -eq $true -and
                    $Attempt.cleanupVerified -eq $true
                )
            }
            return (
                [string]$Attempt.reasonCode -eq "candidate_parallel_probe_failed" -and
                -not $allWorkersPass
            )
        }

        $payload = Read-JsonObject -Path $Path -Code "candidate_holdout_missing"
        $topFields = @(
            "holdoutSnapshotContract", "schemaVersion", "status", "reasonCodes",
            "capturedAtUtc", "runId", "packetId", "parentChampionId", "sourceCommit",
            "bindings", "baseline", "candidate", "executionPolicy", "lifecycle",
            "parallelIsolation", "recoverySummary", "performance", "cases",
            "inputIntegrity", "inputImmutabilityVerified"
        )
        if (
            -not (& $hasExactFields $payload $topFields) -or
            [string]$payload.holdoutSnapshotContract -ne "InstallerSizeYoutubeCandidateHoldoutsV2" -or
            -not (& $isInteger $payload.schemaVersion) -or
            [int]$payload.schemaVersion -ne 2 -or
            [string]$payload.status -ne "pass" -or
            [string]$payload.runId -ne $ExpectedRunId -or
            [string]$payload.packetId -ne $ExpectedPacketId -or
            [string]$payload.parentChampionId -ne $ExpectedParentChampionId -or
            [string]$payload.sourceCommit -ne $ExpectedSourceCommit -or
            $payload.reasonCodes -isnot [System.Array] -or
            @($payload.reasonCodes).Count -ne 0 -or
            $payload.inputImmutabilityVerified -isnot [bool] -or
            $payload.inputImmutabilityVerified -ne $true
        ) {
            return $false
        }

        $baselineInventory = Read-JsonObject -Path $BaselineInventoryPath -Code "baseline_holdout_inventory_missing"
        $parentInventory = Read-JsonObject -Path $ParentInventoryPath -Code "parent_holdout_inventory_missing"
        $candidateInventory = Read-JsonObject -Path $CandidateInventoryPath -Code "candidate_holdout_inventory_missing"
        if (
            -not (Test-HoldoutInventoryBinding -Binding $payload.bindings.baseline -Inventory $baselineInventory -InventoryPath $BaselineInventoryPath) -or
            -not (Test-HoldoutInventoryBinding -Binding $payload.bindings.parent -Inventory $parentInventory -InventoryPath $ParentInventoryPath) -or
            -not (Test-HoldoutInventoryBinding -Binding $payload.bindings.candidate -Inventory $candidateInventory -InventoryPath $CandidateInventoryPath)
        ) {
            return $false
        }

        $policyFields = @(
            "pairing", "capabilityComparisonPolicy", "optionalCapabilityDifferencesBlocking",
            "candidateProbeFailuresBlocking", "candidateProbeRetryCount",
            "candidateProbeRetryScope", "candidateFailureConfirmationPolicy",
            "maximumCandidateOnlyRecoveries", "confirmationAttemptsPersisted",
            "normalPairConfirmationOrder", "parallelConfirmationMode",
            "confirmationRequiresAllRequiredCapabilities", "confirmationRequiresCleanup",
            "performanceCountsLogicalSamplesOnly", "primeCount", "logicalPairCount",
            "parallelLogicalProbeCount", "coldPairsPerCase", "warmPairsPerCase",
            "remoteComponents", "externalPlugins", "firstRunDownloads",
            "exactlyOneCandidateRuntime", "frozenBackendProbe", "privateRandomWorkspaces",
            "reparsePointsAllowed", "aclMode", "workspaceCount", "cleanupCount"
        )
        $confirmationOrder = @($payload.executionPolicy.normalPairConfirmationOrder)
        if (
            -not (& $hasExactFields $payload.executionPolicy $policyFields) -or
            [string]$payload.executionPolicy.pairing -ne "baseline-immediately-followed-by-candidate" -or
            [string]$payload.executionPolicy.capabilityComparisonPolicy -ne "required-capabilities-v2" -or
            $payload.executionPolicy.optionalCapabilityDifferencesBlocking -isnot [bool] -or
            $payload.executionPolicy.optionalCapabilityDifferencesBlocking -ne $false -or
            $payload.executionPolicy.candidateProbeFailuresBlocking -isnot [bool] -or
            $payload.executionPolicy.candidateProbeFailuresBlocking -ne $true -or
            -not (& $isInteger $payload.executionPolicy.candidateProbeRetryCount) -or
            [int]$payload.executionPolicy.candidateProbeRetryCount -ne 1 -or
            [string]$payload.executionPolicy.candidateProbeRetryScope -ne "global-candidate-only" -or
            [string]$payload.executionPolicy.candidateFailureConfirmationPolicy -ne "single-immediate-complete-confirmation-v1" -or
            -not (& $isInteger $payload.executionPolicy.maximumCandidateOnlyRecoveries) -or
            [int]$payload.executionPolicy.maximumCandidateOnlyRecoveries -ne 1 -or
            $payload.executionPolicy.confirmationAttemptsPersisted -isnot [bool] -or
            $payload.executionPolicy.confirmationAttemptsPersisted -ne $true -or
            $payload.executionPolicy.normalPairConfirmationOrder -isnot [System.Array] -or
            $confirmationOrder.Count -ne 2 -or
            [string]$confirmationOrder[0] -ne "baseline" -or
            [string]$confirmationOrder[1] -ne "candidate" -or
            [string]$payload.executionPolicy.parallelConfirmationMode -ne "repeat-complete-two-worker-probe" -or
            $payload.executionPolicy.confirmationRequiresAllRequiredCapabilities -isnot [bool] -or
            $payload.executionPolicy.confirmationRequiresAllRequiredCapabilities -ne $true -or
            $payload.executionPolicy.confirmationRequiresCleanup -isnot [bool] -or
            $payload.executionPolicy.confirmationRequiresCleanup -ne $true -or
            $payload.executionPolicy.performanceCountsLogicalSamplesOnly -isnot [bool] -or
            $payload.executionPolicy.performanceCountsLogicalSamplesOnly -ne $true -or
            -not (& $isInteger $payload.executionPolicy.primeCount) -or
            [int]$payload.executionPolicy.primeCount -ne 6 -or
            -not (& $isInteger $payload.executionPolicy.logicalPairCount) -or
            [int]$payload.executionPolicy.logicalPairCount -ne 24 -or
            -not (& $isInteger $payload.executionPolicy.parallelLogicalProbeCount) -or
            [int]$payload.executionPolicy.parallelLogicalProbeCount -ne 1 -or
            -not (& $isInteger $payload.executionPolicy.coldPairsPerCase) -or
            [int]$payload.executionPolicy.coldPairsPerCase -ne 2 -or
            -not (& $isInteger $payload.executionPolicy.warmPairsPerCase) -or
            [int]$payload.executionPolicy.warmPairsPerCase -ne 2 -or
            $payload.executionPolicy.remoteComponents -isnot [bool] -or
            $payload.executionPolicy.remoteComponents -ne $false -or
            $payload.executionPolicy.externalPlugins -isnot [bool] -or
            $payload.executionPolicy.externalPlugins -ne $false -or
            $payload.executionPolicy.firstRunDownloads -isnot [bool] -or
            $payload.executionPolicy.firstRunDownloads -ne $false -or
            $payload.executionPolicy.exactlyOneCandidateRuntime -isnot [bool] -or
            $payload.executionPolicy.exactlyOneCandidateRuntime -ne $true -or
            [string]$payload.executionPolicy.frozenBackendProbe -ne "InstallerYoutubeFrozenHoldoutProbeV1" -or
            $payload.executionPolicy.privateRandomWorkspaces -isnot [bool] -or
            $payload.executionPolicy.privateRandomWorkspaces -ne $true -or
            $payload.executionPolicy.reparsePointsAllowed -isnot [bool] -or
            $payload.executionPolicy.reparsePointsAllowed -ne $false -or
            $payload.executionPolicy.aclMode -isnot [string] -or
            [string]::IsNullOrWhiteSpace([string]$payload.executionPolicy.aclMode) -or
            -not (& $isNonnegativeInteger $payload.executionPolicy.workspaceCount) -or
            [int64]$payload.executionPolicy.workspaceCount -le 0 -or
            -not (& $isNonnegativeInteger $payload.executionPolicy.cleanupCount) -or
            [int64]$payload.executionPolicy.workspaceCount -ne [int64]$payload.executionPolicy.cleanupCount
        ) {
            return $false
        }

        if (
            $payload.lifecycle.successCleanup -ne $true -or
            $payload.lifecycle.errorCleanup -ne $true -or
            $payload.lifecycle.timeoutCleanup -ne $true -or
            $payload.lifecycle.cancellationCleanup -ne $true -or
            [int]$payload.lifecycle.parallelWorkers -ne 2 -or
            $payload.lifecycle.parallelWorkspaceIsolation -ne $true
        ) {
            return $false
        }

        $caseFields = @(
            "id", "family", "primeStatus", "primeReasonCode", "primeSelectedAttempt",
            "primeCapabilityDiagnostics", "primeBaselineFailureCode",
            "primeCandidateFailureCode", "primeCleanupVerified", "primeAttempts",
            "primeRecovery", "coldPairCount", "warmPairCount", "pairs"
        )
        $pairFields = @(
            "logicalSampleId", "order", "status", "reasonCode", "selectedAttempt",
            "baselineDurationNs", "candidateDurationNs", "semanticCapabilities",
            "capabilityDiagnostics", "baselineFailureCode", "candidateFailureCode",
            "cleanupVerified", "attempts", "recovery", "mode", "pairIndex"
        )
        $cases = @($payload.cases)
        if ($payload.cases -isnot [System.Array] -or $cases.Count -ne 6) {
            return $false
        }
        $ids = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
        $recoveredIds = [System.Collections.Generic.List[string]]::new()
        $logicalPairCount = 0
        $firstCaseRequired = $null
        foreach ($case in $cases) {
            $caseId = [string]$case.id
            if (
                -not (& $hasExactFields $case $caseFields) -or
                $case.id -isnot [string] -or
                $caseId -notmatch '^[a-z0-9][a-z0-9-]{0,63}$' -or
                -not $ids.Add($caseId) -or
                $case.family -isnot [string] -or
                [string]::IsNullOrWhiteSpace([string]$case.family) -or
                -not (& $isInteger $case.coldPairCount) -or [int]$case.coldPairCount -ne 2 -or
                -not (& $isInteger $case.warmPairCount) -or [int]$case.warmPairCount -ne 2
            ) {
                return $false
            }
            $prime = [pscustomobject]@{
                status = $case.primeStatus
                reasonCode = $case.primeReasonCode
                selectedAttempt = $case.primeSelectedAttempt
                capabilityDiagnostics = $case.primeCapabilityDiagnostics
                baselineFailureCode = $case.primeBaselineFailureCode
                candidateFailureCode = $case.primeCandidateFailureCode
                cleanupVerified = $case.primeCleanupVerified
                attempts = $case.primeAttempts
                recovery = $case.primeRecovery
            }
            $primeId = "$($caseId):prime"
            if (-not (& $testPairSample $prime $primeId $false)) {
                return $false
            }
            $caseRequired = $case.primeCapabilityDiagnostics.requiredCapabilities
            if ($null -eq $firstCaseRequired) {
                $firstCaseRequired = $caseRequired
            }
            if (@($case.primeAttempts).Count -eq 2) {
                [void]$recoveredIds.Add($primeId)
            }
            $pairs = @($case.pairs)
            if ($case.pairs -isnot [System.Array] -or $pairs.Count -ne 4) {
                return $false
            }
            $expectedModes = @("cold", "cold", "warm", "warm")
            $expectedIndexes = @(1, 2, 1, 2)
            for ($pairOffset = 0; $pairOffset -lt 4; $pairOffset++) {
                $pair = $pairs[$pairOffset]
                $mode = $expectedModes[$pairOffset]
                $pairIndex = $expectedIndexes[$pairOffset]
                $logicalId = "$($caseId):$($mode):$($pairIndex)"
                if (
                    -not (& $hasExactFields $pair $pairFields) -or
                    [string]$pair.mode -ne $mode -or
                    -not (& $isInteger $pair.pairIndex) -or
                    [int]$pair.pairIndex -ne $pairIndex -or
                    [string]$pair.logicalSampleId -ne $logicalId -or
                    -not (& $arraysEqual $pair.capabilityDiagnostics.requiredCapabilities $caseRequired) -or
                    -not (& $testPairSample $pair $logicalId $true)
                ) {
                    return $false
                }
                $logicalPairCount++
                if (@($pair.attempts).Count -eq 2) {
                    [void]$recoveredIds.Add($logicalId)
                }
            }
        }
        if ($logicalPairCount -ne 24) {
            return $false
        }

        $parallelFields = @(
            "logicalSampleId", "status", "reasonCode", "selectedAttempt", "workerCount",
            "distinctPrivateWorkspaces", "capabilityParity", "capabilityComparisonPolicy",
            "capabilityDiagnostics", "cleanupVerified", "attempts", "recovery"
        )
        $parallel = $payload.parallelIsolation
        if (
            -not (& $hasExactFields $parallel $parallelFields) -or
            [string]$parallel.logicalSampleId -ne "parallel:two-worker" -or
            [string]$parallel.status -ne "pass" -or
            $null -ne $parallel.reasonCode -or
            -not (& $isInteger $parallel.workerCount) -or [int]$parallel.workerCount -ne 2 -or
            $parallel.distinctPrivateWorkspaces -isnot [bool] -or $parallel.distinctPrivateWorkspaces -ne $true -or
            $parallel.capabilityParity -isnot [bool] -or $parallel.capabilityParity -ne $true -or
            [string]$parallel.capabilityComparisonPolicy -ne "required-capabilities-v2" -or
            -not (& $testDiagnostics $parallel.capabilityDiagnostics) -or
            -not (& $arraysEqual $parallel.capabilityDiagnostics.requiredCapabilities $firstCaseRequired) -or
            $parallel.cleanupVerified -isnot [bool] -or $parallel.cleanupVerified -ne $true -or
            $parallel.attempts -isnot [System.Array] -or
            @($parallel.attempts).Count -notin @(1, 2)
        ) {
            return $false
        }
        $parallelAttempts = @($parallel.attempts)
        $parallelRecovered = $parallelAttempts.Count -eq 2
        if ($parallelRecovered) {
            if (
                -not (& $testParallelAttempt $parallelAttempts[0] 1 "original" "fail") -or
                -not (& $testParallelAttempt $parallelAttempts[1] 2 "confirmation" "pass") -or
                [string]$parallel.selectedAttempt -ne "confirmation" -or
                -not (& $testRecovery $parallel.recovery $true "candidate_parallel_probe_failed")
            ) {
                return $false
            }
            $selectedParallel = $parallelAttempts[1]
            [void]$recoveredIds.Add("parallel:two-worker")
        } else {
            if (
                -not (& $testParallelAttempt $parallelAttempts[0] 1 "original" "pass") -or
                [string]$parallel.selectedAttempt -ne "original" -or
                -not (& $testRecovery $parallel.recovery $false "candidate_parallel_probe_failed")
            ) {
                return $false
            }
            $selectedParallel = $parallelAttempts[0]
        }
        if (
            -not (& $arraysEqual $parallel.capabilityDiagnostics.requiredCapabilities $selectedParallel.capabilityDiagnostics.requiredCapabilities) -or
            -not (& $arraysEqual $parallel.capabilityDiagnostics.baselineObservedCapabilities $selectedParallel.capabilityDiagnostics.baselineObservedCapabilities) -or
            -not (& $arraysEqual $parallel.capabilityDiagnostics.candidateObservedCapabilities $selectedParallel.capabilityDiagnostics.candidateObservedCapabilities)
        ) {
            return $false
        }

        $summaryFields = @(
            "maximumCandidateOnlyRecoveries", "candidateOnlyDisturbanceCount",
            "usedCandidateOnlyRecoveries", "acceptedCandidateOnlyRecoveries",
            "failedCandidateOnlyRecoveries", "recoveredLogicalSampleId"
        )
        $recoveredCount = $recoveredIds.Count
        $expectedRecoveredId = if ($recoveredCount -eq 1) { $recoveredIds[0] } else { $null }
        if (
            -not (& $hasExactFields $payload.recoverySummary $summaryFields) -or
            -not (& $isInteger $payload.recoverySummary.maximumCandidateOnlyRecoveries) -or
            [int]$payload.recoverySummary.maximumCandidateOnlyRecoveries -ne 1 -or
            -not (& $isInteger $payload.recoverySummary.candidateOnlyDisturbanceCount) -or
            [int]$payload.recoverySummary.candidateOnlyDisturbanceCount -ne $recoveredCount -or
            -not (& $isInteger $payload.recoverySummary.usedCandidateOnlyRecoveries) -or
            [int]$payload.recoverySummary.usedCandidateOnlyRecoveries -ne $recoveredCount -or
            -not (& $isInteger $payload.recoverySummary.acceptedCandidateOnlyRecoveries) -or
            [int]$payload.recoverySummary.acceptedCandidateOnlyRecoveries -ne $recoveredCount -or
            -not (& $isInteger $payload.recoverySummary.failedCandidateOnlyRecoveries) -or
            [int]$payload.recoverySummary.failedCandidateOnlyRecoveries -ne 0 -or
            $recoveredCount -gt 1 -or
            -not [object]::Equals($payload.recoverySummary.recoveredLogicalSampleId, $expectedRecoveredId)
        ) {
            return $false
        }

        $performanceFields = @(
            "baselineP95Ns", "candidateP95Ns", "maximumCandidateP95Ns",
            "maximumRatioBasisPoints", "pairedSampleCount", "passed"
        )
        if (
            -not (& $hasExactFields $payload.performance $performanceFields) -or
            $payload.performance.passed -isnot [bool] -or $payload.performance.passed -ne $true -or
            -not (& $isInteger $payload.performance.pairedSampleCount) -or [int]$payload.performance.pairedSampleCount -ne 24 -or
            -not (& $isInteger $payload.performance.maximumRatioBasisPoints) -or [int]$payload.performance.maximumRatioBasisPoints -ne 11000 -or
            -not (& $isNonnegativeInteger $payload.performance.baselineP95Ns) -or
            -not (& $isNonnegativeInteger $payload.performance.candidateP95Ns) -or
            -not (& $isNonnegativeInteger $payload.performance.maximumCandidateP95Ns) -or
            [int64]$payload.performance.candidateP95Ns -gt [int64]$payload.performance.maximumCandidateP95Ns
        ) {
            return $false
        }
        return $true
    } catch {
        return $false
    }
}

function New-Gate {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("pass", "fail", "not_run", "not_applicable")][string]$Status,
        [string]$EvidenceSha256 = "",
        [string]$ReasonCode = ""
    )

    $gate = [ordered]@{ status = $Status }
    if ($EvidenceSha256) {
        if ($EvidenceSha256 -notmatch '^[0-9a-f]{64}$') {
            throw "invalid_gate_evidence_hash"
        }
        $gate.evidenceSha256 = $EvidenceSha256
    }
    if ($ReasonCode) {
        if ($ReasonCode -notmatch '^[a-z0-9][a-z0-9._-]{0,95}$') {
            throw "invalid_gate_reason_code"
        }
        $gate.reasonCode = $ReasonCode
    }
    return $gate
}

function Write-GateArtifact {
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][string]$Gate,
        [Parameter(Mandatory = $true)][ValidateSet("pass", "fail", "not_run", "not_applicable")][string]$Status,
        [Parameter(Mandatory = $true)][object[]]$Checks,
        [Parameter(Mandatory = $true)][string]$PacketId,
        [Parameter(Mandatory = $true)][string]$ParentChampionId,
        [Parameter(Mandatory = $true)][string]$SourceCommit,
        [AllowNull()][object]$DetailEvidence = $null
    )

    if ($Gate -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$' -or $Checks.Count -eq 0) {
        throw "invalid_gate_artifact"
    }
    $safeChecks = @()
    foreach ($check in $Checks) {
        $name = [string]$check.name
        $checkStatus = [string]$check.status
        if (
            $name -notmatch '^[a-z0-9][a-z0-9._-]{0,95}$' -or
            $checkStatus -notin @("pass", "fail", "not_run", "not_applicable")
        ) {
            throw "invalid_gate_artifact_check"
        }
        $safeChecks += ,([ordered]@{ name = $name; status = $checkStatus })
    }
    $nonPassingChecks = @($safeChecks | Where-Object { [string]$_.status -ne "pass" })
    if (
        ($Status -eq "pass" -and $nonPassingChecks.Count -ne 0) -or
        ($Status -ne "pass" -and $nonPassingChecks.Count -eq 0)
    ) {
        throw "gate_artifact_status_check_mismatch"
    }
    if ($null -ne $DetailEvidence) {
        if (
            [string]$DetailEvidence.kind -notmatch '^[a-z0-9][a-z0-9._-]{0,95}$' -or
            [string]$DetailEvidence.relativePath -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,255}$' -or
            [string]$DetailEvidence.relativePath -match '(^|/)\.\.(/|$)' -or
            [string]$DetailEvidence.sha256 -notmatch '^[0-9a-f]{64}$'
        ) {
            throw "invalid_gate_detail_evidence"
        }
        $DetailEvidence = [ordered]@{
            kind = [string]$DetailEvidence.kind
            relativePath = ([string]$DetailEvidence.relativePath).Replace('\', '/')
            sha256 = [string]$DetailEvidence.sha256
        }
    }
    $path = Join-Path $EvidenceRoot "gates\$Gate.json"
    $artifact = [ordered]@{
        gateArtifactContract = "InstallerResearchGateArtifactV1"
        schemaVersion = 1
        runId = $RunId
        packetId = $PacketId
        parentChampionId = $ParentChampionId
        sourceCommit = $SourceCommit
        gate = $Gate
        status = $Status
        checks = $safeChecks
        detailEvidence = $DetailEvidence
    }
    Write-JsonAtomic -Path $path -Payload $artifact
    $raw = Get-Content -LiteralPath $path -Raw
    if (
        ([System.Text.Encoding]::UTF8.GetByteCount($raw)) -gt 65536 -or
        $raw -match '(?i)file://' -or
        $raw -match '(?i)[a-z]:[\\/]' -or
        $raw -match '\\\\[^\\]'
    ) {
        throw "unsafe_gate_artifact"
    }
    return Get-Sha256File -Path $path
}

function Test-RetainedUpgradeGateDetail {
    param(
        [Parameter(Mandatory = $true)][object]$Detail,
        [Parameter(Mandatory = $true)][string]$ExpectedStatus,
        [Parameter(Mandatory = $true)][string]$ExpectedPacketId,
        [Parameter(Mandatory = $true)][string]$ExpectedParentChampionId,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceCommit
    )

    try {
        $expectedRelativePath = "packet-evidence/$ExpectedPacketId/upgrade-installed-payload.json"
        if (
            [string]$Detail.kind -ne "same-version-upgrade-tree" -or
            [string]$Detail.relativePath -ne $expectedRelativePath -or
            [string]$Detail.sha256 -notmatch '^[0-9a-f]{64}$'
        ) {
            return $false
        }
        $detailPath = Join-Path $runRoot $expectedRelativePath.Replace('/', '\')
        if (
            -not (Test-Path -LiteralPath $detailPath -PathType Leaf) -or
            (Get-Sha256File -Path $detailPath) -ne [string]$Detail.sha256
        ) {
            return $false
        }
        $raw = Get-Content -LiteralPath $detailPath -Raw
        if (
            ([System.Text.Encoding]::UTF8.GetByteCount($raw)) -gt 65536 -or
            $raw -match '(?i)file://' -or
            $raw -match '(?i)[a-z]:[\\/]' -or
            $raw -match '\\\\[^\\]'
        ) {
            return $false
        }
        $payload = $raw | ConvertFrom-Json
        if (
            [string]$payload.upgradeGateDetailContract -ne "InstallerResearchUpgradeGateDetailV1" -or
            [int]$payload.schemaVersion -ne 1 -or
            [string]$payload.runId -ne $RunId -or
            [string]$payload.packetId -ne $ExpectedPacketId -or
            [string]$payload.parentChampionId -ne $ExpectedParentChampionId -or
            [string]$payload.sourceCommit -ne $ExpectedSourceCommit -or
            [string]$payload.status -ne $ExpectedStatus -or
            $null -eq $payload.PSObject.Properties["installerExitCode"] -or
            [int]$payload.installerExitCode -lt -1
        ) {
            return $false
        }
        $failedCheck = [string]$payload.failedCheck
        $reasonCode = [string]$payload.reasonCode
        if (
            ($ExpectedStatus -eq "pass" -and ($failedCheck -or $reasonCode -or $null -ne $payload.firstDifference)) -or
            ($ExpectedStatus -ne "pass" -and (
                $failedCheck -notmatch '^[a-z0-9][a-z0-9._-]{0,95}$' -or
                $reasonCode -notmatch '^[a-z0-9][a-z0-9._-]{0,95}$'
            ))
        ) {
            return $false
        }
        if ($null -ne $payload.firstDifference) {
            $difference = $payload.firstDifference
            if (
                [string]$difference.kind -notin @("missing", "unexpected", "identity") -or
                [string]$difference.relativePath -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,255}$' -or
                [string]$difference.relativePath -match '(^|/)\.\.(/|$)' -or
                [int64]$difference.expectedLength -lt -1 -or
                [int64]$difference.actualLength -lt -1 -or
                (([string]$difference.expectedSha256) -and [string]$difference.expectedSha256 -notmatch '^[0-9a-f]{64}$') -or
                (([string]$difference.actualSha256) -and [string]$difference.actualSha256 -notmatch '^[0-9a-f]{64}$')
            ) {
                return $false
            }
        }
        return $true
    } catch {
        return $false
    }
}

function Test-RetainedGateArtifact {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedGate,
        [Parameter(Mandatory = $true)][string]$ExpectedStatus,
        [Parameter(Mandatory = $true)][string]$ExpectedPacketId,
        [Parameter(Mandatory = $true)][string]$ExpectedParentChampionId,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceCommit,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    try {
        $payload = Read-JsonObject -Path $Path -Code "retained_gate_artifact_missing"
        if (
            (Get-Sha256File -Path $Path) -ne $ExpectedSha256 -or
            [string]$payload.gateArtifactContract -ne "InstallerResearchGateArtifactV1" -or
            [int]$payload.schemaVersion -ne 1 -or
            [string]$payload.runId -ne $RunId -or
            [string]$payload.packetId -ne $ExpectedPacketId -or
            [string]$payload.parentChampionId -ne $ExpectedParentChampionId -or
            [string]$payload.sourceCommit -ne $ExpectedSourceCommit -or
            [string]$payload.gate -ne $ExpectedGate -or
            [string]$payload.status -ne $ExpectedStatus -or
            @($payload.checks).Count -eq 0
        ) {
            return $false
        }
        $nonPassingChecks = @($payload.checks | Where-Object { [string]$_.status -ne "pass" })
        if (
            ($ExpectedStatus -eq "pass" -and $nonPassingChecks.Count -ne 0) -or
            ($ExpectedStatus -ne "pass" -and $nonPassingChecks.Count -eq 0)
        ) {
            return $false
        }
        if ($ExpectedGate -eq "youtubeWorkflow") {
            $detail = $payload.detailEvidence
            if (
                $null -eq $detail -or
                [string]$detail.kind -notin @("baseline-youtube-holdout", "candidate-youtube-holdout") -or
                [string]$detail.relativePath -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,255}$' -or
                [string]$detail.sha256 -notmatch '^[0-9a-f]{64}$'
            ) {
                return $false
            }
            $detailPath = Join-Path $runRoot ([string]$detail.relativePath).Replace('/', '\')
            if (
                -not (Test-Path -LiteralPath $detailPath -PathType Leaf) -or
                (Get-Sha256File -Path $detailPath) -ne [string]$detail.sha256
            ) {
                return $false
            }
        } elseif ($ExpectedGate -eq "cleanInstallUpgradeUninstall") {
            if ($null -eq $payload.detailEvidence) {
                if ($ExpectedStatus -ne "pass" -and $ExpectedPacketId -ne "baseline-1") {
                    return $false
                }
            } elseif ($ExpectedStatus -eq "pass") {
                return $false
            } elseif (-not (Test-RetainedUpgradeGateDetail `
                -Detail $payload.detailEvidence `
                -ExpectedStatus $ExpectedStatus `
                -ExpectedPacketId $ExpectedPacketId `
                -ExpectedParentChampionId $ExpectedParentChampionId `
                -ExpectedSourceCommit $ExpectedSourceCommit)) {
                return $false
            }
        } elseif ($null -ne $payload.detailEvidence) {
            return $false
        }
        return $true
    } catch {
        return $false
    }
}

function Write-FullSuiteGateArtifact {
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][string]$Gate,
        [Parameter(Mandatory = $true)][string]$CheckName,
        [Parameter(Mandatory = $true)][string]$PacketId,
        [Parameter(Mandatory = $true)][string]$SourceCommit
    )

    if (
        $Gate -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$' -or
        $CheckName -notmatch '^[a-z0-9][a-z0-9._-]{0,95}$'
    ) {
        throw "invalid_full_suite_gate_artifact"
    }
    $path = Join-Path $EvidenceRoot "full-suite\$Gate.json"
    $artifact = [ordered]@{
        fullSuiteGateArtifactContract = "InstallerResearchFullSuiteGateArtifactV1"
        schemaVersion = 1
        runId = $RunId
        packetId = $PacketId
        sourceCommit = $SourceCommit
        gate = $Gate
        status = "pass"
        checks = @([ordered]@{ name = $CheckName; status = "pass" })
    }
    Write-JsonAtomic -Path $path -Payload $artifact
    $raw = Get-Content -LiteralPath $path -Raw
    if (
        ([System.Text.Encoding]::UTF8.GetByteCount($raw)) -gt 65536 -or
        $raw -match '(?i)file://' -or
        $raw -match '(?i)[a-z]:[\\/]' -or
        $raw -match '\\\\[^\\]'
    ) {
        throw "unsafe_full_suite_gate_artifact"
    }
    return Get-Sha256File -Path $path
}

function Invoke-BaselineToCandidateUpgradeGate {
    param(
        [Parameter(Mandatory = $true)][string]$BaselineInstaller,
        [Parameter(Mandatory = $true)][string]$CandidateInstaller,
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$DataRoot,
        [Parameter(Mandatory = $true)][string]$ScratchRoot,
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][object]$CandidateInventory,
        [Parameter(Mandatory = $true)][string]$PacketId,
        [Parameter(Mandatory = $true)][string]$ParentChampionId,
        [Parameter(Mandatory = $true)][string]$SourceCommit
    )

    $outcomes = [ordered]@{
        "baseline-install" = "not_run"
        "data-sentinel" = "not_run"
        "candidate-upgrade-install" = "not_run"
        "candidate-desktop-frontend" = "not_run"
        "candidate-meeting-capture" = "not_run"
        "candidate-installed-payload" = "not_run"
        "candidate-runtime-cleanup" = "not_run"
        "strict-uninstall" = "not_run"
        "gate-cleanup" = "not_run"
    }
    $activeCheck = "baseline-install"
    $cleanupFailed = $false
    $failedCheck = ""
    $failureReasonCode = ""
    $firstDifference = $null
    $candidateInstallerExitCode = -1
    $knownInstallerProcessIds = [System.Collections.Generic.HashSet[int]]::new()
    $installerPaths = @($BaselineInstaller, $CandidateInstaller)
    try {
        Invoke-ExactUninstaller -InstallRoot $InstallRoot
        Remove-ExactUninstallRegistryEntries -InstallRoot $InstallRoot
        foreach ($path in @($InstallRoot, $DataRoot)) {
            if (Test-Path -LiteralPath $path) {
                Remove-ScopedTree -Root $InstallerSmokeNamespace -Path $path -Code "upgrade_preclean_failed"
            }
        }

        $baselineSmokePath = Join-Path $ScratchRoot "baseline-upgrade-source-smoke.json"
        $baselineSmokeLog = Join-Path $ScratchRoot "baseline-upgrade-source-smoke.log"
        $baselineSmokeCommand = Invoke-CapturedCommand -LogPath $baselineSmokeLog -PowerShellScript -Command {
            & (Join-Path $RepoRoot "scripts\smoke_windows_installer.ps1") `
                -RepoRoot $RepoRoot `
                -PythonExecutable $Python `
                -InstallerPath $BaselineInstaller `
                -InstallDir $InstallRoot `
                -DataDir $DataRoot `
                -OutputPath $baselineSmokePath `
                -VerifyFrontend `
                -KeepInstalled
        }
        if ($baselineSmokeCommand.exitCode -ne 0 -or -not (Test-Path -LiteralPath $baselineSmokePath -PathType Leaf)) {
            throw "baseline_upgrade_source_install_failed"
        }
        $baselineSmoke = Read-JsonObject -Path $baselineSmokePath -Code "baseline_upgrade_source_smoke_invalid"
        if (-not (Test-NestedTrue -Value $baselineSmoke -PropertyPath @("ok")) -or -not (Test-NestedTrue -Value $baselineSmoke -PropertyPath @("frontend", "verified"))) {
            throw "baseline_upgrade_source_smoke_failed"
        }
        $outcomes["baseline-install"] = "pass"

        $activeCheck = "data-sentinel"
        $sentinelPath = Join-Path $DataRoot "installer-research-upgrade-sentinel.txt"
        New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
        [System.IO.File]::WriteAllText($sentinelPath, "preserve-across-baseline-candidate-upgrade`n", [System.Text.UTF8Encoding]::new($false))
        $sentinelSha256 = Get-Sha256File -Path $sentinelPath
        $outcomes["data-sentinel"] = "pass"

        $activeCheck = "candidate-upgrade-install"
        $upgradeDeadline = [DateTimeOffset]::UtcNow.AddSeconds(60)
        $process = $null
        try {
            $process = Start-Process -FilePath $CandidateInstaller `
                -ArgumentList @("/S", "/D=$InstallRoot") `
                -PassThru `
                -WindowStyle Hidden
            [void]$knownInstallerProcessIds.Add([int]$process.Id)
            # Materialize the process handle immediately so PID/exit state stays
            # bound to this exact launcher even after it exits.
            $null = $process.Handle
            $remainingLauncherMs = Get-RemainingDeadlineMilliseconds -Deadline $upgradeDeadline
            if ($remainingLauncherMs -le 0 -or -not $process.WaitForExit($remainingLauncherMs)) {
                throw "candidate_upgrade_launcher_timeout"
            }
            $candidateInstallerExitCode = [int]$process.ExitCode
            if ($candidateInstallerExitCode -ne 0) {
                throw "candidate_upgrade_installer_failed"
            }
        } finally {
            if ($null -ne $process) {
                $process.Dispose()
            }
        }
        Wait-CandidateUpgradeInstallStable `
            -KnownProcessIds $knownInstallerProcessIds `
            -InstallRoot $InstallRoot `
            -InstallerPaths $installerPaths `
            -CandidateInventory $CandidateInventory `
            -Deadline $upgradeDeadline `
            -RequiredStableSamples 3 `
            -ObservationIntervalMs 250
        if (-not (Test-Path -LiteralPath $sentinelPath -PathType Leaf) -or (Get-Sha256File -Path $sentinelPath) -ne $sentinelSha256) {
            throw "candidate_upgrade_sentinel_drift"
        }
        $outcomes["candidate-upgrade-install"] = "pass"

        $activeCheck = "candidate-installed-payload"
        Assert-InstalledPayloadMatchesInventory -InstallRoot $InstallRoot -Inventory $CandidateInventory
        $outcomes["candidate-installed-payload"] = "pass"

        $activeCheck = "candidate-desktop-frontend"
        $upgradeDesktopPath = Join-Path $ScratchRoot "candidate-upgrade-desktop-smoke.json"
        $upgradeDesktopLog = Join-Path $ScratchRoot "candidate-upgrade-desktop-smoke.log"
        $appExecutable = Resolve-InstalledAppExecutable -InstallRoot $InstallRoot
        $desktopCommand = Invoke-CapturedCommand -LogPath $upgradeDesktopLog -PowerShellScript -Command {
            & (Join-Path $RepoRoot "scripts\smoke_tauri_desktop.ps1") `
                -RepoRoot $RepoRoot `
                -ExePath $appExecutable `
                -BackendExePath (Join-Path $InstallRoot "backend\scriber-backend.exe") `
                -PythonPath $Python `
                -DataDir $DataRoot `
                -OutputPath $upgradeDesktopPath `
                -VerifyFrontend `
                -VerifyMeetingAudioDeviceTest `
                -VerifyAudioSidecarCleanup `
                -DisableDevFallback
        }
        if ($desktopCommand.exitCode -ne 0 -or -not (Test-Path -LiteralPath $upgradeDesktopPath -PathType Leaf)) {
            throw "candidate_upgrade_desktop_failed"
        }
        $upgradeDesktop = Read-JsonObject -Path $upgradeDesktopPath -Code "candidate_upgrade_desktop_invalid"
        if (-not (Test-NestedTrue -Value $upgradeDesktop -PropertyPath @("ok")) -or -not (Test-NestedTrue -Value $upgradeDesktop -PropertyPath @("frontend", "verified"))) {
            throw "candidate_upgrade_frontend_failed"
        }
        $outcomes["candidate-desktop-frontend"] = "pass"

        $activeCheck = "candidate-meeting-capture"
        if (-not (Test-NestedTrue -Value $upgradeDesktop -PropertyPath @("meetingAudioDeviceTest", "verified"))) {
            throw "candidate_upgrade_meeting_failed"
        }
        $outcomes["candidate-meeting-capture"] = "pass"

        $activeCheck = "candidate-runtime-cleanup"
        if (
            -not (Test-NestedTrue -Value $upgradeDesktop -PropertyPath @("cleanupVerified")) -or
            -not (Test-NestedTrue -Value $upgradeDesktop -PropertyPath @("audioSidecarCleanup", "verified")) -or
            @(Get-ExactInstalledProcesses -InstallRoot $InstallRoot).Count -ne 0
        ) {
            throw "candidate_upgrade_runtime_cleanup_failed"
        }
        $outcomes["candidate-runtime-cleanup"] = "pass"

        $activeCheck = "strict-uninstall"
        Invoke-ExactUninstaller -InstallRoot $InstallRoot -RequireUninstaller
        $deadline = (Get-Date).AddSeconds(15)
        do {
            $remaining = if (Test-Path -LiteralPath $InstallRoot -PathType Container) {
                @(Get-ChildItem -LiteralPath $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue)
            } else {
                @()
            }
            if ($remaining.Count -eq 0) {
                break
            }
            Start-Sleep -Milliseconds 250
        } while ((Get-Date) -lt $deadline)
        if (
            $remaining.Count -ne 0 -or
            @(Get-ExactInstalledProcesses -InstallRoot $InstallRoot).Count -ne 0 -or
            @(Get-ExactUninstallRegistryEntries -InstallRoot $InstallRoot).Count -ne 0 -or
            -not (Test-Path -LiteralPath $sentinelPath -PathType Leaf) -or
            (Get-Sha256File -Path $sentinelPath) -ne $sentinelSha256
        ) {
            throw "candidate_upgrade_strict_uninstall_failed"
        }
        $outcomes["strict-uninstall"] = "pass"
    } catch {
        $failedCheck = $activeCheck
        $failureReasonCode = Get-RedactedUpgradeGateReasonCode -Exception $_.Exception -ActiveCheck $activeCheck
        if ($outcomes.Contains($activeCheck) -and $outcomes[$activeCheck] -eq "not_run") {
            $outcomes[$activeCheck] = "fail"
        }
        $scopedCleanupReady = $true
        try {
            Invoke-ScopedInstallerProcessCleanup `
                -KnownProcessIds $knownInstallerProcessIds `
                -TargetRoot $InstallRoot `
                -InstallerPaths $installerPaths `
                -TimeoutSec 10
        } catch {
            $scopedCleanupReady = $false
        }
        if (
            $activeCheck -in @("candidate-upgrade-install", "candidate-installed-payload") -and
            $scopedCleanupReady -and
            (Test-Path -LiteralPath $InstallRoot -PathType Container)
        ) {
            try {
                $difference = Get-InstalledPayloadFirstDifference -InstallRoot $InstallRoot -Inventory $CandidateInventory
                if ($null -ne $difference) {
                    $firstDifference = $difference.firstDifference
                    if ($activeCheck -eq "candidate-installed-payload") {
                        $failureReasonCode = [string]$difference.reasonCode
                    }
                }
            } catch {
                $firstDifference = $null
            }
        }
    } finally {
        $scopedCleanupReady = $true
        try {
            Invoke-ScopedInstallerProcessCleanup `
                -KnownProcessIds $knownInstallerProcessIds `
                -TargetRoot $InstallRoot `
                -InstallerPaths $installerPaths `
                -TimeoutSec 10
        } catch {
            $cleanupFailed = $true
            $scopedCleanupReady = $false
        }
        if ($scopedCleanupReady) {
            try {
                Invoke-ExactUninstaller -InstallRoot $InstallRoot
            } catch {
                $cleanupFailed = $true
            }
            # The uninstaller may detach its NSIS cleanup worker. Drain that
            # related tree before registry or filesystem cleanup.
            try {
                Invoke-ScopedInstallerProcessCleanup `
                    -KnownProcessIds $knownInstallerProcessIds `
                    -TargetRoot $InstallRoot `
                    -InstallerPaths $installerPaths `
                    -TimeoutSec 10
            } catch {
                $cleanupFailed = $true
                $scopedCleanupReady = $false
            }
        }
        if ($scopedCleanupReady) {
            try {
                Remove-ExactUninstallRegistryEntries -InstallRoot $InstallRoot
            } catch {
                $cleanupFailed = $true
            }
            foreach ($path in @($InstallRoot, $DataRoot)) {
                try {
                    if (Test-Path -LiteralPath $path) {
                        Remove-ScopedTree -Root $InstallerSmokeNamespace -Path $path -Code "upgrade_gate_cleanup_failed"
                    }
                } catch {
                    $cleanupFailed = $true
                }
            }
        }
        if ($cleanupFailed) {
            $outcomes["gate-cleanup"] = "fail"
            $failedCheck = "gate-cleanup"
            $failureReasonCode = "upgrade_gate_cleanup_failed"
        } else {
            $outcomes["gate-cleanup"] = "pass"
        }
    }
    $checks = @(
        foreach ($name in $outcomes.Keys) {
            [ordered]@{ name = $name; status = [string]$outcomes[$name] }
        }
    )
    $status = if (@($outcomes.Values | Where-Object { $_ -ne "pass" }).Count -eq 0) { "pass" } else { "fail" }
    $detailEvidence = if ($status -eq "fail") {
        Write-UpgradeGateDetailArtifact `
            -EvidenceRoot $EvidenceRoot `
            -PacketId $PacketId `
            -ParentChampionId $ParentChampionId `
            -SourceCommit $SourceCommit `
            -Status $status `
            -FailedCheck $failedCheck `
            -ReasonCode $failureReasonCode `
            -InstallerExitCode $candidateInstallerExitCode `
            -FirstDifference $firstDifference
    } else {
        $null
    }
    return [ordered]@{
        status = $status
        checks = $checks
        detailEvidence = $detailEvidence
    }
}

function Invoke-FinalFullSuite {
    param(
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][string]$ScratchRoot,
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][object]$Toolchain,
        [Parameter(Mandatory = $true)][string]$PacketId,
        [Parameter(Mandatory = $true)][string]$SourceCommit,
        [Parameter(Mandatory = $true)][string]$ChampionSha256,
        [Parameter(Mandatory = $true)][string]$ChampionSourceTreeOid
    )

    $toolchainRoot = Split-Path -Parent $Toolchain.path
    $node = Assert-FileIdentity -Path (Join-Path $toolchainRoot "node\node.exe") -Identity $Toolchain.manifest.node -Code "full_suite_pinned_node_drift"
    $npm = Assert-FileIdentity -Path (Join-Path $toolchainRoot "node\node_modules\npm\bin\npm-cli.js") -Identity $Toolchain.manifest.npm -Code "full_suite_pinned_npm_drift"
    $rustupCommand = Get-Command rustup.exe -ErrorAction Stop
    $cargoPath = (& $rustupCommand.Source which --toolchain ([string]$Toolchain.manifest.rustToolchain) cargo).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "full_suite_pinned_cargo_unavailable"
    }
    $rustcPath = (& $rustupCommand.Source which --toolchain ([string]$Toolchain.manifest.rustToolchain) rustc).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "full_suite_pinned_rustc_unavailable"
    }
    $cargo = Assert-FileIdentity -Path $cargoPath -Identity $Toolchain.manifest.cargo -Code "full_suite_pinned_cargo_drift"
    $null = Assert-FileIdentity -Path $rustcPath -Identity $Toolchain.manifest.rustc -Code "full_suite_pinned_rustc_drift"
    $rustfmtPath = (& $rustupCommand.Source which --toolchain ([string]$Toolchain.manifest.rustToolchain) rustfmt).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "full_suite_pinned_rustfmt_unavailable"
    }
    $clippyPath = (& $rustupCommand.Source which --toolchain ([string]$Toolchain.manifest.rustToolchain) clippy-driver).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "full_suite_pinned_clippy_unavailable"
    }
    $null = Assert-FileIdentity -Path $rustfmtPath -Identity $Toolchain.manifest.rustfmt -Code "full_suite_pinned_rustfmt_drift"
    $null = Assert-FileIdentity -Path $clippyPath -Identity $Toolchain.manifest.clippyDriver -Code "full_suite_pinned_clippy_drift"

    $priorRust = $env:RUSTUP_TOOLCHAIN
    $env:RUSTUP_TOOLCHAIN = [string]$Toolchain.manifest.rustToolchain
    try {
        $commands = [ordered]@{
            pythonPytest = {
                & $Python -m pytest -q
            }
            frontendCheck = {
                Push-Location (Join-Path $RepoRoot "Frontend")
                try { & $node $npm run check } finally { Pop-Location }
            }
            frontendI18n = {
                Push-Location (Join-Path $RepoRoot "Frontend")
                try { & $node $npm run "test:i18n" } finally { Pop-Location }
            }
            frontendBuild = {
                Push-Location (Join-Path $RepoRoot "Frontend")
                try { & $node $npm run build } finally { Pop-Location }
            }
            rustCargoTest = {
                Push-Location (Join-Path $RepoRoot "Frontend\src-tauri")
                try { & $cargo test --locked } finally { Pop-Location }
            }
            rustFmt = {
                Push-Location (Join-Path $RepoRoot "Frontend\src-tauri")
                try { & $cargo fmt --check } finally { Pop-Location }
            }
            rustClippy = {
                Push-Location (Join-Path $RepoRoot "Frontend\src-tauri")
                try { & $cargo clippy --locked --all-targets --all-features -- -D warnings } finally { Pop-Location }
            }
        }
        $results = [ordered]@{}
        foreach ($name in $commands.Keys) {
            $logPath = Join-Path $ScratchRoot "full-suite-$name.log"
            $results[$name] = Invoke-CapturedCommand -LogPath $logPath -Command ([scriptblock]$commands[$name])
        }
    } finally {
        if ($null -eq $priorRust) {
            Remove-Item Env:RUSTUP_TOOLCHAIN -ErrorAction SilentlyContinue
        } else {
            $env:RUSTUP_TOOLCHAIN = $priorRust
        }
    }
    if (@($results.Values | Where-Object { [int]$_.exitCode -ne 0 }).Count -ne 0) {
        throw "final_full_suite_failed"
    }
    $checkNames = [ordered]@{
        pythonPytest = "project-pytest"
        frontendCheck = "frontend-check"
        frontendI18n = "frontend-i18n"
        frontendBuild = "frontend-build"
        rustCargoTest = "rust-cargo-test"
        rustFmt = "rust-fmt"
        rustClippy = "rust-clippy"
    }
    $gates = [ordered]@{}
    foreach ($name in $commands.Keys) {
        $artifactSha = Write-FullSuiteGateArtifact `
            -EvidenceRoot $EvidenceRoot `
            -Gate $name `
            -CheckName ([string]$checkNames[$name]) `
            -PacketId $PacketId `
            -SourceCommit $SourceCommit
        $gates[$name] = [ordered]@{
            status = "pass"
            evidenceSha256 = $artifactSha
        }
    }
    $path = Join-Path $EvidenceRoot "full-suite-evidence.json"
    $evidence = [ordered]@{
        fullSuiteEvidenceContract = "InstallerResearchFullSuiteEvidenceV1"
        schemaVersion = 1
        runId = $RunId
        packetId = $PacketId
        sourceCommit = $SourceCommit
        championSha256 = $ChampionSha256
        championSourceTreeOid = $ChampionSourceTreeOid
        gates = $gates
    }
    Write-JsonAtomic -Path $path -Payload $evidence
    $raw = Get-Content -LiteralPath $path -Raw
    if (
        ([System.Text.Encoding]::UTF8.GetByteCount($raw)) -gt 65536 -or
        $raw -match '(?i)file://' -or
        $raw -match '(?i)[a-z]:[\\/]' -or
        $raw -match '\\\\[^\\]'
    ) {
        throw "unsafe_full_suite_evidence"
    }
    return Get-Sha256File -Path $path
}

$canonicalRunId = $null
$runRoot = $null
$buildRoot = $null
$scratchRoot = $null
$installRoot = $null
$dataRoot = $null
$savedSigningEnvironment = @{}
$savedDeterministicBuildEnvironment = @{}
$exitCode = 2
$summary = $null
$failureCode = "packet_producer_failed"

foreach ($name in $SigningEnvironmentNames) {
    $item = Get-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    $savedSigningEnvironment[$name] = if ($null -eq $item) { $null } else { $item.Value }
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}

# Installer research compares independently rebuilt payloads byte-for-byte and
# by compressed length.  Pin all build-time entropy before any Python, Rust,
# Vite, or Tauri producer runs. /Brepro makes the MSVC PE timestamp and CodeView
# identity content-derived; the fixed hash seed stabilizes PyInstaller archive
# order. CARGO_ENCODED_RUSTFLAGS must be absent because Cargo gives it
# precedence over RUSTFLAGS.
$deterministicBuildEnvironment = [ordered]@{
    PYTHONHASHSEED = "0"
    SOURCE_DATE_EPOCH = "946684800"
    RUSTFLAGS = "-C link-arg=/Brepro"
    CARGO_INCREMENTAL = "0"
}
foreach ($name in @($deterministicBuildEnvironment.Keys) + @("CARGO_ENCODED_RUSTFLAGS")) {
    $item = Get-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    $savedDeterministicBuildEnvironment[$name] = if ($null -eq $item) { $null } else { $item.Value }
}
foreach ($name in $deterministicBuildEnvironment.Keys) {
    Set-Item -LiteralPath "Env:$name" -Value ([string]$deterministicBuildEnvironment[$name])
}
Remove-Item -LiteralPath "Env:CARGO_ENCODED_RUSTFLAGS" -ErrorAction SilentlyContinue

try {
    $canonicalRunId = Resolve-CanonicalRunId -Value $RunId
    $RunId = $canonicalRunId
    $runRoot = Assert-StrictDescendant -Root $ResearchNamespace -Path (Join-Path $ResearchNamespace $RunId) -Code "unsafe_run_root"
    if (-not (Test-Path -LiteralPath $runRoot -PathType Container)) {
        throw "run_root_missing"
    }
    $null = Assert-NoReparsePath -Root $RepoRoot -Path $runRoot -Code "unsafe_run_root"
    $session = Read-JsonObject -Path (Join-Path $runRoot "snapshots\session-init.json") -Code "session_init_missing"
    if (
        [string]$session.sessionInitContract -ne "InstallerSizeResearchSessionInitV1" -or
        [int]$session.schemaVersion -ne 1 -or
        [string]$session.profile -ne "installer-size" -or
        [string]$session.runId -ne $RunId
    ) {
        throw "session_identity_mismatch"
    }
    $pendingPath = Join-Path $runRoot "pending-packet.json"
    $packet = Read-JsonObject -Path $pendingPath -Code "pending_packet_missing"
    $packetId = Assert-SafePacketId -Value ([string]$packet.packetId)
    $immutablePacketPath = Join-Path $runRoot "packets\$packetId.json"
    $immutablePacket = Read-JsonObject -Path $immutablePacketPath -Code "immutable_packet_missing"
    if ((Get-Sha256File -Path $pendingPath) -ne (Get-Sha256File -Path $immutablePacketPath)) {
        throw "pending_packet_drift"
    }
    if (
        [string]$packet.packetContract -ne "InstallerResearchPacketV1" -or
        [int]$packet.schemaVersion -ne 1 -or
        [string]$packet.runId -ne $RunId
    ) {
        throw "packet_contract_mismatch"
    }
    $sourceCommit = [string]$packet.sourceCommit
    if ($sourceCommit -notmatch '^(?:[0-9a-f]{40}|[0-9a-f]{64})$') {
        throw "packet_source_commit_invalid"
    }
    $gitStatus = @(& git -C $RepoRoot status --porcelain=v1)
    if ($LASTEXITCODE -ne 0 -or $gitStatus.Count -ne 0) {
        throw "dirty_worktree"
    }
    $head = (& git -C $RepoRoot rev-parse HEAD).Trim()
    $branch = (& git -C $RepoRoot branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -ne $sourceCommit -or -not $branch -or $branch -ne [string]$session.sourceBranch) {
        throw "git_session_identity_mismatch"
    }
    & git -C $RepoRoot cat-file -e "$sourceCommit^{commit}" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "source_commit_not_committed"
    }

    $action = $packet.action
    $expectedKind = "measure-candidate"
    $expectedReplica = 0
    if ($Mode.StartsWith("baseline-")) {
        $expectedKind = "baseline-replica"
        $expectedReplica = [int]$Mode.Substring($Mode.Length - 1)
    } elseif ($Mode.StartsWith("final-")) {
        $expectedKind = "final-replica"
        $expectedReplica = [int]$Mode.Substring($Mode.Length - 1)
    }
    if ([string]$action.kind -ne $expectedKind) {
        throw "packet_mode_kind_mismatch"
    }
    if ($expectedReplica -gt 0 -and [int]$action.replica -ne $expectedReplica) {
        throw "packet_mode_replica_mismatch"
    }
    if ($Mode -in @("baseline-1", "final-1") -and $RunTiming) {
        throw "timing_not_allowed_for_mode"
    }
    if ($Mode -eq "final-2" -and -not $RunTiming) {
        throw "final_timing_required"
    }
    $expectedResultRelative = if ($expectedKind -eq "baseline-replica") {
        "baselines/baseline-replica-$expectedReplica.json"
    } else {
        "packet-results/$packetId.json"
    }
    if (([string]$action.resultRelativePath).Replace('\', '/') -ne $expectedResultRelative) {
        throw "packet_result_path_mismatch"
    }
    $resultPath = Join-Path $runRoot ($expectedResultRelative.Replace('/', '\'))
    if (Test-Path -LiteralPath $resultPath) {
        throw "packet_result_exists"
    }

    $config = Read-JsonObject -Path $ProfileConfig -Code "profile_config_missing"
    if (
        [int]$config.schemaVersion -ne 1 -or
        [string]$config.profile -ne "installer-size" -or
        ($config.baselineReplicas -is [bool]) -or
        [int]$config.baselineReplicas -ne 1 -or
        [string]$config.referenceCompression -ne "bzip2" -or
        [int]$config.minimumInstallerReduction.bytes -ne 262144 -or
        [double]$config.minimumInstallerReduction.fraction -ne 0.0025 -or
        [double]$config.maximumInstallRegressionFraction -ne 0.05
    ) {
        throw "profile_policy_drift"
    }
    $toolchain = Assert-ToolchainManifest -RunRoot $runRoot
    $isBaseline = $expectedKind -eq "baseline-replica"
    $environment = Ensure-HermeticEnvironment -RunRoot $runRoot -SourceCommit $sourceCommit -Baseline $isBaseline
    $productVersion = Get-ProductVersion
    $expectedInstallerName = "Scriber_${productVersion}_x64-setup.exe"
    $canonicalInstaller = Join-Path $RepoRoot "Frontend\src-tauri\target\release\bundle\nsis\$expectedInstallerName"

    $evidenceRoot = Join-Path $runRoot "packet-evidence\$packetId"
    $buildRoot = Join-Path $runRoot "builds\$packetId"
    $payloadRoot = Join-Path $runRoot "payloads\$packetId"
    $artifactRoot = Join-Path $runRoot "artifacts\$packetId"
    $candidateHoldoutPath = Join-Path $evidenceRoot "youtube-holdouts-candidate.json"
    if (Test-Path -LiteralPath $evidenceRoot -PathType Container) {
        $preexisting = @(Get-ChildItem -LiteralPath $evidenceRoot -Force)
        if ($preexisting.Count -ne 0) {
            throw "packet_evidence_exists"
        }
    } else {
        New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null
    }
    if (
        (Test-Path -LiteralPath $payloadRoot) -or
        (Test-Path -LiteralPath $artifactRoot) -or
        (Test-Path -LiteralPath $buildRoot)
    ) {
        throw "packet_build_outputs_exist"
    }
    New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null

    $comparisonKind = "payload"
    $compression = "bzip2"
    $parentChampionId = if ($expectedKind -eq "measure-candidate") { [string]$packet.parentChampionId } else { "baseline" }
    $parentInventory = $null
    $champion = $null
    if ($expectedKind -eq "measure-candidate") {
        $comparisonKind = [string]$action.comparisonKind
        $compression = [string]$action.compression
        if ($comparisonKind -notin @("payload", "compression")) {
            throw "candidate_comparison_kind_invalid"
        }
        if ($comparisonKind -eq "payload" -and $compression -ne "bzip2") {
            throw "payload_candidate_must_use_bzip2"
        }
        if ($comparisonKind -eq "compression" -and $compression -notin @("bzip2", "zlib", "lzma")) {
            throw "compression_candidate_format_invalid"
        }
        $parentInventory = Get-ParentInventory -RunRoot $runRoot -ParentId $parentChampionId
        if ($comparisonKind -eq "compression" -and [string]$parentInventory.sourceCommit -ne $sourceCommit) {
            throw "compression_candidate_source_drift"
        }
        if (
            $comparisonKind -eq "compression" -and
            [string]$action.payloadTreeSha256 -ne [string]$parentInventory.payload.staged.semanticTreeSha256
        ) {
            throw "compression_candidate_payload_binding_mismatch"
        }
    } elseif ($expectedKind -eq "final-replica") {
        $champion = Read-JsonObject -Path (Join-Path $runRoot "champion.json") -Code "champion_missing"
        $championTreeOid = [string]$action.championSourceTreeOid
        if ($championTreeOid -notmatch '^(?:[0-9a-f]{40}|[0-9a-f]{64})$') {
            throw "final_champion_tree_binding_invalid"
        }
        $currentTreeOid = (& git -C $RepoRoot rev-parse "$sourceCommit^{tree}").Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "final_current_tree_unavailable"
        }
        $championSourceCommit = [string]$champion.sourceCommit
        if ($championSourceCommit -notmatch '^(?:[0-9a-f]{40}|[0-9a-f]{64})$') {
            throw "final_champion_source_invalid"
        }
        $championCommitTreeOid = (& git -C $RepoRoot rev-parse "$championSourceCommit^{tree}").Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "final_champion_tree_unavailable"
        }
        if (
            [string]$champion.decision -ne "keep" -or
            [string]$action.championSha256 -ne (Get-Sha256File -Path (Join-Path $runRoot "champion.json")) -or
            $currentTreeOid -ne $championTreeOid -or
            $championCommitTreeOid -ne $championTreeOid
        ) {
            throw "final_champion_binding_mismatch"
        }
        $parentChampionId = [string]$champion.packetId
        $comparisonKind = [string]$champion.comparisonKind
        $compression = [string]$champion.compression
        if ($comparisonKind -notin @("payload", "compression") -or $compression -notin @("bzip2", "zlib", "lzma")) {
            throw "final_champion_shape_invalid"
        }
    }

    $buildCommandEvidenceSha = ""
    $producedInstaller = $canonicalInstaller
    if ($comparisonKind -eq "compression") {
        $payloadSourceId = if ($expectedKind -eq "measure-candidate") { $parentChampionId } else { [string]$champion.packetId }
        if ($payloadSourceId -eq "baseline") {
            $payloadSourceId = "baseline-1"
        }
        $sourcePayload = Join-Path $runRoot "payloads\$payloadSourceId"
        $sourceInventory = if ($expectedKind -eq "measure-candidate") {
            $parentInventory
        } else {
            Read-JsonObject -Path (Join-Path $runRoot "packet-evidence\$($champion.packetId)\inventory.json") -Code "champion_inventory_missing"
        }
        Assert-PayloadMatchesInventory -PayloadRoot $sourcePayload -Inventory $sourceInventory
        Copy-PlainPayload -Source $sourcePayload -Destination $payloadRoot -RunRoot $runRoot
        Assert-PayloadMatchesInventory -PayloadRoot $payloadRoot -Inventory $sourceInventory
        $repackResult = Invoke-CompressionRepack `
            -BuildRoot $buildRoot `
            -PayloadRoot $payloadRoot `
            -Compression $compression `
            -Python $environment.python `
            -Toolchain $toolchain `
            -ExpectedInstallerName $expectedInstallerName
        $buildCommandEvidenceSha = [string]$repackResult.evidenceSha256
        $producedInstaller = [string]$repackResult.installerPath
    } else {
        $buildCommandEvidenceSha = Invoke-FullInstallerBuild `
            -BuildRoot $buildRoot `
            -Python $environment.python `
            -ToolchainManifest $toolchain.path `
            -ExpectedInstaller $canonicalInstaller
        $staged = Join-Path $buildRoot "payload"
        $null = Assert-NoReparsePath -Root $buildRoot -Path $staged -Code "unsafe_staged_payload" -Recurse
        if (-not (Test-Path -LiteralPath $staged -PathType Container)) {
            throw "staged_payload_missing"
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $payloadRoot) -Force | Out-Null
        Move-Item -LiteralPath $staged -Destination $payloadRoot
    }
    $postBuildEnvironment = Ensure-HermeticEnvironment `
        -RunRoot $runRoot `
        -SourceCommit $sourceCommit `
        -Baseline $isBaseline `
        -VerifyOnly
    if (
        [string]$postBuildEnvironment.productDependenciesSha256 -ne [string]$environment.productDependenciesSha256 -or
        [string]$postBuildEnvironment.manifestSha256 -ne [string]$environment.manifestSha256
    ) {
        throw "environment_identity_changed_after_build"
    }
    $environment = $postBuildEnvironment
    $postBuildToolchain = Assert-ToolchainManifest -RunRoot $runRoot
    if ([string]$postBuildToolchain.sha256 -ne [string]$toolchain.sha256) {
        throw "toolchain_identity_changed_after_build"
    }
    $toolchain = $postBuildToolchain
    $null = Assert-NoReparsePath -Root $runRoot -Path $payloadRoot -Code "unsafe_packet_payload" -Recurse
    $archivedInstaller = Join-Path $artifactRoot $expectedInstallerName
    Copy-Item -LiteralPath $producedInstaller -Destination $archivedInstaller -Force
    if ((Get-Sha256File -Path $producedInstaller) -ne (Get-Sha256File -Path $archivedInstaller)) {
        throw "installer_archive_copy_drift"
    }
    $buildEvidencePath = Join-Path $evidenceRoot "build-evidence.json"
    $buildEvidence = [ordered]@{
        buildEvidenceContract = "InstallerResearchBuildEvidenceV1"
        schemaVersion = 1
        runId = $RunId
        packetId = $packetId
        sourceCommit = $sourceCommit
        comparisonKind = $comparisonKind
        compression = $compression
        status = "pass"
        checks = [ordered]@{
            producer = [ordered]@{ status = "pass"; evidenceSha256 = $buildCommandEvidenceSha }
            payload = [ordered]@{ status = "pass"; treeSha256 = Get-PlainTreeIdentitySha256 -Root $payloadRoot }
            notices = [ordered]@{ status = "pass"; sha256 = Get-Sha256File -Path (Join-Path $payloadRoot "THIRD_PARTY_NOTICES.md") }
            installer = [ordered]@{
                status = "pass"
                name = $expectedInstallerName
                length = [int64](Get-Item -LiteralPath $archivedInstaller).Length
                sha256 = Get-Sha256File -Path $archivedInstaller
            }
            toolchain = [ordered]@{ status = "pass"; sha256 = $toolchain.sha256 }
        }
    }
    Write-JsonAtomic -Path $buildEvidencePath -Payload $buildEvidence
    $buildEvidenceSha = Get-Sha256File -Path $buildEvidencePath

    $scratchToken = (Get-Sha256Text -Value "$RunId|$packetId").Substring(0, 16)
    $smokeNamespace = Assert-NoReparsePath -Root $RepoRoot -Path $InstallerSmokeNamespace -Code "unsafe_smoke_namespace"
    $scratchRoot = Join-Path $smokeNamespace "research-$scratchToken"
    $installRoot = Join-Path $smokeNamespace "research-$scratchToken-install"
    $dataRoot = Join-Path $smokeNamespace "research-$scratchToken-data"
    foreach ($path in @($scratchRoot, $installRoot, $dataRoot)) {
        $null = Assert-NoReparsePath -Root $smokeNamespace -Path $path -Code "unsafe_smoke_scratch"
        if (Test-Path -LiteralPath $path) {
            throw "smoke_scratch_exists"
        }
    }
    New-Item -ItemType Directory -Path $scratchRoot -Force | Out-Null
    $installedSmokePath = Join-Path $scratchRoot "installed-smoke.json"
    $installedSmokeLog = Join-Path $scratchRoot "installed-smoke.log"
    $installedSmokeCommand = Invoke-CapturedCommand -LogPath $installedSmokeLog -PowerShellScript -Command {
        & (Join-Path $RepoRoot "scripts\smoke_windows_installer.ps1") `
            -RepoRoot $RepoRoot `
            -PythonExecutable $environment.python `
            -InstallerPath $archivedInstaller `
            -InstallDir $installRoot `
            -DataDir $dataRoot `
            -OutputPath $installedSmokePath `
            -VerifyFrontend `
            -VerifyMediaPreparation `
            -VerifyMeetingAudioDeviceTest `
            -KeepInstalled
    }
    $installedSmoke = $null
    if (Test-Path -LiteralPath $installedSmokePath -PathType Leaf) {
        try {
            $installedSmoke = Read-JsonObject -Path $installedSmokePath -Code "installed_smoke_invalid"
        } catch {
            $installedSmoke = $null
        }
    }
    $desktopSmokePassed = (
        $installedSmokeCommand.exitCode -eq 0 -and
        (Test-NestedTrue -Value $installedSmoke -PropertyPath @("ok")) -and
        (Test-NestedTrue -Value $installedSmoke -PropertyPath @("frontend", "verified"))
    )
    $mediaSmokePassed = (
        $desktopSmokePassed -and
        (Test-NestedTrue -Value $installedSmoke -PropertyPath @("mediaPreparation", "verified")) -and
        (Test-NestedTrue -Value $installedSmoke -PropertyPath @("mediaPreparation", "report", "ok"))
    )
    $meetingSmokePassed = $desktopSmokePassed -and (Test-NestedTrue -Value $installedSmoke -PropertyPath @("meetingAudioDeviceTest", "verified"))
    if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) {
        throw "installed_payload_missing"
    }
    if ($expectedKind -eq "baseline-replica" -and (-not $desktopSmokePassed -or -not $mediaSmokePassed -or -not $meetingSmokePassed)) {
        throw "baseline_installed_smoke_failed"
    }

    # Baseline/final inventories stay provisional until every required gate,
    # final-suite command, and timing check has succeeded.  Publishing directly
    # to resultRelativePath earlier would make a transient late failure look
    # like accepted immutable evidence and block a fresh replica packet.
    $inventoryPath = Join-Path $evidenceRoot "inventory.json"
    $inventoryLog = Join-Path $scratchRoot "inventory.log"
    $buildRootSha = Get-BuildRootIdentitySha256 -Path $payloadRoot
    $inventoryCommand = Invoke-CapturedCommand -LogPath $inventoryLog -Command {
        & $environment.python (Join-Path $RepoRoot "scripts\installer_research.py") inventory `
            --run-id $RunId `
            --source-commit $sourceCommit `
            --replica-id $packetId `
            --build-root-sha256 $buildRootSha `
            --staged-root $payloadRoot `
            --backend-exe (Join-Path $payloadRoot "backend\scriber-backend.exe") `
            --component-map $ComponentMap `
            --installer $archivedInstaller `
            --installed-root $installRoot `
            --product-version $productVersion `
            --compression $compression `
            --toolchain-hash $toolchain.sha256 `
            --output $inventoryPath
    }
    if ($inventoryCommand.exitCode -ne 0 -or -not (Test-Path -LiteralPath $inventoryPath -PathType Leaf)) {
        throw "inventory_failed"
    }
    $inventory = Read-JsonObject -Path $inventoryPath -Code "inventory_invalid"
    if (
        $inventory.ok -ne $true -or
        [string]$inventory.runId -ne $RunId -or
        [string]$inventory.sourceCommit -ne $sourceCommit -or
        [string]$inventory.buildProvenance.replicaId -ne $packetId -or
        [string]$inventory.compression -ne $compression
    ) {
        throw "inventory_binding_failed"
    }

    $runtimeLog = Join-Path $scratchRoot "frozen-runtime-imports.log"
    $runtimeGateCommand = Invoke-CapturedCommand -LogPath $runtimeLog -Command {
        & (Join-Path $installRoot "backend\scriber-backend.exe") --runtime-import-check
    }
    $audioOutput = Join-Path $scratchRoot "audio-synthetic.json"
    $audioLog = Join-Path $scratchRoot "audio-synthetic.log"
    $audioGateCommand = Invoke-CapturedCommand -LogPath $audioLog -Command {
        & $environment.python (Join-Path $RepoRoot "scripts\smoke_rust_audio_sidecar.py") `
            --sidecar-exe (Join-Path $installRoot "scriber-audio-sidecar.exe") `
            --mode synthetic `
            --duration-sec 1.0 `
            --skip-selected-hash `
            --output $audioOutput
    }
    $audioReport = $null
    if (Test-Path -LiteralPath $audioOutput -PathType Leaf) {
        try { $audioReport = Read-JsonObject -Path $audioOutput -Code "audio_report_invalid" } catch { $audioReport = $null }
    }

    $diarizationOutput = Join-Path $scratchRoot "diarization.json"
    $diarizationLog = Join-Path $scratchRoot "diarization.log"
    $diarizationCommand = Invoke-CapturedCommand -LogPath $diarizationLog -Command {
        & $environment.python (Join-Path $RepoRoot "scripts\smoke_diarization_worker_resource.py") `
            --root (Join-Path $installRoot "backend") `
            --output $diarizationOutput
    }
    $diarizationReport = $null
    if (Test-Path -LiteralPath $diarizationOutput -PathType Leaf) {
        try { $diarizationReport = Read-JsonObject -Path $diarizationOutput -Code "diarization_report_invalid" } catch { $diarizationReport = $null }
    }

    $pytestLog = Join-Path $scratchRoot "export-license-tests.log"
    $pytestGateCommand = Invoke-CapturedCommand -LogPath $pytestLog -Command {
        & $environment.python -m pytest -q `
            (Join-Path $RepoRoot "tests\test_meeting_export.py") `
            (Join-Path $RepoRoot "tests\perf\test_upload_export_baseline_script.py") `
            (Join-Path $RepoRoot "tests\test_sidecar_metadata_privacy.py")
    }

    $holdoutSnapshot = Join-Path $runRoot "preflight\youtube-holdouts.snapshot.json"
    $youtubeStatus = "fail"
    $youtubeReason = "deno_holdout_invalid"
    $youtubeChecks = @([ordered]@{ name = "preflight-six-case-snapshot"; status = "fail" })
    $baselineDocument = if (Test-Path -LiteralPath (Join-Path $runRoot "baseline.json") -PathType Leaf) {
        Read-JsonObject -Path (Join-Path $runRoot "baseline.json") -Code "baseline_invalid"
    } else {
        $null
    }
    # Candidate source changes can affect the frozen YouTube path without
    # changing either coarse component bucket (for example, removing an
    # optional yt-dlp dependency from the PyInstaller graph).  Consequently
    # every candidate and every final replica must produce fresh installed,
    # paired holdout evidence.  Only baseline replicas may rely on their
    # run-local Deno preflight plus the fact that they are the pre-change stack.
    $requiresCurrentHoldout = $expectedKind -in @("measure-candidate", "final-replica")
    if (Test-HoldoutSnapshot -Path $holdoutSnapshot -ExpectedContract "InstallerSizeYoutubeHoldoutsV1" -ExpectedRunId $RunId) {
        if (-not $requiresCurrentHoldout) {
            $youtubeStatus = "pass"
            $youtubeReason = ""
            $youtubeChecks = @(
                [ordered]@{ name = "preflight-six-case-snapshot"; status = "pass" },
                [ordered]@{ name = "baseline-prechange-stack"; status = "pass" }
            )
        } else {
            $baselineInventoryPath = Join-Path $runRoot "baselines\baseline-replica-1.json"
            $parentInventoryPath = if ($parentChampionId -eq "baseline") {
                $baselineInventoryPath
            } else {
                Join-Path $runRoot "packet-evidence\$parentChampionId\inventory.json"
            }
            $baselineEnvironment = Ensure-HermeticEnvironment -RunRoot $runRoot -SourceCommit $sourceCommit -Baseline $true
            $candidateHoldoutLog = Join-Path $scratchRoot "youtube-holdouts-candidate.log"
            $candidateHoldoutCommand = Invoke-CapturedCommand -LogPath $candidateHoldoutLog -Command {
                & $baselineEnvironment.python (Join-Path $RepoRoot "scripts\validate_installer_youtube_candidate_holdouts.py") `
                    --repo-root $RepoRoot `
                    --run-id $RunId `
                    --packet-id $packetId `
                    --parent-champion-id $parentChampionId `
                    --source-commit $sourceCommit `
                    --baseline-root (Join-Path $runRoot "payloads\baseline-1") `
                    --candidate-root $installRoot `
                    --baseline-inventory $baselineInventoryPath `
                    --parent-inventory $parentInventoryPath `
                    --candidate-inventory $inventoryPath `
                    --scratch-root $scratchRoot `
                    --fixture (Join-Path $RepoRoot "scripts\perf\profiles\installer-size\youtube-holdouts.json") `
                    --timeout-seconds 120 `
                    --output $candidateHoldoutPath
            }
            $candidateHoldoutPassed = (
                $candidateHoldoutCommand.exitCode -eq 0 -and
                (Test-CandidateHoldoutEvidence `
                    -Path $candidateHoldoutPath `
                    -ExpectedRunId $RunId `
                    -ExpectedPacketId $packetId `
                    -ExpectedParentChampionId $parentChampionId `
                    -ExpectedSourceCommit $sourceCommit `
                    -BaselineInventoryPath $baselineInventoryPath `
                    -ParentInventoryPath $parentInventoryPath `
                    -CandidateInventoryPath $inventoryPath)
            )
            if ($candidateHoldoutPassed) {
                $youtubeStatus = "pass"
                $youtubeReason = ""
                $youtubeChecks = @(
                    [ordered]@{ name = "preflight-six-case-snapshot"; status = "pass" },
                    [ordered]@{ name = "paired-installed-js-holdout"; status = "pass" },
                    [ordered]@{ name = "current-packet-binding"; status = "pass" }
                )
            } else {
                $reportedHoldoutStatus = "not_run"
                if (Test-Path -LiteralPath $candidateHoldoutPath -PathType Leaf) {
                    try {
                        $candidateHoldoutReport = Read-JsonObject -Path $candidateHoldoutPath -Code "candidate_holdout_invalid"
                        if ([string]$candidateHoldoutReport.status -eq "fail") {
                            $reportedHoldoutStatus = "fail"
                        }
                    } catch {
                        $reportedHoldoutStatus = "not_run"
                    }
                }
                $youtubeStatus = $reportedHoldoutStatus
                $youtubeReason = if ($reportedHoldoutStatus -eq "fail") { "candidate_holdout_failed" } else { "candidate_holdout_not_run" }
                $youtubeChecks = @(
                    [ordered]@{ name = "preflight-six-case-snapshot"; status = "pass" },
                    [ordered]@{ name = "paired-installed-js-holdout"; status = $reportedHoldoutStatus },
                    [ordered]@{ name = "current-packet-binding"; status = $reportedHoldoutStatus }
                )
            }
        }
    }

    $upgradeGate = $null
    if ($expectedKind -eq "baseline-replica") {
        $cleanupSmokePath = Join-Path $scratchRoot "baseline-self-upgrade-uninstall-smoke.json"
        $cleanupSmokeLog = Join-Path $scratchRoot "baseline-self-upgrade-uninstall-smoke.log"
        $cleanupSmokeCommand = Invoke-CapturedCommand -LogPath $cleanupSmokeLog -PowerShellScript -Command {
            & (Join-Path $RepoRoot "scripts\smoke_windows_installer.ps1") `
                -RepoRoot $RepoRoot `
                -PythonExecutable $environment.python `
                -InstallerPath $archivedInstaller `
                -InstallDir $installRoot `
                -DataDir $dataRoot `
                -OutputPath $cleanupSmokePath `
                -SimulateUpgrade `
                -VerifyUninstall
        }
        $cleanupSmoke = $null
        if (Test-Path -LiteralPath $cleanupSmokePath -PathType Leaf) {
            try { $cleanupSmoke = Read-JsonObject -Path $cleanupSmokePath -Code "cleanup_smoke_invalid" } catch { $cleanupSmoke = $null }
        }
        $selfUpgradePassed = (
            $cleanupSmokeCommand.exitCode -eq 0 -and
            (Test-NestedTrue -Value $cleanupSmoke -PropertyPath @("upgrade", "verified")) -and
            (Test-NestedTrue -Value $cleanupSmoke -PropertyPath @("uninstall", "verified"))
        )
        $upgradeGate = [ordered]@{
            status = if ($selfUpgradePassed) { "pass" } else { "fail" }
            checks = @(
                [ordered]@{ name = "baseline-self-upgrade"; status = if ($selfUpgradePassed) { "pass" } else { "fail" } },
                [ordered]@{ name = "strict-uninstall"; status = if ($selfUpgradePassed) { "pass" } else { "fail" } }
            )
        }
    } else {
        if ($null -eq $baselineDocument -or $null -eq $baselineDocument.inventory) {
            throw "accepted_baseline_missing_for_upgrade"
        }
        $baselineInstaller = Get-InstallerArchivePath -RunRoot $runRoot -PacketId "baseline-1" -Inventory $baselineDocument.inventory
        $upgradeGate = Invoke-BaselineToCandidateUpgradeGate `
            -BaselineInstaller $baselineInstaller `
            -CandidateInstaller $archivedInstaller `
            -InstallRoot $installRoot `
            -DataRoot $dataRoot `
            -ScratchRoot $scratchRoot `
            -EvidenceRoot $evidenceRoot `
            -Python $environment.python `
            -CandidateInventory $inventory `
            -PacketId $packetId `
            -ParentChampionId $parentChampionId `
            -SourceCommit $sourceCommit
    }

    $noticeOk = (
        (Get-Sha256File -Path (Join-Path $payloadRoot "THIRD_PARTY_NOTICES.md")) -eq
        (Get-Sha256File -Path (Join-Path $RepoRoot "THIRD_PARTY_NOTICES.md"))
    )
    $manifestPaths = @(
        (Join-Path $payloadRoot "backend\sidecar-build-metadata.json"),
        (Join-Path $payloadRoot "backend\runtime-layer-manifest.json")
    )
    $manifestOk = @($manifestPaths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }).Count -eq $manifestPaths.Count
    $runtimeStatus = if ($runtimeGateCommand.exitCode -eq 0) { "pass" } else { "fail" }
    $mediaStatus = if ($mediaSmokePassed) { "pass" } else { "fail" }
    $liveMicPassed = $audioGateCommand.exitCode -eq 0 -and (Test-NestedTrue -Value $audioReport -PropertyPath @("ok"))
    $liveMicStatus = if ($liveMicPassed) { "pass" } else { "fail" }
    $meetingStatus = if ($meetingSmokePassed) { "pass" } else { "fail" }
    $diarizationPassed = $diarizationCommand.exitCode -eq 0 -and (Test-NestedTrue -Value $diarizationReport -PropertyPath @("ok"))
    $diarizationStatus = if ($diarizationPassed) { "pass" } else { "fail" }
    $exportStatus = if ($pytestGateCommand.exitCode -eq 0) { "pass" } else { "fail" }
    $desktopStatus = if ($desktopSmokePassed) { "pass" } else { "fail" }
    $licenseStatus = if ($noticeOk -and $manifestOk -and $pytestGateCommand.exitCode -eq 0) { "pass" } else { "fail" }

    $youtubeDetailEvidence = if ($requiresCurrentHoldout) {
        if (-not (Test-Path -LiteralPath $candidateHoldoutPath -PathType Leaf)) {
            $null
        } else {
            [ordered]@{
                kind = "candidate-youtube-holdout"
                relativePath = "packet-evidence/$packetId/youtube-holdouts-candidate.json"
                sha256 = Get-Sha256File -Path $candidateHoldoutPath
            }
        }
    } else {
        [ordered]@{
            kind = "baseline-youtube-holdout"
            relativePath = "preflight/youtube-holdouts.snapshot.json"
            sha256 = Get-Sha256File -Path $holdoutSnapshot
        }
    }

    $gateDefinitions = [ordered]@{
        frozenRuntimeImports = [ordered]@{ status = $runtimeStatus; reason = "frozen_runtime_import_failed"; checks = @([ordered]@{ name = "runtime-import-check"; status = $runtimeStatus }) }
        mediaPreparation = [ordered]@{ status = $mediaStatus; reason = "media_preparation_failed"; checks = @([ordered]@{ name = "installed-media-preparation"; status = $mediaStatus }) }
        youtubeWorkflow = [ordered]@{ status = $youtubeStatus; reason = $youtubeReason; checks = $youtubeChecks; detailEvidence = $youtubeDetailEvidence }
        liveMic = [ordered]@{ status = $liveMicStatus; reason = "synthetic_live_mic_failed"; checks = @([ordered]@{ name = "synthetic-audio-sidecar"; status = $liveMicStatus }) }
        meetingCapture = [ordered]@{ status = $meetingStatus; reason = "meeting_capture_failed"; checks = @([ordered]@{ name = "installed-meeting-device-test"; status = $meetingStatus }) }
        diarization = [ordered]@{ status = $diarizationStatus; reason = "diarization_smoke_failed"; checks = @([ordered]@{ name = "installed-diarization-worker"; status = $diarizationStatus }) }
        pdfDocxExport = [ordered]@{ status = $exportStatus; reason = "export_tests_failed"; checks = @([ordered]@{ name = "pdf-docx-export-tests"; status = $exportStatus }) }
        desktopFrontend = [ordered]@{ status = $desktopStatus; reason = "desktop_frontend_failed"; checks = @([ordered]@{ name = "installed-desktop-frontend"; status = $desktopStatus }) }
        cleanInstallUpgradeUninstall = [ordered]@{
            status = [string]$upgradeGate.status
            reason = "upgrade_uninstall_failed"
            checks = @($upgradeGate.checks)
            detailEvidence = if ($upgradeGate.Contains("detailEvidence")) { $upgradeGate["detailEvidence"] } else { $null }
        }
        licenseSupplyChain = [ordered]@{
            status = $licenseStatus
            reason = "license_supply_chain_failed"
            checks = @(
                [ordered]@{ name = "third-party-notices"; status = if ($noticeOk) { "pass" } else { "fail" } },
                [ordered]@{ name = "runtime-manifests"; status = if ($manifestOk) { "pass" } else { "fail" } },
                [ordered]@{ name = "export-privacy-tests"; status = $exportStatus }
            )
        }
    }
    $mandatoryGateNames = @(
        "frozenRuntimeImports",
        "mediaPreparation",
        "youtubeWorkflow",
        "liveMic",
        "meetingCapture",
        "diarization",
        "pdfDocxExport",
        "desktopFrontend",
        "cleanInstallUpgradeUninstall",
        "licenseSupplyChain"
    )
    if (
        $gateDefinitions.Count -ne $mandatoryGateNames.Count -or
        @($mandatoryGateNames | Where-Object { -not $gateDefinitions.Contains($_) }).Count -ne 0
    ) {
        throw "mandatory_gate_set_drift"
    }
    $gates = [ordered]@{}
    foreach ($gateName in $gateDefinitions.Keys) {
        $definition = $gateDefinitions[$gateName]
        $detailEvidence = if ($definition.Contains("detailEvidence")) {
            $definition["detailEvidence"]
        } else {
            $null
        }
        $artifactSha = Write-GateArtifact `
            -EvidenceRoot $evidenceRoot `
            -Gate $gateName `
            -Status ([string]$definition.status) `
            -Checks @($definition.checks) `
            -PacketId $packetId `
            -ParentChampionId $parentChampionId `
            -SourceCommit $sourceCommit `
            -DetailEvidence $detailEvidence
        $reasonCode = if ([string]$definition.status -eq "pass") { "" } else { [string]$definition.reason }
        $gates[$gateName] = New-Gate -Status ([string]$definition.status) -EvidenceSha256 $artifactSha -ReasonCode $reasonCode
    }
    foreach ($gateName in $mandatoryGateNames) {
        $gate = $gates[$gateName]
        $artifactPath = Join-Path $evidenceRoot "gates\$gateName.json"
        if (-not (Test-RetainedGateArtifact `
            -Path $artifactPath `
            -ExpectedGate $gateName `
            -ExpectedStatus ([string]$gate.status) `
            -ExpectedPacketId $packetId `
            -ExpectedParentChampionId $parentChampionId `
            -ExpectedSourceCommit $sourceCommit `
            -ExpectedSha256 ([string]$gate.evidenceSha256))) {
            throw "retained_gate_artifact_binding_failed"
        }
    }
    $gateEvidencePath = Join-Path $evidenceRoot "gate-evidence.json"
    $gateEvidence = [ordered]@{
        gateEvidenceContract = "InstallerResearchGateEvidenceV1"
        schemaVersion = 1
        runId = $RunId
        packetId = $packetId
        parentChampionId = $parentChampionId
        sourceCommit = $sourceCommit
        gates = $gates
    }
    Write-JsonAtomic -Path $gateEvidencePath -Payload $gateEvidence

    $allExternalGatesPassed = @(
        $mandatoryGateNames | Where-Object { [string]$gates[$_].status -ne "pass" }
    ).Count -eq 0
    if ($expectedKind -in @("baseline-replica", "final-replica") -and -not $allExternalGatesPassed) {
        throw "mandatory_functional_gate_failed"
    }

    $fullSuiteEvidenceSha = $null
    if ($Mode -eq "final-1") {
        $fullSuiteEvidenceSha = Invoke-FinalFullSuite `
            -EvidenceRoot $evidenceRoot `
            -ScratchRoot $scratchRoot `
            -Python $environment.python `
            -Toolchain $toolchain `
            -PacketId $packetId `
            -SourceCommit $sourceCommit `
            -ChampionSha256 ([string]$action.championSha256) `
            -ChampionSourceTreeOid ([string]$action.championSourceTreeOid)
    }

    $timingPath = Join-Path $evidenceRoot "install-timing.json"
    if ($RunTiming) {
        $timingParentId = $parentChampionId
        $timingParentInventory = $parentInventory
        if ($expectedKind -eq "final-replica") {
            $timingParentId = "baseline"
            $timingParentInventory = (Read-JsonObject -Path (Join-Path $runRoot "baseline.json") -Code "baseline_missing").inventory
        }
        $parentInstaller = Get-InstallerArchivePath `
            -RunRoot $runRoot `
            -PacketId $(if ($timingParentId -eq "baseline") { "baseline-1" } else { $timingParentId }) `
            -Inventory $timingParentInventory
        $timingLog = Join-Path $scratchRoot "install-timing.log"
        $timingCommand = Invoke-CapturedCommand -LogPath $timingLog -PowerShellScript -Command {
            & (Join-Path $RepoRoot "scripts\measure_installer_research.ps1") `
                -BaselineInstallerPath $parentInstaller `
                -CandidateInstallerPath $archivedInstaller `
                -RepoRoot $RepoRoot `
                -OutputPath $timingPath `
                -PairCount 20 `
                -WarmupPerVariant 1 `
                -StableSamples 3 `
                -SampleIntervalMs 750 `
                -ExpectedVersion $productVersion `
                -RunId $RunId `
                -PacketId $packetId `
                -ParentChampionId $timingParentId `
                -SourceCommit $sourceCommit
        }
        if ($timingCommand.exitCode -ne 0 -or -not (Test-Path -LiteralPath $timingPath -PathType Leaf)) {
            throw "installer_timing_failed"
        }
    }

    if ($expectedKind -eq "measure-candidate") {
        $evaluateLog = Join-Path $scratchRoot "evaluate.log"
        $arguments = @(
            (Join-Path $RepoRoot "scripts\installer_research.py"),
            "evaluate",
            "--baseline", (Join-Path $runRoot "baseline.json"),
            "--candidate-inventory", $inventoryPath,
            "--run-id", $RunId,
            "--packet-id", $packetId,
            "--parent-champion-id", $parentChampionId,
            "--hypothesis", [string]$packet.hypothesis.statement,
            "--source-commit", $sourceCommit,
            "--comparison-kind", $comparisonKind,
            "--gate-results", $gateEvidencePath,
            "--min-absolute-reduction-bytes", "262144",
            "--min-relative-basis-points", "25",
            "--output", $resultPath
        )
        if ($parentChampionId -ne "baseline") {
            $arguments += @("--parent-inventory", (Join-Path $runRoot "packet-evidence\$parentChampionId\inventory.json"))
        }
        if ($RunTiming) {
            $arguments += @("--install-measurements", $timingPath)
        }
        $evaluation = Invoke-CapturedCommand -LogPath $evaluateLog -Command { & $environment.python @arguments }
        if ($evaluation.exitCode -notin @(0, 1) -or -not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
            throw "candidate_evaluation_failed"
        }
        $result = Read-JsonObject -Path $resultPath -Code "candidate_result_invalid"
        if ([string]$result.packetId -ne $packetId -or [string]$result.sourceCommit -ne $sourceCommit) {
            throw "candidate_result_binding_failed"
        }
        $exitCode = $evaluation.exitCode
    } else {
        $resultParent = Split-Path -Parent $resultPath
        New-Item -ItemType Directory -Path $resultParent -Force | Out-Null
        $temporaryResult = "$resultPath.$PID.tmp"
        try {
            Copy-Item -LiteralPath $inventoryPath -Destination $temporaryResult -Force
            if ((Get-Sha256File -Path $temporaryResult) -ne (Get-Sha256File -Path $inventoryPath)) {
                throw "replica_result_publish_drift"
            }
            Move-Item -LiteralPath $temporaryResult -Destination $resultPath
        } finally {
            if (Test-Path -LiteralPath $temporaryResult -PathType Leaf) {
                Remove-Item -LiteralPath $temporaryResult -Force
            }
        }
        $exitCode = 0
    }

    $summary = [ordered]@{
        ok = $true
        packetProducerContract = "InstallerSizePacketProducerV1"
        schemaVersion = 1
        runId = $RunId
        packetId = $packetId
        mode = $Mode
        comparisonKind = $comparisonKind
        compression = $compression
        sourceCommit = $sourceCommit
        installer = [ordered]@{
            name = $expectedInstallerName
            length = [int64](Get-Item -LiteralPath $archivedInstaller).Length
            sha256 = Get-Sha256File -Path $archivedInstaller
        }
        inventorySha256 = Get-Sha256File -Path $inventoryPath
        gateEvidenceSha256 = Get-Sha256File -Path $gateEvidencePath
        fullSuiteEvidenceSha256 = $fullSuiteEvidenceSha
        timingEvidenceSha256 = if ($RunTiming) { Get-Sha256File -Path $timingPath } else { $null }
        buildEvidenceSha256 = $buildEvidenceSha
        resultSha256 = Get-Sha256File -Path $resultPath
    }
} catch {
    $failureCode = if ($_.Exception.Message -match '^[a-z0-9][a-z0-9._-]{0,95}$') {
        $_.Exception.Message
    } else {
        "packet_producer_failed"
    }
    $exitCode = 2
} finally {
    if ($installRoot) {
        try { Invoke-ExactUninstaller -InstallRoot $installRoot } catch { $exitCode = 2; $failureCode = "cleanup_uninstaller_failed" }
        try { Remove-ExactUninstallRegistryEntries -InstallRoot $installRoot } catch { $exitCode = 2; $failureCode = "cleanup_registry_failed" }
    }
    if ($scratchRoot -and (Test-Path -LiteralPath $scratchRoot)) {
        try { Remove-ScopedTree -Root $InstallerSmokeNamespace -Path $scratchRoot -Code "cleanup_scratch_failed" } catch { $exitCode = 2; $failureCode = "cleanup_scratch_failed" }
    }
    foreach ($path in @($installRoot, $dataRoot)) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            try { Remove-ScopedTree -Root $InstallerSmokeNamespace -Path $path -Code "cleanup_install_failed" } catch { $exitCode = 2; $failureCode = "cleanup_install_failed" }
        }
    }
    if ($buildRoot -and (Test-Path -LiteralPath $buildRoot)) {
        try { Remove-ScopedTree -Root $runRoot -Path $buildRoot -Code "cleanup_build_failed" } catch { $exitCode = 2; $failureCode = "cleanup_build_failed" }
    }
    foreach ($name in $SigningEnvironmentNames) {
        if ($savedSigningEnvironment.ContainsKey($name) -and $null -ne $savedSigningEnvironment[$name]) {
            Set-Item -LiteralPath "Env:$name" -Value $savedSigningEnvironment[$name]
        } else {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        }
    }
    foreach ($name in $savedDeterministicBuildEnvironment.Keys) {
        if ($null -ne $savedDeterministicBuildEnvironment[$name]) {
            Set-Item -LiteralPath "Env:$name" -Value $savedDeterministicBuildEnvironment[$name]
        } else {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        }
    }
}

if ($exitCode -eq 2) {
    [Console]::Error.WriteLine((@{
        ok = $false
        packetProducerContract = "InstallerSizePacketProducerV1"
        schemaVersion = 1
        errorCode = $failureCode
    } | ConvertTo-Json -Compress))
    exit 2
}

$summary | ConvertTo-Json -Depth 8 -Compress
exit $exitCode
