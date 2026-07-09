param(
    [string]$OutputPath = "benchmarks\results\raw\wpr.etl"
)

$ErrorActionPreference = "Stop"
Write-Output "WPR start is intentionally operator-controlled. Run Windows Performance Recorder manually for now."
Write-Output "Requested output: $OutputPath"
exit 2
