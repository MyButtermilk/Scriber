param(
    [switch]$Compact,
    [switch]$Report
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($env:SCRIBER_PYTHON) { $env:SCRIBER_PYTHON } else { "python" }
$mode = if ($Compact) { "state-compact" } else { "state-report" }
if ($Report) { $mode = "state-report" }

& $python (Join-Path $repoRoot "scripts\perf\autoresearch_state.py") `
    --repo-root $repoRoot `
    $mode
exit $LASTEXITCODE
