[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,
    [string]$NodeVersion = "",
    [string]$RustToolchain = "1.97.0",
    [switch]$RefreshNodeArchive
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Resolve-CanonicalUuid {
    param([Parameter(Mandatory = $true)][string]$Value)

    $parsed = [guid]::Empty
    if (-not [guid]::TryParseExact($Value, "D", [ref]$parsed)) {
        throw "RunId must be a canonical RFC-4122 UUID."
    }
    return $parsed.ToString("D")
}

function Assert-UnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $prefix = $resolvedRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label escaped its required root."
    }
    return $resolvedPath
}

function Assert-NoReparsePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$Recurse
    )

    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $resolvedPath = Assert-UnderRoot -Root $resolvedRoot -Path $Path -Label $Label
    if (Test-Path -LiteralPath $resolvedRoot) {
        $rootItem = Get-Item -LiteralPath $resolvedRoot -Force
        if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label safety root must not be a reparse point."
        }
    }
    $relative = $resolvedPath.Substring($resolvedRoot.Length).TrimStart('\', '/')
    $current = $resolvedRoot
    $separators = [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    foreach ($part in $relative.Split($separators, [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        if (-not (Test-Path -LiteralPath $current)) {
            break
        }
        $item = Get-Item -LiteralPath $current -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse point."
        }
    }
    if ($Recurse -and (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
        $nested = Get-ChildItem -LiteralPath $resolvedPath -Recurse -Force -ErrorAction Stop |
            Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 } |
            Select-Object -First 1
        if ($nested) {
            throw "$Label contains a nested reparse point."
        }
    }
    return $resolvedPath
}

function Invoke-CheckedCapture {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )

    $lines = @(& $Command)
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
    return ($lines -join "`n").Trim()
}

function Get-FileIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Version
    )

    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    return [ordered]@{
        name = $Name
        version = $Version
        fileName = $item.Name
        length = [int64]$item.Length
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

function Get-PlainTreeIdentity {
    param([Parameter(Mandatory = $true)][string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Tree identity root was not found."
    }
    $rootItem = Get-Item -LiteralPath $Root -Force
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Tree identity root must not be a reparse point."
    }
    $rootFull = [System.IO.Path]::GetFullPath($rootItem.FullName).TrimEnd('\', '/')
    $entries = [System.Collections.Generic.List[string]]::new()
    $fileCount = 0
    $totalBytes = [int64]0
    foreach ($item in @(Get-ChildItem -LiteralPath $rootFull -Recurse -Force -ErrorAction Stop)) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Tree identity input contains a reparse point."
        }
        $relative = $item.FullName.Substring($rootFull.Length).TrimStart('\', '/').Replace('\', '/')
        if ($item.PSIsContainer) {
            $entries.Add("D|$relative")
        } else {
            $length = [int64]$item.Length
            $entries.Add("F|$relative|$length|$((Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant())")
            $fileCount += 1
            $totalBytes += $length
        }
    }
    $entries.Sort([System.StringComparer]::Ordinal)
    $canonical = $entries -join "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($canonical)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $treeSha256 = ([System.BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
    return [ordered]@{
        fileCount = [int]$fileCount
        totalBytes = [int64]$totalBytes
        treeSha256 = $treeSha256
    }
}

$canonicalRunId = Resolve-CanonicalUuid -Value $RunId
if ([string]::IsNullOrWhiteSpace($NodeVersion)) {
    $NodeVersion = (Get-Content -LiteralPath (Join-Path $RepoRoot ".node-version") -Raw).Trim()
}
if ($NodeVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "NodeVersion must use an exact numeric semantic version."
}
if ($RustToolchain -notmatch '^\d+\.\d+\.\d+$') {
    throw "RustToolchain must use an exact numeric semantic version."
}

$runRoot = Join-Path $RepoRoot "autoresearch-results\installer-size\$canonicalRunId"
$toolchainRoot = Assert-UnderRoot -Root $runRoot -Path (Join-Path $runRoot "toolchain") -Label "Toolchain"
$downloadsRoot = Assert-UnderRoot -Root $toolchainRoot -Path (Join-Path $toolchainRoot "downloads") -Label "Toolchain downloads"
$nodeRoot = Assert-UnderRoot -Root $toolchainRoot -Path (Join-Path $toolchainRoot "node") -Label "Node toolchain"
$nodeAssetName = "node-v$NodeVersion-win-x64.zip"
$nodeArchive = Assert-UnderRoot -Root $downloadsRoot -Path (Join-Path $downloadsRoot $nodeAssetName) -Label "Node archive"
$nodeChecksums = Assert-UnderRoot -Root $downloadsRoot -Path (Join-Path $downloadsRoot "SHASUMS256-v$NodeVersion.txt") -Label "Node checksums"
$nodeBaseUrl = "https://nodejs.org/dist/v$NodeVersion"

$null = Assert-NoReparsePath -Root $RepoRoot -Path $downloadsRoot -Label "Research toolchain path"
New-Item -ItemType Directory -Force -Path $downloadsRoot | Out-Null
$null = Assert-NoReparsePath -Root $RepoRoot -Path $toolchainRoot -Label "Research toolchain root"
$null = Assert-NoReparsePath -Root $toolchainRoot -Path $downloadsRoot -Label "Toolchain downloads" -Recurse
$null = Assert-NoReparsePath -Root $toolchainRoot -Path $nodeRoot -Label "Node toolchain"
if ($RefreshNodeArchive) {
    foreach ($path in @($nodeArchive, $nodeChecksums)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}
if (-not (Test-Path -LiteralPath $nodeChecksums -PathType Leaf)) {
    Invoke-WebRequest -UseBasicParsing -Uri "$nodeBaseUrl/SHASUMS256.txt" -OutFile $nodeChecksums
}
if (-not (Test-Path -LiteralPath $nodeArchive -PathType Leaf)) {
    Invoke-WebRequest -UseBasicParsing -Uri "$nodeBaseUrl/$nodeAssetName" -OutFile $nodeArchive
}

$escapedAssetName = [regex]::Escape($nodeAssetName)
$checksumMatches = @(
    Get-Content -LiteralPath $nodeChecksums |
        Where-Object { $_ -match "^([0-9a-fA-F]{64})\s+$escapedAssetName$" }
)
if ($checksumMatches.Count -ne 1) {
    throw "The official Node checksum list did not contain exactly one entry for $nodeAssetName."
}
$expectedNodeArchiveSha256 = ([regex]::Match($checksumMatches[0], '^([0-9a-fA-F]{64})')).Groups[1].Value.ToLowerInvariant()
$actualNodeArchiveSha256 = (Get-FileHash -LiteralPath $nodeArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualNodeArchiveSha256 -ne $expectedNodeArchiveSha256) {
    throw "The Node archive SHA-256 did not match the official checksum list."
}

$nodeExecutable = Join-Path $nodeRoot "node.exe"
if (-not (Test-Path -LiteralPath $nodeExecutable -PathType Leaf)) {
    $extractRoot = Assert-UnderRoot -Root $toolchainRoot -Path (Join-Path $toolchainRoot "extract-node-$canonicalRunId") -Label "Node extraction"
    if (Test-Path -LiteralPath $extractRoot) {
        $null = Assert-NoReparsePath -Root $toolchainRoot -Path $extractRoot -Label "Node extraction cleanup" -Recurse
        Remove-Item -LiteralPath $extractRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
    Expand-Archive -LiteralPath $nodeArchive -DestinationPath $extractRoot -Force
    $extractedNode = Join-Path $extractRoot "node-v$NodeVersion-win-x64"
    if (-not (Test-Path -LiteralPath (Join-Path $extractedNode "node.exe") -PathType Leaf)) {
        throw "The verified Node archive did not contain the expected Windows x64 directory."
    }
    if (Test-Path -LiteralPath $nodeRoot) {
        $null = Assert-NoReparsePath -Root $toolchainRoot -Path $nodeRoot -Label "Node toolchain cleanup" -Recurse
        Remove-Item -LiteralPath $nodeRoot -Recurse -Force
    }
    Move-Item -LiteralPath $extractedNode -Destination $nodeRoot
    Remove-Item -LiteralPath $extractRoot -Recurse -Force
}
$null = Assert-NoReparsePath -Root $toolchainRoot -Path $nodeRoot -Label "Node toolchain" -Recurse

$nodeVersionActual = Invoke-CheckedCapture -Label "Pinned Node version check" -Command {
    & $nodeExecutable --version
}
if ($nodeVersionActual -ne "v$NodeVersion") {
    throw "Pinned Node reported $nodeVersionActual instead of v$NodeVersion."
}
$npmCli = Join-Path $nodeRoot "node_modules\npm\bin\npm-cli.js"
$npmVersion = Invoke-CheckedCapture -Label "Pinned npm version check" -Command {
    & $nodeExecutable $npmCli --version
}
$frontendRoot = Join-Path $RepoRoot "Frontend"
$frontendPackageLock = Join-Path $frontendRoot "package-lock.json"
if (-not (Test-Path -LiteralPath $frontendPackageLock -PathType Leaf)) {
    throw "The frontend package-lock.json is required for a hermetic research toolchain."
}
Push-Location $frontendRoot
try {
    $null = Invoke-CheckedCapture -Label "Installing locked frontend dependencies" -Command {
        & $nodeExecutable $npmCli ci --no-audit --no-fund --prefer-offline
    }
} finally {
    Pop-Location
}
$tauriCli = Join-Path $RepoRoot "Frontend\node_modules\@tauri-apps\cli\tauri.js"
if (-not (Test-Path -LiteralPath $tauriCli -PathType Leaf)) {
    throw "The locked frontend dependencies do not contain the Tauri CLI entry point."
}
$tauriVersion = Invoke-CheckedCapture -Label "Tauri CLI version check" -Command {
    & $nodeExecutable $tauriCli --version
}
$frontendNodeModules = Join-Path $frontendRoot "node_modules"
$nativeTauriCli = Join-Path $frontendNodeModules "@tauri-apps\cli-win32-x64-msvc\cli.win32-x64-msvc.node"
if (-not (Test-Path -LiteralPath $nativeTauriCli -PathType Leaf)) {
    throw "The locked frontend dependencies do not contain the native Windows x64 Tauri CLI."
}
$viteTempRoot = Join-Path $frontendNodeModules ".vite-temp"
# Vite's default bundled-config loader creates this directory on every
# production build and removes only its randomized temporary file. Include the
# known-empty directory in the frozen tree identity so a build does not create
# artificial node_modules drift between baseline replicas.
New-Item -ItemType Directory -Force -Path $viteTempRoot | Out-Null
$null = Assert-NoReparsePath -Root $frontendRoot -Path $viteTempRoot -Label "Vite temporary config directory" -Recurse
if (@(Get-ChildItem -LiteralPath $viteTempRoot -Force).Count -ne 0) {
    throw "The Vite temporary config directory is not empty after npm ci."
}
$null = Assert-NoReparsePath -Root $frontendRoot -Path $frontendNodeModules -Label "Frontend node_modules" -Recurse
$frontendNodeModulesIdentity = Get-PlainTreeIdentity -Root $frontendNodeModules

$rustup = (Get-Command rustup -ErrorAction Stop).Source
$null = Invoke-CheckedCapture -Label "Installing the pinned Rust toolchain" -Command {
    & $rustup toolchain install $RustToolchain --profile minimal
}
$null = Invoke-CheckedCapture -Label "Installing pinned Rust formatting and lint components" -Command {
    & $rustup component add --toolchain $RustToolchain rustfmt clippy
}
$rustc = Invoke-CheckedCapture -Label "Resolving pinned rustc" -Command {
    & $rustup which --toolchain $RustToolchain rustc
}
$cargo = Invoke-CheckedCapture -Label "Resolving pinned cargo" -Command {
    & $rustup which --toolchain $RustToolchain cargo
}
$rustfmt = Invoke-CheckedCapture -Label "Resolving pinned rustfmt" -Command {
    & $rustup which --toolchain $RustToolchain rustfmt
}
$clippyDriver = Invoke-CheckedCapture -Label "Resolving pinned clippy-driver" -Command {
    & $rustup which --toolchain $RustToolchain clippy-driver
}
$priorRustToolchain = $env:RUSTUP_TOOLCHAIN
$env:RUSTUP_TOOLCHAIN = $RustToolchain
try {
    $rustcVersion = Invoke-CheckedCapture -Label "Pinned rustc version check" -Command { & $rustc --version }
    $cargoVersion = Invoke-CheckedCapture -Label "Pinned cargo version check" -Command { & $cargo --version }
    $rustfmtVersion = Invoke-CheckedCapture -Label "Pinned rustfmt version check" -Command { & $rustfmt --version }
    $clippyVersion = Invoke-CheckedCapture -Label "Pinned clippy version check" -Command { & $clippyDriver --version }
} finally {
    if ($null -eq $priorRustToolchain) {
        Remove-Item Env:RUSTUP_TOOLCHAIN -ErrorAction SilentlyContinue
    } else {
        $env:RUSTUP_TOOLCHAIN = $priorRustToolchain
    }
}
if ($rustcVersion -notmatch "^rustc $([regex]::Escape($RustToolchain))\b") {
    throw "rustc did not resolve the pinned $RustToolchain toolchain."
}

$nsisRoot = Join-Path $env:LOCALAPPDATA "tauri\NSIS"
$null = Assert-NoReparsePath `
    -Root $env:LOCALAPPDATA `
    -Path $nsisRoot `
    -Label "Tauri NSIS toolchain" `
    -Recurse
$makensisCandidates = @(
    (Join-Path $nsisRoot "Bin\makensis.exe"),
    (Join-Path $nsisRoot "makensis.exe")
)
$makensis = $makensisCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $makensis) {
    throw "Tauri's cached NSIS makensis.exe was not found."
}
$makensisVersion = Invoke-CheckedCapture -Label "NSIS version check" -Command { & $makensis /VERSION }
$makensisRelativePath = [System.IO.Path]::GetFullPath($makensis).Substring(
    [System.IO.Path]::GetFullPath($nsisRoot).TrimEnd('\', '/').Length
).TrimStart('\', '/').Replace('\', '/')
$nsisIdentity = Get-FileIdentity -Name "makensis" -Path $makensis -Version $makensisVersion
$nsisIdentity["relativePath"] = $makensisRelativePath
$nsisTreeIdentity = Get-PlainTreeIdentity -Root $nsisRoot

$manifest = [ordered]@{
    schemaVersion = 1
    kind = "scriber-installer-research-toolchain"
    runId = $canonicalRunId
    node = Get-FileIdentity -Name "node" -Path $nodeExecutable -Version $nodeVersionActual
    npm = Get-FileIdentity -Name "npm-cli" -Path $npmCli -Version $npmVersion
    tauri = Get-FileIdentity -Name "tauri-cli" -Path $tauriCli -Version $tauriVersion
    nativeTauriCli = Get-FileIdentity -Name "native-tauri-cli" -Path $nativeTauriCli -Version $tauriVersion
    frontendNodeModules = $frontendNodeModulesIdentity
    frontendPackageLock = Get-FileIdentity -Name "frontend-package-lock" -Path $frontendPackageLock -Version "lockfile-v3"
    nodeArchive = [ordered]@{
        fileName = $nodeAssetName
        length = [int64](Get-Item -LiteralPath $nodeArchive).Length
        sha256 = $actualNodeArchiveSha256
        checksumSource = "$nodeBaseUrl/SHASUMS256.txt"
    }
    rustc = Get-FileIdentity -Name "rustc-rustup-proxy" -Path $rustc -Version $rustcVersion
    cargo = Get-FileIdentity -Name "cargo-rustup-proxy" -Path $cargo -Version $cargoVersion
    rustfmt = Get-FileIdentity -Name "rustfmt-rustup-proxy" -Path $rustfmt -Version $rustfmtVersion
    clippyDriver = Get-FileIdentity -Name "clippy-driver-rustup-proxy" -Path $clippyDriver -Version $clippyVersion
    rustToolchain = $RustToolchain
    nsis = $nsisIdentity
    nsisTree = $nsisTreeIdentity
}
$manifestJson = $manifest | ConvertTo-Json -Depth 8
$manifestPath = Join-Path $toolchainRoot "toolchain-manifest.json"
$manifestJson | Set-Content -LiteralPath $manifestPath -Encoding utf8

[ordered]@{
    ok = $true
    schemaVersion = 1
    runId = $canonicalRunId
    nodeBin = $nodeRoot
    rustToolchain = $RustToolchain
    manifest = $manifestPath
} | ConvertTo-Json -Depth 5
