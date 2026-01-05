import asyncio
import numpy as np
from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, StartFrame, EndFrame

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
except ImportError as exc:  # pragma: no cover - defensive fallback
    raise ImportError(
        "MicrophoneInput requires pipecat.transports.base_input.BaseInputTransport. "
        "Upgrade pipecat to a version that includes BaseInputTransport."
    ) from exc


class MicrophoneInput(BaseInputTransport):
    def __init__(
        self,
        sample_rate=16000,
        channels=1,
        block_size=1024,
        turn_analyzer=None,
        vad_analyzer=None,
        device="default",
        keep_alive=False,
        on_audio_level=None,
        on_ready=None,
    ):
        if not HAS_SOUNDDEVICE:
            raise RuntimeError("Sounddevice is not available, cannot use MicrophoneInput.")

        params = TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=sample_rate,
            audio_in_channels=channels,
            audio_in_passthrough=True,
            turn_analyzer=turn_analyzer,
            vad_analyzer=vad_analyzer,
        )
        super().__init__(params=params)
        self._target_sample_rate = sample_rate
        self._target_channels = channels
        self.block_size = block_size
        self.device = device
        self.keep_alive = keep_alive
        self.on_audio_level = on_audio_level
        self.on_ready = on_ready
        self.stream = None
        self._running = False
        self._loop = None
        self._queue = asyncio.Queue()
        self._consumer_task = None
        self._stopped = asyncio.Event()
        self._rms_callback_count = 0  # Throttle RMS calculations

    async def start(self, frame: StartFrame):
        """Start audio capture and feed frames into the transport queue."""
        logger.debug(f"MicrophoneInput.start() called, device={self.device}")
        await super().start(frame)
        self._loop = asyncio.get_running_loop()
        self._running = True

        try:
            # Define device_index at the outer scope so it's available for logging
            device_index = None if self.device == "default" else int(self.device)
            
            if not self.stream:
                # Auto-detect channels supported by the device to avoid PaErrorCode -9998.
                # Also validate the device exists and is an input device.
                try:
                    dev_info = sd.query_devices(device=device_index, kind='input')
                    max_channels = int(dev_info.get('max_input_channels', 0))
                    
                    if max_channels == 0:
                        # Device exists but has no input channels - fall back to default
                        raise ValueError(f"Device has no input channels")
                    chosen_channels = self._target_channels
                    if chosen_channels <= 0 or chosen_channels > max_channels:
                        chosen_channels = max_channels
                    if self._target_channels != chosen_channels:
                        logger.info(
                            f"Overriding configured channels {self._target_channels} with device-supported {chosen_channels}"
                        )
                        self._target_channels = chosen_channels

                except Exception as e:
                    if device_index is not None:
                        # Configured device failed - fall back to system default
                        logger.warning(f"Configured device {self.device} unavailable ({e}); falling back to default microphone")
                        device_index = None
                        # Query default device channels
                        try:
                            default_info = sd.query_devices(device=None, kind='input')
                            max_channels = int(default_info.get('max_input_channels', 1))
                            if max_channels > 0:
                                chosen_channels = self._target_channels
                                if chosen_channels <= 0 or chosen_channels > max_channels:
                                    chosen_channels = max_channels
                                self._target_channels = chosen_channels
                                logger.info(f"Using default device with {chosen_channels} channel(s)")
                        except Exception as e2:
                            logger.warning(f"Could not query default device ({e2}); using 1 channel")
                            self._target_channels = 1
                    else:
                        logger.warning(f"Could not query default device ({e}); using 1 channel")
                        self._target_channels = 1

                self.stream = sd.InputStream(
                    samplerate=self._target_sample_rate,
                    channels=self._target_channels,
                    blocksize=self.block_size,
                    dtype="int16",
                    callback=self._audio_callback,
                    device=device_index,
                )
            if not self.stream.active:
                self.stream.start()
            logger.info(f"Microphone stream started (device={'default' if device_index is None else device_index})")
            # Signal that microphone is ready and capturing audio
            if self.on_ready:
                try:
                    self.on_ready()
                except Exception as e:
                    logger.warning(f"on_ready callback error: {e}")
            # Ensure transport audio queue exists before we push frames
            self._create_audio_task()
            self._consumer_task = asyncio.create_task(self._drain_queue(), name="microphone_drain")
        except Exception as e:
            logger.error(f"Microphone error: {e}")
            await self.stop(frame=EndFrame())

    def _audio_callback(self, indata, frames, time, status):
        if status:
            logger.warning(f"Audio status: {status}")
        if self._running:
            # Directly use the buffer without extra processing
            audio_bytes = indata.tobytes()
            self._loop.call_soon_threadsafe(self._queue.put_nowait, audio_bytes)
            
            # Throttled RMS calculation for visualization (every 2nd callback = ~30fps at 1024 block size @ 16kHz)
            self._rms_callback_count += 1
            if self.on_audio_level and (self._rms_callback_count & 1) == 0:
                try:
                    # Optimized RMS: use int16 view directly, compute in float32 for speed
                    # indata is already int16 dtype, shape is (frames, channels)
                    samples = indata.view(np.int16).ravel()
                    # Use float32 for faster computation than float64
                    rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2)) / 32768.0
                    self.on_audio_level(float(rms))
                except Exception:
                    pass

    async def _drain_queue(self):
        # Ensure audio queue exists (BaseInputTransport creates it in _create_audio_task)
        if not hasattr(self, "_audio_in_queue") or self._audio_in_queue is None:
            self._create_audio_task()
        # Wait for queue to be available and start frame delivered downstream
        while not hasattr(self, "_audio_in_queue") or self._audio_in_queue is None:
            await asyncio.sleep(0.01)

        try:
            while self._running:
                try:
                    # Use timeout to allow checking _running flag periodically
                    data = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if data is None:
                    break
                frame = InputAudioRawFrame(
                    audio=data,
                    sample_rate=self._target_sample_rate,
                    num_channels=self._target_channels,
                )
                await self.push_audio_frame(frame)
        except asyncio.CancelledError:
            # Clean up audio stream on cancellation
            self._running = False
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            raise  # Re-raise to properly complete cancellation

    async def stop(self, frame: EndFrame):
        self._running = False
        
        # Stop the audio stream first
        if self.stream and not self.keep_alive:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        
        # Signal the queue to stop
        if self._queue:
            try:
                self._queue.put_nowait(None)
            except Exception:
                pass
        
        # Wait for consumer task with timeout
        if self._consumer_task:
            task, self._consumer_task = self._consumer_task, None
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        
        self._stopped.set()
        await super().stop(frame)

