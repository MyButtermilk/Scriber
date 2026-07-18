<#
.SYNOPSIS
Measures clean-install completion for two Scriber NSIS installers.

.DESCRIPTION
Runs one excluded warm-up per variant followed by counterbalanced A/B pairs.
The timed interval starts immediately before the installer process is created
and ends only after the launcher exited successfully, related installer/updater
processes left, and the installed executable's version/length/SHA-256 tuple was
stable across repeated exclusive-read observations. Cleanup and uninstall work
is deliberately outside the timed interval. The script does not flush the OS
file cache and writes a machine-readable JSON report.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [string]$PacketId,

    [Parameter(Mandatory = $true)]
    [string]$ParentChampionId,

    [Parameter(Mandatory = $true)]
    [string]$SourceCommit,

    [Parameter(Mandatory = $true)]
    [string]$BaselineInstallerPath,

    [Parameter(Mandatory = $true)]
    [string]$CandidateInstallerPath,

    [string]$RepoRoot = "",

    [string]$InstallRoot = "",

    [string]$OutputPath = "",

    [ValidateRange(20, 100)]
    [int]$PairCount = 20,

    [ValidateRange(0, 5)]
    [int]$WarmupPerVariant = 1,

    [ValidateRange(2, 10)]
    [int]$StableSamples = 3,

    [ValidateRange(100, 5000)]
    [int]$SampleIntervalMs = 750,

    [ValidateRange(30, 900)]
    [int]$TimeoutSec = 180,

    [string]$ExpectedVersion = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}

$parsedRunId = [guid]::Empty
if (
    -not [guid]::TryParseExact($RunId, "D", [ref]$parsedRunId) -or
    $parsedRunId -eq [guid]::Empty -or
    $parsedRunId.ToString("D") -cne $RunId -or
    (($parsedRunId.ToByteArray()[8] -band 0xC0) -ne 0x80)
) {
    throw "RunId must be a canonical non-nil RFC 4122 UUID."
}
foreach ($binding in @(
    @{ Name = "PacketId"; Value = $PacketId },
    @{ Name = "ParentChampionId"; Value = $ParentChampionId }
)) {
    if ([string]$binding.Value -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$') {
        throw "$($binding.Name) is not a safe research identifier."
    }
}
if ($SourceCommit -notmatch '^[0-9a-f]{40}$') {
    throw "SourceCommit must be a full lowercase SHA-1 Git commit."
}

function Convert-ToFullPath {
    param(
        [string]$Path,
        [string]$BasePath
    )

    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) {
        $Path
    } else {
        Join-Path $BasePath $Path
    }
    return [System.IO.Path]::GetFullPath($candidate).TrimEnd('\', '/')
}

function Test-PathUnderRoot {
    param(
        [string]$Root,
        [string]$Path,
        [switch]$AllowEqual
    )

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $pathFull = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if ($AllowEqual -and $pathFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    return $pathFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Convert-ToRelativePath {
    param(
        [string]$Root,
        [string]$Path
    )

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $pathFull = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if ($pathFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return "."
    }
    $prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $pathFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the requested relative-path root."
    }
    return $pathFull.Substring($prefix.Length).Replace('\', '/')
}

function Get-RedactedErrorMessage {
    param(
        [string]$Message,
        [string]$Root
    )

    if ([string]::IsNullOrWhiteSpace($Message)) {
        return "Installer timing failed."
    }
    $redacted = [regex]::Replace(
        $Message,
        [regex]::Escape($Root),
        "<repo>",
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    # Failure evidence is durable and must not retain a caller-supplied local
    # installer path, profile path, or UNC share.  Retaining the rest of a line
    # is less important than failing closed on personal absolute paths.
    return [regex]::Replace(
        $redacted,
        '(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n]*',
        '<redacted-absolute-path>'
    )
}

function Assert-NoReparsePath {
    param(
        [string]$Root,
        [string]$Path,
        [string]$Label,
        [switch]$Recurse
    )

    if (-not (Test-PathUnderRoot -Root $Root -Path $Path)) {
        throw "$Label must be a strict descendant of its safety root."
    }
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $pathFull = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if (Test-Path -LiteralPath $rootFull) {
        $rootItem = Get-Item -LiteralPath $rootFull -Force
        if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label safety root must not be a reparse point: $rootFull"
        }
    }
    $relative = $pathFull.Substring($rootFull.Length).TrimStart('\', '/')
    $current = $rootFull
    foreach ($part in $relative.Split([char[]]@('\', '/'), [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        if (-not (Test-Path -LiteralPath $current)) {
            break
        }
        $item = Get-Item -LiteralPath $current -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label path must not contain a reparse point: $current"
        }
    }
    if ($Recurse -and (Test-Path -LiteralPath $pathFull -PathType Container)) {
        $nested = Get-ChildItem -LiteralPath $pathFull -Recurse -Force -ErrorAction Stop |
            Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 } |
            Select-Object -First 1
        if ($nested) {
            throw "$Label tree must not contain a reparse point: $($nested.FullName)"
        }
    }
    return $pathFull
}

function Assert-SafeInstallRoot {
    param(
        [string]$ScratchRoot,
        [string]$TargetRoot
    )

    if (-not (Test-PathUnderRoot -Root $ScratchRoot -Path $TargetRoot)) {
        throw "InstallRoot must be a strict descendant of the installer timing scratch root: $ScratchRoot"
    }
    $verifiedScratch = Assert-NoReparsePath -Root $RepoRoot -Path $ScratchRoot -Label "Installer timing scratch root"
    $null = Assert-NoReparsePath -Root $verifiedScratch -Path $TargetRoot -Label "Installer timing install root" -Recurse
}

function Get-Sha256Hex {
    param([string]$Path)

    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha256.ComputeHash($stream)
        return ([System.BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Get-StringSha256 {
    param([string]$Value)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
    }
}

function Get-NormalizedFileVersion {
    param([string]$Path)

    $versionInfo = (Get-Item -LiteralPath $Path).VersionInfo
    $version = [string]$versionInfo.ProductVersion
    if ([string]::IsNullOrWhiteSpace($version)) {
        $version = [string]$versionInfo.FileVersion
    }
    return $version.Trim()
}

function Test-ExpectedVersion {
    param(
        [string]$Actual,
        [string]$Expected
    )

    if ([string]::IsNullOrWhiteSpace($Expected)) {
        return -not [string]::IsNullOrWhiteSpace($Actual)
    }
    return (
        $Actual -eq $Expected -or
        $Actual -eq "$Expected.0" -or
        $Actual.StartsWith("$Expected+", [System.StringComparison]::Ordinal)
    )
}

function Resolve-InstalledExecutable {
    param([string]$Root)

    foreach ($name in @("scriber-desktop.exe", "Scriber.exe")) {
        $candidate = Join-Path $Root $name
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Resolve-InstalledUninstaller {
    param([string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return $null
    }
    $matches = @(
        Get-ChildItem -LiteralPath $Root -File -Force -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -in @("uninstall.exe", "Uninstall.exe") -or
                $_.Name -match '^unins[^\\/]*\.exe$'
            } |
            Sort-Object FullName
    )
    if ($matches.Count -gt 0) {
        return $matches[0].FullName
    }
    return $null
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
        [System.Collections.Generic.HashSet[int]]$KnownProcessIds,
        [string]$TargetRoot,
        [string[]]$InstallerPaths
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

    $targetNeedle = $TargetRoot.ToLowerInvariant()
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

function Stop-ScopedProcessTree {
    param(
        [System.Collections.Generic.HashSet[int]]$KnownProcessIds,
        [string]$TargetRoot,
        [string[]]$InstallerPaths
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
        if ($processId -eq $PID) {
            continue
        }
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

function Wait-ScopedProcessTreeExit {
    param(
        [System.Collections.Generic.HashSet[int]]$KnownProcessIds,
        [string]$TargetRoot,
        [string[]]$InstallerPaths,
        [DateTimeOffset]$Deadline
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
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for the scoped installer/updater process tree to exit."
}

function Get-RegistryEntriesForInstallRoot {
    param([string]$TargetRoot)

    $uninstallRoot = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall"
    if (-not (Test-Path -LiteralPath $uninstallRoot)) {
        return @()
    }
    $targetFull = [System.IO.Path]::GetFullPath($TargetRoot).TrimEnd('\', '/')
    $matches = [System.Collections.Generic.List[object]]::new()
    foreach ($key in Get-ChildItem -LiteralPath $uninstallRoot -ErrorAction Stop) {
        $entry = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
        if ($null -eq $entry -or [string]$entry.DisplayName -ne "Scriber") {
            continue
        }
        $location = ([string]$entry.InstallLocation).Trim().Trim('"')
        $locationMatches = $false
        if (-not [string]::IsNullOrWhiteSpace($location)) {
            try {
                $locationMatches = [System.IO.Path]::GetFullPath($location).TrimEnd('\', '/').Equals(
                    $targetFull,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            } catch {
                $locationMatches = $false
            }
        }
        $uninstallString = [string]$entry.UninstallString
        $uninstallMatches = (
            -not [string]::IsNullOrWhiteSpace($uninstallString) -and
            $uninstallString.IndexOf($targetFull, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        )
        if ($locationMatches -or $uninstallMatches) {
            $matches.Add($key) | Out-Null
        }
    }
    return @($matches)
}

function Remove-TargetRegistryEntries {
    param([string]$TargetRoot)

    $entries = @(Get-RegistryEntriesForInstallRoot -TargetRoot $TargetRoot)
    foreach ($entry in $entries) {
        Remove-Item -LiteralPath $entry.PSPath -Recurse -Force -ErrorAction Stop
    }
    return $entries.Count
}

function Invoke-CleanInstallState {
    param(
        [string]$ScratchRoot,
        [string]$TargetRoot,
        [string[]]$InstallerPaths,
        [int]$ProcessTimeoutSec
    )

    Assert-SafeInstallRoot -ScratchRoot $ScratchRoot -TargetRoot $TargetRoot
    $started = [System.Diagnostics.Stopwatch]::StartNew()
    $uninstallerExitCode = $null
    $uninstallerFailure = $null
    $forcedProcessCount = 0
    $knownProcessIds = [System.Collections.Generic.HashSet[int]]::new()
    $uninstaller = Resolve-InstalledUninstaller -Root $TargetRoot
    if ($uninstaller) {
        $process = Start-Process -FilePath $uninstaller -ArgumentList @("/S") -PassThru -WindowStyle Hidden
        try {
            [void]$knownProcessIds.Add([int]$process.Id)
            $null = $process.Handle
            if (-not $process.WaitForExit($ProcessTimeoutSec * 1000)) {
                throw "Silent uninstaller exceeded the cleanup timeout."
            }
            $uninstallerExitCode = [int]$process.ExitCode
            if ($uninstallerExitCode -ne 0) {
                throw "Silent uninstaller failed with exit code $uninstallerExitCode."
            }
            Wait-ScopedProcessTreeExit `
                -KnownProcessIds $knownProcessIds `
                -TargetRoot $TargetRoot `
                -InstallerPaths $InstallerPaths `
                -Deadline ([DateTimeOffset]::UtcNow.AddSeconds($ProcessTimeoutSec))
        } catch {
            $uninstallerFailure = $_.Exception.Message
        } finally {
            $before = @(
                Get-RelatedInstallerProcesses `
                    -KnownProcessIds $knownProcessIds `
                    -TargetRoot $TargetRoot `
                    -InstallerPaths $InstallerPaths
            )
            if ($before.Count -gt 0) {
                $forcedProcessCount += $before.Count
                Stop-ScopedProcessTree `
                    -KnownProcessIds $knownProcessIds `
                    -TargetRoot $TargetRoot `
                    -InstallerPaths $InstallerPaths
            }
            $process.Dispose()
        }
    }

    $remainingKnownIds = [System.Collections.Generic.HashSet[int]]::new()
    $remaining = @(
        Get-RelatedInstallerProcesses `
            -KnownProcessIds $remainingKnownIds `
            -TargetRoot $TargetRoot `
            -InstallerPaths @()
    )
    foreach ($process in $remaining) {
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        $forcedProcessCount += 1
    }
    if ($remaining.Count -gt 0) {
        Start-Sleep -Milliseconds 500
    }

    Assert-SafeInstallRoot -ScratchRoot $ScratchRoot -TargetRoot $TargetRoot
    $filesRemoved = $false
    if (Test-Path -LiteralPath $TargetRoot) {
        Remove-Item -LiteralPath $TargetRoot -Recurse -Force -ErrorAction Stop
        $filesRemoved = $true
    }
    $registryEntriesRemoved = Remove-TargetRegistryEntries -TargetRoot $TargetRoot

    if (Test-Path -LiteralPath $TargetRoot) {
        throw "Installer timing cleanup left install artifacts under: $TargetRoot"
    }
    if (@(Get-RegistryEntriesForInstallRoot -TargetRoot $TargetRoot).Count -ne 0) {
        throw "Installer timing cleanup left a matching Scriber uninstall registry entry."
    }
    if ($uninstallerFailure) {
        throw $uninstallerFailure
    }
    $started.Stop()
    return [ordered]@{
        durationMs = [int64]$started.ElapsedMilliseconds
        uninstallerExitCode = $uninstallerExitCode
        forcedProcessCount = $forcedProcessCount
        filesRemoved = $filesRemoved
        registryEntriesRemoved = $registryEntriesRemoved
    }
}

function Get-ExclusiveInstalledIdentity {
    param([string]$Path)

    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::None
    )
    try {
        $length = [int64]$stream.Length
    } finally {
        $stream.Dispose()
    }
    $version = Get-NormalizedFileVersion -Path $Path
    $sha256 = Get-Sha256Hex -Path $Path
    return [ordered]@{
        path = $Path
        version = $version
        length = $length
        sha256 = $sha256
        key = "$version|$length|$sha256"
    }
}

function Get-InstalledTreeInventory {
    param([string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Installed tree is missing before inventory."
    }
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $byPath = [System.Collections.Generic.SortedDictionary[string, object]]::new(
        [System.StringComparer]::Ordinal
    )
    $treeEntries = @(
        Get-ChildItem -LiteralPath $Root -Recurse -Force -ErrorAction Stop
    )
    $reparseEntry = $treeEntries |
        Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 } |
        Select-Object -First 1
    if ($reparseEntry) {
        throw "Installed tree inventory encountered a reparse point."
    }
    foreach ($file in $treeEntries | Where-Object { -not $_.PSIsContainer }) {
        $relative = Convert-ToRelativePath -Root $Root -Path $file.FullName
        if ($byPath.ContainsKey($relative)) {
            throw "Installed tree inventory contains a duplicate relative path."
        }
        $byPath.Add($relative, [ordered]@{
            length = [int64]$file.Length
            sha256 = Get-Sha256Hex -Path $file.FullName
        })
    }

    $totalBytes = [int64]0
    $canonical = [System.Text.StringBuilder]::new()
    foreach ($pair in $byPath.GetEnumerator()) {
        $totalBytes += [int64]$pair.Value["length"]
        [void]$canonical.Append($pair.Key)
        [void]$canonical.Append([char]0)
        [void]$canonical.Append(([int64]$pair.Value["length"]).ToString([System.Globalization.CultureInfo]::InvariantCulture))
        [void]$canonical.Append([char]0)
        [void]$canonical.Append([string]$pair.Value["sha256"])
        [void]$canonical.Append([char]0)
    }
    $treeSha256 = Get-StringSha256 -Value $canonical.ToString()
    $watch.Stop()
    return [ordered]@{
        fileCount = $byPath.Count
        totalBytes = $totalBytes
        treeSha256 = $treeSha256
        inventoryDurationMs = [int64]$watch.ElapsedMilliseconds
    }
}

function Assert-VariantInventoryConsistent {
    param(
        [hashtable]$InventoryByVariant,
        [string]$Variant,
        [object]$Inventory
    )

    $current = [ordered]@{
        fileCount = [int]$Inventory["fileCount"]
        totalBytes = [int64]$Inventory["totalBytes"]
        treeSha256 = [string]$Inventory["treeSha256"]
    }
    if (-not $InventoryByVariant.ContainsKey($Variant)) {
        $current["sampleCount"] = 1
        $InventoryByVariant[$Variant] = $current
        return
    }
    $expected = $InventoryByVariant[$Variant]
    if (
        [int]$expected["fileCount"] -ne [int]$current["fileCount"] -or
        [int64]$expected["totalBytes"] -ne [int64]$current["totalBytes"] -or
        [string]$expected["treeSha256"] -ne [string]$current["treeSha256"]
    ) {
        throw "$Variant installed tree changed across timing samples."
    }
    $expected["sampleCount"] = [int]$expected["sampleCount"] + 1
}

function Invoke-InstallerMeasurement {
    param(
        [string]$Variant,
        [string]$InstallerPath,
        [string]$TargetRoot,
        [string[]]$InstallerPaths,
        [string]$RequiredVersion,
        [int]$CompletionTimeoutSec,
        [int]$RequiredStableSamples,
        [int]$ObservationIntervalMs
    )

    $knownProcessIds = [System.Collections.Generic.HashSet[int]]::new()
    $process = $null
    $qpcStarted = [System.Diagnostics.Stopwatch]::GetTimestamp()
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        # Start the clock immediately before CreateProcess. Cleanup is performed
        # by the caller before this function and is never included here.
        $process = Start-Process `
            -FilePath $InstallerPath `
            -ArgumentList @("/S", "/D=$TargetRoot") `
            -PassThru `
            -WindowStyle Hidden
        [void]$knownProcessIds.Add([int]$process.Id)
        $null = $process.Handle
        if (-not $process.WaitForExit($CompletionTimeoutSec * 1000)) {
            throw "$Variant installer launcher exceeded the $CompletionTimeoutSec-second timeout."
        }
        $launcherExitMs = [int64]$watch.ElapsedMilliseconds
        $launcherExitQpc = [System.Diagnostics.Stopwatch]::GetTimestamp()
        $launcherExitCode = [int]$process.ExitCode
        if ($launcherExitCode -ne 0) {
            throw "$Variant installer launcher failed with exit code $launcherExitCode."
        }

        $deadline = [DateTimeOffset]::UtcNow.AddMilliseconds(
            [Math]::Max(0, ($CompletionTimeoutSec * 1000) - $watch.ElapsedMilliseconds)
        )
        $stableCount = 0
        $lastIdentityKey = ""
        $lastIdentity = $null
        $lastRelatedProcessIds = @()
        while ([DateTimeOffset]::UtcNow -lt $deadline) {
            $related = @(
                Get-RelatedInstallerProcesses `
                    -KnownProcessIds $knownProcessIds `
                    -TargetRoot $TargetRoot `
                    -InstallerPaths $InstallerPaths
            )
            $lastRelatedProcessIds = @($related | ForEach-Object { [int]$_.ProcessId })
            $installedExe = Resolve-InstalledExecutable -Root $TargetRoot
            if ($related.Count -eq 0 -and $installedExe) {
                try {
                    $identity = Get-ExclusiveInstalledIdentity -Path $installedExe
                    if (Test-ExpectedVersion -Actual ([string]$identity.version) -Expected $RequiredVersion) {
                        if ([string]$identity.key -eq $lastIdentityKey) {
                            $stableCount += 1
                        } else {
                            $lastIdentityKey = [string]$identity.key
                            $lastIdentity = $identity
                            $stableCount = 1
                        }
                        if ($stableCount -ge $RequiredStableSamples) {
                            $watch.Stop()
                            $stableInstallMs = [int64]$watch.ElapsedMilliseconds
                            $stableQpc = [System.Diagnostics.Stopwatch]::GetTimestamp()
                            return [ordered]@{
                                launcherExitCode = $launcherExitCode
                                launcherExitMs = $launcherExitMs
                                stableInstallMs = $stableInstallMs
                                postExitCompletionMs = [int64][Math]::Max(0, $stableInstallMs - $launcherExitMs)
                                qpcStarted = [int64]$qpcStarted
                                launcherExitQpc = [int64]$launcherExitQpc
                                stableQpc = [int64]$stableQpc
                                installedExe = [string]$lastIdentity.path
                                installedVersion = [string]$lastIdentity.version
                                installedLength = [int64]$lastIdentity.length
                                installedSha256 = [string]$lastIdentity.sha256
                                stableSamples = $stableCount
                            }
                        }
                    } else {
                        $stableCount = 0
                        $lastIdentityKey = ""
                        $lastIdentity = $null
                    }
                } catch {
                    $stableCount = 0
                    $lastIdentityKey = ""
                    $lastIdentity = $null
                }
            } else {
                $stableCount = 0
                $lastIdentityKey = ""
                $lastIdentity = $null
            }
            Start-Sleep -Milliseconds $ObservationIntervalMs
        }
        $processSuffix = if ($lastRelatedProcessIds.Count -gt 0) {
            " Related process IDs: $($lastRelatedProcessIds -join ', ')."
        } else {
            ""
        }
        throw "$Variant installation did not reach a stable installed executable before timeout.$processSuffix"
    } finally {
        if ($watch.IsRunning) {
            $watch.Stop()
        }
        if ($null -ne $process) {
            try {
                if (-not $process.HasExited) {
                    Stop-ScopedProcessTree `
                        -KnownProcessIds $knownProcessIds `
                        -TargetRoot $TargetRoot `
                        -InstallerPaths $InstallerPaths
                }
            } finally {
                $process.Dispose()
            }
        }
    }
}

function Get-TimingStatistics {
    param(
        [object[]]$Samples,
        [string]$Variant
    )

    $values = @(
        $Samples |
            Where-Object { -not $_.warmup -and [string]$_.variant -eq $Variant } |
            ForEach-Object { [int64]$_.stableInstallMs } |
            Sort-Object
    )
    if ($values.Count -eq 0) {
        throw "No measured timing samples were recorded for $Variant."
    }
    $middle = [int][Math]::Floor($values.Count / 2)
    $median = if (($values.Count % 2) -eq 1) {
        [double]$values[$middle]
    } else {
        ([double]$values[$middle - 1] + [double]$values[$middle]) / 2.0
    }
    $p95Rank = [int][Math]::Ceiling(0.95 * $values.Count)
    return [ordered]@{
        count = $values.Count
        p50Ms = $median
        p95Ms = [int64]$values[$p95Rank - 1]
        minimumMs = [int64]$values[0]
        maximumMs = [int64]$values[$values.Count - 1]
    }
}

function Write-JsonAtomic {
    param(
        [string]$Path,
        [object]$Payload
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporaryPath = "$Path.$PID.tmp"
    $encoding = [System.Text.UTF8Encoding]::new($false)
    try {
        $json = $Payload | ConvertTo-Json -Depth 10
        [System.IO.File]::WriteAllText($temporaryPath, $json, $encoding)
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
$scratchRoot = Join-Path $RepoRoot "tmp\installer-research-timing"
$null = Assert-NoReparsePath -Root $RepoRoot -Path $scratchRoot -Label "Installer timing scratch root"
New-Item -ItemType Directory -Force -Path $scratchRoot | Out-Null
$null = Assert-NoReparsePath -Root $RepoRoot -Path $scratchRoot -Label "Installer timing scratch root" -Recurse

$InstallRoot = if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    Join-Path $scratchRoot "install"
} else {
    Convert-ToFullPath -Path $InstallRoot -BasePath $RepoRoot
}
$OutputPath = if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    Join-Path $scratchRoot "installer-ab-timing.json"
} else {
    Convert-ToFullPath -Path $OutputPath -BasePath $RepoRoot
}
Assert-SafeInstallRoot -ScratchRoot $scratchRoot -TargetRoot $InstallRoot
if (-not (Test-PathUnderRoot -Root $RepoRoot -Path $OutputPath)) {
    throw "OutputPath must stay under RepoRoot."
}
$null = Assert-NoReparsePath -Root $RepoRoot -Path $OutputPath -Label "Installer timing output path"
if (Test-PathUnderRoot -Root $InstallRoot -Path $OutputPath -AllowEqual) {
    throw "OutputPath must stay outside InstallRoot because each sample cleans the install tree."
}

$BaselineInstallerPath = Convert-ToFullPath -Path $BaselineInstallerPath -BasePath $RepoRoot
$CandidateInstallerPath = Convert-ToFullPath -Path $CandidateInstallerPath -BasePath $RepoRoot
foreach ($installerPath in @($BaselineInstallerPath, $CandidateInstallerPath)) {
    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        throw "Installer was not found: $installerPath"
    }
    if ([System.IO.Path]::GetExtension($installerPath) -ne ".exe") {
        throw "Installer must be a Windows executable: $installerPath"
    }
    if (Test-PathUnderRoot -Root $InstallRoot -Path $installerPath -AllowEqual) {
        throw "Installer input must stay outside InstallRoot: $installerPath"
    }
}
if ($BaselineInstallerPath.Equals($CandidateInstallerPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "BaselineInstallerPath and CandidateInstallerPath must be distinct files."
}

if ([string]::IsNullOrWhiteSpace($ExpectedVersion)) {
    $versionSource = Get-Content -LiteralPath (Join-Path $RepoRoot "src\version.py") -Raw
    $versionMatch = [regex]::Match($versionSource, '(?m)^__version__\s*=\s*"([^"]+)"')
    if (-not $versionMatch.Success) {
        throw "Could not resolve ExpectedVersion from src/version.py."
    }
    $ExpectedVersion = $versionMatch.Groups[1].Value
}
$ExpectedVersion = $ExpectedVersion.Trim().TrimStart('v')
if ($ExpectedVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "ExpectedVersion must use X.Y.Z format."
}

$installerPathByVariant = @{
    baseline = $BaselineInstallerPath
    candidate = $CandidateInstallerPath
}
$variants = [ordered]@{
    baseline = [ordered]@{
        installerName = [System.IO.Path]::GetFileName($BaselineInstallerPath)
        length = [int64](Get-Item -LiteralPath $BaselineInstallerPath).Length
        sha256 = Get-Sha256Hex -Path $BaselineInstallerPath
    }
    candidate = [ordered]@{
        installerName = [System.IO.Path]::GetFileName($CandidateInstallerPath)
        length = [int64](Get-Item -LiteralPath $CandidateInstallerPath).Length
        sha256 = Get-Sha256Hex -Path $CandidateInstallerPath
    }
}
$installerPaths = @($BaselineInstallerPath, $CandidateInstallerPath)

$lockPath = Join-Path $scratchRoot "measurement.lock"
$lockStream = $null
$samples = [System.Collections.Generic.List[object]]::new()
$cleanupEvents = [System.Collections.Generic.List[object]]::new()
$inventoryByVariant = @{}
$sequence = 0
$payload = $null
try {
    $lockStream = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )

    foreach ($variant in @("baseline", "candidate")) {
        for ($warmupIndex = 1; $warmupIndex -le $WarmupPerVariant; $warmupIndex += 1) {
            $cleanupEvents.Add((Invoke-CleanInstallState `
                -ScratchRoot $scratchRoot `
                -TargetRoot $InstallRoot `
                -InstallerPaths $installerPaths `
                -ProcessTimeoutSec $TimeoutSec)) | Out-Null
            $sequence += 1
            $measurement = Invoke-InstallerMeasurement `
                -Variant $variant `
                -InstallerPath ([string]$installerPathByVariant[$variant]) `
                -TargetRoot $InstallRoot `
                -InstallerPaths $installerPaths `
                -RequiredVersion $ExpectedVersion `
                -CompletionTimeoutSec $TimeoutSec `
                -RequiredStableSamples $StableSamples `
                -ObservationIntervalMs $SampleIntervalMs
            $measurement["installedExe"] = Convert-ToRelativePath `
                -Root $InstallRoot `
                -Path ([string]$measurement["installedExe"])
            $inventory = Get-InstalledTreeInventory -Root $InstallRoot
            $measurement["installedFileCount"] = [int]$inventory["fileCount"]
            $measurement["installedTotalBytes"] = [int64]$inventory["totalBytes"]
            $measurement["installedTreeSha256"] = [string]$inventory["treeSha256"]
            $measurement["inventoryDurationMs"] = [int64]$inventory["inventoryDurationMs"]
            $sample = [ordered]@{
                sequence = $sequence
                pair = $null
                order = $null
                position = $null
                variant = $variant
                warmup = $true
                warmupIndex = $warmupIndex
            }
            foreach ($key in $measurement.Keys) {
                $sample[$key] = $measurement[$key]
            }
            $samples.Add([pscustomobject]$sample) | Out-Null
            Assert-VariantInventoryConsistent `
                -InventoryByVariant $inventoryByVariant `
                -Variant $variant `
                -Inventory $inventory
        }
    }

    for ($pair = 1; $pair -le $PairCount; $pair += 1) {
        $order = if (($pair % 2) -eq 1) {
            @("baseline", "candidate")
        } else {
            @("candidate", "baseline")
        }
        $orderLabel = if (($pair % 2) -eq 1) { "AB" } else { "BA" }
        for ($position = 0; $position -lt $order.Count; $position += 1) {
            $variant = $order[$position]
            $cleanupEvents.Add((Invoke-CleanInstallState `
                -ScratchRoot $scratchRoot `
                -TargetRoot $InstallRoot `
                -InstallerPaths $installerPaths `
                -ProcessTimeoutSec $TimeoutSec)) | Out-Null
            $sequence += 1
            $measurement = Invoke-InstallerMeasurement `
                -Variant $variant `
                -InstallerPath ([string]$installerPathByVariant[$variant]) `
                -TargetRoot $InstallRoot `
                -InstallerPaths $installerPaths `
                -RequiredVersion $ExpectedVersion `
                -CompletionTimeoutSec $TimeoutSec `
                -RequiredStableSamples $StableSamples `
                -ObservationIntervalMs $SampleIntervalMs
            $measurement["installedExe"] = Convert-ToRelativePath `
                -Root $InstallRoot `
                -Path ([string]$measurement["installedExe"])
            $inventory = Get-InstalledTreeInventory -Root $InstallRoot
            $measurement["installedFileCount"] = [int]$inventory["fileCount"]
            $measurement["installedTotalBytes"] = [int64]$inventory["totalBytes"]
            $measurement["installedTreeSha256"] = [string]$inventory["treeSha256"]
            $measurement["inventoryDurationMs"] = [int64]$inventory["inventoryDurationMs"]
            $sample = [ordered]@{
                sequence = $sequence
                pair = $pair
                order = $orderLabel
                position = $position + 1
                variant = $variant
                warmup = $false
                warmupIndex = $null
            }
            foreach ($key in $measurement.Keys) {
                $sample[$key] = $measurement[$key]
            }
            $samples.Add([pscustomobject]$sample) | Out-Null
            Assert-VariantInventoryConsistent `
                -InventoryByVariant $inventoryByVariant `
                -Variant $variant `
                -Inventory $inventory
        }
    }

    $cleanupEvents.Add((Invoke-CleanInstallState `
        -ScratchRoot $scratchRoot `
        -TargetRoot $InstallRoot `
        -InstallerPaths $installerPaths `
        -ProcessTimeoutSec $TimeoutSec)) | Out-Null

    $payload = [ordered]@{
        apiVersion = "1"
        kind = "installer-ab-timing"
        ok = $true
        runId = $RunId
        packetId = $PacketId
        parentChampionId = $ParentChampionId
        sourceCommit = $SourceCommit
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        expectedVersion = $ExpectedVersion
        pairCount = $PairCount
        warmupPerVariant = $WarmupPerVariant
        stableSamples = $StableSamples
        sampleIntervalMs = $SampleIntervalMs
        timeoutSec = $TimeoutSec
        installRoot = Convert-ToRelativePath -Root $RepoRoot -Path $InstallRoot
        stopwatch = [ordered]@{
            isHighResolution = [System.Diagnostics.Stopwatch]::IsHighResolution
            frequency = [int64][System.Diagnostics.Stopwatch]::Frequency
        }
        variants = $variants
        samples = @($samples)
        statistics = [ordered]@{
            baseline = Get-TimingStatistics -Samples @($samples) -Variant "baseline"
            candidate = Get-TimingStatistics -Samples @($samples) -Variant "candidate"
        }
        inventoryConsistency = [ordered]@{
            baseline = $inventoryByVariant["baseline"]
            candidate = $inventoryByVariant["candidate"]
        }
        cleanup = [ordered]@{
            outsideTimedIntervals = $true
            invocationCount = $cleanupEvents.Count
            events = @($cleanupEvents)
        }
        cachePolicy = [ordered]@{
            osFileCacheFlushed = $false
        }
    }
    Write-JsonAtomic -Path $OutputPath -Payload $payload
    $payload | ConvertTo-Json -Depth 10 -Compress
} catch {
    try {
        $cleanupEvents.Add((Invoke-CleanInstallState `
            -ScratchRoot $scratchRoot `
            -TargetRoot $InstallRoot `
            -InstallerPaths $installerPaths `
            -ProcessTimeoutSec $TimeoutSec)) | Out-Null
    } catch {
        # Preserve the original measurement failure. The scoped cleanup error is
        # still represented by the failed run and can be reproduced directly.
    }
    $failure = [ordered]@{
        apiVersion = "1"
        kind = "installer-ab-timing"
        ok = $false
        runId = $RunId
        packetId = $PacketId
        parentChampionId = $ParentChampionId
        sourceCommit = $SourceCommit
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        expectedVersion = $ExpectedVersion
        pairCount = $PairCount
        warmupPerVariant = $WarmupPerVariant
        stableSamples = $StableSamples
        sampleIntervalMs = $SampleIntervalMs
        timeoutSec = $TimeoutSec
        installRoot = Convert-ToRelativePath -Root $RepoRoot -Path $InstallRoot
        variants = $variants
        samples = @($samples)
        inventoryConsistency = [ordered]@{
            baseline = $inventoryByVariant["baseline"]
            candidate = $inventoryByVariant["candidate"]
        }
        cleanup = [ordered]@{
            outsideTimedIntervals = $true
            invocationCount = $cleanupEvents.Count
            events = @($cleanupEvents)
        }
        error = [ordered]@{
            type = $_.Exception.GetType().FullName
            message = Get-RedactedErrorMessage -Message $_.Exception.Message -Root $RepoRoot
        }
    }
    Write-JsonAtomic -Path $OutputPath -Payload $failure
    throw
} finally {
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
