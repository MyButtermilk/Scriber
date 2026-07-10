"""Bounded-memory helpers for buffered live audio."""

from __future__ import annotations

import asyncio
import tempfile
import wave
from typing import BinaryIO


_COPY_CHUNK_BYTES = 1024 * 1024
SPOOL_MEMORY_LIMIT_BYTES = 10 * 1024 * 1024


def create_pcm_spool() -> BinaryIO:
    """Create Scriber's bounded-memory PCM spool."""
    return tempfile.SpooledTemporaryFile(
        max_size=SPOOL_MEMORY_LIMIT_BYTES,
        mode="w+b",
    )


async def append_pcm_frame(
    audio_stream: BinaryIO,
    current_size: int,
    audio: bytes,
) -> int:
    """Append a frame without copying the full memory spool on the event loop."""
    if not audio:
        return max(0, int(current_size))

    size = max(0, int(current_size))
    next_size = size + len(audio)
    if size <= SPOOL_MEMORY_LIMIT_BYTES < next_size:
        rollover = getattr(audio_stream, "rollover", None)
        if callable(rollover):
            await asyncio.to_thread(rollover)
    audio_stream.write(audio)
    return next_size


def pcm_stream_to_wav(
    audio_stream: BinaryIO,
    sample_rate: int,
    channels: int,
) -> BinaryIO:
    """Build a seekable PCM16 WAV while keeping long recordings off heap."""
    wav_file = create_pcm_spool()
    audio_stream.seek(0)
    try:
        with wave.open(wav_file, "wb") as wav:
            wav.setnchannels(max(1, int(channels or 1)))
            wav.setsampwidth(2)
            wav.setframerate(max(1, int(sample_rate or 16000)))
            while chunk := audio_stream.read(_COPY_CHUNK_BYTES):
                wav.writeframesraw(chunk)
        wav_file.seek(0)
        return wav_file
    except BaseException:
        wav_file.close()
        raise
