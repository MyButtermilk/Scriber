import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import EndFrame, InputAudioRawFrame, StartFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

import src.pipeline as pipeline_module
from src.config import Config
from src.mic_silence_stop import MicSilenceStopObserver
from src.pipeline import ScriberPipeline, _ordered_live_pipeline_steps


@pytest.fixture(autouse=True)
def _isolate_framework_runner(monkeypatch):
    # Unit tests drive the observer directly; the real worker lifecycle is
    # separately exercised by test_pipeline_worker_silence_stop_preserves_audio.
    monkeypatch.setattr(FrameProcessor, "process_frame", AsyncMock())


def _observer(*, seconds=1, analyzer=None):
    clock = SimpleNamespace(now=0.0)
    analyzer = analyzer or SimpleNamespace(
        set_sample_rate=Mock(),
        analyze_audio=AsyncMock(return_value=VADState.QUIET),
        cleanup=AsyncMock(),
    )
    callback = Mock()
    observer = MicSilenceStopObserver(
        analyzer=analyzer,
        silence_seconds=seconds,
        on_timeout=callback,
        clock=lambda: clock.now,
    )
    observer.push_frame = AsyncMock()
    return observer, analyzer, callback, clock


async def _audio(observer, clock, *, duration=0.032):
    clock.now += duration
    frame = InputAudioRawFrame(audio=bytes(round(16_000 * duration) * 2), sample_rate=16_000, num_channels=1)
    await observer.process_frame(frame, FrameDirection.DOWNSTREAM)
    return frame


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [1, 5, 10])
async def test_silence_timeout_uses_audio_without_transcripts_and_fires_once(seconds):
    observer, analyzer, callback, clock = _observer(seconds=seconds)
    await observer.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    for _ in range(seconds * 10):
        await _audio(observer, clock, duration=0.1)
    callback.assert_not_called()
    await _audio(observer, clock, duration=0.2)
    callback.assert_called_once_with()
    count = analyzer.analyze_audio.await_count
    for _ in range(20):
        await _audio(observer, clock)
    assert analyzer.analyze_audio.await_count == count
    callback.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("speech_state", [VADState.STARTING, VADState.SPEAKING, VADState.STOPPING])
async def test_fresh_speech_resets_entire_silence_interval(speech_state):
    observer, analyzer, callback, clock = _observer()
    await observer.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    for _ in range(9):
        await _audio(observer, clock, duration=0.1)
    analyzer.analyze_audio.return_value = speech_state
    await _audio(observer, clock, duration=0.1)
    analyzer.analyze_audio.return_value = VADState.QUIET
    for _ in range(9):
        await _audio(observer, clock, duration=0.1)
    callback.assert_not_called()
    for _ in range(3):
        await _audio(observer, clock, duration=0.1)
    callback.assert_called_once_with()


@pytest.mark.asyncio
async def test_buffered_prewarm_audio_and_device_stalls_do_not_expire_silence():
    observer, _analyzer, callback, clock = _observer()
    await observer.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    frame = InputAudioRawFrame(audio=bytes(32_000), sample_rate=16_000, num_channels=1)
    for _ in range(3):
        await observer.process_frame(frame, FrameDirection.DOWNSTREAM)
    callback.assert_not_called()
    clock.now = 10.0
    await observer.process_frame(
        TranscriptionFrame(text="delayed final", user_id="", timestamp=""), FrameDirection.DOWNSTREAM
    )
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_all_original_frames_pass_through_without_provider_turn_events():
    observer, _analyzer, callback, clock = _observer()
    start = StartFrame()
    await observer.process_frame(start, FrameDirection.DOWNSTREAM)
    audio = await _audio(observer, clock)
    end = EndFrame()
    await observer.process_frame(end, FrameDirection.DOWNSTREAM)
    await _audio(observer, clock, duration=2)
    forwarded = [call.args[0] for call in observer.push_frame.await_args_list]
    assert forwarded[:3] == [start, audio, end]
    assert len(forwarded) == 4
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_analyzer_failure_preserves_audio_and_disables_automatic_stop():
    observer, analyzer, callback, clock = _observer()
    analyzer.analyze_audio.side_effect = RuntimeError("model failed")
    await observer.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    failed_audio = await _audio(observer, clock)
    await _audio(observer, clock, duration=2)
    analyzer.analyze_audio.assert_awaited_once()
    assert observer.push_frame.await_args_list[1].args[0] is failed_audio
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_stop_during_analysis_suppresses_pending_timeout():
    entered, release = asyncio.Event(), asyncio.Event()
    observer, analyzer, callback, clock = _observer()
    await observer.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    await _audio(observer, clock, duration=1)

    async def _pending(_audio):
        entered.set()
        await release.wait()
        return VADState.QUIET

    analyzer.analyze_audio.side_effect = _pending
    frame_task = asyncio.create_task(_audio(observer, clock, duration=1))
    await entered.wait()
    observer.disarm()
    release.set()
    await frame_task
    callback.assert_not_called()


@pytest.mark.parametrize("seconds", [0, 11, 1.5, True, "5"])
def test_invalid_timeout_rejected(seconds):
    with pytest.raises(ValueError, match="integer from 1 to 10"):
        _observer(seconds=seconds)


@pytest.mark.parametrize("enabled,has_callback", [(False, False), (False, True), (True, False)])
def test_disabled_or_non_live_pipeline_does_not_create_analyzer(monkeypatch, enabled, has_callback):
    monkeypatch.setattr(Config, "MIC_AUTO_STOP_ENABLED", enabled)
    factory = Mock(side_effect=AssertionError("disabled silence stop must not create a model"))
    monkeypatch.setattr(pipeline_module, "_create_vad_analyzer", factory)
    pipeline = ScriberPipeline(on_silence_timeout=Mock() if has_callback else None)
    assert pipeline._create_silence_stop_observer() is None
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["soniox", "azure_mai", "modulate", "openai", "onnx_local"])
async def test_observer_is_provider_independent_and_before_provider(monkeypatch, provider):
    monkeypatch.setattr(Config, "MIC_AUTO_STOP_ENABLED", True)
    monkeypatch.setattr(Config, "MIC_AUTO_STOP_SILENCE_SECONDS", 5)
    analyzer = SimpleNamespace(cleanup=AsyncMock())
    monkeypatch.setattr(pipeline_module, "_create_vad_analyzer", Mock(return_value=analyzer))
    observer = ScriberPipeline(service_name=provider, on_silence_timeout=Mock())._create_silence_stop_observer()
    input_step, provider_step = Mock(), Mock()
    steps = _ordered_live_pipeline_steps(
        audio_input=input_step,
        silence_stop_observer=observer,
        vad_processor=None,
        vad_observer=None,
        segmented_gate=None,
        stt_service=provider_step,
        smart_turn_processor=None,
        error_handler=Mock(),
        transcript_callback=None,
        text_injector=Mock(),
    )
    assert steps[:3] == [input_step, observer, provider_step]
    await observer.cleanup()
    analyzer.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_actual_silero_classifies_loud_non_speech_noise_as_silence():
    # An RMS-only detector would keep this loud fan-like noise active forever.
    analyzer = pipeline_module._create_vad_analyzer(quiet_mic=True)
    if analyzer is None:
        pytest.skip("bundled Silero VAD unavailable")
    observer, _analyzer, callback, clock = _observer(analyzer=analyzer)
    await observer.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    rng = np.random.default_rng(44)
    for _ in range(100):
        clock.now += 0.032
        pcm = rng.normal(0, 1800, 512).clip(-32768, 32767).astype("<i2").tobytes()
        await observer.process_frame(
            InputAudioRawFrame(audio=pcm, sample_rate=16_000, num_channels=1), FrameDirection.DOWNSTREAM
        )
    callback.assert_called_once_with()
    await observer.cleanup()
