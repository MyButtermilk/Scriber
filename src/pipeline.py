import asyncio
import aiohttp
import io
import wave
import contextlib
import tempfile
import os
import time
import inspect
import re
from typing import Any, Callable, Optional

from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    EndFrame,
    StartFrame,
    StopFrame,
    CancelFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
    ErrorFrame,
)
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from src.runtime.media_tools import find_media_tool
from src.runtime.provider_dependencies import import_provider_runtime_module
from src.runtime.subprocess_utils import hidden_subprocess_kwargs

try:
    from pipecat.audio.streams.input import SoundDeviceAudioInputStream
except ImportError:
    SoundDeviceAudioInputStream = None

try:
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.processors.user_idle_processor import UserIdleProcessor
    HAS_SMART_TURN = True
except ImportError:
    HAS_SMART_TURN = False
    logger.warning("LocalSmartTurnAnalyzerV3 or UserIdleProcessor not available")

try:
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    HAS_SILERO_VAD = True
except Exception:
    SileroVADAnalyzer = None
    HAS_SILERO_VAD = False
    logger.warning("Silero VAD not available; segmented STT may produce no transcripts")

try:
    from pipecat.audio.vad.vad_analyzer import VADParams
except Exception:
    VADParams = None


# ============================================================================
# Analyzer Cache for faster pipeline start
# Caches heavy ML-based analyzers (VAD, SmartTurn) to avoid reloading models
# ============================================================================
import threading

class _AnalyzerCache:
    """Thread-safe cache for expensive analyzers (VAD, SmartTurn).
    
    These analyzers load ML models that take 200-500ms to initialize.
    By caching them, subsequent recording sessions start faster.
    """
    _lock = threading.Lock()
    _vad_analyzer = None
    _smart_turn_analyzer = None
    
    @classmethod
    def get_vad_analyzer(cls):
        """Get or create a cached Silero VAD analyzer."""
        if not HAS_SILERO_VAD or not SileroVADAnalyzer:
            return None
        with cls._lock:
            if cls._vad_analyzer is None:
                logger.info("Initializing Silero VAD analyzer (cached for future use)")
                cls._vad_analyzer = SileroVADAnalyzer()
            return cls._vad_analyzer
    
    @classmethod
    def get_smart_turn_analyzer(cls):
        """Get or create a cached SmartTurn analyzer."""
        if not HAS_SMART_TURN:
            return None
        with cls._lock:
            if cls._smart_turn_analyzer is None:
                logger.info("Initializing SmartTurn V3 analyzer (cached for future use)")
                cls._smart_turn_analyzer = LocalSmartTurnAnalyzerV3()
            return cls._smart_turn_analyzer
    
    @classmethod
    def clear_cache(cls):
        """Clear cached analyzers (useful for testing or config changes)."""
        with cls._lock:
            cls._vad_analyzer = None
            cls._smart_turn_analyzer = None
            logger.debug("Analyzer cache cleared")


def _live_service_uses_async_finalization(service_name: str) -> bool:
    normalized = str(service_name or "")
    return (
        normalized in {
            "groq",
            "mistral",
            "soniox_async",
            "mistral_async",
            "smallest_async",
            "deepgram_async",
            "gladia_async",
            "openai_async",
            "speechmatics_async",
            "azure_mai",
            "assemblyai",
        }
        or (normalized == "soniox" and Config.SONIOX_MODE == "async")
    )


def _live_service_needs_local_vad(service_name: str) -> bool:
    normalized = str(service_name or "")
    return normalized in {"openai", "deepgram", "gladia", "elevenlabs"} or _live_service_uses_async_finalization(normalized)


def _live_service_uses_smart_turn(service_name: str) -> bool:
    return str(service_name or "") == "soniox" and Config.SONIOX_MODE == "realtime"


def _create_vad_analyzer(*, quiet_mic: bool = False):
    vad_analyzer = _AnalyzerCache.get_vad_analyzer()
    if not vad_analyzer:
        return None
    if quiet_mic and VADParams:
        try:
            vad_analyzer.set_sample_rate(Config.SAMPLE_RATE)
            params = vad_analyzer.params
            vad_analyzer.set_params(
                VADParams(
                    confidence=params.confidence,
                    start_secs=params.start_secs,
                    stop_secs=params.stop_secs,
                    min_volume=0.1,
                )
            )
        except Exception as exc:
            logger.debug(f"VAD param override failed: {exc}")
    return vad_analyzer


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _redact_provider_error_text(message: str) -> str:
    text = str(message or "").replace("\x00", "")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)\bToken\s+[A-Za-z0-9._~+/=-]+", "Token [REDACTED]", text)
    return text

_SONIOX_IMPORT_ERROR: BaseException | None = None
try:
    from pipecat.services.soniox.stt import SonioxSTTService, SonioxInputParams, SonioxContextObject
except Exception as exc:
    _SONIOX_IMPORT_ERROR = exc
    SonioxSTTService = None
    SonioxInputParams = None
    SonioxContextObject = None

# Fallback params object if older pipecat build lacks SonioxInputParams
class _SonioxParamsFallback:
    def __init__(self, context=None, vad_enabled=True, enable_speaker_diarization=False):
        self.context = context
        self.vad_enabled = vad_enabled
        self.enable_speaker_diarization = enable_speaker_diarization


def _format_speaker_transcript_tokens(tokens: list[dict[str, Any]]) -> str:
    """Format provider tokens with contiguous speaker labels."""
    if not tokens:
        return ""

    segments: list[tuple[str, str]] = []
    current_speaker: str | None = None
    current_text: list[str] = []

    for token in tokens:
        if not isinstance(token, dict):
            continue
        text = str(token.get("text") or "")
        speaker = str(token.get("speaker") or "").strip()

        if speaker and speaker != current_speaker:
            if current_text and current_speaker:
                segments.append((current_speaker, "".join(current_text).strip()))
            current_speaker = speaker
            current_text = [text]
        else:
            current_text.append(text)

    if current_text and current_speaker:
        segments.append((current_speaker, "".join(current_text).strip()))

    if not segments:
        return "".join(str(t.get("text") or "") for t in tokens if isinstance(t, dict)).strip()

    return "\n\n".join(
        f"[Speaker {speaker}]: {text}"
        for speaker, text in segments
        if text
    )


def _word_value(word: Any, key: str) -> Any:
    if isinstance(word, dict):
        return word.get(key)
    return getattr(word, key, None)


def _format_deepgram_words_with_speakers(words: list[Any]) -> str:
    speaker_map: dict[str, int] = {}
    next_speaker = 1
    blocks: list[tuple[int, list[str]]] = []

    for word in words:
        speaker_raw = _word_value(word, "speaker")
        if speaker_raw is None or speaker_raw == "":
            continue
        text = str(
            _word_value(word, "punctuated_word")
            or _word_value(word, "word")
            or ""
        ).strip()
        if not text:
            continue
        speaker_key = str(speaker_raw).strip()
        speaker_num = speaker_map.get(speaker_key)
        if speaker_num is None:
            speaker_num = next_speaker
            speaker_map[speaker_key] = speaker_num
            next_speaker += 1
        if not blocks or blocks[-1][0] != speaker_num:
            blocks.append((speaker_num, [text]))
        else:
            blocks[-1][1].append(text)

    return "\n\n".join(
        f"[Speaker {speaker_num}]: {' '.join(parts)}"
        for speaker_num, parts in blocks
        if parts
    ).strip()


def _diarized_text_from_frame_result(result: Any) -> str:
    if isinstance(result, list):
        tokens = [token for token in result if isinstance(token, dict)]
        if tokens and any(token.get("speaker") for token in tokens):
            return _format_speaker_transcript_tokens(tokens)
        return ""

    channel = getattr(result, "channel", None)
    alternatives = getattr(channel, "alternatives", None)
    if alternatives:
        alternative = alternatives[0]
        words = getattr(alternative, "words", None)
        if words:
            formatted = _format_deepgram_words_with_speakers(list(words))
            if formatted:
                return formatted

    if isinstance(result, dict):
        channel_payload = result.get("channel")
        if isinstance(channel_payload, dict):
            alternatives_payload = channel_payload.get("alternatives")
            if isinstance(alternatives_payload, list) and alternatives_payload:
                alternative = alternatives_payload[0]
                if isinstance(alternative, dict):
                    words = alternative.get("words")
                    if isinstance(words, list):
                        return _format_deepgram_words_with_speakers(words)

    return ""


class SonioxAsyncProcessor(FrameProcessor):
    """Async Soniox transcription using REST API; buffers audio and submits on EndFrame."""

    BASE_URL = "https://api.soniox.com/v1"

    def __init__(
        self,
        api_key: str,
        custom_vocab: str = "",
        model: str = "stt-async-v5",
        session: aiohttp.ClientSession = None,
        on_progress: Optional[Callable[[str], None]] = None,
        enable_speaker_diarization: bool = False,
    ):
        super().__init__()
        self.api_key = api_key
        self.custom_vocab = custom_vocab
        self.model = model
        self.session = session
        self.on_progress = on_progress
        self.enable_speaker_diarization = enable_speaker_diarization
        self._buffer = self._create_buffer()
        self._buffer_size = 0
        self._sample_rate = None
        self._channels = None

    def _create_buffer(self):
        """Use spooled temp file to cap RAM usage for long recordings."""
        return tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)

    def _reset_buffer(self) -> None:
        try:
            self._buffer.close()
        except Exception:
            pass
        self._buffer = self._create_buffer()
        self._buffer_size = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame):
            if not self._sample_rate:
                self._sample_rate = frame.sample_rate
            if not self._channels:
                self._channels = frame.num_channels
            self._buffer.write(frame.audio)
            self._buffer_size += len(frame.audio)
            await self.push_frame(frame, direction)
        elif isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            if getattr(self, "_skip_terminal_transcription", False):
                logger.info("Soniox async: skipping terminal transcription for silent recording")
                self._reset_buffer()
                await self.push_frame(frame, direction)
                return
            if not self._buffer_size:
                logger.debug("Soniox async: no audio buffered; skipping transcription")
                await self.push_frame(frame, direction)
                return
            try:
                self._buffer.seek(0)
                audio_bytes = self._buffer.read()
                text = await self._transcribe_async(audio_bytes)
                await self.push_frame(
                    TranscriptionFrame(
                        text=text,
                        user_id="user",
                        timestamp=time_now_iso8601(),
                        result=None,
                    ),
                    direction,
                )
            except Exception as e:
                logger.error(f"Soniox async transcription failed: {e}")
                await self.push_frame(ErrorFrame(error=f"soniox async error: {e}"), direction)
            await self.push_frame(frame, direction)
            self._reset_buffer()
        else:
            await self.push_frame(frame, direction)

    def _report_progress(self, msg: str) -> None:
        """Report progress to callback if set."""
        if self.on_progress:
            try:
                self.on_progress(msg)
            except Exception:
                pass

    async def _transcribe_async(self, audio_bytes: bytes) -> str:
        """
        Upload audio to Soniox async API. Prefer WebM/Opus; retry with WAV if Soniox rejects.
        """
        if not audio_bytes:
            return ""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        done_statuses = {"completed", "done", "succeeded", "success"}
        error_statuses = {"error", "failed", "canceled", "cancelled"}
        poll_start = asyncio.get_running_loop().time()
        # Heuristic: allow at least 60s, or up to ~3x audio duration (min 2m, max 10m)
        sr = self._sample_rate or 16000
        ch = self._channels or 1
        audio_secs = len(audio_bytes) / max(1, (sr * ch * 2))
        poll_timeout = min(600.0, max(120.0, max(60.0, audio_secs * 3.0)))

        self._report_progress("Uploading audio...")

        # OPTIMIZED: Smart format selection with single-pass fallback
        # Try WebM first (smaller, faster upload), fall back to WAV on encoding failure only
        # Avoids re-encoding on upload/API errors by caching both formats
        file_bytes = None
        content_type = None
        filename = None
        webm_encode_failed = False

        # Try WebM encoding first
        try:
            file_bytes, content_type, filename = await self._encode_audio(audio_bytes, prefer_webm=True)
            if Config.DEBUG:
                logger.info(f"Soniox async upload using WebM ({len(file_bytes)} bytes)")
        except Exception as e:
            logger.warning(f"WebM encoding failed ({e}), using WAV fallback")
            webm_encode_failed = True
            file_bytes, content_type, filename = await self._encode_audio(audio_bytes, prefer_webm=False)
            if Config.DEBUG:
                logger.info(f"Soniox async upload using WAV ({len(file_bytes)} bytes)")

        # Upload and transcribe (single attempt, no retry loop)
        try:
            data = aiohttp.FormData()
            data.add_field("file", file_bytes, filename=filename, content_type=content_type)
            async with self.session.post(
                f"{self.BASE_URL}/files",
                data=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                # Accept both 200 OK and 201 Created as success (201 is standard for resource creation)
                if resp.status not in (200, 201):
                    # Capture actual error response from Soniox
                    error_body = await resp.text()
                    logger.error(f"Soniox file upload failed: status={resp.status}, body={error_body}")
                    resp.raise_for_status()
                file_id = (await resp.json())["id"]

            payload = {"file_id": file_id, "model": self.model}
            # Build proper context object if custom_vocab is provided
            if self.custom_vocab:
                terms = [t.strip() for t in self.custom_vocab.split(",") if t.strip()]
                if terms:
                    payload["context"] = {"terms": terms}
            # Enable speaker diarization for file/youtube transcription
            if self.enable_speaker_diarization:
                payload["enable_speaker_diarization"] = True

            async with self.session.post(
                f"{self.BASE_URL}/transcriptions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp2:
                resp2.raise_for_status()
                transcription_id = (await resp2.json())["id"]

                # Poll status with exponential backoff
                self._report_progress("Processing transcription...")
                poll_count = 0
                delay = 0.5  # Start with 500ms for quick jobs
                poll_start_time = asyncio.get_running_loop().time()

                while True:
                    elapsed = asyncio.get_running_loop().time() - poll_start
                    if elapsed > poll_timeout:
                        raise TimeoutError("Soniox async transcription polling timed out")

                    async with self.session.get(
                        f"{self.BASE_URL}/transcriptions/{transcription_id}",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as r:
                        r.raise_for_status()
                        status_payload = await r.json()
                        status = (status_payload.get("status") or "").lower()
                        if status in done_statuses:
                            break
                        if status in error_statuses:
                            raise RuntimeError(status_payload.get("error_message", "Soniox async error"))

                    poll_count += 1

                    # OPTIMIZED: Exponential backoff polling with adaptive delays
                    # Fast polling for short audio (0.5s), slower for long audio (up to 5s)
                    # Reduces API calls by ~80% for long audio (600 polls → 120 polls)
                    if poll_start_time is None:
                        poll_start_time = asyncio.get_running_loop().time()

                    elapsed = asyncio.get_running_loop().time() - poll_start

                    if elapsed < 10:
                        # Fast polling for quick jobs (0-10s)
                        delay = 0.5
                    elif elapsed < 30:
                        # Medium polling for short-medium jobs (10-30s)
                        delay = 1.0
                    elif elapsed < 120:
                        # Longer audio, moderate polling (30-120s)
                        delay = 2.0
                    else:
                        # Very long audio, slow down polling (120s+)
                        delay = 5.0

                    # Log periodically for debugging
                    if poll_count % 10 == 0:
                        elapsed = int(asyncio.get_running_loop().time() - poll_start)
                        logger.debug(f"Soniox async polling: {elapsed}s elapsed, delay={delay}s")

                    await asyncio.sleep(delay)

            self._report_progress("Retrieving transcript...")
            async with self.session.get(
                f"{self.BASE_URL}/transcriptions/{transcription_id}/transcript",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r3:
                r3.raise_for_status()
                transcript_payload = await r3.json()
                tokens = transcript_payload.get("tokens")
                token_list = tokens if isinstance(tokens, list) else []
                text = ""
                if self.enable_speaker_diarization and token_list:
                    text = _format_speaker_transcript_tokens(
                        [token for token in token_list if isinstance(token, dict)]
                    )
                if not text:
                    text = transcript_payload.get("text", "")
                if text:
                    logger.info(f"Soniox async transcription completed ({len(text)} chars)")
                self._report_progress("Completed")
                
                # Clean up: delete file and transcription from Soniox to avoid hitting file limits
                await self._cleanup_soniox_resources(file_id, transcription_id, headers)
                
                return text

        except Exception as e:
            logger.error(f"Soniox async transcription failed: {e}")
            # Try to clean up even on failure
            try:
                if 'file_id' in dir():
                    await self._cleanup_soniox_resources(file_id, transcription_id if 'transcription_id' in dir() else None, headers)
            except Exception:
                pass
            raise

    async def _encode_audio(self, audio_bytes: bytes, prefer_webm: bool = True):
        """
        Encode raw PCM to WebM/Opus (preferred) or WAV.

        Uses temporary file for WebM encoding because WebM containers require
        seekable output to write duration metadata in the header. Piping to
        stdout produces files with missing/zero duration that Soniox rejects
        with 400 Bad Request.
        """

        sr = self._sample_rate or 16000
        ch = self._channels or 1
        
        # Calculate audio duration from PCM data (16-bit = 2 bytes per sample)
        bytes_per_sample = 2 * ch  # int16 * channels
        num_samples = len(audio_bytes) // bytes_per_sample
        duration_secs = num_samples / sr

        ffmpeg = find_media_tool("ffmpeg") if prefer_webm else None
        if ffmpeg:
            # Use temporary files for WebM - required for proper duration metadata
            # WebM/Matroska containers need seekable output to write duration to header
            tmp_input = None
            tmp_output_path = None
            try:
                # Write PCM to temp input file
                tmp_input = tempfile.NamedTemporaryFile(suffix=".pcm", delete=False)
                tmp_input.write(audio_bytes)
                tmp_input.close()
                
                # Create temp output file path
                tmp_output_path = tmp_input.name.replace(".pcm", ".webm")
                
                # Two-pass encoding: input file → output file (allows FFmpeg to write duration)
                cmd = [
                    ffmpeg, "-y",            # Overwrite output
                    "-f", "s16le",           # Input format: signed 16-bit little-endian PCM
                    "-ar", str(sr),          # Input sample rate
                    "-ac", str(ch),          # Input channels
                    "-i", tmp_input.name,    # Read from temp file
                    "-c:a", "libopus",       # Encode to Opus
                    "-ar", "48000",          # Output sample rate (Opus standard)
                    "-ac", "1",              # Mono output
                    "-b:a", "32k",           # Bitrate
                    "-application", "voip",  # Optimize for voice
                    tmp_output_path          # Write to temp file (allows seeking for duration)
                ]

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **hidden_subprocess_kwargs(),
                )

                _, stderr = await proc.communicate()

                if proc.returncode != 0:
                    raise RuntimeError(f"FFmpeg encoding failed: {stderr.decode()}")

                # Read the WebM file with proper duration metadata
                with open(tmp_output_path, "rb") as f:
                    webm_bytes = f.read()

                if Config.DEBUG:
                    logger.info(f"Encoded PCM to WebM via temp file ({len(webm_bytes)} bytes, {duration_secs:.2f}s)")

                return webm_bytes, "audio/webm", "audio.webm"

            except Exception as e:
                logger.warning(f"WebM encode failed ({e}); falling back to WAV")
            finally:
                # Clean up temp files
                try:
                    if tmp_input and os.path.exists(tmp_input.name):
                        os.unlink(tmp_input.name)
                    if tmp_output_path and os.path.exists(tmp_output_path):
                        os.unlink(tmp_output_path)
                except Exception:
                    pass

        # WAV fallback (already in-memory)
        buf = io.BytesIO()
        with contextlib.closing(wave.open(buf, "wb")) as wf:
            wf.setnchannels(ch)
            wf.setsampwidth(2)  # int16
            wf.setframerate(sr)
            wf.writeframes(audio_bytes)
        return buf.getvalue(), "audio/wav", "audio.wav"

    async def _cleanup_soniox_resources(self, file_id: str, transcription_id: str | None, headers: dict):
        """Delete file and transcription from Soniox to avoid hitting file limits."""
        # Delete transcription first (it references the file)
        if transcription_id:
            try:
                async with self.session.delete(
                    f"{self.BASE_URL}/transcriptions/{transcription_id}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 204, 404):
                        logger.debug(f"Deleted Soniox transcription {transcription_id}")
                    else:
                        logger.warning(f"Failed to delete transcription {transcription_id}: {resp.status}")
            except Exception as e:
                logger.warning(f"Error deleting transcription: {e}")
        
        # Delete the uploaded file
        if file_id:
            try:
                async with self.session.delete(
                    f"{self.BASE_URL}/files/{file_id}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 204, 404):
                        logger.debug(f"Deleted Soniox file {file_id}")
                    else:
                        logger.warning(f"Failed to delete file {file_id}: {resp.status}")
            except Exception as e:
                logger.warning(f"Error deleting file: {e}")

# ============================================================================
# STT Services are imported LAZILY inside _create_stt_service() to reduce
# app startup time by ~500-800ms. Each service is only imported when used.
# ============================================================================

from src.config import Config
from src.core.provider_capabilities import injects_immediately_in_live_mode
from src.audio_devices import (
    normalize_device_name,
    resolve_input_microphone_device,
)
from src.injector import TextInjector
from src.microphone import MicrophoneInput
from src.audio_file_input import FfmpegAudioFileInput
from src.mistral_stt import (
    MistralAsyncProcessor,
    MistralRealtimeSTTService,
    format_mistral_segments_with_speakers,
    transcribe_with_mistral,
)
from src.smallest_stt import (
    SmallestAsyncProcessor,
    SmallestRealtimeSTTService,
    smallest_transcript_payload_to_text,
    transcribe_with_smallest_pre_recorded,
)
from src.azure_mai_stt import (
    AzureMaiTranscribeSTTService,
    azure_mai_content_type,
    azure_mai_transcript_payload_to_text,
    prepared_azure_mai_audio_file,
    transcribe_with_azure_mai,
    validate_azure_mai_region,
)
from src.assemblyai_async_stt import (
    AssemblyAIUniversal3ProAsyncProcessor,
    assemblyai_universal_35_language_code,
    assemblyai_transcript_payload_to_text,
    build_keyterms_from_vocab,
    transcribe_with_assemblyai_pre_recorded,
)
from src.gladia_stt import (
    gladia_transcript_payload_to_text,
    transcribe_with_gladia_pre_recorded,
)
from src.cloud_async_stt import (
    DeepgramAsyncProcessor,
    GeminiAsyncProcessor,
    GladiaAsyncProcessor,
    OpenAIAsyncProcessor,
    SpeechmaticsAsyncProcessor,
    deepgram_transcript_payload_to_text,
    gemini_transcript_payload_to_text,
    openai_transcript_payload_to_text,
    speechmatics_transcript_payload_to_text,
    transcribe_with_deepgram_pre_recorded,
    transcribe_with_gemini_audio,
    transcribe_with_openai_audio_transcription,
    transcribe_with_speechmatics_batch,
)

LANGUAGE_MAP = {
    "auto": None,
    "en": Language.EN,
    "de": Language.DE,
    "fr": Language.FR,
    "es": Language.ES,
    "it": Language.IT,
    "pt": Language.PT,
    "nl": Language.NL,
}

def _selected_language():
    config_lang = (Config.LANGUAGE or "").strip().lower().replace("_", "-")
    lang = LANGUAGE_MAP.get(config_lang) or LANGUAGE_MAP.get(config_lang.split("-", 1)[0])
    return lang if lang else None


def _selected_language_code() -> str | None:
    language = _selected_language()
    if language is None:
        return None
    value = getattr(language, "value", language)
    return str(value).split("-", 1)[0] or None


def _filter_supported_kwargs(callable_obj: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in signature.parameters}


def _assemblyai_realtime_legacy_speech_model(configured_model: str | None, language_code: str | None) -> str:
    model = (configured_model or "").strip()
    if model in {"universal-streaming-english", "universal-streaming-multilingual"}:
        return model

    fallback = "universal-streaming-english" if (language_code or "").lower() == "en" else "universal-streaming-multilingual"
    logger.info(
        "Mapping AssemblyAI realtime model '{}' to Pipecat legacy speech_model '{}'",
        model or "<empty>",
        fallback,
    )
    return fallback


def _mistral_segmented_live_model() -> str:
    model = (Config.MISTRAL_RT_MODEL or "").strip()
    if "realtime" in model.lower():
        fallback = (Config.MISTRAL_ASYNC_MODEL or "voxtral-mini-2602").strip()
        logger.info(
            "Mistral segmented live path cannot use realtime-only model '{}'; using '{}' instead",
            model,
            fallback,
        )
        return fallback
    return model or (Config.MISTRAL_ASYNC_MODEL or "voxtral-mini-2602")


def _create_assemblyai_realtime_service(
    *,
    api_key: str,
    enable_speaker_diarization: bool,
) -> object:
    module = import_provider_runtime_module("assemblyai_realtime", "pipecat.services.assemblyai.stt")
    service_cls = getattr(module, "AssemblyAISTTService", None)
    if service_cls is None:
        raise RuntimeError("AssemblyAI Pipecat STT service is unavailable in this build.")

    settings_cls = getattr(service_cls, "Settings", None)
    language_code = assemblyai_universal_35_language_code(Config.LANGUAGE)
    keyterms = build_keyterms_from_vocab(Config.CUSTOM_VOCAB)

    if settings_cls is not None:
        settings_candidates = {"model": Config.ASSEMBLYAI_RT_MODEL}
        if language_code:
            settings_candidates["language_code"] = language_code
        if keyterms:
            settings_candidates["keyterms_prompt"] = keyterms[:100]
        if enable_speaker_diarization:
            settings_candidates["speaker_labels"] = True
        settings_kwargs = _filter_supported_kwargs(settings_cls, settings_candidates)
        if keyterms and "keyterms_prompt" not in settings_kwargs:
            settings_kwargs.update(
                _filter_supported_kwargs(
                    settings_cls,
                    {"prompt": "Likely vocabulary: " + ", ".join(keyterms[:100])},
                )
            )
        try:
            settings = settings_cls(**settings_kwargs)
        except TypeError as exc:
            logger.warning("AssemblyAI Settings rejected available arguments ({}); trying legacy connection_params", exc)
        else:
            service_candidates: dict[str, Any] = {
                "api_key": api_key,
                "settings": settings,
                "vad_force_turn_endpoint": True,
            }
            if language_code and "language_code" not in settings_kwargs:
                service_candidates["language"] = _selected_language()
            return service_cls(**_filter_supported_kwargs(service_cls, service_candidates))

    params_cls = getattr(module, "AssemblyAIConnectionParams", None)
    if params_cls is None:
        raise RuntimeError(
            "AssemblyAI realtime transcription requires a Pipecat build with "
            "AssemblyAISTTService.Settings support."
        )

    try:
        params = params_cls(
            **_filter_supported_kwargs(
                params_cls,
                {
                    "sample_rate": Config.SAMPLE_RATE,
                    "keyterms_prompt": keyterms[:100] or None,
                    "speech_model": _assemblyai_realtime_legacy_speech_model(
                        Config.ASSEMBLYAI_RT_MODEL,
                        language_code,
                    ),
                },
            )
        )
    except Exception as exc:
        raise RuntimeError(
            "AssemblyAI Universal-3.5 Pro realtime requires a newer Pipecat "
            "AssemblyAI STT service. Update the bundled Pipecat runtime."
        ) from exc

    service_candidates = {
        "api_key": api_key,
        "connection_params": params,
        "vad_force_turn_endpoint": True,
    }
    selected_language = _selected_language()
    if selected_language is not None:
        service_candidates["language"] = selected_language
    return service_cls(**_filter_supported_kwargs(service_cls, service_candidates))


def _load_soniox_realtime_classes():
    global SonioxSTTService, SonioxInputParams, SonioxContextObject, _SONIOX_IMPORT_ERROR
    if SonioxSTTService:
        return SonioxSTTService, SonioxInputParams, SonioxContextObject
    module = import_provider_runtime_module("soniox", "pipecat.services.soniox.stt")
    SonioxSTTService = getattr(module, "SonioxSTTService", None)
    SonioxInputParams = getattr(module, "SonioxInputParams", None)
    SonioxContextObject = getattr(module, "SonioxContextObject", None)
    _SONIOX_IMPORT_ERROR = None
    return SonioxSTTService, SonioxInputParams, SonioxContextObject


def _normalize_device_name(name: str) -> str:
    return normalize_device_name(name)


_MIC_DEVICE_CACHE_LOCK = threading.Lock()
_MIC_DEVICE_CACHE: dict[tuple[int, str, str, int, int], tuple[float, str]] = {}


def invalidate_mic_device_resolution_cache() -> None:
    """Clear cached microphone-name-to-index resolutions."""
    with _MIC_DEVICE_CACHE_LOCK:
        _MIC_DEVICE_CACHE.clear()


def _mic_device_resolution_cache_ttl() -> float:
    try:
        return max(0.0, float(os.getenv("SCRIBER_MIC_DEVICE_CACHE_TTL_SEC", "10.0") or 10.0))
    except Exception:
        return 10.0


def _mic_device_cache_key(sd_module, device_name: str, sample_rate: int, channels: int) -> tuple[int, str, str, int, int]:
    return (
        id(sd_module),
        str(device_name or "default"),
        str(getattr(Config, "FAVORITE_MIC", "") or ""),
        int(sample_rate),
        int(channels),
    )


def _get_cached_mic_resolution(key: tuple[int, str, str, int, int]) -> str | None:
    ttl = _mic_device_resolution_cache_ttl()
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _MIC_DEVICE_CACHE_LOCK:
        cached = _MIC_DEVICE_CACHE.get(key)
        if not cached:
            return None
        cached_at, resolved = cached
        if now - cached_at > ttl:
            _MIC_DEVICE_CACHE.pop(key, None)
            return None
        return resolved


def _set_cached_mic_resolution(key: tuple[int, str, str, int, int], resolved: str) -> None:
    if _mic_device_resolution_cache_ttl() <= 0:
        return
    with _MIC_DEVICE_CACHE_LOCK:
        _MIC_DEVICE_CACHE[key] = (time.monotonic(), resolved)


def _resolve_mic_device(device_name: str) -> str:
    """Resolve a saved device name to the current device index.
    
    The mic device setting now stores the device NAME (stable across reboots)
    instead of the index (which can change). This function looks up the current
    index for the named device, falling back to 'default' if not found.
    
    If a FAVORITE_MIC is set and available, it will be used instead of the
    selected device. This allows users to have a preferred mic that is
    automatically used whenever it's connected.
    """
    try:
        import sounddevice as sd
    except Exception as exc:
        logger.warning(f"Sounddevice unavailable while resolving microphone ({exc}); using default")
        return "default"
    
    sample_rate = int(getattr(Config, "SAMPLE_RATE", 16000) or 16000)
    requested_channels = max(1, int(getattr(Config, "CHANNELS", 1) or 1))
    cache_key = _mic_device_cache_key(sd, device_name, sample_rate, requested_channels)
    cached = _get_cached_mic_resolution(cache_key)
    if cached:
        return cached

    def remember(resolved: str) -> str:
        _set_cached_mic_resolution(cache_key, resolved)
        return resolved
    return remember(
        resolve_input_microphone_device(
            sd,
            device_name=device_name,
            favorite_name=getattr(Config, "FAVORITE_MIC", "") or "",
            sample_rate=sample_rate,
            channels=requested_channels,
            logger=logger,
        )
    )


class ConnectionErrorHandlerProcessor(FrameProcessor):
    """Records provider errors and triggers capture cleanup for connection failures."""

    def __init__(
        self,
        on_error: Optional[Callable[[str], None]] = None,
        cleanup_callback: Optional[Callable[[], None]] = None,
        on_provider_error: Optional[Callable[[str], None]] = None,
    ):
        super().__init__()
        self.on_error = on_error
        self.cleanup_callback = cleanup_callback
        self.on_provider_error = on_provider_error
        self._error_triggered = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        # Catch STT ErrorFrames. All provider errors are terminal for the active
        # live recording; connection failures additionally stop capture early.
        if isinstance(frame, ErrorFrame) and not self._error_triggered:
            error_msg = str(frame.error) if hasattr(frame, 'error') else str(frame)
            error_lower = error_msg.lower()

            if self.on_provider_error:
                try:
                    self.on_provider_error(error_msg)
                except Exception as e:
                    logger.warning(f"Provider error callback failed: {e}")

            # Check for connection-related errors
            is_connection_error = (
                "timeout" in error_lower
                or "handshake" in error_lower
                or "connection" in error_lower
                or "websocket" in error_lower
            )

            if is_connection_error:
                self._error_triggered = True
                logger.error(f"Connection error detected: {error_msg}")

                # Trigger error callback
                if self.on_error:
                    try:
                        self.on_error(error_msg)
                    except Exception as e:
                        logger.warning(f"Error callback failed: {e}")

                # Trigger cleanup (stops microphone to prevent frame flood)
                if self.cleanup_callback:
                    try:
                        self.cleanup_callback()
                    except Exception as e:
                        logger.warning(f"Cleanup callback failed: {e}")

                # Don't propagate connection errors downstream
                return

        await self.push_frame(frame, direction)


class PipecatVadSpeechObserver(FrameProcessor):
    """Tracks Pipecat VAD speech events for diagnostics and silent-session skips."""

    def __init__(self, *, enabled: bool):
        super().__init__()
        self.enabled = bool(enabled)
        self._started_count = 0
        self._stopped_count = 0
        self._audio_frame_count = 0
        self._speaking = False
        self._speech_observed = False
        self._last_started_at = 0.0
        self._last_stopped_at = 0.0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if self.enabled:
            now = time.monotonic()
            if isinstance(frame, InputAudioRawFrame):
                self._audio_frame_count += 1
            elif isinstance(frame, (VADUserStartedSpeakingFrame, UserStartedSpeakingFrame)):
                self._started_count += 1
                self._speaking = True
                self._speech_observed = True
                self._last_started_at = now
            elif isinstance(frame, (VADUserStoppedSpeakingFrame, UserStoppedSpeakingFrame)):
                self._stopped_count += 1
                self._speaking = False
                self._last_stopped_at = now

        await self.push_frame(frame, direction)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "enabled": self.enabled,
            "speechObserved": bool(self._speech_observed),
            "speaking": bool(self._speaking),
            "audioFrameCount": int(self._audio_frame_count),
            "speechStartedCount": int(self._started_count),
            "speechStoppedCount": int(self._stopped_count),
            "lastSpeechStartedAgoSeconds": (
                round(now - self._last_started_at, 3)
                if self._last_started_at > 0
                else None
            ),
            "lastSpeechStoppedAgoSeconds": (
                round(now - self._last_stopped_at, 3)
                if self._last_stopped_at > 0
                else None
            ),
        }


class SegmentedSTTRecordingGate(FrameProcessor):
    """Controls whether Pipecat VAD cuts STT audio into multiple live segments.

    HTTP-style Pipecat STT services such as Groq/OpenAI/ElevenLabs/Mistral use
    UserStarted/UserStopped frames to decide when to upload buffered audio. By
    default Scriber keeps one recording-wide segment so the stop hotkey
    transcribes the whole dictation. VAD frames are still observed before this
    gate for silent-session skips.
    """

    def __init__(self, *, vad_segmentation_enabled: bool):
        super().__init__()
        self.vad_segmentation_enabled = bool(vad_segmentation_enabled)
        self._whole_recording_open = False
        self._whole_recording_closed = False
        self._vad_user_speaking = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, StartFrame):
            self._whole_recording_open = False
            self._whole_recording_closed = False
            self._vad_user_speaking = False
            await self.push_frame(frame, direction)
            if not self.vad_segmentation_enabled:
                self._whole_recording_open = True
                logger.debug("Opening recording-wide segment for segmented STT service")
                await self.push_frame(UserStartedSpeakingFrame(), direction)
            return

        if self.vad_segmentation_enabled:
            if isinstance(frame, UserStartedSpeakingFrame):
                self._vad_user_speaking = True
            elif isinstance(frame, UserStoppedSpeakingFrame):
                self._vad_user_speaking = False
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame)):
            # VAD is still used upstream for silence detection, but it must not
            # split HTTP upload-based STT providers unless the user opted in.
            return

        if isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            await self.flush_segment(direction=direction)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    async def flush_segment(self, *, direction=FrameDirection.DOWNSTREAM) -> bool:
        if self.vad_segmentation_enabled:
            if not self._vad_user_speaking:
                return False
            self._vad_user_speaking = False
            logger.debug("Flushing active VAD segment for segmented STT service")
            await self.push_frame(UserStoppedSpeakingFrame(), direction)
            return True

        if self._whole_recording_closed:
            return False
        if not self._whole_recording_open:
            self._whole_recording_open = True
            await self.push_frame(UserStartedSpeakingFrame(), direction)
        self._whole_recording_closed = True
        logger.debug("Closing recording-wide segment for segmented STT service")
        await self.push_frame(UserStoppedSpeakingFrame(), direction)
        return True


class TranscriptionCallbackProcessor(FrameProcessor):
    """Emits interim/final transcription updates via a lightweight callback."""

    def __init__(
        self,
        on_transcription: Optional[Callable[[str, bool], None]],
        *,
        enable_speaker_diarization: bool = False,
        on_final_transcription: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self.on_transcription = on_transcription
        self.enable_speaker_diarization = bool(enable_speaker_diarization)
        self.on_final_transcription = on_final_transcription

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        cb = self.on_transcription
        try:
            if isinstance(frame, InterimTranscriptionFrame):
                if frame.text and cb:
                    cb(frame.text, False)
            elif isinstance(frame, TranscriptionFrame):
                if self.enable_speaker_diarization:
                    text = _diarized_text_from_frame_result(frame.result) or frame.text
                else:
                    text = frame.text
                if text:
                    if self.on_final_transcription:
                        self.on_final_transcription()
                    if cb:
                        cb(text, True)
        except Exception as e:
            logger.error(f"TranscriptionCallback error: {e}")

        await self.push_frame(frame, direction)

class ScriberPipeline:
    def __init__(
        self,
        service_name=Config.DEFAULT_STT_SERVICE,
        on_status_change=None,
        on_audio_level=None,
        on_transcription: Optional[Callable[[str, bool], None]] = None,
        on_text_injected: Optional[Callable[[str], None]] = None,
        on_injection_marker: Optional[Callable[..., None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
        on_mic_ready: Optional[Callable[[], None]] = None,
        on_last_audio_chunk_sent: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        mic_prewarm_manager=None,
        enable_speaker_diarization: bool = False,
        text_injection_enabled: bool = True,
    ):
        self.service_name = service_name
        self.on_status_change = on_status_change
        self.on_audio_level = on_audio_level
        self.on_transcription = on_transcription
        self.on_text_injected = on_text_injected
        self.on_injection_marker = on_injection_marker
        self.on_progress = on_progress
        self.on_mic_ready = on_mic_ready
        self.on_last_audio_chunk_sent = on_last_audio_chunk_sent
        self.on_error = on_error
        self.mic_prewarm_manager = mic_prewarm_manager
        self.enable_speaker_diarization = bool(enable_speaker_diarization)
        self.text_injection_enabled = bool(text_injection_enabled)
        self.pipeline = None
        self.task = None
        self.runner = None
        self.audio_input = None
        self._vad_observer: PipecatVadSpeechObserver | None = None
        self.is_active = False
        self._terminal_error: str | None = None
        self._audio_cleanup_lock = asyncio.Lock()
        self._start_done = asyncio.Event()
        self._start_done.set()
        self._final_transcription_received = asyncio.Event()

    def _record_terminal_error(self, error_msg: str) -> None:
        normalized = str(error_msg or "").strip()
        if normalized and not self._terminal_error:
            self._terminal_error = normalized

    def _mark_final_transcription_received(self) -> None:
        self._final_transcription_received.set()

    @staticmethod
    def _soniox_realtime_stop_final_timeout_seconds() -> float:
        raw = os.getenv("SCRIBER_SONIOX_RT_STOP_FINAL_TIMEOUT_SECONDS", "3.0")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 3.0
        return min(10.0, max(0.25, value))

    async def _wait_for_soniox_realtime_final_or_receive_done(
        self,
        receive_task: asyncio.Task | None,
        *,
        timeout_seconds: float,
    ) -> str:
        if self._final_transcription_received.is_set():
            return "final"

        if receive_task and receive_task.done():
            try:
                await receive_task
            except asyncio.CancelledError:
                return "cancelled"
            except Exception as exc:
                logger.debug(f"Soniox receive task completed with error: {exc}")
                return "error"
            return "receive_done"

        final_task = asyncio.create_task(
            self._final_transcription_received.wait(),
            name="soniox_rt_final_transcription_wait",
        )
        waitables: set[asyncio.Future] = {final_task}
        receive_wait: asyncio.Future | None = None
        if receive_task is not None:
            receive_wait = asyncio.shield(receive_task)
            waitables.add(receive_wait)

        try:
            done, _pending = await asyncio.wait(
                waitables,
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not final_task.done():
                final_task.cancel()
                await asyncio.gather(final_task, return_exceptions=True)

        if final_task in done and self._final_transcription_received.is_set():
            return "final"
        if receive_wait is not None and receive_wait in done:
            try:
                await receive_wait
            except asyncio.CancelledError:
                return "cancelled"
            except Exception as exc:
                logger.debug(f"Soniox receive task completed with error: {exc}")
                return "error"
            return "receive_done"
        if self._final_transcription_received.is_set():
            return "final"
        return "timeout"

    async def _cleanup_audio_input(self) -> None:
        audio_input = self.audio_input
        if not audio_input:
            return
        has_prewarm_manager = self.mic_prewarm_manager is not None
        resume_prewarm = bool(Config.MIC_ALWAYS_ON and has_prewarm_manager)
        prewarm_resumed_before_stop = False
        async with self._audio_cleanup_lock:
            audio_input = self.audio_input
            if not audio_input:
                return
            if has_prewarm_manager:
                try:
                    detach_active_capture = getattr(
                        self.mic_prewarm_manager,
                        "detach_active_capture",
                        None,
                    )
                    if callable(detach_active_capture):
                        await asyncio.to_thread(detach_active_capture, None)
                    if resume_prewarm:
                        prewarm_resumed_before_stop = bool(
                            await asyncio.to_thread(
                                self.mic_prewarm_manager.resume_after_active_capture
                            )
                        )
                except Exception as exc:
                    logger.debug(f"Mic prewarm pre-resume before audio cleanup warning: {exc}")
            try:
                # Pipeline instances are per-session, so keep_alive streams cannot
                # be safely reused here. Always close to avoid orphaned PortAudio
                # resources; a real always-on mic needs an app-level manager.
                await audio_input.stop(EndFrame(), close_stream=True)
            except Exception as exc:
                logger.debug(f"Audio input cleanup warning: {exc}")
            finally:
                self.audio_input = None
        if resume_prewarm and not prewarm_resumed_before_stop:
            try:
                await asyncio.to_thread(self.mic_prewarm_manager.resume_after_active_capture)
            except Exception as exc:
                logger.debug(f"Mic prewarm resume after audio cleanup warning: {exc}")

    async def _stop_audio_capture_for_segmented_finalization(self) -> None:
        """Stop mic capture without ending the Pipecat pipeline.

        File/HTTP-style segmented STT providers finalize when they receive a
        UserStoppedSpeakingFrame. If we end the pipeline first, the downstream
        TextInjector can flush before the provider has emitted a final
        TranscriptionFrame. This helper stops the physical mic and drains queued
        audio frames while keeping the downstream pipeline open for finalization.
        """
        audio_input = self.audio_input
        if not audio_input:
            return
        stop_capture = getattr(audio_input, "stop_capture_for_finalization", None)
        if callable(stop_capture):
            try:
                await stop_capture(close_stream=True)
            except Exception as exc:
                logger.debug(f"Segmented STT capture-stop warning: {exc}")
        try:
            audio_queue = getattr(audio_input, "_audio_in_queue", None)
            if audio_queue is not None and callable(getattr(audio_queue, "join", None)):
                await asyncio.wait_for(audio_queue.join(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.debug("Timed out waiting for segmented STT audio queue to drain")
        except Exception as exc:
            logger.debug(f"Segmented STT audio queue drain warning: {exc}")

    def _segmented_stt_stop_final_timeout_seconds(self) -> float:
        return _env_float(
            "SCRIBER_SEGMENTED_STT_STOP_FINAL_TIMEOUT_SEC",
            20.0,
            minimum=0.5,
            maximum=120.0,
        )

    async def _wait_for_segmented_stt_final_or_done(self, *, timeout_seconds: float) -> str:
        if self._final_transcription_received.is_set():
            return "final"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        while True:
            if self._terminal_error:
                return "error"
            if self.task and self.task.has_finished():
                return "finished"
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "timeout"
            try:
                await asyncio.wait_for(
                    self._final_transcription_received.wait(),
                    timeout=min(0.25, remaining),
                )
            except asyncio.TimeoutError:
                continue
            if self._final_transcription_received.is_set():
                return "final"

    def audio_diagnostics(self) -> dict[str, Any] | None:
        audio_input = self.audio_input
        vad_snapshot = None
        if self._vad_observer is not None:
            try:
                vad_snapshot = self._vad_observer.snapshot()
            except Exception as exc:
                vad_snapshot = {"enabled": True, "available": False, "error": str(exc)}
        if not audio_input:
            if vad_snapshot is not None:
                return {"available": False, "pipecatVad": vad_snapshot}
            return None
        snapshot_fn = getattr(audio_input, "diagnostic_snapshot", None)
        if not callable(snapshot_fn):
            base_snapshot = {"available": False}
        else:
            try:
                base_snapshot = snapshot_fn()
            except Exception as exc:
                base_snapshot = {"available": False, "error": str(exc)}
        if vad_snapshot is not None:
            base_snapshot["pipecatVad"] = vad_snapshot
            if bool(vad_snapshot.get("speechObserved")):
                base_snapshot["speechObserved"] = True
                base_snapshot["speechObservedByVad"] = True
        return base_snapshot

    def ensure_audio_health(
        self,
        *,
        reason: str = "watchdog",
        max_callback_gap_seconds: float | None = None,
    ) -> bool:
        audio_input = self.audio_input
        if not audio_input:
            return False
        ensure = getattr(audio_input, "ensure_stream_health", None)
        if not callable(ensure):
            return True
        return bool(
            ensure(
                reason=reason,
                max_callback_gap_seconds=max_callback_gap_seconds,
            )
        )

    def _iter_pipeline_processors(self) -> list[Any]:
        pipeline = self.pipeline
        if not pipeline:
            return []
        processors: list[Any] = []
        seen: set[int] = set()
        for attr in ("processors", "steps", "_processors"):
            values = getattr(pipeline, attr, None)
            if not values:
                continue
            try:
                iterator = list(values)
            except TypeError:
                continue
            for processor in iterator:
                identity = id(processor)
                if identity not in seen:
                    seen.add(identity)
                    processors.append(processor)
        return processors

    async def _flush_segmented_stt_buffers(self) -> bool:
        flushed = False
        for processor in self._iter_pipeline_processors():
            flush = getattr(processor, "flush_segment", None)
            if callable(flush):
                try:
                    flushed = bool(await flush(direction=FrameDirection.DOWNSTREAM)) or flushed
                except Exception as exc:
                    logger.debug(f"Segmented STT gate flush warning: {exc}")
        if flushed:
            return True

        if not self.pipeline:
            return False
        try:
            if any(isinstance(step, SegmentedSTTService) for step in self._iter_pipeline_processors()):
                logger.debug("Forcing segmented STT flush on stop")
                await self.pipeline.push_frame(
                    UserStoppedSpeakingFrame(),
                    direction=FrameDirection.DOWNSTREAM,
                )
                return True
        except Exception as exc:
            logger.debug(f"Segmented STT fallback flush warning: {exc}")
        return False

    async def _flush_live_vad_finalization_turn(self) -> bool:
        """Finalize a live streaming turn when the hotkey stops while speech is active."""
        if not self.pipeline or not _live_service_needs_local_vad(self.service_name):
            return False
        if self._has_segmented_stt_buffers():
            return False

        vad_snapshot = None
        if self._vad_observer is not None:
            try:
                vad_snapshot = self._vad_observer.snapshot()
            except Exception as exc:
                logger.debug(f"Live VAD finalization snapshot warning: {exc}")

        if vad_snapshot is not None:
            if not vad_snapshot.get("speechObserved"):
                return False
            if (
                not vad_snapshot.get("speaking")
                and int(vad_snapshot.get("speechStoppedCount") or 0) > 0
            ):
                return False

        try:
            logger.debug("Forcing live STT turn finalization on hotkey stop")
            await self.pipeline.push_frame(
                UserStoppedSpeakingFrame(),
                direction=FrameDirection.DOWNSTREAM,
            )
            return True
        except Exception as exc:
            logger.debug(f"Live STT finalization flush warning: {exc}")
            return False

    def _has_segmented_stt_buffers(self) -> bool:
        return any(
            isinstance(processor, SegmentedSTTService)
            or isinstance(processor, SegmentedSTTRecordingGate)
            for processor in self._iter_pipeline_processors()
        )

    def _mark_provider_terminal_transcription_skip(self) -> None:
        for processor in self._iter_pipeline_processors():
            if hasattr(processor, "_buffer_size") or callable(getattr(processor, "_reset_buffer", None)):
                setattr(processor, "_skip_terminal_transcription", True)

    async def cancel_silent_recording(self, timeout_secs: float = 5.0) -> None:
        """Cancel a live session without asking buffered async providers to transcribe silence."""
        logger.info("Cancelling silent live recording without provider finalization")
        self._mark_provider_terminal_transcription_skip()
        if self.on_status_change:
            self.on_status_change("No speech detected")

        await self._cleanup_audio_input()

        if self.task and not self.task.has_finished():
            try:
                try:
                    await self.task.cancel(reason="silent recording")
                except TypeError:
                    await self.task.cancel()
            except Exception as exc:
                logger.debug(f"Silent recording task cancel warning: {exc}")
        if self.runner:
            try:
                await self.runner.cancel()
            except Exception as exc:
                logger.debug(f"Silent recording runner cancel warning: {exc}")

        try:
            await asyncio.wait_for(self._start_done.wait(), timeout=timeout_secs)
        except asyncio.TimeoutError:
            logger.debug("Timed out waiting for silent recording pipeline cancellation")
        self.is_active = False

    def _create_stt_service(self, session: aiohttp.ClientSession, *, for_file: bool = False):
        """Create the appropriate STT service based on configuration.
        
        STT services are imported lazily here to reduce app startup time.
        This saves ~500-800ms by not loading all service dependencies at launch.
        """

        def _get_api_key(service):
            return Config.get_api_key(service)

        if self.service_name in ("soniox", "soniox_async"):
            if not _get_api_key("soniox"): raise ValueError("Soniox API Key is missing.")
            use_async = self.service_name == "soniox_async" or Config.SONIOX_MODE == "async"
            if use_async:
                logger.info("Using Soniox async transcription mode")
                return SonioxAsyncProcessor(
                    api_key=_get_api_key("soniox"),
                    custom_vocab=Config.CUSTOM_VOCAB,
                    model=Config.SONIOX_ASYNC_MODEL,
                    session=session,
                    on_progress=self.on_progress,
                    enable_speaker_diarization=self.enable_speaker_diarization,
                )
            soniox_service_cls, soniox_input_params_cls, soniox_context_cls = _load_soniox_realtime_classes()
            if not soniox_service_cls: raise RuntimeError("SonioxSTTService not available.")
            lang_hint = _selected_language()
            # Use Soniox v5 realtime by default; SCRIBER_SONIOX_RT_MODEL remains an override.
            rt_model = Config.SONIOX_RT_MODEL
            # Build params with model and context
            if Config.CUSTOM_VOCAB and soniox_context_cls:
                terms = [t.strip() for t in Config.CUSTOM_VOCAB.split(",") if t.strip()]
                if terms:
                    logger.info(f"Applying custom vocabulary: {terms}")
                    params = soniox_input_params_cls(
                        model=rt_model,
                        context=soniox_context_cls(terms=terms),
                        language_hints=[lang_hint] if lang_hint else None,
                        enable_speaker_diarization=self.enable_speaker_diarization,
                    ) if soniox_input_params_cls else _SonioxParamsFallback(
                        context=soniox_context_cls(terms=terms),
                        enable_speaker_diarization=self.enable_speaker_diarization,
                    )
                else:
                    params = soniox_input_params_cls(
                        model=rt_model,
                        language_hints=[lang_hint] if lang_hint else None,
                        enable_speaker_diarization=self.enable_speaker_diarization,
                    ) if soniox_input_params_cls else _SonioxParamsFallback(
                        enable_speaker_diarization=self.enable_speaker_diarization,
                    )
            else:
                params = soniox_input_params_cls(
                    model=rt_model,
                    language_hints=[lang_hint] if lang_hint else None,
                    enable_speaker_diarization=self.enable_speaker_diarization,
                ) if soniox_input_params_cls else _SonioxParamsFallback(
                    enable_speaker_diarization=self.enable_speaker_diarization,
                )
            # vad_force_turn_endpoint=True disables automatic endpoint detection which would
            # otherwise close the WebSocket connection when speech pauses are detected.
            # This keeps the connection alive for the entire recording session.
            logger.info(f"Creating SonioxSTTService with vad_force_turn_endpoint=True (endpoint detection DISABLED)")
            return soniox_service_cls(api_key=_get_api_key("soniox"), params=params, vad_force_turn_endpoint=True)

        elif self.service_name in ("mistral", "mistral_async"):
            if not _get_api_key("mistral"):
                raise ValueError("Mistral API Key is missing.")

            if self.service_name == "mistral_async":
                logger.info("Using Mistral async transcription mode")
                return MistralAsyncProcessor(
                    api_key=_get_api_key("mistral"),
                    model=Config.MISTRAL_ASYNC_MODEL,
                    language=_selected_language(),
                    custom_vocab=Config.CUSTOM_VOCAB,
                    session=session,
                    on_progress=self.on_progress,
                    diarize=self.enable_speaker_diarization,
                )

            logger.info("Using Mistral realtime transcription mode")
            return MistralRealtimeSTTService(
                api_key=_get_api_key("mistral"),
                model=_mistral_segmented_live_model(),
                language=_selected_language(),
                custom_vocab=Config.CUSTOM_VOCAB,
                aiohttp_session=session,
            )

        elif self.service_name in ("smallest", "smallest_async"):
            if not _get_api_key("smallest"):
                raise ValueError("Smallest AI API Key is missing.")

            if self.service_name == "smallest_async":
                logger.info("Using Smallest AI Pulse async transcription mode")
                return SmallestAsyncProcessor(
                    api_key=_get_api_key("smallest"),
                    language=Config.LANGUAGE,
                    session=session,
                    on_progress=self.on_progress,
                    diarize=self.enable_speaker_diarization,
                )

            logger.info("Using Smallest AI Pulse realtime transcription mode")
            return SmallestRealtimeSTTService(
                api_key=_get_api_key("smallest"),
                language=Config.LANGUAGE,
                custom_vocab=Config.CUSTOM_VOCAB,
                aiohttp_session=session,
                sample_rate=Config.SAMPLE_RATE,
            )

        elif self.service_name == "assemblyai":
            if not _get_api_key("assemblyai"):
                raise ValueError("AssemblyAI API Key is missing.")
            logger.info("Using AssemblyAI Universal-3.5-Pro async transcription mode")
            return AssemblyAIUniversal3ProAsyncProcessor(
                api_key=_get_api_key("assemblyai"),
                language=Config.LANGUAGE,
                custom_vocab=Config.CUSTOM_VOCAB,
                session=session,
                on_progress=self.on_progress,
                speaker_labels=self.enable_speaker_diarization,
                model=Config.ASSEMBLYAI_ASYNC_MODEL,
            )

        elif self.service_name == "assemblyai_realtime":
            if not _get_api_key("assemblyai"):
                raise ValueError("AssemblyAI API Key is missing.")
            logger.info("Using AssemblyAI Universal-3.5-Pro realtime transcription mode")
            return _create_assemblyai_realtime_service(
                api_key=_get_api_key("assemblyai"),
                enable_speaker_diarization=self.enable_speaker_diarization,
            )
        
        elif self.service_name == "google":
            # Lazy import - only loaded when Google is used
            module = import_provider_runtime_module("google", "pipecat.services.google.stt")
            return module.GoogleSTTService()

        elif self.service_name == "gemini_stt":
            api_key = Config.get_api_key("gemini_stt")
            if not api_key:
                raise ValueError("Gemini API key is missing.")
            logger.info("Using Gemini API audio transcription mode")
            return GeminiAsyncProcessor(
                api_key=api_key,
                language=Config.LANGUAGE,
                custom_vocab=Config.CUSTOM_VOCAB,
                session=session,
                on_progress=self.on_progress,
                diarize=self.enable_speaker_diarization,
            )
        
        elif self.service_name == "elevenlabs":
            # Lazy import - only loaded when ElevenLabs is used
            module = import_provider_runtime_module("elevenlabs", "pipecat.services.elevenlabs.stt")
            if not _get_api_key("elevenlabs"): raise ValueError("ElevenLabs API Key is missing.")

            realtime_cls = getattr(module, "ElevenLabsRealtimeSTTService", None)
            if realtime_cls is None:
                raise RuntimeError("ElevenLabs Live requires a Pipecat build with ElevenLabsRealtimeSTTService.")
            commit_strategy = getattr(getattr(module, "CommitStrategy", object), "MANUAL", None)
            params_cls = getattr(realtime_cls, "InputParams", None)
            params = None
            lang_code = _selected_language_code()
            if params_cls is not None:
                params_candidates = {"language_code": lang_code}
                if commit_strategy is not None:
                    params_candidates["commit_strategy"] = commit_strategy
                params = params_cls(**_filter_supported_kwargs(params_cls, params_candidates))

            logger.info(f"ElevenLabs realtime STT: Using language={lang_code or 'auto-detect'}")

            return realtime_cls(
                **_filter_supported_kwargs(
                    realtime_cls,
                    {
                        "api_key": _get_api_key("elevenlabs"),
                        "model": "scribe_v2_realtime",
                        "sample_rate": Config.SAMPLE_RATE,
                        "params": params,
                    },
                )
            )
        
        elif self.service_name == "deepgram":
            # Lazy import - only loaded when Deepgram is used
            module = import_provider_runtime_module("deepgram", "pipecat.services.deepgram.stt")
            DeepgramSTTService = module.DeepgramSTTService
            if not _get_api_key("deepgram"): raise ValueError("Deepgram API Key is missing.")
            LiveOptions = getattr(module, "LiveOptions", None)
            live_options = None
            if LiveOptions:
                deepgram_options = {
                    "encoding": "linear16",
                    "sample_rate": Config.SAMPLE_RATE,
                    "channels": Config.CHANNELS,
                    "model": "nova-3",
                    "interim_results": True,
                    "smart_format": True,
                    "punctuate": True,
                    "vad_events": False,
                }
                lang_code = _selected_language_code()
                if lang_code:
                    deepgram_options["language"] = lang_code
                if self.enable_speaker_diarization:
                    deepgram_options["diarize"] = True
                live_options = LiveOptions(**deepgram_options)

            pipeline_ref = self

            class ScriberDeepgramSTTService(DeepgramSTTService):
                async def push_error(self, error_msg: str, exception=None, fatal: bool = False):
                    safe_error = _redact_provider_error_text(error_msg)
                    pipeline_ref._record_terminal_error(f"Deepgram streaming error: {safe_error}")
                    await super().push_error(
                        error_msg=error_msg,
                        exception=exception,
                        fatal=fatal,
                    )

            return ScriberDeepgramSTTService(
                api_key=_get_api_key("deepgram"),
                sample_rate=Config.SAMPLE_RATE,
                live_options=live_options,
            )

        elif self.service_name == "deepgram_async":
            if not _get_api_key("deepgram"):
                raise ValueError("Deepgram API Key is missing.")
            logger.info("Using Deepgram async pre-recorded transcription mode")
            return DeepgramAsyncProcessor(
                api_key=_get_api_key("deepgram"),
                language=Config.LANGUAGE,
                custom_vocab=Config.CUSTOM_VOCAB,
                session=session,
                on_progress=self.on_progress,
                diarize=self.enable_speaker_diarization,
            )
        
        elif self.service_name == "openai":
            # Lazy import - only loaded when OpenAI is used
            module = import_provider_runtime_module("openai", "pipecat.services.openai.stt")
            OpenAIRealtimeSTTService = getattr(module, "OpenAIRealtimeSTTService", None)
            if not _get_api_key("openai"): raise ValueError("OpenAI API Key is missing.")
            if OpenAIRealtimeSTTService is None:
                raise RuntimeError("OpenAI Realtime STT requires pipecat-ai 1.4.0 or newer.")
            settings_cls = getattr(OpenAIRealtimeSTTService, "Settings", None)
            settings = (
                settings_cls(model=Config.OPENAI_REALTIME_STT_MODEL, language=_selected_language())
                if settings_cls
                else None
            )
            return OpenAIRealtimeSTTService(
                api_key=_get_api_key("openai"),
                settings=settings,
                turn_detection=False,
            )

        elif self.service_name == "openai_async":
            if not _get_api_key("openai"):
                raise ValueError("OpenAI API Key is missing.")
            logger.info("Using OpenAI async audio transcription mode")
            return OpenAIAsyncProcessor(
                api_key=_get_api_key("openai"),
                model=Config.OPENAI_STT_MODEL,
                language=Config.LANGUAGE,
                custom_vocab=Config.CUSTOM_VOCAB,
                session=session,
                on_progress=self.on_progress,
                diarize=self.enable_speaker_diarization,
            )
        
        elif self.service_name == "azure_mai":
            api_key = _get_api_key("azure_mai")
            if not api_key:
                raise ValueError("Azure MAI Speech Key is missing.")
            logger.info("Using Microsoft MAI Transcribe Pipecat STT service")
            return AzureMaiTranscribeSTTService(
                speech_key=api_key,
                region=validate_azure_mai_region(getattr(Config, "AZURE_MAI_REGION", None)),
                language=Config.LANGUAGE,
                session=session,
                on_progress=self.on_progress,
            )
        
        elif self.service_name == "gladia":
            # Lazy import - only loaded when Gladia is used
            module = import_provider_runtime_module("gladia", "pipecat.services.gladia.stt")
            GladiaSTTService = module.GladiaSTTService
            if not _get_api_key("gladia"): raise ValueError("Gladia API Key is missing.")
            pipeline_ref = self

            class ScriberGladiaSTTService(GladiaSTTService):
                async def stop(self, frame: EndFrame):
                    await super(GladiaSTTService, self).stop(frame)
                    self._should_reconnect = False
                    try:
                        await self._send_stop_recording()
                    except Exception as exc:
                        logger.debug(f"Gladia stop_recording warning: {exc}")

                    timeout = _env_float(
                        "SCRIBER_GLADIA_STOP_FINAL_TIMEOUT_SECONDS",
                        3.0,
                        minimum=0.25,
                        maximum=10.0,
                    )
                    if not pipeline_ref._final_transcription_received.is_set():
                        try:
                            await asyncio.wait_for(
                                pipeline_ref._final_transcription_received.wait(),
                                timeout=timeout,
                            )
                            logger.debug("Gladia final transcription received before websocket cleanup")
                        except asyncio.TimeoutError:
                            logger.debug(
                                f"Timed out waiting {timeout:g}s for Gladia final transcription before websocket cleanup"
                            )

                    if getattr(self, "_connection_task", None):
                        await self.cancel_task(self._connection_task)
                        self._connection_task = None

                    await self._cleanup_connection()

            return ScriberGladiaSTTService(
                **_filter_supported_kwargs(
                    GladiaSTTService,
                    {
                        "api_key": _get_api_key("gladia"),
                        "sample_rate": Config.SAMPLE_RATE,
                        "model": "solaria-1",
                    },
                )
            )

        elif self.service_name == "gladia_async":
            if not _get_api_key("gladia"):
                raise ValueError("Gladia API Key is missing.")
            logger.info("Using Gladia pre-recorded async transcription mode")
            return GladiaAsyncProcessor(
                api_key=_get_api_key("gladia"),
                language=Config.LANGUAGE,
                custom_vocab=Config.CUSTOM_VOCAB,
                session=session,
                on_progress=self.on_progress,
                diarize=self.enable_speaker_diarization,
            )
        
        elif self.service_name == "groq":
            # Lazy import - only loaded when Groq is used
            module = import_provider_runtime_module("groq", "pipecat.services.groq.stt")
            GroqSTTService = module.GroqSTTService
            if not _get_api_key("groq"): raise ValueError("Groq API Key is missing.")
            return GroqSTTService(api_key=_get_api_key("groq"), aiohttp_session=session, language=_selected_language())
        
        elif self.service_name == "speechmatics":
            # Lazy import - only loaded when Speechmatics is used
            module = import_provider_runtime_module("speechmatics", "pipecat.services.speechmatics.stt")
            SpeechmaticsSTTService = module.SpeechmaticsSTTService
            if not _get_api_key("speechmatics"): raise ValueError("Speechmatics API Key is missing.")
            params_cls = getattr(SpeechmaticsSTTService, "InputParams", None)
            params = (
                params_cls(
                    language=Config.LANGUAGE if Config.LANGUAGE != "auto" else "en",
                    enable_diarization=self.enable_speaker_diarization,
                    speaker_active_format="[Speaker {speaker_id}]: {text}",
                    speaker_passive_format="[Speaker {speaker_id}]: {text}",
                )
                if params_cls
                else None
            )
            return SpeechmaticsSTTService(api_key=_get_api_key("speechmatics"), params=params)

        elif self.service_name == "speechmatics_async":
            if not _get_api_key("speechmatics"):
                raise ValueError("Speechmatics API Key is missing.")
            logger.info("Using Speechmatics batch async transcription mode")
            return SpeechmaticsAsyncProcessor(
                api_key=_get_api_key("speechmatics"),
                language=Config.LANGUAGE,
                custom_vocab=Config.CUSTOM_VOCAB,
                session=session,
                on_progress=self.on_progress,
                diarize=self.enable_speaker_diarization,
            )
        
        elif self.service_name == "onnx_local":
            from src.onnx_local_service import OnnxLocalBufferedSTTService, OnnxLocalSTTService
            if for_file:
                return OnnxLocalSTTService(
                    model_name=Config.ONNX_MODEL,
                    language=Config.LANGUAGE,
                    quantization=Config.ONNX_QUANTIZATION,
                )
            return OnnxLocalBufferedSTTService(
                model_name=Config.ONNX_MODEL,
                language=Config.LANGUAGE,
                quantization=Config.ONNX_QUANTIZATION,
                sample_rate=Config.SAMPLE_RATE,
                channels=Config.CHANNELS,
            )

        else:
            raise ValueError(f"Unknown service: {self.service_name}")

    async def start(self):
        # Ensure any previous task is cleaned up
        if self.task and not self.task.has_finished():
            logger.warning("Starting new pipeline but previous task still active - cancelling...")
            try:
                await self.task.cancel()
                if self.runner:
                    await self.runner.cancel()
            except Exception:
                pass
            self.task = None

        if self.is_active:
            return
        logger.info(f"Starting Scriber Pipeline with {self.service_name}")
        self._terminal_error = None
        self._vad_observer = None
        self._final_transcription_received.clear()
        self._start_done.clear()
        try:
            async with aiohttp.ClientSession() as session:
                stt_service = self._create_stt_service(session)
                
                # SmartTurn is only needed for Soniox realtime. Async/finalizing providers
                # use Pipecat VAD below so short silent recordings can skip provider finalization.
                smart_turn = (
                    _AnalyzerCache.get_smart_turn_analyzer()
                    if _live_service_uses_smart_turn(self.service_name)
                    else None
                )

                vad_analyzer = None
                # VAD is needed for:
                # 1. SegmentedSTTService silence-gating and optional mid-recording segmentation.
                # 2. Async live providers so silent recordings can be ended locally.
                # Note: For Soniox RT, we use SmartTurn V3 exclusively for turn detection.
                # Using both VAD and SmartTurn causes double UserStoppedSpeakingFrame events,
                # which triggers duplicate text injection.
                needs_vad = isinstance(stt_service, SegmentedSTTService) or _live_service_needs_local_vad(self.service_name)
                    
                if needs_vad:
                    vad_analyzer = _create_vad_analyzer(
                        quiet_mic=(
                            self.service_name == "onnx_local"
                            or _live_service_uses_async_finalization(self.service_name)
                        )
                    )
                    if not vad_analyzer:
                        logger.warning("VAD analyzer required but not available; transcripts may not finalize properly.")

                prewarm_active = bool(
                    self.mic_prewarm_manager is not None
                    and getattr(self.mic_prewarm_manager, "is_active", False)
                )
                use_prewarm_for_capture = bool(Config.MIC_ALWAYS_ON or prewarm_active)

                # Always use our custom MicrophoneInput to support on_audio_level callback
                self.audio_input = MicrophoneInput(
                    sample_rate=Config.SAMPLE_RATE,
                    channels=Config.CHANNELS,
                    block_size=Config.MIC_BLOCK_SIZE,
                    turn_analyzer=smart_turn,
                    vad_analyzer=vad_analyzer,
                    device=_resolve_mic_device(Config.MIC_DEVICE),
                    keep_alive=use_prewarm_for_capture,
                    prewarm_manager=self.mic_prewarm_manager,
                    on_audio_level=self.on_audio_level,
                    on_ready=self.on_mic_ready,
                    on_last_audio_chunk_sent=self.on_last_audio_chunk_sent,
                )

                inject_immediately = injects_immediately_in_live_mode(self.service_name) and not (
                    self.service_name == "soniox" and Config.SONIOX_MODE == "async"
                )
                text_injector = TextInjector(
                    inject_immediately=inject_immediately,
                    enabled=self.text_injection_enabled,
                    on_injected=self.on_text_injected,
                    on_injection_marker=self.on_injection_marker,
                )
                self.text_injector = text_injector
                needs_soniox_realtime_final_signal = (
                    self.service_name == "soniox" and Config.SONIOX_MODE != "async"
                )
                transcript_cb = (
                    TranscriptionCallbackProcessor(
                        self.on_transcription,
                        enable_speaker_diarization=self.enable_speaker_diarization,
                        on_final_transcription=self._mark_final_transcription_received,
                    ) if (self.on_transcription or needs_soniox_realtime_final_signal) else None
                )

                # Create cleanup callback to stop microphone on connection errors
                def sync_cleanup():
                    """Synchronous cleanup that schedules async cleanup."""
                    if self.audio_input:
                        try:
                            force_stop = getattr(
                                self.audio_input,
                                "force_stop_from_external_error",
                                None,
                            )
                            if callable(force_stop):
                                force_stop(reason="provider_connection_error")
                            elif hasattr(self.audio_input, "stream") and self.audio_input.stream:
                                self.audio_input.stream.stop()
                                self.audio_input._running = False
                        except Exception as e:
                            logger.debug(f"Sync cleanup warning: {e}")

                # Error handler to catch connection errors right after STT service
                error_handler = ConnectionErrorHandlerProcessor(
                    on_error=self.on_error,
                    cleanup_callback=sync_cleanup,
                    on_provider_error=self._record_terminal_error,
                )

                segmented_gate = (
                    SegmentedSTTRecordingGate(
                        vad_segmentation_enabled=bool(Config.SEGMENT_SPEECH_WITH_VAD)
                    )
                    if isinstance(stt_service, SegmentedSTTService)
                    else None
                )

                steps = [self.audio_input]
                if vad_analyzer is not None:
                    self._vad_observer = PipecatVadSpeechObserver(enabled=True)
                    steps.append(self._vad_observer)
                if segmented_gate is not None:
                    steps.append(segmented_gate)
                steps.extend([stt_service, error_handler])
                if transcript_cb:
                    steps.append(transcript_cb)
                steps.append(text_injector)

                self.pipeline = Pipeline(steps)
                self.task = PipelineTask(
                    self.pipeline,
                    params=PipelineParams(allow_interruptions=True),
                    check_dangling_tasks=False,  # suppress false-positive dangling task warnings (e.g., Soniox keepalive)
                )

                # Register error handler to catch non-fatal errors (e.g., Soniox websocket timeout)
                if self.on_error:
                    @self.task.event_handler("on_pipeline_error")
                    async def handle_pipeline_error(task, frame):
                        error_msg = str(frame.error) if hasattr(frame, 'error') else str(frame)
                        logger.error(f"Pipeline error event: {error_msg}")
                        if self.on_error:
                            self.on_error(error_msg)
                        # Cancel the pipeline on connection errors to prevent stuck state
                        if "timeout" in error_msg.lower() or "handshake" in error_msg.lower():
                            await task.cancel(reason=error_msg)

                # Disable signal handling because runner executes in background thread
                self.runner = PipelineRunner(handle_sigint=False, handle_sigterm=False)
                self.is_active = True

                if self.on_status_change:
                    self.on_status_change("Listening")

                await self.runner.run(self.task)

        except (ValueError, ImportError) as e:
            logger.error(f"Configuration error: {e}")
            self._record_terminal_error(str(e))
            self.is_active = False
            if self.on_status_change:
                self.on_status_change(f"Error: {e}")
            raise
        except Exception as e:
            logger.error(f"Error starting pipeline: {e}")
            self._record_terminal_error(str(e))
            self.is_active = False
            if self.on_status_change:
                self.on_status_change("Error")
            raise
        finally:
            # Ensure stop() can always unblock, even if start() exits due to an error or cancellation.
            self.is_active = False
            await self._cleanup_audio_input()
            self._start_done.set()

    async def transcribe_file(self, file_path: str) -> None:
        if self.is_active:
            return
        logger.info(f"Transcribing audio file with {self.service_name}: {file_path}")
        self._start_done.clear()
        file_input: FfmpegAudioFileInput | None = None
        try:
            async with aiohttp.ClientSession() as session:
                stt_service = self._create_stt_service(session, for_file=True)

                vad_analyzer = None
                if isinstance(stt_service, SegmentedSTTService):
                    vad_analyzer = _create_vad_analyzer(quiet_mic=self.service_name == "onnx_local")
                    if not vad_analyzer:
                        logger.warning("Segmented STT requires VAD; transcripts may be empty.")

                file_input = FfmpegAudioFileInput(
                    file_path,
                    sample_rate=Config.SAMPLE_RATE,
                    channels=Config.CHANNELS,
                    vad_analyzer=vad_analyzer,
                )

                transcript_cb = (
                    TranscriptionCallbackProcessor(
                        self.on_transcription,
                        enable_speaker_diarization=self.enable_speaker_diarization,
                    ) if self.on_transcription else None
                )
                steps = [file_input, stt_service]
                if transcript_cb:
                    steps.append(transcript_cb)

                self.pipeline = Pipeline(steps)
                self.task = PipelineTask(
                    self.pipeline,
                    params=PipelineParams(allow_interruptions=False),
                    check_dangling_tasks=False,
                )
                self.runner = PipelineRunner(handle_sigint=False, handle_sigterm=False)
                self.is_active = True

                if self.on_status_change:
                    self.on_status_change("Transcribing...")

                run_task = asyncio.create_task(self.runner.run(self.task), name="scriber_file_pipeline")

                # Wait until the input transport has finished feeding (and its internal audio queue has drained),
                # then end the pipeline gracefully so providers can flush final transcripts.
                await file_input.done.wait()
                await self.task.stop_when_done()

                await run_task

                if file_input.error:
                    raise RuntimeError(file_input.error)

        except (ValueError, ImportError) as e:
            logger.error(f"Configuration error: {e}")
            self.is_active = False
            if self.on_status_change:
                self.on_status_change(f"Error: {e}")
            raise
        except Exception as e:
            logger.error(f"Error transcribing file: {e}")
            self.is_active = False
            if self.on_status_change:
                self.on_status_change("Error")
            raise
        finally:
            self.is_active = False
            self._start_done.set()

    def _format_speaker_transcript(self, tokens: list) -> str:
        """Format tokens with speaker diarization labels.
        
        Groups consecutive tokens by speaker and formats as:
        [Speaker 1]: Hello, how are you?
        [Speaker 2]: I'm doing great, thanks!
        
        Args:
            tokens: List of token dicts with 'text' and optionally 'speaker' fields
            
        Returns:
            Formatted transcript string with speaker labels
        """
        return _format_speaker_transcript_tokens(
            [token for token in tokens if isinstance(token, dict)]
        )

    async def transcribe_file_direct(self, file_path: str) -> None:
        """
        Transcribe audio/video file by uploading directly to provider async APIs.
        This bypasses the PCM conversion pipeline and uploads the original file format.
        Much more efficient for file transcription than PCM conversion.
        """
        from pathlib import Path
        import mimetypes

        if self.is_active:
            return

        path = Path(file_path)
        if not path.exists():
            raise ValueError(f"File not found: {file_path}")

        logger.info(f"Transcribing file directly with {self.service_name}: {file_path}")
        self.is_active = True

        try:
            # Determine content type from extension
            ext = path.suffix.lower()
            content_type_map = {
                ".mp3": "audio/mpeg",
                ".wav": "audio/wav",
                ".m4a": "audio/mp4",
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".webm": "audio/webm",
                ".ogg": "audio/ogg",
                ".flac": "audio/flac",
                ".aac": "audio/aac",
            }
            content_type = content_type_map.get(ext, mimetypes.guess_type(str(path))[0] or "application/octet-stream")

            if self.service_name == "assemblyai":
                api_key = Config.get_api_key("assemblyai")
                if not api_key:
                    raise ValueError("AssemblyAI API key is missing")

                async with aiohttp.ClientSession() as session:
                    with open(path, "rb") as f:
                        payload = await transcribe_with_assemblyai_pre_recorded(
                            session=session,
                            api_key=api_key,
                            audio_source=f,
                            language=Config.LANGUAGE,
                            custom_vocab=Config.CUSTOM_VOCAB or "",
                            speaker_labels=True,  # File/Youtube: diarization enabled
                            model=Config.ASSEMBLYAI_ASYNC_MODEL,
                            on_progress=self.on_progress,
                            timeout_secs=900.0,
                        )

                text = assemblyai_transcript_payload_to_text(
                    payload,
                    prefer_speaker_labels=True,
                )
                if text and self.on_transcription:
                    logger.info(f"AssemblyAI direct transcription completed ({len(text)} chars)")
                    self.on_transcription(text, True)

                if self.on_progress:
                    self.on_progress("Completed")
                return

            if self.service_name in ("mistral", "mistral_async"):
                api_key = Config.get_api_key("mistral")
                if not api_key:
                    raise ValueError("Mistral API key is missing")
                file_size = path.stat().st_size

                language = Config.LANGUAGE if Config.LANGUAGE and Config.LANGUAGE != "auto" else None

                if self.on_progress:
                    self.on_progress("Uploading audio...")

                async with aiohttp.ClientSession() as session:
                    if self.on_progress:
                        self.on_progress("Processing transcription...")
                    logger.info(f"Mistral direct upload: {path.name} ({file_size} bytes, {content_type})")

                    with open(path, "rb") as f:
                        payload = await transcribe_with_mistral(
                            session=session,
                            api_key=api_key,
                            model=Config.MISTRAL_ASYNC_MODEL or "voxtral-mini-2602",
                            file_content=f,
                            filename=path.name,
                            content_type=content_type,
                            language=language,
                            context_bias=Config.CUSTOM_VOCAB or "",
                            diarize=True,
                            timestamp_granularities=["segment"],
                            timeout_secs=900,
                        )

                text = str(payload.get("text") or "").strip()
                segments = payload.get("segments") if isinstance(payload.get("segments"), list) else []
                if segments:
                    diarized = format_mistral_segments_with_speakers(
                        [s for s in segments if isinstance(s, dict)]
                    )
                    if diarized:
                        text = diarized

                if text and self.on_transcription:
                    logger.info(f"Mistral direct transcription completed ({len(text)} chars)")
                    self.on_transcription(text, True)

                if self.on_progress:
                    self.on_progress("Completed")
                return

            if self.service_name in ("smallest", "smallest_async"):
                api_key = Config.get_api_key("smallest")
                if not api_key:
                    raise ValueError("Smallest AI API key is missing")

                async with aiohttp.ClientSession() as session:
                    with open(path, "rb") as f:
                        payload = await transcribe_with_smallest_pre_recorded(
                            session=session,
                            api_key=api_key,
                            audio_source=f,
                            language=Config.LANGUAGE,
                            word_timestamps=True,
                            diarize=True,
                            on_progress=self.on_progress,
                            timeout_secs=900.0,
                        )

                text = smallest_transcript_payload_to_text(
                    payload,
                    prefer_speaker_labels=True,
                )
                if text and self.on_transcription:
                    logger.info(f"Smallest AI direct transcription completed ({len(text)} chars)")
                    self.on_transcription(text, True)

                if self.on_progress:
                    self.on_progress("Completed")
                return

            if self.service_name == "deepgram_async":
                api_key = Config.get_api_key("deepgram")
                if not api_key:
                    raise ValueError("Deepgram API key is missing")

                async with aiohttp.ClientSession() as session:
                    with open(path, "rb") as f:
                        payload = await transcribe_with_deepgram_pre_recorded(
                            session=session,
                            api_key=api_key,
                            audio_source=f,
                            filename=path.name,
                            content_type=content_type,
                            language=Config.LANGUAGE,
                            custom_vocab=Config.CUSTOM_VOCAB or "",
                            diarize=True,
                            on_progress=self.on_progress,
                            timeout_secs=900.0,
                        )

                text = deepgram_transcript_payload_to_text(
                    payload,
                    prefer_speaker_labels=True,
                )
                if text and self.on_transcription:
                    logger.info(f"Deepgram direct transcription completed ({len(text)} chars)")
                    self.on_transcription(text, True)

                if self.on_progress:
                    self.on_progress("Completed")
                return

            if self.service_name == "openai_async":
                api_key = Config.get_api_key("openai")
                if not api_key:
                    raise ValueError("OpenAI API key is missing")

                async with aiohttp.ClientSession() as session:
                    with open(path, "rb") as f:
                        payload = await transcribe_with_openai_audio_transcription(
                            session=session,
                            api_key=api_key,
                            audio_source=f,
                            filename=path.name,
                            content_type=content_type,
                            model=Config.OPENAI_STT_MODEL,
                            language=Config.LANGUAGE,
                            custom_vocab=Config.CUSTOM_VOCAB or "",
                            diarize=True,
                            on_progress=self.on_progress,
                            timeout_secs=900.0,
                        )

                text = openai_transcript_payload_to_text(
                    payload,
                    prefer_speaker_labels=True,
                )
                if text and self.on_transcription:
                    logger.info(f"OpenAI direct transcription completed ({len(text)} chars)")
                    self.on_transcription(text, True)

                if self.on_progress:
                    self.on_progress("Completed")
                return

            if self.service_name == "gemini_stt":
                api_key = Config.get_api_key("gemini_stt")
                if not api_key:
                    raise ValueError("Gemini API key is missing")

                async with aiohttp.ClientSession() as session:
                    with open(path, "rb") as f:
                        payload = await transcribe_with_gemini_audio(
                            session=session,
                            api_key=api_key,
                            audio_source=f,
                            filename=path.name,
                            content_type=content_type,
                            language=Config.LANGUAGE,
                            custom_vocab=Config.CUSTOM_VOCAB or "",
                            diarize=True,
                            on_progress=self.on_progress,
                            timeout_secs=900.0,
                        )

                text = gemini_transcript_payload_to_text(payload)
                if text and self.on_transcription:
                    logger.info(f"Gemini direct transcription completed ({len(text)} chars)")
                    self.on_transcription(text, True)

                if self.on_progress:
                    self.on_progress("Completed")
                return

            if self.service_name == "azure_mai":
                api_key = Config.get_api_key("azure_mai")
                if not api_key:
                    raise ValueError("Azure MAI Speech key is missing")

                region = validate_azure_mai_region(getattr(Config, "AZURE_MAI_REGION", None))
                if self.on_progress:
                    self.on_progress("Preparing audio...")

                async with aiohttp.ClientSession() as session:
                    async with prepared_azure_mai_audio_file(path) as upload_path:
                        with open(upload_path, "rb") as f:
                            payload = await transcribe_with_azure_mai(
                                session=session,
                                speech_key=api_key,
                                region=region,
                                audio_source=f,
                                filename=upload_path.name,
                                content_type=azure_mai_content_type(upload_path),
                                language=Config.LANGUAGE,
                                on_progress=self.on_progress,
                                timeout_secs=900.0,
                            )

                text = azure_mai_transcript_payload_to_text(payload)
                if text and self.on_transcription:
                    logger.info(f"Azure MAI direct transcription completed ({len(text)} chars)")
                    self.on_transcription(text, True)

                if self.on_progress:
                    self.on_progress("Completed")
                return

            if self.service_name in ("gladia", "gladia_async"):
                api_key = Config.get_api_key("gladia")
                if not api_key:
                    raise ValueError("Gladia API key is missing")

                async with aiohttp.ClientSession() as session:
                    with open(path, "rb") as f:
                        payload = await transcribe_with_gladia_pre_recorded(
                            session=session,
                            api_key=api_key,
                            audio_source=f,
                            filename=path.name,
                            content_type=content_type,
                            language=Config.LANGUAGE,
                            custom_vocab=Config.CUSTOM_VOCAB or "",
                            diarize=True,
                            on_progress=self.on_progress,
                            timeout_secs=900.0,
                        )

                text = gladia_transcript_payload_to_text(
                    payload,
                    prefer_speaker_labels=True,
                )
                if text and self.on_transcription:
                    logger.info(f"Gladia direct transcription completed ({len(text)} chars)")
                    self.on_transcription(text, True)

                if self.on_progress:
                    self.on_progress("Completed")
                return

            if self.service_name == "speechmatics_async":
                api_key = Config.get_api_key("speechmatics")
                if not api_key:
                    raise ValueError("Speechmatics API key is missing")

                async with aiohttp.ClientSession() as session:
                    with open(path, "rb") as f:
                        payload = await transcribe_with_speechmatics_batch(
                            session=session,
                            api_key=api_key,
                            audio_source=f,
                            filename=path.name,
                            content_type=content_type,
                            language=Config.LANGUAGE,
                            custom_vocab=Config.CUSTOM_VOCAB or "",
                            diarize=True,
                            on_progress=self.on_progress,
                            timeout_secs=900.0,
                        )

                text = speechmatics_transcript_payload_to_text(
                    payload,
                    prefer_speaker_labels=True,
                )
                if text and self.on_transcription:
                    logger.info(f"Speechmatics direct transcription completed ({len(text)} chars)")
                    self.on_transcription(text, True)

                if self.on_progress:
                    self.on_progress("Completed")
                return

            api_key = Config.get_api_key("soniox")
            if not api_key:
                raise ValueError("Soniox API key is missing")

            headers = {"Authorization": f"Bearer {api_key}"}
            base_url = "https://api.soniox.com/v1"
            model = Config.SONIOX_ASYNC_MODEL or Config.DEFAULT_SONIOX_ASYNC_MODEL
            file_id = None
            transcription_id = None

            if self.on_progress:
                self.on_progress("Uploading audio...")

            async with aiohttp.ClientSession() as session:
                async def _cleanup_resources() -> None:
                    if not file_id and not transcription_id:
                        return
                    if transcription_id:
                        try:
                            async with session.delete(
                                f"{base_url}/transcriptions/{transcription_id}",
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10),
                            ) as resp:
                                if resp.status in (200, 204, 404):
                                    logger.debug(f"Deleted Soniox transcription {transcription_id}")
                                else:
                                    logger.warning(
                                        f"Failed to delete Soniox transcription {transcription_id}: {resp.status}"
                                    )
                        except Exception as exc:
                            logger.warning(f"Error deleting Soniox transcription: {exc}")

                    if file_id:
                        try:
                            async with session.delete(
                                f"{base_url}/files/{file_id}",
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10),
                            ) as resp:
                                if resp.status in (200, 204, 404):
                                    logger.debug(f"Deleted Soniox file {file_id}")
                                else:
                                    logger.warning(f"Failed to delete Soniox file {file_id}: {resp.status}")
                        except Exception as exc:
                            logger.warning(f"Error deleting Soniox file: {exc}")

                try:
                    # Upload file directly
                    file_size = path.stat().st_size
                    logger.info(f"Uploading {path.name} ({file_size} bytes, {content_type})")

                    data = aiohttp.FormData()
                    with open(path, "rb") as f:
                        data.add_field("file", f, filename=path.name, content_type=content_type)

                        async with session.post(
                            f"{base_url}/files",
                            data=data,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=300),  # 5 min for large files
                        ) as resp:
                            resp.raise_for_status()
                            file_id = (await resp.json())["id"]

                    # Start transcription with speaker diarization enabled for file/youtube
                    payload = {"file_id": file_id, "model": model}
                    # Build proper context object if custom_vocab is provided
                    if Config.CUSTOM_VOCAB:
                        terms = [t.strip() for t in Config.CUSTOM_VOCAB.split(",") if t.strip()]
                        if terms:
                            payload["context"] = {"terms": terms}
                    # Enable speaker diarization for file/youtube transcription
                    payload["enable_speaker_diarization"] = True

                    async with session.post(
                        f"{base_url}/transcriptions",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp2:
                        resp2.raise_for_status()
                        transcription_id = (await resp2.json())["id"]

                    # Poll for completion
                    if self.on_progress:
                        self.on_progress("Processing transcription...")

                    done_statuses = {"completed", "done", "succeeded", "success"}
                    error_statuses = {"error", "failed", "canceled", "cancelled"}
                    poll_start = asyncio.get_running_loop().time()
                    poll_timeout = 600.0  # 10 minutes max
                    poll_count = 0

                    while True:
                        elapsed = asyncio.get_running_loop().time() - poll_start
                        if elapsed > poll_timeout:
                            raise TimeoutError("Soniox transcription timed out")

                        async with session.get(
                            f"{base_url}/transcriptions/{transcription_id}",
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as r:
                            r.raise_for_status()
                            status_payload = await r.json()
                            status = (status_payload.get("status") or "").lower()

                            if status in done_statuses:
                                break
                            if status in error_statuses:
                                raise RuntimeError(status_payload.get("error_message", "Soniox transcription error"))

                        poll_count += 1
                        # Log every 10 seconds for debugging
                        if poll_count % 10 == 0:
                            logger.debug(f"Soniox direct polling: {int(elapsed)}s elapsed")

                        await asyncio.sleep(1)

                    # Get transcript
                    if self.on_progress:
                        self.on_progress("Retrieving transcript...")

                    async with session.get(
                        f"{base_url}/transcriptions/{transcription_id}/transcript",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as r3:
                        r3.raise_for_status()
                        transcript_payload = await r3.json()

                        # Parse speaker diarization if available
                        tokens = transcript_payload.get("tokens", [])
                        if tokens and any(t.get("speaker") for t in tokens):
                            # Format with speaker labels
                            text = self._format_speaker_transcript(tokens)
                        else:
                            # Fallback to plain text
                            text = transcript_payload.get("text", "")

                    if text and self.on_transcription:
                        logger.info(f"Soniox direct transcription completed ({len(text)} chars)")
                        self.on_transcription(text, True)

                    if self.on_progress:
                        self.on_progress("Completed")
                finally:
                    await _cleanup_resources()

        except Exception as e:
            logger.error(f"Direct file transcription failed: {e}")
            if self.on_status_change:
                self.on_status_change("Error")
            raise
        finally:
            self.is_active = False

    async def stop(self, timeout_secs: float | None = None):
        if self.task and self.task.has_finished():
            self.is_active = False
            if self.on_status_change:
                self.on_status_change("Error" if self._terminal_error else "Stopped")
            await self._cleanup_audio_input()
            if self._terminal_error:
                raise RuntimeError(self._terminal_error)
            return
        if not self.is_active:
            await self._cleanup_audio_input()
            if self._terminal_error:
                raise RuntimeError(self._terminal_error)
            return
        logger.info("Stopping Scriber Pipeline")

        is_soniox_async = (
            self.service_name == "soniox_async"
            or (self.service_name == "soniox" and Config.SONIOX_MODE == "async")
        )
        is_async_finalization = _live_service_uses_async_finalization(self.service_name)
        if self.on_status_change:
            self.on_status_change("Transcribing..." if is_async_finalization else "Stopping...")

        has_segmented_stt_buffers = self._has_segmented_stt_buffers()
        if has_segmented_stt_buffers:
            # Give audio frames time to propagate before closing the upload segment.
            # This is critical for short recordings where stop is called very quickly.
            await asyncio.sleep(0.15)
            await self._stop_audio_capture_for_segmented_finalization()
            await self._flush_segmented_stt_buffers()
            segmented_wait_result = await self._wait_for_segmented_stt_final_or_done(
                timeout_seconds=self._segmented_stt_stop_final_timeout_seconds()
            )
            if segmented_wait_result == "final":
                logger.debug("Segmented STT final transcription received before pipeline shutdown")
            elif segmented_wait_result == "timeout":
                logger.warning("Timed out waiting for segmented STT final transcription before pipeline shutdown")
            else:
                logger.debug(
                    f"Segmented STT stop wait completed without final transcription ({segmented_wait_result})"
                )
            await self._cleanup_audio_input()
        else:
            # Stop mic capture immediately so LED turns off while transcription finalizes.
            await self._cleanup_audio_input()
            # Give non-segmented providers time to consume the final transport frames.
            await asyncio.sleep(0.15)
            if await self._flush_live_vad_finalization_turn():
                await asyncio.sleep(0.15)

        # For Soniox real-time: send stop_recording and wait for final tokens BEFORE pipeline shutdown.
        # This ensures all spoken audio is transcribed and injected before we close.
        is_soniox_rt = self.service_name == "soniox" and not is_soniox_async
        soniox_manual_stop_done = False
        if is_soniox_rt and self.pipeline and hasattr(self.pipeline, '_processors'):
            for step in self.pipeline._processors:
                if step.__class__.__name__ == "SonioxSTTService":
                    try:
                        from websockets.protocol import State
                        websocket = getattr(step, "_websocket", None)

                        # Log diagnostics about the connection state
                        audio_bytes_sent = getattr(step, "_audio_bytes_sent", 0)
                        logger.debug(f"Soniox stop: audio_bytes_sent={audio_bytes_sent}")

                        # Wait for websocket to be ready if it's still connecting
                        if websocket:
                            wait_start = asyncio.get_running_loop().time()
                            while websocket.state not in (State.OPEN, State.CLOSED) and (asyncio.get_running_loop().time() - wait_start) < 2.0:
                                await asyncio.sleep(0.05)

                        if websocket and websocket.state is State.OPEN:
                            # Wait for any pending audio to be sent before requesting finalization
                            # The STT service might have buffered audio that hasn't been sent yet
                            audio_queue = getattr(step, "_audio_queue", None)
                            if audio_queue:
                                try:
                                    # Wait up to 0.5s for audio queue to drain
                                    drain_start = asyncio.get_running_loop().time()
                                    while not audio_queue.empty() and (asyncio.get_running_loop().time() - drain_start) < 0.5:
                                        await asyncio.sleep(0.02)
                                    if audio_queue.empty():
                                        logger.debug("Soniox audio queue drained successfully")
                                    else:
                                        logger.warning(f"Soniox audio queue not fully drained (approx {audio_queue.qsize()} items remaining)")
                                except Exception as e:
                                    logger.debug(f"Audio queue drain check error: {e}")

                            # Send stop_recording (empty string) to trigger finalization
                            logger.debug("Sending stop_recording to Soniox")
                            await websocket.send("")

                            # Wait for receive task to complete (gets final tokens + finished=True)
                            receive_task = getattr(step, "_receive_task", None)
                            if receive_task and not receive_task.done():
                                final_timeout = self._soniox_realtime_stop_final_timeout_seconds()
                                wait_result = await self._wait_for_soniox_realtime_final_or_receive_done(
                                    receive_task,
                                    timeout_seconds=final_timeout,
                                )
                                if wait_result in {"final", "receive_done"}:
                                    soniox_manual_stop_done = True
                                    logger.debug(
                                        f"Soniox realtime stop completed via {wait_result} before pipeline shutdown"
                                    )
                                elif wait_result == "timeout":
                                    ws_state = websocket.state if websocket else "no websocket"
                                    logger.warning(
                                        f"Soniox realtime stop wait timeout ({final_timeout:g}s) - "
                                        f"ws_state={ws_state}, audio_bytes_sent={audio_bytes_sent}, "
                                        f"final_received={self._final_transcription_received.is_set()}"
                                    )
                                    if websocket and websocket.state is State.OPEN:
                                        logger.debug("Retrying stop_recording signal to Soniox")
                                        try:
                                            await websocket.send("")
                                        except Exception as retry_err:
                                            logger.debug(f"Soniox retry send error: {retry_err}")
                                    if self._final_transcription_received.is_set():
                                        soniox_manual_stop_done = True
                                elif wait_result == "cancelled":
                                    logger.debug("Soniox receive task was cancelled during stop")
                                else:
                                    logger.debug(
                                        f"Soniox receive task did not finish cleanly during stop: {wait_result}"
                                    )
                            else:
                                if receive_task and receive_task.done():
                                    wait_result = await self._wait_for_soniox_realtime_final_or_receive_done(
                                        receive_task,
                                        timeout_seconds=0.25,
                                    )
                                    soniox_manual_stop_done = wait_result in {"final", "receive_done"}
                                    logger.debug("Soniox receive task was already done")
                                else:
                                    if self._final_transcription_received.is_set():
                                        soniox_manual_stop_done = True
                                        logger.debug("Soniox final transcription received without receive task")
                                    else:
                                        logger.warning("No Soniox receive task found")
                        elif websocket:
                            logger.warning(f"Soniox websocket not open on stop (state={websocket.state}) - transcription may be incomplete")
                        else:
                            logger.warning("No Soniox websocket found - recording may have failed to connect")
                    except Exception as e:
                        logger.debug(f"Soniox stop handling error: {e}")

        # Now request pipeline shutdown
        # If we already did manual stop for Soniox RT, use cancel to skip pipecat's stop flow
        # which would try to send stop_recording again
        stop_future = None
        if self.task and not self.task.has_finished():
            if soniox_manual_stop_done:
                stop_future = asyncio.create_task(self.task.cancel(reason="manual stop completed"))
            else:
                stop_future = asyncio.create_task(self.task.stop_when_done())
        
        wait_timeout = timeout_secs
        if wait_timeout is None:
            wait_timeout = 600.0 if is_async_finalization else 30.0

        try:
            # Wait for either start_done (pipeline completely finished) or timeout
            await asyncio.wait_for(self._start_done.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout while stopping pipeline (>{wait_timeout}s); forcing cancel")
            self._record_terminal_error(
                f"Transcription did not finish within {wait_timeout:g} seconds."
            )
            if self.task and not self.task.has_finished():
                try:
                    await self.task.cancel(reason="stop timeout")
                except Exception as e:
                    logger.debug(f"Task cancel error: {e}")
            if self.runner:
                try:
                    await self.runner.cancel()
                except Exception as e:
                    logger.debug(f"Runner cancel error: {e}")

        # Ensure future is cleaned up
        if stop_future and not stop_future.done():
            try:
                await stop_future
            except Exception:
                pass

        # Flush any buffered transcription as a last resort.
        try:
            if hasattr(self, "text_injector") and self.text_injector:
                self.text_injector.flush()
        except Exception as e:
            logger.debug(f"TextInjector flush warning: {e}")

        # Explicitly cleanup Soniox realtime service if present to clear dangling tasks.
        soniox_steps = []
        try:
            if self.pipeline and self.pipeline.steps:
                soniox_steps = [s for s in self.pipeline.steps if s.__class__.__name__ == "SonioxSTTService"]
        except Exception:
            soniox_steps = []
        for step in soniox_steps:
            try:
                if hasattr(step, "_cleanup"):
                    await step._cleanup()
                for attr in ("_keepalive_task", "_receive_task"):
                    t = getattr(step, attr, None)
                    if t:
                        t.cancel()
                        await asyncio.gather(t, return_exceptions=True)
            except Exception as e:
                logger.debug(f"Soniox cleanup warning: {e}")

        await self._cleanup_audio_input()
        self.is_active = False
        if self.on_status_change:
            self.on_status_change("Error" if self._terminal_error else "Stopped")
        if self._terminal_error:
            raise RuntimeError(self._terminal_error)
