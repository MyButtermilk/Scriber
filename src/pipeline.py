import asyncio
import aiohttp
from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.pipeline.runner import PipelineRunner

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

        if self.service_name == "soniox":
            if not _get_api_key("soniox"): raise ValueError("Soniox API Key is missing.")
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
                self.runner = PipelineRunner()
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

    async def stop(self):
        if not self.is_active:
            return
        logger.info("Stopping Scriber Pipeline")
        if self.task:
            await self.task.cancel()
        self.is_active = False
        if self.on_status_change:
            self.on_status_change("Stopped")
