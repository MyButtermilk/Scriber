"""Small PCM helpers shared by latency-sensitive runtime paths."""

from __future__ import annotations

from dataclasses import dataclass

import audioop

PcmBuffer = bytes | bytearray | memoryview


@dataclass(frozen=True, slots=True)
class Pcm16Metrics:
    """Bounded metrics for complete little-endian signed-16 PCM samples."""

    sample_count: int
    rms: int
    peak: int


def _whole_pcm16_frames(pcm: PcmBuffer) -> memoryview:
    """Return complete signed-16 frames, ignoring one trailing partial byte."""

    view = memoryview(pcm).cast("B")
    return view[: len(view) - (len(view) % 2)]


def pcm16le_metrics(pcm: PcmBuffer) -> Pcm16Metrics:
    """Return sample count, RMS, and absolute peak without a Python sample loop.

    Both numerical operations run in ``audioop``'s C implementation. As with
    the other PCM16 helpers, one trailing partial sample byte is ignored.
    """

    complete_frames = _whole_pcm16_frames(pcm)
    if len(complete_frames) < 2:
        return Pcm16Metrics(sample_count=0, rms=0, peak=0)
    return Pcm16Metrics(
        sample_count=len(complete_frames) // 2,
        rms=audioop.rms(complete_frames, 2),
        peak=audioop.max(complete_frames, 2),
    )


def pcm16le_rms(pcm: PcmBuffer) -> int:
    """Return the integer RMS of little-endian signed-16 mono PCM.

    ``audioop.rms`` requires complete frames. Capture boundaries can contain a
    final odd byte, so this helper deliberately ignores that partial sample,
    matching Scriber's previous meeting-level and Smart Turn gate semantics.
    """

    complete_frames = _whole_pcm16_frames(pcm)
    if len(complete_frames) < 2:
        return 0
    return audioop.rms(complete_frames, 2)


def pcm16le_meets_rms_threshold(
    pcm: PcmBuffer,
    *,
    threshold: float,
) -> bool:
    """Return whether at least one complete sample meets the RMS threshold."""

    complete_frames = _whole_pcm16_frames(pcm)
    if len(complete_frames) < 2:
        return False
    return audioop.rms(complete_frames, 2) >= threshold
