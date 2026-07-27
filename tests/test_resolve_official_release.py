from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.resolve_official_release import (
    read_source_version,
    resolve_official_release,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "resolve_official_release.py"
SHA = "1" * 40


def test_only_exact_main_push_tag_and_source_version_is_official(tmp_path: Path) -> None:
    version_file = tmp_path / "version.py"
    version_file.write_text('__version__ = "1.2.3"\n', encoding="utf-8")

    decision = resolve_official_release(
        event_name="push",
        ref="refs/tags/v1.2.3",
        ref_type="tag",
        github_sha=SHA,
        main_sha=SHA,
        source_version=read_source_version(version_file),
    )

    assert decision["officialRelease"] is True
    assert decision["releaseTag"] == "v1.2.3"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_name", "workflow_dispatch"),
        ("ref", "refs/tags/v1.2"),
        ("ref", "refs/tags/v1.2.3-rc.1"),
        ("ref", "refs/heads/main"),
        ("ref_type", "branch"),
        ("github_sha", "2" * 40),
        ("source_version", "1.2.4"),
    ],
)
def test_any_failed_invariant_denies_official_release(field: str, value: str) -> None:
    arguments = {
        "event_name": "push",
        "ref": "refs/tags/v1.2.3",
        "ref_type": "tag",
        "github_sha": SHA,
        "main_sha": SHA,
        "source_version": "1.2.3",
    }
    arguments[field] = value
    assert resolve_official_release(**arguments)["officialRelease"] is False


def test_invalid_automatic_tag_fails_closed(tmp_path: Path) -> None:
    version_file = tmp_path / "version.py"
    version_file.write_text('__version__ = "1.2.4"\n', encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--event-name",
            "push",
            "--ref",
            "refs/tags/v1.2.3",
            "--ref-type",
            "tag",
            "--github-sha",
            SHA,
            "--main-sha",
            SHA,
            "--version-file",
            str(version_file),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "sourceVersionMatchesTag" in completed.stderr
