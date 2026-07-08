param(
    [string]$OutputDir = "build\cache-keys"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$resolvedOutputDir = Join-Path $repoRoot $OutputDir
New-Item -ItemType Directory -Force -Path $resolvedOutputDir | Out-Null

function Get-RelativePath {
    param([string]$Path)
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $rootWithSeparator = $repoRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    $relativeUri = ([Uri]$rootWithSeparator).MakeRelativeUri([Uri]$resolved).ToString()
    return [Uri]::UnescapeDataString($relativeUri).Replace("\", "/")
}

function Get-StringSha256 {
    param([string]$Value)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-FileSha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Normalize-CargoToml {
    param([string]$Text)
    $lines = $Text -split "\r\n|\n|\r"
    $inPackage = $false
    $output = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match '^\[package\]\s*$') {
            $inPackage = $true
        } elseif ($line -match '^\[') {
            $inPackage = $false
        }
        if ($inPackage -and $line -match '^version\s*=') {
            $output.Add('version = "__app_version__"')
        } else {
            $output.Add($line)
        }
    }
    return ($output -join "`n")
}

function Normalize-CargoLock {
    param([string]$Text)
    return [regex]::Replace(
        $Text,
        '(?ms)(\[\[package\]\]\s+name = "scriber-desktop"\s+version = )"[^"]+"',
        '$1"__app_version__"'
    )
}

function Normalize-PythonVersionFile {
    param([string]$Text)
    return [regex]::Replace(
        $Text,
        '(?m)^__version__\s*=\s*"[^"]+"',
        '__version__ = "__app_version__"'
    )
}

function Normalize-FirstJsonVersionProperties {
    param(
        [string]$Text,
        [int]$Count
    )
    $pattern = '(?m)^(\s*"version"\s*:\s*)"[^"]+"'
    $matches = [regex]::Matches($Text, $pattern)
    if ($matches.Count -eq 0 -or $Count -le 0) {
        return $Text
    }
    $builder = New-Object System.Text.StringBuilder
    $lastIndex = 0
    $replaced = 0
    foreach ($match in $matches) {
        if ($replaced -ge $Count) {
            break
        }
        [void]$builder.Append($Text.Substring($lastIndex, $match.Index - $lastIndex))
        [void]$builder.Append($match.Groups[1].Value)
        [void]$builder.Append('"__app_version__"')
        $lastIndex = $match.Index + $match.Length
        $replaced += 1
    }
    [void]$builder.Append($Text.Substring($lastIndex))
    return $builder.ToString()
}

function New-EntryList {
    return New-Object System.Collections.Generic.List[string]
}

function Add-RawFileEntry {
    param(
        [System.Collections.Generic.List[string]]$Entries,
        [string]$Path
    )
    $absolutePath = Join-Path $repoRoot $Path
    $Entries.Add("file`t$(Get-RelativePath $absolutePath)`t$(Get-FileSha256 $absolutePath)")
}

function Add-ContentEntry {
    param(
        [System.Collections.Generic.List[string]]$Entries,
        [string]$Path,
        [string]$Content
    )
    $absolutePath = Join-Path $repoRoot $Path
    $Entries.Add("content`t$(Get-RelativePath $absolutePath)`t$(Get-StringSha256 $Content)")
}

function Add-FileGlobEntries {
    param(
        [System.Collections.Generic.List[string]]$Entries,
        [string]$Root,
        [string]$Filter,
        [scriptblock]$ContentNormalizer = $null
    )
    $absoluteRoot = Join-Path $repoRoot $Root
    Get-ChildItem -LiteralPath $absoluteRoot -Recurse -File -Filter $Filter |
        Sort-Object FullName |
        ForEach-Object {
            $relative = Get-RelativePath $_.FullName
            if ($ContentNormalizer) {
                $content = Get-Content -LiteralPath $_.FullName -Raw
                $normalized = & $ContentNormalizer $relative $content
                $Entries.Add("content`t$relative`t$(Get-StringSha256 $normalized)")
            } else {
                $Entries.Add("file`t$relative`t$(Get-FileSha256 $_.FullName)")
            }
        }
}

function Write-KeyFile {
    param(
        [string]$Name,
        [System.Collections.Generic.List[string]]$Entries
    )
    $path = Join-Path $resolvedOutputDir $Name
    $Entries | Sort-Object | Set-Content -LiteralPath $path -Encoding utf8
    Write-Host "Wrote release cache key input: $path ($($Entries.Count) entries)"
}

$frontendEntries = New-EntryList
Add-RawFileEntry -Entries $frontendEntries -Path ".node-version"
Add-ContentEntry -Entries $frontendEntries -Path "Frontend/package.json" -Content (Normalize-FirstJsonVersionProperties -Text (Get-Content -LiteralPath (Join-Path $repoRoot "Frontend/package.json") -Raw) -Count 1)
Add-ContentEntry -Entries $frontendEntries -Path "Frontend/package-lock.json" -Content (Normalize-FirstJsonVersionProperties -Text (Get-Content -LiteralPath (Join-Path $repoRoot "Frontend/package-lock.json") -Raw) -Count 2)
Write-KeyFile -Name "frontend-dependencies.txt" -Entries $frontendEntries

$cargoToml = Get-Content -LiteralPath (Join-Path $repoRoot "Frontend/src-tauri/Cargo.toml") -Raw
$cargoLock = Get-Content -LiteralPath (Join-Path $repoRoot "Frontend/src-tauri/Cargo.lock") -Raw

$rustEntries = New-EntryList
Add-ContentEntry -Entries $rustEntries -Path "Frontend/src-tauri/Cargo.toml" -Content (Normalize-CargoToml -Text $cargoToml)
Add-ContentEntry -Entries $rustEntries -Path "Frontend/src-tauri/Cargo.lock" -Content (Normalize-CargoLock -Text $cargoLock)
Add-RawFileEntry -Entries $rustEntries -Path "Frontend/src-tauri/build.rs"
Add-FileGlobEntries -Entries $rustEntries -Root "Frontend/src-tauri/src" -Filter "*.rs"
Write-KeyFile -Name "rust-release.txt" -Entries $rustEntries

$rustAudioEntries = New-EntryList
Add-ContentEntry -Entries $rustAudioEntries -Path "Frontend/src-tauri/Cargo.toml" -Content (Normalize-CargoToml -Text $cargoToml)
Add-ContentEntry -Entries $rustAudioEntries -Path "Frontend/src-tauri/Cargo.lock" -Content (Normalize-CargoLock -Text $cargoLock)
foreach ($path in @(
    "Frontend/src-tauri/build.rs",
    "Frontend/src-tauri/src/audio_sidecar.rs",
    "Frontend/src-tauri/src/audio_frame_pipe.rs",
    "Frontend/src-tauri/src/redaction.rs"
)) {
    Add-RawFileEntry -Entries $rustAudioEntries -Path $path
}
Write-KeyFile -Name "rust-audio-sidecar.txt" -Entries $rustAudioEntries

$backendEntries = New-EntryList
foreach ($path in @(
    "requirements-base.txt",
    "requirements-build.txt",
    "packaging/scriber-backend.spec",
    "scripts/build_tauri_backend_sidecar.ps1",
    "Frontend/src-tauri/build.rs",
    "Frontend/src-tauri/src/audio_sidecar.rs",
    "Frontend/src-tauri/src/audio_sidecar_client.rs",
    "Frontend/src-tauri/src/audio_frame_pipe.rs",
    "Frontend/src-tauri/src/redaction.rs"
)) {
    Add-RawFileEntry -Entries $backendEntries -Path $path
}
Add-ContentEntry -Entries $backendEntries -Path "Frontend/src-tauri/Cargo.toml" -Content (Normalize-CargoToml -Text $cargoToml)
Add-ContentEntry -Entries $backendEntries -Path "Frontend/src-tauri/Cargo.lock" -Content (Normalize-CargoLock -Text $cargoLock)
Add-FileGlobEntries -Entries $backendEntries -Root "src" -Filter "*.py" -ContentNormalizer {
    param([string]$RelativePath, [string]$Content)
    if ($RelativePath -eq "src/version.py") {
        return Normalize-PythonVersionFile -Text $Content
    }
    return $Content
}
Write-KeyFile -Name "backend-sidecar.txt" -Entries $backendEntries
