# Scriber - Windows Voice Dictation

Scriber is a high-performance, AI-driven voice dictation application for Windows. It replicates the functionality of tools like Aqua Voice and Wispr Flow, allowing you to dictate text into any application system-wide.

## Quick Start (Windows)

1.  **Download** the repository.
2.  **Double-click** `start.bat`.
    *   It will automatically set up the environment, install dependencies, and prompt you for API keys.
3.  **Dictate**: Press `Ctrl+Alt+S` to start/stop listening.

## Features

*   **System-Wide Dictation**: Works in any application (Word, IDEs, Browser, etc.).
*   **Global Hotkey**: Activate voice capture with `Ctrl+Alt+S` (configurable).
*   **Multi-Engine Support**:
    *   **Soniox**: Ultra-low latency streaming with custom vocabulary.
    *   **AssemblyAI**: High accuracy with punctuation.
    *   **Deepgram**: Fast and cost-effective streaming.
    *   **OpenAI (Whisper)**: High accuracy via Whisper API.
    *   **Azure Speech**: Microsoft's enterprise STT.
    *   **Gladia**: Audio intelligence API.
    *   **Groq**: Fast inference for Whisper models.
    *   **Speechmatics**: Specialized ASR.
    *   **Google Cloud STT**: Enterprise-grade recognition.
    *   **ElevenLabs**: Scribe model integration.
*   **Smart Turn Detection**: Uses VAD and AI models to detect natural pauses.
*   **One-Click Setup**: Automated `start.bat` script for easy installation.

## Manual Installation

If you prefer to run it manually or are on Linux/Mac:

1.  Clone the repository.
2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
3.  Create a `.env` file with your API keys.
4.  Run:
    ```bash
    python src/main.py
    ```

## Configuration

The `start.bat` script will create a `.env` file for you. You can also edit it manually:

```env
# STT Service API Keys
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

# App Settings
SCRIBER_HOTKEY=ctrl+alt+s
SCRIBER_DEFAULT_STT=soniox  # Options: soniox, assemblyai, deepgram, openai, azure, gladia, groq, speechmatics, google, elevenlabs
SCRIBER_MODE=toggle         # toggle or push_to_talk
SCRIBER_CUSTOM_VOCAB=Scriber, Pipecat, Soniox
```

## Requirements

*   Windows 10/11 (Recommended)
*   Python 3.10+
*   Microphone
