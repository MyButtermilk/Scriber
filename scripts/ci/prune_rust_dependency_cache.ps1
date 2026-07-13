param(
    [string]$TargetDir = "Frontend\src-tauri\target"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedTargetDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $TargetDir))
$expectedRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "Frontend\src-tauri\target"))
if (-not $resolvedTargetDir.Equals($expectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Rust dependency cache pruning is restricted to the Scriber Tauri target directory."
}

$releaseDir = Join-Path $resolvedTargetDir "release"
if (-not (Test-Path -LiteralPath $releaseDir -PathType Container)) {
    Write-Host "Rust release target does not exist; no app artifacts need pruning."
    exit 0
}

$candidates = [System.Collections.Generic.List[System.IO.FileSystemInfo]]::new()
foreach ($rule in @(
    @{ Root = ".fingerprint"; Patterns = @("scriber-desktop-*") },
    @{ Root = "build"; Patterns = @("scriber-desktop-*") },
    @{ Root = "deps"; Patterns = @("scriber_desktop*", "libscriber_desktop*", "scriber_audio_sidecar*") },
    @{ Root = "incremental"; Patterns = @("scriber_desktop*", "scriber_audio_sidecar*") }
)) {
    $root = Join-Path $releaseDir $rule.Root
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        continue
    }
    foreach ($pattern in $rule.Patterns) {
        foreach ($item in Get-ChildItem -LiteralPath $root -Filter $pattern -Force -ErrorAction SilentlyContinue) {
            $candidates.Add($item) | Out-Null
        }
    }
}

$releasePrefix = $releaseDir.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
$removed = 0
foreach ($item in @($candidates | Sort-Object FullName -Unique)) {
    $resolvedItem = [System.IO.Path]::GetFullPath($item.FullName)
    if (-not $resolvedItem.StartsWith($releasePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove Rust app artifact outside target/release: $resolvedItem"
    }
    Remove-Item -LiteralPath $resolvedItem -Recurse -Force
    $removed += 1
}

Write-Host "Removed $removed Scriber app-specific entries from the reusable Rust dependency cache."
