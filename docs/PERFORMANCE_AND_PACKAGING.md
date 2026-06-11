# Performance And Packaging

Last verified: 2026-06-11

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
  call timeout support, redacted pipe-name hashing, and redacted transport
  failure text so raw pipe names and session tokens are not persisted in
  diagnostics or returned fallback reasons.
- `/api/runtime/audio-diagnostics` now reports `textInjection.shellIpc` status
  without calling the pipe from readiness or startup paths.
- Added the `injectText` shell IPC command for opt-in Tauri text injection. The
  Rust side reads and sets `CF_UNICODETEXT`, dispatches Ctrl+V with `SendInput`,
  returns structured timing data and `clipboard_set`/`paste` markers, hashes
  foreground-window diagnostics, owns `preDelayMode=auto` foreground-title
  classification for Word/Outlook without logging raw titles, uses
  byte-budgeted request limits, rejects embedded NUL text, enforces a request
  deadline before side effects, and guards clipboard restore with the clipboard
  sequence number.
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
  `--require-tauri-text-injection-smoke`. The report must be real evidence, not
  `--validate-only`, and must show `injectText` success, target text arrival,
  and `clipboard_set` plus `paste` markers. It now also must include structured
  clipboard restore evidence; missing restore fields, restore errors, or
  disabled restore fail promotion evidence. Foreground diagnostics in the Shell
  IPC payload must remain hashed/redacted and must not expose raw window titles,
  HWNDs, process IDs, or process names.
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
   - Implemented on `codex/rust-expansion-plan`: `src/microphone.py` now has an
     internal `AudioFrameSource` boundary and a behavior-preserving
     `PythonSoundDeviceFrameSource`.
   - `MicrophoneInput` still defaults to Python `sounddevice`, keeps prewarm
     adoption unchanged, and exposes `engine`, `requestedEngine`,
     `frameSource`, `engineFallbackReason`, `droppedFrameCount`, and a nested
     source diagnostic snapshot.
   - `SCRIBER_AUDIO_ENGINE=rust-prototype` is still request-only. It now tries
     the prototype frame-pipe reader, then falls back to Python `sounddevice`
     before the first frame if Rust capture is unavailable or unhealthy.
   - Implemented: Python idle prewarm adoption is now engine-scoped. When
     `SCRIBER_AUDIO_ENGINE=rust-prototype` is requested with always-on mic
     enabled, the Python prewarm manager is paused instead of adopted, so
     active capture still exercises the Rust frame-source path. Diagnostics
     expose `prewarmAdoptionSkippedReason` for this gate.
   - Added Python source contract tests for configured-device open, default
     fallback, pause/close lifecycle, device-index parsing, frame-pipe reading,
     Rust-unavailable fallback, and Rust-requested always-on startup without
     Python-prewarm adoption. Existing microphone, prewarm, pipeline-stop, and
     runtime lifecycle tests pass.
2. Native endpoint identity mapping:
   - Implemented on `codex/rust-expansion-plan` as a private diagnostic and
     prototype mapping layer in `src/audio_devices.py`.
   - Current user-facing device IDs remain Python/PortAudio names.
   - `hash_native_endpoint_id`, `NativeEndpointEntry`, and
     `InputEndpointMapping` connect normalized PortAudio names to hashed native
     endpoint IDs, default-input hash, and current favorite mic label.
   - `/api/runtime/audio-diagnostics` includes
     `microphone.nativeEndpointMapping`; `/api/microphones` is unchanged and
     never exposes raw IMMDevice IDs.
   - Implemented for the Rust/WASAPI prototype: Python can pass a redacted
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
   - Still open: proving favorite/default behavior on physical dock/USB
     transitions.
3. Audio support-bundle schema:
   - Partly implemented on `codex/rust-expansion-plan`: support bundles now
     include `audio-diagnostics.redacted.json`, sourced from
     `/api/runtime/audio-diagnostics`.
   - Active Python capture diagnostics include `engine`, `requestedEngine`,
     `frameSource`, `sampleRate`, `targetChannels`, `captureChannels`,
     `blockSize`, `droppedFrameCount`, and last-callback age.
   - Native endpoint mapping diagnostics now include hashed native endpoint
     candidates when an inventory provider is available.
   - Implemented for the Rust prototype: active-capture
     `nativeEndpointIdHash`, sidecar PID, stop exit status, sidecar uptime,
     writer connection state, frames/bytes written, writer error, reader-thread
     liveness, and sidecar restart count are exposed through the nested
     active-capture source diagnostics.
   - Implemented: the REST contract for `/api/runtime/audio-diagnostics`
     validates the optional `microphone.activeCapture` schema, including nested
     Rust `source` diagnostics and bounded-cleanup fields
     `sidecarKilledAfterTimeout` and `sidecarWaitError`.
   - Implemented for the Rust prototype: Python reader diagnostics now include
     frame-pipe frames read, audio frames read, bytes read, sequence/protocol
     error counters, last frame metadata, first-frame read timing, reader end
     reason, and callback-drop counts before protocol failure.
   - Implemented for future Rust prewarm parity: Python reader diagnostics now
     distinguish `SAF1` prebuffer frames from live frames, track prebuffer/live
     audio-frame counts and first live sequence, and reject prebuffer frames
     that arrive after live frames.
   - Implemented: `/api/runtime/audio-diagnostics` now retains the latest mic
     watchdog warning snapshot under `watchdog.lastWarning`, including the
     redacted active-capture diagnostics that caused the warning. Support
     bundles include this snapshot through `audio-diagnostics.redacted.json`,
     so short live/Always-On-Mic interruptions remain diagnosable after the
     recording has stopped.
   - Still open: durable mid-session failure policy and physical support-bundle
     evidence from long real-device runs.
4. Audio frame-pipe protocol:
   - Implemented on `codex/rust-expansion-plan` as shared Rust/Python protocol
     helpers, a Python `RustPrototypeFrameSource`, an opt-in sidecar synthetic
     frame-pipe writer, and an opt-in WASAPI writer with default or
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
   - With `SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1`, the sidecar opens either the
     Windows default capture endpoint or a selected endpoint by redacted native
     endpoint hash through WASAPI shared mode, converts supported float/PCM mix
     formats to requested `pcm_i16_le` blocks, and writes those frames through
     the same `SAF1` protocol. This remains a prototype path.
5. Audio capture control protocol:
   - Partly implemented as private shell IPC commands `audioCaptureStart`,
     `audioCaptureStop`, `audioPrewarmStart`, and `audioPrewarmStop`.
   - Current Rust shell implementation validates the start/stop payloads, calls
     the Tauri-side sidecar client when an allowlisted sidecar executable is
     available, and otherwise returns explicit `audioCaptureUnavailable`.
   - Python treats that explicit failure as a before-first-frame fallback to the
     existing `sounddevice` engine.
6. Rust audio sidecar process skeleton:
   - Partly implemented as separate Cargo binary `scriber-audio-sidecar` in
     `Frontend/src-tauri/src/audio_sidecar.rs`.
   - The binary supports `--self-test` plus `--stdio` JSON-lines commands:
     `ping`, `capabilities`, `captureStart`, `captureStop`, `prewarmStart`,
     `prewarmStop`, and `shutdown`.
   - The sidecar reports the shared audio frame protocol, returns explicit
     `audioCaptureUnavailable` by default, and can run an explicit synthetic
     frame-pipe transport harness through
     `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1` or an explicit WASAPI capture
     prototype through `SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1`. The WASAPI
     prototype can select a specific endpoint by redacted native endpoint hash;
     without a hash, non-default capture requests fail before first frame so
     Python can safely fall back to `sounddevice`.
   - Implemented: the sidecar also has a synthetic idle prewarm harness behind
     `prewarmStart`/`prewarmStop`. It keeps a long-lived sidecar process,
     reports `prewarmId`, counts observed and buffered frames, and returns
     stop-health fields. It is lifecycle evidence only; WASAPI idle stream
     adoption is still open.
   - Implemented: `-BundleRustAudioSidecar` builds
     `scriber-audio-sidecar --release`, copies it into
     `Frontend\src-tauri\resources\audio-sidecar`, and the NSIS bundle includes
     that resource.
   - Implemented: selected endpoint capture by redacted native endpoint hash,
     plus endpoint-selection diagnostics in sidecar responses.
   - Still open: physical favorite/dock/USB smokes, prewarm/watchdog parity,
     long physical-device smokes, and any default promotion decision.
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
     level for Python, and keep capture unavailable by default unless an
     explicit sidecar capture flag is set.
   - `audioPrewarmStart`/`audioPrewarmStop` now route through the same client
     and preserve synthetic prewarm stop-health fields. The commands remain
     private prototype plumbing and are not yet wired to Python always-on
     adoption.

Implementation plan:

1. Refactor without Rust:
   - Introduce the internal frame-source boundary.
   - Keep `SCRIBER_AUDIO_ENGINE=python`.
   - Preserve all current tests and installed live-mic smoke behavior.
2. Add a passive Windows-only WASAPI probe:
   - Implemented on `codex/rust-expansion-plan` as private shell IPC command
     `audioProbe`.
   - Runs only when `SCRIBER_AUDIO_ENGINE=rust-prototype` or
     `SCRIBER_RUST_AUDIO_PROBE=1` is set.
   - Rust probes the Windows default capture endpoint through WASAPI, activates
     `IAudioClient`, reads mix format/device period, attempts shared-mode
     initialization without `Start()`, and returns redacted diagnostics.
   - `/api/runtime/audio-diagnostics` surfaces this as
     `microphone.rustAudioProbe`.
   - Implemented: the probe can use a selected redacted native endpoint hash
     instead of probing only the Windows default endpoint, and refuses to report
     a non-default request as a default success when no native hash is available.
   - The probe does not feed the provider pipeline, does not become an active
     capture engine, and reports `callbackCount=0`, `droppedFrameCount=0`, and
     `closeStatus=closed`.
   - Still open: real callback-based passive observation if needed and installed
     physical-device evidence.
3. Add active capture prototype without prewarm:
   - Partly implemented: Python can start a Rust capture attempt through private
     shell IPC and receive frames through the binary frame-pipe reader.
   - Partly implemented: a separate Rust audio sidecar binary exists for crash
     isolation and exposes a JSON-lines control protocol.
   - Partly implemented: Tauri can discover an allowlisted sidecar executable
     and perform stdio command handshakes outside the WebView/UI thread.
   - Partly implemented: Tauri keeps successful sidecar capture processes alive
     by `streamId` and drains them on capture stop, backend restart, and shell
     exit.
   - Partly implemented: an explicit synthetic sidecar mode creates a private
     Windows named pipe and writes valid `pcm_i16_le` `SAF1` frames through the
     same lifecycle path Python will use for real capture.
   - Partly implemented: an explicit
     `SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1` sidecar mode opens the requested
     native endpoint when a redacted hash is available, otherwise the Windows
     default endpoint for true default requests, and writes real PCM frames into
     that same frame-pipe lifecycle.
   - Partly implemented: Python now sends redacted native endpoint hints for
     the selected PortAudio device; the sidecar selects the matching WASAPI
     capture endpoint by hash and refuses unsafe non-default fallback when no
     hash is available.
   - Still open: prove the long-lived path with physical real-WASAPI sessions,
     favorite restore behavior, dock/USB/default-device transitions, and
     provider-backed transcription smokes.
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
     provider-backed transcription, always-on prewarm parity, and default
     promotion gates.
   - If Rust fails before the first frame, Python falls back to Python capture
     for that session.
   - If Rust stalls mid-session, record diagnostics and fail the current engine
     cleanly. Do not silently splice engines mid-utterance unless a later design
     proves transcript safety.
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
   - Implemented for the opt-in WASAPI prototype: successful sidecar responses
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
     `MIC_PREBUFFER_MS` value as `prebufferMs` when starting the Rust prototype
     frame source, and diagnostics expose the requested value.
   - Partly implemented: synthetic Rust sidecar capture emits correctly flagged
     prebuffer frames before live frames when `prebufferMs` is requested, and
     `scripts/smoke_rust_audio_sidecar.py --prebuffer-ms ...` reports
     prebuffer/live frame counts plus any prebuffer-after-live ordering errors.
   - Partly implemented: WASAPI sidecar capture also flags the requested
     leading frames as prebuffer and exposes writer-side prebuffer/live counts.
     This validates frame ordering and promotion evidence, but it is not yet a
     full idle always-on Rust prewarm stream.
   - Implemented: the active Rust prototype no longer adopts the Python
     always-on prewarm stream. This prevents `SCRIBER_AUDIO_ENGINE=rust-prototype`
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
     `SCRIBER_AUDIO_ENGINE=rust-prototype` is requested. With
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
     redacts raw prewarm IDs in the report. The default smoke ignores locally
     configured favorite microphones so release evidence covers the stable
     Windows default endpoint; `--honor-favorite-mic` is reserved for targeted
     selected-device investigations.
   - Implemented: the hybrid release-readiness runner and validator can consume
     the app-level report through `-RunRustAudioAppPrewarmSmoke`,
     `-UseExistingRustAudioAppPrewarmReport`, and
     `-RequireRustAudioAppPrewarmSmoke`. Required app-level readiness expects
     WASAPI mode, default-endpoint evidence, positive adopted/prebuffer/live
     frame counts, a native endpoint hash, successful idle-prewarm resume, and
     zero sequence/protocol/prebuffer-ordering errors.
   - Implemented: final readiness can also require long app-level Rust
     Always-On-Mic evidence with
     `-MinRustAudioAppPrewarmDurationSec` and
     `-MinRustAudioAppPrewarmPrewarmDurationSec`. This makes the 10-minute
     active-capture / 30-minute idle-prewarm promotion target
     machine-checkable instead of relying on a short smoke report. Supplying
     either app-prewarm minimum duration now makes the app-prewarm report
     required even without the generic `-RequireRustAudioAppPrewarmSmoke` flag.
   - Implemented: final readiness can require an installed live-recording smoke
     with `-RequireInstalledLiveRecordingSmoke` and
     `-MinInstalledLiveRecordingDurationSec`. The validator accepts reports from
     the installed desktop smoke path and requires a `tauri-supervised`
     sidecar runtime, healthy API version/ready state, positive app/backend
     process and port metadata, clean start/stop state, verified cleanup, sufficient
     stability-sample coverage for the requested duration, and zero
     non-recording samples during active capture. A configured minimum duration
     now makes the installed live-recording report required even without the
     generic `-RequireInstalledLiveRecordingSmoke` flag. This is a default-path
     app/installer gate. For Rust default promotion, the runner also sets
     `-RequireInstalledLiveRecordingRustAudio`, which requires every stability
     sample to include compact audio diagnostics proving `rust-prototype`
     `rust-frame-pipe` capture, active callbacks, closed fallback circuit, and
     clean frame-pipe counters. The validator now also requires the Rust
     callback, frame-pipe, and audio-frame counters to increase across
     stability samples, so a stale diagnostics snapshot cannot satisfy the
     long-recording gate. Provider-backed transcript quality and latency still
     require the separate Python/Rust hot-path comparison artifact.
   - Implemented: installed live-recording smoke runners can now request
     `SCRIBER_AUDIO_ENGINE=rust-prototype` plus the explicit Rust sidecar
     capture mode via `-LiveRecordingAudioEngine rust-prototype
     -LiveRecordingRustAudioCaptureMode wasapi` on the desktop/installer smoke
     and `-InstallerLiveRecordingAudioEngine rust-prototype
     -InstallerLiveRecordingRustAudioCaptureMode wasapi` on the build wrapper.
     This makes the installed Rust-audio report producer explicit instead of
     relying on manual environment setup.
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
   - Local evidence from 2026-06-11: the app-level Windows WASAPI prewarm
     adoption smoke passed with
     `python scripts\smoke_rust_audio_app_prewarm.py --mode wasapi --duration-sec 0.5 --prewarm-duration-sec 0.5 --post-resume-duration-sec 0.1 --output tmp\rust-audio-app-prewarm-wasapi-current.json`.
     It reported `adoptedPrewarmBlocks=40`, `prebufferFramesRead=40`,
     `liveFramesRead=42`, `prebufferAfterLiveCount=0`,
     `sequenceErrorCount=0`, `protocolErrorCount=0`,
     `framePipeFirstFrameReadMs=10.271`, and successful idle-prewarm resume.
     A related fix keeps the Rust prewarm manager's default device preference as
     `default` when Python cannot map the PortAudio default to a native endpoint
     hash; non-default Rust capture without a native hash still fails closed.
   - Implemented: the recording hot-path benchmark can now be run as a strict
     provider-backed Rust evidence gate. `--require-provider-transcript`
     requires a final STT provider transcript, and `--require-rust-audio-engine`
     verifies that `/api/runtime/audio-diagnostics` saw an active
     `rust-prototype` `rust-frame-pipe` capture during recording. The hybrid
     baseline runner exposes these as
     `-RequireRecordingHotPathProviderTranscript` and
     `-RequireRecordingHotPathRustAudio`.
     The Rust requirement now also fails when
     `microphone.rustAudioFallbackCircuit.open` is true in the report-level or
     during-recording diagnostics, so a run that fell back to Python during the
     Rust cooldown cannot be used as promotion evidence.
   - Implemented: `scripts/validate_recording_hot_path_comparison.py` builds a
     machine-checkable Python-vs-Rust provider-backed comparison artifact from
     two recording hot-path reports. It rejects validate-only evidence by
     default, requires provider transcript evidence from the same STT provider
     in both reports, requires active `rust-prototype`/`rust-frame-pipe`
     capture in the Rust report, rejects an open Rust fallback circuit in that
     report, and records segment deltas such as hotkey-to-first-frame,
     provider-finalize, and stop-to-text-injection. The final readiness
     validator and
     `scripts/run_hybrid_release_readiness.ps1` can require this artifact with
     `--require-recording-hot-path-comparison` /
     `-RequireRecordingHotPathComparison`.
     The final readiness validator now requires the comparison artifact's
     `sameProvider` and `rustFallbackCircuitClosed` checks alongside
     `rustAudioEngine`, and it requires at least three samples per engine plus
     no clear P95 regression in local audio-owned segments. This gate cannot be
     bypassed by passing a stale, one-shot, or clearly slower comparison schema.
     The comparison validator also rejects unredacted source reports containing
     raw `SWD\MMDEVAPI\...` endpoint IDs, raw `\\.\pipe\scriber-*` pipe names,
     or non-redacted token fields, so sensitive hot-path evidence cannot become
     promotion input. Final hybrid readiness requires that
     `inputReportRedaction` check to be present and passing, so stale
     comparison artifacts from before this gate fail Rust promotion.
     Provider-finalize and total stop-to-text values remain reported but are not
     part of the local-audio regression gate because they are dominated by
     network/STT provider latency.
   - Implemented: `scripts/run_recording_hot_path_comparison.ps1` orchestrates
     the full provider-backed A/B evidence path. It runs
     `measure_hybrid_baseline.ps1` once with `SCRIBER_AUDIO_ENGINE=python`, once
     with `SCRIBER_AUDIO_ENGINE=rust-prototype` and the requested Rust capture
     mode, then calls the comparison validator to produce
     `recording-hot-path-python-rust-comparison.json`.
     The runner defaults to three recording samples per engine and passes that
     as the minimum accepted sample count to the validator.
     It also passes a default 50 ms max P95 regression tolerance for local
     audio-owned hot-path segments.
   - Still open: actually running the long physical Always-On-Mic evidence with
     the Rust manager, device-refresh pause/resume matrix evidence, real
     provider-backed Python/Rust comparison runs using the new gate, and final
     promotion decision gates. The readiness runner now has
     `-RequireRustAudioPromotionReadiness` as a single aggregate switch for the
     final default-path decision; it makes the sidecar, app prewarm, installed
     live-recording, provider-comparison, Rust endpoint inventory, and native
     device-refresh evidence mandatory, and raises the relevant duration minima
     to 10-minute active / 30-minute idle-prewarm promotion values.
   - Keep Python prewarm as default path.
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
     current recording still does not splice to Python mid-utterance, but the
     next `SCRIBER_AUDIO_ENGINE=rust-prototype` session falls back to Python
     during the cooldown and records
     `rustPrototypeCircuitOpen:<reason>` in diagnostics.
   - Implemented: `/api/runtime/audio-diagnostics` also exposes the global
     fallback circuit as `microphone.rustAudioFallbackCircuit`, even when there
     is no active pipeline. Support bundles preserve the same redacted circuit
     state, so a post-failure idle bundle can prove why the next requested Rust
     recording used Python.
   - Still open: physical proof that this restart/cooldown policy behaves well
     during real long recordings and dock/USB/default-device transitions.
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
- For final Rust-audio default promotion only: run
  `scripts\run_hybrid_release_readiness.ps1 -RequireRustAudioPromotionReadiness`
  with either matching `-Run...` flags or validated existing reports. This is
  the canonical aggregate gate before changing defaults; individual Rust gates
  remain available for focused investigations.
- For Rust audio promotion only: 10-minute physical WASAPI sidecar smoke via
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
  to the runner for the real passive WASAPI idle stream. This is not sufficient
  for default promotion because it does not prove app-wide Always-On-Mic
  lifecycle integration or provider-backed transcription.
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
