from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts.collect_meeting_support_bundle_evidence import audit_support_bundle, collect
from scripts.validate_meeting_release_matrix import validate_report


def _bundle(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return path


def test_audit_rejects_audio_database_and_voiceprint_entries(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path / "support.zip",
        {
            "logs/app.log": b"safe",
            "meeting/audio.wav": b"pcm",
            "data/scriber.db": b"sqlite",
            "voiceprint/embedding.bin": b"blob",
        },
    )
    result = audit_support_bundle(bundle)
    assert result["ok"] is False
    assert result["sensitiveFindingCount"] >= 3
    assert result["checks"]["audioAbsent"] is False
    assert result["checks"]["voiceprintArtifactsAbsent"] is False


def test_collect_writes_hash_bound_partial_matrix_evidence(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path / "support.zip",
        {
            "manifest.json": b"{}",
            "runtime.json": b"{}",
            "logs/backend.log": b"redacted diagnostics only",
        },
    )
    smoke = tmp_path / "installed-smoke.json"
    smoke.write_text(
        json.dumps(
            {
                "ok": True,
                "cleanupVerified": True,
                "supportBundle": {
                    "verified": True,
                    "tokenProtected": True,
                    "redactionVerified": True,
                    "downloadPath": str(bundle),
                },
            }
        ),
        encoding="utf-8",
    )
    installer = tmp_path / "Scriber.exe"
    installer.write_bytes(b"installer")
    evidence = tmp_path / "evidence"
    report_path, audit_path = collect(
        installed_smoke_path=smoke,
        installer_path=installer,
        output_dir=evidence,
        app_version="0.4.35",
    )
    assert audit_path.is_file()
    result, payload = validate_report(
        report_path,
        evidence_root=evidence,
        require_signed_installer=False,
    )
    assert result.ok, result.failures
    assert payload is not None
    assert payload["measurements"]["supportBundleSensitiveFindingCount"] == 0


def test_collect_rejects_unverified_installed_smoke(tmp_path: Path) -> None:
    smoke = tmp_path / "installed-smoke.json"
    smoke.write_text(json.dumps({"ok": True, "supportBundle": {"verified": False}}), encoding="utf-8")
    installer = tmp_path / "Scriber.exe"
    installer.write_bytes(b"installer")
    with pytest.raises(ValueError, match="verified=true"):
        collect(
            installed_smoke_path=smoke,
            installer_path=installer,
            output_dir=tmp_path / "evidence",
            app_version="0.4.35",
        )


def test_collect_accepts_persisted_installed_privacy_audit_after_zip_cleanup(tmp_path: Path) -> None:
    smoke = tmp_path / "installed-smoke.json"
    smoke.write_text(
        json.dumps(
            {
                "ok": True,
                "cleanupVerified": True,
                "supportBundle": {
                    "verified": True,
                    "tokenProtected": True,
                    "redactionVerified": True,
                    "downloadPath": str(tmp_path / "already-cleaned.zip"),
                    "entryCount": 13,
                    "meetingPrivacy": {
                        "verified": True,
                        "sensitiveFindingCount": 0,
                        "audioAbsent": True,
                        "transcriptStoresAbsent": True,
                        "outlookCredentialArtifactsAbsent": True,
                        "webhookSecretArtifactsAbsent": True,
                        "voiceprintArtifactsAbsent": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    installer = tmp_path / "Scriber.exe"
    installer.write_bytes(b"installer")
    report, audit = collect(
        installed_smoke_path=smoke,
        installer_path=installer,
        output_dir=tmp_path / "evidence",
        app_version="0.4.35",
    )
    assert report.is_file()
    payload = json.loads(audit.read_text(encoding="utf-8"))
    assert payload["auditSource"] == "persisted-installed-smoke-audit"
