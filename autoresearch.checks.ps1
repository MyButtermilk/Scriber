param(
    [switch]$SkipFullSuite,
    [ValidateSet("ux", "installer-size")]
    [string]$Profile = "ux",
    [string]$RunId = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Profile -eq "installer-size") {
    $tests = @("tests\perf\test_installer_size_autoresearch_profile.py")
    foreach ($candidate in @(
        "tests\test_installer_research_inventory.py",
        "tests\test_installer_research_comparator.py",
        "tests\test_installer_research_timing.py",
        "tests\test_installer_research_environment.py"
    )) {
        if (Test-Path -LiteralPath (Join-Path $repoRoot $candidate) -PathType Leaf) {
            $tests += $candidate
        }
    }
    & (Join-Path $repoRoot "scripts\project-python.cmd") -m pytest -q @tests
    exit $LASTEXITCODE
}
if ($RunId) {
    throw "-RunId is only valid with -Profile installer-size."
}
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
