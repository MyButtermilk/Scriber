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
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -UseProfileBFfmpeg -ValidateSlimMediaTools -ReuseSidecarIfUnchanged -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -UseProfileBFfmpeg -ValidateSlimMediaTools -ReuseSidecarIfUnchanged -BundleRustAudioSidecar -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -UseGyanFfmpegEssentials -ValidateSlimMediaTools -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -SkipBundledFfprobe -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -ValidateSlimMediaTools -MediaToolsDir path\to\slim-ffmpeg -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -ReuseSidecarIfUnchanged -CopyToTauriRelease
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
    [switch]$UseProfileBFfmpeg,
    [switch]$UseGyanFfmpegEssentials,
    [switch]$SkipBundledFfprobe,
    [switch]$ValidateSlimMediaTools,
    [switch]$ReuseSidecarIfUnchanged,
    [string]$SidecarCacheRoot = "",
    [switch]$BundleRustAudioSidecar,
    [switch]$CopyToTauriRelease
)

$ErrorActionPreference = "Stop"
$script:BuildTimingStarted = [System.Diagnostics.Stopwatch]::StartNew()
$script:BuildTimingPhases = [System.Collections.Generic.List[object]]::new()

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

function Invoke-TimedStep {
    param(
        [string]$Label,
        [scriptblock]$Command
    )

    $stepWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $ok = $false
    try {
        & $Command
        $ok = $true
    } finally {
        $stepWatch.Stop()
        $script:BuildTimingPhases.Add([ordered]@{
            label = $Label
            durationMs = [int64]$stepWatch.ElapsedMilliseconds
            ok = $ok
        }) | Out-Null
    }
}

function Get-StringSha256 {
    param([string]$Value)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-RelativePath {
    param(
        [string]$Root,
        [string]$Path
    )

    $rootFull = Convert-ToFullPath -Path $Root
    if (-not $rootFull.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $rootFull += [System.IO.Path]::DirectorySeparatorChar
    }
    $pathFull = Convert-ToFullPath -Path $Path
    $rootUri = [System.Uri]::new($rootFull)
    $pathUri = [System.Uri]::new($pathFull)
    $relative = $rootUri.MakeRelativeUri($pathUri).ToString()
    return ([System.Uri]::UnescapeDataString($relative)).Replace("/", [System.IO.Path]::DirectorySeparatorChar)
}

function Get-FileHashEntry {
    param(
        [string]$Root,
        [string]$Path,
        [bool]$HashContent = $true
    )

    $item = Get-Item -LiteralPath $Path
    $entry = [ordered]@{
        path = (Get-RelativePath -Root $Root -Path $item.FullName)
        length = [int64]$item.Length
    }
    if ($HashContent) {
        $entry["sha256"] = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    } else {
        $entry["lastWriteTimeUtc"] = $item.LastWriteTimeUtc.ToString("o")
    }
    return $entry
}

function Get-InputFileEntries {
    param(
        [string]$Root,
        [string[]]$RelativePaths
    )

    $entries = @()
    foreach ($relative in $RelativePaths) {
        $candidate = Join-Path $Root $relative
        if (-not (Test-Path -LiteralPath $candidate)) {
            continue
        }
        $item = Get-Item -LiteralPath $candidate
        if ($item.PSIsContainer) {
            $files = Get-ChildItem -LiteralPath $item.FullName -Recurse -File |
                Where-Object {
                    $_.FullName -notmatch "\\__pycache__\\" -and
                    $_.Extension -notin @(".pyc", ".pyo")
                } |
                Sort-Object FullName
            foreach ($file in $files) {
                $entries += Get-FileHashEntry -Root $Root -Path $file.FullName
            }
        } else {
            $entries += Get-FileHashEntry -Root $Root -Path $item.FullName
        }
    }
    return $entries
}

function Get-ToolMetadataEntry {
    param(
        [string]$Root,
        [string]$Path,
        [string]$Name
    )

    if (-not $Path) {
        return [ordered]@{
            name = $Name
            path = $null
            exists = $false
        }
    }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        name = $Name
        path = (Get-RelativePath -Root $Root -Path $item.FullName)
        resolvedPath = $item.FullName
        exists = $true
        length = [int64]$item.Length
        lastWriteTimeUtc = $item.LastWriteTimeUtc.ToString("o")
    }
}

function Get-SidecarInputManifest {
    param(
        [string]$Root,
        [string]$Python,
        [string]$SearchDir,
        [bool]$BundleTools,
        [bool]$UseProfileB,
        [bool]$UseGyanEssentials,
        [bool]$SkipFfprobe,
        [bool]$ValidateSlimBundle
    )

    $pythonVersion = (& $Python -c "import sys; print(sys.version)" 2>$null) -join "`n"
    $pyInstallerVersion = (& $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null) -join "`n"
    $inputPaths = @(
        "src",
        "packaging\scriber-backend.spec",
        "requirements-base.txt",
        "pyloudnorm",
        "scripts\build_tauri_backend_sidecar.ps1",
        "scripts\check_backend_runtime_imports.py"
    )
    $tools = @()
    if ($BundleTools -or $SearchDir) {
        $tools += Get-ToolMetadataEntry -Root $Root -Path (Resolve-MediaTool -Names @("ffmpeg.exe", "ffmpeg") -SearchDir $SearchDir) -Name "ffmpeg"
        if (-not $SkipFfprobe) {
            $tools += Get-ToolMetadataEntry -Root $Root -Path (Resolve-MediaTool -Names @("ffprobe.exe", "ffprobe") -SearchDir $SearchDir) -Name "ffprobe"
        }
        $tools += Get-ToolMetadataEntry -Root $Root -Path (Resolve-MediaTool -Names @("yt-dlp.exe", "yt-dlp") -SearchDir $SearchDir) -Name "yt-dlp"
    }
    return [ordered]@{
        apiVersion = "1"
        python = $pythonVersion
        pyInstaller = $pyInstallerVersion
        flags = [ordered]@{
            bundleMediaTools = $BundleTools
            useProfileBFfmpeg = $UseProfileB
            useGyanFfmpegEssentials = $UseGyanEssentials
            skipBundledFfprobe = $SkipFfprobe
            validateSlimMediaTools = $ValidateSlimBundle
        }
        files = Get-InputFileEntries -Root $Root -RelativePaths $inputPaths
        tools = $tools
    }
}

function Copy-DirectoryContents {
    param(
        [string]$SourceDir,
        [string]$TargetDir,
        [string]$TargetLabel
    )

    Assert-UnderRoot -Root $RepoRoot -Path $TargetDir -Label $TargetLabel
    if (Test-Path -LiteralPath $TargetDir) {
        Remove-Item -LiteralPath $TargetDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
    Copy-Item -Path (Join-Path $SourceDir "*") -Destination $TargetDir -Recurse -Force
}

function Copy-RustAudioSidecarToTauriRelease {
    param(
        [string]$Root
    )

    $tauriDir = Join-Path $Root "Frontend\src-tauri"
    $targetDir = Join-Path $tauriDir "resources\audio-sidecar"
    Assert-UnderRoot -Root $Root -Path $targetDir -Label "Tauri release audio sidecar target"

    Push-Location $tauriDir
    try {
        cargo build --release --bin scriber-audio-sidecar
    } finally {
        Pop-Location
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Rust audio sidecar build failed."
    }

    $exeName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "scriber-audio-sidecar.exe" } else { "scriber-audio-sidecar" }
    $sourceExe = Join-Path $tauriDir "target\release\$exeName"
    if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) {
        throw "Rust audio sidecar executable was not found: $sourceExe"
    }

    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    Get-ChildItem -LiteralPath $targetDir -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne ".gitkeep" } |
        Remove-Item -Recurse -Force
    $targetExe = Join-Path $targetDir $exeName
    Copy-Item -LiteralPath $sourceExe -Destination $targetExe -Force

    $metadata = [ordered]@{
        apiVersion = "1"
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        sourceExe = $sourceExe
        targetExe = $targetExe
        sha256 = (Get-FileHash -LiteralPath $targetExe -Algorithm SHA256).Hash.ToLowerInvariant()
        length = [int64](Get-Item -LiteralPath $targetExe).Length
        captureDefault = "disabled"
        optInEnv = "SCRIBER_RUST_AUDIO_WASAPI_CAPTURE"
    }
    $metadataPath = Join-Path $targetDir "audio-sidecar-build-metadata.json"
    $metadata | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $metadataPath -Encoding utf8

    return [ordered]@{
        targetDir = $targetDir
        targetExe = $targetExe
        metadataPath = $metadataPath
        sha256 = $metadata.sha256
        length = $metadata.length
    }
}

function Write-SidecarBuildMetadata {
    param(
        [string]$SidecarDir,
        [string]$SidecarExe,
        [bool]$CacheEnabled,
        [bool]$CacheHit,
        [string]$CacheKey,
        [object]$PreparedMediaTools,
        [object[]]$MediaToolsCopied,
        [object]$RustAudioSidecarCopied,
        [string]$CopiedTo
    )

    $script:BuildTimingStarted.Stop()
    $metadata = [ordered]@{
        apiVersion = "1"
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        sidecarDir = $SidecarDir
        sidecarExe = $SidecarExe
        copiedToTauriRelease = $CopiedTo
        cache = [ordered]@{
            enabled = $CacheEnabled
            hit = $CacheHit
            key = $CacheKey
        }
        flags = [ordered]@{
            bundleMediaTools = [bool]$BundleMediaTools
            useProfileBFfmpeg = [bool]$UseProfileBFfmpeg
            useGyanFfmpegEssentials = [bool]$UseGyanFfmpegEssentials
            skipBundledFfprobe = [bool]$SkipBundledFfprobe
            validateSlimMediaTools = [bool]$ValidateSlimMediaTools
            bundleRustAudioSidecar = [bool]$BundleRustAudioSidecar
        }
        preparedMediaTools = $PreparedMediaTools
        mediaToolsCopied = $MediaToolsCopied
        rustAudioSidecarCopied = $RustAudioSidecarCopied
        totalDurationMs = [int64]$script:BuildTimingStarted.ElapsedMilliseconds
        phases = @($script:BuildTimingPhases)
    }
    $metadataPath = Join-Path $SidecarDir "sidecar-build-metadata.json"
    $metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $metadataPath -Encoding utf8
    return $metadataPath
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
    foreach ($encoder in @("libopus", "libmp3lame", "pcm_s16le")) {
        Assert-MediaToolOutputContains -Output $encoders -Needle $encoder -Label "encoder"
    }

    $decoders = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-decoders") -Label "ffmpeg decoder list"
    foreach ($decoder in @("aac", "opus", "mp3", "flac", "alac")) {
        Assert-MediaToolOutputContains -Output $decoders -Needle $decoder -Label "decoder"
    }

    $demuxers = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-demuxers") -Label "ffmpeg demuxer list"
    foreach ($demuxer in @("matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2", "mp3", "wav", "ogg", "flac", "s16le")) {
        Assert-MediaToolOutputContains -Output $demuxers -Needle $demuxer -Label "demuxer"
    }

    $muxers = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-muxers") -Label "ffmpeg muxer list"
    foreach ($muxer in @("webm", "mp3", "s16le")) {
        Assert-MediaToolOutputContains -Output $muxers -Needle $muxer -Label "muxer"
    }

    $protocols = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-protocols") -Label "ffmpeg protocol list"
    foreach ($protocol in @("file", "pipe")) {
        Assert-MediaToolOutputContains -Output $protocols -Needle $protocol -Label "protocol"
    }
}

function Invoke-ScriberFfmpegProfileManifest {
    param(
        [string]$FfmpegPath,
        [string]$FfprobePath,
        [string]$OutputPath
    )

    $validator = Join-Path $RepoRoot "scripts\ffmpeg\validate_ffmpeg_profile.py"
    if (-not (Test-Path -LiteralPath $validator -PathType Leaf)) {
        throw "Missing FFmpeg profile validator: $validator"
    }

    $validatorArgs = @(
        $validator,
        "--ffmpeg",
        $FfmpegPath,
        "--profile",
        "B",
        "--output",
        $OutputPath
    )
    if ($FfprobePath) {
        $validatorArgs += @("--ffprobe", $FfprobePath)
    }

    & $PythonPath @validatorArgs
    if ($LASTEXITCODE -ne 0) {
        throw "FFmpeg profile validation failed. See manifest: $OutputPath"
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
    if ($SearchDir) {
        foreach ($dll in Get-ChildItem -LiteralPath $SearchDir -Filter "*.dll" -File -ErrorAction SilentlyContinue) {
            $targetDll = Join-Path $toolsTarget $dll.Name
            Copy-Item -LiteralPath $dll.FullName -Destination $targetDll -Force
            $copied += $targetDll
        }
    }

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

    $copiedFfprobe = ""
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

    if ($ValidateSlimBundle) {
        $profileManifestPath = Join-Path $toolsTarget "ffmpeg-profile-manifest.json"
        Invoke-ScriberFfmpegProfileManifest -FfmpegPath $copiedFfmpeg -FfprobePath $copiedFfprobe -OutputPath $profileManifestPath
        $copied += $profileManifestPath
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
if (-not $SidecarCacheRoot) {
    $SidecarCacheRoot = Join-Path $RepoRoot "build\tauri-sidecar-cache"
}
$DistRoot = Convert-ToFullPath -Path $DistRoot
$WorkRoot = Convert-ToFullPath -Path $WorkRoot
$SidecarCacheRoot = Convert-ToFullPath -Path $SidecarCacheRoot
if ($MediaToolsDir) {
    $MediaToolsDir = (Resolve-Path $MediaToolsDir).Path
}
$SpecPath = Join-Path $RepoRoot "packaging\scriber-backend.spec"

Assert-UnderRoot -Root $RepoRoot -Path $DistRoot -Label "DistRoot"
Assert-UnderRoot -Root $RepoRoot -Path $WorkRoot -Label "WorkRoot"
Assert-UnderRoot -Root $RepoRoot -Path $SidecarCacheRoot -Label "SidecarCacheRoot"

if (-not (Test-Path $SpecPath)) {
    throw "Missing PyInstaller spec: $SpecPath"
}

if ($UseProfileBFfmpeg -and $UseGyanFfmpegEssentials) {
    throw "Use either -UseProfileBFfmpeg or -UseGyanFfmpegEssentials, not both."
}

$preparedMediaTools = $null
if ($UseProfileBFfmpeg -and -not $MediaToolsDir) {
    Invoke-TimedStep -Label "prepare-profile-b-ffmpeg" -Command {
        $profileBuildScript = Join-Path $RepoRoot "scripts\ffmpeg\build_profile_b_msys2.ps1"
        if (-not (Test-Path -LiteralPath $profileBuildScript -PathType Leaf)) {
            throw "Missing Profile B FFmpeg build script: $profileBuildScript"
        }
        $profileBuildRoot = Join-Path $RepoRoot "build\ffmpeg-profile-b-msys2"
        $profileReportPath = Join-Path $profileBuildRoot "profile-b-msys2-build-report.json"
        $script:PreparedProfileBReportPath = $profileReportPath
        $script:PreparedProfileBReused = $false
        $script:PreparedProfileBReport = $null
        $script:PreparedProfileBMediaToolsDir = ""

        if (Test-Path -LiteralPath $profileReportPath -PathType Leaf) {
            try {
                $existingReport = Get-Content -LiteralPath $profileReportPath -Raw | ConvertFrom-Json
                $existingMediaToolsDir = [string]($existingReport.mediaToolsDir)
                if (
                    $existingReport.ok -and
                    $existingMediaToolsDir -and
                    (Test-Path -LiteralPath (Join-Path $existingMediaToolsDir "ffmpeg.exe") -PathType Leaf) -and
                    (Test-Path -LiteralPath (Join-Path $existingMediaToolsDir "ffprobe.exe") -PathType Leaf)
                ) {
                    $script:PreparedProfileBReport = $existingReport
                    $script:PreparedProfileBMediaToolsDir = (Resolve-Path $existingMediaToolsDir).Path
                    $script:PreparedProfileBReused = $true
                }
            } catch {
                Write-Host "Ignoring unreadable Profile B build report: $profileReportPath"
            }
        }

        if (-not $script:PreparedProfileBMediaToolsDir) {
            & $profileBuildScript -RepoRoot $RepoRoot -BuildRoot $profileBuildRoot -InstallDependencies
            if ($LASTEXITCODE -ne 0) {
                throw "FFmpeg Profile B build failed with exit code $LASTEXITCODE."
            }
            if (-not (Test-Path -LiteralPath $profileReportPath -PathType Leaf)) {
                throw "FFmpeg Profile B build did not write report: $profileReportPath"
            }
            $buildReport = Get-Content -LiteralPath $profileReportPath -Raw | ConvertFrom-Json
            if (-not $buildReport.ok) {
                throw "FFmpeg Profile B build report did not report ok=true."
            }
            $script:PreparedProfileBReport = $buildReport
            $script:PreparedProfileBMediaToolsDir = (Resolve-Path ([string]$buildReport.mediaToolsDir)).Path
        }

        Test-MediaToolExecutable -Path (Join-Path $script:PreparedProfileBMediaToolsDir "ffmpeg.exe") -Name "Profile B ffmpeg"
        Test-MediaToolExecutable -Path (Join-Path $script:PreparedProfileBMediaToolsDir "ffprobe.exe") -Name "Profile B ffprobe"
    }
    $preparedMediaTools = [ordered]@{
        ok = $true
        kind = "profile-b"
        reused = [bool]$script:PreparedProfileBReused
        mediaToolsDir = $script:PreparedProfileBMediaToolsDir
        report = $script:PreparedProfileBReportPath
    }
    $MediaToolsDir = $script:PreparedProfileBMediaToolsDir
    $ValidateSlimMediaTools = $true
} elseif ($UseProfileBFfmpeg -and $MediaToolsDir) {
    Write-Host "Using explicit MediaToolsDir; skipping Profile B FFmpeg preparation."
    $ValidateSlimMediaTools = $true
} elseif ($UseGyanFfmpegEssentials -and -not $MediaToolsDir) {
    Invoke-TimedStep -Label "prepare-gyan-ffmpeg-essentials" -Command {
        $prepareScript = Join-Path $RepoRoot "scripts\prepare_gyan_ffmpeg_essentials.ps1"
        if (-not (Test-Path -LiteralPath $prepareScript -PathType Leaf)) {
            throw "Missing Gyan FFmpeg essentials prepare script: $prepareScript"
        }
        $prepareOutput = & $prepareScript -RepoRoot $RepoRoot
        $prepareJson = ($prepareOutput | Out-String).Trim()
        if (-not $prepareJson) {
            throw "Gyan FFmpeg essentials prepare script produced no JSON output."
        }
        $script:PreparedMediaTools = $prepareJson | ConvertFrom-Json
        if (-not $script:PreparedMediaTools.ok) {
            throw "Gyan FFmpeg essentials preparation did not report ok=true."
        }
        $script:PreparedMediaToolsDir = (Resolve-Path $script:PreparedMediaTools.mediaToolsDir).Path
    }
    $preparedMediaTools = $script:PreparedMediaTools
    $MediaToolsDir = $script:PreparedMediaToolsDir
    $ValidateSlimMediaTools = $true
} elseif ($UseGyanFfmpegEssentials -and $MediaToolsDir) {
    Write-Host "Using explicit MediaToolsDir; skipping Gyan FFmpeg essentials download."
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

Invoke-TimedStep -Label "backend-runtime-import-check" -Command {
    Invoke-BackendRuntimeImportCheck -Python $PythonPath -Root $RepoRoot
}

if (-not $SkipFrontendBuild) {
    Invoke-TimedStep -Label "frontend-build" -Command {
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
}

New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null

$cacheEnabled = [bool]$ReuseSidecarIfUnchanged
$cacheHit = $false
$cacheKey = $null
$cacheDir = $null

if ($cacheEnabled) {
    Invoke-TimedStep -Label "sidecar-cache-key" -Command {
        $inputManifest = Get-SidecarInputManifest `
            -Root $RepoRoot `
            -Python $PythonPath `
            -SearchDir $MediaToolsDir `
            -BundleTools ([bool]$BundleMediaTools) `
            -UseProfileB ([bool]$UseProfileBFfmpeg) `
            -UseGyanEssentials ([bool]$UseGyanFfmpegEssentials) `
            -SkipFfprobe ([bool]$SkipBundledFfprobe) `
            -ValidateSlimBundle ([bool]$ValidateSlimMediaTools)
        $inputManifestJson = $inputManifest | ConvertTo-Json -Depth 8 -Compress
        $script:SidecarInputManifest = $inputManifest
        $script:SidecarInputManifestJson = $inputManifestJson
        $script:SidecarCacheKey = Get-StringSha256 -Value $inputManifestJson
    }
    $cacheKey = $script:SidecarCacheKey
    $cacheDir = Join-Path $SidecarCacheRoot $cacheKey
    $cachedSidecarDir = Join-Path $cacheDir "scriber-backend"
    $cachedSidecarExe = Join-Path $cachedSidecarDir "scriber-backend.exe"
    if ((Test-Path -LiteralPath $cachedSidecarExe -PathType Leaf) -and (Test-Path -LiteralPath (Join-Path $cacheDir "cache-manifest.json") -PathType Leaf)) {
        Invoke-TimedStep -Label "sidecar-cache-restore" -Command {
            Copy-DirectoryContents -SourceDir $cachedSidecarDir -TargetDir (Join-Path $DistRoot "scriber-backend") -TargetLabel "Restored sidecar dist target"
        }
        $cacheHit = $true
    }
}

if (-not $cacheHit) {
    Invoke-TimedStep -Label "pyinstaller-build" -Command {
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
    }
}

$sidecarDir = Join-Path $DistRoot "scriber-backend"
$sidecarExe = Join-Path $sidecarDir "scriber-backend.exe"
if (-not (Test-Path $sidecarExe)) {
    $sidecarExe = Join-Path $sidecarDir "scriber-backend"
}
if (-not (Test-Path $sidecarExe)) {
    throw "Sidecar build completed but executable was not found under $sidecarDir."
}

Invoke-TimedStep -Label "frozen-runtime-import-check" -Command {
    Invoke-FrozenBackendRuntimeImportCheck -SidecarExe $sidecarExe -SidecarDir $sidecarDir -LogRoot $WorkRoot
}

$mediaToolsCopied = @()
if (-not $cacheHit -and ($BundleMediaTools -or $MediaToolsDir)) {
    Invoke-TimedStep -Label "media-tools-copy" -Command {
        $script:MediaToolsCopied = @(Copy-MediaTools -SidecarDir $sidecarDir -SearchDir $MediaToolsDir -SkipFfprobe ([bool]$SkipBundledFfprobe) -ValidateSlimBundle ([bool]$ValidateSlimMediaTools))
    }
    $mediaToolsCopied = @($script:MediaToolsCopied)
} elseif ($cacheHit) {
    $toolsDir = Join-Path $sidecarDir "tools\ffmpeg"
    if (Test-Path -LiteralPath $toolsDir -PathType Container) {
        $mediaToolsCopied = @(Get-ChildItem -LiteralPath $toolsDir -File | Select-Object -ExpandProperty FullName)
    }
}

if ($cacheEnabled -and -not $cacheHit) {
    Invoke-TimedStep -Label "sidecar-cache-save" -Command {
        New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
        Copy-DirectoryContents -SourceDir $sidecarDir -TargetDir (Join-Path $cacheDir "scriber-backend") -TargetLabel "Sidecar cache target"
        $cacheManifest = [ordered]@{
            apiVersion = "1"
            generatedAt = (Get-Date).ToUniversalTime().ToString("o")
            cacheKey = $cacheKey
            inputManifest = $script:SidecarInputManifest
        }
        $cacheManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $cacheDir "cache-manifest.json") -Encoding utf8
    }
}

$copiedTo = $null
$metadataPath = $null
$rustAudioSidecarCopied = $null

if ($CopyToTauriRelease) {
    Invoke-TimedStep -Label "copy-to-tauri-release" -Command {
        $targetDir = Join-Path $RepoRoot "Frontend\src-tauri\target\release\backend"
        Copy-DirectoryContents -SourceDir $sidecarDir -TargetDir $targetDir -TargetLabel "Tauri release backend target"
        $script:CopiedToTauriRelease = $targetDir
    }
    $copiedTo = $script:CopiedToTauriRelease
}

if ($BundleRustAudioSidecar) {
    Invoke-TimedStep -Label "rust-audio-sidecar-build" -Command {
        $script:RustAudioSidecarCopied = Copy-RustAudioSidecarToTauriRelease -Root $RepoRoot
    }
    $rustAudioSidecarCopied = $script:RustAudioSidecarCopied
}

if ($CopyToTauriRelease) {
    $metadataPath = Write-SidecarBuildMetadata `
        -SidecarDir $sidecarDir `
        -SidecarExe $sidecarExe `
        -CacheEnabled $cacheEnabled `
        -CacheHit $cacheHit `
        -CacheKey $cacheKey `
        -PreparedMediaTools $preparedMediaTools `
        -MediaToolsCopied $mediaToolsCopied `
        -RustAudioSidecarCopied $rustAudioSidecarCopied `
        -CopiedTo $copiedTo
    if (Test-Path -LiteralPath $copiedTo -PathType Container) {
        Copy-Item -LiteralPath $metadataPath -Destination (Join-Path $copiedTo "sidecar-build-metadata.json") -Force
    }
} else {
    $metadataPath = Write-SidecarBuildMetadata `
        -SidecarDir $sidecarDir `
        -SidecarExe $sidecarExe `
        -CacheEnabled $cacheEnabled `
        -CacheHit $cacheHit `
        -CacheKey $cacheKey `
        -PreparedMediaTools $preparedMediaTools `
        -MediaToolsCopied $mediaToolsCopied `
        -RustAudioSidecarCopied $rustAudioSidecarCopied `
        -CopiedTo $copiedTo
}

[pscustomobject]@{
    ok = $true
    sidecarDir = $sidecarDir
    sidecarExe = $sidecarExe
    cacheEnabled = $cacheEnabled
    cacheHit = $cacheHit
    cacheKey = $cacheKey
    mediaToolsCopied = $mediaToolsCopied
    rustAudioSidecarCopied = $rustAudioSidecarCopied
    sidecarBuildMetadata = $metadataPath
    copiedToTauriRelease = $copiedTo
} | ConvertTo-Json -Compress
