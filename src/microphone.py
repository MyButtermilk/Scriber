import asyncio
from loguru import logger
from pipecat.frames.frames import AudioRawFrame

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except Exception:
    sd = None
    HAS_SOUNDDEVICE = False
    logger.warning("Sounddevice not available. Microphone input will be disabled.")

try:
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.base_input import BaseInputTransport
    PARENT_CLASS = BaseInputTransport
    PARENT_NEEDS_PARAMS = True
except ImportError:
    from pipecat.processors.frame_processor import FrameProcessor
    PARENT_CLASS = FrameProcessor
    PARENT_NEEDS_PARAMS = False


class MicrophoneInput(PARENT_CLASS):
    def __init__(self, sample_rate=16000, channels=1, block_size=1024, turn_analyzer=None):
        if not HAS_SOUNDDEVICE:
            raise RuntimeError("Sounddevice is not available, cannot use MicrophoneInput.")

        # Some pipecat versions require an explicit TransportParams on BaseInputTransport.
        if PARENT_NEEDS_PARAMS:
            params = TransportParams(
                audio_in_enabled=True,
                audio_in_sample_rate=sample_rate,
                audio_in_channels=channels,
                audio_in_passthrough=True,
                turn_analyzer=turn_analyzer,
            )
            super().__init__(params=params)
        else:
            super().__init__()
        self._target_sample_rate = sample_rate  # avoid clashing with BaseInputTransport.sample_rate property
        self._target_channels = channels
        self.block_size = block_size
        self.stream = None
        self._running = False
        self._loop = None
        self._queue = asyncio.Queue()

    async def start(self, frame_processor):
        self._loop = asyncio.get_running_loop()
        self._running = True

        try:
            self.stream = sd.InputStream(
                samplerate=self._target_sample_rate,
                channels=self._target_channels,
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
                frame = AudioRawFrame(audio=data, sample_rate=self._target_sample_rate, num_channels=self._target_channels)
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
            self._loop.call_soon_threadsafe(self._queue.put_nowait, indata.tobytes())

    async def stop(self):
        self._running = False
        if self._queue:
            self._queue.put_nowait(None)
