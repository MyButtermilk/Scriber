from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import sys
import pytest

from src.youtube_download import YouTubeDownloadError, download_youtube_audio


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
    with patch.dict("sys.modules", {"yt_dlp": None}):
        # We need to mock _require_ffmpeg because it might fail before checking yt-dlp
        # Patching shutil.which to return True for ffmpeg check
        with patch("src.youtube_download.shutil.which", side_effect=lambda x: "/usr/bin/ffmpeg" if "ffmpeg" in x else None):
            with patch("src.youtube_download._find_yt_dlp_command", return_value=[sys.executable, "-c", "import sys; sys.exit(1)"]):
                with pytest.raises(YouTubeDownloadError):
                    await download_youtube_audio("https://example.com", output_dir=tmp_path)


@pytest.mark.asyncio
async def test_download_youtube_audio_requires_ffmpeg(tmp_path: Path):
    with patch.dict("sys.modules", {"yt_dlp": None}):
        with patch("src.youtube_download._find_yt_dlp_command", return_value=["yt-dlp"]):
            with patch("src.youtube_download.shutil.which", return_value=None):
                with pytest.raises(YouTubeDownloadError, match="ffmpeg not found"):
                    await download_youtube_audio("https://example.com", output_dir=tmp_path)


@pytest.mark.asyncio
async def test_download_youtube_audio_parses_output_path(tmp_path: Path):
    out_file = tmp_path / "abc.mp3"
    out_file.write_bytes(b"fake")

    with patch.dict("sys.modules", {"yt_dlp": None}):
        with patch("src.youtube_download._find_yt_dlp_command", return_value=["yt-dlp"]):
            with patch("src.youtube_download.shutil.which", side_effect=lambda name: "ffmpeg" if "ffmpeg" in name else None):
                with patch(
                    "src.youtube_download.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=_DummyProc(stdout=str(out_file), stderr="", returncode=0)),
                ):
                    got = await download_youtube_audio("https://example.com", output_dir=tmp_path)

    assert got == out_file.resolve()

