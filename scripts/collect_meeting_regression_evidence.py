from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.new_meeting_release_evidence import build_template


RUST_RESULT_RE = re.compile(
    r"test result:\s+ok\.\s+(?P<passed>\d+) passed;\s+(?P<failed>\d+) failed;",
    re.IGNORECASE,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_pytest_junit(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            totals[key] += int(float(suite.attrib.get(key, "0")))
    totals["passed"] = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    return totals


def parse_rust_passed(output: str) -> int:
    matches = list(RUST_RESULT_RE.finditer(output))
    if not matches:
        raise ValueError("cargo test output did not contain a successful test result")
    if any(int(match.group("failed")) != 0 for match in matches):
        raise ValueError("cargo test output contained failed tests")
    return sum(int(match.group("passed")) for match in matches)


def _run(command: list[str], *, cwd: Path, timeout: int) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        tail = "\n".join(output.splitlines()[-30:])
        raise ValueError(f"command failed with exit code {result.returncode}: {' '.join(command[:4])}\n{tail}")
    return output


def _load_json(path: Path, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} is not a JSON object")
    return payload


def _require_json(path: Path, label: str) -> dict[str, Any]:
    payload = _load_json(path, label)
    if payload.get("ok") is not True:
        raise ValueError(f"{label} is not a passing JSON report")
    return payload


def collect(
    *,
    installer_path: Path,
    browser_smoke_path: Path,
    build_timing_path: Path,
    installed_smoke_path: Path,
    output_dir: Path,
    app_version: str,
    python_executable: Path,
) -> tuple[Path, Path]:
    installer = installer_path.expanduser().resolve()
    if not installer.is_file():
        raise ValueError("installer is missing")
    browser = _require_json(browser_smoke_path, "Meeting browser smoke")
    installed = _require_json(installed_smoke_path, "installed package smoke")
    timing = _load_json(build_timing_path, "build timing")

    required_phases = {
        "Tauri sidecar preparation",
        "Tauri Windows bundle",
    }
    phases = {
        str(item.get("label")): item.get("ok") is True
        for item in timing.get("phases", [])
        if isinstance(item, dict)
    }
    missing_phases = sorted(label for label in required_phases if phases.get(label) is not True)
    if missing_phases:
        raise ValueError(f"build timing is missing passing phases: {', '.join(missing_phases)}")
    meeting_scenarios = [
        item for item in browser.get("scenarios", [])
        if isinstance(item, dict) and item.get("route") == "/meetings" and item.get("ok") is True
    ]
    if not meeting_scenarios:
        raise ValueError("Meeting browser smoke lacks a passing /meetings scenario")
    meeting_checks = [
        check for check in meeting_scenarios[0].get("interactionChecks", [])
        if isinstance(check, dict) and check.get("name") == "meeting-end-to-end" and check.get("ok") is True
    ]
    if not meeting_checks:
        raise ValueError("Meeting browser smoke lacks the passing end-to-end interaction")
    if installed.get("cleanupVerified") is not True or installed.get("frontend", {}).get("verified") is not True:
        raise ValueError("installed package smoke lacks frontend or cleanup verification")
    meeting_audio = installed.get("meetingAudioDeviceTest", {})
    meeting_sources = meeting_audio.get("sources", {}) if isinstance(meeting_audio, dict) else {}
    if (
        meeting_audio.get("verified") is not True
        or meeting_audio.get("aecActive") is not True
        or meeting_audio.get("audioPersisted") is not False
        or meeting_audio.get("audioSentToProvider") is not False
        or any(
            not isinstance(meeting_sources.get(source), dict)
            or meeting_sources[source].get("active") is not True
            or int(meeting_sources[source].get("frames") or 0) <= 0
            for source in ("microphone", "system", "mic_clean")
        )
    ):
        raise ValueError("installed package smoke lacks real synthetic Meeting audio evidence")

    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = output_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scriber-regression-evidence-") as temp_dir:
        junit = Path(temp_dir) / "pytest.xml"
        _run(
            [str(python_executable), "-m", "pytest", "-q", f"--junitxml={junit}"],
            cwd=REPO_ROOT,
            timeout=900,
        )
        pytest_counts = parse_pytest_junit(junit)
    if pytest_counts["failures"] or pytest_counts["errors"] or pytest_counts["passed"] < 1099:
        raise ValueError(f"Python regression suite is insufficient: {pytest_counts}")

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    cargo = shutil.which("cargo.exe") or shutil.which("cargo")
    if not npm or not cargo:
        raise ValueError("npm and cargo are required for regression evidence")
    _run([npm, "run", "check"], cwd=REPO_ROOT / "Frontend", timeout=180)
    rust_audio_output = _run(
        [cargo, "test", "--bin", "scriber-audio-sidecar"],
        cwd=REPO_ROOT / "Frontend" / "src-tauri",
        timeout=600,
    )
    rust_lib_output = _run(
        [cargo, "test", "--lib"],
        cwd=REPO_ROOT / "Frontend" / "src-tauri",
        timeout=600,
    )
    rust_audio_passed = parse_rust_passed(rust_audio_output)
    rust_lib_passed = parse_rust_passed(rust_lib_output)
    browser_check_count = int(browser.get("summary", {}).get("interactionCheckCount") or 0)
    automated_tests_passed = pytest_counts["passed"] + rust_audio_passed + rust_lib_passed + browser_check_count

    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    summary = {
        "schemaVersion": 1,
        "kind": "scriber-meeting-automated-regression-summary",
        "ok": True,
        "capturedAtUtc": captured_at,
        "installerSha256": _sha256(installer),
        "counts": {
            "pythonPassed": pytest_counts["passed"],
            "pythonSkipped": pytest_counts["skipped"],
            "rustAudioPassed": rust_audio_passed,
            "rustLibraryPassed": rust_lib_passed,
            "browserInteractionChecksPassed": browser_check_count,
            "automatedTestsPassed": automated_tests_passed,
        },
        "checks": {
            "pythonSuitePassed": True,
            "frontendTypeCheckPassed": True,
            "rustAudioSuitePassed": True,
            "rustLibrarySuitePassed": True,
            "meetingBrowserEndToEndPassed": True,
            "installerBuildPhasesPassed": True,
            "installedFrontendPassed": True,
            "installedCleanupPassed": True,
            "installedMeetingAudioDeviceTestPassed": True,
        },
    }
    summary_path = artifact_dir / "automated-regression-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    report = build_template(
        profile="automated-regression-suite",
        app_version=app_version,
        installer_sha256=summary["installerSha256"],
        signed_installer=False,
    )
    report.update(
        completed=True,
        operatorConfirmed=True,
        capturedAtUtc=captured_at,
        measurements={"automatedTestsPassed": automated_tests_passed},
        checks={"automatedRegressionSuitePassed": True},
        artifacts=[
            {
                "kind": "redacted-automated-regression-summary",
                "path": "artifacts/automated-regression-summary.json",
                "sha256": _sha256(summary_path),
            }
        ],
        notes="Automated source, Rust, browser, build, installed-frontend, and cleanup verification. Raw logs and local paths are intentionally excluded from matrix evidence.",
    )
    report_path = output_dir / "meeting-release-evidence-automated-regression-suite.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report_path, summary_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect hash-bound Meeting automated regression evidence.")
    parser.add_argument("--installer", required=True)
    parser.add_argument("--browser-smoke", required=True)
    parser.add_argument("--build-timing", required=True)
    parser.add_argument("--installed-smoke", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--python", default=sys.executable)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        report, summary = collect(
            installer_path=Path(args.installer),
            browser_smoke_path=Path(args.browser_smoke),
            build_timing_path=Path(args.build_timing),
            installed_smoke_path=Path(args.installed_smoke),
            output_dir=Path(args.output_dir),
            app_version=args.app_version,
            python_executable=Path(args.python).resolve(),
        )
    except (OSError, ValueError, json.JSONDecodeError, ET.ParseError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "report": str(report.resolve()), "summary": str(summary.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
