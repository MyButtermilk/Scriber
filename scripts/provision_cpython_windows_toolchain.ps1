param(
    [string]$LockPath = (Join-Path $PSScriptRoot "..\packaging\cpython-windows-runtime-input-lock-v1.json"),
    [string]$InputCacheRoot = (Join-Path $PSScriptRoot "..\build\runtime-input-cache"),
    [string]$VisualStudioInstallPath = (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2022\BuildTools"),
    [switch]$DownloadInputs,
    [switch]$InstallVisualStudio,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-Sha256File {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-LockedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$LockEntry,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    if ([int64]$item.Length -ne [int64]$LockEntry.sizeBytes) {
        throw "$Label size mismatch: expected $($LockEntry.sizeBytes), found $($item.Length)"
    }
    $actualSha256 = Get-Sha256File -Path $Path
    if ($actualSha256 -cne [string]$LockEntry.sha256) {
        throw "$Label SHA-256 mismatch: expected $($LockEntry.sha256), found $actualSha256"
    }
}

function Save-LockedInput {
    param(
        [Parameter(Mandatory = $true)]$LockEntry,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $destination = Join-Path $InputCacheRoot ([string]$LockEntry.fileName)
    if (Test-Path -LiteralPath $destination -PathType Leaf) {
        Assert-LockedFile -Path $destination -LockEntry $LockEntry -Label $Label
        return $destination
    }
    if (-not $DownloadInputs) {
        throw "$Label is missing and -DownloadInputs was not supplied: $destination"
    }
    $partial = "$destination.partial"
    if (Test-Path -LiteralPath $partial) {
        Remove-Item -LiteralPath $partial -Force
    }
    try {
        & curl.exe --location --fail --retry 3 --output $partial ([string]$LockEntry.url)
        if ($LASTEXITCODE -ne 0) {
            throw "curl.exe failed while downloading $Label with exit code $LASTEXITCODE"
        }
        Assert-LockedFile -Path $partial -LockEntry $LockEntry -Label $Label
        Move-Item -LiteralPath $partial -Destination $destination
    } finally {
        if (Test-Path -LiteralPath $partial) {
            Remove-Item -LiteralPath $partial -Force
        }
    }
    return $destination
}

function Get-VisualStudioInstance {
    param(
        [Parameter(Mandatory = $true)][string]$VsWhere,
        [Parameter(Mandatory = $true)]$VisualStudioLock,
        [bool]$RequireComponents = $true
    )

    $arguments = @(
        "-products",
        [string]$VisualStudioLock.product,
        "-version",
        "[17.14,17.15)"
    )
    if ($RequireComponents) {
        $arguments += "-requires"
        $arguments += @($VisualStudioLock.components)
    }
    $arguments += @("-format", "json", "-utf8")
    $json = @(& $VsWhere @arguments) -join [Environment]::NewLine
    if (-not $json.Trim()) {
        return $null
    }
    $instances = @($json | ConvertFrom-Json)
    return @($instances | Where-Object {
        [string]$_.installationVersion -ceq [string]$VisualStudioLock.installationVersion -and
        [bool]$_.isComplete -and
        -not [bool]$_.isRebootRequired
    }) | Select-Object -First 1
}

$LockPath = [IO.Path]::GetFullPath($LockPath)
$InputCacheRoot = [IO.Path]::GetFullPath($InputCacheRoot)
$VisualStudioInstallPath = [IO.Path]::GetFullPath($VisualStudioInstallPath)
$lock = Get-Content -LiteralPath $LockPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (
    [string]$lock.lockContract -cne "ScriberCPythonWindowsRuntimeInputLockV1" -or
    [int]$lock.schemaVersion -ne 1
) {
    throw "Unexpected CPython Windows runtime input lock contract."
}

New-Item -ItemType Directory -Force -Path $InputCacheRoot | Out-Null
$sourcePath = Save-LockedInput -LockEntry $lock.customBuildInputs.source -Label "CPython source"
$llvmPath = Save-LockedInput -LockEntry $lock.customBuildInputs.llvm -Label "LLVM archive"
$visualStudioLock = $lock.customBuildInputs.visualStudio
$bootstrapperPath = Save-LockedInput -LockEntry $visualStudioLock.bootstrapper -Label "Visual Studio bootstrapper"

$bootstrapper = Get-Item -LiteralPath $bootstrapperPath
if ([string]$bootstrapper.VersionInfo.ProductVersion -cne [string]$visualStudioLock.displayVersion) {
    throw "Visual Studio bootstrapper product version mismatch."
}
if ([string]$bootstrapper.VersionInfo.FileVersion -cne [string]$visualStudioLock.installationVersion) {
    throw "Visual Studio bootstrapper file version mismatch."
}
$signature = Get-AuthenticodeSignature -LiteralPath $bootstrapperPath
if (
    $signature.Status -ne [Management.Automation.SignatureStatus]::Valid -or
    $null -eq $signature.SignerCertificate -or
    [string]$signature.SignerCertificate.Subject -cne [string]$visualStudioLock.bootstrapper.signerSubject
) {
    throw "Visual Studio bootstrapper does not have the locked valid Microsoft signature."
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$instance = if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
    Get-VisualStudioInstance -VsWhere $vswhere -VisualStudioLock $visualStudioLock
} else {
    $null
}

if ($null -eq $instance -and $InstallVisualStudio) {
    if ($VerifyOnly) {
        throw "-VerifyOnly cannot install Visual Studio."
    }
    $installedInstance = if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
        Get-VisualStudioInstance -VsWhere $vswhere -VisualStudioLock $visualStudioLock -RequireComponents $false
    } else {
        $null
    }
    $installerPath = $bootstrapperPath
    $arguments = @("--quiet", "--norestart", "--installPath", $VisualStudioInstallPath)
    if ($null -ne $installedInstance) {
        if ([string]$installedInstance.installationPath -cne $VisualStudioInstallPath) {
            throw "The locked Visual Studio version exists at an unexpected path: $($installedInstance.installationPath)"
        }
        $installerPath = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\setup.exe"
        if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
            throw "Visual Studio setup.exe is missing for the required component modification."
        }
        $arguments = @("modify") + $arguments
    } else {
        $arguments = @("--wait", "--nocache") + $arguments
    }
    foreach ($component in $visualStudioLock.components) {
        $arguments += @("--add", [string]$component)
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $startParameters = @{
        FilePath = $installerPath
        ArgumentList = $arguments
        Wait = $true
        PassThru = $true
        WindowStyle = "Hidden"
    }
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $startParameters.Verb = "RunAs"
    }
    $process = Start-Process @startParameters
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "Visual Studio Build Tools installation failed with exit code $($process.ExitCode)."
    }
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        throw "Visual Studio Installer completed without providing vswhere.exe."
    }
    $instance = Get-VisualStudioInstance -VsWhere $vswhere -VisualStudioLock $visualStudioLock
}

if ($null -eq $instance) {
    throw "The locked Visual Studio Build Tools instance and components are unavailable."
}
if ([string]$instance.installationPath -cne $VisualStudioInstallPath) {
    throw "Visual Studio is installed at an unexpected path: $($instance.installationPath)"
}

$msbuild = Join-Path ([string]$instance.installationPath) "MSBuild\Current\Bin\MSBuild.exe"
$sdkVersion = [string]$lock.customBuildInputs.windowsSdk.directoryVersion
$sdkRc = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin\$sdkVersion\x64\rc.exe"
if (-not (Test-Path -LiteralPath $msbuild -PathType Leaf)) {
    throw "The locked Visual Studio instance is missing MSBuild.exe."
}
if (-not (Test-Path -LiteralPath $sdkRc -PathType Leaf)) {
    throw "The locked Windows SDK is missing rc.exe: $sdkRc"
}

[ordered]@{
    ok = $true
    lockSha256 = Get-Sha256File -Path $LockPath
    source = [ordered]@{
        path = $sourcePath
        sha256 = Get-Sha256File -Path $sourcePath
    }
    llvm = [ordered]@{
        path = $llvmPath
        sha256 = Get-Sha256File -Path $llvmPath
    }
    visualStudio = [ordered]@{
        installationPath = [string]$instance.installationPath
        installationVersion = [string]$instance.installationVersion
        msbuildSha256 = Get-Sha256File -Path $msbuild
    }
    windowsSdk = [ordered]@{
        directoryVersion = $sdkVersion
        rcSha256 = Get-Sha256File -Path $sdkRc
    }
} | ConvertTo-Json -Depth 8
