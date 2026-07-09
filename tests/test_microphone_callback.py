import asyncio
import types

import numpy as np
import pytest

import src.microphone as microphone


class _FakeStream:
    def __init__(self, *, active: bool = False):
        self.active = active
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0

    def start(self):
        self.start_calls += 1
        self.active = True

    def stop(self):
        self.stop_calls += 1
        self.active = False

    def close(self):
        self.close_calls += 1


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
@pytest.mark.asyncio
async def test_audio_callback_throttles_visualizer_work_to_sixty_hz(monkeypatch):
    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    levels: list[float] = []
    times = iter([99.99, 100.0, 100.005, 100.01, 100.015, 100.02, 100.025, 100.03])

    monkeypatch.setattr(microphone, "time", types.SimpleNamespace(monotonic=lambda: next(times)))

    mic.on_audio_level = levels.append
    mic._running = True
    mic._loop = asyncio.get_running_loop()

    data = np.full((512, 1), 1000, dtype=np.int16)
    mic._audio_callback(data, 512, None, None)
    mic._audio_callback(data, 512, None, None)
    mic._audio_callback(data, 512, None, None)
    mic._audio_callback(data, 512, None, None)
    mic._running = False
    await asyncio.sleep(0)

    assert mic._audio_level_interval == pytest.approx(1.0 / 60.0)
    assert len(levels) == 2
    assert mic._queue.qsize() == 4


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
def test_microphone_watchdog_restarts_inactive_direct_stream():
    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    stream = _FakeStream(active=False)
    mic.stream = stream
    mic._running = True

    assert mic.ensure_stream_health(reason="test", max_callback_gap_seconds=15.0) is True

    assert stream.start_calls == 1
    assert stream.active is True
    assert mic.diagnostic_snapshot()["streamActive"] is True

    mic.force_stop_from_external_error(reason="test_cleanup")


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
def test_microphone_watchdog_restarts_stale_direct_stream(monkeypatch):
    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    stream = _FakeStream(active=True)
    mic.stream = stream
    mic._running = True
    mic._last_callback_at = 100.0

    monkeypatch.setattr(microphone.time, "monotonic", lambda: 120.0)

    assert mic.ensure_stream_health(reason="test", max_callback_gap_seconds=10.0) is True

    assert stream.stop_calls == 1
    assert stream.start_calls == 1
    assert stream.active is True

    mic.force_stop_from_external_error(reason="test_cleanup")


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
def test_microphone_watchdog_restarts_direct_stream_when_callbacks_never_arrive(monkeypatch):
    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    stream = _FakeStream(active=True)
    mic.stream = stream
    mic._running = True
    mic._stream_started_at = 100.0
    mic._last_callback_at = 0.0

    monkeypatch.setattr(microphone.time, "monotonic", lambda: 120.0)

    assert mic.ensure_stream_health(reason="test", max_callback_gap_seconds=10.0) is True

    assert stream.stop_calls == 1
    assert stream.start_calls == 1
    assert stream.active is True

    mic.force_stop_from_external_error(reason="test_cleanup")


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
def test_microphone_watchdog_reports_stale_stream_when_restart_is_throttled(monkeypatch):
    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    stream = _FakeStream(active=True)
    mic.stream = stream
    mic._running = True
    mic._last_callback_at = 100.0
    mic._last_health_restart_at = 119.0

    monkeypatch.setattr(microphone.time, "monotonic", lambda: 120.0)

    assert (
        mic.ensure_stream_health(
            reason="test",
            max_callback_gap_seconds=10.0,
            min_restart_interval_seconds=15.0,
        )
        is False
    )

    assert stream.stop_calls == 0
    assert stream.start_calls == 0
    snapshot = mic.diagnostic_snapshot()
    assert snapshot["streamActive"] is True
    assert snapshot["healthRestartCount"] == 0
    assert snapshot["healthRestartThrottleCount"] == 1
    assert snapshot["lastHealthCheckReason"] == "test"
    assert snapshot["lastHealthFailureReason"] == "staleCallbacks"
    assert snapshot["lastHealthRestartThrottledReason"] == "test:staleCallbacks"
    assert snapshot["lastHealthRestartThrottleRemainingSeconds"] == pytest.approx(14.0)

    mic.force_stop_from_external_error(reason="test_cleanup")


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
def test_microphone_watchdog_reopens_inactive_owned_frame_source():
    class _FakeOwnedFrameSource:
        engine = "rust-wasapi"
        name = "rust-frame-pipe"
        target_channels = 1
        capture_channels = 1
        fallback_reason = ""

        def __init__(self):
            self.stream = _FakeStream(active=False)
            self.stop_calls: list[bool] = []
            self.start_calls = 0

        def stop(self, *, close: bool):
            self.stop_calls.append(close)
            self.stream.active = False

        def start(self):
            self.start_calls += 1
            self.stream.active = True

        def diagnostic_snapshot(self):
            return {
                "engine": self.engine,
                "frameSource": self.name,
                "hasStream": True,
                "streamActive": self.stream.active,
            }

    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    source = _FakeOwnedFrameSource()
    mic._frame_source = source
    mic.stream = source.stream
    mic._audio_engine = source.engine
    mic._frame_source_name = source.name
    mic._running = True

    assert (
        mic.ensure_stream_health(
            reason="rust_reader_closed",
            max_callback_gap_seconds=10.0,
            min_restart_interval_seconds=0.0,
        )
        is True
    )

    assert source.stop_calls == [False]
    assert source.start_calls == 1
    snapshot = mic.diagnostic_snapshot()
    assert snapshot["engine"] == "rust-wasapi"
    assert snapshot["streamActive"] is True
    assert snapshot["healthRestartCount"] == 1
    assert snapshot["lastHealthCheckReason"] == "rust_reader_closed"
    assert snapshot["lastHealthRestartReason"] == "rust_reader_closed"
    assert snapshot["lastHealthRestartError"] == ""


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
def test_microphone_watchdog_opens_rust_fallback_circuit_for_next_session(monkeypatch):
    class _FakeRustFrameSource:
        engine = "rust-wasapi"
        name = "rust-frame-pipe"
        target_channels = 1
        capture_channels = 1
        fallback_reason = ""
        mid_session_failure_reason = "pipeClosed"

        def __init__(self):
            self.stream = _FakeStream(active=False)
            self.stop_calls: list[bool] = []
            self.start_calls = 0

        def stop(self, *, close: bool):
            self.stop_calls.append(close)
            self.stream.active = False

        def start(self):
            self.start_calls += 1
            self.stream.active = True

        def diagnostic_snapshot(self):
            return {
                "engine": self.engine,
                "frameSource": self.name,
                "hasStream": True,
                "streamActive": self.stream.active,
                "midSessionFailureReason": self.mid_session_failure_reason,
            }

    microphone._reset_rust_audio_fallback_circuit()
    monkeypatch.setenv("SCRIBER_RUST_AUDIO_FAILURE_COOLDOWN_SEC", "30")
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    monkeypatch.setattr(microphone.time, "monotonic", lambda: 100.0)

    try:
        mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
        source = _FakeRustFrameSource()
        mic._frame_source = source
        mic.stream = source.stream
        mic._audio_engine = source.engine
        mic._frame_source_name = source.name
        mic._running = True

        assert (
            mic.ensure_stream_health(
                reason="watchdog",
                max_callback_gap_seconds=10.0,
                min_restart_interval_seconds=0.0,
            )
            is True
        )

        snapshot = mic.diagnostic_snapshot()
        assert source.stop_calls == [False]
        assert source.start_calls == 1
        assert snapshot["lastRustAudioMidSessionFailureReason"] == "pipeClosed"
        assert snapshot["engineFallbackReason"] == "rustWasapiMidSessionFailure:pipeClosed"
        assert snapshot["rustAudioFallbackCircuitOpen"] is True
        assert snapshot["rustAudioFallbackCircuitReason"] == "pipeClosed"
        assert snapshot["rustAudioFallbackCircuitRemainingSeconds"] == pytest.approx(30.0)

        next_mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
        with pytest.raises(RuntimeError, match="fallback circuit is open"):
            next_mic._create_frame_source()
        assert next_mic._audio_engine_fallback_reason == "rustCircuitOpen:pipeClosed"
    finally:
        microphone._reset_rust_audio_fallback_circuit()


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
@pytest.mark.asyncio
async def test_microphone_drain_notifies_after_last_audio_chunk(monkeypatch):
    markers: list[str] = []
    pushed: list[bytes] = []
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        on_last_audio_chunk_sent=lambda: markers.append("last_chunk_sent"),
    )

    mic._audio_in_queue = object()
    mic._running = False
    mic._queue.put_nowait(b"audio")

    async def fake_push_audio_frame(frame):
        pushed.append(frame.audio)

    mic.push_audio_frame = fake_push_audio_frame

    await mic._drain_queue()

    assert pushed == [b"audio"]
    assert markers == ["last_chunk_sent"]


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
@pytest.mark.asyncio
async def test_microphone_drain_keeps_up_with_sustained_callback_flow():
    pushed: list[bytes] = []
    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    mic._audio_in_queue = object()
    mic._running = True
    mic._consumer_loop = asyncio.get_running_loop()
    mic._queue_wakeup = asyncio.Event()

    async def fake_push_audio_frame(frame):
        pushed.append(frame.audio)

    mic.push_audio_frame = fake_push_audio_frame
    consumer = asyncio.create_task(mic._drain_queue())
    data = np.full((512, 1), 1000, dtype=np.int16)

    for index in range(1024):
        mic._audio_callback(data, 512, None, None)
        if index % 8 == 0:
            await asyncio.sleep(0)

    mic._running = False
    mic._signal_queue_wakeup()
    await asyncio.wait_for(consumer, timeout=1.0)

    snapshot = mic.diagnostic_snapshot()
    assert len(pushed) == 1024
    assert snapshot["droppedFrameCount"] == 0
    assert snapshot["drainedFrameCount"] == 1024
    assert snapshot["audioQueueDepth"] == 0
    assert snapshot["audioQueueMaxDepth"] < snapshot["audioQueueCapacity"]


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
@pytest.mark.asyncio
async def test_microphone_consumer_failure_is_visible_in_diagnostics():
    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    mic._audio_in_queue = object()
    mic._running = True
    mic._consumer_loop = asyncio.get_running_loop()
    mic._queue_wakeup = asyncio.Event()

    async def failing_push_audio_frame(_frame):
        raise RuntimeError("synthetic consumer failure")

    mic.push_audio_frame = failing_push_audio_frame
    task = asyncio.create_task(mic._drain_queue())
    mic._consumer_task = task
    task.add_done_callback(mic._on_consumer_task_done)
    mic._queue.put_nowait(b"audio")
    mic._signal_queue_wakeup()

    with pytest.raises(RuntimeError, match="synthetic consumer failure"):
        await task
    await asyncio.sleep(0)

    snapshot = mic.diagnostic_snapshot()
    assert snapshot["consumerTaskState"] == "failed"
    assert snapshot["consumerError"] == "RuntimeError: synthetic consumer failure"
