param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [switch]$RestoreRustAudio,
    [switch]$RestoreRustDiarization,
    [switch]$RestoreFfmpeg,
    [Parameter(Mandatory = $true)]
    [string]$RustAudioTag,
    [Parameter(Mandatory = $true)]
    [string]$RustAudioAssetName,
    [Parameter(Mandatory = $true)]
    [string]$RustDiarizationTag,
    [Parameter(Mandatory = $true)]
    [string]$RustDiarizationAssetName,
    [Parameter(Mandatory = $true)]
    [string]$FfmpegTag,
    [Parameter(Mandatory = $true)]
    [string]$FfmpegAssetName,
    [string]$OutputRoot = "build\component-cache-artifact-restore"
)

$ErrorActionPreference = "Stop"

function Write-GitHubOutput {
    param([string]$Name, [string]$Value)
    if ($env:GITHUB_OUTPUT) {
        "$Name=$Value" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

function Get-ChildOutputValue {
    param(
        [string]$Path,
        [string]$Name
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    $prefix = "$Name="
    $matches = @(
        Get-Content -LiteralPath $Path |
            ForEach-Object { ([string]$_).TrimStart([char]0xFEFF) } |
            Where-Object { $_.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) }
    )
    if ($matches.Count -eq 0) {
        return ""
    }
    return $matches[-1].Substring($prefix.Length).Trim()
}

function Resolve-RepoPath {
    param([string]$Root, [string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Root $Path))
}

function Assert-UnderRoot {
    param([string]$Root, [string]$Path, [string]$Label)
    $rootWithSeparator = $Root.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (
        -not $Path.Equals($Root, [System.StringComparison]::OrdinalIgnoreCase) -and
        -not $Path.StartsWith($rootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "$Label must stay under the repository root: $Path"
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedOutputRoot = Resolve-RepoPath -Root $repoRoot -Path $OutputRoot
Assert-UnderRoot -Root $repoRoot -Path $resolvedOutputRoot -Label "Parallel cache restore output root"
New-Item -ItemType Directory -Force -Path $resolvedOutputRoot | Out-Null

$genericRestorer = Join-Path $repoRoot "scripts\ci\restore_release_cache_artifact.ps1"
$ffmpegRestorer = Join-Path $repoRoot "scripts\ffmpeg\restore_profile_b_release_artifact.ps1"
foreach ($scriptPath in @($genericRestorer, $ffmpegRestorer)) {
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Required cache restorer was not found: $scriptPath"
    }
}

$components = @(
    [pscustomobject]@{
        Name = "Rust audio sidecar"
        Slug = "rust-audio-sidecar"
        Enabled = [bool]$RestoreRustAudio
        Kind = "generic"
        ScriptPath = $genericRestorer
        Tag = $RustAudioTag
        AssetName = $RustAudioAssetName
        DestinationPath = "build\rust-audio-sidecar-cache"
    },
    [pscustomobject]@{
        Name = "Rust diarization sidecar"
        Slug = "rust-diarization-sidecar"
        Enabled = [bool]$RestoreRustDiarization
        Kind = "generic"
        ScriptPath = $genericRestorer
        Tag = $RustDiarizationTag
        AssetName = $RustDiarizationAssetName
        DestinationPath = "build\rust-diarization-sidecar-cache"
    },
    [pscustomobject]@{
        Name = "FFmpeg Profile B"
        Slug = "ffmpeg-profile-b"
        Enabled = [bool]$RestoreFfmpeg
        Kind = "ffmpeg"
        ScriptPath = $ffmpegRestorer
        Tag = $FfmpegTag
        AssetName = $FfmpegAssetName
        DestinationPath = "build\ffmpeg-profile-b-msys2"
    }
)

$started = [System.Collections.Generic.List[object]]::new()
$rows = [System.Collections.Generic.List[object]]::new()

foreach ($component in $components) {
    $childOutputPath = Join-Path $resolvedOutputRoot ("{0}.github-output.txt" -f $component.Slug)
    $logPath = Join-Path $resolvedOutputRoot ("{0}.log" -f $component.Slug)
    Remove-Item -LiteralPath $childOutputPath, $logPath -Force -ErrorAction SilentlyContinue

    if (-not $component.Enabled) {
        Write-GitHubOutput -Name "$($component.Slug)-restored" -Value "false"
        Write-GitHubOutput -Name "$($component.Slug)-source" -Value "none"
        Write-GitHubOutput -Name "$($component.Slug)-asset" -Value ""
        Write-GitHubOutput -Name "$($component.Slug)-exact" -Value "false"
        $rows.Add([pscustomobject]@{
            Name = $component.Name
            Requested = $false
            Restored = $false
            Source = "none"
            Status = "not-requested"
            ElapsedSeconds = 0.0
            DestinationPath = $component.DestinationPath
        })
        continue
    }

    $job = Start-Job -Name ("scriber-cache-restore-{0}" -f $component.Slug) -ScriptBlock {
        param(
            [string]$WorkingDirectory,
            [string]$ChildOutputPath,
            [string]$LogPath,
            [string]$Kind,
            [string]$ScriptPath,
            [string]$Repository,
            [string]$Tag,
            [string]$AssetName,
            [string]$DestinationPath
        )
        # The checked-in restore scripts own missing-asset handling. Native gh
        # stderr remains diagnostic output unless the script exits non-zero.
        $ErrorActionPreference = "Continue"
        Set-Location -LiteralPath $WorkingDirectory
        $env:GITHUB_OUTPUT = $ChildOutputPath
        $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        $exitCode = 1
        try {
            if ($Kind -eq "ffmpeg") {
                $lines = @(
                    & $ScriptPath `
                        -Repo $Repository `
                        -Tag $Tag `
                        -AssetName $AssetName `
                        -BuildRoot $DestinationPath 2>&1
                )
            } elseif ($Kind -eq "generic") {
                $lines = @(
                    & $ScriptPath `
                        -Repo $Repository `
                        -Tag $Tag `
                        -AssetName $AssetName `
                        -DestinationPath $DestinationPath 2>&1
                )
            } else {
                throw "Unsupported cache restore kind: $Kind"
            }
            # A normal return is success, including the existing soft
            # missing-asset path. The checked-in scripts throw on corrupt
            # archives or other hard failures, which the catch below records.
            $exitCode = 0
            @($lines | ForEach-Object { [string]$_ }) |
                Set-Content -LiteralPath $LogPath -Encoding utf8
        } catch {
            ("Parallel cache restore crashed: {0}" -f $_.Exception.Message) |
                Set-Content -LiteralPath $LogPath -Encoding utf8
            $exitCode = 1
        } finally {
            $stopwatch.Stop()
        }
        [pscustomobject]@{
            ExitCode = $exitCode
            ElapsedSeconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
        }
    } -ArgumentList @(
        $repoRoot,
        $childOutputPath,
        $logPath,
        $component.Kind,
        $component.ScriptPath,
        $Repo,
        $component.Tag,
        $component.AssetName,
        $component.DestinationPath
    )

    $started.Add([pscustomobject]@{
        Component = $component
        Job = $job
        ChildOutputPath = $childOutputPath
        LogPath = $logPath
    })
    Write-Host "Started release-cache fallback restore: $($component.Name)"
}

if ($started.Count -gt 0) {
    $restoreJobs = @($started | ForEach-Object { $_.Job })
    Write-Host "Waiting for $($restoreJobs.Count) release-cache fallbacks in parallel."
    # Preserve the original restore steps' bounded-by-job semantics. Do not
    # Stop-Job while a nested gh process may still be extracting an archive;
    # wait for each fixed, checked-in restorer and drain it before cleanup.
    Wait-Job -Job $restoreJobs | Out-Null
}

$hardFailures = [System.Collections.Generic.List[string]]::new()
foreach ($item in $started) {
    $jobState = [string]$item.Job.State
    $jobResult = @(Receive-Job -Job $item.Job -ErrorAction SilentlyContinue | Select-Object -Last 1)
    $exitCode = if ($jobResult.Count -gt 0 -and $null -ne $jobResult[0].ExitCode) {
        [int]$jobResult[0].ExitCode
    } else {
        1
    }
    $elapsedSeconds = if ($jobResult.Count -gt 0 -and $null -ne $jobResult[0].ElapsedSeconds) {
        [double]$jobResult[0].ElapsedSeconds
    } else {
        0.0
    }
    $restored = (Get-ChildOutputValue -Path $item.ChildOutputPath -Name "restored") -eq "true"
    $source = Get-ChildOutputValue -Path $item.ChildOutputPath -Name "source"
    $asset = Get-ChildOutputValue -Path $item.ChildOutputPath -Name "asset"
    $exact = (Get-ChildOutputValue -Path $item.ChildOutputPath -Name "exact") -eq "true"
    $hardFailure = $jobState -ne "Completed" -or $exitCode -ne 0
    $status = if ($hardFailure) {
        if ($jobState -ne "Completed") { "job-$($jobState.ToLowerInvariant())" } else { "restore-exit-$exitCode" }
    } elseif ($restored) {
        "restored"
    } else {
        "not-found"
    }

    if (Test-Path -LiteralPath $item.LogPath -PathType Leaf) {
        Write-Host "--- $($item.Component.Name) cache restore log ---"
        Get-Content -LiteralPath $item.LogPath | ForEach-Object { Write-Host $_ }
    }
    if ($hardFailure) {
        $hardFailures.Add("$($item.Component.Name): $status") | Out-Null
    }

    Write-GitHubOutput -Name "$($item.Component.Slug)-restored" -Value $(if ($restored) { "true" } else { "false" })
    Write-GitHubOutput -Name "$($item.Component.Slug)-source" -Value $(if ($source) { $source } else { "none" })
    Write-GitHubOutput -Name "$($item.Component.Slug)-asset" -Value $asset
    Write-GitHubOutput -Name "$($item.Component.Slug)-exact" -Value $(if ($exact) { "true" } else { "false" })

    $rows.Add([pscustomobject]@{
        Name = $item.Component.Name
        Requested = $true
        Restored = $restored
        Source = $(if ($source) { $source } else { "none" })
        Status = $status
        ElapsedSeconds = $elapsedSeconds
        DestinationPath = $item.Component.DestinationPath
    })
    Remove-Job -Job $item.Job -Force -ErrorAction SilentlyContinue
}

$summaryPath = Join-Path $resolvedOutputRoot "summary.json"
[ordered]@{
    apiVersion = "1"
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    mode = "parallel-release-cache-fallback-restore"
    requestedCount = @($rows | Where-Object { $_.Requested }).Count
    restoredCount = @($rows | Where-Object { $_.Restored }).Count
    hardFailureCount = $hardFailures.Count
    rows = @($rows)
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath -Encoding utf8

if ($hardFailures.Count -gt 0) {
    throw "Parallel release-cache fallback restore failed: $($hardFailures -join '; ')"
}

Write-Host "Parallel release-cache fallback restore complete: requested=$(@($rows | Where-Object { $_.Requested }).Count); restored=$(@($rows | Where-Object { $_.Restored }).Count)."
