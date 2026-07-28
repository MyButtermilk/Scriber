from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_python314_o0_full_local_correctness.ps1"
VALIDATOR = ROOT / "scripts" / "perf" / "validate_o0_full_local_correctness.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("o0_full_local_validator", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_is_correctness_only_and_fail_closed() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    for required in (
        "smoke_windows_installer.ps1",
        "collect_app_ux.ps1",
        "scripts\\perf\\run.ps1",
        '"FullLocal"',
        "runtime_attestation.py",
        "validate_o0_full_local_correctness.py",
        "Invoke-StrictUninstall",
        "ExpectedInstallerSha256",
        "SourceCommit",
        "SourceTreeSha256",
        "productionPromotionAuthorized = $false",
        "promotionEligible = $false",
        "a13Measured = $false",
        "counterbalancedAbBa = $false",
    ):
        assert required in text

    for forbidden in (
        "Invoke-Expression",
        "-EncodedCommand",
        "Remove-Item -Recurse",
        "Remove-Item -Path *",
        "CandidateInstallerName",
    ):
        assert forbidden not in text

    scoped_process_function = text.split("function Get-ScopedScriberProcesses", maxsplit=1)[1].split(
        "function Get-AllScriberProcesses", maxsplit=1
    )[0]
    assert "ExecutablePath" in scoped_process_function
    assert "CommandLine" not in scoped_process_function


def test_validator_rejects_non_full_local_packet() -> None:
    validator = _load_validator()

    with pytest.raises(validator.EvidenceError, match="raw.schemaVersion"):
        validator.validate(
            {},
            {},
            expected_commit="a" * 40,
            expected_source_content_sha256="b" * 64,
            expected_attestation_id="c" * 64,
            expected_attestation_sha256="d" * 64,
            expected_app_ux_sha256="e" * 64,
        )


def test_validator_pins_full_local_sample_contract() -> None:
    validator = _load_validator()

    assert validator.EXPECTED_SAMPLE_PLAN == {
        "overlayCold": 15,
        "overlayWarm": 30,
        "providerReplay": 30,
        "providerReplayDurationsSeconds": [5, 15, 30, 60],
        "appUxPerScenario": 20,
    }
    assert validator.EXPECTED_PROVIDERS == ("microsoft", "soniox", "speechmatics")
    assert len(validator.EXPECTED_APP_UX_SCENARIOS) == 9
