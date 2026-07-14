param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [Parameter(Mandatory = $true)]
    [string]$AssetName,
    [string]$BuildRoot = "build\ffmpeg-profile-b-msys2"
)

$ErrorActionPreference = "Stop"

if ($Tag -notmatch '^ffmpeg-profile-b-n\d+(?:\.\d+)?-v\d+$') {
    throw "Refusing FFmpeg cache publication for non-cache release tag '$Tag'."
}

function Write-GitHubOutput {
    param([string]$Name, [string]$Value)
    if ($env:GITHUB_OUTPUT) {
        "$Name=$Value" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

function Stop-Soft {
    param([string]$Message)
    Write-Warning $Message
    Write-GitHubOutput -Name "published" -Value "false"
    exit 0
}

function Invoke-GhCommand {
    param(
        [string[]]$Arguments,
        [switch]$SuppressOutput
    )
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        if ($SuppressOutput) {
            & gh @Arguments *> $null
        } else {
            & gh @Arguments
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Remove-SupersededCacheAssets {
    param([string]$ReleaseTag, [string]$KeepAssetName)

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $assets = @()
        for ($attempt = 1; $attempt -le 5; $attempt++) {
            $assetsJson = & gh release view $ReleaseTag --repo $Repo --json assets 2>$null
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Could not list FFmpeg cache assets for '$ReleaseTag'; superseded assets were not pruned."
                return 0
            }
            $assets = @(($assetsJson | ConvertFrom-Json).assets)
            if ($assets.name -contains $KeepAssetName) {
                break
            }
            if ($attempt -lt 5) {
                Start-Sleep -Seconds 1
            }
        }
        $assets = @(
            $assets |
                Sort-Object -Property `
                    @{ Expression = { [DateTimeOffset]$_.createdAt }; Descending = $true }, `
                    @{ Expression = { if ([string]$_.apiUrl -match '/(\d+)$') { [int64]$Matches[1] } else { 0 } }; Descending = $true }, `
                    @{ Expression = { [string]$_.name }; Descending = $true }
        )
        $removed = 0
        foreach ($asset in @($assets | Select-Object -Skip 1)) {
            $null = & gh release delete-asset $ReleaseTag ([string]$asset.name) --repo $Repo --yes
            if ($LASTEXITCODE -ne 0) {
                throw "Deleting superseded FFmpeg cache asset '$($asset.name)' failed."
            }
            $removed += 1
        }
        return $removed
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

Write-GitHubOutput -Name "published" -Value "false"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Stop-Soft "GitHub CLI is not available; skipping FFmpeg Profile B release artifact publish."
}

$requiredPaths = @(
    (Join-Path $BuildRoot "dist\scriber-ffmpeg-profile-b\bin\ffmpeg.exe"),
    (Join-Path $BuildRoot "dist\scriber-ffmpeg-profile-b\bin\ffprobe.exe"),
    (Join-Path $BuildRoot "ffmpeg-profile-b-manifest.json"),
    (Join-Path $BuildRoot "media-preparation-smoke-profile-b.json"),
    (Join-Path $BuildRoot "profile-b-msys2-build-report.json")
)
foreach ($path in $requiredPaths) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Stop-Soft "Cannot publish FFmpeg Profile B release artifact because required file is missing: $path"
    }
}

$packageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("scriber-ffmpeg-profile-b-artifact-" + [System.Guid]::NewGuid().ToString("N"))
$assetPath = Join-Path ([System.IO.Path]::GetTempPath()) $AssetName
if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
    Remove-Item -LiteralPath $assetPath -Force
}

try {
    $releaseViewExitCode = Invoke-GhCommand -Arguments @("release", "view", $Tag, "--repo", $Repo) -SuppressOutput
    if ($releaseViewExitCode -ne 0) {
        $notes = "Internal reusable Scriber release artifact for FFmpeg Profile B. This release is not an app update."
        $releaseCreateExitCode = Invoke-GhCommand -Arguments @(
            "release",
            "create",
            $Tag,
            "--repo",
            $Repo,
            "--title",
            "FFmpeg Profile B n7.0-v4",
            "--notes",
            $notes,
            "--prerelease",
            "--latest=false"
        )
        if ($releaseCreateExitCode -ne 0) {
            $releaseRecheckExitCode = 1
            for ($attempt = 1; $attempt -le 5 -and $releaseRecheckExitCode -ne 0; $attempt++) {
                if ($attempt -gt 1) { Start-Sleep -Seconds 2 }
                $releaseRecheckExitCode = Invoke-GhCommand -Arguments @("release", "view", $Tag, "--repo", $Repo) -SuppressOutput
            }
            if ($releaseRecheckExitCode -ne 0) {
                Stop-Soft "Failed to create FFmpeg Profile B artifact release '$Tag'."
            }
        }
    }

    New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $BuildRoot "dist") -Destination (Join-Path $packageRoot "dist") -Recurse -Force
    foreach ($fileName in @(
        "ffmpeg-profile-b-manifest.json",
        "ffmpeg-profile-b-fixtures.json",
        "media-preparation-smoke-profile-b.json",
        "profile-b-msys2-build-report.json"
    )) {
        $source = Join-Path $BuildRoot $fileName
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $packageRoot $fileName) -Force
        }
    }
    Compress-Archive -Path (Join-Path $packageRoot "*") -DestinationPath $assetPath -CompressionLevel Fastest -Force

    $releaseUploadExitCode = Invoke-GhCommand -Arguments @(
        "release",
        "upload",
        $Tag,
        $assetPath,
        "--repo",
        $Repo,
        "--clobber"
    )
    if ($releaseUploadExitCode -ne 0) {
        Stop-Soft "Failed to upload FFmpeg Profile B release artifact '$AssetName'."
    }
    $prunedAssetCount = Remove-SupersededCacheAssets -ReleaseTag $Tag -KeepAssetName $AssetName

    Write-Host "Published FFmpeg Profile B release artifact '$AssetName' to '$Tag'; pruned $prunedAssetCount superseded asset(s)."
    Write-GitHubOutput -Name "published" -Value "true"
} finally {
    if (Test-Path -LiteralPath $packageRoot -PathType Container) {
        Remove-Item -LiteralPath $packageRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
        Remove-Item -LiteralPath $assetPath -Force
    }
}
