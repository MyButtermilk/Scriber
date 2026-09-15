"""Session-local speech detection for the optional Live Mic silence stop.

The observer never emits provider turn boundaries. Its analyzer runs off the
audio callback thread, and elapsed captured audio (not missing transcripts or
a stalled device) determines whether the configured silence has passed.
"""

import math
import time
from collections.abc import Callable
from typing import Protocol

from loguru import logger
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import CancelFrame, EndFrame, InputAudioRawFrame, StartFrame, StopFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class SilenceAnalyzer(Protocol):
    def set_sample_rate(self, sample_rate: int) -> None: ...

    async def analyze_audio(self, buffer: bytes) -> VADState: ...

    async def cleanup(self) -> None: ...


class MicSilenceStopObserver(FrameProcessor):
    """Pass PCM through unchanged and request one ordinary stop after silence."""

    def __init__(
        self,
        *,
        analyzer: SilenceAnalyzer,
        silence_seconds: int,
        on_timeout: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        if type(silence_seconds) is not int or not 1 <= silence_seconds <= 10:
            raise ValueError("Microphone silence timeout must be an integer from 1 to 10 seconds")
        self._analyzer = analyzer
        self._silence_seconds = silence_seconds
        self._on_timeout = on_timeout
        self._silence_clock = clock
        self._armed = False
        self._quiet_audio_seconds = 0.0
        self._quiet_since: float | None = None
        self._sample_rate: int | None = None

    def disarm(self) -> None:
        """Stop admitting timeout notifications when normal shutdown begins."""
        self._armed = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, StartFrame):
                self._armed = True
                self._quiet_audio_seconds = 0.0
                self._quiet_since = None
            elif isinstance(frame, (CancelFrame, EndFrame, StopFrame)):
                self.disarm()

        # Preserve all original audio and control frames, including when VAD
        # fails. No VAD/turn frames are added to the provider's stream.
        await self.push_frame(frame, direction)
        if not self._armed or direction != FrameDirection.DOWNSTREAM or not isinstance(frame, InputAudioRawFrame):
            return

        try:
            if frame.num_channels != 1 or frame.sample_rate not in (8000, 16000):
                # Production Live Mic is mono 16 kHz. Unsupported test/replay
                # formats must not be mistaken for quiet microphone audio.
                self.disarm()
                logger.warning("Microphone silence stop disabled for unsupported capture format")
                return
            if not frame.audio or len(frame.audio) % 2:
                return
            if self._sample_rate != frame.sample_rate:
                self._analyzer.set_sample_rate(frame.sample_rate)
                self._sample_rate = frame.sample_rate
                self._quiet_audio_seconds = 0.0
                self._quiet_since = None
            state = await self._analyzer.analyze_audio(frame.audio)
            if not self._armed:
                return
            if state != VADState.QUIET:
                # STARTING also resets the deadline: a fresh syllable at the
                # boundary must be given time to become confirmed speech.
                self._quiet_audio_seconds = 0.0
                self._quiet_since = None
                return
            now = self._silence_clock()
            if self._quiet_since is None:
                self._quiet_since = now
            self._quiet_audio_seconds += len(frame.audio) / (frame.sample_rate * 2)
            # The wall-clock floor prevents prewarm/replayed buffered frames
            # from consuming the user's pause allowance before real capture.
            if (
                self._quiet_audio_seconds + 1e-9 >= self._silence_seconds
                and math.isfinite(now)
                and now - self._quiet_since >= self._silence_seconds
            ):
                self.disarm()
                self._on_timeout()
        except Exception as exc:
            self.disarm()
            logger.warning("Microphone silence stop disabled after analyzer failure: {}", type(exc).__name__)

    async def cleanup(self):
        self.disarm()
        try:
            await self._analyzer.cleanup()
        finally:
            await super().cleanup()
