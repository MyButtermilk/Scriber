# Repository Guidelines

## Project Overview
- Scriber is an AI-powered voice transcription app with both a web UI (React) and a desktop UI (Tkinter).
- Backend is Python (aiohttp + Pipecat pipeline). Frontend is Vite/React/Tailwind + shadcn/ui.
- Primary modes: live mic dictation, YouTube transcription, and file transcription with optional LLM summaries.

## Project Structure
- `src/`: Python backend and desktop UI
  - `main.py`: Tkinter entrypoint
  - `web_api.py`: HTTP + WebSocket API (aiohttp)
  - `pipeline.py`: STT pipeline orchestration (Pipecat)
  - `microphone.py`: sounddevice input transport
  - `audio_file_input.py`: ffmpeg file input transport
  - `injector.py`: text injection logic (type/paste/auto)
  - `summarization.py`: OpenAI/Gemini summarization
  - `youtube_api.py`: YouTube Data API integration
  - `youtube_download.py`: yt-dlp audio extraction
  - `overlay.py`: recording overlay (PySide6 with Tk fallback)
  - `config.py`: env + settings.json configuration
- `Frontend/`: React web UI (Vite 7, React 19, Tailwind v4, shadcn/ui)
  - `client/`: main React app
  - `server/`: Express server for dev/prod
  - `shared/`: shared types and Drizzle schema
  - `components/`: shadcn/ui components
- `tests/`: pytest tests
  - `test_config.py`, `test_injector.py`, `test_injector_paste.py`, `test_pipeline_stop.py`, `test_web_api_security.py`
- Root scripts: `start.bat` (Windows), `start.sh` (Linux/macOS), `check_imports.py` (dependency sanity check)

## Build, Test, and Development Commands
- Create venv: `python -m venv venv`
- Activate venv:
  - Windows: `venv\Scripts\activate`
  - Unix: `source venv/bin/activate`
- Install deps: `pip install -r requirements.txt`
- Desktop UI: `python -m src.main`
- Web API backend: `python -m src.web_api`
- Tests: `pytest`
- Dependency check: `python check_imports.py`

### Frontend (from `Frontend/`)
- Install: `npm install`
- Dev client only: `npm run dev:client` (port 5000)
- Dev server: `npm run dev`
- Type check: `npm run check`
- Build: `npm run build`
- Prod: `npm start`

## Configuration
- `.env` stores API keys and settings. Never commit this file.
- `settings.json` stores multi-line settings (summarization prompt).

Key env vars:
- STT keys: `SONIOX_API_KEY`, `ASSEMBLYAI_API_KEY`, `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`,
  `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`, `GLADIA_API_KEY`, `GROQ_API_KEY`,
  `SPEECHMATICS_API_KEY`, `ELEVENLABS_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`
- Summarization: `GOOGLE_API_KEY`, `SCRIBER_SUMMARIZATION_MODEL`, `SCRIBER_AUTO_SUMMARIZE`
- App behavior: `SCRIBER_DEFAULT_STT`, `SCRIBER_HOTKEY`, `SCRIBER_MODE`,
  `SCRIBER_INJECT_METHOD`, `SCRIBER_LANGUAGE`, `SCRIBER_MIC_DEVICE`,
  `SCRIBER_MIC_ALWAYS_ON`, `SCRIBER_DEBUG`
- OpenAI STT model override: `SCRIBER_OPENAI_STT_MODEL`
- Web API security: `SCRIBER_ALLOWED_ORIGINS`, `SCRIBER_UPLOAD_MAX_MB`, `SCRIBER_UPLOAD_MAX_BYTES`

Defaults to keep in sync:
- Hotkey: `ctrl+alt+s`
- Mode: `toggle`

## Pipeline Notes
- OpenAI STT is segmented and requires VAD; Silero VAD is used for segmented STT.
- Keep mic input mono when possible for better VAD and WAV encoding.
- Soniox supports async mode with buffered upload in `SonioxAsyncProcessor`.
- Adding a provider requires updates in `Config.SERVICE_API_KEY_MAP`,
  `Config.SERVICE_LABELS`, and `_create_stt_service` in `pipeline.py`.

## Web API Notes
- Backend default host/port: `127.0.0.1:8765`.
- Web UI default port: `5000`.
- CORS is restricted by default to localhost; set `SCRIBER_ALLOWED_ORIGINS` to allow others.
- File uploads have a size cap; adjust via env vars if needed.

## Testing Guidelines
- Use pytest with async support where needed.
- Name files `test_*.py` and functions `test_*`.
- Mock GUI/keyboard to avoid side effects (use `unittest.mock.patch`).
- For new STT providers, add tests that exercise `_create_stt_service` branches.

## Coding Style
- Python 3.10+, PEP 8, 4-space indents, prefer type hints.
- TypeScript: strict, functional React components.
- Logging via `loguru` (avoid print).
- No hard-coded secrets.

## Commit and PR Guidelines
- Commit messages: short, imperative (optionally `feat:`, `fix:`, `refactor:`).
- Include tests run in PR description.
- For UI changes, include screenshots when possible.
