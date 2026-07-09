param(
    [switch]$SkipFullSuite
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($env:SCRIBER_PYTHON) { $env:SCRIBER_PYTHON } else { "python" }

& $python (Join-Path $repoRoot "scripts\perf\doctor.py") `
    --repo-root $repoRoot `
    --check-benchmark `
    --explain
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $python -m pytest tests\perf\test_windows_autoresearch_contract.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if (-not $SkipFullSuite) {
    & $python -m pytest tests\perf\test_hot_path_tracer.py tests\perf\test_golden_trace.py
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

exit 0
