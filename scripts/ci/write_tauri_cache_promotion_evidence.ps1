<#
Writes passive Tauri cache-promotion evidence. The producer run must only upload
this JSON as data. A later workflow-run consumer, after GitHub reports the
producer completed successfully, may validate it and invoke cache pruning.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,
    [Parameter(Mandatory = $true)]
    [string]$Ref,
    [Parameter(Mandatory = $true)]
    [string]$CacheRef,
    [Parameter(Mandatory = $true)]
    [string]$RunId,
    [Parameter(Mandatory = $true)]
    [string]$RunAttempt,
    [Parameter(Mandatory = $true)]
    [string]$HeadSha,
    [Parameter(Mandatory = $true)]
    [string]$HeadBranch,
    [AllowEmptyString()]
    [string]$CacheId = "",
    [Parameter(Mandatory = $true)]
    [string]$ExpectedCacheKey,
    [AllowEmptyString()]
    [string]$ActionsCacheKey = "",
    [Parameter(Mandatory = $true)]
    [string]$ActionsCacheHit,
    [AllowEmptyString()]
    [string]$ImportUsable = "",
    [AllowEmptyString()]
    [string]$ColdProductsUsable = "",
    [AllowEmptyString()]
    [string]$UsePrebuiltTauriApp = "",
    [AllowEmptyString()]
    [string]$FrontendDependenciesRequired = "",
    [AllowEmptyString()]
    [string]$ImportCliContractSha256 = "",
    [AllowEmptyString()]
    [string]$SelectedCliContractSha256 = "",
    [AllowEmptyString()]
    [string]$ImportCliVersionOutput = "",
    [AllowEmptyString()]
    [string]$SelectedCliVersionOutput = "",
    [AllowEmptyString()]
    [string]$TauriExportOutcome = "",
    [AllowEmptyString()]
    [string]$TauriSaveOutcome = "",
    [AllowEmptyString()]
    [string]$FrontendNodeModulesRestoreOutcome = "",
    [AllowEmptyString()]
    [string]$NpmPackageStoreRestoreOutcome = "",
    [AllowEmptyString()]
    [string]$FrontendInstallOutcome = "",
    [AllowEmptyString()]
    [string]$FrontendCachedUseOutcome = "",
    [AllowEmptyString()]
    [string]$FrontendDependencySaveOutcome = "",
    [AllowEmptyString()]
    [string]$NpmPackageStoreSaveOutcome = "",
    [AllowEmptyString()]
    [string]$CacheSummaryPath = "",
    [AllowEmptyString()]
    [string]$BuildTimingPath = "",
    [AllowEmptyString()]
    [string]$LatestJsonPath = "",
    [AllowEmptyString()]
    [string]$Sha256SumsPath = "",
    [AllowEmptyString()]
    [string]$BundlePath = "",
    [AllowEmptyString()]
    [string]$ExpectedVersion = "",
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [ValidateRange(1, 168)]
    [int]$FreshnessHours = 24
)

$ErrorActionPreference = "Stop"
$script:PromotionReasons = [System.Collections.Generic.List[string]]::new()

function Add-PromotionReason {
    param([Parameter(Mandatory = $true)][string]$Code)

    if (-not $script:PromotionReasons.Contains($Code)) {
        $script:PromotionReasons.Add($Code) | Out-Null
    }
}

function Test-JsonProperty {
    param(
        [AllowNull()][object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name
    )

    return $null -ne $InputObject -and $null -ne $InputObject.PSObject.Properties[$Name]
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            $digest = $sha256.ComputeHash($stream)
        } finally {
            $sha256.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
    return ([System.BitConverter]::ToString($digest) -replace '-', '').ToLowerInvariant()
}

function Resolve-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Path))
}

function Test-RegularInputFile {
    param(
        [AllowEmptyString()][string]$Path,
        [Parameter(Mandatory = $true)][string]$ReasonPrefix,
        [int64]$MaxBytes = 0
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        Add-PromotionReason "$ReasonPrefix-missing"
        return $null
    }
    try {
        $resolvedPath = Resolve-FullPath -Path $Path
    } catch {
        Add-PromotionReason "$ReasonPrefix-path-invalid"
        return $null
    }
    if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
        Add-PromotionReason "$ReasonPrefix-missing"
        return $null
    }
    try {
        $item = Get-Item -LiteralPath $resolvedPath
    } catch {
        Add-PromotionReason "$ReasonPrefix-unreadable"
        return $null
    }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Add-PromotionReason "$ReasonPrefix-reparse-point"
        return $null
    }
    if ($MaxBytes -gt 0 -and [int64]$item.Length -gt $MaxBytes) {
        Add-PromotionReason "$ReasonPrefix-too-large"
        return $null
    }
    return $item
}

function Read-StrictUtf8File {
    param(
        [AllowEmptyString()][string]$Path,
        [Parameter(Mandatory = $true)][string]$ReasonPrefix,
        [int64]$MaxBytes = 1MB
    )

    $item = Test-RegularInputFile -Path $Path -ReasonPrefix $ReasonPrefix -MaxBytes $MaxBytes
    if ($null -eq $item) {
        return $null
    }
    try {
        $bytes = [System.IO.File]::ReadAllBytes($item.FullName)
        if ($bytes.Length -gt $MaxBytes) {
            Add-PromotionReason "$ReasonPrefix-too-large"
            return $null
        }
        $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
        $text = $strictUtf8.GetString($bytes)
        if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) {
            $text = $text.Substring(1)
        }
        return $text
    } catch {
        Add-PromotionReason "$ReasonPrefix-malformed"
        return $null
    }
}

function Read-JsonFixture {
    param(
        [AllowEmptyString()][string]$Path,
        [Parameter(Mandatory = $true)][string]$ReasonPrefix
    )

    $text = Read-StrictUtf8File -Path $Path -ReasonPrefix $ReasonPrefix -MaxBytes 2MB
    if ($null -eq $text) {
        return $null
    }
    try {
        $value = $text | ConvertFrom-Json
    } catch {
        Add-PromotionReason "$ReasonPrefix-malformed"
        return $null
    }
    if ($null -eq $value -or $value -is [System.Array]) {
        Add-PromotionReason "$ReasonPrefix-malformed"
        return $null
    }
    return $value
}

function Get-CheckedInTauriCliContract {
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    $contractPath = Join-Path $repoRoot "packaging\tauri-cli-cache-contract.json"
    if (-not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
        throw "Checked-in Tauri CLI cache contract is missing: $contractPath"
    }
    $contractItem = Get-Item -LiteralPath $contractPath
    if (($contractItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Checked-in Tauri CLI cache contract must not be a reparse point."
    }
    if ([int64]$contractItem.Length -le 0 -or [int64]$contractItem.Length -gt 64KB) {
        throw "Checked-in Tauri CLI cache contract has an invalid size."
    }

    $bytes = [System.IO.File]::ReadAllBytes($contractItem.FullName)
    if (
        ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) -or
        ($bytes -contains [byte]0x0D) -or
        $bytes[$bytes.Length - 1] -ne [byte]0x0A
    ) {
        throw "Checked-in Tauri CLI cache contract must be BOM-free UTF-8 with LF-only line endings and a final LF."
    }

    try {
        $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
        $text = $strictUtf8.GetString($bytes)
        $contract = $text | ConvertFrom-Json
    } catch {
        throw "Checked-in Tauri CLI cache contract is not strict UTF-8 JSON."
    }
    if (
        $null -eq $contract -or
        [string]$contract.schemaVersion -cne '1' -or
        [string]$contract.name -cne 'scriber-tauri-cli-cache-contract' -or
        [string]$contract.target -cne 'win32-x64-msvc' -or
        [string]$contract.version -cnotmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$' -or
        [string]$contract.versionOutput -cne "tauri-cli $([string]$contract.version)" -or
        [string]$contract.entrypoint -cne 'tauri-cli/node_modules/@tauri-apps/cli/tauri.js'
    ) {
        throw "Checked-in Tauri CLI cache contract identity is invalid."
    }

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash($bytes)
    } finally {
        $sha256.Dispose()
    }
    return [pscustomobject][ordered]@{
        sha256 = ([System.BitConverter]::ToString($digest) -replace '-', '').ToLowerInvariant()
        version = [string]$contract.version
        versionOutput = [string]$contract.versionOutput
    }
}

function Convert-ManifestFingerprintToHashFilesFingerprint {
    param([Parameter(Mandatory = $true)][string]$Fingerprint)

    $normalized = ([string]$Fingerprint).Trim().ToLowerInvariant()
    if ($normalized -cnotmatch '^[0-9a-f]{64}$') {
        throw "Tauri app manifest fingerprint must be a lowercase SHA-256 digest."
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
    return -join ($hashFilesDigest | ForEach-Object { $_.ToString('x2') })
}

function ConvertTo-StrictUtcTimestamp {
    param([AllowNull()][object]$Value)

    if ($Value -isnot [string]) {
        return $null
    }
    $text = [string]$Value
    if ($text -cnotmatch '^\d{4}-\d{2}-\d{2}T.+Z$') {
        return $null
    }
    try {
        $timestamp = [DateTimeOffset]::Parse(
            $text,
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

function Test-StrictBoolean {
    param(
        [AllowNull()][object]$Value,
        [bool]$Expected
    )

    return $Value -is [bool] -and [bool]$Value -eq $Expected
}

function Test-FreshTimestamp {
    param(
        [AllowNull()][object]$Timestamp,
        [DateTimeOffset]$NowUtc,
        [int]$Hours
    )

    return (
        $null -ne $Timestamp -and
        $Timestamp -ge $NowUtc.AddHours(-$Hours) -and
        $Timestamp -le $NowUtc.AddMinutes(5)
    )
}

function Test-FileWrittenAfter {
    param(
        [AllowNull()][object]$Item,
        [AllowNull()][object]$MinimumTimestamp,
        [DateTimeOffset]$NowUtc,
        [int]$Hours
    )

    if ($null -eq $Item -or $null -eq $MinimumTimestamp) {
        return $false
    }
    $writtenAt = [DateTimeOffset]::new($Item.LastWriteTimeUtc, [TimeSpan]::Zero)
    return (
        $writtenAt -ge $MinimumTimestamp -and
        $writtenAt -ge $NowUtc.AddHours(-$Hours) -and
        $writtenAt -le $NowUtc.AddMinutes(5)
    )
}

$nowUtc = [DateTimeOffset]::UtcNow
$checkedInCliContract = Get-CheckedInTauriCliContract
$normalizedRepo = ([string]$Repo).Trim()
$normalizedRef = ([string]$Ref).Trim()
$normalizedCacheRef = ([string]$CacheRef).Trim()
$normalizedHeadSha = ([string]$HeadSha).Trim()
$normalizedHeadBranch = ([string]$HeadBranch).Trim()
$normalizedExpectedCacheKey = ([string]$ExpectedCacheKey).Trim()
$normalizedActionsCacheKey = ([string]$ActionsCacheKey).Trim()
$cacheKeyMatch = [regex]::Match(
    $normalizedExpectedCacheKey,
    '^scriber-tauri-app-binary-v(?<generation>3)-Windows-(?<fingerprint>[0-9a-f]{64})$',
    [System.Text.RegularExpressions.RegexOptions]::CultureInvariant
)
$cacheGeneration = if ($cacheKeyMatch.Success) { [int]$cacheKeyMatch.Groups['generation'].Value } else { $null }
$cacheFingerprint = if ($cacheKeyMatch.Success) { [string]$cacheKeyMatch.Groups['fingerprint'].Value } else { $null }
[int64]$parsedCacheId = 0
$cacheIdValid = (
    ([string]$CacheId).Trim() -cmatch '^[1-9][0-9]*$' -and
    [int64]::TryParse(([string]$CacheId).Trim(), [ref]$parsedCacheId)
)
[int64]$parsedRunId = 0
$runIdValid = (
    ([string]$RunId).Trim() -cmatch '^[1-9][0-9]*$' -and
    [int64]::TryParse(([string]$RunId).Trim(), [ref]$parsedRunId)
)
[int]$parsedRunAttempt = 0
$runAttemptValid = (
    ([string]$RunAttempt).Trim() -cmatch '^[1-9][0-9]*$' -and
    [int]::TryParse(([string]$RunAttempt).Trim(), [ref]$parsedRunAttempt)
)
$seedMiss = [string]$ActionsCacheHit -ceq 'false'
$observedExactHit = [string]$ActionsCacheHit -ceq 'true'
$checks = [ordered]@{
    evaluated = -not $seedMiss
    identity = $false
    cacheReuse = $false
    cacheKeyBinding = $false
    cliContract = $false
    cliVersion = $false
    stepIsolation = $false
    cacheSummary = $false
    buildTiming = $false
    releaseMetadata = $false
    bundleIdentity = $false
}
$bundleIdentity = $null
$manifestFingerprint = $null

if ($seedMiss) {
    Add-PromotionReason 'seed-miss'
} else {
    $before = $script:PromotionReasons.Count
    if ($normalizedRepo -cnotmatch '^[0-9A-Za-z_.-]+/[0-9A-Za-z_.-]+$') {
        Add-PromotionReason 'repository-invalid'
    }
    if ($normalizedRef -cnotmatch '^refs/heads/[0-9A-Za-z._/-]+$') {
        Add-PromotionReason 'ref-invalid'
    }
    if ($normalizedCacheRef -cnotmatch '^refs/heads/[0-9A-Za-z._/-]+$') {
        Add-PromotionReason 'cache-ref-invalid'
    } elseif ($normalizedCacheRef -cne $normalizedRef) {
        Add-PromotionReason 'cache-ref-mismatch'
    }
    if ($normalizedHeadBranch -cnotmatch '^[0-9A-Za-z._/-]+$') {
        Add-PromotionReason 'head-branch-invalid'
    } elseif ($normalizedRef -cne "refs/heads/$normalizedHeadBranch") {
        Add-PromotionReason 'ref-head-branch-mismatch'
    }
    if ($normalizedHeadSha -cnotmatch '^[0-9a-f]{40}$') {
        Add-PromotionReason 'head-sha-invalid'
    }
    if (-not $runIdValid) {
        Add-PromotionReason 'run-id-invalid'
    }
    if (-not $runAttemptValid) {
        Add-PromotionReason 'run-attempt-invalid'
    }
    if (-not $cacheIdValid) {
        Add-PromotionReason 'cache-id-invalid'
    }
    if (-not $cacheKeyMatch.Success) {
        Add-PromotionReason 'expected-cache-key-incomplete'
    }
    if ($normalizedActionsCacheKey -cne $normalizedExpectedCacheKey) {
        Add-PromotionReason 'actions-cache-key-mismatch'
    }
    $checks.identity = $script:PromotionReasons.Count -eq $before

    $before = $script:PromotionReasons.Count
    if (-not $observedExactHit) {
        Add-PromotionReason 'actions-cache-hit-invalid'
    }
    if ([string]$ImportUsable -cne 'true') {
        Add-PromotionReason 'normal-import-not-usable'
    }
    if ([string]$ColdProductsUsable -cne 'false') {
        Add-PromotionReason 'cold-product-selected'
    }
    if ([string]$UsePrebuiltTauriApp -cne 'true') {
        Add-PromotionReason 'prebuilt-tauri-not-selected'
    }
    if ([string]$FrontendDependenciesRequired -cne 'false') {
        Add-PromotionReason 'frontend-dependencies-required'
    }
    $checks.cacheReuse = $script:PromotionReasons.Count -eq $before

    $before = $script:PromotionReasons.Count
    foreach ($sha in @($ImportCliContractSha256, $SelectedCliContractSha256)) {
        if ([string]$sha -cnotmatch '^[0-9a-f]{64}$') {
            Add-PromotionReason 'cli-contract-sha-invalid'
            break
        }
    }
    if (
        [string]$ImportCliContractSha256 -cne [string]$checkedInCliContract.sha256 -or
        [string]$SelectedCliContractSha256 -cne [string]$checkedInCliContract.sha256
    ) {
        Add-PromotionReason 'cli-contract-sha-mismatch'
    }
    $checks.cliContract = $script:PromotionReasons.Count -eq $before

    $before = $script:PromotionReasons.Count
    if ([string]$checkedInCliContract.versionOutput -cnotmatch '^tauri-cli \d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\z') {
        Add-PromotionReason 'cli-version-output-invalid'
    }
    if (
        [string]$ImportCliVersionOutput -cne [string]$checkedInCliContract.versionOutput -or
        [string]$SelectedCliVersionOutput -cne [string]$checkedInCliContract.versionOutput
    ) {
        Add-PromotionReason 'cli-version-output-mismatch'
    }
    $checks.cliVersion = $script:PromotionReasons.Count -eq $before

    $before = $script:PromotionReasons.Count
    $expectedSkippedOutcomes = [ordered]@{
        'tauri-export-not-skipped' = $TauriExportOutcome
        'tauri-save-not-skipped' = $TauriSaveOutcome
        'frontend-node-modules-restore-not-skipped' = $FrontendNodeModulesRestoreOutcome
        'npm-package-store-restore-not-skipped' = $NpmPackageStoreRestoreOutcome
        'frontend-install-not-skipped' = $FrontendInstallOutcome
        'frontend-cached-use-not-skipped' = $FrontendCachedUseOutcome
        'frontend-dependency-save-not-skipped' = $FrontendDependencySaveOutcome
        'npm-package-store-save-not-skipped' = $NpmPackageStoreSaveOutcome
    }
    foreach ($entry in $expectedSkippedOutcomes.GetEnumerator()) {
        if ([string]$entry.Value -cne 'skipped') {
            Add-PromotionReason ([string]$entry.Key)
        }
    }
    $checks.stepIsolation = $script:PromotionReasons.Count -eq $before

    $summaryReasonsBefore = $script:PromotionReasons.Count
    $cacheSummary = Read-JsonFixture -Path $CacheSummaryPath -ReasonPrefix 'cache-summary'
    $summaryGeneratedAt = $null
    if ($null -ne $cacheSummary) {
        $summaryGeneratedAt = ConvertTo-StrictUtcTimestamp $cacheSummary.generatedAtUtc
        if ([string]$cacheSummary.schemaVersion -cne '2') {
            Add-PromotionReason 'cache-summary-schema-mismatch'
        }
        if (-not (Test-FreshTimestamp -Timestamp $summaryGeneratedAt -NowUtc $nowUtc -Hours $FreshnessHours)) {
            Add-PromotionReason 'cache-summary-stale'
        }
        if (
            -not (Test-JsonProperty $cacheSummary 'runIdentity') -or
            [string]$cacheSummary.runIdentity.repository -cne $normalizedRepo -or
            [string]$cacheSummary.runIdentity.runId -cne [string]$parsedRunId -or
            [string]$cacheSummary.runIdentity.runAttempt -cne [string]$parsedRunAttempt -or
            [string]$cacheSummary.runIdentity.headSha -cne $normalizedHeadSha -or
            [string]$cacheSummary.runIdentity.ref -cne $normalizedRef
        ) {
            Add-PromotionReason 'cache-summary-run-identity-mismatch'
        }
        if (
            -not (Test-JsonProperty $cacheSummary 'cacheKeyParity') -or
            [string]$cacheSummary.cacheKeyParity.apiVersion -cne '1' -or
            [string]$cacheSummary.cacheKeyParity.planner.tauriAppBinary -cnotmatch '^[0-9a-f]{64}$' -or
            [string]$cacheSummary.cacheKeyParity.packager.tauriAppBinary -cne [string]$cacheSummary.cacheKeyParity.planner.tauriAppBinary -or
            -not (Test-StrictBoolean $cacheSummary.cacheKeyParity.componentMatches.tauriAppBinary $true) -or
            -not (Test-StrictBoolean $cacheSummary.cacheKeyParity.allMatch $true)
        ) {
            Add-PromotionReason 'cache-summary-fingerprint-mismatch'
        }

        $keyBindingReasonsBefore = $script:PromotionReasons.Count
        $manifestFingerprint = [string]$cacheSummary.cacheKeyParity.planner.tauriAppBinary
        if (
            $manifestFingerprint -cnotmatch '^[0-9a-f]{64}$' -or
            [string]$cacheSummary.cacheKeyParity.packager.tauriAppBinary -cne $manifestFingerprint
        ) {
            Add-PromotionReason 'cache-summary-key-binding-mismatch'
        } else {
            try {
                $derivedActionsFingerprint = Convert-ManifestFingerprintToHashFilesFingerprint -Fingerprint $manifestFingerprint
                if (-not $cacheKeyMatch.Success -or $derivedActionsFingerprint -cne $cacheFingerprint) {
                    Add-PromotionReason 'cache-summary-key-binding-mismatch'
                }
            } catch {
                Add-PromotionReason 'cache-summary-key-binding-mismatch'
            }
        }
        $checks.cacheKeyBinding = $script:PromotionReasons.Count -eq $keyBindingReasonsBefore

        if (-not (Test-StrictBoolean $cacheSummary.coldProductsUsed $false)) {
            Add-PromotionReason 'cache-summary-cold-product-selected'
        }
        if (
            -not (Test-JsonProperty $cacheSummary 'tauriAppBinary') -or
            -not (Test-StrictBoolean $cacheSummary.tauriAppBinary.actionsCacheExact $true) -or
            -not (Test-StrictBoolean $cacheSummary.tauriAppBinary.importUsable $true) -or
            [string]$cacheSummary.tauriAppBinary.importedSha256 -cnotmatch '^[0-9a-f]{64}$' -or
            -not (Test-StrictBoolean $cacheSummary.tauriAppBinary.cliAttested $true) -or
            -not (Test-StrictBoolean $cacheSummary.tauriAppBinary.frontendDependenciesRequired $false)
        ) {
            Add-PromotionReason 'cache-summary-tauri-reuse-mismatch'
        }
        $tauriRows = @($cacheSummary.rows | Where-Object { [string]$_.Name -ceq 'Tauri app binary' })
        if (
            $tauriRows.Count -ne 1 -or
            [string]$tauriRows[0].Actions -cne 'exact' -or
            [string]$tauriRows[0].ReleaseArtifact -cne 'n/a' -or
            [string]$tauriRows[0].Effective -cne 'actions-cache-exact-validated'
        ) {
            Add-PromotionReason 'cache-summary-tauri-row-mismatch'
        }
    } else {
        Add-PromotionReason 'cache-summary-key-binding-mismatch'
    }
    $checks.cacheSummary = $script:PromotionReasons.Count -eq $summaryReasonsBefore

    $timingReasonsBefore = $script:PromotionReasons.Count
    $buildTiming = Read-JsonFixture -Path $BuildTimingPath -ReasonPrefix 'build-timing'
    $buildGeneratedAt = $null
    if ($null -ne $buildTiming) {
        $buildGeneratedAt = ConvertTo-StrictUtcTimestamp $buildTiming.generatedAt
        if ([string]$buildTiming.apiVersion -cne '1') {
            Add-PromotionReason 'build-timing-schema-mismatch'
        }
        if (
            -not (Test-FreshTimestamp -Timestamp $buildGeneratedAt -NowUtc $nowUtc -Hours $FreshnessHours) -or
            $null -eq $summaryGeneratedAt -or
            $buildGeneratedAt -lt $summaryGeneratedAt
        ) {
            Add-PromotionReason 'build-timing-stale-or-before-summary'
        }
        $bundlePhases = @($buildTiming.phases | Where-Object { [string]$_.label -ceq 'Tauri Windows bundle' })
        if (
            $bundlePhases.Count -ne 1 -or
            -not (Test-StrictBoolean $bundlePhases[0].ok $true) -or
            [string]$bundlePhases[0].durationMs -cnotmatch '^\d+$'
        ) {
            Add-PromotionReason 'tauri-windows-bundle-phase-invalid'
        }
        if (@($buildTiming.phases | Where-Object { [string]$_.label -ceq 'Tauri app binary build' }).Count -ne 0) {
            Add-PromotionReason 'unexpected-tauri-app-build-phase'
        }
        if (
            -not (Test-JsonProperty $buildTiming 'buildMode') -or
            [string]$buildTiming.totalDurationMs -cnotmatch '^[1-9][0-9]*$' -or
            [string]$buildTiming.buildMode.artifactKind -cne 'installer' -or
            -not (Test-StrictBoolean $buildTiming.buildMode.prebuiltTauriApp $true) -or
            -not (Test-StrictBoolean $buildTiming.buildMode.isolatedTauriCli $true) -or
            -not (Test-StrictBoolean $buildTiming.buildMode.installerBuilt $true) -or
            -not (Test-StrictBoolean $buildTiming.buildMode.tauriAppBuiltBeforeBundle $false) -or
            -not (Test-StrictBoolean $buildTiming.buildMode.tauriAppBuiltInParallel $false)
        ) {
            Add-PromotionReason 'build-mode-not-isolated-prebuilt-tauri'
        }
    }
    $checks.buildTiming = $script:PromotionReasons.Count -eq $timingReasonsBefore

    $bundleReasonsBefore = $script:PromotionReasons.Count
    $bundleItem = Test-RegularInputFile -Path $BundlePath -ReasonPrefix 'bundle' -MaxBytes 1GB
    $expectedBundleName = if ($ExpectedVersion) { "Scriber_$($ExpectedVersion)_x64-setup.exe" } else { "" }
    $bundleSha256 = $null
    if ([string]$ExpectedVersion -cnotmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
        Add-PromotionReason 'expected-version-invalid'
    }
    if ($null -ne $bundleItem) {
        if ([string]$bundleItem.Name -cne $expectedBundleName -or [int64]$bundleItem.Length -le 0) {
            Add-PromotionReason 'bundle-name-or-size-mismatch'
        }
        try {
            $bundleSha256 = Get-Sha256Hex -Path $bundleItem.FullName
        } catch {
            Add-PromotionReason 'bundle-unreadable'
        }
        if (-not (Test-FileWrittenAfter -Item $bundleItem -MinimumTimestamp $summaryGeneratedAt -NowUtc $nowUtc -Hours $FreshnessHours)) {
            Add-PromotionReason 'bundle-stale'
        }
    }
    $checks.bundleIdentity = $script:PromotionReasons.Count -eq $bundleReasonsBefore

    $metadataReasonsBefore = $script:PromotionReasons.Count
    $latestItem = Test-RegularInputFile -Path $LatestJsonPath -ReasonPrefix 'latest-json' -MaxBytes 2MB
    $latest = if ($null -ne $latestItem) { Read-JsonFixture -Path $latestItem.FullName -ReasonPrefix 'latest-json' } else { $null }
    $sumsItem = Test-RegularInputFile -Path $Sha256SumsPath -ReasonPrefix 'sha256sums' -MaxBytes 1MB
    $sumsText = if ($null -ne $sumsItem) { Read-StrictUtf8File -Path $sumsItem.FullName -ReasonPrefix 'sha256sums' -MaxBytes 1MB } else { $null }
    if ($null -ne $latest) {
        $latestPublishedAt = ConvertTo-StrictUtcTimestamp $latest.pub_date
        if (
            -not (Test-FreshTimestamp -Timestamp $latestPublishedAt -NowUtc $nowUtc -Hours $FreshnessHours) -or
            $null -eq $summaryGeneratedAt -or
            $latestPublishedAt -lt $summaryGeneratedAt -or
            ($null -ne $buildGeneratedAt -and $latestPublishedAt -gt $buildGeneratedAt.AddMinutes(5))
        ) {
            Add-PromotionReason 'latest-json-stale-or-out-of-order'
        }
        if ([string]$latest.version -cne [string]$ExpectedVersion) {
            Add-PromotionReason 'latest-json-version-mismatch'
        }
        $latestArtifacts = @($latest.artifacts)
        if ($latestArtifacts.Count -ne 1) {
            Add-PromotionReason 'latest-json-artifact-count-invalid'
        } else {
            $latestArtifact = $latestArtifacts[0]
            if (
                [string]$latestArtifact.name -cne $expectedBundleName -or
                [string]$latestArtifact.sha256 -cne [string]$bundleSha256 -or
                [int64]$latestArtifact.sizeBytes -ne [int64]$bundleItem.Length -or
                [string]::IsNullOrWhiteSpace([string]$latestArtifact.url)
            ) {
                Add-PromotionReason 'latest-json-bundle-identity-mismatch'
            }
            $platformEntry = $latest.platforms.'windows-x86_64'
            if (
                $null -eq $platformEntry -or
                [string]$platformEntry.url -cne [string]$latestArtifact.url -or
                [string]$platformEntry.signature -cne [string]$latestArtifact.signature -or
                (
                    [string]$latestArtifact.url -cne $expectedBundleName -and
                    -not ([string]$latestArtifact.url).EndsWith("/$expectedBundleName", [System.StringComparison]::Ordinal)
                )
            ) {
                Add-PromotionReason 'latest-json-platform-identity-mismatch'
            }
        }
        if (-not (Test-FileWrittenAfter -Item $latestItem -MinimumTimestamp $summaryGeneratedAt -NowUtc $nowUtc -Hours $FreshnessHours)) {
            Add-PromotionReason 'latest-json-file-stale'
        }
    }
    if ($null -ne $sumsText) {
        $expectedSumsText = "$bundleSha256  $expectedBundleName`n"
        $normalizedSumsText = $sumsText.Replace("`r`n", "`n")
        if ($normalizedSumsText -cne $expectedSumsText) {
            Add-PromotionReason 'sha256sums-bundle-identity-mismatch'
        }
        if (-not (Test-FileWrittenAfter -Item $sumsItem -MinimumTimestamp $summaryGeneratedAt -NowUtc $nowUtc -Hours $FreshnessHours)) {
            Add-PromotionReason 'sha256sums-file-stale'
        }
    }
    $checks.releaseMetadata = $script:PromotionReasons.Count -eq $metadataReasonsBefore

    if ($null -ne $bundleItem -and $null -ne $bundleSha256) {
        $bundleIdentity = [ordered]@{
            name = [string]$bundleItem.Name
            length = [int64]$bundleItem.Length
            sha256 = [string]$bundleSha256
        }
    }
}

$eligible = -not $seedMiss -and $observedExactHit -and $script:PromotionReasons.Count -eq 0
$reason = if ($seedMiss) { 'seed-miss' } elseif ($eligible) { 'eligible' } else { 'invariant-failed' }
$evidence = [ordered]@{
    schemaVersion = 2
    generatedAtUtc = $nowUtc.ToString('o')
    repo = $normalizedRepo
    consumption = [ordered]@{
        mode = 'deferred-terminal-run'
        producerRunMustBeCompleted = $true
    }
    cache = [ordered]@{
        id = $(if ($cacheIdValid) { $parsedCacheId } else { $null })
        key = $normalizedExpectedCacheKey
        ref = $normalizedCacheRef
        generation = $cacheGeneration
        fingerprint = $cacheFingerprint
        manifestFingerprint = $manifestFingerprint
    }
    run = [ordered]@{
        id = $(if ($runIdValid) { $parsedRunId } else { $null })
        attempt = $(if ($runAttemptValid) { $parsedRunAttempt } else { $null })
        headSha = $normalizedHeadSha
        headBranch = $normalizedHeadBranch
    }
    reuse = [ordered]@{
        exactCacheHit = [bool]$eligible
        imported = [bool]$eligible
        seedMiss = [bool]$seedMiss
    }
    eligible = [bool]$eligible
    reason = $reason
    reasons = @($script:PromotionReasons)
    checks = $checks
    cliContract = [ordered]@{
        sha256 = [string]$checkedInCliContract.sha256
        version = [string]$checkedInCliContract.version
        versionOutput = [string]$checkedInCliContract.versionOutput
    }
    observations = [ordered]@{
        actionsCacheHit = [string]$ActionsCacheHit
        actionsCacheKey = $normalizedActionsCacheKey
        importUsable = [string]$ImportUsable
        coldProductsUsable = [string]$ColdProductsUsable
        usePrebuiltTauriApp = [string]$UsePrebuiltTauriApp
        frontendDependenciesRequired = [string]$FrontendDependenciesRequired
        cliContractSha256 = [ordered]@{
            checkedIn = [string]$checkedInCliContract.sha256
            imported = [string]$ImportCliContractSha256
            selected = [string]$SelectedCliContractSha256
        }
        cliVersionOutput = [ordered]@{
            checkedIn = [string]$checkedInCliContract.versionOutput
            imported = [string]$ImportCliVersionOutput
            selected = [string]$SelectedCliVersionOutput
        }
        stepOutcomes = [ordered]@{
            tauriExport = [string]$TauriExportOutcome
            tauriSave = [string]$TauriSaveOutcome
            frontendNodeModulesRestore = [string]$FrontendNodeModulesRestoreOutcome
            npmPackageStoreRestore = [string]$NpmPackageStoreRestoreOutcome
            frontendInstall = [string]$FrontendInstallOutcome
            frontendCachedUse = [string]$FrontendCachedUseOutcome
            frontendDependencySave = [string]$FrontendDependencySaveOutcome
            npmPackageStoreSave = [string]$NpmPackageStoreSaveOutcome
        }
    }
    bundle = $bundleIdentity
}

$resolvedOutputPath = Resolve-FullPath -Path $OutputPath
$outputParent = Split-Path -Parent $resolvedOutputPath
if ([string]::IsNullOrWhiteSpace($outputParent)) {
    throw "Tauri cache promotion evidence output must have a parent directory."
}
foreach ($inputPath in @($CacheSummaryPath, $BuildTimingPath, $LatestJsonPath, $Sha256SumsPath, $BundlePath)) {
    if (-not [string]::IsNullOrWhiteSpace($inputPath)) {
        try {
            if ((Resolve-FullPath -Path $inputPath) -ieq $resolvedOutputPath) {
                throw "Tauri cache promotion evidence output must not replace an input fixture."
            }
        } catch {
            if ($_.Exception.Message -like 'Tauri cache promotion evidence output*') {
                throw
            }
        }
    }
}
New-Item -ItemType Directory -Force -Path $outputParent | Out-Null
if (Test-Path -LiteralPath $resolvedOutputPath -PathType Container) {
    throw "Tauri cache promotion evidence output path is a directory: $resolvedOutputPath"
}
if (Test-Path -LiteralPath $resolvedOutputPath -PathType Leaf) {
    $existingOutput = Get-Item -LiteralPath $resolvedOutputPath
    if (($existingOutput.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Tauri cache promotion evidence output must not be a reparse point."
    }
}
$json = $evidence | ConvertTo-Json -Depth 12
$temporaryPath = "$resolvedOutputPath.tmp.$([Guid]::NewGuid().ToString('N'))"
$backupPath = "$resolvedOutputPath.backup.$([Guid]::NewGuid().ToString('N'))"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
try {
    [System.IO.File]::WriteAllText($temporaryPath, $json + "`n", $utf8NoBom)
    if (Test-Path -LiteralPath $resolvedOutputPath -PathType Leaf) {
        [System.IO.File]::Replace($temporaryPath, $resolvedOutputPath, $backupPath)
    } else {
        [System.IO.File]::Move($temporaryPath, $resolvedOutputPath)
    }
} finally {
    if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
    if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
        Remove-Item -LiteralPath $backupPath -Force
    }
}

$evidence | ConvertTo-Json -Depth 12 -Compress
