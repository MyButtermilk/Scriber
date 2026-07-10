from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from src.runtime.paths import app_root, repo_root

_TOOL_ENV = {
    "ffmpeg": "SCRIBER_FFMPEG_PATH",
    "ffprobe": "SCRIBER_FFPROBE_PATH",
    "yt-dlp": "SCRIBER_YT_DLP_PATH",
    "deno": "SCRIBER_DENO_PATH",
}
_TOOLS_DIR_ENV = "SCRIBER_MEDIA_TOOLS_DIR"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _tool_names(tool: str) -> list[str]:
    names = [tool]
    if _is_windows() and not tool.lower().endswith(".exe"):
        names.append(f"{tool}.exe")
    return names


def _resolve_existing_file(path: str | Path) -> str | None:
    try:
        candidate = Path(path).expanduser().resolve()
    except OSError:
        return None
    if candidate.is_file():
        return str(candidate)
    return None


def _candidate_tool_dirs() -> list[Path]:
    dirs: list[Path] = []

    raw_tools_dir = os.getenv(_TOOLS_DIR_ENV, "").strip()
    if raw_tools_dir:
        dirs.append(Path(raw_tools_dir).expanduser())

    for root in (app_root(), repo_root()):
        dirs.extend(
            [
                root,
                root / "bin",
                root / "ffmpeg",
                root / "tools",
                root / "tools" / "ffmpeg",
                root / "runtime" / "bin",
            ]
        )

    unique: list[Path] = []
    for path in dirs:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved not in unique:
            unique.append(resolved)
    return unique


def find_media_tool(tool: str) -> str | None:
    """Find a bundled or system media executable."""
    tool = tool.strip()
    if not tool:
        return None

    env_name = _TOOL_ENV.get(tool)
    if env_name:
        raw_tool_path = os.getenv(env_name, "").strip()
        if raw_tool_path:
            found = _resolve_existing_file(raw_tool_path)
            if found:
                return found

    for directory in _candidate_tool_dirs():
        for name in _tool_names(tool):
            found = _resolve_existing_file(directory / name)
            if found:
                return found

    for name in _tool_names(tool):
        found = shutil.which(name)
        if found:
            return found
    return None


def require_media_tool(tool: str) -> str:
    found = find_media_tool(tool)
    if found:
        return found

    env_name = _TOOL_ENV.get(tool)
    env_hint = f", set {env_name}" if env_name else ""
    raise RuntimeError(
        f"{tool} not found. Install {tool}, add it to PATH{env_hint}, "
        f"or place it under a bundled tools directory."
    )
