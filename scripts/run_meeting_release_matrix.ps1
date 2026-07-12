<#
.SYNOPSIS
Prepares and validates the real installed-app Meeting release matrix.

.DESCRIPTION
This runner never fabricates physical evidence. It can create non-passing draft
reports for every required scenario and validates only completed
meeting-release-evidence-*.json reports. Supporting artifacts stay portable,
relative, redacted, and SHA-256 bound.
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$OutputDir = "tmp\meeting-release-matrix",
    [string]$AppVersion = "",
    [string]$InstallerPath = "",
    [switch]$PlanOnly,
    [switch]$InitializeDrafts,
    [switch]$Validate,
    [switch]$AllowPartial,
    [switch]$AllowUnsignedInstaller
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path $RepoRoot).Path
if ([System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
} else {
    $OutputDir = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $OutputDir))
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

function Write-Utf8NoBomJson {
    param([string]$Path, [object]$Value)
    $json = $Value | ConvertTo-Json -Depth 12
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json, $encoding)
}

function Get-ScriberVersion {
    $source = Get-Content (Join-Path $RepoRoot "src\version.py") -Raw
    $match = [regex]::Match($source, '__version__\s*=\s*"([^"]+)"')
    if (-not $match.Success) {
        throw "Could not read __version__ from src\version.py."
    }
    return $match.Groups[1].Value
}

function Get-Sha256Hex {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            $bytes = $sha256.ComputeHash($stream)
            return ([System.BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
        } finally {
            $sha256.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

if (-not $AppVersion) {
    $AppVersion = Get-ScriberVersion
}
if ($AppVersion -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Invalid -AppVersion '$AppVersion'."
}

$resolvedInstaller = ""
$installerSha256 = "<installer-sha256>"
$authenticodeStatus = "not-checked"
$signedInstaller = $false
if ($InstallerPath) {
    $candidate = if ([System.IO.Path]::IsPathRooted($InstallerPath)) {
        $InstallerPath
    } else {
        Join-Path $RepoRoot $InstallerPath
    }
    $resolvedInstaller = (Resolve-Path $candidate).Path
    $installerSha256 = Get-Sha256Hex -Path $resolvedInstaller
    try {
        $signature = Get-AuthenticodeSignature -FilePath $resolvedInstaller
        $authenticodeStatus = [string]$signature.Status
        $signedInstaller = $signature.Status -eq [System.Management.Automation.SignatureStatus]::Valid
    } catch {
        $authenticodeStatus = "check-unavailable"
        $signedInstaller = $false
    }
}

$profiles = @(
    "teams-laptop-speakerphone",
    "zoom-wired-headset",
    "meet-bluetooth-headset",
    "teams-usb-microphone",
    "zoom-default-device-switch",
    "provider-network-reconnect",
    "backend-crash-recovery",
    "shell-exit-resume",
    "corrupt-chunk-recovery",
    "disk-full-recovery",
    "outlook-work-school",
    "outlook-microsoft-personal",
    "recording-60m",
    "stability-2h",
    "voiceprint-held-corpus",
    "support-bundle-privacy",
    "eu-voiceprint-privacy-review",
    "automated-regression-suite",
    "signed-release"
)

$entries = @()
foreach ($profile in $profiles) {
    $draft = Join-Path $OutputDir ("meeting-release-draft-{0}.json" -f $profile)
    $final = Join-Path $OutputDir ("meeting-release-evidence-{0}.json" -f $profile)
    $entries += [pscustomobject]@{
        profile = $profile
        draft = $draft
        completedEvidence = $final
        instruction = "Run the named scenario in the installed app, attach redacted supporting files under this evidence directory, fill objective measurements, set completed/operatorConfirmed true, and save as the completedEvidence path."
    }
}

$validationPath = Join-Path $OutputDir "meeting-release-matrix-validation.json"
$validationArgs = @(
    "scripts\validate_meeting_release_matrix.py",
    "--input-dir", $OutputDir,
    "--expected-app-version", $AppVersion,
    "--output", $validationPath
)
if ($AllowPartial) {
    $validationArgs += "--allow-partial"
}
if ($AllowUnsignedInstaller) {
    $validationArgs += "--allow-unsigned-installer"
}

$plan = [pscustomobject]@{
    schemaVersion = 1
    kind = "scriber-meeting-release-matrix-runner-plan"
    ok = $true
    planOnly = [bool]$PlanOnly
    outputDir = $OutputDir
    appVersion = $AppVersion
    installerPath = $resolvedInstaller
    installerSha256 = $installerSha256
    authenticodeStatus = $authenticodeStatus
    signedInstaller = $signedInstaller
    readyForDraftInitialization = [bool]$resolvedInstaller
    profiles = $entries
    validationCommand = "python " + ($validationArgs -join " ")
    privacyRule = "Do not store audio, transcript text, tokens, raw endpoint IDs, webhook secrets, voiceprint BLOBs, embeddings, or personal data in matrix JSON."
}
$planPath = Join-Path $OutputDir "meeting-release-matrix-runner-plan.json"
Write-Utf8NoBomJson -Path $planPath -Value $plan

if ($PlanOnly) {
    $plan | ConvertTo-Json -Depth 12 -Compress
    exit 0
}

Push-Location $RepoRoot
try {
    if ($InitializeDrafts) {
        if (-not $resolvedInstaller) {
            throw "-InitializeDrafts requires -InstallerPath so reports are bound to a real installer SHA-256."
        }
        foreach ($entry in $entries) {
            if (Test-Path -LiteralPath $entry.draft) {
                $keepDraft = $false
                $oldDraftSha = "unknown"
                try {
                    $existingDraft = Get-Content -LiteralPath $entry.draft -Raw | ConvertFrom-Json
                    $oldDraftSha = [string]$existingDraft.build.installerSha256
                    $keepDraft = (
                        [string]$existingDraft.appVersion -eq $AppVersion -and
                        $oldDraftSha -eq $installerSha256
                    )
                } catch {
                    $keepDraft = $false
                }
                if ($keepDraft) {
                    Write-Host "Keeping current installer-bound draft $($entry.draft)"
                    continue
                }
                $staleDir = Join-Path $OutputDir "stale-drafts"
                New-Item -ItemType Directory -Force -Path $staleDir | Out-Null
                $safeOldSha = if ($oldDraftSha -match '^[0-9a-fA-F]{12,64}$') {
                    $oldDraftSha.Substring(0, 12).ToLowerInvariant()
                } else {
                    "unknown"
                }
                $stalePath = Join-Path $staleDir (
                    "meeting-release-draft-{0}-{1}.json" -f $entry.profile, $safeOldSha
                )
                if (Test-Path -LiteralPath $stalePath) {
                    $stalePath = Join-Path $staleDir (
                        "meeting-release-draft-{0}-{1}-{2}.json" -f `
                            $entry.profile, $safeOldSha, [System.Guid]::NewGuid().ToString("N")
                    )
                }
                Move-Item -LiteralPath $entry.draft -Destination $stalePath
                Write-Host "Archived stale installer-bound draft at $stalePath"
            }
            $args = @(
                "scripts\new_meeting_release_evidence.py",
                "--profile", $entry.profile,
                "--app-version", $AppVersion,
                "--installer-sha256", $installerSha256,
                "--output", $entry.draft
            )
            if ($signedInstaller) {
                $args += "--signed-installer"
            }
            python @args
            if ($LASTEXITCODE -ne 0) {
                throw "Draft initialization failed for '$($entry.profile)' with exit code $LASTEXITCODE."
            }
        }
    }

    $effectiveValidate = [bool]($Validate -or -not $InitializeDrafts)
    if ($effectiveValidate) {
        python @validationArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Meeting release matrix validation failed with exit code $LASTEXITCODE."
        }
    }
} finally {
    Pop-Location
}

$summary = [pscustomobject]@{
    ok = $true
    planPath = $planPath
    outputDir = $OutputDir
    initializedDrafts = [bool]$InitializeDrafts
    validated = [bool]($Validate -or -not $InitializeDrafts)
    validationPath = $(if ($Validate -or -not $InitializeDrafts) { $validationPath } else { "" })
    signedInstaller = $signedInstaller
    authenticodeStatus = $authenticodeStatus
}
$summary | ConvertTo-Json -Depth 5 -Compress
