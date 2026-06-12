# Scriber

Last verified: 2026-06-09

Scriber is a Windows-first AI transcription app for live dictation, YouTube
transcription, file transcription, transcript history, summaries, and PDF/DOCX
export.

The current primary runtime is:

- Tauri 2 desktop shell for the installed Windows app.
- React 19 + Vite 7 frontend.
- Python `aiohttp` backend sidecar on loopback.
- SQLite runtime data under the configured app data directory.

Legacy Tkinter/Python desktop paths still exist as maintenance fallback, but new
desktop work should target the Tauri runtime.

## Current Status

The hybrid Tauri/Python runtime is the active implementation:

- The Rust shell starts or attaches to a Scriber backend through `/api/health`.
- Managed workers receive a per-run `SCRIBER_SESSION_TOKEN`; REST and WebSocket
  calls are token protected except for health readiness.
- The Tauri shell owns Windows single-instance startup, autostart, global
  hotkey registration, tray lifecycle actions, and worker crash recovery.
- The backend owns recording state, provider calls, transcript persistence,
  jobs, device enumeration, media preparation, logs, and support bundles.
- The frontend reports `/api/runtime/frontend-ready` after it proves that the
  actual WebView can reach the runtime backend.

Recent completed work:

- Native Windows microphone change notifications with polling fallback.
- Recording-aware PortAudio refresh and guarded device enumeration.
- Optional always-on mic prewarm with a rolling prebuffer.
- Canvas/RAF waveform rendering and about 60 Hz audio-level throttling.
- Lazy provider imports and cached analyzer setup.
- Buffered live transcript appends to avoid repeated full-string rebuilds.
- Paginated and virtualized transcript history lists.
- Token-protected debug console with filters, clear-log support, copy-visible
  logs, and redacted support bundles.
- Azure MAI Transcribe defaulting to `mai-transcribe-1.5`; custom vocabulary is
  sent as `phraseList` for that model.
- Azure MAI upload preparation uses low-latency MP3 encoding instead of WAV.
- FFmpeg Profile B is the default no-feature-loss bundled media-tool profile.
- Fast local installer builds can reuse the unchanged PyInstaller sidecar.

Latest validated local build evidence from 2026-06-09:

- Python tests: `465 passed`.
- Frontend type check: `npm run check` passed.
- Frontend build: `npm run build` passed.
- Fast local NSIS installer with Profile B passed installed frontend and media
  preparation smokes.
- Installer: about `102.98 MiB`.
- Installed app: about `267.28 MiB`.
- Backend resource tree: about `254.42 MiB`.
- Installed bundled media tools: about `5.84 MiB`.

## Features

- Live microphone dictation with global hotkey.
- Toggle and push-to-talk recording modes.
- Native overlay for recording, transcribing, and audio visualization.
- Microphone selection, favorite microphone fallback, and device hotplug refresh.
- YouTube search, URL lookup, download, transcription, and summarization.
- File upload transcription for audio and video inputs.
- Transcript history with type filters, search, detail view, delete, summarize,
  cancel, and export.
- PDF and DOCX export.
- Provider routing, retry scheduling, and circuit-breaker support.
- Debug console and redacted support bundle creation in the installed app.

Provider coverage includes Soniox, Mistral, AssemblyAI, Azure MAI, OpenAI,
Deepgram, Gladia, Groq, Speechmatics, ElevenLabs, Google, AWS, Smallest, ONNX,
and NeMo paths. Verify a provider in code before changing its contract because
some providers have realtime, async, or local-model-specific behavior.

## Screenshots

![Live Mic](docs/screenshots/live_mic.png)

![YouTube](docs/screenshots/youtube.png)

![File Upload](docs/screenshots/file_upload.png)

![Transcript Detail](docs/screenshots/transcript_detail.png)

![Settings](docs/screenshots/settings.png)

## Quick Start

### Windows Dev Startup

```powershell
git clone https://github.com/MyButtermilk/Scriber.git
cd Scriber
.\start.bat
```

`start.bat` prepares the Python environment, creates an initial `.env` when
needed, starts the backend, and starts the web UI when Node dependencies are
available.

### Manual Backend

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
python -m src.web_api
```

Backend default:

- Host: `127.0.0.1`
- Port: `8765`
- Health: `http://127.0.0.1:8765/api/health`

### Manual Frontend

```powershell
cd Frontend
npm install
npm run dev:client
```

Frontend dev default:

- `http://localhost:5000`

### Tauri Dev

```powershell
cd Frontend
npm run tauri:dev
```

### Fast Local Installer

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -UseProfileBFfmpeg `
  -ValidateSlimMediaTools `
  -ReuseSidecarIfUnchanged `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke
```

The generated NSIS setup is under:

```text
Frontend\src-tauri\target\release\bundle\nsis\
```

## Configuration

Common environment values:

- `SCRIBER_WEB_HOST`, `SCRIBER_WEB_PORT`: backend bind address.
- `SCRIBER_DATA_DIR`: writable runtime data directory.
- `SCRIBER_SESSION_TOKEN`: local API/WebSocket token in managed runtime.
- `SCRIBER_SERVICE`: active transcription provider.
- `SCRIBER_HOTKEY`, `SCRIBER_MODE`: desktop hotkey behavior.
- `SCRIBER_MIC_ALWAYS_ON`: enables idle mic prewarm when set to `1`.
- `SCRIBER_MIC_PREBUFFER_MS`: rolling prebuffer duration, capped in config.
- `SCRIBER_SONIOX_ASYNC_MODEL`: default `stt-async-v5` for Soniox Async
  file/YouTube transcription.
- `SCRIBER_SONIOX_RT_MODEL`: default `stt-rt-v4` for Soniox realtime live
  transcription.
- `SCRIBER_AZURE_MAI_MODEL`: default `mai-transcribe-1.5`.
- `SCRIBER_CUSTOM_VOCAB`: custom vocabulary, sent as Azure MAI `phraseList`
  for `mai-transcribe-1.5`.
- `SCRIBER_MEDIA_TOOLS_DIR`: explicit ffmpeg/ffprobe directory override.

Runtime data in desktop mode is stored under `SCRIBER_DATA_DIR`, including
settings, `.env`, transcripts, downloads, logs, and support bundles.

## Tests

Run from the repository root unless noted.

```powershell
python -m pytest
```

```powershell
cd Frontend
npm run check
npm run build
```

```powershell
cd Frontend\src-tauri
cargo test
```

Useful focused gates:

```powershell
python scripts\smoke_frontend_browser.py --output tmp\frontend-browser-smoke.json
python scripts\smoke_media_preparation.py --media-tools-dir Frontend\src-tauri\target\release\backend\tools\ffmpeg
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe `
  -VerifyFrontend `
  -VerifyMediaPreparation `
  -VerifySupportBundle `
  -VerifyUninstall
```

## Documentation

Active documentation is intentionally small:

- `AGENTS.md`: editing guide for future agents.
- `docs/ARCHITECTURE.md`: current runtime and code architecture.
- `docs/PERFORMANCE_AND_PACKAGING.md`: implemented performance work,
  packaging decisions, installer size, and remaining optimization ideas.
- `docs/TESTING_AND_RELEASE.md`: tests, smokes, release flow, and CI gates.
- `docs/ROADMAP_AND_KNOWN_ISSUES.md`: current open issues and prioritized
  next work.

Old implementation journals, partial plans, and superseded analysis files were
removed during the 2026-06-09 documentation consolidation. Prefer the current
code over historical notes when there is a conflict.

## Troubleshooting

### Backend Not Available

Check the Tauri debug console first. In installed builds, backend and shell logs
are under the runtime data directory in `logs\`. Use the in-app support bundle
when reporting startup or backend issues.

Useful checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health
```

### Microphone Changes

Device changes are backend-authoritative. The browser/WebView can send a
best-effort hint, but PortAudio enumeration is guarded and refreshes are deferred
while a recording stream is active.

### Missing YouTube or File Media Support

The installed Windows package should bundle Profile B ffmpeg/ffprobe. For dev
mode, set `SCRIBER_MEDIA_TOOLS_DIR`, `SCRIBER_FFMPEG_PATH`, or make ffmpeg and
ffprobe available on `PATH`.

### Slow Stop-To-Text

For cloud STT providers, most stop-to-final-text latency can be provider
finalization and network roundtrip. Use hot-path metrics before optimizing local
code.
