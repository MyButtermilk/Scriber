import asyncio
import queue
import types

import numpy as np
import pytest

import src.microphone as microphone
from pipecat.frames.frames import EndFrame, StartFrame


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
@pytest.mark.asyncio
async def test_microphone_input_adopts_prewarmed_stream(monkeypatch):
    class _FakePrewarmManager:
        def __init__(self):
            self.callback = None
            self.attached = 0
            self.detached = 0
            self.paused = 0

        def attach_active_capture(self, callback, **_kwargs):
            self.callback = callback
            self.attached += 1
            return {
                "capture_channels": 1,
                "device_index": 7,
                "prebuffer_frames": [(np.full((512, 1), 250, dtype=np.int16), 512, {"pre": 1}, None)],
                "prebuffer_ms": 32.0,
            }

        def pause_for_active_capture(self):
            self.paused += 1

        def detach_active_capture(self, callback=None):
            self.detached += 1
            if callback is not None:
                assert callback is self.callback
            self.callback = None
            return True

    fake_sd = types.SimpleNamespace(
        InputStream=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("prewarmed stream adoption should not open InputStream")
        )
    )
    monkeypatch.setattr(microphone, "sd", fake_sd)

    manager = _FakePrewarmManager()
    ready = []
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        keep_alive=True,
        prewarm_manager=manager,
        on_ready=lambda: ready.append(True),
    )
    consumed = []

    def fake_create_audio_task():
        return None

    async def fake_drain_queue():
        while True:
            try:
                item = await asyncio.to_thread(mic._queue.get, True, 0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            consumed.append(item)

    mic._create_audio_task = fake_create_audio_task
    mic._drain_queue = fake_drain_queue

    await mic.start(StartFrame())

    assert manager.attached == 1
    assert manager.paused == 0
    assert mic._using_prewarm_stream is True
    assert ready == [True]

    for _ in range(50):
        if consumed:
            break
        await asyncio.sleep(0.01)

    assert consumed == [np.full((512, 1), 250, dtype=np.int16).tobytes()]

    assert manager.callback is not None
    data = np.full((512, 1), 1000, dtype=np.int16)
    manager.callback(data, 512, None, None)
    for _ in range(50):
        if len(consumed) >= 2:
            break
        await asyncio.sleep(0.01)

    assert consumed == [
        np.full((512, 1), 250, dtype=np.int16).tobytes(),
        data.tobytes(),
    ]

    await mic.stop(EndFrame())

    assert manager.detached == 1
    assert mic._using_prewarm_stream is False


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
        engine = "rust-prototype"
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
    assert snapshot["engine"] == "rust-prototype"
    assert snapshot["streamActive"] is True
    assert snapshot["healthRestartCount"] == 1
    assert snapshot["lastHealthCheckReason"] == "rust_reader_closed"
    assert snapshot["lastHealthRestartReason"] == "rust_reader_closed"
    assert snapshot["lastHealthRestartError"] == ""


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
def test_microphone_external_error_detaches_adopted_prewarm_stream():
    class _FakePrewarmManager:
        def __init__(self):
            self.detached = 0

        def detach_active_capture(self, callback=None):
            self.detached += 1
            return True

    manager = _FakePrewarmManager()
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        keep_alive=True,
        prewarm_manager=manager,
    )
    mic._running = True
    mic._using_prewarm_stream = True
    mic._prewarm_callback = object()

    mic.force_stop_from_external_error(reason="provider_connection_error")

    assert mic._running is False
    assert mic._using_prewarm_stream is False
    assert manager.detached == 1


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
