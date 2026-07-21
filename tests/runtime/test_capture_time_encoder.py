import pytest

from src.runtime.capture_time_encoder import (
    CaptureTimeEncoderError,
    CaptureTimeFfmpegEncoder,
)


@pytest.mark.asyncio
async def test_capture_time_encoder_fails_closed_on_pcm_format_change():
    encoder = CaptureTimeFfmpegEncoder(
        ["unused-encoder"],
        sample_rate=16000,
        channels=1,
    )

    assert not encoder.offer(b"\0\0", sample_rate=48000, channels=1)
    assert encoder.error_code == "pcmFormatChanged"
    with pytest.raises(CaptureTimeEncoderError, match="pcmFormatChanged"):
        await encoder.finish()


@pytest.mark.asyncio
async def test_capture_time_encoder_queue_overload_never_waits_or_loses_fallback():
    encoder = CaptureTimeFfmpegEncoder(
        ["unused-encoder"],
        sample_rate=16000,
        channels=1,
        queue_capacity=1,
        queued_pcm_limit=4,
    )

    assert encoder.offer(b"\0\0", sample_rate=16000, channels=1)
    assert not encoder.offer(b"\0\0", sample_rate=16000, channels=1)
    assert encoder.error_code == "boundedQueueOverflow"
    with pytest.raises(CaptureTimeEncoderError, match="boundedQueueOverflow"):
        await encoder.finish()
