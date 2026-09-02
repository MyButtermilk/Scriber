# Roadmap And Known Issues

Last verified: 2026-08-16

This document replaces old bug lists, code-review notes, and proposal journals.
It tracks current status only.

## Recently Completed

Meta Muse Voice Transcribe integration (2026-09-02): separate Realtime and Async
choices use the same `muse-voice-transcribe-1.0` model and Meta Model API key.
Async means one non-blocking HTTP upload, not a separate Meta model or hosted
background job. Current constraints: ten minutes per file/Meeting final,
60 minutes per realtime session, no automatic splitting/resume, and no word
timestamps. HTTP SSE is available from Meta but not used by Scriber; realtime
previews use the WebSocket endpoint. Protocol tests are local; production
account access and installed microphone smoke still require verification.

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
- Async/finalizing live mic providers use Pipecat Silero VAD plus RMS silence
  gating to skip expensive provider finalization for silent recordings.
- Audio-level visualization is throttled and frontend waveform uses Canvas/RAF;
  the recording overlay and live mic visualizers avoid React state updates for
  hot audio-level animation where practical.
- The native recording overlay WebView is created lazily on first show instead
  of at app startup.
- Live Mic UI state updates correctly after session finish in the current branch.
- Live Mic keeps the completed transcript and elapsed duration visible after
  finalization, exposes explicit starting/stopping/offline states, and bounds
  the live transcript viewport so long dictation does not grow the page without
  limit.

YouTube/file:

- Thumbnail handling was fixed and covered by browser smoke.
- File tab drag/drop was fixed and covered by browser smoke.
- YouTube job progress now advances beyond download completion through upload,
  transcription, summary, and done states.
- Azure MAI file/live preparation uses MP3 for latency rather than WAV.
- YouTube input is restricted to validated YouTube URLs, API/thumbnail responses
  are bounded, redirect targets are revalidated, and canceled library downloads
  use isolated attempt directories so late workers cannot corrupt retries.
- YouTube extraction pins `yt-dlp[default]==2026.8.19`, bundles matching EJS
  scripts plus a manifest-bound QuickJS-ng 0.15.0 wrapper/engine quartet, leaves
  player-client selection to yt-dlp, and validates audio-stream and container
  integrity with ffprobe before provider upload. Frozen builds never resolve a
  raw QuickJS binary from `PATH` and import yt-dlp from the frozen package
  instead of shipping a build-machine-bound pip launcher; in-place upgrades
  delete the exact superseded Deno executable. Corrupted/incomplete transfers
  are retryable download failures rather than successful downloads that later
  fail inside Azure MAI.
- YouTube jobs now prefer manual or automatic caption tracks before downloading
  audio. The default-on preference is stored in runtime settings across installer
  upgrades, and missing or unreadable captions fall back to the audio path.
- Recent videos treats a pending automatic summary as processing, so Ready is
  shown only after summary completion. Live Mic history uses transcript excerpts
  and stable, non-overlapping time sections without layout-motion gaps.
- Live Mic, YouTube, and File now share a responsive history toolbar with exact
  result counts. YouTube separates loading, no-results, and failure states and
  stacks result/history cards at narrow widths. File exposes a clear browse CTA,
  inline rejected-file feedback, compact provider limits, and a two-line mobile
  processing queue.

Reliability and data:

- Job resume/retry scheduling is single-flight, transcript/job deletion is
  coordinated with persistence, and runtime caches/stores have bounded retention.
- Provider adapters now discard response bodies at their error boundary and
  expose only allowlisted status/code categories. Mistral realtime transcription
  has a direct `run_stt` regression test, and Gladia cleanup diagnostics no
  longer fail on a missing logger.
- Ruff lint and formatting cover all maintained Python in `src`, `tests`, and
  `scripts`; frontend ESLint includes the React Hooks rules, and the first
  Testing Library component suite covers Meeting-note autosave behavior.
- Issue #18 is implemented and measured: canonical
  activation/stop-to-visible-text KPIs, exact provider/model/container/codec
  route evidence, pass-through-first media preparation, one shared provider
  HTTP pool, and fail-closed suppression of ambiguous duplicate billable
  retries. Installed replay now arms without starting work and admits a sample
  only from the exact one-shot native hotkey or primary-button QPC marker. Two
  valid 60-sample installed matrices at 5, 15, 30, and 60 seconds exercised
  Azure capture-time MP3, Speechmatics Rust WAV, and the Soniox control. All
  exact-text and cleanup checks passed, but both candidates had canonical
  duration regressions, so both experimental defaults remain off.
  Caption-first YouTube jobs now distinguish their planned audio fallback from
  the selected and executed route.
- Settings updates validate persisted text sizes before mutation; invalid numeric
  tuning values fall back safely instead of crashing provider or runtime paths.
- Unicode export filenames use a safe ASCII fallback plus RFC 5987 UTF-8 metadata.

Local transcript polishing:

- The backend has exactly one active identity: `qad_q4_0`, the LFM2.5 350M
  Production QAD-Q4_0 GGUF. Its production GGUF, schema-3 LFM policy, manifest,
  and license files are public at immutable Hugging Face revision
  `d64f8a14a09b2916000d969edd18bc411745e53a` and install anonymously on demand.
  Retired Gemma Q8_0/BF16 revisions are legacy-removal and rollback identities
  only, not selectable models; no alternative quantization is authorized or
  compared.
- The selected replacement passed the bound 200 Long plus 400 Short regression
  cases exactly. The earlier real Windows 0-of-5 result remains historical
  failure evidence and is not the shipping artifact. On 2026-09-01 the owner
  explicitly confirmed aggregate annual revenue of USD 0 including affiliates,
  so the PRAXIST free-license revenue gate is passed. Third-party availability
  retains `Praxist by Sapient Intelligence`.
- A 640-step V2 hard-case continuation completed successfully, but its compact
  promotion evidence failed closed. Across 300 sealed automatic cases, V1/V2
  normalized exact match was `0.546667`/`0.530000`, deterministic critical
  errors were `400`/`407`, and structured-output parse rate was `0.88`/`0.85`.
  In a separate 100-pair blinded Terra session, V2 won 21, V1 won 15, 64 tied,
  and critical markings were 24 for V2 versus 21 for V1. The evidence is a
  `compact_smoke_test`, not publication-grade evidence.
- V2 is intentionally not quantized, published, or activated. Its ignored,
  hash-bound local verdict and evaluation artifacts document the failure cases
  for a later synthetic hard-case round. Future training must derive only new
  synthetic siblings from aggregate signals and must never reuse sealed test,
  challenge, prediction, or judge content directly.

Meetings:

- The eager Meetings tab now owns a durable capture-to-analysis state machine,
  recovery, canonical/live revisions, notes, editable action items, cited chat,
  exports, playback, retention, and webhook delivery surfaces.
- Meeting delivery was the first domain extracted from `create_app`; Runtime,
  ONNX model management, YouTube, Transcript, Settings, Local Polishing,
  Device, Outlook Calendar, Meeting Import, Voice Component, File
  Transcription, WebSocket, Live Mic start/stop/toggle lifecycle, Meeting
  Capture start/pause/resume/stop, Meeting Workspace
  title/search/correction/note/action-item routes, and Meeting
  Processing reprocess/finalize/retry/analyze routes, Meeting Artifacts
  playback/export/email routes, Meeting Catalog list/detail/discard routes, and
  Meeting Device Readiness capabilities/audio-device/device-test routes
  now follow the same module-level
  registration shape with domain-local ports or collaborator interfaces.
  Transcript routes were the first to need controller internals; those became
  public methods shaped by the routes' needs rather than a wider port.
  Webhook targets are HTTPS-only, DNS-validated and socket-pinned; confirmation
  tokens are single-use and durable so retries cannot duplicate a delivery.
  Meeting notes use a serialized, coalescing save lane with retry and
  page-teardown flushing.
- Native meeting capture uses one Rust audio sidecar for mic plus loopback,
  pinned `aec3-rs` echo cancellation, a shared monotonic timeline, three durable
  tracks, health monitoring, pause/resume gaps, and checksum-validated chunks.
- Pause, stop, cleanup, and device reconnect now arm the recorder before native
  pipes close, so Windows `OSError` disconnects commit the valid partial chunk
  and resume advances to a collision-free sequence. Unexpected failures remain
  capture-watchdog errors.
- Final transcription uses provider-native timestamps for Soniox, AssemblyAI,
  Deepgram, and Mistral when available. Providers without structured timing use
  an explicitly identifiable estimated fallback. One silent microphone/system
  track no longer discards speech from the other track; all-silent input and
  provider/normalization failures still fail finalization.
- Outlook Calendar has public-desktop PKCE, Windows Credential Manager refresh
  token storage, atomic paginated Graph sync, periodic refresh, and offline
  backoff. The default calendar keeps its delta cursor; additional and
  accessible shared calendars are enumerated and refreshed over the same
  bounded event window. Settings persists one calendar selection, defaults it
  to the personal default calendar, and daily/current/next-event reads expose
  only that selection. Its daily view uses browser-computed DST-correct UTC
  boundaries and exposes all events with organizer and attendee names/addresses.
  `/me` preserves both mail and UPN aliases for self identification. Settings
  exposes configuration state, connect, sync, verified disconnect, and last
  sync; Meetings can refresh, choose one event or no event, inspect its details,
  and freeze the selection at Start. Official tag builds fail before expensive
  setup when the public Entra client ID is absent or invalid.
- Optional WeSpeaker embeddings are local, opt-in, hash-pinned, and excluded
  from exports/support bundles. Settings can record a short named sample through
  Rust/WASAPI; PCM stays bounded in memory, is never saved or uploaded, and only
  its normalized local enrollment centroid remains. Settings lists profiles and
  allows users to add, name, or delete them; confident linked Meeting speakers
  are updated through the backend profile API. Merge and incorrect-match split
  remain available in the Meeting workspace.
- The Meeting start check consumes provider profiles, safe capture/render
  endpoint inventory, and dismissible local detection. Interrupted meetings can
  either finalize saved chunks or resume fresh capture.
- The start check can run an explicit 1.5-second mic/loopback/AEC route test;
  it returns only level/activity statistics and never persists or uploads audio.
- Durable post-meeting progress, Overview and Notes views, full-mix timestamp
  and five-to-eight-second speaker playback, document/data exports, compressed Opus audio sharing, and
  preview-confirmed webhook delivery are exposed in the workspace. Outlook EML
  drafts preserve the selected PDF/DOCX/Markdown MIME attachment and use the
  transcript/analysis language for their subject, body, and document labels.
  Desktop exports use native Save As; long audio is streamed instead of copied
  through the WebView, and Open file/Open folder resolve only the bounded opaque
  token returned by that save. Browser builds download normally.
- The pre-React boot shell resolves the stored/system theme synchronously and
  uses the high-contrast dark Scriber mark before the application bundle mounts;
  the real-browser smoke freezes and screenshots this exact dark startup frame.
- Independent Soniox live streams now supervise send/receive failures, reconnect
  with bounded exponential backoff, emit one preview-gap marker per outage, and
  report reconnect/recovery state visibly while durable local capture continues.
- Soniox realtime speaker diarization is active for system audio and shared
  microphones, and final live provider turns preserve every contiguous speaker
  run instead of assigning the whole turn to its majority speaker. The first
  realtime microphone identity remains `You`; additional in-room speakers keep
  anonymous labels. Final batch transcription and the optional local fallback
  separate both canonical tracks, collapsing a microphone result to `You` only
  when exactly one speaker was found.
- Durable recorder errors are watchdog inputs; simulated disk-full preserves
  completed chunks, rejects the incomplete chunk, and stops capture visibly.
- The Meeting pipeline now has an explicit five-hour target (18,000 seconds):
  schema-v3 30-second base/delta checkpoints keep transcript recovery storage
  linear, finalizer leases and provider timeouts survive long jobs, and
  post-meeting analysis uses cached bounded map/reduce rather than one oversized
  prompt.
- Preflight reports six-GiB storage readiness plus estimated capture capacity;
  long transcript rows render start, end, and duration with `H:MM:SS` offsets
  and retain direct click-to-seek playback.
- Real Meeting release evidence now has a versioned validator and a guided
  Windows runner. It binds every completed scenario to one installer SHA-256,
  verifies relative artifact hashes, rejects sensitive report fields, enforces
  scenario-specific thresholds, and can be required by the aggregate hybrid
  readiness gate. Generated drafts are intentionally non-passing.

Debug/support:

- Debug console has severity colors, filters, sticky controls, newest-first
  default, today filter, clear-view, clear-log, copy-visible, refresh, and
  support-bundle download.
- Support bundles are token-protected and redacted.
- Installed support-bundle smoke now gates native device-event diagnostics,
  Rust audio fallback-circuit diagnostics, and structural absence of Meeting
  audio, transcript stores, Outlook credentials, webhook secrets, and
  voiceprint/embedding artifacts.

Packaging/performance:

- Profile B ffmpeg is the default Windows media-tool profile.
- Live-meeting PCM16 RMS checks share the native `audioop-lts` implementation
  instead of per-sample Python loops. Provider wrappers are imported only by the
  selected pipeline branch, and transcript timing uses prefix sums.
- Release dependency resolution is constrained by an exact Windows CPython 3.14.7
  graph. Profile-B restoration is bound to a versioned artifact identity, and
  tag releases remain drafts until downloaded assets, updater signatures,
  Authenticode evidence, installed smoke, and uninstall gates all pass.
- The Python 3.14.7 product runtime is official O0 with JIT disabled. Final
  source-attested AMD screening rejected Clang/PGO C0: startup p50 improved only
  3.56%, App UX p50 regressed 5.69%, and 11 of 48 provider p50/p95 series
  exceeded the protected 3% regression limit. It also rejected Clang/PGO/Tail
  T0: startup p50 improved 4.32% but stayed below the 5% floor, App UX p50
  regressed 9.69%, and five protected provider values regressed, with a 27.93%
  worst case. Stock PyInstaller 6.20 ignores `PYTHON_JIT` in its isolated
  interpreter, so O1/C1/T1 fail closed and require a separate launcher research
  goal rather than a release workaround.
- The latest local unsigned `v0.4.35` LZMA installer is `124.77 MiB`, SHA-256
  `62a141b5f805ae0a61c2ab555b89fd489f6415293854af23601983ddb18a6af8`; its
  installed package smoke measured `320.00 MiB` and passed frontend ownership,
  runtime health, crash recovery, controlled shutdown, support-bundle privacy,
  installed media preparation, synthetic Meeting Mic/System/AEC capture,
  stability, optional-model absence, uninstall, and data preservation.
- Profile B now gates the exact Meeting finalization formats: three FLAC tracks
  in Matroska plus an `amix`-generated Ogg/Opus playback file.
- SciPy is absent from the standard sidecar.
- AWS Transcribe and AWS SDK packages are absent from the standard sidecar.
- Sidecar reuse cache reduces repeated local installer build time.
- Installed stability smokes include role-based process-tree metrics for
  Tauri shell, backend, WebView2, audio sidecar, and other child processes.

Docs:

- Permanent docs were consolidated into README, AGENTS, and four category docs.

## Resolved Bug Audit (2026-07-12)

The 19 defects found by the 2026-07-12 audit have been corrected in the current
working tree. The original reproduction, root cause, and regression boundary are
retained below as durable engineering context. Focused Python, Rust, TypeScript,
PowerShell-parse, media-command, and source-integrity gates now cover the fixed
paths; installed-app and physical-device evidence remains part of release QA.

### `BUG-MTG-001` - Resolved P0 - Packaged Profile-B FFmpeg cannot finalize Meeting playback

- **Reproduction:** Run
  `Frontend\src-tauri\target\release\backend\tools\ffmpeg\ffmpeg.exe -hide_banner -h filter=adelay`.
  The currently staged release binary reports `Unknown filter 'adelay'`.
  Passing any Meeting playback command built by `meeting_opus_playback_args`
  fails before producing the Opus asset.
- **Root cause:** `src/runtime/ffmpeg_commands.py::meeting_opus_playback_args`
  applies `adelay` to every input, including a track with origin zero, while
  `scripts/ffmpeg/create_profile_b_build_kit.py` does not enable the filter and
  `scripts/ffmpeg/validate_ffmpeg_profile.py` does not require it. The
  consolidation loop in `src/meeting_finalizer.py::_consolidate_audio_assets`
  treats the missing playback output as a finalization failure.
- **Build-cache exposure:** the Profile-B reuse path in
  `scripts/build_tauri_backend_sidecar.ps1` accepts an older `ok` report and
  executable without rerunning the newer playback fixture against that exact
  binary.
- **Fix boundary:** enable and validate `adelay`; invalidate the Profile-B cache;
  never accept a reused media binary until the current fixture set has run on
  it.
- **Required regression gate:** exercise lossless archive, mixed playback,
  microphone playback, and system playback using the exact packaged
  `ffmpeg.exe`, including non-zero Meeting-clock origins.

### `BUG-MTG-002` - Resolved P1 - A repeated upload can destroy an accepted Meeting import

- **Reproduction:** create an import, upload it until it reaches `received` (or
  later), then repeat the same `PUT /api/meeting-imports/{id}/content`. A second
  controller using the same SQLite database exposes the same race during a
  concurrent upload.
- **Root cause:** the active-upload guard in
  `src/api/meeting_import_routes.py::upload_import` is process-local. When
  `begin_receiving` rejects the second request, the conflict handler calls
  `MeetingImportStore.mark_failed`; that method may transition any nonterminal
  state through `finalizing` to `failed`. The handler can then remove the first
  worker's import directory.
- **Fix boundary:** a request that did not win the durable `created -> receiving`
  CAS must be observational only. It must never mark the job failed or remove
  files owned by the winning generation.
- **Required regression gate:** duplicate sequential PUT and two-store/two-
  controller parallel PUT tests must preserve the first upload's state, hash,
  byte count, and committed file.

### `BUG-MTG-003` - Resolved P1 - Recovery may bind an old transcript to changed audio

- **Reproduction:** persist a track stage result for multiple valid chunks,
  corrupt a later chunk, then retry finalization after another track caused the
  first attempt to stop. The corrupt chunk is quarantined and a shorter lossless
  track is built, but the old transcript result is reused.
- **Root cause:** `src/meeting_finalizer.py::_run_impl` indexes recovered
  `TrackStageResult` values only by `source_track`. Neither the
  `transcription_track_stage_results` schema nor its immutable result digest in
  `src/data/transcript_artifact_store.py` binds the result to the prepared
  track's PCM hash, sample count, duration, or manifest.
- **Fix boundary:** bind every per-track provider result and local derivation to
  a verified audio identity. A mismatch must supersede/retranscribe the result
  or stop with a durable corruption error; it must never canonicalize stale
  text.
- **Required regression gate:** mutate or quarantine audio after a partial
  attempt and assert that an old provider result cannot become the canonical
  head.

### `BUG-MTG-004` - Resolved P1 privacy - Crash recovery bypasses audio retention indefinitely

- **Reproduction:** leave a recording with retained chunks and a positive
  `audio_retention_days` value, restart the backend, and advance time beyond the
  retention period. The audio never becomes eligible for purge.
- **Root cause:** `MeetingStore.recover_interrupted` changes open Meetings to
  `interrupted` but does not set `ended_at`. `MeetingStore.expired_audio_meetings`
  requires `ended_at IS NOT NULL`, so the recovered Meeting is excluded forever.
- **Fix boundary:** establish a conservative durable end time during recovery or
  define an equivalent retention anchor for interrupted capture.
- **Required regression gate:** recover an old recording with chunks, run the
  retention query after its deadline, and assert purge selection plus tombstone
  completion.

### `BUG-MTG-005` - Resolved P1 privacy - Unhealthy-backend recovery leaves native audio sidecars running

- **Reproduction:** start a Meeting, keep the backend process alive but make
  `/api/health` fail past `SCRIBER_BACKEND_UNHEALTHY_TIMEOUT_MS`. The supervisor
  restarts the backend without first draining the registered Mic/System/AEC
  sidecars.
- **Root cause:** the timed-out health path in
  `Frontend/src-tauri/src/lib.rs::BackendManager::ensure_started` calls
  `terminate_managed_child` directly. Manual restart and shell exit call
  `shutdown_all_audio_sidecars`, but automatic unhealthy recovery does not. The
  Python shutdown path stops consumers/recorders, not the shell-owned producer
  processes.
- **Fix boundary:** every managed-backend replacement must use one ordered
  shutdown boundary: native audio sidecars, authenticated backend cleanup,
  backend termination, then replacement launch.
- **Required regression gate:** simulate an alive-but-unhealthy backend with an
  active sidecar and assert sidecar shutdown completes before the old PID is
  terminated and a new PID starts.

### `BUG-MTG-006` - Resolved P1 - WASAPI loopback passes a stream flag as periodicity

- **Reproduction:** start system-loopback capture on an endpoint that strictly
  validates shared-mode `IAudioClient.Initialize` arguments. Initialization can
  fail with `E_INVALIDARG`, preventing Meeting System audio from starting.
- **Root cause:** `Frontend/src-tauri/src/audio_sidecar.rs` correctly supplies
  `AUDCLNT_STREAMFLAGS_LOOPBACK` as `StreamFlags`, but supplies the same value a
  second time as `hnsPeriodicity`. Shared-mode periodicity must be zero.
- **Fix boundary:** centralize initialization argument construction so capture
  kind changes flags only, not the periodicity field.
- **Required regression gate:** unit-test the complete argument tuple and add a
  physical default-render loopback smoke; synthetic capture cannot cover this
  COM contract.

### `BUG-MTG-007` - Resolved P1 - Mic and System tracks are relabelled as sharing a clock when they do not

- **Reproduction:** feed two controlled upstream frame pipes whose first frames
  differ by 250 ms. The Meeting relay emits them with the same new timestamp
  instead of preserving the gap.
- **Root cause:** Mic and System WASAPI sessions start sequentially and each
  writer timestamps against its own `Instant`. The relay in
  `Frontend/src-tauri/src/audio_sidecar.rs` ignores both input timestamps, reads
  one frame from each pipe, and stamps the pair using a third relay-local
  `Instant`, while startup metadata claims `windowsQueryPerformanceCounter`.
- **Impact:** start skew and device drift are silently converted into apparent
  simultaneity, degrading AEC reference alignment, transcript timing, and
  seekable track alignment.
- **Fix boundary:** establish one QPC-based origin carried through both capture
  sessions and explicitly pad, drop, or resample for skew/drift.
- **Required regression gate:** controlled source origins plus drift must prove
  alignment/resync behavior; merely asserting equal relay timestamps is not a
  valid test.

### `BUG-MTG-008` - Resolved P1 release integrity - Sidecar reuse trusts cache keys but not executable bytes

- **Reproduction:** complete one sidecar build, truncate or alter
  `scriber-backend.exe` or `scriber-audio-sidecar.exe`, retain its metadata and
  cache key, then rerun the reuse build. The target-current/audio-cache paths can
  accept the damaged binary and skip runtime/self-tests.
- **Root cause:** `Test-SidecarTargetCurrent` and the Rust-audio cache-hit path in
  `scripts/build_tauri_backend_sidecar.ps1` verify presence and cache keys but
  not the recorded SHA-256 and length. The diarization worker already performs
  the stronger identity check.
- **Fix boundary:** make executable digest/length part of every cache manifest
  and target-current decision; run the appropriate self-test/import gate on the
  exact bytes that will be packaged.
- **Required regression gate:** tampered backend and audio-sidecar binaries with
  otherwise valid metadata must force a rebuild or fail closed.

### `BUG-MTG-009` - Resolved P1 privacy - Outlook Disconnect can report success while retaining the token

- **Reproduction:** make private Shell IPC return `success: false` for
  `outlookCredentialDelete`, then call `DELETE /api/calendar/outlook`. The API
  still returns `disconnected: true`; the next status can report connected and
  refresh-token acquisition remains possible.
- **Root cause:** `OutlookCalendarService.disconnect` ignores the Shell IPC
  result and clears local events unconditionally; `src/web_api.py::outlook_disconnect`
  returns unconditional success.
- **Fix boundary:** only claim disconnection after verified credential removal;
  define explicit, recoverable behavior for the local event cache when removal
  fails.
- **Required regression gate:** Credential Manager deletion failure must produce
  a non-success response and must never claim the account is disconnected.

### `BUG-MTG-010` - Resolved P1 - The Outlook OAuth lifecycle is not observable from Settings

- **Reproduction:** begin Connect in Settings, finish Microsoft authorization in
  the external browser, and return to Scriber. Settings can remain
  `Disconnected` until remount/reload. A second Connect click can invalidate the
  first callback's state. If token exchange succeeds but the initial Graph sync
  fails, the callback instead says the whole connection failed even though the
  refresh token is already stored.
- **Root cause:** Settings invalidates the status query immediately after the
  `202` connect response, before the callback. Its query has no polling or
  callback/WS signal, while global Query defaults disable focus refetch and use
  infinite stale time. `OutlookCalendarService.begin_connect` clears all pending
  states, and `outlook_callback` combines token exchange and first sync in one
  success/failure block.
- **Fix boundary:** model `idle -> authorizing -> connected -> syncing` explicitly,
  preserve the active state until terminal callback/timeout, and separate
  authentication success from first-sync health.
- **Required regression gate:** delayed callback, repeated click, user cancel,
  successful token plus failed first sync, and app-focus return must all update
  Settings without a route remount.

### `BUG-MTG-011` - Resolved P1 - Outlook delta sync is permanently bound to its first 30-day window

- **Reproduction:** complete the initial sync, advance time beyond the persisted
  `window_end`, add a future event beyond the original range, and sync again.
  The event never enters the local cache.
- **Root cause:** `OutlookCalendarService.sync` reuses any stored `delta_link`
  forever. It persists `window_start`/`window_end` but never reads them to roll
  the `calendarView/delta` window forward.
- **Fix boundary:** expire/reseed the delta cursor before the active horizon
  ages out and reconcile events that leave the old window.
- **Required regression gate:** clock-controlled initial/delta pagination across
  a window rollover must issue a new bounded Graph query and expose the new next
  Meeting.

### `BUG-MTG-012` - Resolved P1 - Outlook UTC event times are stored without timezone identity

- **Reproduction:** ingest a Graph `DateTimeTimeZone` value such as
  `dateTime=2026-07-12T09:00:00.0000000, timeZone=UTC` in Europe/Berlin. The UI's
  `new Date(value)` treats the offset-free value as local 09:00 rather than UTC
  09:00 and displays the event two hours early in summer.
- **Root cause:** sync requests `Prefer: outlook.timezone="UTC"` but stores only
  `dateTime`, discarding `timeZone`. Settings and Meetings parse the resulting
  offset-free string directly; backend event selection also compares the raw
  strings to offset-bearing UTC ISO strings.
- **Fix boundary:** normalize every Graph `DateTimeTimeZone` to a canonical UTC
  instant at ingestion and query/order by a temporal representation rather than
  mixed ISO string forms.
- **Required regression gate:** UTC, local-zone, and DST-boundary fixtures must
  prove correct next/current-event selection and frontend display.

### `BUG-MTG-013` - Resolved P1 identity - Separating a false Voice match preserves the false name

- **Reproduction:** auto-match a Meeting speaker to named profile `Alice`, then
  invoke `split_speaker_profile`. The `profileId` changes, but the Meeting
  speaker display name and all segment labels remain `Alice`.
- **Root cause:** `MeetingStore.split_speaker_profile` reads and updates only the
  profile link/confidence. It neither restores `meeting_speakers.display_name`
  from the anonymous base label nor rewrites the linked
  `meeting_segments.speaker_label` values. The existing test asserts only the
  profile/observation move.
- **Fix boundary:** distinguish user-entered names from profile-derived names and
  atomically remove only the derived identity when splitting.
- **Required regression gate:** split an auto-named match and assert both speaker
  UI data and canonical segment labels revert, while a manual rename is
  preserved.

### `BUG-MTG-014` - Resolved P1 data isolation - Meeting-local chat and search state crosses routes

- **Reproduction:** ask a question or set Transcript search in Meeting A, then
  open Meeting B. A's answer/citations and the old filter remain visible. If A's
  chat response completes after navigation, it is rendered under B.
- **Root cause:** `chatQuestion`, `chatAnswer`, and `transcriptSearch` in
  `Frontend/client/src/pages/Meetings.tsx` are page-global state. The
  `selectedId` reset effect does not clear or scope them, and `chatMutation`
  accepts a late response without comparing its request Meeting id.
- **Fix boundary:** key chat/search state by Meeting id or carry the id with each
  result, clear it on selection change, and ignore/route late responses.
- **Required regression gate:** A-to-B navigation after a settled response and
  during a delayed response must expose no A question, answer, citation, or
  search filter in B.

### `BUG-MTG-015` - Resolved P1 data loss - Notes cannot be cleared and edits can vanish on navigation

- **Reproduction A:** delete all text from an existing workspace note and wait;
  no request is sent and reload restores the old note. **Reproduction B:** type a
  change and navigate to another Meeting inside the 700-ms debounce; cleanup
  cancels the timer without flushing the edit.
- **Root cause:** the frontend autosave effect rejects an empty trimmed body and
  only returns a timer cleanup. `MeetingStore.put_note` also rejects an empty
  body, and no workspace-note delete contract exists.
- **Fix boundary:** define empty text as durable workspace-note deletion (or a
  valid empty value) and flush/commit the correct Meeting id before navigation,
  without allowing a late A save to mutate B's cache.
- **Required regression gate:** fake-timer clear, route-change-before-debounce,
  and delayed-response A-to-B tests must prove durable, correctly scoped notes.

### `BUG-MTG-016` - Resolved P2 - Meeting playback can expose missing or partial audio

- **Reproduction:** open a Meeting after audio retention has purged its assets,
  a legacy record with only an isolated track, or click a system/microphone
  transcript segment. The old controls could expose a missing route or isolate
  one side, so the user no longer heard the complete conversation.
- **Root cause:** Meetings gated playback on any audio derivative and routed
  segment/sample clicks through their source track. Mic/System mute controls
  made that partial state persistent.
- **Fix boundary:** user-facing Meeting playback now requires `playback_mix` and
  always uses its authenticated mix endpoint. Track toggles are removed;
  timestamps, citations, and speaker examples retain the full conversation.
  Speaker examples use a clamped five-to-eight-second window and are disabled
  when the retained mix itself is shorter than five seconds. Purged meetings
  keep their explicit audio-unavailable state.
- **Regression gate:** source tests reject isolated fallback routes and old mute
  state, unit tests cover sample windows at both audio edges, and the browser
  Meeting flow checks the mix URL for both timestamp and speaker-sample clicks.

### `BUG-MTG-017` - Resolved P2 safety - Irreversible Voice Library deletion has no confirmation

- **Reproduction:** click `Delete library` once in Settings. The frontend
  immediately sends DELETE; the backend removes every voice profile plus the
  optional model and disables the opt-in. Individual profile trash buttons are
  likewise one-click destructive.
- **Root cause:** these handlers bypass the confirmation pattern already used by
  Meeting deletion, and the whole-library handler has no explicit in-flight
  state to block repeated clicks.
- **Fix boundary:** require an accessible destructive confirmation that states
  its scope; disable all related controls while the request is pending.
- **Required regression gate:** first click and Cancel send no DELETE; explicit
  confirmation sends exactly one request and locks repeated destructive input.

### `BUG-MTG-018` - Resolved P2 - Newly learned Voice profiles can remain invisible indefinitely

- **Reproduction:** load the Voice-profile query, finalize a Voice-Library
  Meeting that creates a new profile, then open Settings. The old cached list can
  remain visible until an explicit profile mutation or application reload.
- **Root cause:** Meeting terminal WebSocket handling invalidates Meeting detail
  but not `/api/meetings/speaker-profiles`. No profile event is emitted. The
  Settings observer inherits the global infinite stale-time behavior and has no
  polling/refetch override.
- **Fix boundary:** publish/invalidate a versioned profile update whenever
  finalization changes the library; Settings must refresh from that signal.
- **Required regression gate:** cache an empty list, complete profile creation,
  navigate to Settings, and assert the new profile appears without reload.

### `BUG-MTG-019` - Resolved P2 - Import modal can stay permanently busy after a missed terminal event

- **Reproduction:** complete the upload, disconnect WebSocket before the import
  emits its Meeting id, then reconnect. The durable import list refreshes, but
  the open modal remains `Importing...` and cannot be dismissed normally.
- **Root cause:** successful PUT leaves `meetingImportId` set; only the matching
  `meeting_import_progress` WebSocket handler clears it. Query invalidation on
  reconnect does not reconcile that local id with the server-authoritative
  import record returned by GET/list.
- **Fix boundary:** reconcile active local import state from REST after PUT,
  reconnect, visibility change, and timeout. WebSocket progress must be an
  accelerator, not the sole terminal signal.
- **Required regression gate:** drop the terminal WS event, return completed,
  failed, canceled, and committed records from REST, and assert modal recovery
  plus correct Meeting navigation.

### `BUG-MTG-020` - Resolved P1 - Repeated Outlook Connect creates competing PKCE flows

- **Reproduction:** click Connect or Continue more than once before returning
  from Microsoft. Each click creates a new state; completing one browser tab can
  leave another state pending until its ten-minute timeout.
- **Fix:** reuse the one unclaimed PKCE state and reconstruct its S256 challenge
  when reopening Microsoft. A state already being exchanged cannot be replaced.
- **Regression gate:** two Connect calls return the same state/URL and one
  successful callback clears authorization-pending.

### `BUG-MTG-021` - Resolved P1 - A revoked Outlook token still appears connected

- **Reproduction:** revoke Microsoft consent while the refresh token remains in
  Credential Manager, then synchronize. Settings continues to report Connected.
- **Fix:** classify refresh `invalid_grant`/`interaction_required`, corrupt stored
  credentials, and a repeated Graph 401 as `reauthRequired`; clear only the
  in-memory access token, preserve cached events, and offer Reconnect.
- **Regression gate:** status reports `connected=false` and
  `reauthRequired=true` without deleting the last account/event snapshot; a new
  successful PKCE flow clears the state.

### `BUG-MTG-022` - Resolved P1 - Calendar context can choose the wrong nearby event

- **Reproduction:** an all-day entry or recently ended event overlaps an active
  call. Earliest-start sorting selects it instead of the active Meeting.
- **Fix:** rank active non-all-day events first, then upcoming, recently ended,
  and finally all-day context, with deterministic tie-breakers.
- **Regression gate:** one fixture removes each category in turn and proves the
  expected fallback order.

### `BUG-MTG-023` - Resolved P1 cost/UX - Confirming one speaker discards other AI suggestions

- **Reproduction:** request paid AI suggestions for several unresolved speakers,
  then confirm the first one. Refetching the local-only assignment endpoint
  drops every remaining AI suggestion.
- **Fix:** patch only the confirmed assignment in the React Query cache and keep
  the other ephemeral suggestions unchanged.
- **Regression gate:** confirmation is immutable, updates exactly one speaker,
  and preserves the second LLM proposal without another provider request.

### `BUG-MTG-024` - Resolved P2 - Outlook refresh can silently unlink a Meeting

- **Reproduction:** select an event, move or cancel it in Outlook, then refresh.
  The id disappears silently while the copied title still looks linked; cached
  results also provide no durable freshness/error cue.
- **Fix:** show last refresh and cached-sync failure, clear the missing link with
  an explicit warning, preserve intentional title edits, and require a new
  event choice before calendar participants are attached.
- **Regression gate:** event removal yields a visible warning and null event id;
  selecting another event clears the warning, and stale cached data remains
  clearly labelled.

## Current Highest Priorities

### Meeting import and diarization architecture freeze

The following boundaries are mandatory before the optional local speaker path
is release-promoted:

- Do not link Sherpa-ONNX into `scriber-audio-sidecar` or the Tauri shell. Build
  a separate, statically linked `scriber-diarization-sidecar` from the pinned
  Sherpa-ONNX Rust API. Ship its executable through the signed Scriber
  installer/updater; keep only Pyannote/3D-Speaker models plus licenses as the
  optional post-install component. The worker has its own version,
  `--self-test`, schema-versioned JSON stdin/stdout contract, bounded runtime,
  and no transcript text in logs. A worker crash or OOM must not affect live
  capture, Live Mic, or the backend process.
- Publish the Pyannote/3D-Speaker models and licenses as one transactional,
  SHA-256-pinned component manifest. Installation uses a staging directory and
  one atomic rename. Normal status reads use cached file identity/manifest
  checks; full model hashing runs off the event loop during install, explicit
  verification, and first worker start only. Never download an executable from
  this model-component channel.
- Release packaging hook is implemented: `build_tauri_backend_sidecar.ps1`
  builds the locked native crate, verifies the pinned Sherpa static archive,
  copies only the EXE plus adjacent attestation under backend
  `tools/diarization`, and records their hashes/sizes in build metadata. CI has
  separate worker and archive caches, and staged plus installed smokes verify
  the manifest, static identity, self-test, and absence of optional models.
  Release promotion still requires a signed installer run carrying this
  evidence; frozen runtime never falls back to an unpinned remote binary.
- Replace the single long-running multipart request with a durable two-phase
  import job: create/import id, streamed `.part` upload, fsync plus atomic source
  commit, media probe/preparation, Meeting commit, then normal finalization.
  Progress and cancellation are server-authoritative. `DELETE` on an import id
  must set a durable cancel request, terminate ffmpeg/worker/provider work where
  possible, await task exit, and only then remove staged or Meeting files.
- The collection read and compact Pending Imports UI are implemented.
  `GET /api/meeting-imports` is server-authoritative after WebView/app restart,
  prioritizes active work, bounds recent failed/canceled history, and exposes
  only cancel-before-commit, safe Meeting retry, and Meeting-link actions.
  Ambiguous upload network failures preserve the durable job; only an explicit
  user cancellation calls `DELETE`.
- Import cancellation is accepted only through `waiting_for_workspace`.
  `committing` is the durable ownership handoff to the Meeting workspace; later
  import DELETE requests return `409` and the `meetingId`. Meeting deletion has
  a strict ownership barrier: while `_run_meeting_finalization` still owns its
  files, discard returns `409`. Do not claim cooperative cancellation until all
  provider, thread, ffmpeg, and worker operations can be terminated and awaited.
- Persist alignment provenance on every canonical segment:
  `exact_word`, `provider_segment`, or `estimated`. Never present proportional
  distribution of plain transcript text over diarization turns as exact. Model
  recommendations must prefer timestamp-capable STT whenever local speaker
  fallback is selected, and UI/export must disclose estimated alignment.
- Replace provider-only post-response routing with normalized evidence keyed by
  provider, exact model, requested response shape, and parser version. Native
  diarization is proven by parsed speaker-labelled intervals, not by a registry
  boolean. If real speaker evidence is absent, the local fallback remains
  eligible even when the provider generally advertises diarization.
- YouTube captions do not contain enough durable audio evidence for the current
  local speaker worker. Caption-first remains the fast path when speakers are
  not required. An explicit speaker-separation request must select the audio
  path (or a future timestamped-caption-plus-audio aligner); Scriber must not
  invent speaker labels from caption text alone.
- The Rust worker keeps a hard two-hour/1-GiB protocol ceiling, while initial
  product eligibility for local fallback is 60 minutes pending a real 60-minute
  multilingual soak. Above that limit, continue STT but visibly skip local
  fallback and recommend native diarization. Windowed processing is not release-
  ready until speaker identities are clustered globally across windows;
  resetting `Speaker 1` per chunk is forbidden. An explicitly selected expected
  speaker count may be passed through; Outlook attendance is never applied as
  speaker count automatically and clustering threshold remains internal.
- The exact ERes2Net ModelScope card declares training on approximately 10,000
  speakers of 16-kHz Chinese audio. Gate release on held-out German, English,
  mixed-language, accent, pitch-range, and overlap evidence. Treat this as an
  empirical model-quality risk, not a legal conclusion and not proof that the
  model necessarily fails outside its declared training language.
- New File and YouTube work now freezes provider/model/language/transport/parser
  routes, persists normalized timed StageResults, commits stable canonical
  segments plus FTS, and renders `transcripts.content` only as compatibility
  output. Timed JSON3/VTT captions enter the same path without invented
  speakers. Meeting finalization now checkpoints each track independently,
  commits the aggregate artifact, and projects identical stable ids into
  MeetingStore. Remaining migration work is public REST/TypeScript canonical
  segment reads and an explicitly estimated view for legacy plain-text rows.
- Recovery now claims a persisted provider result by lease/CAS and canonicalizes
  it without another cloud call. Provider calls heartbeat their lease without
  changing the state version; pre-result cancellation/failure closes the attempt
  instead of leaving an ownerless `transcribing` row. Successful local speaker
  separation is now persisted as an immutable track derivation bound to its
  parent provider StageResult, frozen route/worker manifest, and checksum. Resume
  reuses that derivation without a second ONNX run, and the canonical artifact
  records it as an explicit `track_derivation` input.
- Freeze exact provider/model/response/parser routes per attempt. Persist a
  validated normalized stage result before local diarization so restart recovery
  never repeats a completed cloud call. Canonical head replacement is a CAS
  transaction; stale attempts become `superseded` rather than overwriting newer
  work. Stable segment ids exclude artifact version and new citations bind both
  artifact and segment id.
- Parse JSON3 `tStartMs`/`dDurationMs` and every valid VTT cue interval rather
  than flattening captions to text. Caption timing is `provider_segment`, never
  speaker evidence. A caption response without valid timing falls back to the
  frozen audio route. Remove absolute local paths from public File `sourceUrl`.
- File/YouTube source audio is registered as `processing_only`; terminal cleanup
  now advances `available -> purge_pending -> purged` only after task release,
  while retries retain the source. The durable tombstone distinguishes intended
  removal from corruption. Startup/maintenance now finishes assets stranded in
  `purge_pending` with path containment, file-only deletion, empty-parent
  cleanup, and a durable recovery tombstone. Opt-in playback retention remains
  P1.
- Preserve Meeting-clock gaps in live preview timestamps. The bounded STT queue
  may drop preview frames without affecting durable capture; map every sent
  provider-audio span back to its source interval and translate provider tokens
  piecewise. Never revert to one connection offset, which silently shifts all
  post-drop segment links.
- Harden 30-second checkpoint publication across the SQLite/filesystem boundary.
  The current final-WAV rename precedes the chunk-row transaction, so a crash can
  leave a valid but rowless chunk. Introduce durable `prepared -> complete`
  chunk state, file fsync, deterministic rename, startup reconciliation for
  every prepared/partial/final combination, and a conservative legacy-orphan
  adopter. Checkpoints must persist source-specific durable frontiers; one
  track's longer chunk must not advance another track's transcript frontier.
- Post-finalization audio tiering now keeps PCM for live/recovery work, verifies
  Matroska/FLAC by full decoded sample hash/count equality, and creates separate
  Meeting-clock-aligned Opus assets for mix, microphone, and system playback.
  After the canonical commit, optional voice work, and durable `ready`
  transition, redundant chunks/final WAVs advance through
  `purge_pending -> purged`; maintenance resumes an interrupted purge only when
  the canonical head and every required archive/playback hash still verify.
  Peak disk usage is now bounded by preparing and verifying one temporary PCM
  track at a time, retaining verified compressed `*.work.flac` inputs, and
  decoding only the required track to a task-scoped WAV for WAV-only optional
  consumers. Do not switch canonical storage to lossy WebM/Opus until
  multilingual quality and pre-skip/end-trim tests pass.
- Provider transport is separate from retention. Soniox Meeting finalization
  freezes `webm_opus_task_derivative` in its RouteSnapshot, creates the compact
  upload from the required lossless track, and removes it in a cancellation-safe
  `finally` boundary after provider release. It never replaces FLAC as local
  canonical evidence.
- Native-audio admission is now both process-local and persisted: Live Mic,
  Meeting start/resume, and device tests first claim the same lazy lock and then
  one expiring SQLite singleton lease before prewarm/native awaits. The lease
  stores only opaque workflow/controller ids, renews every 15 seconds with a
  60-second TTL, transfers the pending Meeting claim to its durable Meeting id,
  and uses CAS-safe release so an old controller cannot delete a successor's
  claim. Paused Meetings retain ownership; stop, terminal failure, watchdog
  failure, and graceful shutdown release it. An abruptly dead process can delay
  takeover for at most the remaining TTL. A heartbeat/Meeting-id-transfer race
  adopts the newer same-controller generation; a genuinely foreign generation
  fails closed through Live Mic emergency stop or the Meeting capture watchdog.
  Meeting-file import remains outside native-audio admission and relies on the
  durable Meeting workflow constraint.
- The release smoke now has a non-user-audio Meeting device gate. An explicit
  synthetic-signal mode generates distinct render, delayed echo, and near-end
  microphone tones, then proves nonzero raw mic/system/AEC-clean levels through
  REST -> private Shell IPC -> Rust sidecar -> named pipes -> Python probe. The
  gate also requires zero persistence/provider upload and sidecar cleanup. This
  complements, but does not replace, the physical Teams/Zoom/Meet matrix.
- Preserve workflow phase on recovery: `analyzing` resumes as analysis-only
  failure, never generic interrupted/finalizing. Reconcile linked import state
  from Meeting terminal states so an analysis crash cannot leave an import at
  97 percent forever. Reserve task slot before state transition and make the
  cancellation boundary rollback-or-run, never open-state-without-worker.
- Analysis output and its automatic action-item generation now commit in one
  SQLite transaction. Regeneration deletes absent unmodified automatic rows and
  preserves edited rows with explicit `carried_user` provenance, so a crash or
  reanalysis cannot expose a new output with stale automatic tasks.
- Freeze language, exact model, response shape, parser version, and request
  options in the attempt RouteSnapshot. Batch provider adapters must not read
  mutable global Settings for queued, retried, or recovered work.

Release tests for this boundary must cover file-first and metadata-first
multipart ordering, client disconnect during upload and preparation, cancel
after upload, crash after source commit, active-Meeting races, finalizer/delete
races, component corruption, worker timeout/crash/OOM, no-timestamp STT,
caption-first YouTube, files above the local memory budget, and native-provider
bypass of the local worker.

Meeting release promotion still requires real Windows evidence for microphone
plus Teams/Zoom/browser loopback, default-device changes, Bluetooth/headset
routes, sleep/resume, long meetings, network loss/recovery, Outlook tenant
types, and installer upgrade/uninstall retention. The optional WeSpeaker model
also remains behind a commercial/legal review because of its VoxCeleb training
data terms. These are release evidence gates, not missing fallback capture
paths; the normal Live Mic workflow intentionally does not enable AEC3 without
a render reference. `scripts\run_meeting_release_matrix.ps1` now prepares 19
atomic non-passing operator drafts and
`scripts\validate_meeting_release_matrix.py` validates completed evidence; the
real physical scenarios themselves have not yet been collected. Two technical
reports are now collected against the current installer SHA:
support-bundle privacy with zero structural findings and an automated
regression summary with 1,670 passing checks, including 1,508 Python tests,
35 Rust audio tests, 115 Rust shell tests, 12 browser interaction gates, and
the installed synthetic Meeting Mic/System/AEC path. These partial reports do
not satisfy the physical, Outlook, voiceprint-corpus, legal-review, soak, or
signed-release gates. Draft initialization now compares app version and
installer SHA: mismatched work-in-progress drafts move to `stale-drafts` before
fresh current-installer drafts are created, so operator notes are preserved but
cannot be mistaken for evidence against another binary.

### Meeting UI/UX research backlog (2026-07-12)

This is an implementation handoff, not a claim that the features below already
exist. The audit used the current React component/API contracts, the existing
browser smoke, GitHub primary sources, and a new mock-backend walkthrough of the
start, device-test, live, completed, analysis, Ask Meeting, Settings, and narrow-
width states. The transient reference captures use deterministic mock data and
remain local audit artifacts rather than repository dependencies. They prove
layout and interaction
structure, not physical audio, real Outlook tenants, long-transcript behavior,
screen-reader conformance, 200% zoom, localization, or release readiness.

#### Do not rebuild the existing baseline

The current product already has Mic/System/AEC3 capture, the explicit route
test, pause/resume/stop, 30-second checkpoints, reconnect health, durable import
and recovery, Outlook connect/status, transparent live/final/analysis model
labels, Voice Library controls, Overview/Decisions/Actions/Questions/Notes/Ask
views, timestamped click-to-seek transcript segments, Meeting-local search,
speaker rename/split/merge, mixed and isolated playback, retention, export,
email preview, and preview-confirmed webhook delivery. New work should deepen
trust, correction, review, and information architecture instead of adding
duplicate tabs or a second Meeting state model.

Priority order:

| ID | Priority | Feature | User outcome |
| --- | --- | --- | --- |
| `UX-MTG-01` | P0 | Adaptive Meeting workspace shell | All controls remain reachable at desktop and narrow widths |
| `UX-MTG-02` | P0 | Global active-Meeting pill and capture health | Recording remains visible and recoverable on every app route |
| `UX-MTG-03` | P0 | Non-destructive transcript corrections | Users can repair ASR text without losing provenance |
| `UX-MTG-04` | P1 | Retranscribe/reprocess from canonical audio | A poor model choice no longer requires reimport or rerecording |
| `UX-MTG-05` | P1 | Explicit calendar-event selection and participant snapshot (implemented) | The correct event and recipients are attached before recording |
| `UX-MTG-06` | P1 | Confidence-driven speaker review (core implemented) | Ambiguous speakers can be resolved quickly and safely |
| `UX-MTG-07` | P1 | Playback follow, match navigation, and bookmarks (implemented) | Review is one synchronized timeline workflow |
| `UX-MTG-08` | P1 | Versioned analysis/output templates | Standups, 1:1s, sales calls, and interviews produce the right output |
| `UX-MTG-09` | P2 | Rich Ask Meeting and action workspace | Answers and tasks become reusable, cited outcomes |
| `UX-MTG-10` | P2 | Global Meeting-library search | Users can retrieve evidence across months of meetings |

Implementation update (2026-07-12): `UX-MTG-01` through `UX-MTG-03` are now
implemented at the selective core boundary. At widths of at least 1,100 CSS
pixels the Meeting list becomes a compact rail beside the workspace; below that
boundary a selected Meeting replaces the list and exposes an explicit back
action. Preflight separates primary configuration from sticky readiness, keeps
the Start action reachable, places lower-frequency retention/model details
behind disclosure, and shows checkpoint freshness plus five-hour storage
readiness. Transcript rows expose Start, End, and Duration with unambiguous
hour-long offsets and direct seeking. Workspace tabs retain explicit
scroll/previous/more affordances, and the narrow browser gate has no horizontal
overflow. An app-shell capture pill keeps title, elapsed time, Mic/System
health, Pause/Resume, Stop, and return navigation visible across routes. Ready
canonical segments support inline correction and undo with optimistic
concurrency, immutable edit history, FTS refresh, WebSocket cache updates, and
visibly stale analysis outputs. Playback following and navigable review search
are now implemented; templates and global library search remain unimplemented
until usage evidence justifies them.
`UX-MTG-05` is implemented with a refreshable all-day Outlook event picker,
explicit no-event selection, participant details, and an immutable event
snapshot. `UX-MTG-06` now implements the safe core: Voice/account-first local
suggestions, user-triggered privacy-bounded LLM suggestions for unresolved
speakers, and individually confirmed Meeting-local assignments. Audio-example
review, bulk apply, and one-step assignment undo remain open.

#### `UX-MTG-01` - Adaptive Meeting workspace shell

**Status:** implemented at the normal-width reachability boundary; the broader
collapsible-rail and sticky-player ideas remain evidence-gated.

**Problem and observed evidence**

At about 1,280 pixels, the live header/actions, tab labels, route-test content,
and parts of playback are visibly clipped even though this is a normal desktop
width. At 390 pixels the horizontal workspace tabs continue offscreen without a
strong scroll/fade or More affordance. The persistent Meeting list also takes
scarce width from the primary task.

**Interaction specification**

- Treat the Meeting list as a collapsible rail/drawer below an evidence-based
  breakpoint, not as a permanent column. Preserve the main app sidebar pattern.
- Keep one sticky compact command bar with title, state, elapsed time,
  checkpoint status, Pause/Resume/Stop, and the relevant post-meeting primary
  action. Secondary actions move into a labelled overflow menu.
- On narrow desktop, make workspace navigation horizontally scrollable with
  edge fade and keyboard-accessible previous/next controls. On phone widths,
  keep the three most relevant destinations and expose the rest through More.
- The player becomes sticky below the command bar during review. It must never
  cover transcript search, focused content, or Windows resize handles.

**Acceptance boundary**

Test 390, 768, 1,024, 1,280, 1,440, and 1,920 CSS pixels, 200% zoom, long German
labels, reduced motion, and keyboard-only navigation. No action, tab, player, or
focus ring may clip or require page-level horizontal scrolling. Targets are at
least 44 CSS pixels where density permits and every icon-only action has an
accessible name.

#### `UX-MTG-02` - Global active-Meeting pill and capture health

**Status:** implemented with app-shell visibility and bounded source-health
labels. Detailed dropped-frame diagnostics remain in the Meeting workspace.

**Problem**

Leaving the Meetings route hides the active recording and its failures. A user
can work in Settings or File while native capture continues, but cannot see the
elapsed time, source health, or last durable checkpoint.

**Interaction specification**

- Maintain one app-level active-Meeting store. Show a compact pill below the
  titlebar on every route with Meeting title, elapsed time, Mic/System health,
  last checkpoint age, click-to-return, Pause/Resume, and Stop.
- Do not animate or announce 60-Hz levels. Change the bounded health label only
  for meaningful states: healthy, source stale, checkpoint overdue,
  reconnecting, paused, finalizing, or action required.
- An amber state opens a concise diagnosis and in-scope recovery action such as
  restarting one capture route. Finalizing remains visible with progress and a
  deep link until the Meeting is ready or failed.

**Backend/data and tests**

Derive health from existing Meeting events plus redacted `lastFrameAt`, dropped-
frame count, `lastCheckpointAt`, and recovery reason per source. Preserve the
single-active-Meeting invariant. Navigate through every primary tab during
capture, disconnect/reconnect WebSocket, restart the backend, pause, and
finalize. Assert one pill, accurate controls, text+icon status rather than color
alone, no high-frequency screen-reader announcements, and no duplicated capture
after recovery.

#### `UX-MTG-03` - Non-destructive transcript corrections

**Status:** implemented for ready canonical segments, including edit, undo,
immutable history, FTS, optimistic `409` conflicts, and stale-output warnings.

**Problem**

ASR text is currently immutable. A misheard name contaminates search, summary,
action items, Ask Meeting, email, and export with no trustworthy correction
path.

**Interaction specification**

- Segment overflow or the `E` shortcut opens an inline labelled editor with
  Save, Cancel, and validation. Keep speaker, source, start/end/duration, and
  original provider evidence visible.
- A saved edit receives an `Edited` badge, undo, and revision history. Existing
  generated outputs become visibly stale and offer explicit regeneration; never
  regenerate or overwrite silently.
- Editing is disabled while the live/canonical revision is still changing. The
  user can still add a timestamped note/bookmark.

**Backend/data and tests**

Use immutable segment edits or transcript revisions with base revision/digest,
old/new text, actor/local timestamp, and optimistic concurrency. Reindex FTS and
bind every analysis/export/chat result to its transcript revision. Cover empty
text, conflicting edits (`409`), undo, speaker rename during an edit, estimated
timing, reload, and keyboard-only save/cancel. The provider original must remain
recoverable and an edit must immediately drive search and new exports.

#### `UX-MTG-04` - Retranscribe/reprocess from canonical audio

**Status:** core implemented. The Meeting workspace offers separate local
speaker refresh and full retranscription modes for both `ready` and
`analysis_failed` Meetings. Availability is derived from retained, bounded
audio evidence, Voice Library readiness, provider credentials, model duration
limits, and exact playback metadata. Full retranscription freezes the current
Settings provider/model and keeps the existing transcript readable until the
new canonical artifact commits. A retry after an artifact/projection crash
reuses that committed provider result rather than paying for transcription
again. Speaker refresh verifies retained playback before local inference and
makes no paid STT request. Retry recovery requires an exact frozen
workload/source/provider/model/language match; provider switches, rollbacks, and
model-specific duration checks use one consistent provider/model pair.

**Problem**

A wrong language, STT model, or diarization choice should not require a
duplicate import or rerecording while verified canonical audio is retained.

**Interaction specification**

- The shipped Process again dialog exposes the selected provider/model, local
  versus cloud handling, destructive transcript implications, and a precise
  reason when either mode is unavailable.
- Remaining enhancement: expose a durable run-history/compare surface and
  explicit activation between successful transcript revisions. Current full
  retranscription activates its newly committed canonical artifact directly;
  failures keep the prior canonical transcript intact.

**Backend/data and tests**

Continue to persist immutable transcription attempts keyed by audio digest and
a frozen route snapshot. Reuse canonical FLAC/provider derivatives; never
overwrite the active Meeting projection on provider failure. Remaining gates
are an explicit user cancellation contract and revision compare/activation UI;
automated coverage already includes missing credentials, malformed/purged
audio evidence, provider/model freezing, and speaker-only versus full mode
admission.

#### `UX-MTG-05` - Explicit calendar event and participant snapshot

**Status:** implemented. The Meetings preflight lists the selected local day's
events, supports manual refresh and explicit no-event selection, shows event and
participant details, and freezes the selected evidence at Start.

**Problem**

Selecting only the nearest/current event is ambiguous for early starts,
overlapping calls, back-to-back meetings, and personal blocks. A wrong match can
also address an email draft to the wrong people.

**Interaction specification**

- The Outlook preflight card lists every cached event for the current local day
  plus `No calendar event`. Selection shows title, time, location, organizer,
  participants, and join link before Start; Refresh requests the newest calendar
  state without silently changing the user's selection.
- Freeze the chosen event for the Meeting. Later calendar changes may be shown
  as an update, but must not silently replace recipients or speaker context.

**Backend/data and tests**

Start with explicit `calendarEventId` or explicit `null`; persist an immutable
snapshot containing event id, subject, organizer, attendees, connected-account
aliases, time range, join URL, ETag, and sync time. Daily boundaries are
browser-computed local midnights converted to UTC so DST works without frozen
backend `tzdata`. Graph delta pages are staged and committed atomically without
unsupported `$select`. Email preview/export reads only the frozen participant
set, excludes self aliases, declined/resource attendees, and never derives
recipients from speaker mappings or LLM output. Regression coverage must
continue to preserve overlap, cancellation/edit, offline start,
duplicate/missing addresses, Outlook disconnect after start, recovery, and
explicit no-event selection.

#### `UX-MTG-06` - Confidence-driven speaker review

**Status:** selective core implemented. Local Voice/account suggestions,
on-demand LLM suggestions, and explicit single-speaker confirmation are shipped;
audio examples, bulk reassignment, and one-step assignment undo remain open.

**Problem**

Voice profiles and the connected-account identity now generate the first local
candidate layer. Unknown speakers can receive optional LLM candidates, but the
remaining advanced review queue must still make low-confidence or conflicting
cases fast to compare without requiring users to understand clustering internals.

**Interaction specification**

- Keep unique local Voice matches first and protect the microphone/account
  identity. For unresolved speakers, send an LLM only participant names, opaque
  ids, and short email-redacted excerpts after an explicit user action; Outlook
  email addresses remain local. Require confirmation before persistence.
- Remaining work: show `Review N speakers` only for low-confidence, low-margin,
  conflicting, or unknown matches. A side sheet presents a few short local
  audio examples, transcript examples, source, and plain-language confidence.
- Remaining work: support apply-to-selected/all and one-step undo. Linking a
  reusable biometric profile remains explicit opt-in, never a side effect of
  rename or participant assignment.

**Backend/data and tests**

Confirmed Meeting-local participant links are persisted only after human action;
LLM output itself never controls email recipients. Remaining work is durable
candidate review state with model/revision/confidence/margin, atomic bulk
reassignment, audio-example handling, and undo. Protect the Mic `You` identity
by default. Test overlap, same names, no calendar, purged audio,
deleted/opted-out profiles, transaction rollback, privacy payloads,
high-confidence no-banner behavior, and that accepted changes update transcript
and Search without changing the frozen export-recipient set.

#### `UX-MTG-07` - One synchronized review timeline

**Implemented behavior (2026-08-16)**

The transcript is now the primary review surface in a calm, rounded workspace,
with a compact Meeting Brief and live notes as its companion column on wide
screens. A dedicated review toolbar owns navigable search, speaker/time filters,
playback following, timestamp bookmarks, and one evidence-marker rail. The
technical and speaker-management controls remain available through progressive
disclosure instead of competing with the transcript.

**Interaction specification**

- Add an explicit Follow toggle. Playback marks the active segment and scrolls
  only while Follow is on; manual scrolling turns it off without fighting the
  user.
- Transcript search gains match count, Previous/Next, Enter-to-play, speaker and
  time filters, and exact/semantic scope when semantic retrieval is later
  justified.
- A live/review Bookmark action stores the current Meeting time plus optional
  text. Show bookmarks, chapters, decisions, and action citations as distinct
  markers on the player timeline and in Notes.

**Backend/data and tests**

Use the existing FTS endpoint and `MeetingNote.atMs`; window/virtualize long
transcripts rather than loading all matches. Cover estimated times, gaps,
mic-only/system-only and purged audio, more than 10,000 segments, search wrap,
manual scroll, reload, citation seek, and keyboard shortcuts. Text search stays
available when audio has been purged; Play is clearly disabled.

#### `UX-MTG-08` - Versioned analysis and delivery templates

**Problem**

One analysis schema cannot serve a standup, 1:1, sales call, interview, and
incident review equally. Separate ad-hoc email/export formatting can also drift
from the on-screen result.

**Interaction specification**

- Settings -> Meetings -> Templates supports create, duplicate, preview,
  import/export, soft delete, and keyboard-accessible reordering. Ship a small,
  reviewed set of Scriber-owned defaults rather than copying another project's
  prompts.
- A Meeting profile chooses its default; preflight may override it. Overview
  always shows template name/version. Regenerate chooses an explicit version,
  and email/export render the same structured output.

**Backend/data and tests**

Version template name, prompt, language, JSON schema, sections, and delivery
rules. Snapshot the version on every output and validate LLM JSON before commit.
Test immutable old outputs, invalid schema/import, multilingual output, missing
credentials, cancel retaining the old result, deleted defaults, and exact
section parity between Overview, email body, Markdown, PDF, and DOCX.

#### `UX-MTG-09` - Rich Ask Meeting and action workspace

The current Ask view is intentionally minimal. Add question suggestions derived
from available output types, durable multi-turn threads, scope selectors for the
whole Meeting/current chapter/selected time range/speaker, citation preview with
playback, and explicit `Save as note/action/decision`. Never create an external
task automatically.

Turn existing action-item owner, due date, status, and citation data into a
review workspace: attendee-aware assignee combobox, due/status filters, bulk
confirm/dismiss, and previewed Copy/Email/ICS/To-Do draft actions. Every answer
and generated task remains bound to transcript/output revision. Test empty
evidence, purged audio, long-chat retrieval, late responses after navigation,
participant without email, and explicit confirmation before any external side
effect.

#### `UX-MTG-10` - Global Meeting library search

Add a library view and Command Palette group that searches title, active
transcript revision, notes, speakers, decisions, and action items. Filters cover
date, participant, speaker, state, and output template. A result names Meeting,
speaker, time, and snippet; opening it deep-links to the Meeting, active tab,
search match, and playback position.

Start with paginated SQLite FTS5 and stable sorting; do not add a vector database
without a measured semantic-search need. Reindex edits and active-revision
changes, remove deleted/purged content correctly, and test multilingual tokens,
pagination, filters, more than 10,000 Meetings, keyboard removal of filter chips,
and accessible result names.

#### Meetily deep comparison and Codex implementation plan

**Pinned comparison:** Meetily
[`0281737d`](https://github.com/Zackriya-Solutions/meetily/tree/0281737d87d26352fb0adc78c8c0975f691b23d1)
(`v0.4.0` source, inspected 2026-08-16) against this Scriber revision. The
comparison treats Meetily as product and architecture evidence, not as a source
of code, prompts, assets, or schemas.

| Axis | Meetily evidence | Scriber evidence and decision |
| --- | --- | --- |
| Product boundary | Local Whisper/Parakeet transcription, optional local or cloud summary providers, import/retranscription, transcript recovery, templates, and a compact two-pane Meeting view | Scriber already covers bot-free capture, local/cloud transcription, import/reprocessing, recovery, speaker review, Outlook context, notes, Ask, exports, email drafts, and delivery. Preserve that broader workflow; borrow only interactions that shorten review. |
| Audio capture | One in-process Rust `RecordingManager`, process-global `Mutex<Option<...>>` plus a separate atomic recording flag, CPAL mic/system streams, an unbounded channel, and a simple 50 ms ring-buffer mix ([commands](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/frontend/src-tauri/src/audio/recording_commands.rs), [pipeline](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/frontend/src-tauri/src/audio/pipeline.rs)) | Scriber deliberately keeps physical WASAPI capture in a supervised Rust sidecar, uses one shared clock for raw mic/system/AEC3-clean tracks, and admits it through a durable cross-process lease. Do not replace this with a process-global frontend manager or mixed-only capture. |
| Lifecycle ownership | Start/stop/pause/resume mutate several globals and listeners; `RecordingManager` contains an `unsafe impl Send` ([manager](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/frontend/src-tauri/src/audio/recording_manager.rs)) | Scriber owns lifecycle in `ScriberWebController`, exposes strict route commands/outcomes, reserves finalization before irreversible stop, and retains native ownership until stop is confirmed. Keep that deep owner; do not move orchestration into React or shallow route adapters. |
| Crash recovery | The browser mirrors transcript events into IndexedDB while Rust encodes 30-second MP4 files. Recovery later scans filenames, estimates every chunk as 30 seconds, and concatenates them ([incremental saver](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/frontend/src-tauri/src/audio/incremental_saver.rs), [recovery hook](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/frontend/src/hooks/useTranscriptRecovery.ts)) | Scriber uses prepared/complete audio-chunk commits, hashes, shared-timeline metadata, base/delta transcript checkpoints, durable Meeting states, corruption fallback, and restart recovery in SQLite. The single durable authority is materially stronger than a browser/filesystem join and stays unchanged. |
| Transcription | Local Whisper and Parakeet engines are first-class and can use platform GPU features; transcript events are buffered and reordered in React | Scriber supports multiple frozen provider routes plus local ONNX, separates live preview from canonical final artifacts, and snapshots route/model evidence. Retain the provider-neutral artifact boundary; a future local GPU route must enter through it rather than fork Meeting semantics. |
| Review interaction | `useAutoScroll` stops following when the user scrolls away; `VirtualizedTranscriptView` windows rows after ten segments ([auto-scroll](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/frontend/src/hooks/useAutoScroll.ts), [virtual transcript](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/frontend/src/components/VirtualizedTranscriptView.tsx)) | Scriber keeps its existing virtualizer and separate live-text following, but now adds canonical active-segment selection, opt-out playback following, ordered match navigation, speaker/time filters, timestamp bookmarks, and evidence markers. The UI adapts Meetily's calm two-pane hierarchy without copying its hook, code, or weaker persistence model. |
| Summary templates | Built-in, bundled, and user JSON files can override the same mutable template id; validation covers non-empty strings and three format labels. A rendered fingerprint participates in one summary cache ([types](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/frontend/src-tauri/src/summary/templates/types.rs), [loader](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/frontend/src-tauri/src/summary/templates/loader.rs), [service](https://github.com/Zackriya-Solutions/meetily/blob/0281737d87d26352fb0adc78c8c0975f691b23d1/frontend/src-tauri/src/summary/service.rs)) | Template choice is useful, but mutable filesystem ids are insufficient for immutable prior output, schema validation, and screen/email/export parity. Follow with `UX-MTG-08`: versioned durable definitions and an exact template snapshot on every output. |
| Persistence model | Meetings, transcript rows, summary-process rows, chunks, settings, and one note row are straightforward SQLite repositories; there is no comparable durable capture/finalization state machine or immutable artifact lineage | Scriber pays more complexity for recoverability: explicit state transitions, transcript edit versions, stale-output projection, immutable provider stages, and durable cleanup intents. Keep these invariants and improve their interfaces incrementally instead of adopting Meetily's simpler schema. |
| Privacy/security | Meetily is local-first but may send summaries to configured cloud providers, ships opt-in PostHog analytics, gives the main WebView broad `$APPDATA`/filesystem permissions, and exposes many Tauri commands directly | Scriber keeps browser access behind a per-run token, resolves privileged work through narrow loopback/shell boundaries, redacts support evidence, and has no product telemetry dependency. Keep least-privilege and explicit provider disclosure; do not equate local capture with an entirely local workflow. |
| Modularity/testability | Meetily's Rust core contains large command/engine modules, global registries, legacy/backup source files, about 194 Rust unit-test functions, no frontend test script, and no automatic PR test workflow at the pinned revision | Scriber still has large controller/store/page ownership units, but route-local ports, cancellation barriers, supervisors, repository-wide static gates, component tests, real backend browser smoke, and installed Tauri gates provide safer seams. Continue bounded deep-module extraction; do not copy the global-manager shape. |
| Platform/release | Meetily builds Windows/macOS/Linux variants with several acceleration features and updater artifacts | Scriber is intentionally Windows-first and has stronger exact-runtime, sidecar, updater, signing, upgrade/uninstall, physical-audio, and privacy gates. Cross-platform support is a separate product decision, not an incidental benefit of this comparison. |

**Implemented slice: `UX-MTG-07`, one synchronized review timeline.**
This was the highest coherent user-visible gain not already implemented, and it
uses existing durable `MeetingSegment`, `MeetingNote.atMs`, audio-asset,
analysis-citation, and action-item evidence rather than adding a competing
store.

Codex implementation sequence and acceptance:

1. Extract pure review-timeline policy from the large Meetings page: canonical
   active-segment selection over exact/estimated timestamps, deterministic
   text/speaker/time matching, wrap-around navigation, and normalized marker
   positions. Its interface is the unit-test surface; React must not rediscover
   those rules.
2. Turn the transcript viewport into the interaction owner for playback Follow.
   The active row is visibly and accessibly marked; playback scrolls it only
   while Follow is enabled; wheel/touch/manual scroll disables Follow; the
   explicit control restores it. Live `Latest text` behavior remains separate.
3. Replace filter-only search with an ordered result workflow: visible match
   count, current `N of M`, Previous/Next, Enter-to-play, wrap-around, speaker
   filter, and bounded start/end-time filters. Search continues to work with
   purged audio while playback controls become unavailable.
4. Add a timestamp Bookmark action at the authoritative current Meeting clock.
   Persist through the existing `POST /api/meetings/{id}/notes` boundary, then
   reconcile the returned note through the existing query/WebSocket event
   contract. Optional text is bounded and explicit; no generated output can
   create a bookmark automatically.
5. Render one accessible marker rail for bookmarks, analysis decisions, action
   citations, and chapter/section citations when present. Marker clicks use the
   existing Meeting-clock-to-asset conversion; missing/purged audio leaves the
   evidence visible but disabled for playback.
6. Prove the policy with unit tests, the note boundary with real aiohttp plus
   SQLite, React behavior with component tests (manual-scroll opt-out, keyboard
   navigation, filters, wrap, active row), and the final vertical slice with a
   real `create_app` browser flow. Extend the installed Tauri Meeting smoke so
   audio playback advances through at least two segments, Follow moves the
   active row, a manual scroll disables it, search navigation seeks, and a
   bookmark survives reload in SQLite. Include a long virtualized fixture and
   narrow-width/focus assertions.
7. Record the final verification boundary and reassess `UX-MTG-08`. Do not
   claim physical Teams/microphone coexistence from a synthetic audio fixture.

Implementation evidence on the 2026-08-16 worktree: the pure timeline and
component suites pass inside the complete 93-library/64-component frontend
run; TypeScript and the production Vite build pass on the pinned Node 26.5.0;
159 focused Meeting/API/smoke-contract tests pass. The complete Chrome product
smoke passes 23 interaction flows across 11 routes with zero critical console,
page, or unhandled-rejection errors and zero mobile overflow; retained result
SHA-256 is
`67E8518E33CA05155D5B96A8FE97BA1C6C7B489C7163665E4EA892CD45ABF957`.
The current Tauri driver verifies search, Enter-to-play, Follow, the marker
rail, and a durable bookmark. The final fresh Tauri/WebView2 build uses MSVC
plus the repository-pinned Windows SDK `10.0.26100.6584` from an isolated
Microsoft C++ NuGet extraction. Its real Rust-sidecar/Python/ONNX/SQLite/React
run passes `recording -> paused -> recording -> finalizing -> ready ->
finalizing -> ready`, persists and reads back the bookmark, exercises the
remaining Workspace/Processing/Artifact/Catalog/Live-Mic path, reports zero
console/page/request failures, and verifies complete cleanup. Privacy-minimal
result:
`tmp/meeting-e2e/runs/8e529a8d51774ff8b71063c72f7d4556/result.json`,
SHA-256
`54E1B8AE3DF19A5F7D2F0868F005E1C9C2E7FD6F427E0BE254BEF50A1FDA83CB`.

#### Primary-source research and adaptation rules

| Primary source | Observed idea | Scriber-specific adaptation |
| --- | --- | --- |
| [Meetily releases](https://github.com/Zackriya-Solutions/meetily/releases) | Import/retranscription, transcript recovery, inline-edit direction, auto-follow, and meeting templates | Immutable revisions, explicit stale outputs, template snapshots, and current Scriber durability contracts |
| [OpenWhispr changelog](https://github.com/OpenWhispr/openwhispr/blob/main/CHANGELOG.md) | Background capture in a global store, floating recording pill, attendee-aware speaker reassignment, and Meeting-specific model settings | App-level capture health, explicit Outlook event snapshot, and opt-in Voice-profile linking |
| [MercuryScribe](https://github.com/literatecomputing/transcribe-with-whisper) | Editable transcript synchronized with media playback | First-class segment revisions plus Follow/search/bookmark workflow using Scriber timestamps |
| [Screenpipe](https://github.com/screenpipe/screenpipe) | SQLite FTS5, timeline navigation, and health-oriented local APIs | Meeting-only global search and health; do not adopt continuous screen/OCR capture |
| [Nojoin](https://github.com/Valtora/Nojoin) | Bot-free local Meeting context, voice library, notes/chat, and cross-recording retrieval concepts | Preserve Scriber capture/privacy boundaries and implement with existing SQLite/Outlook contracts |
| [Millet](https://github.com/pretyflaco/millet) | Typed, versioned summary metadata for downstream tools | One validated output schema shared by Overview, export, email, and future integrations |

Use these as product inspiration only. Do not copy code, prompts, assets, or
schemas without a dependency/license review. Explicit non-goals are Meeting
bots, 24/7 screen/OCR recording, automatic external sending, live coaching, and
a vector database before FTS evidence demonstrates a need.

#### Recommended delivery slices

1. **Trust and reachability:** `UX-MTG-01`, global capture store and
   `UX-MTG-02`, then long-Meeting responsive/browser smokes.
2. **Correction and quality recovery:** `UX-MTG-03` followed by
   `UX-MTG-04`; make every downstream artifact revision-aware once.
3. **Identity and context:** `UX-MTG-05` and `UX-MTG-06` using the same immutable
   participant/speaker contracts.
4. **Fast review and reusable outcomes:** `UX-MTG-07`, `UX-MTG-08`, then Ask,
   actions, and global library search.

Each slice needs component tests, REST/WebSocket contract tests, durable-store
recovery tests, keyboard/focus checks, narrow-width screenshots, and one long-
Meeting fixture. Physical capture, Outlook tenant, and installed Windows matrix
evidence remains mandatory where the slice touches those boundaries.

1. Keep installed app stability high.
   - Run longer idle and live-recording stability smokes.
   - Track backend working-set growth and average idle CPU.
   - Capture support bundles for any spontaneous mic shutoff reports.

2. Continue responsive UI polish.
   - Debug Console and Settings should stay usable at narrow desktop widths.
   - Buttons should not become oversized or clipped.
   - Support-bundle download needs clear visible feedback with saved path when
     the browser/Tauri environment allows it.

3. Keep release packaging reproducible.
   - Profile B should remain standard.
   - Gyan Essentials should remain fallback.
   - Any size pruning must pass installed frontend, media, support-bundle, and
     live overlay smokes.

## Known Open Areas

Architecture and maintainability:

- `src/web_api.py`, `ScriberWebController`, `MeetingStore`, and
  `ScriberPipeline` remain large ownership units. Continue the domain-route
  extraction one bounded group at a time; keep each move behavior-preserving
  and independently covered rather than attempting a big-bang rewrite.
- `Frontend/client/src/pages/Settings.tsx` remains the largest frontend
  component. Split it along the existing tab boundaries before broadening
  component coverage to Settings and the full Meetings workspace.
- Continue expanding mypy through reviewed `src` tranches. Do not weaken the
  repository-wide Ruff/format gate to accommodate new debt.
- The editing contracts in `AGENTS.md` remain intentionally authoritative but
  large. A future documentation-only change may move detailed contracts under
  `docs/contracts/` while keeping `AGENTS.md` as the indexed entry point.

### Historical Praxist / 350M candidate feasibility (2026-08-30; superseded)

This is a source-bounded feasibility record, not training or release evidence.
It pins [Praxist 0.5.0 commit `92b78538`](https://github.com/sapientinc/PRAXIST/tree/92b785381ee13f9ea1435ba52024493c90db35ee)
and [Boldt checkpoint commit `8ed2326e`](https://huggingface.co/Boldt/Boldt-DC-350M/tree/8ed2326ed7bd833ddd832ae961dff8312746b104)
plus [LiquidAI checkpoint commit `9960764e`](https://huggingface.co/LiquidAI/LFM2.5-350M-Base/tree/9960764e30892e01f29a6dc23df2533fcd8bd5ae)
so later work does not silently inherit changed inputs.

**Feasibility verdict and blocking facts:**

- The Boldt checkpoint is a plausible small German causal-LM starting point, but it
  is not a drop-in Scriber model and no quality result has yet been measured.
  Its [model card](https://huggingface.co/Boldt/Boldt-DC-350M/blob/8ed2326ed7bd833ddd832ae961dff8312746b104/README.md)
  explicitly calls it a base model, not instruction-tuned, and recommends text
  completion rather than chat prompting. Reproducing the current Cerebras
  editor therefore requires task-specific supervised pairs and an evaluator;
  the prompt alone is not a fine-tuning dataset.
- **Model-identity correction:** despite the `Boldt-DC-350M` name, Hugging Face's
  [immutable Safetensors metadata](https://huggingface.co/api/models/Boldt/Boldt-DC-350M/revision/8ed2326ed7bd833ddd832ae961dff8312746b104)
  reports exactly **468,239,360 BF16 parameters**, and the repository lists a
  937 MB `model.safetensors`. This is consistent with Hugging Face displaying
  the model as `0.5B`, not with an exact 350-million-parameter requirement.
  The operator explicitly selected this named checkpoint, so the experiment
  treats `350M` as its published product-class label while reporting the real
  count everywhere. It must never be described as exactly 350,000,000
  parameters.
- `LiquidAI/LFM2.5-350M-Base` is the closer nominal alternative, but its pinned
  [Safetensors metadata](https://huggingface.co/api/models/LiquidAI/LFM2.5-350M-Base/revision/9960764e30892e01f29a6dc23df2533fcd8bd5ae)
  reports **354,483,968 BF16 parameters**, not mathematically exactly 350
  million. Its 709 MB source checkpoint is materially smaller than Boldt's and
  its card explicitly positions it as a pre-trained base for fine-tuning. A
  strict exactly-350,000,000-parameter requirement therefore excludes both
  published checkpoints; a nominal `350M` product-class requirement permits a
  measured A/B comparison.
- **Execution prerequisite:** Praxist does not supply the trainer, CUDA,
  datasets, or
  evaluator. Its [platform contract](https://github.com/sapientinc/PRAXIST/blob/92b785381ee13f9ea1435ba52024493c90db35ee/docs/operations/platform-support.md)
  requires an already runnable, measurable task project, release-tests Linux
  on CPython 3.11/3.12, and places Windows-native outside its current research
  runtime. This host now has a dedicated Ubuntu 24.04 WSL2 environment with the
  RTX 4070 visible, Praxist 0.5.0 installed in its own Python 3.12 venv, and the
  Codex-native doctor gate passing. The remaining prerequisite is task-owned:
  the new trainer, fresh corpus, and evaluator must be runnable before takeover.
- **Hosted-compute blocker:** the current public
  [`MyButtermilk/Scriber`](https://github.com/MyButtermilk/Scriber) remote is
  owned by a personal user account. GitHub says
  [larger/GPU runners are available only to organizations and enterprises on
  Team or Enterprise Cloud](https://docs.github.com/en/actions/concepts/runners/larger-runners).
  Consequently this repository cannot presently select a GitHub-hosted GPU
  runner merely because the user has Actions credit. The account's actual
  promotional balance was not exposed by the repository/API checks and remains
  unknown.

**Exact Boldt checkpoint contract:**

- The model card declares German text generation under `Apache-2.0`, says the
  base model was trained from scratch on the German Dense-Core FineWeb-2 subset,
  and warns that it has not had systematic toxicity, bias, or stereotype
  evaluation. Those general benchmark results are not evidence for faithful
  transcript editing.
- The pinned [`config.json`](https://huggingface.co/Boldt/Boldt-DC-350M/blob/8ed2326ed7bd833ddd832ae961dff8312746b104/config.json)
  specifies `LlamaForCausalLM`, 24 layers, hidden size 1,024, FFN size 4,096,
  16 attention and 16 KV heads, untied embeddings, BF16 weights, a 32,000-token
  vocabulary, and a 2,048-token context.
- The pinned [`tokenizer_config.json`](https://huggingface.co/Boldt/Boldt-DC-350M/blob/8ed2326ed7bd833ddd832ae961dff8312746b104/tokenizer_config.json)
  specifies `GPT2Tokenizer`, 2,048 tokens, `<|endoftext|>` BOS/EOS/UNK, and
  `<|pad|>` padding. It defines no chat template. The long production editor
  prompt plus source plus near-length-preserving result can exceed 2,048
  tokens; the trained runtime needs a short fixed task prefix and a measured,
  meaning-preserving chunk/window policy for longer dictations.
- **SFT suitability, as an inference from the published files:** a standard
  Transformers causal-LM Safetensors checkpoint and tokenizer are technically
  suitable inputs to completion-style SFT. That establishes tool shape only,
  not that 468M parameters can match a larger teacher, preserve all numbers and
  names, or learn the requested formatting rules. Full-weight SFT versus a
  parameter-efficient adapter, sequence length, effective batch, precision,
  and memory headroom remain experiment variables.

**Exact LiquidAI checkpoint contract:**

- The pinned
  [model card](https://huggingface.co/LiquidAI/LFM2.5-350M-Base/blob/9960764e30892e01f29a6dc23df2533fcd8bd5ae/README.md)
  distinguishes the `Base` checkpoint from the separately published
  instruction-tuned model and recommends this base only for heavy task,
  language, domain, or post-training adaptation. The included chat template is
  a serialization asset, not evidence that the base weights already follow
  instructions.
- The pinned
  [`config.json`](https://huggingface.co/LiquidAI/LFM2.5-350M-Base/blob/9960764e30892e01f29a6dc23df2533fcd8bd5ae/config.json)
  specifies `Lfm2ForCausalLM`, hidden size 1,024, 16 layers comprising ten
  double-gated LIV convolution blocks and six full-attention GQA blocks, 16
  attention heads, eight KV heads, tied embeddings, BF16, and a 65,536-token
  vocabulary. The config records FFN input 6,656 with auto-adjust enabled; the
  [native Transformers calculation](https://github.com/huggingface/transformers/blob/v4.57.6/src/transformers/models/lfm2/modeling_lfm2.py#L105-L120)
  scales and rounds that to an effective FFN width of 4,608. The card documents
  a 32,768-token context while the config records
  `max_position_embeddings=128000`; the
  [`tokenizer_config.json`](https://huggingface.co/LiquidAI/LFM2.5-350M-Base/blob/9960764e30892e01f29a6dc23df2533fcd8bd5ae/tokenizer_config.json)
  uses an effectively unbounded sentinel rather than a usable limit. Treat
  32,768 as the source-backed product limit until an exact long-context parity
  test explains the discrepancy.
- The pinned
  [`tokenizer.json`](https://huggingface.co/LiquidAI/LFM2.5-350M-Base/blob/9960764e30892e01f29a6dc23df2533fcd8bd5ae/tokenizer.json)
  is a 65,536-entry BPE tokenizer; BOS is `<|startoftext|>`, EOS is
  `<|im_end|>`, and padding is `<|pad|>`. Unlike Boldt, the repository provides
  a concrete
  [`chat_template.jinja`](https://huggingface.co/LiquidAI/LFM2.5-350M-Base/blob/9960764e30892e01f29a6dc23df2533fcd8bd5ae/chat_template.jinja),
  but the base weights are not instruction-tuned. The product experiment will
  use one explicit hash-pinned completion serialization for both arms instead
  of inheriting architecture-specific chat behavior.
- LiquidAI's primary
  [TRL guide](https://docs.liquid.ai/lfm/fine-tuning/trl) requires
  `transformers>=4.55.0`, `torch>=2.6`, PEFT, and Accelerate; it supports full
  SFT and recommends LoRA. The new isolated V2 environment now pins Python
  3.12, Torch 2.11.0+cu128, Transformers 5.16.1, PEFT 0.20.0, and Accelerate
  1.14.0. It does not import the old Gemma-specific trainer or any old ML data.
  Both pinned base checkpoints have completed a real BF16 load,
  forward/backward, adapter save/reload, and second forward/backward on the
  local RTX 4070 with `trust_remote_code=False`.
- **Tokenizer/runtime blocker resolved for research:** the exact checkpoint
  declares `tokenizer_class="TokenizersBackend"`, which requires the isolated
  Transformers-v5 environment. The pinned LFM tokenizer loaded successfully
  there without modifying the downloaded checkpoint. This establishes the
  research loader only; later Transformers-to-GGUF token parity remains a
  product promotion gate.
- **Explicit PEFT contract:** PEFT 0.20.0 has no `lfm2` default in
  its
  [LoRA target-module map](https://github.com/huggingface/peft/blob/v0.20.0/src/peft/utils/constants.py),
  so targets must be explicit. LiquidAI's guide lists `o_proj`, while the exact
  Transformers implementation and checkpoint use `out_proj`; the checkpoint
  also has convolution `in_proj`/`out_proj` and FFN `w1`/`w2`/`w3` tensors.
  The V2 harness therefore pins and discovers the intended exact module set
  rather than silently training only whichever names happen to match. Its real
  smoke found 92 LFM targets (`q_proj`, `k_proj`, `v_proj`, `out_proj`,
  convolution `in_proj`, and `w1`/`w2`/`w3`) and 168 Boldt targets, with finite
  gradients before and after adapter reload.
  The V2 path deliberately uses task-owned completion-only tokenization and a
  small explicit training loop instead of copying a version-sensitive TRL
  sample.
- The model repository ships no custom Python implementation. The task would
  use Apache-2.0 Transformers plus Apache-2.0
  [PEFT](https://github.com/huggingface/peft/blob/v0.20.0/LICENSE); those code
  licenses do not replace the separate LFM weight license below.
- Compute plausibility is not a fit result. The immutable
  [BF16 file](https://huggingface.co/LiquidAI/LFM2.5-350M-Base/blob/9960764e30892e01f29a6dc23df2533fcd8bd5ae/model.safetensors)
  is 708,984,464 bytes, and LiquidAI describes LoRA as training roughly 1-2% of
  model parameters. A 2026-08-30 local `nvidia-smi` probe reports an RTX 4070
  Laptop GPU with 8,188 MiB and compute capability 8.9. This makes
  short-context, micro-batch LoRA credible locally. The actual compatibility
  smokes fit and completed, but they do not yet prove a full training batch,
  evaluator generation, or production latency envelope. As a deliberately
  conservative full-SFT planning inference, BF16
  weights and gradients plus FP32 master weights and two FP32 Adam moments cost
  16 bytes per parameter, or about 5.28 GiB for LFM before activations, CUDA
  workspace, temporary tensors, and allocator headroom. Implementations may
  use a different state layout, so the task must record measured peak VRAM.
  Full-weight SFT, especially at 32k context, must not be declared to fit the
  local 8 GB device or a 16 GB T4 from checkpoint size or this arithmetic alone.

**What Praxist can and cannot own:**

- Praxist 0.5.0 requires CPython 3.11+. Its pinned
  [`pyproject.toml`](https://github.com/sapientinc/PRAXIST/blob/92b785381ee13f9ea1435ba52024493c90db35ee/pyproject.toml)
  has a small core (`PyYAML`, `Jinja2`, `Pydantic`) and optional agent/Codex
  integrations; it does not depend on PyTorch, Transformers, PEFT, TRL, or a
  dataset package. Those task-owned dependencies must be locked separately.
- For this SFT task, Praxist would orchestrate the iteration rather than perform
  gradient training itself: parallel peers develop candidates, the task-owned
  evaluator turns each result into structured evidence, and the planning panel
  carries that evidence into the next generation until convergence or the
  declared budget ends
  ([pinned loop contract](https://github.com/sapientinc/PRAXIST/blob/92b785381ee13f9ea1435ba52024493c90db35ee/README.md#L208-L220)).
  The separate task project must therefore own the pinned trainer command,
  dataset manifests, candidate search surface, metrics, acceptance thresholds,
  and model-export validation; Praxist owns orchestration, evidence, scheduling,
  replay, and lifecycle, not those scientific choices
  ([ownership boundary](https://github.com/sapientinc/PRAXIST/blob/92b785381ee13f9ea1435ba52024493c90db35ee/README.md#L132-L139)).
- The documented setup is
  `python3 -m pip install --index-url https://pypi.org/simple
  "praxist[agents,codex]" && praxist setup --interactive --install-skills codex`.
  After readiness, `praxist --takeover --task-path <research-project>` creates
  or validates a separate task harness; `praxist resolve <task>` and
  `praxist doctor --task-path <task>` validate it without starting;
  `praxist start --task-path <task> --daemonize --json` launches; and
  `praxist status --json`, `praxist --monitor --latest`,
  `praxist stop <run_id>`, and `praxist resume <run_dir>` operate it.
  Installation alone neither chooses Scriber nor starts research, as the pinned
  [README](https://github.com/sapientinc/PRAXIST/blob/92b785381ee13f9ea1435ba52024493c90db35ee/README.md)
  and [Quickstart](https://github.com/sapientinc/PRAXIST/blob/92b785381ee13f9ea1435ba52024493c90db35ee/docs/getting-started/quickstart.md)
  state.
- **Operator/legal setup record:** the same Quickstart requires review of the
  exact Fair Source License, User Agreement, and data notice followed by an
  explicit agree-or-cancel choice whose version and digest are recorded.
  Optional product-usage consent is separate and not preselected. The operator
  explicitly authorized the required confirmations for this autonomous task;
  the local Praxist setup recorded acceptance of the required legal bundle and
  product-usage telemetry was declined. Revenue, distribution, attribution,
  and any commercial-license questions remain separate release gates.
- Before takeover, the task must expose an unchanged training/inference
  baseline, locally reachable data, and a metric with direction; Praxist refuses
  to invent or download missing prerequisites. This is the explicit
  [first-task contract](https://github.com/sapientinc/PRAXIST/blob/92b785381ee13f9ea1435ba52024493c90db35ee/docs/getting-started/first-task.md).
- A suitable Scriber task harness should keep teacher-generation/training data
  disjoint from sealed evaluation, compare candidates against both newly
  generated outputs from the exact current Cerebras prompt and the shipped V1
  model on those same fresh inputs, and score at least content preservation,
  additions/omissions, names/numbers/units/timestamps, punctuation and
  structure, forbidden answer/summary behavior, latency, memory, and GGUF size.
  By explicit operator instruction on 2026-08-30, every prior Scriber-polishing
  training, validation, challenge, prediction, and judge corpus is excluded
  from the new train/development/test sets. Only reusable code and schemas may
  be inspected; zero old examples or targets may enter the experiment.
- Praxist can centrally schedule explicit GPU profiles and passes exact GPU UUID
  and CUDA visibility into task descendants; it does not make a declared GPU
  profile real or scientifically equivalent to CPU execution. Its
  [scheduler contract](https://github.com/sapientinc/PRAXIST/blob/92b785381ee13f9ea1435ba52024493c90db35ee/docs/guides/central-resource-scheduler.md)
  requires measured VRAM/utilization envelopes and fail-closed device binding.

**GitHub Actions option:**

- GitHub's hosted GPU larger runner is one Tesla T4 with 16 GB VRAM, 28 GB RAM,
  4 CPU cores, and 176 GB SSD on Ubuntu or Windows
  ([runner specification](https://docs.github.com/en/actions/reference/runners/larger-runners)).
  This is credible for initial parameter-efficient SFT probes of either 354M
  LFM or 468M Boldt, but full-weight SFT and the chosen sequence/batch settings
  still need a real memory probe; the hardware table alone is not a fit result.
  The T4 is compute capability 7.5
  ([NVIDIA GPU table](https://developer.nvidia.com/cuda/gpus)), while CUDA
  requires compute capability 8.0 or newer for BF16
  ([CUDA floating-point contract](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/mathematical-functions.html#floating-point-data-types)).
  A T4 experiment must therefore smoke-test and use the same supported FP16
  policy for both A/B arms rather than copying the current `bf16=True` setting.
- Current pricing is $0.052/minute for Linux and $0.102/minute for Windows,
  rounded up by job; included minutes cannot pay for larger runners and larger
  runners are not free for public repositories
  ([pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing)).
  That is $3.12/hour or $6.12/hour respectively. Every GitHub-hosted job is
  capped at six hours
  ([Actions limits](https://docs.github.com/en/actions/reference/limits)), so a
  single maximum-length Linux GPU job costs up to $18.72 before storage and
  must checkpoint early enough to finish upload/validation before termination.
- The same limits page lists 500 MB artifact storage and 10 GB cache storage for
  GitHub Free, versus 1 GB artifact storage for GitHub Pro. The account plan is
  unconfirmed: the 937 MB source checkpoint exceeds the Free artifact quota and
  would leave almost no Pro headroom before a tuned BF16 checkpoint, reports,
  or optimizer states. Do not rely on Actions artifacts; use a separately
  authorized model registry/object store with resumable, hash-verified uploads,
  and treat Actions cache as evictable.
- GitHub repository/environment secrets can carry a teacher-provider key or a
  write-scoped model-registry token only when explicitly mapped into the job.
  Fork-triggered workflows do not receive repository secrets
  ([secret handling](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)).
  Training should therefore be manual `workflow_dispatch` on a protected
  revision/environment, never an untrusted pull-request workflow. Downloading
  the public Boldt source needs no Hugging Face token; publishing results does.
- A local self-hosted Linux/WSL Actions runner can use the user's GPU without
  hosted-GPU eligibility, but it is local compute, not GitHub online GPU credit.
  Praxist still requires the Linux task runtime and long jobs must survive
  machine restarts and preserve their run directory.

**GGUF, Scriber integration, and redistribution gates:**

- GGUF conversion is plausible but unverified. Scriber's pinned llama.cpp
  `b10158` converter registers `LlamaForCausalLM` and falls back to GPT-2
  vocabulary conversion when SentencePiece/Llama-HF vocabularies are absent
  ([pinned converter](https://github.com/ggml-org/llama.cpp/blob/f87067841bac583bc089a225382248d857791ca8/conversion/llama.py#L17-L32),
  [vocabulary path](https://github.com/ggml-org/llama.cpp/blob/f87067841bac583bc089a225382248d857791ca8/conversion/llama.py#L104-L140)).
  Promotion still requires conversion of the exact tuned checkpoint, tokenizer
  parity against Transformers, BF16/Q8_0 output comparison, and successful load
  and deterministic completion through the exact bundled `llama-server.exe`.
- LFM2.5 has stronger structural evidence against the exact same pin: b10158's
  [`conversion/lfm2.py`](https://github.com/ggml-org/llama.cpp/blob/f87067841bac583bc089a225382248d857791ca8/conversion/lfm2.py#L15)
  registers `Lfm2ForCausalLM`, its
  [native LFM2 graph](https://github.com/ggml-org/llama.cpp/blob/f87067841bac583bc089a225382248d857791ca8/src/models/lfm2.cpp)
  loads and executes the hybrid convolution/attention architecture, and its
  [tokenizer registry](https://github.com/ggml-org/llama.cpp/blob/f87067841bac583bc089a225382248d857791ca8/convert_hf_to_gguf_update.py#L143)
  names LiquidAI's LFM2.5-350M tokenizer. This confirms converter/runtime code
  paths, not the exact tuned artifact. Promotion still requires merged-adapter
  conversion, tensor/tokenizer/chat-template parity with Transformers,
  QAD-Q4_0 byte parity, and an installed Vulkan/CPU `llama-server.exe` smoke and
  stress run. Those technical gates were completed for the selected LFM arm,
  but they did not satisfy the later fresh-case product acceptance gate.
- The shipping model boundary uses catalog schema v3 with a hash-pinned
  `plain_completion_v1` prompt template, a fixed 384-token cap, and
  `plain_text_v1` output contract.
  [`manager.py`](../src/local_polishing/manager.py) sends that contract directly
  to `/completion` without `/apply-template` and without injecting KEEP markers
  into the raw training-distribution input. The existing schema-v1
  `chat_template_v1`/SST path remains only for exact legacy rollback identities.
  [`runtime.py`](../src/local_polishing/runtime.py) validates llama.cpp b10158's
  exact `stop_type` response: only `eos` or `word` may reach the output safety
  gate, while `limit`, `none`, prompt truncation, token-budget exhaustion, or
  malformed evidence return the unchanged transcript. The manager also has an
  explicit legacy-removal seam bound to the allowlisted repository, revision,
  variant, catalog identity, installation manifest, and managed path. It is not
  automatic: Gemma installations are inactive but are removed only through the
  explicit verified legacy-removal boundary.
- The bundled b10158 Vulkan path no longer assumes that the host's first
  enumerated adapter is the intended GPU. It strictly parses `--list-devices`,
  accepts only one unambiguous NVIDIA adapter, and verifies a second
  child-environment isolation probe before starting it as logical `Vulkan0`.
  Missing, malformed, ambiguous, or mismatched evidence fails over to CPU.
  b10158's exact `vk::PhysicalDevice::createDevice: ErrorExtensionNotPresent`
  startup failure permits one fresh process with
  `GGML_VK_DISABLE_BFLOAT16=1`; the packaged Production QAD factory now sets the
  same child-only flag on its first launch. A physical `Vulkan1` selected by the
  exact probe is isolated and exposed to llama.cpp as logical `Vulkan0`. The
  parent environment is never changed, and a failed compatibility launch
  proceeds directly to CPU.
- The PRAXIST-v2 evaluator now precomputes and verifies each arm's complete
  schedule hash and one common, gradient-accumulation-independent raw-example
  order hash. Candidate hyperparameters are bound to the training identity.
  Published child results are strict metadata projections: unknown top-level or
  nested fields fail closed, while runtime/determinism strings and the shared
  pair config cross the boundary only as hashes. Raw examples, targets,
  predictions, pair IDs, and token IDs are not PRAXIST result fields.
- The LFM-only PRAXIST run, all-2,000-pair SFT lineage, Production QAD recovery,
  GGUF export, and catalog binding are complete. The selected QAD-Q4_0 winner
  and its exact policy/license/manifest files are public at immutable revision
  `d64f8a14a09b2916000d969edd18bc411745e53a` and require no credentials. The
  earlier private/rejected artifact remains historical evidence only.
- The historical public-v7 Soniox/Azure/Cerebras chain is excluded from the
  selected winner. Its partial and failed attempts remain engineering evidence
  only. The active quality source is the independently bound, deterministically
  shuffled 2,000-pair German Word corpus with a 1,600/200/200 split; Production
  used all 2,000 parents plus their deterministic Identity and Noisy children.
- The Boldt repository declares Apache-2.0 but currently contains no separate
  `LICENSE` or `NOTICE` artifact in its pinned file tree. Apache 2.0 permits
  redistribution subject to providing the license, marking modified files, and
  retaining applicable notices
  ([official Section 4](https://www.apache.org/licenses/LICENSE-2.0#redistribution)).
  The bundled llama.cpp runtime is MIT-licensed and its copyright/permission
  notice must remain included
  ([pinned license](https://github.com/ggml-org/llama.cpp/blob/f87067841bac583bc089a225382248d857791ca8/LICENSE)).
- LiquidAI weights use the non-Apache
  [LFM Open License v1.0](https://huggingface.co/LiquidAI/LFM2.5-350M-Base/blob/9960764e30892e01f29a6dc23df2533fcd8bd5ae/LICENSE).
  It permits commercial use only while the legal entity and its controlled
  group remain below USD 10 million annual revenue; commercial use at or above
  that threshold is not licensed by that agreement. Redistribution of tuned or
  GGUF derivative weights must provide the license, mark modified files, and
  retain applicable copyright, patent, trademark, and attribution notices. The
  pinned model repository currently contains no separate `NOTICE` file, so the
  release manifest must preserve every applicable notice found in the actual
  conversion/training inputs rather than assume that none exists. Revenue
  status and any needed separate LiquidAI permission are release gates,
  independent of the Apache-2.0 Transformers code and MIT llama.cpp runtime.
- **Praxist output-license gate:** its Fair Source License 1.0 expressly
  defines model parameter updates and weight files as `Generated Output`. It
  permits free internal business use only while aggregate annual revenue and
  affiliates remain below USD 1 million, requires a Commercial License at or
  above that threshold, and requires the product-name attribution
  `Praxist by Sapient Intelligence` when generated output is made available to
  third parties
  ([generated-output definition](https://github.com/sapientinc/PRAXIST/blob/92b785381ee13f9ea1435ba52024493c90db35ee/LICENSE.md#L51-L55),
  [attribution and revenue terms](https://github.com/sapientinc/PRAXIST/blob/92b785381ee13f9ea1435ba52024493c90db35ee/LICENSE.md#L113-L140)).
  **Revenue gate passed, attribution retained:** on 2026-09-01 the owner
  explicitly confirmed aggregate annual revenue of USD 0 including affiliates.
  The model repository is public, anonymous access is enabled, and both the
  publication package and Scriber UI retain the exact attribution
  `Praxist by Sapient Intelligence`. Do not bundle Praxist itself into Scriber
  without separate legal review and any required written consent.

**Historical preregistered Boldt-versus-LFM A/B contract (superseded by the
operator's LFM-only decision):**

- Freeze both source commits above, the exact UTF-8 teacher prompt supplied by
  the operator in this task, one newly generated hash-manifested
  transcript/teacher-target corpus, one deterministic preprocessing version,
  and source-grouped train/development/sealed-test splits. Near-duplicates,
  alternate corrections, and excerpts of one original dictation remain in one
  split. Teacher targets are generated and validated once before the final split
  is sealed; after sealing there is no regeneration, repair, or additional
  sampling for test cases. The sealed test targets are unavailable to training,
  Praxist peer prompts, evaluator development, hyperparameter/model selection,
  quantization choices, and stopping decisions. Open them once for exactly one
  frozen final candidate per arm; do not retrain, retune, or select again after
  seeing them. No legacy `ml/scriber_polishing` data artifact is eligible.
- Run both arms in one separately pinned and parity-smoked training environment
  that can load both exact tokenizers, with the same supported arithmetic on the
  same GPU class: BF16 may be a common local RTX 4070 track, while a T4 track
  must use common FP16. Use the same plain-text completion instruction and
  teacher targets, raw examples, example order/exposures, maximum training
  sequence length,
  optimizer-update count, optimizer/scheduler policy, seed list, number of
  Praxist candidates, and hard GPU-minute/cost ceiling. The primary corpus must
  fit both serialized tokenizers without truncation under the common 2,048-token
  cap; report each tokenizer's non-padding supervised-token count rather than
  changing or duplicating examples to force token equality. Hash the
  architecture-specific serialization separately.
- If LoRA is used, preregister architecture-specific target modules and match
  the total trainable-parameter budget, or its fraction of base parameters,
  within a declared tolerance; rank alone is not equivalent across Boldt's 24
  attention layers and LFM's mixed 16-layer topology. Keep alpha/dropout and
  the search grid fixed, disclose exact trainable counts, and do not give one
  arm replacement trials after OOM or invalid evidence. A separate fixed
  4,096-plus product track may test LFM's longer context and Boldt's frozen
  chunk policy without contaminating the equal-budget headline result.
- Keep newly generated results from the exact frozen Cerebras prompt and the
  shipped Gemma V1 runtime as untouched reference baselines on the same fresh
  raw inputs. Score those inputs for protected names, numbers,
  units, dates, timestamps and speaker labels; additions, omissions, meaning
  changes, forbidden answering/summarizing, output-contract/safety rejection,
  punctuation, structure, and blind randomized human preference with ties.
  Critical content-preservation failures are hard gates and cannot be averaged
  away by stylistic gains.
- Evaluate merged BF16 and identically produced Q8_0 artifacts through the
  exact pinned Windows runtime. Report quality beside cold/warm p50/p95 latency,
  throughput, peak VRAM/RAM, model/download/installed bytes, start time, and
  failure rate on the same machine and concurrency. Select only a Pareto-valid
  candidate that beats the frozen product baseline without a critical-quality
  regression; otherwise retain the existing catalog and report no winner.

**Current promotion boundary:** Gemma Q8_0/BF16 are no longer active catalog or
settings choices. Their exact identities remain available only for verified
rollback/removal. The sole LFM QAD winner is public at immutable revision
`d64f8a14a09b2916000d969edd18bc411745e53a`, installs anonymously, and passed
the bound 200 Long plus 400 Short regression cases exactly. The PRAXIST owner
revenue confirmation is complete. QAD-Q4_0 remains the only authorized
quantization, with no parallel comparison. Scriber never falls back to a
retired model or mutable local path.

Signing/updater:

- Tauri updater wiring, weekly non-blocking frontend checks, local update cache,
  one-day deferral, per-version skip, manual install/restart, and release-note
  access are implemented for installed builds.
- Free Tauri updater artifact signing is wired through GitHub Actions
  secrets/variables. Each production update still needs the signed installer,
  `.sig`, `latest.json`, and `SHA256SUMS.txt` published to the public GitHub
  Release endpoint, plus publication evidence.
- Official tag releases now fail closed unless updater signing and RSA
  Authenticode signing are both configured. The workflow imports an ephemeral
  PFX, Tauri signs the desktop/NSIS surfaces, and the PyInstaller backend is
  signed before its runtime manifest is generated. Repository owners still
  need to provision the public-trust certificate and signing secrets.
- `run_hybrid_release_readiness.ps1 -RunReleaseBuild` can now run the Windows
  release build as an evidence producer and reuse its Authenticode validation
  report, but it still depends on Authenticode signing when that gate is
  enabled and on public HTTPS updater publication.
- The layered Python backend currently uses exact SHA-256 inventories for
  cache/corruption integrity, not an independent same-user trust boundary. As
  with the previous PyInstaller `onedir` runtime, a process that can rewrite
  installed files can also rewrite their local manifests. Future hardening
  should sign the application/runtime manifests with a release key and verify
  them against a public key embedded in the frozen launcher before importing
  physical application code.
- Rebuilding an expired Python runtime cache now uses an exact
  Windows/Python-3.14.7 constraints graph. The graph does not yet carry wheel
  hashes, so independently rebuilt generations still require the existing
  wheelhouse and runtime-inventory evidence rather than being assumed
  byte-identical.

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

Five-hour Meeting evidence:

- Accelerated tests cover exactly 600 30-second checkpoints / 18,000 seconds,
  bounded checkpoint growth, corrupt-latest-base recovery, duration-scaled
  provider budgets, lease renewal, and 600-segment hierarchical analysis.
- This proves the implemented storage/workflow invariants, not five hours of
  physical WASAPI capture, provider availability, AEC quality, or real-machine
  thermal/memory/disk stability. A production five-hour claim still requires an
  installed Windows soak with representative Teams/Zoom/Meet routes and the
  selected cloud providers. The existing 60-minute recording and two-hour
  release matrix remain minimum evidence rather than substitutes for that soak.
- The green five-hour preflight is deliberately limited to the currently
  bounded Soniox/Soniox Async, AssemblyAI, Azure MAI, and Local ONNX final
  routes. Soniox reaches the target exactly at its fixed 300-minute ceiling;
  there is no advertised headroom beyond it. Deepgram accepts large files, but
  Scriber's synchronous `/v1/listen` route remains labelled as not
  five-hour-verified until its processing-window risk is removed. The
  configured Voxtral Mini Transcribe 2 (`2602`) route is capped at three hours,
  the older `2507`/unknown override at 30 minutes, and Gladia pre-recorded at
  135 minutes. Smallest, Speechmatics, OpenAI, Gemini, and Groq also remain
  available for shorter Meetings but are labelled as not five-hour-compatible
  until their active whole-track transport is proven or replaced with a safe
  chunked route.
- Local Sherpa diarization remains release-routed to 60 minutes until its
  multilingual long-file matrix is complete. Longer Meetings should use a
  provider with native batch diarization or visibly complete transcription
  without local speaker fallback.

Provider latency:

- Cloud STT finalization can dominate stop-to-text latency.
- The installed replay/scoring path now measures externally visible text for
  5-, 15-, 30-, and 60-second Microsoft, Soniox, and Speechmatics fixtures.
  Speechmatics keeps the real Batch-v2 adapter/parser and full WAV validation
  behind a network-free one-shot transport, so the capture-time WAV candidate
  can be compared OFF/ON without credentials or billable requests. The final
  matched installed runs each passed 60/60 samples and 12/12 cleanups. Azure's
  Stop-to-provider p50 improved in every duration by 19.5% to 70.8%, but
  canonical visible-text p50/p95 still regressed in some durations. Speechmatics
  likewise regressed canonical 15-, 30-, or 60-second series. Both candidates
  therefore remain default-off under the no-regression rule.
- A bounded live Speechmatics route check now covers the same four durations
  for both batch WAV and realtime raw PCM. It verified non-empty results and
  measured realtime Stop-to-provider-final at 374-494 ms, but it used one
  sample per duration and did not include installed target observation. Treat
  it as compatibility evidence, not a promotion-quality speed claim.
- Shared HTTP pooling and exact pass-through/transcode selection remove known
  avoidable local work. The local replay establishes their installed product
  path, while a live-provider speed claim still needs controlled cloud evidence.
- Rust `wav_pcm16_virtual` remains a capture-lab candidate with
  `productionReady=false` and `artifactExposed=false`. The separate production
  `wav_pcm16_file_v1` path now streams a bounded lease file during capture,
  validates it at the Tauri/Python boundary, feeds the exact Speechmatics batch
  WAV upload, and cleans it on explicit release or shell shutdown. It activates
  only for a complete current `batch_v2`/`enhanced`/verified-WAV/default-endpoint
  route snapshot; custom or stale routes retain the PCM-spool fallback. This
  closes the artifact-ownership implementation gap. Local codec labs rejected
  the current flacenc/rezin/ruopus/Shine candidates; in-process LAME was 15.68%
  to 41.72% faster than FFmpeg in its local series but still lacks installed
  provider WER/CER and licensing evidence. Additional codecs therefore remain
  experimental rather than a performance-evidence blocker for Issue #18.
- A future Rust-side VAD path is worth evaluating in the audio sidecar. The
  referenced Silero Rust examples use either `ort` with an ONNX model path or
  `wavekat-vad` with compile-time model embedding and 16 kHz frame handling;
  this should be measured against the current Pipecat VAD path before adding
  another packaged model/runtime path.

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
  overlaps prewarm and active capture for adoption and exposes
  `adoptedPrewarm.handoffMode=overlap-capture-start-before-prewarm-stop`. The
  interim 2026-06-29 overlap avoided stopping prewarm before a replacement
  client became live. On 2026-07-15 this was replaced by true in-place
  promotion: after format and actual-endpoint validation, capture attaches its
  frame pipe to the original running prewarm `IAudioClient`, drains snapshot and
  bounded tail once as PREBUFFER, and continues Live frames without reopening
  the microphone. The hardened five-cycle physical Windows app smoke measured
  capture-start responses of `6.069–9.704 ms` and first-Live waits of
  `4.333–10.411 ms`; all cycles reused the client, matched the endpoint,
  confirmed stop and idle resume, and had zero sequence/protocol or
  prebuffer-after-live errors. Mismatch and early handoff failures remain
  fail-closed on a fresh client without replaying incompatible prebuffer audio.
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
