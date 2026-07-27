<#
.SYNOPSIS
Validates Authenticode signatures for Windows release artifacts.

.DESCRIPTION
Checks that every provided artifact has a valid Authenticode signature. The
optional publisher and timestamp gates are intended for release CI after the
actual signing step has run.
#>

param(
    [Parameter(Mandatory = $true)]
    [string[]]$Path,

    [string]$ExpectedPublisher = "",

    [switch]$RequireTimestamp,

    [switch]$AllowNotSigned,

    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

# GitHub Actions often invokes Windows PowerShell from pwsh. In that case the
# inherited PSModulePath may contain PowerShell 7 module directories, which can
# make Microsoft.PowerShell.Security fail to load with duplicate type data.
if ($PSVersionTable.PSEdition -eq "Desktop") {
    $windowsModulePaths = @(
        (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "WindowsPowerShell\Modules"),
        (Join-Path $env:ProgramFiles "WindowsPowerShell\Modules"),
        (Join-Path $env:SystemRoot "system32\WindowsPowerShell\v1.0\Modules")
    ) | Where-Object { Test-Path -LiteralPath $_ }
    $env:PSModulePath = ($windowsModulePaths -join [System.IO.Path]::PathSeparator)
}

Import-Module Microsoft.PowerShell.Security -ErrorAction Stop

function Test-Publisher {
    param(
        [AllowNull()]
        [System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate,
        [string]$ExpectedPublisher
    )

    if (-not $ExpectedPublisher) {
        return $true
    }
    if ($null -eq $Certificate) {
        return $false
    }
    return $Certificate.Subject -like "*$ExpectedPublisher*"
}

function Write-Utf8NoBomJson {
    param(
        [string]$Path,
        [string]$Json
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Json, $encoding)
}

$results = @()

foreach ($rawPath in $Path) {
    $resolvedPaths = Resolve-Path -LiteralPath $rawPath -ErrorAction Stop
    foreach ($resolvedPath in $resolvedPaths) {
        $artifactPath = $resolvedPath.ProviderPath
        $signature = Get-AuthenticodeSignature -LiteralPath $artifactPath
        $signerSubject = ""
        $timestampSubject = ""

        if ($null -ne $signature.SignerCertificate) {
            $signerSubject = $signature.SignerCertificate.Subject
        }
        if ($null -ne $signature.TimeStamperCertificate) {
            $timestampSubject = $signature.TimeStamperCertificate.Subject
        }

        $status = [string]$signature.Status
        $statusAllowed = $status -eq "Valid" -or ($AllowNotSigned -and $status -eq "NotSigned")
        if (-not $statusAllowed) {
            throw "Authenticode signature for '$artifactPath' is '$($signature.Status)': $($signature.StatusMessage)"
        }
        if (
            $status -eq "Valid" -and
            -not (Test-Publisher -Certificate $signature.SignerCertificate -ExpectedPublisher $ExpectedPublisher)
        ) {
            throw "Authenticode publisher for '$artifactPath' does not match expected publisher '$ExpectedPublisher'. Actual subject: '$signerSubject'."
        }
        if ($status -eq "Valid" -and $RequireTimestamp -and $null -eq $signature.TimeStamperCertificate) {
            throw "Authenticode timestamp is required for '$artifactPath' but no timestamp certificate was found."
        }

        $artifactItem = Get-Item -LiteralPath $artifactPath
        $results += [pscustomobject]@{
            path = $artifactPath
            status = $status
            sizeBytes = [int64]$artifactItem.Length
            sha256 = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
            signerSubject = $signerSubject
            timestampSubject = $timestampSubject
        }
    }
}

$payload = [pscustomobject]@{
    schemaVersion = 2
    ok = $true
    allowNotSigned = [bool]$AllowNotSigned
    count = $results.Count
    artifacts = $results
}
$json = $payload | ConvertTo-Json -Depth 5 -Compress

if ($OutputPath) {
    $resolvedOutputPath = [System.IO.Path]::GetFullPath($OutputPath)
    $outputDirectory = Split-Path -Path $resolvedOutputPath -Parent
    if ($outputDirectory) {
        New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
    }
    Write-Utf8NoBomJson -Path $resolvedOutputPath -Json $json
}

$json
