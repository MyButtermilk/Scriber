# Repository Guidelines

## Project Overview
- Scriber is an AI-powered voice transcription app with both a web UI (React) and a desktop UI (Tkinter).
- Backend is Python (aiohttp + Pipecat pipeline). Frontend is Vite/React/Tailwind + shadcn/ui.
- Primary modes: live mic dictation, YouTube transcription, and file transcription with optional LLM summaries.

## Project Structure
- `src/`: Python backend and desktop UI
  - `tray.py`: system tray entrypoint + lifecycle
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
  - `database.py`: SQLite persistence
  - `export.py`: PDF/DOCX export
  - `onnx_local_service.py`, `nemo_local_service.py`: local STT providers
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
### Backend (from repo root)
- Create venv: `python -m venv venv`
- Activate venv: `venv\\Scripts\\activate` (Windows) or `source venv/bin/activate`
- Install deps: `pip install -r requirements.txt`
- Desktop UI: `python -m src.main`
- Tray entrypoint (debug): `python -m src.tray`
- Web API backend: `python -m src.web_api`
- Dependency check: `python check_imports.py`

### Frontend (from `Frontend/`)
- Install: `npm install`
- Dev client only: `npm run dev:client` (port 5000)
- Dev server: `npm run dev`
- Type check: `npm run check`
- Build: `npm run build`
- Prod: `npm start`
- Drizzle schema push: `npm run db:push`

### Tests (from repo root)
- Run all tests: `pytest`
- Run one file: `pytest tests/test_web_api_security.py`
- Run one test (preferred): `pytest tests/test_web_api_security.py::test_origin_allowed_defaults`
- Run by keyword: `pytest -k origin_allowed`

### Lint/Format
- No Python formatter or linter configured; keep formatting consistent with nearby code.
- Use `python check_imports.py` for backend dependency sanity.
- Use `npm run check` for TypeScript strict type checking.

## Configuration
- `.env` stores API keys and settings. Never commit this file.
- `settings.json` stores multi-line settings (summarization prompt). Also gitignored.
- Default hotkey: `ctrl+alt+s`
- Default mode: `toggle`

Key env vars:
- STT keys: `SONIOX_API_KEY`, `ASSEMBLYAI_API_KEY`, `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`,
  `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`, `GLADIA_API_KEY`, `GROQ_API_KEY`,
  `SPEECHMATICS_API_KEY`, `ELEVENLABS_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`
- Summarization: `GOOGLE_API_KEY`, `SCRIBER_SUMMARIZATION_MODEL`, `SCRIBER_AUTO_SUMMARIZE`
- App behavior: `SCRIBER_DEFAULT_STT`, `SCRIBER_HOTKEY`, `SCRIBER_MODE`,
  `SCRIBER_INJECT_METHOD`, `SCRIBER_LANGUAGE`, `SCRIBER_MIC_DEVICE`,
  `SCRIBER_FAVORITE_MIC`, `SCRIBER_MIC_ALWAYS_ON`, `SCRIBER_DEBUG`
- OpenAI STT model override: `SCRIBER_OPENAI_STT_MODEL`
- Local STT: `SCRIBER_ONNX_MODEL`, `SCRIBER_ONNX_QUANTIZATION`, `SCRIBER_ONNX_USE_GPU`,
  `SCRIBER_NEMO_MODEL`
- Web API security: `SCRIBER_ALLOWED_ORIGINS`, `SCRIBER_UPLOAD_MAX_MB`, `SCRIBER_UPLOAD_MAX_BYTES`

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
- `tests/conftest.py` sets safe defaults for injection behavior; avoid changing it unless required.
- For new STT providers, add tests that exercise `_create_stt_service` branches.

## Coding Style (Python)
- Python 3.10+, PEP 8, 4-space indents, prefer type hints.
- Imports order: standard library, third-party, local (`src.*`), with blank lines between groups.
- Use built-in generics (`list[str]`, `dict[str, Any]`) over `typing.List`.
- Naming: `snake_case` for functions/vars, `PascalCase` for classes, `UPPER_SNAKE` for constants.
- Booleans should read well (`is_`, `has_`, `should_`).
- Prefer `pathlib.Path` for filesystem paths and avoid hard-coded separators.
- Async code: avoid blocking the event loop; use `asyncio` APIs and `await` I/O.
- Logging: use `loguru` (`logger.info/warning/error/exception`); avoid `print`.
- Error handling: validate inputs early; raise `ValueError` for config/user errors; log and surface user-friendly messages at API boundaries.

## Coding Style (TypeScript/React)
- TypeScript `strict` is enabled; avoid `any` when possible and narrow API data.
- Components are functional, PascalCase; hooks are `useX`.
- Imports: external packages first, then alias imports (`@/`, `@shared/`), then relative paths.
- Prefer `@/` path alias for client imports.
- Keep formatting consistent with the surrounding file (indentation/semicolons vary across vendor code).
- UI uses Tailwind v4 + shadcn/ui; prefer utility classes and existing neumorphic classes (`neu-panel-raised`, `neu-button`).
- Micro-interactions via CSS transitions; avoid JS animations unless needed.
- Use `toast` for user-visible errors; log to console sparingly.

## Repository Hygiene
- Never commit secrets or local artifacts: `.env`, `settings.json`, `transcripts.db`, `downloads/`, `tmp/`.
- Do not delete or overwrite unrelated changes in the working tree.
- Keep changes scoped to the feature or fix you are implementing.

## Commit and PR Guidelines
- Commit messages: short, imperative (optionally `feat:`, `fix:`, `refactor:`).
- Include tests run in PR description.
- For UI changes, include screenshots when possible.

## Frontend UI/UX Notes
- Uses a neumorphic design system with custom classes (`neu-panel-raised`, `neu-button`, etc.).
- Tailwind CSS v4 with `@tailwindcss/vite` plugin (CSS-first config in `Frontend/client/src/index.css`).
- shadcn/ui components wrap Radix UI primitives.
- Micro-interactions handled via CSS transitions, not JavaScript.

## Cursor/Copilot Rules
- No `.cursor/rules`, `.cursorrules`, or `.github/copilot-instructions.md` found in this repo.

## Documentation
- `README.md`: user-facing documentation
- `AGENTS.md`: this file
- `frontend.md`: frontend architecture details
- `docs/Mic-Performance-Enhancement.md`: microphone latency improvements
- `docs/Performance-Optimization-Proposals.md`: performance improvement roadmap
