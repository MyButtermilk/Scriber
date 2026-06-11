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
    [switch]$RequireRustEndpointInventory,
    [switch]$RequireDeviceRefreshEvidence,
    [switch]$ForceRefreshEachPoll,
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
        Flags = @("--expect-added", $(if ($UsbLabel) { $UsbLabel } else { "<UsbLabel>" }))
        Instruction = "Plug in the USB microphone matching '$(if ($UsbLabel) { $UsbLabel } else { "<UsbLabel>" })', wait until Windows exposes it, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "usb-remove"
        LabelParameter = "UsbLabel"
        LabelValue = $UsbLabel
        Flags = @("--expect-removed", $(if ($UsbLabel) { $UsbLabel } else { "<UsbLabel>" }))
        Instruction = "Unplug the USB microphone matching '$(if ($UsbLabel) { $UsbLabel } else { "<UsbLabel>" })', wait until Windows removes it, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "dock-disconnect"
        LabelParameter = "DockLabel"
        LabelValue = $DockLabel
        Flags = @("--expect-removed", $(if ($DockLabel) { $DockLabel } else { "<DockLabel>" }))
        Instruction = "Disconnect the laptop from the dock matching '$(if ($DockLabel) { $DockLabel } else { "<DockLabel>" })', wait for dock audio endpoints to disappear, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "dock-connect"
        LabelParameter = "DockLabel"
        LabelValue = $DockLabel
        Flags = @("--expect-added", $(if ($DockLabel) { $DockLabel } else { "<DockLabel>" }))
        Instruction = "Reconnect the laptop to the dock matching '$(if ($DockLabel) { $DockLabel } else { "<DockLabel>" })', wait for dock audio endpoints to appear, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "bluetooth-add"
        LabelParameter = "BluetoothLabel"
        LabelValue = $BluetoothLabel
        Flags = @("--expect-added", $(if ($BluetoothLabel) { $BluetoothLabel } else { "<BluetoothLabel>" }))
        Instruction = "Connect the Bluetooth microphone/headset matching '$(if ($BluetoothLabel) { $BluetoothLabel } else { "<BluetoothLabel>" })', wait until Windows exposes it, then press Enter."
    },
    [pscustomobject]@{
        Scenario = "bluetooth-remove"
        LabelParameter = "BluetoothLabel"
        LabelValue = $BluetoothLabel
        Flags = @("--expect-removed", $(if ($BluetoothLabel) { $BluetoothLabel } else { "<BluetoothLabel>" }))
        Instruction = "Disconnect the Bluetooth microphone/headset matching '$(if ($BluetoothLabel) { $BluetoothLabel } else { "<BluetoothLabel>" })', wait until Windows removes it, then press Enter."
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
        Flags = @("--expect-favorite-fallback", "--expect-removed", $(if ($FavoriteLabel) { $FavoriteLabel } else { "<FavoriteLabel>" }))
        Instruction = "Make the configured favorite microphone matching '$(if ($FavoriteLabel) { $FavoriteLabel } else { "<FavoriteLabel>" })' unavailable, wait for Scriber to fall back, then press Enter."
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

function Write-Utf8NoBomJson {
    param(
        [string]$Path,
        [string]$Json
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Json, $encoding)
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
    if ($ForceRefreshEachPoll) {
        $args += "--force-refresh-each-poll"
    }
    $args += $Scenario.Flags
    return $args
}

function Get-MissingLabelParameters {
    $seen = @{}
    $missing = @()
    foreach ($scenario in $scenarios) {
        if ($scenario.LabelParameter -and -not $scenario.LabelValue -and -not $seen.ContainsKey($scenario.LabelParameter)) {
            $missing += $scenario.LabelParameter
            $seen[$scenario.LabelParameter] = $true
        }
    }
    return $missing
}

function New-Plan {
    $missingLabelParameters = @(Get-MissingLabelParameters)
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
    $validationArgs = @(
        "scripts\validate_microphone_hardware_matrix.py",
        "--input-dir",
        $OutputDir,
        "--output",
        (Join-Path $OutputDir "microphone-hardware-matrix-validation.json")
    )
    if ($RequireRustEndpointInventory) {
        $validationArgs += "--require-rust-endpoint-inventory"
    }
    if ($RequireDeviceRefreshEvidence) {
        $validationArgs += "--require-device-refresh-evidence"
    }
    return [pscustomobject]@{
        ok = $true
        planOnly = [bool]$PlanOnly
        baseUrl = $BaseUrl
        outputDir = $OutputDir
        waitSec = $WaitSec
        pollSec = $PollSec
        forceRefreshEachPoll = [bool]$ForceRefreshEachPoll
        requireRustEndpointInventory = [bool]$RequireRustEndpointInventory
        requireDeviceRefreshEvidence = [bool]$RequireDeviceRefreshEvidence
        readyForPhysicalRun = ($missingLabelParameters.Count -eq 0)
        missingLabelParameters = $missingLabelParameters
        scenarios = @($entries)
        validationCommand = ("python " + (Convert-ToDisplayCommand -CommandArgs $validationArgs))
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
Write-Utf8NoBomJson -Path $planPath -Json $planJson

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
        $validationArgs = @(
            "scripts\validate_microphone_hardware_matrix.py",
            "--input-dir",
            $OutputDir,
            "--output",
            $validationOutput
        )
        if ($RequireRustEndpointInventory) {
            $validationArgs += "--require-rust-endpoint-inventory"
        }
        if ($RequireDeviceRefreshEvidence) {
            $validationArgs += "--require-device-refresh-evidence"
        }
        python @validationArgs
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
