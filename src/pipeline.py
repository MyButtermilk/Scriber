import asyncio
import aiohttp
import io
import wave
import contextlib
import tempfile
import os
import time
import inspect
import math
import re
from typing import Any, BinaryIO, Callable, Mapping, Optional

from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.stt_service import SegmentedSTTService, STTService
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
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.time import time_now_iso8601

from src.runtime.media_tools import find_media_tool
from src.runtime.provider_dependencies import import_provider_runtime_module
from src.runtime.subprocess_utils import communicate_or_kill_on_cancel, hidden_subprocess_kwargs
from src.runtime.http_response import read_response_json_limited, read_response_text_limited
from src.runtime.audio_spool import append_pcm_frame, close_pcm_spool, create_pcm_spool, pcm_stream_to_wav
from src.runtime.env_values import env_float as _safe_env_float

try:
    from pipecat.audio.streams.input import SoundDeviceAudioInputStream
except ImportError:
    SoundDeviceAudioInputStream = None

try:
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    HAS_SMART_TURN = True
except ImportError:
    LocalSmartTurnAnalyzerV3 = None
    HAS_SMART_TURN = False
    logger.debug("Optional Pipecat LocalSmartTurnAnalyzerV3 is not available")

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
# One-shot analyzer warmup cache for faster first-pipeline start
# ============================================================================
import threading

class _AnalyzerCache:
    """Thread-safe, one-shot warmup pool for expensive analyzer instances.

    Pipecat analyzers contain mutable audio buffers, recurrent model state, and
    per-session executors. A processor cleans its analyzer at pipeline teardown,
    so returning that same instance to a later recording is unsafe. Startup may
    pre-create one unused instance of each analyzer; ``acquire_*`` atomically
    removes it from the pool and ownership transfers permanently to that
    recording. Later recordings receive newly constructed instances.
    """
    _lock = threading.Lock()
    _vad_analyzer = None
    _smart_turn_analyzer = None
    _refill_in_progress = False

    @classmethod
    def prewarm(
        cls,
        *,
        include_vad: bool = True,
        include_smart_turn: bool = True,
    ) -> None:
        """Create at most one requested, unclaimed analyzer of each type."""
        with cls._lock:
            needs_vad = bool(
                include_vad
                and HAS_SILERO_VAD
                and SileroVADAnalyzer
                and cls._vad_analyzer is None
            )
            needs_smart_turn = bool(
                include_smart_turn
                and HAS_SMART_TURN
                and LocalSmartTurnAnalyzerV3
                and cls._smart_turn_analyzer is None
            )

        # Model construction stays outside the lock. A hotkey arriving during
        # background refill creates its own session instance instead of waiting
        # hundreds of milliseconds for the warm slot.
        vad_analyzer = None
        smart_turn_analyzer = None
        if needs_vad:
            logger.info("Prewarming one-shot Silero VAD analyzer")
            vad_analyzer = SileroVADAnalyzer()
        if needs_smart_turn:
            logger.info("Prewarming one-shot SmartTurn V3 analyzer")
            smart_turn_analyzer = LocalSmartTurnAnalyzerV3()

        with cls._lock:
            if vad_analyzer is not None and cls._vad_analyzer is None:
                cls._vad_analyzer = vad_analyzer
            if smart_turn_analyzer is not None and cls._smart_turn_analyzer is None:
                cls._smart_turn_analyzer = smart_turn_analyzer

    @classmethod
    def request_background_replenish(
        cls,
        *,
        include_vad: bool = True,
        include_smart_turn: bool = True,
    ) -> bool:
        """Refill empty warm slots on a daemon thread after session teardown."""
        with cls._lock:
            needs_vad = bool(
                include_vad
                and HAS_SILERO_VAD
                and SileroVADAnalyzer
                and cls._vad_analyzer is None
            )
            needs_smart_turn = bool(
                include_smart_turn
                and HAS_SMART_TURN
                and LocalSmartTurnAnalyzerV3
                and cls._smart_turn_analyzer is None
            )
            if cls._refill_in_progress or not (needs_vad or needs_smart_turn):
                return False
            cls._refill_in_progress = True

        def refill() -> None:
            try:
                cls.prewarm(
                    include_vad=include_vad,
                    include_smart_turn=include_smart_turn,
                )
            except Exception as exc:
                logger.debug(f"Analyzer warmup refill failed: {exc}")
            finally:
                with cls._lock:
                    cls._refill_in_progress = False

        try:
            threading.Thread(
                target=refill,
                name="scriber-analyzer-warmup",
                daemon=True,
            ).start()
        except Exception as exc:
            with cls._lock:
                cls._refill_in_progress = False
            logger.debug(f"Could not schedule analyzer warmup refill: {exc}")
            return False
        return True

    @classmethod
    def acquire_vad_analyzer(cls):
        """Claim a prewarmed Silero analyzer or create a fresh session instance."""
        if not HAS_SILERO_VAD or not SileroVADAnalyzer:
            return None
        with cls._lock:
            analyzer = cls._vad_analyzer
            cls._vad_analyzer = None
        if analyzer is not None:
            logger.debug("Claimed prewarmed Silero VAD analyzer for one recording")
            return analyzer
        logger.info("Initializing per-session Silero VAD analyzer")
        return SileroVADAnalyzer()

    @classmethod
    def acquire_smart_turn_analyzer(cls):
        """Claim a prewarmed SmartTurn analyzer or create a fresh session instance."""
        if not HAS_SMART_TURN or not LocalSmartTurnAnalyzerV3:
            return None
        with cls._lock:
            analyzer = cls._smart_turn_analyzer
            cls._smart_turn_analyzer = None
        if analyzer is not None:
            logger.debug("Claimed prewarmed SmartTurn V3 analyzer for one recording")
            return analyzer
        logger.info("Initializing per-session SmartTurn V3 analyzer")
        return LocalSmartTurnAnalyzerV3()

    @classmethod
    def clear_cache(cls):
        """Discard only unclaimed warmup instances."""
        with cls._lock:
            cls._vad_analyzer = None
            cls._smart_turn_analyzer = None
            logger.debug("Unclaimed analyzer warmup cache cleared")

    @classmethod
    def discard_vad_cache(cls) -> None:
        """Release an unused Silero warmup when the user disables VAD."""
        with cls._lock:
            cls._vad_analyzer = None
        logger.debug("Unclaimed Silero VAD warmup cache cleared")


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


def _live_analyzer_requirements(
    service_name: str,
    *,
    segmented_service: bool = False,
    provider_replay: bool = False,
) -> tuple[bool, bool]:
    """Return ``(needs_vad, uses_smart_turn)`` for one Live Mic session."""
    vad_enabled = bool(Config.SEGMENT_SPEECH_WITH_VAD)
    uses_smart_turn = bool(
        vad_enabled
        and (provider_replay or _live_service_uses_smart_turn(service_name))
    )
    needs_vad = bool(
        vad_enabled
        and (
            segmented_service
            or _live_service_needs_local_vad(service_name)
            or uses_smart_turn
        )
    )
    return needs_vad, uses_smart_turn


def _live_recording_gate_needed(
    service_name: str,
    *,
    segmented_service: bool,
    vad_attached: bool,
) -> bool:
    """Preserve one provider turn when no local VAD processor is attached."""
    return bool(
        segmented_service
        or (not vad_attached and _live_service_needs_local_vad(service_name))
    )


def _analyzer_warmup_enabled() -> bool:
    explicit = os.getenv("SCRIBER_PREWARM_MODELS_ON_STARTUP", "").strip().lower()
    return bool(Config.MIC_ALWAYS_ON) or explicit in {"1", "true", "yes", "on"}


def _create_vad_analyzer(*, quiet_mic: bool = False):
    vad_analyzer = _AnalyzerCache.acquire_vad_analyzer()
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


def _create_soniox_smart_turn_processor() -> UserTurnProcessor | None:
    """Create a session-owned Pipecat 1.5 SmartTurn processor.

    ``TransportParams`` no longer accepts analyzers. SmartTurn must run after
    Soniox STT so its stop strategy sees both passthrough audio/VAD events and
    Soniox's finalized transcript. Explicit strategies also avoid Pipecat's
    implicit defaults and their optional dependency path.
    """
    analyzer = _AnalyzerCache.acquire_smart_turn_analyzer()
    if analyzer is None:
        return None
    return UserTurnProcessor(
        user_turn_strategies=UserTurnStrategies(
            start=[VADUserTurnStartStrategy()],
            stop=[
                TurnAnalyzerUserTurnStopStrategy(
                    turn_analyzer=analyzer,
                    wait_for_transcript=True,
                )
            ],
        )
    )


def _ordered_live_pipeline_steps(
    *,
    audio_input: FrameProcessor,
    vad_processor: FrameProcessor | None,
    vad_observer: FrameProcessor | None,
    segmented_gate: FrameProcessor | None,
    stt_service: FrameProcessor,
    smart_turn_processor: FrameProcessor | None,
    error_handler: FrameProcessor,
    transcript_callback: FrameProcessor | None,
    text_injector: FrameProcessor,
) -> list[FrameProcessor]:
    """Return the canonical Pipecat 1.5 live-microphone processor order."""
    steps: list[FrameProcessor] = [audio_input]
    if vad_processor is not None:
        steps.append(vad_processor)
    if vad_observer is not None:
        steps.append(vad_observer)
    if segmented_gate is not None:
        # The gate observes/suppresses VAD boundaries before HTTP-style STT.
        steps.append(segmented_gate)
    steps.append(stt_service)
    if smart_turn_processor is not None:
        # SmartTurn needs STT audio passthrough plus finalized transcripts.
        steps.append(smart_turn_processor)
    steps.append(error_handler)
    if transcript_callback is not None:
        steps.append(transcript_callback)
    steps.append(text_injector)
    return steps


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    return _safe_env_float(name, default, minimum=minimum, maximum=maximum)


def _redact_provider_error_text(message: str) -> str:
    text = str(message or "").replace("\x00", "")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)\bToken\s+[A-Za-z0-9._~+/=-]+", "Token [REDACTED]", text)
    return text

SonioxSTTService = None
SonioxContextObject = None


def _format_speaker_transcript_tokens(tokens: list[dict[str, Any]]) -> str:
    """Format provider tokens with contiguous speaker labels."""
    if not tokens:
        return ""

    segments: list[tuple[int, str]] = []
    speaker_numbers: dict[str, int] = {}
    current_speaker: str | None = None
    current_text: list[str] = []

    for token in tokens:
        if not isinstance(token, dict):
            continue
        text = str(token.get("text") or "")
        speaker_value = token.get("speaker")
        speaker = "" if speaker_value in (None, "") else str(speaker_value).strip()

        if speaker and speaker != current_speaker:
            if current_text and current_speaker:
                segments.append((speaker_numbers[current_speaker], "".join(current_text).strip()))
            current_speaker = speaker
            speaker_numbers.setdefault(speaker, len(speaker_numbers) + 1)
            current_text = [text]
        else:
            current_text.append(text)

    if current_text and current_speaker:
        segments.append((speaker_numbers[current_speaker], "".join(current_text).strip()))

    if not segments:
        return "".join(str(t.get("text") or "") for t in tokens if isinstance(t, dict)).strip()

    return "\n\n".join(
        f"[Speaker {speaker}]: {text}"
        for speaker, text in segments
        if text
    )


def direct_file_workflow_timeout_seconds(
    expected_duration_seconds: float | None,
    *,
    minimum_seconds: float = 600.0,
    maximum_seconds: float = 21_600.0,
) -> float:
    """Bound the outer file workflow while allowing long local inference.

    Provider adapters retain tighter upload and polling budgets. The outer
    owner also covers decoded-PCM routes such as local ONNX, so it allows a
    little more than real time plus fixed setup/finalization overhead, capped at
    six hours to retain deterministic cancellation.
    """
    try:
        duration = float(expected_duration_seconds or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration < 0.0:
        duration = 0.0
    try:
        minimum = max(1.0, float(minimum_seconds))
    except (TypeError, ValueError):
        minimum = 600.0
    if not math.isfinite(minimum):
        minimum = 600.0
    try:
        maximum = max(minimum, float(maximum_seconds))
    except (TypeError, ValueError):
        maximum = max(minimum, 21_600.0)
    if not math.isfinite(maximum):
        maximum = max(minimum, 21_600.0)
    return min(maximum, max(minimum, 300.0 + duration * 1.1))


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
        if tokens and any(token.get("speaker") not in (None, "") for token in tokens):
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
        return create_pcm_spool()

    def _reset_buffer(self) -> None:
        close_pcm_spool(getattr(self, "_buffer", None))
        self._buffer = self._create_buffer()
        self._buffer_size = 0

    def __del__(self) -> None:
        close_pcm_spool(getattr(self, "_buffer", None))

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame):
            if not self._sample_rate:
                self._sample_rate = frame.sample_rate
            if not self._channels:
                self._channels = frame.num_channels
            self._buffer_size = await append_pcm_frame(
                self._buffer,
                self._buffer_size,
                frame.audio,
            )
            await self.push_frame(frame, direction)
        elif isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            try:
                if getattr(self, "_skip_terminal_transcription", False):
                    logger.info("Soniox async: skipping terminal transcription for silent recording")
                elif not self._buffer_size:
                    logger.debug("Soniox async: no audio buffered; skipping transcription")
                else:
                    self._buffer.seek(0)
                    text = await self._transcribe_async(
                        audio_stream=self._buffer,
                        audio_size=self._buffer_size,
                    )
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
            finally:
                self._reset_buffer()
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    def _report_progress(self, msg: str) -> None:
        """Report progress to callback if set."""
        if self.on_progress:
            try:
                self.on_progress(msg)
            except Exception:
                pass

    async def _transcribe_async(
        self,
        audio_bytes: bytes | None = None,
        *,
        audio_stream: BinaryIO | None = None,
        audio_size: int | None = None,
    ) -> str:
        """
        Upload audio to Soniox async API as WebM/Opus, with a local WAV encoding fallback.
        """
        raw_audio_size = len(audio_bytes) if audio_bytes is not None else int(audio_size or 0)
        if raw_audio_size <= 0:
            return ""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        done_statuses = {"completed", "done", "succeeded", "success"}
        error_statuses = {"error", "failed", "canceled", "cancelled"}
        poll_start = asyncio.get_running_loop().time()
        # Heuristic: allow at least 60s, or up to ~3x audio duration (min 2m, max 10m)
        sr = self._sample_rate or 16000
        ch = self._channels or 1
        audio_secs = raw_audio_size / max(1, (sr * ch * 2))
        poll_timeout = min(600.0, max(120.0, max(60.0, audio_secs * 3.0)))

        self._report_progress("Uploading audio...")

        cleanup_paths: tuple[str, ...] = ()
        encoded_stream: BinaryIO | None = None
        if audio_stream is not None:
            encoded_stream, content_type, filename, cleanup_paths = await self._encode_audio_stream(
                audio_stream,
                prefer_webm=True,
            )
            file_content: bytes | BinaryIO = encoded_stream
            encoded_size = self._stream_size(encoded_stream)
        else:
            file_content, content_type, filename = await self._encode_audio(
                audio_bytes or b"",
                prefer_webm=True,
            )
            encoded_size = len(file_content)
        if Config.DEBUG:
            logger.info(
                f"Soniox async upload using {filename} ({encoded_size} bytes)"
            )

        async def upload_file(
            content: bytes | BinaryIO,
            *,
            upload_content_type: str,
            upload_filename: str,
        ) -> str:
            data = aiohttp.FormData()
            data.add_field(
                "file",
                content,
                filename=upload_filename,
                content_type=upload_content_type,
            )
            async with self.session.post(
                f"{self.BASE_URL}/files",
                data=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                if response.status in (200, 201):
                    payload = await read_response_json_limited(response, 64 * 1024 * 1024)
                    uploaded_file_id = str(payload.get("id") or "").strip()
                    if not uploaded_file_id:
                        raise RuntimeError("Soniox file upload response did not include an id")
                    return uploaded_file_id

                error_body = await read_response_text_limited(response, 64 * 1024 * 1024)
                logger.error(
                    "Soniox file upload failed: "
                    f"status={response.status}, body={error_body[:500]}"
                )
                response.raise_for_status()
                raise RuntimeError(f"Soniox file upload failed ({response.status})")

        file_id: str | None = None
        transcription_id: str | None = None
        try:
            file_id = await upload_file(
                file_content,
                upload_content_type=content_type,
                upload_filename=filename,
            )
            if not file_id:
                raise RuntimeError("Soniox file upload completed without a file id")

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
                transcription_id = (await read_response_json_limited(resp2, 64 * 1024 * 1024))["id"]

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
                        status_payload = await read_response_json_limited(r, 64 * 1024 * 1024)
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
                transcript_payload = await read_response_json_limited(r3, 64 * 1024 * 1024)
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
                
                return text

        except Exception as e:
            logger.error(f"Soniox async transcription failed: {e}")
            raise
        finally:
            # Cancellation is a BaseException on current Python versions, so
            # remote cleanup must live in finally rather than except Exception.
            if file_id:
                await self._cleanup_soniox_resources(
                    file_id,
                    transcription_id,
                    headers,
                )
            if encoded_stream is not None:
                try:
                    encoded_stream.close()
                except Exception:
                    pass
            for cleanup_path in cleanup_paths:
                try:
                    os.unlink(cleanup_path)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    logger.debug(f"Could not remove Soniox temporary audio file: {exc}")

    @staticmethod
    def _stream_size(stream: BinaryIO) -> int:
        current = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(current)
        return max(0, int(size))

    async def _encode_audio_stream(
        self,
        audio_stream: BinaryIO,
        *,
        prefer_webm: bool = True,
    ) -> tuple[BinaryIO, str, str, tuple[str, ...]]:
        """Encode spooled PCM without materializing the recording as one bytes object."""
        sr = self._sample_rate or 16000
        ch = self._channels or 1
        ffmpeg = find_media_tool("ffmpeg") if prefer_webm else None
        if ffmpeg:
            output = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
            output_path = output.name
            output.close()
            proc = None
            feed_task: asyncio.Task | None = None
            try:
                cmd = [
                    ffmpeg,
                    "-y",
                    "-f",
                    "s16le",
                    "-ar",
                    str(sr),
                    "-ac",
                    str(ch),
                    "-i",
                    "pipe:0",
                    "-c:a",
                    "libopus",
                    "-ar",
                    "48000",
                    "-ac",
                    "1",
                    "-b:a",
                    "32k",
                    "-application",
                    "voip",
                    output_path,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **hidden_subprocess_kwargs(),
                )

                async def _feed_pcm() -> None:
                    assert proc is not None and proc.stdin is not None
                    audio_stream.seek(0)
                    try:
                        while chunk := audio_stream.read(1024 * 1024):
                            proc.stdin.write(chunk)
                            await proc.stdin.drain()
                    finally:
                        proc.stdin.close()

                feed_task = asyncio.create_task(_feed_pcm(), name="soniox_ffmpeg_pcm_feed")
                _stdout, stderr = await communicate_or_kill_on_cancel(
                    proc,
                    max_stdout_bytes=64 * 1024,
                    max_stderr_bytes=1024 * 1024,
                )
                await feed_task
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"FFmpeg encoding failed: {stderr.decode('utf-8', errors='replace')}"
                    )
                encoded = open(output_path, "rb")
                return encoded, "audio/webm", "audio.webm", (output_path,)
            except asyncio.CancelledError:
                if feed_task is not None and not feed_task.done():
                    feed_task.cancel()
                    await asyncio.gather(feed_task, return_exceptions=True)
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
                raise
            except Exception as exc:
                if feed_task is not None and not feed_task.done():
                    feed_task.cancel()
                    await asyncio.gather(feed_task, return_exceptions=True)
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
                logger.warning(f"WebM stream encode failed ({exc}); falling back to WAV")

        wav_stream = await asyncio.to_thread(pcm_stream_to_wav, audio_stream, sr, ch)
        return wav_stream, "audio/wav", "audio.wav", ()

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

                _, stderr = await communicate_or_kill_on_cancel(
                    proc,
                    max_stdout_bytes=64 * 1024,
                    max_stderr_bytes=1024 * 1024,
                )

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
from src.device_monitor import get_device_guard_lock
from src.injector import InjectionTargetGuard, TextInjector
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
) -> object:
    module = import_provider_runtime_module("assemblyai_realtime", "pipecat.services.assemblyai.stt")
    service_cls = getattr(module, "AssemblyAISTTService", None)
    if service_cls is None:
        raise RuntimeError("AssemblyAI Pipecat STT service is unavailable in this build.")

    settings_cls = getattr(service_cls, "Settings", None)
    language_code = assemblyai_universal_35_language_code(Config.LANGUAGE)
    keyterms = build_keyterms_from_vocab(Config.CUSTOM_VOCAB)

    if settings_cls is None:
        raise RuntimeError("AssemblyAI realtime transcription requires Pipecat 1.5.0.")

    settings_candidates = {"model": Config.ASSEMBLYAI_RT_MODEL}
    if language_code:
        settings_candidates["language_code"] = language_code
    if keyterms:
        settings_candidates["keyterms_prompt"] = keyterms[:100]
    settings_candidates["speaker_labels"] = False
    settings_kwargs = _filter_supported_kwargs(settings_cls, settings_candidates)
    settings = settings_cls(**settings_kwargs)
    service_candidates = {
        "api_key": api_key,
        "sample_rate": Config.SAMPLE_RATE,
        "settings": settings,
        "vad_force_turn_endpoint": True,
    }
    if language_code and "language_code" not in settings_kwargs:
        service_candidates["language"] = _selected_language()
    return service_cls(**_filter_supported_kwargs(service_cls, service_candidates))


def _load_soniox_realtime_classes():
    global SonioxSTTService, SonioxContextObject
    if SonioxSTTService:
        return SonioxSTTService, SonioxContextObject
    module = import_provider_runtime_module("soniox", "pipecat.services.soniox.stt")
    SonioxSTTService = getattr(module, "SonioxSTTService", None)
    SonioxContextObject = getattr(module, "SonioxContextObject", None)
    return SonioxSTTService, SonioxContextObject


def _normalize_device_name(name: str) -> str:
    return normalize_device_name(name)


_MIC_DEVICE_CACHE_LOCK = threading.Lock()
_MIC_DEVICE_CACHE: dict[tuple[int, str, str, int, int], tuple[float, str]] = {}


def invalidate_mic_device_resolution_cache() -> None:
    """Clear cached microphone-name-to-index resolutions."""
    with _MIC_DEVICE_CACHE_LOCK:
        _MIC_DEVICE_CACHE.clear()


def _mic_device_resolution_cache_ttl() -> float:
    return _env_float(
        "SCRIBER_MIC_DEVICE_CACHE_TTL_SEC",
        10.0,
        minimum=0.0,
        maximum=3600.0,
    )


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
    with get_device_guard_lock():
        resolved = resolve_input_microphone_device(
            sd,
            device_name=device_name,
            favorite_name=getattr(Config, "FAVORITE_MIC", "") or "",
            sample_rate=sample_rate,
            channels=requested_channels,
            logger=logger,
        )
    return remember(resolved)


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

        # Pipecat broadcasts turn frames in both directions. Count only the
        # canonical downstream VAD path so a downstream UserTurnProcessor's
        # generic frames cannot double the diagnostics on their upstream leg.
        if self.enabled and direction == FrameDirection.DOWNSTREAM:
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
    VADUserStarted/VADUserStopped frames to decide when to upload buffered audio. By
    default Scriber keeps one recording-wide segment so the stop hotkey
    transcribes the whole dictation. When VAD is disabled, this gate creates the
    required start/stop frames itself without loading a local analyzer.
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
                await self.push_frame(VADUserStartedSpeakingFrame(), direction)
            return

        if self.vad_segmentation_enabled:
            if isinstance(frame, VADUserStartedSpeakingFrame):
                self._vad_user_speaking = True
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self._vad_user_speaking = False
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)):
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
            await self.push_frame(VADUserStoppedSpeakingFrame(), direction)
            return True

        if self._whole_recording_closed:
            return False
        if not self._whole_recording_open:
            self._whole_recording_open = True
            await self.push_frame(VADUserStartedSpeakingFrame(), direction)
        self._whole_recording_closed = True
        logger.debug("Closing recording-wide segment for segmented STT service")
        await self.push_frame(VADUserStoppedSpeakingFrame(), direction)
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
        direct_file_speaker_diarization: bool | None = None,
        text_injection_enabled: bool = True,
        execution_route: Mapping[str, Any] | None = None,
        direct_file_expected_duration_seconds: float | None = None,
        injection_target_guard: InjectionTargetGuard | None = None,
        injection_method_override: str | None = None,
        azure_mai_raw_transport=None,
        on_provider_response_complete: Optional[Callable[[], None]] = None,
        soniox_replay_url: str | None = None,
        soniox_replay_final_message_sha256: str | None = None,
        on_soniox_last_final_token_received: Optional[Callable[[], None]] = None,
        soniox_replay_model: str | None = None,
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
        # Direct file jobs historically enable diarization by default. Meeting
        # finalization overrides this per track so the single-user mic is plain
        # while shared system audio remains diarized.
        self.direct_file_speaker_diarization = (
            True if direct_file_speaker_diarization is None
            else bool(direct_file_speaker_diarization)
        )
        self.text_injection_enabled = bool(text_injection_enabled)
        self.injection_target_guard = injection_target_guard
        self.injection_method_override = (
            str(injection_method_override or "").strip().lower() or None
        )
        self.azure_mai_raw_transport = azure_mai_raw_transport
        self.on_provider_response_complete = on_provider_response_complete
        self.soniox_replay_url = str(soniox_replay_url or "").strip() or None
        self.soniox_replay_final_message_sha256 = (
            str(soniox_replay_final_message_sha256 or "").strip().lower() or None
        )
        self.on_soniox_last_final_token_received = on_soniox_last_final_token_received
        self.soniox_replay_model = str(soniox_replay_model or "").strip() or None
        replay_parts = (
            self.soniox_replay_url,
            self.soniox_replay_final_message_sha256,
            self.on_soniox_last_final_token_received,
            self.soniox_replay_model,
        )
        if any(part is not None for part in replay_parts):
            if not all(part is not None for part in replay_parts):
                raise ValueError("Soniox replay configuration must be complete")
            if not re.fullmatch(
                r"ws://127\.0\.0\.1:[1-9][0-9]{0,4}/transcribe-websocket",
                self.soniox_replay_url or "",
            ):
                raise ValueError("Soniox replay URL must be an IPv4 loopback endpoint")
            port_text = (self.soniox_replay_url or "").split(":", 2)[-1].split("/", 1)[0]
            if int(port_text) > 65535:
                raise ValueError("Soniox replay URL port is invalid")
            if not re.fullmatch(
                r"[0-9a-f]{64}",
                self.soniox_replay_final_message_sha256 or "",
            ):
                raise ValueError("Soniox replay final-message digest is invalid")
            if not callable(self.on_soniox_last_final_token_received):
                raise ValueError("Soniox replay receive callback is required")
            if self.soniox_replay_model != "stt-rt-v5":
                raise ValueError("Soniox replay model must be stt-rt-v5")
        # File/YouTube/meeting jobs pass a persisted immutable route here.  Live
        # dictation intentionally keeps ``None`` and continues to read current
        # settings at session start.
        self.execution_route = dict(execution_route) if execution_route is not None else None
        try:
            expected_duration = float(direct_file_expected_duration_seconds or 0.0)
        except (TypeError, ValueError):
            expected_duration = 0.0
        self.direct_file_expected_duration_seconds = (
            expected_duration
            if math.isfinite(expected_duration) and expected_duration > 0.0
            else 0.0
        )
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
        # Provider-native timing/diarization data for meeting finalization.
        # It remains process-local and is not exposed through the REST API.
        self.last_structured_transcript_payload: dict[str, Any] | None = None

    def _execution_value(self, key: str, fallback: Any) -> Any:
        route = self.execution_route
        if route is not None and key in route:
            return route[key]
        return fallback

    def _execution_language(self) -> str:
        return str(self._execution_value("language", Config.LANGUAGE) or "")

    def _execution_selected_language(self) -> str | None:
        value = self._execution_language().strip()
        return None if not value or value.lower() == "auto" else value

    def _execution_custom_vocab(self) -> str:
        return str(self._execution_value("custom_vocab", Config.CUSTOM_VOCAB) or "")

    def _execution_model(self, fallback: str) -> str:
        value = str(self._execution_value("model", fallback) or "").strip()
        return value or fallback

    def _direct_file_batch_timeout_seconds(self) -> float:
        """Bound a provider request/job budget while retaining the 15-minute floor."""
        duration = self.direct_file_expected_duration_seconds
        return min(14_400.0, max(900.0, 300.0 + duration * 0.5))

    def _direct_file_upload_timeout_seconds(self) -> float:
        """Allow long compressed meeting tracks to upload over modest connections."""
        duration = self.direct_file_expected_duration_seconds
        return min(3_600.0, max(300.0, 120.0 + duration / 12.0))

    def _direct_file_poll_timeout_seconds(self) -> float:
        """Bound asynchronous provider polling while supporting five-hour inputs."""
        duration = self.direct_file_expected_duration_seconds
        return min(14_400.0, max(600.0, 300.0 + duration * 0.5))

    def _direct_file_workflow_timeout_seconds(
        self, *, minimum_seconds: float = 600.0
    ) -> float:
        return direct_file_workflow_timeout_seconds(
            self.direct_file_expected_duration_seconds,
            minimum_seconds=minimum_seconds,
        )

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
                    VADUserStoppedSpeakingFrame(),
                    direction=FrameDirection.DOWNSTREAM,
                )
                return True
        except Exception as exc:
            logger.debug(f"Segmented STT fallback flush warning: {exc}")
        return False

    async def _flush_live_vad_finalization_turn(self) -> bool:
        """Finalize a live streaming turn when the hotkey stops while speech is active."""
        if (
            not self.pipeline
            or self._vad_observer is None
            or not _live_service_needs_local_vad(self.service_name)
        ):
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
                VADUserStoppedSpeakingFrame(),
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
            soniox_api_key = _get_api_key("soniox")
            if not soniox_api_key and self.soniox_replay_url is None:
                raise ValueError("Soniox API Key is missing.")
            use_async = (
                False
                if self.soniox_replay_url is not None
                else self.service_name == "soniox_async" or Config.SONIOX_MODE == "async"
            )
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
            soniox_service_cls, soniox_context_cls = _load_soniox_realtime_classes()
            if not soniox_service_cls: raise RuntimeError("SonioxSTTService not available.")
            settings_cls = getattr(soniox_service_cls, "Settings", None)
            if settings_cls is None:
                raise RuntimeError("Soniox realtime transcription requires Pipecat 1.5.0.")
            lang_hint = Language.EN if self.soniox_replay_url is not None else _selected_language()
            rt_model = self.soniox_replay_model or Config.SONIOX_RT_MODEL
            settings_candidates: dict[str, Any] = {
                "model": rt_model,
                "language_hints": [lang_hint] if lang_hint else None,
                "enable_speaker_diarization": self.enable_speaker_diarization if for_file else False,
            }
            soniox_custom_vocab = (
                "" if self.soniox_replay_url is not None else Config.CUSTOM_VOCAB
            )
            if soniox_custom_vocab and soniox_context_cls:
                terms = [t.strip() for t in soniox_custom_vocab.split(",") if t.strip()]
                if terms:
                    logger.info(f"Applying custom vocabulary: {terms}")
                    settings_candidates["context"] = soniox_context_cls(terms=terms)
            settings = settings_cls(**_filter_supported_kwargs(settings_cls, settings_candidates))
            logger.info("Creating SonioxSTTService with Pipecat 1.5 settings and forced turn endpointing")
            service_kwargs: dict[str, Any] = {
                "api_key": (
                    "local-replay"
                    if self.soniox_replay_url is not None
                    else soniox_api_key
                ),
                "sample_rate": Config.SAMPLE_RATE,
                "settings": settings,
                "vad_force_turn_endpoint": True,
            }
            if self.soniox_replay_url is not None:
                service_kwargs["url"] = self.soniox_replay_url
            service = soniox_service_cls(**service_kwargs)
            if self.soniox_replay_url is not None:
                from src.runtime.provider_replay import (
                    install_soniox_replay_receive_observer,
                )

                install_soniox_replay_receive_observer(
                    service,
                    final_message_sha256=(
                        self.soniox_replay_final_message_sha256 or ""
                    ),
                    on_last_final_token_received=(
                        self.on_soniox_last_final_token_received
                    ),
                )
            return service

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
            )
        
        elif self.service_name == "google":
            # Lazy import - only loaded when Google is used
            module = import_provider_runtime_module("google", "pipecat.services.google.stt")
            GoogleSTTService = module.GoogleSTTService
            credentials_path = str(Config.GOOGLE_APPLICATION_CREDENTIALS or "").strip()
            if not credentials_path:
                raise ValueError("Google Cloud credentials path is missing.")
            settings_cls = getattr(GoogleSTTService, "Settings", None)
            if settings_cls is None:
                raise RuntimeError("Google Cloud streaming transcription requires Pipecat 1.5.0.")
            selected_language = _selected_language()
            settings_values = {
                "enable_automatic_punctuation": True,
                "enable_interim_results": True,
                "enable_voice_activity_events": False,
            }
            if selected_language:
                settings_values["languages"] = [selected_language]
            settings = settings_cls(
                **_filter_supported_kwargs(settings_cls, settings_values)
            )
            return GoogleSTTService(
                credentials_path=credentials_path,
                sample_rate=Config.SAMPLE_RATE,
                settings=settings,
            )

        elif self.service_name == "gemini_stt":
            api_key = Config.get_api_key("gemini_stt")
            if not api_key:
                raise ValueError("Gemini API key is missing.")
            logger.info("Using Gemini API audio transcription mode")
            return GeminiAsyncProcessor(
                api_key=api_key,
                model=self._execution_model(Config.GEMINI_STT_MODEL),
                language=self._execution_language(),
                custom_vocab=self._execution_custom_vocab(),
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
                raise RuntimeError("ElevenLabs Live requires Pipecat 1.5.0.")
            commit_strategy = getattr(getattr(module, "CommitStrategy", object), "MANUAL", None)
            settings_cls = getattr(realtime_cls, "Settings", None)
            if settings_cls is None or commit_strategy is None:
                raise RuntimeError("ElevenLabs Live requires Pipecat 1.5.0 settings support.")
            language = _selected_language()
            settings = settings_cls(
                **_filter_supported_kwargs(
                    settings_cls,
                    {
                        "model": "scribe_v2_realtime",
                        "language": language,
                        "keyterms": build_keyterms_from_vocab(Config.CUSTOM_VOCAB)[:100] or None,
                    },
                )
            )

            logger.info(f"ElevenLabs realtime STT: Using language={_selected_language_code() or 'auto-detect'}")

            return realtime_cls(
                **_filter_supported_kwargs(
                    realtime_cls,
                    {
                        "api_key": _get_api_key("elevenlabs"),
                        "sample_rate": Config.SAMPLE_RATE,
                        "commit_strategy": commit_strategy,
                        "settings": settings,
                    },
                )
            )
        
        elif self.service_name == "deepgram":
            # Lazy import - only loaded when Deepgram is used
            module = import_provider_runtime_module("deepgram", "pipecat.services.deepgram.stt")
            DeepgramSTTService = module.DeepgramSTTService
            if not _get_api_key("deepgram"): raise ValueError("Deepgram API Key is missing.")
            settings_cls = getattr(DeepgramSTTService, "Settings", None)
            if settings_cls is None:
                raise RuntimeError("Deepgram streaming transcription requires Pipecat 1.5.0.")
            deepgram_settings = settings_cls(
                **_filter_supported_kwargs(
                    settings_cls,
                    {
                        "model": self._execution_model(Config.DEEPGRAM_MODEL),
                        "language": _selected_language(),
                        "interim_results": True,
                        "smart_format": True,
                        "punctuate": True,
                        "diarize": self.enable_speaker_diarization if for_file else False,
                    },
                )
            )

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
                encoding="linear16",
                channels=Config.CHANNELS,
                sample_rate=Config.SAMPLE_RATE,
                settings=deepgram_settings,
            )

        elif self.service_name == "deepgram_async":
            if not _get_api_key("deepgram"):
                raise ValueError("Deepgram API Key is missing.")
            logger.info("Using Deepgram async pre-recorded transcription mode")
            return DeepgramAsyncProcessor(
                api_key=_get_api_key("deepgram"),
                model=self._execution_model(Config.DEEPGRAM_MODEL),
                language=self._execution_language(),
                custom_vocab=self._execution_custom_vocab(),
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
                raise RuntimeError("OpenAI Realtime STT requires Pipecat 1.5.0.")
            settings_cls = getattr(OpenAIRealtimeSTTService, "Settings", None)
            if settings_cls is None:
                raise RuntimeError("OpenAI Realtime STT requires Pipecat 1.5.0 settings support.")
            settings = settings_cls(model=Config.OPENAI_REALTIME_STT_MODEL, language=_selected_language())
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
            if not api_key and self.azure_mai_raw_transport is None:
                raise ValueError("Azure MAI Speech Key is missing.")
            logger.info("Using Microsoft MAI Transcribe Pipecat STT service")
            return AzureMaiTranscribeSTTService(
                speech_key=api_key or "local-replay",
                region=validate_azure_mai_region(getattr(Config, "AZURE_MAI_REGION", None)),
                language=self._execution_language(),
                model=self._execution_model(Config.AZURE_MAI_MODEL),
                custom_vocab=self._execution_custom_vocab(),
                session=session,
                on_progress=self.on_progress,
                raw_transport=self.azure_mai_raw_transport,
                on_response_complete=self.on_provider_response_complete,
            )
        
        elif self.service_name == "gladia":
            # Lazy import - only loaded when Gladia is used
            module = import_provider_runtime_module("gladia", "pipecat.services.gladia.stt")
            GladiaSTTService = module.GladiaSTTService
            if not _get_api_key("gladia"): raise ValueError("Gladia API Key is missing.")
            pipeline_ref = self

            class ScriberGladiaSTTService(GladiaSTTService):
                async def stop(self, frame: EndFrame):
                    await STTService.stop(self, frame)
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

                    await self._disconnect()

            settings_cls = getattr(GladiaSTTService, "Settings", None)
            if settings_cls is None:
                raise RuntimeError("Gladia realtime transcription requires Pipecat 1.5.0.")
            gladia_settings = settings_cls(
                **_filter_supported_kwargs(
                    settings_cls,
                    {
                        "model": "solaria-1",
                        "language": _selected_language(),
                        "enable_vad": False,
                    },
                )
            )

            return ScriberGladiaSTTService(
                **_filter_supported_kwargs(
                    GladiaSTTService,
                    {
                        "api_key": _get_api_key("gladia"),
                        "sample_rate": Config.SAMPLE_RATE,
                        "channels": Config.CHANNELS,
                        "settings": gladia_settings,
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
            settings_cls = getattr(GroqSTTService, "Settings", None)
            if settings_cls is None:
                raise RuntimeError("Groq transcription requires Pipecat 1.5.0.")
            settings = settings_cls(
                **_filter_supported_kwargs(
                    settings_cls,
                    {
                        "language": _selected_language(),
                        "prompt": (
                            "Likely vocabulary: " + Config.CUSTOM_VOCAB
                            if Config.CUSTOM_VOCAB
                            else None
                        ),
                    },
                )
            )
            return GroqSTTService(api_key=_get_api_key("groq"), settings=settings)
        
        elif self.service_name == "speechmatics":
            # Lazy import - only loaded when Speechmatics is used
            module = import_provider_runtime_module("speechmatics", "pipecat.services.speechmatics.stt")
            SpeechmaticsSTTService = module.SpeechmaticsSTTService
            if not _get_api_key("speechmatics"): raise ValueError("Speechmatics API Key is missing.")
            settings_cls = getattr(SpeechmaticsSTTService, "Settings", None)
            if settings_cls is None:
                raise RuntimeError("Speechmatics realtime transcription requires Pipecat 1.5.0.")
            settings = settings_cls(
                **_filter_supported_kwargs(
                    settings_cls,
                    {
                        "language": _selected_language() or Language.EN,
                        "enable_diarization": self.enable_speaker_diarization if for_file else False,
                        "speaker_active_format": "[Speaker {speaker_id}]: {text}",
                        "speaker_passive_format": "[Speaker {speaker_id}]: {text}",
                    },
                )
            )
            return SpeechmaticsSTTService(
                api_key=_get_api_key("speechmatics"),
                sample_rate=Config.SAMPLE_RATE,
                settings=settings,
            )

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
            from src.onnx_local_service import OnnxLocalBufferedSTTService
            if for_file:
                return OnnxLocalBufferedSTTService(
                    model_name=self._execution_model(Config.ONNX_MODEL),
                    language=self._execution_language(),
                    quantization=Config.ONNX_QUANTIZATION,
                    sample_rate=Config.SAMPLE_RATE,
                    channels=Config.CHANNELS,
                    # Keep file chunks within the duration exercised by the
                    # local model. Each full chunk emits a final frame and the
                    # terminal frame flushes the remainder.
                    max_buffer_secs=30,
                    flush_on_limit=True,
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

                vad_analyzer = None
                # The Settings switch is the master opt-in for local Silero VAD.
                # When it is off, HTTP-style services still receive one synthetic
                # recording-wide turn from SegmentedSTTRecordingGate, while async
                # services finalize normally on stop. Soniox SmartTurn depends on
                # the same explicit VAD boundaries and is therefore disabled too.
                needs_vad, uses_smart_turn = _live_analyzer_requirements(
                    self.service_name,
                    segmented_service=isinstance(stt_service, SegmentedSTTService),
                    provider_replay=self.soniox_replay_url is not None,
                )
                    
                if needs_vad:
                    vad_analyzer = _create_vad_analyzer(
                        quiet_mic=(
                            self.service_name == "onnx_local"
                            or _live_service_uses_async_finalization(self.service_name)
                        )
                    )
                    if not vad_analyzer:
                        logger.warning("VAD analyzer required but not available; transcripts may not finalize properly.")

                vad_processor = (
                    VADProcessor(vad_analyzer=vad_analyzer)
                    if vad_analyzer is not None
                    else None
                )
                smart_turn_processor = None
                if uses_smart_turn:
                    if vad_processor is None:
                        logger.warning(
                            "SmartTurn V3 disabled for this recording because Silero VAD is unavailable"
                        )
                    else:
                        smart_turn_processor = _create_soniox_smart_turn_processor()
                        if smart_turn_processor is None:
                            logger.warning("SmartTurn V3 is unavailable for this recording")

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
                    device=_resolve_mic_device(Config.MIC_DEVICE),
                    keep_alive=use_prewarm_for_capture,
                    prewarm_manager=self.mic_prewarm_manager,
                    on_audio_level=self.on_audio_level,
                    on_ready=self.on_mic_ready,
                    on_last_audio_chunk_sent=self.on_last_audio_chunk_sent,
                )

                inject_immediately = (
                    True
                    if self.soniox_replay_url is not None
                    else injects_immediately_in_live_mode(self.service_name) and not (
                        self.service_name == "soniox" and Config.SONIOX_MODE == "async"
                    )
                )
                text_injector = TextInjector(
                    inject_immediately=inject_immediately,
                    enabled=self.text_injection_enabled,
                    on_injected=self.on_text_injected,
                    on_injection_marker=self.on_injection_marker,
                    target_guard=self.injection_target_guard,
                    injection_method=self.injection_method_override,
                )
                self.text_injector = text_injector
                needs_soniox_realtime_final_signal = (
                    self.service_name == "soniox"
                    and (
                        self.soniox_replay_url is not None
                        or Config.SONIOX_MODE != "async"
                    )
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

                # Providers that normally consume local VAD boundaries still
                # need one deterministic turn when Silero is off. The same gate
                # synthesizes that recording-wide start/stop pair, preserving
                # provider finalization without loading a model.
                needs_recording_gate = _live_recording_gate_needed(
                    self.service_name,
                    segmented_service=isinstance(stt_service, SegmentedSTTService),
                    vad_attached=needs_vad,
                )
                segmented_gate = (
                    SegmentedSTTRecordingGate(
                        vad_segmentation_enabled=bool(Config.SEGMENT_SPEECH_WITH_VAD)
                    )
                    if needs_recording_gate
                    else None
                )

                if vad_processor is not None:
                    self._vad_observer = PipecatVadSpeechObserver(enabled=True)
                steps = _ordered_live_pipeline_steps(
                    audio_input=self.audio_input,
                    vad_processor=vad_processor,
                    vad_observer=self._vad_observer,
                    segmented_gate=segmented_gate,
                    stt_service=stt_service,
                    smart_turn_processor=smart_turn_processor,
                    error_handler=error_handler,
                    transcript_callback=transcript_cb,
                    text_injector=text_injector,
                )

                self.pipeline = Pipeline(steps)
                self.task = PipelineTask(
                    self.pipeline,
                    params=PipelineParams(allow_interruptions=True),
                    # Scriber does not expose RTVI or Pipecat turn-tracking. Keep
                    # those framework processors out of every live task so they
                    # cannot add per-frame work or retain unused conversation state.
                    enable_rtvi=False,
                    enable_turn_tracking=False,
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
            # Refill only after the Pipecat runner and physical capture have
            # completed teardown. The cleaned session analyzers stay owned by
            # the old pipeline and are never returned to this warmup pool.
            if _analyzer_warmup_enabled():
                needs_vad, uses_smart_turn = _live_analyzer_requirements(
                    self.service_name,
                    provider_replay=self.soniox_replay_url is not None,
                )
                _AnalyzerCache.request_background_replenish(
                    include_vad=needs_vad,
                    include_smart_turn=uses_smart_turn,
                )
            self._start_done.set()

    async def _cleanup_aborted_file_pipeline(
        self,
        run_task: asyncio.Task | None,
        file_input: FfmpegAudioFileInput | None,
    ) -> None:
        if self.task and not self.task.has_finished():
            try:
                await asyncio.wait_for(
                    self.task.cancel(reason="file transcription aborted"),
                    timeout=5.0,
                )
            except Exception as exc:
                logger.debug(f"File pipeline task cancellation warning: {exc}")
        if self.runner:
            try:
                await asyncio.wait_for(self.runner.cancel(), timeout=5.0)
            except Exception as exc:
                logger.debug(f"File pipeline runner cancellation warning: {exc}")
        if run_task is not None and not run_task.done():
            run_task.cancel()
        if run_task is not None:
            await asyncio.gather(run_task, return_exceptions=True)
        if file_input is not None:
            try:
                await asyncio.wait_for(file_input.stop(EndFrame()), timeout=7.0)
            except Exception as exc:
                logger.debug(f"File input abort cleanup warning: {exc}")

    async def transcribe_file(self, file_path: str) -> None:
        if self.is_active:
            return
        logger.info(f"Transcribing audio file with {self.service_name}: {file_path}")
        self._start_done.clear()
        file_input: FfmpegAudioFileInput | None = None
        run_task: asyncio.Task | None = None
        pipeline_finished = False
        try:
            async with aiohttp.ClientSession() as session:
                stt_service = self._create_stt_service(session, for_file=True)

                file_input = FfmpegAudioFileInput(
                    file_path,
                    sample_rate=Config.SAMPLE_RATE,
                    channels=Config.CHANNELS,
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
                    enable_rtvi=False,
                    enable_turn_tracking=False,
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
                pipeline_finished = True

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
            if not pipeline_finished:
                await self._cleanup_aborted_file_pipeline(run_task, file_input)
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
            batch_timeout_seconds = self._direct_file_batch_timeout_seconds()
            upload_timeout_seconds = self._direct_file_upload_timeout_seconds()
            poll_timeout_seconds = self._direct_file_poll_timeout_seconds()
            if self.direct_file_expected_duration_seconds:
                logger.info(
                    "Direct-file timeout budget: "
                    f"duration={self.direct_file_expected_duration_seconds:.1f}s, "
                    f"upload={upload_timeout_seconds:.1f}s, "
                    f"batch={batch_timeout_seconds:.1f}s, "
                    f"poll={poll_timeout_seconds:.1f}s"
                )

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
                            language=self._execution_language(),
                            custom_vocab=self._execution_custom_vocab(),
                            speaker_labels=self.direct_file_speaker_diarization,
                            model=self._execution_model(Config.ASSEMBLYAI_ASYNC_MODEL),
                            on_progress=self.on_progress,
                            timeout_secs=batch_timeout_seconds,
                            upload_timeout_secs=upload_timeout_seconds,
                        )

                self.last_structured_transcript_payload = payload
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

                language = self._execution_selected_language()

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
                            model=self._execution_model(Config.MISTRAL_ASYNC_MODEL or "voxtral-mini-2602"),
                            file_content=f,
                            filename=path.name,
                            content_type=content_type,
                            language=language,
                            context_bias=self._execution_custom_vocab(),
                            diarize=self.direct_file_speaker_diarization,
                            timestamp_granularities=["segment"],
                            timeout_secs=batch_timeout_seconds,
                        )

                text = str(payload.get("text") or "").strip()
                self.last_structured_transcript_payload = payload
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
                            language=self._execution_language(),
                            word_timestamps=True,
                            diarize=self.direct_file_speaker_diarization,
                            on_progress=self.on_progress,
                            timeout_secs=batch_timeout_seconds,
                        )

                self.last_structured_transcript_payload = payload
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
                            model=self._execution_model(Config.DEEPGRAM_MODEL),
                            language=self._execution_language(),
                            custom_vocab=self._execution_custom_vocab(),
                            diarize=self.direct_file_speaker_diarization,
                            on_progress=self.on_progress,
                            timeout_secs=batch_timeout_seconds,
                        )

                self.last_structured_transcript_payload = payload
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
                            model=self._execution_model(Config.OPENAI_STT_MODEL),
                            language=self._execution_language(),
                            custom_vocab=self._execution_custom_vocab(),
                            diarize=self.direct_file_speaker_diarization,
                            on_progress=self.on_progress,
                            timeout_secs=batch_timeout_seconds,
                        )

                text = openai_transcript_payload_to_text(
                    payload,
                    prefer_speaker_labels=True,
                )
                self.last_structured_transcript_payload = payload
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
                            model=self._execution_model(Config.GEMINI_STT_MODEL),
                            language=self._execution_language(),
                            custom_vocab=self._execution_custom_vocab(),
                            diarize=self.direct_file_speaker_diarization,
                            on_progress=self.on_progress,
                            timeout_secs=batch_timeout_seconds,
                        )

                text = gemini_transcript_payload_to_text(payload)
                self.last_structured_transcript_payload = payload
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
                                language=self._execution_language(),
                                model=self._execution_model(Config.AZURE_MAI_MODEL),
                                custom_vocab=self._execution_custom_vocab(),
                                on_progress=self.on_progress,
                                timeout_secs=batch_timeout_seconds,
                            )

                text = azure_mai_transcript_payload_to_text(payload)
                self.last_structured_transcript_payload = payload
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
                            language=self._execution_language(),
                            custom_vocab=self._execution_custom_vocab(),
                            diarize=self.direct_file_speaker_diarization,
                            on_progress=self.on_progress,
                            timeout_secs=batch_timeout_seconds,
                        )

                text = gladia_transcript_payload_to_text(
                    payload,
                    prefer_speaker_labels=True,
                )
                self.last_structured_transcript_payload = payload
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
                            language=self._execution_language(),
                            custom_vocab=self._execution_custom_vocab(),
                            diarize=self.direct_file_speaker_diarization,
                            on_progress=self.on_progress,
                            timeout_secs=batch_timeout_seconds,
                        )

                text = speechmatics_transcript_payload_to_text(
                    payload,
                    prefer_speaker_labels=True,
                )
                self.last_structured_transcript_payload = payload
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
            model = self._execution_model(
                Config.SONIOX_ASYNC_MODEL or Config.DEFAULT_SONIOX_ASYNC_MODEL
            )
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
                            timeout=aiohttp.ClientTimeout(total=upload_timeout_seconds),
                        ) as resp:
                            resp.raise_for_status()
                            file_id = (await read_response_json_limited(resp, 64 * 1024 * 1024))["id"]

                    # Start transcription with speaker diarization enabled for file/youtube
                    payload = {"file_id": file_id, "model": model}
                    # Build proper context object if custom_vocab is provided
                    custom_vocab = self._execution_custom_vocab()
                    if custom_vocab:
                        terms = [t.strip() for t in custom_vocab.split(",") if t.strip()]
                        if terms:
                            payload["context"] = {"terms": terms}
                    # Enable speaker diarization for file/youtube transcription
                    payload["enable_speaker_diarization"] = self.direct_file_speaker_diarization

                    async with session.post(
                        f"{base_url}/transcriptions",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp2:
                        resp2.raise_for_status()
                        transcription_id = (await read_response_json_limited(resp2, 64 * 1024 * 1024))["id"]

                    # Poll for completion
                    if self.on_progress:
                        self.on_progress("Processing transcription...")

                    done_statuses = {"completed", "done", "succeeded", "success"}
                    error_statuses = {"error", "failed", "canceled", "cancelled"}
                    poll_start = asyncio.get_running_loop().time()
                    poll_timeout = poll_timeout_seconds
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
                            status_payload = await read_response_json_limited(r, 64 * 1024 * 1024)
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
                        transcript_payload = await read_response_json_limited(r3, 64 * 1024 * 1024)
                        self.last_structured_transcript_payload = transcript_payload

                        # Parse speaker diarization if available
                        tokens = transcript_payload.get("tokens", [])
                        if tokens and any(t.get("speaker") not in (None, "") for t in tokens):
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
            or (
                self.service_name == "soniox"
                and self.soniox_replay_url is None
                and Config.SONIOX_MODE == "async"
            )
        )
        is_async_finalization = (
            False
            if self.soniox_replay_url is not None
            else _live_service_uses_async_finalization(self.service_name)
        )
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
            # Hand active capture back to the app-level idle prewarm before
            # closing this session. With MIC_ALWAYS_ON the native layer starts
            # the replacement WASAPI client first, so the Windows privacy light
            # remains continuous while transcription finalizes.
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

        # Always retrieve the stop task result. A fast failure can complete
        # before _start_done is observed; skipping an already-done task leaves
        # its exception unconsumed and produces noisy asyncio warnings.
        if stop_future is not None:
            stop_result = (await asyncio.gather(stop_future, return_exceptions=True))[0]
            if isinstance(stop_result, BaseException) and not isinstance(
                stop_result,
                asyncio.CancelledError,
            ):
                logger.debug(f"Pipeline stop task warning: {stop_result}")

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
