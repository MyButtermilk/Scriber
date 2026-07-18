param(
    [switch]$Compact,
    [ValidateSet("ux", "installer-size")]
    [string]$Profile = "ux",
    [string]$RunId = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Profile -eq "installer-size") {
    if (-not $RunId) {
        throw "-Profile installer-size requires -RunId <canonical UUID>."
    }
    $installerArgs = @(
        (Join-Path $repoRoot "scripts\perf\installer_size\runner.py"),
        "--repo-root", $repoRoot,
        "--run-id", $RunId,
        "onboarding-packet"
    )
    if ($Compact) {
        $installerArgs += "--compact"
    }
    & (Join-Path $repoRoot "scripts\project-python.cmd") @installerArgs
    exit $LASTEXITCODE
}
if ($RunId) {
    throw "-RunId is only valid with -Profile installer-size."
}
$python = if ($env:SCRIBER_PYTHON) { $env:SCRIBER_PYTHON } else { "python" }
$argsList = @((Join-Path $repoRoot "scripts\perf\autoresearch_state.py"), "--repo-root", $repoRoot, "onboarding-packet")
if ($Compact) {
    $argsList += "--compact"
}

& $python @argsList
exit $LASTEXITCODE
