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

import os
import re
from dataclasses import dataclass
from pathlib import Path

from src.core.provider_capabilities import supports_direct_file_upload

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

_UPLOAD_MAX_BYTES_ENV = "SCRIBER_UPLOAD_MAX_BYTES"
_UPLOAD_MAX_MB_ENV = "SCRIBER_UPLOAD_MAX_MB"
_DEFAULT_UPLOAD_MAX_MB = 200
_DEFAULT_AUDIO_INGEST_MAX_BYTES = 2048 * 1024 * 1024
_DEFAULT_VIDEO_MAX_BYTES = 2048 * 1024 * 1024

_PROVIDER_AUDIO_UPLOAD_LIMITS: dict[str, tuple[int, str]] = {
    "soniox": (524_288_000, "500MB"),
    "soniox_async": (524_288_000, "500MB"),
    "gemini_stt": (100 * 1024 * 1024, "100MB"),
    "mistral": (512 * 1024 * 1024, "512MB"),
    "mistral_async": (512 * 1024 * 1024, "512MB"),
    "smallest": (25 * 1024 * 1024, "25MB"),
    "smallest_async": (25 * 1024 * 1024, "25MB"),
    "azure_mai": (300 * 1024 * 1024, "300MB"),
    "assemblyai": (2_200_000_000, "2.2GB"),
    "deepgram_async": (2_000_000_000, "2GB"),
    "openai_async": (25 * 1024 * 1024, "25MB"),
    "openrouter_stt": (300 * 1024 * 1024, "300MB"),
    "modulate": (100 * 1024 * 1024, "100MB"),
    "modulate_async": (100 * 1024 * 1024, "100MB"),
    "meta_stt": (32_000_000 - 65_536, "32MB including multipart overhead"),
    "meta_stt_async": (32_000_000 - 65_536, "32MB including multipart overhead"),
}


@dataclass(frozen=True, slots=True)
class UploadLimit:
    """One byte boundary and its reviewed public rendering."""

    max_bytes: int
    label: str

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("Upload limits must be positive")
        if not self.label:
            raise ValueError("Upload limits require a public label")


@dataclass(frozen=True, slots=True)
class FileUploadLimits:
    """The complete immutable size policy for one admitted File upload."""

    source_is_video: bool
    ingest: UploadLimit
    final_audio: UploadLimit


def _upload_limit_override_bytes() -> int | None:
    raw_bytes = os.getenv(_UPLOAD_MAX_BYTES_ENV, "").strip()
    if raw_bytes:
        try:
            value = int(raw_bytes)
            if value > 0:
                return value
        except TypeError, ValueError:
            pass
    raw_mb = os.getenv(_UPLOAD_MAX_MB_ENV, "").strip()
    if raw_mb:
        try:
            value_mb = float(raw_mb)
            if value_mb > 0:
                return int(value_mb * 1024 * 1024)
        except TypeError, ValueError:
            pass
    return None


def _provider_audio_limit(provider: str | None) -> UploadLimit:
    override = _upload_limit_override_bytes()
    if override is not None:
        return UploadLimit(override, format_upload_limit(override))
    normalized = (provider or "").strip().lower()
    configured = _PROVIDER_AUDIO_UPLOAD_LIMITS.get(normalized)
    if configured is not None:
        return UploadLimit(*configured)
    if not supports_direct_file_upload(normalized):
        return UploadLimit(
            _DEFAULT_AUDIO_INGEST_MAX_BYTES,
            format_upload_limit(_DEFAULT_AUDIO_INGEST_MAX_BYTES),
        )
    fallback = _DEFAULT_UPLOAD_MAX_MB * 1024 * 1024
    return UploadLimit(fallback, format_upload_limit(fallback))


def file_upload_limits(provider: str | None, *, source_is_video: bool) -> FileUploadLimits:
    """Return every byte boundary for one provider-bound admission decision."""

    final_audio = _provider_audio_limit(provider)
    if source_is_video:
        ingest = UploadLimit(_DEFAULT_VIDEO_MAX_BYTES, format_upload_limit(_DEFAULT_VIDEO_MAX_BYTES))
    else:
        ingest_bytes = max(_DEFAULT_AUDIO_INGEST_MAX_BYTES, final_audio.max_bytes)
        ingest_label = (
            final_audio.label
            if ingest_bytes == final_audio.max_bytes and ingest_bytes > _DEFAULT_AUDIO_INGEST_MAX_BYTES
            else format_upload_limit(ingest_bytes)
        )
        ingest = UploadLimit(ingest_bytes, ingest_label)
    return FileUploadLimits(
        source_is_video=source_is_video,
        ingest=ingest,
        final_audio=final_audio,
    )


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
