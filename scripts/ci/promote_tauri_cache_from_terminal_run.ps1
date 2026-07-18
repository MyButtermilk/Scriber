<#
Consumes one passive Tauri promotion-evidence artifact from a completed trusted
Release Windows workflow_dispatch run. Downloaded content is treated only as
data. The only mutation this script can request is deletion of superseded Tauri
Actions-cache generations on refs/heads/main.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [Parameter(Mandatory = $true)]
    [string]$SourceRunId,
    [Parameter(Mandatory = $true)]
    [string]$SourceRunAttempt,
    [Parameter(Mandatory = $true)]
    [string]$SourceHeadSha,
    [Parameter(Mandatory = $true)]
    [string]$SourceHeadBranch,
    [Parameter(Mandatory = $true)]
    [string]$SourceWorkflowId,
    [Parameter(Mandatory = $true)]
    [string]$SourceWorkflowName,
    [Parameter(Mandatory = $true)]
    [string]$SourceWorkflowPath,
    [Parameter(Mandatory = $true)]
    [string]$SourceEvent,
    [Parameter(Mandatory = $true)]
    [string]$TrustedCheckoutSha,
    [string]$TrustedDefaultBranch = "main",
    [string]$ArtifactName = "scriber-tauri-cache-promotion-evidence",
    [string]$EvidenceFileName = "tauri-cache-promotion-evidence.json",
    [AllowEmptyString()]
    [string]$OutputPath = "",
    [ValidateRange(1, 168)]
    [int]$FreshnessHours = 24
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$pruneScript = Join-Path $PSScriptRoot "prune_obsolete_release_caches.ps1"
$keyScript = Join-Path $PSScriptRoot "write_release_cache_keys.ps1"
$normalizedRepo = ([string]$Repo).Trim()
$normalizedSourceRunId = ([string]$SourceRunId).Trim()
$normalizedSourceRunAttempt = ([string]$SourceRunAttempt).Trim()
$normalizedSourceHeadSha = ([string]$SourceHeadSha).Trim()
$normalizedSourceHeadBranch = ([string]$SourceHeadBranch).Trim()
$normalizedSourceWorkflowId = ([string]$SourceWorkflowId).Trim()
$normalizedSourceWorkflowName = ([string]$SourceWorkflowName).Trim()
$normalizedSourceWorkflowPath = ([string]$SourceWorkflowPath).Trim()
$normalizedSourceEvent = ([string]$SourceEvent).Trim()
$normalizedTrustedCheckoutSha = ([string]$TrustedCheckoutSha).Trim()
$normalizedTrustedDefaultBranch = ([string]$TrustedDefaultBranch).Trim()
$normalizedArtifactName = ([string]$ArtifactName).Trim()
$normalizedEvidenceFileName = ([string]$EvidenceFileName).Trim()

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

function Convert-ManifestFingerprintToHashFilesFingerprint {
    param([Parameter(Mandatory = $true)][string]$Fingerprint)

    $normalized = ([string]$Fingerprint).Trim().ToLowerInvariant()
    if ($normalized -cnotmatch '^[0-9a-f]{64}$') {
        throw "Fresh Tauri app manifest fingerprint is invalid."
    }
    $manifestDigestBytes = New-Object byte[] 32
    for ($index = 0; $index -lt $manifestDigestBytes.Length; $index++) {
        $manifestDigestBytes[$index] = [Convert]::ToByte($normalized.Substring($index * 2, 2), 16)
    }
    return Get-Sha256Hex -Bytes $manifestDigestBytes
}

function Invoke-GhJsonText {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    $output = @(& gh @Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage GitHub CLI exit code: $LASTEXITCODE."
    }
    $text = $output -join "`n"
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw "$FailureMessage GitHub returned an empty response."
    }
    try {
        $null = $text | ConvertFrom-Json
    } catch {
        throw "$FailureMessage GitHub returned malformed JSON."
    }
    return $text
}

function ConvertTo-StrictUtcTimestamp {
    param([AllowNull()][object]$Value)

    if ($Value -is [DateTimeOffset]) {
        if ($Value.Offset -ne [TimeSpan]::Zero) {
            return $null
        }
        return [DateTimeOffset]$Value
    }
    if ($Value -is [DateTime]) {
        if ($Value.Kind -ne [DateTimeKind]::Utc) {
            return $null
        }
        return [DateTimeOffset]::new([DateTime]$Value)
    }
    if ($Value -isnot [string] -or [string]$Value -cnotmatch '^\d{4}-\d{2}-\d{2}T.+Z$') {
        return $null
    }
    try {
        $timestamp = [DateTimeOffset]::Parse(
            [string]$Value,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        )
    } catch {
        return $null
    }
    if ($timestamp.Offset -ne [TimeSpan]::Zero) {
        return $null
    }
    return $timestamp
}

function Assert-ExactTerminalRun {
    param([Parameter(Mandatory = $true)][object]$Run)

    $required = @(
        'id',
        'run_attempt',
        'head_sha',
        'head_branch',
        'workflow_id',
        'name',
        'path',
        'event',
        'status',
        'conclusion',
        'updated_at',
        'repository'
    )
    if (
        $null -eq $Run -or
        @($required | Where-Object { $null -eq $Run.PSObject.Properties[$_] }).Count -gt 0 -or
        $null -eq $Run.repository -or
        $null -eq $Run.repository.PSObject.Properties['full_name']
    ) {
        throw "The exact source-run response is malformed."
    }
    if (
        [string]$Run.repository.full_name -cne $normalizedRepo -or
        [string]$Run.id -cne $normalizedSourceRunId -or
        [string]$Run.run_attempt -cne $normalizedSourceRunAttempt -or
        [string]$Run.head_sha -cne $normalizedSourceHeadSha -or
        [string]$Run.head_branch -cne $normalizedSourceHeadBranch -or
        [string]$Run.workflow_id -cne $normalizedSourceWorkflowId -or
        [string]$Run.name -cne $normalizedSourceWorkflowName -or
        [string]$Run.path -cne $normalizedSourceWorkflowPath -or
        [string]$Run.event -cne $normalizedSourceEvent
    ) {
        throw "The exact source-run response does not match the trusted workflow_run event."
    }
    if ([string]$Run.status -cne 'completed' -or [string]$Run.conclusion -cne 'success') {
        throw "The source workflow attempt is not terminal and successful."
    }
    $rawUpdatedAt = $Run.updated_at
    $updatedAt = ConvertTo-StrictUtcTimestamp -Value $rawUpdatedAt
    $nowUtc = [DateTimeOffset]::UtcNow
    if (
        $null -eq $updatedAt -or
        $updatedAt -lt $nowUtc.AddHours(-$FreshnessHours) -or
        $updatedAt -gt $nowUtc.AddMinutes(5)
    ) {
        throw "The source workflow attempt is stale or has an invalid completion timestamp."
    }
}

function Get-ExactTerminalRun {
    $uri = "/repos/$normalizedRepo/actions/runs/$normalizedSourceRunId/attempts/$normalizedSourceRunAttempt"
    $text = Invoke-GhJsonText -Arguments @('api', $uri) -FailureMessage 'Reading the exact source workflow attempt failed.'
    $run = $text | ConvertFrom-Json
    Assert-ExactTerminalRun -Run $run
    return $run
}

function Assert-TrustedDefaultBranchCheckout {
    $localHead = @(& git -C $repoRoot rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or $localHead.Count -ne 1 -or ([string]$localHead[0]).Trim() -cne $normalizedTrustedCheckoutSha) {
        throw "Terminal promotion code is not checked out at the trusted default-branch SHA."
    }
    $refText = Invoke-GhJsonText `
        -Arguments @('api', "/repos/$normalizedRepo/git/ref/heads/$normalizedTrustedDefaultBranch") `
        -FailureMessage 'Reading the current default-branch ref failed.'
    $trustedRef = $refText | ConvertFrom-Json
    if (
        $null -eq $trustedRef -or
        [string]$trustedRef.ref -cne "refs/heads/$normalizedTrustedDefaultBranch" -or
        $null -eq $trustedRef.object -or
        [string]$trustedRef.object.type -cne 'commit' -or
        [string]$trustedRef.object.sha -cne $normalizedTrustedCheckoutSha
    ) {
        throw "Terminal promotion checkout is no longer the current trusted default-branch commit."
    }
}

function Get-ProtectedReleaseInventory {
    $releaseText = Invoke-GhJsonText `
        -Arguments @('api', '--paginate', '--slurp', "/repos/$normalizedRepo/releases?per_page=100") `
        -FailureMessage 'Reading the protected release inventory failed.'
    $releasePages = $releaseText | ConvertFrom-Json
    $protectedReleases = [System.Collections.Generic.List[object]]::new()
    foreach ($page in @($releasePages)) {
        foreach ($release in @($page)) {
            $tag = ([string]$release.tag_name).Trim()
            if ($tag -cnotmatch '^v' -and $tag -cnotmatch '^(?:ffmpeg-profile-b-|release-cache-)') {
                continue
            }
            $releaseId = ([string]$release.id).Trim()
            if ($releaseId -cnotmatch '^[1-9][0-9]*$') {
                throw "Protected release '$tag' has an invalid id."
            }
            $assetText = Invoke-GhJsonText `
                -Arguments @('api', '--paginate', '--slurp', "/repos/$normalizedRepo/releases/$releaseId/assets?per_page=100") `
                -FailureMessage "Reading assets for protected release '$tag' failed."
            $assetPages = $assetText | ConvertFrom-Json
            $assets = [System.Collections.Generic.List[object]]::new()
            foreach ($assetPage in @($assetPages)) {
                foreach ($asset in @($assetPage)) {
                    $assets.Add([pscustomobject][ordered]@{
                        id = [string]$asset.id
                        name = [string]$asset.name
                        size = [string]$asset.size
                        digest = [string]$asset.digest
                        updatedAt = [string]$asset.updated_at
                    }) | Out-Null
                }
            }
            $protectedReleases.Add([pscustomobject][ordered]@{
                id = $releaseId
                tag = $tag
                draft = [bool]$release.draft
                prerelease = [bool]$release.prerelease
                target = [string]$release.target_commitish
                assets = @($assets | Sort-Object -Property @{ Expression = { [string]$_.id } }, @{ Expression = { [string]$_.name } })
            }) | Out-Null
        }
    }
    $canonical = @($protectedReleases | Sort-Object -Property @{ Expression = { [string]$_.id } }, @{ Expression = { [string]$_.tag } }) |
        ConvertTo-Json -Depth 8 -Compress
    return [pscustomobject][ordered]@{
        sha256 = Get-Sha256Hex -Bytes ([System.Text.UTF8Encoding]::new($false)).GetBytes($canonical)
        releaseCount = $protectedReleases.Count
        canonical = $canonical
    }
}

function Get-ArtifactMetadata {
    $artifactText = Invoke-GhJsonText `
        -Arguments @('api', '--paginate', '--slurp', "/repos/$normalizedRepo/actions/runs/$normalizedSourceRunId/artifacts?per_page=100") `
        -FailureMessage 'Reading the source-run artifact inventory failed.'
    $artifactPages = $artifactText | ConvertFrom-Json
    $matches = [System.Collections.Generic.List[object]]::new()
    foreach ($page in @($artifactPages)) {
        foreach ($response in @($page)) {
            foreach ($artifact in @($response.artifacts)) {
                if ([string]$artifact.name -ceq $normalizedArtifactName) {
                    $matches.Add($artifact) | Out-Null
                }
            }
        }
    }
    if ($matches.Count -eq 0) {
        return $null
    }
    if ($matches.Count -ne 1) {
        throw "Expected exactly one passive Tauri promotion artifact, found $($matches.Count)."
    }
    $match = $matches[0]
    [int64]$artifactSize = 0
    if (
        [string]$match.id -cnotmatch '^[1-9][0-9]*$' -or
        [string]$match.size_in_bytes -cnotmatch '^[1-9][0-9]*$' -or
        -not [int64]::TryParse(([string]$match.size_in_bytes), [ref]$artifactSize) -or
        $artifactSize -gt 256KB -or
        $match.expired -isnot [bool] -or
        [bool]$match.expired -or
        $null -eq $match.workflow_run -or
        [string]$match.workflow_run.id -cne $normalizedSourceRunId -or
        [string]$match.workflow_run.head_sha -cne $normalizedSourceHeadSha -or
        [string]$match.workflow_run.head_branch -cne $normalizedSourceHeadBranch
    ) {
        throw "The passive Tauri promotion artifact identity is invalid."
    }
    return $match
}

function Invoke-Pruner {
    param([Parameter(Mandatory = $true)][hashtable]$Parameters)

    $output = @(& $pruneScript @Parameters)
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri promotion pruning failed with exit code $LASTEXITCODE."
    }
    if ($output.Count -lt 1) {
        throw "Tauri promotion pruning returned no machine-readable result."
    }
    try {
        return ([string]$output[$output.Count - 1]) | ConvertFrom-Json
    } catch {
        throw "Tauri promotion pruning returned malformed machine-readable output."
    }
}

if ($normalizedRepo -cnotmatch '^[0-9A-Za-z_.-]+/[0-9A-Za-z_.-]+$') {
    throw "Repository identity is invalid."
}
if (
    $normalizedSourceRunId -cnotmatch '^[1-9][0-9]*$' -or
    $normalizedSourceRunAttempt -cnotmatch '^[1-9][0-9]*$' -or
    $normalizedSourceHeadSha -cnotmatch '^[0-9a-f]{40}$' -or
    $normalizedSourceHeadBranch -cne 'main' -or
    $normalizedSourceWorkflowId -cnotmatch '^[1-9][0-9]*$' -or
    $normalizedSourceWorkflowName -cne 'Release Windows' -or
    $normalizedSourceWorkflowPath -cne '.github/workflows/release-windows.yml' -or
    $normalizedSourceEvent -cne 'workflow_dispatch' -or
    $normalizedTrustedDefaultBranch -cne 'main' -or
    $normalizedTrustedCheckoutSha -cnotmatch '^[0-9a-f]{40}$'
) {
    throw "Only a trusted main-branch Release Windows workflow_dispatch attempt can promote Tauri cache generations."
}
if (
    $normalizedArtifactName -cne 'scriber-tauri-cache-promotion-evidence' -or
    $normalizedEvidenceFileName -cne 'tauri-cache-promotion-evidence.json'
) {
    throw "The passive Tauri promotion artifact identity is fixed."
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI is required for terminal Tauri cache promotion."
}
foreach ($trustedScript in @($pruneScript, $keyScript)) {
    if (-not (Test-Path -LiteralPath $trustedScript -PathType Leaf)) {
        throw "Trusted terminal promotion script is missing: $trustedScript"
    }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required to validate trusted default-branch promotion code."
}
Assert-TrustedDefaultBranchCheckout

$temporaryBase = if (-not [string]::IsNullOrWhiteSpace([string]$env:RUNNER_TEMP)) {
    [System.IO.Path]::GetFullPath([string]$env:RUNNER_TEMP)
} else {
    [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
}
$temporaryRoot = [System.IO.Path]::GetFullPath((Join-Path $temporaryBase "scriber-tauri-promotion-$([Guid]::NewGuid().ToString('N'))"))
$temporaryPrefix = $temporaryBase.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
if (-not $temporaryRoot.StartsWith($temporaryPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Terminal promotion temporary path escaped the intended temporary root."
}
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null

try {
    # 1. Derive the exact current v3 key from trusted default-branch code.
    $keyRelativeDir = "build\terminal-tauri-promotion-$([Guid]::NewGuid().ToString('N'))\cache-keys"
    $keyOutputRoot = Join-Path $repoRoot $keyRelativeDir
    & $keyScript -OutputDir $keyRelativeDir
    if ($LASTEXITCODE -ne 0) {
        throw "Fresh trusted release-cache key derivation failed."
    }
    $tauriKeyFile = Join-Path $keyOutputRoot 'tauri-app-binary.txt'
    if (-not (Test-Path -LiteralPath $tauriKeyFile -PathType Leaf)) {
        throw "Fresh trusted Tauri app cache-key manifest is missing."
    }
    $manifestFingerprint = (Get-FileHash -LiteralPath $tauriKeyFile -Algorithm SHA256).Hash.ToLowerInvariant()
    $actionsFingerprint = Convert-ManifestFingerprintToHashFilesFingerprint -Fingerprint $manifestFingerprint
    $expectedTauriAppKey = "scriber-tauri-app-binary-v3-Windows-$actionsFingerprint"

    # 2. Validate the terminal attempt and download exactly one small JSON file.
    $null = Get-ExactTerminalRun
    $artifact = Get-ArtifactMetadata
    if ($null -eq $artifact) {
        $skipReport = [ordered]@{
            apiVersion = '1'
            sourceRunId = [int64]$normalizedSourceRunId
            outcome = 'not-promotable'
            reason = 'passive-evidence-artifact-absent'
            mutated = $false
        }
        $skipReportJson = $skipReport | ConvertTo-Json -Compress
        if (-not [string]::IsNullOrWhiteSpace([string]$OutputPath)) {
            $resolvedSkipOutputPath = if ([System.IO.Path]::IsPathRooted([string]$OutputPath)) {
                [System.IO.Path]::GetFullPath([string]$OutputPath)
            } else {
                [System.IO.Path]::GetFullPath((Join-Path $repoRoot ([string]$OutputPath)))
            }
            $skipOutputParent = Split-Path -Parent $resolvedSkipOutputPath
            if ([string]::IsNullOrWhiteSpace($skipOutputParent)) {
                throw "Terminal promotion report output requires a parent directory."
            }
            New-Item -ItemType Directory -Force -Path $skipOutputParent | Out-Null
            [System.IO.File]::WriteAllText($resolvedSkipOutputPath, "$skipReportJson`n", [System.Text.UTF8Encoding]::new($false))
        }
        $skipReportJson
        return
    }
    $downloadRoot = Join-Path $temporaryRoot 'artifact'
    New-Item -ItemType Directory -Path $downloadRoot | Out-Null
    & gh run download $normalizedSourceRunId `
        --repo $normalizedRepo `
        --name $normalizedArtifactName `
        --dir $downloadRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Downloading passive Tauri promotion evidence failed."
    }
    $downloadedItems = @(Get-ChildItem -LiteralPath $downloadRoot -Recurse -Force)
    if (@($downloadedItems | Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 }).Count -gt 0) {
        throw "The passive Tauri promotion artifact contains a reparse point."
    }
    $downloadedDirectories = @($downloadedItems | Where-Object { $_.PSIsContainer })
    $downloadedFiles = @($downloadedItems | Where-Object { -not $_.PSIsContainer })
    if (
        $downloadedDirectories.Count -ne 0 -or
        $downloadedFiles.Count -ne 1 -or
        [string]$downloadedFiles[0].Name -cne $normalizedEvidenceFileName
    ) {
        throw "The passive Tauri promotion artifact must contain exactly '$normalizedEvidenceFileName'."
    }
    $evidenceItem = $downloadedFiles[0]
    if (
        ($evidenceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        [int64]$evidenceItem.Length -le 0 -or
        [int64]$evidenceItem.Length -gt 256KB
    ) {
        throw "The downloaded passive Tauri promotion evidence file is unsafe or oversized."
    }
    $evidenceSha256 = (Get-FileHash -LiteralPath $evidenceItem.FullName -Algorithm SHA256).Hash.ToLowerInvariant()

    # The completed producer may outlive a later main push. Recheck trusted
    # code after artifact work, before any promotion planning.
    Assert-TrustedDefaultBranchCheckout
    $beforeReleases = Get-ProtectedReleaseInventory
    $commonPruneParameters = @{
        Repo = $normalizedRepo
        TauriPromotionOnly = $true
        TauriPromotionEvidencePath = $evidenceItem.FullName
        ExpectedTauriPromotionEvidenceSha256 = $evidenceSha256
        ExpectedTauriAppKey = $expectedTauriAppKey
        ExpectedSourceRunId = $normalizedSourceRunId
        ExpectedSourceRunAttempt = $normalizedSourceRunAttempt
        ExpectedSourceHeadSha = $normalizedSourceHeadSha
        ExpectedSourceHeadBranch = $normalizedSourceHeadBranch
        ExpectedSourceWorkflowId = $normalizedSourceWorkflowId
        ExpectedSourceWorkflowName = $normalizedSourceWorkflowName
        ExpectedSourceWorkflowPath = $normalizedSourceWorkflowPath
        ExpectedSourceEvent = $normalizedSourceEvent
    }

    # 3. Authorize without mutation, then bind apply to that exact inventory.
    $dryRun = Invoke-Pruner -Parameters $commonPruneParameters
    if (
        $dryRun.tauriPromotionAuthorized -isnot [bool] -or
        -not [bool]$dryRun.tauriPromotionAuthorized -or
        [bool]$dryRun.mutationSuppressed -or
        [string]$dryRun.tauriPromotionReason -cne 'validated-successful-cache-reuse' -or
        [string]$dryRun.tauriPromotionDeletionSetSha256 -cnotmatch '^[0-9a-f]{64}$'
    ) {
        throw "Passive Tauri promotion evidence did not authorize the dry-run."
    }

    # 4. Apply only the dry-run-bound Tauri/main deletion set. The pruner makes
    # a fresh exact-attempt query and a fresh cache inventory pass here.
    $applyParameters = @{}
    foreach ($entry in $commonPruneParameters.GetEnumerator()) {
        $applyParameters[$entry.Key] = $entry.Value
    }
    $applyParameters.Apply = $true
    $applyParameters.ExpectedTauriPromotionDeletionSetSha256 = [string]$dryRun.tauriPromotionDeletionSetSha256
    $applyParameters.ExpectedTrustedDefaultBranchSha = $normalizedTrustedCheckoutSha
    # Recheck immediately before invoking Apply. The pruner repeats this ref
    # check once more after its fresh inventory pass and before the first delete.
    Assert-TrustedDefaultBranchCheckout
    $applyResult = Invoke-Pruner -Parameters $applyParameters
    if (
        $applyResult.tauriPromotionAuthorized -isnot [bool] -or
        -not [bool]$applyResult.tauriPromotionAuthorized -or
        [bool]$applyResult.mutationSuppressed -or
        [string]$applyResult.tauriPromotionDeletionSetSha256 -cne [string]$dryRun.tauriPromotionDeletionSetSha256
    ) {
        throw "Tauri promotion apply did not preserve the terminal authorization and dry-run binding."
    }

    # 5. Postflight uses neither producer evidence nor the apply inventory.
    $postflight = Invoke-Pruner -Parameters @{
        Repo = $normalizedRepo
        TauriPromotionOnly = $true
        VerifyCurrentGeneration = $true
        ExpectedTauriAppKey = $expectedTauriAppKey
    }
    if ($postflight.verificationPassed -isnot [bool] -or -not [bool]$postflight.verificationPassed) {
        throw "Fresh Tauri promotion postflight did not pass."
    }

    $afterReleases = Get-ProtectedReleaseInventory
    if (
        [string]$beforeReleases.sha256 -cne [string]$afterReleases.sha256 -or
        [string]$beforeReleases.canonical -cne [string]$afterReleases.canonical
    ) {
        throw "Public or internal cache releases changed during Tauri Actions-cache promotion."
    }

    $report = [ordered]@{
        apiVersion = '1'
        source = [ordered]@{
            runId = [int64]$normalizedSourceRunId
            runAttempt = [int]$normalizedSourceRunAttempt
            headSha = $normalizedSourceHeadSha
            headBranch = $normalizedSourceHeadBranch
            workflowId = [int64]$normalizedSourceWorkflowId
            workflowName = $normalizedSourceWorkflowName
            workflowPath = $normalizedSourceWorkflowPath
            event = $normalizedSourceEvent
        }
        artifact = [ordered]@{
            id = [int64]$artifact.id
            name = $normalizedArtifactName
            evidenceSha256 = $evidenceSha256
        }
        expectedTauriAppKey = $expectedTauriAppKey
        manifestFingerprint = $manifestFingerprint
        dryRunDeletionSetSha256 = [string]$dryRun.tauriPromotionDeletionSetSha256
        proposedDeletionIds = @($dryRun.tauriProposedDeletionIds)
        deletedIds = @($applyResult.deletedCacheIds)
        postflightPassed = [bool]$postflight.verificationPassed
        protectedReleaseInventory = [ordered]@{
            releaseCount = [int]$afterReleases.releaseCount
            beforeSha256 = [string]$beforeReleases.sha256
            afterSha256 = [string]$afterReleases.sha256
            unchanged = $true
        }
    }
    $reportJson = $report | ConvertTo-Json -Depth 10 -Compress
    if (-not [string]::IsNullOrWhiteSpace([string]$OutputPath)) {
        $resolvedOutputPath = if ([System.IO.Path]::IsPathRooted([string]$OutputPath)) {
            [System.IO.Path]::GetFullPath([string]$OutputPath)
        } else {
            [System.IO.Path]::GetFullPath((Join-Path $repoRoot ([string]$OutputPath)))
        }
        $outputParent = Split-Path -Parent $resolvedOutputPath
        if ([string]::IsNullOrWhiteSpace($outputParent)) {
            throw "Terminal promotion report output requires a parent directory."
        }
        New-Item -ItemType Directory -Force -Path $outputParent | Out-Null
        [System.IO.File]::WriteAllText($resolvedOutputPath, "$reportJson`n", [System.Text.UTF8Encoding]::new($false))
    }
    $reportJson
} finally {
    if (Test-Path -LiteralPath $temporaryRoot -PathType Container) {
        $resolvedCleanupPath = [System.IO.Path]::GetFullPath($temporaryRoot)
        if (-not $resolvedCleanupPath.StartsWith($temporaryPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean a terminal promotion path outside the intended temporary root."
        }
        Remove-Item -LiteralPath $resolvedCleanupPath -Recurse -Force
    }
    if (Test-Path -LiteralPath $keyOutputRoot -PathType Container) {
        $resolvedKeyCleanupPath = [System.IO.Path]::GetFullPath($keyOutputRoot)
        $buildPrefix = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'build')).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
        if (-not $resolvedKeyCleanupPath.StartsWith($buildPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean a derived key path outside the repository build directory."
        }
        Remove-Item -LiteralPath (Split-Path -Parent $resolvedKeyCleanupPath) -Recurse -Force
    }
}
