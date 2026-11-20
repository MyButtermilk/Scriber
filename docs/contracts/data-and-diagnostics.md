# Data and diagnostics

- Runtime data belongs under `SCRIBER_DATA_DIR`, not the install directory.
- Legacy runtime data migration must not overwrite existing app-data files.
- Explicit Voice Library enrollment must use the existing private
  `audioCaptureStart`/`audioCaptureStop` Rust/WASAPI path under the shared
  native-audio admission lease. Capture mono 16 kHz PCM into a short bounded
  in-memory buffer only, reject unreadable, short, quiet, or clipped samples,
  require clear speech activity across at least two enrollment windows, and
  clear the capture buffer after local WeSpeaker inference and creation of one
  bounded audit clip on every success, failure, and cancellation path. Keep
  exactly one mono 16 kHz PCM16 WAV reference per profile, centered and capped
  at four seconds, in the local SQLite store so Settings can play it for an
  explicit name-to-voice check. This reference is local biometric audio: serve
  it only through a short-lived token-protected capability with `no-store`,
  exclude it from exports, logs, diagnostics, and support bundles, and delete
  it with the profile/library. Explicit enrollment replaces the reference with
  the newest confirmed sample; automatic Meeting learning may fill a missing
  reference but must not silently replace one. The pinned ONNX export has fixed batch size one:
  infer each accepted window as waveform `[1, 160000]` plus mask `[1, 589]`,
  normalize each result, and persist only their normalized centroid. Validate
  the native start response as mono 16 kHz
  `pcm_i16_le` before reading its frame pipe. Never use WebView `MediaRecorder`, a Python
  capture fallback, a hidden Meeting, or a temporary enrollment audio file.
  Persist only one normalized aggregate enrollment centroid plus count,
  effective-weight, resultant-norm, and time metadata on the local speaker
  profile; reconstruct its weighted sum only in memory for exact incremental
  and merge math. Never persist individual enrollment samples beyond the one
  bounded audit reference described above. Profile
  recomputation, Meeting deletion, merge, and split
  must preserve that explicit seed correctly. Whole-library deletion and
  Meeting-finalizer registration share a durable SQLite enabled gate under
  `BEGIN IMMEDIATE`, so a late finalizer cannot recreate deleted voice data;
  model downloads stay in unique verified staging files and may be promoted
  only while that gate remains enabled, with a post-promotion recheck for
  cross-process deletion. Status and profile collection combine the durable
  gate with the local opt-in before exposing Voice Library state. Preview
  reads/saves and every mutation that creates, renames, or associates biometric
  data must consult both gates; enrollment, rename, merge, split, and finalizer
  registration perform the durable check inside the same `BEGIN IMMEDIATE`
  transaction as their writes. Individual-profile and whole-library deletion
  are privacy-reducing erasure operations: keep them available after opt-out
  without a consent-gate precondition, but map an unknown delete result to a
  redacted 503. Any unknown gate/read/save/write result likewise fails closed
  with a redacted 503 and no PCM response. Status and collection report the
  disabled state without profile data; other gated public operations map a
  disabled gate to 409. Cancellation
  during model promotion still completes the post-check and deletes a promoted
  model when consent is false or unreadable;
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
- Parse the complete Meeting-start command before audio admission. Boolean and
  integer JSON fields are type-strict. Start/resume cleanup and device-test
  cleanup must settle every native/local owner plus the audio lease inside one
  repeated-cancellation barrier. Stop reserves a closed finalizer task gate
  before touching native capture; only a durable `finalizing` commit may open
  that gate and release a pending request cancellation.
  The controller supervisor owns the whole Stop settlement through SQLite
  writer contention. HTTP waits at most 30 seconds before reporting pending
  local recovery; request cancellation still waits for ownership settlement.
  Backend shutdown stops subsequent local retries after the active SQLite call,
  retaining the claim and durable phase for startup recovery.
  Failed imports settle the durable error and uncommitted staging removal
  within one cancellation barrier. Startup removes terminal upload leftovers
  only when no committed original or Meeting workspace owns their directory.
- Retry the initial Meeting finalization by projecting its completed canonical
  head when route, reprocess generation, and source-audio identities match.
  Canonical stage evidence binds every input track's PCM hash, samples,
  duration, and timeline origin, including tracks without speech. A projection
  failure must not repeat an already committed provider request.
- Startup recovery must preserve workflow phase: only `starting`, `recording`,
  and `paused` become resumable `interrupted` capture. `stopping` and
  `finalizing` become `finalization_failed`, and `analyzing` becomes
  `analysis_failed`; never offer capture resume for a post-capture crash.
- Keep audio format layers separate. LocalVQE v1.3, Silero, Smart Turn, live STT, and
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
  The canonical commit must project content, preview, current duration, and
  terminal status in the same transaction; a later terminal-parent CAS is an
  idempotent completion boundary, not a metadata repair path. Bind the probed
  source-media duration into StageResult evidence for source-free recovery.
  Older completed attempts may promote only a placeholder duration through the
  artifact store after revalidating the exact attempt and canonical head; never
  rewrite content or a non-placeholder duration there.
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
  proven. In particular, `openrouter_stt` must remain non-five-hour-capable
  until that exact OpenRouter route has its own bounded long-input evidence.
  Reject an imported or finalized track above a known hard duration before the
  provider call, and keep the live UI's final-30-minute limit warning wired to
  the same central capability.
- Live Mic, Meeting start/resume/reconnect, Meeting device tests, and Voice
  enrollment share one admission lock plus the persisted singleton audio claim.
  Claim before prewarm/device awaits and recheck Meeting state under the lock.
  The claim uses opaque ids, a 60-second expiry, a 15-second heartbeat, and
  CAS-safe transfer/release. Paused Meetings retain ownership. A Meeting becomes
  durable only after its `recording` transition commits; await that owner
  operation outside the capture setup lock so a concurrent loss handler can use
  the same lock to stop and settle capture. Stop, terminal failure, watchdog
  failure, and graceful shutdown release only after native stop and recorder
  cleanup are confirmed. Do not let startup steal a still-valid claim from
  another controller, and do not rely on `_is_listening` alone. A heartbeat that
  races pending-to-Meeting transfer adopts the newer same-controller generation.
  Foreign supersession invokes the generation-bound loss handler. Live Mic must
  win the claim before pipeline construction, and stop must release it before
  clearing `_is_stopping`; otherwise a queued toggle can leave a never-started
  pipeline behind.
- Persisted attempt route values are authoritative for language and exact model.
  Batch providers must not read mutable `Config.LANGUAGE` or model defaults for
  queued/retried/recovered work once a RouteSnapshot exists.
  File ingestion additionally freezes one immutable `FileUploadPlan` before the
  multipart stream is accepted: exact route/provider, source kind, raw-ingest
  limit, and prepared-audio limit. Persist that same plan with the queued job
  and restore it without consulting changed Settings. Source ownership transfers
  when `start_file_transcription` is invoked. The transcript id is also the exact
  durable job id. The shared resume lane serializes admission through adoption,
  exact reconciliation, pending-job scheduling, cancellation settlement, and
  deletion intent so no queued row can be owned by two runners. A confirmed
  enqueue commit keeps the source and is adopted; a confirmed miss cleans every
  admission projection; an unreadable exact state keeps the source, parent,
  mapping, and retry marker while excluding that row from ordinary pending scans.
  Never reselect a provider between upload admission and execution. YouTube has
  no local source at enqueue, but the same post-commit cancellation boundary must
  finish mapping, history publication, artifact-parent persistence, and
  exactly-once scheduling before cancellation is delivered. Terminal recovery
  uses a restart-safe projection protocol. A complete parent is authoritative
  and is persisted before the exact job moves to its terminal state with
  `terminal_projection_pending = 1`; failed or stopped work may use that same
  job flag as a durable no-replay intent while the parent CAS is unavailable.
  Cleanup releases an owned File source only after those durable states agree
  and clears the flag last. A CAS loser adopts the authoritative parent instead
  of saving a stale in-memory record. Startup drains flagged rows with a fair
  keyset cursor and bounded backoff so one poison page cannot starve later work;
  schema migration arms legacy terminal rows for the same recovery. If neither
  the parent nor the job can record a cancellation intent, return the fixed
  redacted 503 response and never schedule the provider. Transcript deletion
  first persists `Deleting`, then removes the job row, owned source, and parent
  in that order. Startup resumes this intent and must never resurrect or schedule
  the deleted job.
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
  status, and due date without duplicating the regenerated item. Resolve an
  exact, unambiguous generated transcript-speaker label to `owner_speaker_id`
  and project its current
  Meeting speaker name when reading tasks, so speaker renames propagate without
  overwriting unmatched free-text owners.
- Meeting analysis keeps the single-call fast path only at or below 48,000
  prompt characters and 60 minutes. Longer transcripts map stable chunks of at
  most 30,000 characters and 30 minutes with concurrency two, then use a
  deterministic hierarchical reduce with fan-in three. Persist map/reduce cache
  entries by algorithm/schema/model/chunk digest, repair only the malformed
  unit, and preserve exact segment citations and timestamp-derived chapter
  boundaries. When equal chapters from different map chunks are deduplicated,
  recompute their start/end from every merged citation before exposing playback
  links. Its context-local request budget defaults to 900 seconds through
  `SCRIBER_MEETING_ANALYSIS_TIMEOUT_SEC`; do not widen the ordinary 240-second
  summary budget to solve long-Meeting timeouts.
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
- Local polishing uses the pinned product identity in `src/local_polishing/catalog.py`.
  Preserve attribution and anonymous install. Validate the allowlisted cache path,
  byte length and SHA-256
  before atomic promotion under `SCRIBER_DATA_DIR/models`, and never execute
  remote code. Downloading or updating a model must not silently activate it;
  only a fully verified and release-gated model may be selected.
  Cancellation must prevent verification/activation and keep only the bounded
  resumable staging state; a later explicit retry may reuse it. Shutdown must
  stop the local inference runtime and leave no active model process.
- Under the active Scriber data directory (`SCRIBER_DATA_DIR` for the backend),
  `logs/` contains `tauri-backend.log`, `tauri-shell.log` and
  `backend-crash-metadata.jsonl`.
- Debug console uses `/api/runtime/logs`, `DELETE /api/runtime/logs`, and
  `/api/runtime/support-bundle`; post-processing debug state is exposed through
  `/api/runtime/post-processing-diagnostics` and the `postProcessing` hot-path
  metrics snapshot. In Tauri, support bundles use native Save As plus the same
  opaque export-token Open file/Open folder/Copy file boundary as other
  exports; never accept a WebView-supplied path for those follow-up actions.

- Diagnostic logging is an explicit persisted boolean shared by Python and the
  native shell. `diagnostic_preferences.py` applies it behind the settings lock
  and a cancellation barrier; the atomic `.env` write is its durable commit.
  Off closes and drains diagnostic writers, including optional metric workers,
  while shell child pipes continue draining without writing files. Re-enabling
  appends to existing logs. Transcript and durable job writes are unaffected.
  Live Mic failures retain bounded exception types and code-only stack/cause
  locations plus content-free native startup counters and durations. Keep this
  metadata visible through the Debug Console projection; never include exception
  messages, frame locals, source lines, device identities, or absolute paths.
  Diagnostic collection is best effort and must not prevent failure delivery or
  capture cleanup. Native microphone failures must not count against the selected
  STT provider's circuit or be presented as that provider's error.
  `operation_diagnostics.py` records authenticated mutations by route template,
  never bodies, query strings, concrete IDs, or raw exception messages. Keep
  HTTP completion separate from background-job completion and distinct latency
  stages separate in the UI. Debug snapshots cache only bounded redacted entries;
  malformed or message-less JSON must never expose raw structured extras.
