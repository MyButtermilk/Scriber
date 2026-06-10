# Testing And Release

Last verified: 2026-06-10

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
- restoration of runtime `.env` and `settings.json` after the test.

Other available installed smokes include:

- worker crash recovery,
- occupied default port fallback,
- controlled worker shutdown and supervisor recovery,
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

## Release Readiness

Use the top-level release-readiness runner for final external evidence:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_hybrid_release_readiness.ps1 -PlanOnly
```

The final readiness validator expects evidence for:

- physical microphone hardware matrix,
- Rust audio sidecar physical smoke when evaluating the Rust prototype,
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
- `tmp\frontend-browser-smoke.json`
- `tmp\installer-smoke\`

These are evidence artifacts, not durable docs. Do not copy their full contents
into permanent Markdown unless a concise current result belongs in
`README.md` or `docs/PERFORMANCE_AND_PACKAGING.md`.
