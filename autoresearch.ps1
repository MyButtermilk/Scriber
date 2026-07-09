param(
    [ValidateSet("Smoke", "FastLocal", "FullLocal", "LiveMicrosoft", "LiveSoniox")]
    [string]$Suite = "FastLocal",
    [string]$InstallRoot = "",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$argsList = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $repoRoot "scripts\perf\run.ps1"),
    "-Suite",
    $Suite
)
if ($InstallRoot) {
    $argsList += @("-InstallRoot", $InstallRoot)
}
if ($OutputDir) {
    $argsList += @("-OutputDir", $OutputDir)
}

& powershell.exe @argsList

exit $LASTEXITCODE
