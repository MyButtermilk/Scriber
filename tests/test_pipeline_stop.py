import asyncio
import gc
import io
import inspect
import threading
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.transcriptions.language import Language
from websockets.protocol import State

from src.config import Config
from src.microphone import MicrophoneInput
import src.pipeline as pipeline_module
from src.pipeline import (
    LIVE_STT_STOP_END_FRAME_FINALIZES,
    LIVE_STT_STOP_PROVIDER_MANUAL,
    LIVE_STT_STOP_VAD_FLUSH_BEFORE_END,
    _AnalyzerCache,
    _create_soniox_smart_turn_processor,
    PipecatVadSpeechObserver,
    ScriberPipeline,
    SegmentedSTTRecordingGate,
    SonioxAsyncProcessor,
    TranscriptionCallbackProcessor,
    _format_speaker_transcript_tokens,
    _live_analyzer_diagnostics,
    _live_analyzer_requirements,
    _live_service_uses_native_streaming,
    _live_recording_gate_needed,
    _live_stt_stop_strategy,
    _ordered_live_pipeline_steps,
    direct_file_workflow_timeout_seconds,
)


def test_microphone_transport_has_no_silently_ignored_analyzer_arguments():
    parameters = inspect.signature(MicrophoneInput.__init__).parameters

    assert "vad_analyzer" not in parameters
    assert "turn_analyzer" not in parameters


def test_live_analyzer_requirements_respect_disabled_vad_setting(monkeypatch):
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", False)
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")

    assert _live_analyzer_requirements("azure_mai") == (False, False)
    assert _live_analyzer_requirements("deepgram", segmented_service=True) == (
        False,
        False,
    )
    assert _live_analyzer_requirements("soniox") == (False, False)


def test_live_analyzer_requirements_enable_only_requested_paths(monkeypatch):
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", True)
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")

    assert _live_analyzer_requirements("azure_mai") == (True, False)
    assert _live_analyzer_requirements("mistral") == (True, False)
    assert _live_analyzer_requirements("groq") == (True, False)
    assert _live_analyzer_requirements("soniox") == (False, False)
    assert _live_analyzer_requirements("modulate") == (False, False)
    assert _live_analyzer_requirements("elevenlabs") == (False, False)


def test_live_analyzer_requirements_treat_soniox_async_as_non_streaming(monkeypatch):
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", True)
    monkeypatch.setattr(Config, "SONIOX_MODE", "async")

    assert _live_service_uses_native_streaming("soniox") is False
    assert _live_analyzer_requirements("soniox") == (True, False)


@pytest.mark.parametrize(
    "service_name",
    [
        "soniox",
        "smallest",
        "assemblyai_realtime",
        "google",
        "deepgram",
        "openai",
        "gladia",
        "speechmatics",
        "modulate",
        "elevenlabs",
    ],
)
def test_native_realtime_services_never_attach_silero(monkeypatch, service_name):
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", True)
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")

    assert _live_service_uses_native_streaming(service_name) is True
    assert _live_analyzer_requirements(service_name) == (False, False)


def test_live_analyzer_diagnostics_distinguish_disabled_silero_from_gate(monkeypatch):
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", False)
    monkeypatch.setattr(pipeline_module, "HAS_SILERO_VAD", True)
    monkeypatch.setattr(pipeline_module, "SileroVADAnalyzer", object)
    gate = SegmentedSTTRecordingGate(vad_segmentation_enabled=False)

    diagnostics = _live_analyzer_diagnostics(
        vad_processor=None,
        segmented_gate=gate,
        segmented_provider=False,
        native_realtime_provider=False,
        stop_strategy=LIVE_STT_STOP_END_FRAME_FINALIZES,
        smart_turn_processor=None,
    )

    assert diagnostics["sileroVadSettingEnabled"] is False
    assert diagnostics["sileroVadAvailable"] is True
    assert diagnostics["sileroVadAttached"] is False
    assert diagnostics["sileroVadEffectiveEnabled"] is False
    assert diagnostics["sileroSuppressedForNativeRealtime"] is False
    assert diagnostics["nativeRealtimeProvider"] is False
    assert diagnostics["recordingGateAttached"] is True
    assert diagnostics["syntheticRecordingBoundary"] is True


def test_live_recording_gate_preserves_one_turn_without_vad(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")

    assert _live_recording_gate_needed(
        "azure_mai",
        segmented_service=False,
        vad_attached=False,
    ) is True
    assert _live_recording_gate_needed(
        "deepgram",
        segmented_service=False,
        vad_attached=True,
    ) is False


@pytest.mark.parametrize(
    ("service_name", "expected"),
    [
        ("openai", LIVE_STT_STOP_VAD_FLUSH_BEFORE_END),
        ("deepgram", LIVE_STT_STOP_VAD_FLUSH_BEFORE_END),
        ("elevenlabs", LIVE_STT_STOP_VAD_FLUSH_BEFORE_END),
        ("azure_mai", LIVE_STT_STOP_END_FRAME_FINALIZES),
        ("gladia", LIVE_STT_STOP_END_FRAME_FINALIZES),
        ("openai_async", LIVE_STT_STOP_END_FRAME_FINALIZES),
        ("soniox_async", LIVE_STT_STOP_END_FRAME_FINALIZES),
    ],
)
def test_live_stt_stop_strategy_records_provider_capability(service_name, expected):
    assert _live_stt_stop_strategy(service_name) == expected


def test_live_stt_stop_strategy_keeps_provider_manual_soniox_path(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")

    assert _live_stt_stop_strategy("soniox") == LIVE_STT_STOP_PROVIDER_MANUAL
    assert (
        _live_stt_stop_strategy("azure_mai", segmented_service=True)
        == LIVE_STT_STOP_VAD_FLUSH_BEFORE_END
    )
    assert _live_recording_gate_needed(
        "soniox",
        segmented_service=False,
        vad_attached=False,
    ) is False


def test_analyzer_warmup_instances_are_claimed_once_per_session(monkeypatch):
    created_vad: list[object] = []
    created_smart_turn: list[object] = []

    def create_vad():
        analyzer = object()
        created_vad.append(analyzer)
        return analyzer

    def create_smart_turn():
        analyzer = object()
        created_smart_turn.append(analyzer)
        return analyzer

    _AnalyzerCache.clear_cache()
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", True)
    monkeypatch.setattr(pipeline_module, "HAS_SILERO_VAD", True)
    monkeypatch.setattr(pipeline_module, "HAS_SMART_TURN", True)
    monkeypatch.setattr(pipeline_module, "SileroVADAnalyzer", create_vad)
    monkeypatch.setattr(pipeline_module, "LocalSmartTurnAnalyzerV3", create_smart_turn)

    try:
        _AnalyzerCache.prewarm()
        first_vad = _AnalyzerCache.acquire_vad_analyzer()
        first_smart_turn = _AnalyzerCache.acquire_smart_turn_analyzer()
        second_vad = _AnalyzerCache.acquire_vad_analyzer()
        second_smart_turn = _AnalyzerCache.acquire_smart_turn_analyzer()
    finally:
        _AnalyzerCache.clear_cache()

    assert first_vad is created_vad[0]
    assert second_vad is created_vad[1]
    assert first_vad is not second_vad
    assert first_smart_turn is created_smart_turn[0]
    assert second_smart_turn is created_smart_turn[1]
    assert first_smart_turn is not second_smart_turn


def test_analyzer_warmup_refills_with_new_unclaimed_instances(monkeypatch):
    created_vad: list[object] = []
    created_smart_turn: list[object] = []

    def create_vad():
        analyzer = object()
        created_vad.append(analyzer)
        return analyzer

    def create_smart_turn():
        analyzer = object()
        created_smart_turn.append(analyzer)
        return analyzer

    class _ImmediateThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            self.target()

    _AnalyzerCache.clear_cache()
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", True)
    monkeypatch.setattr(pipeline_module, "HAS_SILERO_VAD", True)
    monkeypatch.setattr(pipeline_module, "HAS_SMART_TURN", True)
    monkeypatch.setattr(pipeline_module, "SileroVADAnalyzer", create_vad)
    monkeypatch.setattr(pipeline_module, "LocalSmartTurnAnalyzerV3", create_smart_turn)
    monkeypatch.setattr(pipeline_module.threading, "Thread", _ImmediateThread)

    try:
        _AnalyzerCache.prewarm()
        used_vad = _AnalyzerCache.acquire_vad_analyzer()
        used_smart_turn = _AnalyzerCache.acquire_smart_turn_analyzer()

        assert _AnalyzerCache.request_background_replenish() is True

        next_vad = _AnalyzerCache.acquire_vad_analyzer()
        next_smart_turn = _AnalyzerCache.acquire_smart_turn_analyzer()
    finally:
        _AnalyzerCache.clear_cache()

    assert next_vad is created_vad[1]
    assert next_smart_turn is created_smart_turn[1]
    assert next_vad is not used_vad
    assert next_smart_turn is not used_smart_turn


def test_vad_discard_invalidates_analyzer_constructing_outside_cache_lock(monkeypatch):
    construction_started = threading.Event()
    allow_construction = threading.Event()
    cleaned: list[object] = []
    failures: list[BaseException] = []

    class _BlockingVadAnalyzer:
        def __init__(self):
            construction_started.set()
            if not allow_construction.wait(timeout=2):
                raise TimeoutError("test did not release VAD construction")

        def cleanup(self):
            cleaned.append(self)

    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", True)
    monkeypatch.setattr(pipeline_module, "HAS_SILERO_VAD", True)
    monkeypatch.setattr(pipeline_module, "SileroVADAnalyzer", _BlockingVadAnalyzer)
    _AnalyzerCache.clear_cache()

    def warm_vad() -> None:
        try:
            _AnalyzerCache.prewarm(include_vad=True, include_smart_turn=False)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    worker = threading.Thread(target=warm_vad)
    worker.start()
    try:
        assert construction_started.wait(timeout=2)
        monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", False)
        _AnalyzerCache.discard_vad_cache()
        allow_construction.set()
        worker.join(timeout=2)

        assert not worker.is_alive()
        assert failures == []
        assert _AnalyzerCache._vad_analyzer is None
        assert len(cleaned) == 1
    finally:
        allow_construction.set()
        worker.join(timeout=2)
        _AnalyzerCache.clear_cache()


def test_soniox_smart_turn_uses_explicit_pipecat_1_5_strategies(monkeypatch):
    analyzer = object()
    monkeypatch.setattr(
        _AnalyzerCache,
        "acquire_smart_turn_analyzer",
        classmethod(lambda _cls: analyzer),
    )

    processor = _create_soniox_smart_turn_processor()

    assert processor is not None
    strategies = processor._user_turn_controller._user_turn_strategies
    assert len(strategies.start) == 1
    assert isinstance(strategies.start[0], VADUserTurnStartStrategy)
    assert len(strategies.stop) == 1
    assert isinstance(strategies.stop[0], TurnAnalyzerUserTurnStopStrategy)
    assert strategies.stop[0]._turn_analyzer is analyzer
    assert strategies.stop[0].wait_for_transcript is True


def test_live_processor_order_keeps_segment_gate_before_http_stt_and_smart_turn_after_soniox():
    audio_input = object()
    vad_processor = object()
    vad_observer = object()
    segmented_gate = object()
    stt_service = object()
    smart_turn_processor = object()
    error_handler = object()
    transcript_callback = object()
    text_injector = object()

    segmented_steps = _ordered_live_pipeline_steps(
        audio_input=audio_input,
        vad_processor=vad_processor,
        vad_observer=vad_observer,
        segmented_gate=segmented_gate,
        stt_service=stt_service,
        smart_turn_processor=None,
        error_handler=error_handler,
        transcript_callback=transcript_callback,
        text_injector=text_injector,
    )
    soniox_steps = _ordered_live_pipeline_steps(
        audio_input=audio_input,
        vad_processor=vad_processor,
        vad_observer=vad_observer,
        segmented_gate=None,
        stt_service=stt_service,
        smart_turn_processor=smart_turn_processor,
        error_handler=error_handler,
        transcript_callback=transcript_callback,
        text_injector=text_injector,
    )

    assert segmented_steps == [
        audio_input,
        vad_processor,
        vad_observer,
        segmented_gate,
        stt_service,
        error_handler,
        transcript_callback,
        text_injector,
    ]
    assert soniox_steps == [
        audio_input,
        vad_processor,
        vad_observer,
        stt_service,
        smart_turn_processor,
        error_handler,
        transcript_callback,
        text_injector,
    ]


def test_soniox_token_formatter_preserves_speaker_zero_and_numbers_by_first_appearance():
    assert _format_speaker_transcript_tokens([
        {"speaker": 0, "text": " First"},
        {"speaker": 0, "text": " turn."},
        {"speaker": 4, "text": " Reply."},
    ]) == "[Speaker 1]: First turn.\n\n[Speaker 2]: Reply."


def test_direct_file_timeout_budgets_scale_to_five_hours_and_remain_bounded():
    default = ScriberPipeline(service_name="soniox")
    assert default._direct_file_upload_timeout_seconds() == 300.0
    assert default._direct_file_batch_timeout_seconds() == 900.0
    assert default._direct_file_poll_timeout_seconds() == 600.0

    five_hours = ScriberPipeline(
        service_name="soniox",
        direct_file_expected_duration_seconds=5 * 60 * 60,
    )
    assert five_hours._direct_file_upload_timeout_seconds() == 1_620.0
    assert five_hours._direct_file_batch_timeout_seconds() == 9_300.0
    assert five_hours._direct_file_poll_timeout_seconds() == 9_300.0

    extreme = ScriberPipeline(
        service_name="soniox",
        direct_file_expected_duration_seconds=100 * 60 * 60,
    )
    assert extreme._direct_file_upload_timeout_seconds() == 3_600.0
    assert extreme._direct_file_batch_timeout_seconds() == 14_400.0
    assert extreme._direct_file_poll_timeout_seconds() == 14_400.0


def test_outer_direct_file_timeout_scales_for_cloud_and_local_long_media():
    assert direct_file_workflow_timeout_seconds(None) == 600.0
    assert direct_file_workflow_timeout_seconds(60.0) == 600.0
    assert direct_file_workflow_timeout_seconds(5 * 60 * 60) == 20_100.0
    assert direct_file_workflow_timeout_seconds(100 * 60 * 60) == 21_600.0

    local = ScriberPipeline(
        service_name="onnx_local",
        direct_file_expected_duration_seconds=5 * 60 * 60,
    )
    assert local._direct_file_workflow_timeout_seconds() == 20_100.0


@pytest.mark.asyncio
async def test_deepgram_direct_uses_frozen_model_after_config_changes(
    monkeypatch, tmp_path
):
    source = tmp_path / "deepgram.wav"
    source.write_bytes(b"audio")
    captured = {}

    async def fake_transcribe(**kwargs):
        captured.update(kwargs)
        return {
            "results": {
                "channels": [{"alternatives": [{"transcript": "done", "words": []}]}]
            }
        }

    monkeypatch.setattr(
        "src.pipeline.transcribe_with_deepgram_pre_recorded", fake_transcribe
    )
    monkeypatch.setattr(Config, "DEEPGRAM_API_KEY", "key")
    pipeline = ScriberPipeline(
        service_name="deepgram_async",
        execution_route={
            "model": "frozen-deepgram-model",
            "language": "de-DE",
            "custom_vocab": "Frozen Deepgram term",
        },
    )
    monkeypatch.setattr(Config, "DEEPGRAM_MODEL", "changed-deepgram-model")
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed term")

    await pipeline.transcribe_file_direct(str(source))

    assert captured["model"] == "frozen-deepgram-model"
    assert captured["language"] == "de-DE"
    assert captured["custom_vocab"] == "Frozen Deepgram term"


@pytest.mark.asyncio
async def test_gemini_direct_uses_frozen_model_after_config_changes(
    monkeypatch, tmp_path
):
    source = tmp_path / "gemini.wav"
    source.write_bytes(b"audio")
    captured = {}

    async def fake_transcribe(**kwargs):
        captured.update(kwargs)
        return {"candidates": [{"content": {"parts": [{"text": "done"}]}}]}

    monkeypatch.setattr("src.pipeline.transcribe_with_gemini_audio", fake_transcribe)
    monkeypatch.setattr(Config, "GOOGLE_API_KEY", "key")
    pipeline = ScriberPipeline(
        service_name="gemini_stt",
        execution_route={
            "model": "frozen-gemini-model",
            "language": "de-DE",
            "custom_vocab": "Frozen Gemini term",
        },
    )
    monkeypatch.setattr(Config, "GEMINI_STT_MODEL", "changed-gemini-model")
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed term")

    await pipeline.transcribe_file_direct(str(source))

    assert captured["model"] == "frozen-gemini-model"
    assert captured["language"] == "de-DE"
    assert captured["custom_vocab"] == "Frozen Gemini term"


@pytest.mark.asyncio
async def test_azure_mai_direct_uses_frozen_model_and_vocab_after_config_changes(
    monkeypatch, tmp_path
):
    source = tmp_path / "azure.mp3"
    source.write_bytes(b"audio")
    captured = {}

    async def fake_transcribe(**kwargs):
        captured.update(kwargs)
        return {"combinedPhrases": [{"text": "done"}]}

    monkeypatch.setattr("src.pipeline.transcribe_with_azure_mai", fake_transcribe)
    monkeypatch.setattr(Config, "AZURE_MAI_SPEECH_KEY", "key")
    monkeypatch.setattr(Config, "AZURE_MAI_REGION", "northeurope")
    pipeline = ScriberPipeline(
        service_name="azure_mai",
        execution_route={
            "model": "frozen-azure-model",
            "language": "de-DE",
            "custom_vocab": "Frozen Azure term",
        },
    )
    monkeypatch.setattr(Config, "AZURE_MAI_MODEL", "changed-azure-model")
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed term")

    await pipeline.transcribe_file_direct(str(source))

    assert captured["model"] == "frozen-azure-model"
    assert captured["language"] == "de-DE"
    assert captured["custom_vocab"] == "Frozen Azure term"


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


def test_soniox_async_processor_disposal_closes_audio_spool():
    processor = SonioxAsyncProcessor(api_key="test-key", session=object())
    audio_spool = processor._buffer
    processor_ref = weakref.ref(processor)

    del processor
    gc.collect()

    assert processor_ref() is None
    assert audio_spool.closed


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
async def test_soniox_async_upload_uses_selected_eu_region():
    session = _RejectedSonioxUploadSession()
    processor = SonioxAsyncProcessor(
        api_key="test-key",
        session=session,
        base_url="https://api.eu.soniox.com/v1",
    )

    async def encode_webm(_audio_bytes, *, prefer_webm=True):
        assert prefer_webm is True
        return b"webm-audio", "audio/webm", "audio.webm"

    processor._encode_audio = encode_webm

    with pytest.raises(RuntimeError, match="upload rejected"):
        await processor._transcribe_async(b"\0\0" * 160)

    assert session.post_calls[0][0] == "https://api.eu.soniox.com/v1/files"


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
async def test_soniox_async_cancellation_cleans_remote_resources(monkeypatch):
    poll_started = asyncio.Event()

    class _ResponseContext:
        def __init__(self, status):
            self.response = SimpleNamespace(status=status, raise_for_status=lambda: None)

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *_args):
            return False

    class _BlockingResponseContext:
        async def __aenter__(self):
            poll_started.set()
            await asyncio.Event().wait()

        async def __aexit__(self, *_args):
            return False

    class _Session:
        def __init__(self):
            self.post_count = 0

        def post(self, *_args, **_kwargs):
            self.post_count += 1
            return _ResponseContext(201 if self.post_count == 1 else 200)

        def get(self, *_args, **_kwargs):
            return _BlockingResponseContext()

    processor = SonioxAsyncProcessor(api_key="test-key", session=_Session())
    processor._sample_rate = 16000
    processor._channels = 1
    processor._encode_audio = AsyncMock(return_value=(b"webm", "audio/webm", "audio.webm"))
    processor._cleanup_soniox_resources = AsyncMock()
    response_payloads = iter(({"id": "file-id"}, {"id": "transcription-id"}))

    async def _read_json(*_args, **_kwargs):
        return next(response_payloads)

    monkeypatch.setattr("src.pipeline.read_response_json_limited", _read_json)

    task = asyncio.create_task(processor._transcribe_async(b"\0\0" * 320))
    await asyncio.wait_for(poll_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    processor._cleanup_soniox_resources.assert_awaited_once_with(
        "file-id",
        "transcription-id",
        {"Authorization": "Bearer test-key"},
    )


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
        self.external_handoff_prepare_calls = 0
        self.external_handoff_confirm_calls = 0
        self.external_handoff_cancel_calls = 0

    def prepare_external_capture_handoff(self) -> bool:
        self.external_handoff_prepare_calls += 1
        self.events.append("external_handoff_prepare")
        return True

    def confirm_external_capture_handoff(self) -> None:
        self.external_handoff_confirm_calls += 1
        self.events.append("external_handoff_confirm")

    def cancel_external_capture_handoff(self) -> None:
        self.external_handoff_cancel_calls += 1
        self.events.append("external_handoff_cancel")

    async def stop_capture_for_finalization(self, *, close_stream=None):
        self.events.append("capture_stop")

    async def stop(self, frame, *, close_stream=None):
        self.events.append("audio_cleanup_stop")


class _DummySegmentedSTTService(SegmentedSTTService):
    async def run_stt(self, audio):
        if False:
            yield None


class _RecordingStopTask(_DummyTask):
    def __init__(self, done_event: asyncio.Event, events: list[str]):
        super().__init__(done_event, set_done_on_stop=True)
        self.events = events

    async def stop_when_done(self):
        self.events.append("task_stop_when_done")
        await super().stop_when_done()


class _FastFailingStopTask(_DummyTask):
    async def stop_when_done(self):
        self.stop_when_done_called = True
        self._finished = True
        self._done_event.set()
        raise RuntimeError("synthetic stop failure")


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


def test_onnx_file_factory_uses_bounded_flushing_service(monkeypatch):
    monkeypatch.setattr("src.onnx_local_service.is_onnx_available", lambda: True)
    monkeypatch.setattr(Config, "ONNX_MODEL", "parakeet-primeline")
    monkeypatch.setattr(Config, "ONNX_QUANTIZATION", "fp32")
    monkeypatch.setattr(Config, "LANGUAGE", "de")

    service = ScriberPipeline(service_name="onnx_local")._create_stt_service(
        object(),
        for_file=True,
    )

    assert type(service).__name__ == "OnnxLocalBufferedSTTService"
    assert service._quantization == "fp32"
    assert service._max_buffer_secs == 30
    assert service._flush_on_limit is True


def test_onnx_file_factory_uses_frozen_model_and_language(monkeypatch):
    monkeypatch.setattr("src.onnx_local_service.is_onnx_available", lambda: True)
    pipeline = ScriberPipeline(
        service_name="onnx_local",
        execution_route={
            "model": "frozen-onnx-model",
            "language": "de-DE",
        },
    )
    monkeypatch.setattr(Config, "ONNX_MODEL", "changed-after-route-freeze")
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")

    service = pipeline._create_stt_service(object(), for_file=True)

    assert service._model_name == "frozen-onnx-model"
    assert service._language == "de-DE"


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
@pytest.mark.asyncio
async def test_pipecat_1_5_live_factories_match_runtime_signatures(
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
    try:
        assert type(service).__name__ == expected_class_name
        if service_name == "soniox":
            # SmartTurn sits after Soniox and relies on Pipecat's default audio
            # passthrough to receive the same InputAudioRawFrames.
            assert service._audio_passthrough is True
            # Soniox Realtime uses native semantic endpoint detection. Local VAD
            # may support diagnostics, but must not force transcript commits.
            assert service._vad_force_turn_endpoint is False
    finally:
        cleanup = getattr(service, "cleanup", None)
        if callable(cleanup):
            await cleanup()


@pytest.mark.asyncio
async def test_soniox_realtime_factory_uses_selected_eu_region(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_API_KEY", "key")
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")
    monkeypatch.setattr(Config, "SONIOX_REGION", "eu")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "")

    service = ScriberPipeline(service_name="soniox")._create_stt_service(object())
    try:
        assert service._url == "wss://stt-rt.eu.soniox.com/transcribe-websocket"
    finally:
        await service.cleanup()


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


@pytest.mark.parametrize(
    ("service_name", "expected_timeout"),
    (("modulate", 40.0), ("smallest", 30.0)),
)
@pytest.mark.asyncio
async def test_default_realtime_stop_budget_gives_only_modulate_finalize_headroom(
    monkeypatch,
    service_name,
    expected_timeout,
):
    pipeline = ScriberPipeline(service_name=service_name, on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _DummyTask(pipeline._start_done, set_done_on_stop=True)
    observed_timeouts: list[float] = []
    real_wait_for = asyncio.wait_for

    async def capture_wait_timeout(awaitable, *, timeout):
        observed_timeouts.append(float(timeout))
        return await real_wait_for(awaitable, timeout=1.0)

    monkeypatch.setattr(pipeline_module.asyncio, "wait_for", capture_wait_timeout)

    await pipeline.stop()

    assert observed_timeouts == [expected_timeout]
    assert pipeline.task.cancel_called is False


@pytest.mark.asyncio
async def test_stop_consumes_fast_stop_task_failure():
    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _FastFailingStopTask(pipeline._start_done, set_done_on_stop=True)

    await pipeline.stop(timeout_secs=0.5)

    assert pipeline.task.stop_when_done_called is True


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
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    events: list[str] = []
    prewarm = _DummyPrewarmManager(events)
    pipeline = ScriberPipeline(
        service_name="openai",
        on_status_change=None,
        mic_prewarm_manager=prewarm,
    )
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _RecordingStopTask(pipeline._start_done, events)
    audio_input = _SegmentedFinalizationAudioInput(events)
    pipeline.audio_input = audio_input

    class _FinalizingGate(SegmentedSTTRecordingGate):
        async def flush_segment(self, *, direction=FrameDirection.DOWNSTREAM) -> bool:
            events.append("segment_flush")
            await asyncio.sleep(0.03)
            events.append("provider_final")
            pipeline._mark_final_transcription_received()
            return True

    pipeline.pipeline = _DummyRuntimePipelineGraph(
        [
            _FinalizingGate(vad_segmentation_enabled=False),
            _DummySegmentedSTTService(sample_rate=16000),
        ]
    )

    await pipeline.stop(timeout_secs=1.0)

    assert events.index("prewarm_detach") < events.index("prewarm_resume")
    assert events.index("prewarm_resume") < events.index("capture_stop")
    assert events.index("capture_stop") < events.index("segment_flush")
    assert events.index("segment_flush") < events.index("provider_final")
    assert events.index("provider_final") < events.index("audio_cleanup_stop")
    assert events.index("provider_final") < events.index("task_stop_when_done")
    assert prewarm.detach_calls == 1
    assert prewarm.resume_calls == 1
    assert audio_input.external_handoff_prepare_calls == 1
    assert audio_input.external_handoff_confirm_calls == 1
    assert audio_input.external_handoff_cancel_calls == 0


def test_vad_disabled_terminal_provider_gate_does_not_enable_segment_wait(monkeypatch):
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", False)
    needs_vad, _uses_smart_turn = _live_analyzer_requirements("azure_mai")

    assert needs_vad is False
    assert _live_recording_gate_needed(
        "azure_mai",
        segmented_service=False,
        vad_attached=needs_vad,
    ) is True

    pipeline = ScriberPipeline(service_name="azure_mai", on_status_change=None)
    pipeline.pipeline = _DummyRuntimePipelineGraph(
        [SegmentedSTTRecordingGate(vad_segmentation_enabled=False)]
    )

    assert pipeline._requires_pre_endframe_stt_finalization() is False


@pytest.mark.parametrize("service_name", ["openai", "deepgram", "elevenlabs"])
def test_gate_committing_realtime_provider_keeps_pre_endframe_wait(service_name):
    pipeline = ScriberPipeline(service_name=service_name, on_status_change=None)
    pipeline.pipeline = _DummyRuntimePipelineGraph(
        [
            SegmentedSTTRecordingGate(
                vad_segmentation_enabled=False,
                stop_strategy=_live_stt_stop_strategy(service_name),
            )
        ]
    )

    assert pipeline._requires_pre_endframe_stt_finalization() is True


@pytest.mark.asyncio
async def test_gate_committing_realtime_provider_flushes_before_endframe(monkeypatch):
    monkeypatch.setenv("SCRIBER_LIVE_STT_FINAL_FAILURE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False, raising=False)
    events: list[str] = []
    pipeline = ScriberPipeline(service_name="openai", on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _RecordingStopTask(pipeline._start_done, events)
    pipeline.audio_input = _SegmentedFinalizationAudioInput(events)

    class _GateOnlyRealtimeFinalizer(SegmentedSTTRecordingGate):
        async def flush_segment(self, *, direction=FrameDirection.DOWNSTREAM) -> bool:
            events.append("gate_commit")

            async def _mark_final():
                await asyncio.sleep(0.01)
                events.append("provider_final")
                pipeline._mark_final_transcription_received()

            asyncio.create_task(_mark_final())
            return True

    pipeline.pipeline = _DummyRuntimePipelineGraph(
        [
            _GateOnlyRealtimeFinalizer(
                vad_segmentation_enabled=False,
                stop_strategy=LIVE_STT_STOP_VAD_FLUSH_BEFORE_END,
            )
        ]
    )
    pipeline._mark_final_transcription_received()

    await pipeline.stop(timeout_secs=1.0)

    assert events.index("capture_stop") < events.index("gate_commit")
    assert events.index("gate_commit") < events.index("provider_final")
    assert events.index("provider_final") < events.index("task_stop_when_done")


@pytest.mark.asyncio
async def test_async_vad_commit_timeout_is_a_provider_failure(monkeypatch):
    monkeypatch.setenv("SCRIBER_LIVE_STT_FINAL_FAILURE_TIMEOUT_SECONDS", "1")
    pipeline = ScriberPipeline(service_name="openai", on_status_change=None)
    pipeline.pipeline = _DummyRuntimePipelineGraph([])
    pipeline._wait_for_new_final_transcription_or_done = AsyncMock(
        return_value="timeout"
    )

    result = await pipeline._await_async_vad_commit_final(
        after_generation=0,
        final_response_pending=True,
    )

    assert result == "timeout"
    assert pipeline._terminal_error is not None
    assert "did not return its committed final result" in pipeline._terminal_error


@pytest.mark.asyncio
async def test_terminal_buffered_provider_skips_segmented_final_wait():
    events: list[str] = []
    pipeline = ScriberPipeline(service_name="azure_mai", on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _RecordingStopTask(pipeline._start_done, events)
    pipeline.audio_input = _DummyAudioInput(events)
    pipeline.pipeline = _DummyRuntimePipelineGraph(
        [SegmentedSTTRecordingGate(vad_segmentation_enabled=False)]
    )
    pipeline._wait_for_new_final_transcription_or_done = AsyncMock(
        side_effect=AssertionError("terminal-buffered providers must not enter segmented wait")
    )

    await pipeline.stop(timeout_secs=1.0)

    pipeline._wait_for_new_final_transcription_or_done.assert_not_awaited()
    assert events.index("audio_stop") < events.index("task_stop_when_done")


@pytest.mark.asyncio
async def test_segmented_stop_uses_normal_cleanup_when_prewarm_handoff_fails(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    events: list[str] = []
    prewarm = _DummyPrewarmManager(events, resume_result=False)
    pipeline = ScriberPipeline(
        service_name="openai",
        on_status_change=None,
        mic_prewarm_manager=prewarm,
    )
    audio_input = _SegmentedFinalizationAudioInput(events)
    pipeline.audio_input = audio_input

    await pipeline._stop_audio_capture_for_segmented_finalization()

    assert "capture_stop" not in events
    assert events.index("prewarm_detach") < events.index("audio_cleanup_stop")
    assert prewarm.detach_calls == 1
    assert prewarm.resume_calls == 3
    assert audio_input.external_handoff_prepare_calls == 2
    assert audio_input.external_handoff_confirm_calls == 0
    assert audio_input.external_handoff_cancel_calls == 2
    assert pipeline.audio_input is None


@pytest.mark.asyncio
async def test_segmented_stop_serializes_prewarm_handoff_with_cleanup(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    events: list[str] = []

    class _BlockingPrewarmManager(_DummyPrewarmManager):
        def __init__(self):
            super().__init__(events)
            self.resume_started = threading.Event()
            self.release_resume = threading.Event()

        def resume_after_active_capture(self) -> bool:
            self.resume_calls += 1
            events.append("prewarm_resume")
            self.resume_started.set()
            assert self.release_resume.wait(timeout=1.0)
            return True

    prewarm = _BlockingPrewarmManager()
    audio_input = _SegmentedFinalizationAudioInput(events)
    pipeline = ScriberPipeline(
        service_name="openai",
        on_status_change=None,
        mic_prewarm_manager=prewarm,
    )
    pipeline.audio_input = audio_input

    segmented_stop = asyncio.create_task(
        pipeline._stop_audio_capture_for_segmented_finalization()
    )
    assert await asyncio.to_thread(prewarm.resume_started.wait, 1.0)
    full_cleanup = asyncio.create_task(pipeline._cleanup_audio_input())
    await asyncio.sleep(0.02)
    assert full_cleanup.done() is False
    prewarm.release_resume.set()
    await asyncio.gather(segmented_stop, full_cleanup)

    assert prewarm.detach_calls == 1
    assert prewarm.resume_calls == 1
    assert audio_input.external_handoff_prepare_calls == 1
    assert audio_input.external_handoff_confirm_calls == 1
    assert events.index("external_handoff_confirm") < events.index("capture_stop")
    assert events.index("capture_stop") < events.index("audio_cleanup_stop")


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
    pipeline._mark_final_transcription_received()
    previous_final_generation = pipeline._final_transcription_generation

    async def _mark_final_after_stop_signal():
        await asyncio.sleep(0.2)
        pipeline._mark_final_transcription_received()

    marker_task = asyncio.create_task(_mark_final_after_stop_signal())

    await asyncio.wait_for(pipeline.stop(timeout_secs=1.0), timeout=1.0)

    assert soniox._websocket.sent == ['{"type": "finalize"}', ""]
    assert pipeline._final_transcription_generation > previous_final_generation
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
async def test_pipecat_vad_observer_ignores_upstream_turn_broadcast_duplicates():
    observer = PipecatVadSpeechObserver(enabled=True)
    observer.push_frame = AsyncMock()

    await observer.process_frame(
        VADUserStartedSpeakingFrame(),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(
        VADUserStartedSpeakingFrame(),
        FrameDirection.UPSTREAM,
    )

    snapshot = observer.snapshot()

    assert snapshot["speechStartedCount"] == 1
    assert snapshot["speechObserved"] is True
    assert observer.push_frame.await_count == 2


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
