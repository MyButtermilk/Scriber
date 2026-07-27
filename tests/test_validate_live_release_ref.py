from __future__ import annotations

from typing import Any

import pytest

from scripts.ci.validate_live_release_ref import validate_live_release_ref

REPOSITORY = "example/scriber"
RELEASE_TAG = "v1.2.3"
EVENT_SHA = "a" * 40
ROOT_TAG_SHA = "b" * 40
INNER_TAG_SHA = "c" * 40
OTHER_SHA = "d" * 40


class FakeGitHubReader:
    def __init__(self, responses: dict[str, dict[str, Any] | list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get_json(self, path: str) -> dict[str, Any]:
        self.calls.append(path)
        response = self.responses[path]
        if isinstance(response, list):
            if not response:
                raise AssertionError(f"No fake response remains for {path}")
            return response.pop(0)
        return response


def _tag_ref(target_type: str, target_sha: str) -> dict[str, Any]:
    return {
        "ref": f"refs/tags/{RELEASE_TAG}",
        "object": {"type": target_type, "sha": target_sha},
    }


def _main_ref(commit_sha: str) -> dict[str, Any]:
    return {
        "ref": "refs/heads/main",
        "object": {"type": "commit", "sha": commit_sha},
    }


def _annotated_tag(
    *,
    sha: str,
    target_type: str,
    target_sha: str,
    tag: str = RELEASE_TAG,
) -> dict[str, Any]:
    return {
        "sha": sha,
        "tag": tag,
        "object": {"type": target_type, "sha": target_sha},
    }


def _direct_api(*, tag_commit: str = EVENT_SHA, main_commit: str = EVENT_SHA) -> FakeGitHubReader:
    tag_path = f"/repos/{REPOSITORY}/git/ref/tags/{RELEASE_TAG}"
    return FakeGitHubReader(
        {
            tag_path: _tag_ref("tag", ROOT_TAG_SHA),
            f"/repos/{REPOSITORY}/git/tags/{ROOT_TAG_SHA}": _annotated_tag(
                sha=ROOT_TAG_SHA,
                target_type="commit",
                target_sha=tag_commit,
            ),
            f"/repos/{REPOSITORY}/git/ref/heads/main": _main_ref(main_commit),
        }
    )


def test_live_release_ref_accepts_stable_annotated_tag_on_exact_main() -> None:
    report = validate_live_release_ref(
        _direct_api(),
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        event_sha=EVENT_SHA,
    )

    assert report["ok"] is True
    assert report["tagCommitSha"] == EVENT_SHA
    assert report["mainSha"] == EVENT_SHA
    assert report["annotatedTagDepth"] == 1
    assert report["stableRefSnapshot"] is True


def test_live_release_ref_rejects_lightweight_tag() -> None:
    tag_path = f"/repos/{REPOSITORY}/git/ref/tags/{RELEASE_TAG}"
    api = FakeGitHubReader(
        {
            tag_path: _tag_ref("commit", EVENT_SHA),
        }
    )

    with pytest.raises(ValueError, match="annotated Git tag"):
        validate_live_release_ref(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            event_sha=EVENT_SHA,
        )


def test_live_release_ref_rejects_moved_tag() -> None:
    with pytest.raises(ValueError, match="not event SHA"):
        validate_live_release_ref(
            _direct_api(tag_commit=OTHER_SHA),
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            event_sha=EVENT_SHA,
        )


def test_live_release_ref_peels_nested_annotated_tags() -> None:
    tag_path = f"/repos/{REPOSITORY}/git/ref/tags/{RELEASE_TAG}"
    api = FakeGitHubReader(
        {
            tag_path: _tag_ref("tag", ROOT_TAG_SHA),
            f"/repos/{REPOSITORY}/git/tags/{ROOT_TAG_SHA}": _annotated_tag(
                sha=ROOT_TAG_SHA,
                target_type="tag",
                target_sha=INNER_TAG_SHA,
            ),
            f"/repos/{REPOSITORY}/git/tags/{INNER_TAG_SHA}": _annotated_tag(
                sha=INNER_TAG_SHA,
                target_type="commit",
                target_sha=EVENT_SHA,
                tag="signed-release-chain",
            ),
            f"/repos/{REPOSITORY}/git/ref/heads/main": _main_ref(EVENT_SHA),
        }
    )

    report = validate_live_release_ref(
        api,
        repository=REPOSITORY,
        release_tag=RELEASE_TAG,
        event_sha=EVENT_SHA,
    )

    assert report["annotatedTagDepth"] == 2
    assert report["tagCommitSha"] == EVENT_SHA


def test_live_release_ref_rejects_wrong_main_commit() -> None:
    with pytest.raises(ValueError, match="live main ref"):
        validate_live_release_ref(
            _direct_api(main_commit=OTHER_SHA),
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            event_sha=EVENT_SHA,
        )


def test_live_release_ref_rejects_tag_that_moves_during_validation() -> None:
    tag_path = f"/repos/{REPOSITORY}/git/ref/tags/{RELEASE_TAG}"
    api = FakeGitHubReader(
        {
            tag_path: [
                _tag_ref("tag", ROOT_TAG_SHA),
                _tag_ref("tag", INNER_TAG_SHA),
            ],
            f"/repos/{REPOSITORY}/git/tags/{ROOT_TAG_SHA}": _annotated_tag(
                sha=ROOT_TAG_SHA,
                target_type="commit",
                target_sha=EVENT_SHA,
            ),
            f"/repos/{REPOSITORY}/git/ref/heads/main": _main_ref(EVENT_SHA),
        }
    )

    with pytest.raises(ValueError, match="changed while"):
        validate_live_release_ref(
            api,
            repository=REPOSITORY,
            release_tag=RELEASE_TAG,
            event_sha=EVENT_SHA,
        )
