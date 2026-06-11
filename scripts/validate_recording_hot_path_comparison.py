from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.measure_recording_hot_path_baseline import (
    AUDIBLE_AUDIO_SEGMENT,
    CLIPBOARD_SET_TO_PASTE_SEGMENT,
    LAST_CHUNK_TO_PROVIDER_FINAL_SEGMENT,
    PASTE_TO_FIRST_PASTE_SEGMENT,
    PROVIDER_FINAL_TO_CLIPBOARD_SET_SEGMENT,
    PROVIDER_TRANSCRIPT_SEGMENT,
    RUST_AUDIO_ACTIVE_ENGINE,
    RUST_AUDIO_FRAME_SOURCE,
    STOP_TO_LAST_CHUNK_SEGMENT,
    STOP_TO_PROVIDER_FINAL_SEGMENT,
    STOP_TO_TEXT_SEGMENT,
    summarize,
)


COMPARISON_SEGMENTS = [
    "hotkey_received_to_mic_ready_ms",
    "hotkey_received_to_first_audio_frame_ms",
    AUDIBLE_AUDIO_SEGMENT,
    PROVIDER_TRANSCRIPT_SEGMENT,
    STOP_TO_LAST_CHUNK_SEGMENT,
    STOP_TO_PROVIDER_FINAL_SEGMENT,
    LAST_CHUNK_TO_PROVIDER_FINAL_SEGMENT,
    PROVIDER_FINAL_TO_CLIPBOARD_SET_SEGMENT,
    CLIPBOARD_SET_TO_PASTE_SEGMENT,
    PASTE_TO_FIRST_PASTE_SEGMENT,
    STOP_TO_TEXT_SEGMENT,
]

AUDIO_OWNED_LATENCY_SEGMENTS = [
    "hotkey_received_to_mic_ready_ms",
    "hotkey_received_to_first_audio_frame_ms",
    AUDIBLE_AUDIO_SEGMENT,
    STOP_TO_LAST_CHUNK_SEGMENT,
]
DEFAULT_AUDIO_OWNED_MAX_P95_REGRESSION_MS = 50.0
REDACTED_TEXT_MARKERS = {"[REDACTED]", "[redacted]", "<redacted>", "***REDACTED***"}
REDACTED_ENDPOINT_MARKERS = {"[REDACTED_ENDPOINT]", "[redacted-endpoint]", "<redacted-endpoint>"}
COMPARABLE_REQUESTED_FIELDS = [
    "iterations",
    "recordSeconds",
    "speechPromptText",
    "speechPromptDelaySec",
    "requireTextTarget",
    "requireProviderTranscript",
    "textTargetTitle",
    "textTargetSettleSec",
    "textTargetTimeoutSec",
]


def read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} root must be a JSON object")
    return payload


def requirement_status(report: dict[str, Any], name: str) -> str:
    requirements = ((report.get("summary") or {}).get("requirements") or {})
    requirement = requirements.get(name) if isinstance(requirements, dict) else None
    if not isinstance(requirement, dict):
        return "missing"
    return str(requirement.get("status") or "missing")


def segment_values(report: dict[str, Any], segment: str) -> list[float]:
    values: list[float] = []
    samples = report.get("samples")
    if not isinstance(samples, list):
        return values
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        segments = sample.get("segments")
        if not isinstance(segments, dict) or segment not in segments:
            continue
        try:
            values.append(float(segments[segment]))
        except (TypeError, ValueError):
            continue
    return values


def report_samples(report: dict[str, Any]) -> list[dict[str, Any]]:
    samples = report.get("samples")
    if not isinstance(samples, list):
        return []
    return [sample for sample in samples if isinstance(sample, dict)]


def report_contains_validate_only(report: dict[str, Any]) -> bool:
    return any(sample.get("validateOnly") is True for sample in report_samples(report))


def active_capture_samples(report: dict[str, Any]) -> list[dict[str, Any]]:
    captures: list[dict[str, Any]] = []
    for sample in report_samples(report):
        during = sample.get("audioDiagnosticsDuringRecording")
        if not isinstance(during, dict):
            continue
        active = ((during.get("microphone") or {}).get("activeCapture") or {})
        if isinstance(active, dict) and active:
            captures.append(active)
    return captures


def rust_fallback_circuits(report: dict[str, Any]) -> list[dict[str, Any]]:
    circuits: list[dict[str, Any]] = []
    report_circuit = ((report.get("audioDiagnostics") or {}).get("microphone") or {}).get(
        "rustAudioFallbackCircuit"
    )
    if isinstance(report_circuit, dict):
        circuits.append({"source": "report", **report_circuit})
    for sample in report_samples(report):
        during = sample.get("audioDiagnosticsDuringRecording")
        if not isinstance(during, dict):
            continue
        circuit = ((during.get("microphone") or {}).get("rustAudioFallbackCircuit") or {})
        if isinstance(circuit, dict) and circuit:
            circuits.append({"source": f"sample:{sample.get('iteration')}", **circuit})
    return circuits


def microphone_status_snapshots(report: dict[str, Any]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    report_microphone = (report.get("audioDiagnostics") or {}).get("microphone")
    if isinstance(report_microphone, dict):
        snapshots.append({"source": "report", **report_microphone})
    for sample in report_samples(report):
        during = sample.get("audioDiagnosticsDuringRecording")
        if not isinstance(during, dict):
            continue
        microphone = during.get("microphone")
        if isinstance(microphone, dict):
            snapshots.append({"source": f"sample:{sample.get('iteration')}", **microphone})
    return snapshots


def rust_always_on_mic_check(report: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    snapshots = microphone_status_snapshots(report)
    always_on_snapshots = [
        {
            "source": snapshot.get("source"),
            "micAlwaysOn": snapshot.get("micAlwaysOn"),
            "idlePrewarmActive": snapshot.get("idlePrewarmActive"),
            "prewarmEngine": snapshot.get("prewarmEngine"),
        }
        for snapshot in snapshots
        if snapshot.get("micAlwaysOn") is True
    ]
    return bool(always_on_snapshots), {
        "snapshotCount": len(snapshots),
        "micAlwaysOnSnapshotCount": len(always_on_snapshots),
        "snapshots": always_on_snapshots[:5],
    }


def has_open_rust_fallback_circuit(report: dict[str, Any]) -> bool:
    return any(circuit.get("open") is True for circuit in rust_fallback_circuits(report))


def has_rust_frame_pipe(report: dict[str, Any]) -> bool:
    return any(
        capture.get("engine") == RUST_AUDIO_ACTIVE_ENGINE
        and capture.get("frameSource") == RUST_AUDIO_FRAME_SOURCE
        for capture in active_capture_samples(report)
    )


def _normalized_windows_identifier(value: str) -> str:
    normalized = str(value).lower().replace("/", "\\")
    for _ in range(6):
        collapsed = normalized.replace("\\\\", "\\")
        if collapsed == normalized:
            break
        normalized = collapsed
    return normalized


def _looks_like_raw_scriber_pipe(value: str) -> bool:
    return "\\.\\pipe\\scriber-" in _normalized_windows_identifier(value)


def _looks_like_raw_native_endpoint_id(value: str) -> bool:
    normalized = _normalized_windows_identifier(value)
    return "swd\\mmdevapi\\" in normalized or "swd#mmdevapi#" in str(value).lower()


def _looks_like_unredacted_endpoint_id_field(path: str, value: str) -> bool:
    tokens = [token for token in re.split(r"[.\[\]]+", path.lower()) if token]
    if not any(token.endswith("endpointid") and not token.endswith("hash") for token in tokens):
        return False
    normalized = str(value).strip()
    return bool(normalized) and normalized not in REDACTED_ENDPOINT_MARKERS


def _looks_like_unredacted_token_field(path: str, value: str) -> bool:
    path_lower = path.lower()
    if "tokenconfigured" in path_lower:
        return False
    if "token" not in path_lower:
        return False
    normalized = str(value).strip()
    return bool(normalized) and normalized not in REDACTED_TEXT_MARKERS


def find_input_redaction_failures(value: Any, label: str) -> list[str]:
    failures: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                key_str = str(key)
                child_path = f"{path}.{key_str}" if path else key_str
                walk(item, child_path)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            if _looks_like_raw_scriber_pipe(node):
                failures.append(f"{label} contains raw Scriber pipe name at {path}")
            if _looks_like_raw_native_endpoint_id(node):
                failures.append(f"{label} contains raw native endpoint ID at {path}")
            if _looks_like_unredacted_endpoint_id_field(path, node):
                failures.append(f"{label} contains unredacted endpointId value at {path}")
            if _looks_like_unredacted_token_field(path, node):
                failures.append(f"{label} contains unredacted token-like value at {path}")

    walk(value, "")
    return failures


def audio_engine(report: dict[str, Any]) -> str:
    flags = (report.get("audioDiagnostics") or {}).get("featureFlags") or {}
    if isinstance(flags, dict):
        return str(flags.get("audioEngine") or "")
    return ""


def provider_label(report: dict[str, Any]) -> str:
    provider = (report.get("audioDiagnostics") or {}).get("provider") or {}
    if isinstance(provider, dict):
        return str(provider.get("active") or provider.get("configured") or "")
    return ""


def comparable_recording_config(report: dict[str, Any]) -> dict[str, Any] | None:
    requested = report.get("requested")
    if not isinstance(requested, dict):
        return None
    return {field: requested.get(field) for field in COMPARABLE_REQUESTED_FIELDS}


def same_recording_config_check(
    python_report: dict[str, Any],
    rust_report: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    python_config = comparable_recording_config(python_report)
    rust_config = comparable_recording_config(rust_report)
    missing = []
    if python_config is None:
        missing.append("python.requested")
        python_config = {}
    if rust_config is None:
        missing.append("rust.requested")
        rust_config = {}
    mismatched_fields = [
        field
        for field in COMPARABLE_REQUESTED_FIELDS
        if python_config.get(field) != rust_config.get(field)
    ]
    details = {
        "fields": COMPARABLE_REQUESTED_FIELDS,
        "missing": missing,
        "mismatchedFields": mismatched_fields,
        "python": python_config,
        "rust": rust_config,
    }
    return not missing and not mismatched_fields, details


def compare_segment(python_values: list[float], rust_values: list[float]) -> dict[str, Any]:
    python_summary = summarize(python_values)
    rust_summary = summarize(rust_values)
    python_p95 = float(python_summary.get("p95Ms") or 0.0)
    rust_p95 = float(rust_summary.get("p95Ms") or 0.0)
    python_mean = float(python_summary.get("meanMs") or 0.0)
    rust_mean = float(rust_summary.get("meanMs") or 0.0)
    speedup_pct = 0.0
    if python_p95 > 0:
        speedup_pct = round(((python_p95 - rust_p95) / python_p95) * 100.0, 3)
    return {
        "python": python_summary,
        "rust": rust_summary,
        "rustMinusPythonMeanMs": round(rust_mean - python_mean, 3),
        "rustMinusPythonP95Ms": round(rust_p95 - python_p95, 3),
        "rustSpeedupP95Pct": speedup_pct,
        "complete": bool(python_values and rust_values),
    }


def audio_owned_latency_regression_check(
    segments: dict[str, Any],
    *,
    max_p95_regression_ms: float,
) -> tuple[bool, dict[str, Any]]:
    details: dict[str, Any] = {
        "maxAllowedRustP95RegressionMs": max_p95_regression_ms,
        "gatedSegments": {},
    }
    ok = True
    for name in AUDIO_OWNED_LATENCY_SEGMENTS:
        segment = segments.get(name)
        complete = isinstance(segment, dict) and segment.get("complete") is True
        rust_minus_python_p95 = None
        if isinstance(segment, dict) and isinstance(segment.get("rustMinusPythonP95Ms"), (int, float)):
            rust_minus_python_p95 = float(segment["rustMinusPythonP95Ms"])
        segment_ok = complete and rust_minus_python_p95 is not None and rust_minus_python_p95 <= max_p95_regression_ms
        details["gatedSegments"][name] = {
            "complete": complete,
            "rustMinusPythonP95Ms": rust_minus_python_p95,
            "ok": segment_ok,
        }
        ok = ok and segment_ok
    return ok, details


def build_comparison(
    python_report: dict[str, Any],
    rust_report: dict[str, Any],
    *,
    require_provider_transcript: bool = True,
    require_rust_audio: bool = True,
    require_python_engine: bool = True,
    allow_validate_only: bool = False,
    min_samples_per_report: int = 1,
    require_audio_owned_latency_no_regression: bool = True,
    max_audio_owned_p95_regression_ms: float = DEFAULT_AUDIO_OWNED_MAX_P95_REGRESSION_MS,
) -> dict[str, Any]:
    failures: list[str] = []
    checks: list[dict[str, Any]] = []

    def add_check(name: str, ok: bool, detail: dict[str, Any] | None = None, failure: str = "") -> None:
        checks.append({"name": name, "ok": ok, "details": detail or {}})
        if not ok and failure:
            failures.append(failure)

    add_check(
        "pythonReportOk",
        python_report.get("ok") is True,
        {"ok": python_report.get("ok"), "schemaVersion": python_report.get("schemaVersion")},
        "Python recording hot-path report ok must be true",
    )
    add_check(
        "rustReportOk",
        rust_report.get("ok") is True,
        {"ok": rust_report.get("ok"), "schemaVersion": rust_report.get("schemaVersion")},
        "Rust recording hot-path report ok must be true",
    )
    python_sample_count = len(report_samples(python_report))
    rust_sample_count = len(report_samples(rust_report))
    min_samples = max(1, int(min_samples_per_report))
    add_check(
        "sampleCount",
        python_sample_count >= min_samples and rust_sample_count >= min_samples,
        {
            "pythonSamples": python_sample_count,
            "rustSamples": rust_sample_count,
            "minSamplesPerReport": min_samples,
        },
        f"Python and Rust reports must each include at least {min_samples} sample(s)",
    )
    if not allow_validate_only:
        add_check(
            "physicalReports",
            not report_contains_validate_only(python_report) and not report_contains_validate_only(rust_report),
            {
                "pythonValidateOnly": report_contains_validate_only(python_report),
                "rustValidateOnly": report_contains_validate_only(rust_report),
            },
            "Recording hot-path comparison reports must not be validate-only artifacts",
        )

    input_redaction_failures = find_input_redaction_failures(
        python_report, "Python recording hot-path report"
    ) + find_input_redaction_failures(rust_report, "Rust recording hot-path report")
    add_check(
        "inputReportRedaction",
        not input_redaction_failures,
        {
            "failureCount": len(input_redaction_failures),
            "failures": input_redaction_failures[:10],
        },
        "Recording hot-path comparison input reports must be redacted",
    )
    if input_redaction_failures:
        failures.extend(input_redaction_failures)

    if require_provider_transcript:
        python_provider = requirement_status(python_report, "provider_transcript")
        rust_provider = requirement_status(rust_report, "provider_transcript")
        add_check(
            "providerTranscript",
            python_provider == "measured" and rust_provider == "measured",
            {"pythonStatus": python_provider, "rustStatus": rust_provider},
            "Both Python and Rust reports must measure provider_transcript",
        )
        python_provider_label = provider_label(python_report)
        rust_provider_label = provider_label(rust_report)
        add_check(
            "sameProvider",
            bool(python_provider_label)
            and python_provider_label == rust_provider_label,
            {
                "pythonProvider": python_provider_label,
                "rustProvider": rust_provider_label,
            },
            "Python and Rust reports must use the same STT provider",
        )
        same_config, config_details = same_recording_config_check(python_report, rust_report)
        add_check(
            "sameRecordingConfig",
            same_config,
            config_details,
            "Python and Rust reports must use the same recording benchmark configuration",
        )

    if require_rust_audio:
        rust_status = requirement_status(rust_report, "rust_audio_engine")
        add_check(
            "rustAudioEngine",
            rust_status == "measured" and has_rust_frame_pipe(rust_report),
            {
                "status": rust_status,
                "hasRustFramePipe": has_rust_frame_pipe(rust_report),
                "activeCaptureSamples": len(active_capture_samples(rust_report)),
            },
            "Rust report must prove active rust-prototype rust-frame-pipe capture",
        )
        circuits = rust_fallback_circuits(rust_report)
        add_check(
            "rustFallbackCircuitClosed",
            not has_open_rust_fallback_circuit(rust_report),
            {
                "open": has_open_rust_fallback_circuit(rust_report),
                "circuits": circuits[:5],
            },
            "Rust report must not have an open Rust audio fallback circuit",
        )
        always_on_ok, always_on_details = rust_always_on_mic_check(rust_report)
        add_check(
            "rustAlwaysOnMic",
            always_on_ok,
            always_on_details,
            "Rust report must prove MIC always-on was enabled during provider-backed comparison",
        )

    if require_python_engine:
        engine = audio_engine(python_report)
        active_engines = sorted(
            {
                str(capture.get("engine") or "")
                for capture in active_capture_samples(python_report)
                if capture.get("engine")
            }
        )
        add_check(
            "pythonAudioEngine",
            engine in {"", "python"} and RUST_AUDIO_ACTIVE_ENGINE not in active_engines,
            {"audioEngine": engine, "activeCaptureEngines": active_engines},
            "Python report must not use the Rust audio engine",
        )

    segments: dict[str, Any] = {}
    for segment in COMPARISON_SEGMENTS:
        segments[segment] = compare_segment(
            segment_values(python_report, segment),
            segment_values(rust_report, segment),
        )

    if require_audio_owned_latency_no_regression:
        latency_ok, latency_details = audio_owned_latency_regression_check(
            segments,
            max_p95_regression_ms=max_audio_owned_p95_regression_ms,
        )
        add_check(
            "audioOwnedLatencyNoRegression",
            latency_ok,
            latency_details,
            (
                "Rust audio-owned hot-path P95 latency must not regress by more than "
                f"{max_audio_owned_p95_regression_ms} ms on any gated segment"
            ),
        )

    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": not failures,
        "failures": failures,
        "checks": checks,
        "reports": {
            "python": {
                "provider": provider_label(python_report),
                "audioEngine": audio_engine(python_report) or "python",
                "samples": len(report_samples(python_report)),
                "requested": comparable_recording_config(python_report) or {},
            },
            "rust": {
                "provider": provider_label(rust_report),
                "audioEngine": audio_engine(rust_report),
                "samples": len(report_samples(rust_report)),
                "requested": comparable_recording_config(rust_report) or {},
                "micAlwaysOn": rust_always_on_mic_check(rust_report)[0],
            },
        },
        "segments": segments,
        "summary": {
            "completeSegmentCount": sum(1 for segment in segments.values() if segment["complete"]),
            "comparedSegmentCount": len(segments),
            "stopToTextInjection": segments[STOP_TO_TEXT_SEGMENT],
            "providerTranscript": segments[PROVIDER_TRANSCRIPT_SEGMENT],
            "hotkeyToFirstAudioFrame": segments["hotkey_received_to_first_audio_frame_ms"],
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate provider-backed Python-vs-Rust recording hot-path comparison reports."
    )
    parser.add_argument("--python-report", required=True)
    parser.add_argument("--rust-report", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--allow-validate-only", action="store_true")
    parser.add_argument("--no-require-provider-transcript", action="store_true")
    parser.add_argument("--no-require-rust-audio", action="store_true")
    parser.add_argument("--no-require-python-engine", action="store_true")
    parser.add_argument("--min-samples-per-report", type=int, default=1)
    parser.add_argument(
        "--max-audio-owned-p95-regression-ms",
        type=float,
        default=DEFAULT_AUDIO_OWNED_MAX_P95_REGRESSION_MS,
    )
    parser.add_argument("--no-require-audio-owned-latency-no-regression", action="store_true")
    return parser.parse_args(argv)


def write_result(result: dict[str, Any], output_path: str) -> None:
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        path = Path(output_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    print(output)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = build_comparison(
        read_json_object(Path(args.python_report).expanduser().resolve()),
        read_json_object(Path(args.rust_report).expanduser().resolve()),
        require_provider_transcript=not args.no_require_provider_transcript,
        require_rust_audio=not args.no_require_rust_audio,
        require_python_engine=not args.no_require_python_engine,
        allow_validate_only=args.allow_validate_only,
        min_samples_per_report=args.min_samples_per_report,
        require_audio_owned_latency_no_regression=not args.no_require_audio_owned_latency_no_regression,
        max_audio_owned_p95_regression_ms=args.max_audio_owned_p95_regression_ms,
    )
    write_result(result, args.output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
