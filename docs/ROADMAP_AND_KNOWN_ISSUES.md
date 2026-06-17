# Roadmap And Known Issues

Last verified: 2026-06-11

This document replaces old bug lists, code-review notes, and proposal journals.
It tracks current status only.

## Recently Completed

Desktop runtime:

- Tauri is the primary Windows desktop runtime.
- Rust supervisor starts or attaches to the Python backend.
- Per-run session token protects local REST/WebSocket access.
- Backend starts without visible console windows in installed Windows builds.
- Single-instance guard, autostart, global hotkey, tray/menu shell actions, and
  worker crash recovery are implemented.

Mic and recording:

- DeviceMonitor uses native Windows endpoint events where available.
- Native device-event status is included in audio diagnostics/support bundles
  through redacted Tauri shell IPC (`microphone.nativeDeviceEvents`), including
  COM/registration state, callback liveness, event/debounce counts, post
  results, and hashed endpoint identifiers.
- The installed desktop support-bundle smoke now gates native device-event COM
  initialization, monitor registration, and callback liveness whenever Tauri
  shell IPC is available and native events are supported/enabled.
- The microphone hardware matrix is native-event-first and can require
  DeviceMonitor refresh evidence that proves native events, sparse safety
  polling, and zero forced per-poll refreshes.
- Polling fallback is intentionally slow compared with the old aggressive poll.
- PortAudio access is guarded and refreshes are recording-aware.
- Always-on mic prewarm and rolling prebuffer are implemented.
- Audio-level visualization is throttled and frontend waveform uses Canvas/RAF.
- Live Mic UI state updates correctly after session finish in the current branch.

YouTube/file:

- Thumbnail handling was fixed and covered by browser smoke.
- File tab drag/drop was fixed and covered by browser smoke.
- YouTube job progress now advances beyond download completion through upload,
  transcription, summary, and done states.
- Azure MAI file/live preparation uses MP3 for latency rather than WAV.

Debug/support:

- Debug console has severity colors, filters, sticky controls, newest-first
  default, today filter, clear-view, clear-log, copy-visible, refresh, and
  support-bundle download.
- Support bundles are token-protected and redacted.
- Installed support-bundle smoke now gates both native device-event diagnostics
  and Rust audio fallback-circuit diagnostics.

Packaging/performance:

- Profile B ffmpeg is the default Windows media-tool profile.
- Installer size is about `88.10 MiB`; installed app smoke is about
  `200.41 MiB`.
- SciPy is absent from the standard sidecar.
- AWS Transcribe and AWS SDK packages are absent from the standard sidecar.
- Sidecar reuse cache reduces repeated local installer build time.

Docs:

- Permanent docs were consolidated into README, AGENTS, and four category docs.

## Current Highest Priorities

1. Keep installed app stability high.
   - Run longer idle and live-recording stability smokes.
   - Track backend working-set growth and average idle CPU.
   - Capture support bundles for any spontaneous mic shutoff reports.

2. Measure stop-to-text latency precisely.
   - Split `stop_requested` to `last_chunk_sent`,
     `provider_final_received`, `clipboard_set`, and `first_paste`.
   - Optimize only after the provider/local split is proven.

3. Continue responsive UI polish.
   - Debug Console and Settings should stay usable at narrow desktop widths.
   - Buttons should not become oversized or clipped.
   - Support-bundle download needs clear visible feedback with saved path when
     the browser/Tauri environment allows it.

4. Keep release packaging reproducible.
   - Profile B should remain standard.
   - Gyan Essentials should remain fallback.
   - Any size pruning must pass installed frontend, media, support-bundle, and
     live overlay smokes.

## Known Open Areas

Signing/updater:

- Tauri updater wiring exists, but production update flow needs signing keys,
  HTTPS endpoint, signed `latest.json`, and publication evidence.
- Authenticode validation exists, but real signing requires a certificate or
  cloud-signing provider.
- `run_hybrid_release_readiness.ps1 -RunReleaseBuild` can now run the Windows
  release build as an evidence producer and reuse its Authenticode validation
  report, but it still depends on real Tauri updater signing secrets,
  Authenticode signing, and public HTTPS publication.

Physical hardware evidence:

- Scripts exist for a microphone hardware matrix.
- Matrix artifacts now capture redacted Rust/WASAPI endpoint inventory
  before/after each physical action, and validation can require that evidence
  with `-RequireRustEndpointInventory` or the Rust audio release-readiness gate.
- Matrix artifacts now also capture DeviceMonitor refresh counters, and
  validation can require native-event refresh evidence with
  `-RequireDeviceRefreshEvidence`.
- Final release-readiness still needs real physical runs for USB, Bluetooth,
  dock connect/disconnect, Windows default changes, and favorite fallback using
  both Rust endpoint inventory and DeviceMonitor refresh evidence.

Provider latency:

- Cloud STT finalization can dominate stop-to-text latency.
- Local app optimization should be guided by hot-path metrics.

Legacy GUI footprint:

- The installed recording overlay is Tauri-owned; PySide6/Tk overlay runtimes
  are no longer part of the standard backend sidecar.
- Runtime dependency footprint gates reject PySide6, customtkinter, and Tk
  reintroduction in the packaged backend.

Provider runtime footprint:

- Supported cloud-provider runtime modules stay covered by the frozen runtime
  import check.
- The standard sidecar excludes unused Google Generative-AI/TTS SDKs; footprint
  gates fail if those SDKs reappear in the packaged backend.

Rust audio:

- Rust/WASAPI sidecar capture is now the standard live-mic capture and
  Always-On-Mic prewarm path. The Python `sounddevice` capture/prewarm path was
  removed from normal app use after the 2026-06-11 short provider-backed A/B
  comparison showed clearly better Rust median mic-ready and first-audio
  latency with valid frame-pipe flow, adopted prewarm, no dropped frames, and a
  closed fallback circuit.
- Python still owns recording state, Pipecat/provider flow, persistence,
  diagnostics aggregation, and REST/WebSocket contracts. `sounddevice` may still
  be present for microphone listing and PortAudio-to-native endpoint mapping
  helpers, but it must not be used as live capture fallback.
- `SCRIBER_AUDIO_ENGINE` remains only as diagnostic compatibility. Normal WASAPI
  capture/prewarm is available without `SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1`;
  `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1` is test-only, and
  `SCRIBER_RUST_AUDIO_DISABLE_WASAPI_CAPTURE=1` forces unavailable behavior for
  tests.
- Rust/WASAPI endpoint inventory is exposed through private shell IPC and is
  preferred for private PortAudio-to-native mapping before PyCAW fallback.
  Default-device requests are passed as `devicePreference=default` with no
  native endpoint hash. Favorite/non-default microphones use redacted native
  endpoint hashes and fail closed if no hash can be resolved, so the sidecar
  does not silently open the Windows default microphone.
- Rust diagnostics include frame-pipe read counters, sequence/protocol errors,
  prebuffer/live frame counts, first-frame read timing, reader end reason,
  endpoint-selection details, stop-health fields, prewarm status, restart
  counters, and a bounded redacted `recentEvents` timeline for short
  microphone privacy-indicator interruptions.
- The 2026-06-11 targeted Insta360 investigation fixed a Python/Rust endpoint
  hash mismatch by preferring Tauri shell-IPC endpoint inventory for active
  capture and prewarm. A Rust-only provider-backed smoke then passed with Azure
  MAI, `rust-wasapi` / `rust-frame-pipe`, adopted prewarm, no dropped frames,
  selected Insta360 endpoint hash `51112d9ccdd3a140`, and about 126 ms
  hotkey-to-first-audio.
- Still open: longer physical Always-On-Mic evidence, dock/USB/default-device
  matrix evidence, selected-device regression evidence, signing/updater
  publication evidence, and release hardening around sidecar restart/cooldown
  behavior.
  Rust Always-On-Mic prewarm now has an `audioPrewarmStatus` path through
  Shell IPC and the audio sidecar. The Python Rust prewarm watchdog uses that
  status instead of treating a cached `prewarmId` as sufficient proof of an
  active stream, and audio diagnostics expose redacted status/start/stop/health
  timings plus inactive reasons, restart counters, stop-to-prewarm-ready resume
  gap metrics, and a bounded redacted `recentEvents` timeline for
  start/stop/adoption/watchdog restarts. This
  should make short microphone privacy-indicator dropouts visible in support
  bundles without increasing steady-state log volume. Missing
  post-start idle sessions are now recorded explicitly as
  `missingPrewarmSession` for Rust and `missingPrewarmStream` for the Python
  fallback, while first startup activation is not counted as a restart. This
  still needs longer physical evidence for release hardening.
  `scripts/run_hybrid_release_readiness.ps1` now exposes
  `-RequireRustAudioPromotionReadiness` as the aggregate default-promotion
  gate; it bundles Rust sidecar capture, app-level Always-On-Mic prewarm,
  installed live-recording stability, provider-backed Python-vs-Rust
  comparison, Rust endpoint inventory, and native device-refresh evidence with
  the required 10-minute active / 30-minute idle-prewarm minimums. It also
  requires at least two app-level prewarm/capture/stop/resume cycles so a
  single successful resume cannot hide repeated Stop-button failures. Final
  readiness validates per-cycle pre-adoption and post-resume
  `audioPrewarmStatus` snapshots. Installed Rust live-recording evidence now
  also includes post-stop audio diagnostics and measured stop-to-prewarm-ready
  gap fields, so the real Tauri/installer path proves that Always-On-Mic
  resumes after the user stops a recording. When sidecar prewarm adoption is part of that
  gate, app-level prewarm reports must also include the expected redacted
  `recentEvents` lifecycle markers for pre-adoption start and post-resume
  adoption/resume/restart. Reused sidecar reports now must pass explicit
  `--require-rust-audio-sidecar-prewarm-adoption` validation instead of relying
  on the report's own requested flags.
  A local physical Windows WASAPI sidecar smoke passed on 2026-06-10 with
  600.004 seconds observed default capture, selected native-endpoint-hash
  capture, no sequence gaps, matching reader/writer frame counts, and no
  prebuffer-after-live frames. The same sidecar promotion evidence was refreshed
  on 2026-06-11 against the current release `scriber-audio-sidecar.exe` and the
  overlap handoff implementation: 600.003 seconds observed default capture,
  10.008 seconds selected native-endpoint-hash capture,
  `selectedHashVerified=true`, no sequence gaps, no prebuffer-after-live
  frames, matching total read/write frame counts, 34 adopted prewarm blocks, and
  `adoptedPrewarm.handoffMode=overlap-capture-start-before-prewarm-stop`.
  A local app-level WASAPI prewarm adoption smoke passed on 2026-06-11 with 40
  adopted prebuffer blocks, 992 live blocks, no sequence/protocol errors,
  successful idle-prewarm resume, and Windows-default endpoint selection
  evidence. A 30-second installed Rust/WASAPI Always-On-Mic live-recording
  smoke also passed on 2026-06-11 with increasing frame-pipe counters, closed
  fallback circuit, and Windows-default endpoint selection.
  A targeted 2026-06-11 favorite-mic investigation fixed a Python/Rust endpoint
  hash mismatch by preferring the private Tauri shell-IPC endpoint inventory
  for Rust active capture and prewarm. A Rust-only provider-backed smoke then
  passed with Azure MAI, `rust-wasapi` / `rust-frame-pipe`, no Python
  fallback, adopted prewarm, no dropped frames, selected Insta360 endpoint hash
  `51112d9ccdd3a140`, and about 126 ms hotkey-to-first-audio. The sidecar now
  starts the new WASAPI capture before stopping prewarm and exposes
  `adoptedPrewarm.handoffMode=overlap-capture-start-before-prewarm-stop`, which
  is the current mitigation for the visible microphone privacy-light off/on
  gap. This is not yet a true same-stream handoff.
  The hardware matrix now records native DeviceMonitor refresh evidence without
  forced per-poll refreshes. The aggregate readiness runner can now also start
  that guided physical matrix directly with `-RunMicrophoneHardwareMatrix` and
  rejects forced poll refreshes whenever native device-refresh evidence is
  required. Actually running the long physical Always-On-Mic and hardware
  matrix evidence, repeated provider-backed Python/Rust comparison artifacts
  using the aggregate gate, signing/updater publication evidence, and the final
  release hardening are still open. The first one-sample Python/Rust comparison
  after the endpoint fix proved active Rust capture and prewarm adoption but
  failed the old strict local audio-owned P95 no-regression gate; that gate is
  retained only as conservative evidence for old/pre-promotion comparisons.

Tauri text injection:

- `SCRIBER_INJECT_METHOD=tauri` remains strict opt-in. The current branch has
  the private Shell IPC `injectText` command, redacted support-bundle
  diagnostics, Python marker forwarding, explicit protected pipe DACL with
  current-logon-SID hardening when available, and message-only clipboard owner
  HWND usage, plus safe-target smoke support for `--method tauri`. The hybrid
  release-readiness runner can require the safe target evidence with
  `-RequireTauriTextInjectionSmoke`, which validates real Shell IPC success plus
  `clipboard_set`/`paste` markers, structured restore evidence, redacted
  foreground diagnostics, and `deadlineMs` evidence proving the measured Shell
  IPC total stayed within Rust's paste deadline. It can now also produce that
  safe-target artifact directly with `-RunTauriTextInjectionSmoke` when the
  runner is launched with Tauri Shell IPC variables. It can require the full
  installed target-app matrix with `-RequireTauriTextInjectionMatrix` and build
  the aggregate from existing scenario reports with
  `-RunTauriTextInjectionMatrixBuilder`. Actually running and attaching that
  matrix evidence across Notepad, Office, browsers, Electron, elevated windows,
  clipboard edge cases, and Remote Desktop is still open before any
  default-path decision.
- Active-capture watchdog diagnostics now distinguish missing streams, inactive
  streams, no-callback-after-start, stale-callback stalls, and restart-throttle
  suppression. Stale active streams report unhealthy during throttle windows so
  long physical evidence can show short interruptions instead of silently
  treating them as healthy. `/api/runtime/audio-diagnostics` and support
  bundles also retain the latest mic-watchdog warning snapshot. Idle
  Always-On-Mic recoveries now update that snapshot when the prewarm
  `healthRestartCount` increases, so a brief privacy-indicator off/on event
  remains visible after the capture has already ended or after the user clicked
  Stop in the popup.
- Rust frame-pipe failures after the first callback now open a short
  fallback-on-next-session circuit. The current utterance is not switched to
  Python mid-stream, but the next requested rust-wasapi recording uses
  Python during the cooldown and records the circuit-open reason in diagnostics.
  `/api/runtime/audio-diagnostics` exposes that circuit globally, so support
  bundles can explain the fallback even after the failed recording has stopped.
  Recording hot-path summaries, Python/Rust comparison reports, and installed
  live-recording Rust promotion gates now reject explicit
  `midSessionFailureReason` evidence or unexpectedly ended frame-pipe readers,
  so a report with a hidden Rust stream break cannot pass as default-promotion
  evidence.
- Effective runtime audio engine is Rust/WASAPI for live microphone capture.

Local ASR packaging:

- The standard sidecar is the cloud-provider build.
- Heavy local ASR stacks remain excluded from standard packaging.
- Treat local ASR distribution as a separate packaging decision.

## Not Current Bugs Unless Reproduced

These were addressed in the current branch and should only be reopened with new
evidence:

- Backend unavailable because of missing packaged Pipecat/SciPy runtime imports.
- YouTube thumbnails missing due to frontend/backend image path behavior.
- Console windows flashing during backend subprocess work.
- Debug clear-view not working.
- Debug filter overlap in the normal wide layout.
- Live Mic button staying red after recording finishes.
- File tab click working but drag/drop failing.
- Spinner stuck in list after YouTube completion.

## Documentation Policy

For future work:

- Add durable status to this file only if it remains relevant after the task.
- Put implementation details in `docs/ARCHITECTURE.md`.
- Put performance or installer details in `docs/PERFORMANCE_AND_PACKAGING.md`.
- Put test/release gate details in `docs/TESTING_AND_RELEASE.md`.
- Keep temporary experiments in `tmp\` or commit messages.
