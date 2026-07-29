<#
.SYNOPSIS
Imports the CI Authenticode certificate without exposing its material.

.DESCRIPTION
Reads the base64 PFX and password from environment variables, validates that
the imported certificate is RSA-based and permits code signing, then exports
only its public thumbprint to GITHUB_ENV for subsequent release steps.
#>

param(
    [string]$CertificateEnvironmentVariable = "SCRIBER_WINDOWS_CERTIFICATE_BASE64",
    [string]$PasswordEnvironmentVariable = "SCRIBER_WINDOWS_CERTIFICATE_PASSWORD"
)

$ErrorActionPreference = "Stop"

$certificateBase64 = [string][Environment]::GetEnvironmentVariable($CertificateEnvironmentVariable)
$certificatePassword = [string][Environment]::GetEnvironmentVariable($PasswordEnvironmentVariable)
if ([string]::IsNullOrWhiteSpace($certificateBase64)) {
    throw "$CertificateEnvironmentVariable is required for official Windows releases."
}
if ([string]::IsNullOrEmpty($certificatePassword)) {
    throw "$PasswordEnvironmentVariable is required for official Windows releases."
}
if ([string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) {
    throw "GITHUB_ENV is required to publish the imported certificate thumbprint."
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("scriber-signing-" + [Guid]::NewGuid().ToString("N"))
$base64Path = Join-Path $temporaryRoot "certificate.txt"
$pfxPath = Join-Path $temporaryRoot "certificate.pfx"
$importedCertificate = $null
try {
    New-Item -ItemType Directory -Path $temporaryRoot -ErrorAction Stop | Out-Null
    [System.IO.File]::WriteAllText(
        $base64Path,
        $certificateBase64.Trim(),
        [System.Text.Encoding]::ASCII
    )
    & certutil.exe -f -decode $base64Path $pfxPath *> $null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pfxPath -PathType Leaf)) {
        throw "The Windows signing certificate secret is not valid base64 PFX data."
    }

    $securePassword = ConvertTo-SecureString -String $certificatePassword -AsPlainText -Force
    $codeSigningOid = "1.3.6.1.5.5.7.3.3"
    $importedCertificates = @(
        Import-PfxCertificate `
            -FilePath $pfxPath `
            -CertStoreLocation "Cert:\CurrentUser\My" `
            -Password $securePassword `
            -ErrorAction Stop
    )
    $codeSigningCertificates = @(
        $importedCertificates |
            Where-Object {
                $_.HasPrivateKey -and
                @(
                    $_.Extensions |
                        Where-Object {
                            $_ -is [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]
                        } |
                        ForEach-Object { $_.EnhancedKeyUsages } |
                        ForEach-Object { $_ } |
                        Where-Object { $_.Value -eq $codeSigningOid }
                ).Count -gt 0
            }
    )
    if ($codeSigningCertificates.Count -ne 1) {
        throw "The PFX must contain exactly one certificate with a private code-signing key."
    }
    $importedCertificate = $codeSigningCertificates[0]
    if ($importedCertificate.PublicKey.Oid.Value -ne "1.2.840.113549.1.1.1") {
        throw "Official Windows releases require an RSA code-signing certificate."
    }
    $hasCodeSigningUsage = @(
        $importedCertificate.Extensions |
            Where-Object { $_ -is [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension] } |
            ForEach-Object { $_.EnhancedKeyUsages } |
            ForEach-Object { $_ } |
            Where-Object { $_.Value -eq $codeSigningOid }
    ).Count -gt 0
    if (-not $hasCodeSigningUsage) {
        throw "Imported certificate does not permit code signing."
    }

    $thumbprint = ([string]$importedCertificate.Thumbprint -replace '\s', '').ToUpperInvariant()
    if ($thumbprint -notmatch '^[0-9A-F]{40}$') {
        throw "Imported certificate returned an invalid SHA-1 thumbprint."
    }
    "SCRIBER_WINDOWS_CERTIFICATE_THUMBPRINT=$thumbprint" |
        Out-File -LiteralPath $env:GITHUB_ENV -Append -Encoding utf8
    Write-Host "Windows code-signing certificate imported and validated."
} finally {
    if (Test-Path -LiteralPath $base64Path -PathType Leaf) {
        Remove-Item -LiteralPath $base64Path -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $pfxPath -PathType Leaf) {
        Remove-Item -LiteralPath $pfxPath -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $temporaryRoot -PathType Container) {
        Remove-Item -LiteralPath $temporaryRoot -Force -ErrorAction SilentlyContinue
    }
    $certificateBase64 = $null
    $certificatePassword = $null
}
