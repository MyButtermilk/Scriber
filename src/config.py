import os
import json
from dotenv import dotenv_values, load_dotenv

from src.runtime.paths import env_path, migrate_legacy_runtime_data, repo_root, settings_path

_BOOTSTRAP_ENV_KEYS = {
    "SCRIBER_AUTO_MIGRATE_LEGACY_DATA",
    "SCRIBER_DATA_DIR",
    "SCRIBER_LEGACY_DATA_DIR",
    "SCRIBER_SKIP_LEGACY_DATA_MIGRATION",
}


def _bootstrap_runtime_env() -> None:
    """Load only path-related env vars needed before the canonical .env path exists."""
    legacy_env = repo_root() / ".env"
    if not legacy_env.is_file():
        return
    for key, value in dotenv_values(legacy_env).items():
        if key in _BOOTSTRAP_ENV_KEYS and value is not None and key not in os.environ:
            os.environ[key] = value


_bootstrap_runtime_env()
migrate_legacy_runtime_data()
load_dotenv(env_path())

# JSON settings file for complex values (multi-line prompts, etc.)
_JSON_SETTINGS_PATH = settings_path()

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
    DEFAULT_SONIOX_ASYNC_MODEL = "stt-async-v5"
    DEFAULT_SONIOX_RT_MODEL = "stt-rt-v5"
    DEFAULT_ASSEMBLYAI_ASYNC_MODEL = "universal-3-5-pro"
    DEFAULT_ASSEMBLYAI_RT_MODEL = "universal-3-5-pro"

    # API Keys
    SONIOX_API_KEY = os.getenv("SONIOX_API_KEY")
    MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
    SMALLEST_API_KEY = os.getenv("SMALLEST_API_KEY")
    ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
    ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # For Gemini
    GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") # For Cloud STT
    YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")  # For Youtube Data API (future Youtube tab)

    DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
    CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")
    AZURE_MAI_SPEECH_KEY = os.getenv("AZURE_MAI_SPEECH_KEY")
    AZURE_MAI_REGION = os.getenv("SCRIBER_AZURE_MAI_REGION", "northeurope")
    AZURE_MAI_MODEL = os.getenv("SCRIBER_AZURE_MAI_MODEL", "mai-transcribe-1.5")
    GLADIA_API_KEY = os.getenv("GLADIA_API_KEY")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    SPEECHMATICS_API_KEY = os.getenv("SPEECHMATICS_API_KEY")

    # Application settings
    HOTKEY = os.getenv("SCRIBER_HOTKEY", "ctrl+alt+s")
    DEFAULT_STT_SERVICE = os.getenv("SCRIBER_DEFAULT_STT", "soniox")
    SONIOX_MODE = os.getenv("SCRIBER_SONIOX_MODE", "realtime").lower()  # realtime | async
    SONIOX_ASYNC_MODEL = os.getenv("SCRIBER_SONIOX_ASYNC_MODEL", DEFAULT_SONIOX_ASYNC_MODEL)
    SONIOX_RT_MODEL = os.getenv("SCRIBER_SONIOX_RT_MODEL", DEFAULT_SONIOX_RT_MODEL)
    ASSEMBLYAI_ASYNC_MODEL = os.getenv("SCRIBER_ASSEMBLYAI_ASYNC_MODEL", DEFAULT_ASSEMBLYAI_ASYNC_MODEL)
    ASSEMBLYAI_RT_MODEL = os.getenv("SCRIBER_ASSEMBLYAI_RT_MODEL", DEFAULT_ASSEMBLYAI_RT_MODEL)
    MISTRAL_RT_MODEL = os.getenv("SCRIBER_MISTRAL_RT_MODEL", "voxtral-mini-transcribe-realtime-2602")
    MISTRAL_ASYNC_MODEL = os.getenv("SCRIBER_MISTRAL_ASYNC_MODEL", "voxtral-mini-2602")
    DEBUG = os.getenv("SCRIBER_DEBUG", "0") in ("1", "true", "True")
    LANGUAGE = os.getenv("SCRIBER_LANGUAGE", "auto")
    MIC_DEVICE = os.getenv("SCRIBER_MIC_DEVICE", "default")
    FAVORITE_MIC = os.getenv("SCRIBER_FAVORITE_MIC", "")  # Preferred mic - used when available
    MIC_ALWAYS_ON = os.getenv("SCRIBER_MIC_ALWAYS_ON", "0") in ("1", "true", "True")
    SEGMENT_SPEECH_WITH_VAD = (
        str(_json_settings.get("segmentSpeechWithVad", os.getenv("SCRIBER_SEGMENT_SPEECH_WITH_VAD", "0")))
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )
    MIC_POST_RECORDING_PREWARM_SECONDS = max(
        0.0,
        min(600.0, float(os.getenv("SCRIBER_MIC_POST_RECORDING_PREWARM_SECONDS", "120") or 120)),
    )
    MIC_BLOCK_SIZE = max(128, min(4096, int(os.getenv("SCRIBER_MIC_BLOCK_SIZE", "512"))))
    MIC_PREBUFFER_MS = max(0, min(2000, int(os.getenv("SCRIBER_MIC_PREBUFFER_MS", "400"))))
    # Text injection method:
    #   "sendinput" - Windows SendInput API, instant batch injection (~10ms for any length)
    #   "paste" - Clipboard + Ctrl+V, fast and reliable
    #   "tauri" - Opt-in Tauri shell IPC clipboard + Ctrl+V path
    #   "type" - Character-by-character keystrokes (slowest, most compatible)
    #   "auto" - Current default: Python clipboard paste, with Python fallbacks
    INJECT_METHOD = os.getenv("SCRIBER_INJECT_METHOD", "auto").lower()  # auto | sendinput | paste | type | tauri
    DISABLE_TEXT_INJECTION = os.getenv("SCRIBER_DISABLE_TEXT_INJECTION", "0") in ("1", "true", "True")
    INJECT_TARGET_TITLE = os.getenv("SCRIBER_INJECT_TARGET_TITLE", "").strip()
    # Clipboard paste tuning (Windows). Some apps (Word/Outlook) process paste asynchronously.
    PASTE_PRE_DELAY_MS = int(os.getenv("SCRIBER_PASTE_PRE_DELAY_MS", "80"))
    PASTE_RESTORE_DELAY_MS = int(os.getenv("SCRIBER_PASTE_RESTORE_DELAY_MS", "1500"))

    SERVICE_API_KEY_MAP = {
        "soniox": "SONIOX_API_KEY",
        "soniox_async": "SONIOX_API_KEY",
        "gemini_stt": "GOOGLE_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "mistral_async": "MISTRAL_API_KEY",
        "smallest": "SMALLEST_API_KEY",
        "smallest_async": "SMALLEST_API_KEY",
        "assemblyai": "ASSEMBLYAI_API_KEY",
        "assemblyai_realtime": "ASSEMBLYAI_API_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
        "deepgram": "DEEPGRAM_API_KEY",
        "deepgram_async": "DEEPGRAM_API_KEY",
        "openai": "OPENAI_API_KEY",
        "openai_async": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "cerebras": "CEREBRAS_API_KEY",
        "azure_mai": "AZURE_MAI_SPEECH_KEY",
        "gladia": "GLADIA_API_KEY",
        "gladia_async": "GLADIA_API_KEY",
        "groq": "GROQ_API_KEY",
        "speechmatics": "SPEECHMATICS_API_KEY",
        "speechmatics_async": "SPEECHMATICS_API_KEY",
        "onnx_local": None,  # No API key needed for local models
    }

    SERVICE_LABELS = {
        "soniox": "Soniox",
        "soniox_async": "Soniox (Async)",
        "gemini_stt": "Gemini STT",
        "mistral": "Mistral (Realtime)",
        "mistral_async": "Mistral (Async)",
        "smallest": "Smallest AI (Realtime)",
        "smallest_async": "Smallest AI (Async)",
        "assemblyai": "Assembly AI Universal-3.5-Pro",
        "assemblyai_realtime": "Assembly AI Universal-3.5-Pro Realtime",
        "google": "Google Cloud",
        "elevenlabs": "ElevenLabs",
        "deepgram": "Deepgram (Streaming)",
        "deepgram_async": "Deepgram (Async)",
        "openai": "OpenAI Realtime",
        "openai_async": "OpenAI Batch",
        "azure_mai": "Microsoft MAI Transcribe",
        "gladia": "Gladia (Streaming)",
        "gladia_async": "Gladia (Async)",
        "groq": "Groq",
        "speechmatics": "Speechmatics (Realtime)",
        "speechmatics_async": "Speechmatics (Batch)",
        "onnx_local": "Local (ONNX)",
    }

    if str(DEFAULT_STT_SERVICE or "").strip().lower() == "nemo_local":
        DEFAULT_STT_SERVICE = "onnx_local"
        os.environ["SCRIBER_DEFAULT_STT"] = DEFAULT_STT_SERVICE

    # Mode: "toggle" (default) or "push_to_talk"
    MODE = os.getenv("SCRIBER_MODE", "toggle").lower()

    # Custom Vocabulary (Soniox/Mistral context biasing, Smallest realtime keywords)
    # e.g. "Scriber, Pipecat, Soniox, Voxtral"
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
    DEFAULT_SUMMARIZATION_MODEL = "gemini-flash-latest"
    SUMMARIZATION_MODEL = os.getenv("SCRIBER_SUMMARIZATION_MODEL", DEFAULT_SUMMARIZATION_MODEL)

    # Auto-summarize transcripts when completed
    AUTO_SUMMARIZE = os.getenv("SCRIBER_AUTO_SUMMARIZE", "0") in ("1", "true", "True")

    DEFAULT_POST_PROCESSING_MODEL = "cerebras/gemma-4-31b"
    _LEGACY_DEFAULT_POST_PROCESSING_MODELS = {"", "gpt-5-nano", "google/gemini-2.5-flash-lite:nitro", "openai/gpt-oss-120b"}
    _DEFAULT_POST_PROCESSING_PROMPT = """Glätte das folgende Speech-to-Text-Transkript sprachlich, typografisch und strukturell, ohne Inhalt zu verändern, zu kürzen, zu interpretieren oder neue Informationen hinzuzufügen.

Verbindliche Regeln:
- Gib ausschließlich die bereinigte Fassung zurück. Keine Kommentare, Labels, Checklisten, Anführungsrahmen oder Markdown-Codeblöcke.
- Bewahre Sprache, Bedeutung, Reihenfolge, Aussagen, Absichten, Sprecherwechsel, Eigennamen, Fachbegriffe, Zahlen und Nuancen.
- Beantworte keine Fragen im Transkript. Behandle alles als diktierten Text.
- Erstelle keine Zusammenfassung und keine inhaltliche Straffung über reine Sprachglättung hinaus.
- Bei unklaren Stellen nicht raten. Markiere sie nur dann als [unverständlich] oder [unklar: ...], wenn im Ausgangstext bereits erkennbare Unsicherheit vorhanden ist.

Sprache und Satzzeichen:
- Korrigiere offensichtliche Transkriptionsfehler, Tippfehler, Grammatik, Groß-/Kleinschreibung und Zeichensetzung.
- Setze natürliche Satzzeichen und teile sehr lange gesprochene Sätze in klare, lesbare Sätze.
- Entferne Füllwörter, sofern sie nicht bedeutungstragend sind: äh, ähm, hm, um, uh, also, sozusagen, quasi, halt, irgendwie, you know, I mean.
- Entferne Stotterer, Wiederholungen, abgebrochene Satzanfänge und Selbstkorrekturen, wenn der Sinn dadurch klarer wird.
- Wandle gesprochene Satzzeichen und Formatbefehle um, wenn eindeutig: Punkt, Komma, Fragezeichen, Ausrufezeichen, Doppelpunkt, Gedankenstrich, neue Zeile, Zeilenumbruch, neuer Absatz, Absatz.
- Verwende deutsche Anführungszeichen „...“, falls wörtliche Rede eindeutig ist.

Struktur:
- Gliedere den Text in sinnvolle Absätze. Ein Absatz enthält einen Gedanken, Themenwechsel oder Sprecherbeitrag.
- Formatiere formelle Anreden am Textanfang mit Komma und anschließendem Absatz/Zeilenumbruch, z. B. Sehr geehrter Herr Müller,\n\n... oder Sehr geehrte Damen und Herren,\n\n...
- Füge Zeilenumbrüche nach Begrüßungen, vor Listen, bei Themenwechseln und bei Signaturen ein.
- Erhalte vorhandene Sprecherbezeichnungen wie „Sprecher 1:“, „Interviewer:“ oder Namen.
- Erhalte vorhandene Zeitstempel exakt.
- Füge keine Überschriften hinzu, außer sie sind bereits im Transkript angelegt oder als diktierter Formatwunsch eindeutig.
- Nutze Aufzählungszeichen mit "- ", wenn der Sprecher klar mehrere Punkte, Aufgaben, Beispiele, Voraussetzungen oder Argumente aufzählt.
- Erzeuge keine Liste aus einem normalen Fließsatz; nutze Listen nur für echte Aufzählungen.

Zahlen, Daten, Uhrzeiten und Einheiten:
- Formatiere Zahlen konsistent nach deutscher Schreibweise, wenn der Text deutsch ist: 1.250, 25.000, 1.000.000, 3,5.
- Verwende Ziffern für Mengen, Preise, Prozentwerte, Maße, Flächen, Zeitangaben, Daten, Telefonnummern, Adressen und technische Werte.
- Formatiere Geld, Prozent, Daten und Uhrzeiten, wenn eindeutig: fünfzehn Prozent -> 15 %, zweitausend fünfhundert Euro -> 2.500 €, am dritten vierten zwanzig vierundzwanzig -> am 03.04.2024, vierzehn Uhr dreißig -> 14:30 Uhr.
- Formatiere Einheiten kompakt und professionell: Euro pro Quadratmeter -> €/m², Quadratmeter -> m², Kubikmeter -> m³, Kilometer pro Stunde -> km/h, Kilowattstunden -> kWh, Kilowattstunden pro Quadratmeter und Jahr -> kWh/m²a, Grad Celsius -> °C, Meter -> m, Zentimeter -> cm, Kilogramm -> kg.
- Setze zwischen Zahl und Einheit ein Leerzeichen, sofern üblich: 25 m², 3,5 kg, 120 km/h, 15 %.
- Bei zusammengesetzten Einheiten ohne vorangestellte Zahl nutze kompakte Schreibweise: €/m², kWh/m²a.

Transkript:
${output}"""
    POST_PROCESSING_ENABLED = (
        str(_json_settings.get("postProcessingEnabled", os.getenv("SCRIBER_POST_PROCESSING_ENABLED", "1")))
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )
    POST_PROCESSING_HOTKEY = (
        _json_settings.get("postProcessingHotkey")
        or os.getenv("SCRIBER_POST_PROCESSING_HOTKEY")
        or "ctrl+shift+p"
    )
    POST_PROCESSING_PROMPT = (
        _json_settings.get("postProcessingPrompt")
        or os.getenv("SCRIBER_POST_PROCESSING_PROMPT")
        or _DEFAULT_POST_PROCESSING_PROMPT
    )
    _configured_post_processing_model = (_json_settings.get("postProcessingModel") or "").strip()
    _env_post_processing_model = (os.getenv("SCRIBER_POST_PROCESSING_MODEL") or "").strip()
    if _configured_post_processing_model in _LEGACY_DEFAULT_POST_PROCESSING_MODELS:
        _configured_post_processing_model = ""
    if _env_post_processing_model in _LEGACY_DEFAULT_POST_PROCESSING_MODELS:
        _env_post_processing_model = ""
    POST_PROCESSING_MODEL = (
        _configured_post_processing_model
        or _env_post_processing_model
        or DEFAULT_POST_PROCESSING_MODEL
    )

    # OpenAI Speech-to-Text models. Keep realtime and batch separate so the
    # low-latency websocket model cannot accidentally be used for file upload.
    OPENAI_REALTIME_STT_MODEL = os.getenv("SCRIBER_OPENAI_REALTIME_STT_MODEL", "gpt-realtime-whisper")
    OPENAI_STT_MODEL = os.getenv("SCRIBER_OPENAI_STT_MODEL", "gpt-4o-mini-transcribe-2025-12-15")

    # ONNX Local STT settings
    # Supported models include Parakeet/Canary ONNX snapshots and the fp32 DeskScribe Primeline package.
    ONNX_MODEL = os.getenv("SCRIBER_ONNX_MODEL", "nemo-parakeet-tdt-0.6b-v3")
    ONNX_QUANTIZATION = os.getenv("SCRIBER_ONNX_QUANTIZATION", "int8")  # int8 | fp16 | fp32
    ONNX_USE_GPU = os.getenv("SCRIBER_ONNX_USE_GPU", "0") in ("1", "true", "True")

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
    def set_post_processing_hotkey(cls, hotkey: str) -> None:
        cls.POST_PROCESSING_HOTKEY = hotkey.strip()
        os.environ["SCRIBER_POST_PROCESSING_HOTKEY"] = cls.POST_PROCESSING_HOTKEY
        global _json_settings
        _json_settings["postProcessingHotkey"] = cls.POST_PROCESSING_HOTKEY
        _save_json_settings(_json_settings)

    @classmethod
    def set_mode(cls, mode: str) -> None:
        cls.MODE = mode.lower().strip()
        os.environ["SCRIBER_MODE"] = cls.MODE

    @classmethod
    def set_default_service(cls, service: str) -> None:
        if str(service or "").strip().lower() == "nemo_local":
            service = "onnx_local"
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
    def set_openai_realtime_stt_model(cls, model: str) -> None:
        cls.OPENAI_REALTIME_STT_MODEL = model.strip()
        os.environ["SCRIBER_OPENAI_REALTIME_STT_MODEL"] = cls.OPENAI_REALTIME_STT_MODEL

    @classmethod
    def set_onnx_model(cls, model: str) -> None:
        cls.ONNX_MODEL = model.strip()
        os.environ["SCRIBER_ONNX_MODEL"] = cls.ONNX_MODEL

    @classmethod
    def set_onnx_quantization(cls, quantization: str) -> None:
        cls.ONNX_QUANTIZATION = quantization.strip()
        os.environ["SCRIBER_ONNX_QUANTIZATION"] = cls.ONNX_QUANTIZATION

    @classmethod
    def set_onnx_use_gpu(cls, enabled: bool) -> None:
        cls.ONNX_USE_GPU = bool(enabled)
        os.environ["SCRIBER_ONNX_USE_GPU"] = "1" if cls.ONNX_USE_GPU else "0"

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
    def set_segment_speech_with_vad(cls, enabled: bool) -> None:
        cls.SEGMENT_SPEECH_WITH_VAD = bool(enabled)
        os.environ["SCRIBER_SEGMENT_SPEECH_WITH_VAD"] = "1" if cls.SEGMENT_SPEECH_WITH_VAD else "0"
        global _json_settings
        _json_settings["segmentSpeechWithVad"] = cls.SEGMENT_SPEECH_WITH_VAD
        _save_json_settings(_json_settings)

    @classmethod
    def set_mic_post_recording_prewarm_seconds(cls, seconds: float) -> None:
        value = max(0.0, min(600.0, float(seconds)))
        cls.MIC_POST_RECORDING_PREWARM_SECONDS = value
        os.environ["SCRIBER_MIC_POST_RECORDING_PREWARM_SECONDS"] = f"{value:g}"

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
    def set_post_processing_enabled(cls, enabled: bool) -> None:
        cls.POST_PROCESSING_ENABLED = bool(enabled)
        os.environ["SCRIBER_POST_PROCESSING_ENABLED"] = "1" if cls.POST_PROCESSING_ENABLED else "0"
        global _json_settings
        _json_settings["postProcessingEnabled"] = cls.POST_PROCESSING_ENABLED
        _save_json_settings(_json_settings)

    @classmethod
    def set_post_processing_prompt(cls, prompt: str) -> None:
        cls.POST_PROCESSING_PROMPT = prompt.strip() if prompt else cls._DEFAULT_POST_PROCESSING_PROMPT
        global _json_settings
        _json_settings["postProcessingPrompt"] = cls.POST_PROCESSING_PROMPT
        _save_json_settings(_json_settings)

    @classmethod
    def set_post_processing_model(cls, model: str) -> None:
        cls.POST_PROCESSING_MODEL = model.strip() or cls.DEFAULT_POST_PROCESSING_MODEL
        os.environ["SCRIBER_POST_PROCESSING_MODEL"] = cls.POST_PROCESSING_MODEL
        global _json_settings
        _json_settings["postProcessingModel"] = cls.POST_PROCESSING_MODEL
        _save_json_settings(_json_settings)

    @classmethod
    def persist_to_env_file(cls, path: str | None = None) -> None:
        """Persist current settings and API keys to the .env file."""
        target_path = env_path() if path is None else path
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
        add("MISTRAL_API_KEY", cls.MISTRAL_API_KEY or "")
        add("SMALLEST_API_KEY", cls.SMALLEST_API_KEY or "")
        add("ASSEMBLYAI_API_KEY", cls.ASSEMBLYAI_API_KEY or "")
        add("ELEVENLABS_API_KEY", cls.ELEVENLABS_API_KEY or "")
        add("GOOGLE_APPLICATION_CREDENTIALS", cls.GOOGLE_APPLICATION_CREDENTIALS or "")
        add("GOOGLE_API_KEY", getattr(cls, "GOOGLE_API_KEY", "") or "")
        add("YOUTUBE_API_KEY", getattr(cls, "YOUTUBE_API_KEY", "") or "")
        add("DEEPGRAM_API_KEY", cls.DEEPGRAM_API_KEY or "")
        add("OPENAI_API_KEY", cls.OPENAI_API_KEY or "")
        add("OPENROUTER_API_KEY", cls.OPENROUTER_API_KEY or "")
        add("CEREBRAS_API_KEY", cls.CEREBRAS_API_KEY or "")
        add("AZURE_MAI_SPEECH_KEY", cls.AZURE_MAI_SPEECH_KEY or "")
        add("SCRIBER_AZURE_MAI_REGION", cls.AZURE_MAI_REGION or "northeurope")
        add("SCRIBER_AZURE_MAI_MODEL", cls.AZURE_MAI_MODEL or "mai-transcribe-1.5")
        add("GLADIA_API_KEY", cls.GLADIA_API_KEY or "")
        add("GROQ_API_KEY", cls.GROQ_API_KEY or "")
        add("SPEECHMATICS_API_KEY", cls.SPEECHMATICS_API_KEY or "")

        add("SCRIBER_HOTKEY", cls.HOTKEY)
        add("SCRIBER_POST_PROCESSING_HOTKEY", cls.POST_PROCESSING_HOTKEY)
        add("SCRIBER_DEFAULT_STT", cls.DEFAULT_STT_SERVICE)
        add("SCRIBER_MODE", cls.MODE)
        add("SCRIBER_SONIOX_MODE", cls.SONIOX_MODE)
        add("SCRIBER_SONIOX_ASYNC_MODEL", cls.SONIOX_ASYNC_MODEL)
        add("SCRIBER_SONIOX_RT_MODEL", cls.SONIOX_RT_MODEL)
        add("SCRIBER_ASSEMBLYAI_ASYNC_MODEL", cls.ASSEMBLYAI_ASYNC_MODEL)
        add("SCRIBER_ASSEMBLYAI_RT_MODEL", cls.ASSEMBLYAI_RT_MODEL)
        add("SCRIBER_MISTRAL_RT_MODEL", cls.MISTRAL_RT_MODEL)
        add("SCRIBER_MISTRAL_ASYNC_MODEL", cls.MISTRAL_ASYNC_MODEL)
        add("SCRIBER_CUSTOM_VOCAB", cls.CUSTOM_VOCAB or "")
        # Note: SUMMARIZATION_PROMPT is not persisted to .env (multi-line value causes parsing issues)
        # The default prompt from config.py will be used
        add("SCRIBER_SUMMARIZATION_MODEL", cls.SUMMARIZATION_MODEL or cls.DEFAULT_SUMMARIZATION_MODEL)
        add("SCRIBER_AUTO_SUMMARIZE", "1" if cls.AUTO_SUMMARIZE else "0")
        add("SCRIBER_POST_PROCESSING_ENABLED", "1" if cls.POST_PROCESSING_ENABLED else "0")
        add("SCRIBER_POST_PROCESSING_MODEL", cls.POST_PROCESSING_MODEL or cls.DEFAULT_POST_PROCESSING_MODEL)
        add("SCRIBER_DEBUG", "1" if cls.DEBUG else "0")
        add("SCRIBER_LANGUAGE", cls.LANGUAGE)
        add("SCRIBER_OPENAI_STT_MODEL", cls.OPENAI_STT_MODEL)
        add("SCRIBER_OPENAI_REALTIME_STT_MODEL", cls.OPENAI_REALTIME_STT_MODEL)
        add("SCRIBER_ONNX_MODEL", cls.ONNX_MODEL)
        add("SCRIBER_ONNX_QUANTIZATION", cls.ONNX_QUANTIZATION)
        add("SCRIBER_ONNX_USE_GPU", "1" if cls.ONNX_USE_GPU else "0")
        # If a favorite mic is set, always revert MIC_DEVICE to "default" in the saved .env
        # This ensures that on the next restart, the favorite mic is automatically selected
        # (via the startup resolution logic) instead of persisting the last used temporary mic.
        if cls.FAVORITE_MIC:
            add("SCRIBER_MIC_DEVICE", "default")
        else:
            add("SCRIBER_MIC_DEVICE", cls.MIC_DEVICE)
        
        add("SCRIBER_FAVORITE_MIC", cls.FAVORITE_MIC or "")
        add("SCRIBER_MIC_ALWAYS_ON", "1" if cls.MIC_ALWAYS_ON else "0")
        add("SCRIBER_SEGMENT_SPEECH_WITH_VAD", "1" if cls.SEGMENT_SPEECH_WITH_VAD else "0")
        add(
            "SCRIBER_MIC_POST_RECORDING_PREWARM_SECONDS",
            f"{cls.MIC_POST_RECORDING_PREWARM_SECONDS:g}",
        )
        add("SCRIBER_MIC_BLOCK_SIZE", str(cls.MIC_BLOCK_SIZE))
        add("SCRIBER_MIC_PREBUFFER_MS", str(cls.MIC_PREBUFFER_MS))
        add("SCRIBER_INJECT_METHOD", cls.INJECT_METHOD)
        add("SCRIBER_DISABLE_TEXT_INJECTION", "1" if cls.DISABLE_TEXT_INJECTION else "0")
        add("SCRIBER_PASTE_PRE_DELAY_MS", str(cls.PASTE_PRE_DELAY_MS))
        add("SCRIBER_PASTE_RESTORE_DELAY_MS", str(cls.PASTE_RESTORE_DELAY_MS))
        add("SCRIBER_VISUALIZER_BAR_COUNT", str(cls.VISUALIZER_BAR_COUNT))

        with open(target_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
