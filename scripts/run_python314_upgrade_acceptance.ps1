[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [Parameter(Mandatory = $true)]
    [string]$BaselineInstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$CandidateInstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$CandidateRuntimeAttestationPath,
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{40}$")]
    [string]$SourceCommit,
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{64}$")]
    [string]$SourceTreeSha256,
    [string]$ManifestPath = "",
    [string]$InstallerHooksPath = "",
    [string]$WorkRoot = "",
    [string]$OutputPath = "",
    [ValidateRange(30, 600)]
    [int]$TimeoutSec = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:Contract = "ScriberPython314UpgradeAcceptanceV1"
$script:BaselineRepository = "MyButtermilk/Scriber"
$script:BaselineReleaseId = 360523309
$script:BaselineTag = "v0.5.47"
$script:BaselineCommit = "aa918d3d111dab834e05e5b588c24da503f9176d"
$script:BaselineAssetId = 491618831
$script:BaselineInstallerName = "Scriber_0.5.47_x64-setup.exe"
$script:BaselineInstallerLength = [int64]79787381
$script:BaselineInstallerSha256 = "a4c242935e2eb26d6ab2517a38538b4942f204477cb745ef6c7909312efdd8f5"
$script:AttestationName = "scriber-autoresearch-runtime-attestation.json"

function Get-NormalizedFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Test-StrictDescendantPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $fullPath = Get-NormalizedFullPath -Path $Path
    $fullParent = Get-NormalizedFullPath -Path $Parent
    return $fullPath.StartsWith(
        $fullParent + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-NoReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    $cursor = Get-NormalizedFullPath -Path $Path
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "unsafe_reparse_point"
            }
        }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) {
            break
        }
        $cursor = $parent
    }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Write-AtomicJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )
    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $temporary = Join-Path $directory (".{0}.{1}.tmp" -f ([System.IO.Path]::GetFileName($Path)), [guid]::NewGuid())
    try {
        $json = $Value | ConvertTo-Json -Depth 20
        [System.IO.File]::WriteAllText(
            $temporary,
            $json + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Add-GateCheck {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.ArrayList]$Checks,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$Ok
    )
    [void]$Checks.Add([ordered]@{ name = $Name; ok = $Ok })
    if (-not $Ok) {
        throw ("check_failed_{0}" -f $Name)
    }
}

function Assert-PublicBaselineManifest {
    param([Parameter(Mandatory = $true)][object]$Manifest)
    $source = $Manifest.sourceRelease
    $installer = $source.installer
    if (
        [string]$Manifest.contract -cne "ScriberPython314UpgradeManifestV1" -or
        [int]$Manifest.schemaVersion -ne 1 -or
        [string]$source.repository -cne $script:BaselineRepository -or
        [int64]$source.releaseId -ne $script:BaselineReleaseId -or
        [string]$source.tag -cne $script:BaselineTag -or
        [string]$source.commitSha -cne $script:BaselineCommit -or
        [int64]$installer.assetId -ne $script:BaselineAssetId -or
        [string]$installer.name -cne $script:BaselineInstallerName -or
        [int64]$installer.length -ne $script:BaselineInstallerLength -or
        [string]$installer.sha256 -cne $script:BaselineInstallerSha256
    ) {
        throw "public_baseline_manifest_identity_mismatch"
    }
}

function Get-InstalledTreeInventory {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $root = Get-NormalizedFullPath -Path $InstallRoot
    $files = @(
        Get-ChildItem -LiteralPath $root -File -Recurse -Force |
            ForEach-Object {
                $relative = $_.FullName.Substring($root.Length).TrimStart("\", "/").Replace("\", "/")
                [ordered]@{
                    path = $relative
                    length = [int64]$_.Length
                    sha256 = Get-Sha256Hex -Path $_.FullName
                }
            } |
            Sort-Object -Property @{ Expression = { $_.path }; Ascending = $true }
    )
    $treeHasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        foreach ($entry in $files) {
            $line = "{0}`0{1}`0{2}`n" -f $entry.path, $entry.length, $entry.sha256
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($line)
            [void]$treeHasher.TransformBlock($bytes, 0, $bytes.Length, $bytes, 0)
        }
        [void]$treeHasher.TransformFinalBlock([byte[]]::new(0), 0, 0)
        $treeSha256 = ([System.BitConverter]::ToString($treeHasher.Hash)).Replace("-", "").ToLowerInvariant()
    } finally {
        $treeHasher.Dispose()
    }
    $totalBytes = [int64]0
    foreach ($entry in $files) {
        $totalBytes += [int64]$entry.length
    }
    return [ordered]@{
        fileCount = $files.Count
        totalBytes = $totalBytes
        treeSha256 = $treeSha256
        files = $files
    }
}

function Compare-InstalledTreeInventory {
    param(
        [Parameter(Mandatory = $true)][object]$Expected,
        [Parameter(Mandatory = $true)][object]$Actual
    )
    if ([int]$Expected.fileCount -ne [int]$Actual.fileCount) {
        return [ordered]@{ matches = $false; reason = "file_count"; path = "" }
    }
    for ($index = 0; $index -lt $Expected.files.Count; $index++) {
        $left = $Expected.files[$index]
        $right = $Actual.files[$index]
        if ([string]$left.path -cne [string]$right.path) {
            return [ordered]@{ matches = $false; reason = "path"; path = [string]$right.path }
        }
        if ([int64]$left.length -ne [int64]$right.length) {
            return [ordered]@{ matches = $false; reason = "length"; path = [string]$right.path }
        }
        if ([string]$left.sha256 -cne [string]$right.sha256) {
            return [ordered]@{ matches = $false; reason = "sha256"; path = [string]$right.path }
        }
    }
    return [ordered]@{
        matches = ([string]$Expected.treeSha256 -ceq [string]$Actual.treeSha256)
        reason = ""
        path = ""
    }
}

function Get-ScopedScriberProcesses {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $root = Get-NormalizedFullPath -Path $InstallRoot
    $escapedRoot = [regex]::Escape($root)
    $installerNames = @($script:BaselineInstallerName, $script:CandidateInstallerName) |
        Where-Object { $_ } |
        ForEach-Object { [regex]::Escape($_) }
    $installerPattern = if ($installerNames.Count -gt 0) {
        "(?i)(?:{0})" -f ($installerNames -join "|")
    } else {
        "(?!)"
    }
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                [int]$_.ProcessId -ne $PID -and (
                    ($_.ExecutablePath -and (Test-StrictDescendantPath -Path $_.ExecutablePath -Parent $root)) -or
                    ($_.CommandLine -and $_.CommandLine -match $escapedRoot) -or
                    ($_.CommandLine -and $_.CommandLine -match $installerPattern)
                )
            } |
            Select-Object ProcessId, ParentProcessId, Name, ExecutablePath
    )
}

function Get-ScopedUninstallEntries {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $root = Get-NormalizedFullPath -Path $InstallRoot
    $registryRoots = @(
        "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
    )
    $matches = @()
    foreach ($registryRoot in $registryRoots) {
        if (-not (Test-Path -LiteralPath $registryRoot)) {
            continue
        }
        foreach ($entry in Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue) {
            $value = Get-ItemProperty -LiteralPath $entry.PSPath -ErrorAction SilentlyContinue
            if (
                $value -and
                [string]$value.DisplayName -like "Scriber*" -and
                $value.InstallLocation -and
                (Get-NormalizedFullPath -Path ([string]$value.InstallLocation)) -ieq $root
            ) {
                $matches += [ordered]@{
                    keyName = [string]$entry.PSChildName
                    displayName = [string]$value.DisplayName
                }
            }
        }
    }
    return @($matches)
}

function Wait-ScopedProcessesExit {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][int]$Timeout
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($Timeout)
    do {
        $processes = @(Get-ScopedScriberProcesses -InstallRoot $InstallRoot)
        if ($processes.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "scoped_process_timeout"
}

function Invoke-InstallerSmokeKeepInstalled {
    param(
        [Parameter(Mandatory = $true)][string]$InstallerPath,
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$DataRoot,
        [Parameter(Mandatory = $true)][string]$ReportPath
    )
    $smokeScript = Join-Path $RepoRoot "scripts\smoke_windows_installer.ps1"
    & $smokeScript `
        -RepoRoot $RepoRoot `
        -PythonExecutable $PythonExecutable `
        -InstallerPath $InstallerPath `
        -InstallDir $InstallRoot `
        -DataDir $DataRoot `
        -OutputPath $ReportPath `
        -VerifyFrontend `
        -KeepInstalled
    if (-not (Test-Path -LiteralPath $ReportPath -PathType Leaf)) {
        throw "installer_smoke_failed"
    }
    $report = Read-JsonFile -Path $ReportPath
    if (-not [bool]$report.ok -or -not [bool]$report.frontend.verified -or -not [bool]$report.cleanupVerified) {
        throw "installer_smoke_contract_failed"
    }
    return [ordered]@{
        sha256 = Get-Sha256Hex -Path $ReportPath
        frontendVerified = [bool]$report.frontend.verified
        cleanupVerified = [bool]$report.cleanupVerified
    }
}

function Assert-O0Runtime {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $manifestPath = Join-Path $InstallRoot "backend\runtime-layer-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "missing_runtime_layer_manifest"
    }
    $manifest = Read-JsonFile -Path $manifestPath
    $policy = $manifest.runtimePolicy
    if (
        [string]$policy.python.version -cne "3.14.6" -or
        [string]$policy.python.cacheTag -cne "cpython-314" -or
        [string]$policy.runtimeFlavor -cne "Official" -or
        [string]$policy.variantId -cne "O0" -or
        [bool]$policy.tailCallInterpreter -or
        [bool]$policy.jit.expected -or
        [bool]$policy.jit.active
    ) {
        throw "runtime_is_not_official_o0"
    }
    $python314 = Join-Path $InstallRoot "backend\_internal\python314.dll"
    if (-not (Test-Path -LiteralPath $python314 -PathType Leaf)) {
        throw "missing_python314_dll"
    }
    $cp313 = @(
        Get-ChildItem -LiteralPath $InstallRoot -File -Recurse -Force |
            Where-Object {
                $_.Name -ieq "python313.dll" -or
                $_.Name -match "\.cp313-win_amd64\.pyd$"
            }
    )
    if ($cp313.Count -ne 0) {
        throw "python313_artifact_present"
    }
    return [ordered]@{
        pythonVersion = [string]$policy.python.version
        cacheTag = [string]$policy.python.cacheTag
        runtimeFlavor = [string]$policy.runtimeFlavor
        variantId = [string]$policy.variantId
        tailCallInterpreter = [bool]$policy.tailCallInterpreter
        jitAvailable = [bool]$policy.jit.available
        jitExpected = [bool]$policy.jit.expected
        jitActive = [bool]$policy.jit.active
        python314DllSha256 = Get-Sha256Hex -Path $python314
        cp313ArtifactCount = $cp313.Count
    }
}

function Verify-CandidateRuntimeAttestation {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $installedAttestation = Join-Path $InstallRoot $script:AttestationName
    $wasPackaged = Test-Path -LiteralPath $installedAttestation -PathType Leaf
    if ($wasPackaged) {
        if ((Get-Sha256Hex -Path $installedAttestation) -cne (Get-Sha256Hex -Path $CandidateRuntimeAttestationPath)) {
            throw "packaged_attestation_differs"
        }
    } else {
        Copy-Item -LiteralPath $CandidateRuntimeAttestationPath -Destination $installedAttestation
    }
    try {
        $attestationScript = Join-Path $RepoRoot "scripts\perf\runtime_attestation.py"
        $lines = @(& $PythonExecutable $attestationScript verify --repo-root $RepoRoot --install-root $InstallRoot)
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0 -or $lines.Count -eq 0) {
            throw "candidate_runtime_attestation_failed"
        }
        $verified = ($lines -join "`n") | ConvertFrom-Json
        if (-not [bool]$verified.ok) {
            throw "candidate_runtime_attestation_failed"
        }
        return [ordered]@{
            attestationId = [string]$verified.attestationId
            sha256 = Get-Sha256Hex -Path $CandidateRuntimeAttestationPath
            sourceContentSha256 = [string]$verified.sourceContentSha256
            packaged = [bool]$wasPackaged
        }
    } finally {
        if (-not $wasPackaged -and (Test-Path -LiteralPath $installedAttestation)) {
            Remove-Item -LiteralPath $installedAttestation -Force
        }
    }
}

function Wait-CandidateUpgradeInstallStable {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][object]$ExpectedInventory,
        [Parameter(Mandatory = $true)][int]$Timeout
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($Timeout)
    $stableMatches = 0
    do {
        $processCount = @(Get-ScopedScriberProcesses -InstallRoot $InstallRoot).Count
        if ($processCount -eq 0 -and (Test-Path -LiteralPath $InstallRoot -PathType Container)) {
            try {
                $actual = Get-InstalledTreeInventory -InstallRoot $InstallRoot
                $comparison = Compare-InstalledTreeInventory -Expected $ExpectedInventory -Actual $actual
                if ([bool]$comparison.matches) {
                    $stableMatches += 1
                    if ($stableMatches -ge 3) {
                        return $actual
                    }
                } else {
                    $stableMatches = 0
                }
            } catch {
                $stableMatches = 0
            }
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "candidate_upgrade_never_reached_stable_clean_inventory"
}

function Invoke-StrictUninstall {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$DataSentinelPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSentinelSha256
    )
    $uninstallers = @(Get-ChildItem -LiteralPath $InstallRoot -File -Force |
        Where-Object { $_.Name -match "^uninstall(?:er)?\.exe$" })
    if ($uninstallers.Count -ne 1) {
        throw "exclusive_uninstaller_not_found"
    }
    $process = Start-Process -FilePath $uninstallers[0].FullName -ArgumentList @("/S") -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit($TimeoutSec * 1000)) {
        throw "uninstaller_launcher_timeout"
    }
    Wait-ScopedProcessesExit -InstallRoot $InstallRoot -Timeout $TimeoutSec
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
    do {
        $rootRemoved = -not (Test-Path -LiteralPath $InstallRoot)
        $processCount = @(Get-ScopedScriberProcesses -InstallRoot $InstallRoot).Count
        $registryCount = @(Get-ScopedUninstallEntries -InstallRoot $InstallRoot).Count
        if ($rootRemoved -and $processCount -eq 0 -and $registryCount -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    $sentinelPreserved = (
        (Test-Path -LiteralPath $DataSentinelPath -PathType Leaf) -and
        (Get-Sha256Hex -Path $DataSentinelPath) -ceq $ExpectedSentinelSha256
    )
    $result = [ordered]@{
        installRootRemoved = -not (Test-Path -LiteralPath $InstallRoot)
        processCount = @(Get-ScopedScriberProcesses -InstallRoot $InstallRoot).Count
        registryCount = @(Get-ScopedUninstallEntries -InstallRoot $InstallRoot).Count
        dataSentinelPreserved = $sentinelPreserved
    }
    if (
        -not [bool]$result.installRootRemoved -or
        [int]$result.processCount -ne 0 -or
        [int]$result.registryCount -ne 0 -or
        -not [bool]$result.dataSentinelPreserved
    ) {
        throw "strict_uninstall_contract_failed"
    }
    return $result
}

$RepoRoot = Get-NormalizedFullPath -Path $RepoRoot
if (-not $ManifestPath) {
    $ManifestPath = Join-Path $RepoRoot "packaging\python-314-upgrade-from-v0.5.47.json"
}
if (-not $InstallerHooksPath) {
    $InstallerHooksPath = Join-Path $RepoRoot "Frontend\src-tauri\windows\installer-hooks.nsh"
}
if (-not $WorkRoot) {
    $runName = "{0}-{1}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffZ"), [guid]::NewGuid()
    $WorkRoot = Join-Path $RepoRoot ("tmp\installer-smoke\python314-upgrade-acceptance\{0}" -f $runName)
}
$BaselineInstallerPath = Get-NormalizedFullPath -Path $BaselineInstallerPath
$CandidateInstallerPath = Get-NormalizedFullPath -Path $CandidateInstallerPath
$script:CandidateInstallerName = [System.IO.Path]::GetFileName($CandidateInstallerPath)
$CandidateRuntimeAttestationPath = Get-NormalizedFullPath -Path $CandidateRuntimeAttestationPath
$PythonExecutable = Get-NormalizedFullPath -Path $PythonExecutable
$ManifestPath = Get-NormalizedFullPath -Path $ManifestPath
$InstallerHooksPath = Get-NormalizedFullPath -Path $InstallerHooksPath
$WorkRoot = Get-NormalizedFullPath -Path $WorkRoot
if (-not $OutputPath) {
    $OutputPath = Join-Path $WorkRoot "evidence\python314-upgrade-acceptance.json"
}
$OutputPath = Get-NormalizedFullPath -Path $OutputPath

$allowedScratchRoot = Join-Path $RepoRoot "tmp\installer-smoke"
$allowedOutputRoot = Join-Path $RepoRoot "tmp"
if (
    -not (Test-StrictDescendantPath -Path $WorkRoot -Parent $allowedScratchRoot) -or
    -not (Test-StrictDescendantPath -Path $OutputPath -Parent $allowedOutputRoot)
) {
    throw "unsafe_work_or_output_path"
}
Assert-NoReparsePoint -Path $WorkRoot
Assert-NoReparsePoint -Path $OutputPath

$cleanInstallRoot = Join-Path $WorkRoot "clean-install"
$cleanDataRoot = Join-Path $WorkRoot "clean-data"
$upgradeInstallRoot = Join-Path $WorkRoot "upgrade-install"
$upgradeDataRoot = Join-Path $WorkRoot "upgrade-data"
foreach ($protectedRoot in @($cleanInstallRoot, $cleanDataRoot, $upgradeInstallRoot, $upgradeDataRoot)) {
    if (Test-StrictDescendantPath -Path $OutputPath -Parent $protectedRoot) {
        throw "output_path_overlaps_install_or_data"
    }
}

$checks = [System.Collections.ArrayList]::new()
$result = [ordered]@{
    contract = $script:Contract
    schemaVersion = 1
    ok = $false
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    source = [ordered]@{
        commit = $SourceCommit
        contentSha256 = $SourceTreeSha256
        algorithm = "git-worktree-sha256-v1"
    }
    checks = $checks
    baseline = $null
    candidate = $null
    manifest = $null
    clean = $null
    upgrade = $null
    parity = $null
    sentinel = $null
    uninstall = $null
    failureCode = ""
}

$lockStream = $null
try {
    foreach ($requiredFile in @(
        $BaselineInstallerPath,
        $CandidateInstallerPath,
        $CandidateRuntimeAttestationPath,
        $PythonExecutable,
        $ManifestPath,
        $InstallerHooksPath
    )) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "required_input_missing"
        }
    }
    if ($BaselineInstallerPath -ieq $CandidateInstallerPath) {
        throw "baseline_and_candidate_must_differ"
    }
    if (Test-Path -LiteralPath $WorkRoot) {
        $existing = @(Get-ChildItem -LiteralPath $WorkRoot -Force -ErrorAction SilentlyContinue)
        if ($existing.Count -ne 0) {
            throw "work_root_must_be_empty"
        }
    }
    New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
    $lockPath = Join-Path $WorkRoot "acceptance.lock"
    $lockStream = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )

    $manifest = Read-JsonFile -Path $ManifestPath
    Assert-PublicBaselineManifest -Manifest $manifest
    $validator = Join-Path $RepoRoot "scripts\validate_python314_upgrade_manifest.py"
    $validationLines = @(
        & $PythonExecutable $validator `
            --manifest $ManifestPath `
            --installer-hooks $InstallerHooksPath `
            --installer $BaselineInstallerPath
    )
    if ($LASTEXITCODE -ne 0 -or $validationLines.Count -eq 0) {
        throw "baseline_manifest_validation_failed"
    }
    $validation = ($validationLines -join "`n") | ConvertFrom-Json
    Add-GateCheck -Checks $checks -Name "publicBaselineManifestAndInstaller" -Ok (
        [bool]$validation.installerVerified -and
        [int64]$validation.sourceReleaseId -eq $script:BaselineReleaseId -and
        [int64]$validation.sourceAssetId -eq $script:BaselineAssetId
    )

    $attestationPayload = Read-JsonFile -Path $CandidateRuntimeAttestationPath
    Add-GateCheck -Checks $checks -Name "candidateSourceBinding" -Ok (
        [string]$attestationPayload.kind -ceq "scriber-autoresearch-runtime-attestation" -and
        [int]$attestationPayload.schemaVersion -eq 1 -and
        [string]$attestationPayload.source.head -ceq $SourceCommit -and
        [string]$attestationPayload.source.contentSha256 -ceq $SourceTreeSha256 -and
        [string]$attestationPayload.source.algorithm -ceq "git-worktree-sha256-v1"
    )

    $result.manifest = [ordered]@{
        sha256 = Get-Sha256Hex -Path $ManifestPath
        sourceReleaseId = $script:BaselineReleaseId
        sourceAssetId = $script:BaselineAssetId
    }
    $result.baseline = [ordered]@{
        repository = $script:BaselineRepository
        releaseId = $script:BaselineReleaseId
        tag = $script:BaselineTag
        commit = $script:BaselineCommit
        assetId = $script:BaselineAssetId
        name = $script:BaselineInstallerName
        length = [int64](Get-Item -LiteralPath $BaselineInstallerPath).Length
        sha256 = Get-Sha256Hex -Path $BaselineInstallerPath
    }
    $result.candidate = [ordered]@{
        name = [System.IO.Path]::GetFileName($CandidateInstallerPath)
        length = [int64](Get-Item -LiteralPath $CandidateInstallerPath).Length
        sha256 = Get-Sha256Hex -Path $CandidateInstallerPath
        runtimeAttestationSha256 = Get-Sha256Hex -Path $CandidateRuntimeAttestationPath
    }

    New-Item -ItemType Directory -Path $cleanDataRoot -Force | Out-Null
    $cleanSmoke = Invoke-InstallerSmokeKeepInstalled `
        -InstallerPath $CandidateInstallerPath `
        -InstallRoot $cleanInstallRoot `
        -DataRoot $cleanDataRoot `
        -ReportPath (Join-Path $WorkRoot "clean-installer-smoke.json")
    $cleanAttestation = Verify-CandidateRuntimeAttestation -InstallRoot $cleanInstallRoot
    Add-GateCheck -Checks $checks -Name "candidateRuntimeAttestation" -Ok (
        [string]$cleanAttestation.sourceContentSha256 -ceq $SourceTreeSha256
    )
    $cleanRuntime = Assert-O0Runtime -InstallRoot $cleanInstallRoot
    Add-GateCheck -Checks $checks -Name "cleanOfficialPython314O0" -Ok $true
    $cleanInventory = Get-InstalledTreeInventory -InstallRoot $cleanInstallRoot
    $cleanSentinel = Join-Path $cleanDataRoot "clean-uninstall-sentinel.bin"
    [System.IO.File]::WriteAllBytes($cleanSentinel, [System.Text.Encoding]::UTF8.GetBytes("scriber-cp314-clean-data-sentinel-v1"))
    $cleanSentinelSha256 = Get-Sha256Hex -Path $cleanSentinel
    [void](Invoke-StrictUninstall `
        -InstallRoot $cleanInstallRoot `
        -DataSentinelPath $cleanSentinel `
        -ExpectedSentinelSha256 $cleanSentinelSha256)
    Add-GateCheck -Checks $checks -Name "cleanCandidateStrictUninstall" -Ok $true
    $result.clean = [ordered]@{
        smoke = $cleanSmoke
        attestation = $cleanAttestation
        runtime = $cleanRuntime
        inventory = $cleanInventory
    }

    New-Item -ItemType Directory -Path $upgradeDataRoot -Force | Out-Null
    $baselineSmoke = Invoke-InstallerSmokeKeepInstalled `
        -InstallerPath $BaselineInstallerPath `
        -InstallRoot $upgradeInstallRoot `
        -DataRoot $upgradeDataRoot `
        -ReportPath (Join-Path $WorkRoot "baseline-installer-smoke.json")
    Add-GateCheck -Checks $checks -Name "publicBaselineInstalled" -Ok $true
    $sentinelPath = Join-Path $upgradeDataRoot "python314-upgrade-user-data-sentinel.bin"
    [System.IO.File]::WriteAllBytes(
        $sentinelPath,
        [System.Text.Encoding]::UTF8.GetBytes("scriber-python314-upgrade-user-data-sentinel-v1")
    )
    $sentinelSha256 = Get-Sha256Hex -Path $sentinelPath

    $candidateProcess = Start-Process `
        -FilePath $CandidateInstallerPath `
        -ArgumentList @("/S", "/D=$upgradeInstallRoot") `
        -WindowStyle Hidden `
        -PassThru
    if (-not $candidateProcess.WaitForExit($TimeoutSec * 1000)) {
        throw "candidate_installer_launcher_timeout"
    }
    $upgradeInventory = Wait-CandidateUpgradeInstallStable `
        -InstallRoot $upgradeInstallRoot `
        -ExpectedInventory $cleanInventory `
        -Timeout $TimeoutSec
    $parity = Compare-InstalledTreeInventory -Expected $cleanInventory -Actual $upgradeInventory
    Add-GateCheck -Checks $checks -Name "cleanUpgradeByteExactParity" -Ok ([bool]$parity.matches)
    Add-GateCheck -Checks $checks -Name "sentinelSurvivedUpgrade" -Ok (
        (Test-Path -LiteralPath $sentinelPath -PathType Leaf) -and
        (Get-Sha256Hex -Path $sentinelPath) -ceq $sentinelSha256
    )
    $upgradeRuntime = Assert-O0Runtime -InstallRoot $upgradeInstallRoot
    Add-GateCheck -Checks $checks -Name "upgradeOfficialPython314O0" -Ok $true
    Wait-ScopedProcessesExit -InstallRoot $upgradeInstallRoot -Timeout $TimeoutSec
    Add-GateCheck -Checks $checks -Name "upgradeProcessCleanup" -Ok (
        @(Get-ScopedScriberProcesses -InstallRoot $upgradeInstallRoot).Count -eq 0
    )

    $result.upgrade = [ordered]@{
        baselineSmoke = $baselineSmoke
        runtime = $upgradeRuntime
        inventory = $upgradeInventory
    }
    $result.parity = $parity
    $result.sentinel = [ordered]@{
        relativePath = "upgrade-data/python314-upgrade-user-data-sentinel.bin"
        sha256 = $sentinelSha256
        survivedUpgrade = $true
        survivedUninstall = $false
    }
    $uninstall = Invoke-StrictUninstall `
        -InstallRoot $upgradeInstallRoot `
        -DataSentinelPath $sentinelPath `
        -ExpectedSentinelSha256 $sentinelSha256
    Add-GateCheck -Checks $checks -Name "strictUninstallAndDataPreservation" -Ok $true
    $result.uninstall = $uninstall
    $result.sentinel.survivedUninstall = [bool]$uninstall.dataSentinelPreserved
    $result.ok = $true
} catch {
    $result.failureCode = "python314_upgrade_acceptance_failed"
    throw
} finally {
    if ($lockStream) {
        $lockStream.Dispose()
    }
    if (Test-Path -LiteralPath (Split-Path -Parent $OutputPath)) {
        Write-AtomicJson -Path $OutputPath -Value $result
    } elseif (Test-StrictDescendantPath -Path $OutputPath -Parent (Join-Path $RepoRoot "tmp")) {
        Write-AtomicJson -Path $OutputPath -Value $result
    }
}
