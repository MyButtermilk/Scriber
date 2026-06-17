# Scriber

Scriber is a Windows desktop app that turns speech, videos, and recordings into
usable text.

Press one hotkey to dictate into any app. Paste a YouTube link and get notes
without watching the full video again. Drop in a meeting recording and turn it
into a searchable transcript, summary, and export.

Scriber is for people who collect a lot of spoken information and want one
calm place to capture it, understand it, and reuse it.

[Download for Windows](https://github.com/MyButtermilk/Scriber/releases) |
[What it can do](#what-it-can-do) | [Developer setup](#for-developers)

![Scriber live microphone transcription](docs/screenshots/live_mic.png)

## Why Use Scriber?

| What you need | What Scriber does |
| --- | --- |
| Fast dictation | Press the global hotkey, speak, stop, and Scriber pastes the result where you were working. |
| Long-form video notes | Paste or search a YouTube URL, then get a transcript and summary in one place. |
| File transcription | Drop in audio or video files and let Scriber prepare the media, transcribe it, and save the result. |
| Searchable memory | Keep microphone, YouTube, and file transcripts in one local history with search and detail views. |
| Better handoff | Export transcripts and summaries to PDF or DOCX. |
| Practical troubleshooting | Use the built-in console and support bundle when something needs diagnosing. |

## What It Can Do

- Live microphone dictation with a global Windows hotkey.
- Optional mic pre-warming for faster recording starts.
- Native recording overlay with waveform visualization.
- YouTube search, URL lookup, download, transcription, and summarization.
- Audio and video file transcription with bundled ffmpeg/ffprobe.
- Transcript history with search, filters, detail pages, delete, cancel,
  summarize, and export actions.
- Provider configuration from the Settings UI, including direct "Get key" links
  for supported cloud services.
- Windows autostart, tray integration, single-instance behavior, and backend
  recovery through the Tauri desktop shell.
- Debug console with filters, log clearing, visible-log copy, and redacted
  support bundle download.

Cloud and local provider paths currently cover Soniox, Microsoft Azure MAI,
Azure Speech, OpenAI, Deepgram, AssemblyAI, Mistral, Gladia, Groq,
Speechmatics, Smallest AI, ElevenLabs/fal.ai, Google, ONNX, and NeMo.

## Screenshots

### Speak Once, Use It Anywhere

Scriber is designed around a simple live microphone flow: start recording,
watch the input, stop, then use the transcript immediately.

![Live microphone view](docs/screenshots/live_mic.png)

### Turn YouTube Into Notes

Search YouTube or paste a URL, transcribe the video, and keep the output in the
same history as your live recordings.

![YouTube transcription view](docs/screenshots/youtube.png)

### Import Meetings, Calls, and Recordings

Drop an audio or video file into Scriber. The app handles media preparation and
stores the transcript with your other work.

![File transcription view](docs/screenshots/file_upload.png)

### Read, Summarize, Export

Transcript detail pages keep the source, transcript, summary, and export
actions together.

![Transcript detail view](docs/screenshots/transcript_detail.png)

### Configure Once

Choose your microphone, model, language, hotkey, autostart behavior, and API
keys from one Settings screen.

![Settings view](docs/screenshots/settings.png)

## Install

Scriber is Windows-first.

1. Download the latest Windows installer from
   [GitHub Releases](https://github.com/MyButtermilk/Scriber/releases).
2. Run the installer.
3. Open Scriber from the Start Menu or tray.
4. Go to Settings and add the API key for the provider you want to use.

The installed app includes the desktop shell, Python backend sidecar, Rust audio
sidecar, and bundled media tools. No optional installer components are required
for the current feature set.

## First Run Checklist

1. Open Settings.
2. Pick your transcription model.
3. Use the built-in "Get key" link next to the provider field if you still need
   an API key.
4. Select your preferred microphone.
5. Decide whether mic pre-warming should stay enabled.
6. Try a short live recording before using Scriber in a meeting or workflow.

If no cloud STT credentials are saved yet, Settings shows a clear warning for
the selected provider. Local model paths can be used where configured, but most
users should start with a cloud provider for the simplest setup.

## How Scriber Works

Scriber is a desktop app with three cooperating parts:

- A Tauri 2 Windows shell owns the tray, global hotkey, autostart, single
  instance behavior, backend supervision, and native shell integration.
- A React frontend provides the Live Mic, YouTube, File, Console, Settings, and
  Transcript views.
- A Python backend sidecar handles recording state, providers, media
  preparation, transcript storage, logs, support bundles, and API routes.

Live microphone capture uses a Rust/WASAPI audio sidecar. The Python backend
receives the captured frames and routes transcription work to the selected STT
provider or local model path.

Runtime data is stored in the user data directory, not in the install folder.
That includes settings, transcripts, downloads, logs, and support bundles.

## Privacy And Data

Scriber runs its UI and backend locally on loopback. Your transcript database
and runtime files stay on your machine unless a selected cloud provider needs
audio or text to perform transcription or summarization.

Support bundles redact known secret patterns such as API keys, bearer tokens,
session tokens, and similar credentials.

## For Developers

### Requirements

- Windows 10 or newer for the primary desktop runtime.
- Python 3.13.
- Node.js/npm.
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
  -UseProfileBFfmpeg `
  -ValidateSlimMediaTools `
  -ReuseSidecarIfUnchanged `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke
```

The NSIS installer is written to:

```text
Frontend\src-tauri\target\release\bundle\nsis\
```

Recent local release evidence:

- Installer size: about 88 MiB.
- Installed app size in smoke: about 200 MiB.
- Backend resource tree: about 185 MiB.
- Bundled Profile B ffmpeg/ffprobe media tools: about 4.98 MiB installed.
- AWS Transcribe support and AWS SDK packages are not part of the standard app.
- The recording overlay is rendered by Tauri; PySide6/Tk overlay runtimes are
  not part of the standard backend sidecar.
- Supported provider SDKs are bundled explicitly; unused Google
  Generative-AI/TTS SDKs are kept out of the standard backend sidecar.
- Installed frontend and media-preparation smokes pass in the standard local
  build flow.

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
  -InstallerPath Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe `
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

Open Settings and check the API Configuration section. Scriber warns when the
selected provider does not have credentials yet and links to the relevant
provider key page.

### YouTube Or File Transcription Fails

The Windows installer bundles ffmpeg and ffprobe. In development mode, make
sure the bundled media tools were built or set `SCRIBER_MEDIA_TOOLS_DIR`,
`SCRIBER_FFMPEG_PATH`, or `SCRIBER_FFPROBE_PATH`.

### Microphone Changes

Scriber listens for native Windows device events and uses sparse polling as a
fallback. If you dock, undock, or attach a USB microphone, the device list
should refresh without constant aggressive polling.

### Slow Stop-To-Text

For cloud STT providers, the final delay after stopping is often the provider
finalization and network roundtrip. Use the debug console and hot-path metrics
before assuming the local app is the bottleneck.

## Documentation

The active documentation set is intentionally small:

- `AGENTS.md`: editing guide for future agents.
- `docs/ARCHITECTURE.md`: current runtime architecture and ownership
  boundaries.
- `docs/PERFORMANCE_AND_PACKAGING.md`: implemented performance work,
  packaging decisions, installer size, and remaining optimization ideas.
- `docs/TESTING_AND_RELEASE.md`: test commands, smoke gates, installer builds,
  signing, and updater status.
- `docs/ROADMAP_AND_KNOWN_ISSUES.md`: current open issues and prioritized next
  work.

When code and prose disagree, trust the code and update the docs in the same
change.
