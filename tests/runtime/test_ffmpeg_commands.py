from __future__ import annotations

from pathlib import Path

import pytest

from src.runtime.ffmpeg_commands import (
    classify_ffmpeg_stderr,
    lossless_flac_track_args,
    meeting_lossless_archive_args,
    mp3_encode_pcm_pipe_args,
    mp3_transcode_args,
    pcm_pipe_decode_args,
    wav_pcm_transcode_args,
    webm_opus_transcode_args,
)


def test_lossless_meeting_work_track_and_stream_copy_archive_args(tmp_path: Path) -> None:
    source = tmp_path / "system.wav"
    working = tmp_path / "system.work.flac"
    archive = tmp_path / "meeting-tracks.mka"

    encode = lossless_flac_track_args("ffmpeg", source, working)
    assert encode[encode.index("-c:a") + 1] == "flac"
    assert encode[encode.index("-ar") + 1] == "16000"
    assert encode[encode.index("-ac") + 1] == "1"
    assert encode[encode.index("-f") + 1] == "flac"
    assert encode[-1] == str(working)

    remux = meeting_lossless_archive_args(
        "ffmpeg",
        [(working, "System audio")],
        archive,
        stream_copy=True,
    )
    assert remux[remux.index("-c:a") + 1] == "copy"
    assert remux[remux.index("-f") + 1] == "matroska"
    assert remux[-1] == str(archive)


def test_webm_opus_transcode_args_are_local_audio_only_and_encoded(tmp_path: Path) -> None:
    source = tmp_path / "input file.mp4"
    target = tmp_path / "output file.webm"

    args = webm_opus_transcode_args("ffmpeg", source, target, bitrate="64k")

    assert args == [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-map",
        "0:a:0",
        "-c:a",
        "libopus",
        "-b:a",
        "64k",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(target),
    ]
    assert "http://" not in " ".join(args)
    assert "https://" not in " ".join(args)


def test_ffmpeg_command_builders_reject_remote_urls(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="yt-dlp"):
        webm_opus_transcode_args("ffmpeg", "https://www.youtube.com/watch?v=abc", tmp_path / "out.webm")


def test_mp3_transcode_args_are_encoded_not_wav_pcm(tmp_path: Path) -> None:
    source = tmp_path / "input.webm"
    target = tmp_path / "prepared.mp3"

    args = mp3_transcode_args("ffmpeg", source, target, bitrate="64k")

    assert "-c:a" in args
    assert args[args.index("-c:a") + 1] == "libmp3lame"
    assert args[args.index("-b:a") + 1] == "64k"
    assert "pcm_s16le" not in args
    assert "wav" not in args
    assert str(target) in args


def test_mp3_encode_pcm_pipe_args_use_encoded_stdout_not_wav() -> None:
    args = mp3_encode_pcm_pipe_args(
        "ffmpeg",
        input_sample_rate=48000,
        input_channels=2,
        bitrate="64k",
    )

    assert "pipe:0" in args
    assert "pipe:1" in args
    assert args[args.index("-f") + 1] == "s16le"
    assert args[args.index("-c:a") + 1] == "libmp3lame"
    assert args[args.index("-b:a") + 1] == "64k"
    assert "wav" not in args


def test_pcm_pipe_decode_args_write_raw_stdout_only(tmp_path: Path) -> None:
    source = tmp_path / "input.m4a"

    args = pcm_pipe_decode_args("ffmpeg", source)

    assert args[-1] == "-"
    assert "-f" in args
    assert args[args.index("-f") + 1] == "s16le"
    assert "pcm_s16le" in args


def test_wav_pcm_transcode_args_create_standard_local_wav(tmp_path: Path) -> None:
    source = tmp_path / "input.mp3"
    target = tmp_path / "prepared.wav"

    args = wav_pcm_transcode_args("ffmpeg", source, target)

    assert args[args.index("-c:a") + 1] == "pcm_s16le"
    assert args[args.index("-ar") + 1] == "16000"
    assert args[args.index("-ac") + 1] == "1"
    assert args[args.index("-f") + 1] == "wav"
    assert args[-1] == str(target)


def test_classify_ffmpeg_stderr_maps_common_user_failures() -> None:
    assert classify_ffmpeg_stderr("Stream map '0:a:0' matches no streams.") == (
        "No audio stream was found in the selected file."
    )
    assert classify_ffmpeg_stderr("moov atom not found") == "The media file appears to be corrupted or incomplete."
    assert classify_ffmpeg_stderr("Duplicate element; Error opening input: End of file") == (
        "The media file appears to be corrupted or incomplete."
    )
    assert classify_ffmpeg_stderr("Decoder not found") == (
        "The media file uses an audio codec that this app cannot decode."
    )
