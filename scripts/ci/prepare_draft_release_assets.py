"""Prepare an existing draft release for an exact, restartable asset upload."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

from scripts.ci.github_release_api import GitHubApi
from scripts.ci.github_release_state import (
    discover_release_by_tag,
    list_release_assets,
    release_by_id_path,
    require_release_identity,
)

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RELEASE_TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")


class GitHubDraftApi(Protocol):
    def get_json(self, path: str) -> dict[str, Any]: ...

    def get_json_list(self, path: str) -> list[dict[str, Any]]: ...

    def delete(self, path: str) -> None: ...


def _expected_asset_names(expected_dir: Path) -> set[str]:
    if not expected_dir.is_dir():
        raise FileNotFoundError(f"Expected release asset directory was not found: {expected_dir}")
    entries = list(expected_dir.iterdir())
    if not entries or any(not entry.is_file() for entry in entries):
        raise ValueError("Expected release asset directory must be non-empty and flat.")
    names = {entry.name for entry in entries}
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        raise ValueError("Expected release asset names must be unique ignoring case.")
    return names


def _validated_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for index, asset in enumerate(assets):
        asset_id = asset.get("id")
        name = asset.get("name")
        size_bytes = asset.get("size")
        digest = asset.get("digest")
        if not isinstance(asset_id, int) or asset_id <= 0 or asset_id in seen_ids:
            raise ValueError(f"GitHub release asset {index} has an invalid or duplicate ID.")
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError(f"GitHub release asset {index} has an unsafe name.")
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError(f"GitHub release asset {index} has an invalid size.")
        if digest is not None and not isinstance(digest, str):
            raise ValueError(f"GitHub release asset {index} has an invalid digest.")
        seen_ids.add(asset_id)
        validated.append(
            {
                "id": asset_id,
                "name": name,
                "state": str(asset.get("state") or ""),
                "sizeBytes": size_bytes,
                "digest": digest,
            }
        )
    return validated


def _require_same_draft_release(
    release: dict[str, Any],
    *,
    release_id: int,
    release_tag: str,
) -> None:
    try:
        require_release_identity(
            release,
            release_id=release_id,
            release_tag=release_tag,
            draft=True,
        )
    except ValueError as error:
        if release.get("draft") is False:
            raise ValueError("Refusing to mutate assets on a published GitHub release.") from error
        raise


def prepare_draft_release_assets(
    api: GitHubDraftApi,
    *,
    repository: str,
    release_tag: str,
    expected_dir: Path,
) -> dict[str, Any]:
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Repository must use the canonical owner/name form.")
    if RELEASE_TAG_PATTERN.fullmatch(release_tag) is None:
        raise ValueError("Release tag must use canonical vX.Y.Z form.")
    expected_names = _expected_asset_names(expected_dir)
    release = discover_release_by_tag(
        api,
        repository=repository,
        release_tag=release_tag,
    )
    if release is None:
        return {
            "schemaVersion": 1,
            "ok": True,
            "releaseExists": False,
            "releaseId": None,
            "releaseTag": release_tag,
            "expectedAssetCount": len(expected_names),
            "deletedAssetIds": [],
            "retainedAssetNames": [],
        }

    release_id = release.get("id")
    if not isinstance(release_id, int) or release_id <= 0:
        raise ValueError("GitHub release response has an invalid release ID.")
    _require_same_draft_release(release, release_id=release_id, release_tag=release_tag)
    assets = _validated_assets(list_release_assets(api, repository=repository, release_id=release_id))
    name_counts = Counter(asset["name"] for asset in assets)
    stale_assets = [
        asset
        for asset in assets
        if (asset["name"] not in expected_names or name_counts[asset["name"]] > 1 or asset["state"] != "uploaded")
    ]

    exact_release_path = release_by_id_path(repository, release_id)
    deleted: list[dict[str, Any]] = []
    for stale in sorted(stale_assets, key=lambda item: item["id"]):
        current_release = api.get_json(exact_release_path)
        _require_same_draft_release(
            current_release,
            release_id=release_id,
            release_tag=release_tag,
        )
        current_assets = _validated_assets(list_release_assets(api, repository=repository, release_id=release_id))
        current_asset = next((asset for asset in current_assets if asset["id"] == stale["id"]), None)
        if current_asset is None:
            continue
        if current_asset["name"] != stale["name"]:
            raise ValueError("A draft release asset changed identity before deletion.")
        api.delete(f"/repos/{repository}/releases/assets/{stale['id']}")
        deleted.append(stale)

    final_release = api.get_json(exact_release_path)
    _require_same_draft_release(
        final_release,
        release_id=release_id,
        release_tag=release_tag,
    )
    final_assets = _validated_assets(list_release_assets(api, repository=repository, release_id=release_id))
    final_names = [asset["name"] for asset in final_assets]
    if any(name not in expected_names for name in final_names):
        raise ValueError("Draft release still contains an unexpected asset after cleanup.")
    if any(count > 1 for count in Counter(final_names).values()):
        raise ValueError("Draft release still contains duplicate asset names after cleanup.")
    if any(asset["state"] != "uploaded" for asset in final_assets):
        raise ValueError("Draft release still contains an incomplete asset after cleanup.")

    deleted_ids = {asset["id"] for asset in deleted}
    if any(asset["id"] in deleted_ids for asset in final_assets):
        raise ValueError("A deleted draft asset is still present after cleanup.")
    return {
        "schemaVersion": 1,
        "ok": True,
        "releaseExists": True,
        "releaseId": release_id,
        "releaseTag": release_tag,
        "expectedAssetCount": len(expected_names),
        "deletedAssetIds": sorted(deleted_ids),
        "deletedAssetNames": sorted(asset["name"] for asset in deleted),
        "retainedAssetNames": sorted(final_names),
    }


def _append_github_outputs(path: Path, report: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"release-exists={str(report['releaseExists']).lower()}\n")
        output.write(f"release-id={report.get('releaseId') or ''}\n")
        output.write(f"deleted-asset-count={len(report['deletedAssetIds'])}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--expected-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    report = prepare_draft_release_assets(
        GitHubApi(token=token),
        repository=args.repository,
        release_tag=args.release_tag,
        expected_dir=args.expected_dir.resolve(),
    )
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
