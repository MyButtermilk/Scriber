# Scriber Architecture

Last verified: 2026-06-10

This document describes the current implementation. It replaces older scattered
architecture notes and should be updated when ownership boundaries change.

## Runtime Overview

Scriber is a hybrid desktop app:

- Tauri 2 shell for installed Windows desktop runtime.
- React 19/Vite 7 frontend rendered inside the Tauri WebView or browser dev
  server.
- Python `aiohttp` backend for local REST, WebSocket, mic recording, provider
  work, media preparation, persistence, logs, and support bundles.
- SQLite database for transcripts and metadata.
- PyInstaller onedir sidecar for the packaged backend.

The installed app is local-first. The backend binds to loopback, and the Tauri
supervisor injects a per-run session token for local control endpoints.

## Main User Workflows

Live mic:

1. Tauri registers the configured global hotkey.
2. Hotkey calls existing backend live-mic endpoints.
3. Python resolves the microphone under the PortAudio guard.
4. Optional idle prewarm stream can be adopted and prepend its rolling prebuffer.
5. Pipecat/provider pipeline processes audio.
6. Transcript text is injected into the active app and saved to SQLite.
7. Frontend receives versioned WebSocket state, audio, transcript, and history
   events.

YouTube:

1. Frontend search or URL lookup calls backend YouTube endpoints.
2. Backend uses YouTube metadata helpers, `yt-dlp`, and bundled ffmpeg/ffprobe.
3. Persistent job metadata tracks download, media preparation, transcription,
   summary, retry, resume, cancel, and completion states.
4. Transcript and summary are saved as a `youtube` transcript.

File:

1. Frontend uploads audio/video using multipart request.
2. Backend enforces upload limits and writes chunks off the event loop where
   practical.
3. Video/audio is normalized through ffmpeg as needed.
4. Provider transcription and optional summarization run as a persistent job.
5. Transcript and summary are saved as a `file` transcript.

## Backend

Key modules:

- `src/web_api.py`: REST/WebSocket app, controller state, jobs, settings,
  runtime logs, support bundles, static frontend fallback.
- `src/pipeline.py`: STT orchestration, service factory, VAD/analyzer caching,
  mic resolution, direct/async transcription helpers.
- `src/microphone.py`: sounddevice transport, stream lifecycle, channel
  selection, audio-level callback throttling.
- `src/mic_prewarm.py`: idle always-on mic prewarm and rolling raw-audio
  prebuffer.
- `src/device_monitor.py`: event-first microphone change detection, native
  Windows endpoint callbacks, sparse polling safety net, PortAudio refresh
  deferral.
- `src/database.py`: SQLite WAL persistence, metadata loading, FTS5 search.
- `src/data/job_store.py`: durable file/YouTube job state.
- `src/data/latency_metrics_store.py`: hot-path metric persistence.
- `src/runtime/media_tools.py`: ffmpeg/ffprobe resolution.
- `src/core/`: REST/WebSocket contracts, state machine, circuit breaker, retry
  and provider support types, hot-path tracing, logging helpers.
- `src/overlay.py`: native recording overlay, PySide6 preferred.

The backend remains the source of truth for recording state, device selection,
provider calls, transcript storage, and job lifecycle.

## Frontend

Key modules:

- `Frontend/client/src/App.tsx`: Wouter routes and route-level lazy loading.
- `Frontend/client/src/pages/LiveMic.tsx`: live recording UI and canvas
  waveform.
- `Frontend/client/src/pages/YouTube.tsx`: YouTube search, URL workflow, recent
  videos, thumbnail display.
- `Frontend/client/src/pages/FileUpload.tsx`: file upload and drag/drop flow.
- `Frontend/client/src/pages/DebugConsole.tsx`: token-protected log viewer and
  support bundle download.
- `Frontend/client/src/contexts/WebSocketContext.tsx`: one shared WebSocket.
- `Frontend/client/src/lib/backend.ts`: browser/dev/Tauri backend URL and token
  handling.
- `Frontend/client/src/lib/api-types.ts`: shared REST-facing types.

The frontend should not own backend lifecycle decisions. In desktop runtime it
asks Tauri commands for backend access and posts the frontend-ready beacon after
health is proven.

## Tauri Shell

`Frontend/src-tauri/src/lib.rs` owns desktop shell duties:

- Start or attach to a backend after validating `/api/health`.
- Choose a free loopback port when the default is occupied.
- Pass `SCRIBER_SESSION_TOKEN` and `SCRIBER_DATA_DIR` to managed workers.
- Enforce a Windows single-instance mutex.
- Register global hotkey through Tauri.
- Own Windows autostart through `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
- Own tray/menu shell actions: open/focus, restart backend, quit.
- Run worker crash recovery and write crash metadata.
- Avoid visible console windows for the Python child on Windows.

Tauri must not become the owner of recording state. Route recording commands
through backend endpoints.

Rust audio prototype:

- `Frontend/src-tauri/src/audio_sidecar.rs` is a separate Cargo binary reserved
  for crash-isolated audio capture work.
- The sidecar currently exposes `--self-test` and `--stdio` JSON-lines commands
  for `ping`, `capabilities`, `captureStart`, `captureStop`, and `shutdown`.
- With explicit `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1`, the sidecar can create
  a private Windows named pipe and write synthetic `pcm_i16_le` frames using the
  shared `SAF1` frame protocol. This is a transport/lifecycle harness, not a
  microphone engine.
- With explicit `SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1`, the sidecar can open a
  Windows capture endpoint through WASAPI shared mode, convert supported
  float/PCM mix formats to requested `pcm_i16_le` blocks, and write those blocks
  to the same `SAF1` frame pipe. Python may pass a redacted native endpoint hash
  for selected-device capture; if a non-default request has no native hash, the
  Rust path fails before first frame and Python falls back to `sounddevice`.
  This is still a prototype path, not the default recording engine.
- `Frontend/src-tauri/src/audio_sidecar_client.rs` is the Tauri-side client for
  prototype handshakes. It discovers only allowlisted sidecar executable names,
  supports `SCRIBER_AUDIO_SIDECAR_EXE` for local prototype runs, starts sidecar
  children hidden on Windows, validates protocol/request IDs, keeps successful
  capture sidecars keyed by `streamId`, and reports only redacted path hashes.
- Private shell IPC routes `audioCaptureStart` and `audioCaptureStop` through
  the sidecar client when an executable is available.
- `captureStart` still returns explicit unavailable status by default unless an
  explicit sidecar capture flag is set.
- `audioCaptureStop` preserves sidecar health fields, including stop reason,
  writer connection state, frames/bytes written, writer error, uptime, PID, and
  exit status. Python stores these in nested active-capture diagnostics for
  support bundles and long-run smokes.
- Python Rust-frame diagnostics also record frame-pipe frames/audio frames read,
  bytes read, sequence/protocol error counts, first-frame read timing, reader
  end reason, and last frame metadata without exposing raw pipe paths.
- Backend restart and shell exit drain the sidecar lifecycle registry before
  restarting the Python backend or exiting the Tauri shell.
- `src/microphone.py` can opt into the Rust prototype through
  `SCRIBER_AUDIO_ENGINE=rust-prototype`, but falls back to Python `sounddevice`
  before the first frame if the sidecar path is unavailable.
- The passive Rust WASAPI probe and active Rust capture path share the same
  redacted SHA-256/16-hex native endpoint hash contract, so selected-device
  probe evidence is comparable with selected-device capture evidence.
- The standard installer bundles the sidecar under `audio-sidecar/`, but the
  default recording engine remains Python `sounddevice`.

## Contracts

REST:

- `/api/health` is public and used for readiness.
- `/api/runtime` is token-protected when `SCRIBER_SESSION_TOKEN` is configured.
- `/api/runtime/frontend-ready` records non-secret proof that the WebView reached
  the backend.
- `/api/runtime/logs` and `/api/runtime/support-bundle` are token-protected.

WebSocket:

- Events include `apiVersion`.
- Known events include `state`, `status`, `transcript`, `audio_level`,
  `input_warning`, `transcribing`, `session_started`, `session_finished`,
  `history_updated`, and `error`.
- Contract builders and validators live in `src/core/ws_contracts.py`.

Tests:

- REST contract tests live under `tests/contract/` and `tests/test_web_api_security.py`.
- WebSocket contract tests live in `tests/contract/test_ws_events.py`.

## Data and Persistence

Runtime data resolves through `src/runtime/paths.py`.

Desktop runtime stores writable data under `SCRIBER_DATA_DIR`:

- `.env`
- `settings.json`
- `transcripts.db` plus WAL/SHM
- `downloads\`
- `models\`
- `logs\`
- `support-bundles\`

The installed app must not rely on writing to the install directory.

## Media Boundary

Media work is centralized around resolved ffmpeg/ffprobe tools:

1. Explicit tool environment variables.
2. `SCRIBER_MEDIA_TOOLS_DIR`.
3. Bundled app-root media tools such as `tools\ffmpeg`.
4. System `PATH`.

Profile B is the standard Windows release media-tool build. It keeps Scriber
requirements such as MP3, WebM/Opus, AAC/Opus/MP3/FLAC/ALAC decode, stdout PCM,
raw `s16le`, `file` and `pipe` protocols, required demuxers/muxers, and local
media workflow support while excluding unrelated network/GPL/nonfree/hardware
stacks.

## Legacy Fallback

Tkinter UI and Python tray code remain useful for diagnostics, development, and
emergency fallback. They are not the primary architecture for new Windows
desktop behavior.

New desktop lifecycle features should be implemented in Tauri/Rust when they
belong to shell ownership, or in the Python backend when they belong to app
state, provider work, persistence, or recording state.
