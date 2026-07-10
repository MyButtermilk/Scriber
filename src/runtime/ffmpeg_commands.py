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
