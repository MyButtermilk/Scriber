param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($env:SCRIBER_PYTHON) { $env:SCRIBER_PYTHON } else { "python" }

& $python (Join-Path $repoRoot "scripts\perf\autoresearch_state.py") `
    --repo-root $repoRoot `
    finalize-preview
exit $LASTEXITCODE
