<#
.SYNOPSIS
Adds release-runtime inputs to the normalized cache-key manifests.

.DESCRIPTION
The static manifests are written by write_release_cache_keys.ps1. This helper
adds only deterministic release/runtime inputs so a lightweight planning job
can calculate the same keys as the Windows packaging job without installing
the Rust toolchain first. The workflow pins the toolchain and Windows target;
changing either value therefore changes the key explicitly.

No credential value is written to disk. Public configuration values are
represented by presence flags and SHA-256 digests only.
#>

param(
    [string]$CacheKeyDir = "build\cache-keys",
    [Parameter(Mandatory = $true)]
    [string]$SourceCommit,
    [string]$UpdaterPublicKey = "",
    [string]$UpdaterEndpoint = "",
    [string]$OutlookClientId = "",
    [string]$RustToolchain = "1.97.0",
    [string]$RustTarget = "x86_64-pc-windows-msvc"
)

$ErrorActionPreference = "Stop"

$normalizedSourceCommit = $SourceCommit.Trim().ToLowerInvariant()
if ($normalizedSourceCommit -notmatch '^[0-9a-f]{40}$') {
    throw "Release source commit must be a 40-character lowercase hexadecimal Git object id."
}

function Get-StringSha256 {
    param([string]$Value)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [Convert]::ToHexString($sha.ComputeHash($bytes)).ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Reset-DynamicRows {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Release cache-key manifest was not found: $Path"
    }

    $rows = @(
        Get-Content -LiteralPath $Path |
            Where-Object { $_ -notlike "release-runtime`t*" }
    )
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, (($rows -join "`n") + "`n"), $encoding)
}

function Add-DynamicRow {
    param(
        [string]$Path,
        [string]$Name,
        [string]$Value
    )

    $rows = @(Get-Content -LiteralPath $Path)
    $rows += "release-runtime`t$Name`t$Value"
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, (($rows -join "`n") + "`n"), $encoding)
}

$resolvedCacheKeyDir = [System.IO.Path]::GetFullPath($CacheKeyDir)
$rustDependenciesPath = Join-Path $resolvedCacheKeyDir "rust-dependencies.txt"
$tauriAppBinaryPath = Join-Path $resolvedCacheKeyDir "tauri-app-binary.txt"

Reset-DynamicRows -Path $rustDependenciesPath
Reset-DynamicRows -Path $tauriAppBinaryPath

$effectiveUpdaterEndpoint = $UpdaterEndpoint.Trim()
if (-not $effectiveUpdaterEndpoint) {
    $effectiveUpdaterEndpoint = "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json"
}

$publicKeyHash = Get-StringSha256 -Value $UpdaterPublicKey
$outlookClientIdValue = $OutlookClientId.Trim()
$outlookClientIdPresent = [bool]$outlookClientIdValue
$outlookClientIdHash = if ($outlookClientIdPresent) {
    Get-StringSha256 -Value $outlookClientIdValue
} else {
    "missing"
}

foreach ($path in @($rustDependenciesPath, $tauriAppBinaryPath)) {
    Add-DynamicRow -Path $path -Name "rust-toolchain" -Value $RustToolchain
    Add-DynamicRow -Path $path -Name "rust-target" -Value $RustTarget
}

Add-DynamicRow -Path $tauriAppBinaryPath -Name "updater-public-key-sha256" -Value $publicKeyHash
Add-DynamicRow -Path $tauriAppBinaryPath -Name "updater-endpoint" -Value $effectiveUpdaterEndpoint
Add-DynamicRow -Path $tauriAppBinaryPath -Name "outlook-client-id-present" -Value $outlookClientIdPresent.ToString().ToLowerInvariant()
Add-DynamicRow -Path $tauriAppBinaryPath -Name "outlook-client-id-sha256" -Value $outlookClientIdHash
# Keep SourceCommit as a validated invocation/provenance boundary, but do not
# add it to the exact app-product key. The static manifest plus the public
# release-runtime inputs above already bind every binary-producing input, while
# the exported cache manifest retains the commit that produced the executable.

$names = @(
    "frontend-dependencies.txt",
    "rust-dependencies.txt",
    "rust-release.txt",
    "tauri-app-binary.txt",
    "rust-audio-sidecar.txt",
    "rust-diarization-sidecar.txt",
    "sherpa-onnx-archive.txt",
    "backend-runtime.txt",
    "backend-sidecar.txt"
)

$fingerprints = [ordered]@{}
foreach ($name in $names) {
    $path = Join-Path $resolvedCacheKeyDir $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Release cache-key manifest was not found: $path"
    }
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    $fingerprints[$name] = $hash
    Write-Host ("Final release cache fingerprint {0}: {1}" -f $name, $hash.Substring(0, 12))
}

if ($env:GITHUB_OUTPUT) {
    "rust-dependencies-hash=$($fingerprints['rust-dependencies.txt'])" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "tauri-app-binary-hash=$($fingerprints['tauri-app-binary.txt'])" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "backend-runtime-hash=$($fingerprints['backend-runtime.txt'])" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "backend-sidecar-hash=$($fingerprints['backend-sidecar.txt'])" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
}

[ordered]@{
    ok = $true
    rustToolchain = $RustToolchain
    rustTarget = $RustTarget
    fingerprints = $fingerprints
} | ConvertTo-Json -Depth 4 -Compress
