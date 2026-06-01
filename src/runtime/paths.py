from __future__ import annotations

import os
import sys
from pathlib import Path

_APP_NAME = "Scriber"


def repo_root() -> Path:
    """Return the repository root in source checkouts."""
    return Path(__file__).resolve().parents[2]


def is_frozen() -> bool:
    """Return True when Python is running from a frozen executable."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Return the executable/app root for frozen builds, otherwise the repo root."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return repo_root()


def _explicit_data_dir() -> Path | None:
    raw = os.getenv("SCRIBER_DATA_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _default_user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
        if base:
            return Path(base).expanduser().resolve() / _APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_NAME
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")).expanduser().resolve() / "scriber"


def uses_user_data_dir() -> bool:
    """Return True when runtime state should live outside the repo/app root."""
    return _explicit_data_dir() is not None or is_frozen()


def data_dir() -> Path:
    """Return the writable data directory for settings, database, and downloads."""
    path = _explicit_data_dir()
    if path is None:
        path = _default_user_data_dir() if is_frozen() else repo_root()
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return data_dir() / "settings.json"


def database_path() -> Path:
    raw = os.getenv("SCRIBER_DATABASE_PATH", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return data_dir() / "transcripts.db"


def downloads_dir(default_name: str = "downloads") -> Path:
    raw = os.getenv("SCRIBER_DOWNLOADS_DIR", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path.resolve()
        if uses_user_data_dir():
            return (data_dir() / path).resolve()
        return path.resolve()
    if uses_user_data_dir():
        return (data_dir() / default_name).resolve()
    return Path(default_name).resolve()
