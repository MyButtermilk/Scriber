"""Bounded-memory helpers for buffered live audio."""

from __future__ import annotations

import asyncio
import struct
import tempfile
import wave
from typing import BinaryIO


_COPY_CHUNK_BYTES = 1024 * 1024
SPOOL_MEMORY_LIMIT_BYTES = 10 * 1024 * 1024
WAV_PCM16_HEADER_BYTES = 44


def create_pcm_spool(*, reserve_wav_header: bool = False) -> BinaryIO:
    """Create Scriber's bounded-memory PCM spool."""
    spool = tempfile.SpooledTemporaryFile(
        max_size=SPOOL_MEMORY_LIMIT_BYTES,
        mode="w+b",
    )
    if reserve_wav_header:
        # Capture appends authoritative PCM immediately after this fixed-size
        # prefix. Stop then patches only 44 bytes instead of copying the full
        # recording to a second WAV spool.
        spool.write(b"\0" * WAV_PCM16_HEADER_BYTES)
    return spool


def close_pcm_spool(audio_stream: BinaryIO | None) -> None:
    """Best-effort deterministic cleanup for processor-owned PCM spools."""
    if audio_stream is None:
        return
    try:
        audio_stream.close()
    except Exception:
        pass


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
    *,
    reserved_wav_header: bool = False,
    pcm_size: int | None = None,
) -> BinaryIO:
    """Build a seekable PCM16 WAV while keeping long recordings off heap."""
    resolved_sample_rate = max(1, int(sample_rate or 16000))
    resolved_channels = max(1, int(channels or 1))
    if reserved_wav_header:
        size = max(0, int(pcm_size or 0))
        block_align = resolved_channels * 2
        if size % block_align:
            raise ValueError("PCM spool is not sample-frame aligned")
        if size > 0xFFFFFFFF - 36:
            raise ValueError("PCM spool is too large for a RIFF/WAV container")
        byte_rate = resolved_sample_rate * block_align
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            size + 36,
            b"WAVE",
            b"fmt ",
            16,
            1,
            resolved_channels,
            resolved_sample_rate,
            byte_rate,
            block_align,
            16,
            b"data",
            size,
        )
        audio_stream.seek(0, 2)
        actual_size = audio_stream.tell()
        if actual_size != WAV_PCM16_HEADER_BYTES + size:
            raise ValueError("Reserved WAV spool size does not match captured PCM")
        audio_stream.seek(0)
        audio_stream.write(header)
        audio_stream.seek(0)
        return audio_stream

    wav_file = create_pcm_spool()
    audio_stream.seek(0)
    try:
        with wave.open(wav_file, "wb") as wav:
            wav.setnchannels(resolved_channels)
            wav.setsampwidth(2)
            wav.setframerate(resolved_sample_rate)
            while chunk := audio_stream.read(_COPY_CHUNK_BYTES):
                wav.writeframesraw(chunk)
        wav_file.seek(0)
        return wav_file
    except BaseException:
        wav_file.close()
        raise
