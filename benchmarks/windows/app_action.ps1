param(
    [Parameter(Mandatory = $true)]
    [int]$ProcessId,
    [Parameter(Mandatory = $true)]
    [long]$ProcessCreationTime100ns,
    [string]$WindowTitle = "Scriber",
    [string]$ControlName = "",
    [string]$ControlAutomationId = "",
    [ValidateSet("Exact", "Prefix")]
    [string]$ControlNameMatch = "Exact",
    [ValidateSet("", "Button", "Hyperlink", "ListItem", "MenuItem", "TabItem")]
    [string]$ControlType = "",
    [string]$InputMarkerPath = "",
    [string]$OutputPath = "",
    [double]$TimeoutSec = 10
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

if (-not $ControlName -and -not $ControlAutomationId) {
    throw "ControlName or ControlAutomationId is required."
}

function Get-TextSha256 {
    param([string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
    } finally {
        $sha.Dispose()
    }
}

function Get-ProcessCreationTime100ns {
    param([int]$TargetProcessId)
    try {
        return [long](Get-Process -Id $TargetProcessId -ErrorAction Stop).StartTime.ToUniversalTime().ToFileTimeUtc()
    } catch {
        return $null
    }
}

function Resolve-ControlType {
    param([string]$Name)
    switch ($Name) {
        "Button" { return [System.Windows.Automation.ControlType]::Button }
        "Hyperlink" { return [System.Windows.Automation.ControlType]::Hyperlink }
        "ListItem" { return [System.Windows.Automation.ControlType]::ListItem }
        "MenuItem" { return [System.Windows.Automation.ControlType]::MenuItem }
        "TabItem" { return [System.Windows.Automation.ControlType]::TabItem }
        default { return $null }
    }
}

$actualCreationTime = Get-ProcessCreationTime100ns -TargetProcessId $ProcessId
if ($actualCreationTime -ne $ProcessCreationTime100ns) {
    throw "The target process generation no longer matches the requested App UX sample."
}

$deadline = (Get-Date).AddSeconds([Math]::Max(0.5, $TimeoutSec))
$window = $null
$control = $null
$matchingControlCount = 0
$scrolledIntoView = $false
do {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $windowConditions = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            $WindowTitle
        )),
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
            $ProcessId
        ))
    )
    $windows = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Children,
        $windowConditions
    )
    $eligibleWindows = @()
    foreach ($candidate in $windows) {
        try {
            $bounds = $candidate.Current.BoundingRectangle
            if (-not $candidate.Current.IsOffscreen -and $bounds.Width -gt 0 -and $bounds.Height -gt 0) {
                $eligibleWindows += $candidate
            }
        } catch {}
    }
    if ($eligibleWindows.Count -eq 1) {
        $window = $eligibleWindows[0]
        $conditions = New-Object System.Collections.Generic.List[System.Windows.Automation.Condition]
        if ($ControlName -and $ControlNameMatch -eq "Exact") {
            $conditions.Add((New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                $ControlName
            )))
        }
        if ($ControlAutomationId) {
            $conditions.Add((New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
                $ControlAutomationId
            )))
        }
        $requestedControlType = Resolve-ControlType -Name $ControlType
        if ($requestedControlType) {
            $conditions.Add((New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                $requestedControlType
            )))
        }
        $controlCondition = if ($conditions.Count -eq 0) {
            [System.Windows.Automation.Condition]::TrueCondition
        } elseif ($conditions.Count -eq 1) {
            $conditions[0]
        } else {
            [System.Windows.Automation.AndCondition]::new(
                [System.Windows.Automation.Condition[]]$conditions.ToArray()
            )
        }
        $matches = $window.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $controlCondition
        )
        $eligibleControls = @()
        $offscreenControls = @()
        foreach ($candidateControl in $matches) {
            try {
                $bounds = $candidateControl.Current.BoundingRectangle
                $candidateName = [string]$candidateControl.Current.Name
                $nameMatches = if (-not $ControlName) {
                    $true
                } elseif ($ControlNameMatch -eq "Prefix") {
                    $candidateName.StartsWith($ControlName, [System.StringComparison]::Ordinal)
                } else {
                    $candidateName -eq $ControlName
                }
                $automationIdMatches = (
                    -not $ControlAutomationId -or
                    [string]$candidateControl.Current.AutomationId -eq $ControlAutomationId
                )
                if (
                    $nameMatches -and
                    $automationIdMatches -and
                    $candidateControl.Current.IsEnabled
                ) {
                    if (
                        -not $candidateControl.Current.IsOffscreen -and
                        $bounds.Width -gt 0 -and
                        $bounds.Height -gt 0
                    ) {
                        $eligibleControls += $candidateControl
                    } else {
                        $offscreenControls += $candidateControl
                    }
                }
            } catch {}
        }
        $matchingControlCount = $eligibleControls.Count
        if ($matchingControlCount -eq 1) {
            $control = $eligibleControls[0]
            break
        }
        if ($matchingControlCount -eq 0 -and $offscreenControls.Count -eq 1) {
            $scrollPattern = $null
            $offscreenControl = $offscreenControls[0]
            if (
                $offscreenControl.TryGetCurrentPattern(
                    [System.Windows.Automation.ScrollItemPattern]::Pattern,
                    [ref]$scrollPattern
                )
            ) {
                ([System.Windows.Automation.ScrollItemPattern]$scrollPattern).ScrollIntoView()
                Start-Sleep -Milliseconds 50
                try {
                    $bounds = $offscreenControl.Current.BoundingRectangle
                    if (
                        -not $offscreenControl.Current.IsOffscreen -and
                        $offscreenControl.Current.IsEnabled -and
                        $bounds.Width -gt 0 -and
                        $bounds.Height -gt 0
                    ) {
                        $control = $offscreenControl
                        $matchingControlCount = 1
                        $scrolledIntoView = $true
                        break
                    }
                } catch {}
            }
        }
    }
    Start-Sleep -Milliseconds 20
} while ((Get-Date) -lt $deadline)

if (-not $window) {
    throw "No unique visible Scriber window matched the requested process generation."
}
if (-not $control -or $matchingControlCount -ne 1) {
    throw "The requested UI Automation control was not uniquely visible and enabled."
}
if ((Get-ProcessCreationTime100ns -TargetProcessId $ProcessId) -ne $ProcessCreationTime100ns) {
    throw "The target process generation changed before UI input dispatch."
}

$patternName = ""
$pattern = $null
if ($control.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
    $patternName = "InvokePattern"
} elseif ($control.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
    $patternName = "SelectionItemPattern"
} elseif ($control.TryGetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern, [ref]$pattern)) {
    $patternName = "LegacyIAccessiblePattern"
} else {
    throw "The uniquely matched control exposes no supported real UI Automation action pattern."
}

if ($InputMarkerPath) {
    New-Item -ItemType Directory -Force -Path (Split-Path $InputMarkerPath) | Out-Null
    Set-Content -LiteralPath $InputMarkerPath -Value "ready" -Encoding ASCII
}
$inputQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
switch ($patternName) {
    "InvokePattern" { ([System.Windows.Automation.InvokePattern]$pattern).Invoke() }
    "SelectionItemPattern" { ([System.Windows.Automation.SelectionItemPattern]$pattern).Select() }
    "LegacyIAccessiblePattern" { ([System.Windows.Automation.LegacyIAccessiblePattern]$pattern).DoDefaultAction() }
    default { throw "Unsupported UI Automation action pattern." }
}
$actionCompletedQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()

$result = [ordered]@{
    schemaVersion = 1
    ok = $true
    endpoint = "user_input_received"
    source = "uia_invoke"
    processId = $ProcessId
    processCreationTime100ns = $ProcessCreationTime100ns
    nativeWindowHandle = [int64]$window.Current.NativeWindowHandle
    controlNameSha256 = Get-TextSha256 -Text $ControlName
    controlAutomationIdSha256 = Get-TextSha256 -Text $ControlAutomationId
    controlNameMatch = $ControlNameMatch
    controlType = [string]$control.Current.ControlType.ProgrammaticName
    actionPattern = $patternName
    scrolledIntoView = $scrolledIntoView
    qpcTicks = $inputQpcTicks
    inputQpcTicks = $inputQpcTicks
    actionCompletedQpcTicks = $actionCompletedQpcTicks
    qpcFrequency = [System.Diagnostics.Stopwatch]::Frequency
}
$json = $result | ConvertTo-Json -Depth 5
if ($OutputPath) {
    New-Item -ItemType Directory -Force -Path (Split-Path $OutputPath) | Out-Null
    Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8
}
Write-Output $json
