param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [switch]$PublishFfmpeg,
    [switch]$PublishRustAudio,
    [switch]$PublishRustDiarization,
    [switch]$PublishBackend,
    [Parameter(Mandatory = $true)]
    [string]$FfmpegTag,
    [Parameter(Mandatory = $true)]
    [string]$FfmpegAssetName,
    [Parameter(Mandatory = $true)]
    [string]$RustAudioTag,
    [Parameter(Mandatory = $true)]
    [string]$RustAudioAssetName,
    [Parameter(Mandatory = $true)]
    [string]$RustDiarizationTag,
    [Parameter(Mandatory = $true)]
    [string]$RustDiarizationAssetName,
    [Parameter(Mandatory = $true)]
    [string]$BackendTag,
    [Parameter(Mandatory = $true)]
    [string]$BackendAssetName,
    [string]$OutputRoot = "build\finished-component-cache-publication",
    [ValidateRange(30, 3600)]
    [int]$PublicationTimeoutSeconds = 900
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

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedOutputRoot = if ([System.IO.Path]::IsPathRooted($OutputRoot)) {
    [System.IO.Path]::GetFullPath($OutputRoot)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputRoot))
}
New-Item -ItemType Directory -Force -Path $resolvedOutputRoot | Out-Null

$genericPublisher = Join-Path $repoRoot "scripts\ci\publish_release_cache_artifact.ps1"
$ffmpegPublisher = Join-Path $repoRoot "scripts\ffmpeg\publish_profile_b_release_artifact.ps1"
foreach ($scriptPath in @($genericPublisher, $ffmpegPublisher)) {
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Required cache publisher was not found: $scriptPath"
    }
}

$components = @(
    [pscustomobject]@{
        Name = "FFmpeg Profile B"
        Slug = "ffmpeg-profile-b"
        Enabled = [bool]$PublishFfmpeg
        ScriptPath = $ffmpegPublisher
        Arguments = [ordered]@{
            Repo = $Repo
            Tag = $FfmpegTag
            AssetName = $FfmpegAssetName
            BuildRoot = "build\ffmpeg-profile-b-msys2"
        }
    },
    [pscustomobject]@{
        Name = "Rust audio sidecar"
        Slug = "rust-audio-sidecar"
        Enabled = [bool]$PublishRustAudio
        ScriptPath = $genericPublisher
        Arguments = [ordered]@{
            Repo = $Repo
            Tag = $RustAudioTag
            AssetName = $RustAudioAssetName
            SourcePath = "build\rust-audio-sidecar-cache"
            Title = "Scriber Rust Audio Sidecar Cache v1"
        }
    },
    [pscustomobject]@{
        Name = "Rust diarization sidecar"
        Slug = "rust-diarization-sidecar"
        Enabled = [bool]$PublishRustDiarization
        ScriptPath = $genericPublisher
        Arguments = [ordered]@{
            Repo = $Repo
            Tag = $RustDiarizationTag
            AssetName = $RustDiarizationAssetName
            SourcePath = "build\rust-diarization-sidecar-cache"
            Title = "Scriber Rust Diarization Sidecar Cache v1"
        }
    },
    [pscustomobject]@{
        Name = "Backend sidecar"
        Slug = "backend-sidecar"
        Enabled = [bool]$PublishBackend
        ScriptPath = $genericPublisher
        Arguments = [ordered]@{
            Repo = $Repo
            Tag = $BackendTag
            AssetName = $BackendAssetName
            SourcePath = "build\tauri-sidecar-cache"
            Title = "Scriber Backend Sidecar Cache v2"
        }
    }
)

$started = [System.Collections.Generic.List[object]]::new()
$rows = [System.Collections.Generic.List[object]]::new()

foreach ($component in $components) {
    $childOutputPath = Join-Path $resolvedOutputRoot ("{0}.github-output.txt" -f $component.Slug)
    $logPath = Join-Path $resolvedOutputRoot ("{0}.log" -f $component.Slug)
    Remove-Item -LiteralPath $childOutputPath, $logPath -Force -ErrorAction SilentlyContinue

    if (-not $component.Enabled) {
        $rows.Add([pscustomobject]@{
            Name = $component.Name
            Requested = $false
            Published = $false
            Status = "not-requested"
            ElapsedSeconds = 0.0
            SourceBytes = 0
            ArchiveBytes = 0
            CompressionMs = 0
            UploadMs = 0
            OutputFile = [System.IO.Path]::GetFileName($childOutputPath)
            LogFile = [System.IO.Path]::GetFileName($logPath)
        })
        continue
    }

    try {
        $job = Start-Job -Name ("scriber-cache-{0}" -f $component.Slug) -ScriptBlock {
            param(
                [string]$WorkingDirectory,
                [string]$ChildOutputPath,
                [string]$LogPath,
                [string]$ComponentSlug,
                [string]$ScriptPath,
                [string]$Repository,
                [string]$Tag,
                [string]$AssetName,
                [string]$SourcePath,
                [string]$Title,
                [string]$BuildRoot
            )
            # Native stderr from gh is diagnostic output. The publisher scripts
            # own their exit/soft-failure contract, so do not promote a line on
            # stderr to a terminating PowerShell job error here.
            $ErrorActionPreference = "Continue"
            Set-Location -LiteralPath $WorkingDirectory
            $env:GITHUB_OUTPUT = $ChildOutputPath
            $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
            $exitCode = 1
            try {
                # Execute only checked-in publisher scripts with explicit,
                # allowlisted parameters. Avoid generated source, dynamic
                # evaluation, and encoded/inline PowerShell commands:
                # those patterns are unnecessary here and look hostile to EDR.
                if ($ComponentSlug -eq "ffmpeg-profile-b") {
                    $lines = @(
                        & powershell.exe -NoProfile -File $ScriptPath `
                            -Repo $Repository `
                            -Tag $Tag `
                            -AssetName $AssetName `
                            -BuildRoot $BuildRoot 2>&1
                    )
                } elseif ($ComponentSlug -in @("rust-audio-sidecar", "rust-diarization-sidecar", "backend-sidecar")) {
                    $lines = @(
                        & powershell.exe -NoProfile -File $ScriptPath `
                            -Repo $Repository `
                            -Tag $Tag `
                            -AssetName $AssetName `
                            -SourcePath $SourcePath `
                            -Title $Title 2>&1
                    )
                } else {
                    throw "Unsupported cache publisher component: $ComponentSlug"
                }
                $exitCode = $LASTEXITCODE
                @($lines | ForEach-Object { [string]$_ }) |
                    Set-Content -LiteralPath $LogPath -Encoding utf8
            } catch {
                ("Parallel cache publisher crashed: {0}" -f $_.Exception.Message) |
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
            $component.Slug,
            $component.ScriptPath,
            $Repo,
            [string]$component.Arguments["Tag"],
            [string]$component.Arguments["AssetName"],
            [string]$component.Arguments["SourcePath"],
            [string]$component.Arguments["Title"],
            [string]$component.Arguments["BuildRoot"]
        )

        $started.Add([pscustomobject]@{
            Component = $component
            Job = $job
            ChildOutputPath = $childOutputPath
            LogPath = $logPath
        })
        Write-Host "Started bounded cache publication: $($component.Name)"
    } catch {
        $message = "Could not start bounded cache publication for $($component.Name): $($_.Exception.Message)"
        Write-Warning $message
        Write-Host "::warning title=Scriber cache publication::$message"
        $rows.Add([pscustomobject]@{
            Name = $component.Name
            Requested = $true
            Published = $false
            Status = "start-failed"
            ElapsedSeconds = 0.0
            SourceBytes = 0
            ArchiveBytes = 0
            CompressionMs = 0
            UploadMs = 0
            OutputFile = [System.IO.Path]::GetFileName($childOutputPath)
            LogFile = [System.IO.Path]::GetFileName($logPath)
        })
    }
}

if ($started.Count -gt 0) {
    $publisherJobs = @($started | ForEach-Object { $_.Job })
    Write-Host "Waiting up to $PublicationTimeoutSeconds seconds for $($publisherJobs.Count) bounded cache publishers in parallel."
    Wait-Job -Job $publisherJobs -Timeout $PublicationTimeoutSeconds | Out-Null
    $timedOutJobs = @($publisherJobs | Where-Object { $_.State -notin @("Completed", "Failed", "Stopped") })
    if ($timedOutJobs.Count -gt 0) {
        $message = "$($timedOutJobs.Count) cache publication job(s) exceeded the shared $PublicationTimeoutSeconds-second deadline and will be stopped."
        Write-Warning $message
        Write-Host "::warning title=Scriber cache publication::$message"
        Stop-Job -Job $timedOutJobs -ErrorAction SilentlyContinue
    }
}

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
    $published = (Get-ChildOutputValue -Path $item.ChildOutputPath -Name "published") -eq "true"
    $sourceBytes = [int64]([string](Get-ChildOutputValue -Path $item.ChildOutputPath -Name "source-bytes") -as [int64])
    $archiveBytes = [int64]([string](Get-ChildOutputValue -Path $item.ChildOutputPath -Name "archive-bytes") -as [int64])
    $compressionMs = [int64]([string](Get-ChildOutputValue -Path $item.ChildOutputPath -Name "compression-ms") -as [int64])
    $uploadMs = [int64]([string](Get-ChildOutputValue -Path $item.ChildOutputPath -Name "upload-ms") -as [int64])
    $succeeded = $jobState -eq "Completed" -and $exitCode -eq 0 -and $published
    $status = if ($succeeded) { "published" } elseif ($jobState -ne "Completed") { "job-$($jobState.ToLowerInvariant())" } elseif ($exitCode -ne 0) { "publisher-exit-$exitCode" } else { "publisher-declined" }

    if (Test-Path -LiteralPath $item.LogPath -PathType Leaf) {
        Write-Host "--- $($item.Component.Name) cache publication log ---"
        Get-Content -LiteralPath $item.LogPath | ForEach-Object { Write-Host $_ }
    }
    if (-not $succeeded) {
        $message = "$($item.Component.Name) cache publication was best-effort and did not complete ($status). The verified app release remains valid."
        Write-Warning $message
        Write-Host "::warning title=Scriber cache publication::$message"
    }

    $rows.Add([pscustomobject]@{
        Name = $item.Component.Name
        Requested = $true
        Published = $published
        Status = $status
        ElapsedSeconds = $elapsedSeconds
        SourceBytes = $sourceBytes
        ArchiveBytes = $archiveBytes
        CompressionMs = $compressionMs
        UploadMs = $uploadMs
        OutputFile = [System.IO.Path]::GetFileName($item.ChildOutputPath)
        LogFile = [System.IO.Path]::GetFileName($item.LogPath)
    })
    Remove-Job -Job $item.Job -Force -ErrorAction SilentlyContinue
}

$requestedCount = @($rows | Where-Object { $_.Requested }).Count
$publishedCount = @($rows | Where-Object { $_.Published }).Count
$failedCount = @($rows | Where-Object { $_.Requested -and -not $_.Published }).Count
$summaryPath = Join-Path $resolvedOutputRoot "summary.json"
[ordered]@{
    apiVersion = "1"
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    mode = "parallel-best-effort"
    requestedCount = $requestedCount
    publishedCount = $publishedCount
    failedCount = $failedCount
    rows = @($rows)
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath -Encoding utf8

Write-GitHubOutput -Name "requested-count" -Value ([string]$requestedCount)
Write-GitHubOutput -Name "published-count" -Value ([string]$publishedCount)
Write-GitHubOutput -Name "failed-count" -Value ([string]$failedCount)

if ($env:GITHUB_STEP_SUMMARY) {
    $summary = @(
        "### Bounded component cache publication",
        "",
        "The updater release was collected, published, and verified before these best-effort cache uploads started.",
        "",
        "| Component | Requested | Result | Total | Compress | Upload | Archive | Child output |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |"
    )
    foreach ($row in $rows) {
        $summary += ("| {0} | {1} | {2} | {3:N1} s | {4:N1} s | {5:N1} s | {6:N1} MiB | ``{7}`` |" -f $row.Name, $row.Requested, $row.Status, $row.ElapsedSeconds, ($row.CompressionMs / 1000.0), ($row.UploadMs / 1000.0), ($row.ArchiveBytes / 1MB), $row.OutputFile)
    }
    $summary += ""
    $summary += "Cache upload failures are warnings only; they do not invalidate an updater release that already passed publication verification."
    $summary | Out-File -FilePath $env:GITHUB_STEP_SUMMARY -Append -Encoding utf8
}

Write-Host "Bounded cache publication complete: requested=$requestedCount; published=$publishedCount; failed=$failedCount."
exit 0
