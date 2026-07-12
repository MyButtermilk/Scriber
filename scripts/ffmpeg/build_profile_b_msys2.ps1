<#
.SYNOPSIS
Builds the Scriber FFmpeg Profile B candidate with MSYS2/UCRT64.

.DESCRIPTION
This script turns the generated Profile B build kit into an actual Windows
FFmpeg/ffprobe binary pair. It expects MSYS2 to be installed locally and can
optionally install the required UCRT64 packages with pacman. After the build it
runs the Profile B manifest gate, fixture smoke, and media-preparation smoke
against the produced binaries.
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$BuildRoot = "",
    [string]$Msys2Root = "",
    [string]$SourceUrl = "https://git.ffmpeg.org/ffmpeg.git",
    [string]$GitRef = "n7.0",
    [string]$PythonExe = "python",
    [int]$Jobs = 0,
    [switch]$InstallDependencies,
    [switch]$SkipClone,
    [switch]$ForceClean,
    [switch]$RunSidecarGate,
    [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RequiredMsys2Packages = @(
    "git",
    "make",
    "diffutils",
    "pkgconf",
    "mingw-w64-ucrt-x86_64-gcc",
    "mingw-w64-ucrt-x86_64-pkgconf",
    "mingw-w64-ucrt-x86_64-nasm",
    "mingw-w64-ucrt-x86_64-opus",
    "mingw-w64-ucrt-x86_64-lame",
    "mingw-w64-ucrt-x86_64-ffmpeg"
)

function Convert-ToFullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Write-Utf8NoBomJson {
    param(
        [string]$Path,
        [object]$Payload
    )

    $json = $Payload | ConvertTo-Json -Depth 8
    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $json + "`n", [System.Text.UTF8Encoding]::new($false))
}

function Find-Msys2Root {
    param([string]$RequestedRoot)

    $candidates = @()
    if ($RequestedRoot) {
        $candidates += $RequestedRoot
    }
    if ($env:MSYS2_ROOT) {
        $candidates += $env:MSYS2_ROOT
    }
    $candidates += @(
        "C:\msys64",
        "C:\tools\msys64",
        "$env:LOCALAPPDATA\Programs\msys64"
    )

    foreach ($candidate in $candidates) {
        if (-not $candidate) {
            continue
        }
        $bash = Join-Path $candidate "usr\bin\bash.exe"
        if (Test-Path -LiteralPath $bash -PathType Leaf) {
            return (Resolve-Path $candidate).Path
        }
    }
    return ""
}

function New-Msys2Environment {
    param([string]$Root)

    $envBlock = @{}
    foreach ($entry in [System.Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $envBlock[$entry.Key] = [string]$entry.Value
    }
    $envBlock["MSYSTEM"] = "UCRT64"
    $envBlock["CHERE_INVOKING"] = "1"
    $envBlock["PATH"] = "$Root\ucrt64\bin;$Root\usr\bin;$($envBlock["PATH"])"
    return $envBlock
}

function ConvertTo-NativeArgument {
    param([AllowNull()][string]$Argument)

    if ($null -eq $Argument) {
        return '""'
    }
    if ($Argument.Length -eq 0) {
        return '""'
    }

    $escaped = $Argument -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    if ($escaped -match '[\s"]') {
        return '"' + $escaped + '"'
    }
    return $escaped
}

function Invoke-Msys2 {
    param(
        [string]$BashPath,
        [hashtable]$Environment,
        [string]$Command,
        [string]$Label
    )

    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $BashPath
    $psi.Arguments = "$(ConvertTo-NativeArgument "-lc") $(ConvertTo-NativeArgument $Command)"
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    foreach ($key in $Environment.Keys) {
        $psi.Environment[$key] = $Environment[$key]
    }

    $process = [System.Diagnostics.Process]::Start($psi)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result

    if ($process.ExitCode -ne 0) {
        throw "$Label failed with exit code $($process.ExitCode): $stderr"
    }
    return "$stdout`n$stderr"
}

function ConvertTo-MsysPath {
    param(
        [string]$BashPath,
        [hashtable]$Environment,
        [string]$WindowsPath
    )

    $escaped = $WindowsPath.Replace("'", "'\''")
    return (Invoke-Msys2 -BashPath $BashPath -Environment $Environment -Command "cygpath -u '$escaped'" -Label "cygpath").Trim()
}

function Get-FileInfoPayload {
    param([string]$Path)

    if (-not $Path) {
        return [ordered]@{
            path = ""
            exists = $false
        }
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [ordered]@{
            path = $Path
            exists = $false
        }
    }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        path = $item.FullName
        exists = $true
        sizeBytes = [int64]$item.Length
        sizeMiB = [math]::Round($item.Length / 1MB, 2)
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

function Copy-ProfileRuntimeDlls {
    param(
        [string]$Root,
        [string]$TargetDir
    )

    $sourceDir = Join-Path $Root "ucrt64\bin"
    $copied = @()
    foreach ($name in @("libmp3lame-0.dll", "libopus-0.dll", "libwinpthread-1.dll")) {
        $source = Join-Path $sourceDir $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            $target = Join-Path $TargetDir $name
            Copy-Item -LiteralPath $source -Destination $target -Force
            $copied += (Get-FileInfoPayload -Path $target)
        }
    }
    return $copied
}

function Resolve-FixtureFfmpeg {
    param([string]$Root)

    $candidates = @(
        (Join-Path $Root "ucrt64\bin\ffmpeg.exe")
    )
    $pathFfmpeg = Get-Command "ffmpeg.exe" -ErrorAction SilentlyContinue
    if ($pathFfmpeg) {
        $candidates += $pathFfmpeg.Source
    }
    $pathFfmpegNoExt = Get-Command "ffmpeg" -ErrorAction SilentlyContinue
    if ($pathFfmpegNoExt) {
        $candidates += $pathFfmpegNoExt.Source
    }

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path $candidate).Path
        }
    }
    return ""
}

function Invoke-PythonGate {
    param(
        [string[]]$Arguments,
        [string]$Label
    )

    & $PythonExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
if (-not $BuildRoot) {
    $BuildRoot = Join-Path $RepoRoot "build\ffmpeg-profile-b-msys2"
}
$BuildRoot = Convert-ToFullPath -Path $BuildRoot
$KitDir = Join-Path $BuildRoot "kit"
$SourceDir = Join-Path $BuildRoot "src\ffmpeg"
$PrefixDir = Join-Path $BuildRoot "dist\scriber-ffmpeg-profile-b"
$ReportPath = Join-Path $BuildRoot "profile-b-msys2-build-report.json"
$ManifestPath = Join-Path $BuildRoot "ffmpeg-profile-b-manifest.json"
$FixtureSmokePath = Join-Path $BuildRoot "ffmpeg-profile-b-fixtures.json"
$MediaSmokePath = Join-Path $BuildRoot "media-preparation-smoke-profile-b.json"
$SidecarGateReportPath = Join-Path $BuildRoot "sidecar-gate.txt"

$Msys2Root = Find-Msys2Root -RequestedRoot $Msys2Root
$BashPath = if ($Msys2Root) { Join-Path $Msys2Root "usr\bin\bash.exe" } else { "" }
$JobsValue = if ($Jobs -gt 0) { $Jobs } else { [Math]::Max(1, [Environment]::ProcessorCount) }

$plan = [ordered]@{
    apiVersion = "1"
    ok = $true
    mode = if ($PlanOnly) { "plan" } else { "build" }
    repoRoot = $RepoRoot
    buildRoot = $BuildRoot
    msys2Root = $Msys2Root
    bashPath = $BashPath
    sourceUrl = $SourceUrl
    gitRef = $GitRef
    jobs = $JobsValue
    requiredPackages = $RequiredMsys2Packages
    paths = [ordered]@{
        kitDir = $KitDir
        sourceDir = $SourceDir
        prefixDir = $PrefixDir
        report = $ReportPath
        manifest = $ManifestPath
        fixtureSmoke = $FixtureSmokePath
        mediaSmoke = $MediaSmokePath
        sidecarGateReport = $SidecarGateReportPath
    }
}

if ($PlanOnly) {
    $plan.commands = @(
        "python scripts\ffmpeg\create_profile_b_build_kit.py --output-dir `"$KitDir`" --source-url `"$SourceUrl`" --git-ref `"$GitRef`"",
        "pacman -S --needed --noconfirm $($RequiredMsys2Packages -join ' ')",
        "git clone --branch `"$GitRef`" --depth 1 `"$SourceUrl`" <source-dir>",
        "JOBS=$JobsValue <kit>/configure-profile-b.sh <source-dir> <prefix-dir>",
        "python scripts\ffmpeg\validate_ffmpeg_profile.py --media-tools-dir <prefix-dir>\bin --profile B --require-lgpl --output `"$ManifestPath`"",
        "python scripts\ffmpeg\smoke_profile_b_fixtures.py --media-tools-dir <prefix-dir>\bin --fixture-ffmpeg <full-ffmpeg> --require-ffprobe --output `"$FixtureSmokePath`"",
        "python scripts\smoke_media_preparation.py --media-tools-dir <prefix-dir>\bin --require-ffprobe --output `"$MediaSmokePath`""
    )
    Write-Utf8NoBomJson -Path $ReportPath -Payload $plan
    $plan | ConvertTo-Json -Depth 8
    exit 0
}

if (-not $Msys2Root -or -not (Test-Path -LiteralPath $BashPath -PathType Leaf)) {
    $plan.ok = $false
    $plan.error = "MSYS2 bash.exe was not found. Install MSYS2 or pass -Msys2Root."
    Write-Utf8NoBomJson -Path $ReportPath -Payload $plan
    $plan | ConvertTo-Json -Depth 8
    exit 1
}

$started = Get-Date
$msysEnv = New-Msys2Environment -Root $Msys2Root

if ($ForceClean -and (Test-Path -LiteralPath $BuildRoot -PathType Container)) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $BuildRoot, $KitDir | Out-Null

try {
    Invoke-PythonGate -Label "Profile B build kit generation" -Arguments @(
        "scripts\ffmpeg\create_profile_b_build_kit.py",
        "--output-dir",
        $KitDir,
        "--source-url",
        $SourceUrl,
        "--git-ref",
        $GitRef
    )

    if ($InstallDependencies) {
        $packages = $RequiredMsys2Packages -join " "
        Invoke-Msys2 -BashPath $BashPath -Environment $msysEnv -Command "pacman -S --needed --noconfirm $packages" -Label "MSYS2 dependency install" | Out-Host
    }

    $sourceDirMsys = ConvertTo-MsysPath -BashPath $BashPath -Environment $msysEnv -WindowsPath $SourceDir
    $prefixDirMsys = ConvertTo-MsysPath -BashPath $BashPath -Environment $msysEnv -WindowsPath $PrefixDir
    $kitScriptMsys = ConvertTo-MsysPath -BashPath $BashPath -Environment $msysEnv -WindowsPath (Join-Path $KitDir "configure-profile-b.sh")

    if (-not $SkipClone) {
        if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SourceDir) | Out-Null
            $cloneCommand = "git clone --branch '$GitRef' --depth 1 '$SourceUrl' '$sourceDirMsys'"
            Invoke-Msys2 -BashPath $BashPath -Environment $msysEnv -Command $cloneCommand -Label "FFmpeg source clone" | Out-Host
        } else {
            $fetchCommand = "cd '$sourceDirMsys' && git fetch --depth 1 origin '$GitRef' && git checkout FETCH_HEAD"
            Invoke-Msys2 -BashPath $BashPath -Environment $msysEnv -Command $fetchCommand -Label "FFmpeg source update" | Out-Host
        }
    }

    $buildCommand = "JOBS=$JobsValue '$kitScriptMsys' '$sourceDirMsys' '$prefixDirMsys'"
    Invoke-Msys2 -BashPath $BashPath -Environment $msysEnv -Command $buildCommand -Label "FFmpeg Profile B build" | Out-Host

    $mediaToolsDir = Join-Path $PrefixDir "bin"
    $ffmpegPath = Join-Path $mediaToolsDir "ffmpeg.exe"
    $ffprobePath = Join-Path $mediaToolsDir "ffprobe.exe"
    if (-not (Test-Path -LiteralPath $ffmpegPath -PathType Leaf)) {
        throw "Profile B build did not produce ffmpeg.exe at $ffmpegPath"
    }
    if (-not (Test-Path -LiteralPath $ffprobePath -PathType Leaf)) {
        throw "Profile B build did not produce ffprobe.exe at $ffprobePath"
    }
    $runtimeDlls = Copy-ProfileRuntimeDlls -Root $Msys2Root -TargetDir $mediaToolsDir
    $fixtureFfmpegPath = Resolve-FixtureFfmpeg -Root $Msys2Root

    Invoke-PythonGate -Label "Profile B manifest" -Arguments @(
        "scripts\ffmpeg\validate_ffmpeg_profile.py",
        "--media-tools-dir",
        $mediaToolsDir,
        "--profile",
        "B",
        "--require-lgpl",
        "--output",
        $ManifestPath
    )
    Invoke-PythonGate -Label "Profile B fixture smoke" -Arguments @(
        "scripts\ffmpeg\smoke_profile_b_fixtures.py",
        "--media-tools-dir",
        $mediaToolsDir,
        "--fixture-ffmpeg",
        $fixtureFfmpegPath,
        "--require-ffprobe",
        "--output",
        $FixtureSmokePath
    )
    Invoke-PythonGate -Label "Media preparation smoke" -Arguments @(
        "scripts\smoke_media_preparation.py",
        "--media-tools-dir",
        $mediaToolsDir,
        "--require-ffprobe",
        "--output",
        $MediaSmokePath
    )

    if ($RunSidecarGate) {
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 `
            -SkipFrontendBuild `
            -BundleMediaTools `
            -ValidateSlimMediaTools `
            -MediaToolsDir $mediaToolsDir `
            -CopyToTauriRelease 2>&1 | Tee-Object -FilePath $SidecarGateReportPath
        if ($LASTEXITCODE -ne 0) {
            throw "Sidecar candidate gate failed with exit code $LASTEXITCODE"
        }
    }

    $completed = Get-Date
    $payload = [ordered]@{
        apiVersion = "1"
        ok = $true
        mode = "build"
        startedAt = $started.ToUniversalTime().ToString("o")
        completedAt = $completed.ToUniversalTime().ToString("o")
        durationSeconds = [math]::Round(($completed - $started).TotalSeconds, 3)
        repoRoot = $RepoRoot
        buildRoot = $BuildRoot
        msys2Root = $Msys2Root
        sourceUrl = $SourceUrl
        gitRef = $GitRef
        jobs = $JobsValue
        mediaToolsDir = $mediaToolsDir
        ffmpeg = Get-FileInfoPayload -Path $ffmpegPath
        ffprobe = Get-FileInfoPayload -Path $ffprobePath
        fixtureFfmpeg = Get-FileInfoPayload -Path $fixtureFfmpegPath
        runtimeDlls = $runtimeDlls
        reports = [ordered]@{
            manifest = $ManifestPath
            fixtureSmoke = $FixtureSmokePath
            mediaSmoke = $MediaSmokePath
            sidecarGate = if ($RunSidecarGate) { $SidecarGateReportPath } else { $null }
        }
    }
    Write-Utf8NoBomJson -Path $ReportPath -Payload $payload
    $payload | ConvertTo-Json -Depth 8
} catch {
    $payload = [ordered]@{
        apiVersion = "1"
        ok = $false
        mode = "build"
        repoRoot = $RepoRoot
        buildRoot = $BuildRoot
        msys2Root = $Msys2Root
        sourceUrl = $SourceUrl
        gitRef = $GitRef
        error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
        position = $_.InvocationInfo.PositionMessage
        scriptStackTrace = $_.ScriptStackTrace
    }
    Write-Utf8NoBomJson -Path $ReportPath -Payload $payload
    $payload | ConvertTo-Json -Depth 8
    exit 1
}
