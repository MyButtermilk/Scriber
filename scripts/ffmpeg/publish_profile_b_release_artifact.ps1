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
    Compress-Archive -Path (Join-Path $packageRoot "*") -DestinationPath $assetPath -Force

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
            Stop-Soft "Failed to create FFmpeg Profile B artifact release '$Tag'."
        }
    }

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

    Write-Host "Published FFmpeg Profile B release artifact '$AssetName' to '$Tag'."
    Write-GitHubOutput -Name "published" -Value "true"
} finally {
    if (Test-Path -LiteralPath $packageRoot -PathType Container) {
        Remove-Item -LiteralPath $packageRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
        Remove-Item -LiteralPath $assetPath -Force
    }
}
