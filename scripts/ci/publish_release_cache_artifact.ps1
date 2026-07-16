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

$allowedTagPattern = '^release-cache-(backend-sidecar|backend-runtime|python-venv|python-wheelhouse|rust-build|rust-audio-sidecar|rust-diarization-sidecar)-v\d+$'
if ($Tag -notmatch $allowedTagPattern) {
    throw "Refusing cache publication for non-cache release tag '$Tag'."
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
                Write-Warning "Could not list cache assets for '$ReleaseTag'; superseded assets were not pruned."
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
        # Concurrent tag/maintenance runs share these internal release tags.
        # Always keep the globally newest upload, not merely this process's
        # asset, so an older publisher cannot delete a newer sibling.
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
                throw "Deleting superseded cache asset '$($asset.name)' from '$ReleaseTag' failed."
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
    $releaseStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
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
            # GitHub can accept release creation and still close the client
            # connection with a non-zero result. The operation is idempotent:
            # re-read the tag before declining publication so a successfully
            # created empty cache release is immediately populated instead of
            # forcing the next installer to rebuild the component again.
            $releaseRecheckExitCode = 1
            for ($attempt = 1; $attempt -le 5 -and $releaseRecheckExitCode -ne 0; $attempt++) {
                if ($attempt -gt 1) {
                    Start-Sleep -Seconds 2
                }
                $releaseRecheckExitCode = Invoke-GhCommand -Arguments @("release", "view", $Tag, "--repo", $Repo) -SuppressOutput
            }
            if ($releaseRecheckExitCode -ne 0) {
                Stop-Soft "Failed to create release cache artifact release '$Tag'."
            }
            Write-Warning "Release creation returned exit code $releaseCreateExitCode, but '$Tag' now exists; continuing with the cache upload."
        }
    }
    $releaseStopwatch.Stop()

    $sourceFiles = @(Get-ChildItem -LiteralPath $SourcePath -Recurse -File -Force)
    if ($sourceFiles.Count -eq 0) {
        Stop-Soft "Cannot publish release cache artifact because source directory contains no files: $SourcePath"
    }
    $sourceBytes = [int64](($sourceFiles | Measure-Object -Property Length -Sum).Sum)
    $compressionStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    # Finished binaries and Python runtime files are already largely
    # compressed. Fastest avoids spending runner minutes chasing negligible
    # size gains in an internal cache artifact; public installer compression
    # remains governed by Tauri/NSIS and is unaffected.
    Compress-Archive -Path (Join-Path $SourcePath "*") -DestinationPath $assetPath -CompressionLevel Fastest -Force
    $compressionStopwatch.Stop()
    $archiveBytes = [int64](Get-Item -LiteralPath $assetPath).Length

    $uploadStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $releaseUploadExitCode = Invoke-GhCommand -Arguments @(
        "release",
        "upload",
        $Tag,
        $assetPath,
        "--repo",
        $Repo,
        "--clobber"
    )
    $uploadStopwatch.Stop()
    if ($releaseUploadExitCode -ne 0) {
        Stop-Soft "Failed to upload release cache artifact '$AssetName'."
    }
    $prunedAssetCount = Remove-SupersededCacheAssets -ReleaseTag $Tag -KeepAssetName $AssetName

    Write-Host "Published release cache artifact '$AssetName' to '$Tag'."
    Write-Host ("Cache publication metrics: sourceFiles={0}; sourceBytes={1}; archiveBytes={2}; releaseProbeMs={3}; compressionMs={4}; uploadMs={5}; compressionLevel=Fastest; prunedAssets={6}" -f $sourceFiles.Count, $sourceBytes, $archiveBytes, $releaseStopwatch.ElapsedMilliseconds, $compressionStopwatch.ElapsedMilliseconds, $uploadStopwatch.ElapsedMilliseconds, $prunedAssetCount)
    Write-GitHubOutput -Name "published" -Value "true"
    Write-GitHubOutput -Name "source-file-count" -Value ([string]$sourceFiles.Count)
    Write-GitHubOutput -Name "source-bytes" -Value ([string]$sourceBytes)
    Write-GitHubOutput -Name "archive-bytes" -Value ([string]$archiveBytes)
    Write-GitHubOutput -Name "release-probe-ms" -Value ([string]$releaseStopwatch.ElapsedMilliseconds)
    Write-GitHubOutput -Name "compression-ms" -Value ([string]$compressionStopwatch.ElapsedMilliseconds)
    Write-GitHubOutput -Name "upload-ms" -Value ([string]$uploadStopwatch.ElapsedMilliseconds)
    Write-GitHubOutput -Name "compression-level" -Value "Fastest"
    Write-GitHubOutput -Name "pruned-asset-count" -Value ([string]$prunedAssetCount)
} finally {
    if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
        Remove-Item -LiteralPath $assetPath -Force
    }
}
