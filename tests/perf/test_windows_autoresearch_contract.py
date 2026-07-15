from __future__ import annotations

import json
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
from scripts.perf.evaluator.local_wux import compute_local_wux

PERF_ROOT = REPO_ROOT / "scripts" / "perf"
if str(PERF_ROOT) not in sys.path:
    sys.path.insert(0, str(PERF_ROOT))
from scripts.perf import doctor
from scripts.perf import runtime_attestation


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
        f"METRIC {name}={1 if name == 'local_wux' else 0}"
        for name in REQUIRED_METRICS
    )
    assert lint(output) == []


def test_benchmark_lint_rejects_unknown_without_override():
    output = "\n".join(f"METRIC {name}=unknown" for name in REQUIRED_METRICS)
    errors = lint(output)
    assert errors
    assert any("local_wux is unknown" in error for error in errors)
    assert lint(output, allow_unknown=True) == []


def test_local_wux_composite_uses_weighted_latency_ratios():
    scenarios = (
        "overlay_warm",
        "overlay_cold",
        "microsoft_local_tail",
        "soniox_local_tail",
        "app_ux",
    )
    baseline = {
        f"{scenario}_{percentile}_ms": float((index + 1) * 100)
        for index, scenario in enumerate(scenarios)
        for percentile in ("p50", "p95")
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


def test_profile_script_writes_profile_json():
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
        f"METRIC {name}={1 if name == 'local_wux' else 0}"
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
                    f"{scenario}_{percentile}_ms": 2000.0
                    for scenario in (
                        "overlay_warm",
                        "overlay_cold",
                        "microsoft_local_tail",
                        "soniox_local_tail",
                        "app_ux",
                    )
                    for percentile in ("p50", "p95")
                }
            }
        ),
        encoding="utf-8",
    )
    events = []
    freq = 10_000_000
    scenarios = {
        "overlay_warm": ("hotkey_received", "overlay_first_visible_frame"),
        "overlay_cold": ("hotkey_received", "overlay_first_visible_frame"),
        "microsoft_local": ("provider_response_complete", "target_text_observed"),
        "soniox_local": ("last_final_token_received", "target_text_observed"),
    }
    for index, (scenario, (start, end)) in enumerate(scenarios.items(), start=1):
        events.append({"session_id": str(index), "scenario": scenario, "marker": start, "qpc_ticks": 100 * freq})
        if scenario.startswith("overlay_"):
            events.append({"session_id": str(index), "scenario": scenario, "marker": "mic_ready", "qpc_ticks": int(100.2 * freq)})
            events.append({"session_id": str(index), "scenario": scenario, "marker": "first_audio_frame", "qpc_ticks": int(100.3 * freq)})
        events.append({"session_id": str(index), "scenario": scenario, "marker": end, "qpc_ticks": 101 * freq})
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
    assert "installed_backend_provider_replay" in script
    assert '"-KeepAppOpen"' in script
    assert "request_runtime_json" in script
    assert 'payload={' in script
    assert "provider_response_complete" in script
    assert "last_final_token_received" in script
    assert "installed_backend_provider_event" in script
    assert "installed_backend_injector_event" in script
    assert "injection_callback_completed" in script
    assert "target_text_observed" in script
    assert "wait_for_json_file" in script
    assert "text-observer-ready.json" in script
    assert '"-ReadyPath"' in script
    assert "observerReady" in script
    assert '"-PrefixSentinel"' not in script
    assert '"-SuffixSentinel"' not in script
    assert "target_text_observer_ready" in observer
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


def test_provider_replay_validation_accepts_bound_installed_markers():
    from benchmarks.windows import endpoint_probe

    run_id = "1" * 32
    sample_id = "2" * 32
    session_id = "3" * 32
    process_fingerprint = "a" * 64
    target_fingerprint = "b" * 64
    fixture = "Backend-owned replay fixture."
    fixture_hash = endpoint_probe.sha256_text(fixture)

    def response(state, *, session=None, target=None, markers=None):
        return {
            "contractVersion": 1,
            "runId": run_id,
            "sampleId": sample_id,
            "provider": "microsoft",
            "fixtureText": fixture,
            "fixtureTextSha256": fixture_hash,
            "fixtureTextLength": len(fixture),
            "state": state,
            "expiresInMs": 30_000,
            "sessionId": session,
            "processGenerationFingerprint": process_fingerprint,
            "targetGenerationSha256": target,
            "errorCode": None,
            "markers": markers or [],
        }

    ticks = {
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

    result = endpoint_probe.validate_provider_replay_sample(
        provider="microsoft",
        run_id=run_id,
        start_marker="provider_response_complete",
        prepared=response("prepared"),
        armed=response("armed", session=session_id, target=target_fingerprint),
        completed=response(
            "completed",
            session=session_id,
            target=target_fingerprint,
            markers=markers,
        ),
        observed={
            "ok": True,
            "endpoint": "target_text_observed",
            "expectedSha256": fixture_hash,
            "observedSha256": fixture_hash,
            "observedChars": len(fixture),
            "qpcTicks": 1500,
            "qpcFrequency": 1000,
        },
        observer_ready={"ok": True, "endpoint": "target_text_observer_ready"},
        observer_exit_code=0,
        expected_process_generation_sha256=process_fingerprint,
        expected_target_generation_sha256=target_fingerprint,
    )

    assert result["ok"] is True
    assert result["reasons"] == []
    assert result["durationMs"] == 500.0

    markers[1]["source"] = "fabricated_harness_event"
    tampered = endpoint_probe.validate_provider_replay_sample(
        provider="microsoft",
        run_id=run_id,
        start_marker="provider_response_complete",
        prepared=response("prepared"),
        armed=response("armed", session=session_id, target=target_fingerprint),
        completed=response(
            "completed",
            session=session_id,
            target=target_fingerprint,
            markers=markers,
        ),
        observed={
            "ok": True,
            "endpoint": "target_text_observed",
            "expectedSha256": fixture_hash,
            "observedSha256": fixture_hash,
            "observedChars": len(fixture),
            "qpcTicks": 1500,
            "qpcFrequency": 1000,
        },
        observer_ready={"ok": True, "endpoint": "target_text_observer_ready"},
        observer_exit_code=0,
        expected_process_generation_sha256=process_fingerprint,
        expected_target_generation_sha256=target_fingerprint,
    )
    assert tampered["ok"] is False
    assert "marker_source_invalid:provider_response_complete" in tampered["reasons"]


def test_provider_replay_launches_once_and_cleans_up_per_installed_provider(
    monkeypatch,
    tmp_path,
):
    from benchmarks.windows import endpoint_probe

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
    assert len(launches) == len(endpoint_probe.PROVIDER_REPLAY_SCENARIOS) == 2
    assert len(cleanups) == 2
    for launch in launches:
        args = launch["args"]
        assert args[args.index("-ExePath") + 1] == str(
            install_root / "scriber-desktop.exe"
        )
        assert "-KeepAppOpen" in args
        assert "-OccupyDefaultPort" in args
        assert "-SessionToken" in args
        run_id = launch["env"]["SCRIBER_B7_PROVIDER_REPLAY_RUN_ID"]
        assert endpoint_probe._canonical_non_nil_uuid(run_id) == run_id


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
