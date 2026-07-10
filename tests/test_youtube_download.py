from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
import builtins
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.youtube_download import (
    YouTubeDownloadError,
    _ensure_audio_only_file,
    _extract_audio_track,
    _has_video_stream,
    download_youtube_audio,
)


class _DummyProc:
    def __init__(self, *, stdout: str, stderr: str, returncode: int):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout.encode("utf-8"), self._stderr.encode("utf-8")


class _CancelledProc:
    returncode = None

    def __init__(self):
        self.killed = False
        self.waited = False

    async def communicate(self):
        raise asyncio.CancelledError()

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True


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
            with patch("src.youtube_download.find_media_tool", return_value=None):
                with patch(
                    "src.youtube_download.asyncio.create_subprocess_exec",
                    new=AsyncMock(
                        return_value=_DummyProc(
                            stdout="",
                            stderr="yt-dlp not installed",
                            returncode=1,
                        )
                    ),
                ):
                    with pytest.raises(YouTubeDownloadError, match="yt-dlp not installed"):
                        await download_youtube_audio("https://example.com", output_dir=tmp_path)


@pytest.mark.asyncio
async def test_download_youtube_audio_requires_ffmpeg(tmp_path: Path):
    with patch(
        "src.youtube_download.require_media_tool",
        side_effect=RuntimeError("ffmpeg not found"),
    ):
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
        with patch("src.youtube_download.require_media_tool", return_value="ffmpeg"):
            with patch("src.youtube_download.find_media_tool", return_value=None):
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
        with patch("src.youtube_download.require_media_tool", return_value="ffmpeg"):
            with patch("src.youtube_download.find_media_tool", return_value=None):
                with patch(
                    "src.youtube_download.asyncio.create_subprocess_exec",
                    new=AsyncMock(side_effect=procs),
                ) as exec_mock:
                    with patch(
                        "src.youtube_download._ensure_audio_only_file",
                        new=AsyncMock(return_value=out_file.resolve()),
                    ):
                        got = await download_youtube_audio("https://example.com", output_dir=tmp_path)

    assert got == out_file.resolve()
    assert exec_mock.await_count == 2


@pytest.mark.asyncio
async def test_download_youtube_audio_uses_deno_and_current_default_clients(
    monkeypatch,
    tmp_path: Path,
):
    captured_options: dict = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured_options.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            assert download is True
            output_path = Path(
                captured_options["outtmpl"]
                .replace("%(id)s", "video-id")
                .replace("%(ext)s", "webm")
            )
            output_path.write_bytes(b"audio")
            return {"id": "video-id", "ext": "webm"}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    deno_path = tmp_path / "deno.exe"
    deno_path.write_bytes(b"deno")
    with patch("src.youtube_download._require_ffmpeg"):
        with patch("src.youtube_download.find_media_tool", return_value=str(deno_path)):
            with patch(
                "src.youtube_download._ensure_audio_only_file",
                new=AsyncMock(side_effect=lambda path: path),
            ):
                result = await download_youtube_audio(
                    "https://www.youtube.com/watch?v=video-id",
                    output_dir=tmp_path / "downloads",
                )

    assert result.name == "video-id.webm"
    assert "extractor_args" not in captured_options
    assert captured_options["js_runtimes"] == {
        "deno": {"path": str(deno_path)}
    }
    assert captured_options["concurrent_fragment_downloads"] == 4


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
        with patch("src.youtube_download._has_video_stream", new=AsyncMock(return_value=False)):
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
        with patch("src.youtube_download._has_video_stream", new=AsyncMock(return_value=False)):
            got = await _ensure_audio_only_file(mp3_file)

    assert got == webm_file
    extract_mock.assert_awaited_once_with(mp3_file)


@pytest.mark.asyncio
async def test_has_video_stream_kills_ffprobe_on_cancel(tmp_path: Path):
    proc = _CancelledProc()

    with patch("src.youtube_download.find_media_tool", return_value="ffprobe"):
        with patch(
            "src.youtube_download.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _has_video_stream(tmp_path / "video.webm")

    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_has_video_stream_rejects_corrupted_download(tmp_path: Path):
    proc = _DummyProc(
        stdout="",
        stderr="[matroska,webm] Duplicate element\nError opening input: End of file",
        returncode=1,
    )

    with patch("src.youtube_download.find_media_tool", return_value="ffprobe"):
        with patch(
            "src.youtube_download.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with pytest.raises(YouTubeDownloadError, match="incomplete or corrupted"):
                await _has_video_stream(tmp_path / "broken.webm")


@pytest.mark.asyncio
async def test_has_video_stream_requires_audio_stream(tmp_path: Path):
    proc = _DummyProc(stdout="video\n", stderr="", returncode=0)

    with patch("src.youtube_download.find_media_tool", return_value="ffprobe"):
        with patch(
            "src.youtube_download.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with pytest.raises(YouTubeDownloadError, match="does not contain an audio stream"):
                await _has_video_stream(tmp_path / "storyboard.mp4")


@pytest.mark.asyncio
async def test_extract_audio_track_kills_ffmpeg_on_cancel(tmp_path: Path):
    proc = _CancelledProc()

    with patch("src.youtube_download.require_media_tool", return_value="ffmpeg"):
        with patch(
            "src.youtube_download.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _extract_audio_track(tmp_path / "video.mp4")

    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_download_youtube_audio_subprocess_kills_yt_dlp_on_cancel(tmp_path: Path):
    proc = _CancelledProc()
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yt_dlp":
            raise ImportError("yt_dlp not available")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with patch("src.youtube_download._require_ffmpeg"):
            with patch("src.youtube_download.find_media_tool", return_value=None):
                with patch(
                    "src.youtube_download.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=proc),
                ):
                    with pytest.raises(asyncio.CancelledError):
                        await download_youtube_audio("https://example.com", output_dir=tmp_path)

    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_download_youtube_audio_library_stops_worker_on_cancel(monkeypatch, tmp_path: Path):
    started = threading.Event()
    stopped = threading.Event()

    class FakeYoutubeDL:
        def __init__(self, options):
            self._hook = options["progress_hooks"][0]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            assert download is True
            started.set()
            try:
                while True:
                    self._hook(
                        {
                            "status": "downloading",
                            "downloaded_bytes": 1,
                            "total_bytes": 10,
                        }
                    )
                    time.sleep(0.01)
            finally:
                stopped.set()

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    with patch("src.youtube_download._require_ffmpeg"):
        task = asyncio.create_task(
            download_youtube_audio("https://example.com", output_dir=tmp_path)
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert await asyncio.to_thread(stopped.wait, 1.0)
    assert not list(tmp_path.glob(".yt-dlp-*"))
