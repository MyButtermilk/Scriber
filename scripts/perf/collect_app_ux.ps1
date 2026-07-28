[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [Parameter(Mandatory = $true)]
    [string]$InstallRoot,
    [Parameter(Mandatory = $true)]
    [string]$OutputDir,
    [ValidateSet("FastLocal", "FullLocal")]
    [string]$Suite = "FastLocal",
    [string]$PythonExecutable = "",
    [string]$RunId = "",
    [ValidateRange(0, 100)]
    [int]$SamplesPerScenario = 0,
    [ValidateRange(5, 300)]
    [int]$TimeoutSec = 45
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:OrchestrationContract = "b7-app-ux-collection-orchestration-v1"
$script:RequestContract = "b7-app-ux-lifecycle-request-v1"
$script:LifecycleContract = "b7-app-ux-lifecycle-import-v1"
$script:FinalContract = "b7-app-ux-v1"
$script:LifecycleScenarios = @(
    "stop_to_transcribing_visible",
    "provider_result_to_completed_visible",
    "session_finished_to_history_visible"
)

function Get-NormalizedFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Test-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $fullPath = Get-NormalizedFullPath -Path $Path
    $fullParent = Get-NormalizedFullPath -Path $Parent
    return (
        $fullPath -ieq $fullParent -or
        $fullPath.StartsWith(
            $fullParent + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    )
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ScriberProcessCount {
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                [string]$_.Name -match "(?i)scriber" -or
                [string]$_.ExecutablePath -match "(?i)[\\/]scriber(?:-desktop|-backend|-audio-sidecar)?\.exe$"
            }
    ).Count
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Write-AtomicJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )
    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $temporary = Join-Path $directory (".{0}.{1}.tmp" -f ([IO.Path]::GetFileName($Path)), [guid]::NewGuid())
    try {
        $json = $Value | ConvertTo-Json -Depth 12
        [IO.File]::WriteAllText(
            $temporary,
            $json + "`n",
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Assert-ExactStringSequence {
    param(
        [Parameter(Mandatory = $true)][object[]]$Actual,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$FailureCode
    )
    if ($Actual.Count -ne $Expected.Count) {
        throw $FailureCode
    }
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        if ([string]$Actual[$index] -cne [string]$Expected[$index]) {
            throw $FailureCode
        }
    }
}

function Invoke-PythonCollector {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $output = @(& $PythonExecutable @Arguments)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw ("collector_failed_{0}" -f $Label)
    }
    return @($output)
}

function Assert-RequestBinding {
    param(
        [Parameter(Mandatory = $true)][object]$Request,
        [Parameter(Mandatory = $true)][string]$InstalledSha256
    )
    if (
        [string]$Request.contract -cne $script:RequestContract -or
        [int]$Request.schemaVersion -ne 1 -or
        [string]$Request.runId -cne $RunId -or
        [string]$Request.installedExeSha256 -cne $InstalledSha256 -or
        [int]$Request.samplesPerScenario -ne $SamplesPerScenario -or
        [string]$Request.requiredImportContract -cne $script:LifecycleContract -or
        [string]$Request.importSchema.path -cne "benchmarks/windows/app_ux_lifecycle_import.schema.json" -or
        -not ([string]$Request.harnessManifestSha256 -match "^[0-9a-f]{64}$") -or
        -not ([string]$Request.importSchema.sha256 -match "^[0-9a-f]{64}$")
    ) {
        throw "prepared_request_binding_invalid"
    }
    $schemaPath = Join-Path $RepoRoot "benchmarks\windows\app_ux_lifecycle_import.schema.json"
    if (
        -not (Test-Path -LiteralPath $schemaPath -PathType Leaf) -or
        (Get-Sha256Hex -Path $schemaPath) -cne [string]$Request.importSchema.sha256
    ) {
        throw "prepared_request_schema_binding_invalid"
    }
    Assert-ExactStringSequence `
        -Actual @($Request.scenarioOrder) `
        -Expected $script:LifecycleScenarios `
        -FailureCode "prepared_request_scenario_order_invalid"
}

function Assert-LifecycleBinding {
    param(
        [Parameter(Mandatory = $true)][object]$Lifecycle,
        [Parameter(Mandatory = $true)][object]$Request
    )
    if (
        [string]$Lifecycle.contract -cne $script:LifecycleContract -or
        [int]$Lifecycle.schemaVersion -ne 1 -or
        [string]$Lifecycle.runId -cne [string]$Request.runId -or
        [string]$Lifecycle.installedExeSha256 -cne [string]$Request.installedExeSha256 -or
        [string]$Lifecycle.harnessManifestSha256 -cne [string]$Request.harnessManifestSha256 -or
        [int]$Lifecycle.samplesPerScenario -ne [int]$Request.samplesPerScenario
    ) {
        throw "lifecycle_import_binding_invalid"
    }
    Assert-ExactStringSequence `
        -Actual @($Lifecycle.scenarioOrder) `
        -Expected $script:LifecycleScenarios `
        -FailureCode "lifecycle_import_scenario_order_invalid"
    $expectedSampleCount = $script:LifecycleScenarios.Count * $SamplesPerScenario
    if (@($Lifecycle.samples).Count -ne $expectedSampleCount) {
        throw "lifecycle_import_sample_count_invalid"
    }
}

function Assert-FinalBinding {
    param(
        [Parameter(Mandatory = $true)][object]$Final,
        [Parameter(Mandatory = $true)][object]$Request,
        [Parameter(Mandatory = $true)][object]$Validation
    )
    if (
        [string]$Final.contract -cne $script:FinalContract -or
        [int]$Final.schemaVersion -ne 1 -or
        [string]$Final.runId -cne [string]$Request.runId -or
        [string]$Final.installedExeSha256 -cne [string]$Request.installedExeSha256 -or
        [string]$Final.harnessManifestSha256 -cne [string]$Request.harnessManifestSha256 -or
        [int]$Final.samplesPerScenario -ne [int]$Request.samplesPerScenario -or
        -not [bool]$Validation.metricEligible
    ) {
        throw "final_app_ux_binding_or_eligibility_invalid"
    }
    $expectedSampleCount = 9 * $SamplesPerScenario
    if (@($Final.samples).Count -ne $expectedSampleCount) {
        throw "final_app_ux_sample_count_invalid"
    }
    $lifecycleImport = $Final.collector.lifecycleImport
    if (
        -not $lifecycleImport -or
        -not ([string]$lifecycleImport.sha256 -match "^[0-9a-f]{64}$")
    ) {
        throw "final_app_ux_lifecycle_binding_missing"
    }
}

$RepoRoot = Get-NormalizedFullPath -Path $RepoRoot
$InstallRoot = Get-NormalizedFullPath -Path $InstallRoot
$OutputDir = Get-NormalizedFullPath -Path $OutputDir
if (
    (Test-PathWithin -Path $OutputDir -Parent $InstallRoot) -or
    (Test-PathWithin -Path $InstallRoot -Parent $OutputDir)
) {
    throw "app_ux_output_and_install_roots_must_not_overlap"
}
if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) {
    throw "installed_payload_root_missing"
}
$installedExe = Join-Path $InstallRoot "scriber-desktop.exe"
if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) {
    throw "installed_desktop_executable_missing"
}
if ((Get-ScriberProcessCount) -ne 0) {
    throw "preexisting_scriber_process"
}
if (Test-Path -LiteralPath $OutputDir) {
    if (@(Get-ChildItem -LiteralPath $OutputDir -Force -ErrorAction Stop).Count -ne 0) {
        throw "app_ux_output_directory_must_be_empty"
    }
} else {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

if (-not $PythonExecutable) {
    $pythonCommand = if ($env:SCRIBER_PYTHON) {
        Get-Command $env:SCRIBER_PYTHON -ErrorAction SilentlyContinue
    } else {
        Get-Command python.exe -ErrorAction SilentlyContinue
    }
    if (-not $pythonCommand) {
        throw "python_executable_missing"
    }
    $PythonExecutable = $pythonCommand.Source
}
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable -ErrorAction Stop).Path
if (-not $RunId) {
    $RunId = [guid]::NewGuid().ToString("N")
} else {
    $parsedRunId = [guid]::Empty
    if (-not [guid]::TryParse($RunId, [ref]$parsedRunId) -or $parsedRunId -eq [guid]::Empty) {
        throw "run_id_must_be_a_non_nil_uuid"
    }
    $RunId = $parsedRunId.ToString("N")
}
if ($SamplesPerScenario -eq 0) {
    $SamplesPerScenario = if ($Suite -eq "FastLocal") { 1 } else { 20 }
}

$appUxCollector = Join-Path $RepoRoot "benchmarks\windows\app_ux_collector.py"
$lifecycleCollector = Join-Path $RepoRoot "benchmarks\windows\app_ux_lifecycle_collector.py"
foreach ($collector in @($appUxCollector, $lifecycleCollector)) {
    if (-not (Test-Path -LiteralPath $collector -PathType Leaf)) {
        throw "required_app_ux_collector_missing"
    }
}

$requestPath = Join-Path $OutputDir "app-ux-lifecycle-request.json"
$lifecyclePath = Join-Path $OutputDir "app-ux-lifecycle-import.json"
$finalPath = Join-Path $OutputDir "app-ux-evidence.json"
$validationPath = Join-Path $OutputDir "app-ux-validation.json"
$summaryPath = Join-Path $OutputDir "app-ux-collection-summary.json"
$installedSha256 = Get-Sha256Hex -Path $installedExe
$summary = [ordered]@{
    schemaVersion = 1
    contract = $script:OrchestrationContract
    ok = $false
    suite = $Suite
    runId = $RunId
    samplesPerScenario = $SamplesPerScenario
    installedExeSha256 = $installedSha256
    request = $null
    lifecycleImport = $null
    finalEvidence = $null
    validation = $null
    failureCode = ""
}

try {
    [void](Invoke-PythonCollector -Label "prepare" -Arguments @(
        $appUxCollector,
        "--repo-root", $RepoRoot,
        "--install-root", $InstallRoot,
        "--output", $finalPath,
        "--run-id", $RunId,
        "--samples-per-scenario", $SamplesPerScenario.ToString(),
        "--timeout-sec", $TimeoutSec.ToString(),
        "--prepare-only"
    ))
    if (-not (Test-Path -LiteralPath $requestPath -PathType Leaf)) {
        throw "prepared_request_missing"
    }
    $request = Read-JsonFile -Path $requestPath
    Assert-RequestBinding -Request $request -InstalledSha256 $installedSha256
    $summary.request = [ordered]@{
        fileName = [IO.Path]::GetFileName($requestPath)
        sha256 = Get-Sha256Hex -Path $requestPath
        harnessManifestSha256 = [string]$request.harnessManifestSha256
        importSchemaSha256 = [string]$request.importSchema.sha256
    }
    $preparedRequestSha256 = [string]$summary.request.sha256

    [void](Invoke-PythonCollector -Label "lifecycle" -Arguments @(
        $lifecycleCollector,
        "--repo-root", $RepoRoot,
        "--install-root", $InstallRoot,
        "--request", $requestPath,
        "--output", $lifecyclePath,
        "--timeout-sec", $TimeoutSec.ToString()
    ))
    if (
        (Get-Sha256Hex -Path $requestPath) -cne $preparedRequestSha256 -or
        (Get-Sha256Hex -Path $installedExe) -cne $installedSha256
    ) {
        throw "request_or_installed_payload_drift_after_lifecycle"
    }
    if (-not (Test-Path -LiteralPath $lifecyclePath -PathType Leaf)) {
        throw "lifecycle_import_missing"
    }
    $lifecycle = Read-JsonFile -Path $lifecyclePath
    Assert-LifecycleBinding -Lifecycle $lifecycle -Request $request
    $summary.lifecycleImport = [ordered]@{
        fileName = [IO.Path]::GetFileName($lifecyclePath)
        sha256 = Get-Sha256Hex -Path $lifecyclePath
        sampleCount = @($lifecycle.samples).Count
    }

    [void](Invoke-PythonCollector -Label "final_merge" -Arguments @(
        $appUxCollector,
        "--repo-root", $RepoRoot,
        "--install-root", $InstallRoot,
        "--output", $finalPath,
        "--run-id", $RunId,
        "--samples-per-scenario", $SamplesPerScenario.ToString(),
        "--lifecycle-evidence", $lifecyclePath,
        "--timeout-sec", $TimeoutSec.ToString()
    ))
    if (
        (Get-Sha256Hex -Path $requestPath) -cne $preparedRequestSha256 -or
        (Get-Sha256Hex -Path $installedExe) -cne $installedSha256
    ) {
        throw "request_or_installed_payload_drift_after_final_merge"
    }
    foreach ($requiredOutput in @($finalPath, $validationPath)) {
        if (-not (Test-Path -LiteralPath $requiredOutput -PathType Leaf)) {
            throw "final_app_ux_output_missing"
        }
    }
    $final = Read-JsonFile -Path $finalPath
    $validation = Read-JsonFile -Path $validationPath
    Assert-FinalBinding -Final $final -Request $request -Validation $validation
    if ((Get-ScriberProcessCount) -ne 0) {
        throw "app_ux_collector_process_cleanup_failed"
    }
    $summary.finalEvidence = [ordered]@{
        fileName = [IO.Path]::GetFileName($finalPath)
        sha256 = Get-Sha256Hex -Path $finalPath
        sampleCount = @($final.samples).Count
        contract = [string]$final.contract
    }
    $summary.validation = [ordered]@{
        fileName = [IO.Path]::GetFileName($validationPath)
        sha256 = Get-Sha256Hex -Path $validationPath
        metricEligible = [bool]$validation.metricEligible
    }
    $summary.ok = $true
} catch {
    $summary.failureCode = "app_ux_collection_failed"
    throw
} finally {
    Write-AtomicJson -Path $summaryPath -Value $summary
}

Write-Output $finalPath
