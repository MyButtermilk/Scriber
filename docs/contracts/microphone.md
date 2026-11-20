# Microphone and device handling

- Keep PortAudio access guarded through the shared device guard lock.
- Do not enumerate or refresh PortAudio devices while an active stream is being
  torn down unless the existing guarded/deferred path handles it.
- `DeviceMonitor` should use native Windows endpoint events where available.
  With active native events, polling is only a sparse safety net; faster polling
  is fallback-only when native events are unavailable.
- Device refresh is recording-aware and can be deferred until idle.
  Passive polling must preserve an outstanding PortAudio refresh and its
  paired prewarm resume. Repeated quiesce calls retain that resume obligation
  until the actual refresh completes, including when the old producer is
  already pending a confirmed stop and no public prewarm ID remains.
  Refresh quiescence must preserve a capture-owned prewarm handoff, including
  the interval after native adoption and before Python's first-live commit.
  Settings and watchdog disable paths preserve that same capture ownership.
  Refresh also retains starts whose native response is still pending, settles
  their cleanup within a bounded wait, and cannot override a newer refresh,
  capture, or shutdown generation.
- Persisted `SCRIBER_MIC_ALWAYS_ON=1` prewarm starts only after DeviceMonitor's
  initial PortAudio refresh callback has completed. Startup keeps a bounded
  fallback when initial device discovery cannot finish; do not reintroduce a
  prewarm/refresh race that leaves the Windows microphone indicator off until
  first recording. Preserve normal hotplug callback ordering so restored
  favorite-device selection is applied before idle prewarm resumes.
- Physical microphone matrix evidence is native-event-first. Use
  `-RequireDeviceRefreshEvidence` for physical Rust/WASAPI verification so artifacts prove
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
  active. A running worker is not proof of audio progress either: cumulative
  frame counts and last-frame age are independent of retained prebuffer size
  and include silent PCM. After the bounded startup/freshness grace, a stalled
  worker must be stopped with native confirmation before replacement. Fence
  status results and recovery by the current route generation and capture
  ownership so a concurrent hotkey cannot lose its producer.
  A successful stop RPC alone is not confirmation: Python requires the boolean
  `payload.stopped`. Native retries must preserve a bounded completion receipt
  for that exact prewarm ID after a lost response. An unknown ID is not a stop
  receipt; retain an unconfirmed retiring process and block new microphone
  producers until its release is proven.
  Status diagnostics must keep prewarm IDs redacted and preserve
  response time, active/inactive reason, buffered-frame counters, and restart
  counts. The prewarm diagnostics also expose a bounded redacted `recentEvents`
  timeline for start/stop/adoption/watchdog restarts so short Windows
  privacy-indicator dropouts are visible in support bundles without increasing
  steady-state log volume. Rust app-prewarm verification must include
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
  the next hotkey. Favorite restoration during device refresh must also rebuild
  a prewarm start whose native response is still in flight. Invalidate its
  generation and settle its cleanup before publishing the current route; an
  unconfirmed native stop retains ownership and blocks replacement. Never
  weaken the exact route/configuration match at adoption to hide stale metadata.
  A capture without a valid leased route keeps the normal
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
  OpenAI, ElevenLabs and Deepgram count one answer per commit; an answer
  without new text (silence, or Deepgram's empty reply to a Finalize after its
  endpointing already finalized the words) also completes the wait instead of
  failing the recording. Count answers right before the commit is issued.
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
- A successful Rust capture stop ends with exactly one ordered, zero-payload
  `AUDIO_FRAME_FLAG_END_OF_STREAM` control frame after every remaining PCM
  block. Python validates that frame, waits for it after an acknowledged
  sidecar stop, and reports missing EOS as a mid-session failure; pipe EOF or a
  stop response alone is not proof that the last captured audio reached Python.
  Keep Python audio acceptance open through that EOS and drain the downstream
  audio queue before final provider shutdown. The sidecar may finalize an
  optional preparation worker only after EOS and must retain ordered QPC
  markers for last audio, capture stop, and encoder-tail start/completion.
- Frame-pipe client connection waits must have a hard deadline. Live and
  synthetic capture pipes remain nonblocking after connection so a stalled
  Python reader cannot block the audio writer; Meeting relay output pipes may
  switch to blocking mode only after their bounded connection succeeds.
- Rust frame-pipe PCM is read into Python for downstream Pipecat/provider
  processing. If capture fails before the first frame, the recording fails
  visibly; do not reintroduce a Python capture fallback.
- Optional capture-time preparation receives shared PCM segments only after the
  authoritative frame-pipe write succeeds. Its bounded nonblocking queue may
  invalidate and discard only the derived candidate on overflow, worker loss,
  size overflow, or capture failure; it must never block capture or drop the
  authoritative PCM stream. A production artifact may cross processes only
  through the shell-created `wav_pcm16_file_v1` lease. Tauri validates the
  exact path, lease ID, size, and RIFF/data lengths before ownership transfers;
  Python validates the WAV again before opening it and releases the opaque
  lease after upload. Never accept a caller-supplied artifact path or unlink a
  leased file directly from Python. `wav_pcm16_file_v1` is production-capable
  but remains an explicit Speechmatics opt-in after the installed A/B decision.
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
