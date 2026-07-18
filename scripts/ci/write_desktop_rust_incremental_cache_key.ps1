<#
.SYNOPSIS
Writes the bounded ref-local Desktop Rust incremental-cache identity.

.DESCRIPTION
The exact identity binds the already-finalized Rust dependency and Tauri app
manifests plus this cache envelope's checked-in contract and helpers. The
dependency scope remains a separate key segment so actions/cache restore-keys
may offer only the newest state from the same feature ref, toolchain, target,
and Cargo dependency graph. Cargo/rustc remain responsible for deciding which
incremental query results are reusable after a source or frontend change.
#>

param(
    [string]$CacheKeyDir = "build\cache-keys",
    [Parameter(Mandatory = $true)]
    [string]$GitRef,
    [string]$ContractPath = "packaging\desktop-rust-incremental-cache-contract.json",
    [string]$SyncScriptPath = "scripts\ci\sync_desktop_rust_incremental_cache.ps1"
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

function Get-BytesSha256 {
    param([byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-StringSha256 {
    param([string]$Value)
    return Get-BytesSha256 -Bytes ([System.Text.Encoding]::UTF8.GetBytes($Value))
}

function Get-RawFileSha256 {
    param([string]$Path)
    return Get-BytesSha256 -Bytes ([System.IO.File]::ReadAllBytes($Path))
}

function Get-NormalizedTextFileSha256 {
    param([string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    try {
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $text = $strictUtf8.GetString($bytes)
    } catch [System.Text.DecoderFallbackException] {
        throw "Desktop incremental cache identity input is not UTF-8 text: $Path"
    }
    $normalized = $text -replace "\r\n", "`n" -replace "\r", "`n"
    return Get-StringSha256 -Value $normalized
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
        '^thin-lto-past-keys\.bin$',
        '^metadata\.rmeta$',
        '^[0-9a-z]+\.o$',
        '^[0-9a-z]+\.bc\.z$',
        '^[0-9a-z]+\.pre-lto\.bc$',
        '^s-[0-9a-z]+-[0-9a-z]+\.lock$'
    )
    $actualFileNamePatterns = @($contract.incrementalFileNamePatterns | ForEach-Object { [string]$_ })
    if (
        [int]$contract.schemaVersion -ne 1 -or
        [string]$contract.name -cne "scriber-desktop-rust-incremental-cache" -or
        [int]$contract.revision -ne 3 -or
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

$normalizedRef = $GitRef.Trim()
if (
    $normalizedRef -notmatch '^refs/heads/[0-9A-Za-z](?:[0-9A-Za-z._/-]*[0-9A-Za-z])?$' -or
    $normalizedRef -eq 'refs/heads/main' -or
    $normalizedRef.Contains('..') -or
    $normalizedRef.Contains('//') -or
    $normalizedRef.Contains('@{') -or
    $normalizedRef.Contains('\')
) {
    throw "Desktop Rust incremental caching is restricted to a normalized non-main branch ref."
}

$resolvedCacheKeyDir = Resolve-RepoPath -Path $CacheKeyDir -Label "Cache-key directory"
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "build")).TrimEnd("\", "/")
$cacheKeyPrefix = $buildRoot + [System.IO.Path]::DirectorySeparatorChar
if (-not $resolvedCacheKeyDir.StartsWith($cacheKeyPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Desktop Rust incremental cache-key output must stay under the repository build directory."
}

$resolvedContractPath = Resolve-RepoPath -Path $ContractPath -Label "Desktop incremental cache contract"
$resolvedSyncScriptPath = Resolve-RepoPath -Path $SyncScriptPath -Label "Desktop incremental cache sync helper"
$resolvedKeyScriptPath = [System.IO.Path]::GetFullPath($MyInvocation.MyCommand.Path)
foreach ($path in @($resolvedContractPath, $resolvedSyncScriptPath, $resolvedKeyScriptPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Desktop Rust incremental cache identity input is missing: $path"
    }
}
$contract = Get-TrustedContract -Path $resolvedContractPath

$rustDependenciesPath = Join-Path $resolvedCacheKeyDir "rust-dependencies.txt"
$tauriAppBinaryPath = Join-Path $resolvedCacheKeyDir "tauri-app-binary.txt"
foreach ($path in @($rustDependenciesPath, $tauriAppBinaryPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Finalized release cache-key manifest was not found: $path"
    }
}

$dependencyScopeHash = Get-RawFileSha256 -Path $rustDependenciesPath
$tauriInputHash = Get-RawFileSha256 -Path $tauriAppBinaryPath
$rows = @(
    "constant`tcache-generation`t$([string]$contract.generation)",
    "constant`tcargo-incremental`ttrue",
    "constant`tprofile`t$([string]$contract.profile)",
    "constant`ttarget`t$([string]$contract.target)",
    "contract`tpackaging/desktop-rust-incremental-cache-contract.json`t$(Get-NormalizedTextFileSha256 -Path $resolvedContractPath)",
    "helper`tscripts/ci/sync_desktop_rust_incremental_cache.ps1`t$(Get-NormalizedTextFileSha256 -Path $resolvedSyncScriptPath)",
    "helper`tscripts/ci/write_desktop_rust_incremental_cache_key.ps1`t$(Get-NormalizedTextFileSha256 -Path $resolvedKeyScriptPath)",
    "input`trust-dependencies.txt`t$dependencyScopeHash",
    "input`ttauri-app-binary.txt`t$tauriInputHash"
) | Sort-Object

New-Item -ItemType Directory -Force -Path $resolvedCacheKeyDir | Out-Null
$outputPath = Join-Path $resolvedCacheKeyDir "desktop-rust-incremental.txt"
$encoding = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($outputPath, (($rows -join "`n") + "`n"), $encoding)
$exactInputHash = Get-RawFileSha256 -Path $outputPath
$refScopeHash = Get-StringSha256 -Value $normalizedRef

Write-Host "Desktop Rust incremental cache identity: ref=$($refScopeHash.Substring(0, 12)); dependencies=$($dependencyScopeHash.Substring(0, 12)); exact=$($exactInputHash.Substring(0, 12))."
Write-GitHubOutput -Name "ref-scope-hash" -Value $refScopeHash
Write-GitHubOutput -Name "dependency-scope-hash" -Value $dependencyScopeHash
Write-GitHubOutput -Name "tauri-input-hash" -Value $tauriInputHash
Write-GitHubOutput -Name "exact-input-hash" -Value $exactInputHash
Write-GitHubOutput -Name "cache-key-file" -Value $outputPath

[ordered]@{
    ok = $true
    generation = [string]$contract.generation
    gitRef = $normalizedRef
    refScopeHash = $refScopeHash
    dependencyScopeHash = $dependencyScopeHash
    tauriInputHash = $tauriInputHash
    exactInputHash = $exactInputHash
    cacheKeyFile = $outputPath
} | ConvertTo-Json -Compress
