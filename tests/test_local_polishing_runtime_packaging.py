from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import zipfile
from pathlib import Path

import pytest

import scripts.prepare_local_polishing_runtime as local_polishing_runtime
from scripts.prepare_local_polishing_runtime import (
    LOCK_CONTRACT,
    MANIFEST_CONTRACT,
    RuntimePreparationError,
    load_lock,
    prepare,
    verify_runtime,
    write_manifest,
)
from scripts.smoke_local_polishing_runtime import (
    LocalPolishingRuntimeSmokeError,
    validate_resource,
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, *, unsafe_name: str | None = None) -> tuple[Path, Path]:
    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name in (
            "llama-server.exe",
            "llama-server-impl.dll",
            "llama-common.dll",
            "llama.dll",
            "mtmd.dll",
            "ggml.dll",
            "ggml-base.dll",
            "ggml-rpc.dll",
            "ggml-vulkan.dll",
            "ggml-cpu-x64.dll",
            "libomp140.x86_64.dll",
        ):
            bundle.writestr(name, f"bytes:{name}")
        if unsafe_name:
            bundle.writestr(unsafe_name, "unsafe")
    license_path = cache / "LICENSE.llama.cpp.txt"
    license_path.write_text("MIT fixture\n", encoding="utf-8")
    lock = {
        "contract": LOCK_CONTRACT,
        "schemaVersion": 1,
        "status": "locked",
        "platform": {"os": "windows", "architecture": "amd64", "primaryBackend": "vulkan", "cpuFallback": True},
        "release": {
            "tag": "b10158",
            "commit": "f87067841bac583bc089a225382248d857791ca8",
            "versionOutputContains": ["version: 10158", "f87067841"],
        },
        "archive": {
            "filename": archive.name,
            "url": "https://example.invalid/runtime.zip",
            "bytes": archive.stat().st_size,
            "sha256": _sha(archive),
        },
        "license": {
            "filename": "LICENSE.llama.cpp.txt",
            "url": "https://example.invalid/LICENSE",
            "bytes": license_path.stat().st_size,
            "sha256": _sha(license_path),
        },
        "maximumArchiveEntries": 32,
        "maximumExtractedBytes": 100_000,
        "requiredExactFiles": [
            "llama-server.exe",
            "llama-server-impl.dll",
            "llama-common.dll",
            "llama.dll",
            "mtmd.dll",
            "ggml.dll",
            "ggml-base.dll",
            "ggml-rpc.dll",
            "ggml-vulkan.dll",
            "libomp140.x86_64.dll",
        ],
        "requiredFileGlobs": ["ggml-cpu-*.dll"],
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    return lock_path, cache


def test_locked_download_retries_transient_http_failures(tmp_path, monkeypatch) -> None:
    payload = b"locked runtime payload"
    entry = {
        "filename": "runtime.bin",
        "url": "https://example.invalid/runtime.bin",
        "bytes": len(payload),
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }
    attempts = 0
    delays: list[float] = []

    def fake_urlopen(_request, *, timeout):
        nonlocal attempts
        assert timeout == 120
        attempts += 1
        if attempts < 3:
            raise urllib.error.HTTPError(entry["url"], 429, "rate limited", {"Retry-After": "1"}, None)
        return io.BytesIO(payload)

    monkeypatch.setattr(local_polishing_runtime.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(local_polishing_runtime.time, "sleep", delays.append)

    result = local_polishing_runtime._download(entry, tmp_path / "cache", "runtime archive")

    assert result.read_bytes() == payload
    assert attempts == 3
    assert delays == [5.0, 10.0]


def test_prepare_materializes_only_locked_runtime_files(tmp_path):
    lock_path, cache = _fixture(tmp_path)
    lock = load_lock(lock_path)
    output = tmp_path / "backend" / "tools" / "local-polishing"

    manifest = prepare(lock, cache, output)

    assert manifest["contract"] == MANIFEST_CONTRACT
    assert (output / "llama-server.exe").is_file()
    assert not (output / "llama-cli.exe").exists()
    assert {item["path"] for item in manifest["files"]} == {
        path.name for path in output.iterdir() if path.is_file() and path.name != "runtime-manifest.json"
    }


def test_prepare_rejects_archive_path_traversal(tmp_path):
    lock_path, cache = _fixture(tmp_path, unsafe_name="../outside.dll")
    lock = load_lock(lock_path)

    with pytest.raises(RuntimePreparationError, match="unsafe path"):
        prepare(lock, cache, tmp_path / "tools" / "local-polishing")


def test_refresh_manifest_detects_unexpected_runtime_file(tmp_path):
    lock_path, cache = _fixture(tmp_path)
    lock = load_lock(lock_path)
    output = tmp_path / "tools" / "local-polishing"
    prepare(lock, cache, output)
    (output / "unexpected.exe").write_bytes(b"unexpected")

    with pytest.raises(RuntimePreparationError, match="file set differs"):
        write_manifest(lock, output)


def test_verify_runtime_rejects_modified_runtime_file(tmp_path):
    lock_path, cache = _fixture(tmp_path)
    lock = load_lock(lock_path)
    output = tmp_path / "tools" / "local-polishing"
    prepare(lock, cache, output)

    assert verify_runtime(lock, output)["contract"] == MANIFEST_CONTRACT
    (output / "llama-server.exe").write_bytes(b"modified")

    with pytest.raises(RuntimePreparationError, match="byte count differs from manifest"):
        verify_runtime(lock, output)


def test_verify_runtime_rejects_unexpected_directory(tmp_path):
    lock_path, cache = _fixture(tmp_path)
    lock = load_lock(lock_path)
    output = tmp_path / "tools" / "local-polishing"
    prepare(lock, cache, output)
    (output / "foreign").mkdir()

    with pytest.raises(RuntimePreparationError, match="unexpected non-file entry"):
        verify_runtime(lock, output)


def test_installed_runtime_smoke_verifies_manifest_and_no_model_contract(tmp_path):
    lock_path, cache = _fixture(tmp_path)
    lock = load_lock(lock_path)
    backend = tmp_path / "backend"
    prepare(lock, cache, backend / "tools" / "local-polishing")

    result = validate_resource(backend, run_version=False)

    assert result["ok"] is True
    assert result["releaseTag"] == "b10158"
    assert result["primaryBackend"] == "vulkan"
    assert result["cpuFallback"] is True
    assert result["bundledGgufCount"] == 0


def test_installed_runtime_smoke_rejects_bundled_gguf_weights(tmp_path):
    lock_path, cache = _fixture(tmp_path)
    lock = load_lock(lock_path)
    backend = tmp_path / "backend"
    prepare(lock, cache, backend / "tools" / "local-polishing")
    model = backend / "models" / "unexpected.gguf"
    model.parent.mkdir()
    model.write_bytes(b"model")

    with pytest.raises(LocalPolishingRuntimeSmokeError, match="GGUF"):
        validate_resource(backend, run_version=False)


def test_windows_build_and_installer_run_local_runtime_smoke() -> None:
    root = Path(__file__).resolve().parents[1]
    build = (root / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    installer = (root / "scripts" / "smoke_windows_installer.ps1").read_text(encoding="utf-8")

    assert "scripts\\smoke_local_polishing_runtime.py" in build
    assert "local-polishing-runtime-staged-smoke.json" in build
    assert "scripts\\smoke_local_polishing_runtime.py" in installer
    assert "localPolishingRuntime" in installer
