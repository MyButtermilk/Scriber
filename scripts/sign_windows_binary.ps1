<#
.SYNOPSIS
Authenticode-signs one Windows binary with an imported code-signing certificate.

.DESCRIPTION
Uses SignTool with SHA-256 and an RFC 3161 HTTPS timestamp, then verifies the
embedded signature, certificate thumbprint, and timestamp before returning.
No certificate material or password is accepted by this script.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Path,

    [Parameter(Mandatory = $true)]
    [string]$CertificateThumbprint,

    [Parameter(Mandatory = $true)]
    [string]$TimestampUrl
)

$ErrorActionPreference = "Stop"

# GitHub's Windows runner can launch Windows PowerShell from pwsh with a mixed
# PSModulePath. Keep the security module on Windows PowerShell's native paths.
if ($PSVersionTable.PSEdition -eq "Desktop") {
    $windowsModulePaths = @(
        (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "WindowsPowerShell\Modules"),
        (Join-Path $env:ProgramFiles "WindowsPowerShell\Modules"),
        (Join-Path $env:SystemRoot "system32\WindowsPowerShell\v1.0\Modules")
    ) | Where-Object { Test-Path -LiteralPath $_ }
    $env:PSModulePath = ($windowsModulePaths -join [System.IO.Path]::PathSeparator)
}
Import-Module Microsoft.PowerShell.Security -ErrorAction Stop

function Resolve-SignTool {
    $command = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
    $sdkBinRoot = Join-Path $programFilesX86 "Windows Kits\10\bin"
    if (Test-Path -LiteralPath $sdkBinRoot -PathType Container) {
        $candidates = @(
            Get-ChildItem -LiteralPath $sdkBinRoot -Directory -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName "x64\signtool.exe" } |
                Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
        )
        if ($candidates.Count -gt 0) {
            return $candidates[0]
        }
    }

    throw "SignTool was not found. Install the Windows SDK signing tools."
}

$resolvedPath = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
    throw "Authenticode target is not a file: $resolvedPath"
}

$normalizedThumbprint = ($CertificateThumbprint -replace '\s', '').ToUpperInvariant()
if ($normalizedThumbprint -notmatch '^[0-9A-F]{40}$') {
    throw "CertificateThumbprint must be exactly 40 hexadecimal characters."
}

$parsedTimestamp = $null
if (
    -not [Uri]::TryCreate($TimestampUrl, [UriKind]::Absolute, [ref]$parsedTimestamp) -or
    $null -eq $parsedTimestamp -or
    $parsedTimestamp.Scheme -ine [Uri]::UriSchemeHttps -or
    [string]::IsNullOrWhiteSpace($parsedTimestamp.Host)
) {
    throw "TimestampUrl must be an absolute HTTPS URL."
}

$existing = Get-AuthenticodeSignature -LiteralPath $resolvedPath
$existingThumbprint = if ($existing.SignerCertificate) {
    ([string]$existing.SignerCertificate.Thumbprint).ToUpperInvariant()
} else {
    ""
}
if (
    [string]$existing.Status -eq "Valid" -and
    $existingThumbprint -eq $normalizedThumbprint -and
    $null -ne $existing.TimeStamperCertificate
) {
    Write-Host "Authenticode target already has the required signature."
    exit 0
}

$signTool = Resolve-SignTool
& $signTool sign `
    /sha1 $normalizedThumbprint `
    /fd SHA256 `
    /tr $parsedTimestamp.AbsoluteUri `
    /td SHA256 `
    $resolvedPath
if ($LASTEXITCODE -ne 0) {
    throw "SignTool failed with exit code $LASTEXITCODE."
}

$signature = Get-AuthenticodeSignature -LiteralPath $resolvedPath
$actualThumbprint = if ($signature.SignerCertificate) {
    ([string]$signature.SignerCertificate.Thumbprint).ToUpperInvariant()
} else {
    ""
}
if ([string]$signature.Status -ne "Valid") {
    throw "Signed binary failed Authenticode validation: $($signature.StatusMessage)"
}
if ($actualThumbprint -ne $normalizedThumbprint) {
    throw "Signed binary certificate thumbprint does not match the requested identity."
}
if ($null -eq $signature.TimeStamperCertificate) {
    throw "Signed binary is missing its RFC 3161 timestamp."
}

Write-Host "Authenticode signature and timestamp verified."
