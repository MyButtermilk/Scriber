from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import write_diarization_worker_manifest as manifest_writer


def test_manifest_attests_exact_bundled_worker(monkeypatch, tmp_path: Path):
    executable = tmp_path / manifest_writer.WORKER_FILE
    executable.write_bytes(b"static-rust-worker")
    def probe(_executable: Path, argument: str):
        if argument == "--version":
            return {"ok": True}
        return {
            "schemaVersion": 1,
            "ok": True,
            "loadsUserAudio": False,
            "loadsModels": False,
            "platform": {"windows": True, "memoryLimit": "jobObject"},
        }

    monkeypatch.setattr(manifest_writer, "_probe", probe)

    output = manifest_writer.write_manifest(
        executable, tmp_path / manifest_writer.MANIFEST_FILE
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["schemaVersion"] == 1
    assert payload["distribution"] == "bundled-signed-scriber-resource"
    assert payload["worker"]["fileName"] == manifest_writer.WORKER_FILE
    assert payload["worker"]["linkMode"] == "static"
    assert payload["worker"]["byteSize"] == len(b"static-rust-worker")
    assert payload["worker"]["sha256"] == hashlib.sha256(b"static-rust-worker").hexdigest()


def test_manifest_cannot_be_redirected_away_from_worker(monkeypatch, tmp_path: Path):
    executable = tmp_path / manifest_writer.WORKER_FILE
    executable.write_bytes(b"worker")
    monkeypatch.setattr(manifest_writer, "_probe", lambda _executable, _argument: {"ok": True})

    with pytest.raises(ValueError, match="next to the worker"):
        manifest_writer.write_manifest(executable, tmp_path / "elsewhere" / "worker.json")
