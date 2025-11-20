import asyncio
import sounddevice as sd
import numpy as np
from loguru import logger
from pipecat.frames.frames import AudioRawFrame

try:
    from pipecat.transports.base_input import BaseInputTransport
    PARENT_CLASS = BaseInputTransport
except ImportError:
    # Fallback or handle if Pipecat structure is different
    # In some versions, it might be an InputAudioStream or similar.
    # Let's assume we need a processor that produces AudioRawFrames.
    from pipecat.processors.frame_processor import FrameProcessor
    PARENT_CLASS = FrameProcessor

# Since Pipecat 0.0.95, the structure is usually pipeline tasks reading from a source.
# A simple source is just an async generator or a processor that pushes frames.
# Let's implement a simple Microphone Source as a FrameProcessor/Transport.

class MicrophoneInput(PARENT_CLASS):
    def __init__(self, sample_rate=16000, channels=1, block_size=1024):
        super().__init__(params=None) if hasattr(PARENT_CLASS, "params") else super().__init__()
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = block_size
        self.stream = None
        self._running = False
        self._loop = None
        self._queue = asyncio.Queue()

    async def start(self, frame_processor):
        # This signature matches BaseInputTransport.start(frame_processor)
        self._loop = asyncio.get_running_loop()
        self._running = True

        try:
            self.stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                blocksize=self.block_size,
                dtype="int16",
                callback=self._audio_callback
            )
            self.stream.start()
            logger.info("Microphone stream started")

            while self._running:
                data = await self._queue.get()
                if data is None:
                    break
                frame = AudioRawFrame(audio=data, sample_rate=self.sample_rate, num_channels=self.channels)
                await frame_processor.process_frame(frame)

        except Exception as e:
            logger.error(f"Microphone error: {e}")
        finally:
            if self.stream:
                self.stream.stop()
                self.stream.close()

    def _audio_callback(self, indata, frames, time, status):
        if status:
            logger.warning(f"Audio status: {status}")
        if self._running:
            # indata is numpy array. Convert to bytes.
            self._loop.call_soon_threadsafe(self._queue.put_nowait, indata.tobytes())

    async def stop(self):
        self._running = False
        self._queue.put_nowait(None)
