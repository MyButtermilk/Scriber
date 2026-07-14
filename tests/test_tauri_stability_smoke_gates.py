from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_script(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_tauri_shell_defers_backend_start_off_setup_hot_path() -> None:
    lib = read_script("Frontend/src-tauri/src/lib.rs")

    assert "early backend ensure deferred until Tauri setup completes" in lib
    assert "setup backend ensure and global hotkey registration deferred to supervisor" in lib
    assert "let setup_backend_status = manager.ensure_started();" not in lib
    assert "global hotkey registration skipped:" not in lib
    assert "let status = manager.ensure_started();" in lib
    assert "std::thread::sleep(BACKEND_SUPERVISOR_INTERVAL);" in lib


def test_main_window_close_hides_to_tray_without_destroying_restore_target() -> None:
    lib = read_script("Frontend/src-tauri/src/lib.rs")

    assert ".on_window_event(|window, event|" in lib
    assert "should_hide_window_instead_of_closing(window.label())" in lib
    assert "tauri::WindowEvent::CloseRequested" in lib
    assert "api.prevent_close();" in lib
    assert "window.hide()" in lib
    assert "hidden to tray instead of destroyed" in lib


def test_unexpected_backend_exit_stops_all_audio_sidecars_before_recovery() -> None:
    lib = read_script("Frontend/src-tauri/src/lib.rs")

    assert 'shutdown_all_audio_sidecars(\n                    "managedBackendExitedUnexpectedly"' in lib
    assert 'shutdown_all_audio_sidecars(\n                    "managedBackendInspectionFailed"' in lib
    assert "after unexpected backend exit pid={pid}" in lib
    assert "after backend inspection failure pid={pid}" in lib


def test_every_managed_backend_replacement_drains_audio_sidecars_first() -> None:
    lib = read_script("Frontend/src-tauri/src/lib.rs")
    function = lib.split("fn terminate_managed_child", 1)[1].split("\n    fn ", 1)[0]
    assert "shutdown_all_audio_sidecars" in function
    assert '"managedBackendReplacement"' in function
    assert function.index("shutdown_all_audio_sidecars") < function.index("request_backend_shutdown")


def test_meeting_status_keeps_sidecar_until_stop_collects_diagnostics() -> None:
    client = read_script("Frontend/src-tauri/src/audio_sidecar_client.rs")

    assert "A meeting status check is observational" in client
    assert "if remove || !response.success {" in client


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


def test_desktop_smoke_verifies_single_instance_and_audio_sidecar_cleanup() -> None:
    script = read_script("scripts/smoke_tauri_desktop.ps1")

    assert "[switch]$VerifySingleInstance" in script
    assert "[string[]]$SingleInstanceSecondArguments = @()" in script
    assert "[string[]]$SingleInstanceForbiddenLogText = @()" in script
    assert "function Test-SingleInstanceBlock" in script
    assert "SecondArgumentList" in script
    assert "ForbiddenLogText" in script
    assert "another Scriber desktop instance is already running" in script
    assert "single-instance main-window restore requested" in script
    assert "mainWindowRestoreObserved" in script
    assert "Single-instance forbidden probe text leaked to runtime logs." in script
    assert "secondArgumentCount" in script
    assert "forbiddenLogTextAbsent" in script
    assert "noManagedBackendSpawned = $true" in script
    assert "singleInstance = $singleInstance" in script

    assert "[switch]$VerifyAudioSidecarCleanup" in script
    assert "function Start-InstalledAudioSidecarStray" in script
    assert "$env:SCRIBER_AUDIO_SIDECAR_EXE = $audioSidecarProbePath" in script
    assert "$env:SCRIBER_AUDIO_SIDECAR_EXE = $oldAudioSidecarExe" in script
    assert "startupCleanupVerified" in script
    assert "noRemainingInstalledSidecarsAfterExit" in script
    assert "audioSidecarCleanup = $audioSidecarCleanup" in script

    assert "[switch]$VerifyShellMenuSmoke" in script
    assert '[string]$ShellMenuSmokeActions = "show-window,quit"' in script
    assert "function Test-ShellMenuSmoke" in script
    assert "SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTIONS" in script
    assert "SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_FILE" in script
    assert "shell menu smoke action show-window completed" in script
    assert "shell menu smoke action quit completed" in script
    assert "Shell menu smoke Open Scriber did not leave the main window visible." in script
    assert "shellMenuSmoke = $shellMenuSmoke" in script


def test_installer_and_build_scripts_forward_memory_growth_gate() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[double]$MaxBackendWorkingSetGrowthMB = 0" in installer
    assert '"-MaxBackendWorkingSetGrowthMB", $MaxBackendWorkingSetGrowthMB.ToString' in installer
    assert "[double]$InstallerMaxBackendWorkingSetGrowthMB = 0" in build
    assert '"-MaxBackendWorkingSetGrowthMB", $InstallerMaxBackendWorkingSetGrowthMB.ToString' in build


def test_installer_smoke_uses_only_the_current_versioned_artifact() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "src\\version.py" in installer
    assert "Scriber_${currentVersion}_x64-setup.exe" in installer
    assert "Scriber_0.4.2_x64-setup.exe" not in installer
    assert '"-InstallerPath",' in build
    assert "$artifacts[0]" in build


def test_installer_smoke_gates_meeting_notices_and_optional_model_absence() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")

    assert "function Test-InstalledMeetingResources" in installer
    assert 'Join-Path $Root "THIRD_PARTY_NOTICES.md"' in installer
    assert "aec3 0\\.2\\.0" in installer
    assert "Optional WeSpeaker model must not be bundled" in installer
    assert "meetingResources = $meetingResources" in installer


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
    assert '"scriber-audio-sidecar.exe"' in installer
    assert "Resolve-InstalledAudioSidecarExe -Root $InstallDir" in installer
    assert "audioSidecarExe = $audioSidecarExe" in installer


def test_release_and_installer_smokes_attest_static_diarization_worker() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")
    smoke = read_script("scripts/smoke_diarization_worker_resource.py")

    assert "scripts\\smoke_diarization_worker_resource.py" in installer
    assert "diarizationWorker = $diarizationWorker" in installer
    assert "Diarization worker staged resource smoke" in build
    assert "diarization-worker-staged-smoke.json" in build
    assert "diarizationWorkerStagedSmokeValidated = $true" in build
    assert "--self-test" in smoke
    assert "linkMode" in smoke
    assert "_pe_imports" in smoke
    assert "Optional diarization models are present in the base package" in smoke


def test_release_build_and_installer_smoke_report_size_budgets() -> None:
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[double]$MaxInstallerSizeMB = 220" in build
    assert "[double]$InstallerMaxInstalledSizeMB = 0" in build
    assert "scripts\\create_release_size_report.py" in build
    assert "--max-installer-mb" in build
    assert "--installed-smoke-report" in build
    assert "installed-package-smoke.json" in build
    assert "size-report.json" in build
    assert '"-OutputPath",' in build
    assert "$installedPackageSmokePath" in build
    assert '"-MaxInstalledSizeMB", $InstallerMaxInstalledSizeMB.ToString' in build

    assert "[double]$MaxInstalledSizeMB = 0" in installer
    assert "function Get-DirectorySizeReport" in installer
    assert "Installed app size ${totalMb} MB exceeds budget ${MaxSizeMB} MB." in installer
    assert "installSize = $installSize" in installer


def test_sidecar_build_requires_and_validates_bundled_media_tools() -> None:
    sidecar = read_script("scripts/build_tauri_backend_sidecar.ps1")

    assert "function Test-MediaToolExecutable" in sidecar
    assert "function Test-ScriberFfmpegCapabilities" in sidecar
    assert "function Invoke-ScriberFfmpegFixtureSmoke" in sidecar
    assert "function Invoke-ScriberFfmpegProfileManifest" in sidecar
    assert "function Get-SidecarInputManifest" in sidecar
    assert "function Sync-DirectoryContents" in sidecar
    assert "function Copy-FileIfChanged" in sidecar
    assert "function Get-RustAudioSidecarInputManifest" in sidecar
    assert r'Frontend\src-tauri\src\meeting_aec.rs' in sidecar
    assert "function Write-SidecarBuildMetadata" in sidecar
    assert "PySide6" not in sidecar
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
    assert "[switch]$BundleRustAudioSidecar" in sidecar
    assert "[switch]$BundleRustDiarizationSidecar" in sidecar
    assert "function Copy-RustDiarizationSidecarToBackend" in sidecar
    assert "cargo build --release --locked --target-dir $CargoTargetRoot" in sidecar
    assert "SHERPA_ONNX_ARCHIVE_DIR" in sidecar
    assert "f6555701d6397d74f1302b0666a661f32708b599a14a5fde80835d4902fcd315" in sidecar
    assert r"scripts\write_diarization_worker_manifest.py" in sidecar
    assert r"scripts\smoke_diarization_worker_resource.py" in sidecar
    assert "rust-diarization-sidecar-cache" in sidecar
    assert "sherpa-onnx-archive-cache" in sidecar
    assert "rust-diarization-sidecar-build" in sidecar
    assert "rustDiarizationSidecarCopied = $RustDiarizationSidecarCopied" in sidecar
    assert "function Copy-RustAudioSidecarToTauriRelease" in sidecar
    assert "cargo build --release --bin scriber-audio-sidecar" in sidecar
    assert "--target-dir $cargoTargetDir" in sidecar
    assert "rust-audio-sidecar-cache" in sidecar
    assert "rust-audio-sidecar-target" in sidecar
    assert '"target\\release"' in sidecar
    assert "audio-sidecar-build-metadata.json" in sidecar
    assert 'Test-ScriberFfmpegCapabilities -Path $copiedFfmpeg' in sidecar
    assert '-Label "copied"' in sidecar
    assert '-Label "target-current"' in sidecar
    assert '"--meeting-only"' in sidecar
    assert 'foreach ($filter in @("adelay"' in sidecar
    assert 'Test-ScriberFfmpegCapabilities -Path (Join-Path $script:PreparedProfileBMediaToolsDir "ffmpeg.exe")' in sidecar
    assert "Ignoring stale or unusable Profile B build report" in sidecar
    assert "sidecarSha256 = Get-Sha256Hex -Path $cachedSidecarExe" in sidecar
    assert 'Get-ObjectPropertyValue -Object $rustAudio -Name "sha256"' in sidecar
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
    assert "Stale audio sidecar resource" in sidecar
    assert "Stale packaged audio sidecar resource" in sidecar
    assert "function Get-Sha256Hex" in sidecar
    assert "sha256 = Get-Sha256Hex -Path $item.FullName" in sidecar
    assert "lastWriteTimeUtc" not in sidecar


def test_sidecar_cache_key_excludes_frontend_dist() -> None:
    sidecar = read_script("scripts/build_tauri_backend_sidecar.ps1")
    spec = read_script("packaging/scriber-backend.spec")

    manifest_start = sidecar.index("function Get-SidecarInputManifest")
    manifest_end = sidecar.index("function Copy-DirectoryContents")
    manifest_block = sidecar[manifest_start:manifest_end]

    assert '"src"' in manifest_block
    assert '"Frontend\\dist\\public"' not in manifest_block
    assert '"packaging\\scriber-backend.spec"' in manifest_block
    assert '"scripts\\check_backend_runtime_imports.py"' in manifest_block
    assert "Frontend/dist/public" not in spec
    assert "Frontend\" / \"dist\" / \"public" not in spec


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

    assert "PrunePySide6" not in build
    assert "[switch]$SkipBundledFfprobe" in build
    assert "[switch]$ValidateSlimMediaTools" in build
    assert "[switch]$UseProfileBFfmpeg" in build
    assert '[string]$MediaToolsDir = ""' in build
    assert "[switch]$ReuseSidecarIfUnchanged" in build
    assert "[switch]$FastLocalInstaller" in build
    assert "[switch]$SkipPythonTests" in build
    assert "[switch]$SkipFrontendTypeCheck" in build
    assert "function Write-BuildTimingReport" in build
    assert "function New-TauriBundleLogSummary" in build
    assert "function Get-TauriLogRecords" in build
    assert "function Remove-AnsiEscapeSequences" in build
    assert '"\\x1B\\[[0-?]*[ -/]*[@-~]"' in build
    assert '"\\^\\[\\[[0-?]*[ -/]*[@-~]"' in build
    assert "cleanMessage = Remove-AnsiEscapeSequences -Value $message" in build
    assert "tauri-windows-bundle.log" in build
    assert "tauri-bundle-log-summary.json" in build
    assert '[System.IO.StreamWriter]::new($tauriBundleLogPath' in build
    assert "cmd.exe /d /s /c $tauriCommand" in build
    assert "2>&1' -f $quotedBundleArg, $quotedConfigPath" in build
    assert '$tauriLogWriter.WriteLine(("{0}`t{1}"' in build
    assert "Write-Host $line" in build
    assert "$failureBuildMode" in build
    assert "failed = $true" in build
    assert "cargoCompiling" in build
    assert 'cargoFinished = Get-LogMatchCount -Lines $messageLines -Pattern "(?i)^\\s*Finished\\s+.*profile.*target\\(s\\)"' in build
    assert "firstLineToMakensisSeconds" in build
    assert "makensisToUpdaterSignatureSeconds" in build
    assert "firstCargoCompileLines" in build
    assert "build-timing.json" in build
    assert "Prepare Tauri build config" in build
    assert "scripts\\prepare_tauri_updater_config.py" in build
    assert "--remove-before-bundle-command" in build
    assert "--skip-updater-config" in build
    assert "--nsis-compression" in build
    assert "npm run tauri:build -- --bundles \"{0}\" --config \"{1}\" --ci 2>&1" in build
    assert "npm run tauri:bundle -- --bundles \"{0}\" --config \"{1}\" --ci 2>&1" in build
    assert "[switch]$UsePrebuiltTauriApp" in build
    assert "[switch]$ConfigureTauriUpdaterRuntime" in build
    assert "function Add-TauriBeforeBundleCommandSwitch" not in build
    assert "function Add-TauriBeforeBundleCommandValueSwitch" not in build
    assert "function Set-Utf8NoBomContent" not in build
    assert "Set-Utf8NoBomContent -Path $tauriConfigPath" not in build
    assert "if ($FastLocalInstaller)" in build
    assert "$ReuseSidecarIfUnchanged = $true" in build
    assert "$SkipPythonTests = $true" in build
    assert "$UseProfileBFfmpeg = $true" in build
    assert "if ($UseProfileBFfmpeg)" in build
    assert "$RunMediaPreparationSmoke = $true" in build
    assert "$RunRuntimeDependencyFootprint = $true" in build
    assert "$MaxBackendRuntimeDependencyMB = if ($UseProfileBFfmpeg) { 325 } else { 500 }" in build
    assert "$MaxMediaToolsRuntimeDependencyMB = if ($UseProfileBFfmpeg) { 115 } else { 315 }" in build
    assert "$MaxPySide6RuntimeDependencyMB = 65" not in build
    assert "if (-not $SkipChecks -and -not $SkipPythonTests)" in build
    assert "if (-not $SkipChecks -and -not $SkipFrontendTypeCheck)" in build


def test_tauri_before_bundle_uses_profile_b_standard_media_tools() -> None:
    config = read_script("Frontend/src-tauri/tauri.conf.json")

    assert "-UseProfileBFfmpeg" in config
    assert "-ValidateSlimMediaTools" in config
    assert "-ReuseSidecarIfUnchanged" in config
    assert "-BundleRustAudioSidecar" in config
    assert "-BundleRustDiarizationSidecar" in config
    assert "-UseGyanFfmpegEssentials" not in config


def test_diarization_worker_stays_outside_tauri_and_audio_cargo_graph() -> None:
    tauri_cargo = read_script("Frontend/src-tauri/Cargo.toml")
    tauri_lock = read_script("Frontend/src-tauri/Cargo.lock")
    worker_cargo = read_script("native/scriber-diarization-sidecar/Cargo.toml")

    assert "sherpa-onnx" not in tauri_cargo
    assert "sherpa-onnx" not in tauri_lock
    assert 'sherpa-onnx = { version = "=1.13.3"' in worker_cargo
    assert 'sherpa-onnx-sys = { version = "=1.13.3"' in worker_cargo


def test_tauri_desktop_build_only_emits_rlib_for_shell_library() -> None:
    cargo = read_script("Frontend/src-tauri/Cargo.toml")

    assert 'crate-type = ["rlib"]' in cargo
    assert 'crate-type = ["staticlib", "cdylib", "rlib"]' not in cargo


def test_release_workflow_builds_profile_b_media_tools_for_standard_build() -> None:
    workflow = read_script(".github/workflows/release-windows.yml")

    assert "PrunePySide6" not in workflow
    assert "Set up MSYS2" in workflow
    assert "msys2/setup-msys2@v2" in workflow
    assert "Build FFmpeg Profile B media tools" in workflow
    assert "scripts\\ffmpeg\\build_profile_b_msys2.ps1" in workflow
    assert "profile-b-msys2-build-report.json" in workflow
    assert "SCRIBER_RELEASE_MEDIA_TOOLS_DIR" in workflow
    assert '"-MediaToolsDir"' in workflow
    assert "$env:SCRIBER_RELEASE_MEDIA_TOOLS_DIR" in workflow
    assert '"-ValidateSlimMediaTools"' in workflow
    assert '"325"' in workflow
    assert '"115"' in workflow
    assert "choco install ffmpeg" not in workflow
    assert "prepare_gyan_ffmpeg_essentials.ps1" not in workflow
    assert '"-RunMediaPreparationSmoke"' in workflow


def test_release_workflow_uses_incremental_dependency_caches() -> None:
    workflow = read_script(".github/workflows/release-windows.yml")

    assert "branches:\n      - main" not in workflow
    assert 'tags:\n      - "v*"' in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.event.inputs.refresh_release_cache_artifacts == 'true'" in workflow
    assert 'CARGO_INCREMENTAL: "1"' in workflow
    assert "CARGO_LOG: ${{ vars.SCRIBER_CARGO_LOG }}" in workflow
    assert "cache: pip" in workflow
    assert "cache-dependency-path: |\n            requirements-base.txt\n            requirements-build.txt" in workflow
    assert "cache: npm" in workflow
    assert "cache-dependency-path: build/cache-keys/frontend-dependencies.txt" in workflow
    assert "Using NSIS compression 'none' for non-tag cache/warmup build" in workflow
    assert "Using Tauri default NSIS compression for tag release" in workflow
    assert '$effectiveNsisCompression = "none"' in workflow
    assert '$buildArgs += @("-MaxInstallerSizeMB", "0")' in workflow
    assert "Recording uncompressed non-tag installer size without applying the compressed release budget" in workflow
    assert "Compute release cache keys" in workflow
    assert "scripts\\ci\\write_release_cache_keys.ps1" in workflow
    assert "Report release cache key fingerprints" in workflow
    assert "Release cache key fingerprints" in workflow
    assert "If a cache misses, compare these fingerprints with the previous run" in workflow
    assert "build/cache-keys/frontend-dependencies.txt" in workflow
    assert "build/cache-keys/rust-dependencies.txt" in workflow
    assert "build/cache-keys/rust-release.txt" in workflow
    assert "build/cache-keys/tauri-app-binary.txt" in workflow
    assert '"runtime`tsource-commit`t$env:GITHUB_SHA"' in workflow
    assert "SCRIBER_OUTLOOK_CLIENT_ID: ${{ vars.SCRIBER_OUTLOOK_CLIENT_ID }}" in workflow
    assert "Validate Outlook release configuration" in workflow
    assert "if: startsWith(github.ref, 'refs/tags/')" in workflow
    assert workflow.index("Validate Outlook release configuration") < workflow.index(
        "Compute release cache keys"
    )
    assert '[Guid]::TryParseExact($clientId, "D", [ref]$parsed)' in workflow
    assert "Official tag releases require a valid SCRIBER_OUTLOOK_CLIENT_ID" in workflow
    assert '"runtime`toutlook-client-id-present`t$($outlookClientIdPresent.ToString().ToLowerInvariant())"' in workflow
    assert '"runtime`toutlook-client-id-sha256`t$outlookClientIdHash"' in workflow
    assert '"runtime`toutlook-client-id`t$outlookClientId"' not in workflow
    assert "build/cache-keys/rust-audio-sidecar.txt" in workflow
    assert "build/cache-keys/rust-diarization-sidecar.txt" in workflow
    assert "build/cache-keys/sherpa-onnx-archive.txt" in workflow
    assert "build/cache-keys/backend-sidecar.txt" in workflow
    assert "Restore Python wheelhouse cache" in workflow
    assert "scriber-python-wheelhouse-" in workflow
    assert 'Name = "Python pip download store"' in workflow
    assert "pip wheel --wheel-dir $wheelhouse --prefer-binary" in workflow
    assert "requirements-dev.txt" not in workflow
    assert "npm ci --prefer-offline --no-audit --fund=false" in workflow
    assert "npm ls --depth=0 --silent" not in workflow
    assert "node_modules\\.package-lock.json" in workflow
    assert "Cached Frontend/node_modules is missing npm install metadata" in workflow
    assert "Restore frontend dependency cache" in workflow
    assert "path: Frontend/node_modules" in workflow
    assert 'Name = "npm package store"' in workflow
    assert "not-needed-node_modules" in workflow
    assert "hashFiles('build/cache-keys/frontend-dependencies.txt')" in workflow
    assert "key: scriber-backend-sidecar-v2-${{ runner.os }}-python-${{ steps.setup-python.outputs.python-version }}-${{ hashFiles('build/cache-keys/backend-sidecar.txt') }}" in workflow
    assert "scriber-backend-sidecar-${{ runner.os }}-python-${{ steps.setup-python.outputs.python-version }}-${{ hashFiles('build/cache-keys/backend-sidecar.txt') }}.zip" in workflow
    assert "Resolve cached FFmpeg Profile B media tools" in workflow
    assert "Restore FFmpeg Profile B release artifact" in workflow
    assert "Publish FFmpeg Profile B release artifact" in workflow
    assert "Restore Rust audio sidecar cache" in workflow
    assert "Frontend/src-tauri/target/release/incremental" in workflow
    assert "scriber-rust-release-v2-${{ runner.os }}" in workflow
    assert "scriber-rust-audio-sidecar-" in workflow
    assert "scriber-rust-diarization-sidecar-" in workflow
    assert "scriber-sherpa-onnx-archive-" in workflow
    assert 'Name = "Rust audio sidecar"' in workflow
    assert 'Name = "Rust diarization sidecar"' in workflow
    assert 'Name = "Sherpa ONNX static archive"' in workflow
    assert "Cache layer summary:" in workflow
    assert "function Get-ActionsCacheLayer" in workflow
    assert 'if ($ActionsCacheHit -eq "false")' in workflow
    assert 'return "restore-key-or-miss"' in workflow
    assert 'return "empty"' in workflow
    assert 'return "actions-cache-restore-key-or-miss"' in workflow
    assert "Actions cache layer `restore-key-or-miss` means GitHub returned `cache-hit=false`" in workflow
    assert "Inspect path evidence and release-artifact rows before treating it as a rebuild" in workflow
    assert "Cache path evidence:" in workflow
    assert "build\\release-cache-summary.json" in workflow
    assert "Wrote machine-readable cache summary" in workflow
    assert "ConvertTo-Json -Depth 6" in workflow
    assert 'Get-PathEvidence "Rust build" @(".cargo\\registry\\index", ".cargo\\registry\\cache", ".cargo\\git\\db", "Frontend\\src-tauri\\target\\release\\.fingerprint", "Frontend\\src-tauri\\target\\release\\deps", "Frontend\\src-tauri\\target\\release\\incremental")' in workflow
    assert 'Get-PathEvidence "Rust build target cache"' in workflow
    assert "| Cache path | Exists | Non-empty | Existing paths |" in workflow
    assert "Copy-Item -LiteralPath build\\release-cache-summary.json -Destination release-artifacts\\" in workflow
    assert "Copy-Item Frontend\\src-tauri\\target\\release\\release-metadata\\*.log release-artifacts\\ -ErrorAction SilentlyContinue" in workflow
    assert "path: build/tauri-sidecar-cache" in workflow
    assert "build/rust-audio-sidecar-cache\n          key: scriber-backend-sidecar" not in workflow
    assert "Report Windows installer timing" in workflow
    assert "continue-on-error: true" in workflow
    assert "build-timing.json" in workflow
    assert "Windows installer timing" in workflow
    assert "$summary = [System.Collections.Generic.List[string]]::new()" in workflow
    assert '[void]$summary.Add("Phase timings:")' in workflow
    assert '[void]$summary.Add(("- {0}: {1}s ({2})" -f $phase.label, $seconds, $status))' in workflow
    assert "$summary | Out-File -FilePath $env:GITHUB_STEP_SUMMARY -Append -Encoding utf8" in workflow
    assert "Failed to append Windows installer timing summary" in workflow
    assert "Use this timing summary with the cache summary above before changing dependency caches" in workflow
    assert "Collect failure diagnostics" in workflow
    assert "scriber-windows-failure-diagnostics" in workflow
    assert "Summarize release artifact timing" in workflow
    assert "scripts\\summarize_release_artifacts.py release-artifacts --output release-artifacts\\release-artifact-summary.json" in workflow
    assert "Release artifact timing brief" in workflow
    assert "release-artifacts\\release-artifact-summary.json" in workflow
    assert "restore_profile_b_release_artifact.ps1" in workflow
    assert "publish_profile_b_release_artifact.ps1" in workflow
    assert "ffmpeg-profile-b-n7.0-v4" in workflow
    assert 'Name = "FFmpeg Profile B"' in workflow
    assert "$ffmpegProfileBArtifactRestored" in workflow
    assert "mode = \"release-artifact\"" in workflow
    assert "ffmpeg-profile-b-resolve.outputs.usable != 'true'" in workflow
    assert "Restored Profile B media tools were not usable" in workflow
    assert "scriber-ffmpeg-profile-b-msys2-n7.0-v4-" in workflow
    assert "Restore backend sidecar cache" in workflow
    assert "Report release cache hits" in workflow
    assert "Export Rust build release artifact" in workflow
    assert (
        "env.SCRIBER_PUBLISH_RELEASE_CACHE_ARTIFACTS == 'true' && "
        "steps.rust-build-artifact.outputs.exact != 'true'"
    ) in workflow
    assert "steps.rust-build-cache.outputs.cache-matched-key == ''" in workflow
    assert "SCRIBER_SAVE_ACTIONS_CACHES" in workflow
    assert "SCRIBER_PUBLISH_RELEASE_CACHE_ARTIFACTS" in workflow
    assert "Select current backend sidecar cache entry" in workflow
    assert "scripts\\ci\\select_backend_sidecar_cache_entry.ps1" in workflow
    assert "Restore exact Tauri app binary" in workflow
    assert "scripts\\ci\\sync_tauri_app_binary_cache.ps1" in workflow


def test_release_cache_key_script_normalizes_version_only_churn() -> None:
    script = read_script("scripts/ci/write_release_cache_keys.ps1")

    assert "frontend-dependencies.txt" in script
    assert "rust-dependencies.txt" in script
    assert "rust-release.txt" in script
    assert "tauri-app-binary.txt" in script
    assert "rust-audio-sidecar.txt" in script
    assert "rust-diarization-sidecar.txt" in script
    assert "sherpa-onnx-archive.txt" in script
    assert "backend-sidecar.txt" in script
    assert "__app_version__" in script
    assert "Normalize-FirstJsonVersionProperties" in script
    assert "Normalize-CargoToml" in script
    assert "Normalize-CargoLock" in script
    assert "Normalize-PythonVersionFile" in script
    assert 'Add-RawFileEntry -Entries $rustEntries -Path "Frontend/src-tauri/tauri.conf.json"' in script
    assert 'Add-FileGlobEntries -Entries $rustEntries -Root "Frontend/src-tauri/capabilities" -Filter "*.json"' in script
    assert 'Add-FileGlobEntries -Entries $rustEntries -Root "Frontend/src-tauri/icons" -Filter "*"' in script
    assert '"scripts/check_backend_runtime_imports.py"' in script
    assert 'Add-FileGlobEntries -Entries $backendEntries -Root "pyloudnorm" -Filter "*.py"' in script
    assert 'constant`tffmpeg-profile`tffmpeg-profile-b-n7.0-v4' in script
    assert 'constant`tbackend-sidecar-flags`tBundleMediaTools;UseProfileB;ValidateSlim' in script
    backend_block = script.split("$backendEntries = New-EntryList", 1)[1]
    assert "Frontend/src-tauri/Cargo.toml" not in backend_block
    assert "Frontend/src-tauri/src/audio_sidecar.rs" not in backend_block


def test_tauri_build_embeds_validated_outlook_configuration_for_backend() -> None:
    build_script = read_script("Frontend/src-tauri/build.rs")
    desktop = read_script("Frontend/src-tauri/src/lib.rs")
    outlook = read_script("Frontend/src-tauri/src/outlook_config.rs")

    assert "cargo:rerun-if-env-changed=SCRIBER_OUTLOOK_CLIENT_ID" in build_script
    assert 'option_env!("SCRIBER_OUTLOOK_CLIENT_ID")' in outlook
    assert "Uuid::parse_str" in outlook
    assert "parsed.is_nil()" in outlook
    assert "built_in" in outlook and "runtime" in outlook
    assert "command.env_remove(outlook_config::CLIENT_ID_ENV)" in desktop
    assert "outlook_config::configured_client_id()" in desktop


def test_release_cache_key_outputs_are_stable_for_version_only_churn() -> None:
    tracked_paths = [
        REPO_ROOT / "src" / "version.py",
        REPO_ROOT / "Frontend" / "package.json",
        REPO_ROOT / "Frontend" / "package-lock.json",
        REPO_ROOT / "Frontend" / "src-tauri" / "Cargo.toml",
        REPO_ROOT / "Frontend" / "src-tauri" / "Cargo.lock",
    ]
    original_bytes = {path: path.read_bytes() for path in tracked_paths}
    originals = {path: original_bytes[path].decode("utf-8") for path in tracked_paths}
    output_root = REPO_ROOT / "tmp" / f"cache-key-normalization-{uuid.uuid4().hex}"

    def run_cache_key_script(name: str) -> dict[str, str]:
        relative_output = str((Path("tmp") / output_root.name / name)).replace("/", "\\")
        result = subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                "scripts\\ci\\write_release_cache_keys.ps1",
                "-OutputDir",
                relative_output,
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        resolved = REPO_ROOT / relative_output
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(resolved.glob("*.txt"))
        }

    try:
        before = run_cache_key_script("before")

        version_file = REPO_ROOT / "src" / "version.py"
        version_file.write_bytes(
            re.sub(
                r'(?m)^__version__\s*=\s*"[^"]+"',
                '__version__ = "9.9.9"',
                originals[version_file],
            ).encode("utf-8"),
        )

        package_json = REPO_ROOT / "Frontend" / "package.json"
        package_json.write_bytes(
            re.sub(
                r'(?m)^(\s*"version"\s*:\s*)"[^"]+"',
                r'\g<1>"9.9.9"',
                originals[package_json],
                count=1,
            ).encode("utf-8")
        )

        package_lock = REPO_ROOT / "Frontend" / "package-lock.json"
        package_lock.write_bytes(
            re.sub(
                r'(?m)^(\s*"version"\s*:\s*)"[^"]+"',
                r'\g<1>"9.9.9"',
                originals[package_lock],
                count=2,
            ).encode("utf-8")
        )

        cargo_toml = REPO_ROOT / "Frontend" / "src-tauri" / "Cargo.toml"
        cargo_toml.write_bytes(
            re.sub(
                r'(?m)^(version\s*=\s*)"[^"]+"',
                r'\g<1>"9.9.9"',
                originals[cargo_toml],
                count=1,
            ).encode("utf-8"),
        )

        cargo_lock = REPO_ROOT / "Frontend" / "src-tauri" / "Cargo.lock"
        cargo_lock.write_bytes(
            re.sub(
                r'(?ms)(\[\[package\]\]\s+name = "scriber-desktop"\s+version = )"[^"]+"',
                r'\g<1>"9.9.9"',
                originals[cargo_lock],
            ).encode("utf-8"),
        )

        after = run_cache_key_script("after")

        tauri_before = before.pop("tauri-app-binary.txt")
        tauri_after = after.pop("tauri-app-binary.txt")
        assert before == after
        assert tauri_before != tauri_after
    finally:
        for path, content in original_bytes.items():
            path.write_bytes(content)
        shutil.rmtree(output_root, ignore_errors=True)


def test_rust_audio_sidecar_cache_key_ignores_app_version_only_churn() -> None:
    script = read_script("scripts/build_tauri_backend_sidecar.ps1")

    assert "Normalize-CargoTomlForCache" in script
    assert "Normalize-CargoLockForCache" in script
    assert 'version = "__app_version__"' in script
    assert "scriber-desktop" in script
    assert "Get-NormalizedFileHashEntry -Root $Root -RelativePath \"Frontend\\src-tauri\\Cargo.toml\"" in script
    assert "Get-NormalizedFileHashEntry -Root $Root -RelativePath \"Frontend\\src-tauri\\Cargo.lock\"" in script


def test_native_recording_overlay_is_tauri_owned() -> None:
    web_api = read_script("src/web_api.py")
    native_overlay_py = read_script("src/native_overlay.py")
    native_overlay_rs = read_script("Frontend/src-tauri/src/native_overlay.rs")
    lib_rs = read_script("Frontend/src-tauri/src/lib.rs")
    shell_ipc = read_script("Frontend/src-tauri/src/shell_ipc.rs")
    capabilities = read_script("Frontend/src-tauri/capabilities/default.json")
    app = read_script("Frontend/client/src/App.tsx")
    main = read_script("Frontend/client/src/main.tsx")

    assert "from src.native_overlay import" in web_api
    assert "from src.overlay import" not in web_api
    assert "src.overlay" not in native_overlay_py
    assert "recording-overlay" in native_overlay_rs
    assert "scriber-overlay-state" in native_overlay_rs
    assert "create_overlay_window(app)" in lib_rs
    assert "native overlay hidden window precreated" in lib_rs
    assert '"windowCreated"' in native_overlay_rs
    assert "overlayPrepare" in shell_ipc
    assert "overlayShow" in shell_ipc
    assert "overlayHide" in shell_ipc
    assert "nativeOverlay" in shell_ipc
    assert '"recording-overlay"' in capabilities
    assert '"core:event:allow-listen"' in capabilities
    assert "NativeRecordingOverlay" not in app
    assert 'import("./components/NativeRecordingOverlay")' in main
    assert 'import("./App")' in main


def test_transcript_detail_keeps_native_window_titlebar() -> None:
    transcript_detail = read_script("Frontend/client/src/pages/TranscriptDetail.tsx")

    assert 'import { DesktopTitleBar } from "@/components/DesktopTitleBar";' in transcript_detail
    assert "<DesktopTitleBar />" in transcript_detail
    assert "h-screen bg-background flex flex-col overflow-hidden" in transcript_detail
    assert "min-h-0 flex-1 overflow-y-auto" in transcript_detail


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
    assert '[string]$GlobalHotkeySmokeDefaultStt = ""' in desktop
    assert "[switch]$GlobalHotkeySkipStopCleanup" in desktop
    assert "[int]$GlobalHotkeyPreDispatchSettleSec = 0" in desktop
    assert "Start-Sleep -Seconds $GlobalHotkeyPreDispatchSettleSec" in desktop
    assert "function Test-GlobalHotkeyRegistration" in desktop
    assert "function Invoke-GlobalHotkeyChord" in desktop
    assert "function Test-SyntheticGlobalHotkeyDispatchSupport" in desktop
    assert "ctrl+alt+shift+f24" in desktop
    assert "Windows SendInput did not trigger a probe RegisterHotKey" in desktop
    assert "function Test-GlobalHotkeyDispatch" in desktop
    assert '[ValidateSet("synthetic", "manual")]' in desktop
    assert 'Write-Warning "Manual global hotkey smoke: press' in desktop
    assert 'dispatchMethod = $DispatchMethod' in desktop
    assert "function Wait-GlobalHotkeyUserReady" in desktop
    assert "api/metrics/hot-path?limit=10&includeActive=1" in desktop
    assert "hotkey_received_to_mic_ready_ms" in desktop
    assert "hotkey_received_to_first_audio_frame_ms" in desktop
    assert "userReady = $userReady" in desktop
    assert "hotPathMetrics = if ($userReady)" in desktop
    assert "Global hotkey registered: $Hotkey (toggle)" in desktop
    assert "SCRIBER_DEFAULT_STT=$effectiveDefaultStt" in desktop
    assert "Assert-UnderRoot -Root (Join-Path $Root \"tmp\") -Path $RuntimeDataDir -Label \"Global hotkey smoke DataDir\"" in desktop
    assert "$env:SCRIBER_HOTKEY = $globalHotkeySmokeConfig.hotkey" in desktop
    assert "$env:SCRIBER_DEFAULT_STT = $globalHotkeySmokeConfig.defaultStt" in desktop
    assert "stopSkipped = $stopSkipped" in desktop
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
    assert installer.count('"-GlobalHotkeySmokeHotkey", $GlobalHotkeySmokeHotkey') == 1
    assert installer.count('"-GlobalHotkeyDispatchTimeoutSec", $GlobalHotkeyDispatchTimeoutSec.ToString()') == 1
    assert "if ($VerifyGlobalHotkeyRegistration -or $SimulateGlobalHotkey -or $WaitForManualGlobalHotkey)" in installer
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
    assert "$meetingPrivacyFindings" in desktop
    assert "Support bundle contains Meeting-sensitive artifact categories" in desktop
    assert "sensitiveFindingCount = 0" in desktop
    assert "transcriptStoresAbsent = $true" in desktop
    assert "voiceprintArtifactsAbsent = $true" in desktop
    assert "$configSnapshots" in desktop
    assert "[System.IO.File]::WriteAllBytes($snapshot.Path" in desktop
    assert "supportBundle = $supportBundle" in desktop
    assert "[switch]$VerifyFrontend" in desktop
    assert "function Test-FrontendHttp" in desktop
    assert "function Wait-FrontendReady" in desktop
    assert "Backend static fallback unexpectedly served frontend assets" in desktop
    assert 'source = "tauri-webview"' in desktop
    assert "backendStaticFallbackAvailable = $false" in desktop
    assert "http://tauri.localhost" in desktop
    assert "/api/runtime/frontend-ready" in desktop
    assert "Tauri WebView did not report frontend-ready" in desktop
    assert "tauriOriginCors = $true" in desktop
    assert "runtimeCorsVerified" in desktop
    assert "webViewReady = [bool]$frontendReady.ready" in desktop
    assert "webViewBackendBaseUrl" in desktop
    assert "webViewLocationOrigin" in desktop
    assert "Tauri WebView frontend-ready request used Origin" in desktop
    assert "webViewRequestOrigin" in desktop
    assert "Access-Control-Request-Private-Network" in desktop
    assert "privateNetworkPreflight = $true" in desktop
    assert "frontend = $frontend" in desktop

    assert "[switch]$VerifySupportBundle" in installer
    assert '"-VerifySupportBundle"' in installer
    assert "supportBundle = $smoke.supportBundle" in installer
    assert "[switch]$VerifyFrontend" in installer
    assert "actual Tauri WebView frontend-ready" in installer
    assert "function Test-InstalledFrontendAssetOwnership" in installer
    assert "backend\\Frontend\\dist\\public" in installer
    assert "resources\\backend\\Frontend\\dist\\public" in installer
    assert "Installed Python backend sidecar still contains frontend asset tree" in installer
    assert "frontendAssetOwnership = $frontendAssetOwnership" in installer
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


def test_installed_smoke_can_drive_real_synthetic_meeting_audio_pipes() -> None:
    desktop = read_script("scripts/smoke_tauri_desktop.ps1")
    installer = read_script("scripts/smoke_windows_installer.ps1")
    build = read_script("scripts/build_windows.ps1")

    assert "[switch]$VerifyMeetingAudioDeviceTest" in desktop
    assert "function Test-MeetingAudioDeviceTest" in desktop
    assert "/api/meetings/device-test" in desktop
    assert '$env:SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE = "1"' in desktop
    assert '$env:SCRIBER_RUST_AUDIO_SYNTHETIC_SIGNAL = "1"' in desktop
    assert 'foreach ($source in @("microphone", "system", "mic_clean"))' in desktop
    assert "Synthetic Meeting audio source '$source' delivered no active frames." in desktop
    assert 'transport = "tauri-shell-ipc-to-rust-sidecar-frame-pipes"' in desktop
    assert "audioPersisted = [bool]$response.audioPersisted" in desktop
    assert "audioSentToProvider = [bool]$response.audioSentToProvider" in desktop
    assert "meetingAudioDeviceTest = $meetingAudioDeviceTest" in desktop

    assert "[switch]$VerifyMeetingAudioDeviceTest" in installer
    assert '"-VerifyMeetingAudioDeviceTest"' in installer
    assert "meetingAudioDeviceTest = $smoke.meetingAudioDeviceTest" in installer

    assert "[switch]$RunInstallerMeetingAudioDeviceTestSmoke" in build
    assert "$RunInstallerMeetingAudioDeviceTestSmoke" in build
    assert '"-VerifyMeetingAudioDeviceTest"' in build
