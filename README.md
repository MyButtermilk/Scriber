# Scriber

Scriber is an AI-powered voice transcription app with both a modern web UI and a
simple desktop UI. It supports live dictation, YouTube transcription, and file
transcription, with optional LLM summaries.

## What it does

- Live microphone dictation with a global hotkey and optional overlay.
- Text injection into the active app (type, paste, or auto).
- YouTube transcription via yt-dlp (search or URL).
- Audio/video file transcription (mp3, wav, m4a, mp4, webm, etc.).
- Summaries via OpenAI or Google Gemini.
- Multiple STT providers through a single pipeline.

## Quick start

### Windows (one click)

1. Run `start.bat`.
2. It creates a venv, installs deps, and prompts for API keys on first run.
3. Web UI opens at `http://localhost:5000`.

### Linux / macOS

1. Run `start.sh`.
2. It creates a venv and installs deps.
3. Starts the desktop UI by default.

### Manual setup

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt

# Web UI backend
python -m src.web_api

# In another terminal (web UI frontend)
cd Frontend
npm install
npm run dev:client
```

Backend defaults to `http://127.0.0.1:8765` and the web UI to
`http://localhost:5000` (see `start.bat`).

## Usage

- Hotkey mode: `toggle` or `push_to_talk`.
- Injection: `auto` (Word/Outlook paste), `type`, or `paste`.
- Summaries: auto or manual per transcript.
- Transcripts are stored in memory (not persisted) and broadcast via WebSocket.

## Project layout

```
Scriber/
  src/                  Python backend + desktop UI
    main.py             Tkinter entry point (desktop UI)
    web_api.py          aiohttp HTTP + WebSocket API
    pipeline.py         STT pipeline orchestration (Pipecat)
    microphone.py       audio input (sounddevice)
    audio_file_input.py file input (ffmpeg)
    injector.py         text injection
    summarization.py    OpenAI/Gemini summaries
    youtube_api.py      YouTube Data API
    youtube_download.py yt-dlp integration
    overlay.py          recording overlay (Qt/Tk fallback)
    config.py           env + settings.json loader
  Frontend/             React 19 web UI (Vite 7, Tailwind v4, shadcn/ui)
  tests/                pytest tests
  settings.json         multi-line settings (summarization prompt)
  .env                  local configuration
```

## Configuration

### .env (backend)

```env
# STT providers
SONIOX_API_KEY=
ASSEMBLYAI_API_KEY=
DEEPGRAM_API_KEY=
OPENAI_API_KEY=
AZURE_SPEECH_KEY=
AZURE_SPEECH_REGION=
GLADIA_API_KEY=
GROQ_API_KEY=
SPEECHMATICS_API_KEY=
ELEVENLABS_API_KEY=
GOOGLE_APPLICATION_CREDENTIALS=
GOOGLE_API_KEY=
YOUTUBE_API_KEY=

# App behavior
SCRIBER_DEFAULT_STT=soniox
SCRIBER_HOTKEY=ctrl+alt+s
SCRIBER_MODE=toggle
SCRIBER_INJECT_METHOD=auto
SCRIBER_LANGUAGE=auto
SCRIBER_MIC_DEVICE=default
SCRIBER_MIC_ALWAYS_ON=0
SCRIBER_DEBUG=0

# OpenAI STT model override
SCRIBER_OPENAI_STT_MODEL=gpt-4o-mini-transcribe-2025-12-15

# Summarization
SCRIBER_AUTO_SUMMARIZE=0
SCRIBER_SUMMARIZATION_MODEL=gemini-flash-latest

# Paste tuning (Windows)
SCRIBER_PASTE_PRE_DELAY_MS=80
SCRIBER_PASTE_RESTORE_DELAY_MS=1500

# Web API (optional)
SCRIBER_WEB_HOST=127.0.0.1
SCRIBER_WEB_PORT=8765
SCRIBER_ALLOWED_ORIGINS=
SCRIBER_UPLOAD_MAX_MB=200
```

### settings.json

Used for multi-line values such as the summarization prompt:

```json
{
  "summarizationPrompt": "Your custom prompt here"
}
```

## Web API

- `GET /api/health`
- `GET /api/settings`, `PUT /api/settings`
- `GET /api/microphones`
- `GET /api/transcripts`
- `GET /api/transcripts/:id`
- `DELETE /api/transcripts/:id`
- `POST /api/transcripts/:id/summarize`
- `GET /api/youtube/search`
- `GET /api/youtube/video`
- `POST /api/youtube/transcribe`
- `POST /api/file/transcribe`
- `WS /ws` (realtime updates)

## Development

### Backend

```bash
python -m src.web_api
python -m src.main
python check_imports.py
pytest
```

### Frontend

```bash
cd Frontend
npm install
npm run dev:client
npm run dev
npm run check
npm run build
npm start
npm run db:push
```

## Common tasks

### Add a new STT provider

1. Add SDK to `requirements.txt`.
2. Add env + labels in `src/config.py`.
3. Add provider in `ScriberPipeline._create_stt_service()`.
4. Add UI option and key field in the frontend settings page.

### Change summarization

- Update `settings.json` for the prompt.
- Set `SCRIBER_SUMMARIZATION_MODEL` in `.env`.
- Add models in `src/summarization.py` if needed.

## Troubleshooting

- No transcripts: confirm the API key and selected provider, then try with
  `SCRIBER_DEBUG=1` and check backend logs.
- Hotkey not working: on Windows, the `keyboard` module may require admin.
- FFmpeg not found: install FFmpeg and ensure it is on PATH.
- YouTube download errors: update yt-dlp (`pip install -U yt-dlp`).

## License

MIT
