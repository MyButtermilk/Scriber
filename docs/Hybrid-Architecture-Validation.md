# Hybrid Architecture Validation Log

This file records concrete validation evidence for `docs/Hybrid-Architecture-Goal.md`.
It is intentionally separate from the goal text so local goal edits can stay
unmixed with verification results.

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
