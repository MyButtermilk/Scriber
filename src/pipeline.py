import asyncio
import aiohttp
import subprocess
import shutil
import io
import wave
import contextlib
import tempfile
import os
from loguru import logger
from typing import Callable, Optional

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
)
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

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

try:
    from pipecat.services.soniox.stt import SonioxSTTService, SonioxInputParams, SonioxContextObject
except ImportError:
    SonioxSTTService = None
    SonioxInputParams = None
    SonioxContextObject = None

# Fallback params object if older pipecat build lacks SonioxInputParams
class _SonioxParamsFallback:
    def __init__(self, context=None, vad_enabled=True):
        self.context = context
        self.vad_enabled = vad_enabled


class SonioxAsyncProcessor(FrameProcessor):
    """Async Soniox transcription using REST API; buffers audio and submits on EndFrame."""

    BASE_URL = "https://api.soniox.com/v1"

    def __init__(
        self,
        api_key: str,
        custom_vocab: str = "",
        model: str = "stt-async-v3",
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

        if prefer_webm and shutil.which("ffmpeg"):
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
                    "ffmpeg", "-y",          # Overwrite output
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
                    stderr=asyncio.subprocess.PIPE
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
from src.injector import TextInjector
from src.microphone import MicrophoneInput
from src.audio_file_input import FfmpegAudioFileInput

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
    lang = LANGUAGE_MAP.get(Config.LANGUAGE)
    return lang if lang else None


def _resolve_mic_device(device_name: str) -> str:
    """Resolve a saved device name to the current device index.
    
    The mic device setting now stores the device NAME (stable across reboots)
    instead of the index (which can change). This function looks up the current
    index for the named device, falling back to 'default' if not found.
    
    If a FAVORITE_MIC is set and available, it will be used instead of the
    selected device. This allows users to have a preferred mic that is
    automatically used whenever it's connected.
    """
    import sounddevice as sd
    
    def find_device_by_name(name: str) -> str | None:
        """Find device index by name, preferring MME host API."""
        if not name or name == "default":
            return None
            
        try:
            # Get preferred host API (MME for best USB device compatibility)
            host_apis = sd.query_hostapis()
            mme_idx = next((i for i, h in enumerate(host_apis) if h.get('name', '') == 'MME'), None)
            wasapi_idx = next((i for i, h in enumerate(host_apis) if 'WASAPI' in h.get('name', '')), None)
            preferred_hostapi = mme_idx if mme_idx is not None else wasapi_idx
            
            # Search for device by name in preferred host API first
            for idx, dev in enumerate(sd.query_devices()):
                if int(dev.get("max_input_channels", 0) or 0) <= 0:
                    continue
                
                hostapi_idx = dev.get('hostapi', 0)
                if preferred_hostapi is not None and hostapi_idx != preferred_hostapi:
                    continue
                
                dev_name = str(dev.get("name", ""))
                if dev_name == name:
                    return str(idx)
            
            # Try any host API as fallback
            for idx, dev in enumerate(sd.query_devices()):
                if int(dev.get("max_input_channels", 0) or 0) <= 0:
                    continue
                dev_name = str(dev.get("name", ""))
                if dev_name == name:
                    return str(idx)
                    
        except Exception:
            pass
        return None
    
    # Check if favorite mic is set and should be used
    # Favorite mic only overrides when:
    # 1. Selected device is "default" (no explicit selection), OR
    # 2. Selected device is unavailable
    favorite = Config.FAVORITE_MIC
    use_favorite = False
    
    if favorite and favorite != "default":
        if device_name == "default" or not device_name:
            # No explicit selection - use favorite if available
            use_favorite = True
        else:
            # User selected a specific device - only use favorite if selected is unavailable
            selected_idx = find_device_by_name(device_name)
            if not selected_idx:
                # Selected device not available, try favorite
                use_favorite = True
        
        if use_favorite:
            favorite_idx = find_device_by_name(favorite)
            if favorite_idx:
                logger.info(f"Using favorite microphone '{favorite}' (device index {favorite_idx})")
                return favorite_idx
    
    # Fall back to selected device
    if device_name == "default" or not device_name:
        return "default"
    
    # If it's already a numeric index (legacy), validate it exists
    try:
        idx = int(device_name)
        # Validate the device exists
        import sounddevice as sd
        try:
            dev_info = sd.query_devices(device=idx, kind='input')
            if int(dev_info.get('max_input_channels', 0) or 0) > 0:
                return device_name
        except Exception:
            pass
        # Legacy device doesn't exist - fall back to default
        logger.warning(f"Legacy device index {device_name} not valid, falling back to Windows default")
        return "default"
    except ValueError:
        pass
    
    # It's a device name - resolve to index
    device_idx = find_device_by_name(device_name)
    if device_idx:
        logger.info(f"Resolved microphone '{device_name}' to device index {device_idx}")
        return device_idx
    
    # Device not found - fall back to default
    logger.warning(f"Microphone '{device_name}' not available, falling back to Windows default")
    return "default"


class TranscriptionCallbackProcessor(FrameProcessor):
    """Emits interim/final transcription updates via a lightweight callback."""

    def __init__(self, on_transcription: Optional[Callable[[str, bool], None]]):
        super().__init__()
        self.on_transcription = on_transcription

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        cb = self.on_transcription
        if cb:
            try:
                if isinstance(frame, InterimTranscriptionFrame):
                    if frame.text:
                        cb(frame.text, False)
                elif isinstance(frame, TranscriptionFrame):
                    if frame.text:
                        cb(frame.text, True)
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
        on_progress: Optional[Callable[[str], None]] = None,
        on_mic_ready: Optional[Callable[[], None]] = None,
    ):
        self.service_name = service_name
        self.on_status_change = on_status_change
        self.on_audio_level = on_audio_level
        self.on_transcription = on_transcription
        self.on_progress = on_progress
        self.on_mic_ready = on_mic_ready
        self.pipeline = None
        self.task = None
        self.runner = None
        self.audio_input = None
        self.is_active = False
        self._start_done = asyncio.Event()
        self._start_done.set()

    def _create_stt_service(self, session: aiohttp.ClientSession):
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
                # Note: speaker diarization is NOT enabled for live mic (async mode)
                # It's only enabled for file/youtube transcription
                return SonioxAsyncProcessor(
                    api_key=_get_api_key("soniox"),
                    custom_vocab=Config.CUSTOM_VOCAB,
                    model=Config.SONIOX_ASYNC_MODEL,
                    session=session,
                    on_progress=self.on_progress,
                    enable_speaker_diarization=False,  # Disabled for live mic
                )
            if not SonioxSTTService: raise ImportError("SonioxSTTService not available.")
            lang_hint = _selected_language()
            # Use stt-rt-v3 model for realtime transcription
            rt_model = Config.SONIOX_RT_MODEL
            # Build params with model and context
            if Config.CUSTOM_VOCAB and SonioxContextObject:
                terms = [t.strip() for t in Config.CUSTOM_VOCAB.split(",") if t.strip()]
                if terms:
                    logger.info(f"Applying custom vocabulary: {terms}")
                    params = SonioxInputParams(
                        model=rt_model,
                        context=SonioxContextObject(terms=terms),
                        language_hints=[lang_hint] if lang_hint else None,
                    ) if SonioxInputParams else _SonioxParamsFallback(context=SonioxContextObject(terms=terms))
                else:
                    params = SonioxInputParams(model=rt_model, language_hints=[lang_hint] if lang_hint else None) if SonioxInputParams else _SonioxParamsFallback()
            else:
                params = SonioxInputParams(model=rt_model, language_hints=[lang_hint] if lang_hint else None) if SonioxInputParams else _SonioxParamsFallback()
            # vad_force_turn_endpoint=True disables automatic endpoint detection which would
            # otherwise close the WebSocket connection when speech pauses are detected.
            # This keeps the connection alive for the entire recording session.
            logger.info(f"Creating SonioxSTTService with vad_force_turn_endpoint=True (endpoint detection DISABLED)")
            return SonioxSTTService(api_key=_get_api_key("soniox"), params=params, vad_force_turn_endpoint=True)

        elif self.service_name == "assemblyai":
            # Lazy import - only loaded when AssemblyAI is used
            from pipecat.services.assemblyai.stt import AssemblyAISTTService, AssemblyAIConnectionParams
            if not _get_api_key("assemblyai"): raise ValueError("AssemblyAI API Key is missing.")
            
            # Always use multilingual model to support:
            # 1. Auto language detection when LANGUAGE=auto
            # 2. Non-English languages (German, French, Spanish, Italian, Portuguese)
            # The 'universal-streaming-english' model only supports English
            lang = _selected_language()
            
            # Configure connection params with multilingual speech model
            connection_params = AssemblyAIConnectionParams(
                speech_model="universal-streaming-multilingual"
            )
            logger.info(f"AssemblyAI: Using multilingual model (language={lang or 'auto-detect'})")
            
            # When lang is None (auto), don't pass language param to enable auto-detection
            if lang:
                return AssemblyAISTTService(
                    api_key=_get_api_key("assemblyai"), 
                    language=lang,
                    connection_params=connection_params
                )
            else:
                return AssemblyAISTTService(
                    api_key=_get_api_key("assemblyai"), 
                    connection_params=connection_params
                )
        
        elif self.service_name == "google":
            # Lazy import - only loaded when Google is used
            from pipecat.services.google.stt import GoogleSTTService
            return GoogleSTTService()
        
        elif self.service_name == "elevenlabs":
            # Lazy import - only loaded when ElevenLabs is used
            from pipecat.services.elevenlabs.stt import ElevenLabsSTTService
            if not _get_api_key("elevenlabs"): raise ValueError("ElevenLabs API Key is missing.")
            
            # Configure language for ElevenLabs STT
            lang = _selected_language()
            params = ElevenLabsSTTService.InputParams(language=lang) if lang else None
            logger.info(f"ElevenLabs STT: Using language={lang or 'auto-detect'}")
            
            return ElevenLabsSTTService(
                api_key=_get_api_key("elevenlabs"), 
                aiohttp_session=session,
                params=params
            )
        
        elif self.service_name == "deepgram":
            # Lazy import - only loaded when Deepgram is used
            from pipecat.services.deepgram.stt import DeepgramSTTService
            if not _get_api_key("deepgram"): raise ValueError("Deepgram API Key is missing.")
            return DeepgramSTTService(api_key=_get_api_key("deepgram"))
        
        elif self.service_name == "openai":
            # Lazy import - only loaded when OpenAI is used
            from pipecat.services.openai.stt import OpenAISTTService
            if not _get_api_key("openai"): raise ValueError("OpenAI API Key is missing.")
            return OpenAISTTService(
                api_key=_get_api_key("openai"),
                aiohttp_session=session,
                language=_selected_language(),
                model=Config.OPENAI_STT_MODEL,
            )
        
        elif self.service_name == "azure":
            # Lazy import - only loaded when Azure is used
            from pipecat.services.azure.stt import AzureSTTService
            if not Config.AZURE_SPEECH_KEY or not Config.AZURE_SPEECH_REGION: raise ValueError("Azure Speech Key or Region is missing.")
            lang = Language.EN_US if Config.LANGUAGE == "en" else _selected_language()
            return AzureSTTService(api_key=Config.AZURE_SPEECH_KEY, region=Config.AZURE_SPEECH_REGION, language=lang)
        
        elif self.service_name == "gladia":
            # Lazy import - only loaded when Gladia is used
            from pipecat.services.gladia.stt import GladiaSTTService
            if not _get_api_key("gladia"): raise ValueError("Gladia API Key is missing.")
            return GladiaSTTService(api_key=_get_api_key("gladia"), aiohttp_session=session)
        
        elif self.service_name == "groq":
            # Lazy import - only loaded when Groq is used
            from pipecat.services.groq.stt import GroqSTTService
            if not _get_api_key("groq"): raise ValueError("Groq API Key is missing.")
            return GroqSTTService(api_key=_get_api_key("groq"), aiohttp_session=session, language=_selected_language())
        
        elif self.service_name == "speechmatics":
            # Lazy import - only loaded when Speechmatics is used
            from pipecat.services.speechmatics.stt import SpeechmaticsSTTService
            if not _get_api_key("speechmatics"): raise ValueError("Speechmatics API Key is missing.")
            return SpeechmaticsSTTService(api_key=_get_api_key("speechmatics"))
        
        elif self.service_name == "aws":
            # Lazy import - only loaded when AWS is used
            from pipecat.services.aws.stt import AWSTranscribeSTTService
            return AWSTranscribeSTTService()
        
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
        self._start_done.clear()
        try:
            async with aiohttp.ClientSession() as session:
                stt_service = self._create_stt_service(session)
                
                # Use cached analyzers for faster start (saves 200-500ms on subsequent recordings)
                smart_turn = _AnalyzerCache.get_smart_turn_analyzer()

                vad_analyzer = None
                # VAD is needed for:
                # 1. SegmentedSTTService (requires VAD for audio segmentation)
                # Note: For Soniox RT, we use SmartTurn V3 exclusively for turn detection.
                # Using both VAD and SmartTurn causes double UserStoppedSpeakingFrame events,
                # which triggers duplicate text injection.
                needs_vad = isinstance(stt_service, SegmentedSTTService)
                    
                if needs_vad:
                    vad_analyzer = _AnalyzerCache.get_vad_analyzer()
                    if not vad_analyzer:
                        logger.warning("VAD analyzer required but not available; transcripts may not finalize properly.")

                # Always use our custom MicrophoneInput to support on_audio_level callback
                self.audio_input = MicrophoneInput(
                    sample_rate=Config.SAMPLE_RATE,
                    channels=Config.CHANNELS,
                    block_size=Config.MIC_BLOCK_SIZE,
                    turn_analyzer=smart_turn,
                    vad_analyzer=vad_analyzer,
                    device=_resolve_mic_device(Config.MIC_DEVICE),
                    keep_alive=Config.MIC_ALWAYS_ON,
                    on_audio_level=self.on_audio_level,
                    on_ready=self.on_mic_ready,
                )

                inject_immediately = self.service_name == "soniox" and not (self.service_name == "soniox_async" or Config.SONIOX_MODE == "async")
                text_injector = TextInjector(inject_immediately=inject_immediately)
                self.text_injector = text_injector
                transcript_cb = (
                    TranscriptionCallbackProcessor(self.on_transcription) if self.on_transcription else None
                )
                steps = [self.audio_input, stt_service]
                if transcript_cb:
                    steps.append(transcript_cb)
                steps.append(text_injector)

                self.pipeline = Pipeline(steps)
                self.task = PipelineTask(
                    self.pipeline,
                    params=PipelineParams(allow_interruptions=True),
                    check_dangling_tasks=False,  # suppress false-positive dangling task warnings (e.g., Soniox keepalive)
                )
                # Disable signal handling because runner executes in background thread
                self.runner = PipelineRunner(handle_sigint=False, handle_sigterm=False)
                self.is_active = True

                if self.on_status_change:
                    self.on_status_change("Listening")

                await self.runner.run(self.task)

        except (ValueError, ImportError) as e:
            logger.error(f"Configuration error: {e}")
            self.is_active = False
            if self.on_status_change:
                self.on_status_change(f"Error: {e}")
            raise
        except Exception as e:
            logger.error(f"Error starting pipeline: {e}")
            self.is_active = False
            if self.on_status_change:
                self.on_status_change("Error")
            raise
        finally:
            # Ensure stop() can always unblock, even if start() exits due to an error or cancellation.
            self.is_active = False
            self._start_done.set()

    async def transcribe_file(self, file_path: str) -> None:
        if self.is_active:
            return
        logger.info(f"Transcribing audio file with {self.service_name}: {file_path}")
        self._start_done.clear()
        file_input: FfmpegAudioFileInput | None = None
        try:
            async with aiohttp.ClientSession() as session:
                stt_service = self._create_stt_service(session)

                vad_analyzer = None
                if isinstance(stt_service, SegmentedSTTService):
                    vad_analyzer = _AnalyzerCache.get_vad_analyzer()
                    if not vad_analyzer:
                        logger.warning("Segmented STT requires VAD; transcripts may be empty.")

                file_input = FfmpegAudioFileInput(
                    file_path,
                    sample_rate=Config.SAMPLE_RATE,
                    channels=Config.CHANNELS,
                    vad_analyzer=vad_analyzer,
                )

                transcript_cb = (
                    TranscriptionCallbackProcessor(self.on_transcription) if self.on_transcription else None
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
        if not tokens:
            return ""
        
        segments = []
        current_speaker = None
        current_text = []
        
        for token in tokens:
            text = token.get("text", "")
            speaker = token.get("speaker", "")
            
            if speaker and speaker != current_speaker:
                # Save previous segment
                if current_text and current_speaker:
                    segments.append((current_speaker, "".join(current_text).strip()))
                current_speaker = speaker
                current_text = [text]
            else:
                current_text.append(text)
        
        # Don't forget last segment
        if current_text and current_speaker:
            segments.append((current_speaker, "".join(current_text).strip()))
        
        # Format with speaker labels
        if not segments:
            # Fallback: just concatenate all text
            return "".join(t.get("text", "") for t in tokens).strip()
        
        lines = []
        for speaker, text in segments:
            if text:
                lines.append(f"[Speaker {speaker}]: {text}")
        
        return "\n\n".join(lines)

    async def transcribe_file_direct(self, file_path: str) -> None:
        """
        Transcribe audio/video file by uploading directly to Soniox async API.
        This bypasses the PCM conversion pipeline and uploads the original file format.
        Much more efficient for file transcription since Soniox accepts MP3, WAV, M4A, etc.
        """
        from pathlib import Path
        import mimetypes

        if self.is_active:
            return

        path = Path(file_path)
        if not path.exists():
            raise ValueError(f"File not found: {file_path}")

        logger.info(f"Transcribing file directly with Soniox async: {file_path}")
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

            api_key = Config.get_api_key("soniox")
            if not api_key:
                raise ValueError("Soniox API key is missing")

            headers = {"Authorization": f"Bearer {api_key}"}
            base_url = "https://api.soniox.com/v1"
            model = Config.SONIOX_ASYNC_MODEL or "stt-async-v3"

            if self.on_progress:
                self.on_progress("Uploading audio...")

            async with aiohttp.ClientSession() as session:
                # Upload file directly
                with open(path, "rb") as f:
                    file_bytes = f.read()

                logger.info(f"Uploading {path.name} ({len(file_bytes)} bytes, {content_type})")

                data = aiohttp.FormData()
                data.add_field("file", file_bytes, filename=path.name, content_type=content_type)

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
                self.on_status_change("Stopped")
            return
        if not self.is_active:
            return
        logger.info("Stopping Scriber Pipeline")

        is_soniox_async = (
            self.service_name == "soniox_async"
            or (self.service_name == "soniox" and Config.SONIOX_MODE == "async")
        )
        if self.on_status_change:
            self.on_status_change("Transcribing..." if is_soniox_async else "Stopping...")
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
                        if websocket and websocket.state is State.OPEN:
                            # Send stop_recording (empty string) to trigger finalization
                            await websocket.send("")
                            
                            # Wait for receive task to complete (gets final tokens + finished=True)
                            receive_task = getattr(step, "_receive_task", None)
                            if receive_task and not receive_task.done():
                                try:
                                    await asyncio.wait_for(asyncio.shield(receive_task), timeout=3.0)
                                    soniox_manual_stop_done = True
                                except asyncio.TimeoutError:
                                    logger.warning("Soniox receive task timeout (3s)")
                                except asyncio.CancelledError:
                                    pass
                                except Exception:
                                    pass
                    except Exception:
                        pass

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
            wait_timeout = 600.0 if is_soniox_async else 30.0

        try:
            # Wait for either start_done (pipeline completely finished) or timeout
            await asyncio.wait_for(self._start_done.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout while stopping pipeline (>{wait_timeout}s); forcing cancel")
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

        self.is_active = False
        if self.on_status_change:
            self.on_status_change("Stopped")
