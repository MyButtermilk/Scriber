import asyncio
import aiohttp
from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
    EndFrame,
    StartFrame,
    StopFrame,
    CancelFrame,
)
from pipecat.processors.frame_processor import FrameDirection
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

    def __init__(self, api_key: str, custom_vocab: str = "", model: str = "stt-async-preview", session: aiohttp.ClientSession = None):
        super().__init__()
        self.api_key = api_key
        self.custom_vocab = custom_vocab
        self.model = model
        self.session = session
        self._buffer = bytearray()
        self._sample_rate = None
        self._channels = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame):
            if not self._sample_rate:
                self._sample_rate = frame.sample_rate
            if not self._channels:
                self._channels = frame.num_channels
            self._buffer.extend(frame.audio)
            await self.push_frame(frame, direction)
        elif isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            try:
                text = await self._transcribe_async(bytes(self._buffer))
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
            self._buffer = bytearray()
        else:
            await self.push_frame(frame, direction)

    async def _transcribe_async(self, audio_bytes: bytes) -> str:
        """
        Upload audio to Soniox async API. Prefer WebM/Opus; retry with WAV if Soniox rejects.
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}

        # Two-pass strategy: webm first, wav fallback for duration/format errors
        for prefer_webm in (True, False):
            try:
                file_bytes, content_type, filename = await self._encode_audio(audio_bytes, prefer_webm=prefer_webm)
                data = aiohttp.FormData()
                data.add_field("file", file_bytes, filename=filename, content_type=content_type)
                async with self.session.post(f"{self.BASE_URL}/files", data=data, headers=headers) as resp:
                    resp.raise_for_status()
                    file_id = (await resp.json())["id"]

                payload = {"file_id": file_id, "model": self.model}
                if self.custom_vocab:
                    payload["context"] = self.custom_vocab

                async with self.session.post(f"{self.BASE_URL}/transcriptions", json=payload, headers=headers) as resp2:
                    resp2.raise_for_status()
                    transcription_id = (await resp2.json())["id"]

                # Poll status
                while True:
                    async with self.session.get(f"{self.BASE_URL}/transcriptions/{transcription_id}", headers=headers) as r:
                        r.raise_for_status()
                        status_payload = await r.json()
                        status = status_payload.get("status")
                        if status == "completed":
                            break
                        if status == "error":
                            raise RuntimeError(status_payload.get("error_message", "Soniox async error"))
                    await asyncio.sleep(1)

                async with self.session.get(f"{self.BASE_URL}/transcriptions/{transcription_id}/transcript", headers=headers) as r3:
                    r3.raise_for_status()
                    transcript_payload = await r3.json()
                    return transcript_payload.get("text", "")

            except Exception as e:
                if prefer_webm:
                    logger.warning(f"WebM upload failed ({e}); retrying with WAV fallback")
                    continue
                raise

        raise RuntimeError("Async transcription failed in all attempts.")

    async def _encode_audio(self, audio_bytes: bytes, prefer_webm: bool = True):
        """
        Encode raw PCM to WebM/Opus (preferred) or WAV.
        For WebM we first wrap the PCM into a temp WAV so ffmpeg knows the duration
        and writes proper metadata (more reliable than piping raw PCM).
        """
        import subprocess, shutil, io, wave, contextlib, tempfile, os

        sr = self._sample_rate or 16000
        ch = self._channels or 1

        if prefer_webm and shutil.which("ffmpeg"):
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_file:
                    with contextlib.closing(wave.open(wav_file, "wb")) as wf:
                        wf.setnchannels(ch)
                        wf.setsampwidth(2)  # int16
                        wf.setframerate(sr)
                        wf.writeframes(audio_bytes)
                    wav_path = wav_file.name

                webm_path = wav_path.replace(".wav", ".webm")
                remux_path = wav_path.replace(".wav", ".fixed.webm")

                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    wav_path,
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "32k",
                    "-f",
                    "webm",
                    webm_path,
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

                # Remux to ensure duration metadata is present (similar to fix-webm-duration)
                remux_cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    webm_path,
                    "-c",
                    "copy",
                    "-map",
                    "0",
                    remux_path,
                ]
                try:
                    subprocess.run(remux_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                    chosen_path = remux_path
                except Exception:
                    chosen_path = webm_path

                with open(chosen_path, "rb") as f:
                    webm_bytes = f.read()
                return webm_bytes, "audio/webm", "audio.webm"
            except Exception as e:
                logger.warning(f"WebM encode failed ({e}); falling back to WAV")
            finally:
                for p in ("wav_path", "webm_path", "remux_path"):
                    try:
                        fp = locals().get(p)
                        if fp and os.path.exists(fp):
                            os.remove(fp)
                    except Exception:
                        pass

        # WAV fallback
        buf = io.BytesIO()
        with contextlib.closing(wave.open(buf, "wb")) as wf:
            wf.setnchannels(ch)
            wf.setsampwidth(2)  # int16
            wf.setframerate(sr)
            wf.writeframes(audio_bytes)
        return buf.getvalue(), "audio/wav", "audio.wav"

from pipecat.services.assemblyai.stt import AssemblyAISTTService
from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.elevenlabs.stt import ElevenLabsSTTService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.azure.stt import AzureSTTService
from pipecat.services.gladia.stt import GladiaSTTService
from pipecat.services.groq.stt import GroqSTTService
from pipecat.services.speechmatics.stt import SpeechmaticsSTTService
from pipecat.services.aws.stt import AWSTranscribeSTTService

from src.config import Config
from src.injector import TextInjector
from src.microphone import MicrophoneInput

class ScriberPipeline:
    def __init__(self, service_name=Config.DEFAULT_STT_SERVICE, on_status_change=None):
        self.service_name = service_name
        self.on_status_change = on_status_change
        self.pipeline = None
        self.task = None
        self.runner = None
        self.audio_input = None
        self.is_active = False

    def _create_stt_service(self, session: aiohttp.ClientSession):

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
                )
            if not SonioxSTTService: raise ImportError("SonioxSTTService not available.")
            params = SonioxInputParams() if SonioxInputParams else _SonioxParamsFallback()
            if Config.CUSTOM_VOCAB and SonioxContextObject:
                terms = [t.strip() for t in Config.CUSTOM_VOCAB.split(",") if t.strip()]
                if terms:
                    logger.info(f"Applying custom vocabulary: {terms}")
                    params = SonioxInputParams(context=SonioxContextObject(terms=terms)) if SonioxInputParams else _SonioxParamsFallback(context=SonioxContextObject(terms=terms))
            return SonioxSTTService(api_key=_get_api_key("soniox"), params=params)

        elif self.service_name == "assemblyai":
            if not _get_api_key("assemblyai"): raise ValueError("AssemblyAI API Key is missing.")
            return AssemblyAISTTService(api_key=_get_api_key("assemblyai"), aiohttp_session=session)
        elif self.service_name == "google":
            return GoogleSTTService()
        elif self.service_name == "elevenlabs":
            if not _get_api_key("elevenlabs"): raise ValueError("ElevenLabs API Key is missing.")
            return ElevenLabsSTTService(api_key=_get_api_key("elevenlabs"), aiohttp_session=session)
        elif self.service_name == "deepgram":
            if not _get_api_key("deepgram"): raise ValueError("Deepgram API Key is missing.")
            return DeepgramSTTService(api_key=_get_api_key("deepgram"))
        elif self.service_name == "openai":
            if not _get_api_key("openai"): raise ValueError("OpenAI API Key is missing.")
            return OpenAISTTService(api_key=_get_api_key("openai"), aiohttp_session=session)
        elif self.service_name == "azure":
            if not Config.AZURE_SPEECH_KEY or not Config.AZURE_SPEECH_REGION: raise ValueError("Azure Speech Key or Region is missing.")
            return AzureSTTService(api_key=Config.AZURE_SPEECH_KEY, region=Config.AZURE_SPEECH_REGION)
        elif self.service_name == "gladia":
            if not _get_api_key("gladia"): raise ValueError("Gladia API Key is missing.")
            return GladiaSTTService(api_key=_get_api_key("gladia"), aiohttp_session=session)
        elif self.service_name == "groq":
            if not _get_api_key("groq"): raise ValueError("Groq API Key is missing.")
            return GroqSTTService(api_key=_get_api_key("groq"), aiohttp_session=session)
        elif self.service_name == "speechmatics":
            if not _get_api_key("speechmatics"): raise ValueError("Speechmatics API Key is missing.")
            return SpeechmaticsSTTService(api_key=_get_api_key("speechmatics"))
        elif self.service_name == "aws":
            return AWSTranscribeSTTService()
        else:
            raise ValueError(f"Unknown service: {self.service_name}")

    async def start(self):
        if self.is_active:
            return
        logger.info(f"Starting Scriber Pipeline with {self.service_name}")
        try:
            async with aiohttp.ClientSession() as session:
                stt_service = self._create_stt_service(session)
                smart_turn = LocalSmartTurnAnalyzerV3() if HAS_SMART_TURN else None
                if smart_turn:
                    logger.info("Enabling SmartTurn V3")

                if SoundDeviceAudioInputStream:
                    # If the built-in stream is available, prefer it and configure turn analyzer via params if supported.
                    self.audio_input = SoundDeviceAudioInputStream(sample_rate=Config.SAMPLE_RATE, channels=Config.CHANNELS)
                else:
                    self.audio_input = MicrophoneInput(
                        sample_rate=Config.SAMPLE_RATE,
                        channels=Config.CHANNELS,
                        turn_analyzer=smart_turn,
                    )

                text_injector = TextInjector()
                steps = [self.audio_input, stt_service, text_injector]

                self.pipeline = Pipeline(steps)
                self.task = PipelineTask(self.pipeline, params=PipelineParams(allow_interruptions=True))
                # Disable signal handling because runner executes in background thread
                self.runner = PipelineRunner(handle_sigint=False, handle_sigterm=False)
                self.is_active = True

                if self.on_status_change:
                    self.on_status_change("Listening")

                await self.runner.run(self.task)
                # If async mode, ensure any buffered audio gets finalized
                if isinstance(stt_service, SonioxAsyncProcessor):
                    await stt_service.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

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

    async def stop(self):
        if not self.is_active:
            return
        logger.info("Stopping Scriber Pipeline")
        if self.task:
            try:
                await self.task.stop_when_done()
            except Exception:
                # Fallback to cancel if graceful stop fails
                await self.task.cancel()
        self.is_active = False
        if self.on_status_change:
            self.on_status_change("Stopped")
