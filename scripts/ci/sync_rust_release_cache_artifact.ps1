param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Export", "Import")]
    [string]$Mode,
    [string]$CacheRoot = "build\rust-release-cache",
    [string]$CargoHome = $env:CARGO_HOME,
    [string]$TargetDir = "Frontend\src-tauri\target"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Write-GitHubOutput {
    param([string]$Name, [string]$Value)
    if ($env:GITHUB_OUTPUT) {
        "$Name=$Value" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
}

function Resolve-RepoPath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }
    return (Join-Path $repoRoot $Path)
}

function Copy-DirectoryIfPresent {
    param(
        [string]$Source,
        [string]$Destination,
        [System.Collections.Generic.List[string]]$Copied
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        return
    }
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (Test-Path -LiteralPath $Destination -PathType Container) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
    $Copied.Add($Destination)
}

if (-not $CargoHome) {
    $CargoHome = Join-Path $env:USERPROFILE ".cargo"
}

$resolvedCacheRoot = Resolve-RepoPath $CacheRoot
$resolvedCargoHome = Resolve-RepoPath $CargoHome
$resolvedTargetDir = Resolve-RepoPath $TargetDir
$copied = New-Object System.Collections.Generic.List[string]

if ($Mode -eq "Export") {
    Write-GitHubOutput -Name "staged" -Value "false"
    if (Test-Path -LiteralPath $resolvedCacheRoot -PathType Container) {
        Remove-Item -LiteralPath $resolvedCacheRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $resolvedCacheRoot | Out-Null

    Copy-DirectoryIfPresent `
        -Source (Join-Path $resolvedCargoHome "registry\index") `
        -Destination (Join-Path $resolvedCacheRoot "cargo\registry\index") `
        -Copied $copied
    Copy-DirectoryIfPresent `
        -Source (Join-Path $resolvedCargoHome "registry\cache") `
        -Destination (Join-Path $resolvedCacheRoot "cargo\registry\cache") `
        -Copied $copied
    Copy-DirectoryIfPresent `
        -Source (Join-Path $resolvedCargoHome "git\db") `
        -Destination (Join-Path $resolvedCacheRoot "cargo\git\db") `
        -Copied $copied

    foreach ($relative in @(
        "release\.fingerprint",
        "release\build",
        "release\deps",
        "release\incremental"
    )) {
        Copy-DirectoryIfPresent `
            -Source (Join-Path $resolvedTargetDir $relative) `
            -Destination (Join-Path $resolvedCacheRoot ("target\" + $relative)) `
            -Copied $copied
    }

    $manifest = [ordered]@{
        apiVersion = "1"
        mode = "export"
        ok = $copied.Count -gt 0
        exportedAt = (Get-Date).ToUniversalTime().ToString("o")
        cargoHome = $resolvedCargoHome
        targetDir = $resolvedTargetDir
        copiedCount = $copied.Count
        copied = @($copied)
    }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $resolvedCacheRoot "manifest.json") -Encoding utf8

    if ($copied.Count -eq 0) {
        Write-Warning "No Rust release cache directories were found to export."
        exit 0
    }

    Write-Host "Staged Rust release cache artifact at $resolvedCacheRoot ($($copied.Count) directories)."
    Write-GitHubOutput -Name "staged" -Value "true"
    exit 0
}

Write-GitHubOutput -Name "imported" -Value "false"
if (-not (Test-Path -LiteralPath $resolvedCacheRoot -PathType Container)) {
    Write-Host "Rust release cache artifact directory is missing: $resolvedCacheRoot"
    exit 0
}

Copy-DirectoryIfPresent `
    -Source (Join-Path $resolvedCacheRoot "cargo\registry\index") `
    -Destination (Join-Path $resolvedCargoHome "registry\index") `
    -Copied $copied
Copy-DirectoryIfPresent `
    -Source (Join-Path $resolvedCacheRoot "cargo\registry\cache") `
    -Destination (Join-Path $resolvedCargoHome "registry\cache") `
    -Copied $copied
Copy-DirectoryIfPresent `
    -Source (Join-Path $resolvedCacheRoot "cargo\git\db") `
    -Destination (Join-Path $resolvedCargoHome "git\db") `
    -Copied $copied

foreach ($relative in @(
    "release\.fingerprint",
    "release\build",
    "release\deps",
    "release\incremental"
)) {
    Copy-DirectoryIfPresent `
        -Source (Join-Path $resolvedCacheRoot ("target\" + $relative)) `
        -Destination (Join-Path $resolvedTargetDir $relative) `
        -Copied $copied
}

if ($copied.Count -eq 0) {
    Write-Warning "Rust release cache artifact did not contain recognized cache directories."
    exit 0
}

Write-Host "Imported Rust release cache artifact from $resolvedCacheRoot ($($copied.Count) directories)."
Write-GitHubOutput -Name "imported" -Value "true"
