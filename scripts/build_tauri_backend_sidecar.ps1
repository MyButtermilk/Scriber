<#
.SYNOPSIS
Builds the Python backend worker as a PyInstaller onedir sidecar for Tauri.

.DESCRIPTION
The generated sidecar can be launched by the Rust supervisor through
SCRIBER_BACKEND_EXE or by copying it next to the Tauri release executable under
target\release\backend.

Typical flow:
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -InstallPyInstaller -CopyToTauriRelease
  cd Frontend
  npm run tauri:build
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonPath = "",
    [string]$DistRoot = "",
    [string]$WorkRoot = "",
    [switch]$SkipFrontendBuild,
    [switch]$InstallPyInstaller,
    [switch]$CopyToTauriRelease
)

$ErrorActionPreference = "Stop"

function Convert-ToFullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-UnderRoot {
    param(
        [string]$Root,
        [string]$Path,
        [string]$Label
    )

    $rootFull = Convert-ToFullPath -Path $Root
    $pathFull = Convert-ToFullPath -Path $Path
    if (-not $pathFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under repo root. Got: $pathFull"
    }
}

function Resolve-PythonPath {
    param([string]$Root, [string]$Requested)

    if ($Requested -and (Test-Path $Requested)) {
        return (Resolve-Path $Requested).Path
    }

    $candidates = @(
        (Join-Path $Root "venv\Scripts\python.exe"),
        (Join-Path $Root ".venv\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    return "python"
}

function Test-PyInstaller {
    param([string]$Python)

    & $Python -c "import PyInstaller" 2>$null
    return $LASTEXITCODE -eq 0
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
$PythonPath = Resolve-PythonPath -Root $RepoRoot -Requested $PythonPath
if (-not $DistRoot) {
    $DistRoot = Join-Path $RepoRoot "dist\tauri-sidecar"
}
if (-not $WorkRoot) {
    $WorkRoot = Join-Path $RepoRoot "build\tauri-sidecar"
}
$DistRoot = Convert-ToFullPath -Path $DistRoot
$WorkRoot = Convert-ToFullPath -Path $WorkRoot
$SpecPath = Join-Path $RepoRoot "packaging\scriber-backend.spec"

Assert-UnderRoot -Root $RepoRoot -Path $DistRoot -Label "DistRoot"
Assert-UnderRoot -Root $RepoRoot -Path $WorkRoot -Label "WorkRoot"

if (-not (Test-Path $SpecPath)) {
    throw "Missing PyInstaller spec: $SpecPath"
}

if (-not (Test-PyInstaller -Python $PythonPath)) {
    if (-not $InstallPyInstaller) {
        throw "PyInstaller is not installed for $PythonPath. Re-run with -InstallPyInstaller or install pyinstaller manually."
    }
    & $PythonPath -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyInstaller."
    }
}

if (-not $SkipFrontendBuild) {
    Push-Location (Join-Path $RepoRoot "Frontend")
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed."
        }
    } finally {
        Pop-Location
    }
}

New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null

$oldRepoRoot = $env:SCRIBER_REPO_ROOT
$env:SCRIBER_REPO_ROOT = $RepoRoot
try {
    Push-Location $RepoRoot
    try {
        & $PythonPath -m PyInstaller --noconfirm --clean --distpath $DistRoot --workpath $WorkRoot $SpecPath
    } finally {
        Pop-Location
    }
} finally {
    $env:SCRIBER_REPO_ROOT = $oldRepoRoot
}
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller sidecar build failed."
}

$sidecarDir = Join-Path $DistRoot "scriber-backend"
$sidecarExe = Join-Path $sidecarDir "scriber-backend.exe"
if (-not (Test-Path $sidecarExe)) {
    $sidecarExe = Join-Path $sidecarDir "scriber-backend"
}
if (-not (Test-Path $sidecarExe)) {
    throw "Sidecar build completed but executable was not found under $sidecarDir."
}

$copiedTo = $null
if ($CopyToTauriRelease) {
    $targetDir = Join-Path $RepoRoot "Frontend\src-tauri\target\release\backend"
    Assert-UnderRoot -Root $RepoRoot -Path $targetDir -Label "Tauri release backend target"
    if (Test-Path $targetDir) {
        Remove-Item -LiteralPath $targetDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    Copy-Item -Path (Join-Path $sidecarDir "*") -Destination $targetDir -Recurse -Force
    $copiedTo = $targetDir
}

[pscustomobject]@{
    ok = $true
    sidecarDir = $sidecarDir
    sidecarExe = $sidecarExe
    copiedToTauriRelease = $copiedTo
} | ConvertTo-Json -Compress
