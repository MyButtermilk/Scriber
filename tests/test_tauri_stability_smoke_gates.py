from __future__ import annotations

import json
import os
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


def test_hotkey_refresh_is_serialized_and_partial_registration_is_settled() -> None:
    lib = read_script("Frontend/src-tauri/src/lib.rs")
    wrapper = lib.split("fn refresh_global_hotkey_for_app<R: Runtime>", 1)[1].split(
        "fn refresh_global_hotkey_for_app_locked", 1
    )[0]
    locked = lib.split("fn refresh_global_hotkey_for_app_locked", 1)[1].split(
        "fn handle_global_shortcut_event", 1
    )[0]

    assert "registration: Mutex<()>" in lib
    assert "lock_unpoisoned(&hotkey_state.registration)" in wrapper
    assert "refresh_global_hotkey_for_app_locked(app, &hotkey_state)" in wrapper
    assert "Optional shortcut\n    // conflicts are a stable degraded state" in locked
    assert locked.rstrip().endswith("Ok(hotkey_state.status())\n}")


def test_backend_relock_and_frontend_navigation_preserve_runtime_identity() -> None:
    lib = read_script("Frontend/src-tauri/src/lib.rs")
    app = read_script("Frontend/client/src/App.tsx")

    assert "backend_snapshot_identity_matches(" in lib
    assert "state.child.as_ref().map(Child::id)" in lib
    assert "struct PendingNavigationState" in lib
    assert "fn navigation_listener_ready(" in lib
    assert "fn acknowledge_navigation(" in lib
    assert "PendingNavigationState::default()" in lib
    assert 'listen<TauriNavigationRequest>("scriber-navigate"' in app
    assert 'invoke<TauriNavigationRequest | null>("navigation_listener_ready")' in app
    assert 'invoke<boolean>("acknowledge_navigation", { navigationId })' in app
    assert "navigationId <= lastNavigationIdRef.current" in app


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
    rust = read_script("Frontend/src-tauri/src/lib.rs")

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
    assert "SCRIBER_TAURI_SMOKE_QUIT_BARRIER_FILE" in script
    assert "Request-FrontendPerformanceFlush" in script
    assert "heartbeatAcknowledged" in script
    assert "SHELL_MENU_SMOKE_QUIT_BARRIER_FILE_ENV" in rust
    assert "shell menu smoke frontend performance barrier observed" in rust
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
    assert "function Get-BackendRuntimeInputManifest" in sidecar
    assert "function Get-BackendApplicationInputManifest" in sidecar
    assert "function Test-BackendRuntimeCache" in sidecar
    assert "function Get-BackendRuntimeFileIdentityEntries" in sidecar
    assert "function Invoke-StageBackendApplicationLayer" in sidecar
    assert "[string]$RuntimeCacheRoot" in sidecar
    assert "backend-runtime-cache-check" in sidecar
    assert "backend-runtime-stage" in sidecar
    assert "backend-application-stage" in sidecar
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
    assert "[switch]$ParallelizeIndependentBuilds" in sidecar
    assert "[switch]$RustAudioOnly" in sidecar
    assert "Starting Rust audio sidecar preparation in parallel with the Python backend." in sidecar
    assert sidecar.count("-UseIsolatedTarget ([bool]$RustAudioIsolatedTarget)") >= 2
    assert "Parallel Rust audio sidecar build did not write its result" in sidecar
    assert "durationMs = [int64]$rustAudioWatch.ElapsedMilliseconds" in sidecar
    assert 'Get-ObjectPropertyValue -Object $rustAudioParallelPayload -Name "durationMs"' in sidecar
    assert "overlappedWallDurationMs = $rustAudioParallelJoinDurationMs" in sidecar
    assert "Stale audio sidecar resource" in sidecar
    assert "Stale packaged audio sidecar resource" in sidecar
    assert "function Get-Sha256Hex" in sidecar
    assert "sha256 = Get-Sha256Hex -Path $item.FullName" in sidecar
    assert "lastWriteTimeUtc" not in sidecar


def test_prebuilt_tauri_audio_miss_overlaps_shared_target_only_with_backend() -> None:
    workflow = read_script(".github/workflows/release-windows.yml")
    sidecar = read_script("scripts/build_tauri_backend_sidecar.ps1")
    installer = read_script("scripts/build_windows.ps1")

    assert (
        "SCRIBER_PARALLELIZE_SHARED_RUST_AUDIO: "
        "${{ steps.frontend-preparation.outputs.use-prebuilt == 'true' && '1' || '0' }}"
    ) in workflow
    assert '$parallelizeSharedRustAudio = (' in sidecar
    assert '[string]$env:SCRIBER_PARALLELIZE_SHARED_RUST_AUDIO -eq "1"' in sidecar
    assert "-not $ParallelizeIndependentBuilds" in sidecar
    assert "-not $RustAudioIsolatedTarget" in sidecar
    assert (
        "if (($ParallelizeIndependentBuilds -or $parallelizeSharedRustAudio) -and "
        "$BundleRustAudioSidecar)" in sidecar
    )
    assert 'if (-not $parallelizeSharedRustAudio) {' in sidecar
    assert '$rustAudioParallelArgs += "-RustAudioIsolatedTarget"' in sidecar
    assert (
        "-UseIsolatedTarget ([bool]$RustAudioIsolatedTarget)" in sidecar
    )
    audio_start_info = sidecar[
        sidecar.index(
            "$rustAudioParallelStartInfo = [System.Diagnostics.ProcessStartInfo]::new()"
        ) : sidecar.index(
            "$rustAudioParallelProcess = [System.Diagnostics.Process]::new()"
        )
    ]
    child_tauri_overlay = (
        '$rustAudioParallelStartInfo.EnvironmentVariables["TAURI_CONFIG"] = '
        "'{\"bundle\":{\"resources\":[]}}'"
    )
    child_tauri_overlay_block = (
        "if ($parallelizeSharedRustAudio) {\n"
        f"        {child_tauri_overlay}\n"
        "    }"
    )
    assert audio_start_info.count(child_tauri_overlay_block) == 1
    assert audio_start_info.index("UseShellExecute = $false") < audio_start_info.index(
        child_tauri_overlay
    )
    assert sidecar.count(child_tauri_overlay) == 1
    assert '$env:TAURI_CONFIG =' not in sidecar
    assert sidecar.index("$parallelizeSharedRustAudio = (") < sidecar.index(
        "Starting Rust audio sidecar preparation in parallel with the Python backend."
    ) < sidecar.index('Invoke-TimedStep -Label "pyinstaller-build"')
    assert '$sidecarArgs += "-ParallelizeIndependentBuilds"' not in installer


def test_diarization_cold_build_is_prestaged_in_parallel_without_sharing_backend_outputs() -> None:
    sidecar = read_script("scripts/build_tauri_backend_sidecar.ps1")
    installer = read_script("scripts/build_windows.ps1")

    assert "[switch]$ParallelizeRustDiarizationBuild" in sidecar
    assert "[switch]$RustDiarizationPrestageOnly" in sidecar
    assert "[string]$RustDiarizationPrestageBackendDir" in sidecar
    assert (
        "if (($ParallelizeIndependentBuilds -or $ParallelizeRustDiarizationBuild) -and "
        "$BundleRustDiarizationSidecar)" in sidecar
    )
    assert 'Join-Path $RepoRoot "build\\rust-diarization-parallel\\parent-$PID"' in sidecar
    assert 'Join-Path $rustDiarizationParallelRoot "backend"' in sidecar
    assert "RustDiarizationResultPath must stay outside RustDiarizationPrestageBackendDir" in sidecar
    assert "Starting Rust diarization sidecar prestage in parallel with the Python backend." in sidecar
    assert "ReadToEndAsync()" in sidecar
    assert "WaitForExit($rustDiarizationRemainingMs)" in sidecar
    assert "rust-diarization-sidecar-prestage" in sidecar
    assert "Stop-ChildProcessTree -Process $rustDiarizationParallelProcess" in sidecar
    assert 'Join-Path $env:SystemRoot "System32\\taskkill.exe"' in sidecar
    assert "Final Rust diarization staging did not reuse the cache prepared by the parallel worker." in sidecar
    assert sidecar.count("Copy-RustDiarizationSidecarToBackend") >= 3
    assert sidecar.index(
        "Starting Rust diarization sidecar prestage in parallel with the Python backend."
    ) < sidecar.index('Invoke-TimedStep -Label "pyinstaller-build"')

    backend_staging_index = sidecar.index(
        'Invoke-TimedStep -Label "copy-to-tauri-release"'
    )
    shared_audio_index = sidecar.index(
        "$script:RustAudioSidecarCopied = Copy-RustAudioSidecarToTauriRelease "
        "-Root $RepoRoot -UseIsolatedTarget ([bool]$RustAudioIsolatedTarget)"
    )
    diarization_join_index = sidecar.index(
        "if ($rustDiarizationParallelProcess) {\n"
        "    $rustDiarizationParallelOk = $false"
    )
    final_diarization_staging_index = sidecar.index(
        "if ($BundleRustDiarizationSidecar) {\n"
        '    Invoke-TimedStep -Label "rust-diarization-sidecar-build"'
    )
    assert (
        backend_staging_index
        < shared_audio_index
        < diarization_join_index
        < final_diarization_staging_index
    )

    assert '$sidecarArgs += "-ParallelizeRustDiarizationBuild"' in installer
    assert '$sidecarArgs += "-ParallelizeIndependentBuilds"' not in installer


def test_sidecar_cache_key_excludes_frontend_dist() -> None:
    sidecar = read_script("scripts/build_tauri_backend_sidecar.ps1")
    spec = read_script("packaging/scriber-backend.spec")

    manifest_start = sidecar.index("function Get-BackendRuntimeInputManifest")
    manifest_end = sidecar.index("function Copy-DirectoryContents")
    manifest_block = sidecar[manifest_start:manifest_end]

    assert '"src"' in manifest_block
    assert '"Frontend\\dist\\public"' not in manifest_block
    assert '"packaging\\scriber-backend.spec"' in manifest_block
    assert '"scripts/check_backend_runtime_imports.py"' in manifest_block
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
    assert "[switch]$ParallelizeIndependentBuilds" in build
    assert "function Start-TrackedReleaseProcess" in build
    assert "function Complete-TrackedReleaseProcesses" in build
    assert "$null = $process.Handle" in build
    assert 'Join-Path $RepoRoot "scripts\\ci\\prepare_tauri_app.ps1"' in build
    assert '-Label "Tauri app binary build"' in build
    assert "-Arguments $tauriAppBuildArgs" in build
    assert build.index('-Label "Tauri app binary build"') < build.index(
        "Complete-TrackedReleaseProcesses -Tasks $parallelTasks"
    )
    assert "$bundleExistingTauriApp = $UsePrebuiltTauriApp -or $tauriAppBuiltBeforeBundle" in build
    assert 'lastProgressStatus = ""' in build
    assert ".TotalSeconds -ge 60" in build
    assert "if (-not $task.Process -or $task.Disposed)" in build
    assert "if ($RustAudioIsolatedTarget)" in build
    assert "$RustAudioIsolatedTarget -or $ParallelizeIndependentBuilds" not in build
    assert '$sidecarArgs += "-ParallelizeIndependentBuilds"' not in build
    assert "Cargo's target lock safely bounds the rare overlap" in build
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
    assert "$runFrontendTypeCheck = -not $SkipChecks -and -not $SkipFrontendTypeCheck" in build


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
    cache_key_finalizer = read_script("scripts/ci/finalize_release_cache_keys.ps1")

    assert "branches:\n      - main" not in workflow
    assert 'tags:\n      - "v*"' in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.event.inputs.refresh_release_cache_artifacts == 'true'" in workflow
    assert 'CARGO_INCREMENTAL: "1"' in workflow
    assert "CARGO_LOG: ${{ vars.SCRIBER_CARGO_LOG }}" in workflow
    assert "cache: pip" not in workflow
    assert "cache: npm" not in workflow
    assert "Resolve package-store paths" in workflow
    assert "Restore npm package store" in workflow
    assert "Restore pip package store" in workflow
    assert "steps.frontend-node-modules-cache.outputs.cache-hit != 'true'" in workflow
    assert "scriber-npm-package-store-v1-" in workflow
    assert "scriber-python-pip-store-v1-" in workflow
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
    assert "Validate tag release signing preflight" in workflow
    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "scripts\\ci\\validate_tag_release_preflight.ps1" in workflow
    assert workflow.index("Validate Outlook release configuration") < workflow.index(
        "Validate tag release signing preflight"
    ) < workflow.index("Compute release cache keys")
    assert '[Guid]::TryParseExact($clientId, "D", [ref]$parsed)' in workflow
    assert "Official tag releases require a valid SCRIBER_OUTLOOK_CLIENT_ID" in workflow
    assert 'Add-DynamicRow -Path $tauriAppBinaryPath -Name "outlook-client-id-present"' in cache_key_finalizer
    assert 'Add-DynamicRow -Path $tauriAppBinaryPath -Name "outlook-client-id-sha256"' in cache_key_finalizer
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
    assert "Restore independent release-cache fallbacks in parallel" in workflow
    assert "Publish bounded finished component caches in parallel" in workflow
    assert "Restore Rust audio sidecar cache" in workflow
    assert "Frontend/src-tauri/target/release/incremental" in workflow
    assert "scriber-rust-release-v2-${{ runner.os }}" not in workflow
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
    assert "restore_component_cache_artifacts_parallel.ps1" in workflow
    assert "publish_finished_component_caches_parallel.ps1" in workflow
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
    assert "SCRIBER_PUBLISH_FINISHED_COMPONENT_CACHE_ARTIFACTS" in workflow
    assert 'startsWith(github.ref, \'refs/tags/\')' in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.event.inputs.refresh_release_cache_artifacts == 'true'" in workflow
    assert "actions: write" in workflow
    assert '"-ParallelizeIndependentBuilds"' in workflow
    assert "SCRIBER_PUBLISH_RUST_AUDIO_FINISHED_CACHE" in workflow
    assert "SCRIBER_PUBLISH_BACKEND_FINISHED_CACHE" in workflow
    assert "env.SCRIBER_PUBLISH_RELEASE_CACHE_ARTIFACTS == 'true'" in workflow
    assert "steps.backend-sidecar-cache-selection.outputs.selected == 'true'" in workflow
    assert "if: env.SCRIBER_PUBLISH_RELEASE_CACHE_ARTIFACTS == 'true' && steps.rust-build-artifact.outputs.exact != 'true'" in workflow
    assert "Select current backend sidecar cache entry" in workflow
    assert "scripts\\ci\\select_backend_sidecar_cache_entry.ps1" in workflow
    assert "Restore exact Tauri app binary" in workflow
    assert "scripts\\ci\\sync_tauri_app_binary_cache.ps1" in workflow


def test_release_workflow_parallelizes_only_disjoint_finished_cache_fallbacks() -> None:
    workflow = read_script(".github/workflows/release-windows.yml")
    helper = read_script("scripts/ci/restore_component_cache_artifacts_parallel.ps1")
    generic_restorer = read_script("scripts/ci/restore_release_cache_artifact.ps1")
    ffmpeg_restorer = read_script("scripts/ffmpeg/restore_profile_b_release_artifact.ps1")

    restore_index = workflow.index("Restore independent release-cache fallbacks in parallel")
    assert workflow.index("Restore Rust audio sidecar cache") < restore_index
    assert workflow.index("Restore Rust diarization sidecar cache") < restore_index
    assert workflow.index("Restore FFmpeg Profile B cache") < restore_index
    assert restore_index < workflow.index("Report release cache hits")
    assert workflow.count("Restore independent release-cache fallbacks in parallel") == 1
    assert "Restore Rust audio sidecar release artifact" not in workflow
    assert "Restore Rust diarization sidecar release artifact" not in workflow
    assert "Restore FFmpeg Profile B release artifact" not in workflow
    restore_block = workflow[
        restore_index : workflow.index("Restore pip package store", restore_index)
    ]
    assert "$restoreArgs = @{" in restore_block
    assert "$restoreArgs.RestoreRustAudio = $true" in restore_block
    assert "$restoreArgs.RestoreRustDiarization = $true" in restore_block
    assert "$restoreArgs.RestoreFfmpeg = $true" in restore_block
    assert "@restoreArgs" in restore_block
    assert "$restoreArgs = @(" not in restore_block
    assert '$restoreArgs += "-Restore' not in restore_block

    assert "Start-Job" in helper
    assert "Wait-Job -Job $restoreJobs" in helper
    assert "Stop-Job -Job" not in helper
    assert "& $ScriptPath" in helper
    assert "powershell.exe -NoProfile -File" not in helper
    assert '$env:GITHUB_OUTPUT = $ChildOutputPath' in helper
    assert '"{0}.github-output.txt" -f $component.Slug' in helper
    assert 'DestinationPath = "build\\rust-audio-sidecar-cache"' in helper
    assert 'DestinationPath = "build\\rust-diarization-sidecar-cache"' in helper
    assert 'DestinationPath = "build\\ffmpeg-profile-b-msys2"' in helper
    assert "RestorePython" not in helper
    assert "python-wheelhouse" not in helper
    assert "python-venv" not in helper
    assert 'mode = "parallel-release-cache-fallback-restore"' in helper
    assert "return" in generic_restorer
    assert "return" in ffmpeg_restorer
    assert "exit 0" not in generic_restorer
    assert "exit 0" not in ffmpeg_restorer


def test_finished_component_cache_publication_is_parallel_and_post_release() -> None:
    workflow = read_script(".github/workflows/release-windows.yml")
    helper = read_script("scripts/ci/publish_finished_component_caches_parallel.ps1")

    collect_index = workflow.index("Collect release artifacts")
    release_index = workflow.index("Publish GitHub release")
    verify_index = workflow.index("Verify published updater metadata")
    evidence_index = workflow.index("Upload publication evidence")
    caches_index = workflow.index("Publish bounded finished component caches in parallel")
    assert collect_index < release_index < verify_index < evidence_index < caches_index
    assert workflow.count("Publish bounded finished component caches in parallel") == 1
    assert "continue-on-error: true" in workflow[caches_index:]
    assert "SCRIBER_PUBLISH_FFMPEG_FINISHED_CACHE" in workflow[caches_index:]
    assert "SCRIBER_PUBLISH_RUST_AUDIO_FINISHED_CACHE" in workflow[caches_index:]
    assert "SCRIBER_PUBLISH_RUST_DIARIZATION_FINISHED_CACHE" in workflow[caches_index:]
    assert "SCRIBER_PUBLISH_BACKEND_FINISHED_CACHE" in workflow[caches_index:]
    cache_publish_block = workflow[caches_index:]
    assert cache_publish_block.count("env.SCRIBER_PUBLISH_RELEASE_CACHE_ARTIFACTS == 'true'") >= 5
    assert "steps.backend-sidecar-cache-selection.outputs.selected == 'true'" in cache_publish_block
    assert "Publish FFmpeg Profile B release artifact" not in workflow
    assert "Publish Rust audio sidecar release artifact" not in workflow
    assert "Publish Rust diarization sidecar release artifact" not in workflow
    assert "Publish backend sidecar release artifact" not in workflow
    assert "$publishArguments = @{" in cache_publish_block
    assert "$publishArguments.PublishFfmpeg = $true" in cache_publish_block
    assert "$publishArguments.PublishRustAudio = $true" in cache_publish_block
    assert "$publishArguments.PublishRustDiarization = $true" in cache_publish_block
    assert "$publishArguments.PublishBackend = $true" in cache_publish_block
    assert "@publishArguments" in cache_publish_block
    assert "$publishArguments = @(" not in cache_publish_block
    assert '$publishArguments += "-Publish' not in cache_publish_block

    assert "Start-Job" in helper
    assert "Wait-Job -Job $publisherJobs -Timeout $PublicationTimeoutSeconds" in helper
    assert "[int]$PublicationTimeoutSeconds = 900" in helper
    assert "-Timeout $PublicationTimeoutSeconds" in helper
    assert "Stop-Job -Job $timedOutJobs" in helper
    assert '$env:GITHUB_OUTPUT = $ChildOutputPath' in helper
    assert '"{0}.github-output.txt" -f $component.Slug' in helper
    assert "mode = \"parallel-best-effort\"" in helper
    assert "The verified app release remains valid" in helper
    assert "::warning title=Scriber cache publication::" in helper
    assert "publish_profile_b_release_artifact.ps1" in helper
    assert "publish_release_cache_artifact.ps1" in helper
    assert "powershell.exe -NoProfile -File" in helper
    assert ("Invoke-" + "Expression") not in helper
    assert ("-Encoded" + "Command") not in helper


def test_release_cache_publisher_rechecks_idempotent_release_creation() -> None:
    publisher = read_script("scripts/ci/publish_release_cache_artifact.ps1")
    ffmpeg_publisher = read_script("scripts/ffmpeg/publish_profile_b_release_artifact.ps1")

    assert "$releaseCreateExitCode -ne 0" in publisher
    assert "$releaseRecheckExitCode" in publisher
    assert 'release", "view", $Tag, "--repo", $Repo' in publisher
    assert "continuing with the cache upload" in publisher
    assert "Refusing cache publication for non-cache release tag" in publisher
    assert "Refusing FFmpeg cache publication for non-cache release tag" in ffmpeg_publisher
    assert "^release-cache-" in publisher
    assert "^ffmpeg-profile-b-" in ffmpeg_publisher


def test_release_automation_avoids_dynamic_powershell_payloads() -> None:
    candidates = [
        REPO_ROOT / ".github" / "workflows" / "release-windows.yml",
        REPO_ROOT / "scripts" / "build_windows.ps1",
        REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1",
        REPO_ROOT / "tests" / "test_backend_runtime_import_check.py",
    ]
    candidates.extend(sorted((REPO_ROOT / "scripts" / "ci").glob("*.ps1")))
    forbidden_literals = (
        "Invoke-" + "Expression",
        "-Encoded" + "Command",
        "FromBase64" + "String",
    )

    for path in candidates:
        source = path.read_text(encoding="utf-8")
        for literal in forbidden_literals:
            assert literal not in source, f"{path.relative_to(REPO_ROOT)} contains {literal}"
        assert not re.search(
            r"\b(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)\b[^\r\n]{0,240}-Command\b",
            source,
            flags=re.IGNORECASE,
        ), f"{path.relative_to(REPO_ROOT)} contains an inline PowerShell command"


def test_parallel_tauri_prepare_helper_keeps_compile_and_bundle_contracts_separate() -> None:
    helper = read_script("scripts/ci/prepare_tauri_app.ps1")

    assert '[ValidateSet("TypeCheck", "BuildBinary")]' in helper
    assert "npm run check" in helper
    assert 'npm run tauri:build -- --no-bundle --config "{0}" --ci 2>&1' in helper
    assert "npm run tauri:bundle" not in helper
    assert "Assert-UnderRoot" in helper
    assert "function New-CompileOnlyTauriConfig" in helper
    assert '$source.bundle.PSObject.Properties["resources"]' in helper
    assert "Add-Member" in helper
    assert "$source.bundle.resources = @()" in helper
    assert '"tauri.compile-only.conf.json"' in helper
    assert "$compileConfigPath.Replace" in helper
    assert '$writer.WriteLine(("{0}`t{1}"' in helper


def test_parallel_tauri_compile_config_strips_only_bundle_resources(tmp_path: Path) -> None:
    for bundle_mode in ("with-resources", "without-resources", "without-bundle"):
        case_root = tmp_path / bundle_mode
        frontend = case_root / "Frontend"
        frontend.mkdir(parents=True)
        config_path = case_root / "build" / "tauri.generated.conf.json"
        config_path.parent.mkdir()
        source = {
            "identifier": "com.example.scriber",
            "plugins": {
                "updater": {
                    "endpoints": ["https://example.invalid/latest.json"],
                    "pubkey": "public-test-key",
                }
            },
        }
        if bundle_mode != "without-bundle":
            source["bundle"] = {"active": True}
        if bundle_mode == "with-resources":
            source["bundle"]["resources"] = {
                "target/release/backend/": "backend/",
                "../../THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
            }
        config_path.write_text(json.dumps(source), encoding="utf-8")
        fake_bin = case_root / "fake-bin"
        fake_bin.mkdir()
        (fake_bin / "npm.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
        log_path = case_root / "build" / "tauri.log"
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

        powershell = shutil.which("powershell") or shutil.which("pwsh")
        assert powershell, "PowerShell is required for the Tauri compile-config regression test"
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(REPO_ROOT / "scripts" / "ci" / "prepare_tauri_app.ps1"),
                "-Mode",
                "BuildBinary",
                "-RepoRoot",
                str(case_root),
                "-ConfigPath",
                str(config_path),
                "-TauriLogPath",
                str(log_path),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr

        compile_config = json.loads(
            (config_path.parent / "tauri.compile-only.conf.json").read_text(encoding="utf-8")
        )
        assert compile_config["bundle"]["resources"] == []
        if bundle_mode == "without-bundle":
            assert compile_config["bundle"] == {"resources": []}
        else:
            assert compile_config["bundle"]["active"] is True
        assert compile_config["plugins"] == source["plugins"]
        assert json.loads(config_path.read_text(encoding="utf-8")) == source


def test_release_cache_key_script_normalizes_version_only_churn() -> None:
    script = read_script("scripts/ci/write_release_cache_keys.ps1")
    contract = json.loads(read_script("packaging/backend-sidecar-output-contract.json"))
    tauri_contract = json.loads(read_script("packaging/tauri-app-binary-output-contract.json"))

    assert contract == {
        "schemaVersion": 1,
        "name": "scriber-backend-onedir",
        "revision": 3,
    }
    assert tauri_contract == {
        "schemaVersion": 1,
        "name": "scriber-tauri-app-binary",
        "revision": 3,
    }

    assert "frontend-dependencies.txt" in script
    assert "rust-dependencies.txt" in script
    assert "rust-release.txt" in script
    assert "tauri-app-binary.txt" in script
    assert "rust-audio-sidecar.txt" in script
    assert "rust-diarization-sidecar.txt" in script
    assert "sherpa-onnx-archive.txt" in script
    assert "backend-runtime.txt" in script
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
    assert 'Add-FileGlobEntries -Entries $backendRuntimeEntries -Root "backend_runtime" -Filter "*.py"' in script
    assert 'Add-FileGlobEntries -Entries $backendRuntimeEntries -Root "pyloudnorm" -Filter "*.py"' in script
    assert 'constant`tffmpeg-profile`tffmpeg-profile-b-n7.0-v4' in script
    assert "Get-BackendSidecarOutputContract" in script
    assert 'packaging/backend-sidecar-output-contract.json' in script
    assert 'contract`trevision`t$($backendContract.revision)' in script
    assert "Get-TauriAppBinaryOutputContract" in script
    assert 'packaging/tauri-app-binary-output-contract.json' in script
    assert 'contract`trevision`t$($tauriAppContract.revision)' in script
    assert 'flag`tbundleMediaTools`ttrue' in script
    assert 'flag`tuseProfileBFfmpeg`ttrue' in script
    assert 'flag`tuseGyanFfmpegEssentials`tfalse' in script
    assert 'flag`tskipBundledFfprobe`tfalse' in script
    assert 'flag`tvalidateSlimMediaTools`ttrue' in script
    assert 'flag`tpyInstallerClean`ttrue' in script
    assert script.count('constant`ttoolchain`trust-1.97.0') == 3
    assert "Add-GitTrackedEntries" in script
    assert "__pycache__" in script
    assert '@(".pyc", ".pyo")' in script
    backend_block = script.split("$backendEntries = New-EntryList", 1)[1]
    assert '"scripts/build_tauri_backend_sidecar.ps1"' not in backend_block
    assert "Frontend/src-tauri/Cargo.toml" not in backend_block
    assert "Frontend/src-tauri/src/audio_sidecar.rs" not in backend_block


def test_release_cache_gc_keeps_exactly_one_current_generation() -> None:
    workflow = read_script(".github/workflows/release-windows.yml")
    gc = read_script("scripts/ci/prune_obsolete_release_caches.ps1")

    assert "[int]$RetainPerRollingFamily = 1" in gc
    assert "[int]$ListLimit = 10000" in gc
    assert "[string[]]$ProtectedRef = @()" in gc
    assert "[string[]]$PrunableRef = @()" in gc
    assert "[switch]$PruneCurrentRef" in gc
    assert "gh cache delete" in gc
    assert "gh release delete" in gc
    assert "gh release delete-asset" in gc
    assert "--all" not in gc
    assert "refs/heads/main" in gc
    assert "[string]$ExpectedRustDependencyKey" in gc
    assert "[switch]$VerifyCurrentGeneration" in gc
    assert "expected exactly one main Rust dependency cache" in gc
    assert "Release cache generation verification passed" in gc
    assert gc.index("if ($VerifyCurrentGeneration -and $Apply)") < gc.index(
        "if (-not (Get-Command gh"
    )
    assert "release-cache-backend-sidecar-v2" in gc
    assert "release-cache-rust-build-v2" in gc
    assert "ffmpeg-profile-b-n7.0-v4" in gc
    assert "^scriber-tauri-app-binary-v[123]-Windows-" in gc
    obsolete_block = gc.split("$obsoletePatterns = @(", 1)[1].split(")", 1)[0]
    assert "^scriber-tauri-app-binary-v1-Windows-" not in obsolete_block
    assert "$null = $protectedRefs.Add('refs/heads/main')" in gc
    assert "$prunableRefs.Contains([string]$cache.ref)" in gc
    assert "explicitly-prunable-completed-ref-cache" in gc
    assert "foreign or merely unrecognized branches remain untouched by default" in gc
    assert 'if ($PruneCurrentRef)' in gc
    assert '$null = $currentRefs.Add("refs/heads/$branchName")' in gc
    assert '$null = $prunableRefs.Add($currentRef)' in gc
    assert '$null = $protectedRefs.Add($currentRef)' in gc
    assert "Cache ref cannot be both protected and prunable" in gc
    assert "GenerationPattern = '^scriber-tauri-app-binary-v(?<generation>[123])-Windows-'" in gc
    assert "$parsedCaches = $json | ConvertFrom-Json" in gc
    assert "$parsedReleases = $releaseJson | ConvertFrom-Json" in gc
    assert "Dictionary[int64, object]" in gc
    assert "Sort-Object { [int64]$_.Cache.id } -Unique" not in gc
    rolling_gc = gc.split("foreach ($family in $rollingFamilies)", 1)[1].split(
        "# Ref-scoped caches", 1
    )[0]
    assert rolling_gc.index("[int]$Matches.generation") < rolling_gc.index(
        "[DateTimeOffset]$_.createdAt"
    ) < rolling_gc.index("[DateTimeOffset]$_.lastAccessedAt")
    assert "Prune obsolete release caches" in workflow
    assert "Verify current release cache generation" in workflow
    verify_block = workflow.split("- name: Verify current release cache generation", 1)[1]
    assert "-ExpectedRustDependencyKey" in verify_block
    assert "scriber-rust-dependencies-v1-${{ runner.os }}-${{ hashFiles('build/cache-keys/rust-dependencies.txt') }}" in verify_block
    assert "-ExpectedTauriAppKey" in verify_block
    assert "scriber-tauri-app-binary-v3-${{ runner.os }}-${{ hashFiles('build/cache-keys/tauri-app-binary.txt') }}" in verify_block
    assert "-VerifyCurrentGeneration" in verify_block
    assert "continue-on-error: true" not in verify_block.split("\n      - name:", 1)[0]
    assert "actions: write" in workflow
    assert "env.SCRIBER_SAVE_ACTIONS_CACHES == 'true' && github.ref == 'refs/heads/main'" in workflow


def test_python_release_environment_is_exact_and_reproducible() -> None:
    workflow = read_script(".github/workflows/release-windows.yml")
    requirements = read_script("requirements-build.txt")

    venv_block = workflow.split("- name: Restore Python dependency cache", 1)[1].split(
        "- name: Restore Python venv release artifact", 1
    )[0]
    assert "restore-keys:" not in venv_block
    assert "pyinstaller==6.20.0" in requirements
    assert "pyinstaller-hooks-contrib==2026.5" in requirements
    assert "setuptools==80.10.1" in requirements
    assert ">=" not in requirements


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
        backend_before = before.pop("backend-sidecar.txt")
        backend_after = after.pop("backend-sidecar.txt")
        assert before == after
        assert tauri_before != tauri_after
        assert backend_before != backend_after
        assert before["backend-runtime.txt"] == after["backend-runtime.txt"]
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
    assert 'index.html?overlay=1&overlayMode=initializing' in native_overlay_rs
    assert '"rendererReady"' in native_overlay_rs
    assert "pub fn mark_renderer_ready()" in native_overlay_rs
    assert "create_overlay_window(app)" in lib_rs
    assert "fn native_overlay_renderer_ready()" in lib_rs
    assert "native_overlay::mark_renderer_ready()" in lib_rs
    assert "native_overlay_renderer_ready," in lib_rs
    assert "native overlay hidden window precreated" in lib_rs
    assert '"windowCreated"' in native_overlay_rs
    assert "overlayPrepare" in shell_ipc
    assert "overlayShow" in shell_ipc
    assert "overlayHide" in shell_ipc
    assert "nativeOverlay" in shell_ipc
    ui_timeout = re.search(
        r"OVERLAY_UI_COMMAND_TIMEOUT: Duration = Duration::from_secs\((\d+)\)",
        native_overlay_rs,
    )
    client_timeout = re.search(
        r"_OVERLAY_TRANSITION_TIMEOUT_SECONDS = ([0-9.]+)",
        native_overlay_py,
    )
    assert ui_timeout is not None
    assert client_timeout is not None
    assert float(client_timeout.group(1)) > float(ui_timeout.group(1))
    assert "handle_shell_command_on_ui_thread" in shell_ipc
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
    assert "Global hotkey registered: $Hotkey (toggle), post-processing: ctrl+shift+f, meeting: ctrl+shift+m" in desktop
    assert '"SCRIBER_POST_PROCESSING_HOTKEY=ctrl+shift+f"' in desktop
    assert '"SCRIBER_MEETING_HOTKEY=ctrl+shift+m"' in desktop
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
