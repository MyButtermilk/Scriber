param(
    [string]$WindowTitle = "Scriber",
    [string[]]$ExpectedText = @(),
    [string[]]$ForbiddenText = @(),
    [int]$ExpectedProcessId = 0,
    [long]$ExpectedProcessCreationTime100ns = 0,
    [string]$StartAfterPath = "",
    [double]$TimeoutSec = 10,
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Get-TextSha256 {
    param([string]$Text)
    if (-not $Text) { return "" }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($bytes)
        return (($hash | ForEach-Object { $_.ToString("x2") }) -join "")
    } finally {
        $sha.Dispose()
    }
}

function Get-ProcessCreationTime100ns {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $null }
    try {
        return [long](Get-Process -Id $ProcessId -ErrorAction Stop).StartTime.ToUniversalTime().ToFileTimeUtc()
    } catch {
        return $null
    }
}

function Get-VisibleText {
    param($Element)
    $texts = New-Object System.Collections.Generic.List[string]
    try {
        $nodes = $Element.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition
        )
    } catch {
        return ""
    }
    foreach ($node in $nodes) {
        try {
            $name = [string]$node.Current.Name
        } catch {
            $name = ""
        }
        if ($name) { $texts.Add($name) }
    }
    return ($texts -join "`n")
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$observerStartedQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
$startGateObserved = $false
$startGateQpcTicks = $null
if ($StartAfterPath) {
    do {
        if (Test-Path -LiteralPath $StartAfterPath -PathType Leaf) {
            $startGateObserved = $true
            $startGateQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
            break
        }
        Start-Sleep -Milliseconds 20
    } while ((Get-Date) -lt $deadline)
} else {
    $startGateObserved = $true
}

$lastText = ""
$stable = $false
$previousText = $null
$sampleCount = 0
$matchingSampleCount = 0
$stableSampleCount = 0
$firstWindowQpcTicks = $null
$firstNonEmptyQpcTicks = $null
$lastSampleQpcTicks = $null
$stableQpcTicks = $null
$stableConfirmedQpcTicks = $null
$observedProcessId = $null
$observedProcessCreationTime100ns = $null
$observedNativeWindowHandle = $null
$observedWindowVisible = $false
$firstTextTraversalMs = $null
$lastTextTraversalMs = $null
$maxTextTraversalMs = $null
if ($startGateObserved) {
    do {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        $condition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            $WindowTitle
        )
        $windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $condition)
        $window = $null
        $eligibleWindowCount = 0
        foreach ($candidateWindow in $windows) {
            try {
                $candidateProcessId = [int]$candidateWindow.Current.ProcessId
                $candidateCreationTime = Get-ProcessCreationTime100ns -ProcessId $candidateProcessId
                $candidateBounds = $candidateWindow.Current.BoundingRectangle
                $candidateVisible = (
                    -not [bool]$candidateWindow.Current.IsOffscreen -and
                    $candidateBounds.Width -gt 0 -and
                    $candidateBounds.Height -gt 0
                )
                if (-not $candidateVisible) { continue }
                if ($ExpectedProcessId -gt 0 -and $candidateProcessId -ne $ExpectedProcessId) { continue }
                if (
                    $ExpectedProcessCreationTime100ns -gt 0 -and
                    $candidateCreationTime -ne $ExpectedProcessCreationTime100ns
                ) { continue }
                $eligibleWindowCount += 1
                if ($eligibleWindowCount -eq 1) {
                    $window = $candidateWindow
                    $observedProcessId = $candidateProcessId
                    $observedProcessCreationTime100ns = $candidateCreationTime
                    $observedNativeWindowHandle = [int64]$candidateWindow.Current.NativeWindowHandle
                    $observedWindowVisible = $candidateVisible
                }
            } catch {
                continue
            }
        }
        if ($eligibleWindowCount -ne 1) {
            $window = $null
        }
        if ($window) {
            $sampleTick = [System.Diagnostics.Stopwatch]::GetTimestamp()
            $sampleCount += 1
            $lastSampleQpcTicks = $sampleTick
            if ($null -eq $firstWindowQpcTicks) {
                $firstWindowQpcTicks = $sampleTick
            }
            try {
                $lastText = Get-VisibleText -Element $window
            } catch {
                $lastText = ""
            }
            $textTraversalDoneTick = [System.Diagnostics.Stopwatch]::GetTimestamp()
            $textTraversalMs = [Math]::Round(
                (($textTraversalDoneTick - $sampleTick) * 1000.0) / [System.Diagnostics.Stopwatch]::Frequency,
                3
            )
            if ($null -eq $firstTextTraversalMs) {
                $firstTextTraversalMs = $textTraversalMs
            }
            $lastTextTraversalMs = $textTraversalMs
            if ($null -eq $maxTextTraversalMs -or $textTraversalMs -gt $maxTextTraversalMs) {
                $maxTextTraversalMs = $textTraversalMs
            }
            if ($lastText.Length -gt 0 -and $null -eq $firstNonEmptyQpcTicks) {
                $firstNonEmptyQpcTicks = $sampleTick
            }
            $containsAll = $true
            foreach ($expected in $ExpectedText) {
                if ($lastText -notlike "*$expected*") {
                    $containsAll = $false
                    break
                }
            }
            foreach ($forbidden in $ForbiddenText) {
                if ($forbidden -and $lastText -like "*$forbidden*") {
                    $containsAll = $false
                    break
                }
            }
            if ($containsAll) {
                $matchingSampleCount += 1
                if ($previousText -eq $lastText) {
                    $stableSampleCount = [Math]::Max($stableSampleCount, 2)
                } else {
                    $stableSampleCount = 1
                }
            } else {
                $stableSampleCount = 0
            }
            if ($containsAll -and $previousText -eq $lastText) {
                $stable = $true
                $stableQpcTicks = $sampleTick
                $stableConfirmedQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
                break
            }
            $previousText = $lastText
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
}

$result = [pscustomobject]@{
    schemaVersion = 1
    ok = $stable
    endpoint = "first_stable_visible_frame"
    windowTitle = $WindowTitle
    expectedTextSha256 = @($ExpectedText | ForEach-Object { Get-TextSha256 -Text ([string]$_) })
    forbiddenTextSha256 = @($ForbiddenText | ForEach-Object { Get-TextSha256 -Text ([string]$_) })
    expectedProcessId = if ($ExpectedProcessId -gt 0) { $ExpectedProcessId } else { $null }
    expectedProcessCreationTime100ns = if ($ExpectedProcessCreationTime100ns -gt 0) { $ExpectedProcessCreationTime100ns } else { $null }
    processId = $observedProcessId
    processCreationTime100ns = $observedProcessCreationTime100ns
    nativeWindowHandle = $observedNativeWindowHandle
    windowVisible = $observedWindowVisible
    startAfterPath = $StartAfterPath
    observerStartedQpcTicks = $observerStartedQpcTicks
    startGateObserved = $startGateObserved
    startGateQpcTicks = $startGateQpcTicks
    firstWindowQpcTicks = $firstWindowQpcTicks
    firstNonEmptyQpcTicks = $firstNonEmptyQpcTicks
    lastSampleQpcTicks = $lastSampleQpcTicks
    stableQpcTicks = $stableQpcTicks
    stableConfirmedQpcTicks = $stableConfirmedQpcTicks
    sampleCount = $sampleCount
    matchingSampleCount = $matchingSampleCount
    stableSampleCount = $stableSampleCount
    observedChars = $lastText.Length
    lastTextSha256 = Get-TextSha256 -Text $lastText
    firstTextTraversalMs = $firstTextTraversalMs
    lastTextTraversalMs = $lastTextTraversalMs
    maxTextTraversalMs = $maxTextTraversalMs
    qpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
    qpcFrequency = [System.Diagnostics.Stopwatch]::Frequency
}
$json = $result | ConvertTo-Json -Depth 6
if ($OutputPath) {
    New-Item -ItemType Directory -Force -Path (Split-Path $OutputPath) | Out-Null
    Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8
}
Write-Output $json
if ($stable) { exit 0 } else { exit 1 }
