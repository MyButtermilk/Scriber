<#
.SYNOPSIS
Builds Scriber's locked CPython 3.14 NumPy no-BLAS product wheel.

.DESCRIPTION
Downloads only byte-locked inputs, verifies them before use, prepares a clean
build venv, patches NumPy's local distribution version, and then performs an
offline no-isolation Meson build. Normal builds must reproduce the artifact
already recorded in packaging/wheels/numpy-noblas-wheel-lock-v1.json.

-BootstrapArtifact is a diagnostic maintenance mode. It writes an unattested
wheel and a machine-readable inventory below build/ but never promotes it into
packaging/wheels. Update and review the lock, then rerun without that switch
before using -Promote.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,
    [string]$RepoRoot = "",
    [string]$LockPath = "",
    [string]$CacheRoot = "",
    [string]$WorkRoot = "",
    [string]$OutputDirectory = "",
    [string]$VisualStudioInstallPath = "",
    [switch]$Offline,
    [switch]$BootstrapArtifact,
    [switch]$Promote
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-PhysicalPathUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $pathFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay below $rootFull"
    }
    if (Test-Path -LiteralPath $rootFull) {
        $rootItem = Get-Item -LiteralPath $rootFull -Force
        if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label root must not be a reparse point."
        }
    }
    $relative = $pathFull.Substring($rootFull.Length).TrimStart('\', '/')
    $current = $rootFull
    foreach ($part in $relative.Split([char[]]@('\', '/'), [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        if (-not (Test-Path -LiteralPath $current)) {
            break
        }
        $item = Get-Item -LiteralPath $current -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label path contains a reparse point: $current"
        }
    }
    return $pathFull
}

function Assert-FileIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Identity,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    $expectedLength = [int64]$Identity.length
    $expectedSha256 = [string]$Identity.sha256
    if (
        $expectedLength -le 0 -or
        $expectedSha256 -notmatch '^[0-9a-f]{64}$' -or
        [int64]$item.Length -ne $expectedLength -or
        (Get-Sha256Hex -Path $item.FullName) -ne $expectedSha256
    ) {
        throw "$Label identity differs from the lock."
    }
    return $item.FullName
}

function Get-LockedInput {
    param(
        [Parameter(Mandatory = $true)][object]$Identity,
        [Parameter(Mandatory = $true)][string]$DestinationRoot,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$NoNetwork
    )

    $fileName = [string]$Identity.fileName
    $url = [string]$Identity.url
    if (
        -not $fileName -or
        [System.IO.Path]::GetFileName($fileName) -ne $fileName -or
        $url -notmatch '^https://'
    ) {
        throw "$Label lock entry is invalid."
    }
    $destination = Join-Path $DestinationRoot $fileName
    if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
        if ($NoNetwork) {
            throw "$Label is absent from the offline cache: $destination"
        }
        Invoke-WebRequest -Uri $url -OutFile $destination
    }
    return Assert-FileIdentity -Path $destination -Identity $Identity -Label $Label
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Set-LockedTextReplacement {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][object]$Patch
    )

    $relative = [string]$Patch.relativePath
    if (-not $relative -or $relative -match '(^|[\\/])\.\.([\\/]|$)' -or [System.IO.Path]::IsPathRooted($relative)) {
        throw "NumPy patch path is unsafe."
    }
    $path = Join-Path $SourceRoot $relative
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if (
        [int64]$item.Length -ne [int64]$Patch.upstreamLength -or
        (Get-Sha256Hex -Path $path) -ne [string]$Patch.upstreamSha256
    ) {
        throw "NumPy patch source drifted: $relative"
    }
    $encoding = [System.Text.UTF8Encoding]::new($false)
    $text = [System.IO.File]::ReadAllText($path, $encoding)
    $expected = [string]$Patch.replacement.expected
    $replacement = [string]$Patch.replacement.value
    $first = $text.IndexOf($expected, [System.StringComparison]::Ordinal)
    if ($first -lt 0 -or $text.IndexOf($expected, $first + $expected.Length, [System.StringComparison]::Ordinal) -ge 0) {
        throw "NumPy patch target must occur exactly once: $relative"
    }
    $patched = $text.Substring(0, $first) + $replacement + $text.Substring($first + $expected.Length)
    [System.IO.File]::WriteAllText($path, $patched, $encoding)
    $patchedItem = Get-Item -LiteralPath $path -Force
    if (
        [int64]$patchedItem.Length -ne [int64]$Patch.patchedLength -or
        (Get-Sha256Hex -Path $path) -ne [string]$Patch.patchedSha256
    ) {
        throw "NumPy patch result differs from the lock: $relative"
    }
}

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
if (-not $LockPath) {
    $LockPath = Join-Path $RepoRoot "packaging\wheels\numpy-noblas-wheel-lock-v1.json"
}
$LockPath = (Resolve-Path -LiteralPath $LockPath -ErrorAction Stop).Path
if (-not $CacheRoot) {
    $CacheRoot = Join-Path $RepoRoot "build\numpy-noblas-input-cache"
}
if (-not $WorkRoot) {
    $WorkRoot = Join-Path $RepoRoot "build\numpy-noblas-work"
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $RepoRoot "build\numpy-noblas-output"
}
$CacheRoot = Assert-PhysicalPathUnderRoot -Root $RepoRoot -Path $CacheRoot -Label "NumPy input cache"
$WorkRoot = Assert-PhysicalPathUnderRoot -Root $RepoRoot -Path $WorkRoot -Label "NumPy work root"
$OutputDirectory = Assert-PhysicalPathUnderRoot -Root $RepoRoot -Path $OutputDirectory -Label "NumPy output directory"
if ($Promote -and $BootstrapArtifact) {
    throw "-Promote cannot be combined with -BootstrapArtifact."
}

$pythonCandidate = if ([System.IO.Path]::IsPathRooted($PythonExecutable)) {
    $PythonExecutable
} else {
    Join-Path $RepoRoot $PythonExecutable
}
$PythonExecutable = (Resolve-Path -LiteralPath $pythonCandidate -ErrorAction Stop).Path

$lock = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Json
if (
    [string]$lock.contract -ne "ScriberNumPyNoBlasWheelLockV1" -or
    [int]$lock.schemaVersion -ne 1 -or
    [string]$lock.artifact.pythonTag -ne "cp314" -or
    [string]$lock.artifact.abiTag -ne "cp314" -or
    [string]$lock.artifact.platformTag -ne "win_amd64"
) {
    throw "NumPy wheel lock is not the CPython 3.14 Windows x64 product contract."
}

$pythonIdentityJson = & $PythonExecutable -c @'
import hashlib,json,platform,sys,sysconfig
from pathlib import Path
root=Path(sys.base_prefix)
def item(path):
    data=path.read_bytes()
    return {'length':len(data),'sha256':hashlib.sha256(data).hexdigest()}
print(json.dumps({
    'version':platform.python_version(),
    'implementation':platform.python_implementation(),
    'architecture':platform.machine(),
    'cacheTag':sys.implementation.cache_tag,
    'executable':item(Path(sys.executable)),
    'runtimeDll':item(root/'python314.dll'),
    'importLibrary':item(root/'libs'/'python314.lib'),
},separators=(',',':')))
'@
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the CPython build runtime."
}
$pythonIdentity = $pythonIdentityJson | ConvertFrom-Json
$lockedPython = $lock.build.python
if (
    [string]$pythonIdentity.version -ne [string]$lockedPython.version -or
    [string]$pythonIdentity.implementation -ne [string]$lockedPython.implementation -or
    [string]$pythonIdentity.architecture -ne [string]$lockedPython.architecture -or
    [string]$pythonIdentity.cacheTag -ne "cpython-314" -or
    [int64]$pythonIdentity.executable.length -ne [int64]$lockedPython.executableLength -or
    [string]$pythonIdentity.executable.sha256 -ne [string]$lockedPython.executableSha256 -or
    [int64]$pythonIdentity.runtimeDll.length -ne [int64]$lockedPython.runtimeDllLength -or
    [string]$pythonIdentity.runtimeDll.sha256 -ne [string]$lockedPython.runtimeDllSha256 -or
    [int64]$pythonIdentity.importLibrary.length -ne [int64]$lockedPython.importLibraryLength -or
    [string]$pythonIdentity.importLibrary.sha256 -ne [string]$lockedPython.importLibrarySha256
) {
    throw "CPython build runtime differs from the NumPy wheel lock."
}

foreach ($root in @($CacheRoot, $OutputDirectory)) {
    New-Item -ItemType Directory -Force -Path $root | Out-Null
}
if (Test-Path -LiteralPath $WorkRoot) {
    $null = Assert-PhysicalPathUnderRoot -Root $RepoRoot -Path $WorkRoot -Label "NumPy work cleanup"
    Remove-Item -LiteralPath $WorkRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null

$sourceArchive = Get-LockedInput -Identity $lock.source -DestinationRoot $CacheRoot -Label "NumPy source" -NoNetwork:$Offline
$buildWheelPaths = @()
foreach ($inputWheel in @($lock.build.pythonBuildWheels)) {
    $buildWheelPaths += Get-LockedInput -Identity $inputWheel -DestinationRoot $CacheRoot -Label "NumPy build wheel" -NoNetwork:$Offline
}

$sourceExtractRoot = Join-Path $WorkRoot "source"
New-Item -ItemType Directory -Path $sourceExtractRoot | Out-Null
Invoke-Checked -Label "NumPy source extraction" -Command {
    & tar.exe -xf $sourceArchive -C $sourceExtractRoot
}
$sourceRoot = Join-Path $sourceExtractRoot ("numpy-{0}" -f [string]$lock.source.version)
if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
    throw "NumPy source archive did not contain the expected root."
}
foreach ($patch in @($lock.source.patches)) {
    Set-LockedTextReplacement -SourceRoot $sourceRoot -Patch $patch
}

$headerSource = Join-Path $RepoRoot "packaging\wheels\build-inputs\stdalign.h"
$headerIdentity = $lock.build.compatibilityHeader
$null = Assert-FileIdentity -Path $headerSource -Identity $headerIdentity -Label "MSVC stdalign compatibility header"
$headerRoot = Join-Path $WorkRoot "compat-include"
New-Item -ItemType Directory -Path $headerRoot | Out-Null
Copy-Item -LiteralPath $headerSource -Destination (Join-Path $headerRoot "stdalign.h")

if (-not $VisualStudioInstallPath) {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        throw "vswhere.exe is required to resolve the locked Visual Studio toolchain."
    }
    $VisualStudioInstallPath = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
}
$VisualStudioInstallPath = (Resolve-Path -LiteralPath $VisualStudioInstallPath -ErrorAction Stop).Path
$devShellModule = Join-Path $VisualStudioInstallPath "Common7\Tools\Microsoft.VisualStudio.DevShell.dll"
if (-not (Test-Path -LiteralPath $devShellModule -PathType Leaf)) {
    throw "Visual Studio developer shell module is missing."
}
Import-Module $devShellModule
$vcToolsVersion = [string]$lock.build.nativeToolchain.vcToolsVersion
$sdkVersion = [string]$lock.build.nativeToolchain.windowsSdkVersion
Enter-VsDevShell `
    -VsInstallPath $VisualStudioInstallPath `
    -SkipAutomaticLocation `
    -DevCmdArguments "-arch=amd64 -host_arch=amd64 -vcvars_ver=$vcToolsVersion -winsdk=$sdkVersion"

$toolIdentities = @(
    @{ command = "cl.exe"; identity = $lock.build.nativeToolchain.compiler; label = "MSVC compiler" },
    @{ command = "link.exe"; identity = $lock.build.nativeToolchain.linker; label = "MSVC linker" },
    @{ command = "lib.exe"; identity = $lock.build.nativeToolchain.librarian; label = "MSVC librarian" },
    @{ command = "rc.exe"; identity = $lock.build.nativeToolchain.resourceCompiler; label = "Windows resource compiler" }
)
foreach ($tool in $toolIdentities) {
    $command = Get-Command $tool.command -ErrorAction Stop | Select-Object -First 1
    $null = Assert-FileIdentity -Path $command.Source -Identity $tool.identity -Label $tool.label
}

$buildVenv = Join-Path $WorkRoot "venv"
Invoke-Checked -Label "NumPy build venv creation" -Command {
    & $PythonExecutable -m venv $buildVenv
}
$buildPython = Join-Path $buildVenv "Scripts\python.exe"
Invoke-Checked -Label "NumPy offline build dependency installation" -Command {
    & $buildPython -m pip install `
        --disable-pip-version-check `
        --no-index `
        --no-cache-dir `
        --no-deps `
        --find-links $CacheRoot `
        @($buildWheelPaths)
}

$oldEnvironment = @{}
foreach ($name in @(
    "PATH",
    "PYTHONHASHSEED",
    "SOURCE_DATE_EPOCH",
    "CL",
    "LINK",
    "CFLAGS",
    "CXXFLAGS",
    "LDFLAGS",
    "INCLUDE",
    "PIP_NO_INDEX",
    "PIP_DISABLE_PIP_VERSION_CHECK"
)) {
    $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
try {
    $buildScripts = Join-Path $buildVenv "Scripts"
    $env:PATH = $buildScripts + [System.IO.Path]::PathSeparator + $env:PATH
    $env:PYTHONHASHSEED = [string]$lock.build.determinism.PYTHONHASHSEED
    $env:SOURCE_DATE_EPOCH = [string]$lock.build.determinism.SOURCE_DATE_EPOCH
    $env:CL = [string]$lock.build.determinism.CL
    $env:LINK = [string]$lock.build.determinism.LINK
    $env:CFLAGS = [string]$lock.build.determinism.CFLAGS
    $env:CXXFLAGS = [string]$lock.build.determinism.CXXFLAGS
    $env:LDFLAGS = [string]$lock.build.determinism.LDFLAGS
    $env:INCLUDE = $headerRoot + [System.IO.Path]::PathSeparator + $env:INCLUDE
    $env:PIP_NO_INDEX = "1"
    $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

    Get-ChildItem -LiteralPath $OutputDirectory -Filter "numpy-*.whl" -File -ErrorAction SilentlyContinue |
        Remove-Item -Force
    $settings = @()
    foreach ($setting in @($lock.build.meson.configSettings)) {
        $settings += "-Csetup-args=$setting"
    }
    Invoke-Checked -Label "NumPy no-BLAS wheel build" -Command {
        & $buildPython -m pip wheel `
            --no-index `
            --no-cache-dir `
            --no-deps `
            --no-build-isolation `
            --wheel-dir $OutputDirectory `
            @settings `
            $sourceRoot
    }
} finally {
    foreach ($entry in $oldEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable([string]$entry.Key, $entry.Value, "Process")
    }
}

$builtWheels = @(Get-ChildItem -LiteralPath $OutputDirectory -Filter "numpy-*.whl" -File)
if ($builtWheels.Count -ne 1) {
    throw "Expected exactly one built NumPy wheel."
}
$builtWheel = $builtWheels[0]
if ($builtWheel.Name -ne [string]$lock.artifact.fileName) {
    throw "Built NumPy wheel has an unexpected name: $($builtWheel.Name)"
}
$validator = Join-Path $RepoRoot "scripts\validate_numpy_noblas_wheel.py"
Invoke-Checked -Label "NumPy wheel canonicalization" -Command {
    & $PythonExecutable $validator --canonicalize-built-wheel --wheel $builtWheel.FullName
}
$builtWheel = Get-Item -LiteralPath $builtWheel.FullName -Force

$summary = [ordered]@{
    schemaVersion = 1
    contract = "ScriberNumPyNoBlasBuildResultV1"
    bootstrapArtifact = [bool]$BootstrapArtifact
    wheel = [ordered]@{
        path = $builtWheel.FullName
        fileName = $builtWheel.Name
        length = [int64]$builtWheel.Length
        sha256 = Get-Sha256Hex -Path $builtWheel.FullName
    }
    python = $pythonIdentity
    visualStudio = [string]$lock.build.nativeToolchain.visualStudio
    vcToolsVersion = $vcToolsVersion
    windowsSdkVersion = $sdkVersion
}

if (-not $BootstrapArtifact) {
    Invoke-Checked -Label "Locked NumPy wheel validation" -Command {
        & $PythonExecutable $validator --lock $LockPath --wheel $builtWheel.FullName
    }
    if ($Promote) {
        $artifactPath = Join-Path $RepoRoot ([string]$lock.artifact.relativePath)
        $artifactPath = [System.IO.Path]::GetFullPath($artifactPath)
        $artifactParent = Split-Path -Parent $artifactPath
        if (-not $artifactParent.Equals((Join-Path $RepoRoot "packaging\wheels"), [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Locked NumPy artifact path escaped packaging/wheels."
        }
        Copy-Item -LiteralPath $builtWheel.FullName -Destination $artifactPath -Force
        $summary["promotedPath"] = $artifactPath
    }
}

$resultPath = Join-Path $OutputDirectory "numpy-noblas-build-result.json"
$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $resultPath -Encoding utf8
Write-Output ($summary | ConvertTo-Json -Depth 8 -Compress)
