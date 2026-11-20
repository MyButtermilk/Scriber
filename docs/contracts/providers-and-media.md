# Providers and media

## YouTube session handling

YouTube sign-in recovery works without an add-on: `src.youtube_login` owns an
explicitly requested, temporary Chrome/Edge instance with a fresh profile and a
private context disposed on debugger disconnect. Never attach to existing
profiles, inspect login fields, or read cookies for any URL except YouTube.
Join launch/cleanup across cancellation; close only that owned instance. A
session revision prevents late sign-in from overriding a newer clear/import.
The extension uses `activeTab` for the ordinary video handoff. Session transfer
requests the optional `cookies` permission and host access limited to
`https://*.youtube.com/*` and `http://127.0.0.1:8765/*`. Each video handoff offers a one-use RAM capability; the
authenticated app accepts the matching video before the extension uploads.
Keep this narrow extension-origin loopback lane separate from general API
CORS/token authentication. Never put cookies or capabilities into deep links,
logs, job persistence, files, or status responses. Filter domains before
transmission and again in `src.youtube_session`; private HTTPS extractor jars
expire after two hours. Manual cookies.txt import is an advanced fallback.
The recovery UI links to the official Store ID `ilbdnbhdihacgkaedacmndeabbiondob`.
Verify the published version before describing Store availability.
Cookie permission must never gate the in-page video deep link. Session uploads
accept only the official Store extension origin, require a non-empty account
cookie, and compare the revision captured at app acceptance; manual import and
disconnect revoke pending offers. Visitor cookies must not replace a sign-in or
invalidate a private login. Bind automatic retries to both attempt and transcript
identity. Queue observed video handoffs through acceptance and job creation.
Distinguish browser/protocol timeouts from the five-minute sign-in timeout.
`backend_runtime.yt_dlp_compat` corrects the pinned TV identity and disables
unneeded EJS player-cache output without relaxing the hardened wrapper bounds.
Validate accepted authentication with a real download before reporting sign-in recovery.

## Provider requests and media processing

- Batch audio capabilities are keyed by exact provider, route, and model family
  in `src/core/provider_audio_formats.py`; batch and realtime capabilities are
  separate. A generic OGG or WebM container claim never proves Opus support,
  and capability lookup for unknown models, inactive routes, or declared custom
  endpoints fails closed. Preserve the registry revision, verification date,
  evidence reference, exact container+codec format, upload bound, and explicit
  direct-pass-through allowlist.
- File-backed direct STT probes both container and codec before selecting
  provider input. New batch encodes prefer mono 64-kbit/s MP3 when the exact
  route accepts it; convert lossless inputs instead of uploading large WAVs.
  Recognize Float32 PCM WAV as decodable source audio, then normalize it to the
  route's verified upload representation; recognition is not provider
  pass-through capability. Verify the generated container and codec strictly.
  Preserve allowed lossy originals byte-for-byte, including MP3 and verified
  Opus/AAC, to avoid increasing size or adding another lossy encode. Azure MAI
  and OpenRouter retain their narrower MP3-only original policy. Meta Voice
  remains WAV-only. Generate only implemented, verified representations.
  `prepare_provider_wav_stream` applies this same policy to buffered final-only
  cloud processors and owns their temporary upload files, without closing the
  borrowed capture spool. Speechmatics public SaaS MP3 support was verified
  against its current Batch Input documentation on 2026-09-27; injected replay
  transports and custom endpoints retain their exact WAV contract. Soniox's
  existing buffered 32-kbit/s Opus path, segmented Pipecat requests, native
  realtime PCM, and local inference are unchanged. Smaller requests do not
  establish lower end-to-end latency: encoding and provider decoding also cost
  time. Generated provider artifacts are task-scoped, size-checked, and
  deleted when their async context exits. File/YouTube jobs persist the frozen
  provider route, exact format, capability id/revision, selection mode, and
  implementation before the provider request; recovery must not switch any of
  those fields silently.
- Long-file limits are specific to the active endpoint: OpenRouter MAI multipart
  is 25,000,000 bytes, with a conservative MAI2-only two-hour model-card bound;
  Azure Speech REST `2025-10-15` is below 250 MB and two
  hours per request. Soniox async documents 300 minutes, but no numerical
  per-file byte ceiling; its 10-GB stored-file quota is not an upload limit.
  Keep local ingestion bounds distinct from these provider limits. See the
  dated official-source table in `docs/ARCHITECTURE.md` before changing them.
  Preserve fitting files intact. Necessary audio parts retain a versioned
  original-time manifest and short overlap, prefer locally detected pauses,
  and are individually size/duration checked before HTTP.
  Merge by monotone timestamp-and-text evidence, never global string dedup or
  an LLM rewrite. Preserve uncertain text and project boundary warnings outside
  transcript speech. Scope provider speaker IDs to each request until overlap
  evidence supports a mapping.
- File-job part checkpoints bind the exact source hash, frozen route, request
  shape, and manifest. Validate and commit each received result before
  cancellable response/request cleanup or starting the next part. Explicit
  resume reuses paid results and must refuse ambiguous in-flight
  requests; never clear a no-replay fence merely because a partial result
  exists. Retain owned source audio while resume requires it, and delete the
  checkpoints with transcript deletion. Soniox can resume known remote IDs
  through polling; a lost create response is not an idempotent request.
- Caption-first YouTube jobs persist the audio STT route only as
  `plannedFallbackRoute`. They select one actual `executionRoute` after caption
  resolution, and `executedRoute` must later match that selection. A successful
  caption run must never be reported as if the planned billable audio fallback
  executed.
- Direct aiohttp provider calls created by the production controller share one
  `ProviderHttpTransport` on the owning asyncio event loop. Keep its bounded
  Happy-Eyeballs/DNS/connection pool and close it during backend shutdown.
  Diagnostics may retain only a bounded provider label, opaque flow id, hashed
  origin, connection/DNS outcome, chunk counts, and timings; never URL paths,
  queries, headers, filenames, request bodies, or transcripts. Once
  `request_started` makes remote acceptance ambiguous, or a provider result was
  received but its durable provider-stage write cannot be confirmed, automatic
  job retry is disabled so billable work is not replayed. A durable recovered
  provider StageResult remains the only safe way to continue without a second
  provider call.
- Standard backend builds pin `pipecat-ai[silero]==1.14.0`. Regenerate both
  constraint files with a Windows/CPython 3.14.9 resolution and keep them
  identical apart from their headers. Pipecat 1.8+ connects services inside
  `setup()` and holds frames until `StartFrame`; tests that drive a real
  service connection must patch the connector before `setup()` or drive
  `_connect_websocket()` directly so no provider traffic leaves the test.
  Provider factories
  must use the Pipecat `Settings` API; do not restore deprecated
  `InputParams`, `LiveOptions`, or pre-1.5 fallback paths. Overrides of
  `push_error` must forward Pipecat 1.14's `category` and
  `force_treat_as_permanent` keywords. Every Scriber `PipelineWorker` passes
  `idle_timeout_secs=None`: Pipecat otherwise cancels a worker after 300 s
  without transcription or VAD speech frames, which discards a long
  whole-recording dictation and truncates a file with a long quiet passage.
  The Pipecat file path routes every provider `ErrorFrame` to the worker's
  `on_pipeline_error` handler, including downstream errors through
  `_FileProviderErrorProcessor`. Cancel unfinished input immediately rather
  than waiting for EOF, and never report a pipeline that ended before its
  `EndFrame` as a complete transcript. A file has no local VAD, so services
  that transcribe only audio a VAD stop closes get boundaries from
  `_FileCommitSegmenter`: OpenAI, ElevenLabs and segmented uploads such as
  Groq every 20-30 s at a pause, Deepgram one Finalize after the last audio. Before `EndFrame` the file path
  waits, idle-bounded, for one provider result per commit. Pipecat queues a
  worker cancellation behind the EndFrame it waits for without a bound, and a
  processor that raises on that EndFrame never forwards it. Error, abort and
  stop-timeout paths therefore use `_cancel_worker_past_end_frame`, which also
  sends a CancelFrame into the pipeline itself; never await the runner task
  where a caller's cancellation could reach it, and bound abort waits.
  Synthetic VAD turn
  boundaries must use `VADUserStartedSpeakingFrame` and
  `VADUserStoppedSpeakingFrame`, which are the frames consumed by Pipecat
  streaming and segmented STT services. The frozen runtime import gate must
  reject a sidecar containing any other Pipecat version. Every direct
  `pipecat.*` import under `src` must also appear as that exact module in the
  frozen runtime contract; the AST parity gate prevents a nearby bundled module
  from masking a missing import such as `pipecat.transports.base_input`. Custom
  STT services must initialize complete `STTSettings` with at least `model` and `language`,
  and must consume `STTUpdateSettingsFrame.delta`; do not restore legacy
  `frame.settings` dictionaries or constructors that leave Pipecat settings as
  `NOT_GIVEN`.
- Buffered STT cancellation discards PCM and releases adopted WAV artifacts
  without encoding, uploading, or emitting final text. Recording gates discard
  active boundaries on `CancelFrame`, including later cleanup flushes;
  `EndFrame` and `StopFrame` retain their normal finalization behavior.
- Do not add Pipecat's `local-smart-turn` extra to the standard sidecar: in
  Pipecat 1.14 it pulls Torch, Torchaudio, and Transformers. SmartTurn remains
  optional; import `LocalSmartTurnAnalyzerV3` directly and do not couple it to
  the removed `pipecat.processors.user_idle_processor` module. Pipecat
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
- Soniox realtime authenticates every WebSocket upgrade (including reconnects)
  with `Authorization: Bearer`; start messages must not contain `api_key`.
  `src/soniox_realtime.py` adapts pinned Pipecat 1.14 connection setup and
  error frames (including authentication errors without tokens);
  preserve its inherited parser and lifecycle and the `SonioxSTTService` name
  used by manual-stop/cleanup. It opens the socket through Pipecat's
  `_websocket_connect` so the two-second closing handshake and bounded sends
  apply. Keep transport wire logging disabled so DEBUG
  cannot expose credentials. The shared transport boundary strips raw exception
  and close-reason text while retaining normal/error closure classes and codes.
  Installed replay enforces the same handshake.
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
  requests Soniox speaker diarization for both the system-audio and microphone
  streams because multiple in-room participants may share one microphone. The
  first realtime microphone speaker remains `You`; additional detected speakers
  retain anonymous labels. Preserve every contiguous speaker-token run as its
  own timestamped final live segment, normalize raw provider speaker ids by
  first appearance, and scope raw ids to one WebSocket connection so reconnect
  reuse cannot silently merge people. Final batch transcription requests native
  or local speaker separation independently for both canonical tracks; collapse
  the microphone result to `You` only when it contains exactly one speaker.
- Live microphone transcription must not request or format provider speaker
  diarization. Keep `enable_speaker_diarization=False` for live pipelines so
  single-speaker dictation inserts plain text. File and YouTube jobs may enable
  diarization where the provider adapter has stable anonymous speaker output.
- Azure `MAI-Transcribe-2` defaults to `enhancedMode.modelOptions.transcribeStyle=clean`
  in the shared request builder for every product workflow, including Live Mic.
  Direct File/YouTube/Podcast and Meeting finalization additionally request
  top-level `diarization.enabled=true` and `modelOptions.timestamps=word`.
  Preserve native phrase speaker IDs, complete word timestamps, and the live
  no-diarization boundary. Legacy explicit MAI-1.5 verbatim callers remain valid.
- Live Mic may broadcast `InterimTranscriptionFrame` text as replaceable UI
  preview, but it must never append, persist, or inject that text. Only provider
  `TranscriptionFrame` finals enter the transcript and text injector; this also
  applies to ElevenLabs manual-commit realtime transcription. A repeated final
  that the record and injector drop is broadcast without text, so the live
  view clears its interim preview without showing the repeat.
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
- Interactive PDF exports are typeset in the WebView with the vendored pdfcn
  Forme components under `Frontend/client/src/components/pdf/` (re-add or
  update them with `npx shadcn add @pdfcn/forme/<name>`; keep them unedited
  apart from formatting) and `Frontend/client/src/lib/pdf-export/`. The
  backend supplies only the versioned `export/document` JSON render input and
  must never put Outlook or speaker-mapping addresses in it. Keep the engine,
  WASM, and fonts out of eagerly loaded chunks; the CSP permits WebAssembly
  compilation via `'wasm-unsafe-eval'` only, never `'unsafe-eval'`. Full
  transcript and Meeting PDFs fall back to the backend renderer on any
  typesetting failure; `.eml` attachments always use the backend renderer.
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
  text after stop, then route it to the explicitly selected cloud provider or
  local GGUF engine. The cloud path keeps the configured `${output}` prompt,
  keeps the general cross-provider fallback inside one seven-second model-
  routing deadline by default, preventing a serial retry chain from delaying
  insertion without bound. Its default latency lane uses low OpenRouter
  reasoning, the explicitly selected `POST_PROCESSING_FALLBACK_MODEL`, one
  semantic generation, and no replay of an interrupted response-body request.
  A confirmed 401/403 credential rejection bypasses that exact primary
  model/credential pair until
  the key/model changes or the process restarts; HTTP 402 quota failures use a
  60-second circuit, while request-specific 400 and transient failures are not
  cached. These overrides are task-local and must not change Meeting or ordinary
  summary routing. The complete primary/fallback route holds one provider-HTTP
  transport lease: shutdown seals new work immediately but the last active
  borrower owns cancellation-safe session closure, so it cannot close the pool
  between primary and fallback. `SCRIBER_LIVE_POST_PROCESSING_TIMEOUT_SEC` may
  tune the deadline from 0.05 to 30 seconds for diagnostics. A provider failure
  or deadline expiry retains and inserts the raw transcript immediately.
  Only a successfully accepted cloud fallback may request the authenticated
  Tauri `postProcessingFallbackNotify` shell command and publish the versioned
  `post_processing_fallback_used` event. The native Windows toast is the normal
  hidden/background-main-window path, but it may acknowledge acceptance only
  after the WinRT notifier reports `Enabled` and accepts `Show`. Isolate that
  call on the dedicated fail-fast notification worker; never hold the event-ID
  history lock across WinRT or consume an unbounded shared shell IPC worker.
  Carry the same bounded event ID through shell IPC, retain the last 100 accepted
  IDs in the shell, and start the original 750-ms post-injection deadline before
  executor admission. The event carries `desktopNotificationAccepted`; the app-
  global frontend bridge owns a deduplicated in-app toast when the foreground
  main window owns delivery or native delivery was unavailable. Keep a native-
  rejected event in the authoritative WebSocket state for at most two minutes so
  a transiently disconnected WebView can replay it by the same event ID. Primary
  success, failed fallback, raw-text
  fallback, and local polishing must use neither path. Settings exposes only
  supported OpenRouter fallback choices and persists the selection independently
  from the primary post-processing model.
  The local path uses only the pinned LFM QAD prompt/policy and the sole
  `qad_q4_0` model. It must never fall through to a cloud provider after a local
  failure. In every failure case retain and insert the raw transcript. Do not
  route File or YouTube jobs through this path.
  Local prewarm must wake an existing idle llama.cpp process as well as start
  a missing one. Its authenticated empty `/tokenize` request resumes b10158
  without inference or prompt-cache changes; `/health` and `/props` do not
  wake sleeping models. Keep this work under the manager's runtime lock and
  scheduled at capture start so reload latency overlaps dictation.
- Azure MAI defaults to `MAI-Transcribe-2`.
- Migrate the exact app-persisted `mai-transcribe-1.5` default to that model;
  preserve explicit process overrides and unrelated custom model values.
  Retain the exact 1.5 capability and MP3 preparation for explicit overrides
  and already frozen jobs, including recovery of durable provider results.
- Keep `SCRIBER_AZURE_MAI_MODEL=mai-transcribe-1` available as region/resource
  fallback.
- For Azure MAI-Transcribe-2, `SCRIBER_CUSTOM_VOCAB` is sent as `phraseList`.
- Azure MAI upload preparation is latency-first: existing MP3 uploads directly,
  non-MP3 inputs are transcoded to mono 64k MP3, and live PCM buffers are encoded
  to MP3 before upload. The shipped control remains post-stop FFmpeg MP3;
  capture-time FFmpeg MP3 is production-safe but default-off after its mixed
  canonical A/B result. Do not restore WAV upload without measured provider need.
- `openrouter_stt` is the separate active OpenRouter route for Microsoft
  MAI-Transcribe-2. Keep its exact model fixed to
  `microsoft/mai-transcribe-2`, reuse the existing `OPENROUTER_API_KEY` used
  by OpenRouter summaries and post-processing, and send MP3 audio
  to `/api/v1/audio/transcriptions` as a multipart `file` with `audio/mpeg`.
  For eligible new File and YouTube MAI-2 jobs, request `verbose_json` with
  word timestamps. Preserve plain JSON for MAI-1.5, frozen `final_text`
  attempts and buffered live dictation. This route supplies no native speaker
  labels. Omit `SCRIBER_CUSTOM_VOCAB` and Azure `phraseList`. The direct
  `azure_mai` route has its own credential, endpoint and preparation path.
  Already frozen `microsoft/mai-transcribe-1.5` work retains its exact legacy
  capability and request model; new work continues to select version 2.
  All new file/YouTube and buffered live uploads use MP3 regardless of duration.
  Non-MP3 imports and live WAV spools use mono 64-kbit/s MP3 before upload.
  Multipart carries the MP3 bytes unchanged and enforces OpenRouter's documented
  25,000,000-byte file limit; do not restore the former 18,000,000-byte base64
  allowance as an audio limit. The logical file route admits up to 2 GiB of
  prepared audio and preserves MP3 originals even above the per-request limit.
  Apply the frozen route's byte and duration limits when deciding whether an MP3
  fits. The part manifest retains original timing for ordered transcript assembly.
  Do not apply the generic lossy WebM ingest compression to OpenRouter MP3s.
  An incomplete sequence must never be published as a successful full transcript
  or automatically replay completed parts; existing durable job fences remain.
  In-budget frozen
  WAV/FLAC jobs retain their exact persisted representation and legacy JSON
  transport with a 25,000,000-byte serialized-request budget. Preparation and
  both serializers enforce their respective source/request limits; generated artifacts
  and request spools are cleaned on failure. HTTP 413 is an `audio_invalid`
  failure, not `internal_bug`.
  File/YouTube failure events use the typed provider category and expose bounded
  HTTP status, public error code, request/response byte counts, operation, and
  canonical reason. Recognize JSON, plain/HTML gateway failures, and nested
  OpenRouter `error.metadata.raw` upstream errors without retaining free-form
  provider messages. Keep these fields in the debug-console allowlist; never
  restore raw response bodies to exceptions, logs, or support bundles.
  Retry confirmed HTTP 429 responses at most twice, within a five-second
  retry-admission window and the original overall request deadline. Respect
  `Retry-After` seconds and HTTP dates; do not shorten a server wait to fit that
  window. Without a valid hint, use bounded exponential backoff with jitter.
  Retain the original audio for an allowed retry. Build a fresh multipart body
  for MP3 or a fresh JSON body for frozen legacy JSON requests because aiohttp
  closes the uploaded stream. Non-seekable audio, explicit credit/spending failures,
  other HTTP errors, ambiguous response timeouts, and cancellation must never
  trigger an automatic replay. HTTP 429 alone does not establish missing
  OpenRouter credit; keep its temporary rate-limit message distinct from 402.
- Failed Live Mic records persist their safe provider message in `step`, with
  only recognized speech in transcript content. Preserve partial speech and
  empty failed history entries. Transcript Detail shows the failure separately
  and excludes legacy error-only content from word counts and copy/export
  actions; the Live Mic history marks failed cards and its copy action uses the
  same `isFailureOnlyContent` rule. Never add an error string as a transcript
  segment. This includes provider-error emergency stops: preserve the failed record
  independently of cleanup errors and caller cancellation. A native cleanup
  retry cannot recover speech after the record reference has been discarded.
  It also includes a pipeline task
  that ends without a stop request; the latter must move INITIALIZING/RECORDING through FAILED before
  IDLE, or the next start ignores its microphone-ready edge. The app-global
  toast bridge suppresses a failed `session_finished` already reported by its
  session's `error` event, because one visible toast would otherwise replace
  the actionable provider message with the generic fallback.
- Meta language-model choices are `muse-spark-1.3` and
  `muse-spark-1.3-contributor`; exact persisted 1.2 built-in selections migrate
  to their matching 1.3 tier without rewriting custom model codes. This also
  applies to stored Meeting analysis selections at database initialization;
  keep historical analysis output and model provenance unchanged.
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
  dependency. Pipecat cancels its Google stream on stop and abandons it at the
  four-minute rotation while the open utterance is still interim; Scriber's
  Google subclass instead ends the request stream (gRPC half-close), drains the
  finals with a bounded wait, and carries the rotation chunk to the next
  stream in order. OpenAI live STT uses Pipecat's OpenAI Realtime STT service with
  `gpt-realtime-whisper`, while `openai_async` uses the direct OpenAI Audio
  Transcriptions HTTP adapter with `gpt-4o-mini-transcribe-2025-12-15`. Keep
  the explicit `openai` SDK and `websockets` dependencies for these paths.
  Groq STT uses Pipecat's `groq` SDK dependency. Pipecat 1.12+ segments
  sentences with the embedded `sentencex` rules and measures loudness with the
  NumPy-only `loudness` package, so the frozen backend bundles neither NLTK,
  Punkt data, SciPy, nor a local `pyloudnorm`; the frozen gate runs Pipecat's
  sentence probes and fails if NLTK is loaded. Speechmatics live uses
  Pipecat's Agent STT service (`/v2/agent`, model `linden-1`) with
  server-side turn detection, because Scriber attaches no local VAD to native
  streams; the Pipecat 1.14 speaker template applies to every final, so only
  diarized files use speaker labels. Pipecat's `stop()` discards its message
  queue before the SDK disconnect flushes the open turn, so Scriber's
  Speechmatics subclass disconnects first while the consumer still runs and
  waits for an in-order marker; keep that flush or the last utterance of a
  dictation or file is lost. Gladia live
  transcription uses
  Pipecat's Gladia service. Its stop waits (bounded) for the socket close that
  follows `stop_recording`, with reconnection disabled first; a final from an
  earlier utterance is not proof that the last one arrived. Gladia file and YouTube transcription use the
  direct pre-recorded HTTP upload/polling API and should not be routed through
  the live WebSocket pipeline. The direct async adapters
  `deepgram_async`, `gladia_async`, `openai_async`, `gemini_stt`, and
  `speechmatics_async` live in `src/cloud_async_stt.py`; keep them as direct
  HTTP/batch adapters unless a measured provider SDK change justifies adding
  more packaged dependencies. Do not add `speechmatics-batch` to the standard
  sidecar while the direct Speechmatics batch API path is sufficient. Gemini
  async transcription uses the Interactions API's exact
  `gemini-3.5-transcribe` model contract. Automatically migrate only an
  app-managed historical default; preserve explicit process overrides and
  reject incompatible values before Files API upload with credential-free
  configuration guidance. Keep
  `onnx-asr[cpu,hub]` in the standard sidecar for the ONNX local-ASR path.
  NeMo/Torch is not exposed as a local provider in Settings.
  Local ONNX file transcription uses the buffered Pipecat service with bounded
  30-second flush chunks. Pipecat no longer carries the legacy transport
  `vad_analyzer` parameter, so file transcription must not depend on transport
  VAD frames to trigger ONNX recognition. Long files and Live Mic dictations
  beyond five minutes must flush completed chunks instead of dropping earlier
  PCM; each chunk ends in the quietest 100 ms of its last 10 seconds so no word
  is split. CancelFrame and the terminal-transcription skip mark discard the
  buffer without inference. Live Mic starts loading an already downloaded
  model at StartFrame (never a download), and its stop budget is
  `max(30, 8 + 0.35 × recording length)` seconds, capped at 600, because the
  buffered recording is transcribed locally after stop.
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
- Standard YouTube builds pin `yt-dlp[default]==2026.8.19` with matching
  `yt-dlp-ejs==0.8.0` and the locked QuickJS-ng `0.15.0` engine behind
  `scriber-quickjs-wrapper`. Stage `qjs.exe`, `qjs-engine.exe`,
  `LICENSE.quickjs-ng.txt`, and `js-runtime-manifest.json` as one indivisible
  bundle. The wrapper accepts only the exact `--script FILE` protocol, verifies
  the patched engine identity, bounds input/output/memory/runtime, prevents
  native module and secondary-process use, and cleans up its child through a
  Windows job. Keep its internal timeout/child-reap and capability-boundary
  tests in the build verifier. Frozen resolution must bind the wrapper, engine,
  manifest, and license to the committed app-layer trust root and require the
  exact wrapper self-test; the installed manifest must never authenticate its
  own file identities. Run that synchronous attestation off the aiohttp event
  loop and reuse its result for yt-dlp's library path and the source/dev-only
  subprocess fallback. Frozen builds import the exact pinned PyInstaller
  package and fail closed if it is unavailable; never stage a pip/distlib
  `yt-dlp.exe`, because its embedded shebang names the build-machine Python.
  Do not fall back to a raw QuickJS executable.
  The NSIS postinstall hook must continue deleting exactly the superseded
  `backend\tools\ffmpeg\deno.exe` and path-bound `yt-dlp.exe` so an in-place
  upgrade matches a clean install.
  Let current yt-dlp defaults select YouTube player clients; do not restore the
  stale forced `android,web` client pair. Every returned download must pass
  ffprobe container and audio stream validation before transcription. Keep
  malformed/incomplete downloads retryable and never report them as download
  success.
- Keep PySide6, customtkinter, and Tk overlay fallbacks out of the standard
  sidecar. Installed recording overlay rendering is owned by Tauri/Rust.
