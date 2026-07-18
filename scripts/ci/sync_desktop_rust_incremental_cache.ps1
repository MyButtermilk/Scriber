<#
.SYNOPSIS
Exports or imports the bounded Desktop Rust incremental compiler state.

.DESCRIPTION
Only the Cargo-metadata-confirmed Desktop targets scriber_desktop-* (the thin
binary) and scriber_desktop_lib-* (the app rlib) are eligible. The Actions
cache stores a detached envelope under build/, never Cargo fingerprints,
build-script output, dependency objects, executables, PDBs, audio-sidecar
state, or another crate's incremental directory. Import validates the complete
inventory before touching Cargo's target tree. Cargo/rustc remain the sole
authority for deciding whether restored incremental query results are usable.
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Export", "Import")]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$CurrentInputKey,
    [Parameter(Mandatory = $true)]
    [string]$DependencyScopeKey,
    [Parameter(Mandatory = $true)]
    [string]$RefScopeKey,
    [string]$MatchedCacheKey = "",
    [string]$RunnerOs = "Windows",
    [string]$CacheRoot = "build\desktop-rust-incremental-cache",
    [string]$TargetDir = "Frontend\src-tauri\target",
    [string]$BuildTimingPath = "Frontend\src-tauri\target\release\release-metadata\build-timing.json",
    [string]$BinaryPath = "Frontend\src-tauri\target\release\scriber-desktop.exe",
    [string]$ContractPath = "packaging\desktop-rust-incremental-cache-contract.json",
    [string]$SourceCommit = $env:GITHUB_SHA
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Write-GitHubOutput {
    param([string]$Name, [string]$Value)
    if ($env:GITHUB_OUTPUT) {
        "$Name=$Value" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

function Resolve-RepoPath {
    param([string]$Path, [string]$Label)
    $resolved = if ([System.IO.Path]::IsPathRooted($Path)) {
        [System.IO.Path]::GetFullPath($Path)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
    }
    $root = $repoRoot.TrimEnd("\", "/")
    $prefix = $root + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under the repository root: $resolved"
    }
    return $resolved
}

function Assert-UnderRoot {
    param([string]$Root, [string]$Path, [string]$Label, [switch]$AllowEqual)
    $canonicalRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd("\", "/")
    $canonicalPath = [System.IO.Path]::GetFullPath($Path)
    $prefix = $canonicalRoot + [System.IO.Path]::DirectorySeparatorChar
    $valid = $canonicalPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
    if ($AllowEqual -and $canonicalPath.Equals($canonicalRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        $valid = $true
    }
    if (-not $valid) {
        throw "$Label must stay under ${canonicalRoot}: $canonicalPath"
    }
}

function Get-RelativePathUnderRoot {
    param([string]$Root, [string]$Path, [string]$Label)
    $canonicalRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd("\", "/")
    $canonicalPath = [System.IO.Path]::GetFullPath($Path)
    Assert-UnderRoot -Root $canonicalRoot -Path $canonicalPath -Label $Label
    $prefix = $canonicalRoot + [System.IO.Path]::DirectorySeparatorChar
    return $canonicalPath.Substring($prefix.Length).Replace('\', '/')
}

function Get-FileSha256 {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $digest = $sha.ComputeHash($stream)
        } finally {
            $sha.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
    return ([System.BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
}

function Assert-SafeItem {
    param(
        [System.IO.FileSystemInfo]$Item,
        [string]$Label,
        [switch]$AllowRegularFileHardLink
    )
    if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not contain a reparse point: $($Item.FullName)"
    }
    $linkTypeProperty = $Item.PSObject.Properties["LinkType"]
    $linkType = if ($linkTypeProperty) { [string]$linkTypeProperty.Value } else { "" }
    if (-not [string]::IsNullOrWhiteSpace($linkType)) {
        $allowedHardLink = (
            $AllowRegularFileHardLink -and
            -not $Item.PSIsContainer -and
            $linkType.Equals("HardLink", [System.StringComparison]::OrdinalIgnoreCase)
        )
        if (-not $allowedHardLink) {
            throw "$Label must not contain a symbolic, junction, or hard link: $($Item.FullName)"
        }
    }
}

function Assert-SafePathAncestry {
    param(
        [string]$BoundaryRoot,
        [string]$Path,
        [string]$Label,
        [switch]$AllowLeafHardLink
    )
    $root = [System.IO.Path]::GetFullPath($BoundaryRoot).TrimEnd("\", "/")
    $candidate = [System.IO.Path]::GetFullPath($Path)
    Assert-UnderRoot -Root $root -Path $candidate -Label $Label -AllowEqual
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "$Label boundary root does not exist: $root"
    }
    Assert-SafeItem -Item (Get-Item -LiteralPath $root -Force) -Label "$Label boundary root"
    if ($candidate.Equals($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        return
    }
    $prefix = $root + [System.IO.Path]::DirectorySeparatorChar
    $relative = $candidate.Substring($prefix.Length)
    $current = $root
    foreach ($segment in $relative.Split([System.IO.Path]::DirectorySeparatorChar)) {
        if ([string]::IsNullOrWhiteSpace($segment)) {
            throw "$Label contains an empty path segment."
        }
        $current = Join-Path $current $segment
        if (-not (Test-Path -LiteralPath $current)) {
            break
        }
        $item = Get-Item -LiteralPath $current -Force
        $isLeaf = $current.Equals($candidate, [System.StringComparison]::OrdinalIgnoreCase)
        Assert-SafeItem `
            -Item $item `
            -Label $Label `
            -AllowRegularFileHardLink:($AllowLeafHardLink -and $isLeaf)
    }
}

function Get-TrustedContract {
    param([string]$Path)
    try {
        $contract = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    } catch {
        throw "Desktop Rust incremental cache contract is not valid JSON: $Path"
    }
    $expectedFileNamePatterns = @(
        '^(?:dep-graph|query-cache|work-products)\.bin$',
        '^metadata\.rmeta$',
        '^[0-9a-z]+\.o$',
        '^[0-9a-z]+\.bc\.z$',
        '^s-[0-9a-z]+-[0-9a-z]+\.lock$'
    )
    $actualFileNamePatterns = @($contract.incrementalFileNamePatterns | ForEach-Object { [string]$_ })
    if (
        [int]$contract.schemaVersion -ne 1 -or
        [string]$contract.name -cne "scriber-desktop-rust-incremental-cache" -or
        [int]$contract.revision -ne 2 -or
        [string]$contract.generation -cne "scriber-desktop-rust-incremental-v1" -or
        [string]$contract.target -cne "x86_64-pc-windows-msvc" -or
        [string]$contract.profile -cne "release" -or
        [string]$contract.crateDirectoryPattern -cne '^scriber_desktop(?:_lib)?-[0-9A-Za-z_-]+$' -or
        $actualFileNamePatterns.Count -ne $expectedFileNamePatterns.Count -or
        ($actualFileNamePatterns -join "`n") -cne ($expectedFileNamePatterns -join "`n") -or
        [int]$contract.maxCrateDirectories -ne 8 -or
        [int]$contract.maxDirectories -ne 4096 -or
        [int]$contract.maxFiles -ne 10000 -or
        [int64]$contract.maxBytes -ne 536870912 -or
        [int64]$contract.maxFileBytes -ne 268435456 -or
        [int64]$contract.maxManifestBytes -ne 262144 -or
        [int64]$contract.maxInventoryBytes -ne 8388608 -or
        [int]$contract.maxRelativePathLength -ne 240 -or
        [int]$contract.maxPathSegments -ne 16 -or
        [int]$contract.maxSegmentLength -ne 128
    ) {
        throw "Desktop Rust incremental cache contract has an unsupported identity or bounds."
    }
    return $contract
}

function Assert-SafeRelativePath {
    param([string]$Path, $Contract, [string]$Label)
    if (
        [string]::IsNullOrWhiteSpace($Path) -or
        $Path.Length -gt [int]$Contract.maxRelativePathLength -or
        $Path.Contains('\') -or
        $Path.Contains([char]0) -or
        $Path.StartsWith('/') -or
        $Path.EndsWith('/') -or
        $Path.Contains('//')
    ) {
        throw "$Label has an unsafe relative path: $Path"
    }
    $segments = @($Path.Split('/'))
    if ($segments.Count -gt [int]$Contract.maxPathSegments) {
        throw "$Label exceeds the path-segment bound: $Path"
    }
    foreach ($segment in $segments) {
        if (
            [string]::IsNullOrWhiteSpace($segment) -or
            $segment -eq '.' -or
            $segment -eq '..' -or
            $segment.Length -gt [int]$Contract.maxSegmentLength -or
            $segment -notmatch '^[0-9A-Za-z._-]+$'
        ) {
            throw "$Label contains an unsafe path segment: $Path"
        }
    }
}

function Assert-RustIncrementalFileName {
    param([string]$Name, $Contract, [string]$Label)
    foreach ($pattern in @($Contract.incrementalFileNamePatterns)) {
        if ($Name -cmatch [string]$pattern) {
            return
        }
    }
    throw "$Label contains a file that is not an allowlisted rustc incremental artifact: $Name"
}

function Get-SafeTree {
    param(
        [string]$Root,
        [string]$RelativeTo,
        $Contract,
        [string]$Label,
        [switch]$AllowSourceFileHardLinks,
        [switch]$EnforceIncrementalFileNames
    )
    Assert-SafePathAncestry -BoundaryRoot $RelativeTo -Path $Root -Label "$Label root"
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "$Label root is not a directory: $Root"
    }
    $pending = New-Object System.Collections.Queue
    $pending.Enqueue([System.IO.Path]::GetFullPath($Root))
    $directories = New-Object System.Collections.Generic.List[string]
    $files = New-Object System.Collections.Generic.List[object]
    [int64]$totalBytes = 0
    while ($pending.Count -gt 0) {
        $directory = [string]$pending.Dequeue()
        foreach ($child in @(Get-ChildItem -LiteralPath $directory -Force)) {
            Assert-SafeItem -Item $child -Label $Label -AllowRegularFileHardLink:$AllowSourceFileHardLinks
            $relative = Get-RelativePathUnderRoot -Root $RelativeTo -Path $child.FullName -Label $Label
            Assert-SafeRelativePath -Path $relative -Contract $Contract -Label $Label
            if ($child.PSIsContainer) {
                $directories.Add($relative)
                if ($directories.Count -gt [int]$Contract.maxDirectories) {
                    throw "$Label exceeds the directory-count bound."
                }
                $pending.Enqueue($child.FullName)
            } else {
                if ($EnforceIncrementalFileNames) {
                    Assert-RustIncrementalFileName -Name $child.Name -Contract $Contract -Label $Label
                }
                if ([int64]$child.Length -gt [int64]$Contract.maxFileBytes) {
                    throw "$Label contains a file larger than the per-file bound: $relative"
                }
                $totalBytes += [int64]$child.Length
                if ($totalBytes -gt [int64]$Contract.maxBytes) {
                    throw "$Label exceeds the total-byte bound."
                }
                $files.Add([ordered]@{
                    path = $relative
                    length = [int64]$child.Length
                    sha256 = Get-FileSha256 -Path $child.FullName
                })
                if ($files.Count -gt [int]$Contract.maxFiles) {
                    throw "$Label exceeds the file-count bound."
                }
            }
        }
    }
    return [pscustomobject]@{
        directories = @($directories | Sort-Object)
        files = @($files | Sort-Object { [string]$_.path })
        totalBytes = $totalBytes
    }
}

function Assert-ExactStringSet {
    param([string[]]$Actual, [string[]]$Expected, [string]$Label)
    $actualSorted = @($Actual | Sort-Object -Unique)
    $expectedSorted = @($Expected | Sort-Object -Unique)
    if (
        $actualSorted.Count -ne @($Actual).Count -or
        $expectedSorted.Count -ne @($Expected).Count -or
        ($actualSorted -join "`n") -cne ($expectedSorted -join "`n")
    ) {
        throw "$Label does not match the attested inventory."
    }
}

function Test-CacheEnvelope {
    param(
        [string]$Root,
        [string]$ExpectedMatchedCacheKey,
        [string]$ExpectedCurrentInputKey,
        [string]$ExpectedDependencyScopeKey,
        [string]$ExpectedRefScopeKey,
        [string]$ExpectedRunnerOs,
        [string]$ContractSha256,
        $Contract
    )
    $manifestPath = Join-Path $Root "manifest.json"
    $inventoryPath = Join-Path $Root "inventory.json"
    foreach ($path in @($manifestPath, $inventoryPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Desktop Rust incremental cache envelope is incomplete."
        }
    }
    if ([int64](Get-Item -LiteralPath $manifestPath -Force).Length -gt [int64]$Contract.maxManifestBytes) {
        throw "Desktop Rust incremental cache manifest exceeds its size bound."
    }
    if ([int64](Get-Item -LiteralPath $inventoryPath -Force).Length -gt [int64]$Contract.maxInventoryBytes) {
        throw "Desktop Rust incremental cache inventory exceeds its size bound."
    }
    $tree = Get-SafeTree -Root $Root -RelativeTo $Root -Contract $Contract -Label "Desktop Rust incremental cache"
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $inventory = Get-Content -LiteralPath $inventoryPath -Raw | ConvertFrom-Json
    } catch {
        throw "Desktop Rust incremental cache metadata is not valid JSON."
    }

    $sourceInputKey = ([string]$manifest.inputKey).Trim()
    $expectedFullKey = "$([string]$Contract.generation)-$ExpectedRunnerOs-$ExpectedRefScopeKey-$ExpectedDependencyScopeKey-$sourceInputKey"
    if (
        [string]$manifest.apiVersion -cne "1" -or
        [string]$manifest.generation -cne [string]$Contract.generation -or
        $sourceInputKey -cnotmatch '^[0-9a-f]{64}$' -or
        $ExpectedCurrentInputKey -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$manifest.dependencyScopeKey -cne $ExpectedDependencyScopeKey -or
        [string]$manifest.refScopeKey -cne $ExpectedRefScopeKey -or
        [string]$manifest.target -cne [string]$Contract.target -or
        [string]$manifest.profile -cne [string]$Contract.profile -or
        [string]$manifest.contractSha256 -cne $ContractSha256 -or
        [string]$manifest.sourceCommit -cnotmatch '^[0-9a-f]{40}$' -or
        [string]$manifest.buildEvidence.timingSha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$manifest.buildEvidence.executableSha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [int64]$manifest.buildEvidence.executableLength -le 0 -or
        $ExpectedMatchedCacheKey -cne $expectedFullKey
    ) {
        throw "Desktop Rust incremental cache identity or matched key is invalid."
    }
    if (
        [string]$inventory.apiVersion -cne "1" -or
        [string]$manifest.inventory.sha256 -cne (Get-FileSha256 -Path $inventoryPath) -or
        [int64]$manifest.inventory.length -ne [int64](Get-Item -LiteralPath $inventoryPath -Force).Length
    ) {
        throw "Desktop Rust incremental cache inventory attestation is invalid."
    }

    $directoryPaths = @($inventory.directories | ForEach-Object { [string]$_ })
    $fileRecords = @($inventory.files)
    if (
        $directoryPaths.Count -gt [int]$Contract.maxDirectories -or
        $fileRecords.Count -le 0 -or
        $fileRecords.Count -gt [int]$Contract.maxFiles -or
        [int]$manifest.content.directoryCount -ne $directoryPaths.Count -or
        [int]$manifest.content.fileCount -ne $fileRecords.Count -or
        [int64]$manifest.content.totalBytes -gt [int64]$Contract.maxBytes
    ) {
        throw "Desktop Rust incremental cache counts exceed the checked-in bounds."
    }

    $seenDirectories = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)
    $directoryCrateNames = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)
    foreach ($relative in $directoryPaths) {
        Assert-SafeRelativePath -Path $relative -Contract $Contract -Label "Desktop Rust incremental cache directory"
        $segments = @($relative.Split('/'))
        if (
            $segments.Count -lt 2 -or
            $segments[0] -cne 'payload' -or
            $segments[1] -cnotmatch [string]$Contract.crateDirectoryPattern -or
            -not $seenDirectories.Add($relative)
        ) {
            throw "Desktop Rust incremental cache contains a duplicate or foreign directory path."
        }
        [void]$directoryCrateNames.Add($segments[1])
        $parentIndex = $relative.LastIndexOf('/')
        $parent = if ($parentIndex -gt 0) { $relative.Substring(0, $parentIndex) } else { "" }
        if ($parent -cne 'payload' -and -not $seenDirectories.Contains($parent)) {
            throw "Desktop Rust incremental cache directory parent is not inventoried: $relative"
        }
    }
    $seenFiles = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)
    [int64]$actualTotalBytes = 0
    $crateNames = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)
    foreach ($record in $fileRecords) {
        $relative = [string]$record.path
        Assert-SafeRelativePath -Path $relative -Contract $Contract -Label "Desktop Rust incremental cache file"
        $segments = @($relative.Split('/'))
        if (
            $segments.Count -lt 3 -or
            $segments[0] -cne 'payload' -or
            $segments[1] -cnotmatch [string]$Contract.crateDirectoryPattern -or
            -not $seenFiles.Add($relative) -or
            -not $seenDirectories.Contains("payload/$($segments[1])")
        ) {
            throw "Desktop Rust incremental cache contains a duplicate or foreign file path."
        }
        Assert-RustIncrementalFileName -Name $segments[-1] -Contract $Contract -Label "Desktop Rust incremental cache"
        [void]$crateNames.Add($segments[1])
        $parent = $relative.Substring(0, $relative.LastIndexOf('/'))
        if (-not $seenDirectories.Contains($parent)) {
            throw "Desktop Rust incremental cache file parent is not inventoried: $relative"
        }
        $path = [System.IO.Path]::GetFullPath((Join-Path $Root ($relative -replace '/', '\')))
        Assert-UnderRoot -Root $Root -Path $path -Label "Desktop Rust incremental cache file"
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Desktop Rust incremental cache file is missing: $relative"
        }
        $item = Get-Item -LiteralPath $path -Force
        if (
            [int64]$record.length -lt 0 -or
            [int64]$record.length -gt [int64]$Contract.maxFileBytes -or
            [int64]$record.length -ne [int64]$item.Length -or
            [string]$record.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            [string]$record.sha256 -cne (Get-FileSha256 -Path $path)
        ) {
            throw "Desktop Rust incremental cache file attestation is invalid: $relative"
        }
        $actualTotalBytes += [int64]$item.Length
        if ($actualTotalBytes -gt [int64]$Contract.maxBytes) {
            throw "Desktop Rust incremental cache exceeds the total-byte bound."
        }
    }

    $manifestCrates = @($manifest.content.crateDirectories | ForEach-Object { [string]$_ })
    if (
        $crateNames.Count -le 0 -or
        $crateNames.Count -gt [int]$Contract.maxCrateDirectories -or
        $directoryCrateNames.Count -ne $crateNames.Count -or
        [int]$manifest.content.crateDirectoryCount -ne $crateNames.Count -or
        [int64]$manifest.content.totalBytes -ne $actualTotalBytes
    ) {
        throw "Desktop Rust incremental cache crate or byte totals are invalid."
    }
    Assert-ExactStringSet -Actual $manifestCrates -Expected @($crateNames | Sort-Object) -Label "Desktop Rust incremental crate set"
    Assert-ExactStringSet -Actual @($directoryCrateNames | Sort-Object) -Expected @($crateNames | Sort-Object) -Label "Desktop Rust incremental directory crate set"
    Assert-ExactStringSet -Actual $tree.directories -Expected (@('payload') + $directoryPaths) -Label "Desktop Rust incremental directory tree"
    Assert-ExactStringSet -Actual @($tree.files | ForEach-Object { [string]$_.path }) -Expected (@('inventory.json', 'manifest.json') + @($fileRecords | ForEach-Object { [string]$_.path })) -Label "Desktop Rust incremental file tree"

    return [pscustomobject]@{
        manifest = $manifest
        inventory = $inventory
        sourceInputKey = $sourceInputKey
        exactCurrent = $sourceInputKey -ceq $ExpectedCurrentInputKey
        crateDirectories = @($crateNames | Sort-Object)
        fileCount = $fileRecords.Count
        directoryCount = $directoryPaths.Count
        totalBytes = $actualTotalBytes
    }
}

function Remove-CheckedDirectory {
    param([string]$BoundaryRoot, [string]$Path, [string]$Label)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    Assert-UnderRoot -Root $BoundaryRoot -Path $resolved -Label $Label
    if (Test-Path -LiteralPath $resolved) {
        Assert-SafePathAncestry -BoundaryRoot $BoundaryRoot -Path $resolved -Label $Label
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

Write-GitHubOutput -Name "usable" -Value "false"
Write-GitHubOutput -Name "staged" -Value "false"
foreach ($value in @($CurrentInputKey, $DependencyScopeKey, $RefScopeKey)) {
    if ($value -cnotmatch '^[0-9a-f]{64}$') {
        throw "Desktop Rust incremental cache keys must be lowercase SHA-256 values."
    }
}
if ($RunnerOs -cne "Windows") {
    throw "Desktop Rust incremental caching supports only the Windows release target."
}
$normalizedSourceCommit = ([string]$SourceCommit).Trim().ToLowerInvariant()
if ($Mode -eq "Export" -and $normalizedSourceCommit -cnotmatch '^[0-9a-f]{40}$') {
    throw "Desktop Rust incremental cache export requires a lowercase 40-hex source commit."
}

$buildRoot = Resolve-RepoPath -Path "build" -Label "Build root"
$resolvedCacheRoot = Resolve-RepoPath -Path $CacheRoot -Label "Desktop Rust incremental cache root"
Assert-UnderRoot -Root $buildRoot -Path $resolvedCacheRoot -Label "Desktop Rust incremental cache root"
$resolvedTargetDir = Resolve-RepoPath -Path $TargetDir -Label "Desktop Rust target directory"
$defaultTargetDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "Frontend\src-tauri\target"))
if (-not $resolvedTargetDir.Equals($defaultTargetDir, [System.StringComparison]::OrdinalIgnoreCase)) {
    if ($env:SCRIBER_DESKTOP_INCREMENTAL_CACHE_TEST_MODE -ne "1" -or [string]::IsNullOrWhiteSpace([string]$env:PYTEST_CURRENT_TEST)) {
        throw "A non-production Desktop Rust target is allowed only from the focused pytest contract fixture."
    }
    Assert-UnderRoot -Root $buildRoot -Path $resolvedTargetDir -Label "Test Desktop Rust target directory"
}
$resolvedContractPath = Resolve-RepoPath -Path $ContractPath -Label "Desktop Rust incremental cache contract"
$defaultContractPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "packaging\desktop-rust-incremental-cache-contract.json"))
if (-not $resolvedContractPath.Equals($defaultContractPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Desktop Rust incremental cache import must use the checked-in production contract."
}
Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path $resolvedContractPath -Label "Desktop Rust incremental cache contract"
$contract = Get-TrustedContract -Path $resolvedContractPath
$contractSha256 = Get-FileSha256 -Path $resolvedContractPath
$incrementalRoot = Join-Path $resolvedTargetDir "release\incremental"

if ($Mode -eq "Export") {
    $resolvedBuildTimingPath = Resolve-RepoPath -Path $BuildTimingPath -Label "Desktop build timing evidence"
    $resolvedBinaryPath = Resolve-RepoPath -Path $BinaryPath -Label "Desktop executable build evidence"
    foreach ($path in @($resolvedBuildTimingPath, $resolvedBinaryPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Desktop Rust incremental export requires completed app-build evidence: $path"
        }
    }
    Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path $resolvedBuildTimingPath -Label "Desktop app-build timing evidence"
    Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path $resolvedBinaryPath -Label "Desktop executable build evidence" -AllowLeafHardLink
    try {
        $timing = Get-Content -LiteralPath $resolvedBuildTimingPath -Raw | ConvertFrom-Json
    } catch {
        throw "Desktop app-build timing evidence is not valid JSON."
    }
    if (
        [string]$timing.buildMode.artifactKind -cne "installer" -or
        [bool]$timing.buildMode.prebuiltTauriApp -or
        -not [bool]$timing.buildMode.tauriAppBuiltBeforeBundle -or
        -not [bool]$timing.buildMode.installerBuilt
    ) {
        throw "Desktop Rust incremental export requires a successful fresh Desktop app build, not a prebuilt-app bundle."
    }
    if (-not (Test-Path -LiteralPath $incrementalRoot -PathType Container)) {
        Write-Host "No Desktop Rust incremental directory was produced; no bounded cache will be staged."
        exit 0
    }
    Assert-SafePathAncestry -BoundaryRoot $resolvedTargetDir -Path $incrementalRoot -Label "Cargo incremental root"
    $crateDirectories = @(
        Get-ChildItem -LiteralPath $incrementalRoot -Directory -Force |
            Where-Object { $_.Name -cmatch [string]$contract.crateDirectoryPattern } |
            Sort-Object Name
    )
    if ($crateDirectories.Count -eq 0) {
        Write-Host "Cargo produced no Desktop app-crate incremental state; no bounded cache will be staged."
        exit 0
    }
    if ($crateDirectories.Count -gt [int]$contract.maxCrateDirectories) {
        throw "Cargo produced more Desktop incremental crate directories than the contract allows."
    }
    [int]$sourceDirectoryCount = 0
    [int]$sourceFileCount = 0
    [int64]$sourceTotalBytes = 0
    foreach ($directory in $crateDirectories) {
        Assert-SafeItem -Item $directory -Label "Desktop Rust incremental source directory"
        $sourceTree = Get-SafeTree -Root $directory.FullName -RelativeTo $incrementalRoot -Contract $contract -Label "Desktop Rust incremental source" -AllowSourceFileHardLinks -EnforceIncrementalFileNames
        $sourceDirectoryCount += 1 + @($sourceTree.directories).Count
        $sourceFileCount += @($sourceTree.files).Count
        $sourceTotalBytes += [int64]$sourceTree.totalBytes
        if (
            $sourceDirectoryCount -gt [int]$contract.maxDirectories -or
            $sourceFileCount -gt [int]$contract.maxFiles -or
            $sourceTotalBytes -gt [int64]$contract.maxBytes
        ) {
            throw "Combined Desktop Rust incremental source exceeds the checked-in directory, file, or byte bound."
        }
    }

    if (Test-Path -LiteralPath $resolvedCacheRoot) {
        try {
            Assert-SafePathAncestry -BoundaryRoot $buildRoot -Path $resolvedCacheRoot -Label "Existing Desktop Rust incremental cache root"
            [void](Get-SafeTree -Root $resolvedCacheRoot -RelativeTo $resolvedCacheRoot -Contract $contract -Label "Existing Desktop Rust incremental cache")
            Remove-CheckedDirectory -BoundaryRoot $buildRoot -Path $resolvedCacheRoot -Label "Existing Desktop Rust incremental cache root"
        } catch {
            Write-Warning "Skipping optional Desktop Rust incremental re-export because the restored cache root is unsafe; the successful installer build remains authoritative: $($_.Exception.Message)"
            Write-GitHubOutput -Name "skip-reason" -Value "unsafe-existing-envelope"
            exit 0
        }
    }
    New-Item -ItemType Directory -Force -Path $resolvedCacheRoot | Out-Null
    $payloadRoot = Join-Path $resolvedCacheRoot "payload"
    New-Item -ItemType Directory -Force -Path $payloadRoot | Out-Null
    foreach ($directory in $crateDirectories) {
        Copy-Item -LiteralPath $directory.FullName -Destination $payloadRoot -Recurse -Force
    }

    $payloadTree = Get-SafeTree -Root $payloadRoot -RelativeTo $resolvedCacheRoot -Contract $contract -Label "Exported Desktop Rust incremental payload" -EnforceIncrementalFileNames
    $crateNames = @($crateDirectories | ForEach-Object { $_.Name })
    $inventory = [ordered]@{
        apiVersion = "1"
        directories = @($payloadTree.directories)
        files = @($payloadTree.files)
    }
    $inventoryPath = Join-Path $resolvedCacheRoot "inventory.json"
    $inventory | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $inventoryPath -Encoding utf8
    $inventoryItem = Get-Item -LiteralPath $inventoryPath -Force
    if ([int64]$inventoryItem.Length -gt [int64]$contract.maxInventoryBytes) {
        throw "Exported Desktop Rust incremental inventory exceeds its size bound."
    }
    $binaryItem = Get-Item -LiteralPath $resolvedBinaryPath -Force
    $manifest = [ordered]@{
        apiVersion = "1"
        generation = [string]$contract.generation
        inputKey = $CurrentInputKey
        dependencyScopeKey = $DependencyScopeKey
        refScopeKey = $RefScopeKey
        contractSha256 = $contractSha256
        target = [string]$contract.target
        profile = [string]$contract.profile
        sourceCommit = $normalizedSourceCommit
        buildEvidence = [ordered]@{
            timingSha256 = Get-FileSha256 -Path $resolvedBuildTimingPath
            executableLength = [int64]$binaryItem.Length
            executableSha256 = Get-FileSha256 -Path $resolvedBinaryPath
        }
        content = [ordered]@{
            crateDirectoryCount = $crateNames.Count
            crateDirectories = $crateNames
            directoryCount = @($payloadTree.directories).Count
            fileCount = @($payloadTree.files).Count
            totalBytes = [int64]$payloadTree.totalBytes
        }
        inventory = [ordered]@{
            length = [int64]$inventoryItem.Length
            sha256 = Get-FileSha256 -Path $inventoryPath
        }
        exportedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    }
    $manifestPath = Join-Path $resolvedCacheRoot "manifest.json"
    $manifest | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    if ([int64](Get-Item -LiteralPath $manifestPath -Force).Length -gt [int64]$contract.maxManifestBytes) {
        throw "Exported Desktop Rust incremental manifest exceeds its size bound."
    }
    $fullKey = "$([string]$contract.generation)-$RunnerOs-$RefScopeKey-$DependencyScopeKey-$CurrentInputKey"
    $validated = Test-CacheEnvelope `
        -Root $resolvedCacheRoot `
        -ExpectedMatchedCacheKey $fullKey `
        -ExpectedCurrentInputKey $CurrentInputKey `
        -ExpectedDependencyScopeKey $DependencyScopeKey `
        -ExpectedRefScopeKey $RefScopeKey `
        -ExpectedRunnerOs $RunnerOs `
        -ContractSha256 $contractSha256 `
        -Contract $contract
    Write-Host "Staged bounded Desktop Rust incremental cache: crates=$($validated.crateDirectories.Count); files=$($validated.fileCount); bytes=$($validated.totalBytes)."
    Write-GitHubOutput -Name "staged" -Value "true"
    Write-GitHubOutput -Name "file-count" -Value ([string]$validated.fileCount)
    Write-GitHubOutput -Name "directory-count" -Value ([string]$validated.directoryCount)
    Write-GitHubOutput -Name "total-bytes" -Value ([string]$validated.totalBytes)
    Write-GitHubOutput -Name "source-input-key" -Value $CurrentInputKey
    exit 0
}

if ([string]::IsNullOrWhiteSpace($MatchedCacheKey)) {
    Write-Host "No ref-local Desktop Rust incremental Actions cache matched; Cargo will build without imported app state."
    exit 0
}
if (-not (Test-Path -LiteralPath $resolvedCacheRoot -PathType Container)) {
    Write-Host "No Desktop Rust incremental cache envelope was restored; Cargo will build without imported app state."
    exit 0
}

$stagingRoot = $null
$promotionStarted = $false
try {
    Assert-SafePathAncestry -BoundaryRoot $buildRoot -Path $resolvedCacheRoot -Label "Restored Desktop Rust incremental cache root"
    $validated = Test-CacheEnvelope `
        -Root $resolvedCacheRoot `
        -ExpectedMatchedCacheKey $MatchedCacheKey `
        -ExpectedCurrentInputKey $CurrentInputKey `
        -ExpectedDependencyScopeKey $DependencyScopeKey `
        -ExpectedRefScopeKey $RefScopeKey `
        -ExpectedRunnerOs $RunnerOs `
        -ContractSha256 $contractSha256 `
        -Contract $contract

    $releaseRoot = Join-Path $resolvedTargetDir "release"
    New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
    Assert-SafePathAncestry -BoundaryRoot $resolvedTargetDir -Path $releaseRoot -Label "Desktop Rust release target"
    $stagingRoot = Join-Path $releaseRoot (".scriber-desktop-incremental-import-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $stagingRoot | Out-Null
    $payloadRoot = Join-Path $resolvedCacheRoot "payload"
    foreach ($crateName in $validated.crateDirectories) {
        $source = Join-Path $payloadRoot $crateName
        Copy-Item -LiteralPath $source -Destination $stagingRoot -Recurse -Force
    }
    $stagedTree = Get-SafeTree -Root $stagingRoot -RelativeTo $stagingRoot -Contract $contract -Label "Staged Desktop Rust incremental import" -EnforceIncrementalFileNames
    $expectedStagedDirectories = @($validated.inventory.directories | ForEach-Object { ([string]$_).Substring('payload/'.Length) })
    $expectedStagedFiles = @($validated.inventory.files | ForEach-Object { ([string]$_.path).Substring('payload/'.Length) })
    Assert-ExactStringSet -Actual $stagedTree.directories -Expected $expectedStagedDirectories -Label "Staged Desktop Rust incremental directories"
    Assert-ExactStringSet -Actual @($stagedTree.files | ForEach-Object { [string]$_.path }) -Expected $expectedStagedFiles -Label "Staged Desktop Rust incremental files"
    $stagedByPath = @{}
    foreach ($record in $stagedTree.files) {
        $stagedByPath[[string]$record.path] = $record
    }
    foreach ($record in @($validated.inventory.files)) {
        $relative = ([string]$record.path).Substring('payload/'.Length)
        $staged = $stagedByPath[$relative]
        if ([int64]$staged.length -ne [int64]$record.length -or [string]$staged.sha256 -cne [string]$record.sha256) {
            throw "Desktop Rust incremental staging copy changed an inventoried file: $relative"
        }
    }

    New-Item -ItemType Directory -Force -Path $incrementalRoot | Out-Null
    Assert-SafePathAncestry -BoundaryRoot $resolvedTargetDir -Path $incrementalRoot -Label "Cargo incremental root"
    $promotionStarted = $true
    foreach ($existing in @(Get-ChildItem -LiteralPath $incrementalRoot -Directory -Force | Where-Object { $_.Name -cmatch [string]$contract.crateDirectoryPattern })) {
        [void](Get-SafeTree -Root $existing.FullName -RelativeTo $incrementalRoot -Contract $contract -Label "Existing Desktop Rust incremental state" -AllowSourceFileHardLinks -EnforceIncrementalFileNames)
        Remove-CheckedDirectory -BoundaryRoot $incrementalRoot -Path $existing.FullName -Label "Existing Desktop Rust incremental state"
    }
    foreach ($crateName in $validated.crateDirectories) {
        $source = Join-Path $stagingRoot $crateName
        $destination = Join-Path $incrementalRoot $crateName
        Assert-UnderRoot -Root $incrementalRoot -Path $destination -Label "Desktop Rust incremental import destination"
        Move-Item -LiteralPath $source -Destination $destination
    }
    Remove-CheckedDirectory -BoundaryRoot $releaseRoot -Path $stagingRoot -Label "Desktop Rust incremental import staging"
    $stagingRoot = $null
    Write-Host "Imported bounded Desktop Rust incremental cache: exactCurrent=$($validated.exactCurrent.ToString().ToLowerInvariant()); crates=$($validated.crateDirectories.Count); files=$($validated.fileCount); bytes=$($validated.totalBytes)."
    Write-GitHubOutput -Name "usable" -Value "true"
    Write-GitHubOutput -Name "exact-current" -Value $validated.exactCurrent.ToString().ToLowerInvariant()
    Write-GitHubOutput -Name "file-count" -Value ([string]$validated.fileCount)
    Write-GitHubOutput -Name "directory-count" -Value ([string]$validated.directoryCount)
    Write-GitHubOutput -Name "total-bytes" -Value ([string]$validated.totalBytes)
    Write-GitHubOutput -Name "source-input-key" -Value $validated.sourceInputKey
} catch {
    if ($stagingRoot -and (Test-Path -LiteralPath $stagingRoot)) {
        try {
            $releaseRoot = Join-Path $resolvedTargetDir "release"
            Remove-CheckedDirectory -BoundaryRoot $releaseRoot -Path $stagingRoot -Label "Rejected Desktop Rust incremental import staging"
        } catch {
            Write-Warning "Could not remove rejected Desktop Rust incremental staging: $($_.Exception.Message)"
        }
    }
    if ($promotionStarted -and (Test-Path -LiteralPath $incrementalRoot -PathType Container)) {
        foreach ($existing in @(Get-ChildItem -LiteralPath $incrementalRoot -Directory -Force | Where-Object { $_.Name -cmatch [string]$contract.crateDirectoryPattern })) {
            try {
                Remove-CheckedDirectory -BoundaryRoot $incrementalRoot -Path $existing.FullName -Label "Partial Desktop Rust incremental import"
            } catch {
                Write-Warning "Could not remove a partial Desktop Rust incremental directory: $($_.Exception.Message)"
            }
        }
    }
    Write-Warning "Ignoring rejected Desktop Rust incremental cache; Cargo will rebuild normally: $($_.Exception.Message)"
}
