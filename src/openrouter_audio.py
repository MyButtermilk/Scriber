"""Scoped, bounded MP3 parts for OpenRouter's synchronous transcription route."""

from __future__ import annotations

import asyncio
import math
import tempfile
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from src.audio_prepare import ProviderAudioPreparationError, probe_audio_input_file
from src.core.provider_audio_formats import OPENROUTER_STT_MAX_AUDIO_BYTES, AudioInputFormat
from src.runtime.cancellation import to_thread_cancellation_barrier
from src.runtime.ffmpeg_commands import classify_ffmpeg_stderr
from src.runtime.media_tools import require_media_tool
from src.runtime.subprocess_utils import communicate_or_kill_on_cancel, hidden_subprocess_kwargs

# Split the compressed audio, not the source video's bytes or elapsed minutes.
# Leave headroom for variable bitrate and MP3 framing below the 25-MB API cap.
OPENROUTER_PART_TARGET_BYTES = 20_000_000


@dataclass(frozen=True, slots=True)
class OpenRouterAudioPart:
    path: Path
    index: int
    count: int


async def _run_part_command(command: list[str], target: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )
    _, stderr = await communicate_or_kill_on_cancel(process, max_stdout_bytes=64 * 1024, max_stderr_bytes=1024 * 1024)
    if process.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = classify_ffmpeg_stderr(stderr.decode("utf-8", errors="replace"))
        raise ProviderAudioPreparationError(detail or "Could not prepare an audio part.")


async def _parts(source: Path) -> AsyncIterator[OpenRouterAudioPart]:
    probe = await to_thread_cancellation_barrier(probe_audio_input_file, source)
    if probe.audio_format != AudioInputFormat.MP3:
        raise ProviderAudioPreparationError("OpenRouter parts require verified MP3 audio.")
    if probe.byte_length <= OPENROUTER_STT_MAX_AUDIO_BYTES:
        yield OpenRouterAudioPart(source, 1, 1)
        return
    duration_ms = probe.duration_ms
    if duration_ms is None:
        raise ProviderAudioPreparationError("Could not determine the recording duration for automatic splitting.")
    duration = duration_ms / 1000
    count = math.ceil(probe.byte_length / OPENROUTER_PART_TARGET_BYTES)
    part_seconds = duration * OPENROUTER_PART_TARGET_BYTES / probe.byte_length
    windows = deque((index * part_seconds, min((index + 1) * part_seconds, duration)) for index in range(count))
    ffmpeg = require_media_tool("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="scriber-openrouter-parts-") as directory:
        index = 0
        while windows:
            target = Path(directory) / "part.mp3"
            start, end = windows.popleft()
            seconds = end - start
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-ss",
                str(start),
                "-i",
                str(source),
                "-t",
                str(seconds),
                "-vn",
                "-map",
                "0:a:0",
                "-c:a",
                "copy",
                "-map_metadata",
                "-1",
                "-id3v2_version",
                "0",
                "-write_id3v1",
                "0",
                str(target),
            ]
            await _run_part_command(command, target)
            if target.stat().st_size > OPENROUTER_STT_MAX_AUDIO_BYTES:
                # A VBR interval can exceed the source's average bitrate.
                # Refine its boundary before HTTP, retaining both halves and
                # the original codec. Never retry a billable upload to resize it.
                if seconds < 0.1:
                    raise ProviderAudioPreparationError("Could not fit an MP3 part within the upload limit.")
                fraction = min(0.8, OPENROUTER_PART_TARGET_BYTES / target.stat().st_size)
                split = start + seconds * fraction
                windows.appendleft((split, end))
                windows.appendleft((start, split))
                count += 1
                target.unlink(missing_ok=True)
                continue
            part_probe = await to_thread_cancellation_barrier(probe_audio_input_file, target)
            if part_probe.audio_format != AudioInputFormat.MP3 or not (
                0 < part_probe.byte_length <= OPENROUTER_STT_MAX_AUDIO_BYTES
            ):
                raise ProviderAudioPreparationError("Prepared audio part failed MP3 size/format verification.")
            try:
                index += 1
                yield OpenRouterAudioPart(target, index, count)
            finally:
                target.unlink(missing_ok=True)


@asynccontextmanager
async def openrouter_audio_parts(source: Path) -> AsyncIterator[AsyncIterator[OpenRouterAudioPart]]:
    """Keep at most one derivative and clean it even when its consumer fails."""
    iterator = _parts(source)
    try:
        yield iterator
    finally:
        await iterator.aclose()
