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

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $resolvedLogPath) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $writer = [System.IO.StreamWriter]::new($resolvedLogPath, $false, $encoding)
    $quotedConfigPath = $resolvedConfigPath.Replace('"', '\"')
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
