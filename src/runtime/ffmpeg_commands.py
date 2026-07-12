from __future__ import annotations

import re
from pathlib import Path


DEFAULT_AUDIO_SAMPLE_RATE = 16000
DEFAULT_AUDIO_CHANNELS = 1
DEFAULT_OPUS_BITRATE = "64k"


def _path_arg(path: str | Path) -> str:
    raw = str(path)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raise ValueError("FFmpeg command builders accept only local file paths; use yt-dlp for website URLs.")
    return str(Path(raw))


def webm_opus_transcode_args(
    ffmpeg: str,
    source_path: str | Path,
    target_path: str | Path,
    *,
    bitrate: str = DEFAULT_OPUS_BITRATE,
    sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
    channels: int = DEFAULT_AUDIO_CHANNELS,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        _path_arg(source_path),
        "-vn",
        "-map",
        "0:a:0",
        "-c:a",
        "libopus",
        "-b:a",
        str(bitrate),
        "-ar",
        str(int(sample_rate)),
        "-ac",
        str(int(channels)),
        _path_arg(target_path),
    ]


def meeting_multitrack_flac_args(
    ffmpeg: str,
    microphone_clean_path: str | Path,
    system_path: str | Path,
    target_path: str | Path,
    *,
    microphone_raw_path: str | Path | None = None,
) -> list[str]:
    """Create independently addressable mic-clean/system and optional mic-raw FLAC tracks."""
    paths: list[tuple[str | Path, str]] = []
    if microphone_raw_path is not None:
        paths.append((microphone_raw_path, "Microphone raw"))
    paths.extend([(microphone_clean_path, "Microphone clean"), (system_path, "System audio")])
    return meeting_lossless_archive_args(ffmpeg, paths, target_path)


def meeting_lossless_archive_args(
    ffmpeg: str,
    tracks: list[tuple[str | Path, str]],
    target_path: str | Path,
    *,
    stream_copy: bool = False,
) -> list[str]:
    """Create a Matroska/FLAC archive with one addressable stream per source."""
    if not tracks:
        raise ValueError("A meeting archive requires at least one audio track.")
    inputs: list[str] = []
    maps: list[str] = []
    metadata: list[str] = []
    for index, (path, title) in enumerate(tracks):
        inputs.extend(["-i", _path_arg(path)])
        maps.extend(["-map", f"{index}:a:0"])
        metadata.extend([f"-metadata:s:a:{index}", f"title={title}"])
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *inputs, *maps, "-c:a", "copy" if stream_copy else "flac",
        *metadata, "-f", "matroska",
        _path_arg(target_path),
    ]


def lossless_flac_track_args(
    ffmpeg: str,
    source_path: str | Path,
    target_path: str | Path,
) -> list[str]:
    """Encode one canonical meeting PCM track as a lossless FLAC working file."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", _path_arg(source_path), "-map", "0:a:0", "-vn",
        "-c:a", "flac", "-ar", "16000", "-ac", "1", "-f", "flac",
        _path_arg(target_path),
    ]


def meeting_opus_mix_args(
    ffmpeg: str,
    microphone_path: str | Path,
    system_path: str | Path,
    target_path: str | Path,
) -> list[str]:
    return meeting_opus_playback_args(
        ffmpeg,
        [microphone_path, system_path],
        target_path,
    )


def meeting_opus_playback_args(
    ffmpeg: str,
    source_paths: list[str | Path],
    target_path: str | Path,
    *,
    timeline_origins_ms: list[int] | None = None,
) -> list[str]:
    """Create a mono Opus playback derivative from one or more source tracks."""
    if not source_paths:
        raise ValueError("Meeting playback requires at least one audio track.")
    origins = list(timeline_origins_ms or [0] * len(source_paths))
    if len(origins) != len(source_paths):
        raise ValueError("Meeting playback requires one timeline origin per source track.")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in origins):
        raise ValueError("Meeting playback timeline origins must be non-negative milliseconds.")
    inputs: list[str] = []
    for path in source_paths:
        inputs.extend(["-i", _path_arg(path)])
    delay_filters = [
        f"[{index}:a]adelay={origin}:all=1[a{index}]"
        for index, origin in enumerate(origins)
    ]
    if len(source_paths) == 1:
        filter_graph = delay_filters[0]
        output_label = "[a0]"
    else:
        labels = "".join(f"[a{index}]" for index in range(len(source_paths)))
        filter_graph = ";".join([
            *delay_filters,
            f"{labels}amix=inputs={len(source_paths)}:duration=longest:normalize=0[a]",
        ])
        output_label = "[a]"
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *inputs, "-filter_complex", filter_graph, "-map", output_label,
        "-c:a", "libopus", "-b:a", "64k", "-ar", "16000", "-ac", "1",
        _path_arg(target_path),
    ]


def mp3_transcode_args(
    ffmpeg: str,
    source_path: str | Path,
    target_path: str | Path,
    *,
    bitrate: str = "64k",
    sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
    channels: int = DEFAULT_AUDIO_CHANNELS,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        _path_arg(source_path),
        "-vn",
        "-map",
        "0:a:0",
        "-c:a",
        "libmp3lame",
        "-b:a",
        str(bitrate),
        "-ar",
        str(int(sample_rate)),
        "-ac",
        str(int(channels)),
        _path_arg(target_path),
    ]


def mp3_encode_pcm_pipe_args(
    ffmpeg: str,
    *,
    input_sample_rate: int,
    input_channels: int,
    bitrate: str = "64k",
    output_sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
    output_channels: int = DEFAULT_AUDIO_CHANNELS,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(int(input_sample_rate)),
        "-ac",
        str(int(input_channels)),
        "-i",
        "pipe:0",
        "-vn",
        "-map",
        "0:a:0",
        "-c:a",
        "libmp3lame",
        "-b:a",
        str(bitrate),
        "-ar",
        str(int(output_sample_rate)),
        "-ac",
        str(int(output_channels)),
        "-f",
        "mp3",
        "pipe:1",
    ]


def pcm_pipe_decode_args(
    ffmpeg: str,
    source_path: str | Path,
    *,
    sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
    channels: int = DEFAULT_AUDIO_CHANNELS,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        _path_arg(source_path),
        "-vn",
        "-map",
        "0:a:0",
        "-ac",
        str(int(channels)),
        "-ar",
        str(int(sample_rate)),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]


def wav_pcm_transcode_args(
    ffmpeg: str,
    source_path: str | Path,
    target_path: str | Path,
    *,
    sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
    channels: int = DEFAULT_AUDIO_CHANNELS,
) -> list[str]:
    """Normalize local media to a standard PCM WAV accepted by local ASR."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        _path_arg(source_path),
        "-vn",
        "-map",
        "0:a:0",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(int(sample_rate)),
        "-ac",
        str(int(channels)),
        "-f",
        "wav",
        _path_arg(target_path),
    ]


def ffprobe_duration_args(ffprobe: str, file_path: str | Path) -> list[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        _path_arg(file_path),
    ]


def ffprobe_video_stream_args(
    ffprobe: str,
    file_path: str | Path,
    *,
    include_all_streams: bool = False,
) -> list[str]:
    args = [
        ffprobe,
        "-v",
        "error",
    ]
    if not include_all_streams:
        args.extend(["-select_streams", "v:0"])
    args.extend([
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        _path_arg(file_path),
    ])
    return args


def classify_ffmpeg_stderr(stderr: str) -> str:
    text = (stderr or "").strip()
    lowered = text.lower()
    if "stream map" in lowered and "matches no streams" in lowered:
        return "No audio stream was found in the selected file."
    if any(
        marker in lowered
        for marker in (
            "invalid data found",
            "moov atom not found",
            "duplicate element",
            "invalid as first byte of an ebml number",
            "exceeds containing master element",
            "error opening input: end of file",
        )
    ):
        return "The media file appears to be corrupted or incomplete."
    if "unknown decoder" in lowered or "unsupported codec" in lowered or "decoder not found" in lowered:
        return "The media file uses an audio codec that this app cannot decode."
    return text
