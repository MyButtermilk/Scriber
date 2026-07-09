param(
    [string]$WindowTitle = "Scriber Autoresearch TextReceiver",
    [string]$AutomationId = "ScriberAutoresearchTextBox",
    [Parameter(Mandatory=$true)][string]$ExpectedSha256,
    [string]$PrefixSentinel = "",
    [string]$SuffixSentinel = "",
    [double]$TimeoutSec = 10,
    [string]$OutputPath = "",
    [string]$ReadyPath = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class ScriberTextObserverWin32
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, StringBuilder lParam);
}
"@

function Get-Sha256Text {
    param([string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Find-TextTarget {
    param($Window, [string]$AutomationId)
    $all = $Window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $script:descendantCount = $all.Count
    $sample = @()
    for ($i = 0; $i -lt [Math]::Min(5, $all.Count); $i++) {
        $candidate = $all.Item($i)
        $sample += [pscustomobject]@{
            name = [string]$candidate.Current.Name
            automationId = [string]$candidate.Current.AutomationId
            className = [string]$candidate.Current.ClassName
            controlType = [string]$candidate.Current.ControlType.ProgrammaticName
            hwnd = [int]$candidate.Current.NativeWindowHandle
        }
    }
    $script:descendantSample = @($sample)
    $fallback = $null
    for ($i = 0; $i -lt $all.Count; $i++) {
        $candidate = $all.Item($i)
        $candidateAutomationId = [string]$candidate.Current.AutomationId
        $candidateName = [string]$candidate.Current.Name
        $candidateClassName = [string]$candidate.Current.ClassName
        $candidateControlType = [string]$candidate.Current.ControlType.ProgrammaticName
        if ($candidateAutomationId -eq $AutomationId -or $candidateName -eq $AutomationId) {
            return $candidate
        }
        if (
            -not $fallback -and (
                $candidateControlType -in @("ControlType.Edit", "ControlType.Document") -or
                $candidateClassName.Contains(".EDIT.")
            )
        ) {
            $fallback = $candidate
        }
    }
    return $fallback
}

function Get-ElementText {
    param($Element)
    if (-not $Element) {
        return ""
    }
    try {
        $pattern = $Element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        if ($pattern) {
            $value = [string]$pattern.Current.Value
            if ($value) {
                return $value
            }
        }
    } catch {
    }
    try {
        $textPattern = $Element.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern)
        if ($textPattern) {
            $value = [string]$textPattern.DocumentRange.GetText(-1)
            if ($value) {
                return $value
            }
        }
    } catch {
    }
    try {
        $legacyPattern = $Element.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern)
        if ($legacyPattern) {
            $value = [string]$legacyPattern.Current.Value
            if ($value) {
                return $value
            }
        }
    } catch {
    }
    try {
        $hwnd = [IntPtr]$Element.Current.NativeWindowHandle
        if ($hwnd -ne [IntPtr]::Zero) {
            $wmGetTextLength = 0x000E
            $wmGetText = 0x000D
            $length = [int][ScriberTextObserverWin32]::SendMessage($hwnd, $wmGetTextLength, [IntPtr]::Zero, [IntPtr]::Zero)
            if ($length -gt 0) {
                $builder = New-Object System.Text.StringBuilder ($length + 1)
                [void][ScriberTextObserverWin32]::SendMessage($hwnd, $wmGetText, [IntPtr]$builder.Capacity, $builder)
                return $builder.ToString()
            }
        }
    } catch {
    }
    try {
        return [string]$Element.Current.Name
    } catch {
        return ""
    }
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$last = ""
$observed = $false
$windowFound = $false
$targetFound = $false
$targetAutomationId = ""
$targetControlType = ""
$targetHwnd = 0
$descendantCount = 0
$descendantSample = @()
$readyWritten = $false

function Write-ObserverReady {
    if (-not $ReadyPath -or $script:readyWritten) {
        return
    }
    $ready = [pscustomobject]@{
        schemaVersion = 1
        ok = $true
        endpoint = "target_text_observer_ready"
        windowFound = $windowFound
        targetFound = $targetFound
        targetAutomationId = $targetAutomationId
        targetControlType = $targetControlType
        targetHwnd = $targetHwnd
        qpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
        qpcFrequency = [System.Diagnostics.Stopwatch]::Frequency
    }
    $readyJson = $ready | ConvertTo-Json -Depth 6
    New-Item -ItemType Directory -Force -Path (Split-Path $ReadyPath) | Out-Null
    Set-Content -LiteralPath $ReadyPath -Value $readyJson -Encoding UTF8
    $script:readyWritten = $true
}

do {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $windowCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $WindowTitle
    )
    $window = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $windowCondition)
    if ($window) {
        $windowFound = $true
        $target = Find-TextTarget -Window $window -AutomationId $AutomationId
        if ($target) {
            $targetFound = $true
            $targetAutomationId = [string]$target.Current.AutomationId
            $targetControlType = [string]$target.Current.ControlType.ProgrammaticName
            $targetHwnd = [int]$target.Current.NativeWindowHandle
            Write-ObserverReady
            $last = Get-ElementText -Element $target
            $hash = Get-Sha256Text -Text $last
            $expectedTextSeen = $false
            if ($PrefixSentinel -and $SuffixSentinel) {
                $prefixIndex = $last.IndexOf($PrefixSentinel, [System.StringComparison]::Ordinal)
                $suffixIndex = $last.IndexOf($SuffixSentinel, [System.StringComparison]::Ordinal)
                $expectedTextSeen = $prefixIndex -ge 0 -and $suffixIndex -gt $prefixIndex
            }
            $prefixOk = (-not $PrefixSentinel) -or $last.Contains($PrefixSentinel)
            $suffixOk = (-not $SuffixSentinel) -or $last.Contains($SuffixSentinel)
            if ((($hash -eq $ExpectedSha256.ToLowerInvariant()) -or $expectedTextSeen) -and $prefixOk -and $suffixOk) {
                $observed = $true
                break
            }
        }
    }
    Start-Sleep -Milliseconds 50
} while ((Get-Date) -lt $deadline)

$result = [pscustomobject]@{
    schemaVersion = 1
    ok = $observed
    endpoint = "target_text_observed"
    expectedSha256 = $ExpectedSha256.ToLowerInvariant()
    observedSha256 = if ($last) { Get-Sha256Text -Text $last } else { "" }
    prefixSentinel = $PrefixSentinel
    suffixSentinel = $SuffixSentinel
    observedChars = $last.Length
    windowFound = $windowFound
    targetFound = $targetFound
    targetAutomationId = $targetAutomationId
    targetControlType = $targetControlType
    targetHwnd = $targetHwnd
    descendantCount = $descendantCount
    descendantSample = @($descendantSample)
    qpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()
    qpcFrequency = [System.Diagnostics.Stopwatch]::Frequency
}
$json = $result | ConvertTo-Json -Depth 6
if ($OutputPath) {
    New-Item -ItemType Directory -Force -Path (Split-Path $OutputPath) | Out-Null
    Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8
}
Write-Output $json
if ($observed) { exit 0 } else { exit 1 }
