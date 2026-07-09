param(
    [switch]$CheckBenchmark,
    [switch]$Explain
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
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
