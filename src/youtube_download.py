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


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") or shutil.which("ffmpeg.exe"):
        return
    raise YouTubeDownloadError("ffmpeg not found on PATH (required for audio extraction).")


def _find_yt_dlp_command() -> list[str]:
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if not exe:
        return [sys.executable, "-m", "yt_dlp"]
    return [exe]


async def download_youtube_audio(
    url: str,
    *,
    output_dir: str | Path,
    audio_format: str = "mp3",
    on_progress: Optional[Callable[[DownloadProgress], None]] = None,
) -> Path:
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
            "format": "bestaudio/best",
            "outtmpl": template,
            "noplaylist": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "0",
            }],
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
        }
        
        def _run_download():
            nonlocal final_path
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    # Get the final filename after post-processing
                    video_id = info.get("id", "video")
                    final_path = out_dir / f"{video_id}.{audio_format}"
        
        # Run in thread to not block event loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _run_download)
        
        if final_path and final_path.exists():
            return final_path
        
        # Try to find the file
        for f in out_dir.glob(f"*.{audio_format}"):
            return f
        
        raise YouTubeDownloadError("Downloaded file not found")
        
    except ImportError:
        logger.warning("yt-dlp library not available, falling back to subprocess")
    
    # Fallback to subprocess if library import fails
    exe_cmd = _find_yt_dlp_command()
    
    args = [
        *exe_cmd,
        "--no-playlist",
        "-f",
        "bestaudio/best",
        "-x",
        "--audio-format",
        str(audio_format),
        "--audio-quality",
        "0",
        "-o",
        template,
        "--print",
        "after_move:filepath",
        "--no-progress",
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")

    if proc.returncode != 0:
        msg = stderr.strip() or stdout.strip() or f"yt-dlp exited with code {proc.returncode}"
        raise YouTubeDownloadError(msg)

    lines = [ln.strip().strip('"') for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        raise YouTubeDownloadError("yt-dlp did not report an output file path.")

    path = Path(lines[-1])
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        raise YouTubeDownloadError(f"Downloaded file not found: {path}")
    return path
