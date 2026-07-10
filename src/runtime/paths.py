from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

_APP_NAME = "Scriber"
_LEGACY_DATA_FILES = (
    ".env",
    "settings.json",
    "transcripts.db",
    "transcripts.db-wal",
    "transcripts.db-shm",
)
_LEGACY_DATA_DIRS = ("downloads", "models")


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


def env_path() -> Path:
    return data_dir() / ".env"


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


def logs_dir() -> Path:
    raw = os.getenv("SCRIBER_LOG_DIR", "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path.resolve() if path.is_absolute() else (data_dir() / path).resolve()
    if uses_user_data_dir():
        return (data_dir() / "logs").resolve()
    return repo_root()


def support_bundles_dir() -> Path:
    return (data_dir() / "support-bundles").resolve()


def legacy_data_candidates() -> list[Path]:
    """Return possible legacy source-checkout data locations."""
    candidates: list[Path] = []

    raw = os.getenv("SCRIBER_LEGACY_DATA_DIR", "").strip()
    if raw:
        for entry in raw.replace(",", os.pathsep).split(os.pathsep):
            item = entry.strip()
            if item:
                _push_unique_path(candidates, Path(item).expanduser().resolve())

    if _should_auto_migrate_legacy_data():
        for candidate in _default_legacy_data_candidates():
            _push_unique_path(candidates, candidate)

    return candidates


def migrate_legacy_runtime_data() -> dict[str, object]:
    """Copy first-run legacy runtime data into the user data directory.

    The migration is intentionally non-destructive: existing target files are
    never overwritten, and directory migration only copies missing files.
    """
    if os.getenv("SCRIBER_SKIP_LEGACY_DATA_MIGRATION", "").strip().lower() in {"1", "true", "yes"}:
        return {"attempted": False, "reason": "disabled", "copied": []}
    if not uses_user_data_dir():
        return {"attempted": False, "reason": "repo_data_dir", "copied": []}

    target = data_dir()
    copied: list[str] = []
    sources: list[str] = []

    for source in legacy_data_candidates():
        if not source.is_dir() or _same_path(source, target):
            continue

        source_copied: list[str] = []
        target_db_preexisting = (target / "transcripts.db").exists()
        for name in _LEGACY_DATA_FILES:
            if name in {"transcripts.db-wal", "transcripts.db-shm"} and target_db_preexisting:
                continue
            src = source / name
            dst = target / name
            if _copy_file_if_missing(src, dst):
                source_copied.append(name)

        for name in _LEGACY_DATA_DIRS:
            src_dir = source / name
            dst_dir = target / name
            source_copied.extend(f"{name}/{item}" for item in _copy_tree_missing(src_dir, dst_dir))

        if source_copied:
            sources.append(str(source))
            copied.extend(source_copied)

    result: dict[str, object] = {"attempted": True, "target": str(target), "copied": copied}
    if sources:
        result["source"] = sources[0]
        result["sources"] = sources
    return result


def _should_auto_migrate_legacy_data() -> bool:
    raw = os.getenv("SCRIBER_AUTO_MIGRATE_LEGACY_DATA", "").strip().lower()
    if raw in {"0", "false", "no"}:
        return False
    if raw in {"1", "true", "yes"}:
        return True
    return _same_path(data_dir(), _default_user_data_dir())


def _default_legacy_data_candidates() -> list[Path]:
    candidates: list[Path] = []
    try:
        _push_unique_path(candidates, repo_root())
    except Exception:
        pass
    try:
        _push_unique_path(candidates, Path.cwd().resolve())
    except Exception:
        pass

    user_profile = os.getenv("USERPROFILE", "").strip()
    if user_profile:
        profile = Path(user_profile).expanduser()
        for github_dir in ("Github", "GitHub"):
            _push_unique_path(candidates, (profile / "Documents" / github_dir / "Scriber").resolve())
            _push_unique_path(candidates, (profile / "OneDrive" / "Documents" / github_dir / "Scriber").resolve())

    return candidates


def _push_unique_path(paths: list[Path], path: Path) -> None:
    if not any(_same_path(path, existing) for existing in paths):
        paths.append(path)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except Exception:
        return str(left) == str(right)


def _copy_file_if_missing(src: Path, dst: Path) -> bool:
    if not src.is_file() or dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    temporary = dst.with_name(f".{dst.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        shutil.copy2(src, temporary)
        if dst.exists():
            return False
        os.replace(temporary, dst)
        return True
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _copy_tree_missing(src_dir: Path, dst_dir: Path) -> list[str]:
    copied: list[str] = []
    if not src_dir.is_dir():
        return copied
    for src in src_dir.rglob("*"):
        if not src.is_file():
            continue
        relative = src.relative_to(src_dir)
        dst = dst_dir / relative
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(str(relative).replace("\\", "/"))
    return copied
