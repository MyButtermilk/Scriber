param(
    [string]$RuntimeDataDir,
    [switch]$ExitFixture
)

$ErrorActionPreference = 'Stop'
if ($ExitFixture) { exit 7 }
if (-not $RuntimeDataDir) { throw 'RuntimeDataDir is required.' }

Import-Module (Join-Path $PSScriptRoot '..\..\scripts\SmokeStartupDiagnostics.psm1') -Force
$env:SCRIBER_TEST_API_KEY = 'sentinel-env-key'
$current = Get-Process -Id $PID
$running = Get-SmokeStartupFailureDiagnostics -RuntimeDataDir $RuntimeDataDir -AppProcess $current -ManagedBackendCount 1 -Token 'sentinel-session-value'
$missing = Get-SmokeStartupFailureDiagnostics -RuntimeDataDir (Join-Path $RuntimeDataDir 'missing') -AppProcess $null -ManagedBackendCount 0
$child = Start-Process -FilePath $current.Path -ArgumentList @(
    '-NoProfile', '-NonInteractive', '-File', ('"{0}"' -f $PSCommandPath), '-ExitFixture'
) -WindowStyle Hidden -PassThru
$child.WaitForExit()
$exited = Get-SmokeStartupFailureDiagnostics -RuntimeDataDir $RuntimeDataDir -AppProcess $child -ManagedBackendCount 0 -Token 'sentinel-session-value'
$locked = [System.IO.File]::Open((Join-Path $RuntimeDataDir 'logs/tauri-shell.log'), 'Open', 'Read', 'None')
try {
    $unreadable = Get-SmokeStartupFailureDiagnostics -RuntimeDataDir $RuntimeDataDir -AppProcess $null -Token 'sentinel-session-value'
} finally { $locked.Dispose() }
@{running=$running; missing=$missing; exited=$exited; unreadable=$unreadable} | ConvertTo-Json -Depth 8 -Compress
