"""Upload policy shared by every ingest path.

Filename sanitisation, the accepted media extensions, and limit formatting are
identical for Live Mic file uploads and Meeting recording imports. They lived in
``web_api`` as private helpers, so a route module could not reach them without
importing the module that registers it.

The rules are Windows-shaped on purpose: the app ships there, and a name that
NTFS rejects has to be repaired before it reaches the filesystem rather than
after.
"""

from __future__ import annotations

import re
from pathlib import Path

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_UPLOAD_FILENAME_CHARS = 180

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".flv", ".wmv", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"}
ALLOWED_UPLOAD_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


def safe_upload_filename(name: str) -> str:
    """Reduce a client-supplied name to one NTFS will accept."""
    raw = (name or "").strip()
    base = Path(raw).name
    base = INVALID_FILENAME_CHARS.sub("_", base).rstrip(" .")
    if not base or base in {".", ".."}:
        return "uploaded_file"
    if len(base) > MAX_UPLOAD_FILENAME_CHARS:
        path = Path(base)
        suffix = path.suffix
        stem_limit = max(1, MAX_UPLOAD_FILENAME_CHARS - len(suffix))
        base = f"{path.stem[:stem_limit]}{suffix}"
    stem = Path(base).stem
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        base = f"_{base}"
    return base


def format_upload_limit(limit_bytes: int) -> str:
    """Render a byte limit the way the user-facing rejection message needs it."""
    if limit_bytes >= 1024 * 1024 * 1024:
        whole_gb, remainder = divmod(limit_bytes, 1024 * 1024 * 1024)
        if remainder == 0:
            return f"{whole_gb}GB"
        return f"{limit_bytes / (1024 * 1024 * 1024):.1f}GB"
    return f"{limit_bytes / (1024 * 1024):.0f}MB"
