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
- Installer size dropped to about `102.98 MiB` with no intended feature loss.
- SciPy is absent from the standard sidecar.
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

PySide6 footprint:

- PySide6 remains required for the native overlay.
- Translation/plugin/software-OpenGL pruning is possible only after installed
  overlay smoke evidence.

Rust audio:

- `SCRIBER_AUDIO_ENGINE=rust-prototype` is request-only.
- The current branch has frame-source boundaries, diagnostics, shared
  frame-pipe protocol helpers, a sidecar skeleton, and a Tauri stdio sidecar
  lifecycle client. It also has an explicit synthetic sidecar frame-pipe
  transport harness for plumbing tests and an explicit
  `SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1` WASAPI capture prototype with redacted
  native-endpoint-hash selection. The sidecar is now bundled as an installed
  resource, but it remains opt-in. Python-side Rust diagnostics include
  frame-pipe read counters, sequence/protocol error counts, first-frame read
  timing, reader end reason, and stop-health fields for support bundles.
  Passive Rust WASAPI probes now use the same redacted native endpoint hash
  contract as active Rust capture, so selected-device probe evidence is no
  longer default-only. Rust/WASAPI endpoint inventory is now exposed through
  private shell IPC and preferred by audio diagnostics for private
  PortAudio-to-native mapping before PyCAW fallback. The hardware matrix
  scripts now persist that redacted endpoint inventory as acceptance evidence
  and can fail promotion if it is missing or falls back away from `rust-wasapi`.
  The Python Rust frame reader also tracks `SAF1` prebuffer/live frame counts
  and rejects prebuffer-after-live interleaving. Always-on startup no longer
  adopts the Python prewarm stream when `SCRIBER_AUDIO_ENGINE=rust-prototype`
  is requested. A Rust prewarm manager now exists behind the same app-wide
  Always-On-Mic lifecycle for the explicit Rust prototype: it keeps
  `audioPrewarmStart` alive while idle and passes the `prewarmId` into the next
  Rust capture for sidecar-local buffered-frame adoption. The app-level smoke
  `scripts/smoke_rust_audio_app_prewarm.py` now verifies that the Python
  manager/source lifecycle performs this handoff against the real sidecar and
  resumes idle prewarm after capture. It also keeps unfavorited default-device
  requests as `devicePreference=default` with no native endpoint hash, so the
  Rust WASAPI sidecar opens the real Windows default capture endpoint directly.
  Non-default Rust capture without a native endpoint hash still fails closed.
  The hybrid release-readiness runner can require this app-level smoke via
  `-RequireRustAudioAppPrewarmSmoke`. It can also require explicit app-level
  Rust Always-On-Mic durations with `-MinRustAudioAppPrewarmDurationSec` and
  `-MinRustAudioAppPrewarmPrewarmDurationSec` plus repeated stop/resume cycles
  with `-MinRustAudioAppPrewarmCaptureCycles`, so the 10-minute active-capture
  / 30-minute idle-prewarm promotion target is machine-checkable. Default-path
  Rust promotion can now also require installed live-recording start/stop
  stability through `-RequireInstalledLiveRecordingSmoke` and
  `-MinInstalledLiveRecordingDurationSec`; this gates the app/installer path
  separately from provider-backed transcript quality and now validates managed
  Tauri runtime metadata plus stability-sample coverage for the requested
  recording duration. The aggregate Rust promotion gate also requires
  installed live-recording Rust-audio sample evidence, so installed reports must
  prove `rust-prototype` / `rust-frame-pipe` active capture, adopted Rust
  prewarm evidence, a closed fallback circuit, and `micAlwaysOn=true` instead
  of only proving generic on-demand live-mic stability.
  The recording hot-path benchmark now has strict provider/Rust promotion
  flags: `--require-provider-transcript` requires a final STT provider segment,
  and `--require-rust-audio-engine` verifies active `rust-prototype`
  `rust-frame-pipe` capture diagnostics during recording. It also rejects open
  Rust fallback-circuit diagnostics, so a cooldown fallback cannot pass as Rust
  capture evidence. The hybrid baseline runner exposes those checks as
  `-RequireRecordingHotPathProviderTranscript` and
  `-RequireRecordingHotPathRustAudio`. When `micAlwaysOn=true` is present in
  the Rust hot-path report, the `rust_audio_engine` requirement also requires
  adopted Rust prewarm evidence before the report can be marked measured.
  `scripts/validate_recording_hot_path_comparison.py` now turns separate
  provider-backed Python and Rust hot-path reports into a required promotion
  artifact, and `run_hybrid_release_readiness.ps1` can gate it with
  `-RequireRecordingHotPathComparison`. The comparison validator also requires
  the same STT provider, the same benchmark configuration, active
  `micAlwaysOn=true` Rust evidence, adopted Rust prewarm evidence on every
  `rust-frame-pipe` sample, and rejects an open Rust fallback circuit in the
  Rust report. Final readiness now also requires at least three samples per
  engine, so one-shot comparison artifacts are not acceptable Rust promotion
  evidence. The same artifact now also fails Rust promotion when local
  audio-owned P95 hot-path segments regress clearly against Python, or when
  Rust frame-pipe callback/audio-frame counters are missing or empty, or when
  Rust active-capture watchdog restart/throttle evidence appears during the
  provider-backed run; the provider-finalize and total stop-to-text values stay
  diagnostic-only because they are network/provider dominated.
  `scripts/run_recording_hot_path_comparison.ps1` now orchestrates the Python
  pass, Rust-prototype pass, and comparison artifact creation for real
  provider-backed A/B runs, defaulting to three recording samples per engine
  and a 50 ms max P95 regression tolerance for local audio-owned segments. The
  aggregate release-readiness runner can now produce that artifact directly
  with `-RunRecordingHotPathComparison`, using `-RustAlwaysOnMic`, before final
  validation.
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
  still needs long physical evidence before default promotion.
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
  prebuffer-after-live frames. A local app-level WASAPI prewarm adoption smoke
  passed on 2026-06-11 with 40 adopted prebuffer blocks, 992 live blocks, no
  sequence/protocol errors, successful idle-prewarm resume, and Windows-default
  endpoint selection evidence. A 30-second installed Rust/WASAPI Always-On-Mic
  live-recording smoke also passed on 2026-06-11 with increasing frame-pipe
  counters, closed fallback circuit, and Windows-default endpoint selection.
  The hardware matrix now records native DeviceMonitor refresh evidence without
  forced per-poll refreshes. The aggregate readiness runner can now also start
  that guided physical matrix directly with `-RunMicrophoneHardwareMatrix` and
  rejects forced poll refreshes whenever native device-refresh evidence is
  required. Actually running the long physical Always-On-Mic and hardware
  matrix evidence, real provider-backed Python/Rust comparison artifacts using
  the aggregate gate, signing/updater publication evidence, and the final
  promotion decision are still open before default promotion.

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
  Python mid-stream, but the next requested Rust-prototype recording uses
  Python during the cooldown and records the circuit-open reason in diagnostics.
  `/api/runtime/audio-diagnostics` exposes that circuit globally, so support
  bundles can explain the fallback even after the failed recording has stopped.
  Recording hot-path summaries, Python/Rust comparison reports, and installed
  live-recording Rust promotion gates now reject explicit
  `midSessionFailureReason` evidence or unexpectedly ended frame-pipe readers,
  so a report with a hidden Rust stream break cannot pass as default-promotion
  evidence.
- Effective runtime audio engine remains Python until a measured Rust prototype
  proves meaningful latency, stability, and maintainability gains.

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
