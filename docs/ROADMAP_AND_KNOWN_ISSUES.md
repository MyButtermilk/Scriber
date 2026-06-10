# Roadmap And Known Issues

Last verified: 2026-06-10

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

Physical hardware evidence:

- Scripts exist for a microphone hardware matrix.
- Final release-readiness still needs real physical runs for USB, Bluetooth,
  dock connect/disconnect, Windows default changes, and favorite fallback.

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
  PortAudio-to-native mapping before PyCAW fallback.
  The Python Rust frame reader also tracks `SAF1` prebuffer/live frame counts
  and rejects prebuffer-after-live interleaving, but Rust-side always-on
  prewarm adoption is still not complete.
  A local physical Windows WASAPI sidecar smoke passed on 2026-06-10 with
  600.004 seconds observed default capture, selected native-endpoint-hash
  capture, no sequence gaps, matching reader/writer frame counts, and no
  prebuffer-after-live frames. Physical device matrix, provider-backed
  transcription, and Rust prewarm parity remain open before default promotion.
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
