[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{64}$")]
    [string]$ExpectedInstallerSha256,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeAttestationPath,
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{40}$")]
    [string]$SourceCommit,
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{64}$")]
    [string]$SourceTreeSha256,
    [string]$WorkRoot = "",
    [string]$OutputPath = "",
    [ValidateRange(120, 1800)]
    [int]$InstallTimeoutSec = 600,
    [ValidateRange(300, 14400)]
    [int]$AppUxTimeoutSec = 7200,
    [ValidateRange(1800, 43200)]
    [int]$FullLocalTimeoutSec = 21600,
    [ValidateRange(30, 600)]
    [int]$UninstallTimeoutSec = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:Contract = "ScriberPython314O0FullLocalCorrectnessV1"
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

function Get-TextSha256Hex {
    param([Parameter(Mandatory = $true)][string]$Value)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
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

function Get-ArtifactReference {
    param([Parameter(Mandatory = $true)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force
    return [ordered]@{
        name = [System.IO.Path]::GetFileName($Path)
        length = [int64]$item.Length
        sha256 = Get-Sha256Hex -Path $Path
    }
}

function ConvertTo-CommandLineArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    if (-not $Value) {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    if ($Value.Contains('"')) {
        throw "unsupported_quote_in_process_argument"
    }
    return '"{0}"' -f $Value
}

function Invoke-BoundedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSec
    )
    $argumentLine = (@($Arguments | ForEach-Object { ConvertTo-CommandLineArgument -Value $_ }) -join " ")
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $argumentLine `
        -RedirectStandardOutput $StdoutPath `
        -RedirectStandardError $StderrPath `
        -WindowStyle Hidden `
        -PassThru
    $timedOut = -not $process.WaitForExit($TimeoutSec * 1000)
    if ($timedOut) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        [void]$process.WaitForExit(5000)
    }
    return [ordered]@{
        exitCode = if ($timedOut) { 124 } else { [int]$process.ExitCode }
        timedOut = $timedOut
    }
}

function Get-ScopedScriberProcesses {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $root = Get-NormalizedFullPath -Path $InstallRoot
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                [int]$_.ProcessId -ne $PID -and
                $_.ExecutablePath -and
                (
                    Test-StrictDescendantPath `
                        -Path ([string]$_.ExecutablePath) `
                        -Parent $root
                )
            } |
            Select-Object ProcessId, ParentProcessId, Name
    )
}

function Get-AllScriberProcesses {
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                [string]$_.Name -match "(?i)^scriber(?:-desktop|-backend|-audio-sidecar)?\.exe$" -or
                [string]$_.ExecutablePath -match "(?i)[\\/]scriber(?:-desktop|-backend|-audio-sidecar)?\.exe$"
            } |
            Select-Object ProcessId, ParentProcessId, Name
    )
}

function Stop-ScopedScriberProcesses {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $processes = @(Get-ScopedScriberProcesses -InstallRoot $InstallRoot)
    foreach ($process in $processes) {
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    if ($processes.Count -gt 0) {
        Start-Sleep -Seconds 2
    }
    return $processes.Count
}

function Wait-ScopedProcessesExit {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][int]$TimeoutSec
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
    do {
        if (@(Get-ScopedScriberProcesses -InstallRoot $InstallRoot).Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "scoped_process_timeout"
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
                    keyHash = Get-TextSha256Hex -Value ([string]$entry.PSChildName)
                }
            }
        }
    }
    return @($matches)
}

function Get-AllScriberUninstallEntryCount {
    $registryRoots = @(
        "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
    )
    $count = 0
    foreach ($registryRoot in $registryRoots) {
        if (-not (Test-Path -LiteralPath $registryRoot)) {
            continue
        }
        foreach ($entry in Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue) {
            $value = Get-ItemProperty -LiteralPath $entry.PSPath -ErrorAction SilentlyContinue
            if ($value -and [string]$value.DisplayName -like "Scriber*") {
                $count += 1
            }
        }
    }
    return $count
}

function Get-HostPolicy {
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    if (-not $cpu -or [string]$cpu.Manufacturer -notmatch "(?i)AMD") {
        throw "amd_host_required"
    }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    $standardUser = -not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $standardUser) {
        throw "standard_user_required"
    }
    $defender = Get-MpComputerStatus -ErrorAction Stop
    $defenderEnabled = [bool]$defender.AntivirusEnabled -and [bool]$defender.RealTimeProtectionEnabled
    if (-not $defenderEnabled) {
        throw "defender_required"
    }
    $os = Get-CimInstance Win32_OperatingSystem
    $power = (& powercfg.exe /getactivescheme 2>$null | Out-String).Trim()
    $hostMaterial = [ordered]@{
        cpu = [ordered]@{
            manufacturer = [string]$cpu.Manufacturer
            name = [string]$cpu.Name
            cores = [int]$cpu.NumberOfCores
            logicalProcessors = [int]$cpu.NumberOfLogicalProcessors
        }
        windows = [ordered]@{
            version = [string]$os.Version
            buildNumber = [string]$os.BuildNumber
            architecture = [string]$os.OSArchitecture
        }
        totalVisibleMemoryKb = [int64]$os.TotalVisibleMemorySize
        powerScheme = $power
    }
    $hostJson = $hostMaterial | ConvertTo-Json -Depth 5 -Compress
    return [ordered]@{
        cpuFamily = "amd"
        hostProfileSha256 = Get-TextSha256Hex -Value $hostJson
        defenderEnabled = $defenderEnabled
        standardUser = $standardUser
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
        [string]$policy.python.version -cne "3.14.7" -or
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
    $cp313Artifacts = @(
        Get-ChildItem -LiteralPath $InstallRoot -File -Recurse -Force |
            Where-Object {
                $_.Name -ieq "python313.dll" -or
                $_.Name -match "\.cp313-win_amd64\.pyd$"
            }
    )
    if ($cp313Artifacts.Count -ne 0) {
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
        runtimeManifestSha256 = Get-Sha256Hex -Path $manifestPath
        cp313ArtifactCount = $cp313Artifacts.Count
    }
}

function Invoke-StrictUninstall {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$DataSentinelPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSentinelSha256,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath
    )
    Wait-ScopedProcessesExit -InstallRoot $InstallRoot -TimeoutSec $UninstallTimeoutSec
    $uninstallers = @(
        Get-ChildItem -LiteralPath $InstallRoot -File -Force |
            Where-Object { $_.Name -match "^uninstall(?:er)?\.exe$" }
    )
    if ($uninstallers.Count -ne 1) {
        throw "exclusive_uninstaller_not_found"
    }
    $execution = Invoke-BoundedProcess `
        -FilePath $uninstallers[0].FullName `
        -Arguments @("/S") `
        -StdoutPath $StdoutPath `
        -StderrPath $StderrPath `
        -TimeoutSec $UninstallTimeoutSec
    if ([int]$execution.exitCode -ne 0) {
        throw "uninstaller_failed"
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($UninstallTimeoutSec)
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
        exitCode = [int]$execution.exitCode
        timedOut = [bool]$execution.timedOut
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
$InstallerPath = Get-NormalizedFullPath -Path $InstallerPath
$RuntimeAttestationPath = Get-NormalizedFullPath -Path $RuntimeAttestationPath
$PythonExecutable = Get-NormalizedFullPath -Path $PythonExecutable
if (-not $WorkRoot) {
    $runName = "{0}-{1}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffZ"), [guid]::NewGuid()
    $WorkRoot = Join-Path $RepoRoot ("tmp\python-runtime-o0-full-local-correctness\{0}" -f $runName)
}
$WorkRoot = Get-NormalizedFullPath -Path $WorkRoot
if (-not $OutputPath) {
    $OutputPath = Join-Path $WorkRoot "evidence\o0-full-local-correctness.json"
}
$OutputPath = Get-NormalizedFullPath -Path $OutputPath
$allowedScratchRoot = Join-Path $RepoRoot "tmp"
if (
    -not (Test-StrictDescendantPath -Path $WorkRoot -Parent $allowedScratchRoot) -or
    -not (Test-StrictDescendantPath -Path $OutputPath -Parent $WorkRoot)
) {
    throw "unsafe_work_or_output_path"
}
Assert-NoReparsePoint -Path $WorkRoot
Assert-NoReparsePoint -Path $OutputPath

$installRoot = Join-Path $WorkRoot "install"
$dataRoot = Join-Path $WorkRoot "data"
$appUxRoot = Join-Path $WorkRoot "app-ux"
$perfRoot = Join-Path $WorkRoot "perf"
$logsRoot = Join-Path $WorkRoot "logs"
$validationPath = Join-Path $WorkRoot "evidence\measurement-validation.json"
$smokeReportPath = Join-Path $WorkRoot "evidence\installer-smoke.json"
$attestationVerifyPath = Join-Path $WorkRoot "evidence\runtime-attestation-verification.json"
$sentinelPath = Join-Path $dataRoot "o0-full-local-data-sentinel.bin"

$result = [ordered]@{
    contract = $script:Contract
    schemaVersion = 1
    ok = $false
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    scope = "o0-fallback-correctness-only"
    correctnessEligible = $false
    promotionEligible = $false
    productionPromotionAuthorized = $false
    source = [ordered]@{
        commit = $SourceCommit
        contentSha256 = $SourceTreeSha256
        algorithm = "git-worktree-sha256-v1"
    }
    host = $null
    installer = $null
    runtime = $null
    steps = [ordered]@{
        installerSmokeExitCode = -1
        runtimeAttestationExitCode = -1
        appUxExitCode = -1
        fullLocalExitCode = -1
        measurementValidationExitCode = -1
        strictUninstallExitCode = -1
    }
    artifacts = [ordered]@{}
    measurement = $null
    cleanup = $null
    limitations = [ordered]@{
        a13Measured = $false
        counterbalancedAbBa = $false
        rebootBlocks = 0
        promotionProfileSatisfied = $false
        purpose = "The selected official O0 fallback is measured for installed correctness only."
    }
    failedPhase = ""
    failureCode = ""
}

$lockStream = $null
$installed = $false
$sentinelSha256 = ""
$measurementPassed = $false
$failurePhase = "preflight"
$fatalFailure = $false
$previousScriberPython = [Environment]::GetEnvironmentVariable("SCRIBER_PYTHON", "Process")
try {
    foreach ($requiredFile in @($InstallerPath, $RuntimeAttestationPath, $PythonExecutable)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "required_input_missing"
        }
    }
    if ((Get-Sha256Hex -Path $InstallerPath) -cne $ExpectedInstallerSha256) {
        throw "installer_sha256_mismatch"
    }
    $head = (& git.exe -C $RepoRoot rev-parse HEAD 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -cne $SourceCommit) {
        throw "source_commit_mismatch"
    }
    $attestationInput = Read-JsonFile -Path $RuntimeAttestationPath
    if (
        [string]$attestationInput.kind -cne "scriber-autoresearch-runtime-attestation" -or
        [int]$attestationInput.schemaVersion -ne 1 -or
        [string]$attestationInput.source.head -cne $SourceCommit -or
        [string]$attestationInput.source.contentSha256 -cne $SourceTreeSha256 -or
        [string]$attestationInput.source.algorithm -cne "git-worktree-sha256-v1" -or
        -not ([string]$attestationInput.attestationId -match "^[0-9a-f]{64}$")
    ) {
        throw "runtime_attestation_source_binding_invalid"
    }
    if (@(Get-AllScriberProcesses).Count -ne 0) {
        throw "preexisting_scriber_process"
    }
    if ((Get-AllScriberUninstallEntryCount) -ne 0) {
        throw "preexisting_scriber_installation"
    }
    if (Test-Path -LiteralPath $WorkRoot) {
        $existing = @(Get-ChildItem -LiteralPath $WorkRoot -Force -ErrorAction SilentlyContinue)
        if ($existing.Count -ne 0) {
            throw "work_root_must_be_empty"
        }
    }
    New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $logsRoot -Force | Out-Null
    $lockPath = Join-Path $WorkRoot "runner.lock"
    $lockStream = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )

    $result.host = Get-HostPolicy
    $result.installer = [ordered]@{
        name = [System.IO.Path]::GetFileName($InstallerPath)
        length = [int64](Get-Item -LiteralPath $InstallerPath).Length
        sha256 = Get-Sha256Hex -Path $InstallerPath
    }
    $result.artifacts.runtimeAttestationInput = Get-ArtifactReference -Path $RuntimeAttestationPath

    $failurePhase = "fresh-install"
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $smokeExecution = Invoke-BoundedProcess `
        -FilePath $powershell `
        -Arguments @(
            "-NoProfile",
            "-File", (Join-Path $RepoRoot "scripts\smoke_windows_installer.ps1"),
            "-RepoRoot", $RepoRoot,
            "-PythonExecutable", $PythonExecutable,
            "-InstallerPath", $InstallerPath,
            "-InstallDir", $installRoot,
            "-DataDir", $dataRoot,
            "-OutputPath", $smokeReportPath,
            "-VerifyFrontend",
            "-KeepInstalled"
        ) `
        -StdoutPath (Join-Path $logsRoot "installer-smoke.out") `
        -StderrPath (Join-Path $logsRoot "installer-smoke.err") `
        -TimeoutSec $InstallTimeoutSec
    $result.steps.installerSmokeExitCode = [int]$smokeExecution.exitCode
    if ([int]$smokeExecution.exitCode -ne 0 -or -not (Test-Path -LiteralPath $smokeReportPath -PathType Leaf)) {
        throw "fresh_installer_smoke_failed"
    }
    $smoke = Read-JsonFile -Path $smokeReportPath
    if (-not [bool]$smoke.ok -or -not [bool]$smoke.frontend.verified -or -not [bool]$smoke.cleanupVerified) {
        throw "fresh_installer_smoke_contract_failed"
    }
    $installed = Test-Path -LiteralPath $installRoot -PathType Container
    if (-not $installed) {
        throw "fresh_install_root_missing"
    }
    $result.artifacts.installerSmoke = Get-ArtifactReference -Path $smokeReportPath

    $failurePhase = "runtime-binding"
    $installedAttestationPath = Join-Path $installRoot $script:AttestationName
    $attestationWasPackaged = Test-Path -LiteralPath $installedAttestationPath -PathType Leaf
    if ($attestationWasPackaged) {
        if ((Get-Sha256Hex -Path $installedAttestationPath) -cne (Get-Sha256Hex -Path $RuntimeAttestationPath)) {
            throw "packaged_attestation_differs"
        }
    } else {
        Copy-Item -LiteralPath $RuntimeAttestationPath -Destination $installedAttestationPath
    }
    $attestationExecution = Invoke-BoundedProcess `
        -FilePath $PythonExecutable `
        -Arguments @(
            (Join-Path $RepoRoot "scripts\perf\runtime_attestation.py"),
            "verify",
            "--repo-root", $RepoRoot,
            "--install-root", $installRoot
        ) `
        -StdoutPath $attestationVerifyPath `
        -StderrPath (Join-Path $logsRoot "runtime-attestation.err") `
        -TimeoutSec 300
    $result.steps.runtimeAttestationExitCode = [int]$attestationExecution.exitCode
    if (
        [int]$attestationExecution.exitCode -ne 0 -or
        -not (Test-Path -LiteralPath $attestationVerifyPath -PathType Leaf)
    ) {
        throw "runtime_attestation_verification_failed"
    }
    $attestationVerification = Read-JsonFile -Path $attestationVerifyPath
    if (
        -not [bool]$attestationVerification.ok -or
        [string]$attestationVerification.attestationId -cne [string]$attestationInput.attestationId -or
        [string]$attestationVerification.sourceContentSha256 -cne $SourceTreeSha256
    ) {
        throw "runtime_attestation_verification_contract_failed"
    }
    $runtime = Assert-O0Runtime -InstallRoot $installRoot
    $result.runtime = [ordered]@{
        identity = $runtime
        attestationId = [string]$attestationVerification.attestationId
        attestationSha256 = Get-Sha256Hex -Path $installedAttestationPath
        sourceContentSha256 = [string]$attestationVerification.sourceContentSha256
        benchmarkManifestInjected = -not $attestationWasPackaged
    }
    $result.artifacts.runtimeAttestationVerification = Get-ArtifactReference -Path $attestationVerifyPath

    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    [System.IO.File]::WriteAllBytes(
        $sentinelPath,
        [System.Text.Encoding]::UTF8.GetBytes("scriber-o0-full-local-correctness-data-sentinel-v1")
    )
    $sentinelSha256 = Get-Sha256Hex -Path $sentinelPath

    $failurePhase = "app-ux-full-local"
    $appUxExecution = Invoke-BoundedProcess `
        -FilePath $powershell `
        -Arguments @(
            "-NoProfile",
            "-File", (Join-Path $RepoRoot "scripts\perf\collect_app_ux.ps1"),
            "-Suite", "FullLocal",
            "-InstallRoot", $installRoot,
            "-OutputDir", $appUxRoot,
            "-PythonExecutable", $PythonExecutable
        ) `
        -StdoutPath (Join-Path $logsRoot "app-ux.out") `
        -StderrPath (Join-Path $logsRoot "app-ux.err") `
        -TimeoutSec $AppUxTimeoutSec
    $result.steps.appUxExitCode = [int]$appUxExecution.exitCode
    $appUxEvidencePath = Join-Path $appUxRoot "app-ux-evidence.json"
    if ([int]$appUxExecution.exitCode -ne 0 -or -not (Test-Path -LiteralPath $appUxEvidencePath -PathType Leaf)) {
        throw "full_local_app_ux_failed"
    }
    $appUxSha256 = Get-Sha256Hex -Path $appUxEvidencePath
    $result.artifacts.appUx = Get-ArtifactReference -Path $appUxEvidencePath

    $failurePhase = "full-local"
    $repoEnvPath = Join-Path $RepoRoot ".env"
    if (Test-Path -LiteralPath $repoEnvPath -PathType Leaf) {
        $scriberPythonOverride = @(
            Get-Content -LiteralPath $repoEnvPath |
                Where-Object { $_ -match "^\s*SCRIBER_PYTHON\s*=" }
        )
        if ($scriberPythonOverride.Count -ne 0) {
            throw "dotenv_scriber_python_override_forbidden"
        }
    }
    [Environment]::SetEnvironmentVariable("SCRIBER_PYTHON", $PythonExecutable, "Process")
    $fullLocalExecution = Invoke-BoundedProcess `
        -FilePath $powershell `
        -Arguments @(
            "-NoProfile",
            "-File", (Join-Path $RepoRoot "scripts\perf\run.ps1"),
            "-Suite", "FullLocal",
            "-InstallRoot", $installRoot,
            "-OutputDir", $perfRoot,
            "-AppUxEvidencePath", $appUxEvidencePath,
            "-AzureMaiCaptureTimeMp3", "Disabled",
            "-SpeechmaticsCaptureTimeWav", "Disabled"
        ) `
        -StdoutPath (Join-Path $logsRoot "full-local.out") `
        -StderrPath (Join-Path $logsRoot "full-local.err") `
        -TimeoutSec $FullLocalTimeoutSec
    $result.steps.fullLocalExitCode = [int]$fullLocalExecution.exitCode
    if ([int]$fullLocalExecution.exitCode -notin @(0, 2)) {
        throw "full_local_runner_failed_without_evidence"
    }
    $rawCandidates = @(Get-ChildItem -LiteralPath $perfRoot -File -Filter "fulllocal-*.json")
    if ($rawCandidates.Count -ne 1) {
        throw "full_local_raw_evidence_not_unique"
    }
    $rawPath = $rawCandidates[0].FullName
    $result.artifacts.fullLocalRaw = Get-ArtifactReference -Path $rawPath

    $failurePhase = "measurement-validation"
    $validatorExecution = Invoke-BoundedProcess `
        -FilePath $PythonExecutable `
        -Arguments @(
            (Join-Path $RepoRoot "scripts\perf\validate_o0_full_local_correctness.py"),
            "--raw", $rawPath,
            "--app-ux", $appUxEvidencePath,
            "--expected-commit", $SourceCommit,
            "--expected-source-content-sha256", $SourceTreeSha256,
            "--expected-attestation-id", ([string]$attestationInput.attestationId),
            "--expected-attestation-sha256", (Get-Sha256Hex -Path $RuntimeAttestationPath),
            "--expected-app-ux-sha256", $appUxSha256,
            "--output", $validationPath
        ) `
        -StdoutPath (Join-Path $logsRoot "measurement-validation.out") `
        -StderrPath (Join-Path $logsRoot "measurement-validation.err") `
        -TimeoutSec 300
    $result.steps.measurementValidationExitCode = [int]$validatorExecution.exitCode
    if (
        [int]$validatorExecution.exitCode -ne 0 -or
        -not (Test-Path -LiteralPath $validationPath -PathType Leaf)
    ) {
        throw "full_local_measurement_validation_failed"
    }
    $measurement = Read-JsonFile -Path $validationPath
    if (
        -not [bool]$measurement.ok -or
        -not [bool]$measurement.correctnessEligible -or
        [bool]$measurement.promotionEligible -or
        [bool]$measurement.productionPromotionAuthorized
    ) {
        throw "full_local_measurement_validation_contract_failed"
    }
    $runtimeAfter = Assert-O0Runtime -InstallRoot $installRoot
    if ([string]$runtimeAfter.runtimeManifestSha256 -cne [string]$runtime.runtimeManifestSha256) {
        throw "runtime_manifest_drift"
    }
    $result.artifacts.measurementValidation = Get-ArtifactReference -Path $validationPath
    $result.measurement = $measurement
    $result.correctnessEligible = $true
    $measurementPassed = $true
} catch {
    $fatalFailure = $true
    $result.failedPhase = $failurePhase
    $result.failureCode = "o0_full_local_correctness_failed"
} finally {
    [Environment]::SetEnvironmentVariable("SCRIBER_PYTHON", $previousScriberPython, "Process")
    if ($installed -or (Test-Path -LiteralPath $installRoot -PathType Container)) {
        $failurePhase = "strict-uninstall"
        if (-not $measurementPassed) {
            $forcedCount = Stop-ScopedScriberProcesses -InstallRoot $installRoot
        } else {
            $forcedCount = 0
        }
        try {
            if (-not $sentinelSha256) {
                New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
                [System.IO.File]::WriteAllBytes(
                    $sentinelPath,
                    [System.Text.Encoding]::UTF8.GetBytes("scriber-o0-full-local-correctness-data-sentinel-v1")
                )
                $sentinelSha256 = Get-Sha256Hex -Path $sentinelPath
            }
            $uninstall = Invoke-StrictUninstall `
                -InstallRoot $installRoot `
                -DataSentinelPath $sentinelPath `
                -ExpectedSentinelSha256 $sentinelSha256 `
                -StdoutPath (Join-Path $logsRoot "uninstall.out") `
                -StderrPath (Join-Path $logsRoot "uninstall.err")
            $result.steps.strictUninstallExitCode = [int]$uninstall.exitCode
            $result.cleanup = [ordered]@{
                strictUninstallVerified = $true
                forcedProcessCountAfterFailure = $forcedCount
                installRootRemoved = [bool]$uninstall.installRootRemoved
                remainingProcessCount = [int]$uninstall.processCount
                remainingRegistryCount = [int]$uninstall.registryCount
                dataSentinelPreserved = [bool]$uninstall.dataSentinelPreserved
                dataSentinelSha256 = $sentinelSha256
            }
        } catch {
            $fatalFailure = $true
            $result.failedPhase = "strict-uninstall"
            $result.failureCode = "o0_full_local_correctness_failed"
            $result.cleanup = [ordered]@{
                strictUninstallVerified = $false
                forcedProcessCountAfterFailure = $forcedCount
            }
        }
    } else {
        $result.cleanup = [ordered]@{
            strictUninstallVerified = $false
            notAttemptedBecauseNoInstall = $true
        }
    }
    if ($lockStream) {
        $lockStream.Dispose()
    }
    $result.ok = (
        -not $fatalFailure -and
        $measurementPassed -and
        $result.cleanup -and
        [bool]$result.cleanup.strictUninstallVerified
    )
    if (-not $result.ok -and -not $result.failureCode) {
        $result.failureCode = "o0_full_local_correctness_failed"
    }
    if (-not $result.ok -and -not $result.failedPhase) {
        $result.failedPhase = $failurePhase
    }
    if (
        (Test-StrictDescendantPath -Path $OutputPath -Parent $WorkRoot) -and
        (
            (Test-Path -LiteralPath $WorkRoot -PathType Container) -or
            (Test-StrictDescendantPath -Path $WorkRoot -Parent $allowedScratchRoot)
        )
    ) {
        Write-AtomicJson -Path $OutputPath -Value $result
    }
}

Write-Output $OutputPath
if ($result.ok) {
    exit 0
}
exit 2
