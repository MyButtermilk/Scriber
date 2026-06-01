# Hybrid Architecture Baseline

Last verified: 2026-06-01

This document tracks the Phase 0 baseline gate for the hybrid architecture work:
React UI + Tauri/Rust desktop shell + Python worker.

The goal is not to claim the migration is faster by default. The goal is to
produce comparable before/after measurements and to make missing measurements
visible.

## Baseline Runner

Use the Windows runner from the repository root after building the Tauri release
executable and backend sidecar:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 -Iterations 3 -DisableDevFallback
```

For CI/headless-style startup measurements, skip the visible-window timing:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\measure_hybrid_baseline.ps1 -Iterations 3 -Hidden -DisableDevFallback
```

The script writes a JSON artifact under `tmp\hybrid-baseline\` and also prints
the same JSON to stdout. Use `-FailOnIncompleteGate` when the run should fail
unless every Phase 0 requirement is measured.

## Automated Today

`scripts/measure_hybrid_baseline.ps1` currently measures:

- cold start to main window visible via the Tauri process `MainWindowHandle`
  when the app is not started with `-Hidden` or `-SkipUiVisibleWait`;
- Tauri process start to managed backend listener;
- Tauri process start to `/api/health` ready;
- `/api/runtime` fetch latency with the session token;
- managed backend cleanup after Tauri exit;
- available hot-path metric segment names from `/api/metrics/hot-path`.

The runner intentionally reports an incomplete Phase 0 gate until all required
measurements are present. Missing fields are listed in
`phase0Gate.incompleteRequirements`.

## Hot-Path Segments

The backend hot-path tracer now emits all ordered milestone pairs, not just
adjacent pairs. The following segments are the key Phase 0 fields once a real
recording sample has run and text injection has completed:

- `hotkey_received_to_mic_ready_ms`
- `hotkey_received_to_first_audio_frame_ms`
- `stop_requested_to_first_paste_ms`
- `hotkey_received_to_first_paste_ms`

`first_audio_frame` is marked from the audio callback path before WebSocket/UI
throttling decisions, so it measures backend audio arrival even without a
connected frontend client.

## Still Open

The following baseline requirements are not automated yet:

- upload/export under load;
- WebSocket events/sec and JSON serialization cost;
- history scrolling with many transcripts.

Until those are wired into the runner or separate benchmark artifacts, the
Phase 0 gate must stay incomplete.
