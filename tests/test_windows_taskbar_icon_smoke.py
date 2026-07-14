from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_live_windows_taskbar_icon_smoke_checks_native_large_and_small_icons() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_windows_taskbar_icon.ps1").read_text(
        encoding="utf-8"
    )

    assert '[int]$ProcessId' in script
    assert "EnumWindows" in script
    assert "GetWindowThreadProcessId" in script
    assert "IsWindowVisible" in script
    assert "WM_GETICON/ICON_BIG" in script
    assert "WM_GETICON/ICON_SMALL" in script
    assert "WM_GETICON/ICON_SMALL2" in script
    assert "window-class/GCLP_HICON" in script
    assert "window-class/GCLP_HICONSM" in script
    assert "MinLightPixelFraction" in script
    assert "MinLightExtentFraction" in script
    assert "MinRingLightFraction" in script
    assert "lightPixelFraction" in script
    assert "lightWidthFraction" in script
    assert "lightHeightFraction" in script
    assert "ringLightFraction" in script
    assert '$explicitLargeWindowIcon = $largeHandle.Source -eq "WM_GETICON/ICON_BIG"' in script
    assert '$explicitSmallWindowIcon = $smallHandle.Source -eq "WM_GETICON/ICON_SMALL"' in script
    assert "explicitWindowIcons" in script
    assert "large = $large" in script
    assert "small = $small" in script
    assert "Assert-UnderRepoTmp" in script
    assert "raw window title" not in script.lower()


def test_taskbar_icon_smoke_does_not_fall_back_to_an_unrelated_executable_icon() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_windows_taskbar_icon.ps1").read_text(
        encoding="utf-8"
    )

    # The taskbar regression is specifically about the icon exposed by the
    # live Tauri window. Falling back to ExtractIconEx would let a broken
    # WM_SETICON/runtime path pass merely because icon.ico is correct.
    assert "ExtractIconEx" not in script
    assert "The main window exposes no $Kind HICON" in script
