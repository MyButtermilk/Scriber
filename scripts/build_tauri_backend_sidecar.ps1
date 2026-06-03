<#
.SYNOPSIS
Builds the Python backend worker as a PyInstaller onedir sidecar for Tauri.

.DESCRIPTION
The generated sidecar can be launched by the Rust supervisor through
SCRIBER_BACKEND_EXE or by copying it next to the Tauri release executable under
target\release\backend.

Typical flow:
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -InstallPyInstaller -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -SkipBundledFfprobe -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -ValidateSlimMediaTools -MediaToolsDir path\to\slim-ffmpeg -CopyToTauriRelease
  cd Frontend
  npm run tauri:build
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonPath = "",
    [string]$DistRoot = "",
    [string]$WorkRoot = "",
    [string]$MediaToolsDir = "",
    [switch]$SkipFrontendBuild,
    [switch]$InstallPyInstaller,
    [switch]$BundleMediaTools,
    [switch]$SkipBundledFfprobe,
    [switch]$ValidateSlimMediaTools,
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

function Invoke-BackendRuntimeImportCheck {
    param(
        [string]$Python,
        [string]$Root
    )

    $checkScript = Join-Path $Root "scripts\check_backend_runtime_imports.py"
    if (-not (Test-Path $checkScript)) {
        throw "Missing backend runtime import check: $checkScript"
    }

    Push-Location $Root
    try {
        & $Python $checkScript
    } finally {
        Pop-Location
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Backend runtime dependency check failed. Install the standard build dependencies with: $Python -m pip install -r requirements-base.txt"
    }
}

function Invoke-FrozenBackendRuntimeImportCheck {
    param(
        [string]$SidecarExe,
        [string]$SidecarDir,
        [string]$LogRoot
    )

    $requiredPaths = @(
        "_internal\onnxruntime",
        "_internal\onnxruntime\capi"
    )
    $missingPaths = @()
    foreach ($relativePath in $requiredPaths) {
        $candidate = Join-Path $SidecarDir $relativePath
        if (-not (Test-Path $candidate)) {
            $missingPaths += $relativePath
        }
    }
    if ($missingPaths.Count -gt 0) {
        throw "Frozen backend sidecar is missing runtime dependencies: $($missingPaths -join ', ')"
    }

    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    $stdoutPath = Join-Path $LogRoot "frozen-runtime-import-check.out"
    $stderrPath = Join-Path $LogRoot "frozen-runtime-import-check.err"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

    $process = Start-Process `
        -FilePath $SidecarExe `
        -ArgumentList @("--runtime-import-check") `
        -WorkingDirectory $SidecarDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -Wait `
        -PassThru

    if ($process.ExitCode -ne 0) {
        $stdout = if (Test-Path $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        throw "Frozen backend runtime dependency check failed with exit code $($process.ExitCode). stdout: $stdout stderr: $stderr"
    }
}

function Resolve-MediaTool {
    param(
        [string[]]$Names,
        [string]$SearchDir
    )

    if ($SearchDir) {
        foreach ($name in $Names) {
            $candidate = Join-Path $SearchDir $name
            if (Test-Path $candidate) {
                return (Resolve-Path $candidate).Path
            }
        }
        return $null
    }

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and $command.Source -and (Test-Path $command.Source)) {
            return (Resolve-Path $command.Source).Path
        }
    }
    return $null
}

function Test-MediaToolExecutable {
    param(
        [string]$Path,
        [string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Name executable was not found: $Path"
    }

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $process = $null
    try {
        $process = Start-Process `
            -FilePath $Path `
            -ArgumentList @("-version") `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -Wait `
            -PassThru
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }

    if ($null -eq $process) {
        throw "$Name executable validation did not start: $Path"
    }
    if ($process.ExitCode -ne 0) {
        throw "$Name executable failed validation with exit code $($process.ExitCode): $Path"
    }
}

function Invoke-MediaToolText {
    param(
        [string]$Path,
        [string[]]$Arguments,
        [string]$Label
    )

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $process = $null
    try {
        $process = Start-Process `
            -FilePath $Path `
            -ArgumentList $Arguments `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -Wait `
            -PassThru

        $stdout = if (Test-Path $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }

    if ($null -eq $process) {
        throw "$Label validation did not start: $Path"
    }
    if ($process.ExitCode -ne 0) {
        throw "$Label validation failed with exit code $($process.ExitCode): $stderr"
    }

    return "$stdout`n$stderr"
}

function Assert-MediaToolOutputContains {
    param(
        [string]$Output,
        [string]$Needle,
        [string]$Label
    )

    if ($Output -notmatch [regex]::Escape($Needle)) {
        throw "Slim ffmpeg validation failed: missing $Label '$Needle'."
    }
}

function Test-ScriberFfmpegCapabilities {
    param([string]$Path)

    $encoders = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-encoders") -Label "ffmpeg encoder list"
    foreach ($encoder in @("libopus", "libmp3lame")) {
        Assert-MediaToolOutputContains -Output $encoders -Needle $encoder -Label "encoder"
    }

    $decoders = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-decoders") -Label "ffmpeg decoder list"
    foreach ($decoder in @("aac", "opus", "mp3")) {
        Assert-MediaToolOutputContains -Output $decoders -Needle $decoder -Label "decoder"
    }

    $demuxers = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-demuxers") -Label "ffmpeg demuxer list"
    foreach ($demuxer in @("matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2", "mp3", "wav")) {
        Assert-MediaToolOutputContains -Output $demuxers -Needle $demuxer -Label "demuxer"
    }

    $muxers = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-muxers") -Label "ffmpeg muxer list"
    foreach ($muxer in @("webm", "mp3")) {
        Assert-MediaToolOutputContains -Output $muxers -Needle $muxer -Label "muxer"
    }
}

function Copy-MediaTools {
    param(
        [string]$SidecarDir,
        [string]$SearchDir,
        [bool]$SkipFfprobe = $false,
        [bool]$ValidateSlimBundle = $false
    )

    $toolsTarget = Join-Path $SidecarDir "tools\ffmpeg"
    Assert-UnderRoot -Root $RepoRoot -Path $toolsTarget -Label "Bundled media tools target"
    New-Item -ItemType Directory -Force -Path $toolsTarget | Out-Null

    $copied = @()
    $ffmpeg = Resolve-MediaTool -Names @("ffmpeg.exe", "ffmpeg") -SearchDir $SearchDir
    if (-not $ffmpeg) {
        if ($SearchDir) {
            throw "ffmpeg was not found in MediaToolsDir: $SearchDir"
        }
        throw "ffmpeg was not found on PATH. Pass -MediaToolsDir or install ffmpeg before using -BundleMediaTools."
    }
    Copy-Item -LiteralPath $ffmpeg -Destination (Join-Path $toolsTarget (Split-Path $ffmpeg -Leaf)) -Force
    $copiedFfmpeg = Join-Path $toolsTarget (Split-Path $ffmpeg -Leaf)
    Test-MediaToolExecutable -Path $copiedFfmpeg -Name "ffmpeg"
    if ($ValidateSlimBundle) {
        Test-ScriberFfmpegCapabilities -Path $copiedFfmpeg
    }
    $copied += $copiedFfmpeg

    if (-not $SkipFfprobe) {
        $ffprobe = Resolve-MediaTool -Names @("ffprobe.exe", "ffprobe") -SearchDir $SearchDir
        if (-not $ffprobe) {
            if ($SearchDir) {
                throw "ffprobe was not found in MediaToolsDir: $SearchDir"
            }
            throw "ffprobe was not found on PATH. Install ffprobe or pass -SkipBundledFfprobe only for explicit slim-size experiments."
        }
        Copy-Item -LiteralPath $ffprobe -Destination (Join-Path $toolsTarget (Split-Path $ffprobe -Leaf)) -Force
        $copiedFfprobe = Join-Path $toolsTarget (Split-Path $ffprobe -Leaf)
        Test-MediaToolExecutable -Path $copiedFfprobe -Name "ffprobe"
        $copied += $copiedFfprobe
    } else {
        Write-Host "Skipping bundled ffprobe; packaged duration and stream probing will use env/system ffprobe or best-effort fallbacks."
    }

    $ytDlp = Resolve-MediaTool -Names @("yt-dlp.exe", "yt-dlp") -SearchDir $SearchDir
    if ($ytDlp) {
        Copy-Item -LiteralPath $ytDlp -Destination (Join-Path $toolsTarget (Split-Path $ytDlp -Leaf)) -Force
        $copied += (Join-Path $toolsTarget (Split-Path $ytDlp -Leaf))
    }

    return $copied
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
if ($MediaToolsDir) {
    $MediaToolsDir = (Resolve-Path $MediaToolsDir).Path
}
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

Invoke-BackendRuntimeImportCheck -Python $PythonPath -Root $RepoRoot

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

Invoke-FrozenBackendRuntimeImportCheck -SidecarExe $sidecarExe -SidecarDir $sidecarDir -LogRoot $WorkRoot

$mediaToolsCopied = @()
if ($BundleMediaTools -or $MediaToolsDir) {
    $mediaToolsCopied = @(Copy-MediaTools -SidecarDir $sidecarDir -SearchDir $MediaToolsDir -SkipFfprobe ([bool]$SkipBundledFfprobe) -ValidateSlimBundle ([bool]$ValidateSlimMediaTools))
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
    mediaToolsCopied = $mediaToolsCopied
    copiedToTauriRelease = $copiedTo
} | ConvertTo-Json -Compress
