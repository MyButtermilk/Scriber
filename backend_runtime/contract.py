"""Compatibility contract shared by the frozen launcher and its builder."""

from __future__ import annotations

RUNTIME_CONTRACT_NAME = "scriber-frozen-python-runtime"
RUNTIME_CONTRACT_REVISION = 3
APPLICATION_LAYER_SCHEMA_VERSION = 1
RUNTIME_LAYER_SCHEMA_VERSION = 1
APPLICATION_ENTRY_POINT = "src.backend_worker:main"
APPLICATION_DIRECTORY_NAME = "app"
APPLICATION_MANIFEST_NAME = "app-layer-manifest.json"
RUNTIME_MANIFEST_NAME = "runtime-layer-manifest.json"

# These imports are the third-party boundary required by Scriber.  The frozen
# runtime check imports every entry without importing application code.  Keep
# the list credential-free and update the contract revision when its meaning
# changes incompatibly.
RUNTIME_REQUIRED_IMPORTS: tuple[tuple[str, str], ...] = (
    ("aiohttp", "backend HTTP and WebSocket server runtime"),
    ("comtypes", "Windows audio endpoint integration"),
    ("docx", "DOCX meeting export runtime"),
    ("dotenv", "local settings environment loader"),
    ("huggingface_hub", "local ONNX model download runtime"),
    ("keyboard", "compatibility hotkey runtime"),
    ("loguru", "structured backend logging runtime"),
    ("numpy", "audio and model array runtime"),
    ("onnx_asr", "local ONNX speech-to-text runtime"),
    ("onnxruntime", "Silero and local ONNX native runtime"),
    ("openai", "OpenAI API runtime"),
    ("pipecat.frames.frames", "Pipecat frame runtime"),
    ("pipecat.pipeline.pipeline", "Pipecat pipeline graph runtime"),
    ("pipecat.pipeline.task", "Pipecat pipeline task runtime"),
    ("pipecat.pipeline.runner", "Pipecat pipeline runner runtime"),
    ("pipecat.processors.frame_processor", "Pipecat frame processor runtime"),
    ("pipecat.services.ai_service", "Pipecat AI service runtime"),
    ("pipecat.services.settings", "Pipecat STT settings runtime"),
    ("pipecat.services.stt_service", "Pipecat STT base runtime"),
    ("pipecat.transcriptions.language", "Pipecat transcription language runtime"),
    ("pipecat.transports.base_input", "Pipecat audio input transport runtime"),
    ("pipecat.transports.base_transport", "Pipecat audio transport runtime"),
    ("pipecat.utils.time", "Pipecat timestamp runtime"),
    ("pipecat.audio.vad.vad_analyzer", "Pipecat VAD analyzer runtime"),
    ("pipecat.audio.vad.silero", "Silero VAD runtime"),
    (
        "pipecat.audio.turn.smart_turn.local_smart_turn_v3",
        "Smart Turn V3 runtime",
    ),
    ("pipecat.processors.audio.vad_processor", "Pipecat VAD processor runtime"),
    ("pipecat.turns.user_start", "Pipecat user-turn start runtime"),
    ("pipecat.turns.user_stop", "Pipecat user-turn stop runtime"),
    ("pipecat.turns.user_turn_processor", "Pipecat turn processor runtime"),
    ("pipecat.turns.user_turn_strategies", "Pipecat turn strategy runtime"),
    ("pipecat.services.soniox.stt", "Soniox realtime STT provider"),
    ("pipecat.services.assemblyai.stt", "AssemblyAI realtime STT provider"),
    ("pipecat.services.google.stt", "Google Cloud STT provider"),
    ("pipecat.services.deepgram.stt", "Deepgram STT provider"),
    ("pipecat.services.openai.stt", "OpenAI realtime STT provider"),
    ("pipecat.services.gladia.stt", "Gladia STT provider"),
    ("pipecat.services.groq.stt", "Groq STT provider"),
    ("pipecat.services.speechmatics.stt", "Speechmatics STT provider"),
    ("pipecat.services.elevenlabs.stt", "ElevenLabs STT provider"),
    ("pyautogui", "compatibility text injection runtime"),
    ("pycaw.pycaw", "Windows audio session runtime"),
    ("pyloudnorm", "local loudness compatibility runtime"),
    ("reportlab.platypus", "PDF meeting export runtime"),
    ("sounddevice", "audio device enumeration runtime"),
    ("tqdm", "model download progress runtime"),
    ("websockets.asyncio.client", "provider WebSocket client runtime"),
    ("yt_dlp", "YouTube media extraction runtime"),
    ("yt_dlp_ejs", "YouTube JavaScript challenge runtime"),
)

REQUIRED_PACKAGE_VERSIONS: tuple[tuple[str, str], ...] = (
    ("pipecat-ai", "1.5.0"),
    ("yt-dlp", "2026.7.4"),
    ("yt-dlp-ejs", "0.8.0"),
)

# Direct non-stdlib import roots allowed in ``src``.  A test derives the roots
# from the application AST so adding a new dependency cannot silently bypass
# the stable runtime contract.  Optional imports that are intentionally not
# frozen are tracked separately rather than disappearing from this boundary.
APPLICATION_EXTERNAL_IMPORT_ROOTS = frozenset(
    {
        "aiohttp",
        "comtypes",
        "docx",
        "dotenv",
        "huggingface_hub",
        "keyboard",
        "loguru",
        "numpy",
        "onnx_asr",
        "onnxruntime",
        "openai",
        "pipecat",
        "pyautogui",
        "pycaw",
        "pyloudnorm",
        "reportlab",
        "sounddevice",
        "tqdm",
        "websockets",
        "yt_dlp",
    }
)

APPLICATION_OPTIONAL_IMPORT_EXEMPTIONS = frozenset(
    {
        # ``src.gemini_transcribe`` keeps a compatibility-only optional import.
        # The current supported Gemini path does not require this legacy SDK,
        # and the PyInstaller spec excludes it deliberately.
        ("src/gemini_transcribe.py", "google.generativeai"),
    }
)
