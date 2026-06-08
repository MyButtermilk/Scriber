import asyncio
import time
import numpy as np
from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, StartFrame, EndFrame

from src.device_monitor import get_device_guard_lock, mark_stream_started, mark_stream_stopped

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


def _select_best_mono_channel(
    indata: np.ndarray,
    previous_channel: int | None = None,
) -> tuple[np.ndarray, int | None]:
    """Pick the strongest channel and convert multichannel input to mono int16.

    Some Windows endpoints expose a silent/noisy channel next to the real mic
    signal. Averaging channels can cancel speech (phase issues), so we keep the
    strongest channel with mild hysteresis to avoid frequent channel flips.
    """
    if not isinstance(indata, np.ndarray) or indata.ndim != 2 or indata.shape[1] <= 1:
        return indata, previous_channel

    try:
        channel_energy = np.mean(np.square(indata.astype(np.float32)), axis=0)
    except Exception:
        channel_energy = np.zeros(indata.shape[1], dtype=np.float32)

    best_channel = int(np.argmax(channel_energy))
    chosen_channel = best_channel

    if previous_channel is not None and 0 <= previous_channel < indata.shape[1]:
        prev_energy = float(channel_energy[previous_channel])
        best_energy = float(channel_energy[best_channel])
        # Keep previous channel unless another one is clearly stronger.
        if prev_energy > 0.0 and best_energy < (prev_energy * 1.35):
            chosen_channel = previous_channel

    mono = indata[:, chosen_channel]
    if mono.dtype != np.int16:
        mono = np.clip(mono, -32768, 32767).astype(np.int16)
    else:
        mono = np.ascontiguousarray(mono)

    return mono.reshape(-1, 1), chosen_channel


def _determine_capture_channels(output_channels: int, max_channels: int) -> int:
    """Choose capture channel count for robust mono transcription.

    For mono output we still capture several channels on mic arrays, then pick
    the strongest channel in the callback. This avoids silent-channel devices.
    """
    safe_max = max(1, int(max_channels))
    safe_output = max(1, min(int(output_channels), safe_max))
    if safe_output == 1 and safe_max >= 2:
        return min(8, safe_max)
    return safe_output


class MicrophoneInput(BaseInputTransport):
    def __init__(
        self,
        sample_rate=16000,
        channels=1,
        block_size=512,
        turn_analyzer=None,
        vad_analyzer=None,
        device="default",
        keep_alive=False,
        prewarm_manager=None,
        on_audio_level=None,
        on_ready=None,
        on_last_audio_chunk_sent=None,
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
        self._capture_channels = channels
        self.block_size = block_size
        self.device = device
        self.keep_alive = keep_alive
        self.prewarm_manager = prewarm_manager
        self.on_audio_level = on_audio_level
        self.on_ready = on_ready
        self.on_last_audio_chunk_sent = on_last_audio_chunk_sent
        self.stream = None
        self._using_prewarm_stream = False
        self._prewarm_callback = None
        self._running = False
        self._loop = None
        self._queue = asyncio.Queue()
        self._consumer_task = None
        self._stopped = asyncio.Event()
        # Visualizer gating (reduce noise-triggered movement)
        self._noise_floor_db = -70.0
        self._speech_active = False
        self._speech_hold_until = 0.0
        self._visual_level = 0.0
        self._active_capture_channel = None
        self._channel_selection_counter = 0
        self._channel_selection_interval_frames = 10
        self._last_audio_level_at = 0.0
        self._audio_level_interval = 1.0 / 60.0
        self._stream_claimed = False
        self._last_audio_chunk_sent_notified = False
        self._callback_count = 0
        self._last_callback_at = 0.0
        self._last_status = ""
        self._last_callback_exception = ""
        self._last_health_restart_at = 0.0

    def _claim_active_stream(self) -> None:
        if self._stream_claimed:
            return
        mark_stream_started()
        self._stream_claimed = True

    def _release_active_stream(self) -> None:
        if not self._stream_claimed:
            return
        mark_stream_stopped()
        self._stream_claimed = False

    async def start(self, frame: StartFrame):
        """Start audio capture and feed frames into the transport queue."""
        logger.debug(f"MicrophoneInput.start() called, device={self.device}")
        await super().start(frame)
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._active_capture_channel = None
        self._channel_selection_counter = 0
        self._last_audio_level_at = 0.0
        self._last_audio_chunk_sent_notified = False
        self._callback_count = 0
        self._last_callback_at = 0.0
        self._last_status = ""
        self._last_callback_exception = ""
        self._last_health_restart_at = 0.0
        self._create_audio_task()
        self._consumer_task = asyncio.create_task(self._drain_queue(), name="microphone_drain")

        try:
            if self.keep_alive and self.prewarm_manager is not None:
                adopted = None
                self._prewarm_callback = self._audio_callback
                try:
                    adopted = self.prewarm_manager.attach_active_capture(
                        self._prewarm_callback,
                        sample_rate=self._target_sample_rate,
                        target_channels=self._target_channels,
                        block_size=self.block_size,
                        device=self.device,
                    )
                except Exception as exc:
                    logger.debug(f"Could not attach prewarmed microphone stream: {exc}")

                if adopted:
                    self._capture_channels = int(
                        adopted.get("capture_channels") or self._target_channels
                    )
                    self._using_prewarm_stream = True
                    self._prepend_prewarm_audio(adopted)
                    logger.info(
                        "Microphone prewarm stream adopted "
                        f"(device={'default' if adopted.get('device_index') is None else adopted.get('device_index')}, "
                        f"prebuffer={float(adopted.get('prebuffer_ms') or 0.0):.1f} ms)"
                    )
                    if self.on_ready:
                        try:
                            self.on_ready()
                        except Exception as e:
                            logger.warning(f"on_ready callback error: {e}")
                    return

                try:
                    self.prewarm_manager.pause_for_active_capture()
                except Exception as exc:
                    logger.debug(f"Could not pause idle mic prewarm before fallback capture: {exc}")
                self._prewarm_callback = None

            # Device enumeration/open is guarded against concurrent PortAudio refresh.
            with get_device_guard_lock():
                # Define device_index at the outer scope so it's available for logging
                device_index = None
                if self.device not in ("default", "", None):
                    try:
                        device_index = int(self.device)
                    except (TypeError, ValueError):
                        logger.warning(f"Invalid microphone device id '{self.device}', using default microphone")
                        device_index = None
                
                if not self.stream:
                    # Auto-detect channels supported by the device to avoid PaErrorCode -9998.
                    # Also validate the device exists and is an input device.
                    try:
                        dev_info = sd.query_devices(device=device_index, kind='input')
                        max_channels = int(dev_info.get('max_input_channels', 0))
                        
                        if max_channels == 0:
                            # Device exists but has no input channels - fall back to default
                            raise ValueError(f"Device has no input channels")
                        output_channels = self._target_channels
                        if output_channels <= 0:
                            output_channels = 1
                        if output_channels > max_channels:
                            output_channels = max_channels
                        if self._target_channels != output_channels:
                            logger.info(
                                f"Overriding configured channels {self._target_channels} with device-supported {output_channels}"
                            )
                            self._target_channels = output_channels

                        # Some Windows mic arrays expose silence on channel 0 with mono capture.
                        # Capture up to 4 channels and downmix/select strongest in callback.
                        capture_channels = _determine_capture_channels(output_channels, max_channels)
                        self._capture_channels = capture_channels
                        if capture_channels != output_channels:
                            logger.info(
                                f"Using {capture_channels} capture channels for stability, downmixing to {output_channels}"
                            )

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
                                    self._capture_channels = _determine_capture_channels(chosen_channels, max_channels)
                                    logger.info(f"Using default device with {chosen_channels} channel(s)")
                            except Exception as e2:
                                logger.warning(f"Could not query default device ({e2}); using 1 channel")
                                self._target_channels = 1
                                self._capture_channels = 1
                        else:
                            logger.warning(f"Could not query default device ({e}); using 1 channel")
                            self._target_channels = 1
                            self._capture_channels = 1

                    # Try to open the stream - with fallback to default if configured device fails
                    try:
                        self.stream = sd.InputStream(
                            samplerate=self._target_sample_rate,
                            channels=self._capture_channels,
                            blocksize=self.block_size,
                            dtype="int16",
                            callback=self._audio_callback,
                            device=device_index,
                        )
                    except Exception as stream_err:
                        if device_index is not None:
                            # Configured device failed to open - fall back to system default
                            logger.warning(
                                f"Could not open device {device_index} ({stream_err}); "
                                f"falling back to Windows default microphone"
                            )
                            device_index = None
                            # Re-query default device channels
                            try:
                                default_info = sd.query_devices(device=None, kind='input')
                                max_channels = int(default_info.get('max_input_channels', 1))
                                if max_channels > 0:
                                    chosen_channels = self._target_channels
                                    if chosen_channels <= 0 or chosen_channels > max_channels:
                                        chosen_channels = max_channels
                                    self._target_channels = chosen_channels
                                    self._capture_channels = _determine_capture_channels(chosen_channels, max_channels)
                            except Exception:
                                self._target_channels = 1
                                self._capture_channels = 1

                            # Try default device
                            self.stream = sd.InputStream(
                                samplerate=self._target_sample_rate,
                                channels=self._capture_channels,
                                blocksize=self.block_size,
                                dtype="int16",
                                callback=self._audio_callback,
                                device=None,  # System default
                            )
                        else:
                            # Already using default, re-raise
                            raise

                if not self.stream.active:
                    self.stream.start()
                self._claim_active_stream()
            logger.info(f"Microphone stream started (device={'default' if device_index is None else device_index})")
            # Signal that microphone is ready and capturing audio
            if self.on_ready:
                try:
                    self.on_ready()
                except Exception as e:
                    logger.warning(f"on_ready callback error: {e}")
        except Exception as e:
            logger.error(f"Microphone error: {e}")
            await self.stop(frame=EndFrame())
            # Re-raise to notify the pipeline that microphone initialization failed
            raise RuntimeError(f"Microphone initialization failed: {e}") from e

    def _audio_callback(self, indata, frames, time_info, status):
        try:
            self._callback_count += 1
            self._last_callback_at = time.monotonic()
            if status:
                self._last_status = str(status)
                logger.warning(f"Audio status: {status}")
            if not self._running:
                return

            output_data = indata
            if (
                isinstance(indata, np.ndarray)
                and indata.ndim == 2
                and indata.shape[1] > self._target_channels
                and self._target_channels == 1
            ):
                self._channel_selection_counter += 1
                previous_channel = self._active_capture_channel
                should_rescan_channel = (
                    previous_channel is None
                    or previous_channel < 0
                    or previous_channel >= indata.shape[1]
                    or self._channel_selection_counter >= self._channel_selection_interval_frames
                )
                if should_rescan_channel:
                    self._channel_selection_counter = 0
                    output_data, chosen_channel = _select_best_mono_channel(
                        indata,
                        previous_channel,
                    )
                    if chosen_channel != previous_channel and chosen_channel is not None:
                        logger.debug(
                            "Microphone channel selection changed: {} -> {}",
                            previous_channel,
                            chosen_channel,
                        )
                    self._active_capture_channel = chosen_channel
                else:
                    mono = indata[:, previous_channel]
                    if mono.dtype != np.int16:
                        mono = np.clip(mono, -32768, 32767).astype(np.int16)
                    else:
                        mono = np.ascontiguousarray(mono)
                    output_data = mono.reshape(-1, 1)

            audio_bytes = output_data.tobytes()
            try:
                if self._loop and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._queue.put_nowait, audio_bytes)
            except RuntimeError as exc:
                # Event loop already closing/closed; stop pushing frames from callback thread.
                logger.debug(f"Audio callback loop unavailable: {exc}")
                self._running = False
                return

            # Visualizer/input-warning calculation is capped to UI frame rate. The
            # raw audio still flows downstream on every callback.
            if self.on_audio_level:
                try:
                    now = time.monotonic()
                    if now - self._last_audio_level_at < self._audio_level_interval:
                        return
                    self._last_audio_level_at = now
                    # Optimized RMS: use int16 view directly, compute in float32 for speed
                    # Use the exact frame we send downstream (after channel selection/downmix).
                    samples = np.asarray(output_data).astype(np.int16, copy=False).ravel()
                    # Use float32 for faster computation than float64
                    rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2)) / 32768.0

                    # Speech-focused gating (dynamic noise floor + hysteresis)
                    db = 20.0 * float(np.log10(rms + 1e-6))

                    # Update noise floor: quick to drop, very slow to rise (avoid "locking out" speech)
                    if (not self._speech_active) or (db < self._noise_floor_db + 3.0):
                        if db < self._noise_floor_db:
                            self._noise_floor_db = self._noise_floor_db * 0.8 + db * 0.2
                        elif db <= self._noise_floor_db + 1.0:
                            self._noise_floor_db = self._noise_floor_db * 0.98 + db * 0.02

                    # Lower thresholds for responsive visualization
                    threshold_high = max(self._noise_floor_db + 6.0, -58.0)
                    threshold_low = threshold_high - 8.0
                    abs_on_rms = 0.0007
                    abs_off_rms = 0.00025

                    if db >= threshold_high or rms >= abs_on_rms:
                        self._speech_active = True
                        self._speech_hold_until = now + 0.45
                    elif (
                        (db <= threshold_low and rms <= abs_off_rms)
                        and now >= self._speech_hold_until
                    ):
                        self._speech_active = False

                    # Keep visualization continuous across syllables.
                    vis_target = float(rms) if self._speech_active else max(0.0, rms * 0.10)
                    if vis_target > self._visual_level:
                        self._visual_level = self._visual_level * 0.25 + vis_target * 0.75
                    else:
                        self._visual_level = self._visual_level * 0.70 + vis_target * 0.30
                    self.on_audio_level(self._visual_level)
                except Exception:
                    pass
        except Exception as exc:
            # Never raise from PortAudio callback threads.
            self._last_callback_exception = str(exc)
            logger.debug(f"Audio callback exception ignored: {exc}")

    def diagnostic_snapshot(self) -> dict:
        stream = self.stream
        return {
            "running": bool(self._running),
            "hasStream": bool(stream),
            "streamActive": bool(stream and getattr(stream, "active", False)),
            "usingPrewarmStream": bool(self._using_prewarm_stream),
            "streamClaimed": bool(self._stream_claimed),
            "sampleRate": int(self._target_sample_rate),
            "targetChannels": int(self._target_channels),
            "captureChannels": int(self._capture_channels),
            "blockSize": int(self.block_size),
            "device": str(self.device),
            "callbackCount": int(self._callback_count),
            "lastCallbackAgoSeconds": (
                round(time.monotonic() - self._last_callback_at, 3)
                if self._last_callback_at > 0
                else None
            ),
            "lastStatus": self._last_status,
            "lastCallbackException": self._last_callback_exception,
        }

    def ensure_stream_health(
        self,
        *,
        reason: str = "watchdog",
        max_callback_gap_seconds: float | None = None,
        min_restart_interval_seconds: float = 15.0,
    ) -> bool:
        if not self._running:
            return True

        if self._using_prewarm_stream:
            ensure = getattr(self.prewarm_manager, "ensure_active_capture_healthy", None)
            if callable(ensure):
                healthy = bool(
                    ensure(
                        self._prewarm_callback,
                        reason=reason,
                        max_callback_gap_seconds=max_callback_gap_seconds,
                        min_restart_interval_seconds=min_restart_interval_seconds,
                    )
                )
                if not healthy:
                    logger.warning(f"Mic watchdog found unhealthy adopted prewarm stream ({reason})")
                return healthy
            return True

        stream = self.stream
        active = bool(stream and getattr(stream, "active", False))
        if stream and not active:
            self._release_active_stream()
        now = time.monotonic()
        callback_stale = (
            max_callback_gap_seconds is not None
            and self._last_callback_at > 0
            and now - self._last_callback_at > max_callback_gap_seconds
        )

        if active and not callback_stale:
            return True
        if not stream:
            logger.warning(f"Mic watchdog found missing capture stream while recording ({reason})")
            return False
        if now - self._last_health_restart_at < min_restart_interval_seconds:
            return active

        self._last_health_restart_at = now
        try:
            with get_device_guard_lock():
                if active and callback_stale:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                stream.start()
                self._claim_active_stream()
            logger.warning(
                "Microphone capture stream restarted "
                f"({reason}, was_active={active}, stale_callbacks={callback_stale})"
            )
            return bool(getattr(stream, "active", False))
        except Exception as exc:
            self._last_callback_exception = str(exc)
            if not bool(getattr(stream, "active", False)):
                self._release_active_stream()
            logger.warning(f"Microphone capture stream restart failed ({reason}): {exc}")
            return False

    def force_stop_from_external_error(self, *, reason: str = "external_error") -> None:
        logger.warning(f"Microphone capture stopped by pipeline error ({reason})")
        self._running = False
        if self._using_prewarm_stream:
            try:
                if self.prewarm_manager is not None:
                    self.prewarm_manager.detach_active_capture(self._prewarm_callback)
            except Exception as exc:
                logger.debug(f"Could not detach prewarmed microphone stream after pipeline error: {exc}")
            finally:
                self._using_prewarm_stream = False
                self._prewarm_callback = None
            return

        with get_device_guard_lock():
            if self.stream:
                try:
                    self.stream.stop()
                except Exception as exc:
                    logger.debug(f"Microphone stream stop after pipeline error failed: {exc}")
            self._release_active_stream()

    def _prepend_prewarm_audio(self, adopted: dict) -> None:
        frames = adopted.get("prebuffer_frames") or []
        if not frames:
            return
        for item in frames:
            try:
                indata, frame_count, time_info, status = item
                self._audio_callback(indata, int(frame_count or 0), time_info, status)
            except Exception as exc:
                logger.debug(f"Prewarmed audio prepend skipped frame: {exc}")

    async def _drain_queue(self):
        # Ensure audio queue exists (BaseInputTransport creates it in _create_audio_task)
        if not hasattr(self, "_audio_in_queue") or self._audio_in_queue is None:
            self._create_audio_task()
        # Wait for queue to be available and start frame delivered downstream
        while not hasattr(self, "_audio_in_queue") or self._audio_in_queue is None:
            await asyncio.sleep(0.01)

        try:
            while True:
                if not self._running and self._queue.empty():
                    break
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
            self._notify_last_audio_chunk_sent()
        except asyncio.CancelledError:
            # Clean up audio stream on cancellation
            self._running = False
            with get_device_guard_lock():
                if self.stream:
                    try:
                        self.stream.stop()
                        self.stream.close()
                    except Exception:
                        pass
                    self.stream = None
                self._release_active_stream()
            raise  # Re-raise to properly complete cancellation

    def _notify_last_audio_chunk_sent(self) -> None:
        if self._last_audio_chunk_sent_notified:
            return
        self._last_audio_chunk_sent_notified = True
        if not self.on_last_audio_chunk_sent:
            return
        try:
            self.on_last_audio_chunk_sent()
        except Exception as exc:
            logger.debug(f"Microphone last-chunk callback failed: {exc}")

    async def stop(self, frame: EndFrame, *, close_stream: bool | None = None):
        self._running = False

        # OPTIMIZED: Always stop stream to prevent CPU usage and buffer overflow
        # With keep_alive: pause stream (fast restart via stream.start())
        # Without keep_alive: close stream entirely
        should_close_stream = (not self.keep_alive) if close_stream is None else bool(close_stream)
        if self._using_prewarm_stream:
            try:
                if self.prewarm_manager is not None:
                    self.prewarm_manager.detach_active_capture(self._prewarm_callback)
            except Exception as exc:
                logger.debug(f"Could not detach prewarmed microphone stream: {exc}")
            finally:
                self._using_prewarm_stream = False
                self._prewarm_callback = None
        else:
            with get_device_guard_lock():
                if self.stream:
                    try:
                        self.stream.stop()  # Stops callbacks, saves CPU, prevents overflow
                        if should_close_stream:
                            self.stream.close()
                            self.stream = None
                    except Exception:
                        pass
                self._release_active_stream()

        # Signal the queue to stop
        if self._queue:
            try:
                if self._queue.empty():
                    self._queue.put_nowait(None)
            except Exception:
                pass

        # Wait for consumer task with timeout
        if self._consumer_task:
            task, self._consumer_task = self._consumer_task, None
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self._stopped.set()
        await super().stop(frame)
