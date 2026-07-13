param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Export", "Import")]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$CacheKey,
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$CacheRoot = "build\tauri-app-binary-cache",
    [string]$BinaryPath = "Frontend\src-tauri\target\release\scriber-desktop.exe"
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

function Assert-UnderRoot {
    param([string]$Root, [string]$Path, [string]$Label)
    $prefix = $Root.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (-not $Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under ${Root}: $Path"
    }
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
$cachedBinaryPath = Join-Path $resolvedCacheRoot "scriber-desktop.exe"
$manifestPath = Join-Path $resolvedCacheRoot "manifest.json"
Assert-UnderRoot -Root $buildRoot -Path $resolvedCacheRoot -Label "Tauri app binary cache root"
Assert-UnderRoot -Root $repoRoot -Path $resolvedBinaryPath -Label "Tauri app binary path"

if ($Mode -eq "Export") {
    if (-not (Test-Path -LiteralPath $resolvedBinaryPath -PathType Leaf)) {
        throw "Tauri app binary was not found for export: $resolvedBinaryPath"
    }
    $actualVersion = Get-NormalizedFileVersion -Path $resolvedBinaryPath
    if (-not (Test-ExpectedVersion -Actual $actualVersion -Expected $Version)) {
        throw "Tauri app binary version '$actualVersion' does not match expected version '$Version'."
    }
    if (Test-Path -LiteralPath $resolvedCacheRoot -PathType Container) {
        Remove-Item -LiteralPath $resolvedCacheRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $resolvedCacheRoot | Out-Null
    Copy-Item -LiteralPath $resolvedBinaryPath -Destination $cachedBinaryPath -Force
    $item = Get-Item -LiteralPath $cachedBinaryPath
    $manifest = [ordered]@{
        apiVersion = "1"
        cacheKey = $CacheKey
        appVersion = $Version
        binaryVersion = $actualVersion
        sourceCommit = [string]$env:GITHUB_SHA
        target = "x86_64-pc-windows-msvc"
        profile = "release"
        executable = [ordered]@{
            name = "scriber-desktop.exe"
            length = [int64]$item.Length
            sha256 = (Get-FileHash -LiteralPath $cachedBinaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        exportedAt = (Get-Date).ToUniversalTime().ToString("o")
    }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    Write-Host "Exported exact Tauri app binary cache for $Version ($($item.Length) bytes)."
    Write-GitHubOutput -Name "usable" -Value "true"
    Write-GitHubOutput -Name "sha256" -Value ([string]$manifest.executable.sha256)
    exit 0
}

if (-not (Test-Path -LiteralPath $cachedBinaryPath -PathType Leaf) -or -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    Write-Host "No complete Tauri app binary cache was restored."
    exit 0
}

try {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $item = Get-Item -LiteralPath $cachedBinaryPath
    $sha256 = (Get-FileHash -LiteralPath $cachedBinaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $actualVersion = Get-NormalizedFileVersion -Path $cachedBinaryPath
    $expectedCommit = [string]$env:GITHUB_SHA
    $manifestCommit = [string]$manifest.sourceCommit
    $commitMatches = if ([string]::IsNullOrWhiteSpace($expectedCommit)) {
        $true
    } else {
        -not [string]::IsNullOrWhiteSpace($manifestCommit) -and $manifestCommit -eq $expectedCommit
    }
    $valid = (
        [string]$manifest.apiVersion -eq "1" -and
        [string]$manifest.cacheKey -eq $CacheKey -and
        [string]$manifest.appVersion -eq $Version -and
        [string]$manifest.target -eq "x86_64-pc-windows-msvc" -and
        [string]$manifest.profile -eq "release" -and
        $commitMatches -and
        [int64]$manifest.executable.length -eq [int64]$item.Length -and
        [string]$manifest.executable.sha256 -eq $sha256 -and
        (Test-ExpectedVersion -Actual $actualVersion -Expected $Version)
    )
    if (-not $valid) {
        Write-Warning "Ignoring Tauri app binary cache because its exact attestation did not validate."
        exit 0
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $resolvedBinaryPath) | Out-Null
    Copy-Item -LiteralPath $cachedBinaryPath -Destination $resolvedBinaryPath -Force
    $copiedSha256 = (Get-FileHash -LiteralPath $resolvedBinaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($copiedSha256 -ne $sha256) {
        throw "Imported Tauri app binary checksum changed during copy."
    }
    Write-Host "Imported exact Tauri app binary cache for $Version ($($item.Length) bytes)."
    Write-GitHubOutput -Name "usable" -Value "true"
    Write-GitHubOutput -Name "sha256" -Value $sha256
} catch {
    Write-Warning "Ignoring unusable Tauri app binary cache: $($_.Exception.Message)"
}
