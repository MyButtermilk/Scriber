"""Create or replace one draft release's assets through numeric GitHub IDs.

The mutation flow deliberately never uploads through a tag lookup.  A release is
discovered once through bounded list pagination, bound to its numeric database
ID, and then re-read by that ID immediately before every DELETE or upload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol, cast

from scripts.ci.github_release_api import MAX_ASSET_BYTES, GitHubApi
from scripts.ci.github_release_metadata import (
    GeneratedReleaseMetadata,
    generate_release_metadata,
    require_generated_release_metadata,
)
from scripts.ci.github_release_state import (
    discover_release_by_tag,
    list_release_assets,
    release_by_id_path,
    require_release_identity,
)
from scripts.ci.validate_live_release_ref import validate_live_release_ref

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RELEASE_TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
MAX_ASSET_COUNT = 100
MAX_TOTAL_ASSET_BYTES = 1024 * 1024 * 1024


class GitHubDraftPublisherApi(Protocol):
    """The narrow API surface required for ID-bound draft publication."""

    def get_json(self, path: str) -> dict[str, Any]: ...

    def get_json_list(self, path: str) -> list[dict[str, Any]]: ...

    def post_json(
        self,
        path: str,
        value: dict[str, Any],
        *,
        expected_status: int = 201,
    ) -> dict[str, Any]: ...

    def delete(self, path: str) -> None: ...

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
    ) -> dict[str, Any]: ...

    def download_asset(
        self,
        path: str,
        *,
        destination: Path,
        max_bytes: int = MAX_ASSET_BYTES,
    ) -> dict[str, Any]: ...


def _positive_id(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive GitHub database ID.")
    return value


def _safe_asset_name(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} has an unsafe name.")
    return value


def _sha256_file(path: Path, *, expected_size: int | None = None) -> tuple[int, str]:
    before = path.stat()
    if before.st_size <= 0:
        raise ValueError(f"Release asset {path.name!r} must not be empty.")
    if before.st_size > MAX_ASSET_BYTES:
        raise ValueError(f"Release asset {path.name!r} exceeds the per-asset byte limit.")
    if expected_size is not None and before.st_size != expected_size:
        raise ValueError(f"Release asset {path.name!r} changed size before upload.")

    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size_bytes += len(chunk)
            if size_bytes > MAX_ASSET_BYTES:
                raise ValueError(f"Release asset {path.name!r} exceeds the per-asset byte limit.")
            digest.update(chunk)

    after = path.stat()
    if size_bytes != before.st_size or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        raise ValueError(f"Release asset {path.name!r} changed while it was being read.")
    return size_bytes, digest.hexdigest()


def _expected_assets(expected_dir: Path) -> dict[str, dict[str, Any]]:
    if not expected_dir.is_dir():
        raise FileNotFoundError(f"Expected release asset directory was not found: {expected_dir}")
    entries = sorted(expected_dir.iterdir(), key=lambda path: path.name.casefold())
    if not entries or any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise ValueError("Expected release asset directory must be non-empty, flat, and symlink-free.")
    if len(entries) > MAX_ASSET_COUNT:
        raise ValueError(f"Expected release asset count exceeds the limit of {MAX_ASSET_COUNT}.")

    folded_names: set[str] = set()
    total_bytes = 0
    expected: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name = _safe_asset_name(entry.name, label="Expected release asset")
        folded = name.casefold()
        if folded in folded_names:
            raise ValueError("Expected release asset names must be unique ignoring case.")
        folded_names.add(folded)
        size_bytes, digest = _sha256_file(entry)
        total_bytes += size_bytes
        if total_bytes > MAX_TOTAL_ASSET_BYTES:
            raise ValueError("Expected release assets exceed the total byte limit.")
        expected[name] = {
            "source": entry,
            "sizeBytes": size_bytes,
            "sha256": digest,
        }
    return expected


def _asset_metadata(asset: dict[str, Any], *, complete: bool) -> dict[str, Any]:
    asset_id = _positive_id(asset.get("id"), label="Release asset ID")
    name = _safe_asset_name(asset.get("name"), label=f"Release asset {asset_id}")
    size_bytes = asset.get("size")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError(f"Release asset {asset_id} has an invalid byte size.")
    state = asset.get("state")
    if not isinstance(state, str) or not state:
        raise ValueError(f"Release asset {asset_id} has an invalid upload state.")

    github_digest = asset.get("digest")
    digest_match = SHA256_DIGEST_PATTERN.fullmatch(github_digest) if isinstance(github_digest, str) else None
    if github_digest is not None and digest_match is None:
        raise ValueError(f"Release asset {asset_id} has an invalid GitHub digest.")
    if complete and (state != "uploaded" or size_bytes <= 0 or digest_match is None):
        raise ValueError(f"Release asset {asset_id} is not a complete, digest-bound upload.")
    return {
        "assetId": asset_id,
        "name": name,
        "state": state,
        "sizeBytes": size_bytes,
        "githubDigest": github_digest.lower() if isinstance(github_digest, str) else None,
    }


def _listed_assets(
    api: GitHubDraftPublisherApi,
    *,
    repository: str,
    release_id: int,
    complete: bool,
) -> list[dict[str, Any]]:
    raw_assets = list_release_assets(api, repository=repository, release_id=release_id)
    if len(raw_assets) > MAX_ASSET_COUNT:
        raise ValueError(f"GitHub release asset count exceeds the limit of {MAX_ASSET_COUNT}.")
    assets = [_asset_metadata(asset, complete=complete) for asset in raw_assets]
    ids = [asset["assetId"] for asset in assets]
    if len(ids) != len(set(ids)):
        raise ValueError("GitHub returned duplicate release asset IDs.")
    return assets


def _require_bound_draft(
    api: GitHubDraftPublisherApi,
    *,
    repository: str,
    release_id: int,
    release_tag: str,
    expected_metadata: GeneratedReleaseMetadata,
) -> dict[str, Any]:
    release = api.get_json(release_by_id_path(repository, release_id))
    try:
        require_release_identity(
            release,
            release_id=release_id,
            release_tag=release_tag,
            draft=True,
        )
    except ValueError as error:
        if release.get("draft") is False:
            raise ValueError("Refusing to mutate a published GitHub release.") from error
        raise
    if release.get("prerelease") is not False:
        raise ValueError("The bound GitHub release must be a non-prerelease draft.")
    if release.get("published_at") is not None:
        raise ValueError("The bound GitHub draft unexpectedly has a publication timestamp.")
    require_generated_release_metadata(release, expected=expected_metadata)
    upload_url = release.get("upload_url")
    if not isinstance(upload_url, str) or not upload_url:
        raise ValueError("The bound GitHub draft has no asset upload URL.")
    return release


def _require_tag_binding(
    api: GitHubDraftPublisherApi,
    *,
    repository: str,
    release_id: int,
    release_tag: str,
    expected_metadata: GeneratedReleaseMetadata,
) -> None:
    discovered = discover_release_by_tag(
        api,
        repository=repository,
        release_tag=release_tag,
    )
    if discovered is None:
        raise ValueError("The bound GitHub release tag disappeared.")
    if _positive_id(discovered.get("id"), label="Discovered release ID") != release_id:
        raise ValueError("The GitHub release tag was rebound to a different release ID.")
    _require_bound_draft(
        api,
        repository=repository,
        release_id=release_id,
        release_tag=release_tag,
        expected_metadata=expected_metadata,
    )


def _create_or_bind_draft(
    api: GitHubDraftPublisherApi,
    *,
    repository: str,
    release_tag: str,
    release_title: str,
    target_commitish: str,
    expected_metadata: GeneratedReleaseMetadata,
) -> tuple[int, bool]:
    release = discover_release_by_tag(
        api,
        repository=repository,
        release_tag=release_tag,
    )
    created = False
    if release is None:
        # Repeat the bounded read directly before release creation.  A racing
        # creator is allowed to make the POST fail; this helper never guesses
        # which release should be adopted after an ambiguous mutation.
        if (
            discover_release_by_tag(
                api,
                repository=repository,
                release_tag=release_tag,
            )
            is not None
        ):
            raise ValueError("The GitHub release appeared during draft creation; refusing an ambiguous mutation.")
        validate_live_release_ref(
            api,
            repository=repository,
            release_tag=release_tag,
            event_sha=target_commitish,
        )
        release = api.post_json(
            f"/repos/{repository}/releases",
            {
                "tag_name": release_tag,
                "target_commitish": target_commitish,
                "name": release_title,
                "draft": True,
                "prerelease": False,
                "generate_release_notes": True,
            },
        )
        created = True

    release_id = _positive_id(release.get("id"), label="Release ID")
    try:
        require_release_identity(
            release,
            release_id=release_id,
            release_tag=release_tag,
            draft=True,
        )
    except ValueError as error:
        if release.get("draft") is False:
            raise ValueError("Refusing to mutate a published GitHub release.") from error
        raise
    _require_tag_binding(
        api,
        repository=repository,
        release_id=release_id,
        release_tag=release_tag,
        expected_metadata=expected_metadata,
    )
    return release_id, created


def _assert_remote_asset_set(
    assets: list[dict[str, Any]],
    *,
    expected: dict[str, dict[str, Any]],
    uploaded: dict[str, dict[str, Any]],
) -> None:
    names = [asset["name"] for asset in assets]
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("GitHub returned duplicate release asset names.")
    if set(names) != set(uploaded):
        raise ValueError("GitHub release assets changed outside the bound upload sequence.")
    by_name = {asset["name"]: asset for asset in assets}
    for name, upload in uploaded.items():
        live = by_name[name]
        local = expected[name]
        if (
            live["assetId"] != upload["assetId"]
            or live["state"] != "uploaded"
            or live["sizeBytes"] != local["sizeBytes"]
            or live["githubDigest"] != f"sha256:{local['sha256']}"
        ):
            raise ValueError(f"GitHub release asset identity changed for {name!r}.")


def sync_draft_release_assets(
    api: GitHubDraftPublisherApi,
    *,
    repository: str,
    release_tag: str,
    release_title: str,
    target_commitish: str,
    expected_dir: Path,
    verification_dir: Path,
) -> dict[str, Any]:
    """Replace all assets on one exact draft and verify the uploaded bytes."""

    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Repository must use the canonical owner/name form.")
    if RELEASE_TAG_PATTERN.fullmatch(release_tag) is None:
        raise ValueError("Release tag must use canonical vX.Y.Z form.")
    expected_title = f"Scriber {release_tag.removeprefix('v')}"
    if release_title != expected_title:
        raise ValueError(f"Release title must be exactly {expected_title!r}.")
    target_commitish = target_commitish.strip().lower()
    if COMMIT_SHA_PATTERN.fullmatch(target_commitish) is None:
        raise ValueError("Release target commit must be a canonical 40-character Git SHA.")

    expected = _expected_assets(expected_dir)
    verification_dir.mkdir(parents=True, exist_ok=True)
    if verification_dir.is_symlink() or not verification_dir.is_dir():
        raise ValueError("Release verification directory must be a real directory.")

    validate_live_release_ref(
        api,
        repository=repository,
        release_tag=release_tag,
        event_sha=target_commitish,
    )
    expected_metadata = generate_release_metadata(
        api,
        repository=repository,
        release_tag=release_tag,
        release_title=release_title,
        target_commitish=target_commitish,
    )
    release_id, created = _create_or_bind_draft(
        api,
        repository=repository,
        release_tag=release_tag,
        release_title=release_title,
        target_commitish=target_commitish,
        expected_metadata=expected_metadata,
    )

    deleted_ids: list[int] = []
    initial_assets = _listed_assets(
        api,
        repository=repository,
        release_id=release_id,
        complete=False,
    )
    for stale in sorted(initial_assets, key=lambda asset: asset["assetId"]):
        current_assets = _listed_assets(
            api,
            repository=repository,
            release_id=release_id,
            complete=False,
        )
        current = next(
            (asset for asset in current_assets if asset["assetId"] == stale["assetId"]),
            None,
        )
        if current != stale:
            raise ValueError("A GitHub release asset changed identity before deletion.")

        # This exact-ID read is intentionally the final API call before DELETE.
        _require_bound_draft(
            api,
            repository=repository,
            release_id=release_id,
            release_tag=release_tag,
            expected_metadata=expected_metadata,
        )
        api.delete(f"/repos/{repository}/releases/assets/{stale['assetId']}")
        deleted_ids.append(stale["assetId"])

        _require_bound_draft(
            api,
            repository=repository,
            release_id=release_id,
            release_tag=release_tag,
            expected_metadata=expected_metadata,
        )
        remaining_ids = {
            asset["assetId"]
            for asset in _listed_assets(
                api,
                repository=repository,
                release_id=release_id,
                complete=False,
            )
        }
        if stale["assetId"] in remaining_ids:
            raise ValueError("GitHub still returns an asset after its exact-ID deletion.")

    _require_tag_binding(
        api,
        repository=repository,
        release_id=release_id,
        release_tag=release_tag,
        expected_metadata=expected_metadata,
    )
    if _listed_assets(api, repository=repository, release_id=release_id, complete=False):
        raise ValueError("The bound GitHub draft was not empty after asset cleanup.")

    uploaded: dict[str, dict[str, Any]] = {}
    for name, local in expected.items():
        current_assets = _listed_assets(
            api,
            repository=repository,
            release_id=release_id,
            complete=True,
        )
        _assert_remote_asset_set(current_assets, expected=expected, uploaded=uploaded)
        size_bytes, digest = _sha256_file(local["source"], expected_size=local["sizeBytes"])
        if size_bytes != local["sizeBytes"] or digest != local["sha256"]:
            raise ValueError(f"Release asset {name!r} changed before upload.")

        # This exact-ID read is intentionally the final API call before upload.
        bound_release = _require_bound_draft(
            api,
            repository=repository,
            release_id=release_id,
            release_tag=release_tag,
            expected_metadata=expected_metadata,
        )
        uploaded_response = api.upload_asset(
            bound_release["upload_url"],
            repository=repository,
            release_id=release_id,
            asset_name=name,
            source=local["source"],
            expected_size=local["sizeBytes"],
            expected_sha256=local["sha256"],
        )
        uploaded_asset = _asset_metadata(uploaded_response, complete=True)
        if (
            uploaded_asset["name"] != name
            or uploaded_asset["sizeBytes"] != local["sizeBytes"]
            or uploaded_asset["githubDigest"] != f"sha256:{local['sha256']}"
        ):
            raise ValueError(f"GitHub returned the wrong uploaded asset identity for {name!r}.")
        if uploaded_asset["assetId"] in {asset["assetId"] for asset in uploaded.values()}:
            raise ValueError("GitHub reused a release asset ID during upload.")
        uploaded[name] = uploaded_asset

        _require_bound_draft(
            api,
            repository=repository,
            release_id=release_id,
            release_tag=release_tag,
            expected_metadata=expected_metadata,
        )
        _assert_remote_asset_set(
            _listed_assets(
                api,
                repository=repository,
                release_id=release_id,
                complete=True,
            ),
            expected=expected,
            uploaded=uploaded,
        )

    _require_tag_binding(
        api,
        repository=repository,
        release_id=release_id,
        release_tag=release_tag,
        expected_metadata=expected_metadata,
    )
    final_assets = _listed_assets(
        api,
        repository=repository,
        release_id=release_id,
        complete=True,
    )
    _assert_remote_asset_set(final_assets, expected=expected, uploaded=uploaded)

    for name, local in expected.items():
        remote = uploaded[name]
        destination = verification_dir / name
        downloaded = api.download_asset(
            f"/repos/{repository}/releases/assets/{remote['assetId']}",
            destination=destination,
            max_bytes=local["sizeBytes"],
        )
        if downloaded.get("sizeBytes") != local["sizeBytes"] or downloaded.get("sha256") != local["sha256"]:
            raise ValueError(f"Downloaded GitHub release asset bytes do not match for {name!r}.")
        downloaded_size, downloaded_sha256 = _sha256_file(
            destination,
            expected_size=local["sizeBytes"],
        )
        if downloaded_size != local["sizeBytes"] or downloaded_sha256 != local["sha256"]:
            raise ValueError(f"Stored GitHub release asset bytes do not match for {name!r}.")

    verification_entries = list(verification_dir.iterdir())
    if any(entry.is_symlink() or not entry.is_file() for entry in verification_entries) or {
        entry.name for entry in verification_entries
    } != set(expected):
        raise ValueError("Release verification directory does not contain the exact downloaded asset set.")

    # Re-read both the tag binding and complete numeric asset set after every
    # download so the report describes one stable, still-draft release.
    _require_tag_binding(
        api,
        repository=repository,
        release_id=release_id,
        release_tag=release_tag,
        expected_metadata=expected_metadata,
    )
    stable_assets = _listed_assets(
        api,
        repository=repository,
        release_id=release_id,
        complete=True,
    )
    _assert_remote_asset_set(stable_assets, expected=expected, uploaded=uploaded)

    return {
        "schemaVersion": 1,
        "ok": True,
        "releaseId": release_id,
        "releaseTag": release_tag,
        "phase": "draft",
        "draft": True,
        "publishedAt": None,
        "created": created,
        **expected_metadata.evidence(),
        "deletedAssetIds": deleted_ids,
        "assetCount": len(uploaded),
        "assets": [
            {
                "assetId": uploaded[name]["assetId"],
                "name": name,
                "sizeBytes": expected[name]["sizeBytes"],
                "sha256": expected[name]["sha256"],
                "githubDigest": uploaded[name]["githubDigest"],
            }
            for name in expected
        ],
    }


def _append_github_outputs(path: Path, report: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"release-id={report['releaseId']}\n")
        output.write(f"release-created={str(report['created']).lower()}\n")
        output.write(f"asset-count={report['assetCount']}\n")


def _require_client_capabilities(api: object) -> GitHubDraftPublisherApi:
    required = {
        "get_json",
        "get_json_list",
        "post_json",
        "delete",
        "upload_asset",
        "download_asset",
    }
    missing = sorted(name for name in required if not callable(getattr(api, name, None)))
    if missing:
        raise RuntimeError(f"GitHub API client is missing required release mutation methods: {', '.join(missing)}")
    return cast(GitHubDraftPublisherApi, api)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--release-title", required=True)
    parser.add_argument("--target-commitish", required=True)
    parser.add_argument("--expected-dir", type=Path, required=True)
    parser.add_argument("--verification-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    api = _require_client_capabilities(GitHubApi(token=token))

    def run(verification_dir: Path) -> dict[str, Any]:
        return sync_draft_release_assets(
            api,
            repository=args.repository,
            release_tag=args.release_tag,
            release_title=args.release_title,
            target_commitish=args.target_commitish,
            expected_dir=args.expected_dir.resolve(),
            verification_dir=verification_dir,
        )

    if args.verification_dir is not None:
        report = run(args.verification_dir.resolve())
    else:
        with TemporaryDirectory(prefix="scriber-release-upload-") as temporary:
            report = run(Path(temporary))

    payload = json.dumps(report, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.github_output:
        _append_github_outputs(args.github_output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
