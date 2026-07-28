from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.windows import (
    app_ux_collector,
    app_ux_lifecycle_collector,
    endpoint_probe,
    trace_collector,
)

EXPECTED_SCENARIOS = (
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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_artifact(root: Path, name: str, payload: dict[str, object]) -> dict[str, str]:
    path = root / name
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return {"path": name, "sha256": _sha(path)}


def _payload(root: Path, samples_per_scenario: int = 2) -> dict[str, object]:
    run_id = "102aa8a3f1964e4da382f18489df1b6a"
    installed_sha = "a" * 64
    harness_sha = "b" * 64
    expected_hash = hashlib.sha256(b"expected stable text").hexdigest()
    start_markers = {
        "provider_result_to_completed_visible": "provider_response_complete",
        "session_finished_to_history_visible": "session_finished_emitted",
    }
    start_sources = {
        "cold_app_launch": "windows_create_process",
        "warm_app_activation": "windows_second_instance_launch",
        "provider_result_to_completed_visible": "installed_backend_provider_event",
        "session_finished_to_history_visible": "installed_backend_session_event",
    }
    samples: list[dict[str, object]] = []
    sequence = 0
    for scenario_index, scenario in enumerate(EXPECTED_SCENARIOS):
        for iteration in range(1, samples_per_scenario + 1):
            sequence += 1
            sample_id = f"{sequence:032x}"
            session_id = f"{sequence + 10_000:032x}"
            start_ticks = 10_000_000 + sequence * 100_000
            end_ticks = start_ticks + (scenario_index + 1) * 10_000 + iteration
            start_marker = start_markers.get(scenario, "user_input_received")
            start_source = start_sources.get(scenario, "uia_invoke")
            action_artifact = _write_artifact(
                root,
                f"action-{scenario}-{iteration}.json",
                {
                    "ok": True,
                    "endpoint": start_marker,
                    "source": start_source,
                    "qpcTicks": start_ticks,
                    "qpcFrequency": 10_000_000,
                },
            )
            observer_artifact = _write_artifact(
                root,
                f"observer-{scenario}-{iteration}.json",
                {
                    "ok": True,
                    "endpoint": "first_stable_visible_frame",
                    "stableQpcTicks": end_ticks,
                    "qpcFrequency": 10_000_000,
                    "stableSampleCount": 2,
                    "processId": 4321,
                    "processCreationTime100ns": 133_000_000_000_000_000,
                    "expectedTextSha256": [expected_hash],
                },
            )
            performance_payload = {
                "observerSupported": True,
                "sourceInstanceId": "a173c38c8ae840f48f098df75e9ba716",
                "queryAfterSequence": sequence - 1,
                "lastSequence": sequence,
                "truncated": False,
                "droppedEntriesBefore": 0,
                "droppedEntriesAfter": 0,
                "sequenceGapsBefore": 0,
                "sequenceGapsAfter": 0,
                "heartbeatAcknowledged": True,
                "measurementEndQpcTicks": end_ticks + 100,
                "heartbeatAckQpcTicks": end_ticks + 200,
                "count": 0,
                "maxDurationMs": 0.0,
                "totalDurationMs": 0.0,
            }
            performance_artifact = _write_artifact(
                root,
                f"performance-{scenario}-{iteration}.json",
                performance_payload,
            )
            generation_payload = {
                "ok": True,
                "reasons": [],
                "app": {
                    "pid": 4321,
                    "parentPid": 100,
                    "name": "scriber-desktop.exe",
                    "creationTime100ns": 133_000_000_000_000_000,
                },
                "backend": {
                    "pid": 4322,
                    "parentPid": 4321,
                    "name": "scriber-backend.exe",
                    "creationTime100ns": 133_000_000_000_000_100,
                },
                "webViewProcesses": [
                    {
                        "pid": 4323,
                        "parentPid": 4321,
                        "name": "msedgewebview2.exe",
                        "creationTime100ns": 133_000_000_000_000_200,
                    }
                ],
                "backendStartedAt": "2026-07-15T12:00:00Z",
                "frontendReadyReceivedAt": "2026-07-15T12:00:01Z",
            }
            generation = endpoint_probe.process_generation_fingerprint(generation_payload)
            generation_payload["fingerprint"] = generation
            replay_generation = endpoint_probe.provider_replay_process_generation_sha256(generation_payload)
            generation_artifact = _write_artifact(
                root,
                f"generation-{scenario}-{iteration}.json",
                generation_payload,
            )
            sample: dict[str, object] = {
                "scenario": scenario,
                "iteration": iteration,
                "runId": run_id,
                "sampleId": sample_id,
                "installedExeSha256": installed_sha,
                "harnessManifestSha256": harness_sha,
                "processGenerationFingerprint": generation,
                "processGeneration": {
                    "fingerprint": generation,
                    "artifact": generation_artifact,
                },
                "start": {
                    "marker": start_marker,
                    "source": start_source,
                    "qpcTicks": start_ticks,
                    "qpcFrequency": 10_000_000,
                    "processGenerationFingerprint": generation,
                    "artifact": action_artifact,
                },
                "stableFrame": {
                    "marker": "first_stable_visible_frame",
                    "source": "windows_uia",
                    "qpcTicks": end_ticks,
                    "qpcFrequency": 10_000_000,
                    "stableSampleCount": 2,
                    "expectedTextSha256": [expected_hash],
                    "windowProcessId": 4321,
                    "processCreationTime100ns": 133_000_000_000_000_000,
                    "processGenerationFingerprint": generation,
                    "artifact": observer_artifact,
                },
                "durationMs": round((end_ticks - start_ticks) / 10_000.0, 3),
                "frontendPerformance": {**performance_payload, "artifact": performance_artifact},
            }
            if scenario in endpoint_probe.APP_UX_LIFECYCLE_SCENARIOS:
                event_markers = {
                    "stop_to_transcribing_visible": "recording_state_transcribing_emitted",
                    "provider_result_to_completed_visible": "provider_response_complete",
                    "session_finished_to_history_visible": "session_finished_emitted",
                }
                event_sources = {
                    "stop_to_transcribing_visible": "installed_backend_state_event",
                    "provider_result_to_completed_visible": "installed_backend_provider_event",
                    "session_finished_to_history_visible": "installed_backend_session_event",
                }
                event_ticks = start_ticks + 2 if scenario == "stop_to_transcribing_visible" else start_ticks
                event_payload = {
                    "ok": True,
                    "source": event_sources[scenario],
                    "marker": event_markers[scenario],
                    "qpcTicks": event_ticks,
                    "qpcFrequency": 10_000_000,
                    "apiVersion": 1,
                    "runId": run_id,
                    "sampleId": sample_id,
                    "sessionId": session_id,
                    "processGenerationFingerprint": generation,
                    "providerReplayProcessGenerationFingerprint": replay_generation,
                }
                raw_event = {
                    **event_payload,
                    "processGenerationFingerprint": replay_generation,
                }
                event_payload["installedEvent"] = raw_event
                if scenario == "stop_to_transcribing_visible":
                    manual_base = {
                        "contractVersion": 1,
                        "runId": run_id,
                        "sampleId": sample_id,
                        "sessionId": session_id,
                        "processGenerationFingerprint": replay_generation,
                        "qpcFrequency": 10_000_000,
                    }
                    event_payload["manualStopReadyAttestation"] = {
                        **manual_base,
                        "source": "rust_audio_frame_pipe_fixture_consumed",
                        "qpcTicks": start_ticks - 1,
                    }
                    event_payload["manualStopRequest"] = {
                        **manual_base,
                        "source": "uia_stop_request",
                        "qpcTicks": start_ticks + 1,
                    }
                event_artifact = _write_artifact(
                    root,
                    f"event-{scenario}-{iteration}.json",
                    event_payload,
                )
                sample["eventEvidence"] = {
                    "installedRuntime": True,
                    "apiVersion": 1,
                    "runId": run_id,
                    "sampleId": sample_id,
                    "sessionId": session_id,
                    "source": event_sources[scenario],
                    "marker": event_markers[scenario],
                    "qpcTicks": event_ticks,
                    "processGenerationFingerprint": generation,
                    "providerReplayProcessGenerationFingerprint": replay_generation,
                    "artifact": event_artifact,
                }
            samples.append(sample)
    resource_payload = {
        "runId": run_id,
        "installedExeSha256": installed_sha,
        "harnessManifestSha256": harness_sha,
        "sampleCount": len(samples),
        "idleCpuPercent": 0.25,
        "workingSetMb": 180.0,
    }
    resource_artifact = _write_artifact(root, "resource.json", resource_payload)
    return {
        "schemaVersion": 1,
        "contract": endpoint_probe.APP_UX_EVIDENCE_CONTRACT,
        "runId": run_id,
        "installedExeSha256": installed_sha,
        "harnessManifestSha256": harness_sha,
        "samplesPerScenario": samples_per_scenario,
        "samples": samples,
        "resourceEvidence": {**resource_payload, "artifact": resource_artifact},
    }


def _validate(root: Path, payload: dict[str, object], count: int = 2) -> dict[str, object]:
    return endpoint_probe.validate_app_ux_evidence(
        payload,
        artifact_root=root,
        required_samples_per_scenario=count,
    )


def _lifecycle_payload(root: Path, samples_per_scenario: int = 2) -> dict[str, object]:
    full = _payload(root, samples_per_scenario=samples_per_scenario)
    samples = [sample for sample in full["samples"] if sample["scenario"] in endpoint_probe.APP_UX_LIFECYCLE_SCENARIOS]
    resource_payload = {
        "runId": full["runId"],
        "installedExeSha256": full["installedExeSha256"],
        "harnessManifestSha256": full["harnessManifestSha256"],
        "sampleCount": len(samples),
        "idleCpuPercent": 0.3,
        "workingSetMb": 190.0,
    }
    resource_artifact = _write_artifact(root, "lifecycle-resource.json", resource_payload)
    return {
        "schemaVersion": 1,
        "contract": endpoint_probe.APP_UX_LIFECYCLE_IMPORT_CONTRACT,
        "runId": full["runId"],
        "installedExeSha256": full["installedExeSha256"],
        "harnessManifestSha256": full["harnessManifestSha256"],
        "samplesPerScenario": samples_per_scenario,
        "scenarioOrder": [
            scenario for scenario in EXPECTED_SCENARIOS if scenario in endpoint_probe.APP_UX_LIFECYCLE_SCENARIOS
        ],
        "samples": samples,
        "resourceEvidence": {**resource_payload, "artifact": resource_artifact},
    }


def _validate_lifecycle(
    root: Path,
    payload: dict[str, object],
    count: int = 2,
) -> dict[str, object]:
    return endpoint_probe.validate_app_ux_lifecycle_import(
        payload,
        artifact_root=root,
        required_samples_per_scenario=count,
        expected_run_id=str(payload["runId"]),
        expected_installed_exe_sha256=str(payload["installedExeSha256"]),
        expected_harness_manifest_sha256=str(payload["harnessManifestSha256"]),
    )


def test_contract_names_all_nine_goal_scenarios_and_full_local_runs_each_twenty() -> None:
    assert endpoint_probe.APP_UX_SCENARIOS == EXPECTED_SCENARIOS
    assert endpoint_probe.SAMPLE_PLANS["FullLocal"]["appUxPerScenario"] == 20
    assert "appUx" not in endpoint_probe.SAMPLE_PLANS["FullLocal"]


def test_complete_hash_bound_nine_scenario_matrix_is_eligible(tmp_path: Path) -> None:
    result = _validate(tmp_path, _payload(tmp_path))

    assert result["metricEligible"] is True
    assert result["metricBlockedReason"] is None
    assert result["metrics"]["app_ux_sample_count"] == 18
    assert isinstance(result["metrics"]["app_ux_p50_ms"], float)
    assert isinstance(result["metrics"]["app_ux_p75_ms"], float)
    assert isinstance(result["metrics"]["app_ux_p95_ms"], float)
    assert result["resourceMetrics"] == {
        "ui_long_tasks_gt_200ms": 0,
        "ui_long_task_max_ms": 0.0,
        "ui_long_task_total_ms": 0.0,
        "idle_cpu_pct": 0.25,
        "working_set_mb": 180.0,
    }
    assert all(
        item["sampleCount"] == 2 and item["metricEligible"] is True for item in result["scenarioResults"].values()
    )


def test_twenty_generic_show_window_samples_cannot_stand_in_for_matrix(tmp_path: Path) -> None:
    payload = _payload(tmp_path, samples_per_scenario=20)
    payload["samples"] = [sample for sample in payload["samples"] if sample["scenario"] == "warm_app_activation"]
    payload["resourceEvidence"]["sampleCount"] = 20

    result = _validate(tmp_path, payload, count=20)

    assert result["metricEligible"] is False
    assert result["metrics"]["app_ux_p95_ms"] == "unknown"
    assert result["scenarioResults"]["warm_app_activation"]["sampleCount"] == 20
    assert result["scenarioResults"]["cold_app_launch"]["sampleCount"] == 0
    assert "scenario_sample_count:cold_app_launch" in result["reasons"]


def test_one_missing_scenario_sample_blocks_entire_metric(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["samples"].pop()
    payload["resourceEvidence"]["sampleCount"] -= 1

    result = _validate(tmp_path, payload)

    assert result["metricEligible"] is False
    assert result["scenarioResults"]["return_to_dashboard"]["sampleCount"] == 1
    assert result["metrics"]["app_ux_p50_ms"] == "unknown"


def test_duplicate_iteration_does_not_inflate_scenario_count(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    duplicate = dict(payload["samples"][0])
    payload["samples"][1] = duplicate

    result = _validate(tmp_path, payload)

    assert result["metricEligible"] is False
    assert any("duplicate_scenario_iteration" in invalid["reasons"] for invalid in result["invalidSamples"])


def test_tampered_uia_artifact_blocks_sample(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    (tmp_path / "observer-cold_app_launch-1.json").write_text("tampered", encoding="utf-8")

    result = _validate(tmp_path, payload)

    assert result["metricEligible"] is False
    assert any("stable_frame_artifact_sha256_mismatch" in invalid["reasons"] for invalid in result["invalidSamples"])


def test_unbound_or_tampered_process_generation_blocks_sample(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    generation_path = tmp_path / payload["samples"][0]["processGeneration"]["artifact"]["path"]
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    generation["app"]["creationTime100ns"] += 1
    generation_path.write_text(json.dumps(generation), encoding="utf-8")

    result = _validate(tmp_path, payload)

    assert result["metricEligible"] is False
    assert any(
        "process_generation_artifact_sha256_mismatch" in invalid["reasons"] for invalid in result["invalidSamples"]
    )


def test_duplicate_sample_id_is_not_accepted_across_scenarios(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["samples"][2]["sampleId"] = payload["samples"][0]["sampleId"]

    result = _validate(tmp_path, payload)

    assert result["metricEligible"] is False
    assert any("duplicate_sample_id" in invalid["reasons"] for invalid in result["invalidSamples"])


def test_lifecycle_scenario_requires_installed_event_artifact(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    sample = next(item for item in payload["samples"] if item["scenario"] == "provider_result_to_completed_visible")
    sample.pop("eventEvidence")

    result = _validate(tmp_path, payload)

    assert result["metricEligible"] is False
    invalid = next(
        item for item in result["invalidSamples"] if item["scenario"] == "provider_result_to_completed_visible"
    )
    assert "installed_runtime_event_unproven" in invalid["reasons"]
    assert "event_artifact_binding_missing" in invalid["reasons"]


def test_unacknowledged_post_frame_long_task_flush_is_unknown_not_zero(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["samples"][0]["frontendPerformance"]["heartbeatAcknowledged"] = False

    result = _validate(tmp_path, payload)

    assert result["metricEligible"] is False
    assert result["metrics"]["app_ux_p95_ms"] == "unknown"
    assert "long_task_heartbeat_unacknowledged" in result["invalidSamples"][0]["reasons"]


def test_artifact_path_escape_is_rejected(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    outside = tmp_path.parent / "outside-app-ux.json"
    outside.write_text("{}", encoding="utf-8")
    payload["samples"][0]["start"]["artifact"] = {
        "path": "../outside-app-ux.json",
        "sha256": _sha(outside),
    }

    result = _validate(tmp_path, payload)

    assert result["metricEligible"] is False
    assert "start_artifact_path_outside_evidence_root" in result["invalidSamples"][0]["reasons"]


def test_evidence_must_match_current_installed_binary_and_harness(tmp_path: Path) -> None:
    payload = _payload(tmp_path)

    result = endpoint_probe.validate_app_ux_evidence(
        payload,
        artifact_root=tmp_path,
        required_samples_per_scenario=2,
        expected_installed_exe_sha256="d" * 64,
        expected_harness_manifest_sha256="e" * 64,
    )

    assert result["metricEligible"] is False
    assert result["metrics"]["app_ux_p95_ms"] == "unknown"
    assert "installed_exe_sha256_mismatch" in result["reasons"]
    assert "harness_manifest_sha256_mismatch" in result["reasons"]


def test_lifecycle_import_accepts_exact_three_scenario_matrix(tmp_path: Path) -> None:
    result = _validate_lifecycle(tmp_path, _lifecycle_payload(tmp_path))

    assert result["metricEligible"] is True
    assert result["scenarioCounts"] == {
        "stop_to_transcribing_visible": 2,
        "provider_result_to_completed_visible": 2,
        "session_finished_to_history_visible": 2,
    }
    assert len(result["results"]) == 6
    assert result["resourceMetrics"] == {
        "idleCpuPercent": 0.3,
        "workingSetMb": 190.0,
    }


def test_lifecycle_import_fails_closed_for_missing_or_uia_scenario(tmp_path: Path) -> None:
    payload = _lifecycle_payload(tmp_path)
    payload["samples"].pop()
    payload["samples"].append(_payload(tmp_path)["samples"][0])

    result = _validate_lifecycle(tmp_path, payload)

    assert result["metricEligible"] is False
    assert "invalid_samples" in result["reasons"]
    assert "scenario_sample_count:session_finished_to_history_visible" in result["reasons"]
    assert any("non_lifecycle_scenario_in_import" in invalid["reasons"] for invalid in result["invalidSamples"])


def test_lifecycle_import_rejects_tampered_installed_event(tmp_path: Path) -> None:
    payload = _lifecycle_payload(tmp_path)
    sample = payload["samples"][0]
    event_path = tmp_path / sample["eventEvidence"]["artifact"]["path"]
    event_path.write_text("{}", encoding="utf-8")

    result = _validate_lifecycle(tmp_path, payload)

    assert result["metricEligible"] is False
    assert any("event_artifact_sha256_mismatch" in invalid["reasons"] for invalid in result["invalidSamples"])


def test_lifecycle_import_requires_unchanged_raw_runtime_marker(
    tmp_path: Path,
) -> None:
    payload = _lifecycle_payload(tmp_path)
    sample = payload["samples"][0]
    event_path = tmp_path / sample["eventEvidence"]["artifact"]["path"]
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event.pop("installedEvent")
    event_path.write_text(json.dumps(event, sort_keys=True), encoding="utf-8")
    sample["eventEvidence"]["artifact"]["sha256"] = _sha(event_path)

    result = _validate_lifecycle(tmp_path, payload)

    assert result["metricEligible"] is False
    assert any("installed_event_raw_marker_missing" in invalid["reasons"] for invalid in result["invalidSamples"])


def test_lifecycle_import_rejects_runtime_marker_from_another_generation(
    tmp_path: Path,
) -> None:
    payload = _lifecycle_payload(tmp_path)
    sample = payload["samples"][0]
    event_path = tmp_path / sample["eventEvidence"]["artifact"]["path"]
    event = json.loads(event_path.read_text(encoding="utf-8"))
    foreign_replay_generation = "c" * 64
    sample["eventEvidence"]["providerReplayProcessGenerationFingerprint"] = foreign_replay_generation
    event["providerReplayProcessGenerationFingerprint"] = foreign_replay_generation
    event["installedEvent"]["processGenerationFingerprint"] = foreign_replay_generation
    event_path.write_text(json.dumps(event, sort_keys=True), encoding="utf-8")
    sample["eventEvidence"]["artifact"]["sha256"] = _sha(event_path)

    result = _validate_lifecycle(tmp_path, payload)

    assert result["metricEligible"] is False
    assert any("event_replay_process_generation_mismatch" in invalid["reasons"] for invalid in result["invalidSamples"])


def test_trace_collector_rejects_generic_app_ux_qpc_events(tmp_path: Path) -> None:
    generic = tmp_path / "generic.json"
    generic.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "session_id": "1",
                        "scenario": "app_ux",
                        "marker": "user_input_received",
                        "qpc_ticks": 100,
                    },
                    {
                        "session_id": "1",
                        "scenario": "app_ux",
                        "marker": "first_stable_visible_frame",
                        "qpc_ticks": 200,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    assert trace_collector.load_app_ux_metrics([generic]) == {}


def test_checked_in_uia_driver_is_process_generation_bound_and_real() -> None:
    action = (endpoint_probe.REPO_ROOT / "benchmarks/windows/app_action.ps1").read_text(encoding="utf-8")
    observer = (endpoint_probe.REPO_ROOT / "benchmarks/windows/app_observer.ps1").read_text(encoding="utf-8")

    assert "[long]$ProcessCreationTime100ns" in action
    assert "[string]$ControlAutomationId" in action
    assert "AutomationElement]::AutomationIdProperty" in action
    assert "ControlName or ControlAutomationId is required." in action
    assert "$matchingControlCount -eq 1" in action
    assert '[ValidateSet("Exact", "Prefix")]' in action
    assert ".StartsWith($ControlName" in action
    assert "InvokePattern" in action
    assert "SelectionItemPattern" in action
    assert "LegacyIAccessiblePattern" in action
    assert "ScrollItemPattern" in action
    assert "ScrollIntoView()" in action
    assert "scrolledIntoView = $scrolledIntoView" in action
    assert "$inputQpcTicks = [System.Diagnostics.Stopwatch]::GetTimestamp()" in action
    assert "qpcTicks = $inputQpcTicks" in action
    assert 'source = "uia_invoke"' in action
    assert action.index("ScrollIntoView()") < action.index("$inputQpcTicks =")
    assert action.index("$inputQpcTicks =") < action.index(").Invoke()")
    assert "SendInput" not in action
    assert "Set-Content" in action  # evidence artifact/observer gate only
    assert "ExpectedProcessCreationTime100ns" in observer
    assert "candidateWindow.Current.ProcessId" in observer
    assert "$eligibleWindowCount -ne 1" in observer
    assert "expectedTextSha256" in observer
    assert "expectedText =" not in observer


def test_collector_uses_real_uia_and_requires_installed_lifecycle_import() -> None:
    collector = (endpoint_probe.REPO_ROOT / "benchmarks/windows/app_ux_collector.py").read_text(encoding="utf-8")
    lifecycle_collector = (endpoint_probe.REPO_ROOT / "benchmarks/windows/app_ux_lifecycle_collector.py").read_text(
        encoding="utf-8"
    )
    schema = json.loads(
        (endpoint_probe.REPO_ROOT / "benchmarks/windows/app_ux_lifecycle_import.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert "app_action.ps1" in collector
    assert "app_observer.ps1" in collector
    assert "windows_create_process" in collector
    assert "windows_second_instance_launch" in collector
    assert 'control_name_match="Prefix"' in collector
    assert "validate_app_ux_lifecycle_import" in collector
    assert "installed_lifecycle_evidence_required" in collector
    assert "Do not synthesize, infer, or relabel provider/session markers." in collector
    assert "SendInput" not in collector
    assert collector.index('"open_transcript_detail"') < collector.index('"setup-return-from-transcript-detail"')
    assert collector.index('"setup-return-from-transcript-detail"') < collector.index('"setup-switch-command-palette"')
    assert "benchmarks/windows/app_ux_lifecycle_collector.py" in endpoint_probe.APP_UX_HARNESS_FILES
    assert "app_action.ps1" in lifecycle_collector
    assert "ux._start_observer" in lifecycle_collector
    assert "manualStopRequired" in lifecycle_collector
    assert "installedEvent" in lifecycle_collector
    assert "providerReplayProcessGenerationFingerprint" in lifecycle_collector
    assert "validate_app_ux_lifecycle_import" in lifecycle_collector
    assert 'repo_root / "tmp" / "app-ux-lifecycle-smoke"' in lifecycle_collector
    assert "EXPECTED_VISIBLE_TEXT_KEY" in lifecycle_collector
    assert app_ux_collector.UI_TEXT["de"]["recent_recordings"] == "Letzte Aufnahmen"
    assert app_ux_collector.UI_TEXT["de"]["transcribing"] == "Wird transkribiert"
    assert schema["properties"]["contract"]["const"] == endpoint_probe.APP_UX_LIFECYCLE_IMPORT_CONTRACT
    assert schema["properties"]["scenarioOrder"]["const"] == [
        "stop_to_transcribing_visible",
        "provider_result_to_completed_visible",
        "session_finished_to_history_visible",
    ]


def test_collector_detects_persisted_ui_locale_with_allowlisted_uia_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed: list[str] = []

    monkeypatch.setattr(
        app_ux_collector,
        "_preferred_ui_locale_order",
        lambda: ("de", "en"),
    )

    def fake_start(
        _repo_root: Path,
        sample_dir: Path,
        *,
        expected_text: str,
        **_kwargs,
    ):
        observed.append(expected_text)
        output = sample_dir / "stable-frame.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        return object(), output

    def fake_wait(_process, _output: Path, sample_dir: Path, _timeout: float):
        if sample_dir.name == "locale-de":
            raise RuntimeError("not German")
        return {"ok": True}

    monkeypatch.setattr(app_ux_collector, "_start_observer", fake_start)
    monkeypatch.setattr(app_ux_collector, "_wait_observer", fake_wait)

    locale = app_ux_collector._detect_ui_locale(
        endpoint_probe.REPO_ROOT,
        tmp_path,
        process_id=123,
        process_creation_time_100ns=456,
        timeout_sec=10,
    )

    assert locale == "en"
    assert observed == ["Letzte Aufnahmen", "Recent recordings"]
    attestation = json.loads((tmp_path / "ui-locale.json").read_text(encoding="utf-8"))
    assert attestation["locale"] == "en"
    assert "Recent recordings" not in (tmp_path / "ui-locale.json").read_text(encoding="utf-8")


def test_collector_discovers_tauri_owned_runtime_port(monkeypatch) -> None:
    class AppProcess:
        pid = 123
        returncode = None

        @staticmethod
        def poll():
            return None

    requests: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_ux_collector,
        "_runtime_port_candidates",
        lambda _app_pid, _hinted_port: [54321],
    )

    def fake_request(port: int, _token: str, path: str, **_kwargs):
        requests.append((port, path))
        if path == "/api/health":
            return {
                "ok": True,
                "runtimeMode": "tauri-supervised",
                "pid": 456,
            }
        return {"ready": True, "lastSeen": {"pid": 456}}

    monkeypatch.setattr(app_ux_collector, "_request_json", fake_request)

    backend_pid, health, port = app_ux_collector._wait_runtime(
        AppProcess(),
        60000,
        "token",
        1,
    )

    assert (backend_pid, port) == (456, 54321)
    assert health["runtimeMode"] == "tauri-supervised"
    assert requests == [
        (54321, "/api/health"),
        (54321, "/api/runtime/frontend-ready"),
    ]
    assert "SCRIBER_WEB_PORT" not in app_ux_collector._runtime_environment(
        Path("runtime-data"),
        "token",
    )


def test_collector_prepare_only_pins_run_binary_harness_and_schema(tmp_path: Path) -> None:
    install_root = tmp_path / "installed"
    install_root.mkdir()
    executable = install_root / "scriber-desktop.exe"
    executable.write_bytes(b"installed-production-fixture")
    output = tmp_path / "evidence" / "app-ux.json"
    run_id = "6b8ae0e6fcb7445ab418d103bc9deab1"

    exit_code = app_ux_collector.main(
        [
            "--repo-root",
            str(endpoint_probe.REPO_ROOT),
            "--install-root",
            str(install_root),
            "--output",
            str(output),
            "--run-id",
            run_id,
            "--samples-per-scenario",
            "2",
            "--prepare-only",
        ]
    )

    assert exit_code == 0
    request = json.loads((output.parent / "app-ux-lifecycle-request.json").read_text(encoding="utf-8"))
    assert request["runId"] == run_id
    assert request["installedExeSha256"] == _sha(executable)
    assert request["harnessManifestSha256"] == endpoint_probe.app_ux_harness_manifest_sha256(endpoint_probe.REPO_ROOT)
    assert request["samplesPerScenario"] == 2
    assert request["requiredImportContract"] == endpoint_probe.APP_UX_LIFECYCLE_IMPORT_CONTRACT
    schema_path = endpoint_probe.REPO_ROOT / request["importSchema"]["path"]
    assert request["importSchema"]["sha256"] == _sha(schema_path)
    validated = app_ux_lifecycle_collector.load_and_validate_request(
        output.parent / "app-ux-lifecycle-request.json",
        repo_root=endpoint_probe.REPO_ROOT,
        install_root=install_root,
    )
    assert validated.run_id == run_id
    assert validated.installed_exe_sha256 == _sha(executable)
    assert validated.samples_per_scenario == 2


def test_lifecycle_collector_rejects_request_after_harness_drift(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "installed"
    install_root.mkdir()
    executable = install_root / "scriber-desktop.exe"
    executable.write_bytes(b"installed-production-fixture")
    request_path = tmp_path / "app-ux-lifecycle-request.json"
    request_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "contract": "b7-app-ux-lifecycle-request-v1",
                "runId": "6b8ae0e6fcb7445ab418d103bc9deab1",
                "installedExeSha256": _sha(executable),
                "harnessManifestSha256": "d" * 64,
                "samplesPerScenario": 1,
                "scenarioOrder": [
                    "stop_to_transcribing_visible",
                    "provider_result_to_completed_visible",
                    "session_finished_to_history_visible",
                ],
                "requiredImportContract": endpoint_probe.APP_UX_LIFECYCLE_IMPORT_CONTRACT,
                "importSchema": {
                    "path": "benchmarks/windows/app_ux_lifecycle_import.schema.json",
                    "sha256": _sha(endpoint_probe.REPO_ROOT / "benchmarks/windows/app_ux_lifecycle_import.schema.json"),
                },
                "semanticValidator": ("benchmarks.windows.endpoint_probe.validate_app_ux_lifecycle_import"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="harness hash drifted"):
        app_ux_lifecycle_collector.load_and_validate_request(
            request_path,
            repo_root=endpoint_probe.REPO_ROOT,
            install_root=install_root,
        )


def test_lifecycle_collector_binds_only_exact_installed_marker(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path, samples_per_scenario=1)
    sample = next(item for item in payload["samples"] if item["scenario"] == "provider_result_to_completed_visible")
    generation_path = tmp_path / sample["processGeneration"]["artifact"]["path"]
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    event_path = tmp_path / sample["eventEvidence"]["artifact"]["path"]
    event = json.loads(event_path.read_text(encoding="utf-8"))
    raw_marker = event["installedEvent"]

    bound = app_ux_lifecycle_collector.bind_installed_event(
        raw_marker,
        generation=generation,
        run_id=str(sample["runId"]),
        sample_id=str(sample["sampleId"]),
        scenario=str(sample["scenario"]),
    )

    assert bound["installedEvent"] == raw_marker
    assert bound["installedEvent"] is not raw_marker
    assert bound["providerReplayProcessGenerationFingerprint"] == raw_marker["processGenerationFingerprint"]
    assert bound["processGenerationFingerprint"] == generation["fingerprint"]

    missing_fingerprint = dict(raw_marker)
    missing_fingerprint.pop("processGenerationFingerprint")
    with pytest.raises(
        RuntimeError,
        match="replay process fingerprint",
    ):
        app_ux_lifecycle_collector.bind_installed_event(
            missing_fingerprint,
            generation=generation,
            run_id=str(sample["runId"]),
            sample_id=str(sample["sampleId"]),
            scenario=str(sample["scenario"]),
        )


def test_collector_uia_only_is_diagnostic_and_cannot_fabricate_lifecycle_evidence(tmp_path: Path, monkeypatch) -> None:
    install_root = tmp_path / "installed"
    install_root.mkdir()
    executable = install_root / "scriber-desktop.exe"
    executable.write_bytes(b"installed-production-fixture")
    output = tmp_path / "evidence" / "app-ux.json"

    def stop_before_windows_ui(*_args, **_kwargs):
        raise RuntimeError("diagnostic-stop-before-ui")

    monkeypatch.setattr(
        app_ux_collector,
        "_collect_uia_iteration",
        stop_before_windows_ui,
    )

    exit_code = app_ux_collector.main(
        [
            "--repo-root",
            str(endpoint_probe.REPO_ROOT),
            "--install-root",
            str(install_root),
            "--output",
            str(output),
            "--samples-per-scenario",
            "1",
            "--uia-only",
        ]
    )

    package = json.loads(output.read_text(encoding="utf-8"))
    validation = json.loads((output.parent / "app-ux-validation.json").read_text(encoding="utf-8"))
    assert exit_code == 2
    assert package["collector"]["diagnosticUiaOnly"] is True
    assert package["collector"]["lifecycleImport"] is None
    assert package["collector"]["failures"] == [{"iteration": 1, "error": "diagnostic-stop-before-ui"}]
    assert package["samples"] == []
    assert validation["metricEligible"] is False
    for scenario in endpoint_probe.APP_UX_LIFECYCLE_SCENARIOS:
        assert validation["scenarioResults"][scenario]["sampleCount"] == 0
