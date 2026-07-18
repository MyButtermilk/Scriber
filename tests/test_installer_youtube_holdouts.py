from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.validate_installer_youtube_holdouts import observed_capabilities


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
