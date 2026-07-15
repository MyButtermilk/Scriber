param(
    [switch]$Execute,
    [switch]$IncludeUnusedLocalModels
)

$ErrorActionPreference = "Stop"

function Assert-WorkspaceChild {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $root = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd("\", "/")
    $candidate = [System.IO.Path]::GetFullPath($Path)
    $prefix = $root + [System.IO.Path]::DirectorySeparatorChar
    if (
        $candidate -eq $root -or
        -not $candidate.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Refusing to prune a path outside the Scriber workspace: $candidate"
    }
    return $candidate
}

function Assert-ScriberWorkspace {
    param([Parameter(Mandatory = $true)][string]$WorkspaceRoot)

    foreach ($relativeMarker in @(
        ".git",
        "AGENTS.md",
        "src\web_api.py",
        "Frontend\src-tauri\tauri.conf.json"
    )) {
        $marker = Join-Path $WorkspaceRoot $relativeMarker
        if (-not (Test-Path -LiteralPath $marker)) {
            throw "Refusing to prune a directory that is not the Scriber workspace; missing marker: $relativeMarker"
        }
    }
}

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)][System.IO.FileSystemInfo]$Item)

    return [bool]($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
}

function Assert-NoReparseAncestor {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$AllowTargetReparsePoint
    )

    $root = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd("\", "/")
    $candidate = Assert-WorkspaceChild -WorkspaceRoot $root -Path $Path
    $relative = $candidate.Substring($root.Length).TrimStart("\", "/")
    $segments = @($relative -split "[\\/]" | Where-Object { $_ })
    $current = $root

    for ($index = 0; $index -lt $segments.Count; $index += 1) {
        $current = Join-Path $current $segments[$index]
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) {
            # A descendant cannot exist once an ancestor is missing. The caller
            # may still use the verified lexical path for a later Test-Path.
            break
        }

        $isTarget = $index -eq ($segments.Count - 1)
        if (Test-ReparsePoint -Item $item) {
            if ($isTarget -and $AllowTargetReparsePoint) {
                return $candidate
            }
            throw "Refusing to traverse a reparse point while pruning Scriber: $current"
        }
        if (-not $isTarget -and -not $item.PSIsContainer) {
            throw "Refusing to traverse a non-directory workspace ancestor: $current"
        }
    }

    return $candidate
}

function Get-SafeWorkspaceTreeSizeBytes {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $root = Assert-NoReparseAncestor `
        -WorkspaceRoot $WorkspaceRoot `
        -Path $Path `
        -AllowTargetReparsePoint
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($root)
    $sizeBytes = [int64]0

    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        $safeCurrent = Assert-NoReparseAncestor `
            -WorkspaceRoot $WorkspaceRoot `
            -Path $current `
            -AllowTargetReparsePoint
        $item = Get-Item -LiteralPath $safeCurrent -Force -ErrorAction SilentlyContinue
        if ($null -eq $item -or (Test-ReparsePoint -Item $item)) {
            # A reparse point has no workspace-owned payload. Its external or
            # aliased target must not contribute to the report.
            continue
        }
        if (-not $item.PSIsContainer) {
            $sizeBytes += [int64]$item.Length
            continue
        }

        foreach ($child in Get-ChildItem -LiteralPath $safeCurrent -Force -ErrorAction Stop) {
            $safeChild = Assert-WorkspaceChild -WorkspaceRoot $WorkspaceRoot -Path $child.FullName
            if (Test-ReparsePoint -Item $child) {
                continue
            }
            if ($child.PSIsContainer) {
                $pending.Push($safeChild)
            } else {
                $sizeBytes += [int64]$child.Length
            }
        }
    }

    return $sizeBytes
}

function Remove-WorkspaceReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $safePath = Assert-NoReparseAncestor `
        -WorkspaceRoot $WorkspaceRoot `
        -Path $Path `
        -AllowTargetReparsePoint
    $item = Get-Item -LiteralPath $safePath -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        return
    }
    if (-not (Test-ReparsePoint -Item $item)) {
        throw "Expected a reparse point while pruning Scriber: $safePath"
    }

    if ($item.PSIsContainer) {
        # Directory.Delete(recursive=false) removes the verified junction or
        # directory symlink itself and never walks its target.
        [System.IO.Directory]::Delete($safePath, $false)
    } else {
        [System.IO.File]::Delete($safePath)
    }
}

function Remove-SafeWorkspaceTree {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $root = Assert-NoReparseAncestor `
        -WorkspaceRoot $WorkspaceRoot `
        -Path $Path `
        -AllowTargetReparsePoint
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $directories = [System.Collections.Generic.List[string]]::new()
    $pending.Push($root)

    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        $safeCurrent = Assert-NoReparseAncestor `
            -WorkspaceRoot $WorkspaceRoot `
            -Path $current `
            -AllowTargetReparsePoint
        $item = Get-Item -LiteralPath $safeCurrent -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) {
            continue
        }
        if (Test-ReparsePoint -Item $item) {
            Remove-WorkspaceReparsePoint -WorkspaceRoot $WorkspaceRoot -Path $safeCurrent
            continue
        }
        if (-not $item.PSIsContainer) {
            Remove-Item -LiteralPath $safeCurrent -Force
            continue
        }

        $directories.Add($safeCurrent) | Out-Null
        foreach ($child in Get-ChildItem -LiteralPath $safeCurrent -Force -ErrorAction Stop) {
            $safeChild = Assert-WorkspaceChild -WorkspaceRoot $WorkspaceRoot -Path $child.FullName
            $pending.Push($safeChild)
        }
    }

    foreach ($directory in @($directories | Sort-Object { $_.Length } -Descending)) {
        $safeDirectory = Assert-NoReparseAncestor `
            -WorkspaceRoot $WorkspaceRoot `
            -Path $directory `
            -AllowTargetReparsePoint
        $item = Get-Item -LiteralPath $safeDirectory -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) {
            continue
        }
        if (Test-ReparsePoint -Item $item) {
            Remove-WorkspaceReparsePoint -WorkspaceRoot $WorkspaceRoot -Path $safeDirectory
            continue
        }
        if (-not $item.PSIsContainer) {
            Remove-Item -LiteralPath $safeDirectory -Force
            continue
        }

        # Non-recursive removal is deliberate. A concurrent writer leaves the
        # directory non-empty and makes the prune fail safely instead of widening
        # the deletion boundary.
        [System.IO.Directory]::Delete($safeDirectory, $false)
    }
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Assert-ScriberWorkspace -WorkspaceRoot $repoRoot
$buildRoot = Join-Path $repoRoot "build"

# Preserve the current release incrementals and the expensive, reusable component
# caches needed for another installer build. Everything else below is reproducible.
$preservedBuildDirectories = @(
    "cache-keys",
    "diarization-worker-staged",
    "ffmpeg-profile-b-msys2",
    "rust-audio-sidecar-cache",
    "rust-audio-sidecar-target",
    "rust-diarization-sidecar-cache",
    "rust-diarization-sidecar-target",
    "rust-release-cache",
    "sherpa-onnx-archive-cache",
    "tauri-app-binary-cache",
    "tauri-sidecar-cache"
)

$candidatePaths = [System.Collections.Generic.List[string]]::new()
foreach ($relativePath in @(
    ".pytest_cache",
    ".ruff_cache",
    "Frontend\dist",
    "Frontend\src-tauri\target\debug",
    "Frontend\src-tauri\target\release\bundle",
    "native\scriber-diarization-sidecar\target",
    "dist\tauri-sidecar",
    "Scriber Install",
    "tmp"
)) {
    $candidatePaths.Add((Join-Path $repoRoot $relativePath)) | Out-Null
}

if ($IncludeUnusedLocalModels) {
    # This exact public Hugging Face snapshot is reproducible at revision
    # 2a97df7e501bc25c6106a150a3379c5272088c53. Scriber downloads supported
    # runtime models into its user data directory; no installer or test contract
    # reads this legacy repository-root copy.
    $candidatePaths.Add((Join-Path $repoRoot "sherpa-onnx-parakeet-primeline-de-int8")) | Out-Null
}

if (Test-Path -LiteralPath $buildRoot -PathType Container) {
    # Never enumerate build through a junction/symlink to another tree.
    Assert-NoReparseAncestor -WorkspaceRoot $repoRoot -Path $buildRoot | Out-Null
    foreach ($directory in Get-ChildItem -LiteralPath $buildRoot -Force -Directory) {
        if ($directory.Name -notin $preservedBuildDirectories) {
            $candidatePaths.Add($directory.FullName) | Out-Null
        }
    }
}

$targets = @(
    $candidatePaths |
        ForEach-Object { Assert-WorkspaceChild -WorkspaceRoot $repoRoot -Path $_ } |
        Sort-Object -Unique |
        Where-Object { Test-Path -LiteralPath $_ }
)

$report = [System.Collections.Generic.List[object]]::new()
$bytesRemoved = [int64]0
foreach ($target in $targets) {
    $safeTarget = Assert-NoReparseAncestor `
        -WorkspaceRoot $repoRoot `
        -Path $target `
        -AllowTargetReparsePoint
    $sizeBytes = Get-SafeWorkspaceTreeSizeBytes -WorkspaceRoot $repoRoot -Path $safeTarget
    $report.Add([pscustomobject]@{
        path = $safeTarget
        bytes = $sizeBytes
        gib = [Math]::Round($sizeBytes / 1GB, 3)
        action = if ($Execute) { "removed" } else { "would_remove" }
    }) | Out-Null

    if ($Execute) {
        Remove-SafeWorkspaceTree -WorkspaceRoot $repoRoot -Path $safeTarget
        if (Test-Path -LiteralPath $safeTarget) {
            throw "Prune target still exists after removal: $safeTarget"
        }
        $bytesRemoved += $sizeBytes
    }
}

if ($Execute) {
    New-Item -ItemType Directory -Path (Join-Path $repoRoot "tmp") -Force | Out-Null
}

$summary = [ordered]@{
    apiVersion = "1"
    mode = if ($Execute) { "execute" } else { "dry_run" }
    workspaceRoot = $repoRoot
    targetCount = $targets.Count
    bytesRemoved = $bytesRemoved
    gibRemoved = [Math]::Round($bytesRemoved / 1GB, 3)
    includeUnusedLocalModels = [bool]$IncludeUnusedLocalModels
    preservedBuildDirectories = $preservedBuildDirectories
    targets = $report
}
$summary | ConvertTo-Json -Depth 5
