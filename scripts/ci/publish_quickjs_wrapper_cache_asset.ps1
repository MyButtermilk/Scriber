param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [Parameter(Mandatory = $true)]
    [string]$SourcePath
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$lockPath = Join-Path $repoRoot "packaging\quickjs-youtube-runtime-lock-v1.json"
$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
$artifact = $lock.wrapper.artifact
$tag = "release-cache-quickjs-wrapper-v3"
$assetName = "scriber-quickjs-wrapper-v3-windows-x86_64.exe"
$expectedUrl = "https://github.com/$Repo/releases/download/$tag/$assetName"
$expectedLength = [int64]$artifact.length
$expectedSha256 = ([string]$artifact.sha256).ToLowerInvariant()
if (
    [string]$lock.contract -ne "ScriberQuickJsWrapperRuntimeLockV1" -or
    [string]$artifact.fileName -ne $assetName -or
    [string]$artifact.url -ne $expectedUrl -or
    $expectedLength -le 0 -or
    $expectedSha256 -notmatch '^[0-9a-f]{64}$'
) {
    throw "QuickJS wrapper cache artifact lock is invalid for repository '$Repo'."
}

$resolvedSource = (Resolve-Path -LiteralPath $SourcePath).Path
if (-not (Test-Path -LiteralPath $resolvedSource -PathType Leaf)) {
    throw "QuickJS wrapper cache source is not a file: $SourcePath"
}
$sourceItem = Get-Item -LiteralPath $resolvedSource
$sourceSha256 = (Get-FileHash -LiteralPath $resolvedSource -Algorithm SHA256).Hash.ToLowerInvariant()
if ($sourceItem.Length -ne $expectedLength -or $sourceSha256 -ne $expectedSha256) {
    throw "Refusing to publish a QuickJS wrapper that differs from the protected identity."
}

$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "scriber-quickjs-cache-publish-{0}" -f [Guid]::NewGuid().ToString("N")
)
try {
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    $assetPath = Join-Path $temporaryRoot $assetName
    Copy-Item -LiteralPath $resolvedSource -Destination $assetPath

    & gh release view $tag --repo $Repo *> $null
    if ($LASTEXITCODE -ne 0) {
        & gh release create $tag `
            --repo $Repo `
            --title "Scriber QuickJS Wrapper Cache v3" `
            --notes "Byte-locked Windows x86_64 QuickJS wrapper used by hermetic Scriber backend builds." `
            --prerelease `
            --latest=false
        if ($LASTEXITCODE -ne 0) {
            & gh release view $tag --repo $Repo *> $null
            if ($LASTEXITCODE -ne 0) {
                throw "Creating QuickJS wrapper cache release '$tag' failed."
            }
        }
    }

    & gh release upload $tag $assetPath --repo $Repo --clobber
    if ($LASTEXITCODE -ne 0) {
        throw "Uploading QuickJS wrapper cache asset failed."
    }
} finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

& (Join-Path $PSScriptRoot "verify_quickjs_wrapper_cache_asset.ps1") -Repo $Repo
if ($LASTEXITCODE -ne 0) {
    throw "Published QuickJS wrapper cache asset did not pass download verification."
}
