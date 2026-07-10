import asyncio
import threading

import pytest

from src.runtime.audio_spool import SPOOL_MEMORY_LIMIT_BYTES, append_pcm_frame


@pytest.mark.asyncio
async def test_append_pcm_frame_rolls_memory_spool_off_event_loop():
    event_loop_thread = threading.get_ident()

    class RecordingSpool:
        def __init__(self):
            self.rollover_thread = None
            self.write_thread = None
            self.written = b""

        def rollover(self):
            self.rollover_thread = threading.get_ident()

        def write(self, data):
            self.write_thread = threading.get_ident()
            self.written += data

    spool = RecordingSpool()
    size = await append_pcm_frame(spool, SPOOL_MEMORY_LIMIT_BYTES, b"frame")

    assert size == SPOOL_MEMORY_LIMIT_BYTES + len(b"frame")
    assert spool.rollover_thread is not None
    assert spool.rollover_thread != event_loop_thread
    assert spool.write_thread == event_loop_thread
    assert spool.written == b"frame"


@pytest.mark.asyncio
async def test_append_pcm_frame_keeps_small_spool_in_memory():
    class RecordingSpool:
        def __init__(self):
            self.rollovers = 0
            self.written = b""

        def rollover(self):
            self.rollovers += 1

        def write(self, data):
            self.written += data

    spool = RecordingSpool()
    size = await append_pcm_frame(spool, 100, b"frame")
    await asyncio.sleep(0)

    assert size == 105
    assert spool.rollovers == 0
    assert spool.written == b"frame"
