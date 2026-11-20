# Performance contracts and measurement history

Already implemented and should not be regressed:

- Lazy STT provider imports.
- Frozen-backend size pruning keeps PyInstaller `Analysis(optimize=0)` and real
  assertion execution, then recursively removes only compiler-owned docstrings
  from the retained code cache. Preserve pycparser/PLY grammar docstrings as
  executable parser data. Preserve the Deepgram `listen.v1` runtime
  allowlist and the exclusions for unused Deepgram surfaces, OpenAI SDK
  Realtime/Conversations/Webhooks implementation modules, and synchronous grpc
  helpers. The frozen gate must initialize every supported provider path, reject
  representative pruned imports, and exercise Pipecat's sentencex
  segmentation without NLTK.
- The Windows frozen backend uses the validated
  `numpy-2.4.6+scriber.noblas.1` wheel through a work-root-local,
  PyInstaller-child-only overlay. Keep wheel/lock/validator in both the internal
  runtime key and the normalized CI runtime key, while leaving requirements,
  wheelhouse, pip-store, and build-venv identities on public NumPy `2.4.6`.
  The spec must fail closed on wheel path, import origin, version, metadata, and
  licenses; the frozen import probe must reject any external BLAS/LAPACK
  configuration or retained OpenBLAS file. SmartTurn may use only the pinned
  fixed-matrix ONNX adapter already backed by the bundled ONNX Runtime, with
  complete feature/probability parity and German recognition gates. H16 upgrade
  cleanup uses exact obsolete filenames plus a non-recursive empty-directory
  removal; never replace it with wildcard or recursive `numpy.libs` deletion.
- Cached VAD/analyzer setup.
- No-client WebSocket broadcast fast path.
- About 60 Hz audio-level throttling.
- Canonical activation-to-visible-text tracing with separate hotkey/button
  origins, externally observed visible-text completion, primary/secondary KPI
  helpers, and p50/p90/p95/max/variance/failure-rate aggregation for controlled
  provider replay evidence.
- Canvas/RAF waveform drawing instead of per-frame React state.
- Buffered transcript appends for long live sessions.
- Timed file stitching retires only the sorted prefix ending strictly before
  the next part. Preserve conservative overlap evidence, speakers and warnings;
  compare changes with the frozen reference in the merge diagnostic benchmark.
  Keep two alignment score rows and byte-sized traceback moves, without
  truncating difficult overlaps. Tests bound timestamp reads and traced memory.
- Summary contents navigation reuses heading elements and reads current
  geometry with binary search for ordinary block headings. Preserve the linear
  fallback for tables/snapshot grids and refresh references on summary changes.
- Saved Meeting playback reuses `createReviewPlaybackLookup` per segment
  snapshot; preserve canonical/alignment/start-time priority and stable ties.
  Do not build the interval index for live previews without saved playback audio.
  Keep transcript edit/undo callbacks and virtualizer item keys stable, and
  avoid whole-transcript scans on playback ticks. `Meetings.test.tsx` gates zero
  row renders within an unchanged active segment; the timeline tests gate at
  most 64 timestamp reads per seek on 12,000 overlapping synthetic segments.
  `npm run benchmark:meeting-review` reports informational CPU timings and
  index construction cost; it is not an installed Windows latency claim.
- Settings renders persisted values independently of microphone discovery and
  native autostart. Preserve shared bootstrap requests, cache expiry, generation
  guards, and in-flight detachment on invalidation; late peripheral results must
  not overwrite user edits or notify after unmount.
- Settings keeps custom vocabulary, the summary and live-cleanup prompts, and
  both custom OpenRouter model codes in `lib/text-draft-store.ts` stores, not
  page state. A keystroke renders only its field. Loads, resets, rollback and
  canonicalization write the store; blur saves and "Use" read it. Vocabulary
  autosave and localized-default prompt replacement subscribe to their store
  with the former debounce/recheck semantics. Tests require zero page-wide
  tooltip renders while typing.
- The Visualizer-bars Settings control owns drag state and its saved-value
  reference. Commit through the existing Settings write queue. Failed earlier
  writes must not overwrite a newer draft, and reverting to the saved value
  behind an outstanding write must still persist that revert.
- `VirtualTranscriptHistory` is memoized; Live Mic/YouTube/File must keep both
  row-render callbacks and next-page callbacks stable across unrelated inputs.
  Include all row-visible action state in callback dependencies. Preserve
  internal scroll/resize updates, replacement data, locale and layout changes.
- Live Mic hydrates `/api/state` once on route re-entry because the shared
  WebSocket does not replay its initial snapshot. Newer lifecycle and transcript
  events fence stale responses. Preserve completed text on idle snapshots and
  clear it when a new session starts, including an explicitly empty transcript.
- Live Mic interim text, elapsed time, status and warnings stay inside the
  memoized `LiveMicStage`; history query, search and cards stay in the page.
  The stage receives only the history refresh callback for a finished session.
- The Meetings page owns each note queue, pagehide flush and autosave delay
  without subscribing to drafts; `MeetingNotesEditor` subscribes through
  `useMeetingNotesAutosaveSnapshot`. Restart the delay only when draft text or
  dirtiness changes, as the former render-driven effect did. The page-owned
  chat question is a draft store read by `MeetingChatComposer`; Meeting
  switches and successful answers still clear it, view switches keep it.
- Meeting transcript rows keep separate memo boundaries. Pass draft/saving
  state only to the affected row, and keep playback callbacks independent of
  unrelated detail changes. The page-local display projector consumes immutable
  segment snapshots and a stable label resolver; change resolver identity when
  speaker metadata or locale changes. Its positional fast path and weak-key
  fallback must preserve text, timing, revision, speaker and locale updates.
  Tests bound row formatting and new display objects; benchmark CPU timings
  remain informational and include the initial projection cost.
- Debug Console keeps virtualizer key callbacks and level counts stable for
  unchanged log snapshots. Memoized log messages receive stable copy callbacks
  and row-local feedback; serialize raw structured JSON only when opened.
- Paginated transcript endpoints and virtualized history lists.
- Shared history virtualizers and the Live Mic/YouTube/File callers keep stable
  item-key functions; scrolling must not reconstruct every loaded row's key.
  Completed summary renderers skip unrelated UI updates but invalidate when
  content or locale changes. Intl formatter caches are bounded to 32 instances
  per kind, retain no rendered text, and reset date formatters on window focus.
- Idle settings/device, active-Meeting, and history preloads are independent;
  keep history requests sequential/frame-yielded, forward query abort signals,
  and suppress late settings publication after cancellation.
- Transcript history indexes include the ID tie-breaker and status for ordered,
  covering reads. Migrate only their app-owned definitions once. Punctuation
  search reuses FTS visible-summary text with a fallback for missing/mismatched
  index rows; preserve literal substring semantics and hidden-HTML exclusion.
- Metadata-only transcript saves compare the real indexed projection under the
  parent write transaction and skip unchanged FTS rewrites. Preserve missing/
  stale-row repair, summary-format semantics and rollback on projection errors.
- Upload patches that change no field keep snapshot identity and do not notify
  subscribers. Preserve the server-processing transition after rounded 100%.
  File history partitions depend on history data, not upload or copy UI state.
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
- Live Meeting `meeting_segment` merges are linear: one scan plus binary-search
  insertion, with the stable full sort only for unsorted input or comparator
  ties. `meeting-segment-merge.test.ts` proves equivalence with
  replace/append-then-sort and holds a deterministic `startMs`-read ceiling
  for a 3,000-event stream; lower that ceiling when work drops, never raise it
  to admit a regression.
- Meeting library rows prefetch detail after 150 ms of sustained pointer or
  keyboard-focus intent, with the query's own 30-second stale time. Keep one
  speculative request active, cancel unused owned requests on leave, and retain
  requests adopted by navigation or observed elsewhere. Never cancel an existing
  consumer's request or prefetch the selected Meeting or one pending discard.
  A speculative response must retain cache updates received while it was in
  flight, including live segments and edits, instead of restoring its stale snapshot.
  Review matching uses one pass; build the membership set only for active
  filters and reuse visible rows for timeline matches. Unfiltered previous/next
  navigation still needs the ordered IDs.
- Chunked/offloaded upload writes and export/cleanup work where practical.
- JobStore and latency metrics store connection reuse.
- App-owned aiohttp provider connection reuse with DNS caching and bounded
  privacy-safe connection/chunk timing.
- Provider-aware file preparation preserves allowed compressed originals,
  prefers MP3 for new batch encodes, and records its exact
  format/capability decision before upload; generated derivatives are cleaned
  without becoming canonical source evidence.
- CORS origin decision cache.
- Primary-tab code for Live Mic, Meetings, YouTube, File, Podcasts and Settings loads eagerly
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
  plus exact file-integrity metadata and the stable QuickJS wrapper quartet;
  yt-dlp is part of the frozen Python package set rather than a media executable;
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
  unchanged. `native/scriber-audio-sidecar` owns the independent worker version
  and PE resources; it compiles the existing audited audio source modules.
  App-version changes must preserve this finished product. Source, dependency,
  worker-version, toolchain, and resource changes still invalidate it. Tauri
  bundles the validated worker as an external binary and must not rebuild it as
  a desktop Cargo target. GitHub release builds keep
  `build\rust-audio-sidecar-cache` in a separate Actions cache from the Python
  backend sidecar cache so Python/backend changes do not force an audio sidecar
  executable rebuild.
- Release workflow cache keys normalize app-version-only files before hashing
  dependency/build caches, so patch version bumps do not invalidate frontend,
  Rust, or backend scratch caches without real input changes. Exact
  version-bearing outputs are excluded from this rule: composed backend and
  Tauri app must change, while audio retains its own worker version. The main Rust release key still
  includes real Tauri shell inputs such as `tauri.conf.json`, capabilities, and
  icons.
- Frontend dependency reuse in GitHub release builds is two-layered: restore
  `Frontend\node_modules` first, then restore the explicitly keyed npm package
  store only when that stronger cache misses. `actions/setup-node` must not
  eagerly restore the same package store on a hot `node_modules` path. Cache
  maintenance retains exactly one active
  `node-cache-Windows-x64-npm-*` generation on `main` for the two Quality
  Gates; do not classify that live setup-node family as obsolete.
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
- Release and quality workflows pin the `dtolnay/rust-toolchain` action by
  full commit SHA. Keep the Rust version and compiled-cache identity aligned.
  When changing toolchains, rebuild the cache and verify a warm run.
- `Frontend\src-tauri\Cargo.toml` keeps the shell library crate type to
  `["rlib"]` for Windows desktop releases. Do not restore Tauri mobile
  `staticlib`/`cdylib` outputs unless mobile targets are introduced; they create
  extra release library artifacts that do not help the NSIS updater build.
- `v*` tag releases require Tauri updater signing. Authenticode is optional
  until `SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE=1` is configured; that switch
  remains fail-closed for certificate, publisher, and timestamp evidence.
- Non-tag GitHub cache/warmup builds use `-NsisCompression none` by default to
  reduce packaging time and intentionally ignore `SCRIBER_NSIS_COMPRESSION`.
  Use `SCRIBER_NON_TAG_NSIS_COMPRESSION` only for explicit non-tag packaging
  experiments. The workflow records uncompressed artifact size but disables the
  compressed-installer size gate for `none` non-tag runs; signed `v*` releases
  must continue to enforce the normal size budget. Signed `v*` updater releases
  may use `SCRIBER_NSIS_COMPRESSION` after a measured size/time tradeoff.
- Evaluate changes to release compression or dependency caches using a fresh
  `build-timing.json` and `release-artifact-summary.json` from the applicable
  build. The [packaging handbook](../PERFORMANCE_AND_PACKAGING.md#installer-build-modes)
  records the signed compression sweep and warm-cache measurements.
- Release workflow Actions caches are backed by internal GitHub release
  artifacts for the Python virtualenv, Python wheelhouse, backend sidecar cache,
  main Rust/Tauri build cache, Rust audio sidecar cache, and FFmpeg Profile B so
  sibling tag builds can reuse heavy outputs even when ref-scoped Actions caches
  miss. The main Rust/Tauri release artifact supports a latest-prefix fallback
  only when Actions reports no matched key; a partial Actions restore must not
  trigger the 1.6-GB fallback. Ordinary `main` pushes do not run the full
  installer workflow. Cold Tauri producers export current missing Rust/frontend
  dependency snapshots after product readiness; a detached main-branch job
  verifies canonical publication/source provenance and passively seeds caches
  visible to future tags. Large durable Cargo/venv/wheelhouse snapshots still
  use the explicit `refresh_release_cache_artifacts=true` maintenance path. Tag releases do,
  however, self-heal a missing bounded exact backend, FFmpeg, audio, or
  diarization finished-product artifact after a successful rebuild. Manual
  cache publication is allowed only from `main` with
  `refresh_release_cache_artifacts=true`; feature-branch diagnostics are
  read-only with respect to shared caches. An explicit refresh must enter the
  exact CPython-versioned `.venv` and wheelhouse restore/validation path even
  when the complete backend is already reusable, verify both products, publish
  both durable artifacts, seed missing main Actions caches, and fail unless
  both publishers confirm success. That maintenance path retains exactly one
  Actions-cache generation per allowlisted family, removes superseded internal
  cache-release tags, and current cache publishers keep only their replacement
  asset. Generic durable cache ZIPs must use native Windows bsdtar with standard
  Deflate, archive from inside the source root so dot/hidden files survive, and
  verify exact path/count/uncompressed-length inventory parity before upload;
  keep their current `.zip` names and `Expand-Archive` restore compatibility.
  After best-effort GC, the maintenance workflow must perform a fresh,
  non-best-effort inventory pass that requires the exact computed Rust
  dependency key to be the sole main-branch Rust generation and rejects any
  remaining allowlisted GC candidate. Publish and verify the app release first,
  then upload the four independent finished-component cache products in
  parallel with one private `GITHUB_OUTPUT` file per child. Those
  finished-component uploads remain best-effort and must not delay or
  invalidate an already verified updater release. Heavy
  Actions caches remain restore-only on tags, so a routine release has one
  complete tag-triggered build rather than a duplicate main warm-up plus tag
  build.
- The Rust Actions cache is keyed by normalized Cargo dependency metadata plus
  resolved toolchain/target/profile, not by ordinary app source. The exact
  Tauri bundle-binary set is a separate small cache keyed by full Rust/frontend
  sources, concrete version, commit, toolchain, target/profile, updater runtime
  fingerprint, and the transfer scripts. It must attest and transfer both
  `scriber-desktop.exe` and `minisign-verify.exe`; Tauri enumerates both package
  binaries during `tauri bundle`, even though only the desktop executable is
  launched as the app. A validated hit may run bundle-only packaging; NSIS,
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
  validation. Treat every versioned Profile B tag and locked ZIP as immutable:
  reuse an exact existing asset, fail closed on any mismatch, and introduce a
  new `vN` tag plus reviewed lock for changed bytes. General cache refreshes
  must never clobber the locked generation.
- Profile B ffmpeg media tools, about `5.11 MiB` installed. Meeting
  finalization requires the FLAC encoder, Matroska and Ogg muxers, and `amix`;
  keep all four in the profile and fixture gate.
