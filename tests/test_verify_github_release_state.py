from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.verify_github_release_state import verify_github_release_state

REPOSITORY = "example/scriber"
RELEASE_TAG = "v1.2.3"
RELEASE_ID = 42
RELEASE_TITLE = "Scriber 1.2.3"
TARGET_COMMIT = "a" * 40
RELEASE_BODY = "## What's Changed\n\n* Verified release notes.\n"


class FakeReleaseApi:
    def __init__(
        self,
        files: dict[str, bytes],
        *,
        draft: bool = True,
        release_id: int = RELEASE_ID,
        body: str = RELEASE_BODY,
    ) -> None:
        self.files = dict(files)
        self.downloaded_files = dict(files)
        self.draft = draft
        self.release_id = release_id
        self.body = body
        self.paths: list[str] = []
        self.asset_ids = {name: index + 100 for index, name in enumerate(sorted(files))}

    def _release(self) -> dict[str, Any]:
        return {
            "id": self.release_id,
            "tag_name": RELEASE_TAG,
            "draft": self.draft,
            "prerelease": False,
            "published_at": None if self.draft else "2026-07-27T12:00:00Z",
            "name": RELEASE_TITLE,
            "target_commitish": TARGET_COMMIT,
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
        self.paths.append(path)
        if path.endswith(f"/releases/{self.release_id}"):
            return self._release()
        raise AssertionError(f"Unexpected GitHub API path: {path}")

    def get_json_list(self, path: str) -> list[dict[str, Any]]:
        self.paths.append(path)
        assert "/releases/tags/" not in path
        if "/releases?per_page=100&page=" in path:
            return [self._release()] if path.endswith("page=1") else []
        if f"/releases/{self.release_id}/assets?per_page=100&page=" in path:
            return self._assets() if path.endswith("page=1") else []
        raise AssertionError(f"Unexpected GitHub API path: {path}")

    def download_asset(
        self,
        path: str,
        *,
        destination: Path,
        max_bytes: int,
    ) -> dict[str, Any]:
        self.paths.append(path)
        asset_id = int(path.rsplit("/", 1)[1])
        name = next(name for name, known_id in self.asset_ids.items() if known_id == asset_id)
        content = self.downloaded_files[name]
        assert max_bytes >= len(self.files[name])
        destination.write_bytes(content)
        return {
            "sizeBytes": len(content),
            "sha256": sha256(content).hexdigest(),
        }


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


def _expected_state_report(
    tmp_path: Path,
    api: FakeReleaseApi,
    *,
    release_id: int = RELEASE_ID,
) -> Path:
    body_bytes = RELEASE_BODY.encode("utf-8")
    report = {
        "schemaVersion": 1,
        "ok": True,
        "phase": "draft",
        "releaseId": release_id,
        "releaseTag": RELEASE_TAG,
        "releaseTitle": RELEASE_TITLE,
        "targetCommitish": TARGET_COMMIT,
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
    path = tmp_path / f"expected-state-{len(list(tmp_path.glob('expected-state-*.json')))}.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_draft_verification_binds_release_and_asset_ids_to_downloaded_bytes(
    tmp_path: Path,
) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakeReleaseApi(files)
    prior = _expected_state_report(tmp_path, api)

    report = verify_github_release_state(
        api,
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        release_id=RELEASE_ID,
        expected_target_commitish=TARGET_COMMIT,
        phase="draft",
        expected_dir=expected,
        download_dir=tmp_path / "download",
        expected_state_report=prior,
    )

    assert report["releaseId"] == RELEASE_ID
    assert report["draft"] is True
    assert {asset["name"] for asset in report["assets"]} == set(files)
    assert all(isinstance(asset["assetId"], int) for asset in report["assets"])
    assert all(asset["githubDigest"] == f"sha256:{asset['sha256']}" for asset in report["assets"])
    assert report["releaseTitle"] == RELEASE_TITLE
    assert report["targetCommitish"] == TARGET_COMMIT
    assert report["releaseBodySizeBytes"] == len(RELEASE_BODY.encode("utf-8"))
    assert report["releaseBodySha256"] == sha256(RELEASE_BODY.encode("utf-8")).hexdigest()
    assert "body" not in report
    assert not any("/releases/tags/" in path for path in api.paths)
    for name, content in files.items():
        assert (tmp_path / "download" / name).read_bytes() == content


def test_prior_state_requires_exact_same_asset_ids(tmp_path: Path) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakeReleaseApi(files)
    prior_path = _expected_state_report(tmp_path, api)
    first_report = json.loads(prior_path.read_text(encoding="utf-8"))
    first_report["assets"][0]["assetId"] += 999
    prior_path.write_text(json.dumps(first_report), encoding="utf-8")

    with pytest.raises(ValueError, match="identities changed"):
        verify_github_release_state(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            expected_target_commitish=TARGET_COMMIT,
            phase="draft",
            expected_dir=expected,
            download_dir=tmp_path / "second-download",
            expected_state_report=prior_path,
        )


def test_published_verification_accepts_same_prior_draft_identity(tmp_path: Path) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakeReleaseApi(files)
    initial_state = _expected_state_report(tmp_path, api)
    draft_report = verify_github_release_state(
        api,
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        release_id=RELEASE_ID,
        expected_target_commitish=TARGET_COMMIT,
        phase="draft",
        expected_dir=expected,
        download_dir=tmp_path / "draft-download",
        expected_state_report=initial_state,
    )
    prior_path = tmp_path / "draft-state.json"
    prior_path.write_text(json.dumps(draft_report), encoding="utf-8")
    api.draft = False

    published_report = verify_github_release_state(
        api,
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        release_id=RELEASE_ID,
        expected_target_commitish=TARGET_COMMIT,
        phase="published",
        expected_dir=expected,
        download_dir=tmp_path / "published-download",
        expected_state_report=prior_path,
    )

    assert published_report["releaseId"] == RELEASE_ID
    assert published_report["draft"] is False
    assert published_report["publishedAt"] == "2026-07-27T12:00:00Z"
    assert published_report["assets"] == draft_report["assets"]


def test_downloaded_asset_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakeReleaseApi(files)
    api.downloaded_files["latest.json"] = b"tampered"
    prior = _expected_state_report(tmp_path, api)

    with pytest.raises(ValueError, match="Downloaded GitHub release asset bytes"):
        verify_github_release_state(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            expected_target_commitish=TARGET_COMMIT,
            phase="draft",
            expected_dir=expected,
            download_dir=tmp_path / "download",
            expected_state_report=prior,
        )


def test_exact_tag_must_resolve_to_requested_release_id(tmp_path: Path) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakeReleaseApi(files, release_id=RELEASE_ID + 1)
    prior = _expected_state_report(tmp_path, api, release_id=RELEASE_ID)

    with pytest.raises(ValueError, match="different release ID"):
        verify_github_release_state(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            expected_target_commitish=TARGET_COMMIT,
            phase="draft",
            expected_dir=expected,
            download_dir=tmp_path / "download",
            expected_state_report=prior,
        )


def test_published_phase_refuses_a_draft_release(tmp_path: Path) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakeReleaseApi(files, draft=True)
    prior = _expected_state_report(tmp_path, api)

    with pytest.raises(ValueError, match="expected published"):
        verify_github_release_state(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            expected_target_commitish=TARGET_COMMIT,
            phase="published",
            expected_dir=expected,
            download_dir=tmp_path / "download",
            expected_state_report=prior,
        )


def test_release_body_must_match_expected_state_report(tmp_path: Path) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakeReleaseApi(files, body="Edited after sync")
    prior = _expected_state_report(tmp_path, api)

    with pytest.raises(ValueError, match="metadata changed"):
        verify_github_release_state(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            expected_target_commitish=TARGET_COMMIT,
            phase="draft",
            expected_dir=expected,
            download_dir=tmp_path / "download",
            expected_state_report=prior,
        )


def test_expected_state_target_must_equal_event_sha(tmp_path: Path) -> None:
    expected, files = _expected_dir(tmp_path)
    api = FakeReleaseApi(files)
    prior = _expected_state_report(tmp_path, api)
    report = json.loads(prior.read_text(encoding="utf-8"))
    report["targetCommitish"] = "b" * 40
    prior.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="expected event SHA"):
        verify_github_release_state(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            release_id=RELEASE_ID,
            expected_target_commitish=TARGET_COMMIT,
            phase="draft",
            expected_dir=expected,
            download_dir=tmp_path / "download",
            expected_state_report=prior,
        )
