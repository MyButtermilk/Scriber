# Scriber Agent Guide

Last verified: 2026-07-16

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

- Scriber is an AI transcription app for live microphone dictation, bot-free
  meeting capture, YouTube transcription, file transcription, transcript
  management, summaries, and PDF/DOCX export.
- Primary desktop runtime: Tauri 2 shell, React frontend, Python backend sidecar.
- Backend default: `127.0.0.1:8765`, implemented with `aiohttp`, WebSocket
  events, SQLite, Pipecat pipeline code, and provider adapters.
- Frontend default in dev: `localhost:5000`, implemented with Vite 8, React 19,
  TypeScript, Tailwind v4, Wouter, and TanStack Query.
- Runtime is Windows-first. Linux/macOS support is mostly fallback/dev support.
- Legacy Python tray/UI code was removed. The Tauri shell owns desktop UI,
  tray/menu actions, global hotkeys, and the recording overlay.
- The pre-created recording-overlay WebView must register its native event
  listener before completing `native_overlay_renderer_ready`; that handshake
  returns the authoritative current snapshot so a hotkey fired during lazy
  renderer startup cannot leave a visible but transparent popup.

## Repository Map

Backend and runtime:

- `src/web_api.py`: main aiohttp controller, routes, WebSocket server, settings,
  jobs, transcript history, mic control, uploads, logs, support bundles.
- `src/pipeline.py`: STT pipeline orchestration, provider factory, analyzer
  cache, mic resolution, async/direct transcription.
- `src/modulate_stt.py`: Modulate multilingual batch and streaming adapters.
  Both paths expose final transcript text only; they explicitly disable
  diarization, partials, emotion, accent, deepfake, and PII/PHI signals and
  discard provider utterance metadata at the boundary.
- `src/microphone.py`: live microphone capture boundary backed by the Rust
  WASAPI frame-pipe source, channel selection, RMS callback, stream lifecycle.
- `src/mic_prewarm.py`: Rust/WASAPI idle mic prewarm and rolling prebuffer.
- `src/device_monitor.py`: microphone hotplug monitor, native Windows endpoint
  callbacks, polling fallback, PortAudio refresh deferral.
- `src/audio_devices.py`: microphone normalization, compatibility filtering, and
  private PortAudio-to-native endpoint mapping with redacted endpoint hashes.
- `src/audio_file_input.py`, `src/youtube_download.py`, `src/runtime/media_tools.py`:
  ffmpeg/ffprobe resolution and media preparation. YouTube extraction uses the
  pinned yt-dlp/EJS/Deno runtime and validates downloaded audio before provider
  upload.
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

- `Frontend/client/src/App.tsx`: routes; the five primary user tabs are eager,
  while Debug Console, transcript detail, and not-found surfaces remain lazy.
- `Frontend/client/src/pages/`: Live Mic, Meetings, YouTube, File, Settings,
  Debug Console, Transcript Detail.
- `Frontend/client/src/contexts/WebSocketContext.tsx`: shared WebSocket.
- `Frontend/client/src/lib/backend.ts`: backend URL and Tauri token bridge.
- `Frontend/client/src/lib/api-types.ts`: shared REST-facing TS types.
- `Frontend/client/src/i18n/`: persistent `de`/`en` interface locale,
  translation catalogs, locale-aware formatting, and catalog completeness
  tests. Keep interface locale separate from STT/output language. Every
  user-facing literal passed to `t(...)` needs a German catalog entry, and the
  Tauri `set_ui_locale` bridge must keep native tray tooltips synchronized.
- `Frontend/client/src/components/transcription-history-toolbar.tsx`: shared
  count/search/list-grid toolbar for Live Mic, YouTube, and File history.
- `Frontend/client/src/index.css`: Tailwind v4 CSS-first design system. The six
  primary tabs share the `app-page-shell` 1320 px desktop frame and expose a
  stable `data-page-shell` hook; do not introduce per-tab maximum widths.
  Motion follows the shared transitions.dev Refine/Polish tokens in `:root`.
  Match duration and easing tokens by interaction type, keep frequent tab and
  keyboard navigation immediate, use faster closes than opens for transient
  surfaces, never add `transition: all`, gate hover-only transforms to fine
  pointers, and preserve the existing reduced-motion fallbacks.
- `Frontend/src-tauri/src/audio_sidecar.rs`: separate Rust audio sidecar with
  `--self-test`, `--stdio` JSON-lines protocol, a test-only
  `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1` frame-pipe transport harness, and
  default WASAPI capture/prewarm support. It is bundled once as Tauri's
  install-root sidecar executable and is the standard live-mic capture engine.
  Synthetic-capture tests may additionally set the absolute
  `SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH` to replay one
  bounded 48 kHz mono signed-16 PCM microphone fixture. That fixture plays once
  and then yields silence; it must not alter the default synthetic sine signal
  or any production WASAPI path.
  Meeting capture uses one sidecar process for 48 kHz microphone plus loopback,
  pinned AEC3 processing, and shared-timeline raw mic/system/clean mic pipes.
  The token-protected Meeting device test must reuse this path, remain explicit
  and local-only, return only bounded level/activity statistics, and always stop
  its ephemeral sidecar capture without persisting or uploading PCM.
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
- `scripts/build_windows.ps1`: Windows installer orchestration. Official GitHub
  releases use `-ParallelizeIndependentBuilds`: frontend typecheck, Python
  sidecar preparation, and the Tauri `--no-bundle` app compile overlap. That
  compile uses a generated overlay whose `bundle.resources` is JSON `[]`, so
  Tauri does not validate the not-yet-staged backend during compile; the final
  `tauri bundle` always waits for every producer and uses the original complete
  config, revalidating and packaging all resources. Rust audio uses the shared,
  restored Tauri Cargo target after backend preparation by default; Cargo's
  target lock bounds a rare overlap with the app compile and avoids a cold
  duplicate dependency build. `-RustAudioIsolatedTarget` remains an explicit
  diagnostic/local opt-in. NSIS, updater signing, and verification start only
  after every producer succeeds.
- `native/scriber-diarization-sidecar/`: isolated, statically linked
  Sherpa-ONNX worker; release preparation stages its attested EXE under backend
  `tools/diarization`. Its worker cache and pinned Sherpa archive cache remain
  separate from Tauri, audio-sidecar, and Python backend caches.
- `scripts/ffmpeg/build_profile_b_msys2.ps1`: Profile B custom ffmpeg build.
- `scripts/smoke_*.ps1` and `scripts/smoke_*.py`: installed app, desktop,
  frontend, media, and workflow gates.
- `scripts/run_hybrid_release_readiness.ps1 -RunReleaseBuild` may invoke
  `scripts/build_windows.ps1` as an evidence producer, but it still requires
  real updater signing secrets, HTTPS publication, and Authenticode signing
  evidence for final readiness.
- `scripts/run_meeting_release_matrix.ps1` creates non-passing operator drafts
  for the real Teams/Zoom/Meet, route, failure, Outlook, privacy, and soak
  matrix. `scripts/validate_meeting_release_matrix.py` accepts only completed
  `meeting-release-evidence-*.json` reports whose relative supporting artifacts
  exist and match their SHA-256. Final Meeting promotion must run hybrid
  readiness with `-RequireMeetingReleaseMatrix`; never treat generated drafts,
  partial validation, or unsigned validation as release evidence.
- Meeting validation areas stay atomic. Support-bundle privacy and automated
  regression evidence may be collected with
  `scripts/collect_meeting_support_bundle_evidence.py` and
  `scripts/collect_meeting_regression_evidence.py`; do not merge them with the
  held voiceprint corpus, EU legal/privacy approval, or signed-release profile.
  Collectors must emit redacted summaries, not raw logs, support ZIPs,
  transcripts, audio, personal paths, or credentials.

## Non-Negotiable Contracts

### Tauri Runtime

- Tauri is the primary desktop runtime.
- The Rust supervisor validates `/api/health` before attaching to a backend.
- Managed workers receive `SCRIBER_RUNTIME_MODE=tauri-supervised`,
  `SCRIBER_WEB_HOST`, `SCRIBER_WEB_PORT`, `SCRIBER_SESSION_TOKEN`,
  `SCRIBER_BACKEND_LAUNCH_KIND`, optional private shell IPC env
  `SCRIBER_SHELL_IPC_PIPE`, `SCRIBER_SHELL_IPC_TOKEN`,
  `SCRIBER_SHELL_IPC_API_VERSION`, and writable `SCRIBER_DATA_DIR`. Official
  builds also embed the public `SCRIBER_OUTLOOK_CLIENT_ID` in the Tauri binary
  and pass only a canonical non-nil GUID to the worker; source builds may use a
  valid process environment fallback. Tag releases must fail before expensive
  setup when that repository variable is missing or invalid, and cache
  fingerprints may contain only its presence flag and SHA-256, never the raw
  identifier.
- `/api/health` remains public. Token-protected endpoints must accept the
  session token via `scriberToken` query parameter or `X-Scriber-Token`.
- `POST /api/runtime/frontend-ready` is the proof that the actual WebView reached
  the runtime backend.
- The main WebView reports Long Tasks API support and only bounded monotonic
  timing records over 200 ms through token-protected
  `/api/runtime/frontend-performance`. Keep this event-driven and privacy
  minimal: no polling, entry names, URLs, DOM attribution, route data, or text.
  AutoResearch must compare one source instance and sequence-bounded windows;
  unsupported observers, retained-ring truncation, dropped entries, or a source
  change are `unknown`, never an invented zero. A measured zero additionally
  requires a post-interaction flush request whose source-bound heartbeat was
  observed by the WebView, reported after draining `PerformanceObserver`
  records, and acknowledged after the measurement window ended. Installed
  shell smoke Quit must wait for that bounded acknowledgement barrier.
- AutoResearch B7 hotkey timing starts at the actual Tauri global-shortcut
  callback. The shell emits its Windows-QPC marker only when
  `SCRIBER_TAURI_BENCHMARK_HOTKEY_RUN_ID` contains a non-nil UUID, attaches it
  to the exact Live Mic start/toggle request, and includes only run/sample UUIDs,
  the shell PID, and QPC integers. The managed backend must validate its direct
  parent PID, timestamp freshness, and the configured run before binding the
  marker to that session; Windows key dispatch remains diagnostic-only.
- Rust owns Windows autostart, global hotkey registration, single-instance
  startup, tray/menu shell actions, and worker crash recovery. The supervisor
  must keep the named single-instance restore event: a second launch exits
  before backend/audio side effects but signals the primary instance to show,
  unminimize, and focus its main window. A mutex-only early return is a UX
  regression for tray-hidden starts.
  must also recover a managed process that stays alive but fails `/api/health`:
  allow the bounded unhealthy window (`SCRIBER_BACKEND_UNHEALTHY_TIMEOUT_MS`,
  30 seconds by default), then use the authenticated graceful shutdown path
  before the existing hard-termination fallback and restart it.
- Keep Windows shell identity contrast-safe. The installed PE bundle icon
  (`Frontend/src-tauri/icons/icon.ico`), all normal/update/recording tray
  artwork, and the runtime main-window icon use the same white-disc feather at
  taskbar sizes. The canonical feather stays in
  `Frontend/client/public/favicon.svg`; regenerate the high-occupancy SVG
  master, native 16/24/32/48/64/128/256 px ICO frames, 256 px runtime window
  image, and 16/20/24/28/32/36/40/48 px normal tray RGBA variants with
  `venv\Scripts\python.exe scripts\generate_windows_app_icon.py`. Tray state
  changes add only a bounded blue update or red recording badge; regenerate
  their DPI-specific raw-RGBA variants and legacy 32 px preview PNGs afterwards with
  `venv\Scripts\python.exe scripts\generate_tray_state_icons.py`;
  the Tauri tray must select the native raster nearest the primary monitor's
  scale factor instead of making Explorer downsample one fixed 32 px HICON.
  `build.rs` must watch the ICO so incremental release builds cannot retain an
  older executable resource. Tauri/Tao's runtime `set_icon` currently updates
  only `WM_SETICON/ICON_SMALL`, so the main HWND must also receive explicit
  process-owned `ICON_BIG` and `ICON_SMALL` HICONs created from the native 256
  px and 32 px ICO frames. Keep those HICONs alive for the process lifetime;
  never destroy a handle while Windows may still query it. This does not
  change the in-WebView brand mark: it stays unboxed on light surfaces and uses
  the generated white-disc SVG only in dark mode.
  `Frontend/client/public/favicon-dark.svg` must remain byte-equal to
  `Frontend/src-tauri/icons/windows-app-icon.svg`; both are emitted by the
  Windows icon generator so the boot shell, app header, taskbar, and tray share
  one vector-backed identity.
- Closing the main window routes Scriber to the tray: intercept only the main
  window's close request, prevent destruction, and hide it. Tray and
  single-instance show actions must reveal that same WebView again; do not leave
  a headless tray process whose main window no longer exists. Explicit tray
  Quit and app exit still use the bounded graceful backend/audio cleanup path.
- Rust registers both live-mic shortcuts and the Meeting shortcut after the
  token-protected backend identity is ready. Fresh installs default to
  `Ctrl+Shift+D` for Live Mic, `Ctrl+Shift+F` for post-processing, and
  `Ctrl+Shift+M` for Meetings; persisted `.env`/Settings choices remain
  authoritative. Startup, authentication, Settings-read, and primary-shortcut
  failures remain retryable. An unavailable optional shortcut is a stable
  degraded state: keep every successfully registered shortcut active and wait
  for an explicit Settings refresh instead of tearing them all down on every
  supervisor tick. Registration, shortcut capture, and resume must share one
  serialized shell mutation lane. The normal hotkey
  must keep plain live dictation output. The post-processing hotkey must
  dispatch only to the dedicated live-mic post-processing endpoint and must
  not affect File or YouTube jobs. The Meeting hotkey must reveal, unminimize,
  focus, and navigate the existing main WebView to `/meetings` before its
  backend detection event is dispatched. Queue navigation by monotonic id until
  the WebView listener acknowledges it so an early startup hotkey cannot lose
  the route change.
- Python push-to-talk polling is replaceable lifecycle plumbing, not the owner
  of provider finalization. On key release it must schedule the controller's
  tracked background stop and shield that task from poller cancellation so
  hotkey re-registration or shutdown cannot strand `_is_stopping` or lose the
  transcript. Shutdown must continue to drain that tracked stop task.
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
  HGLOBAL-backed clipboard formats before setting transcript text. This includes
  application-registered formats in the Windows `0xC000..=0xFFFF` range (for
  example HTML/RTF and Chromium metadata), while handle formats such as
  `CF_BITMAP` or `CF_ENHMETAFILE` must never reach `GlobalSize`/`GlobalLock`.
  Restore the snapshot only if the clipboard sequence is unchanged; do not
  regress this to text-only clipboard preservation.
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
- Private shell IPC is a bounded multi-instance transport. Python uses
  OVERLAPPED named-pipe I/O with `CancelIoEx` followed by completion draining;
  never free an OVERLAPPED request or its buffers while cancellation is still
  pending. Rust workers must contain command panics and close every pipe through
  their ownership guard even on unwind. Narrow Python domain locks preserve
  ordering for audio, overlay,
  injection, and Outlook mutations without serializing unrelated commands.
  Rust owner-level mutation lanes remain authoritative for audio lifecycle and
  overlay state, including calls that bypass IPC such as the global hotkey. Do
  not restore a single process-wide transport lock or a single-instance server.
- Shell IPC response delivery must remain bounded. Do not add an unbounded
  `FlushFileBuffers`. Every complete response uses a request-ID- and
  API-version-bound `responseAck` before the pipe instance is reclaimed; a
  client disconnect is not an acknowledgement. If delivery of a successful
  capture, prewarm, or Meeting audio start cannot be confirmed, Rust must roll
  that start back. Missing acknowledgement for non-start commands remains a
  delivery diagnostic and must never acquire audio rollback semantics.
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
- Managed backend restart and Tauri exit must request the token-protected
  `/api/runtime/shutdown` path and allow the backend's bounded cleanup window
  before escalating to process termination. Do not regress this to an
  immediate kill that can lose debounced settings or pending transcript writes.
- Python owns recording state and provider work.
- Local `npm run tauri:dev` must build the current `scriber-audio-sidecar`
  before Vite starts. The Cargo package keeps
  `default-run = "scriber-desktop"`. This prevents a stale debug sidecar or
  Cargo's multiple-binary ambiguity from making the Meeting route test disagree
  with installed capture behavior.

### REST and WebSocket Contracts

- WebSocket events are versioned with `apiVersion`.
- Use builders and validators in `src/core/ws_contracts.py` when adding events.
- `/api/health`, `/api/runtime`, and frontend-ready payloads are versioned and
  validated through `src/core/rest_contracts.py`.
- Add or update contract tests when changing payload shape.
- Frontend REST consumers should use `Frontend/client/src/lib/api-types.ts`
  instead of ad hoc `any` boundaries.
- `/api/runtime/logs` may expose only a compact human-readable message plus
  bounded, allowlisted structured context. Recursively redact public metadata,
  omit identifiers, secrets, transcript/prompt content, and arbitrary extras,
  and keep the complete machine record in the local log file. The Debug Console
  keeps technical context and long legacy messages collapsed by default, shows
  named hot-path startup/finalization timings, and offers per-entry copyable
  redacted JSON rather than rendering dictionary dumps inline.
- Every STT execution must log one credential-free runtime configuration with
  workload, provider, exact effective model, mode, language, sample rate, and
  channel count; Soniox also includes the selected region. Never include API
  keys, authenticated URLs, request payloads, or transcript text. Keep full
  pairwise hot-path matrices in the latency metrics store rather than normal
  human-facing log messages.
- Soniox Realtime transcript insertion uses only finalized transcript frames.
  Soniox owns semantic endpoint detection; local VAD may support diagnostics
  but must not force provider turn endpoints or transcript commits. A Live Mic
  hotkey stop must send Soniox manual finalization before ending the websocket
  stream and must wait only for a final generation produced after that stop, not
  a stale earlier endpoint.
- Interactive Live Mic stop controls use the token-protected
  `POST /api/live-mic/stop-request` acknowledgement path. It must return a
  bounded `202` without awaiting provider finalization, remain idempotent for
  repeated stops, and never arm a deferred toggle. The existing state and
  WebSocket events remain completion authority; `/api/live-mic/stop` is kept
  only for compatibility callers that explicitly need a synchronous result.
- Meeting segments treat `startMs`, `endMs`, and `durationMs` as one contract.
  `durationMs` must equal `endMs - startMs` in REST and `meeting_segment`
  events; transcript and citation controls must preserve timestamp seeking.
- Meeting finalization/analysis progress is a nullable durable snapshot. Persist
  it before the matching WebSocket event, hydrate the exact Meeting cache/detail
  after navigation, and clear it at retry/terminal boundaries. `null` means an
  indeterminate active operation, never an invented zero percent.
- User-facing Meeting playback is mix-only. Transcript timestamps, citations,
  and Meeting speaker-identification samples must use the retained
  `playback_mix`; never expose per-track mute/source switches in these flows.
  Speaker samples are five to eight seconds, adding surrounding mix context for
  short utterances and disabling themselves when the retained recording is
  shorter than five seconds.
- Canonical File/YouTube/Meeting artifact begin, provider-stage, and commit/FTS
  phases must stay off the aiohttp event loop. Once a SQLite worker mutation has
  started, cancellation must observe it through its durable boundary; mutate
  the shared `TranscriptRecord` only after returning to the event-loop thread.
- File and YouTube attempt leases remain heartbeated continuously from attempt
  acquisition through source preparation, provider execution, optional local
  diarization, and canonical commit. Each renewal must reload the current
  attempt version because persisting `provider_result_ready` advances the CAS
  version while the same worker still owns the lease.
- Meeting and canonical transcript FTS5 projections use base-table `rowid` as
  their FTS `rowid`. Keep schema-versioned atomic rebuilds, rowid-based trigger
  deletes/parity checks, Meeting-scoped MATCH expressions, and the explicit
  single-snapshot transaction in `MeetingStore.detail`.
- Durable File/YouTube job claims and terminal transitions are SQL CAS updates.
  Preserve idempotent `queued/running/completed -> completed` reconciliation,
  but never allow a late completion to overwrite `canceled` or `failed`.
- Meeting exports must use `src/meeting_export.py` as the shared template
  boundary. Email headers must remain single-line and participant addresses
  validated/deduplicated; body-only drafts must not claim an attachment exists.
  Saved `.eml` drafts use SMTP CRLF line endings, an ASCII-safe
  quoted-printable UTF-8 body, plus `X-Unsent: 1`, and the
  selected PDF, DOCX, or Markdown attachment must remain a real MIME part when
  Outlook opens the draft. Export labels, email subject/body, and document
  headings follow conservative transcript-language evidence first, then the
  analysis `outputLanguage`, a concrete Meeting language, and finally the
  configured language/English fallback.
  In Tauri, exports use the native Save As dialog and an atomic file replace.
  Subsequent Open/Open Folder commands must resolve only the bounded,
  process-local opaque token returned by that save; never accept a frontend
  path for those commands. Compressed Meeting audio reuses the finalized
  64-kbit/s Opus playback mix; desktop saves stream it from the authenticated,
  allowlisted local Meeting endpoint into an atomic destination so five-hour
  files do not cross the WebView byte-array boundary. Browser builds keep the
  normal download fallback.

### Outlook Calendar and Participant Identity

- Keep exactly one reusable, unclaimed PKCE authorization flow per runtime.
  Repeated Connect actions may reopen that flow but must not create competing
  states that leave Settings stuck in authorization-pending after one browser
  callback succeeds.
- Outlook daily-event reads accept the browser-computed local-day start and next
  local-day start as UTC instants. This keeps DST boundaries correct using the
  WebView's system timezone; do not add a packaged Python `tzdata` dependency or
  reconstruct the requested local-day boundary from an IANA timezone inside the
  frozen backend.
- Microsoft Graph `calendarView/delta` requests must not use `$select`, which the
  delta API does not support. Validate every pagination URL, stage every page,
  and commit events plus the final delta cursor atomically so a failed or
  interrupted multi-page sync cannot expose a partial day or advance its cursor.
- Manual Outlook refresh uses a 70-second WebView request deadline in both
  Meetings and Settings. It must remain longer than the backend's bounded
  60-second Graph sync plus its final credential-backed status read; do not send
  this operation through the generic 30-second API deadline, which can report a
  false failure while the backend successfully commits the new delta cursor.
- `/me` is the account identity boundary. Preserve both `mail` and
  `userPrincipalName` as normalized aliases so the connected user is recognized
  even when an invitation uses the other address. Never expose access or refresh
  tokens in REST payloads, logs, or support bundles.
- A rejected refresh credential is a reauthorization state, not a connected
  state. Preserve previously synchronized events for offline context, expose a
  structured `reauthRequired` status, and require the user to reconnect before
  new Graph reads. Do not erase cached Meeting context merely because Microsoft
  rejected a token.
- Nearby-event suggestions prioritize an active non-all-day event, then the
  next upcoming event, then the most recently ended event; all-day entries are
  last-resort context. Do not return the earliest-starting row unconditionally.
- Outlook Disconnect succeeds only after the Credential Manager refresh token
  has been removed; it then clears the local Outlook account, delta state, and
  cached events. Existing Meetings keep their immutable event snapshots.
- Meeting start must distinguish an explicit `calendarEventId` from explicit
  `null`. Resolve an id only against the token-protected local Outlook cache and
  freeze the selected event, organizer, account identity, participants, and
  addresses into the Meeting. `null` means no calendar context and must not fall
  back to a nearby event.
- Post-Meeting identity suggestions are layered: unique local Voice Library
  matches first, then the microphone track's connected-account identity, then
  an optional LLM request only when the user asks for suggestions. The LLM may
  receive participant names, opaque ids, and short email-redacted transcript
  excerpts, but never Outlook email addresses. Treat the entire Meeting context
  sent to the model—including calendar names, speaker labels, and transcript
  text—as untrusted input, and require explicit human confirmation before
  persisting any proposed speaker-to-participant link.
- LLM speaker suggestions are ephemeral and may be paid provider results.
  Confirming one speaker must patch only that assignment in the client cache;
  it must not discard the remaining unconfirmed suggestions and force another
  provider request.
- Post-Meeting speaker naming must also accept a meeting-local free-text label
  for people, teams, rooms, or shared microphones. That label may update this
  Meeting's segments, but must not rename a Voice Library profile, create an
  Outlook identity, or become an email recipient. Meetings without calendar
  context still expose this naming flow. The Meeting UI may invoke the same
  explicit, permanent Voice Library merge used by Settings when two detected
  speaker profiles are one person; require a confirmation dialog and preserve
  manually assigned meeting-local labels while merging profile evidence.
- Canonical system segments without provider speaker evidence use one stable,
  meeting-local `Meeting audio` speaker so the transcript label opens the same
  explicit naming flow. Startup backfill may add that identity to legacy blank
  system rows, but must never overwrite a manually assigned Meeting name.
- Email/export recipients come exclusively from the Meeting's frozen calendar
  event, never from Voice matches, LLM output, or confirmed speaker mappings.
  Validate and deduplicate addresses and exclude the connected user (including
  aliases), resource/room attendees, and declined attendees.

### Microphone and Device Handling

- Keep PortAudio access guarded through the shared device guard lock.
- Do not enumerate or refresh PortAudio devices while an active stream is being
  torn down unless the existing guarded/deferred path handles it.
- `DeviceMonitor` should use native Windows endpoint events where available.
  With active native events, polling is only a sparse safety net; faster polling
  is fallback-only when native events are unavailable.
- Device refresh is recording-aware and can be deferred until idle.
- Persisted `SCRIBER_MIC_ALWAYS_ON=1` prewarm starts only after DeviceMonitor's
  initial PortAudio refresh callback has completed. Startup keeps a bounded
  fallback when initial device discovery cannot finish; do not reintroduce a
  prewarm/refresh race that leaves the Windows microphone indicator off until
  first recording. Preserve normal hotplug callback ordering so restored
  favorite-device selection is applied before idle prewarm resumes.
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
- Meeting device selection may expose native capture/render inventory only via
  token-protected payloads containing friendly labels and hashed IDs. Never
  expose raw capture or render IMMDevice endpoint IDs.
- Rust/WASAPI is the default and only live microphone capture path. The old
  Python `sounddevice` capture and Python idle-prewarm path have been removed;
  `sounddevice` may still be used for device listing and PortAudio-to-native
  endpoint mapping until those helper surfaces are fully native.
- `SCRIBER_AUDIO_ENGINE` is retained only as a backwards-compatible diagnostic
  input; it no longer selects Python capture. `SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1`
  may run the sidecar's synthetic frame-pipe transport harness for tests only.
  It remains silent unless test-only `SCRIBER_RUST_AUDIO_SYNTHETIC_SIGNAL=1` is
  also set; that mode emits deterministic system render, delayed microphone
  echo, and independent near-end speech-like tones so the installed Meeting
  device-test path can prove nonzero microphone/system/AEC-clean levels without
  user audio.
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
  adopts any prewarm session, including one whose initial rolling snapshot is
  empty, do not stop the idle `PrewarmSession` in the parent `captureStart`
  handler. `begin_handoff` atomically drains the rolling snapshot and redirects
  later blocks into a bounded tail; overflow must fail capture visibly. Before
  writing any adopted block, capture start must resolve the currently requested
  endpoint on a separate, single-flight COM worker while the prewarm audio worker keeps
  draining, then compare its actual endpoint hash with the endpoint already
  opened so a Windows default-device change cannot mix microphone A and B. When
  capture kind, sample format, block size, and actual endpoint still match,
  promote that same running `IAudioClient` in place:
  attach the new frame pipe, keep later blocks in the bounded handoff tail until
  the pipe connects, drain snapshot plus tail exactly once as PREBUFFER, then
  write live frames from the same client without stopping or activating another
  client. Promotion ownership uses one atomic
  `PENDING -> ACCEPTED/CANCELLED` transition before `begin_handoff`: a timeout
  that wins cancellation may use the existing overlap/snapshot fallback, while
  an accepted promotion must never silently start a second capture. The frame
  pipe handle remains RAII-owned across the command queue, nonblocking writes
  retry partial/zero/transient results only inside fixed deadlines, and the
  promoted stop path must neither call unbounded `FlushFileBuffers` nor wait
  indefinitely for its worker. The promoted `CaptureSession` owns the original
  prewarm worker so response-delivery/ACK rollback and `captureStop` both request
  stop and join only within the bounded cleanup window. A confirmed format or
  endpoint incompatibility starts the replacement client while the old client
  remains active, then stops the old client without replaying incompatible
  audio. Transient preflight or pipe-setup failures reuse the existing bounded
  snapshot/tail overlap fallback instead of first stopping prewarm. A timed-out
  resolver keeps the single-flight lease until its detached worker really exits,
  so repeated capture starts cannot accumulate COM threads. The overlap fallback
  must bound its old-worker join, finalize the tail only after that worker exited,
  fail closed on timeout, and close its frame pipe without `FlushFileBuffers`.
  Python must likewise
  keep adoption provisional until its frame reader has successfully processed
  the first non-prebuffer live frame. Prebuffer frames remain durable and are
  delivered downstream, but they must not fire `on_ready` or commit the prewarm
  handoff by themselves. Early failures must
  stop the deferred session with explicit reasons such as `captureStartFailed`
  or `captureWriterFinishedBeforePrewarmHandoff`. This keeps
  `SCRIBER_MIC_ALWAYS_ON=1` optimized for minimum hotkey latency and prevents a
  visible Windows microphone privacy-indicator off/on blink between idle
  prewarm and live capture. Resolve the PortAudio/native capture route once
  when idle prewarm starts and bind it to that exact prewarm ID plus the current
  microphone/favorite configuration. A warm hotkey may reuse this immutable
  requested route and must not repeat endpoint inventory or PortAudio
  compatibility probes; Rust's actual opened-endpoint comparison remains the
  fail-closed authority. Native device events and microphone/favorite Settings
  changes must invalidate the resolution cache and rebuild idle prewarm before
  the next hotkey. A capture without a valid leased route keeps the normal
  fresh-resolution path. The reverse stop handoff is equally overlap-first:
  a replacement prewarm must successfully call `IAudioClient.Start()` and
  report ready before Tauri drains the active capture sidecar. A failed prewarm
  start leaves capture running until normal cleanup; never close capture first
  and reopen idle prewarm afterwards. This ordering also applies to segmented
  STT providers: resume idle prewarm before stopping physical capture, then let
  provider finalization continue independently. Before Tauri starts that
  replacement, Python must mark the active frame pipe as a pending external
  handoff; confirm it only when prewarm start succeeds, and restore normal
  `pipeClosed` failure classification when it fails. This prevents the expected
  old-sidecar EOF from polluting mid-session failure diagnostics. A
  `SegmentedSTTRecordingGate` alone does not prove that a provider can finalize
  before EndFrame. Only a real `SegmentedSTTService` or a provider with the
  explicit `vad_flush_before_end` stop capability may enter pre-EndFrame
  finalization; realtime OpenAI, Deepgram, and ElevenLabs use the latter path.
  Completion is event-driven with no fixed settle sleep: capture the final
  generation before issuing the commit, continue immediately when a newer
  final arrives, and do not let a stale earlier final satisfy the last commit.
  A real `SegmentedSTTService` completes synchronously inside its awaited flush;
  only asynchronous VAD-commit providers use the short failure deadline.
  Terminal-buffered
  services such as Azure MAI, and Gladia's bounded custom EndFrame stop, must go
  directly through EndFrame finalization instead of entering the VAD-commit
  wait. Each Live Mic start must log both the persisted
  Silero setting and the actual analyzer attachment state so support logs can
  distinguish a synthetic protocol boundary from local VAD execution. When
  the first explicit Live Mic start
  encounters the lazily unloaded Pipecat runtime, first confirm a temporary
  Rust prewarm, then import Pipecat off the aiohttp event loop; do not submit
  both to the same executor concurrently because a one-worker executor can run
  the heavy import
  first. The prewarm retains up to six seconds of post-hotkey audio. This
  capture-first buffer is never started before provider validation and explicit
  user intent; construction failure/cancellation must retain it only under the
  normal bounded post-recording prewarm policy. Live Mic start owns an explicit
  generation: a second toggle, explicit stop, or shutdown cancels that exact
  generation after ownership-changing waits finish, releases the persistent
  audio claim, and prevents late provider activation. The Rust prewarm path is the app
  default. When no
  favorite/non-default mic is selected, keep the request as
  `devicePreference=default` with no `nativeEndpointIdHash`; the Rust sidecar
  must open the Windows default WASAPI capture endpoint directly so the visible
  microphone privacy indicator matches the active device. For selected or
  favorite microphones, the backend should prefer the private Tauri
  `audioEndpointInventory` shell IPC response for native endpoint inventory and
  use Python/PyCAW inventory only as fallback. That fallback must keep only
  active capture endpoints; PyCAW may omit flow metadata and enumerate stale
  render endpoints, so infer the local MMDevice flow from its private ID before
  hashing and never expose or return that raw ID. Active capture, prewarm, and
  passive Rust probe selection should all use the same Rust/Tauri endpoint hash
  when available. Non-default Rust capture without a native endpoint hash must
  fail before first frame; it must not silently use the Windows default endpoint
  or attach default-device metadata to a resolved favorite.
- The Rust audio frame-pipe protocol is length-prefixed and versioned. Keep the
  Rust and Python header fixtures in sync when changing it.
- Frame-pipe client connection waits must have a hard deadline. Live and
  synthetic capture pipes remain nonblocking after connection so a stalled
  Python reader cannot block the audio writer; Meeting relay output pipes may
  switch to blocking mode only after their bounded connection succeeds.
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
- Native WASAPI microphone-array downmixing selects the strongest RMS source
  channel with hysteresis instead of averaging all channels, because anti-phase
  array channels can cancel audible speech. Preserve that selection across an
  Always-On-Mic prewarm adoption. System loopback remains an all-channel
  average; do not apply microphone selection semantics to rendered audio.

### Providers and Media

- Standard backend builds pin `pipecat-ai[silero]==1.5.0`. Provider factories
  must use the Pipecat 1.5 `Settings` API; do not restore deprecated
  `InputParams`, `LiveOptions`, or pre-1.5 fallback paths. Synthetic VAD turn
  boundaries must use `VADUserStartedSpeakingFrame` and
  `VADUserStoppedSpeakingFrame`, which are the frames consumed by Pipecat 1.5
  streaming and segmented STT services. The frozen runtime import gate must
  reject a sidecar containing any other Pipecat version. Every direct
  `pipecat.*` import under `src` must also appear as that exact module in the
  frozen runtime contract; the AST parity gate prevents a nearby bundled module
  from masking a missing import such as `pipecat.transports.base_input`. Custom
  STT services must initialize complete `STTSettings` with at least `model` and `language`,
  and must consume `STTUpdateSettingsFrame.delta`; do not restore legacy
  `frame.settings` dictionaries or constructors that leave Pipecat settings as
  `NOT_GIVEN`.
- Do not add Pipecat's `local-smart-turn` extra to the standard sidecar: in
  Pipecat 1.5 it pulls Torch, Torchaudio, and Transformers. SmartTurn remains
  optional; import `LocalSmartTurnAnalyzerV3` directly and do not couple it to
  the removed `pipecat.processors.user_idle_processor` module. Pipecat 1.5
  `TransportParams` does not own VAD or turn analyzers: eligible segmented and
  async live pipelines must wire an explicit `VADProcessor`. Native realtime
  providers, including Soniox, do not attach local SmartTurn. Analyzer instances
  are session-owned; a startup-warmed
  instance may be claimed once but must never return to a global cache after
  processor cleanup. When model prewarming is enabled, replenish empty warmup
  slots in the background only after the prior pipeline and capture have torn
  down, using newly constructed instances. The standard
  recording path uses bundled Silero VAD and ONNX runtimes without those
  heavyweight dependencies.
- Meeting Smart Turn is an optional microphone-preview boundary refinement. It
  may merge provider-final tokens while the local analyzer reports an
  incomplete phrase, but must not gate durable audio, system-audio preview, or
  canonical finalization. Failure must fall back to provider endpointing.
- Meeting transcription mode is a persisted per-meeting contract with exactly
  `live_final` and `final_only`. Settings owns the choice; the Meeting start and
  import surfaces may summarize it but must not expose a second provider/model
  selector. `final_only` must skip every live-preview start/restart path while
  preserving native capture, 30-second checkpoints, final transcription,
  diarization, analysis, and recovery. Imports are always `final_only`.
- Provider-cost copy is an estimate, not billing authority. Captured Meetings
  assume separate microphone and system tracks; imported Meeting audio assumes
  one track. Add provider diarization cost only to the system/import track on
  which it is requested, and keep pricing sources plus the checked date visible.
- Soniox Async defaults to `stt-async-v5`. Keep
  `SCRIBER_SONIOX_ASYNC_MODEL` as an override for temporary compatibility, but
  do not restore `stt-async-v4` as the code default. Direct Soniox async upload
  prefers WebM/Opus, which the provider accepts; WAV is only a local encoding
  fallback when bundled ffmpeg cannot produce WebM, not an API-rejection retry.
- Soniox realtime live transcription defaults to `stt-rt-v5`. Keep
  `SCRIBER_SONIOX_RT_MODEL` as an override for temporary compatibility, but do
  not restore `stt-rt-v4` as the code default.
- Soniox data residency defaults to `SCRIBER_SONIOX_REGION=us`. Settings may
  select only `us` or `eu`; persist the choice independently of the API key.
  Every Soniox boundary in one session must resolve from that same choice:
  Pipecat realtime, dual-stream Meeting preview, buffered async, and direct
  file/YouTube/Meeting finalization. EU uses `api.eu.soniox.com` and
  `stt-rt.eu.soniox.com`; US uses the unqualified Soniox domains. Never infer a
  region from an API key, silently fail over across regions, or log credentials.
  The API-key popup must explain that Soniox first enables regional deployment
  for the organization and that the user must create an EU project and use its
  region-specific key; keep the official docs and `support@soniox.com` links.
- Meeting Soniox realtime uses two independent supervised streams. Provider
  preview is best-effort: start Native Capture plus the durable recorder before
  connecting live STT on initial start, every resume, and default-device
  recovery. Missing credentials, provider initialization failures, and later
  disconnects must leave capture in `recording` with a visible degraded preview,
  not fail or backpressure the recorder. Retain bounded preview queues, one
  `live_stt_reconnect` gap per outage, bounded exponential reconnect,
  shared-timeline timestamp rebasing, and versioned `meeting_live_status`
  reconnect/recovery/degraded events. Report the first preview-queue overflow
  immediately; durable recorder loss must remain zero. Meeting realtime
  requests Soniox speaker diarization for the system-audio stream only; the
  microphone stream remains the local `You` track. Preserve every contiguous
  system-speaker token run as its own timestamped final live segment, normalize
  raw provider speaker ids by first appearance, and scope raw ids to one
  WebSocket connection so reconnect reuse cannot silently merge people.
- Live microphone transcription must not request or format provider speaker
  diarization. Keep `enable_speaker_diarization=False` for live pipelines so
  single-speaker dictation inserts plain text. File and YouTube jobs may enable
  diarization where the provider adapter has stable anonymous speaker output.
- Live Mic may broadcast `InterimTranscriptionFrame` text as replaceable UI
  preview, but it must never append, persist, or inject that text. Only provider
  `TranscriptionFrame` finals enter the transcript and text injector; this also
  applies to ElevenLabs manual-commit realtime transcription.
- Keep Silero/Pipecat VAD opt-in through `SCRIBER_SEGMENT_SPEECH_WITH_VAD` and
  the Settings toggle only for segmented/async Live Mic routes. A
  provider-native realtime route must never construct, attach, or replenish a
  Silero analyzer, even when the persisted preference is enabled; Settings must
  show that effective switch as off and disabled, and selecting that route must
  discard an unused analyzer warmed for the previous provider. HTTP-style
  providers use one synthetic recording-wide segment flushed on stop when
  Silero is off. Saving the disabled setting must not import the heavy pipeline:
  discard an already-loaded analyzer immediately or record one deferred discard
  for the in-flight/next lazy import. Cleanup failure is diagnostic-only and
  must never roll back the persisted setting. When enabled, VAD may split
  eligible HTTP-style live STT at pauses. Live Soniox uses its native semantic
  endpointing without local SmartTurn; Meeting Smart Turn remains a separate
  preview feature. In Settings, classify only original-provider native streams
  as cloud realtime. Keep segmented uploads such as Scriber's current Mistral
  route and Groq in the cloud async/segmented/batch group, sorted there by the
  displayed error rate.
- Settings obtains exact effective provider model names from the backend and
  shows `Model Name` under the current provider. Do not invent a model slug for
  APIs without a model selector; label that case as the provider default.
- New File and YouTube summaries are stored as safe semantic HTML. The editable
  content prompt is always followed by Scriber's mandatory HTML output
  contract, which owns the editorial hierarchy while the frontend owns all
  typography, spacing, color, and interaction. Keep model output free of CSS,
  classes, ids, scripts, arbitrary attributes, and document wrappers; normalize
  and sanitize before persisting `summaryFormat=html`, then sanitize again in
  the WebView. Model links are reduced to plain text so untrusted transcript
  content cannot navigate the main WebView. A completed HTML summary requires a
  non-empty first `section` with `h2` title and `p` standfirst; reject empty or
  unstructured sanitized output. Legacy summaries remain Markdown. The first
  release carrying the HTML-summary prompt contract must replace every prompt
  stored by an older installation exactly once and persist a versioned marker
  in `settings.json`. Once that marker exists, later user prompt edits remain
  authoritative and must never be reset by startup. Persist this migration
  through the JSON-only path so it cannot rewrite `.env`.
- Modulate realtime follows the provider's binary-audio plus empty-text EOS
  protocol and emits finalized `utterance` text only. Do not enable aiohttp's
  client heartbeat: its half-interval PONG deadline can terminate a valid
  finalization as close code 1006, while the explicit 30-second final-response
  timeout already bounds shutdown. The outer Modulate pipeline stop must remain
  longer than that provider timeout so its ErrorFrame and cleanup complete.
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
  `:nitro`. Provider error logging in these direct summary paths must never use
  diagnostic tracebacks while API keys, authenticated headers, prompts, or
  payloads remain in local scope; log only a bounded provider/stage and exception
  type. Google Cloud STT uses
  `google-cloud-speech` plus Pipecat's required `google-genai` namespace
  dependency, OpenAI live STT uses Pipecat's OpenAI Realtime STT service with
  `gpt-realtime-whisper`, while `openai_async` uses the direct OpenAI Audio
  Transcriptions HTTP adapter with `gpt-4o-mini-transcribe-2025-12-15`. Keep
  the explicit `openai` SDK and `websockets` dependencies for these paths.
  Groq STT uses Pipecat's `groq` SDK dependency, and Pipecat provider imports
  require `nltk` at runtime. Gladia live transcription uses
  Pipecat's Gladia service; Gladia file and YouTube transcription use the
  direct pre-recorded HTTP upload/polling API and should not be routed through
  the live WebSocket pipeline. The direct async adapters
  `deepgram_async`, `gladia_async`, `openai_async`, `gemini_stt`, and
  `speechmatics_async` live in `src/cloud_async_stt.py`; keep them as direct
  HTTP/batch adapters unless a measured provider SDK change justifies adding
  more packaged dependencies. Do not add `speechmatics-batch` to the standard
  sidecar while the direct Speechmatics batch API path is sufficient. Keep
  `onnx-asr[cpu,hub]` in the standard sidecar for the ONNX local-ASR path.
  NeMo/Torch is not exposed as a local provider in Settings.
  Local ONNX file transcription uses the buffered Pipecat service with bounded
  30-second flush chunks. Pipecat 1.5 no longer carries the legacy transport
  `vad_analyzer` parameter, so file transcription must not depend on transport
  VAD frames to trigger ONNX recognition. Long files must flush completed
  chunks instead of dropping earlier PCM at the live-recording buffer limit.
  Primeline Parakeet support uses ready Hugging Face artifacts only; Scriber
  must not quantize this model on end-user machines. Preserve `fp32` through
  `geier/deskscribe-parakeet-primeline-onnx` with the manifest/checksum-backed
  archive flow: download ZIP + manifest + sha256 file, verify the archive
  SHA-256, extract the required ONNX Runtime files into the local model cache,
  and load the extracted directory through `onnx-asr`. Preserve `int8` through
  the trusted `Buttermilk03/parakeet-primeline-onnx` repo and its ready
  `encoder-model-int8.onnx` / `decoder_joint-model-int8.onnx` files.
- FFmpeg Profile B is the standard Windows bundled media-tool path. Gyan
  Essentials is explicit fallback only. Profile B must include both the WAV
  demuxer and WAV muxer: local ONNX ASR and the optional Sherpa speaker
  post-process normalize WebM/other media into mono 16 kHz PCM WAV.
- Keep ffmpeg and ffprobe bundled in the standard installer. `-SkipBundledFfprobe`
  is an experiment, not the release default.
- YouTube transcription prefers manual subtitles and then automatic captions by
  default. Keep `youtubePreferCaptions` persistent in writable runtime settings,
  fall back to provider audio transcription when captions are unavailable, and
  do not expose the preference as an inline control on the YouTube page.
- Standard YouTube builds pin `yt-dlp[default,deno]==2026.7.4` with matching
  `yt-dlp-ejs==0.8.0` and bundled Deno `2.9.2`. Let current yt-dlp defaults
  select YouTube player clients; do not restore the stale forced `android,web`
  client pair. Every returned download must pass ffprobe container and audio
  stream validation before transcription. Keep malformed/incomplete downloads
  retryable and never report them as download success.
- Keep PySide6, customtkinter, and Tk overlay fallbacks out of the standard
  sidecar. Installed recording overlay rendering is owned by Tauri/Rust.

### Data and Diagnostics

- Runtime data belongs under `SCRIBER_DATA_DIR`, not the install directory.
- Legacy runtime data migration must not overwrite existing app-data files.
- Explicit Voice Library enrollment must use the existing private
  `audioCaptureStart`/`audioCaptureStop` Rust/WASAPI path under the shared
  native-audio admission lease. Capture mono 16 kHz PCM into a short bounded
  in-memory buffer only, reject unreadable, short, quiet, or clipped samples,
  require clear speech activity across at least two enrollment windows, and
  clear the buffer after local WeSpeaker inference on every success, failure,
  and cancellation path. The pinned ONNX export has fixed batch size one:
  infer each accepted window as waveform `[1, 160000]` plus mask `[1, 589]`,
  normalize each result, and persist only their normalized centroid. Validate
  the native start response as mono 16 kHz
  `pcm_i16_le` before reading its frame pipe. Never use WebView `MediaRecorder`, a Python
  capture fallback, a hidden Meeting, or a temporary enrollment audio file.
  Persist only one normalized aggregate enrollment centroid plus count,
  effective-weight, resultant-norm, and time metadata on the local speaker
  profile; reconstruct its weighted sum only in memory for exact incremental
  and merge math. Never persist individual enrollment samples. Profile
  recomputation, Meeting deletion, merge, and split
  must preserve that explicit seed correctly. Whole-library deletion and
  Meeting-finalizer registration share a durable SQLite enabled gate under
  `BEGIN IMMEDIATE`, so a late finalizer cannot recreate deleted voice data;
  model downloads stay in unique verified staging files and may be promoted
  only while that gate remains enabled, with a post-promotion recheck for
  cross-process deletion;
  public REST responses,
  exports, logs, diagnostics, and support bundles must not expose PCM,
  embeddings, raw endpoint IDs, frame-pipe names, or local paths.
- Meeting capture rotates every source into 30-second WAV chunks. Publishing a
  completed chunk must keep its `meeting_audio_chunks` row and the corresponding
  checksum-protected `meeting_transcript_checkpoints` snapshot in one SQLite
  transaction. Startup recovery may restore missing final live segments from
  the newest valid snapshot, but must not overwrite rows that survived the
  interruption or trust a snapshot whose SHA-256/count validation fails.
- Meeting transcript checkpoints use schema v3. Every twentieth 30-second
  sequence is a compact full base; intervening checkpoints are deltas with
  per-source segment frontiers. Keep a redundant prior-base fallback so one
  corrupt newest base does not discard the following durable deltas, and prune
  superseded payload bodies to bounded tombstones while retaining their metadata
  rows. Do not restore cumulative full-transcript payloads on every chunk; their
  storage grows quadratically over long Meetings.
- The filesystem side uses a recoverable two-phase commit: close/fsync/hash the
  deterministic `.partial.wav`, persist a `prepared` row, atomically rename,
  then mark `complete` together with its transcript checkpoint. Startup must
  reconcile verified prepared partial/final combinations. Never create a
  rowless final WAV in the new path; adopt legacy rowless finals only when
  sequence, WAV shape, digest, and start offset are unambiguous. Checkpoints
  carry per-source durable frontiers and may include a live segment only through
  its own source's committed frontier.
- Arm Meeting readers for an expected disconnect before asking native capture
  to pause, stop, reconnect, or clean up. Windows named pipes may close with
  `OSError` instead of an end-of-stream frame; that intentional boundary must
  commit a valid partial WAV before resume, while a failed native command must
  disarm it. Never let resume reuse a deterministic sequence still occupied by
  the preceding pause's partial file. Unexpected reader/storage failures remain
  watchdog failures and must not be reclassified as intentional stops.
- Startup recovery must preserve workflow phase: only `starting`, `recording`,
  and `paused` become resumable `interrupted` capture. `stopping` and
  `finalizing` become `finalization_failed`, and `analyzing` becomes
  `analysis_failed`; never offer capture resume for a post-capture crash.
- Keep audio format layers separate. AEC3, Silero, Smart Turn, live STT, and
  checkpoint capture use PCM. The verified long-lived archive is lossless
  Matroska/FLAC; timeline-aligned mix, clean-microphone, and system Opus files
  are playback derivatives, not canonical inference input. Persist and validate
  an explicit source-to-stream manifest for multistream Matroska; its streams
  are separate mono tracks and must never be described as one 2/3-channel
  stream. System-only Meeting imports and mic-only capture remain valid archive
  cases. Purge redundant WAVs only after canonical commit plus archive and all
  required playback-derivative verification, using durable asset states. Every
  lossless track manifest includes sample count and canonical decoded-PCM hash;
  decode the archive track and prove both equal before marking it purge-safe.
  An archive file hash plus ffprobe metadata alone is insufficient. Bound
  finalization peak disk use by releasing each temporary PCM track before the
  next is materialized. Retry/local ONNX may decode a required archive track to
  a job-scoped temporary WAV. Do not make direct chunked WebM/Opus the default
  without pre-skip/end-trim timeline tests and multilingual STT/speaker quality
  evidence.
- Provider upload encoding is independent of archive encoding. Meeting, File,
  and YouTube must share the frozen route's transport preparation. Soniox async
  may use a task-scoped WebM/Opus derivative for efficient upload, but that file
  is deleted after provider release and never becomes canonical local evidence.
- Successful local Meeting diarization is persisted separately from its provider
  track result. Keep `transcription_track_derivations` immutable and bound to
  the parent result digest plus frozen route/worker manifest; recovery must reuse
  it without rerunning ONNX, and canonical inputs must include its provenance.
- Support bundles must redact API keys, session tokens, bearer tokens, and known
  secret patterns.
- Providers without native batch diarization use the optional, checksum-pinned
  Sherpa-ONNX 1.13.3 component after STT when
  `SPEAKER_DIARIZATION_FALLBACK_ENABLED` is active. Keep this one post-process
  shared across File, YouTube audio, Meeting finalization, and Meeting file
  imports. Align Sherpa turns to provider word timestamps when present and skip
  it only when the active response parser produced real native speaker
  intervals. This is optional post-processing: media/model/worker failures must
  preserve the already completed provider transcript and must not mark the File
  or YouTube job failed. The model/license component is an explicit post-install download
  under `SCRIBER_DATA_DIR`; do not add PyTorch, Torchaudio, TorchCodec,
  Lightning, or Pyannote's Python package to the base sidecar.
- Provider timing/diarization capabilities are executable contracts, not
  marketing metadata. Mark a provider as timestamp-capable only when the active
  request asks for that response shape, `provider_transcript.py` normalizes it,
  and a fixture test proves units plus speaker-zero handling. Canonical
  anonymous provider and Sherpa speakers are numbered by chronological first
  appearance. Persist whether alignment is exact-word, provider-segment, or
  estimated; never present proportional plain-text distribution as exact.
- Provider capability flags are preflight hints only. Post-response routing
  uses normalized evidence bound to provider, exact model, requested response
  shape, and parser version. Native diarization is proven only by successfully
  parsed speaker-labelled intervals; a registry boolean must not suppress local
  fallback when that evidence is absent.
- File, YouTube audio/captions, captured Meetings, and imported Meetings must
  converge on one transcript-artifact pipeline: frozen route plan and route
  snapshot, durable normalized stage result, optional local diarization,
  immutable canonical artifact, then UI/summary/export/legacy projections.
  `transcripts.content` is compatibility output, never a second canonical truth.
  Each canonical segment has stable identity, integer-millisecond start/end,
  timing and speaker origin, and alignment quality. New citations bind both
  artifact id and stable segment id.
- Freeze provider, exact model, response shape, parser id/version, language,
  timestamp/diarization request, and redacted request options for every attempt.
  Route snapshots may persist a custom-vocabulary SHA-256/presence/count but
  never its plaintext, API keys, bearer material, or signed URLs. Before the
  first provider call, a vocabulary digest mismatch creates a new route/attempt
  rather than mutating the old request; recovery from a durable stage result no
  longer requires the vocabulary value.
  Persist the validated normalized provider/caption result before optional local
  diarization. Recovery after that checkpoint must not repeat the cloud call.
  Meeting finalization persists microphone/system results independently in
  `transcription_track_stage_results`; retry only missing tracks, then aggregate
  them into the attempt StageResult and canonical artifact. Project canonical
  artifact segment ids unchanged into `meeting_segments`. A genuinely silent
  individual canonical track contributes no fabricated segment and does not
  fail finalization when another track contains usable speech. Fail when every
  available canonical track is empty, when provider work raises, or when text
  cannot be normalized into usable segments; preserve the surviving track's
  source and Meeting-clock timing unchanged.
  Attempt transitions and canonical-head replacement use compare-and-swap state
  versions; stale attempts become `superseded` and cannot overwrite newer work.
- Stable fallback segment ids hash transcript id, source track, start/end,
  canonical speaker key, and NFKC/whitespace-normalized text. Do not include the
  artifact version. Parse timed YouTube JSON3/VTT cues as provider-segment
  evidence; captions without valid times fall back to audio and captions never
  prove audio speakers.
- File/YouTube source audio is initially `processing_only`. Delete it only after
  all task owners have released it, through a durable
  `purge_pending -> purged` transition, and retain a non-sensitive asset
  tombstone. Public transcript metadata must never expose absolute local paths.
- Meeting live-STT timestamps use the sent-audio-to-Meeting-clock span map.
  Backpressure may discard live preview frames while durable capture continues;
  provider time is then discontinuous relative to the Meeting clock. Keep span
  coalescing and boundary-aware mapping, and do not restore a single connection
  offset for token timestamps.
- Stopping a Meeting live-STT preview uses one bounded deadline. Enqueue the
  stop sentinel without awaiting queue capacity; if the best-effort preview
  queue is full, discard at most one preview frame, report the gap, and reserve
  time to cancel provider tasks and close the WebSocket. Durable Meeting audio
  is upstream and must never be discarded or blocked by preview shutdown.
- Process-local Meeting task maps are not durable ownership. Finalization,
  analysis, imports, and canonicalization use persisted attempt id, state
  version, lease owner/expiry, and CAS. A losing attempt exits `superseded` and
  never marks another owner failed. Reserve the local task slot before durable
  state transition; cancellation must roll back or transfer to that reserved
  task. Identity-check done callbacks before removing map entries.
- Meeting finalization uses a 30-minute artifact-attempt lease renewed every
  five minutes with bounded retries and cancellation-safe cleanup. Pass the
  verified Meeting duration into the frozen provider route so upload, batch,
  and poll timeouts scale for inputs up to 18,000 seconds while retaining hard
  caps; do not regress to short fixed timeouts that expire during a valid
  five-hour finalization. Five-hour readiness is also provider-route-specific:
  expose it only when `supports_five_hour_meeting()` is true. The currently
  bounded routes are Soniox/Soniox Async (task-scoped WebM/Opus), AssemblyAI
  (2.2-GB upload boundary), Azure MAI (mono 64-kbit/s MP3), and Local ONNX (no
  cloud upload). Soniox's fixed async and realtime duration ceiling is exactly
  300 minutes, so do not imply support beyond the 18,000-second target. Gladia
  pre-recorded is capped at 135 minutes. Voxtral Mini Transcribe 2 (`2602`) is
  capped at three hours; retain the conservative 30-minute ceiling for the
  older `2507` or unknown Mistral override. Deepgram accepts large files, but
  Scriber's current synchronous `/v1/listen` request is not a verified
  five-hour route until long inputs are safely chunked/merged or an asynchronous
  transport is implemented. Do not show a green five-hour state for whole-track
  routes whose size, duration, and processing-window boundaries have not been
  proven.
  Reject an imported or finalized track above a known hard duration before the
  provider call, and keep the live UI's final-30-minute limit warning wired to
  the same central capability.
- Live Mic, Meeting start/resume, and Meeting device tests share one admission
  lock plus persisted singleton audio claim. Claim before prewarm/device awaits
  and recheck Meeting state under the lock. The persisted claim uses opaque ids,
  a 60-second expiry, a 15-second heartbeat, and CAS-safe transfer/release.
  Paused Meetings retain ownership; stop, terminal failure, watchdog failure,
  and graceful shutdown release it. Do not let startup steal a still-valid
  claim from another controller, and do not rely on `_is_listening` alone. A
  heartbeat that races the pending-to-durable Meeting transfer must adopt the
  newer same-controller generation. Foreign supersession must fail closed by
  stopping Live Mic or routing the Meeting through its capture watchdog. Live
  Mic must win the persisted claim before constructing its pipeline, and stop
  must release the claim before clearing `_is_stopping`; otherwise a queued
  toggle can leave a never-started pipeline behind.
- Persisted attempt route values are authoritative for language and exact model.
  Batch providers must not read mutable `Config.LANGUAGE` or model defaults for
  queued/retried/recovered work once a RouteSnapshot exists.
  A recoverable Meeting attempt may be resumed only when workload, source track,
  provider, model, and language all match the Meeting's frozen route. A failed
  full-reprocess provider switch must update or roll back `final_provider` and
  `reprocessFinalModel` together, and duration admission must use that frozen
  model rather than a newer Settings value.
- Analysis output and derived automatic action items commit as one generation.
  Remove absent unmodified rows on regeneration; preserve user-modified rows
  only with explicit carried-user provenance. Automatic action ids must remain
  stable across reordered model output by hashing normalized semantic content
  plus citations; semantic/citation matching must retain user text, owner,
  status, and due date without duplicating the regenerated item.
- Meeting analysis keeps the single-call fast path only at or below 48,000
  prompt characters and 60 minutes. Longer transcripts map stable chunks of at
  most 30,000 characters and 30 minutes with concurrency two, then use a
  deterministic hierarchical reduce with fan-in three. Persist map/reduce cache
  entries by algorithm/schema/model/chunk digest, repair only the malformed
  unit, and preserve exact segment citations and timestamp-derived chapter
  boundaries. When equal chapters from different map chunks are deduplicated,
  recompute their start/end from every merged citation before exposing playback
  links.
- The release local diarization implementation is a separate statically linked
  Rust worker. Do not link Sherpa into the live audio sidecar or Tauri shell.
  Ship the worker executable as a versioned resource of the signed Scriber
  installer/updater under backend `tools/diarization`, beside its generated
  build attestation; only both models and their licenses are an optional,
  manifest/hash-verified download. Frozen Python accepts only that allowlisted
  path. Never introduce a second remote executable download channel. Model
  hashing/status work must not block the aiohttp event loop.
- The pinned ERes2Net model provenance declares roughly 10,000 speakers of
  16-kHz Chinese training audio. Do not infer multilingual quality from this
  metadata. Release promotion needs held German, English, mixed-language,
  accent, pitch-range, and overlap evidence; this is a quality gate, not legal
  advice.
- Keep the worker's two-hour/1-GiB limits as hard defense-in-depth ceilings,
  not normal product eligibility. Local fallback is release-routed only through
  60 minutes until the multilingual long-file matrix proves a higher bound.
  An explicit expected speaker count may be passed to the worker; never derive
  it automatically from Outlook attendance, and do not expose clustering
  threshold as a normal user setting.
- Meeting recording import is a Meeting workspace entry point, not a File-job
  alias. Preserve both the sanitized original upload and normalized durable
  system track, record their relative metadata, show the selected final STT and
  diarization route before upload, and clean up both the Meeting row and folder
  on pre-finalization failure. Keep upload cancellation and progress visible;
  accepted imports must use the normal Meeting finalizer and retry states.
  Persist `committing` with a preallocated Meeting ID before creating the
  Meeting row or moving files; recovery must reuse that ID. Import cancellation
  is allowed only through `waiting_for_workspace`. From `committing` onward the
  Meeting workspace owns the artifacts, DELETE returns `409` plus `meetingId`,
  and discard must reject while a finalizer/analysis task still owns files.
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
- Meeting detail assembly validates existence once and reuses its SQLite
  connection for related collections instead of repeating helper lookups.
- The native 10-ms Meeting Mic/System/AEC relay reuses decode, clean-output,
  downsample, and encoded PCM scratch buffers across frames.
- Meeting checkpoint payload growth is linear/bounded through schema-v3
  base/delta compaction and pruning; do not restore cumulative snapshots.
- Long Meeting analysis is chunk-budgeted, concurrency-limited, hierarchically
  reduced, and persistently cached. Meeting finalization renews its durable
  lease, applies duration-scaled provider budgets, and deduplicates cross-track
  echo with a timeline sweep instead of all mic-by-system pairs.
- Coalesced `history_updated` events.
- Chunked/offloaded upload writes and export/cleanup work where practical.
- JobStore and latency metrics store connection reuse.
- CORS origin decision cache.
- Primary-tab code for Live Mic, Meetings, YouTube, File, and Settings is loaded eagerly
  in the local WebView. Do not restore route-level Suspense blanks for these
  tabs. `AppLayout` must use the existing Wouter router and must not wrap routed
  children in a second keyed Router that remounts the page on every tab change.
- Frontend Motion packages stay on a React-19-compatible release. The real
  browser smoke treats `element.ref` compatibility warnings and blank samples
  during primary-tab switching as failures.
- The compact active-Meeting response used by the global pill/idle preloader
  and the paginated Meeting library have different TanStack Query cache shapes.
  Keep the flat `['/api/meetings']` key separate from the infinite
  `['/api/meetings', 'history']` key, and project Meeting WebSocket state into
  both. Sharing one key crashes the Meetings tab when idle preload wins the
  mount race.
- The global active-Meeting pill and idle preloader call
  `/api/meetings?limit=1`. `activeMeeting` is returned independently of the
  history page, so increasing that limit only transfers unused Meeting rows at
  every startup and tab preload.
- Layered backend caches avoid PyInstaller when only application code changes.
  `build\tauri-sidecar-runtime-cache` contains the stable frozen Python runtime
  plus exact file-integrity metadata and stable Deno/yt-dlp media executables;
  tracked current `src` files are staged separately under `backend\app` with a
  concrete-version manifest. The full sidecar key uses media-tool content
  hashes instead of timestamps. Both the internal manifests and
  `scripts\ci\write_release_cache_keys.ps1` use
  `packaging\backend-sidecar-output-contract.json` instead of hashing the whole
  sidecar orchestration script. Bump that contract revision whenever builder
  behavior can change frozen backend or bundled-media bytes without changing a
  hashed source/spec/requirements/tool/flag input. Do not bump it for logging,
  timing, or parallel-process orchestration changes.
- Keep the runtime and application identities distinct. The expensive runtime
  key excludes `src`; the application and full-sidecar keys include the exact
  concrete version. The outer GitHub workflow fingerprint is an additional
  cache-source binding and must never be treated as the builder's inner runtime
  key or retroactively attached while validating a restored cache.
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
  `Frontend\node_modules` first, then restore the explicitly keyed npm package
  store only when that stronger cache misses. `actions/setup-node` must not
  eagerly restore the same package store on a hot `node_modules` path.
- Python dependency reuse in GitHub release builds is layered: prebuilt backend
  sidecar first, `.venv`/wheelhouse next, and an explicitly keyed pip package
  store only as a final fallback when every stronger product misses.
- GitHub Actions cache restore steps remain sequential within the release job.
  Only the disjoint Rust-audio, Rust-diarization, and FFmpeg internal-artifact
  fallbacks may overlap through
  `scripts\ci\restore_component_cache_artifacts_parallel.ps1`. Each child must
  use a private `GITHUB_OUTPUT` and a fixed destination. Keep Rust/main-target,
  backend, Python `.venv`, and Python wheelhouse restore/import paths serialized
  because they overlap destinations or form producer dependencies.
- `src/version.py` remains the leading app release version, but
  `Frontend\src-tauri\Cargo.toml` intentionally keeps a stable internal package
  version. `scripts\build_windows.ps1` writes a generated minimal Tauri release
  config overlay with the concrete app version and release-only overrides, and
  the Rust shell passes that value to the Python backend through
  `SCRIBER_VERSION`; do not restore per-release Cargo version churn.
- GitHub release builds set `CARGO_INCREMENTAL=1` and cache
  `Frontend\src-tauri\target\release\incremental` in the v2 Rust release cache.
- GitHub release and hybrid-check builds intentionally keep the installer
  toolchain pinned through `dtolnay/rust-toolchain@1.97.0`. This is the exact
  toolchain used by the current hot Cargo cache; update the pin and rebuild the
  cache deliberately as one operation, never by following `stable` implicitly.
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
  experiments. The workflow records uncompressed artifact size but disables the
  compressed-installer size gate for `none` non-tag runs; signed `v*` releases
  must continue to enforce the normal size budget. Signed `v*` updater releases
  may use `SCRIBER_NSIS_COMPRESSION` after a measured size/time tradeoff.
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
  only when Actions reports no matched key; a partial Actions restore must not
  trigger the 1.6-GB fallback. Ordinary `main` pushes do not run the full
  installer workflow. Exact Actions caches and large Cargo/venv/wheelhouse
  snapshots are refreshed only by the manual `release-windows.yml`
  `refresh_release_cache_artifacts=true` maintenance path. Tag releases do,
  however, self-heal a missing bounded exact backend, FFmpeg, audio, or
  diarization finished-product artifact after a successful rebuild. Manual
  shared cache publication is allowed only from `main` with
  `refresh_release_cache_artifacts=true`; feature-branch diagnostics are
  read-only with respect to shared caches and internal cache releases. An
  explicit non-main `workflow_dispatch` may save only the bounded exact Tauri
  app product in that ref's isolated Actions-cache namespace. That cache cannot
  warm `main`, tags, or sibling refs and does not enable any other cache save or
  publication. The maintenance path retains
  exactly one Actions-cache generation per allowlisted family, removes
  superseded internal cache-release tags, and current cache publishers keep
  only their replacement asset. After best-effort GC, the maintenance workflow
  must perform a fresh, non-best-effort inventory pass that requires the exact
  computed Rust dependency key to be the sole main-branch Rust generation and
  rejects any remaining allowlisted GC candidate. Publish and verify
  the app release first, then upload those four independent cache products in
  parallel with one private `GITHUB_OUTPUT` file per child. Cache publication
  is best-effort and must not delay or invalidate an already verified updater
  release. Heavy
  Actions caches remain restore-only on tags, so a routine release has one
  complete tag-triggered build rather than a duplicate main warm-up plus tag
  build.
- The Rust Actions cache is keyed by normalized Cargo dependency metadata plus
  resolved toolchain/target/profile, not by ordinary app source. The exact
  Tauri app binary is a separate small v2 cache keyed by full Rust/frontend
  sources, the Node version, binary-producing helper scripts, the versioned
  `packaging/tauri-app-binary-output-contract.json`, concrete version,
  toolchain, target/profile, updater runtime, and Outlook configuration
  fingerprints. The whole workflow is deliberately excluded: bump the output
  contract revision for otherwise-unrepresented binary-producing behavior, but
  not for cache probes, scheduling, diagnostics, or non-producing setup skips.
  Its attestation retains the producing
  commit as provenance without making unrelated Python-only commits miss. A
  validated hit keeps the restored frontend dependencies for the repository-local
  Tauri CLI, skips the redundant frontend type check, and may run bundle-only
  packaging; NSIS,
  updater signatures, checksums, and publication evidence are always fresh.
- Before backend sidecar cache save/publication,
  `scripts/ci/select_backend_sidecar_cache_entry.ps1` must validate and retain
  exactly the current internal SHA-256 directory. Never publish the cumulative
  `build/tauri-sidecar-cache` history. Rust/AEC/audio/diarization inputs remain
  outside the frozen Python backend key because those independent products are
  composed after the backend cache is created.
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
  the internal reusable GitHub release artifact `ffmpeg-profile-b-n7.0-v4`, and
  rebuild through MSYS2 only when restored Profile B tools are absent or fail
  validation.
- Profile B ffmpeg media tools, about `5.11 MiB` installed. Meeting
  finalization requires the FLAC encoder, Matroska and Ogg muxers, and `amix`;
  keep all four in the profile and fixture gate.

## Commands

Run from repository root unless stated.

Scriber owns its Python environment. Use `scripts\project-python.cmd` for every
local Python command; it resolves `venv\Scripts\python.exe` (or `.venv` as a
fallback) and fails closed when neither exists. Never use bare `python` or `py`
for tests, runtime checks, generators, or smoke scripts after the environment
has been created. This prevents a global Python installation with stale
Pipecat/provider packages from producing misleading failures. The launcher also
checks the `pipecat-ai` version against the exact pin in
`requirements-base.txt` before it executes the requested command.

```powershell
scripts\project-python.cmd -m pytest
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

`-FastLocalStagedApp` must finish by writing
`scriber-autoresearch-runtime-attestation.json` into the staged release root.
The attestation binds the current Git worktree digest to the final desktop,
backend, and audio-sidecar hashes, sizes, and native versions. FastLocal Doctor,
profile, and scoring must fail closed when it is missing or stale. Do not
retroactively attest an older candidate after unrelated source or binary
changes, and do not reintroduce full local paths or hardware inventory into the
tracked benchmark profile.

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
scripts\project-python.cmd scripts\smoke_frontend_browser.py --output tmp\frontend-browser-smoke.json
```

Rust audio sidecar short physical smoke:

```powershell
scripts\project-python.cmd scripts\smoke_rust_audio_sidecar.py --mode wasapi --duration-sec 1 --output tmp\rust-audio-sidecar-smoke.json
```

Rust audio prewarm sidecar smoke:

```powershell
scripts\project-python.cmd scripts\smoke_rust_audio_prewarm_sidecar.py --duration-sec 1 --prebuffer-ms 400 --output tmp\rust-audio-prewarm-sidecar-smoke.json
```

Use `--mode wasapi` to exercise the real passive WASAPI prewarm worker:

```powershell
scripts\project-python.cmd scripts\smoke_rust_audio_prewarm_sidecar.py --mode wasapi --duration-sec 1 --prebuffer-ms 400 --output tmp\rust-audio-prewarm-sidecar-wasapi-smoke.json
```

Use `--prewarm-before-capture` on the sidecar capture smoke to prove buffered
prewarm frames are adopted into the next capture within one sidecar session:

```powershell
scripts\project-python.cmd scripts\smoke_rust_audio_sidecar.py --mode wasapi --duration-sec 1 --prebuffer-ms 400 --prewarm-before-capture --skip-selected-hash --output tmp\rust-audio-sidecar-adopt-wasapi-smoke.json
```

Rust audio app-level prewarm adoption smoke:

```powershell
scripts\project-python.cmd scripts\smoke_rust_audio_app_prewarm.py --mode wasapi --duration-sec 1 --prewarm-duration-sec 1 --capture-cycles 1 --prebuffer-ms 400 --output tmp\rust-audio-app-prewarm-wasapi-smoke.json
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
scripts\project-python.cmd scripts\validate_recording_hot_path_comparison.py `
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

## Endpoint-Security-Safe Automation

- Treat enterprise EDR visibility as a design constraint. Never use
  `Invoke-Expression`/`iex`, `-EncodedCommand`, downloaded or generated script
  text, or AST extraction followed by execution. Do not pass generated
  multi-line source through `powershell.exe -Command`.
- Put reusable PowerShell logic in reviewed, checked-in `.ps1` or `.psm1`
  files with typed, allowlisted parameters. Invoke those files with `-File`;
  prefer direct Python, Rust, npm, Cargo, or GitHub Actions primitives when
  PowerShell adds no value.
- PowerShell tests may parse a script to validate syntax, but must not
  re-evaluate extracted function bodies. Prefer static contract tests or a
  narrow invocation of the real checked-in script with fixed arguments.
- Do not routinely add `-ExecutionPolicy Bypass` to new local commands. It is
  not an EDR bypass or a security boundary. Existing documented invocations
  may retain it for compatibility until they are deliberately migrated.
- Keep full installer builds, parallel cache publication, and large process
  trees on GitHub-hosted runners unless the user explicitly requests a local
  build. Locally, run the smallest focused gate once and avoid repeatedly
  replaying an EDR-sensitive command while debugging.
- Never recommend disabling CrowdStrike or excluding `powershell.exe`, a user,
  the whole repository, or all child processes. After removing the suspicious
  technique, any still-reproducible benign alert must be reviewed by the
  administrator and, if necessary, receive only a narrow, time-bounded IOA or
  signed-publisher exception scoped to the exact script, command line, host
  group, and detection pattern.

## Editing Guidance

- Keep edits scoped to the feature or bug being addressed.
- Preserve established local patterns before adding abstractions.
- Add tests when changing contracts, pipeline lifecycle, provider behavior,
  packaging gates, or user-visible workflows.
- Use docs only for durable facts and decisions. Put temporary investigation
  output in `tmp\` or commit messages, not new permanent markdown files.
- When changing implementation status, update `README.md`, this file, or the
  relevant category doc in the same change.
