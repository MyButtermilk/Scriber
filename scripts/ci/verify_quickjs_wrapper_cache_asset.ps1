param(
    [Parameter(Mandatory = $true)]
    [string]$Repo
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$lockPath = Join-Path $repoRoot "packaging\quickjs-youtube-runtime-lock-v1.json"
$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
if ([string]$lock.contract -ne "ScriberQuickJsWrapperRuntimeLockV1") {
    throw "QuickJS wrapper runtime lock contract is invalid."
}

$artifact = $lock.wrapper.artifact
$tag = "release-cache-quickjs-wrapper-v3"
$expectedName = "scriber-quickjs-wrapper-v3-windows-x86_64.exe"
$expectedUrl = "https://github.com/$Repo/releases/download/$tag/$expectedName"
$expectedLength = [int64]$artifact.length
$expectedSha256 = ([string]$artifact.sha256).ToLowerInvariant()
if (
    [string]$artifact.fileName -ne $expectedName -or
    [string]$artifact.url -ne $expectedUrl -or
    $expectedLength -le 0 -or
    $expectedSha256 -notmatch '^[0-9a-f]{64}$'
) {
    throw "QuickJS wrapper cache artifact lock is invalid for repository '$Repo'."
}

$releaseJson = & gh release view $tag --repo $Repo --json assets 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "QuickJS wrapper cache release '$tag' is unavailable."
}
$matchingAssets = @(
    ($releaseJson | ConvertFrom-Json).assets |
        Where-Object { [string]$_.name -ceq $expectedName }
)
if ($matchingAssets.Count -ne 1) {
    throw "QuickJS wrapper cache release must contain exactly one '$expectedName' asset."
}
$asset = $matchingAssets[0]
if (
    [int64]$asset.size -ne $expectedLength -or
    ([string]$asset.digest).ToLowerInvariant() -ne "sha256:$expectedSha256"
) {
    throw "QuickJS wrapper cache release metadata differs from its protected identity."
}

$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "scriber-quickjs-cache-check-{0}" -f [Guid]::NewGuid().ToString("N")
)
try {
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    & gh release download $tag `
        --repo $Repo `
        --pattern $expectedName `
        --dir $temporaryRoot `
        --clobber
    if ($LASTEXITCODE -ne 0) {
        throw "Downloading the QuickJS wrapper cache asset failed."
    }
    $downloaded = Join-Path $temporaryRoot $expectedName
    if (-not (Test-Path -LiteralPath $downloaded -PathType Leaf)) {
        throw "Downloaded QuickJS wrapper cache asset is missing."
    }
    $downloadedItem = Get-Item -LiteralPath $downloaded
    $downloadedSha256 = (Get-FileHash -LiteralPath $downloaded -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($downloadedItem.Length -ne $expectedLength -or $downloadedSha256 -ne $expectedSha256) {
        throw "Downloaded QuickJS wrapper cache asset differs from its protected identity."
    }
} finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

[ordered]@{
    contract = "ScriberQuickJsWrapperCacheAssetCheckV1"
    ok = $true
    tag = $tag
    asset = $expectedName
    length = $expectedLength
    sha256 = $expectedSha256
} | ConvertTo-Json -Compress
