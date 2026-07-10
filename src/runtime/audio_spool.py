"""Bounded-memory helpers for buffered live audio."""

from __future__ import annotations

import tempfile
import wave
from typing import BinaryIO


_COPY_CHUNK_BYTES = 1024 * 1024
_SPOOL_MEMORY_LIMIT_BYTES = 10 * 1024 * 1024


def pcm_stream_to_wav(
    audio_stream: BinaryIO,
    sample_rate: int,
    channels: int,
) -> BinaryIO:
    """Build a seekable PCM16 WAV while keeping long recordings off heap."""
    wav_file = tempfile.SpooledTemporaryFile(
        max_size=_SPOOL_MEMORY_LIMIT_BYTES,
        mode="w+b",
    )
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
