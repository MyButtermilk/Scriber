param(
    [string]$OutputPath = "",
    [string]$InstallRoot = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $OutputPath) {
    $OutputPath = Join-Path $RepoRoot "benchmarks\results\profile.json"
}
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $RepoRoot "Scriber Install"
}

function Get-CommandText {
    param([string[]]$Command)
    try {
        $result = & $Command[0] @($Command[1..($Command.Count - 1)]) 2>$null
        return (($result | Out-String).Trim())
    } catch {
        return ""
    }
}

function Get-FileHashOrEmpty {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
    $stream = [System.IO.File]::OpenRead($Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToUpperInvariant()
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
}

function Get-JsonHash {
    param([object]$Value)
    $json = $Value | ConvertTo-Json -Depth 20 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-NormalizedFileVersion {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
    $versionInfo = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($Path)
    $candidate = [string]$versionInfo.ProductVersion
    if (-not $candidate) {
        $candidate = [string]$versionInfo.FileVersion
    }
    if ($candidate -match "^(\d+\.\d+\.\d+)") {
        return $Matches[1]
    }
    return $candidate.Trim()
}

$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$gpu = @(Get-CimInstance Win32_VideoController | Sort-Object Name, DriverVersion | ForEach-Object {
    [pscustomobject]@{
        name = $_.Name
        driverVersion = $_.DriverVersion
        adapterRam = $_.AdapterRAM
    }
})
$memoryBytes = [int64]$os.TotalVisibleMemorySize * 1024
$power = Get-CommandText @("powercfg", "/getactivescheme")
$battery = @(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Sort-Object Name, DeviceID | ForEach-Object {
    [pscustomobject]@{
        name = $_.Name
        batteryStatus = $_.BatteryStatus
        estimatedChargeRemaining = $_.EstimatedChargeRemaining
    }
})
$batteryIdentity = @($battery | ForEach-Object {
    [pscustomobject]@{
        name = $_.name
        batteryStatus = $_.batteryStatus
    }
})

$screens = @()
try {
    Add-Type -AssemblyName System.Windows.Forms
    $screens = @([System.Windows.Forms.Screen]::AllScreens | Sort-Object DeviceName | ForEach-Object {
        [pscustomobject]@{
            deviceName = $_.DeviceName
            primary = $_.Primary
            bounds = [pscustomobject]@{
                x = $_.Bounds.X
                y = $_.Bounds.Y
                width = $_.Bounds.Width
                height = $_.Bounds.Height
            }
            workingArea = [pscustomobject]@{
                x = $_.WorkingArea.X
                y = $_.WorkingArea.Y
                width = $_.WorkingArea.Width
                height = $_.WorkingArea.Height
            }
        }
    })
} catch {
    $screens = @()
}

$audio = @(Get-CimInstance Win32_SoundDevice -ErrorAction SilentlyContinue | Sort-Object Name, Manufacturer, DeviceID | ForEach-Object {
    [pscustomobject]@{
        name = $_.Name
        manufacturer = $_.Manufacturer
        status = $_.Status
    }
})

$pythonVersion = ""
$nodeVersion = Get-CommandText @("node", "--version")
$desktopExe = Join-Path $InstallRoot "scriber-desktop.exe"
$backendExe = Join-Path $InstallRoot "backend\scriber-backend.exe"
$audioSidecarExe = Join-Path $InstallRoot "scriber-audio-sidecar.exe"
$packageJsonPath = Join-Path $RepoRoot "Frontend\package.json"
$expectedAppVersion = ""
if (Test-Path -LiteralPath $packageJsonPath -PathType Leaf) {
    $expectedAppVersion = [string]((Get-Content -LiteralPath $packageJsonPath -Raw | ConvertFrom-Json).version)
}
$desktopProductVersion = Get-NormalizedFileVersion -Path $desktopExe
$backendProductVersion = Get-NormalizedFileVersion -Path $backendExe
$audioSidecarProductVersion = Get-NormalizedFileVersion -Path $audioSidecarExe
$binaryVersionMatchesSource = (
    [bool]$expectedAppVersion -and
    $desktopProductVersion -eq $expectedAppVersion -and
    $audioSidecarProductVersion -eq $expectedAppVersion
)
$attestationScript = Join-Path $RepoRoot "scripts\perf\runtime_attestation.py"
$runtimeAttestation = $null
$runtimeAttestationChecked = $false
$runtimeAttestationExitCode = -1
$pythonCommand = if ($Python) {
    Get-Command $Python -ErrorAction SilentlyContinue
} elseif ($env:SCRIBER_PYTHON) {
    Get-Command $env:SCRIBER_PYTHON -ErrorAction SilentlyContinue
} else {
    Get-Command python.exe -ErrorAction SilentlyContinue
}
if ($pythonCommand) {
    $pythonVersion = Get-CommandText @($pythonCommand.Source, "--version")
}
if ($pythonCommand -and (Test-Path -LiteralPath $attestationScript -PathType Leaf)) {
    $runtimeAttestationChecked = $true
    $attestationOutput = @(& $pythonCommand.Source $attestationScript verify `
        --repo-root $RepoRoot `
        --install-root $InstallRoot 2>$null)
    $runtimeAttestationExitCode = $LASTEXITCODE
    try {
        $runtimeAttestation = (($attestationOutput -join "`n") | ConvertFrom-Json)
    } catch {
        $runtimeAttestation = $null
    }
}
$runtimeAttestationValid = (
    $runtimeAttestationExitCode -eq 0 -and
    $null -ne $runtimeAttestation -and
    [bool]$runtimeAttestation.ok
)
$runtimeAttestationErrorCodes = @()
if ($runtimeAttestation -and $runtimeAttestation.errors) {
    $runtimeAttestationErrorCodes = @($runtimeAttestation.errors | ForEach-Object { [string]$_.code })
}
$buildAttestationId = if ($runtimeAttestationValid) { [string]$runtimeAttestation.attestationId } else { "" }
$scorerPath = Join-Path $RepoRoot "scripts\perf\evaluator\local_wux.py"
$scorerHash = Get-FileHashOrEmpty -Path $scorerPath
$evaluatorFiles = @(
    (Join-Path $RepoRoot "scripts\perf\run.ps1"),
    (Join-Path $RepoRoot "scripts\perf\benchmark_lint.py"),
    (Join-Path $RepoRoot "scripts\perf\doctor.py"),
    (Join-Path $RepoRoot "scripts\perf\runtime_attestation.py"),
    (Join-Path $RepoRoot "benchmarks\windows\profile.ps1"),
    (Join-Path $RepoRoot "benchmarks\windows\endpoint_probe.py"),
    (Join-Path $RepoRoot "benchmarks\windows\app_ux_collector.py"),
    (Join-Path $RepoRoot "benchmarks\windows\app_ux_lifecycle_import.schema.json"),
    (Join-Path $RepoRoot "benchmarks\windows\app_action.ps1"),
    (Join-Path $RepoRoot "benchmarks\windows\app_observer.ps1"),
    (Join-Path $RepoRoot "benchmarks\windows\trace_collector.py"),
    $scorerPath
)
$evaluatorHashSource = @($evaluatorFiles | ForEach-Object { Get-FileHashOrEmpty -Path $_ }) -join "|"
$commit = Get-CommandText @("git", "rev-parse", "HEAD")
$baselinePath = Join-Path $RepoRoot "benchmarks\results\baseline.json"
$baselineSha256 = Get-FileHashOrEmpty -Path $baselinePath
$baselineId = ""
if (Test-Path -LiteralPath $baselinePath -PathType Leaf) {
    try {
        $baselineId = [string]((Get-Content -LiteralPath $baselinePath -Raw | ConvertFrom-Json).baselineId)
    } catch {
        $baselineId = ""
    }
}
$windowsIdentity = [pscustomobject]@{
    caption = $os.Caption
    version = $os.Version
    buildNumber = $os.BuildNumber
    architecture = $os.OSArchitecture
}
$cpuIdentity = [pscustomobject]@{
    name = $cpu.Name
    logicalProcessors = $cpu.NumberOfLogicalProcessors
    cores = $cpu.NumberOfCores
    maxClockMhz = $cpu.MaxClockSpeed
}
$providerIdentity = [pscustomobject]@{
    defaultStt = $env:SCRIBER_DEFAULT_STT
    sonioxMode = $env:SCRIBER_SONIOX_MODE
    azureMaiRegion = $env:SCRIBER_AZURE_MAI_REGION
    azureMaiModel = $env:SCRIBER_AZURE_MAI_MODEL
    locale = $env:SCRIBER_LANGUAGE
}
$azureCaptureTimeRaw = [string]$env:SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3
$azureCaptureTimeEnabled = (
    $azureCaptureTimeRaw -and
    $azureCaptureTimeRaw.Trim().ToLowerInvariant() -notin @("0", "false", "no", "off")
)
$speechmaticsCaptureTimeRaw = [string]$env:SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV
$speechmaticsCaptureTimeEnabled = (
    $speechmaticsCaptureTimeRaw -and
    $speechmaticsCaptureTimeRaw.Trim().ToLowerInvariant() -notin @("0", "false", "no", "off")
)
$providerCandidate = [pscustomobject]@{
    azureMaiCaptureTimeMp3 = if ($azureCaptureTimeEnabled) { "enabled" } else { "disabled" }
    speechmaticsCaptureTimeWav = if ($speechmaticsCaptureTimeEnabled) { "enabled" } else { "disabled" }
}
$networkAdapters = @(Get-CimInstance Win32_NetworkAdapterConfiguration | Where-Object { $_.IPEnabled } | Sort-Object Description, SettingID | ForEach-Object {
    [pscustomobject]@{
        description = $_.Description
        dhcpEnabled = $_.DHCPEnabled
    }
})

# profile_id identifies only the comparable machine/runtime environment. Build,
# evaluator, attestation, timestamps, and battery charge are recorded separately
# so a candidate build can still be compared on the same environment profile.
$environmentIdentity = [ordered]@{
    schemaVersion = 1
    windows = $windowsIdentity
    cpu = $cpuIdentity
    ramBytes = $memoryBytes
    gpu = $gpu
    powerScheme = $power
    battery = $batteryIdentity
    monitors = $screens
    audioDevices = $audio
    micBlockSize = $env:SCRIBER_MIC_BLOCK_SIZE
    pythonVersion = $pythonVersion
    nodeVersion = $nodeVersion
    productionBuildMode = "packaged-tauri"
    tauriVersion = ""
    webview2Version = ""
    provider = $providerIdentity
    textInjectionMethod = $env:SCRIBER_INJECT_METHOD
    networkAdapters = $networkAdapters
}
$profileId = Get-JsonHash -Value $environmentIdentity

$payloadNoId = [ordered]@{
    schemaVersion = 1
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    environmentProfileSchemaVersion = 1
    windows = $windowsIdentity
    cpu = $cpuIdentity
    ramBytes = $memoryBytes
    gpu = $gpu
    powerScheme = $power
    battery = $battery
    monitors = $screens
    audioDevices = $audio
    micBlockSize = $env:SCRIBER_MIC_BLOCK_SIZE
    pythonVersion = $pythonVersion
    nodeVersion = $nodeVersion
    productionBuildMode = "packaged-tauri"
    tauriVersion = ""
    webview2Version = ""
    scriberCommit = $commit
    expectedAppVersion = $expectedAppVersion
    desktopProductVersion = $desktopProductVersion
    backendProductVersion = $backendProductVersion
    audioSidecarProductVersion = $audioSidecarProductVersion
    binaryVersionMatchesSource = [bool]$binaryVersionMatchesSource
    buildAttestationId = $buildAttestationId
    runtimeAttestationChecked = [bool]$runtimeAttestationChecked
    runtimeAttestationExitCode = [int]$runtimeAttestationExitCode
    runtimeAttestationValid = [bool]$runtimeAttestationValid
    runtimeAttestationId = if ($runtimeAttestation) { [string]$runtimeAttestation.attestationId } else { "" }
    runtimeAttestationManifestSha256 = if ($runtimeAttestation) { [string]$runtimeAttestation.manifestSha256 } else { "" }
    runtimeAttestationSourceContentSha256 = if ($runtimeAttestation) { [string]$runtimeAttestation.sourceContentSha256 } else { "" }
    runtimeAttestationErrorCodes = @($runtimeAttestationErrorCodes)
    desktopSha256 = Get-FileHashOrEmpty -Path $desktopExe
    backendSha256 = Get-FileHashOrEmpty -Path $backendExe
    audioSidecarSha256 = Get-FileHashOrEmpty -Path $audioSidecarExe
    baselineId = $baselineId
    baselineSha256 = $baselineSha256
    provider = $providerIdentity
    providerCandidate = $providerCandidate
    textInjectionMethod = $env:SCRIBER_INJECT_METHOD
    networkAdapters = $networkAdapters
    evaluatorVersion = 1
    evaluatorHash = (Get-JsonHash -Value $evaluatorHashSource)
    scorerHash = $scorerHash
}

$payload = [ordered]@{}
foreach ($key in $payloadNoId.Keys) {
    $payload[$key] = $payloadNoId[$key]
}
$payload["profile_id"] = $profileId

New-Item -ItemType Directory -Force -Path (Split-Path $OutputPath) | Out-Null
$payload | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Output "PROFILE_ID $profileId"
