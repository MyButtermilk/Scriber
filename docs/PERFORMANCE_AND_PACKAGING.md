# Performance And Packaging

Last verified: 2026-06-09

This document consolidates the previous performance, startup, mic, FFmpeg,
installer-size, and optimization notes.

## Current Baseline

Latest validated local Profile B installer build from 2026-06-09:

- Installer: about `102.98 MiB`.
- Installed app: about `267.28 MiB`.
- Backend resource tree: about `254.42 MiB`.
- Installed media tools: about `5.84 MiB`.
- Profile B portable media-tool build: about `4.98 MiB` for `ffmpeg.exe`,
  `ffprobe.exe`, and required runtime DLLs.
- Python tests: `465 passed`.
- Frontend type check and build passed.
- Installed frontend smoke passed.
- Installed media-preparation smoke passed `5/5`.
- Real installed file and YouTube workflow smoke previously passed `2/2` for
  the Profile B path with `https://www.youtube.com/watch?v=0wEjbSYNUM8`.

Historical comparison points:

- Full/Gyan-style media tooling produced much larger installed packages.
- Gyan Essentials remains a fallback path, not the default.
- Removing ffprobe is still an experiment because standard workflows expect
  ffprobe availability.

## Implemented Performance Work

Startup and imports:

- STT provider imports are mostly lazy in the service factory.
- Expensive VAD/analyzer setup is cached.
- Runtime import diagnostics are separated from normal readiness paths where
  possible.

Live mic:

- Optional idle mic prewarm keeps a PortAudio stream ready.
- Rolling prebuffer can prepend the latest audio frames when recording starts.
- Device-name/favorite resolution has a short TTL cache.
- Device refresh is deferred while a recording stream is active.
- Audio-level UI work is throttled to about 60 Hz.
- Live waveform uses Canvas/RAF instead of per-frame React state.

Backend/WebSocket:

- WebSocket broadcast skips JSON serialization when no clients are connected.
- Audio-level callback avoids scheduling frontend work when there are no clients
  and no overlay consumer.
- `history_updated` events are coalesced and carry enough context for targeted
  refresh behavior.
- Transcript append handling avoids repeated full transcript string rebuilds for
  long sessions.

Data and frontend:

- Transcript list endpoints use pagination and avoid full content loading for
  metadata views.
- Frontend history pages use infinite loading and scroll-container virtualization.
- Production frontend build uses manual vendor chunks.
- Route chunks are lazy for non-default pages.

I/O:

- Multipart upload writes use chunked helpers and offload file writes where
  practical.
- Export rendering and cleanup paths are moved off async request hot paths where
  practical.
- Job and latency stores reuse SQLite connections more efficiently.
- Settings persistence is debounced and flushed on shutdown.

Packaging/build:

- PyInstaller sidecar can be reused through a hash cache.
- Frontend dist timestamp changes alone should not invalidate the sidecar cache.
- Runtime dependency footprint gate rejects SciPy reintroduction and unused heavy
  runtime paths.

## FFmpeg Profile B

Profile B is the standard no-feature-loss Windows media-tool profile.

It is built through:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ffmpeg\build_profile_b_msys2.ps1
```

The build expects MSYS2/UCRT64 and can install dependencies when the script is
run with the matching option. The produced tools are validated by:

- Profile manifest creation.
- Fixture smoke checks.
- Scriber media-preparation smoke.
- Sidecar slim-media validation.
- Installed package media-preparation smoke.
- Real file/YouTube workflow smoke when provider credentials and network are
  available.

Profile B keeps required Scriber capabilities:

- MP3 encode/decode through `libmp3lame`.
- WebM/Opus support.
- AAC, Opus, MP3, FLAC, ALAC decode.
- stdout `pcm_s16le`.
- raw `s16le`.
- common local demuxers/muxers.
- `file` and `pipe` protocols.

It intentionally excludes unrelated network protocols, GPL/nonfree flags, video
encoders, and hardware acceleration stacks.

## Installer Build Modes

Fast local installer:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -UseProfileBFfmpeg `
  -ValidateSlimMediaTools `
  -ReuseSidecarIfUnchanged `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke
```

What it does:

- Syncs versions.
- Builds frontend.
- Reuses sidecar when inputs are unchanged.
- Builds Tauri/NSIS.
- Runs size and runtime dependency footprint gates.
- Runs selected installed-package smokes.

Release workflow:

- `.github/workflows/release-windows.yml` builds Profile B with MSYS2/UCRT64.
- It passes the produced media tools into `scripts/build_windows.ps1`.
- It collects size, media-preparation, runtime dependency, and timing evidence.
- Authenticode and Tauri updater signing gates are available but require real
  signing/updater secrets.

## Size Decisions

No-feature-loss constraints:

- No optional installer components.
- ffmpeg and ffprobe remain bundled in the standard installer.
- PySide6 remains bundled for the native mic overlay.
- Provider dependencies remain bundled when the provider is still supported by
  backend or UI configuration.
- Local ASR packaging remains separate from the standard cloud-provider sidecar.

Current packaging choices:

- `pyloudnorm` is provided locally without SciPy in the packaged sidecar.
- ONNXRuntime remains because Pipecat Silero VAD needs it.
- SciPy, Torch, NeMo, ONNX-ASR, ONNX tooling, numba, llvmlite, and unused
  ONNXRuntime tooling are excluded from the standard sidecar.
- Pillow AVIF binaries are excluded.

PySide6 size experiments are allowed only with installed overlay smoke evidence:

- prune translations,
- prune unused plugins,
- prune software OpenGL DLL.

Do not make those default without proving that the native overlay still works.

## Remaining Performance Opportunities

Highest-value current opportunities:

- Split stop-to-text latency into provider finalize, clipboard, and paste
  segments before optimizing locally.
- Run React Profiler on Live Mic during recording to catch broad subtree
  rerenders from status/history WebSocket events.
- Keep investigating UI responsive behavior at narrow widths.
- Add longer installed idle/live stability runs with CPU and memory budgets.
- Consider PySide6 pruning only after visible overlay smoke coverage is robust.

Lower-value or risky opportunities:

- yt-dlp extractor filtering. This is risky and should not be default.
- Removing ffprobe. This is an explicit experiment only.
- Rust audio engine. Treat as requested-only until a measured prototype beats
  the current Python audio path without maintainability loss.

## Current Direction

The best path forward is to keep the hybrid architecture:

- Rust/Tauri for desktop shell, process supervision, hotkey, autostart, tray,
  updater, and Windows integration.
- Python for audio pipeline, providers, media preparation, data, and contracts.
- React for the app UI.

This keeps the main speed gains already achieved without moving high-churn
provider and pipeline code into Rust prematurely.
