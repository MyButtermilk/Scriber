"""Validate the live annotated release tag, event commit, and main commit."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from scripts.ci.github_release_api import GitHubApi

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RELEASE_TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MAX_TAG_DEPTH = 16


class GitHubReader(Protocol):
    def get_json(self, path: str) -> dict[str, Any]: ...


def _normalize_sha(value: object, *, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if SHA_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{label} must be a 40-character lowercase Git commit/object SHA.")
    return normalized


def _read_ref_object(payload: dict[str, Any], *, expected_ref: str, label: str) -> tuple[str, str]:
    if payload.get("ref") != expected_ref:
        raise ValueError(f"{label} response did not match {expected_ref}.")
    target = payload.get("object")
    if not isinstance(target, dict):
        raise ValueError(f"{label} response is missing its target object.")
    target_type = str(target.get("type") or "")
    target_sha = _normalize_sha(target.get("sha"), label=f"{label} target")
    return target_type, target_sha


def _peel_annotated_tag(
    api: GitHubReader,
    *,
    repository: str,
    release_tag: str,
    root_tag_sha: str,
) -> tuple[str, int]:
    seen: set[str] = set()
    current_sha = root_tag_sha
    for depth in range(1, MAX_TAG_DEPTH + 1):
        if current_sha in seen:
            raise ValueError("Annotated release tag chain contains a cycle.")
        seen.add(current_sha)
        payload = api.get_json(f"/repos/{repository}/git/tags/{current_sha}")
        payload_sha = _normalize_sha(payload.get("sha"), label=f"annotated tag object at depth {depth}")
        if payload_sha != current_sha:
            raise ValueError("GitHub returned an annotated tag object with the wrong SHA.")
        if depth == 1 and payload.get("tag") != release_tag:
            raise ValueError("The outer annotated tag object has the wrong tag name.")
        target = payload.get("object")
        if not isinstance(target, dict):
            raise ValueError("Annotated tag object is missing its target.")
        target_type = str(target.get("type") or "")
        target_sha = _normalize_sha(target.get("sha"), label=f"annotated tag target at depth {depth}")
        if target_type == "commit":
            return target_sha, depth
        if target_type != "tag":
            raise ValueError(f"Annotated tag targets unsupported Git object type {target_type!r}.")
        current_sha = target_sha
    raise ValueError(f"Annotated release tag chain exceeds the maximum depth of {MAX_TAG_DEPTH}.")


def validate_live_release_ref(
    api: GitHubReader,
    *,
    repository: str,
    release_tag: str,
    event_sha: str,
) -> dict[str, Any]:
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Repository must use the canonical owner/name form.")
    if RELEASE_TAG_PATTERN.fullmatch(release_tag) is None:
        raise ValueError("Release tag must use canonical vX.Y.Z form.")
    expected_commit = _normalize_sha(event_sha, label="event SHA")
    encoded_tag = quote(release_tag, safe="")
    tag_path = f"/repos/{repository}/git/ref/tags/{encoded_tag}"
    main_path = f"/repos/{repository}/git/ref/heads/main"

    tag_ref_before = api.get_json(tag_path)
    tag_type, root_tag_sha = _read_ref_object(
        tag_ref_before,
        expected_ref=f"refs/tags/{release_tag}",
        label="release tag ref",
    )
    if tag_type != "tag":
        raise ValueError("Official releases require an annotated Git tag; lightweight tags are rejected.")
    tag_commit_sha, tag_depth = _peel_annotated_tag(
        api,
        repository=repository,
        release_tag=release_tag,
        root_tag_sha=root_tag_sha,
    )

    main_ref_before = api.get_json(main_path)
    main_type, main_sha = _read_ref_object(
        main_ref_before,
        expected_ref="refs/heads/main",
        label="main branch ref",
    )
    if main_type != "commit":
        raise ValueError("The live main ref does not target a commit.")

    tag_ref_after = api.get_json(tag_path)
    stable_tag_type, stable_root_tag_sha = _read_ref_object(
        tag_ref_after,
        expected_ref=f"refs/tags/{release_tag}",
        label="second release tag ref",
    )
    main_ref_after = api.get_json(main_path)
    stable_main_type, stable_main_sha = _read_ref_object(
        main_ref_after,
        expected_ref="refs/heads/main",
        label="second main branch ref",
    )
    if (stable_tag_type, stable_root_tag_sha) != (tag_type, root_tag_sha):
        raise ValueError("The live release tag changed while it was being validated.")
    if (stable_main_type, stable_main_sha) != (main_type, main_sha):
        raise ValueError("The live main ref changed while it was being validated.")
    if tag_commit_sha != expected_commit:
        raise ValueError(f"The live annotated release tag peels to {tag_commit_sha}, not event SHA {expected_commit}.")
    if main_sha != expected_commit:
        raise ValueError(f"The live main ref is {main_sha}, not event SHA {expected_commit}.")

    return {
        "schemaVersion": 1,
        "ok": True,
        "repository": repository,
        "releaseTag": release_tag,
        "eventSha": expected_commit,
        "tagObjectSha": root_tag_sha,
        "tagCommitSha": tag_commit_sha,
        "mainSha": main_sha,
        "annotatedTagDepth": tag_depth,
        "stableRefSnapshot": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--event-sha", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    report = validate_live_release_ref(
        GitHubApi(token=token),
        repository=args.repository,
        release_tag=args.release_tag,
        event_sha=args.event_sha,
    )
    payload = json.dumps(report, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
