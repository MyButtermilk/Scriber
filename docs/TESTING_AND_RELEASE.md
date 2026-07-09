# Testing And Release

Last verified: 2026-07-09

This document consolidates test, smoke, installer, release, signing, and updater
notes.

## Core Test Commands

Run from repository root unless specified.

Python:

```powershell
python -m pytest
```

Frontend:

```powershell
cd Frontend
npm run check
npm run build
```

Rust:

```powershell
cd Frontend\src-tauri
cargo test
```

Frontend browser smoke:

```powershell
python scripts\smoke_frontend_browser.py --output tmp\frontend-browser-smoke.json
```

## Important Test Areas

Backend contracts:

- `tests/contract/test_ws_events.py`
- `tests/contract/test_rest_contracts.py`
- `tests/test_web_api_security.py`

Mic/device:

- `tests/test_device_monitor.py`
- `tests/test_microphone_device_resolution.py`
- `tests/test_microphone_channel_selection.py`
- `tests/test_microphone_callback.py`
- `tests/test_mic_prewarm.py`

Pipeline/provider/runtime:

- `tests/test_pipeline_stop.py`
- `tests/test_azure_mai_stt.py`
- `tests/runtime/test_provider_router.py`
- `tests/runtime/test_retry_scheduler.py`
- `tests/core/test_state_machine.py`
- `tests/core/test_provider_circuit_breaker.py`

Web/API/jobs:

- `tests/test_web_api_lifecycle.py`
- `tests/test_web_api_jobs.py`
- `tests/test_web_api_job_resume.py`
- `tests/test_web_api_reliability.py`
- `tests/test_web_api_timeouts.py`

Performance/packaging:

- `tests/perf/test_hot_path_tracer.py`
- `tests/perf/test_frontend_vendor_chunk_config.py`
- `tests/test_tauri_security_gates.py`
- `tests/test_tauri_stability_smoke_gates.py`
- Tauri bundle resources include the Python `backend/` resource tree. The Rust
  audio sidecar is bundled once as Tauri's install-root
  `scriber-audio-sidecar.exe` and is the standard live-mic capture/prewarm
  engine. `scripts\build_windows.ps1` prepares the Python backend sidecar before
  calling `tauri build`, then writes a generated minimal Tauri config overlay
  under `build\tauri-release-config\` with only the concrete app version and
  release-only overrides such as `beforeBundleCommand = null`. This keeps fresh
  CI runners compatible with Tauri's early resource-path validation while preserving the checked-in
  `beforeBundleCommand` for direct developer `npm run tauri:build` workflows.
- `requirements-base.txt` pins the Pipecat/provider SDK combination used by the
  frozen backend runtime import gate. Pipecat, Deepgram, Speechmatics RT, and
  `speechmatics-voice` must move together: unpinned Pipecat or provider SDK
  drift can break fresh CI runners, while `pipecat-ai[speechmatics]` would pull
  `transformers`/HuggingFace into the standard installer.
- The frozen runtime import gate also covers the AssemblyAI realtime Pipecat
  module and `onnx_asr`. This protects the installed AssemblyAI Universal-3.5
  realtime path and the bundled ONNX local-ASR fallback used when full NeMo is
  unavailable.
- Live-mic post-processing coverage should include prompt-template tests,
  Settings payload typing, and Rust global-hotkey dispatch tests. The expected
  behavior is a second shortcut that posts to
  `/api/live-mic/toggle-post-processing`; the normal shortcut must keep plain
  output. Debug coverage should also verify that post-processing diagnostics are
  exposed only as redacted metadata through the Debug Console, hot-path metrics,
  and support bundles.

## Installer Builds

`src/version.py` is the leading release version. `scripts\sync_version.py`
copies that value into the frontend package metadata before building and keeps
`Frontend\src-tauri\tauri.conf.json` pointing at `..\package.json` for direct
developer builds. Cargo package metadata intentionally stays on a stable
internal version so patch-only app releases do not invalidate the main Rust
release cache. `scripts\build_windows.ps1` passes the concrete app version to
Tauri through a generated release config, and the Rust shell passes that same
version to the Python backend through `SCRIBER_VERSION`. In GitHub Actions tag
releases, the sync step must match the `v*` tag; if `src/version.py` still
contains an older version, the signed release build fails instead of silently
publishing an installer with the wrong version.

Fast local Profile B installer:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke
```

`-FastLocalInstaller` enables Profile B media tools, sidecar cache reuse,
runtime dependency footprint checks, and local `lzma` NSIS compression by
default. Build metadata records the effective compression and marks these
artifacts as `devOnly=true`. Explicit `-NsisCompression zlib`, `bzip2`, `lzma`,
or `none` remains available for installer builds; GitHub signed tag releases
may set the `SCRIBER_NSIS_COMPRESSION` repository variable when a measured
packaging speed/size tradeoff is desired. GitHub non-tag cache/warmup builds
use `none` by default to reduce NSIS packaging time and are not affected by
`SCRIBER_NSIS_COMPRESSION`; use `SCRIBER_NON_TAG_NSIS_COMPRESSION` only for an
intentional non-tag packaging experiment.
Treat non-tag cache/warmup timings as cache-health evidence, not signed-release
packaging evidence. The 2026-07-09 hot `workflow_dispatch` measurement
`28997179965` proved the heavy cache path by completing `build_windows.ps1` in
about `49.2s` with exact hits for backend sidecar, Rust build, Rust audio
sidecar, FFmpeg Profile B, frontend dependencies, and Tauri bundler cache.
That run used the non-tag packaging shape. A signed `v*` release still needs
its own timing review because default NSIS compression, updater signing,
GitHub release upload, and publication verification can be the dominant
remaining cost.

Typical output:

```text
Frontend\src-tauri\target\release\bundle\nsis\Scriber_<current-version>_x64-setup.exe
```

Broader local installed workflow smoke:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke `
  -RunInstallerSupportBundleSmoke `
  -RunInstallerUninstallSmoke
```

Real file/YouTube workflow smoke, when credentials and network are available:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke `
  -RunInstallerRealMediaWorkflowSmoke
```

For the fastest local app-start/package loop without NSIS:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalStagedApp `
  -SkipChecks `
  -SkipSmoke
```

This produces a staged `target\release\scriber-desktop.exe` plus sidecars,
records `buildMode.artifactKind=staged-app`, and does not claim installer
validation.

Default real YouTube smoke URL:

```text
https://www.youtube.com/watch?v=0wEjbSYNUM8
```

## Smoke Gate Coverage

Installed frontend smoke verifies:

- installed backend starts without dev Python/Node fallback,
- frontend entrypoint is served,
- referenced JS/CSS assets are served,
- Tauri-origin CORS works for `/api/health`,
- tokenized `/api/runtime` works,
- the real WebView reports frontend-ready.

Installed package smoke also verifies that the bundled
`scriber-audio-sidecar.exe` exists at the installed app root. This is a
packaging gate only; it does not promote Rust audio capture to the default
engine.

Rust audio prewarm sidecar smoke verifies the prewarm lifecycle:

- `prewarmStart` returns a `prewarmId`,
- `prewarmStop` is idempotently routed through the sidecar client,
- stop-health reports observed and buffered frame counters,
- no raw native endpoint IDs are required or exposed,
- `--mode synthetic` checks protocol plumbing,
- `--mode wasapi` starts a real passive WASAPI idle capture stream.

It can be run locally after building the sidecar binary. Synthetic mode remains
the default for CI-safe plumbing checks:

```powershell
python scripts\smoke_rust_audio_prewarm_sidecar.py `
  --mode synthetic `
  --duration-sec 1 `
  --prebuffer-ms 400 `
  --output tmp\rust-audio-prewarm-sidecar-smoke.json
```

Use WASAPI mode on a Windows machine with a real microphone to exercise the
passive idle stream:

```powershell
python scripts\smoke_rust_audio_prewarm_sidecar.py `
  --mode wasapi `
  --duration-sec 1 `
  --prebuffer-ms 400 `
  --output tmp\rust-audio-prewarm-sidecar-wasapi-smoke.json
```

This lifecycle-only report does not prove active-capture adoption. Use
`--prewarm-before-capture` on the Rust sidecar capture smoke when the evidence
needs to show buffered idle frames flowing into the next capture:

```powershell
python scripts\smoke_rust_audio_sidecar.py `
  --mode wasapi `
  --duration-sec 1 `
  --prebuffer-ms 400 `
  --prewarm-before-capture `
  --skip-selected-hash `
  --output tmp\rust-audio-sidecar-adopt-wasapi-smoke.json
```

Use the app-level smoke to verify that Python's `RustAudioPrewarmManager`
actually hands the adopted `prewarmId` to `RustPrototypeFrameSource`, that the
sidecar emits prebuffer frames before live frames, and that idle prewarm resumes
after capture:

```powershell
python scripts\smoke_rust_audio_app_prewarm.py `
  --mode wasapi `
  --duration-sec 1 `
  --prewarm-duration-sec 1 `
  --prebuffer-ms 400 `
  --output tmp\rust-audio-app-prewarm-wasapi-smoke.json
```

The app-level smoke ignores locally configured favorite microphones by default
so release evidence uses the stable Windows default endpoint. In the default
case, Rust requests must keep `devicePreference=default` and omit
`nativeEndpointIdHash`; the WASAPI sidecar must then report
`endpointSelection.mode=default` and `usedDefaultEndpoint=true`. Add
`--honor-favorite-mic` only for a targeted selected-device investigation. These
Rust audio smokes still do not promote Rust audio to the default engine; longer
physical Always-On-Mic matrix evidence and provider-backed transcription smokes
remain required. The release-readiness validator also redaction-gates the Rust
audio sidecar, prewarm sidecar, and app prewarm smoke artifacts: raw
`SWD\MMDEVAPI\...` endpoint IDs and raw `\\.\pipe\scriber-*` pipe names must
not appear in those reports; only hashes or explicit redaction markers are
acceptable release evidence.

For selected/favorite microphone investigations, a valid Rust prewarm artifact
must either show a redacted native endpoint hash for that selected device or
fail closed before capture. It must not silently fall back to
`endpointSelection.mode=default` for a different microphone. When Python/PyCAW
native inventory is empty in the Tauri runtime, `RustAudioPrewarmManager` can
query private shell IPC `audioEndpointInventory` and use that redacted endpoint
inventory for the selected/favorite mapping. Standalone sidecar smokes without
Tauri shell IPC can still fail closed for selected devices; that is safer than
opening the wrong microphone and is not sufficient installed-app evidence.
The 2026-06-11 Insta360 investigation used this rule: standalone sidecar
evidence failed closed without a native hash, then the rebuilt Tauri backend
support bundle proved `prewarm.engine=rust-wasapi`, the selected
`Mikrofon (4- Insta360 Link)` label, a matching redacted native endpoint hash,
and `usedDefaultEndpoint=false`.

Use the recording hot-path benchmark when Rust audio evidence must include the
actual provider path, not only sidecar frame delivery. The strict provider/Rust
flags require a final STT provider transcript and verify that
`/api/runtime/audio-diagnostics` reported an active `rust-wasapi`
`rust-frame-pipe` capture during recording. The same gate rejects reports where
`microphone.rustAudioFallbackCircuit.open` is true, because those runs prove a
fallback cooldown rather than valid Rust capture evidence. When the Rust report
shows `micAlwaysOn=true`, the individual benchmark summary also requires
redacted `rustPrewarmAdoption` evidence before `rust_audio_engine` is marked
`measured`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 `
  -RecordHotPathSamples `
  -RequireRecordingHotPathProviderTranscript `
  -RequireRecordingHotPathRustAudio `
  -RecordingHotPathSpeechPrompt "Scriber provider-backed Rust audio validation"
```

This gate needs real provider credentials, microphone access, and the Rust
prototype environment, for example `SCRIBER_AUDIO_ENGINE=rust-wasapi` plus
the WASAPI sidecar feature flags used by the current prototype. Add
`-RequireRecordingHotPathTextTarget` with a controlled
`-RecordingHotPathTextTargetFile` when the evidence also needs to prove
end-to-end text insertion into a target window.

The provider-backed Python-vs-Rust comparison runner is now historical or for
pre-promotion builds that still contain Python capture. It was used for the
2026-06-11 aggressive Rust/WASAPI decision: Rust clearly improved median
hotkey-to-mic-ready and hotkey-to-first-audio latency, delivered valid
`rust-frame-pipe` samples, adopted prewarm, reported no dropped frames, and
kept the fallback circuit closed. Current builds use Rust/WASAPI as the only
live-mic capture path, so a `SCRIBER_AUDIO_ENGINE=python` request is not a
product-path selector anymore. Provider-finalize and total stop-to-text latency
remain visible in reports but are network/STT dominated and do not decide local
audio capture ownership.

For old builds or comparison archaeology, the runner still exists:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_recording_hot_path_comparison.ps1 `
  -RustAlwaysOnMic `
  -RecordingHotPathIterations 3 `
  -RecordingHotPathSeconds 3 `
  -RecordingHotPathEnvFile .env `
  -RecordingHotPathDefaultStt soniox `
  -RecordingHotPathSonioxMode realtime `
  -RecordingHotPathSpeechPrompt "Scriber provider-backed Rust audio validation"
```

The runner sets a legacy `SCRIBER_AUDIO_ENGINE=python` request for the first
pass, then runs the Rust/WASAPI pass. Use `-RecordingHotPathEnvFile` plus
explicit provider overrides when the comparison must load credentials and
provider defaults from a local/release `.env`; the runner records only the file
path and provider names, never secret values. It finally calls:

```powershell
python scripts\validate_recording_hot_path_comparison.py `
  --python-report tmp\hybrid-baseline\python-recording-hot-path-baseline-recording-hot-path-1.json `
  --rust-report tmp\hybrid-baseline\rust-recording-hot-path-baseline-recording-hot-path-1.json `
  --min-samples-per-report 3 `
  --max-audio-owned-p95-regression-ms 50 `
  --output tmp\hybrid-baseline\recording-hot-path-python-rust-comparison.json
```

The comparison validator rejects unredacted input reports before producing
promotion evidence. Raw `SWD\MMDEVAPI\...` endpoint IDs, raw
`\\.\pipe\scriber-*` pipe names, and non-redacted token fields in either the
Python or Rust hot-path report fail the comparison gate. Final hybrid
readiness also requires the resulting comparison artifact to contain a passing
`inputReportRedaction`, `rustAlwaysOnMic`, `rustMidSessionClean`,
`rustFramePipeFlow`, `rustNoDroppedFrames`, `rustActiveCaptureStable`, and
`rustPrewarmAdoption` checks, so stale comparison artifacts created before
those gates cannot be reused for Rust promotion.

The final readiness runner can require that artifact:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RequireRecordingHotPathComparison
```

It can also produce the artifact directly when provider credentials,
microphone access, and the app under test are available:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunRecordingHotPathComparison `
  -RequireRecordingHotPathComparison `
  -RecordingHotPathIterations 3 `
  -RecordingHotPathSeconds 3 `
  -RecordingHotPathEnvFile .env `
  -RecordingHotPathDefaultStt soniox `
  -RecordingHotPathSonioxMode realtime
```

That runner path calls `scripts\run_recording_hot_path_comparison.ps1` with
`-RustAlwaysOnMic`, writes
`recording-hot-path-python-rust-comparison.json` into the hardware input
directory, and then passes it into final readiness validation.

The hybrid release-readiness runner can produce and validate this app-level
report when Rust audio promotion evidence is being assembled:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunRustAudioAppPrewarmSmoke `
  -RequireRustAudioAppPrewarmSmoke
```

Required app-prewarm evidence now includes the Rust prewarm watchdog status
path. The smoke calls `RustAudioPrewarmManager.ensure_healthy()` before capture
adoption and after idle resume; the report must include
`managerPreAdoptionHealth` and, when resume is enabled,
`managerPostResumeHealth`. Final readiness rejects reports where those snapshots
do not prove active `audioPrewarmStatus` responses with redacted prewarm IDs,
non-negative response times, empty health errors, and
`healthRestartCount=0`. It also requires the bounded redacted `recentEvents`
timeline to contain the expected lifecycle markers: `started` before adoption,
and `adopted_for_capture`, `resume_active_capture`, and `started` after idle
resume. Post-resume snapshots must also expose positive
`activeCaptureResumeReadyCount` plus non-negative
`lastActiveCaptureResumeGapMs`, `lastActiveCaptureStopToReadyMs`, and
`maxActiveCaptureStopToReadyMs`. This prevents a cached `prewarmId` from
counting as proof that Always-On-Mic was still holding a live Rust/WASAPI
prewarm session, and prevents a report with a hidden idle-session dropout or an
unmeasured stop-to-prewarm-ready gap from passing as stable promotion evidence
merely because the watchdog recovered before the final snapshot.
The same app-prewarm smoke rejects source-final reports with
`midSessionFailureReason`, `fallbackReason`, non-empty `lastError`, or a
`framePipeReaderEndReason` other than empty, `stopRequested`, or `endOfStream`.
This keeps app-level Always-On-Mic promotion evidence aligned with the
installed live-recording and provider-backed comparison gates: a broken Rust
frame pipe cannot pass merely because adoption counters were positive before
the break.

For the Always-On-Mic handoff regression specifically, evidence should prove
that adopted WASAPI capture does not stop idle prewarm from the parent
`captureStart` handler. The sidecar must transfer the old `PrewarmSession` into
the capture writer, write adopted prebuffer blocks, call `IAudioClient.Start()`
for the replacement stream, and then stop prewarm with reason
`adoptedIntoCapture`. Failure reports should preserve explicit reasons such as
`captureStartFailed` or `captureWriterFinishedBeforePrewarmHandoff`. Physical
or installed-app checks should include the post-idle hotkey case, not only
rapid repeated hotkey presses, because the observed regression appeared after
the app had been idle while `SCRIBER_MIC_ALWAYS_ON=1` was enabled.

The running app also persists recovered idle-prewarm watchdog restarts under
`watchdog.lastWarning` when `healthRestartCount` increases during a watchdog
check. Support bundles should therefore show the last brief Always-On-Mic
dropout/recovery even when the stream was already healthy again before the user
opened the Debug Console or clicked Stop in the recording popup.

For Always-On-Mic promotion evidence, make the app-level smoke a long run and
require the same durations plus repeated stop/resume capture cycles in final
validation:

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

Supplying either app-prewarm minimum duration or the minimum capture-cycle count
also makes the app-prewarm smoke artifact required, even without the generic
`-RequireRustAudioAppPrewarmSmoke` flag. `-RequireRustAudioPromotionReadiness`
sets the capture-cycle minimum to `2`, so promotion evidence must prove that
Always-On-Mic resumes after at least two active recording stop events. Final
readiness validates each cycle's pre-adoption and post-resume
`audioPrewarmStatus` snapshots plus their `recentEvents` lifecycle markers, so
a report cannot pass by showing only a final healthy prewarm state after one
failed resume.

The top-level release-readiness runner can also produce and validate the
lifecycle report when explicit lifecycle evidence is wanted:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunRustAudioPrewarmSidecarSmoke `
  -RequireRustAudioPrewarmSidecarSmoke
```

Add `-RustAudioPrewarmSidecarMode wasapi` to make that runner gate use the real
passive WASAPI prewarm worker. The gate remains separate from
`-RequireRustAudioSidecarSmoke`: it does not by itself require Rust/WASAPI
endpoint inventory in the physical microphone matrix because it still does not
prove active-capture adoption.

Installed media-preparation smoke verifies real helper paths against bundled
media tools:

- file upload compression,
- upload audio extraction,
- YouTube post-download normalization,
- Azure MAI MP3 preparation,
- optional duration probing.

Support bundle smoke verifies:

- token protection,
- redaction of dummy secrets in env/settings/logs,
- required diagnostic ZIP members,
- native device-event diagnostics are present and redacted when the Tauri shell
  IPC is available; on supported Windows runs where native events are not
  disabled, the smoke requires COM initialization, monitor registration, and
  callback liveness evidence,
- Rust audio fallback-circuit diagnostics are present and redacted; when the
  circuit is open, the smoke requires reason and remaining-cooldown evidence,
- Rust Always-On-Mic prewarm diagnostics are covered by the REST contract and
  support-bundle redaction: stop-to-prewarm-ready gap metrics must stay typed,
  while raw `prewarmId` / `prewarm_id` values are rejected or redacted,
- redacted audio/text-injection diagnostics, including the latest sanitized
  Tauri `injectText` attempt when present, and Shell IPC transport failures do
  not leak raw pipe names or session tokens,
- redacted post-processing diagnostics in
  `post-processing-diagnostics.redacted.json`, including status, model,
  duration, size counters, and sanitized errors without raw transcript text or
  processed output,
- support-bundle text redaction removes raw Scriber Shell IPC named-pipe paths
  from env files and logs; the installed support-bundle smoke injects a dummy
  Shell IPC pipe path and fails if any raw `scriber-shell-*` pipe remains,
- support-bundle text redaction removes raw Windows `SWD\MMDEVAPI\...` native
  audio endpoint IDs from logs; the installed smoke injects a dummy endpoint ID
  and fails if the raw endpoint survives,
- restoration of runtime `.env` and `settings.json` after the test.

Other available installed smokes include:

- worker crash recovery,
- occupied default port fallback,
- controlled worker shutdown and supervisor recovery,
- bounded Rust audio sidecar cleanup on backend restart and shell exit,
- external backend attach,
- startup-timeout replacement,
- idle stability,
- live recording stability,
- legacy data migration,
- upgrade data preservation,
- strict silent uninstall,
- hotkey registration and optional dispatch/manual hotkey evidence.

## CI

`.github/workflows/hybrid-pr-checks.yml` is the fast PR gate.

It runs focused hybrid Python gates, frontend typecheck/build, and Tauri Rust
tests. It intentionally does not build the full NSIS installer on every PR.
Node setup uses the repository `.node-version` file so CI stays aligned with
`Frontend/package.json` engines. Keep GitHub-owned actions on current majors
(`checkout`, `setup-python`, `setup-node`, `cache`, and `upload-artifact`) so
the workflows do not fall back to deprecated Node action runtimes.

`.github/workflows/release-windows.yml` is the Windows release build.

It:

- sets up Python, Node, Rust, and MSYS2/UCRT64,
- runs on `main` pushes as a cache-warming build and on `v*` tags as the signed
  updater release path,
- computes normalized release cache key files before dependency setup so
  version-only changes in `package-lock.json` and `src/version.py` do not
  invalidate dependency caches that do not actually depend on the app version.
  Cargo metadata is kept stable, and the release-only Tauri version is injected
  through a generated config overlay,
- reports entry counts and short SHA-256 fingerprints for each normalized cache
  key file in the GitHub Step Summary. Compare these fingerprints between runs
  before assuming a cache miss means unnecessary dependency rebuilding,
- restores heavyweight caches with `actions/cache/restore` and saves them only
  on `main` or an explicit cache-refresh dispatch. Signed `v*` tag releases are
  restore-only for those large caches, so they do not spend post-job time
  uploading tag-scoped cache payloads that sibling tags cannot reliably reuse,
- restores release caches for Python `.venv`, Python wheels, frontend
  `node_modules`, Rust/Tauri, backend sidecars, and Profile B media tools. The
  Node setup step also restores the npm package store from the normalized
  `build\cache-keys\frontend-dependencies.txt` input, so a cold `node_modules`
  cache can still install from a warmer download cache without version-only
  lockfile churn changing that fallback key. On an exact `node_modules` cache
  hit, the workflow checks npm install metadata only; `npm run check` remains
  the real frontend correctness gate inside `scripts\build_windows.ps1`,
- restores the backend sidecar cache before Python `.venv` and wheelhouse
  restore. When a prebuilt backend sidecar is available, the workflow skips the
  Python dependency environment entirely and lets the sidecar builder perform
  only the frozen-sidecar validation/copy path. The workflow-level backend
  sidecar cache key includes the resolved Python version and mirrors the
  builder's relevant source/build inputs closely enough that a restored cache
  root should contain the expected internal sidecar key,
- restores Python `.venv` from the internal `release-cache-python-venv-v1`
  artifact when the ref-scoped Actions cache is cold, so unchanged Python
  requirements can skip pip installation entirely after `pip check`,
- restores setup-python's pip download/build cache from
  `requirements-base.txt` and `requirements-build.txt` as a final fallback when
  both the prebuilt backend sidecar and wheelhouse/venv layers miss,
- can import the newest internal Rust/Tauri cache artifact as a prefix fallback
  when the exact Rust cache key is absent,
- reports exact Actions hits, ambiguous Actions `restore-key-or-miss` outputs,
  release-artifact fallbacks, and cheap path evidence separately. In GitHub
  cache terminology, `cache-hit=false` can mean a restore-key hit or a true
  miss. The same evidence is uploaded as `release-cache-summary.json`; use it
  with the effective source and path evidence in the summary before concluding
  that unchanged packages were downloaded or rebuilt,
- downloads from a finished GH run can be reduced to an Oracle/AutoResearch
  input with
  `python scripts\summarize_release_artifacts.py tmp\installer-speed-runs\<run-id> --output tmp\installer-speed-runs\<run-id>\release-artifact-summary.json`.
  Current release workflow runs already generate `release-artifact-summary.json`
  before uploading `scriber-windows-release`, so this command is mainly for
  regenerating the summary after local artifact inspection.
  The summary combines `release-metadata\build-timing.json` and
  `release-cache-summary.json` so speed reviews start from measured phases and
  cache evidence instead of raw-log screenshots. It also reads backend sidecar
  metadata from `build-timing.json`, including the internal PyInstaller sidecar
  cache hit, Rust audio sidecar cache hit, and nested sidecar phases. Rust cache
  evidence includes a combined `Rust build` row for automated correlation plus
  split Cargo/target rows for manual diagnosis. The summary emits diagnostic
  codes for common timing causes, including `pyinstaller-rebuilt`,
  `rust-audio-rebuilt`, `backend-sidecar-cache-not-hot`,
  `effective-cache-miss`, `ambiguous-actions-restore`, and
  `tauri-bundle-dominant`. It also emits recommendation codes such as
  `inspect-backend-sidecar-cache`, `inspect-rust-audio-cache`,
  `inspect-effective-cache-misses`, `inspect-path-evidence`, and
  `profile-tauri-bundle`,
- captures the Tauri bundle console output as
  `release-metadata\tauri-windows-bundle.log` and writes
  `tauri-bundle-log-summary.json` with counts for Cargo index updates, crate
  downloads, Cargo compile lines, NSIS, updater/signing lines, and the first
  compile lines. ANSI color sequences are stripped before matching so colored
  Cargo output from GitHub logs is counted reliably. Each captured line is
  timestamped, so the same summary also reports first-output-to-`makensis`,
  `makensis`-to-updater-signature, and first-output-to-last-output durations.
  The capture path runs `npm run tauri:build` through
  `cmd.exe /d /s /c "... 2>&1"` because Tauri/Node can write normal
  informational lines to stderr. Those lines are not release failures unless
  the native exit code is non-zero, and PowerShell must not surface them as
  `NativeCommandError`.
  The release artifact summary reads
  this file and can emit `tauri-crate-downloads-detected`,
  `tauri-cargo-compile-detected`, `tauri-bundle-no-cargo-rebuild-detected`, or
  `tauri-nsis-signing-heavy`, plus recommendations such as
  `inspect-tauri-cargo-fingerprints`, `measure-nsis-signing`, or
  `profile-nsis-compression-signing`,
- includes the version-stable `tauri.conf.json`, Tauri capabilities, and app
  icons in the main Rust release cache key so those real shell inputs still
  invalidate the cache while patch-version-only churn does not,
- keeps the backend sidecar version-neutral only because the Rust supervisor
  injects `SCRIBER_VERSION` and `src.version.app_version()` prefers that runtime
  value. The `tests/test_version_contract.py` regression tests protect this
  contract; if they fail, stop normalizing `src/version.py` out of the backend
  sidecar cache key until version reporting is fixed,
- enables `CARGO_INCREMENTAL=1` for the main Tauri release binary and caches
  `Frontend\src-tauri\target\release\incremental` in both the Actions cache and
  the internal Rust release artifact. This gives version-bump and small
  Rust-shell edits a warmer rebuild path while preserving normal release
  validation,
- builds the Tauri shell library as `rlib` only for the Windows desktop
  release path. The `staticlib` and `cdylib` crate types are kept out of this
  Windows-first package because Tauri documents them for mobile targets and
  they produced extra library artifacts that do not participate in the NSIS
  updater installer,
- accepts optional `SCRIBER_CARGO_LOG` as a GitHub repository variable. Set it
  to `cargo::core::compiler::fingerprint=info` for a diagnostic run when Cargo
  recompiles unexpectedly, then unset it after the fingerprint reason is known,
- restores Profile B from a reusable GitHub release artifact when the
  per-ref Actions cache is cold, validates restored Profile B media tools before
  reuse, and builds FFmpeg Profile B only when neither restored source passes
  validation,
- appends the `Build Windows installer` phase timings from
  `release-metadata\build-timing.json` to the GitHub Step Summary, so residual
  build time can be attributed before changing dependency caches,
- collects only release artifacts listed in `release-metadata\latest.json`. If
  a listed artifact has a non-empty updater signature in `latest.json`, the
  sibling `<artifact>.sig` file is required and the workflow fails before upload
  if it is missing,
- keeps non-tag cache/warmup artifact uploads metadata-only by default. The
  installer is still built and validated, but the large `.exe` and `.sig` are
  copied to `release-artifacts` only for signed `v*` tags or when
  `SCRIBER_UPLOAD_FULL_NON_TAG_INSTALLER=1` is set for a deliberate non-tag
  installer download,
- passes the produced media tools to `scripts/build_windows.ps1`,
- skips the full Python unit suite in the packaging step; run it before release
  or through PR/readiness gates. The release workflow therefore installs
  `requirements-base.txt` and `requirements-build.txt`, but not
  `requirements-dev.txt`,
- runs media-preparation and runtime dependency footprint gates,
- uploads or publishes NSIS artifacts and release metadata; GitHub Actions
  artifact upload uses `compression-level: 0` because the installer is already
  compressed and double-compressing it burns runner time with little or no size
  benefit,
- optionally validates Authenticode signatures and Tauri updater metadata when
  signing/updater secrets are configured.

Normal tag releases restore internal release-cache artifacts but do not
automatically repack and clobber the large Python `.venv`, wheelhouse, Rust,
backend sidecar, Rust audio sidecar, or FFmpeg cache assets. `main` pushes are
the cache-warming path and may refresh those internal artifacts after real cache
misses. A manual maintenance refresh is also available through
`workflow_dispatch` with `refresh_release_cache_artifacts=true`. Signed app
release tags should spend time on changed installer inputs, not on re-uploading
unchanged reusable cache payloads.

The Python backend sidecar cache is allowed to be version-neutral for
`src/version.py` because the Tauri supervisor passes the installed app version
through `SCRIBER_VERSION` at runtime and `src.version.app_version()` reads that
environment override. Keep that runtime contract intact when changing version
reporting; otherwise a cached sidecar could report the wrong installed version.
The Rust shell uses Tauri package metadata for that value, not
`CARGO_PKG_VERSION`, because Cargo package metadata is deliberately stable for
cache reuse.

For build-time triage, use the GitHub Step Summary and
`release-metadata\build-timing.json` before removing checks. The v0.4.15 GitHub
release build showed that media-preparation smoke and runtime dependency
footprint were about 1.4 seconds and 0.25 seconds respectively, while backend
PyInstaller and Tauri/NSIS dominated the build. Keep the cheap release evidence
gates unless they become a measured bottleneck.

`scripts\ci\write_release_cache_keys.ps1` writes the normalized key inputs used
by the release workflow. Keep this normalization in place when changing release
version files; otherwise patch-only version bumps will cause avoidable cache
misses for frontend dependencies, Rust build outputs, and backend sidecar
scratch caches.

The reusable FFmpeg Profile B artifact is published to the internal prerelease
tag `ffmpeg-profile-b-n7.0-v2` as
`scriber-ffmpeg-profile-b-n7.0-v2-Windows.zip`. That asset is not an app
release. It is only a fallback source for future release builds when GitHub
Actions cache scope isolation prevents a new tag from seeing a previously saved
cache. Every restored copy is still validated with the Profile B manifest and
media-preparation smoke before it can be bundled. Refresh it only through the
manual release-cache refresh workflow path after real Profile B input changes.

## Signing And Updater Status

Tauri updater plugin wiring exists in the shell, frontend settings UI, and the
startup path for installed builds. The frontend owns update checks: it waits
briefly after startup, checks the configured updater endpoint in the background
about once per week, caches the latest result locally, and suppresses update
notifications while recording or transcription is active. Settings exposes
manual check, install/restart, release notes, one-day deferral, per-version
skip, and an automatic-check toggle. The tray panel also surfaces actionable
updates with a prominent install-and-restart button, while the Windows tray icon
switches to a blue download badge for available updates and a recording badge
while capture is active.

The Python backend must not run an updater cron or ping. Update publication is
validated at release time through signed Tauri metadata.

The GitHub release workflow uses Tauri's free updater artifact signing with
these repository secrets/variables:

- `SCRIBER_TAURI_UPDATER_PUBLIC_KEY`
- `TAURI_SIGNING_PRIVATE_KEY`
- `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`
- `SCRIBER_TAURI_UPDATER_ENDPOINT`

`scripts\prepare_tauri_updater_config.py` prepares the generated release Tauri
config overlay: it writes only the concrete app version, release-only
`beforeBundleCommand = null`, optional NSIS compression, updater artifacts, the
public key/endpoints, and Windows updater passive install mode. It should write
to `build\tauri-release-config\...` for release builds instead of mutating or
copying the checked-in `tauri.conf.json`. An empty
`SCRIBER_TAURI_UPDATER_ENDPOINT` falls back to the standard GitHub
`latest.json` endpoint. For local signed builds, `scripts\build_windows.ps1` also accepts
`TAURI_SIGNING_PRIVATE_KEY_PATH` and normalizes it to
`TAURI_SIGNING_PRIVATE_KEY` before invoking the Tauri CLI.
`v*` tag release jobs fail when updater signing is missing unless
`SCRIBER_ALLOW_UNSIGNED_TAG_RELEASE=1` is set deliberately for a one-off
unsigned tag test build.
`scripts\create_release_metadata.py` prefers artifacts whose filename contains
the current app version when auto-discovering release files, and
`scripts\build_windows.ps1` applies the same current-version filter before
writing release metadata. This keeps stale installers left in the bundle tree
out of `latest.json`.

Publishing an update still requires uploading the installer, `.sig`,
`latest.json`, and `SHA256SUMS.txt` to a public HTTPS GitHub Release endpoint,
then running publication verification against the released `latest.json`.

Authenticode validation exists as a gate, but actual signing requires a real
certificate or cloud-signing provider.

Unsigned local builds are valid for development and smoke testing, but they do
not satisfy final external release-readiness.

The release-readiness runner can invoke the Windows release build before
validating final evidence:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunReleaseBuild `
  -ReleaseBuildEnableTauriUpdater `
  -ReleaseBuildRequireUpdaterSignatures `
  -ReleaseBuildRequireAuthenticodeSignature `
  -ReleaseBuildUseProfileBFfmpeg `
  -ReleaseBuildValidateSlimMediaTools `
  -ReleaseBuildRunMediaPreparationSmoke `
  -ReleaseBuildRunRuntimeDependencyFootprint
```

This calls `scripts\build_windows.ps1` and can produce signed updater metadata,
release metadata, media-preparation evidence, runtime-footprint evidence, and
the build's Authenticode validation report when the required secrets and
certificate/cloud-signing step are available. It still does not create signing
credentials or publish `latest.json`; publication verification remains a
separate gate.

## Release Readiness

Use the top-level release-readiness runner for final external evidence:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 -PlanOnly
```

For extended Rust/WASAPI release hardening, use the aggregate gate first:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RequireRustAudioPromotionReadiness `
  -PlanOnly
```

`-RequireRustAudioPromotionReadiness` turns on the full Rust-audio evidence
bundle without starting long hardware tests by itself. It requires the Rust
WASAPI sidecar smoke, app-level Always-On-Mic prewarm smoke, installed
live-recording smoke, provider-backed hot-path evidence,
Rust/WASAPI endpoint inventory in the physical microphone matrix, and native
device-refresh evidence. It also raises the promotion minima to a 10-minute
Rust sidecar smoke, 10-minute active app prewarm capture, 30-minute idle prewarm
window, and 10-minute installed live-recording smoke. For the installed
live-recording gate it also requires sampled `rust-wasapi` /
`rust-frame-pipe` audio diagnostics with a closed Rust fallback circuit. The
app-level prewarm gate requires at least two stop/resume capture cycles. The
provider-backed comparison artifact is useful only for old/pre-promotion builds
that still contain Python capture; current release evidence should focus on
Rust-only live-recording, prewarm, provider, endpoint, and device-refresh
reports. Add the matching `-Run...` or `-UseExisting...` flags when producing
or reusing reports.

For physical Rust audio hardening, add the sidecar smoke, Rust endpoint
inventory evidence, and native DeviceMonitor refresh evidence as hard gates:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunRustAudioSidecarSmoke `
  -RequireRustAudioSidecarSmoke `
  -RustAudioSidecarDurationSec 600
```

`-RequireRustAudioSidecarSmoke` and `-RequireRustAudioAppPrewarmSmoke`
automatically make the physical microphone matrix require
`--require-rust-endpoint-inventory` and `--require-device-refresh-evidence`.
Device-refresh evidence must show positive native Tauri refresh-hint and
native-hint PortAudio-refresh deltas, so legacy Python/native monitor events
alone cannot satisfy Rust-native device-event evidence.

Add sidecar-local prewarm adoption evidence to that physical smoke when testing
Rust prewarm parity:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunRustAudioSidecarSmoke `
  -RequireRustAudioSidecarSmoke `
  -RustAudioSidecarDurationSec 600 `
  -RustAudioSidecarPrewarmBeforeCapture
```

`-RequireRustAudioSidecarSmoke` validates the generated
`rust-audio-sidecar-smoke.json` in `validate_hybrid_release_readiness.py`. The
report must come from real WASAPI capture, include default-device and selected
native-endpoint-hash runs, read frames without sequence gaps, and preserve
sidecar stop-health metrics. When `-RustAudioSidecarPrewarmBeforeCapture` is
set, the validator also requires positive adopted prewarm blocks in the default
capture, and the runner passes
`--require-rust-audio-sidecar-prewarm-adoption` so reused sidecar reports cannot
silently skip that evidence. Supplying `-RustAudioSidecarPrewarmBeforeCapture`
also makes the sidecar smoke artifact required, even without the generic
`-RequireRustAudioSidecarSmoke` flag. `-RequireRustAudioSidecarSmoke` also makes the
microphone hardware matrix validator require redacted Rust/WASAPI endpoint
inventory evidence for every physical device scenario. Without that flag the
Rust smoke remains visible in the runner plan but optional unless required by
the release evidence target.

The prewarm sidecar smoke can be added independently with
`-RunRustAudioPrewarmSidecarSmoke` and
`-RequireRustAudioPrewarmSidecarSmoke`. This validates
`rust-audio-prewarm-sidecar-smoke.json` for prewarm start/stop routing,
matching `prewarmId`, positive observed and buffered counters, and clean stop
health. `-RustAudioPrewarmSidecarMode wasapi` makes it use the real passive
WASAPI idle stream. It is useful lifecycle evidence, but it is not enough for
default Rust audio promotion without the sidecar capture adoption smoke,
app-wide Always-On-Mic lifecycle integration, and provider-backed transcription
smokes.

For installed live-mic start/stop stability evidence, require:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RequireInstalledLiveRecordingSmoke `
  -MinInstalledLiveRecordingDurationSec 600
```

This validates an existing `installed-live-recording-smoke.json` produced by
`scripts\build_windows.ps1 -RunInstallerLiveRecordingSmoke`,
`scripts\smoke_windows_installer.ps1 -LiveRecordingDurationSec`, or
`scripts\smoke_tauri_desktop.ps1 -LiveRecordingDurationSec` over an installed
app. Supplying `-MinInstalledLiveRecordingDurationSec` or
`-RequireInstalledLiveRecordingRustAudio` also makes this artifact required,
even without the generic `-RequireInstalledLiveRecordingSmoke` flag. The report
must show a `tauri-supervised` sidecar runtime, healthy `apiVersion=1`/ready
state, positive app/backend PID and backend-port metadata, clean live recording
start/stop state, no non-recording samples during the recording window,
stability samples that cover at least half of the expected probe count for the
requested duration, and verified cleanup. Add
`-RequireInstalledLiveRecordingRustAudio` when the report is used as Rust
promotion evidence; that requires every stability sample to include compact
audio diagnostics proving `audioEngine=rust-wasapi`,
`activeCapture.frameSource=rust-frame-pipe`,
`activeCapture.rustPrewarmAdoption.adopted=true` with a redacted prewarm hash,
active callbacks, no frame-pipe sequence/protocol/prebuffer-order errors, and
`rustAudioFallbackCircuit.open=false`. The same compact diagnostics must show
`activeCapture.healthRestartCount=0`,
`activeCapture.healthRestartThrottleCount=0`, an empty
`activeCapture.lastHealthFailureReason`, and an empty
`activeCapture.lastHealthRestartError`, so a recovered active-capture stall does
not pass as stable installed evidence. The same gate rejects
`activeCapture.midSessionFailureReason`,
`activeCapture.lastRustAudioMidSessionFailureReason`, nested source
mid-session failures, and any `framePipeReaderEndReason` other than empty or
`running`; this prevents a broken Rust frame-pipe from passing merely because a
later fallback circuit or final snapshot looks healthy. Rust-audio promotion
evidence also requires `liveRecording.micAlwaysOn=true` and
`audioDiagnostics.microphone.micAlwaysOn=true` in every stability sample, so
the installed report proves the Always-On-Mic path was active instead of only
proving an on-demand live recording. The same installed report must now include
`liveRecording.postStopAudioDiagnostics`: after the stop response and idle
state transition, the smoke polls `/api/runtime/audio-diagnostics` until the
idle Rust prewarm is active again. Rust promotion validation requires
`prewarmEngine=rust-wasapi`, `prewarmActive=true`, positive
`prewarmActiveCaptureResumeReadyCount`, zero
`prewarmActiveCaptureResumeFailedCount`, and non-negative post-stop
`prewarmLastActiveCaptureResumeGapMs`,
`prewarmLastActiveCaptureStopToReadyMs`, and
`prewarmMaxActiveCaptureStopToReadyMs`. This turns the visible mic-light
off/on transition after pressing Stop into measured installer-path evidence.
For default-device release evidence, the
compact diagnostics must also show
`activeCapture.sourceEndpointSelectionMode=default` and
`activeCapture.sourceEndpointSelectionUsedDefault=true`; this proves the
installed WASAPI sidecar opened the Windows default endpoint rather than a
PortAudio-to-native hash mapping. The Rust callback, frame-pipe, and audio-frame
counters must also increase across stability samples, so a stale positive
diagnostics snapshot cannot satisfy long-recording evidence. The validator also
rejects raw
`SWD\MMDEVAPI\...` endpoint IDs and raw `\\.\pipe\scriber-*` pipe names in the
installed live-recording artifact. It complements the provider-backed
Python/Rust hot-path comparison; it does not replace transcript-quality
evidence. To produce installed Rust-audio evidence, run the installed smoke with
`-LiveRecordingAudioEngine rust-wasapi -LiveRecordingRustAudioCaptureMode
wasapi -LiveRecordingMicAlwaysOn`; the build wrapper exposes the same path as
`-InstallerLiveRecordingAudioEngine rust-wasapi
-InstallerLiveRecordingRustAudioCaptureMode wasapi
-InstallerLiveRecordingMicAlwaysOn`. When the smoke needs provider credentials
from a local/release env file, pass `-LiveRecordingEnvFile .env` plus an
explicit provider override such as `-LiveRecordingDefaultStt soniox
-LiveRecordingSonioxMode realtime`; the installer smoke, build wrapper, and
release-readiness runner expose the same controls as
`-InstallerLiveRecordingEnvFile` / `-InstalledLiveRecordingEnvFile` and matching
provider-mode parameters. Secret values are loaded into the child process
environment but are not written into the smoke report. When
`-RequireInstalledLiveRecordingRustAudio` is used, the release-readiness runner
also enables `-InstalledLiveRecordingMicAlwaysOn` automatically so the produced
report can satisfy the Rust-promotion validator. The release-readiness runner
can now run the same installed smoke directly with
`-RunInstalledLiveRecordingSmoke -InstalledLiveRecordingInstallerPath <setup.exe>
-RequireInstalledLiveRecordingRustAudio`.

When evaluating whether Tauri/Rust text injection can become more than an
opt-in path, require safe target-window evidence as well:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RequireTauriTextInjectionSmoke
```

This validates an existing `tauri-text-injection-smoke.json` from
`scripts\smoke_text_injection_target.py --method tauri`. That smoke must be run
from a Tauri-managed backend environment so `SCRIBER_SHELL_IPC_PIPE`,
`SCRIBER_SHELL_IPC_TOKEN`, and `SCRIBER_SHELL_IPC_API_VERSION` are present. The
artifact must not be validate-only evidence; it must show safe target-window
text arrival, `injectText` Shell IPC success, and both `clipboard_set` and
`paste` markers. It must also include structured clipboard restore evidence:
`restoreScheduled` must match `restore.scheduled`, restore status fields must be
typed, and restore errors or disabled restore do not satisfy release evidence.
Foreground diagnostics must be redacted: successful evidence must provide hashed
window diagnostics and must not expose raw window titles, HWNDs, process IDs, or
process names. The validator also rejects raw Shell IPC pipe names and
unredacted token-like values in the report. It is only the safe target gate.
Manual Notepad, Word,
Outlook, browser, Electron, elevated, and Remote Desktop target-app evidence is
still required before changing defaults.

When the readiness runner itself is launched inside that Tauri-managed backend
environment, it can produce the safe-target artifact directly:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunTauriTextInjectionSmoke `
  -RequireTauriTextInjectionSmoke
```

This invokes `scripts\smoke_text_injection_target.py --method tauri`, writes
`tauri-text-injection-smoke.json`, then passes it into the aggregate validator.
Use `-UseExistingTauriTextInjectionSmokeReport` when the smoke was already run.

For a default-path decision, require the full installed target-app matrix:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RequireTauriTextInjectionMatrix
```

This validates `tauri-text-injection-matrix.json`, an aggregate artifact whose
`scenarios` list contains real Tauri `injectText` reports. Build it from the
individual reports with `scripts\build_tauri_text_injection_matrix.py`.
Required scenario IDs are `notepad`, `word`, `outlook`, `browser-input`,
`browser-contenteditable`, `electron`, `elevated-target`, `elevated-scriber`,
`clipboard-text`, `clipboard-non-text`, `clipboard-locked`,
`restore-user-copy`, and `restore-same-text-copy`. `remote-desktop` is optional
when unavailable, but if present it must pass the same Shell IPC, target text,
and marker checks. Every scenario must also prove `preDelayMode=auto` in the
redacted Shell IPC payload, and the Word/Outlook scenarios must show a positive
applied `timingsMs.preDelay` so default evidence proves the Rust foreground
policy, not Python-side title heuristics. Every scenario must also carry the
same structured restore evidence as the safe smoke; restore errors or disabled
restore fail the matrix. Foreground diagnostics must remain hashed/redacted in
every scenario. The same redaction gate rejects raw Shell IPC pipe names and
unredacted token-like values in every scenario report.

The aggregate runner can also build the matrix artifact from already collected
scenario reports:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunTauriTextInjectionMatrixBuilder `
  -TauriTextInjectionMatrixInputDir tmp\hybrid-baseline\tauri-text-injection `
  -TauriTextInjectionMatrixUnsupportedOptional "remote-desktop=Remote Desktop is not available on this test machine" `
  -RequireTauriTextInjectionMatrix
```

This only aggregates real target-app reports; it does not replace the manual
Notepad/Word/Outlook/browser/Electron/elevated/clipboard scenario runs. Use
`-UseExistingTauriTextInjectionMatrixReport` to reuse a validated aggregate
artifact.

The microphone matrix can also be run directly with the same Rust promotion
gates:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_microphone_hardware_matrix.ps1 `
  -RequireRustEndpointInventory `
  -RequireDeviceRefreshEvidence
```

That direct gate captures before/after audio diagnostics, validates
`rustNativeEndpointInventoryChange`, requires the `rust-wasapi` inventory
source, checks expected added/removed/default-change labels, and rejects raw
IMMDevice endpoint IDs, raw `\\.\pipe\scriber-*` pipe names, and unredacted
token fields in the artifact. With `-RequireDeviceRefreshEvidence`, each
scenario also proves native DeviceMonitor events are active, the safety poll
interval remains sparse, and the smoke did not use forced per-poll refresh
requests. Use `-ForceRefreshEachPoll` only for diagnosing legacy fallback
behavior, not for Rust-promotion evidence.

The aggregate readiness runner can produce the same physical matrix artifacts
before final validation when an operator is present:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RunMicrophoneHardwareMatrix `
  -MicrophoneMatrixUsbLabel "USB Mic" `
  -MicrophoneMatrixDockLabel "Dock Mic" `
  -MicrophoneMatrixBluetoothLabel "Bluetooth Headset" `
  -MicrophoneMatrixFavoriteLabel "Favorite Mic" `
  -RequireRustAudioPromotionReadiness
```

`-RunMicrophoneHardwareMatrix` invokes
`scripts\run_microphone_hardware_matrix.ps1`, writes the eight scenario
artifacts into `-HardwareInputDir`, then runs the normal matrix validator and
final readiness validator. If native device-refresh evidence is required, the
aggregate runner rejects `-MicrophoneMatrixForceRefreshEachPoll`; Rust
promotion evidence must prove the native event-driven path, not a forced poll
loop.

The final readiness validator expects evidence for:

- physical microphone hardware matrix,
- Rust audio sidecar physical smoke, Rust endpoint inventory evidence, and
  native DeviceMonitor refresh evidence when hardening the Rust/WASAPI path,
- installed live-recording smoke for Rust/WASAPI release evidence,
- Tauri text-injection safe target smoke before promoting Tauri/Rust injection
  beyond opt-in,
- Tauri text-injection target-app matrix before changing text-injection
  defaults,
- media preparation smoke,
- runtime dependency footprint,
- signed updater publication,
- Authenticode validation,
- release artifact metadata consistency.

Local unsigned builds should remain red for signing/updater portions.

## Build Artifacts

Common generated evidence:

- `release-metadata\size-report.json`
- `release-metadata\build-timing.json`
- `release-metadata\media-preparation-smoke.json`
- `release-metadata\runtime-dependency-footprint.json`
- `tmp\rust-audio-sidecar-smoke.json`
- `tmp\rust-audio-prewarm-sidecar-smoke.json`
- `tmp\hybrid-baseline\tauri-text-injection-matrix.json`
- `tmp\frontend-browser-smoke.json`
- `tmp\installer-smoke\`

Installer speed evidence:

- Download `scriber-windows-release` from the relevant GitHub Actions run.
- Inspect `release-artifact-summary.json` first; it combines
  `build-timing.json`, `release-cache-summary.json`, and
  `tauri-bundle-log-summary.json`.
- If heavy rows are exact cache hits and `Tauri sidecar preparation` is already
  single-digit seconds, do not spend more time on Python dependencies,
  PyInstaller, FFmpeg, Rust audio sidecar, or `node_modules` caching.
- For signed tag releases after heavy caches are hot, inspect the Tauri bundle
  summary around `makensis`, updater signing, upload, and publication
  verification. A non-tag `NsisCompression=none` run is useful as a cache
  proof, but not as the final signed-release timing baseline.
- The current signed hot-tag baseline is `v0.4.21` / run `28999468872` from
  2026-07-09. It completed in about `3m57s` end-to-end; the build script took
  about `137.5s`, with `Tauri Windows bundle` at `122.0s` and sidecar
  preparation at `6.2s`. Heavy caches were exact hits. Treat future slowdowns
  from this shape as Tauri/NSIS/signing/upload regressions first, not
  dependency-cache regressions.
- The 2026-07-09 compression sweep kept the same exact heavy-cache shape and
  measured signed tag installer tradeoffs:
  `tauri-default` `137.5s` / `74.4 MiB`, `none` `58.2s` / `189.3 MiB`,
  `zlib` `72.4s` / `92.4 MiB`, and `bzip2` `76.9s` / `90.3 MiB`.
  The repository variable `SCRIBER_NSIS_COMPRESSION` is set to `bzip2` as the
  current release default because it saves about one minute compared with the
  Tauri default while adding about `15.9 MiB`, and it avoids the very large
  `none` installer.
- Main run `29002731350` confirmed the split compression behavior for non-tag
  builds: with `SCRIBER_NSIS_COMPRESSION=bzip2` set for signed tags, the main
  warmup still used `nsisCompression=none`, completed the job in `2m17s`, and
  spent `58.44s` inside `build_windows.ps1`.
- Main run `29003544425` tested removing `dtolnay/rust-toolchain@stable` in
  favor of preinstalled runner Rust. It failed as an optimization: the job took
  `8m9s`, `build_windows.ps1` took `413.9s`, and the Tauri bundle log showed
  `285` Cargo compile lines. Treat that path as rejected unless the Rust cache
  is intentionally rebuilt and remeasured.
- Main run `29004179335` verified the rollback to `dtolnay`: the job returned
  to `2m34s`, `build_windows.ps1` took `55.1s`, and the Tauri bundle log showed
  only the expected single `scriber-desktop` compile line.
- Non-tag runs after the metadata-only artifact change should no longer upload
  the full uncompressed `none` installer by default. Inspect
  `artifact-upload-mode.json` in `scriber-windows-release`; it should report
  `isTagRelease=false` and `uploadFullInstaller=false`. Signed `v*` runs must
  still report `uploadFullInstaller=true` and publish the installer plus `.sig`.

These are evidence artifacts, not durable docs. Do not copy their full contents
into permanent Markdown unless a concise current result belongs in
`README.md` or `docs/PERFORMANCE_AND_PACKAGING.md`.
