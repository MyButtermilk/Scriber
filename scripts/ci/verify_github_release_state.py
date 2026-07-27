"""Verify one exact GitHub release and its immutable asset identity set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Protocol

from scripts.ci.github_release_api import MAX_ASSET_BYTES, GitHubApi
from scripts.ci.github_release_metadata import (
    expected_release_title,
    require_reported_release_metadata,
    validated_reported_release_metadata,
)
from scripts.ci.github_release_state import (
    discover_release_by_tag,
    list_release_assets,
    release_by_id_path,
    require_release_identity,
)

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RELEASE_TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_ASSET_COUNT = 100
MAX_TOTAL_ASSET_BYTES = 2 * 1024 * 1024 * 1024
Phase = Literal["draft", "published"]


class GitHubReleaseApi(Protocol):
    def get_json(self, path: str) -> dict[str, Any]: ...

    def get_json_list(self, path: str) -> list[dict[str, Any]]: ...

    def download_asset(
        self,
        path: str,
        *,
        destination: Path,
        max_bytes: int = MAX_ASSET_BYTES,
    ) -> dict[str, Any]: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_asset_name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{label} has an unsafe asset name.")
    return value


def _expected_assets(expected_dir: Path) -> dict[str, dict[str, Any]]:
    if not expected_dir.is_dir():
        raise FileNotFoundError(f"Expected release asset directory was not found: {expected_dir}")
    entries = sorted(expected_dir.iterdir(), key=lambda item: item.name)
    if not entries or any(not entry.is_file() or entry.is_symlink() for entry in entries):
        raise ValueError("Expected release asset directory must be non-empty, flat, and symlink-free.")
    if len(entries) > MAX_ASSET_COUNT:
        raise ValueError(f"Expected release asset count exceeds the limit of {MAX_ASSET_COUNT}.")
    folded_names = [entry.name.casefold() for entry in entries]
    if len(folded_names) != len(set(folded_names)):
        raise ValueError("Expected release asset names must be unique ignoring case.")

    assets: dict[str, dict[str, Any]] = {}
    total_size = 0
    for entry in entries:
        name = _safe_asset_name(entry.name, label="Expected file")
        size_bytes = entry.stat().st_size
        if size_bytes > MAX_ASSET_BYTES:
            raise ValueError(f"Expected release asset {name!r} exceeds the per-asset limit.")
        total_size += size_bytes
        if total_size > MAX_TOTAL_ASSET_BYTES:
            raise ValueError("Expected release assets exceed the total byte limit.")
        assets[name] = {
            "name": name,
            "sizeBytes": size_bytes,
            "sha256": _sha256_file(entry),
        }
    return assets


def validated_live_asset_identities(
    assets: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if len(assets) > MAX_ASSET_COUNT:
        raise ValueError(f"GitHub release asset count exceeds the limit of {MAX_ASSET_COUNT}.")
    validated: dict[str, dict[str, Any]] = {}
    seen_ids: set[int] = set()
    folded_names: set[str] = set()
    total_size = 0
    for index, asset in enumerate(assets):
        asset_id = asset.get("id")
        name = _safe_asset_name(asset.get("name"), label=f"GitHub release asset {index}")
        size_bytes = asset.get("size")
        digest = asset.get("digest")
        if not isinstance(asset_id, int) or asset_id <= 0 or asset_id in seen_ids:
            raise ValueError(f"GitHub release asset {index} has an invalid or duplicate ID.")
        if name.casefold() in folded_names:
            raise ValueError("GitHub release asset names must be unique ignoring case.")
        if asset.get("state") != "uploaded":
            raise ValueError(f"GitHub release asset {name!r} is not completely uploaded.")
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError(f"GitHub release asset {name!r} has an invalid size.")
        if size_bytes > MAX_ASSET_BYTES:
            raise ValueError(f"GitHub release asset {name!r} exceeds the per-asset limit.")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or SHA256_PATTERN.fullmatch(digest.removeprefix("sha256:")) is None
        ):
            raise ValueError(f"GitHub release asset {name!r} has no valid SHA-256 digest.")
        total_size += size_bytes
        if total_size > MAX_TOTAL_ASSET_BYTES:
            raise ValueError("GitHub release assets exceed the total byte limit.")
        seen_ids.add(asset_id)
        folded_names.add(name.casefold())
        validated[name] = {
            "assetId": asset_id,
            "name": name,
            "sizeBytes": size_bytes,
            "sha256": digest.removeprefix("sha256:"),
            "githubDigest": digest,
        }
    return validated


def require_release_phase(release: dict[str, Any], *, phase: Phase) -> None:
    published_at = release.get("published_at")
    if release.get("prerelease") is not False:
        raise ValueError("GitHub release must not be a prerelease.")
    if phase == "draft":
        if release.get("draft") is not True:
            raise ValueError("GitHub release is not the expected draft.")
        if published_at is not None:
            raise ValueError("Draft GitHub release unexpectedly has a publication timestamp.")
    else:
        if release.get("draft") is not False:
            raise ValueError("GitHub release is not published.")
        if not isinstance(published_at, str) or not published_at.strip():
            raise ValueError("Published GitHub release has no publication timestamp.")


def _load_prior_state(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Prior release state report is unreadable: {path}") from error
    if not isinstance(report, dict) or report.get("schemaVersion") != 1 or report.get("ok") is not True:
        raise ValueError("Prior release state report is not a successful schema-v1 report.")
    return report


def _asset_identity_map(report: dict[str, Any], *, label: str) -> dict[str, dict[str, Any]]:
    assets = report.get("assets")
    if not isinstance(assets, list):
        raise ValueError(f"{label} is missing its asset identity list.")
    identities: dict[str, dict[str, Any]] = {}
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise ValueError(f"{label} asset {index} is not an object.")
        name = _safe_asset_name(asset.get("name"), label=f"{label} asset {index}")
        identity = {
            "assetId": asset.get("assetId"),
            "name": name,
            "sizeBytes": asset.get("sizeBytes"),
            "sha256": asset.get("sha256"),
            "githubDigest": asset.get("githubDigest"),
        }
        if name in identities:
            raise ValueError(f"{label} contains duplicate asset names.")
        identities[name] = identity
    return identities


def verify_github_release_state(
    api: GitHubReleaseApi,
    *,
    repository: str,
    release_tag: str,
    release_id: int,
    expected_target_commitish: str,
    phase: Phase,
    expected_dir: Path,
    download_dir: Path,
    expected_state_report: Path,
) -> dict[str, Any]:
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Repository must use the canonical owner/name form.")
    if RELEASE_TAG_PATTERN.fullmatch(release_tag) is None:
        raise ValueError("Release tag must use canonical vX.Y.Z form.")
    if not isinstance(release_id, int) or release_id <= 0:
        raise ValueError("Release ID must be a positive GitHub database ID.")
    if phase not in {"draft", "published"}:
        raise ValueError("Release phase must be draft or published.")

    prior = _load_prior_state(expected_state_report)
    if prior.get("releaseId") != release_id or prior.get("releaseTag") != release_tag:
        raise ValueError("Prior release state report identifies a different release.")
    expected_metadata = validated_reported_release_metadata(
        prior,
        label="Prior release state report",
    )
    if expected_metadata["releaseTitle"] != expected_release_title(release_tag):
        raise ValueError("Prior release state report has the wrong canonical release title.")
    if expected_metadata["targetCommitish"] != expected_target_commitish:
        raise ValueError("Prior release state report is not bound to the expected event SHA.")

    expected = _expected_assets(expected_dir)
    discovered = discover_release_by_tag(
        api,
        repository=repository,
        release_tag=release_tag,
    )
    if discovered is None:
        raise ValueError("The exact GitHub release tag was not found.")
    if discovered.get("id") != release_id:
        raise ValueError("The exact GitHub release tag resolves to a different release ID.")
    require_release_identity(
        discovered,
        release_id=release_id,
        release_tag=release_tag,
        draft=phase == "draft",
    )
    require_release_phase(discovered, phase=phase)
    release_metadata = require_reported_release_metadata(
        discovered,
        expected_report=prior,
        label="Prior release state report",
    )

    live = validated_live_asset_identities(list_release_assets(api, repository=repository, release_id=release_id))
    if set(live) != set(expected):
        raise ValueError("GitHub release asset names do not exactly match the expected asset set.")
    for name, expected_asset in expected.items():
        live_asset = live[name]
        if live_asset["sizeBytes"] != expected_asset["sizeBytes"] or live_asset["sha256"] != expected_asset["sha256"]:
            raise ValueError(f"GitHub release asset metadata does not match local bytes for {name!r}.")

    if _asset_identity_map(prior, label="Prior release state report") != live:
        raise ValueError("GitHub release asset identities changed from the prior verified state.")

    download_dir.mkdir(parents=True, exist_ok=True)
    if any(download_dir.iterdir()):
        raise ValueError("Release asset download directory must be empty.")
    downloaded: dict[str, dict[str, Any]] = {}
    for name in sorted(live):
        asset = live[name]
        result = api.download_asset(
            f"/repos/{repository}/releases/assets/{asset['assetId']}",
            destination=download_dir / name,
            max_bytes=max(1, asset["sizeBytes"]),
        )
        if result.get("sizeBytes") != asset["sizeBytes"] or result.get("sha256") != asset["sha256"]:
            raise ValueError(f"Downloaded GitHub release asset bytes do not match for {name!r}.")
        downloaded[name] = asset

    final_release = api.get_json(release_by_id_path(repository, release_id))
    require_release_identity(
        final_release,
        release_id=release_id,
        release_tag=release_tag,
        draft=phase == "draft",
    )
    require_release_phase(final_release, phase=phase)
    final_metadata = require_reported_release_metadata(
        final_release,
        expected_report=prior,
        label="Prior release state report",
    )
    if final_metadata != release_metadata:
        raise ValueError("GitHub release metadata changed during verification.")
    final_assets = validated_live_asset_identities(
        list_release_assets(api, repository=repository, release_id=release_id)
    )
    if final_assets != downloaded:
        raise ValueError("GitHub release asset identities changed during verification.")

    return {
        "schemaVersion": 1,
        "ok": True,
        "phase": phase,
        "releaseId": release_id,
        "releaseTag": release_tag,
        "draft": final_release["draft"],
        "publishedAt": final_release.get("published_at"),
        **final_metadata,
        "assetCount": len(final_assets),
        "assets": [final_assets[name] for name in sorted(final_assets)],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--release-id", required=True, type=int)
    parser.add_argument("--expected-target-commitish", required=True)
    parser.add_argument("--phase", required=True, choices=("draft", "published"))
    parser.add_argument("--expected-dir", required=True, type=Path)
    parser.add_argument("--download-dir", required=True, type=Path)
    parser.add_argument("--expected-state-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    report = verify_github_release_state(
        GitHubApi(token=token),
        repository=args.repository,
        release_tag=args.release_tag,
        release_id=args.release_id,
        expected_target_commitish=args.expected_target_commitish,
        phase=args.phase,
        expected_dir=args.expected_dir.resolve(),
        download_dir=args.download_dir.resolve(),
        expected_state_report=args.expected_state_report.resolve(),
    )
    payload = json.dumps(report, indent=2, sort_keys=True)
    print(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
