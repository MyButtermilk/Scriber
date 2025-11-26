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
    SONIOX_MODE = os.getenv("SCRIBER_SONIOX_MODE", "realtime").lower()  # realtime | async
    SONIOX_ASYNC_MODEL = os.getenv("SCRIBER_SONIOX_ASYNC_MODEL", "stt-async-preview")

    SERVICE_API_KEY_MAP = {
        "soniox": "SONIOX_API_KEY",
        "soniox_async": "SONIOX_API_KEY",
        "assemblyai": "ASSEMBLYAI_API_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
        "deepgram": "DEEPGRAM_API_KEY",
        "openai": "OPENAI_API_KEY",
        "azure": "AZURE_SPEECH_KEY",
        "gladia": "GLADIA_API_KEY",
        "groq": "GROQ_API_KEY",
        "speechmatics": "SPEECHMATICS_API_KEY",
    }

    SERVICE_LABELS = {
        "soniox": "Soniox",
        "soniox_async": "Soniox (Async)",
        "assemblyai": "AssemblyAI",
        "google": "Google Cloud",
        "elevenlabs": "ElevenLabs",
        "deepgram": "Deepgram",
        "openai": "OpenAI",
        "azure": "Azure",
        "gladia": "Gladia",
        "groq": "Groq",
        "speechmatics": "Speechmatics",
        "aws": "AWS Transcribe",
    }

    # Mode: "toggle" (default) or "push_to_talk"
    MODE = os.getenv("SCRIBER_MODE", "toggle").lower()

    # Custom Vocabulary (Soniox only)
    # e.g. "Scriber, Pipecat, Soniox"
    CUSTOM_VOCAB = os.getenv("SCRIBER_CUSTOM_VOCAB", "")

    # Audio settings
    SAMPLE_RATE = 16000
    CHANNELS = 1

    @classmethod
    def get_api_key(cls, service_name: str) -> str:
        attr = cls.SERVICE_API_KEY_MAP.get(service_name)
        if not attr:
            return ""
        return getattr(cls, attr, "") or ""

    @classmethod
    def set_api_key(cls, service_name: str, value: str) -> None:
        attr = cls.SERVICE_API_KEY_MAP.get(service_name)
        if attr:
            setattr(cls, attr, value.strip())
            os.environ[attr] = value.strip()

    @classmethod
    def set_hotkey(cls, hotkey: str) -> None:
        cls.HOTKEY = hotkey.strip()
        os.environ["SCRIBER_HOTKEY"] = cls.HOTKEY

    @classmethod
    def set_mode(cls, mode: str) -> None:
        cls.MODE = mode.lower().strip()
        os.environ["SCRIBER_MODE"] = cls.MODE

    @classmethod
    def set_default_service(cls, service: str) -> None:
        cls.DEFAULT_STT_SERVICE = service
        os.environ["SCRIBER_DEFAULT_STT"] = service

    @classmethod
    def set_soniox_mode(cls, mode: str) -> None:
        cls.SONIOX_MODE = mode.lower().strip()
        os.environ["SCRIBER_SONIOX_MODE"] = cls.SONIOX_MODE
