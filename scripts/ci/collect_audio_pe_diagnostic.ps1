param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [ValidateSet("off", "sequential", "overlap")]
    [string]$Mode = "off",
    [string]$OutputDirectory = "release-artifacts",
    [string]$SourceCommit = "",
    [string]$Repository = "",
    [string]$RunId = "",
    [string]$Ref = "",
    [string]$RefType = "",
    [string]$RefreshRequested = "false",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

function Convert-ToFullPath {
    param(
        [string]$Root,
        [string]$Path
    )

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Root $Path))
}

function Assert-UnderRoot {
    param(
        [string]$Root,
        [string]$Path,
        [string]$Label
    )

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    if (-not $pathFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under the repository root."
    }
}

function Get-Sha256Hex {
    param([string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Convert-ManifestFingerprintToHashFilesFingerprint {
    param([string]$Fingerprint)

    $normalized = ([string]$Fingerprint).Trim().ToLowerInvariant()
    if ($normalized -notmatch '^[0-9a-f]{64}$') {
        throw "Rust dependency manifest fingerprint must be a 64-character SHA-256 digest."
    }

    $manifestDigestBytes = New-Object byte[] 32
    for ($index = 0; $index -lt $manifestDigestBytes.Length; $index++) {
        $manifestDigestBytes[$index] = [Convert]::ToByte(
            $normalized.Substring($index * 2, 2),
            16
        )
    }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashFilesDigest = $sha256.ComputeHash($manifestDigestBytes)
    } finally {
        $sha256.Dispose()
    }
    return -join ($hashFilesDigest | ForEach-Object { $_.ToString("x2") })
}

function Test-ObjectProperty {
    param(
        [object]$Object,
        [string]$Name
    )

    if ($null -eq $Object) {
        return $false
    }
    return $null -ne $Object.PSObject.Properties[$Name]
}

function Assert-ObjectProperties {
    param(
        [object]$Object,
        [string[]]$Names,
        [string]$Label
    )

    if ($null -eq $Object) {
        throw "$Label is missing."
    }
    foreach ($name in $Names) {
        if (-not (Test-ObjectProperty -Object $Object -Name $name)) {
            throw "$Label is missing required property '$name'."
        }
    }
}

function Assert-BooleanValue {
    param(
        [object]$Value,
        [string]$Label
    )

    if ($Value -isnot [bool]) {
        throw "$Label must be a JSON boolean."
    }
}

function Assert-IntegerValue {
    param(
        [object]$Value,
        [string]$Label
    )

    $integerTypes = @(
        [byte], [sbyte], [int16], [uint16], [int32], [uint32], [int64], [uint64]
    )
    foreach ($integerType in $integerTypes) {
        if ($Value -is $integerType) {
            return
        }
    }
    throw "$Label must be a JSON integer."
}

function Get-EnvironmentSetting {
    param([string]$Name)

    $item = Get-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
    return [ordered]@{
        present = $null -ne $item
        value = if ($null -ne $item) { [string]$item.Value } else { $null }
    }
}

function Get-RelativePathUnderRoot {
    param(
        [string]$Root,
        [string]$Path,
        [string]$Label
    )

    Assert-UnderRoot -Root $Root -Path $Path -Label $Label
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    return $pathFull.Substring($rootFull.Length).Replace("\", "/")
}

function Get-PeToolIdentity {
    param(
        [string]$Path,
        [string]$Root,
        [string]$Family,
        [string]$VersionDirectory,
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label executable is missing: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    $fileVersion = [string]$item.VersionInfo.FileVersion
    if ([string]::IsNullOrWhiteSpace($fileVersion)) {
        throw "$Label executable does not expose a file version."
    }
    return [ordered]@{
        family = $Family
        versionDirectory = $VersionDirectory
        relativePath = Get-RelativePathUnderRoot -Root $Root -Path $item.FullName -Label $Label
        sha256 = Get-Sha256Hex -Path $item.FullName
        length = [int64]$item.Length
        fileVersion = $fileVersion.Trim()
    }
}

function Resolve-MsvcLinkerIdentity {
    $programFilesX86 = [string]${env:ProgramFiles(x86)}
    if ([string]::IsNullOrWhiteSpace($programFilesX86)) {
        throw "ProgramFiles(x86) is unavailable while resolving MSVC link.exe."
    }
    $vswhere = Join-Path $programFilesX86 "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        throw "vswhere.exe is unavailable while resolving MSVC link.exe."
    }
    $installationLines = @(& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "vswhere.exe failed while resolving MSVC link.exe."
    }
    $installations = @($installationLines | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($installations.Count -ne 1) {
        throw "Expected exactly one latest Visual Studio installation with the x64 C++ toolchain."
    }
    $installationPath = [System.IO.Path]::GetFullPath($installations[0].Trim())
    $versionFile = Join-Path $installationPath "VC\Auxiliary\Build\Microsoft.VCToolsVersion.default.txt"
    if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
        throw "The latest Visual Studio installation has no default MSVC tools version file."
    }
    $toolsVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
    if ($toolsVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "The default MSVC tools version is invalid: '$toolsVersion'."
    }
    $linkPath = Join-Path $installationPath "VC\Tools\MSVC\$toolsVersion\bin\Hostx64\x64\link.exe"
    return Get-PeToolIdentity `
        -Path $linkPath `
        -Root $installationPath `
        -Family "msvc-link" `
        -VersionDirectory $toolsVersion `
        -Label "MSVC link.exe"
}

function Resolve-WindowsResourceCompilerIdentity {
    $programFilesX86 = [string]${env:ProgramFiles(x86)}
    if ([string]::IsNullOrWhiteSpace($programFilesX86)) {
        throw "ProgramFiles(x86) is unavailable while resolving Windows SDK rc.exe."
    }
    $kitsRoot = Join-Path $programFilesX86 "Windows Kits\10"
    $kitsBinRoot = Join-Path $kitsRoot "bin"
    if (-not (Test-Path -LiteralPath $kitsBinRoot -PathType Container)) {
        throw "Windows Kits 10 bin directory is unavailable while resolving rc.exe."
    }
    $candidates = @()
    foreach ($directory in @(Get-ChildItem -LiteralPath $kitsBinRoot -Directory)) {
        try {
            $parsedVersion = [version]$directory.Name
        } catch {
            continue
        }
        $candidatePath = Join-Path $directory.FullName "x64\rc.exe"
        if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
            $candidates += [pscustomobject]@{
                version = $parsedVersion
                versionText = $directory.Name
                path = $candidatePath
            }
        }
    }
    $selectedCandidates = @($candidates | Sort-Object -Property version -Descending | Select-Object -First 1)
    if ($selectedCandidates.Count -ne 1) {
        throw "No versioned x64 Windows SDK rc.exe was found."
    }
    $selected = $selectedCandidates[0]
    return Get-PeToolIdentity `
        -Path $selected.path `
        -Root $kitsRoot `
        -Family "windows-sdk-rc" `
        -VersionDirectory $selected.versionText `
        -Label "Windows SDK rc.exe"
}

function Get-UniqueCacheSummaryRow {
    param(
        [object[]]$Rows,
        [string]$Name
    )

    $matches = @($Rows | Where-Object { (Test-ObjectProperty -Object $_ -Name "Name") -and [string]$_.Name -ceq $Name })
    if ($matches.Count -ne 1) {
        throw "Release cache summary requires exactly one '$Name' row."
    }
    Assert-ObjectProperties -Object $matches[0] -Names @("Name", "Actions", "ReleaseArtifact", "Effective") -Label "Release cache summary '$Name' row"
    return $matches[0]
}

if ($Mode -eq "off") {
    [pscustomobject]@{
        ok = $true
        mode = $Mode
        diagnosticOnly = $true
        collected = $false
    } | ConvertTo-Json -Compress
    return
}

if ($RefType -ne "branch" -or -not $Ref.StartsWith("refs/heads/", [System.StringComparison]::Ordinal)) {
    throw "Rust Audio PE diagnostics are allowed only on a branch workflow_dispatch run."
}
if ($Ref -eq "refs/heads/main") {
    throw "Rust Audio PE diagnostics are forbidden on main."
}
if ($RefreshRequested -ne "false") {
    throw "Rust Audio PE diagnostics require refresh_release_cache_artifacts=false."
}
if ($SourceCommit -notmatch '^[0-9a-f]{40}$') {
    throw "Rust Audio PE diagnostics require a canonical lowercase source commit."
}
if ($RunId -notmatch '^[1-9][0-9]*$') {
    throw "Rust Audio PE diagnostics require a positive GitHub run id."
}
if ([string]::IsNullOrWhiteSpace($Repository)) {
    throw "Rust Audio PE diagnostics require the repository identity."
}
if (Test-Path -LiteralPath "Env:TAURI_CONFIG") {
    throw "Rust Audio PE diagnostics require TAURI_CONFIG to be absent before Cargo starts."
}

if ($ValidateOnly) {
    [pscustomobject]@{
        ok = $true
        mode = $Mode
        diagnosticOnly = $true
        collected = $false
        sourceCommit = $SourceCommit
        ref = $Ref
        runId = [int64]$RunId
    } | ConvertTo-Json -Compress
    return
}

$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$outputRoot = Convert-ToFullPath -Root $RepoRoot -Path $OutputDirectory
Assert-UnderRoot -Root $RepoRoot -Path $outputRoot -Label "Audio PE diagnostic output"
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null

$targetRelease = Join-Path $RepoRoot "Frontend\src-tauri\target\release"
$audioExe = Join-Path $targetRelease "scriber-audio-sidecar.exe"
$buildTimingPath = Join-Path $targetRelease "release-metadata\build-timing.json"
$audioBuildMetadataPath = Join-Path $RepoRoot "build\rust-audio-sidecar-cache\audio-sidecar-build-metadata.json"
$rustDependencyKeyPath = Join-Path $RepoRoot "build\cache-keys\rust-dependencies.txt"
$cacheSummaryPath = Join-Path $RepoRoot "build\release-cache-summary.json"
foreach ($required in @($audioExe, $buildTimingPath, $audioBuildMetadataPath, $rustDependencyKeyPath, $cacheSummaryPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Rust Audio PE diagnostic input is missing: $required"
    }
}

$timing = Get-Content -LiteralPath $buildTimingPath -Raw | ConvertFrom-Json
Assert-ObjectProperties -Object $timing -Names @("sidecar") -Label "build-timing.json"
Assert-ObjectProperties `
    -Object $timing.sidecar `
    -Names @("rustAudioSidecarCopied", "rustAudioParallelSetup", "phases") `
    -Label "build-timing.json sidecar"
$audio = $timing.sidecar.rustAudioSidecarCopied
$setup = $timing.sidecar.rustAudioParallelSetup
Assert-ObjectProperties `
    -Object $audio `
    -Names @("cacheHit", "cacheKey", "isolatedCargoTarget", "sha256", "length") `
    -Label "build-timing.json Rust Audio identity"
Assert-ObjectProperties `
    -Object $setup `
    -Names @(
        "sharedCargoTarget",
        "backendResourcePlaceholderCreated",
        "backendResourcePlaceholderInitialFileCount",
        "childTauriConfigPresent",
        "backendStagedOutsideResourcePath",
        "backendPromotedAfterAudio"
    ) `
    -Label "build-timing.json Rust Audio parallel setup"

$audioPhases = @($timing.sidecar.phases | Where-Object { (Test-ObjectProperty -Object $_ -Name "label") -and $_.label -eq "rust-audio-sidecar-build" })
if ($audioPhases.Count -ne 1) {
    throw "Rust Audio PE diagnostic requires exactly one audio build phase."
}
$audioPhase = $audioPhases[0]
Assert-ObjectProperties `
    -Object $audioPhase `
    -Names @("label", "durationMs", "ok") `
    -Label "build-timing.json Rust Audio phase"

Assert-BooleanValue -Value $audio.cacheHit -Label "Rust Audio cacheHit"
Assert-BooleanValue -Value $audio.isolatedCargoTarget -Label "Rust Audio isolatedCargoTarget"
Assert-IntegerValue -Value $audio.length -Label "Rust Audio length"
Assert-IntegerValue -Value $audioPhase.durationMs -Label "Rust Audio phase durationMs"
Assert-BooleanValue -Value $audioPhase.ok -Label "Rust Audio phase ok"
foreach ($booleanSetupProperty in @(
    "sharedCargoTarget",
    "backendResourcePlaceholderCreated",
    "backendStagedOutsideResourcePath",
    "backendPromotedAfterAudio"
)) {
    Assert-BooleanValue -Value $setup.$booleanSetupProperty -Label "Rust Audio setup $booleanSetupProperty"
}
if ($null -ne $setup.backendResourcePlaceholderInitialFileCount) {
    Assert-IntegerValue `
        -Value $setup.backendResourcePlaceholderInitialFileCount `
        -Label "Rust Audio setup backendResourcePlaceholderInitialFileCount"
}
if ($null -ne $setup.childTauriConfigPresent) {
    Assert-BooleanValue -Value $setup.childTauriConfigPresent -Label "Rust Audio setup childTauriConfigPresent"
}

if ($audio.cacheHit) {
    throw "Rust Audio PE diagnostic requires a real internal Audio cache miss."
}
if ([string]$audio.cacheKey -notmatch '^[0-9a-f]{64}$') {
    throw "Rust Audio PE diagnostic received an invalid internal Audio cache key."
}
if ([string]$audio.sha256 -notmatch '^[0-9a-f]{64}$') {
    throw "Rust Audio PE diagnostic received an invalid Audio executable SHA-256."
}
if ($audio.isolatedCargoTarget) {
    throw "Rust Audio PE diagnostic requires the shared Tauri Cargo target."
}
if ([int64]$audio.length -le 0) {
    throw "Rust Audio PE diagnostic received an invalid Audio executable length."
}
if (-not $audioPhase.ok -or [int64]$audioPhase.durationMs -lt 0) {
    throw "Rust Audio PE diagnostic received an unsuccessful or invalid Audio build phase."
}

$actualLength = [int64](Get-Item -LiteralPath $audioExe).Length
$actualSha256 = Get-Sha256Hex -Path $audioExe
if ([int64]$audio.length -ne $actualLength -or [string]$audio.sha256 -cne $actualSha256) {
    throw "Rust Audio PE diagnostic executable identity does not match build-timing.json."
}

$phaseParallelPropertyPresent = Test-ObjectProperty -Object $audioPhase -Name "parallel"
$phaseSharedTargetPropertyPresent = Test-ObjectProperty -Object $audioPhase -Name "sharedCargoTarget"
$phaseSchema = $null
$phaseParallel = $false
$phaseSharedCargoTarget = $false
if ($Mode -eq "overlap") {
    if (-not $phaseParallelPropertyPresent -or -not $phaseSharedTargetPropertyPresent) {
        throw "Rust Audio PE overlap diagnostic requires explicit parallel and sharedCargoTarget phase properties."
    }
    Assert-BooleanValue -Value $audioPhase.parallel -Label "Rust Audio overlap phase parallel"
    Assert-BooleanValue -Value $audioPhase.sharedCargoTarget -Label "Rust Audio overlap phase sharedCargoTarget"
    $phaseSchema = "explicit-overlap"
    $phaseParallel = [bool]$audioPhase.parallel
    $phaseSharedCargoTarget = [bool]$audioPhase.sharedCargoTarget
    $overlapSetupMatches = (
        $phaseParallel -and
        $phaseSharedCargoTarget -and
        $setup.sharedCargoTarget -and
        $setup.backendResourcePlaceholderCreated -and
        [int64]$setup.backendResourcePlaceholderInitialFileCount -eq 0 -and
        $setup.childTauriConfigPresent -eq $false -and
        $setup.backendStagedOutsideResourcePath -and
        $setup.backendPromotedAfterAudio
    )
    if (-not $overlapSetupMatches) {
        throw "Rust Audio PE overlap diagnostic did not activate the complete race-free setup."
    }
} else {
    if ($phaseParallelPropertyPresent -ne $phaseSharedTargetPropertyPresent) {
        throw "Rust Audio PE sequential phase must expose both optional parallel properties or neither."
    }
    if ($phaseParallelPropertyPresent) {
        Assert-BooleanValue -Value $audioPhase.parallel -Label "Rust Audio sequential phase parallel"
        Assert-BooleanValue -Value $audioPhase.sharedCargoTarget -Label "Rust Audio sequential phase sharedCargoTarget"
        if ($audioPhase.parallel -or $audioPhase.sharedCargoTarget) {
            throw "Rust Audio PE sequential phase explicitly reports parallel execution."
        }
        $phaseSchema = "explicit-sequential"
        $phaseParallel = [bool]$audioPhase.parallel
        $phaseSharedCargoTarget = [bool]$audioPhase.sharedCargoTarget
    } else {
        # Invoke-TimedStep emits only the core phase schema for the sequential
        # arm. Treat that exact two-property absence as an attested schema
        # variant instead of silently coercing missing JSON values to false.
        $phaseSchema = "sequential-core-without-parallel-properties"
    }
    $sequentialSetupMatches = (
        -not $phaseParallel -and
        -not $phaseSharedCargoTarget -and
        -not $setup.sharedCargoTarget -and
        -not $setup.backendResourcePlaceholderCreated -and
        $null -eq $setup.backendResourcePlaceholderInitialFileCount -and
        $null -eq $setup.childTauriConfigPresent -and
        -not $setup.backendStagedOutsideResourcePath -and
        -not $setup.backendPromotedAfterAudio
    )
    if (-not $sequentialSetupMatches) {
        throw "Rust Audio PE sequential diagnostic unexpectedly activated overlap setup."
    }
}

$audioBuildMetadata = Get-Content -LiteralPath $audioBuildMetadataPath -Raw | ConvertFrom-Json
Assert-ObjectProperties `
    -Object $audioBuildMetadata `
    -Names @(
        "apiVersion",
        "cacheHit",
        "cacheKey",
        "targetExe",
        "cargoTargetDir",
        "isolatedCargoTarget",
        "sha256",
        "length"
    ) `
    -Label "Rust Audio build metadata"
Assert-BooleanValue -Value $audioBuildMetadata.cacheHit -Label "Rust Audio build metadata cacheHit"
Assert-BooleanValue -Value $audioBuildMetadata.isolatedCargoTarget -Label "Rust Audio build metadata isolatedCargoTarget"
Assert-IntegerValue -Value $audioBuildMetadata.length -Label "Rust Audio build metadata length"
if (
    [string]$audioBuildMetadata.apiVersion -cne "1" -or
    $audioBuildMetadata.cacheHit -or
    $audioBuildMetadata.isolatedCargoTarget -or
    [string]$audioBuildMetadata.cacheKey -cne [string]$audio.cacheKey -or
    [string]$audioBuildMetadata.sha256 -cne $actualSha256 -or
    [int64]$audioBuildMetadata.length -ne $actualLength
) {
    throw "Rust Audio build metadata does not describe the same real shared-target cache miss."
}
$metadataTargetExe = Convert-ToFullPath -Root $RepoRoot -Path ([string]$audioBuildMetadata.targetExe)
if ($metadataTargetExe -ne [System.IO.Path]::GetFullPath($audioExe)) {
    throw "Rust Audio build metadata points at a different target executable."
}
$metadataCargoTarget = Convert-ToFullPath -Root $RepoRoot -Path ([string]$audioBuildMetadata.cargoTargetDir)
$cargoTargetRelativePath = Get-RelativePathUnderRoot `
    -Root $RepoRoot `
    -Path $metadataCargoTarget `
    -Label "Rust Audio Cargo target"
if ($cargoTargetRelativePath -ine "Frontend/src-tauri/target") {
    throw "Rust Audio PE diagnostic requires the canonical shared Frontend/src-tauri/target Cargo target."
}

$cacheSummary = Get-Content -LiteralPath $cacheSummaryPath -Raw | ConvertFrom-Json
Assert-ObjectProperties `
    -Object $cacheSummary `
    -Names @(
        "schemaVersion",
        "runIdentity",
        "cacheKeyParity",
        "refreshReleaseCacheArtifacts",
        "saveActionsCaches",
        "publishReleaseCacheArtifacts",
        "rows",
        "rustBuildReleaseArtifact",
        "backendSidecarPrebuilt",
        "backendRuntimeValidated",
        "coldProductsUsed",
        "tauriAppBinary"
    ) `
    -Label "release-cache-summary.json"
Assert-IntegerValue -Value $cacheSummary.schemaVersion -Label "release-cache-summary.json schemaVersion"
if ([int64]$cacheSummary.schemaVersion -ne 2) {
    throw "Rust Audio PE diagnostic requires release-cache-summary.json schemaVersion 2."
}
Assert-ObjectProperties `
    -Object $cacheSummary.runIdentity `
    -Names @("repository", "runId", "headSha", "ref", "eventName") `
    -Label "release cache run identity"
Assert-IntegerValue -Value $cacheSummary.runIdentity.runId -Label "release cache run id"
if (
    [string]$cacheSummary.runIdentity.repository -cne $Repository -or
    [int64]$cacheSummary.runIdentity.runId -ne [int64]$RunId -or
    [string]$cacheSummary.runIdentity.headSha -cne $SourceCommit -or
    [string]$cacheSummary.runIdentity.ref -cne $Ref -or
    [string]$cacheSummary.runIdentity.eventName -cne "workflow_dispatch"
) {
    throw "Release cache summary run identity does not match this diagnostic run."
}

Assert-ObjectProperties `
    -Object $cacheSummary.cacheKeyParity `
    -Names @("apiVersion", "planner", "packager", "componentMatches", "allMatch") `
    -Label "release cache key parity"
Assert-ObjectProperties `
    -Object $cacheSummary.cacheKeyParity.planner `
    -Names @("backendSidecar", "backendRuntime", "tauriAppBinary") `
    -Label "release cache planner fingerprints"
Assert-ObjectProperties `
    -Object $cacheSummary.cacheKeyParity.packager `
    -Names @("backendSidecar", "backendRuntime", "tauriAppBinary") `
    -Label "release cache packager fingerprints"
Assert-ObjectProperties `
    -Object $cacheSummary.cacheKeyParity.componentMatches `
    -Names @("backendSidecar", "backendRuntime", "tauriAppBinary") `
    -Label "release cache component parity"
Assert-BooleanValue -Value $cacheSummary.cacheKeyParity.allMatch -Label "release cache allMatch"
foreach ($componentName in @("backendSidecar", "backendRuntime", "tauriAppBinary")) {
    $plannerFingerprint = [string]$cacheSummary.cacheKeyParity.planner.$componentName
    $packagerFingerprint = [string]$cacheSummary.cacheKeyParity.packager.$componentName
    Assert-BooleanValue `
        -Value $cacheSummary.cacheKeyParity.componentMatches.$componentName `
        -Label "release cache $componentName component match"
    if (
        $plannerFingerprint -notmatch '^[0-9a-f]{64}$' -or
        $packagerFingerprint -notmatch '^[0-9a-f]{64}$' -or
        $plannerFingerprint -cne $packagerFingerprint -or
        -not $cacheSummary.cacheKeyParity.componentMatches.$componentName
    ) {
        throw "Release cache $componentName fingerprints do not have exact planner/packager parity."
    }
}
if (-not $cacheSummary.cacheKeyParity.allMatch) {
    throw "Release cache summary reports incomplete fingerprint parity."
}

$summaryRows = @($cacheSummary.rows)
$rustBuildRow = Get-UniqueCacheSummaryRow -Rows $summaryRows -Name "Rust build"
$tauriAppRow = Get-UniqueCacheSummaryRow -Rows $summaryRows -Name "Tauri app binary"
$backendSidecarRow = Get-UniqueCacheSummaryRow -Rows $summaryRows -Name "Backend sidecar"
$backendRuntimeRow = Get-UniqueCacheSummaryRow -Rows $summaryRows -Name "Backend runtime"
$audioSidecarRow = Get-UniqueCacheSummaryRow -Rows $summaryRows -Name "Rust audio sidecar"
if (
    [string]$audioSidecarRow.Actions -cne "miss" -or
    [string]$audioSidecarRow.ReleaseArtifact -cne "false" -or
    [string]$audioSidecarRow.Effective -cne "miss"
) {
    throw "Rust Audio PE diagnostic requires an Actions miss with no restored Audio release artifact."
}

$rustDependencyManifestFingerprint = Get-Sha256Hex -Path $rustDependencyKeyPath
if ($rustDependencyManifestFingerprint -notmatch '^[0-9a-f]{64}$') {
    throw "Rust dependency fingerprint is invalid."
}
$rustDependencyHashFilesFingerprint = Convert-ManifestFingerprintToHashFilesFingerprint `
    -Fingerprint $rustDependencyManifestFingerprint
$expectedRustActionsKey = "scriber-rust-dependencies-v1-Windows-$rustDependencyHashFilesFingerprint"
Assert-ObjectProperties `
    -Object $cacheSummary.rustBuildReleaseArtifact `
    -Names @("actionsMatchedKey", "exact", "restored", "imported", "asset") `
    -Label "Rust build restore context"
$matchedRustActionsKey = [string]$cacheSummary.rustBuildReleaseArtifact.actionsMatchedKey
if (
    [string]$rustBuildRow.Actions -cne "exact" -or
    [string]$rustBuildRow.Effective -cne "actions-cache-exact" -or
    $matchedRustActionsKey -cne $expectedRustActionsKey
) {
    throw "Rust Audio PE diagnostic requires the exact expected Rust dependency Actions cache."
}

Assert-ObjectProperties `
    -Object $cacheSummary.tauriAppBinary `
    -Names @("actionsCacheExact", "importUsable", "importedSha256") `
    -Label "Tauri app binary restore context"
Assert-BooleanValue -Value $cacheSummary.tauriAppBinary.actionsCacheExact -Label "Tauri app binary actionsCacheExact"
Assert-BooleanValue -Value $cacheSummary.tauriAppBinary.importUsable -Label "Tauri app binary importUsable"
$tauriImportedSha256 = [string]$cacheSummary.tauriAppBinary.importedSha256
if (
    -not $cacheSummary.tauriAppBinary.actionsCacheExact -or
    -not $cacheSummary.tauriAppBinary.importUsable -or
    $tauriImportedSha256 -notmatch '^[0-9a-f]{64}$' -or
    [string]$tauriAppRow.Actions -cne "exact" -or
    [string]$tauriAppRow.Effective -cne "actions-cache-exact-validated"
) {
    throw "Rust Audio PE diagnostic requires an exact validated Tauri app binary import."
}
$tauriManifestFingerprint = [string]$cacheSummary.cacheKeyParity.packager.tauriAppBinary
$tauriHashFilesFingerprint = Convert-ManifestFingerprintToHashFilesFingerprint `
    -Fingerprint $tauriManifestFingerprint
$expectedTauriActionsKey = "scriber-tauri-app-binary-v3-Windows-$tauriHashFilesFingerprint"

Assert-BooleanValue -Value $cacheSummary.backendSidecarPrebuilt -Label "backendSidecarPrebuilt"
Assert-BooleanValue -Value $cacheSummary.coldProductsUsed -Label "coldProductsUsed"
$backendRuntimeValidated = [string]$cacheSummary.backendRuntimeValidated
if ($backendRuntimeValidated -notin @("true", "false", "empty")) {
    throw "Release cache summary contains an invalid backendRuntimeValidated state."
}
if ($cacheSummary.coldProductsUsed) {
    throw "Rust Audio PE diagnostics forbid the combined cold-products artifact."
}
if (
    -not $cacheSummary.backendSidecarPrebuilt -and
    $backendRuntimeValidated -cne "true"
) {
    throw "Rust Audio PE diagnostic requires a validated restored backend product."
}

$runnerOS = [string]$env:RUNNER_OS
$runnerArch = [string]$env:RUNNER_ARCH
$imageOS = [string]$env:ImageOS
$imageVersion = [string]$env:ImageVersion
if ($runnerOS -cne "Windows" -or $runnerArch -cne "X64") {
    throw "Rust Audio PE diagnostics require the Windows X64 GitHub runner."
}
if ([string]::IsNullOrWhiteSpace($imageOS) -or [string]::IsNullOrWhiteSpace($imageVersion)) {
    throw "Rust Audio PE diagnostics require ImageOS and ImageVersion runner identities."
}
$runnerContext = [ordered]@{
    runnerOS = $runnerOS
    runnerArch = $runnerArch
    imageOS = $imageOS
    imageVersion = $imageVersion
}

$environmentContext = [ordered]@{
    RUSTFLAGS = Get-EnvironmentSetting -Name "RUSTFLAGS"
    CARGO_ENCODED_RUSTFLAGS = Get-EnvironmentSetting -Name "CARGO_ENCODED_RUSTFLAGS"
    CARGO_INCREMENTAL = Get-EnvironmentSetting -Name "CARGO_INCREMENTAL"
    CARGO_TARGET_DIR = Get-EnvironmentSetting -Name "CARGO_TARGET_DIR"
    tauriConfigPresent = Test-Path -LiteralPath "Env:TAURI_CONFIG"
}
if (
    -not $environmentContext.CARGO_INCREMENTAL.present -or
    [string]$environmentContext.CARGO_INCREMENTAL.value -cne "1"
) {
    throw "Rust Audio PE diagnostics require CARGO_INCREMENTAL=1."
}
if ($environmentContext.tauriConfigPresent) {
    throw "Rust Audio PE diagnostic collection observed TAURI_CONFIG after the build."
}

$cacheAndPublicationFlagNames = @(
    "SCRIBER_SAVE_ACTIONS_CACHES",
    "SCRIBER_SAVE_REF_LOCAL_TAURI_CACHE",
    "SCRIBER_SAVE_REF_LOCAL_DESKTOP_INCREMENTAL_CACHE",
    "SCRIBER_PUBLISH_RELEASE_CACHE_ARTIFACTS",
    "SCRIBER_PUBLISH_FINISHED_COMPONENT_CACHE_ARTIFACTS",
    "SCRIBER_REFRESH_RELEASE_CACHE_ARTIFACTS"
)
$cacheAndPublicationEnvironment = [ordered]@{}
foreach ($flagName in $cacheAndPublicationFlagNames) {
    $flagSetting = Get-EnvironmentSetting -Name $flagName
    if (-not $flagSetting.present -or [string]$flagSetting.value -cne "false") {
        throw "Rust Audio PE diagnostics require $flagName=false."
    }
    $cacheAndPublicationEnvironment[$flagName] = [string]$flagSetting.value
}
if (
    [string]$cacheSummary.refreshReleaseCacheArtifacts -cne $cacheAndPublicationEnvironment.SCRIBER_REFRESH_RELEASE_CACHE_ARTIFACTS -or
    [string]$cacheSummary.saveActionsCaches -cne $cacheAndPublicationEnvironment.SCRIBER_SAVE_ACTIONS_CACHES -or
    [string]$cacheSummary.publishReleaseCacheArtifacts -cne $cacheAndPublicationEnvironment.SCRIBER_PUBLISH_RELEASE_CACHE_ARTIFACTS
) {
    throw "Release cache summary disagrees with the cache/publication environment safeguards."
}

$cacheContext = [ordered]@{
    rustDependencies = [ordered]@{
        manifestFingerprint = $rustDependencyManifestFingerprint
        hashFilesFingerprint = $rustDependencyHashFilesFingerprint
        expectedActionsKey = $expectedRustActionsKey
        matchedActionsKey = $matchedRustActionsKey
        actionsCacheExact = $true
        effectiveRestoreSource = [string]$rustBuildRow.Effective
        releaseArtifactExact = [string]$cacheSummary.rustBuildReleaseArtifact.exact
        releaseArtifactRestored = [string]$cacheSummary.rustBuildReleaseArtifact.restored
        releaseArtifactImported = [string]$cacheSummary.rustBuildReleaseArtifact.imported
    }
    tauriAppBinary = [ordered]@{
        manifestFingerprint = $tauriManifestFingerprint
        hashFilesFingerprint = $tauriHashFilesFingerprint
        expectedActionsKey = $expectedTauriActionsKey
        actionsCacheExact = [bool]$cacheSummary.tauriAppBinary.actionsCacheExact
        importUsable = [bool]$cacheSummary.tauriAppBinary.importUsable
        importedSha256 = $tauriImportedSha256
        effectiveRestoreSource = [string]$tauriAppRow.Effective
    }
    audioSidecar = [ordered]@{
        actions = [string]$audioSidecarRow.Actions
        releaseArtifact = [string]$audioSidecarRow.ReleaseArtifact
        effectiveRestoreSource = [string]$audioSidecarRow.Effective
        internalCacheHit = [bool]$audio.cacheHit
        internalCacheKey = [string]$audio.cacheKey
    }
    backendProducts = [ordered]@{
        sidecar = [ordered]@{
            fingerprint = [string]$cacheSummary.cacheKeyParity.packager.backendSidecar
            actions = [string]$backendSidecarRow.Actions
            releaseArtifact = [string]$backendSidecarRow.ReleaseArtifact
            effectiveRestoreSource = [string]$backendSidecarRow.Effective
        }
        runtime = [ordered]@{
            fingerprint = [string]$cacheSummary.cacheKeyParity.packager.backendRuntime
            actions = [string]$backendRuntimeRow.Actions
            releaseArtifact = [string]$backendRuntimeRow.ReleaseArtifact
            effectiveRestoreSource = [string]$backendRuntimeRow.Effective
            validated = $backendRuntimeValidated
        }
        sidecarPrebuilt = [bool]$cacheSummary.backendSidecarPrebuilt
        coldProductsUsed = [bool]$cacheSummary.coldProductsUsed
    }
}

$cargoTargetContext = [ordered]@{
    relativePath = "Frontend/src-tauri/target"
    metadataApiVersion = [string]$audioBuildMetadata.apiVersion
    metadataCacheKey = [string]$audioBuildMetadata.cacheKey
    metadataCacheHit = [bool]$audioBuildMetadata.cacheHit
    isolated = [bool]$audioBuildMetadata.isolatedCargoTarget
}

$msvcLinkerIdentity = Resolve-MsvcLinkerIdentity
$resourceCompilerIdentity = Resolve-WindowsResourceCompilerIdentity

$selfTestLines = @(& $audioExe --self-test 2>&1)
$selfTestExitCode = $LASTEXITCODE
if ($selfTestExitCode -ne 0) {
    throw "Rust Audio PE diagnostic self-test failed with exit code $selfTestExitCode."
}
$selfTestText = ($selfTestLines -join [Environment]::NewLine).Trim()
try {
    $selfTest = $selfTestText | ConvertFrom-Json
} catch {
    throw "Rust Audio PE diagnostic self-test did not return JSON."
}

$rustcLines = @(& rustc -Vv 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "Rust Audio PE diagnostic could not read rustc identity."
}
$cargoLines = @(& cargo -V 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "Rust Audio PE diagnostic could not read Cargo identity."
}

$binaryName = "scriber-audio-sidecar-pe-$Mode.exe"
$binaryOutput = Join-Path $outputRoot $binaryName
$attestationOutput = Join-Path $outputRoot "scriber-audio-sidecar-pe-$Mode.json"
foreach ($output in @($binaryOutput, $attestationOutput)) {
    Assert-UnderRoot -Root $outputRoot -Path $output -Label "Audio PE diagnostic file"
    if (Test-Path -LiteralPath $output) {
        throw "Refusing to replace stale Rust Audio PE diagnostic output: $output"
    }
}
Copy-Item -LiteralPath $audioExe -Destination $binaryOutput
$copiedLength = [int64](Get-Item -LiteralPath $binaryOutput).Length
$copiedSha256 = Get-Sha256Hex -Path $binaryOutput
if ($copiedLength -ne $actualLength -or $copiedSha256 -cne $actualSha256) {
    throw "Copied Rust Audio PE diagnostic executable differs from its verified source."
}

$attestation = [ordered]@{
    schemaVersion = 1
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    diagnosticOnly = $true
    mode = $Mode
    runIdentity = [ordered]@{
        repository = $Repository
        runId = [int64]$RunId
        sourceCommit = $SourceCommit
        ref = $Ref
    }
    executable = [ordered]@{
        fileName = $binaryName
        sha256 = $copiedSha256
        length = $copiedLength
    }
    audioBuild = [ordered]@{
        cacheHit = [bool]$audio.cacheHit
        cacheKey = [string]$audio.cacheKey
        isolatedCargoTarget = [bool]$audio.isolatedCargoTarget
        durationMs = [int64]$audioPhase.durationMs
        parallel = $phaseParallel
        sharedCargoTarget = $phaseSharedCargoTarget
        phaseSchema = $phaseSchema
        phaseParallelPropertyPresent = $phaseParallelPropertyPresent
        phaseSharedCargoTargetPropertyPresent = $phaseSharedTargetPropertyPresent
    }
    overlapSetup = $setup
    runner = $runnerContext
    environment = $environmentContext
    cargoTarget = $cargoTargetContext
    cacheContext = $cacheContext
    selfTest = $selfTest
    toolchain = [ordered]@{
        rustc = ($rustcLines -join "`n").Trim()
        cargo = ($cargoLines -join "`n").Trim()
        linker = $msvcLinkerIdentity
        resourceCompiler = $resourceCompilerIdentity
    }
    safeguards = [ordered]@{
        featureBranchOnly = $true
        refreshReleaseCacheArtifacts = $false
        cacheHitForbidden = $true
        publicationForbidden = $true
        tauriConfigPresent = $false
        cacheAndPublicationEnvironment = $cacheAndPublicationEnvironment
        cacheAndPublicationFlagsAllFalse = $true
    }
}
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText(
    $attestationOutput,
    ($attestation | ConvertTo-Json -Depth 12),
    $utf8WithoutBom
)

$attestation | ConvertTo-Json -Depth 12 -Compress
