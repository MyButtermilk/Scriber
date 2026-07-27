from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.github_release_api import GitHubApiError
from scripts.ci.publish_github_release import (
    PublicationTransactionError,
    publish_verified_github_release,
)

REPOSITORY = "example/scriber"
RELEASE_TAG = "v1.2.3"
RELEASE_ID = 42
EVENT_SHA = "a" * 40
TAG_OBJECT_SHA = "b" * 40
RELEASE_TITLE = "Scriber 1.2.3"
RELEASE_BODY = "## What's Changed\n\n* Transaction-bound release notes.\n"


class FakePublishApi:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.downloaded_files = dict(files)
        self.asset_ids = {name: index + 100 for index, name in enumerate(sorted(files))}
        self.exists = True
        self.draft = True
        self.published_at: str | None = None
        self.release_title = RELEASE_TITLE
        self.target_commitish = EVENT_SHA
        self.body = RELEASE_BODY
        self.release_reads = 0
        self.mutate_body_on_release_read: int | None = None
        self.tamper_body_after_publish = False
        self.asset_list_calls = 0
        self.mutate_asset_on_list_call: int | None = None
        self.tamper_download_after_publish = False
        self.fail_draft_rollback = False
        self.patch_calls: list[tuple[str, dict[str, Any]]] = []
        self.delete_calls: list[str] = []

    def _release(self) -> dict[str, Any]:
        return {
            "id": RELEASE_ID,
            "tag_name": RELEASE_TAG,
            "draft": self.draft,
            "prerelease": False,
            "published_at": self.published_at,
            "name": self.release_title,
            "target_commitish": self.target_commitish,
            "body": self.body,
        }

    def _assets(self) -> list[dict[str, Any]]:
        return [
            {
                "id": self.asset_ids[name],
                "name": name,
                "state": "uploaded",
                "size": len(content),
                "digest": f"sha256:{sha256(content).hexdigest()}",
            }
            for name, content in sorted(self.files.items())
        ]

    def get_json(self, path: str) -> dict[str, Any]:
        if path == f"/repos/{REPOSITORY}/releases/{RELEASE_ID}":
            if not self.exists:
                raise GitHubApiError("not found", status_code=404)
            self.release_reads += 1
            if self.mutate_body_on_release_read == self.release_reads:
                self.body = "Edited immediately before publication"
            return self._release()
        if path == f"/repos/{REPOSITORY}/git/ref/tags/{RELEASE_TAG}":
            return {
                "ref": f"refs/tags/{RELEASE_TAG}",
                "object": {"type": "tag", "sha": TAG_OBJECT_SHA},
            }
        if path == f"/repos/{REPOSITORY}/git/tags/{TAG_OBJECT_SHA}":
            return {
                "sha": TAG_OBJECT_SHA,
                "tag": RELEASE_TAG,
                "object": {"type": "commit", "sha": EVENT_SHA},
            }
        if path == f"/repos/{REPOSITORY}/git/ref/heads/main":
            return {
                "ref": "refs/heads/main",
                "object": {"type": "commit", "sha": EVENT_SHA},
            }
        raise AssertionError(f"Unexpected GitHub API path: {path}")

    def get_json_list(self, path: str) -> list[dict[str, Any]]:
        if "/releases?per_page=100&page=" in path:
            return [self._release()] if self.exists and path.endswith("page=1") else []
        if f"/releases/{RELEASE_ID}/assets?per_page=100&page=" in path:
            self.asset_list_calls += 1
            if self.mutate_asset_on_list_call == self.asset_list_calls:
                first_name = sorted(self.asset_ids)[0]
                self.asset_ids[first_name] += 1000
            return self._assets() if path.endswith("page=1") else []
        raise AssertionError(f"Unexpected GitHub API path: {path}")

    def download_asset(
        self,
        path: str,
        *,
        destination: Path,
        max_bytes: int,
    ) -> dict[str, Any]:
        asset_id = int(path.rsplit("/", 1)[1])
        name = next(name for name, known_id in self.asset_ids.items() if known_id == asset_id)
        content = self.downloaded_files[name]
        assert len(content) <= max_bytes
        destination.write_bytes(content)
        return {
            "sizeBytes": len(content),
            "sha256": sha256(content).hexdigest(),
        }

    def patch_json(self, path: str, value: dict[str, Any]) -> dict[str, Any]:
        assert path == f"/repos/{REPOSITORY}/releases/{RELEASE_ID}"
        self.patch_calls.append((path, dict(value)))
        if value == {"draft": False, "make_latest": "true"}:
            self.draft = False
            self.published_at = "2026-07-27T12:00:00Z"
            if self.tamper_download_after_publish:
                first_name = sorted(self.downloaded_files)[0]
                self.downloaded_files[first_name] = b"tampered after publish"
            if self.tamper_body_after_publish:
                self.body = "Edited immediately after publication"
            return self._release()
        if value == {"draft": True}:
            if self.fail_draft_rollback:
                raise GitHubApiError("rollback failed", status_code=500)
            self.draft = True
            return self._release()
        raise AssertionError(f"Unexpected release patch: {value}")

    def delete(self, path: str) -> None:
        assert path == f"/repos/{REPOSITORY}/releases/{RELEASE_ID}"
        self.delete_calls.append(path)
        self.exists = False


def _expected_dir(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    files = {
        "Scriber_1.2.3_x64-setup.exe": b"installer bytes",
        "latest.json": b'{"version":"1.2.3"}',
    }
    expected = tmp_path / "release-artifacts"
    expected.mkdir()
    for name, content in files.items():
        (expected / name).write_bytes(content)
    return expected, files


def _prior_state(
    tmp_path: Path,
    api: FakePublishApi,
    expected_dir: Path,
) -> Path:
    body_bytes = RELEASE_BODY.encode("utf-8")
    report = {
        "schemaVersion": 1,
        "ok": True,
        "phase": "draft",
        "releaseId": RELEASE_ID,
        "releaseTag": RELEASE_TAG,
        "releaseTitle": RELEASE_TITLE,
        "targetCommitish": EVENT_SHA,
        "releaseBodySizeBytes": len(body_bytes),
        "releaseBodySha256": sha256(body_bytes).hexdigest(),
        "assets": [
            {
                "assetId": asset["id"],
                "name": asset["name"],
                "sizeBytes": asset["size"],
                "sha256": asset["digest"].removeprefix("sha256:"),
                "githubDigest": asset["digest"],
            }
            for asset in api._assets()
        ],
    }
    path = tmp_path / "initial-state.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    api.asset_list_calls = 0
    api.release_reads = 0
    return path


def test_publish_helper_verifies_and_publishes_only_exact_release_id(
    tmp_path: Path,
) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakePublishApi(files)
    prior = _prior_state(tmp_path, api, expected)

    report = publish_verified_github_release(
        api,
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        release_id=RELEASE_ID,
        event_sha=EVENT_SHA,
        expected_dir=expected,
        expected_state_report=prior,
        pre_publish_download_dir=tmp_path / "pre-download",
        post_publish_download_dir=tmp_path / "post-download",
    )

    assert report["ok"] is True
    assert report["releaseId"] == RELEASE_ID
    assert report["draftVerification"]["releaseTitle"] == RELEASE_TITLE
    assert report["publishedVerification"]["releaseBodySha256"] == sha256(RELEASE_BODY.encode("utf-8")).hexdigest()
    assert api.patch_calls == [
        (
            f"/repos/{REPOSITORY}/releases/{RELEASE_ID}",
            {"draft": False, "make_latest": "true"},
        )
    ]
    assert api.delete_calls == []


def test_release_body_change_at_last_pre_publish_read_prevents_patch(
    tmp_path: Path,
) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakePublishApi(files)
    prior = _prior_state(tmp_path, api, expected)
    api.mutate_body_on_release_read = 3

    with pytest.raises(ValueError, match="metadata changed"):
        publish_verified_github_release(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            event_sha=EVENT_SHA,
            expected_dir=expected,
            expected_state_report=prior,
            pre_publish_download_dir=tmp_path / "pre-download",
            post_publish_download_dir=tmp_path / "post-download",
        )

    assert api.patch_calls == []
    assert api.draft is True


def test_release_body_change_in_publish_response_rolls_back_exact_release(
    tmp_path: Path,
) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakePublishApi(files)
    prior = _prior_state(tmp_path, api, expected)
    api.tamper_body_after_publish = True

    with pytest.raises(PublicationTransactionError) as caught:
        publish_verified_github_release(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            event_sha=EVENT_SHA,
            expected_dir=expected,
            expected_state_report=prior,
            pre_publish_download_dir=tmp_path / "pre-download",
            post_publish_download_dir=tmp_path / "post-download",
        )

    assert caught.value.report["rollback"]["action"] == "restored-draft"
    assert api.draft is True
    assert [value for _path, value in api.patch_calls] == [
        {"draft": False, "make_latest": "true"},
        {"draft": True},
    ]


def test_asset_identity_change_at_last_pre_publish_read_prevents_patch(
    tmp_path: Path,
) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakePublishApi(files)
    prior = _prior_state(tmp_path, api, expected)
    api.mutate_asset_on_list_call = 3

    with pytest.raises(ValueError, match="immediately before publication"):
        publish_verified_github_release(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            event_sha=EVENT_SHA,
            expected_dir=expected,
            expected_state_report=prior,
            pre_publish_download_dir=tmp_path / "pre-download",
            post_publish_download_dir=tmp_path / "post-download",
        )

    assert api.patch_calls == []
    assert api.draft is True
    assert api.delete_calls == []


def test_failed_postcondition_rolls_exact_release_back_to_verified_draft(
    tmp_path: Path,
) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakePublishApi(files)
    prior = _prior_state(tmp_path, api, expected)
    api.tamper_download_after_publish = True

    with pytest.raises(PublicationTransactionError) as caught:
        publish_verified_github_release(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            event_sha=EVENT_SHA,
            expected_dir=expected,
            expected_state_report=prior,
            pre_publish_download_dir=tmp_path / "pre-download",
            post_publish_download_dir=tmp_path / "post-download",
        )

    assert caught.value.report["rollback"] == {
        "action": "restored-draft",
        "verified": True,
        "releaseId": RELEASE_ID,
    }
    assert api.draft is True
    assert api.exists is True
    assert api.delete_calls == []
    assert [value for _path, value in api.patch_calls] == [
        {"draft": False, "make_latest": "true"},
        {"draft": True},
    ]


def test_unverifiable_draft_rollback_deletes_only_exact_release_and_verifies_404(
    tmp_path: Path,
) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakePublishApi(files)
    prior = _prior_state(tmp_path, api, expected)
    api.tamper_download_after_publish = True
    api.fail_draft_rollback = True

    with pytest.raises(PublicationTransactionError) as caught:
        publish_verified_github_release(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            event_sha=EVENT_SHA,
            expected_dir=expected,
            expected_state_report=prior,
            pre_publish_download_dir=tmp_path / "pre-download",
            post_publish_download_dir=tmp_path / "post-download",
        )

    assert caught.value.report["rollback"] == {
        "action": "deleted-release",
        "verified": True,
        "releaseId": RELEASE_ID,
    }
    assert api.exists is False
    assert api.delete_calls == [f"/repos/{REPOSITORY}/releases/{RELEASE_ID}"]
