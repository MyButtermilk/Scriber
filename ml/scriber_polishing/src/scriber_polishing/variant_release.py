"""Fail-closed release matrix across every enabled local model variant."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .policy_artifact import (
    PolicyArtifactError,
    frozen_lexical_policy_binding,
    validate_frozen_lexical_policy_requirement,
)
from .quality_delta import QualityDeltaError, verify_quality_delta_report
from .variant_artifacts import (
    VARIANT_MANIFEST_FILENAME,
    verify_variant_artifact_manifest,
)

_ROOT = Path(__file__).parents[2]
_REPORT_SCHEMA = _ROOT / "contracts" / "variant_release_matrix_report_schema.json"
_BENCHMARK_SCHEMA = _ROOT / "contracts" / "benchmark_report_schema.json"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_KINDS = (
    "artifact",
    "reload",
    "predictions",
    "evaluation",
    "delta",
    "benchmark",
)
_CANONICAL_VARIANTS = ("bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M")
_FAILURE_STAGES = frozenset(_EVIDENCE_KINDS)
_QUALIFICATION_FAILURE_STAGES = frozenset({"evaluation", "delta"})
_REASON_CODE_RE = re.compile(r"^[a-z0-9_]+$")


class VariantReleaseError(RuntimeError):
    """One enabled variant lacks complete, identity-bound release evidence."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VariantReleaseError(f"{label} must be a mapping")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise VariantReleaseError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise VariantReleaseError(f"{label} must be numeric")
    return float(value)


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise VariantReleaseError(f"{label} must use sha256:<64 lowercase hex>")
    return value


@cache
def _validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _validate(path: Path, value: Mapping[str, object], label: str) -> None:
    errors = sorted(
        _validator(path).iter_errors(dict(value)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise VariantReleaseError(f"{label} schema failed at {location}: {errors[0].message}")


def verify_variant_release_records(
    config: Mapping[str, object],
    records: Mapping[str, Mapping[str, object]],
    *,
    ineligible_variants: Mapping[str, Mapping[str, object]] | None = None,
    qualification_failures: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Recompute gates and bind technical and quality failures separately."""

    enabled = list(config.get("enabled_variants", []))
    if enabled != list(_CANONICAL_VARIANTS):
        raise VariantReleaseError("quantization policy has a non-canonical enabled variant set")
    raw_ineligible = dict(ineligible_variants or {})
    raw_qualification_failures = dict(qualification_failures or {})
    if set(records).intersection(raw_ineligible):
        raise VariantReleaseError("a variant cannot be both eligible and ineligible")
    if set(records).union(raw_ineligible) != set(enabled):
        missing = [
            variant
            for variant in enabled
            if variant not in records and variant not in raw_ineligible
        ]
        extra = sorted(set(records).union(raw_ineligible).difference(enabled))
        raise VariantReleaseError(
            f"release matrix tested variant set mismatch; missing={missing}, extra={extra}"
        )
    if "bf16" not in records:
        raise VariantReleaseError("bf16 baseline must remain an eligible variant")
    normalized_ineligible: dict[str, dict[str, str]] = {}
    for variant in enabled:
        if variant not in raw_ineligible:
            continue
        failure = _mapping(
            raw_ineligible[variant],
            f"{variant} ineligible evidence",
        )
        if set(failure) != {"stage", "reason_code", "evidence_sha256"}:
            raise VariantReleaseError(
                f"{variant} ineligible evidence fields are incomplete or unexpected"
            )
        stage = failure.get("stage")
        reason_code = failure.get("reason_code")
        if stage not in _FAILURE_STAGES:
            raise VariantReleaseError(f"{variant} ineligible stage is invalid")
        if (
            not isinstance(reason_code, str)
            or _REASON_CODE_RE.fullmatch(reason_code) is None
        ):
            raise VariantReleaseError(f"{variant} ineligible reason code is invalid")
        normalized_ineligible[variant] = {
            "stage": str(stage),
            "reason_code": reason_code,
            "evidence_sha256": _require_hash(
                failure.get("evidence_sha256"),
                f"{variant} ineligible evidence hash",
            ),
        }
    if not set(raw_qualification_failures).issubset(records):
        unknown = sorted(set(raw_qualification_failures).difference(records))
        raise VariantReleaseError(
            "qualification failures must belong to technically eligible variants: "
            f"{unknown}"
        )
    normalized_qualification_failures: dict[str, dict[str, str]] = {}
    for variant in enabled:
        if variant not in raw_qualification_failures:
            continue
        failure = _mapping(
            raw_qualification_failures[variant],
            f"{variant} qualification failure evidence",
        )
        if set(failure) != {"stage", "reason_code", "evidence_sha256"}:
            raise VariantReleaseError(
                f"{variant} qualification failure fields are incomplete or unexpected"
            )
        stage = failure.get("stage")
        reason_code = failure.get("reason_code")
        if stage not in _QUALIFICATION_FAILURE_STAGES:
            raise VariantReleaseError(
                f"{variant} qualification failure stage is invalid"
            )
        if (
            not isinstance(reason_code, str)
            or _REASON_CODE_RE.fullmatch(reason_code) is None
        ):
            raise VariantReleaseError(
                f"{variant} qualification failure reason code is invalid"
            )
        normalized_qualification_failures[variant] = {
            "stage": str(stage),
            "reason_code": reason_code,
            "evidence_sha256": _require_hash(
                failure.get("evidence_sha256"),
                f"{variant} qualification failure evidence hash",
            ),
        }
    release_policy = _mapping(config.get("release_matrix"), "release_matrix policy")
    if release_policy.get("baseline_variant") != "bf16":
        raise VariantReleaseError("release matrix baseline must be bf16")
    try:
        validate_frozen_lexical_policy_requirement(config.get("protection_policy"))
    except PolicyArtifactError as error:
        raise VariantReleaseError("quantization policy does not bind the frozen protection policy") from error
    expected_protection_policy = frozen_lexical_policy_binding()
    baseline_evaluation_hash: str | None = None
    training_fingerprint: str | None = None
    summaries: dict[str, dict[str, object]] = {}
    eligible = [variant for variant in enabled if variant in records]
    for variant in eligible:
        record = _mapping(records[variant], f"{variant} record")
        for kind in _EVIDENCE_KINDS:
            if kind not in record:
                raise VariantReleaseError(f"{variant} is missing required {kind} evidence")
        evidence_hashes = _mapping(
            record.get("evidence_hashes"),
            f"{variant} evidence hashes",
        )
        required_hashes = {
            name: _require_hash(
                evidence_hashes.get(name),
                f"{variant} {name} evidence hash",
            )
            for name in (
                "artifact_manifest",
                "reload_report",
                "predictions",
                "evaluation_report",
                "delta_report",
                "benchmark_report",
            )
        }
        artifact = _mapping(record["artifact"], f"{variant} artifact")
        if artifact.get("variant") != variant:
            raise VariantReleaseError(f"{variant} artifact variant does not match")
        manifest_hash = _require_hash(
            artifact.get("manifest_sha256"),
            f"{variant} artifact manifest hash",
        )
        tree_hash = _require_hash(
            artifact.get("artifact_tree_sha256"),
            f"{variant} artifact tree hash",
        )
        fingerprint = _require_hash(
            artifact.get("training_fingerprint"),
            f"{variant} training fingerprint",
        )
        if required_hashes["artifact_manifest"] != manifest_hash:
            raise VariantReleaseError(f"{variant} artifact manifest evidence hash differs")
        quantization_hash = _require_hash(
            artifact.get("quantization_config_sha256"),
            f"{variant} quantization config hash",
        )
        toolchain_hash = _require_hash(
            artifact.get("toolchain_sha256"),
            f"{variant} toolchain hash",
        )
        artifact_policy = _mapping(
            artifact.get("protection_policy"),
            f"{variant} artifact protection policy",
        )
        if dict(artifact_policy) != expected_protection_policy:
            raise VariantReleaseError(f"{variant} artifact protection policy differs from the frozen policy")
        if training_fingerprint is None:
            training_fingerprint = fingerprint
        elif training_fingerprint != fingerprint:
            raise VariantReleaseError(f"{variant} training fingerprint differs from bf16")

        reload = _mapping(record["reload"], f"{variant} reload")
        if (
            reload.get("status") != "passed"
            or reload.get("variant") != variant
            or reload.get("process_boundary") != "fresh_subprocess"
            or reload.get("configuration_source") != "saved_artifact"
            or reload.get("valid_sst") is not True
            or reload.get("deterministic") is not True
        ):
            raise VariantReleaseError(f"{variant} fresh reload evidence is incomplete")
        if reload.get("artifact_tree_sha256") != tree_hash:
            raise VariantReleaseError(f"{variant} reload artifact tree hash differs")
        if reload.get("training_fingerprint") != fingerprint:
            raise VariantReleaseError(f"{variant} reload training fingerprint differs")
        if reload.get("report_sha256") != required_hashes["reload_report"]:
            raise VariantReleaseError(f"{variant} reload report hash differs")
        reload_policy = _mapping(
            reload.get("protection_policy"),
            f"{variant} reload protection policy",
        )
        if dict(reload_policy) != expected_protection_policy or dict(reload_policy) != dict(artifact_policy):
            raise VariantReleaseError(f"{variant} reload protection policy differs from its artifact")

        predictions = _mapping(record["predictions"], f"{variant} predictions")
        reference_count = _integer(
            predictions.get("reference_count"),
            f"{variant} reference_count",
            minimum=1,
        )
        prediction_count = _integer(
            predictions.get("prediction_count"),
            f"{variant} prediction_count",
            minimum=1,
        )
        prediction_hash = _require_hash(
            predictions.get("predictions_sha256"),
            f"{variant} predictions hash",
        )
        if (
            predictions.get("status") != "complete"
            or predictions.get("variant") != variant
            or predictions.get("artifact_tree_sha256") != tree_hash
            or predictions.get("exact_case_coverage") is not True
            or prediction_count != reference_count
        ):
            raise VariantReleaseError(f"{variant} full prediction evidence is incomplete")
        if prediction_hash != required_hashes["predictions"]:
            raise VariantReleaseError(f"{variant} prediction evidence hash differs")

        evaluation = _mapping(record["evaluation"], f"{variant} evaluation")
        evaluation_hash = _require_hash(
            evaluation.get("report_sha256"),
            f"{variant} evaluation report hash",
        )
        qualification_failure = normalized_qualification_failures.get(variant)
        evaluation_passed = evaluation.get("passed") is True
        evaluation_failure_bound = (
            qualification_failure is not None
            and qualification_failure["stage"] == "evaluation"
            and qualification_failure["evidence_sha256"] == evaluation_hash
        )
        if (
            evaluation.get("status") != "complete"
            or evaluation.get("candidate") != variant
            or evaluation.get("exact_case_coverage") is not True
            or evaluation.get("predictions_sha256") != prediction_hash
        ):
            raise VariantReleaseError(f"{variant} full evaluation evidence is incomplete")
        if not evaluation_passed and not evaluation_failure_bound:
            raise VariantReleaseError(
                f"{variant} failed evaluation is not bound as a qualification failure"
            )
        if (
            qualification_failure is not None
            and qualification_failure["stage"] == "evaluation"
            and evaluation_passed
        ):
            raise VariantReleaseError(
                f"{variant} declares an evaluation failure although the gate passed"
            )
        if (
            qualification_failure is not None
            and qualification_failure["stage"] == "evaluation"
            and not evaluation_failure_bound
        ):
            raise VariantReleaseError(
                f"{variant} evaluation qualification failure hash differs"
            )
        if variant == "bf16":
            baseline_evaluation_hash = evaluation_hash
        if evaluation_hash != required_hashes["evaluation_report"]:
            raise VariantReleaseError(f"{variant} evaluation evidence hash differs")

        delta = _mapping(record["delta"], f"{variant} delta")
        if (
            delta.get("status") != "complete"
            or delta.get("baseline_variant") != "bf16"
            or delta.get("candidate_variant") != variant
        ):
            raise VariantReleaseError(f"{variant} delta report identity is incomplete")
        maximum_drop = _number(
            delta.get("maximum_absolute_metric_drop"),
            f"{variant} maximum metric drop",
        )
        new_critical_errors = _integer(
            delta.get("new_critical_errors"),
            f"{variant} new critical errors",
        )
        if baseline_evaluation_hash is None:
            raise VariantReleaseError("bf16 baseline must be verified before derived variants")
        if (
            delta.get("baseline_evaluation_sha256") != baseline_evaluation_hash
            or delta.get("candidate_evaluation_sha256") != evaluation_hash
        ):
            raise VariantReleaseError(f"{variant} delta report evaluation hashes differ")
        if variant == "bf16":
            allowed_drop = 0.0
            allowed_critical_errors = 0
        else:
            budget = _mapping(
                _mapping(config.get("quality_delta_budgets"), "quality delta budgets").get(variant),
                f"{variant} delta budget",
            )
            allowed_drop = _number(
                budget.get("maximum_absolute_metric_drop"),
                f"{variant} configured maximum metric drop",
            )
            allowed_critical_errors = _integer(
                budget.get("maximum_new_critical_errors"),
                f"{variant} configured maximum new critical errors",
            )
        delta_exceeded = (
            maximum_drop > allowed_drop
            or new_critical_errors > allowed_critical_errors
        )
        delta_failure_bound = (
            qualification_failure is not None
            and qualification_failure["stage"] == "delta"
            and qualification_failure["evidence_sha256"]
            == required_hashes["delta_report"]
        )
        if delta_exceeded and not (delta_failure_bound or evaluation_failure_bound):
            raise VariantReleaseError(
                f"{variant} exceeds its quality delta budget: "
                f"drop={maximum_drop}>{allowed_drop} or "
                f"critical={new_critical_errors}>{allowed_critical_errors}"
            )
        if (
            qualification_failure is not None
            and qualification_failure["stage"] == "delta"
            and not delta_exceeded
        ):
            raise VariantReleaseError(
                f"{variant} declares a delta failure although the budget passed"
            )
        if (
            qualification_failure is not None
            and qualification_failure["stage"] == "delta"
            and not delta_failure_bound
        ):
            raise VariantReleaseError(
                f"{variant} delta qualification failure hash differs"
            )
        if (
            qualification_failure is not None
            and qualification_failure["stage"] == "evaluation"
            and evaluation_passed
        ):
            raise VariantReleaseError(
                f"{variant} qualification failure is inconsistent with evaluation evidence"
            )
        if delta.get("report_sha256") != required_hashes["delta_report"]:
            raise VariantReleaseError(f"{variant} delta report hash differs")

        benchmark = _mapping(record["benchmark"], f"{variant} benchmark")
        benchmark_identity = _mapping(
            benchmark.get("artifact_identity"),
            f"{variant} benchmark artifact identity",
        )
        protocol = _mapping(benchmark.get("runtime_protocol"), f"{variant} runtime protocol")
        cold = _mapping(protocol.get("cold_loads"), f"{variant} cold-load protocol")
        warm = _mapping(protocol.get("warm_runtime"), f"{variant} warm-runtime protocol")
        if (
            benchmark.get("schema_version") != 4
            or benchmark.get("status") != "complete"
            or benchmark.get("variant") != variant
            or benchmark.get("artifact_manifest_sha256") != manifest_hash
            or benchmark.get("artifact_tree_sha256") != tree_hash
            or benchmark.get("training_fingerprint") != fingerprint
            or benchmark_identity.get("variant") != variant
            or benchmark_identity.get("quantization_config_sha256") != quantization_hash
            or benchmark_identity.get("toolchain_sha256") != toolchain_hash
            or _integer(benchmark.get("cold_sample_count"), f"{variant} cold samples") < 3
            or _integer(benchmark.get("warm_sample_count"), f"{variant} warm samples", minimum=1) < 1
            or _integer(cold.get("adapter_instances"), f"{variant} cold adapters") < 3
            or _integer(cold.get("repetitions"), f"{variant} cold repetitions") < 3
            or warm.get("adapter_instances") != 1
            or benchmark.get("hardware_identity_complete") is not True
        ):
            raise VariantReleaseError(f"{variant} benchmark evidence is incomplete")
        if benchmark.get("report_sha256") != required_hashes["benchmark_report"]:
            raise VariantReleaseError(f"{variant} benchmark report hash differs")
        persistent_warm = warm.get("persistent_model_process") is True
        if release_policy.get("require_persistent_warm_runtime") is True and not persistent_warm:
            raise VariantReleaseError(f"{variant} benchmark has no persistent warm runtime")
        summaries[variant] = {
            "artifact_tree_sha256": tree_hash,
            "artifact_manifest_sha256": manifest_hash,
            "reload_report_sha256": required_hashes["reload_report"],
            "predictions_sha256": required_hashes["predictions"],
            "evaluation_report_sha256": required_hashes["evaluation_report"],
            "delta_report_sha256": required_hashes["delta_report"],
            "benchmark_report_sha256": required_hashes["benchmark_report"],
            "prediction_count": prediction_count,
            "maximum_absolute_metric_drop": maximum_drop,
            "new_critical_errors": new_critical_errors,
            "persistent_warm_runtime": persistent_warm,
            "evidence": list(_EVIDENCE_KINDS),
        }
    assert training_fingerprint is not None
    passed = not normalized_ineligible and not normalized_qualification_failures
    report: dict[str, object] = {
        "schema_version": 4,
        "status": (
            "RELEASE_MATRIX_PASSED"
            if passed
            else "PRIVATE_CANDIDATE_MATRIX"
        ),
        "passed": passed,
        "baseline_variant": "bf16",
        "training_fingerprint": training_fingerprint,
        "protection_policy": expected_protection_policy,
        "tested_variants": enabled,
        "variants": eligible,
        "variant_summaries": summaries,
        "ineligible_variants": normalized_ineligible,
        "qualification_failures": normalized_qualification_failures,
        "reasons": [
            f"{variant}:{failure['stage']}:{failure['reason_code']}"
            for variant, failure in normalized_ineligible.items()
        ]
        + [
            (
                f"{variant}:qualification:{failure['stage']}:"
                f"{failure['reason_code']}"
            )
            for variant, failure in normalized_qualification_failures.items()
        ],
    }
    _validate(_REPORT_SCHEMA, report, "variant release matrix report")
    return report


def validate_variant_release_report(
    report: Mapping[str, object],
) -> dict[str, object]:
    """Validate a complete pass or evidence-bound private-candidate matrix."""

    materialized = dict(report)
    _validate(_REPORT_SCHEMA, materialized, "variant release matrix report")
    if materialized.get("tested_variants") != list(_CANONICAL_VARIANTS):
        raise VariantReleaseError("variant release matrix tested variants are not canonical")
    variants = materialized.get("variants")
    summaries = materialized.get("variant_summaries")
    failures = materialized.get("ineligible_variants")
    qualification_failures = materialized.get("qualification_failures")
    if (
        not isinstance(variants, list)
        or not isinstance(summaries, Mapping)
        or not isinstance(failures, Mapping)
        or not isinstance(qualification_failures, Mapping)
    ):
        raise VariantReleaseError("variant release matrix qualification fields are invalid")
    expected_eligible = [
        variant for variant in _CANONICAL_VARIANTS if variant not in failures
    ]
    if variants != expected_eligible or set(summaries) != set(expected_eligible):
        raise VariantReleaseError(
            "variant release matrix eligible variants and summaries differ"
        )
    if "bf16" not in expected_eligible:
        raise VariantReleaseError("variant release matrix has no eligible bf16 baseline")
    expected_reasons = [
        f"{variant}:{failures[variant]['stage']}:{failures[variant]['reason_code']}"
        for variant in _CANONICAL_VARIANTS
        if variant in failures
    ] + [
        (
            f"{variant}:qualification:{qualification_failures[variant]['stage']}:"
            f"{qualification_failures[variant]['reason_code']}"
        )
        for variant in _CANONICAL_VARIANTS
        if variant in qualification_failures
    ]
    if materialized.get("reasons") != expected_reasons:
        raise VariantReleaseError("variant release matrix reasons differ from failures")
    passed = materialized.get("passed") is True
    if passed != (not failures and not qualification_failures):
        raise VariantReleaseError("variant release matrix passed state differs from failures")
    return materialized


def derive_qualification_failures(
    config: Mapping[str, object],
    records: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, str]]:
    """Bind deterministic release-gate failures to the evidence that proves them."""

    failures: dict[str, dict[str, str]] = {}
    budgets = _mapping(config.get("quality_delta_budgets"), "quality delta budgets")
    for variant in config["enabled_variants"]:
        if variant not in records:
            continue
        record = _mapping(records[variant], f"{variant} record")
        evidence_hashes = _mapping(
            record.get("evidence_hashes"),
            f"{variant} evidence hashes",
        )
        evaluation = _mapping(record.get("evaluation"), f"{variant} evaluation")
        if evaluation.get("passed") is not True:
            failures[variant] = {
                "stage": "evaluation",
                "reason_code": "hard_quality_gates_failed",
                "evidence_sha256": _require_hash(
                    evidence_hashes.get("evaluation_report"),
                    f"{variant} evaluation evidence hash",
                ),
            }
            continue
        delta = _mapping(record.get("delta"), f"{variant} delta")
        maximum_drop = _number(
            delta.get("maximum_absolute_metric_drop"),
            f"{variant} maximum metric drop",
        )
        new_critical_errors = _integer(
            delta.get("new_critical_errors"),
            f"{variant} new critical errors",
        )
        if variant == "bf16":
            allowed_drop = 0.0
            allowed_critical_errors = 0
        else:
            budget = _mapping(budgets.get(variant), f"{variant} delta budget")
            allowed_drop = _number(
                budget.get("maximum_absolute_metric_drop"),
                f"{variant} configured maximum metric drop",
            )
            allowed_critical_errors = _integer(
                budget.get("maximum_new_critical_errors"),
                f"{variant} configured maximum new critical errors",
            )
        if maximum_drop > allowed_drop or new_critical_errors > allowed_critical_errors:
            failures[variant] = {
                "stage": "delta",
                "reason_code": "quality_delta_budget_exceeded",
                "evidence_sha256": _require_hash(
                    evidence_hashes.get("delta_report"),
                    f"{variant} delta evidence hash",
                ),
            }
    return failures


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise VariantReleaseError(f"release evidence is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VariantReleaseError(f"{label} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VariantReleaseError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise VariantReleaseError(f"{label} must contain a JSON object")
    return value


def collect_variant_release_failures(
    matrix_path: str | Path,
) -> dict[str, dict[str, str]]:
    """Verify evidence files for variants that failed before full qualification."""

    matrix_file = Path(matrix_path).resolve()
    matrix = _load_json(matrix_file, "release matrix input")
    definitions = _mapping(matrix.get("variants"), "release matrix variants")
    failures = _mapping(
        matrix.get("ineligible_variants"),
        "release matrix ineligible variants",
    )
    if set(definitions).intersection(failures):
        raise VariantReleaseError(
            "release matrix variant cannot be both eligible and ineligible"
        )
    if set(definitions).union(failures) != set(_CANONICAL_VARIANTS):
        raise VariantReleaseError(
            "release matrix does not account for every canonical variant"
        )
    normalized: dict[str, dict[str, str]] = {}
    for variant in _CANONICAL_VARIANTS:
        if variant not in failures:
            continue
        failure = _mapping(failures[variant], f"{variant} failure source")
        evidence_file = failure.get("evidence_file")
        if not isinstance(evidence_file, str) or not evidence_file:
            raise VariantReleaseError(f"{variant} failure evidence file is missing")
        evidence_path = (matrix_file.parent / evidence_file).resolve()
        evidence_sha256 = _sha256_file(evidence_path)
        if evidence_sha256 != failure.get("evidence_sha256"):
            raise VariantReleaseError(
                f"{variant} failure evidence file hash differs"
            )
        normalized[variant] = {
            "stage": str(failure.get("stage")),
            "reason_code": str(failure.get("reason_code")),
            "evidence_sha256": evidence_sha256,
        }
    return normalized


def collect_variant_release_records(
    matrix_path: str | Path,
    config: Mapping[str, object],
    *,
    eligible_variants: tuple[str, ...] | list[str] | None = None,
) -> dict[str, dict[str, object]]:
    """Load path-based evidence, verify real artifacts/files, and normalize records."""

    matrix_file = Path(matrix_path).resolve()
    matrix = _load_json(matrix_file, "release matrix input")
    definitions = _mapping(matrix.get("variants"), "release matrix variants")
    records: dict[str, dict[str, object]] = {}
    baseline_evaluation: dict[str, Any] | None = None
    baseline_evaluation_sha256: str | None = None
    selected_variants = list(
        (
            variant
            for variant in config["enabled_variants"]
            if variant in definitions
        )
        if eligible_variants is None
        else eligible_variants
    )
    if "bf16" not in selected_variants:
        raise VariantReleaseError("eligible release evidence must include bf16 first")
    canonical_order = [
        variant
        for variant in config["enabled_variants"]
        if variant in selected_variants
    ]
    if selected_variants != canonical_order or len(selected_variants) != len(
        set(selected_variants)
    ):
        raise VariantReleaseError(
            "eligible release evidence variants must use canonical order without duplicates"
        )
    for variant in selected_variants:
        definition = _mapping(definitions.get(variant), f"{variant} matrix definition")

        def evidence_path(
            name: str,
            *,
            definition: Mapping[str, Any] = definition,
            variant: str = variant,
        ) -> Path:
            value = definition.get(name)
            if not isinstance(value, str) or not value:
                raise VariantReleaseError(f"{variant} matrix path {name} is missing")
            return (matrix_file.parent / value).resolve()

        artifact_root = evidence_path("artifact_root")
        artifact = verify_variant_artifact_manifest(artifact_root)
        manifest_path = artifact_root / VARIANT_MANIFEST_FILENAME
        reload = _load_json(evidence_path("reload_report"), f"{variant} reload report")
        reload_sha256 = _sha256_file(evidence_path("reload_report"))
        artifact_reload = _mapping(artifact.get("reload"), f"{variant} artifact reload")
        for field in (
            "status",
            "process_boundary",
            "configuration_source",
            "deterministic",
            "valid_sst",
            "generated_tokens",
            "protection_policy",
        ):
            if reload.get(field) != artifact_reload.get(field):
                raise VariantReleaseError(f"{variant} reload report differs from artifact manifest at {field}")
        normalized_reload = dict(reload)
        normalized_reload.update(
            {
                "variant": variant,
                "artifact_tree_sha256": artifact["artifact"]["tree_sha256"],
                "training_fingerprint": artifact["training_fingerprint"],
                "report_sha256": reload_sha256,
            }
        )
        prediction_manifest = _load_json(
            evidence_path("predictions_manifest"),
            f"{variant} predictions manifest",
        )
        predictions_path = evidence_path("predictions_file")
        prediction_hash = _sha256_file(predictions_path)
        prediction_count = 0
        for line_number, line in enumerate(
            predictions_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line:
                raise VariantReleaseError(f"{variant} predictions has blank line {line_number}")
            try:
                json.loads(line)
            except json.JSONDecodeError as error:
                raise VariantReleaseError(f"{variant} predictions has invalid JSON at line {line_number}") from error
            prediction_count += 1
        evaluation_path = evidence_path("evaluation_report")
        evaluation = _load_json(evaluation_path, f"{variant} evaluation report")
        evaluation_sha256 = _sha256_file(evaluation_path)
        if variant == "bf16":
            baseline_evaluation = evaluation
            baseline_evaluation_sha256 = evaluation_sha256
        if baseline_evaluation is None or baseline_evaluation_sha256 is None:
            raise VariantReleaseError("bf16 evaluation must be loaded before derived variant deltas")
        unverified_delta = _load_json(
            evidence_path("delta_report"),
            f"{variant} delta report",
        )
        delta_report_sha256 = _sha256_file(evidence_path("delta_report"))
        try:
            delta = verify_quality_delta_report(
                unverified_delta,
                baseline_evaluation,
                evaluation,
                baseline_sha256=baseline_evaluation_sha256,
                candidate_sha256=evaluation_sha256,
                candidate_variant=variant,
            )
        except QualityDeltaError as error:
            raise VariantReleaseError(f"{variant} quality delta verification failed: {error}") from error
        benchmark = _load_json(evidence_path("benchmark_report"), f"{variant} benchmark report")
        benchmark_report_sha256 = _sha256_file(evidence_path("benchmark_report"))
        _validate(_BENCHMARK_SCHEMA, benchmark, f"{variant} benchmark report")
        resources = _mapping(benchmark.get("resources"), f"{variant} benchmark resources")
        hardware = _mapping(resources.get("hardware"), f"{variant} hardware identity")
        normalized_benchmark = dict(benchmark)
        normalized_benchmark["report_sha256"] = benchmark_report_sha256
        normalized_benchmark["hardware_identity_complete"] = all(
            hardware.get(field) not in {None, "", 0}
            for field in (
                "os",
                "python",
                "cpu_name",
                "logical_cpu_count",
                "physical_cpu_count",
                "total_ram_bytes",
                "gpu_name",
                "gpu_uuid",
                "gpu_driver_version",
                "gpu_total_memory_bytes",
            )
        )
        normalized_evaluation = dict(evaluation)
        gate = _mapping(evaluation.get("gate"), f"{variant} evaluation gate")
        declared_evaluation_prediction_hash = evaluation.get("prediction_sha256")
        if declared_evaluation_prediction_hash not in {
            prediction_hash,
            prediction_hash.removeprefix("sha256:"),
        }:
            raise VariantReleaseError(f"{variant} evaluation predictions hash differs from the prediction file")
        normalized_evaluation.update(
            {
                "status": "complete",
                "passed": gate.get("passed") is True,
                "predictions_sha256": prediction_hash,
                "report_sha256": evaluation_sha256,
            }
        )
        metrics = _mapping(evaluation.get("metrics"), f"{variant} evaluation metrics")
        reference_count = _integer(
            metrics.get("total"),
            f"{variant} evaluated case count",
            minimum=1,
        )
        declared_prediction_hash = prediction_manifest.get(
            "predictions_sha256",
            prediction_manifest.get("prediction_sha256"),
        )
        if declared_prediction_hash not in {
            prediction_hash,
            prediction_hash.removeprefix("sha256:"),
        }:
            raise VariantReleaseError(f"{variant} predictions file hash differs from manifest")
        declared_prediction_count = prediction_manifest.get(
            "prediction_count",
            prediction_manifest.get("record_count"),
        )
        if declared_prediction_count != prediction_count:
            raise VariantReleaseError(f"{variant} predictions row count differs from manifest")
        normalized_predictions = {
            "status": "complete",
            "variant": variant,
            "artifact_tree_sha256": artifact["artifact"]["tree_sha256"],
            "reference_count": reference_count,
            "prediction_count": prediction_count,
            "exact_case_coverage": evaluation.get("exact_case_coverage") is True,
            "predictions_sha256": prediction_hash,
        }
        records[variant] = {
            "artifact": {
                "variant": artifact["variant"],
                "manifest_sha256": _sha256_file(manifest_path),
                "artifact_tree_sha256": artifact["artifact"]["tree_sha256"],
                "training_fingerprint": artifact["training_fingerprint"],
                "quantization_config_sha256": artifact["quantization"]["config_sha256"],
                "toolchain_sha256": "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        artifact["toolchain"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "protection_policy": dict(artifact["protection_policy"]),
            },
            "reload": normalized_reload,
            "predictions": normalized_predictions,
            "evaluation": normalized_evaluation,
            "delta": {**delta, "report_sha256": delta_report_sha256},
            "benchmark": normalized_benchmark,
            "evidence_hashes": {
                "artifact_manifest": _sha256_file(manifest_path),
                "reload_report": reload_sha256,
                "predictions": prediction_hash,
                "evaluation_report": evaluation_sha256,
                "delta_report": delta_report_sha256,
                "benchmark_report": benchmark_report_sha256,
            },
        }
    return records
