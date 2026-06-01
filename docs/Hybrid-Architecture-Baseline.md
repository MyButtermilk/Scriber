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

WebSocket broadcast and JSON serialization cost are measured by default. Tune
that synthetic benchmark with `-WsIterations`, `-WsWarmup`, and
`-WsClientCounts`, or skip it with `-SkipWsBenchmark`.

Upload stream and export rendering load are also measured by default. Tune that
synthetic benchmark with `-UploadFiles`, `-UploadSizeMb`, `-UploadChunkMb`,
`-ExportIterations`, `-ExportConcurrency`, and `-ExportParagraphs`, or skip it
with `-SkipUploadExportBenchmark`.

Large-history browser scrolling is measured by default against the real React
history UI and a synthetic paginated mock backend. Tune it with `-HistoryItems`,
`-HistoryRoutes`, and `-HistoryViews`, or skip it with
`-SkipHistoryScrollBenchmark`.

General frontend route health is covered by `scripts/smoke_frontend_browser.py`.
It starts the Vite app with a synthetic backend and uses Chrome/Edge CDP to
verify `/`, `/youtube`, `/file`, `/settings`, and a transcript detail route
without requiring real API keys, microphone hardware, or the Python backend.

Live recording hot-path samples are opt-in because they open the microphone and
may inject transcribed text into the active app. Run with `-RecordHotPathSamples`
on a machine with microphone and provider credentials. Speak a short phrase
during the recording window if `stop_requested_to_first_paste_ms` should be
measured.

## Automated Today

`scripts/measure_hybrid_baseline.ps1` currently measures:

- cold start to main window visible via the Tauri process `MainWindowHandle`
  when the app is not started with `-Hidden` or `-SkipUiVisibleWait`;
- Tauri process start to managed backend listener;
- Tauri process start to `/api/health` ready;
- `/api/runtime` fetch latency with the session token;
- managed backend cleanup after Tauri exit;
- available hot-path metric segment names from `/api/metrics/hot-path`.
- optional live recording hot-path samples via
  `scripts/measure_recording_hot_path_baseline.py` when
  `-RecordHotPathSamples` is passed;
- concurrent synthetic upload stream writes, parallel PDF/DOCX export
  rendering, and `/api/health` plus `/api/state` responsiveness during that
  load via `scripts/measure_upload_export_baseline.py`;
- WebSocket JSON serialization, no-client broadcast fast path, and broadcast
  throughput with synthetic clients via `scripts/measure_ws_broadcast_baseline.py`.
- large transcript-history browser scrolling, API pagination, and rendered-card
  counts via `scripts/measure_history_scroll_baseline.py`.
- frontend route smoke coverage, expected-route text, history virtualization
  markers, and browser console/page errors via `scripts/smoke_frontend_browser.py`.

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

The following baseline requirement still needs a real spoken/injected sample:

- stop to text injection.

`-RecordHotPathSamples` can measure hotkey/API-start to recording state and
first audio frame even when no text is produced. The stop-to-injection segment
is only present when STT returns text and injection succeeds.
