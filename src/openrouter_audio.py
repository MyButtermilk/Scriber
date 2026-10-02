"""Bounded MP3 packet copies with a stable, source-time part manifest."""

from __future__ import annotations

import asyncio
import math
import struct
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from src.audio_prepare import (
    PreparedProviderAudio,
    ProbedAudioInput,
    ProviderAudioPreparationError,
    probe_audio_input_file,
)
from src.core.provider_audio_formats import OPENROUTER_STT_MAX_AUDIO_BYTES, AudioInputFormat
from src.runtime.cancellation import await_with_delayed_cancellation, to_thread_cancellation_barrier
from src.runtime.ffmpeg_commands import classify_ffmpeg_stderr
from src.runtime.media_tools import require_media_tool
from src.runtime.subprocess_utils import communicate_or_kill_on_cancel, hidden_subprocess_kwargs

# These are targets only: fitting originals always pass through unchanged.
OPENROUTER_PART_TARGET_BYTES = 24_000_000
MP3_PART_OVERLAP_MS = 8_000
_MANIFEST_VERSION = 1
_FRAME_MARGIN_MS = 100
_MIN_CORE_MS = 20
_MAX_PARTS = 100_000
_SILENCE_RADIUS_MS = 4_000
_SILENCE_SAMPLE_RATE = 8_000
_TIMESTAMP_TOLERANCE_MS = 250


@dataclass(frozen=True, slots=True)
class AudioPartWindow:
    """Nominal upload bounds and non-overlapping ownership on the source clock.

    Packet copies are MP3-frame aligned; bounds are not sample-exact seeks.
    """

    index: int
    count: int
    start_ms: int
    end_ms: int
    core_start_ms: int
    core_end_ms: int


@dataclass(frozen=True, slots=True)
class AudioPart:
    path: Path
    index: int
    count: int
    start_ms: int = 0
    end_ms: int = 0
    core_start_ms: int = 0
    core_end_ms: int = 0
    timestamp_tolerance_ms: int = _TIMESTAMP_TOLERANCE_MS


# Existing callers construct this with just (path, index, count).
OpenRouterAudioPart = AudioPart


@dataclass(frozen=True, slots=True)
class AudioPartsManifest:
    """Path-free split plan. The caller separately binds its source SHA-256."""

    duration_ms: int
    source_byte_length: int
    max_audio_bytes: int
    max_duration_ms: int | None
    parts: tuple[AudioPartWindow, ...]
    passthrough: bool = False
    overlap_ms: int = MP3_PART_OVERLAP_MS
    version: int = _MANIFEST_VERSION
    timestamp_tolerance_ms: int = _TIMESTAMP_TOLERANCE_MS

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["parts"] = [asdict(part) for part in self.parts]
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AudioPartsManifest:
        try:
            if not isinstance(payload, Mapping):
                raise ValueError
            if set(payload) != {
                "duration_ms",
                "source_byte_length",
                "max_audio_bytes",
                "max_duration_ms",
                "parts",
                "passthrough",
                "overlap_ms",
                "version",
                "timestamp_tolerance_ms",
            }:
                raise ValueError
            values = dict(payload)
            raw_parts = values.pop("parts")
            if not isinstance(raw_parts, (list, tuple)) or not 1 <= len(raw_parts) <= _MAX_PARTS:
                raise ValueError
            manifest = cls(parts=tuple(AudioPartWindow(**part) for part in raw_parts), **values)
            manifest.validate()
            return manifest
        except (TypeError, ValueError, KeyError) as exc:
            raise ProviderAudioPreparationError("Invalid saved audio-part manifest.") from exc

    def validate(self) -> None:
        def integer(value: Any, minimum: int = 0) -> bool:
            return type(value) is int and value >= minimum

        if (
            type(self.version) is not int
            or self.version != _MANIFEST_VERSION
            or not integer(self.duration_ms)
            or not integer(self.source_byte_length, 1)
            or not integer(self.max_audio_bytes, 1)
            or (self.max_duration_ms is not None and not integer(self.max_duration_ms, 1))
            or not integer(self.overlap_ms)
            or self.overlap_ms > MP3_PART_OVERLAP_MS
            or type(self.passthrough) is not bool
            or not integer(self.timestamp_tolerance_ms)
            or self.timestamp_tolerance_ms != (0 if self.passthrough else _TIMESTAMP_TOLERANCE_MS)
            or not isinstance(self.parts, tuple)
            or not 1 <= len(self.parts) <= _MAX_PARTS
        ):
            raise ProviderAudioPreparationError("Invalid audio-part manifest limits.")
        previous_core_end = 0
        for index, part in enumerate(self.parts, 1):
            if not isinstance(part, AudioPartWindow) or not all(integer(value) for value in asdict(part).values()):
                raise ProviderAudioPreparationError("Invalid audio-part manifest boundary.")
            if (
                part.index != index
                or part.count != len(self.parts)
                or part.core_start_ms != previous_core_end
                or not 0 <= part.start_ms <= part.core_start_ms <= part.core_end_ms <= part.end_ms <= self.duration_ms
                or (part.core_end_ms == part.core_start_ms and not self.passthrough)
                or part.core_start_ms - part.start_ms > self.overlap_ms // 2
                or part.end_ms - part.core_end_ms > self.overlap_ms - self.overlap_ms // 2
                or (self.max_duration_ms is not None and part.end_ms - part.start_ms > self.max_duration_ms)
            ):
                raise ProviderAudioPreparationError("Invalid audio-part manifest coverage.")
            previous_core_end = part.core_end_ms
        if previous_core_end != self.duration_ms or (
            self.passthrough and (len(self.parts) != 1 or self.source_byte_length > self.max_audio_bytes)
        ):
            raise ProviderAudioPreparationError("Invalid audio-part manifest source coverage.")
        if self.max_duration_ms is not None and not self.duration_ms:
            raise ProviderAudioPreparationError("Audio-part manifest requires a known duration.")


async def _run_ffmpeg(command: list[str], *, max_stdout_bytes: int = 64 * 1024) -> tuple[int, bytes, bytes]:
    # Cancellation during process creation must not lose ownership of the
    # child. Repeated cancellation also cannot interrupt the final reap.
    process, pending_cancel = await await_with_delayed_cancellation(
        asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
    )

    async def communicate() -> tuple[bytes | None, bytes | None]:
        async with asyncio.timeout(120):
            return await communicate_or_kill_on_cancel(
                process, max_stdout_bytes=max_stdout_bytes, max_stderr_bytes=1024 * 1024
            )

    worker = asyncio.create_task(communicate())
    try:
        if pending_cancel is not None:
            raise pending_cancel
        stdout, stderr = await asyncio.shield(worker)
    except asyncio.CancelledError:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        with suppress(Exception):
            await await_with_delayed_cancellation(worker)
        raise
    return process.returncode, stdout or b"", stderr or b""


async def _run_part_command(command: list[str], target: Path) -> None:
    returncode, _, stderr = await _run_ffmpeg(command)
    if returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = classify_ffmpeg_stderr(stderr.decode("utf-8", errors="replace"))
        raise ProviderAudioPreparationError(detail or "Could not prepare an audio part.")


def _copy_command(ffmpeg: str, source: Path, target: Path, part: AudioPartWindow) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{part.start_ms / 1000:.3f}",
        "-i",
        str(source),
        "-t",
        f"{(part.end_ms - part.start_ms) / 1000:.3f}",
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


async def _silence_cut(ffmpeg: str, source: Path, candidate_ms: int, minimum_ms: int, maximum_ms: int) -> int:
    """Decode at most eight local seconds, never the recording's whole PCM."""
    start_ms = max(minimum_ms, candidate_ms - _SILENCE_RADIUS_MS)
    end_ms = min(maximum_ms, candidate_ms + _SILENCE_RADIUS_MS)
    if end_ms - start_ms < 200:
        return candidate_ms
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(source),
        "-t",
        f"{(end_ms - start_ms) / 1000:.3f}",
        "-vn",
        "-map",
        "0:a:0",
        "-ar",
        str(_SILENCE_SAMPLE_RATE),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        returncode, pcm, _ = await _run_ffmpeg(command, max_stdout_bytes=2 * 8 * _SILENCE_SAMPLE_RATE)
    except OSError, TimeoutError:
        return candidate_ms
    if returncode or not pcm:
        return candidate_ms
    frame_bytes = _SILENCE_SAMPLE_RATE * 2 // 50
    silent_start: int | None = None
    candidates: list[int] = []
    for frame in range(len(pcm) // frame_bytes + 1):
        data = pcm[frame * frame_bytes : (frame + 1) * frame_bytes]
        silent = len(data) == frame_bytes and sum(sample[0] ** 2 for sample in struct.iter_unpack("<h", data)) <= (
            frame_bytes // 2 * 327**2
        )
        if silent and silent_start is None:
            silent_start = frame
        if not silent and silent_start is not None:
            if frame - silent_start >= 10:
                lower = start_ms + silent_start * 20 + 100
                upper = start_ms + frame * 20 - 100
                candidates.append(max(lower, min(candidate_ms, upper)))
            silent_start = None
    return min(candidates, key=lambda value: (abs(value - candidate_ms), value)) if candidates else candidate_ms


def _window(start_ms: int, end_ms: int, duration_ms: int, overlap_ms: int) -> AudioPartWindow:
    return AudioPartWindow(
        index=0,
        count=0,
        start_ms=max(0, start_ms - overlap_ms // 2),
        end_ms=min(duration_ms, end_ms + overlap_ms - overlap_ms // 2),
        core_start_ms=start_ms,
        core_end_ms=end_ms,
    )


async def _source_probe(source: Path, prepared_audio: PreparedProviderAudio | None) -> ProbedAudioInput:
    # MP3 retains its packet-copy verification. Only an already verified exact
    # WAV/FLAC representation can borrow preparation evidence for one request.
    if prepared_audio is None or prepared_audio.selected_format == AudioInputFormat.MP3:
        return await to_thread_cancellation_barrier(probe_audio_input_file, source)
    source_stat = source.stat()
    if (
        prepared_audio.selected_format not in {AudioInputFormat.WAV_PCM16, AudioInputFormat.FLAC}
        or prepared_audio.path.resolve() != source.resolve()
        or prepared_audio.byte_length != source_stat.st_size
        or (
            prepared_audio.verified_mtime_ns is not None and prepared_audio.verified_mtime_ns != source_stat.st_mtime_ns
        )
    ):
        raise ProviderAudioPreparationError("Prepared audio no longer matches its verified file.")
    duration_ms = prepared_audio.duration_ms
    if duration_ms is None or prepared_audio.verified_mtime_ns is None:
        probe = await to_thread_cancellation_barrier(probe_audio_input_file, source)
        if probe.audio_format != prepared_audio.selected_format or probe.byte_length != prepared_audio.byte_length:
            raise ProviderAudioPreparationError("Prepared audio no longer matches its verified format or size.")
        return probe
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ProviderAudioPreparationError("Prepared audio requires a known positive recording duration.")
    return ProbedAudioInput(
        prepared_audio.selected_format,
        prepared_audio.selected_format.container.value,
        prepared_audio.selected_format.codec.value,
        None,
        None,
        duration_ms,
        prepared_audio.byte_length,
    )


def _check_probe(
    probe: ProbedAudioInput,
    *,
    max_duration_ms: int | None,
    prepared_audio: PreparedProviderAudio | None = None,
) -> None:
    verified_passthrough = prepared_audio is not None and probe.audio_format == prepared_audio.selected_format
    if (probe.audio_format != AudioInputFormat.MP3 and not verified_passthrough) or probe.byte_length <= 0:
        raise ProviderAudioPreparationError("Audio parts require verified MP3 audio.")
    if (max_duration_ms is not None or probe.audio_format != AudioInputFormat.MP3) and (
        type(probe.duration_ms) is not int or probe.duration_ms <= 0
    ):
        raise ProviderAudioPreparationError("Could not determine the recording duration for automatic splitting.")


def _fits(probe: ProbedAudioInput, max_audio_bytes: int, max_duration_ms: int | None) -> bool:
    return probe.byte_length <= max_audio_bytes and (
        max_duration_ms is None or (probe.duration_ms is not None and 0 < probe.duration_ms <= max_duration_ms)
    )


def _source_stamp(source: Path) -> tuple[int, int]:
    stat = source.stat()
    return stat.st_size, stat.st_mtime_ns


class _OverlapTooLarge(Exception):
    pass


async def _plan_windows(
    source: Path,
    *,
    ffmpeg: str,
    target: Path,
    duration_ms: int,
    core_span_ms: int,
    overlap_ms: int,
    max_audio_bytes: int,
    max_duration_ms: int | None,
) -> tuple[AudioPartWindow, ...]:
    windows: list[AudioPartWindow] = []
    start_ms = 0
    while start_ms < duration_ms:
        if len(windows) >= _MAX_PARTS:
            raise ProviderAudioPreparationError("The recording requires too many audio parts.")
        end_ms = min(duration_ms, start_ms + core_span_ms)
        if end_ms < duration_ms:
            end_ms = await _silence_cut(
                ffmpeg,
                source,
                end_ms,
                max(start_ms + _MIN_CORE_MS, end_ms - min(_SILENCE_RADIUS_MS, core_span_ms // 4)),
                min(duration_ms, end_ms + min(_SILENCE_RADIUS_MS, core_span_ms // 4)),
            )
        for _ in range(32):
            part = _window(start_ms, end_ms, duration_ms, overlap_ms)
            try:
                await _run_part_command(_copy_command(ffmpeg, source, target, part), target)
                probe = await to_thread_cancellation_barrier(probe_audio_input_file, target)
                _check_probe(probe, max_duration_ms=max_duration_ms)
                if _fits(probe, max_audio_bytes, max_duration_ms) and target.stat().st_size <= max_audio_bytes:
                    windows.append(part)
                    start_ms = end_ms
                    break
                # Average bitrate is only a hint. Refine measured VBR bytes
                # and duration before publishing a manifest or making HTTP.
                ratios = [0.8, max_audio_bytes * 0.96 / max(probe.byte_length, target.stat().st_size)]
                if max_duration_ms is not None and probe.duration_ms:
                    ratios.append(max(1, max_duration_ms - _FRAME_MARGIN_MS) / probe.duration_ms)
                desired_span = math.floor((part.end_ms - part.start_ms) * min(ratios))
                next_end = part.start_ms + desired_span - (part.end_ms - part.core_end_ms)
                end_ms = min(end_ms - 1, next_end)
                if end_ms - start_ms < _MIN_CORE_MS:
                    raise _OverlapTooLarge
            finally:
                target.unlink(missing_ok=True)
        else:
            raise ProviderAudioPreparationError("Could not fit an MP3 part within the upload limits.")
    return tuple(replace(part, index=index, count=len(windows)) for index, part in enumerate(windows, 1))


async def plan_mp3_audio_parts(
    source: Path,
    *,
    max_audio_bytes: int,
    max_duration_ms: int | None = None,
    target_audio_bytes: int | None = None,
    prepared_audio: PreparedProviderAudio | None = None,
) -> AudioPartsManifest:
    """Freeze all bounds after local size/duration checks, with one temp file.

    The durable attempt owner binds the source content fingerprint separately.
    """
    if type(max_audio_bytes) is not int or max_audio_bytes <= 0:
        raise ValueError("max_audio_bytes must be positive")
    if max_duration_ms is not None and (type(max_duration_ms) is not int or max_duration_ms <= 0):
        raise ValueError("max_duration_ms must be positive")
    if target_audio_bytes is not None and (type(target_audio_bytes) is not int or target_audio_bytes <= 0):
        raise ValueError("target_audio_bytes must be positive")
    source = Path(source)
    stamp = _source_stamp(source)
    probe = await _source_probe(source, prepared_audio)
    _check_probe(probe, max_duration_ms=max_duration_ms, prepared_audio=prepared_audio)
    if probe.byte_length != stamp[0]:
        raise ProviderAudioPreparationError("The audio source changed while preparing its parts.")
    duration_ms = probe.duration_ms or 0
    if _fits(probe, max_audio_bytes, max_duration_ms):
        parts = (AudioPartWindow(1, 1, 0, duration_ms, 0, duration_ms),)
        manifest = AudioPartsManifest(
            duration_ms, stamp[0], max_audio_bytes, max_duration_ms, parts, True, 0, timestamp_tolerance_ms=0
        )
    else:
        if probe.audio_format != AudioInputFormat.MP3:
            raise ProviderAudioPreparationError(
                "Frozen prepared audio exceeds the provider request byte or duration limit; splitting requires MP3."
            )
        if duration_ms <= 0:
            raise ProviderAudioPreparationError("Could not determine the recording duration for automatic splitting.")
        target_bytes = min(target_audio_bytes or max_audio_bytes, max(1, math.floor(max_audio_bytes * 0.96)))
        target_duration = max(1, duration_ms * target_bytes // probe.byte_length)
        if max_duration_ms is not None:
            target_duration = min(target_duration, max(1, max_duration_ms - _FRAME_MARGIN_MS))
        overlap_ms = min(MP3_PART_OVERLAP_MS, target_duration // 4)
        ffmpeg = require_media_tool("ffmpeg")
        with tempfile.TemporaryDirectory(prefix="scriber-mp3-parts-plan-") as directory:
            while True:
                try:
                    parts = await _plan_windows(
                        source,
                        ffmpeg=ffmpeg,
                        target=Path(directory) / "part.mp3",
                        duration_ms=duration_ms,
                        core_span_ms=max(_MIN_CORE_MS, target_duration - overlap_ms),
                        overlap_ms=overlap_ms,
                        max_audio_bytes=max_audio_bytes,
                        max_duration_ms=max_duration_ms,
                    )
                    break
                except _OverlapTooLarge:
                    if overlap_ms == 0:
                        raise ProviderAudioPreparationError(
                            "Could not fit an MP3 part within the upload limits."
                        ) from None
                    overlap_ms //= 2
        manifest = AudioPartsManifest(duration_ms, stamp[0], max_audio_bytes, max_duration_ms, parts, False, overlap_ms)
    if _source_stamp(source) != stamp:
        raise ProviderAudioPreparationError("The audio source changed while preparing its parts.")
    manifest.validate()
    return manifest


async def _parts(
    source: Path, manifest: AudioPartsManifest, prepared_audio: PreparedProviderAudio | None = None
) -> AsyncIterator[AudioPart]:
    manifest.validate()
    stamp = _source_stamp(source)
    probe = await _source_probe(source, prepared_audio)
    _check_probe(probe, max_duration_ms=manifest.max_duration_ms, prepared_audio=prepared_audio)
    if stamp[0] != manifest.source_byte_length or (probe.duration_ms or 0) != manifest.duration_ms:
        raise ProviderAudioPreparationError("The audio source no longer matches its saved part manifest.")
    if manifest.passthrough:
        if not _fits(probe, manifest.max_audio_bytes, manifest.max_duration_ms) or _source_stamp(source) != stamp:
            raise ProviderAudioPreparationError("The audio source no longer fits its upload limits.")
        yield AudioPart(
            path=source, **asdict(manifest.parts[0]), timestamp_tolerance_ms=manifest.timestamp_tolerance_ms
        )
        return
    if probe.audio_format != AudioInputFormat.MP3:
        raise ProviderAudioPreparationError("Saved audio-part splitting requires verified MP3 audio.")
    ffmpeg = require_media_tool("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="scriber-mp3-parts-") as directory:
        target = Path(directory) / "part.mp3"
        for part in manifest.parts:
            try:
                if _source_stamp(source) != stamp:
                    raise ProviderAudioPreparationError("The audio source changed while preparing its parts.")
                await _run_part_command(_copy_command(ffmpeg, source, target, part), target)
                probe = await to_thread_cancellation_barrier(probe_audio_input_file, target)
                _check_probe(probe, max_duration_ms=manifest.max_duration_ms)
                if (
                    not _fits(probe, manifest.max_audio_bytes, manifest.max_duration_ms)
                    or target.stat().st_size > manifest.max_audio_bytes
                    or _source_stamp(source) != stamp
                ):
                    raise ProviderAudioPreparationError("Prepared audio part failed its saved size/duration limits.")
                yield AudioPart(path=target, **asdict(part), timestamp_tolerance_ms=manifest.timestamp_tolerance_ms)
            finally:
                target.unlink(missing_ok=True)


@asynccontextmanager
async def mp3_audio_parts(
    source: Path,
    *,
    max_audio_bytes: int,
    max_duration_ms: int | None = None,
    manifest: AudioPartsManifest | None = None,
    target_audio_bytes: int | None = None,
    prepared_audio: PreparedProviderAudio | None = None,
) -> AsyncIterator[AsyncIterator[AudioPart]]:
    """Yield verified planned parts, keeping at most one temporary derivative."""
    if manifest is None:
        manifest = await plan_mp3_audio_parts(
            source,
            max_audio_bytes=max_audio_bytes,
            max_duration_ms=max_duration_ms,
            target_audio_bytes=target_audio_bytes,
            prepared_audio=prepared_audio,
        )
    elif manifest.max_audio_bytes != max_audio_bytes or manifest.max_duration_ms != max_duration_ms:
        raise ProviderAudioPreparationError("Saved audio-part manifest uses different provider limits.")
    iterator = _parts(Path(source), manifest, prepared_audio)
    try:
        yield iterator
    finally:
        await iterator.aclose()


@asynccontextmanager
async def openrouter_audio_parts(
    source: Path, *, manifest: AudioPartsManifest | None = None
) -> AsyncIterator[AsyncIterator[OpenRouterAudioPart]]:
    async with mp3_audio_parts(
        source,
        max_audio_bytes=OPENROUTER_STT_MAX_AUDIO_BYTES,
        target_audio_bytes=OPENROUTER_PART_TARGET_BYTES,
        manifest=manifest,
    ) as iterator:
        yield iterator
