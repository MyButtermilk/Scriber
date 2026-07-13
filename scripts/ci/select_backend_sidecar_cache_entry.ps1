param(
    [string]$CacheRoot = "build\tauri-sidecar-cache",
    [string]$MetadataPath = "Frontend\src-tauri\target\release\backend\sidecar-build-metadata.json"
)

$ErrorActionPreference = "Stop"

function Write-GitHubOutput {
    param([string]$Name, [string]$Value)
    if ($env:GITHUB_OUTPUT) {
        "$Name=$Value" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

function Get-DirectorySizeBytes {
    param([string]$Path)
    return [int64](@(
        Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction Stop |
            Measure-Object -Property Length -Sum
    )[0].Sum)
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedCacheRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $CacheRoot))
$resolvedMetadataPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $MetadataPath))
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "build"))
$buildRootPrefix = $buildRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar

if (-not $resolvedCacheRoot.StartsWith($buildRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Backend sidecar cache root must stay under the repository build directory: $resolvedCacheRoot"
}
if (-not (Test-Path -LiteralPath $resolvedMetadataPath -PathType Leaf)) {
    throw "Backend sidecar metadata was not found: $resolvedMetadataPath"
}

$metadata = Get-Content -LiteralPath $resolvedMetadataPath -Raw | ConvertFrom-Json
$cacheKey = [string]$metadata.cache.key
if ($cacheKey -notmatch '^[0-9a-f]{64}$') {
    throw "Backend sidecar metadata contains an invalid cache key."
}

$selectedPath = [System.IO.Path]::GetFullPath((Join-Path $resolvedCacheRoot $cacheKey))
$cacheRootPrefix = $resolvedCacheRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
if (-not $selectedPath.StartsWith($cacheRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Selected backend sidecar cache entry escaped the cache root."
}

$selectedExe = Join-Path $selectedPath "scriber-backend\scriber-backend.exe"
$selectedManifestPath = Join-Path $selectedPath "cache-manifest.json"
if (-not (Test-Path -LiteralPath $selectedExe -PathType Leaf)) {
    throw "Selected backend sidecar cache executable was not found: $selectedExe"
}
if (-not (Test-Path -LiteralPath $selectedManifestPath -PathType Leaf)) {
    throw "Selected backend sidecar cache manifest was not found: $selectedManifestPath"
}
$selectedManifest = Get-Content -LiteralPath $selectedManifestPath -Raw | ConvertFrom-Json
if ([string]$selectedManifest.cacheKey -ne $cacheKey) {
    throw "Selected backend sidecar cache manifest key does not match build metadata."
}
$selectedExeItem = Get-Item -LiteralPath $selectedExe
$selectedExeSha256 = (Get-FileHash -LiteralPath $selectedExe -Algorithm SHA256).Hash.ToLowerInvariant()
if (
    [string]$selectedManifest.sidecarSha256 -ne $selectedExeSha256 -or
    [int64]$selectedManifest.sidecarLength -ne [int64]$selectedExeItem.Length
) {
    throw "Selected backend sidecar cache executable does not match its manifest attestation."
}

$entriesBefore = @(
    Get-ChildItem -LiteralPath $resolvedCacheRoot -Directory -Force -ErrorAction SilentlyContinue
)
$bytesBefore = if (Test-Path -LiteralPath $resolvedCacheRoot -PathType Container) {
    Get-DirectorySizeBytes -Path $resolvedCacheRoot
} else {
    0
}

$removed = [System.Collections.Generic.List[string]]::new()
foreach ($entry in $entriesBefore) {
    if ($entry.Name -eq $cacheKey) {
        continue
    }
    $resolvedEntry = [System.IO.Path]::GetFullPath($entry.FullName)
    if (-not $resolvedEntry.StartsWith($cacheRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove backend cache entry outside the cache root: $resolvedEntry"
    }
    Remove-Item -LiteralPath $resolvedEntry -Recurse -Force
    $removed.Add($entry.Name) | Out-Null
}

$entriesAfter = @(
    Get-ChildItem -LiteralPath $resolvedCacheRoot -Directory -Force -ErrorAction SilentlyContinue
)
if ($entriesAfter.Count -ne 1 -or $entriesAfter[0].Name -ne $cacheKey) {
    throw "Backend sidecar cache pruning did not leave exactly the selected entry."
}
$bytesAfter = Get-DirectorySizeBytes -Path $resolvedCacheRoot

$report = [ordered]@{
    apiVersion = "1"
    ok = $true
    cacheKey = $cacheKey
    entriesBefore = $entriesBefore.Count
    entriesAfter = $entriesAfter.Count
    removedEntries = $removed.Count
    bytesBefore = $bytesBefore
    bytesAfter = $bytesAfter
    bytesRemoved = [Math]::Max([int64]0, [int64]($bytesBefore - $bytesAfter))
}
$reportPath = Join-Path $repoRoot "build\backend-sidecar-cache-selection.json"
$report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $reportPath -Encoding utf8

Write-Host (
    "Selected backend sidecar cache entry {0}; entries {1}->{2}, bytes {3}->{4}." -f `
        $cacheKey.Substring(0, 12), $entriesBefore.Count, $entriesAfter.Count, $bytesBefore, $bytesAfter
)
Write-GitHubOutput -Name "selected" -Value "true"
Write-GitHubOutput -Name "cache-key" -Value $cacheKey
Write-GitHubOutput -Name "bytes-before" -Value ([string]$bytesBefore)
Write-GitHubOutput -Name "bytes-after" -Value ([string]$bytesAfter)
Write-GitHubOutput -Name "bytes-removed" -Value ([string]$report.bytesRemoved)
