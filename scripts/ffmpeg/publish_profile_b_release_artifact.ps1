param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [Parameter(Mandatory = $true)]
    [string]$AssetName,
    [string]$BuildRoot = "build\ffmpeg-profile-b-msys2",
    [string]$LockPath = "packaging\ffmpeg-profile-b-release-lock-v1.json"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if ($Tag -notmatch '^ffmpeg-profile-b-n\d+(?:\.\d+)?-v\d+$') {
    throw "Refusing FFmpeg cache publication for non-cache release tag '$Tag'."
}

function Resolve-RepoPath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
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

function Get-ReleaseAssets {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $assetsJson = & gh release view $Tag --repo $Repo --json assets 2>$null
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        return @((($assetsJson | ConvertFrom-Json).assets))
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Assert-FileIdentity {
    param(
        [string]$Path,
        [long]$ExpectedSize,
        [string]$ExpectedSha256,
        [string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    if ([long]$item.Length -ne $ExpectedSize) {
        throw "$Label size mismatch: expected $ExpectedSize bytes, found $($item.Length)."
    }
    $actualSha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -cne $ExpectedSha256) {
        throw "$Label SHA-256 mismatch: expected $ExpectedSha256, found $actualSha256."
    }
}

Write-GitHubOutput -Name "published" -Value "false"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Stop-Soft "GitHub CLI is not available; skipping FFmpeg Profile B release artifact publish."
}

$resolvedLockPath = Resolve-RepoPath -Path $LockPath
if (-not (Test-Path -LiteralPath $resolvedLockPath -PathType Leaf)) {
    throw "FFmpeg Profile B release lock is missing: $resolvedLockPath"
}
$lock = Get-Content -LiteralPath $resolvedLockPath -Raw | ConvertFrom-Json
if (
    [int]$lock.schemaVersion -ne 1 -or
    [string]$lock.profile -cne "profile-b" -or
    [string]$lock.repository -cne $Repo -or
    [string]$lock.release.tag -cne $Tag -or
    [string]$lock.release.asset.name -cne $AssetName
) {
    throw "FFmpeg Profile B publication request does not match the immutable release lock."
}
$lockedArchiveSize = [long]$lock.release.asset.sizeBytes
$lockedArchiveSha256 = [string]$lock.release.asset.sha256
if (
    $lockedArchiveSize -le 0 -or
    $lockedArchiveSha256 -cnotmatch '^[0-9a-f]{64}$'
) {
    throw "FFmpeg Profile B release lock has an invalid archive identity."
}

$packageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("scriber-ffmpeg-profile-b-artifact-" + [System.Guid]::NewGuid().ToString("N"))
$assetPath = Join-Path ([System.IO.Path]::GetTempPath()) $AssetName
if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
    Remove-Item -LiteralPath $assetPath -Force
}

$releaseAssets = Get-ReleaseAssets
if ($null -ne $releaseAssets) {
    $matchingAssets = @($releaseAssets | Where-Object { [string]$_.name -ceq $AssetName })
    if ($matchingAssets.Count -gt 1) {
        throw "FFmpeg Profile B release contains duplicate locked assets."
    }
    if ($matchingAssets.Count -eq 1) {
        $matchingAsset = $matchingAssets[0]
        $reportedDigest = [string]$matchingAsset.digest
        if (
            [long]$matchingAsset.size -ne $lockedArchiveSize -or
            $reportedDigest -cne "sha256:$lockedArchiveSha256"
        ) {
            throw "Refusing to overwrite FFmpeg Profile B asset '$AssetName': the published asset differs from the immutable release lock."
        }
        $downloadExitCode = Invoke-GhCommand -Arguments @(
            "release",
            "download",
            $Tag,
            "--repo",
            $Repo,
            "--pattern",
            $AssetName,
            "--dir",
            ([System.IO.Path]::GetDirectoryName($assetPath)),
            "--clobber"
        )
        if ($downloadExitCode -ne 0) {
            throw "Downloading the existing locked FFmpeg Profile B asset failed."
        }
        Assert-FileIdentity `
            -Path $assetPath `
            -ExpectedSize $lockedArchiveSize `
            -ExpectedSha256 $lockedArchiveSha256 `
            -Label "Published FFmpeg Profile B archive"
        Write-Host "Reused immutable FFmpeg Profile B release artifact '$AssetName'; no archive or upload was needed."
        Write-GitHubOutput -Name "published" -Value "true"
        Write-GitHubOutput -Name "source" -Value "existing-locked-release"
        Remove-Item -LiteralPath $assetPath -Force
        return
    }
    if ($releaseAssets.Count -gt 0) {
        throw "Refusing FFmpeg Profile B publication because the fixed cache release already contains a different asset."
    }
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
    Assert-FileIdentity `
        -Path $assetPath `
        -ExpectedSize $lockedArchiveSize `
        -ExpectedSha256 $lockedArchiveSha256 `
        -Label "Prepared FFmpeg Profile B archive"

    $releaseUploadExitCode = Invoke-GhCommand -Arguments @(
        "release",
        "upload",
        $Tag,
        $assetPath,
        "--repo",
        $Repo
    )
    if ($releaseUploadExitCode -ne 0) {
        Stop-Soft "Failed to upload FFmpeg Profile B release artifact '$AssetName'."
    }

    Write-Host "Published the exact locked FFmpeg Profile B release artifact '$AssetName' to '$Tag'."
    Write-GitHubOutput -Name "published" -Value "true"
    Write-GitHubOutput -Name "source" -Value "new-locked-release"
} finally {
    if (Test-Path -LiteralPath $packageRoot -PathType Container) {
        Remove-Item -LiteralPath $packageRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
        Remove-Item -LiteralPath $assetPath -Force
    }
}
