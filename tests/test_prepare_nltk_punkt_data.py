from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from scripts.prepare_nltk_punkt_data import PunktDataError, prepare


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_archive(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _write_lock(path: Path, archive: Path, assets: list[str]) -> None:
    content = archive.read_bytes()
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "contract": "ScriberNltkPunktTabLockV1",
                "url": "https://example.invalid/punkt_tab.zip",
                "archive": {
                    "length": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
                "assets": assets,
            }
        ),
        encoding="utf-8",
    )


def test_prepare_extracts_only_locked_punkt_assets(tmp_path: Path) -> None:
    archive = tmp_path / "punkt.zip"
    _write_archive(
        archive,
        {
            "punkt_tab/README": b"locked readme\n",
            "punkt_tab/english/abbrev_types.txt": b"dr\n",
            "punkt_tab/german/abbrev_types.txt": b"dr\n",
            "punkt_tab/spanish/abbrev_types.txt": b"unused\n",
        },
    )
    lock = tmp_path / "lock.json"
    assets = [
        "README",
        "english/abbrev_types.txt",
        "german/abbrev_types.txt",
    ]
    _write_lock(lock, archive, assets)
    output = tmp_path / "output"

    result = prepare(lock, output, archive_path=archive)

    assert result["ok"] is True
    assert result["fileCount"] == 3
    assert (output / "tokenizers/punkt_tab/README").read_bytes() == b"locked readme\n"
    assert (output / "tokenizers/punkt_tab/english/abbrev_types.txt").is_file()
    assert (output / "tokenizers/punkt_tab/german/abbrev_types.txt").is_file()
    assert not (output / "tokenizers/punkt_tab/spanish").exists()


def test_prepare_rejects_archive_identity_mismatch(tmp_path: Path) -> None:
    archive = tmp_path / "punkt.zip"
    _write_archive(archive, {"punkt_tab/README": b"first"})
    lock = tmp_path / "lock.json"
    _write_lock(lock, archive, ["README"])
    _write_archive(archive, {"punkt_tab/README": b"changed"})

    with pytest.raises(PunktDataError, match="identity"):
        prepare(lock, tmp_path / "output", archive_path=archive)


def test_prepare_rejects_unsafe_archive_member(tmp_path: Path) -> None:
    archive = tmp_path / "punkt.zip"
    _write_archive(
        archive,
        {
            "punkt_tab/README": b"readme",
            "punkt_tab/../escape.txt": b"escape",
        },
    )
    lock = tmp_path / "lock.json"
    _write_lock(lock, archive, ["README"])

    with pytest.raises(PunktDataError, match="unsafe"):
        prepare(lock, tmp_path / "output", archive_path=archive)


def test_prepare_rejects_nonempty_output(tmp_path: Path) -> None:
    archive = tmp_path / "punkt.zip"
    _write_archive(archive, {"punkt_tab/README": b"readme"})
    lock = tmp_path / "lock.json"
    _write_lock(lock, archive, ["README"])
    output = tmp_path / "output"
    output.mkdir()
    (output / "sentinel").write_text("keep", encoding="utf-8")

    with pytest.raises(PunktDataError, match="empty"):
        prepare(lock, output, archive_path=archive)


def test_production_lock_retains_exactly_english_and_german() -> None:
    lock = json.loads(
        (REPO_ROOT / "packaging/nltk-punkt-tab-lock-v1.json").read_text(
            encoding="utf-8"
        )
    )
    required_files = {
        "abbrev_types.txt",
        "collocations.tab",
        "ortho_context.tab",
        "sent_starters.txt",
    }
    expected_assets = {"README"} | {
        f"{language}/{filename}"
        for language in ("english", "german")
        for filename in required_files
    }

    assert set(lock["assets"]) == expected_assets
    assert "/550b6625bcef1f2abff2ff770a5a0d272c9c6b2a/" in lock["url"]
    assert lock["archive"] == {
        "length": 4319076,
        "sha256": "e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106",
    }
