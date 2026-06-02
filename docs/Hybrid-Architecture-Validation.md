# Hybrid Architecture Validation Log

This file records concrete validation evidence for `docs/Hybrid-Architecture-Goal.md`.
It is intentionally separate from the goal text so local goal edits can stay
unmixed with verification results.

## 2026-06-02 - Rebuilt NSIS Installer With Installed Hotkey Gate

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -SkipChecks `
  -SkipSmoke `
  -RunInstallerGlobalHotkeyRegistrationSmoke `
  -InstallerGlobalHotkeySmokeHotkey "ctrl+alt+shift+f12" `
  -InstallerGlobalHotkeyDispatchTimeoutSec 30
```

Result: passed for a freshly rebuilt NSIS installer with the Tauri-managed
sidecar and installed-app global-hotkey registration gate.

Evidence:

- Installer:
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Installer size: 206,872,336 bytes.
- Installer timestamp: 2026-06-02 03:52:22 +02:00.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Installed app:
  `tmp\installer-smoke\Scriber\scriber-desktop.exe`.
- Installed data dir:
  `tmp\installer-smoke\data-0f22647e8d114748862b317d7dd889a0`.
- Sidecar runtime import preflight: `ok: true`, `missing: []`.
- Frozen backend runtime import check: passed before bundling.
- Bundled media tools copied: `ffmpeg.exe`, `ffprobe.exe`.
- Release metadata validation: `ok: true`, platform `windows-x86_64`,
  signatures not required for this local unsigned build.
- Global hotkey verified: true.
- Global hotkey registered: `ctrl+alt+shift+f12`.
- Hotkey mode: `toggle`.
- Shell log:
  `tmp\installer-smoke\data-0f22647e8d114748862b317d7dd889a0\logs\tauri-shell.log`.
- Dispatch verified: false.
- Cleanup verified: true.
- Silent uninstall attempted: true.
- Silent uninstall verified: true.
- Install artifacts removed: true.
- Runtime data sentinel preserved: true.

Goal coverage:

- Phase 2: confirms the current rebuilt installer launches the packaged Python
  sidecar without source-checkout Python fallback.
- Phase 4: verifies installed Tauri global-hotkey registration after the
  backend-ready retry fix.
- Phase 6: confirms the regenerated NSIS package, release metadata, installed
  app smoke, cleanup, and uninstall path still pass.
- Phase 7/8: makes the installed hotkey registration check available as a
  repeatable build gate through `scripts\build_windows.ps1`.

Remaining limits:

- This proves installed hotkey registration, not physical OS-key dispatch.
- The stricter dispatch gate remains available through
  `-RunInstallerGlobalHotkeySmoke`, but this desktop session has not produced a
  valid synthetic-dispatch proof.
- This was a local unsigned build. Real release closure still requires
  Authenticode signing, Tauri updater signing keys, and a published signed
  updater manifest.

## 2026-06-02 - Tauri Global Hotkey Registration Retry + Runtime Gate

Commands:

```powershell
$scripts = @('scripts\smoke_tauri_desktop.ps1','scripts\smoke_windows_installer.ps1','scripts\build_windows.ps1')
foreach ($script in $scripts) {
  $tokens=$null; $errors=$null
  $null=[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $script), [ref]$tokens, [ref]$errors)
  if ($errors.Count) { $errors | ForEach-Object { Write-Error "${script}: $($_.Message)" }; exit 1 }
}
'OK'

venv\Scripts\python.exe -m pytest tests\test_tauri_stability_smoke_gates.py
cargo test --manifest-path Frontend\src-tauri\Cargo.toml hotkey_registration_retries_once_after_backend_ready -- --nocapture
cargo build --manifest-path Frontend\src-tauri\Cargo.toml --release

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1 `
  -ExePath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\scriber-desktop.exe" `
  -DataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\tauri-smoke-data\global-hotkey-registration-20260602" `
  -DisableDevFallback `
  -VerifyGlobalHotkeyRegistration `
  -GlobalHotkeySmokeHotkey "ctrl+alt+shift+f12" `
  -GlobalHotkeyDispatchTimeoutSec 30 `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\tauri-global-hotkey-registration-20260602.json"
```

Result: passed for global-hotkey registration in the Tauri-managed sidecar
runtime.

Implemented improvements:

- `Frontend\src-tauri\src\lib.rs` now retries global-hotkey registration once
  from the backend supervisor after the backend becomes ready. This closes the
  race where setup could attempt registration before `/api/settings` was
  reachable and then never retry.
- `scripts\smoke_tauri_desktop.ps1` now supports
  `-VerifyGlobalHotkeyRegistration` for a deterministic registration gate.
- The same script also has a stricter `-SimulateGlobalHotkey` dispatch gate for
  environments where synthetic or manual OS keyboard input reaches the global
  shortcut hook.
- `scripts\smoke_windows_installer.ps1` and `scripts\build_windows.ps1` can
  forward the registration and dispatch hotkey gates to installed-app smokes.

Evidence:

- Artifact:
  `tmp\hybrid-baseline\tauri-global-hotkey-registration-20260602.json`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Hotkey registered: `ctrl+alt+shift+f12`.
- Registration verified: true.
- Dispatch verified: false.
- Cleanup verified: true.
- Shell log:
  `tmp\tauri-smoke-data\global-hotkey-registration-20260602\logs\tauri-shell.log`.
- Shell log observed:
  `Global hotkey registered: ctrl+alt+shift+f12 (toggle)`.
- Updated release executable:
  `Frontend\src-tauri\target\release\scriber-desktop.exe`
  (12,456,960 bytes, timestamp 2026-06-02 03:28:01).
- PowerShell parser check passed for `scripts\smoke_tauri_desktop.ps1`,
  `scripts\smoke_windows_installer.ps1`, and `scripts\build_windows.ps1`.
- `tests\test_tauri_stability_smoke_gates.py`: `8 passed`.
- Targeted Rust test:
  `hotkey_registration_retries_once_after_backend_ready`: `1 passed`.

Goal coverage:

- Phase 4: hardens global-hotkey registration in the Tauri shell and prevents
  startup timing from leaving hotkeys silently unregistered.
- Phase 7: adds smoke-script and Rust regression coverage for this behavior.
- Phase 8: adds a runtime gate that verifies Tauri hotkey registration against
  the packaged sidecar runtime without Node/Python dev fallback.

Remaining limits:

- This proves registration, not a physical OS-key dispatch into
  `/api/live-mic/toggle`.
- The stricter synthetic dispatch gate was attempted with `keybd_event` and
  `SendInput`, but this desktop session did not deliver the synthetic key event
  to the Tauri global-shortcut hook. No dispatch proof is claimed from those
  failed attempts.
- A final manual Windows hotkey smoke still needs a physical keypress or an
  automation environment where global shortcut hooks receive synthetic input.

## 2026-06-02 - Installer Legacy Upgrade Stability + Smoke Output Artifacts

Commands:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -LegacyDataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -VerifyLegacyDataMigration `
  -SimulateUpgrade `
  -VerifyUninstall `
  -StabilityDurationSec 120 `
  -StabilityProbeIntervalSec 10 `
  -MaxBackendWorkingSetGrowthMB 25 `
  -MaxIdleCpuPercent 2

$scripts = @('scripts\smoke_tauri_desktop.ps1','scripts\smoke_windows_installer.ps1')
foreach ($script in $scripts) {
  $tokens=$null; $errors=$null
  $null=[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $script), [ref]$tokens, [ref]$errors)
  if ($errors.Count) { $errors | ForEach-Object { Write-Error "${script}: $($_.Message)" }; exit 1 }
}
'OK'

venv\Scripts\python.exe -m py_compile tests\test_tauri_stability_smoke_gates.py
venv\Scripts\python.exe -m pytest tests\test_tauri_stability_smoke_gates.py

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1 `
  -ExePath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\scriber-desktop.exe" `
  -DataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\tauri-smoke-data\outputpath-20260602" `
  -DisableDevFallback `
  -StabilityDurationSec 5 `
  -StabilityProbeIntervalSec 2 `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\tauri-smoke-outputpath-20260602.json"

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -LegacyDataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -VerifyLegacyDataMigration `
  -VerifyUninstall `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\installer-smoke-outputpath-20260602.json"
```

Result: passed.

Implemented improvements:

- `scripts\smoke_tauri_desktop.ps1` now supports `-OutputPath` and writes the
  same JSON payload it emits to stdout.
- `scripts\smoke_windows_installer.ps1` now supports `-OutputPath` for
  persisted installer-smoke JSON.
- Smoke output paths are restricted to the repository `tmp` tree before writing.

Evidence:

- Long installer smoke:
  - Runtime mode: `tauri-supervised`.
  - Launch kind: `sidecar`.
  - Legacy migration verified from
    `C:\Users\Alexander.Immler\Documents\Github\Scriber`.
  - Migrated files: `.env` 2,162 bytes, `settings.json` 944 bytes,
    `transcripts.db` 24,276,992 bytes.
  - Upgrade verified: true.
  - Upgrade sentinel preserved: true.
  - Silent uninstall verified: true.
  - Runtime cleanup verified: true.
  - Stability duration after upgrade: 120 seconds.
  - Stability samples after upgrade: 12.
  - Backend working set start/end/max: 187.66 MB / 187.68 MB / 187.73 MB.
  - Backend working-set peak growth: 0.07 MB under the 25 MB gate.
  - Combined app+backend idle CPU max/avg: 0.03% / 0.02% under the 2% gate.
  - `/api/health` and `/api/state` remained responsive for all samples.
- Output artifact smoke:
  - Artifact:
    `tmp\hybrid-baseline\tauri-smoke-outputpath-20260602.json`.
  - Runtime mode: `tauri-supervised`.
  - Launch kind: `sidecar`.
  - Stability samples: 3.
  - Backend working-set growth: 0.07 MB.
  - Combined idle CPU average: 0.01%.
  - Cleanup verified: true.
- Installer output artifact smoke:
  - Artifact:
    `tmp\hybrid-baseline\installer-smoke-outputpath-20260602.json`.
  - Runtime mode: `tauri-supervised`.
  - Launch kind: `sidecar`.
  - Legacy migration verified: true.
  - Silent uninstall verified: true.
  - Cleanup verified: true.
- PowerShell parser check passed for both smoke scripts.
- `tests\test_tauri_stability_smoke_gates.py`: `6 passed`.

Goal coverage:

- Phase 6: strengthens fresh install, legacy-data migration, upgrade,
  uninstall, data preservation, and installed sidecar startup evidence.
- Phase 7: adds regression coverage for persisted smoke JSON artifacts.
- Phase 8: adds an installed idle stability gate with explicit memory and CPU
  thresholds.

Remaining limits:

- This is an installed idle stability run, not a real 30-minute live STT
  provider/microphone session.
- It does not close the physical USB/Bluetooth/dock/default-mic manual matrix.
- It does not replace real Authenticode signing keys or a published signed
  updater manifest.

## 2026-06-02 - Synthetic 30-Minute Transcript Buffer Growth Guard

Commands:

```powershell
venv\Scripts\python.exe -m py_compile scripts\check_transcript_buffer_growth.py tests\perf\test_transcript_buffer_growth_script.py
venv\Scripts\python.exe -m pytest tests\perf\test_transcript_buffer_growth_script.py
venv\Scripts\python.exe scripts\check_transcript_buffer_growth.py --output tmp\hybrid-baseline\transcript-buffer-growth-20260602.json
```

Result: passed.

Evidence:

- Artifact:
  `tmp\hybrid-baseline\transcript-buffer-growth-20260602.json`.
- Simulated final transcript segments: 1,800.
- Segment size: 96 chars.
- Metadata reads during append loop: 60.
- Append loop time: 18.139 ms.
- Final materialization time: 0.199 ms.
- Expected content length: 176,398 chars.
- Materialized content length: 176,398 chars.
- Pending segments before materialization: 1,799.
- Peak memory before explicit materialization: 272,704 bytes.
- Peak memory after explicit materialization: 623,386 bytes.
- `metadataContentLeaked`: false.
- Checks passed:
  `metadataDoesNotExposeContent`,
  `appendDidNotMaterializePendingSegments`,
  `contentStayedAtFirstSegmentBeforeMaterialize`,
  `materializedContentHasExpectedLength`,
  `pendingSegmentsClearedAfterMaterialize`.
- `tests\perf\test_transcript_buffer_growth_script.py`: `2 passed`.

Goal coverage:

- Phase 8: adds the synthetic guard for a 30-minute live transcript session
  without unbounded transcript-string growth.
- Phase 7: keeps this behavior under focused pytest coverage.
- Backend validation: confirms public transcript metadata access does not
  force full transcript materialization.

Remaining limits:

- This is a synthetic backend guard. It does not replace a real 30-minute
  microphone/provider run.
- It does not measure provider memory, frontend live-update memory, audio
  buffers, OS text injection, or docking/device churn during a long session.

## 2026-06-02 - Combined Phase 0 Baseline Attempt With Legacy Data

Command:

```powershell
$env:SCRIBER_LEGACY_DATA_DIR = 'C:\Users\Alexander.Immler\Documents\Github\Scriber'
$env:SCRIBER_AUTO_MIGRATE_LEGACY_DATA = '1'
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 1 `
  -DisableDevFallback `
  -RecordHotPathSamples `
  -RecordingHotPathIterations 3 `
  -RecordingHotPathSeconds 8 `
  -RecordingHotPathTimeoutSec 120 `
  -RecordingHotPathTextTargetFile "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\phase0-full-recording-target.txt" `
  -RecordingHotPathSpeechPrompt "Dies ist ein Scriber Test. Bitte schreibe diesen kurzen Satz in das Textfeld." `
  -RecordingHotPathSpeechDelaySec 1.0 `
  -RecordingHotPathTextTargetSettleSec 1.5 `
  -FailOnPerformanceBudget `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\hybrid-baseline-20260602-phase0-complete-attempt.json"
```

Result: performance budget passed, but Phase 0 is not complete in this combined
run because `stop_to_text_injection` reported `missing_text_injection`.

Evidence:

- Artifact:
  `tmp\hybrid-baseline\hybrid-baseline-20260602-phase0-complete-attempt.json`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- `phase0Complete`: false.
- Incomplete requirement: `stop_to_text_injection`.
- UI visible p95: 1,483.36 ms.
- Backend ready p95: 2,519.4 ms.
- Upload/export benchmark: passed.
- WebSocket benchmark: passed.
- History scroll benchmark: passed.
- Recording hotpath iterations: 3.
- `hotkey_to_recording_state`: measured, p95 301.091 ms.
- `hotkey_to_first_audio_frame`: measured, p95 574.904 ms.
- `stop_to_text_injection`: `missing_text_injection`.
- Upload throughput: 726.06 MB/s across 4 x 4 MB uploads.
- Export total: 345.514 ms.

Goal coverage:

- Phase 0: consolidates startup, upload/export, WebSocket, history, and partial
  recording hot-path evidence in one Tauri-managed sidecar run.
- Phase 2: confirms the combined run used the packaged sidecar with
  `DisableDevFallback`.
- Phase 6: uses the existing legacy data/config directory as runtime input.

Remaining limits:

- This artifact cannot close Phase 0 because stop-to-text injection was missing
  in the combined run.
- The focused sample in
  `tmp\hybrid-baseline\hybrid-baseline-20260602-stop-to-text-quoted.json`
  remains the current stop-to-text latency evidence.

## 2026-06-02 - Stop-to-Text Hot-Path Sample + Gate Hardening

Commands:

```powershell
venv\Scripts\python.exe -m py_compile scripts\measure_recording_hot_path_baseline.py tests\perf\test_recording_hot_path_baseline_script.py
venv\Scripts\python.exe -m pytest tests\perf\test_recording_hot_path_baseline_script.py

$tokens=$null; $errors=$null
$null=[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path scripts\measure_hybrid_baseline.ps1), [ref]$tokens, [ref]$errors)
if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 } else { 'OK' }

$env:SCRIBER_LEGACY_DATA_DIR = 'C:\Users\Alexander.Immler\Documents\Github\Scriber'
$env:SCRIBER_AUTO_MIGRATE_LEGACY_DATA = '1'
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 1 `
  -DisableDevFallback `
  -RecordHotPathSamples `
  -RecordingHotPathIterations 1 `
  -RecordingHotPathSeconds 8 `
  -RecordingHotPathTimeoutSec 120 `
  -RecordingHotPathTextTargetFile "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\recording-hotpath-text-target-quoted.txt" `
  -RecordingHotPathSpeechPrompt "Dies ist ein Scriber Test. Bitte schreibe diesen kurzen Satz in das Textfeld." `
  -RecordingHotPathSpeechDelaySec 1.0 `
  -RecordingHotPathTextTargetSettleSec 1.5 `
  -SkipUploadExportBenchmark `
  -SkipWsBenchmark `
  -SkipHistoryScrollBenchmark `
  -KeepArtifacts `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\hybrid-baseline-20260602-stop-to-text-quoted.json"
```

Result: passed for the session-specific live recording hot-path sample.

Implemented improvements:

- `scripts\measure_hybrid_baseline.ps1` now quotes recording-hotpath child
  process arguments before `Start-Process`; speech prompts containing spaces
  are passed as one argument.
- When `-RecordHotPathSamples` is set, Phase 0 recording-hotpath requirement
  status no longer falls back to arbitrary existing `/api/metrics/hot-path`
  rows. This prevents legacy migrated DB metrics from falsely satisfying a
  failed live recording benchmark.
- The optional text target window in
  `scripts\measure_recording_hot_path_baseline.py` now stays topmost and
  refocuses periodically during the measurement.

Evidence:

- Syntax checks passed for the Python recording script and test.
- PowerShell parser check passed for `scripts\measure_hybrid_baseline.ps1`.
- `tests\perf\test_recording_hot_path_baseline_script.py`: `7 passed`.
- Artifact:
  `tmp\hybrid-baseline\hybrid-baseline-20260602-stop-to-text-quoted.json`.
- Child recording artifact:
  `tmp\hybrid-baseline-data\a55ad3757e414e27aa3635bb6bee3e72\recording-hot-path-1.json`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- UI visible: 1,455.38 ms.
- Backend ready: 2,283.69 ms.
- `hotkey_received_to_mic_ready_ms`: 363.664 ms.
- `hotkey_received_to_first_audio_frame_ms`: 604.303 ms.
- `stop_requested_to_first_paste_ms`: 2,397.109 ms.
- `afterStopInjectionSamples`: 1.
- Recording benchmark summary: `complete: true`.

Goal coverage:

- Phase 0: completes the previously missing live recording hot-path evidence
  for hotkey-to-recording-state, hotkey-to-first-audio-frame, and
  stop-to-text-injection in a Tauri-managed sidecar run.
- Phase 2: confirms the sample ran with `DisableDevFallback` and the packaged
  sidecar runtime.
- Phase 7: adds regression coverage around the measurement runner so failed
  live samples cannot be masked by old database metrics.

Remaining limits:

- The dedicated text target file reported `capturedChars: 0` in the successful
  latency sample, so the measured value is the backend/injector callback timing,
  not verified persisted text inside the target window.
- This is still one short prompted live sample, not the final long live-session
  stability gate.
- Upload/export, WebSocket, and history benchmarks were intentionally skipped
  in this focused run; earlier full-baseline artifacts cover them.

## 2026-06-02 - Stop-to-Text Hot-Path Measurement Tooling

Commands:

```powershell
venv\Scripts\python.exe -m py_compile scripts\measure_recording_hot_path_baseline.py tests\perf\test_recording_hot_path_baseline_script.py
venv\Scripts\python.exe -m pytest tests\perf\test_recording_hot_path_baseline_script.py

$tokens=$null; $errors=$null
$null=[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path scripts\measure_hybrid_baseline.ps1), [ref]$tokens, [ref]$errors)
if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 } else { 'OK' }
```

Result: passed.

Implemented improvements:

- `scripts\measure_recording_hot_path_baseline.py` now has an optional
  `--text-target-file` mode that opens a dedicated Tk text target window and
  periodically persists injected text length. This gives manual or prompted
  hot-path runs a safe injection destination instead of relying on whatever
  application happens to be focused.
- The same script now supports optional Windows SAPI prompt playback via
  `--speech-prompt-text` and `--speech-prompt-delay-sec` so future runs can
  attempt a reproducible speech sample without changing the STT pipeline.
- Multi-iteration target files are suffixed per iteration to keep captured
  injection evidence separated.
- `scripts\measure_hybrid_baseline.ps1` forwards these options through
  `-RecordingHotPathTextTargetFile`, `-RecordingHotPathSpeechPrompt`,
  `-RecordingHotPathSpeechDelaySec`, and
  `-RecordingHotPathTextTargetSettleSec`.

Evidence:

- Recording hot-path script syntax check passed.
- PowerShell parser check for `scripts\measure_hybrid_baseline.ps1` passed.
- `tests\perf\test_recording_hot_path_baseline_script.py`: `5 passed`.

Goal coverage:

- Phase 0: reduces the remaining `stop_to_text_injection` measurement risk by
  making real text-injection samples safer and more reproducible.
- Phase 7: adds regression coverage for the new recording hot-path measurement
  flags and per-iteration target-file behavior.

Remaining limits:

- This is tooling evidence only. It does not itself prove
  `stop_requested_to_first_paste_ms`; that still requires a live sample where
  STT recognizes speech and the injector writes text.
- Prompt playback depends on Windows audio routing and microphone pickup; it is
  an aid for repeatability, not a substitute for final manual microphone tests.

## 2026-06-02 - Live Mic Hot-Path Partial Hardware Sample

Command:

```powershell
$env:SCRIBER_LEGACY_DATA_DIR = 'C:\Users\Alexander.Immler\Documents\Github\Scriber'
$env:SCRIBER_AUTO_MIGRATE_LEGACY_DATA = '1'
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 1 `
  -DisableDevFallback `
  -RecordHotPathSamples `
  -RecordingHotPathIterations 1 `
  -RecordingHotPathSeconds 2 `
  -RecordingHotPathTimeoutSec 90 `
  -SkipUploadExportBenchmark `
  -SkipWsBenchmark `
  -SkipHistoryScrollBenchmark `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\hybrid-baseline-20260602-recording-hotpath.json"
```

Result: passed as a partial Phase 0 hot-path sample.

Evidence:

- Artifact:
  `tmp\hybrid-baseline\hybrid-baseline-20260602-recording-hotpath.json`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- UI visible: 1,541.78 ms.
- Backend ready: 3,113.52 ms.
- `hotkey_received_to_mic_ready_ms`: 860.163 ms.
- `hotkey_received_to_first_audio_frame_ms`: 1,153.683 ms.
- Performance budget remained green for this run:
  UI p95 <= 3,000 ms and backend-ready p95 <= 5,000 ms.

Goal coverage:

- Phase 0: replaces the previous missing live-recording startup evidence for
  hotkey-to-recording-state and hotkey-to-first-audio-frame with a real
  Tauri-managed sidecar sample using the local microphone stack.
- Phase 2: confirms the measurement ran with `DisableDevFallback` and the
  packaged sidecar rather than the source Python module.
- Phase 6: confirms the sample could use temporary runtime data populated from
  the legacy Scriber data directory.

Remaining limits:

- `stop_to_text_injection` is still not complete. The sample reported
  `missing_text_injection` because no text was injected during the 2-second
  recording.
- This was a single short sample, not a statistically meaningful latency run.
- Upload/export, WebSocket, and history benchmarks were intentionally skipped
  in this run because earlier full-baseline artifacts already cover them.

## 2026-06-02 - Tauri Global Hotkey Endpoint Contract

Commands:

```powershell
cargo test --manifest-path Frontend\src-tauri\Cargo.toml desktop_hotkey -- --nocapture
cargo test --manifest-path Frontend\src-tauri\Cargo.toml --lib
venv\Scripts\python.exe -m pytest tests\test_tauri_security_gates.py tests\test_tauri_stability_smoke_gates.py
```

Result: passed.

Implemented improvements:

- Added Rust unit coverage for Tauri global hotkey dispatch semantics.
- Toggle mode dispatches only `POST /api/live-mic/toggle` on shortcut press.
- Push-to-talk mode dispatches `POST /api/live-mic/start` on press and
  `POST /api/live-mic/stop` on release.
- Toggle press events remain debounced at the Tauri shell boundary.

Evidence:

- Targeted Rust hotkey tests: `2 passed`.
- Full Tauri Rust library tests: `22 passed`.
- Existing Python Tauri security and stability source gates: `9 passed`.

Goal coverage:

- Phase 4: strengthens the guarantee that the Tauri desktop shell only calls
  existing Python live-mic API endpoints instead of owning recording state.
- Phase 1/2: preserves the localhost REST contract and session-token backend
  boundary used by the Tauri hotkey path.
- Security rule: keeps Tauri capabilities minimal and does not introduce a
  shell/opener permission to implement hotkeys.

Remaining limits:

- This is unit/source-gate evidence, not a real OS global shortcut smoke with
  physical key presses.
- It does not verify real microphone capture, text injection, or provider
  latency after the hotkey event.

## 2026-06-02 - Frozen Frontend Static Serving Boundary

Commands:

```powershell
venv\Scripts\python.exe -m pytest tests\test_web_api_security.py::test_static_frontend_routes_do_not_bypass_api_session_token tests\test_web_api_security.py::test_frontend_file_for_request_blocks_path_traversal
venv\Scripts\python.exe -m pytest tests\test_web_api_security.py tests\test_web_api_lifecycle.py
venv\Scripts\python.exe -m py_compile src\web_api.py tests\test_web_api_security.py
```

Result: passed.

Implemented improvements:

- `src.web_api` can now serve built React assets from
  `SCRIBER_FRONTEND_DIST_DIR`, PyInstaller `sys._MEIPASS`, app-root, or
  source-checkout `Frontend/dist/public` candidates.
- Non-API routes use an SPA fallback to `index.html`.
- Missing concrete asset paths such as `/assets/missing.js` return 404 instead
  of incorrectly rendering the SPA shell.
- `/api/*` and `/ws` are never handled by the static fallback and remain
  session-token protected when `SCRIBER_SESSION_TOKEN` is configured.

Evidence:

- `tests\test_web_api_security.py::test_static_frontend_routes_do_not_bypass_api_session_token`
  proves `/`, `/settings`, and `/assets/app.js` are served from a synthetic
  frontend dist directory while `/api/runtime` still returns 401 without the
  token and 200 with `X-Scriber-Token`.
- `tests\test_web_api_security.py::test_frontend_file_for_request_blocks_path_traversal`
  proves path traversal is rejected before file serving.
- Full targeted web API regression: `55 passed` for
  `tests\test_web_api_security.py tests\test_web_api_lifecycle.py`.

Goal coverage:

- Phase 3: moves React production serving closer to a self-contained
  Tauri/Python runtime and keeps Express as dev/legacy infrastructure.
- Phase 1/2: preserves the session-token boundary for API and WebSocket routes.
- Phase 6: supports PyInstaller-bundled frontend assets already included by
  `packaging/scriber-backend.spec`.

Remaining limits:

- Tauri production primarily loads the frontend through Tauri's bundled
  `frontendDist`; this backend static fallback is still secondary validation
  for frozen/browser or legacy paths.
- This does not decide when the legacy Express server can be removed.

## 2026-06-02 - Frontend Typecheck, Build, and Strict Browser Smoke

Commands:

```powershell
cd Frontend
npm run check
npm run build

cd ..
venv\Scripts\python.exe scripts\smoke_frontend_browser.py `
  --output tmp\frontend-smoke-20260602-strict.json
```

Result: passed.

Implemented improvements:

- Hardened `scripts\smoke_frontend_browser.py` cleanup so Windows browser/CDP
  shutdown no longer prints a Proactor `ConnectionResetError` after a
  successful smoke run.
- Made the frontend browser smoke stricter for history-heavy routes:
  `/`, `/youtube`, and `/file` now wait for the virtualized history root and
  fail if it is missing.

Evidence:

- TypeScript strict check: `npm run check` passed.
- Production frontend/server build: `npm run build` passed.
- Build output included statically split route chunks for
  `TranscriptDetail`, `FileTranscribe`, `Youtube`, `Settings`, React/vendor,
  and the app entry.
- Browser smoke artifact:
  `tmp\frontend-smoke-20260602-strict.json`.
- Smoke result: `ok: true`.
- Routes verified in a real browser via CDP:
  `/`, `/youtube`, `/file`, `/settings`, `/transcript/mic-00001`.
- Critical console errors: 0.
- Page errors: 0.
- Unhandled rejections: 0.
- Virtualized history routes: `/`, `/youtube`, `/file`.
- Visible synthetic history cards: 30 on each virtualized route.

Goal coverage:

- Phase 3: proves the React app still builds as static Tauri-ready assets.
- Phase 7: adds real-browser frontend coverage across Live Mic, YouTube, File,
  Settings, and Transcript Detail routes.
- Phase 7/8: strengthens the frontend smoke so history virtualization is a
  checked condition instead of an incidental observation.

Remaining limits:

- This is a synthetic-backend frontend smoke; it does not prove real STT
  provider behavior, microphone hardware, or text injection.
- It does not replace the full Tauri installed-app smoke, because it runs the
  web UI in a browser with a synthetic backend.

## 2026-06-02 - Startup Hot Path + Corrected Baseline Runner

Commands:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 `
  -SkipFrontendBuild `
  -BundleMediaTools `
  -CopyToTauriRelease

cargo build --manifest-path Frontend\src-tauri\Cargo.toml --release

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 3 `
  -DisableDevFallback `
  -SkipUploadExportBenchmark `
  -SkipWsBenchmark `
  -SkipHistoryScrollBenchmark `
  -FailOnPerformanceBudget `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\hybrid-baseline-20260602-early-backend-corrected-startup-budget.json"

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 3 `
  -DisableDevFallback `
  -FailOnPerformanceBudget `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\hybrid-baseline-20260602-early-backend-corrected-full-budget.json"
```

Result: passed for the corrected startup and full visible baseline budgets.

Implemented improvements:

- `src.web_api` no longer imports the Pipecat-backed `src.pipeline` at module
  import time. The transcription pipeline is loaded lazily when live mic,
  file, or YouTube transcription actually needs it.
- The Tauri shell creates `BackendManager` before the Tauri builder and calls
  `ensure_started()` immediately, so the backend sidecar starts before shell
  menu, tray, updater, global shortcut, and WebView setup work.
- `scripts\measure_hybrid_baseline.ps1` now measures UI visibility and backend
  health in parallel and polls `/api/health` directly on the expected managed
  backend port. The previous serial flow waited for UI first and then used
  expensive process/TCP enumeration, which over-counted backend readiness by
  several seconds.
- Child benchmark waits now use explicit process wait timeouts and compact
  artifact summaries. This fixed the previous final-JSON hang after child
  benchmark output had already been written.

Evidence:

- Source import timing before lazy pipeline loading: `import src.web_api`
  imported `src.pipeline` and took about 10.4 seconds, with `src.pipeline`
  accounting for about 9.1 seconds.
- Source import timing after lazy pipeline loading: `import src.web_api` took
  about 1.3-1.4 seconds, and `src.pipeline` was no longer imported during
  web API startup.
- Rebuilt sidecar copied to
  `Frontend\src-tauri\target\release\backend\scriber-backend.exe`
  (33,442,124 bytes, timestamp 2026-06-02 02:03:08).
- Rebuilt desktop shell:
  `Frontend\src-tauri\target\release\scriber-desktop.exe`
  (12,442,624 bytes, timestamp 2026-06-02 02:11:17).
- A rebuilt-sidecar startup run with the old serial/WMI runner still failed the
  backend budget: UI p95 296.36 ms passed, backend ready p95 11,932.67 ms
  failed. Keep-artifact logs showed the Python sidecar reached the HTTP
  listener about 1.6 seconds after spawn, so the old runner was measuring
  shell/runner delay rather than only backend readiness.
- Corrected startup-only baseline:
  `tmp\hybrid-baseline\hybrid-baseline-20260602-early-backend-corrected-startup-budget.json`.
  UI visible p95: 1,630.87 ms. Backend listener p95: 2,595.13 ms.
  Backend ready p95: 2,595.13 ms. Both budgets passed.
- Corrected full visible baseline:
  `tmp\hybrid-baseline\hybrid-baseline-20260602-early-backend-corrected-full-budget.json`.
  UI visible p95: 1,597.45 ms. Backend listener p95: 2,669.90 ms.
  Backend ready p95: 2,669.90 ms. Runtime fetch p95: 71.77 ms.
  Both startup budgets passed.
- Upload/export artifact:
  `tmp\hybrid-baseline\hybrid-baseline-20260602-early-backend-corrected-full-budget-upload-export.json`.
  Synthetic upload: 4 x 4 MB, total 20.36 ms, 785.84 MB/s, upload p95
  18.76 ms. Export p95: 236.37 ms. `/api/health` p95 under load:
  27.56 ms. `/api/state` p95 under load: 27.35 ms.
- WebSocket artifact:
  `tmp\hybrid-baseline\hybrid-baseline-20260602-early-backend-corrected-full-budget-websocket.json`.
  JSON serialization p95: 0.0023 ms. Broadcast p95: 0.0002 ms with no
  clients, 0.0102 ms with one client, 0.0236 ms with five clients.
- History scroll artifact:
  `tmp\hybrid-baseline\hybrid-baseline-20260602-early-backend-corrected-full-budget-history-scroll.json`.
  2,000 transcript items, virtualized: true, max visible cards: 54, total API
  requests: 80, duration p95: 34,314.9 ms, max frame gap p95: 316.7 ms.

Goal coverage:

- Phase 0: adds corrected, repeatable startup evidence for UI-visible and
  backend-ready budgets.
- Phase 0: adds upload/export, WebSocket serialization/broadcast, and large
  history scroll baseline artifacts.
- Phase 2: validates that the release Tauri shell can start the packaged
  sidecar early and reach `tauri-supervised` health without dev fallback.
- Phase 8: adds baseline endpoint responsiveness evidence under synthetic
  upload/export load.

Remaining limits:

- Phase 0 is still incomplete for live recording hot-path metrics:
  hotkey-to-recording-state, hotkey-to-first-audio-frame, and
  stop-to-text-injection were not collected in this run.
- The history list is virtualized, but the browser scroll benchmark still shows
  high duration and frame gaps; this remains a UX performance improvement
  target rather than a startup gate failure.
- This run did not exercise microphone hardware, provider stability during a
  long live recording, Authenticode signing, updater publication, or the final
  long-duration memory-growth gate.

## 2026-06-02 - Tauri Sidecar + Real Legacy Data Smoke

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1 `
  -BackendExePath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\backend\scriber-backend.exe" `
  -LegacyDataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -VerifyLegacyDataMigration `
  -DisableDevFallback `
  -TimeoutSec 90 `
  -BackendHealthTimeoutSec 40
```

Result: passed.

Evidence:

- Release shell: `Frontend\src-tauri\target\release\scriber-desktop.exe`.
- Backend launch kind: `sidecar`.
- Runtime mode: `tauri-supervised`.
- Backend health: ready, API version `1`.
- Backend port: `127.0.0.1:8765`.
- Test data target: `tmp\tauri-smoke-data\7cb1578c03224e00bbb178b9673a219e`.
- Runtime `dataDir` matched the test data target.
- Runtime `downloadsDir` was under the test data target.
- Legacy source: `C:\Users\Alexander.Immler\Documents\Github\Scriber`.
- Legacy `.env`: copied, 2162 bytes, hash matched.
- Legacy `settings.json`: copied, 944 bytes, hash matched.
- Legacy `transcripts.db`: copied, 24276992 bytes.
- Legacy `downloads`: checked, 0 source files.
- Smoke cleanup: verified.

Goal coverage:

- Phase 2: proves the release Tauri shell can supervise a packaged Python
  sidecar and reach the backend health/runtime contracts without dev fallback.
- Phase 6: proves existing local `.env`, `settings.json`, and `transcripts.db`
  are migrated into `SCRIBER_DATA_DIR` without overwriting the source data.
- Phase 7: adds a real Windows desktop smoke run using the user's actual legacy
  data directory instead of only synthetic fixtures.

Remaining limits:

- This run did not exercise the NSIS installer, upgrade, uninstall, Authenticode
  signing, updater publication, microphone hardware, or long stability gates.
- `transcripts.db` was verified by existence and byte size during the running
  backend smoke, not by a full content hash, because the smoke avoids locking or
  reading the live SQLite database more aggressively than necessary.

## 2026-06-02 - NSIS Installer + Real Legacy Data + Upgrade + Uninstall Smoke

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -LegacyDataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -VerifyLegacyDataMigration `
  -SimulateUpgrade `
  -VerifyUninstall
```

Result: passed.

Evidence:

- Installer: `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Temporary install dir: `tmp\installer-smoke\Scriber`.
- Installed app executable: `tmp\installer-smoke\Scriber\scriber-desktop.exe`.
- Test data target: `tmp\installer-smoke\data-78908a0fc6b34873a733802976723e60`.
- First installed runtime mode: `tauri-supervised`.
- First installed launch kind: `sidecar`.
- Legacy source: `C:\Users\Alexander.Immler\Documents\Github\Scriber`.
- Legacy `.env`: copied, 2162 bytes, hash matched.
- Legacy `settings.json`: copied, 944 bytes, hash matched.
- Legacy `transcripts.db`: copied, 24276992 bytes.
- Upgrade simulation: verified.
- Upgrade sentinel: preserved.
- Second installed runtime mode: `tauri-supervised`.
- Second installed launch kind: `sidecar`.
- Second smoke cleanup: verified.
- Silent uninstall: verified.
- Installed app artifacts after uninstall: removed.
- Runtime data directory after uninstall: preserved.
- Uninstall data sentinel: preserved.

Goal coverage:

- Phase 2: proves the installed app starts the packaged Python sidecar without
  Node or a manual Python setup.
- Phase 6: proves first install, upgrade rerun, and silent uninstall behavior
  for the generated NSIS package while preserving existing runtime data.
- Phase 7: adds a real installed-app Windows smoke using the user's actual
  legacy data directory.

Remaining limits:

- This run did not exercise Authenticode signing, updater publication,
  microphone hardware, worker-crash recovery, occupied-port recovery, startup
  timeout recovery, or long stability gates.
- As in the desktop smoke, `transcripts.db` was verified by existence and byte
  size while the backend was active, not by full content hash.

## 2026-06-02 - NSIS Managed Recovery + Port Conflict + Stability Smoke

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -OccupyDefaultPort `
  -SimulateBackendCrash `
  -SimulateBackendShutdown `
  -StabilityDurationSec 30 `
  -StabilityProbeIntervalSec 5 `
  -MaxBackendWorkingSetGrowthMB 128 `
  -MaxIdleCpuPercent 2
```

Result: passed.

Evidence:

- Installer: `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Default port conflict: verified.
- Occupied port: `127.0.0.1:8765`.
- Initial backend port: `51257`.
- Recovered backend port: `51257`.
- Worker crash recovery: verified.
- Killed backend PID: `28436`.
- Crash replacement backend PID: `35852`.
- Controlled shutdown recovery: verified.
- Shutdown backend PID: `35852`.
- Shutdown replacement backend PID: `35424`.
- Shutdown endpoint response: `Shutdown requested`.
- Crash metadata path: `tmp\installer-smoke\data-d2b5c3bc163645d7bc026c22e9975384\logs\backend-crash-metadata.jsonl`.
- Stability duration: 30 seconds.
- Stability sample count: 6.
- Backend working-set start/end/max: 178.89 MB / 178.94 MB / 178.98 MB.
- Backend working-set peak growth: 0.09 MB against a 128 MB gate.
- Combined idle CPU average: 0.07% against a 2% gate.
- Combined idle CPU max: 0.11%.
- `/api/health` and `/api/state` probes stayed ready/idle.
- Silent uninstall: verified.
- Installed app artifacts after uninstall: removed.
- Runtime data directory after uninstall: preserved.

Goal coverage:

- Phase 2: proves worker supervision recovers both hard worker crashes and
  token-protected controlled shutdowns in the installed app.
- Phase 2: proves the installed supervisor selects a non-default loopback port
  when `127.0.0.1:8765` is occupied.
- Phase 6: proves crash metadata is written under the runtime data log
  directory during installed-app recovery.
- Phase 8: adds a short installed idle stability gate for health/state
  responsiveness, memory growth, and CPU budget.

Remaining limits:

- This run did not exercise startup-timeout recovery, external-backend attach,
  Authenticode signing, updater publication, microphone hardware, or long
  recording stability.
- The 30-second idle stability gate is a smoke test, not the final 30-minute
  live-session memory-growth gate from Phase 8.

## 2026-06-02 - NSIS Startup-Timeout Recovery Smoke

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -SimulateBackendStartupTimeout `
  -StabilityDurationSec 15 `
  -StabilityProbeIntervalSec 5 `
  -MaxBackendWorkingSetGrowthMB 128 `
  -MaxIdleCpuPercent 2
```

Result: passed.

Evidence:

- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Startup-timeout recovery: verified.
- Timed-out backend PID: `30212`.
- Replacement backend PID: `52304`.
- Backend startup timeout threshold: 3000 ms.
- Startup-timeout marker: `tmp\installer-smoke\data-370f3077a2924b0fb4efcf3fa0f5094b\startup-timeout-once.marker`.
- Stability duration: 15 seconds.
- Stability sample count: 3.
- Backend working-set start/end/max: 179.89 MB / 179.97 MB / 179.97 MB.
- Backend working-set peak growth: 0.08 MB against a 128 MB gate.
- Combined idle CPU average: 0.00% against a 2% gate.
- Combined idle CPU max: 0.01%.
- `/api/health` and `/api/state` probes stayed ready/idle after replacement.
- Silent uninstall: verified.
- Installed app artifacts after uninstall: removed.
- Runtime data directory after uninstall: preserved.

Goal coverage:

- Phase 2: proves the installed supervisor replaces a worker that starts but
  never reaches backend readiness in time.
- Phase 8: adds a short stability check after startup-timeout replacement.

Remaining limits:

- This run did not exercise worker-crash recovery, controlled shutdown recovery,
  external-backend attach, Authenticode signing, updater publication,
  microphone hardware, or long recording stability.

## 2026-06-02 - NSIS External Backend Attach Smoke

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -AttachExternalBackend `
  -StabilityDurationSec 15 `
  -StabilityProbeIntervalSec 5 `
  -MaxBackendWorkingSetGrowthMB 128 `
  -MaxIdleCpuPercent 2
```

Result: passed.

Evidence:

- Runtime mode: `external-python`.
- Launch kind: `external-python`.
- External backend attach: verified.
- External backend PID: `50500`.
- External backend port: `127.0.0.1:8765`.
- Managed backend spawned: false.
- Stability duration: 15 seconds.
- Stability sample count: 3.
- Backend working-set start/end/max: 381.53 MB / 381.54 MB / 381.54 MB.
- Backend working-set peak growth: 0.01 MB against a 128 MB gate.
- Combined idle CPU average: 0.29% against a 2% gate.
- Combined idle CPU max: 0.82%.
- `/api/health` and `/api/state` probes stayed ready/idle.
- Silent uninstall: verified.
- Installed app artifacts after uninstall: removed.
- Runtime data directory after uninstall: preserved.

Goal coverage:

- Phase 2/3: proves the installed Tauri shell can attach to an already running
  Python backend on the runtime backend URL path without spawning a duplicate
  managed sidecar.
- Phase 8: adds a short stability check for the external-backend runtime mode.

Remaining limits:

- This run did not exercise managed sidecar recovery, Authenticode signing,
  updater publication, microphone hardware, or long recording stability.
