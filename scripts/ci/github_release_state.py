"""Bounded discovery and validation helpers for GitHub release state."""

from __future__ import annotations

from typing import Any, Protocol

PAGE_SIZE = 100
MAX_RELEASE_PAGES = 10
MAX_ASSET_PAGES = 10


class GitHubReleaseReader(Protocol):
    def get_json(self, path: str) -> dict[str, Any]: ...

    def get_json_list(self, path: str) -> list[dict[str, Any]]: ...


def _positive_id(value: object, *, label: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive GitHub database ID.")
    return value


def require_release_identity(
    release: dict[str, Any],
    *,
    release_id: int,
    release_tag: str,
    draft: bool | None,
) -> dict[str, Any]:
    if _positive_id(release.get("id"), label="Release ID") != release_id:
        raise ValueError("GitHub returned a different release ID.")
    if release.get("tag_name") != release_tag:
        raise ValueError("GitHub returned a release with the wrong tag.")
    if not isinstance(release.get("draft"), bool):
        raise ValueError("GitHub release response has an invalid draft state.")
    if draft is not None and release["draft"] is not draft:
        expected = "draft" if draft else "published"
        raise ValueError(f"GitHub release is not the expected {expected} release.")
    return release


def discover_release_by_tag(
    api: GitHubReleaseReader,
    *,
    repository: str,
    release_tag: str,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    terminated = False
    for page in range(1, MAX_RELEASE_PAGES + 1):
        releases = api.get_json_list(f"/repos/{repository}/releases?per_page={PAGE_SIZE}&page={page}")
        for release in releases:
            if release.get("tag_name") == release_tag:
                matches.append(release)
        if len(releases) < PAGE_SIZE:
            terminated = True
            break
    if not terminated:
        raise ValueError(f"GitHub release discovery exceeded {MAX_RELEASE_PAGES * PAGE_SIZE} bounded entries.")
    if len(matches) > 1:
        raise ValueError("GitHub returned multiple releases for the same exact tag.")
    if not matches:
        return None
    release_id = _positive_id(matches[0].get("id"), label="Discovered release ID")
    release = api.get_json(f"/repos/{repository}/releases/{release_id}")
    return require_release_identity(
        release,
        release_id=release_id,
        release_tag=release_tag,
        draft=None,
    )


def list_release_assets(
    api: GitHubReleaseReader,
    *,
    repository: str,
    release_id: int,
) -> list[dict[str, Any]]:
    release_id = _positive_id(release_id, label="Release ID")
    assets: list[dict[str, Any]] = []
    terminated = False
    for page in range(1, MAX_ASSET_PAGES + 1):
        page_assets = api.get_json_list(
            f"/repos/{repository}/releases/{release_id}/assets?per_page={PAGE_SIZE}&page={page}"
        )
        assets.extend(page_assets)
        if len(page_assets) < PAGE_SIZE:
            terminated = True
            break
    if not terminated:
        raise ValueError(f"GitHub asset discovery exceeded {MAX_ASSET_PAGES * PAGE_SIZE} bounded entries.")
    return assets


def release_by_id_path(repository: str, release_id: int) -> str:
    return f"/repos/{repository}/releases/{_positive_id(release_id, label='Release ID')}"
