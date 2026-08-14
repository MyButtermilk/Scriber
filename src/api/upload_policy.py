"""Upload policy shared by every ingest path.

Filename sanitisation, the accepted media extensions, and limit formatting are
identical for File transcription uploads and Meeting recording imports. They
lived in ``web_api`` as private helpers, so a route module could not reach them
without importing the module that registers it.

The rules are Windows-shaped on purpose: the app ships there, and a name that
NTFS rejects has to be repaired before it reaches the filesystem rather than
after.
"""

from __future__ import annotations

import re
from pathlib import Path

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\ud800-\udfff]')
MAX_UPLOAD_FILENAME_UTF16_UNITS = 180

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
    }
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³")
    }
)


VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".avi", ".mkv", ".flv", ".wmv", ".m4v"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"})
ALLOWED_UPLOAD_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


def _utf16_units(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _truncate_utf16(value: str, max_units: int) -> str:
    used_units = 0
    for end, character in enumerate(value):
        character_units = 2 if ord(character) > 0xFFFF else 1
        if used_units + character_units > max_units:
            return value[:end]
        used_units += character_units
    return value


def _truncate_filename(value: str) -> str:
    if _utf16_units(value) <= MAX_UPLOAD_FILENAME_UTF16_UNITS:
        return value
    path = Path(value)
    suffix = path.suffix
    suffix_units = _utf16_units(suffix)
    if suffix and suffix_units < MAX_UPLOAD_FILENAME_UTF16_UNITS:
        stem_limit = MAX_UPLOAD_FILENAME_UTF16_UNITS - suffix_units
        return f"{_truncate_utf16(path.stem, stem_limit)}{suffix}"
    return _truncate_utf16(value, MAX_UPLOAD_FILENAME_UTF16_UNITS).rstrip(" .")


def _is_windows_reserved(value: str) -> bool:
    device_name = value.split(".", 1)[0].rstrip(" .").upper()
    return device_name in _WINDOWS_RESERVED_NAMES


def safe_upload_filename(name: str) -> str:
    """Reduce a client-supplied name to one NTFS will accept."""
    base = Path(name or "").name.strip()
    base = _INVALID_FILENAME_CHARS.sub("_", base).rstrip(" .")
    if not base or base in {".", ".."}:
        return "uploaded_file"
    base = _truncate_filename(base)
    if not base or base in {".", ".."}:
        return "uploaded_file"
    if _is_windows_reserved(base):
        base = _truncate_filename(f"_{base}")
    return base


def format_upload_limit(limit_bytes: int) -> str:
    """Render a byte limit the way the user-facing rejection message needs it."""
    if limit_bytes >= 1024 * 1024 * 1024:
        whole_gb, remainder = divmod(limit_bytes, 1024 * 1024 * 1024)
        if remainder == 0:
            return f"{whole_gb}GB"
        return f"{limit_bytes / (1024 * 1024 * 1024):.1f}GB"
    return f"{limit_bytes / (1024 * 1024):.0f}MB"
