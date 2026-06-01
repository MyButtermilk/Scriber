from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_script(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_desktop_stability_smoke_reports_memory_growth_gate() -> None:
    script = read_script("scripts/smoke_tauri_desktop.ps1")

    assert "[double]$MaxBackendWorkingSetGrowthMB = 0" in script
    assert "[double]$MaxWorkingSetGrowthMB = 0" in script
    assert "backendWorkingSetGrowthMb = $workingSetGrowth" in script
    assert "backendWorkingSetPeakGrowthMb = $workingSetPeakGrowth" in script
    assert "maxBackendWorkingSetGrowthMb =" in script
    assert "working-set peak growth" in script
    assert "-MaxWorkingSetGrowthMB $MaxBackendWorkingSetGrowthMB" in script


def test_desktop_stability_smoke_reports_idle_cpu_gate() -> None:
    script = read_script("scripts/smoke_tauri_desktop.ps1")

    assert "[double]$MaxIdleCpuPercent = 0" in script
    assert "Get-ProcessTotalCpuSeconds" in script
    assert "combinedCpuMaxPercent = $combinedCpuMax" in script
    assert "combinedCpuAvgPercent = $combinedCpuAvg" in script
    assert "maxIdleCpuPercent =" in script
    assert "average idle CPU" in script
    assert "-MaxIdleCpuPercent $MaxIdleCpuPercent" in script


def test_installer_and_build_scripts_forward_memory_growth_gate() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[double]$MaxBackendWorkingSetGrowthMB = 0" in installer
    assert '"-MaxBackendWorkingSetGrowthMB", $MaxBackendWorkingSetGrowthMB.ToString' in installer
    assert "[double]$InstallerMaxBackendWorkingSetGrowthMB = 0" in build
    assert '"-MaxBackendWorkingSetGrowthMB", $InstallerMaxBackendWorkingSetGrowthMB.ToString' in build


def test_installer_and_build_scripts_forward_idle_cpu_gate() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[double]$MaxIdleCpuPercent = 0" in installer
    assert '"-MaxIdleCpuPercent", $MaxIdleCpuPercent.ToString' in installer
    assert "[double]$InstallerMaxIdleCpuPercent = 0" in build
    assert '"-MaxIdleCpuPercent", $InstallerMaxIdleCpuPercent.ToString' in build


def test_installer_uninstall_smoke_is_a_strict_build_gate() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[switch]$VerifyUninstall" in installer
    assert "Invoke-InstalledUninstallCheck" in installer
    assert "Silent uninstall verification failed" in installer
    assert "installArtifactsRemoved" in installer
    assert "dataDirPreserved" in installer
    assert "uninstall = $null" in installer
    assert "-VerifyUninstall cannot be combined with -KeepInstalled." in installer

    assert "[switch]$RunInstallerUninstallSmoke" in build
    assert "$RunInstallerUninstallSmoke" in build
    assert '$installerSmokeArgs += "-VerifyUninstall"' in build
