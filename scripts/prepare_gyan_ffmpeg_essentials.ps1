<#
.SYNOPSIS
Downloads and validates the Gyan FFmpeg release essentials build.

.DESCRIPTION
The script prepares a local media-tools directory containing ffmpeg.exe and
ffprobe.exe from Gyan's release essentials ZIP. The archive is verified against
Gyan's published SHA256 before extraction. The resulting bin directory is
reported as JSON so build scripts can pass it to -MediaToolsDir.
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$OutputRoot = "",
    [string]$DownloadUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    [string]$Sha256Url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Convert-ToFullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-UnderRoot {
    param(
        [string]$Root,
        [string]$Path,
        [string]$Label
    )

    $rootFull = Convert-ToFullPath -Path $Root
    $pathFull = Convert-ToFullPath -Path $Path
    if (-not $rootFull.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $rootFull += [System.IO.Path]::DirectorySeparatorChar
    }
    if (-not $pathFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under $rootFull. Got: $pathFull"
    }
}

function Get-FirstSha256Token {
    param([string]$Text)

    $token = (($Text.Trim() -split "\s+") | Select-Object -First 1).ToLowerInvariant()
    if (-not ($token -match "^[0-9a-f]{64}$")) {
        throw "Downloaded SHA256 content did not contain a valid SHA256 hash."
    }
    return $token
}

function Get-FileInfoPayload {
    param([string]$Path)

    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        path = $item.FullName
        sizeBytes = [int64]$item.Length
        sizeMiB = [math]::Round($item.Length / 1MB, 2)
    }
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $RepoRoot "build\media-tools\gyan-essentials"
}
$OutputRoot = Convert-ToFullPath -Path $OutputRoot

Assert-UnderRoot -Root $RepoRoot -Path $OutputRoot -Label "Gyan FFmpeg essentials cache"
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$archivePath = Join-Path $OutputRoot "ffmpeg-release-essentials.zip"
$shaPath = Join-Path $OutputRoot "ffmpeg-release-essentials.zip.sha256"

if ($Force -or -not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $archivePath
}
if ($Force -or -not (Test-Path -LiteralPath $shaPath -PathType Leaf)) {
    Invoke-WebRequest -Uri $Sha256Url -OutFile $shaPath
}

$expectedSha256 = Get-FirstSha256Token -Text (Get-Content -LiteralPath $shaPath -Raw)
$actualSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -ne $expectedSha256) {
    throw "Gyan FFmpeg essentials SHA256 mismatch. Expected $expectedSha256, got $actualSha256."
}

$extractRoot = Join-Path $OutputRoot ("extracted-" + $actualSha256.Substring(0, 16))
$binDir = $null
if (Test-Path -LiteralPath $extractRoot -PathType Container) {
    $binDir = Get-ChildItem -LiteralPath $extractRoot -Recurse -Directory |
        Where-Object { $_.Name -eq "bin" -and (Test-Path -LiteralPath (Join-Path $_.FullName "ffmpeg.exe")) } |
        Select-Object -First 1
}

if (-not $binDir) {
    if (Test-Path -LiteralPath $extractRoot) {
        Assert-UnderRoot -Root $OutputRoot -Path $extractRoot -Label "Gyan FFmpeg essentials extraction target"
        Remove-Item -LiteralPath $extractRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
    $binDir = Get-ChildItem -LiteralPath $extractRoot -Recurse -Directory |
        Where-Object { $_.Name -eq "bin" -and (Test-Path -LiteralPath (Join-Path $_.FullName "ffmpeg.exe")) } |
        Select-Object -First 1
}

if (-not $binDir) {
    throw "Could not find a bin directory with ffmpeg.exe in extracted Gyan essentials archive."
}

$ffmpegPath = Join-Path $binDir.FullName "ffmpeg.exe"
$ffprobePath = Join-Path $binDir.FullName "ffprobe.exe"
if (-not (Test-Path -LiteralPath $ffmpegPath -PathType Leaf)) {
    throw "Gyan essentials ffmpeg.exe is missing: $ffmpegPath"
}
if (-not (Test-Path -LiteralPath $ffprobePath -PathType Leaf)) {
    throw "Gyan essentials ffprobe.exe is missing: $ffprobePath"
}

[ordered]@{
    apiVersion = "1"
    ok = $true
    source = "gyan-release-essentials"
    downloadUrl = $DownloadUrl
    sha256Url = $Sha256Url
    sha256 = $actualSha256
    archive = Get-FileInfoPayload -Path $archivePath
    extractRoot = $extractRoot
    mediaToolsDir = $binDir.FullName
    ffmpeg = Get-FileInfoPayload -Path $ffmpegPath
    ffprobe = Get-FileInfoPayload -Path $ffprobePath
} | ConvertTo-Json -Depth 6
