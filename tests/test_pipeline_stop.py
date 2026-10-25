import asyncio
import gc
import hashlib
import inspect
import io
import threading
import weakref
from enum import StrEnum
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InputAudioRawFrame,
    StartFrame,
    StopFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.transcriptions.language import Language
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.workers.runner import WorkerRunner
from websockets.protocol import State

import src.pipeline as pipeline_module
from src.config import Config
from src.core.provider_errors import ProviderTransportError
from src.mic_silence_stop import MicSilenceStopObserver
from src.microphone import MicrophoneInput, RustPrototypeFrameSource
from src.pipeline import (
    LIVE_STT_STOP_END_FRAME_FINALIZES,
    LIVE_STT_STOP_PROVIDER_MANUAL,
    LIVE_STT_STOP_VAD_FLUSH_BEFORE_END,
    PipecatVadSpeechObserver,
    ScriberPipeline,
    SegmentedSTTRecordingGate,
    SonioxAsyncProcessor,
    TranscriptionCallbackProcessor,
    _AnalyzerCache,
    _create_soniox_smart_turn_processor,
    _format_speaker_transcript_tokens,
    _live_analyzer_diagnostics,
    _live_analyzer_requirements,
    _live_recording_gate_needed,
    _live_service_uses_native_streaming,
    _live_stt_stop_strategy,
    _ordered_live_pipeline_steps,
    _ProviderIngressDrainProcessor,
    _rust_capture_wav_plan,
    _speechmatics_capture_time_wav_enabled,
    direct_file_workflow_timeout_seconds,
)
from src.transcript_artifacts import freeze_provider_route


def test_microphone_transport_has_no_silently_ignored_analyzer_arguments():
    parameters = inspect.signature(MicrophoneInput.__init__).parameters

    assert "vad_analyzer" not in parameters
    assert "turn_analyzer" not in parameters


@pytest.mark.asyncio
async def test_pipeline_worker_silence_stop_preserves_audio():
    from pipecat.audio.vad.vad_analyzer import VADState

    clock = SimpleNamespace(now=0.0)
    timed_out = asyncio.Event()
    received = []

    async def _analyze(_pcm):
        clock.now += 0.032
        return VADState.QUIET

    analyzer = SimpleNamespace(
        set_sample_rate=lambda _rate: None,
        analyze_audio=_analyze,
        cleanup=AsyncMock(),
    )
    observer = MicSilenceStopObserver(
        analyzer=analyzer,
        silence_seconds=1,
        on_timeout=timed_out.set,
        clock=lambda: clock.now,
    )

    class _Provider(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if direction == FrameDirection.DOWNSTREAM:
                received.append(frame)
            await self.push_frame(frame, direction)

    worker = PipelineWorker(
        Pipeline([observer, _Provider()]),
        params=PipelineParams(),
        enable_rtvi=False,
        enable_turn_tracking=False,
    )
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())
    audio = [InputAudioRawFrame(audio=bytes(1024), sample_rate=16_000, num_channels=1) for _ in range(40)]
    try:
        await worker.queue_frames(audio)
        await asyncio.wait_for(timed_out.wait(), timeout=2.0)
        await worker.stop_when_done()
        await asyncio.wait_for(run_task, timeout=2.0)
        assert [frame for frame in received if isinstance(frame, InputAudioRawFrame)] == audio
        assert not any(
            isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)) for frame in received
        )
    finally:
        if not worker.has_finished():
            await worker.cancel(reason="test cleanup")
        if not run_task.done():
            await runner.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
    analyzer.cleanup.assert_awaited_once()


def test_rust_capture_wav_plan_is_limited_to_exact_speechmatics_batch_route(monkeypatch):
    monkeypatch.delenv("SCRIBER_SPEECHMATICS_BATCH_BASE_URL", raising=False)
    monkeypatch.delenv("SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV", raising=False)
    endpoint_sha256 = hashlib.sha256(
        pipeline_module.SPEECHMATICS_BATCH_DEFAULT_BASE_URL.rstrip("/").encode("utf-8")
    ).hexdigest()
    route = freeze_provider_route(
        workload="live_mic",
        provider="speechmatics_async",
        model="batch-v2",
        provider_route="batch_v2",
        audio_input_format="wav_pcm16",
        audio_selection_mode="generated",
        audio_preparation_implementation="wav_pcm16_file_v1",
        provider_endpoint_sha256=endpoint_sha256,
        diarization_requested=False,
    ).execution_route()
    assert (
        _rust_capture_wav_plan(
            "speechmatics_async",
            sample_rate=16_000,
            channels=1,
            execution_route=route,
        )
        is None
    )

    def enabled_plan(provider, **kwargs):
        return _rust_capture_wav_plan(
            provider,
            candidate_enabled=True,
            **kwargs,
        )

    plan = enabled_plan(
        "speechmatics_async",
        sample_rate=16_000,
        channels=1,
        execution_route=route,
    )
    assert plan == {
        "schemaVersion": "1",
        "format": "wav_pcm16",
        "implementation": "wav_pcm16_file_v1",
        "sampleRate": 16_000,
        "channels": 1,
        "bitsPerSample": 16,
        "queueCapacityFrames": 64,
        "maxPcmBytes": 64 * 1024 * 1024,
    }
    assert (
        enabled_plan(
            "azure_mai",
            sample_rate=16_000,
            channels=1,
            execution_route=route,
        )
        is None
    )
    assert (
        enabled_plan(
            "speechmatics_async",
            sample_rate=16_000,
            channels=1,
            execution_route=None,
        )
        is None
    )
    for field, stale_value in (
        ("model", "unknown"),
        ("provider_route", "legacy"),
        ("audio_input_format", "mp3"),
        ("provider_audio_capability_id", "stale"),
        ("provider_audio_capability_revision", "stale"),
        ("audio_selection_mode", "passthrough"),
        ("audio_preparation_implementation", "wav_pcm16_virtual"),
        ("provider_endpoint_sha256", "0" * 64),
    ):
        stale_route = dict(route)
        stale_route[field] = stale_value
        assert (
            enabled_plan(
                "speechmatics_async",
                sample_rate=16_000,
                channels=1,
                execution_route=stale_route,
            )
            is None
        )

    unverified_route = dict(route)
    unverified_route["audio_input_format_verified"] = False
    assert (
        enabled_plan(
            "speechmatics_async",
            sample_rate=16_000,
            channels=1,
            execution_route=unverified_route,
        )
        is None
    )

    monkeypatch.setenv(
        "SCRIBER_SPEECHMATICS_BATCH_BASE_URL",
        "https://custom.invalid/v2",
    )
    # The route is an immutable start-time snapshot. A later env mutation must
    # not change the already-frozen candidate decision.
    assert (
        enabled_plan(
            "speechmatics_async",
            sample_rate=16_000,
            channels=1,
            execution_route=route,
        )
        == plan
    )

    baseline_route = dict(route)
    baseline_route["audio_preparation_implementation"] = "python_reserved_wav_header_v1"
    assert (
        enabled_plan(
            "speechmatics_async",
            sample_rate=16_000,
            channels=1,
            execution_route=baseline_route,
        )
        is None
    )


def test_speechmatics_capture_time_wav_defaults_off(monkeypatch):
    monkeypatch.delenv("SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV", raising=False)

    assert _speechmatics_capture_time_wav_enabled() is False


def test_speechmatics_capture_time_candidate_is_frozen_per_pipeline(monkeypatch):
    monkeypatch.setenv("SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV", "0")
    pipeline = ScriberPipeline(service_name="speechmatics_async")
    monkeypatch.setenv("SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV", "1")

    assert pipeline._speechmatics_capture_time_wav_enabled is False


def test_live_analyzer_requirements_respect_disabled_vad_setting(monkeypatch):
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", False)
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")

    assert _live_analyzer_requirements("azure_mai") == (False, False)
    assert _live_analyzer_requirements("openrouter_stt") == (False, False)
    assert _live_analyzer_requirements("deepgram", segmented_service=True) == (
        False,
        False,
    )
    assert _live_analyzer_requirements("soniox") == (False, False)


def test_live_analyzer_requirements_enable_only_requested_paths(monkeypatch):
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", True)
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")

    assert _live_analyzer_requirements("azure_mai") == (True, False)
    assert _live_analyzer_requirements("openrouter_stt") == (True, False)
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

    assert (
        _live_recording_gate_needed(
            "azure_mai",
            segmented_service=False,
            vad_attached=False,
        )
        is True
    )
    assert (
        _live_recording_gate_needed(
            "deepgram",
            segmented_service=False,
            vad_attached=True,
        )
        is False
    )


@pytest.mark.parametrize(
    ("service_name", "expected"),
    [
        ("openai", LIVE_STT_STOP_VAD_FLUSH_BEFORE_END),
        ("deepgram", LIVE_STT_STOP_VAD_FLUSH_BEFORE_END),
        ("elevenlabs", LIVE_STT_STOP_VAD_FLUSH_BEFORE_END),
        ("azure_mai", LIVE_STT_STOP_END_FRAME_FINALIZES),
        ("gladia", LIVE_STT_STOP_END_FRAME_FINALIZES),
        ("openai_async", LIVE_STT_STOP_END_FRAME_FINALIZES),
        ("openrouter_stt", LIVE_STT_STOP_END_FRAME_FINALIZES),
        ("soniox_async", LIVE_STT_STOP_END_FRAME_FINALIZES),
    ],
)
def test_live_stt_stop_strategy_records_provider_capability(service_name, expected):
    assert _live_stt_stop_strategy(service_name) == expected


def test_live_stt_stop_strategy_keeps_provider_manual_soniox_path(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")

    assert _live_stt_stop_strategy("soniox") == LIVE_STT_STOP_PROVIDER_MANUAL
    assert _live_stt_stop_strategy("azure_mai", segmented_service=True) == LIVE_STT_STOP_VAD_FLUSH_BEFORE_END
    assert (
        _live_recording_gate_needed(
            "soniox",
            segmented_service=False,
            vad_attached=False,
        )
        is False
    )


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


@pytest.mark.asyncio
async def test_analyzer_cleanup_tasks_have_a_bounded_shutdown_drain():
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()

    class AsyncCleanupAnalyzer:
        async def cleanup(self) -> None:
            cleanup_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_cancelled.set()
                raise

    _AnalyzerCache._cleanup_unclaimed(AsyncCleanupAnalyzer(), label="test analyzer")
    await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
    try:
        assert await _AnalyzerCache.drain_pending_cleanup_tasks(timeout_seconds=0.01) == 1
        assert (
            await _AnalyzerCache.drain_pending_cleanup_tasks(
                timeout_seconds=1.0,
                cancel=True,
            )
            == 0
        )
        assert cleanup_cancelled.is_set()
    finally:
        await _AnalyzerCache.drain_pending_cleanup_tasks(timeout_seconds=1.0, cancel=True)


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
    provider_ingress_drain = object()

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
    terminal_buffered_steps = _ordered_live_pipeline_steps(
        audio_input=audio_input,
        vad_processor=vad_processor,
        vad_observer=vad_observer,
        segmented_gate=None,
        stt_service=stt_service,
        smart_turn_processor=None,
        error_handler=error_handler,
        transcript_callback=transcript_callback,
        text_injector=text_injector,
        provider_ingress_drain=provider_ingress_drain,
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
    assert terminal_buffered_steps == [
        audio_input,
        vad_processor,
        vad_observer,
        stt_service,
        provider_ingress_drain,
        error_handler,
        transcript_callback,
        text_injector,
    ]


@pytest.mark.parametrize(
    "service_name",
    ["azure_mai", "gemini_realtime", "speechmatics_async"],
)
@pytest.mark.asyncio
async def test_provider_ingress_drain_waits_for_delayed_last_system_audio_before_endframe(
    service_name,
):
    class _Source(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, StartFrame):
                self.started.set()
            await self.push_frame(frame, direction)

    class _DelayedTerminalProvider(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.last_audio_started = asyncio.Event()
            self.release_last_audio = asyncio.Event()
            self.received = bytearray()
            self.terminal_snapshots: list[bytes] = []

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, InputAudioRawFrame):
                if frame.audio.startswith(b"\x02\x00"):
                    self.last_audio_started.set()
                    await self.release_last_audio.wait()
                self.received.extend(frame.audio)
            elif isinstance(frame, EndFrame):
                self.terminal_snapshots.append(bytes(self.received))
            await self.push_frame(frame, direction)

    class _Sink(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    source = _Source()
    provider = _DelayedTerminalProvider()
    drain_processor = _ProviderIngressDrainProcessor()
    runtime_pipeline = Pipeline([source, provider, drain_processor, _Sink()])
    task = PipelineWorker(
        runtime_pipeline,
        params=PipelineParams(),
        enable_rtvi=False,
        enable_turn_tracking=False,
        check_dangling_tasks=False,
    )
    runner = WorkerRunner(
        handle_sigint=False,
        handle_sigterm=False,
        check_dangling_tasks=False,
    )
    pipeline = ScriberPipeline(service_name=service_name, on_status_change=None)
    pipeline.pipeline = runtime_pipeline
    pipeline.task = task
    pipeline.runner = runner
    pipeline.audio_input = source
    pipeline._provider_ingress_drain_processor = drain_processor

    await runner.add_workers(task)
    run_task = asyncio.create_task(runner.run())
    first = b"\x01\x00" * 256
    last = b"\x02\x00" * 256
    try:
        await asyncio.wait_for(source.started.wait(), timeout=1.0)
        await task.queue_frame(
            InputAudioRawFrame(
                audio=first,
                sample_rate=48_000,
                num_channels=1,
            )
        )
        await task.queue_frame(
            InputAudioRawFrame(
                audio=last,
                sample_rate=48_000,
                num_channels=1,
            )
        )
        await asyncio.wait_for(provider.last_audio_started.wait(), timeout=1.0)

        drain_task = asyncio.create_task(pipeline._await_provider_ingress_audio_drain(source))
        await asyncio.sleep(0.02)
        assert drain_task.done() is False

        provider.release_last_audio.set()
        assert await asyncio.wait_for(drain_task, timeout=1.0) is True
        await task.stop_when_done()
        await asyncio.wait_for(run_task, timeout=1.0)

        assert provider.terminal_snapshots == [first + last]
    finally:
        provider.release_last_audio.set()
        if not task.has_finished():
            await task.cancel(reason="test cleanup")
        if not run_task.done():
            await runner.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_gemini_file_pipeline_fences_delayed_last_system_audio_before_endframe(monkeypatch):
    first = b"\x01\x00" * 256
    last = b"\x02\x00" * 256

    class _FileInput(FrameProcessor):
        def __init__(self, *_args, **_kwargs):
            super().__init__()
            self.done = asyncio.Event()
            self.error = None

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if isinstance(frame, StartFrame):
                await self.push_frame(
                    InputAudioRawFrame(audio=first, sample_rate=16_000, num_channels=1),
                    direction,
                )
                await self.push_frame(
                    InputAudioRawFrame(audio=last, sample_rate=16_000, num_channels=1),
                    direction,
                )
                self.done.set()

        async def stop(self, _frame):
            return None

    class _DelayedProvider(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.last_audio_started = asyncio.Event()
            self.release_last_audio = asyncio.Event()
            self.received = bytearray()
            self.terminal_snapshots: list[bytes] = []

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, InputAudioRawFrame):
                if frame.audio.startswith(b"\x02\x00"):
                    self.last_audio_started.set()
                    await self.release_last_audio.wait()
                self.received.extend(frame.audio)
            elif isinstance(frame, EndFrame):
                self.terminal_snapshots.append(bytes(self.received))
            await self.push_frame(frame, direction)

    monkeypatch.setattr(pipeline_module, "FfmpegAudioFileInput", _FileInput)
    provider = _DelayedProvider()
    pipeline = ScriberPipeline(service_name="gemini_realtime", on_status_change=None)
    monkeypatch.setattr(
        pipeline,
        "_create_stt_service",
        lambda _session, for_file=False: provider,
    )

    transcription = asyncio.create_task(pipeline.transcribe_file("synthetic.wav"))
    try:
        await asyncio.wait_for(provider.last_audio_started.wait(), timeout=1.0)
        await asyncio.sleep(0.02)
        assert transcription.done() is False
        assert pipeline._provider_ingress_drain_processor is not None

        provider.release_last_audio.set()
        await asyncio.wait_for(transcription, timeout=2.0)

        assert provider.terminal_snapshots == [first + last]
    finally:
        provider.release_last_audio.set()
        if not transcription.done():
            transcription.cancel()
        await asyncio.gather(transcription, return_exceptions=True)


class _SingleFrameFileInput(FrameProcessor):
    """File input that feeds one audio block and optionally never finishes."""

    def __init__(self, *_args, finishes: bool = True, **_kwargs):
        super().__init__()
        self.done = asyncio.Event()
        self.error = None
        self.finishes = finishes
        self.stopped = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(
                InputAudioRawFrame(audio=b"\x01\x00" * 256, sample_rate=16_000, num_channels=1),
                direction,
            )
            if self.finishes:
                self.done.set()

    async def stop(self, _frame):
        self.stopped = True


class _FailingFileProvider(FrameProcessor):
    def __init__(self, *, permanent: bool = False, downstream: bool = False):
        super().__init__()
        self.permanent = permanent
        self.downstream = downstream

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(TranscriptionFrame("partial", "user", "now"), direction)
            if self.downstream:
                await self.push_frame(pipeline_module.ErrorFrame(error="provider rejected the file"), direction)
            else:
                await self.push_error("provider rejected the file", force_treat_as_permanent=self.permanent)
            return
        await self.push_frame(frame, direction)


def _file_pipeline_with(monkeypatch, provider, file_input, *, service_name="deepgram"):
    monkeypatch.setattr(pipeline_module, "FfmpegAudioFileInput", lambda *_a, **_k: file_input)
    texts = []
    pipeline = ScriberPipeline(
        service_name=service_name,
        on_status_change=None,
        on_transcription=lambda text, *_a, **_k: texts.append(text),
    )
    monkeypatch.setattr(pipeline, "_create_stt_service", lambda _session, for_file=False: provider)
    return pipeline, texts


@pytest.mark.asyncio
@pytest.mark.parametrize(("permanent", "downstream"), [(False, False), (True, False), (False, True)])
async def test_file_pipeline_provider_error_fails_instead_of_returning_partial_text(monkeypatch, permanent, downstream):
    file_input = _SingleFrameFileInput()
    pipeline, texts = _file_pipeline_with(
        monkeypatch,
        _FailingFileProvider(permanent=permanent, downstream=downstream),
        file_input,
    )

    with pytest.raises(RuntimeError, match="provider rejected the file"):
        await asyncio.wait_for(pipeline.transcribe_file("synthetic.wav"), timeout=5.0)

    assert texts == ["partial"]
    assert pipeline._provider_request_state != "result_received"


@pytest.mark.asyncio
@pytest.mark.parametrize("downstream", [False, True])
async def test_file_pipeline_cancelled_by_provider_error_does_not_wait_for_unfinished_input(monkeypatch, downstream):
    # FfmpegAudioFileInput signals `done` only after its queue drains, which a
    # cancelled pipeline never does; the error must still end the call.
    file_input = _SingleFrameFileInput(finishes=False)
    pipeline, _texts = _file_pipeline_with(monkeypatch, _FailingFileProvider(downstream=downstream), file_input)

    with pytest.raises(RuntimeError, match="provider rejected the file"):
        await asyncio.wait_for(pipeline.transcribe_file("synthetic.wav"), timeout=5.0)

    assert file_input.stopped is True
    assert pipeline.is_active is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["start", "audio"])
async def test_gemini_file_provider_failure_aborts_unfinished_input(monkeypatch, failure_stage):
    from src.gemini_realtime_stt import GeminiTranscribeLiveSTTService

    file_input = _SingleFrameFileInput(finishes=False)
    provider = GeminiTranscribeLiveSTTService(api_key="inert-test-key")
    connection_attempts = 0

    async def reject_connection():
        nonlocal connection_attempts
        connection_attempts += 1
        if failure_stage == "start" or connection_attempts > 1:
            # Exercise the actual adapter's downstream fatal ErrorFrame at
            # startup and after StartFrame has already passed the provider.
            await provider._fail("connection", "synthetic provider refusal", close_connection=False)
            return False
        return True

    monkeypatch.setattr(provider, "_connect", reject_connection)
    pipeline, _texts = _file_pipeline_with(monkeypatch, provider, file_input, service_name="gemini_realtime")

    with pytest.raises(RuntimeError, match="synthetic provider refusal"):
        await asyncio.wait_for(pipeline.transcribe_file("synthetic.wav"), timeout=5.0)

    assert not file_input.done.is_set()
    assert file_input.stopped is True
    assert pipeline.is_active is False
    assert pipeline.task.has_finished()
    assert pipeline._provider_request_state != "result_received"


@pytest.mark.asyncio
async def test_file_pipeline_disables_pipecat_idle_cancellation(monkeypatch):
    worker_kwargs = []

    def _worker(*args, **kwargs):
        worker_kwargs.append(kwargs)
        return PipelineWorker(*args, **kwargs)

    class _PassthroughProvider(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    monkeypatch.setattr(pipeline_module, "PipelineWorker", _worker)
    pipeline, _texts = _file_pipeline_with(monkeypatch, _PassthroughProvider(), _SingleFrameFileInput())

    await asyncio.wait_for(pipeline.transcribe_file("synthetic.wav"), timeout=5.0)

    assert worker_kwargs and worker_kwargs[0]["idle_timeout_secs"] is None
    assert pipeline._provider_request_state == "result_received"


class _EndFrameFailingProvider(FrameProcessor):
    """A provider whose stop fails, so the EndFrame never reaches the sink."""

    def __init__(self, *, blocks: bool = False):
        super().__init__()
        self.blocks = blocks
        self.end_received = asyncio.Event()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, EndFrame):
            self.end_received.set()
            if self.blocks:
                await asyncio.Event().wait()
            raise RuntimeError("provider stop failed")
        await self.push_frame(frame, direction)


@pytest.mark.asyncio
async def test_file_pipeline_fails_instead_of_hanging_when_the_provider_swallows_end_frame(monkeypatch):
    pipeline, _texts = _file_pipeline_with(
        monkeypatch, _EndFrameFailingProvider(), _SingleFrameFileInput(), service_name="groq"
    )

    with pytest.raises(RuntimeError, match="provider stop failed"):
        await asyncio.wait_for(pipeline.transcribe_file("synthetic.wav"), timeout=5.0)

    assert pipeline.task.has_finished()
    assert pipeline.is_active is False


@pytest.mark.asyncio
async def test_cancelling_a_file_stuck_on_its_end_frame_finishes(monkeypatch):
    provider = _EndFrameFailingProvider(blocks=True)
    pipeline, _texts = _file_pipeline_with(monkeypatch, provider, _SingleFrameFileInput(), service_name="groq")
    transcription = asyncio.create_task(pipeline.transcribe_file("synthetic.wav"))
    await asyncio.wait_for(provider.end_received.wait(), timeout=5.0)

    transcription.cancel()
    await asyncio.wait_for(asyncio.gather(transcription, return_exceptions=True), timeout=5.0)

    assert pipeline.task.has_finished()
    assert pipeline.is_active is False


@pytest.mark.asyncio
async def test_worker_cancel_reaches_a_worker_waiting_for_a_swallowed_end_frame():
    provider = _EndFrameFailingProvider()
    pipeline = ScriberPipeline(service_name="groq", on_status_change=None)
    pipeline.pipeline = Pipeline([provider])
    task = PipelineWorker(pipeline.pipeline, params=PipelineParams(), idle_timeout_secs=None)
    pipeline.task = task
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(task)
    run_task = asyncio.create_task(runner.run())
    try:
        await task.stop_when_done()
        await asyncio.wait_for(provider.end_received.wait(), timeout=5.0)
        await task.cancel(reason="plain cancel")
        await asyncio.sleep(0.2)
        assert not task.has_finished()  # Pipecat holds the cancel behind the EndFrame.

        await pipeline._cancel_worker_past_end_frame(task, reason="stop timeout")

        await asyncio.wait_for(task.wait(), timeout=5.0)
        await runner.cancel()
        await asyncio.wait_for(run_task, timeout=5.0)
    finally:
        if not run_task.done():
            run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


_FILE_BLOCK = 1024


class _BlocksFileInput(FrameProcessor):
    """File input that feeds 1024-sample blocks of the given levels."""

    def __init__(self, levels):
        super().__init__()
        self.levels = levels
        self.done = asyncio.Event()
        self.error = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            for level in self.levels:
                audio = int(level).to_bytes(2, "little", signed=True) * _FILE_BLOCK
                await self.push_frame(InputAudioRawFrame(audio=audio, sample_rate=16_000, num_channels=1), direction)
            self.done.set()

    async def stop(self, _frame):
        return None


class _CommittingProvider(FrameProcessor):
    """Transcribes each committed segment shortly after its VAD stop."""

    reports_provider_commit_results = True

    def __init__(self, *, answers: bool = True):
        super().__init__()
        self.answers = answers
        self.samples = 0
        self.commit_points: list[int] = []
        self.settled_at_end: int | None = None
        self._answer_tasks: list[asyncio.Task] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.samples += len(frame.audio) // 2
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.commit_points.append(self.samples)
            if self.answers:
                self._answer_tasks.append(asyncio.create_task(self._answer(len(self.commit_points))))
        elif isinstance(frame, EndFrame):
            self.settled_at_end = pipeline_module._provider_commit_results(self).settled
        await self.push_frame(frame, direction)

    async def _answer(self, number: int) -> None:
        await asyncio.sleep(0.05)
        await self.push_frame(TranscriptionFrame(f"segment {number}", "user", "now"))
        pipeline_module._provider_commit_results(self).settle()


def _blocks(seconds: float, level: int) -> list[int]:
    return [level] * round(seconds * 16_000 / _FILE_BLOCK)


@pytest.mark.asyncio
async def test_realtime_file_commits_close_at_pauses_and_wait_for_every_result(monkeypatch):
    # 25 s speech, 0.5 s pause, 10 s speech: one cut in the pause, one tail.
    levels = _blocks(25, 8000) + _blocks(0.5, 0) + _blocks(10, 8000)
    provider = _CommittingProvider()
    pipeline, texts = _file_pipeline_with(monkeypatch, provider, _BlocksFileInput(levels), service_name="openai")

    await asyncio.wait_for(pipeline.transcribe_file("synthetic.wav"), timeout=5.0)

    pause_cut = (len(_blocks(25, 8000)) + 4) * _FILE_BLOCK  # 0.2 s of quiet blocks
    assert provider.commit_points == [pause_cut, len(levels) * _FILE_BLOCK]
    assert texts == ["segment 1", "segment 2"]
    assert provider.settled_at_end == 2


@pytest.mark.asyncio
async def test_realtime_file_commits_cut_continuous_speech_at_the_maximum(monkeypatch):
    levels = _blocks(35, 8000)
    provider = _CommittingProvider()
    pipeline, texts = _file_pipeline_with(monkeypatch, provider, _BlocksFileInput(levels), service_name="elevenlabs")

    await asyncio.wait_for(pipeline.transcribe_file("synthetic.wav"), timeout=5.0)

    forced_cut = -(-30 * 16_000 // _FILE_BLOCK) * _FILE_BLOCK
    assert provider.commit_points == [forced_cut, len(levels) * _FILE_BLOCK]
    assert texts == ["segment 1", "segment 2"]


class _RecordingSegmentedProvider(SegmentedSTTService):
    def __init__(self):
        super().__init__(sample_rate=16_000)
        self.segments: list[int] = []

    async def run_stt(self, audio):
        self.segments.append(len(audio))
        yield TranscriptionFrame(f"segment {len(self.segments)}", "user", "now")


@pytest.mark.asyncio
async def test_segmented_file_uploads_close_at_pauses_instead_of_keeping_one_second(monkeypatch):
    # Without VAD frames a segmented service kept only the last second and
    # uploaded nothing.
    levels = _blocks(25, 8000) + _blocks(0.5, 0) + _blocks(10, 8000)
    provider = _RecordingSegmentedProvider()
    pipeline, texts = _file_pipeline_with(monkeypatch, provider, _BlocksFileInput(levels), service_name="groq")

    await asyncio.wait_for(pipeline.transcribe_file("synthetic.wav"), timeout=10.0)

    assert len(provider.segments) == 2
    assert texts == ["segment 1", "segment 2"]


@pytest.mark.asyncio
async def test_deepgram_file_finalizes_once_after_the_last_audio(monkeypatch):
    levels = _blocks(35, 8000) + _blocks(1, 0)
    provider = _CommittingProvider()
    pipeline, texts = _file_pipeline_with(monkeypatch, provider, _BlocksFileInput(levels), service_name="deepgram")

    await asyncio.wait_for(pipeline.transcribe_file("synthetic.wav"), timeout=5.0)

    assert provider.commit_points == [len(levels) * _FILE_BLOCK]
    assert texts == ["segment 1"]
    assert provider.settled_at_end == 1


@pytest.mark.asyncio
async def test_realtime_services_report_one_result_per_commit(monkeypatch):
    for attribute in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY"):
        monkeypatch.setattr(Config, attribute, "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "")
    openai = ScriberPipeline(service_name="openai")._create_stt_service(object())
    elevenlabs = ScriberPipeline(service_name="elevenlabs")._create_stt_service(object())
    deepgram = ScriberPipeline(service_name="deepgram")._create_stt_service(object())

    # Results without text still answer their commit.
    await openai._handle_transcription_completed({"transcript": ""})
    await elevenlabs._on_committed_transcript({"text": ""})
    await deepgram._on_message(SimpleNamespace(is_final=True, from_finalize=True))
    await deepgram._on_message(SimpleNamespace(is_final=True, from_finalize=False))

    for service in (openai, elevenlabs, deepgram):
        assert service.reports_provider_commit_results is True
        assert pipeline_module._provider_commit_results(service).settled == 1


@pytest.mark.asyncio
async def test_realtime_file_without_a_commit_result_finishes_after_the_idle_timeout(monkeypatch):
    monkeypatch.setattr(pipeline_module, "_FILE_COMMIT_RESULT_IDLE_TIMEOUT_SECONDS", 0.1)
    provider = _CommittingProvider(answers=False)
    pipeline, texts = _file_pipeline_with(
        monkeypatch, provider, _BlocksFileInput(_blocks(2, 8000)), service_name="openai"
    )

    await asyncio.wait_for(pipeline.transcribe_file("synthetic.wav"), timeout=5.0)

    assert provider.commit_points == [len(_blocks(2, 8000)) * _FILE_BLOCK]
    assert texts == []
    assert provider.settled_at_end == 0


@pytest.mark.asyncio
async def test_provider_ingress_drain_timeout_fails_closed_without_terminal_upload(
    monkeypatch,
):
    class _NeverAcknowledgedAudioInput:
        def __init__(self):
            self.frames = []

        async def push_frame(self, frame, direction):
            self.frames.append((frame, direction))

    monkeypatch.setattr(
        pipeline_module,
        "_PROVIDER_INGRESS_DRAIN_TIMEOUT_SECONDS",
        0.01,
    )
    provider = _BufferedProvider()
    pipeline = ScriberPipeline(service_name="azure_mai", on_status_change=None)
    pipeline.pipeline = _DummyRuntimePipelineGraph([provider])
    pipeline.task = _DummyTask(asyncio.Event(), set_done_on_stop=False)
    pipeline.runner = _DummyRunner()
    pipeline._provider_ingress_drain_processor = _ProviderIngressDrainProcessor()
    audio_input = _NeverAcknowledgedAudioInput()

    with pytest.raises(RuntimeError, match="drain barrier timed out"):
        await pipeline._await_provider_ingress_audio_drain(audio_input)

    assert len(audio_input.frames) == 1
    assert provider._skip_terminal_transcription is True
    assert pipeline.task.cancel_called is True
    assert pipeline.runner.cancel_called is True
    assert pipeline._terminal_error is not None


@pytest.mark.asyncio
async def test_provider_ingress_drain_unavailable_with_task_fails_closed():
    pipeline = ScriberPipeline(service_name="azure_mai", on_status_change=None)
    pipeline.task = _DummyTask(asyncio.Event(), set_done_on_stop=False)
    pipeline.runner = _DummyRunner()

    with pytest.raises(RuntimeError, match="drain barrier is unavailable"):
        await pipeline._await_provider_ingress_audio_drain(None)

    assert pipeline.task.cancel_called is True
    assert pipeline.runner.cancel_called is True
    assert pipeline._terminal_error is not None


def test_soniox_token_formatter_preserves_speaker_zero_and_numbers_by_first_appearance():
    assert (
        _format_speaker_transcript_tokens(
            [
                {"speaker": 0, "text": " First"},
                {"speaker": 0, "text": " turn."},
                {"speaker": 4, "text": " Reply."},
            ]
        )
        == "[Speaker 1]: First turn.\n\n[Speaker 2]: Reply."
    )


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


@pytest.mark.parametrize("provider", ("google", "groq"))
@pytest.mark.parametrize("runner_fails", (False, True))
@pytest.mark.asyncio
async def test_pipecat_owned_file_request_state_is_terminal_only_after_success(
    monkeypatch,
    provider,
    runner_fails,
):
    class _Transport:
        async def session_view(self, *, provider, marker):
            marker("request_started", timestamp_ns=1)
            return object()

    class _FileInput:
        def __init__(self, *_args, **_kwargs):
            self.done = asyncio.Event()
            self.done.set()
            self.error = None

        async def stop(self, _frame):
            return None

    class _Task:
        def __init__(self, *_args, **_kwargs):
            self.finished = False
            self.stop_requested = asyncio.Event()

        def event_handler(self, _name):
            return lambda handler: handler

        def has_finished(self):
            return self.finished

        async def stop_when_done(self):
            self.stop_requested.set()

        async def cancel(self, **_kwargs):
            self.finished = True

    class _Runner:
        def __init__(self, **_kwargs):
            self.workers = ()

        async def add_workers(self, *workers):
            self.workers = workers

        async def run(self):
            # A real worker runs until the EndFrame queued by stop_when_done.
            await self.workers[0].stop_requested.wait()
            self.workers[0].finished = True
            if runner_fails:
                raise RuntimeError("synthetic provider failure")

        async def cancel(self):
            return None

    monkeypatch.setattr("src.pipeline.FfmpegAudioFileInput", _FileInput)
    monkeypatch.setattr("src.pipeline.Pipeline", lambda steps: steps)
    monkeypatch.setattr("src.pipeline.PipelineWorker", _Task)
    monkeypatch.setattr("src.pipeline.WorkerRunner", _Runner)

    pipeline = ScriberPipeline(
        service_name=provider,
        provider_http_transport=_Transport(),
    )
    monkeypatch.setattr(
        pipeline,
        "_create_stt_service",
        lambda _session, for_file=False: object(),
    )

    if runner_fails:
        with pytest.raises(RuntimeError, match="synthetic provider failure"):
            await pipeline.transcribe_file("synthetic.wav")
        assert pipeline._provider_request_state == "bytes_may_have_been_sent"
    else:
        await pipeline.transcribe_file("synthetic.wav")
        assert pipeline._provider_request_state == "result_received"
    assert pipeline._provider_request_started is True


@pytest.mark.asyncio
async def test_deepgram_direct_uses_frozen_model_after_config_changes(monkeypatch, tmp_path):
    source = tmp_path / "deepgram.wav"
    source.write_bytes(b"audio")
    captured = {}

    async def fake_transcribe(**kwargs):
        captured.update(kwargs)
        return {"results": {"channels": [{"alternatives": [{"transcript": "done", "words": []}]}]}}

    monkeypatch.setattr(
        "src.cloud_async_stt.transcribe_with_deepgram_pre_recorded",
        fake_transcribe,
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
async def test_gemini_direct_uses_frozen_model_after_config_changes(monkeypatch, tmp_path):
    source = tmp_path / "gemini.wav"
    source.write_bytes(b"audio")
    captured = {}

    async def fake_transcribe(**kwargs):
        captured.update(kwargs)
        return {"candidates": [{"content": {"parts": [{"text": "done"}]}}]}

    monkeypatch.setattr("src.cloud_async_stt.transcribe_with_gemini_audio", fake_transcribe)
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
@pytest.mark.parametrize("diarize", [True, False])
async def test_azure_mai_direct_uses_frozen_model_and_vocab_after_config_changes(monkeypatch, tmp_path, diarize):
    from src.audio_prepare import ProbedAudioInput
    from src.core.provider_audio_formats import AudioInputFormat

    source = tmp_path / "azure.mp3"
    source.write_bytes(b"audio")
    captured = {}
    monkeypatch.setattr(
        "src.openrouter_audio.probe_audio_input_file",
        lambda path: ProbedAudioInput(AudioInputFormat.MP3, "mp3", "mp3", 16_000, 1, 1_000, path.stat().st_size),
    )

    async def fake_transcribe(**kwargs):
        captured.update(kwargs)
        return {"combinedPhrases": [{"text": "done"}]}

    monkeypatch.setattr("src.azure_mai_stt.transcribe_with_azure_mai", fake_transcribe)
    monkeypatch.setattr(Config, "AZURE_MAI_SPEECH_KEY", "key")
    monkeypatch.setattr(Config, "AZURE_MAI_REGION", "northeurope")
    pipeline = ScriberPipeline(
        service_name="azure_mai",
        direct_file_speaker_diarization=diarize,
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
    assert captured["diarize"] is diarize


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["microsoft/mai-transcribe-2", "microsoft/mai-transcribe-1.5"])
async def test_openrouter_mai_direct_uses_frozen_model_and_language_without_openai_options(
    monkeypatch, tmp_path, model
):
    source = tmp_path / "openrouter.wav"
    source.write_bytes(b"RIFF-audio")
    captured = {}

    async def fake_transcribe(**kwargs):
        captured.update(kwargs)
        return {"text": "done"}

    monkeypatch.setattr(
        "src.cloud_async_stt.transcribe_with_openrouter_audio_transcription",
        fake_transcribe,
    )
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "one-key")
    pipeline = ScriberPipeline(
        service_name="openrouter_stt",
        execution_route={
            "model": model,
            "language": "de-DE",
        },
    )
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")

    await pipeline.transcribe_file_direct(str(source))

    assert captured["api_key"] == "one-key"
    assert captured["model"] == model
    assert captured["language"] == "de-DE"
    assert "custom_vocab" not in captured
    assert "diarize" not in captured


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

    with pytest.raises(ProviderTransportError) as raised:
        await processor._transcribe_async(b"\0\0" * 160)

    error = raised.value
    assert error.provider == "soniox"
    assert error.operation == "upload"
    assert error.status == 415
    assert error.diagnostic_metadata() == {
        "provider_operation": "upload",
        "status": 415,
        "response_bytes": len(b"unsupported request"),
    }
    assert "unsupported request" not in str(error)
    assert "unsupported request" not in repr(vars(error))
    assert "upload rejected" not in str(error)
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

    with pytest.raises(ProviderTransportError) as raised:
        await processor._transcribe_async(b"\0\0" * 160)

    error = raised.value
    assert error.provider == "soniox"
    assert error.operation == "upload"
    assert error.status == 415
    assert "unsupported request" not in str(error)
    assert "unsupported request" not in repr(vars(error))
    assert "upload rejected" not in str(error)
    assert len(session.post_calls) == 1
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
        isinstance(frame, TranscriptionFrame) and frame.text == "streamed transcript" for frame, _direction in captured
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


@pytest.mark.parametrize("model", [None, "microsoft/mai-transcribe-1.5"])
def test_openrouter_mai_factory_and_runtime_configuration_use_pinned_batch_route(monkeypatch, model):
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "shared-openrouter-key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    session = object()
    pipeline = ScriberPipeline(
        service_name="openrouter_stt",
        execution_route={"model": model} if model is not None else None,
    )

    service = pipeline._create_stt_service(session)
    configuration = pipeline.stt_runtime_configuration()

    assert type(service).__name__ == "OpenRouterSTTProcessor"
    assert service._api_key == "shared-openrouter-key"
    assert service._model == (model or "microsoft/mai-transcribe-2")
    assert service._language == "de-DE"
    assert service._session is session
    assert configuration["provider"] == "openrouter_stt"
    assert configuration["model"] == (model or "microsoft/mai-transcribe-2")
    assert configuration["mode"] == "batch"
    assert configuration["language"] == "de-DE"


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
        ("elevenlabs", "ELEVENLABS_API_KEY", "ScriberElevenLabsRealtimeSTTService"),
        ("deepgram", "DEEPGRAM_API_KEY", "ScriberDeepgramSTTService"),
        ("gladia", "GLADIA_API_KEY", "ScriberGladiaSTTService"),
        ("groq", "GROQ_API_KEY", "GroqSTTService"),
        ("openai", "OPENAI_API_KEY", "ScriberOpenAIRealtimeSTTService"),
        ("speechmatics", "SPEECHMATICS_API_KEY", "ScriberSpeechmaticsSTTService"),
    ),
)
@pytest.mark.asyncio
async def test_pipecat_live_factories_match_runtime_signatures(
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
        if service_name == "speechmatics":
            # Pipecat 1.14 reaches Speechmatics live only through Agent STT.
            assert service._base_url == "wss://eu2.rt.speechmatics.com/v2/agent"
            assert service._settings.model == "linden-1"
            assert service._settings.turn_detection_mode == service.TurnDetectionMode.VAD
            assert service._settings.speaker_active_format == "{text}"
            assert [entry.content for entry in service._settings.additional_vocab] == ["Scriber", "Pipecat"]
    finally:
        cleanup = getattr(service, "cleanup", None)
        if callable(cleanup):
            await cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("flushing", [True, False])
async def test_speechmatics_stop_delivers_the_open_turn_before_teardown(monkeypatch, flushing):
    from pipecat.utils.asyncio.task_manager import TaskManager

    monkeypatch.setattr(Config, "SPEECHMATICS_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "")
    if not flushing:
        # Pipecat's own stop path, kept as the control for the regression.
        monkeypatch.setattr(pipeline_module, "_speechmatics_stop_flushing_service_class", lambda base: base)
    service = ScriberPipeline(service_name="speechmatics")._create_stt_service(object())
    service._task_manager = TaskManager()

    class _Client:
        def __init__(self):
            self.disconnects = 0

        async def disconnect(self):
            self.disconnects += 1
            if self.disconnects == 1:
                # The SDK flushes the open turn before EndOfTranscript arrives.
                service._stt_msg_queue.put_nowait(
                    {
                        "message": "AddSegment",
                        "segment": {"transcript": "der letzte Satz"},
                        "metadata": {"start_time": 1.0, "end_time": 2.0},
                    }
                )
                await asyncio.sleep(0)

    pushed = []

    async def _push_frame(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    monkeypatch.setattr(service, "push_frame", _push_frame)
    client = _Client()
    service._client = client
    service._stt_msg_task = service.create_task(service._process_stt_messages())

    await asyncio.wait_for(service.stop(EndFrame()), timeout=5.0)

    finals = [frame.text for frame in pushed if isinstance(frame, TranscriptionFrame)]
    assert finals == (["der letzte Satz"] if flushing else [])
    assert service._client is None
    assert service._stt_msg_task is None


class _FakeGoogleStreams:
    """Google streaming: the open utterance is finalized once requests end."""

    def __init__(self):
        self.streams: list[list[bytes]] = []
        self.audio_received = asyncio.Event()

    async def streaming_recognize(self, *, requests):
        received: list[bytes] = []
        self.streams.append(received)
        half_closed = asyncio.Event()

        async def _consume():
            async for request in requests:
                if request.audio:
                    received.append(request.audio)
                    self.audio_received.set()
            half_closed.set()

        consumer = asyncio.create_task(_consume())

        async def _responses():
            try:
                await half_closed.wait()
                result = SimpleNamespace(
                    alternatives=[SimpleNamespace(transcript=b" ".join(received).decode())],
                    is_final=True,
                )
                yield SimpleNamespace(results=[result])
            finally:
                consumer.cancel()

        return _responses()


def _google_service_with_fake_streams(monkeypatch, tmp_path, *, finalizing=True):
    import pipecat.services.google.stt as google_stt
    from pipecat.utils.asyncio.task_manager import TaskManager

    credentials = tmp_path / "google.json"
    credentials.write_text('{"project_id": "scriber-test"}', encoding="utf-8")
    client = _FakeGoogleStreams()
    monkeypatch.setattr(
        google_stt.service_account.Credentials,
        "from_service_account_file",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(google_stt.speech_v2, "SpeechAsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(Config, "GOOGLE_APPLICATION_CREDENTIALS", str(credentials))
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    if not finalizing:
        # Pipecat's own stop path, kept as the control for the regression.
        monkeypatch.setattr(pipeline_module, "_google_finalizing_service_class", lambda base, _request: base)
    service = ScriberPipeline(service_name="google")._create_stt_service(object())
    service._task_manager = TaskManager()
    pushed = []

    async def _push_frame(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    monkeypatch.setattr(service, "push_frame", _push_frame)
    return service, client, pushed


async def _send_google_audio(service, *chunks: bytes) -> None:
    for chunk in chunks:
        async for _frame in service.run_stt(chunk):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("finalizing", (True, False))
async def test_google_stop_finalizes_the_open_utterance(monkeypatch, tmp_path, finalizing):
    service, client, pushed = _google_service_with_fake_streams(monkeypatch, tmp_path, finalizing=finalizing)
    await service._connect()
    await _send_google_audio(service, b"der", b"letzte", b"Satz")
    await asyncio.wait_for(client.audio_received.wait(), timeout=5.0)

    await asyncio.wait_for(service.stop(EndFrame()), timeout=15.0)

    finals = [frame.text for frame in pushed if isinstance(frame, TranscriptionFrame)]
    assert finals == (["der letzte Satz"] if finalizing else [])
    assert service._streaming_task is None


@pytest.mark.asyncio
async def test_google_rotation_finalizes_each_stream_in_audio_order(monkeypatch, tmp_path):
    # Rotate before every later chunk so each stream carries exactly one.
    monkeypatch.setattr(pipeline_module, "_GOOGLE_STREAM_ROTATION_SECONDS", -1.0)
    service, client, pushed = _google_service_with_fake_streams(monkeypatch, tmp_path)
    await service._connect()
    await _send_google_audio(service, b"eins", b"zwei", b"drei")

    await asyncio.wait_for(service.stop(EndFrame()), timeout=15.0)

    assert client.streams == [[b"eins"], [b"zwei"], [b"drei"]]
    finals = [frame.text for frame in pushed if isinstance(frame, TranscriptionFrame)]
    assert finals == ["eins", "zwei", "drei"]


@pytest.mark.asyncio
async def test_google_stop_without_audio_opens_no_stream(monkeypatch, tmp_path):
    service, client, pushed = _google_service_with_fake_streams(monkeypatch, tmp_path)
    await service._connect()

    await asyncio.wait_for(service.stop(EndFrame()), timeout=5.0)

    assert client.streams == []
    assert not [frame for frame in pushed if isinstance(frame, TranscriptionFrame)]


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


def test_runtime_configuration_uses_frozen_route_language_region_and_mode(monkeypatch):
    route = freeze_provider_route(
        workload="file",
        provider="soniox",
        language="de-DE",
        provider_region="eu",
    )
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "SONIOX_REGION", "us")
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")

    configuration = ScriberPipeline(
        service_name="soniox",
        execution_route=route.execution_route(),
    ).stt_runtime_configuration()

    assert configuration["model"] == route.model
    assert configuration["mode"] == "batch"
    assert configuration["language"] == "de-DE"
    assert configuration["region"] == "eu"


@pytest.mark.parametrize(
    ("provider", "default_model"),
    (
        ("assemblyai_realtime", "universal-3-5-pro"),
        ("deepgram", "nova-3"),
        ("openai", "gpt-realtime-whisper"),
        ("google", "latest_long"),
        ("elevenlabs", "scribe_v2_realtime"),
        ("speechmatics", "enhanced"),
    ),
)
def test_streaming_background_contract_rejects_unknown_frozen_model(
    provider,
    default_model,
):
    execution_route = freeze_provider_route(
        workload="file",
        provider=provider,
        model=default_model,
    ).execution_route()
    execution_route["model"] = "unknown-model"

    with pytest.raises(ValueError, match="no verified audio format capability"):
        ScriberPipeline(
            service_name=provider,
            execution_route=execution_route,
        )._require_realtime_pcm_request_contract(
            provider=provider,
            default_model=default_model,
        )


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

    module = SimpleNamespace(
        GoogleSTTService=_GoogleSTTService,
        speech_v2=SimpleNamespace(__name__="google.cloud.speech_v2"),
        cloud_speech=SimpleNamespace(StreamingRecognizeRequest=dict),
    )
    monkeypatch.setattr(Config, "GOOGLE_APPLICATION_CREDENTIALS", "C:\\keys\\google.json")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="google")._create_stt_service(object())

    assert isinstance(service, _GoogleSTTService)
    assert type(service).__name__ == "ScriberGoogleSTTService"
    assert service.credentials_path == "C:\\keys\\google.json"
    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.settings.kwargs == {
        "model": "latest_long",
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

    module = SimpleNamespace(
        GoogleSTTService=_GoogleSTTService,
        speech_v2=SimpleNamespace(__name__="google.cloud.speech_v2"),
        cloud_speech=SimpleNamespace(StreamingRecognizeRequest=dict),
    )
    monkeypatch.setattr(Config, "GOOGLE_APPLICATION_CREDENTIALS", "C:\\keys\\google.json")
    monkeypatch.setattr(Config, "LANGUAGE", "auto")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(service_name="google")._create_stt_service(object())

    assert "languages" not in service.settings.kwargs
    assert service.settings.kwargs["model"] == "latest_long"


def test_google_cloud_factory_requires_frozen_v2_route_and_model(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _GoogleSTTService:
        Settings = _Settings

        def __init__(self, *, credentials_path, sample_rate, settings):
            self.settings = settings

    module = SimpleNamespace(
        GoogleSTTService=_GoogleSTTService,
        speech_v2=SimpleNamespace(__name__="google.cloud.speech_v2"),
        cloud_speech=SimpleNamespace(StreamingRecognizeRequest=dict),
    )
    monkeypatch.setattr(Config, "GOOGLE_APPLICATION_CREDENTIALS", "C:\\keys\\google.json")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)
    route = freeze_provider_route(
        workload="file",
        provider="google",
        language="de-DE",
        custom_vocab="Scriber, Pipecat",
    )
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed")

    service = ScriberPipeline(
        service_name="google",
        execution_route=route.execution_route(),
    )._create_stt_service(object())
    assert service.settings.kwargs["model"] == "latest_long"
    assert service.settings.kwargs["languages"] == [Language.DE]

    stale_route = route.execution_route()
    stale_route["provider_route"] = "cloud_streaming_v1"
    with pytest.raises(ValueError, match="no verified audio format capability"):
        ScriberPipeline(
            service_name="google",
            execution_route=stale_route,
        )._create_stt_service(object())

    unknown_model = route.execution_route()
    unknown_model["model"] = "unknown-google-model"
    with pytest.raises(ValueError, match="no verified audio format capability"):
        ScriberPipeline(
            service_name="google",
            execution_route=unknown_model,
        )._create_stt_service(object())


def test_google_cloud_factory_rejects_non_v2_pipecat_module(monkeypatch):
    module = SimpleNamespace(
        GoogleSTTService=SimpleNamespace,
        speech_v2=SimpleNamespace(__name__="google.cloud.speech_v1"),
    )
    monkeypatch.setattr(Config, "GOOGLE_APPLICATION_CREDENTIALS", "C:\\keys\\google.json")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    with pytest.raises(RuntimeError, match="verified Speech V2 API"):
        ScriberPipeline(service_name="google")._create_stt_service(object())


def test_openai_realtime_factory_uses_frozen_model_and_language(monkeypatch):
    class _Settings:
        def __init__(self, *, model, language):
            self.model = model
            self.language = language

    class _OpenAIRealtimeSTTService:
        Settings = _Settings

        def __init__(self, *, api_key, settings, turn_detection):
            self.api_key = api_key
            self.settings = settings
            self.turn_detection = turn_detection

    module = SimpleNamespace(
        OpenAIRealtimeSTTService=_OpenAIRealtimeSTTService,
    )
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "key")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)
    route = freeze_provider_route(
        workload="file",
        provider="openai",
        model="gpt-realtime-whisper",
        language="de-DE",
        custom_vocab="Scriber, Pipecat",
    )
    monkeypatch.setattr(Config, "OPENAI_REALTIME_STT_MODEL", "changed-after-freeze")
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed")

    service = ScriberPipeline(
        service_name="openai",
        execution_route=route.execution_route(),
    )._create_stt_service(object())

    assert service.settings.model == "gpt-realtime-whisper"
    assert service.settings.language == Language.DE
    assert service.turn_detection is False


def test_groq_factory_binds_frozen_model_and_openai_v1_endpoint(monkeypatch):
    class _Settings:
        def __init__(self, *, model, language, prompt):
            self.model = model
            self.language = language
            self.prompt = prompt

    class _GroqSTTService:
        Settings = _Settings

        def __init__(self, *, api_key, base_url, settings):
            self.api_key = api_key
            self.base_url = base_url
            self.settings = settings

    module = SimpleNamespace(GroqSTTService=_GroqSTTService)
    monkeypatch.setattr(Config, "GROQ_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Scriber, Pipecat")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)
    route = freeze_provider_route(workload="file", provider="groq")
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed")

    service = ScriberPipeline(
        service_name="groq",
        execution_route=route.execution_route(),
    )._create_stt_service(object())

    assert service.base_url == "https://api.groq.com/openai/v1"
    assert service.settings.model == "whisper-large-v3-turbo"
    assert service.settings.language == Language.DE
    assert service.settings.prompt == "Likely vocabulary: Scriber, Pipecat"

    unknown_model = route.execution_route()
    unknown_model["model"] = "unknown-groq-model"
    with pytest.raises(ValueError, match="no verified audio format capability"):
        ScriberPipeline(
            service_name="groq",
            execution_route=unknown_model,
        )._create_stt_service(object())


@pytest.mark.asyncio
async def test_installed_groq_pipecat_request_shape_uses_bound_model():
    from pipecat.services.groq.stt import GroqSTTService

    service = GroqSTTService(
        api_key="test-key",
        base_url="https://api.groq.com/openai/v1",
        settings=GroqSTTService.Settings(
            model="whisper-large-v3-turbo",
            language=Language.DE,
        ),
    )
    original_client = service._client
    assert str(original_client.base_url).rstrip("/") == "https://api.groq.com/openai/v1"
    await original_client.close()

    captured = {}

    class _Transcriptions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text="done")

    service._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=_Transcriptions()))
    response = await service._transcribe(b"wav-bytes")

    assert response.text == "done"
    assert captured["model"] == "whisper-large-v3-turbo"
    assert captured["file"] == ("audio.wav", b"wav-bytes", "audio/wav")


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
    route = freeze_provider_route(
        workload="file",
        provider="assemblyai_realtime",
        model="universal-3-5-pro",
        language="de-DE",
        custom_vocab="Scriber, Pipecat",
    )
    monkeypatch.setattr(Config, "ASSEMBLYAI_RT_MODEL", "changed-after-freeze")
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed")

    service = ScriberPipeline(
        service_name="assemblyai_realtime",
        enable_speaker_diarization=True,
        execution_route=route.execution_route(),
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

    with pytest.raises(RuntimeError, match="Pipecat 1.14.0"):
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
    route = freeze_provider_route(
        workload="file",
        provider="elevenlabs",
        language="de-DE",
        custom_vocab="Scriber, Pipecat",
    )
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed")

    service = ScriberPipeline(
        service_name="elevenlabs",
        execution_route=route.execution_route(),
    )._create_stt_service(object())

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
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Scriber, Pipecat")
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)
    route = freeze_provider_route(
        workload="file",
        provider="deepgram",
        model="nova-3",
        language="de-DE",
        custom_vocab="Scriber, Pipecat",
    )
    monkeypatch.setattr(Config, "DEEPGRAM_MODEL", "changed-after-freeze")
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed")

    service = ScriberPipeline(
        service_name="deepgram",
        execution_route=route.execution_route(),
    )._create_stt_service(object())

    assert service.sample_rate == Config.SAMPLE_RATE
    assert service.encoding == "linear16"
    assert service.channels == Config.CHANNELS
    assert service.settings.kwargs == {
        "model": "nova-3",
        "language": Language.DE,
        "keyterm": ["Scriber", "Pipecat"],
        "interim_results": True,
        "smart_format": True,
        "punctuate": True,
        "diarize": False,
    }

    service_with_speakers = ScriberPipeline(
        service_name="deepgram",
        enable_speaker_diarization=True,
        execution_route=route.execution_route(),
    )._create_stt_service(object())

    assert service_with_speakers.settings.kwargs == {
        "model": "nova-3",
        "language": Language.DE,
        "keyterm": ["Scriber", "Pipecat"],
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

        class TurnDetectionMode(StrEnum):
            EXTERNAL = "external"
            VAD = "vad"

        class AdditionalVocabEntry:
            def __init__(self, *, content):
                self.content = content

        def __init__(self, *, api_key, base_url, sample_rate, settings):
            self.api_key = api_key
            self.base_url = base_url
            self.sample_rate = sample_rate
            self.settings = settings

    module = type(
        "SpeechmaticsModule",
        (),
        {"SpeechmaticsSTTService": _SpeechmaticsSTTService},
    )

    monkeypatch.setattr(Config, "SPEECHMATICS_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Scriber, Pipecat")
    monkeypatch.delenv("SPEECHMATICS_RT_URL", raising=False)
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)
    realtime_url = "wss://eu2.rt.speechmatics.com/v2/agent"
    route = freeze_provider_route(
        workload="file",
        provider="speechmatics",
        model="linden-1",
        language="de-DE",
        custom_vocab="Scriber, Pipecat",
        provider_endpoint_sha256=hashlib.sha256(realtime_url.encode("utf-8")).hexdigest(),
    )
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed")

    service = ScriberPipeline(
        service_name="speechmatics",
        execution_route=route.execution_route(),
    )._create_stt_service(object())

    settings = dict(service.settings.kwargs)
    additional_vocab = settings.pop("additional_vocab")
    assert settings == {
        "model": "linden-1",
        "enable_diarization": False,
        "language": Language.DE,
        "turn_detection_mode": _SpeechmaticsSTTService.TurnDetectionMode.VAD,
        "speaker_active_format": "{text}",
    }
    assert [entry.content for entry in additional_vocab] == ["Scriber", "Pipecat"]
    assert service.base_url == realtime_url

    service_with_speakers = ScriberPipeline(
        service_name="speechmatics",
        enable_speaker_diarization=True,
        execution_route=route.execution_route(),
    )._create_stt_service(object())

    speaker_settings = dict(service_with_speakers.settings.kwargs)
    speaker_settings.pop("additional_vocab")
    assert speaker_settings == {
        "model": "linden-1",
        "enable_diarization": False,
        "language": Language.DE,
        "turn_detection_mode": _SpeechmaticsSTTService.TurnDetectionMode.VAD,
        "speaker_active_format": "{text}",
    }

    monkeypatch.setenv(
        "SPEECHMATICS_RT_URL",
        "wss://private.invalid/speechmatics/v2/agent",
    )
    with pytest.raises(RuntimeError, match="endpoint no longer matches"):
        ScriberPipeline(
            service_name="speechmatics",
            execution_route=route.execution_route(),
        )._create_stt_service(object())


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
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "")
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


@pytest.mark.asyncio
async def test_gladia_stop_delivers_the_last_utterance_after_an_earlier_final(monkeypatch):
    import json

    from pipecat.utils.asyncio.task_manager import TaskManager

    monkeypatch.setattr(Config, "GLADIA_API_KEY", "key")
    pipeline = ScriberPipeline(service_name="gladia")
    service = pipeline._create_stt_service(object())
    service._task_manager = TaskManager()
    # An earlier sentence was already finalized in this session.
    pipeline._final_transcription_received.set()

    class _Socket:
        state = State.OPEN

        def __init__(self):
            self.sent = []
            self._messages: asyncio.Queue = asyncio.Queue()

        async def send(self, message):
            self.sent.append(json.loads(message))
            # Gladia finalizes the remaining audio, then closes with 1000.
            self._messages.put_nowait(
                json.dumps(
                    {
                        "type": "transcript",
                        "data": {"is_final": True, "utterance": {"language": "de", "text": "der letzte Satz"}},
                    }
                )
            )
            self._messages.put_nowait(None)

        def __aiter__(self):
            return self

        async def __anext__(self):
            message = await self._messages.get()
            if message is None:
                self.state = State.CLOSED
                raise StopAsyncIteration
            return message

        async def close(self):
            self.state = State.CLOSED

    pushed = []

    async def _push_frame(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    reconnects = []

    async def _try_reconnect(**_kwargs):
        reconnects.append(True)
        return False

    monkeypatch.setattr(service, "push_frame", _push_frame)
    monkeypatch.setattr(service, "_try_reconnect", _try_reconnect)
    socket = _Socket()
    service._websocket = socket
    service._connection_active = True
    service._disconnecting = False
    service._receive_task = service.create_task(service._receive_task_handler(service._report_error))

    await asyncio.wait_for(service.stop(EndFrame()), timeout=5.0)

    assert socket.sent == [{"type": "stop_recording"}]
    assert [frame.text for frame in pushed if isinstance(frame, TranscriptionFrame)] == ["der letzte Satz"]
    assert reconnects == []
    assert service._receive_task is None


def test_speechmatics_factory_enables_batch_labeled_diarization(monkeypatch):
    class _Settings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _SpeechmaticsSTTService:
        Settings = _Settings

        class TurnDetectionMode(StrEnum):
            EXTERNAL = "external"
            VAD = "vad"

        class AdditionalVocabEntry:
            def __init__(self, *, content):
                self.content = content

        def __init__(self, *, api_key, base_url, sample_rate, settings):
            self.api_key = api_key
            self.base_url = base_url
            self.sample_rate = sample_rate
            self.settings = settings

    module = type(
        "SpeechmaticsModule",
        (),
        {"SpeechmaticsSTTService": _SpeechmaticsSTTService},
    )

    monkeypatch.setattr(Config, "SPEECHMATICS_API_KEY", "key")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "")
    monkeypatch.delenv("SPEECHMATICS_RT_URL", raising=False)
    monkeypatch.setattr("src.pipeline.import_provider_runtime_module", lambda *_args: module)

    service = ScriberPipeline(
        service_name="speechmatics",
        enable_speaker_diarization=True,
    )._create_stt_service(object())

    assert service.settings.kwargs == {
        "model": "linden-1",
        "enable_diarization": False,
        "language": Language.DE,
        "additional_vocab": [],
        "turn_detection_mode": _SpeechmaticsSTTService.TurnDetectionMode.VAD,
        "speaker_active_format": "{text}",
    }

    file_service = ScriberPipeline(
        service_name="speechmatics",
        enable_speaker_diarization=True,
    )._create_stt_service(object(), for_file=True)

    # Only diarized files carry speaker labels; Pipecat 1.14 applies the
    # template to every final, so plain dictation must keep "{text}".
    assert file_service.settings.kwargs["enable_diarization"] is True
    assert file_service.settings.kwargs["speaker_active_format"] == "[Speaker {speaker_id}]: {text}"


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
    assert (
        _live_recording_gate_needed(
            "azure_mai",
            segmented_service=False,
            vad_attached=needs_vad,
        )
        is True
    )

    pipeline = ScriberPipeline(service_name="azure_mai", on_status_change=None)
    pipeline.pipeline = _DummyRuntimePipelineGraph([SegmentedSTTRecordingGate(vad_segmentation_enabled=False)])

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

            self._mark_final_task = asyncio.create_task(_mark_final())
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
async def test_live_stop_accepts_a_commit_answered_without_new_text(monkeypatch):
    # Deepgram answers the stop's Finalize with an empty result when its
    # endpointing already finalized the last words; silence does the same.
    monkeypatch.setenv("SCRIBER_LIVE_STT_FINAL_FAILURE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False, raising=False)
    events: list[str] = []
    pipeline = ScriberPipeline(service_name="deepgram", on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _RecordingStopTask(pipeline._start_done, events)
    pipeline.audio_input = _SegmentedFinalizationAudioInput(events)

    class _AnsweringService:
        reports_commit_results = True

    service = _AnsweringService()

    class _CommittingGate(SegmentedSTTRecordingGate):
        async def flush_segment(self, *, direction=FrameDirection.DOWNSTREAM) -> bool:
            events.append("gate_commit")

            async def _answer():
                await asyncio.sleep(0.05)
                events.append("empty_answer")
                pipeline_module._provider_commit_results(service).settle()

            self._answer_task = asyncio.create_task(_answer())
            return True

    pipeline.pipeline = _DummyRuntimePipelineGraph(
        [
            _CommittingGate(vad_segmentation_enabled=False, stop_strategy=LIVE_STT_STOP_VAD_FLUSH_BEFORE_END),
            service,
        ]
    )
    # Answers to earlier commits must not satisfy the stop's commit.
    pipeline_module._provider_commit_results(service).settle()
    pipeline._mark_final_transcription_received()

    await pipeline.stop(timeout_secs=1.0)

    assert pipeline._terminal_error is None
    assert events.index("gate_commit") < events.index("empty_answer")
    assert events.index("empty_answer") < events.index("task_stop_when_done")


@pytest.mark.asyncio
async def test_async_vad_commit_timeout_is_a_provider_failure(monkeypatch):
    monkeypatch.setenv("SCRIBER_LIVE_STT_FINAL_FAILURE_TIMEOUT_SECONDS", "1")
    pipeline = ScriberPipeline(service_name="openai", on_status_change=None)
    pipeline.pipeline = _DummyRuntimePipelineGraph([])
    pipeline._wait_for_new_final_transcription_or_done = AsyncMock(return_value="timeout")

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
    pipeline.pipeline = _DummyRuntimePipelineGraph([SegmentedSTTRecordingGate(vad_segmentation_enabled=False)])
    pipeline._wait_for_new_final_transcription_or_done = AsyncMock(
        side_effect=AssertionError("terminal-buffered providers must not enter segmented wait")
    )

    async def _drain_provider_ingress(_audio_input):
        events.append("provider_ingress_drained")
        return True

    pipeline._await_provider_ingress_audio_drain = AsyncMock(side_effect=_drain_provider_ingress)

    await pipeline.stop(timeout_secs=1.0)

    pipeline._wait_for_new_final_transcription_or_done.assert_not_awaited()
    assert events.index("audio_stop") < events.index("task_stop_when_done")
    assert events.index("audio_stop") < events.index("provider_ingress_drained")
    assert events.index("provider_ingress_drained") < events.index("task_stop_when_done")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "azure_mai"])
async def test_capture_release_callback_precedes_provider_finalization_and_retires_prewarm(monkeypatch, provider):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    events: list[str] = []
    prewarm = _DummyPrewarmManager(events)
    counts_at_release = []

    async def _capture_stopped():
        assert pipeline.native_audio_stop_confirmed() is True
        assert pipeline.mic_prewarm_manager is None
        counts_at_release.append((prewarm.detach_calls, prewarm.resume_calls))
        events.append("capture_released")

    pipeline = ScriberPipeline(
        service_name=provider,
        mic_prewarm_manager=prewarm,
        on_capture_stopped=_capture_stopped,
    )
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _RecordingStopTask(pipeline._start_done, events)
    pipeline.audio_input = _SegmentedFinalizationAudioInput(events)
    pipeline.pipeline = _DummyRuntimePipelineGraph(
        [
            SegmentedSTTRecordingGate(
                vad_segmentation_enabled=False,
                stop_strategy=_live_stt_stop_strategy(provider),
            )
        ]
    )

    async def _drain(_audio_input):
        events.append("provider_ingress_drained")
        return True

    async def _flush():
        events.append("provider_finalization")
        return False

    pipeline._await_provider_ingress_audio_drain = AsyncMock(side_effect=_drain)
    pipeline._flush_segmented_stt_buffers = AsyncMock(side_effect=_flush)
    pipeline._flush_live_vad_finalization_turn = AsyncMock(side_effect=_flush)
    await pipeline.stop(timeout_secs=1.0)
    await pipeline._notify_capture_stopped()

    assert events.index("provider_ingress_drained") < events.index("capture_released")
    assert events.index("capture_released") < events.index("provider_finalization")
    assert events.count("capture_released") == 1
    assert counts_at_release == [(prewarm.detach_calls, prewarm.resume_calls)]


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [False, None])
async def test_unknown_native_stop_cannot_notify_capture_released(confirmation):
    callback = AsyncMock()
    manager = object()
    pipeline = ScriberPipeline(on_capture_stopped=callback, mic_prewarm_manager=manager)
    pipeline._native_audio_stop_confirmation = confirmation
    await pipeline._notify_capture_stopped()
    callback.assert_not_awaited()
    assert pipeline.mic_prewarm_manager is manager
    assert pipeline._capture_stopped_notified is False


@pytest.mark.asyncio
async def test_native_stop_without_successful_eos_drain_cannot_release_early():
    class _FailedDrainAudioInput:
        async def stop(self, _frame, *, close_stream):
            raise RuntimeError("rustFramePipeMissingEndOfStream")

        def native_audio_stop_confirmed(self):
            return True

    callback = AsyncMock()
    pipeline = ScriberPipeline(on_capture_stopped=callback)
    pipeline.audio_input = _FailedDrainAudioInput()
    await pipeline._cleanup_audio_input()
    assert pipeline.native_audio_stop_confirmed() is True
    await pipeline._notify_capture_stopped()
    callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "failure"),
    [("azure_mai", "missing_eos"), ("openai", "missing_eos"), ("openai", "downstream_timeout")],
)
async def test_capture_integrity_failure_aborts_provider_finalization(provider, failure):
    statuses = []
    callback = AsyncMock()
    pipeline = ScriberPipeline(service_name=provider, on_status_change=statuses.append, on_capture_stopped=callback)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _RecordingStopTask(pipeline._start_done, [])
    mic = MicrophoneInput()
    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device="default",
        shell_call=lambda *_args, **_kwargs: {"success": True, "payload": {"stopped": True}},
    )
    source.stream_id = "missing-eos"
    source._requires_terminal_eos = True
    source._end_of_stream_wait_seconds = 0
    mic._frame_source = source
    mic.stream = source.stream
    if failure == "downstream_timeout":
        source._end_of_stream_event.set()

        class StalledDownstreamQueue:
            async def join(self):
                raise TimeoutError

        mic._audio_in_queue = StalledDownstreamQueue()
    pipeline.audio_input = mic
    pipeline._native_audio_stop_confirmation = None
    pipeline.pipeline = _DummyRuntimePipelineGraph(
        [SegmentedSTTRecordingGate(vad_segmentation_enabled=False, stop_strategy=_live_stt_stop_strategy(provider))]
    )
    pipeline._await_provider_ingress_audio_drain = AsyncMock(return_value=True)
    pipeline._flush_segmented_stt_buffers = AsyncMock(return_value=False)
    pipeline._flush_live_vad_finalization_turn = AsyncMock(return_value=False)

    with pytest.raises(RuntimeError, match="Microphone audio capture did not finish cleanly"):
        await pipeline.stop(timeout_secs=0.1)

    assert pipeline.task.cancel_called is True
    assert pipeline.task.stop_when_done_called is False
    assert pipeline.native_audio_stop_confirmed() is True
    assert pipeline.audio_input is None
    assert statuses[-1] == "Error"
    callback.assert_not_awaited()
    pipeline._await_provider_ingress_audio_drain.assert_not_awaited()
    pipeline._flush_segmented_stt_buffers.assert_not_awaited()
    pipeline._flush_live_vad_finalization_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_closed_microphone_remains_available_for_stop_retry():
    calls = []

    def shell_call(command, payload, **_kwargs):
        calls.append((command, payload))
        if len(calls) <= 2:
            raise TimeoutError("stop response lost")
        source._end_of_stream_event.set()
        return {"success": True, "payload": {"stopped": True, "eosWritten": True}}

    source = RustPrototypeFrameSource(
        sample_rate=16000, target_channels=1, block_size=512, device="default", shell_call=shell_call
    )
    source.stream_id = "pending-stop"
    source._requires_terminal_eos = True
    mic = MicrophoneInput()
    mic._frame_source = source
    mic.stream = source.stream
    pipeline = ScriberPipeline(service_name="azure_mai")
    pipeline.audio_input = mic
    pipeline._native_audio_stop_confirmation = None

    await pipeline._cleanup_audio_input()

    assert pipeline.audio_input is mic
    assert mic.stream is None
    assert pipeline.native_audio_stop_confirmed() is False
    with pytest.raises(RuntimeError, match="Microphone audio capture did not finish cleanly"):
        await pipeline.stop()

    assert calls == [("audioCaptureStop", {"streamId": "pending-stop"})] * 3
    assert pipeline.native_audio_stop_confirmed() is True
    assert pipeline.audio_input is None


@pytest.mark.asyncio
async def test_pipeline_completion_surfaces_capture_cleanup_failure(monkeypatch):
    started = asyncio.Event()

    class FinishingMicrophone(FrameProcessor):
        def __init__(self, **_kwargs):
            super().__init__()

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if isinstance(frame, StartFrame):
                started.set()

        async def stop(self, _frame, *, close_stream):
            raise RuntimeError("rustFramePipeMissingEndOfStream")

        def native_audio_stop_confirmed(self):
            return True

    monkeypatch.setattr(pipeline_module, "MicrophoneInput", FinishingMicrophone)
    monkeypatch.setattr(pipeline_module, "_resolve_live_mic_capture_device", lambda _manager: ("default", False))
    monkeypatch.setattr(pipeline_module, "_analyzer_warmup_enabled", lambda: False)
    monkeypatch.setattr(Config, "MIC_AUTO_STOP_ENABLED", False)
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", False)

    class PassthroughProvider(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    pipeline = ScriberPipeline(service_name="azure_mai", text_injection_enabled=False)
    monkeypatch.setattr(pipeline, "_create_stt_service", lambda _session: PassthroughProvider())

    running = asyncio.create_task(pipeline.start())
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        await pipeline.task.stop_when_done()
        with pytest.raises(RuntimeError, match="Microphone audio capture did not finish cleanly"):
            await asyncio.wait_for(running, timeout=2.0)
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    assert pipeline._start_done.is_set()
    assert pipeline.native_audio_stop_confirmed() is True
    assert pipeline.audio_input is None


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

    segmented_stop = asyncio.create_task(pipeline._stop_audio_capture_for_segmented_finalization())
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
@pytest.mark.parametrize("native_stop_confirmed", [True, False, None])
async def test_cleanup_audio_input_preserves_native_stop_confirmation_after_cleanup_error(
    native_stop_confirmed,
):
    class _CleanupErrorAudioInput:
        async def stop(self, _frame, *, close_stream=None):
            assert close_stream is True
            raise RuntimeError("synthetic downstream cleanup failure")

        def native_audio_stop_confirmed(self):
            return native_stop_confirmed

    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    pipeline._native_audio_stop_confirmation = None
    audio_input = _CleanupErrorAudioInput()
    pipeline.audio_input = audio_input

    assert pipeline.native_audio_stop_confirmed() is native_stop_confirmed
    await pipeline._cleanup_audio_input()

    assert pipeline.audio_input is (None if native_stop_confirmed is True else audio_input)
    assert pipeline.native_audio_stop_confirmed() is native_stop_confirmed


@pytest.mark.asyncio
async def test_cleanup_audio_input_retains_private_provider_replay_capture_attestation():
    expected = {
        "source": "rust_audio_frame_pipe_reader",
        "eosObserved": True,
    }

    class _ReplayAudioInput(_DummyAudioInput):
        def provider_replay_capture_attestation(self):
            return dict(expected)

    pipeline = ScriberPipeline(
        service_name="azure_mai",
        on_status_change=None,
        azure_mai_raw_transport=lambda **_kwargs: None,
    )
    audio_input = _ReplayAudioInput()
    pipeline.audio_input = audio_input

    await pipeline._cleanup_audio_input()

    assert audio_input.close_stream is True
    assert pipeline.audio_input is None
    assert pipeline.provider_replay_capture_attestation() == expected
    returned = pipeline.provider_replay_capture_attestation()
    assert returned is not None
    returned["eosObserved"] = False
    assert pipeline.provider_replay_capture_attestation() == expected
    assert pipeline.stt_runtime_configuration()["sampleRateHz"] == 48_000


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
    assert (
        pipeline.ensure_audio_health(
            reason="test",
            max_callback_gap_seconds=3.0,
        )
        is True
    )
    assert audio_input.health_calls == [{"reason": "test", "max_callback_gap_seconds": 3.0}]


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
    assert runtime_pipeline.pushed == [(VADUserStoppedSpeakingFrame, FrameDirection.DOWNSTREAM)]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_frame", [EndFrame, StopFrame])
async def test_segmented_stt_gate_defaults_to_whole_recording_segment(terminal_frame):
    gate = SegmentedSTTRecordingGate(vad_segmentation_enabled=False)
    pushed = []

    async def _capture_push(frame, direction):
        pushed.append((type(frame), direction))

    gate.push_frame = _capture_push

    await gate.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(terminal_frame(), FrameDirection.DOWNSTREAM)

    assert pushed == [
        (VADUserStartedSpeakingFrame, FrameDirection.DOWNSTREAM),
        (VADUserStoppedSpeakingFrame, FrameDirection.DOWNSTREAM),
        (terminal_frame, FrameDirection.DOWNSTREAM),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("vad_enabled", [False, True])
async def test_segmented_stt_gate_cancel_never_commits_buffered_speech(vad_enabled):
    gate = SegmentedSTTRecordingGate(vad_segmentation_enabled=vad_enabled)
    gate.push_frame = AsyncMock()
    await gate.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(
        InputAudioRawFrame(audio=b"\x01\x00" * 1600, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    gate.push_frame.reset_mock()

    cancel = CancelFrame()
    await gate.process_frame(cancel, FrameDirection.DOWNSTREAM)

    # A VAD stop tells segmented providers to upload the buffered recording.
    # Cancellation must discard that boundary, including a later cleanup flush.
    gate.push_frame.assert_awaited_once_with(cancel, FrameDirection.DOWNSTREAM)
    assert await gate.flush_segment() is False
    gate.push_frame.assert_awaited_once_with(cancel, FrameDirection.DOWNSTREAM)


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
    processor = TranscriptionCallbackProcessor(lambda text, is_final: captured.append((text, is_final)))

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

    assert captured == [("[Speaker 1]: Hallo Welt\n\n[Speaker 2]: Antwort", True)]
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


@pytest.mark.asyncio
async def test_soniox_webm_encode_failure_logs_no_ffmpeg_stderr_paths(monkeypatch):
    from loguru import logger

    secret_path = "C:\\Users\\Private Person\\AppData\\Local\\Temp\\tmpabc.pcm"

    class FailedProcess:
        returncode = 1

    async def fake_exec(*_args, **_kwargs):
        return FailedProcess()

    async def fake_communicate(_proc, **_kwargs):
        return b"", f"{secret_path}: Invalid data found when processing input".encode()

    monkeypatch.setattr(pipeline_module, "find_media_tool", lambda _name: "ffmpeg")
    monkeypatch.setattr(pipeline_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(pipeline_module, "communicate_or_kill_on_cancel", fake_communicate)
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        processor = SonioxAsyncProcessor(api_key="test-key", session=object())
        encoded, content_type, filename = await processor._encode_audio(b"\0\0" * 320)
    finally:
        logger.remove(sink)

    assert (content_type, filename) == ("audio/wav", "audio.wav")
    assert encoded[:4] == b"RIFF"
    warnings = [message for message in messages if "WebM encode failed" in message]
    assert warnings and "exit code 1" in warnings[0]
    assert all("Private Person" not in message and "Invalid data" not in message for message in messages)
