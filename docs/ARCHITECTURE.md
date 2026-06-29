# Scriber Architecture

Last verified: 2026-06-29

This document describes the current implementation. It replaces older scattered
architecture notes and should be updated when ownership boundaries change.

## Runtime Overview

Scriber is a hybrid desktop app:

- Tauri 2 shell for installed Windows desktop runtime.
- React 19/Vite 8 frontend rendered inside the Tauri WebView or browser dev
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
  runtime logs, support bundles, and explicit dev/test frontend fallback.
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
- `src/native_overlay.py`: backend facade for the Tauri-owned recording overlay
  controlled through private shell IPC.

The backend remains the source of truth for recording state, device selection,
provider calls, transcript storage, and job lifecycle.
In installed Tauri builds, the Python sidecar does not embed or serve the
production React asset tree. The only backend static frontend fallback is the
explicit `SCRIBER_FRONTEND_DIST_DIR`/source-checkout path used for dev and
tests.

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
Installed frontend assets are owned by Tauri through `frontendDist` and are
loaded from the WebView origin (`http://tauri.localhost`), not from the Python
backend loopback server.

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
- Own the installed frontend asset bundle through Tauri `frontendDist`; the
  backend sidecar remains API-only unless a developer explicitly points
  `SCRIBER_FRONTEND_DIST_DIR` at a frontend build.

Tauri must not become the owner of recording state. Route recording commands
through backend endpoints.

Rust audio:

- `Frontend/src-tauri/src/audio_sidecar.rs` is a separate Cargo binary reserved
  for crash-isolated audio capture work.
- The sidecar currently exposes `--self-test` and `--stdio` JSON-lines commands
  for `ping`, `capabilities`, `captureStart`, `captureStop`, `prewarmStart`,
  `prewarmStop`, and `shutdown`.
- With explicit `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1`, the sidecar can create
  a private Windows named pipe and write synthetic `pcm_i16_le` frames using the
  shared `SAF1` frame protocol. This is a transport/lifecycle harness, not a
  microphone engine.
- With the same explicit synthetic flag, the sidecar can also start a synthetic
  idle prewarm session. It keeps a long-lived sidecar process, tracks observed
  and buffered frame counts, and returns stop-health data through
  `prewarmStop`. This validates prewarm lifecycle plumbing only; it does not
  yet adopt a WASAPI idle stream into active capture.
- By default, the sidecar opens a Windows capture endpoint through WASAPI shared
  mode, converts supported float/PCM mix formats to requested `pcm_i16_le`
  blocks, and writes those blocks to the same `SAF1` frame pipe. Python may pass
  a redacted native endpoint hash for selected-device capture; if a non-default
  request has no native hash, capture fails before first frame. There is no
  Python `sounddevice` capture fallback.
- Private shell IPC exposes `audioEndpointInventory` for Rust/WASAPI capture
  endpoint diagnostics. It returns friendly names, redacted endpoint hashes,
  active state, and default roles without raw IMMDevice IDs. Backend audio
  diagnostics include this as `microphone.rustNativeEndpointInventory`, and the
  private PortAudio-to-native mapping prefers that Rust inventory before
  falling back to PyCAW or PortAudio-only mapping.
- `Frontend/src-tauri/src/audio_sidecar_client.rs` is the Tauri-side client for
  sidecar handshakes. It discovers only allowlisted sidecar executable names,
  supports `SCRIBER_AUDIO_SIDECAR_EXE` for local prototype runs, starts sidecar
  children hidden on Windows, validates protocol/request IDs, keeps successful
  capture sidecars keyed by `streamId`, and reports only redacted path hashes.
- Private shell IPC routes `audioCaptureStart`, `audioCaptureStop`,
  `audioPrewarmStart`, and `audioPrewarmStop` through the sidecar client when
  an executable is available. `SCRIBER_RUST_AUDIO_DISABLE_WASAPI_CAPTURE=1`
  exists only for tests that need the explicit unavailable path.
- `audioCaptureStop` preserves sidecar health fields, including stop reason,
  writer connection state, total/prebuffer/live frames written, bytes written,
  writer error, uptime, PID, and exit status. Python stores these in nested
  active-capture diagnostics for support bundles and long-run smokes.
- Python Rust-frame diagnostics also record frame-pipe frames/audio frames read,
  bytes read, sequence/protocol error counts, first-frame read timing, reader
  end reason, and last frame metadata without exposing raw pipe paths.
- `MicrophoneInput.ensure_stream_health()` can restart source-owned frame
  sources when the reader/stream becomes inactive during active recording. For
  Rust WASAPI capture it first performs `stop(close=false)` so stale
  `streamId` and frame-pipe state are released, then starts a fresh sidecar
  source. Active-capture diagnostics expose health restart count, latest health
  check reason, latest restart reason, and restart error.
- The Rust frame reader distinguishes `SAF1` prebuffer frames from live frames
  and rejects prebuffer frames that arrive after live frames.
- Synthetic and WASAPI sidecar capture can mark the requested leading frames as
  `SAF1` prebuffer frames and return writer-side prebuffer/live counts on stop.
- The sidecar client keeps successful capture processes keyed by `streamId` and
  successful synthetic prewarm processes keyed by `prewarmId`. Backend restart
  and shell exit drain both registries.
- `src/microphone.py` always uses the Rust frame-pipe source for live
  microphone capture. `SCRIBER_AUDIO_ENGINE` is accepted only for backwards
  diagnostic compatibility and no longer selects Python capture.
- `src/mic_prewarm.py` uses the Rust prewarm manager as the only app-level
  idle-prewarm implementation. It keeps `audioPrewarmStart` alive during idle,
  hands the `prewarmId` to the next Rust capture, and records redacted adoption
  diagnostics.
- The passive Rust WASAPI probe and active Rust capture path share the same
  redacted SHA-256/16-hex native endpoint hash contract, so selected-device
  probe evidence is comparable with selected-device capture evidence.
- The standard installer bundles the sidecar under `audio-sidecar/`; this is
  the default live microphone engine.

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

## Provider Boundary

Provider selection is owned by backend configuration and persisted settings.
Soniox is the default STT family: realtime live transcription uses
`stt-rt-v5`, while Soniox Async file and YouTube transcription defaults to
`stt-async-v5`. `SCRIBER_SONIOX_RT_MODEL` and
`SCRIBER_SONIOX_ASYNC_MODEL` remain escape hatches for provider compatibility,
but older Soniox realtime and async models are not release defaults.

Speaker diarization is enabled where the current backend adapter has both a
supported provider request flag and a stable speaker-output path. This covers
Soniox async/direct and Soniox realtime callback formatting, Mistral
async/direct, Smallest AI async/direct, AssemblyAI async/direct, Gladia
pre-recorded file/YouTube transcription, Deepgram live result formatting, and
Speechmatics live speaker formatting. These paths produce anonymous
`[Speaker n]` labels. True known-speaker name identification is not enabled
unless Scriber gains a UI/config source for provider-specific known speaker
names or enrollment identifiers. The current Pipecat OpenAI STT bridge remains
plain transcription because OpenAI's diarize model is exposed through the
transcriptions API response format and needs a dedicated full-audio adapter
rather than a safe factory flag.

The standard sidecar keeps runtime support for the shipped cloud/external
providers exposed in Settings, but the dependency boundary is explicit. Local
ASR choices such as ONNX and NeMo are configuration/UI surfaces with fail-closed
readiness handling in the standard build; a successful installed local-ASR
transcription path requires a separate local-ASR sidecar or packaging decision.
Google Cloud STT is packaged through `google-cloud-speech` plus Pipecat's
required `google-genai` namespace dependency; Gemini and OpenRouter
summarization use direct HTTP and do not require `google-generativeai`.
OpenRouter summary models are sent with `:nitro` variants and are used as the
automatic cross-provider summary fallback when an OpenRouter key is configured.
OpenAI STT uses the explicit `openai` SDK dependency, Groq STT uses Pipecat's
`groq` SDK dependency, and Pipecat provider imports require `nltk` at runtime.
Gladia live transcription
still uses Pipecat's Gladia service, while file and YouTube transcription use
Gladia's pre-recorded HTTP upload/polling API directly to avoid empty
live-WebSocket finalization for complete files. Build-time runtime import checks
cover the offered standard provider modules, and the footprint analyzer rejects
unused provider SDKs if PyInstaller pulls them back in.

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

Legacy Python UI and tray code remain source-only diagnostic fallback. They are
not part of the standard packaged backend and are not the primary architecture
for new Windows desktop behavior.

New desktop lifecycle features should be implemented in Tauri/Rust when they
belong to shell ownership, or in the Python backend when they belong to app
state, provider work, persistence, or recording state.
