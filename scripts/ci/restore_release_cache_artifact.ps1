param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [Parameter(Mandatory = $true)]
    [string]$AssetName,
    [Parameter(Mandatory = $true)]
    [string]$DestinationPath
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
    Write-Warning "GitHub CLI is not available; skipping release cache artifact restore."
    exit 0
}

$artifactDir = Join-Path ([System.IO.Path]::GetTempPath()) ("scriber-release-cache-restore-" + [System.Guid]::NewGuid().ToString("N"))
$assetPath = Join-Path $artifactDir $AssetName

try {
    New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
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
        Write-Host "Release cache artifact '$AssetName' was not found on '$Tag'."
        exit 0
    }

    if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) {
        Write-Warning "GitHub release download completed, but asset was not found at $assetPath."
        exit 0
    }

    if (Test-Path -LiteralPath $DestinationPath -PathType Container) {
        Remove-Item -LiteralPath $DestinationPath -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $DestinationPath | Out-Null
    Expand-Archive -LiteralPath $assetPath -DestinationPath $DestinationPath -Force

    Write-Host "Restored release cache artifact '$AssetName' into $DestinationPath."
    Write-GitHubOutput -Name "restored" -Value "true"
    Write-GitHubOutput -Name "source" -Value "github-release"
} finally {
    if (Test-Path -LiteralPath $artifactDir -PathType Container) {
        Remove-Item -LiteralPath $artifactDir -Recurse -Force
    }
}
