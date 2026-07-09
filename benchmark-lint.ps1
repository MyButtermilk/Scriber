param(
    [string]$InputPath = "",
    [switch]$AllowUnknown
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($env:SCRIBER_PYTHON) { $env:SCRIBER_PYTHON } else { "python" }
$argsList = @((Join-Path $repoRoot "scripts\perf\benchmark_lint.py"))
if ($InputPath) {
    $argsList += @("--input", $InputPath)
}
if ($AllowUnknown) {
    $argsList += "--allow-unknown"
}

& $python @argsList
exit $LASTEXITCODE
