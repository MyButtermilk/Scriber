from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional
from loguru import logger


class YouTubeDownloadError(RuntimeError):
    pass


_AUDIO_ONLY_FORMAT = "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio"
_FORMAT_SELECTORS = (
    _AUDIO_ONLY_FORMAT,
    "bestaudio/best[acodec!=none]",
    "best[ext=webm]/best[ext=mp4]/best",
)


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
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg:
        raise YouTubeDownloadError("ffmpeg not found on PATH.")


def _is_forbidden_error(message: str) -> bool:
    text = (message or "").lower()
    return "403" in text or "forbidden" in text


def _is_format_unavailable_error(message: str) -> bool:
    text = (message or "").lower()
    return "requested format is not available" in text


async def _has_video_stream(path: Path) -> bool:
    """Return True when ffprobe sees at least one video stream."""
    ffprobe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not ffprobe:
        return False

    proc = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, _stderr_b = await proc.communicate()
    return bool((stdout_b or b"").decode("utf-8", errors="replace").strip())


async def _extract_audio_track(source_path: Path) -> Path:
    """Transcode media to audio-only WebM/Opus."""
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg:
        raise YouTubeDownloadError("ffmpeg not found on PATH.")

    if source_path.suffix.lower() == ".webm":
        target_path = source_path.with_name(f"{source_path.stem}.audio.webm")
    else:
        target_path = source_path.with_suffix(".webm")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libopus",
        "-b:a",
        "64k",
        str(target_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout_b, stderr_b = await proc.communicate()
    if proc.returncode != 0 or not target_path.exists():
        err_msg = (stderr_b or b"").decode("utf-8", errors="replace").strip()
        raise YouTubeDownloadError(
            f"Failed to extract audio from downloaded video: {err_msg or f'exit code {proc.returncode}'}"
        )
    return target_path


async def _ensure_audio_only_file(path: Path) -> Path:
    """Guarantee that returned file is audio-only WebM."""
    suffix = path.suffix.lower()
    if suffix == ".webm":
        try:
            if not await _has_video_stream(path):
                return path
        except Exception as exc:
            logger.debug(f"Could not inspect WEBM streams ({path.name}): {exc}")
            return path
        logger.info(f"Downloaded WEBM contains video stream; extracting audio only: {path.name}")
    else:
        logger.info(f"Normalizing downloaded media to audio-only WebM: {path.name}")

    audio_path = await _extract_audio_track(path)
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

        def progress_hook(d: dict) -> None:
            nonlocal final_path

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
            "outtmpl": template,
            "noplaylist": True,
            # No postprocessors - keep original format for speed
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
            # Use Android client to avoid 403 errors (YouTube blocks web client more aggressively)
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }

        def _run_download():
            nonlocal final_path
            max_retries = 3
            last_error = None

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
                                final_path = out_dir / f"{video_id}.{ext}"
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

        # Run in thread to not block event loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _run_download)

        if final_path and final_path.exists():
            return await _ensure_audio_only_file(final_path)

        # Try to find the file (audio formats only)
        for ext in ["webm", "m4a", "opus", "mp3", "wav", "ogg", "flac", "aac"]:
            for f in out_dir.glob(f"*.{ext}"):
                return await _ensure_audio_only_file(f)

        raise YouTubeDownloadError("Downloaded file not found")
        
    except ImportError:
        logger.warning("yt-dlp library not available, falling back to subprocess")
    
    # Fallback to subprocess if library import fails
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
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
            # Use Android client to avoid 403 errors (YouTube blocks web client more aggressively)
            "--extractor-args",
            "youtube:player_client=android,web",
            url,
        ]

        for attempt in range(max_retries):
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await proc.communicate()
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
