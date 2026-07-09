# Scriber Agent Guide

Last verified: 2026-06-11

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
- Frontend default in dev: `localhost:5000`, implemented with Vite 8, React 19,
  TypeScript, Tailwind v4, Wouter, and TanStack Query.
- Runtime is Windows-first. Linux/macOS support is mostly fallback/dev support.
- Legacy Python tray/UI code was removed. The Tauri shell owns desktop UI,
  tray/menu actions, global hotkeys, and the recording overlay.

## Repository Map

Backend and runtime:

- `src/web_api.py`: main aiohttp controller, routes, WebSocket server, settings,
  jobs, transcript history, mic control, uploads, logs, support bundles.
- `src/pipeline.py`: STT pipeline orchestration, provider factory, analyzer
  cache, mic resolution, async/direct transcription.
- `src/microphone.py`: live microphone capture boundary backed by the Rust
  WASAPI frame-pipe source, channel selection, RMS callback, stream lifecycle.
- `src/mic_prewarm.py`: Rust/WASAPI idle mic prewarm and rolling prebuffer.
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
- `src/runtime/audio_frame_pipe.py`: Python decoder/validator for the Rust
  audio frame-pipe protocol.
- `src/native_overlay.py`: Python facade for the Tauri-owned recording overlay
  exposed through private shell IPC.
- `src/main.py`: compatibility notice for the removed Python desktop UI; use
  Tauri for desktop runs.

Frontend and shell:

- `Frontend/client/src/App.tsx`: routes and lazy loading.
- `Frontend/client/src/pages/`: Live Mic, YouTube, File, Settings, Debug Console,
  Transcript Detail.
- `Frontend/client/src/contexts/WebSocketContext.tsx`: shared WebSocket.
- `Frontend/client/src/lib/backend.ts`: backend URL and Tauri token bridge.
- `Frontend/client/src/lib/api-types.ts`: shared REST-facing TS types.
- `Frontend/client/src/index.css`: Tailwind v4 CSS-first design system.
- `Frontend/src-tauri/src/audio_sidecar.rs`: separate Rust audio sidecar with
  `--self-test`, `--stdio` JSON-lines protocol, a test-only
  `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1` frame-pipe transport harness, and
  default WASAPI capture/prewarm support. It is bundled once as Tauri's
  install-root sidecar executable and is the standard live-mic capture engine.
- `Frontend/src-tauri/src/audio_sidecar_client.rs`: Tauri-side sidecar lookup,
  stdio JSON-lines client, and process lifecycle registry. It only uses
  allowlisted executable names, supports `SCRIBER_AUDIO_SIDECAR_EXE` for local
  test runs, keeps successful capture sidecars keyed by `streamId`, and redacts
  executable paths to hashes in diagnostics.
- `Frontend/src-tauri/src/audio_frame_pipe.rs`: Rust encoder/validator for the
  audio sidecar binary frame protocol.
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
- `scripts/run_hybrid_release_readiness.ps1 -RunReleaseBuild` may invoke
  `scripts/build_windows.ps1` as an evidence producer, but it still requires
  real updater signing secrets, HTTPS publication, and Authenticode signing
  evidence for final readiness.

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
- Rust registers both live-mic shortcuts. The normal hotkey must keep plain
  live dictation output. The post-processing hotkey must dispatch only to the
  dedicated live-mic post-processing endpoint and must not affect File or
  YouTube jobs.
- Rust initializes the Tauri updater plugin, but frontend code owns update
  checks and user-facing update UX. Keep update checks non-blocking, cached,
  about weekly by default, and suppress automatic prompts while recording or
  transcription is active. Do not add a Python backend updater cron or ping.
  Production update builds must use signed Tauri updater artifacts, a public
  HTTPS `latest.json`, and publication verification. `scripts/build_windows.ps1`
  may accept a local `TAURI_SIGNING_PRIVATE_KEY_PATH`, but it must normalize it
  to `TAURI_SIGNING_PRIVATE_KEY` before invoking Tauri; do not commit updater
  private keys. If `latest.json` lists a signed updater artifact, the matching
  sibling `.sig` file is required in collected release assets; do not silently
  upload a signed metadata file without the corresponding signature asset.
- Rust also exposes a private shell IPC channel for opt-in native text
  injection. `SCRIBER_INJECT_METHOD=tauri` is strict; `auto` must stay on the
  existing Python paste path until installed target-app evidence justifies a
  default change. Clipboard-based injection paths, including the default Python
  paste path and Tauri `injectText`, must preserve a bounded snapshot of safe
  HGLOBAL-backed clipboard formats before setting transcript text, then restore
  that snapshot only if the clipboard sequence is unchanged; do not regress this
  to text-only clipboard preservation, and do not call `GlobalSize`/`GlobalLock`
  on handle formats such as `CF_BITMAP` or `CF_ENHMETAFILE`.
- Shell IPC diagnostics may expose the latest `injectText` attempt only in
  sanitized form: error codes, fallback reason, allowed markers, restore status,
  `preDelayMode`, requested/applied pre-delay numbers, timing numbers, and
  hashed foreground identifiers. Never store transcript text, raw pipe names,
  session tokens, raw HWNDs, raw window titles, or raw process identifiers in
  diagnostics or support bundles.
- Readiness can produce the safe Tauri injection smoke with
  `-RunTauriTextInjectionSmoke` only when Shell IPC env vars are present. The
  full target-app matrix still needs real scenario reports; the runner may
  aggregate them with `-RunTauriTextInjectionMatrixBuilder`, but must not
  replace the manual Notepad/Office/browser/Electron/elevated/clipboard
  coverage with validate-only evidence.
- The same private shell IPC exposes native diagnostics such as `audioProbe`.
  These diagnostics are not public API and must not expose raw endpoint IDs.
- Native Windows device-event diagnostics are surfaced through
  `microphone.nativeDeviceEvents` in `/api/runtime/audio-diagnostics`, backed by
  private shell IPC command `nativeDeviceEventsStatus`. Keep this status
  redacted: event counters, mode, COM/registration state, post results, hashes,
  and age/timing values are allowed; raw IMMDevice endpoint IDs are not.
- Private shell IPC routes `audioCaptureStart`, `audioCaptureStop`,
  `audioPrewarmStart`, and `audioPrewarmStop` through an allowlisted
  `scriber-audio-sidecar --stdio` handshake. Normal WASAPI capture/prewarm is
  enabled by default; `SCRIBER_RUST_AUDIO_DISABLE_WASAPI_CAPTURE=1` exists for
  tests that need to force the unavailable path. Python must fail visibly if
  the sidecar cannot deliver frames; do not add a Python capture fallback.
- `scriber-audio-sidecar` is a separate Cargo binary for crash-isolated audio
  work and is the standard live-mic capture path.
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
- Physical microphone matrix evidence is native-event-first. Use
  `-RequireDeviceRefreshEvidence` for Rust-promotion gates so artifacts prove
  native events, sparse safety polling, and zero forced per-poll refreshes.
  `-ForceRefreshEachPoll` is legacy diagnostic fallback only. The aggregate
  readiness runner can produce the guided matrix with
  `-RunMicrophoneHardwareMatrix`; when native refresh evidence is required it
  must reject forced per-poll refreshes.
- Native endpoint IDs must stay private. Use hashed native endpoint IDs in
  diagnostics and prototype mapping; do not expose raw IMMDevice IDs as public
  microphone IDs or log fields.
- Rust/WASAPI is the default and only live microphone capture path. The old
  Python `sounddevice` capture and Python idle-prewarm path have been removed;
  `sounddevice` may still be used for device listing and PortAudio-to-native
  endpoint mapping until those helper surfaces are fully native.
- `SCRIBER_AUDIO_ENGINE` is retained only as a backwards-compatible diagnostic
  input; it no longer selects Python capture. `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1`
  may run the sidecar's synthetic frame-pipe transport harness for tests only.
  Normal WASAPI capture/prewarm is available without
  `SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1`; `SCRIBER_RUST_AUDIO_DISABLE_WASAPI_CAPTURE=1`
  exists for tests that need the unavailable path. Within a single sidecar
  session, `captureStart` may adopt a matching `prewarmId` and write those
  buffered frames before live audio. With `SCRIBER_MIC_ALWAYS_ON=1`, the backend
  uses a Rust prewarm manager that keeps `audioPrewarmStart` alive while idle
  and passes its `prewarmId` to the next Rust capture. The
  Rust prewarm watchdog must verify live sidecar state with `audioPrewarmStatus`;
  a cached `prewarmId` alone is not proof that the microphone stream is still
  active. Status diagnostics must keep prewarm IDs redacted and preserve
  response time, active/inactive reason, buffered-frame counters, and restart
  counts. The prewarm diagnostics also expose a bounded redacted `recentEvents`
  timeline for start/stop/adoption/watchdog restarts so short Windows
  privacy-indicator dropouts are visible in support bundles without increasing
  steady-state log volume. Rust app-prewarm promotion evidence must include
  `recentEvents` lifecycle markers for pre-adoption start and post-resume
  adoption/resume/restart, not only a final healthy status snapshot. Always-on
  handoff is latency- and privacy-indicator-sensitive: when WASAPI capture
  adopts non-empty prewarm blocks, do not stop the idle `PrewarmSession` in the
  parent `captureStart` handler. Transfer that session into the capture writer,
  write the adopted prebuffer, start the replacement WASAPI `IAudioClient`, and
  stop prewarm only after `IAudioClient.Start()` succeeds. Early failures must
  stop the deferred session with explicit reasons such as `captureStartFailed`
  or `captureWriterFinishedBeforePrewarmHandoff`. This keeps
  `SCRIBER_MIC_ALWAYS_ON=1` optimized for minimum hotkey latency and prevents a
  visible Windows microphone privacy-indicator off/on blink between idle
  prewarm and live capture. The Rust prewarm path is the app default. When no
  favorite/non-default mic is selected, keep the request as
  `devicePreference=default` with no `nativeEndpointIdHash`; the Rust sidecar
  must open the Windows default WASAPI capture endpoint directly so the visible
  microphone privacy indicator matches the active device. For selected or
  favorite microphones, the backend should prefer the private Tauri
  `audioEndpointInventory` shell IPC response for native endpoint inventory and
  use Python/PyCAW inventory only as fallback. Active capture, prewarm, and
  passive Rust probe selection should all use the same Rust/Tauri endpoint hash
  when available. Non-default Rust capture without a native endpoint hash must
  fail before first frame; it must not silently use the Windows default endpoint
  or attach default-device metadata to a resolved favorite.
- The Rust audio frame-pipe protocol is length-prefixed and versioned. Keep the
  Rust and Python header fixtures in sync when changing it.
- Rust frame-pipe PCM is read into Python for downstream Pipecat/provider
  processing. If capture fails before the first frame, the recording fails
  visibly; do not reintroduce a Python capture fallback.
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

- Soniox Async defaults to `stt-async-v5`. Keep
  `SCRIBER_SONIOX_ASYNC_MODEL` as an override for temporary compatibility, but
  do not restore `stt-async-v4` as the code default.
- Soniox realtime live transcription defaults to `stt-rt-v5`. Keep
  `SCRIBER_SONIOX_RT_MODEL` as an override for temporary compatibility, but do
  not restore `stt-rt-v4` as the code default.
- Live microphone transcription must not request or format provider speaker
  diarization. Keep `enable_speaker_diarization=False` for live pipelines so
  single-speaker dictation inserts plain text. File and YouTube jobs may enable
  diarization where the provider adapter has stable anonymous speaker output.
- Keep Silero/Pipecat VAD speech gating separate from mid-recording speech
  segmentation. VAD may be used by default to skip silent live recordings before
  provider finalization/upload. Pause-based segmentation for HTTP-style
  segmented live STT providers is opt-in via `SCRIBER_SEGMENT_SPEECH_WITH_VAD`
  and the Settings toggle; the default live behavior is one recording-wide
  segment flushed on stop. In Settings, keep those HTTP-style live providers in
  the cloud async/batch group rather than a separate segmented provider group.
- Live microphone post-processing is opt-in per session through the second
  hotkey. When active, suppress pipeline raw-text injection, wait for final STT
  text after stop, run the configured LLM prompt with the `${output}` raw text
  placeholder, and paste the processed output. If post-processing fails, retain
  and insert the raw transcript. Do not route File or YouTube jobs through this
  path.
- Azure MAI defaults to `mai-transcribe-1.5`.
- Keep `SCRIBER_AZURE_MAI_MODEL=mai-transcribe-1` available as region/resource
  fallback.
- For Azure MAI 1.5, `SCRIBER_CUSTOM_VOCAB` is sent as `phraseList`.
- Azure MAI upload preparation is latency-first: existing MP3 uploads directly,
  non-MP3 inputs are transcoded to mono 64k MP3, and live PCM buffers are encoded
  to MP3 before upload. Do not restore WAV upload without measured provider need.
- AssemblyAI defaults to Universal-3.5-Pro for both async/batch and realtime
  paths. Keep `SCRIBER_ASSEMBLYAI_ASYNC_MODEL` and
  `SCRIBER_ASSEMBLYAI_RT_MODEL` as temporary compatibility overrides, but do not
  restore Universal-3 as the release default.
- AWS Transcribe is no longer a supported frontend/backend provider. Keep
  `boto3`, `botocore`, `s3transfer`, `aioboto3`, `aiobotocore`, and Pipecat AWS
  service modules out of the standard sidecar unless AWS support is explicitly
  reintroduced.
- Standard provider packaging uses explicit SDK dependencies instead of broad
  Pipecat provider extras. Keep `google-generativeai` and Google Cloud
  Text-to-Speech out of the standard sidecar unless a product path is
  reintroduced that actually imports them. Gemini summarization and Gemini STT
  use direct HTTP with `GOOGLE_API_KEY`; this is the simple Google path and
  should stay separate from Google Cloud Speech credentials. Direct Cerebras
  summarization/post-processing uses the OpenAI-compatible Cerebras chat
  completions endpoint and `cerebras/gemma-4-31b` is the live
  post-processing default. OpenRouter summarization and post-processing use
  direct HTTP chat completions. Most OpenRouter fallback models use `:nitro`
  variants for throughput-sorted provider routing; `openai/gpt-oss-120b` must
  be routed with OpenRouter provider order `baseten,cerebras` instead of adding
  `:nitro`. Google Cloud STT uses
  `google-cloud-speech` plus Pipecat's required `google-genai` namespace
  dependency, OpenAI STT uses the explicit `openai`
  SDK dependency, Groq STT uses Pipecat's `groq` SDK dependency, and Pipecat
  provider imports require `nltk` at runtime. Gladia live transcription uses
  Pipecat's Gladia service; Gladia file and YouTube transcription use the
  direct pre-recorded HTTP upload/polling API and should not be routed through
  the live WebSocket pipeline. The direct async adapters
  `deepgram_async`, `gladia_async`, `openai_async`, `gemini_stt`, and
  `speechmatics_async` live in `src/cloud_async_stt.py`; keep them as direct
  HTTP/batch adapters unless a measured provider SDK change justifies adding
  more packaged dependencies. Do not add `speechmatics-batch` to the standard
  sidecar while the direct Speechmatics batch API path is sufficient. Keep
  `onnx-asr[cpu,hub]` in the standard sidecar for the ONNX local-ASR path and
  NeMo UI fallback, but keep full NeMo/Torch out of the standard sidecar.
  Primeline Parakeet support uses the prepared
  `Buttermilk03/parakeet-primeline-onnx` Hugging Face snapshot. Preserve its
  validated `int8` and `fp32` quantization metadata; fp32 uses ONNX external
  data and must be loaded from the complete snapshot, not from onnx-asr's
  narrow default file list.
- FFmpeg Profile B is the standard Windows bundled media-tool path. Gyan
  Essentials is explicit fallback only.
- Keep ffmpeg and ffprobe bundled in the standard installer. `-SkipBundledFfprobe`
  is an experiment, not the release default.
- Keep PySide6, customtkinter, and Tk overlay fallbacks out of the standard
  sidecar. Installed recording overlay rendering is owned by Tauri/Rust.

### Data and Diagnostics

- Runtime data belongs under `SCRIBER_DATA_DIR`, not the install directory.
- Legacy runtime data migration must not overwrite existing app-data files.
- Support bundles must redact API keys, session tokens, bearer tokens, and known
  secret patterns.
- Post-processing diagnostics are redacted runtime metadata only. They may
  include model, prompt/output sizes, duration, status, and sanitized error type
  or message, but must never include raw transcript text or processed output.
- Backend logs: `logs\tauri-backend.log`.
- Shell logs: `logs\tauri-shell.log`.
- Crash metadata: `logs\backend-crash-metadata.jsonl`.
- Debug console uses `/api/runtime/logs`, `DELETE /api/runtime/logs`, and
  `/api/runtime/support-bundle`; post-processing debug state is exposed through
  `/api/runtime/post-processing-diagnostics` and the `postProcessing` hot-path
  metrics snapshot.

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
  The sidecar cache key normalizes `src/version.py`, uses media-tool content
  hashes instead of timestamps, and must be computed before requiring
  PyInstaller/backend runtime imports so restored sidecars skip Python
  dependency work.
- Target-current sidecar metadata that skips restoring/copying the backend tree
  when `target\release\backend` already matches the current cache key and
  release resource flags.
- Rust audio sidecar hash cache that avoids recompiling when inputs are
  unchanged; the cache key is limited to the Rust audio sidecar dependency set,
  normalizes app-version-only Cargo metadata churn, and the normal Tauri Cargo
  target is used by default. GitHub release builds keep
  `build\rust-audio-sidecar-cache` in a separate Actions cache from the Python
  backend sidecar cache so Python/backend changes do not force an audio sidecar
  executable rebuild.
- Release workflow cache keys normalize app-version-only files before hashing
  dependency/build caches, so patch version bumps do not invalidate frontend,
  Rust, or backend scratch caches without real input changes. The main Rust
  release key still includes real Tauri shell inputs such as `tauri.conf.json`,
  capabilities, and icons.
- Frontend dependency reuse in GitHub release builds is two-layered: restore
  `Frontend\node_modules` first, and keep the npm package store warm through
  `actions/setup-node` keyed by the normalized
  `build\cache-keys\frontend-dependencies.txt` input.
- Python dependency reuse in GitHub release builds is layered: prebuilt backend
  sidecar first, `.venv`/wheelhouse next, and setup-python's pip cache only as
  a final download/build-store fallback keyed by the release requirements.
- `src/version.py` remains the leading app release version, but
  `Frontend\src-tauri\Cargo.toml` intentionally keeps a stable internal package
  version. `scripts\build_windows.ps1` writes a generated minimal Tauri release
  config overlay with the concrete app version and release-only overrides, and
  the Rust shell passes that value to the Python backend through
  `SCRIBER_VERSION`; do not restore per-release Cargo version churn.
- GitHub release builds set `CARGO_INCREMENTAL=1` and cache
  `Frontend\src-tauri\target\release\incremental` in the v2 Rust release cache.
- GitHub release builds intentionally keep `dtolnay/rust-toolchain@stable`.
  A 2026-07-09 experiment that used the Windows runner's preinstalled Rust
  saved the setup-action time but invalidated Cargo fingerprints: run
  `29003544425` spent `413.9s` in `build_windows.ps1`, `397.6s` in the Tauri
  bundle phase, and emitted `285` Cargo compile lines. Do not switch back to
  preinstalled Rust unless the Rust release cache is rebuilt for that exact
  toolchain and a follow-up hot run proves a net win.
- `Frontend\src-tauri\Cargo.toml` keeps the shell library crate type to
  `["rlib"]` for Windows desktop releases. Do not restore Tauri mobile
  `staticlib`/`cdylib` outputs unless mobile targets are introduced; they create
  extra release library artifacts that do not help the NSIS updater build.
- `v*` tag releases require Tauri updater signing by default. Use
  `SCRIBER_ALLOW_UNSIGNED_TAG_RELEASE=1` only for an intentional unsigned tag
  test build.
- Non-tag GitHub cache/warmup builds use `-NsisCompression none` by default to
  reduce packaging time and intentionally ignore `SCRIBER_NSIS_COMPRESSION`.
  Use `SCRIBER_NON_TAG_NSIS_COMPRESSION` only for explicit non-tag packaging
  experiments. Signed `v*` updater releases may use
  `SCRIBER_NSIS_COMPRESSION` after a measured size/time tradeoff.
- The 2026-07-09 hot cache measurement (`workflow_dispatch` run `28997179965`)
  proved the optimized heavy-cache path: `build_windows.ps1` took about
  `49.2s`, with backend sidecar, Rust build, Rust audio sidecar, FFmpeg Profile
  B, frontend dependencies, and Tauri bundler all restored as exact Actions
  cache hits. Once a run shows that shape, do not keep changing Python/npm,
  FFmpeg, PyInstaller, or Rust-audio cache logic without new
  `build-timing.json` and `release-artifact-summary.json` evidence. The next
  signed hot-tag measurement (`v0.4.21`, run `28999468872`) completed in about
  `3m57s` end-to-end with exact heavy cache hits. `build_windows.ps1` took
  about `137.5s`, dominated by `Tauri Windows bundle` at `122.0s`; the bundle
  log showed no crate downloads, one `scriber-desktop` compile at about `25s`,
  and about `90.2s` from `makensis` start to updater signature completion.
  The follow-up signed tag compression sweep measured `none` at `58.2s` /
  `189.3 MiB`, `zlib` at `72.4s` / `92.4 MiB`, and `bzip2` at `76.9s` /
  `90.3 MiB` versus Tauri default at `137.5s` / `74.4 MiB`. The current
  release default is `SCRIBER_NSIS_COMPRESSION=bzip2`, because it saves about
  one minute while adding about `15.9 MiB`; do not change dependency caches
  again unless a fresh artifact summary shows they regressed.
- Release workflow Actions caches are backed by internal GitHub release
  artifacts for the Python virtualenv, Python wheelhouse, backend sidecar cache,
  main Rust/Tauri build cache, Rust audio sidecar cache, and FFmpeg Profile B so
  sibling tag builds can reuse heavy outputs even when ref-scoped Actions caches
  miss. The main Rust/Tauri release artifact supports a latest-prefix fallback
  as a warm start when the exact key is absent. Normal tag releases must restore
  these large release-cache artifacts without repacking or clobbering them;
  refresh them on `main` cache-warming pushes or through the manual
  `release-windows.yml` `refresh_release_cache_artifacts=true` maintenance
  path. Heavy Actions caches are restore-only on tag releases; explicit
  `actions/cache/save` steps are allowed only on that refresh path.
- The release workflow's cache summary distinguishes exact Actions cache hits,
  ambiguous `restore-key-or-miss` Actions outputs, internal `release-artifact`
  fallbacks, and effective `miss` rows. GitHub reports both restore-key hits
  and true misses as `cache-hit=false`, so the workflow also reports short
  fingerprints for normalized files under `build\cache-keys` plus cheap path
  evidence. The same data is uploaded as `release-cache-summary.json` with the
  build artifacts. Combine it with `build-timing.json` sidecar metadata before
  concluding that equivalent input sets rebuilt across tag and main runs; the
  restore report is not by itself proof that PyInstaller or Rust audio sidecar
  work was skipped. The release workflow also uploads
  `release-artifact-summary.json`, which combines those inputs and includes an
  Oracle-ready timing brief plus diagnostic codes for common causes such as
  PyInstaller rebuilds, Rust audio rebuilds, effective cache misses, ambiguous
  Actions restore-key rows, and Tauri bundle dominance. It also captures
  `tauri-windows-bundle.log` plus `tauri-bundle-log-summary.json` so the
  residual Tauri bundle phase can be attributed to Cargo compile/download work
  or NSIS/updater/signing overhead before another cache change is proposed.
  The captured Tauri log is timestamped per line, and the summary reports
  milestone durations around `makensis` and updater signature completion. It
  emits recommendation codes that point to the next investigation path. While
  capturing that log, run `npm run tauri:build` through
  `cmd.exe /d /s /c "... 2>&1"` so Tauri/Node informational stderr is merged
  before PowerShell sees it. The release should fail from the native exit code
  rather than stderr presence.
- GitHub release artifact upload uses `compression-level: 0`; NSIS installers
  and updater metadata are already compressed or small, so recompressing them in
  `actions/upload-artifact` wastes runner CPU.
- Non-tag release workflow runs are cache/warmup evidence by default. They still
  build and validate the installer, but `scriber-windows-release` uploads only
  metadata, logs, timing, checksums, and cache summaries unless
  `SCRIBER_UPLOAD_FULL_NON_TAG_INSTALLER=1` is explicitly set. Signed `v*`
  releases must always upload the installer executable and sibling `.sig`.
- FFmpeg Profile B release builds restore from Actions cache first, then from
  the internal reusable GitHub release artifact `ffmpeg-profile-b-n7.0-v2`, and
  rebuild through MSYS2 only when restored Profile B tools are absent or fail
  validation.
- Profile B ffmpeg media tools, about `4.98 MiB` installed.

## Commands

Run from repository root unless stated.

```powershell
python -m pytest
```

```powershell
cd Frontend
npm run check
npm run build:webview
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
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke
```

Fast local staged app without NSIS:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalStagedApp `
  -SkipChecks `
  -SkipSmoke
```

Broader installed workflow smoke when provider credentials and network are
available:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke `
  -RunInstallerRealMediaWorkflowSmoke
```

Rust-promotion microphone matrix:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_microphone_hardware_matrix.ps1 `
  -RequireRustEndpointInventory `
  -RequireDeviceRefreshEvidence
```

Frontend browser smoke:

```powershell
python scripts\smoke_frontend_browser.py --output tmp\frontend-browser-smoke.json
```

Rust audio sidecar short physical smoke:

```powershell
python scripts\smoke_rust_audio_sidecar.py --mode wasapi --duration-sec 1 --output tmp\rust-audio-sidecar-smoke.json
```

Rust audio prewarm sidecar smoke:

```powershell
python scripts\smoke_rust_audio_prewarm_sidecar.py --duration-sec 1 --prebuffer-ms 400 --output tmp\rust-audio-prewarm-sidecar-smoke.json
```

Use `--mode wasapi` to exercise the real passive WASAPI prewarm worker:

```powershell
python scripts\smoke_rust_audio_prewarm_sidecar.py --mode wasapi --duration-sec 1 --prebuffer-ms 400 --output tmp\rust-audio-prewarm-sidecar-wasapi-smoke.json
```

Use `--prewarm-before-capture` on the sidecar capture smoke to prove buffered
prewarm frames are adopted into the next capture within one sidecar session:

```powershell
python scripts\smoke_rust_audio_sidecar.py --mode wasapi --duration-sec 1 --prebuffer-ms 400 --prewarm-before-capture --skip-selected-hash --output tmp\rust-audio-sidecar-adopt-wasapi-smoke.json
```

Rust audio app-level prewarm adoption smoke:

```powershell
python scripts\smoke_rust_audio_app_prewarm.py --mode wasapi --duration-sec 1 --prewarm-duration-sec 1 --capture-cycles 1 --prebuffer-ms 400 --output tmp\rust-audio-app-prewarm-wasapi-smoke.json
```

This verifies the Python `RustAudioPrewarmManager` plus
`RustPrototypeFrameSource` handoff against the real `scriber-audio-sidecar`.
By default it ignores user favorite microphones so release evidence exercises
the stable Windows default endpoint. Use `--honor-favorite-mic` only for a
targeted selected-device investigation.

The same lifecycle smoke can be included in the hybrid readiness runner when
explicitly needed:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunRustAudioPrewarmSidecarSmoke `
  -RequireRustAudioPrewarmSidecarSmoke
```

The app-level Rust prewarm adoption smoke can also be included:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunRustAudioAppPrewarmSmoke `
  -RequireRustAudioAppPrewarmSmoke
```

Long Always-On-Mic Rust prewarm evidence should require explicit durations:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunRustAudioAppPrewarmSmoke `
  -RequireRustAudioAppPrewarmSmoke `
  -RustAudioAppPrewarmDurationSec 600 `
  -RustAudioAppPrewarmPrewarmDurationSec 1800 `
  -RustAudioAppPrewarmCaptureCycles 2 `
  -MinRustAudioAppPrewarmDurationSec 600 `
  -MinRustAudioAppPrewarmPrewarmDurationSec 1800 `
  -MinRustAudioAppPrewarmCaptureCycles 2
```

These Rust smokes must not be used alone to promote Rust audio to default.
Longer physical Always-On-Mic matrix runs, device-change evidence, and
provider-backed transcription smokes are still required.

Provider-backed Rust recording hot-path evidence:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -RecordHotPathSamples `
  -RequireRecordingHotPathProviderTranscript `
  -RequireRecordingHotPathRustAudio `
  -RecordingHotPathSpeechPrompt "Scriber provider-backed Rust audio validation"
```

This requires real provider credentials, microphone access, and explicit Rust
audio prototype environment flags. It proves the STT provider emitted a final
transcript and the active recording diagnostics used `rust-wasapi` with the
`rust-frame-pipe` source. Promotion evidence must also prove adopted Rust
prewarm via `activeCapture.rustPrewarmAdoption` with a redacted prewarm hash;
on-demand Rust capture alone does not replace long physical matrix evidence.

Python-vs-Rust provider-backed comparison artifact:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_recording_hot_path_comparison.ps1 `
  -RustAlwaysOnMic `
  -RecordingHotPathIterations 3 `
  -RecordingHotPathSeconds 3 `
  -RecordingHotPathSpeechPrompt "Scriber provider-backed Rust audio validation"
```

Manual validator form for pre-existing reports:

```powershell
python scripts\validate_recording_hot_path_comparison.py `
  --python-report tmp\hybrid-baseline\python-recording-hot-path-baseline-recording-hot-path-1.json `
  --rust-report tmp\hybrid-baseline\rust-recording-hot-path-baseline-recording-hot-path-1.json `
  --output tmp\hybrid-baseline\recording-hot-path-python-rust-comparison.json
```

Final Rust promotion readiness can require that artifact with
`-RequireRecordingHotPathComparison` on `scripts\run_hybrid_release_readiness.ps1`.
Use `-RunRecordingHotPathComparison` on the aggregate runner when provider
credentials and the app under test are available; it invokes
`scripts\run_recording_hot_path_comparison.ps1 -RustAlwaysOnMic` before final
validation.
The comparison artifact must contain passing `rustAlwaysOnMic` and
`rustPrewarmAdoption` checks for Rust audio promotion.

Rust audio promotion readiness gate:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RequireRustAudioPromotionReadiness `
  -PlanOnly
```

Use this aggregate gate before any default Rust-audio promotion. It makes the
Rust sidecar smoke, app-level Always-On-Mic prewarm smoke, installed live
recording smoke, provider-backed Python-vs-Rust comparison, Rust endpoint
inventory, and native device-refresh evidence mandatory, and raises the
promotion minima to 10-minute active / 30-minute idle-prewarm evidence plus at
least two app-level prewarm/capture/stop/resume cycles. Each cycle must carry
its own pre-adoption and post-resume `audioPrewarmStatus` health snapshot; a
final healthy snapshot alone is not promotion evidence.
The installed live-recording report must also prove sampled
`rust-wasapi`/`rust-frame-pipe` active capture, adopted Rust prewarm
evidence through `activeCapture.rustPrewarmAdoption`, and a closed Rust
fallback circuit; generic Python live-mic stability is not enough for Rust
promotion.
Then add the matching `-Run...` or `-UseExisting...` flags to produce or reuse
the required reports.

When `-RustAudioSidecarPrewarmBeforeCapture` is active, the runner must pass
`--require-rust-audio-sidecar-prewarm-adoption` to the final validator. This
keeps old sidecar reports without adopted prewarm blocks from satisfying Rust
promotion evidence.

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
