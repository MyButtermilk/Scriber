"""Build an honest BF16-only private-candidate matrix from separate evaluation lanes.

The canonical evaluation suites can contain overlapping case identifiers.  This
builder therefore never concatenates or rewrites prediction rows.  It binds the
three lanes independently, counts their rows, and marks every quantized variant
without a complete quality evaluation as ineligible for release qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.variant_release import (  # noqa: E402, I001
    VariantReleaseError,
    validate_variant_release_report,
)


_VARIANTS = ("bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M")
_SUITES = ("regular_test", "critical_regression", "ai_gold")


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _load(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    resolved = path.resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    payload = resolved.read_bytes()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object: {path}")
    return value, payload


def _row_count(path: Path, label: str) -> int:
    resolved = path.resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    count = 0
    with resolved.open("rb") as stream:
        for line in stream:
            if line in {b"\n", b"\r\n"}:
                raise ValueError(f"{label} contains a blank row")
            count += 1
    if count < 1:
        raise ValueError(f"{label} is empty")
    return count


def _failure(value: str) -> tuple[str, str, str, Path]:
    parts = value.split("=", 3)
    if len(parts) != 4:
        raise ValueError("--failure must be VARIANT=STAGE=REASON=PATH")
    variant, stage, reason, raw_path = parts
    if variant not in _VARIANTS or variant == "bf16":
        raise ValueError(f"unsupported ineligible variant: {variant}")
    if stage not in {"artifact", "reload", "predictions", "evaluation", "delta", "benchmark"}:
        raise ValueError(f"unsupported failure stage: {stage}")
    if not reason or not reason.replace("_", "").isalnum() or reason.lower() != reason:
        raise ValueError(f"invalid failure reason: {reason}")
    return variant, stage, reason, Path(raw_path)


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bf16-manifest", type=Path, required=True)
    parser.add_argument("--bf16-benchmark", type=Path, required=True)
    parser.add_argument("--bf16-reload", type=Path, required=True)
    for suite in _SUITES:
        parser.add_argument(f"--{suite.replace('_', '-')}-evaluation", type=Path, required=True)
        parser.add_argument(f"--{suite.replace('_', '-')}-predictions", type=Path, required=True)
    parser.add_argument("--failure", action="append", default=[], required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    manifest, manifest_payload = _load(args.bf16_manifest, "BF16 artifact manifest")
    benchmark, benchmark_payload = _load(args.bf16_benchmark, "BF16 benchmark")
    reload_report, reload_payload = _load(args.bf16_reload, "BF16 reload report")
    training_fingerprint = manifest.get("training_fingerprint")
    if (
        manifest.get("variant") != "bf16"
        or benchmark.get("variant") != "bf16"
        or benchmark.get("training_fingerprint") != training_fingerprint
        or reload_report.get("run_fingerprint") != training_fingerprint
        or reload_report.get("status") != "passed"
    ):
        raise ValueError("BF16 artifact, benchmark, and reload identities differ")
    if benchmark.get("status") != "complete":
        raise ValueError("BF16 benchmark is incomplete")
    warm_runtime = benchmark.get("runtime_protocol", {}).get("warm_runtime", {})
    if (
        warm_runtime.get("persistent_model_process") is not True
        or warm_runtime.get("instance_identity_stable") is not True
    ):
        raise ValueError("BF16 benchmark did not prove one persistent warm runtime")

    lane_evidence: dict[str, Any] = {}
    prediction_count = 0
    all_gates_failed = True
    for suite in _SUITES:
        evaluation_path: Path = getattr(args, f"{suite}_evaluation")
        predictions_path: Path = getattr(args, f"{suite}_predictions")
        evaluation, evaluation_payload = _load(evaluation_path, f"{suite} evaluation")
        rows = _row_count(predictions_path, f"{suite} predictions")
        metrics = evaluation.get("metrics")
        gate = evaluation.get("gate")
        if not isinstance(metrics, Mapping) or not isinstance(gate, Mapping):
            raise ValueError(f"{suite} evaluation has no metrics or gate")
        if metrics.get("total") != rows or evaluation.get("exact_case_coverage") is not True:
            raise ValueError(f"{suite} evaluation and prediction coverage differ")
        all_gates_failed = all_gates_failed and gate.get("passed") is False
        prediction_payload = predictions_path.resolve().read_bytes()
        lane_evidence[suite] = {
            "evaluation_sha256": _hash_bytes(evaluation_payload),
            "gate_status": gate.get("status"),
            "gate_passed": gate.get("passed"),
            "prediction_count": rows,
            "predictions_sha256": _hash_bytes(prediction_payload),
        }
        prediction_count += rows
    if prediction_count != 3250:
        raise ValueError(f"BF16 evaluation lanes contain {prediction_count} rows, expected 3250")
    if not all_gates_failed:
        raise ValueError("private candidate must preserve all failed BF16 gate states")

    parsed_failures = [_failure(value) for value in args.failure]
    if {item[0] for item in parsed_failures} != set(_VARIANTS[1:]):
        raise ValueError("failures must account for all four non-BF16 variants exactly once")
    failures: dict[str, dict[str, str]] = {}
    failure_sources: dict[str, Any] = {}
    for variant in _VARIANTS[1:]:
        matches = [item for item in parsed_failures if item[0] == variant]
        if len(matches) != 1:
            raise ValueError(f"duplicate failure evidence for {variant}")
        _, stage, reason, path = matches[0]
        _, payload = _load(path, f"{variant} failure evidence")
        evidence_hash = _hash_bytes(payload)
        failures[variant] = {
            "stage": stage,
            "reason_code": reason,
            "evidence_sha256": evidence_hash,
        }
        failure_sources[variant] = {
            **failures[variant],
            "source_file": path.as_posix(),
        }

    predictions_evidence = {
        "schema_version": 1,
        "kind": "scriber_split_aware_prediction_evidence",
        "row_identity_policy": "suite_scoped_case_ids_preserved_without_rewriting",
        "prediction_count": prediction_count,
        "suites": lane_evidence,
    }
    evaluation_evidence = {
        "schema_version": 1,
        "kind": "scriber_split_aware_evaluation_evidence",
        "status": "PRIVATE_CANDIDATE",
        "all_gates_passed": False,
        "prediction_count": prediction_count,
        "suites": lane_evidence,
    }
    delta = {
        "schema_version": 1,
        "kind": "scriber_private_candidate_baseline_self_delta",
        "baseline_variant": "bf16",
        "candidate_variant": "bf16",
        "maximum_absolute_metric_drop": 0.0,
        "new_critical_errors": 0,
        "note": "Identity delta only; BF16 hard quality gates remain failed.",
    }
    source_evidence = {
        "schema_version": 1,
        "kind": "scriber_split_aware_private_candidate_source_evidence",
        "training_fingerprint": training_fingerprint,
        "artifact_manifest_sha256": _hash_bytes(manifest_payload),
        "benchmark_sha256": _hash_bytes(benchmark_payload),
        "reload_sha256": _hash_bytes(reload_payload),
        "prediction_evidence_sha256": _hash_bytes(_canonical(predictions_evidence)),
        "evaluation_evidence_sha256": _hash_bytes(_canonical(evaluation_evidence)),
        "failures": failure_sources,
    }

    report = {
        "schema_version": 4,
        "status": "PRIVATE_CANDIDATE_MATRIX",
        "passed": False,
        "baseline_variant": "bf16",
        "training_fingerprint": training_fingerprint,
        "protection_policy": manifest.get("protection_policy"),
        "tested_variants": list(_VARIANTS),
        "variants": ["bf16"],
        "variant_summaries": {
            "bf16": {
                "artifact_tree_sha256": manifest["artifact"]["tree_sha256"],
                "artifact_manifest_sha256": _hash_bytes(manifest_payload),
                "reload_report_sha256": _hash_bytes(reload_payload),
                "predictions_sha256": _hash_bytes(_canonical(predictions_evidence)),
                "evaluation_report_sha256": _hash_bytes(_canonical(evaluation_evidence)),
                "delta_report_sha256": _hash_bytes(_canonical(delta)),
                "benchmark_report_sha256": _hash_bytes(benchmark_payload),
                "prediction_count": prediction_count,
                "maximum_absolute_metric_drop": 0.0,
                "new_critical_errors": 0,
                "persistent_warm_runtime": True,
                "evidence": [
                    "artifact",
                    "reload",
                    "predictions",
                    "evaluation",
                    "delta",
                    "benchmark",
                ],
            }
        },
        "ineligible_variants": failures,
        "qualification_failures": {
            "bf16": {
                "stage": "evaluation",
                "reason_code": "hard_quality_gates_failed",
                "evidence_sha256": _hash_bytes(_canonical(evaluation_evidence)),
            }
        },
        "reasons": [
            *[
                f"{variant}:{failures[variant]['stage']}:{failures[variant]['reason_code']}"
                for variant in _VARIANTS[1:]
            ],
            "bf16:qualification:evaluation:hard_quality_gates_failed",
        ],
    }
    try:
        validate_variant_release_report(report)
    except VariantReleaseError as error:
        raise ValueError(f"generated private candidate report is invalid: {error}") from error

    target = args.output_dir.resolve()
    if target.exists() or target.is_symlink():
        raise ValueError(f"refusing to overwrite output: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        _write_new(staging / "variant-release-matrix-report.json", _canonical(report))
        _write_new(staging / "split-aware-source-evidence.json", _canonical(source_evidence))
        _write_new(staging / "bf16-predictions-evidence.json", _canonical(predictions_evidence))
        _write_new(staging / "bf16-evaluation-evidence.json", _canonical(evaluation_evidence))
        _write_new(staging / "bf16-self-delta.json", _canonical(delta))
        target.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(
        "split_aware_private_candidate=complete "
        f"prediction_rows={prediction_count} eligible=bf16 ineligible=4"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
