# Performance And Packaging

Last verified: 2026-07-08

This document consolidates the previous performance, startup, mic, FFmpeg,
installer-size, and optimization notes.

## Current Baseline

Latest validated local Profile B installer build from 2026-06-17:

- Installer: about `88.22 MiB`.
- Installed app: about `199.73 MiB`.
- Backend resource tree: about `185.45 MiB`.
- Installed media tools: about `4.98 MiB`.
- Profile B portable media-tool build: about `4.98 MiB` for `ffmpeg.exe`,
  `ffprobe.exe`, and required runtime DLLs.
- Previous PySide6 overlay runtime component: about `62.70 MiB`; current
  standard builds remove this runtime and render the overlay in Tauri.
- AWS SDK footprint: absent, `0.00 MiB`.
- Targeted package/provider-removal tests: `188 passed`.
- Frontend type check passed.
- Installed frontend smoke passed.
- Installed media-preparation smoke passed `5/5`.
- Installed uninstall smoke passed.
- Real installed file and YouTube workflow smoke previously passed `2/2` for
  the Profile B path with `https://www.youtube.com/watch?v=0wEjbSYNUM8`.

Compared with the 2026-06-12 Profile B baseline, the 2026-06-17 build is
about `0.2 MiB` smaller as a compressed installer, about `43 MiB` smaller
after installation, and about `42.3 MiB` smaller in the backend resource tree.

Historical comparison points:

- Full/Gyan-style media tooling produced much larger installed packages.
- Gyan Essentials remains a fallback path, not the default.
- Removing ffprobe is still an experiment because standard workflows expect
  ffprobe availability.

## Implemented Performance Work

Startup and imports:

- STT provider imports are mostly lazy in the service factory.
- Expensive VAD/analyzer setup is cached.
- Startup ML analyzer and STT provider prewarm follow Always-On-Mic by default:
  with `SCRIBER_MIC_ALWAYS_ON=1`, Silero VAD/SmartTurn analyzer setup and the
  selected STT provider import are warmed during startup so the hotkey path does
  not pay that cost. `SCRIBER_PREWARM_MODELS_ON_STARTUP` and
  `SCRIBER_PREWARM_STT_ON_STARTUP` remain manual opt-in switches when
  Always-On-Mic is disabled.
- In Tauri-supervised runtime, Rust owns global hotkeys; the Python `keyboard`
  hook and polling fallback are skipped unless
  `SCRIBER_ENABLE_PYTHON_HOTKEYS_IN_TAURI=1`.
- Runtime import diagnostics are separated from normal readiness paths where
  possible.

Live mic:

- Rust/WASAPI sidecar capture is now the standard live-mic capture path.
- Always-On-Mic uses the Rust prewarm manager and can prepend adopted sidecar
  prebuffer frames when recording starts.
- Async/finalizing live mic providers run Pipecat Silero VAD in the input
  pipeline and can skip provider upload/finalization when no speech was
  detected and the RMS silence gate also stayed quiet.
- Device-name/favorite resolution has a short TTL cache.
- Device refresh is deferred while a recording stream is active.
- Audio-level UI work is throttled to about 60 Hz.
- Live waveform uses Canvas/RAF instead of per-frame React state.
- The recording overlay WebView is created lazily on first show instead of at
  app startup; hidden overlays do not keep their own WebSocket connection.
- Overlay and live mic visualizers cap drawing to about 30 FPS and keep audio
  level updates out of React state where practical.

Backend/WebSocket:

- WebSocket broadcast skips JSON serialization when no clients are connected.
- Audio-level callback avoids scheduling frontend work when there are no clients
  and no overlay consumer.
- `history_updated` events are coalesced and carry enough context for targeted
  refresh behavior.
- Transcript append handling avoids repeated full transcript string rebuilds for
  long sessions.

Data and frontend:

- Transcript list endpoints use pagination and avoid full content loading for
  metadata views.
- Frontend history pages use infinite loading and scroll-container virtualization.
- Production frontend build uses manual vendor chunks.
- Route chunks are lazy for non-default pages.

I/O:

- Multipart upload writes use chunked helpers and offload file writes where
  practical.
- Export rendering and cleanup paths are moved off async request hot paths where
  practical.
- Job and latency stores reuse SQLite connections more efficiently.
- Settings persistence is debounced and flushed on shutdown.

Packaging/build:

- PyInstaller sidecar can be reused through a hash cache.
- PyInstaller sidecar release sync is content-aware, so unchanged files are not
  rewritten into the Tauri release tree.
- When the Tauri release backend target already has the current sidecar cache
  key and required release resources, the sidecar builder records
  `targetCurrent=true` and skips PyInstaller restore, frozen import check,
  backend tree sync, and Rust audio sidecar copy work.
- The Rust audio sidecar has its own input hash cache under
  `build\rust-audio-sidecar-cache`. Its cache key is limited to
  `Cargo.toml`, `Cargo.lock`, `build.rs`, `audio_sidecar.rs`,
  `audio_frame_pipe.rs`, and `redaction.rs`, with a module guard in the build
  script. It uses Tauri's normal Cargo target by default so desktop and audio
  sidecar builds can share compiled dependencies; `-RustAudioIsolatedTarget` is
  troubleshooting fallback.
- Installed frontend assets are owned by the Tauri WebView bundle and are not
  embedded in the Python/PyInstaller backend sidecar.
- The Rust audio sidecar is bundled once as Tauri's install-root
  `scriber-audio-sidecar.exe`, not as a duplicate `audio-sidecar/` resource
  directory.
- The recording overlay is owned by Tauri WebView shell IPC, so PySide6/Tk
  overlay runtimes are excluded from the Python backend sidecar.
- Frontend dist changes do not invalidate the sidecar cache; the backend static
  frontend fallback is explicit dev/test opt-in through
  `SCRIBER_FRONTEND_DIST_DIR` or a source checkout.
- Runtime dependency footprint gate rejects SciPy reintroduction and unused heavy
  runtime paths.
- Installed desktop stability smokes include per-role process-tree metrics for
  Tauri shell, backend, WebView2, audio sidecar, and other child processes.
- Local ONNX STT intentionally bundles only the small `onnx_asr/preprocessors`
  package data files required at runtime by models such as Parakeet TDT. Actual
  model weights remain in the user/model cache and are not embedded in the
  backend sidecar.
- GitHub release builds compute normalized cache-key inputs so patch version
  bumps do not invalidate frontend dependency, Rust build, or backend sidecar
  scratch caches unless their real inputs changed.
- FFmpeg Profile B has a reusable GitHub release artifact fallback in addition
  to Actions cache restore, so new app tags do not need to rebuild FFmpeg when
  the Profile B source/ref/profile is unchanged.

## FFmpeg Profile B

Profile B is the standard no-feature-loss Windows media-tool profile.

It is built through:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ffmpeg\build_profile_b_msys2.ps1
```

The build expects MSYS2/UCRT64 and can install dependencies when the script is
run with the matching option. The produced tools are validated by:

- Profile manifest creation.
- Fixture smoke checks.
- Scriber media-preparation smoke.
- Sidecar slim-media validation.
- Installed package media-preparation smoke.
- Real file/YouTube workflow smoke when provider credentials and network are
  available.

In GitHub release builds, Profile B reuse is layered:

1. Restore the normal Actions cache for the current ref.
2. If that misses, download the internal reusable release artifact
   `scriber-ffmpeg-profile-b-n7.0-v2-Windows.zip` from tag
   `ffmpeg-profile-b-n7.0-v2`.
3. Validate the restored tools and media-preparation behavior.
4. Build through MSYS2 only if both restored sources are absent or invalid.

When a real Profile B rebuild is required, the workflow republishes that
internal artifact with `--clobber`. App version changes alone must not require a
Profile B rebuild.

Profile B keeps required Scriber capabilities:

- MP3 encode/decode through `libmp3lame`.
- WebM/Opus support.
- AAC, Opus, MP3, FLAC, ALAC decode.
- stdout `pcm_s16le`.
- raw `s16le`.
- common local demuxers/muxers.
- `file` and `pipe` protocols.

It intentionally excludes unrelated network protocols, GPL/nonfree flags, video
encoders, and hardware acceleration stacks.

## Installer Build Modes

Fast local installer:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke
```

What it does:

- Syncs versions.
- Builds the Tauri WebView frontend through `npm run build:webview`.
- Reuses sidecar when backend/runtime inputs are unchanged; frontend asset
  changes are handled by Tauri and do not rebuild the Python sidecar.
- Reuses the Rust audio sidecar when Rust audio inputs are unchanged.
- Builds Tauri/NSIS. `-FastLocalInstaller` defaults NSIS compression to `lzma`
  so local installer sizes match GitHub release builds; it records
  `buildMode.devOnly=true` plus `buildMode.nsisCompression` in
  `build-timing.json`.
- Runs size and runtime dependency footprint gates.
- Writes the installed package smoke report into release metadata and uses it
  for the installed-app size section in `size-report.json`.
- Runs selected installed-package smokes.

Very tight local staged app loop:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalStagedApp `
  -SkipChecks `
  -SkipSmoke
```

This builds/copies the sidecars, runs `tauri build --no-bundle`, writes
`buildMode.artifactKind=staged-app`, and runs the media-preparation and runtime
dependency footprint checks. It is dev-only and intentionally does not validate
NSIS install, upgrade, shortcut, or uninstall behavior.

Release workflow:

- `.github/workflows/release-windows.yml` builds Profile B with MSYS2/UCRT64.
- The workflow caches stable heavy build inputs and outputs: a project-local
  Python `.venv` keyed by Python version plus release requirements,
  a Python wheelhouse for incremental package restores, `Frontend\node_modules`
  keyed by the frontend lockfile with npm's package cache as fallback, Cargo
  release dependency artifacts keyed by the Rust lockfile/source set, the Tauri
  bundler download cache, the PyInstaller/Rust-audio sidecar caches, and the
  Profile B media-tool output keyed by FFmpeg ref plus a manual cache profile
  version. Cache hits and restore-key hits still run the relevant validation
  gates before the installer consumes restored outputs. If a restored Profile B
  output fails validation, the workflow falls back to a fresh MSYS2 build.
- The signed release workflow installs only `requirements-base.txt` and
  `requirements-build.txt`. `requirements-dev.txt` is intentionally excluded
  because the packaging step skips the Python unit suite; run tests before
  tagging or through PR/readiness gates instead.
- Workflow concurrency cancels older in-progress release builds for the same
  ref so stale branch builds do not keep consuming runner time after a newer
  commit starts.
- It passes the produced media tools into `scripts/build_windows.ps1`.
- It collects size, media-preparation, runtime dependency, and timing evidence.
- Authenticode and Tauri updater signing gates are available but require real
  signing/updater secrets.
- The hybrid release-readiness runner can now invoke `scripts\build_windows.ps1`
  with `-RunReleaseBuild`, pass through updater/signature/Profile-B release
  flags, and reuse the build-generated Authenticode validation report before
  the final aggregate validator runs. This makes the signed release build a
  first-class evidence producer while still requiring real Tauri signing keys,
  HTTPS publication, and Authenticode certificate/cloud-signing evidence.

## Size Decisions

No-feature-loss constraints:

- No optional installer components.
- ffmpeg and ffprobe remain bundled in the standard installer.
- PySide6, customtkinter, and Tk overlay runtimes remain absent from the
  standard sidecar; the installed overlay is Tauri-owned.
- Provider runtime support remains bundled when the provider is still supported
  by backend or UI configuration, but provider SDKs are explicit rather than
  broad Pipecat extras.
- CPU ONNX local-ASR support is part of the standard sidecar. Full NeMo/Torch
  packaging remains outside the standard cloud-provider sidecar.

Current packaging choices:

- `pyloudnorm` is provided locally without SciPy in the packaged sidecar.
- ONNXRuntime remains because Pipecat Silero VAD needs it.
- SciPy, Torch, full NeMo, ONNX training/tooling stacks, numba, llvmlite, and
  unused ONNXRuntime tooling are excluded from the standard sidecar. The
  installed local-ASR path keeps `onnx-asr[cpu,hub]` plus ONNXRuntime so the
  Settings ONNX path works and the NeMo surface can fall back to ONNX when
  full NeMo/Torch is unavailable.
- AWS Transcribe is no longer exposed in frontend or backend settings. The
  standard sidecar excludes `boto3`, `botocore`, `s3transfer`, `aioboto3`,
  `aiobotocore`, and Pipecat AWS service modules.
- Gemini summaries use direct HTTP, so `google-generativeai` and Google Cloud
  Text-to-Speech are excluded from the standard sidecar. Google Cloud STT
  remains supported through `google-cloud-speech` plus Pipecat's required
  `google-genai` namespace dependency; OpenAI STT, Groq STT, and Pipecat's
  provider import path remain supported through explicit `openai`, `groq`, and
  `nltk` runtime dependencies.
- Deepgram Async, Gladia Async, OpenAI Async, and Speechmatics Batch use direct
  HTTP/batch adapters in `src/cloud_async_stt.py`. Speechmatics Batch does not
  add `speechmatics-batch`; the standard sidecar keeps only the existing
  Speechmatics realtime/runtime packages.
- AssemblyAI uses Universal-3.5-Pro by default for both direct async/batch and
  realtime Pipecat paths. Runtime import checks include Pipecat's AssemblyAI
  realtime module.
- Pillow AVIF binaries are excluded.
- Runtime dependency footprint gates reject SciPy, AWS SDK packages, PySide6,
  customtkinter, Tk, and unused provider SDK reintroduction in the packaged
  backend.

## Remaining Performance Opportunities

Highest-value current opportunities:

- Split stop-to-text latency into provider finalize, clipboard, and paste
  segments before optimizing locally.
- Run React Profiler on Live Mic during recording to catch broad subtree
  rerenders from status/history WebSocket events.
- Keep investigating UI responsive behavior at narrow widths.
- Add longer installed idle/live stability runs with CPU and memory budgets.

Lower-value or risky opportunities:

- yt-dlp extractor filtering. This is risky and should not be default.
- Removing ffprobe. This is an explicit experiment only.
- Moving more provider or pipeline work into Rust. Capture/prewarm are already
  Rust/WASAPI; further migration needs measured benefit and rollback gates.

## Rust Expansion Plan

Rust expansion must be additive, measurable, and reversible. The goal is not a
rewrite. Rust should be used only when the feature is fundamentally Windows
shell/native integration, benefits from a long-lived native resource outside
Python, reduces packaging/runtime fragility without behavior change, and can be
disabled with the current Python implementation still available.

Do not move provider code, Pipecat pipeline ownership, transcript persistence,
media preparation, REST/WebSocket contracts, settings semantics, or support
bundle ownership into Rust as part of this plan.

Required prerequisites before any Rust path becomes default:

1. Feature flags and rollback switches:
   - `SCRIBER_NATIVE_DEVICE_EVENTS=auto|0|1`
   - `SCRIBER_INJECT_METHOD=auto|paste|sendinput|type|tauri`
   - `SCRIBER_AUDIO_ENGINE` is diagnostic compatibility only; it no longer
     selects Python capture. Rust/WASAPI is the live-mic capture default.
2. Private shell IPC for backend-initiated shell work:
   - Tauri commands cover frontend-to-shell calls, but live text injection
     originates inside the Python pipeline.
   - Add a private Tauri-hosted Windows named pipe such as
     `\\.\pipe\scriber-shell-{sessionId}`.
   - Pass pipe name and per-run token to the backend via environment variables
     when Tauri starts the Python sidecar.
   - Keep this IPC out of public REST/WebSocket contracts.
   - Messages include `apiVersion`, `requestId`, `command`, `token`, and a
     bounded payload. Responses include `success`, `errorCode`,
     `fallbackReason`, and timing fields where applicable.
   - If the pipe is unavailable, token validation fails, or a command times
     out, Python continues with the current fallback path.
3. Diagnostics and support-bundle plumbing:
   - Extend `/api/runtime/audio-diagnostics` with a `native` section for Rust
     device events and any Rust audio prototype.
   - Hash/redact native endpoint IDs and foreground-window titles by default.
   - Report which engine/path was used, which fallback was selected, last
     error, and whether a Rust component is disabled by flag or health failure.
4. Measurement harness:
   - Keep the existing hot-path split between provider finalization,
     `clipboard_set`, `paste`, and `first_paste`.
   - Add comparable metrics for native-device refreshes and any Rust audio
     prototype: hotkey to first frame, hotkey to first non-silent frame, stop to
     last audio chunk sent, dropped audio frames, capture restart count, idle
     CPU/memory, live CPU/memory, and support-bundle diagnostic completeness.

Recommended sequence:

1. Windows audio device events first. This is the lowest-risk Rust candidate
   and establishes native endpoint diagnostics before any Rust capture work.
2. Clipboard/text injection second. This requires private backend-to-shell IPC
   and can be tested without touching the audio/provider flow.
3. Audio capture / always-on mic last. This is the highest-complexity candidate
   and must stay a prototype until it proves a material win over the current
   Python `sounddevice` path.

Do not remove `pycaw`/`comtypes`, `keyboard`/`pyautogui`, or `sounddevice`
until the relevant Rust replacement has passed installed Windows smokes and
physical-device coverage. Tk overlay and clipboard fallbacks are no longer part
of the standard sidecar.

### 1. Windows Audio Device Events

Decision:

- Proceed first, but only move Windows endpoint notification subscription and
  event debouncing into Rust. This is OS integration and fits Tauri ownership.
  The benefit is reliability, packaging simplification, and less Python COM
  surface, not a user-visible feature change.

Current code exploration:

- `src/device_monitor.py` owns microphone hotplug handling today.
- It uses `pycaw`/`comtypes` for Windows endpoint notifications when available.
- It falls back to sparse polling and still owns PortAudio refresh deferral,
  capture/render endpoint filtering, device signature comparison, and
  `microphones_updated` callbacks.
- `src/web_api.py` wires the monitor into controller startup, favorite-mic
  restore, `POST /api/microphones/refresh`, and `/api/runtime/audio-diagnostics`.
- Tests in `tests/test_device_monitor.py` cover endpoint filtering, deferred
  refresh, non-invasive polling, and poll interval behavior.

Target:

- Move Windows endpoint notification subscription into the Tauri process.
- Rust listens to Core Audio `IMMNotificationClient` events and posts a
  token-protected backend refresh hint only for capture/default-device changes.
- Python remains the source of truth for the microphone list, favorite fallback,
  PortAudio cache refresh, and WebSocket `microphones_updated` events.
- Rust must never enumerate the user-facing microphone list as source of truth
  in this phase. It only tells Python that a capture endpoint likely changed.

Ownership boundary:

- Rust owns `IMMNotificationClient` registration/unregistration, COM
  setup/teardown on a dedicated native-event thread, capture/default-device
  event filtering, debounce/duplicate suppression, token-protected backend
  refresh hints, and native event health diagnostics.
- Python keeps public microphone list semantics, favorite/default microphone
  restore, PortAudio cache refresh, active-stream detection and deferral,
  `microphones_updated` WebSocket emission, `/api/microphones/refresh`
  behavior, `/api/runtime/audio-diagnostics` aggregation, and sparse polling
  fallback.

Implementation plan:

1. Extend `POST /api/microphones/refresh` to accept an optional hint body:
   `source`, `eventKind`, `flow`, `role`, `endpointIdHash`,
   `forcePortAudioRefresh`, and `nativeTimestampMs`.
   - Treat this body as a hint only.
   - Python still decides whether to refresh PortAudio now, defer until idle,
     or only update diagnostics.
   - Reject render-only events in Rust where possible. If Rust cannot determine
     flow, send `flow: "unknown"` and let Python refresh conservatively.
2. Add a Windows-only Rust module, for example
   `Frontend/src-tauri/src/audio_devices.rs`, behind `#[cfg(windows)]`.
3. Use the `windows` crate for COM/Core Audio interfaces rather than adding
   another Python COM dependency path.
4. Register an `IMMNotificationClient` during Tauri setup after backend access
   is available, initially in observe-only mode:
   - log registration status to the shell log,
   - do not call `/api/microphones/refresh` yet,
   - add diagnostics for COM init status, callback liveness, last event age,
     event counts by kind, ignored render events, debounce count, and last
     error.
5. Filter events to capture endpoints only:
   - `OnDeviceAdded`
   - `OnDeviceRemoved`
   - `OnDeviceStateChanged`
   - `OnDefaultDeviceChanged` for capture flow
   - property changes only when they affect capture endpoint identity/name.
6. When observe-only evidence is clean, post refresh hints from a Rust worker
   thread. Do not call backend HTTP directly from the COM callback.
7. Keep Python `DeviceMonitor` fully enabled while Rust event hints roll out.
8. After several installer smokes and physical dock/USB tests, make Python
   `pycaw`/`comtypes` callbacks optional in installed Windows mode only if no
   other feature uses them. Keep sparse Python polling as permanent safety net.

Implementation status on `codex/rust-expansion-plan`:

- Implemented the backend prerequisite for native device hints:
  `POST /api/microphones/refresh` accepts the optional hint body and remains
  backward compatible with empty manual refresh requests.
- Added `SCRIBER_NATIVE_DEVICE_EVENTS=auto|0|1` runtime feature-flag
  reporting.
- `DeviceMonitor` now records redacted native-hint diagnostics, ignores
  render-only hints, and keeps PortAudio refresh scheduling/deferral in Python.
- Added a Tauri-owned Windows `IMMNotificationClient` worker with COM lifetime
  management, event channel handoff, render filtering, debounce, hashed endpoint
  IDs, and backend JSON-body request support.
- In `SCRIBER_NATIVE_DEVICE_EVENTS=auto`, the worker is observe-only and logs
  native events without changing backend behavior. In
  `SCRIBER_NATIVE_DEVICE_EVENTS=1`, it posts token-protected refresh hints to
  `POST /api/microphones/refresh`.
- Implemented support-bundle native status reporting beyond backend hints:
  Tauri maintains a redacted native device event monitor status snapshot and
  exposes it through private shell IPC command `nativeDeviceEventsStatus`.
  `/api/runtime/audio-diagnostics` now includes it as
  `microphone.nativeDeviceEvents`, covering requested/effective mode,
  COM/registration/running state, callback liveness, event counts by kind,
  render-ignored and debounced counts, post success/failure counts, last event
  age, and last redacted post error.
- Added focused Python tests for render filtering, capture scheduling,
  non-invasive hints, controller forwarding, runtime feature flags, and REST
  contract coverage, plus Rust tests for native mode parsing, event filtering,
  redaction, hint payloads, debounce, and native-status IPC.
- Implemented installed support-bundle smoke coverage for native COM
  registration: when Tauri shell IPC is available and native events are not
  disabled or unsupported, `scripts/smoke_tauri_desktop.ps1` now requires
  `available`, `running`, `registered`, `comInitialized`, and `callbackAlive`
  to be true, and records the verdict under `nativeDeviceEvents`.
- Implemented installed support-bundle smoke coverage for Rust audio fallback
  diagnostics: the same smoke now requires
  `microphone.rustAudioFallbackCircuit` in `audio-diagnostics.redacted.json`,
  including `available`, boolean `open`, non-negative cooldown fields, and
  reason/remaining-time evidence whenever the circuit is open.
- Still open: physical dock/USB/default-device matrix coverage.

Acceptance gates:

- Unit tests for Rust event filtering and debounce logic.
- Existing Python tests for `DeviceMonitor` continue passing.
- New Python tests prove native hint bodies still defer PortAudio refresh while
  active streams are running.
- Installed smoke proves backend and frontend still start without COM errors.
- Physical hardware matrix covers USB mic add/remove, dock connect/disconnect,
  Windows default input changes, and favorite mic restore. For Rust audio
  promotion, run `scripts\run_microphone_hardware_matrix.ps1` or the hybrid
  readiness runner with `-RequireRustEndpointInventory` and
  `-RequireDeviceRefreshEvidence` so every physical artifact also captures and
  validates Rust/WASAPI endpoint inventory changes plus native-event
  DeviceMonitor refresh evidence without raw IMMDevice IDs or forced per-poll
  refreshes.
- Always-on mic light must not blink during idle safety periods unless an actual
  capture endpoint event occurred.
- Support bundle clearly reports whether Rust events or Python fallback handled
  the last refresh, and whether the Rust audio fallback circuit is open.

Rollback:

- Set `SCRIBER_NATIVE_DEVICE_EVENTS=0`.
- Python `DeviceMonitor` remains fully capable.
- Backend ignores unknown native hint fields.
- If Rust registration fails, log it once and leave Python fallback active.

Risks:

- COM apartment mismatch or a dropped callback object can silently disable
  events.
- Property-change events can be noisy; do not refresh on every property unless
  it affects endpoint identity/name/default status.
- Endpoint IDs are hardware-identifying; hash/redact them in logs and support
  bundles.
- Rust event success can hide PortAudio cache issues. Python must remain owner
  of PortAudio refresh and active-stream deferral.

### 2. Clipboard And Text Injection

Decision:

- Proceed second, after private shell IPC exists. Rust is useful because this is
  native Windows shell integration and Tauri already owns shell behavior. The
  goal is one well-diagnosed native operation, not assuming it will be
  dramatically faster than the already optimized Python clipboard path.

Current code exploration:

- `src/injector.py` owns live text injection inside the Pipecat pipeline.
- Clipboard paste uses Win32 via `ctypes`, then `keyboard.press_and_release`.
  The default Python paste path snapshots bounded safe HGLOBAL-backed clipboard
  formats before setting transcript text and restores that snapshot when the
  clipboard sequence is unchanged. It preserves common text, file-drop, and DIB
  image payloads without touching non-HGLOBAL handles such as `CF_BITMAP`.
- Fallback paths use `pyautogui` and `keyboard.write`; Tkinter clipboard helpers
  were removed from the standard path.
- `Frontend/src-tauri/src/lib.rs` already has a Windows clipboard helper for
  tray recent transcript copying, but it does not send paste keystrokes or
  preserve/restore clipboard contents for live transcription.
- Hot-path markers in Python already split `provider_final_received`,
  `clipboard_set`, `paste`, and `first_paste`.

Target:

- Move Windows clipboard set, optional restore, and Ctrl+V dispatch to Rust.
- Python pipeline still decides when text should be injected and records
  hot-path markers.
- Rust performs only the OS operation and returns structured timing/status.
- Rust must not become a transcript owner. It receives text, performs an OS
  operation, and returns status.

Ownership boundary:

- Rust owns capturing a bounded snapshot of the current Windows clipboard
  formats, setting new Unicode clipboard text, dispatching Ctrl+V, safe
  optional snapshot restore, foreground-window snapshot, basic diagnostics, and
  structured timing/status response.
- Python keeps Pipecat `TextInjector` frame handling, interim/final buffering,
  the decision of when text is injected, hot-path markers, `on_injected`
  callbacks, existing `paste`/`sendinput`/`type` modes, fallback selection,
  provider pipeline, and transcript persistence.

Implementation plan:

1. Implement the private Tauri named-pipe command server described in the
   prerequisites.
   - Add a Python client with short connect/write/read deadlines.
   - Add shell capability diagnostics such as `shellIpc.available`,
     `shellIpc.lastError`, `shellIpc.lastCommand`, and
     `shellIpc.lastCommandAgoSeconds`.
   - Do not route text injection through it until this IPC is independently
     stable.
2. Add a Rust `injectText` command over the private pipe. Request payload:
   `text`, `restoreClipboard`, `restoreDelayMs`, `preDelayMode`, `dispatch`,
   `maxClipboardRetries`, and `clipboardRetryDelayMs`.
3. Response payload should include:
   - `success`,
   - `method`,
   - marker list such as `clipboard_set` and `paste`,
   - timings for clipboard read, clipboard set, pre-delay, paste dispatch,
     restore, and total duration,
   - foreground-window change status,
   - hashed foreground-window title and process id,
   - `fallbackReason` and `errorCode`.
4. Reuse and extend `copy_text_to_clipboard` in `Frontend/src-tauri/src/lib.rs`:
   - capture a bounded snapshot of current restorable clipboard formats,
   - set new clipboard text,
   - dispatch Ctrl+V with `SendInput`,
   - restore only if the clipboard sequence has not changed since Scriber set
     the injected text,
   - never hold the clipboard open while sleeping or dispatching input.
5. Add target-window diagnostics using `GetForegroundWindow` and
   `GetWindowTextW` so Word/Outlook delay decisions can move out of Python.
6. Add strict `SCRIBER_INJECT_METHOD=tauri`.
   - In strict `tauri`, failure logs a warning and does not silently use
     another method unless an explicit fallback flag is enabled.
   - In `auto`, keep the current Python paste path as default until installed
     target-app evidence justifies changing it.
7. Preserve hot-path marker semantics by forwarding Rust response markers to
   `on_injection_marker("clipboard_set")` and `on_injection_marker("paste")`.
8. Add packaging gates to ensure Rust injection does not reintroduce visible
   helper windows or new heavy Python dependencies.

Implementation status on `codex/rust-expansion-plan`:

- Implemented the private Tauri shell IPC foundation for Windows:
  per-process named pipe, per-session token, newline-delimited JSON request/
  response protocol, `ping` and `capabilities` commands, and explicit
  `apiVersion=1`.
- Managed backend workers receive `SCRIBER_SHELL_IPC_PIPE`,
  `SCRIBER_SHELL_IPC_TOKEN`, and `SCRIBER_SHELL_IPC_API_VERSION` only when the
  Tauri pipe server is running. The token is not logged.
- Added the Python `src.runtime.shell_ipc` client/diagnostic module with short
  call timeout support, redacted pipe-name hashing, and redacted transport
  failure text so raw pipe names and session tokens are not persisted in
  diagnostics or returned fallback reasons.
- `/api/runtime/audio-diagnostics` now reports `textInjection.shellIpc` status
  without calling the pipe from readiness or startup paths.
- Added the `injectText` shell IPC command for opt-in Tauri text injection. The
  Rust side captures a bounded Windows clipboard format snapshot, sets
  `CF_UNICODETEXT`, dispatches Ctrl+V with `SendInput`, returns structured
  timing data and `clipboard_set`/`paste` markers, hashes foreground-window
  diagnostics, owns `preDelayMode=auto` foreground-title classification for
  Word/Outlook without logging raw titles, uses byte-budgeted request limits,
  rejects embedded NUL text, enforces a request deadline before side effects,
  and guards clipboard restore with the clipboard sequence number.
- Hardened the private Tauri shell IPC pipe with an explicit protected Windows
  DACL. The pipe still uses a per-process random name, a per-session token, and
  `PIPE_REJECT_REMOTE_CLIENTS`. When the current logon SID is available, access
  is limited to that logon session plus LocalSystem and built-in administrators;
  otherwise it falls back to owner, LocalSystem, and built-in administrators
  instead of relying on the default security descriptor.
- Replaced ownerless clipboard access in the Tauri text-injection path with a
  short-lived message-only `ScriberClipboardOwner` window. `injectText` now
  calls `OpenClipboard(hwnd)` for read, set, and delayed restore operations
  instead of `OpenClipboard(NULL)`.
- Clipboard restore now preserves a bounded snapshot of safe HGLOBAL-backed
  clipboard formats instead of only `CF_UNICODETEXT`. This keeps common text,
  file-drop, and DIB/DIBV5 image payloads from being permanently replaced by the
  injected transcript text. Restore still skips when the Windows clipboard
  sequence changed after Scriber set its text, so user copies during the restore
  delay win over Scriber's delayed restore. Non-HGLOBAL handle formats such as
  `CF_BITMAP`, `CF_PALETTE`, and `CF_ENHMETAFILE` are counted as unsupported and
  skipped before any `GetClipboardData`/`GlobalSize` access.
- The Python `auto`/`paste` injection path now follows the same snapshot rule as
  Tauri `injectText`. It no longer continues with clipboard paste when the
  existing clipboard cannot be snapshotted, because that would overwrite
  non-text assets without a safe restore path.
- Added strict `SCRIBER_INJECT_METHOD=tauri` in Python `TextInjector`.
  `auto` intentionally still uses the existing Python paste path until installed
  target-app evidence justifies changing the default.
- Implemented redacted last-attempt diagnostics for Tauri text injection:
  Shell IPC records `lastErrorCode`, `lastFallbackReason`, and a sanitized
  `lastResponse`; `TextInjector` overwrites transport-level success when
  injection validation fails, such as invalid payload or missing
  `clipboard_set` / `paste` markers. Support bundles include these fields
  through
  `audio-diagnostics.redacted.json` without transcript text, raw pipe names,
  raw foreground titles, or session tokens.
- Tauri text-injection hot-path metrics now use the Rust-reported
  `clipboardSet` and `pasteDispatch` timing offsets to place Python
  `clipboard_set` and `paste` markers inside the real Shell IPC call window,
  instead of stamping both markers only after the IPC response returns.
- Added Rust unit tests for the shell IPC protocol and backend env contract,
  injectText payload validation, retry-limit clamping, request/text budget
  consistency, NUL rejection, deadline failure payloads, and message-mode
  partial-read handling for large IPC requests, plus Python unit/contract
  coverage for shell IPC diagnostics, strict Tauri injection marker forwarding,
  and response protocol validation.
- Implemented the first promotion evidence hook for the safe target-window
  path: `scripts/smoke_text_injection_target.py` now accepts `--method tauri`
  and records the redacted Shell IPC diagnostic snapshot in its JSON artifact.
  The hybrid release-readiness runner and validator can require that artifact
  with `-RequireTauriTextInjectionSmoke` /
  `--require-tauri-text-injection-smoke`, and the runner can now produce it
  directly with `-RunTauriTextInjectionSmoke` when launched inside the
  Tauri-managed backend environment that provides Shell IPC variables. The
  report must be real evidence, not `--validate-only`, and must show
  `injectText` success, target text arrival, and `clipboard_set` plus `paste`
  markers. It now also must include structured clipboard restore evidence;
  missing restore fields, restore errors, or disabled restore fail promotion
  evidence. Foreground diagnostics in the Shell IPC payload must remain
  hashed/redacted and must not expose raw window titles, HWNDs, process IDs, or
  process names.
- Added a stronger Tauri text-injection matrix readiness gate:
  `-RequireTauriTextInjectionMatrix` /
  `--require-tauri-text-injection-matrix`. The matrix artifact must aggregate
  real installed target-app reports for Notepad, Word, Outlook, browser input,
  browser contenteditable, Electron, elevated-target, elevated-Scriber,
  clipboard text/non-text/locked, restore user-copy, and same-text restore
  scenarios. Remote Desktop is optional when unavailable but is validated if
  present. `scripts/build_tauri_text_injection_matrix.py` builds the aggregate
  from the individual scenario reports and validates it before returning
  success. The matrix reuses the safe-smoke restore gate for every scenario, so
  target-app evidence cannot omit restore diagnostics or hide restore failures.
  It also reuses the foreground redaction gate for every target-app scenario.
  Tauri injection evidence now must include `deadlineMs` in the redacted Shell
  IPC response payload, and the validator rejects artifacts whose
  `timingsMs.total` exceeds that deadline, so stale reports cannot prove the
  "do not paste after Python timed out" invariant.
  The hybrid release-readiness runner can now invoke the matrix builder with
  `-RunTauriTextInjectionMatrixBuilder`, but this only aggregates already
  collected real target-app reports; it does not simulate the required manual
  Notepad/Office/browser/Electron/elevated/clipboard coverage.
- Still open: actually running the installed target-app smoke matrix, packaging
  smoke evidence, and default-path decision based on installed evidence.

Tauri injection default blockers:

- `auto` remains Python paste. `SCRIBER_INJECT_METHOD=tauri` is strict opt-in
  and fails closed; it must not silently fall back to Python paste.
- Fallback to Python applies only to a future explicit `auto` routing decision,
  not to strict `tauri`.
- No clipboard mutation is acceptable unless the previous text state was
  captured, restore is explicitly disabled by request, or the command fails
  before setting the clipboard.
- Every failure after a successful clipboard set must restore immediately or
  report `restore.skippedReason` / `restore.errorCode`.
- Rust must not paste after the Python client deadline. The request includes
  `deadlineMs`; Rust checks remaining budget before clipboard set and before
  paste.
- Normal logs, diagnostics, and support bundles must not contain transcript
  text, session tokens, raw pipe names, raw foreground titles, or unredacted
  target identifiers.
- Manual installed smokes must include: text clipboard, non-text clipboard,
  clipboard locked by another app, user copies during restore delay, same-text
  user copy, Notepad, Word, Outlook, browser input, browser contenteditable,
  Electron, elevated target, elevated Scriber, and Remote Desktop if supported.
  For default-promotion evidence, aggregate those results into
  `tauri-text-injection-matrix.json` with
  `scripts/build_tauri_text_injection_matrix.py` and require
  `-RequireTauriTextInjectionMatrix`. Matrix validation now requires
  `preDelayMode=auto` for every scenario and positive applied pre-delay for
  Word/Outlook, so the artifact proves Rust owns the foreground delay policy.
- Support bundles should surface the last Tauri injection fields:
  `textInjection.method`, `shellIpc.available`, `lastCommand`, `lastErrorCode`,
  `fallbackReason`, `preDelayMode`, `requestedPreDelayMs`, `restoreScheduled`,
  `restore.succeeded`, `restore.skippedReason`, `foregroundBefore`,
  `foregroundAfter`, `foregroundChanged`, and `timingsMs`, all redacted.

Acceptance gates:

- Rust unit tests for clipboard string encoding and option handling.
- Python tests for `TextInjector` fallback selection, IPC timeout, Rust success
  marker forwarding, Rust failure behavior, and disabled text injection.
- Existing text-injection smoke with a safe target window.
- Manual smoke in Notepad, Word, Outlook, browser text field, browser
  contenteditable, Electron app, elevated target versus non-elevated Scriber,
  Scriber elevated versus non-elevated target, and Remote Desktop if supported.
- Hot-path report should show local injection overhead remains small compared
  with provider finalization. If not, revert to Python paste as default.
- Failure path is diagnosable from support bundle without leaking transcript
  text or raw foreground-window titles.

Rollback:

- Set `SCRIBER_INJECT_METHOD=paste` or keep `auto` with Rust injection disabled.
- In strict `tauri`, shell IPC failure stops injection for that attempt. Existing
  Python paths continue to be used by `auto`, `paste`, `sendinput`, and `type`.
- Keep Python fallback indefinitely unless a separate deprecation plan proves
  coverage across target apps and privilege levels.

Risks:

- Focus can change after clipboard set and before paste.
- Clipboard restore can overwrite a user copy unless guarded carefully.
- Elevated app behavior can differ by input method and system policy.
- Window-title diagnostics can leak private document names; hash/redact by
  default.
- A faster path that pastes into the wrong window is worse than a slower path.

### 3. Audio Capture And Always-On Mic

Decision:

- Do not promote Rust audio to default in this plan. Build only a measured
  prototype after native device events and shell IPC are stable. The current
  Python path owns more than reading microphone bytes: device fallback, channel
  selection, queue backpressure, prewarm adoption, rolling prebuffer, waveform
  levels, watchdogs, Pipecat frame semantics, and stop-flush timing.
- Rust is beneficial only if a prototype proves materially faster
  hotkey-to-first-frame, fewer capture stalls/restarts on real hardware, lower
  idle/live CPU without losing prebuffer behavior, better recovery from
  dock/default-device changes, or a simpler installed support story.

Current code exploration:

- `src/microphone.py` owns active capture with `sounddevice.InputStream`,
  channel selection, queueing, audio-level throttling, watchdog health checks,
  and Pipecat `InputAudioRawFrame` emission.
- `src/mic_prewarm.py` owns always-on idle prewarm, rolling prebuffer,
  adoption into active capture, and stream diagnostics.
- `src/device_monitor.py` exposes the shared PortAudio guard and active-stream
  counter to avoid unsafe refreshes.
- `src/pipeline.py` instantiates `MicrophoneInput`, passes prewarm manager, and
  flushes the pipeline on stop.

Current decision:

- Rust/WASAPI sidecar capture is the default and only live microphone capture
  path after the 2026-06-11 short provider-backed A/B comparison.
- The comparison did not pass the historical strict P95 no-regression gate
  because first-audible-frame P95 was noisy, but it satisfied the chosen
  aggressive decision rule: Rust was clearly faster for median mic-ready and
  first-audio latency, delivered frames, adopted prewarm, used the correct
  endpoint path, reported no dropped frames, and kept the fallback circuit
  closed.
- Python still owns recording state, Pipecat/provider flow, persistence,
  diagnostics aggregation, and REST/WebSocket contracts. It no longer owns live
  microphone capture or idle prewarm.
- Sounddevice/PortAudio may remain as helper infrastructure for microphone
  listing and PortAudio-to-native endpoint mapping until those surfaces are
  fully native. It must not be used as a live-capture fallback.
- The Rust sidecar must feed Python the same downstream contract:
  16 kHz mono PCM `i16` frames that become `InputAudioRawFrame` with expected
  sample rate and channel count.

Ownership boundary:

- Rust owns WASAPI stream open/start/stop, native endpoint
  selection for the selected engine, callback/frame timestamps, drop counting,
  native capture restart attempts, rolling prebuffer, and binary PCM transport
  to Python.
- Python keeps provider pipeline, Pipecat transport semantics, VAD/analyzer
  ownership, recording state, REST/WebSocket contracts, transcript persistence,
  text-injection trigger flow, user-facing microphone list and favorite/default
  semantics, and support-bundle aggregation.

Missing prerequisites:

1. Engine-neutral Python frame-source boundary:
   - Implemented and promoted: `src/microphone.py` keeps the
     `AudioFrameSource` boundary but now creates the Rust frame-pipe source for
     live microphone capture.
   - `PythonSoundDeviceFrameSource`, Python idle prewarm adoption, and
     first-frame fallback to Python were removed from the normal product path.
   - `MicrophoneInput` exposes `engine`, `requestedEngine`, `frameSource`,
     `engineFallbackReason`, `droppedFrameCount`, and nested Rust source
     diagnostics. If Rust capture cannot deliver frames, recording fails
     visibly instead of silently switching engines.
   - `SCRIBER_AUDIO_ENGINE` is retained as a backwards-compatible diagnostic
     input and no longer selects Python capture.
   - Focused source contract tests cover device-index parsing, frame-pipe
     reading, Rust-unavailable failure, and Always-On-Mic startup without
     Python-prewarm adoption.
2. Native endpoint identity mapping:
   - Implemented on `codex/rust-expansion-plan` as a private diagnostic and
     Rust/WASAPI mapping layer in `src/audio_devices.py`.
   - Current user-facing device IDs remain Python/PortAudio names.
   - `hash_native_endpoint_id`, `NativeEndpointEntry`, and
     `InputEndpointMapping` connect normalized PortAudio names to hashed native
     endpoint IDs, default-input hash, and current favorite mic label.
   - `/api/runtime/audio-diagnostics` includes
     `microphone.nativeEndpointMapping`; `/api/microphones` is unchanged and
     never exposes raw IMMDevice IDs.
   - Implemented for Rust/WASAPI: Python can pass a redacted
     `nativeEndpointIdHash` derived from the private mapping into
     `audioCaptureStart`; the sidecar hashes IMMDevice IDs with the same
     SHA-256/16-hex convention and selects the matching capture endpoint when
     present.
   - Implemented for the passive Rust/WASAPI probe: `/api/runtime/audio-diagnostics`
     now sends the selected PortAudio label and redacted native endpoint hash
     when available, and the Tauri probe uses the same SHA-256/16-hex endpoint
     hash contract as Python and the active audio sidecar.
   - Implemented: private shell IPC now exposes `audioEndpointInventory`, a
     Rust/WASAPI capture endpoint inventory that returns friendly names,
     redacted endpoint hashes, active state, and default roles without raw
     IMMDevice IDs. `/api/runtime/audio-diagnostics` includes this as
     `microphone.rustNativeEndpointInventory`, and
     `microphone.nativeEndpointMapping` prefers that Rust inventory over
     best-effort PyCAW before falling back to PyCAW or PortAudio-only mapping.
     Passive Rust probe selection uses the same Rust/Tauri inventory first, so
     probe, prewarm, and active capture resolve selected/favorite microphones
     to the same redacted endpoint hash when the shell IPC inventory is
     available.
   - Still open: proving favorite/default behavior on physical dock/USB
     transitions.
3. Audio support-bundle schema:
   - Partly implemented on `codex/rust-expansion-plan`: support bundles now
     include `audio-diagnostics.redacted.json`, sourced from
     `/api/runtime/audio-diagnostics`.
   - Active capture diagnostics include `engine`, `requestedEngine`,
     `frameSource`, `sampleRate`, `targetChannels`, `captureChannels`,
     `blockSize`, `droppedFrameCount`, and last-callback age.
   - Native endpoint mapping diagnostics now include hashed native endpoint
     candidates when an inventory provider is available.
   - Implemented for Rust/WASAPI: active-capture
     `nativeEndpointIdHash`, sidecar PID, stop exit status, sidecar uptime,
     writer connection state, frames/bytes written, writer error, reader-thread
     liveness, and sidecar restart count are exposed through the nested
     active-capture source diagnostics.
   - Implemented: the REST contract for `/api/runtime/audio-diagnostics`
     validates the optional `microphone.activeCapture` schema, including nested
     Rust `source` diagnostics and bounded-cleanup fields
     `sidecarKilledAfterTimeout` and `sidecarWaitError`.
   - Implemented: the same REST contract validates optional
     `microphone.prewarm` diagnostics, including Rust Always-On-Mic
     stop-to-prewarm-ready gap metrics, restart/resume counters, status timing,
     recent event metadata, and rejection of raw `prewarmId` / `prewarm_id`
     fields.
   - Implemented for Rust/WASAPI: Python reader diagnostics now include
     frame-pipe frames read, audio frames read, bytes read, sequence/protocol
     error counters, last frame metadata, first-frame read timing, reader end
     reason, and callback-drop counts before protocol failure.
   - Implemented for future Rust prewarm parity: Python reader diagnostics now
     distinguish `SAF1` prebuffer frames from live frames, track prebuffer/live
     audio-frame counts and first live sequence, and reject prebuffer frames
     that arrive after live frames.
   - Implemented: `/api/runtime/audio-diagnostics` now retains the latest mic
     watchdog warning snapshot under `watchdog.lastWarning`, including the
     redacted active-capture or idle-prewarm diagnostics that caused the
     warning. Successful idle-prewarm recoveries are captured too when
     `healthRestartCount` increases during a watchdog check, so a brief
     microphone privacy-indicator off/on event is visible even if the stream is
     already healthy again by the time the user opens the Debug Console.
     Support bundles include this snapshot through
     `audio-diagnostics.redacted.json`, so short live/Always-On-Mic
     interruptions remain diagnosable after the recording has stopped.
   - Implemented: support-bundle redaction now treats raw `prewarmId` and
     `prewarm_id` JSON keys as sensitive while preserving already-redacted
     hash fields such as `prewarmIdHash`.
   - Implemented: Rust mid-session frame-pipe failures are surfaced as
     `midSessionFailureReason`, open a fallback-on-next-session circuit, and
     are now rejected by recording hot-path summaries, Python/Rust comparison
     gates, and installed live-recording Rust promotion evidence.
   - Still open: physical support-bundle evidence from long real-device runs.
4. Audio frame-pipe protocol:
   - Implemented on `codex/rust-expansion-plan` as shared Rust/Python protocol
     helpers, a Python `RustPrototypeFrameSource`, a test-only synthetic
     frame-pipe writer, and the default WASAPI writer with default or
     redacted-hash endpoint selection.
   - Rust owns `Frontend/src-tauri/src/audio_frame_pipe.rs`; Python owns
     `src/runtime/audio_frame_pipe.py`.
   - The frame header is fixed-size, little-endian, versioned, and covers magic,
     payload length, sequence, timestamp, frame count, channels, and flags.
   - Tests keep a shared documented header fixture in sync across Rust and
     Python and validate payload length and sequence ordering.
   - Python reads the binary frame pipe, validates sequence/order/channel count,
     forwards PCM frames through the existing callback path, and redacts the raw
     frame-pipe name in diagnostics.
   - With `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1`, the sidecar creates a
     private Windows named pipe and writes synthetic silence frames in the shared
     `SAF1` protocol. This proves transport and lifecycle plumbing only; it is
     not a microphone capture engine.
   - For normal app runs, the sidecar opens either the Windows default capture
     endpoint or a selected endpoint by redacted native endpoint hash through
     WASAPI shared mode, converts supported float/PCM mix formats to requested
     `pcm_i16_le` blocks, and writes those frames through the same `SAF1`
     protocol. `SCRIBER_RUST_AUDIO_DISABLE_WASAPI_CAPTURE=1` is test-only for
     forcing the unavailable path.
5. Audio capture control protocol:
   - Implemented as private shell IPC commands `audioCaptureStart`,
     `audioCaptureStop`, `audioPrewarmStart`, and `audioPrewarmStop`.
   - Current Rust shell implementation validates the start/stop payloads, calls
     the Tauri-side sidecar client when an allowlisted sidecar executable is
     available, and otherwise returns explicit `audioCaptureUnavailable`.
   - Python treats that explicit failure as a recording failure with diagnostics;
     the Python `sounddevice` capture fallback has been removed.
6. Rust audio sidecar process skeleton:
   - Implemented as separate Cargo binary `scriber-audio-sidecar` in
     `Frontend/src-tauri/src/audio_sidecar.rs`.
   - The binary supports `--self-test` plus `--stdio` JSON-lines commands:
     `ping`, `capabilities`, `captureStart`, `captureStop`, `prewarmStart`,
     `prewarmStop`, and `shutdown`.
   - The sidecar reports the shared audio frame protocol and runs WASAPI capture
     by default. The synthetic frame-pipe transport harness remains available
     through `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1` for tests. WASAPI can
     select a specific endpoint by redacted native endpoint hash; without a
     hash, non-default capture requests fail before first frame so the wrong
     Windows default microphone is not opened silently.
   - Implemented: the sidecar also has an idle prewarm harness behind
     `prewarmStart`/`prewarmStop`. It keeps a long-lived sidecar process,
     reports `prewarmId`, counts observed and buffered frames, and returns
     stop-health fields.
   - Implemented: `-BundleRustAudioSidecar` builds
     `scriber-audio-sidecar --release`, copies it into
     `Frontend\src-tauri\resources\audio-sidecar`, and the NSIS bundle includes
     that resource.
   - Implemented: selected endpoint capture by redacted native endpoint hash,
     plus endpoint-selection diagnostics in sidecar responses.
   - Still open: more physical favorite/dock/USB smokes and long
     physical-device evidence for edge cases.
7. Tauri audio sidecar client:
   - Partly implemented in `Frontend/src-tauri/src/audio_sidecar_client.rs`.
   - Discovers only allowlisted sidecar executable names, with
     `SCRIBER_AUDIO_SIDECAR_EXE` available for local prototype runs.
   - Starts `scriber-audio-sidecar --stdio`, sends JSON-lines commands,
     validates protocol version and request ID, hides the Windows child
     console, and redacts executable paths to hashes.
   - Maintains lifecycle registries for successful capture sessions keyed by
     `streamId` and synthetic prewarm sessions keyed by `prewarmId`;
     `audioCaptureStop`, `audioPrewarmStop`, backend restart, and shell exit
     drain those registries and send sidecar shutdown.
   - Shell IPC capabilities now report sidecar executable availability and
     protocol version.
   - The client searches the installed Tauri resource layout
     `resources\audio-sidecar` in addition to development/env override paths.
   - `audioCaptureStart`/`audioCaptureStop` route through the client, expose
     successful sidecar fields such as `streamId` and `framePipe` at the top
     level for Python. The obsolete unavailable-by-default prototype behavior
     was removed when Rust/WASAPI became the standard capture path.
   - `audioPrewarmStart`/`audioPrewarmStop` now route through the same client
     and preserve prewarm stop-health fields. The commands remain private
     shell IPC, and Python uses them through the Rust prewarm manager.

Implementation plan:

1. Refactor without Rust:
   - Completed historically: introduced the internal frame-source boundary.
   - Superseded: `SCRIBER_AUDIO_ENGINE=python` no longer selects Python capture.
   - Preserve installed live-mic smoke behavior with the Rust/WASAPI default.
2. Add a passive Windows-only WASAPI probe:
   - Implemented on `codex/rust-expansion-plan` as private shell IPC command
     `audioProbe`.
   - Runs as a native diagnostic when requested by runtime diagnostics or when
     `SCRIBER_RUST_AUDIO_PROBE=1` is set.
   - Rust probes the Windows default capture endpoint through WASAPI, activates
     `IAudioClient`, reads mix format/device period, attempts shared-mode
     initialization without `Start()`, and returns redacted diagnostics.
   - `/api/runtime/audio-diagnostics` surfaces this as
     `microphone.rustAudioProbe`.
   - Implemented: the probe can use a selected redacted native endpoint hash
     instead of probing only the Windows default endpoint, and refuses to report
     a non-default request as a default success when no native hash is available.
   - The probe does not feed the provider pipeline and reports
     `callbackCount=0`, `droppedFrameCount=0`, and
     `closeStatus=closed`.
   - Still open: real callback-based passive observation if needed and installed
     physical-device evidence.
3. Add active capture without prewarm:
   - Implemented: Python can start Rust capture through private
     shell IPC and receive frames through the binary frame-pipe reader.
   - Implemented: a separate Rust audio sidecar binary exists for crash
     isolation and exposes a JSON-lines control protocol.
   - Implemented: Tauri can discover an allowlisted sidecar executable
     and perform stdio command handshakes outside the WebView/UI thread.
   - Implemented: Tauri keeps successful sidecar capture processes alive
     by `streamId` and drains them on capture stop, backend restart, and shell
     exit.
   - Implemented: the synthetic sidecar mode creates a private
     Windows named pipe and writes valid `pcm_i16_le` `SAF1` frames through the
     same lifecycle path Python uses for real capture.
   - Implemented: WASAPI sidecar capture opens the requested native endpoint
     when a redacted hash is available, otherwise the Windows default endpoint
     for true default requests, and writes real PCM frames into that same
     frame-pipe lifecycle.
   - Implemented: Python sends redacted native endpoint hints for
     the selected PortAudio device; the sidecar selects the matching WASAPI
     capture endpoint by hash and refuses unsafe non-default fallback when no
     hash is available.
   - Still open: more physical evidence for favorite restore behavior,
     dock/USB/default-device transitions, and longer provider-backed sessions.
   - Implemented: `scripts/smoke_rust_audio_sidecar.py` records reusable JSON
     evidence for default WASAPI capture, selected native endpoint hash capture,
     first-frame timing, frame counts, sequence gaps, stop health, and sidecar
     writer metrics. Use a short run for local validation and `--duration-sec
     600` for the 10-minute physical stability gate.
   - Implemented: the smoke script itself now fails captures when requested
     prebuffer evidence is missing, sequence gaps occur, stop health is invalid,
     observed frame span is shorter than requested, or writer counts fall
     below the frames observed by the reader.
   - Implemented: when `--max-frames` is not explicitly set, the smoke script
     now derives an effective frame cap from duration, sample rate, block size,
     and requested prebuffer, so long 10-minute gates are not truncated by a
     short local-smoke default.
   - Implemented: the hybrid release-readiness runner and validator can now
     consume that JSON evidence through `-RunRustAudioSidecarSmoke`,
     `-UseExistingRustAudioSidecarReport`, and
     `-RequireRustAudioSidecarSmoke`. The Rust smoke is optional for standard
     Python-capture releases and becomes a hard gate only when evaluating Rust
     audio promotion.
   - Implemented: promotion smoke orchestration now requests a 400 ms Rust
     sidecar prebuffer by default and the readiness validator rejects reports
     where requested prebuffer frames are missing, arrive after live frames, or
     never transition to live frames.
   - Implemented: the real WASAPI sidecar writer now applies the same requested
     prebuffer frame boundary as the synthetic harness, sets `SAF1` prebuffer
     flags on the leading frames, and reports prebuffer/live writer counts in
     stop-health payloads.
   - Implemented: promotion validation now cross-checks reader and writer
     counts so written total/prebuffer/live frames cannot be lower than the
     frames observed through the pipe reader.
   - Local evidence from 2026-06-10: a direct physical Windows WASAPI smoke
     passed with
     `python scripts\smoke_rust_audio_sidecar.py --mode wasapi --duration-sec 600 --selected-duration-sec 10 --prebuffer-ms 400 --output tmp\rust-audio-sidecar-wasapi-10min-smoke.json`.
     The default capture reported `observedDurationSec=600.004`,
     `sequenceGapCount=0`, `prebufferAfterLiveCount=0`, and matching
     reader/writer frame counts. The selected native endpoint hash capture
     reported `observedDurationSec=10.009`, verified the selected hash, and
     also had no sequence gaps or prebuffer-after-live frames. This is strong
     sidecar evidence, but it does not replace the remaining physical matrix,
     provider-backed transcription and always-on prewarm evidence. It is now
     part of the standard live-mic capture path.
   - If Rust fails before the first frame, recording fails visibly with
     diagnostics; the Python capture fallback has been removed.
   - Implemented: if Rust stalls mid-session after audio has started, Python
     records `midSessionFailureReason`, opens a short fallback circuit for the
     next requested Rust session, and does not splice Python capture into the
     current utterance. Promotion gates reject reports that contain
     `midSessionFailureReason`, an unexpected `framePipeReaderEndReason`, or an
     open Rust fallback circuit.
4. Use a narrow control protocol:
   - Partly implemented: shared Rust/Python helpers define and validate the
     binary PCM frame header, and private shell IPC reserves
     `audioCaptureStart`/`audioCaptureStop` plus
     `audioPrewarmStart`/`audioPrewarmStop`.
   - Partly implemented: `scriber-audio-sidecar --stdio` accepts sidecar-local
     `captureStart`/`captureStop` and `prewarmStart`/`prewarmStop` commands
     using JSON Lines.
   - Partly implemented: Tauri shell IPC now routes start/stop requests through
     the sidecar client, retains successful capture sidecars by `streamId`, and
     returns redacted sidecar status to Python.
   - Partly implemented: the shell payload preserves sidecar response fields at
     top level so Python can open `framePipe` and associate `streamId`.
   - Start request includes sample rate, channels, block size, device preference
     (`default`, `favorite`, `portAudioLabel`, or `nativeEndpointHash`), and
     `prebufferMs`.
   - Implemented for WASAPI capture: successful sidecar responses
     include stream id, format, capture channels, hashed native endpoint id,
     endpoint-selection details, frame pipe, and resampler metadata.
   - Implemented: stop responses preserve sidecar health fields through Tauri
     shell IPC, and Python stores them in `RustPrototypeFrameSource`
     diagnostics, including total/prebuffer/live writer frame counts.
   - Implemented: `RustPrototypeFrameSource` records richer fallback reasons and
     frame-pipe reader counters for frames, bytes, sequence errors, protocol
     errors, first-frame read timing, and reader end reason.
   - Still open: physical packaging/smoke gates.
   - Each PCM frame uses the shared fixed-size binary header followed by
     `pcm_i16_le`.
5. Add prewarm parity only after active capture works:
   - Match configured prebuffer duration, frame ordering, adoption into active
     capture, no prebuffer/live-frame interleaving, pause during device refresh,
     and resume after active capture.
   - Partly implemented: the Python Rust frame source understands the
     `AUDIO_FRAME_FLAG_PREBUFFER` flag, preserves prebuffer frames before live
     frames, exposes prebuffer/live counters, and treats prebuffer-after-live as
     a protocol failure.
   - Partly implemented: Python now sends the configured bounded
     `MIC_PREBUFFER_MS` value as `prebufferMs` when starting the Rust WASAPI
     frame source, and diagnostics expose the requested value.
   - Partly implemented: synthetic Rust sidecar capture emits correctly flagged
     prebuffer frames before live frames when `prebufferMs` is requested, and
     `scripts/smoke_rust_audio_sidecar.py --prebuffer-ms ...` reports
     prebuffer/live frame counts plus any prebuffer-after-live ordering errors.
   - Partly implemented: WASAPI sidecar capture also flags the requested
     leading frames as prebuffer and exposes writer-side prebuffer/live counts.
     This validates frame ordering and promotion evidence, but it is not yet a
     full idle always-on Rust prewarm stream.
   - Implemented: the active Rust WASAPI path no longer adopts the Python
     always-on prewarm stream. This prevents `SCRIBER_AUDIO_ENGINE=rust-wasapi`
     plus `SCRIBER_MIC_ALWAYS_ON=1` from silently taking the Python capture path
     before Rust evidence can be collected.
   - Implemented: a Rust prewarm sidecar lifecycle harness exists behind
     private shell IPC. Synthetic mode validates long-lived prewarm session
     start/stop behavior and stop-health reporting. WASAPI mode starts a real
     passive Windows idle capture stream, uses the same default/native-endpoint
     selection rules as active Rust capture, and reports redacted endpoint,
     mix-format, observed-block, and buffered-frame counters.
   - Implemented: within one sidecar process, `captureStart` can take a
     matching `prewarmId`, stop that prewarm session with reason
     `adoptedIntoCapture`, snapshot the bounded PCM ringbuffer, and write those
     frames into the capture frame pipe as leading `AUDIO_FRAME_FLAG_PREBUFFER`
     frames before live WASAPI or synthetic frames.
   - Implemented: the backend now selects a Rust idle-prewarm manager when
     `SCRIBER_AUDIO_ENGINE=rust-wasapi` is requested. With
     `SCRIBER_MIC_ALWAYS_ON=1`, it keeps a Rust `audioPrewarmStart` session
     during idle, hands the `prewarmId` to the next Rust `audioCaptureStart`,
     and lets the sidecar adopt buffered frames locally. The Python
     `sounddevice` prewarm manager remains the default for normal releases.
   - Implemented: `scripts/smoke_rust_audio_prewarm_sidecar.py` records
     prewarm lifecycle evidence in `--mode synthetic` or `--mode wasapi`:
     `prewarmId`, start/stop timings, observed frame counters, buffered frame
     counters, mode-specific start payloads, and validation errors.
   - Implemented: `scripts/smoke_rust_audio_sidecar.py
     --prewarm-before-capture` proves sidecar-local prewarm adoption by
     starting prewarm, passing its `prewarmId` into `captureStart`, and
     validating adopted prebuffer frames before live frames.
   - Implemented: the hybrid release-readiness runner and validator can consume
     this report through `-RunRustAudioPrewarmSidecarSmoke`,
     `-UseExistingRustAudioPrewarmSidecarReport`, and
     `-RequireRustAudioPrewarmSidecarSmoke`; `-RustAudioPrewarmSidecarMode
     wasapi` exercises the real passive WASAPI idle stream. This is
     intentionally separate from `-RequireRustAudioSidecarSmoke` because it
     still does not prove buffered idle audio adoption into active capture.
   - Implemented: the hybrid release-readiness runner can add prewarm adoption
     evidence to the physical sidecar smoke with
     `-RustAudioSidecarPrewarmBeforeCapture`; the validator rejects such reports
     when the default capture does not show positive adopted prewarm blocks.
     Supplying this flag now makes the sidecar smoke report required even
     without the generic `-RequireRustAudioSidecarSmoke` flag.
   - Implemented: `scripts/smoke_rust_audio_app_prewarm.py` exercises the
     app-level Python lifecycle around the real sidecar. It starts
     `RustAudioPrewarmManager`, waits for idle buffering, attaches
     `RustPrototypeFrameSource` with the adopted `prewarmId`, validates
     prebuffer-before-live ordering, resumes idle prewarm after capture, and
     redacts raw prewarm IDs in the report. It can now run repeated
     prewarm-adoption / capture / stop / idle-resume cycles with
     `--capture-cycles`, preserving each cycle in the evidence report. The
     default smoke ignores locally configured favorite microphones so release
     evidence covers the stable Windows default endpoint; `--honor-favorite-mic`
     is reserved for targeted selected-device investigations.
   - Implemented: the hybrid release-readiness runner and validator can consume
     the app-level report through `-RunRustAudioAppPrewarmSmoke`,
     `-UseExistingRustAudioAppPrewarmReport`, and
     `-RequireRustAudioAppPrewarmSmoke`. Required app-level readiness expects
     WASAPI mode, default-endpoint evidence, positive adopted/prebuffer/live
     frame counts, a native endpoint hash, successful idle-prewarm resume, and
     zero sequence/protocol/prebuffer-ordering errors. It also rejects
     `midSessionFailureReason`, `fallbackReason`, non-empty `lastError`, and
     final frame-pipe reader states other than empty, `stopRequested`, or
     `endOfStream`, so a report with a broken Rust reader cannot pass on
     adoption counters alone.
   - Implemented: final readiness can also require long app-level Rust
     Always-On-Mic evidence with
     `-MinRustAudioAppPrewarmDurationSec` and
     `-MinRustAudioAppPrewarmPrewarmDurationSec`, and it can require repeated
     stop/resume coverage with `-MinRustAudioAppPrewarmCaptureCycles`. This
     makes the 10-minute active-capture / 30-minute idle-prewarm promotion
     target machine-checkable instead of relying on a short smoke report.
     Supplying either app-prewarm minimum duration or minimum capture-cycle
     count now makes the app-prewarm report required even without the generic
     `-RequireRustAudioAppPrewarmSmoke` flag. The aggregate
     `-RequireRustAudioPromotionReadiness` gate raises the cycle requirement to
     two, so promotion evidence must prove idle-prewarm resume after repeated
     active recording stops. Final readiness validates each cycle's
     pre-adoption and post-resume `audioPrewarmStatus` snapshot rather than
     trusting a final aggregate healthy state.
   - Implemented: final readiness can require an installed live-recording smoke
     with `-RequireInstalledLiveRecordingSmoke` and
     `-MinInstalledLiveRecordingDurationSec`. The validator accepts reports from
     the installed desktop smoke path and requires a `tauri-supervised`
     sidecar runtime, healthy API version/ready state, positive app/backend
     process and port metadata, clean start/stop state, verified cleanup, sufficient
     stability-sample coverage for the requested duration, and zero
     non-recording samples during active capture. A configured minimum duration
     now makes the installed live-recording report required even without the
     generic `-RequireInstalledLiveRecordingSmoke` flag. This is the live-mic
     app/installer gate. For Rust/WASAPI release evidence, the runner also sets
     `-RequireInstalledLiveRecordingRustAudio`, which requires every stability
     sample to include compact audio diagnostics proving `rust-wasapi`
     `rust-frame-pipe` capture, adopted Rust prewarm evidence via
     `activeCapture.rustPrewarmAdoption.adopted=true` plus a redacted prewarm
     hash, active callbacks, closed fallback circuit, and clean frame-pipe
     counters. The aggregate Rust-promotion runner now also
     enables `-InstalledLiveRecordingMicAlwaysOn`, and the validator requires
     `liveRecording.micAlwaysOn=true` plus
     `audioDiagnostics.microphone.micAlwaysOn=true` in every stability sample.
     Installed Rust evidence therefore proves the Always-On-Mic path,
     not only an on-demand live recording. The installed smoke now also records
     `liveRecording.postStopAudioDiagnostics` after Stop and idle-state
     confirmation, and Rust promotion validation requires the idle Rust prewarm
     to be active there with positive resume-ready count, zero resume failures,
     and non-negative stop-to-prewarm-ready gap metrics. This makes the
     observed microphone privacy-indicator off/on transition after Stop part of
     the installer-path evidence. The validator now also requires the Rust
     callback, frame-pipe, and audio-frame counters to increase across
     stability samples, so a stale diagnostics snapshot cannot satisfy the
     long-recording gate. Installed Rust-audio samples must also show
     `activeCapture.healthRestartCount=0`,
     `activeCapture.healthRestartThrottleCount=0`, and empty active-capture
     health failure/restart-error fields, so a recovered capture stall cannot
     pass as stable installed evidence. Provider-backed transcript quality and
     latency still require the separate Python/Rust hot-path comparison
     artifact.
   - Implemented: installed live-recording smoke runners can now request
     `SCRIBER_AUDIO_ENGINE=rust-wasapi` plus the explicit Rust sidecar
     capture mode via `-LiveRecordingAudioEngine rust-wasapi
     -LiveRecordingRustAudioCaptureMode wasapi -LiveRecordingMicAlwaysOn` on the desktop/installer smoke
     and `-InstallerLiveRecordingAudioEngine rust-wasapi
     -InstallerLiveRecordingRustAudioCaptureMode wasapi
     -InstallerLiveRecordingMicAlwaysOn` on the build wrapper.
     This makes the installed Rust-audio report producer explicit instead of
     relying on manual environment setup.
   - Implemented: installed live-recording smoke runners can now load provider
     credentials from an explicit env file and override the live STT provider
     without printing secret values:
     `-LiveRecordingEnvFile .env -LiveRecordingDefaultStt soniox
     -LiveRecordingSonioxMode realtime` on desktop/installer smoke,
     `-InstallerLiveRecordingEnvFile .env
     -InstallerLiveRecordingDefaultStt soniox
     -InstallerLiveRecordingSonioxMode realtime` on the build wrapper, and
     `-InstalledLiveRecordingEnvFile .env
     -InstalledLiveRecordingDefaultStt soniox
     -InstalledLiveRecordingSonioxMode realtime` on the release-readiness
     runner. This closes the previous reproducibility gap where a temporary
     smoke data directory could fall back to `soniox` without a loaded API key
     even though the real developer/release environment had credentials.
     The runner now also sets `-InstalledLiveRecordingMicAlwaysOn` whenever
     `-RequireInstalledLiveRecordingRustAudio` is used, so producer flags match
     the validator's Rust-promotion requirements.
   - Implemented: the hybrid readiness runner can now produce the installed
     live-recording artifact directly with `-RunInstalledLiveRecordingSmoke`
     and `-InstalledLiveRecordingInstallerPath`, or reuse an existing artifact
     with `-UseExistingInstalledLiveRecordingSmokeReport`.
   - Local evidence from 2026-06-10: a direct Windows WASAPI prewarm smoke
     passed with
     `python scripts\smoke_rust_audio_prewarm_sidecar.py --mode wasapi --duration-sec 0.5 --prebuffer-ms 400 --output tmp\rust-audio-prewarm-sidecar-wasapi-current.json`.
     It reported `source=wasapi-prewarm`, `wasapiPrewarm=true`, a redacted
     native endpoint hash, `totalBlocksObserved=42`, and
     `bufferedBlocks=40`.
   - Local evidence from 2026-06-10: a direct Windows WASAPI adoption smoke
     passed with
     `python scripts\smoke_rust_audio_sidecar.py --mode wasapi --duration-sec 0.5 --prebuffer-ms 400 --prewarm-before-capture --prewarm-duration-sec 0.5 --skip-selected-hash --output tmp\rust-audio-sidecar-adopt-wasapi-current.json`.
     It reported `totalAdoptedPrewarmBlocks=40`,
     `prebufferFramesRead=40`, `liveFramesRead=43`,
     `prebufferAfterLiveCount=0`, and `sequenceGapCount=0`.
   - Local evidence from 2026-06-11: the Rust sidecar Windows WASAPI promotion
     smoke passed with the release `scriber-audio-sidecar.exe` built from the
     current branch:
     `python scripts\smoke_rust_audio_sidecar.py --sidecar-exe Frontend\src-tauri\target\release\scriber-audio-sidecar.exe --mode wasapi --duration-sec 600 --selected-duration-sec 10 --prebuffer-ms 400 --prewarm-before-capture --prewarm-duration-sec 0.5 --output tmp\hybrid-baseline\rust-audio-sidecar-smoke.json`.
     It reported a 600.003 second default capture, a 10.008 second selected
     native-endpoint-hash capture, `selectedHashVerified=true`, matching
     `totalFramesRead=61035` and `totalFramesWritten=61035`, zero sequence
     gaps, zero prebuffer-after-live frames, `totalAdoptedPrewarmBlocks=34`,
     and `adoptedPrewarm.handoffMode=overlap-capture-start-before-prewarm-stop`.
     A focused `validate_hybrid_release_readiness.py` invocation marked
     `rustAudioSidecarSmoke.ok=true` for this report with
     `--min-rust-audio-duration-sec 600` and
     `--require-rust-audio-sidecar-prewarm-adoption`; the overall aggregate
     remained red only because unrelated release/signing/hardware evidence was
     intentionally not supplied in that focused check.
   - Local evidence from 2026-06-11: the app-level Windows WASAPI prewarm
     adoption smoke passed with
     `python scripts\smoke_rust_audio_app_prewarm.py --mode wasapi --duration-sec 10 --prewarm-duration-sec 2 --post-resume-duration-sec 2 --output tmp\rust-promotion-evidence\rust-audio-app-prewarm-wasapi-10s-default-endpoint-fix.json`.
     It reported `adoptedPrewarmBlocks=40`, `prebufferFramesRead=40`,
     `liveFramesRead=992`, `prebufferAfterLiveCount=0`,
     `sequenceErrorCount=0`, `protocolErrorCount=0`,
     `framePipeFirstFrameReadMs=10.186`, and successful idle-prewarm resume.
     The same run showed `endpointSelection.mode=default` and
     `usedDefaultEndpoint=true`.
   - Implemented: an unfavorited default microphone request is passed through
     to the Rust sidecar as
     `devicePreference=default` with no native endpoint hash. Rust then opens
     the real Windows default capture endpoint with WASAPI, which matches the
     visible Windows microphone privacy indicator. Explicit or favorite
     non-default devices still use the redacted native endpoint hash path and
     fail closed if no native endpoint hash can be resolved. The Python
     `RustAudioPrewarmManager` now also tries the private Tauri shell-IPC
     `audioEndpointInventory` payload when Python/PyCAW native inventory is
     empty, so selected/favorite microphone prewarm can use the same redacted
     endpoint inventory as Tauri diagnostics. If a favorite resolves to a
     concrete PortAudio device but no native endpoint hash can be found, the
     manager no longer attaches default-device metadata or silently opens the
     Windows default microphone.
   - Local evidence from 2026-06-11: a targeted
     `--honor-favorite-mic` smoke with `Mikrofon (4- Insta360 Link)` exposed a
     previous unsafe fallback where the Rust prewarm path opened
     `Microphone (Jabra Engage 75)` as the Windows default despite resolving
     the favorite to PortAudio index `11`. After the fix, the standalone
     sidecar smoke fails closed with `devicePreference=11` and no
     `portAudioLabel`/native hash instead of opening Jabra. A rebuilt Tauri
     backend was then verified with
     `tmp\rust-promotion-evidence\20260611-161011\tauri-insta360-rust-prewarm-smoke.json`.
     The support-bundle `audio-diagnostics.redacted.json` showed
     `favoriteMic=Mikrofon (4- Insta360 Link)`, `prewarm.engine=rust-wasapi`,
     `prewarm.active=true`, `signature.device_preference=11`,
     `signature.port_audio_label=Mikrofon (4- Insta360 Link)`,
     matching `native_endpoint_id_hash=51112d9ccdd3a140`, and
     `endpointSelection.usedDefaultEndpoint=false`. This proves the Tauri
     shell-IPC endpoint inventory can map the Insta360 favorite to a native
     WASAPI endpoint instead of opening the Windows default microphone.
   - Local evidence from 2026-06-11: the installed Rust/WASAPI Always-On-Mic
     live-recording smoke passed with
     `tmp\rust-promotion-evidence\installed-live-recording-rust-wasapi-alwayson-30s-default-endpoint-fix-v2.json`.
     It verified 30 seconds of installed `tauri-supervised` recording with
     `SCRIBER_AUDIO_ENGINE=rust-wasapi`, `frameSource=rust-frame-pipe`,
     increasing callback and frame-pipe counters, no sequence/protocol/prebuffer
     ordering errors, `rustAudioFallbackCircuit.open=false`, and
     `sourceEndpointSelectionMode=default` /
     `sourceEndpointSelectionUsedDefault=true`.
   - Implemented: the recording hot-path benchmark can now be run as a strict
     provider-backed Rust evidence gate. `--require-provider-transcript`
     requires a final STT provider transcript, and `--require-rust-audio-engine`
     verifies that `/api/runtime/audio-diagnostics` saw an active
     `rust-wasapi` `rust-frame-pipe` capture during recording. The hybrid
     baseline runner exposes these as
     `-RequireRecordingHotPathProviderTranscript` and
     `-RequireRecordingHotPathRustAudio`.
     The Rust requirement now also fails when
     `microphone.rustAudioFallbackCircuit.open` is true in the report-level or
     during-recording diagnostics, so a run that fell back to Python during the
     Rust cooldown cannot be used as promotion evidence.
     When the Rust hot-path report shows `micAlwaysOn=true`, the
     `rust_audio_engine` requirement now also requires redacted
     `activeCapture.rustPrewarmAdoption` evidence before it is marked
     `measured`.
   - Implemented: `scripts/validate_recording_hot_path_comparison.py` builds a
     machine-checkable Python-vs-Rust provider-backed comparison artifact from
     two recording hot-path reports. It rejects validate-only evidence by
     default, requires provider transcript evidence from the same STT provider
     in both reports, requires the same benchmark configuration through the
     report-level `requested` object, requires active
     `rust-wasapi`/`rust-frame-pipe` capture in the Rust report, rejects an
     open Rust fallback circuit in that report, requires `micAlwaysOn=true`
     evidence in the Rust report, requires each Rust `rust-frame-pipe` sample to
     prove `activeCapture.rustPrewarmAdoption.adopted=true` with a redacted
     prewarm hash and no raw prewarm ID, requires positive callback,
     frame-pipe frame, and audio-frame counters for each Rust `rust-frame-pipe`
     sample, rejects any reported dropped active-capture frames, rejects
     active-capture watchdog restarts or lingering active-capture health errors
     during the Rust run, and
     records segment deltas such as hotkey-to-first-frame, provider-finalize,
     and stop-to-text-injection.
     The final readiness validator and
     `scripts/run_hybrid_release_readiness.ps1` can require this artifact with
     `--require-recording-hot-path-comparison` /
     `-RequireRecordingHotPathComparison`.
     The final readiness validator now requires the comparison artifact's
     `sameProvider`, `sameRecordingConfig`, `rustFallbackCircuitClosed`, and
     `rustAlwaysOnMic` checks alongside `rustAudioEngine`,
     `rustMidSessionClean`, `rustFramePipeFlow`, `rustNoDroppedFrames`,
     `rustActiveCaptureStable`, and `rustPrewarmAdoption`, and it requires at
     least three samples per engine plus no clear P95 regression in local
     audio-owned segments.
     This gate cannot be bypassed by passing a stale, one-shot, mismatched, or
     clearly slower comparison schema.
     The comparison validator also rejects unredacted source reports containing
     raw `SWD\MMDEVAPI\...` endpoint IDs, raw `\\.\pipe\scriber-*` pipe names,
     or non-redacted token fields, so sensitive hot-path evidence cannot become
     promotion input. Final hybrid readiness requires that
     `inputReportRedaction`, `rustMidSessionClean`,
     `rustFramePipeFlow`, `rustNoDroppedFrames`, `rustActiveCaptureStable`, and
     `rustPrewarmAdoption` checks be present and passing, so stale comparison
     artifacts from before this gate fail Rust promotion.
     Provider-finalize and total stop-to-text values remain reported but are not
     part of the local-audio regression gate because they are dominated by
     network/STT provider latency.
   - Implemented: `scripts/run_recording_hot_path_comparison.ps1` orchestrates
     the full provider-backed A/B evidence path. It runs
     `measure_hybrid_baseline.ps1` once with a legacy
     `SCRIBER_AUDIO_ENGINE=python` request and once with Rust/WASAPI, then calls
     the comparison validator to produce
     `recording-hot-path-python-rust-comparison.json`. This remains useful for
     historical/pre-promotion builds that still contain Python capture. Current
     builds ignore the legacy Python request for live capture.
     Non-plan runs now fail early without `-RustAlwaysOnMic`, because the
     comparison validator requires both Always-On-Mic and adopted Rust prewarm
     evidence.
     The runner defaults to three recording samples per engine and passes that
     as the minimum accepted sample count to the validator.
     It also passes a default 50 ms max P95 regression tolerance for local
     audio-owned hot-path segments.
     For reproducible provider-backed runs it accepts
     `-RecordingHotPathEnvFile`, `-RecordingHotPathDefaultStt`, and
     `-RecordingHotPathSonioxMode`, applying those settings to both the Python
     and Rust passes while keeping secret `.env` values out of PlanOnly output
     and reports.
   - Implemented: `scripts/run_hybrid_release_readiness.ps1` can now produce
     that comparison artifact directly with `-RunRecordingHotPathComparison`.
     The aggregate runner invokes
     `scripts\run_recording_hot_path_comparison.ps1 -RustAlwaysOnMic`, writes
     the comparison JSON into the hardware input directory, forwards the same
     sanitized recording-hot-path provider flags when supplied, and then feeds
     that report into final readiness validation. Without the run flag, the
     comparison remains an explicit external evidence requirement.
   - Implemented: the same aggregate runner can now produce the physical
     microphone matrix with `-RunMicrophoneHardwareMatrix`, passing through the
     USB/dock/Bluetooth/favorite labels, Rust endpoint-inventory requirement,
     and native device-refresh requirement to
     `scripts\run_microphone_hardware_matrix.ps1` before final validation. It
     rejects forced per-poll refresh when native device-refresh evidence is
     required, so Rust promotion evidence cannot be satisfied by the legacy
     polling diagnostic mode.
   - Still open: more long physical Always-On-Mic evidence with the Rust
     manager, device-refresh pause/resume matrix evidence, signing/updater
     publication evidence, and release hardening of the current Rust default.
     The historical `-RequireRustAudioPromotionReadiness` aggregate switch still
     documents the full evidence bundle and can be reused as an extended
     regression gate.
   - Rust prewarm is the default path.
6. Add watchdog and restart parity:
   - Mirror existing Python active-capture and prewarm diagnostics: stream
   active, callback count, last frame age, last status/error, dropped frames,
   restart count, endpoint hash, format, prebuffer duration, sidecar PID, and
   sidecar exit status.
   - Partly implemented: the Rust frame source can reopen a fresh sidecar after
     a watchdog-style `stop(close=false)`, and diagnostics include sidecar start
     count, restart count, reader-thread liveness, stop reason, exit status,
     writer connection state, total/prebuffer/live frames written, bytes
     written, frame-pipe read counters, sequence/protocol error counts,
     first-frame read timing, and reader end reason.
   - Implemented: `MicrophoneInput.ensure_stream_health()` now treats an
     inactive source-owned frame source during active recording as restartable
     health failure, calls `stop(close=false)` first to release stale Rust
     `streamId`/frame-pipe state, and then starts a fresh source. Top-level
     active-capture diagnostics include health restart count, last health-check
     reason, last restart reason, and last restart error.
   - Implemented: active-capture watchdog diagnostics now also record the
     concrete health failure reason, restart-throttle count, throttled reason,
     and remaining throttle interval. A stale active stream no longer reports
     healthy merely because a restart is still inside the minimum restart
     interval; it returns unhealthy so the backend emits a diagnostic warning
     without spam-restarting the stream.
   - Implemented: Rust frame-pipe failures after the first callback now record
     `midSessionFailureReason`. When the active-capture watchdog observes such
     a source-owned Rust failure, it opens a short Rust fallback circuit. The
     current recording still does not splice to another engine mid-utterance.
     The next live-mic session fails fast while the short Rust circuit is open
     and records `rustCircuitOpen:<reason>` in diagnostics.
   - Implemented: `/api/runtime/audio-diagnostics` also exposes the global
     fallback circuit as `microphone.rustAudioFallbackCircuit`, even when there
     is no active pipeline. Support bundles preserve the same redacted circuit
     state, so a post-failure idle bundle can explain why the next recording was
     blocked before capture.
   - Implemented: Rust Always-On-Mic prewarm now has a real status path. Tauri
     and the audio sidecar expose `audioPrewarmStatus`; the Python
     `RustAudioPrewarmManager` watchdog queries it instead of trusting a cached
     `prewarmId`. If the sidecar process exited, the worker finished, or the
     sidecar reports `active=false`, Python clears stale state and restarts the
     idle prewarm session. `/api/runtime/audio-diagnostics` now includes
     redacted status payloads plus start/stop/health response times, last
     inactive reason, restart counters, and a bounded redacted `recentEvents`
     timeline for start/stop/adoption/watchdog restarts, which makes brief
     microphone privacy-indicator dropouts diagnosable in support bundles
     without increasing steady-state log volume. The Rust manager also records
     `lastActiveCaptureResumeGapMs`, `lastActiveCaptureStopToReadyMs`, and
     `maxActiveCaptureStopToReadyMs`, so the visible off/on gap after a user
     stops an active recording is measured instead of inferred. The watchdog
     now also records and logs `missingPrewarmSession` when a previously active
     Rust idle session disappears before a status query can be made. The initial
     startup activation is not counted as a health
     restart, so these counters point to real post-start interruptions. The
     app watchdog now snapshots recovered idle-prewarm restarts under
     `watchdog.lastWarning` when `healthRestartCount` increases, even if
     `ensure_healthy()` successfully restored the stream in the same check.
   - Implemented: the app-level Rust prewarm smoke now exercises that status
     path before capture adoption and after idle resume. Final hybrid readiness
     rejects Rust app-prewarm reports that lack active `audioPrewarmStatus`
     evidence, redacted prewarm IDs, health response timing, an empty health
     error, `healthRestartCount=0`, or the expected bounded redacted
     `recentEvents` lifecycle markers (`started` before adoption and
     `adopted_for_capture` / `resume_active_capture` / `started` after idle
     resume). Post-resume snapshots must also include positive
     `activeCaptureResumeReadyCount` and non-negative resume-gap timing fields.
     This prevents stale cached `prewarmId` state, a recovered idle-session
     dropout, or an unmeasured stop-to-prewarm gap from satisfying the
     Always-On-Mic promotion gate.
   - Implemented on 2026-06-11 and extended on 2026-06-16: Rust active
     capture, Rust prewarm, and passive Rust probe selection now prefer the
     private Tauri shell-IPC `audioEndpointInventory` payload over Python-local
     endpoint inventory when resolving favorite/non-default microphones. This
     prevents a Python-only endpoint hash from being sent to the Rust sidecar
     when both layers name the same physical microphone but derive different
     redacted hashes. The targeted Insta360 investigation reproduced the
     failure with `Mikrofon (4- Insta360 Link)` and confirmed the fixed active
     capture uses the Rust/Tauri endpoint hash `51112d9ccdd3a140` instead of
     the stale Python-local hash.
   - Implemented on 2026-06-11 and tightened on 2026-06-29: Rust prewarm
     adoption overlaps idle prewarm with the next WASAPI capture instead of
     stopping prewarm before the live stream exists. The 2026-06-29 decision is
     that adopted WASAPI capture owns the old `PrewarmSession` until the capture
     writer has written adopted prebuffer blocks and successfully called
     `IAudioClient.Start()` on the replacement capture client. Only then may the
     writer stop prewarm with reason `adoptedIntoCapture`. If pipe creation,
     writer startup, or live-capture handoff fails first, the deferred session
     must be stopped with an explicit failure reason such as
     `captureStartFailed` or `captureWriterFinishedBeforePrewarmHandoff`.
     This avoids the observed case where `SCRIBER_MIC_ALWAYS_ON=1` still showed
     a brief Windows microphone privacy-light off/on blink after several idle
     minutes because the parent command handler stopped prewarm before the new
     WASAPI client was actually live. The design deliberately favors minimum
     always-on hotkey latency and privacy-indicator continuity over releasing
     the idle microphone between recordings. It is still an overlap of two
     shared WASAPI clients for a short handoff window, not a same-stream
     transfer.
   - Provider-backed Rust-only evidence on 2026-06-11:
     `tmp\rust-promotion-evidence\rust-only-provider-after-overlap-handoff-provider-confirm-recording-hot-path-1.json`
     passed with Azure MAI provider transcript, `engine=rust-wasapi`,
     `frameSource=rust-frame-pipe`, no Python fallback, no dropped frames,
     favorite mic `Mikrofon (4- Insta360 Link)`, active endpoint hash
     `51112d9ccdd3a140`, adopted prewarm, and
     `hotkey_received_to_first_audio_frame_ms` about `126 ms`.
   - Still open: physical proof that this restart/cooldown policy behaves well
     during real long recordings and dock/USB/default-device transitions.
     The first one-sample Python-vs-Rust provider comparison after the endpoint
     fix proved active Rust capture and prewarm adoption, but failed the strict
     audio-owned latency no-regression gate by about 100 ms on first audio.
     More repeated A/B evidence is required before Rust audio can be promoted.
7. Run A/B measurements before any promotion:
   - hotkey to first audio frame,
   - hotkey to first audible audio frame,
   - stop to last chunk sent,
   - idle/live CPU and memory,
   - dropped frames,
   - 30-minute idle always-on stability,
   - 10-minute live recording stability.
   - Partly implemented: the Rust sidecar smoke captures the Rust-side
     first-frame and frame-pipe metrics needed for the Rust half of this
     comparison. The recording hot-path benchmark now has strict gates for a
     final provider transcript and active Rust capture diagnostics, and the
     comparison validator now turns Python/Rust recording reports into a
     promotion-ready A/B artifact. Real provider-backed A/B artifacts are still
     required before promotion.
8. Promote to default only if physical hardware tests show fewer interruptions
   or a meaningful latency win. Otherwise keep Python as default and retain the
   Rust/WASAPI path for future investigation.

Acceptance gates:

- Existing Python mic tests continue passing.
- New Rust unit tests for endpoint selection/default fallback, format
  negotiation, frame header encode/decode, monotonic sequence numbers, bounded
  buffer/drop accounting, sidecar start/stop lifecycle, watchdog restart
  throttling, and redacted diagnostics.
- New Python `AudioFrameSource` contract tests, Rust frame-source fallback
  tests, diagnostics schema tests, and support-bundle redaction tests.
- Installed live-mic smoke with visible waveform and successful transcription.
- For extended Rust-audio regression evidence: run
  `scripts\run_hybrid_release_readiness.ps1 -RequireRustAudioPromotionReadiness`
  with either matching `-Run...` flags or validated existing reports. This was
  the canonical aggregate gate before changing defaults and is still useful as
  a broad release hardening gate; individual Rust gates remain available for
  focused investigations.
- For Rust audio sidecar hardening: 10-minute physical WASAPI sidecar smoke via
  `scripts\run_hybrid_release_readiness.ps1 -RunRustAudioSidecarSmoke
  -RequireRustAudioSidecarSmoke`, including default capture, selected native
  endpoint hash capture, requested prebuffer delivery, no prebuffer-after-live
  interleaving, observed default-capture frame span meeting the requested gate,
  reader/writer frame-count consistency, no sequence gaps, and valid
  stop-health metrics. Add `-RustAudioSidecarPrewarmBeforeCapture` to require
  sidecar-local prewarm adoption evidence in the default capture. The readiness
  runner passes `--require-rust-audio-sidecar-prewarm-adoption` when that mode
  is enabled, so reused sidecar reports without adopted prewarm blocks fail
  validation instead of silently satisfying the promotion gate. The readiness
  runner also requires Rust/WASAPI endpoint inventory evidence in the physical
  microphone matrix when this promotion gate is enabled.
- Rust prewarm lifecycle smoke via
  `python scripts\smoke_rust_audio_prewarm_sidecar.py --mode wasapi --duration-sec 1
  --prebuffer-ms 400 --output tmp\rust-audio-prewarm-sidecar-smoke.json`
  or via the readiness runner flags `-RunRustAudioPrewarmSidecarSmoke` and
  `-RequireRustAudioPrewarmSidecarSmoke` proves prewarm sidecar start/stop
  plumbing and stop-health counters. Add `-RustAudioPrewarmSidecarMode wasapi`
  to the runner for the real passive WASAPI idle stream. Pair it with app-level
  Always-On-Mic lifecycle and provider-backed transcription evidence when
  hardening a release.
- Physical mic matrix across built-in, USB, Bluetooth, docked, undocked, and
  Windows default-device changes, with `--require-rust-endpoint-inventory` and
  `--require-device-refresh-evidence` validation for Rust promotion. The matrix
  smoke now observes `/api/microphones` by default and relies on native events
  or sparse fallback polling; `--force-refresh-each-poll` exists only as an
  explicit legacy fallback because it can mask over-aggressive PortAudio
  refresh behavior. For Rust promotion, the matrix validator now also requires
  positive native Tauri refresh-hint and native-hint PortAudio-refresh deltas,
  so legacy Python/native monitor events cannot satisfy the Rust-native event
  evidence by themselves. The matrix validator also rejects raw IMMDevice
  endpoint IDs, raw `\\.\pipe\scriber-*` pipe names, and unredacted token fields
  anywhere in the hardware artifact.
- No feature loss for provider streaming, final transcript injection, overlay,
  audio diagnostics, support bundles, and fallback settings.
- Backend restart and Tauri exit clean up the Rust audio sidecar. Cleanup now
  uses bounded waits; if a Rust audio sidecar does not exit after
  `captureStop`/`prewarmStop` plus `shutdown`, Tauri kills it instead of
  hanging backend restart or shell exit. Stop diagnostics expose
  `sidecarKilledAfterTimeout` and `sidecarWaitError` through Python audio
  diagnostics/support bundles.

Rollback:

- The old Python `sounddevice` capture/prewarm implementation was removed from
  the normal product path. Rolling back capture now means reverting the Rust
  promotion change, not toggling an environment variable.
- `SCRIBER_AUDIO_ENGINE` is diagnostic compatibility only and does not select
  Python capture.
- If the Rust audio sidecar fails before first audio frame, the recording fails
  visibly and preserves diagnostics. Do not silently corrupt frame ordering by
  switching engines mid-stream.
- Keep `sounddevice` packaged only for remaining microphone listing and
  PortAudio-to-native endpoint mapping helpers until those are fully native.

Risks:

- IMMDevice IDs do not naturally match current PortAudio device names.
- WASAPI shared-mode devices often expose 48 kHz float even when the provider
  wants 16 kHz mono `i16`; resampling quality and CPU must be measured.
- Bluetooth devices can change format or latency after reconnect.
- Dock/default-device changes can race active capture.
- Cross-process real-time IPC can introduce drops or latency if backpressure is
  handled poorly.
- A Rust capture crash inside the main Tauri process would take down the shell;
  keep prototype capture isolated in a sidecar until proven.
- Always-on mic behavior affects user privacy indicators. Do not increase idle
  mic activation beyond the existing setting. When that setting is enabled,
  minimize hotkey latency and keep the privacy indicator continuous across
  idle-prewarm-to-live-capture handoff; do not reintroduce a fixed prewarm
  timeout or parent-handler prewarm stop that makes the indicator blink.

## Current Direction

The best path forward is to keep the hybrid architecture:

- Rust/Tauri for desktop shell, process supervision, hotkey, autostart, tray,
  updater, native device event hints, native WASAPI live capture/prewarm,
  optional native text injection, and Windows integration.
- Python for recording state, microphone list semantics, PortAudio refresh and
  endpoint mapping helpers, provider pipeline, Pipecat frame flow, media
  preparation, persistence, REST/WebSocket contracts, and diagnostics
  aggregation.
- React for the app UI.

Near-term Rust work should harden the promoted WASAPI path: selected-device
evidence, dock/USB/default-device transitions, longer Always-On-Mic runs,
restart diagnostics, and eventually moving more device-listing/mapping helpers
out of `sounddevice`.
