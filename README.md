# Scriber

Scriber is an AI-powered voice transcription app for Windows (and other platforms) featuring a modern web UI, background system tray operation, and local persistence. It supports live dictation, YouTube video transcription, and audio file uploads, with optional LLM-powered summarization.

## Features

- **Live Dictation**: Global hotkey (`Ctrl+Alt+S` default) to toggle recording with a responsive audio visualizer overlay.
- **System Tray Integration**: Runs silently in the background. Right-click the tray icon to:
  - View application logs.
  - Access recent recordings (copy to clipboard instantly).
  - Open the Web UI.
  - Restart or Quit the application.
- **YouTube Transcription**: Paste a YouTube URL or search directly to transcribe videos.
- **File Transcription**: Upload audio/video files (mp3, wav, mp4, etc.) for processing.
- **Persistence**: All transcripts are saved to a local SQLite database (`transcripts.db`), so your history is preserved across restarts.
- **Summarization**: Generates summaries of transcripts using Google Gemini or OpenAI models.
- **Modern UI**: Built with React 19, Vite, and Tailwind CSS.
- **Multiple STT Providers**: Support for Soniox, Deepgram, AssemblyAI, OpenAI Whisper, Azure, Gladia, and more.

## Quick Start (Windows)

1. **Install Python 3.10+**: Ensure it's in your PATH.
2. **Run `start.bat`**:
   - This will automatically install dependencies (backend & frontend).
   - It will launch the application in the background.
   - A **Scriber icon** will appear in your system tray (notification area).
   - The Web UI will open automatically in your browser at `http://localhost:5000`.

**Note:** The command window will close automatically after starting. The app continues running in the background. Use the tray icon to control it.

## Usage

- **Toggle Recording**: Press `Ctrl+Alt+S` (or your configured hotkey) to start/stop live dictation anywhere.
- **Web Interface**:
  - **Live Mic Tab**: View real-time transcription and visualized audio levels.
  - **YouTube Tab**: Search/Paste URLs to transcribe.
  - **Files Tab**: Upload media files.
  - **Settings**: Configure API keys, hotkeys, and preferences.
- **Tray Menu**:
  - **Recent Recordings**: Hover to see the last 5 recordings. Click one to copy its text to your clipboard.
  - **View Logs**: distinct logs for Backend and Frontend services.

## Configuration

Scriber uses a `.env` file for configuration. This file is created automatically on first run if it doesn't exist.

**Key Settings:**

```env
# STT API Keys
SONIOX_API_KEY=...
OPENAI_API_KEY=...
DEEPGRAM_API_KEY=...
# ...and others

# App Settings
SCRIBER_HOTKEY=ctrl+alt+s
SCRIBER_DEFAULT_STT=soniox
SCRIBER_MIC_DEVICE=default
SCRIBER_VISUALIZER_BAR_COUNT=60
SCRIBER_AUTO_SUMMARIZE=0
SCRIBER_SUMMARIZATION_MODEL=gemini-2.0-flash
```

## Architecture

The application is composed of three main parts managed by a central tray application:

1.  **System Tray (`src/tray.py`)**: The entry point. It manages the lifecycle of the backend and frontend processes, handles global hotkeys, and provides the tray menu interface.
2.  **Backend (`src/web_api.py`)**: A Python `aiohttp` server that handles:
    -   Speech-to-Text pipeline orchestration.
    -   WebSocket broadcasting for real-time frontend updates.
    -   Database operations (SQLite).
    -   YouTube downloads and file processing.
3.  **Frontend (`Frontend/`)**: A React/Vite SPA that connects to the backend via WebSocket and REST API.

## Project Structure

```
Scriber/
  src/
    tray.py             # Main entry point & process manager (System Tray)
    web_api.py          # Backend API & WebSocket server
    database.py         # SQLite database interface
    pipeline.py         # STT provider orchestration
    overlay.py          # Visualizer overlay logic
    config.py           # Configuration loader
    ...
  Frontend/             # React application source
  transcripts.db        # Local database file (auto-generated)
  start.bat             # Windows launcher
```

## Development

To run manually without `start.bat`:

1.  Activate virtual environment: `venv\Scripts\activate`
2.  Run the tray app: `python -m src.tray`

This will automatically launch the backend and the frontend (`npm run dev:client`) in background threads/processes.

## Troubleshooting

-   **App doesn't start?** Check `start.bat` output or run `python -m src.tray` manually in a terminal to see immediate errors.
-   **Logs**: Right-click the tray icon and select "View Logs" to debug issues with API keys or devices.
-   **Copying fails?** The tray app uses direct Windows API calls for clipboard reliability. If it fails, check the logs.

## License

MIT
