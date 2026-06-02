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


def test_release_build_and_installer_smoke_report_size_budgets() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[double]$MaxInstallerSizeMB = 220" in build
    assert "[double]$InstallerMaxInstalledSizeMB = 0" in build
    assert "scripts\\create_release_size_report.py" in build
    assert "--max-installer-mb" in build
    assert "size-report.json" in build
    assert '"-MaxInstalledSizeMB", $InstallerMaxInstalledSizeMB.ToString' in build

    assert "[double]$MaxInstalledSizeMB = 0" in installer
    assert "function Get-DirectorySizeReport" in installer
    assert "Installed app size ${totalMb} MB exceeds budget ${MaxSizeMB} MB." in installer
    assert "installSize = $installSize" in installer


def test_sidecar_build_requires_and_validates_bundled_media_tools() -> None:
    sidecar = read_script("scripts/build_tauri_backend_sidecar.ps1")

    assert "function Test-MediaToolExecutable" in sidecar
    assert 'Test-MediaToolExecutable -Path $copiedFfmpeg -Name "ffmpeg"' in sidecar
    assert 'Test-MediaToolExecutable -Path $copiedFfprobe -Name "ffprobe"' in sidecar
    assert "ffprobe was not found on PATH" in sidecar
    assert "ffprobe was not found in MediaToolsDir" in sidecar
    assert "executable failed validation" in sidecar


def test_release_workflow_installs_media_tools_for_standard_build() -> None:
    workflow = read_script(".github/workflows/release-windows.yml")

    assert "Install media tools" in workflow
    assert "choco install ffmpeg --yes --no-progress" in workflow
    assert "Get-Command ffmpeg -ErrorAction Stop" in workflow
    assert "Get-Command ffprobe -ErrorAction Stop" in workflow
    assert "& $ffmpeg.Source -version" in workflow
    assert "& $ffprobe.Source -version" in workflow


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


def test_desktop_and_installer_smokes_can_persist_json_output_under_tmp() -> None:
    desktop = read_script("scripts/smoke_tauri_desktop.ps1")
    installer = read_script("scripts/smoke_windows_installer.ps1")

    for script in (desktop, installer):
        assert '[string]$OutputPath = ""' in script
        assert "function Write-SmokeJson" in script
        assert 'Assert-UnderRoot -Root (Join-Path $Root "tmp") -Path $outputFull -Label "Smoke output"' in script
        assert "Set-Content -LiteralPath $outputFull -Value $json -Encoding UTF8" in script
        assert "ConvertTo-Json -Compress -Depth 8" in script

    assert "Write-SmokeJson -Payload $result -Path $OutputPath -Root $RepoRoot" in desktop
    assert "Write-SmokeJson -Payload ([pscustomobject]$result) -Path $OutputPath -Root $RepoRoot" in installer


def test_desktop_smoke_can_verify_os_global_hotkey_dispatch() -> None:
    desktop = read_script("scripts/smoke_tauri_desktop.ps1")

    assert "[switch]$VerifyGlobalHotkeyRegistration" in desktop
    assert "[switch]$SimulateGlobalHotkey" in desktop
    assert "[switch]$WaitForManualGlobalHotkey" in desktop
    assert '[string]$GlobalHotkeySmokeHotkey = "ctrl+alt+shift+f12"' in desktop
    assert "function Test-GlobalHotkeyRegistration" in desktop
    assert "function Invoke-GlobalHotkeyChord" in desktop
    assert "function Test-GlobalHotkeyDispatch" in desktop
    assert '[ValidateSet("synthetic", "manual")]' in desktop
    assert 'Write-Warning "Manual global hotkey smoke: press' in desktop
    assert 'dispatchMethod = $DispatchMethod' in desktop
    assert "Global hotkey registered: $Hotkey (toggle)" in desktop
    assert "SCRIBER_DEFAULT_STT=$invalidProvider" in desktop
    assert "Assert-UnderRoot -Root (Join-Path $Root \"tmp\") -Path $RuntimeDataDir -Label \"Global hotkey smoke DataDir\"" in desktop
    assert "$env:SCRIBER_HOTKEY = $globalHotkeySmokeConfig.hotkey" in desktop
    assert "$env:SCRIBER_DEFAULT_STT = $globalHotkeySmokeConfig.invalidProvider" in desktop
    assert "$env:SCRIBER_HOTKEY = $oldScriberHotkey" in desktop
    assert "globalHotkey = $globalHotkey" in desktop


def test_installer_and_build_scripts_forward_global_hotkey_smoke() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[switch]$VerifyGlobalHotkeyRegistration" in installer
    assert "[switch]$SimulateGlobalHotkey" in installer
    assert "[switch]$WaitForManualGlobalHotkey" in installer
    assert '"-VerifyGlobalHotkeyRegistration"' in installer
    assert '"-WaitForManualGlobalHotkey"' in installer
    assert '"-GlobalHotkeySmokeHotkey", $GlobalHotkeySmokeHotkey' in installer
    assert '"-GlobalHotkeyDispatchTimeoutSec", $GlobalHotkeyDispatchTimeoutSec.ToString()' in installer
    assert "globalHotkey = $smoke.globalHotkey" in installer

    assert "[switch]$RunInstallerGlobalHotkeyRegistrationSmoke" in build
    assert "[switch]$RunInstallerGlobalHotkeySmoke" in build
    assert "[switch]$RunInstallerManualGlobalHotkeySmoke" in build
    assert "[string]$InstallerGlobalHotkeySmokeHotkey" in build
    assert "$RunInstallerGlobalHotkeyRegistrationSmoke" in build
    assert "$RunInstallerGlobalHotkeySmoke" in build
    assert "$RunInstallerManualGlobalHotkeySmoke" in build
    assert '"-VerifyGlobalHotkeyRegistration"' in build
    assert '"-WaitForManualGlobalHotkey"' in build
    assert '"-GlobalHotkeySmokeHotkey", $InstallerGlobalHotkeySmokeHotkey' in build


def test_desktop_installer_and_build_scripts_support_bundle_gate() -> None:
    desktop = read_script("scripts/smoke_tauri_desktop.ps1")
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[switch]$VerifySupportBundle" in desktop
    assert "function Test-SupportBundle" in desktop
    assert "/api/runtime/support-bundle" in desktop
    assert "Support bundle endpoint allowed an unauthenticated request" in desktop
    assert "support-bundle-secret-smoke.log" in desktop
    assert "Support bundle leaked a secret value" in desktop
    assert "redactionVerified = $true" in desktop
    assert "$configSnapshots" in desktop
    assert "[System.IO.File]::WriteAllBytes($snapshot.Path" in desktop
    assert "supportBundle = $supportBundle" in desktop
    assert "[switch]$VerifyFrontend" in desktop
    assert "function Test-FrontendHttp" in desktop
    assert "function Wait-FrontendReady" in desktop
    assert "Frontend root HTML does not contain the React root element" in desktop
    assert "http://tauri.localhost" in desktop
    assert "/api/runtime/frontend-ready" in desktop
    assert "Tauri WebView did not report frontend-ready" in desktop
    assert "tauriOriginCors = $true" in desktop
    assert "runtimeCorsVerified" in desktop
    assert "webViewReady = [bool]$frontendReady.ready" in desktop
    assert "webViewBackendBaseUrl" in desktop
    assert "webViewLocationOrigin" in desktop
    assert "frontend = $frontend" in desktop

    assert "[switch]$VerifySupportBundle" in installer
    assert '"-VerifySupportBundle"' in installer
    assert "supportBundle = $smoke.supportBundle" in installer
    assert "[switch]$VerifyFrontend" in installer
    assert "frontend-ready beacon" in installer
    assert '"-VerifyFrontend"' in installer
    assert "frontend = $smoke.frontend" in installer

    assert "[switch]$RunInstallerSupportBundleSmoke" in build
    assert "$RunInstallerSupportBundleSmoke" in build
    assert '"-VerifySupportBundle"' in build
    assert "[switch]$RunInstallerFrontendSmoke" in build
    assert "$RunInstallerFrontendSmoke" in build
    assert '"-VerifyFrontend"' in build


def test_desktop_and_installer_smokes_support_live_recording_stability_gate() -> None:
    desktop = read_script("scripts/smoke_tauri_desktop.ps1")
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[int]$LiveRecordingDurationSec = 0" in desktop
    assert "function Test-LiveRecordingStability" in desktop
    assert "Invoke-LiveMicStart" in desktop
    assert "Invoke-LiveMicStop" in desktop
    assert "nonRecordingSampleCount = $nonRecordingSamples.Count" in desktop
    assert "[switch]$DisableLiveTextInjection" in desktop
    assert "$env:SCRIBER_DISABLE_TEXT_INJECTION = \"1\"" in desktop
    assert "textInjectionDisabled = $TextInjectionDisabled" in desktop
    assert "liveRecording = $liveRecording" in desktop

    assert "[int]$LiveRecordingDurationSec = 0" in installer
    assert "[switch]$DisableLiveTextInjection" in installer
    assert '"-LiveRecordingDurationSec", $LiveRecordingDurationSec.ToString()' in installer
    assert '"-DisableLiveTextInjection"' in installer
    assert "liveRecording = $smoke.liveRecording" in installer

    assert "[switch]$RunInstallerLiveRecordingSmoke" in build
    assert "[switch]$InstallerDisableLiveTextInjection" in build
    assert "[int]$InstallerLiveRecordingDurationSec = 0" in build
    assert "$RunInstallerLiveRecordingSmoke" in build
    assert '"-LiveRecordingDurationSec", $liveDuration.ToString()' in build
    assert '"-DisableLiveTextInjection"' in build
    assert '"-MaxLiveBackendWorkingSetGrowthMB", $InstallerMaxLiveBackendWorkingSetGrowthMB.ToString' in build
