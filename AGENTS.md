# Scriber Agent Guide

Last verified: 2026-06-10

This is the working guide for agents editing Scriber. Keep it current when the
implementation changes. Prefer code and tests over older prose when they
conflict, then update the docs in the same task.

## Active Documentation

The repository intentionally keeps only a small documentation set:

- `README.md`: user-facing overview, setup, configuration, and basic commands.
- `AGENTS.md`: this editing guide.
- `docs/ARCHITECTURE.md`: current system architecture and ownership boundaries.
- `docs/PERFORMANCE_AND_PACKAGING.md`: implemented performance work, Profile B
  ffmpeg, sidecar packaging, installer size, and remaining size/perf ideas.
- `docs/TESTING_AND_RELEASE.md`: test commands, smoke gates, installer builds,
  CI, signing, and updater status.
- `docs/ROADMAP_AND_KNOWN_ISSUES.md`: current open issues and prioritized next
  work.

Old implementation journals and superseded analysis docs were removed in the
2026-06-09 consolidation. Do not recreate fragmented one-off status files unless
the user explicitly asks for a temporary investigation note.

## Product Snapshot

- Scriber is an AI transcription app for live microphone dictation, YouTube
  transcription, file transcription, transcript management, summaries, and
  PDF/DOCX export.
- Primary desktop runtime: Tauri 2 shell, React frontend, Python backend sidecar.
- Backend default: `127.0.0.1:8765`, implemented with `aiohttp`, WebSocket
  events, SQLite, Pipecat pipeline code, and provider adapters.
- Frontend default in dev: `localhost:5000`, implemented with Vite 7, React 19,
  TypeScript, Tailwind v4, Wouter, and TanStack Query.
- Runtime is Windows-first. Linux/macOS support is mostly fallback/dev support.
- Legacy Tkinter/Python tray code remains maintenance fallback, not the primary
  direction for new desktop work.

## Repository Map

Backend and runtime:

- `src/web_api.py`: main aiohttp controller, routes, WebSocket server, settings,
  jobs, transcript history, mic control, uploads, logs, support bundles.
- `src/pipeline.py`: STT pipeline orchestration, provider factory, analyzer
  cache, mic resolution, async/direct transcription.
- `src/microphone.py`: engine-neutral `AudioFrameSource` boundary, Python
  `sounddevice` frame source, opt-in Rust prototype frame-pipe reader, channel
  selection, RMS callback, stream lifecycle.
- `src/mic_prewarm.py`: optional idle mic prewarm and rolling prebuffer.
- `src/device_monitor.py`: microphone hotplug monitor, native Windows endpoint
  callbacks, polling fallback, PortAudio refresh deferral.
- `src/audio_devices.py`: microphone normalization, compatibility filtering, and
  private PortAudio-to-native endpoint mapping with redacted endpoint hashes.
- `src/audio_file_input.py`, `src/youtube_download.py`, `src/runtime/media_tools.py`:
  ffmpeg/ffprobe resolution and media preparation.
- `src/database.py`: SQLite WAL persistence, metadata loading, FTS5 search.
- `src/data/job_store.py`: persistent file/YouTube jobs.
- `src/data/latency_metrics_store.py`: hot-path metrics.
- `src/core/`: contracts, state machine, circuit breaker, logging, tracing.
- `src/runtime/audio_frame_pipe.py`: Python decoder/validator for the future
  Rust audio frame-pipe protocol.
- `src/overlay.py`: native mic overlay, PySide6 preferred, Tk fallback.
- `src/tray.py`, `src/main.py`, `src/ui.py`: legacy fallback desktop paths.

Frontend and shell:

- `Frontend/client/src/App.tsx`: routes and lazy loading.
- `Frontend/client/src/pages/`: Live Mic, YouTube, File, Settings, Debug Console,
  Transcript Detail.
- `Frontend/client/src/contexts/WebSocketContext.tsx`: shared WebSocket.
- `Frontend/client/src/lib/backend.ts`: backend URL and Tauri token bridge.
- `Frontend/client/src/lib/api-types.ts`: shared REST-facing TS types.
- `Frontend/client/src/index.css`: Tailwind v4 CSS-first design system.
- `Frontend/src-tauri/src/audio_sidecar.rs`: separate Rust audio sidecar
  prototype with `--self-test`, `--stdio` JSON-lines protocol, explicit
  `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1` frame-pipe transport harness, and
  explicit `SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1` default-endpoint WASAPI capture
  path. It is bundled as an installed resource but remains inactive unless the
  Rust audio prototype is explicitly enabled.
- `Frontend/src-tauri/src/audio_sidecar_client.rs`: Tauri-side sidecar lookup,
  stdio JSON-lines client, and prototype process lifecycle registry. It only
  uses allowlisted executable names, supports `SCRIBER_AUDIO_SIDECAR_EXE` for
  prototype runs, keeps successful capture sidecars keyed by `streamId`, and
  redacts executable paths to hashes in diagnostics.
- `Frontend/src-tauri/src/audio_frame_pipe.rs`: Rust encoder/validator for the
  future audio sidecar binary frame protocol.
- `Frontend/src-tauri/src/lib.rs`: Rust supervisor, Tauri commands, tray/menu,
  autostart, global hotkey, single instance, updater/process plugins.
- `Frontend/src-tauri/src/shell_ipc.rs`: private backend-to-shell named-pipe
  IPC for opt-in native shell work, including text injection and diagnostics.
- `Frontend/src-tauri/tauri.conf.json`: Tauri build, CSP, NSIS bundle, backend
  resource mapping, before-bundle sidecar command.

Packaging and scripts:

- `packaging/scriber-backend.spec`: PyInstaller onedir backend sidecar spec.
- `scripts/build_tauri_backend_sidecar.ps1`: sidecar build, runtime import
  checks, media-tool bundling, optional cache reuse.
- `scripts/build_windows.ps1`: Windows installer orchestration.
- `scripts/ffmpeg/build_profile_b_msys2.ps1`: Profile B custom ffmpeg build.
- `scripts/smoke_*.ps1` and `scripts/smoke_*.py`: installed app, desktop,
  frontend, media, and workflow gates.

## Non-Negotiable Contracts

### Tauri Runtime

- Tauri is the primary desktop runtime.
- The Rust supervisor validates `/api/health` before attaching to a backend.
- Managed workers receive `SCRIBER_RUNTIME_MODE=tauri-supervised`,
  `SCRIBER_WEB_HOST`, `SCRIBER_WEB_PORT`, `SCRIBER_SESSION_TOKEN`,
  `SCRIBER_BACKEND_LAUNCH_KIND`, optional private shell IPC env
  `SCRIBER_SHELL_IPC_PIPE`, `SCRIBER_SHELL_IPC_TOKEN`,
  `SCRIBER_SHELL_IPC_API_VERSION`, and writable `SCRIBER_DATA_DIR`.
- `/api/health` remains public. Token-protected endpoints must accept the
  session token via `scriberToken` query parameter or `X-Scriber-Token`.
- `POST /api/runtime/frontend-ready` is the proof that the actual WebView reached
  the runtime backend.
- Rust owns Windows autostart, global hotkey registration, single-instance
  startup, tray/menu shell actions, and worker crash recovery.
- Rust also exposes a private shell IPC channel for opt-in native text
  injection. `SCRIBER_INJECT_METHOD=tauri` is strict; `auto` must stay on the
  existing Python paste path until installed target-app evidence justifies a
  default change.
- The same private shell IPC may expose opt-in native diagnostics such as
  `audioProbe`. These diagnostics are not public API, must not expose raw
  endpoint IDs, and must not become an active capture path unless the Rust audio
  prototype passes the documented gates.
- Private shell IPC also reserves `audioCaptureStart`, `audioCaptureStop`,
  `audioPrewarmStart`, and `audioPrewarmStop` for the Rust audio prototype. The
  shell may attempt an allowlisted `scriber-audio-sidecar --stdio` handshake.
  Without
  `SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1` or
  `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1`, `audioCaptureStart` must fail
  explicitly and Python must fall back before the first frame.
- `scriber-audio-sidecar` is a separate Cargo binary for crash-isolated audio
  work. Until lifecycle, watchdog, and physical-device gates are added, do not
  depend on it for the standard default capture path.
- Backend restart and Tauri exit must call the audio sidecar cleanup path before
  backend process changes or shell exit.
- Python owns recording state and provider work.

### REST and WebSocket Contracts

- WebSocket events are versioned with `apiVersion`.
- Use builders and validators in `src/core/ws_contracts.py` when adding events.
- `/api/health`, `/api/runtime`, and frontend-ready payloads are versioned and
  validated through `src/core/rest_contracts.py`.
- Add or update contract tests when changing payload shape.
- Frontend REST consumers should use `Frontend/client/src/lib/api-types.ts`
  instead of ad hoc `any` boundaries.

### Microphone and Device Handling

- Keep PortAudio access guarded through the shared device guard lock.
- Do not enumerate or refresh PortAudio devices while an active stream is being
  torn down unless the existing guarded/deferred path handles it.
- `DeviceMonitor` should use native Windows endpoint events where available.
  With active native events, polling is only a sparse safety net; faster polling
  is fallback-only when native events are unavailable.
- Device refresh is recording-aware and can be deferred until idle.
- Native endpoint IDs must stay private. Use hashed native endpoint IDs in
  diagnostics and prototype mapping; do not expose raw IMMDevice IDs as public
  microphone IDs or log fields.
- `SCRIBER_AUDIO_ENGINE=rust-prototype` and `SCRIBER_RUST_AUDIO_PROBE=1` may
  run a passive WASAPI diagnostics probe. `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1`
  may run the sidecar's synthetic frame-pipe transport harness for tests and
  prototype plumbing only. The same synthetic flag may run the sidecar's
  synthetic prewarm lifecycle harness through private shell IPC; this is not yet
  real WASAPI idle-stream adoption. `SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1` may
  run the sidecar's opt-in WASAPI capture prototype, including selected-endpoint
  capture by redacted native endpoint hash. Non-default Rust capture without a
  native endpoint hash must fail before first frame and let Python fall back to
  `sounddevice`; it must not silently use the Windows default endpoint. The
  default capture path remains Python `sounddevice` until a measured Rust
  prototype is explicitly promoted.
- The Rust audio frame-pipe protocol is length-prefixed and versioned. Keep the
  Rust and Python header fixtures in sync when changing it.
- The opt-in Rust prototype may read frame-pipe PCM into Python, but if capture
  fails before the first frame, the recording falls back to Python `sounddevice`
  for that session. Do not silently switch engines after frames have been
  delivered.
- Preserve Rust audio stop-health diagnostics across all layers: sidecar stop
  reason, writer connection state, frames/bytes written, writer error, uptime,
  PID, exit status, reader-thread liveness, prewarm session counters, and
  restart counts must stay available in nested active-capture or prewarm
  diagnostics.
- `SCRIBER_MIC_ALWAYS_ON` is implemented as idle prewarm plus bounded rolling
  prebuffer. Do not reuse Pipecat session state across recordings.
- `MicrophoneInput` still queues raw callback frames; only visualizer/input RMS
  work is throttled to about 60 Hz.

### Providers and Media

- Azure MAI defaults to `mai-transcribe-1.5`.
- Keep `SCRIBER_AZURE_MAI_MODEL=mai-transcribe-1` available as region/resource
  fallback.
- For Azure MAI 1.5, `SCRIBER_CUSTOM_VOCAB` is sent as `phraseList`.
- Azure MAI upload preparation is latency-first: existing MP3 uploads directly,
  non-MP3 inputs are transcoded to mono 64k MP3, and live PCM buffers are encoded
  to MP3 before upload. Do not restore WAV upload without measured provider need.
- FFmpeg Profile B is the standard Windows bundled media-tool path. Gyan
  Essentials is explicit fallback only.
- Keep ffmpeg and ffprobe bundled in the standard installer. `-SkipBundledFfprobe`
  is an experiment, not the release default.
- Do not remove PySide6. It is used for the native mic overlay; Tk is fallback.

### Data and Diagnostics

- Runtime data belongs under `SCRIBER_DATA_DIR`, not the install directory.
- Legacy runtime data migration must not overwrite existing app-data files.
- Support bundles must redact API keys, session tokens, bearer tokens, and known
  secret patterns.
- Backend logs: `logs\tauri-backend.log`.
- Shell logs: `logs\tauri-shell.log`.
- Crash metadata: `logs\backend-crash-metadata.jsonl`.
- Debug console uses `/api/runtime/logs`, `DELETE /api/runtime/logs`, and
  `/api/runtime/support-bundle`.

## Performance Status To Preserve

Already implemented and should not be regressed:

- Lazy STT provider imports.
- Cached VAD/analyzer setup.
- No-client WebSocket broadcast fast path.
- About 60 Hz audio-level throttling.
- Canvas/RAF waveform drawing instead of per-frame React state.
- Buffered transcript appends for long live sessions.
- Paginated transcript endpoints and virtualized history lists.
- Coalesced `history_updated` events.
- Chunked/offloaded upload writes and export/cleanup work where practical.
- JobStore and latency metrics store connection reuse.
- CORS origin decision cache.
- Sidecar hash cache that avoids PyInstaller when inputs are unchanged.
- Profile B ffmpeg media tools, about `5.84 MiB` installed.

## Commands

Run from repository root unless stated.

```powershell
python -m pytest
```

```powershell
cd Frontend
npm run check
npm run build
```

```powershell
cd Frontend\src-tauri
cargo test
```

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

Broader installed workflow smoke when provider credentials and network are
available:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -UseProfileBFfmpeg `
  -ValidateSlimMediaTools `
  -ReuseSidecarIfUnchanged `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke `
  -RunInstallerRealMediaWorkflowSmoke
```

Frontend browser smoke:

```powershell
python scripts\smoke_frontend_browser.py --output tmp\frontend-browser-smoke.json
```

Rust audio sidecar short physical smoke:

```powershell
python scripts\smoke_rust_audio_sidecar.py --mode wasapi --duration-sec 1 --output tmp\rust-audio-sidecar-smoke.json
```

Rust audio promotion readiness gate:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunRustAudioSidecarSmoke `
  -RequireRustAudioSidecarSmoke `
  -RustAudioSidecarDurationSec 600
```

## Editing Guidance

- Keep edits scoped to the feature or bug being addressed.
- Preserve established local patterns before adding abstractions.
- Add tests when changing contracts, pipeline lifecycle, provider behavior,
  packaging gates, or user-visible workflows.
- Use docs only for durable facts and decisions. Put temporary investigation
  output in `tmp\` or commit messages, not new permanent markdown files.
- When changing implementation status, update `README.md`, this file, or the
  relevant category doc in the same change.
