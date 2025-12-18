# Scriber - AI-Powered Voice Transcription & Summarization

Scriber is a comprehensive AI-driven voice transcription application with a modern web interface. It supports **live microphone dictation**, **YouTube video transcription**, and **audio/video file transcription** with automatic LLM-powered summarization.

## Features

### 🎤 Live Microphone Recording
- Real-time speech-to-text with live audio visualization
- System-wide dictation with global hotkey (`Ctrl+Alt+S`)
- Multiple injection modes: auto, type, or paste

### 📺 YouTube Transcription
- Search YouTube videos directly from the app
- Paste any YouTube URL to transcribe
- Automatic audio download via `yt-dlp`

### 📁 File Transcription
- Upload audio/video files (MP3, WAV, M4A, MP4, WebM, etc.)
- Drag-and-drop or click to select files
- Direct upload to STT API for efficient processing

### ✨ AI Summarization
- Automatic or manual summarization of transcripts
- Supports **OpenAI GPT** and **Google Gemini** models
- Customizable summarization prompt (supports Markdown output)
- Summaries rendered with proper formatting (headers, bullets, bold text)

### 🎯 Multi-Engine STT Support
| Service | Type | Notes |
|---------|------|-------|
| **Soniox** | Streaming/Async | Ultra-low latency, custom vocabulary |
| **AssemblyAI** | Streaming | High accuracy with punctuation |
| **Deepgram** | Streaming | Fast and cost-effective |
| **OpenAI Whisper** | Batch | High accuracy |
| **Azure Speech** | Streaming | Microsoft enterprise STT |
| **Gladia** | Streaming | Audio intelligence API |
| **Groq** | Batch | Fast Whisper inference |
| **Speechmatics** | Streaming | Specialized ASR |
| **Google Cloud STT** | Streaming | Enterprise-grade |
| **ElevenLabs** | Batch | Scribe model integration |

---

## Quick Start

### Windows (One-Click)
1. Download the repository
2. Double-click `start.bat`
   - Automatically sets up Python environment
   - Installs dependencies
   - Prompts for API keys
3. Access the web UI at `http://localhost:5000`

### Manual Installation
```bash
# Clone repository
git clone https://github.com/YourUsername/Scriber.git
cd Scriber

# Install Python dependencies
pip install -r requirements.txt

# Start the backend
python -m src.web_api

# In a new terminal, start the frontend
cd Frontend
npm install
npm run dev:client
```

---

## Architecture

```
Scriber/
├── src/                    # Python Backend
│   ├── web_api.py          # HTTP/WebSocket API server (aiohttp)
│   ├── pipeline.py         # Multi-engine STT pipeline (Pipecat)
│   ├── summarization.py    # LLM summarization (OpenAI/Gemini)
│   ├── youtube_api.py      # YouTube Data API integration
│   ├── youtube_download.py # Audio download via yt-dlp
│   ├── config.py           # Configuration management
│   ├── main.py             # Desktop app entry point
│   └── ...
├── Frontend/               # React Web UI
│   └── client/src/pages/
│       ├── LiveMic.tsx     # Live microphone recording
│       ├── Youtube.tsx     # YouTube search & transcription
│       ├── FileTranscribe.tsx # File upload transcription
│       ├── TranscriptDetail.tsx # View transcript & summary
│       └── Settings.tsx    # API keys & preferences
├── settings.json           # Persistent settings (summarization prompt)
├── .env                    # API keys and configuration
├── start.bat               # Windows launcher
└── start.sh                # Linux/Mac launcher
```

---

## Configuration

### API Keys (`.env` file)
```env
# STT Services (at least one required)
SONIOX_API_KEY=your_key
ASSEMBLYAI_API_KEY=your_key
DEEPGRAM_API_KEY=your_key
OPENAI_API_KEY=your_key
AZURE_SPEECH_KEY=your_key
AZURE_SPEECH_REGION=westus
GLADIA_API_KEY=your_key
GROQ_API_KEY=your_key
SPEECHMATICS_API_KEY=your_key
ELEVENLABS_API_KEY=your_key
GOOGLE_APPLICATION_CREDENTIALS=path/to/json
GOOGLE_API_KEY=your_gemini_key

# YouTube (for YouTube tab)
YOUTUBE_API_KEY=your_key

# App Settings
SCRIBER_DEFAULT_STT=soniox
SCRIBER_HOTKEY=ctrl+alt+s
SCRIBER_MODE=toggle              # toggle or push_to_talk
SCRIBER_INJECT_METHOD=auto       # auto, type, paste
SCRIBER_LANGUAGE=auto            # auto, en, de, fr, es, it, pt, nl
SCRIBER_AUTO_SUMMARIZE=0         # 1 to enable auto-summarization
SCRIBER_SUMMARIZATION_MODEL=gemini-flash-latest
```

### Settings JSON (`settings.json`)
Complex settings like the summarization prompt are stored in JSON for proper multi-line support:
```json
{
  "summarizationPrompt": "Your custom prompt here..."
}
```

---

## Web UI Features

### Transcript Detail View
- **Summary Section**: Expanded by default when available, with Markdown rendering
- **Transcript Section**: Full text with paragraph breaks
- **Copy Buttons**: Separate buttons for copying transcript and summary
- **Progress Indicators**: Real-time status (Downloading, Transcribing, Summarizing)
- **Summarize Button**: Manual summarization (hidden when auto-summarize is enabled)

### Settings Page
- Configure API keys for all STT services
- Select default STT provider
- Set hotkey, language, and injection method
- Enable/disable auto-summarization
- Choose summarization model (GPT or Gemini)
- Customize summarization prompt

---

## Requirements

- **OS**: Windows 10/11 (recommended), Linux/Mac supported
- **Python**: 3.10+
- **Node.js**: 18+ (for frontend)
- **FFmpeg**: Required for audio processing
- **yt-dlp**: Required for YouTube audio download

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/settings` | Get current settings |
| `PUT` | `/api/settings` | Update settings |
| `GET` | `/api/microphones` | List available microphones |
| `GET` | `/api/transcripts` | List all transcripts |
| `GET` | `/api/transcripts/:id` | Get transcript details |
| `DELETE` | `/api/transcripts/:id` | Delete a transcript |
| `POST` | `/api/transcripts/:id/summarize` | Generate summary |
| `POST` | `/api/youtube/search` | Search YouTube videos |
| `POST` | `/api/youtube/transcribe` | Start YouTube transcription |
| `POST` | `/api/transcribe/file` | Upload file for transcription |
| `WS` | `/ws` | WebSocket for real-time updates |

---

## Development

```bash
# Run backend with debug logging
python -m src.web_api

# Run frontend in development mode
cd Frontend
npm run dev:client      # Starts on http://localhost:5000

# Run tests
pytest tests/
```

---

## License

MIT License
