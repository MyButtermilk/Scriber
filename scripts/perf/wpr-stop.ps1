param(
    [string]$OutputPath = "benchmarks\results\raw\wpr.etl"
)

$ErrorActionPreference = "Stop"
Write-Output "WPR stop is intentionally operator-controlled. No active WPR session is controlled by this harness."
Write-Output "Requested output: $OutputPath"
exit 2
