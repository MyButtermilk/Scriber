<#
.SYNOPSIS
Runs the five-lane installed YouTube video matrix against one verified installer.

.DESCRIPTION
Installs one candidate into a repository-local temporary root, verifies the
bundled offline QuickJS/yt-dlp runtime first, and then runs the five installed
lanes sequentially against one isolated data root. Evidence retains only
allowlisted hashes, timings, outcomes, expected duration/extraction lanes, and
cleanup results. Provider configuration may be loaded from an optional env
file for each child process; its path and values are never written to evidence.
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$ReleaseTag,
    [Parameter(Mandatory = $true)]
    [string]$PreflightEvidencePath,
    [string]$FixturePath = "",
    [string]$InstallDir = "",
    [string]$DataDir = "",
    [string]$OutputDir = "",
    [string]$PythonExecutable = "",
    [string]$EnvFile = "",
    [string[]]$InstallerArguments = @("/S"),
    [int]$InstallTimeoutSec = 300,
    [int]$DesktopTimeoutSec = 900,
    [int]$WorkflowTimeoutSec = 7200
)

$ErrorActionPreference = "Stop"

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-Utf8Json {
    param(
        [Parameter(Mandatory = $true)][object]$Payload,
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Depth = 10
    )
    $json = $Payload | ConvertTo-Json -Depth $Depth
    [System.IO.File]::WriteAllText(
        $Path,
        $json + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function ConvertTo-NativeProcessArgument {
    param([AllowEmptyString()][string]$Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append([char]34)
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes += 1
            continue
        }
        if ($character -eq [char]34) {
            if ($backslashes -gt 0) {
                [void]$builder.Append([char]92, (2 * $backslashes))
            }
            [void]$builder.Append([char]92)
            [void]$builder.Append([char]34)
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append([char]92, $backslashes)
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append([char]92, (2 * $backslashes))
    }
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function Resolve-ProjectPython {
    param([string]$Root, [string]$Requested)
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested -PathType Leaf)) {
            throw "Python executable does not exist."
        }
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    foreach ($candidate in @(
        (Join-Path $Root "venv\Scripts\python.exe"),
        (Join-Path $Root ".venv\Scripts\python.exe")
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "A repository Python executable is required."
}

function Assert-PathBelowRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$AllowedRoot,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $resolvedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
    $resolvedRoot = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd("\")
    if (-not $resolvedPath.StartsWith($resolvedRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must remain below the repository tmp root."
    }
    return $resolvedPath
}

function Read-EnvFileAssignments {
    param([string]$Path)
    $assignments = @()
    if (-not $Path) {
        return $assignments
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Env file does not exist."
    }
    $reservedNames = @(
        "SCRIBER_DATA_DIR",
        "SCRIBER_DATABASE_PATH",
        "SCRIBER_DOWNLOADS_DIR",
        "SCRIBER_LOG_DIR",
        "PYTHONHOME",
        "PYTHONPATH"
    )
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = [string]$rawLine
        if (-not $line.Trim() -or $line.TrimStart().StartsWith("#")) {
            continue
        }
        if ($line.TrimStart().StartsWith("export ")) {
            $line = $line.TrimStart().Substring(7)
        }
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) {
            throw "Env file contains an invalid assignment."
        }
        $name = $line.Substring(0, $separator).Trim()
        if ($name -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
            throw "Env file contains an invalid variable name."
        }
        if ($reservedNames -contains $name.ToUpperInvariant()) {
            throw "Env file may not override matrix path or Python isolation variables."
        }
        $value = $line.Substring($separator + 1).Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $assignments += [pscustomobject]@{ Name = $name; Value = $value }
    }
    return $assignments
}

function Invoke-WithScopedEnvironment {
    param(
        [object[]]$Assignments,
        [scriptblock]$Action
    )
    $previous = @{}
    try {
        foreach ($assignment in $Assignments) {
            $name = [string]$assignment.Name
            $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
            [Environment]::SetEnvironmentVariable($name, [string]$assignment.Value, "Process")
        }
        & $Action
    } finally {
        foreach ($entry in $previous.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable([string]$entry.Key, $entry.Value, "Process")
        }
    }
}

function Wait-InstalledExecutable {
    param(
        [string]$Path,
        [string]$ExpectedVersion,
        [int]$TimeoutSec
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $stableIdentity = ""
    $stableSamples = 0
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $item = Get-Item -LiteralPath $Path
            $identity = "{0}:{1}" -f $item.Length, (Get-Sha256Hex -Path $Path)
            if ($identity -eq $stableIdentity) {
                $stableSamples += 1
            } else {
                $stableIdentity = $identity
                $stableSamples = 1
            }
            $productVersion = [string]$item.VersionInfo.ProductVersion
            if ($stableSamples -ge 3 -and $productVersion.StartsWith($ExpectedVersion, [System.StringComparison]::OrdinalIgnoreCase)) {
                return
            }
        }
        Start-Sleep -Seconds 1
    }
    throw "Installed executable did not reach the expected stable version."
}

function Stop-IsolatedInstallProcesses {
    param([string]$Root)
    $normalizedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd("\") + "\"
    foreach ($process in Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) {
        $executablePath = [string]$process.ExecutablePath
        if ($executablePath -and $executablePath.StartsWith($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-StrictUninstall {
    param(
        [string]$Root,
        [int]$TimeoutSec
    )
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return
    }
    Stop-IsolatedInstallProcesses -Root $Root
    $uninstaller = @(
        (Join-Path $Root "uninstall.exe"),
        (Join-Path $Root "Uninstall.exe")
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $uninstaller) {
        throw "Strict uninstall could not find the exact installed uninstaller."
    }
    $process = Start-Process `
        -FilePath $uninstaller `
        -ArgumentList "/S" `
        -PassThru `
        -WindowStyle Hidden
    if (-not $process.WaitForExit($TimeoutSec * 1000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "Strict uninstall timed out."
    }
    if ($process.ExitCode -ne 0) {
        throw "Strict uninstall returned a non-zero exit code."
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline -and (Test-Path -LiteralPath $Root)) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-Path -LiteralPath $Root) {
        throw "Strict uninstall left the install root behind."
    }
    $remaining = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $path = [string]$_.ExecutablePath
                $path -and $path.StartsWith(
                    [System.IO.Path]::GetFullPath($Root).TrimEnd("\") + "\",
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    if ($remaining.Count -ne 0) {
        throw "Strict uninstall left install-root processes behind."
    }
}

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$tmpRoot = Join-Path $RepoRoot "tmp"
[System.IO.Directory]::CreateDirectory($tmpRoot) | Out-Null
$matrixRoot = Join-Path $tmpRoot "installed-youtube-video-matrix"
if (-not $FixturePath) {
    $FixturePath = Join-Path $RepoRoot "scripts\fixtures\installed-youtube-video-matrix-v1.json"
}
if (-not $InstallDir) {
    $InstallDir = Join-Path $matrixRoot "install"
}
if (-not $DataDir) {
    $DataDir = Join-Path $matrixRoot "data"
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $matrixRoot "evidence"
}

$InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
$FixturePath = (Resolve-Path -LiteralPath $FixturePath).Path
$PreflightEvidencePath = (Resolve-Path -LiteralPath $PreflightEvidencePath).Path
if ($ReleaseTag -notmatch "^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$") {
    throw "ReleaseTag must be an exact v-prefixed semantic version."
}
$InstallDir = Assert-PathBelowRoot -Path $InstallDir -AllowedRoot $tmpRoot -Label "InstallDir"
$DataDir = Assert-PathBelowRoot -Path $DataDir -AllowedRoot $tmpRoot -Label "DataDir"
$OutputDir = Assert-PathBelowRoot -Path $OutputDir -AllowedRoot $tmpRoot -Label "OutputDir"
$PythonExecutable = Resolve-ProjectPython -Root $RepoRoot -Requested $PythonExecutable
$envAssignments = @(Read-EnvFileAssignments -Path $EnvFile)
$scopedChildEnvironment = @($envAssignments) + @(
    [pscustomobject]@{ Name = "SCRIBER_DATABASE_PATH"; Value = "" },
    [pscustomobject]@{ Name = "SCRIBER_DOWNLOADS_DIR"; Value = "" },
    [pscustomobject]@{ Name = "SCRIBER_LOG_DIR"; Value = "" },
    [pscustomobject]@{ Name = "PYTHONHOME"; Value = "" },
    [pscustomobject]@{ Name = "PYTHONPATH"; Value = "" }
)
$contractScript = Join-Path $RepoRoot "scripts\installed_youtube_video_matrix.py"
$desktopSmokeScript = Join-Path $RepoRoot "scripts\smoke_tauri_desktop.ps1"
$offlineSmokeScript = Join-Path $RepoRoot "scripts\smoke_quickjs_youtube_runtime.py"
$offlineFixture = Join-Path $RepoRoot "scripts\perf\profiles\installer-size\youtube-holdouts.json"

& $PythonExecutable $contractScript validate-fixture --fixture $FixturePath | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Installed YouTube matrix fixture validation failed."
}
$fixture = Get-Content -LiteralPath $FixturePath -Raw | ConvertFrom-Json
$installerSha256 = Get-Sha256Hex -Path $InstallerPath
$preflightEvidenceSha256 = Get-Sha256Hex -Path $PreflightEvidencePath
$expectedVersion = $ReleaseTag.TrimStart("v")

foreach ($argument in $InstallerArguments) {
    if ([string]$argument -match "^/D=") {
        throw "InstallerArguments must not contain /D; the runner appends its verified InstallDir."
    }
}

foreach ($path in @($InstallDir, $DataDir, $OutputDir)) {
    if (Test-Path -LiteralPath $path) {
        throw "InstallDir, DataDir, and OutputDir must be fresh and nonexistent."
    }
}
$resolvedRoots = @($InstallDir, $DataDir, $OutputDir)
for ($leftIndex = 0; $leftIndex -lt $resolvedRoots.Count; $leftIndex += 1) {
    for ($rightIndex = $leftIndex + 1; $rightIndex -lt $resolvedRoots.Count; $rightIndex += 1) {
        $left = $resolvedRoots[$leftIndex].TrimEnd("\") + "\"
        $right = $resolvedRoots[$rightIndex].TrimEnd("\") + "\"
        if (
            $left.StartsWith($right, [System.StringComparison]::OrdinalIgnoreCase) -or
            $right.StartsWith($left, [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            throw "InstallDir, DataDir, and OutputDir must not overlap."
        }
    }
}
[System.IO.Directory]::CreateDirectory($matrixRoot) | Out-Null
$installedExe = $null
$transientRoot = Join-Path $matrixRoot "transient"
$rawRoot = Join-Path $transientRoot "raw"
$offlineOutput = Join-Path $transientRoot "offline-quickjs.json"
$offlineScratch = Join-Path $transientRoot "offline-quickjs-scratch"
$aggregatePath = $null
if (Test-Path -LiteralPath $transientRoot) {
    throw "Transient matrix work root must be fresh and nonexistent."
}

try {

$installerArgs = @($InstallerArguments) + @("/D=$InstallDir")
$installerArgumentLine = @(
    $installerArgs | ForEach-Object { ConvertTo-NativeProcessArgument -Value ([string]$_) }
) -join " "
$installProcess = Start-Process `
    -FilePath $InstallerPath `
    -ArgumentList $installerArgumentLine `
    -PassThru `
    -WindowStyle Hidden
if (-not $installProcess.WaitForExit($InstallTimeoutSec * 1000)) {
    $installProcess.Kill()
    throw "Installer timed out."
}
if ($installProcess.ExitCode -ne 0) {
    throw "Installer failed with a non-zero exit code."
}

$installedExe = Join-Path $InstallDir "Scriber.exe"
if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) {
    $installedExe = Join-Path $InstallDir "scriber-desktop.exe"
}
Wait-InstalledExecutable -Path $installedExe -ExpectedVersion $expectedVersion -TimeoutSec $InstallTimeoutSec

$dataRootSha256 = (& $PythonExecutable $contractScript path-hash --path $DataDir | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or $dataRootSha256 -notmatch "^[0-9a-f]{64}$") {
    throw "Could not bind the isolated data-root identity."
}

[System.IO.Directory]::CreateDirectory($offlineScratch) | Out-Null
& $PythonExecutable $offlineSmokeScript `
    --candidate-payload $InstallDir `
    --runtime (Join-Path $InstallDir "backend\tools\ffmpeg\qjs.exe") `
    --fixture $offlineFixture `
    --scratch-root $offlineScratch `
    --timeout-seconds 120 `
    --output $offlineOutput | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Bundled offline QuickJS/yt-dlp gate failed before the installed video matrix."
}

& $PythonExecutable $contractScript validate-preflight `
    --fixture $FixturePath `
    --evidence $PreflightEvidencePath `
    --candidate-payload $InstallDir | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Exact-runtime format-selection preflight did not match the installed candidate."
}
$preflightEvidence = Get-Content -LiteralPath $PreflightEvidencePath -Raw | ConvertFrom-Json
[System.IO.Directory]::CreateDirectory($OutputDir) | Out-Null

$laneReports = [System.Collections.Generic.List[string]]::new()
[System.IO.Directory]::CreateDirectory($rawRoot) | Out-Null

function Invoke-LaneAttempt {
    param(
        [object]$Lane,
        [ValidateSet("primary", "replacement")][string]$SelectionMarker,
        [int]$AttemptIndex,
        [bool]$TransientRetryUsed
    )
    $candidate = $Lane.$SelectionMarker
    $safeStem = "{0}-{1}-{2}" -f $Lane.laneId, $AttemptIndex, $SelectionMarker
    $desktopOutput = Join-Path $rawRoot "$safeStem-desktop.json"
    $workflowOutput = Join-Path $rawRoot "$safeStem-workflow.json"
    $laneOutput = Join-Path $rawRoot "$safeStem-lane.json"

    $desktopArgs = @(
        "-NoProfile", "-File", $desktopSmokeScript,
        "-RepoRoot", $RepoRoot,
        "-ExePath", $installedExe,
        "-PythonPath", $PythonExecutable,
        "-DataDir", $DataDir,
        "-OutputPath", $desktopOutput,
        "-TimeoutSec", $DesktopTimeoutSec.ToString(),
        "-VerifyRealMediaWorkflows",
        "-RealWorkflowSkipFile",
        "-RealWorkflowNoSummary",
        "-RealWorkflowEvidenceOutputPath", $workflowOutput,
        "-RealWorkflowYoutubeUrl", [string]$candidate.url,
        "-RealWorkflowYoutubeTimeoutSec", $WorkflowTimeoutSec.ToString(),
        "-RealWorkflowYoutubeLaneId", [string]$Lane.laneId,
        "-RealWorkflowYoutubeSelectionMarker", $SelectionMarker,
        "-RealWorkflowYoutubeExpectedExtractionLane", [string]$Lane.expectedExtractionLane,
        "-RealWorkflowYoutubeExecutionMode", [string]$Lane.executionMode,
        "-RealWorkflowYoutubeDurationMinSec", ([string]$candidate.durationBandSeconds.min),
        "-RealWorkflowYoutubeDurationMaxSec", ([string]$candidate.durationBandSeconds.max),
        "-RealWorkflowReleaseTag", $ReleaseTag,
        "-RealWorkflowInstallerSha256", $installerSha256,
        "-RealWorkflowDataRootSha256", $dataRootSha256
    )
    $attemptTimeoutSec = [Math]::Max($WorkflowTimeoutSec + 120, $DesktopTimeoutSec + 120)
    $desktopArgumentLine = @(
        $desktopArgs | ForEach-Object { ConvertTo-NativeProcessArgument -Value ([string]$_) }
    ) -join " "
    $desktopOutcome = Invoke-WithScopedEnvironment -Assignments $scopedChildEnvironment -Action {
        $desktopProcess = Start-Process `
            -FilePath "powershell.exe" `
            -ArgumentList $desktopArgumentLine `
            -PassThru `
            -WindowStyle Hidden
        $completed = $desktopProcess.WaitForExit($attemptTimeoutSec * 1000)
        if (-not $completed) {
            Stop-Process -Id $desktopProcess.Id -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $desktopProcess.Id -Timeout 10 -ErrorAction SilentlyContinue | Out-Null
        }
        return [pscustomobject]@{
            completed = $completed
            exitCode = if ($completed) { $desktopProcess.ExitCode } else { $null }
        }
    }
    if (-not $desktopOutcome.completed) {
        Stop-IsolatedInstallProcesses -Root $InstallDir
    }

    if (-not (Test-Path -LiteralPath $workflowOutput -PathType Leaf)) {
        Write-Utf8Json -Path $workflowOutput -Payload @{
            apiVersion = "1"
            ok = $false
            checks = @()
            failures = @(@{
                workflow = "youtube"
                ok = $false
                failureCategory = "bundled-runtime"
                failureCode = "workflow_evidence_missing"
            })
        } -Depth 8
    }
    if (-not (Test-Path -LiteralPath $desktopOutput -PathType Leaf)) {
        Write-Utf8Json -Path $desktopOutput -Payload @{ cleanupVerified = $false }
    }

    $finalizeArgs = @(
        $contractScript, "finalize-lane",
        "--fixture", $FixturePath,
        "--workflow-report", $workflowOutput,
        "--desktop-report", $desktopOutput,
        "--lane-id", [string]$Lane.laneId,
        "--selection-marker", $SelectionMarker,
        "--attempt-index", $AttemptIndex.ToString(),
        "--installer-sha256", $installerSha256,
        "--release-tag", $ReleaseTag,
        "--data-root-sha256", $dataRootSha256,
        "--output", $laneOutput
    )
    if ($TransientRetryUsed) {
        $finalizeArgs += "--transient-retry-used"
    }
    & $PythonExecutable @finalizeArgs *> $null
    if (-not (Test-Path -LiteralPath $laneOutput -PathType Leaf)) {
        throw "Lane finalizer did not write redacted evidence."
    }
    return Get-Content -LiteralPath $laneOutput -Raw | ConvertFrom-Json
}

    foreach ($lane in $fixture.lanes) {
        $attempts = [System.Collections.Generic.List[object]]::new()
        $selected = Invoke-LaneAttempt -Lane $lane -SelectionMarker "primary" -AttemptIndex 1 -TransientRetryUsed $false
        $attempts.Add($selected)

        if (-not $selected.ok -and $selected.failureCategory -eq "external-availability") {
            $selected = Invoke-LaneAttempt -Lane $lane -SelectionMarker "primary" -AttemptIndex 2 -TransientRetryUsed $true
            $attempts.Add($selected)
            if (-not $selected.ok -and $selected.failureCategory -eq "external-availability") {
                $selected = Invoke-LaneAttempt -Lane $lane -SelectionMarker "replacement" -AttemptIndex 3 -TransientRetryUsed $true
                $attempts.Add($selected)
            }
        }

        $attemptHistory = @(
            foreach ($attempt in $attempts) {
                [ordered]@{
                    attemptIndex = $attempt.attemptIndex
                    selectionMarker = $attempt.selectionMarker
                    fixtureUrlSha256 = $attempt.fixtureUrlSha256
                    outcome = $attempt.outcome
                    failureCategory = $attempt.failureCategory
                    failureCode = $attempt.failureCode
                    elapsedMs = $attempt.elapsedMs
                    sourceAssetCleanupVerified = $attempt.sourceAssetCleanupVerified
                    childProcessCleanupVerified = $attempt.childProcessCleanupVerified
                }
            }
        )
        $selected | Add-Member -NotePropertyName "attemptHistory" -NotePropertyValue $attemptHistory -Force
        $selected | Add-Member -NotePropertyName "retryPolicy" -NotePropertyValue "one-transient-primary-retry-then-replacement" -Force
        $selected | Add-Member -NotePropertyName "preflightEvidenceSha256" -NotePropertyValue $preflightEvidenceSha256 -Force
        $observedPreflight = @(
            $preflightEvidence.rows |
                Where-Object {
                    $_.laneId -eq $lane.laneId -and
                    $_.selectionMarker -eq $selected.selectionMarker
                }
        )
        if ($observedPreflight.Count -ne 1) {
            throw "Selected lane does not have exactly one bound preflight observation."
        }
        $selected | Add-Member -NotePropertyName "observedExtraction" -NotePropertyValue $observedPreflight[0].observed -Force
        $finalLanePath = Join-Path $OutputDir ("lane-{0}.json" -f $lane.laneId)
        Write-Utf8Json -Path $finalLanePath -Payload $selected -Depth 10
        $laneReports.Add($finalLanePath)
        if (-not $selected.ok) {
            break
        }
    }

    $aggregatePath = Join-Path $OutputDir "installed-youtube-video-matrix.json"
    $aggregateArgs = @($contractScript, "aggregate", "--fixture", $FixturePath)
    foreach ($laneReport in $laneReports) {
        $aggregateArgs += @("--lane-report", $laneReport)
    }
    $aggregateArgs += @("--output", $aggregatePath)
    & $PythonExecutable @aggregateArgs *> $null
    $aggregateExitCode = $LASTEXITCODE
    if (-not (Test-Path -LiteralPath $aggregatePath -PathType Leaf)) {
        throw "Matrix aggregator did not write evidence."
    }
    $aggregate = Get-Content -LiteralPath $aggregatePath -Raw | ConvertFrom-Json
    $aggregate | Add-Member -NotePropertyName "offlineQuickJsVerified" -NotePropertyValue $true -Force
    $aggregate | Add-Member -NotePropertyName "retryPolicy" -NotePropertyValue "one-transient-primary-retry-then-replacement" -Force
    $aggregate | Add-Member -NotePropertyName "preflightEvidenceSha256" -NotePropertyValue $preflightEvidenceSha256 -Force
    Write-Utf8Json -Path $aggregatePath -Payload $aggregate -Depth 12
    if ($aggregateExitCode -ne 0 -or -not $aggregate.ok) {
        throw "Installed YouTube video matrix failed."
    }
} finally {
    $cleanupFailure = $null
    $strictUninstallVerified = $false
    $ephemeralCleanupVerified = $true
    try {
        Invoke-StrictUninstall -Root $InstallDir -TimeoutSec $InstallTimeoutSec
        $strictUninstallVerified = -not (Test-Path -LiteralPath $InstallDir)
    } catch {
        $cleanupFailure = $_
    }
    foreach ($cleanupPath in @($DataDir, $transientRoot)) {
        if ($cleanupPath -and (Test-Path -LiteralPath $cleanupPath)) {
            try {
                Remove-Item -LiteralPath $cleanupPath -Recurse -Force
            } catch {
                $ephemeralCleanupVerified = $false
                if (-not $cleanupFailure) {
                    $cleanupFailure = $_
                }
            }
            if (Test-Path -LiteralPath $cleanupPath) {
                $ephemeralCleanupVerified = $false
            }
        }
    }
    if ($aggregatePath -and (Test-Path -LiteralPath $aggregatePath -PathType Leaf)) {
        $finalAggregate = Get-Content -LiteralPath $aggregatePath -Raw | ConvertFrom-Json
        $finalAggregate | Add-Member -NotePropertyName "strictUninstallVerified" -NotePropertyValue $strictUninstallVerified -Force
        $finalAggregate | Add-Member -NotePropertyName "ephemeralCleanupVerified" -NotePropertyValue $ephemeralCleanupVerified -Force
        if (-not $strictUninstallVerified -or -not $ephemeralCleanupVerified) {
            $finalAggregate.ok = $false
            $cleanupFailures = @($finalAggregate.failures)
            if (-not $strictUninstallVerified) {
                $cleanupFailures += "strict uninstall was not verified"
            }
            if (-not $ephemeralCleanupVerified) {
                $cleanupFailures += "ephemeral matrix cleanup was not verified"
            }
            $finalAggregate.failures = $cleanupFailures
        }
        Write-Utf8Json -Path $aggregatePath -Payload $finalAggregate -Depth 12
    }
    if ($cleanupFailure) {
        throw $cleanupFailure
    }
}
