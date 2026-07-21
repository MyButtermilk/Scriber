"""Exact provider-aware preparation for file-backed STT audio.

The source is probed by both container and codec.  Selection then uses the
route/model capability registry and preserves an accepted original unchanged
before considering a generated representation.  Generated artifacts are
created under an explicit work directory and are always cleaned by the async
context manager.
"""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Mapping
from uuid import uuid4

from src.core.provider_audio_formats import (
    AudioInputFormat,
    AudioInputSelection,
    AudioSelectionMode,
    ProviderAudioRouteKind,
    ProviderAudioInputCapabilities,
    UnsupportedAudioInputFormat,
    require_exact_audio_input_format,
    resolve_batch_provider_audio_capabilities,
    select_audio_input_format,
)
from src.runtime.ffmpeg_commands import (
    classify_ffmpeg_stderr,
    ffprobe_audio_format_args,
    flac_transcode_args,
    mp3_transcode_args,
    ogg_opus_transcode_args,
    wav_pcm_transcode_args,
    webm_opus_transcode_args,
)
from src.runtime.media_tools import require_media_tool
from src.runtime.subprocess_utils import (
    communicate_or_kill_on_cancel,
    hidden_subprocess_kwargs,
)


_PROBE_MAX_BYTES = 64 * 1024
_PROBE_TIMEOUT_SECONDS = 20.0


class AudioFormatProbeError(ValueError):
    pass


class ProviderAudioPreparationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProbedAudioInput:
    audio_format: AudioInputFormat
    container_name: str
    codec_name: str
    sample_rate: int | None
    channels: int | None
    duration_ms: int | None
    byte_length: int


@dataclass(frozen=True, slots=True)
class PreparedProviderAudio:
    path: Path
    source_format: AudioInputFormat
    selected_format: AudioInputFormat
    selection_mode: AudioSelectionMode
    implementation: str
    content_type: str
    capability_id: str
    capability_revision: str
    byte_length: int
    generated: bool

    def frozen_request_options(self) -> dict[str, Any]:
        return {
            "audioInputFormat": self.selected_format.value,
            "audioSelectionMode": self.selection_mode.value,
            "audioPreparationImplementation": self.implementation,
            "providerAudioCapabilityId": self.capability_id,
            "providerAudioCapabilityRevision": self.capability_revision,
        }


_CONTENT_TYPES: dict[AudioInputFormat, str] = {
    AudioInputFormat.WAV_PCM16: "audio/wav",
    AudioInputFormat.WAV_PCM24: "audio/wav",
    AudioInputFormat.WAV_PCM32: "audio/wav",
    AudioInputFormat.RAW_PCM16: "audio/L16",
    AudioInputFormat.RAW_PCM32_FLOAT: "application/octet-stream",
    AudioInputFormat.MP3: "audio/mpeg",
    AudioInputFormat.FLAC: "audio/flac",
    AudioInputFormat.AAC: "audio/aac",
    AudioInputFormat.AIFF_PCM: "audio/aiff",
    AudioInputFormat.M4A_AAC: "audio/mp4",
    AudioInputFormat.M4A_ALAC: "audio/mp4",
    AudioInputFormat.OGG_VORBIS: "audio/ogg",
    AudioInputFormat.OGG_OPUS: "audio/ogg; codecs=opus",
    AudioInputFormat.WEBM_OPUS: "audio/webm; codecs=opus",
    AudioInputFormat.WEBM_VORBIS: "audio/webm; codecs=vorbis",
    AudioInputFormat.MP4_AUDIO: "audio/mp4",
    AudioInputFormat.MULAW: "audio/basic",
    AudioInputFormat.ALAW: "audio/basic",
    AudioInputFormat.AMR_NB: "audio/amr",
    AudioInputFormat.AMR_WB: "audio/amr-wb",
    AudioInputFormat.SPEEX: "audio/speex",
    AudioInputFormat.G729: "audio/G729",
}

_GENERATED_SUFFIXES: dict[AudioInputFormat, str] = {
    AudioInputFormat.WAV_PCM16: ".wav",
    AudioInputFormat.MP3: ".mp3",
    AudioInputFormat.FLAC: ".flac",
    AudioInputFormat.OGG_OPUS: ".ogg",
    AudioInputFormat.WEBM_OPUS: ".webm",
}

_GENERATED_IMPLEMENTATIONS: dict[AudioInputFormat, str] = {
    AudioInputFormat.WAV_PCM16: "ffmpeg_wav_pcm16_control",
    AudioInputFormat.MP3: "current_ffmpeg_mp3_fallback",
    AudioInputFormat.FLAC: "ffmpeg_flac_fast_control",
    AudioInputFormat.OGG_OPUS: "ogg_opus_libopus_ffmpeg_control",
    AudioInputFormat.WEBM_OPUS: "webm_opus_verified_ffmpeg",
}


def audio_preparation_implementation(
    selection: AudioInputSelection,
) -> str:
    """Return the bounded implementation identity frozen into job evidence."""

    if selection.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH:
        return "original_passthrough"
    implementation = _GENERATED_IMPLEMENTATIONS.get(selection.audio_format, "")
    if not implementation:
        raise ProviderAudioPreparationError(
            "Selected provider format has no promoted local implementation."
        )
    return implementation


def _first_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, list) or not value or not isinstance(value[0], Mapping):
        raise AudioFormatProbeError("Audio probe did not return an audio stream.")
    return value[0]


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _format_from_probe_payload(
    payload: Mapping[str, Any],
    *,
    suffix: str,
) -> tuple[AudioInputFormat, str, str]:
    stream = _first_mapping(payload.get("streams"))
    format_info = payload.get("format")
    if not isinstance(format_info, Mapping):
        format_info = {}
    codec = str(stream.get("codec_name") or "").strip().lower()
    container = str(format_info.get("format_name") or "").strip().lower()
    containers = {item.strip() for item in container.split(",") if item.strip()}
    suffix = suffix.strip().lower()

    claimed_container_groups = {
        ".wav": {"wav"},
        ".wave": {"wav"},
        ".mp3": {"mp3"},
        ".flac": {"flac"},
        ".aac": {"aac"},
        ".aif": {"aiff"},
        ".aiff": {"aiff"},
        ".ogg": {"ogg"},
        ".oga": {"ogg"},
        ".webm": {"webm"},
        ".m4a": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
        ".mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
    }
    claimed_containers = claimed_container_groups.get(suffix)
    if claimed_containers is not None and not containers & claimed_containers:
        raise AudioFormatProbeError(
            "Audio filename container does not match the probed container."
        )

    if codec == "pcm_s16le" and "wav" in containers:
        return AudioInputFormat.WAV_PCM16, container, codec
    if codec in {"pcm_s24le", "pcm_s24be"} and "wav" in containers:
        return AudioInputFormat.WAV_PCM24, container, codec
    if codec in {"pcm_s32le", "pcm_s32be"} and "wav" in containers:
        return AudioInputFormat.WAV_PCM32, container, codec
    if codec == "mp3" and "mp3" in containers:
        return AudioInputFormat.MP3, container, codec
    if codec == "flac" and "flac" in containers:
        return AudioInputFormat.FLAC, container, codec
    if codec == "aac" and "aac" in containers:
        return AudioInputFormat.AAC, container, codec
    iso_media_containers = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
    if codec == "aac" and containers & iso_media_containers:
        return (
            AudioInputFormat.M4A_AAC
            if suffix == ".m4a"
            else AudioInputFormat.MP4_AUDIO
        ), container, codec
    if codec == "alac" and containers & iso_media_containers:
        return AudioInputFormat.M4A_ALAC, container, codec
    if codec.startswith("pcm_") and "aiff" in containers:
        return AudioInputFormat.AIFF_PCM, container, codec
    if "ogg" in containers:
        if codec == "opus":
            return AudioInputFormat.OGG_OPUS, container, codec
        if codec == "vorbis":
            return AudioInputFormat.OGG_VORBIS, container, codec
        raise AudioFormatProbeError(
            "OGG container uses an unverified codec for this STT route."
        )
    if "webm" in containers:
        if codec == "opus":
            return AudioInputFormat.WEBM_OPUS, container, codec
        if codec == "vorbis":
            return AudioInputFormat.WEBM_VORBIS, container, codec
        raise AudioFormatProbeError(
            "WebM container uses an unverified codec for this STT route."
        )
    raise AudioFormatProbeError(
        "Audio container/codec pair is not recognized as an exact input format."
    )


def probe_audio_input_file(
    path: str | Path,
    *,
    ffprobe: str | None = None,
) -> ProbedAudioInput:
    source = Path(path)
    if not source.is_file():
        raise AudioFormatProbeError("Audio source is not a file.")
    tool = ffprobe or require_media_tool("ffprobe")
    try:
        result = subprocess.run(
            ffprobe_audio_format_args(tool, source),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioFormatProbeError("Audio format probe could not run.") from exc
    if result.returncode != 0:
        raise AudioFormatProbeError("Audio format probe rejected the source file.")
    if len(result.stdout) > _PROBE_MAX_BYTES:
        raise AudioFormatProbeError("Audio format probe response is too large.")
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AudioFormatProbeError("Audio format probe returned invalid JSON.") from exc
    if not isinstance(payload, Mapping):
        raise AudioFormatProbeError("Audio format probe returned an invalid object.")
    audio_format, container, codec = _format_from_probe_payload(
        payload,
        suffix=source.suffix,
    )
    stream = _first_mapping(payload.get("streams"))
    format_info = payload.get("format")
    duration_ms: int | None = None
    if isinstance(format_info, Mapping):
        try:
            duration = float(format_info.get("duration"))
        except (TypeError, ValueError):
            duration = 0.0
        if math.isfinite(duration) and duration > 0:
            duration_ms = max(1, round(duration * 1000))
    return ProbedAudioInput(
        audio_format=audio_format,
        container_name=container[:96],
        codec_name=codec[:48],
        sample_rate=_positive_int(stream.get("sample_rate")),
        channels=_positive_int(stream.get("channels")),
        duration_ms=duration_ms,
        byte_length=source.stat().st_size,
    )


def resolve_provider_audio_selection(
    *,
    provider: str,
    model: str,
    probe: ProbedAudioInput,
    custom_endpoint: bool = False,
    max_bytes: int | None = None,
) -> tuple[ProviderAudioInputCapabilities, AudioInputSelection]:
    capability = resolve_batch_provider_audio_capabilities(
        provider,
        model,
        custom_endpoint=custom_endpoint,
    )
    effective_limit = min(
        value
        for value in (capability.max_upload_bytes, max_bytes)
        if isinstance(value, int) and value > 0
    ) if any(
        isinstance(value, int) and value > 0
        for value in (capability.max_upload_bytes, max_bytes)
    ) else None
    original = probe.audio_format if effective_limit is None or probe.byte_length <= effective_limit else None
    selection = select_audio_input_format(
        capability,
        route_kind=ProviderAudioRouteKind.BATCH,
        original_format=original,
    )
    return capability, selection


def _generated_command(
    *,
    ffmpeg: str,
    source: Path,
    target: Path,
    audio_format: AudioInputFormat,
) -> list[str]:
    if audio_format == AudioInputFormat.WAV_PCM16:
        return wav_pcm_transcode_args(ffmpeg, source, target)
    if audio_format == AudioInputFormat.MP3:
        return mp3_transcode_args(ffmpeg, source, target)
    if audio_format == AudioInputFormat.FLAC:
        return flac_transcode_args(ffmpeg, source, target)
    if audio_format == AudioInputFormat.OGG_OPUS:
        return ogg_opus_transcode_args(ffmpeg, source, target)
    if audio_format == AudioInputFormat.WEBM_OPUS:
        return webm_opus_transcode_args(ffmpeg, source, target)
    raise UnsupportedAudioInputFormat(
        "Selected provider format has no promoted local preparation implementation."
    )


async def _run_generated_preparation(command: list[str], target: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )
    _stdout, stderr = await communicate_or_kill_on_cancel(
        process,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=1024 * 1024,
    )
    if process.returncode != 0:
        detail = classify_ffmpeg_stderr(
            stderr.decode("utf-8", errors="replace") if stderr else ""
        )
        raise ProviderAudioPreparationError(
            detail or "Provider audio preparation failed."
        )
    if not target.is_file() or target.stat().st_size <= 0:
        raise ProviderAudioPreparationError(
            "Provider audio preparation did not create an artifact."
        )


@asynccontextmanager
async def prepare_provider_audio_file(
    source_path: str | Path,
    *,
    provider: str,
    model: str,
    work_dir: str | Path | None = None,
    custom_endpoint: bool = False,
    max_bytes: int | None = None,
    frozen_selection: AudioInputSelection | None = None,
) -> AsyncIterator[PreparedProviderAudio]:
    """Yield one exact, verified representation and clean generated output."""

    source = Path(source_path)
    probe = await asyncio.to_thread(probe_audio_input_file, source)
    capability, selected = resolve_provider_audio_selection(
        provider=provider,
        model=model,
        probe=probe,
        custom_endpoint=custom_endpoint,
        max_bytes=max_bytes,
    )
    if frozen_selection is not None:
        if (
            frozen_selection.capability_id != capability.capability_id
            or frozen_selection.capability_revision != capability.revision
        ):
            raise ProviderAudioPreparationError(
                "Frozen provider audio capability no longer matches the route."
            )
        require_exact_audio_input_format(
            capability,
            frozen_selection.audio_format,
            route_kind=ProviderAudioRouteKind.BATCH,
        )
        if (
            frozen_selection.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH
            and frozen_selection.audio_format != probe.audio_format
        ):
            raise ProviderAudioPreparationError(
                "Frozen pass-through format does not match the probed source."
            )
        selected = frozen_selection

    generated = selected.mode != AudioSelectionMode.ORIGINAL_PASSTHROUGH
    generated_path: Path | None = None
    output_path = source
    implementation = audio_preparation_implementation(selected)
    try:
        if generated:
            suffix = _GENERATED_SUFFIXES.get(selected.audio_format)
            if not suffix or not implementation:
                raise ProviderAudioPreparationError(
                    "Selected provider format has no promoted local implementation."
                )
            destination = Path(work_dir) if work_dir is not None else source.parent
            destination = destination.resolve()
            destination.mkdir(parents=True, exist_ok=True)
            generated_path = destination / f"provider-audio-{uuid4().hex}{suffix}"
            command = _generated_command(
                ffmpeg=require_media_tool("ffmpeg"),
                source=source,
                target=generated_path,
                audio_format=selected.audio_format,
            )
            await _run_generated_preparation(command, generated_path)
            generated_probe = await asyncio.to_thread(
                probe_audio_input_file,
                generated_path,
            )
            if generated_probe.audio_format != selected.audio_format:
                raise ProviderAudioPreparationError(
                    "Generated provider audio failed exact container/codec verification."
                )
            output_path = generated_path

        byte_length = output_path.stat().st_size
        effective_limit = min(
            value
            for value in (capability.max_upload_bytes, max_bytes)
            if isinstance(value, int) and value > 0
        ) if any(
            isinstance(value, int) and value > 0
            for value in (capability.max_upload_bytes, max_bytes)
        ) else None
        if effective_limit is not None and byte_length > effective_limit:
            raise ProviderAudioPreparationError(
                "Prepared provider audio exceeds the verified upload limit."
            )
        yield PreparedProviderAudio(
            path=output_path,
            source_format=probe.audio_format,
            selected_format=selected.audio_format,
            selection_mode=selected.mode,
            implementation=implementation,
            content_type=_CONTENT_TYPES[selected.audio_format],
            capability_id=selected.capability_id,
            capability_revision=selected.capability_revision,
            byte_length=byte_length,
            generated=generated,
        )
    finally:
        if generated_path is not None:
            generated_path.unlink(missing_ok=True)


__all__ = [
    "AudioFormatProbeError",
    "PreparedProviderAudio",
    "ProbedAudioInput",
    "ProviderAudioPreparationError",
    "audio_preparation_implementation",
    "prepare_provider_audio_file",
    "probe_audio_input_file",
    "resolve_provider_audio_selection",
]
