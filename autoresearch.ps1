param(
    [ValidateSet("Smoke", "FastLocal", "FullLocal", "LiveMicrosoft", "LiveSoniox")]
    [string]$Suite = "FastLocal",
    [string]$InstallRoot = "",
    [string]$OutputDir = "",
    [ValidateSet("ux", "installer-size")]
    [string]$Profile = "ux",
    [ValidateRange(1, 2147483647)]
    [int]$DurationSeconds = 43200,
    [string]$RunId = "",
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Profile -eq "installer-size") {
    if ($PSBoundParameters.ContainsKey("Suite") -or $InstallRoot -or $OutputDir) {
        throw "-Suite, -InstallRoot, and -OutputDir are only valid with -Profile ux."
    }
    if (-not $RunId) {
        throw "-Profile installer-size requires -RunId <canonical UUID>."
    }
    $runnerArgs = @(
        (Join-Path $repoRoot "scripts\perf\installer_size\runner.py"),
        "--repo-root", $repoRoot,
        "--run-id", $RunId
    )
    if ($PSBoundParameters.ContainsKey("DurationSeconds")) {
        $runnerArgs += @("--duration-seconds", [string]$DurationSeconds)
    }
    $runnerArgs += "start"
    if ($Resume) {
        $runnerArgs += "--resume"
    }
    & (Join-Path $repoRoot "scripts\project-python.cmd") @runnerArgs
    exit $LASTEXITCODE
}
if ($RunId -or $Resume -or $PSBoundParameters.ContainsKey("DurationSeconds")) {
    throw "-RunId, -DurationSeconds, and -Resume are only valid with -Profile installer-size."
}
$argsList = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $repoRoot "scripts\perf\run.ps1"),
    "-Suite",
    $Suite
)
if ($InstallRoot) {
    $argsList += @("-InstallRoot", $InstallRoot)
}
if ($OutputDir) {
    $argsList += @("-OutputDir", $OutputDir)
}

& powershell.exe @argsList

exit $LASTEXITCODE
