<#
.SYNOPSIS
Exports or imports the frozen backend products used by the adaptive cold path.

.DESCRIPTION
The cold backend runner prepares immutable, checksum-attested cache products.
This helper stages only those bounded products for the packaging runner and
validates the source commit, Python runtime, cache manifests, and critical
executables before importing anything. Import failures are reported as
usable=false so the workflow can retain the established single-runner path.
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Export", "Import")]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$PythonVersion,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$BackendWorkflowFingerprint,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$RuntimeWorkflowFingerprint,
    [switch]$PublishFfmpeg,
    [switch]$PublishRustAudio,
    [switch]$PublishRustDiarization,
    [switch]$PublishBackendRuntime,
    [switch]$PublishBackend,
    [string]$SourceCommit = "",
    [string]$ProductRoot = "build\cold-backend-product",
    [string]$BackendCacheRoot = "build\tauri-sidecar-cache",
    [string]$RuntimeCacheRoot = "build\tauri-sidecar-runtime-cache",
    [string]$RustAudioCacheRoot = "build\rust-audio-sidecar-cache",
    [string]$RustDiarizationCacheRoot = "build\rust-diarization-sidecar-cache",
    [string]$FfmpegCacheRoot = "build\ffmpeg-profile-b-msys2"
)

$ErrorActionPreference = "Stop"
if ($SourceCommit -and $SourceCommit -notmatch '^[0-9a-f]{40}$') {
    throw "Cold backend product source commit must be a lowercase 40-character SHA."
}

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

function Assert-UnderRoot {
    param([string]$Root, [string]$Path, [string]$Label)
    $prefix = $Root.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (-not $Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under ${Root}: $Path"
    }
}

function Copy-Tree {
    param([string]$Source, [string]$Destination)
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Cold backend product source was not found: $Source"
    }
    if (Test-Path -LiteralPath $Destination -PathType Container) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
    }
}

function Copy-FfmpegCacheAllowlist {
    param([string]$Source, [string]$Destination)
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "FFmpeg Profile B cache source was not found: $Source"
    }
    if (Test-Path -LiteralPath $Destination -PathType Container) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $allowlist = @(
        "dist\scriber-ffmpeg-profile-b",
        "ffmpeg-profile-b-manifest.json",
        "ffmpeg-profile-b-fixtures.json",
        "media-preparation-smoke-profile-b.json",
        "profile-b-msys2-build-report.json"
    )
    foreach ($relativePath in $allowlist) {
        $sourcePath = Join-Path $Source $relativePath
        $destinationPath = Join-Path $Destination $relativePath
        if (-not (Test-Path -LiteralPath $sourcePath)) {
            throw "Required FFmpeg Profile B cache output was not found: $sourcePath"
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
        if (Test-Path -LiteralPath $sourcePath -PathType Container) {
            Copy-Tree -Source $sourcePath -Destination $destinationPath
        } else {
            Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Force
        }
    }
}

function Get-FileAttestation {
    param([string]$Root, [string]$Path)
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    $prefix = $Root.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (-not $item.FullName.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Cold backend product file escaped its staging root: $($item.FullName)"
    }
    $relative = $item.FullName.Substring($prefix.Length).Replace("\", "/")
    return [ordered]@{
        path = $relative
        length = [int64]$item.Length
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

function Test-FileAttestation {
    param([string]$Root, [object]$Entry)
    $relative = [string]$Entry.path
    if (
        -not $relative -or
        [System.IO.Path]::IsPathRooted($relative) -or
        $relative.Contains("\") -or
        $relative.StartsWith("/") -or
        $relative -match '(^|/)\.\.($|/)'
    ) {
        return $false
    }
    $path = [System.IO.Path]::GetFullPath((Join-Path $Root $relative))
    Assert-UnderRoot -Root $Root -Path $path -Label "Cold backend attestation path"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return $false
    }
    $item = Get-Item -LiteralPath $path
    return (
        [int64]$Entry.length -eq [int64]$item.Length -and
        [string]$Entry.sha256 -eq (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    )
}

function Test-ManifestFileSet {
    param(
        [string]$Root,
        [object[]]$Entries,
        [string[]]$ExcludedRelativePaths = @()
    )

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return $false
    }
    $excluded = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($relative in $ExcludedRelativePaths) {
        [void]$excluded.Add($relative.Replace("\", "/"))
    }
    $actualByPath = @{}
    $prefix = $Root.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force) {
        $relative = $file.FullName.Substring($prefix.Length).Replace("\", "/")
        if ($excluded.Contains($relative)) {
            continue
        }
        if ($actualByPath.ContainsKey($relative)) {
            return $false
        }
        $actualByPath[$relative] = $file
    }
    if ($actualByPath.Count -ne $Entries.Count) {
        return $false
    }
    $seen = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($entry in $Entries) {
        $relative = [string]$entry.path
        if (
            -not $relative -or
            -not $seen.Add($relative) -or
            $relative.Contains("\") -or
            $relative.StartsWith("/") -or
            $relative -match '(^|/)\.\.($|/)'
        ) {
            return $false
        }
        $file = $actualByPath[$relative]
        if (
            $null -eq $file -or
            [int64]$entry.length -ne [int64]$file.Length -or
            [string]$entry.sha256 -ne (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        ) {
            return $false
        }
    }
    return $true
}

Write-GitHubOutput -Name "usable" -Value "false"

if ($PythonVersion -notmatch '^3\.13\.\d+$') {
    throw "Cold backend product requires an exact Python 3.13 patch version."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$buildRoot = Resolve-RepoPath -Root $repoRoot -Path "build"
$resolvedProductRoot = Resolve-RepoPath -Root $repoRoot -Path $ProductRoot
$resolvedBackendCacheRoot = Resolve-RepoPath -Root $repoRoot -Path $BackendCacheRoot
$resolvedRuntimeCacheRoot = Resolve-RepoPath -Root $repoRoot -Path $RuntimeCacheRoot
$resolvedAudioCacheRoot = Resolve-RepoPath -Root $repoRoot -Path $RustAudioCacheRoot
$resolvedDiarizationCacheRoot = Resolve-RepoPath -Root $repoRoot -Path $RustDiarizationCacheRoot
$resolvedFfmpegCacheRoot = Resolve-RepoPath -Root $repoRoot -Path $FfmpegCacheRoot
Assert-UnderRoot -Root $buildRoot -Path $resolvedProductRoot -Label "Cold backend product root"
Assert-UnderRoot -Root $buildRoot -Path $resolvedBackendCacheRoot -Label "Backend cache root"
Assert-UnderRoot -Root $buildRoot -Path $resolvedRuntimeCacheRoot -Label "Backend runtime cache root"
Assert-UnderRoot -Root $buildRoot -Path $resolvedAudioCacheRoot -Label "Rust audio cache root"
Assert-UnderRoot -Root $buildRoot -Path $resolvedDiarizationCacheRoot -Label "Rust diarization cache root"
Assert-UnderRoot -Root $buildRoot -Path $resolvedFfmpegCacheRoot -Label "FFmpeg cache root"

$manifestPath = Join-Path $resolvedProductRoot "product-manifest.json"

if ($Mode -eq "Export") {
    $backendEntries = @(Get-ChildItem -LiteralPath $resolvedBackendCacheRoot -Directory -Force -ErrorAction Stop)
    if ($backendEntries.Count -ne 1 -or $backendEntries[0].Name -notmatch '^[0-9a-f]{24}$') {
        throw "Cold backend export requires exactly one selected backend cache entry."
    }

    $backendCacheManifestPath = Join-Path $backendEntries[0].FullName "cache-manifest.json"
    $backendCacheManifest = Get-Content -LiteralPath $backendCacheManifestPath -Raw | ConvertFrom-Json
    $backendCacheKey = [string]$backendCacheManifest.cacheKey
    if (
        $backendCacheKey -notmatch '^[0-9a-f]{64}$' -or
        $backendEntries[0].Name -ne $backendCacheKey.Substring(0, 24)
    ) {
        throw "Cold backend cache entry does not match its complete SHA-256 identity."
    }

    if (Test-Path -LiteralPath $resolvedProductRoot -PathType Container) {
        Remove-Item -LiteralPath $resolvedProductRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $resolvedProductRoot | Out-Null
    $productBackendRoot = Join-Path $resolvedProductRoot "tauri-sidecar-cache"
    $productRuntimeRoot = Join-Path $resolvedProductRoot "tauri-sidecar-runtime-cache"
    $productAudioRoot = Join-Path $resolvedProductRoot "rust-audio-sidecar-cache"
    $productDiarizationRoot = Join-Path $resolvedProductRoot "rust-diarization-sidecar-cache"
    $productFfmpegRoot = Join-Path $resolvedProductRoot "ffmpeg-profile-b-msys2"
    Copy-Tree -Source $resolvedBackendCacheRoot -Destination $productBackendRoot
    Copy-Tree -Source $resolvedRuntimeCacheRoot -Destination $productRuntimeRoot
    Copy-Tree -Source $resolvedAudioCacheRoot -Destination $productAudioRoot
    Copy-Tree -Source $resolvedDiarizationCacheRoot -Destination $productDiarizationRoot
    Copy-FfmpegCacheAllowlist -Source $resolvedFfmpegCacheRoot -Destination $productFfmpegRoot

    $runtimeManifestPath = Join-Path $productRuntimeRoot "runtime-cache-manifest.json"
    if (-not (Test-Path -LiteralPath $runtimeManifestPath -PathType Leaf)) {
        throw "Cold backend runtime cache manifest was not found."
    }
    $runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw | ConvertFrom-Json
    $runtimeCacheKey = [string]$runtimeManifest.cacheKey
    if ($runtimeCacheKey -notmatch '^[0-9a-f]{64}$') {
        throw "Cold backend runtime cache manifest contains an invalid key."
    }
    $attestations = @(
        Get-ChildItem -LiteralPath $resolvedProductRoot -Recurse -File -Force |
            Where-Object { $_.FullName -ne $manifestPath } |
            ForEach-Object { Get-FileAttestation -Root $resolvedProductRoot -Path $_.FullName }
    )
    $attestations = @($attestations | Sort-Object path)
    $manifest = [ordered]@{
        apiVersion = "1"
        sourceCommit = if ($SourceCommit) { $SourceCommit.Trim() } else { ([string]$env:GITHUB_SHA).Trim() }
        pythonVersion = $PythonVersion
        backendCacheKey = $backendCacheKey
        runtimeCacheKey = $runtimeCacheKey
        backendWorkflowFingerprint = $BackendWorkflowFingerprint
        runtimeWorkflowFingerprint = $RuntimeWorkflowFingerprint
        publish = [ordered]@{
            ffmpeg = [bool]$PublishFfmpeg
            rustAudio = [bool]$PublishRustAudio
            rustDiarization = [bool]$PublishRustDiarization
            backendRuntime = [bool]$PublishBackendRuntime
            backend = [bool]$PublishBackend
        }
        files = $attestations
    }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    Write-Host "Exported cold backend product for cache $($backendCacheKey.Substring(0, 12))."
    Write-GitHubOutput -Name "usable" -Value "true"
    Write-GitHubOutput -Name "backend-cache-key" -Value $backendCacheKey
    Write-GitHubOutput -Name "runtime-cache-key" -Value $runtimeCacheKey
    exit 0
}

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    Write-Warning "Cold backend product manifest was not downloaded; using the normal single-runner path."
    exit 0
}

try {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $expectedCommit = if ($SourceCommit) { $SourceCommit.Trim() } else { ([string]$env:GITHUB_SHA).Trim() }
    $backendCacheKey = [string]$manifest.backendCacheKey
    $backendCacheEntryName = if ($backendCacheKey -match '^[0-9a-f]{64}$') {
        $backendCacheKey.Substring(0, 24)
    } else {
        ""
    }
    $runtimeCacheKey = [string]$manifest.runtimeCacheKey
    if (
        [string]$manifest.apiVersion -ne "1" -or
        [string]$manifest.pythonVersion -ne $PythonVersion -or
        $backendCacheKey -notmatch '^[0-9a-f]{64}$' -or
        $runtimeCacheKey -notmatch '^[0-9a-f]{64}$' -or
        [string]$manifest.backendWorkflowFingerprint -ne $BackendWorkflowFingerprint -or
        [string]$manifest.runtimeWorkflowFingerprint -ne $RuntimeWorkflowFingerprint -or
        ($expectedCommit -and [string]$manifest.sourceCommit -ne $expectedCommit)
    ) {
        throw "Cold backend product identity did not match this packaging run."
    }
    $attestedFiles = @($manifest.files)
    if ($attestedFiles.Count -lt 8) {
        throw "Cold backend product did not attest every required output."
    }
    $actualFiles = @(
        Get-ChildItem -LiteralPath $resolvedProductRoot -Recurse -File -Force |
            Where-Object { $_.FullName -ne $manifestPath }
    )
    if ($actualFiles.Count -ne $attestedFiles.Count) {
        throw "Cold backend product file inventory did not match its manifest."
    }
    $attestationPaths = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($entry in $attestedFiles) {
        if (
            -not $attestationPaths.Add([string]$entry.path) -or
            -not (Test-FileAttestation -Root $resolvedProductRoot -Entry $entry)
        ) {
            throw "Cold backend product file attestation failed."
        }
    }

    $productBackendRoot = Join-Path $resolvedProductRoot "tauri-sidecar-cache"
    $backendManifestPath = Join-Path $productBackendRoot "$backendCacheEntryName\cache-manifest.json"
    $backendExePath = Join-Path $productBackendRoot "$backendCacheEntryName\scriber-backend\scriber-backend.exe"
    $backendManifest = Get-Content -LiteralPath $backendManifestPath -Raw | ConvertFrom-Json
    $backendExe = Get-Item -LiteralPath $backendExePath
    if (
        [string]$backendManifest.cacheKey -ne $backendCacheKey -or
        [int64]$backendManifest.sidecarLength -ne [int64]$backendExe.Length -or
        [string]$backendManifest.sidecarSha256 -ne (Get-FileHash -LiteralPath $backendExePath -Algorithm SHA256).Hash.ToLowerInvariant()
    ) {
        throw "Cold backend cache manifest did not validate."
    }
    $runtimeManifestPath = Join-Path $resolvedProductRoot "tauri-sidecar-runtime-cache\runtime-cache-manifest.json"
    $runtimeWorkflowEnvelopePath = Join-Path $resolvedProductRoot "tauri-sidecar-runtime-cache\workflow-cache-envelope.json"
    $runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw | ConvertFrom-Json
    $runtimeWorkflowEnvelope = Get-Content -LiteralPath $runtimeWorkflowEnvelopePath -Raw | ConvertFrom-Json
    $productRuntimeBackendRoot = Join-Path $resolvedProductRoot "tauri-sidecar-runtime-cache\scriber-backend"
    $runtimeLayerManifestPath = Join-Path $productRuntimeBackendRoot "runtime-layer-manifest.json"
    $runtimeLayerManifest = Get-Content -LiteralPath $runtimeLayerManifestPath -Raw | ConvertFrom-Json
    $runtimeExePath = Join-Path $productRuntimeBackendRoot "scriber-backend.exe"
    if (-not (Test-Path -LiteralPath $runtimeExePath -PathType Leaf)) {
        $runtimeExePath = Join-Path $productRuntimeBackendRoot "scriber-backend"
    }
    $runtimeExe = Get-Item -LiteralPath $runtimeExePath
    $stableMediaRoot = Join-Path (Join-Path $resolvedProductRoot "tauri-sidecar-runtime-cache") "media-tools"
    $expectedStableMedia = @($runtimeManifest.stableMediaFiles)
    $actualStableMedia = @(
        Get-ChildItem -LiteralPath $stableMediaRoot -Recurse -File -Force -ErrorAction SilentlyContinue
    )
    $stableMediaValid = $actualStableMedia.Count -eq $expectedStableMedia.Count
    $stableMediaSeen = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($stableEntry in $expectedStableMedia) {
        $stableRelative = [string]$stableEntry.path
        if (
            -not $stableRelative -or
            -not $stableMediaSeen.Add($stableRelative) -or
            -not $stableRelative.StartsWith("media-tools/", [System.StringComparison]::Ordinal) -or
            $stableRelative.Contains("\") -or
            $stableRelative.StartsWith("/") -or
            $stableRelative -match '(^|/)\.\.($|/)'
        ) {
            $stableMediaValid = $false
            continue
        }
        $stablePath = Join-Path (Join-Path $resolvedProductRoot "tauri-sidecar-runtime-cache") $stableRelative
        if (
            -not (Test-Path -LiteralPath $stablePath -PathType Leaf) -or
            [int64]$stableEntry.length -ne [int64](Get-Item -LiteralPath $stablePath).Length -or
            [string]$stableEntry.sha256 -ne (Get-FileHash -LiteralPath $stablePath -Algorithm SHA256).Hash.ToLowerInvariant()
        ) {
            $stableMediaValid = $false
        }
    }
    $stableMediaValid = $stableMediaValid -and [bool]($expectedStableMedia | Where-Object { [string]$_.path -eq "media-tools/deno.exe" })
    if (
        [int]$runtimeManifest.apiVersion -ne 1 -or
        [string]$runtimeManifest.cacheKey -ne $runtimeCacheKey -or
        [int]$runtimeWorkflowEnvelope.apiVersion -ne 1 -or
        [string]$runtimeWorkflowEnvelope.workflowFingerprint -ne $RuntimeWorkflowFingerprint -or
        [string]$runtimeWorkflowEnvelope.innerCacheKey -ne $runtimeCacheKey -or
        [string]$runtimeWorkflowEnvelope.runtimeManifestSha256 -ne (Get-FileHash -LiteralPath $runtimeManifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -or
        [int64]$runtimeWorkflowEnvelope.runtimeManifestLength -ne [int64](Get-Item -LiteralPath $runtimeManifestPath).Length -or
        [int]$runtimeLayerManifest.schemaVersion -ne 1 -or
        [string]$runtimeLayerManifest.name -ne "scriber-backend-runtime-layer" -or
        [string]$runtimeLayerManifest.cacheKey -ne $runtimeCacheKey -or
        [string]$runtimeManifest.sidecarSha256 -ne (Get-FileHash -LiteralPath $runtimeExePath -Algorithm SHA256).Hash.ToLowerInvariant() -or
        [int64]$runtimeManifest.sidecarLength -ne [int64]$runtimeExe.Length -or
        [string]$runtimeLayerManifest.executable.sha256 -ne [string]$runtimeManifest.sidecarSha256 -or
        [int64]$runtimeLayerManifest.executable.length -ne [int64]$runtimeManifest.sidecarLength -or
        -not $stableMediaValid -or
        -not (Test-ManifestFileSet `
            -Root $productRuntimeBackendRoot `
            -Entries @($runtimeManifest.runtimeFiles) `
            -ExcludedRelativePaths @("runtime-layer-manifest.json")) -or
        -not (Test-ManifestFileSet `
            -Root $productRuntimeBackendRoot `
            -Entries @($runtimeLayerManifest.content.files) `
            -ExcludedRelativePaths @("runtime-layer-manifest.json"))
    ) {
        throw "Cold backend runtime cache manifest did not validate."
    }

    $fullBackendRoot = Join-Path $productBackendRoot "$backendCacheEntryName\scriber-backend"
    $fullRuntimeLayerManifestPath = Join-Path $fullBackendRoot "runtime-layer-manifest.json"
    $applicationRoot = Join-Path $fullBackendRoot "app"
    $applicationManifestPath = Join-Path $applicationRoot "app-layer-manifest.json"
    $applicationManifest = Get-Content -LiteralPath $applicationManifestPath -Raw | ConvertFrom-Json
    $fullBackendExePath = Join-Path $fullBackendRoot "scriber-backend.exe"
    if (-not (Test-Path -LiteralPath $fullBackendExePath -PathType Leaf)) {
        $fullBackendExePath = Join-Path $fullBackendRoot "scriber-backend"
    }
    if (
        (Get-FileHash -LiteralPath $fullRuntimeLayerManifestPath -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $runtimeLayerManifestPath -Algorithm SHA256).Hash -or
        (Get-FileHash -LiteralPath $fullBackendExePath -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $runtimeExePath -Algorithm SHA256).Hash -or
        [int]$applicationManifest.schemaVersion -ne 1 -or
        [string]$applicationManifest.name -ne "scriber-backend-application-layer" -or
        [string]$applicationManifest.runtimeCacheKey -ne $runtimeCacheKey -or
        -not (Test-ManifestFileSet `
            -Root $applicationRoot `
            -Entries @($applicationManifest.files) `
            -ExcludedRelativePaths @("app-layer-manifest.json"))
    ) {
        throw "Cold backend composed application layer did not validate."
    }

    Copy-Tree -Source $productBackendRoot -Destination $resolvedBackendCacheRoot
    Copy-Tree -Source (Join-Path $resolvedProductRoot "tauri-sidecar-runtime-cache") -Destination $resolvedRuntimeCacheRoot
    Copy-Tree -Source (Join-Path $resolvedProductRoot "rust-audio-sidecar-cache") -Destination $resolvedAudioCacheRoot
    Copy-Tree -Source (Join-Path $resolvedProductRoot "rust-diarization-sidecar-cache") -Destination $resolvedDiarizationCacheRoot
    Copy-FfmpegCacheAllowlist -Source (Join-Path $resolvedProductRoot "ffmpeg-profile-b-msys2") -Destination $resolvedFfmpegCacheRoot

    $mediaToolsDir = Join-Path $resolvedBackendCacheRoot "$backendCacheEntryName\scriber-backend\tools\ffmpeg"
    Write-Host "Imported attested cold backend product for cache $($backendCacheKey.Substring(0, 12))."
    Write-GitHubOutput -Name "usable" -Value "true"
    Write-GitHubOutput -Name "backend-cache-key" -Value $backendCacheKey
    Write-GitHubOutput -Name "runtime-cache-key" -Value $runtimeCacheKey
    Write-GitHubOutput -Name "media-tools-dir" -Value $mediaToolsDir
} catch {
    Write-Warning "Cold backend product was unusable; using the normal single-runner path: $($_.Exception.Message)"
}
