[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,
    [Parameter(Mandatory = $true)]
    [string]$BasePython,
    [ValidatePattern("^[a-z0-9][a-z0-9._-]{0,63}$")]
    [string]$EnvironmentName = "baseline",
    [string]$RequirementsBase = "requirements-base.txt",
    [string]$RequirementsBuild = "requirements-build.txt",
    [switch]$RebuildWheelhouse,
    [switch]$RecreateEnvironment
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

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Resolve-PlainFileUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) {
        [System.IO.Path]::GetFullPath($Path)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $Root $Path))
    }
    $candidate = Assert-UnderRoot -Root $Root -Path $candidate -Label $Label
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "$Label was not found."
    }
    $item = Get-Item -LiteralPath $candidate -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a reparse point."
    }
    $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
    return Assert-UnderRoot -Root $Root -Path $resolved -Label $Label
}

$canonicalRunId = Resolve-CanonicalUuid -Value $RunId
$resolvedBasePython = (Resolve-Path -LiteralPath $BasePython -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $resolvedBasePython -PathType Leaf)) {
    throw "BasePython must resolve to a Python executable file."
}

$resolvedRequirementsBase = Resolve-PlainFileUnderRoot -Root $RepoRoot -Path $RequirementsBase -Label "Base requirements"
$resolvedRequirementsBuild = Resolve-PlainFileUnderRoot -Root $RepoRoot -Path $RequirementsBuild -Label "Build requirements"
if ($EnvironmentName -eq "baseline") {
    $canonicalRequirementsBase = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot "requirements-base.txt"))
    $canonicalRequirementsBuild = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot "requirements-build.txt"))
    if (
        -not $resolvedRequirementsBase.Equals($canonicalRequirementsBase, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not $resolvedRequirementsBuild.Equals($canonicalRequirementsBuild, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "The baseline environment requires the canonical repository requirements files."
    }
}
$runRoot = Join-Path $RepoRoot "autoresearch-results\installer-size\$canonicalRunId"
$environmentParent = Join-Path $runRoot "environments"
$environmentRoot = Assert-UnderRoot -Root $runRoot -Path (Join-Path $environmentParent $EnvironmentName) -Label "Environment"
$venvRoot = Assert-UnderRoot -Root $environmentRoot -Path (Join-Path $environmentRoot ".venv") -Label "Virtual environment"
$wheelhouseRoot = Assert-UnderRoot -Root $runRoot -Path (Join-Path $runRoot "wheelhouse") -Label "Wheelhouse"
$wheelhouseManifest = Join-Path $runRoot "wheelhouse-manifest.json"
$environmentManifest = Join-Path $environmentRoot "environment-manifest.json"

$null = Assert-NoReparsePath -Root $RepoRoot -Path $runRoot -Label "Research run root"
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
$null = Assert-NoReparsePath -Root $RepoRoot -Path $runRoot -Label "Research run root"
if ($EnvironmentName -eq "baseline") {
    $snapshotRoot = Assert-UnderRoot -Root $runRoot -Path (Join-Path $runRoot "snapshots") -Label "Baseline requirement snapshots"
    New-Item -ItemType Directory -Force -Path $snapshotRoot | Out-Null
    $null = Assert-NoReparsePath -Root $runRoot -Path $snapshotRoot -Label "Baseline requirement snapshots" -Recurse
    $snapshotPairs = @(
        @($resolvedRequirementsBase, (Join-Path $snapshotRoot "requirements-base.txt")),
        @($resolvedRequirementsBuild, (Join-Path $snapshotRoot "requirements-build.txt"))
    )
    foreach ($pair in $snapshotPairs) {
        $source = [string]$pair[0]
        $destination = [string]$pair[1]
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            $item = Get-Item -LiteralPath $destination -Force
            if (
                ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
                (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne
                    (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
            ) {
                throw "The immutable baseline requirement snapshot differs from repository initialization."
            }
        } else {
            Copy-Item -LiteralPath $source -Destination $destination
        }
    }
    $resolvedRequirementsBase = Resolve-PlainFileUnderRoot -Root $runRoot -Path (Join-Path $snapshotRoot "requirements-base.txt") -Label "Baseline base requirements snapshot"
    $resolvedRequirementsBuild = Resolve-PlainFileUnderRoot -Root $runRoot -Path (Join-Path $snapshotRoot "requirements-build.txt") -Label "Baseline build requirements snapshot"
}
$null = Assert-NoReparsePath -Root $runRoot -Path $wheelhouseRoot -Label "Wheelhouse"
$null = Assert-NoReparsePath -Root $runRoot -Path $environmentRoot -Label "Environment"

if ($RebuildWheelhouse -and (Test-Path -LiteralPath $wheelhouseRoot)) {
    $verifiedWheelhouse = Assert-NoReparsePath -Root $runRoot -Path $wheelhouseRoot -Label "Wheelhouse cleanup" -Recurse
    Remove-Item -LiteralPath $verifiedWheelhouse -Recurse -Force
}

if (-not (Test-Path -LiteralPath $wheelhouseRoot -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $wheelhouseRoot | Out-Null
    Invoke-Checked -Label "Building the locked research wheelhouse" -Command {
        & $resolvedBasePython -m pip wheel `
            --wheel-dir $wheelhouseRoot `
            --prefer-binary `
            -r $resolvedRequirementsBase `
            -r $resolvedRequirementsBuild
    }
} elseif (-not (Test-Path -LiteralPath $wheelhouseManifest -PathType Leaf)) {
    $existingWheels = @(Get-ChildItem -LiteralPath $wheelhouseRoot -Filter *.whl -File)
    if ($existingWheels.Count -gt 0) {
        throw "A pre-existing wheelhouse without an attested manifest cannot be reused. Use -RebuildWheelhouse."
    }
    Invoke-Checked -Label "Building the locked research wheelhouse" -Command {
        & $resolvedBasePython -m pip wheel `
            --wheel-dir $wheelhouseRoot `
            --prefer-binary `
            -r $resolvedRequirementsBase `
            -r $resolvedRequirementsBuild
    }
}

if ($RecreateEnvironment -and (Test-Path -LiteralPath $environmentRoot)) {
    $verifiedEnvironment = Assert-NoReparsePath -Root $runRoot -Path $environmentRoot -Label "Environment cleanup" -Recurse
    Remove-Item -LiteralPath $verifiedEnvironment -Recurse -Force
}

if (Test-Path -LiteralPath $environmentRoot) {
    throw "Research environment already exists. Use a new EnvironmentName or -RecreateEnvironment."
}

New-Item -ItemType Directory -Force -Path $environmentRoot | Out-Null
Invoke-Checked -Label "Creating the research virtual environment" -Command {
    & $resolvedBasePython -m venv $venvRoot
}

$venvPython = Join-Path $venvRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "The research virtual environment did not produce Scripts\python.exe."
}

Invoke-Checked -Label "Installing runtime requirements from the locked wheelhouse" -Command {
    & $venvPython -m pip install `
        --no-index `
        --find-links $wheelhouseRoot `
        -r $resolvedRequirementsBase
}
Invoke-Checked -Label "Installing build requirements from the locked wheelhouse" -Command {
    & $venvPython -m pip install `
        --no-index `
        --find-links $wheelhouseRoot `
        -r $resolvedRequirementsBuild
}
Invoke-Checked -Label "Validating the research Python dependency graph" -Command {
    & $venvPython -m pip check
}

# Importing comtypes.client creates the otherwise wheel-external
# comtypes/gen/__init__.py package on first use.  PyInstaller imports that
# module while analysing Scriber, so leaving this mutation to the first
# replica makes the second replica contain one additional PYZ entry.  Seed the
# generated package before the immutable environment manifest is written so
# every baseline, candidate, and final replica starts from the same graph.
Invoke-Checked -Label "Seeding the deterministic comtypes generated package" -Command {
    & $venvPython -c "import comtypes.client, comtypes.gen; assert comtypes.gen.__file__"
}

$manifestWriter = Join-Path $RepoRoot "scripts\write_installer_research_environment_manifest.py"
$manifestArgs = @(
    $manifestWriter,
    "--run-id", $canonicalRunId,
    "--environment-name", $EnvironmentName,
    "--wheelhouse", $wheelhouseRoot,
    "--requirements", $resolvedRequirementsBase,
    "--requirements", $resolvedRequirementsBuild,
    "--output", $environmentManifest
)
Invoke-Checked -Label "Attesting the research Python environment" -Command {
    & $venvPython @manifestArgs | Out-Null
}

$currentWheelhouseManifest = Get-Content -LiteralPath $environmentManifest -Raw | ConvertFrom-Json
if (Test-Path -LiteralPath $wheelhouseManifest -PathType Leaf) {
    $attestedWheelhouse = Get-Content -LiteralPath $wheelhouseManifest -Raw | ConvertFrom-Json
    if (
        [string]$attestedWheelhouse.wheelhouseSha256 -ne [string]$currentWheelhouseManifest.wheelhouseSha256
    ) {
        throw "The wheelhouse drifted from its run attestation."
    }
    if (
        $EnvironmentName -eq "baseline" -and
        [string]$attestedWheelhouse.requirementsSha256 -ne [string]$currentWheelhouseManifest.requirementsSha256
    ) {
        throw "The baseline requirements drifted from the run wheelhouse attestation."
    }
} else {
    if ($EnvironmentName -ne "baseline") {
        throw "The immutable wheelhouse must be established by the baseline environment first."
    }
    [ordered]@{
        schemaVersion = 1
        kind = "scriber-installer-research-wheelhouse"
        runId = $canonicalRunId
        requirements = @($currentWheelhouseManifest.requirements)
        requirementsSha256 = [string]$currentWheelhouseManifest.requirementsSha256
        wheelhouse = @($currentWheelhouseManifest.wheelhouse)
        wheelhouseSha256 = [string]$currentWheelhouseManifest.wheelhouseSha256
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $wheelhouseManifest -Encoding utf8
}

[ordered]@{
    ok = $true
    schemaVersion = 1
    runId = $canonicalRunId
    environmentName = $EnvironmentName
    pythonExecutable = $venvPython
    environmentManifest = $environmentManifest
    wheelhouseManifest = $wheelhouseManifest
    productDependenciesSha256 = [string]$currentWheelhouseManifest.productDependenciesSha256
} | ConvertTo-Json -Depth 6
