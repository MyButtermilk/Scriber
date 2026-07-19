from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from src.runtime import media_tools
from src.runtime import quickjs_runtime_lock


def _tool_file(directory: Path, name: str) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    path = directory / f"{name}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"tool")
    return path


def _quickjs_bundle(directory: Path) -> Path:
    wrapper = directory / "qjs.exe"
    engine = directory / "qjs-engine.exe"
    license_path = directory / "LICENSE.quickjs-ng.txt"
    directory.mkdir(parents=True, exist_ok=True)
    wrapper.write_bytes(b"bounded-wrapper")
    engine.write_bytes(b"locked-engine")
    license_path.write_bytes(b"MIT license")
    manifest = {
        "contract": "ScriberYoutubeJsRuntimeManifestV3",
        "schemaVersion": 3,
        "runtime": {
            "kind": "quickjs",
            "implementation": "bounded-quickjs-wrapper",
            "protocol": "ScriberYtDlpQuickJsFileV1",
            "executable": wrapper.name,
            "length": wrapper.stat().st_size,
            "sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
            "engine": engine.name,
            "engineLength": engine.stat().st_size,
            "engineSha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
            "licenseFile": license_path.name,
        },
        "policy": {
            "remoteComponents": False,
            "firstRunDownloads": False,
            "exactArgumentProtocol": True,
            "engineHashVerified": True,
            "killOnJobClose": True,
        },
    }
    (directory / "js-runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return wrapper


def test_frozen_quickjs_root_is_exactly_bound_to_packaging_lock() -> None:
    repo = Path(__file__).resolve().parents[2]
    lock_path = repo / quickjs_runtime_lock.SOURCE_LOCK_FILE
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    manifest_bytes = (
        json.dumps(
            lock["manifest"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )

    assert quickjs_runtime_lock.ROOT_CONTRACT == "ScriberFrozenQuickJsRuntimeRootV1"
    assert len(lock_bytes) == quickjs_runtime_lock.SOURCE_LOCK_LENGTH
    assert (
        hashlib.sha256(lock_bytes).hexdigest()
        == quickjs_runtime_lock.SOURCE_LOCK_SHA256
    )
    assert quickjs_runtime_lock.WRAPPER == (
        lock["wrapper"]["output"]["installedFileName"],
        lock["wrapper"]["output"]["length"],
        lock["wrapper"]["output"]["sha256"],
    )
    assert quickjs_runtime_lock.HARDENED_ENGINE == (
        lock["engine"]["installedFileName"],
        lock["engine"]["length"],
        lock["engine"]["sha256"],
    )
    assert quickjs_runtime_lock.MANIFEST == (
        "js-runtime-manifest.json",
        len(manifest_bytes),
        lock["manifestCanonicalSha256"],
    )
    assert (
        hashlib.sha256(manifest_bytes).hexdigest()
        == quickjs_runtime_lock.MANIFEST.sha256
    )
    assert quickjs_runtime_lock.LICENSE == (
        lock["license"]["installedFileName"],
        lock["license"]["length"],
        lock["license"]["sha256"],
    )
    expected_self_test = {
        "contract": lock["manifest"]["runtime"]["protocol"],
        "ok": True,
        "quickjsVersion": lock["manifest"]["runtime"]["version"],
    }
    assert json.loads(quickjs_runtime_lock.SELF_TEST_STDOUT) == expected_self_test


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


def test_find_media_tool_supports_explicit_quickjs_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    quickjs = _quickjs_bundle(tmp_path / "runtime")

    monkeypatch.setenv("SCRIBER_QUICKJS_DEV_WRAPPER_PATH", str(quickjs))
    monkeypatch.setattr(media_tools, "is_frozen", lambda: False)
    monkeypatch.setattr(media_tools, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(media_tools, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: None)

    assert media_tools.find_media_tool("qjs") == str(quickjs.resolve())


def test_frozen_quickjs_resolution_never_uses_path_or_dev_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    raw_path_qjs = _tool_file(tmp_path / "path", "qjs")
    dev_wrapper = _quickjs_bundle(tmp_path / "dev")
    app = tmp_path / "app"

    monkeypatch.setenv("SCRIBER_QUICKJS_DEV_WRAPPER_PATH", str(dev_wrapper))
    monkeypatch.setattr(media_tools, "is_frozen", lambda: True)
    monkeypatch.setattr(media_tools, "app_root", lambda: app)
    monkeypatch.setattr(media_tools, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: str(raw_path_qjs))

    assert media_tools.find_media_tool("qjs") is None

    # A complete but self-described fake quartet must not become authoritative
    # merely because its forged manifest matches its fake executable bytes.
    _quickjs_bundle(app / "tools" / "ffmpeg")
    assert media_tools.find_media_tool("qjs") is None


def test_frozen_quickjs_resolution_requires_bound_self_test(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    wrapper = _quickjs_bundle(tmp_path / "runtime")
    self_test_candidates: list[Path] = []

    monkeypatch.setattr(
        media_tools,
        "_locked_runtime_file_matches",
        lambda _parent, _identity: True,
    )
    monkeypatch.setattr(
        media_tools,
        "_quickjs_self_test_matches",
        lambda candidate: self_test_candidates.append(candidate) or False,
    )

    assert media_tools._resolve_quickjs_wrapper(wrapper, frozen=True) is None
    assert self_test_candidates == [wrapper.resolve()]


def test_quickjs_self_test_is_exactly_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    candidate = tmp_path / quickjs_runtime_lock.WRAPPER.name
    candidate.write_bytes(b"not executed by this unit test")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run_exact(command: list[str], **kwargs: object):
        calls.append((command, kwargs))
        return media_tools.subprocess.CompletedProcess(
            command,
            0,
            stdout=quickjs_runtime_lock.SELF_TEST_STDOUT,
            stderr=b"",
        )

    monkeypatch.setattr(media_tools.subprocess, "run", run_exact)

    assert media_tools._quickjs_self_test_matches(candidate) is True
    assert calls[0][0] == [
        str(candidate),
        *quickjs_runtime_lock.SELF_TEST_ARGUMENTS,
    ]
    assert calls[0][1]["timeout"] == quickjs_runtime_lock.SELF_TEST_TIMEOUT_SECONDS

    def run_self_described(command: list[str], **_kwargs: object):
        return media_tools.subprocess.CompletedProcess(
            command,
            0,
            stdout=b'{"ok":true}\n',
            stderr=b"",
        )

    monkeypatch.setattr(media_tools.subprocess, "run", run_self_described)
    assert media_tools._quickjs_self_test_matches(candidate) is False


@pytest.mark.skipif(sys.platform != "win32", reason="locked quartet is Windows-only")
def test_frozen_quickjs_resolution_accepts_real_built_locked_quartet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    repo = Path(__file__).resolve().parents[2]
    configured_runtime = os.environ.get("SCRIBER_TEST_QUICKJS_RUNTIME_DIR", "").strip()
    identities = (
        quickjs_runtime_lock.WRAPPER,
        quickjs_runtime_lock.HARDENED_ENGINE,
        quickjs_runtime_lock.MANIFEST,
        quickjs_runtime_lock.LICENSE,
    )
    candidates = (
        [Path(configured_runtime)]
        if configured_runtime
        else [
            repo / "build" / "quickjs-final-offline-runtime",
            repo / "build" / "quickjs-builder-runtime",
        ]
    )

    def is_locked_runtime(directory: Path) -> bool:
        return all(
            (directory / identity.name).is_file()
            and (directory / identity.name).stat().st_size == identity.length
            and hashlib.sha256((directory / identity.name).read_bytes()).hexdigest()
            == identity.sha256
            for identity in identities
        )

    source_runtime = next(
        (candidate for candidate in candidates if is_locked_runtime(candidate)),
        None,
    )
    if source_runtime is None:
        pytest.skip("locked QuickJS builder output is not available")

    app = tmp_path / "app"
    destination = app / "tools" / "ffmpeg"
    destination.mkdir(parents=True)
    for identity in identities:
        shutil.copy2(
            source_runtime / identity.name,
            destination / identity.name,
        )

    monkeypatch.setattr(media_tools, "is_frozen", lambda: True)
    monkeypatch.setattr(media_tools, "app_root", lambda: app)

    expected = (destination / quickjs_runtime_lock.WRAPPER.name).resolve()
    assert media_tools.find_media_tool("qjs") == str(expected)


def test_quickjs_resolution_rejects_tampered_wrapper_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    wrapper = _quickjs_bundle(tmp_path / "runtime")
    wrapper.write_bytes(b"raw replacement qjs")

    monkeypatch.setenv("SCRIBER_QUICKJS_DEV_WRAPPER_PATH", str(wrapper))
    monkeypatch.setattr(media_tools, "is_frozen", lambda: False)
    monkeypatch.setattr(media_tools, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(media_tools, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: str(wrapper))

    assert media_tools.find_media_tool("qjs") is None


def test_require_quickjs_reports_the_bundle_override_not_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.delenv("SCRIBER_QUICKJS_DEV_WRAPPER_PATH", raising=False)
    monkeypatch.setattr(media_tools, "is_frozen", lambda: False)
    monkeypatch.setattr(media_tools, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(media_tools, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="SCRIBER_QUICKJS_DEV_WRAPPER_PATH") as error:
        media_tools.require_media_tool("qjs")

    assert "add it to PATH" not in str(error.value)


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
