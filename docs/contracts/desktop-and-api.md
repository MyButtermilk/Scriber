# Desktop and API

## Tauri Runtime

- Tauri is the primary desktop runtime.
- The packaged backend runtime is exact CPython 3.14.9 (`cpython-314`). Release
  defaults are `PythonRuntimeFlavor=Official` and
  `PythonJitMode=Disabled`; they are build parameters, not `.env` or Settings
  values. Before launching the worker, Rust removes inherited `PYTHON_JIT`,
  `PYTHON_GIL`, and `PYTHON_TLBC`, then sets the manifest-bound JIT state
  explicitly. The frozen backend must fail closed when its version, cache tag,
  `python314.dll` hash, runtime-lock hash, flavour, tail status, JIT
  availability, or expected/actual JIT state disagrees with the packaged
  policy. Do not enable general Python environment processing to make JIT work
  in PyInstaller; that would weaken the embedded-runtime isolation contract.
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
  Installed provider replay must stop at `activation_armed`: the native shell
  binds one exact run/sample/lane and only the actual hotkey callback or the
  primary-button Tauri command may consume it. Replay hotkeys route to the
  strict start endpoint, never through toggle interpretation. Missing QPC,
  lane mismatch, duplicate activation, or an unarmed marker fails closed and
  leaves the activation KPI unknown.
- Rust owns Windows autostart, global hotkey registration, single-instance
  startup, tray/menu shell actions, and worker crash recovery. A persisted
  `desktop-autostart-user-choice` value must be reconciled to the current
  executable path on release startup; `enabled` repairs a missing or stale
  HKCU Run value and `disabled` removes it. Only a missing preference receives
  the install default. The supervisor
  must keep the named single-instance restore event: a second launch exits
  before backend/audio side effects but signals the primary instance to show,
  unminimize, and focus its main window. A mutex-only early return is a UX
  regression for tray-hidden starts.
- The backend supervisor must also recover a managed process that stays alive but fails `/api/health`:
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
  `scripts\project-python.cmd scripts\generate_windows_app_icon.py`. Tray state
  changes add only a bounded blue update or red recording badge; regenerate
  their DPI-specific raw-RGBA variants and legacy 32 px preview PNGs afterwards with
  `scripts\project-python.cmd scripts\generate_tray_state_icons.py`;
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
- The custom tray WebView keeps all action targets visible without a scroll
  container. Create it from an async command or background event task because
  synchronous WebView2 creation can deadlock Windows IPC. Serialize competing
  show requests. Rows share
  the available height; supplementary descriptions collapse before actions.
  The native window fits the monitor work area in logical pixels, including
  high-DPI displays. Verify normal, update, error, and recent-history views
  at 668, 560, and 480 logical pixels high when changing this layout. It refreshes
  recent transcripts on every native show
  through `scriber-tray-opened`, and after installing its listener on first
  startup. Abort and generation guards prevent older requests from replacing
  newer history. The Transcript routes' `/recent` and `/{id}/copy` reads use
  one durable SQLite snapshot each: completed rows only for the list, current
  canonical head content when available, and durable Mic/legacy content when
  no artifact head exists. Never copy cached previews or in-memory history.
  Labels are bounded plain-text previews; empty text stays visibly disabled.
  Copy validates the selected ID and final status again, runs off the UI
  dispatcher, and uses an owned Scriber HWND for Windows clipboard ownership.
  Entering Recent Transcripts may reuse an in-flight read from that opening;
  a new native opening must still invalidate it. Refresh shortcut labels on
  opening, never on recording/update status events. Subscribe before reading
  initial tray status, bound listener setup, and reject late snapshots after
  newer events. Native unchanged status skips icon/menu/event work; app-wide
  status delivery already reaches the tray and must not be duplicated.
  A failed or stalled opened-listener registration must still perform the
  initial shortcut/history read. Dispose timers and late listeners on unmount.
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
- Native push-to-talk press/release requests carry one opaque gesture UUID and
  increasing revision assigned at the shortcut callback. The backend rejects
  superseded edges before awaiting audio admission and rechecks ownership after
  the wait. A newer release cancels delayed or queued PTT starts; an old release
  must not stop a successor or a UI-owned recording. Preserve admitted gesture
  metadata through continuation and benchmark activation.
- Rust initializes the Tauri updater plugin, but frontend code owns update
  checks and user-facing update UX. Keep update checks non-blocking, cached,
  about weekly by default, and suppress automatic prompts while recording or
  transcription is active. Do not add a Python backend updater cron or ping.
  Tray and main-window installs share `desktop-updates.ts` and the native
  cross-window installation gate. Both checked-in and generated updater configs
  use quiet installation; cancellation/failure releases the gate for retry.
  Keep the installing phase process-local: Windows exits before the installer
  finishes, so localStorage must retain only the stable available/current
  result. Migrate old persisted installing entries against the running build
  version and clear their last-check timestamp so startup can check again.
  Reading that cache must never claim a new native installation is active.
  Production update builds must use signed Tauri updater artifacts, a public
  HTTPS `latest.json`, and publication verification. `scripts/build_windows.ps1`
  may accept a local `TAURI_SIGNING_PRIVATE_KEY_PATH`, but it must normalize it
  to `TAURI_SIGNING_PRIVATE_KEY` before invoking Tauri; do not commit updater
  private keys. If `latest.json` lists a signed updater artifact, the matching
  sibling `.sig` file is required in collected release assets; do not silently
  upload a signed metadata file without the corresponding signature asset.
- Official GitHub publication must remain draft-first and ID-bound. Discover
  an existing draft through bounded authenticated release-list pagination,
  bind the exact numeric release ID before upload, and refuse any published
  match. Before binding either a new or existing draft, call GitHub's bounded
  `releases/generate-notes` endpoint for the exact release tag and event SHA and
  require HTTP 200. The bound release must retain the exact
  `Scriber X.Y.Z` title, event-SHA `target_commitish`, and freshly generated
  body; never patch or adopt foreign/edited metadata. Evidence may retain only
  the title, target SHA, bounded UTF-8 body length, and body SHA-256, never the
  body itself. Create a missing release as an empty draft with
  `generate_release_notes=true`; never attach assets in a tag-based create
  command. Delete and upload assets only through numeric
  release/asset IDs or that exact release's validated `upload_url`, with a
  final ID/tag/draft read immediately before every mutation. The last asset
  identity/download and release-metadata verification plus numeric-ID publish
  PATCH belong to one transaction helper. Recheck the expected-state metadata
  immediately before and after that PATCH. Verify the same release ID, tag,
  title, target, body identity, and every asset ID, name, byte size, GitHub
  SHA-256, and bounded download hash immediately after publication. On any
  postcondition failure, restore that exact release ID to a verified draft; if
  rollback cannot be verified, delete only that ID and require an HTTP 404.
- Rust also exposes a private shell IPC channel for opt-in native text
  injection. `SCRIBER_INJECT_METHOD=tauri` is strict; `auto` stays Python-owned.
  `office_text_insert.py` first binds the exact focused classic Office `_WwG`
  editor by HWND and inserts via NativeOM without touching the clipboard.
  Preserve the UTF-16 BSTR length, repeated target checks, bounded COM
  cancellation, conservative unsupported-selection fallback, and terminal
  outcome after an uncertain write; never retry that write through paste.
  `windows_clipboard_lease.py` restores verified standard Edit consumers after
  their confirmed read completes. Unknown readers, including RichEdit, keep the
  conservative delay because
  Office and other consumers may reread formats during one paste operation.
  Clipboard-based injection paths, including the default Python
  paste path and Tauri `injectText`, must preserve a bounded snapshot of safe
  HGLOBAL-backed clipboard formats before setting transcript text. This includes
  application-registered formats in the Windows `0xC000..=0xFFFF` range (for
  example HTML/RTF and Chromium metadata), while handle formats such as
  `CF_BITMAP` or `CF_ENHMETAFILE` must never reach `GlobalSize`/`GlobalLock`.
  Restore the snapshot only if the clipboard sequence is unchanged; do not
  regress this to text-only clipboard preservation.
  Live Mic insertion (the pipeline `TextInjector` and deferred or
  post-processed output via `inject_text_once_async`) runs on one dedicated,
  ordered `scriber-text-injection` worker, never on the asyncio loop: lease
  waits, pre-delays, Office COM, and typing fallbacks otherwise stall
  WebSocket delivery and a successor recording. Cancellation withdraws an
  insertion still queued behind another (focus may have moved) and waits for a
  started dispatch. Immediate realtime finals are owned by `TextInjector`, not
  by Pipecat's process task: the Soniox manual stop ends the pipeline with
  CancelFrame, so finals that already arrived are still inserted, and
  End/Stop/Cancel frames, flush, and cleanup wait for them. Finals arriving
  while an insertion runs (for example behind the previous paste's clipboard
  lease) are coalesced into one insertion instead of one lease wait each.
  Emergency stops retain that session's insertion predecessor until its
  remaining output settles, even after native capture is released. A later
  recording may capture immediately but must not insert ahead of older text;
  shutdown must retain and join the owning finalizer.
  If that deferred recording fails, insert its retained raw speech after the
  predecessor, respecting disabled text injection and skipping LLM polishing.
  The synchronous `flush()` remains only for the benchmark visible-text
  contract. Keystroke typing is the last fallback: an interrupted
  `keyboard.write` is terminal, never retyped through pyautogui, and injector
  failures log exception types only. Log FFmpeg upload-encoding failures by
  exit code and error type only; its stderr names temporary paths under the
  user profile.
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

## REST and WebSocket Contracts

- WebSocket events are versioned with `apiVersion`.
- Use builders and validators in `src/core/ws_contracts.py` when adding events.
- `/api/health`, `/api/runtime`, and frontend-ready payloads are versioned and
  validated through `src/core/rest_contracts.py`.
- Add or update contract tests when changing payload shape.
- Frontend REST consumers should use `Frontend/client/src/lib/api-types.ts`
  instead of ad hoc `any` boundaries.
- `/api/runtime/logs` may expose only a compact human-readable message plus
  bounded, allowlisted structured context. Recursively redact public metadata,
  omit arbitrary identifiers, secrets, transcript/prompt content, and extras,
  and keep the complete machine record in the local log file. The Debug Console
  keeps technical context and long legacy messages collapsed by default, shows
  named hot-path startup/finalization timings, and offers per-entry copyable
  redacted JSON rather than rendering dictionary dumps inline.
  File/Podcast correlation and explicit job/transcript/episode/subscription
  cross-references are permitted only as validated opaque UUIDs. Keep upload
  references distinct from durable job references and preserve the hand-off.
- Every STT execution must log one credential-free runtime configuration with
  workload, provider, exact effective model, mode, language, sample rate, and
  channel count; Soniox also includes the selected region. Never include API
  keys, authenticated URLs, request payloads, or transcript text. Keep full
  pairwise hot-path matrices in the latency metrics store rather than normal
  human-facing log messages.
- Live-dictation latency traces use the canonical activation-to-visible-text
  vocabulary in `src/core/hot_path_tracer.py`. Preserve distinct hotkey and
  button activation markers, the primary
  `activation_received_to_final_text_observed_ms` KPI, the secondary
  `stop_requested_to_final_text_observed_ms` KPI, and non-speech overhead only
  when an authoritative fixture duration is supplied. `final_text_observed`
  is an external target-observer event bound by run/sample/session and QPC
  identity; an injector callback or paste return is not proof that text became
  visible. Missing markers remain missing/unknown and must never be synthesized
  as zero.
- Installed provider replay runs Microsoft, Soniox, and Speechmatics separately
  at exactly 5, 15, 30, and 60 seconds. Speechmatics traverses the real
  `speechmatics_async` Batch-v2 adapter/parser through a one-shot local raw
  transport that fully validates the uploaded WAV and never performs network
  I/O. Microsoft's one-shot transport likewise keeps the real Azure-MAI
  adapter/parser but requires the frozen North Europe URL, API version, model,
  locale, request definition, and `local-replay` credential sentinel. Before
  activation it builds bounded references with the bundled ffmpeg; after stop
  it decodes the actual uploaded MP3 and binds it exactly to the hash-verified
  48-kHz PCM fixture plus at most two seconds of zero tail. Candidate evidence
  must attest the preparation actually used, not only the requested switch:
  capture-time fallbacks or failed media validation invalidate that replay sample.
  Promotion evaluates both canonical KPIs for every
  provider-duration series; it must fail closed when a series is missing,
  invalid, failed, or slower than its matching baseline. Do not pool durations
  or score the legacy provider-final tail. Non-speech overhead is numeric only
  when fixture bytes, format, duration, and measurement phase are attested. Keep Azure capture-time
  MP3 and Speechmatics Rust WAV default-off unless newer matched evidence passes
  the same no-regression contract.
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
  Routes that upload the whole recording after stop get a finalization budget
  of 8 s plus 0.35 times the recording length, capped at 600 s (below the
  900-second provider requests); the ElevenLabs/OpenAI realtime streams keep
  their 90-second ceiling. A 90-second ceiling for whole-recording uploads
  cancelled long dictations and discarded them. Stop-path failure logs carry
  exception types only.
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
  single-snapshot transaction in `MeetingStore.detail`. Punctuation-only
  transcript searches use literal substrings, never SQL wildcard semantics.
- Durable File/YouTube job claims and terminal transitions are SQL CAS updates.
  Preserve idempotent `queued/running/completed -> completed` reconciliation,
  but never allow a late completion to overwrite `canceled` or `failed`.
- Meeting exports must use `src/meeting_export.py` as the shared template
  boundary. Their duration uses the retained playback mix's duration plus
  Meeting-clock origin, including trailing silence; segment end times are the
  fallback when no mix duration is available. Email headers must remain
  single-line and participant addresses validated/deduplicated; body-only drafts
  must not claim an attachment exists.
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

## Outlook Calendar and Participant Identity

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
