<#
.SYNOPSIS
Selects the warm single-runner or cold two-runner Windows release path.

.DESCRIPTION
This is a read-only GitHub probe. The cold path is selected only for a v* tag
when both immutable products needed by packaging are absent: the exact Tauri
application binary and the exact frozen backend sidecar (Actions cache and
durable release fallback). Any probe failure falls back to the established
single-runner path; it never weakens release validation.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [Parameter(Mandatory = $true)]
    [string]$GitRef,
    [Parameter(Mandatory = $true)]
    [string]$PythonVersion,
    [Parameter(Mandatory = $true)]
    [string]$BackendSidecarHash,
    [Parameter(Mandatory = $true)]
    [string]$TauriAppBinaryHash,
    [string]$BackendArtifactTag = "release-cache-backend-sidecar-v2",
    [string]$RunnerOs = "Windows",
    [ValidateSet("", "0", "1")]
    [string]$RequireAuthenticodeSignature = "",
    [switch]$EmitDerivedCacheKeysOnly
)

$ErrorActionPreference = "Stop"

function Write-OutputValue {
    param([string]$Name, [string]$Value)

    if ($env:GITHUB_OUTPUT) {
        "$Name=$Value" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

function Convert-ManifestFingerprintToHashFilesFingerprint {
    param([string]$Fingerprint)

    $normalized = ([string]$Fingerprint).Trim().ToLowerInvariant()
    if ($normalized -notmatch '\A[0-9a-f]{64}\z') {
        throw "Release cache manifest fingerprint must be a 64-character SHA-256 digest."
    }

    $manifestDigestBytes = New-Object byte[] 32
    for ($index = 0; $index -lt $manifestDigestBytes.Length; $index++) {
        $manifestDigestBytes[$index] = [Convert]::ToByte($normalized.Substring($index * 2, 2), 16)
    }

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashFilesDigest = $sha256.ComputeHash($manifestDigestBytes)
    } finally {
        $sha256.Dispose()
    }
    return -join ($hashFilesDigest | ForEach-Object { $_.ToString("x2") })
}

function Test-MainCacheKey {
    param([string]$Key)

    $escapedKey = [Uri]::EscapeDataString($Key)
    $response = gh api "/repos/$Repo/actions/caches?ref=refs%2Fheads%2Fmain&key=$escapedKey&per_page=100" | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub Actions cache probe failed."
    }
    return [bool](@($response.actions_caches | Where-Object { [string]$_.key -eq $Key }).Count -gt 0)
}

function Test-ReleaseAsset {
    param([string]$Tag, [string]$AssetName)

    try {
        $release = gh api "/repos/$Repo/releases/tags/$Tag" 2>$null | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
        return [bool](@($release.assets | Where-Object { [string]$_.name -eq $AssetName }).Count -gt 0)
    } catch {
        return $false
    }
}

$backendActionsHash = Convert-ManifestFingerprintToHashFilesFingerprint -Fingerprint $BackendSidecarHash
$tauriActionsHash = Convert-ManifestFingerprintToHashFilesFingerprint -Fingerprint $TauriAppBinaryHash
$isTagRelease = $GitRef -like "refs/tags/v*"
$backendActionsKey = "scriber-backend-sidecar-v2-$RunnerOs-python-$PythonVersion-$backendActionsHash"
$tauriActionsKey = "scriber-tauri-app-binary-v3-$RunnerOs-$tauriActionsHash"
$backendAssetName = "scriber-backend-sidecar-$RunnerOs-python-$PythonVersion-$backendActionsHash.zip"

if ($EmitDerivedCacheKeysOnly) {
    [ordered]@{
        backendActionsKey = $backendActionsKey
        tauriActionsKey = $tauriActionsKey
        backendAssetName = $backendAssetName
    } | ConvertTo-Json -Compress
    exit 0
}

$backendActionsReady = $false
$tauriActionsReady = $false
$backendReleaseReady = $false
$probeOk = $true
$probeError = ""

try {
    $backendActionsReady = Test-MainCacheKey -Key $backendActionsKey
    $tauriActionsReady = Test-MainCacheKey -Key $tauriActionsKey
    $backendReleaseReady = Test-ReleaseAsset -Tag $BackendArtifactTag -AssetName $backendAssetName
} catch {
    $probeOk = $false
    $probeError = $_.Exception.Message
    Write-Warning "Release fast-path probe failed; retaining the safe single-runner path."
}

$backendReady = $backendActionsReady -or $backendReleaseReady
$authenticodeRequiresSingleRunner = $RequireAuthenticodeSignature -eq "1"
$useColdPath = (
    $probeOk -and
    $isTagRelease -and
    -not $authenticodeRequiresSingleRunner -and
    -not $backendReady -and
    -not $tauriActionsReady
)
$reason = if (-not $isTagRelease) {
    "non-tag"
} elseif (-not $probeOk) {
    "probe-failed"
} elseif ($authenticodeRequiresSingleRunner) {
    "authenticode-requires-single-runner"
} elseif ($useColdPath) {
    "backend-and-tauri-products-missing"
} elseif ($backendReady -and $tauriActionsReady) {
    "both-products-ready"
} elseif ($backendReady) {
    "backend-product-ready"
} else {
    "tauri-product-ready"
}

Write-OutputValue -Name "use-cold-path" -Value $useColdPath.ToString().ToLowerInvariant()
Write-OutputValue -Name "reason" -Value $reason
Write-OutputValue -Name "backend-ready" -Value $backendReady.ToString().ToLowerInvariant()
Write-OutputValue -Name "tauri-ready" -Value $tauriActionsReady.ToString().ToLowerInvariant()

[ordered]@{
    ok = $probeOk
    useColdPath = $useColdPath
    reason = $reason
    authenticodeRequiresSingleRunner = $authenticodeRequiresSingleRunner
    backend = [ordered]@{
        actionsCache = $backendActionsReady
        releaseArtifact = $backendReleaseReady
        ready = $backendReady
    }
    tauri = [ordered]@{
        actionsCache = $tauriActionsReady
        ready = $tauriActionsReady
    }
    error = if ($probeError) { "probe unavailable" } else { $null }
} | ConvertTo-Json -Depth 5 -Compress
