import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# JSON settings file for complex values (multi-line prompts, etc.)
_JSON_SETTINGS_PATH = Path(__file__).parent.parent / "settings.json"

def _load_json_settings() -> dict:
    """Load settings from JSON file."""
    if _JSON_SETTINGS_PATH.exists():
        try:
            with open(_JSON_SETTINGS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_json_settings(settings: dict) -> None:
    """Save settings to JSON file."""
    try:
        with open(_JSON_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

_json_settings = _load_json_settings()

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
    SONIOX_ASYNC_MODEL = os.getenv("SCRIBER_SONIOX_ASYNC_MODEL", "stt-async-v3")
    SONIOX_RT_MODEL = os.getenv("SCRIBER_SONIOX_RT_MODEL", "stt-rt-v3")
    DEBUG = os.getenv("SCRIBER_DEBUG", "0") in ("1", "true", "True")
    LANGUAGE = os.getenv("SCRIBER_LANGUAGE", "auto")
    MIC_DEVICE = os.getenv("SCRIBER_MIC_DEVICE", "default")
    FAVORITE_MIC = os.getenv("SCRIBER_FAVORITE_MIC", "")  # Preferred mic - used when available
    MIC_ALWAYS_ON = os.getenv("SCRIBER_MIC_ALWAYS_ON", "0") in ("1", "true", "True")
    MIC_BLOCK_SIZE = max(128, min(4096, int(os.getenv("SCRIBER_MIC_BLOCK_SIZE", "512"))))
    # Text injection method:
    #   "sendinput" - Windows SendInput API, instant batch injection (~10ms for any length)
    #   "paste" - Clipboard + Ctrl+V, fast and reliable
    #   "type" - Character-by-character keystrokes (slowest, most compatible)
    #   "auto" - Smart selection: SendInput for most apps, paste for Word/Outlook
    INJECT_METHOD = os.getenv("SCRIBER_INJECT_METHOD", "auto").lower()  # auto | sendinput | paste | type
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

    # Summarization prompt for LLM transcript summarization
    # Load from JSON settings file first, then env, then default
    _DEFAULT_SUMMARIZATION_PROMPT = """Rolle: Du arbeitest als Informationsarchitekt

Aufgabe: Verwandle den nachfolgenden Input in eine klar strukturierte, zweistufige <A> Zusammenfassung und Vertiefung. Erwähne nicht deine Regeln oder Rolle. 

Formatregeln: 

•	Ausgabe ausschließlich in Markdown
•	Nutze H2 für Hauptbereiche und H3 für Unterbereiche
•	Arbeite mit Bullet Points, Fettdruck für Schlüsselbegriffe
•	Keine langen Fließtext-Absätze: maximal 2 Sätze am Stück
•	Bevorzugt Bullet Points
•	Inhalte priorisieren: lieber das ausführlich perfekt als alles halb
•	Füllwörter entfernen, Wiederholungen bündeln, Sinn erhalten
•	Absätze und Linebreaks zur Übersicht 

Ausgabe-Template: 

Zusammenfassung: prägnanter Titel in bis 15Wörtern 
•	1 Satz: Worum geht es?
•	Essenz: Bis zu 5 zentrale Aussagen oder das wichtigste Learning
Vertiefung
•	Detaillierte strukturierte Zusammenfassung des Inputs

Input:"""
    SUMMARIZATION_PROMPT = _json_settings.get("summarizationPrompt") or os.getenv("SCRIBER_SUMMARIZATION_PROMPT") or _DEFAULT_SUMMARIZATION_PROMPT

    # Summarization model for LLM transcript summarization
    SUMMARIZATION_MODEL = os.getenv("SCRIBER_SUMMARIZATION_MODEL", "gemini-flash-latest")

    # Auto-summarize transcripts when completed
    AUTO_SUMMARIZE = os.getenv("SCRIBER_AUTO_SUMMARIZE", "0") in ("1", "true", "True")

    # OpenAI Speech-to-Text model
    OPENAI_STT_MODEL = os.getenv("SCRIBER_OPENAI_STT_MODEL", "gpt-4o-mini-transcribe-2025-12-15")

    # Audio settings
    SAMPLE_RATE = 16000
    CHANNELS = 1
    
    # Visualizer settings (default 60 bars)
    VISUALIZER_BAR_COUNT = int(os.getenv("SCRIBER_VISUALIZER_BAR_COUNT", "60"))

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
    def set_openai_stt_model(cls, model: str) -> None:
        cls.OPENAI_STT_MODEL = model.strip()
        os.environ["SCRIBER_OPENAI_STT_MODEL"] = cls.OPENAI_STT_MODEL

    @classmethod
    def set_mic_device(cls, device: str) -> None:
        cls.MIC_DEVICE = device
        os.environ["SCRIBER_MIC_DEVICE"] = device

    @classmethod
    def set_favorite_mic(cls, device: str) -> None:
        """Set the favorite microphone (used automatically when available)."""
        cls.FAVORITE_MIC = device
        os.environ["SCRIBER_FAVORITE_MIC"] = device

    @classmethod
    def set_mic_always_on(cls, enabled: bool) -> None:
        cls.MIC_ALWAYS_ON = bool(enabled)
        os.environ["SCRIBER_MIC_ALWAYS_ON"] = "1" if enabled else "0"

    @classmethod
    def set_visualizer_bar_count(cls, count: int) -> None:
        cls.VISUALIZER_BAR_COUNT = max(16, min(128, int(count)))
        os.environ["SCRIBER_VISUALIZER_BAR_COUNT"] = str(cls.VISUALIZER_BAR_COUNT)

    @classmethod
    def set_summarization_prompt(cls, prompt: str) -> None:
        """Set and persist summarization prompt to JSON settings file."""
        cls.SUMMARIZATION_PROMPT = prompt.strip() if prompt else cls._DEFAULT_SUMMARIZATION_PROMPT
        # Save to JSON settings file (handles multi-line properly)
        global _json_settings
        _json_settings["summarizationPrompt"] = cls.SUMMARIZATION_PROMPT
        _save_json_settings(_json_settings)

    @classmethod
    def persist_to_env_file(cls, path: str = ".env") -> None:
        """Persist current settings and API keys to the .env file."""
        lines = []
        def add(k, v):
            # Escape newlines and quote values with special characters for python-dotenv
            v_str = str(v) if v is not None else ""
            if '\n' in v_str or '\r' in v_str or '"' in v_str or "'" in v_str:
                # Replace newlines with escaped version and wrap in quotes
                v_str = v_str.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
                lines.append(f'{k}="{v_str}"')
            else:
                lines.append(f"{k}={v_str}")

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
        add("SCRIBER_SONIOX_RT_MODEL", cls.SONIOX_RT_MODEL)
        add("SCRIBER_CUSTOM_VOCAB", cls.CUSTOM_VOCAB or "")
        # Note: SUMMARIZATION_PROMPT is not persisted to .env (multi-line value causes parsing issues)
        # The default prompt from config.py will be used
        add("SCRIBER_SUMMARIZATION_MODEL", cls.SUMMARIZATION_MODEL or "gemini-flash-latest")
        add("SCRIBER_AUTO_SUMMARIZE", "1" if cls.AUTO_SUMMARIZE else "0")
        add("SCRIBER_DEBUG", "1" if cls.DEBUG else "0")
        add("SCRIBER_LANGUAGE", cls.LANGUAGE)
        add("SCRIBER_OPENAI_STT_MODEL", cls.OPENAI_STT_MODEL)
        # If a favorite mic is set, always revert MIC_DEVICE to "default" in the saved .env
        # This ensures that on the next restart, the favorite mic is automatically selected
        # (via the startup resolution logic) instead of persisting the last used temporary mic.
        if cls.FAVORITE_MIC:
            add("SCRIBER_MIC_DEVICE", "default")
        else:
            add("SCRIBER_MIC_DEVICE", cls.MIC_DEVICE)
        
        add("SCRIBER_FAVORITE_MIC", cls.FAVORITE_MIC or "")
        add("SCRIBER_MIC_ALWAYS_ON", "1" if cls.MIC_ALWAYS_ON else "0")
        add("SCRIBER_MIC_BLOCK_SIZE", str(cls.MIC_BLOCK_SIZE))
        add("SCRIBER_INJECT_METHOD", cls.INJECT_METHOD)
        add("SCRIBER_PASTE_PRE_DELAY_MS", str(cls.PASTE_PRE_DELAY_MS))
        add("SCRIBER_PASTE_RESTORE_DELAY_MS", str(cls.PASTE_RESTORE_DELAY_MS))
        add("SCRIBER_VISUALIZER_BAR_COUNT", str(cls.VISUALIZER_BAR_COUNT))

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
