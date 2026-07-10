import asyncio
import io

import pytest
from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transcriptions.language import Language
from websockets.protocol import State

from src.config import Config
from src.pipeline import (
    PipecatVadSpeechObserver,
    ScriberPipeline,
    SegmentedSTTRecordingGate,
    SonioxAsyncProcessor,
    TranscriptionCallbackProcessor,
)


class _DummyTask:
    def __init__(self, done_event: asyncio.Event, *, set_done_on_stop: bool):
        self._done_event = done_event
        self._set_done_on_stop = set_done_on_stop
        self._finished = False
        self.stop_when_done_called = False
        self.cancel_called = False

    def has_finished(self) -> bool:
        return self._finished

    async def stop_when_done(self):
        self.stop_when_done_called = True
        if self._set_done_on_stop:
            await asyncio.sleep(0)
            self._finished = True
            self._done_event.set()

    async def cancel(self, *, reason: str | None = None):
        self.cancel_called = True
        self._finished = True
        self._done_event.set()


class _DummyRunner:
    def __init__(self):
        self.cancel_called = False

    async def cancel(self):
        self.cancel_called = True


class _DummyPipelineGraph:
    def __init__(self, processors):
        self.processors = processors


class _DummyRuntimePipelineGraph:
    def __init__(self, steps):
        self.processors = steps
        self._processors = steps
        self.steps = steps


class _PushRecordingPipelineGraph(_DummyRuntimePipelineGraph):
    def __init__(self, steps=None):
        super().__init__(steps or [])
        self.pushed = []

    async def push_frame(self, frame, *, direction):
        self.pushed.append((type(frame), direction))


class _BufferedProvider:
    def __init__(self):
        self._buffer_size = 1024
        self._skip_terminal_transcription = False


class _RejectedUploadResponse:
    status = 415

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return "unsupported request"

    def raise_for_status(self):
        raise RuntimeError("upload rejected")


class _RejectedSonioxUploadSession:
    def __init__(self):
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _RejectedUploadResponse()


@pytest.mark.asyncio
async def test_soniox_async_webm_upload_does_not_retry_as_wav_on_api_error():
    session = _RejectedSonioxUploadSession()
    processor = SonioxAsyncProcessor(api_key="test-key", session=session)

    async def encode_webm(_audio_bytes, *, prefer_webm=True):
        assert prefer_webm is True
        return b"webm-audio", "audio/webm", "audio.webm"

    processor._encode_audio = encode_webm

    with pytest.raises(RuntimeError, match="upload rejected"):
        await processor._transcribe_async(b"\0\0" * 160)

    assert len(session.post_calls) == 1
    assert session.post_calls[0][0] == "https://api.soniox.com/v1/files"


@pytest.mark.asyncio
async def test_soniox_async_terminal_frame_streams_spooled_audio():
    processor = SonioxAsyncProcessor(api_key="test-key", session=object())
    captured = []

    async def _capture_push(frame, direction):
        captured.append((frame, direction))

    async def _transcribe(audio_bytes=None, *, audio_stream=None, audio_size=None):
        assert audio_bytes is None
        assert audio_stream is processor._buffer
        assert audio_size == 640
        return "streamed transcript"

    processor.push_frame = _capture_push
    processor._transcribe_async = _transcribe

    await processor.process_frame(
        InputAudioRawFrame(audio=b"\0" * 640, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    assert any(
        isinstance(frame, TranscriptionFrame) and frame.text == "streamed transcript"
        for frame, _direction in captured
    )
    assert processor._buffer_size == 0


@pytest.mark.asyncio
async def test_soniox_wav_stream_fallback_never_uses_unbounded_read():
    class BoundedReadStream(io.BytesIO):
        def read(self, size=-1):
            assert size >= 0
            return super().read(size)

    processor = SonioxAsyncProcessor(api_key="test-key", session=object())
    source = BoundedReadStream(b"\0\0" * 320)

    encoded, content_type, filename, cleanup_paths = await processor._encode_audio_stream(
        source,
        prefer_webm=False,
    )
    try:
        assert encoded.read(4) == b"RIFF"
        assert content_type == "audio/wav"
        assert filename == "audio.wav"
        assert cleanup_paths == ()
    finally:
        encoded.close()


class _DummyAudioInput:
    def __init__(self, events: list[str] | None = None) -> None:
        self.close_stream = None
        self._events = events

    async def stop(self, frame, *, close_stream=None):
        self.close_stream = close_stream
        if self._events is not None:
            self._events.append("audio_stop")


class _DummyFileInput:
    def __init__(self) -> None:
        self.stop_called = False

    async def stop(self, frame):
        assert isinstance(frame, EndFrame)
        self.stop_called = True


class _SegmentedFinalizationAudioInput:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def stop_capture_for_finalization(self, *, close_stream=None):
        self.events.append("capture_stop")

    async def stop(self, frame, *, close_stream=None):
        self.events.append("audio_cleanup_stop")


class _RecordingStopTask(_DummyTask):
    def __init__(self, done_event: asyncio.Event, events: list[str]):
        super().__init__(done_event, set_done_on_stop=True)
        self.events = events

    async def stop_when_done(self):
        self.events.append("task_stop_when_done")
        await super().stop_when_done()


class _DummyPrewarmManager:
    def __init__(self, events: list[str] | None = None, *, resume_result: bool = True) -> None:
        self.detach_calls = 0
        self.resume_calls = 0
        self._events = events
        self._resume_result = resume_result

    def detach_active_capture(self, _callback=None) -> bool:
        self.detach_calls += 1
        if self._events is not None:
            self._events.append("prewarm_detach")
        return False

    def resume_after_active_capture(self) -> bool:
        self.resume_calls += 1
        if self._events is not None:
            self._events.append("prewarm_resume")
        return self._resume_result


@pytest.mark.asyncio
async def test_aborted_file_pipeline_cancels_runner_task_and_input():
    pipe = ScriberPipeline(service_name="soniox")
    done = asyncio.Event()
    pipe.task = _DummyTask(done, set_done_on_stop=False)
    pipe.runner = _DummyRunner()
    file_input = _DummyFileInput()
    run_task = asyncio.create_task(asyncio.Event().wait())

    await pipe._cleanup_aborted_file_pipeline(run_task, file_input)

    assert pipe.task.cancel_called is True
    assert pipe.runner.cancel_called is True
    assert run_task.done() is True
    assert file_input.stop_called is True


def test_buffered_provider_factories_disable_diarization_for_live_by_default(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_API_KEY", "key")
    monkeypatch.setattr(Config, "MISTRAL_API_KEY", "key")
    monkeypatch.setattr(Config, "SMALLEST_API_KEY", "key")
    monkeypatch.setattr(Config, "ASSEMBLYAI_API_KEY", "key")

    soniox = ScriberPipeline(service_name="soniox_async")._create_stt_service(object())
    mistral = ScriberPipeline(service_name="mistral_async")._create_stt_service(object())
    smallest = ScriberPipeline(service_name="smallest_async")._create_stt_service(object())
    assemblyai = ScriberPipeline(service_name="assemblyai")._create_stt_service(object())

    assert soniox.enable_speaker_diarization is False
    assert mistral._diarize is False
    assert smallest._diarize is False
    assert assemblyai._speaker_labels is False


def test_buffered_provider_factories_enable_diarization_for_batch_jobs(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_API_KEY", "key")
    monkeypatch.setattr(Config, "MISTRAL_API_KEY", "key")
    monkeypatch.setattr(Config, "SMALLEST_API_KEY", "key")
    monkeypatch.setattr(Config, "ASSEMBLYAI_API_KEY", "key")

    soniox = ScriberPipeline(
        service_name="soniox_async",
        enable_speaker_diarization=True,
    )._create_stt_service(object())
    mistral = ScriberPipeline(
        service_name="mistral_async",
        enable_speaker_diarization=True,
    )._create_stt_service(object())
    smallest = ScriberPipeline(
        service_name="smallest_async",
        enable_speaker_diarization=True,
    )._create_stt_service(object())
    assemblyai = ScriberPipeline(
        service_name="assemblyai",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert soniox.enable_speaker_diarization is True
    assert mistral._diarize is True
    assert smallest._diarize is True
    assert assemblyai._speaker_labels is True


def test_mistral_segmented_live_uses_transcribe_model_when_rt_model_is_realtime_only(monkeypatch):
    monkeypatch.setattr(Config, "MISTRAL_API_KEY", "key")
    monkeypatch.setattr(Config, "MISTRAL_RT_MODEL", "voxtral-mini-transcribe-realtime-2602")
    monkeypatch.setattr(Config, "MISTRAL_ASYNC_MODEL", "voxtral-mini-2602")

    service = ScriberPipeline(service_name="mistral")._create_stt_service(object())

    assert service._model == "voxtral-mini-2602"


@pytest.mark.parametrize(
    ("service_name", "api_key_attribute", "expected_class_name"),
    (
        ("soniox", "SONIOX_API_KEY", "SonioxSTTService"),
        ("assemblyai_realtime", "ASSEMBLYAI_API_KEY", "AssemblyAISTTService"),
        ("elevenlabs", "ELEVENLABS_API_KEY", "ElevenLabsRealtimeSTTService"),
        ("deepgram", "DEEPGRAM_API_KEY", "ScriberDeepgramSTTService"),
        ("gladia", "GLADIA_API_KEY", "ScriberGladiaSTTService"),
        ("groq", "GROQ_API_KEY", "GroqSTTService"),
        ("openai", "OPENAI_API_KEY", "OpenAIRealtimeSTTService"),
        ("speechmatics", "SPEECHMATICS_API_KEY", "SpeechmaticsSTTService"),
    ),
)
def test_pipecat_1_5_live_factories_match_runtime_signatures(
    monkeypatch,
    service_name,
    api_key_attribute,
    expected_class_name,
):
    monkeypatch.setattr(Config, api_key_attribute, "key")
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Scriber, Pipecat")

    service = ScriberPipeline(service_name=service_name)._create_stt_service(object())

    assert type(service).__name__ == expected_class_name


def test_google_cloud_factory_uses_pipecat_1_5_settings(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _GoogleSTTService:
        Settings = _Settings

        def __init__(self, *, credentials_path, sample_rate, settings):
            self.credentials_path = credentials_path
            self.sample_rate = sample_rate
            self.settings = settings

    module = type("GoogleModule", (), {"GoogleSTTService": _GoogleSTTService})
    monkeypatch.setattr(Config, "GOOGLE_APPLICATION_CREDENTIALS", "C:\\keys\\google.json")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="google")._create_stt_service(object())

    assert service.credentials_path == "C:\\keys\\google.json"
    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.settings.kwargs == {
        "languages": [Language.DE],
        "enable_automatic_punctuation": True,
        "enable_interim_results": True,
        "enable_voice_activity_events": False,
    }


def test_google_cloud_factory_omits_languages_for_auto_detection(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _GoogleSTTService:
        Settings = _Settings

        def __init__(self, *, credentials_path, sample_rate, settings):
            self.settings = settings

    module = type("GoogleModule", (), {"GoogleSTTService": _GoogleSTTService})
    monkeypatch.setattr(Config, "GOOGLE_APPLICATION_CREDENTIALS", "C:\\keys\\google.json")
    monkeypatch.setattr(Config, "LANGUAGE", "auto")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="google")._create_stt_service(object())

    assert "languages" not in service.settings.kwargs


def test_assemblyai_realtime_factory_uses_pipecat_1_5_settings_without_live_diarization(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _AssemblyAISTTService:
        Settings = _Settings

        def __init__(self, *, api_key, sample_rate, settings, vad_force_turn_endpoint):
            self.api_key = api_key
            self.sample_rate = sample_rate
            self.settings = settings
            self.vad_force_turn_endpoint = vad_force_turn_endpoint

    module = type(
        "AssemblyAIModule",
        (),
        {"AssemblyAISTTService": _AssemblyAISTTService},
    )

    monkeypatch.setattr(Config, "ASSEMBLYAI_API_KEY", "key")
    monkeypatch.setattr(Config, "ASSEMBLYAI_RT_MODEL", "universal-3-5-pro")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Scriber, Pipecat")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(
        service_name="assemblyai_realtime",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert service.api_key == "key"
    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.vad_force_turn_endpoint is True
    assert service.settings.kwargs == {
        "model": "universal-3-5-pro",
        "language_code": "de",
        "keyterms_prompt": ["Scriber", "Pipecat"],
        "speaker_labels": False,
    }


def test_assemblyai_realtime_factory_filters_unsupported_settings_keywords(monkeypatch):
    class _Settings:
        def __init__(self, *, model, keyterms_prompt=None, speaker_labels=None):
            self.kwargs = {
                "model": model,
                "keyterms_prompt": keyterms_prompt,
                "speaker_labels": speaker_labels,
            }

    class _AssemblyAISTTService:
        Settings = _Settings

        def __init__(self, *, api_key, sample_rate, settings, language=None, vad_force_turn_endpoint):
            self.api_key = api_key
            self.sample_rate = sample_rate
            self.settings = settings
            self.language = language
            self.vad_force_turn_endpoint = vad_force_turn_endpoint

    module = type(
        "AssemblyAIModule",
        (),
        {"AssemblyAISTTService": _AssemblyAISTTService},
    )

    monkeypatch.setattr(Config, "ASSEMBLYAI_API_KEY", "key")
    monkeypatch.setattr(Config, "ASSEMBLYAI_RT_MODEL", "universal-3-5-pro")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Scriber, Pipecat")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(
        service_name="assemblyai_realtime",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert service.settings.kwargs == {
        "model": "universal-3-5-pro",
        "keyterms_prompt": ["Scriber", "Pipecat"],
        "speaker_labels": False,
    }
    assert service.language.value == "de"


def test_assemblyai_realtime_factory_rejects_pre_1_5_pipecat(monkeypatch):
    class _AssemblyAISTTService:
        pass

    module = type(
        "AssemblyAIModule",
        (),
        {
            "AssemblyAISTTService": _AssemblyAISTTService,
        },
    )

    monkeypatch.setattr(Config, "ASSEMBLYAI_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "auto")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    with pytest.raises(RuntimeError, match="Pipecat 1.5.0"):
        ScriberPipeline(service_name="assemblyai_realtime")._create_stt_service(object())


def test_elevenlabs_factory_uses_realtime_service(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _ElevenLabsRealtimeSTTService:
        Settings = _Settings

        def __init__(self, *, api_key, sample_rate, commit_strategy, settings):
            self.api_key = api_key
            self.sample_rate = sample_rate
            self.commit_strategy = commit_strategy
            self.settings = settings

    class _CommitStrategy:
        MANUAL = "manual"

    module = type(
        "ElevenLabsModule",
        (),
        {
            "ElevenLabsRealtimeSTTService": _ElevenLabsRealtimeSTTService,
            "CommitStrategy": _CommitStrategy,
        },
    )

    monkeypatch.setattr(Config, "ELEVENLABS_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Scriber, Pipecat")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="elevenlabs")._create_stt_service(object())

    assert service.api_key == "key"
    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.commit_strategy == "manual"
    assert service.settings.kwargs == {
        "model": "scribe_v2_realtime",
        "language": Language.DE,
        "keyterms": ["Scriber", "Pipecat"],
    }


def test_deepgram_factory_disables_live_diarization_by_default(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _DeepgramSTTService:
        Settings = _Settings

        def __init__(self, *, api_key, encoding, channels, sample_rate, settings):
            self.api_key = api_key
            self.encoding = encoding
            self.channels = channels
            self.sample_rate = sample_rate
            self.settings = settings

    module = type(
        "DeepgramModule",
        (),
        {"DeepgramSTTService": _DeepgramSTTService},
    )

    monkeypatch.setattr(Config, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="deepgram")._create_stt_service(object())

    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.encoding == "linear16"
    assert service.channels == Config.CHANNELS
    assert service.settings.kwargs == {
        "model": "nova-3",
        "language": Language.DE,
        "interim_results": True,
        "smart_format": True,
        "punctuate": True,
        "diarize": False,
    }

    service_with_speakers = ScriberPipeline(
        service_name="deepgram",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert service_with_speakers.settings.kwargs == {
        "model": "nova-3",
        "language": Language.DE,
        "interim_results": True,
        "smart_format": True,
        "punctuate": True,
        "diarize": False,
    }


def test_speechmatics_factory_disables_labeled_diarization_by_default(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _SpeechmaticsSTTService:
        Settings = _Settings

        def __init__(self, *, api_key, sample_rate, settings):
            self.api_key = api_key
            self.sample_rate = sample_rate
            self.settings = settings

    module = type(
        "SpeechmaticsModule",
        (),
        {"SpeechmaticsSTTService": _SpeechmaticsSTTService},
    )

    monkeypatch.setattr(Config, "SPEECHMATICS_API_KEY", "key")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="speechmatics")._create_stt_service(object())

    assert service.settings.kwargs == {
        "enable_diarization": False,
        "language": Language.DE,
        "speaker_active_format": "[Speaker {speaker_id}]: {text}",
        "speaker_passive_format": "[Speaker {speaker_id}]: {text}",
    }

    service_with_speakers = ScriberPipeline(
        service_name="speechmatics",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert service_with_speakers.settings.kwargs == {
        "enable_diarization": False,
        "language": Language.DE,
        "speaker_active_format": "[Speaker {speaker_id}]: {text}",
        "speaker_passive_format": "[Speaker {speaker_id}]: {text}",
    }


def test_deepgram_live_factory_never_enables_diarization(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _DeepgramSTTService:
        Settings = _Settings

        def __init__(self, *, api_key, encoding, channels, sample_rate, settings):
            self.api_key = api_key
            self.encoding = encoding
            self.channels = channels
            self.sample_rate = sample_rate
            self.settings = settings

    module = type(
        "DeepgramModule",
        (),
        {"DeepgramSTTService": _DeepgramSTTService},
    )

    monkeypatch.setattr(Config, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(
        service_name="deepgram",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.settings.kwargs == {
        "model": "nova-3",
        "language": Language.DE,
        "interim_results": True,
        "smart_format": True,
        "punctuate": True,
        "diarize": False,
    }


def test_gladia_factory_uses_current_pipecat_signature(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _GladiaSTTService:
        Settings = _Settings

        def __init__(self, *, api_key, sample_rate, channels, settings):
            self.api_key = api_key
            self.sample_rate = sample_rate
            self.channels = channels
            self.settings = settings

    module = type(
        "GladiaModule",
        (),
        {"GladiaSTTService": _GladiaSTTService},
    )

    monkeypatch.setattr(Config, "GLADIA_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="gladia")._create_stt_service(object())

    assert service.api_key == "key"
    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.channels == Config.CHANNELS
    assert service.settings.kwargs == {
        "model": "solaria-1",
        "language": Language.DE,
        "enable_vad": False,
    }


@pytest.mark.asyncio
async def test_gladia_stop_sends_finalization_before_disconnect(monkeypatch):
    monkeypatch.setattr(Config, "GLADIA_API_KEY", "key")
    pipeline = ScriberPipeline(service_name="gladia")
    service = pipeline._create_stt_service(object())
    pipeline._final_transcription_received.set()
    events: list[str] = []

    async def _send_stop_recording():
        events.append("stop_recording")

    async def _disconnect():
        events.append("disconnect")

    service._send_stop_recording = _send_stop_recording
    service._disconnect = _disconnect

    await service.stop(EndFrame())

    assert events == ["stop_recording", "disconnect"]


def test_speechmatics_factory_enables_batch_labeled_diarization(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _SpeechmaticsSTTService:
        Settings = _Settings

        def __init__(self, *, api_key, sample_rate, settings):
            self.api_key = api_key
            self.sample_rate = sample_rate
            self.settings = settings

    module = type(
        "SpeechmaticsModule",
        (),
        {"SpeechmaticsSTTService": _SpeechmaticsSTTService},
    )

    monkeypatch.setattr(Config, "SPEECHMATICS_API_KEY", "key")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(
        service_name="speechmatics",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert service.settings.kwargs == {
        "enable_diarization": False,
        "language": Language.DE,
        "speaker_active_format": "[Speaker {speaker_id}]: {text}",
        "speaker_passive_format": "[Speaker {speaker_id}]: {text}",
    }


@pytest.mark.asyncio
async def test_stop_waits_for_start_done_gracefully():
    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _DummyTask(pipeline._start_done, set_done_on_stop=True)

    await pipeline.stop(timeout_secs=0.5)

    assert pipeline.task.stop_when_done_called is True
    assert pipeline.task.cancel_called is False


@pytest.mark.asyncio
async def test_stop_times_out_and_cancels():
    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _DummyTask(pipeline._start_done, set_done_on_stop=False)

    with pytest.raises(RuntimeError, match="Transcription did not finish within"):
        await pipeline.stop(timeout_secs=0.01)

    assert pipeline.task.stop_when_done_called is True
    assert pipeline.task.cancel_called is True


@pytest.mark.asyncio
async def test_segmented_stt_stop_waits_for_final_before_pipeline_shutdown(monkeypatch):
    monkeypatch.setenv("SCRIBER_SEGMENTED_STT_STOP_FINAL_TIMEOUT_SEC", "1")
    events: list[str] = []
    pipeline = ScriberPipeline(service_name="openai", on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _RecordingStopTask(pipeline._start_done, events)
    pipeline.audio_input = _SegmentedFinalizationAudioInput(events)

    class _FinalizingGate(SegmentedSTTRecordingGate):
        async def flush_segment(self, *, direction=FrameDirection.DOWNSTREAM) -> bool:
            events.append("segment_flush")

            async def _mark_final():
                await asyncio.sleep(0.03)
                events.append("provider_final")
                pipeline._mark_final_transcription_received()

            asyncio.create_task(_mark_final())
            return True

    pipeline.pipeline = _DummyRuntimePipelineGraph([
        _FinalizingGate(vad_segmentation_enabled=False)
    ])

    await pipeline.stop(timeout_secs=1.0)

    assert events.index("capture_stop") < events.index("segment_flush")
    assert events.index("segment_flush") < events.index("provider_final")
    assert events.index("provider_final") < events.index("audio_cleanup_stop")
    assert events.index("provider_final") < events.index("task_stop_when_done")


@pytest.mark.asyncio
async def test_soniox_realtime_stop_does_not_wait_for_reconnecting_receive_task(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")

    class _FakeWebSocket:
        state = State.OPEN

        def __init__(self):
            self.sent: list[str] = []

        async def send(self, data):
            self.sent.append(data)

    class SonioxSTTService:
        def __init__(self, receive_task):
            self._websocket = _FakeWebSocket()
            self._receive_task = receive_task
            self._audio_bytes_sent = 0

    async def _never_finishes():
        await asyncio.Event().wait()

    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _DummyTask(pipeline._start_done, set_done_on_stop=False)
    receive_task = asyncio.create_task(_never_finishes())
    soniox = SonioxSTTService(receive_task)
    pipeline.pipeline = _DummyRuntimePipelineGraph([soniox])

    async def _mark_final_after_stop_signal():
        await asyncio.sleep(0.2)
        pipeline._mark_final_transcription_received()

    marker_task = asyncio.create_task(_mark_final_after_stop_signal())

    await asyncio.wait_for(pipeline.stop(timeout_secs=1.0), timeout=1.0)

    assert soniox._websocket.sent == [""]
    assert pipeline.task.cancel_called is True
    assert pipeline.task.stop_when_done_called is False
    assert receive_task.cancelled()
    await marker_task


@pytest.mark.asyncio
async def test_cleanup_audio_input_forces_stream_close():
    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    audio_input = _DummyAudioInput()
    pipeline.audio_input = audio_input

    await pipeline._cleanup_audio_input()

    assert audio_input.close_stream is True
    assert pipeline.audio_input is None


@pytest.mark.asyncio
async def test_cleanup_audio_input_resumes_always_on_prewarm_immediately(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    events: list[str] = []
    prewarm = _DummyPrewarmManager(events)
    pipeline = ScriberPipeline(
        service_name="soniox",
        on_status_change=None,
        mic_prewarm_manager=prewarm,
    )
    audio_input = _DummyAudioInput(events)
    pipeline.audio_input = audio_input

    await pipeline._cleanup_audio_input()

    assert audio_input.close_stream is True
    assert pipeline.audio_input is None
    assert prewarm.detach_calls == 1
    assert prewarm.resume_calls == 1
    assert events == ["prewarm_detach", "prewarm_resume", "audio_stop"]


@pytest.mark.asyncio
async def test_cleanup_audio_input_retries_prewarm_resume_after_stop(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    events: list[str] = []
    prewarm = _DummyPrewarmManager(events, resume_result=False)
    pipeline = ScriberPipeline(
        service_name="soniox",
        on_status_change=None,
        mic_prewarm_manager=prewarm,
    )
    audio_input = _DummyAudioInput(events)
    pipeline.audio_input = audio_input

    await pipeline._cleanup_audio_input()

    assert audio_input.close_stream is True
    assert pipeline.audio_input is None
    assert prewarm.detach_calls == 1
    assert prewarm.resume_calls == 2
    assert events == [
        "prewarm_detach",
        "prewarm_resume",
        "audio_stop",
        "prewarm_resume",
    ]


def test_pipeline_delegates_audio_diagnostics_and_health():
    class _HealthAudioInput:
        def __init__(self):
            self.health_calls = []

        def diagnostic_snapshot(self):
            return {"running": True, "streamActive": False}

        def ensure_stream_health(self, **kwargs):
            self.health_calls.append(kwargs)
            return True

    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    audio_input = _HealthAudioInput()
    pipeline.audio_input = audio_input

    assert pipeline.audio_diagnostics() == {"running": True, "streamActive": False}
    assert pipeline.ensure_audio_health(
        reason="test",
        max_callback_gap_seconds=3.0,
    ) is True
    assert audio_input.health_calls == [
        {"reason": "test", "max_callback_gap_seconds": 3.0}
    ]


def test_pipeline_audio_diagnostics_merges_pipecat_vad_snapshot():
    class _HealthAudioInput:
        def diagnostic_snapshot(self):
            return {"running": True, "speechObserved": False}

    class _VadObserver:
        def snapshot(self):
            return {
                "enabled": True,
                "speechObserved": True,
                "speechStartedCount": 1,
            }

    pipeline = ScriberPipeline(service_name="soniox_async", on_status_change=None)
    pipeline.audio_input = _HealthAudioInput()
    pipeline._vad_observer = _VadObserver()

    diagnostics = pipeline.audio_diagnostics()

    assert diagnostics["speechObserved"] is True
    assert diagnostics["speechObservedByVad"] is True
    assert diagnostics["pipecatVad"]["speechStartedCount"] == 1


@pytest.mark.asyncio
async def test_pipecat_vad_observer_counts_audio_frames():
    observer = PipecatVadSpeechObserver(enabled=True)
    pushed = []

    async def _capture_push(frame, direction):
        pushed.append((frame, direction))

    observer.push_frame = _capture_push

    await observer.process_frame(
        InputAudioRawFrame(audio=b"\0" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(
        InputAudioRawFrame(audio=b"\0" * 320, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )

    snapshot = observer.snapshot()

    assert snapshot["audioFrameCount"] == 2
    assert snapshot["speechStartedCount"] == 0
    assert snapshot["speechObserved"] is False
    assert len(pushed) == 2


@pytest.mark.asyncio
async def test_live_vad_finalization_flushes_when_hotkey_stops_during_speech():
    class _VadObserver:
        def snapshot(self):
            return {
                "speechObserved": True,
                "speaking": True,
                "speechStoppedCount": 0,
            }

    pipeline = ScriberPipeline(service_name="deepgram", on_status_change=None)
    runtime_pipeline = _PushRecordingPipelineGraph()
    pipeline.pipeline = runtime_pipeline
    pipeline._vad_observer = _VadObserver()

    flushed = await pipeline._flush_live_vad_finalization_turn()

    assert flushed is True
    assert runtime_pipeline.pushed == [
        (VADUserStoppedSpeakingFrame, FrameDirection.DOWNSTREAM)
    ]


@pytest.mark.asyncio
async def test_segmented_stt_gate_defaults_to_whole_recording_segment():
    gate = SegmentedSTTRecordingGate(vad_segmentation_enabled=False)
    pushed = []

    async def _capture_push(frame, direction):
        pushed.append((type(frame), direction))

    gate.push_frame = _capture_push

    await gate.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    assert pushed == [
        (VADUserStartedSpeakingFrame, FrameDirection.DOWNSTREAM),
        (VADUserStoppedSpeakingFrame, FrameDirection.DOWNSTREAM),
        (EndFrame, FrameDirection.DOWNSTREAM),
    ]


@pytest.mark.asyncio
async def test_segmented_stt_gate_can_pass_vad_segments_when_enabled():
    gate = SegmentedSTTRecordingGate(vad_segmentation_enabled=True)
    pushed = []

    async def _capture_push(frame, direction):
        pushed.append((type(frame), direction))

    gate.push_frame = _capture_push

    await gate.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    flushed = await gate.flush_segment(direction=FrameDirection.DOWNSTREAM)
    flushed_again = await gate.flush_segment(direction=FrameDirection.DOWNSTREAM)

    assert flushed is True
    assert flushed_again is False
    assert pushed == [
        (VADUserStartedSpeakingFrame, FrameDirection.DOWNSTREAM),
        (VADUserStoppedSpeakingFrame, FrameDirection.DOWNSTREAM),
    ]


@pytest.mark.asyncio
async def test_transcription_callback_uses_plain_text_without_diarization():
    captured: list[tuple[str, bool]] = []
    pushed = []
    processor = TranscriptionCallbackProcessor(
        lambda text, is_final: captured.append((text, is_final))
    )

    async def _capture_push(frame, direction):
        pushed.append((frame, direction))

    processor.push_frame = _capture_push

    await processor.process_frame(
        TranscriptionFrame(
            text="Hallo plain fallback",
            user_id="user",
            timestamp="2026-06-29T00:00:00Z",
            result=[
                {"text": "Hallo", "speaker": "1"},
                {"text": " Welt", "speaker": "1"},
                {"text": "Antwort", "speaker": "2"},
            ],
        ),
        FrameDirection.DOWNSTREAM,
    )

    assert captured == [("Hallo plain fallback", True)]
    assert len(pushed) == 1


@pytest.mark.asyncio
async def test_transcription_callback_marks_final_without_external_callback():
    final_seen = asyncio.Event()
    pushed = []
    processor = TranscriptionCallbackProcessor(
        None,
        on_final_transcription=final_seen.set,
    )

    async def _capture_push(frame, direction):
        pushed.append((frame, direction))

    processor.push_frame = _capture_push

    await processor.process_frame(
        TranscriptionFrame(
            text="Hallo final",
            user_id="user",
            timestamp="2026-07-06T00:00:00Z",
            result=None,
        ),
        FrameDirection.DOWNSTREAM,
    )

    assert final_seen.is_set()
    assert len(pushed) == 1


@pytest.mark.asyncio
async def test_transcription_callback_formats_soniox_speaker_tokens_when_enabled():
    captured: list[tuple[str, bool]] = []
    pushed = []
    processor = TranscriptionCallbackProcessor(
        lambda text, is_final: captured.append((text, is_final)),
        enable_speaker_diarization=True,
    )

    async def _capture_push(frame, direction):
        pushed.append((frame, direction))

    processor.push_frame = _capture_push

    await processor.process_frame(
        TranscriptionFrame(
            text="Hallo plain fallback",
            user_id="user",
            timestamp="2026-06-29T00:00:00Z",
            result=[
                {"text": "Hallo", "speaker": "1"},
                {"text": " Welt", "speaker": "1"},
                {"text": "Antwort", "speaker": "2"},
            ],
        ),
        FrameDirection.DOWNSTREAM,
    )

    assert captured == [
        ("[Speaker 1]: Hallo Welt\n\n[Speaker 2]: Antwort", True)
    ]
    assert len(pushed) == 1


@pytest.mark.asyncio
async def test_cancel_silent_recording_marks_buffered_provider_skip():
    events: list[str] = []
    provider = _BufferedProvider()
    pipeline = ScriberPipeline(service_name="soniox_async", on_status_change=None)
    pipeline.pipeline = _DummyPipelineGraph([provider])
    pipeline.audio_input = _DummyAudioInput(events)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _DummyTask(pipeline._start_done, set_done_on_stop=False)
    pipeline.runner = _DummyRunner()

    await pipeline.cancel_silent_recording()

    assert provider._skip_terminal_transcription is True
    assert pipeline.task.cancel_called is True
    assert pipeline.runner.cancel_called is True
    assert events == ["audio_stop"]
