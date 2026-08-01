"""Deterministically bridge complete quantization evidence into the release matrix."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .evaluation_bundle import (
    EvaluationBundleError,
    EvaluationPackagePart,
    combine_prediction_packages,
    combine_reference_packages,
)
from .evaluation_gate import (
    evaluate_candidate_gate,
    load_evaluation_config,
)
from .evaluation_io import (
    join_evaluation_cases,
    load_candidate_predictions,
    load_evaluation_references,
)
from .hf_quantization_pipeline import (
    REFERENCE_SUITES,
    VARIANTS,
    HfQuantizationError,
    validate_hf_quantization_plan,
    verify_hf_quantization_result_file,
)
from .metrics import evaluate_predictions
from .quality_delta import QualityDeltaError, build_quality_delta_report
from .quantization import QuantizationError, load_quantization_config
from .variant_artifacts import (
    VARIANT_MANIFEST_FILENAME,
    VariantArtifactError,
    verify_variant_artifact_manifest,
)
from .variant_release import (
    VariantReleaseError,
    collect_variant_release_failures,
    collect_variant_release_records,
    derive_qualification_failures,
    verify_variant_release_records,
)

_ROOT = Path(__file__).parents[2]
_MATRIX_SCHEMA = _ROOT / "contracts" / "variant_release_matrix_input_schema.json"
_RELOAD_SCHEMA = _ROOT / "contracts" / "variant_release_reload_report_schema.json"
_EXPECTED_SUITE_COUNTS = {
    "ai_gold": 1750,
    "critical_regression": 500,
    "regular_test": 1000,
}
_EXPECTED_CASE_COUNT = 3250
_MATRIX_FILENAME = "variant-release-matrix.json"
_COMBINED_REFERENCE_FILENAME = "all-3250.references.jsonl"
_COMBINED_REFERENCE_MANIFEST_FILENAME = "all-3250.references.manifest.json"


class VariantReleaseBuildError(RuntimeError):
    """The complete five-variant release matrix could not be materialized safely."""


@dataclass(frozen=True, slots=True)
class VariantReleaseMatrixBuild:
    """One immutable matrix input plus its independently recomputed release report."""

    output_dir: Path
    matrix_path: Path
    matrix: Mapping[str, Any]
    verified_report: Mapping[str, Any]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VariantReleaseBuildError(f"{label} must be an object")
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise VariantReleaseBuildError(f"value is not finite canonical JSON: {error}") from error


def _pretty_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise VariantReleaseBuildError(f"value is not finite JSON: {error}") from error


def _sha256_bytes(payload: bytes, *, prefixed: bool = True) -> str:
    value = hashlib.sha256(payload).hexdigest()
    return f"sha256:{value}" if prefixed else value


def _stable_bytes(path: str | Path, label: str) -> tuple[Path, bytes]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise VariantReleaseBuildError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise VariantReleaseBuildError(f"{label} must be a regular file: {resolved}")
    before = resolved.stat()
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise VariantReleaseBuildError(f"{label} could not be read: {error}") from error
    after = resolved.stat()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise VariantReleaseBuildError(f"{label} changed while it was read")
    return resolved, payload


def _stable_json(path: str | Path, label: str) -> tuple[Path, bytes, dict[str, Any]]:
    resolved, payload = _stable_bytes(path, label)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VariantReleaseBuildError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise VariantReleaseBuildError(f"{label} must contain an object")
    return resolved, payload, value


@cache
def _schema_validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _validate_schema(path: Path, value: Mapping[str, Any], label: str) -> dict[str, Any]:
    materialized = dict(value)
    errors = sorted(
        _schema_validator(path).iter_errors(materialized),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise VariantReleaseBuildError(
            f"{label} schema failed at {location}: {errors[0].message}"
        )
    return materialized


def validate_variant_release_matrix_input(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the closed, path-based matrix consumed by existing release APIs."""
    matrix = _validate_schema(_MATRIX_SCHEMA, value, "variant release matrix input")
    variants = set(_mapping(matrix.get("variants"), "matrix variants"))
    failures = set(
        _mapping(matrix.get("ineligible_variants"), "matrix ineligible variants")
    )
    if variants.intersection(failures):
        raise VariantReleaseBuildError(
            "matrix variant cannot be both eligible and ineligible"
        )
    if variants.union(failures) != set(VARIANTS):
        raise VariantReleaseBuildError(
            "matrix must account for every canonical tested variant"
        )
    if "bf16" not in variants:
        raise VariantReleaseBuildError("matrix must retain the bf16 baseline")
    return matrix


def validate_variant_release_reload_report(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one standalone reload report extracted from verified artifact evidence."""

    report = _validate_schema(_RELOAD_SCHEMA, value, "variant release reload report")
    if report["loaded_quantization"] != report["variant"]:
        raise VariantReleaseBuildError(
            "variant release reload report has a different loaded quantization"
        )
    return report


def _write_new(path: Path, payload: bytes, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise VariantReleaseBuildError(f"refusing to overwrite {label}: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise VariantReleaseBuildError(f"stale temporary {label}: {temporary}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise VariantReleaseBuildError(f"could not write {label}: {error}") from error


def _relative_path(base: Path, target: Path, label: str) -> str:
    try:
        relative = Path(os.path.relpath(target.resolve(), start=base.resolve())).as_posix()
    except ValueError as error:
        raise VariantReleaseBuildError(
            f"{label} must be on the same filesystem as the matrix output"
        ) from error
    if not relative or relative == "." or "\\" in relative or "\r" in relative or "\n" in relative:
        raise VariantReleaseBuildError(f"{label} produced an invalid relative path")
    return relative


def _artifact_root(output_root: Path, variant: str) -> Path:
    if variant in {"Q8_0", "Q4_K_M"}:
        return output_root / "variants" / "gguf" / variant
    return output_root / "variants" / variant


def _planned_suite_bindings(
    plan: Mapping[str, Any],
    reference_root: Path,
) -> tuple[list[EvaluationPackagePart], dict[str, dict[str, Any]]]:
    references = _mapping(plan.get("references"), "quantization reference bindings")
    if set(references) != set(REFERENCE_SUITES):
        raise VariantReleaseBuildError("quantization plan does not bind exactly three required suites")
    parts: list[EvaluationPackagePart] = []
    bindings: dict[str, dict[str, Any]] = {}
    for suite_id in REFERENCE_SUITES:
        record = _mapping(references[suite_id], f"{suite_id} reference binding")
        expected_count = _EXPECTED_SUITE_COUNTS[suite_id]
        if record.get("record_count") != expected_count:
            raise VariantReleaseBuildError(
                f"{suite_id} must contain exactly {expected_count} frozen cases"
            )
        records_name = record.get("records_filename")
        manifest_name = record.get("manifest_filename")
        if (
            not isinstance(records_name, str)
            or not records_name
            or Path(records_name).name != records_name
            or not isinstance(manifest_name, str)
            or not manifest_name
            or Path(manifest_name).name != manifest_name
        ):
            raise VariantReleaseBuildError(f"{suite_id} reference filenames are invalid")
        records_path, records_payload = _stable_bytes(
            reference_root / records_name,
            f"{suite_id} frozen references",
        )
        manifest_path, manifest_payload = _stable_bytes(
            reference_root / manifest_name,
            f"{suite_id} frozen reference manifest",
        )
        records_hash = _sha256_bytes(records_payload, prefixed=False)
        manifest_hash = _sha256_bytes(manifest_payload, prefixed=False)
        if (
            records_hash != record.get("records_sha256")
            or manifest_hash != record.get("manifest_sha256")
        ):
            raise VariantReleaseBuildError(
                f"{suite_id} frozen reference package differs from the quantization plan"
            )
        parts.append(EvaluationPackagePart(records_path, manifest_path))
        bindings[suite_id] = {
            "records_filename": records_name,
            "records_sha256": records_hash,
            "manifest_filename": manifest_name,
            "manifest_sha256": manifest_hash,
            "record_count": expected_count,
        }
    return parts, bindings


def _combined_evaluation(
    *,
    references_path: Path,
    predictions_path: Path,
    candidate: str,
    evaluation_config_sha256: str,
    evaluation_config: Mapping[str, Any],
    require_gate_pass: bool = True,
) -> dict[str, Any]:
    references = load_evaluation_references(references_path)
    predictions = load_candidate_predictions(predictions_path)
    semantic_plans = {
        reference["case_id"]: reference["semantic_plan"]
        for reference in references
    }
    address_modes = {
        reference["case_id"]: reference.get("address_mode")
        for reference in references
    }
    cases = tuple(
        replace(
            case,
            semantic_plan=semantic_plans[case.case_id],
            address_mode=address_modes[case.case_id],
        )
        for case in join_evaluation_cases(references, predictions)
    )
    if len(cases) != _EXPECTED_CASE_COUNT:
        raise VariantReleaseBuildError(
            f"{candidate} combined evaluation has {len(cases)} cases instead of {_EXPECTED_CASE_COUNT}"
        )
    metrics = asdict(evaluate_predictions(cases))
    gate = evaluate_candidate_gate(metrics, evaluation_config, candidate=candidate)
    report = {
        "schema_version": 1,
        "candidate": candidate,
        "evaluation_config_sha256": evaluation_config_sha256,
        "reference_sha256": _sha256_bytes(
            references_path.read_bytes(),
            prefixed=False,
        ),
        "prediction_sha256": _sha256_bytes(
            predictions_path.read_bytes(),
            prefixed=False,
        ),
        "exact_case_coverage": True,
        "metrics": metrics,
        "gate": gate,
    }
    if require_gate_pass and gate.get("passed") is not True:
        raise VariantReleaseBuildError(
            f"{candidate} combined 3250-case evaluation did not pass"
        )
    materialized = json.loads(_canonical_bytes(report))
    if not isinstance(materialized, dict):
        raise VariantReleaseBuildError(
            f"{candidate} combined evaluation did not materialize as an object"
        )
    return materialized


def _standalone_reload_report(
    *,
    variant: str,
    artifact: Mapping[str, Any],
    artifact_manifest_path: Path,
) -> dict[str, Any]:
    reload_evidence = _mapping(artifact.get("reload"), f"{variant} embedded reload evidence")
    report = {
        "schema_version": 1,
        "kind": "scriber_variant_release_reload_report",
        "status": reload_evidence.get("status"),
        "variant": variant,
        "artifact_tree_sha256": _mapping(
            artifact.get("artifact"),
            f"{variant} artifact inventory",
        ).get("tree_sha256"),
        "training_fingerprint": artifact.get("training_fingerprint"),
        "process_boundary": reload_evidence.get("process_boundary"),
        "configuration_source": reload_evidence.get("configuration_source"),
        "loaded_quantization": reload_evidence.get("loaded_quantization"),
        "deterministic": reload_evidence.get("deterministic"),
        "valid_sst": reload_evidence.get("valid_sst"),
        "generated_tokens": reload_evidence.get("generated_tokens"),
        "protection_policy": reload_evidence.get("protection_policy"),
        "source_variant_manifest_sha256": _sha256_bytes(
            artifact_manifest_path.read_bytes()
        ),
        "source_reload_sha256": _sha256_bytes(_canonical_bytes(reload_evidence)),
    }
    return validate_variant_release_reload_report(report)


def _prediction_parts(
    *,
    quantization_root: Path,
    variant: str,
    result: Mapping[str, Any],
) -> list[EvaluationPackagePart]:
    result_variant = _mapping(
        _mapping(result.get("variants"), "quantization result variants").get(variant),
        f"{variant} quantization result",
    )
    result_evaluations = _mapping(
        result_variant.get("evaluation"),
        f"{variant} quantization result evaluations",
    )
    if set(result_evaluations) != set(REFERENCE_SUITES):
        raise VariantReleaseBuildError(f"{variant} does not cover exactly the three required suites")
    parts: list[EvaluationPackagePart] = []
    for suite_id in REFERENCE_SUITES:
        lane = quantization_root / "evaluation" / variant / suite_id
        predictions_path, prediction_payload = _stable_bytes(
            lane / "predictions.jsonl",
            f"{variant}/{suite_id} predictions",
        )
        manifest_path, _ = _stable_bytes(
            lane / "predictions.manifest.json",
            f"{variant}/{suite_id} prediction manifest",
        )
        expected_hash = _mapping(
            result_evaluations[suite_id],
            f"{variant}/{suite_id} quantization result",
        ).get("prediction_sha256")
        if _sha256_bytes(prediction_payload) != expected_hash:
            raise VariantReleaseBuildError(
                f"{variant}/{suite_id} predictions differ from the completed quantization result"
            )
        parts.append(EvaluationPackagePart(predictions_path, manifest_path))
    return parts


def _local_prediction_parts(
    *,
    evaluation_root: Path,
    variant: str,
) -> tuple[list[EvaluationPackagePart], str]:
    parts: list[EvaluationPackagePart] = []
    source_candidates: set[str] = set()
    for suite_id in REFERENCE_SUITES:
        lane = evaluation_root / suite_id
        predictions_path, _ = _stable_bytes(
            lane / "predictions.jsonl",
            f"{variant}/{suite_id} local predictions",
        )
        manifest_path, _, manifest = _stable_json(
            lane / "predictions.manifest.json",
            f"{variant}/{suite_id} local prediction manifest",
        )
        if manifest.get("record_count") != _EXPECTED_SUITE_COUNTS[suite_id]:
            raise VariantReleaseBuildError(
                f"{variant}/{suite_id} local predictions have incomplete coverage"
            )
        source_candidate = manifest.get("candidate")
        if not isinstance(source_candidate, str) or not source_candidate:
            raise VariantReleaseBuildError(
                f"{variant}/{suite_id} local prediction candidate is missing"
            )
        source_candidates.add(source_candidate)
        parts.append(EvaluationPackagePart(predictions_path, manifest_path))
    if len(source_candidates) != 1:
        raise VariantReleaseBuildError(
            f"{variant} local suite manifests disagree on candidate identity"
        )
    return parts, next(iter(source_candidates))


def _combine_local_predictions(
    *,
    parts: Sequence[EvaluationPackagePart],
    source_candidate: str,
    variant: str,
    references: Path,
    reference_manifest: Path,
    destination: Path,
    manifest_destination: Path,
) -> dict[str, Any]:
    temporary_records = destination.with_name("source-candidate.predictions.jsonl")
    temporary_manifest = manifest_destination.with_name(
        "source-candidate.predictions.manifest.json"
    )
    try:
        combine_prediction_packages(
            parts,
            candidate=source_candidate,
            references=references,
            reference_manifest=reference_manifest,
            destination=temporary_records,
            manifest_destination=temporary_manifest,
        )
        records_payload = temporary_records.read_bytes()
        manifest = json.loads(temporary_manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise VariantReleaseBuildError(
                f"{variant} combined prediction manifest is not an object"
            )
        manifest["candidate"] = variant
        _write_new(destination, records_payload, f"{variant} combined predictions")
        _write_new(
            manifest_destination,
            _canonical_bytes(manifest),
            f"{variant} combined prediction manifest",
        )
    finally:
        temporary_records.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
    return manifest


def _validate_benchmark_mapping(
    benchmark_reports: Mapping[str, str | Path],
) -> dict[str, Path]:
    if set(benchmark_reports) != set(VARIANTS):
        missing = [variant for variant in VARIANTS if variant not in benchmark_reports]
        extra = sorted(set(benchmark_reports).difference(VARIANTS))
        raise VariantReleaseBuildError(
            f"benchmark report set mismatch; missing={missing}, extra={extra}"
        )
    return {
        variant: _stable_bytes(
            benchmark_reports[variant],
            f"{variant} benchmark report",
        )[0]
        for variant in VARIANTS
    }


def build_variant_release_matrix(
    *,
    quantization_root: str | Path,
    reference_root: str | Path,
    benchmark_reports: Mapping[str, str | Path],
    quantization_config_path: str | Path,
    evaluation_config_path: str | Path,
    output_dir: str | Path,
) -> VariantReleaseMatrixBuild:
    """Build all five complete 3250-case records without sampling or curation."""

    quant_root = Path(quantization_root).expanduser().resolve()
    references = Path(reference_root).expanduser().resolve()
    target = Path(output_dir).expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise VariantReleaseBuildError(f"refusing to overwrite matrix output: {target}")
    if quant_root.is_symlink() or not quant_root.is_dir():
        raise VariantReleaseBuildError(f"quantization output root is invalid: {quant_root}")
    if references.is_symlink() or not references.is_dir():
        raise VariantReleaseBuildError(f"reference root is invalid: {references}")
    config_path, config_payload = _stable_bytes(
        quantization_config_path,
        "quantization configuration",
    )
    eval_config_path, eval_config_payload = _stable_bytes(
        evaluation_config_path,
        "evaluation configuration",
    )
    try:
        config = load_quantization_config(config_path)
        evaluation_config = load_evaluation_config(eval_config_path)
    except (OSError, QuantizationError, ValueError) as error:
        raise VariantReleaseBuildError(f"release configuration is invalid: {error}") from error
    if list(config.get("enabled_variants", [])) != list(VARIANTS):
        raise VariantReleaseBuildError("quantization configuration does not enable the canonical five variants")
    plan_path, plan_payload, raw_plan = _stable_json(
        quant_root / "hf-quantization-plan.json",
        "HF quantization plan",
    )
    result_path, result_payload, _ = _stable_json(
        quant_root / "hf-quantization-result.json",
        "HF quantization result",
    )
    try:
        plan = validate_hf_quantization_plan(raw_plan)
        result = verify_hf_quantization_result_file(
            manifest_path=plan_path,
            output_root=quant_root,
            result_path=result_path,
        )
    except (HfQuantizationError, OSError, ValueError) as error:
        raise VariantReleaseBuildError(
            f"completed HF quantization evidence is invalid: {error}"
        ) from error
    planned_evaluation = _mapping(plan.get("evaluation"), "quantization evaluation binding")
    if planned_evaluation.get("config_sha256") != _sha256_bytes(
        eval_config_payload,
        prefixed=False,
    ):
        raise VariantReleaseBuildError(
            "evaluation configuration differs from the completed quantization plan"
        )
    reference_parts, suite_bindings = _planned_suite_bindings(plan, references)
    benchmarks = _validate_benchmark_mapping(benchmark_reports)
    benchmark_hashes = {
        variant: _sha256_bytes(path.read_bytes())
        for variant, path in benchmarks.items()
    }
    evaluation_config_sha256 = _sha256_bytes(
        eval_config_payload,
        prefixed=False,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.tmp-",
            dir=target.parent,
        )
    )
    try:
        combined_references = staging / _COMBINED_REFERENCE_FILENAME
        combined_reference_manifest_path = (
            staging / _COMBINED_REFERENCE_MANIFEST_FILENAME
        )
        try:
            combined_reference_manifest = combine_reference_packages(
                reference_parts,
                destination=combined_references,
                manifest_destination=combined_reference_manifest_path,
            )
        except (EvaluationBundleError, OSError, ValueError) as error:
            raise VariantReleaseBuildError(
                f"three-suite reference combination failed: {error}"
            ) from error
        if combined_reference_manifest.get("record_count") != _EXPECTED_CASE_COUNT:
            raise VariantReleaseBuildError(
                "combined references do not contain exactly 3250 cases"
            )
        variant_definitions: dict[str, dict[str, str]] = {}
        baseline_evaluation: dict[str, Any] | None = None
        for variant in VARIANTS:
            artifact_root = _artifact_root(quant_root, variant)
            try:
                artifact = verify_variant_artifact_manifest(artifact_root)
            except (OSError, VariantArtifactError) as error:
                raise VariantReleaseBuildError(
                    f"{variant} artifact verification failed: {error}"
                ) from error
            variant_root = staging / "variants" / variant
            variant_root.mkdir(parents=True)
            artifact_manifest_path = artifact_root / VARIANT_MANIFEST_FILENAME
            reload_report_path = variant_root / "reload-report.json"
            reload_report = _standalone_reload_report(
                variant=variant,
                artifact=artifact,
                artifact_manifest_path=artifact_manifest_path,
            )
            _write_new(
                reload_report_path,
                _pretty_bytes(reload_report),
                f"{variant} standalone reload report",
            )
            predictions_path = variant_root / "predictions.jsonl"
            predictions_manifest_path = variant_root / "predictions.manifest.json"
            try:
                prediction_manifest = combine_prediction_packages(
                    _prediction_parts(
                        quantization_root=quant_root,
                        variant=variant,
                        result=result,
                    ),
                    candidate=variant,
                    references=combined_references,
                    reference_manifest=combined_reference_manifest_path,
                    destination=predictions_path,
                    manifest_destination=predictions_manifest_path,
                )
            except (EvaluationBundleError, OSError, ValueError) as error:
                raise VariantReleaseBuildError(
                    f"{variant} three-suite prediction combination failed: {error}"
                ) from error
            if prediction_manifest.get("record_count") != _EXPECTED_CASE_COUNT:
                raise VariantReleaseBuildError(
                    f"{variant} predictions do not contain exactly 3250 cases"
                )
            evaluation = _combined_evaluation(
                references_path=combined_references,
                predictions_path=predictions_path,
                candidate=variant,
                evaluation_config_sha256=evaluation_config_sha256,
                evaluation_config=evaluation_config,
            )
            evaluation_path = variant_root / "evaluation.json"
            _write_new(
                evaluation_path,
                _pretty_bytes(evaluation),
                f"{variant} combined evaluation report",
            )
            if variant == "bf16":
                baseline_evaluation = evaluation
            if baseline_evaluation is None:
                raise VariantReleaseBuildError(
                    "bf16 must be evaluated before every derived variant"
                )
            baseline_path = staging / "variants" / "bf16" / "evaluation.json"
            baseline_hash = _sha256_bytes(baseline_path.read_bytes())
            evaluation_hash = _sha256_bytes(evaluation_path.read_bytes())
            try:
                delta = build_quality_delta_report(
                    baseline_evaluation,
                    evaluation,
                    baseline_sha256=baseline_hash,
                    candidate_sha256=evaluation_hash,
                    candidate_variant=variant,
                )
            except QualityDeltaError as error:
                raise VariantReleaseBuildError(
                    f"{variant} combined quality delta failed: {error}"
                ) from error
            delta_path = variant_root / "quality-delta.json"
            _write_new(
                delta_path,
                _pretty_bytes(delta),
                f"{variant} combined quality delta",
            )
            variant_definitions[variant] = {
                "artifact_root": _relative_path(staging, artifact_root, f"{variant} artifact root"),
                "reload_report": _relative_path(staging, reload_report_path, f"{variant} reload report"),
                "predictions_file": _relative_path(staging, predictions_path, f"{variant} predictions"),
                "predictions_manifest": _relative_path(
                    staging,
                    predictions_manifest_path,
                    f"{variant} prediction manifest",
                ),
                "evaluation_report": _relative_path(
                    staging,
                    evaluation_path,
                    f"{variant} evaluation report",
                ),
                "delta_report": _relative_path(staging, delta_path, f"{variant} delta report"),
                "benchmark_report": _relative_path(
                    staging,
                    benchmarks[variant],
                    f"{variant} benchmark report",
                ),
            }
        matrix = validate_variant_release_matrix_input(
            {
                "schema_version": 2,
                "kind": "scriber_variant_release_matrix_input",
                "source_evidence_sha256": _sha256_bytes(
                    _canonical_bytes(
                        {
                            "quantization_plan_sha256": _sha256_bytes(plan_payload),
                            "quantization_result_sha256": _sha256_bytes(result_payload),
                        }
                    )
                ),
                "quantization_plan_sha256": _sha256_bytes(plan_payload),
                "quantization_result_sha256": _sha256_bytes(result_payload),
                "quantization_config_sha256": _sha256_bytes(config_payload),
                "evaluation_config_sha256": _sha256_bytes(eval_config_payload),
                "coverage": {
                    "policy": (
                        "concatenate_every_row_from_every_required_suite_in_declared_order"
                    ),
                    "suite_order": list(REFERENCE_SUITES),
                    "suite_count": len(REFERENCE_SUITES),
                    "expected_case_count": _EXPECTED_CASE_COUNT,
                    "actual_case_count": combined_reference_manifest["record_count"],
                    "exact_case_coverage": True,
                    "duplicate_case_ids": "rejected_fail_closed",
                    "suites": suite_bindings,
                    "combined_reference": {
                        "records_file": _COMBINED_REFERENCE_FILENAME,
                        "records_sha256": combined_reference_manifest["records_sha256"],
                        "manifest_file": _COMBINED_REFERENCE_MANIFEST_FILENAME,
                        "manifest_sha256": _sha256_bytes(
                            combined_reference_manifest_path.read_bytes(),
                            prefixed=False,
                        ),
                        "case_ids_sha256": combined_reference_manifest[
                            "case_ids_sha256"
                        ],
                        "record_count": combined_reference_manifest["record_count"],
                    },
                },
                "variants": variant_definitions,
                "ineligible_variants": {},
            }
        )
        matrix_path = staging / _MATRIX_FILENAME
        _write_new(matrix_path, _pretty_bytes(matrix), "variant release matrix")
        try:
            records = collect_variant_release_records(matrix_path, config)
            verified_report = verify_variant_release_records(config, records)
        except (OSError, VariantReleaseError) as error:
            raise VariantReleaseBuildError(
                f"materialized matrix failed existing release verification: {error}"
            ) from error
        if (
            _sha256_bytes(plan_path.read_bytes()) != matrix["quantization_plan_sha256"]
            or _sha256_bytes(result_path.read_bytes())
            != matrix["quantization_result_sha256"]
            or _sha256_bytes(config_path.read_bytes())
            != matrix["quantization_config_sha256"]
            or _sha256_bytes(eval_config_path.read_bytes())
            != matrix["evaluation_config_sha256"]
            or any(
                _sha256_bytes(benchmarks[variant].read_bytes())
                != benchmark_hashes[variant]
                for variant in VARIANTS
            )
        ):
            raise VariantReleaseBuildError(
                "a bound matrix input changed before publication"
            )
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return VariantReleaseMatrixBuild(
        output_dir=target,
        matrix_path=target / _MATRIX_FILENAME,
        matrix=matrix,
        verified_report=verified_report,
    )


def build_private_candidate_release_matrix(
    *,
    reference_packages: Mapping[str, EvaluationPackagePart],
    variant_sources: Mapping[str, Mapping[str, str | Path]],
    ineligible_variants: Mapping[str, Mapping[str, str | Path]],
    quantization_config_path: str | Path,
    evaluation_config_path: str | Path,
    output_dir: str | Path,
) -> VariantReleaseMatrixBuild:
    """Build an evidence-bound private matrix from mixed cloud/local results."""

    target = Path(output_dir).expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise VariantReleaseBuildError(f"refusing to overwrite matrix output: {target}")
    if set(reference_packages) != set(REFERENCE_SUITES):
        raise VariantReleaseBuildError(
            "private candidate requires exactly the three canonical reference suites"
        )
    eligible = [variant for variant in VARIANTS if variant in variant_sources]
    failed = [variant for variant in VARIANTS if variant in ineligible_variants]
    if set(eligible).intersection(failed) or set(eligible).union(failed) != set(VARIANTS):
        raise VariantReleaseBuildError(
            "private candidate must account for every canonical variant exactly once"
        )
    if not eligible or eligible[0] != "bf16":
        raise VariantReleaseBuildError(
            "private candidate must retain bf16 as its eligible baseline"
        )
    config_path, config_payload = _stable_bytes(
        quantization_config_path,
        "quantization configuration",
    )
    eval_config_path, eval_config_payload = _stable_bytes(
        evaluation_config_path,
        "evaluation configuration",
    )
    try:
        config = load_quantization_config(config_path)
        evaluation_config = load_evaluation_config(eval_config_path)
    except (OSError, QuantizationError, ValueError) as error:
        raise VariantReleaseBuildError(
            f"private candidate release configuration is invalid: {error}"
        ) from error
    if list(config.get("enabled_variants", [])) != list(VARIANTS):
        raise VariantReleaseBuildError(
            "quantization configuration does not enable the canonical five variants"
        )

    reference_parts: list[EvaluationPackagePart] = []
    suite_bindings: dict[str, dict[str, Any]] = {}
    source_evidence: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_private_candidate_matrix_source_evidence",
        "quantization_config_sha256": _sha256_bytes(config_payload),
        "evaluation_config_sha256": _sha256_bytes(eval_config_payload),
        "references": {},
        "variants": {},
        "ineligible_variants": {},
    }
    for suite_id in REFERENCE_SUITES:
        part = reference_packages[suite_id]
        records_path, records_payload = _stable_bytes(
            part.records_path,
            f"{suite_id} private candidate references",
        )
        manifest_path, manifest_payload = _stable_bytes(
            part.manifest_path,
            f"{suite_id} private candidate reference manifest",
        )
        record_count = len(records_payload.splitlines())
        if record_count != _EXPECTED_SUITE_COUNTS[suite_id]:
            raise VariantReleaseBuildError(
                f"{suite_id} private candidate references have {record_count} rows"
            )
        reference_parts.append(EvaluationPackagePart(records_path, manifest_path))
        binding = {
            "records_filename": records_path.name,
            "records_sha256": _sha256_bytes(records_payload, prefixed=False),
            "manifest_filename": manifest_path.name,
            "manifest_sha256": _sha256_bytes(manifest_payload, prefixed=False),
            "record_count": record_count,
        }
        suite_bindings[suite_id] = binding
        source_evidence["references"][suite_id] = binding

    normalized_sources: dict[str, dict[str, Path]] = {}
    for variant in eligible:
        source = _mapping(variant_sources[variant], f"{variant} private source")
        if set(source) != {"artifact_root", "evaluation_root", "benchmark_report"}:
            raise VariantReleaseBuildError(
                f"{variant} private source fields are incomplete or unexpected"
            )
        artifact_root = Path(source["artifact_root"]).expanduser().resolve()
        evaluation_root = Path(source["evaluation_root"]).expanduser().resolve()
        benchmark_report, benchmark_payload = _stable_bytes(
            source["benchmark_report"],
            f"{variant} private benchmark report",
        )
        if artifact_root.is_symlink() or not artifact_root.is_dir():
            raise VariantReleaseBuildError(
                f"{variant} private artifact root is invalid: {artifact_root}"
            )
        if evaluation_root.is_symlink() or not evaluation_root.is_dir():
            raise VariantReleaseBuildError(
                f"{variant} private evaluation root is invalid: {evaluation_root}"
            )
        artifact_manifest, artifact_manifest_payload = _stable_bytes(
            artifact_root / VARIANT_MANIFEST_FILENAME,
            f"{variant} private artifact manifest",
        )
        prediction_bindings: dict[str, dict[str, str]] = {}
        for suite_id in REFERENCE_SUITES:
            lane = evaluation_root / suite_id
            predictions, predictions_payload = _stable_bytes(
                lane / "predictions.jsonl",
                f"{variant}/{suite_id} private predictions",
            )
            prediction_manifest, prediction_manifest_payload = _stable_bytes(
                lane / "predictions.manifest.json",
                f"{variant}/{suite_id} private prediction manifest",
            )
            prediction_bindings[suite_id] = {
                "predictions_filename": predictions.name,
                "predictions_sha256": _sha256_bytes(predictions_payload),
                "manifest_filename": prediction_manifest.name,
                "manifest_sha256": _sha256_bytes(prediction_manifest_payload),
            }
        normalized_sources[variant] = {
            "artifact_root": artifact_root,
            "evaluation_root": evaluation_root,
            "benchmark_report": benchmark_report,
        }
        source_evidence["variants"][variant] = {
            "artifact_manifest_filename": artifact_manifest.name,
            "artifact_manifest_sha256": _sha256_bytes(artifact_manifest_payload),
            "benchmark_filename": benchmark_report.name,
            "benchmark_sha256": _sha256_bytes(benchmark_payload),
            "predictions": prediction_bindings,
        }

    failure_payloads: dict[str, tuple[bytes, dict[str, str]]] = {}
    for variant in failed:
        failure = _mapping(
            ineligible_variants[variant],
            f"{variant} private failure source",
        )
        if set(failure) != {"stage", "reason_code", "evidence_path"}:
            raise VariantReleaseBuildError(
                f"{variant} private failure fields are incomplete or unexpected"
            )
        stage = failure.get("stage")
        reason_code = failure.get("reason_code")
        if not isinstance(stage, str) or not isinstance(reason_code, str):
            raise VariantReleaseBuildError(
                f"{variant} private failure stage and reason must be strings"
            )
        _, evidence_payload = _stable_bytes(
            failure["evidence_path"],
            f"{variant} private failure evidence",
        )
        normalized = {
            "stage": stage,
            "reason_code": reason_code,
            "evidence_sha256": _sha256_bytes(evidence_payload),
        }
        failure_payloads[variant] = (evidence_payload, normalized)
        source_evidence["ineligible_variants"][variant] = normalized

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent)
    )
    try:
        combined_references = staging / _COMBINED_REFERENCE_FILENAME
        combined_reference_manifest_path = (
            staging / _COMBINED_REFERENCE_MANIFEST_FILENAME
        )
        combined_reference_manifest = combine_reference_packages(
            reference_parts,
            destination=combined_references,
            manifest_destination=combined_reference_manifest_path,
        )
        if combined_reference_manifest.get("record_count") != _EXPECTED_CASE_COUNT:
            raise VariantReleaseBuildError(
                "private candidate combined references do not contain 3250 cases"
            )
        source_evidence_path = staging / "source-evidence.json"
        source_evidence_payload = _pretty_bytes(source_evidence)
        _write_new(
            source_evidence_path,
            source_evidence_payload,
            "private candidate source evidence",
        )
        matrix_failures: dict[str, dict[str, str]] = {}
        for variant in failed:
            evidence_payload, failure = failure_payloads[variant]
            evidence_path = staging / "failures" / f"{variant}.evidence"
            _write_new(
                evidence_path,
                evidence_payload,
                f"{variant} private failure evidence",
            )
            matrix_failures[variant] = {
                "stage": failure["stage"],
                "reason_code": failure["reason_code"],
                "evidence_file": _relative_path(
                    staging,
                    evidence_path,
                    f"{variant} failure evidence",
                ),
                "evidence_sha256": failure["evidence_sha256"],
            }

        evaluation_config_sha256 = _sha256_bytes(
            eval_config_payload,
            prefixed=False,
        )
        variant_definitions: dict[str, dict[str, str]] = {}
        baseline_evaluation: dict[str, Any] | None = None
        for variant in eligible:
            source = normalized_sources[variant]
            try:
                artifact = verify_variant_artifact_manifest(source["artifact_root"])
            except (OSError, VariantArtifactError) as error:
                raise VariantReleaseBuildError(
                    f"{variant} private artifact verification failed: {error}"
                ) from error
            variant_root = staging / "variants" / variant
            variant_root.mkdir(parents=True)
            artifact_manifest_path = (
                source["artifact_root"] / VARIANT_MANIFEST_FILENAME
            )
            reload_report_path = variant_root / "reload-report.json"
            reload_report = _standalone_reload_report(
                variant=variant,
                artifact=artifact,
                artifact_manifest_path=artifact_manifest_path,
            )
            _write_new(
                reload_report_path,
                _pretty_bytes(reload_report),
                f"{variant} private reload report",
            )
            prediction_parts, source_candidate = _local_prediction_parts(
                evaluation_root=source["evaluation_root"],
                variant=variant,
            )
            predictions_path = variant_root / "predictions.jsonl"
            predictions_manifest_path = variant_root / "predictions.manifest.json"
            prediction_manifest = _combine_local_predictions(
                parts=prediction_parts,
                source_candidate=source_candidate,
                variant=variant,
                references=combined_references,
                reference_manifest=combined_reference_manifest_path,
                destination=predictions_path,
                manifest_destination=predictions_manifest_path,
            )
            if prediction_manifest.get("record_count") != _EXPECTED_CASE_COUNT:
                raise VariantReleaseBuildError(
                    f"{variant} private predictions do not contain 3250 cases"
                )
            evaluation = _combined_evaluation(
                references_path=combined_references,
                predictions_path=predictions_path,
                candidate=variant,
                evaluation_config_sha256=evaluation_config_sha256,
                evaluation_config=evaluation_config,
                require_gate_pass=False,
            )
            evaluation_path = variant_root / "evaluation.json"
            _write_new(
                evaluation_path,
                _pretty_bytes(evaluation),
                f"{variant} private combined evaluation",
            )
            if variant == "bf16":
                baseline_evaluation = evaluation
            if baseline_evaluation is None:
                raise VariantReleaseBuildError(
                    "bf16 must be evaluated before derived private variants"
                )
            baseline_path = staging / "variants" / "bf16" / "evaluation.json"
            try:
                delta = build_quality_delta_report(
                    baseline_evaluation,
                    evaluation,
                    baseline_sha256=_sha256_bytes(baseline_path.read_bytes()),
                    candidate_sha256=_sha256_bytes(evaluation_path.read_bytes()),
                    candidate_variant=variant,
                )
            except QualityDeltaError as error:
                raise VariantReleaseBuildError(
                    f"{variant} private quality delta failed: {error}"
                ) from error
            delta_path = variant_root / "quality-delta.json"
            _write_new(
                delta_path,
                _pretty_bytes(delta),
                f"{variant} private quality delta",
            )
            variant_definitions[variant] = {
                "artifact_root": _relative_path(
                    staging,
                    source["artifact_root"],
                    f"{variant} artifact root",
                ),
                "reload_report": _relative_path(
                    staging,
                    reload_report_path,
                    f"{variant} reload report",
                ),
                "predictions_file": _relative_path(
                    staging,
                    predictions_path,
                    f"{variant} predictions",
                ),
                "predictions_manifest": _relative_path(
                    staging,
                    predictions_manifest_path,
                    f"{variant} prediction manifest",
                ),
                "evaluation_report": _relative_path(
                    staging,
                    evaluation_path,
                    f"{variant} evaluation report",
                ),
                "delta_report": _relative_path(
                    staging,
                    delta_path,
                    f"{variant} delta report",
                ),
                "benchmark_report": _relative_path(
                    staging,
                    source["benchmark_report"],
                    f"{variant} benchmark report",
                ),
            }

        matrix = validate_variant_release_matrix_input(
            {
                "schema_version": 2,
                "kind": "scriber_variant_release_matrix_input",
                "source_evidence_sha256": _sha256_bytes(source_evidence_payload),
                "quantization_config_sha256": _sha256_bytes(config_payload),
                "evaluation_config_sha256": _sha256_bytes(eval_config_payload),
                "coverage": {
                    "policy": (
                        "concatenate_every_row_from_every_required_suite_in_declared_order"
                    ),
                    "suite_order": list(REFERENCE_SUITES),
                    "suite_count": len(REFERENCE_SUITES),
                    "expected_case_count": _EXPECTED_CASE_COUNT,
                    "actual_case_count": combined_reference_manifest["record_count"],
                    "exact_case_coverage": True,
                    "duplicate_case_ids": "rejected_fail_closed",
                    "suites": suite_bindings,
                    "combined_reference": {
                        "records_file": _COMBINED_REFERENCE_FILENAME,
                        "records_sha256": combined_reference_manifest[
                            "records_sha256"
                        ],
                        "manifest_file": _COMBINED_REFERENCE_MANIFEST_FILENAME,
                        "manifest_sha256": _sha256_bytes(
                            combined_reference_manifest_path.read_bytes(),
                            prefixed=False,
                        ),
                        "case_ids_sha256": combined_reference_manifest[
                            "case_ids_sha256"
                        ],
                        "record_count": combined_reference_manifest["record_count"],
                    },
                },
                "variants": variant_definitions,
                "ineligible_variants": matrix_failures,
            }
        )
        matrix_path = staging / _MATRIX_FILENAME
        _write_new(matrix_path, _pretty_bytes(matrix), "private candidate matrix")
        records = collect_variant_release_records(matrix_path, config)
        verified_report = verify_variant_release_records(
            config,
            records,
            ineligible_variants=collect_variant_release_failures(matrix_path),
            qualification_failures=derive_qualification_failures(config, records),
        )
        report_path = staging / "variant-release-matrix-report.json"
        _write_new(
            report_path,
            _pretty_bytes(verified_report),
            "private candidate matrix report",
        )
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return VariantReleaseMatrixBuild(
        output_dir=target,
        matrix_path=target / _MATRIX_FILENAME,
        matrix=matrix,
        verified_report=verified_report,
    )


def parse_benchmark_report_arguments(values: Sequence[str]) -> dict[str, Path]:
    """Parse five deterministic ``VARIANT=PATH`` CLI bindings."""

    parsed: dict[str, Path] = {}
    for value in values:
        variant, separator, raw_path = value.partition("=")
        if not separator or variant not in VARIANTS or not raw_path:
            raise VariantReleaseBuildError(
                "benchmark report must use one canonical VARIANT=PATH binding"
            )
        if variant in parsed:
            raise VariantReleaseBuildError(
                f"duplicate benchmark report binding: {variant}"
            )
        parsed[variant] = Path(raw_path)
    _validate_benchmark_mapping(parsed)
    return parsed
