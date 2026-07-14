<#
.SYNOPSIS
Verifies the live Windows taskbar icon exposed by a running Scriber window.

.DESCRIPTION
Windows does not display an SVG directly in the taskbar. It asks the native
top-level window for its large and small HICON handles. This smoke enumerates
the visible top-level windows owned by the requested process, selects the main
window, requires direct WM_GETICON/ICON_BIG and WM_GETICON/ICON_SMALL results,
and analyzes the pixels Windows can actually consume. Alternate small/class
icons are retained only as diagnostics and can never turn the smoke green.

Both the large and small icon must keep Scriber's contrast-safe white-disc
identity. The checks deliberately cover light-pixel area, light-pixel extent,
and a light outer ring so a tiny dark feather, a stale transparent icon, or a
state badge without the white disc fails visibly.

Example:
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_taskbar_icon.ps1 `
    -ProcessId 1234 `
    -OutputPath tmp\taskbar-icon-smoke.json `
    -CaptureDirectory tmp\taskbar-icon-smoke
#>

param(
    [Parameter(Mandatory = $true)]
    [int]$ProcessId,
    [int]$TimeoutSec = 20,
    [string]$WindowTitlePattern = "^Scriber(?:$|\\s)",
    [double]$MinLightPixelFraction = 0.25,
    [double]$MinLightExtentFraction = 0.75,
    [double]$MinRingLightFraction = 0.45,
    [string]$OutputPath = "",
    [string]$CaptureDirectory = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ([Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "The live taskbar-icon smoke is Windows-only."
}
if ($ProcessId -le 0) {
    throw "ProcessId must be positive."
}
if ($TimeoutSec -le 0) {
    throw "TimeoutSec must be positive."
}
foreach ($threshold in @(
    [pscustomobject]@{ Name = "MinLightPixelFraction"; Value = $MinLightPixelFraction },
    [pscustomobject]@{ Name = "MinLightExtentFraction"; Value = $MinLightExtentFraction },
    [pscustomobject]@{ Name = "MinRingLightFraction"; Value = $MinRingLightFraction }
)) {
    if ($threshold.Value -lt 0 -or $threshold.Value -gt 1) {
        throw "$($threshold.Name) must be between 0 and 1."
    }
}

Add-Type -AssemblyName System.Drawing
if (-not ("ScriberTaskbarIconNative" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class ScriberTaskbarIconNative
{
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int maxCount);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(
        IntPtr hWnd,
        uint message,
        IntPtr wParam,
        IntPtr lParam,
        uint flags,
        uint timeoutMs,
        out IntPtr result);

    [DllImport("user32.dll", EntryPoint = "GetClassLongPtrW", SetLastError = true)]
    private static extern IntPtr GetClassLongPtr64(IntPtr hWnd, int index);

    [DllImport("user32.dll", EntryPoint = "GetClassLongW", SetLastError = true)]
    private static extern uint GetClassLong32(IntPtr hWnd, int index);

    public static IntPtr GetClassIcon(IntPtr hWnd, int index)
    {
        return IntPtr.Size == 8
            ? GetClassLongPtr64(hWnd, index)
            : new IntPtr(unchecked((int)GetClassLong32(hWnd, index)));
    }
}
"@
}

function Convert-ToFullPath {
    param([string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $Path))
}

function Assert-UnderRepoTmp {
    param(
        [string]$Path,
        [string]$Label
    )

    $tmpRoot = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot "tmp"))
    $fullPath = Convert-ToFullPath -Path $Path
    $tmpPrefix = $tmpRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($tmpPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay below the repository tmp directory. Got: $fullPath"
    }
    return $fullPath
}

function Get-VisibleProcessWindows {
    param([int]$TargetProcessId)

    $matches = [System.Collections.Generic.List[object]]::new()
    $callback = [ScriberTaskbarIconNative+EnumWindowsProc]{
        param([IntPtr]$windowHandle, [IntPtr]$unused)

        [uint32]$ownerProcessId = 0
        [ScriberTaskbarIconNative]::GetWindowThreadProcessId(
            $windowHandle,
            [ref]$ownerProcessId
        ) | Out-Null
        if (
            $ownerProcessId -eq [uint32]$TargetProcessId -and
            [ScriberTaskbarIconNative]::IsWindowVisible($windowHandle)
        ) {
            $titleBuffer = [System.Text.StringBuilder]::new(512)
            [ScriberTaskbarIconNative]::GetWindowText(
                $windowHandle,
                $titleBuffer,
                $titleBuffer.Capacity
            ) | Out-Null
            $rect = [ScriberTaskbarIconNative+RECT]::new()
            [ScriberTaskbarIconNative]::GetWindowRect($windowHandle, [ref]$rect) | Out-Null
            $width = [Math]::Max(0, $rect.Right - $rect.Left)
            $height = [Math]::Max(0, $rect.Bottom - $rect.Top)
            $matches.Add([pscustomobject]@{
                Handle = $windowHandle
                Title = $titleBuffer.ToString()
                Width = $width
                Height = $height
                Area = [int64]$width * [int64]$height
            })
        }
        return $true
    }
    [ScriberTaskbarIconNative]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
    return @($matches.ToArray())
}

function Wait-MainWindow {
    param(
        [int]$TargetProcessId,
        [int]$DeadlineSec,
        [string]$TitlePattern
    )

    $deadline = (Get-Date).AddSeconds($DeadlineSec)
    do {
        $process = Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue
        if (-not $process) {
            throw "Process $TargetProcessId exited before its taskbar icon could be inspected."
        }
        $windows = @(Get-VisibleProcessWindows -TargetProcessId $TargetProcessId)
        if ($windows.Count -gt 0) {
            $ranked = @(
                $windows |
                    Sort-Object `
                        @{ Expression = { if ($_.Title -match $TitlePattern) { 1 } else { 0 } }; Descending = $true }, `
                        @{ Expression = { $_.Area }; Descending = $true }
            )
            return [pscustomobject]@{
                Window = $ranked[0]
                CandidateCount = $windows.Count
                TitlePatternMatched = [bool]($ranked[0].Title -match $TitlePattern)
            }
        }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)

    throw "No visible top-level window appeared for process $TargetProcessId within ${DeadlineSec}s."
}

function Get-WindowIconHandle {
    param(
        [IntPtr]$WindowHandle,
        [ValidateSet("large", "small")]
        [string]$Kind
    )

    $wmGetIcon = 0x007F
    $sendMessageAbortIfHung = 0x0002
    $requests = if ($Kind -eq "large") {
        @(
            [pscustomobject]@{ Code = 1; Source = "WM_GETICON/ICON_BIG" },
            [pscustomobject]@{ Code = 2; Source = "WM_GETICON/ICON_SMALL2" },
            [pscustomobject]@{ Code = 0; Source = "WM_GETICON/ICON_SMALL" }
        )
    } else {
        @(
            [pscustomobject]@{ Code = 0; Source = "WM_GETICON/ICON_SMALL" },
            [pscustomobject]@{ Code = 2; Source = "WM_GETICON/ICON_SMALL2" },
            [pscustomobject]@{ Code = 1; Source = "WM_GETICON/ICON_BIG" }
        )
    }

    foreach ($request in $requests) {
        [IntPtr]$iconResult = [IntPtr]::Zero
        $sent = [ScriberTaskbarIconNative]::SendMessageTimeout(
            $WindowHandle,
            $wmGetIcon,
            [IntPtr]::new([int]$request.Code),
            [IntPtr]::Zero,
            $sendMessageAbortIfHung,
            1000,
            [ref]$iconResult
        )
        if ($sent -ne [IntPtr]::Zero -and $iconResult -ne [IntPtr]::Zero) {
            return [pscustomobject]@{ Handle = $iconResult; Source = $request.Source }
        }
    }

    $classIndexes = if ($Kind -eq "large") {
        @(
            [pscustomobject]@{ Index = -14; Source = "window-class/GCLP_HICON" },
            [pscustomobject]@{ Index = -34; Source = "window-class/GCLP_HICONSM" }
        )
    } else {
        @(
            [pscustomobject]@{ Index = -34; Source = "window-class/GCLP_HICONSM" },
            [pscustomobject]@{ Index = -14; Source = "window-class/GCLP_HICON" }
        )
    }
    foreach ($request in $classIndexes) {
        $classIcon = [ScriberTaskbarIconNative]::GetClassIcon($WindowHandle, $request.Index)
        if ($classIcon -ne [IntPtr]::Zero) {
            return [pscustomobject]@{ Handle = $classIcon; Source = $request.Source }
        }
    }
    throw "The main window exposes no $Kind HICON through WM_GETICON or its window class."
}

function Test-IconBitmap {
    param(
        [IntPtr]$IconHandle,
        [string]$Kind,
        [string]$Source,
        [string]$CapturePath = ""
    )

    $icon = [System.Drawing.Icon]::FromHandle($IconHandle)
    $bitmap = $null
    try {
        $bitmap = $icon.ToBitmap()
        if ($CapturePath) {
            $bitmap.Save($CapturePath, [System.Drawing.Imaging.ImageFormat]::Png)
        }

        $width = $bitmap.Width
        $height = $bitmap.Height
        if ($width -le 0 -or $height -le 0) {
            throw "Windows returned an empty $Kind icon bitmap."
        }

        $opaqueCount = 0
        $lightCount = 0
        $ringCount = 0
        $ringLightCount = 0
        $lightMinX = $width
        $lightMinY = $height
        $lightMaxX = -1
        $lightMaxY = -1
        $centerX = ($width - 1) / 2.0
        $centerY = ($height - 1) / 2.0
        $radiusScale = [Math]::Min($width, $height) / 2.0

        for ($y = 0; $y -lt $height; $y++) {
            for ($x = 0; $x -lt $width; $x++) {
                $pixel = $bitmap.GetPixel($x, $y)
                if ($pixel.A -ge 32) {
                    $opaqueCount++
                }
                $maximum = [Math]::Max($pixel.R, [Math]::Max($pixel.G, $pixel.B))
                $minimum = [Math]::Min($pixel.R, [Math]::Min($pixel.G, $pixel.B))
                $luminance = (0.2126 * $pixel.R) + (0.7152 * $pixel.G) + (0.0722 * $pixel.B)
                $isLight = $pixel.A -ge 160 -and $luminance -ge 215 -and ($maximum - $minimum) -le 45
                if ($isLight) {
                    $lightCount++
                    $lightMinX = [Math]::Min($lightMinX, $x)
                    $lightMinY = [Math]::Min($lightMinY, $y)
                    $lightMaxX = [Math]::Max($lightMaxX, $x)
                    $lightMaxY = [Math]::Max($lightMaxY, $y)
                }

                $normalizedRadius = [Math]::Sqrt(
                    [Math]::Pow(($x - $centerX) / $radiusScale, 2) +
                    [Math]::Pow(($y - $centerY) / $radiusScale, 2)
                )
                if ($normalizedRadius -ge 0.54 -and $normalizedRadius -le 0.88) {
                    $ringCount++
                    if ($isLight) {
                        $ringLightCount++
                    }
                }
            }
        }

        $pixelCount = $width * $height
        $lightExtentWidth = if ($lightMaxX -ge $lightMinX) { $lightMaxX - $lightMinX + 1 } else { 0 }
        $lightExtentHeight = if ($lightMaxY -ge $lightMinY) { $lightMaxY - $lightMinY + 1 } else { 0 }
        $lightPixelFraction = $lightCount / [double]$pixelCount
        $lightWidthFraction = $lightExtentWidth / [double]$width
        $lightHeightFraction = $lightExtentHeight / [double]$height
        $ringLightFraction = if ($ringCount -gt 0) { $ringLightCount / [double]$ringCount } else { 0 }

        $passed = (
            $lightPixelFraction -ge $MinLightPixelFraction -and
            $lightWidthFraction -ge $MinLightExtentFraction -and
            $lightHeightFraction -ge $MinLightExtentFraction -and
            $ringLightFraction -ge $MinRingLightFraction
        )
        return [pscustomobject]@{
            kind = $Kind
            source = $Source
            width = $width
            height = $height
            opaquePixelFraction = [Math]::Round($opaqueCount / [double]$pixelCount, 4)
            lightPixelFraction = [Math]::Round($lightPixelFraction, 4)
            lightWidthFraction = [Math]::Round($lightWidthFraction, 4)
            lightHeightFraction = [Math]::Round($lightHeightFraction, 4)
            ringLightFraction = [Math]::Round($ringLightFraction, 4)
            passed = $passed
            capture = if ($CapturePath) { $CapturePath } else { $null }
        }
    } finally {
        if ($bitmap) {
            $bitmap.Dispose()
        }
        $icon.Dispose()
    }
}

$captureRoot = ""
if ($CaptureDirectory) {
    $captureRoot = Assert-UnderRepoTmp -Path $CaptureDirectory -Label "CaptureDirectory"
    New-Item -ItemType Directory -Force -Path $captureRoot | Out-Null
}
$outputFull = ""
if ($OutputPath) {
    $outputFull = Assert-UnderRepoTmp -Path $OutputPath -Label "OutputPath"
    New-Item -ItemType Directory -Force -Path (Split-Path $outputFull) | Out-Null
}

$selection = Wait-MainWindow `
    -TargetProcessId $ProcessId `
    -DeadlineSec $TimeoutSec `
    -TitlePattern $WindowTitlePattern
$largeHandle = Get-WindowIconHandle -WindowHandle $selection.Window.Handle -Kind "large"
$smallHandle = Get-WindowIconHandle -WindowHandle $selection.Window.Handle -Kind "small"
$largeCapture = if ($captureRoot) { Join-Path $captureRoot "taskbar-large.png" } else { "" }
$smallCapture = if ($captureRoot) { Join-Path $captureRoot "taskbar-small.png" } else { "" }
$large = Test-IconBitmap `
    -IconHandle $largeHandle.Handle `
    -Kind "large" `
    -Source $largeHandle.Source `
    -CapturePath $largeCapture
$small = Test-IconBitmap `
    -IconHandle $smallHandle.Handle `
    -Kind "small" `
    -Source $smallHandle.Source `
    -CapturePath $smallCapture

$explicitLargeWindowIcon = $largeHandle.Source -eq "WM_GETICON/ICON_BIG"
$explicitSmallWindowIcon = $smallHandle.Source -eq "WM_GETICON/ICON_SMALL"
$ok = [bool](
    $large.passed -and
    $small.passed -and
    $explicitLargeWindowIcon -and
    $explicitSmallWindowIcon
)
$result = [pscustomobject]@{
    ok = $ok
    processId = $ProcessId
    candidateWindowCount = $selection.CandidateCount
    titlePatternMatched = $selection.TitlePatternMatched
    thresholds = [pscustomobject]@{
        minLightPixelFraction = $MinLightPixelFraction
        minLightExtentFraction = $MinLightExtentFraction
        minRingLightFraction = $MinRingLightFraction
    }
    explicitWindowIcons = [pscustomobject]@{
        large = $explicitLargeWindowIcon
        small = $explicitSmallWindowIcon
    }
    large = $large
    small = $small
}
$json = $result | ConvertTo-Json -Compress -Depth 5
if ($outputFull) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($outputFull, $json, $encoding)
}
$json
if (-not $ok) {
    throw "The live Windows taskbar icon is missing explicit ICON_BIG/ICON_SMALL handles or Scriber's contrast-safe, high-occupancy white-disc identity."
}
