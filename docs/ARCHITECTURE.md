# Scriber Architecture

Last verified: 2026-07-15

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
- Layered packaged backend: a stable PyInstaller onedir runtime plus a physical,
  checksummed `backend/app` overlay containing current first-party Python code.

The frozen runtime starts `backend_runtime.launcher`, validates its runtime
manifest, validates the exact file set and SHA-256 of the application overlay,
prepends `backend/app` to `sys.path`, checks the staged Scriber version, and only
then imports `src.backend_worker`. The runtime cache therefore contains no
`src` package and can survive ordinary backend-code changes; the complete
composed sidecar remains independently cacheable for exact hits.

The installed app is local-first. The backend binds to loopback, and the Tauri
supervisor injects a per-run session token for local control endpoints.

## Main User Workflows

Live mic:

1. Tauri registers the configured global hotkey.
2. Hotkey calls backend live-mic endpoints. A second optional post-processing
   hotkey calls the dedicated live-mic post-processing endpoint; the normal
   hotkey always keeps plain STT output.
3. Without a valid warm lease, Python resolves the microphone under the
   PortAudio guard. Always-on idle prewarm instead binds the already-resolved
   PortAudio/native route to its exact prewarm ID, so the hotkey path does not
   repeat device inventory or compatibility probing.
4. A matching idle WASAPI prewarm stream is promoted in place: Rust validates
   the currently requested actual endpoint, attaches the capture frame pipe,
   emits the rolling snapshot plus bounded handoff tail as prebuffer, and then
   continues live audio from the same running `IAudioClient`. Format, endpoint,
   or handoff failures fall back to a fresh replacement client without replaying
   incompatible prebuffer audio.
5. Pipecat/provider pipeline processes audio.
6. Transcript text is injected into the active app and saved to SQLite. When a
   session was started through the post-processing hotkey, pipeline raw-text
   injection is suppressed, the completed live transcript is sent through the
   configured LLM prompt using `${output}`, and the processed result is pasted
   after provider finalization. File and YouTube jobs do not use this path.
7. Frontend receives versioned WebSocket state, audio, transcript, and history
   events.
8. On stop, Always-on capture hands the endpoint back to a replacement idle
   prewarm before releasing the recording stream. Provider finalization then
   continues without turning off the Windows microphone indicator. Only real
   Pipecat `SegmentedSTTService` instances and providers explicitly classified
   as `vad_flush_before_end` finalize a VAD segment before EndFrame. Segmented
   HTTP services finish inside the awaited flush; asynchronous realtime commits
   continue immediately when their newer final-generation event arrives, with
   no fixed settle delay.
   Terminal-buffered providers such as Azure MAI finalize through EndFrame
   without entering that path. The old capture pipe is marked as a pending
   external handoff before Tauri starts the replacement, so its expected EOF is
   not reported as a Rust mid-session audio failure.

YouTube:

1. Frontend search or URL lookup calls backend YouTube endpoints.
2. Backend asks yt-dlp for manual subtitles first, then automatic captions. The
   persistent `youtubePreferCaptions` setting defaults to enabled in the writable
   runtime data directory. If no usable track exists, the job falls back to the
   configured STT provider and audio workflow.
3. The fallback uses pinned current `yt-dlp`, bundled EJS challenge scripts,
   Deno, and bundled ffmpeg/ffprobe. yt-dlp owns current YouTube player-client
   selection; Scriber does not force stale client names. Every downloaded file
   must pass ffprobe audio/structure validation before it can reach a provider.
4. Persistent job metadata tracks the caption preference, download, media
   preparation, transcription, summary, retry, resume, cancel, and completion.
5. Transcript and summary are saved as a `youtube` transcript. A pending summary
   remains a processing state in Recent videos even after transcript persistence.

File:

1. Frontend uploads audio/video using multipart request.
2. Backend enforces upload limits and writes chunks off the event loop where
   practical.
3. Video/audio is normalized through ffmpeg as needed.
4. Provider transcription and optional summarization run as a persistent job.
5. Transcript and summary are saved as a `file` transcript.

Meetings:

1. The eager **Meetings** tab creates a durable meeting row with a frozen
   transcription mode and provider snapshot before capture. Only one meeting
   may own capture at a time. The mode is selected in Settings, not on the
   start surface: `live_final` adds best-effort live text and then a canonical
   final pass; `final_only` records locally and opens no live STT connection.
2. One crash-isolated Rust audio-sidecar process opens WASAPI microphone and
   loopback sources at 48 kHz. Pinned `aec3-rs` consumes the loopback render
   reference and produces a cleaned microphone stream before all three tracks
   are downsampled to 16 kHz and stamped on one monotonic timeline.
   Its private endpoint inventory covers capture and render flows; the
   token-protected Meeting API exposes only friendly labels and hashed IDs for
   explicit route selection. Its explicit local device test reuses the same
   dual-capture/AEC path for 1.5 seconds, computes only bounded RMS/peak
   statistics, then destroys the frames without persistence or provider calls.
   On pause/stop, the relay also returns bounded render-active energy counters
   and a raw-to-clean microphone attenuation value. Python preserves at most 20
   sanitized native-stop snapshots in meeting metadata; relay errors become a
   boolean health flag and raw error text is not retained. This is diagnostic
   evidence, not a perceptual quality score: release calibration uses a
   dedicated remote-only render-active capture session, while double-talk is
   evaluated separately.
3. Python persists raw mic, AEC-clean mic, and system PCM into 30-second WAV
   chunks. Cross-filesystem publication uses a recoverable two-phase protocol:
   close and fsync a deterministic `.partial.wav`, hash it, persist a `prepared`
   chunk row, atomically rename it, then mark the chunk `complete` and replace
   its checksum-protected transcript checkpoint in one SQLite transaction.
   Startup reconciles verified `prepared+partial` and `prepared+final` states;
   missing or mismatched artifacts become explicit gaps/quarantine. Legacy
   rowless final WAVs are adopted only when canonical sequence, WAV shape,
   digest, and start offset are unambiguous. New code never creates rowless
   final WAVs.
   Schema-v3 checkpoints store source-specific durable frontiers without copying
   the complete transcript into every row. Every twentieth 30-second sequence is
   a compact full base; intervening rows contain only new segments plus their
   per-source frontiers. A delta also carries a redundant prior-base fallback,
   so recovery can bypass one corrupt newest base. Superseded payload bodies are
   pruned to bounded tombstones while row metadata remains available. A final
   live segment is included only through that segment source's complete
   mic/system frontier; one longer track cannot claim durability for another.
   The scalar cutoff is derived compatibility metadata, not proof that all
   sources reached it. On startup Scriber walks backward to the newest
   SHA-256-valid recoverable base/delta chain and restores only missing live
   segments before marking the meeting interrupted. It never overwrites
   transcript updates that survived the crash. Live captions use clean mic plus
   system audio; raw mic remains a recovery/debug source and is never sent as
   duplicate speech.
   After commit, the backend publishes only redacted checkpoint metadata and
   committed final segments over the versioned WebSocket contract. The frontend
   patches exact Meeting caches incrementally. Import progress is monotonic and
   uses HTTP polling only while the shared WebSocket is disconnected. Selection,
   reconnect, and terminal transitions reconcile only the affected collection,
   capability, detail, or import keys; child caches and paid ephemeral speaker
   suggestions are not discarded by a broad prefix invalidation. The complete
   Meeting is not polled during steady live capture.
   Native Capture and the durable recorder start before optional live STT on initial
   start, resume, interrupted recovery, and default-device reconnect. Each
   Soniox live source in `live_final` then runs as a best-effort preview with an independent
   reconnect supervisor. Missing credentials, initialization failure, or a
   later disconnect never blocks the recorder: bounded live queues may drop
   preview frames, one visible gap is emitted per outage, reconnect attempts use
   bounded exponential backoff, and provider-relative timestamps are rebased
   onto the shared meeting timeline after recovery. The first failure or
   live-queue overflow is surfaced immediately as a visible degraded-preview
   state; it never implies loss from the upstream durable recorder.
   Soniox realtime diarization is enabled on the system-audio preview stream;
   the microphone preview remains the local `You` stream. Final live tokens are
   split into contiguous speaker runs with their own timestamps instead of
   collapsing a provider turn to its majority speaker. Raw speaker ids are
   normalized by first appearance and namespaced to one WebSocket epoch because
   Soniox may reuse them after reconnect. Canonical post-meeting transcription
   remains authoritative.
   Recorder write failures are independently watched: disk-full stops capture
   visibly as `meeting_storage_full`, retains completed chunks, and leaves the
   open partial for startup quarantine instead of publishing it as complete.
   In `final_only`, this entire provider-preview branch is skipped on start,
   pause/resume, crash recovery, and device reconnect; durable recording,
   checkpoints, finalization, speaker processing, and analysis are unchanged.
   Optional Smart Turn V3 runs only on the clean microphone preview stream. It
   may hold and merge an early Soniox endpoint when the local ONNX analyzer
   reports an incomplete phrase, but it never gates durable recording, system
   audio, or the canonical post-meeting transcript. Analyzer failures fall back
   to provider endpointing and remain visible only as redacted counters.
4. Pause arms the Python readers for an intentional disconnect before releasing
   native capture, commits any valid in-progress WAV through the normal
   two-phase path, and records a timeline gap. This ordering is required because
   Windows named pipes may report `OSError` rather than a clean end-of-stream;
   resume must open fresh pipes at the next durable sequence instead of
   colliding with the preceding `.partial.wav`. A rejected native command
   disarms the boundary, while unplanned reader/storage failures still reach the
   capture watchdog. Stop and default-device reconnect use the same ordering.
   Stop then validates chunk hashes/headers, builds FLAC multitrack and Opus
   playback assets, and runs final provider transcription. Provider-native word
   or utterance times become canonical segments; explicitly marked estimated
   timing is used only when a provider has no structured timestamps. The
   verified duration is passed into the frozen route so upload, batch, and poll
   timeouts scale through the 18,000-second target while remaining hard-capped.
   The UI combines this capture/storage result with the selected final STT
   route: Soniox/Soniox Async, AssemblyAI, Azure MAI, and Local ONNX are
   currently marked five-hour-capable. Deepgram's pre-recorded API accepts up to
   2 GB and has no audio-duration ceiling, but Scriber's current synchronous
   `/v1/listen` request is not five-hour-verified against the provider's request
   processing window. Soniox is capped at exactly 300 minutes; Gladia
   pre-recorded at 135 minutes and the configured Voxtral Mini Transcribe 2
   (`2602`) at three hours. The older `2507` or an unknown Mistral override is
   conservatively limited to 30 minutes. Other whole-track routes remain usable
   for shorter Meetings but never receive a false green long-session state.
   Import probing and finalizer
   admission reject tracks above these hard limits before encoding/provider
   work, while live capture shows a countdown in the final 30 minutes.
   Cross-track echo removal uses a timeline sweep over overlapping mic/system
   segments rather than comparing every pair, preserving the same overlap and
   similarity rules with bounded long-transcript cost.

Audio format is intentionally tiered rather than conflated with model input:

- AEC3, Silero, Smart Turn, live STT, and the durable capture boundary consume
  PCM frames. Compression is never inserted into this latency-sensitive branch.
- Recoverable 30-second work chunks remain 16-kHz mono PCM until their two-phase
  publication and the canonical transcript commit are proven durable.
- The long-lived reprocessing archive is the verified Matroska/FLAC multitrack.
  Playback has timeline-aligned Opus derivatives for the mix, clean microphone,
  and system track; otherwise the existing Mic/System mute controls would retain
  a hidden dependency on the large final WAVs. FLAC remains lossless for future
  STT/diarization and Opus is not treated as canonical model evidence.
- Finalization prepares sources sequentially. It concatenates one source to a
  task-scoped PCM WAV, hashes its canonical decoded samples, encodes a
  `*.work.flac`, decodes that FLAC again to prove sample-count and PCM-hash
  equality, and removes the full WAV before preparing the next source. This
  bounds the normal peak to checkpoint chunks, compressed working tracks, and
  at most one full-length PCM source instead of retaining every concatenated
  WAV simultaneously.
- Provider upload derivatives are created from the verified working FLAC.
  Optional WAV-only consumers such as local voice embeddings receive one
  job-scoped decoded WAV per track and delete it in a `finally` boundary. A
  failed FLAC encode keeps the source WAV for retry and never replaces a prior
  verified working track with a partial file.
- After artifact commit and archive verification, redundant WAV chunks/final
  WAVs move through durable `purge_pending -> purged` only after the Meeting is
  durably `ready`. Maintenance resumes interrupted purges after rechecking the
  canonical head and every required archive/playback hash. Retry and optional
  local consumers materialize only the required FLAC stream as a task-scoped
  WAV when their runtime contract cannot consume FLAC directly.

Archive verification is semantic, not just a file checksum. Each lossless track
manifest carries source, stream index, sample count, timeline origin, and a hash
of canonical decoded `s16le` PCM. Before WAV purge, the archived stream is
decoded and must reproduce that sample count and PCM hash; ffprobe codec checks
and an archive-file SHA-256 alone cannot detect a swapped or truncated track.

Direct 30-second WebM/Opus capture is not the default: WebM is a container, Opus
is lossy, and independently encoded chunks add codec pre-skip/end-trim mapping
to the first-class Meeting clock. A compact Opus-only archive may be offered
later only after multilingual STT and speaker-diarization quality tests prove an
acceptable regression and the UI discloses loss of lossless reprocessing.

Provider transport is separate from both capture and archive. A frozen route
may create a provider-specific, job-scoped lossy upload derivative; notably
Soniox async prefers WebM/Opus over a large WAV upload. The derivative is made
from verified local evidence, recorded as
`webm_opus_task_derivative` in the immutable RouteSnapshot, deleted in a
cancellation-safe provider-release boundary, and never promoted to the lossless
archive. Meeting finalization applies this policy independently to each missing
track; a recovered track StageResult therefore never repeats either encoding or
the paid provider call. An individual microphone or system call may validly
return no speech; that track contributes no synthetic segment when another
canonical track has usable units. Finalization still fails if every available
canonical track is empty, a provider request raises, or returned text cannot be
normalized into usable segments. Surviving units retain their original source
and Meeting-clock intervals.

Local speaker separation is a second immutable evidence layer rather than a
mutation of the provider result. `transcription_track_derivations` binds the
derived units to the exact parent track StageResult, frozen route/worker
manifest, and a canonical SHA-256 payload. A retry reuses the validated
derivation without rerunning ONNX; canonical artifact inputs retain both the
provider track result and its `track_derivation` provenance.

The storage budget is explicit: 16-kHz, signed-16-bit mono PCM is 115.2 MB per
hour per track (decimal), so raw mic + clean mic + system peak at 345.6 MB/hour
before temporary finalization copies. A 64-kbit/s Opus mix is about 28.8 MB/hour;
FLAC size is content-dependent and is never estimated as a fixed retention
guarantee. The Meeting capability response treats six GiB of currently free
runtime-volume space as the five-hour readiness floor and also reports an
estimated capture duration after reserving two GiB for finalization. This is a
preflight guardrail, not a retention-size promise. It becomes a green five-hour
state only when the selected final STT route is also explicitly verified for
18,000-second input. Release soak evidence records both steady-state and peak
bytes.
   Capability routing prefers native batch diarization. When the selected STT
   model has no speaker attribution, File, YouTube audio, Meeting finalization,
   and imported meeting recordings share one optional Sherpa-ONNX 1.13.3
   fallback. Pyannote 3.0 INT8 segmentation, 3D-Speaker embeddings, and native
   clustering produce local speaker turns which are aligned to exact provider
   word timestamps. Without word timestamps, text is distributed over the real
   speech intervals. The static Rust worker is a signed backend resource; only
   the checksum-pinned models and their notices live below
   `SCRIBER_DATA_DIR/models`. PyTorch, Torchaudio, TorchCodec, Lightning, and
   Pyannote's Python runtime remain outside the standard sidecar.
   `POST /api/meetings/import` validates the chosen profile and uploaded media,
   preserves the source as `original-*`, creates a durable 16-kHz mono system
   track, and enters this same canonical finalization path. The frontend exposes
   file metadata, profile, native-versus-local speaker routing, upload progress,
   preparation progress, and cancellation before navigating directly into the
   new workspace. Imports therefore receive the same meeting search, playback,
   analysis, exports, retry recovery, and email output as captured meetings.
   Interrupted capture phases can resume into fresh capture and optional STT
   sessions while retaining the prior hashed device selection and recording one
   explicit `crash-recovery` gap. A process exit during `stopping` or
   `finalizing` becomes `finalization_failed` instead, so the UI retries from
   saved audio and never offers to append new capture to a stopped meeting.
   A completed Meeting in either `ready` or `analysis_failed` may be processed
   again from retained evidence. Speaker-only refresh never calls an STT
   provider: it verifies the persisted Opus playback asset and its SHA-256,
   reruns local Voice Library matching, and swaps the identity observations
   atomically. Microphone segments may use the deterministic microphone-speaker
   identity even when a provider did not emit a speaker id; system segments are
   eligible only when diarization produced a nonempty speaker id. Full
   retranscription freezes the provider and model currently selected in
   Settings, reopens the verified lossless FLAC archive, and preserves the old
   canonical transcript until the new artifact commits. If the process exits
   after that artifact commit but before the Meeting projection, retry projects
   the already-paid artifact instead of calling the provider again. Recovery
   reuses partial or recoverable attempts only when workload, source
   track, provider, model, and language match the frozen reprocess route. A
   provider switch changes the frozen provider/model pair atomically; rollback
   restores both, and model-specific duration checks read that same pair.
   Temporary decoded Voice-matching WAVs live only under the bounded runtime temp area,
   are removed in `finally`, and crash leftovers are cleared on next startup.
   The Process again
   dialog reports each mode independently when retained audio, credentials,
   duration support, or the local Voice model is unavailable.
   Meeting Technical details are evidence-driven rather than inferred from the
   current Settings. Pyannote/Sherpa-ONNX is reported only from a persisted
   local diarization derivation, native diarization only from parsed provider
   speaker evidence, Silero only from the VAD evidence used for that Meeting,
   and Smart Turn V3 from the completed live-session snapshot including its
   bounded analysis and failure counts. Older Meetings without versioned
   evidence say that the component was not recorded instead of guessing.
5. The canonical transcript is immutable input to versioned MeetingAnalysisV1
   output. Notes, speaker renames, action-item edits, cited chat, exports, and
   webhook delivery are separate durable work objects. Analysis stays on a
   single request only when the prompt is at most 48,000 characters and the
   Meeting is at most 60 minutes. Longer Meetings map stable, timestamped chunks
   capped at 30,000 characters and 30 minutes with concurrency two, then reduce
   those validated evidence objects hierarchically with fan-in three. Map and
   reduce results are persisted by algorithm/schema/model/chunk digest, so retry
   regenerates only a changed or malformed branch. Schema repair is scoped to
   that branch, and the final deterministic merge preserves exact cited segment
   ids and derives chapter times from their canonical timestamps.
   Every REST and live WebSocket segment carries `startMs`, `endMs`, and the
   derived `durationMs`. Transcript rows and AI citations seek the corresponding
   microphone, system, or playback-mix asset directly. Offsets at or above one
   hour render as unambiguous `H:MM:SS`; each row exposes start, end, and duration
   while retaining click-to-seek. The Meeting view offers immediate speaker/text
   filtering; the token-protected `/search` endpoint uses FTS5 with
   chronological neighboring segments and falls back to the live revision until
   a canonical transcript exists.
   JSON and Markdown exports preserve the normalized workspace directly. PDF
   and DOCX receive separate structured summary and timestamped-transcript
   render inputs, avoiding duplicate document headers. Email preview and RFC
   822 `.eml` drafts reuse the same summary template, populate valid unique
   Outlook participant addresses, and support body-only, Markdown, PDF, or DOCX
   attachment modes without claiming that a missing attachment exists. Drafts
   use SMTP CRLF plus `X-Unsent: 1`, so Outlook opens an editable draft and
   retains the selected MIME attachment. Email and document labels follow
   conservative transcript-language evidence first, with analysis output,
   Meeting, and configuration fallbacks for short or language-neutral text.
   The Save or share surface also exports the finalized 64-kbit/s Opus playback
   mix. Tauri streams this authenticated, allowlisted local response directly
   to the native Save As destination with an atomic replace, avoiding the
   64-MiB WebView/IPC byte-array boundary for long Meetings. Other Tauri
   exports use the same native Save As dialog and atomic replacement;
   the resulting Open file/Open folder actions accept only a bounded,
   process-local opaque registry token, never a path supplied by the WebView.
   Browser builds retain the ordinary download behavior.
6. Optional speaker recognition is local and opt-in. The pinned WeSpeaker ONNX
   model is downloaded after installation, hash-verified before first use, and
   never included in transcript/export payloads. Settings can enroll a named
   speaker through the shared Rust/WASAPI microphone path. The short mono sample
   remains in a bounded memory buffer, is quality-checked, becomes one normalized
   sample centroid from at least two speech-active windows locally, and is then
   cleared without writing or uploading audio. The pinned model has fixed batch
   size one, so each accepted window is inferred separately before its normalized
   embedding enters that centroid. The native start contract must
   confirm mono 16 kHz `pcm_i16_le` before the private frame pipe is consumed.
   The profile stores only a normalized aggregate enrollment
   centroid plus count/effective-weight/resultant-norm/time metadata, never its
   individual samples. The weighted sum is reconstructed only in memory, which
   retains exact order-independent incremental and merge math. Meeting-derived
   observations can refine the
   combined centroid, while deletion, merge, and split preserve the deliberate
   enrollment seed. A durable SQLite enabled gate serializes whole-library
   deletion against late Meeting-finalizer registration, preventing deleted
   voice data from being recreated. The optional model downloads to a unique,
   checksum-verified staging file; promotion and a cross-process post-check use
   the same enabled gate so an in-flight download cannot restore deleted data.
   After finalization, speaker identity resolution is deliberately layered.
   Scriber first proposes unique local Voice Library matches, then assigns the
   microphone track to the connected Outlook account where possible. Only an
   explicit user action may ask the configured LLM about unresolved speakers.
   That request contains opaque speaker/participant ids, participant names, and
   short email-redacted transcript excerpts; Outlook email addresses are never
   included. Calendar names, speaker labels, and transcript excerpts are all
   wrapped as untrusted data rather than instructions, and every LLM proposal
   remains unconfirmed until the user accepts it. A confirmed link updates
   Meeting-local speaker labels but does not silently enroll or alter a reusable
   biometric profile. The same review surface is available without Outlook and
   accepts a free person, team, room, or shared-microphone label. Such a label is
   explicitly Meeting-local: it never renames a Voice Library profile, creates
   an Outlook identity, or enters the export recipient list. If diarization
   produced two durable profiles for one real person, the Meeting surface can
   invoke the existing explicit Voice Library merge. The user chooses the
   identity to keep, confirms the irreversible merge, and Meeting-local manual
   labels remain intact while reusable profile evidence is combined.
7. Outlook Calendar uses public desktop PKCE. Official builds embed the public
   Entra application ID in the Tauri shell, validate it as a canonical non-nil
   GUID, and forward it to the backend worker without logging it. The system
   browser returns through the registered
   `http://localhost:<port>/api/calendar/outlook/callback` loopback shape, whose
   dynamic port follows the supervised backend. The refresh token stays in
   Windows Credential Manager while the backend keeps access tokens in memory
   only. One unclaimed PKCE state is reused while authorization is pending, so
   repeated Connect actions reopen the same browser flow instead of creating
   competing callbacks. A rejected or corrupt refresh credential produces the
   structured `reauthRequired` state: the app stops claiming it is connected,
   clears in-memory access tokens, preserves the last local calendar snapshot,
   and asks the user to reconnect. A verified Disconnect removes that credential and then clears the
   local account, delta cursor, and cached events; immutable event snapshots in
   existing Meetings remain available.
   Sync reads `/me` and retains both `mail` and `userPrincipalName` as account
   aliases. Graph `calendarView/delta` is paginated without unsupported
   `$select`; every response page is staged before events and the final cursor
   are committed atomically. Daily event queries receive local-midnight and
   next-local-midnight boundaries already converted to UTC by the browser. This
   makes 23- and 25-hour DST days correct without bundling Python `tzdata`.
   The Meeting preflight can refresh and list every cached event for that local
   day, select one event with its title, time, location/join link, organizer,
   participant names and addresses, or deliberately select no event. It shows
   refresh freshness and the last sync failure; if a selected event disappears
   after refresh, its link is cleared with an explicit warning while a manually
   edited title is preserved. Nearby automatic suggestions prefer an active
   non-all-day event, then an upcoming event, then a recently ended event, with
   all-day context last. Start
   carries either an explicit `calendarEventId` or explicit `null`; an id is
   resolved only in the authenticated local cache and frozen as an immutable
   Meeting snapshot. Later calendar edits, syncs, account disconnects, and LLM
   suggestions cannot rewrite that evidence.
   Email previews and exports derive recipients only from the frozen event,
   independently of speaker suggestions and confirmed mappings. Address
   validation and deduplication exclude the connected user's `mail`/UPN
   aliases, declined invitees, and room/resource attendees.
   Paid LLM speaker suggestions remain client-ephemeral until confirmation.
   Confirming one assignment patches that speaker only so unresolved suggestions
   remain available without a duplicate provider request.

### Durable Meeting import protocol

Meeting import uses a durable job rather than keeping one multipart handler
alive through upload, ffmpeg, persistence, and finalizer scheduling.

```text
created -> receiving -> received -> probing -> preparing -> waiting_for_workspace
        -> committing -> finalizing -> completed
created..waiting_for_workspace -> cancel_requested -> canceled
any processing state -> failed
failed(with Meeting id) -> finalizing (explicit Meeting retry only)
```

`meeting_import_jobs` stores an opaque import id, state, sanitized display name,
expected/received bytes, source SHA-256, relative staging path, selected profile
snapshot, optional Meeting id, durable cancel flag, redacted error code/message,
and timestamps. It never stores an absolute path. The source body is streamed to
`meeting-imports/<id>/source.part`; completion fsyncs the file, atomically
renames it to `source.<ext>`, and only then persists `received` as the durable
commit marker. Probe and normalization write new
`.part` files and use the same commit rule.

`waiting_for_workspace -> committing` allocates and persists the final Meeting
UUID before the Meeting row is inserted or either artifact is moved. That UUID
is the recovery manifest: a restart reuses it, accepts either the staging or
the deterministic `meetings/<meetingId>/import` directory, verifies both sizes
and hashes, and idempotently recreates the chunk/finalization handoff. It never
creates a replacement Meeting for the same claimed import.

The REST contract is deliberately small:

- `POST /api/meeting-imports` accepts filename, byte size, title, language, and
  profile id and returns `201` with the import id and upload URL.
- `PUT /api/meeting-imports/{id}/content` streams only the binary body. This
  removes multipart field-order ambiguity. Exactly one PUT may claim a
  `created` job; neither an interrupted nor a committed upload can be
  overwritten in place.
- `GET /api/meeting-imports/{id}` returns the durable phase and bounded progress.
- `GET /api/meeting-imports` returns active and recent failed imports so the
  Meetings tab can reconstruct progress, retry/cancel actions, and Meeting links
  after a WebView or application restart. Browser-local import ids are never the
  recovery source of truth. The collection omits staging paths, hashes, probes,
  and provider snapshots. A lost upload response invalidates this collection but
  never implicitly deletes the accepted server job.
- `DELETE /api/meeting-imports/{id}` durably requests cancellation only before
  the workspace claim and returns after upload/ffmpeg work has exited. From
  `committing` onward it returns `409` plus `meetingId`; the Meeting workspace
  owns the artifacts and its explicit retry/discard lifecycle applies.

WebSocket `meeting_import_progress` carries `apiVersion`, import id, phase,
progress in `[0,1]`, received/expected bytes, optional Meeting id, and a bounded
status label. It carries no file path or transcript text. Startup recovery
removes stale uncommitted `.part` files, requeues `received` through `committing`
jobs idempotently, reconciles `committing` jobs via their manifest/Meeting id,
and honors `cancel_requested` before doing more work. Failed and canceled work
has its owned staging directory removed only after its task barrier exits.

Import origin is first-class. Imported media uses `origin=imported` and does not
fabricate `consentConfirmed=true`; that field describes capture-time consent
only. The normalized Meeting owns the original source and 16-kHz work track
after commit, and its normal audio-retention policy removes both.

### Isolated Rust diarization worker contract

`scriber-diarization-sidecar` is a separate static Rust executable, not linked
into the capture sidecar or shell process. The executable is built and shipped
as a versioned resource of the signed Scriber installer/updater. Only its large
models and model licenses are an optional post-install component. This keeps
executable trust, rollback, antivirus reputation, and application compatibility
on the normal release channel instead of inventing a second remote executable
channel.

The signed package maps the staged backend resource tree into the installed
backend directory. The frozen-runtime allowlist therefore contains exactly
`tools/diarization/scriber-diarization-sidecar.exe` below that backend root. A
signed-build manifest beside it pins filename,
size, SHA-256, worker/protocol version, Sherpa version, and static link mode.
`build_tauri_backend_sidecar.ps1` builds the locked standalone crate with the
pinned Sherpa 1.13.3 static-MT archive, writes that manifest, and stages exactly
the EXE plus manifest under backend `tools/diarization`. The executable cache
(`build/rust-diarization-sidecar-cache`) and verified upstream archive cache
(`build/sherpa-onnx-archive-cache`) are independent from the Python backend,
Tauri Rust, and live-audio caches. The base-package smoke rechecks the manifest
digest and size, `--version`, `--self-test`, the PE import table, and absence of
both optional ONNX models.
The optional data-directory component uses schema 2 and pins that worker digest
plus both models and every license/provenance file. Status verification and
hashing run on a worker thread and are cached by file identity; ordinary REST
status reads never synchronously rehash models on the aiohttp loop. Development
discovery is limited to the separate native crate's Cargo target or an explicit
source-checkout-only executable override.

Its stdin request is:

```json
{
  "schemaVersion": 1,
  "jobId": "opaque-id",
  "audioPath": "validated-local-wav",
  "segmentationModelPath": "validated-local-onnx",
  "embeddingModelPath": "validated-local-onnx",
  "clustering": {"numSpeakers": null, "threshold": 0.9},
  "limits": {"maxDurationMs": 7200000, "maxResidentBytes": 1073741824}
}
```

Paths are accepted only after the parent canonicalizes them below the component
or job roots. Stdout contains exactly one bounded JSON result with engine/model
versions, sample rate, duration, speaker count, and sorted
`{startMs,endMs,speaker}` turns. Stderr is diagnostic metadata only and must not
contain paths or transcript text. `--version` and `--self-test` do not load user
audio. The process assigns itself to a Windows Job Object with the requested
memory ceiling; the parent also enforces timeout and kill-on-shutdown.

The worker protocol has a hard defense-in-depth ceiling of two hours of 16-kHz
mono PCM and 1 GiB resident memory. Initial product eligibility for local
fallback is more conservative: 60 minutes until a real 60-minute multilingual,
multi-speaker soak proves memory, runtime, and cluster quality. Longer inputs
continue transcription but must use native provider diarization or visibly omit
local speaker separation. Future windowing requires global embedding clustering;
per-window speaker numbers must never be concatenated as global identities.

Profiles may carry an optional explicit `expectedSpeakerCount`. It is never
inferred automatically from Outlook attendees because invitees and actual
speakers are not equivalent. The clustering threshold remains an internal,
pinned value (`0.9`) rather than an end-user tuning control until a representative
corpus justifies another default.

The pinned ERes2Net model card declares training on approximately 10,000
speakers of 16-kHz Chinese audio. This is provenance, not proof of quality for
every language or a legal conclusion. Release promotion therefore requires a
held-out multilingual matrix covering German, English, mixed German/English,
varied accents and pitch ranges, and overlapping speech.

Every canonical transcript segment exposes `alignmentQuality` with one of:

- `exact_word`: words were aligned against local turns using provider word
  timestamps;
- `provider_segment`: a real provider time interval was assigned as one unit;
- `estimated`: text was apportioned without token-level evidence.

UI, exports, and API responses preserve this field. `estimated` timing is
visibly disclosed and is never advertised as word-accurate. Provider capability
flags are enabled only when an adapter parser and fixture prove the exact payload
shape; provider marketing support by itself is insufficient.

Capabilities are only preflight/request hints. The post-response authority is a
normalized transcription-evidence envelope containing provider, exact model,
requested response shape, parser version, canonical timed units, and explicit
facts for word timing and speaker attribution. Native diarization is considered
successful only when that active parser produces real speaker-labelled
intervals. A declared provider capability must never suppress local fallback
when the returned payload lacks that evidence.

### Canonical transcript artifact contract

File, YouTube audio, timed YouTube captions, captured Meetings, and imported
Meetings must converge on one durable transcript pipeline:

```text
RoutePlan -> immutable RouteSnapshot -> durable NormalizedStageResult
  -> optional local diarization -> immutable CanonicalTranscriptArtifact
  -> UI / summary / export / compatibility projections
```

`transcripts.content` is a deterministic compatibility rendering, not the
canonical record. The canonical record is an immutable, versioned artifact with
ordered segments. Every segment carries a stable id, source track, start/end in
integer milliseconds, text, speaker key/label, timing origin, speaker origin,
and `exact_word`, `provider_segment`, or `estimated` alignment quality. During
the migration, Meeting canonical segments are projected atomically with the
same ids; new citations bind `{artifactId, segmentId}`.

The enqueue-time route plan freezes user intent and the exact fallback route.
Immediately before provider or caption work, an immutable route snapshot freezes
workload, source track, provider, exact model, transport, language, response
shape, requested timestamp/diarization mode, parser id/version, redacted request
options, and the local worker/model manifest. Settings changes cannot mutate a
running or recovered attempt. API keys, signed source URLs, and clear-text custom
vocabulary never enter public snapshots, logs, or support bundles.

The durable attempt state machine is:

```text
queued -> resolving_source -> source_ready -> transcribing
  -> provider_result_ready -> diarizing? -> canonicalizing
  -> committing -> completed
```

Every transition is a compare-and-swap on the previous state and state version.
The validated normalized provider/caption result is committed before optional
diarization. Recovery from `provider_result_ready` therefore resumes without a
second cloud call. A canonical commit is one `BEGIN IMMEDIATE` transaction: it
checks the expected head generation, writes artifact/segments/inputs, advances
the head by CAS, updates compatibility projections, and completes the attempt.
A stale attempt becomes `superseded` and cannot replace a newer artifact.
Artifact begin, provider-stage persistence, canonical commit/FTS projection,
and Meeting track-stage persistence execute as coarse worker-thread phases so
large transcripts do not block aiohttp. Cancellation observes every started
SQLite phase through its actual durable boundary; after a successful commit,
legacy `TranscriptRecord` projection is completed on the event-loop thread.

Route snapshots never persist API keys, signed URLs, bearer material, or plain
custom vocabulary. They persist only a normalized vocabulary digest and safe
shape metadata. Before the first provider call, execution may use the current
private vocabulary only when its digest matches the frozen snapshot; a mismatch
requires a new attempt and snapshot rather than silently changing an existing
request. Once `provider_result_ready` is durable, recovery no longer needs that
private input because it continues from the normalized stage result.

Stable segment ids use a provider-native stable id when available; otherwise
they hash transcript id, source track, start/end, canonical speaker key, and
NFKC/whitespace-normalized text. Only a deterministic chronological occurrence
index resolves collisions. Artifact version is deliberately excluded so
unchanged segments survive re-finalization and citations remain stable.

Timed JSON3 captions use `tStartMs` plus `dDurationMs`; VTT parses every valid
`-->` interval. Caption cues are provider-segment timing evidence and never
audio-speaker evidence. If a selected caption response has no valid timed cues,
YouTube falls back to its frozen audio route. File/YouTube audio is initially
`processing_only`: purge follows task release through `purge_pending -> purged`,
while a durable asset tombstone explains unavailable playback. Maintenance
replays a crash-stranded pending purge using only a runtime-root-contained file
path, then clears that private path in the recovered tombstone. Public source
URLs must never contain absolute local paths.

The additive persistence boundary is `transcription_route_snapshots`,
`transcription_attempts`, `transcription_track_stage_results`,
`transcription_stage_results`,
`canonical_transcript_artifacts`, `canonical_transcript_segments`,
`canonical_transcript_heads`, `canonical_artifact_inputs`, and File/YouTube
`transcript_source_assets`. Legacy history is not fully migrated at startup;
new work dual-writes artifacts plus projections, while legacy plain text receives
only an explicitly `estimated` compatibility view.

The shared implementation lives in `src/transcript_artifacts.py` and
`src/data/transcript_artifact_store.py`. File and YouTube runners pass the
snapshot's exact model/language/private vocabulary values into
`ScriberPipeline.execution_route`, so queued work no longer rereads mutable
global settings during the provider call. The normalized provider StageResult
is durable before optional local diarization begins. Attempt leases are renewed
by heartbeat without incrementing the workflow state version; recovery claims
the latest unleased StageResult for that transcript and skips provider work.
Empty StageResults are terminal invalid evidence rather than recoverable poison.

Meeting finalization checkpoints each microphone/system provider response in
`transcription_track_stage_results` before starting the next track. A retry
claims the partial attempt and invokes only missing tracks, then materializes one
aggregate StageResult and CanonicalArtifact. Its stable artifact segment ids are
projected unchanged into `meeting_segments`, so cited analysis, Meeting search,
global transcript FTS, and playback links share identity. Local diarization may
be rerun from the normalized recovered track intervals; it never requires a
second cloud request. The final compatibility transcript is rendered only by
ArtifactStore; Meeting analysis updates summary state without rewriting content.
The Meeting-specific artifact lease lasts 30 minutes and is renewed every five
minutes with bounded retry and cancellation-safe cleanup, so a legitimate
five-hour provider job cannot be taken over merely because one request outlives
the short general workflow cadence.

Ready canonical Meeting segments may be corrected without overwriting their
provider provenance. Each edit or undo appends an immutable
`meeting_segment_edits` row, advances the Meeting-wide transcript edit version,
updates the canonical projection and FTS index in one transaction, and emits a
versioned `meeting_transcript_edited` event. Generated outputs snapshot the edit
version they used; older outputs remain available but are explicitly stale until
the user regenerates them.

Live-preview timing also preserves source-clock gaps. The bounded live STT
queue may drop preview frames under backpressure, while durable capture remains
lossless. Each successfully sent provider-audio span is therefore mapped to its
original Meeting-clock interval. Provider token timestamps are translated
through this piecewise mapping; a single connection-start offset is insufficient
after a drop. Exact discontinuities map token starts to the right span and token
ends to the left span. The spans are coalesced while clocks remain contiguous,
so normal long meetings do not accumulate one mapping row per audio frame.
Preview shutdown is bounded by one total deadline. Its stop marker is inserted
without waiting for queue capacity; a full best-effort queue may lose one
preview frame so finalization and application shutdown can still cancel the
provider tasks and close the WebSocket. This never affects the durable recorder,
which owns the audio before it enters the preview queue.

### Workflow ownership and admission

Process-local task dictionaries are observability caches, not durable locks.
Every transcription/finalization attempt receives a persisted attempt id,
monotonic state version, lease owner, and bounded lease expiry. State changes
use compare-and-swap on attempt id plus version. A second controller may renew
or take over only an expired lease; a CAS loser exits as `superseded` and must
never mark the winner's job failed. Analysis and canonical commit retain their
distinct phases so recovery after a canonical transcript never repeats STT.
The persistent File/YouTube job queue uses status-qualified SQL updates for
claims and terminal transitions. Exactly one worker can claim a queued job, and
a late completion cannot overwrite a canceled or failed row; direct
`queued -> completed` reconciliation remains an intentional idempotent path.

Meeting and canonical transcript FTS5 projections bind FTS `rowid` to the
corresponding base-table `rowid`. Versioned migrations rebuild the index and
its triggers atomically, while startup parity checks and delete/update triggers
use rowid instead of scanning unindexed text identifiers. Meeting detail reads
hold one explicit WAL read transaction until every constituent row set has
been captured, then decode JSON and assemble the response after releasing the
snapshot.

Task creation uses reservation gates even within one process: reserve the
meeting task slot synchronously, commit the corresponding durable state, then
open the gate. Cancellation before gate-open must either roll the state back or
let the matching reserved worker own it; an open `finalizing`/`analyzing` state
without an owner is forbidden. Done callbacks remove a task only when the map
still points to that exact task.

Live Mic, Meeting start/resume, Meeting device test, and shutdown share one
audio-admission coordinator. The process lock is acquired before any prewarm or
device await, and one SQLite singleton lease records opaque owner kind/id,
controller id, CAS generation, and expiry. A 15-second heartbeat renews the
60-second lease while capture is owned. Every path rechecks both the claim and
Meeting state while holding the lock. Paused Meetings retain ownership by
product policy; stop, terminal start/resume failures, capture-watchdog failure,
and graceful shutdown release it. An ungraceful controller death is recovered
by expiry rather than by a startup process-liveness guess, so a concurrently
running controller cannot have its valid lease stolen. This prevents both the
single-process await-window race before `_is_listening` becomes true and the
same race between two backend controllers. The heartbeat explicitly adopts a
newer same-controller generation when it races the pending-to-durable Meeting
transfer. A foreign generation fails closed: Live Mic emergency-stops, while a
recording Meeting signals its capture watchdog to stop native capture, preserve
completed chunks, and transition to `capture_failed`. Live Mic claims before
pipeline construction and releases before `_is_stopping` becomes false, so a
queued toggle cannot enter between idle publication and lease release.

Derived projections share the commit of their source generation. In particular,
an analysis output and its automatic action-item snapshot are one transaction.
Regeneration deletes automatic rows absent from the new generation; explicitly
user-modified rows survive with carried-user provenance rather than masquerading
as current model output. Stable semantic/citation hashes identify regenerated
automatic items even when the model reorders them; semantic/citation matching
retains user text, owner, status, due date, and merged citations without
duplicates. A crash cannot expose a new analysis with old automatic action
items.

## Backend

Key modules:

- `src/web_api.py`: REST/WebSocket app, controller state, jobs, settings,
  runtime logs, support bundles, and explicit dev/test frontend fallback.
- `src/pipeline.py`: STT orchestration, service factory, VAD/analyzer caching,
  mic resolution, direct/async transcription helpers.
- `src/microphone.py`: Python boundary for the Rust/WASAPI frame-pipe capture,
  stream lifecycle, channel selection, and audio-level callback throttling.
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
- `src/meeting_capture.py`, `src/meeting_finalizer.py`: durable meeting chunks,
  canonical provider-timed transcript, multitrack assets, and final analysis.
- `src/data/meeting_store.py`: normalized meeting workflow, speakers, notes,
  outputs, deliveries, FTS, retention, and recovery state.
- `src/outlook_calendar.py`, `src/speaker_intelligence.py`: optional PKCE/delta
  calendar context and local hash-pinned voice embeddings.

The backend remains the source of truth for recording state, device selection,
provider calls, transcript storage, and job lifecycle.
In installed Tauri builds, the Python sidecar does not embed or serve the
production React asset tree. The only backend static frontend fallback is the
explicit `SCRIBER_FRONTEND_DIST_DIR`/source-checkout path used for dev and
tests.

## Frontend

Key modules:

- `Frontend/client/src/App.tsx`: Wouter routes. Live Mic, Meetings, YouTube,
  File, and Settings are eager because they are primary desktop tabs; Debug
  Console, transcript detail, and not-found remain lazy.
- `Frontend/client/src/pages/LiveMic.tsx`: live recording UI, canvas waveform,
  durable elapsed-time presentation, retained last-transcript preview, and
  transcript-excerpt history cards.
- `Frontend/client/src/pages/Meetings.tsx`: responsive preflight, durable live
  capture state, long-session readiness, checkpoint freshness, timestamped
  review, analysis, and delivery workspace.
- `Frontend/client/src/pages/Youtube.tsx`: YouTube search, URL workflow,
  explicit loading/empty/start states, recent videos, and thumbnail display.
- `Frontend/client/src/pages/FileTranscribe.tsx`: file upload and drag/drop,
  inline rejection feedback, provider-aware limits, and processing queue.
- `Frontend/client/src/components/transcription-history-toolbar.tsx`: shared
  count, search, and list/grid controls for Live Mic, YouTube, and File history.
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
`AppLayout` does not create a nested Wouter router or key-remount the routed
page. It preserves the shell and synchronously swaps primary-tab content, then
resets only the shared scroll container. This avoids a visible blank/Suspense
interval on the first tab visit.
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
action when an update is available. It also shows the installed app version,
links directly to the Meeting workspace, and displays the effective registered
Meeting shortcut, including a Windows registration fallback when necessary.
Unsigned/dev builds keep the updater plugin wired but are expected to report
that release updater configuration is missing.

## Tauri Shell

`Frontend/src-tauri/src/lib.rs` owns desktop shell duties:

- Start or attach to a backend after validating `/api/health`. Attaching to an
  already-listening process additionally requires a successful token-protected
  `/api/runtime` identity probe, so a stale Scriber worker with a different
  shell session token cannot be mistaken for the current backend.
- Choose a free loopback port when the default is occupied.
- Pass `SCRIBER_SESSION_TOKEN` and `SCRIBER_DATA_DIR` to managed workers.
- Enforce a Windows single-instance mutex plus an auto-reset restore event. A
  second launch performs no backend/audio initialization, signals the primary
  process, and exits; the primary then shows, unminimizes, and focuses its main
  window through the same tray-safe path.
- Intercept the main window's close request and hide that WebView to the tray
  instead of destroying it. A tray action or second-instance signal can then
  reveal the same window; explicit Quit still drains backend/audio work before
  process exit.
- Register the three global shortcuts through Tauri after authenticated backend
  readiness. Fresh defaults are `Ctrl+Shift+D` (Live Mic), `Ctrl+Shift+F`
  (post-processing), and `Ctrl+Shift+M` (Meetings); existing `.env` and saved
  Settings values win. Startup/authentication/primary registration failures
  stay pending and retry. Optional conflicts keep successful shortcuts alive as
  a stable degraded state until the next explicit refresh; all registration and
  shortcut-capture mutations share one serialized lane.
- Dispatch the optional live post-processing shortcut to
  `/api/live-mic/toggle-post-processing`. The Meeting shortcut first reveals,
  unminimizes, focuses, and navigates the main WebView to `/meetings`, then
  dispatches the Meeting detection endpoint. A monotonic pending-navigation
  handshake preserves route requests fired before the WebView listener exists.
- Own Windows autostart through `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
- Own tray/menu shell actions: open/focus, restart backend, quit, and tray
  status/icon updates for recording and available desktop updates.
- Keep the Windows PE/taskbar identity and every tray state on the same
  contrast-safe white-disc feather. The source remains the canonical vector
  feather in `Frontend/client/public/favicon.svg`; the Windows generator wraps
  it in the disc and renders each ICO frame natively rather than enlarging the
  32 px tray bitmap. Tauri reapplies the dedicated 256 px runtime window image
  before the main window is revealed or restored. Because the current Tauri/Tao
  `set_icon` path sets only the small per-window icon, Rust additionally sends
  `WM_SETICON` for both `ICON_BIG` and `ICON_SMALL` using HICONs created from
  the native 256 px and 32 px ICO frames and retained for the process lifetime.
  This keeps the live taskbar HWND authoritative instead of falling back to a
  stale PE/class icon. Update and recording states
  add only a bounded blue or red lower-right badge, generated together with
  their raw RGBA runtime pairs by `scripts/generate_tray_state_icons.py`. The
  WebView brand mark remains theme-aware and unboxed on light surfaces.
- Render the recording overlay as a non-taskbar, non-focusable window; on
  Windows it is shown without activation so hotkey recordings do not flash the
  main taskbar icon while the user is working in another app.
- Initialize the Tauri updater plugin. Release builds provide updater endpoint,
  public key, and signed artifacts through build-time configuration; Windows
  updater installation runs in Tauri's passive mode.
- Run worker crash recovery and write crash metadata. A managed worker that
  remains alive but fails `/api/health` is given a bounded 30-second recovery
  window, then is gracefully stopped (hard-killed only as fallback) and
  restarted; `SCRIBER_BACKEND_UNHEALTHY_TIMEOUT_MS` can tune that window.
- Request authenticated graceful worker shutdown before restart or shell exit,
  wait through the backend's bounded persistence/cleanup window, and use hard
  process termination only as fallback.
- Avoid visible console windows for the Python child on Windows.
- Own the installed frontend asset bundle through Tauri `frontendDist`; the
  backend sidecar remains API-only unless a developer explicitly points
  `SCRIBER_FRONTEND_DIST_DIR` at a frontend build.

Tauri must not become the owner of recording state. Route recording commands
through backend endpoints.

Local `npm run tauri:dev` uses `beforeDevCommand = npm run dev:tauri`, which
builds the current `scriber-audio-sidecar` before starting Vite. Cargo keeps
`default-run = "scriber-desktop"` because the package contains both desktop and
audio-sidecar binaries. These development contracts prevent stale capture code
and multiple-binary ambiguity from producing misleading Meeting device-test
results.

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
  hands the `prewarmId` plus its immutable resolved route to the next Rust
  capture, and records redacted adoption diagnostics. A valid route lease skips
  a second PortAudio/native inventory pass at hotkey time; native endpoint events
  and Settings route changes invalidate the cache and rebuild idle prewarm.
- A compatible leased WASAPI session is promoted without stopping or opening a
  second `IAudioClient`. The running prewarm worker atomically redirects new
  blocks into a bounded handoff tail while the capture pipe connects, writes the
  snapshot and tail exactly once as PREBUFFER, and then writes live frames. Rust
  re-resolves and compares the actual endpoint before exposing any buffered
  audio. The promoted capture owns the worker through response acknowledgement,
  capture stop, and cleanup.
- On process startup, persisted Always-on prewarm waits for DeviceMonitor's
  forced initial PortAudio refresh callback, including favorite-device
  resolution, and then starts against the final device inventory. A
  three-second bounded fallback starts prewarm even if initial device discovery
  cannot report completion. Normal hotplug callback ordering remains unchanged.
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
`SCRIBER_SONIOX_REGION` defaults to `us` and is the single data-residency route
for all Soniox transports. `eu` selects `api.eu.soniox.com` for REST upload,
polling, transcript retrieval, and cleanup, plus `stt-rt.eu.soniox.com` for
Pipecat live STT and both Meeting preview streams. The region is validated at
the Settings boundary and frozen when a processor or stream is created; Scriber
does not fall back across regions. Soniox regional access remains project/key
specific, so the API-key dialog directs users to request organization access,
create an EU project, and use that project's key.
On live Soniox Realtime stop, Scriber sends Soniox's documented empty
end-of-audio WebSocket frame, waits briefly for either a provider receive-task
finish or a final transcript frame, then shuts the local pipeline down. Once a
final transcript frame has arrived, Scriber must not wait on a reconnecting
Soniox receive task; `SCRIBER_SONIOX_RT_STOP_FINAL_TIMEOUT_SECONDS` exists only
as a bounded troubleshooting override for unusually slow finalization.

Modulate.AI is exposed as `modulate` for multilingual streaming and
`modulate_async` for multilingual batch. The streaming adapter sends 16-kHz
mono `s16le`, explicitly disables partial results, speaker diarization, emotion,
accent, deepfake, and PII/PHI signals, and forwards only text from provider-final
messages. The API requires the credential in the WebSocket query string, so the
complete connection URL is never logged and provider errors pass through a
credential/query redactor. The batch adapter explicitly disables the same
optional outputs and reduces the response to top-level final text plus bounded
duration before it can enter Scriber state; Modulate's unavoidable utterance
array is discarded at that boundary. The current direct batch route enforces
the documented 100-MB upload limit and advertises neither native diarization nor
provider timestamps. Meeting finalization first creates a task-owned 64-kbit/s
WebM/Opus derivative and caps this route at three hours, keeping normal
multi-hour recordings below that file boundary without claiming five-hour
support.

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

Pipecat/Silero VAD is opt-in through `SCRIBER_SEGMENT_SPEECH_WITH_VAD` and the
Settings toggle. When disabled, Live Mic neither loads nor attaches Silero;
HTTP-style providers receive one synthetic recording-wide turn that closes on
stop, and Soniox SmartTurn is disabled for that session. When enabled, Silero
may segment HTTP-style providers at pauses, skip confirmed silent sessions, and
provide the explicit turn boundaries required by Soniox SmartTurn. If the user
presses the hotkey while a live streaming provider is still inside an active
VAD speech turn, Scriber pushes a final `VADUserStoppedSpeakingFrame` before
pipeline shutdown so Deepgram and ElevenLabs can finalize/commit the last
transcript. Mistral Live is
currently a segment-finalized Voxtral transcription path because the bundled
Pipecat runtime does not expose a Mistral realtime service; if an installed
configuration still points `SCRIBER_MISTRAL_RT_MODEL` at Mistral's
realtime-only model, Scriber maps that segmented live path to the configured
Voxtral transcribe model instead.

AssemblyAI is exposed as both a direct async/batch provider and a realtime
Pipecat provider. Both default to Universal-3.5-Pro. The async adapter sends
the configured model through AssemblyAI's `speech_models` field; the realtime
path uses Pipecat `AssemblyAISTTService.Settings` when available, filters
settings by the Pipecat 1.5 signature, and fails visibly if an older runtime is
present. Soniox, ElevenLabs, Deepgram, Gladia, OpenAI, and Speechmatics live
factories use the same Pipecat 1.5 `Settings` contract instead of deprecated
`InputParams` or `LiveOptions` compatibility paths.
Custom buffered STT services use Pipecat 1.5 `STTUpdateSettingsFrame.delta`
objects, initialize complete `STTSettings` (`model` and `language` included),
and use the explicit `AIService` lifecycle so terminal audio is flushed before
an `EndFrame` is forwarded. This contract also applies to Scriber's Mistral,
Azure MAI, and ONNX service implementations; leaving those fields `NOT_GIVEN`
creates Pipecat 1.5 runtime warnings and ambiguous updates.
Pipecat's `local-smart-turn` extra is intentionally not installed because its
Torch/Torchaudio/Transformers dependency chain conflicts with the standard
ONNX-only local-runtime footprint. The lightweight bundled ONNX
`LocalSmartTurnAnalyzerV3` import stays available without the removed
`UserIdleProcessor`; Silero remains the bundled VAD path. Pipecat 1.5 analyzers
are processors rather than transport parameters: live input runs through an
explicit `VADProcessor`, the optional segmented-HTTP gate remains between VAD
and STT, and Soniox SmartTurn runs in an explicit `UserTurnProcessor` after STT
so it sees passthrough audio plus final transcript frames. Startup warming keeps
at most one unclaimed analyzer of each type; claiming transfers it permanently
to one recording, and cleaned mutable analyzers are never reused. When warming
is enabled, session teardown schedules a background refill with brand-new
instances so later hotkeys retain the warm-start benefit.

The Settings page has a dedicated Meetings section. It snapshots the selected
final STT provider, analysis model, Smart Turn, AEC3, automatic-analysis, and
audio-retention defaults into each new meeting. Final STT choices expose the
exact model plus native timestamp and diarization capability; changing a
default never mutates an existing meeting's pipeline snapshot.

Soniox async/direct live finalization encodes buffered PCM as WebM/Opus by
default and uploads it once with `audio/webm`. Soniox supports that format.
When local WebM encoding fails, Scriber may create WAV locally before upload;
an API error must not trigger a second full-audio upload in another format.

On Windows, the backend event loop suppresses only the known Proactor cleanup
callback carrying `ConnectionResetError`/WinError 10054 from
`_ProactorBasePipeTransport._call_connection_lost`. All unrelated loop errors
still flow to the previous/default exception handler. This prevents harmless
post-session disconnect noise from growing logs without hiding provider or
pipeline failures.

The standard sidecar keeps runtime support for the shipped cloud/external
providers exposed in Settings, but the dependency boundary is explicit. The
standard build bundles the CPU ONNX local-ASR runtime through `onnx-asr`. ONNX
is the only local STT provider exposed in Settings; full NeMo/Torch remains
excluded from the standard sidecar because it would dominate installer size.
ONNX file jobs decode media to mono PCM and feed the buffered service in
bounded 30-second chunks. Each completed chunk emits a final transcript frame,
and the terminal frame flushes the remainder. This path does not rely on the
removed Pipecat transport-level VAD hook and does not discard earlier audio
from long files when the buffer limit is reached.
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
Scriber stop wrapper that runs the base STT stop hook without disconnecting,
sends `stop_recording`, waits briefly for a final transcript, and only then
disconnects the websocket. `gladia_async`, file, and YouTube
transcription use Gladia's pre-recorded HTTP upload/polling API directly to
avoid empty live-WebSocket finalization for complete files.
`deepgram_async`, `openai_async`, `gemini_stt`, and
`speechmatics_async` are implemented as direct HTTP/batch adapters in
`src/cloud_async_stt.py`; the Speechmatics batch path intentionally avoids
adding the separate `speechmatics-batch` SDK to the standard sidecar. Build-time
runtime import checks cover the offered standard provider modules, and the
footprint analyzer rejects unused provider SDKs if PyInstaller pulls them back
in. The runtime import gate verifies both provider imports and the exact
`pipecat-ai==1.5.0` distribution version so stale sidecar caches cannot pass.

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
