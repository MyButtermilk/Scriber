import asyncio
from loguru import logger
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import Frame, AudioRawFrame
from pipecat.audio.vad.vad_analyzer import VADAnalyzer

class VADProcessor(FrameProcessor):
    """
    A FrameProcessor that wraps a VADAnalyzer to perform VAD on AudioRawFrames.
    It doesn't block audio, but it could emit VAD events.
    However, for this dictation app, we might rely on STT services doing their own VAD/Silence detection
    (often better for streaming) or use this to gate audio.

    If we want to use Pipecat's VAD to "detect end of speech", we usually need it to control flow.
    But `SileroVADAnalyzer` in Pipecat is designed to be used by `VoiceActivityController` or Transports.

    Since we are building a custom pipeline without a standard WebRTC transport,
    we will implement a simple VADProcessor that runs the analyzer.
    """
    def __init__(self, analyzer: VADAnalyzer):
        super().__init__()
        self.analyzer = analyzer

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, AudioRawFrame):
            # Analyze
            confidence = self.analyzer.voice_confidence(frame.audio)
            # Here we could decide to emit UserStartedSpeaking/StoppedSpeaking frames
            # if we were tracking state.
            # For now, we just pass through, as the primary goal of VAD in this request
            # was "Smart Turn Detection" which uses VAD.
            # BUT SmartTurnAnalyzer ALSO needs to be run.
            pass

        await self.push_frame(frame, direction)
