param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [switch]$Apply,
    [ValidateRange(1, 10)]
    [int]$RetainPerRollingFamily = 1,
    [ValidateRange(10, 10000)]
    [int]$ListLimit = 10000,
    [string]$ExpectedRustDependencyKey = "",
    [string[]]$ProtectedRef = @(),
    [string[]]$PrunableRef = @(),
    [switch]$PruneCurrentRef,
    [switch]$VerifyCurrentGeneration,
    [string]$TauriPromotionEvidencePath = "",
    [string]$ExpectedTauriPromotionEvidenceSha256 = "",
    [string]$ExpectedTauriAppKey = ""
)

$ErrorActionPreference = "Stop"
$normalizedExpectedRustDependencyKey = ([string]$ExpectedRustDependencyKey).Trim()
$normalizedTauriPromotionEvidencePath = ([string]$TauriPromotionEvidencePath).Trim()
$normalizedExpectedTauriPromotionEvidenceSha256 = ([string]$ExpectedTauriPromotionEvidenceSha256).Trim().ToLowerInvariant()
$normalizedExpectedTauriAppKey = ([string]$ExpectedTauriAppKey).Trim()
$tauriPromotionEvidencePathSupplied = -not [string]::IsNullOrWhiteSpace($normalizedTauriPromotionEvidencePath)
$tauriPromotionEvidenceShaSupplied = -not [string]::IsNullOrWhiteSpace($normalizedExpectedTauriPromotionEvidenceSha256)
$tauriPromotionExpectedKeySupplied = -not [string]::IsNullOrWhiteSpace($normalizedExpectedTauriAppKey)
$tauriPromotionRequested = $tauriPromotionEvidencePathSupplied -or $tauriPromotionEvidenceShaSupplied
$tauriPromotionAuthorized = $false
$tauriPromotionReason = 'not-requested'
$tauriPromotionEvidenceSha256 = $null
$tauriPromotedCache = $null
$mutationSuppressed = $false

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash($Bytes)
    } finally {
        $sha256.Dispose()
    }
    return ([System.BitConverter]::ToString($digest) -replace '-', '').ToLowerInvariant()
}

if ($VerifyCurrentGeneration -and $Apply) {
    throw "Current-generation verification must run after apply in a fresh inventory pass."
}
if (
    $VerifyCurrentGeneration -and
    $normalizedExpectedRustDependencyKey -notmatch '^scriber-rust-dependencies-v1-Windows-[0-9a-f]{64}$'
) {
    throw "Current-generation verification requires the full expected Rust dependency cache key."
}
if (
    $VerifyCurrentGeneration -and
    $normalizedExpectedTauriAppKey -cnotmatch '^scriber-tauri-app-binary-v[123]-Windows-[0-9a-f]{64}$'
) {
    throw "Current-generation verification requires the full expected Tauri app cache key."
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI is required for release-cache pruning."
}

$protectedRefs = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
$prunableRefs = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
$currentRefs = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
$null = $protectedRefs.Add('refs/heads/main')
foreach ($candidateRef in @($ProtectedRef)) {
    $normalizedRef = ([string]$candidateRef).Trim()
    if ($normalizedRef -notmatch '^refs/heads/[0-9A-Za-z._/-]+$') {
        throw "Protected cache ref must be a complete refs/heads/* value: '$candidateRef'."
    }
    $null = $protectedRefs.Add($normalizedRef)
}
foreach ($candidateRef in @($PrunableRef)) {
    $normalizedRef = ([string]$candidateRef).Trim()
    if ($normalizedRef -notmatch '^refs/heads/[0-9A-Za-z._/-]+$' -or $normalizedRef -eq 'refs/heads/main') {
        throw "Prunable cache ref must be a non-main complete refs/heads/* value: '$candidateRef'."
    }
    $null = $prunableRefs.Add($normalizedRef)
}
$githubRef = ([string]$env:GITHUB_REF).Trim()
if ($githubRef -match '^refs/heads/[0-9A-Za-z._/-]+$') {
    $null = $currentRefs.Add($githubRef)
}
if (Get-Command git -ErrorAction SilentlyContinue) {
    $branchOutput = @(& git symbolic-ref --quiet --short HEAD 2>$null)
    $branchExitCode = $LASTEXITCODE
    if ($branchExitCode -eq 0 -and $branchOutput.Count -gt 0) {
        $branchName = ([string]$branchOutput[0]).Trim()
        if ($branchName -match '^[0-9A-Za-z._/-]+$') {
            $null = $currentRefs.Add("refs/heads/$branchName")
        }
    }
}
foreach ($currentRef in $currentRefs) {
    if ($currentRef -eq 'refs/heads/main') {
        continue
    }
    if ($PruneCurrentRef) {
        $null = $prunableRefs.Add($currentRef)
    } else {
        $null = $protectedRefs.Add($currentRef)
    }
}
foreach ($candidateRef in $prunableRefs) {
    if ($protectedRefs.Contains($candidateRef)) {
        throw "Cache ref cannot be both protected and prunable: '$candidateRef'."
    }
}

$json = & gh cache list --repo $Repo --limit $ListLimit --json id,key,ref,sizeInBytes,createdAt,lastAccessedAt
if ($LASTEXITCODE -ne 0) {
    throw "GitHub cache inventory failed with exit code $LASTEXITCODE."
}
$parsedCaches = $json | ConvertFrom-Json
$caches = @($parsedCaches)

# Tauri generation promotion is deliberately separate from normal rolling
# retention. A newly seeded schema generation is not proof that it restored or
# completed successfully, so local inventory recency alone must never evict the
# last cache from an older generation.
$tauriFamily = [pscustomobject]@{
    Name = 'tauri-app'
    Pattern = '^scriber-tauri-app-binary-v[123]-Windows-'
    GenerationPattern = '^scriber-tauri-app-binary-v(?<generation>[123])-Windows-'
    KeyPattern = '^scriber-tauri-app-binary-v(?<generation>[123])-Windows-(?<fingerprint>[0-9a-f]{64})$'
}

if ($tauriPromotionRequested) {
    $tauriPromotionReason = 'paired-parameters-required'
    $mutationSuppressed = $true

    if ($tauriPromotionEvidencePathSupplied -and $tauriPromotionEvidenceShaSupplied -and $tauriPromotionExpectedKeySupplied) {
        if ($normalizedExpectedTauriAppKey -cnotmatch '^scriber-tauri-app-binary-v[123]-Windows-[0-9a-f]{64}$') {
            $tauriPromotionReason = 'invalid-expected-tauri-key'
        } elseif ($normalizedExpectedTauriPromotionEvidenceSha256 -cnotmatch '^[0-9a-f]{64}$') {
            $tauriPromotionReason = 'invalid-expected-evidence-sha256'
        } elseif (-not (Test-Path -LiteralPath $normalizedTauriPromotionEvidencePath -PathType Leaf)) {
            $tauriPromotionReason = 'evidence-file-missing'
        } else {
            $evidence = $null
            $evidenceRaw = $null
            try {
                $evidenceItem = Get-Item -LiteralPath $normalizedTauriPromotionEvidencePath
                $evidenceBytes = [System.IO.File]::ReadAllBytes($evidenceItem.FullName)
                if ([int64]$evidenceItem.Length -gt 1MB -or $evidenceBytes.Length -gt 1MB) {
                    $tauriPromotionReason = 'evidence-file-too-large'
                } else {
                    $tauriPromotionEvidenceSha256 = Get-Sha256Hex -Bytes $evidenceBytes
                    if ($tauriPromotionEvidenceSha256 -cne $normalizedExpectedTauriPromotionEvidenceSha256) {
                        $tauriPromotionReason = 'evidence-sha256-mismatch'
                    } else {
                        $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
                        $evidenceRaw = $strictUtf8.GetString($evidenceBytes)
                        $evidence = $evidenceRaw | ConvertFrom-Json
                    }
                }
            } catch {
                $tauriPromotionReason = 'evidence-malformed'
                $evidence = $null
            }

            if ($null -ne $evidence) {
                $requiredTopLevelProperties = @('schemaVersion', 'generatedAtUtc', 'repo', 'cache', 'run', 'reuse')
                $missingTopLevelProperty = @(
                    $requiredTopLevelProperties |
                        Where-Object { $null -eq $evidence.PSObject.Properties[$_] }
                ).Count -gt 0
                if ($missingTopLevelProperty -or [string]$evidence.schemaVersion -cne '1') {
                    $tauriPromotionReason = 'evidence-schema-mismatch'
                } else {
                    $nowUtc = [DateTimeOffset]::UtcNow
                    $evidenceGeneratedAt = $null
                    try {
                        $evidenceGeneratedAt = [DateTimeOffset]::Parse(
                            [string]$evidence.generatedAtUtc,
                            [System.Globalization.CultureInfo]::InvariantCulture,
                            [System.Globalization.DateTimeStyles]::RoundtripKind
                        )
                    } catch {
                        $evidenceGeneratedAt = $null
                    }

                    if ($null -eq $evidenceGeneratedAt -or $evidenceGeneratedAt.Offset -ne [TimeSpan]::Zero) {
                        $tauriPromotionReason = 'evidence-generated-at-invalid'
                    } elseif ($evidenceGeneratedAt -lt $nowUtc.AddHours(-24) -or $evidenceGeneratedAt -gt $nowUtc.AddMinutes(5)) {
                        $tauriPromotionReason = 'evidence-stale'
                    } elseif (
                        ([string]$Repo).Trim() -notmatch '^[0-9A-Za-z_.-]+/[0-9A-Za-z_.-]+$' -or
                        [string]$evidence.repo -ine ([string]$Repo).Trim()
                    ) {
                        $tauriPromotionReason = 'evidence-repo-mismatch'
                    } elseif (
                        $null -eq $evidence.cache -or
                        @(
                            @('id', 'key', 'ref', 'generation', 'fingerprint') |
                                Where-Object { $null -eq $evidence.cache.PSObject.Properties[$_] }
                        ).Count -gt 0
                    ) {
                        $tauriPromotionReason = 'evidence-cache-binding-invalid'
                    } elseif ([string]$evidence.cache.ref -cnotmatch '^refs/heads/[0-9A-Za-z._/-]+$') {
                        $tauriPromotionReason = 'evidence-cache-ref-invalid'
                    } elseif (
                        -not $protectedRefs.Contains([string]$evidence.cache.ref) -or
                        $prunableRefs.Contains([string]$evidence.cache.ref)
                    ) {
                        $tauriPromotionReason = 'evidence-cache-ref-not-protected'
                    } elseif ([string]$evidence.cache.key -cnotmatch $tauriFamily.KeyPattern) {
                        $tauriPromotionReason = 'evidence-cache-key-invalid'
                    } elseif ([string]$evidence.cache.key -cne $normalizedExpectedTauriAppKey) {
                        $tauriPromotionReason = 'evidence-cache-key-not-expected'
                    } else {
                        $keyGeneration = [int]$Matches.generation
                        $keyFingerprint = [string]$Matches.fingerprint
                        $evidenceCacheIdText = ([string]$evidence.cache.id).Trim()
                        $evidenceGenerationText = ([string]$evidence.cache.generation).Trim()
                        [int64]$evidenceCacheId = 0
                        if (
                            $evidenceCacheIdText -cnotmatch '^[1-9][0-9]*$' -or
                            -not [int64]::TryParse($evidenceCacheIdText, [ref]$evidenceCacheId) -or
                            $evidenceGenerationText -cnotmatch '^[123]$' -or
                            [int]$evidenceGenerationText -ne $keyGeneration -or
                            [string]$evidence.cache.fingerprint -cne $keyFingerprint
                        ) {
                            $tauriPromotionReason = 'evidence-cache-key-fields-mismatch'
                        } else {
                            $idMatches = @($caches | Where-Object { [int64]$_.id -eq $evidenceCacheId })
                            $keyRefMatches = @(
                                $caches |
                                    Where-Object {
                                        [string]$_.key -ceq [string]$evidence.cache.key -and
                                        [string]$_.ref -ceq [string]$evidence.cache.ref
                                    }
                            )
                            if (
                                $idMatches.Count -ne 1 -or
                                $keyRefMatches.Count -ne 1 -or
                                [int64]$keyRefMatches[0].id -ne $evidenceCacheId -or
                                [string]$idMatches[0].key -cne [string]$evidence.cache.key -or
                                [string]$idMatches[0].ref -cne [string]$evidence.cache.ref
                            ) {
                                $tauriPromotionReason = 'evidence-cache-not-unique'
                            } elseif (
                                $null -eq $evidence.run -or
                                @(
                                    @('id', 'attempt', 'headSha', 'headBranch') |
                                        Where-Object { $null -eq $evidence.run.PSObject.Properties[$_] }
                                ).Count -gt 0
                            ) {
                                $tauriPromotionReason = 'evidence-run-binding-invalid'
                            } else {
                                $runIdText = ([string]$evidence.run.id).Trim()
                                $runAttemptText = ([string]$evidence.run.attempt).Trim()
                                $runHeadSha = ([string]$evidence.run.headSha).Trim()
                                $runHeadBranch = ([string]$evidence.run.headBranch).Trim()
                                [int64]$runId = 0
                                [int]$runAttempt = 0
                                if (
                                    $runIdText -cnotmatch '^[1-9][0-9]*$' -or
                                    -not [int64]::TryParse($runIdText, [ref]$runId) -or
                                    $runAttemptText -cnotmatch '^[1-9][0-9]*$' -or
                                    -not [int]::TryParse($runAttemptText, [ref]$runAttempt) -or
                                    $runHeadSha -cnotmatch '^[0-9a-f]{40}$' -or
                                    $runHeadBranch -cnotmatch '^[0-9A-Za-z._/-]+$'
                                ) {
                                    $tauriPromotionReason = 'evidence-run-binding-invalid'
                                } elseif ([string]$evidence.cache.ref -cne "refs/heads/$runHeadBranch") {
                                    $tauriPromotionReason = 'evidence-run-ref-mismatch'
                                } elseif (
                                    $null -eq $evidence.reuse -or
                                    @(
                                        @('exactCacheHit', 'imported', 'seedMiss') |
                                            Where-Object { $null -eq $evidence.reuse.PSObject.Properties[$_] }
                                    ).Count -gt 0
                                ) {
                                    $tauriPromotionReason = 'evidence-reuse-binding-invalid'
                                } elseif (
                                    $evidence.reuse.exactCacheHit -isnot [bool] -or
                                    $evidence.reuse.imported -isnot [bool] -or
                                    $evidence.reuse.seedMiss -isnot [bool]
                                ) {
                                    $tauriPromotionReason = 'evidence-reuse-binding-invalid'
                                } elseif ([bool]$evidence.reuse.seedMiss) {
                                    $tauriPromotionReason = 'seed-miss-not-promotable'
                                } elseif (-not [bool]$evidence.reuse.exactCacheHit -or -not [bool]$evidence.reuse.imported) {
                                    $tauriPromotionReason = 'cache-reuse-not-proven'
                                } else {
                                    $runJson = & gh api "/repos/$Repo/actions/runs/$runId/attempts/$runAttempt" 2>$null
                                    if ($LASTEXITCODE -ne 0) {
                                        $tauriPromotionReason = 'github-run-query-failed'
                                    } else {
                                        $freshRun = $null
                                        try {
                                            $freshRun = $runJson | ConvertFrom-Json
                                        } catch {
                                            $freshRun = $null
                                        }
                                        if (
                                            $null -eq $freshRun -or
                                            $null -eq $freshRun.repository -or
                                            @(
                                                @('id', 'run_attempt', 'head_sha', 'head_branch', 'status', 'conclusion', 'updated_at') |
                                                    Where-Object { $null -eq $freshRun.PSObject.Properties[$_] }
                                            ).Count -gt 0 -or
                                            $null -eq $freshRun.repository.PSObject.Properties['full_name']
                                        ) {
                                            $tauriPromotionReason = 'github-run-response-malformed'
                                        } elseif (
                                            [string]$freshRun.repository.full_name -ine ([string]$Repo).Trim() -or
                                            [string]$freshRun.id -cne [string]$runId -or
                                            [string]$freshRun.run_attempt -cne [string]$runAttempt -or
                                            [string]$freshRun.head_sha -cne $runHeadSha -or
                                            [string]$freshRun.head_branch -cne $runHeadBranch
                                        ) {
                                            $tauriPromotionReason = 'github-run-binding-mismatch'
                                        } elseif (
                                            [string]$freshRun.status -cne 'completed' -or
                                            [string]$freshRun.conclusion -cne 'success'
                                        ) {
                                            $tauriPromotionReason = 'github-run-not-successful'
                                        } else {
                                            $runUpdatedAt = $null
                                            try {
                                                $runUpdatedAt = [DateTimeOffset]::Parse(
                                                    [string]$freshRun.updated_at,
                                                    [System.Globalization.CultureInfo]::InvariantCulture,
                                                    [System.Globalization.DateTimeStyles]::RoundtripKind
                                                )
                                            } catch {
                                                $runUpdatedAt = $null
                                            }
                                            if (
                                                $null -eq $runUpdatedAt -or
                                                $runUpdatedAt.Offset -ne [TimeSpan]::Zero -or
                                                $runUpdatedAt -lt $nowUtc.AddHours(-24) -or
                                                $runUpdatedAt -gt $nowUtc.AddMinutes(5)
                                            ) {
                                                $tauriPromotionReason = 'github-run-stale'
                                            } else {
                                                $tauriPromotionAuthorized = $true
                                                $tauriPromotionReason = 'validated-successful-cache-reuse'
                                                $mutationSuppressed = $false
                                                $tauriPromotedCache = [pscustomobject][ordered]@{
                                                    id = $evidenceCacheId
                                                    key = [string]$evidence.cache.key
                                                    ref = [string]$evidence.cache.ref
                                                    generation = $keyGeneration
                                                    fingerprint = $keyFingerprint
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

$releaseJson = & gh release list --repo $Repo --limit $ListLimit --json tagName
if ($LASTEXITCODE -ne 0) {
    throw "GitHub release-cache inventory failed with exit code $LASTEXITCODE."
}
$parsedReleases = $releaseJson | ConvertFrom-Json
$releases = @($parsedReleases)

# Durable cache snapshots use internal prerelease tags. Keep exactly the
# current schema generation for each family and never match public app tags.
$currentReleaseTags = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
foreach ($tag in @(
    'ffmpeg-profile-b-n7.0-v4',
    'release-cache-backend-runtime-v1',
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
    '^release-cache-backend-runtime-v\d+$',
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
    '^scriber-backend-runtime-Windows-',
    '^scriber-ffmpeg-profile-b-msys2-n7\.0-v[23]-Windows$',
    '^scriber-rust-release-v2-Windows-',
    '^setup-python-',
    '^node-cache-'
)

# Non-Tauri rolling products retain exactly the newest current entry by
# default. Tauri's rollback-safe generation retention is handled separately.
# Durable release assets continue to use their existing single-entry invariant.
$rollingFamilies = @(
    [pscustomobject]@{ Name = 'backend-v2'; Pattern = '^scriber-backend-sidecar-v2-Windows-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'backend-runtime-v1'; Pattern = '^scriber-backend-runtime-v1-Windows-python-'; Retain = $RetainPerRollingFamily },
    [pscustomobject]@{ Name = 'audio'; Pattern = '^scriber-rust-audio-sidecar-Windows-'; Retain = $RetainPerRollingFamily; GenerationPattern = '' },
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
    foreach ($protectedRef in $protectedRefs) {
        $matchingMembers = @(
            $caches |
                Where-Object { [string]$_.ref -eq $protectedRef -and [string]$_.key -match $family.Pattern }
        )
        $members = if (-not [string]::IsNullOrWhiteSpace([string]$family.GenerationPattern)) {
            @(
                $matchingMembers |
                    # Schema generation wins before timestamp for any generic
                    # rolling family that explicitly declares generations.
                    # Tauri uses the evidence-bound retention path below.
                    Sort-Object -Property `
                        @{ Expression = { if ([string]$_.key -match $family.GenerationPattern) { [int]$Matches.generation } else { 0 } }; Descending = $true }, `
                        @{ Expression = { [DateTimeOffset]$_.createdAt }; Descending = $true }, `
                        @{ Expression = { [int64]$_.id }; Descending = $true }, `
                        @{ Expression = { [DateTimeOffset]$_.lastAccessedAt }; Descending = $true }
            )
        } else {
            @(
                $matchingMembers |
                    # Generation recency is creation time. A concurrent older
                    # tag can touch an obsolete cache after its replacement is
                    # created, so lastAccessedAt is only the final tiebreaker.
                    Sort-Object -Property `
                        @{ Expression = { [DateTimeOffset]$_.createdAt }; Descending = $true }, `
                        @{ Expression = { [int64]$_.id }; Descending = $true }, `
                        @{ Expression = { [DateTimeOffset]$_.lastAccessedAt }; Descending = $true }
            )
        }
        foreach ($cache in @($members | Select-Object -Skip ([int]$family.Retain))) {
            if (-not ($deletions | Where-Object { [int64]$_.Cache.id -eq [int64]$cache.id })) {
                $deletions.Add([pscustomobject]@{ Cache = $cache; Reason = "rolling-$($family.Name)-beyond-$($family.Retain)-on-$protectedRef" }) | Out-Null
            }
        }
    }
}

$tauriRetentionProposedDeletionIds = [System.Collections.Generic.HashSet[int64]]::new()
$tauriPromotionProposedDeletionIds = [System.Collections.Generic.HashSet[int64]]::new()
foreach ($protectedRef in $protectedRefs) {
    $members = @(
        $caches |
            Where-Object {
                [string]$_.ref -eq $protectedRef -and
                [string]$_.key -cmatch $tauriFamily.KeyPattern
            } |
            Sort-Object -Property `
                @{ Expression = { if ([string]$_.key -match $tauriFamily.GenerationPattern) { [int]$Matches.generation } else { 0 } }; Descending = $true }, `
                @{ Expression = { [DateTimeOffset]$_.createdAt }; Descending = $true }, `
                @{ Expression = { [int64]$_.id }; Descending = $true }, `
                @{ Expression = { [DateTimeOffset]$_.lastAccessedAt }; Descending = $true }
    )
    foreach ($generation in 1..3) {
        $generationMembers = @(
            $members |
                Where-Object {
                    [string]$_.key -cmatch $tauriFamily.KeyPattern -and
                    [int]$Matches.generation -eq $generation
                }
        )
        if ($generationMembers.Count -eq 0) {
            continue
        }

        $promotionAppliesToRef = (
            $tauriPromotionAuthorized -and
            [string]$tauriPromotedCache.ref -ceq $protectedRef
        )
        if ($promotionAppliesToRef -and $generation -lt [int]$tauriPromotedCache.generation) {
            foreach ($cache in $generationMembers) {
                $deletions.Add([pscustomobject]@{
                    Cache = $cache
                    Reason = "tauri-promoted-generation-$($tauriPromotedCache.generation)-supersedes-$generation-on-$protectedRef"
                }) | Out-Null
                $null = $tauriPromotionProposedDeletionIds.Add([int64]$cache.id)
            }
            continue
        }

        if ($promotionAppliesToRef -and $generation -eq [int]$tauriPromotedCache.generation) {
            foreach ($cache in @($generationMembers | Where-Object { [int64]$_.id -ne [int64]$tauriPromotedCache.id })) {
                $deletions.Add([pscustomobject]@{
                    Cache = $cache
                    Reason = "tauri-promoted-cache-is-sole-generation-$generation-cache-on-$protectedRef"
                }) | Out-Null
                $null = $tauriPromotionProposedDeletionIds.Add([int64]$cache.id)
            }
            continue
        }

        foreach ($cache in @($generationMembers | Select-Object -Skip 1)) {
            $deletions.Add([pscustomobject]@{
                Cache = $cache
                Reason = "tauri-generation-$generation-duplicate-on-$protectedRef"
            }) | Out-Null
            $null = $tauriRetentionProposedDeletionIds.Add([int64]$cache.id)
        }
    }
}

# Ref-scoped caches cannot warm main or sibling tags once those releases have
# completed. Delete only exact refs that the caller explicitly marked prunable;
# foreign or merely unrecognized branches remain untouched by default.
$knownCachePattern = '^((scriber-|setup-python-|node-cache-|msys2-pkgs-).*)$'
foreach ($cache in $caches) {
    if (
        $prunableRefs.Contains([string]$cache.ref) -and
        [string]$cache.key -match $knownCachePattern
    ) {
        $deletions.Add([pscustomobject]@{ Cache = $cache; Reason = 'explicitly-prunable-completed-ref-cache' }) | Out-Null
    }
}

$deletionsById = [System.Collections.Generic.Dictionary[int64, object]]::new()
foreach ($entry in $deletions) {
    $cacheId = [int64]$entry.Cache.id
    if (-not $deletionsById.ContainsKey($cacheId)) {
        $deletionsById.Add($cacheId, $entry)
    }
}
$uniqueDeletions = @($deletionsById.Values | Sort-Object { [int64]$_.Cache.id })
$bytes = [int64](($uniqueDeletions | ForEach-Object { [int64]$_.Cache.sizeInBytes } | Measure-Object -Sum).Sum)
$mainRustDependencyCaches = @(
    $caches |
        Where-Object {
            [string]$_.ref -eq 'refs/heads/main' -and
            [string]$_.key -match '^scriber-rust-dependencies-v1-Windows-'
        }
)
$expectedRustDependencyCaches = @(
    $mainRustDependencyCaches |
        Where-Object { [string]$_.key -ceq $normalizedExpectedRustDependencyKey }
)
$expectedTauriAppCaches = @(
    $caches |
        Where-Object {
            [string]$_.key -ceq $normalizedExpectedTauriAppKey -and
            $protectedRefs.Contains([string]$_.ref)
        }
)
$expectedTauriRefCaches = @()
if ($expectedTauriAppCaches.Count -eq 1) {
    $expectedTauriRef = [string]$expectedTauriAppCaches[0].ref
    $expectedTauriRefCaches = @(
        $caches |
            Where-Object {
                [string]$_.ref -ceq $expectedTauriRef -and
                [string]$_.key -cmatch $tauriFamily.KeyPattern
            }
    )
}
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

$deletedCacheIds = [System.Collections.Generic.HashSet[int64]]::new()
$tauriRetentionDeletedIds = [System.Collections.Generic.HashSet[int64]]::new()
$tauriPromotionDeletedIds = [System.Collections.Generic.HashSet[int64]]::new()
$effectiveApply = [bool]$Apply -and -not $mutationSuppressed
if ($Apply -and $mutationSuppressed) {
    Write-Warning "Release cache mutation suppressed because Tauri promotion evidence was not authorized: $tauriPromotionReason."
}
if ($effectiveApply) {
    foreach ($entry in $uniqueDeletions) {
        & gh cache delete ([string]$entry.Cache.id) --repo $Repo
        if ($LASTEXITCODE -ne 0) {
            throw "Deleting allowlisted cache id $($entry.Cache.id) failed with exit code $LASTEXITCODE."
        }
        $deletedCacheId = [int64]$entry.Cache.id
        $null = $deletedCacheIds.Add($deletedCacheId)
        if ($tauriRetentionProposedDeletionIds.Contains($deletedCacheId)) {
            $null = $tauriRetentionDeletedIds.Add($deletedCacheId)
        }
        if ($tauriPromotionProposedDeletionIds.Contains($deletedCacheId)) {
            $null = $tauriPromotionDeletedIds.Add($deletedCacheId)
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

$verificationPassed = $null
if ($VerifyCurrentGeneration) {
    $verificationIssues = [System.Collections.Generic.List[string]]::new()
    if ($mainRustDependencyCaches.Count -ne 1) {
        $verificationIssues.Add("expected exactly one main Rust dependency cache, found $($mainRustDependencyCaches.Count)") | Out-Null
    }
    if ($expectedRustDependencyCaches.Count -ne 1) {
        $verificationIssues.Add("expected Rust dependency cache '$normalizedExpectedRustDependencyKey' was not the sole exact match") | Out-Null
    }
    if ($expectedTauriAppCaches.Count -ne 1) {
        $verificationIssues.Add("expected Tauri app cache '$normalizedExpectedTauriAppKey' was not the sole exact protected-ref match") | Out-Null
    } elseif ($expectedTauriRefCaches.Count -ne 1) {
        $verificationIssues.Add("expected Tauri app cache '$normalizedExpectedTauriAppKey' was not the only Tauri generation on its protected ref") | Out-Null
    }
    if ($uniqueDeletions.Count -ne 0) {
        $verificationIssues.Add("$($uniqueDeletions.Count) obsolete Actions-cache entries remain") | Out-Null
    }
    if ($obsoleteReleaseTags.Count -ne 0) {
        $verificationIssues.Add("$($obsoleteReleaseTags.Count) obsolete internal cache-release tags remain") | Out-Null
    }
    if ($obsoleteReleaseAssets.Count -ne 0) {
        $verificationIssues.Add("$($obsoleteReleaseAssets.Count) superseded internal cache-release assets remain") | Out-Null
    }

    if ($verificationIssues.Count -gt 0) {
        throw "Release cache generation verification failed: $($verificationIssues -join '; ')."
    }
    $verificationPassed = $true
    Write-Host "Release cache generation verification passed: the expected Rust and Tauri caches are unique, the promoted Tauri ref has one generation, and no GC candidates remain."
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
    protectedRefs = @($protectedRefs | Sort-Object)
    prunableRefs = @($prunableRefs | Sort-Object)
    pruneCurrentRef = [bool]$PruneCurrentRef
    verifyCurrentGeneration = [bool]$VerifyCurrentGeneration
    verificationPassed = $verificationPassed
    expectedRustDependencyKey = $normalizedExpectedRustDependencyKey
    expectedRustDependencyMatches = $expectedRustDependencyCaches.Count
    mainRustDependencyGenerations = $mainRustDependencyCaches.Count
    expectedTauriAppKey = $normalizedExpectedTauriAppKey
    expectedTauriAppMatches = $expectedTauriAppCaches.Count
    expectedTauriRefGenerations = $expectedTauriRefCaches.Count
    mutationSuppressed = $mutationSuppressed
    tauriPromotionAuthorized = $tauriPromotionAuthorized
    tauriPromotionReason = $tauriPromotionReason
    tauriPromotedCache = $tauriPromotedCache
    tauriPromotionEvidenceSha256 = $tauriPromotionEvidenceSha256
    tauriProposedDeletionIds = @(
        @($tauriRetentionProposedDeletionIds) + @($tauriPromotionProposedDeletionIds) |
            Sort-Object -Unique
    )
    tauriDeletedIds = @(
        @($tauriRetentionDeletedIds) + @($tauriPromotionDeletedIds) |
            Sort-Object -Unique
    )
    tauriRetentionProposedDeletionIds = @($tauriRetentionProposedDeletionIds | Sort-Object)
    tauriRetentionDeletedIds = @($tauriRetentionDeletedIds | Sort-Object)
    tauriPromotionProposedDeletionIds = @($tauriPromotionProposedDeletionIds | Sort-Object)
    tauriPromotionDeletedIds = @($tauriPromotionDeletedIds | Sort-Object)
    deletedCacheIds = @($deletedCacheIds | Sort-Object)
} | ConvertTo-Json -Compress
