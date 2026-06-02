import asyncio
import types

import numpy as np
import pytest

import src.microphone as microphone
from pipecat.frames.frames import EndFrame, StartFrame


@pytest.mark.skipif(not microphone.HAS_SOUNDDEVICE, reason="sounddevice unavailable")
@pytest.mark.asyncio
async def test_audio_callback_throttles_visualizer_work_to_sixty_hz(monkeypatch):
    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    levels: list[float] = []
    times = iter([100.0, 100.01, 100.02, 100.03, 100.04])

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
            item = await mic._queue.get()
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

    for _ in range(10):
        if consumed:
            break
        await asyncio.sleep(0)

    assert consumed == [np.full((512, 1), 250, dtype=np.int16).tobytes()]

    assert manager.callback is not None
    data = np.full((512, 1), 1000, dtype=np.int16)
    manager.callback(data, 512, None, None)
    for _ in range(10):
        if len(consumed) >= 2:
            break
        await asyncio.sleep(0)

    assert consumed == [
        np.full((512, 1), 250, dtype=np.int16).tobytes(),
        data.tobytes(),
    ]

    await mic.stop(EndFrame())

    assert manager.detached == 1
    assert mic._using_prewarm_stream is False


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
