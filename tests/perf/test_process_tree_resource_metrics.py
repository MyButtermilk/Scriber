from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _powershell_function(source: str, name: str, next_name: str) -> str:
    return source.split(f"function {name}", 1)[1].split(f"function {next_name}", 1)[0]


def _load_endpoint_probe() -> ModuleType:
    path = REPO_ROOT / "benchmarks" / "windows" / "endpoint_probe.py"
    spec = importlib.util.spec_from_file_location("scriber_endpoint_probe_resource_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_stability_aggregates_the_complete_detected_process_tree() -> None:
    source = _read("scripts/smoke_tauri_desktop.ps1")
    roles = _powershell_function(source, "Get-ScriberProcessRole", "Get-ProcessTreeMetrics")
    stability = _powershell_function(source, "Test-RuntimeStability", "Test-LiveRecordingStability")

    for role in (
        'return "tauriShell"',
        'return "backend"',
        'return "webview2"',
        'return "audioSidecar"',
        'return "diarizationSidecar"',
    ):
        assert role in roles

    assert "foreach ($metric in $currentProcessMetrics)" in stability
    assert "-PreviousSeconds $lastCpuTotals[$metricPid]" in stability
    assert "$processTreeCpuTotal += [double]$metricCpuPercent" in stability
    assert "processTreeCpuPercent = $processTreeCpuPercent" in stability
    assert "Measure-Object -Property workingSetMb -Sum" in stability
    assert "totalWorkingSetMb = $totalWorkingSetMb" in stability
    assert "processTreeCpuAvgPercent = $processTreeCpuAvg" in stability
    assert "totalWorkingSetMaxMb = $totalWorkingSetMax" in stability

    # Existing backend-only evidence remains available to older consumers.
    assert "backendWorkingSetMaxMb = $workingSetMax" in stability
    assert "combinedCpuAvgPercent = $combinedCpuAvg" in stability

    # The generic idle CPU gate now guards the complete Scriber process tree.
    assert "-not $processTreeCpuValues.Count" in stability
    assert "$processTreeCpuAvg -gt $MaxIdleCpuPercent" in stability


def test_shell_menu_smoke_captures_a_sequence_bounded_long_task_window() -> None:
    source = _read("scripts/smoke_tauri_desktop.ps1")
    diagnostics = _powershell_function(
        source,
        "Get-FrontendPerformanceDiagnostics",
        "Invoke-BackendShutdown",
    )
    shell_smoke = _powershell_function(source, "Test-ShellMenuSmoke", "Wait-BackendCrashMetadata")
    endpoint_probe = _read("benchmarks/windows/endpoint_probe.py")

    assert "/api/runtime/frontend-performance" in diagnostics
    assert '"X-Scriber-Token"' in diagnostics
    assert "$frontendPerformanceBaseline = Get-FrontendPerformanceDiagnostics" in shell_smoke
    assert "-AfterSequence ([int64]$baselineWindow.lastSequence)" in shell_smoke
    assert "-SourceInstanceId ([string]$frontendPerformanceBaseline.sourceInstanceId)" in shell_smoke
    assert "measurementWindowMs" in shell_smoke
    assert "Request-FrontendPerformanceFlush" in shell_smoke
    assert "heartbeatAcknowledged" in shell_smoke
    assert "Set-Content -LiteralPath $QuitBarrierPath" in shell_smoke
    assert 'measured_window.get("truncated") is False' in endpoint_probe
    assert "dropped_unchanged" in endpoint_probe
    assert "sequence_gaps_unchanged" in endpoint_probe
    assert "heartbeat_acknowledged" in endpoint_probe
    assert 'resource_metrics["ui_long_tasks_gt_200ms"] = "unknown"' in endpoint_probe


def test_app_frame_probe_maps_only_measured_process_tree_resources(
    monkeypatch,
    tmp_path: Path,
) -> None:
    endpoint_probe = _load_endpoint_probe()
    observer = SimpleNamespace(terminate=lambda: None, kill=lambda: None)
    capture = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(endpoint_probe, "run_process", lambda *args, **kwargs: observer)
    monkeypatch.setattr(endpoint_probe, "run_capture", lambda *args, **kwargs: capture)
    monkeypatch.setattr(endpoint_probe, "wait_process", lambda *args, **kwargs: 0)
    monkeypatch.setattr(endpoint_probe, "smoke_args", lambda *args, **kwargs: ["smoke"])
    monkeypatch.setattr(endpoint_probe, "qpc_ticks", lambda: 900)

    observed = {
        "ok": True,
        "qpcTicks": 2_000,
        "qpcFrequency": 1_000,
        "startGateQpcTicks": 1_000,
        "firstNonEmptyQpcTicks": 1_100,
        "stableQpcTicks": 1_900,
        "stableConfirmedQpcTicks": 1_950,
    }
    smoke = {
        "ok": True,
        "shellMenuSmoke": {
            "triggerQpcTicks": 1_000,
            "qpcFrequency": 1_000,
            "showWindow": {"elapsedMs": 100},
        },
        "stability": {
            "processTreeCpuAvgPercent": 7.251,
            "totalWorkingSetMaxMb": 512.567,
            "combinedCpuAvgPercent": 99.0,
            "backendWorkingSetMaxMb": 999.0,
        },
    }

    def fake_load_json(path: Path) -> dict:
        return observed if path.name == "app-observer.json" else smoke

    monkeypatch.setattr(endpoint_probe, "load_json", fake_load_json)

    result = endpoint_probe.run_app_frame_probe(
        REPO_ROOT,
        tmp_path / "install",
        tmp_path / "output",
        10,
    )

    assert result["resourceMetrics"] == {
        "idle_cpu_pct": 7.251,
        "working_set_mb": 512.567,
        "ui_long_tasks_gt_200ms": "unknown",
    }


def test_app_frame_probe_does_not_invent_unmeasured_resource_values(
    monkeypatch,
    tmp_path: Path,
) -> None:
    endpoint_probe = _load_endpoint_probe()
    observer = SimpleNamespace(terminate=lambda: None, kill=lambda: None)
    capture = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(endpoint_probe, "run_process", lambda *args, **kwargs: observer)
    monkeypatch.setattr(endpoint_probe, "run_capture", lambda *args, **kwargs: capture)
    monkeypatch.setattr(endpoint_probe, "wait_process", lambda *args, **kwargs: 0)
    monkeypatch.setattr(endpoint_probe, "smoke_args", lambda *args, **kwargs: ["smoke"])
    monkeypatch.setattr(endpoint_probe, "qpc_ticks", lambda: 900)
    monkeypatch.setattr(
        endpoint_probe,
        "load_json",
        lambda path: (
            {"ok": True, "qpcTicks": 2_000, "qpcFrequency": 1_000}
            if path.name == "app-observer.json"
            else {
                "ok": True,
                "shellMenuSmoke": {"triggerQpcTicks": 1_000, "qpcFrequency": 1_000},
                "stability": {
                    "combinedCpuAvgPercent": 4.0,
                    "backendWorkingSetMaxMb": 128.0,
                },
            }
        ),
    )

    result = endpoint_probe.run_app_frame_probe(
        REPO_ROOT,
        tmp_path / "install",
        tmp_path / "output",
        10,
    )

    assert result["resourceMetrics"] == {"ui_long_tasks_gt_200ms": "unknown"}


def test_app_frame_probe_maps_real_frontend_long_task_delta(
    monkeypatch,
    tmp_path: Path,
) -> None:
    endpoint_probe = _load_endpoint_probe()
    observer = SimpleNamespace(terminate=lambda: None, kill=lambda: None)
    capture = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(endpoint_probe, "run_process", lambda *args, **kwargs: observer)
    monkeypatch.setattr(endpoint_probe, "run_capture", lambda *args, **kwargs: capture)
    monkeypatch.setattr(endpoint_probe, "wait_process", lambda *args, **kwargs: 0)
    monkeypatch.setattr(endpoint_probe, "smoke_args", lambda *args, **kwargs: ["smoke"])
    monkeypatch.setattr(endpoint_probe, "qpc_ticks", lambda: 900)
    smoke = {
        "ok": True,
        "shellMenuSmoke": {
            "triggerQpcTicks": 1_000,
            "qpcFrequency": 1_000,
            "frontendPerformance": {
                "measurementWindowMs": 450.0,
                "measurementEndQpcTicks": 2_500,
                "heartbeatAckQpcTicks": 2_600,
                "heartbeatAcknowledged": True,
                "flushRequest": {
                    "heartbeatSequence": 4,
                    "requestedAfterFrontendUptimeMs": 7_500.0,
                    "requestedAtUptimeSeconds": 12.0,
                },
                "baseline": {
                    "available": True,
                    "observerSupported": True,
                    "sourceInstanceId": "webview-123",
                    "window": {
                        "lastSequence": 3,
                        "droppedEntries": 0,
                        "sequenceGaps": 0,
                        "observedAtFrontendUptimeMs": 7_500.0,
                    },
                },
                "afterShow": {
                    "available": True,
                    "observerSupported": True,
                    "sourceInstanceId": "webview-123",
                    "window": {
                        "queryAfterSequence": 3,
                        "count": 2,
                        "maxDurationMs": 325.5,
                        "totalDurationMs": 550.75,
                        "droppedEntries": 0,
                        "sequenceGaps": 0,
                        "heartbeatSequence": 4,
                        "heartbeatObservedAtFrontendUptimeMs": 8_000.0,
                        "heartbeatReceivedAtUptimeSeconds": 12.1,
                        "truncated": False,
                    },
                },
            },
        },
    }

    monkeypatch.setattr(
        endpoint_probe,
        "load_json",
        lambda path: (
            {"ok": True, "qpcTicks": 2_000, "qpcFrequency": 1_000}
            if path.name == "app-observer.json"
            else smoke
        ),
    )

    result = endpoint_probe.run_app_frame_probe(
        REPO_ROOT,
        tmp_path / "install",
        tmp_path / "output",
        10,
    )

    assert result["frontendPerformance"]["measured"] is True
    assert result["resourceMetrics"] == {
        "ui_long_tasks_gt_200ms": 2,
        "ui_long_task_max_ms": 325.5,
        "ui_long_task_total_ms": 550.75,
        "ui_long_task_window_ms": 450.0,
    }


@pytest.mark.parametrize(
    "invalid_evidence",
    (
        "stale_heartbeat",
        "stale_frontend_observed_at",
        "stale_ack_time",
        "source_change",
        "dropped",
        "sequence_gap",
        "truncated",
    ),
)
def test_app_frame_probe_rejects_invalid_frontend_barrier_evidence(
    monkeypatch,
    tmp_path: Path,
    invalid_evidence: str,
) -> None:
    endpoint_probe = _load_endpoint_probe()
    observer = SimpleNamespace(terminate=lambda: None, kill=lambda: None)
    capture = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(endpoint_probe, "run_process", lambda *args, **kwargs: observer)
    monkeypatch.setattr(endpoint_probe, "run_capture", lambda *args, **kwargs: capture)
    monkeypatch.setattr(endpoint_probe, "wait_process", lambda *args, **kwargs: 0)
    monkeypatch.setattr(endpoint_probe, "smoke_args", lambda *args, **kwargs: ["smoke"])
    monkeypatch.setattr(endpoint_probe, "qpc_ticks", lambda: 900)
    baseline_window = {"lastSequence": 10, "droppedEntries": 0, "sequenceGaps": 0}
    after_window = {
        "queryAfterSequence": 10,
        "count": 0,
        "maxDurationMs": 0.0,
        "totalDurationMs": 0.0,
        "droppedEntries": 0,
        "sequenceGaps": 0,
        "heartbeatSequence": 11,
        "heartbeatObservedAtFrontendUptimeMs": 8_000.0,
        "heartbeatReceivedAtUptimeSeconds": 12.1,
        "truncated": False,
    }
    source_after = "webview-a"
    heartbeat_ack_ticks = 2_600
    if invalid_evidence == "stale_heartbeat":
        after_window["heartbeatSequence"] = 10
    elif invalid_evidence == "stale_frontend_observed_at":
        after_window["heartbeatObservedAtFrontendUptimeMs"] = 7_400.0
    elif invalid_evidence == "stale_ack_time":
        heartbeat_ack_ticks = 2_400
    elif invalid_evidence == "source_change":
        source_after = "webview-b"
    elif invalid_evidence == "dropped":
        after_window["droppedEntries"] = 1
    elif invalid_evidence == "sequence_gap":
        after_window["sequenceGaps"] = 1
    elif invalid_evidence == "truncated":
        after_window["truncated"] = True

    smoke = {
        "ok": True,
        "shellMenuSmoke": {
            "triggerQpcTicks": 1_000,
            "qpcFrequency": 1_000,
            "frontendPerformance": {
                "measurementWindowMs": 400.0,
                "measurementEndQpcTicks": 2_500,
                "heartbeatAckQpcTicks": heartbeat_ack_ticks,
                "heartbeatAcknowledged": True,
                "flushRequest": {
                    "heartbeatSequence": 11,
                    "requestedAfterFrontendUptimeMs": 7_500.0,
                    "requestedAtUptimeSeconds": 12.0,
                },
                "baseline": {
                    "available": True,
                    "observerSupported": True,
                    "sourceInstanceId": "webview-a",
                    "window": baseline_window,
                },
                "afterShow": {
                    "available": True,
                    "observerSupported": True,
                    "sourceInstanceId": source_after,
                    "window": after_window,
                },
            },
        },
    }
    monkeypatch.setattr(
        endpoint_probe,
        "load_json",
        lambda path: (
            {"ok": True, "qpcTicks": 2_000, "qpcFrequency": 1_000}
            if path.name == "app-observer.json"
            else smoke
        ),
    )

    result = endpoint_probe.run_app_frame_probe(
        REPO_ROOT,
        tmp_path / "install",
        tmp_path / "output",
        10,
    )

    assert result["frontendPerformance"]["measured"] is False
    assert result["resourceMetrics"] == {"ui_long_tasks_gt_200ms": "unknown"}
