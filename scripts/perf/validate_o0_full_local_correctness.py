from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CONTRACT = "ScriberPython314O0FullLocalMeasurementValidationV1"
APP_UX_CONTRACT = "b7-app-ux-v1"
EXPECTED_APP_UX_SCENARIOS = (
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
EXPECTED_PROVIDERS = ("microsoft", "soniox", "speechmatics")
EXPECTED_DURATIONS_SECONDS = (5, 15, 30, 60)
EXPECTED_SAMPLE_PLAN = {
    "overlayCold": 15,
    "overlayWarm": 30,
    "providerReplay": 30,
    "providerReplayDurationsSeconds": list(EXPECTED_DURATIONS_SECONDS),
    "appUxPerScenario": 20,
}
EXPECTED_PROVIDER_CANDIDATE = {
    "azureMaiCaptureTimeMp3": "disabled",
    "speechmaticsCaptureTimeWav": "disabled",
}
PROTECTED_ERROR_METRICS = (
    "text_errors",
    "focus_errors",
    "clipboard_errors",
    "overlay_errors",
    "ui_long_tasks_gt_200ms",
)


class EvidenceError(ValueError):
    """Raised when an O0 correctness packet is incomplete or ambiguous."""


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{path} must be an object")
    return value


def _list(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"{path} must be an array")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{path} must be a non-empty string")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceError(f"{path} must be boolean")
    return value


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvidenceError(f"{path} must be an integer >= {minimum}")
    return value


def _number(value: Any, path: str, *, minimum: float = 0.0, exclusive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (exclusive and result == minimum):
        relation = ">" if exclusive else ">="
        raise EvidenceError(f"{path} must be {relation} {minimum}")
    return result


def _sha256(value: Any, path: str) -> str:
    result = _text(value, path).lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise EvidenceError(f"{path} must be a lowercase SHA-256")
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_equal(actual: Any, expected: Any, path: str) -> None:
    if actual != expected:
        raise EvidenceError(f"{path} does not match the O0 FullLocal correctness contract")


def _validate_attestation_binding(
    raw: Mapping[str, Any],
    *,
    expected_commit: str,
    expected_source_content_sha256: str,
    expected_attestation_id: str,
    expected_attestation_sha256: str,
    expected_app_ux_sha256: str,
) -> None:
    _require_equal(raw.get("sourceCommit"), expected_commit, "raw.sourceCommit")
    _require_equal(raw.get("runtimeAttestationPreValid"), True, "raw.runtimeAttestationPreValid")
    _require_equal(raw.get("runtimeAttestationPostValid"), True, "raw.runtimeAttestationPostValid")
    _require_equal(raw.get("runtimeAttestationDriftDetected"), False, "raw.runtimeAttestationDriftDetected")
    _require_equal(raw.get("appUxEvidenceDriftDetected"), False, "raw.appUxEvidenceDriftDetected")
    for phase in ("Pre", "Post"):
        _require_equal(
            raw.get(f"runtimeAttestation{phase}Id"),
            expected_attestation_id,
            f"raw.runtimeAttestation{phase}Id",
        )
        _require_equal(
            str(raw.get(f"runtimeAttestation{phase}ManifestSha256") or "").lower(),
            expected_attestation_sha256,
            f"raw.runtimeAttestation{phase}ManifestSha256",
        )
        _require_equal(
            raw.get(f"runtimeAttestation{phase}SourceContentSha256"),
            expected_source_content_sha256,
            f"raw.runtimeAttestation{phase}SourceContentSha256",
        )
    _require_equal(
        str(raw.get("appUxEvidenceSha256") or "").lower(),
        expected_app_ux_sha256,
        "raw.appUxEvidenceSha256",
    )
    _require_equal(
        str(raw.get("appUxEvidencePostSha256") or "").lower(),
        expected_app_ux_sha256,
        "raw.appUxEvidencePostSha256",
    )


def _validate_overlay(evidence: Mapping[str, Any]) -> dict[str, int]:
    overlay = _object(evidence.get("overlayHotkey"), "endpoint.evidence.overlayHotkey")
    _require_equal(overlay.get("attempted"), True, "overlay.attempted")
    _require_equal(overlay.get("metricEligible"), True, "overlay.metricEligible")
    _require_equal(overlay.get("sameProcessWarmContract"), True, "overlay.sameProcessWarmContract")
    _require_equal(overlay.get("externalVisibleFrameObserved"), True, "overlay.externalVisibleFrameObserved")
    _require_equal(
        dict(_object(overlay.get("requestedSamples"), "overlay.requestedSamples")),
        {"cold": 15, "warm": 30},
        "overlay.requestedSamples",
    )
    series = _list(overlay.get("series"), "overlay.series")
    _require_equal(len(series), 15, "overlay.series count")
    for index, raw_series in enumerate(series):
        series_value = _object(raw_series, f"overlay.series[{index}]")
        _require_equal(series_value.get("ok"), True, f"overlay.series[{index}].ok")
        cleanup = _object(series_value.get("cleanup"), f"overlay.series[{index}].cleanup")
        _require_equal(cleanup.get("ok"), True, f"overlay.series[{index}].cleanup.ok")

    samples = _list(overlay.get("samples"), "overlay.samples")
    scenarios: Counter[str] = Counter()
    for index, raw_sample in enumerate(samples):
        sample = _object(raw_sample, f"overlay.samples[{index}]")
        scenario = _text(sample.get("scenario"), f"overlay.samples[{index}].scenario")
        if scenario not in {"overlay_cold", "overlay_warm"}:
            raise EvidenceError(f"overlay.samples[{index}].scenario is invalid")
        scenarios[scenario] += 1
        for field in (
            "metricEligible",
            "externalVisibleFrameObserved",
            "expectedOverlayTargetObserved",
            "sessionCompleted",
            "terminalSuccessful",
            "focusPreserved",
            "processGenerationBeforeMatches",
            "processGenerationAfterMatches",
        ):
            _require_equal(sample.get(field), True, f"overlay.samples[{index}].{field}")
        _number(sample.get("durationMs"), f"overlay.samples[{index}].durationMs", exclusive=True)
        _require_equal(
            list(_list(sample.get("metricBlockedReasons"), f"overlay.samples[{index}].metricBlockedReasons")),
            [],
            f"overlay.samples[{index}].metricBlockedReasons",
        )
    expected = Counter({"overlay_cold": 15, "overlay_warm": 30})
    _require_equal(scenarios, expected, "overlay sample distribution")

    metrics = _object(overlay.get("metrics"), "overlay.metrics")
    _require_equal(metrics.get("overlay_cold_sample_count"), 15, "overlay.metrics.overlay_cold_sample_count")
    _require_equal(metrics.get("overlay_warm_sample_count"), 30, "overlay.metrics.overlay_warm_sample_count")
    for field in (
        "overlay_cold_p50_ms",
        "overlay_cold_p95_ms",
        "overlay_warm_p50_ms",
        "overlay_warm_p95_ms",
        "hotkey_mic_ready_p95_ms",
        "hotkey_first_audio_frame_p95_ms",
    ):
        _number(metrics.get(field), f"overlay.metrics.{field}", exclusive=True)
    return {"cold": scenarios["overlay_cold"], "warm": scenarios["overlay_warm"]}


def _validate_app_ux(
    evidence: Mapping[str, Any],
    app_ux: Mapping[str, Any],
    *,
    expected_app_ux_sha256: str,
) -> dict[str, Any]:
    _require_equal(app_ux.get("contract"), APP_UX_CONTRACT, "appUx.contract")
    _require_equal(app_ux.get("schemaVersion"), 1, "appUx.schemaVersion")
    _require_equal(app_ux.get("samplesPerScenario"), 20, "appUx.samplesPerScenario")
    _sha256(app_ux.get("installedExeSha256"), "appUx.installedExeSha256")
    app_samples = _list(app_ux.get("samples"), "appUx.samples")
    _require_equal(len(app_samples), 180, "appUx.samples count")
    app_distribution = Counter(
        _text(_object(sample, f"appUx.samples[{index}]").get("scenario"), f"appUx.samples[{index}].scenario")
        for index, sample in enumerate(app_samples)
    )
    _require_equal(
        app_distribution,
        Counter({scenario: 20 for scenario in EXPECTED_APP_UX_SCENARIOS}),
        "appUx sample distribution",
    )

    app_frame = _object(evidence.get("appFrame"), "endpoint.evidence.appFrame")
    _require_equal(app_frame.get("contract"), APP_UX_CONTRACT, "appFrame.contract")
    _require_equal(app_frame.get("attempted"), True, "appFrame.attempted")
    _require_equal(app_frame.get("metricEligible"), True, "appFrame.metricEligible")
    _require_equal(app_frame.get("externalStableFrameObserved"), True, "appFrame.externalStableFrameObserved")
    _require_equal(app_frame.get("requestedSamplesPerScenario"), 20, "appFrame.requestedSamplesPerScenario")
    _require_equal(app_frame.get("requiredScenarioCount"), 9, "appFrame.requiredScenarioCount")
    _require_equal(
        list(_list(app_frame.get("scenarioOrder"), "appFrame.scenarioOrder")),
        list(EXPECTED_APP_UX_SCENARIOS),
        "appFrame.scenarioOrder",
    )
    _require_equal(
        list(_list(app_frame.get("invalidSamples"), "appFrame.invalidSamples")), [], "appFrame.invalidSamples"
    )
    _require_equal(app_frame.get("installedExeSha256"), app_ux.get("installedExeSha256"), "appFrame installed exe")
    artifact = _object(app_frame.get("evidenceArtifact"), "appFrame.evidenceArtifact")
    _require_equal(
        str(artifact.get("sha256") or "").lower(),
        expected_app_ux_sha256,
        "appFrame.evidenceArtifact.sha256",
    )

    scenario_results = _object(app_frame.get("scenarioResults"), "appFrame.scenarioResults")
    _require_equal(set(scenario_results), set(EXPECTED_APP_UX_SCENARIOS), "appFrame.scenarioResults keys")
    for scenario in EXPECTED_APP_UX_SCENARIOS:
        result = _object(scenario_results[scenario], f"appFrame.scenarioResults.{scenario}")
        _require_equal(result.get("sampleCount"), 20, f"appFrame.scenarioResults.{scenario}.sampleCount")
        _require_equal(
            result.get("requiredSampleCount"),
            20,
            f"appFrame.scenarioResults.{scenario}.requiredSampleCount",
        )
        _require_equal(result.get("metricEligible"), True, f"appFrame.scenarioResults.{scenario}.metricEligible")
        _number(result.get("p50Ms"), f"appFrame.scenarioResults.{scenario}.p50Ms", exclusive=True)
        _number(result.get("p95Ms"), f"appFrame.scenarioResults.{scenario}.p95Ms", exclusive=True)

    resources = _object(app_frame.get("resourceMetrics"), "appFrame.resourceMetrics")
    _require_equal(resources.get("ui_long_tasks_gt_200ms"), 0, "appFrame.resourceMetrics.ui_long_tasks_gt_200ms")
    idle_cpu = _number(resources.get("idle_cpu_pct"), "appFrame.resourceMetrics.idle_cpu_pct")
    working_set = _number(resources.get("working_set_mb"), "appFrame.resourceMetrics.working_set_mb", exclusive=True)
    return {
        "scenarioCount": 9,
        "samplesPerScenario": 20,
        "sampleCount": 180,
        "idleCpuPercent": idle_cpu,
        "workingSetMb": working_set,
    }


def _validate_provider_replay(evidence: Mapping[str, Any]) -> dict[str, Any]:
    replay = _object(evidence.get("providerReplay"), "endpoint.evidence.providerReplay")
    for field in ("attempted", "metricEligible", "ok", "installedRuntimeOnly"):
        _require_equal(replay.get(field), True, f"providerReplay.{field}")
    _require_equal(
        replay.get("requestedSamplesPerProviderDuration"),
        30,
        "providerReplay.requestedSamplesPerProviderDuration",
    )
    _require_equal(
        dict(_object(replay.get("providerCandidate"), "providerReplay.providerCandidate")),
        EXPECTED_PROVIDER_CANDIDATE,
        "providerReplay.providerCandidate",
    )
    for field in ("textErrors", "focusErrors", "clipboardErrors"):
        _require_equal(replay.get(field), 0, f"providerReplay.{field}")

    expected_series = {
        (provider, duration) for provider in EXPECTED_PROVIDERS for duration in EXPECTED_DURATIONS_SECONDS
    }
    observed_series: set[tuple[str, int]] = set()
    series = _list(replay.get("series"), "providerReplay.series")
    _require_equal(len(series), 12, "providerReplay.series count")
    for index, raw_series in enumerate(series):
        value = _object(raw_series, f"providerReplay.series[{index}]")
        provider = _text(value.get("provider"), f"providerReplay.series[{index}].provider")
        fixture_duration_ms = _number(
            value.get("fixtureDurationMs"),
            f"providerReplay.series[{index}].fixtureDurationMs",
            exclusive=True,
        )
        duration_seconds = int(round(fixture_duration_ms / 1000.0))
        observed_series.add((provider, duration_seconds))
        for field in ("requestedSamples", "measuredSamples"):
            _require_equal(value.get(field), 30, f"providerReplay.series[{index}].{field}")
        for field in ("captureFixtureAttested", "runtimeStarted", "ok"):
            _require_equal(value.get(field), True, f"providerReplay.series[{index}].{field}")
        cleanup = _object(value.get("cleanup"), f"providerReplay.series[{index}].cleanup")
        _require_equal(cleanup.get("ok"), True, f"providerReplay.series[{index}].cleanup.ok")
        _require_equal(value.get("reason"), "measured", f"providerReplay.series[{index}].reason")
    _require_equal(observed_series, expected_series, "providerReplay series matrix")

    results = _list(replay.get("results"), "providerReplay.results")
    _require_equal(len(results), 360, "providerReplay.results count")
    sample_distribution: Counter[tuple[str, int]] = Counter()
    for index, raw_sample in enumerate(results):
        sample = _object(raw_sample, f"providerReplay.results[{index}]")
        provider = _text(sample.get("provider"), f"providerReplay.results[{index}].provider")
        duration_ms = _number(
            sample.get("fixtureDurationMs"),
            f"providerReplay.results[{index}].fixtureDurationMs",
            exclusive=True,
        )
        duration_seconds = int(round(duration_ms / 1000.0))
        sample_distribution[(provider, duration_seconds)] += 1
        for field in (
            "metricEligible",
            "targetTextObserved",
            "captureFixtureAttested",
            "clipboardOk",
            "pasteOk",
            "generationAfterMatches",
        ):
            _require_equal(sample.get(field), True, f"providerReplay.results[{index}].{field}")
        _require_equal(
            list(_list(sample.get("reasons"), f"providerReplay.results[{index}].reasons")),
            [],
            f"providerReplay.results[{index}].reasons",
        )
        _number(
            sample.get("activationReceivedToFinalTextObservedMs"),
            f"providerReplay.results[{index}].activationReceivedToFinalTextObservedMs",
            exclusive=True,
        )
        _number(
            sample.get("stopRequestedToFinalTextObservedMs"),
            f"providerReplay.results[{index}].stopRequestedToFinalTextObservedMs",
            exclusive=True,
        )
    _require_equal(
        sample_distribution,
        Counter({series_key: 30 for series_key in expected_series}),
        "providerReplay sample distribution",
    )
    return {
        "providerCount": 3,
        "durationSeconds": list(EXPECTED_DURATIONS_SECONDS),
        "samplesPerProviderDuration": 30,
        "seriesCount": 12,
        "sampleCount": 360,
    }


def validate(
    raw: Mapping[str, Any],
    app_ux: Mapping[str, Any],
    *,
    expected_commit: str,
    expected_source_content_sha256: str,
    expected_attestation_id: str,
    expected_attestation_sha256: str,
    expected_app_ux_sha256: str,
) -> dict[str, Any]:
    if len(expected_commit) != 40 or any(character not in "0123456789abcdef" for character in expected_commit):
        raise EvidenceError("expected_commit must be a lowercase Git SHA")
    expected_source_content_sha256 = _sha256(
        expected_source_content_sha256,
        "expected_source_content_sha256",
    )
    expected_attestation_id = _sha256(expected_attestation_id, "expected_attestation_id")
    expected_attestation_sha256 = _sha256(expected_attestation_sha256, "expected_attestation_sha256")
    expected_app_ux_sha256 = _sha256(expected_app_ux_sha256, "expected_app_ux_sha256")

    _require_equal(raw.get("schemaVersion"), 1, "raw.schemaVersion")
    _require_equal(raw.get("suite"), "FullLocal", "raw.suite")
    _validate_attestation_binding(
        raw,
        expected_commit=expected_commit,
        expected_source_content_sha256=expected_source_content_sha256,
        expected_attestation_id=expected_attestation_id,
        expected_attestation_sha256=expected_attestation_sha256,
        expected_app_ux_sha256=expected_app_ux_sha256,
    )
    endpoint = _object(raw.get("endpointProbe"), "raw.endpointProbe")
    _require_equal(endpoint.get("schemaVersion"), 1, "endpoint.schemaVersion")
    _require_equal(endpoint.get("suite"), "FullLocal", "endpoint.suite")
    _require_equal(
        dict(_object(endpoint.get("samplePlan"), "endpoint.samplePlan")), EXPECTED_SAMPLE_PLAN, "endpoint.samplePlan"
    )

    evidence = _object(endpoint.get("evidence"), "endpoint.evidence")
    overlay_counts = _validate_overlay(evidence)
    app_ux_counts = _validate_app_ux(
        evidence,
        app_ux,
        expected_app_ux_sha256=expected_app_ux_sha256,
    )
    provider_counts = _validate_provider_replay(evidence)

    metrics = _object(endpoint.get("metrics"), "endpoint.metrics")
    protected_errors: dict[str, int] = {}
    for field in PROTECTED_ERROR_METRICS:
        value = _integer(metrics.get(field), f"endpoint.metrics.{field}")
        if value != 0:
            raise EvidenceError(f"endpoint.metrics.{field} must be zero")
        protected_errors[field] = value

    endpoint_status = _text(endpoint.get("status"), "endpoint.status")
    endpoint_reason = _text(endpoint.get("reason"), "endpoint.reason")
    endpoint_promotion = _boolean(endpoint.get("promotionEligible"), "endpoint.promotionEligible")
    if endpoint_promotion:
        _require_equal(endpoint_status, "MEASURED", "endpoint.status")
    else:
        _require_equal(endpoint_status, "PARTIAL", "endpoint.status")
        _require_equal(
            endpoint_reason,
            "canonical_provider_replay_not_promotion_eligible",
            "endpoint.reason",
        )
    _require_equal(raw.get("status"), endpoint_status, "raw.status")
    _require_equal(raw.get("reason"), endpoint_reason, "raw.reason")

    return {
        "contract": CONTRACT,
        "schemaVersion": 1,
        "ok": True,
        "scope": "o0-fallback-correctness-only",
        "suite": "FullLocal",
        "correctnessEligible": True,
        "promotionEligible": False,
        "productionPromotionAuthorized": False,
        "rawStatus": endpoint_status,
        "rawReason": endpoint_reason,
        "measurementCounts": {
            "cold": overlay_counts["cold"],
            "warm": overlay_counts["warm"],
            "providerReplayPerProviderAndDuration": provider_counts["samplesPerProviderDuration"],
            "providerReplaySeries": provider_counts["seriesCount"],
            "providerReplaySamples": provider_counts["sampleCount"],
            "appUxPerScenario": app_ux_counts["samplesPerScenario"],
            "appUxScenarioCount": app_ux_counts["scenarioCount"],
            "appUxSamples": app_ux_counts["sampleCount"],
        },
        "resources": {
            "idleCpuPercent": app_ux_counts["idleCpuPercent"],
            "workingSetMb": app_ux_counts["workingSetMb"],
        },
        "protectedErrors": protected_errors,
        "limitations": {
            "a13Measured": False,
            "counterbalancedAbBa": False,
            "rebootBlocks": 0,
            "promotionProfileSatisfied": False,
            "unreportedPromotionErrorFields": ["frame", "token"],
        },
    }


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"could not load {label}: {exc}") from exc
    return dict(_object(value, label))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate installed O0 FullLocal correctness evidence.")
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--app-ux", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-source-content-sha256", required=True)
    parser.add_argument("--expected-attestation-id", required=True)
    parser.add_argument("--expected-attestation-sha256", required=True)
    parser.add_argument("--expected-app-ux-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        raw_path = arguments.raw.resolve()
        app_ux_path = arguments.app_ux.resolve()
        expected_app_ux_sha256 = _sha256(arguments.expected_app_ux_sha256, "expected_app_ux_sha256")
        if _file_sha256(app_ux_path) != expected_app_ux_sha256:
            raise EvidenceError("app UX file SHA-256 does not match the expected binding")
        result = validate(
            _load_json(raw_path, "raw FullLocal evidence"),
            _load_json(app_ux_path, "App UX evidence"),
            expected_commit=arguments.expected_commit,
            expected_source_content_sha256=arguments.expected_source_content_sha256,
            expected_attestation_id=arguments.expected_attestation_id,
            expected_attestation_sha256=arguments.expected_attestation_sha256,
            expected_app_ux_sha256=expected_app_ux_sha256,
        )
        result["artifacts"] = {
            "rawSha256": _file_sha256(raw_path),
            "appUxSha256": expected_app_ux_sha256,
        }
        _write_json(arguments.output.resolve(), result)
    except EvidenceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
