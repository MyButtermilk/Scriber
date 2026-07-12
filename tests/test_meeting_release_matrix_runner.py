from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.version import __version__


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "run_meeting_release_matrix.ps1"


def _powershell() -> str:
    for candidate in ("powershell", "pwsh"):
        try:
            subprocess.run(
                [candidate, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
                capture_output=True,
                check=True,
                text=True,
            )
            return candidate
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    pytest.skip("PowerShell is unavailable")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_plan_only_lists_all_real_release_profiles(tmp_path: Path) -> None:
    result = _run("-PlanOnly", "-OutputDir", str(tmp_path))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["planOnly"] is True
    assert payload["appVersion"] == __version__
    assert payload["readyForDraftInitialization"] is False
    assert len(payload["profiles"]) == 19
    assert {item["profile"] for item in payload["profiles"]} >= {
        "teams-laptop-speakerphone",
        "zoom-wired-headset",
        "meet-bluetooth-headset",
        "outlook-work-school",
        "recording-60m",
        "stability-2h",
        "voiceprint-held-corpus",
        "support-bundle-privacy",
        "eu-voiceprint-privacy-review",
        "automated-regression-suite",
        "signed-release",
    }
    assert "validate_meeting_release_matrix.py" in payload["validationCommand"]


def test_initialize_creates_non_passing_sha_bound_drafts(tmp_path: Path) -> None:
    installer = tmp_path / "Scriber_0.4.35_x64-setup.exe"
    installer.write_bytes(b"synthetic installer fixture")
    output = tmp_path / "matrix"

    result = _run(
        "-InitializeDrafts",
        "-OutputDir",
        str(output),
        "-InstallerPath",
        str(installer),
    )

    assert result.returncode == 0, result.stderr
    drafts = sorted(output.glob("meeting-release-draft-*.json"))
    assert len(drafts) == 19
    payload = json.loads(drafts[0].read_text(encoding="utf-8"))
    assert payload["completed"] is False
    assert payload["operatorConfirmed"] is False
    assert payload["build"]["installedApp"] is True
    assert payload["build"]["signedInstaller"] is False
    assert payload["build"]["authenticodeValid"] is False
    assert payload["build"]["updaterSignatureVerified"] is False
    assert len(payload["build"]["installerSha256"]) == 64
    assert not list(output.glob("meeting-release-evidence-*.json"))


def test_runner_source_keeps_drafts_separate_from_accepted_evidence() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "meeting-release-draft-{0}.json" in source
    assert "meeting-release-evidence-{0}.json" in source
    assert "Get-AuthenticodeSignature" in source
    assert "Do not store audio, transcript text, tokens" in source


def test_reinitialize_archives_drafts_bound_to_an_old_installer(tmp_path: Path) -> None:
    installer = tmp_path / "Scriber_0.4.35_x64-setup.exe"
    installer.write_bytes(b"first installer fixture")
    output = tmp_path / "matrix"
    first = _run(
        "-InitializeDrafts",
        "-OutputDir",
        str(output),
        "-InstallerPath",
        str(installer),
    )
    assert first.returncode == 0, first.stderr
    draft = output / "meeting-release-draft-automated-regression-suite.json"
    original = json.loads(draft.read_text(encoding="utf-8"))
    original["notes"] = "operator work in progress"
    draft.write_text(json.dumps(original), encoding="utf-8")

    installer.write_bytes(b"second installer fixture")
    second = _run(
        "-InitializeDrafts",
        "-OutputDir",
        str(output),
        "-InstallerPath",
        str(installer),
    )

    assert second.returncode == 0, second.stderr
    current = json.loads(draft.read_text(encoding="utf-8"))
    assert current["build"]["installerSha256"] != original["build"]["installerSha256"]
    archived = list((output / "stale-drafts").glob(
        "meeting-release-draft-automated-regression-suite-*.json"
    ))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))["notes"] == (
        "operator work in progress"
    )
