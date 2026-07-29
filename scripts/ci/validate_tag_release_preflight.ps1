<#
.SYNOPSIS
Fails a v* release before cache restores when required release signing settings
are missing or internally inconsistent.

.DESCRIPTION
Reads the same environment variables consumed by the Windows release workflow.
The script reports only configuration state and never prints key material,
passwords, certificate subjects, or configured file paths.
#>

$ErrorActionPreference = "Stop"

function Get-TrimmedEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    return ([string][Environment]::GetEnvironmentVariable($Name)).Trim()
}

function Assert-OptionalBooleanEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $value = Get-TrimmedEnvironmentValue -Name $Name
    if ($value -and $value -notin @("0", "1")) {
        throw "$Name must be unset, '0', or '1'."
    }
    return $value
}

$requireAuthenticodeValue = Assert-OptionalBooleanEnvironmentValue -Name "SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE"
$requireAuthenticodeTimestampValue = Assert-OptionalBooleanEnvironmentValue -Name "SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP"

$requireAuthenticode = $requireAuthenticodeValue -eq "1"
$requireAuthenticodeTimestamp = $requireAuthenticodeTimestampValue -eq "1"
$authenticodePublisher = Get-TrimmedEnvironmentValue -Name "SCRIBER_AUTHENTICODE_PUBLISHER"

if ($requireAuthenticodeTimestamp -and -not $requireAuthenticode) {
    throw "SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP=1 requires SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE=1."
}
if ($authenticodePublisher -and -not $requireAuthenticode) {
    throw "SCRIBER_AUTHENTICODE_PUBLISHER is only valid when SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE=1."
}

$windowsCertificate = Get-TrimmedEnvironmentValue -Name "SCRIBER_WINDOWS_CERTIFICATE_BASE64"
$windowsCertificatePassword = Get-TrimmedEnvironmentValue -Name "SCRIBER_WINDOWS_CERTIFICATE_PASSWORD"
$authenticodeTimestampUrl = Get-TrimmedEnvironmentValue -Name "SCRIBER_AUTHENTICODE_TIMESTAMP_URL"
if ($requireAuthenticode) {
    if (-not $requireAuthenticodeTimestamp) {
        throw "Authenticode releases require SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP=1."
    }
    if (-not $authenticodePublisher) {
        throw "Authenticode releases require SCRIBER_AUTHENTICODE_PUBLISHER."
    }
    if (-not $windowsCertificate -or -not $windowsCertificatePassword) {
        throw "Authenticode releases require the Windows certificate and password secrets."
    }
    $parsedTimestampUrl = $null
    if (
        -not [Uri]::TryCreate($authenticodeTimestampUrl, [UriKind]::Absolute, [ref]$parsedTimestampUrl) -or
        $null -eq $parsedTimestampUrl -or
        $parsedTimestampUrl.Scheme -ine [Uri]::UriSchemeHttps -or
        [string]::IsNullOrWhiteSpace($parsedTimestampUrl.Host)
    ) {
        throw "SCRIBER_AUTHENTICODE_TIMESTAMP_URL must be an absolute HTTPS URL."
    }
}

$updaterPublicKey = Get-TrimmedEnvironmentValue -Name "SCRIBER_TAURI_UPDATER_PUBLIC_KEY"
$updaterPrivateKey = Get-TrimmedEnvironmentValue -Name "TAURI_SIGNING_PRIVATE_KEY"
$updaterPrivateKeyPath = Get-TrimmedEnvironmentValue -Name "TAURI_SIGNING_PRIVATE_KEY_PATH"
$hasPrivateKey = [bool]($updaterPrivateKey -or $updaterPrivateKeyPath)
$hasUpdaterSigning = [bool]($updaterPublicKey -and $hasPrivateKey)

if (-not $updaterPrivateKey -and $updaterPrivateKeyPath) {
    if (-not (Test-Path -LiteralPath $updaterPrivateKeyPath -PathType Leaf)) {
        throw "TAURI_SIGNING_PRIVATE_KEY_PATH is configured but does not point to an existing file."
    }
}

if (-not $hasUpdaterSigning) {
    throw "Official releases require SCRIBER_TAURI_UPDATER_PUBLIC_KEY and TAURI_SIGNING_PRIVATE_KEY or TAURI_SIGNING_PRIVATE_KEY_PATH."
}

$updaterEndpoint = Get-TrimmedEnvironmentValue -Name "SCRIBER_TAURI_UPDATER_ENDPOINT"
if (-not $updaterEndpoint) {
    $updaterEndpoint = "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json"
}

$parsedEndpoint = $null
$isAbsoluteUri = [Uri]::TryCreate($updaterEndpoint, [UriKind]::Absolute, [ref]$parsedEndpoint)
if (
    -not $isAbsoluteUri -or
    $null -eq $parsedEndpoint -or
    $parsedEndpoint.Scheme -ine [Uri]::UriSchemeHttps -or
    [string]::IsNullOrWhiteSpace($parsedEndpoint.Host)
) {
    throw "SCRIBER_TAURI_UPDATER_ENDPOINT must be an absolute HTTPS URL."
}

if ($requireAuthenticode) {
    Write-Host "Tag release preflight passed: updater signing, Authenticode signing, timestamping, and HTTPS publication are configured."
} else {
    Write-Host "Tag release preflight passed: updater signing and HTTPS publication are configured; Authenticode is not enabled."
}
