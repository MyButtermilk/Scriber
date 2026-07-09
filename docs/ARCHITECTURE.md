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
2. Hotkey calls backend live-mic endpoints. A second optional post-processing
   hotkey calls the dedicated live-mic post-processing endpoint; the normal
   hotkey always keeps plain STT output.
3. Python resolves the microphone under the PortAudio guard.
4. Optional idle prewarm stream can be adopted and prepend its rolling prebuffer.
5. Pipecat/provider pipeline processes audio.
6. Transcript text is injected into the active app and saved to SQLite. When a
   session was started through the post-processing hotkey, pipeline raw-text
   injection is suppressed, the completed live transcript is sent through the
   configured LLM prompt using `${output}`, and the processed result is pasted
   after provider finalization. File and YouTube jobs do not use this path.
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
- `Frontend/client/src/pages/DebugConsole.tsx`: token-protected log viewer,
  redacted post-processing diagnostics, and support bundle download.
- `Frontend/client/src/contexts/WebSocketContext.tsx`: one shared WebSocket.
- `Frontend/client/src/lib/backend.ts`: browser/dev/Tauri backend URL and token
  handling.
- `Frontend/client/src/lib/desktop-updates.ts`: Tauri updater guest API wrapper,
  local update cache, weekly automatic-check policy, per-version dismissal,
  reminder deferral, and release-notes opener.
- `Frontend/client/src/lib/api-types.ts`: shared REST-facing types.

The frontend should not own backend lifecycle decisions. In desktop runtime it
asks Tauri commands for backend access and posts the frontend-ready beacon after
health is proven.
Installed frontend assets are owned by Tauri through `frontendDist` and are
loaded from the WebView origin (`http://tauri.localhost`), not from the Python
backend loopback server.
Settings model selectors are credential-gated in the UI: cloud STT,
summarization, and live post-processing choices require the matching provider
API key or credential path before selection. Missing-credential prompts open the
matching API-key dialog directly instead of forcing users to scroll.
Local transcription models remain selectable without credentials.
Desktop update checks are frontend/Tauri-owned rather than Python-backend
work. Installed builds check the configured Tauri updater endpoint in the
background after startup and then about once per week, cache the result in
local storage, and suppress update prompts while recording or transcription is
active. Users can install, defer for a day, skip the current version, or open
release notes from Settings. The custom tray panel mirrors actionable update
state with a blue download indicator and exposes a direct install-and-restart
action when an update is available. Unsigned/dev builds keep the updater plugin
wired but are expected to report that release updater configuration is missing.

## Tauri Shell

`Frontend/src-tauri/src/lib.rs` owns desktop shell duties:

- Start or attach to a backend after validating `/api/health`.
- Choose a free loopback port when the default is occupied.
- Pass `SCRIBER_SESSION_TOKEN` and `SCRIBER_DATA_DIR` to managed workers.
- Enforce a Windows single-instance mutex.
- Register global hotkey through Tauri.
- Register the optional live post-processing hotkey through Tauri and dispatch
  it to `/api/live-mic/toggle-post-processing`.
- Own Windows autostart through `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
- Own tray/menu shell actions: open/focus, restart backend, quit, and tray
  status/icon updates for recording and available desktop updates.
- Render the recording overlay as a non-taskbar, non-focusable window; on
  Windows it is shown without activation so hotkey recordings do not flash the
  main taskbar icon while the user is working in another app.
- Initialize the Tauri updater plugin. Release builds provide updater endpoint,
  public key, and signed artifacts through build-time configuration; Windows
  updater installation runs in Tauri's passive mode.
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
- `/api/runtime/logs`, `/api/runtime/post-processing-diagnostics`, and
  `/api/runtime/support-bundle` are token-protected.
- `/api/metrics/hot-path` includes a bounded `postProcessing` snapshot so live
  dictation failures can be correlated with timing data without storing
  transcript text.

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

Post-processing diagnostics are intentionally metadata-only. The backend records
bounded recent attempts with status, configured model, prompt/output character
counts, duration, fallback state, and sanitized error summaries. These entries
are visible in the Debug Console, included in hot-path metrics, and written to
support bundles as `post-processing-diagnostics.redacted.json`; neither raw
transcript text nor processed output belongs in any of those surfaces.

## Provider Boundary

Provider selection is owned by backend configuration and persisted settings.
Soniox is the default STT family: realtime live transcription uses
`stt-rt-v5`, while Soniox Async file and YouTube transcription defaults to
`stt-async-v5`. `SCRIBER_SONIOX_RT_MODEL` and
`SCRIBER_SONIOX_ASYNC_MODEL` remain escape hatches for provider compatibility,
but older Soniox realtime and async models are not release defaults.
On live Soniox Realtime stop, Scriber sends Soniox's documented empty
end-of-audio WebSocket frame, waits briefly for either a provider receive-task
finish or a final transcript frame, then shuts the local pipeline down. Once a
final transcript frame has arrived, Scriber must not wait on a reconnecting
Soniox receive task; `SCRIBER_SONIOX_RT_STOP_FINAL_TIMEOUT_SECONDS` exists only
as a bounded troubleshooting override for unusually slow finalization.

Speaker diarization is a batch-transcription feature, not a live dictation
feature. File and YouTube jobs enable provider diarization where the current
backend adapter has both a supported provider request flag and a stable
speaker-output path. This covers Soniox async/direct, Mistral async/direct,
Smallest AI async/direct, AssemblyAI async/direct, Gladia pre-recorded
file/YouTube transcription, Deepgram async/direct, OpenAI async/direct, and
Speechmatics async/direct when those providers are used for batch jobs. These paths produce
anonymous `[Speaker n]` labels. Live microphone transcription explicitly
disables speaker diarization and ignores provider speaker metadata at the
callback boundary so single-speaker dictation is inserted as plain text. True
known-speaker name identification is not enabled unless Scriber gains a
UI/config source for provider-specific known speaker names or enrollment
identifiers. OpenAI live dictation uses Pipecat's OpenAI Realtime STT service
with `gpt-realtime-whisper`; full recording/file OpenAI transcription is
exposed through the dedicated `openai_async` direct adapter.

Pipecat/Silero VAD has two separate live-mic roles. It remains enabled as a
speech gate where needed so silent recordings can be cancelled locally without
provider finalization or audio upload. Mid-recording VAD segmentation for
HTTP-style live STT providers is opt-in through `SCRIBER_SEGMENT_SPEECH_WITH_VAD`
and the Settings toggle; by default Scriber opens one recording-wide segment
and closes it when the user stops recording. When the user presses the hotkey
while a live streaming provider is still inside an active VAD speech turn,
Scriber pushes a final `UserStoppedSpeakingFrame` before pipeline shutdown so
Deepgram and ElevenLabs can finalize/commit the last transcript. Mistral Live is
currently a segment-finalized Voxtral transcription path because the bundled
Pipecat runtime does not expose a Mistral realtime service; if an installed
configuration still points `SCRIBER_MISTRAL_RT_MODEL` at Mistral's
realtime-only model, Scriber maps that segmented live path to the configured
Voxtral transcribe model instead.

AssemblyAI is exposed as both a direct async/batch provider and a realtime
Pipecat provider. Both default to Universal-3.5-Pro. The async adapter sends
the configured model through AssemblyAI's `speech_models` field; the realtime
path uses Pipecat `AssemblyAISTTService.Settings` when available, filters
settings by the installed Pipecat signature, and falls back to
`AssemblyAIConnectionParams` for older Pipecat runtimes. In that legacy path,
Scriber's `universal-3-5-pro` setting is mapped to AssemblyAI's supported
streaming `speech_model` names so the service can start on bundled runtimes that
predate the Settings API.

The standard sidecar keeps runtime support for the shipped cloud/external
providers exposed in Settings, but the dependency boundary is explicit. The
standard build bundles the CPU ONNX local-ASR runtime through `onnx-asr`. ONNX
is the only local STT provider exposed in Settings; full NeMo/Torch remains
excluded from the standard sidecar because it would dominate installer size.
The German Primeline Parakeet model is offered through prepared ONNX artifacts
instead of exporting `primeline/parakeet-primeline` on user machines. The
`fp32` option uses the prepared `geier/deskscribe-parakeet-primeline-onnx`
Hugging Face repo, which publishes a DeskScribe ONNX Runtime package ZIP plus
manifest and checksum. Scriber downloads that package set, verifies the
SHA-256, extracts the required ONNX files into the local model cache, and loads
the extracted directory through the existing `onnx-asr` path. The smaller `int8`
Primeline option uses the trusted `Buttermilk03/parakeet-primeline-onnx`
Hugging Face repo, which provides ready `encoder-model-int8.onnx` and
`decoder_joint-model-int8.onnx` files for the same
`primeline/parakeet-primeline` source model. Scriber does not quantize this
model on end-user machines.
Google Cloud STT is packaged through `google-cloud-speech` plus Pipecat's
required `google-genai` namespace dependency and still requires Google Cloud
credentials for a Speech-to-Text project. Gemini STT is a separate direct Gemini
API audio-transcription adapter in `src/cloud_async_stt.py`; it reuses the
stored `GOOGLE_API_KEY` used by Gemini summaries and post-processing so users
can configure the simple Google path with one Gemini API key. Gemini, Cerebras,
and OpenRouter summarization/post-processing use direct HTTP and do not require
`google-generativeai`. Direct Cerebras calls use `cerebras/gemma-4-31b`, which
is the live post-processing default. Most OpenRouter summary fallback models are
sent with `:nitro` variants; `openai/gpt-oss-120b` keeps explicit OpenRouter
provider ordering through `baseten,cerebras` when selected. OpenRouter remains
the automatic cross-provider summary fallback when an OpenRouter key is
configured.
OpenAI live STT uses Pipecat's OpenAI Realtime STT service plus the explicit
`openai` SDK and `websockets` dependencies; OpenAI async/batch uses the direct
Audio Transcriptions HTTP adapter. Groq STT uses Pipecat's `groq` SDK
dependency, and Pipecat provider imports require `nltk` at runtime.
Gladia live transcription still uses Pipecat's Gladia service with a small
Scriber stop wrapper that sends `stop_recording` and waits briefly for a final
transcript before websocket cleanup, while `gladia_async`, file, and YouTube
transcription use Gladia's pre-recorded HTTP upload/polling API directly to
avoid empty live-WebSocket finalization for complete files.
`deepgram_async`, `openai_async`, `gemini_stt`, and
`speechmatics_async` are implemented as direct HTTP/batch adapters in
`src/cloud_async_stt.py`; the Speechmatics batch path intentionally avoids
adding the separate `speechmatics-batch` SDK to the standard sidecar. Build-time
runtime import checks cover the offered standard provider modules, and the
footprint analyzer rejects unused provider SDKs if PyInstaller pulls them back
in.

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
