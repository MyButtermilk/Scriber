import asyncio
import types

import numpy as np
import pytest

import src.microphone as microphone


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
