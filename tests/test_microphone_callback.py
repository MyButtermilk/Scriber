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
            return {"capture_channels": 1, "device_index": 7}

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

    assert manager.callback is not None
    data = np.full((512, 1), 1000, dtype=np.int16)
    manager.callback(data, 512, None, None)
    for _ in range(10):
        if consumed:
            break
        await asyncio.sleep(0)

    assert consumed == [data.tobytes()]

    await mic.stop(EndFrame())

    assert manager.detached == 1
    assert mic._using_prewarm_stream is False
