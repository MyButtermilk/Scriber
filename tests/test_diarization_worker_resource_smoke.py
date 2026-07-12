from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import smoke_diarization_worker_resource as smoke


def _identity_payload(*, self_test: bool = False) -> dict:
    payload = {
        "schemaVersion": 1,
        "ok": True,
        "worker": {"name": smoke.WORKER_NAME, "version": smoke.WORKER_VERSION},
        "engine": {
            "name": "sherpa-onnx",
            "version": smoke.SHERPA_VERSION,
            "linkMode": "static",
        },
    }
    if self_test:
        payload.update(
            {
                "loadsUserAudio": False,
                "loadsModels": False,
                "platform": {"windows": True, "memoryLimit": "jobObject"},
            }
        )
    return payload


def _resource(tmp_path: Path) -> tuple[Path, Path]:
    resource = tmp_path / "backend" / "tools" / "diarization"
    resource.mkdir(parents=True)
    executable = resource / smoke.WORKER_FILE
    executable.write_bytes(b"signed-static-worker")
    manifest = {
        "schemaVersion": 1,
        "distribution": "bundled-signed-scriber-resource",
        "worker": {
            "name": smoke.WORKER_NAME,
            "fileName": smoke.WORKER_FILE,
            "version": smoke.WORKER_VERSION,
            "protocolSchemaVersion": 1,
            "sherpaOnnxVersion": smoke.SHERPA_VERSION,
            "linkMode": "static",
            "sha256": hashlib.sha256(b"signed-static-worker").hexdigest(),
            "byteSize": len(b"signed-static-worker"),
        },
    }
    (resource / smoke.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    return resource, executable


def test_validate_resource_attests_worker_and_optional_model_absence(monkeypatch, tmp_path: Path):
    _resource(tmp_path)
    monkeypatch.setattr(
        smoke,
        "_run_control",
        lambda _executable, argument: _identity_payload(self_test=argument == "--self-test"),
    )
    monkeypatch.setattr(smoke, "_pe_imports", lambda _path: ["KERNEL32.dll", "bcrypt.dll"])

    result = smoke.validate_resource(tmp_path)

    assert result["ok"] is True
    assert result["worker"]["linkMode"] == "static"
    assert result["optionalModelsAbsent"] is True
    assert result["resourceDir"] == "backend/tools/diarization"


def test_validate_resource_rejects_dynamic_onnx_runtime(monkeypatch, tmp_path: Path):
    _resource(tmp_path)
    monkeypatch.setattr(
        smoke,
        "_run_control",
        lambda _executable, argument: _identity_payload(self_test=argument == "--self-test"),
    )
    monkeypatch.setattr(smoke, "_pe_imports", lambda _path: ["onnxruntime.dll"])

    with pytest.raises(RuntimeError, match="statically self-contained"):
        smoke.validate_resource(tmp_path)


def test_validate_resource_rejects_optional_model_in_package(monkeypatch, tmp_path: Path):
    resource, _ = _resource(tmp_path)
    (resource / smoke.SEGMENTATION_MODEL_FILE).write_bytes(b"model")
    monkeypatch.setattr(
        smoke,
        "_run_control",
        lambda _executable, argument: _identity_payload(self_test=argument == "--self-test"),
    )
    monkeypatch.setattr(smoke, "_pe_imports", lambda _path: ["KERNEL32.dll"])

    with pytest.raises(RuntimeError, match="Optional diarization models"):
        smoke.validate_resource(tmp_path)


def test_validate_resource_rejects_manifest_digest_mismatch(monkeypatch, tmp_path: Path):
    resource, executable = _resource(tmp_path)
    executable.write_bytes(b"tampered")
    monkeypatch.setattr(smoke, "_run_control", lambda *_args: _identity_payload())
    monkeypatch.setattr(smoke, "_pe_imports", lambda _path: [])

    with pytest.raises(RuntimeError, match="attestation does not match"):
        smoke.validate_resource(tmp_path)
