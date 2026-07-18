from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_installer_youtube_holdouts import (
    HoldoutError,
    observed_capabilities,
    pinned_deno_version,
    require_baseline_environment_root,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    REPO_ROOT
    / "scripts"
    / "perf"
    / "profiles"
    / "installer-size"
    / "youtube-holdouts.json"
)


def test_frozen_holdouts_are_distinct_pending_public_routes() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = payload["cases"]

    assert len(cases) == 6
    assert len({case["url"] for case in cases}) == 6
    assert all(case["status"] == "pending_validation" for case in cases)
    assert all(case["url"].startswith("https://") for case in cases)
    shorts = next(case for case in cases if case["family"] == "shorts")
    music = next(case for case in cases if case["family"] == "music")
    assert "/shorts/" in shorts["url"]
    assert music["url"].startswith("https://music.youtube.com/watch?")


def test_capabilities_require_observed_deno_jsc_and_route_shapes() -> None:
    info = {
        "id": "abcdefghijk",
        "extractor_key": "Youtube",
        "formats": [
            {
                "acodec": "opus",
                "url": "https://media.example.test/audio?n=token&sig=signed",
            }
        ],
        "subtitles": {"en": [{}]},
        "automatic_captions": {},
        "live_status": "was_live",
        "was_live": True,
    }
    debug = (
        '[youtube] Downloading player 123-main\n'
        '[youtube] [jsc:deno] Solving JS challenges using deno\n'
    )

    observed, details = observed_capabilities(
        family="signature-challenge",
        url="https://www.youtube.com/watch?v=abcdefghijk",
        info=info,
        debug_log=debug,
    )

    assert {
        "metadata",
        "audio-format-url",
        "player-js",
        "deno-jsc",
        "signature",
        "js-challenge-solved",
    }.issubset(observed)
    assert details["denoJscObserved"] is True
    assert details["hasNQuery"] is True


def test_capabilities_do_not_claim_a_solved_challenge_without_signed_media() -> None:
    observed, _details = observed_capabilities(
        family="signature-challenge",
        url="https://www.youtube.com/watch?v=abcdefghijk",
        info={
            "id": "abcdefghijk",
            "extractor_key": "Youtube",
            "formats": [
                {
                    "acodec": "opus",
                    "url": "https://media.example.test/audio?n=token",
                }
            ],
        },
        debug_log=(
            '[youtube] Downloading player 123-main\n'
            '[youtube] [jsc:deno] Solving JS challenges using deno\n'
        ),
    )

    assert "deno-jsc" in observed
    assert "js-challenge-solved" not in observed


def test_invalid_run_id_fails_before_network_access() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "validate_installer_youtube_holdouts.py"),
            "--repo-root",
            str(REPO_ROOT),
            "--run-id",
            "not-a-uuid",
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout)["ok"] is False


@pytest.mark.parametrize(
    "stdout",
    (
        "deno 2.9.2\nv8 14.9.107.11-rusty\ntypescript 6.0.2\n",
        "deno 2.9.2 (stable, release, x86_64-pc-windows-msvc)\n",
    ),
)
def test_pinned_deno_version_normalizes_supported_stable_output(stdout: str) -> None:
    assert pinned_deno_version(stdout) == "2.9.2"


@pytest.mark.parametrize(
    "stdout",
    (
        "deno 2.9.3\n",
        "deno 2.9.2-rc.0\n",
        "2.9.2\n",
        "deno 2.9.2 unexpected trailing text\n",
        "",
    ),
)
def test_pinned_deno_version_rejects_unpinned_or_ambiguous_output(stdout: str) -> None:
    with pytest.raises(HoldoutError, match="pinned 2.9.2"):
        pinned_deno_version(stdout)


def test_holdout_process_is_bound_to_exact_run_environment(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    expected = run_root / "environments" / "baseline" / ".venv"
    expected.mkdir(parents=True)
    other = tmp_path / "other-venv"
    other.mkdir()

    assert require_baseline_environment_root(run_root, expected) == expected.resolve()
    with pytest.raises(HoldoutError, match="this RunId's baseline environment"):
        require_baseline_environment_root(run_root, other)
