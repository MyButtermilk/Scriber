from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import validate_installer_youtube_holdouts as holdouts
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


def test_probe_disables_plugins_and_sanitizes_python_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inherited = {
        "PATH": "preserved",
        "PYTHONBREAKPOINT": "debugger.hook",
        "PYTHONHOME": "host-python",
        "PYTHONINSPECT": "1",
        "PYTHONPATH": "host-injection",
        "PYTHONSTARTUP": "startup.py",
        "PYTHONUSERBASE": "user-site",
        "YTDLP_CONFIG": "host-config",
        "YTDLP_NO_LAZY_EXTRACTORS": "1",
        "YT_DLP_CONFIG": "alternate-host-config",
        "YTDLP_NO_PLUGINS": "0",
    }
    captured: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["environment"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"id": "abcdefghijk"}),
            stderr="probe-debug",
        )

    monkeypatch.setattr(holdouts.subprocess, "run", fake_run)
    deno_executable = tmp_path / "deno.exe"
    payload, debug_log, policy = holdouts._run_probe(
        url="https://www.youtube.com/watch?v=abcdefghijk",
        deno_executable=deno_executable,
        timeout_seconds=30,
        inherited_environment=inherited,
    )

    command = captured["command"]
    environment = captured["environment"]
    assert isinstance(command, list)
    assert isinstance(environment, dict)
    assert command.count("--no-plugin-dirs") == 1
    assert command[command.index("--js-runtimes") + 1] == f"deno:{deno_executable}"
    assert all(
        name not in environment for name in holdouts.PROBE_ENVIRONMENT_REMOVALS
    )
    assert environment["PATH"] == "preserved"
    assert environment["YTDLP_NO_PLUGINS"] == "1"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONSAFEPATH"] == "1"
    assert "YTDLP_NO_LAZY_EXTRACTORS" not in environment
    assert inherited["PYTHONPATH"] == "host-injection"
    assert inherited["YTDLP_NO_LAZY_EXTRACTORS"] == "1"
    assert payload == {"id": "abcdefghijk"}
    assert debug_log == "probe-debug"
    assert policy == {
        "configDiscovery": False,
        "download": False,
        "explicitSingleRuntime": True,
        "externalPlugins": False,
        "pythonPathInheritance": False,
        "pythonUserSite": False,
        "remoteComponents": False,
        "ytDlpCache": False,
    }


def test_probe_policy_rejects_missing_plugin_cli_boundary(tmp_path: Path) -> None:
    environment = holdouts._sanitized_probe_environment({})
    with pytest.raises(HoldoutError, match="policy switches are not exact"):
        holdouts._probe_policy(
            command=[
                sys.executable,
                "-m",
                "yt_dlp",
                "--no-config",
                "--no-cache-dir",
                "--no-js-runtimes",
                "--js-runtimes",
                f"deno:{tmp_path / 'deno.exe'}",
                "--no-remote-components",
            ],
            environment=environment,
            deno_executable=tmp_path / "deno.exe",
        )
