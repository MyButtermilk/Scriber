from __future__ import annotations

import json
import hashlib
import math
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.perf.benchmark_lint import REQUIRED_METRICS, lint
from scripts.perf.evaluator.local_wux import (
    PROVIDER_REPLAY_DURATION_SECONDS,
    PROVIDER_REPLAY_WEIGHTS,
    SCENARIO_METRICS,
    canonical_provider_replay_promotion_eligible,
    compute_local_wux,
)

PERF_ROOT = REPO_ROOT / "scripts" / "perf"
if str(PERF_ROOT) not in sys.path:
    sys.path.insert(0, str(PERF_ROOT))
from scripts.perf import doctor
from scripts.perf import runtime_attestation


def valid_lint_metric_value(name: str) -> int:
    if (
        name == "local_wux"
        or name.endswith("_sample_count")
        or name.endswith("_capture_attested")
        or name.endswith("_ms")
    ):
        return 1
    return 0


def test_required_autoresearch_files_exist():
    for relative in [
        "GOAL.md",
        "autoresearch.md",
        "autoresearch.jsonl",
        "autoresearch.config.json",
        "autoresearch.ideas.md",
        "autoresearch.ps1",
        "autoresearch.checks.ps1",
        "scripts/perf/run.ps1",
        "scripts/perf/benchmark_lint.py",
        "scripts/perf/doctor.py",
        "scripts/perf/runtime_attestation.py",
        "benchmarks/windows/profile.ps1",
        "benchmarks/windows/overlay_observer.py",
        "benchmarks/windows/TextReceiver.ps1",
        "benchmarks/windows/text_observer.ps1",
        "benchmarks/windows/app_action.ps1",
        "benchmarks/windows/app_observer.ps1",
        "benchmarks/windows/app_ux_collector.py",
        "benchmarks/windows/app_ux_lifecycle_import.schema.json",
        "benchmarks/windows/endpoint_probe.py",
        "benchmarks/windows/trace_collector.py",
        "benchmarks/results/baseline.json",
        "scripts/perf/evaluator/local_wux.py",
    ]:
        assert (REPO_ROOT / relative).exists(), relative


def test_autoresearch_config_matches_goal_contract():
    config = json.loads((REPO_ROOT / "autoresearch.config.json").read_text(encoding="utf-8"))
    assert config["sessionName"] == "Scriber Windows Perceived Performance"
    assert config["primaryMetric"] == "local_wux"
    assert config["direction"] == "lower"
    assert config["benchmarkCommand"].endswith(".\\autoresearch.ps1 -Suite FastLocal")
    assert config["checksCommand"].endswith(".\\autoresearch.checks.ps1")
    assert config["baseline"]["accepted"] is True
    assert config["baseline"]["value"] == 1.0
    assert "benchmarks/windows/" in config["protectedBenchmarkSurface"]


def test_benchmark_lint_accepts_complete_finite_metric_package():
    output = "\n".join(
        f"METRIC {name}={valid_lint_metric_value(name)}"
        for name in REQUIRED_METRICS
    )
    assert lint(output) == []
    assert "microsoft_local_tail_p95_ms" not in REQUIRED_METRICS
    assert (
        "microsoft_local_60s_activation_received_to_final_text_observed_p95_ms"
        in REQUIRED_METRICS
    )
    assert (
        "soniox_local_15s_stop_requested_to_final_text_observed_failure_rate"
        in REQUIRED_METRICS
    )
    assert (
        "speechmatics_local_30s_activation_received_to_final_text_observed_capture_attested"
        in REQUIRED_METRICS
    )


def test_endpoint_dotenv_preserves_explicit_candidate_overrides(
    monkeypatch,
    tmp_path,
):
    from benchmarks.windows import endpoint_probe

    env_path = tmp_path / ".env"
    env_path.write_text(
        "SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3=1\n"
        "SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV=0\n"
        "SCRIBER_TEST_DOTENV_ONLY=loaded\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV", "1")
    monkeypatch.setenv("SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3", "0")
    monkeypatch.delenv("SCRIBER_TEST_DOTENV_ONLY", raising=False)

    imported = endpoint_probe.import_dotenv_into_process(env_path)

    assert endpoint_probe.os.environ["SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV"] == "1"
    assert endpoint_probe.os.environ["SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3"] == "0"
    assert endpoint_probe.os.environ["SCRIBER_TEST_DOTENV_ONLY"] == "loaded"
    assert imported == ["SCRIBER_TEST_DOTENV_ONLY"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process contract")
def test_process_generation_treats_queryable_terminated_process_as_exited():
    from benchmarks.windows import endpoint_probe

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        assert endpoint_probe._process_creation_time_100ns(child.pid) is not None
        child.terminate()
        child.wait(timeout=10)

        # Popen intentionally still owns its native process handle here. The
        # exited process object is queryable, but it is no longer a live PID
        # generation and cleanup must recognize that immediately.
        assert endpoint_probe._process_creation_time_100ns(child.pid) is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def _runtime_generation_snapshot(
    endpoint_probe,
    *,
    frontend_ready_received_at: str = "2026-07-21T00:00:01Z",
    webviews: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "reasons": [],
        "generationComparisonContract": "baseline-descendants-v1",
        "app": {
            "pid": 100,
            "parentPid": 10,
            "name": "scriber-desktop.exe",
            "creationTime100ns": 1_000,
        },
        "backend": {
            "pid": 200,
            "parentPid": 100,
            "name": "scriber-backend.exe",
            "creationTime100ns": 2_000,
        },
        "webViewProcesses": webviews
        if webviews is not None
        else [
            {
                "pid": 300,
                "parentPid": 100,
                "name": "msedgewebview2.exe",
                "creationTime100ns": 3_000,
            }
        ],
        "backendStartedAt": "2026-07-21T00:00:00Z",
        "frontendReadyReceivedAt": frontend_ready_received_at,
    }
    payload["fingerprint"] = endpoint_probe.process_generation_fingerprint(payload)
    return payload


def _refresh_runtime_generation_fingerprint(endpoint_probe, payload) -> None:
    payload["fingerprint"] = endpoint_probe.process_generation_fingerprint(payload)


def test_process_generation_match_allows_frontend_heartbeat_update() -> None:
    from benchmarks.windows import endpoint_probe

    baseline = _runtime_generation_snapshot(endpoint_probe)
    observed = _runtime_generation_snapshot(
        endpoint_probe,
        frontend_ready_received_at="2026-07-21T00:00:10Z",
    )

    assert baseline["fingerprint"] != observed["fingerprint"]
    assert endpoint_probe.process_generation_matches(baseline, observed) is True


def test_process_generation_match_allows_only_additional_webview_descendants() -> None:
    from benchmarks.windows import endpoint_probe

    baseline = _runtime_generation_snapshot(endpoint_probe)
    observed = _runtime_generation_snapshot(
        endpoint_probe,
        frontend_ready_received_at="2026-07-21T00:00:10Z",
        webviews=[
            *baseline["webViewProcesses"],
            {
                "pid": 301,
                "parentPid": 300,
                "name": "msedgewebview2.exe",
                "creationTime100ns": 3_100,
            },
        ],
    )

    assert endpoint_probe.process_generation_matches(baseline, observed) is True
    assert endpoint_probe.process_generation_matches(observed, baseline) is False


def test_process_generation_match_rejects_restarts_and_webview_replacement() -> None:
    from benchmarks.windows import endpoint_probe

    baseline = _runtime_generation_snapshot(endpoint_probe)

    app_restarted = json.loads(json.dumps(baseline))
    app_restarted["app"]["creationTime100ns"] += 1
    _refresh_runtime_generation_fingerprint(endpoint_probe, app_restarted)
    assert endpoint_probe.process_generation_matches(baseline, app_restarted) is False

    backend_restarted = json.loads(json.dumps(baseline))
    backend_restarted["backend"]["creationTime100ns"] += 1
    _refresh_runtime_generation_fingerprint(endpoint_probe, backend_restarted)
    assert (
        endpoint_probe.process_generation_matches(baseline, backend_restarted)
        is False
    )

    webview_lost = json.loads(json.dumps(baseline))
    webview_lost["webViewProcesses"] = []
    _refresh_runtime_generation_fingerprint(endpoint_probe, webview_lost)
    assert endpoint_probe.process_generation_matches(baseline, webview_lost) is False

    webview_replaced = json.loads(json.dumps(baseline))
    webview_replaced["webViewProcesses"][0]["creationTime100ns"] += 1
    _refresh_runtime_generation_fingerprint(endpoint_probe, webview_replaced)
    assert (
        endpoint_probe.process_generation_matches(baseline, webview_replaced)
        is False
    )


def test_process_generation_match_rejects_tampered_structured_snapshot() -> None:
    from benchmarks.windows import endpoint_probe

    baseline = _runtime_generation_snapshot(endpoint_probe)
    observed = json.loads(json.dumps(baseline))
    observed["frontendReadyReceivedAt"] = "2026-07-21T00:00:10Z"

    assert endpoint_probe.process_generation_matches(baseline, observed) is False


@pytest.mark.parametrize(
    ("metric", "value", "error_fragment"),
    [
        (
            "microsoft_local_5s_activation_received_to_final_text_observed_p50_ms",
            "0",
            "must be positive",
        ),
        (
            "microsoft_local_5s_activation_received_to_final_text_observed_sample_count",
            "0",
            "must be positive",
        ),
        (
            "soniox_local_60s_stop_requested_to_final_text_observed_failure_rate",
            "1.1",
            "between zero and one",
        ),
        (
            "soniox_local_30s_activation_received_to_final_text_observed_capture_attested",
            "0",
            "must equal one",
        ),
    ],
)
def test_benchmark_lint_rejects_invalid_canonical_series_guards(
    metric,
    value,
    error_fragment,
):
    output = "\n".join(
        f"METRIC {name}={value if name == metric else valid_lint_metric_value(name)}"
        for name in REQUIRED_METRICS
    )
    assert any(error_fragment in error for error in lint(output))


def test_benchmark_lint_rejects_unknown_without_override():
    output = "\n".join(f"METRIC {name}=unknown" for name in REQUIRED_METRICS)
    errors = lint(output)
    assert errors
    assert any("local_wux is unknown" in error for error in errors)
    assert lint(output, allow_unknown=True) == []


def test_local_wux_composite_uses_weighted_latency_ratios():
    baseline = {
        metric_name: float((index + 1) * 100)
        for index, scenario_metrics in enumerate(SCENARIO_METRICS.values())
        for metric_name in scenario_metrics.values()
    }
    candidate = {name: value * 0.8 for name, value in baseline.items()}
    assert compute_local_wux(candidate, baseline) == 0.8
    assert math.isclose(
        sum(compute_local_wux.__globals__["SCENARIO_WEIGHTS"].values()),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-9,
    )
    assert "hotkey_mic_ready_p95_ms" not in baseline
    assert "hotkey_first_audio_frame_p95_ms" not in baseline


def test_local_wux_composite_blocks_missing_baseline_values():
    assert compute_local_wux({"overlay_warm_p95_ms": 100.0}, {}) == "unknown"


def test_profile_script_writes_profile_json(monkeypatch):
    monkeypatch.delenv("SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3", raising=False)
    monkeypatch.delenv("SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV", raising=False)
    profile_path = REPO_ROOT / "tmp" / "autoresearch-test-profile.json"
    repeat_path = REPO_ROOT / "tmp" / "autoresearch-test-profile-repeat.json"

    def collect(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(REPO_ROOT / "benchmarks" / "windows" / "profile.ps1"),
                "-OutputPath",
                str(path),
                "-Python",
                sys.executable,
            ],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )

    result = collect(profile_path)
    assert result.returncode == 0, result.stderr
    repeat = collect(repeat_path)
    assert repeat.returncode == 0, repeat.stderr
    profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    repeated_profile = json.loads(repeat_path.read_text(encoding="utf-8-sig"))
    assert profile["schemaVersion"] == 1
    assert profile["profile_id"]
    assert repeated_profile["profile_id"] == profile["profile_id"]
    assert profile["scriberCommit"]
    assert profile["evaluatorHash"]
    doctor_evaluator_hash, missing_evaluator_files = doctor.current_evaluator_hash(REPO_ROOT)
    assert missing_evaluator_files == []
    assert profile["evaluatorHash"].casefold() == doctor_evaluator_hash.casefold()
    assert profile["scorerHash"]
    assert profile["baselineId"]
    assert profile["baselineSha256"]
    assert profile["expectedAppVersion"]
    assert "desktopProductVersion" in profile
    assert "audioSidecarProductVersion" in profile
    assert isinstance(profile["binaryVersionMatchesSource"], bool)
    assert isinstance(profile["runtimeAttestationValid"], bool)
    assert "buildAttestationId" in profile
    assert isinstance(profile["runtimeAttestationChecked"], bool)
    assert "runtimeAttestationExitCode" in profile
    assert "runtimeAttestationManifestSha256" in profile
    assert "runtimeAttestationSourceContentSha256" in profile
    assert profile["providerCandidate"] == {
        "azureMaiCaptureTimeMp3": "disabled",
        "speechmaticsCaptureTimeWav": "disabled",
    }
    assert repeated_profile["providerCandidate"] == profile["providerCandidate"]
    assert "installRoot" not in profile


def test_environment_profile_id_excludes_build_and_dynamic_run_evidence():
    source = (REPO_ROOT / "benchmarks" / "windows" / "profile.ps1").read_text(encoding="utf-8")
    battery_identity = source.split("$batteryIdentity =", 1)[1].split("$screens =", 1)[0]
    identity = source.split("$environmentIdentity = [ordered]@{", 1)[1].split(
        "$profileId = Get-JsonHash -Value $environmentIdentity", 1
    )[0]
    assert "estimatedChargeRemaining" not in battery_identity
    assert "generatedAtUtc" not in identity
    assert "scriberCommit" not in identity
    assert "buildAttestationId" not in identity
    assert "runtimeAttestation" not in identity
    assert "estimatedChargeRemaining" not in identity
    assert "evaluatorHash" not in identity
    assert "scorerHash" not in identity
    assert "baselineSha256" not in identity
    assert "providerCandidate" not in identity
    assert "batteryIdentity" in identity


def test_raw_packages_share_complete_provenance_and_endpoint_drift_guards():
    source = (REPO_ROOT / "scripts" / "perf" / "run.ps1").read_text(encoding="utf-8")
    required_fields = {
        "buildAttestationId",
        "baselineId",
        "baselineSha256",
        "evaluatorHash",
        "scorerHash",
        "runtimeAttestationId",
        "runtimeAttestationChecked",
        "runtimeAttestationExitCode",
        "runtimeAttestationManifestSha256",
        "runtimeAttestationSourceContentSha256",
        "providerCandidate",
        "runtimeAttestationPreChecked",
        "runtimeAttestationPreValid",
        "runtimeAttestationPreExitCode",
        "runtimeAttestationPreSourceContentSha256",
        "runtimeAttestationPreErrorCodes",
        "runtimeAttestationPostChecked",
        "runtimeAttestationPostValid",
        "runtimeAttestationPostExitCode",
        "runtimeAttestationPostSourceContentSha256",
        "runtimeAttestationPostErrorCodes",
        "runtimeAttestationDriftDetected",
        "desktopSha256",
        "backendSha256",
        "audioSidecarSha256",
    }
    provenance_block = source.split("$rawProvenance = [ordered]@{", 1)[1].split(
        "function Add-RawProvenance", 1
    )[0]
    for field in required_fields:
        assert field in provenance_block
    assert "function Write-RawPayload" in source
    assert source.count("Write-RawPayload -Payload") >= 8
    assert "Set-Content -LiteralPath $rawPath" not in source
    assert "Set-Content -LiteralPath $RawPath" not in source
    assert "Invoke-RuntimeAttestationVerification" in source
    assert "runtime_attestation_preflight_drift" in source
    assert "runtime_attestation_drift" in source
    assert "baseline_drift" in source
    assert "$endpointPostAttestation = Invoke-RuntimeAttestationVerification" in source
    assert "$endpointPostPayload.attestationId -eq [string]$endpointPrePayload.attestationId" in source
    assert "& $pythonExecutable (Join-Path $RepoRoot \"benchmarks\\windows\\endpoint_probe.py\")" in source


def _git(repo_root: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _runtime_fixture(tmp_path: Path):
    repo_root = tmp_path / "repo"
    install_root = tmp_path / "install"
    (repo_root / "Frontend").mkdir(parents=True)
    (repo_root / "Frontend" / "package.json").write_text('{"version":"1.2.3"}\n', encoding="utf-8")
    (repo_root / "source.txt").write_text("candidate source\n", encoding="utf-8")
    _git(repo_root, "init")
    _git(repo_root, "add", ".")
    _git(
        repo_root,
        "-c",
        "user.name=Scriber Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "fixture",
    )

    (install_root / "backend").mkdir(parents=True)
    (install_root / "scriber-desktop.exe").write_bytes(b"desktop-current")
    (install_root / "backend" / "scriber-backend.exe").write_bytes(b"backend-current")
    (install_root / "scriber-audio-sidecar.exe").write_bytes(b"audio-current")

    def version_reader(path: Path) -> str:
        if path.name in {"scriber-desktop.exe", "scriber-audio-sidecar.exe"}:
            return "1.2.3"
        return ""

    runtime_attestation.write_attestation(repo_root, install_root, version_reader=version_reader)
    return repo_root, install_root, version_reader


@pytest.mark.parametrize(
    "relative_path",
    [
        "scriber-desktop.exe",
        "backend/scriber-backend.exe",
        "scriber-audio-sidecar.exe",
    ],
)
def test_runtime_attestation_rejects_stale_same_version_component(tmp_path, relative_path):
    repo_root, install_root, version_reader = _runtime_fixture(tmp_path)
    component = install_root.joinpath(*relative_path.split("/"))
    component.write_bytes(component.read_bytes() + b"-stale")

    result = runtime_attestation.verify_attestation(
        repo_root,
        install_root,
        version_reader=version_reader,
    )

    assert result["ok"] is False
    assert any(error["code"] == "component_hash_mismatch" for error in result["errors"])


def test_runtime_attestation_rejects_source_change_after_write(tmp_path):
    repo_root, install_root, version_reader = _runtime_fixture(tmp_path)
    (repo_root / "source.txt").write_text("changed after build\n", encoding="utf-8")

    result = runtime_attestation.verify_attestation(
        repo_root,
        install_root,
        version_reader=version_reader,
    )

    assert result["ok"] is False
    assert any(error["code"] == "source_content_mismatch" for error in result["errors"])


def test_runtime_attestation_rejects_missing_audio_sidecar(tmp_path):
    repo_root, install_root, version_reader = _runtime_fixture(tmp_path)
    (install_root / "scriber-audio-sidecar.exe").unlink()

    result = runtime_attestation.verify_attestation(
        repo_root,
        install_root,
        version_reader=version_reader,
    )

    assert result["ok"] is False
    assert any(
        error["code"] == "missing_component" and error.get("component") == "audioSidecar"
        for error in result["errors"]
    )


def test_runtime_attestation_ignores_generated_benchmark_results(tmp_path):
    repo_root, install_root, version_reader = _runtime_fixture(tmp_path)
    generated = repo_root / "benchmarks" / "results" / "raw" / "measurement.json"
    generated.parent.mkdir(parents=True)
    generated.write_text('{"generated":true}\n', encoding="utf-8")

    result = runtime_attestation.verify_attestation(
        repo_root,
        install_root,
        version_reader=version_reader,
    )

    assert result["ok"] is True


def test_doctor_blocks_a_desktop_binary_from_an_older_source_version(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    install_root = tmp_path / "install"
    (repo_root / "Frontend").mkdir(parents=True)
    install_root.mkdir()
    (repo_root / "Frontend" / "package.json").write_text('{"version":"0.5.16"}', encoding="utf-8")
    (install_root / "scriber-desktop.exe").write_bytes(b"desktop")
    (install_root / "backend").mkdir()
    (install_root / "backend" / "scriber-backend.exe").write_bytes(b"backend")
    monkeypatch.setattr(doctor, "read_windows_file_version", lambda _path: "0.5.11")
    monkeypatch.setattr(doctor, "detect_foreign_scriber_instances", lambda *_args: [])

    findings = doctor.check_static(repo_root, install_root)

    mismatch = next(item for item in findings if item.get("code") == "binary_version_mismatch")
    assert mismatch["expectedVersion"] == "0.5.16"
    assert mismatch["actualVersion"] == "0.5.11"


def test_doctor_passes_explicit_install_root_to_fastlocal(monkeypatch, tmp_path):
    captured: list[str] = []
    metric_output = "\n".join(
        f"METRIC {name}={valid_lint_metric_value(name)}"
        for name in REQUIRED_METRICS
    )

    def fake_run_capture(args, _cwd, timeout=120):
        captured.extend(args)
        return SimpleNamespace(returncode=0, stdout=metric_output, stderr="")

    monkeypatch.setattr(doctor, "run_capture", fake_run_capture)
    monkeypatch.setattr(doctor, "verify_attestation", lambda *_args: {"ok": True, "errors": []})
    install_root = tmp_path / "chosen release"

    findings = doctor.check_benchmark(tmp_path, install_root)

    assert findings[0]["code"] == "benchmark_contract"
    index = captured.index("-InstallRoot")
    assert captured[index + 1] == str(install_root)


def test_doctor_process_inventory_uses_native_windows_api_not_inline_powershell():
    source = (REPO_ROOT / "scripts" / "perf" / "doctor.py").read_text(encoding="utf-8")
    assert "CreateToolhelp32Snapshot" in source
    assert "QueryFullProcessImageNameW" in source
    assert "Get-CimInstance Win32_Process" not in source
    assert '"-Command"' not in source


def test_fastlocal_staged_build_writes_runtime_attestation_after_build():
    build_script = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    assert 'Invoke-Checked -Label "FastLocal runtime attestation"' in build_script
    assert "scripts\\perf\\runtime_attestation.py write" in build_script
    assert "--install-root $targetRelease" in build_script
    assert 'if ($LASTEXITCODE -ne 0)' in build_script
    assert 'runtimeAttested = [bool]$runtimeAttestationPath' in build_script


def test_trace_collector_keeps_missing_endpoint_metrics_unknown(tmp_path):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"events": []}), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "benchmarks" / "windows" / "trace_collector.py"),
            "--input",
            str(trace),
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    assert "METRIC local_wux=unknown" in result.stdout
    assert "METRIC text_errors=unknown" in result.stdout


def test_trace_collector_outputs_finite_local_wux_for_complete_trace(tmp_path):
    baseline = tmp_path / "b7-baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "metrics": {
                    metric_name: 200_000.0
                    for scenario_metrics in SCENARIO_METRICS.values()
                    for metric_name in scenario_metrics.values()
                }
            }
        ),
        encoding="utf-8",
    )
    events = []
    freq = 10_000_000
    overlay_scenarios = {
        "overlay_warm": ("hotkey_received", "overlay_first_visible_frame"),
        "overlay_cold": ("hotkey_received", "overlay_first_visible_frame"),
    }
    for index, (scenario, (start, end)) in enumerate(overlay_scenarios.items(), start=1):
        events.append({"session_id": str(index), "scenario": scenario, "marker": start, "qpc_ticks": 100 * freq})
        events.append({"session_id": str(index), "scenario": scenario, "marker": "mic_ready", "qpc_ticks": int(100.2 * freq)})
        events.append({"session_id": str(index), "scenario": scenario, "marker": "first_audio_frame", "qpc_ticks": int(100.3 * freq)})
        events.append({"session_id": str(index), "scenario": scenario, "marker": end, "qpc_ticks": 101 * freq})
    session_index = len(overlay_scenarios)
    for provider in PROVIDER_REPLAY_WEIGHTS:
        for duration in PROVIDER_REPLAY_DURATION_SECONDS:
            session_index += 1
            scenario = f"{provider}_{duration}s"
            final_ticks = int((100 + duration + 1) * freq)
            events.extend(
                [
                    {
                        "session_id": str(session_index),
                        "scenario": scenario,
                        "marker": "activation_received",
                        "qpc_ticks": 100 * freq,
                    },
                    {
                        "session_id": str(session_index),
                        "scenario": scenario,
                        "marker": "stop_requested",
                        "qpc_ticks": int((100 + duration) * freq),
                    },
                    {
                        "session_id": str(session_index),
                        "scenario": scenario,
                        "marker": "final_text_observed",
                        "qpc_ticks": final_ticks,
                    },
                ]
            )
    trace = tmp_path / "trace.json"
    trace.write_text(
        json.dumps(
            {
                "events": events,
                "resourceMetrics": {"idle_cpu_pct": 1.5, "working_set_mb": 250.0},
                "evidence": {
                    "appFrame": {
                        "contract": "b7-app-ux-v1",
                        "metricEligible": True,
                        "externalStableFrameObserved": True,
                        "scenarioOrder": [
                            "cold_app_launch",
                            "warm_app_activation",
                            "open_transcript_detail",
                            "open_settings",
                            "stop_to_transcribing_visible",
                            "provider_result_to_completed_visible",
                            "session_finished_to_history_visible",
                            "switch_between_transcripts",
                            "return_to_dashboard",
                        ],
                        "requestedSamplesPerScenario": 1,
                        "scenarioResults": {
                            scenario: {
                                "metricEligible": True,
                                "sampleCount": 1,
                                "requiredSampleCount": 1,
                            }
                            for scenario in (
                                "cold_app_launch",
                                "warm_app_activation",
                                "open_transcript_detail",
                                "open_settings",
                                "stop_to_transcribing_visible",
                                "provider_result_to_completed_visible",
                                "session_finished_to_history_visible",
                                "switch_between_transcripts",
                                "return_to_dashboard",
                            )
                        },
                        "metrics": {
                            "app_ux_p50_ms": 500.0,
                            "app_ux_p95_ms": 1000.0,
                            "app_ux_sample_count": 9,
                        },
                        "resourceMetrics": {
                            "ui_long_tasks_gt_200ms": 0,
                            "idle_cpu_pct": 1.5,
                            "working_set_mb": 250.0,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "benchmarks" / "windows" / "trace_collector.py"),
            "--input",
            str(trace),
            "--qpc-frequency",
            str(freq),
            "--baseline",
            str(baseline),
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    metric_line = next(line for line in result.stdout.splitlines() if line.startswith("METRIC local_wux="))
    value = float(metric_line.split("=", 1)[1])
    assert math.isfinite(value)
    assert value != 1.0


def test_endpoint_probe_validate_only_keeps_baseline_unknown(tmp_path):
    output = tmp_path / "endpoint-probe.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "benchmarks" / "windows" / "endpoint_probe.py"),
            "--repo-root",
            str(REPO_ROOT),
            "--output",
            str(output),
            "--validate-only",
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "VALIDATE_ONLY"
    assert payload["metrics"]["local_wux"] == "unknown"
    assert payload["evidence"]["providerReplay"]["reason"] == "validate_only"


def test_endpoint_probe_uses_real_text_receiver_and_uia_observer():
    script = (REPO_ROOT / "benchmarks" / "windows" / "endpoint_probe.py").read_text(encoding="utf-8")
    observer = (REPO_ROOT / "benchmarks" / "windows" / "text_observer.ps1").read_text(encoding="utf-8")
    receiver = (REPO_ROOT / "benchmarks" / "windows" / "TextReceiver.ps1").read_text(encoding="utf-8")
    assert "TextReceiver.ps1" in script
    assert "text_observer.ps1" in script
    assert "set_receiver_text_direct" not in script
    assert "wm_settext" not in script.lower()
    assert "direct_text_receiver_wm_settext" not in script
    assert "inject_via_scriber_product_path" not in script
    assert "TextInjector" not in script
    assert "from src.injector" not in script
    assert "from src.config" not in script
    assert "Config." not in script
    assert "SetForegroundWindow" in script
    assert "clipboard_snapshot_ctrl_v_restore" not in script
    assert "_windows_clipboard_snapshot" not in script
    assert '"-Sta"' in script
    assert "SCRIBER_B7_PROVIDER_REPLAY_RUN_ID" in script
    assert "SCRIBER_TAURI_BENCHMARK_HOTKEY_RUN_ID" in script
    assert 'PROVIDER_REPLAY_ACTIVATION_KINDS = ("hotkey", "button")' in script
    assert '"live-mic-toggle-button"' in script
    assert 'action.pop("nativeWindowHandle", None)' in script
    assert "installed_backend_provider_replay" in script
    assert '"-KeepAppOpen"' in script
    assert "request_runtime_json" in script
    assert 'payload={' in script
    assert "provider_response_complete" in script
    assert "last_final_token_received" in script
    assert "installed_backend_provider_event" in script
    assert "installed_backend_injector_event" in script
    assert "injection_callback_completed" in script
    assert "final_text_observed" in script
    assert "wait_for_json_file" in script
    assert "text-observer-ready.json" in script
    assert '"-ReadyPath"' in script
    assert "observerReady" in script
    assert '"-PrefixSentinel"' not in script
    assert '"-SuffixSentinel"' not in script
    assert "target_text_observer_ready" in observer
    assert 'endpoint = "final_text_observed"' in observer
    assert "$observedQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()" in observer
    assert "qpcTicks = $observedQpcTicks" in observer
    assert "Write-ObserverReady" in observer
    assert "($hash -eq $ExpectedSha256.ToLowerInvariant()) -and $prefixOk -and $suffixOk" in observer
    assert "expectedTextSeen" not in observer
    assert "show_no_activate = 8" in script
    assert "ShowWindow(ctypes.c_void_p(hwnd), show_no_activate)" in script
    assert "WS_EX_NOACTIVATE" not in receiver
    assert "ShowWithoutActivation" not in receiver
    assert ".Activate()" in receiver
    assert ".Focus()" in receiver
    assert ".EDIT." in observer
    assert "NativeWindowHandle" in observer


def test_endpoint_probe_waits_through_transient_json_read_errors(monkeypatch, tmp_path):
    from benchmarks.windows import endpoint_probe

    calls = iter(
        [
            {"parseError": "[Errno 13] Permission denied: 'text-observer-ready.json'"},
            {"ok": True, "endpoint": "target_text_observer_ready"},
        ]
    )
    monkeypatch.setattr(endpoint_probe, "load_json", lambda _path: next(calls))
    monkeypatch.setattr(endpoint_probe.time, "sleep", lambda _seconds: None)

    assert endpoint_probe.wait_for_json_file(tmp_path / "ready.json", timeout_sec=1) == {
        "ok": True,
        "endpoint": "target_text_observer_ready",
    }


def test_runtime_json_request_sends_exact_authenticated_payload(monkeypatch):
    from benchmarks.windows import endpoint_probe

    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"ok":true}'

    def urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(endpoint_probe.urllib.request, "urlopen", urlopen)
    payload = {"schemaVersion": 1, "runId": "a" * 32, "provider": "microsoft"}

    result = endpoint_probe.request_runtime_json(
        43210,
        "session-token",
        "/api/runtime/benchmark/provider-replay/prepare",
        method="POST",
        payload=payload,
        timeout_sec=3.0,
    )

    request = captured["request"]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert result == {"ok": True}
    assert request.get_method() == "POST"
    assert json.loads(request.data.decode("utf-8")) == payload
    assert headers["x-scriber-token"] == "session-token"
    assert headers["content-type"] == "application/json"
    assert captured["timeout"] == 3.0


def test_provider_replay_validation_accepts_bound_installed_markers(tmp_path):
    from benchmarks.windows import endpoint_probe

    run_id = "1" * 32
    sample_id = "2" * 32
    session_id = "3" * 32
    process_fingerprint = "a" * 64
    target_fingerprint = "b" * 64
    fixture = "Backend-owned replay fixture."
    fixture_hash = endpoint_probe.sha256_text(fixture)
    visible_fixture = endpoint_probe.provider_replay_expected_visible_text(fixture)
    visible_fixture_hash = endpoint_probe.sha256_text(visible_fixture)
    fixture_pcm = b"\x01\x00" * 240_000
    fixture_pcm_path = tmp_path / "provider-replay-5s.pcm"
    fixture_pcm_path.write_bytes(fixture_pcm)
    zero_tail_frames = 0
    captured_pcm_sha256 = hashlib.sha256(
        fixture_pcm + (b"\x00\x00" * zero_tail_frames)
    ).hexdigest()
    audio_fixture = {
        "sha256": hashlib.sha256(fixture_pcm).hexdigest(),
        "durationMs": 5_000.0,
        "sampleRate": 48_000,
        "channels": 1,
        "sampleWidthBytes": 2,
        "frameCount": 240_000,
        "byteLength": 480_000,
        "path": str(fixture_pcm_path),
    }
    expected_audio_preparation = "post_stop_ffmpeg_mp3_v1"

    def response(
        state,
        *,
        session=None,
        target=None,
        activation_kind=None,
        markers=None,
    ):
        return {
            "contractVersion": 1,
            "runId": run_id,
            "sampleId": sample_id,
            "provider": "microsoft",
            "fixtureText": fixture,
            "fixtureTextSha256": fixture_hash,
            "fixtureTextLength": len(fixture),
            "authoritativeFixtureDurationMs": 5_000,
            "state": state,
            "expiresInMs": 30_000,
            "sessionId": session,
            "processGenerationFingerprint": process_fingerprint,
            "targetGenerationSha256": target,
            "activationKind": activation_kind,
            "errorCode": None,
            "audioPreparationImplementationExpected": (
                expected_audio_preparation
            ),
            "audioPreparationImplementationActual": (
                expected_audio_preparation if state == "completed" else None
            ),
            "markers": markers or [],
        }

    ticks = {
        "activation_received": 500,
        "hotkey_received": 500,
        "recording_state_visible": 700,
        "stop_requested": 800,
        "recording_state_transcribing_emitted": 900,
        "provider_response_complete": 1000,
        "clipboard_set": 1100,
        "paste": 1200,
        "injection_callback_completed": 1300,
        "session_finished_emitted": 1400,
    }
    sources = endpoint_probe.PROVIDER_REPLAY_MARKER_SOURCES
    markers = [
        {
            "ok": True,
            "apiVersion": 1,
            "runId": run_id,
            "sampleId": sample_id,
            "sessionId": session_id,
            "processGenerationFingerprint": process_fingerprint,
            "source": sources[name],
            "marker": name,
            "qpcTicks": tick,
            "qpcFrequency": 1000,
        }
        for name, tick in ticks.items()
    ]
    capture_attestation = {
        "contractVersion": 1,
        "source": "rust_audio_frame_pipe_reader",
        "runId": run_id,
        "sampleId": sample_id,
        "sessionId": session_id,
        "processGenerationFingerprint": process_fingerprint,
        "fixturePcmSha256": audio_fixture["sha256"],
        "capturedPcmSha256": captured_pcm_sha256,
        "sampleRate": audio_fixture["sampleRate"],
        "channels": audio_fixture["channels"],
        "sampleWidthBytes": audio_fixture["sampleWidthBytes"],
        "fixturePayloadBytesRead": audio_fixture["byteLength"],
        "fixtureAudioFramesRead": audio_fixture["frameCount"],
        "payloadBytesRead": audio_fixture["byteLength"] + zero_tail_frames * 2,
        "audioFramesRead": audio_fixture["frameCount"] + zero_tail_frames,
        "trailingZeroFrames": zero_tail_frames,
        "expectedTrailingZeroFrames": zero_tail_frames,
        "captureBlockSizeFrames": 480,
        "exactFixtureEndAccepted": True,
        "eosFramesRead": 1,
        "eosObserved": True,
        "sidecarEosWritten": True,
        "droppedFrameCount": 0,
        "sequenceErrorCount": 0,
        "protocolErrorCount": 0,
        "prebufferAfterLiveCount": 0,
        "readerEndReason": "endOfStream",
        "tailKind": "zero_pcm_s16le",
        "fixturePrefixMatched": True,
        "tailAllZero": True,
    }
    completed_payload = response(
        "completed",
        session=session_id,
        target=target_fingerprint,
        activation_kind="hotkey",
        markers=markers,
    )
    completed_payload["captureAttestation"] = capture_attestation
    observed_payload = {
        "ok": True,
        "endpoint": "final_text_observed",
        "expectedSha256": visible_fixture_hash,
        "observedSha256": visible_fixture_hash,
        "observedChars": len(visible_fixture),
        "qpcTicks": 1500,
        "qpcFrequency": 1000,
    }
    activation_action = {
        "schemaVersion": 1,
        "ok": True,
        "endpoint": "user_input_received",
        "source": "windows_send_input",
        "qpcTicks": 450,
        "qpcFrequency": 1000,
    }

    result = endpoint_probe.validate_provider_replay_sample(
        provider="microsoft",
        run_id=run_id,
        start_marker="provider_response_complete",
        activation_kind="hotkey",
        prepared=response("prepared"),
        armed=response(
            "activation_armed",
            target=target_fingerprint,
            activation_kind="hotkey",
        ),
        completed=completed_payload,
        observed=observed_payload,
        observer_ready={"ok": True, "endpoint": "target_text_observer_ready"},
        observer_exit_code=0,
        activation_action=activation_action,
        expected_process_generation_sha256=process_fingerprint,
        expected_target_generation_sha256=target_fingerprint,
        expected_fixture_duration_ms=5_000,
        expected_audio_fixture=audio_fixture,
        expected_audio_preparation_implementation=(
            expected_audio_preparation
        ),
    )

    assert result["ok"] is True
    assert result["reasons"] == []
    assert result["durationMs"] == 500.0
    assert result["activationReceivedToFinalTextObservedMs"] == 1000.0
    assert result["stopRequestedToFinalTextObservedMs"] == 700.0
    assert result["fixtureDurationAttested"] is True
    assert result["captureFixtureAttested"] is True
    assert result["fixtureTextSha256"] == fixture_hash
    assert result["expectedVisibleTextSha256"] == visible_fixture_hash
    assert result["expectedVisibleTextLength"] == len(visible_fixture)
    assert result["audioPreparationImplementationActual"] == (
        expected_audio_preparation
    )
    assert result["canonicalKpis"] == {
        "activation_received_to_final_text_observed_ms": 1000.0,
        "hotkey_received_to_final_text_observed_ms": 1000.0,
        "stop_requested_to_final_text_observed_ms": 700.0,
        "provider_final_received_to_final_text_observed_ms": 500.0,
        "stop_requested_to_provider_final_received_ms": 200.0,
    }

    raw_fixture_observation = dict(observed_payload)
    raw_fixture_observation.update(
        {
            "expectedSha256": fixture_hash,
            "observedSha256": fixture_hash,
            "observedChars": len(fixture),
        }
    )
    raw_fixture_result = endpoint_probe.validate_provider_replay_sample(
        provider="microsoft",
        run_id=run_id,
        start_marker="provider_response_complete",
        activation_kind="hotkey",
        prepared=response("prepared"),
        armed=response(
            "activation_armed",
            target=target_fingerprint,
            activation_kind="hotkey",
        ),
        completed=completed_payload,
        observed=raw_fixture_observation,
        observer_ready={"ok": True, "endpoint": "target_text_observer_ready"},
        observer_exit_code=0,
        activation_action=activation_action,
        expected_process_generation_sha256=process_fingerprint,
        expected_target_generation_sha256=target_fingerprint,
        expected_fixture_duration_ms=5_000,
        expected_audio_fixture=audio_fixture,
        expected_audio_preparation_implementation=expected_audio_preparation,
    )
    assert raw_fixture_result["ok"] is False
    assert "observer_expected_hash_mismatch" in raw_fixture_result["reasons"]
    assert "observer_hash_mismatch" in raw_fixture_result["reasons"]
    assert "observer_length_mismatch" in raw_fixture_result["reasons"]

    preparation_mismatch_payload = dict(completed_payload)
    preparation_mismatch_payload["audioPreparationImplementationActual"] = (
        "capture_time_ffmpeg_mp3_v1"
    )
    preparation_mismatch = endpoint_probe.validate_provider_replay_sample(
        provider="microsoft",
        run_id=run_id,
        start_marker="provider_response_complete",
        activation_kind="hotkey",
        prepared=response("prepared"),
        armed=response(
            "activation_armed",
            target=target_fingerprint,
            activation_kind="hotkey",
        ),
        completed=preparation_mismatch_payload,
        observed=observed_payload,
        observer_ready={"ok": True, "endpoint": "target_text_observer_ready"},
        observer_exit_code=0,
        activation_action=activation_action,
        expected_process_generation_sha256=process_fingerprint,
        expected_target_generation_sha256=target_fingerprint,
        expected_fixture_duration_ms=5_000,
        expected_audio_fixture=audio_fixture,
        expected_audio_preparation_implementation=(
            expected_audio_preparation
        ),
    )
    assert preparation_mismatch["ok"] is False
    assert "completed_audio_preparation_actual_mismatch" in (
        preparation_mismatch["reasons"]
    )

    missing_capture_payload = dict(completed_payload)
    missing_capture_payload.pop("captureAttestation")
    missing_capture = endpoint_probe.validate_provider_replay_sample(
        provider="microsoft",
        run_id=run_id,
        start_marker="provider_response_complete",
        activation_kind="hotkey",
        prepared=response("prepared"),
        armed=response(
            "activation_armed",
            target=target_fingerprint,
            activation_kind="hotkey",
        ),
        completed=missing_capture_payload,
        observed=observed_payload,
        observer_ready={"ok": True, "endpoint": "target_text_observer_ready"},
        observer_exit_code=0,
        activation_action=activation_action,
        expected_process_generation_sha256=process_fingerprint,
        expected_target_generation_sha256=target_fingerprint,
        expected_fixture_duration_ms=5_000,
        expected_audio_fixture=audio_fixture,
        expected_audio_preparation_implementation=(
            expected_audio_preparation
        ),
    )
    assert missing_capture["ok"] is False
    assert missing_capture["captureFixtureAttested"] is False
    assert "capture_attestation_missing" in missing_capture["reasons"]

    missing_activation_payload = response(
        "completed",
        session=session_id,
        target=target_fingerprint,
        activation_kind="hotkey",
        markers=[
            marker
            for marker in markers
            if marker["marker"] not in {"activation_received", "hotkey_received"}
        ],
    )
    missing_activation_payload["captureAttestation"] = capture_attestation
    missing_activation = endpoint_probe.validate_provider_replay_sample(
        provider="microsoft",
        run_id=run_id,
        start_marker="provider_response_complete",
        activation_kind="hotkey",
        prepared=response("prepared"),
        armed=response(
            "activation_armed",
            target=target_fingerprint,
            activation_kind="hotkey",
        ),
        completed=missing_activation_payload,
        observed=observed_payload,
        observer_ready={"ok": True, "endpoint": "target_text_observer_ready"},
        observer_exit_code=0,
        activation_action=activation_action,
        expected_process_generation_sha256=process_fingerprint,
        expected_target_generation_sha256=target_fingerprint,
        expected_fixture_duration_ms=5_000,
        expected_audio_fixture=audio_fixture,
        expected_audio_preparation_implementation=(
            expected_audio_preparation
        ),
    )
    assert missing_activation["ok"] is False
    assert "marker_missing:activation_received" in missing_activation["reasons"]
    assert "marker_missing:hotkey_received" in missing_activation["reasons"]
    assert missing_activation["activationReceivedToFinalTextObservedMs"] == "unknown"
    assert missing_activation["nativeActivationToFinalTextObservedMs"] == "unknown"

    next(
        marker
        for marker in markers
        if marker["marker"] == "provider_response_complete"
    )["source"] = "fabricated_harness_event"
    tampered_completed = response(
        "completed",
        session=session_id,
        target=target_fingerprint,
        activation_kind="hotkey",
        markers=markers,
    )
    tampered_completed["authoritativeFixtureDurationMs"] = 15_000
    tampered_capture_attestation = dict(capture_attestation)
    tampered_capture_attestation["capturedPcmSha256"] = "d" * 64
    tampered_completed["captureAttestation"] = tampered_capture_attestation
    tampered = endpoint_probe.validate_provider_replay_sample(
        provider="microsoft",
        run_id=run_id,
        start_marker="provider_response_complete",
        activation_kind="hotkey",
        prepared=response("prepared"),
        armed=response(
            "activation_armed",
            target=target_fingerprint,
            activation_kind="hotkey",
        ),
        completed=tampered_completed,
        observed=observed_payload,
        observer_ready={"ok": True, "endpoint": "target_text_observer_ready"},
        observer_exit_code=0,
        activation_action=activation_action,
        expected_process_generation_sha256=process_fingerprint,
        expected_target_generation_sha256=target_fingerprint,
        expected_fixture_duration_ms=5_000,
        expected_audio_fixture=audio_fixture,
        expected_audio_preparation_implementation=(
            expected_audio_preparation
        ),
    )
    assert tampered["ok"] is False
    assert "marker_source_invalid:provider_response_complete" in tampered["reasons"]
    assert "completed_fixture_duration_mismatch" in tampered["reasons"]
    assert (
        "capture_attestation_captured_pcm_sha256_mismatch" in tampered["reasons"]
    )
    assert tampered["fixtureDurationAttested"] is False
    assert tampered["captureFixtureAttested"] is False


def test_provider_replay_launches_once_and_cleans_up_per_installed_provider(
    monkeypatch,
    tmp_path,
):
    from benchmarks.windows import endpoint_probe

    monkeypatch.delenv("SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3", raising=False)
    monkeypatch.delenv("SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV", raising=False)

    launches = []
    cleanups = []

    def run_capture(args, cwd, timeout, *, env=None):
        launches.append({"args": args, "cwd": cwd, "timeout": timeout, "env": env})
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(endpoint_probe, "run_capture", run_capture)
    monkeypatch.setattr(endpoint_probe, "load_json", lambda _path: {})
    monkeypatch.setattr(
        endpoint_probe,
        "terminate_runtime",
        lambda app, backend, port, token: cleanups.append((app, backend, port, token))
        or {"ok": True, "appExited": True, "backendExited": True},
    )
    install_root = tmp_path / "installed"

    result = endpoint_probe.run_provider_text_replay(
        REPO_ROOT,
        install_root,
        tmp_path / "work",
        timeout_sec=5,
        iterations=2,
    )

    assert result["metricEligible"] is False
    assert result["providerCandidate"] == {
        "azureMaiCaptureTimeMp3": "disabled",
        "speechmaticsCaptureTimeWav": "disabled",
    }
    assert len(launches) == len(endpoint_probe.PROVIDER_REPLAY_SCENARIOS) == 3
    assert len(cleanups) == 3
    for launch in launches:
        args = launch["args"]
        assert args[args.index("-ExePath") + 1] == str(
            install_root / "scriber-desktop.exe"
        )
        assert "-KeepAppOpen" in args
        assert "-OccupyDefaultPort" in args
        assert "-VerifyFrontend" in args
        assert "-SessionToken" in args
        run_id = launch["env"]["SCRIBER_B7_PROVIDER_REPLAY_RUN_ID"]
        assert endpoint_probe._canonical_non_nil_uuid(run_id) == run_id
        assert launch["env"][
            "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH"
        ].endswith("provider-replay-fixture-s16le-48000-mono.pcm")
        assert len(
            launch["env"]["SCRIBER_B7_PROVIDER_REPLAY_FIXTURE_PCM_SHA256"]
        ) == 64
        assert launch["env"]["SCRIBER_SPEECHMATICS_BATCH_BASE_URL"] == (
            endpoint_probe.SPEECHMATICS_BATCH_DEFAULT_BASE_URL
        )
        assert launch["env"]["AZURE_MAI_SPEECH_KEY"] == ""
        assert launch["env"]["SCRIBER_AZURE_MAI_REGION"] == "northeurope"
        assert launch["env"]["SPEECHMATICS_API_KEY"] == ""


@pytest.mark.parametrize(
    "fixture",
    [
        "Scriber deterministic Microsoft provider replay.",
        "Scriber deterministic Soniox provider replay.",
        "Scriber deterministic Speechmatics provider replay.",
    ],
)
def test_provider_replay_visible_text_contract_matches_product_injector(fixture):
    from benchmarks.windows import endpoint_probe
    from src.injector import TextInjector

    injected: list[str] = []
    injector = TextInjector()
    injector._inject_text_safely = lambda text: injected.append(text) or True
    injector._buffer = [fixture]

    injector.flush()

    expected = endpoint_probe.provider_replay_expected_visible_text(fixture)
    assert injected == [expected]
    assert expected.endswith(" ")
    assert not expected.endswith("  ")


def test_provider_replay_button_activation_restores_bound_external_target(
    monkeypatch,
):
    from benchmarks.windows import endpoint_probe

    target_generation = "a" * 64
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        endpoint_probe,
        "focus_receiver_window",
        lambda title: calls.append(("focus", title))
        or {"ok": True, "targetHwndHash": "b" * 64},
    )
    monkeypatch.setattr(
        endpoint_probe,
        "_provider_replay_target_attestation",
        lambda **kwargs: calls.append(("attest", kwargs))
        or {
            "ok": True,
            "targetGenerationSha256": target_generation,
        },
    )

    result = endpoint_probe.ensure_provider_replay_target_focus_after_activation(
        activation_kind="button",
        title="Bound target",
        receiver_pid=123,
        expected_target_generation_sha256=target_generation,
    )

    assert result["ok"] is True
    assert result["required"] is True
    assert result["targetGenerationMatches"] is True
    assert calls == [
        ("focus", "Bound target"),
        ("attest", {"title": "Bound target", "receiver_pid": 123}),
    ]


def test_provider_replay_hotkey_preserves_target_without_focus_action(monkeypatch):
    from benchmarks.windows import endpoint_probe

    monkeypatch.setattr(
        endpoint_probe,
        "focus_receiver_window",
        lambda *_args, **_kwargs: pytest.fail("hotkey refocused the target"),
    )
    monkeypatch.setattr(
        endpoint_probe,
        "_provider_replay_target_attestation",
        lambda *_args, **_kwargs: pytest.fail("hotkey re-attested the target"),
    )

    result = endpoint_probe.ensure_provider_replay_target_focus_after_activation(
        activation_kind="hotkey",
        title="Bound target",
        receiver_pid=123,
        expected_target_generation_sha256="a" * 64,
    )

    assert result == {
        "ok": True,
        "required": False,
        "method": "foreground_preserved_by_hotkey",
    }


def test_provider_replay_button_refocus_rejects_replaced_target(monkeypatch):
    from benchmarks.windows import endpoint_probe

    monkeypatch.setattr(
        endpoint_probe,
        "focus_receiver_window",
        lambda _title: {"ok": True},
    )
    monkeypatch.setattr(
        endpoint_probe,
        "_provider_replay_target_attestation",
        lambda **_kwargs: {
            "ok": True,
            "targetGenerationSha256": "b" * 64,
        },
    )

    result = endpoint_probe.ensure_provider_replay_target_focus_after_activation(
        activation_kind="button",
        title="Bound target",
        receiver_pid=123,
        expected_target_generation_sha256="a" * 64,
    )

    assert result["ok"] is False
    assert result["targetGenerationMatches"] is False


def test_provider_replay_stage_zero_distribution_includes_failures_and_variance():
    from benchmarks.windows import endpoint_probe

    summary = endpoint_probe.summarize_stage_zero_distribution(
        [100.0, 200.0, 300.0],
        attempted=4,
    )

    assert summary == {
        "count": 3,
        "p50Ms": 200.0,
        "p90Ms": 300.0,
        "p95Ms": 300.0,
        "maxMs": 300.0,
        "varianceMs2": 6666.666667,
        "failureRate": 0.25,
    }

    invalid = endpoint_probe.summarize_stage_zero_distribution(
        [-1.0, float("nan"), True, 10.0],
        attempted=4,
    )
    assert invalid["count"] == 1
    assert invalid["p50Ms"] == 10.0
    assert invalid["failureRate"] == 0.75


def test_provider_replay_audio_fixture_is_byte_identical_and_duration_bound(tmp_path):
    from benchmarks.windows import endpoint_probe

    first_path = tmp_path / "first.pcm"
    second_path = tmp_path / "second.pcm"
    first = endpoint_probe.write_provider_replay_audio_fixture(first_path)
    second = endpoint_probe.write_provider_replay_audio_fixture(second_path)

    assert first == second
    assert first_path.read_bytes() == second_path.read_bytes()
    assert first["durationMs"] == 350.0
    assert first["frameCount"] == 16_800
    assert endpoint_probe.attest_provider_replay_audio_fixture(
        first_path,
        first,
        expected_duration_ms=350.0,
    ) is True
    first_path.write_bytes(first_path.read_bytes()[:-2])
    assert endpoint_probe.attest_provider_replay_audio_fixture(
        first_path,
        first,
        expected_duration_ms=350.0,
    ) is False


def test_provider_replay_full_suite_declares_required_duration_matrix(tmp_path):
    from benchmarks.windows import endpoint_probe

    assert tuple(
        endpoint_probe.SAMPLE_PLANS["FullLocal"][
            "providerReplayDurationsSeconds"
        ]
    ) == endpoint_probe.PROVIDER_REPLAY_REQUIRED_DURATION_SECONDS
    assert tuple(
        endpoint_probe.SAMPLE_PLANS["FastLocal"][
            "providerReplayDurationsSeconds"
        ]
    ) == endpoint_probe.PROVIDER_REPLAY_REQUIRED_DURATION_SECONDS
    assert endpoint_probe.SAMPLE_PLANS["ProviderReplay"] == {
        "providerReplay": 5,
        "providerReplayDurationsSeconds": (5, 15, 30, 60),
    }
    fixture = endpoint_probe.write_provider_replay_audio_fixture(
        tmp_path / "five-seconds.pcm",
        duration_ms=5_000,
    )
    assert fixture["durationMs"] == 5_000.0
    assert fixture["frameCount"] == 240_000
    assert fixture["byteLength"] == 480_000


def test_provider_replay_suite_skips_ui_probes_and_has_scoped_success_contract(
    monkeypatch,
    tmp_path,
):
    from benchmarks.windows import endpoint_probe

    provider_metrics = {}
    for scenario in endpoint_probe.PROVIDER_REPLAY_SCENARIO_WEIGHTS:
        provider_metrics.update(
            {
                f"{scenario}_p50_ms": 10.0,
                f"{scenario}_p95_ms": 20.0,
                f"{scenario}_failure_rate": 0.0,
                f"{scenario}_sample_count": 5,
                f"{scenario}_capture_attested": 1,
            }
        )
    replay_call = {}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ProviderReplay must not call an overlay or App UX probe")

    def run_provider_replay(
        _repo_root,
        _install_root,
        _output_dir,
        _timeout_sec,
        iterations,
        *,
        fixture_durations_ms,
    ):
        replay_call["iterations"] = iterations
        replay_call["fixtureDurationsMs"] = fixture_durations_ms
        return {
            "attempted": True,
            "metricEligible": True,
            "ok": True,
            "reason": "measured",
            "textErrors": 0,
            "focusErrors": 0,
            "clipboardErrors": 0,
            "metrics": provider_metrics,
        }

    monkeypatch.setattr(endpoint_probe, "run_overlay_hotkey_probes", forbidden)
    monkeypatch.setattr(endpoint_probe, "run_app_frame_probes", forbidden)
    monkeypatch.setattr(endpoint_probe, "load_and_validate_app_ux_evidence", forbidden)
    monkeypatch.setattr(endpoint_probe, "run_provider_text_replay", run_provider_replay)
    monkeypatch.setattr(endpoint_probe, "load_baseline_metrics", forbidden)
    monkeypatch.setattr(endpoint_probe, "qpc_frequency", lambda: 1_000_000)

    output_path = tmp_path / "provider-replay.json"
    exit_code = endpoint_probe.main(
        [
            "--repo-root",
            str(tmp_path / "repo"),
            "--install-root",
            str(tmp_path / "install"),
            "--output",
            str(output_path),
            "--work-dir",
            str(tmp_path / "work"),
            "--suite",
            "ProviderReplay",
            "--app-ux-evidence",
            str(tmp_path / "must-not-be-read.json"),
        ]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert replay_call == {
        "iterations": 5,
        "fixtureDurationsMs": (5_000, 15_000, 30_000, 60_000),
    }
    assert payload["status"] == "PROVIDER_REPLAY_MEASURED"
    assert (
        payload["reason"]
        == "installed_provider_replay_measured_not_general_promotion_gate"
    )
    assert payload["scope"] == "installed_provider_replay_only"
    assert payload["promotionEligible"] is False
    assert payload["promotionEvaluation"] == {
        "evaluated": False,
        "reason": "provider_replay_suite_not_general_promotion_gate",
        "canonicalProviderReplayEvidenceValid": True,
        "providerDurationPoolingAllowed": False,
        "localWuxEvaluated": False,
    }
    assert payload["metrics"]["local_wux"] == "unknown"
    assert payload["metrics"]["overlay_errors"] == "unknown"
    assert payload["evidence"]["overlayHotkey"] == {
        "attempted": False,
        "metricEligible": False,
        "reason": "excluded_by_provider_replay_suite",
    }
    assert payload["evidence"]["appFrame"] == {
        "attempted": False,
        "metricEligible": False,
        "reason": "excluded_by_provider_replay_suite",
    }


def test_provider_replay_suite_fails_closed_with_clean_status_and_exit(
    monkeypatch,
    tmp_path,
):
    from benchmarks.windows import endpoint_probe

    monkeypatch.setattr(
        endpoint_probe,
        "run_overlay_hotkey_probes",
        lambda *_args, **_kwargs: pytest.fail("overlay probe was called"),
    )
    monkeypatch.setattr(
        endpoint_probe,
        "run_app_frame_probes",
        lambda *_args, **_kwargs: pytest.fail("App UX probe was called"),
    )
    monkeypatch.setattr(
        endpoint_probe,
        "run_provider_text_replay",
        lambda *_args, **_kwargs: {
            "attempted": True,
            "metricEligible": False,
            "ok": False,
            "reason": "installed_provider_replay_failed_closed",
            "metrics": {},
        },
    )
    monkeypatch.setattr(endpoint_probe, "qpc_frequency", lambda: 1_000_000)

    output_path = tmp_path / "provider-replay-failed.json"
    exit_code = endpoint_probe.main(
        [
            "--repo-root",
            str(tmp_path / "repo"),
            "--output",
            str(output_path),
            "--suite",
            "ProviderReplay",
        ]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == "installed_provider_replay_failed_closed"
    assert payload["scope"] == "installed_provider_replay_only"
    assert payload["promotionEligible"] is False
    assert payload["metrics"]["local_wux"] == "unknown"


def test_provider_replay_powershell_suite_keeps_attestation_and_cli_overrides():
    source = (REPO_ROOT / "scripts" / "perf" / "run.ps1").read_text(
        encoding="utf-8"
    )

    assert '"FullLocal", "ProviderReplay", "LiveMicrosoft"' in source
    assert source.index("$importedEnvNames = @(Import-DotEnvIntoProcess") < source.index(
        'if ($AzureMaiCaptureTimeMp3 -ne "Default")'
    )
    assert source.index("$endpointPreAttestation = Invoke-RuntimeAttestationVerification") < source.index(
        'if ($Suite -eq "ProviderReplay") {'
    )
    assert source.index("$endpointPostAttestation = Invoke-RuntimeAttestationVerification") < source.rindex(
        'if ($Suite -eq "ProviderReplay") {'
    )
    assert '[string]$endpointProbe.status -eq "PROVIDER_REPLAY_MEASURED"' in source
    assert '-not [bool]$endpointProbe.promotionEligible' in source
    assert '$localWux -eq "unknown"' in source


def test_provider_replay_expands_each_provider_across_requested_durations(
    monkeypatch,
    tmp_path,
):
    from benchmarks.windows import endpoint_probe

    launches = []

    def run_capture(args, cwd, timeout, *, env=None):
        launches.append(dict(env or {}))
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(endpoint_probe, "run_capture", run_capture)
    monkeypatch.setattr(endpoint_probe, "load_json", lambda _path: {})
    monkeypatch.setattr(
        endpoint_probe,
        "terminate_runtime",
        lambda *_args: {"ok": True, "appExited": True, "backendExited": True},
    )

    result = endpoint_probe.run_provider_text_replay(
        REPO_ROOT,
        tmp_path / "installed",
        tmp_path / "matrix",
        timeout_sec=5,
        iterations=1,
        fixture_durations_ms=(5_000, 15_000),
    )

    assert result["metricEligible"] is False
    assert len(launches) == len(endpoint_probe.PROVIDER_REPLAY_SCENARIOS) * 2
    assert {
        launch["SCRIBER_B7_PROVIDER_REPLAY_FIXTURE_DURATION_MS"]
        for launch in launches
    } == {"5000", "15000"}
    assert [item["durationMs"] for item in result["audioFixtures"]] == [
        5_000.0,
        15_000.0,
    ]
    assert set(result["stageZeroDistributions"]) == {
        "microsoft_local_5s",
        "microsoft_local_15s",
        "soniox_local_5s",
        "soniox_local_15s",
        "speechmatics_local_5s",
        "speechmatics_local_15s",
    }
    assert "activation_received_to_final_text_observed_p50_ms" not in result[
        "metrics"
    ]
    assert "stop_requested_to_final_text_observed_p95_ms" not in result[
        "metrics"
    ]
    assert all(
        series["captureFixtureAttested"] is False
        and series["kpis"]["non_speech_overhead"]["p50Ms"] == "unknown"
        for series in result["stageZeroDistributions"].values()
    )
    assert canonical_provider_replay_promotion_eligible(result["metrics"], {}) is False


@pytest.mark.parametrize("duration_ms", [180_000, 600_000, 10_000, -1])
def test_provider_replay_rejects_durations_outside_short_issue18_matrix(
    tmp_path,
    duration_ms,
):
    from benchmarks.windows import endpoint_probe

    with pytest.raises(ValueError, match="duration matrix is invalid"):
        endpoint_probe.run_provider_text_replay(
            REPO_ROOT,
            tmp_path / "installed",
            tmp_path / "matrix",
            timeout_sec=5,
            iterations=1,
            fixture_durations_ms=(duration_ms,),
        )


def test_live_provider_baseline_output_stays_under_tmp():
    script = (REPO_ROOT / "scripts" / "perf" / "run.ps1").read_text(encoding="utf-8")
    assert '$liveWorkDir = Join-Path $RepoRoot "tmp\\autoresearch-live-provider"' in script
    assert '$benchmarkPath = Join-Path $liveWorkDir' in script
    assert '"-OutputPath", $benchmarkPath' in script
    assert '$ok = $providerTranscriptStatus -eq "measured" -and $textTargetStatus -eq "measured"' in script
    assert '$ok = $providerTranscriptStatus -eq "measured" -and $textTargetStatus -eq "measured" -and $focusErrors -eq 0' in script
    assert '$ok = $benchmarkExit -eq 0 -and $providerTranscriptStatus' not in script
    assert '$Reason -like "*measured*"' in script
    assert "function Get-TextTargetFocusErrors" in script
    assert 'Get-TextTargetFocusErrors -Report $report' in script
    assert "live_text_target_focus_unverified" in script
    assert "textTargetFocusErrors" in script
    assert 'focus_errors = $(if ($textTargetStatus -eq "measured" -and $focusErrors -eq 0) { 0 } else { 1 })' in script
    assert "SCRIBER_INJECT_TARGET_TITLE" in (
        REPO_ROOT / "scripts" / "measure_hybrid_baseline.ps1"
    ).read_text(encoding="utf-8")
    assert '"--text-target-title", $RecordingHotPathTextTargetTitle' in (
        REPO_ROOT / "scripts" / "measure_hybrid_baseline.ps1"
    ).read_text(encoding="utf-8")
    assert (
        '$benchmarkPath = Join-Path $OutputDir "$($SuiteName.ToLowerInvariant())-provider-hot-path-$Stamp.json"'
        not in script
    )
    assert "$existingScriberProcesses = @(Get-ScriberProcesses)" in script
    assert "preexisting_scriber_instance" in script
    assert "preexistingProcesses = @($existingScriberProcesses)" in script


def test_endpoint_probe_overlay_uses_microsoft_and_soniox_not_local_stt():
    script = (REPO_ROOT / "benchmarks" / "windows" / "endpoint_probe.py").read_text(encoding="utf-8")
    assert '"defaultStt": "azure_mai"' in script
    assert '"defaultStt": "soniox"' in script
    assert "AZURE_MAI_SPEECH_KEY" in script
    assert "SONIOX_API_KEY" in script
    assert "onnx_local" not in script
    assert "nemo_local" not in script


def test_endpoint_probe_can_emit_metric_eligible_overlay_app_and_resource_metrics():
    script = (REPO_ROOT / "benchmarks" / "windows" / "endpoint_probe.py").read_text(encoding="utf-8")
    desktop = (REPO_ROOT / "scripts" / "smoke_tauri_desktop.ps1").read_text(encoding="utf-8")
    app_observer = (REPO_ROOT / "benchmarks" / "windows" / "app_observer.ps1").read_text(encoding="utf-8")
    run_script = (REPO_ROOT / "scripts" / "perf" / "run.ps1").read_text(encoding="utf-8")

    assert "dispatchStartQpcTicks" in desktop
    assert "triggerQpcTicks" in desktop
    assert "[string]$StartAfterPath" in app_observer
    assert "startGateObserved" in app_observer
    assert "firstNonEmptyQpcTicks" in app_observer
    assert "stableQpcTicks" in app_observer
    assert "sampleCount" in app_observer
    assert "lastTextSha256" in app_observer
    assert "try {" in app_observer
    assert 'shell_menu_trigger_path = probe_dir / "data" / "shell-menu-smoke.trigger"' in script
    assert '"-StartAfterPath"' in script
    assert '"observerStartGateObserved"' in script
    assert '"observerFirstNonEmptyQpcTicks"' in script
    assert '"observerSampleCount"' in script
    assert '"observerFirstTextTraversalMs"' in script
    assert '"observerMaxTextTraversalMs"' in script
    assert '"observerStableConfirmedQpcTicks"' in script
    assert '"observerStableConfirmedToOutputMs"' in script
    assert '"observerStartGateToFirstNonEmptyMs"' in script
    assert '"observerFirstNonEmptyToStableMs"' in script
    assert '"internalShowWindowCompleteToStableApproxMs"' in script
    assert '"-ShellMenuSmokeActionDelayMs"' not in script
    assert '"marker": "hotkey_received"' in script
    assert '"marker": "overlay_first_visible_frame"' in script
    assert '"sameProcessWarmContract": True' in script
    assert '"sameProcessAsCold": cycle > 0' in script
    assert '"micAlwaysOn": False' in script
    assert '"-LiveRecordingMicAlwaysOn"' not in script
    assert "send_global_hotkey_chord()" in script
    assert '"FullLocal": {' in script
    assert '"overlayWarm": 30' in script
    assert '"overlayCold": 15' in script
    assert '"hotkey_mic_ready_p95_ms"' in script
    assert '"hotkey_first_audio_frame_p95_ms"' in script
    assert '"hotkeyToMicReadyMs"' in script
    assert '"hotkeyToFirstAudioFrameMs"' in script
    assert "USER_READY_METRICS" in script
    assert "includeActive=1" in desktop
    assert "function Wait-GlobalHotkeyUserReady" in desktop
    assert "userReady = $userReady" in desktop
    assert "hotPathMetrics = if ($userReady)" in desktop
    assert '"marker": "user_input_received"' in script
    assert '"marker": "first_stable_visible_frame"' in script
    assert '"idle_cpu_pct"' in script
    assert '"working_set_mb"' in script
    assert '"ui_long_tasks_gt_200ms"' in script
    assert '"hotkey_mic_ready_p95_ms"' in run_script
    assert '"hotkey_first_audio_frame_p95_ms"' in run_script
    assert 'statusWord = if ($Reason -eq "measured" -or $Reason -like "*measured*")' in run_script
    assert '[string]$endpointProbe.status -eq "MEASURED"' in run_script


def test_fastlocal_imports_repo_dotenv_without_logging_values():
    script = (REPO_ROOT / "scripts" / "perf" / "run.ps1").read_text(encoding="utf-8")
    assert "Import-DotEnvIntoProcess" in script
    assert "[Environment]::SetEnvironmentVariable($name, $value, \"Process\")" in script
    assert "importedEnvNames" in script


def test_fastlocal_candidate_override_is_applied_after_dotenv_import():
    script = (REPO_ROOT / "scripts" / "perf" / "run.ps1").read_text(
        encoding="utf-8"
    )

    import_offset = script.index(
        "$importedEnvNames = @(Import-DotEnvIntoProcess -Path $repoEnvPath)"
    )
    override_offset = script.index(
        '"SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3"', import_offset
    )
    speechmatics_override_offset = script.index(
        '"SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV"', override_offset
    )
    profile_offset = script.index(
        "benchmarks\\windows\\profile.ps1", speechmatics_override_offset
    )

    assert override_offset > import_offset
    assert speechmatics_override_offset > override_offset
    assert profile_offset > speechmatics_override_offset
    assert '[ValidateSet("Default", "Enabled", "Disabled")]' in script


def test_fastlocal_and_doctor_prefer_current_release_build_when_available():
    run_script = (REPO_ROOT / "scripts" / "perf" / "run.ps1").read_text(encoding="utf-8")
    doctor = (REPO_ROOT / "scripts" / "perf" / "doctor.py").read_text(encoding="utf-8")
    assert 'Frontend\\src-tauri\\target\\release' in run_script
    assert "release_root = repo_root / \"Frontend\" / \"src-tauri\" / \"target\" / \"release\"" in doctor
    assert "default_install_root(repo_root)" in doctor
    assert '"-InstallRoot"' in doctor
    assert "binary_version_mismatch" in run_script


def test_recommend_next_ignores_instrumentation_only_keeps_as_product_champions():
    state_helper = (REPO_ROOT / "scripts" / "perf" / "autoresearch_state.py").read_text(encoding="utf-8")
    assert "def is_instrumentation_only_keep" in state_helper
    assert 'str(asi.get("lane", "")).lower() in {"instrument", "instrumentation"}' in state_helper
    assert "and not is_instrumentation_only_keep(row)" in state_helper


def test_finalize_preview_separates_baseline_acceptance_from_product_grade_readiness():
    state_helper = (REPO_ROOT / "scripts" / "perf" / "autoresearch_state.py").read_text(encoding="utf-8")
    assert "def baseline_accepted" in state_helper
    assert '"baselineAccepted": baseline_ok' in state_helper
    assert 'blocker or ("" if baseline_ok else "No local baseline package has been accepted yet.")' in state_helper
    assert '"productGradeAllowed": False' in state_helper


def test_live_provider_suites_require_credentials_and_real_hot_path():
    script = (REPO_ROOT / "scripts" / "perf" / "run.ps1").read_text(encoding="utf-8")
    assert "LiveMicrosoft" in script
    assert "LiveSoniox" in script
    assert "AZURE_MAI_SPEECH_KEY" in script
    assert "SONIOX_API_KEY" in script
    assert "scripts\\measure_hybrid_baseline.ps1" in script
    assert "-RequireRecordingHotPathProviderTranscript" in script
    assert "-RequireRecordingHotPathTextTarget" in script
    assert "baselineEligible = $false" in script
