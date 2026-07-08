param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [Parameter(Mandatory = $true)]
    [string]$AssetName,
    [Parameter(Mandatory = $true)]
    [string]$SourcePath,
    [Parameter(Mandatory = $true)]
    [string]$Title,
    [string]$Notes = "Internal reusable Scriber release cache artifact. This release is not an app update."
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
    Stop-Soft "GitHub CLI is not available; skipping release cache artifact publish."
}

if (-not (Test-Path -LiteralPath $SourcePath -PathType Container)) {
    Stop-Soft "Cannot publish release cache artifact because source directory is missing: $SourcePath"
}

$sourceChildren = @(Get-ChildItem -LiteralPath $SourcePath -Force)
if ($sourceChildren.Count -eq 0) {
    Stop-Soft "Cannot publish release cache artifact because source directory is empty: $SourcePath"
}

$assetPath = Join-Path ([System.IO.Path]::GetTempPath()) $AssetName
if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
    Remove-Item -LiteralPath $assetPath -Force
}

try {
    Compress-Archive -Path (Join-Path $SourcePath "*") -DestinationPath $assetPath -Force

    $releaseViewExitCode = Invoke-GhCommand -Arguments @("release", "view", $Tag, "--repo", $Repo) -SuppressOutput
    if ($releaseViewExitCode -ne 0) {
        $releaseCreateExitCode = Invoke-GhCommand -Arguments @(
            "release",
            "create",
            $Tag,
            "--repo",
            $Repo,
            "--title",
            $Title,
            "--notes",
            $Notes,
            "--prerelease",
            "--latest=false"
        )
        if ($releaseCreateExitCode -ne 0) {
            Stop-Soft "Failed to create release cache artifact release '$Tag'."
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
        Stop-Soft "Failed to upload release cache artifact '$AssetName'."
    }

    Write-Host "Published release cache artifact '$AssetName' to '$Tag'."
    Write-GitHubOutput -Name "published" -Value "true"
} finally {
    if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
        Remove-Item -LiteralPath $assetPath -Force
    }
}
