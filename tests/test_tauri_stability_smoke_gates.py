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


def test_installer_and_build_scripts_forward_real_media_workflow_gate() -> None:
    desktop = read_script("scripts/smoke_tauri_desktop.ps1")
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[switch]$VerifyRealMediaWorkflows" in desktop
    assert "scripts\\smoke_installed_transcription_workflows.py" in desktop
    assert "SCRIBER_SMOKE_SESSION_TOKEN" in desktop
    assert "realMediaWorkflows = $realMediaWorkflows" in desktop

    assert "[switch]$VerifyRealMediaWorkflows" in installer
    assert '$smokeArgs += "-VerifyRealMediaWorkflows"' in installer
    assert "realMediaWorkflows = $smoke.realMediaWorkflows" in installer

    assert "[switch]$RunInstallerRealMediaWorkflowSmoke" in build
    assert "$RunInstallerRealMediaWorkflowSmoke" in build
    assert '$installerSmokeArgs += "-VerifyRealMediaWorkflows"' in build
    assert "[string]$InstallerRealWorkflowYoutubeUrl" in build


def test_installer_smoke_requires_packaged_audio_sidecar() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")

    assert "function Resolve-InstalledAudioSidecarExe" in installer
    assert "resources\\audio-sidecar\\scriber-audio-sidecar.exe" in installer
    assert "Resolve-InstalledAudioSidecarExe -Root $InstallDir" in installer
    assert "audioSidecarExe = $audioSidecarExe" in installer


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
    assert "function Test-ScriberFfmpegCapabilities" in sidecar
    assert "function Invoke-ScriberFfmpegProfileManifest" in sidecar
    assert "function Get-SidecarInputManifest" in sidecar
    assert "function Write-SidecarBuildMetadata" in sidecar
    assert "function Invoke-PySide6Pruning" in sidecar
    assert "[switch]$UseProfileBFfmpeg" in sidecar
    assert "prepare-profile-b-ffmpeg" in sidecar
    assert "scripts\\ffmpeg\\build_profile_b_msys2.ps1" in sidecar
    assert 'kind = "profile-b"' in sidecar
    assert "[switch]$UseGyanFfmpegEssentials" in sidecar
    assert "scripts\\prepare_gyan_ffmpeg_essentials.ps1" in sidecar
    assert "prepare-gyan-ffmpeg-essentials" in sidecar
    assert "$ValidateSlimMediaTools = $true" in sidecar
    assert "preparedMediaTools = $PreparedMediaTools" in sidecar
    assert 'Test-MediaToolExecutable -Path $copiedFfmpeg -Name "ffmpeg"' in sidecar
    assert 'Test-MediaToolExecutable -Path $copiedFfprobe -Name "ffprobe"' in sidecar
    assert "[switch]$ValidateSlimMediaTools" in sidecar
    assert "[switch]$ReuseSidecarIfUnchanged" in sidecar
    assert "[switch]$PrunePySide6Translations" in sidecar
    assert "[switch]$PrunePySide6UnusedPlugins" in sidecar
    assert "[switch]$PrunePySide6SoftwareOpenGl" in sidecar
    assert "[switch]$BundleRustAudioSidecar" in sidecar
    assert "function Copy-RustAudioSidecarToTauriRelease" in sidecar
    assert "cargo build --release --bin scriber-audio-sidecar" in sidecar
    assert "resources\\audio-sidecar" in sidecar
    assert "audio-sidecar-build-metadata.json" in sidecar
    assert 'Test-ScriberFfmpegCapabilities -Path $copiedFfmpeg' in sidecar
    assert "scripts\\ffmpeg\\validate_ffmpeg_profile.py" in sidecar
    assert "ffmpeg-profile-manifest.json" in sidecar
    assert "Invoke-ScriberFfmpegProfileManifest" in sidecar
    assert "-encoders" in sidecar
    assert "libopus" in sidecar
    assert "libmp3lame" in sidecar
    assert "pcm_s16le" in sidecar
    assert "-decoders" in sidecar
    assert "matroska,webm" in sidecar
    assert "mov,mp4,m4a,3gp,3g2,mj2" in sidecar
    assert "-protocols" in sidecar
    assert "pipe" in sidecar
    assert "ffprobe was not found on PATH" in sidecar
    assert "ffprobe was not found in MediaToolsDir" in sidecar
    assert "executable failed validation" in sidecar
    assert '-Filter "*.dll"' in sidecar
    assert "[switch]$SkipBundledFfprobe" in sidecar
    assert 'Copy-MediaTools -SidecarDir $sidecarDir -SearchDir $MediaToolsDir -SkipFfprobe ([bool]$SkipBundledFfprobe) -ValidateSlimBundle ([bool]$ValidateSlimMediaTools)' in sidecar
    assert "Skipping bundled ffprobe" in sidecar
    assert "sidecar-build-metadata.json" in sidecar
    assert "sidecar-cache-save" in sidecar
    assert "rust-audio-sidecar-build" in sidecar
    assert '$entry["sha256"] = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()' in sidecar
    assert '$entry["lastWriteTimeUtc"] = $item.LastWriteTimeUtc.ToString("o")' in sidecar


def test_sidecar_cache_key_includes_frontend_dist() -> None:
    sidecar = read_script("scripts/build_tauri_backend_sidecar.ps1")

    manifest_start = sidecar.index("function Get-SidecarInputManifest")
    manifest_end = sidecar.index("function Copy-DirectoryContents")
    manifest_block = sidecar[manifest_start:manifest_end]

    assert '"src"' in manifest_block
    assert '"Frontend\\dist\\public"' in manifest_block
    assert '"packaging\\scriber-backend.spec"' in manifest_block
    assert '"scripts\\check_backend_runtime_imports.py"' in manifest_block


def test_gyan_essentials_prepare_script_downloads_and_verifies_archive() -> None:
    script = read_script("scripts/prepare_gyan_ffmpeg_essentials.ps1")

    assert "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" in script
    assert "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256" in script
    assert "Invoke-WebRequest" in script
    assert "Get-FileHash -LiteralPath $archivePath -Algorithm SHA256" in script
    assert "Gyan FFmpeg essentials SHA256 mismatch" in script
    assert "Expand-Archive" in script
    assert "ffmpeg.exe" in script
    assert "ffprobe.exe" in script
    assert "mediaToolsDir" in script
    assert "gyan-release-essentials" in script


def test_release_build_can_opt_into_experimental_ffmpeg_only_media_bundle() -> None:
    build = read_script("scripts/build_windows.ps1")

    assert "[switch]$SkipBundledFfprobe" in build
    assert "[switch]$ValidateSlimMediaTools" in build
    assert "[switch]$UseProfileBFfmpeg" in build
    assert '[string]$MediaToolsDir = ""' in build
    assert "[switch]$ReuseSidecarIfUnchanged" in build
    assert "[switch]$PrunePySide6Translations" in build
    assert "[switch]$PrunePySide6UnusedPlugins" in build
    assert "[switch]$PrunePySide6SoftwareOpenGl" in build
    assert "[switch]$FastLocalInstaller" in build
    assert "[switch]$SkipPythonTests" in build
    assert "[switch]$SkipFrontendTypeCheck" in build
    assert "function Add-TauriBeforeBundleCommandSwitch" in build
    assert "function Add-TauriBeforeBundleCommandValueSwitch" in build
    assert "function Write-BuildTimingReport" in build
    assert 'SwitchName "-SkipBundledFfprobe"' in build
    assert 'SwitchName "-ValidateSlimMediaTools"' in build
    assert 'SwitchName "-UseProfileBFfmpeg"' in build
    assert 'SwitchName "-MediaToolsDir"' in build
    assert '$commandArgument = if ($Value -match' in build
    assert 'SwitchName "-ReuseSidecarIfUnchanged"' in build
    assert 'SwitchName "-PrunePySide6Translations"' in build
    assert '$ConfigText.Replace($copySwitch, " $SwitchName$copySwitch")' in build
    assert "build-timing.json" in build
    assert "finally {" in build
    assert "function Set-Utf8NoBomContent" in build
    assert "Set-Utf8NoBomContent -Path $tauriConfigPath -Value $currentTauriConfig" in build
    assert "Set-Utf8NoBomContent -Path $tauriConfigPath -Value $tauriConfigOriginal" in build
    assert "if ($FastLocalInstaller)" in build
    assert "$ReuseSidecarIfUnchanged = $true" in build
    assert "$SkipPythonTests = $true" in build
    assert "$UseProfileBFfmpeg = $true" in build
    assert "$PrunePySide6Translations = $true" in build
    assert "$PrunePySide6UnusedPlugins = $true" in build
    assert "$PrunePySide6SoftwareOpenGl = $true" not in build
    assert "if ($UseProfileBFfmpeg)" in build
    assert "$RunMediaPreparationSmoke = $true" in build
    assert "$RunRuntimeDependencyFootprint = $true" in build
    assert "$MaxBackendRuntimeDependencyMB = if ($UseProfileBFfmpeg) { 325 } else { 500 }" in build
    assert "$MaxMediaToolsRuntimeDependencyMB = if ($UseProfileBFfmpeg) { 10 } else { 210 }" in build
    assert "$MaxPySide6RuntimeDependencyMB = 65" in build
    assert "if (-not $SkipChecks -and -not $SkipPythonTests)" in build
    assert "if (-not $SkipChecks -and -not $SkipFrontendTypeCheck)" in build


def test_tauri_before_bundle_uses_profile_b_standard_media_tools() -> None:
    config = read_script("Frontend/src-tauri/tauri.conf.json")

    assert "-UseProfileBFfmpeg" in config
    assert "-ValidateSlimMediaTools" in config
    assert "-ReuseSidecarIfUnchanged" in config
    assert "-BundleRustAudioSidecar" in config
    assert "-UseGyanFfmpegEssentials" not in config


def test_release_workflow_builds_profile_b_media_tools_for_standard_build() -> None:
    workflow = read_script(".github/workflows/release-windows.yml")

    assert "Set up MSYS2" in workflow
    assert "msys2/setup-msys2@v2" in workflow
    assert "Build FFmpeg Profile B media tools" in workflow
    assert "scripts\\ffmpeg\\build_profile_b_msys2.ps1" in workflow
    assert "profile-b-msys2-build-report.json" in workflow
    assert "SCRIBER_RELEASE_MEDIA_TOOLS_DIR" in workflow
    assert '"-MediaToolsDir"' in workflow
    assert "$env:SCRIBER_RELEASE_MEDIA_TOOLS_DIR" in workflow
    assert '"-ValidateSlimMediaTools"' in workflow
    assert '"-PrunePySide6Translations"' in workflow
    assert '"-PrunePySide6UnusedPlugins"' in workflow
    assert '"-PrunePySide6SoftwareOpenGl"' not in workflow
    assert '"325"' in workflow
    assert '"10"' in workflow
    assert '"65"' in workflow
    assert "choco install ffmpeg" not in workflow
    assert "prepare_gyan_ffmpeg_essentials.ps1" not in workflow
    assert '"-RunMediaPreparationSmoke"' in workflow


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
        assert "function Write-Utf8NoBomJson" in script
        assert "Write-Utf8NoBomJson -Path $outputFull -Json $json" in script
        assert "ConvertTo-Json -Compress -Depth 8" in script

    assert "Write-SmokeJson -Payload $result -Path $OutputPath -Root $RepoRoot" in desktop
    assert "function Get-SmokeFailureDiagnostics" in desktop
    assert "failureDiagnostics = $failureDiagnostics" in desktop
    assert "if ($failure) {" in desktop
    assert "Write-SmokeJson -Payload ([pscustomobject]$result) -Path $OutputPath -Root $RepoRoot" in installer
    assert '$desktopSmokeOutputPath = Join-Path $RuntimeDataDir "installed-desktop-smoke.json"' in installer
    assert '"-OutputPath"' in installer
    assert "$smokeExitCode = $LASTEXITCODE" in installer
    assert "if (-not $result.ok) {" in installer
    assert "failureDiagnostics = $smoke.failureDiagnostics" in installer
    assert "desktopSmokeFailure = $desktopSmokeFailure" in installer


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
    assert "scriber-shell-support-bundle-smoke" in desktop
    assert "Support bundle leaked a raw Shell IPC pipe name" in desktop
    assert "[REDACTED_PIPE]" in desktop
    assert "Support bundle did not contain the Shell IPC pipe redaction marker" in desktop
    assert "support-bundle-capture-smoke" in desktop
    assert "Support bundle leaked a raw native audio endpoint ID" in desktop
    assert "[REDACTED_ENDPOINT_ID]" in desktop
    assert "Support bundle did not contain the native audio endpoint redaction marker" in desktop
    assert "audio-diagnostics.redacted.json" in desktop
    assert "microphone.nativeDeviceEvents" in desktop
    assert "microphone.rustAudioFallbackCircuit" in desktop
    assert "shellIpcAvailable" in desktop
    assert "function Test-NativeDeviceEventDiagnostics" in desktop
    assert "function Test-RustAudioFallbackCircuitDiagnostics" in desktop
    assert "wasapi-imm-notification" in desktop
    assert "comInitialized" in desktop
    assert "callbackAlive" in desktop
    assert "Rust audio fallback-circuit diagnostics" in desktop
    assert "rustAudioFallbackCircuit = $rustAudioFallbackCircuit" in desktop
    assert "registrationVerified = $true" in desktop
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
    assert "Access-Control-Request-Private-Network" in desktop
    assert "privateNetworkPreflight = $true" in desktop
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
    assert "[scriptblock]$FailurePredicate = $null" in desktop
    assert "Backend entered failure state while waiting for '$Label'" in desktop
    assert "function Convert-AudioDiagnosticsSummary" in desktop
    assert "/api/runtime/audio-diagnostics" in desktop
    assert "-CollectAudioDiagnostics $true" in desktop
    assert "Invoke-LiveMicStart" in desktop
    assert '-FailurePredicate { param($state) ([string]$state.recordingState -eq "failed") -or ([string]$state.status -eq "Error") }' in desktop
    assert "Invoke-LiveMicStop" in desktop
    assert "nonRecordingSampleCount = $nonRecordingSamples.Count" in desktop
    assert "[switch]$DisableLiveTextInjection" in desktop
    assert "$env:SCRIBER_DISABLE_TEXT_INJECTION = \"1\"" in desktop
    assert '[string]$LiveRecordingEnvFile = ""' in desktop
    assert '[string]$LiveRecordingDefaultStt = ""' in desktop
    assert '[string]$LiveRecordingSonioxMode = ""' in desktop
    assert "function Read-EnvFileAssignments" in desktop
    assert "Set-SmokeEnvironmentVariable -Name \"SCRIBER_DEFAULT_STT\" -Value $LiveRecordingDefaultStt" in desktop
    assert "Set-SmokeEnvironmentVariable -Name \"SCRIBER_SONIOX_MODE\" -Value $LiveRecordingSonioxMode" in desktop
    assert '[string]$LiveRecordingAudioEngine = ""' in desktop
    assert '[string]$LiveRecordingRustAudioCaptureMode = ""' in desktop
    assert "[switch]$LiveRecordingMicAlwaysOn" in desktop
    assert "$env:SCRIBER_AUDIO_ENGINE = $LiveRecordingAudioEngine" in desktop
    assert "$env:SCRIBER_RUST_AUDIO_WASAPI_CAPTURE = \"1\"" in desktop
    assert "$env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = \"1\"" in desktop
    assert "$env:SCRIBER_MIC_ALWAYS_ON = \"1\"" in desktop
    assert "-LiveRecordingRustAudioCaptureMode requires -LiveRecordingAudioEngine rust-wasapi." in desktop
    assert "textInjectionDisabled = $TextInjectionDisabled" in desktop
    assert "liveRecording = $liveRecording" in desktop
    assert "verified = $false" in desktop
    assert "runtimeLogs = $runtimeLogs" in desktop

    assert "[int]$LiveRecordingDurationSec = 0" in installer
    assert "[switch]$DisableLiveTextInjection" in installer
    assert '[string]$LiveRecordingEnvFile = ""' in installer
    assert '[string]$LiveRecordingDefaultStt = ""' in installer
    assert '[string]$LiveRecordingSonioxMode = ""' in installer
    assert '[string]$LiveRecordingAudioEngine = ""' in installer
    assert '[string]$LiveRecordingRustAudioCaptureMode = ""' in installer
    assert "[switch]$LiveRecordingMicAlwaysOn" in installer
    assert '"-LiveRecordingDurationSec", $LiveRecordingDurationSec.ToString()' in installer
    assert '"-LiveRecordingEnvFile", $LiveRecordingEnvFile' in installer
    assert '"-LiveRecordingDefaultStt", $LiveRecordingDefaultStt' in installer
    assert '"-LiveRecordingSonioxMode", $LiveRecordingSonioxMode' in installer
    assert '"-DisableLiveTextInjection"' in installer
    assert '"-LiveRecordingAudioEngine", $LiveRecordingAudioEngine' in installer
    assert '"-LiveRecordingRustAudioCaptureMode", $LiveRecordingRustAudioCaptureMode' in installer
    assert '"-LiveRecordingMicAlwaysOn"' in installer
    assert "appPid = $smoke.appPid" in installer
    assert "backendPid = $smoke.backendPid" in installer
    assert "backendPort = $smoke.backendPort" in installer
    assert "apiVersion = $smoke.apiVersion" in installer
    assert "ready = $smoke.ready" in installer
    assert "liveRecording = $smoke.liveRecording" in installer
    assert "ok = $smokeOk" in installer
    assert "$desktopSmokeFailure = if (-not $smokeOk) { $smoke } else { $null }" in installer

    assert "[switch]$RunInstallerLiveRecordingSmoke" in build
    assert "[switch]$InstallerDisableLiveTextInjection" in build
    assert '[string]$InstallerLiveRecordingAudioEngine = ""' in build
    assert '[string]$InstallerLiveRecordingRustAudioCaptureMode = ""' in build
    assert '[string]$InstallerLiveRecordingEnvFile = ""' in build
    assert '[string]$InstallerLiveRecordingDefaultStt = ""' in build
    assert '[string]$InstallerLiveRecordingSonioxMode = ""' in build
    assert "[switch]$InstallerLiveRecordingMicAlwaysOn" in build
    assert "[int]$InstallerLiveRecordingDurationSec = 0" in build
    assert "$RunInstallerLiveRecordingSmoke" in build
    assert '"-LiveRecordingDurationSec", $liveDuration.ToString()' in build
    assert '"-LiveRecordingEnvFile", $InstallerLiveRecordingEnvFile' in build
    assert '"-LiveRecordingDefaultStt", $InstallerLiveRecordingDefaultStt' in build
    assert '"-LiveRecordingSonioxMode", $InstallerLiveRecordingSonioxMode' in build
    assert '"-DisableLiveTextInjection"' in build
    assert '"-LiveRecordingAudioEngine", $InstallerLiveRecordingAudioEngine' in build
    assert '"-LiveRecordingRustAudioCaptureMode", $InstallerLiveRecordingRustAudioCaptureMode' in build
    assert '"-LiveRecordingMicAlwaysOn"' in build
    assert '"-MaxLiveBackendWorkingSetGrowthMB", $InstallerMaxLiveBackendWorkingSetGrowthMB.ToString' in build
