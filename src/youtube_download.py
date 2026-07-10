from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from loguru import logger

from src.runtime.ffmpeg_commands import classify_ffmpeg_stderr, ffprobe_video_stream_args, webm_opus_transcode_args
from src.runtime.media_tools import find_media_tool, require_media_tool
from src.runtime.subprocess_utils import communicate_or_kill_on_cancel, hidden_subprocess_kwargs


class YouTubeDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class YouTubeTranscript:
    text: str
    language: str
    is_automatic: bool


_AUDIO_ONLY_FORMAT = "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio"
_FORMAT_SELECTORS = (
    _AUDIO_ONLY_FORMAT,
    "bestaudio/best[acodec!=none]",
    "best[ext=webm][acodec!=none]/best[ext=mp4][acodec!=none]/best[acodec!=none]",
)
_CONCURRENT_FRAGMENT_DOWNLOADS = 4
_MAX_CAPTION_BYTES = 16 * 1024 * 1024
_CAPTION_FORMAT_PRIORITY = {"json3": 0, "vtt": 1, "srv3": 2, "ttml": 3}


class DownloadProgress:
    """Progress information for YouTube download."""
    def __init__(self):
        self.percent: float = 0.0
        self.downloaded_bytes: int = 0
        self.total_bytes: int = 0
        self.speed: str = ""  # e.g., "1.5MiB/s"
        self.speed_bytes: float = 0.0  # bytes per second
        self.eta: str = ""  # e.g., "00:15"
        self.eta_seconds: int = 0
        self.status: str = ""  # "downloading", "finished", etc.


def _format_bytes(num_bytes: float) -> str:
    """Format bytes into human-readable string."""
    for unit in ['B', 'KiB', 'MiB', 'GiB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f}TiB"


def _format_eta(seconds: int) -> str:
    """Format ETA seconds into MM:SS or HH:MM:SS."""
    if seconds < 0:
        return "Unknown"
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        try:
            require_media_tool(tool)
        except RuntimeError as exc:
            raise YouTubeDownloadError(str(exc)) from exc


def _is_forbidden_error(message: str) -> bool:
    text = (message or "").lower()
    return "403" in text or "forbidden" in text


def _is_format_unavailable_error(message: str) -> bool:
    text = (message or "").lower()
    return "requested format is not available" in text


def _normalize_caption_language(value: str | None) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _caption_language_candidates(
    tracks: dict[str, Any],
    *,
    preferred_language: str,
    source_language: str,
    automatic: bool,
) -> list[str]:
    available = [
        str(key)
        for key, value in tracks.items()
        if key != "live_chat" and isinstance(value, list) and value
    ]
    normalized = {key: _normalize_caption_language(key) for key in available}
    preferred = _normalize_caption_language(preferred_language)
    source = _normalize_caption_language(source_language)
    requested = [] if preferred in {"", "auto"} else [preferred]
    if source:
        requested.insert(0, source)

    ranked: list[str] = []

    def add_matching(language: str, *, original_first: bool = False) -> None:
        if not language:
            return
        exact = [key for key in available if normalized[key] == language]
        originals = [key for key in available if normalized[key] == f"{language}-orig"]
        regional = [
            key
            for key in available
            if normalized[key].startswith(f"{language}-") and key not in originals
        ]
        for key in (originals + exact + regional if original_first else exact + originals + regional):
            if key not in ranked:
                ranked.append(key)

    for language in requested:
        add_matching(language, original_first=automatic)

    if automatic:
        for key in available:
            if normalized[key].endswith("-orig") and key not in ranked:
                ranked.append(key)

    for key in available:
        if key not in ranked:
            ranked.append(key)
    return ranked


def _select_caption_track(
    info: dict[str, Any],
    *,
    preferred_language: str,
) -> tuple[str, bool, dict[str, Any]] | None:
    source_language = str(info.get("language") or info.get("original_language") or "")
    for field_name, automatic in (("subtitles", False), ("automatic_captions", True)):
        tracks = info.get(field_name)
        if not isinstance(tracks, dict):
            continue
        for language in _caption_language_candidates(
            tracks,
            preferred_language=preferred_language,
            source_language=source_language,
            automatic=automatic,
        ):
            formats = tracks.get(language)
            if not isinstance(formats, list):
                continue
            candidates = [
                item
                for item in formats
                if isinstance(item, dict) and str(item.get("url") or "").strip()
            ]
            if not candidates:
                continue
            candidates.sort(
                key=lambda item: _CAPTION_FORMAT_PRIORITY.get(
                    str(item.get("ext") or "").strip().lower(),
                    100,
                )
            )
            return language, automatic, candidates[0]
    return None


def _clean_caption_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def _caption_text_from_json3_bytes(payload: bytes) -> str:
    data = json.loads(payload.decode("utf-8-sig", errors="replace"))
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return ""
    lines: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        segments = event.get("segs")
        if not isinstance(segments, list):
            continue
        raw_text = "".join(
            str(segment.get("utf8") or "")
            for segment in segments
            if isinstance(segment, dict)
        )
        for raw_line in raw_text.splitlines() or [raw_text]:
            line = _clean_caption_text(raw_line)
            if line and (not lines or line != lines[-1]):
                lines.append(line)
    return "\n".join(lines).strip()


def _caption_text_from_vtt_bytes(payload: bytes) -> str:
    text = payload.decode("utf-8-sig", errors="replace")
    lines: list[str] = []
    in_note = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            in_note = True
            continue
        if in_note:
            if not stripped:
                in_note = False
            continue
        if (
            not stripped
            or stripped == "WEBVTT"
            or "-->" in stripped
            or stripped.isdigit()
            or stripped.startswith("Kind:")
            or stripped.startswith("Language:")
        ):
            continue
        line = _clean_caption_text(stripped)
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def _parse_caption_payload(payload: bytes, extension: str) -> str:
    if extension.lower() == "json3":
        return _caption_text_from_json3_bytes(payload)
    return _caption_text_from_vtt_bytes(payload)


async def download_youtube_transcript(
    url: str,
    *,
    preferred_language: str = "auto",
) -> YouTubeTranscript | None:
    """Return YouTube-provided captions without downloading media.

    Manual subtitles are preferred over automatic captions. ``None`` means
    that the video has no usable caption track, so callers can fall back to
    the normal audio transcription path.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("Missing URL")

    def _extract() -> YouTubeTranscript | None:
        try:
            import yt_dlp
            from yt_dlp.networking import Request
        except ImportError as exc:
            raise YouTubeDownloadError("yt-dlp not installed") from exc

        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        deno_path = find_media_tool("deno")
        if deno_path:
            options["js_runtimes"] = {"deno": {"path": deno_path}}

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                return None
            selected = _select_caption_track(
                info,
                preferred_language=preferred_language,
            )
            if selected is None:
                return None
            language, automatic, caption_format = selected
            request = Request(
                str(caption_format["url"]),
                headers=caption_format.get("http_headers") or info.get("http_headers") or {},
            )
            with ydl.urlopen(request) as response:
                payload = response.read(_MAX_CAPTION_BYTES + 1)
            if len(payload) > _MAX_CAPTION_BYTES:
                raise YouTubeDownloadError("YouTube caption track is unexpectedly large")
            text = _parse_caption_payload(
                payload,
                str(caption_format.get("ext") or "vtt"),
            )
            if not text.strip():
                return None
            return YouTubeTranscript(
                text=text,
                language=language,
                is_automatic=automatic,
            )

    try:
        return await asyncio.to_thread(_extract)
    except asyncio.CancelledError:
        raise
    except YouTubeDownloadError:
        raise
    except Exception as exc:
        raise YouTubeDownloadError(f"Failed to read YouTube captions: {exc}") from exc


async def _has_video_stream(path: Path) -> bool:
    """Validate downloaded media and report whether it also contains video."""
    ffprobe = find_media_tool("ffprobe")
    if not ffprobe:
        raise YouTubeDownloadError(
            "ffprobe is required to validate downloaded YouTube audio."
        )

    proc = await asyncio.create_subprocess_exec(
        *ffprobe_video_stream_args(ffprobe, path, include_all_streams=True),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )
    stdout_b, stderr_b = await communicate_or_kill_on_cancel(
        proc,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=1024 * 1024,
    )
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        friendly = classify_ffmpeg_stderr(stderr)
        raise YouTubeDownloadError(
            "Downloaded YouTube media is incomplete or corrupted: "
            f"{friendly or f'ffprobe exited with code {proc.returncode}'}"
        )

    stream_types = {
        line.strip().lower()
        for line in (stdout_b or b"").decode("utf-8", errors="replace").splitlines()
        if line.strip()
    }
    if "audio" not in stream_types:
        raise YouTubeDownloadError(
            "Downloaded YouTube media does not contain an audio stream."
        )
    return "video" in stream_types


async def _extract_audio_track(source_path: Path) -> Path:
    """Transcode media to audio-only WebM/Opus."""
    try:
        ffmpeg = require_media_tool("ffmpeg")
    except RuntimeError as exc:
        raise YouTubeDownloadError(str(exc)) from exc

    if source_path.suffix.lower() == ".webm":
        target_path = source_path.with_name(f"{source_path.stem}.audio.webm")
    else:
        target_path = source_path.with_suffix(".webm")
    proc = await asyncio.create_subprocess_exec(
        *webm_opus_transcode_args(ffmpeg, source_path, target_path, bitrate="64k"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )
    _stdout_b, stderr_b = await communicate_or_kill_on_cancel(
        proc,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=1024 * 1024,
    )
    if proc.returncode != 0 or not target_path.exists():
        err_msg = (stderr_b or b"").decode("utf-8", errors="replace").strip()
        friendly = classify_ffmpeg_stderr(err_msg)
        raise YouTubeDownloadError(
            f"Failed to extract audio from downloaded video: {friendly or f'exit code {proc.returncode}'}"
        )
    return target_path


async def _ensure_audio_only_file(path: Path) -> Path:
    """Guarantee that returned file is audio-only WebM."""
    suffix = path.suffix.lower()
    if suffix == ".webm":
        if not await _has_video_stream(path):
            return path
        logger.info(f"Downloaded WEBM contains video stream; extracting audio only: {path.name}")
    else:
        logger.info(f"Normalizing downloaded media to audio-only WebM: {path.name}")

    audio_path = await _extract_audio_track(path)
    if await _has_video_stream(audio_path):
        audio_path.unlink(missing_ok=True)
        raise YouTubeDownloadError(
            "Normalized YouTube audio unexpectedly still contains a video stream."
        )
    try:
        if path != audio_path:
            path.unlink(missing_ok=True)
    except Exception as exc:
        logger.debug(f"Could not remove original downloaded file ({path.name}): {exc}")
    return audio_path



async def download_youtube_audio(
    url: str,
    *,
    output_dir: str | Path,
    on_progress: Optional[Callable[[DownloadProgress], None]] = None,
) -> Path:
    """Download a YouTube source and return an audio-only local file path.

    The downloader first tries audio-only selectors, then broader fallbacks for
    videos where strict selectors are unavailable. Any downloaded file that
    contains a video stream is converted to audio before returning.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("Missing URL")

    _require_ffmpeg()

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    template = str(out_dir / "%(id)s.%(ext)s")

    # Try to use yt-dlp as a library first (better progress hooks)
    try:
        import yt_dlp
        import time

        final_path: Path | None = None
        cancel_event = threading.Event()
        # A library worker cannot be force-killed while blocked inside a socket
        # call. Keep each attempt isolated so a late worker cannot recreate or
        # overwrite files belonging to a retry using the parent job directory.
        library_out_dir = out_dir / f".yt-dlp-{uuid4().hex}"
        library_out_dir.mkdir(parents=True, exist_ok=False)
        library_template = str(library_out_dir / "%(id)s.%(ext)s")

        def progress_hook(d: dict) -> None:
            nonlocal final_path

            if cancel_event.is_set():
                raise YouTubeDownloadError("YouTube download cancelled")

            if on_progress:
                progress = DownloadProgress()
                progress.status = d.get("status", "")

                if d.get("status") == "downloading":
                    # Get progress info
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes", 0)
                    speed = d.get("speed") or 0
                    eta = d.get("eta") or 0

                    progress.total_bytes = int(total)
                    progress.downloaded_bytes = int(downloaded)
                    progress.speed_bytes = float(speed) if speed else 0.0
                    progress.eta_seconds = int(eta) if eta else 0

                    if total > 0:
                        progress.percent = (downloaded / total) * 100

                    if speed:
                        progress.speed = f"{_format_bytes(speed)}/s"

                    if eta and eta > 0:
                        progress.eta = _format_eta(eta)

                    try:
                        on_progress(progress)
                    except Exception:
                        pass

                elif d.get("status") == "finished":
                    progress.percent = 100.0
                    progress.status = "finished"
                    if "filename" in d:
                        final_path = Path(d["filename"])
                    try:
                        on_progress(progress)
                    except Exception:
                        pass

        ydl_opts = {
            # Prefer audio-only formats; fallback selectors are handled in _run_download.
            "format": _FORMAT_SELECTORS[0],
            "outtmpl": library_template,
            "noplaylist": True,
            # No postprocessors - keep original format for speed
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 15,
            # Recent yt-dlp versions select clients based on EJS/runtime and
            # PO-token availability. Do not override those maintained defaults.
            "concurrent_fragment_downloads": _CONCURRENT_FRAGMENT_DOWNLOADS,
        }
        deno_path = find_media_tool("deno")
        if deno_path:
            ydl_opts["js_runtimes"] = {"deno": {"path": deno_path}}

        def _run_download():
            nonlocal final_path
            max_retries = 3
            last_error = None
            try:
                for format_selector in _FORMAT_SELECTORS:
                    ydl_opts["format"] = format_selector
                    for attempt in range(max_retries):
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                info = ydl.extract_info(url, download=True)
                                if info:
                                    # Get the final filename (no post-processing, so ext comes from format)
                                    video_id = info.get("id", "video")
                                    ext = info.get("ext", "webm")
                                    final_path = library_out_dir / f"{video_id}.{ext}"
                                return  # Success
                        except Exception as e:
                            last_error = e
                            error_str = str(e)

                            # Retry on transient 403 errors
                            if _is_forbidden_error(error_str) and attempt < max_retries - 1:
                                delay = 2.0 + attempt * 2.0  # Increasing delay: 2s, 4s, 6s
                                logger.warning(f"YouTube download got 403 error, retrying in {delay}s ({attempt + 1}/{max_retries})...")
                                time.sleep(delay)
                                continue

                            # Try broader selectors when format is not available.
                            if _is_format_unavailable_error(error_str):
                                logger.warning(
                                    f"YouTube format selector unavailable ({format_selector}); trying fallback selector."
                                )
                                break

                            # For other errors, abort immediately.
                            raise

                # If we exhausted all retries
                if last_error:
                    raise last_error
            finally:
                if cancel_event.is_set():
                    shutil.rmtree(library_out_dir, ignore_errors=True)

        # Run in thread to not block event loop
        loop = asyncio.get_running_loop()
        download_future = loop.run_in_executor(None, _run_download)
        try:
            await asyncio.shield(download_future)
        except asyncio.CancelledError:
            cancel_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(download_future), timeout=2.0)
            except Exception:
                pass
            raise

        if final_path and final_path.exists():
            return await _ensure_audio_only_file(final_path)

        # Try to find the file (audio formats only)
        for ext in ["webm", "m4a", "opus", "mp3", "wav", "ogg", "flac", "aac"]:
            for f in library_out_dir.glob(f"*.{ext}"):
                return await _ensure_audio_only_file(f)

        raise YouTubeDownloadError("Downloaded file not found")
        
    except ImportError:
        logger.warning("yt-dlp library not available, falling back to subprocess")
    
    # Fallback to subprocess if library import fails
    exe = find_media_tool("yt-dlp")
    if not exe:
        exe_cmd = [sys.executable, "-m", "yt_dlp"]
    else:
        exe_cmd = [exe]

    # Retry logic for 403 errors and format selector fallbacks
    max_retries = 3
    last_error_msg = ""
    stdout = ""
    stderr = ""
    proc = None

    for format_selector in _FORMAT_SELECTORS:
        args = [
            *exe_cmd,
            "--no-playlist",
            "-f",
            format_selector,
            "-o",
            template,
            "--print",
            "after_move:filepath",
            "--no-progress",
            "--concurrent-fragments",
            str(_CONCURRENT_FRAGMENT_DOWNLOADS),
            url,
        ]
        deno_path = find_media_tool("deno")
        if deno_path:
            args[-1:-1] = ["--js-runtimes", f"deno:{deno_path}"]

        for attempt in range(max_retries):
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **hidden_subprocess_kwargs(),
            )
            stdout_b, stderr_b = await communicate_or_kill_on_cancel(
                proc,
                max_stdout_bytes=1024 * 1024,
                max_stderr_bytes=1024 * 1024,
            )
            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")

            if proc.returncode == 0:
                break

            last_error_msg = stderr.strip() or stdout.strip() or f"yt-dlp exited with code {proc.returncode}"

            if _is_forbidden_error(last_error_msg) and attempt < max_retries - 1:
                delay = 2.0 + attempt * 2.0  # Increasing delay: 2s, 4s, 6s
                logger.warning(f"YouTube download got 403 error, retrying in {delay}s ({attempt + 1}/{max_retries})...")
                await asyncio.sleep(delay)
                continue

            if _is_format_unavailable_error(last_error_msg):
                logger.warning(
                    f"YouTube format selector unavailable ({format_selector}); trying fallback selector."
                )
                break

            raise YouTubeDownloadError(last_error_msg)

        if proc and proc.returncode == 0:
            break
    else:
        raise YouTubeDownloadError(last_error_msg or "yt-dlp failed to download audio")

    lines = [ln.strip().strip('"') for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        raise YouTubeDownloadError("yt-dlp did not report an output file path.")

    path = Path(lines[-1])
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        raise YouTubeDownloadError(f"Downloaded file not found: {path}")
    return await _ensure_audio_only_file(path)
