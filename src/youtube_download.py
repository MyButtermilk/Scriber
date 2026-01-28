from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional
from loguru import logger


class YouTubeDownloadError(RuntimeError):
    pass


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



async def download_youtube_audio(
    url: str,
    *,
    output_dir: str | Path,
    on_progress: Optional[Callable[[DownloadProgress], None]] = None,
) -> Path:
    """Download audio from YouTube video directly as webm (no conversion needed).
    
    Downloads only the audio stream in webm format, which is natively supported
    by Soniox STT. This avoids unnecessary FFmpeg conversion overhead.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("Missing URL")

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
            # Prefer webm audio (Opus codec), fall back to any audio format
            "format": "bestaudio[ext=webm]/bestaudio",
            "outtmpl": template,
            "noplaylist": True,
            # No postprocessors - keep original format for speed
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
        }

        def _run_download():
            nonlocal final_path
            max_retries = 3
            last_error = None

            for attempt in range(max_retries):
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        if info:
                            # Get the final filename (no post-processing, so ext comes from format)
                            video_id = info.get("id", "video")
                            ext = info.get("ext", "webm")
                            final_path = out_dir / f"{video_id}.{ext}"
                        return  # Success, exit retry loop
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()
                    # Retry on 403 Forbidden errors (often transient)
                    if "403" in error_str or "forbidden" in error_str:
                        if attempt < max_retries - 1:
                            logger.warning(f"YouTube download got 403 error, retrying ({attempt + 1}/{max_retries})...")
                            time.sleep(1.0 + attempt * 0.5)  # Increasing delay: 1s, 1.5s, 2s
                            continue
                    # For other errors or final retry, raise immediately
                    raise

            # If we exhausted all retries
            if last_error:
                raise last_error

        # Run in thread to not block event loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _run_download)

        if final_path and final_path.exists():
            return final_path

        # Try to find the file (webm preferred, then any audio)
        for ext in ["webm", "m4a", "opus", "mp3"]:
            for f in out_dir.glob(f"*.{ext}"):
                return f

        raise YouTubeDownloadError("Downloaded file not found")
        
    except ImportError:
        logger.warning("yt-dlp library not available, falling back to subprocess")
    
    # Fallback to subprocess if library import fails
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if not exe:
        exe_cmd = [sys.executable, "-m", "yt_dlp"]
    else:
        exe_cmd = [exe]

    args = [
        *exe_cmd,
        "--no-playlist",
        "-f",
        "bestaudio[ext=webm]/bestaudio",  # Prefer webm, no conversion
        "-o",
        template,
        "--print",
        "after_move:filepath",
        "--no-progress",
        url,
    ]

    # Retry logic for 403 errors
    max_retries = 3
    last_error_msg = ""
    stdout = ""
    stderr = ""

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
            break  # Success

        last_error_msg = stderr.strip() or stdout.strip() or f"yt-dlp exited with code {proc.returncode}"

        # Check for 403 error and retry
        if "403" in last_error_msg.lower() or "forbidden" in last_error_msg.lower():
            if attempt < max_retries - 1:
                logger.warning(f"YouTube download got 403 error, retrying ({attempt + 1}/{max_retries})...")
                await asyncio.sleep(1.0 + attempt * 0.5)  # Increasing delay: 1s, 1.5s, 2s
                continue

        # For other errors or final retry, raise immediately
        raise YouTubeDownloadError(last_error_msg)

    # Check final result after retries
    if proc.returncode != 0:
        raise YouTubeDownloadError(last_error_msg)

    lines = [ln.strip().strip('"') for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        raise YouTubeDownloadError("yt-dlp did not report an output file path.")

    path = Path(lines[-1])
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        raise YouTubeDownloadError(f"Downloaded file not found: {path}")
    return path
