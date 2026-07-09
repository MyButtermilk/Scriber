import asyncio

import pytest
from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from websockets.protocol import State

from src.config import Config
from src.pipeline import (
    PipecatVadSpeechObserver,
    ScriberPipeline,
    SegmentedSTTRecordingGate,
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


class _DummyAudioInput:
    def __init__(self, events: list[str] | None = None) -> None:
        self.close_stream = None
        self._events = events

    async def stop(self, frame, *, close_stream=None):
        self.close_stream = close_stream
        if self._events is not None:
            self._events.append("audio_stop")


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


def test_assemblyai_realtime_factory_uses_new_pipecat_settings(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _AssemblyAISTTService:
        Settings = _Settings

        def __init__(self, *, api_key, settings, vad_force_turn_endpoint):
            self.api_key = api_key
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
    assert service.vad_force_turn_endpoint is True
    assert service.settings.kwargs == {
        "model": "universal-3-5-pro",
        "language_code": "de",
        "keyterms_prompt": ["Scriber", "Pipecat"],
        "speaker_labels": True,
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

        def __init__(self, *, api_key, settings, language=None, vad_force_turn_endpoint):
            self.api_key = api_key
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
        "speaker_labels": True,
    }
    assert service.language.value == "de"


def test_assemblyai_realtime_factory_supports_legacy_pipecat_connection_params(monkeypatch):
    class _AssemblyAIConnectionParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _AssemblyAISTTService:
        def __init__(self, *, api_key, connection_params, vad_force_turn_endpoint):
            self.api_key = api_key
            self.connection_params = connection_params
            self.vad_force_turn_endpoint = vad_force_turn_endpoint

    module = type(
        "AssemblyAIModule",
        (),
        {
            "AssemblyAISTTService": _AssemblyAISTTService,
            "AssemblyAIConnectionParams": _AssemblyAIConnectionParams,
        },
    )

    monkeypatch.setattr(Config, "ASSEMBLYAI_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "auto")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="assemblyai_realtime")._create_stt_service(object())

    assert service.api_key == "key"
    assert service.vad_force_turn_endpoint is True
    assert service.connection_params.kwargs == {
        "sample_rate": Config.SAMPLE_RATE,
        "keyterms_prompt": None,
        "speech_model": "universal-streaming-multilingual",
    }


def test_elevenlabs_factory_uses_realtime_service(monkeypatch):
    class _InputParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _ElevenLabsRealtimeSTTService:
        InputParams = _InputParams

        def __init__(self, *, api_key, model, sample_rate, params):
            self.api_key = api_key
            self.model = model
            self.sample_rate = sample_rate
            self.params = params

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
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="elevenlabs")._create_stt_service(object())

    assert service.api_key == "key"
    assert service.model == "scribe_v2_realtime"
    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.params.kwargs == {
        "language_code": "de",
        "commit_strategy": "manual",
    }


def test_deepgram_factory_disables_live_diarization_by_default(monkeypatch):
    class _LiveOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _DeepgramSTTService:
        def __init__(self, *, api_key, sample_rate=None, live_options=None):
            self.api_key = api_key
            self.sample_rate = sample_rate
            self.live_options = live_options

    module = type(
        "DeepgramModule",
        (),
        {"DeepgramSTTService": _DeepgramSTTService, "LiveOptions": _LiveOptions},
    )

    monkeypatch.setattr(Config, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="deepgram")._create_stt_service(object())

    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.live_options.kwargs == {
        "encoding": "linear16",
        "sample_rate": Config.SAMPLE_RATE,
        "channels": Config.CHANNELS,
        "model": "nova-3",
        "language": "de",
        "interim_results": True,
        "smart_format": True,
        "punctuate": True,
        "vad_events": False,
    }

    service_with_speakers = ScriberPipeline(
        service_name="deepgram",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert service_with_speakers.live_options.kwargs == {
        "encoding": "linear16",
        "sample_rate": Config.SAMPLE_RATE,
        "channels": Config.CHANNELS,
        "model": "nova-3",
        "language": "de",
        "interim_results": True,
        "smart_format": True,
        "punctuate": True,
        "vad_events": False,
        "diarize": True,
    }


def test_speechmatics_factory_disables_labeled_diarization_by_default(monkeypatch):
    class _InputParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _SpeechmaticsSTTService:
        InputParams = _InputParams

        def __init__(self, *, api_key, params=None):
            self.api_key = api_key
            self.params = params

    module = type(
        "SpeechmaticsModule",
        (),
        {"SpeechmaticsSTTService": _SpeechmaticsSTTService},
    )

    monkeypatch.setattr(Config, "SPEECHMATICS_API_KEY", "key")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="speechmatics")._create_stt_service(object())

    assert service.params.kwargs == {
        "enable_diarization": False,
        "language": "de",
        "speaker_active_format": "[Speaker {speaker_id}]: {text}",
        "speaker_passive_format": "[Speaker {speaker_id}]: {text}",
    }

    service_with_speakers = ScriberPipeline(
        service_name="speechmatics",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert service_with_speakers.params.kwargs == {
        "enable_diarization": True,
        "language": "de",
        "speaker_active_format": "[Speaker {speaker_id}]: {text}",
        "speaker_passive_format": "[Speaker {speaker_id}]: {text}",
    }


def test_deepgram_factory_enables_batch_diarization(monkeypatch):
    class _LiveOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _DeepgramSTTService:
        def __init__(self, *, api_key, sample_rate=None, live_options=None):
            self.api_key = api_key
            self.sample_rate = sample_rate
            self.live_options = live_options

    module = type(
        "DeepgramModule",
        (),
        {"DeepgramSTTService": _DeepgramSTTService, "LiveOptions": _LiveOptions},
    )

    monkeypatch.setattr(Config, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(
        service_name="deepgram",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.live_options.kwargs == {
        "encoding": "linear16",
        "sample_rate": Config.SAMPLE_RATE,
        "channels": Config.CHANNELS,
        "model": "nova-3",
        "language": "de",
        "interim_results": True,
        "smart_format": True,
        "punctuate": True,
        "vad_events": False,
        "diarize": True,
    }


def test_gladia_factory_uses_current_pipecat_signature(monkeypatch):
    class _GladiaSTTService:
        def __init__(self, *, api_key, sample_rate=None, model="solaria-1"):
            self.api_key = api_key
            self.sample_rate = sample_rate
            self.model = model

    module = type(
        "GladiaModule",
        (),
        {"GladiaSTTService": _GladiaSTTService},
    )

    monkeypatch.setattr(Config, "GLADIA_API_KEY", "key")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="gladia")._create_stt_service(object())

    assert service.api_key == "key"
    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.model == "solaria-1"


def test_speechmatics_factory_enables_batch_labeled_diarization(monkeypatch):
    class _InputParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _SpeechmaticsSTTService:
        InputParams = _InputParams

        def __init__(self, *, api_key, params=None):
            self.api_key = api_key
            self.params = params

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

    assert service.params.kwargs == {
        "enable_diarization": True,
        "language": "de",
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
        (UserStoppedSpeakingFrame, FrameDirection.DOWNSTREAM)
    ]


@pytest.mark.asyncio
async def test_segmented_stt_gate_defaults_to_whole_recording_segment():
    gate = SegmentedSTTRecordingGate(vad_segmentation_enabled=False)
    pushed = []

    async def _capture_push(frame, direction):
        pushed.append((type(frame), direction))

    gate.push_frame = _capture_push

    await gate.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    assert pushed == [
        (UserStartedSpeakingFrame, FrameDirection.DOWNSTREAM),
        (UserStoppedSpeakingFrame, FrameDirection.DOWNSTREAM),
        (EndFrame, FrameDirection.DOWNSTREAM),
    ]


@pytest.mark.asyncio
async def test_segmented_stt_gate_can_pass_vad_segments_when_enabled():
    gate = SegmentedSTTRecordingGate(vad_segmentation_enabled=True)
    pushed = []

    async def _capture_push(frame, direction):
        pushed.append((type(frame), direction))

    gate.push_frame = _capture_push

    await gate.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    flushed = await gate.flush_segment(direction=FrameDirection.DOWNSTREAM)
    flushed_again = await gate.flush_segment(direction=FrameDirection.DOWNSTREAM)

    assert flushed is True
    assert flushed_again is False
    assert pushed == [
        (UserStartedSpeakingFrame, FrameDirection.DOWNSTREAM),
        (UserStoppedSpeakingFrame, FrameDirection.DOWNSTREAM),
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
