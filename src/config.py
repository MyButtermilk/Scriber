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
    YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")  # For Youtube Data API (future Youtube tab)

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
    DEBUG = os.getenv("SCRIBER_DEBUG", "0") in ("1", "true", "True")
    LANGUAGE = os.getenv("SCRIBER_LANGUAGE", "auto")
    MIC_DEVICE = os.getenv("SCRIBER_MIC_DEVICE", "default")
    MIC_ALWAYS_ON = os.getenv("SCRIBER_MIC_ALWAYS_ON", "0") in ("1", "true", "True")
    # Text injection method: "type" (keystrokes), "paste" (clipboard + Ctrl+V), or "auto" (choose by app/window).
    INJECT_METHOD = os.getenv("SCRIBER_INJECT_METHOD", "auto").lower()  # auto | type | paste
    # Clipboard paste tuning (Windows). Some apps (Word/Outlook) process paste asynchronously.
    PASTE_PRE_DELAY_MS = int(os.getenv("SCRIBER_PASTE_PRE_DELAY_MS", "80"))
    PASTE_RESTORE_DELAY_MS = int(os.getenv("SCRIBER_PASTE_RESTORE_DELAY_MS", "1500"))

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

    @classmethod
    def set_debug(cls, enabled: bool) -> None:
        cls.DEBUG = bool(enabled)
        os.environ["SCRIBER_DEBUG"] = "1" if enabled else "0"

    @classmethod
    def set_language(cls, code: str) -> None:
        cls.LANGUAGE = code
        os.environ["SCRIBER_LANGUAGE"] = code

    @classmethod
    def set_mic_device(cls, device: str) -> None:
        cls.MIC_DEVICE = device
        os.environ["SCRIBER_MIC_DEVICE"] = device

    @classmethod
    def set_mic_always_on(cls, enabled: bool) -> None:
        cls.MIC_ALWAYS_ON = bool(enabled)
        os.environ["SCRIBER_MIC_ALWAYS_ON"] = "1" if enabled else "0"

    @classmethod
    def persist_to_env_file(cls, path: str = ".env") -> None:
        """Persist current settings and API keys to the .env file."""
        lines = []
        def add(k, v): lines.append(f"{k}={v}")

        add("SONIOX_API_KEY", cls.SONIOX_API_KEY or "")
        add("ASSEMBLYAI_API_KEY", cls.ASSEMBLYAI_API_KEY or "")
        add("ELEVENLABS_API_KEY", cls.ELEVENLABS_API_KEY or "")
        add("GOOGLE_APPLICATION_CREDENTIALS", cls.GOOGLE_APPLICATION_CREDENTIALS or "")
        add("GOOGLE_API_KEY", getattr(cls, "GOOGLE_API_KEY", "") or "")
        add("YOUTUBE_API_KEY", getattr(cls, "YOUTUBE_API_KEY", "") or "")
        add("DEEPGRAM_API_KEY", cls.DEEPGRAM_API_KEY or "")
        add("OPENAI_API_KEY", cls.OPENAI_API_KEY or "")
        add("AZURE_SPEECH_KEY", cls.AZURE_SPEECH_KEY or "")
        add("AZURE_SPEECH_REGION", cls.AZURE_SPEECH_REGION or "")
        add("GLADIA_API_KEY", cls.GLADIA_API_KEY or "")
        add("GROQ_API_KEY", cls.GROQ_API_KEY or "")
        add("SPEECHMATICS_API_KEY", cls.SPEECHMATICS_API_KEY or "")

        add("SCRIBER_HOTKEY", cls.HOTKEY)
        add("SCRIBER_DEFAULT_STT", cls.DEFAULT_STT_SERVICE)
        add("SCRIBER_MODE", cls.MODE)
        add("SCRIBER_SONIOX_MODE", cls.SONIOX_MODE)
        add("SCRIBER_SONIOX_ASYNC_MODEL", cls.SONIOX_ASYNC_MODEL)
        add("SCRIBER_CUSTOM_VOCAB", cls.CUSTOM_VOCAB or "")
        add("SCRIBER_DEBUG", "1" if cls.DEBUG else "0")
        add("SCRIBER_LANGUAGE", cls.LANGUAGE)
        add("SCRIBER_MIC_DEVICE", cls.MIC_DEVICE)
        add("SCRIBER_MIC_ALWAYS_ON", "1" if cls.MIC_ALWAYS_ON else "0")
        add("SCRIBER_INJECT_METHOD", cls.INJECT_METHOD)
        add("SCRIBER_PASTE_PRE_DELAY_MS", str(cls.PASTE_PRE_DELAY_MS))
        add("SCRIBER_PASTE_RESTORE_DELAY_MS", str(cls.PASTE_RESTORE_DELAY_MS))

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
