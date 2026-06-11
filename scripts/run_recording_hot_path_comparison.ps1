<#
.SYNOPSIS
Builds provider-backed Python-vs-Rust recording hot-path comparison evidence.

.DESCRIPTION
Runs the installed Tauri app through measure_hybrid_baseline.ps1 once with the
default Python audio engine and once with the opt-in Rust audio prototype, then
validates both recording hot-path reports with
validate_recording_hot_path_comparison.py.

This script is evidence orchestration only. It requires a built app, microphone
access, provider credentials, and explicit Rust prototype feature flags for the
Rust pass. It does not promote Rust audio to the default engine.
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$OutputDir = "",
    [string]$PythonBaselineOutput = "",
    [string]$RustBaselineOutput = "",
    [string]$ComparisonOutput = "",
    [string]$ExePath = "",
    [string]$PythonPath = "",
    [string]$BackendExePath = "",
    [string]$LegacyDataDir = "",
    [int]$RecordingHotPathIterations = 3,
    [double]$RecordingHotPathSeconds = 2.0,
    [int]$RecordingHotPathTimeoutSec = 60,
    [string]$RecordingHotPathSpeechPrompt = "Scriber provider-backed Rust audio validation",
    [double]$RecordingHotPathSpeechDelaySec = 0.5,
    [double]$MaxAudioOwnedP95RegressionMs = 50.0,
    [string]$RecordingHotPathTextTargetFile = "",
    [double]$RecordingHotPathTextTargetSettleSec = 1.0,
    [double]$RecordingHotPathTextTargetTimeoutSec = 5.0,
    [switch]$RequireRecordingHotPathTextTarget,
    [ValidateSet("wasapi", "synthetic")]
    [string]$RustCaptureMode = "wasapi",
    [switch]$RustAlwaysOnMic,
    [switch]$Hidden,
    [switch]$SkipUiVisibleWait,
    [switch]$DisableDevFallback,
    [switch]$KeepArtifacts,
    [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"

function Convert-ToFullPath {
    param(
        [string]$Path,
        [string]$Root
    )

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Root $Path))
}

function Assert-UnderRoot {
    param(
        [string]$Root,
        [string]$Path,
        [string]$Label
    )

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    if ($pathFull -ne $rootFull -and -not $pathFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must be under $rootFull, got $pathFull"
    }
}

function Convert-ToDisplayCommand {
    param([string[]]$CommandArgs)

    return (($CommandArgs | ForEach-Object {
        if ($_ -match "\s" -or $_ -eq "") {
            '"' + ($_ -replace '"', '\"') + '"'
        } else {
            $_
        }
    }) -join " ")
}

function Write-Utf8NoBomJson {
    param(
        [string]$Path,
        [string]$Json
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Json, $encoding)
}

function Get-CurrentPowerShellPath {
    $process = Get-Process -Id $PID
    if ($process.Path) {
        return $process.Path
    }
    return "powershell"
}

function Get-HotPathReportPath {
    param([string]$BaselineOutput)

    $outputDir = Split-Path $BaselineOutput
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($BaselineOutput)
    return (Join-Path $outputDir "$baseName-recording-hot-path-1.json")
}

function New-BaselineArgs {
    param(
        [string]$OutputPath,
        [bool]$RequireRustAudio
    )

    $args = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        (Join-Path $RepoRoot "scripts\measure_hybrid_baseline.ps1"),
        "-RepoRoot",
        $RepoRoot,
        "-OutputPath",
        $OutputPath,
        "-Iterations",
        "1",
        "-RecordHotPathSamples",
        "-RecordingHotPathIterations",
        ([string]$RecordingHotPathIterations),
        "-RecordingHotPathSeconds",
        ([string]$RecordingHotPathSeconds),
        "-RecordingHotPathTimeoutSec",
        ([string]$RecordingHotPathTimeoutSec),
        "-RequireRecordingHotPathProviderTranscript",
        "-SkipUploadExportBenchmark",
        "-SkipWsBenchmark",
        "-SkipHistoryScrollBenchmark"
    )
    if ($RequireRustAudio) {
        $args += "-RequireRecordingHotPathRustAudio"
    }
    if ($RecordingHotPathSpeechPrompt) {
        $args += @(
            "-RecordingHotPathSpeechPrompt",
            $RecordingHotPathSpeechPrompt,
            "-RecordingHotPathSpeechDelaySec",
            ([string]$RecordingHotPathSpeechDelaySec)
        )
    }
    if ($RecordingHotPathTextTargetFile) {
        $args += @(
            "-RecordingHotPathTextTargetFile",
            $RecordingHotPathTextTargetFile,
            "-RecordingHotPathTextTargetSettleSec",
            ([string]$RecordingHotPathTextTargetSettleSec),
            "-RecordingHotPathTextTargetTimeoutSec",
            ([string]$RecordingHotPathTextTargetTimeoutSec)
        )
    }
    if ($RequireRecordingHotPathTextTarget) {
        $args += "-RequireRecordingHotPathTextTarget"
    }
    if ($ExePath) {
        $args += @("-ExePath", $ExePath)
    }
    if ($PythonPath) {
        $args += @("-PythonPath", $PythonPath)
    }
    if ($BackendExePath) {
        $args += @("-BackendExePath", $BackendExePath)
    }
    if ($LegacyDataDir) {
        $args += @("-LegacyDataDir", $LegacyDataDir)
    }
    if ($Hidden) {
        $args += "-Hidden"
    }
    if ($SkipUiVisibleWait) {
        $args += "-SkipUiVisibleWait"
    }
    if ($DisableDevFallback) {
        $args += "-DisableDevFallback"
    }
    if ($KeepArtifacts) {
        $args += "-KeepArtifacts"
    }
    return $args
}

function Invoke-BaselineWithEnvironment {
    param(
        [string]$Label,
        [string]$AudioEngine,
        [string]$OutputPath,
        [bool]$RequireRustAudio
    )

    $oldAudioEngine = $env:SCRIBER_AUDIO_ENGINE
    $oldWasapiCapture = $env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE
    $oldSyntheticCapture = $env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE
    $oldAlwaysOnMic = $env:SCRIBER_MIC_ALWAYS_ON
    try {
        $env:SCRIBER_AUDIO_ENGINE = $AudioEngine
        if ($AudioEngine -eq "rust-prototype") {
            if ($RustCaptureMode -eq "wasapi") {
                $env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE = "1"
                $env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = $null
            } else {
                $env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = "1"
                $env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE = $null
            }
            if ($RustAlwaysOnMic) {
                $env:SCRIBER_MIC_ALWAYS_ON = "1"
            }
        } else {
            $env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE = $null
            $env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = $null
        }
        if ($AudioEngine -ne "rust-prototype" -or -not $RustAlwaysOnMic) {
            $env:SCRIBER_MIC_ALWAYS_ON = $null
        }

        $ps = Get-CurrentPowerShellPath
        $args = New-BaselineArgs -OutputPath $OutputPath -RequireRustAudio $RequireRustAudio
        & $ps @args
        if ($LASTEXITCODE -ne 0) {
            throw "$Label baseline failed with exit code $LASTEXITCODE."
        }
    } finally {
        $env:SCRIBER_AUDIO_ENGINE = $oldAudioEngine
        $env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE = $oldWasapiCapture
        $env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = $oldSyntheticCapture
        $env:SCRIBER_MIC_ALWAYS_ON = $oldAlwaysOnMic
    }
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot "tmp\hybrid-baseline"
} else {
    $OutputDir = Convert-ToFullPath -Path $OutputDir -Root $RepoRoot
}
Assert-UnderRoot -Root (Join-Path $RepoRoot "tmp") -Path $OutputDir -Label "Comparison output dir"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

if (-not $PythonBaselineOutput) {
    $PythonBaselineOutput = Join-Path $OutputDir "python-recording-hot-path-baseline.json"
} else {
    $PythonBaselineOutput = Convert-ToFullPath -Path $PythonBaselineOutput -Root $RepoRoot
}
if (-not $RustBaselineOutput) {
    $RustBaselineOutput = Join-Path $OutputDir "rust-recording-hot-path-baseline.json"
} else {
    $RustBaselineOutput = Convert-ToFullPath -Path $RustBaselineOutput -Root $RepoRoot
}
if (-not $ComparisonOutput) {
    $ComparisonOutput = Join-Path $OutputDir "recording-hot-path-python-rust-comparison.json"
} else {
    $ComparisonOutput = Convert-ToFullPath -Path $ComparisonOutput -Root $RepoRoot
}

foreach ($path in @($PythonBaselineOutput, $RustBaselineOutput, $ComparisonOutput)) {
    Assert-UnderRoot -Root (Join-Path $RepoRoot "tmp") -Path $path -Label "Comparison artifact"
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
}

$PythonHotPathReport = Get-HotPathReportPath -BaselineOutput $PythonBaselineOutput
$RustHotPathReport = Get-HotPathReportPath -BaselineOutput $RustBaselineOutput
$comparisonArgs = @(
    "scripts\validate_recording_hot_path_comparison.py",
    "--python-report",
    $PythonHotPathReport,
    "--rust-report",
    $RustHotPathReport,
    "--min-samples-per-report",
    ([string]$RecordingHotPathIterations),
    "--max-audio-owned-p95-regression-ms",
    ([string]$MaxAudioOwnedP95RegressionMs),
    "--output",
    $ComparisonOutput
)

$plan = [pscustomobject]@{
    ok = $true
    planOnly = [bool]$PlanOnly
    repoRoot = $RepoRoot
    outputDir = $OutputDir
    rustCaptureMode = $RustCaptureMode
    rustAlwaysOnMic = [bool]$RustAlwaysOnMic
    pythonBaselineOutput = $PythonBaselineOutput
    rustBaselineOutput = $RustBaselineOutput
    pythonHotPathReport = $PythonHotPathReport
    rustHotPathReport = $RustHotPathReport
    comparisonOutput = $ComparisonOutput
    commands = @(
        [pscustomobject]@{
            name = "pythonRecordingHotPath"
            environment = [pscustomobject]@{
                SCRIBER_AUDIO_ENGINE = "python"
            }
            command = (Get-CurrentPowerShellPath) + " " + (Convert-ToDisplayCommand -CommandArgs (New-BaselineArgs -OutputPath $PythonBaselineOutput -RequireRustAudio $false))
        },
        [pscustomobject]@{
            name = "rustRecordingHotPath"
            environment = [pscustomobject]@{
                SCRIBER_AUDIO_ENGINE = "rust-prototype"
                SCRIBER_RUST_AUDIO_WASAPI_CAPTURE = $(if ($RustCaptureMode -eq "wasapi") { "1" } else { "" })
                SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = $(if ($RustCaptureMode -eq "synthetic") { "1" } else { "" })
                SCRIBER_MIC_ALWAYS_ON = $(if ($RustAlwaysOnMic) { "1" } else { "" })
            }
            command = (Get-CurrentPowerShellPath) + " " + (Convert-ToDisplayCommand -CommandArgs (New-BaselineArgs -OutputPath $RustBaselineOutput -RequireRustAudio $true))
        },
        [pscustomobject]@{
            name = "comparisonValidation"
            command = "python " + (Convert-ToDisplayCommand -CommandArgs $comparisonArgs)
        }
    )
}

if ($PlanOnly) {
    $plan | ConvertTo-Json -Depth 8 -Compress
    exit 0
}

Invoke-BaselineWithEnvironment -Label "Python recording hot-path" -AudioEngine "python" -OutputPath $PythonBaselineOutput -RequireRustAudio $false
if (-not (Test-Path -LiteralPath $PythonHotPathReport -PathType Leaf)) {
    throw "Python recording hot-path report was not found: $PythonHotPathReport"
}

Invoke-BaselineWithEnvironment -Label "Rust recording hot-path" -AudioEngine "rust-prototype" -OutputPath $RustBaselineOutput -RequireRustAudio $true
if (-not (Test-Path -LiteralPath $RustHotPathReport -PathType Leaf)) {
    throw "Rust recording hot-path report was not found: $RustHotPathReport"
}

python @comparisonArgs
if ($LASTEXITCODE -ne 0) {
    throw "Recording hot-path Python/Rust comparison validation failed with exit code $LASTEXITCODE."
}

$payload = [pscustomobject]@{
    ok = $true
    pythonBaselineOutput = $PythonBaselineOutput
    rustBaselineOutput = $RustBaselineOutput
    pythonHotPathReport = $PythonHotPathReport
    rustHotPathReport = $RustHotPathReport
    comparisonOutput = $ComparisonOutput
}
$payload | ConvertTo-Json -Depth 5 -Compress
