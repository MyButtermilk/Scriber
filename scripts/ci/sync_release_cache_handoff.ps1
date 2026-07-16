<#
.SYNOPSIS
Stages or imports the data-only cache-maintenance handoff for a release run.

.DESCRIPTION
The handoff contains a small JSON control envelope plus an optional passive,
checksum-attested cache payload. The payload can contain binaries, but the
maintenance workflow never executes files downloaded from it. It checks the
envelope against the completed Release Windows run, checks out that exact
source SHA, validates the inventory again, then publishes only fixed component
roots through reviewed repository scripts.
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Export", "Import")]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$Repository,
    [Parameter(Mandatory = $true)]
    [string]$SourceRunId,
    [Parameter(Mandatory = $true)]
    [string]$SourceSha,
    [Parameter(Mandatory = $true)]
    [string]$SourceRef,
    [Parameter(Mandatory = $true)]
    [string]$PythonVersion,
    [string]$WorkflowName = "Release Windows",
    [string]$HandoffRoot = "build\release-cache-handoff",
    [switch]$UseColdBackendProduct,
    [switch]$PublishFfmpeg,
    [switch]$PublishRustAudio,
    [switch]$PublishRustDiarization,
    [switch]$PublishBackendRuntime,
    [switch]$PublishBackend
)

$ErrorActionPreference = "Stop"

function Write-OutputValue {
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
        throw "$Label must remain under $Root."
    }
}

function Copy-Tree {
    param([string]$Source, [string]$Destination)
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Release cache handoff source was not found: $Source"
    }
    if (Test-Path -LiteralPath $Destination -PathType Container) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
    }
}

function Copy-FfmpegAllowlist {
    param([string]$Source, [string]$Destination)
    if (Test-Path -LiteralPath $Destination -PathType Container) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($relative in @(
        "dist\scriber-ffmpeg-profile-b",
        "ffmpeg-profile-b-manifest.json",
        "ffmpeg-profile-b-fixtures.json",
        "media-preparation-smoke-profile-b.json",
        "profile-b-msys2-build-report.json"
    )) {
        $sourcePath = Join-Path $Source $relative
        $destinationPath = Join-Path $Destination $relative
        if (-not (Test-Path -LiteralPath $sourcePath)) {
            throw "Required FFmpeg handoff output was not found: $sourcePath"
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destinationPath) | Out-Null
        if (Test-Path -LiteralPath $sourcePath -PathType Container) {
            Copy-Tree -Source $sourcePath -Destination $destinationPath
        } else {
            Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Force
        }
    }
}

function Get-Attestations {
    param([string]$Root, [string]$ManifestPath)
    $prefix = $Root.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    return @(
        Get-ChildItem -LiteralPath $Root -Recurse -File -Force |
            Where-Object { $_.FullName -ne $ManifestPath } |
            ForEach-Object {
                [ordered]@{
                    path = $_.FullName.Substring($prefix.Length).Replace("\", "/")
                    length = [int64]$_.Length
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            } |
            Sort-Object path
    )
}

function Test-Attestations {
    param([string]$Root, [string]$ManifestPath, [object[]]$Entries)
    $prefix = $Root.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    $actualByPath = @{}
    foreach ($file in @(
        Get-ChildItem -LiteralPath $Root -Recurse -File -Force |
            Where-Object { $_.FullName -ne $ManifestPath }
    )) {
        $relative = $file.FullName.Substring($prefix.Length).Replace("\", "/")
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
            [System.IO.Path]::IsPathRooted($relative) -or
            $relative.Contains("\") -or
            $relative -match '(^|/)\.\.($|/)'
        ) {
            return $false
        }
        $path = [System.IO.Path]::GetFullPath((Join-Path $Root $relative))
        $file = $actualByPath[$relative]
        if (
            -not $path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) -or
            $null -eq $file -or
            $file.FullName -ne $path
        ) {
            return $false
        }
        if (
            [int64]$entry.length -ne [int64]$file.Length -or
            [string]$entry.sha256 -ne (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        ) {
            return $false
        }
    }
    return $true
}

function Get-KeyFingerprint {
    param([string]$Name)
    $path = Join-Path $repoRoot "build\cache-keys\$Name"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Release cache key input was not found: $path"
    }
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

Write-OutputValue -Name "stage-ready" -Value "false"
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw "Repository is not valid." }
if ($SourceRunId -notmatch '^\d+$') { throw "Source run id is not valid." }
if ($SourceSha -notmatch '^[0-9a-f]{40}$') { throw "Source SHA is not valid." }
if ($SourceRef -notmatch '^refs/tags/v[0-9A-Za-z.+-]+$') { throw "Cache handoff is allowed only for a v* tag ref." }
if ($PythonVersion -notmatch '^3\.13\.\d+$') { throw "Cache handoff requires an exact Python 3.13 patch version." }

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$buildRoot = Resolve-RepoPath -Root $repoRoot -Path "build"
$resolvedHandoffRoot = Resolve-RepoPath -Root $repoRoot -Path $HandoffRoot
Assert-UnderRoot -Root $buildRoot -Path $resolvedHandoffRoot -Label "Release cache handoff"
$manifestPath = Join-Path $resolvedHandoffRoot "handoff-manifest.json"
$dataRoot = Join-Path $resolvedHandoffRoot "data"

$publish = [ordered]@{
    ffmpeg = [bool]$PublishFfmpeg
    rustAudio = [bool]$PublishRustAudio
    rustDiarization = [bool]$PublishRustDiarization
    backendRuntime = [bool]$PublishBackendRuntime
    backend = [bool]$PublishBackend
}

if ($Mode -eq "Export") {
    if (Test-Path -LiteralPath $resolvedHandoffRoot -PathType Container) {
        Remove-Item -LiteralPath $resolvedHandoffRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $resolvedHandoffRoot | Out-Null
    $sourceKind = if ($UseColdBackendProduct) { "cold-backend-product" } else { "embedded" }
    if (-not $UseColdBackendProduct) {
        New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
        if ($PublishFfmpeg) { Copy-FfmpegAllowlist -Source (Join-Path $buildRoot "ffmpeg-profile-b-msys2") -Destination (Join-Path $dataRoot "ffmpeg-profile-b-msys2") }
        if ($PublishRustAudio) { Copy-Tree -Source (Join-Path $buildRoot "rust-audio-sidecar-cache") -Destination (Join-Path $dataRoot "rust-audio-sidecar-cache") }
        if ($PublishRustDiarization) { Copy-Tree -Source (Join-Path $buildRoot "rust-diarization-sidecar-cache") -Destination (Join-Path $dataRoot "rust-diarization-sidecar-cache") }
        if ($PublishBackendRuntime) { Copy-Tree -Source (Join-Path $buildRoot "tauri-sidecar-runtime-cache") -Destination (Join-Path $dataRoot "tauri-sidecar-runtime-cache") }
        if ($PublishBackend) { Copy-Tree -Source (Join-Path $buildRoot "tauri-sidecar-cache") -Destination (Join-Path $dataRoot "tauri-sidecar-cache") }
    }
    $manifest = [ordered]@{
        apiVersion = 1
        repository = $Repository
        workflowName = $WorkflowName
        sourceRunId = $SourceRunId
        sourceSha = $SourceSha
        sourceRef = $SourceRef
        sourceTag = $SourceRef.Substring("refs/tags/".Length)
        pythonVersion = $PythonVersion
        sourceKind = $sourceKind
        coldArtifactName = if ($UseColdBackendProduct) { "scriber-cold-backend-product" } else { $null }
        fingerprints = [ordered]@{
            backendRuntime = Get-KeyFingerprint -Name "backend-runtime.txt"
            backend = Get-KeyFingerprint -Name "backend-sidecar.txt"
            rustAudio = Get-KeyFingerprint -Name "rust-audio-sidecar.txt"
            rustDiarization = Get-KeyFingerprint -Name "rust-diarization-sidecar.txt"
        }
        publish = $publish
        files = @(Get-Attestations -Root $resolvedHandoffRoot -ManifestPath $manifestPath)
    }
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($manifestPath, (($manifest | ConvertTo-Json -Depth 8) + "`n"), $encoding)
    Write-OutputValue -Name "stage-ready" -Value "true"
    Write-OutputValue -Name "source-kind" -Value $sourceKind
    exit 0
}

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "Release cache handoff manifest was not found." }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if (
    [int]$manifest.apiVersion -ne 1 -or
    [string]$manifest.repository -ne $Repository -or
    [string]$manifest.workflowName -ne $WorkflowName -or
    [string]$manifest.sourceRunId -ne $SourceRunId -or
    [string]$manifest.sourceSha -ne $SourceSha -or
    [string]$manifest.sourceRef -ne $SourceRef -or
    [string]$manifest.pythonVersion -ne $PythonVersion -or
    [string]$manifest.sourceKind -ne "embedded" -or
    [string]$manifest.fingerprints.backendRuntime -ne (Get-KeyFingerprint -Name "backend-runtime.txt") -or
    [string]$manifest.fingerprints.backend -ne (Get-KeyFingerprint -Name "backend-sidecar.txt") -or
    [string]$manifest.fingerprints.rustAudio -ne (Get-KeyFingerprint -Name "rust-audio-sidecar.txt") -or
    [string]$manifest.fingerprints.rustDiarization -ne (Get-KeyFingerprint -Name "rust-diarization-sidecar.txt") -or
    -not (Test-Attestations -Root $resolvedHandoffRoot -ManifestPath $manifestPath -Entries @($manifest.files))
) {
    throw "Release cache handoff identity or file inventory did not validate."
}

$fixedImports = @(
    [pscustomobject]@{ Enabled = [bool]$manifest.publish.rustAudio; Source = "rust-audio-sidecar-cache"; Destination = "rust-audio-sidecar-cache"; Ffmpeg = $false },
    [pscustomobject]@{ Enabled = [bool]$manifest.publish.rustDiarization; Source = "rust-diarization-sidecar-cache"; Destination = "rust-diarization-sidecar-cache"; Ffmpeg = $false },
    [pscustomobject]@{ Enabled = [bool]$manifest.publish.backendRuntime; Source = "tauri-sidecar-runtime-cache"; Destination = "tauri-sidecar-runtime-cache"; Ffmpeg = $false },
    [pscustomobject]@{ Enabled = [bool]$manifest.publish.backend; Source = "tauri-sidecar-cache"; Destination = "tauri-sidecar-cache"; Ffmpeg = $false },
    [pscustomobject]@{ Enabled = [bool]$manifest.publish.ffmpeg; Source = "ffmpeg-profile-b-msys2"; Destination = "ffmpeg-profile-b-msys2"; Ffmpeg = $true }
)
foreach ($entry in $fixedImports) {
    if (-not $entry.Enabled) { continue }
    $source = Join-Path $dataRoot $entry.Source
    $destination = Join-Path $buildRoot $entry.Destination
    if ($entry.Ffmpeg) {
        Copy-FfmpegAllowlist -Source $source -Destination $destination
    } else {
        Copy-Tree -Source $source -Destination $destination
    }
}

Write-OutputValue -Name "stage-ready" -Value "true"
Write-OutputValue -Name "publish-ffmpeg" -Value (([bool]$manifest.publish.ffmpeg).ToString().ToLowerInvariant())
Write-OutputValue -Name "publish-rust-audio" -Value (([bool]$manifest.publish.rustAudio).ToString().ToLowerInvariant())
Write-OutputValue -Name "publish-rust-diarization" -Value (([bool]$manifest.publish.rustDiarization).ToString().ToLowerInvariant())
Write-OutputValue -Name "publish-backend-runtime" -Value (([bool]$manifest.publish.backendRuntime).ToString().ToLowerInvariant())
Write-OutputValue -Name "publish-backend" -Value (([bool]$manifest.publish.backend).ToString().ToLowerInvariant())
