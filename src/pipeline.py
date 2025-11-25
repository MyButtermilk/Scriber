import asyncio
from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.pipeline.runner import PipelineRunner

# Use our custom MicrophoneInput or try SoundDeviceAudioInputStream if available
try:
    from pipecat.audio.streams.input import SoundDeviceAudioInputStream
except ImportError:
    SoundDeviceAudioInputStream = None

# SmartTurn
try:
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.processors.user_idle_processor import UserIdleProcessor
    HAS_SMART_TURN = True
except ImportError:
    HAS_SMART_TURN = False
    logger.warning("LocalSmartTurnAnalyzerV3 or UserIdleProcessor not available")

# STT Services Imports
try:
    from pipecat.services.soniox.stt import SonioxSTTService, SonioxInputParams, SonioxContextObject
except ImportError:
    SonioxSTTService = None
    SonioxInputParams = None
    SonioxContextObject = None

from pipecat.services.assemblyai.stt import AssemblyAISTTService
from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.elevenlabs.stt import ElevenLabsSTTService

# Additional Services
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

    def _create_stt_service(self):
        if self.service_name == "soniox":
            if not Config.SONIOX_API_KEY:
                logger.warning("Soniox API Key missing. Ensure SONIOX_API_KEY is set.")
            if not SonioxSTTService:
                raise ImportError("SonioxSTTService not available")

            # Parse custom vocab
            params = None
            if Config.CUSTOM_VOCAB and SonioxInputParams and SonioxContextObject:
                terms = [t.strip() for t in Config.CUSTOM_VOCAB.split(",") if t.strip()]
                if terms:
                    logger.info(f"Applying custom vocabulary: {terms}")
                    params = SonioxInputParams(
                        context=SonioxContextObject(terms=terms)
                    )

            return SonioxSTTService(api_key=Config.SONIOX_API_KEY or "test", params=params)

        elif self.service_name == "assemblyai":
            if not Config.ASSEMBLYAI_API_KEY:
                logger.warning("AssemblyAI API Key missing")
            return AssemblyAISTTService(api_key=Config.ASSEMBLYAI_API_KEY or "test")

        elif self.service_name == "google":
            return GoogleSTTService()

        elif self.service_name == "elevenlabs":
            if not Config.ELEVENLABS_API_KEY:
                logger.warning("ElevenLabs API Key missing")
            return ElevenLabsSTTService(api_key=Config.ELEVENLABS_API_KEY or "test")

        elif self.service_name == "deepgram":
            if not Config.DEEPGRAM_API_KEY:
                logger.warning("Deepgram API Key missing")
            return DeepgramSTTService(api_key=Config.DEEPGRAM_API_KEY or "test")

        elif self.service_name == "openai":
            if not Config.OPENAI_API_KEY:
                logger.warning("OpenAI API Key missing")
            return OpenAISTTService(api_key=Config.OPENAI_API_KEY or "test")

        elif self.service_name == "azure":
            if not Config.AZURE_SPEECH_KEY or not Config.AZURE_SPEECH_REGION:
                logger.warning("Azure Speech Key or Region missing")
            return AzureSTTService(
                api_key=Config.AZURE_SPEECH_KEY or "test",
                region=Config.AZURE_SPEECH_REGION or "westus"
            )

        elif self.service_name == "gladia":
            if not Config.GLADIA_API_KEY:
                logger.warning("Gladia API Key missing")
            return GladiaSTTService(api_key=Config.GLADIA_API_KEY or "test")

        elif self.service_name == "groq":
            if not Config.GROQ_API_KEY:
                logger.warning("Groq API Key missing")
            return GroqSTTService(api_key=Config.GROQ_API_KEY or "test")

        elif self.service_name == "speechmatics":
            if not Config.SPEECHMATICS_API_KEY:
                logger.warning("Speechmatics API Key missing")
            return SpeechmaticsSTTService(api_key=Config.SPEECHMATICS_API_KEY or "test")

        elif self.service_name == "aws":
            # AWS usually checks env vars, but class might allow passing params
            return AWSTranscribeSTTService()

        else:
            raise ValueError(f"Unknown service: {self.service_name}")

    async def start(self):
        if self.is_active:
            return

        logger.info(f"Starting Scriber Pipeline with {self.service_name}")

        try:
            # Input
            if SoundDeviceAudioInputStream:
                self.audio_input = SoundDeviceAudioInputStream(
                    sample_rate=Config.SAMPLE_RATE,
                    channels=Config.CHANNELS
                )
            else:
                self.audio_input = MicrophoneInput(
                    sample_rate=Config.SAMPLE_RATE,
                    channels=Config.CHANNELS
                )

            # STT
            stt_service = self._create_stt_service()

            # Injector
            text_injector = TextInjector()

            steps = [
                self.audio_input,
                stt_service,
                text_injector
            ]

            if HAS_SMART_TURN:
                logger.info("Enabling SmartTurn V3 via UserIdleProcessor")
                smart_turn = LocalSmartTurnAnalyzerV3()
                # UserIdleProcessor detects end of speech using the analyzer
                # and emits UserStoppedSpeakingFrame.
                # We place it before or after STT?
                # Usually parallel or just watching flow.
                # In pipeline, it processes frames.
                user_idle = UserIdleProcessor(callback=None, timeout=2.0, analyzer=smart_turn)
                # Note: timeout here acts as fallback if analyzer doesn't fire?
                # Actually, Pipecat docs suggest using it for interaction loops.
                # Adding it to the pipeline ensures the model runs.
                steps.insert(2, user_idle)

            self.pipeline = Pipeline(steps)

            self.task = PipelineTask(
                self.pipeline,
                params=PipelineParams(
                    allow_interruptions=True
                )
            )

            self.runner = PipelineRunner()

            self.is_active = True
            if self.on_status_change:
                self.on_status_change("Listening")

            await self.runner.run(self.task)

        except Exception as e:
            logger.error(f"Error starting pipeline: {e}")
            self.is_active = False
            if self.on_status_change:
                self.on_status_change("Error")

    async def stop(self):
        if not self.is_active:
            return

        logger.info("Stopping Scriber Pipeline")
        if self.task:
            await self.task.cancel()

        self.is_active = False
        if self.on_status_change:
            self.on_status_change("Stopped")
