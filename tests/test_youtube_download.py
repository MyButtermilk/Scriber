from __future__ import annotations

from pathlib import Path
import builtins
from unittest.mock import AsyncMock, patch

import pytest

from src.youtube_download import (
    YouTubeDownloadError,
    _ensure_audio_only_file,
    download_youtube_audio,
)


class _DummyProc:
    def __init__(self, *, stdout: str, stderr: str, returncode: int):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout.encode("utf-8"), self._stderr.encode("utf-8")


@pytest.mark.asyncio
async def test_download_youtube_audio_requires_url(tmp_path: Path):
    with pytest.raises(ValueError):
        await download_youtube_audio("", output_dir=tmp_path)


@pytest.mark.asyncio
async def test_download_youtube_audio_requires_yt_dlp(tmp_path: Path):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yt_dlp":
            raise ImportError("yt_dlp not available")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with patch("src.youtube_download._require_ffmpeg"):
            with patch("src.youtube_download.shutil.which", return_value=None):
                with patch(
                    "src.youtube_download.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=_DummyProc(stdout="", stderr="yt-dlp not installed", returncode=1)),
                ):
                    with pytest.raises(YouTubeDownloadError, match="yt-dlp not installed"):
                        await download_youtube_audio("https://example.com", output_dir=tmp_path)


@pytest.mark.asyncio
async def test_download_youtube_audio_requires_ffmpeg(tmp_path: Path):
    with patch("src.youtube_download.shutil.which", return_value=None):
        with pytest.raises(YouTubeDownloadError, match="ffmpeg not found"):
            await download_youtube_audio("https://example.com", output_dir=tmp_path)


@pytest.mark.asyncio
async def test_download_youtube_audio_parses_output_path(tmp_path: Path):
    out_file = tmp_path / "abc.mp3"
    out_file.write_bytes(b"fake")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yt_dlp":
            raise ImportError("yt_dlp not available")
        return real_import(name, *args, **kwargs)

    ensured = tmp_path / "abc_audio.mp3"
    ensured.write_bytes(b"audio")

    with patch("builtins.__import__", side_effect=fake_import):
        with patch("src.youtube_download.shutil.which", side_effect=lambda name: "ffmpeg" if "ffmpeg" in name else None):
            with patch(
                "src.youtube_download.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_DummyProc(stdout=str(out_file), stderr="", returncode=0)),
            ):
                with patch(
                    "src.youtube_download._ensure_audio_only_file",
                    new=AsyncMock(return_value=ensured),
                ) as ensure_mock:
                    got = await download_youtube_audio("https://example.com", output_dir=tmp_path)

    assert got == ensured
    ensure_mock.assert_awaited_once_with(out_file.resolve())


@pytest.mark.asyncio
async def test_download_youtube_audio_subprocess_falls_back_on_unavailable_format(tmp_path: Path):
    out_file = tmp_path / "abc.webm"
    out_file.write_bytes(b"fake")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yt_dlp":
            raise ImportError("yt_dlp not available")
        return real_import(name, *args, **kwargs)

    procs = [
        _DummyProc(
            stdout="",
            stderr="ERROR: [youtube] xyz: Requested format is not available. Use --list-formats",
            returncode=1,
        ),
        _DummyProc(stdout=str(out_file), stderr="", returncode=0),
    ]

    with patch("builtins.__import__", side_effect=fake_import):
        with patch("src.youtube_download.shutil.which", side_effect=lambda name: "ffmpeg" if "ffmpeg" in name else None):
            with patch(
                "src.youtube_download.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=procs),
            ) as exec_mock:
                got = await download_youtube_audio("https://example.com", output_dir=tmp_path)

    assert got == out_file.resolve()
    assert exec_mock.await_count == 2


@pytest.mark.asyncio
async def test_ensure_audio_only_file_leaves_webm_without_video(tmp_path: Path):
    webm_file = tmp_path / "audio.webm"
    webm_file.write_bytes(b"fake")

    with patch("src.youtube_download._has_video_stream", new=AsyncMock(return_value=False)):
        got = await _ensure_audio_only_file(webm_file)

    assert got == webm_file


@pytest.mark.asyncio
async def test_ensure_audio_only_file_converts_video_extension(tmp_path: Path):
    mp4_file = tmp_path / "video.mp4"
    mp4_file.write_bytes(b"fake")
    webm_file = tmp_path / "video.webm"
    webm_file.write_bytes(b"audio")

    with patch("src.youtube_download._extract_audio_track", new=AsyncMock(return_value=webm_file)) as extract_mock:
        got = await _ensure_audio_only_file(mp4_file)

    assert got == webm_file
    extract_mock.assert_awaited_once_with(mp4_file)


@pytest.mark.asyncio
async def test_ensure_audio_only_file_converts_non_webm_audio(tmp_path: Path):
    mp3_file = tmp_path / "audio.mp3"
    mp3_file.write_bytes(b"fake")
    webm_file = tmp_path / "audio.webm"
    webm_file.write_bytes(b"audio")

    with patch("src.youtube_download._extract_audio_track", new=AsyncMock(return_value=webm_file)) as extract_mock:
        got = await _ensure_audio_only_file(mp3_file)

    assert got == webm_file
    extract_mock.assert_awaited_once_with(mp3_file)

