param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [Parameter(Mandatory = $true)]
    [string]$AssetName,
    [Parameter(Mandatory = $true)]
    [string]$DestinationPath,
    [string]$FallbackAssetNamePrefix = ""
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
Write-GitHubOutput -Name "asset" -Value ""
Write-GitHubOutput -Name "exact" -Value "false"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Warning "GitHub CLI is not available; skipping release cache artifact restore."
    return
}

function Restore-Asset {
    param(
        [string]$Name,
        [bool]$Exact
    )

    $assetPath = Join-Path $artifactDir $Name
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
        $Name,
        "--dir",
        $artifactDir,
        "--clobber"
    )
    if ($downloadExitCode -ne 0) {
        return $false
    }

    if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) {
        Write-Warning "GitHub release download completed, but asset was not found at $assetPath."
        return $false
    }

    if (Test-Path -LiteralPath $DestinationPath -PathType Container) {
        Remove-Item -LiteralPath $DestinationPath -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $DestinationPath | Out-Null
    Expand-Archive -LiteralPath $assetPath -DestinationPath $DestinationPath -Force

    Write-Host "Restored release cache artifact '$Name' into $DestinationPath."
    Write-GitHubOutput -Name "restored" -Value "true"
    Write-GitHubOutput -Name "source" -Value $(if ($Exact) { "github-release-exact" } else { "github-release-prefix" })
    Write-GitHubOutput -Name "asset" -Value $Name
    Write-GitHubOutput -Name "exact" -Value $(if ($Exact) { "true" } else { "false" })
    return $true
}

$artifactDir = Join-Path ([System.IO.Path]::GetTempPath()) ("scriber-release-cache-restore-" + [System.Guid]::NewGuid().ToString("N"))

try {
    New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null

    if (Restore-Asset -Name $AssetName -Exact $true) {
        return
    }

    if ($FallbackAssetNamePrefix) {
        $releaseJson = $null
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $releaseJson = & gh release view $Tag --repo $Repo --json assets 2>$null
            $releaseViewExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($releaseViewExitCode -ne 0) {
            $releaseJson = $null
        }
        if ($releaseJson) {
            $release = $releaseJson | ConvertFrom-Json
            $fallback = @($release.assets) |
                Where-Object { $_.name -like "$FallbackAssetNamePrefix*" -and $_.name -ne $AssetName } |
                Sort-Object -Property @{ Expression = { [DateTime]$_.updatedAt }; Descending = $true } |
                Select-Object -First 1
            if ($fallback -and (Restore-Asset -Name $fallback.name -Exact $false)) {
                return
            }
        }
    }

    Write-Host "Release cache artifact '$AssetName' was not found on '$Tag'."
} finally {
    if (Test-Path -LiteralPath $artifactDir -PathType Container) {
        Remove-Item -LiteralPath $artifactDir -Recurse -Force
    }
}
