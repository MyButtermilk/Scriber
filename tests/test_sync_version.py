from __future__ import annotations

import pytest

from scripts import sync_version


def test_sync_version_rejects_tag_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF_NAME", "v9.9.9")
    monkeypatch.setattr(sync_version, "read_version", lambda: "1.2.3")

    with pytest.raises(RuntimeError, match="version mismatch"):
        sync_version.main()


def test_sync_version_accepts_matching_tag_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF_NAME", "v1.2.3")
    monkeypatch.setattr(sync_version, "read_version", lambda: "1.2.3")
    monkeypatch.setattr(sync_version, "update_tauri_conf", lambda version: False)
    monkeypatch.setattr(sync_version, "update_cargo_toml", lambda version: False)
    monkeypatch.setattr(sync_version, "update_package_json", lambda version: False)
    monkeypatch.setattr(sync_version, "update_package_lock", lambda version: False)

    assert sync_version.main() == 0
