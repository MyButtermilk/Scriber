from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = REPO_ROOT / "scripts" / "perf" / "collect_app_ux.ps1"
RUNNER = REPO_ROOT / "scripts" / "perf" / "run.ps1"


def test_app_ux_orchestrator_runs_prepare_lifecycle_and_final_merge_in_order() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")

    prepare = source.index('"--prepare-only"')
    lifecycle = source.index("$lifecycleCollector", prepare)
    final_merge = source.index('"--lifecycle-evidence", $lifecyclePath', lifecycle)

    assert prepare < lifecycle < final_merge
    assert "benchmarks\\windows\\app_ux_collector.py" in source
    assert "benchmarks\\windows\\app_ux_lifecycle_collector.py" in source
    assert '"--request", $requestPath' in source
    assert '"--output", $lifecyclePath' in source
    assert source.count('"--output", $finalPath') == 2
    assert '"--run-id", $RunId' in source
    assert source.count('ToString("N")') == 2
    assert 'ToString("D")' not in source
    assert source.count('"--samples-per-scenario", $SamplesPerScenario.ToString()') == 2


def test_app_ux_orchestrator_treats_prepared_request_as_binding_authority() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")

    for marker in (
        'RequestContract = "b7-app-ux-lifecycle-request-v1"',
        'LifecycleContract = "b7-app-ux-lifecycle-import-v1"',
        'FinalContract = "b7-app-ux-v1"',
        "Assert-RequestBinding",
        "Assert-LifecycleBinding",
        "Assert-FinalBinding",
        "Request.installedExeSha256 -cne $InstalledSha256",
        "Lifecycle.installedExeSha256 -cne [string]$Request.installedExeSha256",
        "Lifecycle.harnessManifestSha256 -cne [string]$Request.harnessManifestSha256",
        "Final.installedExeSha256 -cne [string]$Request.installedExeSha256",
        "Final.harnessManifestSha256 -cne [string]$Request.harnessManifestSha256",
        "Validation.metricEligible",
        "request_or_installed_payload_drift_after_lifecycle",
        "request_or_installed_payload_drift_after_final_merge",
        "prepared_request_schema_binding_invalid",
    ):
        assert marker in source

    assert source.count("Assert-ExactStringSequence") >= 3
    assert "lifecycle_import_sample_count_invalid" in source
    assert "final_app_ux_sample_count_invalid" in source


def test_app_ux_orchestrator_is_fail_closed_and_does_not_evaluate_source() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    lowered = source.casefold()

    assert "app_ux_output_directory_must_be_empty" in source
    assert "app_ux_output_and_install_roots_must_not_overlap" in source
    assert "required_app_ux_collector_missing" in source
    assert "preexisting_scriber_process" in source
    assert "app_ux_collector_process_cleanup_failed" in source
    assert source.count("Get-ScriberProcessCount") >= 3
    assert "Write-AtomicJson -Path $summaryPath" in source
    assert "app_ux_collection_failed" in source
    assert "Remove-Item -Recurse" not in source
    for forbidden in (
        "invoke-expression",
        "scriptblock]::create",
        "scriptblock::create",
        "-encodedcommand",
        "frombase64string",
    ):
        assert forbidden not in lowered
    assert not re.search(r"(?im)(?:^|[;&|]\s*)iex(?:\s|$)", source)


def test_fastlocal_accepts_explicit_app_ux_evidence_and_suppresses_env_fallback() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "[string]$AppUxEvidencePath" in source
    assert "-AppUxEvidencePath is allowed only with FastLocal or FullLocal." in source
    assert "appUxEvidenceProvided" in source
    assert "appUxEvidenceSha256" in source
    assert "appUxEvidencePostSha256" in source
    assert "appUxEvidenceDriftDetected" in source
    assert "app_ux_evidence_drift" in source
    assert '$endpointProbeArgs += @("--app-ux-evidence", $AppUxEvidencePath)' in source
    assert '[Environment]::SetEnvironmentVariable("SCRIBER_B7_APP_UX_EVIDENCE", $null, "Process")' in source
    assert "previousAppUxEvidenceEnv" in source
    assert source.index("$endpointProbeArgs = @(") < source.index("& $pythonExecutable @endpointProbeArgs")
