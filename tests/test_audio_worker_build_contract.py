"""The audio product must never inherit the desktop release identity."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_audio_worker_has_one_shared_source_and_no_tauri_application_build_dependency() -> None:
    desktop = tomllib.loads((ROOT / "Frontend/src-tauri/Cargo.toml").read_text(encoding="utf-8"))
    worker_root = ROOT / "native/scriber-audio-sidecar"
    worker = tomllib.loads((worker_root / "Cargo.toml").read_text(encoding="utf-8"))
    # Tauri invokes Cargo with --bins. Keeping an audio target in this package
    # would silently recompile it on every desktop version/frontend change.
    assert "scriber-audio-sidecar" not in {binary["name"] for binary in desktop["bin"]}
    assert len(worker["bin"]) == 1
    assert worker["bin"][0]["name"] == "scriber-audio-sidecar"
    assert (worker_root / worker["bin"][0]["path"]).resolve() == (
        ROOT / "Frontend/src-tauri/src/audio_sidecar.rs"
    ).resolve()
    assert "tauri-build" not in worker.get("build-dependencies", {})
    assert "tauri" not in worker.get("dependencies", {})
    assert "aec3" not in desktop["dependencies"]
    assert worker["dependencies"]["aec3"] == "=0.2.0"
    assert worker["features"]["default"] == []


def test_audio_worker_is_one_external_install_root_binary() -> None:
    config = json.loads((ROOT / "Frontend/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert config["bundle"]["externalBin"] == ["resources/audio-sidecar/scriber-audio-sidecar"]
    assert not any("audio-sidecar" in source for source in config["bundle"]["resources"])


def test_audio_cache_only_validation_does_not_require_a_local_rust_compiler(tmp_path: Path) -> None:
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell 7 is required for the release staging regression")
    required = [
        "backend_runtime/contract.py",
        "native/scriber-audio-sidecar/Cargo.toml",
        "native/scriber-audio-sidecar/Cargo.lock",
        "native/scriber-audio-sidecar/build.rs",
        "native/scriber-audio-sidecar/windows-app-manifest.xml",
        "Frontend/src-tauri/icons/icon.ico",
        *(
            f"Frontend/src-tauri/src/{name}.rs"
            for name in (
                "audio_sidecar",
                "audio_codec",
                "audio_frame_pipe",
                "audio_prepare",
                "meeting_aec",
                "redaction",
            )
        ),
    ]
    for relative in required:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    # Execute the real entrypoint with a Rust-free PATH. A cache-only miss must
    # report the missing validated artifact, never probe/install a compiler.
    environment = dict(os.environ, PATH=str(tmp_path))
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts/build_tauri_backend_sidecar.ps1"),
            "-RepoRoot",
            str(tmp_path),
            "-RustAudioStageFromCacheOnly",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "cache is missing, stale, or does not match worker version 0.1.0" in result.stderr
    assert "rustc" not in result.stderr
