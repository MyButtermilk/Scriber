# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Scriber is an AI-powered voice transcription application with a modern React web interface. It consists of:
- **Python backend** (`src/`): aiohttp-based HTTP/WebSocket API server with multi-engine STT pipeline using Pipecat
- **React frontend** (`Frontend/`): Vite 7 + React 19 + Tailwind CSS v4 + shadcn/ui web interface

The application supports live microphone recording with global hotkeys, YouTube video transcription via yt-dlp, file upload transcription, and LLM-powered summarization using OpenAI GPT or Google Gemini.

## Development Commands

### Python Backend

```bash
# Setup environment (Windows)
python -m venv venv
venv\Scripts\activate

# Setup environment (Unix)
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run backend server (starts on port 5000)
python -m src.web_api

# Run desktop app (Tkinter UI with global hotkey)
python -m src.main

# Run tests
pytest

# Quick dependency check
python check_imports.py
```

### React Frontend

```bash
# Navigate to frontend
cd Frontend

# Install dependencies
npm install

# Development mode (client only, port 5000)
npm run dev:client

# Development mode (Express server + Vite middleware)
npm run dev

# Type check
npm run check

# Production build
npm run build

# Start production server
npm start

# Database schema push (requires DATABASE_URL)
npm run db:push
```

### Typical Development Workflow

For web development, run both servers:
1. Terminal 1: `python -m src.web_api` (backend on port 5000)
2. Terminal 2: `cd Frontend && npm run dev:client` (frontend also on port 5000)

The frontend dev server proxies API requests to the backend.

## Architecture Overview

### Backend Architecture (`src/`)

**Core Pipeline Flow:**
The STT pipeline (`pipeline.py`) is built on Pipecat and orchestrates the transcription flow:
1. **Audio Input** → `SoundDeviceAudioInputStream` (microphone) or file input (`audio_file_input.py`)
2. **Audio Frames** → `InputAudioRawFrame` objects streamed through the pipeline
3. **STT Service** → Pluggable transcription engines (Soniox, AssemblyAI, Deepgram, OpenAI, Azure, Gladia, Groq, Speechmatics, Google Cloud, ElevenLabs, AWS Transcribe)
4. **Transcription Frames** → `InterimTranscriptionFrame` (real-time partial results) and `TranscriptionFrame` (final results)
5. **Smart Turn Detection** → Optional `LocalSmartTurnAnalyzerV3` for detecting speech boundaries

**Key Modules:**

- `web_api.py`: Main aiohttp server with HTTP/REST endpoints and WebSocket for real-time updates. Manages transcript records, handles YouTube search/download, file uploads, and coordinates transcription/summarization
- `pipeline.py`: Pipeline orchestration using Pipecat. Contains `ScriberPipeline` class that creates and manages the audio → STT → text pipeline. Supports both streaming (real-time) and async (batch) STT modes
- `config.py`: Configuration management via `.env` and `settings.json`. Loads API keys, app settings (hotkey, STT service, language, etc.)
- `summarization.py`: LLM summarization using OpenAI or Gemini APIs. Supports configurable prompts via `settings.json`
- `youtube_api.py`: YouTube Data API v3 integration for video search and metadata
- `youtube_download.py`: Audio extraction using yt-dlp
- `injector.py`: Text injection into active applications via keyboard simulation (pyautogui) or clipboard paste
- `microphone.py`: Audio input device management using sounddevice
- `overlay.py`: Tkinter-based recording overlay UI with audio visualization
- `main.py`: Desktop app entry point (Tkinter UI with global hotkey support via keyboard library)
- `ui.py`: Full Tkinter settings UI for desktop app

**STT Service Architecture:**
The `_create_stt_service()` function in `pipeline.py` is the factory for STT providers. Each provider has specific initialization:
- Streaming services (Soniox, AssemblyAI, Deepgram, Azure, etc.): Use WebSocket connections for real-time transcription
- Batch services (OpenAI, Groq, ElevenLabs): Process complete audio files
- Special case: `soniox_async` uses a custom `SonioxAsyncProcessor` that buffers audio and submits on `EndFrame`

When adding a new STT provider:
1. Add API key mapping to `Config.SERVICE_API_KEY_MAP` in `config.py`
2. Add service label to `Config.SERVICE_LABELS`
3. Add provider case to `_create_stt_service()` in `pipeline.py`
4. Pass language hints if the provider supports them

### Frontend Architecture (`Frontend/`)

**Directory Structure:**
- `client/`: React 19 app with Vite 7 build system
  - `src/pages/`: Main UI pages (LiveMic, Youtube, FileTranscribe, TranscriptDetail, Settings)
  - `src/components/`: Reusable React components + shadcn/ui components
  - `src/lib/`: Utilities, query client, API helpers, mock data
  - `src/hooks/`: Custom React hooks
- `server/`: Express server that serves Vite dev middleware or static build
- `shared/`: Shared TypeScript types and Drizzle schema
- `script/build.ts`: Production build pipeline
- `components/`: shadcn/ui component source (managed by CLI)

**Routing & Navigation:**
Uses `wouter` for client-side routing with bottom tab bar navigation:
- `/` → LiveMic (microphone recording)
- `/youtube` → YouTube search and transcription
- `/file` → File upload transcription
- `/settings` → API keys and preferences
- `/transcript/:id` → Transcript detail view

All routes are wrapped in `AppLayout.tsx` which provides the tab bar and page transitions via framer-motion.

**Data Flow:**
- TanStack Query (`lib/queryClient.ts`) for data fetching and caching
- WebSocket connection (`ws`) for real-time transcript updates from backend
- API requests via `apiRequest()` helper with `credentials: "include"`

**Backend Integration:**
The frontend expects the Python backend to be running on `localhost:5000` and connects to:
- `/api/settings` (GET/PUT) - Settings management
- `/api/microphones` (GET) - Available microphones list
- `/api/transcripts` (GET) - Transcript history
- `/api/transcripts/:id` (GET/DELETE) - Individual transcript operations
- `/api/transcripts/:id/summarize` (POST) - Generate summary
- `/api/youtube/search` (POST) - YouTube video search
- `/api/youtube/transcribe` (POST) - Start YouTube transcription
- `/api/transcribe/file` (POST) - Upload file for transcription
- `/ws` (WebSocket) - Real-time updates

## Configuration Files

### `.env` (Backend Configuration)
Required for backend operation. Contains API keys and application settings:

**STT Service Keys** (at least one required):
- `SONIOX_API_KEY`, `ASSEMBLYAI_API_KEY`, `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`
- `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`, `GLADIA_API_KEY`, `GROQ_API_KEY`
- `SPEECHMATICS_API_KEY`, `ELEVENLABS_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`

**Summarization Keys** (for LLM summarization):
- `GOOGLE_API_KEY` (for Gemini)
- `OPENAI_API_KEY` (for GPT, also used for Whisper STT)

**YouTube Integration**:
- `YOUTUBE_API_KEY` (for YouTube Data API v3)

**App Settings**:
- `SCRIBER_DEFAULT_STT`: Default STT service (e.g., "soniox", "assemblyai", "deepgram")
- `SCRIBER_HOTKEY`: Global hotkey (default: "ctrl+alt+s")
- `SCRIBER_MODE`: "toggle" or "push_to_talk"
- `SCRIBER_INJECT_METHOD`: "auto", "type", or "paste"
- `SCRIBER_LANGUAGE`: Language code (e.g., "auto", "en", "de", "fr")
- `SCRIBER_AUTO_SUMMARIZE`: "1" to enable auto-summarization, "0" to disable
- `SCRIBER_SUMMARIZATION_MODEL`: Model for summarization (e.g., "gemini-flash-latest", "gpt-5.2")
- `SCRIBER_MIC_DEVICE`: Microphone device name or "default"
- `SCRIBER_MIC_ALWAYS_ON`: "1" to keep mic always on, "0" for hotkey control
- `SCRIBER_DEBUG`: "1" to enable debug logging

### `settings.json` (Complex Settings)
Used for multi-line settings that don't work well in `.env`:

```json
{
  "summarizationPrompt": "Your custom multi-line summarization prompt...",
  "otherComplexSettings": "..."
}
```

The backend loads this via `config.py` using `_load_json_settings()` and saves changes via `_save_json_settings()`.

### `Frontend/.env` (Frontend Environment)
Optional environment variables for frontend:
- `PORT`: Server port (default: 5000)
- `NODE_ENV`: "development" or "production"
- `DATABASE_URL`: PostgreSQL connection for Drizzle (if using DB)
- Replit-specific: `REPL_ID`, `REPLIT_INTERNAL_APP_DOMAIN`, `REPLIT_DEV_DOMAIN`

## Testing

### Python Tests (`tests/`)
Framework: pytest with pytest-asyncio and pytest-mock

Test files:
- `test_config.py`: Configuration loading
- `test_injector.py`, `test_injector_paste.py`: Text injection logic
- `test_pipeline_stop.py`: Pipeline shutdown behavior
- `conftest.py`: Fixtures and path setup

Run tests: `pytest`

When adding tests:
- Name files `test_*.py` and functions `test_*`
- Use `unittest.mock.patch` for GUI/keyboard to avoid side effects
- Mock external APIs (STT services, YouTube API, etc.)

### Frontend Testing
Currently no test suite configured. Consider adding Vitest for unit tests and Playwright for E2E tests.

## Code Style

### Python
- Python 3.10+, PEP 8 with 4-space indents
- Type hints preferred (see `main.py`, `pipeline.py`)
- Use loguru for logging (never print statements)
- Snake_case for functions/variables, CapWords for classes
- Never hard-code secrets; always use Config/environment

### TypeScript/React
- TypeScript strict mode
- Functional components with hooks
- Component files in PascalCase (e.g., `LiveMic.tsx`)
- Use shadcn/ui components from `components/ui/`
- TanStack Query for async state management
- Tailwind CSS v4 for styling (CSS-first config in `client/src/index.css`)

## Important Implementation Details

### Async vs Streaming STT
- **Streaming**: Real-time transcription via WebSocket (Soniox, AssemblyAI, Deepgram, Azure, Gladia, Speechmatics, Google Cloud)
- **Async/Batch**: Process complete audio file (OpenAI Whisper, Groq, ElevenLabs)
- **Special**: `soniox_async` mode buffers audio and submits on EndFrame for ultra-low latency with custom vocabulary

### Text Injection Modes
The injector supports three modes (`INJECT_METHOD` in `.env`):
- `auto`: Automatically choose between type/paste based on active window
- `type`: Simulate keystrokes (slower but works everywhere)
- `paste`: Use clipboard + Ctrl+V (faster but some apps delay paste processing)

Clipboard paste tuning for Windows:
- `PASTE_PRE_DELAY_MS`: Delay before paste (default: 80ms)
- `PASTE_RESTORE_DELAY_MS`: Delay before restoring clipboard (default: 1500ms)

### Frontend State Management
The frontend uses multiple state management approaches:
- TanStack Query for server state (transcripts, settings)
- React hooks (useState, useEffect) for local UI state
- WebSocket for real-time updates (new transcripts, processing status)

Mock data in `client/src/lib/mockData.ts` is used during development when backend is unavailable.

### Transcript Processing Flow
1. **Audio Input**: Mic/YouTube/File → Backend receives audio
2. **Transcription**: Pipeline processes audio → STT service → text
3. **Storage**: Transcript saved to in-memory store (TranscriptRecord)
4. **Summarization** (optional): LLM processes transcript → summary
5. **WebSocket Broadcast**: Updates pushed to connected clients
6. **Frontend Update**: UI refreshes via TanStack Query invalidation

## Windows-Specific Considerations

- `start.bat`: Bootstrap script that creates venv, installs deps, prompts for API keys
- Global hotkeys use the `keyboard` library which requires admin privileges on some systems
- Audio device enumeration via `sounddevice` may require ASIO/WASAPI drivers for low latency
- File paths use backslashes; use `Path` from `pathlib` for cross-platform compatibility

## Dependencies of Note

### Python
- `pipecat-ai[...]`: Core streaming audio pipeline framework with STT provider integrations
- `aiohttp`: Async HTTP server for web API
- `sounddevice`: Audio I/O
- `keyboard`: Global hotkey capture (Windows-focused)
- `pyautogui`: Cross-platform keyboard/mouse automation
- `yt-dlp`: YouTube audio download
- `google-generativeai`: Gemini API client
- `loguru`: Structured logging

### Frontend
- React 19 with new features (use async components where beneficial)
- Vite 7 with improved performance
- Tailwind CSS v4 with CSS-first configuration (no tailwind.config.js)
- `@tailwindcss/vite` plugin for v4 support
- shadcn/ui components built on Radix UI primitives
- framer-motion for page transitions
- TanStack Query v5 for data fetching

## Common Development Tasks

### Adding a New STT Provider

1. Install provider SDK: Add to `requirements.txt` and run `pip install -r requirements.txt`
2. Update `config.py`:
   - Add API key env var (e.g., `PROVIDER_API_KEY = os.getenv("PROVIDER_API_KEY")`)
   - Add to `SERVICE_API_KEY_MAP`: `"provider": "PROVIDER_API_KEY"`
   - Add to `SERVICE_LABELS`: `"provider": "Provider Name"`
3. Update `pipeline.py`:
   - Import provider's STT service class
   - Add case to `_create_stt_service()` method
   - Configure language hints if supported
4. Update `Frontend/client/src/pages/Settings.tsx`:
   - Add provider option to STT service dropdown
   - Add API key input field
5. Test with `python -m src.web_api` and verify in UI

### Adding a New UI Feature

1. Create component in `Frontend/client/src/components/` or page in `Frontend/client/src/pages/`
2. Add route to `App.tsx` if needed
3. Wire up backend API endpoint in `src/web_api.py` if needed
4. Use TanStack Query for data fetching
5. Follow existing patterns (see `LiveMic.tsx` or `TranscriptDetail.tsx`)

### Modifying Summarization

1. Edit prompt: Update `settings.json` → `summarizationPrompt` field
2. Change model: Set `SCRIBER_SUMMARIZATION_MODEL` in `.env`
3. Add new model: Update `SummarizationModel` type in `summarization.py`
4. Modify logic: Edit `summarize_text()` in `src/summarization.py`

## Known Issues & Gotchas

- **Frontend `nanoid` missing**: `server/vite.ts` imports `nanoid` but it's not in `package.json` (may work transitively; add explicitly if issues arise)
- **Tailwind v4 config**: `Frontend/components.json` references `tailwind.config.ts` but project uses CSS-first config in `index.css`
- **Two separate repos**: `Frontend/.git/` contains nested git metadata; this is intentional for separate version control
- **Mock data**: Frontend uses mock data (`lib/mockData.ts`) when backend is unavailable; switch to real API by ensuring backend is running
- **Admin privileges**: Global hotkeys on Windows may require running as administrator depending on security settings
- **FFmpeg required**: Audio processing requires FFmpeg to be installed and in PATH
- **yt-dlp updates**: YouTube frequently changes; keep yt-dlp updated with `pip install -U yt-dlp`

## Security Notes

- Never commit `.env` files (already in `.gitignore`)
- API keys are stored in `.env` and loaded via `Config` class
- Frontend sends credentials with API requests (`credentials: "include"`)
- Settings API allows updating `.env` values dynamically (be cautious in production)

## Additional Documentation

- `README.md`: User-facing setup and feature documentation
- `AGENTS.md`: Original agent guidelines (deprecated, use this file instead)
- `frontend.md`: Detailed frontend architecture notes
