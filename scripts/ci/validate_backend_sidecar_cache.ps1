<#
.SYNOPSIS
Validates a restored full backend sidecar without executing its binaries.

.DESCRIPTION
Checks the self-derived cache identity and the exact runtime, application, and
media inventories. A corrupt Actions cache or durable artifact therefore falls
back to the runtime/venv build path before dependency restores are skipped.
#>

param(
    [string]$CacheRoot = "build\tauri-sidecar-cache",
    [switch]$FailIfUnusable
)

$ErrorActionPreference = "Stop"

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
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
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

function Add-ExpectedEntries {
    param(
        [hashtable]$Target,
        [object[]]$Entries,
        [string]$Prefix = ""
    )
    foreach ($entry in $Entries) {
        $relative = [string]$entry.path
        if (
            -not $relative -or
            $relative.Contains("\") -or
            $relative.StartsWith("/") -or
            $relative -match '(^|/)\.\.($|/)'
        ) { throw "Backend cache manifest contains an unsafe path." }
        $fullRelative = "$Prefix$relative"
        if ($Target.ContainsKey($fullRelative)) {
            throw "Backend cache manifests contain duplicate path '$fullRelative'."
        }
        $Target[$fullRelative] = [pscustomobject]@{
            length = [int64]$entry.length
            sha256 = [string]$entry.sha256
        }
    }
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
    throw "Backend sidecar cache root must remain under the repository build directory."
}

$usable = $false
$reason = "missing"
try {
    $entries = @(Get-ChildItem -LiteralPath $resolvedCacheRoot -Directory -Force -ErrorAction SilentlyContinue)
    if ($entries.Count -ne 1 -or $entries[0].Name -notmatch '^[0-9a-f]{64}$') {
        throw "full backend cache must contain exactly one keyed entry"
    }
    $cacheKey = $entries[0].Name
    $entryRoot = $entries[0].FullName
    $sidecarRoot = Join-Path $entryRoot "scriber-backend"
    $cacheManifestPath = Join-Path $entryRoot "cache-manifest.json"
    $runtimeManifestPath = Join-Path $sidecarRoot "runtime-layer-manifest.json"
    $appManifestPath = Join-Path $sidecarRoot "app\app-layer-manifest.json"
    $exePath = Join-Path $sidecarRoot "scriber-backend.exe"
    foreach ($path in @($cacheManifestPath, $runtimeManifestPath, $appManifestPath, $exePath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "required full backend cache file is absent"
        }
    }

    $cacheManifest = Get-Content -LiteralPath $cacheManifestPath -Raw | ConvertFrom-Json
    $runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw | ConvertFrom-Json
    $appManifest = Get-Content -LiteralPath $appManifestPath -Raw | ConvertFrom-Json
    # Full-sidecar keys intentionally use depth 8 in the builder. Keep this
    # separate from the frozen-runtime key, whose canonical depth is 10.
    $inputJson = $cacheManifest.inputManifest | ConvertTo-Json -Depth 8 -Compress
    $runtimeKey = [string]$cacheManifest.inputManifest.runtimeCacheKey
    if (
        [string]$cacheManifest.apiVersion -ne "1" -or
        [string]$cacheManifest.cacheKey -ne $cacheKey -or
        (Get-StringSha256 -Value $inputJson) -ne $cacheKey -or
        $runtimeKey -notmatch '^[0-9a-f]{64}$' -or
        [int]$runtimeManifest.schemaVersion -ne 1 -or
        [string]$runtimeManifest.name -ne "scriber-backend-runtime-layer" -or
        [string]$runtimeManifest.cacheKey -ne $runtimeKey -or
        [string]$runtimeManifest.runtimeContract.name -ne "scriber-frozen-python-runtime" -or
        [int]$runtimeManifest.runtimeContract.revision -ne 1 -or
        [int]$appManifest.schemaVersion -ne 1 -or
        [string]$appManifest.name -ne "scriber-backend-application-layer" -or
        [string]$appManifest.runtimeCacheKey -ne $runtimeKey
    ) { throw "full backend cache identities are inconsistent" }

    $expected = @{}
    Add-ExpectedEntries -Target $expected -Entries @($runtimeManifest.content.files)
    Add-ExpectedEntries -Target $expected -Entries @($appManifest.files) -Prefix "app/"
    Add-ExpectedEntries -Target $expected -Entries @($cacheManifest.mediaFiles) -Prefix "tools/ffmpeg/"
    foreach ($manifestFile in @("runtime-layer-manifest.json", "app/app-layer-manifest.json")) {
        $path = Join-Path $sidecarRoot ($manifestFile -replace '/', '\')
        $expected[$manifestFile] = [pscustomobject]@{
            length = [int64](Get-Item -LiteralPath $path).Length
            sha256 = Get-Sha256 -Path $path
        }
    }
    $actual = @{}
    $prefix = $sidecarRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    foreach ($file in Get-ChildItem -LiteralPath $sidecarRoot -Recurse -File -Force) {
        $relative = $file.FullName.Substring($prefix.Length).Replace("\", "/")
        if ($actual.ContainsKey($relative)) { throw "full backend cache has a duplicate file path" }
        $actual[$relative] = $file
    }
    if ($actual.Count -ne $expected.Count) { throw "full backend cache file inventory differs" }
    foreach ($relative in $expected.Keys) {
        $file = $actual[$relative]
        $identity = $expected[$relative]
        if (
            $null -eq $file -or
            [int64]$identity.length -ne [int64]$file.Length -or
            [string]$identity.sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$identity.sha256 -ne (Get-Sha256 -Path $file.FullName)
        ) { throw "full backend cache checksum inventory differs" }
    }

    $exe = Get-Item -LiteralPath $exePath
    $exeSha = Get-Sha256 -Path $exePath
    $hasDeno = @($cacheManifest.mediaFiles | Where-Object { [string]$_.path -eq "deno.exe" }).Count -eq 1
    $usable = (
        [int]$runtimeManifest.content.fileCount -eq @($runtimeManifest.content.files).Count -and
        [string]$runtimeManifest.content.treeSha256 -eq (Get-FileIdentityTreeSha256 -Entries @($runtimeManifest.content.files)) -and
        [string]$runtimeManifest.executable.sha256 -eq $exeSha -and
        [int64]$runtimeManifest.executable.length -eq [int64]$exe.Length -and
        [string]$cacheManifest.sidecarSha256 -eq $exeSha -and
        [int64]$cacheManifest.sidecarLength -eq [int64]$exe.Length -and
        $hasDeno
    )
    if (-not $usable) { throw "full backend cache critical attestations differ" }
    $reason = "validated"
} catch {
    $usable = $false
    $reason = "invalid"
    Write-Warning "Backend sidecar cache is not usable: $($_.Exception.Message)"
}

Write-OutputValue -Name "usable" -Value $usable.ToString().ToLowerInvariant()
Write-OutputValue -Name "reason" -Value $reason
[ordered]@{ ok = $true; usable = $usable; reason = $reason } | ConvertTo-Json -Compress
if ($FailIfUnusable -and -not $usable) {
    throw "Backend sidecar cache validation failed: $reason"
}
