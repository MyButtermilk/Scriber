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

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Warning "GitHub CLI is not available; skipping FFmpeg Profile B release artifact restore."
    return
}

$artifactDir = Join-Path (Split-Path $BuildRoot -Parent) "ffmpeg-profile-b-release-artifact"
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

if (Test-Path -LiteralPath $BuildRoot -PathType Container) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
Expand-Archive -LiteralPath $assetPath -DestinationPath $BuildRoot -Force

Write-Host "Restored FFmpeg Profile B release artifact '$AssetName' into $BuildRoot."
Write-GitHubOutput -Name "restored" -Value "true"
Write-GitHubOutput -Name "source" -Value "github-release"
