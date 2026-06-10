# Performance And Packaging

Last verified: 2026-06-10

This document consolidates the previous performance, startup, mic, FFmpeg,
installer-size, and optimization notes.

## Current Baseline

Latest validated local Profile B installer build from 2026-06-09:

- Installer: about `102.98 MiB`.
- Installed app: about `267.28 MiB`.
- Backend resource tree: about `254.42 MiB`.
- Installed media tools: about `5.84 MiB`.
- Profile B portable media-tool build: about `4.98 MiB` for `ffmpeg.exe`,
  `ffprobe.exe`, and required runtime DLLs.
- Python tests: `465 passed`.
- Frontend type check and build passed.
- Installed frontend smoke passed.
- Installed media-preparation smoke passed `5/5`.
- Real installed file and YouTube workflow smoke previously passed `2/2` for
  the Profile B path with `https://www.youtube.com/watch?v=0wEjbSYNUM8`.

Historical comparison points:

- Full/Gyan-style media tooling produced much larger installed packages.
- Gyan Essentials remains a fallback path, not the default.
- Removing ffprobe is still an experiment because standard workflows expect
  ffprobe availability.

## Implemented Performance Work

Startup and imports:

- STT provider imports are mostly lazy in the service factory.
- Expensive VAD/analyzer setup is cached.
- Runtime import diagnostics are separated from normal readiness paths where
  possible.

Live mic:

- Optional idle mic prewarm keeps a PortAudio stream ready.
- Rolling prebuffer can prepend the latest audio frames when recording starts.
- Device-name/favorite resolution has a short TTL cache.
- Device refresh is deferred while a recording stream is active.
- Audio-level UI work is throttled to about 60 Hz.
- Live waveform uses Canvas/RAF instead of per-frame React state.

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
- Frontend dist timestamp changes alone should not invalidate the sidecar cache.
- Runtime dependency footprint gate rejects SciPy reintroduction and unused heavy
  runtime paths.

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
  -UseProfileBFfmpeg `
  -ValidateSlimMediaTools `
  -ReuseSidecarIfUnchanged `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke
```

What it does:

- Syncs versions.
- Builds frontend.
- Reuses sidecar when inputs are unchanged.
- Builds Tauri/NSIS.
- Runs size and runtime dependency footprint gates.
- Runs selected installed-package smokes.

Release workflow:

- `.github/workflows/release-windows.yml` builds Profile B with MSYS2/UCRT64.
- It passes the produced media tools into `scripts/build_windows.ps1`.
- It collects size, media-preparation, runtime dependency, and timing evidence.
- Authenticode and Tauri updater signing gates are available but require real
  signing/updater secrets.

## Size Decisions

No-feature-loss constraints:

- No optional installer components.
- ffmpeg and ffprobe remain bundled in the standard installer.
- PySide6 remains bundled for the native mic overlay.
- Provider dependencies remain bundled when the provider is still supported by
  backend or UI configuration.
- Local ASR packaging remains separate from the standard cloud-provider sidecar.

Current packaging choices:

- `pyloudnorm` is provided locally without SciPy in the packaged sidecar.
- ONNXRuntime remains because Pipecat Silero VAD needs it.
- SciPy, Torch, NeMo, ONNX-ASR, ONNX tooling, numba, llvmlite, and unused
  ONNXRuntime tooling are excluded from the standard sidecar.
- Pillow AVIF binaries are excluded.

PySide6 size experiments are allowed only with installed overlay smoke evidence:

- prune translations,
- prune unused plugins,
- prune software OpenGL DLL.

Do not make those default without proving that the native overlay still works.

## Remaining Performance Opportunities

Highest-value current opportunities:

- Split stop-to-text latency into provider finalize, clipboard, and paste
  segments before optimizing locally.
- Run React Profiler on Live Mic during recording to catch broad subtree
  rerenders from status/history WebSocket events.
- Keep investigating UI responsive behavior at narrow widths.
- Add longer installed idle/live stability runs with CPU and memory budgets.
- Consider PySide6 pruning only after visible overlay smoke coverage is robust.

Lower-value or risky opportunities:

- yt-dlp extractor filtering. This is risky and should not be default.
- Removing ffprobe. This is an explicit experiment only.
- Rust audio engine. Treat as requested-only until a measured prototype beats
  the current Python audio path without maintainability loss.

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
   - `SCRIBER_AUDIO_ENGINE=python|rust-prototype`
   - Defaults stay on current behavior until installed Windows acceptance gates
     pass.
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

Do not remove `pycaw`/`comtypes`, `keyboard`/`pyautogui`/Tkinter fallbacks, or
`sounddevice` until the relevant Rust replacement has passed installed Windows
smokes and physical-device coverage. For audio capture, keep `sounddevice`
available even after a Rust prototype exists unless a later release plan proves
there is no support or compatibility loss.

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
- Added focused Python tests for render filtering, capture scheduling,
  non-invasive hints, controller forwarding, runtime feature flags, and REST
  contract coverage, plus Rust tests for native mode parsing, event filtering,
  redaction, hint payloads, and debounce.
- Still open: installed smoke evidence for COM registration, support-bundle
  native status reporting beyond backend hint diagnostics, and physical
  dock/USB/default-device matrix coverage.

Acceptance gates:

- Unit tests for Rust event filtering and debounce logic.
- Existing Python tests for `DeviceMonitor` continue passing.
- New Python tests prove native hint bodies still defer PortAudio refresh while
  active streams are running.
- Installed smoke proves backend and frontend still start without COM errors.
- Physical hardware matrix covers USB mic add/remove, dock connect/disconnect,
  Windows default input changes, and favorite mic restore.
- Always-on mic light must not blink during idle safety periods unless an actual
  capture endpoint event occurred.
- Support bundle clearly reports whether Rust events or Python fallback handled
  the last refresh.

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
- Fallback paths use `pyautogui`, `keyboard.write`, and Tkinter clipboard
  helpers.
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

- Rust owns reading current `CF_UNICODETEXT`, setting new Unicode clipboard
  text, dispatching Ctrl+V, safe optional restore, foreground-window snapshot,
  basic diagnostics, and structured timing/status response.
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
   - read current `CF_UNICODETEXT`,
   - set new clipboard text,
   - dispatch Ctrl+V with `SendInput`,
   - restore only if the clipboard still contains the injected text, preferably
     guarded by clipboard sequence number where available,
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
  call timeout support and redacted pipe-name hashing.
- `/api/runtime/audio-diagnostics` now reports `textInjection.shellIpc` status
  without calling the pipe from readiness or startup paths.
- Added Rust unit tests for the shell IPC protocol and backend env contract,
  plus Python unit/contract coverage for shell IPC diagnostics.
- Still open: `injectText` command, foreground-window diagnostics, marker
  forwarding into `TextInjector`, strict `SCRIBER_INJECT_METHOD=tauri`, target
  app smoke matrix, and default-path decision based on installed evidence.

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
- If shell IPC is unavailable, Python continues using existing injection paths.
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

Target:

- Do not rewrite the full audio path first.
- Build a measured Rust capture prototype that can run beside the Python path.
- Only promote it if it improves stability or start latency without weakening
  provider compatibility, diagnostics, or testability.
- A Rust engine must feed Python the same effective downstream contract as the
  current `MicrophoneInput`: 16 kHz mono PCM `i16` frames that become
  `InputAudioRawFrame` with expected sample rate and channel count.

Ownership boundary:

- Rust prototype may own WASAPI stream open/start/stop, native endpoint
  selection for the selected engine, callback/frame timestamps, drop counting,
  native capture restart attempts, optional rolling prebuffer for the Rust
  engine, and binary PCM transport to Python.
- Python keeps provider pipeline, Pipecat transport semantics, VAD/analyzer
  ownership, recording state, REST/WebSocket contracts, transcript persistence,
  text-injection trigger flow, user-facing microphone list and favorite/default
  semantics, support-bundle aggregation, and the default `sounddevice` engine
  until promotion gates pass.

Missing prerequisites:

1. Engine-neutral Python frame-source boundary:
   - Add an internal `AudioFrameSource` boundary behind `MicrophoneInput`.
   - Keep current `sounddevice` implementation as
     `PythonSoundDeviceFrameSource`.
   - First PR must be behavior-preserving and prove existing mic/prewarm tests
     still pass.
2. Native endpoint identity mapping:
   - Current user-facing device IDs are Python/PortAudio names.
   - WASAPI capture needs IMMDevice identity.
   - Add a private mapping layer for Windows default input, normalized friendly
     name, hashed native endpoint ID, and current favorite mic label.
   - Do not expose raw IMMDevice IDs as stable public microphone IDs.
3. Audio support-bundle schema:
   - Include `engine`, `frameSource`, `nativeEndpointIdHash`, `sampleRate`,
     `targetChannels`, `captureChannels`, `blockSize`, `prebufferMs`,
     `droppedFrameCount`, `lastFrameAgeSeconds`, and `restartCount`.

Implementation plan:

1. Refactor without Rust:
   - Introduce the internal frame-source boundary.
   - Keep `SCRIBER_AUDIO_ENGINE=python`.
   - Preserve all current tests and installed live-mic smoke behavior.
2. Add a passive Windows-only WASAPI probe:
   - Run only when `SCRIBER_AUDIO_ENGINE=rust-prototype` or explicit diagnostics
     are enabled.
   - Enumerate capture endpoints and open the selected endpoint without feeding
     the provider pipeline.
   - Capture diagnostics only: open success/failure, endpoint hash, mix format,
     requested format, callback count, last callback age, drop count, and close
     status.
3. Add active capture prototype without prewarm:
   - Implement a Tauri-managed Rust audio sidecar process for crash isolation,
     not an in-WebView or UI-thread feature.
   - Use a private named pipe for control and a length-prefixed binary named
     pipe for PCM frames.
   - Python starts Rust capture through shell IPC and receives frames through
     the binary pipe.
   - If Rust fails before the first frame, fall back to Python capture for that
     session.
   - If Rust stalls mid-session, record diagnostics and fail the current engine
     cleanly. Do not silently splice engines mid-utterance unless a later design
     proves transcript safety.
4. Use a narrow control protocol:
   - Request includes sample rate, channels, block size, device preference
     (`default`, `favorite`, `portAudioLabel`, or `nativeEndpointHash`), and
     `prebufferMs`.
   - Response includes engine, stream id, format, capture channels,
     native endpoint hash, frame pipe, resampler, and fallback reason.
   - Each PCM frame uses a small binary header: payload length, sequence,
     timestamp, frame count, channels, flags, then `pcm_i16_le`.
5. Add prewarm parity only after active capture works:
   - Match configured prebuffer duration, frame ordering, adoption into active
     capture, no prebuffer/live-frame interleaving, pause during device refresh,
     and resume after active capture.
   - Keep Python prewarm as default path.
6. Add watchdog and restart parity:
   - Mirror existing Python active-capture and prewarm diagnostics: stream
     active, callback count, last frame age, last status/error, dropped frames,
     restart count, endpoint hash, format, prebuffer duration, sidecar PID, and
     sidecar exit status.
   - Match stale callback detection, minimum restart interval, restart count,
     graceful close, and fallback-on-next-session policy.
7. Run A/B measurements before any promotion:
   - hotkey to first audio frame,
   - hotkey to first audible audio frame,
   - stop to last chunk sent,
   - idle/live CPU and memory,
   - dropped frames,
   - 30-minute idle always-on stability,
   - 10-minute live recording stability.
8. Promote to default only if physical hardware tests show fewer interruptions
   or a meaningful latency win. Otherwise keep Python as default and retain the
   Rust prototype for future investigation.

Acceptance gates:

- Existing Python mic tests continue passing.
- New Rust unit tests for endpoint selection/default fallback, format
  negotiation, frame header encode/decode, monotonic sequence numbers, bounded
  buffer/drop accounting, sidecar start/stop lifecycle, watchdog restart
  throttling, and redacted diagnostics.
- New Python `AudioFrameSource` contract tests, Rust frame-source fallback
  tests, diagnostics schema tests, and support-bundle redaction tests.
- Installed live-mic smoke with visible waveform and successful transcription.
- Physical mic matrix across built-in, USB, Bluetooth, docked, undocked, and
  Windows default-device changes.
- No feature loss for provider streaming, final transcript injection, overlay,
  audio diagnostics, support bundles, and fallback settings.
- Backend restart and Tauri quit clean up the Rust audio sidecar.

Rollback:

- Default remains `SCRIBER_AUDIO_ENGINE=python`.
- `SCRIBER_AUDIO_ENGINE=rust-prototype` is opt-in.
- If the Rust audio sidecar fails before first audio frame, Python capture can
  be used for that recording.
- If Rust capture fails mid-recording, report the engine failure and preserve
  diagnostics. Do not silently corrupt frame ordering by switching engines
  mid-stream.
- The Python `sounddevice` path remains packaged and tested.

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
  mic activation beyond the existing setting.

## Current Direction

The best path forward is to keep the hybrid architecture:

- Rust/Tauri for desktop shell, process supervision, hotkey, autostart, tray,
  updater, native device event hints, optional native text injection, and
  Windows integration.
- Python for recording state, microphone list semantics, PortAudio refresh,
  provider pipeline, Pipecat frame flow, media preparation, persistence,
  REST/WebSocket contracts, diagnostics aggregation, and current default audio
  capture.
- React for the app UI.

Near-term Rust work should reduce native Windows fragility and improve
diagnostics without changing user-visible workflows. Audio capture should remain
a measured prototype until it beats the current Python path on real installed
Windows hardware without losing prewarm, provider, overlay, diagnostics,
support-bundle, or fallback behavior.
