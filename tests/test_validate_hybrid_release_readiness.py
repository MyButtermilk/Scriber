from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from scripts.validate_hybrid_release_readiness import validate_release_readiness
from scripts.validate_tauri_updater_metadata import sha256_file


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_hybrid_release_readiness.py"


SCENARIO_FIXTURES = {
    "usb-add": (
        {"expectAdded": "usb", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [{"deviceId": "USB Mic", "label": "USB Mic"}], "removed": [], "defaultChanged": False},
        {},
    ),
    "usb-remove": (
        {"expectAdded": "", "expectRemoved": "usb", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [], "removed": [{"deviceId": "USB Mic", "label": "USB Mic"}], "defaultChanged": False},
        {},
    ),
    "dock-disconnect": (
        {"expectAdded": "", "expectRemoved": "dock", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [], "removed": [{"deviceId": "Dock Mic", "label": "Dock Mic"}], "defaultChanged": False},
        {},
    ),
    "dock-connect": (
        {"expectAdded": "dock", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [{"deviceId": "Dock Mic", "label": "Dock Mic"}], "removed": [], "defaultChanged": False},
        {},
    ),
    "bluetooth-add": (
        {"expectAdded": "bluetooth", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [{"deviceId": "Bluetooth Headset", "label": "Bluetooth Headset"}], "removed": [], "defaultChanged": False},
        {},
    ),
    "bluetooth-remove": (
        {"expectAdded": "", "expectRemoved": "bluetooth", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [], "removed": [{"deviceId": "Bluetooth Headset", "label": "Bluetooth Headset"}], "defaultChanged": False},
        {},
    ),
    "default-mic-change": (
        {"expectAdded": "", "expectRemoved": "", "expectDefaultChanged": True, "expectFavoriteFallback": False},
        {"added": [], "removed": [], "defaultChanged": True},
        {},
    ),
    "favorite-fallback": (
        {"expectAdded": "", "expectRemoved": "favorite", "expectDefaultChanged": False, "expectFavoriteFallback": True},
        {"added": [], "removed": [{"deviceId": "Favorite Mic", "label": "Favorite Mic"}], "defaultChanged": False},
        {"micDevice": "Built-in Mic", "favoriteMic": "Favorite Mic", "favoriteMicAvailable": False},
    ),
}


def write_full_hardware_matrix(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for scenario, (expectations, change, settings_after) in SCENARIO_FIXTURES.items():
        payload = {
            "ok": True,
            "scenarios": [scenario],
            "planOnly": False,
            "assumeCompleted": True,
            "result": {
                "before": [],
                "after": [],
                "settingsBefore": {},
                "settingsAfter": settings_after,
                "change": change,
                "expectations": expectations,
                "failures": [],
            },
        }
        (directory / f"microphone-hardware-{scenario}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )


def write_signed_release_fixture(tmp_path: Path, *, signature: str = "signed-update") -> tuple[Path, Path, Path]:
    artifact_dir = tmp_path / "release-artifacts"
    artifact_dir.mkdir()
    artifact = artifact_dir / "Scriber_0.1.0_x64-setup.exe"
    artifact.write_bytes(b"signed Scriber setup")
    checksum = sha256(artifact.read_bytes()).hexdigest()
    metadata = tmp_path / "latest.json"
    metadata.write_text(
        json.dumps(
            {
                "version": "0.1.0",
                "notes": "Release notes",
                "pub_date": "2026-06-02T12:00:00Z",
                "platforms": {
                    "windows-x86_64": {
                        "signature": signature,
                        "url": "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/Scriber_0.1.0_x64-setup.exe",
                    }
                },
                "artifacts": [
                    {
                        "name": artifact.name,
                        "url": "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/Scriber_0.1.0_x64-setup.exe",
                        "sha256": checksum,
                        "sizeBytes": artifact.stat().st_size,
                        "signature": signature,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(f"{checksum}  {artifact.name}\n", encoding="utf-8")
    return metadata, artifact_dir, sums


def write_authenticode_report(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "ok": True,
                "count": 1,
                "artifacts": [
                    {
                        "path": "Scriber_0.1.0_x64-setup.exe",
                        "status": "Valid",
                        "signerSubject": "CN=Scriber Release Publisher",
                        "timestampSubject": "CN=Timestamp Authority",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def write_publication_report(path: Path, metadata: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "ok": True,
                "url": "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
                "statusCode": 200,
                "requireSignatures": True,
                "metadataSha256": sha256_file(metadata),
            }
        ),
        encoding="utf-8",
    )


def write_complete_evidence(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    hardware_dir = tmp_path / "hardware"
    write_full_hardware_matrix(hardware_dir)
    metadata, artifact_dir, sums = write_signed_release_fixture(tmp_path)
    authenticode_report = tmp_path / "authenticode.json"
    publication_report = tmp_path / "updater-publication.json"
    write_authenticode_report(authenticode_report)
    write_publication_report(publication_report, metadata)
    return hardware_dir, metadata, artifact_dir, sums, publication_report, authenticode_report


def test_validate_release_readiness_accepts_complete_evidence(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, publication_report, authenticode_report = write_complete_evidence(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        expected_authenticode_publisher="Scriber Release Publisher",
        require_authenticode_timestamp=True,
    )

    assert result["ok"] is True
    assert {check["name"] for check in result["checks"]} == {
        "physicalMicrophoneMatrix",
        "signedTauriUpdaterMetadata",
        "publishedUpdaterManifest",
        "authenticodeSignatures",
    }


def test_validate_release_readiness_rejects_missing_external_reports(tmp_path: Path) -> None:
    hardware_dir = tmp_path / "hardware"
    write_full_hardware_matrix(hardware_dir)
    metadata, artifact_dir, sums = write_signed_release_fixture(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
    )

    assert result["ok"] is False
    failures_by_name = {check["name"]: check["failures"] for check in result["checks"]}
    assert "published updater evidence report is required" in failures_by_name["publishedUpdaterManifest"]
    assert "Authenticode validation report is required" in failures_by_name["authenticodeSignatures"]


def test_validate_release_readiness_rejects_unsigned_updater_metadata(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    data = json.loads(metadata.read_text(encoding="utf-8"))
    data["platforms"]["windows-x86_64"]["signature"] = ""
    data["artifacts"][0]["signature"] = ""
    metadata.write_text(json.dumps(data), encoding="utf-8")
    write_publication_report(publication_report, metadata)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
    )

    assert result["ok"] is False
    updater_check = next(check for check in result["checks"] if check["name"] == "signedTauriUpdaterMetadata")
    assert any("signature is required" in failure for failure in updater_check["failures"])


def test_validate_release_readiness_cli_writes_summary(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    output = tmp_path / "readiness.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(VALIDATE_SCRIPT),
            "--hardware-input-dir",
            str(hardware_dir),
            "--updater-metadata",
            str(metadata),
            "--updater-artifact-dir",
            str(artifact_dir),
            "--sha256sums",
            str(sums),
            "--updater-publication-report",
            str(publication_report),
            "--authenticode-report",
            str(authenticode_report),
            "--expected-authenticode-publisher",
            "Scriber Release Publisher",
            "--require-authenticode-timestamp",
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    stdout_payload = json.loads(completed.stdout)
    written_payload = json.loads(output.read_text(encoding="utf-8"))
    assert stdout_payload["ok"] is True
    assert written_payload == stdout_payload
