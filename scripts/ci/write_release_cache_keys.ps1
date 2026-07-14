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
        '${1}"__app_version__"'
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

function Get-BackendSidecarOutputContract {
    $contractPath = Join-Path $repoRoot "packaging/backend-sidecar-output-contract.json"
    if (-not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
        throw "Missing backend sidecar output contract: $contractPath"
    }

    try {
        $contract = Get-Content -LiteralPath $contractPath -Raw | ConvertFrom-Json
    } catch {
        throw "Backend sidecar output contract is not valid JSON: $contractPath"
    }

    if (
        [int]$contract.schemaVersion -ne 1 -or
        [string]$contract.name -ne "scriber-backend-onedir" -or
        [int]$contract.revision -lt 1
    ) {
        throw "Backend sidecar output contract is invalid: $contractPath"
    }

    return [ordered]@{
        schemaVersion = 1
        name = "scriber-backend-onedir"
        revision = [int]$contract.revision
    }
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

$rustDependencyEntries = New-EntryList
Add-ContentEntry -Entries $rustDependencyEntries -Path "Frontend/src-tauri/Cargo.toml" -Content (Normalize-CargoToml -Text $cargoToml)
Add-ContentEntry -Entries $rustDependencyEntries -Path "Frontend/src-tauri/Cargo.lock" -Content (Normalize-CargoLock -Text $cargoLock)
$rustDependencyEntries.Add("constant`ttarget`tx86_64-pc-windows-msvc")
$rustDependencyEntries.Add("constant`tprofile`trelease-incremental")
Write-KeyFile -Name "rust-dependencies.txt" -Entries $rustDependencyEntries

$rustEntries = New-EntryList
$rustEntries.Add("constant`ttoolchain`trust-1.97.0")
Add-ContentEntry -Entries $rustEntries -Path "Frontend/src-tauri/Cargo.toml" -Content (Normalize-CargoToml -Text $cargoToml)
Add-ContentEntry -Entries $rustEntries -Path "Frontend/src-tauri/Cargo.lock" -Content (Normalize-CargoLock -Text $cargoLock)
Add-RawFileEntry -Entries $rustEntries -Path "Frontend/src-tauri/build.rs"
Add-RawFileEntry -Entries $rustEntries -Path "Frontend/src-tauri/tauri.conf.json"
Add-RawFileEntry -Entries $rustEntries -Path "THIRD_PARTY_NOTICES.md"
Add-FileGlobEntries -Entries $rustEntries -Root "Frontend/src-tauri/capabilities" -Filter "*.json"
Add-FileGlobEntries -Entries $rustEntries -Root "Frontend/src-tauri/icons" -Filter "*"
Add-FileGlobEntries -Entries $rustEntries -Root "Frontend/src-tauri/src" -Filter "*.rs"
Write-KeyFile -Name "rust-release.txt" -Entries $rustEntries

$tauriAppEntries = New-EntryList
foreach ($path in @(
    "src/version.py",
    "Frontend/package.json",
    "Frontend/package-lock.json",
    "Frontend/components.json",
    "Frontend/postcss.config.js",
    "Frontend/tsconfig.json",
    "Frontend/vite-plugin-meta-images.ts",
    "Frontend/vite.config.ts",
    "Frontend/src-tauri/Cargo.toml",
    "Frontend/src-tauri/Cargo.lock",
    "Frontend/src-tauri/build.rs",
    "Frontend/src-tauri/tauri.conf.json",
    "scripts/build_windows.ps1",
    "scripts/prepare_tauri_updater_config.py",
    "THIRD_PARTY_NOTICES.md"
)) {
    Add-RawFileEntry -Entries $tauriAppEntries -Path $path
}
Add-FileGlobEntries -Entries $tauriAppEntries -Root "Frontend/client" -Filter "*"
Add-FileGlobEntries -Entries $tauriAppEntries -Root "Frontend/shared" -Filter "*"
Add-FileGlobEntries -Entries $tauriAppEntries -Root "Frontend/src-tauri/capabilities" -Filter "*.json"
Add-FileGlobEntries -Entries $tauriAppEntries -Root "Frontend/src-tauri/icons" -Filter "*"
Add-FileGlobEntries -Entries $tauriAppEntries -Root "Frontend/src-tauri/src" -Filter "*.rs"
$tauriAppEntries.Add("constant`ttarget`tx86_64-pc-windows-msvc")
$tauriAppEntries.Add("constant`tprofile`trelease")
Write-KeyFile -Name "tauri-app-binary.txt" -Entries $tauriAppEntries

$rustAudioEntries = New-EntryList
$rustAudioEntries.Add("constant`ttoolchain`trust-1.97.0")
Add-ContentEntry -Entries $rustAudioEntries -Path "Frontend/src-tauri/Cargo.toml" -Content (Normalize-CargoToml -Text $cargoToml)
Add-ContentEntry -Entries $rustAudioEntries -Path "Frontend/src-tauri/Cargo.lock" -Content (Normalize-CargoLock -Text $cargoLock)
foreach ($path in @(
    "Frontend/src-tauri/build.rs",
    "Frontend/src-tauri/src/audio_sidecar.rs",
    "Frontend/src-tauri/src/audio_frame_pipe.rs",
    "Frontend/src-tauri/src/meeting_aec.rs",
    "Frontend/src-tauri/src/redaction.rs"
)) {
    Add-RawFileEntry -Entries $rustAudioEntries -Path $path
}
Write-KeyFile -Name "rust-audio-sidecar.txt" -Entries $rustAudioEntries

$rustDiarizationEntries = New-EntryList
$rustDiarizationEntries.Add("constant`ttoolchain`trust-1.97.0")
foreach ($path in @(
    "native/scriber-diarization-sidecar/.cargo/config.toml",
    "native/scriber-diarization-sidecar/Cargo.toml",
    "native/scriber-diarization-sidecar/Cargo.lock",
    "native/scriber-diarization-sidecar/build.rs",
    "scripts/write_diarization_worker_manifest.py"
)) {
    Add-RawFileEntry -Entries $rustDiarizationEntries -Path $path
}
Add-FileGlobEntries -Entries $rustDiarizationEntries -Root "native/scriber-diarization-sidecar/src" -Filter "*.rs"
$rustDiarizationEntries.Add("constant`ttarget`tx86_64-pc-windows-msvc")
$rustDiarizationEntries.Add("constant`tcache-contract`tstatic-sherpa-worker-v1")
$rustDiarizationEntries.Add("constant`tsherpa-archive-sha256`tf6555701d6397d74f1302b0666a661f32708b599a14a5fde80835d4902fcd315")
Write-KeyFile -Name "rust-diarization-sidecar.txt" -Entries $rustDiarizationEntries

$sherpaArchiveEntries = New-EntryList
$sherpaArchiveEntries.Add("constant`tname`tsherpa-onnx-v1.13.3-win-x64-static-MT-Release-lib.tar.bz2")
$sherpaArchiveEntries.Add("constant`tsha256`tf6555701d6397d74f1302b0666a661f32708b599a14a5fde80835d4902fcd315")
Write-KeyFile -Name "sherpa-onnx-archive.txt" -Entries $sherpaArchiveEntries

$backendEntries = New-EntryList
foreach ($path in @(
    "requirements-base.txt",
    "requirements-build.txt",
    "packaging/scriber-backend.spec",
    "scripts/check_backend_runtime_imports.py"
)) {
    Add-RawFileEntry -Entries $backendEntries -Path $path
}
$backendContract = Get-BackendSidecarOutputContract
$backendEntries.Add("contract`tschema-version`t$($backendContract.schemaVersion)")
$backendEntries.Add("contract`tname`t$($backendContract.name)")
$backendEntries.Add("contract`trevision`t$($backendContract.revision)")
Add-FileGlobEntries -Entries $backendEntries -Root "pyloudnorm" -Filter "*.py"
$backendSourceRoot = Join-Path $repoRoot "src"
Get-ChildItem -LiteralPath $backendSourceRoot -Recurse -File |
    Where-Object {
        $_.FullName -notmatch "\\__pycache__\\" -and
        $_.Extension -notin @(".pyc", ".pyo")
    } |
    Sort-Object FullName |
    ForEach-Object {
        $relative = Get-RelativePath $_.FullName
        if ($relative -eq "src/version.py") {
            $normalized = Normalize-PythonVersionFile -Text (Get-Content -LiteralPath $_.FullName -Raw)
            $backendEntries.Add("content`t$relative`t$(Get-StringSha256 $normalized)")
        } else {
            $backendEntries.Add("file`t$relative`t$(Get-FileSha256 $_.FullName)")
        }
    }
$backendEntries.Add("constant`tffmpeg-profile`tffmpeg-profile-b-n7.0-v4")
$backendEntries.Add("flag`tbundleMediaTools`ttrue")
$backendEntries.Add("flag`tuseProfileBFfmpeg`ttrue")
$backendEntries.Add("flag`tuseGyanFfmpegEssentials`tfalse")
$backendEntries.Add("flag`tskipBundledFfprobe`tfalse")
$backendEntries.Add("flag`tvalidateSlimMediaTools`ttrue")
$backendEntries.Add("flag`tpyInstallerClean`ttrue")
Write-KeyFile -Name "backend-sidecar.txt" -Entries $backendEntries
