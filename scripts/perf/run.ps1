param(
    [ValidateSet("Smoke", "FastLocal", "FullLocal", "LiveMicrosoft", "LiveSoniox")]
    [string]$Suite = "FastLocal",
    [string]$InstallRoot = "",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $InstallRoot) {
    $releaseRoot = Join-Path $RepoRoot "Frontend\src-tauri\target\release"
    $releaseDesktop = Join-Path $releaseRoot "scriber-desktop.exe"
    $releaseBackend = Join-Path $releaseRoot "backend\scriber-backend.exe"
    if ((Test-Path -LiteralPath $releaseDesktop -PathType Leaf) -and (Test-Path -LiteralPath $releaseBackend -PathType Leaf)) {
        $InstallRoot = $releaseRoot
    } else {
        $InstallRoot = Join-Path $RepoRoot "Scriber Install"
    }
}
$InstallRoot = (Resolve-Path -LiteralPath $InstallRoot -ErrorAction Stop).Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot "benchmarks\results\raw"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

function Import-DotEnvIntoProcess {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return @()
    }
    $imported = @()
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $separator = $trimmed.IndexOf("=")
        if ($separator -le 0) {
            continue
        }
        $name = $trimmed.Substring(0, $separator).Trim()
        if ($name -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
            continue
        }
        $value = $trimmed.Substring($separator + 1).Trim()
        if ($value.Length -ge 2) {
            $quote = $value[0]
            if (($quote -eq '"' -or $quote -eq "'") -and $value[$value.Length - 1] -eq $quote) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
        $imported += $name
    }
    return @($imported | Sort-Object -Unique)
}

$repoEnvPath = Join-Path $RepoRoot ".env"
$importedEnvNames = @(Import-DotEnvIntoProcess -Path $repoEnvPath)

$profilePath = Join-Path $RepoRoot "benchmarks\results\profile.json"
& powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $RepoRoot "benchmarks\windows\profile.ps1") `
    -OutputPath $profilePath `
    -InstallRoot $InstallRoot | Out-Null

$profile = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$rawPath = Join-Path $OutputDir "$($Suite.ToLowerInvariant())-$stamp.json"

function Write-UnknownMetrics {
    param([string]$Reason)
    $required = @(
        "local_wux",
        "overlay_warm_p95_ms",
        "overlay_cold_p95_ms",
        "microsoft_local_tail_p95_ms",
        "soniox_local_tail_p95_ms",
        "app_ux_p95_ms",
        "hotkey_mic_ready_p95_ms",
        "hotkey_first_audio_frame_p95_ms",
        "text_errors",
        "focus_errors",
        "clipboard_errors",
        "overlay_errors",
        "ui_long_tasks_gt_200ms",
        "idle_cpu_pct",
        "working_set_mb"
    )
    Write-Output "STATUS blocked reason=$Reason"
    foreach ($metric in $required) {
        Write-Output "METRIC $metric=unknown"
    }
}

function Write-MetricPackage {
    param(
        [hashtable]$Metrics,
        [string]$Reason
    )
    $required = @(
        "local_wux",
        "overlay_warm_p95_ms",
        "overlay_cold_p95_ms",
        "microsoft_local_tail_p95_ms",
        "soniox_local_tail_p95_ms",
        "app_ux_p95_ms",
        "hotkey_mic_ready_p95_ms",
        "hotkey_first_audio_frame_p95_ms",
        "text_errors",
        "focus_errors",
        "clipboard_errors",
        "overlay_errors",
        "ui_long_tasks_gt_200ms",
        "idle_cpu_pct",
        "working_set_mb"
    )
    $statusWord = if ($Reason -eq "measured" -or $Reason -like "*measured*") { "measured" } else { "blocked" }
    Write-Output "STATUS $statusWord reason=$Reason"
    foreach ($metric in $required) {
        $value = "unknown"
        if ($Metrics -and $Metrics.ContainsKey($metric)) {
            $value = $Metrics[$metric]
        }
        Write-Output "METRIC $metric=$value"
    }
}

if (-not $profile.runtimeAttestationValid) {
    $payload = [pscustomobject]@{
        schemaVersion = 1
        suite = $Suite
        status = "INVALID_BUILD"
        reason = "runtime_attestation_invalid"
        generatedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        profileId = $profile.profile_id
        importedEnvNames = @($importedEnvNames)
        runtimeAttestationId = $profile.runtimeAttestationId
        runtimeAttestationManifestSha256 = $profile.runtimeAttestationManifestSha256
        runtimeAttestationSourceContentSha256 = $profile.runtimeAttestationSourceContentSha256
        runtimeAttestationErrorCodes = @($profile.runtimeAttestationErrorCodes)
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $rawPath -Encoding UTF8
    Write-UnknownMetrics -Reason "runtime_attestation_invalid"
    exit 2
}

if (-not $profile.binaryVersionMatchesSource) {
    $payload = [pscustomobject]@{
        schemaVersion = 1
        suite = $Suite
        status = "INVALID_BUILD"
        reason = "binary_version_mismatch"
        generatedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        profileId = $profile.profile_id
        installRoot = $InstallRoot
        importedEnvNames = @($importedEnvNames)
        expectedAppVersion = $profile.expectedAppVersion
        desktopProductVersion = $profile.desktopProductVersion
        audioSidecarProductVersion = $profile.audioSidecarProductVersion
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $rawPath -Encoding UTF8
    Write-UnknownMetrics -Reason "binary_version_mismatch"
    exit 2
}

function Get-RequiredEnvPresence {
    param([string[]]$Names)

    $items = @()
    foreach ($name in $Names) {
        $items += [pscustomobject]@{
            name = $name
            present = [bool]([Environment]::GetEnvironmentVariable($name, "Process"))
        }
    }
    return @($items)
}

function Get-RequirementStatus {
    param(
        [object]$Report,
        [string]$RequirementName
    )

    $statuses = @()
    foreach ($benchmark in @($Report.recordingHotPathBenchmarks)) {
        $requirements = $benchmark.summary.requirements
        if (-not $requirements) {
            continue
        }
        $property = $requirements.PSObject.Properties[$RequirementName]
        if ($property -and $property.Value -and $property.Value.status) {
            $statuses += [string]$property.Value.status
        }
    }
    foreach ($sample in @($Report.samples)) {
        $requirements = $sample.recordingHotPathBenchmark.summary.requirements
        if (-not $requirements) {
            continue
        }
        $property = $requirements.PSObject.Properties[$RequirementName]
        if ($property -and $property.Value -and $property.Value.status) {
            $statuses += [string]$property.Value.status
        }
    }
    if ($statuses -contains "measured") {
        return "measured"
    }
    if ($statuses.Count -gt 0) {
        return [string]($statuses | Select-Object -First 1)
    }
    return "missing"
}

function Get-RequirementP95 {
    param(
        [object]$Report,
        [string]$RequirementName
    )

    foreach ($benchmark in @($Report.recordingHotPathBenchmarks)) {
        $requirements = $benchmark.summary.requirements
        if (-not $requirements) {
            continue
        }
        $property = $requirements.PSObject.Properties[$RequirementName]
        if ($property -and $property.Value -and $property.Value.durations -and $property.Value.durations.count -gt 0) {
            return $property.Value.durations.p95Ms
        }
        if ($property -and $property.Value -and $property.Value.providerTranscriptDurations -and $property.Value.providerTranscriptDurations.count -gt 0) {
            return $property.Value.providerTranscriptDurations.p95Ms
        }
    }
    return "unknown"
}

function Get-TextTargetFocusErrors {
    param([object]$Report)

    $values = @()
    foreach ($benchmark in @($Report.recordingHotPathBenchmarks)) {
        $target = $benchmark.summary.textTarget
        if ($target -and $null -ne $target.focusErrors) {
            $values += [int]$target.focusErrors
        }
    }
    foreach ($benchmark in @($Report.summary.recordingHotPathBenchmarks)) {
        $target = $benchmark.summary.textTarget
        if ($target -and $null -ne $target.focusErrors) {
            $values += [int]$target.focusErrors
        }
    }
    foreach ($sample in @($Report.samples)) {
        $target = $sample.recordingHotPathBenchmark.summary.textTarget
        if ($target -and $null -ne $target.focusErrors) {
            $values += [int]$target.focusErrors
        }
    }
    if ($values.Count -eq 0) {
        return 1
    }
    return [int](($values | Measure-Object -Maximum).Maximum)
}

function Invoke-LiveProviderSuite {
    param(
        [string]$SuiteName,
        [string]$RawPath,
        [string]$Stamp
    )

    if ($SuiteName -eq "LiveMicrosoft") {
        $provider = "microsoft"
        $defaultStt = "azure_mai"
        $sonioxMode = ""
        $requiredEnvNames = @("AZURE_MAI_SPEECH_KEY")
        $speechPrompt = "Scriber Microsoft MAI live transcription validation phrase."
    } else {
        $provider = "soniox"
        $defaultStt = "soniox"
        $sonioxMode = "realtime"
        $requiredEnvNames = @("SONIOX_API_KEY")
        $speechPrompt = "Scriber Soniox realtime live transcription validation phrase."
    }

    $requiredEnv = @(Get-RequiredEnvPresence -Names $requiredEnvNames)
    $missingEnv = @($requiredEnv | Where-Object { -not $_.present } | ForEach-Object { $_.name })
    if ($missingEnv.Count -gt 0) {
        $payload = [pscustomobject]@{
            schemaVersion = 1
            suite = $SuiteName
            status = "BLOCKED"
            reason = "missing_provider_credentials"
            generatedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            profileId = $profile.profile_id
            provider = $provider
            importedEnvNames = @($importedEnvNames)
            requiredEnv = @($requiredEnv)
        }
        $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $RawPath -Encoding UTF8
        Write-UnknownMetrics -Reason "missing_provider_credentials"
        exit 2
    }

    $liveWorkDir = Join-Path $RepoRoot "tmp\autoresearch-live-provider"
    New-Item -ItemType Directory -Force -Path $liveWorkDir | Out-Null
    $benchmarkPath = Join-Path $liveWorkDir "$($SuiteName.ToLowerInvariant())-provider-hot-path-$Stamp.json"
    $stdoutPath = Join-Path $OutputDir "$($SuiteName.ToLowerInvariant())-provider-hot-path-$Stamp.out"
    $stderrPath = Join-Path $OutputDir "$($SuiteName.ToLowerInvariant())-provider-hot-path-$Stamp.err"
    $textTargetPath = Join-Path $OutputDir "$($SuiteName.ToLowerInvariant())-text-target-$Stamp.txt"
    Remove-Item -LiteralPath $benchmarkPath, $stdoutPath, $stderrPath, $textTargetPath -Force -ErrorAction SilentlyContinue

    $oldDefaultStt = [Environment]::GetEnvironmentVariable("SCRIBER_DEFAULT_STT", "Process")
    $oldSonioxMode = [Environment]::GetEnvironmentVariable("SCRIBER_SONIOX_MODE", "Process")
    $oldAutoSummarize = [Environment]::GetEnvironmentVariable("SCRIBER_AUTO_SUMMARIZE", "Process")
    try {
        [Environment]::SetEnvironmentVariable("SCRIBER_DEFAULT_STT", $defaultStt, "Process")
        if ($sonioxMode) {
            [Environment]::SetEnvironmentVariable("SCRIBER_SONIOX_MODE", $sonioxMode, "Process")
        }
        [Environment]::SetEnvironmentVariable("SCRIBER_AUTO_SUMMARIZE", "0", "Process")

        $exePath = Join-Path $InstallRoot "scriber-desktop.exe"
        $baselineArgs = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", (Join-Path $RepoRoot "scripts\measure_hybrid_baseline.ps1"),
            "-RepoRoot", $RepoRoot,
            "-ExePath", $exePath,
            "-OutputPath", $benchmarkPath,
            "-Iterations", "1",
            "-TimeoutSec", "150",
            "-BackendHealthTimeoutSec", "45",
            "-UiVisibleTimeoutSec", "30",
            "-RecordHotPathSamples",
            "-RecordingHotPathIterations", "1",
            "-RecordingHotPathSeconds", "8",
            "-RecordingHotPathTimeoutSec", "120",
            "-RecordingHotPathTextTargetFile", $textTargetPath,
            "-RecordingHotPathTextTargetSettleSec", "1",
            "-RecordingHotPathTextTargetTimeoutSec", "12",
            "-RequireRecordingHotPathTextTarget",
            "-RequireRecordingHotPathProviderTranscript",
            "-RecordingHotPathSpeechPrompt", $speechPrompt,
            "-RecordingHotPathSpeechDelaySec", "0.7",
            "-SkipUploadExportBenchmark",
            "-SkipWsBenchmark",
            "-SkipHistoryScrollBenchmark",
            "-DisableDevFallback",
            "-FailOnIncompleteGate"
        )
        & powershell.exe @baselineArgs 1> $stdoutPath 2> $stderrPath
        $benchmarkExit = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable("SCRIBER_DEFAULT_STT", $oldDefaultStt, "Process")
        [Environment]::SetEnvironmentVariable("SCRIBER_SONIOX_MODE", $oldSonioxMode, "Process")
        [Environment]::SetEnvironmentVariable("SCRIBER_AUTO_SUMMARIZE", $oldAutoSummarize, "Process")
    }

    $report = $null
    if (Test-Path -LiteralPath $benchmarkPath -PathType Leaf) {
        $report = Get-Content -LiteralPath $benchmarkPath -Raw | ConvertFrom-Json
    }
    $providerTranscriptStatus = if ($report) { Get-RequirementStatus -Report $report -RequirementName "provider_transcript" } else { "missing_report" }
    $textTargetStatus = if ($report) { Get-RequirementStatus -Report $report -RequirementName "text_target_persistence" } else { "missing_report" }
    $stopToTextStatus = if ($report) { Get-RequirementStatus -Report $report -RequirementName "stop_to_text_injection" } else { "missing_report" }
    $providerP95 = if ($report) { Get-RequirementP95 -Report $report -RequirementName "provider_transcript" } else { "unknown" }
    $textP95 = if ($report) { Get-RequirementP95 -Report $report -RequirementName "text_target_persistence" } else { "unknown" }
    $focusErrors = if ($report) { Get-TextTargetFocusErrors -Report $report } else { 1 }
    $ok = $providerTranscriptStatus -eq "measured" -and $textTargetStatus -eq "measured" -and $focusErrors -eq 0
    $reason = if ($ok) {
        "live_provider_measured_not_baseline"
    } elseif ($providerTranscriptStatus -ne "measured") {
        "live_provider_transcript_missing"
    } elseif ($textTargetStatus -ne "measured") {
        "live_text_target_missing"
    } elseif ($focusErrors -ne 0) {
        "live_text_target_focus_unverified"
    } else {
        "live_provider_benchmark_failed"
    }

    $metricMap = @{
        text_errors = $(if ($textTargetStatus -eq "measured") { 0 } else { 1 })
        focus_errors = $(if ($textTargetStatus -eq "measured" -and $focusErrors -eq 0) { 0 } else { 1 })
        clipboard_errors = 0
    }
    if ($SuiteName -eq "LiveMicrosoft" -and $providerP95 -ne "unknown") {
        $metricMap["microsoft_local_tail_p95_ms"] = $providerP95
    }
    if ($SuiteName -eq "LiveSoniox" -and $providerP95 -ne "unknown") {
        $metricMap["soniox_local_tail_p95_ms"] = $providerP95
    }

    $payload = [pscustomobject]@{
        schemaVersion = 1
        suite = $SuiteName
        status = if ($ok) { "LIVE_PROVIDER_MEASURED" } else { "BLOCKED" }
        reason = $reason
        generatedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        profileId = $profile.profile_id
        provider = $provider
        defaultStt = $defaultStt
        importedEnvNames = @($importedEnvNames)
        requiredEnv = @($requiredEnv)
        benchmarkPath = $benchmarkPath
        benchmarkExitCode = $benchmarkExit
        stdoutPath = $stdoutPath
        stderrPath = $stderrPath
        textTargetPath = $textTargetPath
        requirements = [pscustomobject]@{
            providerTranscript = $providerTranscriptStatus
            textTargetPersistence = $textTargetStatus
            stopToTextInjection = $stopToTextStatus
            textTargetFocusErrors = $focusErrors
        }
        liveDurations = [pscustomobject]@{
            providerTranscriptP95Ms = $providerP95
            textTargetP95Ms = $textP95
        }
        metrics = $metricMap
        baselineEligible = $false
    }
    $payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $RawPath -Encoding UTF8
    Write-MetricPackage -Metrics $metricMap -Reason $reason
    if ($ok) { exit 0 } else { exit 2 }
}

function Get-ScriberProcesses {
    Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match "scriber" } |
        Select-Object ProcessId,Name,ExecutablePath,CommandLine
}

function Normalize-PathForCompare {
    param([string]$Path)
    if (-not $Path) { return "" }
    try {
        return ([System.IO.Path]::GetFullPath($Path)).TrimEnd("\").ToLowerInvariant()
    } catch {
        return $Path.ToLowerInvariant()
    }
}

$allowedRoots = @((Normalize-PathForCompare -Path $InstallRoot))
$releaseRoot = Join-Path $RepoRoot "Frontend\src-tauri\target\release"
if (Test-Path -LiteralPath $releaseRoot -PathType Container) {
    $allowedRoots += (Normalize-PathForCompare -Path $releaseRoot)
}

$existingScriberProcesses = @(Get-ScriberProcesses)
$foreign = @()
foreach ($proc in $existingScriberProcesses) {
    $exe = Normalize-PathForCompare -Path ([string]$proc.ExecutablePath)
    if (-not $exe) { continue }
    $allowed = $false
    foreach ($root in $allowedRoots) {
        if ($exe.StartsWith($root)) {
            $allowed = $true
            break
        }
    }
    if (-not $allowed) {
        $foreign += $proc
    }
}

if ($foreign.Count -gt 0) {
    $payload = [pscustomobject]@{
        schemaVersion = 1
        suite = $Suite
        status = "INVALID_ENVIRONMENT"
        reason = "foreign_scriber_instance"
        generatedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        profileId = $profile.profile_id
        installRoot = $InstallRoot
        importedEnvNames = @($importedEnvNames)
        foreignProcesses = @($foreign)
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $rawPath -Encoding UTF8
    Write-UnknownMetrics -Reason "foreign_scriber_instance"
    exit 2
}

if ($existingScriberProcesses.Count -gt 0) {
    $payload = [pscustomobject]@{
        schemaVersion = 1
        suite = $Suite
        status = "INVALID_ENVIRONMENT"
        reason = "preexisting_scriber_instance"
        generatedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        profileId = $profile.profile_id
        installRoot = $InstallRoot
        importedEnvNames = @($importedEnvNames)
        preexistingProcesses = @($existingScriberProcesses)
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $rawPath -Encoding UTF8
    Write-UnknownMetrics -Reason "preexisting_scriber_instance"
    exit 2
}

if ($Suite -eq "Smoke") {
    $smokePath = Join-Path $OutputDir "desktop-smoke-$stamp.json"
    $dataDir = Join-Path $OutputDir "desktop-smoke-data-$stamp"
    $exePath = Join-Path $InstallRoot "scriber-desktop.exe"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $RepoRoot "scripts\smoke_tauri_desktop.ps1") `
        -ExePath $exePath `
        -OutputPath $smokePath `
        -DataDir $dataDir `
        -VerifyFrontend `
        -VerifyShellMenuSmoke `
        -ShellMenuSmokeActions "show-window,overlay-initializing,overlay-recording,overlay-transcribing,overlay-hide,quit" `
        -ShellMenuSmokeTimeoutSec 30 `
        -TimeoutSec 90 | Out-Null
    $smoke = Get-Content -LiteralPath $smokePath -Raw | ConvertFrom-Json
    $payload = [pscustomobject]@{
        schemaVersion = 1
        suite = $Suite
        status = if ($smoke.ok) { "SMOKE_OK" } else { "SMOKE_FAILED" }
        generatedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        profileId = $profile.profile_id
        importedEnvNames = @($importedEnvNames)
        smokePath = $smokePath
        smoke = $smoke
    }
    $payload | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $rawPath -Encoding UTF8
    Write-Output "STATUS smoke_ok=$($smoke.ok)"
    Write-UnknownMetrics -Reason "smoke_only_not_baseline"
    if ($smoke.ok) { exit 0 } else { exit 2 }
}

if ($Suite -in @("LiveMicrosoft", "LiveSoniox")) {
    Invoke-LiveProviderSuite -SuiteName $Suite -RawPath $rawPath -Stamp $stamp
}

$payload = [pscustomobject]@{
    schemaVersion = 1
    suite = $Suite
    status = "BLOCKED"
    reason = "user_endpoint_probe_failed"
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    profileId = $profile.profile_id
    importedEnvNames = @($importedEnvNames)
    contract = "FastLocal requires external visible overlay, UI Automation text, and stable app frame evidence."
}
$endpointProbePath = Join-Path $OutputDir "endpoint-probe-$stamp.json"
$endpointProbeWorkDir = Join-Path $RepoRoot "tmp\autoresearch-endpoint-probe-$stamp"
$endpointProbeExit = 0
& python (Join-Path $RepoRoot "benchmarks\windows\endpoint_probe.py") `
    --repo-root $RepoRoot `
    --install-root $InstallRoot `
    --output $endpointProbePath `
    --work-dir $endpointProbeWorkDir `
    --timeout-sec 45 | Out-Null
$endpointProbeExit = $LASTEXITCODE
$endpointProbe = $null
if (Test-Path -LiteralPath $endpointProbePath -PathType Leaf) {
    $endpointProbe = Get-Content -LiteralPath $endpointProbePath -Raw | ConvertFrom-Json
}
if ($endpointProbe) {
    $payload.status = $endpointProbe.status
    $payload.reason = $endpointProbe.reason
    $payload | Add-Member -NotePropertyName endpointProbePath -NotePropertyValue $endpointProbePath
    $payload | Add-Member -NotePropertyName endpointProbeExitCode -NotePropertyValue $endpointProbeExit
    $payload | Add-Member -NotePropertyName endpointProbe -NotePropertyValue $endpointProbe
}
$payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $rawPath -Encoding UTF8
$metricMap = @{}
if ($endpointProbe -and $endpointProbe.metrics) {
    foreach ($property in $endpointProbe.metrics.PSObject.Properties) {
        $metricMap[$property.Name] = $property.Value
    }
}
$reason = if ($endpointProbe -and $endpointProbe.reason) { [string]$endpointProbe.reason } else { "missing_real_user_endpoint_evidence" }
Write-MetricPackage -Metrics $metricMap -Reason $reason
$localWux = if ($metricMap.ContainsKey("local_wux")) { [string]$metricMap["local_wux"] } else { "unknown" }
if ($endpointProbe -and [string]$endpointProbe.status -eq "MEASURED" -and $localWux -ne "unknown") {
    exit 0
}
exit 2
