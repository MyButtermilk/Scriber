# Hybrid Architecture Validation Log

This file records concrete validation evidence for `docs/Hybrid-Architecture-Goal.md`.
It is intentionally separate from the goal text so local goal edits can stay
unmixed with verification results.

## 2026-06-02 - Strict Recording Text Target Persistence Gate

Commands:

```powershell
python -m py_compile `
  scripts\measure_recording_hot_path_baseline.py `
  tests\perf\test_recording_hot_path_baseline_script.py

powershell -NoProfile -Command `
  '$tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path "scripts\measure_hybrid_baseline.ps1"), [ref]$tokens, [ref]$errors) | Out-Null; if ($errors.Count -gt 0) { $errors | Format-List *; exit 1 }'

python -m pytest `
  tests/perf/test_recording_hot_path_baseline_script.py `
  tests/perf/test_text_injection_smoke_script.py

python scripts\measure_recording_hot_path_baseline.py `
  --validate-only `
  --require-text-target `
  --text-target-file tmp\hybrid-baseline\strict-text-target-validate.txt `
  --output tmp\hybrid-baseline\recording-hotpath-strict-text-target-validate-20260602.json

git diff --check
```

Result: passed.

Implemented improvements:

- `scripts\measure_recording_hot_path_baseline.py` now waits briefly after a
  live recording metric appears to verify whether the controlled target file
  contains non-empty text.
- Recording child artifacts now include `summary.textTarget` with configured
  target samples, captured sample count, captured character counts, max
  captured chars, and capture elapsed durations.
- `--require-text-target` adds a `text_target_persistence` requirement. It
  reports `missing_target_window` when no target is configured and
  `missing_target_text` when the target exists but captured zero characters.
- `scripts\measure_hybrid_baseline.ps1` forwards
  `-RecordingHotPathTextTargetTimeoutSec` and
  `-RequireRecordingHotPathTextTarget`, adds the strict requirement to the Phase
  0 gate, and validates that strict mode has a target file.

Evidence:

- Focused perf-gate tests: `21 passed`.
- Validate-only strict target artifact: passed with
  `text_target_persistence.status=measured`.
- PowerShell parser check: passed.
- Python compile check: passed.
- Whitespace diff check: passed.

Limitations:

- This adds a stricter, repeatable gate. It does not by itself prove a real
  spoken live recording injected text into the target; that still requires a
  live run with `-RecordHotPathSamples -RecordingHotPathTextTargetFile ...`
  plus `-RequireRecordingHotPathTextTarget`.

## 2026-06-02 - Always-On Mic Stream Reuse For Faster Preparing

Commands:

```powershell
python -m py_compile `
  src/mic_prewarm.py `
  src/microphone.py `
  src/pipeline.py `
  src/web_api.py

python -m pytest `
  tests/test_mic_prewarm.py `
  tests/test_microphone_callback.py `
  tests/test_web_api_lifecycle.py `
  tests/test_pipeline_stop.py `
  tests/test_microphone_device_resolution.py `
  tests/test_web_api_hot_path_metrics.py `
  tests/test_web_api_reliability.py

git diff --check

powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\build_windows.ps1 `
  -SkipChecks `
  -SkipSmoke

Frontend\src-tauri\target\release\backend\scriber-backend.exe `
  --runtime-import-check
```

Result: passed.

Implemented improvements:

- `MIC_ALWAYS_ON` no longer necessarily closes the warm idle PortAudio stream
  before live recording starts.
- `MicrophonePrewarmManager` can route its warm stream callback into the active
  `MicrophoneInput` when sample rate, target channels, block size, and requested
  device still match.
- `MicrophoneInput` adopts that warm stream before falling back to the old
  close-and-open path; stop detaches the callback and returns the stream to idle
  discard mode.
- `ScriberWebController.start_listening()` passes the prewarm manager into the
  pipeline and avoids the pre-start close when `MIC_ALWAYS_ON` is enabled.
- The warm stream now uses the same multi-channel capture strategy as active
  mono capture where available, preserving the strongest-channel workaround.

Evidence:

- Current installed-package logs before this change showed the hotkey dispatch
  itself was not the bottleneck: `hotkey_received_to_controller_accepted_ms`
  was about `0 ms`, while `hotkey_received_to_mic_ready_ms` varied between
  about `176 ms` and `632 ms`; the slow segment was PortAudio stream
  query/open/start and not Tauri hotkey dispatch.
- Focused lifecycle tests: `64 passed`.
- Python compile check: passed.
- Whitespace diff check: passed.
- Windows build without smoke gates: passed.
- Frozen backend runtime import check: passed.
- New installer:
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- New installer size: `205.78 MiB`, under the `220 MiB` budget.

## 2026-06-02 - Installed Always-On Stream Reuse Package Smoke

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\smoke_windows_installer.ps1 `
  -InstallerPath "Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -InstallDir "tmp\installer-smoke\alwayson-stream\Scriber" `
  -DataDir "tmp\installer-smoke\alwayson-stream\data" `
  -OutputPath "tmp\hybrid-baseline\installer-alwayson-frontend-smoke-20260602.json" `
  -VerifyFrontend `
  -MaxInstalledSizeMB 650
```

Result: passed.

Evidence:

- Installed runtime mode: `tauri-supervised`.
- Installed launch kind: `sidecar`.
- Real Tauri WebView frontend readiness: `webViewReady=true`.
- Tauri production origin verified: `webViewLocationOrigin=http://tauri.localhost`.
- WebView backend URL: `http://127.0.0.1:8765`.
- Frontend root served HTTP `200`, and all `6` referenced JS/CSS assets returned HTTP `200`.
- Tauri-origin CORS checks passed for `/api/health` and tokenized `/api/runtime`; Chromium private-network preflight passed.
- Installed directory size: `614.49 MiB`, under the temporary `650 MiB` smoke budget. The largest files remain bundled `ffmpeg.exe` and `ffprobe.exe`, which are intentionally retained.
- Cleanup verified: managed backend exited with the app.
- Silent uninstall verified: install artifacts were removed and runtime-data preservation was confirmed before temp cleanup.

Artifact:

- `tmp\hybrid-baseline\installer-alwayson-frontend-smoke-20260602.json`

## 2026-06-02 - Always-On Stream Reuse Live Hot-Path Measurement

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\measure_hybrid_baseline.ps1 `
  -Iterations 1 `
  -DisableDevFallback `
  -Hidden `
  -SkipUiVisibleWait `
  -SkipUploadExportBenchmark `
  -SkipWsBenchmark `
  -SkipHistoryScrollBenchmark `
  -RecordHotPathSamples `
  -RecordingHotPathIterations 1 `
  -RecordingHotPathSeconds 3 `
  -RecordingHotPathTimeoutSec 90 `
  -RecordingHotPathTextTargetFile "tmp\hybrid-baseline\alwayson-hotpath-target-20260602.txt" `
  -RecordingHotPathTextTargetTimeoutSec 8 `
  -RecordingHotPathSpeechPrompt "Scriber hot path validation prompt. This is a short recording test." `
  -RecordingHotPathSpeechDelaySec 0.5 `
  -LegacyDataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -OutputPath "tmp\hybrid-baseline\hybrid-baseline-alwayson-hotpath-20260602.json"
```

Result: passed.

Evidence:

- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- `SCRIBER_MIC_ALWAYS_ON=1` was supplied through migrated legacy `.env`.
- Backend listener: `2728.52 ms`.
- Backend ready: `2825.19 ms`.
- Recording hot-path summary: `complete=true`.
- Hotkey to mic ready: `70.933 ms`.
- Hotkey to first audio frame: `99.428 ms`.
- Hotkey to first audible audio frame: `1507.902 ms`.
- Hotkey to first final provider token: `4262.776 ms`.
- Stop to text injection: `1387.75 ms`.
- Controlled text target captured persisted text: `capturedSamples=1`,
  `maxCapturedChars=39`, `captureElapsedMs=4636.593`.
- Cleanup verified: managed backend exited with the Tauri app.

Artifacts:

- `tmp\hybrid-baseline\hybrid-baseline-alwayson-hotpath-20260602.json`
- `tmp\hybrid-baseline\hybrid-baseline-alwayson-hotpath-20260602-recording-hot-path-1.json`
- `tmp\hybrid-baseline\alwayson-hotpath-target-20260602.txt`

Goal coverage:

- Phase 0: replaces the previous weak stop-to-text/live-target evidence with a
  real Tauri-managed sidecar run that persisted non-empty injected text in the
  controlled target window.
- Phase 8: provides the first measured post-change evidence that always-on
  stream adoption reduces `Preparing` from the earlier `176-632 ms` mic-ready
  range to about `71 ms` for this sample and first audio frame to about `99 ms`.

## 2026-06-02 - Current Installer Release Metadata Integrity

Commands:

```powershell
python scripts\validate_tauri_updater_metadata.py `
  --metadata Frontend\src-tauri\target\release\release-metadata\latest.json `
  --artifact-dir Frontend\src-tauri\target\release\bundle `
  --sha256sums Frontend\src-tauri\target\release\release-metadata\SHA256SUMS.txt `
  --allow-local-urls

python scripts\create_release_metadata.py `
  --artifact Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe `
  --output-dir tmp\release-metadata-https-smoke `
  --base-url https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0

python scripts\validate_tauri_updater_metadata.py `
  --metadata tmp\release-metadata-https-smoke\latest.json `
  --artifact-dir Frontend\src-tauri\target\release\bundle `
  --sha256sums tmp\release-metadata-https-smoke\SHA256SUMS.txt
```

Result: passed.

Evidence:

- Current local release metadata artifact:
  `Frontend\src-tauri\target\release\release-metadata\latest.json`.
- Current checksum manifest:
  `Frontend\src-tauri\target\release\release-metadata\SHA256SUMS.txt`.
- Current installer artifact:
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Local unsigned metadata validation passed with `allowLocalUrls=true` and
  `localArtifactsVerified=1`, proving the generated metadata matches the
  current installer size and SHA256 manifest.
- Temporary HTTPS metadata generation and validation passed with
  `allowLocalUrls=false` and `localArtifactsVerified=1`, proving the generator
  can emit Tauri-updater-compatible absolute HTTPS URLs when a release base URL
  is supplied.

Limitations:

- This does not prove a published updater endpoint exists.
- This does not prove Tauri updater signatures because no real signing key was
  provided for this local build.

## 2026-06-02 - Current Installer Supervisor Recovery Matrix

Commands:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\smoke_windows_installer.ps1 `
  -InstallerPath "Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -InstallDir "tmp\installer-smoke\current-recovery\Scriber" `
  -DataDir "tmp\installer-smoke\current-recovery\data" `
  -OutputPath "tmp\hybrid-baseline\installer-current-recovery-20260602.json" `
  -VerifyFrontend `
  -VerifySupportBundle `
  -SimulateBackendCrash `
  -SimulateBackendShutdown `
  -OccupyDefaultPort `
  -VerifyUninstall `
  -MaxInstalledSizeMB 650

powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\smoke_windows_installer.ps1 `
  -InstallerPath "Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -InstallDir "tmp\installer-smoke\current-startup-timeout\Scriber" `
  -DataDir "tmp\installer-smoke\current-startup-timeout\data" `
  -OutputPath "tmp\hybrid-baseline\installer-current-startup-timeout-20260602.json" `
  -VerifyFrontend `
  -SimulateBackendStartupTimeout `
  -VerifyUninstall `
  -MaxInstalledSizeMB 650

powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\smoke_windows_installer.ps1 `
  -InstallerPath "Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -InstallDir "tmp\installer-smoke\current-external-attach\Scriber" `
  -DataDir "tmp\installer-smoke\current-external-attach\data" `
  -OutputPath "tmp\hybrid-baseline\installer-current-external-attach-20260602.json" `
  -VerifyFrontend `
  -AttachExternalBackend `
  -VerifyUninstall `
  -MaxInstalledSizeMB 650
```

Result: passed.

Evidence:

- Current installer:
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Installed directory size in all three runs: `614.49 MiB`, under the temporary
  `650 MiB` installed-size smoke budget.
- Recovery run:
  - Runtime mode: `tauri-supervised`.
  - Launch kind: `sidecar`.
  - Default backend port `8765` was occupied; the installed supervisor selected
    port `50812`.
  - Worker crash recovery verified:
    killed backend PID `44036`, replacement backend PID `40512`.
  - Controlled shutdown recovery verified:
    stopped backend PID `40512`, replacement backend PID `43500`, response
    message `Shutdown requested`.
  - Crash metadata written:
    `tmp\installer-smoke\current-recovery\data\logs\backend-crash-metadata.jsonl`.
  - Support bundle verified: token-protected, unauthorized request returned
    `401`, `11` required entries present, redaction verified.
  - WebView frontend ready:
    `webViewReady=true`,
    `webViewBackendBaseUrl=http://127.0.0.1:50812`,
    `webViewLocationOrigin=http://tauri.localhost`.
  - Cleanup verified and strict silent uninstall verified.
- Startup-timeout run:
  - Runtime mode: `tauri-supervised`.
  - Launch kind: `sidecar`.
  - Startup timeout recovery verified:
    timed-out backend PID `38948`, replacement backend PID `42504`,
    `backendStartupTimeoutMs=3000`.
  - WebView frontend ready:
    `webViewReady=true`,
    `webViewBackendBaseUrl=http://127.0.0.1:8765`.
  - Cleanup verified and strict silent uninstall verified.
- External-backend attach run:
  - Runtime mode: `external-python`.
  - Launch kind: `external-python`.
  - External backend attach verified:
    external backend PID `3008`, port `8765`,
    `managedBackendSpawned=false`.
  - WebView frontend ready:
    `webViewReady=true`,
    `webViewBackendBaseUrl=http://127.0.0.1:8765`.
  - Cleanup verified and strict silent uninstall verified.

Artifacts:

- `tmp\hybrid-baseline\installer-current-recovery-20260602.json`
- `tmp\hybrid-baseline\installer-current-startup-timeout-20260602.json`
- `tmp\hybrid-baseline\installer-current-external-attach-20260602.json`

Goal coverage:

- Phase 2: revalidates the current installed supervisor against crash,
  controlled shutdown, startup-timeout replacement, default-port conflict, and
  external-backend attach paths.
- Phase 3: revalidates the real installed Tauri WebView readiness beacon and
  runtime backend URL in sidecar, dynamic-port, and external-backend modes.
- Phase 6/7: revalidates current NSIS install/uninstall behavior, installed
  support-bundle redaction, and sidecar packaging after the Always-On rebuild.

## 2026-06-02 - Legacy Desktop Fallback Decision

Artifact:

- `docs\Legacy-Desktop-Fallback-Decision.md`

Decision:

- Tauri is the primary and release-target desktop runtime.
- Legacy Tkinter and Python tray paths remain available as maintenance-only
  fallback until at least two Tauri release candidates pass the installed smoke
  matrix and manual hardware/release gates.
- No new user-facing desktop-shell features should be added to legacy Tkinter or
  Python tray code unless needed to keep fallback startup functional.
- Tauri-managed runtime keeps ownership of desktop autostart and global hotkey
  dispatch; recording state remains owned by the Python backend through the
  existing API.

Goal coverage:

- Phase 4/8: closes the previously open architecture decision about whether
  `src.main`, `src.ui`, and `src.tray` stay as fallback or are removed now. They
  stay as maintenance-only fallback; removal is gated on two stable Tauri
  release candidates plus the remaining physical and release-signing gates.

## 2026-06-02 - Frontend Scale, YouTube Thumbnail, Tray, and Waveform Fixes

Commands:

```powershell
python -m pytest `
  tests/test_frontend_type_gates.py `
  tests/test_web_api_security.py `
  tests/test_microphone_callback.py `
  tests/test_tauri_security_gates.py `
  tests/test_web_api_lifecycle.py

cd Frontend
npm run check
npm run build
cd src-tauri
cargo test
cd ..\..

python scripts\smoke_frontend_browser.py --output tmp\frontend-browser-smoke-ui-fixes.json
python -m py_compile src/web_api.py src/microphone.py
git diff --check

powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\build_windows.ps1 `
  -SkipChecks `
  -SkipSmoke `
  -RunInstallerSmoke `
  -RunInstallerFrontendSmoke
```

Result: passed.

Implemented improvements:

- Frontend UI scale is reduced slightly through the root rem size.
- YouTube thumbnails are loaded through a backend thumbnail proxy, then rendered
  from `blob:` URLs so the installed Tauri CSP can stay restrictive.
- YouTube history cards no longer show the processing spinner when a stale
  cache item still says `processing` but the backend step is already completed.
- The native Tauri window menu is no longer installed; shell actions remain in
  the tray only.
- The tray menu now includes a recent-transcripts submenu and can copy recent
  transcript contents to the Windows clipboard through the token-protected
  backend API.
- The recording popup waveform is drawn on a canvas at animation-frame cadence
  instead of using React state for every visual frame. Backend and mic
  audio-level throttles now run at about 60Hz for smoother UI motion.

Evidence:

- Focused Python gates: `73 passed`.
- Re-run thumbnail/frontend security gates: `40 passed`.
- Re-run thumbnail proxy endpoint/frontend gates after adding full route
  coverage: `42 passed`.
- Frontend TypeScript check: passed.
- Frontend production build: passed.
- Tauri Rust unit tests: `25 passed`.
- Frontend browser smoke: passed with 5 routes, 0 critical console errors, 0
  page errors, and 0 unhandled rejections.
- Python compile check for `src/web_api.py` and `src/microphone.py`: passed.
- Whitespace diff check: passed.
- Installed NSIS package smoke: passed with `runtimeMode=tauri-supervised`,
  `launchKind=sidecar`, `webViewReady=true`,
  `privateNetworkPreflight=true`,
  `webViewBackendBaseUrl=http://127.0.0.1:8765`,
  `webViewLocationOrigin=http://tauri.localhost`, `cleanupVerified=true`, and
  `uninstall.verified=true`.
- Built installer:
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.

## 2026-06-02 - Installed WebView Backend Availability Fix

Commands:

```powershell
python -m pytest `
  tests/test_frontend_type_gates.py `
  tests/test_web_api_security.py `
  tests/test_tauri_stability_smoke_gates.py

cd Frontend
npm run check
npm run build
cd ..

python -m py_compile src/web_api.py
git diff --check

powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\build_windows.ps1 `
  -SkipChecks `
  -SkipSmoke `
  -RunInstallerFrontendSmoke
```

Result: passed.

Implemented improvements:

- In Tauri runtime, `BackendStatusProvider` now treats the Rust supervisor's
  `ensure_backend_running` readiness as the primary online signal instead of
  requiring an additional browser-side `/api/health` fetch before clearing the
  offline overlay.
- If the Tauri IPC supervisor check fails, the hook logs a debug diagnostic and
  falls back to the direct HTTP health probe instead of failing the whole check.
- The hook refreshes backend access through `loadBackendBaseUrlFromTauri()` on
  health checks, so a missed initial backend URL/token lookup can recover.
- Backend CORS now returns `Access-Control-Allow-Private-Network: true` for
  allowed origins that request the Chromium/WebView private-network preflight.
- The installed package frontend smoke now verifies the private-network
  preflight in addition to the real Tauri WebView `frontend-ready` beacon.

Evidence:

- Focused Python gates: `50 passed`.
- Frontend TypeScript check: passed.
- Frontend production build: passed.
- Python compile check for `src/web_api.py`: passed.
- Whitespace diff check: passed.
- Installed NSIS package smoke: passed with `webViewReady=true`,
  `privateNetworkPreflight=true`,
  `webViewBackendBaseUrl=http://127.0.0.1:8765`, and
  `webViewLocationOrigin=http://tauri.localhost`.
- Built installer:
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.

Goal coverage:

- Phase 3/4: closes the installed Tauri shell failure mode where backend,
  tray, and hotkey could work while the React WebView stayed behind
  `Backend Not Available`.
- Phase 7: expands the installed frontend smoke so future installers validate
  the WebView-to-backend readiness path, CORS origin, and Chromium
  private-network preflight.

## 2026-06-02 - Typed Legacy WebSocket Hook Gate

Commands:

```powershell
python -m pytest tests/test_frontend_type_gates.py

cd Frontend
npm run check
npm run build
cd ..

git diff --check
```

Result: passed.

Implemented improvements:

- Exported the shared `isScriberWebSocketMessage()` guard from
  `Frontend/client/src/contexts/WebSocketContext.tsx`.
- Updated the legacy `useWebSocket` hook to use `ScriberWebSocketMessage`,
  parse incoming JSON as `unknown`, guard it through the shared WebSocket
  contract, and send `unknown` payloads instead of `any`.
- Added a static regression test so this hook cannot silently reintroduce the
  untyped `data: any` boundary.

Goal coverage:

- Phase 1: improves the frontend typed API/WebSocket boundary and removes an
  old `any` message path from code compiled by the React client.
- Phase 7: adds a focused guard for the legacy hook while the shared
  `WebSocketProvider` remains the primary runtime path.

## 2026-06-02 - Versioned Frontend-Ready Request Contract

Commands:

```powershell
python -m pytest `
  tests/contract/test_rest_contracts.py `
  tests/test_web_api_security.py

cd Frontend
npm run check
npm run build
cd ..

python -m py_compile src/core/rest_contracts.py src/web_api.py
git diff --check
```

Result: passed.

Implemented improvements:

- `POST /api/runtime/frontend-ready` now requires `apiVersion: "1"` in the
  request body instead of accepting an unversioned JSON object.
- Added `validate_frontend_ready_request_payload()` to the REST contract gate.
- Added typed frontend request/response shapes in
  `Frontend/client/src/lib/api-types.ts`.
- `reportFrontendReady()` now sends a typed/versioned request and only marks
  the beacon as reported when the backend responds with the expected API
  version and `ready=true`.

Evidence:

- Focused REST contract/security tests: `41 passed`.
- Frontend TypeScript check: passed.
- Frontend production build: passed.
- Python compile check for touched backend modules: passed.
- Whitespace diff check: passed.

Goal coverage:

- Phase 1: strengthens the contract-first boundary for the new WebView
  readiness endpoint and removes the last unversioned frontend-ready request
  payload.
- Phase 3/7: keeps the installed WebView startup gate typed on the React side
  and contract-validated on the backend side.

## 2026-06-02 - Installed Tauri WebView Backend Ready Gate

Commands:

```powershell
python -m pytest `
  tests/contract/test_rest_contracts.py `
  tests/test_web_api_security.py `
  tests/test_tauri_stability_smoke_gates.py

cd Frontend
npm run check
npm run build
cd ..

python -m py_compile src/core/rest_contracts.py src/web_api.py

powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -SkipChecks `
  -SkipSmoke `
  -RunInstallerSmoke `
  -RunInstallerFrontendSmoke
```

Result: passed.

Implemented improvements:

- Added token-protected `GET/POST /api/runtime/frontend-ready`.
- React now posts the readiness beacon after a successful backend health check.
  The payload is non-secret and records Tauri runtime detection, runtime
  backend URL, WebView origin, request origin, and timestamp.
- `scripts\smoke_tauri_desktop.ps1 -VerifyFrontend` now waits for that beacon
  and verifies the reported backend URL plus `http://tauri.localhost` origin.
- The installer frontend smoke now proves the actual Tauri WebView can resolve
  the runtime backend URL and reach the token-protected backend API, not only
  that static assets and CORS are reachable from a PowerShell HTTP client.

Evidence:

- Focused contract/security/smoke-gate tests: `53 passed`.
- Frontend TypeScript check: passed.
- Frontend production build: passed.
- Python compile check for touched backend contract/API modules: passed.
- PowerShell parser checks for desktop and installer smoke scripts: passed.
- Fresh NSIS build artifact:
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Installer size: `205.75` MiB, under the default `220` MiB gate.
- Installed frontend smoke:
  - root URL `http://127.0.0.1:8765/`, HTTP `200`;
  - `6` of `6` referenced JS/CSS assets verified;
  - Tauri-origin CORS verified for `/api/health`;
  - tokenized `/api/runtime` CORS verified;
  - `webViewReady=true`;
  - `webViewTauriRuntime=true`;
  - `webViewBackendBaseUrl=http://127.0.0.1:8765`;
  - `webViewLocationOrigin=http://tauri.localhost`;
  - smoke cleanup verified;
  - silent uninstall verified with runtime data preserved.

Goal coverage:

- Phase 1: adds a versioned REST contract for frontend WebView readiness.
- Phase 3: closes the practical Tauri production frontend gap that previously
  could show "Backend Not Available" even while the backend/hotkey path worked.
- Phase 7: strengthens installed-package smoke evidence from static asset/CORS
  checks to a real WebView-to-backend token path.
- Phase 8: keeps the Windows installer frontend startup gate repeatable.

Remaining limits:

- This does not close Authenticode signing, real updater publication,
  microphone hardware matrix, or long live-recording stability gates.
- Installed app size is still dominated by bundled ffmpeg/ffprobe and remains a
  separate packaging-size optimization target.

## 2026-06-02 - Recording Hot-Path Audio Diagnostics Evidence

Commands:

```powershell
python -m pytest -p no:cacheprovider -p no:langsmith `
  tests\contract\test_rest_contracts.py `
  tests\test_web_api_lifecycle.py::test_runtime_and_health_contract_include_sidecar_fields `
  tests\perf\test_recording_hot_path_baseline_script.py `
  -q

python -m py_compile `
  src\core\rest_contracts.py `
  src\web_api.py `
  scripts\measure_recording_hot_path_baseline.py

python scripts\measure_recording_hot_path_baseline.py `
  --validate-only `
  --output tmp\hybrid-baseline\recording-hot-path-validate-audio-diagnostics-20260602.json
```

Result: passed.

Implemented improvements:

- Added token-protected `GET /api/runtime/audio-diagnostics`.
- The endpoint reports non-secret live-recording readiness data:
  effective/requested audio engine flags, configured and active provider,
  microphone selection, idle prewarm state, text-injection method/disable flag,
  paste timing settings, and importability for SciPy, pyloudnorm, ONNXRuntime,
  Pipecat frames, Pipecat VAD, Silero VAD, SmartTurn, and UserIdleProcessor.
- `scripts\measure_recording_hot_path_baseline.py` now writes that
  `audioDiagnostics` snapshot into each benchmark artifact before driving the
  live-mic start/stop sample.
- Added REST-contract validation for the new diagnostic payload and regression
  coverage that keeps the benchmark artifact format wired.

Evidence:

- Focused tests: `18 passed`.
- Python compile check for touched backend/script modules: passed.
- Validate-only recording-hot-path artifact contained `audioDiagnostics` with
  provider, microphone, text-injection, and runtime import entries for
  `onnxruntime` and `pipecat.audio.vad.silero`.

Goal coverage:

- Phase 0 and Phase 7: improves the evidentiary quality of the remaining
  live-recording stop-to-text gate. A failed recording run can now distinguish
  runtime dependency readiness from missing audible audio, provider transcript,
  or injection.

Remaining limits:

- This is diagnostic instrumentation, not proof of the real spoken
  stop-to-text-injection requirement. That gate still needs a live sample with
  audible microphone input, provider transcript text, and successful injection.

## 2026-06-02 - Installed Sidecar Runtime Imports and Frontend Startup

Commands:

```powershell
python -m pytest -p no:cacheprovider -p no:langsmith `
  tests\test_backend_runtime_import_check.py `
  -q

python scripts\check_backend_runtime_imports.py

powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\build_tauri_backend_sidecar.ps1 `
  -SkipFrontendBuild `
  -InstallPyInstaller `
  -BundleMediaTools `
  -CopyToTauriRelease

powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\smoke_tauri_desktop.ps1 `
  -DisableDevFallback `
  -VerifyFrontend `
  -OutputPath tmp\tauri-desktop-frontend-smoke-20260602.json

powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\build_windows.ps1 `
  -SkipChecks `
  -SkipSmoke `
  -RunInstallerSmoke `
  -RunInstallerFrontendSmoke
```

Result: passed.

Implemented improvements:

- Added explicit `onnxruntime` to `requirements-base.txt` for the standard
  cloud-provider runtime because Pipecat Silero VAD depends on the ONNXRuntime
  native runtime.
- Extended `scripts\check_backend_runtime_imports.py` so both the source
  preflight and frozen sidecar `--runtime-import-check` cover ONNXRuntime and
  `pipecat.audio.vad.silero`, not just SciPy/pyloudnorm/Pipecat frames.
- Updated `packaging\scriber-backend.spec` to bundle ONNXRuntime native
  libraries and data for Silero VAD while excluding heavy local ASR/tooling
  modules (`onnx`, `numba`, `llvmlite`, `torch`, NeMo, ONNX-ASR).
- Extended the sidecar frozen dependency path check to require
  `_internal\onnxruntime` and `_internal\onnxruntime\capi`.

Evidence:

- Focused backend runtime import tests: `8 passed`.
- Source runtime import check: `{"ok":true,"missing":[]}`.
- Frozen sidecar runtime import check:
  `build\tauri-sidecar\frozen-runtime-import-check.out` contained
  `{"ok":true,"missing":[]}`.
- Optimized sidecar output after ONNXRuntime bundling:
  `Frontend\src-tauri\target\release\backend` was `601.62` MiB and did not
  contain top-level `onnx`, `numba`, `llvmlite`, `torch`, `pandas`, or
  `pyarrow` directories.
- Tauri desktop frontend smoke:
  `runtimeMode=tauri-supervised`, `launchKind=sidecar`, backend ready on
  `127.0.0.1:8765`, frontend root HTTP `200`, all `6` referenced JS/CSS assets
  fetched successfully, cleanup verified.
- Fresh NSIS installer build and installed package smoke:
  `Scriber_0.1.0_x64-setup.exe` was `205.72` MiB, under the default `220` MiB
  installer gate; installed package started the sidecar backend, served the
  bundled frontend root and all `6` referenced JS/CSS assets, verified
  Tauri-origin CORS for `/api/health` and tokenized `/api/runtime`, and the
  silent uninstaller removed install artifacts while preserving the runtime
  data sentinel.

Goal coverage:

- Phase 6: standard Windows installer now bundles the runtime dependency that
  was missing in the packaged backend startup path.
- Phase 7: build and installed-package smoke gates now fail early if SciPy,
  pyloudnorm, ONNXRuntime, Silero VAD, Pipecat frames, or `src.web_api` are not
  importable from the packaged sidecar.

Remaining limits:

- This closes the observed packaged-backend startup/import failure. It does not
  count as the real spoken stop-to-text-injection Phase 0 sample; that still
  requires audible microphone input, provider transcript text, and successful
  text injection evidence in the same run.

## 2026-06-02 - App-Level Idle Mic Prewarm

Commands:

```powershell
venv\Scripts\python.exe -m pytest -p no:cacheprovider -p no:langsmith `
  tests\test_mic_prewarm.py `
  tests\test_device_monitor.py `
  tests\test_microphone_device_resolution.py `
  tests\test_pipeline_stop.py `
  -q

venv\Scripts\python.exe -m pytest -p no:cacheprovider -p no:langsmith `
  tests\test_web_api_lifecycle.py `
  tests\test_web_api_security.py::test_session_token_middleware_and_shutdown_endpoint `
  -q

venv\Scripts\python.exe -m py_compile `
  src\audio_devices.py `
  src\mic_prewarm.py `
  src\device_monitor.py `
  src\pipeline.py `
  src\web_api.py
```

Result: passed.

Implemented improvements:

- Added `src\mic_prewarm.py` with `MicrophonePrewarmManager`, an app-level
  idle prewarm owner for `SCRIBER_MIC_ALWAYS_ON`.
- The manager opens a discard-only PortAudio stream while the app is idle,
  releases it before live recording opens the per-session Pipecat
  `MicrophoneInput`, and resumes it after recording stops if the setting is
  still enabled.
- The manager quiesces during DeviceMonitor PortAudio refreshes so idle
  prewarm does not permanently defer hotplug/default-device updates.
- Microphone device-name/favorite resolution was moved into
  `src.audio_devices.resolve_input_microphone_device()` so pipeline and
  prewarm use the same lightweight device-selection policy without importing
  the heavy Pipecat pipeline during backend startup.
- Test setup now forces `SCRIBER_MIC_ALWAYS_ON=0` by default, and prewarm tests
  opt in explicitly, keeping local developer settings from changing test
  behavior.
- Added controller lifecycle coverage proving that `ScriberWebController`
  schedules idle mic prewarm when `MIC_ALWAYS_ON` is enabled and that
  `PUT /api/settings` toggles the idle prewarm manager without opening real
  PortAudio streams during tests.

Evidence:

- Mic prewarm/device-monitor/device-resolution/pipeline-stop focused tests:
  `36 passed`.
- Web API lifecycle/session-token focused tests after controller prewarm
  coverage: `25 passed`.
- Python compile check for touched runtime modules: passed.

Goal coverage:

- Phase 5: keeps microphone capture in Python, adds an app-level idle prewarm
  path, and preserves Python/sounddevice fallback semantics.
- Phase 8: reduces repeated live mic cold-start risk without blocking backend
  readiness or leaving per-session PortAudio streams orphaned.

Remaining limits:

- This is idle prewarming, not a rolling speech pre-buffer. If users still lose
  first words, a separate pre-buffer design remains the next audio-latency
  follow-up.

## 2026-06-02 - Release Metadata Artifact Integrity Gate

Commands:

```powershell
venv\Scripts\python.exe -m pytest `
  tests\test_tauri_updater_release_gates.py `
  -q

$tokens=$null
$errors=$null
$null=[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path 'scripts\build_windows.ps1'),
  [ref]$tokens,
  [ref]$errors
)
if ($errors.Count) {
  $errors | ForEach-Object { Write-Error $_.Message }
  exit 1
}
'OK'

python scripts\validate_tauri_updater_metadata.py `
  --metadata Frontend\src-tauri\target\release\release-metadata\latest.json `
  --allow-local-urls `
  --artifact-dir Frontend\src-tauri\target\release\bundle `
  --sha256sums Frontend\src-tauri\target\release\release-metadata\SHA256SUMS.txt
```

Result: passed.

Implemented improvements:

- `scripts\validate_tauri_updater_metadata.py` can now verify local release
  artifacts listed in `latest.json` against the built artifact file size and
  SHA256 digest.
- The validator can cross-check `SHA256SUMS.txt` against `latest.json`, so
  stale or mismatched release metadata fails before publication.
- `scripts\build_windows.ps1` now passes the local bundle root and
  `SHA256SUMS.txt` to the updater metadata validation step.
- Nested Tauri bundle directories such as `bundle\nsis\` are supported by
  resolving each manifest artifact name under the bundle root and rejecting
  ambiguous duplicate names.

Evidence:

- Focused updater release-gate tests: `8 passed`.
- PowerShell parser check for `scripts\build_windows.ps1`: passed.
- Current release metadata validation verified `1` local artifact against
  `Frontend\src-tauri\target\release\bundle` and
  `Frontend\src-tauri\target\release\release-metadata\SHA256SUMS.txt`.

Goal coverage:

- Phase 6: strengthens the Windows release/update metadata gate before real
  signing keys and publication are enabled.
- Phase 7: adds regression coverage for manifest-to-artifact integrity and
  checksum mismatch failures.

Remaining limits:

- This does not replace the external Authenticode signing step, Tauri updater
  signing keys, or a published signed `latest.json`; it makes those later
  release steps harder to mispackage.

## 2026-06-02 - Full Media Tools Sidecar Gate

Commands:

```powershell
venv\Scripts\python.exe -m pytest `
  tests\test_tauri_stability_smoke_gates.py `
  -q

$tokens=$null
$errors=$null
$null=[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path 'scripts\build_tauri_backend_sidecar.ps1'),
  [ref]$tokens,
  [ref]$errors
)
if ($errors.Count) {
  $errors | ForEach-Object { Write-Error $_.Message }
  exit 1
}
'OK'

powershell -NoProfile -ExecutionPolicy Bypass -File `
  scripts\build_tauri_backend_sidecar.ps1 `
  -SkipFrontendBuild `
  -InstallPyInstaller `
  -BundleMediaTools `
  -CopyToTauriRelease
```

Result: passed.

Implemented improvements:

- `scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools` now requires
  both `ffmpeg` and `ffprobe`.
- The script validates each copied media tool by running `-version` after the
  copy into `tools\ffmpeg\`.
- Missing or non-executable media tools now fail the sidecar build before NSIS
  packaging can produce an incomplete standard Windows build.

Evidence:

- Focused smoke-gate tests: `12 passed`.
- PowerShell parser check: passed.
- Sidecar runtime-import preflight: `ok: true`.
- Frozen sidecar runtime-import check: passed.
- Copied media tools:
  `dist\tauri-sidecar\scriber-backend\tools\ffmpeg\ffmpeg.exe` and
  `dist\tauri-sidecar\scriber-backend\tools\ffmpeg\ffprobe.exe`.
- Tauri release backend copy:
  `Frontend\src-tauri\target\release\backend`.

Goal coverage:

- Phase 6: strengthens the standard Windows sidecar packaging gate for
  ffmpeg/ffprobe while preserving bundled media-tool functionality.
- Phase 7: adds a regression test that keeps the media-tool requirement wired
  into future sidecar build changes.

## 2026-06-02 - Release Size Report Gate

Commands:

```powershell
venv\Scripts\python.exe -m pytest `
  tests\test_release_size_report.py `
  tests\test_tauri_stability_smoke_gates.py `
  -q

$scripts = @(
  'scripts\build_windows.ps1',
  'scripts\smoke_windows_installer.ps1'
)
foreach ($script in $scripts) {
  $tokens=$null; $errors=$null
  $null=[System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $script),
    [ref]$tokens,
    [ref]$errors
  )
  if ($errors.Count) {
    $errors | ForEach-Object { Write-Error "${script}: $($_.Message)" }
    exit 1
  }
}
'OK'

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -SkipChecks `
  -SkipSmoke `
  -MaxInstallerSizeMB 220

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -MaxInstalledSizeMB 1000 `
  -OutputPath tmp\hybrid-baseline\installer-size-smoke-20260602.json
```

Result: passed for the current NSIS installer artifact.

Implemented improvements:

- Added `scripts\create_release_size_report.py`.
- `scripts\build_windows.ps1` now writes
  `Frontend\src-tauri\target\release\release-metadata\size-report.json` after
  release metadata validation and fails when the largest installer artifact
  exceeds `-MaxInstallerSizeMB` (default `220` MiB).
- `scripts\smoke_windows_installer.ps1` now measures the temporary installed
  app directory and can fail with `-MaxInstalledSizeMB <mb>`.
- The release workflow already copies `release-metadata\*.json`, so the size
  report is included with CI/release artifacts.

Evidence:

- Focused tests: `15 passed`.
- PowerShell parser check for build and installer smoke scripts: passed.
- Size report artifact:
  `Frontend\src-tauri\target\release\release-metadata\size-report.json`.
- Installer artifact:
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Installer size: 206,860,594 bytes / 197.28 MiB.
- Installer budget: 220 MiB.
- Installer budget result: passed.
- Backend resource tree measured for top-file analysis:
  594,820,351 bytes / 567.26 MiB.
- Installed package smoke with a generous 1000-MiB gate: passed.
- Installed app directory size:
  608,263,965 bytes / 580.09 MiB.
- Installed package cleanup and silent uninstall: passed.
- Largest backend-resource files:
  `tools\ffmpeg\ffmpeg.exe` at 133.58 MiB and
  `tools\ffmpeg\ffprobe.exe` at 133.43 MiB.

Goal coverage:

- Phase 6: adds a repeatable packaging-size artifact to the Windows release
  path.
- Phase 8: turns the standard setup budget into an automatic hard gate and makes
  installed-size pressure visible through installer-smoke evidence.

Remaining limits:

- The setup artifact is under budget, but installed app size is currently
  580.09 MiB and still needs a deliberate reduction pass if the 450-MiB
  installed standard-app target is kept.
- ffmpeg/ffprobe dominate the current backend resource footprint; reducing that
  requires a smaller compatible media-tools bundle or another full-function
  packaging strategy. Removing ffmpeg/ffprobe from the standard Windows build
  is intentionally out of scope because the app needs that functionality.

## 2026-06-02 - Release Workflow Media Tools Prerequisite Gate

Commands:

```powershell
venv\Scripts\python.exe -m pytest -p no:cacheprovider -p no:langsmith `
  tests\test_tauri_stability_smoke_gates.py `
  tests\test_windows_authenticode_gate.py `
  tests\test_tauri_updater_release_gates.py `
  -q

venv\Scripts\python.exe -m py_compile `
  tests\test_tauri_stability_smoke_gates.py
```

Result: passed.

Implemented improvements:

- `.github\workflows\release-windows.yml` now installs `ffmpeg` through
  Chocolatey before the Windows installer build.
- The workflow verifies both `ffmpeg` and `ffprobe` with `Get-Command` and
  `-version` before Tauri invokes the sidecar build.
- `tests\test_tauri_stability_smoke_gates.py` now guards that the workflow
  keeps this media-tools prerequisite wired.

Evidence:

- Focused workflow/release-gate tests: `26 passed`.
- Python compile check for the updated workflow-gate test: passed.

Goal coverage:

- Phase 6: keeps the GitHub Windows release path aligned with the standard
  full media-tools build; CI should not attempt a standard package that cannot
  bundle `ffmpeg`/`ffprobe`.
- Phase 7: adds regression coverage so future workflow edits cannot silently
  drop the media-tools prerequisite.

Remaining limits:

- This verifies the CI prerequisite wiring, not a fresh GitHub-hosted run.
- It does not replace real Authenticode signing keys or a published signed
  updater manifest.

## 2026-06-02 - Safe Installed Live Recording 5-Minute Gate

Commands:

```powershell
venv\Scripts\python.exe -m pytest `
  tests\test_injector_methods.py `
  tests\test_config.py `
  tests\test_tauri_stability_smoke_gates.py `
  -q

$scripts = @(
  'scripts\smoke_tauri_desktop.ps1',
  'scripts\smoke_windows_installer.ps1',
  'scripts\build_windows.ps1'
)
foreach ($script in $scripts) {
  $tokens=$null; $errors=$null
  $null=[System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $script),
    [ref]$tokens,
    [ref]$errors
  )
  if ($errors.Count) {
    $errors | ForEach-Object { Write-Error "${script}: $($_.Message)" }
    exit 1
  }
}
'OK'

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -SkipChecks `
  -SkipSmoke

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -InstallDir "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\installer-smoke\live-diag-Scriber" `
  -DataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\installer-smoke\live-diag-data" `
  -LegacyDataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -VerifyLegacyDataMigration `
  -LiveRecordingDurationSec 30 `
  -DisableLiveTextInjection `
  -LiveRecordingProbeIntervalSec 5 `
  -MaxLiveBackendWorkingSetGrowthMB 100 `
  -MaxLiveCpuPercent 10 `
  -KeepInstalled `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\installer-live-recording-safe-diagnostic-20260602.json"

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1 `
  -RepoRoot "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -ExePath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\installer-smoke\live-diag-Scriber\scriber-desktop.exe" `
  -DataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\installer-smoke\live-diag-data" `
  -DisableDevFallback `
  -LiveRecordingDurationSec 300 `
  -DisableLiveTextInjection `
  -LiveRecordingProbeIntervalSec 30 `
  -MaxLiveBackendWorkingSetGrowthMB 100 `
  -MaxLiveCpuPercent 10 `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\tauri-live-recording-safe-5m-20260602.json"
```

Result: passed. The 5-minute live provider/microphone gate is accepted as
sufficient for this iteration by the user on 2026-06-02; the attempted
30-minute continuation was stopped at user request and is not claimed as
evidence.

Implemented improvements:

- `SCRIBER_DISABLE_TEXT_INJECTION=1` now disables all OS text-injection paths
  in `TextInjector` before clipboard, SendInput, or typing fallback can run.
- `Config.persist_to_env_file(...)` preserves
  `SCRIBER_DISABLE_TEXT_INJECTION`, so saving settings does not drop the flag.
- `scripts\smoke_tauri_desktop.ps1` exposes `-DisableLiveTextInjection` and
  reports `liveRecording.textInjectionDisabled`.
- `scripts\smoke_windows_installer.ps1` and `scripts\build_windows.ps1` forward
  the safe live-recording flag through installed-package release gates.
- README and AGENTS document the safe diagnostic/live-stability mode.

Evidence:

- Focused tests: `22 passed`.
- PowerShell parser check for the three release smoke/build scripts: passed.
- New installer:
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Installer timestamp: `2026-06-02 09:52:36`.
- Sidecar executable timestamp: `2026-06-02 09:47:25`.
- Sidecar runtime-import check during build: `ok: true`.
- 30-second installed diagnostic artifact:
  `tmp\hybrid-baseline\installer-live-recording-safe-diagnostic-20260602.json`.
- 5-minute installed live artifact:
  `tmp\hybrid-baseline\tauri-live-recording-safe-5m-20260602.json`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Provider: `azure_mai`.
- Microphone: favorite mic resolved to `Mikrofon (4- Insta360 Link)`.
- Text injection disabled: true.
- Duration: 300 seconds.
- Probe interval: 30 seconds.
- Sample count: 10.
- Non-recording sample count: 0.
- Backend working-set start/end/growth:
  194.71 MB / 208.23 MB / 13.52 MB.
- Backend working-set growth was below the 100 MB gate.
- Combined app+backend CPU max/avg:
  0.90% / 0.55% below the 10% live gate.
- Cleanup verified: true.

Goal coverage:

- Phase 6/7: verifies the rebuilt installed Tauri sidecar package can run a
  live microphone/provider recording gate without development fallback.
- Phase 8: adds a safe live-provider stability run that cannot write
  transcribed text into the active desktop app.
- The 5-minute live run replaces the previously open live-provider proof for
  this iteration by explicit user acceptance.

Remaining limits:

- This is not a 30-minute live-provider soak. Longer live soaks are optional
  follow-up, not claimed here.
- Physical USB/Bluetooth/dock/default-mic hardware matrix, physical hotkey
  dispatch, Authenticode signing, and published signed updater metadata remain
  separate open items.

## 2026-06-02 - Installed CSP Package, Legacy Data, Support Bundle, Upgrade, Uninstall

Commands:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -SkipChecks `
  -SkipSmoke

venv\Scripts\python.exe -m pytest tests\test_tauri_stability_smoke_gates.py -q

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -VerifySupportBundle `
  -LegacyDataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -VerifyLegacyDataMigration `
  -SimulateUpgrade `
  -VerifyUninstall `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\installer-csp-support-legacy-upgrade-uninstall-20260602.json"
```

Result: passed for the installed NSIS package.

Implemented improvements:

- `scripts\smoke_tauri_desktop.ps1` now restores the original runtime `.env`
  and `settings.json` after the support-bundle redaction check.
- This allows `-VerifySupportBundle`, `-VerifyLegacyDataMigration`, and
  `-SimulateUpgrade` to run together against the same installed runtime data
  directory without contaminating the second migration check with dummy secrets.
- `tests\test_tauri_stability_smoke_gates.py` now asserts that the
  support-bundle gate snapshots and restores runtime config files.

Evidence:

- New installer:
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Installer timestamp: `2026-06-02 09:16:38`.
- Installer size: 206,880,108 bytes.
- Sidecar runtime-import check during build: `ok: true`.
- Focused smoke-gate tests: `10 passed`.
- Installed smoke artifact:
  `tmp\hybrid-baseline\installer-csp-support-legacy-upgrade-uninstall-20260602.json`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Legacy source:
  `C:\Users\Alexander.Immler\Documents\Github\Scriber`.
- Legacy files verified:
  `.env` hash matched, `settings.json` hash matched,
  `transcripts.db` copied with 24,276,992 bytes.
- Upgrade verified: true.
- Upgrade sentinel preserved: true.
- Support bundle verified on first and second smoke: true.
- Support bundle unauthorized status: 401.
- Support bundle redaction verified: true.
- Support bundle ZIP entry count: 11.
- Cleanup verified: true.
- Strict uninstall verified: true.
- Installed app artifacts removed: true.
- Runtime data preservation during uninstall verified before final temp cleanup.

Goal coverage:

- Phase 2/3: verifies the hardened Tauri WebView build inside the real NSIS
  package, not only a raw release executable.
- Phase 2/6: verifies the packaged sidecar starts without development fallback
  and uses the token-protected support-bundle endpoint.
- Phase 6: verifies first-run migration from the legacy source checkout data
  and preservation across an installer rerun.
- Phase 8: keeps the release smoke composable by making the support-bundle
  redaction test restore runtime config files.

Remaining limits:

- This is an unsigned local build; real Authenticode signing and public updater
  metadata remain separate release gates.
- Physical USB/Bluetooth/dock/default-mic hardware matrix and long live
  provider/hardware runs remain open.

## 2026-06-02 - Tauri CSP Hardening

Commands:

```powershell
venv\Scripts\python.exe -m pytest tests\test_tauri_security_gates.py

cd Frontend
npm run check
npm run build
npm run tauri:build -- --no-bundle

cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1 `
  -ExePath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\scriber-desktop.exe" `
  -DataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\tauri-smoke-data\csp-hardened-20260602" `
  -DisableDevFallback `
  -StabilityDurationSec 3 `
  -StabilityProbeIntervalSec 1 `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\tauri-csp-hardened-20260602.json"
```

Result: passed.

Implemented improvements:

- `Frontend\src-tauri\tauri.conf.json` now defines a restrictive WebView CSP
  instead of `csp: null`.
- The CSP keeps scripts local, disallows `unsafe-eval`, blocks object/embed
  content, blocks form submission and framing, and restricts network access to
  the app plus loopback HTTP/WebSocket backend URLs.
- External Google Fonts links were removed from `Frontend\client\index.html`.
- Font tokens in `Frontend\client\src\index.css` now use system font stacks, so
  the desktop app no longer depends on external font CSS.
- `tests\test_tauri_security_gates.py` now asserts the CSP directives and
  checks that the frontend entrypoint stays compatible with that CSP.

Evidence:

- Tauri security gates: `6 passed`.
- TypeScript strict check: passed.
- Production frontend/server build: passed.
- Tauri build without bundling: passed.
- Built executable:
  `Frontend\src-tauri\target\release\scriber-desktop.exe`.
- Runtime smoke artifact:
  `tmp\hybrid-baseline\tauri-csp-hardened-20260602.json`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Stability verified: true.
- Samples: 3.
- Backend working-set peak growth: 0.06 MB.
- Combined CPU max/avg: 0% / 0%.
- Cleanup verified: true.

Goal coverage:

- Architecture boundary: strengthens the Tauri security surface while keeping
  REST/WebSocket over localhost.
- Phase 2/3: keeps the Tauri WebView constrained to local assets and the
  runtime backend URL model.
- Phase 8: hardens the desktop shell and removes an external runtime
  dependency from the packaged UI.

Remaining limits:

- This is CSP/build/startup evidence, not a full installed NSIS smoke with the
  newly built executable.
- Signing/updater publication, physical hardware matrix, and long live
  provider runs remain separate open items.

## 2026-06-02 - Runtime Support Bundle Gate

Commands:

```powershell
$scripts = @(
  'scripts\smoke_tauri_desktop.ps1',
  'scripts\smoke_windows_installer.ps1',
  'scripts\build_windows.ps1'
)
foreach ($script in $scripts) {
  $tokens=$null; $errors=$null
  $null=[System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $script),
    [ref]$tokens,
    [ref]$errors
  )
  if ($errors.Count) {
    $errors | ForEach-Object { Write-Error "${script}: $($_.Message)" }
    exit 1
  }
}
'OK'

venv\Scripts\python.exe -m pytest `
  tests\test_tauri_stability_smoke_gates.py `
  tests\runtime\test_support_bundle.py `
  tests\test_web_api_security.py::test_session_token_middleware_and_shutdown_endpoint

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1 `
  -ExePath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\scriber-desktop.exe" `
  -DataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\tauri-smoke-data\support-bundle-20260602-pass" `
  -DisableDevFallback `
  -VerifySupportBundle `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\tauri-support-bundle-20260602.json"
```

Result: passed for the Tauri-supervised sidecar runtime.

Implemented improvements:

- `scripts\smoke_tauri_desktop.ps1` now supports `-VerifySupportBundle`.
- The gate writes dummy secrets into runtime `.env`, `settings.json`, and a log
  file, calls the real `POST /api/runtime/support-bundle` endpoint, and
  inspects the returned ZIP.
- The gate verifies that unauthenticated support-bundle requests return 401
  when `SCRIBER_SESSION_TOKEN` is configured.
- The gate verifies required ZIP entries, non-empty download bytes, redaction
  markers, and absence of the dummy secrets plus the real session token.
- `scripts\smoke_windows_installer.ps1` forwards `-VerifySupportBundle`.
- `scripts\build_windows.ps1` now exposes `-RunInstallerSupportBundleSmoke` as
  a release build gate.

Evidence:

- PowerShell parser check for all three scripts: passed.
- Focused tests: `13 passed`.
- Runtime artifact:
  `tmp\hybrid-baseline\tauri-support-bundle-20260602.json`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Cleanup verified: true.
- Support bundle verified: true.
- Token protection verified: true.
- Unauthorized status: 401.
- Redaction verified: true.
- Download size: 6,598 bytes.
- ZIP entry count: 11.
- Required entries included:
  `manifest.json`, `runtime.json`, `state.redacted.json`,
  `environment.redacted.json`, `config/env.redacted.txt`,
  `config/settings.redacted.txt`, and
  `logs/support-bundle-secret-smoke.log`.

Goal coverage:

- Phase 2: adds runtime evidence for Python worker log/support diagnostics
  under the Tauri-supervised sidecar path.
- Phase 2/6: verifies the support bundle is token-protected and suitable for
  installed-app smoke forwarding.
- Phase 8: turns secret redaction into a release-gateable runtime check instead
  of relying only on unit tests.

Remaining limits:

- This verifies a local unsigned runtime. It does not replace real
  Authenticode signing, updater publication, or human review of a production
  support bundle from an affected user machine.

## 2026-06-02 - Text Injection Target Smoke + Clipboard Fallback

Commands:

```powershell
venv\Scripts\python.exe -m py_compile `
  src\injector.py `
  tests\test_injector_paste.py `
  scripts\smoke_text_injection_target.py `
  tests\perf\test_text_injection_smoke_script.py

venv\Scripts\python.exe -m pytest `
  tests\perf\test_text_injection_smoke_script.py `
  tests\test_injector.py `
  tests\test_injector_paste.py `
  tests\test_injector_methods.py

venv\Scripts\python.exe scripts\smoke_text_injection_target.py `
  --validate-only `
  --text "Scriber injection validate 20260602" `
  --output tmp\hybrid-baseline\text-injection-smoke-validate-20260602.json

venv\Scripts\python.exe scripts\smoke_text_injection_target.py `
  --method paste `
  --text "Scriber paste injection smoke foreground 20260602." `
  --timeout-sec 8 `
  --settle-sec 1.5 `
  --output tmp\hybrid-baseline\text-injection-smoke-paste-foreground-20260602.json
```

Result: implemented a standalone text-injection smoke gate and fixed a Windows
clipboard reliability gap found by that gate.

Implemented improvements:

- Added `scripts\smoke_text_injection_target.py`, which opens the existing safe
  Tk text target window, invokes the real `TextInjector._inject_text(...)`
  path, polls the target text file, and writes JSON evidence.
- The smoke classifies failures as `failed`, `callback_without_target_text`,
  `target_text_without_callback`, or `passed`, so provider/STT issues do not
  get conflated with injector/focus issues.
- The smoke now searches the target window by title, brings it to foreground,
  clicks inside it using window-relative coordinates, and records focus
  evidence in `targetFocus`.
- `src\injector.py` now falls back to Tkinter clipboard read/write when the
  fast Win32 clipboard path cannot open the clipboard in the current desktop
  session.
- Paste injection no longer aborts solely because the previous clipboard could
  not be read. It continues without restoring the unknown previous value.

Evidence:

- Focused tests: `17 passed`.
- Validate artifact:
  `tmp\hybrid-baseline\text-injection-smoke-validate-20260602.json`.
- Direct clipboard helper diagnostic after fallback:
  - `_windows_clipboard_set_text(...)`: true.
  - `_windows_clipboard_get_text(...)`: returned the fallback-set text.
- Real paste smoke artifact:
  `tmp\hybrid-baseline\text-injection-smoke-paste-foreground-20260602.json`.
- Real paste smoke result in this Codex desktop session:
  - status: `callback_without_target_text`.
  - callback verified: true.
  - target text verified: false.
  - callback elapsed: 651.928 ms.
  - target captured chars: 0.
  - clipboard read/write used the Tkinter fallback successfully.
  - target window was found by title and clicked at window-relative coordinates.

Goal coverage:

- Phase 0/7: adds a repeatable standalone gate for the stop-to-text-injection
  tail, independent of microphone routing and STT provider behavior.
- Phase 0: proves the current failure boundary is after the injector callback
  in this desktop session, not in the transcription pipeline or clipboard
  staging.
- Phase 8: improves paste robustness for blocked Win32 clipboard sessions
  without adding overhead to the normal fast path.

Remaining limits:

- This does not yet close real stop-to-text-injection. In this Codex desktop
  session, OS-level keyboard/paste events did not reach the Tk target window
  even after foreground activation and even in a direct `pyautogui.hotkey`
  diagnostic.
- A final pass still needs to run the smoke in a normal interactive foreground
  desktop target or another trusted editable app where OS input delivery is
  known to work.

## 2026-06-02 - Audible Audio Hotpath Diagnostics

Commands:

```powershell
venv\Scripts\python.exe -m py_compile `
  scripts\measure_recording_hot_path_baseline.py `
  tests\perf\test_recording_hot_path_baseline_script.py `
  tests\test_web_api_hot_path_metrics.py `
  src\web_api.py

venv\Scripts\python.exe -m pytest `
  tests\perf\test_recording_hot_path_baseline_script.py `
  tests\test_web_api_hot_path_metrics.py

venv\Scripts\python.exe scripts\measure_recording_hot_path_baseline.py `
  --validate-only `
  --output tmp\hybrid-baseline\recording-hotpath-diagnostic-validate-20260602.json

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 `
  -SkipFrontendBuild `
  -BundleMediaTools `
  -CopyToTauriRelease

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1 `
  -ExePath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\scriber-desktop.exe" `
  -DataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\tauri-smoke-data\audible-diagnostic-sidecar-20260602" `
  -DisableDevFallback `
  -StabilityDurationSec 3 `
  -StabilityProbeIntervalSec 1 `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\tauri-audible-diagnostic-sidecar-20260602.json"

# Before the live command, .env was loaded into the process environment only;
# secret values were not printed and .env was not modified.
# The live command additionally set process-local:
# $env:SCRIBER_MIC_DEVICE = 'Stereomix (Realtek HD Audio Stereo input)'
# $env:SCRIBER_FAVORITE_MIC = ''
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 1 `
  -DisableDevFallback `
  -SkipUploadExportBenchmark `
  -SkipWsBenchmark `
  -SkipHistoryScrollBenchmark `
  -RecordHotPathSamples `
  -RecordingHotPathIterations 1 `
  -RecordingHotPathSeconds 10 `
  -RecordingHotPathTimeoutSec 120 `
  -RecordingHotPathTextTargetFile "tmp\hybrid-baseline\live-hotpath-text-target-audible-diagnostic-20260602.txt" `
  -RecordingHotPathTextTargetSettleSec 1.5 `
  -RecordingHotPathSpeechPrompt "Hallo Scriber. Dies ist ein Test der Transkription fuer die Hybrid Architektur Messung." `
  -RecordingHotPathSpeechDelaySec 1.0 `
  -OutputPath "tmp\hybrid-baseline\hybrid-baseline-live-hotpath-audible-diagnostic-20260602.json"
```

Result: implemented and validated a sharper live-recording diagnostic boundary.
The previous `first_audio_frame` marker proved that frames reached the backend,
but could not distinguish silent frames from speech/audio. The hotpath now also
records `first_audible_audio_frame` when RMS reaches the existing low-mic clear
threshold.

Implemented improvements:

- `src\web_api.py` now marks `first_audible_audio_frame` only when RMS is high
  enough to clear the low-input warning threshold.
- `src\web_api.py` now marks `first_final_token` only for non-empty final
  transcript text.
- `scripts\measure_recording_hot_path_baseline.py` now classifies
  stop-to-text-injection failures as:
  - `missing_audible_audio`: frames arrived, but no audible input was observed.
  - `missing_provider_transcript`: audible input was observed, but no final
    provider transcript arrived.
  - `missing_injection_after_transcript`: provider text arrived, but no paste
    callback happened.
- The recording-hotpath JSON now includes diagnostic counts and durations for
  audible audio and provider transcript arrival.

Evidence:

- Focused tests: `17 passed`.
- Validate artifact:
  `tmp\hybrid-baseline\recording-hotpath-diagnostic-validate-20260602.json`.
- Rebuilt sidecar:
  `Frontend\src-tauri\target\release\backend\scriber-backend.exe`.
- Rebuilt sidecar size: 33,443,742 bytes.
- Rebuilt sidecar timestamp: 2026-06-02 05:24:24 +02:00.
- Tauri sidecar smoke artifact:
  `tmp\hybrid-baseline\tauri-audible-diagnostic-sidecar-20260602.json`.
- Tauri sidecar smoke:
  - Runtime mode: `tauri-supervised`.
  - Launch kind: `sidecar`.
  - Stability verified: true.
  - Samples: 3.
  - Backend working-set peak growth: 0.05 MB.
  - Combined CPU max: 0.06%.
  - Cleanup verified: true.
- Live diagnostic baseline artifact:
  `tmp\hybrid-baseline\hybrid-baseline-live-hotpath-audible-diagnostic-20260602.json`.
- Live diagnostic recording child artifact:
  `tmp\hybrid-baseline\hybrid-baseline-live-hotpath-audible-diagnostic-20260602-recording-hot-path-1.json`.
- Live diagnostic result:
  - Runtime mode: `tauri-supervised`.
  - Launch kind: `sidecar`.
  - UI visible: 1,931.81 ms.
  - Backend ready: 2,736.33 ms.
  - Hotkey/API start to mic ready: 760.646 ms.
  - Hotkey/API start to first audio frame: 777.673 ms.
  - Stop-to-text-injection status: `missing_audible_audio`.
  - Audible audio samples: 0.
  - Provider transcript samples: 0.
  - Target window captured chars: 0.

Goal coverage:

- Phase 0: separates "backend received audio frames" from "backend received
  audible speech/audio" for hotkey-to-audio analysis.
- Phase 0: makes the remaining stop-to-text-injection gap actionable by showing
  whether the next failed run is input routing, provider STT, or injector/focus.
- Phase 2/3: verifies the rebuilt Tauri-supervised sidecar still starts without
  dev fallback after the instrumentation change.

Remaining limits:

- Stop-to-text-injection remains incomplete. The latest Stereomix/TTS attempt
  did not prove audible input reached the backend, so it cannot prove provider
  or injector behavior.
- A real spoken live sample or a verified loopback route with audible RMS is
  still required to close Phase 0 stop-to-text-injection.
- This is not the 30-minute live microphone/STT provider stability pass.
- Physical global-hotkey dispatch, USB/Bluetooth/dock/default-mic hardware
  matrix, real signing, and updater publication remain open.

## 2026-06-02 - Live Hotpath Artifact Persistence + Loopback Attempts

Commands:

```powershell
$tokens=$null; $errors=$null
$null=[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path scripts\measure_hybrid_baseline.ps1),
  [ref]$tokens,
  [ref]$errors
)
if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }
'OK'

venv\Scripts\python.exe -m pytest tests\perf\test_recording_hot_path_baseline_script.py

# Before each live command, .env was loaded into the process environment only;
# secret values were not printed and .env was not modified.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 1 `
  -DisableDevFallback `
  -SkipUploadExportBenchmark `
  -SkipWsBenchmark `
  -SkipHistoryScrollBenchmark `
  -RecordHotPathSamples `
  -RecordingHotPathIterations 1 `
  -RecordingHotPathSeconds 8 `
  -RecordingHotPathTimeoutSec 90 `
  -RecordingHotPathTextTargetFile "tmp\hybrid-baseline\live-hotpath-text-target-persistent-20260602.txt" `
  -RecordingHotPathTextTargetSettleSec 1.5 `
  -RecordingHotPathSpeechPrompt "Dies ist ein kurzer Scriber Test fuer die Hybrid Architektur Messung." `
  -RecordingHotPathSpeechDelaySec 1.0 `
  -OutputPath "tmp\hybrid-baseline\hybrid-baseline-live-hotpath-persistent-20260602.json"

# Second attempt additionally set process-local:
# $env:SCRIBER_MIC_DEVICE = 'Stereomix (Realtek HD Audio Stereo input)'
# $env:SCRIBER_FAVORITE_MIC = ''
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 1 `
  -DisableDevFallback `
  -SkipUploadExportBenchmark `
  -SkipWsBenchmark `
  -SkipHistoryScrollBenchmark `
  -RecordHotPathSamples `
  -RecordingHotPathIterations 1 `
  -RecordingHotPathSeconds 12 `
  -RecordingHotPathTimeoutSec 120 `
  -RecordingHotPathTextTargetFile "tmp\hybrid-baseline\live-hotpath-text-target-stereomix-20260602.txt" `
  -RecordingHotPathTextTargetSettleSec 1.5 `
  -RecordingHotPathSpeechPrompt "Hallo Scriber. Dies ist ein Test der Transkription fuer die Hybrid Architektur Messung. Bitte schreibe diesen kurzen Satz." `
  -RecordingHotPathSpeechDelaySec 1.0 `
  -OutputPath "tmp\hybrid-baseline\hybrid-baseline-live-hotpath-stereomix-20260602.json"
```

Result: recording hotpath artifact persistence fixed and verified. Two short
real live-mic hotpath attempts measured recording startup and first audio frame
through the Tauri-supervised sidecar. Stop-to-text-injection remains open
because neither attempt produced transcribed text in the target window.

Implemented improvements:

- `scripts\measure_hybrid_baseline.ps1` now writes each recording-hotpath child
  artifact next to the main baseline JSON as
  `<baseline-name>-recording-hot-path-N.json`.
- The old path placed `recording-hot-path-N.json` inside the temporary runtime
  data directory, which was removed during cleanup while the main artifact still
  referenced it.
- `tests\perf\test_recording_hot_path_baseline_script.py` now asserts that the
  recording artifact is a persistent sibling of the main baseline output.

Persistent artifact evidence:

- Main default-mic artifact:
  `tmp\hybrid-baseline\hybrid-baseline-live-hotpath-persistent-20260602.json`.
- Recording child artifact:
  `tmp\hybrid-baseline\hybrid-baseline-live-hotpath-persistent-20260602-recording-hot-path-1.json`.
- Child artifact size: 3,193 bytes.
- Child artifact timestamp: 2026-06-02 05:02:32 +02:00.
- Default/favorite mic run:
  - Runtime mode: `tauri-supervised`.
  - Launch kind: `sidecar`.
  - UI visible: 1,679.75 ms.
  - Backend ready: 2,639.06 ms.
  - Hotkey/API start to mic ready: 340.447 ms.
  - Hotkey/API start to first audio frame: 607.519 ms.
  - Text target captured chars: 0.
  - Stop-to-text-injection status: `missing_text_injection`.
- Stereomix loopback artifact:
  `tmp\hybrid-baseline\hybrid-baseline-live-hotpath-stereomix-20260602.json`.
- Stereomix recording child artifact:
  `tmp\hybrid-baseline\hybrid-baseline-live-hotpath-stereomix-20260602-recording-hot-path-1.json`.
- Stereomix run:
  - Runtime mode: `tauri-supervised`.
  - Launch kind: `sidecar`.
  - UI visible: 2,030.19 ms.
  - Backend ready: 2,500.45 ms.
  - Hotkey/API start to mic ready: 3,450.247 ms.
  - Hotkey/API start to first audio frame: 3,459.799 ms.
  - Stop-to-text-injection status: `missing_text_injection`.

Regression evidence:

- PowerShell parser check for `scripts\measure_hybrid_baseline.ps1`: passed.
- `tests\perf\test_recording_hot_path_baseline_script.py`: `8 passed`.

Goal coverage:

- Phase 0: adds real short live-mic measurements for hotkey/API-start to
  recording readiness and first audio frame in the Tauri sidecar runtime.
- Phase 0/7: makes recording-hotpath child artifacts audit-stable instead of
  pointing at deleted temporary data directories.
- Phase 8: confirms the startup performance budget still passes during these
  short live-hotpath runs.

Remaining limits:

- Phase 0 is still incomplete for stop-to-text-injection because no provider
  transcript was injected into the target window in either attempt.
- This is not the 30-minute live microphone/STT provider stability pass.
- Physical global-hotkey dispatch, USB/Bluetooth/dock/default-mic hardware
  matrix, real signing, and updater publication remain open.

## 2026-06-02 - Manual Microphone Hardware Matrix Gate

Commands:

```powershell
venv\Scripts\python.exe -m py_compile scripts\smoke_microphone_hardware_matrix.py tests\test_microphone_hardware_matrix_smoke.py
venv\Scripts\python.exe -m pytest tests\test_microphone_hardware_matrix_smoke.py
venv\Scripts\python.exe -m pytest tests\test_device_monitor.py tests\test_microphone_device_resolution.py
venv\Scripts\python.exe scripts\smoke_microphone_hardware_matrix.py `
  --plan-only `
  --output tmp\hybrid-baseline\microphone-hardware-matrix-plan-20260602.json
```

Result: implemented and covered by focused tests. Not executed as a physical
hardware pass in this Codex desktop session because it requires operator-driven
USB/Bluetooth/dock/default-input changes.

Implemented improvements:

- Added `scripts\smoke_microphone_hardware_matrix.py`, a manual hardware matrix
  gate that uses the existing backend REST contract:
  `GET /api/microphones`, `POST /api/microphones/refresh`, and
  `GET /api/settings`.
- The script captures before/after microphone snapshots, posts refresh hints,
  polls for expected changes, and writes JSON evidence.
- Supported scenarios:
  `usb-add`, `usb-remove`, `dock-disconnect`, `dock-connect`,
  `bluetooth-add`, `bluetooth-remove`, `default-mic-change`, and
  `favorite-fallback`.
- Expectation flags cover added labels, removed labels, default input movement,
  and favorite-mic fallback.
- The gate refuses to pass a no-op run unless an explicit expectation is set and
  satisfied.

Example physical USB-add gate:

```powershell
venv\Scripts\python.exe scripts\smoke_microphone_hardware_matrix.py `
  --scenario usb-add `
  --expect-added "usb" `
  --wait-sec 60 `
  --output tmp\hybrid-baseline\microphone-hardware-usb-add.json
```

Plan artifact:

- `tmp\hybrid-baseline\microphone-hardware-matrix-plan-20260602.json`.
- Artifact size: 1,687 bytes.
- Artifact timestamp: 2026-06-02 04:53:14 +02:00.

Regression evidence:

- `tests\test_microphone_hardware_matrix_smoke.py`: `4 passed`.
- `tests\test_device_monitor.py` and
  `tests\test_microphone_device_resolution.py`: `27 passed`.

Goal coverage:

- Phase 5: creates a repeatable evidence path for USB mic add/remove,
  Bluetooth mic add/remove, dock connect/disconnect, default-mic changes, and
  favorite fallback using the authoritative backend APIs.
- Phase 7/8: converts the hardware matrix from an ad-hoc manual note into a
  JSON-producing gate with regression tests.

Remaining limits:

- The gate exists and is tested, but no physical USB/Bluetooth/dock/default-mic
  pass is claimed until it is run with real hardware.
- Physical global-hotkey dispatch, real live provider recording, and real
  signing/updater publication remain separate open items.

## 2026-06-02 - Manual Microphone Hardware Matrix Operator Plan Enrichment

Commands:

```powershell
python -m py_compile scripts\smoke_microphone_hardware_matrix.py tests\test_microphone_hardware_matrix_smoke.py
python -m pytest tests\test_microphone_hardware_matrix_smoke.py
python scripts\smoke_microphone_hardware_matrix.py `
  --plan-only `
  --output tmp\hybrid-baseline\microphone-hardware-matrix-plan-enriched-20260602.json
```

Result: passed.

Implemented improvements:

- `--plan-only` now emits a `plan` array in addition to the short
  instructions list.
- Every manual hardware scenario now includes:
  - scenario-specific expectation flags,
  - the expected evidence statement,
  - a ready-to-run example command with an output artifact path.
- Tokenized plan runs use a `<session token>` placeholder in example commands
  instead of echoing the real session token into stdout or the output JSON.

Evidence:

- `tests\test_microphone_hardware_matrix_smoke.py`: `5 passed`.
- Python compile check: passed.
- Enriched plan artifact:
  `tmp\hybrid-baseline\microphone-hardware-matrix-plan-enriched-20260602.json`.
- Artifact size: 6,597 bytes.
- Artifact timestamp: 2026-06-02 19:16:15 +02:00.

Goal coverage:

- Phase 7: makes the remaining physical microphone hardware matrix easier to
  execute consistently by turning the operator checklist into scenario-specific
  commands and expectation evidence.
- Phase 8: improves repeatability of the remaining manual gate without changing
  the Python-audio/default-device architecture.

Remaining limits:

- This still does not claim a physical USB/Bluetooth/dock/default-mic pass. The
  eight physical scenario commands must be run with real hardware before the
  manual matrix is closed.

## 2026-06-02 - Physical Microphone Hardware Matrix Aggregator Gate

Commands:

```powershell
python -m py_compile `
  scripts\smoke_microphone_hardware_matrix.py `
  scripts\validate_microphone_hardware_matrix.py `
  tests\test_microphone_hardware_matrix_smoke.py `
  tests\test_validate_microphone_hardware_matrix.py

python -m pytest `
  tests\test_microphone_hardware_matrix_smoke.py `
  tests\test_validate_microphone_hardware_matrix.py

python scripts\validate_microphone_hardware_matrix.py `
  --input-dir tmp\hybrid-baseline `
  --output tmp\hybrid-baseline\microphone-hardware-matrix-validation-current-20260602.json
```

Result: implemented and covered by focused tests. The current repository
artifact directory validation intentionally returned `ok=false` because no
physical hardware scenario artifacts are present yet.

Implemented improvements:

- Added `scripts\validate_microphone_hardware_matrix.py` as the aggregation
  gate for completed physical microphone matrix runs.
- The validator requires all selected scenario artifacts, rejects
  `planOnly=true`, rejects placeholder expectation labels, requires
  `assumeCompleted=true`, and checks scenario-specific change evidence.
- The validator writes a JSON summary with pass/fail status per scenario so a
  release run can archive a single matrix verdict.

Evidence:

- `tests\test_microphone_hardware_matrix_smoke.py` and
  `tests\test_validate_microphone_hardware_matrix.py`: `9 passed`.
- Python compile check: passed.
- Current incomplete-validation artifact:
  `tmp\hybrid-baseline\microphone-hardware-matrix-validation-current-20260602.json`.
- Current incomplete artifact size: 2,755 bytes.
- Current incomplete artifact timestamp: 2026-06-02 19:20:37 +02:00.
- Current incomplete artifact reports all eight physical scenario JSON files as
  missing, which matches the known external hardware-evidence gap.

Goal coverage:

- Phase 7: adds a hard aggregator for the manual USB/Bluetooth/dock/default
  microphone matrix instead of relying on ad-hoc inspection of individual
  files.
- Phase 8: makes the remaining physical gate auditable and prevents a plan-only
  or placeholder-label artifact from being mistaken for completed hardware
  evidence.

Remaining limits:

- This still does not close the physical matrix. It makes missing physical
  evidence explicit and gives the release process a strict final validator once
  the eight operator-driven runs are complete.

## 2026-06-02 - Live Recording Stability Gate

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

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1 `
  -ExePath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\scriber-desktop.exe" `
  -DataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\tauri-smoke-data\live-gate-script-regression-20260602" `
  -DisableDevFallback `
  -StabilityDurationSec 3 `
  -StabilityProbeIntervalSec 1 `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\tauri-live-gate-script-regression-20260602.json"
```

Result: implemented, covered by smoke-script regression tests, and verified
with a short non-recording Tauri sidecar runtime smoke. The live recording gate
itself was not executed as a microphone/provider proof in this Codex desktop
session because it starts real live mic recording and should only be run
intentionally with known microphone routing and STT provider credentials.

Implemented improvements:

- `scripts\smoke_tauri_desktop.ps1` now supports
  `-LiveRecordingDurationSec`. The gate starts `/api/live-mic/start`, waits for
  recording/listening state, samples `/api/health`, token-protected
  `/api/state`, backend memory, and app/backend CPU while recording is active,
  stops through `/api/live-mic/stop`, and verifies the app returns to idle.
- The live gate fails if the backend exits, the backend PID changes, health
  becomes invalid, the state leaves recording/listening during the sampling
  window, backend working-set peak growth exceeds
  `-MaxLiveBackendWorkingSetGrowthMB`, or average live CPU exceeds
  `-MaxLiveCpuPercent`.
- `scripts\smoke_windows_installer.ps1` forwards the live recording stability
  options to the installed-app smoke.
- `scripts\build_windows.ps1` exposes `-RunInstallerLiveRecordingSmoke` with
  duration, probe interval, start/stop timeout, memory-growth, and CPU gates.
- The regular stability sampler now records `status` and `listening` alongside
  `recordingState`, which lets the live gate fail if recording stops during the
  sampling window.

Runtime regression evidence:

- Artifact:
  `tmp\hybrid-baseline\tauri-live-gate-script-regression-20260602.json`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Short stability samples: 3.
- Backend PID: `47972`.
- Cleanup verified: true.
- `liveRecording`: null, because this was intentionally a non-recording
  runtime regression smoke.

Example installed 30-minute gate:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -LiveRecordingDurationSec 1800 `
  -DisableLiveTextInjection `
  -LiveRecordingProbeIntervalSec 30 `
  -MaxLiveBackendWorkingSetGrowthMB 100 `
  -MaxLiveCpuPercent 10 `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\installer-live-recording-30m.json"
```

Goal coverage:

- Phase 0/8: adds a concrete full-duration live recording/provider stability
  gate for the previously open long live-session requirement.
- Phase 6/7: makes the same live recording gate available against the installed
  NSIS package and as a build-script release gate.

Remaining limits:

- The gate exists and is tested, but no live microphone/provider pass is claimed
  until it is run intentionally with real microphone input and valid STT
  provider configuration.
- This does not close the physical USB/Bluetooth/dock/default-mic manual
  matrix, physical global-hotkey dispatch, or real signing/updater publication.

## 2026-06-02 - 30-Minute Installed Idle Stability Gate

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -LegacyDataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -VerifyLegacyDataMigration `
  -VerifyUninstall `
  -StabilityDurationSec 1800 `
  -StabilityProbeIntervalSec 30 `
  -MaxBackendWorkingSetGrowthMB 100 `
  -MaxIdleCpuPercent 2 `
  -OutputPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\tmp\hybrid-baseline\installer-idle-stability-30m-20260602.json"
```

Result: passed for a full 30-minute installed idle stability gate.

Evidence:

- Artifact:
  `tmp\hybrid-baseline\installer-idle-stability-30m-20260602.json`.
- Artifact size: 15,774 bytes.
- Artifact timestamp: 2026-06-02 04:37:22 +02:00.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Legacy migration verified: true.
- Silent uninstall verified: true.
- Cleanup verified: true.
- Stability verified: true.
- Duration: 1,800 seconds.
- Probe interval: 30 seconds.
- Sample count: 60.
- Backend PID stayed stable: `48496`.
- Backend working-set start/end/max:
  187.11 MB / 187.21 MB / 187.25 MB.
- Backend working-set growth: 0.10 MB.
- Backend working-set peak growth: 0.14 MB under the 100 MB gate.
- Combined app+backend idle CPU max/avg:
  0.43% / 0.04% under the 2% gate.
- Max `/api/health` latency: 455.94 ms.
- Max token-protected `/api/state` latency: 148.13 ms.
- All samples reported health ready: true.
- All samples stayed in recording state `idle`.

Goal coverage:

- Phase 6: confirms the installed NSIS package, migrated runtime data,
  packaged sidecar, cleanup, and silent uninstall path over a full-duration
  installed run.
- Phase 8: provides a real 30-minute installed-app idle CPU and memory-growth
  gate instead of only short stability smokes.
- Phase 7: persists the long-run JSON artifact for future comparison.

Remaining limits:

- This is a 30-minute installed idle run, not a 30-minute live microphone/STT
  provider session.
- It does not close the physical USB/Bluetooth/dock/default-mic manual matrix.
- It does not replace real Authenticode signing keys or a published signed
  updater manifest.

## 2026-06-02 - Manual Physical Hotkey Smoke Gate

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
```

Result: implemented and covered by smoke-script regression tests. Not executed
as a physical-dispatch proof in this Codex desktop session because it requires
a real Windows keypress while the smoke is waiting.

Implemented improvements:

- `scripts\smoke_tauri_desktop.ps1` now supports
  `-WaitForManualGlobalHotkey`. It configures the temporary runtime data dir,
  verifies Tauri registered the configured shortcut, prompts the operator to
  press the shortcut, and waits for backend state or mic transcript changes.
- Successful manual dispatch evidence is serialized with
  `globalHotkey.dispatchVerified: true` and
  `globalHotkey.dispatchMethod: manual`.
- `scripts\smoke_windows_installer.ps1` forwards
  `-WaitForManualGlobalHotkey` to the installed-app smoke.
- `scripts\build_windows.ps1` exposes
  `-RunInstallerManualGlobalHotkeySmoke` for an interactive installed-package
  release gate.

Goal coverage:

- Phase 4: adds a real physical-dispatch verification path for the Tauri-owned
  global hotkey without adding duplicate recording state in Rust.
- Phase 6/7: makes the same manual dispatch check available against the
  installed NSIS package.
- Phase 8: converts the last hotkey gap from an ad-hoc manual note into a
  repeatable smoke gate with JSON evidence.

Remaining limits:

- The gate exists and is tested, but no physical-dispatch pass is claimed until
  `-WaitForManualGlobalHotkey` is run and the operator presses the configured
  shortcut before timeout.
- Signing/updater publication and real long-duration provider/hardware runs
  remain separate open items.

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

## 2026-06-02 - NSIS Frontend/Backend Startup Smoke

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -VerifyFrontend `
  -VerifyUninstall `
  -OutputPath tmp\hybrid-baseline\installer-frontend-smoke-20260602.json
```

Result: passed.

Evidence:

- Installed package: `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Runtime mode: `tauri-supervised`.
- Launch kind: `sidecar`.
- Backend URL used for the frontend asset check: `http://127.0.0.1:8765/`.
- Frontend root returned HTTP 200 and contained the React root element.
- Frontend assets verified: 6 of 6 referenced JS/CSS assets returned HTTP 200
  and non-empty content.
- Verified assets included the app entry bundle, React vendor chunk, general
  vendor chunk, TanStack Query vendor chunk, Motion vendor chunk, and CSS bundle.
- Silent uninstall: verified.
- Installed app artifacts after uninstall: removed.
- Runtime data directory after uninstall: preserved.

Goal coverage:

- Phase 7: proves that the generated NSIS installer can install, start the app,
  reach the supervised backend, and serve the bundled frontend assets without
  relying on source-checkout Python/Node dev fallback.
- Phase 8: adds a repeatable packaged startup gate for the practical
  "installation runs, backend works, frontend works" user-facing requirement.

Remaining limits:

- This run did not exercise a full browser-rendered Tauri window, Authenticode
  signing, updater publication, microphone hardware, or long recording
  stability.

## 2026-06-02 - Full Hybrid Baseline Performance Budget Gate

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 3 `
  -DisableDevFallback `
  -FailOnPerformanceBudget `
  -OutputPath tmp\hybrid-baseline\hybrid-baseline-full-20260602.json
```

Result: passed.

Evidence:

- Runtime path: Tauri release executable with packaged sidecar.
- Iterations: 3.
- Runtime mode: `tauri-supervised`.
- UI visible P95: 1442.93 ms against the 3000 ms budget.
- Backend ready P95: 2697.19 ms against the 5000 ms budget.
- Cleanup P95: 464.89 ms; no managed backend PIDs remained after cleanup.
- Upload benchmark: 4 x 4 MiB synthetic uploads, ok.
- Export benchmark: 2 PDF and 2 DOCX exports at concurrency 2, ok.
- `/api/health` responsiveness under upload/export load: P95 14.3413 ms.
- `/api/state` responsiveness under upload/export load: P95 13.614 ms.
- WebSocket JSON serialization: P95 0.0022 ms.
- WebSocket no-client broadcast: P95 0.0002 ms with zero send calls.
- WebSocket broadcast with 1 client: P95 0.0138 ms.
- WebSocket broadcast with 5 clients: P95 0.0238 ms.
- History-scroll benchmark: 2000 items, list and grid, virtualized true,
  max visible cards 54, max frame gap 116.7 ms.
- Performance budget: complete.
- Phase 0 gate: still incomplete because live recording hot-path samples were
  not requested in this full synthetic run.

Goal coverage:

- Phase 0: provides comparable full baseline evidence for startup,
  backend-ready timing, upload/export load, WebSocket/JSON cost, and large
  history scrolling without source-checkout dev fallback.
- Phase 8: proves the current Tauri release build meets the UI-visible and
  backend-ready P95 budgets on this reference run.

Remaining limits:

- The run did not collect live recording hot-path samples, microphone hardware
  matrix evidence, Authenticode signing, updater publication, or long recording
  stability.

## 2026-06-02 - Recording Hot-Path Missing-Evidence Handling

Commands:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 1 `
  -DisableDevFallback `
  -RecordHotPathSamples `
  -RecordingHotPathIterations 1 `
  -RecordingHotPathSeconds 15 `
  -RecordingHotPathTimeoutSec 90 `
  -RecordingHotPathSpeechPrompt "Scriber baseline recording test. Please transcribe this short sentence." `
  -RecordingHotPathTextTargetFile tmp\hybrid-baseline\recording-hot-path-target-20260602.txt `
  -SkipUploadExportBenchmark `
  -SkipWsBenchmark `
  -SkipHistoryScrollBenchmark `
  -OutputPath tmp\hybrid-baseline\hybrid-baseline-recording-20260602.json
```

This first run failed before the parent baseline JSON was written because the
child recording benchmark wrote `ok=false`. The child artifact
`tmp\hybrid-baseline\hybrid-baseline-recording-20260602-recording-hot-path-1.json`
showed:

- Backend health existed briefly with runtime mode `tauri-supervised`.
- The live mic state returned to `status: Error`.
- No hot-path segments were emitted.
- `hotkey_to_recording_state`: missing.
- `hotkey_to_first_audio_frame`: missing.
- `stop_to_text_injection`: `missing_audible_audio`.

The runner was changed so recording-hot-path `ok=false` is retained as evidence
instead of aborting the parent baseline. The validation command after the change
was:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 1 `
  -Hidden `
  -DisableDevFallback `
  -RecordHotPathSamples `
  -RecordingHotPathIterations 1 `
  -RecordingHotPathSeconds 10 `
  -RecordingHotPathTimeoutSec 60 `
  -RecordingHotPathSpeechPrompt "Scriber baseline recording test. Please transcribe this short sentence." `
  -RecordingHotPathTextTargetFile tmp\hybrid-baseline\recording-hot-path-target-20260602-hidden.txt `
  -SkipUploadExportBenchmark `
  -SkipWsBenchmark `
  -SkipHistoryScrollBenchmark `
  -OutputPath tmp\hybrid-baseline\hybrid-baseline-recording-hidden-20260602.json
```

Result: passed as an incomplete-evidence baseline.

Evidence:

- Backend ready P95: 2040.24 ms.
- Cleanup verified: true.
- Child recording benchmark `ok`: false.
- Parent baseline JSON was written.
- Recording statuses:
  - `hotkey_to_recording_state`: missing.
  - `hotkey_to_first_audio_frame`: missing.
  - `stop_to_text_injection`: `missing_audible_audio`.

Goal coverage:

- Phase 0: missing recording hot-path evidence is now preserved in the baseline
  artifact instead of being lost to a thrown parent process.
- Phase 7: adds regression coverage that expected live-environment failure
  states stay reportable.

Remaining limits:

- This does not close the live recording gate. A successful run still needs a
  microphone/provider/text-injection path that emits the hot-path segments.

## 2026-06-02 - Baseline Legacy DataDir Hot-Path Guard

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -Iterations 1 `
  -Hidden `
  -DisableDevFallback `
  -LegacyDataDir . `
  -SkipUploadExportBenchmark `
  -SkipWsBenchmark `
  -SkipHistoryScrollBenchmark `
  -OutputPath tmp\hybrid-baseline\hybrid-baseline-legacydata-smoke-20260602-afterfix.json
```

Result: passed.

Evidence:

- `options.legacyDataDir` resolved to the repository path.
- Runtime mode: `tauri-supervised`.
- Backend ready P95: 2065.11 ms.
- Backend cleanup verified: true.
- Legacy hot-path segment names from the migrated DB remained visible for
  diagnostics.
- Phase 0 recording requirements stayed `not_requested` because
  `-RecordHotPathSamples` was not used:
  - `hotkey_to_recording_state`: `not_requested`.
  - `hotkey_to_first_audio_frame`: `not_requested`.
  - `stop_to_text_injection`: `not_requested`.

Goal coverage:

- Phase 0: allows baseline runs to use existing `.env`/settings/database via
  `SCRIBER_LEGACY_DATA_DIR` while preventing old persisted hot-path rows from
  being counted as current recording evidence.
- Phase 7: adds regression coverage that baseline recording gates depend on the
  explicit recording child benchmark, not stale database state.

Remaining limits:

- This run intentionally did not perform a live recording. It only proves safe
  legacy data wiring and stale-metric isolation.
