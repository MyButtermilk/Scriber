"""Atomically bind verification, publication, and fail-safe rollback by release ID."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Protocol

from scripts.ci.github_release_api import GitHubApi, GitHubApiError
from scripts.ci.github_release_metadata import (
    require_reported_release_metadata,
    validated_reported_release_metadata,
)
from scripts.ci.github_release_state import (
    list_release_assets,
    release_by_id_path,
    require_release_identity,
)
from scripts.ci.validate_live_release_ref import validate_live_release_ref
from scripts.ci.verify_github_release_state import (
    require_release_phase,
    validated_live_asset_identities,
    verify_github_release_state,
)


class GitHubPublishApi(Protocol):
    def get_json(self, path: str) -> dict[str, Any]: ...

    def get_json_list(self, path: str) -> list[dict[str, Any]]: ...

    def patch_json(self, path: str, value: dict[str, Any]) -> dict[str, Any]: ...

    def delete(self, path: str) -> None: ...

    def download_asset(
        self,
        path: str,
        *,
        destination: Path,
        max_bytes: int,
    ) -> dict[str, Any]: ...


class PublicationTransactionError(RuntimeError):
    def __init__(self, message: str, *, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


def _report_asset_identities(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assets = report.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Verified draft report is missing its asset identities.")
    identities: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise ValueError("Verified draft report has an invalid asset identity.")
        identity = {
            "assetId": asset.get("assetId"),
            "name": asset["name"],
            "sizeBytes": asset.get("sizeBytes"),
            "sha256": asset.get("sha256"),
            "githubDigest": asset.get("githubDigest"),
        }
        if asset["name"] in identities:
            raise ValueError("Verified draft report has duplicate asset names.")
        identities[asset["name"]] = identity
    return identities


def _require_exact_draft(
    api: GitHubPublishApi,
    *,
    repository: str,
    release_id: int,
    release_tag: str,
    expected_assets: dict[str, dict[str, Any]],
    expected_state: dict[str, Any],
) -> None:
    release = api.get_json(release_by_id_path(repository, release_id))
    require_release_identity(
        release,
        release_id=release_id,
        release_tag=release_tag,
        draft=True,
    )
    require_release_phase(release, phase="draft")
    require_reported_release_metadata(
        release,
        expected_report=expected_state,
        label="Verified draft state",
    )
    assets = validated_live_asset_identities(list_release_assets(api, repository=repository, release_id=release_id))
    if assets != expected_assets:
        raise ValueError("GitHub release asset identities changed immediately before publication.")


def _rollback_publication(
    api: GitHubPublishApi,
    *,
    repository: str,
    release_id: int,
    release_tag: str,
) -> dict[str, Any]:
    exact_path = release_by_id_path(repository, release_id)
    rollback_error: Exception | None = None
    try:
        response = api.patch_json(exact_path, {"draft": True})
        require_release_identity(
            response,
            release_id=release_id,
            release_tag=release_tag,
            draft=True,
        )
        verified = api.get_json(exact_path)
        require_release_identity(
            verified,
            release_id=release_id,
            release_tag=release_tag,
            draft=True,
        )
        return {
            "action": "restored-draft",
            "verified": True,
            "releaseId": release_id,
        }
    except Exception as error:
        rollback_error = error

    delete_error: Exception | None = None
    try:
        api.delete(exact_path)
    except GitHubApiError as error:
        if error.status_code != 404:
            delete_error = error
    except Exception as error:
        delete_error = error

    missing_verified = False
    verification_error: Exception | None = None
    try:
        api.get_json(exact_path)
    except GitHubApiError as error:
        if error.status_code == 404:
            missing_verified = True
        else:
            verification_error = error
    except Exception as error:
        verification_error = error
    else:
        verification_error = RuntimeError("The exact release still exists after fallback deletion.")

    if missing_verified:
        return {
            "action": "deleted-release",
            "verified": True,
            "releaseId": release_id,
        }

    details = {
        "action": "rollback-failed",
        "verified": False,
        "releaseId": release_id,
        "draftRollbackError": type(rollback_error).__name__,
        "deleteError": type(delete_error).__name__ if delete_error else None,
        "deleteVerificationError": (type(verification_error).__name__ if verification_error else None),
    }
    raise PublicationTransactionError(
        "Publication failed and the exact release could not be returned to a verified safe state.",
        report={
            "schemaVersion": 1,
            "ok": False,
            "releaseId": release_id,
            "releaseTag": release_tag,
            "rollback": details,
        },
    )


def publish_verified_github_release(
    api: GitHubPublishApi,
    *,
    repository: str,
    release_tag: str,
    release_id: int,
    event_sha: str,
    expected_dir: Path,
    expected_state_report: Path,
    pre_publish_download_dir: Path,
    post_publish_download_dir: Path,
) -> dict[str, Any]:
    draft_report = verify_github_release_state(
        api,
        repository=repository,
        release_tag=release_tag,
        release_id=release_id,
        expected_target_commitish=event_sha,
        phase="draft",
        expected_dir=expected_dir,
        download_dir=pre_publish_download_dir,
        expected_state_report=expected_state_report,
    )
    expected_assets = _report_asset_identities(draft_report)
    expected_metadata = validated_reported_release_metadata(
        draft_report,
        label="Verified draft state",
    )
    live_ref_report = validate_live_release_ref(
        api,
        repository=repository,
        release_tag=release_tag,
        event_sha=event_sha,
    )
    _require_exact_draft(
        api,
        repository=repository,
        release_id=release_id,
        release_tag=release_tag,
        expected_assets=expected_assets,
        expected_state=draft_report,
    )

    exact_path = release_by_id_path(repository, release_id)
    publication_attempted = False
    try:
        publication_attempted = True
        published = api.patch_json(
            exact_path,
            {
                "draft": False,
                "make_latest": "true",
            },
        )
        require_release_identity(
            published,
            release_id=release_id,
            release_tag=release_tag,
            draft=False,
        )
        require_release_phase(published, phase="published")
        published_metadata = require_reported_release_metadata(
            published,
            expected_report=draft_report,
            label="Verified draft state",
        )
        if published_metadata != expected_metadata:
            raise ValueError("Published release metadata differs from the final draft identity.")
        published_report = verify_github_release_state(
            api,
            repository=repository,
            release_tag=release_tag,
            release_id=release_id,
            expected_target_commitish=event_sha,
            phase="published",
            expected_dir=expected_dir,
            download_dir=post_publish_download_dir,
            expected_state_report=expected_state_report,
        )
        if _report_asset_identities(published_report) != expected_assets:
            raise ValueError("Published release assets differ from the final draft identity.")
        if (
            validated_reported_release_metadata(
                published_report,
                label="Published release verification",
            )
            != expected_metadata
        ):
            raise ValueError("Published release metadata differs from the final draft identity.")
        post_publish_live_ref_report = validate_live_release_ref(
            api,
            repository=repository,
            release_tag=release_tag,
            event_sha=event_sha,
        )
    except Exception as error:
        if not publication_attempted:
            raise
        try:
            rollback = _rollback_publication(
                api,
                repository=repository,
                release_id=release_id,
                release_tag=release_tag,
            )
        except PublicationTransactionError as rollback_failure:
            report = dict(rollback_failure.report)
            report["publicationError"] = type(error).__name__
            raise PublicationTransactionError(
                str(rollback_failure),
                report=report,
            ) from error
        report = {
            "schemaVersion": 1,
            "ok": False,
            "releaseId": release_id,
            "releaseTag": release_tag,
            "publicationError": type(error).__name__,
            "rollback": rollback,
        }
        raise PublicationTransactionError(
            "Publication postconditions failed; the exact release was returned to a verified safe state.",
            report=report,
        ) from error

    return {
        "schemaVersion": 1,
        "ok": True,
        "releaseId": release_id,
        "releaseTag": release_tag,
        "prePublishLiveRef": live_ref_report,
        "draftVerification": draft_report,
        "publishedVerification": published_report,
        "postPublishLiveRef": post_publish_live_ref_report,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--release-id", required=True, type=int)
    parser.add_argument("--event-sha", required=True)
    parser.add_argument("--expected-dir", required=True, type=Path)
    parser.add_argument("--expected-state-report", required=True, type=Path)
    parser.add_argument("--pre-publish-download-dir", required=True, type=Path)
    parser.add_argument("--post-publish-download-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    try:
        report = publish_verified_github_release(
            GitHubApi(token=token),
            repository=args.repository,
            release_tag=args.release_tag,
            release_id=args.release_id,
            event_sha=args.event_sha,
            expected_dir=args.expected_dir.resolve(),
            expected_state_report=args.expected_state_report.resolve(),
            pre_publish_download_dir=args.pre_publish_download_dir.resolve(),
            post_publish_download_dir=args.post_publish_download_dir.resolve(),
        )
    except PublicationTransactionError as error:
        _write_report(args.output, error.report)
        print(str(error), file=sys.stderr)
        return 1
    _write_report(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
