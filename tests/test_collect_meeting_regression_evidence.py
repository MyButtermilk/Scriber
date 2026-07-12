from __future__ import annotations

from pathlib import Path

import pytest

from scripts.collect_meeting_regression_evidence import _load_json, parse_pytest_junit, parse_rust_passed


def test_parse_pytest_junit_counts_passed_and_skipped(tmp_path: Path) -> None:
    report = tmp_path / "pytest.xml"
    report.write_text(
        '<testsuites><testsuite tests="1255" failures="0" errors="0" skipped="2" /></testsuites>',
        encoding="utf-8",
    )
    assert parse_pytest_junit(report) == {
        "tests": 1255,
        "failures": 0,
        "errors": 0,
        "skipped": 2,
        "passed": 1253,
    }


def test_parse_rust_passed_sums_multiple_test_binaries() -> None:
    output = """
test result: ok. 32 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
"""
    assert parse_rust_passed(output) == 35


def test_parse_rust_passed_rejects_missing_result() -> None:
    with pytest.raises(ValueError, match="successful test result"):
        parse_rust_passed("Finished test profile")


def test_load_json_accepts_utf8_bom_from_powershell_reports(tmp_path: Path) -> None:
    report = tmp_path / "build-timing.json"
    report.write_text('{"ok": true}', encoding="utf-8-sig")

    assert _load_json(report, "build timing") == {"ok": True}


def test_collector_requires_installed_meeting_audio_evidence() -> None:
    source = Path("scripts/collect_meeting_regression_evidence.py").read_text(
        encoding="utf-8"
    )

    assert '"Tauri sidecar preparation"' in source
    assert '"Tauri Windows bundle"' in source
    assert '"Frontend type check",' not in source
    assert 'meeting_audio = installed.get("meetingAudioDeviceTest", {})' in source
    assert '("microphone", "system", "mic_clean")' in source
    assert '"installedMeetingAudioDeviceTestPassed": True' in source
