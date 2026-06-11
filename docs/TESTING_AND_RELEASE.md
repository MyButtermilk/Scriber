# Testing And Release

Last verified: 2026-06-11

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
- Tauri bundle resources include both `backend/` and the opt-in
  `audio-sidecar/` Rust prototype binary. The sidecar is packaged but not used
  by default.

## Installer Builds

Fast local Profile B installer:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -UseProfileBFfmpeg `
  -ValidateSlimMediaTools `
  -ReuseSidecarIfUnchanged `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke
```

Typical output:

```text
Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe
```

Broader local installed workflow smoke:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -UseProfileBFfmpeg `
  -ValidateSlimMediaTools `
  -ReuseSidecarIfUnchanged `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke `
  -RunInstallerSupportBundleSmoke `
  -RunInstallerUninstallSmoke
```

Real file/YouTube workflow smoke, when credentials and network are available:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -UseProfileBFfmpeg `
  -ValidateSlimMediaTools `
  -ReuseSidecarIfUnchanged `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke `
  -RunInstallerRealMediaWorkflowSmoke
```

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
`scriber-audio-sidecar.exe` exists under the installed `audio-sidecar/` resource
layout. This is a packaging gate only; it does not promote Rust audio capture to
the default engine.

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

Use the recording hot-path benchmark when Rust audio evidence must include the
actual provider path, not only sidecar frame delivery. The strict provider/Rust
flags require a final STT provider transcript and verify that
`/api/runtime/audio-diagnostics` reported an active `rust-prototype`
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
prototype environment, for example `SCRIBER_AUDIO_ENGINE=rust-prototype` plus
the WASAPI sidecar feature flags used by the current prototype. Add
`-RequireRecordingHotPathTextTarget` with a controlled
`-RecordingHotPathTextTargetFile` when the evidence also needs to prove
end-to-end text insertion into a target window.

For Rust audio promotion, run the recording hot-path benchmark once with the
default Python engine and once with `SCRIBER_AUDIO_ENGINE=rust-prototype`, then
turn both provider-backed reports into the required comparison artifact. Both
runs must use the same STT provider and the same benchmark configuration;
provider or configuration mismatches are rejected because they make latency
deltas ambiguous. The benchmark report now records a `requested` object, and
the comparison gate requires matching iterations, recording seconds, speech
prompt, prompt delay, and text-target settings across Python and Rust. The
dedicated runner defaults to three recording samples per engine, and final
readiness rejects comparison artifacts with fewer than three Python or Rust
samples. The Rust report must also prove `micAlwaysOn=true` in its runtime audio
diagnostics, so provider-backed evidence exercises the same Always-On-Mic path
intended for default promotion instead of only an on-demand Rust capture path.
The comparison validator also requires every Rust `rust-frame-pipe` sample to
include `activeCapture.rustPrewarmAdoption.adopted=true` plus a redacted prewarm
hash, and it rejects raw `prewarmId` / `prewarm_id` values in that evidence.
It also rejects clear P95 regressions in local audio-owned segments such as
first audio frame, first audible frame, and stop-to-last-chunk.
Provider-finalize and total stop-to-text latency remain visible in the report
but are not used as this local Rust-audio regression gate:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_recording_hot_path_comparison.ps1 `
  -RustAlwaysOnMic `
  -RecordingHotPathIterations 3 `
  -RecordingHotPathSeconds 3 `
  -RecordingHotPathSpeechPrompt "Scriber provider-backed Rust audio validation"
```

The runner sets `SCRIBER_AUDIO_ENGINE=python` for the first pass, then
`SCRIBER_AUDIO_ENGINE=rust-prototype` plus the requested Rust capture mode for
the second pass. With `-RustAlwaysOnMic`, it also sets
`SCRIBER_MIC_ALWAYS_ON=1` for the Rust pass. Non-plan comparison runs now fail
early without `-RustAlwaysOnMic`, because valid Rust promotion evidence requires
both `rustAlwaysOnMic` and `rustPrewarmAdoption` checks. It finally calls:

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
`inputReportRedaction`, `rustAlwaysOnMic`, and `rustPrewarmAdoption` checks, so
stale comparison artifacts created before those gates cannot be reused for Rust
promotion.

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
  -RecordingHotPathSeconds 3
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
- redacted audio/text-injection diagnostics, including the latest sanitized
  Tauri `injectText` attempt when present, and Shell IPC transport failures do
  not leak raw pipe names or session tokens,
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

`.github/workflows/release-windows.yml` is the Windows release build.

It:

- sets up Python, Node, Rust, and MSYS2/UCRT64,
- builds FFmpeg Profile B,
- passes the produced media tools to `scripts/build_windows.ps1`,
- runs media-preparation and runtime dependency footprint gates,
- uploads or publishes NSIS artifacts and release metadata,
- optionally validates Authenticode signatures and Tauri updater metadata when
  signing/updater secrets are configured.

## Signing And Updater Status

Tauri updater plugin wiring exists in the shell and frontend settings UI.

Updater artifacts and update publication require:

- Tauri updater public key,
- Tauri signing private key,
- HTTPS updater endpoint,
- generated and published `latest.json`,
- signature-required metadata validation,
- publication verification.

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

For a default-path Rust audio promotion decision, use the aggregate gate first:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 `
  -RequireRustAudioPromotionReadiness `
  -PlanOnly
```

`-RequireRustAudioPromotionReadiness` turns on the full Rust-audio promotion
bundle without starting long hardware tests by itself. It requires the Rust
WASAPI sidecar smoke, app-level Always-On-Mic prewarm smoke, installed
live-recording smoke, provider-backed Python-vs-Rust hot-path comparison,
Rust/WASAPI endpoint inventory in the physical microphone matrix, and native
device-refresh evidence. It also raises the promotion minima to a 10-minute
Rust sidecar smoke, 10-minute active app prewarm capture, 30-minute idle prewarm
window, and 10-minute installed live-recording smoke. For the installed
live-recording gate it also requires sampled `rust-prototype` /
`rust-frame-pipe` audio diagnostics with a closed Rust fallback circuit. The
app-level prewarm gate requires at least two stop/resume capture cycles. The
provider-backed comparison artifact must be produced from a Rust pass that used
`-RustAlwaysOnMic`. Add the matching `-Run...` or `-UseExisting...` flags when
producing or reusing those reports.

When evaluating whether the Rust audio prototype can be promoted, add the
physical sidecar smoke, Rust endpoint inventory evidence, and native
DeviceMonitor refresh evidence as hard gates:

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
Rust smoke remains visible in the runner plan but optional, so standard
Python-capture release builds are not blocked by prototype evidence.

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

When evaluating a default-path Rust audio promotion, also require installed
live-mic start/stop stability evidence:

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
audio diagnostics proving `audioEngine=rust-prototype`,
`activeCapture.frameSource=rust-frame-pipe`,
`activeCapture.rustPrewarmAdoption.adopted=true` with a redacted prewarm hash,
active callbacks, no frame-pipe sequence/protocol/prebuffer-order errors, and
`rustAudioFallbackCircuit.open=false`. The same compact diagnostics must show
`activeCapture.healthRestartCount=0`,
`activeCapture.healthRestartThrottleCount=0`, an empty
`activeCapture.lastHealthFailureReason`, and an empty
`activeCapture.lastHealthRestartError`, so a recovered active-capture stall does
not pass as stable installed evidence. Rust-audio promotion evidence also
requires `liveRecording.micAlwaysOn=true` and
`audioDiagnostics.microphone.micAlwaysOn=true` in every stability sample, so
the installed report proves the Always-On-Mic path was active instead of only
proving an on-demand live recording. For default-device release evidence, the
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
`-LiveRecordingAudioEngine rust-prototype -LiveRecordingRustAudioCaptureMode
wasapi -LiveRecordingMicAlwaysOn`; the build wrapper exposes the same path as
`-InstallerLiveRecordingAudioEngine rust-prototype
-InstallerLiveRecordingRustAudioCaptureMode wasapi
-InstallerLiveRecordingMicAlwaysOn`. The release-readiness
runner can now run the same installed smoke directly with
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
  native DeviceMonitor refresh evidence when evaluating the Rust prototype,
- installed live-recording smoke when evaluating a default-path Rust audio
  promotion,
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

These are evidence artifacts, not durable docs. Do not copy their full contents
into permanent Markdown unless a concise current result belongs in
`README.md` or `docs/PERFORMANCE_AND_PACKAGING.md`.
