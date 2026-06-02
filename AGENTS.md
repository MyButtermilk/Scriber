# Scriber Agent Guide

Last verified: 2026-06-02

This file is the working guide for agents editing this repository. Keep it accurate when implementation status changes. Prefer the code over old docs when there is a conflict, then update the docs as part of the same task.

## Product Snapshot

- Scriber is an AI-powered transcription app with a Python backend, a React web UI, an experimental Tauri desktop shell, and a legacy Tkinter desktop UI.
- Main user workflows are live microphone dictation, YouTube transcription, file transcription, transcript management, summaries, and PDF/DOCX export.
- Backend default: `127.0.0.1:8765`, implemented with `aiohttp`, WebSocket events, SQLite, Pipecat pipeline code, and provider-specific STT adapters.
- Frontend default: `localhost:5000`, implemented with Vite 7, React 19, TypeScript, Tailwind v4, Radix/shadcn-style components, Wouter, and TanStack Query.
- Tauri shell: `Frontend/src-tauri/`, implemented with Tauri 2 and a Rust supervisor that starts or attaches to the Python backend through the `/api/health` contract.
- Runtime is primarily Windows-oriented. Linux/macOS support exists mainly for fallback paths.

## Repository Map

### Backend and Desktop Code

- `src/web_api.py`: main aiohttp controller, REST routes, WebSocket server, settings, jobs, transcript history, mic control, upload endpoints.
- `src/pipeline.py`: STT pipeline orchestration, provider factory, analyzer cache, mic device resolution, async/direct transcription helpers.
- `src/microphone.py`: `sounddevice` input transport, channel selection, RMS/visualizer callback, stream lifecycle.
- `src/audio_devices.py`: microphone normalization, host API priority, compatibility checks, deduplicated input list.
- `src/device_monitor.py`: microphone hotplug monitor, native Windows endpoint callbacks, polling fallback, PortAudio refresh guarding.
- `src/audio_file_input.py`: ffmpeg-backed file input transport for pipeline use.
- `src/config.py`: environment configuration and `.env` persistence helpers.
- `src/database.py`: SQLite transcript persistence, WAL mode, metadata loading, FTS5 search.
- `src/data/job_store.py`: persistent file/YouTube job metadata.
- `src/data/latency_metrics_store.py`: hot-path latency metrics storage.
- `src/runtime/provider_router.py`: provider routing.
- `src/runtime/retry_scheduler.py`: retry/backoff scheduling.
- `src/core/`: error taxonomy, state machine, circuit breaker, provider capabilities, REST/WebSocket contracts, hot-path tracing, logging helpers.
- `src/injector.py`: text injection via paste, SendInput, typing fallback.
- `src/youtube_api.py`: YouTube Data API search/video metadata.
- `src/youtube_download.py`: yt-dlp download and ffmpeg extraction.
- `src/summarization.py`: Gemini/OpenAI summarization.
- `src/export.py`: PDF/DOCX export.
- `src/overlay.py`: recording overlay, PySide6 preferred with Tk fallback.
- `src/tray.py`: tray lifecycle, backend/frontend process management, autostart.
- `src/main.py`, `src/ui.py`: legacy Tkinter entrypoints/UI.
- Provider modules include `mistral_stt.py`, `assemblyai_async_stt.py`, `azure_mai_stt.py`, `smallest_stt.py`, `gemini_transcribe.py`, `onnx_*`, and `nemo_*`.

### Frontend Code

- `Frontend/client/src/App.tsx`: Wouter routing and route-level lazy loading.
- `Frontend/client/src/lib/backend.ts`: backend URL resolver and session-token transport for browser/dev/Tauri runtime.
- `Frontend/client/src/hooks/use-backend-status.tsx`: backend health polling and Tauri `ensure_backend_running` integration.
- `Frontend/client/src/pages/`: Live Mic, YouTube, File Upload, Transcript Detail, Settings.
- `Frontend/client/src/contexts/WebSocketContext.tsx`: single shared WebSocket connection.
- `Frontend/client/src/components/`: app layout, command palette, recording popup, UI primitives.
- `Frontend/client/src/lib/`: API URL helpers, React Query helpers, request error handling, mock data fallback.
- `Frontend/client/src/index.css`: Tailwind v4 CSS-first design system and neumorphic classes.
- `Frontend/server/`: Express host for dev/prod frontend serving.
- `Frontend/shared/`: shared TS types and Drizzle schema.
- `Frontend/src-tauri/`: Tauri 2 desktop shell, Rust backend supervisor, capabilities, config, icon, Cargo manifest.

### Tests

- Tests live in `tests/` plus subfolders `tests/core/`, `tests/data/`, `tests/runtime/`, `tests/perf/`, and `tests/contract/`.
- Current suite has 36 `test_*.py` files.
- Important focused test areas:
  - `tests/test_device_monitor.py`
  - `tests/test_microphone_device_resolution.py`
  - `tests/test_microphone_channel_selection.py`
  - `tests/test_microphone_callback.py`
  - `tests/test_pipeline_stop.py`
  - `tests/test_web_api_security.py`
  - `tests/test_web_api_lifecycle.py`
  - `tests/test_web_api_jobs.py`
  - `tests/test_web_api_job_resume.py`
  - `tests/contract/test_ws_events.py`
  - `tests/runtime/test_provider_router.py`
  - `tests/runtime/test_retry_scheduler.py`
  - `tests/core/test_state_machine.py`
  - `tests/core/test_provider_circuit_breaker.py`
  - `tests/perf/test_hot_path_tracer.py`

### Documentation

- `README.md`: user-facing overview and setup.
- `AGENTS.md`: this agent guide.
- `frontend.md`: frontend architecture notes.
- `pipeline.md`: pipeline robustness proposals.
- `performance.md`: current performance analysis.
- `BUGS.md`, `CODE_REVIEW.md`, `AntigravityBugs.md`: bug/risk review notes.
- `docs/PRD.md`: broad product/architecture snapshot.
- `docs/Performance-Optimization-Proposals.md`: implementation status for performance roadmap.
- `docs/Mic-Performance-Enhancement.md`: mic latency and prewarming status.
- `docs/Startup-Latency-Analysis.md`: startup optimization status.
- `docs/Hybrid-Architecture-Goal.md`: authoritative Codex goal and gates for the Tauri/Rust shell + Python worker architecture.
- `docs/Hybrid-Architecture-Baseline.md`: Phase 0 baseline gate and measurement status for the Tauri/Python hybrid runtime.
- `docs/Legacy-Desktop-Fallback-Decision.md`: decision to keep legacy Tkinter/Python tray paths as maintenance-only fallback while Tauri remains the primary desktop runtime.
- `docs/PIPELINE_ARCHITECTURE.md`: live mic pipeline architecture.

## Current Implementation Status

### Live Mic and Device Handling

- `DeviceMonitor` is implemented. It uses native Windows endpoint notifications through pycaw when available and falls back to polling.
- Default DeviceMonitor polling is intentionally slow when native events are available: 60 seconds with native events, 10 seconds for polling-only fallback.
- `Frontend/client/src/hooks/use-device-change-refresh.ts` listens for browser/WebView `devicechange` events and posts `/api/microphones/refresh` as a best-effort hint. The backend still owns authoritative enumeration, PortAudio locking, and active-stream deferral through `DeviceMonitor`.
- PortAudio access is guarded by `get_device_guard_lock()` across monitor refresh, mic enumeration, and stream open/stop/close.
- PortAudio cache refresh is recording-aware. If an input stream is active, refresh is deferred and then run once after the stream becomes idle.
- `_enumerate_microphones()` runs under the shared device guard lock. Do not remove this lock; it protects against native `sounddevice`/PortAudio races.
- `_resolve_mic_device()` in `pipeline.py` caches device-name/favorite-to-index resolution for a short TTL. Default TTL is `SCRIBER_MIC_DEVICE_CACHE_TTL_SEC=10.0`.
- The mic resolution cache is invalidated when DeviceMonitor sees a device-list change and when `micDevice` or `favoriteMic` settings change.
- `MIC_ALWAYS_ON` is implemented by `src/mic_prewarm.py` as an app-level idle prewarm stream. It opens a discard-only PortAudio stream while the app is idle; live recording now first tries to adopt that warm stream by routing its callback into `MicrophoneInput`, and only falls back to closing/reopening PortAudio when the stream signature or device no longer matches.
- Per-session pipeline cleanup still calls `stop(..., close_stream=True)`; do not try to reuse a Pipecat session transport across sessions. The app-level prewarm manager owns only idle warmup, not transcription audio delivery.
- `SCRIBER_AUDIO_ENGINE=rust` is treated as requested-only until a measured Rust audio prototype exists. `/api/runtime.featureFlags.audioEngine` is the effective engine and must remain `python` while `rustAudioAvailable=false`; `requestedAudioEngine` and `rustAudioRequested` expose the opt-in request separately.
- `MicrophoneInput` still queues raw audio on every PortAudio callback. Only UI/visualizer/input-warning RMS work is throttled to about 60Hz.
- Multi-channel capture rescans strongest-channel selection every 10 callback frames and reuses the last channel between rescans.

### Pipeline and Providers

- Analyzer caching exists for expensive VAD/SmartTurn setup.
- STT provider imports are mostly lazy inside `_create_stt_service()` to keep startup lighter.
- Provider routing and circuit breaker logic exist in `src/runtime/` and `src/core/`.
- Soniox supports realtime and async modes.
- Mistral supports realtime and async modes.
- AssemblyAI Universal-3-Pro async, Azure MAI, Smallest, OpenAI, Deepgram, Gladia, Groq, Speechmatics, ElevenLabs, Google, AWS, ONNX, and NeMo paths exist, but verify exact behavior in code before changing provider contracts.
- OpenAI segmented STT requires VAD. Keep mic input mono for provider compatibility where possible.
- Adding a provider usually touches `Config.SERVICE_API_KEY_MAP`, `Config.SERVICE_LABELS`, provider capabilities/routing, settings API/UI, and `_create_stt_service()`.

### Transcript Storage and Search

- SQLite uses WAL mode and thread-local connections in `database.py`.
- Transcript list endpoints use pagination with `offset` and `limit`; `limit` is clamped to `1..100`.
- Metadata/list views avoid loading full transcript content when possible.
- FTS5 search exists for transcript search.
- `_history_by_id` is used for O(1) in-memory transcript lookups.
- `history_updated` WebSocket events are globally throttled/coalesced.

### WebSocket and Frontend Data Flow

- The frontend uses one shared WebSocket through `WebSocketProvider`.
- Backend sends events such as `state`, `status`, `transcript`, `audio_level`, `input_warning`, `transcribing`, `session_started`, `session_finished`, `history_updated`, and `error`.
- WebSocket events are versioned with `apiVersion`. Use builders and validators in `src/core/ws_contracts.py` or explicitly wrap manual payloads with `version_event_payload()`.
- `tests/contract/test_ws_events.py` is the gate for WebSocket payload compatibility. Add new event types there before broadcasting them.
- `/api/health` and `/api/runtime` payloads are versioned with `apiVersion` and validated by `src/core/rest_contracts.py`; `tests/contract/test_rest_contracts.py` is the gate for REST runtime/readiness payload compatibility.
- Frontend REST consumers should prefer shared API types from `Frontend/client/src/lib/api-types.ts`. Settings and transcript-history routes are already typed there; do not add new ad hoc `any` payload boundaries for those endpoints.
- `audio_level` is throttled around 60Hz for smoother waveform rendering.
- `broadcast()` skips JSON serialization when there are no connected WebSocket clients.
- `_on_audio_level()` avoids scheduling UI broadcast work when there are no WebSocket clients and the native overlay is not consuming waveform updates.
- Frontend routes: LiveMic is eager for first paint; YouTube, File, Settings, TranscriptDetail, and NotFound are lazy-loaded.
- Intent prefetch exists for route chunks in the layout.
- Production frontend build uses manual vendor chunks for React, TanStack Query, motion libraries, charts, and remaining vendor code.
- `tests/perf/test_frontend_vendor_chunk_config.py` guards that Vite manual vendor chunk split; update the test with any deliberate chunking change.
- Transcript history pages use infinite `/api/transcripts` pagination and shared scroll-container virtualization for large list/grid histories.

### Uploads, Jobs, and Exports

- File and YouTube jobs use persistent job metadata, retry scheduling, and resume flows.
- Upload limits are enforced with audio/video distinctions. Verify provider-specific upload limits in `web_api.py` before changing file flow.
- Multipart upload writes use a chunked helper that offloads file writes with `asyncio.to_thread`.
- Cleanup of upload/download directories and PDF/DOCX export rendering are offloaded from async request paths with `asyncio.to_thread`.
- ffmpeg/provider work can still dominate file workflows; treat it separately from the request-handler file I/O fixes.

### Hybrid Tauri Runtime

- Tauri commands exposed by `Frontend/src-tauri/src/lib.rs`: `get_backend_access`, `get_backend_base_url`, `backend_status`, `ensure_backend_running`, `restart_backend`, `get_desktop_autostart`, `set_desktop_autostart`, `global_hotkey_status`, `refresh_global_hotkey`.
- The Rust supervisor first validates the current backend port through the Scriber `/api/health` contract (`ok`, `apiVersion`, `runtimeMode`) before attaching.
- If the default backend port is unavailable, the supervisor selects a loopback port and starts a managed backend with `SCRIBER_WEB_HOST`, `SCRIBER_WEB_PORT`, `SCRIBER_RUNTIME_MODE=tauri-supervised`, `SCRIBER_BACKEND_LAUNCH_KIND`, `SCRIBER_SESSION_TOKEN`, and a writable `SCRIBER_DATA_DIR`.
- The Rust supervisor creates a random per-run `SCRIBER_SESSION_TOKEN` unless one is already provided in the environment. The token is passed only to the managed Python worker and exposed to the React UI through `get_backend_access`.
- When `SCRIBER_SESSION_TOKEN` is set, `src.web_api` requires the token for local REST and WebSocket access. `/api/health` remains public for readiness probing; `/api/runtime` reports `featureFlags.sessionTokenRequired=true`.
- The frontend appends the token as the `scriberToken` query parameter for backend REST and WebSocket URLs. Smoke/support scripts may also send `X-Scriber-Token`; browser WebSocket constructors cannot set custom headers.
- The React app posts `POST /api/runtime/frontend-ready` after a successful backend health check. The request and response are versioned with `apiVersion: "1"`. The endpoint is token-protected when `SCRIBER_SESSION_TOKEN` is set and stores only non-secret readiness evidence such as Tauri runtime detection, backend base URL, WebView origin, request origin, and timestamp. Use it to prove that the actual Tauri WebView, not just a smoke script HTTP client, resolved the runtime backend URL and reached the backend with the session token.
- The Windows Tauri shell enforces single-instance startup with a named mutex (`Local\ScriberDesktopSingleInstance`) before the backend supervisor starts. A second desktop process exits early and cannot create another managed worker.
- The Tauri app menu and tray are owned by Rust. Current shell actions are intentionally limited to opening/focusing the main window, restarting the managed backend through `BackendManager.restart()`, and quitting the app. Do not add recording state to Rust tray code; route recording actions through existing Python endpoints if they are added later.
- The Rust shell owns worker crash recovery. It runs a lightweight supervisor loop that periodically calls `ensure_backend_running()`, records managed child exits in `logs\backend-crash-metadata.jsonl`, and starts a replacement worker without depending on the React health poll.
- Windows desktop autostart is owned by Tauri in desktop runtime. The Settings UI calls `get_desktop_autostart`/`set_desktop_autostart`; browser/legacy mode still uses backend `/api/autostart`. Tauri writes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Scriber` to the current desktop executable and treats old Python-tray commands as not enabled.
- Global hotkey is owned by Tauri for managed desktop runtime. Rust reads `SCRIBER_HOTKEY`/`SCRIBER_MODE` through backend `/api/settings`, registers the shortcut with `tauri-plugin-global-shortcut`, disables Python keyboard hooks for managed workers via `SCRIBER_DISABLE_HOTKEYS=1`, and calls only existing backend endpoints (`/api/live-mic/toggle`, `/start`, `/stop`). Recording state remains exclusively in Python.
- `POST /api/runtime/shutdown` is a local-control endpoint. It requires loopback access, a configured session token, and a valid token, then signals the aiohttp server stop event for controlled worker shutdown.
- `SCRIBER_FORCE_MANAGED_BACKEND=1` is for release/smoke tests that must ignore an already-running external dev backend on `127.0.0.1:8765`.
- Backend launch priority: explicit `SCRIBER_BACKEND_EXE` only when the file name is one of the allowlisted `scriber-backend` sidecar names, then `scriber-backend` beside the Tauri executable under `backend\` or `binaries\`, then development fallback to `python -m src.web_api`.
- `src/backend_worker.py` is the standalone Python entry point for packaged backend workers. `packaging/scriber-backend.spec` and `scripts/build_tauri_backend_sidecar.ps1` build the PyInstaller onedir sidecar.
- Media tool resolution is centralized in `src/runtime/media_tools.py`: explicit tool env var, `SCRIBER_MEDIA_TOOLS_DIR`, bundled app-root folders such as `tools\ffmpeg\`, then system `PATH`.
- The sidecar spec bundles the `yt-dlp` Python package and ONNXRuntime for Pipecat Silero VAD. `scripts/build_tauri_backend_sidecar.ps1 -BundleMediaTools` requires local `ffmpeg` and `ffprobe`, copies them into the sidecar output, and validates each copied binary with `-version`.
- `Frontend/src-tauri/tauri.conf.json` enables the NSIS bundle and maps `target/release/backend/` to bundled resource path `backend/`. The supervisor also searches `app.path().resource_dir()/backend`.
- Tauri `beforeBundleCommand` runs the sidecar build with `-SkipFrontendBuild -InstallPyInstaller -BundleMediaTools -CopyToTauriRelease`, so `npm run tauri:build -- --bundles nsis` can produce an installer with the backend resource included.
- App version is centralized in `src/version.py`. Run `python scripts\sync_version.py` before release builds to sync `tauri.conf.json`, `Cargo.toml`, `Frontend/package.json`, and `Frontend/package-lock.json`. `scripts/build_windows.ps1` does this automatically.
- `scripts/check_backend_runtime_imports.py` is the sidecar preflight for startup imports such as SciPy, pyloudnorm, ONNXRuntime, Pipecat frames, Silero VAD, and `src.web_api`; `scripts/build_tauri_backend_sidecar.ps1` runs it before PyInstaller and again through the frozen `scriber-backend --runtime-import-check` so missing bundled runtime dependencies fail the build early.
- Runtime secrets and settings use the data directory: `src.runtime.paths.env_path()` resolves `.env` beside `settings.json` and `transcripts.db` under `SCRIBER_DATA_DIR`. `migrate_legacy_runtime_data()` copies missing `.env`, `settings.json`, `transcripts.db` (+ WAL/SHM), `downloads/`, and `models/` from `SCRIBER_LEGACY_DATA_DIR` or common source-checkout locations into the app-data directory without overwriting existing files.
- `PUT /api/settings` mutates live `Config` state immediately, broadcasts `settings_updated`, and debounces `.env` persistence through `SCRIBER_SETTINGS_PERSIST_DEBOUNCE_SEC` (default `0.5`). Pending writes are flushed on backend shutdown; tests in `tests/test_web_api_lifecycle.py` guard batching and shutdown flush.
- `Frontend/src-tauri/src/lib.rs` initializes `tauri-plugin-updater` and `tauri-plugin-process`; the default Tauri capability grants only app version lookup, process relaunch, update check, and download-and-install. The shell/opener plugins are intentionally not registered, and `tests/test_tauri_security_gates.py` guards this boundary.
- `Frontend/client/src/lib/desktop-updates.ts` exposes a manual Settings UI update check/install path for installed Tauri builds. It is intentionally inert until the release build is configured with a Tauri updater public key, HTTPS `latest.json` endpoint, and signing key.
- `scripts/create_release_metadata.py` writes `latest.json` and `SHA256SUMS.txt` for release artifacts. `scripts/create_release_size_report.py` writes `size-report.json`, gates the largest installer artifact with `-MaxInstallerSizeMB` from `scripts/build_windows.ps1` (default 220 MiB), and can include installed-app size/top-file data when an install directory is provided. `scripts\validate_windows_authenticode.ps1` validates Authenticode status, expected publisher, and optional timestamp after an external signing step. `scripts/validate_tauri_updater_metadata.py` validates the Tauri updater manifest shape, verifies local artifact size/checksum against `SHA256SUMS.txt` during Windows builds, and can require non-empty signatures. `scripts/prepare_tauri_updater_config.py` enables `createUpdaterArtifacts` and injects the updater public key/endpoints for signed release builds.
- `.github/workflows/release-windows.yml` builds the Windows NSIS artifact on `workflow_dispatch` or `v*` tags and uploads/publishes release files. It installs and verifies `ffmpeg`/`ffprobe` before the build so the standard sidecar can always bundle media tools. If `SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE=1`, it passes the Authenticode validation gate through `scripts/build_windows.ps1`; optional `SCRIBER_AUTHENTICODE_PUBLISHER` and `SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP=1` make the gate stricter. If `SCRIBER_TAURI_UPDATER_PUBLIC_KEY` and `TAURI_SIGNING_PRIVATE_KEY` are present, the workflow enables Tauri updater artifacts and signature-required metadata validation.
- The current sidecar spec is the standard cloud-provider build. It includes ONNXRuntime only for Silero VAD and excludes heavy local ASR/tooling stacks (`onnx`, `numba`, `llvmlite`, `torch`, NeMo, ONNX-ASR). Treat local ASR packaging as a separate optional package path.
- Managed backend stdout/stderr go to `logs\tauri-backend.log` under `SCRIBER_DATA_DIR`.
- Rust shell lifecycle logs go to `logs\tauri-shell.log`; managed backend exits are appended to `logs\backend-crash-metadata.jsonl`.
- `POST /api/runtime/support-bundle` creates a redacted diagnostic ZIP under `support-bundles\` in `SCRIBER_DATA_DIR`. It includes runtime/state metadata, selected logs, redacted settings/env data, and must not contain API keys or session tokens.
- `scripts/smoke_tauri_desktop.ps1 -VerifySupportBundle` temporarily injects dummy secrets into runtime `.env`, `settings.json`, and logs to verify redaction. It must restore `.env` and `settings.json` before returning so support-bundle checks can be combined with legacy migration and upgrade smokes.
- `scripts/smoke_tauri_desktop.ps1 -VerifyFrontend` fetches the frontend entrypoint and referenced JS/CSS assets from the running backend static fallback, verifies Tauri-origin CORS for `/api/health` plus tokenized `/api/runtime`, and waits for the actual Tauri WebView to report `/api/runtime/frontend-ready` with the expected backend URL and `http://tauri.localhost` origin. Use it when validating that the installed app starts with both backend and bundled frontend available.
- On Windows, the managed Python child is spawned with `CREATE_NO_WINDOW`.
- Managed backend startup has a timeout and will be restarted by `ensure_backend_running` instead of staying in `starting` forever.
- `GET /api/runtime/audio-diagnostics` is a token-protected diagnostic endpoint for live-recording evidence. It reports effective/requested audio engine flags, configured/active live provider, microphone selection, text-injection settings, and importability for runtime modules such as ONNXRuntime and Pipecat Silero VAD without exposing API keys.
- `scripts/measure_hybrid_baseline.ps1` is the Phase 0 baseline runner. It measures Tauri startup/backend readiness, checks cleanup, pulls available `/api/metrics/hot-path` segments, can opt into live recording samples with `-RecordHotPathSamples`, and can pass existing runtime data into the temporary managed worker with `-LegacyDataDir path\to\old\Scriber` so `.env`/settings/DB migration is exercised without printing secrets. It runs `scripts/measure_upload_export_baseline.py` for synthetic upload/export load plus `/api/health` and `/api/state` responsiveness under that load, runs `scripts/measure_ws_broadcast_baseline.py` for WebSocket/JSON costs, runs `scripts/measure_history_scroll_baseline.py` for synthetic browser history-scroll behavior, writes JSON to `tmp\hybrid-baseline\`, and leaves the gate incomplete when real recording text-injection timing is missing. Recording child artifacts from `scripts/measure_recording_hot_path_baseline.py` include an `audioDiagnostics` snapshot from `/api/runtime/audio-diagnostics` so missing text evidence can be classified against audio runtime readiness, configured provider, microphone selection, and text-injection settings. `stop_to_text_injection` is measured from `stop_requested_to_first_paste_ms`; for realtime providers that injected text before stop, `first_paste_to_stop_requested_ms` is counted as `0 ms` stop-to-text wait. When `-RecordingHotPathTextTargetFile` is set, child artifacts include `summary.textTarget`; add `-RequireRecordingHotPathTextTarget` to make the Phase 0 gate fail unless non-empty text is persisted in that controlled target. The JSON also includes a Phase 8 `performanceBudget`; `-FailOnPerformanceBudget` fails unless UI-visible P95 <= `-MaxUiVisibleP95Ms` (default 3000) and backend-ready P95 <= `-MaxBackendReadyP95Ms` (default 5000).
- `scripts/check_transcript_buffer_growth.py` is the Phase 8 synthetic transcript string-growth guard. Its default shape appends one final segment per second for a 30-minute live session and fails if metadata reads materialize the full transcript string before content is explicitly requested.
- `scripts/smoke_microphone_hardware_matrix.py` is the manual hardware matrix gate. It uses `GET /api/microphones`, `POST /api/microphones/refresh`, and `GET /api/settings` to capture before/after JSON evidence for USB mic add/remove, Bluetooth mic add/remove, dock connect/disconnect, Windows default input changes, and favorite-mic fallback. Use `--plan-only` for the operator checklist; physical runs should pass scenario-specific expectation flags such as `--expect-added`, `--expect-removed`, `--expect-default-changed`, or `--expect-favorite-fallback`.
- `scripts/validate_microphone_hardware_matrix.py` validates completed physical matrix artifacts under `tmp\hybrid-baseline` by default. It fails on missing scenario JSON, `planOnly=true`, placeholder expectation labels, failed run artifacts, or missing change evidence. Use it after all eight physical runs before claiming the manual hardware matrix gate is closed.
- `scripts/verify_tauri_updater_publication.py` fetches the published HTTPS updater `latest.json`, validates it with signature-required Tauri updater metadata rules, compares the downloaded SHA256 with the local release metadata, and writes the publication report consumed by the final release-readiness validator.
- `scripts/validate_hybrid_release_readiness.py` is the final external-evidence aggregator for the hybrid goal. It requires a passing physical microphone matrix, signed absolute-HTTPS Tauri updater metadata, a publication report for the fetched `latest.json`, and an Authenticode validation JSON report. It should remain red for unsigned local builds and is the last check before claiming the hardware/signing/updater release gates are closed.
- `scripts/smoke_frontend_browser.py` is the frontend browser smoke gate. It starts Vite with a synthetic aiohttp backend, drives Chrome/Edge through CDP, visits `/`, `/youtube`, `/file`, `/settings`, and `/transcript/mic-00001`, verifies expected route text, checks history virtualization on list routes, and fails on critical console/page errors.
- `scripts/smoke_tauri_desktop.ps1` is the Windows release smoke test for the hybrid runtime. It starts the Tauri executable with a random session token, verifies the managed `tauri-supervised` backend, hard-stops Tauri, and asserts that the newly spawned backend process exits, waiting up to `-CleanupTimeoutSec` for Windows job-object cleanup. With `-VerifyFrontend`, it fetches the bundled frontend entrypoint plus referenced JS/CSS assets through the running backend, verifies Tauri-origin CORS for `/api/health` plus tokenized `/api/runtime`, and waits for the WebView readiness beacon. With `-SimulateBackendCrash`, it kills the managed worker, waits for `ensure_backend_running` recovery, and verifies `backend-crash-metadata.jsonl`. With `-OccupyDefaultPort`, it binds `127.0.0.1:8765` before launch and verifies dynamic backend-port selection. With `-SimulateBackendShutdown`, it calls the token-protected `/api/runtime/shutdown` endpoint, waits for the worker to exit, and verifies supervisor recovery. With `-AttachExternalBackend`, it starts an external Python backend on the default port, starts Tauri without force-managed mode, and verifies that no managed sidecar is spawned. With `-SimulateBackendStartupTimeout`, it forces the first backend launch to block before readiness and verifies that the supervisor replaces it. With `-StabilityDurationSec <seconds>`, it repeatedly probes `/api/health` and token-protected `/api/state`, verifies backend PID stability, and captures backend working-set plus normalized Tauri/backend CPU samples; add `-MaxBackendWorkingSetGrowthMB <mb>` to fail on excessive peak working-set growth and `-MaxIdleCpuPercent <percent>` to fail on excessive average idle CPU. With `-LiveRecordingDurationSec <seconds>`, it explicitly starts `/api/live-mic/start`, requires recording/listening state, samples health/state plus CPU/memory while recording, stops via `/api/live-mic/stop`, and verifies idle state afterward. With `-LegacyDataDir <old-scriber-dir> -VerifyLegacyDataMigration`, it verifies first-run migration into `SCRIBER_DATA_DIR` without printing secret values. With `-VerifyGlobalHotkeyRegistration`, it checks the Tauri shell log for configured shortcut registration after backend readiness; `-SimulateGlobalHotkey` attempts synthetic OS shortcut dispatch, and `-WaitForManualGlobalHotkey` waits for a physical shortcut press and records successful evidence as `globalHotkey.dispatchMethod: manual`.
- `scripts/smoke_windows_installer.ps1` installs the generated NSIS setup into `tmp\installer-smoke\`, runs the desktop smoke without `SCRIBER_REPO_ROOT`/`SCRIBER_PYTHON` dev fallback, and removes the temporary install/data directories afterward. Pass `-VerifyFrontend` to assert that the installed backend can serve the bundled frontend entrypoint and JS/CSS assets, accepts Tauri-origin browser requests, and receives the WebView readiness beacon. Pass `-MaxInstalledSizeMB <mb>` to measure the real install directory and fail if it exceeds the configured size budget. Pass `-SimulateBackendCrash` to run the installed-package worker-crash recovery gate. Pass `-OccupyDefaultPort` to verify the installed supervisor avoids the occupied default backend port. Pass `-SimulateBackendShutdown` to verify controlled worker shutdown and supervisor recovery in the installed package. Pass `-AttachExternalBackend` to verify that the installed Tauri shell attaches to an already-running external backend without spawning a sidecar. Pass `-SimulateBackendStartupTimeout` to verify installed worker startup-timeout recovery. Pass `-StabilityDurationSec <seconds>` to run installed health/state stability probes before cleanup; add `-MaxBackendWorkingSetGrowthMB <mb>` for memory-growth gating and `-MaxIdleCpuPercent <percent>` for average idle-CPU gating. Pass `-LiveRecordingDurationSec <seconds>` only for intentional live microphone/provider runs; it forwards the live recording stability gate to the installed app. Pass `-LegacyDataDir <old-scriber-dir> -VerifyLegacyDataMigration -SimulateUpgrade` to verify legacy runtime-data migration and data preservation across a second installer run. `-VerifySupportBundle` can be combined with that migration/upgrade gate because the desktop smoke restores runtime `.env` and `settings.json` after redaction checks. Pass `-VerifyUninstall` to make the silent uninstaller a strict gate: it must remove installed app artifacts while preserving the runtime data sentinel before temp cleanup. Pass `-VerifyGlobalHotkeyRegistration` to verify installed Tauri shortcut registration after backend readiness; `-SimulateGlobalHotkey` attempts synthetic OS shortcut dispatch, and `-WaitForManualGlobalHotkey` waits for a physical shortcut press and records successful evidence as `globalHotkey.dispatchMethod: manual`.
- `scripts/build_windows.ps1 -RunInstallerSmoke` builds the NSIS package and then runs the installed-package smoke gate. The build always writes `release-metadata\size-report.json` and fails if the largest setup artifact exceeds `-MaxInstallerSizeMB` (default 220 MiB). Pass `-InstallerMaxInstalledSizeMB <mb>` with any installed-package smoke to gate the real temporary install directory. `-RequireAuthenticodeSignature` fails the build unless generated Windows release artifacts are Authenticode-valid; combine it with `-ExpectedAuthenticodePublisher` and `-RequireAuthenticodeTimestamp` after a real signing step. `-RunInstallerCrashSmoke`, `-RunInstallerPortConflictSmoke`, `-RunInstallerControlledShutdownSmoke`, `-RunInstallerExternalBackendSmoke`, `-RunInstallerStartupTimeoutSmoke`, `-RunInstallerFrontendSmoke`, `-RunInstallerStabilitySmoke`, `-RunInstallerLiveRecordingSmoke`, `-RunInstallerLegacyDataSmoke`, `-RunInstallerUpgradeSmoke`, `-RunInstallerUninstallSmoke`, `-RunInstallerGlobalHotkeyRegistrationSmoke`, `-RunInstallerGlobalHotkeySmoke`, and `-RunInstallerManualGlobalHotkeySmoke` add worker-crash, default-port-conflict, controlled-shutdown, external-backend-attach, startup-timeout, installed frontend asset, Tauri-origin CORS, WebView readiness, idle stability, live recording stability, legacy-data migration, data-preserving installer-rerun, strict silent-uninstall, installed hotkey registration, synthetic hotkey dispatch, and physical hotkey dispatch checks. `-InstallerMaxBackendWorkingSetGrowthMB <mb>` turns the installer idle stability smoke into a memory-growth gate; `-InstallerMaxIdleCpuPercent <percent>` adds an average idle-CPU gate. `-InstallerMaxLiveBackendWorkingSetGrowthMB <mb>` and `-InstallerMaxLiveCpuPercent <percent>` gate live recording runs; add `-InstallerDisableLiveTextInjection` when the live run should not write transcript text into the active desktop app.
- Current Tauri status: hybrid runtime with writable runtime-data paths, session-token protected worker API, native app menu/tray lifecycle actions, Windows single-instance guard, Windows desktop autostart commands, Tauri-owned global hotkey dispatch, redacted support bundles, WebView readiness beacon, sidecar backend launch support, bundled yt-dlp support, bundled ffmpeg/ffprobe resolution, ONNXRuntime/Silero VAD runtime import gates, NSIS installer generation, installed-package smoke coverage, updater plugin wiring, Settings UI update check/install controls, and signed-manifest gates. A fresh current-installer smoke set passed on 2026-06-02 with Tauri-origin frontend CORS, tokenized `/api/runtime`, real WebView readiness, cleanup, strict uninstall, support-bundle redaction, crash recovery, controlled worker shutdown recovery, startup-timeout recovery, default-port-conflict handling, external-backend attach, and a 205.78 MiB setup artifact under the 220 MiB gate verified. Legacy Tkinter/Python tray paths are maintenance-only fallback per `docs/Legacy-Desktop-Fallback-Decision.md`; real release updates still require configured signing keys and a published signed updater manifest.

## Commands

Run commands from the repository root unless specified.

### Backend

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python check_imports.py
python -m src.web_api
python -m src.tray
python -m src.main
```

`requirements.txt` is the full aggregate install. Use `requirements-base.txt` for the standard cloud-provider sidecar/runtime, `requirements-local-asr.txt` only when local ONNX/NeMo features are needed, `requirements-dev.txt` for tests, and `requirements-build.txt` for PyInstaller.

On Linux/macOS, activate with `source venv/bin/activate`.

### Frontend

```bash
cd Frontend
npm install
npm run dev:client
npm run dev
npm run check
npm run build
python ../scripts/smoke_frontend_browser.py --output ../tmp/frontend-browser-smoke.json
npm run tauri:dev
npm run tauri:build
npm start
npm run db:push
```

- `dev:client` and `dev` both use port `5000`; do not run them together.
- Browser/dev backend API is expected at `http://127.0.0.1:8765` unless `VITE_BACKEND_URL` is set. In Tauri, prefer the runtime backend URL from the Rust supervisor.

### Tauri Sidecar Build

```powershell
python scripts\sync_version.py
powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -InstallPyInstaller -CopyToTauriRelease
powershell -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 -BundleMediaTools -CopyToTauriRelease
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerSmoke
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerCrashSmoke
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerPortConflictSmoke
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerControlledShutdownSmoke
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerExternalBackendSmoke
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerStartupTimeoutSmoke
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerStabilitySmoke -InstallerStabilityDurationSec 30
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerStabilitySmoke -InstallerStabilityDurationSec 1800 -InstallerMaxBackendWorkingSetGrowthMB 100 -InstallerMaxIdleCpuPercent 2
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerLegacyDataSmoke -RunInstallerUpgradeSmoke
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerUninstallSmoke
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerFrontendSmoke -RunInstallerUninstallSmoke
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerGlobalHotkeyRegistrationSmoke -InstallerGlobalHotkeySmokeHotkey "ctrl+alt+shift+f12"
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerGlobalHotkeySmoke -InstallerGlobalHotkeySmokeHotkey "ctrl+alt+shift+f12"
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerManualGlobalHotkeySmoke -InstallerGlobalHotkeySmokeHotkey "ctrl+alt+shift+f12" -InstallerGlobalHotkeyDispatchTimeoutSec 30
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -RunInstallerLiveRecordingSmoke -InstallerDisableLiveTextInjection -InstallerLiveRecordingDurationSec 1800 -InstallerLiveRecordingProbeIntervalSec 30 -InstallerMaxLiveBackendWorkingSetGrowthMB 100 -InstallerMaxLiveCpuPercent 10
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -MaxInstallerSizeMB 220
powershell -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 -Iterations 3 -DisableDevFallback
powershell -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 -Iterations 1 -DisableDevFallback -RecordHotPathSamples -LegacyDataDir path\to\old\Scriber
```

Run the sidecar script before raw release smoke tests if you want the Tauri release executable to find `backend\scriber-backend.exe` automatically. For a complete NSIS installer build, prefer `scripts\build_windows.ps1`; it lets Tauri run the sidecar build before bundling. Without the sidecar, Tauri development still falls back to the repo virtualenv.

### Tests

```bash
pytest
pytest tests/test_device_monitor.py
pytest tests/test_microphone_device_resolution.py tests/test_microphone_callback.py
pytest tests/test_web_api_security.py::test_origin_allowed_defaults
pytest -k origin_allowed
```

Use targeted tests for the changed area first. Broaden when touching shared runtime, WebSocket contracts, settings, database, or provider factory code.

### Useful Verification

```bash
python -m py_compile src\microphone.py src\pipeline.py src\web_api.py
python scripts/check_transcript_buffer_growth.py
git diff --check
```

For the Windows Tauri release runtime, run after `npm run tauri:build`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -SimulateBackendCrash
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -OccupyDefaultPort
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -SimulateBackendShutdown
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -AttachExternalBackend
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -SimulateBackendStartupTimeout
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -StabilityDurationSec 30
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -StabilityDurationSec 1800 -MaxBackendWorkingSetGrowthMB 100 -MaxIdleCpuPercent 2
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -LegacyDataDir path\to\old\Scriber -VerifyLegacyDataMigration -SimulateUpgrade
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -LegacyDataDir path\to\old\Scriber -VerifyLegacyDataMigration -SimulateUpgrade -VerifySupportBundle -VerifyUninstall
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -VerifyUninstall
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -VerifyFrontend -VerifyUninstall
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -VerifyGlobalHotkeyRegistration -GlobalHotkeySmokeHotkey "ctrl+alt+shift+f12"
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -SimulateGlobalHotkey -GlobalHotkeySmokeHotkey "ctrl+alt+shift+f12"
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -WaitForManualGlobalHotkey -GlobalHotkeySmokeHotkey "ctrl+alt+shift+f12" -GlobalHotkeyDispatchTimeoutSec 30
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -LiveRecordingDurationSec 1800 -DisableLiveTextInjection -LiveRecordingProbeIntervalSec 30 -MaxLiveBackendWorkingSetGrowthMB 100 -MaxLiveCpuPercent 10
powershell -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 -MaxInstalledSizeMB 450
python scripts\smoke_microphone_hardware_matrix.py --plan-only --output tmp\hybrid-baseline\microphone-hardware-matrix-plan.json
python scripts\smoke_microphone_hardware_matrix.py --scenario usb-add --expect-added usb --wait-sec 60 --output tmp\hybrid-baseline\microphone-hardware-usb-add.json
python scripts\validate_microphone_hardware_matrix.py --input-dir tmp\hybrid-baseline --output tmp\hybrid-baseline\microphone-hardware-matrix-validation.json
python scripts\verify_tauri_updater_publication.py --url https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json --metadata Frontend\src-tauri\target\release\release-metadata\latest.json --output tmp\hybrid-baseline\updater-publication.json
python scripts\validate_hybrid_release_readiness.py --hardware-input-dir tmp\hybrid-baseline --updater-metadata Frontend\src-tauri\target\release\release-metadata\latest.json --updater-publication-report tmp\hybrid-baseline\updater-publication.json --authenticode-report path\to\authenticode.json --output tmp\hybrid-baseline\hybrid-release-readiness.json
```

For frontend changes, run:

```bash
cd Frontend
npm run check
npm run build
```

## Configuration

Never commit `.env`, `settings.json`, `transcripts.db`, `downloads/`, or temp artifacts.

Important environment variables:

- Web/API: `SCRIBER_WEB_HOST`, `SCRIBER_WEB_PORT`, `SCRIBER_ALLOWED_ORIGINS`
- Runtime storage: `SCRIBER_DATA_DIR`, `SCRIBER_DATABASE_PATH`, `SCRIBER_DOWNLOADS_DIR`
- Tauri backend worker: `SCRIBER_BACKEND_EXE`, `SCRIBER_BACKEND_DIR`, `SCRIBER_BACKEND_LAUNCH_KIND`, `SCRIBER_FORCE_MANAGED_BACKEND`, `SCRIBER_SESSION_TOKEN`, `SCRIBER_PYTHON`, `SCRIBER_TAURI_GLOBAL_HOTKEY`
- Diagnostics: `SCRIBER_LOG_DIR`
- Media tools: `SCRIBER_MEDIA_TOOLS_DIR`, `SCRIBER_FFMPEG_PATH`, `SCRIBER_FFPROBE_PATH`, `SCRIBER_YT_DLP_PATH`
- STT provider keys: `SONIOX_API_KEY`, `MISTRAL_API_KEY`, `ASSEMBLYAI_API_KEY`, `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`, `GLADIA_API_KEY`, `GROQ_API_KEY`, `SPEECHMATICS_API_KEY`, `ELEVENLABS_API_KEY`, `GOOGLE_API_KEY`, `YOUTUBE_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`
- AWS STT: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`
- Provider/model behavior: `SCRIBER_DEFAULT_STT`, `SCRIBER_SONIOX_MODE`, `SCRIBER_SONIOX_ASYNC_MODEL`, `SCRIBER_SONIOX_RT_MODEL`, `SCRIBER_MISTRAL_RT_MODEL`, `SCRIBER_MISTRAL_ASYNC_MODEL`, `SCRIBER_OPENAI_STT_MODEL`
- App behavior: `SCRIBER_HOTKEY`, `SCRIBER_MODE`, `SCRIBER_DISABLE_HOTKEYS`, `SCRIBER_INJECT_METHOD`, `SCRIBER_DISABLE_TEXT_INJECTION`, `SCRIBER_LANGUAGE`, `SCRIBER_DEBUG`, `SCRIBER_CUSTOM_VOCAB`, `SCRIBER_SETTINGS_PERSIST_DEBOUNCE_SEC`, `SCRIBER_AUDIO_ENGINE`
- Mic: `SCRIBER_MIC_DEVICE`, `SCRIBER_FAVORITE_MIC`, `SCRIBER_MIC_ALWAYS_ON`, `SCRIBER_MIC_BLOCK_SIZE`, `SCRIBER_MIC_DEVICE_CACHE_TTL_SEC`
- Upload/jobs/timeouts: `SCRIBER_UPLOAD_MAX_MB`, `SCRIBER_UPLOAD_MAX_BYTES`, `SCRIBER_JOB_MAX_ATTEMPTS`, `SCRIBER_JOB_RETRY_BASE_SEC`, `SCRIBER_JOB_RETRY_MAX_SEC`, `SCRIBER_TIMEOUT_FILE_TRANSCRIBE_SEC`, `SCRIBER_TIMEOUT_YOUTUBE_TRANSCRIBE_SEC`, `SCRIBER_TIMEOUT_YOUTUBE_DOWNLOAD_SEC`
- Summaries: `SCRIBER_SUMMARIZATION_MODEL`, `SCRIBER_AUTO_SUMMARIZE`, `SCRIBER_SUMMARY_MIN_WORDS`, `SCRIBER_SUMMARY_MAX_WORDS`
- Local models: `SCRIBER_ONNX_MODEL`, `SCRIBER_ONNX_QUANTIZATION`, `SCRIBER_ONNX_USE_GPU`, `SCRIBER_NEMO_MODEL`
- UI: `SCRIBER_VISUALIZER_BAR_COUNT`

Current summarization default is `gemini-flash-latest`.

## Coding Rules

### Python

- Python 3.10+.
- Use 4-space indentation and type hints for new public functions.
- Import order: standard library, third-party, local `src.*`, with blank lines between groups.
- Prefer built-in generics such as `list[str]` and `dict[str, Any]`.
- Prefer `pathlib.Path` for filesystem paths.
- Use `asyncio` APIs in async code. Do not block the event loop with CPU or file I/O in request handlers if avoidable.
- Use `loguru` for logging. Avoid `print`.
- Validate user/config input early. Raise `ValueError` for user-facing config issues where existing patterns do that.
- Be careful with PortAudio/sounddevice. Any new device enumeration or stream lifecycle code must respect `get_device_guard_lock()`.
- Do not describe `MIC_ALWAYS_ON` as a speech pre-buffer. It reuses the warm PortAudio stream for active capture when possible, but it does not buffer earlier speech and still depends on the live recording pipeline for transcription.

### TypeScript and React

- TypeScript strict mode is enabled. Avoid `any`; narrow API responses.
- WebSocket consumers should use `ScriberWebSocketMessage` from `Frontend/client/src/contexts/WebSocketContext.tsx`, not untyped `any` handlers.
- Components are functional and `PascalCase`; hooks are `useX`.
- Import order: external packages, aliases (`@/`, `@shared/`), then relative imports.
- Prefer `@/` alias for client imports.
- Use existing neumorphic classes and the current Tailwind v4 CSS-first setup.
- Use CSS transitions for small interactions; avoid JS animation unless needed.
- Use toast for user-visible errors.
- Keep WebSocket subscriptions through `WebSocketProvider` unless there is a clear architectural reason not to.
- For list performance, prefer pagination/infinite query/virtualization over loading unbounded lists.

## Testing Guidance by Change Type

- Device monitor or mic device selection:
  - `pytest tests/test_device_monitor.py tests/test_microphone_device_resolution.py`
- Microphone callback/channel handling:
  - `pytest tests/test_microphone_channel_selection.py tests/test_microphone_callback.py`
- Pipeline stop/lifecycle:
  - `pytest tests/test_pipeline_stop.py tests/test_web_api_lifecycle.py`
- WebSocket payload changes:
  - `pytest tests/contract/test_ws_events.py tests/test_web_api_lifecycle.py`
- Settings/security/CORS:
  - `pytest tests/test_config.py tests/test_web_api_security.py`
- Diagnostics/support bundle:
  - `pytest tests/runtime/test_support_bundle.py tests/test_runtime_paths.py tests/test_web_api_security.py`
- Job retry/resume:
  - `pytest tests/test_web_api_jobs.py tests/test_web_api_job_resume.py tests/runtime/test_retry_scheduler.py`
- Provider routing/circuit breaker:
  - `pytest tests/runtime/test_provider_router.py tests/core/test_provider_circuit_breaker.py tests/core/test_provider_capabilities.py`
- YouTube:
  - `pytest tests/test_youtube_api.py tests/test_youtube_download.py`
- Injection:
  - `pytest tests/test_injector.py tests/test_injector_methods.py tests/test_injector_paste.py`
- Summarization:
  - `pytest tests/test_summarization.py`
- Frontend:
  - `cd Frontend && npm run check`
  - `cd Frontend && npm run build`
  - `python scripts\smoke_frontend_browser.py --output tmp\frontend-browser-smoke.json`

## Known Open Engineering Work

- Optional speech pre-buffering if first-word loss remains after idle `SCRIBER_MIC_ALWAYS_ON` prewarming.
- Real recording text-injection samples during `-RecordHotPathSamples`: either `stop_requested_to_first_paste_ms` for async injection after stop or an already-injected-before-stop realtime sample counted as `0 ms` stop-to-text wait.
- Full bundled desktop release activation: actual Authenticode signing step/certificate, Tauri updater signing keys, signed update artifacts, and published `latest.json`. The Authenticode validation gate is wired, but CI still needs a real signing provider before enabling it.
- Optional longer live recording/provider soak tests. A 5-minute installed `azure_mai` + Insta360 Link live run passed with `-DisableLiveTextInjection`; a 30-minute installed idle stability gate has also passed.
- Remaining CPU-heavy media preprocessing profiling around ffmpeg/provider behavior.
- Installed app size reduction must preserve bundled media-tool functionality. The setup artifact is under the 220 MiB gate, but the current bundled backend resource tree is still dominated by full ffmpeg/ffprobe binaries; do not solve this by removing ffmpeg/ffprobe from the standard Windows build.
- More hardware regression tests for dock connect/disconnect, USB mic add/remove, and favorite mic fallback.
- Stronger typed API contract between backend and frontend across remaining REST endpoints; Settings and transcript-history consumers already use shared frontend API types.
- Splitting `web_api.py` into smaller domain modules.

## Repository Hygiene

- Do not delete or overwrite unrelated working-tree changes.
- Do not run destructive git commands unless explicitly asked.
- Keep changes scoped to the requested behavior.
- Do not commit secrets or local runtime artifacts.
- Avoid large unrelated formatting churn.
- If a file already has user changes, read around the changed area and work with it rather than reverting it.

## Documentation Rules

- Update docs when behavior or implementation status changes.
- Keep `MIC_ALWAYS_ON` docs precise: it keeps an idle prewarm stream open and can hand that stream to active capture, but it is not a reusable Pipecat transcription pipeline and not a rolling speech pre-buffer.
- Keep `README.md` user-facing.
- Keep `docs/PRD.md` broad and current.
- Keep performance docs explicit about completed, partial, and pending work.
- Keep `AGENTS.md` focused on how future agents should safely work in the repo.

## Commit and PR Notes

- Commit messages should be short and imperative, optionally prefixed with `feat:`, `fix:`, `refactor:`, or `docs:`.
- Include tests run in PR descriptions.
- For UI changes, include screenshots or at least state why screenshots were not needed.
- For behavior changes, mention affected endpoints, settings, and compatibility concerns.
