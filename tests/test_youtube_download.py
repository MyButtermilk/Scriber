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
    YouTubeCaptionCue,
    YouTubeDownloadError,
    _caption_cues_from_json3_bytes,
    _caption_cues_from_vtt_bytes,
    _caption_text_from_json3_bytes,
    _caption_text_from_vtt_bytes,
    _ensure_audio_only_file,
    _extract_audio_track,
    _has_video_stream,
    _parse_caption_payload,
    _select_caption_track,
    download_youtube_audio,
    download_youtube_transcript,
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


def test_caption_track_prefers_manual_subtitles_over_automatic_translation():
    selected = _select_caption_track(
        {
            "language": "en",
            "subtitles": {
                "de": [{"ext": "vtt", "url": "https://example.test/manual-de"}],
            },
            "automatic_captions": {
                "de": [{"ext": "json3", "url": "https://example.test/automatic-de"}],
                "en-orig": [{"ext": "json3", "url": "https://example.test/automatic-en"}],
            },
        },
        preferred_language="de",
    )

    assert selected is not None
    language, automatic, caption_format = selected
    assert language == "de"
    assert automatic is False
    assert caption_format["url"].endswith("manual-de")


def test_caption_track_prefers_original_automatic_language():
    selected = _select_caption_track(
        {
            "language": "en",
            "automatic_captions": {
                "de": [{"ext": "json3", "url": "https://example.test/de"}],
                "en": [{"ext": "vtt", "url": "https://example.test/en"}],
                "en-orig": [{"ext": "json3", "url": "https://example.test/en-orig"}],
            },
        },
        preferred_language="auto",
    )

    assert selected is not None
    language, automatic, caption_format = selected
    assert language == "en-orig"
    assert automatic is True
    assert caption_format["ext"] == "json3"


def test_caption_parsers_remove_transport_markup_and_duplicate_lines():
    json3 = b'{"events":[{"segs":[{"utf8":"Hello "},{"utf8":"world"}]},{"segs":[{"utf8":"Hello world"}]},{"segs":[{"utf8":"Next line"}]}]}'
    vtt = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<c>Hello world</c>\n\n00:00:01.000 --> 00:00:02.000\nHello world\nNext line\n"

    assert _caption_text_from_json3_bytes(json3) == "Hello world\nNext line"
    assert _caption_text_from_vtt_bytes(vtt) == "Hello world\nNext line"


def test_json3_caption_cues_preserve_provider_times_and_estimate_only_to_next_start():
    payload = b'''{
      "events": [
        {"tStartMs": 1000, "dDurationMs": 500, "segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
        {"dDurationMs": 100, "segs": [{"utf8": "missing start"}]},
        {"tStartMs": 2000, "segs": [{"utf8": "Estimated ending"}]},
        {"tStartMs": 2600, "dDurationMs": 400, "segs": [{"utf8": "Exact ending"}]},
        {"tStartMs": 3200, "segs": [{"utf8": "no defensible ending"}]}
      ]
    }'''

    assert _caption_cues_from_json3_bytes(payload) == (
        YouTubeCaptionCue(1000, 1500, "Hello world"),
        YouTubeCaptionCue(2000, 2600, "Estimated ending", "estimated"),
        YouTubeCaptionCue(2600, 3000, "Exact ending"),
    )


def test_vtt_caption_cues_parse_each_valid_timing_line_and_clean_markup():
    payload = b'''WEBVTT

first
00:00:01.250 --> 00:00:02.750 align:start position:0%
<v Alice>Hello &amp; <b>world</b></v>

bad
not-a-time --> 00:00:04.000
Discard me

backwards
00:00:05.000 --> 00:00:04.000
Discard me too

00:01:02.000 --> 00:01:03.125
Second line
'''

    assert _caption_cues_from_vtt_bytes(payload) == (
        YouTubeCaptionCue(1250, 2750, "Hello & world"),
        YouTubeCaptionCue(62000, 63125, "Second line"),
    )


def test_caption_cues_keep_time_separated_repeated_text():
    payload = b'''{
      "events": [
        {"tStartMs": 0, "dDurationMs": 500, "segs": [{"utf8": "Thank you"}]},
        {"tStartMs": 5000, "dDurationMs": 500, "segs": [{"utf8": "Thank you"}]}
      ]
    }'''

    cues = _caption_cues_from_json3_bytes(payload)

    assert [cue.text for cue in cues] == ["Thank you", "Thank you"]
    assert [cue.start_ms for cue in cues] == [0, 5000]


def test_caption_cues_collapse_only_immediately_overlapping_exact_duplicates():
    payload = b'''{
      "events": [
        {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Repeat"}]},
        {"tStartMs": 500, "dDurationMs": 1000, "segs": [{"utf8": "Repeat"}]},
        {"tStartMs": 1500, "dDurationMs": 500, "segs": [{"utf8": "Repeat"}]}
      ]
    }'''

    assert _caption_cues_from_json3_bytes(payload) == (
        YouTubeCaptionCue(0, 1500, "Repeat", "estimated"),
        YouTubeCaptionCue(1500, 2000, "Repeat"),
    )


def test_rolling_caption_prefix_is_trimmed_only_for_immediate_overlap():
    payload = b'''{
      "events": [
        {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Hello"}]},
        {"tStartMs": 500, "dDurationMs": 1000, "segs": [{"utf8": "Hello world"}]},
        {"tStartMs": 3000, "dDurationMs": 500, "segs": [{"utf8": "Hello again"}]}
      ]
    }'''

    assert _caption_cues_from_json3_bytes(payload) == (
        YouTubeCaptionCue(0, 1000, "Hello"),
        YouTubeCaptionCue(500, 1500, "world", "estimated"),
        YouTubeCaptionCue(3000, 3500, "Hello again"),
    )


@pytest.mark.parametrize(
    ("payload", "extension"),
    [
        (b'{"events":[{"segs":[{"utf8":"text only"}]}]}', "json3"),
        (b"{not-json", "json3"),
        (b"WEBVTT\n\ntext without a timing line\n", "vtt"),
        (b"<transcript><text>legacy untimed text</text></transcript>", "srv3"),
    ],
)
def test_structured_caption_parser_rejects_malformed_or_untimed_payloads(
    payload: bytes,
    extension: str,
):
    assert _parse_caption_payload(payload, extension) == ()


@pytest.mark.asyncio
async def test_download_youtube_transcript_returns_none_for_untimed_caption_track(
    monkeypatch,
):
    captured_options: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"events":[{"segs":[{"utf8":"text only"}]}]}'

    class FakeYoutubeDL:
        def __init__(self, options):
            captured_options.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            assert download is False
            return {
                "language": "en",
                "subtitles": {
                    "en": [{"ext": "json3", "url": "https://example.test/captions"}],
                },
            }

        def urlopen(self, _request):
            return FakeResponse()

    class FakeRequest:
        def __init__(self, url, *, headers):
            self.url = url
            self.headers = headers

    yt_dlp_module = types.ModuleType("yt_dlp")
    yt_dlp_module.YoutubeDL = FakeYoutubeDL
    networking_module = types.ModuleType("yt_dlp.networking")
    networking_module.Request = FakeRequest
    monkeypatch.setitem(sys.modules, "yt_dlp", yt_dlp_module)
    monkeypatch.setitem(sys.modules, "yt_dlp.networking", networking_module)
    monkeypatch.setattr(
        "src.youtube_download._apply_youtube_only_runtime_policy", lambda: None
    )

    assert await download_youtube_transcript("https://example.test/video") is None
    assert captured_options["allowed_extractors"] == [r"youtube.*"]
    assert captured_options["remote_components"] == []
    assert captured_options["js_runtimes"] == {}


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
                ) as exec_mock:
                    with patch(
                        "src.youtube_download._ensure_audio_only_file",
                        new=AsyncMock(return_value=ensured),
                    ) as ensure_mock:
                        got = await download_youtube_audio("https://example.com", output_dir=tmp_path)

    assert got == ensured
    ensure_mock.assert_awaited_once_with(out_file.resolve())
    command = exec_mock.await_args.args
    assert "--no-config" in command
    assert "--no-plugin-dirs" in command
    assert "--no-remote-components" in command
    assert command[command.index("--use-extractors") + 1] == "youtube.*"
    assert command.count("--no-js-runtimes") == 1


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
async def test_download_youtube_audio_uses_quickjs_and_current_default_clients(
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
    monkeypatch.setattr(
        "src.youtube_download._apply_youtube_only_runtime_policy", lambda: None
    )
    quickjs_path = tmp_path / "qjs.exe"
    quickjs_path.write_bytes(b"quickjs")
    with patch("src.youtube_download._require_ffmpeg"):
        with patch(
            "src.youtube_download.find_media_tool", return_value=str(quickjs_path)
        ):
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
        "quickjs": {"path": str(quickjs_path)}
    }
    assert captured_options["concurrent_fragment_downloads"] == 4
    assert captured_options["noprogress"] is True
    assert captured_options["socket_timeout"] == 15
    assert captured_options["retries"] == 3
    assert captured_options["fragment_retries"] == 3
    assert captured_options["extractor_retries"] == 3
    assert captured_options["allowed_extractors"] == [r"youtube.*"]
    assert captured_options["remote_components"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("use_library", [True, False], ids=["library", "subprocess-fallback"])
async def test_download_youtube_audio_attests_quickjs_without_blocking_event_loop(
    monkeypatch,
    tmp_path: Path,
    use_library: bool,
):
    """A timeout-like bound self-test must never stall aiohttp's event loop."""

    from src.runtime import media_tools

    app_root = tmp_path / "app"
    quickjs_path = app_root / "tools" / "ffmpeg" / "qjs.exe"
    quickjs_path.parent.mkdir(parents=True)
    quickjs_path.write_bytes(b"locked wrapper identity is stubbed below")

    self_test_started = threading.Event()
    self_test_finished = threading.Event()
    release_self_test = threading.Event()
    heartbeat_observed = asyncio.Event()
    self_test_candidates: list[Path] = []

    def timeout_like_self_test(candidate: Path) -> bool:
        assert candidate == quickjs_path.resolve()
        self_test_candidates.append(candidate)
        self_test_started.set()
        # If resolution accidentally runs on the event-loop thread, the
        # heartbeat cannot release this gate before its bounded timeout.
        released_by_heartbeat = release_self_test.wait(timeout=2.0)
        self_test_finished.set()
        return released_by_heartbeat

    async def event_loop_heartbeat() -> None:
        while not self_test_started.is_set():
            await asyncio.sleep(0)
        if not self_test_finished.is_set():
            heartbeat_observed.set()
            release_self_test.set()

    monkeypatch.setattr(media_tools, "is_frozen", lambda: True)
    monkeypatch.setattr(media_tools, "app_root", lambda: app_root)
    monkeypatch.setattr(
        media_tools,
        "_locked_runtime_file_matches",
        lambda _parent, _identity: True,
    )
    monkeypatch.setattr(media_tools, "_quickjs_self_test_matches", timeout_like_self_test)
    monkeypatch.setattr(
        "src.youtube_download._apply_youtube_only_runtime_policy", lambda: None
    )

    out_file = tmp_path / "downloaded.webm"
    out_file.write_bytes(b"audio")
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

    real_import = builtins.__import__

    def import_with_optional_fallback(name, *args, **kwargs):
        if name == "yt_dlp" and not use_library:
            raise ImportError("exercise subprocess fallback")
        return real_import(name, *args, **kwargs)

    if use_library:
        monkeypatch.setitem(
            sys.modules,
            "yt_dlp",
            types.SimpleNamespace(YoutubeDL=FakeYoutubeDL),
        )

    heartbeat_task = asyncio.create_task(event_loop_heartbeat())
    with patch("builtins.__import__", side_effect=import_with_optional_fallback):
        with patch("src.youtube_download._require_ffmpeg"):
            with patch(
                "src.youtube_download.asyncio.create_subprocess_exec",
                new=AsyncMock(
                    return_value=_DummyProc(
                        stdout=str(out_file),
                        stderr="",
                        returncode=0,
                    )
                ),
            ) as subprocess_exec:
                with patch(
                    "src.youtube_download._ensure_audio_only_file",
                    new=AsyncMock(side_effect=lambda path: path),
                ):
                    result = await asyncio.wait_for(
                        download_youtube_audio(
                            "https://www.youtube.com/watch?v=video-id",
                            output_dir=tmp_path / "downloads",
                        ),
                        timeout=3.0,
                    )

    await asyncio.wait_for(heartbeat_task, timeout=3.0)
    assert heartbeat_observed.is_set()
    assert self_test_finished.is_set()
    assert self_test_candidates == [quickjs_path.resolve()]
    if use_library:
        assert result.name == "video-id.webm"
        assert captured_options["js_runtimes"] == {
            "quickjs": {"path": str(quickjs_path.resolve())}
        }
        subprocess_exec.assert_not_awaited()
    else:
        assert result == out_file.resolve()
        command = subprocess_exec.await_args.args
        runtime_index = command.index("--js-runtimes")
        assert command[runtime_index + 1] == f"quickjs:{quickjs_path.resolve()}"


@pytest.mark.asyncio
async def test_download_youtube_audio_library_failure_cleans_attempt_directory(monkeypatch, tmp_path: Path):
    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            assert download is True
            raise RuntimeError("synthetic yt-dlp failure")

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(
        "src.youtube_download._apply_youtube_only_runtime_policy", lambda: None
    )
    with patch("src.youtube_download._require_ffmpeg"):
        with pytest.raises(RuntimeError, match="synthetic yt-dlp failure"):
            await download_youtube_audio("https://example.com", output_dir=tmp_path)

    assert not list(tmp_path.glob(".yt-dlp-*"))


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
    monkeypatch.setattr(
        "src.youtube_download._apply_youtube_only_runtime_policy", lambda: None
    )
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
