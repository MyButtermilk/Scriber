<# Read-only, redacted readiness probe for the real Meeting release matrix. #>
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$OutputPath = ""
)
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path $RepoRoot).Path

function Test-AnyPath([string[]]$Paths) {
    foreach ($candidate in $Paths) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $true }
    }
    return $false
}

function Test-ConfiguredKey([string]$Name) {
    foreach ($scope in @("Process", "User", "Machine")) {
        if ([Environment]::GetEnvironmentVariable($Name, $scope)) { return $true }
    }
    foreach ($envFile in @((Join-Path $RepoRoot ".env"), (Join-Path $RepoRoot "config\.env"))) {
        if (Test-Path -LiteralPath $envFile -PathType Leaf) {
            $pattern = "(?m)^\s*" + [regex]::Escape($Name) + "\s*=\s*(?!\s*(?:#|$)).+"
            if ([regex]::IsMatch((Get-Content -LiteralPath $envFile -Raw), $pattern)) { return $true }
        }
    }
    return $false
}

$displayNames = [System.Collections.Generic.List[string]]::new()
foreach ($root in @(
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
)) {
    try {
        foreach ($item in Get-ItemProperty -Path $root -ErrorAction SilentlyContinue) {
            if ($item.DisplayName) { [void]$displayNames.Add(([string]$item.DisplayName).ToLowerInvariant()) }
        }
    } catch {}
}
$appxTeams = $false
try { $appxTeams = [bool](Get-AppxPackage -Name "MSTeams" -ErrorAction SilentlyContinue | Select-Object -First 1) } catch {}
$teamsDetected = $appxTeams -or [bool]($displayNames | Where-Object { $_ -match "microsoft teams|^teams$" } | Select-Object -First 1)
$zoomDetected = [bool]($displayNames | Where-Object { $_ -match "zoom workplace|zoom meetings|^zoom$" } | Select-Object -First 1) -or (Test-AnyPath @(
    (Join-Path $env:APPDATA "Zoom\bin\Zoom.exe"),
    (Join-Path $env:LOCALAPPDATA "Zoom\bin\Zoom.exe")
))
$programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
$chromeDetected = Test-AnyPath @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path $programFilesX86 "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)

$endpointLabels = @()
try {
    $endpointLabels = @(Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue | ForEach-Object { ([string]$_.FriendlyName).ToLowerInvariant() })
} catch {}
$microphoneCount = @($endpointLabels | Where-Object { $_ -match "microphone|mikrofon|mic array|headset" }).Count
$renderCount = @($endpointLabels | Where-Object { $_ -match "speaker|lautsprecher|headphone|kopfhörer|headset|audio" }).Count
$bluetoothAvailable = [bool]($endpointLabels | Where-Object { $_ -match "bluetooth|hands-free|handsfree|stereo" } | Select-Object -First 1)
$usbAvailable = [bool]($endpointLabels | Where-Object { $_ -match "usb" } | Select-Object -First 1)
$headsetAvailable = [bool]($endpointLabels | Where-Object { $_ -match "headset|headphone|kopfhörer" } | Select-Object -First 1)
$speakerAvailable = [bool]($endpointLabels | Where-Object { $_ -match "speaker|lautsprecher" } | Select-Object -First 1)

$versionSource = Get-Content -LiteralPath (Join-Path $RepoRoot "src\version.py") -Raw
$versionMatch = [regex]::Match($versionSource, '(?m)^__version__\s*=\s*"([^"]+)"')
$version = if ($versionMatch.Success) { $versionMatch.Groups[1].Value } else { "unknown" }
$installerName = "Scriber_" + $version + "_x64-setup.exe"
$installer = Join-Path $RepoRoot ("Frontend\src-tauri\target\release\bundle\nsis\" + $installerName)
$installerAvailable = Test-Path -LiteralPath $installer -PathType Leaf
$installerSigned = $false
if ($installerAvailable) {
    try { $installerSigned = (Get-AuthenticodeSignature -LiteralPath $installer).Status -eq [System.Management.Automation.SignatureStatus]::Valid } catch {}
}

$outlookConfigured = Test-ConfiguredKey "SCRIBER_OUTLOOK_CLIENT_ID"
$profiles = [ordered]@{
    teamsLaptopSpeakerphone = [bool]($teamsDetected -and $speakerAvailable -and $microphoneCount -gt 0)
    zoomWiredHeadset = [bool]($zoomDetected -and $headsetAvailable)
    meetBluetoothHeadset = [bool]($chromeDetected -and $bluetoothAvailable)
    teamsUsbMicrophone = [bool]($teamsDetected -and $usbAvailable)
    zoomDefaultDeviceSwitch = [bool]($zoomDetected -and $renderCount -gt 1 -and $microphoneCount -gt 1)
    outlookWorkSchool = $outlookConfigured
    outlookMicrosoftPersonal = $outlookConfigured
    signedRelease = $installerSigned
}

$payload = [ordered]@{
    schemaVersion = 1
    kind = "scriber-meeting-release-readiness-probe"
    generatedAtUtc = [DateTime]::UtcNow.ToString("o")
    ok = $true
    privacy = [ordered]@{
        rawEndpointIdsEmitted = $false
        deviceNamesEmitted = $false
        accountDetailsEmitted = $false
        secretValuesEmitted = $false
    }
    meetingClients = [ordered]@{
        teamsDesktop = $teamsDetected
        zoomDesktop = $zoomDetected
        googleChrome = $chromeDetected
    }
    audioCategories = [ordered]@{
        activeEndpointCount = $endpointLabels.Count
        microphoneEndpointCount = $microphoneCount
        renderEndpointCount = $renderCount
        laptopOrExternalSpeakers = $speakerAvailable
        wiredOrWirelessHeadset = $headsetAvailable
        bluetooth = $bluetoothAvailable
        usb = $usbAvailable
    }
    configuration = [ordered]@{
        outlookPublicClientConfigured = $outlookConfigured
        updaterSigningKeyConfigured = (Test-ConfiguredKey "TAURI_SIGNING_PRIVATE_KEY")
        authenticodeSigningConfigured = [bool]((Test-ConfiguredKey "AZURE_KEY_VAULT_URL") -or (Test-ConfiguredKey "WINDOWS_CERTIFICATE_THUMBPRINT"))
    }
    build = [ordered]@{
        currentInstallerAvailable = $installerAvailable
        currentInstallerAuthenticodeValid = $installerSigned
    }
    runnableProfileHints = $profiles
    readyProfileHintCount = @($profiles.GetEnumerator() | Where-Object { $_.Value }).Count
    note = "Readiness hints are not release evidence. A profile passes only after its real scenario report and hashed artifacts validate."
}

$json = $payload | ConvertTo-Json -Depth 8
if ($OutputPath) {
    $resolvedOutput = if ([System.IO.Path]::IsPathRooted($OutputPath)) { $OutputPath } else { Join-Path $RepoRoot $OutputPath }
    $resolvedOutput = [System.IO.Path]::GetFullPath($resolvedOutput)
    $tmpRoot = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot "tmp"))
    if (-not $resolvedOutput.StartsWith($tmpRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Readiness output must stay under the repository tmp directory."
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $resolvedOutput) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($resolvedOutput, $json, $encoding)
}
$json
