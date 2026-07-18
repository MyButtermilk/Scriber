param(
    [switch]$CheckBenchmark,
    [switch]$Explain,
    [ValidateSet("ux", "installer-size")]
    [string]$Profile = "ux",
    [string]$RunId = "",
    [ValidateSet("prepare", "run", "finalize")]
    [string]$Phase = "prepare"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Profile -eq "installer-size") {
    if ($CheckBenchmark) {
        throw "-CheckBenchmark is only valid with -Profile ux."
    }
    if (-not $RunId) {
        throw "-Profile installer-size requires -RunId <canonical UUID>."
    }
    $installerArgs = @(
        (Join-Path $repoRoot "scripts\perf\installer_size\runner.py"),
        "--repo-root", $repoRoot,
        "--run-id", $RunId,
        "doctor",
        "--phase", $Phase
    )
    if ($Explain) {
        $installerArgs += "--explain"
    }
    & (Join-Path $repoRoot "scripts\project-python.cmd") @installerArgs
    exit $LASTEXITCODE
}
if ($RunId -or $PSBoundParameters.ContainsKey("Phase")) {
    throw "-RunId and -Phase are only valid with -Profile installer-size."
}
$python = if ($env:SCRIBER_PYTHON) { $env:SCRIBER_PYTHON } else { "python" }
$argsList = @((Join-Path $repoRoot "scripts\perf\doctor.py"), "--repo-root", $repoRoot)
if ($CheckBenchmark) {
    $argsList += "--check-benchmark"
}
if ($Explain) {
    $argsList += "--explain"
}

& $python @argsList
exit $LASTEXITCODE
