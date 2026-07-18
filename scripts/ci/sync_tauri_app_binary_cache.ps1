param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Export", "Import")]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$CacheKey,
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$CacheRoot = "build\tauri-app-binary-cache",
    [string]$BinaryPath = "Frontend\src-tauri\target\release\scriber-desktop.exe",
    [string]$PackageLockPath = "Frontend\package-lock.json",
    [string]$TauriCliPackagePath = "Frontend\node_modules\@tauri-apps\cli",
    [string]$TauriCliPlatformPackagePath = "Frontend\node_modules\@tauri-apps\cli-win32-x64-msvc"
)

$ErrorActionPreference = "Stop"

function Write-GitHubOutput {
    param([string]$Name, [string]$Value)
    if ($env:GITHUB_OUTPUT) {
        "$Name=$Value" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

function Resolve-RepoPath {
    param([string]$Root, [string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Root $Path))
}

function Assert-UnderRoot {
    param([string]$Root, [string]$Path, [string]$Label)
    $prefix = $Root.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (-not $Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under ${Root}: $Path"
    }
}

function Get-NormalizedFileVersion {
    param([string]$Path)
    $versionInfo = (Get-Item -LiteralPath $Path).VersionInfo
    $candidate = [string]$versionInfo.ProductVersion
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = [string]$versionInfo.FileVersion
    }
    return $candidate.Trim()
}

function Test-ExpectedVersion {
    param([string]$Actual, [string]$Expected)
    if ([string]::IsNullOrWhiteSpace($Actual)) {
        return $false
    }
    return $Actual -eq $Expected -or $Actual -eq "$Expected.0" -or $Actual.StartsWith("$Expected+", [System.StringComparison]::Ordinal)
}

function Test-OptionalSourceCommit {
    param([string]$Value)
    return [string]::IsNullOrWhiteSpace($Value) -or $Value -match '^[0-9a-f]{40}$'
}

function Get-LockedTauriCliContract {
    param([string]$Path)
    $extractContract = @'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    packages = json.load(handle)["packages"]
cli = packages["node_modules/@tauri-apps/cli"]
platform = packages["node_modules/@tauri-apps/cli-win32-x64-msvc"]
print(json.dumps({
    "version": cli.get("version", ""),
    "packageIntegrity": cli.get("integrity", ""),
    "platformVersion": platform.get("version", ""),
    "platformPackageIntegrity": platform.get("integrity", ""),
}, separators=(",", ":")))
'@
    $contractJson = & python -c $extractContract $Path
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri CLI package lock is not valid JSON: $Path"
    }
    try {
        $contract = $contractJson | ConvertFrom-Json
    } catch {
        throw "Tauri CLI package lock extraction was not valid JSON: $Path"
    }
    if (
        [string]::IsNullOrWhiteSpace([string]$contract.version) -or
        [string]$contract.version -ne [string]$contract.platformVersion -or
        [string]$contract.packageIntegrity -notmatch '^sha512-[A-Za-z0-9+/=]+$' -or
        [string]$contract.platformPackageIntegrity -notmatch '^sha512-[A-Za-z0-9+/=]+$'
    ) {
        throw "Frontend package lock does not contain one exact Windows x64 Tauri CLI contract."
    }
    return [ordered]@{
        version = [string]$contract.version
        packageName = "@tauri-apps/cli"
        packageIntegrity = [string]$contract.packageIntegrity
        platformPackageName = "@tauri-apps/cli-win32-x64-msvc"
        platformPackageIntegrity = [string]$contract.platformPackageIntegrity
    }
}

function Get-JsonPackageIdentity {
    param([string]$Path)
    try {
        $package = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    } catch {
        throw "Tauri CLI package manifest is not valid JSON: $Path"
    }
    return [ordered]@{
        name = [string]$package.name
        version = [string]$package.version
    }
}

function Get-AttestedFileRecord {
    param([string]$Root, [string]$RelativePath)
    $path = [System.IO.Path]::GetFullPath((Join-Path $Root ($RelativePath -replace '/', '\')))
    Assert-UnderRoot -Root $Root -Path $path -Label "Tauri CLI cache file"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Attested Tauri CLI cache file is missing: $RelativePath"
    }
    $item = Get-Item -LiteralPath $path
    return [ordered]@{
        path = $RelativePath
        length = [int64]$item.Length
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

Write-GitHubOutput -Name "usable" -Value "false"

if ($CacheKey -notmatch '^[0-9a-f]{64}$') {
    throw "Tauri app binary cache key must be a lowercase SHA-256 value."
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Tauri app binary version is not valid SemVer: $Version"
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$buildRoot = Resolve-RepoPath -Root $repoRoot -Path "build"
$resolvedCacheRoot = Resolve-RepoPath -Root $repoRoot -Path $CacheRoot
$resolvedBinaryPath = Resolve-RepoPath -Root $repoRoot -Path $BinaryPath
$resolvedPackageLockPath = Resolve-RepoPath -Root $repoRoot -Path $PackageLockPath
$resolvedTauriCliPackagePath = Resolve-RepoPath -Root $repoRoot -Path $TauriCliPackagePath
$resolvedTauriCliPlatformPackagePath = Resolve-RepoPath -Root $repoRoot -Path $TauriCliPlatformPackagePath
$cachedBinaryPath = Join-Path $resolvedCacheRoot "scriber-desktop.exe"
$cachedTauriCliRoot = Join-Path $resolvedCacheRoot "tauri-cli\node_modules\@tauri-apps"
$cachedTauriCliEntrypoint = Join-Path $cachedTauriCliRoot "cli\tauri.js"
$manifestPath = Join-Path $resolvedCacheRoot "manifest.json"
Assert-UnderRoot -Root $buildRoot -Path $resolvedCacheRoot -Label "Tauri app binary cache root"
Assert-UnderRoot -Root $repoRoot -Path $resolvedBinaryPath -Label "Tauri app binary path"
Assert-UnderRoot -Root $repoRoot -Path $resolvedPackageLockPath -Label "Frontend package lock"
Assert-UnderRoot -Root $repoRoot -Path $resolvedTauriCliPackagePath -Label "Tauri CLI package"
Assert-UnderRoot -Root $repoRoot -Path $resolvedTauriCliPlatformPackagePath -Label "Tauri CLI platform package"
$tauriCliContract = Get-LockedTauriCliContract -Path $resolvedPackageLockPath
$tauriCliFiles = @(
    "tauri-cli/node_modules/@tauri-apps/cli/package.json",
    "tauri-cli/node_modules/@tauri-apps/cli/tauri.js",
    "tauri-cli/node_modules/@tauri-apps/cli/main.js",
    "tauri-cli/node_modules/@tauri-apps/cli/index.js",
    "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/package.json",
    "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/cli.win32-x64-msvc.node"
)

if ($Mode -eq "Export") {
    if (-not (Test-Path -LiteralPath $resolvedBinaryPath -PathType Leaf)) {
        throw "Tauri app binary was not found for export: $resolvedBinaryPath"
    }
    $actualVersion = Get-NormalizedFileVersion -Path $resolvedBinaryPath
    if (-not (Test-ExpectedVersion -Actual $actualVersion -Expected $Version)) {
        throw "Tauri app binary version '$actualVersion' does not match expected version '$Version'."
    }
    $cliPackageIdentity = Get-JsonPackageIdentity -Path (Join-Path $resolvedTauriCliPackagePath "package.json")
    $platformPackageIdentity = Get-JsonPackageIdentity -Path (Join-Path $resolvedTauriCliPlatformPackagePath "package.json")
    if (
        [string]$cliPackageIdentity.name -ne [string]$tauriCliContract.packageName -or
        [string]$cliPackageIdentity.version -ne [string]$tauriCliContract.version -or
        [string]$platformPackageIdentity.name -ne [string]$tauriCliContract.platformPackageName -or
        [string]$platformPackageIdentity.version -ne [string]$tauriCliContract.version
    ) {
        throw "Installed Windows x64 Tauri CLI packages do not match Frontend/package-lock.json."
    }
    $tauriCliSourceFiles = [ordered]@{
        "tauri-cli/node_modules/@tauri-apps/cli/package.json" = (Join-Path $resolvedTauriCliPackagePath "package.json")
        "tauri-cli/node_modules/@tauri-apps/cli/tauri.js" = (Join-Path $resolvedTauriCliPackagePath "tauri.js")
        "tauri-cli/node_modules/@tauri-apps/cli/main.js" = (Join-Path $resolvedTauriCliPackagePath "main.js")
        "tauri-cli/node_modules/@tauri-apps/cli/index.js" = (Join-Path $resolvedTauriCliPackagePath "index.js")
        "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/package.json" = (Join-Path $resolvedTauriCliPlatformPackagePath "package.json")
        "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/cli.win32-x64-msvc.node" = (Join-Path $resolvedTauriCliPlatformPackagePath "cli.win32-x64-msvc.node")
    }
    foreach ($sourcePath in $tauriCliSourceFiles.Values) {
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Installed Tauri CLI file was not found for export: $sourcePath"
        }
    }
    if (Test-Path -LiteralPath $resolvedCacheRoot -PathType Container) {
        Remove-Item -LiteralPath $resolvedCacheRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $resolvedCacheRoot | Out-Null
    Copy-Item -LiteralPath $resolvedBinaryPath -Destination $cachedBinaryPath -Force
    foreach ($entry in $tauriCliSourceFiles.GetEnumerator()) {
        $destination = [System.IO.Path]::GetFullPath((Join-Path $resolvedCacheRoot ($entry.Key -replace '/', '\')))
        Assert-UnderRoot -Root $resolvedCacheRoot -Path $destination -Label "Tauri CLI export destination"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $entry.Value -Destination $destination -Force
    }
    $item = Get-Item -LiteralPath $cachedBinaryPath
    $sourceCommit = ([string]$env:GITHUB_SHA).Trim().ToLowerInvariant()
    if (-not (Test-OptionalSourceCommit -Value $sourceCommit)) {
        throw "Tauri app binary source commit must be empty or a 40-character lowercase hexadecimal Git object id."
    }
    $manifest = [ordered]@{
        apiVersion = "3"
        cacheKey = $CacheKey
        appVersion = $Version
        binaryVersion = $actualVersion
        sourceCommit = $sourceCommit
        target = "x86_64-pc-windows-msvc"
        profile = "release"
        executable = [ordered]@{
            name = "scriber-desktop.exe"
            length = [int64]$item.Length
            sha256 = (Get-FileHash -LiteralPath $cachedBinaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        tauriCli = [ordered]@{
            version = [string]$tauriCliContract.version
            packageName = [string]$tauriCliContract.packageName
            packageIntegrity = [string]$tauriCliContract.packageIntegrity
            platformPackageName = [string]$tauriCliContract.platformPackageName
            platformPackageIntegrity = [string]$tauriCliContract.platformPackageIntegrity
            entrypoint = "tauri-cli/node_modules/@tauri-apps/cli/tauri.js"
            files = @($tauriCliFiles | ForEach-Object { Get-AttestedFileRecord -Root $resolvedCacheRoot -RelativePath $_ })
        }
        exportedAt = (Get-Date).ToUniversalTime().ToString("o")
    }
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    Write-Host "Exported exact Tauri app binary and lock-bound CLI cache for $Version ($($item.Length) app bytes)."
    Write-GitHubOutput -Name "usable" -Value "true"
    Write-GitHubOutput -Name "sha256" -Value ([string]$manifest.executable.sha256)
    Write-GitHubOutput -Name "cli-entrypoint" -Value $cachedTauriCliEntrypoint
    Write-GitHubOutput -Name "cli-version" -Value ([string]$tauriCliContract.version)
    exit 0
}

if (
    -not (Test-Path -LiteralPath $cachedBinaryPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $cachedTauriCliEntrypoint -PathType Leaf) -or
    -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)
) {
    Write-Host "No complete Tauri app binary and CLI cache was restored."
    exit 0
}

try {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $item = Get-Item -LiteralPath $cachedBinaryPath
    $sha256 = (Get-FileHash -LiteralPath $cachedBinaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $actualVersion = Get-NormalizedFileVersion -Path $cachedBinaryPath
    $expectedCommit = ([string]$env:GITHUB_SHA).Trim().ToLowerInvariant()
    $manifestCommit = ([string]$manifest.sourceCommit).Trim().ToLowerInvariant()
    $commitProvenanceValid = (
        (Test-OptionalSourceCommit -Value $expectedCommit) -and
        (Test-OptionalSourceCommit -Value $manifestCommit) -and
        ([string]::IsNullOrWhiteSpace($expectedCommit) -or -not [string]::IsNullOrWhiteSpace($manifestCommit))
    )
    $manifestCliFiles = @($manifest.tauriCli.files)
    $manifestCliFileByPath = @{}
    $cliFilesValid = $manifestCliFiles.Count -eq $tauriCliFiles.Count
    foreach ($record in $manifestCliFiles) {
        $relativePath = [string]$record.path
        if (-not $relativePath -or $manifestCliFileByPath.ContainsKey($relativePath)) {
            $cliFilesValid = $false
            continue
        }
        $manifestCliFileByPath[$relativePath] = $record
    }
    foreach ($relativePath in $tauriCliFiles) {
        if (-not $manifestCliFileByPath.ContainsKey($relativePath)) {
            $cliFilesValid = $false
            continue
        }
        $expectedRecord = $manifestCliFileByPath[$relativePath]
        $actualRecord = Get-AttestedFileRecord -Root $resolvedCacheRoot -RelativePath $relativePath
        if (
            [int64]$expectedRecord.length -ne [int64]$actualRecord.length -or
            [string]$expectedRecord.sha256 -ne [string]$actualRecord.sha256
        ) {
            $cliFilesValid = $false
        }
    }
    $actualCliFiles = @(
        Get-ChildItem -LiteralPath (Join-Path $resolvedCacheRoot "tauri-cli") -Recurse -File |
            ForEach-Object {
                [System.IO.Path]::GetRelativePath($resolvedCacheRoot, $_.FullName).Replace('\', '/')
            } |
            Sort-Object
    )
    if (($actualCliFiles -join "`n") -cne ((@($tauriCliFiles | Sort-Object)) -join "`n")) {
        $cliFilesValid = $false
    }
    $cachedCliPackageIdentity = Get-JsonPackageIdentity -Path (Join-Path $cachedTauriCliRoot "cli\package.json")
    $cachedPlatformPackageIdentity = Get-JsonPackageIdentity -Path (Join-Path $cachedTauriCliRoot "cli-win32-x64-msvc\package.json")
    $valid = (
        [string]$manifest.apiVersion -eq "3" -and
        [string]$manifest.cacheKey -eq $CacheKey -and
        [string]$manifest.appVersion -eq $Version -and
        [string]$manifest.binaryVersion -eq $actualVersion -and
        [string]$manifest.target -eq "x86_64-pc-windows-msvc" -and
        [string]$manifest.profile -eq "release" -and
        $commitProvenanceValid -and
        $cliFilesValid -and
        [int64]$manifest.executable.length -eq [int64]$item.Length -and
        [string]$manifest.executable.sha256 -eq $sha256 -and
        [string]$manifest.tauriCli.version -eq [string]$tauriCliContract.version -and
        [string]$manifest.tauriCli.packageName -eq [string]$tauriCliContract.packageName -and
        [string]$manifest.tauriCli.packageIntegrity -eq [string]$tauriCliContract.packageIntegrity -and
        [string]$manifest.tauriCli.platformPackageName -eq [string]$tauriCliContract.platformPackageName -and
        [string]$manifest.tauriCli.platformPackageIntegrity -eq [string]$tauriCliContract.platformPackageIntegrity -and
        [string]$manifest.tauriCli.entrypoint -eq "tauri-cli/node_modules/@tauri-apps/cli/tauri.js" -and
        [string]$cachedCliPackageIdentity.name -eq [string]$tauriCliContract.packageName -and
        [string]$cachedCliPackageIdentity.version -eq [string]$tauriCliContract.version -and
        [string]$cachedPlatformPackageIdentity.name -eq [string]$tauriCliContract.platformPackageName -and
        [string]$cachedPlatformPackageIdentity.version -eq [string]$tauriCliContract.version -and
        (Test-ExpectedVersion -Actual $actualVersion -Expected $Version)
    )
    if ($valid) {
        $cliVersionOutput = (& node $cachedTauriCliEntrypoint --version 2>&1 | Out-String).Trim()
        $cliExitCode = $LASTEXITCODE
        $expectedCliVersionPattern = "(?:^|\s)$([regex]::Escape([string]$tauriCliContract.version))$"
        if ($cliExitCode -ne 0 -or $cliVersionOutput -notmatch $expectedCliVersionPattern) {
            $valid = $false
        }
    }
    if (-not $valid) {
        Write-Warning "Ignoring Tauri app binary cache because its exact attestation did not validate."
        exit 0
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $resolvedBinaryPath) | Out-Null
    Copy-Item -LiteralPath $cachedBinaryPath -Destination $resolvedBinaryPath -Force
    $copiedSha256 = (Get-FileHash -LiteralPath $resolvedBinaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($copiedSha256 -ne $sha256) {
        throw "Imported Tauri app binary checksum changed during copy."
    }
    Write-Host "Imported exact Tauri app binary and lock-bound CLI cache for $Version ($($item.Length) app bytes)."
    Write-GitHubOutput -Name "usable" -Value "true"
    Write-GitHubOutput -Name "sha256" -Value $sha256
    Write-GitHubOutput -Name "cli-entrypoint" -Value $cachedTauriCliEntrypoint
    Write-GitHubOutput -Name "cli-version" -Value ([string]$tauriCliContract.version)
} catch {
    Write-Warning "Ignoring unusable Tauri app binary cache: $($_.Exception.Message)"
}
