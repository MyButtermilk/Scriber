<#
.SYNOPSIS
Builds the Python backend worker as a PyInstaller onedir sidecar for Tauri.

.DESCRIPTION
The generated sidecar can be launched by the Rust supervisor through
SCRIBER_BACKEND_EXE or by copying it next to the Tauri release executable under
target\release\backend.

Typical flow:
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -InstallPyInstaller -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -UseProfileBFfmpeg -ValidateSlimMediaTools -ReuseSidecarIfUnchanged -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -UseProfileBFfmpeg -ValidateSlimMediaTools -ReuseSidecarIfUnchanged -BundleRustAudioSidecar -BundleRustDiarizationSidecar -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -UseGyanFfmpegEssentials -ValidateSlimMediaTools -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -SkipBundledFfprobe -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -ValidateSlimMediaTools -MediaToolsDir path\to\slim-ffmpeg -CopyToTauriRelease
  powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -ReuseSidecarIfUnchanged -CopyToTauriRelease
  cd Frontend
  npm run tauri:build
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonPath = "",
    [string]$DistRoot = "",
    [string]$WorkRoot = "",
    [string]$MediaToolsDir = "",
    [switch]$SkipFrontendBuild,
    [switch]$InstallPyInstaller,
    [switch]$BundleMediaTools,
    [switch]$UseProfileBFfmpeg,
    [switch]$UseGyanFfmpegEssentials,
    [switch]$SkipBundledFfprobe,
    [switch]$ValidateSlimMediaTools,
    [switch]$ReuseSidecarIfUnchanged,
    [string]$SidecarCacheRoot = "",
    [string]$RuntimeCacheRoot = "",
    [switch]$BundleRustAudioSidecar,
    [switch]$BundleRustDiarizationSidecar,
    [switch]$ValidateFinishedRustProductsOnly,
    [string]$RustAudioSidecarCacheRoot = "",
    [string]$RustDiarizationSidecarCacheRoot = "",
    [string]$SherpaOnnxArchiveCacheRoot = "",
    [string]$RustDiarizationTargetRoot = "",
    [switch]$RustAudioIsolatedTarget,
    [switch]$ParallelizeIndependentBuilds,
    [switch]$ParallelizeRustDiarizationBuild,
    [switch]$RustAudioOnly,
    [string]$RustAudioResultPath = "",
    [switch]$RustDiarizationPrestageOnly,
    [string]$RustDiarizationResultPath = "",
    [string]$RustDiarizationPrestageBackendDir = "",
    [switch]$LocalPyInstallerNoClean,
    [switch]$CopyToTauriRelease
)

$ErrorActionPreference = "Stop"
$script:BuildTimingStarted = [System.Diagnostics.Stopwatch]::StartNew()
$script:BuildTimingPhases = [System.Collections.Generic.List[object]]::new()

function Get-BackendRuntimeContractRevisionFromSource {
    param([string]$Root)

    $contractPath = Join-Path $Root "backend_runtime\contract.py"
    if (-not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
        throw "Missing frozen backend runtime contract: $contractPath"
    }
    $match = [regex]::Match(
        (Get-Content -LiteralPath $contractPath -Raw),
        '(?m)^RUNTIME_CONTRACT_REVISION\s*=\s*(\d+)\s*$'
    )
    if (-not $match.Success -or [int]$match.Groups[1].Value -lt 1) {
        throw "Frozen backend runtime contract revision is invalid: $contractPath"
    }
    return [int]$match.Groups[1].Value
}

function Convert-ToFullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-UnderRoot {
    param(
        [string]$Root,
        [string]$Path,
        [string]$Label
    )

    $rootFull = Convert-ToFullPath -Path $Root
    $pathFull = Convert-ToFullPath -Path $Path
    if (-not $pathFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under repo root. Got: $pathFull"
    }
}

function Invoke-TimedStep {
    param(
        [string]$Label,
        [scriptblock]$Command
    )

    $stepWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $ok = $false
    try {
        & $Command
        $ok = $true
    } finally {
        $stepWatch.Stop()
        $script:BuildTimingPhases.Add([ordered]@{
            label = $Label
            durationMs = [int64]$stepWatch.ElapsedMilliseconds
            ok = $ok
        }) | Out-Null
    }
}

function Stop-ChildProcessTree {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Label
    )

    if ($null -eq $Process) {
        return
    }

    try {
        if ($Process.HasExited) {
            return
        }
    } catch {
        return
    }

    Write-Warning "Stopping unfinished $Label process tree."
    $terminated = $false
    $taskkillPath = if ($env:SystemRoot) { Join-Path $env:SystemRoot "System32\taskkill.exe" } else { "" }
    if ($taskkillPath -and (Test-Path -LiteralPath $taskkillPath -PathType Leaf)) {
        try {
            $taskkillProcess = Start-Process `
                -FilePath $taskkillPath `
                -ArgumentList @("/PID", [string]$Process.Id, "/T", "/F") `
                -WindowStyle Hidden `
                -Wait `
                -PassThru
            $terminated = ($taskkillProcess.ExitCode -eq 0)
            $taskkillProcess.Dispose()
        } catch {
            $terminated = $false
        }
    }

    if (-not $terminated) {
        try {
            $Process.Kill()
        } catch {
            # The child may have exited between the status check and Kill().
        }
    }

    try {
        [void]$Process.WaitForExit(10000)
    } catch {
        # Cleanup is best-effort and must not hide the original build failure.
    }
}

function Get-StringSha256 {
    param([string]$Value)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-FileIdentityTreeSha256 {
    param([object[]]$Entries)

    # Do not hash ConvertTo-Json output here. Windows PowerShell 5.1 builds the
    # frozen layer, while cache validation runs in PowerShell 7; their JSON
    # serializers are not byte-identical for every filename/value. This
    # NUL-delimited, ordinally sorted representation is stable across both.
    $byPath = [System.Collections.Generic.SortedDictionary[string, object]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($entry in @($Entries)) {
        $path = [string](Get-ObjectPropertyValue -Object $entry -Name "path")
        if (-not $path -or $path.IndexOf([char]0) -ge 0 -or $byPath.ContainsKey($path)) {
            throw "Runtime file identity contains an invalid or duplicate path."
        }
        $byPath.Add($path, $entry)
    }
    $builder = [System.Text.StringBuilder]::new()
    foreach ($pair in $byPath.GetEnumerator()) {
        $length = [int64](Get-ObjectPropertyValue -Object $pair.Value -Name "length")
        $sha256 = [string](Get-ObjectPropertyValue -Object $pair.Value -Name "sha256")
        [void]$builder.Append($pair.Key)
        [void]$builder.Append([char]0)
        [void]$builder.Append($length.ToString([System.Globalization.CultureInfo]::InvariantCulture))
        [void]$builder.Append([char]0)
        [void]$builder.Append($sha256)
        [void]$builder.Append([char]0)
    }
    return Get-StringSha256 -Value $builder.ToString()
}

function Get-Sha256Hex {
    param([string]$Path)

    $fullPath = Convert-ToFullPath -Path $Path
    $stream = [System.IO.File]::OpenRead($fullPath)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha.ComputeHash($stream)
        return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Normalize-CargoTomlForCache {
    param([string]$Text)

    $lines = $Text -split "\r\n|\n|\r"
    $inPackage = $false
    $output = [System.Collections.Generic.List[string]]::new()
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

function Normalize-CargoLockForCache {
    param([string]$Text)

    return [regex]::Replace(
        $Text,
        '(?ms)(\[\[package\]\]\s+name = "scriber-desktop"\s+version = )"[^"]+"',
        '${1}"__app_version__"'
    )
}

function Normalize-PythonVersionForCache {
    param([string]$Text)

    return [regex]::Replace(
        $Text,
        '(?m)^__version__\s*=\s*"[^"]+"',
        '__version__ = "__app_version__"'
    )
}

function Get-RelativePath {
    param(
        [string]$Root,
        [string]$Path
    )

    $rootFull = Convert-ToFullPath -Path $Root
    if (-not $rootFull.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $rootFull += [System.IO.Path]::DirectorySeparatorChar
    }
    $pathFull = Convert-ToFullPath -Path $Path
    $rootUri = [System.Uri]::new($rootFull)
    $pathUri = [System.Uri]::new($pathFull)
    $relative = $rootUri.MakeRelativeUri($pathUri).ToString()
    return ([System.Uri]::UnescapeDataString($relative)).Replace("/", [System.IO.Path]::DirectorySeparatorChar)
}

function Get-ContentHashEntry {
    param(
        [string]$Root,
        [string]$Path,
        [string]$Content
    )

    $canonicalContent = $Content -replace "\r\n", "`n" -replace "\r", "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($canonicalContent)
    return [ordered]@{
        path = (Get-RelativePath -Root $Root -Path $Path)
        length = [int64]$bytes.Length
        sha256 = Get-StringSha256 -Value $canonicalContent
    }
}

function Get-NormalizedFileHashEntry {
    param(
        [string]$Root,
        [string]$RelativePath,
        [scriptblock]$Normalizer
    )

    $path = Join-Path $Root $RelativePath
    $content = Get-Content -LiteralPath $path -Raw
    $normalized = & $Normalizer $content
    return Get-ContentHashEntry -Root $Root -Path $path -Content $normalized
}

function Get-FileHashEntry {
    param(
        [string]$Root,
        [string]$Path
    )

    $item = Get-Item -LiteralPath $Path
    $textExtensions = @(
        ".c", ".cc", ".cmake", ".conf", ".cpp", ".css", ".csv", ".h", ".hpp",
        ".html", ".ini", ".js", ".json", ".jsx", ".md", ".mjs", ".ps1",
        ".psd1", ".psm1", ".py", ".rs", ".scss", ".svg", ".toml", ".ts",
        ".tsx", ".txt", ".xml", ".yaml", ".yml"
    )
    $isText = (
        $item.Extension -in $textExtensions -or
        $item.Name -in @("Cargo.lock", "Dockerfile", "LICENSE", "Makefile", ".node-version")
    )
    if ($isText) {
        return Get-ContentHashEntry -Root $Root -Path $item.FullName -Content ([System.IO.File]::ReadAllText($item.FullName))
    }
    return [ordered]@{
        path = (Get-RelativePath -Root $Root -Path $item.FullName)
        length = [int64]$item.Length
        sha256 = Get-Sha256Hex -Path $item.FullName
    }
}

function Get-InputFileEntries {
    param(
        [string]$Root,
        [string[]]$RelativePaths,
        [bool]$NormalizeApplicationVersion = $true
    )

    $entries = @()
    foreach ($relative in $RelativePaths) {
        $candidate = Join-Path $Root $relative
        if (-not (Test-Path -LiteralPath $candidate)) {
            continue
        }
        $item = Get-Item -LiteralPath $candidate
        if ($item.PSIsContainer) {
            $files = Get-ChildItem -LiteralPath $item.FullName -Recurse -File |
                Where-Object {
                    $_.FullName -notmatch "\\__pycache__\\" -and
                    $_.Extension -notin @(".pyc", ".pyo")
                } |
                Sort-Object FullName
            foreach ($file in $files) {
                $relative = Get-RelativePath -Root $Root -Path $file.FullName
                $normalizedRelative = $relative -replace '/', '\'
                if ($NormalizeApplicationVersion -and $normalizedRelative -eq "src\version.py") {
                    $entries += Get-ContentHashEntry -Root $Root -Path $file.FullName -Content (Normalize-PythonVersionForCache -Text (Get-Content -LiteralPath $file.FullName -Raw))
                } else {
                    $entries += Get-FileHashEntry -Root $Root -Path $file.FullName
                }
            }
        } else {
            $relative = Get-RelativePath -Root $Root -Path $item.FullName
            $normalizedRelative = $relative -replace '/', '\'
            if ($NormalizeApplicationVersion -and $normalizedRelative -eq "src\version.py") {
                $entries += Get-ContentHashEntry -Root $Root -Path $item.FullName -Content (Normalize-PythonVersionForCache -Text (Get-Content -LiteralPath $item.FullName -Raw))
            } else {
                $entries += Get-FileHashEntry -Root $Root -Path $item.FullName
            }
        }
    }
    return $entries
}

function Get-GitTrackedFileEntries {
    param(
        [string]$Root,
        [string[]]$RelativePaths
    )

    $trackedPaths = @(& git -C $Root ls-files -- @RelativePaths)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not enumerate tracked backend application files."
    }
    $entries = @()
    foreach ($relativePath in @($trackedPaths | Where-Object { $_ } | Sort-Object -Unique)) {
        $candidate = Join-Path $Root ($relativePath -replace '/', '\')
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "Tracked backend application file is missing: $relativePath"
        }
        if ($relativePath -match '(^|/)__pycache__(/|$)' -or [System.IO.Path]::GetExtension($relativePath) -in @(".pyc", ".pyo")) {
            throw "Tracked bytecode is not allowed in the backend application layer: $relativePath"
        }
        $entries += Get-FileHashEntry -Root $Root -Path $candidate
    }
    return @($entries)
}

function Get-PythonFileEntries {
    param(
        [string]$Root,
        [string[]]$RelativeDirectories
    )

    $entries = @()
    foreach ($relativeDirectory in $RelativeDirectories) {
        $directory = Join-Path $Root $relativeDirectory
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            throw "Required Python source directory is missing: $relativeDirectory"
        }
        foreach ($file in Get-ChildItem -LiteralPath $directory -Recurse -File -Filter "*.py" | Sort-Object FullName) {
            if ($file.FullName -match "\\__pycache__\\") {
                throw "Python source enumeration encountered bytecode cache content: $($file.FullName)"
            }
            $entries += Get-FileHashEntry -Root $Root -Path $file.FullName
        }
    }
    return @($entries)
}

function Get-ObjectPropertyValue {
    param(
        [object]$Object,
        [string]$Name
    )

    if ($null -eq $Object) {
        return $null
    }
    # Windows PowerShell 5.1 does not adapt OrderedDictionary keys into
    # PSObject properties the same way as PowerShell 7. Freshly generated file
    # identities are ordered dictionaries, while JSON-restored identities are
    # PSCustomObjects, so support both representations explicitly.
    if ($Object -is [System.Collections.IDictionary] -and $Object.Contains($Name)) {
        return $Object[$Name]
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) {
        return $property.Value
    }
    return $null
}

function Get-SidecarFlagState {
    return [ordered]@{
        bundleMediaTools = [bool]$BundleMediaTools
        useProfileBFfmpeg = [bool]$UseProfileBFfmpeg
        useGyanFfmpegEssentials = [bool]$UseGyanFfmpegEssentials
        skipBundledFfprobe = [bool]$SkipBundledFfprobe
        validateSlimMediaTools = [bool]$ValidateSlimMediaTools
        bundleRustAudioSidecar = [bool]$BundleRustAudioSidecar
        bundleRustDiarizationSidecar = [bool]$BundleRustDiarizationSidecar
        pyInstallerClean = -not [bool]$LocalPyInstallerNoClean
        rustAudioIsolatedTarget = [bool]$RustAudioIsolatedTarget
    }
}

function Get-BackendSidecarOutputContract {
    param([string]$Root)

    $contractPath = Join-Path $Root "packaging\backend-sidecar-output-contract.json"
    if (-not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
        throw "Missing backend sidecar output contract: $contractPath"
    }

    try {
        $contract = Get-Content -LiteralPath $contractPath -Raw | ConvertFrom-Json
    } catch {
        throw "Backend sidecar output contract is not valid JSON: $contractPath"
    }

    if (
        [int](Get-ObjectPropertyValue -Object $contract -Name "schemaVersion") -ne 1 -or
        [string](Get-ObjectPropertyValue -Object $contract -Name "name") -ne "scriber-backend-onedir" -or
        [int](Get-ObjectPropertyValue -Object $contract -Name "revision") -lt 1
    ) {
        throw "Backend sidecar output contract is invalid: $contractPath"
    }

    # Select only the canonical fields so harmless JSON formatting changes do
    # not invalidate the frozen backend. Bump revision whenever output-affecting
    # builder behavior changes without changing one of the hashed inputs below.
    return [ordered]@{
        schemaVersion = 1
        name = "scriber-backend-onedir"
        revision = [int]$contract.revision
    }
}

function Get-ToolMetadataEntry {
    param(
        [string]$Path,
        [string]$Name
    )

    if (-not $Path) {
        return [ordered]@{
            name = $Name
            path = $null
            exists = $false
        }
    }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        name = $Name
        exists = $true
        length = [int64]$item.Length
        sha256 = Get-Sha256Hex -Path $item.FullName
    }
}

function Get-BackendRuntimeInputManifest {
    param(
        [string]$Root,
        [string]$Python,
        [bool]$PyInstallerClean
    )

    $pythonVersion = (& $Python -c "import sys; print(sys.version); print(sys.implementation.cache_tag)" 2>$null) -join "`n"
    $inputPaths = @(
        "packaging\scriber-backend.spec",
        "packaging\backend-sidecar-output-contract.json",
        "requirements-base.txt",
        "requirements-build.txt"
    )
    $files = @(Get-InputFileEntries -Root $Root -RelativePaths $inputPaths)
    $files += @(Get-PythonFileEntries -Root $Root -RelativeDirectories @("backend_runtime", "pyloudnorm"))
    return [ordered]@{
        apiVersion = "1"
        name = "scriber-backend-runtime-layer"
        outputContract = Get-BackendSidecarOutputContract -Root $Root
        python = $pythonVersion
        flags = [ordered]@{
            pyInstallerClean = $PyInstallerClean
        }
        files = $files
    }
}

function Get-BackendApplicationInputManifest {
    param([string]$Root)

    $files = @(
        Get-GitTrackedFileEntries -Root $Root -RelativePaths @(
            "src",
            "scripts/__init__.py",
            "scripts/check_backend_runtime_imports.py"
        )
    )
    $files += @(Get-InputFileEntries -Root $Root -NormalizeApplicationVersion $false -RelativePaths @(
        "scripts\stage_backend_application_layer.py"
    ))
    return [ordered]@{
        apiVersion = "1"
        name = "scriber-backend-application-layer"
        files = $files
    }
}

function Get-SidecarInputManifest {
    param(
        [string]$Root,
        [string]$RuntimeCacheKey,
        [string]$SearchDir,
        [bool]$BundleTools,
        [bool]$UseProfileB,
        [bool]$UseGyanEssentials,
        [bool]$SkipFfprobe,
        [bool]$ValidateSlimBundle,
        [bool]$PyInstallerClean
    )

    $tools = @()
    if ($BundleTools -or $SearchDir) {
        if ($SearchDir) {
            foreach ($dll in Get-ChildItem -LiteralPath $SearchDir -Filter "*.dll" -File -ErrorAction SilentlyContinue | Sort-Object Name) {
                $tools += Get-ToolMetadataEntry -Path $dll.FullName -Name ("dll:{0}" -f $dll.Name)
            }
        }
        $tools += Get-ToolMetadataEntry -Path (Resolve-MediaTool -Names @("ffmpeg.exe", "ffmpeg") -SearchDir $SearchDir) -Name "ffmpeg"
        if (-not $SkipFfprobe) {
            $tools += Get-ToolMetadataEntry -Path (Resolve-MediaTool -Names @("ffprobe.exe", "ffprobe") -SearchDir $SearchDir) -Name "ffprobe"
        }
        $resolvedYtDlp = Resolve-BackendStableMediaTool -Names @("yt-dlp.exe", "yt-dlp") -Python $PythonPath -ExpectedRuntimeCacheKey $RuntimeCacheKey
        $tools += Get-ToolMetadataEntry -Path $resolvedYtDlp -Name "yt-dlp"
        $resolvedDeno = Resolve-BackendStableMediaTool -Names @("deno.exe", "deno") -Python $PythonPath -ExpectedRuntimeCacheKey $RuntimeCacheKey -Required
        $tools += Get-ToolMetadataEntry -Path $resolvedDeno -Name "deno"
    }
    return [ordered]@{
        apiVersion = "3"
        outputContract = Get-BackendSidecarOutputContract -Root $Root
        runtimeCacheKey = $RuntimeCacheKey
        application = Get-BackendApplicationInputManifest -Root $Root
        flags = [ordered]@{
            bundleMediaTools = $BundleTools
            useProfileBFfmpeg = $UseProfileB
            useGyanFfmpegEssentials = $UseGyanEssentials
            skipBundledFfprobe = $SkipFfprobe
            validateSlimMediaTools = $ValidateSlimBundle
            pyInstallerClean = $PyInstallerClean
        }
        tools = $tools
    }
}

function Get-BackendRuntimeFileIdentityEntries {
    param([string]$RuntimeDir)

    $entries = @()
    foreach ($file in Get-ChildItem -LiteralPath $RuntimeDir -Recurse -File | Sort-Object FullName) {
        $relative = (Get-RelativePath -Root $RuntimeDir -Path $file.FullName) -replace '\\', '/'
        if ($relative -eq "runtime-layer-manifest.json") {
            continue
        }
        $entries += [ordered]@{
            path = $relative
            length = [int64]$file.Length
            sha256 = Get-Sha256Hex -Path $file.FullName
        }
    }
    return @($entries)
}

function Get-BackendMediaFileIdentityEntries {
    param([string]$SidecarDir)

    $mediaRoot = Join-Path $SidecarDir "tools\ffmpeg"
    if (-not (Test-Path -LiteralPath $mediaRoot -PathType Container)) {
        return @()
    }
    $entries = @()
    foreach ($file in Get-ChildItem -LiteralPath $mediaRoot -Recurse -File | Sort-Object FullName) {
        $entries += [ordered]@{
            path = ((Get-RelativePath -Root $mediaRoot -Path $file.FullName) -replace '\\', '/')
            length = [int64]$file.Length
            sha256 = Get-Sha256Hex -Path $file.FullName
        }
    }
    return @($entries)
}

function Get-BackendStableMediaFileIdentityEntries {
    param([string]$CacheRoot)

    $stableRoot = Join-Path $CacheRoot "media-tools"
    if (-not (Test-Path -LiteralPath $stableRoot -PathType Container)) {
        return @()
    }
    $entries = @()
    foreach ($file in Get-ChildItem -LiteralPath $stableRoot -Recurse -File | Sort-Object FullName) {
        $relativeWithinMedia = (Get-RelativePath -Root $stableRoot -Path $file.FullName) -replace '\\', '/'
        $entries += [ordered]@{
            path = "media-tools/$relativeWithinMedia"
            length = [int64]$file.Length
            sha256 = Get-Sha256Hex -Path $file.FullName
        }
    }
    return @($entries)
}

function Test-BackendStableMediaFiles {
    param(
        [string]$CacheRoot,
        [object[]]$ExpectedFiles
    )

    $expected = @($ExpectedFiles)
    $actual = @(Get-BackendStableMediaFileIdentityEntries -CacheRoot $CacheRoot)
    if ($expected.Count -lt 1 -or $expected.Count -ne $actual.Count) {
        return $false
    }
    $actualByPath = @{}
    foreach ($entry in $actual) {
        $actualByPath[[string]$entry.path] = $entry
    }
    $seen = @{}
    $denoFound = $false
    foreach ($entry in $expected) {
        $relative = [string](Get-ObjectPropertyValue -Object $entry -Name "path")
        $length = Get-ObjectPropertyValue -Object $entry -Name "length"
        $sha256 = [string](Get-ObjectPropertyValue -Object $entry -Name "sha256")
        $found = $actualByPath[$relative]
        if (
            -not $relative -or
            $relative.Contains("\") -or
            $relative.StartsWith("/") -or
            $relative -match '(^|/)\.\.($|/)' -or
            $seen.ContainsKey($relative) -or
            $null -eq $length -or
            [int64]$length -lt 0 -or
            $sha256 -notmatch '^[0-9a-f]{64}$' -or
            $null -eq $found -or
            [int64]$length -ne [int64]$found.length -or
            $sha256 -ne [string]$found.sha256
        ) {
            return $false
        }
        $seen[$relative] = $true
        if ([System.IO.Path]::GetFileName($relative) -in @("deno", "deno.exe")) {
            $denoFound = $true
        }
    }
    return $denoFound
}

function Resolve-BackendSidecarCandidateMediaTool {
    param(
        [string[]]$Names,
        [string]$ExpectedRuntimeCacheKey
    )

    if ($script:SidecarCandidateMediaCacheKey -ne $ExpectedRuntimeCacheKey) {
        $script:SidecarCandidateMediaCacheKey = $ExpectedRuntimeCacheKey
        $script:SidecarCandidateMediaCandidates = @()
        if ($SidecarCacheRoot -and (Test-Path -LiteralPath $SidecarCacheRoot -PathType Container)) {
            foreach ($cacheDirectory in Get-ChildItem -LiteralPath $SidecarCacheRoot -Directory -ErrorAction SilentlyContinue | Sort-Object FullName) {
                $backendDir = Join-Path $cacheDirectory.FullName "scriber-backend"
                $manifestPath = Join-Path $cacheDirectory.FullName "cache-manifest.json"
                if (
                    -not (Test-Path -LiteralPath $backendDir -PathType Container) -or
                    -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)
                ) {
                    continue
                }
                try {
                    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
                    if (
                        [string]$manifest.inputManifest.runtimeCacheKey -ne $ExpectedRuntimeCacheKey -or
                        -not (Test-BackendMediaFiles -SidecarDir $backendDir -ExpectedFiles @($manifest.mediaFiles))
                    ) {
                        continue
                    }
                    $stableMap = @{}
                    foreach ($entry in @($manifest.mediaFiles)) {
                        $leaf = [System.IO.Path]::GetFileName([string]$entry.path).ToLowerInvariant()
                        if ($leaf -in @("deno", "deno.exe", "yt-dlp", "yt-dlp.exe")) {
                            $stableMap[$leaf] = [pscustomobject]@{
                                path = Join-Path $backendDir ("tools\ffmpeg\{0}" -f [string]$entry.path)
                                length = [int64]$entry.length
                                sha256 = [string]$entry.sha256
                            }
                        }
                    }
                    $script:SidecarCandidateMediaCandidates += [pscustomobject]@{
                        cacheRoot = $cacheDirectory.FullName
                        files = $stableMap
                    }
                } catch {
                    continue
                }
            }
        }
    }

    $matches = @()
    foreach ($candidate in @($script:SidecarCandidateMediaCandidates)) {
        foreach ($name in $Names) {
            $entry = $candidate.files[$name.ToLowerInvariant()]
            if ($entry) {
                $matches += [pscustomobject]@{
                    cacheRoot = $candidate.cacheRoot
                    path = $entry.path
                    identity = "{0}:{1}" -f $entry.sha256, $entry.length
                }
                break
            }
        }
    }
    $distinctIdentities = @($matches | Select-Object -ExpandProperty identity -Unique)
    if ($distinctIdentities.Count -gt 1) {
        throw "Full-sidecar cache generations disagree about the requested stable media tool bytes."
    }
    if ($matches.Count -ge 1) {
        return ($matches | Sort-Object cacheRoot | Select-Object -First 1).path
    }
    return $null
}

function Resolve-BackendStableMediaTool {
    param(
        [string[]]$Names,
        [string]$Python,
        [string]$ExpectedRuntimeCacheKey,
        [switch]$Required
    )

    $sidecarCandidate = Resolve-BackendSidecarCandidateMediaTool `
        -Names $Names `
        -ExpectedRuntimeCacheKey $ExpectedRuntimeCacheKey
    if ($sidecarCandidate) {
        return $sidecarCandidate
    }

    $stableRoot = Join-Path $RuntimeCacheRoot "media-tools"
    if (Test-Path -LiteralPath $stableRoot -PathType Container) {
        if ($script:StableMediaCacheKey -ne $ExpectedRuntimeCacheKey) {
            $script:StableMediaCacheKey = $ExpectedRuntimeCacheKey
            $script:StableMediaMap = @{}
            $manifestPath = Join-Path $RuntimeCacheRoot "runtime-cache-manifest.json"
            if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
                throw "The stable media runtime cache has no attestation manifest."
            }
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            if (
                [string]$manifest.cacheKey -ne $ExpectedRuntimeCacheKey -or
                -not (Test-BackendStableMediaFiles -CacheRoot $RuntimeCacheRoot -ExpectedFiles @($manifest.stableMediaFiles))
            ) {
                throw "The stable media runtime cache is incomplete, modified, or belongs to another runtime generation."
            }
            foreach ($file in Get-ChildItem -LiteralPath $stableRoot -File) {
                $script:StableMediaMap[$file.Name.ToLowerInvariant()] = $file.FullName
            }
        }
        foreach ($name in $Names) {
            $candidate = $script:StableMediaMap[$name.ToLowerInvariant()]
            if ($candidate) {
                return $candidate
            }
        }
    }

    $resolved = Resolve-PythonInstalledTool -Names $Names -Python $Python
    if (-not $resolved) {
        $resolved = Resolve-MediaTool -Names $Names -SearchDir ""
    }
    if (-not $resolved -and $Required) {
        throw "Required stable media tool was not found: $($Names -join ', ')"
    }
    return $resolved
}

function Initialize-BackendRuntimeStableMediaTools {
    param(
        [string]$CacheRoot,
        [string]$Python
    )

    $stableRoot = Join-Path $CacheRoot "media-tools"
    if (Test-Path -LiteralPath $stableRoot -PathType Container) {
        Remove-Item -LiteralPath $stableRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $stableRoot | Out-Null
    $deno = Resolve-PythonInstalledTool -Names @("deno.exe", "deno") -Python $Python
    if (-not $deno) {
        throw "Deno was not installed with the frozen backend dependencies."
    }
    $denoName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "deno.exe" } else { "deno" }
    Copy-Item -LiteralPath $deno -Destination (Join-Path $stableRoot $denoName) -Force

    $ytDlp = Resolve-MediaTool -Names @("yt-dlp.exe", "yt-dlp") -SearchDir ""
    if ($ytDlp) {
        $ytDlpName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "yt-dlp.exe" } else { "yt-dlp" }
        Copy-Item -LiteralPath $ytDlp -Destination (Join-Path $stableRoot $ytDlpName) -Force
    }
}

function Test-BackendMediaFiles {
    param(
        [string]$SidecarDir,
        [object[]]$ExpectedFiles
    )

    try {
        $expected = @($ExpectedFiles)
        $actual = @(Get-BackendMediaFileIdentityEntries -SidecarDir $SidecarDir)
        if ($expected.Count -ne $actual.Count) {
            return $false
        }
        $actualByPath = @{}
        foreach ($entry in $actual) {
            $actualByPath[[string]$entry.path] = $entry
        }
        $seen = @{}
        foreach ($entry in $expected) {
            $relative = [string](Get-ObjectPropertyValue -Object $entry -Name "path")
            $length = Get-ObjectPropertyValue -Object $entry -Name "length"
            $sha256 = [string](Get-ObjectPropertyValue -Object $entry -Name "sha256")
            if (
                -not $relative -or
                $relative.Contains("\") -or
                $relative.StartsWith("/") -or
                $relative -match '(^|/)\.\.($|/)' -or
                $seen.ContainsKey($relative) -or
                $null -eq $length -or
                [int64]$length -lt 0 -or
                $sha256 -notmatch '^[0-9a-f]{64}$'
            ) {
                return $false
            }
            $seen[$relative] = $true
            $found = $actualByPath[$relative]
            if (
                $null -eq $found -or
                [int64]$length -ne [int64]$found.length -or
                $sha256 -ne [string]$found.sha256
            ) {
                return $false
            }
        }
        return $true
    } catch {
        return $false
    }
}

function Test-BackendRuntimeLayer {
    param(
        [string]$RuntimeDir,
        [string]$ExpectedCacheKey,
        [switch]$PureRuntime
    )

    $layerManifestPath = Join-Path $RuntimeDir "runtime-layer-manifest.json"
    $runtimeExe = Join-Path $RuntimeDir "scriber-backend.exe"
    if (-not (Test-Path -LiteralPath $runtimeExe -PathType Leaf)) {
        $runtimeExe = Join-Path $RuntimeDir "scriber-backend"
    }
    if (
        -not (Test-Path -LiteralPath $layerManifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $runtimeExe -PathType Leaf)
    ) {
        return $false
    }
    try {
        $layerManifest = Get-Content -LiteralPath $layerManifestPath -Raw | ConvertFrom-Json
        $contract = Get-ObjectPropertyValue -Object $layerManifest -Name "runtimeContract"
        $content = Get-ObjectPropertyValue -Object $layerManifest -Name "content"
        $expectedFiles = @((Get-ObjectPropertyValue -Object $content -Name "files"))
        if ($expectedFiles.Count -lt 1 -or $expectedFiles.Count -gt 32768) {
            return $false
        }

        $actualFiles = @()
        foreach ($file in Get-ChildItem -LiteralPath $RuntimeDir -Recurse -File | Sort-Object FullName) {
            $relative = (Get-RelativePath -Root $RuntimeDir -Path $file.FullName) -replace '\\', '/'
            if ($relative -eq "runtime-layer-manifest.json") {
                continue
            }
            if (-not $PureRuntime) {
                if (
                    $relative.StartsWith("app/", [System.StringComparison]::Ordinal) -or
                    $relative.StartsWith("tools/", [System.StringComparison]::Ordinal) -or
                    $relative -eq "sidecar-build-metadata.json"
                ) {
                    continue
                }
            }
            $actualFiles += [ordered]@{
                path = $relative
                length = [int64]$file.Length
                sha256 = Get-Sha256Hex -Path $file.FullName
            }
        }
        if ($expectedFiles.Count -ne $actualFiles.Count) {
            return $false
        }

        $actualByPath = @{}
        foreach ($entry in $actualFiles) {
            $actualByPath[[string]$entry.path] = $entry
        }
        $seen = @{}
        foreach ($expected in $expectedFiles) {
            $relative = [string](Get-ObjectPropertyValue -Object $expected -Name "path")
            $expectedLength = Get-ObjectPropertyValue -Object $expected -Name "length"
            $expectedSha256 = [string](Get-ObjectPropertyValue -Object $expected -Name "sha256")
            if (
                -not $relative -or
                $relative.Contains("\") -or
                $relative.StartsWith("/") -or
                $relative -match '(^|/)\.\.($|/)' -or
                $seen.ContainsKey($relative) -or
                $null -eq $expectedLength -or
                [int64]$expectedLength -lt 0 -or
                $expectedSha256 -notmatch '^[0-9a-f]{64}$'
            ) {
                return $false
            }
            $seen[$relative] = $true
            $actual = $actualByPath[$relative]
            if (
                $null -eq $actual -or
                [int64]$expectedLength -ne [int64]$actual.length -or
                $expectedSha256 -ne [string]$actual.sha256
            ) {
                return $false
            }
        }

        $identity = Get-ObjectPropertyValue -Object $layerManifest -Name "executable"
        return (
            [int](Get-ObjectPropertyValue -Object $layerManifest -Name "schemaVersion") -eq 1 -and
            [string](Get-ObjectPropertyValue -Object $layerManifest -Name "name") -eq "scriber-backend-runtime-layer" -and
            [string](Get-ObjectPropertyValue -Object $layerManifest -Name "cacheKey") -eq $ExpectedCacheKey -and
            [string](Get-ObjectPropertyValue -Object $contract -Name "name") -eq "scriber-frozen-python-runtime" -and
            [int](Get-ObjectPropertyValue -Object $contract -Name "revision") -eq $script:BackendRuntimeContractRevision -and
            [int](Get-ObjectPropertyValue -Object $content -Name "fileCount") -eq $actualFiles.Count -and
            [string](Get-ObjectPropertyValue -Object $content -Name "treeSha256") -eq (Get-FileIdentityTreeSha256 -Entries $actualFiles) -and
            [string](Get-ObjectPropertyValue -Object $identity -Name "sha256") -eq (Get-Sha256Hex -Path $runtimeExe) -and
            [int64](Get-ObjectPropertyValue -Object $identity -Name "length") -eq [int64](Get-Item -LiteralPath $runtimeExe).Length
        )
    } catch {
        return $false
    }
}

function Test-BackendRuntimeCache {
    param(
        [string]$CacheRoot,
        [string]$ExpectedCacheKey
    )

    $runtimeDir = Join-Path $CacheRoot "scriber-backend"
    $runtimeExe = Join-Path $runtimeDir "scriber-backend.exe"
    if (-not (Test-Path -LiteralPath $runtimeExe -PathType Leaf)) {
        $runtimeExe = Join-Path $runtimeDir "scriber-backend"
    }
    $cacheManifestPath = Join-Path $CacheRoot "runtime-cache-manifest.json"
    $layerManifestPath = Join-Path $runtimeDir "runtime-layer-manifest.json"
    if (
        -not (Test-Path -LiteralPath $runtimeExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $cacheManifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $layerManifestPath -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $runtimeDir "app"))
    ) {
        return $false
    }
    if (-not (Test-BackendRuntimeLayer -RuntimeDir $runtimeDir -ExpectedCacheKey $ExpectedCacheKey -PureRuntime)) {
        return $false
    }
    try {
        $cacheManifest = Get-Content -LiteralPath $cacheManifestPath -Raw | ConvertFrom-Json
        $layerManifest = Get-Content -LiteralPath $layerManifestPath -Raw | ConvertFrom-Json
        $contract = Get-ObjectPropertyValue -Object $layerManifest -Name "runtimeContract"
        $content = Get-ObjectPropertyValue -Object $layerManifest -Name "content"
        $runtimeInputJson = (Get-ObjectPropertyValue -Object $cacheManifest -Name "inputManifest") | ConvertTo-Json -Depth 10 -Compress
        if ($runtimeInputJson -match '"path":"src[\\/]') {
            return $false
        }
        $expectedRuntimeFiles = @((Get-ObjectPropertyValue -Object $cacheManifest -Name "runtimeFiles"))
        if (-not (Test-BackendStableMediaFiles -CacheRoot $CacheRoot -ExpectedFiles @($cacheManifest.stableMediaFiles))) {
            return $false
        }
        $actualRuntimeFiles = @(Get-BackendRuntimeFileIdentityEntries -RuntimeDir $runtimeDir)
        if ($expectedRuntimeFiles.Count -ne $actualRuntimeFiles.Count) {
            return $false
        }
        $actualByPath = @{}
        foreach ($entry in $actualRuntimeFiles) {
            $actualByPath[[string]$entry.path] = $entry
        }
        foreach ($expected in $expectedRuntimeFiles) {
            $relative = [string](Get-ObjectPropertyValue -Object $expected -Name "path")
            if (-not $relative -or $relative.Contains("\") -or $relative.StartsWith("/") -or $relative -match '(^|/)\.\.($|/)') {
                return $false
            }
            $actual = $actualByPath[$relative]
            if (
                $null -eq $actual -or
                [int64](Get-ObjectPropertyValue -Object $expected -Name "length") -ne [int64]$actual.length -or
                [string](Get-ObjectPropertyValue -Object $expected -Name "sha256") -ne [string]$actual.sha256
            ) {
                return $false
            }
        }
        $actualTreeSha256 = Get-FileIdentityTreeSha256 -Entries $actualRuntimeFiles
        $executableIdentity = Get-ObjectPropertyValue -Object $layerManifest -Name "executable"
        return (
            [int](Get-ObjectPropertyValue -Object $cacheManifest -Name "apiVersion") -eq 1 -and
            [string](Get-ObjectPropertyValue -Object $cacheManifest -Name "cacheKey") -eq $ExpectedCacheKey -and
            [int](Get-ObjectPropertyValue -Object $layerManifest -Name "schemaVersion") -eq 1 -and
            [string](Get-ObjectPropertyValue -Object $layerManifest -Name "name") -eq "scriber-backend-runtime-layer" -and
            [string](Get-ObjectPropertyValue -Object $layerManifest -Name "cacheKey") -eq $ExpectedCacheKey -and
            [string](Get-ObjectPropertyValue -Object $contract -Name "name") -eq "scriber-frozen-python-runtime" -and
            [int](Get-ObjectPropertyValue -Object $contract -Name "revision") -eq $script:BackendRuntimeContractRevision -and
            [int](Get-ObjectPropertyValue -Object $content -Name "fileCount") -eq $actualRuntimeFiles.Count -and
            [string](Get-ObjectPropertyValue -Object $content -Name "treeSha256") -eq $actualTreeSha256 -and
            [string](Get-ObjectPropertyValue -Object $cacheManifest -Name "sidecarSha256") -eq (Get-Sha256Hex -Path $runtimeExe) -and
            [int64](Get-ObjectPropertyValue -Object $cacheManifest -Name "sidecarLength") -eq [int64](Get-Item -LiteralPath $runtimeExe).Length -and
            [string](Get-ObjectPropertyValue -Object $executableIdentity -Name "sha256") -eq (Get-Sha256Hex -Path $runtimeExe) -and
            [int64](Get-ObjectPropertyValue -Object $executableIdentity -Name "length") -eq [int64](Get-Item -LiteralPath $runtimeExe).Length
        )
    } catch {
        return $false
    }
}

function Write-BackendRuntimeCacheMetadata {
    param(
        [string]$RuntimeDir,
        [string]$RuntimeExe,
        [string]$CacheRoot,
        [string]$CacheKey,
        [object]$InputManifest,
        [string]$Python
    )

    $identity = [ordered]@{
        sha256 = Get-Sha256Hex -Path $RuntimeExe
        length = [int64](Get-Item -LiteralPath $RuntimeExe).Length
    }
    $pythonIdentity = (& $Python -c "import json,sys; print(json.dumps({'version':sys.version,'cacheTag':sys.implementation.cache_tag},separators=(',',':')))" 2>$null) -join "`n"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to resolve Python runtime identity."
    }
    $runtimeFiles = @(Get-BackendRuntimeFileIdentityEntries -RuntimeDir $RuntimeDir)
    $layerManifest = [ordered]@{
        schemaVersion = 1
        name = "scriber-backend-runtime-layer"
        cacheKey = $CacheKey
        runtimeContract = [ordered]@{
            name = "scriber-frozen-python-runtime"
            revision = $script:BackendRuntimeContractRevision
        }
        python = ($pythonIdentity | ConvertFrom-Json)
        executable = $identity
        content = [ordered]@{
            fileCount = $runtimeFiles.Count
            treeSha256 = Get-FileIdentityTreeSha256 -Entries $runtimeFiles
            files = $runtimeFiles
        }
    }
    $layerManifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $RuntimeDir "runtime-layer-manifest.json") -Encoding utf8

    New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null
    $cacheManifest = [ordered]@{
        apiVersion = 1
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        cacheKey = $CacheKey
        sidecarSha256 = $identity["sha256"]
        sidecarLength = $identity["length"]
        inputManifest = $InputManifest
        runtimeFiles = $runtimeFiles
        stableMediaFiles = @(Get-BackendStableMediaFileIdentityEntries -CacheRoot $CacheRoot)
    }
    $cacheManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $CacheRoot "runtime-cache-manifest.json") -Encoding utf8
}

function Invoke-StageBackendApplicationLayer {
    param(
        [string]$Root,
        [string]$Python,
        [string]$BackendDir,
        [string]$RuntimeCacheKey,
        [string]$LogRoot
    )

    $scriptPath = Join-Path $Root "scripts\stage_backend_application_layer.py"
    $resultPath = Join-Path $LogRoot "application-layer-stage.json"
    & $Python $scriptPath `
        --repo-root $Root `
        --backend-root $BackendDir `
        --runtime-cache-key $RuntimeCacheKey `
        --output $resultPath
    if ($LASTEXITCODE -ne 0) {
        throw "Staging the Scriber backend application layer failed."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $BackendDir "app\app-layer-manifest.json") -PathType Leaf)) {
        throw "Staging the Scriber backend application layer did not produce its manifest."
    }
}

function Invoke-ValidateBackendApplicationLayer {
    param(
        [string]$Root,
        [string]$Python,
        [string]$BackendDir,
        [string]$RuntimeCacheKey,
        [string]$LogRoot
    )

    $scriptPath = Join-Path $Root "scripts\stage_backend_application_layer.py"
    $resultPath = Join-Path $LogRoot "application-layer-validation.json"
    & $Python $scriptPath `
        --backend-root $BackendDir `
        --runtime-cache-key $RuntimeCacheKey `
        --validate-only `
        --output $resultPath
    if ($LASTEXITCODE -ne 0) {
        throw "The Scriber backend application layer changed during its frozen import check."
    }
}

function Copy-DirectoryContents {
    param(
        [string]$SourceDir,
        [string]$TargetDir,
        [string]$TargetLabel
    )

    Sync-DirectoryContents -SourceDir $SourceDir -TargetDir $TargetDir -TargetLabel $TargetLabel | Out-Null
}

function Test-FileContentEqual {
    param(
        [string]$SourcePath,
        [string]$TargetPath
    )

    if (-not (Test-Path -LiteralPath $TargetPath -PathType Leaf)) {
        return $false
    }

    $source = Get-Item -LiteralPath $SourcePath
    $target = Get-Item -LiteralPath $TargetPath
    if ($source.Length -ne $target.Length) {
        return $false
    }
    if ($source.LastWriteTimeUtc -eq $target.LastWriteTimeUtc) {
        return $true
    }

    $sourceHash = Get-Sha256Hex -Path $source.FullName
    $targetHash = Get-Sha256Hex -Path $target.FullName
    return $sourceHash -eq $targetHash
}

function Copy-FileIfChanged {
    param(
        [string]$SourcePath,
        [string]$TargetPath
    )

    if (Test-FileContentEqual -SourcePath $SourcePath -TargetPath $TargetPath) {
        $source = Get-Item -LiteralPath $SourcePath
        $target = Get-Item -LiteralPath $TargetPath
        if ($source.LastWriteTimeUtc -ne $target.LastWriteTimeUtc) {
            [System.IO.File]::SetLastWriteTimeUtc($target.FullName, $source.LastWriteTimeUtc)
        }
        return $false
    }

    $targetParent = Split-Path -Parent $TargetPath
    New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
    Copy-Item -LiteralPath $SourcePath -Destination $TargetPath -Force
    $source = Get-Item -LiteralPath $SourcePath
    [System.IO.File]::SetLastWriteTimeUtc($TargetPath, $source.LastWriteTimeUtc)
    return $true
}

function Sync-DirectoryContents {
    param(
        [string]$SourceDir,
        [string]$TargetDir,
        [string]$TargetLabel
    )

    Assert-UnderRoot -Root $RepoRoot -Path $TargetDir -Label $TargetLabel
    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
        throw "Source directory was not found for ${TargetLabel}: ${SourceDir}"
    }

    $sourceRoot = Convert-ToFullPath -Path $SourceDir
    $targetRoot = Convert-ToFullPath -Path $TargetDir
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

    $sourceRelativePaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $copied = 0
    $skipped = 0
    foreach ($sourceFile in Get-ChildItem -LiteralPath $sourceRoot -Recurse -File) {
        $relative = Get-RelativePath -Root $sourceRoot -Path $sourceFile.FullName
        $sourceRelativePaths.Add($relative) | Out-Null
        $targetPath = Join-Path $targetRoot $relative
        Assert-UnderRoot -Root $targetRoot -Path $targetPath -Label "$TargetLabel file target"
        if (Copy-FileIfChanged -SourcePath $sourceFile.FullName -TargetPath $targetPath) {
            $copied += 1
        } else {
            $skipped += 1
        }
    }

    $removed = 0
    foreach ($targetFile in Get-ChildItem -LiteralPath $targetRoot -Recurse -File -ErrorAction SilentlyContinue) {
        $relative = Get-RelativePath -Root $targetRoot -Path $targetFile.FullName
        if (-not $sourceRelativePaths.Contains($relative)) {
            Assert-UnderRoot -Root $targetRoot -Path $targetFile.FullName -Label "$TargetLabel stale file"
            Remove-Item -LiteralPath $targetFile.FullName -Force
            $removed += 1
        }
    }

    foreach ($targetDirectory in Get-ChildItem -LiteralPath $targetRoot -Recurse -Directory -ErrorAction SilentlyContinue | Sort-Object FullName -Descending) {
        if (-not (Get-ChildItem -LiteralPath $targetDirectory.FullName -Force -ErrorAction SilentlyContinue)) {
            Assert-UnderRoot -Root $targetRoot -Path $targetDirectory.FullName -Label "$TargetLabel stale directory"
            Remove-Item -LiteralPath $targetDirectory.FullName -Force
        }
    }

    return [ordered]@{
        copied = $copied
        skipped = $skipped
        removed = $removed
    }
}

function Get-RustAudioSidecarInputManifest {
    param([string]$Root)

    $relativePaths = @(
        "Frontend\src-tauri\build.rs",
        "Frontend\src-tauri\src\audio_sidecar.rs",
        "Frontend\src-tauri\src\audio_frame_pipe.rs",
        "Frontend\src-tauri\src\meeting_aec.rs",
        "Frontend\src-tauri\src\redaction.rs"
    )
    $knownPaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($relative in $relativePaths) {
        $knownPaths.Add($relative) | Out-Null
    }

    $audioSidecarPath = Join-Path $Root "Frontend\src-tauri\src\audio_sidecar.rs"
    if (Test-Path -LiteralPath $audioSidecarPath -PathType Leaf) {
        $modulePattern = '^\s*mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;'
        foreach ($line in Get-Content -LiteralPath $audioSidecarPath) {
            $match = [regex]::Match($line, $modulePattern)
            if ($match.Success) {
                $moduleRelativePath = "Frontend\src-tauri\src\$($match.Groups[1].Value).rs"
                if (-not $knownPaths.Contains($moduleRelativePath)) {
                    throw "Rust audio sidecar cache manifest is missing module dependency: $moduleRelativePath"
                }
            }
        }
    }

    $entries = @()
    $entries += Get-NormalizedFileHashEntry -Root $Root -RelativePath "Frontend\src-tauri\Cargo.toml" -Normalizer ${function:Normalize-CargoTomlForCache}
    $entries += Get-NormalizedFileHashEntry -Root $Root -RelativePath "Frontend\src-tauri\Cargo.lock" -Normalizer ${function:Normalize-CargoLockForCache}
    $entries += Get-InputFileEntries -Root $Root -RelativePaths $relativePaths

    return [ordered]@{
        apiVersion = "1"
        files = $entries
    }
}

function Get-RustAudioSidecarCacheKey {
    param([string]$Root)

    $inputManifest = Get-RustAudioSidecarInputManifest -Root $Root
    $inputManifestJson = $inputManifest | ConvertTo-Json -Depth 8 -Compress
    return Get-StringSha256 -Value $inputManifestJson
}

function Get-RustAudioSidecarCacheValidation {
    param(
        [string]$Root,
        [string]$CacheRoot,
        [switch]$RunSelfTest
    )

    $exeName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "scriber-audio-sidecar.exe" } else { "scriber-audio-sidecar" }
    $cacheExe = Join-Path $CacheRoot $exeName
    $cacheManifestPath = Join-Path $CacheRoot "audio-sidecar-cache-manifest.json"
    $cacheKey = Get-RustAudioSidecarCacheKey -Root $Root
    $result = [ordered]@{
        usable = $false
        cacheKey = $cacheKey
        reason = "missing-files"
    }

    if (
        -not (Test-Path -LiteralPath $cacheExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $cacheManifestPath -PathType Leaf)
    ) {
        return [pscustomobject]$result
    }

    try {
        $cacheManifest = Get-Content -LiteralPath $cacheManifestPath -Raw | ConvertFrom-Json
        $identityMatches = (
            ([string]$cacheManifest.cacheKey) -eq $cacheKey -and
            [string]$cacheManifest.executableSha256 -eq (Get-Sha256Hex -Path $cacheExe) -and
            [int64]$cacheManifest.executableLength -eq [int64](Get-Item -LiteralPath $cacheExe).Length
        )
        if (-not $identityMatches) {
            $result.reason = "identity-mismatch"
            return [pscustomobject]$result
        }
        if ($RunSelfTest) {
            & $cacheExe --self-test | Out-Null
            if ($LASTEXITCODE -ne 0) {
                $result.reason = "self-test-failed"
                return [pscustomobject]$result
            }
        }
        $result.usable = $true
        $result.reason = "validated"
    } catch {
        $result.reason = "validation-error"
    }
    return [pscustomobject]$result
}

function Copy-RustAudioSidecarToTauriRelease {
    param(
        [string]$Root,
        [bool]$UseIsolatedTarget = $false
    )

    $tauriDir = Join-Path $Root "Frontend\src-tauri"
    $targetDir = Join-Path $tauriDir "target\release"
    $staleResourceDir = Join-Path $tauriDir "resources\audio-sidecar"
    $staleTargetResourceDir = Join-Path $targetDir "audio-sidecar"
    $cacheRoot = Join-Path $Root "build\rust-audio-sidecar-cache"
    $cargoTargetDir = if ($UseIsolatedTarget) { Join-Path $Root "build\rust-audio-sidecar-target" } else { Join-Path $tauriDir "target" }
    Assert-UnderRoot -Root $Root -Path $targetDir -Label "Tauri release audio sidecar target"
    Assert-UnderRoot -Root $Root -Path $staleResourceDir -Label "Stale audio sidecar resource"
    Assert-UnderRoot -Root $Root -Path $staleTargetResourceDir -Label "Stale packaged audio sidecar resource"
    Assert-UnderRoot -Root $Root -Path $cacheRoot -Label "Rust audio sidecar cache"
    Assert-UnderRoot -Root $Root -Path $cargoTargetDir -Label "Rust audio sidecar cargo target"

    $exeName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "scriber-audio-sidecar.exe" } else { "scriber-audio-sidecar" }
    $cacheExe = Join-Path $cacheRoot $exeName
    $cacheManifestPath = Join-Path $cacheRoot "audio-sidecar-cache-manifest.json"
    $metadataPath = Join-Path $cacheRoot "audio-sidecar-build-metadata.json"
    $inputManifest = Get-RustAudioSidecarInputManifest -Root $Root
    $inputManifestJson = $inputManifest | ConvertTo-Json -Depth 8 -Compress
    $cacheKey = Get-StringSha256 -Value $inputManifestJson
    $cacheValidation = Get-RustAudioSidecarCacheValidation -Root $Root -CacheRoot $cacheRoot
    $cacheHit = [bool]$cacheValidation.usable

    if (-not $cacheHit) {
        Push-Location $tauriDir
        try {
            cargo build --release --bin scriber-audio-sidecar --target-dir $cargoTargetDir
        } finally {
            Pop-Location
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Rust audio sidecar build failed."
        }

        $sourceExe = Join-Path $cargoTargetDir "release\$exeName"
        if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) {
            throw "Rust audio sidecar executable was not found: $sourceExe"
        }

        New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null
        Copy-FileIfChanged -SourcePath $sourceExe -TargetPath $cacheExe | Out-Null
        $cacheManifestPayload = [ordered]@{
            apiVersion = "1"
            cacheKey = $cacheKey
            executableSha256 = Get-Sha256Hex -Path $cacheExe
            executableLength = [int64](Get-Item -LiteralPath $cacheExe).Length
            inputManifest = $inputManifest
        }
        $cacheManifestPayload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $cacheManifestPath -Encoding utf8
    }

    if (-not (Test-Path -LiteralPath $cacheExe -PathType Leaf)) {
        throw "Rust audio sidecar cache executable was not found: $cacheExe"
    }

    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    $targetExe = Join-Path $targetDir $exeName
    $targetCopied = Copy-FileIfChanged -SourcePath $cacheExe -TargetPath $targetExe
    & $targetExe --self-test | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Rust audio sidecar self-test failed for packaged executable."
    }

    if (Test-Path -LiteralPath $staleResourceDir -PathType Container) {
        foreach ($staleItem in Get-ChildItem -LiteralPath $staleResourceDir -Force -ErrorAction SilentlyContinue) {
            if ($staleItem.Name -ne ".gitkeep") {
                Assert-UnderRoot -Root $staleResourceDir -Path $staleItem.FullName -Label "Stale audio sidecar resource"
                Remove-Item -LiteralPath $staleItem.FullName -Recurse -Force
            }
        }
        if (-not (Get-ChildItem -LiteralPath $staleResourceDir -Force -ErrorAction SilentlyContinue)) {
            Assert-UnderRoot -Root $Root -Path $staleResourceDir -Label "Empty audio sidecar resource directory"
            Remove-Item -LiteralPath $staleResourceDir -Force
        }
    }

    if (Test-Path -LiteralPath $staleTargetResourceDir -PathType Container) {
        Assert-UnderRoot -Root $targetDir -Path $staleTargetResourceDir -Label "Stale packaged audio sidecar resource"
        Remove-Item -LiteralPath $staleTargetResourceDir -Recurse -Force
    }

    $metadata = [ordered]@{
        apiVersion = "1"
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        cacheHit = $cacheHit
        cacheKey = $cacheKey
        sourceExe = $cacheExe
        targetExe = $targetExe
        cargoTargetDir = $cargoTargetDir
        isolatedCargoTarget = [bool]$UseIsolatedTarget
        sha256 = Get-Sha256Hex -Path $targetExe
        length = [int64](Get-Item -LiteralPath $targetExe).Length
        targetCopied = [bool]$targetCopied
        captureDefault = "disabled"
        optInEnv = "SCRIBER_RUST_AUDIO_WASAPI_CAPTURE"
    }
    $metadata | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $metadataPath -Encoding utf8

    return [ordered]@{
        targetDir = $targetDir
        targetExe = $targetExe
        metadataPath = $metadataPath
        sha256 = $metadata.sha256
        length = $metadata.length
        cacheHit = $cacheHit
        cacheKey = $cacheKey
        isolatedCargoTarget = [bool]$UseIsolatedTarget
    }
}

function Get-RustDiarizationSidecarInputManifest {
    param([string]$Root)

    $relativePaths = @(
        "native\scriber-diarization-sidecar\.cargo\config.toml",
        "native\scriber-diarization-sidecar\Cargo.toml",
        "native\scriber-diarization-sidecar\Cargo.lock",
        "native\scriber-diarization-sidecar\build.rs",
        "native\scriber-diarization-sidecar\src",
        "scripts\write_diarization_worker_manifest.py"
    )
    return [ordered]@{
        apiVersion = "1"
        target = "x86_64-pc-windows-msvc"
        cacheContract = "static-sherpa-worker-v1"
        sherpaOnnx = [ordered]@{
            version = "1.13.3"
            archiveName = "sherpa-onnx-v1.13.3-win-x64-static-MT-Release-lib.tar.bz2"
            archiveSha256 = "f6555701d6397d74f1302b0666a661f32708b599a14a5fde80835d4902fcd315"
        }
        files = Get-InputFileEntries -Root $Root -RelativePaths $relativePaths
    }
}

function Get-RustDiarizationSidecarCacheKey {
    param([string]$Root)

    $inputManifest = Get-RustDiarizationSidecarInputManifest -Root $Root
    $inputManifestJson = $inputManifest | ConvertTo-Json -Depth 8 -Compress
    return Get-StringSha256 -Value $inputManifestJson
}

function Get-RustDiarizationSidecarCacheValidation {
    param(
        [string]$Root,
        [string]$Python,
        [string]$WorkerCacheRoot,
        [switch]$RunSmoke
    )

    $cacheResourceRoot = Join-Path $WorkerCacheRoot "backend"
    $cacheResourceDir = Join-Path $cacheResourceRoot "tools\diarization"
    $cacheExe = Join-Path $cacheResourceDir "scriber-diarization-sidecar.exe"
    $cacheWorkerManifest = Join-Path $cacheResourceDir "scriber-diarization-sidecar.manifest.json"
    $cacheManifestPath = Join-Path $WorkerCacheRoot "diarization-sidecar-cache-manifest.json"
    $buildMetadataPath = Join-Path $WorkerCacheRoot "diarization-sidecar-build-metadata.json"
    $cacheKey = Get-RustDiarizationSidecarCacheKey -Root $Root
    $result = [ordered]@{
        usable = $false
        cacheKey = $cacheKey
        reason = "missing-files"
        buildMetadata = $null
    }

    if (
        -not (Test-Path -LiteralPath $cacheExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $cacheWorkerManifest -PathType Leaf) -or
        -not (Test-Path -LiteralPath $cacheManifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $buildMetadataPath -PathType Leaf)
    ) {
        return [pscustomobject]$result
    }

    try {
        $cacheManifest = Get-Content -LiteralPath $cacheManifestPath -Raw | ConvertFrom-Json
        $cachedBuildMetadata = Get-Content -LiteralPath $buildMetadataPath -Raw | ConvertFrom-Json
        $cachedArchive = Get-ObjectPropertyValue -Object $cachedBuildMetadata -Name "archive"
        $identityMatches = (
            ([string]$cacheManifest.cacheKey) -eq $cacheKey -and
            ([string]$cachedBuildMetadata.cacheKey) -eq $cacheKey -and
            ([string](Get-ObjectPropertyValue -Object $cachedArchive -Name "sha256")) -eq "f6555701d6397d74f1302b0666a661f32708b599a14a5fde80835d4902fcd315"
        )
        if (-not $identityMatches) {
            $result.reason = "identity-mismatch"
            return [pscustomobject]$result
        }
        if ($RunSmoke) {
            Invoke-DiarizationWorkerResourceSmoke -Root $Root -Python $Python -ResourceRoot $cacheResourceRoot | Out-Null
        }
        $result.usable = $true
        $result.reason = "validated"
        $result.buildMetadata = $cachedBuildMetadata
    } catch {
        $result.reason = "validation-error"
    }
    return [pscustomobject]$result
}

function Invoke-BoundedDownload {
    param(
        [string]$Uri,
        [string]$OutputPath,
        [int64]$MaxBytes
    )

    Add-Type -AssemblyName System.Net.Http
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $true
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [System.TimeSpan]::FromMinutes(10)
    $client.DefaultRequestHeaders.UserAgent.ParseAdd("Scriber-release-builder/1")
    $response = $null
    $inputStream = $null
    $outputStream = $null
    try {
        $response = $client.GetAsync(
            $Uri,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        [void]$response.EnsureSuccessStatusCode()
        $contentLength = $response.Content.Headers.ContentLength
        if ($null -ne $contentLength -and [int64]$contentLength -gt $MaxBytes) {
            throw "Download exceeds the configured size ceiling."
        }
        $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $outputStream = [System.IO.File]::Open(
            $OutputPath,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $buffer = New-Object byte[] (1024 * 1024)
        [int64]$total = 0
        while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $total += $read
            if ($total -gt $MaxBytes) {
                throw "Download exceeds the configured size ceiling."
            }
            $outputStream.Write($buffer, 0, $read)
        }
        $outputStream.Flush($true)
    } finally {
        if ($outputStream) { $outputStream.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Dispose() }
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-SherpaOnnxStaticArchive {
    param(
        [string]$Root,
        [string]$CacheRoot
    )

    $archiveName = "sherpa-onnx-v1.13.3-win-x64-static-MT-Release-lib.tar.bz2"
    $archiveUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.3/$archiveName"
    $expectedSha256 = "f6555701d6397d74f1302b0666a661f32708b599a14a5fde80835d4902fcd315"
    $maxArchiveBytes = 128MB
    Assert-UnderRoot -Root $Root -Path $CacheRoot -Label "Sherpa-ONNX archive cache"
    New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null
    $archivePath = Join-Path $CacheRoot $archiveName
    $cacheHit = $false

    if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
        $archiveItem = Get-Item -LiteralPath $archivePath
        if ($archiveItem.Length -le $maxArchiveBytes -and (Get-Sha256Hex -Path $archivePath) -eq $expectedSha256) {
            $cacheHit = $true
        } else {
            Assert-UnderRoot -Root $CacheRoot -Path $archivePath -Label "Invalid Sherpa-ONNX archive cache file"
            Remove-Item -LiteralPath $archivePath -Force
        }
    }

    if (-not $cacheHit) {
        $partialPath = Join-Path $CacheRoot ("$archiveName.part-" + $PID)
        Assert-UnderRoot -Root $CacheRoot -Path $partialPath -Label "Sherpa-ONNX partial archive"
        try {
            if (Test-Path -LiteralPath $partialPath -PathType Leaf) {
                Remove-Item -LiteralPath $partialPath -Force
            }
            Write-Host "Downloading pinned Sherpa-ONNX 1.13.3 static archive."
            Invoke-BoundedDownload -Uri $archiveUrl -OutputPath $partialPath -MaxBytes $maxArchiveBytes
            $partialItem = Get-Item -LiteralPath $partialPath
            if ($partialItem.Length -gt $maxArchiveBytes) {
                throw "Sherpa-ONNX static archive exceeds the pinned size ceiling."
            }
            $actualSha256 = Get-Sha256Hex -Path $partialPath
            if ($actualSha256 -ne $expectedSha256) {
                throw "Sherpa-ONNX static archive checksum mismatch."
            }
            Move-Item -LiteralPath $partialPath -Destination $archivePath
        } finally {
            if (Test-Path -LiteralPath $partialPath -PathType Leaf) {
                Remove-Item -LiteralPath $partialPath -Force
            }
        }
    }

    $archiveItem = Get-Item -LiteralPath $archivePath
    $archiveManifest = [ordered]@{
        apiVersion = "1"
        source = $archiveUrl
        fileName = $archiveName
        sha256 = $expectedSha256
        length = [int64]$archiveItem.Length
    }
    $archiveManifestPath = Join-Path $CacheRoot "sherpa-onnx-archive-manifest.json"
    $archiveManifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $archiveManifestPath -Encoding utf8
    return [ordered]@{
        cacheHit = $cacheHit
        cacheRoot = $CacheRoot
        archivePath = $archivePath
        manifestPath = $archiveManifestPath
        source = $archiveUrl
        fileName = $archiveName
        sha256 = $expectedSha256
        length = [int64]$archiveItem.Length
    }
}

function Invoke-DiarizationWorkerResourceSmoke {
    param(
        [string]$Root,
        [string]$Python,
        [string]$ResourceRoot
    )

    $smokeScript = Join-Path $Root "scripts\smoke_diarization_worker_resource.py"
    if (-not (Test-Path -LiteralPath $smokeScript -PathType Leaf)) {
        throw "Missing diarization worker resource smoke: $smokeScript"
    }
    $smokeJson = & $Python $smokeScript --root $ResourceRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Diarization worker resource smoke failed."
    }
    try {
        $smoke = ($smokeJson | Out-String).Trim() | ConvertFrom-Json
    } catch {
        throw "Diarization worker resource smoke returned invalid JSON."
    }
    if (-not $smoke.ok) {
        throw "Diarization worker resource smoke did not report ok=true."
    }
    return $smoke
}

function Copy-RustDiarizationSidecarToBackend {
    param(
        [string]$Root,
        [string]$Python,
        [string]$BackendDir,
        [string]$WorkerCacheRoot,
        [string]$ArchiveCacheRoot,
        [string]$CargoTargetRoot
    )

    if (-not ($IsWindows -or $env:OS -eq "Windows_NT")) {
        throw "The release diarization worker currently supports Windows x64 only."
    }
    Assert-UnderRoot -Root $Root -Path $BackendDir -Label "Diarization backend resource target"
    Assert-UnderRoot -Root $Root -Path $WorkerCacheRoot -Label "Rust diarization sidecar cache"
    Assert-UnderRoot -Root $Root -Path $ArchiveCacheRoot -Label "Sherpa-ONNX archive cache"
    Assert-UnderRoot -Root $Root -Path $CargoTargetRoot -Label "Rust diarization cargo target"

    $crateDir = Join-Path $Root "native\scriber-diarization-sidecar"
    $manifestWriter = Join-Path $Root "scripts\write_diarization_worker_manifest.py"
    $exeName = "scriber-diarization-sidecar.exe"
    $workerManifestName = "scriber-diarization-sidecar.manifest.json"
    $cacheResourceRoot = Join-Path $WorkerCacheRoot "backend"
    $cacheResourceDir = Join-Path $cacheResourceRoot "tools\diarization"
    $cacheExe = Join-Path $cacheResourceDir $exeName
    $cacheWorkerManifest = Join-Path $cacheResourceDir $workerManifestName
    $cacheManifestPath = Join-Path $WorkerCacheRoot "diarization-sidecar-cache-manifest.json"
    $buildMetadataPath = Join-Path $WorkerCacheRoot "diarization-sidecar-build-metadata.json"
    $inputManifest = Get-RustDiarizationSidecarInputManifest -Root $Root
    $inputManifestJson = $inputManifest | ConvertTo-Json -Depth 8 -Compress
    $cacheKey = Get-StringSha256 -Value $inputManifestJson
    $cacheValidation = Get-RustDiarizationSidecarCacheValidation `
        -Root $Root `
        -Python $Python `
        -WorkerCacheRoot $WorkerCacheRoot `
        -RunSmoke
    $cacheHit = [bool]$cacheValidation.usable
    $cachedBuildMetadata = $cacheValidation.buildMetadata
    if (-not $cacheHit -and $cacheValidation.reason -ne "missing-files") {
        Write-Host "Ignoring invalid Rust diarization sidecar cache."
    }

    $archive = $null
    $rustcVersion = $null
    $cargoVersion = $null
    if (-not $cacheHit) {
        $archive = Get-SherpaOnnxStaticArchive -Root $Root -CacheRoot $ArchiveCacheRoot
        $rustcVersion = (& rustc -Vv 2>$null) -join "`n"
        if ($LASTEXITCODE -ne 0 -or -not $rustcVersion) {
            throw "rustc is required to build the diarization worker."
        }
        $cargoVersion = (& cargo -V 2>$null) -join "`n"
        if ($LASTEXITCODE -ne 0 -or -not $cargoVersion) {
            throw "cargo is required to build the diarization worker."
        }

        $oldArchiveDir = $env:SHERPA_ONNX_ARCHIVE_DIR
        $env:SHERPA_ONNX_ARCHIVE_DIR = $ArchiveCacheRoot
        Push-Location $crateDir
        try {
            & cargo build --release --locked --target-dir $CargoTargetRoot
            $cargoExitCode = $LASTEXITCODE
        } finally {
            Pop-Location
            if ($null -eq $oldArchiveDir) {
                Remove-Item Env:SHERPA_ONNX_ARCHIVE_DIR -ErrorAction SilentlyContinue
            } else {
                $env:SHERPA_ONNX_ARCHIVE_DIR = $oldArchiveDir
            }
        }
        if ($cargoExitCode -ne 0) {
            throw "Rust diarization sidecar build failed with exit code $cargoExitCode."
        }

        $sourceExe = Join-Path $CargoTargetRoot "release\$exeName"
        if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) {
            throw "Rust diarization sidecar executable was not found: $sourceExe"
        }
        New-Item -ItemType Directory -Force -Path $cacheResourceDir | Out-Null
        Copy-FileIfChanged -SourcePath $sourceExe -TargetPath $cacheExe | Out-Null
        & $Python $manifestWriter --executable $cacheExe --output $cacheWorkerManifest
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $cacheWorkerManifest -PathType Leaf)) {
            throw "Diarization worker build attestation generation failed."
        }
        foreach ($staleItem in Get-ChildItem -LiteralPath $cacheResourceDir -Force -ErrorAction SilentlyContinue) {
            if ($staleItem.Name -notin @($exeName, $workerManifestName)) {
                Assert-UnderRoot -Root $cacheResourceDir -Path $staleItem.FullName -Label "Stale diarization cache resource"
                Remove-Item -LiteralPath $staleItem.FullName -Recurse -Force
            }
        }
        $cacheSmoke = Invoke-DiarizationWorkerResourceSmoke -Root $Root -Python $Python -ResourceRoot $cacheResourceRoot
        $cacheManifestPayload = [ordered]@{
            apiVersion = "1"
            generatedAt = (Get-Date).ToUniversalTime().ToString("o")
            cacheKey = $cacheKey
            inputManifest = $inputManifest
        }
        $cacheManifestPayload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $cacheManifestPath -Encoding utf8
        $cachedBuildMetadata = [ordered]@{
            apiVersion = "1"
            generatedAt = (Get-Date).ToUniversalTime().ToString("o")
            cacheKey = $cacheKey
            rustc = $rustcVersion
            cargo = $cargoVersion
            archive = [ordered]@{
                source = $archive.source
                fileName = $archive.fileName
                sha256 = $archive.sha256
                length = $archive.length
                cacheHitDuringWorkerBuild = [bool]$archive.cacheHit
            }
            worker = [ordered]@{
                sha256 = Get-Sha256Hex -Path $cacheExe
                length = [int64](Get-Item -LiteralPath $cacheExe).Length
            }
            smoke = $cacheSmoke
        }
        $cachedBuildMetadata | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $buildMetadataPath -Encoding utf8
    }

    $targetResourceDir = Join-Path $BackendDir "tools\diarization"
    $sync = Sync-DirectoryContents -SourceDir $cacheResourceDir -TargetDir $targetResourceDir -TargetLabel "Diarization backend resource target"
    $targetExe = Join-Path $targetResourceDir $exeName
    $targetManifest = Join-Path $targetResourceDir $workerManifestName
    $targetSmoke = Invoke-DiarizationWorkerResourceSmoke -Root $Root -Python $Python -ResourceRoot $BackendDir
    if ($null -eq $cachedBuildMetadata -and (Test-Path -LiteralPath $buildMetadataPath -PathType Leaf)) {
        $cachedBuildMetadata = Get-Content -LiteralPath $buildMetadataPath -Raw | ConvertFrom-Json
    }
    $archiveBuildInput = if ($archive) { $archive } else { Get-ObjectPropertyValue -Object $cachedBuildMetadata -Name "archive" }

    return [ordered]@{
        targetDir = $targetResourceDir
        targetExe = $targetExe
        targetManifest = $targetManifest
        sha256 = Get-Sha256Hex -Path $targetExe
        length = [int64](Get-Item -LiteralPath $targetExe).Length
        manifestSha256 = Get-Sha256Hex -Path $targetManifest
        manifestLength = [int64](Get-Item -LiteralPath $targetManifest).Length
        cacheHit = $cacheHit
        cacheKey = $cacheKey
        cacheRoot = $WorkerCacheRoot
        archiveCacheRoot = $ArchiveCacheRoot
        archive = [ordered]@{
            source = Get-ObjectPropertyValue -Object $archiveBuildInput -Name "source"
            fileName = Get-ObjectPropertyValue -Object $archiveBuildInput -Name "fileName"
            sha256 = Get-ObjectPropertyValue -Object $archiveBuildInput -Name "sha256"
            length = Get-ObjectPropertyValue -Object $archiveBuildInput -Name "length"
            verifiedDuringWorkerBuild = $true
            requiredForCurrentBuild = -not $cacheHit
            cacheHitForCurrentBuild = if ($archive) { [bool]$archive.cacheHit } else { $null }
        }
        cargoTargetDir = $CargoTargetRoot
        buildMetadataPath = $buildMetadataPath
        sync = $sync
        smoke = $targetSmoke
    }
}

function Test-SidecarTargetCurrent {
    param(
        [string]$TargetDir,
        [string]$ExpectedCacheKey,
        [string]$ExpectedRuntimeCacheKey,
        [string]$ExpectedApplicationLayerKey,
        [object]$ExpectedFlags,
        [string]$ExpectedRustAudioCacheKey,
        [string]$ExpectedRustDiarizationCacheKey
    )

    $metadataPath = Join-Path $TargetDir "sidecar-build-metadata.json"
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
        return $false
    }
    try {
        $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
    } catch {
        return $false
    }

    $cache = Get-ObjectPropertyValue -Object $metadata -Name "cache"
    if ([string](Get-ObjectPropertyValue -Object $cache -Name "key") -ne $ExpectedCacheKey) {
        return $false
    }

    $runtimeLayer = Get-ObjectPropertyValue -Object $metadata -Name "runtimeLayer"
    $applicationLayer = Get-ObjectPropertyValue -Object $metadata -Name "applicationLayer"
    if (
        [string](Get-ObjectPropertyValue -Object $runtimeLayer -Name "cacheKey") -ne $ExpectedRuntimeCacheKey -or
        [string](Get-ObjectPropertyValue -Object $applicationLayer -Name "key") -ne $ExpectedApplicationLayerKey
    ) {
        return $false
    }

    $runtimeManifestPath = Join-Path $TargetDir "runtime-layer-manifest.json"
    $applicationManifestPath = Join-Path $TargetDir "app\app-layer-manifest.json"
    if (
        -not (Test-Path -LiteralPath $runtimeManifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $applicationManifestPath -PathType Leaf)
    ) {
        return $false
    }
    try {
        $runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw | ConvertFrom-Json
        $applicationManifest = Get-Content -LiteralPath $applicationManifestPath -Raw | ConvertFrom-Json
    } catch {
        return $false
    }
    if (
        [string](Get-ObjectPropertyValue -Object $runtimeManifest -Name "cacheKey") -ne $ExpectedRuntimeCacheKey -or
        [string](Get-ObjectPropertyValue -Object $applicationManifest -Name "runtimeCacheKey") -ne $ExpectedRuntimeCacheKey -or
        [string](Get-ObjectPropertyValue -Object $runtimeLayer -Name "manifestSha256") -ne (Get-Sha256Hex -Path $runtimeManifestPath) -or
        [string](Get-ObjectPropertyValue -Object $applicationLayer -Name "manifestSha256") -ne (Get-Sha256Hex -Path $applicationManifestPath)
    ) {
        return $false
    }
    $mediaTools = Get-ObjectPropertyValue -Object $metadata -Name "mediaTools"
    $expectedMediaFiles = @((Get-ObjectPropertyValue -Object $mediaTools -Name "files"))
    if (-not (Test-BackendMediaFiles -SidecarDir $TargetDir -ExpectedFiles $expectedMediaFiles)) {
        return $false
    }
    if (-not (Test-BackendRuntimeLayer -RuntimeDir $TargetDir -ExpectedCacheKey $ExpectedRuntimeCacheKey)) {
        return $false
    }

    $flags = Get-ObjectPropertyValue -Object $metadata -Name "flags"
    foreach ($key in $ExpectedFlags.Keys) {
        if ([bool](Get-ObjectPropertyValue -Object $flags -Name $key) -ne [bool]$ExpectedFlags[$key]) {
            return $false
        }
    }

    $sidecarExe = Join-Path $TargetDir "scriber-backend.exe"
    if (-not (Test-Path -LiteralPath $sidecarExe -PathType Leaf)) {
        $sidecarExe = Join-Path $TargetDir "scriber-backend"
    }
    if (-not (Test-Path -LiteralPath $sidecarExe -PathType Leaf)) {
        return $false
    }
    $sidecarIdentity = Get-ObjectPropertyValue -Object $metadata -Name "sidecar"
    if (
        [string](Get-ObjectPropertyValue -Object $sidecarIdentity -Name "sha256") -ne (Get-Sha256Hex -Path $sidecarExe) -or
        [int64](Get-ObjectPropertyValue -Object $sidecarIdentity -Name "length") -ne [int64](Get-Item -LiteralPath $sidecarExe).Length
    ) {
        return $false
    }
    foreach ($requiredPath in @("_internal\onnxruntime", "_internal\onnxruntime\capi")) {
        if (-not (Test-Path -LiteralPath (Join-Path $TargetDir $requiredPath))) {
            return $false
        }
    }
    if ($BundleMediaTools -or $MediaToolsDir) {
        if (-not (Test-Path -LiteralPath (Join-Path $TargetDir "tools\ffmpeg\ffmpeg.exe") -PathType Leaf)) {
            return $false
        }
        if (-not $SkipBundledFfprobe -and -not (Test-Path -LiteralPath (Join-Path $TargetDir "tools\ffmpeg\ffprobe.exe") -PathType Leaf)) {
            return $false
        }
        if (-not (Test-Path -LiteralPath (Join-Path $TargetDir "tools\ffmpeg\deno.exe") -PathType Leaf)) {
            return $false
        }
        if ($ValidateSlimMediaTools -and -not (Test-Path -LiteralPath (Join-Path $TargetDir "tools\ffmpeg\ffmpeg-profile-manifest.json") -PathType Leaf)) {
            return $false
        }
    }
    if ($BundleRustAudioSidecar) {
        $releaseDir = Split-Path -Parent $TargetDir
        $exeName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "scriber-audio-sidecar.exe" } else { "scriber-audio-sidecar" }
        if (-not (Test-Path -LiteralPath (Join-Path $releaseDir $exeName) -PathType Leaf)) {
            return $false
        }
        $rustAudio = Get-ObjectPropertyValue -Object $metadata -Name "rustAudioSidecarCopied"
        $rustAudioExe = Join-Path $releaseDir $exeName
        if (
            -not $ExpectedRustAudioCacheKey -or
            [string](Get-ObjectPropertyValue -Object $rustAudio -Name "cacheKey") -ne $ExpectedRustAudioCacheKey -or
            [string](Get-ObjectPropertyValue -Object $rustAudio -Name "sha256") -ne (Get-Sha256Hex -Path $rustAudioExe) -or
            [int64](Get-ObjectPropertyValue -Object $rustAudio -Name "length") -ne [int64](Get-Item -LiteralPath $rustAudioExe).Length
        ) {
            return $false
        }
    }
    if ($BundleRustDiarizationSidecar) {
        $diarizationDir = Join-Path $TargetDir "tools\diarization"
        $diarizationExe = Join-Path $diarizationDir "scriber-diarization-sidecar.exe"
        $diarizationManifest = Join-Path $diarizationDir "scriber-diarization-sidecar.manifest.json"
        if (
            -not (Test-Path -LiteralPath $diarizationExe -PathType Leaf) -or
            -not (Test-Path -LiteralPath $diarizationManifest -PathType Leaf)
        ) {
            return $false
        }
        $rustDiarization = Get-ObjectPropertyValue -Object $metadata -Name "rustDiarizationSidecarCopied"
        if (
            -not $ExpectedRustDiarizationCacheKey -or
            [string](Get-ObjectPropertyValue -Object $rustDiarization -Name "cacheKey") -ne $ExpectedRustDiarizationCacheKey -or
            [string](Get-ObjectPropertyValue -Object $rustDiarization -Name "sha256") -ne (Get-Sha256Hex -Path $diarizationExe) -or
            [int64](Get-ObjectPropertyValue -Object $rustDiarization -Name "length") -ne [int64](Get-Item -LiteralPath $diarizationExe).Length -or
            [string](Get-ObjectPropertyValue -Object $rustDiarization -Name "manifestSha256") -ne (Get-Sha256Hex -Path $diarizationManifest)
        ) {
            return $false
        }
    }
    return $true
}

function Write-SidecarBuildMetadata {
    param(
        [string]$SidecarDir,
        [string]$SidecarExe,
        [bool]$CacheEnabled,
        [bool]$CacheHit,
        [string]$CacheKey,
        [bool]$RuntimeCacheHit,
        [string]$RuntimeCacheKey,
        [string]$ApplicationLayerKey,
        [object]$PreparedMediaTools,
        [object[]]$MediaToolsCopied,
        [object]$RustAudioSidecarCopied,
        [object]$RustDiarizationSidecarCopied,
        [string]$CopiedTo,
        [bool]$TargetCurrent = $false
    )

    $runtimeManifestPath = Join-Path $SidecarDir "runtime-layer-manifest.json"
    $applicationManifestPath = Join-Path $SidecarDir "app\app-layer-manifest.json"
    if (
        -not (Test-Path -LiteralPath $runtimeManifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $applicationManifestPath -PathType Leaf)
    ) {
        throw "Layered backend metadata cannot be written because a layer manifest is missing."
    }
    $runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw | ConvertFrom-Json
    $applicationManifest = Get-Content -LiteralPath $applicationManifestPath -Raw | ConvertFrom-Json
    if (
        [string](Get-ObjectPropertyValue -Object $runtimeManifest -Name "cacheKey") -ne $RuntimeCacheKey -or
        [string](Get-ObjectPropertyValue -Object $applicationManifest -Name "runtimeCacheKey") -ne $RuntimeCacheKey
    ) {
        throw "Layered backend metadata does not match the expected frozen runtime identity."
    }

    $script:BuildTimingStarted.Stop()
    $metadata = [ordered]@{
        apiVersion = "1"
        generatedAt = (Get-Date).ToUniversalTime().ToString("o")
        sidecarDir = $SidecarDir
        sidecarExe = $SidecarExe
        sidecar = [ordered]@{
            sha256 = Get-Sha256Hex -Path $SidecarExe
            length = [int64](Get-Item -LiteralPath $SidecarExe).Length
        }
        copiedToTauriRelease = $CopiedTo
        targetCurrent = [bool]$TargetCurrent
        cache = [ordered]@{
            enabled = $CacheEnabled
            hit = $CacheHit
            key = $CacheKey
        }
        runtimeLayer = [ordered]@{
            cacheHit = [bool]$RuntimeCacheHit
            cacheKey = $RuntimeCacheKey
            contract = Get-ObjectPropertyValue -Object $runtimeManifest -Name "runtimeContract"
            manifestSha256 = Get-Sha256Hex -Path $runtimeManifestPath
            manifestLength = [int64](Get-Item -LiteralPath $runtimeManifestPath).Length
        }
        mediaTools = [ordered]@{
            files = @(Get-BackendMediaFileIdentityEntries -SidecarDir $SidecarDir)
        }
        applicationLayer = [ordered]@{
            key = $ApplicationLayerKey
            applicationVersion = Get-ObjectPropertyValue -Object $applicationManifest -Name "applicationVersion"
            runtimeCacheKey = Get-ObjectPropertyValue -Object $applicationManifest -Name "runtimeCacheKey"
            fileCount = @((Get-ObjectPropertyValue -Object $applicationManifest -Name "files")).Count
            manifestSha256 = Get-Sha256Hex -Path $applicationManifestPath
            manifestLength = [int64](Get-Item -LiteralPath $applicationManifestPath).Length
        }
        flags = [ordered]@{
            bundleMediaTools = [bool]$BundleMediaTools
            useProfileBFfmpeg = [bool]$UseProfileBFfmpeg
            useGyanFfmpegEssentials = [bool]$UseGyanFfmpegEssentials
            skipBundledFfprobe = [bool]$SkipBundledFfprobe
            validateSlimMediaTools = [bool]$ValidateSlimMediaTools
            bundleRustAudioSidecar = [bool]$BundleRustAudioSidecar
            bundleRustDiarizationSidecar = [bool]$BundleRustDiarizationSidecar
            pyInstallerClean = -not [bool]$LocalPyInstallerNoClean
            rustAudioIsolatedTarget = [bool]$RustAudioIsolatedTarget
        }
        preparedMediaTools = $PreparedMediaTools
        mediaToolsCopied = $MediaToolsCopied
        rustAudioSidecarCopied = $RustAudioSidecarCopied
        rustDiarizationSidecarCopied = $RustDiarizationSidecarCopied
        totalDurationMs = [int64]$script:BuildTimingStarted.ElapsedMilliseconds
        phases = @($script:BuildTimingPhases)
    }
    $metadataPath = Join-Path $SidecarDir "sidecar-build-metadata.json"
    $metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $metadataPath -Encoding utf8
    return $metadataPath
}

function Resolve-PythonPath {
    param([string]$Root, [string]$Requested)

    if ($Requested -and (Test-Path $Requested)) {
        return (Resolve-Path $Requested).Path
    }

    $candidates = @(
        (Join-Path $Root "venv\Scripts\python.exe"),
        (Join-Path $Root ".venv\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    return "python"
}

function Test-PyInstaller {
    param([string]$Python)

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Python -c "import PyInstaller" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Invoke-BackendRuntimeImportCheck {
    param(
        [string]$Python,
        [string]$Root
    )

    $checkScript = Join-Path $Root "scripts\check_backend_runtime_imports.py"
    if (-not (Test-Path $checkScript)) {
        throw "Missing backend runtime import check: $checkScript"
    }

    Push-Location $Root
    try {
        & $Python $checkScript
    } finally {
        Pop-Location
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Backend runtime dependency check failed. Install the standard build dependencies with: $Python -m pip install -r requirements-base.txt"
    }
}

function Invoke-FrozenBackendRuntimeLayerCheck {
    param(
        [string]$SidecarExe,
        [string]$SidecarDir,
        [string]$LogRoot
    )

    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    $stdoutPath = Join-Path $LogRoot "frozen-runtime-layer-check.out"
    $stderrPath = Join-Path $LogRoot "frozen-runtime-layer-check.err"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    $process = Start-Process `
        -FilePath $SidecarExe `
        -ArgumentList @("--runtime-layer-check") `
        -WorkingDirectory $SidecarDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0) {
        $stdout = if (Test-Path $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        throw "Frozen backend runtime-layer check failed with exit code $($process.ExitCode). stdout: $stdout stderr: $stderr"
    }
}

function Invoke-FrozenBackendRuntimeImportCheck {
    param(
        [string]$SidecarExe,
        [string]$SidecarDir,
        [string]$LogRoot
    )

    $requiredPaths = @(
        "_internal\onnxruntime",
        "_internal\onnxruntime\capi"
    )
    $missingPaths = @()
    foreach ($relativePath in $requiredPaths) {
        $candidate = Join-Path $SidecarDir $relativePath
        if (-not (Test-Path $candidate)) {
            $missingPaths += $relativePath
        }
    }
    if ($missingPaths.Count -gt 0) {
        throw "Frozen backend sidecar is missing runtime dependencies: $($missingPaths -join ', ')"
    }

    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    $stdoutPath = Join-Path $LogRoot "frozen-runtime-import-check.out"
    $stderrPath = Join-Path $LogRoot "frozen-runtime-import-check.err"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

    $process = Start-Process `
        -FilePath $SidecarExe `
        -ArgumentList @("--runtime-import-check") `
        -WorkingDirectory $SidecarDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -Wait `
        -PassThru

    if ($process.ExitCode -ne 0) {
        $stdout = if (Test-Path $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        throw "Frozen backend runtime dependency check failed with exit code $($process.ExitCode). stdout: $stdout stderr: $stderr"
    }
}

function Resolve-MediaTool {
    param(
        [string[]]$Names,
        [string]$SearchDir
    )

    if ($SearchDir) {
        foreach ($name in $Names) {
            $candidate = Join-Path $SearchDir $name
            if (Test-Path $candidate) {
                return (Resolve-Path $candidate).Path
            }
        }
        return $null
    }

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and $command.Source -and (Test-Path $command.Source)) {
            return (Resolve-Path $command.Source).Path
        }
    }
    return $null
}

function Resolve-PythonInstalledTool {
    param(
        [string[]]$Names,
        [string]$Python
    )

    if ($Python) {
        $resolvedPython = $Python
        $pythonCommand = Get-Command $Python -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pythonCommand -and $pythonCommand.Source) {
            $resolvedPython = $pythonCommand.Source
        }
        $pythonDir = Split-Path -Parent $resolvedPython
        if ($pythonDir) {
            $candidateDirs = @(
                $pythonDir,
                (Join-Path $pythonDir "Scripts"),
                (Join-Path $pythonDir "bin")
            )
            $pythonParentDir = Split-Path -Parent $pythonDir
            if ($pythonParentDir) {
                $candidateDirs += Join-Path $pythonParentDir "bin"
            }
            foreach ($candidateDir in $candidateDirs | Select-Object -Unique) {
                $resolved = Resolve-MediaTool -Names $Names -SearchDir $candidateDir
                if ($resolved) {
                    return $resolved
                }
            }
        }
    }
    return Resolve-MediaTool -Names $Names -SearchDir ""
}

function Test-MediaToolExecutable {
    param(
        [string]$Path,
        [string]$Name,
        [string[]]$VersionArguments = @("-version")
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Name executable was not found: $Path"
    }

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $process = $null
    try {
        $process = Start-Process `
            -FilePath $Path `
            -ArgumentList $VersionArguments `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -Wait `
            -PassThru
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }

    if ($null -eq $process) {
        throw "$Name executable validation did not start: $Path"
    }
    if ($process.ExitCode -ne 0) {
        throw "$Name executable failed validation with exit code $($process.ExitCode): $Path"
    }
}

function Invoke-MediaToolText {
    param(
        [string]$Path,
        [string[]]$Arguments,
        [string]$Label
    )

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $process = $null
    try {
        $process = Start-Process `
            -FilePath $Path `
            -ArgumentList $Arguments `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -Wait `
            -PassThru

        $stdout = if (Test-Path $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }

    if ($null -eq $process) {
        throw "$Label validation did not start: $Path"
    }
    if ($process.ExitCode -ne 0) {
        throw "$Label validation failed with exit code $($process.ExitCode): $stderr"
    }

    return "$stdout`n$stderr"
}

function Assert-MediaToolOutputContains {
    param(
        [string]$Output,
        [string]$Needle,
        [string]$Label
    )

    if ($Output -notmatch [regex]::Escape($Needle)) {
        throw "Slim ffmpeg validation failed: missing $Label '$Needle'."
    }
}

function Test-ScriberFfmpegCapabilities {
    param([string]$Path)

    $encoders = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-encoders") -Label "ffmpeg encoder list"
    foreach ($encoder in @("flac", "libopus", "libmp3lame", "pcm_s16le")) {
        Assert-MediaToolOutputContains -Output $encoders -Needle $encoder -Label "encoder"
    }

    $decoders = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-decoders") -Label "ffmpeg decoder list"
    foreach ($decoder in @("aac", "opus", "mp3", "flac", "alac")) {
        Assert-MediaToolOutputContains -Output $decoders -Needle $decoder -Label "decoder"
    }

    $demuxers = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-demuxers") -Label "ffmpeg demuxer list"
    foreach ($demuxer in @("matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2", "mp3", "wav", "ogg", "flac", "s16le")) {
        Assert-MediaToolOutputContains -Output $demuxers -Needle $demuxer -Label "demuxer"
    }

    $muxers = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-muxers") -Label "ffmpeg muxer list"
    foreach ($muxer in @("flac", "matroska", "ogg", "webm", "mp3", "s16le", "wav")) {
        Assert-MediaToolOutputContains -Output $muxers -Needle $muxer -Label "muxer"
    }

    $protocols = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-protocols") -Label "ffmpeg protocol list"
    foreach ($protocol in @("file", "pipe")) {
        Assert-MediaToolOutputContains -Output $protocols -Needle $protocol -Label "protocol"
    }

    $filters = Invoke-MediaToolText -Path $Path -Arguments @("-hide_banner", "-v", "error", "-filters") -Label "ffmpeg filter list"
    foreach ($filter in @("adelay", "aformat", "amix", "anull", "aresample", "pan")) {
        Assert-MediaToolOutputContains -Output $filters -Needle $filter -Label "filter"
    }
}

function Invoke-ScriberFfmpegFixtureSmoke {
    param(
        [string]$FfmpegPath,
        [string]$FfprobePath,
        [string]$Label = "packaged"
    )

    $smoke = Join-Path $RepoRoot "scripts\ffmpeg\smoke_profile_b_fixtures.py"
    if (-not (Test-Path -LiteralPath $smoke -PathType Leaf)) {
        throw "Missing FFmpeg fixture smoke: $smoke"
    }
    $safeLabel = ($Label -replace '[^A-Za-z0-9_.-]', '-')
    $report = Join-Path $WorkRoot "ffmpeg-profile-b-$safeLabel-smoke.json"
    $arguments = @(
        $smoke,
        "--ffmpeg", $FfmpegPath,
        "--meeting-only",
        "--output", $report,
        "--duration-sec", "0.25",
        "--timeout-sec", "30"
    )
    if ($FfprobePath) {
        $arguments += @("--ffprobe", $FfprobePath, "--require-ffprobe")
    }
    & $PythonPath @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "FFmpeg fixture smoke failed for the exact $Label binary. See: $report"
    }
}

function Invoke-ScriberFfmpegProfileManifest {
    param(
        [string]$FfmpegPath,
        [string]$FfprobePath,
        [string]$OutputPath
    )

    $validator = Join-Path $RepoRoot "scripts\ffmpeg\validate_ffmpeg_profile.py"
    if (-not (Test-Path -LiteralPath $validator -PathType Leaf)) {
        throw "Missing FFmpeg profile validator: $validator"
    }

    $validatorArgs = @(
        $validator,
        "--ffmpeg",
        $FfmpegPath,
        "--profile",
        "B",
        "--output",
        $OutputPath
    )
    if ($FfprobePath) {
        $validatorArgs += @("--ffprobe", $FfprobePath)
    }

    & $PythonPath @validatorArgs
    if ($LASTEXITCODE -ne 0) {
        throw "FFmpeg profile validation failed. See manifest: $OutputPath"
    }
}

function Copy-MediaTools {
    param(
        [string]$SidecarDir,
        [string]$SearchDir,
        [bool]$SkipFfprobe = $false,
        [bool]$ValidateSlimBundle = $false
    )

    $toolsTarget = Join-Path $SidecarDir "tools\ffmpeg"
    Assert-UnderRoot -Root $RepoRoot -Path $toolsTarget -Label "Bundled media tools target"
    New-Item -ItemType Directory -Force -Path $toolsTarget | Out-Null

    $copied = @()
    if ($SearchDir) {
        foreach ($dll in Get-ChildItem -LiteralPath $SearchDir -Filter "*.dll" -File -ErrorAction SilentlyContinue) {
            $targetDll = Join-Path $toolsTarget $dll.Name
            Copy-Item -LiteralPath $dll.FullName -Destination $targetDll -Force
            $copied += $targetDll
        }
    }

    $ffmpeg = Resolve-MediaTool -Names @("ffmpeg.exe", "ffmpeg") -SearchDir $SearchDir
    if (-not $ffmpeg) {
        if ($SearchDir) {
            throw "ffmpeg was not found in MediaToolsDir: $SearchDir"
        }
        throw "ffmpeg was not found on PATH. Pass -MediaToolsDir or install ffmpeg before using -BundleMediaTools."
    }
    Copy-Item -LiteralPath $ffmpeg -Destination (Join-Path $toolsTarget (Split-Path $ffmpeg -Leaf)) -Force
    $copiedFfmpeg = Join-Path $toolsTarget (Split-Path $ffmpeg -Leaf)
    Test-MediaToolExecutable -Path $copiedFfmpeg -Name "ffmpeg"
    if ($ValidateSlimBundle) {
        Test-ScriberFfmpegCapabilities -Path $copiedFfmpeg
    }
    $copied += $copiedFfmpeg

    $copiedFfprobe = ""
    if (-not $SkipFfprobe) {
        $ffprobe = Resolve-MediaTool -Names @("ffprobe.exe", "ffprobe") -SearchDir $SearchDir
        if (-not $ffprobe) {
            if ($SearchDir) {
                throw "ffprobe was not found in MediaToolsDir: $SearchDir"
            }
            throw "ffprobe was not found on PATH. Install ffprobe or pass -SkipBundledFfprobe only for explicit slim-size experiments."
        }
        Copy-Item -LiteralPath $ffprobe -Destination (Join-Path $toolsTarget (Split-Path $ffprobe -Leaf)) -Force
        $copiedFfprobe = Join-Path $toolsTarget (Split-Path $ffprobe -Leaf)
        Test-MediaToolExecutable -Path $copiedFfprobe -Name "ffprobe"
        $copied += $copiedFfprobe
    } else {
        Write-Host "Skipping bundled ffprobe; packaged duration and stream probing will use env/system ffprobe or best-effort fallbacks."
    }

    if ($ValidateSlimBundle) {
        Invoke-ScriberFfmpegFixtureSmoke `
            -FfmpegPath $copiedFfmpeg `
            -FfprobePath $copiedFfprobe `
            -Label "copied"
        $profileManifestPath = Join-Path $toolsTarget "ffmpeg-profile-manifest.json"
        Invoke-ScriberFfmpegProfileManifest -FfmpegPath $copiedFfmpeg -FfprobePath $copiedFfprobe -OutputPath $profileManifestPath
        $copied += $profileManifestPath
    }

    $ytDlp = Resolve-BackendStableMediaTool -Names @("yt-dlp.exe", "yt-dlp") -Python $PythonPath -ExpectedRuntimeCacheKey $runtimeCacheKey
    if ($ytDlp) {
        Copy-Item -LiteralPath $ytDlp -Destination (Join-Path $toolsTarget (Split-Path $ytDlp -Leaf)) -Force
        $copied += (Join-Path $toolsTarget (Split-Path $ytDlp -Leaf))
    }

    $deno = Resolve-BackendStableMediaTool -Names @("deno.exe", "deno") -Python $PythonPath -ExpectedRuntimeCacheKey $runtimeCacheKey -Required
    $copiedDeno = Join-Path $toolsTarget "deno.exe"
    Copy-Item -LiteralPath $deno -Destination $copiedDeno -Force
    Test-MediaToolExecutable -Path $copiedDeno -Name "deno" -VersionArguments @("--version")
    $copied += $copiedDeno

    return $copied
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
$script:BackendRuntimeContractRevision = Get-BackendRuntimeContractRevisionFromSource -Root $RepoRoot
$PythonPath = Resolve-PythonPath -Root $RepoRoot -Requested $PythonPath
if (-not $DistRoot) {
    $DistRoot = Join-Path $RepoRoot "dist\tauri-sidecar"
}
if (-not $WorkRoot) {
    $WorkRoot = Join-Path $RepoRoot "build\tauri-sidecar"
}
if (-not $SidecarCacheRoot) {
    $SidecarCacheRoot = Join-Path $RepoRoot "build\tauri-sidecar-cache"
}
if (-not $RuntimeCacheRoot) {
    $RuntimeCacheRoot = Join-Path $RepoRoot "build\tauri-sidecar-runtime-cache"
}
if (-not $RustAudioSidecarCacheRoot) {
    $RustAudioSidecarCacheRoot = Join-Path $RepoRoot "build\rust-audio-sidecar-cache"
}
if (-not $RustDiarizationSidecarCacheRoot) {
    $RustDiarizationSidecarCacheRoot = Join-Path $RepoRoot "build\rust-diarization-sidecar-cache"
}
if (-not $SherpaOnnxArchiveCacheRoot) {
    $SherpaOnnxArchiveCacheRoot = Join-Path $RepoRoot "build\sherpa-onnx-archive-cache"
}
if (-not $RustDiarizationTargetRoot) {
    $RustDiarizationTargetRoot = Join-Path $RepoRoot "build\rust-diarization-sidecar-target"
}
$DistRoot = Convert-ToFullPath -Path $DistRoot
$WorkRoot = Convert-ToFullPath -Path $WorkRoot
$SidecarCacheRoot = Convert-ToFullPath -Path $SidecarCacheRoot
$RuntimeCacheRoot = Convert-ToFullPath -Path $RuntimeCacheRoot
$RustAudioSidecarCacheRoot = Convert-ToFullPath -Path $RustAudioSidecarCacheRoot
$RustDiarizationSidecarCacheRoot = Convert-ToFullPath -Path $RustDiarizationSidecarCacheRoot
$SherpaOnnxArchiveCacheRoot = Convert-ToFullPath -Path $SherpaOnnxArchiveCacheRoot
$RustDiarizationTargetRoot = Convert-ToFullPath -Path $RustDiarizationTargetRoot
if ($MediaToolsDir) {
    $MediaToolsDir = (Resolve-Path $MediaToolsDir).Path
}
$SpecPath = Join-Path $RepoRoot "packaging\scriber-backend.spec"

Assert-UnderRoot -Root $RepoRoot -Path $DistRoot -Label "DistRoot"
Assert-UnderRoot -Root $RepoRoot -Path $WorkRoot -Label "WorkRoot"
Assert-UnderRoot -Root $RepoRoot -Path $SidecarCacheRoot -Label "SidecarCacheRoot"
Assert-UnderRoot -Root $RepoRoot -Path $RuntimeCacheRoot -Label "RuntimeCacheRoot"
Assert-UnderRoot -Root $RepoRoot -Path $RustAudioSidecarCacheRoot -Label "RustAudioSidecarCacheRoot"
Assert-UnderRoot -Root $RepoRoot -Path $RustDiarizationSidecarCacheRoot -Label "RustDiarizationSidecarCacheRoot"
Assert-UnderRoot -Root $RepoRoot -Path $SherpaOnnxArchiveCacheRoot -Label "SherpaOnnxArchiveCacheRoot"
Assert-UnderRoot -Root $RepoRoot -Path $RustDiarizationTargetRoot -Label "RustDiarizationTargetRoot"

if ($ValidateFinishedRustProductsOnly) {
    $audioValidation = Get-RustAudioSidecarCacheValidation `
        -Root $RepoRoot `
        -CacheRoot $RustAudioSidecarCacheRoot `
        -RunSelfTest
    $diarizationValidation = Get-RustDiarizationSidecarCacheValidation `
        -Root $RepoRoot `
        -Python $PythonPath `
        -WorkerCacheRoot $RustDiarizationSidecarCacheRoot `
        -RunSmoke
    $usable = [bool]$audioValidation.usable -and [bool]$diarizationValidation.usable
    if ($env:GITHUB_OUTPUT) {
        "audio-usable=$(if ($audioValidation.usable) { 'true' } else { 'false' })" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
        "diarization-usable=$(if ($diarizationValidation.usable) { 'true' } else { 'false' })" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
        "usable=$(if ($usable) { 'true' } else { 'false' })" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    }
    [ordered]@{
        ok = $true
        usable = $usable
        audio = [ordered]@{
            usable = [bool]$audioValidation.usable
            cacheKey = [string]$audioValidation.cacheKey
            reason = [string]$audioValidation.reason
        }
        diarization = [ordered]@{
            usable = [bool]$diarizationValidation.usable
            cacheKey = [string]$diarizationValidation.cacheKey
            reason = [string]$diarizationValidation.reason
        }
    } | ConvertTo-Json -Depth 5 -Compress
    return
}

if ($RustAudioOnly) {
    if (-not $RustAudioResultPath) {
        throw "-RustAudioOnly requires -RustAudioResultPath."
    }
    $RustAudioResultPath = Convert-ToFullPath -Path $RustAudioResultPath
    Assert-UnderRoot -Root $RepoRoot -Path $RustAudioResultPath -Label "RustAudioResultPath"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RustAudioResultPath) | Out-Null
    $rustAudioWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $rustAudioResult = $null
    $rustAudioOk = $false
    try {
        $rustAudioResult = Copy-RustAudioSidecarToTauriRelease `
            -Root $RepoRoot `
            -UseIsolatedTarget ([bool]$RustAudioIsolatedTarget)
        $rustAudioOk = $true
    } finally {
        $rustAudioWatch.Stop()
    }
    $rustAudioPayload = [ordered]@{
        ok = $rustAudioOk
        durationMs = [int64]$rustAudioWatch.ElapsedMilliseconds
        result = $rustAudioResult
    }
    $rustAudioTempPath = "$RustAudioResultPath.$PID.tmp"
    $rustAudioPayload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $rustAudioTempPath -Encoding utf8
    Move-Item -LiteralPath $rustAudioTempPath -Destination $RustAudioResultPath -Force
    $rustAudioPayload | ConvertTo-Json -Depth 8 -Compress
    return
}

if ($RustDiarizationPrestageOnly) {
    if (-not $RustDiarizationResultPath) {
        throw "-RustDiarizationPrestageOnly requires -RustDiarizationResultPath."
    }
    if (-not $RustDiarizationPrestageBackendDir) {
        throw "-RustDiarizationPrestageOnly requires -RustDiarizationPrestageBackendDir."
    }

    $RustDiarizationResultPath = Convert-ToFullPath -Path $RustDiarizationResultPath
    $RustDiarizationPrestageBackendDir = Convert-ToFullPath -Path $RustDiarizationPrestageBackendDir
    Assert-UnderRoot -Root $RepoRoot -Path $RustDiarizationResultPath -Label "RustDiarizationResultPath"
    Assert-UnderRoot -Root $RepoRoot -Path $RustDiarizationPrestageBackendDir -Label "RustDiarizationPrestageBackendDir"
    $prestagePrefix = $RustDiarizationPrestageBackendDir.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if ($RustDiarizationResultPath.StartsWith($prestagePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "RustDiarizationResultPath must stay outside RustDiarizationPrestageBackendDir."
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RustDiarizationResultPath) | Out-Null
    if (Test-Path -LiteralPath $RustDiarizationPrestageBackendDir -PathType Container) {
        Remove-Item -LiteralPath $RustDiarizationPrestageBackendDir -Recurse -Force
    }

    $rustDiarizationWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $rustDiarizationResult = $null
    $rustDiarizationOk = $false
    $rustDiarizationFailure = $null
    try {
        $rustDiarizationResult = Copy-RustDiarizationSidecarToBackend `
            -Root $RepoRoot `
            -Python $PythonPath `
            -BackendDir $RustDiarizationPrestageBackendDir `
            -WorkerCacheRoot $RustDiarizationSidecarCacheRoot `
            -ArchiveCacheRoot $SherpaOnnxArchiveCacheRoot `
            -CargoTargetRoot $RustDiarizationTargetRoot
        $rustDiarizationOk = $true
    } catch {
        $rustDiarizationFailure = $_
    } finally {
        $rustDiarizationWatch.Stop()
        if (Test-Path -LiteralPath $RustDiarizationPrestageBackendDir -PathType Container) {
            Remove-Item -LiteralPath $RustDiarizationPrestageBackendDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    $rustDiarizationPayload = [ordered]@{
        ok = $rustDiarizationOk
        durationMs = [int64]$rustDiarizationWatch.ElapsedMilliseconds
        result = $rustDiarizationResult
    }
    $rustDiarizationTempPath = "$RustDiarizationResultPath.$PID.tmp"
    $rustDiarizationPayload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $rustDiarizationTempPath -Encoding utf8
    Move-Item -LiteralPath $rustDiarizationTempPath -Destination $RustDiarizationResultPath -Force
    if ($null -ne $rustDiarizationFailure) {
        throw $rustDiarizationFailure
    }
    $rustDiarizationPayload | ConvertTo-Json -Depth 10 -Compress
    return
}

if (-not (Test-Path $SpecPath)) {
    throw "Missing PyInstaller spec: $SpecPath"
}

if ($UseProfileBFfmpeg -and $UseGyanFfmpegEssentials) {
    throw "Use either -UseProfileBFfmpeg or -UseGyanFfmpegEssentials, not both."
}

$preparedMediaTools = $null
if ($UseProfileBFfmpeg -and -not $MediaToolsDir) {
    Invoke-TimedStep -Label "prepare-profile-b-ffmpeg" -Command {
        $profileBuildScript = Join-Path $RepoRoot "scripts\ffmpeg\build_profile_b_msys2.ps1"
        if (-not (Test-Path -LiteralPath $profileBuildScript -PathType Leaf)) {
            throw "Missing Profile B FFmpeg build script: $profileBuildScript"
        }
        $profileBuildRoot = Join-Path $RepoRoot "build\ffmpeg-profile-b-msys2"
        $profileReportPath = Join-Path $profileBuildRoot "profile-b-msys2-build-report.json"
        $script:PreparedProfileBReportPath = $profileReportPath
        $script:PreparedProfileBReused = $false
        $script:PreparedProfileBReport = $null
        $script:PreparedProfileBMediaToolsDir = ""

        if (Test-Path -LiteralPath $profileReportPath -PathType Leaf) {
            try {
                $existingReport = Get-Content -LiteralPath $profileReportPath -Raw | ConvertFrom-Json
                $existingMediaToolsDir = [string]($existingReport.mediaToolsDir)
                if (
                    $existingReport.ok -and
                    $existingMediaToolsDir -and
                    (Test-Path -LiteralPath (Join-Path $existingMediaToolsDir "ffmpeg.exe") -PathType Leaf) -and
                    (Test-Path -LiteralPath (Join-Path $existingMediaToolsDir "ffprobe.exe") -PathType Leaf)
                ) {
                    $script:PreparedProfileBReport = $existingReport
                    $script:PreparedProfileBMediaToolsDir = (Resolve-Path $existingMediaToolsDir).Path
                    Test-MediaToolExecutable -Path (Join-Path $script:PreparedProfileBMediaToolsDir "ffmpeg.exe") -Name "Cached Profile B ffmpeg"
                    Test-MediaToolExecutable -Path (Join-Path $script:PreparedProfileBMediaToolsDir "ffprobe.exe") -Name "Cached Profile B ffprobe"
                    Test-ScriberFfmpegCapabilities -Path (Join-Path $script:PreparedProfileBMediaToolsDir "ffmpeg.exe")
                    $script:PreparedProfileBReused = $true
                }
            } catch {
                Write-Host "Ignoring stale or unusable Profile B build report: $profileReportPath ($($_.Exception.Message))"
                $script:PreparedProfileBReport = $null
                $script:PreparedProfileBMediaToolsDir = ""
                $script:PreparedProfileBReused = $false
            }
        }

        if (-not $script:PreparedProfileBMediaToolsDir) {
            & $profileBuildScript -RepoRoot $RepoRoot -BuildRoot $profileBuildRoot -PythonExe $PythonPath -InstallDependencies
            if ($LASTEXITCODE -ne 0) {
                throw "FFmpeg Profile B build failed with exit code $LASTEXITCODE."
            }
            if (-not (Test-Path -LiteralPath $profileReportPath -PathType Leaf)) {
                throw "FFmpeg Profile B build did not write report: $profileReportPath"
            }
            $buildReport = Get-Content -LiteralPath $profileReportPath -Raw | ConvertFrom-Json
            if (-not $buildReport.ok) {
                throw "FFmpeg Profile B build report did not report ok=true."
            }
            $script:PreparedProfileBReport = $buildReport
            $script:PreparedProfileBMediaToolsDir = (Resolve-Path ([string]$buildReport.mediaToolsDir)).Path
        }

        Test-MediaToolExecutable -Path (Join-Path $script:PreparedProfileBMediaToolsDir "ffmpeg.exe") -Name "Profile B ffmpeg"
        Test-MediaToolExecutable -Path (Join-Path $script:PreparedProfileBMediaToolsDir "ffprobe.exe") -Name "Profile B ffprobe"
        Test-ScriberFfmpegCapabilities -Path (Join-Path $script:PreparedProfileBMediaToolsDir "ffmpeg.exe")
    }
    $preparedMediaTools = [ordered]@{
        ok = $true
        kind = "profile-b"
        reused = [bool]$script:PreparedProfileBReused
        mediaToolsDir = $script:PreparedProfileBMediaToolsDir
        report = $script:PreparedProfileBReportPath
    }
    $MediaToolsDir = $script:PreparedProfileBMediaToolsDir
    $ValidateSlimMediaTools = $true
} elseif ($UseProfileBFfmpeg -and $MediaToolsDir) {
    Write-Host "Using explicit MediaToolsDir; skipping Profile B FFmpeg preparation."
    $ValidateSlimMediaTools = $true
} elseif ($UseGyanFfmpegEssentials -and -not $MediaToolsDir) {
    Invoke-TimedStep -Label "prepare-gyan-ffmpeg-essentials" -Command {
        $prepareScript = Join-Path $RepoRoot "scripts\prepare_gyan_ffmpeg_essentials.ps1"
        if (-not (Test-Path -LiteralPath $prepareScript -PathType Leaf)) {
            throw "Missing Gyan FFmpeg essentials prepare script: $prepareScript"
        }
        $prepareOutput = & $prepareScript -RepoRoot $RepoRoot
        $prepareJson = ($prepareOutput | Out-String).Trim()
        if (-not $prepareJson) {
            throw "Gyan FFmpeg essentials prepare script produced no JSON output."
        }
        $script:PreparedMediaTools = $prepareJson | ConvertFrom-Json
        if (-not $script:PreparedMediaTools.ok) {
            throw "Gyan FFmpeg essentials preparation did not report ok=true."
        }
        $script:PreparedMediaToolsDir = (Resolve-Path $script:PreparedMediaTools.mediaToolsDir).Path
    }
    $preparedMediaTools = $script:PreparedMediaTools
    $MediaToolsDir = $script:PreparedMediaToolsDir
    $ValidateSlimMediaTools = $true
} elseif ($UseGyanFfmpegEssentials -and $MediaToolsDir) {
    Write-Host "Using explicit MediaToolsDir; skipping Gyan FFmpeg essentials download."
}

if (-not $SkipFrontendBuild) {
    Invoke-TimedStep -Label "frontend-build" -Command {
        Push-Location (Join-Path $RepoRoot "Frontend")
        try {
            npm run build
            if ($LASTEXITCODE -ne 0) {
                throw "Frontend build failed."
            }
        } finally {
            Pop-Location
        }
    }
}

New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null

$cacheEnabled = [bool]$ReuseSidecarIfUnchanged
$cacheHit = $false
$cacheKey = $null
$cacheDir = $null
$runtimeCacheHit = $false
$runtimeCacheKey = $null
$applicationLayerKey = $null

Invoke-TimedStep -Label "backend-runtime-cache-key" -Command {
    $script:BackendRuntimeInputManifest = Get-BackendRuntimeInputManifest `
        -Root $RepoRoot `
        -Python $PythonPath `
        -PyInstallerClean (-not [bool]$LocalPyInstallerNoClean)
    $runtimeManifestJson = $script:BackendRuntimeInputManifest | ConvertTo-Json -Depth 10 -Compress
    $script:BackendRuntimeCacheKey = Get-StringSha256 -Value $runtimeManifestJson
    $script:BackendApplicationInputManifest = Get-BackendApplicationInputManifest -Root $RepoRoot
    $applicationManifestJson = $script:BackendApplicationInputManifest | ConvertTo-Json -Depth 10 -Compress
    $script:BackendApplicationLayerKey = Get-StringSha256 -Value $applicationManifestJson
}
$runtimeCacheKey = $script:BackendRuntimeCacheKey
$applicationLayerKey = $script:BackendApplicationLayerKey

if ($cacheEnabled) {
    Invoke-TimedStep -Label "sidecar-cache-key" -Command {
        $inputManifest = Get-SidecarInputManifest `
            -Root $RepoRoot `
            -RuntimeCacheKey $runtimeCacheKey `
            -SearchDir $MediaToolsDir `
            -BundleTools ([bool]$BundleMediaTools) `
            -UseProfileB ([bool]$UseProfileBFfmpeg) `
            -UseGyanEssentials ([bool]$UseGyanFfmpegEssentials) `
            -SkipFfprobe ([bool]$SkipBundledFfprobe) `
            -ValidateSlimBundle ([bool]$ValidateSlimMediaTools) `
            -PyInstallerClean (-not [bool]$LocalPyInstallerNoClean)
        $inputManifestJson = $inputManifest | ConvertTo-Json -Depth 8 -Compress
        $script:SidecarInputManifest = $inputManifest
        $script:SidecarInputManifestJson = $inputManifestJson
        $script:SidecarCacheKey = Get-StringSha256 -Value $inputManifestJson
    }
    $cacheKey = $script:SidecarCacheKey
    # Keep the on-disk entry short enough for Windows PowerShell 5.1/.NET
    # Framework MAX_PATH handling. The complete SHA-256 identity remains in
    # cache-manifest.json and is always verified before an entry is reused.
    $cacheEntryName = $cacheKey.Substring(0, 24)
    $cacheDir = Join-Path $SidecarCacheRoot $cacheEntryName
    $existingCacheManifestPath = Join-Path $cacheDir "cache-manifest.json"
    if (Test-Path -LiteralPath $existingCacheManifestPath -PathType Leaf) {
        try {
            $existingCacheManifest = Get-Content -LiteralPath $existingCacheManifestPath -Raw | ConvertFrom-Json
            $existingCacheKey = [string]$existingCacheManifest.cacheKey
            $existingInputJson = $existingCacheManifest.inputManifest | ConvertTo-Json -Depth 8 -Compress
            $existingIdentityValid = (
                $existingCacheKey -match '^[0-9a-f]{64}$' -and
                $existingCacheKey.StartsWith($cacheEntryName, [System.StringComparison]::Ordinal) -and
                (Get-StringSha256 -Value $existingInputJson) -eq $existingCacheKey
            )
            if ($existingIdentityValid -and $existingCacheKey -ne $cacheKey) {
                throw "Backend sidecar cache entry prefix collision; refusing to replace a different complete SHA-256 identity."
            }
        } catch {
            if ($_.Exception.Message -like "Backend sidecar cache entry prefix collision*") {
                throw
            }
            # A malformed or interrupted entry is not reusable. The normal
            # exact validation and directory sync below will replace it.
        }
    }
    $cachedSidecarDir = Join-Path $cacheDir "scriber-backend"
    $cachedSidecarExe = Join-Path $cachedSidecarDir "scriber-backend.exe"
    $expectedFlags = Get-SidecarFlagState
    $expectedRustAudioCacheKey = if ($BundleRustAudioSidecar) { Get-RustAudioSidecarCacheKey -Root $RepoRoot } else { "" }
    $expectedRustDiarizationCacheKey = if ($BundleRustDiarizationSidecar) { Get-RustDiarizationSidecarCacheKey -Root $RepoRoot } else { "" }
    if ($CopyToTauriRelease) {
        $targetDir = Join-Path $RepoRoot "Frontend\src-tauri\target\release\backend"
        Invoke-TimedStep -Label "sidecar-target-current-check" -Command {
            $script:SidecarTargetCurrent = Test-SidecarTargetCurrent `
                -TargetDir $targetDir `
                -ExpectedCacheKey $cacheKey `
                -ExpectedRuntimeCacheKey $runtimeCacheKey `
                -ExpectedApplicationLayerKey $applicationLayerKey `
                -ExpectedFlags $expectedFlags `
                -ExpectedRustAudioCacheKey $expectedRustAudioCacheKey `
                -ExpectedRustDiarizationCacheKey $expectedRustDiarizationCacheKey
        }
        if ($script:SidecarTargetCurrent) {
            $cacheHit = $true
            $runtimeCacheHit = $true
            $sidecarDir = $targetDir
            $sidecarExe = Join-Path $sidecarDir "scriber-backend.exe"
            if (-not (Test-Path -LiteralPath $sidecarExe -PathType Leaf)) {
                $sidecarExe = Join-Path $sidecarDir "scriber-backend"
            }
            $mediaToolsCopied = @()
            $toolsDir = Join-Path $sidecarDir "tools\ffmpeg"
            if (Test-Path -LiteralPath $toolsDir -PathType Container) {
                $mediaToolsCopied = @(Get-ChildItem -LiteralPath $toolsDir -File | Select-Object -ExpandProperty FullName)
            }
            $targetMetadata = Get-Content -LiteralPath (Join-Path $targetDir "sidecar-build-metadata.json") -Raw | ConvertFrom-Json
            Invoke-FrozenBackendRuntimeImportCheck -SidecarExe $sidecarExe -SidecarDir $sidecarDir -LogRoot $WorkRoot
            Invoke-ValidateBackendApplicationLayer `
                -Root $RepoRoot `
                -Python $PythonPath `
                -BackendDir $sidecarDir `
                -RuntimeCacheKey $runtimeCacheKey `
                -LogRoot $WorkRoot
            if ($BundleRustAudioSidecar) {
                $targetAudioExeName = if ($IsWindows -or $env:OS -eq "Windows_NT") { "scriber-audio-sidecar.exe" } else { "scriber-audio-sidecar" }
                $targetAudioExe = Join-Path (Split-Path -Parent $targetDir) $targetAudioExeName
                & $targetAudioExe --self-test | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    throw "Target-current Rust audio sidecar self-test failed."
                }
            }
            if ($BundleMediaTools -or $MediaToolsDir) {
                $targetFfmpeg = Join-Path $targetDir "tools\ffmpeg\ffmpeg.exe"
                $targetFfprobe = Join-Path $targetDir "tools\ffmpeg\ffprobe.exe"
                Test-ScriberFfmpegCapabilities -Path $targetFfmpeg
                Invoke-ScriberFfmpegFixtureSmoke `
                    -FfmpegPath $targetFfmpeg `
                    -FfprobePath $(if (Test-Path -LiteralPath $targetFfprobe -PathType Leaf) { $targetFfprobe } else { "" }) `
                    -Label "target-current"
            }
            $metadataPath = Write-SidecarBuildMetadata `
                -SidecarDir $sidecarDir `
                -SidecarExe $sidecarExe `
                -CacheEnabled $cacheEnabled `
                -CacheHit $true `
                -CacheKey $cacheKey `
                -RuntimeCacheHit $true `
                -RuntimeCacheKey $runtimeCacheKey `
                -ApplicationLayerKey $applicationLayerKey `
                -PreparedMediaTools $preparedMediaTools `
                -MediaToolsCopied $mediaToolsCopied `
                -RustAudioSidecarCopied $targetMetadata.rustAudioSidecarCopied `
                -RustDiarizationSidecarCopied $targetMetadata.rustDiarizationSidecarCopied `
                -CopiedTo $targetDir `
                -TargetCurrent $true

            [pscustomobject]@{
                ok = $true
                sidecarDir = $sidecarDir
                sidecarExe = $sidecarExe
                cacheEnabled = $cacheEnabled
                cacheHit = $true
                cacheKey = $cacheKey
                runtimeCacheHit = $true
                runtimeCacheKey = $runtimeCacheKey
                applicationLayerKey = $applicationLayerKey
                targetCurrent = $true
                mediaToolsCopied = $mediaToolsCopied
                rustAudioSidecarCopied = $targetMetadata.rustAudioSidecarCopied
                rustDiarizationSidecarCopied = $targetMetadata.rustDiarizationSidecarCopied
                sidecarBuildMetadata = $metadataPath
                copiedToTauriRelease = $targetDir
            } | ConvertTo-Json -Compress
            return
        }
    }
    $backendCacheValid = $false
    $backendCacheManifestPath = Join-Path $cacheDir "cache-manifest.json"
    if ((Test-Path -LiteralPath $cachedSidecarExe -PathType Leaf) -and (Test-Path -LiteralPath $backendCacheManifestPath -PathType Leaf)) {
        try {
            $backendCacheManifest = Get-Content -LiteralPath $backendCacheManifestPath -Raw | ConvertFrom-Json
            $backendCacheValid = (
                [string]$backendCacheManifest.cacheKey -eq $cacheKey -and
                [string]$backendCacheManifest.sidecarSha256 -eq (Get-Sha256Hex -Path $cachedSidecarExe) -and
                [int64]$backendCacheManifest.sidecarLength -eq [int64](Get-Item -LiteralPath $cachedSidecarExe).Length -and
                (Test-BackendRuntimeLayer -RuntimeDir $cachedSidecarDir -ExpectedCacheKey $runtimeCacheKey) -and
                (Test-BackendMediaFiles -SidecarDir $cachedSidecarDir -ExpectedFiles @($backendCacheManifest.mediaFiles))
            )
        } catch {
            $backendCacheValid = $false
        }
    }
    if ($backendCacheValid) {
        Invoke-TimedStep -Label "sidecar-cache-restore" -Command {
            Copy-DirectoryContents -SourceDir $cachedSidecarDir -TargetDir (Join-Path $DistRoot "scriber-backend") -TargetLabel "Restored sidecar dist target"
        }
        $cacheHit = $true
        $runtimeCacheHit = $true
    }
}

$rustAudioParallelProcess = $null
$rustAudioParallelResultPath = ""
$rustAudioParallelStdoutPath = ""
$rustAudioParallelStderrPath = ""
$rustAudioParallelStopwatch = $null
$rustAudioParallelStdoutTask = $null
$rustAudioParallelStderrTask = $null
$rustDiarizationParallelProcess = $null
$rustDiarizationParallelRoot = ""
$rustDiarizationParallelResultPath = ""
$rustDiarizationParallelBackendDir = ""
$rustDiarizationParallelStdoutPath = ""
$rustDiarizationParallelStderrPath = ""
$rustDiarizationParallelStopwatch = $null
$rustDiarizationParallelStdoutTask = $null
$rustDiarizationParallelStderrTask = $null
$rustDiarizationParallelTimeoutMs = 30 * 60 * 1000
$rustDiarizationPreparedInParallel = $false
$parallelizeSharedRustAudio = (
    [string]$env:SCRIBER_PARALLELIZE_SHARED_RUST_AUDIO -eq "1" -and
    -not $ParallelizeIndependentBuilds -and
    -not $RustAudioIsolatedTarget
)

try {
if (($ParallelizeIndependentBuilds -or $parallelizeSharedRustAudio) -and $BundleRustAudioSidecar) {
    $rustAudioParallelResultPath = Join-Path $WorkRoot "rust-audio-parallel-$PID.json"
    $rustAudioParallelStdoutPath = Join-Path $WorkRoot "rust-audio-parallel-$PID.stdout.log"
    $rustAudioParallelStderrPath = Join-Path $WorkRoot "rust-audio-parallel-$PID.stderr.log"
    foreach ($path in @($rustAudioParallelResultPath, $rustAudioParallelStdoutPath, $rustAudioParallelStderrPath)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force
        }
    }
    $rustAudioParallelArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "scripts\build_tauri_backend_sidecar.ps1",
        "-RepoRoot", ('"{0}"' -f $RepoRoot),
        "-RustAudioOnly"
    )
    if (-not $parallelizeSharedRustAudio) {
        $rustAudioParallelArgs += "-RustAudioIsolatedTarget"
    }
    $rustAudioParallelArgs += @(
        "-RustAudioResultPath", ('"{0}"' -f $rustAudioParallelResultPath)
    )
    Write-Host "Starting Rust audio sidecar preparation in parallel with the Python backend."
    $rustAudioParallelStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $rustAudioParallelStartInfo.FileName = (Get-Command powershell.exe -ErrorAction Stop).Source
    $rustAudioParallelStartInfo.Arguments = ($rustAudioParallelArgs -join " ")
    $rustAudioParallelStartInfo.WorkingDirectory = $RepoRoot
    $rustAudioParallelStartInfo.UseShellExecute = $false
    $rustAudioParallelStartInfo.CreateNoWindow = $true
    $rustAudioParallelStartInfo.RedirectStandardOutput = $true
    $rustAudioParallelStartInfo.RedirectStandardError = $true
    $rustAudioParallelProcess = [System.Diagnostics.Process]::new()
    $rustAudioParallelProcess.StartInfo = $rustAudioParallelStartInfo
    $rustAudioParallelStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        if (-not $rustAudioParallelProcess.Start()) {
            throw "Parallel Rust audio sidecar process did not start."
        }
        # Drain both streams immediately so a verbose Cargo build cannot fill a
        # pipe and block while the Python sidecar is being prepared in parallel.
        $rustAudioParallelStdoutTask = $rustAudioParallelProcess.StandardOutput.ReadToEndAsync()
        $rustAudioParallelStderrTask = $rustAudioParallelProcess.StandardError.ReadToEndAsync()
    } catch {
        $rustAudioParallelStopwatch.Stop()
        Stop-ChildProcessTree -Process $rustAudioParallelProcess -Label "Rust audio sidecar"
        $rustAudioParallelProcess.Dispose()
        $rustAudioParallelProcess = $null
        throw
    }
}

if (($ParallelizeIndependentBuilds -or $ParallelizeRustDiarizationBuild) -and $BundleRustDiarizationSidecar) {
    $rustDiarizationParallelRoot = Join-Path $RepoRoot "build\rust-diarization-parallel\parent-$PID"
    $rustDiarizationParallelResultPath = Join-Path $rustDiarizationParallelRoot "result.json"
    $rustDiarizationParallelBackendDir = Join-Path $rustDiarizationParallelRoot "backend"
    $rustDiarizationParallelStdoutPath = Join-Path $rustDiarizationParallelRoot "stdout.log"
    $rustDiarizationParallelStderrPath = Join-Path $rustDiarizationParallelRoot "stderr.log"
    Assert-UnderRoot -Root $RepoRoot -Path $rustDiarizationParallelRoot -Label "Rust diarization parallel root"
    if (Test-Path -LiteralPath $rustDiarizationParallelRoot -PathType Container) {
        Remove-Item -LiteralPath $rustDiarizationParallelRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $rustDiarizationParallelRoot | Out-Null

    # This is a fixed, checked-in -File invocation. Do not weaken the local
    # execution policy for the new worker; GitHub checkout files run as local.
    $rustDiarizationParallelArgs = @(
        "-NoProfile",
        "-File", "scripts\build_tauri_backend_sidecar.ps1",
        "-RepoRoot", ('"{0}"' -f $RepoRoot),
        "-PythonPath", ('"{0}"' -f $PythonPath),
        "-RustDiarizationPrestageOnly",
        "-RustDiarizationResultPath", ('"{0}"' -f $rustDiarizationParallelResultPath),
        "-RustDiarizationPrestageBackendDir", ('"{0}"' -f $rustDiarizationParallelBackendDir),
        "-RustDiarizationSidecarCacheRoot", ('"{0}"' -f $RustDiarizationSidecarCacheRoot),
        "-SherpaOnnxArchiveCacheRoot", ('"{0}"' -f $SherpaOnnxArchiveCacheRoot),
        "-RustDiarizationTargetRoot", ('"{0}"' -f $RustDiarizationTargetRoot)
    )
    Write-Host "Starting Rust diarization sidecar prestage in parallel with the Python backend."
    $rustDiarizationParallelStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $rustDiarizationParallelStartInfo.FileName = (Get-Command powershell.exe -ErrorAction Stop).Source
    $rustDiarizationParallelStartInfo.Arguments = ($rustDiarizationParallelArgs -join " ")
    $rustDiarizationParallelStartInfo.WorkingDirectory = $RepoRoot
    $rustDiarizationParallelStartInfo.UseShellExecute = $false
    $rustDiarizationParallelStartInfo.CreateNoWindow = $true
    $rustDiarizationParallelStartInfo.RedirectStandardOutput = $true
    $rustDiarizationParallelStartInfo.RedirectStandardError = $true
    $rustDiarizationParallelProcess = [System.Diagnostics.Process]::new()
    $rustDiarizationParallelProcess.StartInfo = $rustDiarizationParallelStartInfo
    $rustDiarizationParallelStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        if (-not $rustDiarizationParallelProcess.Start()) {
            throw "Parallel Rust diarization sidecar process did not start."
        }
        # Drain Cargo and smoke output from both pipes from process start.
        $rustDiarizationParallelStdoutTask = $rustDiarizationParallelProcess.StandardOutput.ReadToEndAsync()
        $rustDiarizationParallelStderrTask = $rustDiarizationParallelProcess.StandardError.ReadToEndAsync()
    } catch {
        $rustDiarizationParallelStopwatch.Stop()
        Stop-ChildProcessTree -Process $rustDiarizationParallelProcess -Label "Rust diarization prestage"
        $rustDiarizationParallelProcess.Dispose()
        $rustDiarizationParallelProcess = $null
        throw
    }
}

if (-not $cacheHit) {
    Invoke-TimedStep -Label "backend-runtime-cache-check" -Command {
        $script:BackendRuntimeCacheHit = Test-BackendRuntimeCache `
            -CacheRoot $RuntimeCacheRoot `
            -ExpectedCacheKey $runtimeCacheKey
    }
    $runtimeCacheHit = [bool]$script:BackendRuntimeCacheHit

    if (-not $runtimeCacheHit) {
        if (-not (Test-PyInstaller -Python $PythonPath)) {
            if (-not $InstallPyInstaller) {
                throw "PyInstaller is not installed for $PythonPath. Re-run with -InstallPyInstaller or install pyinstaller manually."
            }
            & $PythonPath -m pip install pyinstaller
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to install PyInstaller."
            }
        }

        Invoke-TimedStep -Label "backend-runtime-import-check" -Command {
            Invoke-BackendRuntimeImportCheck -Python $PythonPath -Root $RepoRoot
        }

        $runtimeBuildDistRoot = Join-Path $WorkRoot "runtime-dist"
        $runtimeBuildWorkRoot = Join-Path $WorkRoot "runtime-work"
        foreach ($runtimeBuildPath in @($runtimeBuildDistRoot, $runtimeBuildWorkRoot)) {
            Assert-UnderRoot -Root $RepoRoot -Path $runtimeBuildPath -Label "Frozen backend runtime build path"
            if (Test-Path -LiteralPath $runtimeBuildPath -PathType Container) {
                Remove-Item -LiteralPath $runtimeBuildPath -Recurse -Force
            }
        }

        Invoke-TimedStep -Label "pyinstaller-build" -Command {
            $oldRepoRoot = $env:SCRIBER_REPO_ROOT
            $env:SCRIBER_REPO_ROOT = $RepoRoot
            try {
                Push-Location $RepoRoot
                try {
                    $pyInstallerArgs = @("--noconfirm")
                    if (-not $LocalPyInstallerNoClean) {
                        $pyInstallerArgs += "--clean"
                    }
                    $pyInstallerArgs += @(
                        "--distpath", $runtimeBuildDistRoot,
                        "--workpath", $runtimeBuildWorkRoot,
                        $SpecPath
                    )
                    & $PythonPath -m PyInstaller @pyInstallerArgs
                } finally {
                    Pop-Location
                }
            } finally {
                $env:SCRIBER_REPO_ROOT = $oldRepoRoot
            }
            if ($LASTEXITCODE -ne 0) {
                throw "PyInstaller frozen runtime build failed."
            }
        }

        $builtRuntimeDir = Join-Path $runtimeBuildDistRoot "scriber-backend"
        $builtRuntimeExe = Join-Path $builtRuntimeDir "scriber-backend.exe"
        if (-not (Test-Path -LiteralPath $builtRuntimeExe -PathType Leaf)) {
            $builtRuntimeExe = Join-Path $builtRuntimeDir "scriber-backend"
        }
        if (-not (Test-Path -LiteralPath $builtRuntimeExe -PathType Leaf)) {
            throw "PyInstaller completed but the frozen runtime executable was not found."
        }
        Invoke-TimedStep -Label "frozen-runtime-layer-check" -Command {
            Invoke-FrozenBackendRuntimeLayerCheck `
                -SidecarExe $builtRuntimeExe `
                -SidecarDir $builtRuntimeDir `
                -LogRoot $WorkRoot
        }

        Invoke-TimedStep -Label "backend-runtime-cache-save" -Command {
            $cachedRuntimeDir = Join-Path $RuntimeCacheRoot "scriber-backend"
            Copy-DirectoryContents `
                -SourceDir $builtRuntimeDir `
                -TargetDir $cachedRuntimeDir `
                -TargetLabel "Frozen backend runtime cache target"
            $cachedRuntimeExe = Join-Path $cachedRuntimeDir "scriber-backend.exe"
            if (-not (Test-Path -LiteralPath $cachedRuntimeExe -PathType Leaf)) {
                $cachedRuntimeExe = Join-Path $cachedRuntimeDir "scriber-backend"
            }
            Initialize-BackendRuntimeStableMediaTools `
                -CacheRoot $RuntimeCacheRoot `
                -Python $PythonPath
            Write-BackendRuntimeCacheMetadata `
                -RuntimeDir $cachedRuntimeDir `
                -RuntimeExe $cachedRuntimeExe `
                -CacheRoot $RuntimeCacheRoot `
                -CacheKey $runtimeCacheKey `
                -InputManifest $script:BackendRuntimeInputManifest `
                -Python $PythonPath
            if (-not (Test-BackendRuntimeCache -CacheRoot $RuntimeCacheRoot -ExpectedCacheKey $runtimeCacheKey)) {
                throw "The newly built frozen backend runtime cache failed validation."
            }
        }
    }

    $cachedRuntimeDir = Join-Path $RuntimeCacheRoot "scriber-backend"
    Invoke-TimedStep -Label "backend-runtime-stage" -Command {
        Copy-DirectoryContents `
            -SourceDir $cachedRuntimeDir `
            -TargetDir (Join-Path $DistRoot "scriber-backend") `
            -TargetLabel "Frozen backend runtime dist target"
    }
    Invoke-TimedStep -Label "backend-application-stage" -Command {
        Invoke-StageBackendApplicationLayer `
            -Root $RepoRoot `
            -Python $PythonPath `
            -BackendDir (Join-Path $DistRoot "scriber-backend") `
            -RuntimeCacheKey $runtimeCacheKey `
            -LogRoot $WorkRoot
    }
}

$sidecarDir = Join-Path $DistRoot "scriber-backend"
$sidecarExe = Join-Path $sidecarDir "scriber-backend.exe"
if (-not (Test-Path $sidecarExe)) {
    $sidecarExe = Join-Path $sidecarDir "scriber-backend"
}
if (-not (Test-Path $sidecarExe)) {
    throw "Sidecar build completed but executable was not found under $sidecarDir."
}

Invoke-TimedStep -Label "backend-runtime-final-validation" -Command {
    if (-not (Test-BackendRuntimeLayer -RuntimeDir $sidecarDir -ExpectedCacheKey $runtimeCacheKey)) {
        throw "The assembled backend contains a missing, extra, or modified frozen runtime file."
    }
}

Invoke-TimedStep -Label "frozen-runtime-import-check" -Command {
    Invoke-FrozenBackendRuntimeImportCheck -SidecarExe $sidecarExe -SidecarDir $sidecarDir -LogRoot $WorkRoot
}
Invoke-TimedStep -Label "backend-application-post-import-validation" -Command {
    Invoke-ValidateBackendApplicationLayer `
        -Root $RepoRoot `
        -Python $PythonPath `
        -BackendDir $sidecarDir `
        -RuntimeCacheKey $runtimeCacheKey `
        -LogRoot $WorkRoot
}

$mediaToolsCopied = @()
if (-not $cacheHit -and ($BundleMediaTools -or $MediaToolsDir)) {
    Invoke-TimedStep -Label "media-tools-copy" -Command {
        $script:MediaToolsCopied = @(Copy-MediaTools -SidecarDir $sidecarDir -SearchDir $MediaToolsDir -SkipFfprobe ([bool]$SkipBundledFfprobe) -ValidateSlimBundle ([bool]$ValidateSlimMediaTools))
    }
    $mediaToolsCopied = @($script:MediaToolsCopied)
} elseif ($cacheHit) {
    $toolsDir = Join-Path $sidecarDir "tools\ffmpeg"
    if (Test-Path -LiteralPath $toolsDir -PathType Container) {
        $mediaToolsCopied = @(Get-ChildItem -LiteralPath $toolsDir -File | Select-Object -ExpandProperty FullName)
    }
}

if ($BundleMediaTools -or $MediaToolsDir) {
    $bundledDeno = Join-Path $sidecarDir "tools\ffmpeg\deno.exe"
    Test-MediaToolExecutable -Path $bundledDeno -Name "deno" -VersionArguments @("--version")
}

if ($cacheEnabled -and -not $cacheHit) {
    Invoke-TimedStep -Label "sidecar-cache-save" -Command {
        New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
        Copy-DirectoryContents -SourceDir $sidecarDir -TargetDir (Join-Path $cacheDir "scriber-backend") -TargetLabel "Sidecar cache target"
        $cacheManifest = [ordered]@{
            apiVersion = "1"
            generatedAt = (Get-Date).ToUniversalTime().ToString("o")
            cacheKey = $cacheKey
            sidecarSha256 = Get-Sha256Hex -Path $cachedSidecarExe
            sidecarLength = [int64](Get-Item -LiteralPath $cachedSidecarExe).Length
            inputManifest = $script:SidecarInputManifest
            mediaFiles = @(Get-BackendMediaFileIdentityEntries -SidecarDir (Join-Path $cacheDir "scriber-backend"))
        }
        $cacheManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $cacheDir "cache-manifest.json") -Encoding utf8
    }
}

$copiedTo = $null
$metadataPath = $null
$rustAudioSidecarCopied = $null
$rustDiarizationSidecarCopied = $null

if ($CopyToTauriRelease) {
    Invoke-TimedStep -Label "copy-to-tauri-release" -Command {
        $targetDir = Join-Path $RepoRoot "Frontend\src-tauri\target\release\backend"
        Copy-DirectoryContents -SourceDir $sidecarDir -TargetDir $targetDir -TargetLabel "Tauri release backend target"
        if (-not (Test-BackendRuntimeLayer -RuntimeDir $targetDir -ExpectedCacheKey $runtimeCacheKey)) {
            throw "The Tauri release backend contains a missing, extra, or modified frozen runtime file."
        }
        $script:CopiedToTauriRelease = $targetDir
    }
    $copiedTo = $script:CopiedToTauriRelease
}

if ($BundleRustAudioSidecar) {
    if ($rustAudioParallelProcess) {
        $rustAudioParallelOk = $false
        $rustAudioChildDurationMs = $null
        try {
            $rustAudioParallelProcess.WaitForExit()
            $rustAudioParallelProcess.Refresh()
            $rustAudioParallelStdout = $rustAudioParallelStdoutTask.GetAwaiter().GetResult()
            $rustAudioParallelStderr = $rustAudioParallelStderrTask.GetAwaiter().GetResult()
            $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
            [System.IO.File]::WriteAllText($rustAudioParallelStdoutPath, $rustAudioParallelStdout, $utf8WithoutBom)
            [System.IO.File]::WriteAllText($rustAudioParallelStderrPath, $rustAudioParallelStderr, $utf8WithoutBom)
            if (Test-Path -LiteralPath $rustAudioParallelStdoutPath -PathType Leaf) {
                Get-Content -LiteralPath $rustAudioParallelStdoutPath | Out-Host
            }
            if (Test-Path -LiteralPath $rustAudioParallelStderrPath -PathType Leaf) {
                Get-Content -LiteralPath $rustAudioParallelStderrPath | Out-Host
            }
            if ($rustAudioParallelProcess.ExitCode -ne 0) {
                throw "Parallel Rust audio sidecar build failed with exit code $($rustAudioParallelProcess.ExitCode)."
            }
            if (-not (Test-Path -LiteralPath $rustAudioParallelResultPath -PathType Leaf)) {
                throw "Parallel Rust audio sidecar build did not write its result: $rustAudioParallelResultPath"
            }
            $rustAudioParallelPayload = Get-Content -LiteralPath $rustAudioParallelResultPath -Raw | ConvertFrom-Json
            if (-not $rustAudioParallelPayload.ok -or -not $rustAudioParallelPayload.result) {
                throw "Parallel Rust audio sidecar result was invalid."
            }
            $rustAudioChildDurationValue = Get-ObjectPropertyValue -Object $rustAudioParallelPayload -Name "durationMs"
            if ($null -eq $rustAudioChildDurationValue) {
                throw "Parallel Rust audio sidecar result did not report its build duration."
            }
            $rustAudioChildDurationMs = [int64]$rustAudioChildDurationValue
            if ($rustAudioChildDurationMs -lt 0) {
                throw "Parallel Rust audio sidecar result reported an invalid duration."
            }
            $rustAudioSidecarCopied = $rustAudioParallelPayload.result
            $rustAudioParallelOk = $true
        } finally {
            $rustAudioParallelStopwatch.Stop()
            $rustAudioParallelJoinDurationMs = [int64]$rustAudioParallelStopwatch.ElapsedMilliseconds
            $rustAudioParallelDurationMs = if ($null -ne $rustAudioChildDurationMs) {
                [int64]$rustAudioChildDurationMs
            } else {
                $rustAudioParallelJoinDurationMs
            }
            $script:BuildTimingPhases.Add([ordered]@{
                label = "rust-audio-sidecar-build"
                durationMs = $rustAudioParallelDurationMs
                ok = $rustAudioParallelOk
                parallel = $true
                overlappedWallDurationMs = $rustAudioParallelJoinDurationMs
            }) | Out-Null
        }
    } else {
        Invoke-TimedStep -Label "rust-audio-sidecar-build" -Command {
            $script:RustAudioSidecarCopied = Copy-RustAudioSidecarToTauriRelease -Root $RepoRoot -UseIsolatedTarget ([bool]$RustAudioIsolatedTarget)
        }
        $rustAudioSidecarCopied = $script:RustAudioSidecarCopied
    }
}

if ($rustDiarizationParallelProcess) {
    $rustDiarizationParallelOk = $false
    $rustDiarizationChildDurationMs = $null
    $rustDiarizationPrestageCacheHit = $null
    try {
        $rustDiarizationRemainingMs = [int][Math]::Max(
            0,
            $rustDiarizationParallelTimeoutMs - [int64]$rustDiarizationParallelStopwatch.ElapsedMilliseconds
        )
        if (
            -not $rustDiarizationParallelProcess.HasExited -and
            ($rustDiarizationRemainingMs -eq 0 -or -not $rustDiarizationParallelProcess.WaitForExit($rustDiarizationRemainingMs))
        ) {
            Stop-ChildProcessTree -Process $rustDiarizationParallelProcess -Label "Rust diarization prestage"
            throw "Parallel Rust diarization sidecar prestage exceeded the 30-minute limit."
        }
        $rustDiarizationParallelProcess.Refresh()
        $rustDiarizationParallelStdout = $rustDiarizationParallelStdoutTask.GetAwaiter().GetResult()
        $rustDiarizationParallelStderr = $rustDiarizationParallelStderrTask.GetAwaiter().GetResult()
        $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
        [System.IO.File]::WriteAllText($rustDiarizationParallelStdoutPath, $rustDiarizationParallelStdout, $utf8WithoutBom)
        [System.IO.File]::WriteAllText($rustDiarizationParallelStderrPath, $rustDiarizationParallelStderr, $utf8WithoutBom)
        Get-Content -LiteralPath $rustDiarizationParallelStdoutPath | Out-Host
        Get-Content -LiteralPath $rustDiarizationParallelStderrPath | Out-Host
        if ($rustDiarizationParallelProcess.ExitCode -ne 0) {
            throw "Parallel Rust diarization sidecar prestage failed with exit code $($rustDiarizationParallelProcess.ExitCode)."
        }
        if (-not (Test-Path -LiteralPath $rustDiarizationParallelResultPath -PathType Leaf)) {
            throw "Parallel Rust diarization sidecar prestage did not write its result: $rustDiarizationParallelResultPath"
        }
        $rustDiarizationParallelPayload = Get-Content -LiteralPath $rustDiarizationParallelResultPath -Raw | ConvertFrom-Json
        if (-not $rustDiarizationParallelPayload.ok -or -not $rustDiarizationParallelPayload.result) {
            throw "Parallel Rust diarization sidecar prestage result was invalid."
        }
        $rustDiarizationChildDurationValue = Get-ObjectPropertyValue -Object $rustDiarizationParallelPayload -Name "durationMs"
        if ($null -eq $rustDiarizationChildDurationValue) {
            throw "Parallel Rust diarization sidecar prestage did not report its duration."
        }
        $rustDiarizationChildDurationMs = [int64]$rustDiarizationChildDurationValue
        if ($rustDiarizationChildDurationMs -lt 0 -or $rustDiarizationChildDurationMs -gt $rustDiarizationParallelTimeoutMs) {
            throw "Parallel Rust diarization sidecar prestage reported an invalid duration."
        }
        $rustDiarizationPrestageCacheHit = [bool](Get-ObjectPropertyValue -Object $rustDiarizationParallelPayload.result -Name "cacheHit")
        $rustDiarizationPreparedInParallel = $true
        $rustDiarizationParallelOk = $true
    } finally {
        $rustDiarizationParallelStopwatch.Stop()
        $rustDiarizationParallelJoinDurationMs = [int64]$rustDiarizationParallelStopwatch.ElapsedMilliseconds
        $rustDiarizationParallelDurationMs = if ($null -ne $rustDiarizationChildDurationMs) {
            [int64]$rustDiarizationChildDurationMs
        } else {
            $rustDiarizationParallelJoinDurationMs
        }
        $script:BuildTimingPhases.Add([ordered]@{
            label = "rust-diarization-sidecar-prestage"
            durationMs = $rustDiarizationParallelDurationMs
            ok = $rustDiarizationParallelOk
            parallel = $true
            cacheHitBeforePrestage = $rustDiarizationPrestageCacheHit
            overlappedWallDurationMs = $rustDiarizationParallelJoinDurationMs
        }) | Out-Null
    }
}
} finally {
    if ($rustAudioParallelProcess) {
        Stop-ChildProcessTree -Process $rustAudioParallelProcess -Label "Rust audio sidecar"
        $rustAudioParallelProcess.Dispose()
        $rustAudioParallelProcess = $null
    }
    if ($rustDiarizationParallelProcess) {
        Stop-ChildProcessTree -Process $rustDiarizationParallelProcess -Label "Rust diarization prestage"
        $rustDiarizationParallelProcess.Dispose()
        $rustDiarizationParallelProcess = $null
    }
    if ($rustDiarizationParallelRoot -and (Test-Path -LiteralPath $rustDiarizationParallelRoot -PathType Container)) {
        Assert-UnderRoot -Root $RepoRoot -Path $rustDiarizationParallelRoot -Label "Rust diarization parallel cleanup root"
        Remove-Item -LiteralPath $rustDiarizationParallelRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($BundleRustDiarizationSidecar) {
    Invoke-TimedStep -Label "rust-diarization-sidecar-build" -Command {
        $diarizationBackendDir = if ($script:CopiedToTauriRelease) { $script:CopiedToTauriRelease } else { $sidecarDir }
        $script:RustDiarizationSidecarCopied = Copy-RustDiarizationSidecarToBackend `
            -Root $RepoRoot `
            -Python $PythonPath `
            -BackendDir $diarizationBackendDir `
            -WorkerCacheRoot $RustDiarizationSidecarCacheRoot `
            -ArchiveCacheRoot $SherpaOnnxArchiveCacheRoot `
            -CargoTargetRoot $RustDiarizationTargetRoot
        if ($rustDiarizationPreparedInParallel -and -not $script:RustDiarizationSidecarCopied.cacheHit) {
            throw "Final Rust diarization staging did not reuse the cache prepared by the parallel worker."
        }
    }
    $rustDiarizationSidecarCopied = $script:RustDiarizationSidecarCopied
}

if ($CopyToTauriRelease) {
    $metadataPath = Write-SidecarBuildMetadata `
        -SidecarDir $sidecarDir `
        -SidecarExe $sidecarExe `
        -CacheEnabled $cacheEnabled `
        -CacheHit $cacheHit `
        -CacheKey $cacheKey `
        -RuntimeCacheHit $runtimeCacheHit `
        -RuntimeCacheKey $runtimeCacheKey `
        -ApplicationLayerKey $applicationLayerKey `
        -PreparedMediaTools $preparedMediaTools `
        -MediaToolsCopied $mediaToolsCopied `
        -RustAudioSidecarCopied $rustAudioSidecarCopied `
        -RustDiarizationSidecarCopied $rustDiarizationSidecarCopied `
        -CopiedTo $copiedTo
    if (Test-Path -LiteralPath $copiedTo -PathType Container) {
        Copy-Item -LiteralPath $metadataPath -Destination (Join-Path $copiedTo "sidecar-build-metadata.json") -Force
    }
} else {
    $metadataPath = Write-SidecarBuildMetadata `
        -SidecarDir $sidecarDir `
        -SidecarExe $sidecarExe `
        -CacheEnabled $cacheEnabled `
        -CacheHit $cacheHit `
        -CacheKey $cacheKey `
        -RuntimeCacheHit $runtimeCacheHit `
        -RuntimeCacheKey $runtimeCacheKey `
        -ApplicationLayerKey $applicationLayerKey `
        -PreparedMediaTools $preparedMediaTools `
        -MediaToolsCopied $mediaToolsCopied `
        -RustAudioSidecarCopied $rustAudioSidecarCopied `
        -RustDiarizationSidecarCopied $rustDiarizationSidecarCopied `
        -CopiedTo $copiedTo
}

[pscustomobject]@{
    ok = $true
    sidecarDir = $sidecarDir
    sidecarExe = $sidecarExe
    cacheEnabled = $cacheEnabled
    cacheHit = $cacheHit
    cacheKey = $cacheKey
    runtimeCacheHit = $runtimeCacheHit
    runtimeCacheKey = $runtimeCacheKey
    applicationLayerKey = $applicationLayerKey
    mediaToolsCopied = $mediaToolsCopied
    rustAudioSidecarCopied = $rustAudioSidecarCopied
    rustDiarizationSidecarCopied = $rustDiarizationSidecarCopied
    sidecarBuildMetadata = $metadataPath
    copiedToTauriRelease = $copiedTo
} | ConvertTo-Json -Compress
