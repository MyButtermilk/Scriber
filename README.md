# Scriber

**Scriber turns speech, YouTube videos, meetings, calls, and recordings into
usable text without forcing you into one transcription provider or one workflow.**

Use one global hotkey to dictate into any Windows app. Paste a YouTube link and
turn it into a transcript and summary. Drop in audio or video files, including
multiple files at once, and keep everything in one searchable local history.

[Download for Windows](https://github.com/MyButtermilk/Scriber/releases) |
[See Screenshots](#screenshots) |
[Features](#features) |
[Developer Setup](#for-developers)

![Scriber live microphone transcription](docs/screenshots/live_mic.png)

## Why Scriber

Most transcription tools are either a recorder, a YouTube helper, a file
transcriber, or a cloud-provider wrapper. Scriber is built as a desktop
workflow tool around the way spoken information is actually used.

| Common limitation | What Scriber does differently |
| --- | --- |
| Dictation is trapped inside a web page | Press a Windows hotkey, speak, stop, and Scriber pastes into the app you were already using. |
| You must trust one STT provider | Choose between streaming, async/batch, and local providers. Settings show practical price/error estimates where available. |
| File, YouTube, and microphone transcripts live in separate places | Scriber stores live recordings, YouTube transcripts, and uploaded files in one searchable local transcript history. |
| Summaries and cleanup are bolted on | Scriber has automatic summaries, manual summaries, and a separate live post-processing hotkey for cleaned dictation. |
| Desktop behavior is an afterthought | Tauri owns tray actions, global hotkeys, autostart, update checks, single-instance behavior, and the recording overlay. |
| Debugging is opaque | The Console view exposes logs, redacted support bundles, post-processing diagnostics, and runtime state. |
| Privacy choices are vague | Runtime data stays local. Audio/text is sent only to the provider you select, and local ONNX models need no API key. |

## Built For People Who Work From Spoken Information

Scriber is useful when you regularly handle calls, research videos, legal or
tax discussions, product notes, meeting recordings, voice notes, interviews, or
any workflow where spoken content needs to become structured text quickly.

- **Dictate anywhere:** use the normal hotkey for raw STT, or a second hotkey
  for polished live post-processing.
- **Capture long-form content:** transcribe YouTube videos and imported audio or
  video files without leaving the app.
- **Reuse the result:** search, copy, summarize, export to PDF/DOCX, or open
  transcript detail pages later.
- **Choose the model for the job:** low-latency streaming, cheaper async/batch,
  Gemini with one simple API key, or local ONNX/NeMo models.

## Screenshots

### Live Dictation

Scriber keeps live recording simple: one button or hotkey, waveform feedback,
recent recordings, search, and fast reuse.

![Live microphone view](docs/screenshots/live_mic.png)

### YouTube To Transcript

Search YouTube or paste a URL, then turn a video into a transcript and summary
that lives beside your other recordings.

![YouTube transcription view](docs/screenshots/youtube.png)

### File Transcription

Drag in audio or video files. Scriber prepares media with bundled ffmpeg/ffprobe,
tracks progress, and saves the finished transcript.

![File transcription view](docs/screenshots/file_upload.png)

### Summary, Transcript, Export

Transcript detail pages keep summaries, full transcript text, copy actions, and
PDF/DOCX export in one place.

![Transcript detail view](docs/screenshots/transcript_detail.png)

### Settings

Configure microphone behavior, providers, API keys, custom vocabulary,
summaries, live post-processing, local models, updates, and language from one
screen.

![Settings view](docs/screenshots/settings.png)

## Features

### Live Microphone

- Global Windows hotkey for start/stop dictation.
- Optional push-to-talk or toggle mode.
- Separate live post-processing hotkey for cleaned dictation.
- Prompt-based post-processing for punctuation, paragraphs, filler-word removal,
  numbers, dates, currency, units, and professional formatting.
- Rust/WASAPI audio capture sidecar for Windows-first microphone handling.
- Optional mic pre-warming for lower recording-start latency.
- Speech gate with Silero VAD so silent starts can be skipped locally.
- Optional pause-based speech segmentation for providers that benefit from it.
- Native overlay with waveform visualization.
- Clipboard-aware text injection with bounded clipboard restore behavior.
- Recent recording cards with search, copy, delete, and detail navigation.

### YouTube

- Search YouTube from inside Scriber.
- Paste a direct URL when you already have the video.
- Download and prepare audio through bundled media tools.
- Progress, retry, cancel, and durable job state for longer jobs.
- Transcript and summary saved to the same history as live recordings.
- Provider diarization support for batch jobs where the adapter supports stable
  speaker output.

### File Upload

- Drag and drop audio or video files.
- Select multiple files for batch transcription.
- Automatic audio extraction from video.
- Compression/preparation path for large inputs.
- Recent file transcripts with grid/list views, search, copy, delete, status,
  cancellation, and transcript detail navigation.
- PDF and DOCX export from completed transcripts.

### Transcript History

- One local history for microphone, YouTube, and file transcripts.
- Searchable, paginated, virtualized history lists for larger libraries.
- Detail pages with transcript, summary, metadata, copy actions, and export.
- Status tracking for processing, failed, stopped, summary failed, and ready
  items.
- Speaker-label rendering for diarized batch transcripts.

### Summaries

- Automatic summarization for new transcripts.
- Manual summary actions from transcript detail.
- Configurable summary prompt.
- Separate summary model selection from live post-processing model selection.
- Gemini, OpenRouter, OpenAI, and Cerebras summary/post-processing paths.
- OpenRouter Nitro options for throughput-oriented routes.

### Provider Choice

Scriber exposes provider modes by how they behave in real work:

- **Cloud streaming:** low-latency streams for live speech.
- **Cloud async/batch:** completed audio upload or finalization after capture.
- **Local:** ONNX/NeMo paths for users who want local inference and no STT API
  key.

Current provider coverage includes Soniox, AssemblyAI, Microsoft Azure MAI,
OpenAI, OpenRouter, Deepgram, Mistral, Gladia, Groq, Speechmatics, Smallest AI,
ElevenLabs/fal.ai, Gemini, Google Cloud, ONNX, and NeMo.

Settings keep cloud model choices locked until the matching credential is
available, and missing-key prompts open the correct API-key dialog directly.
Gemini STT uses the same stored Gemini API key as Gemini summaries, so many
Google users can configure one key and be done. Google Cloud STT remains a
separate path for users with Google Cloud Speech credentials.

### Local Models

- Local ONNX runtime is bundled through `onnx-asr[cpu,hub]`.
- ONNX models can be downloaded from Hugging Face from inside Settings.
- NVIDIA Parakeet/Canary style local models are available through the ONNX path.
- Primeline German Parakeet is available as a prepared ONNX snapshot:
  `Buttermilk03/parakeet-primeline-onnx`.
- Primeline supports a compact CPU-valid `int8` export and full `fp32`; users do
  not need to export the original `.nemo` file locally.
- The NeMo Settings surface falls back to the ONNX local model path when the
  full NeMo/Torch runtime is not bundled.

### Settings And Personalization

- Provider API-key dialogs with saved/not-set status.
- Direct key links for supported providers.
- Credential-gated model selection so unavailable cloud models cannot be chosen
  accidentally.
- Custom vocabulary for names, brands, and domain-specific terms.
- Separate prompts for summaries and live post-processing.
- Separate model choices for STT, summaries, and post-processing.
- STT model rows with practical cost/error estimates where benchmark data is
  available.
- Post-processing model rows with practical cost/speed estimates where model
  data is available.
- Interface language and automatic language behavior.
- Recording mode, hotkey, post-processing hotkey, visualizer size, microphone,
  favorite microphone, and mic pre-warming controls.
- Light/dark/system theme support through the app shell.

### Desktop App

- Windows-first Tauri 2 desktop shell.
- Tray panel with start recording, YouTube, file transcription, recent
  transcripts that can be copied directly, settings, update checks, restart,
  and quit.
- Tray icon changes for active recording and available updates.
- One-click install-and-restart when a signed update is available.
- Autostart with Windows.
- Single-instance behavior.
- Backend supervision and recovery.
- Fast route/data preloading for primary tabs.

### Diagnostics And Support

- Debug Console with runtime logs and filters.
- Log clearing from the app.
- Redacted support bundle generation.
- Post-processing diagnostics without raw transcript leakage.
- Runtime health and backend availability state.
- Provider-specific error toasts.

## Install

Scriber is Windows-first.

1. Download the latest Windows installer from
   [GitHub Releases](https://github.com/MyButtermilk/Scriber/releases).
2. Run the installer.
3. Open Scriber from the Start Menu or tray.
4. Open Settings and add the API key for the provider you want to use.

For the simplest first setup, add a Gemini API key and use Gemini for both STT
and summaries. Local ONNX models remain selectable without an API key.

The installed app includes the Tauri shell, React frontend, Python backend
sidecar, Rust audio sidecar, and bundled media tools. No optional installer
components are required for the normal feature set.

## First Run Checklist

1. Select a transcription provider in Settings.
2. Add the matching API key, or choose a local ONNX model.
3. Select your microphone and decide whether mic pre-warming should stay on.
4. Choose normal dictation or configure the optional post-processing hotkey.
5. Try a short live recording before using Scriber in a meeting or workflow.

## Privacy And Data

Scriber runs its UI and backend locally on loopback. Transcript history,
settings, logs, downloads, and runtime data stay in the user data directory.

Cloud providers receive audio or text only when you select a cloud provider for
transcription, summarization, or post-processing. Local ONNX models do not need
an STT API key. Support bundles redact known secret patterns such as API keys,
bearer tokens, session tokens, and similar credentials.

## Architecture

Scriber has three main parts:

- **Tauri 2 shell:** tray, global hotkeys, autostart, single instance, update
  checks, backend supervision, native overlay, and desktop integration.
- **React frontend:** Live Mic, YouTube, File, Settings, Console, and Transcript
  views.
- **Python backend sidecar:** recording state, provider routing, media
  preparation, transcript storage, logs, support bundles, and REST/WebSocket
  APIs.

Live microphone capture uses a Rust/WASAPI audio sidecar. Python receives the
captured frames and routes transcription work to the selected STT provider or
local model path.

## For Developers

### Requirements

- Windows 10 or newer for the primary desktop runtime.
- Python 3.13.
- Node.js 26.3.1 Current and npm.
- Rust toolchain.
- Git.

### Start In Dev Mode

```powershell
git clone https://github.com/MyButtermilk/Scriber.git
cd Scriber
cd Frontend
npm install
npm run tauri:dev
```

Manual backend:

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
python -m src.web_api
```

Manual frontend:

```powershell
cd Frontend
npm install
npm run dev:client
```

Tauri dev shell:

```powershell
cd Frontend
npm run tauri:dev
```

## Build A Windows Installer

Fast local installer build:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke
```

`-FastLocalInstaller` enables Profile B media tools, sidecar cache reuse, and
local LZMA NSIS compression by default, matching the GitHub release installer
size profile.

The NSIS installer is written to:

```text
Frontend\src-tauri\target\release\bundle\nsis\
```

Recent local release evidence:

- Fast local and release LZMA installer size: about 72-88 MiB, depending on
  dependency wheel versions and signing metadata.
- Installed app size in smoke: about 195 MiB.
- Backend resource tree: about 180 MiB.
- Bundled Profile B ffmpeg/ffprobe media tools: about 4.98 MiB installed.
- AWS Transcribe support and AWS SDK packages are not part of the standard app.
- The recording overlay is rendered by Tauri; PySide6/Tk overlay runtimes are
  not part of the standard backend sidecar.
- Supported provider SDKs are bundled explicitly; unused provider SDKs are kept
  out of the standard backend sidecar.
- ONNX local ASR support is bundled through `onnx-asr[cpu,hub]`; full
  NeMo/Torch remains outside the standard sidecar.

## Test

Run from the repository root unless stated otherwise.

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
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath Frontend\src-tauri\target\release\bundle\nsis\Scriber_<version>_x64-setup.exe `
  -VerifyFrontend `
  -VerifyMediaPreparation `
  -VerifySupportBundle `
  -VerifyUninstall
```

## Troubleshooting

### Backend Not Available

Open the installed desktop app rather than a raw browser tab. The desktop shell
passes a private session token to the frontend. If the backend still does not
come up, open the Console tab or create a support bundle.

### Missing API Keys

Open Settings and check the API keys section. Scriber warns when the selected
provider does not have credentials yet and opens the relevant key dialog.

### YouTube Or File Transcription Fails

The Windows installer bundles ffmpeg and ffprobe. In development mode, make sure
the bundled media tools were built or set `SCRIBER_MEDIA_TOOLS_DIR`,
`SCRIBER_FFMPEG_PATH`, or `SCRIBER_FFPROBE_PATH`.

### Microphone Changes

Scriber listens for native Windows device events and uses sparse polling as a
fallback. If you dock, undock, or attach a USB microphone, the device list
should refresh without constant aggressive polling.

### Slow Stop-To-Text

For cloud STT providers, the final delay after stopping is often provider
finalization and network roundtrip. Use the Console and hot-path metrics before
assuming the local app is the bottleneck.

## Documentation

The active documentation set is intentionally small:

- `AGENTS.md`: editing guide for future agents.
- `docs/ARCHITECTURE.md`: current runtime architecture and ownership
  boundaries.
- `docs/PERFORMANCE_AND_PACKAGING.md`: implemented performance work, packaging
  decisions, installer size, and remaining optimization ideas.
- `docs/TESTING_AND_RELEASE.md`: test commands, smoke gates, installer builds,
  signing, and updater status.
- `docs/ROADMAP_AND_KNOWN_ISSUES.md`: current open issues and prioritized next
  work.

When code and prose disagree, trust the code and update the docs in the same
change.
