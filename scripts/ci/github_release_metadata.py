"""Fail-closed generated-note and GitHub release metadata contracts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RELEASE_TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_RELEASE_BODY_BYTES = 256 * 1024


class GitHubGenerateNotesApi(Protocol):
    def post_json(
        self,
        path: str,
        value: dict[str, Any],
        *,
        expected_status: int = 201,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GeneratedReleaseMetadata:
    release_title: str
    target_commitish: str
    body: str
    body_size_bytes: int
    body_sha256: str

    def evidence(self) -> dict[str, Any]:
        return {
            "releaseTitle": self.release_title,
            "targetCommitish": self.target_commitish,
            "releaseBodySizeBytes": self.body_size_bytes,
            "releaseBodySha256": self.body_sha256,
        }


def expected_release_title(release_tag: str) -> str:
    if RELEASE_TAG_PATTERN.fullmatch(release_tag) is None:
        raise ValueError("Release tag must use canonical vX.Y.Z form.")
    return f"Scriber {release_tag.removeprefix('v')}"


def _body_identity(value: object, *, label: str) -> tuple[str, int, str]:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} is not valid UTF-8 text.") from error
    if len(encoded) > MAX_RELEASE_BODY_BYTES:
        raise ValueError(f"{label} exceeds the {MAX_RELEASE_BODY_BYTES}-byte limit.")
    return value, len(encoded), hashlib.sha256(encoded).hexdigest()


def generate_release_metadata(
    api: GitHubGenerateNotesApi,
    *,
    repository: str,
    release_tag: str,
    release_title: str,
    target_commitish: str,
) -> GeneratedReleaseMetadata:
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Repository must use the canonical owner/name form.")
    expected_title = expected_release_title(release_tag)
    if release_title != expected_title:
        raise ValueError(f"Release title must be exactly {expected_title!r}.")
    if COMMIT_SHA_PATTERN.fullmatch(target_commitish) is None:
        raise ValueError("Release target commit must be a canonical 40-character Git SHA.")

    response = api.post_json(
        f"/repos/{repository}/releases/generate-notes",
        {
            "tag_name": release_tag,
            "target_commitish": target_commitish,
        },
        expected_status=200,
    )
    body, body_size_bytes, body_sha256 = _body_identity(
        response.get("body"),
        label="Generated GitHub release notes body",
    )
    return GeneratedReleaseMetadata(
        release_title=release_title,
        target_commitish=target_commitish,
        body=body,
        body_size_bytes=body_size_bytes,
        body_sha256=body_sha256,
    )


def validated_release_metadata(release: dict[str, Any]) -> dict[str, Any]:
    release_title = release.get("name")
    target_commitish = release.get("target_commitish")
    if not isinstance(release_title, str):
        raise ValueError("GitHub release title is invalid.")
    if not isinstance(target_commitish, str) or COMMIT_SHA_PATTERN.fullmatch(target_commitish) is None:
        raise ValueError("GitHub release target commit is invalid.")
    _body, body_size_bytes, body_sha256 = _body_identity(
        release.get("body"),
        label="GitHub release body",
    )
    return {
        "releaseTitle": release_title,
        "targetCommitish": target_commitish,
        "releaseBodySizeBytes": body_size_bytes,
        "releaseBodySha256": body_sha256,
    }


def validated_reported_release_metadata(
    report: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    release_title = report.get("releaseTitle")
    target_commitish = report.get("targetCommitish")
    body_size_bytes = report.get("releaseBodySizeBytes")
    body_sha256 = report.get("releaseBodySha256")
    if not isinstance(release_title, str) or not release_title:
        raise ValueError(f"{label} has an invalid release title.")
    if not isinstance(target_commitish, str) or COMMIT_SHA_PATTERN.fullmatch(target_commitish) is None:
        raise ValueError(f"{label} has an invalid target commit.")
    if (
        isinstance(body_size_bytes, bool)
        or not isinstance(body_size_bytes, int)
        or body_size_bytes < 0
        or body_size_bytes > MAX_RELEASE_BODY_BYTES
    ):
        raise ValueError(f"{label} has an invalid release body byte length.")
    if not isinstance(body_sha256, str) or SHA256_PATTERN.fullmatch(body_sha256) is None:
        raise ValueError(f"{label} has an invalid release body SHA-256.")
    return {
        "releaseTitle": release_title,
        "targetCommitish": target_commitish,
        "releaseBodySizeBytes": body_size_bytes,
        "releaseBodySha256": body_sha256,
    }


def require_generated_release_metadata(
    release: dict[str, Any],
    *,
    expected: GeneratedReleaseMetadata,
) -> dict[str, Any]:
    actual = validated_release_metadata(release)
    if release.get("body") != expected.body or actual != expected.evidence():
        raise ValueError("GitHub release metadata does not exactly match the freshly generated release notes.")
    return actual


def require_reported_release_metadata(
    release: dict[str, Any],
    *,
    expected_report: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    expected = validated_reported_release_metadata(expected_report, label=label)
    actual = validated_release_metadata(release)
    if actual != expected:
        raise ValueError("GitHub release metadata changed from the expected verified state.")
    return actual
