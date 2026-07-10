from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.runtime import media_tools


def _tool_file(directory: Path, name: str) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    path = directory / f"{name}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"tool")
    return path


def test_find_media_tool_prefers_explicit_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    explicit = _tool_file(tmp_path, "custom-ffmpeg")
    bundled = _tool_file(tmp_path / "app" / "tools" / "ffmpeg", "ffmpeg")

    monkeypatch.setenv("SCRIBER_FFMPEG_PATH", str(explicit))
    monkeypatch.setattr(media_tools, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(media_tools, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: str(bundled))

    assert media_tools.find_media_tool("ffmpeg") == str(explicit.resolve())


def test_find_media_tool_uses_bundled_dir_before_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    bundled = _tool_file(tmp_path / "app" / "tools" / "ffmpeg", "ffmpeg")
    path_tool = _tool_file(tmp_path / "path", "ffmpeg")

    monkeypatch.delenv("SCRIBER_FFMPEG_PATH", raising=False)
    monkeypatch.delenv("SCRIBER_MEDIA_TOOLS_DIR", raising=False)
    monkeypatch.setattr(media_tools, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(media_tools, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: str(path_tool))

    assert media_tools.find_media_tool("ffmpeg") == str(bundled.resolve())


def test_find_media_tool_uses_media_tools_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    configured = _tool_file(tmp_path / "configured", "ffprobe")

    monkeypatch.setenv("SCRIBER_MEDIA_TOOLS_DIR", str(configured.parent))
    monkeypatch.setattr(media_tools, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(media_tools, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: None)

    assert media_tools.find_media_tool("ffprobe") == str(configured.resolve())


def test_find_media_tool_supports_explicit_deno_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    deno = _tool_file(tmp_path / "runtime", "deno")

    monkeypatch.setenv("SCRIBER_DENO_PATH", str(deno))
    monkeypatch.setattr(media_tools, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(media_tools, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: None)

    assert media_tools.find_media_tool("deno") == str(deno.resolve())


def test_require_media_tool_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.delenv("SCRIBER_FFMPEG_PATH", raising=False)
    monkeypatch.delenv("SCRIBER_MEDIA_TOOLS_DIR", raising=False)
    monkeypatch.setattr(media_tools, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(media_tools, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="SCRIBER_FFMPEG_PATH"):
        media_tools.require_media_tool("ffmpeg")
