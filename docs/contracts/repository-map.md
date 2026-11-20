# Repository map

## Product Snapshot

- Scriber is an AI transcription app for live microphone dictation, bot-free
  meeting capture, YouTube transcription, file transcription, podcast subscriptions, transcript
  management, summaries, and PDF/DOCX export.
- Primary desktop runtime: Tauri 2 shell, React frontend, Python backend sidecar.
- Backend default: `127.0.0.1:8765`, implemented with `aiohttp`, WebSocket
  events, SQLite, Pipecat pipeline code, and provider adapters.
- Frontend default in dev: `localhost:5000`, implemented with Vite 8, React 19,
  TypeScript, Tailwind v4, Wouter, and TanStack Query. The Vite-only frontend
  toolchain is pinned to Node.js 26.12.1 through `.node-version` and
  `Frontend/package.json`; the removed Express/Replit scaffold is not part of
  the desktop runtime or build. `npm ci` consumes the canonical npm lock and
  installs Bun 1.4.3 locally for the app/test type checks and isolated logic
  tests. The logic launcher forwards `SIGINT`/`SIGTERM` to its active Bun child,
  waits for closure and reports cancellation as a failure even after a clean
  child exit. `npm run test:tooling` verifies this separate launcher contract.
  Keep the `typescript` alias to `@typescript/typescript6@6.0.2` for
  ESLint and other API consumers. The normalized configs use an explicit
  ES2015 target with `noEmit` and relative path aliases; Vite produces the
  JavaScript. Component tests use Node Vitest/jsdom in the `vmThreads` pool
  with two to four reused workers; each file still gets a fresh VM context, and
  `vitest.setup.ts` fails the run if files share one. The VM pool needs at least
  two workers. For isolated serial debugging, use
  `npm run test:components -- --pool=threads --maxWorkers=1` (or
  `--pool=threads --no-file-parallelism`). ESLint uses its content cache.
  Preserve the npm lock and the install metadata used to validate an exact
  release dependency cache hit.
- Runtime is Windows-first. Linux/macOS support is mostly fallback/dev support.
- Legacy Python tray/UI code was removed. The Tauri shell owns desktop UI,
  tray/menu actions, global hotkeys, and the recording overlay.
- The pre-created recording-overlay WebView must register its native event
  listener before completing `native_overlay_renderer_ready`; that handshake
  returns the authoritative current snapshot so a hotkey fired during lazy
  renderer startup cannot leave a visible but transparent popup.
- Native lifecycle revisions order overlay events and renderer-ready snapshots.
  A hotkey preview has a UUID owner, is admitted only while hidden, and failure
  cleanup must compare that owner under the native mutation lane. It must never
  hide a later backend transition. Backend shows carry the recording-session
  owner too; finalization and startup cancellation hide only that owner. A
  successor can already be initializing before the controller's active session
  changes, so native ownership remains authoritative during this overlap. Log
  lifecycle changes and dispatch outcomes, not RMS frames, transcript content,
  or raw window identifiers.

## Component owners

### Backend and runtime

- `src/web_api.py`: main aiohttp application composition and controller,
  settings, jobs, transcript history, mic control, and the routes not yet split
  into domain modules.
- `src/api/` owns the HTTP route modules, WebSocket handling, shared app keys
  and authentication checks. File and Meeting imports share the upload policy.
  [YouTube browser sessions](../../src/api/youtube_browser_session.py) and
  [podcasts](../../src/api/podcast_routes.py) each have a dedicated route boundary.
  Controller-backed route modules own their narrow structural controller port
  beside the handlers that consume it; controller-free domains own local
  collaborator interfaces/providers and a wiring guard instead. Do not recreate
  a central controller-port catalogue. The matching route test owns the exact
  adapter or wiring assertion, including boundary-critical return types; shared
  test support may provide reflection mechanics but never a list of application
  ports. Runtime routes
  own debug logs and support bundles. Each ONNX
  download owns a request-local progress scope so parallel requests cannot
  drain one another; final state follows its admitted progress work, and
  aiohttp cleanup seals every active scope before cancellation. Transcript
  routes read through `ScriberWebController.transcript_view` and delegate the
  whole summary lifecycle to `summarize_transcript`, which returns a domain
  outcome the route maps onto a status code. The Transcript route module owns
  the immutable `TranscriptView` and `SummaryOutcome` DTOs returned across that
  boundary; do not recreate looser writable-field Protocols for those values.
  When an extracted handler needs
  controller internals, add a public method shaped by that need instead of
  widening a port with private names. Local Polishing owns the typed
  failure-code table that maps each bounded error onto its status and public
  message; an unexpected failure never reaches the client verbatim. Device
  routes keep the Tauri-owned autostart contract answering explicitly instead
  of 404, and enumerate microphones off the loop. Outlook Calendar depends on
  the calendar collaborator rather than the controller, and resolves it per
  request: composition must stay able to build an application whose controller
  never materializes a calendar. Its OAuth callback answers the system browser
  in HTML and records every sync failure before responding, so a degraded
  connection stays visible in `status()`. File Transcription owns multipart
  parsing, bounded disk streaming, ffmpeg preparation, workspace cleanup, and
  the one ownership hand-off to its durable job. Its controller port exposes
  only the immutable admission plan, a public workspace root, that hand-off,
  and explicit checkpointed File-job resume;
  the route must never read `_downloads_dir` or mutable provider settings.
- WebSocket routes own browser-origin validation, initial state delivery, ping
  handling, and the add/remove-client lifecycle behind a four-member controller
  port. Every registered connection is removed in `finally`, including when its
  initial send fails.
- Live Mic routes own request-marker parsing plus start, stop, stop-request, and
  toggle response mapping. Their four-method controller port exposes only
  lifecycle commands and route-owned outcomes; microphone, task, provider, and
  audio-admission state remain controller-owned. Installed provider replay stays
  behind a separate exact activation collaborator rather than widening the
  controller port or exposing private controller fields.
- Meeting Capture owns strict parsing and normalization for start plus the
  start/pause/resume/stop HTTP lifecycle. Its immutable start command and three
  ID-only commands cross a four-method public controller port; claim, native
  shell, recorder, preview, finalizer reservation, and durable state
  orchestration remain controller-owned.
- Meeting Workspace is controller-free: title changes, transcript search and
  corrections, notes, and action-item edits share one request-resolved durable
  store plus broadcast boundary. Keep its eight store operations behind the
  domain-local collaborator Protocol; do not mirror them as controller
  middle-men.
- Meeting Processing owns the HTTP boundary for speaker-identity/full-transcript
  reprocessing, finalization retries, and analysis reruns. Strict immutable
  commands cross one three-method controller port; the controller remains the
  single owner of task reservation, durable state transitions, provider/model
  changes, rollback, and worker-gate ordering.
- Meeting Artifacts is controller-free: playback, JSON/Markdown/document/audio
  exports, email previews, and RFC 822 drafts resolve one immutable dependency
  bundle per request. Its boundary exposes only exact Meeting reads, the public
  storage root, the document renderer, and the fallback language; never add
  forwarding controller methods or expose private storage paths in responses.
- Meeting Catalog owns list pagination, enriched detail reads, and both discard
  aliases behind one three-method controller port. The controller retains
  artifact/reprocessing projection, task/import admission checks, the durable
  discard tombstone, and cancellation-safe workspace/transcript/store cleanup;
  a canceled HTTP request must not abandon any cleanup step after tombstoning.
- Meeting Device Readiness owns strict device-test input parsing plus the
  capabilities, redacted endpoint inventory, and ephemeral probe HTTP
  responses. Its three-method controller port leaves native-audio admission,
  Shell IPC, level-probe ownership, prewarm, lease renewal, and
  repeated-cancellation cleanup with the controller. The probe persists and
  uploads no PCM.
- `src/api/upload_policy.py` owns the Windows-safe filename, accepted
  media-extension, immutable provider-bound byte limits, and limit-label
  invariants; ingest handlers consume that interface directly rather than
  through aliases in `web_api.py`. Voice Component owns the optional speaker model and local
  diarizer together with the Voice Library profile collection, opaque preview
  capabilities, native enrollment workflow, and every model/profile mutation
  lock. Keep its enrollment-admission provider separate and lazy: model status
  must not materialize global audio state or a diarizer. The admission adapter
  is the only place this domain may coordinate Live Mic/Meeting/device-test
  conflicts, the shared native-audio lease, prewarm, or the global active flag.
- `src/pipeline.py`: STT pipeline orchestration, provider factory, analyzer
  cache, mic resolution, async/direct transcription.
- `src/mic_silence_stop.py`: optional session-local Silero silence observer for
  Live Mic (`micAutoStopEnabled`, default off; `micAutoStopSilenceSeconds`, 1–10,
  default 5). It passes PCM through without provider VAD/turn frames and requests
  one normal stop. Confirmed native stop plus EOS/provider-ingress drain may
  release audio admission before provider finalization. Keep finalizing records
  and ordered insertion futures session-owned, preserve raw/polishing mode for
  a queued start, and prevent old UI/overlay callbacks or prewarm cleanup from
  affecting a newer recording. Caller cancellation must wait for finalization;
  shutdown joins every finalizer before closing the shared polisher.
- `src/core/provider_audio_formats.py` and `src/audio_prepare.py`: exact
  provider/route/model audio-format registry, ffprobe container+codec
  validation, compressed batch selection, bounded ffmpeg preparation,
  and task-scoped cleanup of generated upload artifacts.
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
  pinned yt-dlp/EJS/QuickJS-ng runtime and validates downloaded audio before
  provider upload. Frozen builds accept only the complete manifest-bound
  wrapper bundle under `tools/ffmpeg`; yt-dlp itself is imported from the
  frozen PyInstaller package and no pip/distlib launcher is shipped. Source
  runs may use the explicit
  `SCRIBER_QUICKJS_DEV_WRAPPER_PATH` override, but never an arbitrary `qjs`
  from `PATH`.
- `src/database.py`: SQLite WAL persistence, metadata loading, FTS5 search.
- `src/podcasts/`: public-only RSS/directory transport, durable subscription and
  episode state, temporary processing downloads, independent feed refresh, and three concurrent
  processing workers. Its own `truststore` TLS client context uses native system
  certificate-chain and hostname verification, including managed Windows trust.
  Never inject this context globally or disable TLS verification; retain the
  public-address resolver and redirect checks. Downloads serialize their capacity
  check and write to preserve
  the shared cache limit. Terminal completion and media-ownership release precede
  best-effort ready notifications; a delayed notification must not block download
  removal or clear a successor retry's ownership. Its processor alone adapts to
  File admission and Transcript summaries. Commit the transcript ID before
  admission and resume that identity;
  only explicit retry may allocate a new attempt after failure. The first
  subscription queues its latest episode, subsequent new episodes queue while
  the app runs. Preserve the per-episode/cache limits and cancellation barriers.
  File's `stopped` status is terminal unsuccessful work; explicit episode retry
  allocates a fresh transcript identity. Summary registration ends in the
  summary function's identity-checked `finally`, even when its caller is a
  persistent podcast worker.
  Cover artwork (RSS `itunes:image`/`image`, Apple `artworkUrl600`) is stored
  per subscription and served only through the authenticated
  `GET /api/podcasts/artwork/{id}` route, because the webview CSP allows local
  images only. Its fetches use a separate cookie-less two-connection pool with
  the same public-address and redirect checks, HTTPS on every hop, a 2 MB limit,
  and a JPEG/PNG/WebP content-type and magic-byte allowlist. The disk cache under
  the podcasts root holds at most 300 files and is pruned on unsubscribe; logs
  never contain artwork URLs.
  Commit completion before deleting audio after successful transcription and
  summary. Startup and pre-download cleanup verify both saved results before
  reclaiming legacy completed audio, including files whose byte-count commit was
  interrupted; failed work remains retryable. Listening
  streams from the publisher through the public-only transport with bounded
  chunks and byte-range support, never a download/provider job. Forward no
  browser credentials and close upstream responses on every exit. Keep refreshed
  enclosure URLs current for completed episodes too. Retire legacy audio-only
  jobs without creating new transcription attempts.
  `src/api/podcast_routes.py` owns the strict controller-free HTTP boundary and
  local collaborator port; never add scheduler logic or private paths to the UI.
  Episode listening state (`seen_at`, `read_at`, `played_at`) lives in the same
  store. Only post-subscription refreshes create unseen episodes; the schema
  migration marks existing rows seen/read so an upgrade never floods users.
  One refresh pass
  publishes one `podcast_episodes_discovered` event; each completion publishes
  `podcast_episode_ready`. Their bounded titles reach only the WebSocket, never
  logs, and a notification failure must not affect refresh or processing.
- `src/data/job_store.py`: persistent file/YouTube jobs.
- `src/data/latency_metrics_store.py`: hot-path metrics.
- `src/core/`: contracts, state machine, circuit breaker, logging, tracing.
- `src/runtime/audio_frame_pipe.py`: Python decoder/validator for the Rust
  audio frame-pipe protocol.
- `src/runtime/provider_http.py`: event-loop-owned reusable aiohttp provider
  transport, bounded DNS/connection pooling, and privacy-safe request timing.
- `src/runtime/cancellation.py`: the boundary discipline for work a thread
  cannot abandon. `to_thread_cancellation_barrier` holds a durable commit
  point until its worker really finishes; `await_with_delayed_cancellation`
  returns the pending `CancelledError` beside the result so a caller can
  record the ownership it just acquired before unwinding. Call these directly
  rather than through aliases in `web_api.py`.
  Controller maintenance SQLite workers use this barrier; shutdown joins the
  maintenance task before persistence-store close even after the general drain
  timeout or repeated cancellation. Synchronous shutdown retains its task
  reference until that join can finish.
- `src/runtime/sqlite_guard.py`: every thread-local SQLite store
  (`database.py`, job, latency-metrics, Meeting-import, and transcript-artifact
  stores) opens `GuardedConnection`. Its `close()` waits for a foreign thread's
  running statement step or `with connection:` transaction before freeing the
  native handle; an orphaned `to_thread` read otherwise crashed shutdown with a
  Windows access violation. A close that cannot wait leaves the retired handle
  to its owner. Keep new store connections on this factory.
- `src/runtime/task_supervisor.py`: event-loop-local ownership for intentionally
  concurrent asyncio work. It retains tasks, observes every result, reports
  failures through the loop handler, reserves thread-safe submissions before
  they reach the loop, and exposes bounded drain/cancel plus a sealing close
  barrier. Do not schedule fire-and-forget work through hidden `create_task()`
  lambdas; use the owning module's supervisor so shutdown can account for work
  accepted immediately before the barrier and reject work offered afterwards.
- `src/runtime/audio_admission.py`: the single owner of this process's native
  audio lease. Only one capture may hold the device and the exclusion is
  durable and cross-process. `AudioAdmissionOwner` serializes acquire, transfer,
  durable marking, loss, release, and close; converts the store's authoritative
  UTC expiry into monotonic stop/expiry deadlines; and privately supervises one
  heartbeat plus an independent watchdog. A per-capture loss handler must
  confirm native producer and local persistence cleanup before the owner may
  release SQLite ownership. Unknown stop or release state retains the claim for
  retry. Live Mic must carry the Rust producer's explicit stop confirmation
  through `MicrophoneInput` and `ScriberPipeline`: downstream provider/queue/
  text cleanup failure must not retain the lease after native stop is confirmed,
  while an unknown native result remains fail closed.
  Live Mic retains the exact pipeline after an unconfirmed stop so explicit
  Stop, lease loss, and shutdown can retry that producer. Expose
  `nativeCleanupPending` until both native release and durable admission release
  are confirmed; the Live Mic page and command palette keep Stop available.
  Missing EOS or an undrained audio queue fails the transcript even when the
  native producer has already exited; never finalize partial PCM as success.
  Only a current Meeting
  claim explicitly marked durable after its `recording` row commits may ride
  out a generic renewal-store outage; pending
  Meetings, Live Mic, device tests, and Voice enrollment fail closed before
  expiry. The default ten-second stop margin permits two immediate four-second
  Shell stop attempts; another attempt requires a successful renewal first.
  Same-controller Meeting rebinding is adopted, foreign supersession is a loss,
  and cancellation cannot abandon a committed acquire/transfer/release. Close
  seals lifecycle admission before synchronization; only its final supervised-
  task drain is time-bounded. Go through the owner rather than touching
  `_persistent_audio_claim` directly; that attribute remains only as the
  controller-storage migration seam. Heartbeat/watchdog tasks and loss state
  belong exclusively to the owner and must never be mirrored on the controller.
- `src/native_overlay.py`: Python facade for the Tauri-owned recording overlay
  exposed through private shell IPC.
- `src/local_polishing/`: immutable, fail-closed Hugging Face GGUF catalog,
  resumable/hash-verified model installation, conservative output safety gate,
  and loopback-only llama.cpp lifecycle for optional local live-mic polishing.
  The sole product variant identity is `qad_q4_0`: the LFM2.5 350M Production QAD-Q4_0
  GGUF. The former local Gemma models have been removed from active product
  choices; their exact descriptors remain only for verified legacy cleanup.
  The active schema-3 contract renders the exact
  short LFM training serialization as `plain_completion_v1`, sends the raw
  transcript without KEEP markers, caps generation at 384 tokens, requires
  `plain_text_v1`, and returns the source unchanged after any runtime or safety
  failure. QAD is the only permitted quantization for this model: do not export,
  benchmark, or add PTQ, Q8, BF16, or another quantized product alternative.
  Resolve artifact identity from `src/local_polishing/catalog.py`. Keep anonymous
  install and exact attribution. For training, evaluation, corpus work or export,
  follow [the active PRAXIST contract](../../praxist_task/AGENTS.md).
- `src/main.py`: compatibility notice for the removed Python desktop UI; use
  Tauri for desktop runs.

### Frontend and shell

- `Frontend/client/src/pages/Podcasts.tsx` marks episodes seen only while the
  page is visible and keeps their New marker for that visit. Opening a
  transcript anywhere marks it read.
  The Podcast overview separates newly discovered episodes from ready episodes
  still neither read nor played, and links each count to a matching podcast.
  Sidebar attention counts deduplicate their overlap; their description exposes
  both counts. Keep read and played indicators independent, and never treat
  merely displaying the episode list as consuming a ready episode.

- `browser-extension/chrome/`: loadable Manifest V3 extension for the
  user-initiated YouTube-to-Scriber handoff. It extracts only the current public
  video ID plus bounded visible title/channel metadata and opens the versioned
  `scriber://youtube/transcribe` protocol; it must never receive the backend
  session token. The optional session transfer requests YouTube cookie access
  and host access to `http://127.0.0.1:8765`. Preserve the injected action
  across YouTube SPA navigation and keep the toolbar popup fallback.
  See [YouTube session handling](providers-and-media.md#youtube-session-handling).
- `Frontend/client/src/App.tsx`: routes; the six primary user tabs are eager,
  while Debug Console, transcript detail, and not-found surfaces remain lazy.
- `Frontend/client/src/main.tsx` starts the initial locale catalog and only the
  selected main/tray/overlay module together, then waits for both before React
  renders. Keep the overlay's transparent document markers ahead of its import.
  Main and tray also prefetch their bounded initial native backend access during
  module loading. Reuse that startup promise at mount without blocking the
  localized loader; normal health/restart checks must still refresh access.
  Idle Settings preloading publishes the settings resource independently of
  microphone discovery. Before writing the query cache, verify the bootstrap
  generation and unchanged query state so a late preload cannot replace a save
  or an independent query response, even within the same millisecond.
- `Frontend/client/src/hooks/use-backend-status.tsx` subscribes to the native
  `backend-status-changed` invalidation before its initial authoritative query.
  Coalesce concurrent checks and retain one follow-up for events received during
  a check; bound listener setup and retain polling/focus recovery. Action-only
  consumers use the stable `useBackendActions` context instead of subscribing to
  every health-check timestamp. The native supervisor emits an empty event only
  when readiness, running/starting state, or backend URL changes. `RuntimeShell`
  consumes only the separate boolean `useBackendOnline` value so its parent
  render cannot reintroduce timestamp-driven renders of the routed pages.
- `Frontend/client/src/pages/`: Live Mic, Meetings, YouTube, File, Podcasts, Settings,
  Debug Console, Transcript Detail.
- `Frontend/client/src/hooks/use-browser-youtube-import.ts` owns one-shot
  browser handoff consumption. It must subscribe to search-string changes so a
  second `scriber://` request is consumed even when the running app is already
  on `/youtube`, and it must retain a new request while an earlier start is in
  flight.
- Meeting review keeps its timeline math and deterministic search/filter rules
  in `Frontend/client/src/lib/meeting-review-timeline.ts`; do not duplicate them
  in row rendering or transport code. `MeetingReviewToolbar` owns the visible
  review controls, while `Meetings.tsx` remains the audio element and durable
  note hand-off owner. Playback may follow the active virtual row only while
  Follow is enabled; a manual review scroll must turn Follow off rather than
  fight the user. Keep live Latest-text following as a separate behavior.
- Meeting note teardown saves compare the current draft with the newest
  effective write generation. Matching older saved or in-flight text must not
  skip a newer write needed to preserve a user revert; discard superseded
  queued drafts before deduplicating the teardown save.
- `Frontend/client/src/contexts/WebSocketContext.tsx`: shared WebSocket.
- `Frontend/client/src/lib/backend.ts`: backend URL and Tauri token bridge.
- `Frontend/client/src/lib/api-types.ts`: shared REST-facing TS types.
- `Frontend/client/src/components/NativeRecordingOverlay.tsx` owns recording
  overlay state, settings, the static full-pill CSS gradient, and the classic
  full-pill bar renderer. All three selectable styles begin at the far-left pill
  edge, draw at most 60 FPS on their normal paths, cap DPR at 3, and keep their
  per-frame buffers out of React state;
  `MicrophoneEnergyField.tsx` owns the allocation-bounded transparent Canvas
  renderer. Its normal path is capped at 60 FPS and DPR 3, samples 96 points per
  strand, and keeps the core stroke one physical pixel wide. The energy wave
  spans the full pill from its left edge. In the transparent Tauri window the
  Canvas must use the normal alpha-composited 2D context, paint the same static
  pill background itself, and keep its own rounded clip; WebView2 may otherwise
  promote the low-latency `desynchronized` path into an opaque rectangular
  layer. Keep the drop shadow on a static, isolated, pill-shaped sibling behind
  the clipped surface so its spread and fade cannot become box-shaped. The stop
  control stays in the DOM for all three styles but is visually hidden only for
  fine-pointer hover devices, then revealed by pill hover or keyboard focus;
  the default remains visible for touch-only devices. Do not add an
  `any-pointer: coarse` override:
  hybrid Windows devices can report both coarse and fine pointers. Keep this
  interaction CSS-only, keep per-frame RMS out of React state, and preserve the
  listener-before-`native_overlay_renderer_ready` handshake described above.
  `MicrophoneBlueFlame.tsx` and `blue-flame-visualizer.ts` own `blue_flame`:
  16 fine strands with 96 samples each and one reused Float32 buffer, bounded
  frame rate/DPR, reduced-motion behavior, normal alpha composition, and the
  same static pill background, rounded clip, shadow, and stop interaction.
  `ui/info-tooltip.tsx` owns neutral supplementary information with hover,
  keyboard focus, tap, and Escape support. Contributor data-sharing information
  stays directly adjacent to its model choice as well as in its tooltip.
- `Frontend/client/src/i18n/`: persistent `de`/`en` interface locale,
  translation catalogs, locale-aware formatting, and catalog completeness
  tests. Keep interface locale separate from STT/output language. Every
  user-facing literal passed to `t(...)` needs a German catalog entry, and the
  Tauri `set_ui_locale` bridge must keep native tray tooltips synchronized.
- `Frontend/client/src/components/transcription-history-toolbar.tsx`: shared
  count/search/list-grid toolbar for Live Mic, YouTube, and File history.
- `Frontend/client/src/index.css`: Tailwind v4 CSS-first design system. The six primary tabs
  and the lazy Debug Console share the `app-page-shell` 1320 px desktop frame and expose a
  stable `data-page-shell` hook; do not introduce per-tab maximum widths.
  The main app viewport uses a document-owned, auto-hiding overlay scrollbar
  and a one-pixel inset window frame. Keep both the native DWM border and the
  undecorated-window shadow disabled; the latter otherwise reserves a
  DPI-dependent non-client row above the WebView. Request the rounded Windows
  corner preference once before the hidden main window is revealed. The main
  HWND frame must remain theme-invariant so the document-owned frame and every
  visible edge participate in the circular light/dark View Transition instead
  of changing color after it finishes.
  Motion follows the shared transitions.dev Refine/Polish tokens in `:root`.
  Reusable text/icon swaps, success checks, tooltips, error shakes, and animated
  checkboxes live under `Frontend/client/src/components/ui`; history skeletons,
  the list/grid pill, and accordion disclosure reuse the same semantic timing
  tokens. Toggle/Switch controls intentionally retain their existing behavior.
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
  or any production WASAPI path. End-to-end Live Mic speech tests may separately
  set `SCRIBER_RUST_AUDIO_SYNTHETIC_LIVE_MIC_PCM_S16LE_16000_MONO_PATH` to one
  bounded 16 kHz mono signed-16 PCM fixture; it has the same play-once and
  test-only constraints.
  Meeting capture uses one sidecar process for 48 kHz microphone plus loopback,
  pinned LocalVQE v1.3 processing, and shared-timeline raw mic/system/clean mic pipes.
  `native/scriber-localvqe` owns the single embedded 4.8M F32 model and static
  GGML bridge; source/model revisions and hashes live in its `inputs.json`.
  x64 enhancement requires AVX2/FMA/F16C, checked before native inference.
  Preserve the 16 kHz 256-sample hop adapter, one-hop delay compensation,
  bounded shared-timeline queue, and Stop/Pause/EOF tail flush. Model construction
  must emit no stdout because that channel carries the sidecar JSON protocol.
  Initialize the model and connect output consumers before opening upstream
  capture pipes; opening those pipes starts bounded producer writes.
  The token-protected Meeting device test must reuse this path, remain explicit
  and local-only, return only bounded level/activity statistics, and always stop
  its ephemeral sidecar capture without persisting or uploading PCM. Product
  requests remain capped at five seconds. The installed physical-audio gate may
  raise the fail-closed cap to exactly 60 seconds before backend launch; it must
  use real WASAPI, renew the native-audio admission lease, require continuous
  microphone/system/clean-mic frames, bound full-process-tree CPU and
  working-set/private-byte growth, create no audio artifacts, and finish with a
  strict uninstall.
- `Frontend/src-tauri/src/audio_sidecar_client.rs`: Tauri-side sidecar lookup,
  stdio JSON-lines client, and process lifecycle registry. It only uses
  allowlisted executable names, supports `SCRIBER_AUDIO_SIDECAR_EXE` for local
  test runs, keeps successful capture sidecars keyed by `streamId`, and redacts
  executable paths to hashes in diagnostics.
- `Frontend/src-tauri/src/audio_frame_pipe.rs`: Rust encoder/validator for the
  audio sidecar binary frame protocol.
- `Frontend/src-tauri/src/audio_codec.rs` and
  `Frontend/src-tauri/src/audio_prepare.rs`: fail-closed capture-time audio
  preparation contract and bounded worker. `wav_pcm16_virtual` is a
  capture-lab-only virtual PCM16 WAV view over shared capture segments and must
  advertise `productionReady=false` and `artifactExposed=false`; it is not a
  provider upload artifact. `wav_pcm16_file_v1` is the production PCM16/WAV
  writer: it streams into a shell-created bounded lease file during capture,
  patches the RIFF header at finalization, and hands the validated file to the
  Python backend for the exact Speechmatics batch upload. The backend must
  release it through `audioCaptureArtifactRelease`; shell shutdown is the final
  cleanup owner. Enable it only from a frozen `speechmatics_async` + `batch_v2`
  + `enhanced` capability snapshot with verified `wav_pcm16`, the current
  capability revision, and the default endpoint fingerprint; custom or stale
  routes keep the existing PCM-spool fallback. Experimental codec feature
  names are inert lab gates until a measured implementation is pinned and
  promoted.
- `Frontend/src-tauri/src/lib.rs`: Rust supervisor, Tauri commands, tray/menu,
  autostart, global hotkey, single instance, updater/process plugins, and the
  strictly parsed `scriber://` browser handoff. A second Windows instance must
  queue only a validated, expiring YouTube request for the primary instance and
  must not log raw video metadata.
  Initialize Rustls's existing Ring provider before starting Shell IPC or Tauri
  threads. Reqwest uses `rustls-no-provider`; an updater check must never be
  required before another shell client can construct its TLS transport.
  Bind the backend's package version and resource directory atomically at the
  start of desktop setup. Tauri can create WebViews before the setup callback;
  early backend commands must defer until configured, without probing or
  spawning. Never use the stable internal Cargo version as an app-version fallback.
- `Frontend/src-tauri/src/shell_ipc.rs`: private backend-to-shell named-pipe
  IPC for opt-in native shell work, including text injection, diagnostics, and
  the synchronous Windows fallback-model toast acknowledgement.
- `Frontend/src-tauri/tauri.conf.json`: Tauri build, CSP, NSIS bundle, backend
  resource mapping, before-bundle sidecar command.
- `Frontend/src-tauri/windows/installer-template.nsi`: reviewed Tauri CLI
  2.12.4 NSIS template with manual NSIS version upgrades routed directly to the
  non-destructive overlay path. Do not restore Tauri's destructive maintenance
  prompt for this case: its uninstall path intentionally removes taskbar pins,
  Start-menu shortcuts, and the HKCU autostart value. Same-version maintenance,
  downgrades, WiX migration, and strict standalone uninstall retain upstream
  behavior.

### Packaging and scripts

- `packaging/scriber-backend.spec`: PyInstaller onedir backend sidecar spec.
- `packaging/wheels/numpy-2.4.6+scriber.noblas.1-cp314-cp314-win_amd64.whl`:
  locked no-external-BLAS NumPy product wheel for the frozen backend only. Its
  provenance and complete validation contract live in
  `packaging/wheels/numpy-noblas-wheel-lock-v1.json`; never install it into the
  shared build venv or replace the public NumPy requirement with it. Rebuild it
  only through the fail-closed builder with the tracked `stdalign.h` input; two
  independent clean builds must produce identical shipping bytes.
- `packaging/cpython-windows-runtime-input-lock-v1.json`,
  `scripts/build_cpython_windows_runtime.ps1`, and
  `scripts/perf/profiles/python-runtime/`: exact CPython 3.14.9/LLVM 19.1.7
  runtime inputs, offline ClangCL/ThinLTO/PGO/tail builders, and the isolated
  `python-runtime-ab-v1` promotion profile. Shipping defaults to official
  CPython 3.14.9 with JIT disabled unless installed FullLocal evidence promotes
  a variant; microbenchmarks cannot select a product runtime. Stock PyInstaller
  6.20 ignores `PYTHON_JIT` in its isolated embedded interpreter, so O1/C1/T1
  currently fail the frozen runtime policy and are not eligible variants.
  `scripts/run_python314_o0_full_local_correctness.ps1` measures the selected
  O0 fallback from a fresh installed tree but is explicitly not promotion
  evidence because it has no same-source A13 or rebooted AB/BA blocks.
- `packaging/python-314-upgrade-from-v0.5.47.json`: public v0.5.47 installer
  identity, 33 exact CP313 ABI tombstones, 43 exact CP313/UCRT runtime
  tombstones, and the one known empty runtime directory for the 3.14
  transition. Installer hooks may use only exact `Delete` and non-recursive
  `RMDir` statements. Final acceptance compares a real v0.5.47 overlay
  byte-for-byte with a clean CP314 install and preserves user data through
  strict uninstall.
- `packaging/overlay-upgrade-from-v0.5.138.json`: public v0.5.138 installer
  identity plus the 48 files and 17 directories an overlay would otherwise
  keep after Pipecat 1.14 dropped NLTK/Punkt/regex and the dependency
  updates renamed versioned package metadata. Derive such tombstones from a
  diff of the published and candidate installer inventories, never from
  guesses; `tests/test_overlay_upgrade_tombstones.py` binds each entry to one
  exact hook line and rejects tombstones for currently pinned metadata.
- `packaging/quickjs-youtube-runtime-lock-v1.json`,
  `native/scriber-quickjs-wrapper/`, and
  `scripts/build_quickjs_youtube_runtime.py`: byte-locked QuickJS-ng engine,
  bounded yt-dlp file-protocol wrapper, deterministic engine hardening, and
  offline-capable four-file YouTube JavaScript runtime preparation. Normal
  builds provision the byte-locked wrapper from the dedicated prerelease cache
  asset; `--rebuild-wrapper` is diagnostic-only because MSVC linker updates can
  change PE bytes even with the pinned Rust toolchain and `/Brepro`.
  `scripts/ci/verify_quickjs_wrapper_cache_asset.ps1` must download and hash the
  asset before release producers start; cache maintenance must preserve the
  current QuickJS cache tag.
- `scripts/build_tauri_backend_sidecar.ps1`: sidecar build, runtime import
  checks, media-tool bundling, optional cache reuse. Official signed builds
  Authenticode-sign `scriber-backend.exe` before the runtime-layer manifest is
  generated; the signing thumbprint and timestamp URL belong to the runtime
  cache identity. Keep both PyInstaller `EXE` and `COLLECT` on `upx=False` so
  shipped PE layout never depends on a build-host UPX installation.
- `packaging/llama-cpp-polishing-runtime-lock-v1.json` and
  `scripts/prepare_local_polishing_runtime.py`: exact llama.cpp b10158 Windows
  Vulkan runtime, CPU fallback libraries, MIT notice, safe archive extraction,
  and complete staged-file verification. Bundle this one runtime under
  `backend/tools/local-polishing`; model repos may provide only catalog-pinned
  data artifacts and never executable code.
- `scripts/build_windows.ps1`: Windows installer orchestration. Official GitHub
  releases use `-ParallelizeIndependentBuilds`: frontend typecheck, Python
  sidecar preparation, and the Tauri `--no-bundle` app compile overlap. That
  compile uses a generated overlay whose `bundle.resources` and `bundle.externalBin`
  are JSON `[]`, so
  Tauri does not validate the not-yet-staged backend during compile; the final
  `tauri bundle` always waits for every producer and uses the original complete
  config, revalidating and packaging all resources. Rust audio uses the shared,
  restored Tauri Cargo target after backend preparation by default; Cargo's
  target lock bounds a rare overlap with the app compile and avoids a cold
  duplicate dependency build. `-RustAudioIsolatedTarget` remains an explicit
  diagnostic/local opt-in. NSIS, updater signing, and verification start only
  after every producer succeeds.
- Official `v*` tags are always fail-closed for Tauri updater signing.
  Authenticode is an additional opt-in controlled by
  `SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE=1`. When enabled, the workflow must
  import an RSA code-signing PFX only into the ephemeral runner store,
  configure Tauri with SHA-256 plus an HTTPS RFC 3161 timestamp, and validate
  the desktop, backend, NSIS installer, and timestamp/publisher evidence.
  Never call an updater-signed-only installer Authenticode-signed.
- `.github/workflows/release-windows.yml`: adaptive release DAG.
  Official tags additionally require a completed `release-qualification.yml`
  main dispatch for the identical SHA, created within 24 hours and completed
  before the tag run began. That read-only pre-tag workflow reuses the six
  quality/browser gates from a verified canonical main run for the identical SHA,
  allowing three minutes for delayed main-run discovery and waiting boundedly
  for pending work. Only missing or unusable source evidence
  starts its own six gates; known matching failures remain blockers. A successful
  `Reuse canonical main quality (<run>/<attempt>)` API job binds the selected
  source independently of diagnostic artifacts. The qualification contract,
  tag helper and pre-signing boundary revalidate that exact source and all six
  jobs; intentional skipped local gates are valid only with this binding.
  Create tags only through
  `scripts/ci/pre_tag_qualification.py create-tag` after it succeeds. Planning
  and the pre-signing boundary revalidate its run/attempt/provenance and jobs;
  missing, failed, pending, stale or post-tag evidence cannot fall back to local
  checks. Existing signing, quality, installer and publication gates remain mandatory.
  Qualification planning can wait up to 20 minutes for a matching pending main
  run. At tag time and before signing, pending evidence blocks the release.
  Release planning may reuse matching canonical main evidence with immutable
  workflow provenance within 24 hours. Missing evidence runs the release suite.
  A known failure for that revision blocks release.
  Cold producers and final-job dependency preparation start after planning.
  The packager accepts exact current-run artifact IDs after successful producer
  build/upload steps without waiting for optional cache export. Before assembly
  and signing, `release_quality_source.py` verifies all six required jobs and
  rechecks current-release and selected-source run/attempt/SHA identities.
  Failure, missing evidence, or skipped required checks block shipping.
  Official packaging omits its duplicate staged-app startup smoke. The mandatory
  downloaded-installer gate owns clean startup, upgrade, frontend, support,
  media, cleanup and uninstall evidence before publication; validate its report
  against the exact candidate and retain the clean-install result separately
  from the upgrade result. Standalone local builds retain staged startup smoke.
  Never reuse quality evidence across different SHAs without a separately
  implemented complete-input proof.
  The planner must probe with the same direct single-file Actions
  `hashFiles(...)` expressions used by producers and the final job; keep the
  finalizer's raw key-file SHA-256 outputs separate for cross-runner parity.
  The cold backend producer must restore and prune the same
  dependency/toolchain-keyed Rust state as the Tauri producer, keep a
  independent worker rebuild on the shared `Frontend\src-tauri\target`, and
  parallelize only the standalone diarization worker inside the sidecar build.
- `scripts/perf/profiles/installer-size/`, `scripts/perf/installer_size/`, and
  `scripts/run_installer_size_packet.ps1`: isolated installer-size
  AutoResearch profile. It freezes a hermetic Python/Node/Rust/NSIS toolchain,
  requires one fully gated bzip2 baseline before arming its fixed 43,200-second
  clock, evaluates one immutable hypothesis packet at a time, and retains
  inventory-, functional-, upgrade/uninstall-, YouTube-runtime-, and install-
  timing evidence. Promotion still requires two fresh final inventories plus
  the counterbalanced install-timing evidence. It is an unsigned local research
  workflow only; never treat its artifacts as signed release evidence. The
  byte-locked profile inputs, validators, legacy Deno wrapper, and current
  QuickJS build inputs are forced to LF by `.gitattributes`; preserve those
  rules so a Windows `core.autocrlf` checkout cannot invalidate raw-byte locks.
- `native/scriber-diarization-sidecar/`: isolated, statically linked
  Sherpa-ONNX worker; release preparation stages its attested EXE under backend
  `tools/diarization`. Its worker cache and pinned Sherpa archive cache remain
  separate from Tauri, audio-sidecar, and Python backend caches.
- `scripts/ffmpeg/build_profile_b_msys2.ps1`: Profile B custom ffmpeg build.
- `scripts/smoke_*.ps1` and `scripts/smoke_*.py`: installed app, desktop,
  frontend, media, and workflow gates.
  Tagged installer smoke must include `-SimulateUpgrade`, seed the retired
  `backend/app/src/gemini_transcribe.py` module, and verify that the actual
  NSIS overlay removes it before backend startup while preserving user data.
  Keep application-layer cleanup exact; never weaken manifest validation or
  recursively remove the application directory to accommodate stale files.
- `scripts/fixtures/installed-youtube-video-matrix-v1.json`,
  `scripts/run_installed_youtube_video_matrix.ps1`, and
  `scripts/installed_youtube_video_matrix.py`: install-once five-lane YouTube
  release gate, redacted lane evidence, and strict aggregation. Audio lanes
  remain fail-closed until the exact installed extractor runtime has produced a
  hash-bound pre-vetting artifact whose observed selector/format proves the
  expected lane. The captions-first lane additionally fetches and parses the
  selected bounded caption payload; public caption metadata is not sufficient.
  The installed run clears its bounded log window and must emit the lane's exact
  `youtube.captions.completed` or `pipeline.transcription.completed` event.
  Never use an unselected alternate format catalog or a test-only selector
  override as production-path evidence. Only `external-availability` receives
  one primary retry followed by one replacement attempt.
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
  Every physical meeting-client profile must additionally prove that the
  conference client's microphone and camera remain active while Scriber owns
  its shared-mode Mic/System capture; Scriber-source activity alone is not a
  coexistence check.
- Meeting validation areas stay atomic. Support-bundle privacy and automated
  regression evidence may be collected with
  `scripts/collect_meeting_support_bundle_evidence.py` and
  `scripts/collect_meeting_regression_evidence.py`; do not merge them with the
  held voiceprint corpus, EU legal/privacy approval, or signed-release profile.
  Collectors must emit redacted summaries, not raw logs, support ZIPs,
  transcripts, audio, personal paths, or credentials.
