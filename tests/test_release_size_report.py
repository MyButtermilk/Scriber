from __future__ import annotations

from pathlib import Path

import pytest

from scripts.create_release_size_report import build_report, size_mb


def write_bytes(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_release_size_report_gates_largest_artifact(tmp_path: Path) -> None:
    small = tmp_path / "Scriber_0.1.0_x64-setup.exe"
    large = tmp_path / "Scriber_0.1.0_x64.msi"
    write_bytes(small, 2 * 1024 * 1024)
    write_bytes(large, 3 * 1024 * 1024)

    report = build_report([small, large], max_installer_mb=4)

    assert report["ok"] is True
    assert report["artifactCount"] == 2
    assert report["largestArtifactMb"] == size_mb(3 * 1024 * 1024)
    assert report["budgets"]["installer"]["withinBudget"] is True


def test_release_size_report_fails_when_artifact_exceeds_budget(tmp_path: Path) -> None:
    artifact = tmp_path / "Scriber_0.1.0_x64-setup.exe"
    write_bytes(artifact, 3 * 1024 * 1024)

    report = build_report([artifact], max_installer_mb=2)

    assert report["ok"] is False
    assert report["budgets"]["installer"]["withinBudget"] is False


def test_release_size_report_includes_installed_app_top_files(tmp_path: Path) -> None:
    artifact = tmp_path / "bundle" / "Scriber_0.1.0_x64-setup.exe"
    install_dir = tmp_path / "installed"
    write_bytes(artifact, 1 * 1024 * 1024)
    write_bytes(install_dir / "scriber-desktop.exe", 4 * 1024 * 1024)
    write_bytes(install_dir / "backend" / "scriber-backend.exe", 2 * 1024 * 1024)

    report = build_report(
        [artifact],
        install_dir=install_dir,
        max_installer_mb=2,
        max_installed_mb=8,
        top_files_limit=1,
    )

    assert report["ok"] is True
    assert report["installedApp"]["budget"]["withinBudget"] is True
    assert report["installedApp"]["fileCount"] == 2
    assert report["installedApp"]["topFiles"][0]["path"] == "scriber-desktop.exe"


def test_release_size_report_can_use_installer_smoke_install_size(tmp_path: Path) -> None:
    artifact = tmp_path / "bundle" / "Scriber_0.1.0_x64-setup.exe"
    smoke_report = tmp_path / "installed-package-smoke.json"
    write_bytes(artifact, 1 * 1024 * 1024)
    smoke_report.write_text(
        """{
          "ok": true,
          "installSize": {
            "path": "C:/tmp/Scriber",
            "fileCount": 2,
            "totalBytes": 6291456,
            "totalMb": 6.0,
            "topFiles": [
              {"path": "scriber-desktop.exe", "sizeBytes": 4194304, "sizeMb": 4.0}
            ]
          }
        }""",
        encoding="utf-8",
    )

    report = build_report(
        [artifact],
        installed_smoke_report=smoke_report,
        max_installer_mb=2,
        max_installed_mb=8,
    )

    assert report["ok"] is True
    assert report["installedApp"]["budget"]["withinBudget"] is True
    assert report["installedApp"]["fileCount"] == 2
    assert report["installedApp"]["topFiles"][0]["path"] == "scriber-desktop.exe"


def test_release_size_report_requires_existing_artifacts(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_report([tmp_path / "missing.exe"])
