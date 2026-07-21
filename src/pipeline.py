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
import hashlib
from typing import Any, BinaryIO, Callable, Mapping, Optional

from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.stt_service import SegmentedSTTService, STTService
from pipecat.frames.frames import (
    SystemFrame,
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

from src.audio_prepare import (
    PreparedProviderAudio,
    ProviderAudioPreparationError,
    prepare_provider_audio_file,
)
from src.core.provider_audio_formats import (
    AudioInputFormat,
    SPEECHMATICS_BATCH_DEFAULT_BASE_URL,
    SPEECHMATICS_REALTIME_DEFAULT_BASE_URL,
    ProviderAudioCapabilityError,
    ProviderAudioInputCapabilities,
    ProviderAudioRouteKind,
    batch_route_for_provider,
    coerce_audio_input_format,
    realtime_pcm_preparation_implementation,
    realtime_route_for_provider,
    require_exact_audio_input_format,
    resolve_batch_provider_audio_capabilities,
    resolve_provider_audio_capabilities,
    speechmatics_realtime_base_url,
)
from src.core.provider_capabilities import get_capabilities
from src.runtime.media_tools import find_media_tool
from src.runtime.provider_dependencies import import_provider_runtime_module
from src.runtime.provider_http import ProviderHttpTransport
from src.runtime.smart_turn_mel import install_smart_turn_mel_acceleration
from src.runtime.subprocess_utils import communicate_or_kill_on_cancel, hidden_subprocess_kwargs
from src.runtime.http_response import read_response_json_limited, read_response_text_limited
from src.runtime.audio_spool import append_pcm_frame, close_pcm_spool, create_pcm_spool, pcm_stream_to_wav
from src.runtime.env_values import env_float as _safe_env_float
from src.soniox_region import soniox_realtime_websocket_url, soniox_rest_api_base_url


_SONIOX_MANUAL_FINALIZE_MESSAGE = '{"type": "finalize"}'
_GROQ_OPENAI_V1_BASE_URL = "https://api.groq.com/openai/v1"
_PROVIDER_INGRESS_DRAIN_SERVICES = frozenset(
    {"azure_mai", "speechmatics_async"}
)
_PROVIDER_INGRESS_DRAIN_TIMEOUT_SECONDS = 2.0
_PROVIDER_INGRESS_ABORT_TIMEOUT_SECONDS = 2.0


def _speechmatics_capture_time_wav_enabled() -> bool:
    """Keep Rust WAV opt-in after the installed A/B no-regression check."""

    return os.getenv(
        "SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV",
        "0",
    ).strip().lower() not in {"0", "false", "no", "off"}


def _rust_capture_wav_plan(
    service_name: str,
    *,
    sample_rate: int,
    channels: int,
    execution_route: Mapping[str, Any] | None,
    candidate_enabled: bool | None = None,
) -> dict[str, Any] | None:
    """Return the production Rust WAV plan for an exact WAV-upload route.

    Speechmatics batch is deliberately the first production consumer. Azure
    MAI and Soniox keep their separately measured preparation controls; this
    must not silently run a second encoder beside those routes.
    """

    if candidate_enabled is None:
        candidate_enabled = _speechmatics_capture_time_wav_enabled()
    if not candidate_enabled:
        return None
    if str(service_name or "").strip().lower() != "speechmatics_async":
        return None
    if not isinstance(execution_route, Mapping):
        return None
    if isinstance(sample_rate, bool) or isinstance(channels, bool):
        return None
    try:
        capability = resolve_provider_audio_capabilities(
            "speechmatics_async",
            "batch_v2",
            "enhanced",
        )
        require_exact_audio_input_format(
            capability,
            AudioInputFormat.WAV_PCM16,
            route_kind=ProviderAudioRouteKind.BATCH,
        )
    except ProviderAudioCapabilityError:
        return None

    default_endpoint = SPEECHMATICS_BATCH_DEFAULT_BASE_URL.rstrip("/")
    expected_endpoint_sha256 = hashlib.sha256(
        default_endpoint.encode("utf-8")
    ).hexdigest()
    exact_route = {
        "model": "batch-v2",
        "provider_route": capability.route,
        "audio_input_format": AudioInputFormat.WAV_PCM16.value,
        "provider_audio_capability_id": capability.capability_id,
        "provider_audio_capability_revision": capability.revision,
        "audio_selection_mode": "generated",
        "audio_preparation_implementation": "wav_pcm16_file_v1",
        "transport": "direct_upload",
        "provider_endpoint_sha256": expected_endpoint_sha256,
    }
    if any(
        str(execution_route.get(key) or "").strip() != expected
        for key, expected in exact_route.items()
    ):
        return None
    if execution_route.get("audio_input_format_verified") is not True:
        return None

    try:
        sample_rate = int(sample_rate)
        channels = int(channels)
    except (TypeError, ValueError):
        return None
    if not 8_000 <= sample_rate <= 192_000 or not 1 <= channels <= 16:
        return None
    block_align = channels * 2
    max_pcm_bytes = (64 * 1024 * 1024 // block_align) * block_align
    return {
        "schemaVersion": "1",
        "format": "wav_pcm16",
        "implementation": "wav_pcm16_file_v1",
        "sampleRate": sample_rate,
        "channels": channels,
        "bitsPerSample": 16,
        "queueCapacityFrames": 64,
        "maxPcmBytes": max_pcm_bytes,
    }

try:
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    HAS_SMART_TURN = True
except ImportError:
    LocalSmartTurnAnalyzerV3 = None
    HAS_SMART_TURN = False
    logger.debug("Optional Pipecat LocalSmartTurnAnalyzerV3 is not available")


def _create_local_smart_turn_analyzer(**kwargs: Any) -> Any:
    """Create SmartTurn with Scriber's compact-NumPy matrix acceleration."""

    try:
        install_smart_turn_mel_acceleration()
    except Exception as exc:
        # The no-BLAS fallback remains functionally correct if the tiny model
        # cannot be loaded. Release gates require the accelerated path.
        logger.warning(
            "SmartTurn mel acceleration unavailable; using NumPy fallback: {}",
            type(exc).__name__,
        )
    return LocalSmartTurnAnalyzerV3(**kwargs)

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
    _vad_generation = 0
    _smart_turn_generation = 0

    @staticmethod
    def _cleanup_unclaimed(analyzer: Any, *, label: str) -> None:
        """Best-effort cleanup for a constructed analyzer that lost its slot."""

        if analyzer is None:
            return
        cleanup = getattr(analyzer, "cleanup", None)
        if not callable(cleanup):
            return
        try:
            cleanup_result = cleanup()
            if inspect.isawaitable(cleanup_result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(cleanup_result)
                else:
                    loop.create_task(cleanup_result)
        except Exception as exc:
            logger.debug(f"Unused {label} analyzer cleanup failed: {exc}")

    @classmethod
    def prewarm(
        cls,
        *,
        include_vad: bool = True,
        include_smart_turn: bool = True,
    ) -> None:
        """Create at most one requested, unclaimed analyzer of each type."""
        with cls._lock:
            vad_generation = cls._vad_generation
            smart_turn_generation = cls._smart_turn_generation
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
            smart_turn_analyzer = _create_local_smart_turn_analyzer()

        unused_vad = None
        unused_smart_turn = None
        with cls._lock:
            if (
                vad_analyzer is not None
                and cls._vad_analyzer is None
                and cls._vad_generation == vad_generation
                and bool(Config.SEGMENT_SPEECH_WITH_VAD)
            ):
                cls._vad_analyzer = vad_analyzer
            else:
                unused_vad = vad_analyzer
            if (
                smart_turn_analyzer is not None
                and cls._smart_turn_analyzer is None
                and cls._smart_turn_generation == smart_turn_generation
                and bool(Config.SEGMENT_SPEECH_WITH_VAD)
            ):
                cls._smart_turn_analyzer = smart_turn_analyzer
            else:
                unused_smart_turn = smart_turn_analyzer
        cls._cleanup_unclaimed(unused_vad, label="Silero VAD")
        cls._cleanup_unclaimed(unused_smart_turn, label="SmartTurn")

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
        return _create_local_smart_turn_analyzer()

    @classmethod
    def clear_cache(cls):
        """Discard only unclaimed warmup instances."""
        with cls._lock:
            vad_analyzer = cls._vad_analyzer
            smart_turn_analyzer = cls._smart_turn_analyzer
            cls._vad_analyzer = None
            cls._smart_turn_analyzer = None
            cls._vad_generation += 1
            cls._smart_turn_generation += 1
            logger.debug("Unclaimed analyzer warmup cache cleared")
        cls._cleanup_unclaimed(vad_analyzer, label="Silero VAD")
        cls._cleanup_unclaimed(smart_turn_analyzer, label="SmartTurn")

    @classmethod
    def discard_vad_cache(cls) -> None:
        """Release an unused Silero warmup when the user disables VAD."""
        with cls._lock:
            vad_analyzer = cls._vad_analyzer
            cls._vad_analyzer = None
            # Invalidate a constructor that is currently running outside the
            # lock. Even if the setting is re-enabled before it returns, that
            # stale generation must not repopulate the discarded warm slot.
            cls._vad_generation += 1
        logger.debug("Unclaimed Silero VAD warmup cache cleared")
        cls._cleanup_unclaimed(vad_analyzer, label="Silero VAD")


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
            "modulate_async",
            "azure_mai",
            "assemblyai",
        }
        or (normalized == "soniox" and Config.SONIOX_MODE == "async")
    )


def _live_service_needs_local_vad(service_name: str) -> bool:
    normalized = str(service_name or "")
    return normalized in {"openai", "deepgram", "gladia", "elevenlabs"} or _live_service_uses_async_finalization(normalized)


def _live_service_uses_native_streaming(service_name: str) -> bool:
    """Whether the active Live Mic route is a provider-native stream."""

    normalized = str(service_name or "").strip().lower()
    if normalized == "soniox" and Config.SONIOX_MODE == "async":
        return False
    return bool(get_capabilities(normalized).supports_live_streaming)


LIVE_STT_STOP_VAD_FLUSH_BEFORE_END = "vad_flush_before_end"
LIVE_STT_STOP_END_FRAME_FINALIZES = "end_frame_finalizes"
LIVE_STT_STOP_PROVIDER_MANUAL = "provider_manual"


def _live_stt_stop_strategy(
    service_name: str,
    *,
    segmented_service: bool = False,
) -> str:
    """Describe how one live STT service produces its final result on stop."""
    normalized = str(service_name or "")
    if segmented_service or normalized in {"openai", "deepgram", "elevenlabs"}:
        return LIVE_STT_STOP_VAD_FLUSH_BEFORE_END
    if normalized == "soniox" and Config.SONIOX_MODE == "realtime":
        return LIVE_STT_STOP_PROVIDER_MANUAL
    return LIVE_STT_STOP_END_FRAME_FINALIZES


def _live_analyzer_requirements(
    service_name: str,
    *,
    segmented_service: bool = False,
    provider_replay: bool = False,
) -> tuple[bool, bool]:
    """Return ``(needs_vad, uses_smart_turn)`` for one Live Mic session."""
    # Provider-native realtime transports own their streaming/session
    # boundaries.  A persisted segmentation preference must never attach
    # Silero to them and turn short local pauses into premature provider
    # commits. The flag remains available for segmented and async routes where
    # it deliberately creates upload boundaries.
    if _live_service_uses_native_streaming(service_name):
        return False, False

    vad_enabled = bool(Config.SEGMENT_SPEECH_WITH_VAD)
    uses_smart_turn = False
    needs_vad = bool(
        vad_enabled
        and (
            segmented_service
            or _live_service_needs_local_vad(service_name)
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


def _live_analyzer_diagnostics(
    *,
    vad_processor: Any,
    segmented_gate: Any,
    segmented_provider: bool,
    native_realtime_provider: bool,
    stop_strategy: str,
    smart_turn_processor: Any,
) -> dict[str, Any]:
    """Return explicit, structured analyzer state for support diagnostics."""
    return {
        "sileroVadSettingEnabled": bool(Config.SEGMENT_SPEECH_WITH_VAD),
        "sileroVadAvailable": bool(
            HAS_SILERO_VAD and SileroVADAnalyzer is not None
        ),
        "sileroVadAttached": vad_processor is not None,
        "sileroVadEffectiveEnabled": bool(
            Config.SEGMENT_SPEECH_WITH_VAD and not native_realtime_provider
        ),
        "sileroSuppressedForNativeRealtime": bool(
            Config.SEGMENT_SPEECH_WITH_VAD and native_realtime_provider
        ),
        "recordingGateAttached": segmented_gate is not None,
        "syntheticRecordingBoundary": bool(
            segmented_gate is not None
            and not bool(
                getattr(segmented_gate, "vad_segmentation_enabled", False)
            )
        ),
        "segmentedProvider": bool(segmented_provider),
        "nativeRealtimeProvider": bool(native_realtime_provider),
        "stopStrategy": str(stop_strategy),
        "smartTurnAttached": smart_turn_processor is not None,
    }


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
    provider_ingress_drain: FrameProcessor | None = None,
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
    if provider_ingress_drain is not None:
        # InputAudioRawFrame is a Pipecat SystemFrame while EndFrame uses the
        # independent control-frame lane.  This post-STT acknowledgement point
        # lets terminal buffered providers prove that every earlier audio
        # SystemFrame reached their ingress before EndFrame can overtake it.
        steps.append(provider_ingress_drain)
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

    def __init__(
        self,
        api_key: str,
        custom_vocab: str = "",
        model: str = "stt-async-v5",
        session: aiohttp.ClientSession = None,
        on_progress: Optional[Callable[[str], None]] = None,
        enable_speaker_diarization: bool = False,
        base_url: str | None = None,
    ):
        super().__init__()
        self.api_key = api_key
        self.custom_vocab = custom_vocab
        self.model = model
        self.session = session
        self.on_progress = on_progress
        self.enable_speaker_diarization = enable_speaker_diarization
        self.base_url = base_url or soniox_rest_api_base_url(Config.SONIOX_REGION)
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
                f"{self.base_url}/files",
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
                f"{self.base_url}/transcriptions",
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
                        f"{self.base_url}/transcriptions/{transcription_id}",
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
                f"{self.base_url}/transcriptions/{transcription_id}/transcript",
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
                    f"{self.base_url}/transcriptions/{transcription_id}",
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
                    f"{self.base_url}/files/{file_id}",
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
from src.modulate_stt import (
    ModulateAsyncProcessor,
    ModulateRealtimeSTTService,
    modulate_transcript_payload_to_text,
    transcribe_with_modulate_multilingual,
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

def _pipecat_language(language: str | None) -> Language | str | None:
    normalized = str(language or "").strip().lower().replace("_", "-")
    if not normalized or normalized == "auto":
        return None
    lang = LANGUAGE_MAP.get(normalized) or LANGUAGE_MAP.get(
        normalized.split("-", 1)[0]
    )
    return lang if lang else None


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
    model: str,
    language: str,
    custom_vocab: str,
) -> object:
    module = import_provider_runtime_module("assemblyai_realtime", "pipecat.services.assemblyai.stt")
    service_cls = getattr(module, "AssemblyAISTTService", None)
    if service_cls is None:
        raise RuntimeError("AssemblyAI Pipecat STT service is unavailable in this build.")

    settings_cls = getattr(service_cls, "Settings", None)
    language_code = assemblyai_universal_35_language_code(language)
    keyterms = build_keyterms_from_vocab(custom_vocab)

    if settings_cls is None:
        raise RuntimeError("AssemblyAI realtime transcription requires Pipecat 1.5.0.")

    settings_candidates = {"model": model}
    if language_code:
        settings_candidates["language_code"] = language_code
    if keyterms:
        settings_candidates["keyterms_prompt"] = keyterms[:100]
    settings_candidates["speaker_labels"] = False
    settings_kwargs = _filter_supported_kwargs(settings_cls, settings_candidates)
    if settings_kwargs.get("model") != model:
        raise RuntimeError(
            "AssemblyAI Pipecat settings cannot bind the frozen model."
        )
    if keyterms and settings_kwargs.get("keyterms_prompt") != keyterms[:100]:
        raise RuntimeError(
            "AssemblyAI Pipecat settings cannot bind the frozen vocabulary."
        )
    settings = settings_cls(**settings_kwargs)
    service_candidates = {
        "api_key": api_key,
        "sample_rate": Config.SAMPLE_RATE,
        "settings": settings,
        "vad_force_turn_endpoint": True,
    }
    if language_code and "language_code" not in settings_kwargs:
        service_candidates["language"] = _pipecat_language(language)
    service_kwargs = _filter_supported_kwargs(service_cls, service_candidates)
    if language_code and "language_code" not in settings_kwargs:
        if service_kwargs.get("language") != _pipecat_language(language):
            raise RuntimeError(
                "AssemblyAI Pipecat service cannot bind the frozen language."
            )
    return service_cls(**service_kwargs)


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
        # Native endpoint notifications and Settings changes explicitly
        # invalidate this cache. A ten-second default made every longer
        # dictation pay PortAudio enumeration again on the next hotkey even
        # though the device generation had not changed.
        3600.0,
        minimum=0.0,
        maximum=86400.0,
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


def _resolve_live_mic_capture_device(
    mic_prewarm_manager: object | None,
) -> tuple[str, bool]:
    """Return the capture device plus whether it came from the warm lease."""

    leased_device = getattr(
        mic_prewarm_manager,
        "leased_capture_device_preference",
        None,
    )
    if callable(leased_device):
        try:
            resolved = leased_device(
                sample_rate=int(getattr(Config, "SAMPLE_RATE", 16000) or 16000),
                target_channels=max(
                    1,
                    int(getattr(Config, "CHANNELS", 1) or 1),
                ),
                block_size=max(
                    64,
                    int(getattr(Config, "MIC_BLOCK_SIZE", 512) or 512),
                ),
            )
            if resolved not in (None, ""):
                return str(resolved), True
        except Exception as exc:
            logger.debug(
                "Could not reuse the idle microphone route; resolving it normally: {}",
                type(exc).__name__,
            )
    return _resolve_mic_device(Config.MIC_DEVICE), False


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
        if isinstance(frame, ErrorFrame):
            error_msg = str(frame.error) if hasattr(frame, 'error') else str(frame)
            error_lower = error_msg.lower()

            # Check for connection-related errors
            is_connection_error = (
                "timeout" in error_lower
                or "handshake" in error_lower
                or "connection" in error_lower
                or "cannot connect" in error_lower
                or "could not connect" in error_lower
                or "clientconnectorerror" in error_lower
                or "websocket" in error_lower
            )

            if is_connection_error:
                # A failed socket can yield more than one ErrorFrame (for
                # example, a send failure followed by a receive-loop close).
                # Consume every duplicate so downstream processors see neither
                # an error flood nor frames after terminal cleanup began.
                if self._error_triggered:
                    return
                self._error_triggered = True

                if self.on_provider_error:
                    try:
                        self.on_provider_error(error_msg)
                    except Exception as e:
                        logger.warning(f"Provider error callback failed: {e}")

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

            if not self._error_triggered and self.on_provider_error:
                try:
                    self.on_provider_error(error_msg)
                except Exception as e:
                    logger.warning(f"Provider error callback failed: {e}")

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
    """Supplies recording boundaries without requiring a local VAD analyzer.

    Segmented upload services use the frames to decide when to submit buffered
    audio; several native streaming services use the stop frame as an explicit
    final commit. By default Scriber keeps one recording-wide turn. On eligible
    segmented routes an attached Silero processor may instead provide multiple
    boundaries at pauses.
    """

    def __init__(
        self,
        *,
        vad_segmentation_enabled: bool,
        stop_strategy: str = LIVE_STT_STOP_END_FRAME_FINALIZES,
    ):
        super().__init__()
        self.vad_segmentation_enabled = bool(vad_segmentation_enabled)
        self.stop_strategy = str(stop_strategy or LIVE_STT_STOP_END_FRAME_FINALIZES)
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
                logger.debug(
                    "Opening recording-wide STT upload boundary "
                    "(local VAD segmentation disabled)"
                )
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
        logger.debug("Closing recording-wide STT upload boundary")
        await self.push_frame(VADUserStoppedSpeakingFrame(), direction)
        return True


class _ProviderIngressDrainFrame(SystemFrame):
    """Private same-lane fence for terminal buffered provider ingress."""

    def __init__(self) -> None:
        super().__init__()
        self._acknowledged = asyncio.Event()

    def acknowledge(self) -> None:
        self._acknowledged.set()

    async def wait(self) -> None:
        await self._acknowledged.wait()


class _ProviderIngressDrainProcessor(FrameProcessor):
    """Acknowledge the private drain fence immediately after the STT step."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, _ProviderIngressDrainFrame)
        ):
            frame.acknowledge()
            return
        await self.push_frame(frame, direction)


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
        on_audio_start_marker: Optional[Callable[..., None]] = None,
        on_provider_replay_fixture_consumed: Optional[Callable[[], None]] = None,
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
        azure_mai_capture_time_mp3_enabled: bool | None = None,
        speechmatics_batch_raw_transport=None,
        speechmatics_capture_time_wav_enabled: bool | None = None,
        on_provider_response_complete: Optional[Callable[[], None]] = None,
        soniox_replay_url: str | None = None,
        soniox_replay_final_message_sha256: str | None = None,
        on_soniox_last_final_token_received: Optional[Callable[[], None]] = None,
        soniox_replay_model: str | None = None,
        provider_http_transport: ProviderHttpTransport | None = None,
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
        self.on_audio_start_marker = on_audio_start_marker
        self.on_provider_replay_fixture_consumed = (
            on_provider_replay_fixture_consumed
        )
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
        self.azure_mai_capture_time_mp3_enabled = (
            azure_mai_capture_time_mp3_enabled
        )
        self.speechmatics_batch_raw_transport = speechmatics_batch_raw_transport
        self.on_provider_response_complete = on_provider_response_complete
        self.soniox_replay_url = str(soniox_replay_url or "").strip() or None
        self.soniox_replay_final_message_sha256 = (
            str(soniox_replay_final_message_sha256 or "").strip().lower() or None
        )
        self.on_soniox_last_final_token_received = on_soniox_last_final_token_received
        self.soniox_replay_model = str(soniox_replay_model or "").strip() or None
        self._provider_replay_capture_enabled = bool(
            self.azure_mai_raw_transport is not None
            or self.speechmatics_batch_raw_transport is not None
            or self.soniox_replay_url is not None
        )
        self.provider_http_transport = provider_http_transport
        self._speechmatics_capture_time_wav_enabled = (
            _speechmatics_capture_time_wav_enabled()
            if speechmatics_capture_time_wav_enabled is None
            else bool(speechmatics_capture_time_wav_enabled)
        )
        if (
            self.speechmatics_batch_raw_transport is not None
            and not callable(self.speechmatics_batch_raw_transport)
        ):
            raise ValueError("Speechmatics replay transport must be callable")
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
        self._provider_ingress_drain_processor: (
            _ProviderIngressDrainProcessor | None
        ) = None
        self._provider_replay_capture_attestation: dict[str, Any] | None = None
        self._vad_observer: PipecatVadSpeechObserver | None = None
        self.is_active = False
        self._terminal_error: str | None = None
        self._audio_cleanup_lock = asyncio.Lock()
        self._mic_prewarm_handoff_prepared = False
        self._mic_prewarm_handoff_resumed = False
        self._start_done = asyncio.Event()
        self._start_done.set()
        self._final_transcription_received = asyncio.Event()
        self._final_transcription_generation = 0
        self._last_final_transcription_at = 0.0
        self._provider_request_started = False
        self._provider_request_state = "not_started"
        # Provider-native timing/diarization data for meeting finalization.
        # It remains process-local and is not exposed through the REST API.
        self.last_structured_transcript_payload: dict[str, Any] | None = None

    def _execution_value(self, key: str, fallback: Any) -> Any:
        route = self.execution_route
        if route is not None and key in route:
            return route[key]
        return fallback

    @contextlib.asynccontextmanager
    async def _provider_session(self):
        """Yield the app-owned provider pool, with an isolated test fallback.

        Production controller paths inject :class:`ProviderHttpTransport` so
        sequential dictations reuse DNS/TCP/TLS state.  Directly constructed
        pipelines in focused tests retain their historical owned-session
        behavior and close it deterministically.
        """

        if self.provider_http_transport is not None:
            def on_http_marker(name: str, *, timestamp_ns: int | None = None) -> None:
                if name == "request_started":
                    self._provider_request_state = "request_started"
                elif name == "first_request_chunk_sent":
                    self._provider_request_started = True
                    self._provider_request_state = "bytes_may_have_been_sent"
                elif name == "response_headers_received":
                    self._provider_request_started = True
                    self._provider_request_state = "response_received"
                elif name == "first_response_chunk_received":
                    self._provider_request_started = True
                    self._provider_request_state = "response_body_received"
                callback = self.on_audio_start_marker
                if callable(callback):
                    try:
                        callback(name, timestamp_ns=timestamp_ns)
                    except TypeError:
                        callback(name)

            yield await self.provider_http_transport.session_view(
                provider=self.service_name,
                marker=on_http_marker,
            )
            return
        # Focused tests and non-controller callers may construct a pipeline
        # without the application transport.  There is no first-body trace in
        # that compatibility path, so cross the boundary conservatively before
        # handing a session to any remote adapter.
        self.mark_provider_request_may_be_committed()
        async with aiohttp.ClientSession() as session:
            yield session

    def _execution_language(self) -> str:
        return str(self._execution_value("language", Config.LANGUAGE) or "")

    def _execution_selected_language(self) -> str | None:
        value = self._execution_language().strip()
        return None if not value or value.lower() == "auto" else value

    def _execution_pipecat_language(self) -> Language | str | None:
        """Return the frozen language in the shape Pipecat settings accept.

        Background jobs must not consult mutable ``Config.LANGUAGE`` after
        their execution route has been selected.  Live sessions have no
        execution route and retain the existing configuration fallback.
        """

        value = self._execution_selected_language()
        if value is None:
            return None
        normalized = value.strip().lower().replace("_", "-")
        return (
            LANGUAGE_MAP.get(normalized)
            or LANGUAGE_MAP.get(normalized.split("-", 1)[0])
            or value
        )

    def _execution_custom_vocab(self) -> str:
        return str(self._execution_value("custom_vocab", Config.CUSTOM_VOCAB) or "")

    def _execution_model(self, fallback: str) -> str:
        value = str(self._execution_value("model", fallback) or "").strip()
        return value or fallback

    def _execution_provider_region(self, fallback: str) -> str:
        value = str(self._execution_value("provider_region", fallback) or "").strip()
        return value or str(fallback or "").strip()

    def _bind_execution_provider_endpoint(self, endpoint_identity: str) -> str:
        """Verify the endpoint used now against the frozen privacy-safe hash."""

        normalized = str(endpoint_identity or "").strip().rstrip("/")
        expected = str(
            self._execution_value("provider_endpoint_sha256", "") or ""
        ).strip().lower()
        if expected:
            actual = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if actual != expected:
                raise RuntimeError(
                    "Provider endpoint no longer matches the frozen execution route"
                )
        return normalized

    def mark_provider_result_durable(self) -> None:
        """Advance the local request state after durable stage persistence."""

        if self._provider_request_state in {
            "response_received",
            "response_body_received",
            "result_received",
        }:
            self._provider_request_state = "result_durable"

    def mark_provider_request_may_be_committed(self) -> None:
        """Conservatively cross the remote-acceptance boundary.

        Pipecat-owned transports (notably Google Speech V2 and Groq's
        OpenAI-compatible transcription client) do not use the app-owned
        ``aiohttp`` session and therefore cannot emit its request-body trace
        marker.  File jobs call this immediately before the real pipeline can
        feed audio to those clients.  Local ONNX work never crosses a remote
        boundary and deliberately remains retryable.
        """

        if self.service_name == "onnx_local":
            return
        self._provider_request_started = True
        self._provider_request_state = "bytes_may_have_been_sent"

    def _require_exact_provider_request_contract(
        self,
        *,
        provider: str,
        default_route: str,
        default_model: str,
        route_kind: ProviderAudioRouteKind,
        audio_input_format: AudioInputFormat,
        transport: str,
        preparation_implementation: str,
    ) -> str:
        """Validate one frozen provider request before constructing its client.

        Google and Groq are Pipecat-owned transports, so the generic direct-file
        preparation boundary cannot validate them later.  Bind their exact API
        route/model and the representation Pipecat actually puts on the wire at
        service construction instead.  A persisted route from an older or
        unknown implementation is rejected before credentials or audio reach a
        provider.
        """

        route_snapshot = self.execution_route
        if route_snapshot is None:
            route = default_route
            model = default_model
        else:
            route = str(route_snapshot.get("provider_route") or "").strip()
            model = str(route_snapshot.get("model") or "").strip()
            if not route or not model:
                raise ProviderAudioCapabilityError(
                    "Frozen provider request is missing its exact route or model."
                )

        capability = resolve_provider_audio_capabilities(provider, route, model)
        if (
            capability.route_kind != route_kind
            or route != capability.route
            or model != capability.model_family
        ):
            raise ProviderAudioCapabilityError(
                "Frozen provider request does not match the exact active API contract."
            )
        require_exact_audio_input_format(
            capability,
            audio_input_format,
            route_kind=route_kind,
        )

        if route_snapshot is not None:
            expected = {
                "provider_audio_capability_id": capability.capability_id,
                "provider_audio_capability_revision": capability.revision,
                "audio_input_format": audio_input_format.value,
                "audio_input_format_verified": True,
                "audio_selection_mode": "generated",
                "audio_preparation_implementation": preparation_implementation,
                "transport": transport,
            }
            if any(route_snapshot.get(key) != value for key, value in expected.items()):
                raise ProviderAudioCapabilityError(
                    "Frozen provider request metadata does not match the active API contract."
                )
        return model

    def _require_realtime_pcm_request_contract(
        self,
        *,
        provider: str,
        default_model: str,
    ) -> str:
        """Bind one streaming-only background route to its exact PCM client."""

        route = realtime_route_for_provider(provider)
        implementation = realtime_pcm_preparation_implementation(provider)
        if not route or not implementation:
            raise ProviderAudioCapabilityError(
                "Realtime provider has no active exact PCM request contract."
            )
        return self._require_exact_provider_request_contract(
            provider=provider,
            default_route=route,
            default_model=default_model,
            route_kind=ProviderAudioRouteKind.REALTIME,
            audio_input_format=AudioInputFormat.RAW_PCM16,
            transport="decoded_pcm",
            preparation_implementation=implementation,
        )

    def stt_runtime_configuration(self) -> dict[str, Any]:
        """Return the effective, credential-free STT route for diagnostics."""

        service = str(self.service_name or "").strip().lower()
        frozen_provider_route = str(
            (self.execution_route or {}).get("provider_route") or ""
        ).strip()
        soniox_async = service == "soniox_async" or (
            service == "soniox"
            and self.soniox_replay_url is None
            and (
                frozen_provider_route == "async_transcription"
                if self.execution_route is not None
                else Config.SONIOX_MODE == "async"
            )
        )
        configured_models = Config.transcription_provider_models()

        models: dict[str, str] = {
            "soniox": (
                configured_models["soniox-async"]
                if soniox_async
                else self.soniox_replay_model or configured_models["soniox-realtime"]
            ),
            "soniox_async": configured_models["soniox-async"],
            "mistral": configured_models["mistral-realtime"],
            "mistral_async": configured_models["mistral-async"],
            "smallest": configured_models["smallest-realtime"],
            "smallest_async": configured_models["smallest-async"],
            "assemblyai": configured_models["assemblyai"],
            "assemblyai_realtime": configured_models["assemblyai-realtime"],
            "google": configured_models["google"],
            "gemini_stt": configured_models["gemini-stt"],
            "elevenlabs": configured_models["elevenlabs"],
            "deepgram": configured_models["deepgram"],
            "deepgram_async": configured_models["deepgram-async"],
            "openai": configured_models["openai"],
            "openai_async": configured_models["openai-async"],
            "azure_mai": configured_models["azure_mai"],
            "gladia": configured_models["gladia"],
            "gladia_async": configured_models["gladia-async"],
            "groq": configured_models["groq"],
            "speechmatics": configured_models["speechmatics"],
            "speechmatics_async": configured_models["speechmatics-async"],
            "modulate": configured_models["modulate-realtime"],
            "modulate_async": configured_models["modulate-async"],
            "onnx_local": configured_models["onnx_local"],
        }
        modes: dict[str, str] = {
            "soniox": "batch" if soniox_async else "realtime",
            "soniox_async": "batch",
            "mistral": "segmented",
            "mistral_async": "batch",
            "smallest": "realtime",
            "smallest_async": "batch",
            "assemblyai": "batch",
            "assemblyai_realtime": "realtime",
            "google": "realtime",
            "gemini_stt": "batch",
            "elevenlabs": "realtime",
            "deepgram": "realtime",
            "deepgram_async": "batch",
            "openai": "realtime",
            "openai_async": "batch",
            "azure_mai": "segmented",
            "gladia": "realtime",
            "gladia_async": "batch",
            "groq": "segmented",
            "speechmatics": "realtime",
            "speechmatics_async": "batch",
            "modulate": "realtime",
            "modulate_async": "batch",
            "onnx_local": "local",
        }
        fallback_model = models.get(service, "provider-default")
        configuration: dict[str, Any] = {
            "provider": service or "unknown",
            "model": self._execution_model(fallback_model),
            "mode": modes.get(service, "unknown"),
            "language": self._execution_language().strip() or "auto",
            "sampleRateHz": (
                48_000
                if self._provider_replay_capture_enabled
                else int(Config.SAMPLE_RATE)
            ),
            "channels": int(Config.CHANNELS),
        }
        if service in {"soniox", "soniox_async"}:
            configuration["region"] = self._execution_provider_region(
                Config.SONIOX_REGION
            )
        return configuration

    def _log_stt_runtime_configuration(self, *, workload: str) -> None:
        configuration = self.stt_runtime_configuration()
        logger.bind(
            component="pipeline",
            event="stt.runtime.configured",
            workflow=workload,
            provider=configuration["provider"],
            meta=configuration,
        ).info(
            "STT configured · provider={} · model={} · mode={} · language={}",
            configuration["provider"],
            configuration["model"],
            configuration["mode"],
            configuration["language"],
        )

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
        self._final_transcription_generation += 1
        self._last_final_transcription_at = time.monotonic()
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
        after_generation: int,
        timeout_seconds: float,
    ) -> str:
        if self._final_transcription_generation > after_generation:
            return "final"

        # This event is shared across the session. A final token from an older
        # semantic endpoint must never satisfy the explicit hotkey-stop wait.
        self._final_transcription_received.clear()
        if self._final_transcription_generation > after_generation:
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

        if (
            final_task in done
            and self._final_transcription_generation > after_generation
        ):
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
        if self._final_transcription_generation > after_generation:
            return "final"
        return "timeout"

    async def _prepare_mic_prewarm_for_capture_stop_locked(self) -> bool:
        """Start idle prewarm before the active WASAPI client is stopped.

        Segmented providers keep their downstream pipeline alive while a final
        HTTP request runs.  The microphone handoff must not wait for that
        provider work: starting the replacement client first keeps the Windows
        privacy indicator continuous.  The state flags make the later full
        audio cleanup idempotent while still allowing a failed start to retry.
        Callers must hold ``_audio_cleanup_lock`` so provider-failure cleanup
        cannot race a user-requested segmented stop.
        """
        manager = self.mic_prewarm_manager
        if manager is None:
            return False

        if not self._mic_prewarm_handoff_prepared:
            detach_active_capture = getattr(manager, "detach_active_capture", None)
            if callable(detach_active_capture):
                try:
                    await asyncio.to_thread(detach_active_capture, None)
                except Exception as exc:
                    logger.debug(f"Mic prewarm detach before audio cleanup warning: {exc}")
            self._mic_prewarm_handoff_prepared = True

        if not Config.MIC_ALWAYS_ON:
            return False
        if self._mic_prewarm_handoff_resumed:
            return True

        audio_input = self.audio_input
        external_handoff_prepared = False
        prepare_external_handoff = getattr(
            audio_input,
            "prepare_external_capture_handoff",
            None,
        )
        if callable(prepare_external_handoff):
            try:
                external_handoff_prepared = bool(prepare_external_handoff())
            except Exception as exc:
                logger.debug(
                    f"Mic external handoff diagnostic preparation warning: {exc}"
                )

        try:
            self._mic_prewarm_handoff_resumed = bool(
                await asyncio.to_thread(manager.resume_after_active_capture)
            )
        except Exception as exc:
            logger.debug(f"Mic prewarm pre-resume before audio cleanup warning: {exc}")
            self._mic_prewarm_handoff_resumed = False
        if external_handoff_prepared:
            handoff_resolution = (
                "confirm_external_capture_handoff"
                if self._mic_prewarm_handoff_resumed
                else "cancel_external_capture_handoff"
            )
            resolve_external_handoff = getattr(
                audio_input,
                handoff_resolution,
                None,
            )
            if callable(resolve_external_handoff):
                try:
                    resolve_external_handoff()
                except Exception as exc:
                    logger.debug(
                        f"Mic external handoff diagnostic resolution warning: {exc}"
                    )
        return self._mic_prewarm_handoff_resumed

    def _requires_provider_ingress_audio_drain(self) -> bool:
        return (
            str(self.service_name or "").strip().lower()
            in _PROVIDER_INGRESS_DRAIN_SERVICES
        )

    async def _abort_after_provider_ingress_drain_failure(self) -> None:
        """Cancel without allowing a buffered provider to upload partial PCM."""

        self._mark_provider_terminal_transcription_skip()

        task = self.task
        if task is not None and not task.has_finished():
            try:
                try:
                    cancellation = task.cancel(
                        reason="provider ingress audio drain failed"
                    )
                except TypeError:
                    cancellation = task.cancel()
                if inspect.isawaitable(cancellation):
                    await asyncio.wait_for(
                        cancellation,
                        timeout=_PROVIDER_INGRESS_ABORT_TIMEOUT_SECONDS,
                    )
            except asyncio.TimeoutError:
                logger.debug(
                    "Timed out cancelling pipeline after provider ingress drain failure"
                )
            except Exception as exc:
                logger.debug(
                    "Pipeline cancel after provider ingress drain failure warning: {}",
                    exc,
                )

        runner = self.runner
        cancel_runner = getattr(runner, "cancel", None)
        if callable(cancel_runner):
            try:
                cancellation = cancel_runner()
                if inspect.isawaitable(cancellation):
                    await asyncio.wait_for(
                        cancellation,
                        timeout=_PROVIDER_INGRESS_ABORT_TIMEOUT_SECONDS,
                    )
            except asyncio.TimeoutError:
                logger.debug(
                    "Timed out cancelling runner after provider ingress drain failure"
                )
            except Exception as exc:
                logger.debug(
                    "Runner cancel after provider ingress drain failure warning: {}",
                    exc,
                )

    async def _await_provider_ingress_audio_drain(self, audio_input: Any) -> bool:
        """Fence SystemFrame audio before terminal provider finalization.

        Pipecat processes ``InputAudioRawFrame`` (a SystemFrame) and ``EndFrame``
        (a ControlFrame) on independent tasks.  Waiting for the transport's
        audio queue proves only that frames were enqueued at the first
        downstream processor.  This private SystemFrame traverses the same
        ingress path and is acknowledged immediately after the STT processor,
        making all earlier audio visible to Azure/Speechmatics before EndFrame.
        """

        if not self._requires_provider_ingress_audio_drain():
            return False

        # Without a PipelineTask there is no SystemFrame lane, EndFrame, or
        # terminal provider upload to fence.  Once a task exists, a missing
        # barrier remains a fail-closed runtime error below.
        if self.task is None:
            return False

        drain_processor = self._provider_ingress_drain_processor
        push_frame = getattr(audio_input, "push_frame", None)
        if drain_processor is None or not callable(push_frame):
            reason = "Provider ingress audio drain barrier is unavailable."
        else:
            frame = _ProviderIngressDrainFrame()
            try:
                await push_frame(frame, FrameDirection.DOWNSTREAM)
                await asyncio.wait_for(
                    frame.wait(),
                    timeout=_PROVIDER_INGRESS_DRAIN_TIMEOUT_SECONDS,
                )
                return True
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                reason = (
                    "Provider ingress audio drain barrier timed out before "
                    "terminal transcription."
                )
            except Exception as exc:
                logger.debug(
                    "Provider ingress audio drain barrier warning: {}",
                    exc,
                )
                reason = (
                    "Provider ingress audio drain barrier failed before "
                    "terminal transcription."
                )

        logger.error(reason)
        self._record_terminal_error(reason)
        await self._abort_after_provider_ingress_drain_failure()
        raise RuntimeError(reason)

    async def _cleanup_audio_input(self) -> None:
        audio_input = self.audio_input
        if not audio_input:
            return
        has_prewarm_manager = self.mic_prewarm_manager is not None
        resume_prewarm = bool(Config.MIC_ALWAYS_ON and has_prewarm_manager)
        prewarm_resumed_before_stop = self._mic_prewarm_handoff_resumed
        async with self._audio_cleanup_lock:
            audio_input = self.audio_input
            if not audio_input:
                return
            if has_prewarm_manager:
                prewarm_resumed_before_stop = bool(
                    await self._prepare_mic_prewarm_for_capture_stop_locked()
                )
            try:
                # Pipeline instances are per-session, so keep_alive streams cannot
                # be safely reused here. Always close to avoid orphaned PortAudio
                # resources; a real always-on mic needs an app-level manager.
                await audio_input.stop(EndFrame(), close_stream=True)
            except Exception as exc:
                logger.debug(f"Audio input cleanup warning: {exc}")
            finally:
                capture_snapshot = getattr(
                    audio_input,
                    "provider_replay_capture_attestation",
                    None,
                )
                if callable(capture_snapshot):
                    try:
                        self._provider_replay_capture_attestation = (
                            capture_snapshot()
                        )
                    except Exception:
                        self._provider_replay_capture_attestation = None
                self.audio_input = None
                self._mic_prewarm_handoff_prepared = False
                self._mic_prewarm_handoff_resumed = False
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
        use_normal_cleanup = False
        async with self._audio_cleanup_lock:
            audio_input = self.audio_input
            if not audio_input:
                return
            prewarm_resumed = (
                await self._prepare_mic_prewarm_for_capture_stop_locked()
            )
            if (
                Config.MIC_ALWAYS_ON
                and self.mic_prewarm_manager is not None
                and not prewarm_resumed
            ):
                # Do not create a deliberate privacy-indicator gap when the
                # overlap-first start fails. Release the lock before falling
                # back to normal cleanup, which retries before capture closes.
                use_normal_cleanup = True
            else:
                stop_capture = getattr(
                    audio_input,
                    "stop_capture_for_finalization",
                    None,
                )
                if callable(stop_capture):
                    try:
                        await stop_capture(close_stream=True)
                    except Exception as exc:
                        logger.debug(f"Segmented STT capture-stop warning: {exc}")
                try:
                    audio_queue = getattr(audio_input, "_audio_in_queue", None)
                    if audio_queue is not None and callable(
                        getattr(audio_queue, "join", None)
                    ):
                        await asyncio.wait_for(audio_queue.join(), timeout=1.0)
                except asyncio.TimeoutError:
                    logger.debug(
                        "Timed out waiting for segmented STT audio queue to drain"
                    )
                except Exception as exc:
                    logger.debug(f"Segmented STT audio queue drain warning: {exc}")

        if use_normal_cleanup:
            logger.warning(
                "Segmented STT prewarm handoff was not ready; using normal audio cleanup"
            )
            await self._cleanup_audio_input()

    def _live_stt_final_failure_timeout_seconds(self) -> float:
        return _env_float(
            "SCRIBER_LIVE_STT_FINAL_FAILURE_TIMEOUT_SECONDS",
            5.0,
            minimum=0.5,
            maximum=30.0,
        )

    async def _wait_for_new_final_transcription_or_done(
        self,
        *,
        after_generation: int,
        timeout_seconds: float,
    ) -> str:
        if self._final_transcription_generation > after_generation:
            return "final"
        self._final_transcription_received.clear()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        while True:
            if self._final_transcription_generation > after_generation:
                return "final"
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
            if self._final_transcription_generation > after_generation:
                return "final"
            self._final_transcription_received.clear()

    async def _await_async_vad_commit_final(
        self,
        *,
        after_generation: int,
        final_response_pending: bool,
    ) -> str:
        if not final_response_pending or not self._uses_async_vad_commit_finalization():
            return "not_required"
        timeout_seconds = self._live_stt_final_failure_timeout_seconds()
        result = await self._wait_for_new_final_transcription_or_done(
            after_generation=after_generation,
            timeout_seconds=timeout_seconds,
        )
        if result == "final":
            logger.debug("Committed live STT final received; continuing immediately")
        elif result == "timeout":
            message = (
                "The live transcription provider did not return its committed "
                f"final result within {timeout_seconds:g} seconds."
            )
            logger.warning(message)
            self._record_terminal_error(message)
        else:
            logger.debug(
                f"Committed live STT final wait ended without a final result ({result})"
            )
        return result

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

    def provider_replay_capture_attestation(self) -> dict[str, Any] | None:
        """Return private replay evidence retained across audio cleanup."""

        snapshot = self._provider_replay_capture_attestation
        return dict(snapshot) if isinstance(snapshot, dict) else None

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

    def _adopt_capture_wav_artifact(self, artifact: Any) -> bool:
        """Transfer one Rust-owned WAV lease to the exact provider consumer."""

        for processor in self._iter_pipeline_processors():
            adopt = getattr(processor, "adopt_capture_wav_artifact", None)
            if not callable(adopt):
                continue
            try:
                if bool(adopt(artifact)):
                    return True
            except Exception as exc:
                logger.debug(f"Rust capture WAV provider handoff warning: {exc}")
        return False

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
        if self._requires_pre_endframe_stt_finalization():
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

    def _requires_pre_endframe_stt_finalization(self) -> bool:
        return any(
            isinstance(processor, SegmentedSTTService)
            or (
                isinstance(processor, SegmentedSTTRecordingGate)
                and processor.stop_strategy == LIVE_STT_STOP_VAD_FLUSH_BEFORE_END
            )
            for processor in self._iter_pipeline_processors()
        )

    def _uses_async_vad_commit_finalization(self) -> bool:
        if any(
            isinstance(processor, SegmentedSTTService)
            for processor in self._iter_pipeline_processors()
        ):
            return False
        return (
            _live_stt_stop_strategy(self.service_name)
            == LIVE_STT_STOP_VAD_FLUSH_BEFORE_END
        )

    def _local_vad_final_response_pending(self) -> bool:
        observer = self._vad_observer
        if observer is None:
            return False
        try:
            snapshot = observer.snapshot()
        except Exception:
            return False
        if not snapshot.get("speechObserved"):
            return False
        if snapshot.get("speaking"):
            return True
        stopped_ago = snapshot.get("lastSpeechStoppedAgoSeconds")
        if stopped_ago is None:
            return False
        try:
            stopped_at = time.monotonic() - max(0.0, float(stopped_ago))
        except (TypeError, ValueError):
            return False
        return self._last_final_transcription_at < stopped_at

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
            soniox_region = self._execution_provider_region(Config.SONIOX_REGION)
            soniox_base_url = self._bind_execution_provider_endpoint(
                soniox_rest_api_base_url(soniox_region)
            )
            if use_async:
                logger.info("Using Soniox async transcription mode")
                return SonioxAsyncProcessor(
                    api_key=_get_api_key("soniox"),
                    custom_vocab=self._execution_custom_vocab(),
                    model=self._execution_model(Config.SONIOX_ASYNC_MODEL),
                    session=session,
                    on_progress=self.on_progress,
                    enable_speaker_diarization=self.enable_speaker_diarization,
                    base_url=soniox_base_url,
                )
            soniox_service_cls, soniox_context_cls = _load_soniox_realtime_classes()
            if not soniox_service_cls: raise RuntimeError("SonioxSTTService not available.")
            settings_cls = getattr(soniox_service_cls, "Settings", None)
            if settings_cls is None:
                raise RuntimeError("Soniox realtime transcription requires Pipecat 1.5.0.")
            lang_hint = (
                Language.EN
                if self.soniox_replay_url is not None
                else self._execution_pipecat_language()
            )
            rt_model = self.soniox_replay_model or self._execution_model(
                Config.SONIOX_RT_MODEL
            )
            settings_candidates: dict[str, Any] = {
                "model": rt_model,
                "language_hints": [lang_hint] if lang_hint else None,
                "enable_speaker_diarization": self.enable_speaker_diarization if for_file else False,
            }
            soniox_custom_vocab = (
                ""
                if self.soniox_replay_url is not None
                else self._execution_custom_vocab()
            )
            if soniox_custom_vocab and soniox_context_cls:
                terms = [t.strip() for t in soniox_custom_vocab.split(",") if t.strip()]
                if terms:
                    logger.info(f"Applying custom vocabulary: {terms}")
                    settings_candidates["context"] = soniox_context_cls(terms=terms)
            settings = settings_cls(**_filter_supported_kwargs(settings_cls, settings_candidates))
            logger.info(
                "Creating SonioxSTTService with Pipecat 1.5 settings and "
                "Soniox semantic endpoint detection"
            )
            service_kwargs: dict[str, Any] = {
                "api_key": (
                    "local-replay"
                    if self.soniox_replay_url is not None
                    else soniox_api_key
                ),
                "sample_rate": (
                    48_000
                    if self._provider_replay_capture_enabled
                    else Config.SAMPLE_RATE
                ),
                "settings": settings,
                # Soniox v5 performs semantic endpoint detection itself. Local
                # VAD may still support UI diagnostics, but must not force a
                # transcript commit on every short pause.
                "vad_force_turn_endpoint": False,
            }
            if self.soniox_replay_url is not None:
                service_kwargs["url"] = self.soniox_replay_url
            else:
                service_kwargs["url"] = soniox_realtime_websocket_url(
                    soniox_region
                )
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
                    model=self._execution_model(Config.MISTRAL_ASYNC_MODEL),
                    language=self._execution_pipecat_language(),
                    custom_vocab=self._execution_custom_vocab(),
                    session=session,
                    on_progress=self.on_progress,
                    diarize=self.enable_speaker_diarization,
                )

            logger.info("Using Mistral realtime transcription mode")
            return MistralRealtimeSTTService(
                api_key=_get_api_key("mistral"),
                model=self._execution_model(_mistral_segmented_live_model()),
                language=self._execution_pipecat_language(),
                custom_vocab=self._execution_custom_vocab(),
                aiohttp_session=session,
            )

        elif self.service_name in ("smallest", "smallest_async"):
            if not _get_api_key("smallest"):
                raise ValueError("Smallest AI API Key is missing.")

            if self.service_name == "smallest_async":
                logger.info("Using Smallest AI Pulse async transcription mode")
                return SmallestAsyncProcessor(
                    api_key=_get_api_key("smallest"),
                    language=self._execution_language(),
                    session=session,
                    on_progress=self.on_progress,
                    diarize=self.enable_speaker_diarization,
                )

            logger.info("Using Smallest AI Pulse realtime transcription mode")
            return SmallestRealtimeSTTService(
                api_key=_get_api_key("smallest"),
                language=self._execution_language(),
                custom_vocab=self._execution_custom_vocab(),
                aiohttp_session=session,
                sample_rate=Config.SAMPLE_RATE,
            )

        elif self.service_name == "assemblyai":
            if not _get_api_key("assemblyai"):
                raise ValueError("AssemblyAI API Key is missing.")
            logger.info("Using AssemblyAI Universal-3.5-Pro async transcription mode")
            return AssemblyAIUniversal3ProAsyncProcessor(
                api_key=_get_api_key("assemblyai"),
                language=self._execution_language(),
                custom_vocab=self._execution_custom_vocab(),
                session=session,
                on_progress=self.on_progress,
                speaker_labels=self.enable_speaker_diarization,
                model=self._execution_model(Config.ASSEMBLYAI_ASYNC_MODEL),
            )

        elif self.service_name == "assemblyai_realtime":
            if not _get_api_key("assemblyai"):
                raise ValueError("AssemblyAI API Key is missing.")
            logger.info("Using AssemblyAI Universal-3.5-Pro realtime transcription mode")
            bound_model = self._require_realtime_pcm_request_contract(
                provider="assemblyai_realtime",
                default_model=Config.ASSEMBLYAI_RT_MODEL,
            )
            return _create_assemblyai_realtime_service(
                api_key=_get_api_key("assemblyai"),
                model=bound_model,
                language=self._execution_language(),
                custom_vocab=self._execution_custom_vocab(),
            )
        
        elif self.service_name == "google":
            # Lazy import - only loaded when Google is used
            module = import_provider_runtime_module("google", "pipecat.services.google.stt")
            GoogleSTTService = module.GoogleSTTService
            speech_v2_module = getattr(module, "speech_v2", None)
            if getattr(speech_v2_module, "__name__", "") != "google.cloud.speech_v2":
                raise RuntimeError(
                    "Google Cloud transcription requires the verified Speech V2 API."
                )
            credentials_path = str(Config.GOOGLE_APPLICATION_CREDENTIALS or "").strip()
            if not credentials_path:
                raise ValueError("Google Cloud credentials path is missing.")
            settings_cls = getattr(GoogleSTTService, "Settings", None)
            if settings_cls is None:
                raise RuntimeError("Google Cloud streaming transcription requires Pipecat 1.5.0.")
            provider_route = realtime_route_for_provider("google")
            if not provider_route:
                raise ProviderAudioCapabilityError(
                    "Google Cloud has no active exact streaming route."
                )
            bound_model = self._require_exact_provider_request_contract(
                provider="google",
                default_route=provider_route,
                default_model="latest_long",
                route_kind=ProviderAudioRouteKind.REALTIME,
                audio_input_format=AudioInputFormat.RAW_PCM16,
                transport="decoded_pcm",
                preparation_implementation="pipecat_google_speech_v2_raw_pcm16",
            )
            selected_language = self._execution_pipecat_language()
            settings_values = {
                "model": bound_model,
                "enable_automatic_punctuation": True,
                "enable_interim_results": True,
                "enable_voice_activity_events": False,
            }
            if selected_language:
                settings_values["languages"] = [selected_language]
            settings_kwargs = _filter_supported_kwargs(settings_cls, settings_values)
            if settings_kwargs.get("model") != bound_model:
                raise RuntimeError(
                    "Google Cloud Pipecat settings cannot bind the frozen model."
                )
            if selected_language and settings_kwargs.get("languages") != [
                selected_language
            ]:
                raise RuntimeError(
                    "Google Cloud Pipecat settings cannot bind the frozen language."
                )
            settings = settings_cls(**settings_kwargs)
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
            bound_model = self._require_realtime_pcm_request_contract(
                provider="elevenlabs",
                default_model="scribe_v2_realtime",
            )
            language = self._execution_pipecat_language()
            elevenlabs_keyterms = build_keyterms_from_vocab(
                self._execution_custom_vocab()
            )[:100]
            settings_values = {
                "model": bound_model,
                "language": language,
                "keyterms": elevenlabs_keyterms or None,
            }
            settings_kwargs = _filter_supported_kwargs(
                settings_cls,
                settings_values,
            )
            if settings_kwargs.get("model") != bound_model:
                raise RuntimeError(
                    "ElevenLabs Pipecat settings cannot bind the frozen model."
                )
            if elevenlabs_keyterms and (
                settings_kwargs.get("keyterms") != elevenlabs_keyterms
            ):
                raise RuntimeError(
                    "ElevenLabs Pipecat settings cannot bind the frozen vocabulary."
                )
            settings = settings_cls(**settings_kwargs)

            logger.info(
                "ElevenLabs realtime STT: Using language={}",
                self._execution_selected_language() or "auto-detect",
            )

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
            bound_model = self._require_realtime_pcm_request_contract(
                provider="deepgram",
                default_model=Config.DEEPGRAM_MODEL,
            )
            deepgram_keyterms = build_keyterms_from_vocab(
                self._execution_custom_vocab()
            )
            deepgram_settings_values = {
                "model": bound_model,
                "language": self._execution_pipecat_language(),
                "interim_results": True,
                "smart_format": True,
                "punctuate": True,
                "diarize": self.enable_speaker_diarization if for_file else False,
            }
            if deepgram_keyterms:
                deepgram_settings_values["keyterm"] = deepgram_keyterms
            deepgram_settings_kwargs = _filter_supported_kwargs(
                settings_cls,
                deepgram_settings_values,
            )
            if deepgram_settings_kwargs.get("model") != bound_model:
                raise RuntimeError(
                    "Deepgram Pipecat settings cannot bind the frozen model."
                )
            if deepgram_keyterms and (
                deepgram_settings_kwargs.get("keyterm") != deepgram_keyterms
            ):
                raise RuntimeError(
                    "Deepgram Pipecat settings cannot bind the frozen vocabulary."
                )
            deepgram_settings = settings_cls(**deepgram_settings_kwargs)

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
            bound_model = self._require_realtime_pcm_request_contract(
                provider="openai",
                default_model=Config.OPENAI_REALTIME_STT_MODEL,
            )
            settings = settings_cls(
                model=bound_model,
                language=self._execution_pipecat_language(),
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
                model=self._execution_model(Config.OPENAI_STT_MODEL),
                language=self._execution_language(),
                custom_vocab=self._execution_custom_vocab(),
                session=session,
                on_progress=self.on_progress,
                diarize=self.enable_speaker_diarization,
            )
        
        elif self.service_name == "azure_mai":
            api_key = _get_api_key("azure_mai")
            if not api_key and self.azure_mai_raw_transport is None:
                raise ValueError("Azure MAI Speech Key is missing.")
            logger.info("Using Microsoft MAI Transcribe Pipecat STT service")
            bound_model = self._execution_model(Config.AZURE_MAI_MODEL)
            provider_route = batch_route_for_provider("azure_mai")
            if not provider_route:
                raise ProviderAudioCapabilityError(
                    "Azure MAI has no active exact batch request contract."
                )
            capability = resolve_provider_audio_capabilities(
                "azure_mai",
                provider_route,
                bound_model,
            )
            require_exact_audio_input_format(
                capability,
                AudioInputFormat.MP3,
                route_kind=ProviderAudioRouteKind.BATCH,
            )
            azure_region = validate_azure_mai_region(
                self._execution_provider_region(
                    getattr(Config, "AZURE_MAI_REGION", None)
                )
            )
            self._bind_execution_provider_endpoint(
                f"azure-mai-region:{azure_region}"
            )
            return AzureMaiTranscribeSTTService(
                speech_key=(
                    "local-replay"
                    if self.azure_mai_raw_transport is not None
                    else api_key
                ),
                region=azure_region,
                language=self._execution_language(),
                model=bound_model,
                custom_vocab=self._execution_custom_vocab(),
                session=session,
                on_progress=self.on_progress,
                raw_transport=self.azure_mai_raw_transport,
                on_response_complete=self.on_provider_response_complete,
                on_encoder_marker=self.on_audio_start_marker,
                capture_time_mp3_enabled=(
                    self.azure_mai_capture_time_mp3_enabled
                ),
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
                        "language": self._execution_pipecat_language(),
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
                language=self._execution_language(),
                custom_vocab=self._execution_custom_vocab(),
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
            provider_route = batch_route_for_provider("groq")
            if not provider_route:
                raise ProviderAudioCapabilityError(
                    "Groq has no active exact segmented request route."
                )
            bound_model = self._require_exact_provider_request_contract(
                provider="groq",
                default_route=provider_route,
                default_model="whisper-large-v3-turbo",
                route_kind=ProviderAudioRouteKind.BATCH,
                audio_input_format=AudioInputFormat.WAV_PCM16,
                transport="decoded_pcm",
                preparation_implementation="pipecat_segmented_wav_pcm16",
            )
            bound_language = self._execution_pipecat_language()
            bound_prompt = (
                "Likely vocabulary: " + self._execution_custom_vocab()
                if self._execution_custom_vocab()
                else None
            )
            settings_values = {
                "model": bound_model,
                "language": bound_language,
                "prompt": bound_prompt,
            }
            settings_kwargs = _filter_supported_kwargs(settings_cls, settings_values)
            if settings_kwargs.get("model") != bound_model:
                raise RuntimeError("Groq Pipecat settings cannot bind the frozen model.")
            if bound_language and settings_kwargs.get("language") != bound_language:
                raise RuntimeError(
                    "Groq Pipecat settings cannot bind the frozen language."
                )
            if bound_prompt and settings_kwargs.get("prompt") != bound_prompt:
                raise RuntimeError(
                    "Groq Pipecat settings cannot bind the frozen vocabulary."
                )
            settings = settings_cls(**settings_kwargs)
            service_kwargs = _filter_supported_kwargs(
                GroqSTTService,
                {
                    "api_key": _get_api_key("groq"),
                    "base_url": self._bind_execution_provider_endpoint(
                        _GROQ_OPENAI_V1_BASE_URL
                    ),
                    "settings": settings,
                },
            )
            if service_kwargs.get("base_url") != _GROQ_OPENAI_V1_BASE_URL:
                raise RuntimeError("Groq Pipecat service cannot bind the verified v1 API.")
            return GroqSTTService(**service_kwargs)
        
        elif self.service_name == "speechmatics":
            # Lazy import - only loaded when Speechmatics is used
            module = import_provider_runtime_module("speechmatics", "pipecat.services.speechmatics.stt")
            SpeechmaticsSTTService = module.SpeechmaticsSTTService
            if not _get_api_key("speechmatics"): raise ValueError("Speechmatics API Key is missing.")
            settings_cls = getattr(SpeechmaticsSTTService, "Settings", None)
            if settings_cls is None:
                raise RuntimeError("Speechmatics realtime transcription requires Pipecat 1.5.0.")
            bound_model = self._require_realtime_pcm_request_contract(
                provider="speechmatics",
                default_model="enhanced",
            )
            operating_point_cls = getattr(
                SpeechmaticsSTTService,
                "OperatingPoint",
                getattr(module, "OperatingPoint", None),
            )
            if operating_point_cls is None:
                raise RuntimeError(
                    "Speechmatics Pipecat service cannot bind the frozen model."
                )
            try:
                operating_point = operating_point_cls(bound_model)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Speechmatics Pipecat service cannot bind the frozen model."
                ) from exc
            speechmatics_terms = build_keyterms_from_vocab(
                self._execution_custom_vocab()
            )
            additional_vocab_cls = getattr(
                SpeechmaticsSTTService,
                "AdditionalVocabEntry",
                getattr(module, "AdditionalVocabEntry", None),
            )
            if speechmatics_terms and additional_vocab_cls is None:
                raise RuntimeError(
                    "Speechmatics Pipecat service cannot bind the frozen vocabulary."
                )
            additional_vocab = (
                [additional_vocab_cls(content=term) for term in speechmatics_terms]
                if additional_vocab_cls is not None
                else []
            )
            bound_language = self._execution_pipecat_language() or Language.EN
            settings_values = {
                "model": bound_model,
                "language": bound_language,
                "additional_vocab": additional_vocab,
                "operating_point": operating_point,
                "enable_diarization": (
                    self.enable_speaker_diarization if for_file else False
                ),
                "speaker_active_format": "[Speaker {speaker_id}]: {text}",
                "speaker_passive_format": "[Speaker {speaker_id}]: {text}",
            }
            settings_kwargs = _filter_supported_kwargs(
                settings_cls,
                settings_values,
            )
            if (
                settings_kwargs.get("model") != bound_model
                or settings_kwargs.get("operating_point") != operating_point
            ):
                raise RuntimeError(
                    "Speechmatics Pipecat settings cannot bind the frozen model."
                )
            if settings_kwargs.get("language") != bound_language:
                raise RuntimeError(
                    "Speechmatics Pipecat settings cannot bind the frozen language."
                )
            if speechmatics_terms and (
                settings_kwargs.get("additional_vocab") != additional_vocab
            ):
                raise RuntimeError(
                    "Speechmatics Pipecat settings cannot bind the frozen vocabulary."
                )
            settings = settings_cls(**settings_kwargs)
            speechmatics_base_url = self._bind_execution_provider_endpoint(
                speechmatics_realtime_base_url(
                    os.getenv(
                        "SPEECHMATICS_RT_URL",
                        SPEECHMATICS_REALTIME_DEFAULT_BASE_URL,
                    )
                )
            )
            service_kwargs = _filter_supported_kwargs(
                SpeechmaticsSTTService,
                {
                    "api_key": _get_api_key("speechmatics"),
                    "base_url": speechmatics_base_url,
                    "sample_rate": Config.SAMPLE_RATE,
                    "settings": settings,
                },
            )
            if service_kwargs.get("base_url") != speechmatics_base_url:
                raise RuntimeError(
                    "Speechmatics Pipecat service cannot bind the frozen endpoint."
                )
            return SpeechmaticsSTTService(**service_kwargs)

        elif self.service_name == "speechmatics_async":
            api_key = _get_api_key("speechmatics")
            if not api_key and self.speechmatics_batch_raw_transport is None:
                raise ValueError("Speechmatics API Key is missing.")
            logger.info("Using Speechmatics batch async transcription mode")
            speechmatics_base_url = self._bind_execution_provider_endpoint(
                (
                    SPEECHMATICS_BATCH_DEFAULT_BASE_URL
                    if self.speechmatics_batch_raw_transport is not None
                    else os.getenv(
                        "SCRIBER_SPEECHMATICS_BATCH_BASE_URL",
                        SPEECHMATICS_BATCH_DEFAULT_BASE_URL,
                    )
                )
            )
            return SpeechmaticsAsyncProcessor(
                api_key=(
                    "local-replay"
                    if self.speechmatics_batch_raw_transport is not None
                    else api_key
                ),
                language=self._execution_language(),
                custom_vocab=self._execution_custom_vocab(),
                session=session,
                on_progress=self.on_progress,
                diarize=self.enable_speaker_diarization,
                base_url=speechmatics_base_url,
                raw_transport=self.speechmatics_batch_raw_transport,
                on_response_complete=self.on_provider_response_complete,
            )

        elif self.service_name in {"modulate", "modulate_async"}:
            api_key = _get_api_key("modulate")
            if not api_key:
                raise ValueError("Modulate API Key is missing.")
            if self.service_name == "modulate_async":
                logger.info("Using Modulate multilingual batch transcription mode")
                return ModulateAsyncProcessor(
                    api_key=api_key,
                    language=self._execution_language(),
                    session=session,
                    on_progress=self.on_progress,
                )
            logger.info("Using Modulate multilingual realtime transcription mode")
            return ModulateRealtimeSTTService(
                api_key=api_key,
                language=self._execution_language(),
                aiohttp_session=session,
                sample_rate=Config.SAMPLE_RATE,
                channels=Config.CHANNELS,
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
        self._provider_request_started = False
        self._provider_request_state = "not_started"
        self._provider_replay_capture_attestation = None
        self._terminal_error = None
        self._vad_observer = None
        self._provider_ingress_drain_processor = None
        self._final_transcription_received.clear()
        self._start_done.clear()
        try:
            async with self._provider_session() as session:
                stt_service = self._create_stt_service(session)
                if self._requires_provider_ingress_audio_drain():
                    self._provider_ingress_drain_processor = (
                        _ProviderIngressDrainProcessor()
                    )
                self._log_stt_runtime_configuration(workload="live_mic")

                vad_analyzer = None
                # The Settings switch is the master opt-in for local Silero VAD
                # on segmented/async routes. Provider-native streams always keep
                # it detached so local pauses cannot force provider commits.
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
                capture_device, capture_device_from_warm_lease = (
                    _resolve_live_mic_capture_device(self.mic_prewarm_manager)
                )
                self.audio_input = MicrophoneInput(
                    sample_rate=(
                        48_000
                        if self._provider_replay_capture_enabled
                        else Config.SAMPLE_RATE
                    ),
                    channels=Config.CHANNELS,
                    block_size=Config.MIC_BLOCK_SIZE,
                    device=capture_device,
                    fresh_device_resolver=(
                        (lambda: _resolve_mic_device(Config.MIC_DEVICE))
                        if capture_device_from_warm_lease
                        else None
                    ),
                    keep_alive=use_prewarm_for_capture,
                    prewarm_manager=self.mic_prewarm_manager,
                    on_audio_level=self.on_audio_level,
                    on_ready=self.on_mic_ready,
                    on_last_audio_chunk_sent=self.on_last_audio_chunk_sent,
                    on_start_marker=self.on_audio_start_marker,
                    on_provider_replay_fixture_consumed=(
                        self.on_provider_replay_fixture_consumed
                    ),
                    capture_audio_preparation=_rust_capture_wav_plan(
                        self.service_name,
                        sample_rate=(
                            48_000
                            if self._provider_replay_capture_enabled
                            else Config.SAMPLE_RATE
                        ),
                        channels=Config.CHANNELS,
                        execution_route=self.execution_route,
                        candidate_enabled=(
                            self._speechmatics_capture_time_wav_enabled
                        ),
                    ),
                    on_capture_wav_artifact=self._adopt_capture_wav_artifact,
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
                    """Stop accepting frames without blocking Pipecat's loop.

                    Native WASAPI shutdown is owned by the serialized async
                    pipeline stop.  Calling it here can block the event loop on
                    the device guard and shell IPC exactly when an error must be
                    broadcast promptly.
                    """
                    if self.audio_input:
                        try:
                            request_stop = getattr(
                                self.audio_input,
                                "request_stop_from_external_error",
                                None,
                            )
                            if callable(request_stop):
                                request_stop(reason="provider_connection_error")
                            else:
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
                segmented_provider = isinstance(stt_service, SegmentedSTTService)
                stop_strategy = _live_stt_stop_strategy(
                    self.service_name,
                    segmented_service=segmented_provider,
                )
                needs_recording_gate = _live_recording_gate_needed(
                    self.service_name,
                    segmented_service=segmented_provider,
                    vad_attached=vad_processor is not None,
                )
                segmented_gate = (
                    SegmentedSTTRecordingGate(
                        vad_segmentation_enabled=bool(
                            Config.SEGMENT_SPEECH_WITH_VAD
                            and vad_processor is not None
                        ),
                        stop_strategy=stop_strategy,
                    )
                    if needs_recording_gate
                    else None
                )
                analyzer_diagnostics = _live_analyzer_diagnostics(
                    vad_processor=vad_processor,
                    segmented_gate=segmented_gate,
                    segmented_provider=segmented_provider,
                    native_realtime_provider=_live_service_uses_native_streaming(
                        self.service_name
                    ),
                    stop_strategy=stop_strategy,
                    smart_turn_processor=smart_turn_processor,
                )
                logger.bind(
                    component="pipeline",
                    event="live_mic.analyzers.configured",
                    workflow="live_mic",
                    stage="analyzer_configuration",
                    provider=self.service_name,
                    meta=analyzer_diagnostics,
                ).info(
                    "Live mic analyzer configuration: Silero setting={}, "
                    "attached={}, effective={}; available={}; native realtime={}; "
                    "recording gate={}; synthetic boundary={}; segmented provider={}; stop strategy={}; "
                    "Smart Turn attached={}",
                    analyzer_diagnostics["sileroVadSettingEnabled"],
                    analyzer_diagnostics["sileroVadAttached"],
                    analyzer_diagnostics["sileroVadEffectiveEnabled"],
                    analyzer_diagnostics["sileroVadAvailable"],
                    analyzer_diagnostics["nativeRealtimeProvider"],
                    analyzer_diagnostics["recordingGateAttached"],
                    analyzer_diagnostics["syntheticRecordingBoundary"],
                    analyzer_diagnostics["segmentedProvider"],
                    analyzer_diagnostics["stopStrategy"],
                    analyzer_diagnostics["smartTurnAttached"],
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
                    provider_ingress_drain=(
                        self._provider_ingress_drain_processor
                    ),
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
        self._provider_request_started = False
        self._provider_request_state = "not_started"
        if self.is_active:
            return
        logger.info(f"Transcribing audio file with {self.service_name}: {file_path}")
        self._start_done.clear()
        file_input: FfmpegAudioFileInput | None = None
        run_task: asyncio.Task | None = None
        pipeline_finished = False
        try:
            async with self._provider_session() as session:
                stt_service = self._create_stt_service(session, for_file=True)
                self._log_stt_runtime_configuration(workload="file")

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

                self.mark_provider_request_may_be_committed()
                run_task = asyncio.create_task(self.runner.run(self.task), name="scriber_file_pipeline")

                # Wait until the input transport has finished feeding (and its internal audio queue has drained),
                # then end the pipeline gracefully so providers can flush final transcripts.
                await file_input.done.wait()
                await self.task.stop_when_done()

                await run_task
                pipeline_finished = True

                if file_input.error:
                    raise RuntimeError(file_input.error)
                self._provider_request_state = "result_received"

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

    @staticmethod
    def _direct_file_content_type(path: Any) -> str:
        import mimetypes

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
        return content_type_map.get(
            path.suffix.lower(),
            mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        )

    @staticmethod
    def _execution_audio_value(
        route: Mapping[str, Any],
        snake_case: str,
        camel_case: str,
    ) -> Any:
        if snake_case in route:
            return route[snake_case]
        return route.get(camel_case)

    def _frozen_provider_audio_capability(
        self,
    ) -> tuple[ProviderAudioInputCapabilities, AudioInputFormat | None] | None:
        """Validate capability metadata without touching source bytes or HTTP."""

        route = self.execution_route
        if route is None:
            return None
        raw_capability_id = self._execution_audio_value(
            route,
            "provider_audio_capability_id",
            "providerAudioCapabilityId",
        )
        raw_revision = self._execution_audio_value(
            route,
            "provider_audio_capability_revision",
            "providerAudioCapabilityRevision",
        )
        capability_id = str(raw_capability_id or "").strip()
        revision = str(raw_revision or "").strip()
        if not capability_id and not revision:
            transport = str(route.get("transport") or "").strip().lower()
            if transport == "direct_upload":
                raise ProviderAudioPreparationError(
                    "Frozen direct-upload route has no verified provider audio capability."
                )
            return None
        if not capability_id or not revision:
            raise ProviderAudioPreparationError(
                "Frozen provider audio capability metadata is incomplete."
            )

        provider = str(self.service_name or "").strip().lower()
        frozen_provider = str(route.get("provider") or "").strip().lower()
        if frozen_provider and frozen_provider != provider:
            raise ProviderAudioPreparationError(
                "Frozen provider audio route does not match the active provider."
            )
        model = str(route.get("model") or "").strip()
        if not provider or not model:
            raise ProviderAudioPreparationError(
                "Frozen provider audio route requires provider and model identifiers."
            )
        try:
            capability = resolve_batch_provider_audio_capabilities(provider, model)
        except ProviderAudioCapabilityError as exc:
            raise ProviderAudioPreparationError(
                "Frozen provider/model has no verified audio capability."
            ) from exc
        if capability.capability_id != capability_id or capability.revision != revision:
            raise ProviderAudioPreparationError(
                "Frozen provider audio capability does not match the active registry."
            )
        frozen_route = str(
            self._execution_audio_value(route, "provider_route", "providerRoute") or ""
        ).strip()
        if frozen_route and frozen_route != capability.route:
            raise ProviderAudioPreparationError(
                "Frozen provider audio route does not match its capability."
            )

        raw_format = self._execution_audio_value(
            route,
            "audio_input_format",
            "audioInputFormat",
        )
        raw_verified = self._execution_audio_value(
            route,
            "audio_input_format_verified",
            "audioInputFormatVerified",
        )
        if raw_verified is not None and not isinstance(raw_verified, bool):
            raise ProviderAudioPreparationError(
                "Frozen provider audio verification marker is invalid."
            )
        if raw_verified is False:
            raise ProviderAudioPreparationError(
                "Frozen provider audio format is not verified for this route."
            )

        frozen_format: AudioInputFormat | None = None
        if raw_format not in (None, ""):
            if raw_verified is not True:
                raise ProviderAudioPreparationError(
                    "Frozen provider audio format lacks a verified marker."
                )
            try:
                frozen_format = coerce_audio_input_format(raw_format)
            except ProviderAudioCapabilityError as exc:
                raise ProviderAudioPreparationError(
                    "Frozen provider audio format is not an exact representation."
                ) from exc
        elif raw_verified is True:
            raise ProviderAudioPreparationError(
                "Frozen provider audio verification marker has no exact format."
            )
        return capability, frozen_format

    @staticmethod
    def _validate_prepared_provider_audio(
        prepared: PreparedProviderAudio,
        *,
        capability: ProviderAudioInputCapabilities,
        frozen_format: AudioInputFormat | None,
    ) -> None:
        if not isinstance(prepared, PreparedProviderAudio):
            raise ProviderAudioPreparationError(
                "Prepared provider audio has an invalid boundary object."
            )
        if not prepared.path.is_file() or prepared.byte_length <= 0:
            raise ProviderAudioPreparationError(
                "Prepared provider audio file is missing or empty."
            )
        if prepared.path.stat().st_size != prepared.byte_length:
            raise ProviderAudioPreparationError(
                "Prepared provider audio changed after format verification."
            )
        if (
            prepared.capability_id != capability.capability_id
            or prepared.capability_revision != capability.revision
        ):
            raise ProviderAudioPreparationError(
                "Prepared audio does not match the frozen provider capability."
            )
        try:
            require_exact_audio_input_format(
                capability,
                prepared.selected_format,
                route_kind=ProviderAudioRouteKind.BATCH,
            )
        except ProviderAudioCapabilityError as exc:
            raise ProviderAudioPreparationError(
                "Prepared audio format is not verified for the frozen provider route."
            ) from exc
        if frozen_format is not None and prepared.selected_format != frozen_format:
            raise ProviderAudioPreparationError(
                "Prepared audio does not match the frozen exact audio format."
            )

    async def transcribe_file_direct(
        self,
        file_path: str,
        *,
        prepared_audio: PreparedProviderAudio | None = None,
    ) -> None:
        """Prepare and upload one file through its frozen provider route."""

        from pathlib import Path

        self._provider_request_started = False
        self._provider_request_state = "not_started"
        if self.is_active:
            return

        path = Path(file_path)
        if not path.exists():
            raise ValueError(f"File not found: {file_path}")

        logger.info(f"Transcribing file directly with {self.service_name}: {file_path}")
        self._log_stt_runtime_configuration(workload="file_direct")
        self.is_active = True

        try:
            frozen_audio = self._frozen_provider_audio_capability()
            if prepared_audio is not None:
                if frozen_audio is None:
                    raise ProviderAudioPreparationError(
                        "Prepared provider audio requires frozen capability metadata."
                    )
                capability, frozen_format = frozen_audio
                if frozen_format is None:
                    raise ProviderAudioPreparationError(
                        "Prepared provider audio requires a frozen verified exact format."
                    )
                self._validate_prepared_provider_audio(
                    prepared_audio,
                    capability=capability,
                    frozen_format=frozen_format,
                )
                # Ownership and cleanup remain with the caller's preparation
                # context; this method only borrows the verified path.
                await self._transcribe_file_direct_prepared(
                    prepared_audio.path,
                    content_type=prepared_audio.content_type,
                    capability_prepared=True,
                )
                self._provider_request_state = "result_received"
                return
            if frozen_audio is None:
                await self._transcribe_file_direct_prepared(
                    path,
                    content_type=self._direct_file_content_type(path),
                    capability_prepared=False,
                )
                self._provider_request_state = "result_received"
                return

            capability, frozen_format = frozen_audio
            with tempfile.TemporaryDirectory(prefix="scriber-provider-audio-") as work_dir:
                async with prepare_provider_audio_file(
                    path,
                    provider=str(self.service_name or "").strip().lower(),
                    model=str(self.execution_route.get("model") or "").strip(),
                    work_dir=work_dir,
                ) as prepared:
                    self._validate_prepared_provider_audio(
                        prepared,
                        capability=capability,
                        frozen_format=frozen_format,
                    )
                    # The provider session is opened only after every frozen
                    # identity and exact-format check above has succeeded.
                    await self._transcribe_file_direct_prepared(
                        prepared.path,
                        content_type=prepared.content_type,
                        capability_prepared=True,
                    )
                    self._provider_request_state = "result_received"
        except Exception as e:
            logger.error(f"Direct file transcription failed: {e}")
            if self.on_status_change:
                self.on_status_change("Error")
            raise
        finally:
            self.is_active = False

    async def _transcribe_file_direct_prepared(
        self,
        path: Any,
        *,
        content_type: str,
        capability_prepared: bool,
    ) -> None:
        """Run the existing provider request against already selected bytes."""

        try:
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

                async with self._provider_session() as session:
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

                async with self._provider_session() as session:
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

                async with self._provider_session() as session:
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

                async with self._provider_session() as session:
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

                async with self._provider_session() as session:
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

                async with self._provider_session() as session:
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

                region = validate_azure_mai_region(
                    self._execution_provider_region(
                        getattr(Config, "AZURE_MAI_REGION", None)
                    )
                )
                self._bind_execution_provider_endpoint(
                    f"azure-mai-region:{region}"
                )
                if self.on_progress:
                    self.on_progress("Preparing audio...")

                async with self._provider_session() as session:
                    upload_context = (
                        contextlib.nullcontext(path)
                        if capability_prepared
                        else prepared_azure_mai_audio_file(path)
                    )
                    async with upload_context as upload_path:
                        with open(upload_path, "rb") as f:
                            payload = await transcribe_with_azure_mai(
                                session=session,
                                speech_key=api_key,
                                region=region,
                                audio_source=f,
                                filename=upload_path.name,
                                content_type=(
                                    content_type
                                    if capability_prepared
                                    else azure_mai_content_type(upload_path)
                                ),
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

                async with self._provider_session() as session:
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

            if self.service_name in {"modulate", "modulate_async"}:
                api_key = Config.get_api_key("modulate")
                if not api_key:
                    raise ValueError("Modulate API key is missing")

                async with self._provider_session() as session:
                    with open(path, "rb") as f:
                        payload = await transcribe_with_modulate_multilingual(
                            session=session,
                            api_key=api_key,
                            audio_source=f,
                            filename=path.name,
                            content_type=content_type,
                            language=self._execution_language(),
                            on_progress=self.on_progress,
                            timeout_secs=batch_timeout_seconds,
                        )

                # The adapter has already removed Modulate's utterance array and
                # every enrichment field.  Only final text and duration cross
                # this boundary.
                self.last_structured_transcript_payload = payload
                text = modulate_transcript_payload_to_text(payload)
                if text and self.on_transcription:
                    logger.info(
                        f"Modulate direct transcription completed ({len(text)} chars)"
                    )
                    self.on_transcription(text, True)

                if self.on_progress:
                    self.on_progress("Completed")
                return

            if self.service_name == "speechmatics_async":
                api_key = Config.get_api_key("speechmatics")
                if not api_key:
                    raise ValueError("Speechmatics API key is missing")

                speechmatics_base_url = self._bind_execution_provider_endpoint(
                    os.getenv(
                        "SCRIBER_SPEECHMATICS_BATCH_BASE_URL",
                        SPEECHMATICS_BATCH_DEFAULT_BASE_URL,
                    )
                )
                async with self._provider_session() as session:
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
                            base_url=speechmatics_base_url,
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
            soniox_region = self._execution_provider_region(Config.SONIOX_REGION)
            base_url = self._bind_execution_provider_endpoint(
                soniox_rest_api_base_url(soniox_region)
            )
            model = self._execution_model(
                Config.SONIOX_ASYNC_MODEL or Config.DEFAULT_SONIOX_ASYNC_MODEL
            )
            file_id = None
            transcription_id = None

            if self.on_progress:
                self.on_progress("Uploading audio...")

            async with self._provider_session() as session:
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

        except Exception:
            raise

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

        requires_pre_endframe_finalization = (
            self._requires_pre_endframe_stt_finalization()
        )
        provider_ingress_audio_input = self.audio_input
        if requires_pre_endframe_finalization:
            final_generation_before_commit = self._final_transcription_generation
            await self._stop_audio_capture_for_segmented_finalization()
            await self._await_provider_ingress_audio_drain(
                provider_ingress_audio_input
            )
            boundary_flushed = await self._flush_segmented_stt_buffers()
            await self._await_async_vad_commit_final(
                after_generation=final_generation_before_commit,
                final_response_pending=boundary_flushed,
            )
            await self._cleanup_audio_input()
        else:
            final_generation_before_commit = self._final_transcription_generation
            final_response_pending = self._local_vad_final_response_pending()
            # Hand active capture back to the app-level idle prewarm before
            # closing this session. With MIC_ALWAYS_ON the native layer starts
            # the replacement WASAPI client first, so the Windows privacy light
            # remains continuous while transcription finalizes.
            await self._cleanup_audio_input()
            await self._await_provider_ingress_audio_drain(
                provider_ingress_audio_input
            )
            boundary_flushed = await self._flush_live_vad_finalization_turn()
            await self._await_async_vad_commit_final(
                after_generation=final_generation_before_commit,
                final_response_pending=(
                    final_response_pending or boundary_flushed
                ),
            )

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

                            # Push-to-talk stop uses Soniox's explicit manual
                            # finalization contract first. The following empty
                            # message ends the stream after the finalize request;
                            # WebSocket ordering guarantees the provider sees
                            # both in this order.
                            final_generation_before_stop = (
                                self._final_transcription_generation
                            )
                            logger.debug("Requesting manual finalization from Soniox")
                            await websocket.send(_SONIOX_MANUAL_FINALIZE_MESSAGE)
                            logger.debug("Sending stop_recording to Soniox")
                            await websocket.send("")

                            # Wait for receive task to complete (gets final tokens + finished=True)
                            receive_task = getattr(step, "_receive_task", None)
                            if receive_task and not receive_task.done():
                                final_timeout = self._soniox_realtime_stop_final_timeout_seconds()
                                wait_result = await self._wait_for_soniox_realtime_final_or_receive_done(
                                    receive_task,
                                    after_generation=final_generation_before_stop,
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
                                        after_generation=final_generation_before_stop,
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
            # Modulate owns a bounded 30-second wait for its final ``done``
            # message. Give that provider error enough time to traverse the
            # downstream ErrorFrame lane before the outer pipeline watchdog
            # cancels the task; using the same 30-second deadline made the two
            # timeouts race and could misclassify a provider hang as an
            # internal pipeline failure. Other realtime providers retain the
            # existing 30-second budget.
            if is_async_finalization:
                wait_timeout = 600.0
            elif self.service_name == "modulate":
                wait_timeout = 40.0
            else:
                wait_timeout = 30.0

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
