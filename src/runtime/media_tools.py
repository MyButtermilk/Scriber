from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.runtime.paths import app_root, is_frozen, repo_root
from src.runtime.quickjs_runtime_lock import (
    HARDENED_ENGINE as _LOCKED_QUICKJS_ENGINE,
    LICENSE as _LOCKED_QUICKJS_LICENSE,
    MANIFEST as _LOCKED_QUICKJS_MANIFEST,
    SELF_TEST_ARGUMENTS as _QUICKJS_SELF_TEST_ARGUMENTS,
    SELF_TEST_STDOUT as _QUICKJS_SELF_TEST_STDOUT,
    SELF_TEST_TIMEOUT_SECONDS as _QUICKJS_SELF_TEST_TIMEOUT_SECONDS,
    WRAPPER as _LOCKED_QUICKJS_WRAPPER,
    LockedRuntimeFile,
)
from src.runtime.subprocess_utils import hidden_subprocess_kwargs

_TOOL_ENV = {
    "ffmpeg": "SCRIBER_FFMPEG_PATH",
    "ffprobe": "SCRIBER_FFPROBE_PATH",
    "yt-dlp": "SCRIBER_YT_DLP_PATH",
}
_TOOLS_DIR_ENV = "SCRIBER_MEDIA_TOOLS_DIR"
_QUICKJS_DEV_PATH_ENV = "SCRIBER_QUICKJS_DEV_WRAPPER_PATH"
_QUICKJS_MANIFEST_NAME = "js-runtime-manifest.json"
_QUICKJS_MANIFEST_CONTRACT = "ScriberYoutubeJsRuntimeManifestV3"
_QUICKJS_IMPLEMENTATION = "bounded-quickjs-wrapper"
_QUICKJS_PROTOCOL = "ScriberYtDlpQuickJsFileV1"


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _locked_file_matches(
    parent: Path,
    *,
    name: Any,
    length: Any,
    sha256: Any,
) -> bool:
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or isinstance(length, bool)
        or not isinstance(length, int)
        or length <= 0
        or not isinstance(sha256, str)
        or len(sha256) != 64
    ):
        return False
    try:
        candidate = parent / name
        if candidate.is_symlink() or not candidate.is_file():
            return False
        return candidate.stat().st_size == length and _sha256_file(candidate) == sha256
    except OSError:
        return False


def _locked_runtime_file_matches(parent: Path, identity: LockedRuntimeFile) -> bool:
    return _locked_file_matches(
        parent,
        name=identity.name,
        length=identity.length,
        sha256=identity.sha256,
    )


def _quickjs_self_test_matches(candidate: Path) -> bool:
    try:
        result = subprocess.run(
            [str(candidate), *_QUICKJS_SELF_TEST_ARGUMENTS],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_QUICKJS_SELF_TEST_TIMEOUT_SECONDS,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        result.returncode == 0
        and result.stdout == _QUICKJS_SELF_TEST_STDOUT
        and result.stderr == b""
    )


def _resolve_frozen_quickjs_wrapper(candidate: Path) -> str | None:
    """Validate the installed quartet against the embedded first-party root."""

    if candidate.name != _LOCKED_QUICKJS_WRAPPER.name:
        return None
    for identity in (
        _LOCKED_QUICKJS_WRAPPER,
        _LOCKED_QUICKJS_ENGINE,
        _LOCKED_QUICKJS_MANIFEST,
        _LOCKED_QUICKJS_LICENSE,
    ):
        if not _locked_runtime_file_matches(candidate.parent, identity):
            return None
    if not _quickjs_self_test_matches(candidate):
        return None
    return str(candidate)


def _resolve_quickjs_wrapper(
    path: str | Path,
    *,
    frozen: bool,
) -> str | None:
    """Accept a complete Scriber QuickJS wrapper bundle.

    Frozen resolution is rooted exclusively in committed first-party
    identities.  Source runs retain the explicit developer-bundle contract.
    """

    try:
        candidate = Path(path).expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            return None
        candidate = candidate.resolve(strict=True)
        if frozen:
            return _resolve_frozen_quickjs_wrapper(candidate)
        manifest_path = candidate.parent / _QUICKJS_MANIFEST_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return None
        if manifest_path.stat().st_size > 64 * 1024:
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    runtime = manifest.get("runtime")
    policy = manifest.get("policy")
    if (
        manifest.get("contract") != _QUICKJS_MANIFEST_CONTRACT
        or manifest.get("schemaVersion") != 3
        or not isinstance(runtime, dict)
        or not isinstance(policy, dict)
        or runtime.get("kind") != "quickjs"
        or runtime.get("implementation") != _QUICKJS_IMPLEMENTATION
        or runtime.get("protocol") != _QUICKJS_PROTOCOL
        or runtime.get("executable") != candidate.name
        or policy.get("remoteComponents") is not False
        or policy.get("firstRunDownloads") is not False
        or policy.get("exactArgumentProtocol") is not True
        or policy.get("engineHashVerified") is not True
        or policy.get("killOnJobClose") is not True
    ):
        return None
    if not _locked_file_matches(
        candidate.parent,
        name=candidate.name,
        length=runtime.get("length"),
        sha256=runtime.get("sha256"),
    ):
        return None
    if not _locked_file_matches(
        candidate.parent,
        name=runtime.get("engine"),
        length=runtime.get("engineLength"),
        sha256=runtime.get("engineSha256"),
    ):
        return None
    license_name = runtime.get("licenseFile")
    if not isinstance(license_name, str) or Path(license_name).name != license_name:
        return None
    license_path = candidate.parent / license_name
    if license_path.is_symlink() or not license_path.is_file():
        return None
    return str(candidate)


def _find_quickjs_wrapper() -> str | None:
    # Frozen production may execute only the bundle staged next to the backend.
    # It never consults PATH, generic media-tool overrides, or a developer path.
    frozen = is_frozen()
    bundled = app_root() / "tools" / "ffmpeg" / "qjs.exe"
    found = _resolve_quickjs_wrapper(bundled, frozen=frozen)
    if found or frozen:
        return found

    # Source/dev runs require an explicit wrapper override or a complete bundle
    # in a repository-owned tools directory. Raw qjs from PATH is never valid.
    raw_override = os.getenv(_QUICKJS_DEV_PATH_ENV, "").strip()
    if raw_override:
        found = _resolve_quickjs_wrapper(raw_override, frozen=False)
        if found:
            return found
    for root in (app_root(), repo_root()):
        for relative in (
            Path("tools") / "ffmpeg" / "qjs.exe",
            Path("backend") / "tools" / "ffmpeg" / "qjs.exe",
        ):
            found = _resolve_quickjs_wrapper(root / relative, frozen=False)
            if found:
                return found
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
    if tool == "qjs":
        return _find_quickjs_wrapper()

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

    if tool.strip() == "qjs":
        raise RuntimeError(
            "Scriber QuickJS wrapper bundle not found. Source runs may set "
            f"{_QUICKJS_DEV_PATH_ENV}; frozen builds require the complete bundle "
            "under tools/ffmpeg."
        )

    env_name = _TOOL_ENV.get(tool)
    env_hint = f", set {env_name}" if env_name else ""
    raise RuntimeError(
        f"{tool} not found. Install {tool}, add it to PATH{env_hint}, "
        f"or place it under a bundled tools directory."
    )
