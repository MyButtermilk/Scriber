from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.ci.prepare_draft_release_assets import prepare_draft_release_assets

REPOSITORY = "example/scriber"
RELEASE_TAG = "v1.2.3"
RELEASE_ID = 42


class FakeDraftApi:
    def __init__(
        self,
        *,
        assets: list[dict[str, Any]] | None = None,
        draft: bool = True,
        exists: bool = True,
        publish_before_delete: bool = False,
    ) -> None:
        self.assets = [
            {
                **asset,
                "size": asset.get("size", 1),
                "digest": asset.get("digest", f"sha256:{'a' * 64}"),
            }
            for asset in (assets or [])
        ]
        self.draft = draft
        self.exists = exists
        self.publish_before_delete = publish_before_delete
        self.deleted_ids: list[int] = []
        self.release_id_reads = 0

    def _release(self) -> dict[str, Any]:
        return {
            "id": RELEASE_ID,
            "tag_name": RELEASE_TAG,
            "draft": self.draft,
        }

    def get_json(self, path: str) -> dict[str, Any]:
        if path.endswith(f"/releases/{RELEASE_ID}"):
            self.release_id_reads += 1
            if self.publish_before_delete and self.release_id_reads == 2:
                self.draft = False
            return self._release()
        raise AssertionError(f"Unexpected GitHub API path: {path}")

    def get_json_list(self, path: str) -> list[dict[str, Any]]:
        assert "/releases/tags/" not in path
        if "/releases?per_page=100&page=" in path:
            return [self._release()] if self.exists and path.endswith("page=1") else []
        if f"/releases/{RELEASE_ID}/assets?per_page=100&page=" in path:
            return [dict(asset) for asset in self.assets] if path.endswith("page=1") else []
        raise AssertionError(f"Unexpected GitHub API path: {path}")

    def delete(self, path: str) -> None:
        asset_id = int(path.rsplit("/", 1)[1])
        self.deleted_ids.append(asset_id)
        self.assets = [asset for asset in self.assets if asset["id"] != asset_id]


def _expected_dir(tmp_path: Path) -> Path:
    expected = tmp_path / "release-artifacts"
    expected.mkdir(exist_ok=True)
    (expected / "installer.exe").write_bytes(b"installer")
    (expected / "latest.json").write_text("{}", encoding="utf-8")
    return expected


def test_missing_release_requires_no_cleanup(tmp_path: Path) -> None:
    api = FakeDraftApi(exists=False)

    report = prepare_draft_release_assets(
        api,
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        expected_dir=_expected_dir(tmp_path),
    )

    assert report["releaseExists"] is False
    assert report["deletedAssetIds"] == []


def test_published_release_is_never_mutated(tmp_path: Path) -> None:
    api = FakeDraftApi(
        draft=False,
        assets=[{"id": 1, "name": "stale.zip", "state": "uploaded"}],
    )

    with pytest.raises(ValueError, match="published"):
        prepare_draft_release_assets(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            expected_dir=_expected_dir(tmp_path),
        )

    assert api.deleted_ids == []


def test_draft_cleanup_deletes_stale_and_incomplete_assets_by_id(tmp_path: Path) -> None:
    api = FakeDraftApi(
        assets=[
            {"id": 10, "name": "installer.exe", "state": "uploaded"},
            {"id": 11, "name": "old-installer.exe", "state": "uploaded"},
            {"id": 12, "name": "latest.json", "state": "new"},
        ],
    )

    report = prepare_draft_release_assets(
        api,
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        expected_dir=_expected_dir(tmp_path),
    )

    assert report["releaseExists"] is True
    assert report["deletedAssetIds"] == [11, 12]
    assert report["retainedAssetNames"] == ["installer.exe"]
    assert api.deleted_ids == [11, 12]

    rerun = prepare_draft_release_assets(
        api,
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        expected_dir=_expected_dir(tmp_path),
    )
    assert rerun["releaseId"] == RELEASE_ID
    assert rerun["deletedAssetIds"] == []


def test_duplicate_expected_assets_are_removed_for_clean_reupload(tmp_path: Path) -> None:
    api = FakeDraftApi(
        assets=[
            {"id": 20, "name": "installer.exe", "state": "uploaded"},
            {"id": 21, "name": "installer.exe", "state": "uploaded"},
        ],
    )

    report = prepare_draft_release_assets(
        api,
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        expected_dir=_expected_dir(tmp_path),
    )

    assert report["deletedAssetIds"] == [20, 21]
    assert report["retainedAssetNames"] == []


def test_cleanup_rechecks_draft_state_before_every_delete(tmp_path: Path) -> None:
    api = FakeDraftApi(
        assets=[{"id": 30, "name": "stale.zip", "state": "uploaded"}],
        publish_before_delete=True,
    )

    with pytest.raises(ValueError, match="published"):
        prepare_draft_release_assets(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            expected_dir=_expected_dir(tmp_path),
        )

    assert api.deleted_ids == []
