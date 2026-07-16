<#
.SYNOPSIS
Builds and exports the exact Tauri application binary for a cold release path.
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$CacheKey,
    [string]$RepoRoot = "",
    [string]$CacheRoot = "build\cold-transfer\tauri-app-binary-cache",
    [string]$UpdaterPublicKey = "",
    [string]$UpdaterEndpoint = ""
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
$configPath = Join-Path $RepoRoot "build\tauri-release-config\tauri.generated.conf.json"
$logPath = Join-Path $RepoRoot "build\tauri-release-config\tauri-cold-binary.log"

function Quote-NativeArgument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

Push-Location $RepoRoot
try {
    & python scripts\sync_version.py
    if ($LASTEXITCODE -ne 0) { throw "Cold Tauri version synchronization failed." }
    $version = (& python -c "from scripts.create_release_metadata import read_version; print(read_version())").Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
        throw "Cold Tauri product version could not be resolved."
    }

    $configArgs = @(
        "scripts\prepare_tauri_updater_config.py",
        "--config", "Frontend\src-tauri\tauri.conf.json",
        "--output", $configPath,
        "--version", $version,
        "--remove-before-bundle-command",
        "--skip-signing-key-check",
        "--skip-updater-artifacts"
    )
    if ($UpdaterPublicKey) { $configArgs += @("--public-key", $UpdaterPublicKey) }
    if ($UpdaterEndpoint) { $configArgs += @("--endpoint", $UpdaterEndpoint) }
    & python @configArgs
    if ($LASTEXITCODE -ne 0) { throw "Cold Tauri updater-runtime configuration failed." }

    # Type checking and the no-bundle Rust compile are independent. Start the
    # compile through one fixed, checked-in -File command and use this process
    # for the type check, matching the existing release DAG without dynamic
    # PowerShell evaluation.
    $powershellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
    $prepareScript = Join-Path $RepoRoot "scripts\ci\prepare_tauri_app.ps1"
    $compileArguments = @(
        "-NoProfile", "-File", $prepareScript,
        "-Mode", "BuildBinary",
        "-RepoRoot", $RepoRoot,
        "-ConfigPath", $configPath,
        "-TauriLogPath", $logPath
    )
    $compileStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $compileStartInfo.FileName = $powershellExe
    $compileStartInfo.Arguments = (($compileArguments | ForEach-Object { Quote-NativeArgument ([string]$_) }) -join " ")
    $compileStartInfo.WorkingDirectory = $RepoRoot
    $compileStartInfo.UseShellExecute = $false
    $compileStartInfo.CreateNoWindow = $true
    $compileStartInfo.RedirectStandardOutput = $true
    $compileStartInfo.RedirectStandardError = $true
    $compileProcess = [System.Diagnostics.Process]::new()
    $compileProcess.StartInfo = $compileStartInfo
    if (-not $compileProcess.Start()) { throw "Cold Tauri application binary process did not start." }
    $compileStdout = $compileProcess.StandardOutput.ReadToEndAsync()
    $compileStderr = $compileProcess.StandardError.ReadToEndAsync()

    $typeCheckFailure = $null
    try {
        & powershell -NoProfile -File scripts\ci\prepare_tauri_app.ps1 -Mode TypeCheck -RepoRoot $RepoRoot
        if ($LASTEXITCODE -ne 0) { throw "Cold Tauri frontend type check failed." }
    } catch {
        $typeCheckFailure = $_
    }

    $compileProcess.WaitForExit()
    $compileOutput = $compileStdout.GetAwaiter().GetResult()
    $compileError = $compileStderr.GetAwaiter().GetResult()
    if ($compileOutput) { Write-Host $compileOutput.TrimEnd() }
    if ($compileError) { Write-Host $compileError.TrimEnd() }
    $compileExitCode = $compileProcess.ExitCode
    $compileProcess.Dispose()
    if ($null -ne $typeCheckFailure) { throw $typeCheckFailure }
    if ($compileExitCode -ne 0) { throw "Cold Tauri application binary build failed with exit code $compileExitCode." }

    & powershell -NoProfile -File scripts\ci\sync_tauri_app_binary_cache.ps1 `
        -Mode Export `
        -CacheKey $CacheKey `
        -Version $version `
        -CacheRoot $CacheRoot
    if ($LASTEXITCODE -ne 0) { throw "Cold Tauri application binary export failed." }
} finally {
    Pop-Location
}
