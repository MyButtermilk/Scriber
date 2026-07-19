<#
.SYNOPSIS
Validates the immutable frozen-Python backend runtime cache.

.DESCRIPTION
The check is dependency-free and safe to run before restoring a Python venv.
It verifies the exact file inventory, runtime/application separation, runtime
contract, executable identity, and both manifest identities. Invalid or absent
caches report usable=false so callers can rebuild through the normal path.
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$ExpectedWorkflowFingerprint,
    [switch]$BindIfMissing,
    [switch]$FailIfUnusable,
    [string]$CacheRoot = "build\tauri-sidecar-runtime-cache"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$contractSource = Get-Content -LiteralPath (Join-Path $repoRoot "backend_runtime\contract.py") -Raw
$contractRevisionMatch = [regex]::Match(
    $contractSource,
    '(?m)^RUNTIME_CONTRACT_REVISION\s*=\s*(\d+)\s*$'
)
if (-not $contractRevisionMatch.Success -or [int]$contractRevisionMatch.Groups[1].Value -lt 1) {
    throw "Frozen backend runtime contract revision could not be resolved."
}
$expectedRuntimeContractRevision = [int]$contractRevisionMatch.Groups[1].Value

function Write-OutputValue {
    param([string]$Name, [string]$Value)
    if ($env:GITHUB_OUTPUT) {
        "$Name=$Value" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-StringSha256 {
    param([string]$Value)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-FileIdentityTreeSha256 {
    param([object[]]$Entries)

    $byPath = [System.Collections.Generic.SortedDictionary[string, object]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($entry in @($Entries)) {
        $path = [string]$entry.path
        if (-not $path -or $path.IndexOf([char]0) -ge 0 -or $byPath.ContainsKey($path)) {
            throw "runtime file identity contains an invalid or duplicate path"
        }
        $byPath.Add($path, $entry)
    }
    $builder = [System.Text.StringBuilder]::new()
    foreach ($pair in $byPath.GetEnumerator()) {
        [void]$builder.Append($pair.Key)
        [void]$builder.Append([char]0)
        [void]$builder.Append(([int64]$pair.Value.length).ToString([System.Globalization.CultureInfo]::InvariantCulture))
        [void]$builder.Append([char]0)
        [void]$builder.Append([string]$pair.Value.sha256)
        [void]$builder.Append([char]0)
    }
    return Get-StringSha256 -Value $builder.ToString()
}

function Get-FileEntries {
    param([string]$Root)
    $prefix = $Root.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    $entries = @()
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force | Sort-Object FullName) {
        $relative = $file.FullName.Substring($prefix.Length).Replace("\", "/")
        if ($relative -eq "runtime-layer-manifest.json") {
            continue
        }
        $entries += [pscustomobject][ordered]@{
            path = $relative
            length = [int64]$file.Length
            sha256 = Get-Sha256 -Path $file.FullName
        }
    }
    return @($entries)
}

function Test-ExpectedEntries {
    param([object[]]$Expected, [object[]]$ActualEntries)
    if ($Expected.Count -ne $ActualEntries.Count) {
        $script:EntryMismatchReason = "count expected=$($Expected.Count) actual=$($ActualEntries.Count)"
        return $false
    }
    $actualByPath = @{}
    foreach ($entry in $ActualEntries) {
        $relative = [string]$entry.path
        if (-not $relative -or $actualByPath.ContainsKey($relative)) {
            $script:EntryMismatchReason = "invalid or duplicate actual path"
            return $false
        }
        $actualByPath[$relative] = $entry
    }
    $seen = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($entry in $Expected) {
        $relative = [string]$entry.path
        if (
            -not $relative -or
            -not $seen.Add($relative) -or
            $relative.Contains("\") -or
            $relative.StartsWith("/") -or
            $relative -match '(^|/)\.\.($|/)'
        ) {
            $script:EntryMismatchReason = "invalid or duplicate expected path"
            return $false
        }
        $actualEntry = $actualByPath[$relative]
        if ($null -eq $actualEntry -or [int64]$entry.length -ne [int64]$actualEntry.length -or [string]$entry.sha256 -ne [string]$actualEntry.sha256) {
            $script:EntryMismatchReason = if ($null -eq $actualEntry) {
                "missing path $relative"
            } elseif ([int64]$entry.length -ne [int64]$actualEntry.length) {
                "length $([int64]$entry.length)/$([int64]$actualEntry.length) at $relative"
            } else {
                "checksum mismatch at $relative"
            }
            return $false
        }
    }
    return $true
}

Write-OutputValue -Name "usable" -Value "false"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "build"))
$resolvedCacheRoot = if ([System.IO.Path]::IsPathRooted($CacheRoot)) {
    [System.IO.Path]::GetFullPath($CacheRoot)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $repoRoot $CacheRoot))
}
$buildPrefix = $buildRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
if (-not $resolvedCacheRoot.StartsWith($buildPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Backend runtime cache root must remain under the repository build directory."
}

$usable = $false
$reason = "missing"
try {
    $runtimeRoot = Join-Path $resolvedCacheRoot "scriber-backend"
    $cacheManifestPath = Join-Path $resolvedCacheRoot "runtime-cache-manifest.json"
    $layerManifestPath = Join-Path $runtimeRoot "runtime-layer-manifest.json"
    $workflowEnvelopePath = Join-Path $resolvedCacheRoot "workflow-cache-envelope.json"
    $stableMediaRoot = Join-Path $resolvedCacheRoot "media-tools"
    $runtimeExePath = Join-Path $runtimeRoot "scriber-backend.exe"
    if (-not (Test-Path -LiteralPath $runtimeExePath -PathType Leaf)) {
        $runtimeExePath = Join-Path $runtimeRoot "scriber-backend"
    }
    if (
        -not (Test-Path -LiteralPath $runtimeExePath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $cacheManifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $layerManifestPath -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $runtimeRoot "app"))
    ) {
        throw "required runtime cache files are absent or an application layer leaked into the runtime cache"
    }

    $cacheManifest = Get-Content -LiteralPath $cacheManifestPath -Raw | ConvertFrom-Json
    $layerManifest = Get-Content -LiteralPath $layerManifestPath -Raw | ConvertFrom-Json
    $innerCacheKey = [string]$cacheManifest.cacheKey
    if ($innerCacheKey -notmatch '^[0-9a-f]{64}$') {
        throw "runtime cache contains an invalid internal identity"
    }
    $inputManifestJson = $cacheManifest.inputManifest | ConvertTo-Json -Depth 10 -Compress
    if ($inputManifestJson -match '"path":"src[\\/]') {
        throw "runtime cache input identity contains application source"
    }
    if ((Get-StringSha256 -Value $inputManifestJson) -ne $innerCacheKey) {
        throw "runtime cache internal key does not match its canonical input manifest"
    }

    $actualFiles = @(Get-FileEntries -Root $runtimeRoot)
    $expectedFiles = @($cacheManifest.runtimeFiles)
    if (-not (Test-ExpectedEntries -Expected $expectedFiles -ActualEntries $actualFiles)) {
        throw "runtime file inventory differs ($script:EntryMismatchReason)"
    }
    if (-not (Test-ExpectedEntries -Expected @($layerManifest.content.files) -ActualEntries $actualFiles)) {
        throw "inner runtime layer inventory differs ($script:EntryMismatchReason)"
    }

    $stableMediaFiles = @()
    if (Test-Path -LiteralPath $stableMediaRoot -PathType Container) {
        $mediaPrefix = $resolvedCacheRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
        $stableMediaFiles = @(
            Get-ChildItem -LiteralPath $stableMediaRoot -Recurse -File -Force |
                Sort-Object FullName |
                ForEach-Object {
                    [pscustomobject][ordered]@{
                        path = $_.FullName.Substring($mediaPrefix.Length).Replace("\", "/")
                        length = [int64]$_.Length
                        sha256 = Get-Sha256 -Path $_.FullName
                    }
                }
        )
    }
    $requiredQuickJsFiles = @(
        "media-tools/qjs.exe",
        "media-tools/qjs-engine.exe",
        "media-tools/LICENSE.quickjs-ng.txt",
        "media-tools/js-runtime-manifest.json"
    )
    $quickJsRuntimeComplete = @(
        $requiredQuickJsFiles | Where-Object {
            $requiredPath = $_
            @($stableMediaFiles | Where-Object { [string]$_.path -eq $requiredPath }).Count -eq 1
        }
    ).Count -eq $requiredQuickJsFiles.Count
    if (
        -not (Test-ExpectedEntries -Expected @($cacheManifest.stableMediaFiles) -ActualEntries $stableMediaFiles) -or
        -not $quickJsRuntimeComplete
    ) {
        throw "stable media-tool inventory differs or the QuickJS runtime is incomplete"
    }

    $runtimeExe = Get-Item -LiteralPath $runtimeExePath
    $runtimeExeSha = Get-Sha256 -Path $runtimeExePath
    $manifestChecks = [ordered]@{
        cacheApiVersion = [int]$cacheManifest.apiVersion -eq 1
        cacheIdentity = [string]$cacheManifest.cacheKey -eq $innerCacheKey
        layerSchema = [int]$layerManifest.schemaVersion -eq 1
        layerName = [string]$layerManifest.name -eq "scriber-backend-runtime-layer"
        layerIdentity = [string]$layerManifest.cacheKey -eq $innerCacheKey
        contractName = [string]$layerManifest.runtimeContract.name -eq "scriber-frozen-python-runtime"
        contractRevision = [int]$layerManifest.runtimeContract.revision -eq $expectedRuntimeContractRevision
        fileCount = [int]$layerManifest.content.fileCount -eq $actualFiles.Count
        treeIdentity = [string]$layerManifest.content.treeSha256 -eq (Get-FileIdentityTreeSha256 -Entries $actualFiles)
        cacheExecutableHash = [string]$cacheManifest.sidecarSha256 -eq $runtimeExeSha
        cacheExecutableLength = [int64]$cacheManifest.sidecarLength -eq [int64]$runtimeExe.Length
        layerExecutableHash = [string]$layerManifest.executable.sha256 -eq $runtimeExeSha
        layerExecutableLength = [int64]$layerManifest.executable.length -eq [int64]$runtimeExe.Length
    }
    $failedManifestChecks = @(
        $manifestChecks.GetEnumerator() |
            Where-Object { -not [bool]$_.Value } |
            ForEach-Object { [string]$_.Key }
    )
    $usable = $failedManifestChecks.Count -eq 0
    if (-not $usable) {
        throw "runtime cache manifests are not self-consistent (failed=$($failedManifestChecks -join ','))"
    }

    $runtimeManifestSha = Get-Sha256 -Path $cacheManifestPath
    if (-not (Test-Path -LiteralPath $workflowEnvelopePath -PathType Leaf) -and $BindIfMissing) {
        $envelope = [ordered]@{
            apiVersion = 1
            workflowFingerprint = $ExpectedWorkflowFingerprint
            innerCacheKey = $innerCacheKey
            runtimeManifestSha256 = $runtimeManifestSha
            runtimeManifestLength = [int64](Get-Item -LiteralPath $cacheManifestPath).Length
        }
        $encoding = [System.Text.UTF8Encoding]::new($false)
        [System.IO.File]::WriteAllText(
            $workflowEnvelopePath,
            (($envelope | ConvertTo-Json -Depth 4) + "`n"),
            $encoding
        )
    }
    if (-not (Test-Path -LiteralPath $workflowEnvelopePath -PathType Leaf)) {
        throw "runtime cache has no workflow fingerprint envelope"
    }
    $workflowEnvelope = Get-Content -LiteralPath $workflowEnvelopePath -Raw | ConvertFrom-Json
    $usable = (
        [int]$workflowEnvelope.apiVersion -eq 1 -and
        [string]$workflowEnvelope.workflowFingerprint -eq $ExpectedWorkflowFingerprint -and
        [string]$workflowEnvelope.innerCacheKey -eq $innerCacheKey -and
        [string]$workflowEnvelope.runtimeManifestSha256 -eq $runtimeManifestSha -and
        [int64]$workflowEnvelope.runtimeManifestLength -eq [int64](Get-Item -LiteralPath $cacheManifestPath).Length
    )
    $reason = if ($usable) { "validated" } else { "workflow-envelope-mismatch" }
} catch {
    $usable = $false
    $reason = "invalid"
    Write-Warning "Backend runtime cache is not usable: $($_.Exception.Message)"
}

Write-OutputValue -Name "usable" -Value $usable.ToString().ToLowerInvariant()
Write-OutputValue -Name "reason" -Value $reason
[ordered]@{
    ok = $true
    usable = $usable
    reason = $reason
    workflowFingerprint = $ExpectedWorkflowFingerprint
} | ConvertTo-Json -Compress
if ($FailIfUnusable -and -not $usable) {
    throw "Backend runtime cache validation failed: $reason"
}
