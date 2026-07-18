from __future__ import annotations

from pathlib import Path

import pytest

from scripts.stage_installer_research_payload import StagingError, stage_payload


def _release_fixture(tmp_path: Path) -> tuple[Path, Path]:
    release = tmp_path / "target" / "release"
    backend = release / "backend"
    backend.mkdir(parents=True)
    (release / "scriber-desktop.exe").write_bytes(b"desktop")
    (release / "scriber-audio-sidecar.exe").write_bytes(b"audio")
    (backend / "scriber-backend.exe").write_bytes(b"backend")
    (release / "unrelated.pdb").write_bytes(b"debug")
    notices = tmp_path / "THIRD_PARTY_NOTICES.md"
    notices.write_text("notices\n", encoding="utf-8")
    return release, notices


def test_stages_only_the_nsis_payload_allowlist(tmp_path: Path) -> None:
    release, notices = _release_fixture(tmp_path)
    output = tmp_path / "research" / "payload"

    stage_payload(release_root=release, notices=notices, output=output)

    assert sorted(path.relative_to(output).as_posix() for path in output.rglob("*")) == [
        "THIRD_PARTY_NOTICES.md",
        "backend",
        "backend/scriber-backend.exe",
        "scriber-audio-sidecar.exe",
        "scriber-desktop.exe",
    ]
    assert not (output / "unrelated.pdb").exists()


def test_refuses_to_overwrite_a_prior_replica(tmp_path: Path) -> None:
    release, notices = _release_fixture(tmp_path)
    output = tmp_path / "payload"
    output.mkdir()

    with pytest.raises(StagingError, match="already exists"):
        stage_payload(release_root=release, notices=notices, output=output)


def test_refuses_a_reparse_source(tmp_path: Path) -> None:
    release, notices = _release_fixture(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = release / "backend" / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("creating a Windows symlink requires additional privileges")

    with pytest.raises(StagingError, match="reparse"):
        stage_payload(
            release_root=release,
            notices=notices,
            output=tmp_path / "payload",
        )
