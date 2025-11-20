import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # API Keys
    SONIOX_API_KEY = os.getenv("SONIOX_API_KEY")
    ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
    ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # For Gemini
    GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") # For Cloud STT

    DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY")
    AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")
    GLADIA_API_KEY = os.getenv("GLADIA_API_KEY")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    SPEECHMATICS_API_KEY = os.getenv("SPEECHMATICS_API_KEY")

    # Application settings
    HOTKEY = os.getenv("SCRIBER_HOTKEY", "ctrl+alt+s")
    DEFAULT_STT_SERVICE = os.getenv("SCRIBER_DEFAULT_STT", "soniox")

    # Mode: "toggle" (default) or "push_to_talk"
    MODE = os.getenv("SCRIBER_MODE", "toggle").lower()

    # Custom Vocabulary (Soniox only)
    # e.g. "Scriber, Pipecat, Soniox"
    CUSTOM_VOCAB = os.getenv("SCRIBER_CUSTOM_VOCAB", "")

    # Audio settings
    SAMPLE_RATE = 16000
    CHANNELS = 1
