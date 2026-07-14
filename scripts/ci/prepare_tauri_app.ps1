param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("TypeCheck", "BuildBinary")]
    [string]$Mode,
    [string]$RepoRoot = "",
    [string]$ConfigPath = "",
    [string]$TauriLogPath = ""
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
$frontendRoot = Join-Path $RepoRoot "Frontend"

function Convert-ToFullPath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $Path))
}

function Assert-UnderRoot {
    param([string]$Path, [string]$Label)
    $fullPath = Convert-ToFullPath -Path $Path
    $rootWithSeparator = $RepoRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (
        -not $fullPath.Equals($RepoRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
        -not $fullPath.StartsWith($rootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "$Label must stay under the repository root: $fullPath"
    }
    return $fullPath
}

function New-CompileOnlyTauriConfig {
    param([string]$SourcePath)

    $source = Get-Content -LiteralPath $SourcePath -Raw | ConvertFrom-Json
    if (-not $source.bundle) {
        throw "Generated Tauri config does not contain a bundle section: $SourcePath"
    }

    # `tauri build --no-bundle` still validates bundle resources even though
    # it does not package them. The backend resource directory is prepared by
    # PyInstaller in parallel, so compile the executable with an otherwise
    # identical config whose resource map is intentionally empty. The later
    # `tauri bundle` command uses the original config after the sidecar join
    # and therefore revalidates and packages the complete backend tree.
    # Tauri merges `--config` with the checked-in base config. An empty object
    # would preserve the existing resource map under JSON Merge Patch rules;
    # an empty array changes the value type and therefore replaces it.
    $resourcesProperty = $source.bundle.PSObject.Properties["resources"]
    if ($null -eq $resourcesProperty) {
        Add-Member `
            -InputObject $source.bundle `
            -MemberType NoteProperty `
            -Name "resources" `
            -Value @()
    } else {
        $source.bundle.resources = @()
    }
    $destination = Join-Path (Split-Path -Parent $SourcePath) "tauri.compile-only.conf.json"
    $destination = Assert-UnderRoot -Path $destination -Label "Compile-only Tauri config"
    $json = $source | ConvertTo-Json -Depth 100
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($destination, $json, $encoding)
    return $destination
}

Push-Location $frontendRoot
try {
    if ($Mode -eq "TypeCheck") {
        npm run check
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend type check failed with exit code $LASTEXITCODE."
        }
        return
    }

    if (-not $ConfigPath) {
        throw "BuildBinary mode requires -ConfigPath."
    }
    if (-not $TauriLogPath) {
        throw "BuildBinary mode requires -TauriLogPath."
    }
    $resolvedConfigPath = Assert-UnderRoot -Path $ConfigPath -Label "Tauri config"
    $resolvedLogPath = Assert-UnderRoot -Path $TauriLogPath -Label "Tauri log"
    if (-not (Test-Path -LiteralPath $resolvedConfigPath -PathType Leaf)) {
        throw "Generated Tauri config was not found: $resolvedConfigPath"
    }

    $compileConfigPath = New-CompileOnlyTauriConfig -SourcePath $resolvedConfigPath

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $resolvedLogPath) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $writer = [System.IO.StreamWriter]::new($resolvedLogPath, $false, $encoding)
    $quotedConfigPath = $compileConfigPath.Replace('"', '\"')
    $command = 'npm run tauri:build -- --no-bundle --config "{0}" --ci 2>&1' -f $quotedConfigPath
    try {
        cmd.exe /d /s /c $command |
            ForEach-Object {
                $line = $_.ToString()
                $writer.WriteLine(("{0}`t{1}" -f (Get-Date).ToUniversalTime().ToString("o"), $line))
                Write-Host $line
            }
    } finally {
        $writer.Dispose()
    }
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Parallel Tauri app binary build failed with exit code $exitCode."
    }
} finally {
    Pop-Location
}
