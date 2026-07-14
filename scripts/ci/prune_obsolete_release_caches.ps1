param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [switch]$Apply,
    [ValidateRange(1, 10)]
    [int]$RetainPerRollingFamily = 1,
    [ValidateRange(10, 10000)]
    [int]$ListLimit = 10000
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI is required for release-cache pruning."
}

$json = & gh cache list --repo $Repo --limit $ListLimit --json id,key,ref,sizeInBytes,createdAt,lastAccessedAt
if ($LASTEXITCODE -ne 0) {
    throw "GitHub cache inventory failed with exit code $LASTEXITCODE."
}
$caches = @($json | ConvertFrom-Json)

$releaseJson = & gh release list --repo $Repo --limit $ListLimit --json tagName
if ($LASTEXITCODE -ne 0) {
    throw "GitHub release-cache inventory failed with exit code $LASTEXITCODE."
}
$releases = @($releaseJson | ConvertFrom-Json)

# Durable cache snapshots use internal prerelease tags. Keep exactly the
# current schema generation for each family and never match public app tags.
$currentReleaseTags = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
foreach ($tag in @(
    'ffmpeg-profile-b-n7.0-v4',
    'release-cache-backend-sidecar-v2',
    'release-cache-python-venv-v1',
    'release-cache-python-wheelhouse-v2',
    'release-cache-rust-build-v2',
    'release-cache-rust-audio-sidecar-v1',
    'release-cache-rust-diarization-sidecar-v1'
)) {
    $null = $currentReleaseTags.Add($tag)
}
$cacheReleasePatterns = @(
    '^ffmpeg-profile-b-n7\.0-v\d+$',
    '^release-cache-backend-sidecar-v\d+$',
    '^release-cache-python-venv-v\d+$',
    '^release-cache-python-wheelhouse-v\d+$',
    '^release-cache-rust-build-v\d+$',
    '^release-cache-rust-audio-sidecar-v\d+$',
    '^release-cache-rust-diarization-sidecar-v\d+$'
)
$obsoleteReleaseTags = @(
    $releases |
        ForEach-Object { [string]$_.tagName } |
        Where-Object {
            $candidateTag = $_
            -not $currentReleaseTags.Contains($_) -and
            @($cacheReleasePatterns | Where-Object { $candidateTag -match $_ }).Count -gt 0
        }
)

$obsoleteReleaseAssets = [System.Collections.Generic.List[object]]::new()
foreach ($tag in $currentReleaseTags) {
    $assetJson = & gh release view $tag --repo $Repo --json assets 2>$null
    if ($LASTEXITCODE -ne 0) {
        # A current durable fallback may not exist until its first cold build.
        continue
    }
    $assets = @(
        ($assetJson | ConvertFrom-Json).assets |
            Sort-Object -Property `
                @{ Expression = { [DateTimeOffset]$_.createdAt }; Descending = $true }, `
                @{ Expression = { if ([string]$_.apiUrl -match '/(\d+)$') { [int64]$Matches[1] } else { 0 } }; Descending = $true }, `
                @{ Expression = { [string]$_.name }; Descending = $true }
    )
    foreach ($asset in @($assets | Select-Object -Skip 1)) {
        $obsoleteReleaseAssets.Add([pscustomobject]@{
            Tag = $tag
            Name = [string]$asset.name
            CreatedAt = [string]$asset.createdAt
        }) | Out-Null
    }
}

# These generations cannot match any key emitted by the current release
# workflow. Keep the allowlist explicit; never turn this into broad cache GC.
$obsoletePatterns = @(
    '^scriber-backend-sidecar-Windows-',
    '^scriber-ffmpeg-profile-b-msys2-n7\.0-v[23]-Windows$',
    '^scriber-rust-release-v2-Windows-',
    '^setup-python-',
    '^node-cache-'
)

# Rolling products retain exactly the newest current entry by default. This is
# intentionally not a rollback policy: the user requested one generation per
# cache family, and durable release assets are pruned to the same invariant.
$rollingFamilies = @(
    [pscustomobject]@{ Name = 'backend-v2'; Pattern = '^scriber-backend-sidecar-v2-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'audio'; Pattern = '^scriber-rust-audio-sidecar-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'tauri-app'; Pattern = '^scriber-tauri-app-binary-v1-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'frontend'; Pattern = '^scriber-frontend-node-modules-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'python-venv'; Pattern = '^scriber-python-venv-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'python-wheelhouse'; Pattern = '^scriber-python-wheelhouse-v2-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'rust-dependencies'; Pattern = '^scriber-rust-dependencies-v1-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'diarization'; Pattern = '^scriber-rust-diarization-sidecar-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'sherpa'; Pattern = '^scriber-sherpa-onnx-archive-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'ffmpeg'; Pattern = '^scriber-ffmpeg-profile-b-msys2-n7\.0-v4-Windows$'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'tauri-bundler'; Pattern = '^scriber-tauri-bundler-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'npm-store'; Pattern = '^scriber-npm-package-store-v1-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'pip-store'; Pattern = '^scriber-python-pip-store-v1-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'msys2'; Pattern = '^msys2-pkgs-'; Retain = $RetainPerRollingFamily }
)

$deletions = [System.Collections.Generic.List[object]]::new()
foreach ($cache in $caches) {
    if ([string]$cache.ref -ne 'refs/heads/main') {
        continue
    }
    if ($obsoletePatterns | Where-Object { [string]$cache.key -match $_ }) {
        $deletions.Add([pscustomobject]@{ Cache = $cache; Reason = 'obsolete-generation' }) | Out-Null
    }
}

foreach ($family in $rollingFamilies) {
    $members = @(
        $caches |
            Where-Object { [string]$_.ref -eq 'refs/heads/main' -and [string]$_.key -match $family.Pattern } |
            # Generation recency is creation time. A concurrent older tag can
            # touch an obsolete cache after the replacement is created, so
            # lastAccessedAt must never make the old generation win GC.
            Sort-Object -Property @{ Expression = { [DateTimeOffset]$_.createdAt }; Descending = $true }, @{ Expression = { [int64]$_.id }; Descending = $true }, @{ Expression = { [DateTimeOffset]$_.lastAccessedAt }; Descending = $true }
    )
    foreach ($cache in @($members | Select-Object -Skip ([int]$family.Retain))) {
        if (-not ($deletions | Where-Object { [int64]$_.Cache.id -eq [int64]$cache.id })) {
            $deletions.Add([pscustomobject]@{ Cache = $cache; Reason = "rolling-$($family.Name)-beyond-$($family.Retain)" }) | Out-Null
        }
    }
}

# Ref-scoped caches cannot warm main or sibling tags once those releases have
# completed. Restrict deletion to known Scriber/setup cache families.
$knownCachePattern = '^((scriber-|setup-python-|node-cache-|msys2-pkgs-).*)$'
foreach ($cache in $caches) {
    if (
        [string]$cache.ref -ne 'refs/heads/main' -and
        [string]$cache.key -match $knownCachePattern
    ) {
        $deletions.Add([pscustomobject]@{ Cache = $cache; Reason = 'inaccessible-completed-ref-cache' }) | Out-Null
    }
}

$uniqueDeletions = @($deletions | Sort-Object { [int64]$_.Cache.id } -Unique)
$bytes = [int64](($uniqueDeletions | ForEach-Object { [int64]$_.Cache.sizeInBytes } | Measure-Object -Sum).Sum)
Write-Host ("Release cache GC: candidates={0}; reclaimMiB={1:N1}; apply={2}" -f $uniqueDeletions.Count, ($bytes / 1MB), [bool]$Apply)
foreach ($entry in $uniqueDeletions) {
    Write-Host ("  {0}: {1} ({2:N1} MiB)" -f $entry.Reason, $entry.Cache.key, ([int64]$entry.Cache.sizeInBytes / 1MB))
}
Write-Host ("Internal cache release GC: candidates={0}; apply={1}" -f $obsoleteReleaseTags.Count, [bool]$Apply)
foreach ($tag in $obsoleteReleaseTags) {
    Write-Host "  obsolete-release-generation: $tag"
}
Write-Host ("Current cache release asset GC: candidates={0}; apply={1}" -f $obsoleteReleaseAssets.Count, [bool]$Apply)
foreach ($asset in $obsoleteReleaseAssets) {
    Write-Host ("  superseded-release-asset: {0}/{1}" -f $asset.Tag, $asset.Name)
}

if ($Apply) {
    foreach ($entry in $uniqueDeletions) {
        & gh cache delete ([string]$entry.Cache.id) --repo $Repo
        if ($LASTEXITCODE -ne 0) {
            throw "Deleting allowlisted cache id $($entry.Cache.id) failed with exit code $LASTEXITCODE."
        }
    }
    foreach ($tag in $obsoleteReleaseTags) {
        & gh release delete $tag --repo $Repo --cleanup-tag --yes
        if ($LASTEXITCODE -ne 0) {
            throw "Deleting allowlisted internal cache release '$tag' failed with exit code $LASTEXITCODE."
        }
    }
    foreach ($asset in $obsoleteReleaseAssets) {
        & gh release delete-asset $asset.Tag $asset.Name --repo $Repo --yes
        if ($LASTEXITCODE -ne 0) {
            throw "Deleting superseded cache release asset '$($asset.Tag)/$($asset.Name)' failed with exit code $LASTEXITCODE."
        }
    }
}

[ordered]@{
    apiVersion = '1'
    mode = $(if ($Apply) { 'apply' } else { 'dry-run' })
    scanned = $caches.Count
    candidates = $uniqueDeletions.Count
    reclaimBytes = $bytes
    releaseCandidates = $obsoleteReleaseTags.Count
    releaseAssetCandidates = $obsoleteReleaseAssets.Count
    retainedPerRollingFamily = $RetainPerRollingFamily
} | ConvertTo-Json -Compress
