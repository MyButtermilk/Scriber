param(
    [switch]$Compact
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($env:SCRIBER_PYTHON) { $env:SCRIBER_PYTHON } else { "python" }
$argsList = @((Join-Path $repoRoot "scripts\perf\autoresearch_state.py"), "--repo-root", $repoRoot, "onboarding-packet")
if ($Compact) {
    $argsList += "--compact"
}

& $python @argsList
exit $LASTEXITCODE
