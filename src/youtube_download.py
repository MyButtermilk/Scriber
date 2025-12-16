from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path


class YouTubeDownloadError(RuntimeError):
    pass


def _find_yt_dlp_command() -> list[str]:
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if exe:
        return [exe]

    try:
        import yt_dlp  # type: ignore  # noqa: F401
    except Exception:
        return []

    return [sys.executable, "-m", "yt_dlp"]


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") or shutil.which("ffmpeg.exe"):
        return
    raise YouTubeDownloadError("ffmpeg not found on PATH (required for audio extraction).")


async def download_youtube_audio(
    url: str,
    *,
    output_dir: str | Path,
    audio_format: str = "mp3",
) -> Path:
    url = (url or "").strip()
    if not url:
        raise ValueError("Missing URL")

    cmd = _find_yt_dlp_command()
    if not cmd:
        raise YouTubeDownloadError("yt-dlp not installed (install with `pip install yt-dlp`).")

    _require_ffmpeg()

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    template = str(out_dir / "%(id)s.%(ext)s")

    args = [
        *cmd,
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

