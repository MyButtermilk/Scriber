[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("A13", "O0", "O1", "C0", "C1", "T0", "T1", "K0")]
    [string]$Variant,
    [string]$LockPath = "",
    [string]$InputCacheRoot = "",
    [string]$ExternalsRoot = "",
    [string]$HostPythonRoot = "",
    [string]$OfficialA13Root = "",
    [string]$OutputRoot = "",
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-Sha256File {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-RelativeNormalizedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd("\") + "\"
    $rootUri = [System.Uri]::new($rootPath)
    $pathUri = [System.Uri]::new([System.IO.Path]::GetFullPath($Path))
    return [System.Uri]::UnescapeDataString($rootUri.MakeRelativeUri($pathUri).ToString()).Replace("\", "/")
}

function Convert-HexToBytes {
    param([Parameter(Mandatory = $true)][string]$Value)
    $bytes = New-Object byte[] ($Value.Length / 2)
    for ($index = 0; $index -lt $bytes.Length; $index += 1) {
        $bytes[$index] = [Convert]::ToByte($Value.Substring($index * 2, 2), 16)
    }
    return $bytes
}

function Convert-BytesToLowerHex {
    param([Parameter(Mandatory = $true)][byte[]]$Value)
    return ([BitConverter]::ToString($Value)).Replace("-", "").ToLowerInvariant()
}

function Assert-LockedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$LockEntry,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing locked $Label input: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -ne [int64]$LockEntry.sizeBytes) {
        throw "$Label size mismatch: expected $($LockEntry.sizeBytes), got $($item.Length)"
    }
    $actualHash = Get-Sha256File -Path $Path
    if ($actualHash -cne [string]$LockEntry.sha256) {
        throw "$Label SHA-256 mismatch: expected $($LockEntry.sha256), got $actualHash"
    }
}

function Get-TreeIdentity {
    param([Parameter(Mandatory = $true)][string]$Root)
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    $files = @(
        Get-ChildItem -LiteralPath $resolvedRoot -Recurse -File |
            Sort-Object {
                $relativeSortPath = Get-RelativeNormalizedPath -Root $resolvedRoot -Path $_.FullName
                Convert-BytesToLowerHex -Value ([System.Text.Encoding]::UTF8.GetBytes($relativeSortPath))
            }
    )
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $total = [int64]0
    try {
        foreach ($file in $files) {
            $relative = Get-RelativeNormalizedPath -Root $resolvedRoot -Path $file.FullName
            $relativeBytes = [System.Text.Encoding]::UTF8.GetBytes($relative)
            $relativeLength = [System.BitConverter]::GetBytes([System.Net.IPAddress]::HostToNetworkOrder([int]$relativeBytes.Length))
            $sha.TransformBlock($relativeLength, 0, $relativeLength.Length, $null, 0) | Out-Null
            $sha.TransformBlock($relativeBytes, 0, $relativeBytes.Length, $null, 0) | Out-Null

            $lengthBytes = [System.BitConverter]::GetBytes([System.Net.IPAddress]::HostToNetworkOrder([int64]$file.Length))
            $sha.TransformBlock($lengthBytes, 0, $lengthBytes.Length, $null, 0) | Out-Null
            $fileHash = Convert-HexToBytes -Value (Get-Sha256File -Path $file.FullName)
            $sha.TransformBlock($fileHash, 0, $fileHash.Length, $null, 0) | Out-Null
            $total += $file.Length
        }
        $sha.TransformFinalBlock([byte[]]::new(0), 0, 0) | Out-Null
        return [ordered]@{
            fileCount = $files.Count
            sizeBytes = $total
            treeSha256 = Convert-BytesToLowerHex -Value $sha.Hash
        }
    } finally {
        $sha.Dispose()
    }
}

function Assert-TreeIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)]$LockEntry
    )
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Missing locked CPython external: $Root"
    }
    $actual = Get-TreeIdentity -Root $Root
    if (
        $actual.fileCount -ne [int]$LockEntry.fileCount -or
        $actual.sizeBytes -ne [int64]$LockEntry.sizeBytes -or
        $actual.treeSha256 -cne [string]$LockEntry.treeSha256
    ) {
        $treeLabel = if ($null -ne $LockEntry.PSObject.Properties["directory"]) {
            [string]$LockEntry.directory
        } else {
            $Root
        }
        throw "Locked tree '$treeLabel' does not match the expected file count, size, and tree SHA-256."
    }
}

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Child
    )
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd("\") + "\"
    $childFull = [System.IO.Path]::GetFullPath($Child)
    if (-not $childFull.StartsWith($parentFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe work path outside the selected output root: $childFull"
    }
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = ""
    )
    $previousLocation = Get-Location
    try {
        if ($WorkingDirectory) {
            Set-Location -LiteralPath $WorkingDirectory
        }
        & $Executable @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Executable exited with code $LASTEXITCODE"
        }
    } finally {
        Set-Location -LiteralPath $previousLocation
    }
}

function Get-ToolFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing required $Name tool: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        path = $item.FullName
        fileVersion = [string]$item.VersionInfo.FileVersion
        productVersion = [string]$item.VersionInfo.ProductVersion
        sizeBytes = [int64]$item.Length
        sha256 = Get-Sha256File -Path $item.FullName
    }
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if (-not $LockPath) {
    $LockPath = Join-Path $repoRoot "packaging\cpython-windows-runtime-input-lock-v1.json"
}
if (-not $InputCacheRoot) {
    $InputCacheRoot = Join-Path $repoRoot "build\runtime-input-cache"
}
if (-not $ExternalsRoot) {
    $ExternalsRoot = Join-Path $InputCacheRoot "cpython-3.14.6-externals"
}
if (-not $HostPythonRoot) {
    $HostPythonRoot = Join-Path $repoRoot "build\python-3.14.6-official"
}
if (-not $OfficialA13Root) {
    $OfficialA13Root = Join-Path $repoRoot "build\python-3.13.14-official"
}
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $repoRoot "build\cpython-windows-runtimes"
}

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "CPython Windows runtime production is supported only on Windows."
}
if (-not (Test-Path -LiteralPath $LockPath -PathType Leaf)) {
    throw "Missing runtime input lock: $LockPath"
}
$lock = Get-Content -LiteralPath $LockPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (
    [string]$lock.lockContract -cne "ScriberCPythonWindowsRuntimeInputLockV1" -or
    [int]$lock.schemaVersion -ne 1 -or
    [string]$lock.target.pythonVersion -cne "3.14.6" -or
    [string]$lock.target.cpythonTag -cne "v3.14.6"
) {
    throw "The CPython Windows runtime input lock has an unsupported identity."
}
$lockSha256 = Get-Sha256File -Path $LockPath
$builderSha256 = Get-Sha256File -Path $PSCommandPath
$variantLock = $lock.buildMatrix.$Variant
if ($null -eq $variantLock) {
    throw "Variant '$Variant' is not present in the runtime lock."
}

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$outputRootFull = [System.IO.Path]::GetFullPath($OutputRoot)
$familyRoot = Join-Path $outputRootFull ("families\" + [string]$variantLock.family)
$variantManifestRoot = Join-Path $outputRootFull "variants"
$workRoot = Join-Path $outputRootFull "work"
Assert-ChildPath -Parent $outputRootFull -Child $familyRoot
Assert-ChildPath -Parent $outputRootFull -Child $variantManifestRoot
Assert-ChildPath -Parent $outputRootFull -Child $workRoot
New-Item -ItemType Directory -Force -Path $variantManifestRoot, $workRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $familyRoot) | Out-Null

$isOfficial = $Variant -in @("A13", "O0", "O1")
$familyCompletePath = Join-Path $familyRoot "scriber-runtime-family-manifest.json"
$toolchain = [ordered]@{}
$buildArguments = @()

if ($isOfficial) {
    $officialKey = if ($Variant -eq "A13") { "A13" } else { "O" }
    $officialLock = $lock.officialRuntimes.$officialKey
    $installerPath = Join-Path $InputCacheRoot ([string]$officialLock.installer.fileName)
    Assert-LockedFile -Path $installerPath -LockEntry $officialLock.installer -Label "$Variant official installer"
    if (-not (Test-Path -LiteralPath $familyCompletePath -PathType Leaf)) {
        if (Test-Path -LiteralPath $familyRoot) {
            throw "Incomplete runtime family already exists; remove this exact build output before retrying: $familyRoot"
        }
        $temporaryFamily = Join-Path $workRoot ("official-" + [guid]::NewGuid().ToString("N"))
        Assert-ChildPath -Parent $workRoot -Child $temporaryFamily
        New-Item -ItemType Directory -Force -Path $temporaryFamily | Out-Null
        try {
            $existingOfficialRoot = if ($Variant -eq "A13") { $OfficialA13Root } else { $HostPythonRoot }
            if (Test-Path -LiteralPath (Join-Path $existingOfficialRoot "python.exe") -PathType Leaf) {
                Get-ChildItem -LiteralPath $existingOfficialRoot -Force |
                    Copy-Item -Destination $temporaryFamily -Recurse
            } else {
                $arguments = @(
                    "/quiet",
                    "InstallAllUsers=0",
                    "PrependPath=0",
                    "Include_launcher=0",
                    "AssociateFiles=0",
                    "Include_test=1",
                    "CompileAll=1",
                    "TargetDir=$temporaryFamily"
                )
                $process = Start-Process -FilePath $installerPath -ArgumentList $arguments -Wait -PassThru -NoNewWindow
                if ($process.ExitCode -ne 0) {
                    throw "Official Python installer exited with code $($process.ExitCode)"
                }
            }
            $pythonPath = Join-Path $temporaryFamily "python.exe"
            $reportedIdentity = (& $pythonPath -c "import json,sys; print(json.dumps({'version': '.'.join(map(str, sys.version_info[:3])), 'cacheTag': sys.implementation.cache_tag}, sort_keys=True))")
            if ($LASTEXITCODE -ne 0) {
                throw "Official Python identity probe failed."
            }
            $reportedIdentity = $reportedIdentity | ConvertFrom-Json
            if (
                [string]$reportedIdentity.version -cne [string]$officialLock.version -or
                [string]$reportedIdentity.cacheTag -cne [string]$officialLock.cacheTag
            ) {
                throw "Official Python identity does not match the locked version and cache tag."
            }
            if ($officialKey -eq "O") {
                foreach ($fileLock in $officialLock.lockedInstalledFiles) {
                    Assert-LockedFile -Path (Join-Path $temporaryFamily ([string]$fileLock.relativePath)) -LockEntry $fileLock -Label "O family output"
                }
            }
            $runtimeOutputs = @()
            $requiredOutputs = if ($Variant -eq "A13") {
                @("python.exe", "python3.dll", "python313.dll")
            } else {
                @($lock.requiredRuntimeOutputs)
            }
            foreach ($relativePath in $requiredOutputs) {
                $path = Join-Path $temporaryFamily ([string]$relativePath)
                if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                    throw "Official runtime is missing required output $relativePath"
                }
                $item = Get-Item -LiteralPath $path
                $runtimeOutputs += [ordered]@{
                    relativePath = [string]$relativePath
                    sizeBytes = [int64]$item.Length
                    sha256 = Get-Sha256File -Path $path
                }
            }
            $familyManifest = [ordered]@{
                manifestContract = "ScriberCPythonWindowsRuntimeFamilyManifestV1"
                schemaVersion = 1
                family = [string]$variantLock.family
                runtimeFlavor = [string]$variantLock.runtimeFlavor
                pythonVersion = [string]$officialLock.version
                cacheTag = [string]$officialLock.cacheTag
                compiler = "official-cpython-windows"
                tailCallInterpreter = $false
                jitAvailable = ($Variant -ne "A13")
                runtimeLockSha256 = $lockSha256
                builderSha256 = $builderSha256
                inputInstaller = [ordered]@{
                    sizeBytes = [int64]$officialLock.installer.sizeBytes
                    sha256 = [string]$officialLock.installer.sha256
                }
                runtimeOutputs = $runtimeOutputs
                runtimeTree = Get-TreeIdentity -Root $temporaryFamily
            }
            $familyManifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $temporaryFamily "scriber-runtime-family-manifest.json") -Encoding UTF8
            Move-Item -LiteralPath $temporaryFamily -Destination $familyRoot
        } finally {
            if (Test-Path -LiteralPath $temporaryFamily) {
                Remove-Item -LiteralPath $temporaryFamily -Recurse -Force
            }
        }
    }
} else {
    $sourceLock = $lock.customBuildInputs.source
    $llvmLock = $lock.customBuildInputs.llvm
    $sourceArchive = Join-Path $InputCacheRoot ([string]$sourceLock.fileName)
    $llvmArchive = Join-Path $InputCacheRoot ([string]$llvmLock.fileName)
    Assert-LockedFile -Path $sourceArchive -LockEntry $sourceLock -Label "CPython source"
    Assert-LockedFile -Path $llvmArchive -LockEntry $llvmLock -Label "LLVM"
    foreach ($externalLock in $lock.customBuildInputs.externals) {
        Assert-TreeIdentity -Root (Join-Path $ExternalsRoot ([string]$externalLock.directory)) -LockEntry $externalLock
    }
    foreach ($hostFileLock in $lock.customBuildInputs.hostPython.files) {
        Assert-LockedFile -Path (Join-Path $HostPythonRoot ([string]$hostFileLock.relativePath)) -LockEntry $hostFileLock -Label "host Python"
    }

    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        throw "Visual Studio Installer vswhere.exe is unavailable."
    }
    $installationPath = (@(& $vswhere -products ([string]$lock.customBuildInputs.visualStudio.product) -version "[17.14,17.15)" -latest -property installationPath) -join "").Trim()
    $installationVersion = (@(& $vswhere -products ([string]$lock.customBuildInputs.visualStudio.product) -version "[17.14,17.15)" -latest -property installationVersion) -join "").Trim()
    if (-not $installationPath -or $installationVersion -cne [string]$lock.customBuildInputs.visualStudio.installationVersion) {
        throw "Visual Studio Build Tools $($lock.customBuildInputs.visualStudio.installationVersion) is required; found '$installationVersion'."
    }
    $msbuildPath = Join-Path $installationPath "MSBuild\Current\Bin\MSBuild.exe"
    $sdkDirectoryVersion = [string]$lock.customBuildInputs.windowsSdk.directoryVersion
    $sdkBinRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin\$sdkDirectoryVersion\x64"
    $rcPath = Join-Path $sdkBinRoot "rc.exe"
    $toolchain.visualStudioInstallationVersion = $installationVersion
    $toolchain.msbuild = Get-ToolFile -Path $msbuildPath -Name "MSBuild"
    $toolchain.windowsSdkServicingVersion = [string]$lock.customBuildInputs.windowsSdk.servicingVersion
    $toolchain.windowsSdkRc = Get-ToolFile -Path $rcPath -Name "Windows SDK rc.exe"

    if (-not (Test-Path -LiteralPath $familyCompletePath -PathType Leaf) -and -not $VerifyOnly) {
        if (Test-Path -LiteralPath $familyRoot) {
            throw "Incomplete runtime family already exists; remove this exact build output before retrying: $familyRoot"
        }
        $temporaryRoot = Join-Path $workRoot ("custom-" + [guid]::NewGuid().ToString("N"))
        Assert-ChildPath -Parent $workRoot -Child $temporaryRoot
        $sourceRoot = Join-Path $temporaryRoot "source"
        $llvmRoot = Join-Path $temporaryRoot "llvm"
        New-Item -ItemType Directory -Force -Path $sourceRoot, $llvmRoot | Out-Null
        try {
            Invoke-NativeChecked -Executable "tar.exe" -Arguments @("-xf", $sourceArchive, "-C", $sourceRoot, "--strip-components=1")
            Invoke-NativeChecked -Executable "tar.exe" -Arguments @("-xf", $llvmArchive, "-C", $llvmRoot, "--strip-components=1")
            # CPython's Windows project asks git for build metadata. The locked
            # source archive intentionally has no .git directory, but it is
            # extracted below the Scriber checkout. Without an explicit git
            # boundary, git walks up to Scriber's .git directory and embeds the
            # Scriber branch, commit, and dirty state in sys.version/sys._git.
            # Use a deterministic failing shim so archive builds retain empty
            # SCM metadata. The post-layout identity probe below is the
            # fail-closed guard that proves the shim actually took effect.
            $gitIsolationShim = Join-Path $temporaryRoot "git-metadata-disabled.cmd"
            [IO.File]::WriteAllLines(
                $gitIsolationShim,
                [string[]]@(
                    "@echo off",
                    "exit /b 1"
                ),
                [Text.ASCIIEncoding]::new()
            )
            $gitIsolationShimSha256 = Get-Sha256File -Path $gitIsolationShim
            $stagedExternals = Join-Path $sourceRoot "externals"
            New-Item -ItemType Directory -Force -Path $stagedExternals | Out-Null
            foreach ($externalLock in $lock.customBuildInputs.externals) {
                Copy-Item -LiteralPath (Join-Path $ExternalsRoot ([string]$externalLock.directory)) -Destination $stagedExternals -Recurse
            }

            $familyPrefix = $Variant.Substring(0, 1)
            $lockedArguments = @($lock.customBuildCommands.$familyPrefix)
            if ($lockedArguments -contains "-E") {
                throw "The locked CPython build command illegally contains -E."
            }
            $buildScript = Join-Path $sourceRoot ([string]$lockedArguments[0])
            $effectiveBuildArguments = @($lockedArguments | Select-Object -Skip 1)
            $effectiveBuildArguments += "/p:LLVMInstallDir=$llvmRoot"
            $effectiveBuildArguments += "/p:WindowsTargetPlatformVersion=$sdkDirectoryVersion"
            $effectiveBuildArguments += "/p:GIT=$gitIsolationShim"
            $msbuildProperties = @($effectiveBuildArguments | Where-Object { [string]$_ -like "/p:*" })
            $buildArguments = @($effectiveBuildArguments | Where-Object { [string]$_ -notlike "/p:*" })
            if ($buildArguments.Count -gt 9) {
                throw "CPython build.bat accepts no more than nine non-MSBuild arguments."
            }
            $msbuildResponsePath = Join-Path $sourceRoot "PCbuild\msbuild.rsp"
            if (Test-Path -LiteralPath $msbuildResponsePath) {
                throw "The pristine CPython source unexpectedly contains PCbuild\msbuild.rsp."
            }
            [IO.File]::WriteAllLines(
                $msbuildResponsePath,
                [string[]]$msbuildProperties,
                [Text.UTF8Encoding]::new($false)
            )
            $buildLog = Join-Path $temporaryRoot "build.log"

            $savedEnvironment = @{}
            foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "PIP_NO_INDEX", "PYTHON_JIT", "PYTHON_GIL", "PYTHON_TLBC", "IncludeLLVM", "GIT")) {
                $savedEnvironment[$name] = [System.Environment]::GetEnvironmentVariable($name, "Process")
            }
            try {
                $env:HTTP_PROXY = "http://127.0.0.1:9"
                $env:HTTPS_PROXY = "http://127.0.0.1:9"
                $env:ALL_PROXY = "http://127.0.0.1:9"
                $env:NO_PROXY = ""
                $env:PIP_NO_INDEX = "1"
                $env:IncludeLLVM = "false"
                # PCbuild\build.bat discovers Git itself and appends a later
                # /p:GIT property, which overrides msbuild.rsp. Pin the
                # environment variable as well so every instrumented and PGO
                # build invocation uses the isolation shim.
                $env:GIT = $gitIsolationShim
                Remove-Item Env:PYTHON_JIT -ErrorAction SilentlyContinue
                Remove-Item Env:PYTHON_GIL -ErrorAction SilentlyContinue
                Remove-Item Env:PYTHON_TLBC -ErrorAction SilentlyContinue
                & $buildScript @buildArguments *> $buildLog
                if ($LASTEXITCODE -ne 0) {
                    $failureRoot = Join-Path $OutputRoot "failures"
                    New-Item -ItemType Directory -Force -Path $failureRoot | Out-Null
                    $retainedBuildLog = Join-Path $failureRoot "$Variant-build.log"
                    Copy-Item -LiteralPath $buildLog -Destination $retainedBuildLog -Force
                    throw "CPython build exited with code $LASTEXITCODE. See retained log $retainedBuildLog"
                }
            } finally {
                foreach ($name in $savedEnvironment.Keys) {
                    $value = $savedEnvironment[$name]
                    if ($null -eq $value) {
                        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
                    } else {
                        [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
                    }
                }
            }
            $logText = Get-Content -LiteralPath $buildLog -Raw -Encoding UTF8
            if (
                $logText -match (
                    "(?im)^\s*Fetching (?!external (?:libraries|binaries)\.\.\.)|" +
                    "^\s*(downloading|cloning)\b|git\s+clone|invoke-webrequest|curl(?:\.exe)?\s"
                )
            ) {
                throw "The offline CPython build attempted to fetch an input."
            }

            $builtRoot = Join-Path $sourceRoot "PCbuild\amd64"
            foreach ($requiredOutput in $lock.requiredRuntimeOutputs) {
                if (-not (Test-Path -LiteralPath (Join-Path $builtRoot ([string]$requiredOutput)) -PathType Leaf)) {
                    throw "Custom CPython build is missing required output $requiredOutput"
                }
            }
            $temporaryFamily = Join-Path $temporaryRoot "family"
            $layoutScript = Join-Path $sourceRoot "PC\layout"
            $layoutWork = Join-Path $temporaryRoot "layout-work"
            $layoutArguments = @(
                $layoutScript,
                "-s", $sourceRoot,
                "-b", $builtRoot,
                "--arch", "amd64",
                "--copy", $temporaryFamily,
                "-t", $layoutWork,
                "--include-pip",
                "--include-venv",
                "--include-alias3",
                "--include-alias3x",
                "--include-stable",
                "--flat-dlls"
            )
            Invoke-NativeChecked `
                -Executable (Join-Path $HostPythonRoot "python.exe") `
                -Arguments $layoutArguments
            foreach ($requiredLayoutPath in @(
                "Lib\encodings\__init__.py",
                "Lib\ensurepip\__init__.py",
                "Lib\venv\__init__.py"
            )) {
                if (-not (Test-Path -LiteralPath (Join-Path $temporaryFamily $requiredLayoutPath) -PathType Leaf)) {
                    throw "Custom CPython layout is missing required runtime file $requiredLayoutPath"
                }
            }
            $identityProbe = @'
import json
import sys

print(json.dumps({
    "version": sys.version,
    "versionInfo": list(sys.version_info[:3]),
    "cacheTag": sys.implementation.cache_tag,
    "git": list(getattr(sys, "_git", ())),
}, sort_keys=True))
'@
            $identityOutput = @(
                & (Join-Path $temporaryFamily "python.exe") -I -c $identityProbe 2>&1
            )
            if ($LASTEXITCODE -ne 0) {
                throw "Custom CPython identity probe exited with code $LASTEXITCODE."
            }
            try {
                $buildIdentity = ($identityOutput -join [Environment]::NewLine).Trim() | ConvertFrom-Json
            } catch {
                throw "Custom CPython identity probe did not emit valid JSON."
            }
            $buildGitIdentity = @($buildIdentity.git)
            if (
                @($buildIdentity.versionInfo).Count -ne 3 -or
                [int]$buildIdentity.versionInfo[0] -ne 3 -or
                [int]$buildIdentity.versionInfo[1] -ne 14 -or
                [int]$buildIdentity.versionInfo[2] -ne 6 -or
                [string]$buildIdentity.cacheTag -cne "cpython-314" -or
                $buildGitIdentity.Count -ne 3 -or
                [string]$buildGitIdentity[0] -cne "CPython" -or
                [string]$buildGitIdentity[1] -cne "" -or
                [string]$buildGitIdentity[2] -cne ""
            ) {
                throw "Custom CPython build identity is not the isolated v3.14.6 archive identity."
            }
            $runtimeOutputs = @()
            foreach ($relativePath in $lock.requiredRuntimeOutputs) {
                $path = Join-Path $temporaryFamily ([string]$relativePath)
                $item = Get-Item -LiteralPath $path
                $runtimeOutputs += [ordered]@{
                    relativePath = [string]$relativePath
                    sizeBytes = [int64]$item.Length
                    sha256 = Get-Sha256File -Path $path
                }
            }
            $clangPath = Join-Path $llvmRoot "bin\clang-cl.exe"
            $toolchain.clang = Get-ToolFile -Path $clangPath -Name "clang-cl"
            $familyManifest = [ordered]@{
                manifestContract = "ScriberCPythonWindowsRuntimeFamilyManifestV1"
                schemaVersion = 1
                family = [string]$variantLock.family
                runtimeFlavor = [string]$variantLock.runtimeFlavor
                pythonVersion = "3.14.6"
                cacheTag = "cpython-314"
                compiler = "ClangCL 19.1.7 ThinLTO PGO=$($familyPrefix -ne 'K')"
                tailCallInterpreter = [bool]$variantLock.tailCallInterpreter
                jitAvailable = ($familyPrefix -ne "K")
                runtimeLockSha256 = $lockSha256
                builderSha256 = $builderSha256
                source = [ordered]@{
                    sizeBytes = [int64]$sourceLock.sizeBytes
                    sha256 = [string]$sourceLock.sha256
                }
                llvm = [ordered]@{
                    version = [string]$llvmLock.version
                    sizeBytes = [int64]$llvmLock.sizeBytes
                    sha256 = [string]$llvmLock.sha256
                }
                externals = @($lock.customBuildInputs.externals)
                hostPython = @($lock.customBuildInputs.hostPython.files)
                buildArguments = $effectiveBuildArguments
                msbuildResponseProperties = $msbuildProperties
                gitMetadataIsolation = [ordered]@{
                    mode = "disabled-for-locked-source-archive"
                    shimSha256 = $gitIsolationShimSha256
                    observedSysGit = $buildGitIdentity
                }
                buildIdentity = $buildIdentity
                toolchain = $toolchain
                runtimeOutputs = $runtimeOutputs
                runtimeTree = Get-TreeIdentity -Root $temporaryFamily
                buildLog = [ordered]@{
                    sizeBytes = [int64](Get-Item -LiteralPath $buildLog).Length
                    sha256 = Get-Sha256File -Path $buildLog
                }
            }
            $familyManifest | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath (Join-Path $temporaryFamily "scriber-runtime-family-manifest.json") -Encoding UTF8
            Move-Item -LiteralPath $temporaryFamily -Destination $familyRoot
        } finally {
            if (Test-Path -LiteralPath $temporaryRoot) {
                Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
            }
        }
    }
}

if (-not (Test-Path -LiteralPath $familyCompletePath -PathType Leaf)) {
    if ($VerifyOnly) {
        throw "VerifyOnly requested, but the runtime family manifest does not exist: $familyCompletePath"
    }
    throw "Runtime family production did not create its completion manifest."
}
$familyManifest = Get-Content -LiteralPath $familyCompletePath -Raw -Encoding UTF8 | ConvertFrom-Json
if (
    [string]$familyManifest.manifestContract -cne "ScriberCPythonWindowsRuntimeFamilyManifestV1" -or
    [string]$familyManifest.family -cne [string]$variantLock.family -or
    [string]$familyManifest.runtimeLockSha256 -cne $lockSha256 -or
    [string]$familyManifest.builderSha256 -cne $builderSha256
) {
    throw "Runtime family manifest does not match the selected lock and family."
}
foreach ($output in $familyManifest.runtimeOutputs) {
    Assert-LockedFile -Path (Join-Path $familyRoot ([string]$output.relativePath)) -LockEntry $output -Label "runtime family output"
}

$jitExpected = [bool]$variantLock.jit
$variantManifest = [ordered]@{
    manifestContract = "ScriberCPythonWindowsRuntimeVariantManifestV1"
    schemaVersion = 1
    variantId = $Variant
    family = [string]$variantLock.family
    runtimeFlavor = [string]$variantLock.runtimeFlavor
    pythonVersion = [string]$familyManifest.pythonVersion
    cacheTag = [string]$familyManifest.cacheTag
    compiler = [string]$familyManifest.compiler
    tailCallInterpreter = [bool]$familyManifest.tailCallInterpreter
    jitAvailable = [bool]$familyManifest.jitAvailable
    jitExpected = $jitExpected
    pythonJitEnvironment = if ($jitExpected) { "1" } else { "0" }
    runtimeLockSha256 = $lockSha256
    builderSha256 = $builderSha256
    familyManifestSha256 = Get-Sha256File -Path $familyCompletePath
    pythonDll = @($familyManifest.runtimeOutputs | Where-Object { [string]$_.relativePath -match "^python\d{3,}\.dll$" })[0]
}
if ($variantManifest.jitExpected -and -not $variantManifest.jitAvailable) {
    throw "Variant '$Variant' expects JIT, but its binary family does not provide JIT."
}
$variantManifestPath = Join-Path $variantManifestRoot "$Variant.json"
$variantManifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $variantManifestPath -Encoding UTF8
$variantManifest | ConvertTo-Json -Depth 12
