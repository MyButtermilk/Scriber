param(
    [switch]$Compact,
    [switch]$OperatorChecklist
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($env:SCRIBER_PYTHON) { $env:SCRIBER_PYTHON } else { "python" }
$argsList = @((Join-Path $repoRoot "scripts\perf\autoresearch_state.py"), "--repo-root", $repoRoot, "recommend-next")
if ($Compact) {
    $argsList += "--compact"
}
if ($OperatorChecklist) {
    $argsList += "--operator-checklist"
}

& $python @argsList
exit $LASTEXITCODE
