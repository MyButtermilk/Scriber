<#
.SYNOPSIS
Runs the physical microphone hardware matrix in a guided Windows flow.

.DESCRIPTION
This script orchestrates the eight manual microphone hardware scenarios used by
the hybrid release gate. It still requires a human operator to connect,
disconnect, or reconfigure devices, but it standardizes output paths and runs
the final matrix validator at the end.
#>

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$BaseUrl = "http://127.0.0.1:8765",
    [string]$Token = "",
    [string]$OutputDir = "tmp\hybrid-baseline",
    [double]$WaitSec = 60,
    [double]$PollSec = 1,
    [string]$UsbLabel = "",
    [string]$DockLabel = "",
    [string]$BluetoothLabel = "",
    [string]$FavoriteLabel = "",
    [switch]$AssumeCompleted,
    [switch]$PlanOnly,
    [switch]$SkipValidation
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path $RepoRoot).Path
if ([System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
} else {
    $OutputDir = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $OutputDir))
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$scenarios = @(
    [pscustomobject]@{
        Scenario = "usb-add"
        LabelParameter = "UsbLabel"
        LabelValue = $UsbLabel
        Flags = @("--expect-added", $UsbLabel)
        Instruction = "Plug in the USB microphone matching '$UsbLabel', wait until Windows exposes it, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "usb-remove"
        LabelParameter = "UsbLabel"
        LabelValue = $UsbLabel
        Flags = @("--expect-removed", $UsbLabel)
        Instruction = "Unplug the USB microphone matching '$UsbLabel', wait until Windows removes it, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "dock-disconnect"
        LabelParameter = "DockLabel"
        LabelValue = $DockLabel
        Flags = @("--expect-removed", $DockLabel)
        Instruction = "Disconnect the laptop from the dock matching '$DockLabel', wait for dock audio endpoints to disappear, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "dock-connect"
        LabelParameter = "DockLabel"
        LabelValue = $DockLabel
        Flags = @("--expect-added", $DockLabel)
        Instruction = "Reconnect the laptop to the dock matching '$DockLabel', wait for dock audio endpoints to appear, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "bluetooth-add"
        LabelParameter = "BluetoothLabel"
        LabelValue = $BluetoothLabel
        Flags = @("--expect-added", $BluetoothLabel)
        Instruction = "Connect the Bluetooth microphone/headset matching '$BluetoothLabel', wait until Windows exposes it, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "bluetooth-remove"
        LabelParameter = "BluetoothLabel"
        LabelValue = $BluetoothLabel
        Flags = @("--expect-removed", $BluetoothLabel)
        Instruction = "Disconnect the Bluetooth microphone/headset matching '$BluetoothLabel', wait until Windows removes it, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "default-mic-change"
        LabelParameter = ""
        LabelValue = ""
        Flags = @("--expect-default-changed")
        Instruction = "Change the Windows default input device, wait for the default marker to move, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "favorite-fallback"
        LabelParameter = "FavoriteLabel"
        LabelValue = $FavoriteLabel
        Flags = @("--expect-favorite-fallback", "--expect-removed", $FavoriteLabel)
        Instruction = "Make the configured favorite microphone matching '$FavoriteLabel' unavailable, wait for Scriber to fall back, then press Enter."
    }
)

function Convert-ToDisplayCommand {
    param([string[]]$CommandArgs)

    $displayArgs = @()
    for ($i = 0; $i -lt $CommandArgs.Count; $i++) {
        if ($CommandArgs[$i] -eq "--token" -and ($i + 1) -lt $CommandArgs.Count) {
            $displayArgs += "--token"
            $displayArgs += "<session token>"
            $i += 1
            continue
        }
        $displayArgs += $CommandArgs[$i]
    }
    return (($displayArgs | ForEach-Object {
        if ($_ -match "\s" -or $_ -eq "") {
            '"' + ($_ -replace '"', '\"') + '"'
        } else {
            $_
        }
    }) -join " ")
}

function New-SmokeArgs {
    param([pscustomobject]$Scenario)

    $outputPath = Join-Path $OutputDir ("microphone-hardware-{0}.json" -f $Scenario.Scenario)
    $args = @(
        "scripts\smoke_microphone_hardware_matrix.py",
        "--base-url",
        $BaseUrl,
        "--scenario",
        $Scenario.Scenario,
        "--wait-sec",
        ([string]$WaitSec),
        "--poll-sec",
        ([string]$PollSec),
        "--instruction",
        $Scenario.Instruction,
        "--output",
        $outputPath
    )
    if ($Token) {
        $args += @("--token", $Token)
    }
    if ($AssumeCompleted) {
        $args += "--assume-completed"
    }
    $args += $Scenario.Flags
    return $args
}

function New-Plan {
    $entries = @()
    foreach ($scenario in $scenarios) {
        $args = New-SmokeArgs -Scenario $scenario
        $entries += [pscustomobject]@{
            scenario = $scenario.Scenario
            instruction = $scenario.Instruction
            labelParameter = $scenario.LabelParameter
            labelValueConfigured = [bool]$scenario.LabelValue
            output = (Join-Path $OutputDir ("microphone-hardware-{0}.json" -f $scenario.Scenario))
            command = ("python " + (Convert-ToDisplayCommand -CommandArgs $args))
        }
    }
    return [pscustomobject]@{
        ok = $true
        planOnly = [bool]$PlanOnly
        baseUrl = $BaseUrl
        outputDir = $OutputDir
        waitSec = $WaitSec
        pollSec = $PollSec
        scenarios = @($entries)
        validationCommand = "python scripts\validate_microphone_hardware_matrix.py --input-dir `"$OutputDir`" --output `"$((Join-Path $OutputDir "microphone-hardware-matrix-validation.json"))`""
    }
}

function Assert-RequiredLabels {
    foreach ($scenario in $scenarios) {
        if ($scenario.LabelParameter -and -not $scenario.LabelValue) {
            throw "Missing -$($scenario.LabelParameter) for scenario '$($scenario.Scenario)'."
        }
    }
}

$plan = New-Plan
$planPath = Join-Path $OutputDir "microphone-hardware-matrix-runner-plan.json"
$planJson = $plan | ConvertTo-Json -Depth 8 -Compress
Set-Content -LiteralPath $planPath -Value $planJson -Encoding UTF8

if ($PlanOnly) {
    $planJson
    exit 0
}

Assert-RequiredLabels

Push-Location $RepoRoot
try {
    foreach ($scenario in $scenarios) {
        Write-Host "==> $($scenario.Scenario)"
        $args = New-SmokeArgs -Scenario $scenario
        python @args
        if ($LASTEXITCODE -ne 0) {
            throw "Microphone hardware scenario '$($scenario.Scenario)' failed with exit code $LASTEXITCODE."
        }
    }

    if (-not $SkipValidation) {
        $validationOutput = Join-Path $OutputDir "microphone-hardware-matrix-validation.json"
        python scripts\validate_microphone_hardware_matrix.py --input-dir $OutputDir --output $validationOutput
        if ($LASTEXITCODE -ne 0) {
            throw "Microphone hardware matrix validation failed with exit code $LASTEXITCODE."
        }
    }
} finally {
    Pop-Location
}

$summary = [pscustomobject]@{
    ok = $true
    planPath = $planPath
    outputDir = $OutputDir
    validationPath = $(if ($SkipValidation) { "" } else { Join-Path $OutputDir "microphone-hardware-matrix-validation.json" })
}
$summary | ConvertTo-Json -Depth 5 -Compress
