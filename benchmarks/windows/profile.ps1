param(
    [string]$OutputPath = "",
    [string]$InstallRoot = ""
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

$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$gpu = @(Get-CimInstance Win32_VideoController | ForEach-Object {
    [pscustomobject]@{
        name = $_.Name
        driverVersion = $_.DriverVersion
        adapterRam = $_.AdapterRAM
    }
})
$memoryBytes = [int64]$os.TotalVisibleMemorySize * 1024
$power = Get-CommandText @("powercfg", "/getactivescheme")
$battery = @(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{
        name = $_.Name
        batteryStatus = $_.BatteryStatus
        estimatedChargeRemaining = $_.EstimatedChargeRemaining
    }
})

$screens = @()
try {
    Add-Type -AssemblyName System.Windows.Forms
    $screens = @([System.Windows.Forms.Screen]::AllScreens | ForEach-Object {
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

$audio = @(Get-CimInstance Win32_SoundDevice -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{
        name = $_.Name
        manufacturer = $_.Manufacturer
        status = $_.Status
    }
})

$pythonVersion = Get-CommandText @("python", "--version")
$nodeVersion = Get-CommandText @("node", "--version")
$desktopExe = Join-Path $InstallRoot "scriber-desktop.exe"
$backendExe = Join-Path $InstallRoot "backend\scriber-backend.exe"
$audioSidecarExe = Join-Path $InstallRoot "scriber-audio-sidecar.exe"
$evaluatorFiles = @(
    (Join-Path $RepoRoot "scripts\perf\run.ps1"),
    (Join-Path $RepoRoot "scripts\perf\benchmark_lint.py"),
    (Join-Path $RepoRoot "scripts\perf\doctor.py"),
    (Join-Path $RepoRoot "benchmarks\windows\profile.ps1")
)
$evaluatorHashSource = @($evaluatorFiles | ForEach-Object { Get-FileHashOrEmpty -Path $_ }) -join "|"
$commit = Get-CommandText @("git", "rev-parse", "HEAD")

$payloadNoId = [ordered]@{
    schemaVersion = 1
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    windows = [pscustomobject]@{
        caption = $os.Caption
        version = $os.Version
        buildNumber = $os.BuildNumber
        architecture = $os.OSArchitecture
    }
    cpu = [pscustomobject]@{
        name = $cpu.Name
        logicalProcessors = $cpu.NumberOfLogicalProcessors
        cores = $cpu.NumberOfCores
        maxClockMhz = $cpu.MaxClockSpeed
    }
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
    installRoot = (Resolve-Path -LiteralPath $InstallRoot -ErrorAction SilentlyContinue).Path
    desktopSha256 = Get-FileHashOrEmpty -Path $desktopExe
    backendSha256 = Get-FileHashOrEmpty -Path $backendExe
    audioSidecarSha256 = Get-FileHashOrEmpty -Path $audioSidecarExe
    provider = [pscustomobject]@{
        defaultStt = $env:SCRIBER_DEFAULT_STT
        sonioxMode = $env:SCRIBER_SONIOX_MODE
        azureMaiRegion = $env:SCRIBER_AZURE_MAI_REGION
        azureMaiModel = $env:SCRIBER_AZURE_MAI_MODEL
        locale = $env:SCRIBER_LANGUAGE
    }
    textInjectionMethod = $env:SCRIBER_INJECT_METHOD
    networkAdapters = @(Get-CimInstance Win32_NetworkAdapterConfiguration | Where-Object { $_.IPEnabled } | ForEach-Object {
        [pscustomobject]@{
            description = $_.Description
            dhcpEnabled = $_.DHCPEnabled
        }
    })
    evaluatorVersion = 1
    evaluatorHash = (Get-JsonHash -Value $evaluatorHashSource)
}

$profileId = Get-JsonHash -Value $payloadNoId
$payload = [ordered]@{}
foreach ($key in $payloadNoId.Keys) {
    $payload[$key] = $payloadNoId[$key]
}
$payload["profile_id"] = $profileId

New-Item -ItemType Directory -Force -Path (Split-Path $OutputPath) | Out-Null
$payload | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Output "PROFILE_ID $profileId"
