from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from scripts.verify_release_asset_set import verify_release_asset_set


def _fixture(root: Path) -> None:
    root.mkdir()
    artifact = root / "Scriber_1.2.3_x64-setup.exe"
    artifact.write_bytes(b"signed installer bytes")
    digest = sha256(artifact.read_bytes()).hexdigest()
    signature = "untrusted comment: test\nsignature\ntrusted comment: test\ntrusted"
    (root / f"{artifact.name}.sig").write_text(signature + "\n", encoding="utf-8")
    metadata = {
        "version": "1.2.3",
        "platforms": {
            "windows-x86_64": {
                "url": f"https://example.test/{artifact.name}",
                "signature": signature,
            }
        },
        "artifacts": [
            {
                "name": artifact.name,
                "url": f"https://example.test/{artifact.name}",
                "sha256": digest,
                "sizeBytes": artifact.stat().st_size,
                "signature": signature,
            }
        ],
    }
    (root / "latest.json").write_text(json.dumps(metadata), encoding="utf-8")
    (root / "SHA256SUMS.txt").write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")


def test_release_asset_round_trip_verifies_exact_set_and_metadata(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected"
    downloaded = tmp_path / "downloaded"
    _fixture(expected)
    shutil.copytree(expected, downloaded)

    report = verify_release_asset_set(expected_dir=expected, downloaded_dir=downloaded)

    assert report["ok"] is True
    assert report["updaterArtifactsValidated"] == 1
    assert report["expectedAssetCount"] == 4


@pytest.mark.parametrize("target", ["installer", "signature", "metadata"])
def test_release_asset_round_trip_rejects_tampering(tmp_path: Path, target: str) -> None:
    expected = tmp_path / "expected"
    downloaded = tmp_path / "downloaded"
    _fixture(expected)
    shutil.copytree(expected, downloaded)
    names = {
        "installer": "Scriber_1.2.3_x64-setup.exe",
        "signature": "Scriber_1.2.3_x64-setup.exe.sig",
        "metadata": "latest.json",
    }
    with (downloaded / names[target]).open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(ValueError, match="mismatch"):
        verify_release_asset_set(expected_dir=expected, downloaded_dir=downloaded)


def test_release_asset_round_trip_rejects_missing_asset(tmp_path: Path) -> None:
    expected = tmp_path / "expected"
    downloaded = tmp_path / "downloaded"
    _fixture(expected)
    shutil.copytree(expected, downloaded)
    (downloaded / "Scriber_1.2.3_x64-setup.exe.sig").unlink()

    with pytest.raises(ValueError, match="asset set differs"):
        verify_release_asset_set(expected_dir=expected, downloaded_dir=downloaded)
