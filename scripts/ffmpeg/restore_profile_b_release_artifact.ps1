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

function Resolve-RepoPath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
}

function Assert-Sha256 {
    param(
        [string]$Expected,
        [string]$Label
    )
    if ($Expected -notmatch '^[0-9a-f]{64}$') {
        throw "$Label must be a lowercase SHA-256 digest."
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

function Write-GitHubOutput {
    param([string]$Name, [string]$Value)
    if ($env:GITHUB_OUTPUT) {
        "$Name=$Value" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

function Invoke-GhCommand {
    param([string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & gh @Arguments
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

Write-GitHubOutput -Name "restored" -Value "false"
Write-GitHubOutput -Name "source" -Value "none"

$resolvedLockPath = Resolve-RepoPath -Path $LockPath
if (-not (Test-Path -LiteralPath $resolvedLockPath -PathType Leaf)) {
    throw "FFmpeg Profile B release lock is missing: $resolvedLockPath"
}
try {
    $lock = Get-Content -LiteralPath $resolvedLockPath -Raw | ConvertFrom-Json
} catch {
    throw "FFmpeg Profile B release lock is not valid JSON: $resolvedLockPath"
}
if (
    [int]$lock.schemaVersion -ne 1 -or
    [string]$lock.profile -cne "profile-b" -or
    [string]$lock.repository -cne $Repo -or
    [string]$lock.release.tag -cne $Tag -or
    [string]$lock.release.asset.name -cne $AssetName
) {
    throw "FFmpeg Profile B release request does not match the locked repository, tag, and asset."
}
$archiveSize = [long]$lock.release.asset.sizeBytes
$archiveSha256 = [string]$lock.release.asset.sha256
if ($archiveSize -le 0) {
    throw "FFmpeg Profile B release lock has an invalid archive size."
}
Assert-Sha256 -Expected $archiveSha256 -Label "Locked archive digest"
if (-not $lock.files -or @($lock.files).Count -lt 2) {
    throw "FFmpeg Profile B release lock must contain the packaged executable identities."
}
foreach ($lockedFile in @($lock.files)) {
    Assert-Sha256 -Expected ([string]$lockedFile.sha256) -Label "Locked file digest"
    if ([long]$lockedFile.sizeBytes -le 0) {
        throw "FFmpeg Profile B release lock has an invalid file size."
    }
    $relativePath = ([string]$lockedFile.path).Replace("/", "\")
    if (
        [string]::IsNullOrWhiteSpace($relativePath) -or
        [System.IO.Path]::IsPathRooted($relativePath) -or
        $relativePath.Split("\") -contains ".."
    ) {
        throw "FFmpeg Profile B release lock contains an unsafe file path."
    }
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Warning "GitHub CLI is not available; skipping FFmpeg Profile B release artifact restore."
    return
}

$resolvedBuildRoot = Resolve-RepoPath -Path $BuildRoot
$buildParent = Split-Path $resolvedBuildRoot -Parent
$artifactDir = Join-Path $buildParent "ffmpeg-profile-b-release-artifact"
New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
$assetPath = Join-Path $artifactDir $AssetName
if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
    Remove-Item -LiteralPath $assetPath -Force
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
    $artifactDir,
    "--clobber"
)
if ($downloadExitCode -ne 0) {
    Write-Host "FFmpeg Profile B release artifact '$AssetName' was not found on release '$Tag'."
    return
}

if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) {
    Write-Warning "GitHub release download completed, but asset was not found at $assetPath."
    return
}

Assert-FileIdentity `
    -Path $assetPath `
    -ExpectedSize $archiveSize `
    -ExpectedSha256 $archiveSha256 `
    -Label "Downloaded FFmpeg Profile B archive"

# Never replace a usable cache with unverified bytes. The archive identity is
# checked before extraction, and every executable/runtime file is checked in a
# task-local staging directory before the verified tree is promoted.
$extractRoot = Join-Path $buildParent (
    "ffmpeg-profile-b-lock-verify-" + [Guid]::NewGuid().ToString("N")
)
try {
    New-Item -ItemType Directory -Path $extractRoot | Out-Null
    Expand-Archive -LiteralPath $assetPath -DestinationPath $extractRoot
    foreach ($lockedFile in @($lock.files)) {
        $relativePath = ([string]$lockedFile.path).Replace("/", "\")
        Assert-FileIdentity `
            -Path (Join-Path $extractRoot $relativePath) `
            -ExpectedSize ([long]$lockedFile.sizeBytes) `
            -ExpectedSha256 ([string]$lockedFile.sha256) `
            -Label "Extracted FFmpeg Profile B file '$relativePath'"
    }

    $buildReportPath = Join-Path $extractRoot "profile-b-msys2-build-report.json"
    if (-not (Test-Path -LiteralPath $buildReportPath -PathType Leaf)) {
        throw "FFmpeg Profile B provenance report is missing from the locked archive."
    }
    $buildReport = Get-Content -LiteralPath $buildReportPath -Raw | ConvertFrom-Json
    if (
        [string]$buildReport.sourceUrl -cne [string]$lock.source.url -or
        [string]$buildReport.gitRef -cne [string]$lock.source.gitRef
    ) {
        throw "FFmpeg Profile B archive provenance does not match its release lock."
    }

    if (Test-Path -LiteralPath $resolvedBuildRoot) {
        Remove-Item -LiteralPath $resolvedBuildRoot -Recurse -Force
    }
    Move-Item -LiteralPath $extractRoot -Destination $resolvedBuildRoot
} finally {
    if (Test-Path -LiteralPath $extractRoot) {
        Remove-Item -LiteralPath $extractRoot -Recurse -Force
    }
}

Write-Host "Restored and verified FFmpeg Profile B release artifact '$AssetName' into $resolvedBuildRoot."
Write-GitHubOutput -Name "restored" -Value "true"
Write-GitHubOutput -Name "source" -Value "github-release"
