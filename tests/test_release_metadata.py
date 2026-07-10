from __future__ import annotations

import json
from pathlib import Path

from scripts.create_release_metadata import (
    build_latest_json,
    default_release_tag,
    discover_artifacts,
    sha256_file,
    write_metadata,
)


def test_write_release_metadata_creates_checksums_and_latest_json(tmp_path: Path):
    artifact = tmp_path / "Scriber_0.1.0_x64-setup.exe"
    artifact.write_bytes(b"installer")
    output_dir = tmp_path / "metadata"

    result = write_metadata(
        [artifact],
        output_dir=output_dir,
        version="0.1.0",
        notes="Release notes",
        platform="windows-x86_64",
        base_url="https://example.com/releases/v0.1.0",
        repository=None,
        tag=None,
    )

    assert result["ok"] is True
    checksums = (output_dir / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert sha256_file(artifact) in checksums
    assert artifact.name in checksums

    latest = json.loads((output_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["version"] == "0.1.0"
    assert latest["notes"] == "Release notes"
    assert latest["platforms"]["windows-x86_64"]["url"] == (
        "https://example.com/releases/v0.1.0/Scriber_0.1.0_x64-setup.exe"
    )
    assert latest["artifacts"][0]["sha256"] == sha256_file(artifact)


def test_latest_json_uses_github_release_url(tmp_path: Path):
    artifact = tmp_path / "Scriber_0.1.0_x64-setup.exe"
    artifact.write_bytes(b"installer")

    latest = build_latest_json(
        [artifact],
        version="0.1.0",
        notes="",
        pub_date="2026-06-01T00:00:00Z",
        platform="windows-x86_64",
        base_url=None,
        repository="MyButtermilk/Scriber",
        tag="v0.1.0",
    )

    assert latest["platforms"]["windows-x86_64"]["url"] == (
        "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/"
        "Scriber_0.1.0_x64-setup.exe"
    )


def test_default_release_tag_ignores_branch_refs(monkeypatch):
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")

    assert default_release_tag() == ""


def test_default_release_tag_accepts_tag_refs(monkeypatch):
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF_NAME", "v0.1.0")

    assert default_release_tag() == "v0.1.0"


def test_discover_artifacts_does_not_fall_back_to_stale_version(tmp_path: Path):
    stale = tmp_path / "Scriber_0.4.33_x64-setup.exe"
    stale.write_bytes(b"stale")

    assert discover_artifacts(tmp_path, version="0.4.34") == []


def test_discover_artifacts_matches_exact_version_boundary(tmp_path: Path):
    exact = tmp_path / "Scriber_0.4.34_x64-setup.exe"
    future = tmp_path / "Scriber_0.4.340_x64-setup.exe"
    exact.write_bytes(b"exact")
    future.write_bytes(b"future")

    assert discover_artifacts(tmp_path, version="v0.4.34") == [exact]
