from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.github_release_metadata import MAX_RELEASE_BODY_BYTES
from scripts.ci.sync_draft_release_assets import sync_draft_release_assets

REPOSITORY = "example/scriber"
RELEASE_TAG = "v1.2.3"
RELEASE_ID = 42
TARGET_COMMIT = "b" * 40
TAG_OBJECT = "a" * 40
RELEASE_TITLE = "Scriber 1.2.3"
GENERATED_BODY = "## What's Changed\n\n* Deterministic release notes.\n"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _release(
    release_id: int,
    tag: str,
    *,
    draft: bool,
    name: str = RELEASE_TITLE,
    target_commitish: str = TARGET_COMMIT,
    body: str = GENERATED_BODY,
) -> dict[str, Any]:
    return {
        "id": release_id,
        "tag_name": tag,
        "draft": draft,
        "prerelease": False,
        "published_at": None if draft else "2026-07-27T12:00:00Z",
        "name": name,
        "target_commitish": target_commitish,
        "body": body,
        "upload_url": (f"https://uploads.github.com/repos/{REPOSITORY}/releases/{release_id}/assets{{?name,label}}"),
    }


class FakeNotFound(RuntimeError):
    pass


class FakePublisherApi:
    def __init__(
        self,
        *,
        exists: bool = True,
        draft: bool = True,
        assets: list[tuple[int, str, bytes]] | None = None,
        action_on_release_read: dict[int, str] | None = None,
        tamper_download: bool = False,
        tag_exists: bool = True,
        live_target: str = TARGET_COMMIT,
        release_metadata: dict[str, str] | None = None,
        generated_notes_response: dict[str, Any] | None = None,
        lag_created_release_in_list: bool = False,
    ) -> None:
        metadata = release_metadata or {}
        self.releases: dict[int, dict[str, Any]] = (
            {
                RELEASE_ID: _release(
                    RELEASE_ID,
                    RELEASE_TAG,
                    draft=draft,
                    name=metadata.get("name", RELEASE_TITLE),
                    target_commitish=metadata.get("target_commitish", TARGET_COMMIT),
                    body=metadata.get("body", GENERATED_BODY),
                )
            }
            if exists
            else {}
        )
        self.assets: dict[int, list[dict[str, Any]]] = {RELEASE_ID: []} if exists else {}
        self.asset_bytes: dict[int, bytes] = {}
        for asset_id, name, content in assets or []:
            self.assets[RELEASE_ID].append(
                {
                    "id": asset_id,
                    "name": name,
                    "state": "uploaded",
                    "size": len(content),
                    "digest": f"sha256:{_digest(content)}",
                }
            )
            self.asset_bytes[asset_id] = content
        self.action_on_release_read = action_on_release_read or {}
        self.tamper_download = tamper_download
        self.tag_exists = tag_exists
        self.live_target = live_target
        self.release_reads = 0
        self.release_by_id_reads: list[int] = []
        self.release_list_reads = 0
        self.lag_created_release_in_list = lag_created_release_in_list
        self.created_release_id: int | None = None
        self.next_release_id = 77
        self.next_asset_id = 100
        self.post_payloads: list[dict[str, Any]] = []
        self.generate_notes_payloads: list[dict[str, Any]] = []
        self.generated_notes_response = (
            {"name": RELEASE_TAG, "body": GENERATED_BODY}
            if generated_notes_response is None
            else dict(generated_notes_response)
        )
        self.deleted_ids: list[int] = []
        self.upload_calls: list[dict[str, Any]] = []
        self.downloaded_ids: list[int] = []

    def _apply_release_read_action(self, release_id: int) -> None:
        action = self.action_on_release_read.get(self.release_reads)
        if action == "publish":
            release = self.releases[release_id]
            release["draft"] = False
            release["published_at"] = "2026-07-27T12:00:00Z"
        elif action == "retag-and-recreate":
            self.releases[release_id]["tag_name"] = "v9.9.9"
            replacement_id = self.next_release_id
            self.next_release_id += 1
            self.releases[replacement_id] = _release(replacement_id, RELEASE_TAG, draft=True)
            self.assets[replacement_id] = []
        elif action == "delete-and-recreate":
            del self.releases[release_id]
            self.assets.pop(release_id, None)
            replacement_id = self.next_release_id
            self.next_release_id += 1
            self.releases[replacement_id] = _release(replacement_id, RELEASE_TAG, draft=True)
            self.assets[replacement_id] = []
        elif action is not None:
            raise AssertionError(f"Unknown fake action: {action}")

    def get_json(self, path: str) -> dict[str, Any]:
        if path == f"/repos/{REPOSITORY}/git/ref/tags/{RELEASE_TAG}":
            if not self.tag_exists:
                raise FakeNotFound("The release tag does not exist.")
            return {
                "ref": f"refs/tags/{RELEASE_TAG}",
                "object": {"type": "tag", "sha": TAG_OBJECT},
            }
        if path == f"/repos/{REPOSITORY}/git/tags/{TAG_OBJECT}":
            return {
                "sha": TAG_OBJECT,
                "tag": RELEASE_TAG,
                "object": {"type": "commit", "sha": self.live_target},
            }
        if path == f"/repos/{REPOSITORY}/git/ref/heads/main":
            return {
                "ref": "refs/heads/main",
                "object": {"type": "commit", "sha": self.live_target},
            }
        match = re.fullmatch(rf"/repos/{re.escape(REPOSITORY)}/releases/([0-9]+)", path)
        if match is None:
            raise AssertionError(f"Unexpected GitHub API path: {path}")
        release_id = int(match.group(1))
        if release_id not in self.releases:
            raise FakeNotFound(f"Release {release_id} was deleted.")
        self.release_reads += 1
        self.release_by_id_reads.append(release_id)
        self._apply_release_read_action(release_id)
        if release_id not in self.releases:
            raise FakeNotFound(f"Release {release_id} was deleted.")
        return dict(self.releases[release_id])

    def get_json_list(self, path: str) -> list[dict[str, Any]]:
        if "/releases?per_page=100&page=" in path:
            self.release_list_reads += 1
            if not path.endswith("page=1"):
                return []
            releases = self.releases.values()
            if self.lag_created_release_in_list and self.created_release_id is not None:
                releases = (release for release in releases if release["id"] != self.created_release_id)
            return [dict(release) for release in releases]
        match = re.fullmatch(
            rf"/repos/{re.escape(REPOSITORY)}/releases/([0-9]+)/assets\?per_page=100&page=([0-9]+)",
            path,
        )
        if match is None:
            raise AssertionError(f"Unexpected GitHub API path: {path}")
        release_id = int(match.group(1))
        page = int(match.group(2))
        if release_id not in self.releases:
            raise FakeNotFound(f"Release {release_id} was deleted.")
        return [dict(asset) for asset in self.assets[release_id]] if page == 1 else []

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        expected_status: int = 201,
    ) -> dict[str, Any]:
        if path == f"/repos/{REPOSITORY}/releases/generate-notes":
            assert expected_status == 200
            self.generate_notes_payloads.append(dict(payload))
            return dict(self.generated_notes_response)
        assert path == f"/repos/{REPOSITORY}/releases"
        assert expected_status == 201
        assert not any(release["tag_name"] == payload["tag_name"] for release in self.releases.values())
        self.post_payloads.append(dict(payload))
        release_id = self.next_release_id
        self.next_release_id += 1
        created = _release(
            release_id,
            payload["tag_name"],
            draft=payload["draft"],
            name=payload["name"],
            target_commitish=payload["target_commitish"],
            body=GENERATED_BODY,
        )
        self.releases[release_id] = created
        self.assets[release_id] = []
        self.created_release_id = release_id
        return dict(created)

    def delete(self, path: str) -> None:
        match = re.fullmatch(rf"/repos/{re.escape(REPOSITORY)}/releases/assets/([0-9]+)", path)
        if match is None:
            raise AssertionError(f"Unexpected GitHub API path: {path}")
        asset_id = int(match.group(1))
        release_id = next(
            (
                candidate_id
                for candidate_id, assets in self.assets.items()
                if any(asset["id"] == asset_id for asset in assets)
            ),
            None,
        )
        assert release_id is not None
        assert self.releases[release_id]["draft"] is True, "attempted asset deletion on a published release"
        self.deleted_ids.append(asset_id)
        self.assets[release_id] = [asset for asset in self.assets[release_id] if asset["id"] != asset_id]
        self.asset_bytes.pop(asset_id, None)

    def upload_asset(
        self,
        upload_url: str,
        *,
        repository: str,
        release_id: int,
        asset_name: str,
        source: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> dict[str, Any]:
        assert repository == REPOSITORY
        assert release_id in self.releases
        assert upload_url == self.releases[release_id]["upload_url"]
        assert self.releases[release_id]["tag_name"] == RELEASE_TAG
        assert self.releases[release_id]["draft"] is True, "attempted asset upload on a published release"
        content = source.read_bytes()
        assert len(content) == expected_size
        assert _digest(content) == expected_sha256
        assert not any(asset["name"] == asset_name for asset in self.assets[release_id])
        asset_id = self.next_asset_id
        self.next_asset_id += 1
        asset = {
            "id": asset_id,
            "name": asset_name,
            "state": "uploaded",
            "size": len(content),
            "digest": f"sha256:{_digest(content)}",
        }
        self.assets[release_id].append(asset)
        self.asset_bytes[asset_id] = content
        self.upload_calls.append(
            {
                "releaseId": release_id,
                "assetId": asset_id,
                "name": asset_name,
            }
        )
        return dict(asset)

    def download_asset(
        self,
        path: str,
        *,
        destination: Path,
        max_bytes: int,
    ) -> dict[str, Any]:
        match = re.fullmatch(rf"/repos/{re.escape(REPOSITORY)}/releases/assets/([0-9]+)", path)
        if match is None:
            raise AssertionError(f"Unexpected GitHub API path: {path}")
        asset_id = int(match.group(1))
        content = self.asset_bytes[asset_id]
        if self.tamper_download:
            content += b"tampered"
        assert len(content) <= max_bytes or self.tamper_download
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        self.downloaded_ids.append(asset_id)
        return {
            "sizeBytes": len(content),
            "sha256": _digest(content),
        }


def _expected_dir(tmp_path: Path) -> Path:
    expected = tmp_path / "release-artifacts"
    expected.mkdir()
    (expected / "Scriber_1.2.3_x64-setup.exe").write_bytes(b"installer")
    (expected / "latest.json").write_bytes(b'{"version":"1.2.3"}')
    return expected


def _sync(api: FakePublisherApi, tmp_path: Path) -> dict[str, Any]:
    return sync_draft_release_assets(
        api,
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        release_title=RELEASE_TITLE,
        target_commitish=TARGET_COMMIT,
        expected_dir=_expected_dir(tmp_path),
        verification_dir=tmp_path / "verified",
    )


@pytest.mark.parametrize(
    ("release_title", "target_commitish", "message"),
    [
        ("Scriber v1.2.3", TARGET_COMMIT, "exactly 'Scriber 1.2.3'"),
        ("Scriber 1.2.3", "short-sha", "canonical 40-character"),
    ],
)
def test_release_creation_identity_inputs_are_canonical(
    tmp_path: Path,
    release_title: str,
    target_commitish: str,
    message: str,
) -> None:
    api = FakePublisherApi(exists=False)

    with pytest.raises(ValueError, match=message):
        sync_draft_release_assets(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_title=release_title,
            target_commitish=target_commitish,
            expected_dir=_expected_dir(tmp_path),
            verification_dir=tmp_path / "verified",
        )

    assert api.post_payloads == []


def test_missing_release_is_created_as_empty_draft_then_uploaded_by_numeric_id(tmp_path: Path) -> None:
    api = FakePublisherApi(exists=False)

    report = _sync(api, tmp_path)

    assert report["created"] is True
    assert report["releaseId"] == 77
    assert report["phase"] == "draft"
    assert report["publishedAt"] is None
    assert report["assetCount"] == 2
    assert report["releaseTitle"] == RELEASE_TITLE
    assert report["targetCommitish"] == TARGET_COMMIT
    assert report["releaseBodySizeBytes"] == len(GENERATED_BODY.encode("utf-8"))
    assert report["releaseBodySha256"] == _digest(GENERATED_BODY.encode("utf-8"))
    assert "body" not in report
    assert api.generate_notes_payloads == [
        {
            "tag_name": RELEASE_TAG,
            "target_commitish": TARGET_COMMIT,
        }
    ]
    assert api.post_payloads == [
        {
            "tag_name": RELEASE_TAG,
            "target_commitish": TARGET_COMMIT,
            "name": "Scriber 1.2.3",
            "draft": True,
            "prerelease": False,
            "generate_release_notes": True,
        }
    ]
    assert {call["releaseId"] for call in api.upload_calls} == {77}
    assert {asset["assetId"] for asset in report["assets"]} == set(api.downloaded_ids)


def test_created_draft_stays_id_bound_while_release_list_visibility_lags(tmp_path: Path) -> None:
    api = FakePublisherApi(
        exists=False,
        lag_created_release_in_list=True,
    )

    report = _sync(api, tmp_path)

    assert report["created"] is True
    assert report["releaseId"] == 77
    assert api.created_release_id == 77
    assert api.release_list_reads == 2
    assert api.release_by_id_reads
    assert set(api.release_by_id_reads) == {77}
    assert {call["releaseId"] for call in api.upload_calls} == {77}


@pytest.mark.parametrize(
    ("api", "message"),
    [
        (FakePublisherApi(exists=False, tag_exists=False), "does not exist"),
        (FakePublisherApi(exists=False, live_target="c" * 40), "not event SHA"),
    ],
)
def test_missing_release_requires_verified_live_tag_before_creation(
    tmp_path: Path,
    api: FakePublisherApi,
    message: str,
) -> None:
    with pytest.raises((ValueError, FakeNotFound), match=message):
        _sync(api, tmp_path)

    assert api.post_payloads == []
    assert api.generate_notes_payloads == []


@pytest.mark.parametrize(
    ("release_metadata", "message"),
    [
        ({"name": "Foreign title"}, "metadata does not exactly match"),
        ({"target_commitish": "c" * 40}, "metadata does not exactly match"),
        ({"body": "Edited release notes"}, "metadata does not exactly match"),
    ],
)
def test_existing_draft_with_foreign_metadata_is_never_adopted_or_mutated(
    tmp_path: Path,
    release_metadata: dict[str, str],
    message: str,
) -> None:
    api = FakePublisherApi(
        assets=[(10, "stale.zip", b"stale")],
        release_metadata=release_metadata,
    )

    with pytest.raises(ValueError, match=message):
        _sync(api, tmp_path)

    assert api.post_payloads == []
    assert api.deleted_ids == []
    assert api.upload_calls == []


@pytest.mark.parametrize(
    "generated_notes_response",
    [
        {},
        {"body": None},
        {"body": "x" * (MAX_RELEASE_BODY_BYTES + 1)},
        {"body": "\ud800"},
    ],
)
def test_invalid_or_oversized_generated_notes_fail_before_release_mutation(
    tmp_path: Path,
    generated_notes_response: dict[str, Any],
) -> None:
    api = FakePublisherApi(
        exists=False,
        generated_notes_response=generated_notes_response,
    )

    with pytest.raises(ValueError, match="release notes body"):
        _sync(api, tmp_path)

    assert api.post_payloads == []
    assert api.deleted_ids == []
    assert api.upload_calls == []


def test_existing_draft_rerun_replaces_every_asset_by_exact_ids(tmp_path: Path) -> None:
    api = FakePublisherApi(
        assets=[
            (10, "Scriber_1.2.3_x64-setup.exe", b"old-installer"),
            (11, "latest.json", b'{"version":"old"}'),
        ]
    )

    first = _sync(api, tmp_path)
    first_uploaded_ids = [asset["assetId"] for asset in first["assets"]]

    second_dir = tmp_path / "rerun"
    second_dir.mkdir()
    second = _sync(api, second_dir)
    second_uploaded_ids = [asset["assetId"] for asset in second["assets"]]

    assert first["created"] is False
    assert first["deletedAssetIds"] == [10, 11]
    assert second["created"] is False
    assert second["deletedAssetIds"] == sorted(first_uploaded_ids)
    assert set(first_uploaded_ids).isdisjoint(second_uploaded_ids)
    assert {call["releaseId"] for call in api.upload_calls} == {RELEASE_ID}
    assert api.post_payloads == []
    assert api.generate_notes_payloads == [
        {"tag_name": RELEASE_TAG, "target_commitish": TARGET_COMMIT},
        {"tag_name": RELEASE_TAG, "target_commitish": TARGET_COMMIT},
    ]


@pytest.mark.parametrize("action", ["retag-and-recreate", "delete-and-recreate"])
def test_retag_or_delete_recreate_before_delete_never_mutates_either_release(
    tmp_path: Path,
    action: str,
) -> None:
    api = FakePublisherApi(
        assets=[(10, "stale.zip", b"stale")],
        # Reads 1-3 bind the discovered release. Read 4 is the exact-ID read
        # immediately before the first DELETE.
        action_on_release_read={4: action},
    )

    with pytest.raises((ValueError, FakeNotFound)):
        _sync(api, tmp_path)

    assert api.deleted_ids == []
    assert api.upload_calls == []


def test_publish_on_final_preupload_read_causes_no_asset_mutation(tmp_path: Path) -> None:
    api = FakePublisherApi(
        # With an initially empty draft, read 6 is the exact-ID read that is
        # intentionally the final API operation before the first upload.
        action_on_release_read={6: "publish"},
    )

    with pytest.raises(ValueError, match="published"):
        _sync(api, tmp_path)

    assert api.deleted_ids == []
    assert api.upload_calls == []


def test_publish_on_final_predelete_read_causes_no_asset_mutation(tmp_path: Path) -> None:
    api = FakePublisherApi(
        assets=[(10, "stale.zip", b"stale")],
        action_on_release_read={4: "publish"},
    )

    with pytest.raises(ValueError, match="published"):
        _sync(api, tmp_path)

    assert api.deleted_ids == []
    assert api.upload_calls == []


def test_preexisting_published_release_is_never_mutated(tmp_path: Path) -> None:
    api = FakePublisherApi(
        draft=False,
        assets=[(10, "stale.zip", b"stale")],
    )

    with pytest.raises(ValueError, match="published"):
        _sync(api, tmp_path)

    assert api.post_payloads == []
    assert api.deleted_ids == []
    assert api.upload_calls == []


def test_post_upload_download_hash_must_match_exact_asset_id(tmp_path: Path) -> None:
    api = FakePublisherApi(tamper_download=True)

    with pytest.raises(ValueError, match="Downloaded GitHub release asset bytes"):
        _sync(api, tmp_path)

    assert api.upload_calls
    assert api.downloaded_ids == [api.upload_calls[0]["assetId"]]
