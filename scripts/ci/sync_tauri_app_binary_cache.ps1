param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Export", "Import")]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$CacheKey,
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$CacheRoot = "build\tauri-app-binary-cache",
    [string]$BinaryPath = "Frontend\src-tauri\target\release\scriber-desktop.exe",
    [string]$PackageLockPath = "Frontend\package-lock.json",
    [string]$ContractPath = "packaging\tauri-cli-cache-contract.json",
    [string]$TauriCliPackagePath = "Frontend\node_modules\@tauri-apps\cli",
    [string]$TauriCliPlatformPackagePath = "Frontend\node_modules\@tauri-apps\cli-win32-x64-msvc"
)

$ErrorActionPreference = "Stop"

function Write-GitHubOutput {
    param([string]$Name, [string]$Value)
    if ($env:GITHUB_OUTPUT) {
        "$Name=$Value" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

function Resolve-RepoPath {
    param([string]$Root, [string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Root $Path))
}

function Assert-UnderOrEqualRoot {
    param([string]$Root, [string]$Path, [string]$Label)
    $canonicalRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd("\", "/")
    $canonicalPath = [System.IO.Path]::GetFullPath($Path)
    $prefix = $canonicalRoot + [System.IO.Path]::DirectorySeparatorChar
    if (
        -not $canonicalPath.Equals($canonicalRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
        -not $canonicalPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "$Label must stay under ${canonicalRoot}: $canonicalPath"
    }
}

function Assert-UnderRoot {
    param([string]$Root, [string]$Path, [string]$Label)
    $canonicalRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd("\", "/")
    $canonicalPath = [System.IO.Path]::GetFullPath($Path)
    $prefix = $canonicalRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $canonicalPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
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

function Assert-SafeFileSystemItem {
    param([System.IO.FileSystemInfo]$Item, [string]$Label)
    if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a reparse point: $($Item.FullName)"
    }
    $linkTypeProperty = $Item.PSObject.Properties["LinkType"]
    if ($linkTypeProperty -and -not [string]::IsNullOrWhiteSpace([string]$linkTypeProperty.Value)) {
        throw "$Label must not be a symbolic, junction, or hard link: $($Item.FullName)"
    }
    if (-not $Item.PSIsContainer) {
        $hardLinks = @(& fsutil.exe hardlink list $Item.FullName 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "$Label hard-link count could not be verified: $($Item.FullName)"
        }
        $hardLinkCount = @(
            $hardLinks |
                ForEach-Object { ([string]$_).Trim() } |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        ).Count
        if ($hardLinkCount -ne 1) {
            throw "$Label must have exactly one filesystem link: $($Item.FullName)"
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
    $canonicalRoot = [System.IO.Path]::GetFullPath($BoundaryRoot).TrimEnd("\", "/")
    $canonicalPath = [System.IO.Path]::GetFullPath($Path)
    Assert-UnderOrEqualRoot -Root $canonicalRoot -Path $canonicalPath -Label $Label
    if (-not (Test-Path -LiteralPath $canonicalRoot)) {
        throw "$Label boundary root does not exist: $canonicalRoot"
    }
    Assert-SafeFileSystemItem -Item (Get-Item -LiteralPath $canonicalRoot -Force) -Label "$Label boundary root"
    if ($canonicalPath.Equals($canonicalRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        return
    }
    $prefix = $canonicalRoot + [System.IO.Path]::DirectorySeparatorChar
    $relative = $canonicalPath.Substring($prefix.Length)
    $current = $canonicalRoot
    foreach ($segment in $relative.Split([System.IO.Path]::DirectorySeparatorChar)) {
        if ([string]::IsNullOrWhiteSpace($segment)) {
            throw "$Label contains an empty path segment: $canonicalPath"
        }
        $current = Join-Path $current $segment
        if (-not (Test-Path -LiteralPath $current)) {
            break
        }
        $item = Get-Item -LiteralPath $current -Force
        $isLeaf = $current.Equals($canonicalPath, [System.StringComparison]::OrdinalIgnoreCase)
        if ($AllowLeafHardLink -and $isLeaf -and -not $item.PSIsContainer) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label must not be a reparse point: $($item.FullName)"
            }
            $linkTypeProperty = $item.PSObject.Properties["LinkType"]
            $linkType = if ($linkTypeProperty) { [string]$linkTypeProperty.Value } else { "" }
            if (
                -not [string]::IsNullOrWhiteSpace($linkType) -and
                -not $linkType.Equals("HardLink", [System.StringComparison]::OrdinalIgnoreCase)
            ) {
                throw "$Label must not be a symbolic or junction link: $($item.FullName)"
            }
        } else {
            Assert-SafeFileSystemItem -Item $item -Label $Label
        }
    }
}

function Get-SafeTreeRelativeFiles {
    param([string]$Root, [string]$RelativeTo, [string]$Label)
    Assert-SafePathAncestry -BoundaryRoot $RelativeTo -Path $Root -Label "$Label root"
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "$Label root is not a directory: $Root"
    }
    $pending = New-Object System.Collections.Queue
    $pending.Enqueue([System.IO.Path]::GetFullPath($Root))
    $files = @()
    while ($pending.Count -gt 0) {
        $directory = [string]$pending.Dequeue()
        foreach ($child in @(Get-ChildItem -LiteralPath $directory -Force)) {
            Assert-SafeFileSystemItem -Item $child -Label $Label
            if ($child.PSIsContainer) {
                $pending.Enqueue($child.FullName)
            } else {
                $files += Get-RelativePathUnderRoot -Root $RelativeTo -Path $child.FullName -Label $Label
            }
        }
    }
    return @($files | Sort-Object)
}

function Assert-CleanNodeEnvironment {
    foreach ($name in @("NODE_OPTIONS", "NODE_PATH", "NAPI_RS_NATIVE_LIBRARY_PATH")) {
        if (Test-Path -LiteralPath ("Env:{0}" -f $name)) {
            throw "$name must be unset before an attested Tauri CLI is executed."
        }
    }
}

function Get-FileSha256 {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            $digest = $sha256.ComputeHash($stream)
        } finally {
            $sha256.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
    return ([System.BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
}

function Get-NormalizedFileVersion {
    param([string]$Path)
    $versionInfo = (Get-Item -LiteralPath $Path).VersionInfo
    $candidate = [string]$versionInfo.ProductVersion
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = [string]$versionInfo.FileVersion
    }
    return $candidate.Trim()
}

function Test-ExpectedVersion {
    param([string]$Actual, [string]$Expected)
    if ([string]::IsNullOrWhiteSpace($Actual)) {
        return $false
    }
    return $Actual -eq $Expected -or $Actual -eq "$Expected.0" -or $Actual.StartsWith("$Expected+", [System.StringComparison]::Ordinal)
}

function Test-OptionalSourceCommit {
    param([string]$Value)
    return [string]::IsNullOrWhiteSpace($Value) -or $Value -match '^[0-9a-f]{40}$'
}

function Get-TrustedTauriCliContract {
    param([string]$Path)
    try {
        $contractBytes = [System.IO.File]::ReadAllBytes($Path)
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $raw = $utf8.GetString($contractBytes) | ConvertFrom-Json
    } catch {
        throw "Checked-in Tauri CLI cache contract is not valid JSON: $Path"
    }
    $expectedPackageNames = @("@tauri-apps/cli", "@tauri-apps/cli-win32-x64-msvc")
    $expectedFilePaths = @(
        "tauri-cli/node_modules/@tauri-apps/cli/package.json",
        "tauri-cli/node_modules/@tauri-apps/cli/tauri.js",
        "tauri-cli/node_modules/@tauri-apps/cli/main.js",
        "tauri-cli/node_modules/@tauri-apps/cli/index.js",
        "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/package.json",
        "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/cli.win32-x64-msvc.node"
    )
    if (
        [int]$raw.schemaVersion -ne 1 -or
        [string]$raw.name -ne "scriber-tauri-cli-cache-contract" -or
        [string]$raw.revision -notmatch '^tauri-cli-\d+\.\d+\.\d+-win32-x64-msvc-v\d+$' -or
        [string]$raw.target -ne "win32-x64-msvc" -or
        [string]$raw.version -notmatch '^\d+\.\d+\.\d+$' -or
        [string]$raw.versionOutput -ne "tauri-cli $([string]$raw.version)" -or
        [string]$raw.entrypoint -ne "tauri-cli/node_modules/@tauri-apps/cli/tauri.js"
    ) {
        throw "Checked-in Tauri CLI cache contract has an unsupported identity."
    }
    $packagesByName = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::Ordinal)
    foreach ($package in @($raw.packages)) {
        $name = [string]$package.name
        if (
            -not $expectedPackageNames.Contains($name) -or
            $packagesByName.ContainsKey($name) -or
            [string]$package.version -ne [string]$raw.version -or
            [string]$package.integrity -notmatch '^sha512-[A-Za-z0-9+/=]+$'
        ) {
            throw "Checked-in Tauri CLI cache contract contains an invalid package record."
        }
        $packagesByName.Add($name, $package)
    }
    if ($packagesByName.Count -ne $expectedPackageNames.Count) {
        throw "Checked-in Tauri CLI cache contract must attest exactly two packages."
    }
    $filesByPath = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::Ordinal)
    foreach ($record in @($raw.files)) {
        $relativePath = [string]$record.path
        if (
            -not $expectedFilePaths.Contains($relativePath) -or
            $filesByPath.ContainsKey($relativePath) -or
            [int64]$record.length -le 0 -or
            [string]$record.sha256 -notmatch '^[0-9a-f]{64}$'
        ) {
            throw "Checked-in Tauri CLI cache contract contains an invalid file record."
        }
        $filesByPath.Add($relativePath, $record)
    }
    if ($filesByPath.Count -ne $expectedFilePaths.Count) {
        throw "Checked-in Tauri CLI cache contract must attest exactly six files."
    }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $contractDigest = $sha256.ComputeHash($contractBytes)
    } finally {
        $sha256.Dispose()
    }
    return [pscustomobject]@{
        sha256 = ([System.BitConverter]::ToString($contractDigest)).Replace("-", "").ToLowerInvariant()
        name = [string]$raw.name
        revision = [string]$raw.revision
        version = [string]$raw.version
        versionOutput = [string]$raw.versionOutput
        entrypoint = [string]$raw.entrypoint
        packagesByName = $packagesByName
        files = @($raw.files)
        filesByPath = $filesByPath
        filePaths = @($expectedFilePaths)
    }
}

function Get-PackageLockTauriCliContract {
    param([string]$Path, [string]$ExtractScript)
    $contractJson = & python $ExtractScript --package-lock $Path
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri CLI package lock is not valid JSON: $Path"
    }
    try {
        $contract = $contractJson | ConvertFrom-Json
    } catch {
        throw "Tauri CLI package lock extraction was not valid JSON: $Path"
    }
    if (
        [string]::IsNullOrWhiteSpace([string]$contract.version) -or
        [string]$contract.version -ne [string]$contract.platformVersion -or
        [string]$contract.packageIntegrity -notmatch '^sha512-[A-Za-z0-9+/=]+$' -or
        [string]$contract.platformPackageIntegrity -notmatch '^sha512-[A-Za-z0-9+/=]+$'
    ) {
        throw "Frontend package lock does not contain one exact Windows x64 Tauri CLI contract."
    }
    return [pscustomobject]@{
        version = [string]$contract.version
        packageIntegrity = [string]$contract.packageIntegrity
        platformVersion = [string]$contract.platformVersion
        platformPackageIntegrity = [string]$contract.platformPackageIntegrity
    }
}

function Assert-PackageLockMatchesTrustedContract {
    param($Locked, $Trusted)
    $cliPackage = $Trusted.packagesByName["@tauri-apps/cli"]
    $platformPackage = $Trusted.packagesByName["@tauri-apps/cli-win32-x64-msvc"]
    if (
        [string]$Locked.version -ne [string]$Trusted.version -or
        [string]$Locked.platformVersion -ne [string]$Trusted.version -or
        [string]$Locked.packageIntegrity -cne [string]$cliPackage.integrity -or
        [string]$Locked.platformPackageIntegrity -cne [string]$platformPackage.integrity
    ) {
        throw "Frontend package lock does not match the checked-in Tauri CLI cache contract."
    }
}

function Get-JsonPackageIdentity {
    param([string]$Path)
    try {
        $package = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    } catch {
        throw "Tauri CLI package manifest is not valid JSON: $Path"
    }
    return [pscustomobject]@{
        name = [string]$package.name
        version = [string]$package.version
    }
}

function Get-ActualFileRecord {
    param([string]$Root, [string]$RelativePath)
    $path = [System.IO.Path]::GetFullPath((Join-Path $Root ($RelativePath -replace '/', '\')))
    Assert-UnderRoot -Root $Root -Path $path -Label "Tauri CLI cache file"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Attested Tauri CLI cache file is missing: $RelativePath"
    }
    $item = Get-Item -LiteralPath $path -Force
    return [ordered]@{
        path = $RelativePath
        length = [int64]$item.Length
        sha256 = Get-FileSha256 -Path $path
    }
}

function Assert-FileMatchesTrustedRecord {
    param([string]$Root, [string]$RelativePath, $TrustedFilesByPath)
    if (-not $TrustedFilesByPath.ContainsKey($RelativePath)) {
        throw "Tauri CLI file is not present in the checked-in cache contract: $RelativePath"
    }
    $expected = $TrustedFilesByPath[$RelativePath]
    $actual = Get-ActualFileRecord -Root $Root -RelativePath $RelativePath
    if (
        [int64]$actual.length -ne [int64]$expected.length -or
        [string]$actual.sha256 -cne [string]$expected.sha256
    ) {
        throw "Tauri CLI file does not match the checked-in cache contract: $RelativePath"
    }
    return $actual
}

function Assert-TauriCliVersionOutput {
    param([string]$Entrypoint, [string]$ExpectedOutput)
    Assert-CleanNodeEnvironment
    $lines = @(& node $Entrypoint --version 2>&1)
    $exitCode = $LASTEXITCODE
    $actualOutput = (@($lines | ForEach-Object { [string]$_ }) -join "`n")
    if ($exitCode -ne 0 -or $actualOutput -cne $ExpectedOutput) {
        throw "Tauri CLI version output did not exactly match '$ExpectedOutput'."
    }
}

Write-GitHubOutput -Name "usable" -Value "false"

if ($CacheKey -notmatch '^[0-9a-f]{64}$') {
    throw "Tauri app binary cache key must be a lowercase SHA-256 value."
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Tauri app binary version is not valid SemVer: $Version"
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$buildRoot = Resolve-RepoPath -Root $repoRoot -Path "build"
$resolvedCacheRoot = Resolve-RepoPath -Root $repoRoot -Path $CacheRoot
$resolvedBinaryPath = Resolve-RepoPath -Root $repoRoot -Path $BinaryPath
$resolvedPackageLockPath = Resolve-RepoPath -Root $repoRoot -Path $PackageLockPath
$resolvedTauriCliPackagePath = Resolve-RepoPath -Root $repoRoot -Path $TauriCliPackagePath
$resolvedTauriCliPlatformPackagePath = Resolve-RepoPath -Root $repoRoot -Path $TauriCliPlatformPackagePath
$defaultTrustedContractPath = Resolve-RepoPath -Root $repoRoot -Path "packaging\tauri-cli-cache-contract.json"
$trustedContractPath = Resolve-RepoPath -Root $repoRoot -Path $ContractPath
$extractScript = Join-Path $PSScriptRoot "read_tauri_cli_lock.py"
$cachedBinaryPath = Join-Path $resolvedCacheRoot "scriber-desktop.exe"
$cachedTauriCliRoot = Join-Path $resolvedCacheRoot "tauri-cli\node_modules\@tauri-apps"
$cachedTauriCliEntrypoint = Join-Path $cachedTauriCliRoot "cli\tauri.js"
$manifestPath = Join-Path $resolvedCacheRoot "manifest.json"

Assert-UnderRoot -Root $buildRoot -Path $resolvedCacheRoot -Label "Tauri app binary cache root"
Assert-UnderRoot -Root $repoRoot -Path $resolvedBinaryPath -Label "Tauri app binary path"
Assert-UnderRoot -Root $repoRoot -Path $resolvedPackageLockPath -Label "Frontend package lock"
Assert-UnderRoot -Root $repoRoot -Path $resolvedTauriCliPackagePath -Label "Tauri CLI package"
Assert-UnderRoot -Root $repoRoot -Path $resolvedTauriCliPlatformPackagePath -Label "Tauri CLI platform package"
Assert-UnderRoot -Root $repoRoot -Path $trustedContractPath -Label "Tauri CLI cache contract"
if (-not $trustedContractPath.Equals($defaultTrustedContractPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    if (
        $env:SCRIBER_TAURI_CLI_CACHE_TEST_CONTRACT -ne "1" -or
        [string]::IsNullOrWhiteSpace([string]$env:PYTEST_CURRENT_TEST)
    ) {
        throw "A non-production Tauri CLI cache contract is allowed only from the focused pytest contract fixture."
    }
}
Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path $trustedContractPath -Label "Checked-in Tauri CLI cache contract"
Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path $extractScript -Label "Tauri CLI package-lock extractor"
Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path $resolvedPackageLockPath -Label "Frontend package lock"
Assert-SafePathAncestry `
    -BoundaryRoot $repoRoot `
    -Path $resolvedBinaryPath `
    -Label "Tauri app binary path" `
    -AllowLeafHardLink:($Mode -eq "Export")
if (Test-Path -LiteralPath $resolvedCacheRoot) {
    Assert-SafePathAncestry -BoundaryRoot $buildRoot -Path $resolvedCacheRoot -Label "Tauri app binary cache root"
} else {
    Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path $resolvedCacheRoot -Label "Tauri app binary cache root"
}
Assert-CleanNodeEnvironment

$trustedContract = Get-TrustedTauriCliContract -Path $trustedContractPath
$trustedContractSha256 = [string]$trustedContract.sha256
$lockedContract = Get-PackageLockTauriCliContract -Path $resolvedPackageLockPath -ExtractScript $extractScript
Assert-PackageLockMatchesTrustedContract -Locked $lockedContract -Trusted $trustedContract
Write-Host "Verified checked-in Tauri CLI cache contract SHA-256 $trustedContractSha256."
Write-GitHubOutput -Name "cli-contract-sha256" -Value $trustedContractSha256

if ($Mode -eq "Export") {
    if (-not (Test-Path -LiteralPath $resolvedBinaryPath -PathType Leaf)) {
        throw "Tauri app binary was not found for export: $resolvedBinaryPath"
    }
    Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path $resolvedTauriCliPackagePath -Label "Installed Tauri CLI package root"
    Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path $resolvedTauriCliPlatformPackagePath -Label "Installed Tauri CLI platform package root"
    $actualVersion = Get-NormalizedFileVersion -Path $resolvedBinaryPath
    if (-not (Test-ExpectedVersion -Actual $actualVersion -Expected $Version)) {
        throw "Tauri app binary version '$actualVersion' does not match expected version '$Version'."
    }
    $sourceBinarySha256 = Get-FileSha256 -Path $resolvedBinaryPath
    $cliPackageIdentity = Get-JsonPackageIdentity -Path (Join-Path $resolvedTauriCliPackagePath "package.json")
    $platformPackageIdentity = Get-JsonPackageIdentity -Path (Join-Path $resolvedTauriCliPlatformPackagePath "package.json")
    if (
        [string]$cliPackageIdentity.name -cne "@tauri-apps/cli" -or
        [string]$cliPackageIdentity.version -cne [string]$trustedContract.version -or
        [string]$platformPackageIdentity.name -cne "@tauri-apps/cli-win32-x64-msvc" -or
        [string]$platformPackageIdentity.version -cne [string]$trustedContract.version
    ) {
        throw "Installed Windows x64 Tauri CLI packages do not match the checked-in cache contract."
    }
    $tauriCliSourceFiles = [ordered]@{
        "tauri-cli/node_modules/@tauri-apps/cli/package.json" = (Join-Path $resolvedTauriCliPackagePath "package.json")
        "tauri-cli/node_modules/@tauri-apps/cli/tauri.js" = (Join-Path $resolvedTauriCliPackagePath "tauri.js")
        "tauri-cli/node_modules/@tauri-apps/cli/main.js" = (Join-Path $resolvedTauriCliPackagePath "main.js")
        "tauri-cli/node_modules/@tauri-apps/cli/index.js" = (Join-Path $resolvedTauriCliPackagePath "index.js")
        "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/package.json" = (Join-Path $resolvedTauriCliPlatformPackagePath "package.json")
        "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/cli.win32-x64-msvc.node" = (Join-Path $resolvedTauriCliPlatformPackagePath "cli.win32-x64-msvc.node")
    }
    foreach ($entry in $tauriCliSourceFiles.GetEnumerator()) {
        if (-not (Test-Path -LiteralPath $entry.Value -PathType Leaf)) {
            throw "Installed Tauri CLI file was not found for export: $($entry.Value)"
        }
        Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path $entry.Value -Label "Installed Tauri CLI file"
        $sourceRecord = [ordered]@{
            path = $entry.Key
            length = [int64](Get-Item -LiteralPath $entry.Value -Force).Length
            sha256 = Get-FileSha256 -Path $entry.Value
        }
        $trustedRecord = $trustedContract.filesByPath[$entry.Key]
        if (
            [int64]$sourceRecord.length -ne [int64]$trustedRecord.length -or
            [string]$sourceRecord.sha256 -cne [string]$trustedRecord.sha256
        ) {
            throw "Installed Tauri CLI file does not match the checked-in cache contract: $($entry.Key)"
        }
    }
    Assert-SafePathAncestry -BoundaryRoot $repoRoot -Path (Join-Path $resolvedTauriCliPackagePath "tauri.js") -Label "Installed Tauri CLI entrypoint ancestry"
    Assert-TauriCliVersionOutput -Entrypoint (Join-Path $resolvedTauriCliPackagePath "tauri.js") -ExpectedOutput $trustedContract.versionOutput

    if (Test-Path -LiteralPath $resolvedCacheRoot -PathType Container) {
        [void](Get-SafeTreeRelativeFiles -Root $resolvedCacheRoot -RelativeTo $buildRoot -Label "Existing Tauri app binary cache tree")
        Remove-Item -LiteralPath $resolvedCacheRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $resolvedCacheRoot | Out-Null
    Copy-Item -LiteralPath $resolvedBinaryPath -Destination $cachedBinaryPath -Force
    Assert-SafePathAncestry -BoundaryRoot $resolvedCacheRoot -Path $cachedBinaryPath -Label "Exported Tauri app binary"
    if ((Get-FileSha256 -Path $cachedBinaryPath) -ne $sourceBinarySha256) {
        throw "Tauri app binary checksum changed while creating the independent cache copy."
    }
    foreach ($entry in $tauriCliSourceFiles.GetEnumerator()) {
        $destination = [System.IO.Path]::GetFullPath((Join-Path $resolvedCacheRoot ($entry.Key -replace '/', '\')))
        Assert-UnderRoot -Root $resolvedCacheRoot -Path $destination -Label "Tauri CLI export destination"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $entry.Value -Destination $destination -Force
    }
    Assert-SafePathAncestry -BoundaryRoot $resolvedCacheRoot -Path $cachedTauriCliEntrypoint -Label "Cached Tauri CLI entrypoint ancestry"
    $attestedCliFiles = @(
        $trustedContract.filePaths |
            ForEach-Object { Assert-FileMatchesTrustedRecord -Root $resolvedCacheRoot -RelativePath $_ -TrustedFilesByPath $trustedContract.filesByPath }
    )
    Assert-TauriCliVersionOutput -Entrypoint $cachedTauriCliEntrypoint -ExpectedOutput $trustedContract.versionOutput
    $item = Get-Item -LiteralPath $cachedBinaryPath -Force
    $sourceCommit = ([string]$env:GITHUB_SHA).Trim().ToLowerInvariant()
    if (-not (Test-OptionalSourceCommit -Value $sourceCommit)) {
        throw "Tauri app binary source commit must be empty or a 40-character lowercase hexadecimal Git object id."
    }
    $manifest = [ordered]@{
        apiVersion = "4"
        cacheKey = $CacheKey
        appVersion = $Version
        binaryVersion = $actualVersion
        sourceCommit = $sourceCommit
        target = "x86_64-pc-windows-msvc"
        profile = "release"
        executable = [ordered]@{
            name = "scriber-desktop.exe"
            length = [int64]$item.Length
            sha256 = Get-FileSha256 -Path $cachedBinaryPath
        }
        tauriCli = [ordered]@{
            contractSha256 = $trustedContractSha256
            contractName = [string]$trustedContract.name
            contractRevision = [string]$trustedContract.revision
            version = [string]$trustedContract.version
            versionOutput = [string]$trustedContract.versionOutput
            packageName = "@tauri-apps/cli"
            packageIntegrity = [string]$trustedContract.packagesByName["@tauri-apps/cli"].integrity
            platformPackageName = "@tauri-apps/cli-win32-x64-msvc"
            platformPackageIntegrity = [string]$trustedContract.packagesByName["@tauri-apps/cli-win32-x64-msvc"].integrity
            entrypoint = [string]$trustedContract.entrypoint
            files = $attestedCliFiles
        }
        exportedAt = (Get-Date).ToUniversalTime().ToString("o")
    }
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    Assert-SafePathAncestry -BoundaryRoot $resolvedCacheRoot -Path $manifestPath -Label "Tauri app binary cache manifest"
    $expectedCacheFiles = @("manifest.json", "scriber-desktop.exe") + @($trustedContract.filePaths)
    $actualCacheFiles = Get-SafeTreeRelativeFiles -Root $resolvedCacheRoot -RelativeTo $resolvedCacheRoot -Label "Exported Tauri app binary cache tree"
    if (($actualCacheFiles -join "`n") -cne ((@($expectedCacheFiles | Sort-Object)) -join "`n")) {
        throw "Exported Tauri app binary cache contains files outside the checked-in contract."
    }
    Write-Host "Exported exact Tauri app binary and externally attested CLI cache for $Version ($($item.Length) app bytes)."
    Write-GitHubOutput -Name "usable" -Value "true"
    Write-GitHubOutput -Name "sha256" -Value ([string]$manifest.executable.sha256)
    Write-GitHubOutput -Name "cli-entrypoint" -Value $cachedTauriCliEntrypoint
    Write-GitHubOutput -Name "cli-version" -Value ([string]$trustedContract.version)
    Write-GitHubOutput -Name "cli-version-output" -Value ([string]$trustedContract.versionOutput)
    exit 0
}

if (
    -not (Test-Path -LiteralPath $cachedBinaryPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $cachedTauriCliEntrypoint -PathType Leaf) -or
    -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)
) {
    Write-Host "No complete Tauri app binary and CLI cache was restored."
    exit 0
}

Assert-SafePathAncestry -BoundaryRoot $resolvedCacheRoot -Path $cachedBinaryPath -Label "Cached Tauri app binary"
Assert-SafePathAncestry -BoundaryRoot $resolvedCacheRoot -Path $manifestPath -Label "Tauri app binary cache manifest"
Assert-SafePathAncestry -BoundaryRoot $resolvedCacheRoot -Path $cachedTauriCliEntrypoint -Label "Cached Tauri CLI entrypoint ancestry"
$actualCacheFiles = Get-SafeTreeRelativeFiles -Root $resolvedCacheRoot -RelativeTo $resolvedCacheRoot -Label "Restored Tauri app binary cache tree"

try {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $item = Get-Item -LiteralPath $cachedBinaryPath -Force
    $sha256 = Get-FileSha256 -Path $cachedBinaryPath
    $actualVersion = Get-NormalizedFileVersion -Path $cachedBinaryPath
    $expectedCommit = ([string]$env:GITHUB_SHA).Trim().ToLowerInvariant()
    $manifestCommit = ([string]$manifest.sourceCommit).Trim().ToLowerInvariant()
    $commitProvenanceValid = (
        (Test-OptionalSourceCommit -Value $expectedCommit) -and
        (Test-OptionalSourceCommit -Value $manifestCommit) -and
        ([string]::IsNullOrWhiteSpace($expectedCommit) -or -not [string]::IsNullOrWhiteSpace($manifestCommit))
    )
    $expectedCacheFiles = @("manifest.json", "scriber-desktop.exe") + @($trustedContract.filePaths)
    $cliFilesValid = ($actualCacheFiles -join "`n") -ceq ((@($expectedCacheFiles | Sort-Object)) -join "`n")
    $manifestCliFiles = @($manifest.tauriCli.files)
    $manifestCliFileByPath = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::Ordinal)
    if ($manifestCliFiles.Count -ne $trustedContract.filePaths.Count) {
        $cliFilesValid = $false
    }
    foreach ($record in $manifestCliFiles) {
        $relativePath = [string]$record.path
        if (-not $relativePath -or $manifestCliFileByPath.ContainsKey($relativePath)) {
            $cliFilesValid = $false
            continue
        }
        $manifestCliFileByPath.Add($relativePath, $record)
    }
    foreach ($relativePath in $trustedContract.filePaths) {
        if (-not $manifestCliFileByPath.ContainsKey($relativePath)) {
            $cliFilesValid = $false
            continue
        }
        $manifestRecord = $manifestCliFileByPath[$relativePath]
        $trustedRecord = $trustedContract.filesByPath[$relativePath]
        $actualRecord = Get-ActualFileRecord -Root $resolvedCacheRoot -RelativePath $relativePath
        if (
            [int64]$manifestRecord.length -ne [int64]$trustedRecord.length -or
            [string]$manifestRecord.sha256 -cne [string]$trustedRecord.sha256 -or
            [int64]$actualRecord.length -ne [int64]$trustedRecord.length -or
            [string]$actualRecord.sha256 -cne [string]$trustedRecord.sha256
        ) {
            $cliFilesValid = $false
        }
    }
    $cachedCliPackageIdentity = Get-JsonPackageIdentity -Path (Join-Path $cachedTauriCliRoot "cli\package.json")
    $cachedPlatformPackageIdentity = Get-JsonPackageIdentity -Path (Join-Path $cachedTauriCliRoot "cli-win32-x64-msvc\package.json")
    $valid = (
        [string]$manifest.apiVersion -eq "4" -and
        [string]$manifest.cacheKey -eq $CacheKey -and
        [string]$manifest.appVersion -eq $Version -and
        [string]$manifest.binaryVersion -eq $actualVersion -and
        [string]$manifest.target -eq "x86_64-pc-windows-msvc" -and
        [string]$manifest.profile -eq "release" -and
        $commitProvenanceValid -and
        $cliFilesValid -and
        [int64]$manifest.executable.length -eq [int64]$item.Length -and
        [string]$manifest.executable.sha256 -ceq $sha256 -and
        [string]$manifest.tauriCli.contractSha256 -ceq $trustedContractSha256 -and
        [string]$manifest.tauriCli.contractName -ceq [string]$trustedContract.name -and
        [string]$manifest.tauriCli.contractRevision -ceq [string]$trustedContract.revision -and
        [string]$manifest.tauriCli.version -ceq [string]$trustedContract.version -and
        [string]$manifest.tauriCli.versionOutput -ceq [string]$trustedContract.versionOutput -and
        [string]$manifest.tauriCli.packageName -ceq "@tauri-apps/cli" -and
        [string]$manifest.tauriCli.packageIntegrity -ceq [string]$trustedContract.packagesByName["@tauri-apps/cli"].integrity -and
        [string]$manifest.tauriCli.platformPackageName -ceq "@tauri-apps/cli-win32-x64-msvc" -and
        [string]$manifest.tauriCli.platformPackageIntegrity -ceq [string]$trustedContract.packagesByName["@tauri-apps/cli-win32-x64-msvc"].integrity -and
        [string]$manifest.tauriCli.entrypoint -ceq [string]$trustedContract.entrypoint -and
        [string]$cachedCliPackageIdentity.name -ceq "@tauri-apps/cli" -and
        [string]$cachedCliPackageIdentity.version -ceq [string]$trustedContract.version -and
        [string]$cachedPlatformPackageIdentity.name -ceq "@tauri-apps/cli-win32-x64-msvc" -and
        [string]$cachedPlatformPackageIdentity.version -ceq [string]$trustedContract.version -and
        (Test-ExpectedVersion -Actual $actualVersion -Expected $Version)
    )
    if ($valid) {
        Assert-TauriCliVersionOutput -Entrypoint $cachedTauriCliEntrypoint -ExpectedOutput $trustedContract.versionOutput
    }
    if (-not $valid) {
        Write-Warning "Ignoring Tauri app binary cache because its external attestation did not validate."
        exit 0
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $resolvedBinaryPath) | Out-Null
    Copy-Item -LiteralPath $cachedBinaryPath -Destination $resolvedBinaryPath -Force
    $copiedSha256 = Get-FileSha256 -Path $resolvedBinaryPath
    if ($copiedSha256 -ne $sha256) {
        throw "Imported Tauri app binary checksum changed during copy."
    }
    Write-Host "Imported exact Tauri app binary and externally attested CLI cache for $Version ($($item.Length) app bytes)."
    Write-GitHubOutput -Name "usable" -Value "true"
    Write-GitHubOutput -Name "sha256" -Value $sha256
    Write-GitHubOutput -Name "cli-entrypoint" -Value $cachedTauriCliEntrypoint
    Write-GitHubOutput -Name "cli-version" -Value ([string]$trustedContract.version)
    Write-GitHubOutput -Name "cli-version-output" -Value ([string]$trustedContract.versionOutput)
} catch {
    Write-Warning "Ignoring unusable Tauri app binary cache: $($_.Exception.Message)"
}
