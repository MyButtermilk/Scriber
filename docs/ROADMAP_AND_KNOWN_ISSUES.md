# Roadmap And Known Issues

Last verified: 2026-07-12

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
- YouTube extraction pins `yt-dlp[default,deno]==2026.7.4`, bundles matching EJS
  scripts plus Deno, leaves player-client selection to yt-dlp, and validates an
  audio stream and container integrity with ffprobe before provider upload.
  Corrupted/incomplete transfers are retryable download failures rather than
  successful downloads that later fail inside Azure MAI.
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
- Settings updates validate persisted text sizes before mutation; invalid numeric
  tuning values fall back safely instead of crashing provider or runtime paths.
- Unicode export filenames use a safe ASCII fallback plus RFC 5987 UTF-8 metadata.

Meetings:

- The eager Meetings tab now owns a durable capture-to-analysis state machine,
  recovery, canonical/live revisions, notes, editable action items, cited chat,
  exports, playback, retention, and webhook delivery surfaces.
- Native meeting capture uses one Rust audio sidecar for mic plus loopback,
  pinned `aec3-rs` echo cancellation, a shared monotonic timeline, three durable
  tracks, health monitoring, pause/resume gaps, and checksum-validated chunks.
- Final transcription uses provider-native timestamps for Soniox, AssemblyAI,
  Deepgram, and Mistral when available. Providers without structured timing use
  an explicitly identifiable estimated fallback.
- Outlook Calendar has public-desktop PKCE, Windows Credential Manager refresh
  token storage, incremental Graph delta sync, periodic refresh, and offline
  backoff. Settings exposes configuration state, connect, sync, disconnect,
  last sync, and the next event. A production connection still requires the
  official public Entra client ID to be supplied to the release.
- Optional WeSpeaker embeddings are local, opt-in, hash-pinned, and excluded
  from exports/support bundles. Settings lists local profiles and allows users
  to name or delete individual profiles; confident linked Meeting speakers are
  updated through the backend profile API. Merge and incorrect-match split
  remain available in the Meeting workspace.
- The Meeting start check consumes provider profiles, safe capture/render
  endpoint inventory, and dismissible local detection. Interrupted meetings can
  either finalize saved chunks or resume fresh capture.
- The start check can run an explicit 1.5-second mic/loopback/AEC route test;
  it returns only level/activity statistics and never persists or uploads audio.
- Post-meeting progress, Overview and Notes views, independent track mute
  controls, all four exports, and preview-confirmed webhook delivery are exposed
  in the workspace.
- The pre-React boot shell resolves the stored/system theme synchronously and
  uses the high-contrast dark Scriber mark before the application bundle mounts;
  the real-browser smoke freezes and screenshots this exact dark startup frame.
- Independent Soniox live streams now supervise send/receive failures, reconnect
  with bounded exponential backoff, emit one preview-gap marker per outage, and
  report reconnect/recovery state visibly while durable local capture continues.
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
  `src/web_api.py::upload_meeting_import` is process-local. When
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

### `BUG-MTG-016` - Resolved P2 - Playback controls are rendered from transcript presence, not audio availability

- **Reproduction:** open a Meeting after audio retention has purged its assets,
  or a single-track import. The player and both Mic/System toggles are still
  shown. Selecting a missing source calls an endpoint that correctly returns
  `404`.
- **Root cause:** Meetings gates playback on `detail.segments.length > 0` and
  always constructs all three source URLs. It does not derive available routes
  from `detail.audioAssets` or `audioPurgedAt`.
- **Fix boundary:** expose only verified assets, disable impossible mixes/source
  switches, and show a durable `Audio no longer retained` state after purge.
- **Required regression gate:** no-assets, purged, microphone-only, system-only,
  and full-mix component cases.

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
| `UX-MTG-05` | P1 | Explicit calendar-event selection and participant snapshot | The correct event and recipients are attached before recording |
| `UX-MTG-06` | P1 | Confidence-driven speaker review | Ambiguous speakers can be resolved quickly and safely |
| `UX-MTG-07` | P1 | Playback follow, match navigation, and bookmarks | Review becomes one synchronized timeline workflow |
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
visibly stale analysis outputs. Automatic playback following, templates, and
global library search remain unimplemented until usage evidence justifies them.

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

**Problem**

A wrong language, STT model, or diarization choice currently forces a duplicate
import or rerecording even when verified canonical audio is retained.

**Interaction specification**

- Add `Retranscribe` to Models used and the Meeting overflow. The dialog exposes
  profile/provider/model, language, native/local diarization, local/cloud data
  handling, estimated cost when knowable, and why an option is unavailable.
- Run a durable background job with stage progress, cancel/retry, and a run
  history. Compare old/new transcript revisions, then explicitly activate one.
  Offer analysis regeneration as a separate confirmed step.

**Backend/data and tests**

Persist immutable transcription runs keyed by audio digest and a frozen route
snapshot, with parent run, progress, result digest, and error. Reuse canonical
FLAC/provider derivatives; never overwrite the active revision on failure.
Cover idempotent same-route retry, crash resume, cancel, missing credentials,
purged audio, native versus fallback diarization, and activation driving Search,
Ask, export, and playback citations.

#### `UX-MTG-05` - Explicit calendar event and participant snapshot

**Problem**

Selecting only the nearest/current event is ambiguous for early starts,
overlapping calls, back-to-back meetings, and personal blocks. A wrong match can
also address an email draft to the wrong people.

**Interaction specification**

- The Outlook preflight card lists the current and next three plausible events
  plus `No calendar event`. Selection shows title, time, organizer,
  participants, and join link before Start.
- Freeze the chosen event for the Meeting. Later calendar changes may be shown
  as an update, but must not silently replace recipients or speaker context.

**Backend/data and tests**

Start with explicit `calendarEventId`; persist an immutable snapshot containing
event id, subject, organizer, attendees, time range, join URL, ETag, and sync
time. Keep heuristic matching only as a visible fallback when no explicit event
was selected. Test overlap, cancellation/edit, offline start, duplicate/missing
addresses, Outlook disconnect after start, recovery, and that email preview uses
exactly the frozen participant set.

#### `UX-MTG-06` - Confidence-driven speaker review

**Problem**

Voice profiles and speaker rename/split exist, but low-confidence or conflicting
matches have no focused review queue. Users should not have to inspect every
segment or understand clustering internals.

**Interaction specification**

- Show `Review N speakers` only for low-confidence, low-margin, conflicting, or
  unknown matches. A side sheet presents a few short local audio examples,
  transcript examples, source, and plain-language confidence.
- Map to a frozen Outlook participant, an existing Voice profile, a new named
  person, or Unknown. Support apply-to-selected/all and one-step undo. Linking a
  reusable biometric profile is explicit opt-in, never a side effect of rename.

**Backend/data and tests**

Persist match candidates with model/revision/confidence/margin/review state and
atomic bulk reassignment. Protect the Mic `You` identity by default. Test
overlap, same names, no calendar, purged audio, deleted/opted-out profiles,
transaction rollback, high-confidence no-banner behavior, and that accepted
changes update transcript, Search, analysis-stale state, and export.

#### `UX-MTG-07` - One synchronized review timeline

**Problem**

Timestamp click-to-seek exists, but playback does not visibly follow and scroll
the current segment. Local search only filters the list; users cannot navigate
`3 of 12` matches. Timestamped notes are not presented as a first-class bookmark
flow.

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

- Tauri updater wiring, weekly non-blocking frontend checks, local update cache,
  one-day deferral, per-version skip, manual install/restart, and release-note
  access are implemented for installed builds.
- Free Tauri updater artifact signing is wired through GitHub Actions
  secrets/variables. Each production update still needs the signed installer,
  `.sig`, `latest.json`, and `SHA256SUMS.txt` published to the public GitHub
  Release endpoint, plus publication evidence.
- Authenticode validation exists, but real signing requires a certificate or
  cloud-signing provider.
- `run_hybrid_release_readiness.ps1 -RunReleaseBuild` can now run the Windows
  release build as an evidence producer and reuse its Authenticode validation
  report, but it still depends on Authenticode signing when that gate is
  enabled and on public HTTPS updater publication.

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
- Local app optimization should be guided by hot-path metrics.
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
  `adoptedPrewarm.handoffMode=overlap-capture-start-before-prewarm-stop`. On
  2026-06-29 this was tightened after a visible Always-On-Mic privacy-light
  blink was still observed following a longer idle period: when adopted WASAPI
  prebuffer blocks exist, the old `PrewarmSession` is moved into the capture
  writer and is stopped only after the replacement WASAPI `IAudioClient.Start()`
  succeeds. Early handoff failures stop the deferred session with explicit
  reasons (`captureStartFailed` or
  `captureWriterFinishedBeforePrewarmHandoff`) instead of silently dropping
  idle prewarm. This is the current mitigation for privacy-light continuity and
  minimum hotkey latency with `SCRIBER_MIC_ALWAYS_ON=1`; it is not yet a true
  same-stream handoff.
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
