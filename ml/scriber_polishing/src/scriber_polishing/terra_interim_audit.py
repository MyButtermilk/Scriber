"""Fail-closed, leakage-safe interim auditing for partial Terra judge stages.

This module intentionally validates only the existing Terra stage artifacts.  It
does not synthesize a Sol result, does not invoke a release decision, and never
emits source, reference, or candidate text.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .judge_aggregation import (
    judge_bundle_sha256,
    judge_verdicts_sha256,
    resume_judge_stage_verdicts,
)
from .judge_bundle import (
    JUDGE_SCORE_DIMENSIONS,
    validate_judge_bundle_case,
    validate_judge_bundle_manifest,
    validate_judge_stage_packet_manifest,
    validate_judge_stage_resume_manifest,
)
from .schemas import validate_judge_verdict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _PROJECT_ROOT / "contracts"
_COMPARISON_KINDS = frozenset({"fine_tune_vs_base", "fine_tune_vs_rule_baseline"})
_OPPONENT_ROLES = {
    "fine_tune_vs_base": "base_model",
    "fine_tune_vs_rule_baseline": "rule_baseline",
}
_PRIVATE_MAPPING_FIELDS = frozenset(
    {
        "schema_version",
        "bundle_sha256",
        "blind_seed",
        "comparison_kind",
        "candidate_labels",
        "candidate_roles",
        "reference_sha256",
        "reference_manifest_sha256",
        "coverage_policy_sha256",
        "materialization_plan_sha256",
        "candidate_prediction_sha256",
        "cases",
    }
)
_PRIVATE_CASE_FIELDS = frozenset(
    {
        "public_case_id",
        "base_case_id",
        "position_variant",
        "candidate_a_label",
        "candidate_b_label",
        "candidate_outputs_identical",
        "paired_public_case_id",
    }
)
_POLICY = "generate_novel_sibling_cases_only"


@dataclass(frozen=True, slots=True)
class TerraPackagePaths:
    """All inputs needed to validate one comparison without a full campaign."""

    comparison_kind: str
    verdicts: Path
    stage_packet: Path
    stage_packet_manifest: Path
    bundle: Path
    private_mapping: Path
    bundle_manifest: Path


@dataclass(frozen=True, slots=True)
class TerraPackageAudit:
    """Validated, internal-only material used to create a safe aggregate."""

    comparison_kind: str
    verdicts: tuple[dict[str, Any], ...]
    cases_by_id: dict[str, dict[str, Any]]
    private_cases: dict[str, dict[str, Any]]
    fine_tune_label: str
    provenance: dict[str, str]
    package_summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TerraInterimArtifacts:
    """Canonical report, backlog, and manifest payloads ready for serialization."""

    audit_report: dict[str, Any]
    backlog_records: tuple[dict[str, Any], ...]
    markdown_report: str
    artifact_manifest: dict[str, Any]


def canonical_json(value: object) -> str:
    """Serialize one report object using the repository's canonical JSON bytes."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_json_bytes(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _canonical_jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    if not rows:
        raise ValueError("canonical JSONL output must not be empty")
    return ("\n".join(canonical_json(row) for row in rows) + "\n").encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _prefixed_sha256(payload: bytes) -> str:
    return "sha256:" + _sha256_bytes(payload)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _load_canonical_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    if raw != _canonical_json_bytes(value):
        raise ValueError(f"{label} must use canonical JSON bytes")
    return value, raw


def _load_canonical_jsonl(path: Path, label: str) -> tuple[list[dict[str, Any]], bytes]:
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"{label} contains a blank JSONL row at {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{label} row {line_number} must be a JSON object")
        rows.append(value)
    if not rows:
        raise ValueError(f"{label} is empty")
    if raw != _canonical_jsonl_bytes(rows):
        raise ValueError(f"{label} must use canonical JSONL bytes")
    return rows, raw


@cache
def _contract_validator(filename: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((_CONTRACTS / filename).read_text(encoding="utf-8")))


def _validate_output(value: Mapping[str, Any], filename: str, label: str) -> dict[str, Any]:
    materialized = dict(value)
    errors = sorted(_contract_validator(filename).iter_errors(materialized), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"{label} schema failed: {errors[0].message}")
    return materialized


def validate_terra_remediation_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Reject remediation output with non-contract fields, including text payloads."""

    return _validate_output(record, "terra_remediation_backlog_schema.json", "Terra remediation record")


def validate_terra_interim_audit(report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a non-release interim report."""

    return _validate_output(report, "terra_interim_audit_schema.json", "Terra interim audit")


def validate_terra_interim_artifact_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate hashes that bind the three emitted interim artifacts."""

    return _validate_output(
        manifest,
        "terra_interim_artifact_manifest_schema.json",
        "Terra interim artifact manifest",
    )


def _validate_private_mapping(
    payload: Mapping[str, Any],
    *,
    cases_by_id: Mapping[str, Mapping[str, Any]],
    bundle_manifest: Mapping[str, Any],
    comparison_kind: str,
    bundle_sha256: str,
) -> tuple[dict[str, dict[str, Any]], str]:
    if set(payload) != _PRIVATE_MAPPING_FIELDS or payload.get("schema_version") != 2:
        raise ValueError("private judge mapping has an unexpected shape or schema version")
    if payload["comparison_kind"] != comparison_kind:
        raise ValueError("private judge mapping comparison differs from the package")
    if payload["bundle_sha256"] != bundle_sha256:
        raise ValueError("private judge mapping is bound to another bundle")
    if not isinstance(payload["blind_seed"], int) or isinstance(payload["blind_seed"], bool):
        raise ValueError("private judge mapping blind seed is invalid")
    for field in (
        "reference_sha256",
        "reference_manifest_sha256",
        "coverage_policy_sha256",
        "materialization_plan_sha256",
    ):
        if not _is_sha256(payload[field]) or payload[field] != bundle_manifest[field]:
            raise ValueError("private judge mapping provenance differs from the bundle manifest")

    labels = payload["candidate_labels"]
    roles = payload["candidate_roles"]
    if (
        not isinstance(labels, list)
        or len(labels) != 2
        or len(set(labels)) != 2
        or not all(isinstance(label, str) and label for label in labels)
        or not isinstance(roles, dict)
        or set(roles) != set(labels)
        or set(roles.values()) != {"fine_tune", _OPPONENT_ROLES[comparison_kind]}
    ):
        raise ValueError("private judge mapping candidate roles are invalid")
    fine_tune_labels = [label for label, role in roles.items() if role == "fine_tune"]
    if len(fine_tune_labels) != 1:
        raise ValueError("private judge mapping must have one fine-tune label")

    prediction_hashes = payload["candidate_prediction_sha256"]
    if not isinstance(prediction_hashes, dict) or set(prediction_hashes) != set(labels):
        raise ValueError("private judge mapping prediction hashes are invalid")
    if not all(_is_sha256(value) for value in prediction_hashes.values()):
        raise ValueError("private judge mapping prediction hash is invalid")

    raw_cases = payload["cases"]
    if not isinstance(raw_cases, dict) or set(raw_cases) != set(cases_by_id):
        raise ValueError("private judge mapping does not cover the exact bundle")
    private_cases: dict[str, dict[str, Any]] = {}
    for public_case_id, raw_mapping in raw_cases.items():
        if not isinstance(raw_mapping, dict) or set(raw_mapping) != _PRIVATE_CASE_FIELDS:
            raise ValueError("private judge mapping case has an unexpected shape")
        if raw_mapping["public_case_id"] != public_case_id:
            raise ValueError("private judge mapping public case ID differs from its key")
        if not isinstance(raw_mapping["base_case_id"], str) or not raw_mapping["base_case_id"]:
            raise ValueError("private judge mapping base case ID is invalid")
        if raw_mapping["position_variant"] not in {"primary", "swapped"}:
            raise ValueError("private judge mapping position variant is invalid")
        if (
            raw_mapping["candidate_a_label"] not in labels
            or raw_mapping["candidate_b_label"] not in labels
            or raw_mapping["candidate_a_label"] == raw_mapping["candidate_b_label"]
            or not isinstance(raw_mapping["candidate_outputs_identical"], bool)
        ):
            raise ValueError("private judge mapping candidate labels are invalid")
        paired_public_case_id = raw_mapping["paired_public_case_id"]
        if paired_public_case_id is not None and paired_public_case_id not in raw_cases:
            raise ValueError("private judge mapping paired case is absent")
        public_case = cases_by_id[public_case_id]
        if raw_mapping["candidate_outputs_identical"] is not (public_case["candidate_a"] == public_case["candidate_b"]):
            raise ValueError("private judge mapping candidate equivalence differs from the bundle")
        if raw_mapping["candidate_outputs_identical"] and (
            public_case["deterministic_critical_flags"]["A"] != public_case["deterministic_critical_flags"]["B"]
        ):
            raise ValueError("identical public candidates have differing deterministic flags")
        private_cases[public_case_id] = dict(raw_mapping)

    for public_case_id, mapping in private_cases.items():
        paired_public_case_id = mapping["paired_public_case_id"]
        if paired_public_case_id is None:
            continue
        paired = private_cases[paired_public_case_id]
        if paired["paired_public_case_id"] != public_case_id:
            raise ValueError("private judge mapping paired case relation is not symmetric")
        if paired["base_case_id"] != mapping["base_case_id"]:
            raise ValueError("private judge mapping paired case changes its base case")
    return private_cases, fine_tune_labels[0]


def audit_terra_package(paths: TerraPackagePaths) -> TerraPackageAudit:
    """Validate one exact partial Terra stage and retain only internal audit state."""

    if paths.comparison_kind not in _COMPARISON_KINDS:
        raise ValueError("unknown Terra comparison kind")

    bundle_rows, bundle_bytes = _load_canonical_jsonl(paths.bundle, "judge bundle")
    validated_bundle = tuple(validate_judge_bundle_case(row) for row in bundle_rows)
    bundle_sha256 = _sha256_bytes(bundle_bytes)
    if judge_bundle_sha256(validated_bundle) != bundle_sha256:
        raise ValueError("judge bundle canonical hash differs from its bytes")
    cases_by_id = {case["case_id"]: case for case in validated_bundle}
    if len(cases_by_id) != len(validated_bundle):
        raise ValueError("judge bundle contains duplicate case IDs")

    bundle_manifest, bundle_manifest_bytes = _load_canonical_json(paths.bundle_manifest, "judge bundle manifest")
    bundle_manifest = validate_judge_bundle_manifest(bundle_manifest)
    if bundle_manifest["bundle_sha256"] != bundle_sha256:
        raise ValueError("judge bundle manifest differs from bundle bytes")

    private_payload, private_bytes = _load_canonical_json(paths.private_mapping, "private judge mapping")
    private_mapping_sha256 = _sha256_bytes(private_bytes)
    if bundle_manifest["private_mapping_sha256"] != private_mapping_sha256:
        raise ValueError("judge bundle manifest differs from private mapping bytes")
    private_cases, fine_tune_label = _validate_private_mapping(
        private_payload,
        cases_by_id=cases_by_id,
        bundle_manifest=bundle_manifest,
        comparison_kind=paths.comparison_kind,
        bundle_sha256=bundle_sha256,
    )

    stage_rows, stage_packet_bytes = _load_canonical_jsonl(paths.stage_packet, "Terra stage packet")
    stage_manifest, stage_packet_manifest_bytes = _load_canonical_json(
        paths.stage_packet_manifest,
        "Terra stage packet manifest",
    )
    stage_manifest = validate_judge_stage_packet_manifest(stage_manifest)
    stage_packet_sha256 = _sha256_bytes(stage_packet_bytes)
    if stage_manifest["judge_stage"] != "terra_prejudge":
        raise ValueError("interim audit accepts only Terra prejudge packets")
    if stage_manifest["stage_bundle_sha256"] != stage_packet_sha256:
        raise ValueError("Terra stage packet manifest differs from packet bytes")
    if stage_manifest["reviewed_bundle_sha256"] != bundle_sha256:
        raise ValueError("Terra stage packet is bound to another judge bundle")
    if stage_manifest["source_public_case_count"] != len(validated_bundle):
        raise ValueError("Terra stage packet source count differs from bundle")
    if set(case["case_id"] for case in stage_rows).difference(cases_by_id):
        raise ValueError("Terra stage packet contains a case outside the bundle")

    verdict_rows, verdict_bytes = _load_canonical_jsonl(paths.verdicts, "Terra verdicts")
    verdicts = tuple(validate_judge_verdict(row) for row in verdict_rows)
    source_verdict_sha256 = _sha256_bytes(verdict_bytes)
    if judge_verdicts_sha256(verdicts) != source_verdict_sha256:
        raise ValueError("Terra verdict output hash is not canonical")
    resumed = resume_judge_stage_verdicts(stage_rows, stage_manifest, (verdicts,))
    merged_verdict_bytes = _canonical_jsonl_bytes(resumed.verdicts)
    if source_verdict_sha256 != _sha256_bytes(merged_verdict_bytes):
        raise ValueError("Terra verdicts are not in canonical stage order")

    first_verdict = resumed.verdicts[0]
    resume_manifest = validate_judge_stage_resume_manifest(
        {
            "schema_version": 1,
            "status": resumed.status,
            "judge_stage": first_verdict["judge_stage"],
            "judge_id": first_verdict["judge_id"],
            "judge_session_id": first_verdict["judge_session_id"],
            "model_family": first_verdict["model_family"],
            "reasoning_effort": first_verdict["reasoning_effort"],
            "prompt_hash": first_verdict["prompt_hash"],
            "reviewed_bundle_sha256": first_verdict["reviewed_bundle_sha256"],
            "stage_assignment_sha256": first_verdict["stage_assignment_sha256"],
            "stage_packet_sha256": stage_packet_sha256,
            "stage_packet_manifest_sha256": _sha256_bytes(stage_packet_manifest_bytes),
            "expected_count": len(stage_rows),
            "completed_count": resumed.completed_count,
            "missing_count": len(resumed.missing_case_ids),
            "missing_case_ids": list(resumed.missing_case_ids),
            "merged_verdict_output_sha256": source_verdict_sha256,
            "output_manifest_sha256": None,
            "hard_deterministic_errors_are_binding": True,
        }
    )
    if resumed.status == "complete":
        raise ValueError("Terra interim audit is reserved for incomplete stages")

    provenance = {
        "comparison_kind": paths.comparison_kind,
        "judge_stage": first_verdict["judge_stage"],
        "judge_id": first_verdict["judge_id"],
        "judge_session_id": first_verdict["judge_session_id"],
        "model_family": first_verdict["model_family"],
        "reasoning_effort": first_verdict["reasoning_effort"],
        "prompt_hash": first_verdict["prompt_hash"],
        "stage_assignment_sha256": first_verdict["stage_assignment_sha256"],
        "verdict_output_sha256": source_verdict_sha256,
        "stage_packet_sha256": stage_packet_sha256,
        "stage_packet_manifest_sha256": _sha256_bytes(stage_packet_manifest_bytes),
        "bundle_sha256": bundle_sha256,
        "bundle_manifest_sha256": _sha256_bytes(bundle_manifest_bytes),
        "private_mapping_sha256": private_mapping_sha256,
    }
    package_summary = {
        "comparison_kind": paths.comparison_kind,
        "judge_stage": first_verdict["judge_stage"],
        "package_status": resumed.status,
        "expected_case_count": len(stage_rows),
        "completed_case_count": resumed.completed_count,
        "missing_case_count": len(resumed.missing_case_ids),
        "coverage_fraction": resumed.completed_count / len(stage_rows),
        "judge_id": first_verdict["judge_id"],
        "judge_session_id": first_verdict["judge_session_id"],
        "model_family": first_verdict["model_family"],
        "reasoning_effort": first_verdict["reasoning_effort"],
        "prompt_hash": first_verdict["prompt_hash"],
        "stage_assignment_sha256": first_verdict["stage_assignment_sha256"],
        "verdict_output_sha256": source_verdict_sha256,
        "resume_manifest_sha256": _sha256_bytes(_canonical_json_bytes(resume_manifest)),
        "stage_packet_sha256": stage_packet_sha256,
        "stage_packet_manifest_sha256": _sha256_bytes(stage_packet_manifest_bytes),
        "bundle_sha256": bundle_sha256,
        "bundle_manifest_sha256": _sha256_bytes(bundle_manifest_bytes),
        "private_mapping_sha256": private_mapping_sha256,
    }
    return TerraPackageAudit(
        comparison_kind=paths.comparison_kind,
        verdicts=tuple(resumed.verdicts),
        cases_by_id=cases_by_id,
        private_cases=private_cases,
        fine_tune_label=fine_tune_label,
        provenance=provenance,
        package_summary=package_summary,
    )


def build_remediation_records(
    *,
    comparison_kind: str,
    verdicts: Sequence[Mapping[str, Any]],
    cases_by_id: Mapping[str, Mapping[str, Any]],
    private_cases: Mapping[str, Mapping[str, Any]],
    fine_tune_label: str,
    provenance: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Aggregate only failure signals for one validated Terra comparison.

    The stable remediation key is intentionally a one-way identifier.  No public
    judge ID, base case ID, text, AST, candidate, or judge reason is emitted.
    """

    grouped: dict[str, dict[str, Any]] = {}
    for raw_verdict in verdicts:
        verdict = validate_judge_verdict(raw_verdict)
        public_case_id = verdict["case_id"]
        if public_case_id not in private_cases or public_case_id not in cases_by_id:
            raise ValueError("Terra verdict cannot be attributed to the validated package")
        mapping = private_cases[public_case_id]
        selected_label = mapping["candidate_a_label"] if verdict["candidate"] == "A" else mapping["candidate_b_label"]
        fine_tune_position = "A" if mapping["candidate_a_label"] == fine_tune_label else "B"
        deterministic_flags = set(cases_by_id[public_case_id]["deterministic_critical_flags"][fine_tune_position])
        fine_tune_selected = selected_label == fine_tune_label
        attributed_flags = set(deterministic_flags)
        signals: set[str] = set()
        if not fine_tune_selected:
            signals.add("pairwise_preference_loss")
        if fine_tune_selected and not verdict["acceptable"]:
            signals.add("selected_fine_tune_unacceptable")
        if fine_tune_selected and verdict["critical_error"]:
            signals.add("selected_fine_tune_critical")
        if deterministic_flags:
            signals.add("deterministic_fine_tune_error")
        if not signals:
            continue
        if fine_tune_selected:
            attributed_flags.update(verdict["flags"])

        base_case_id = mapping["base_case_id"]
        item = grouped.setdefault(
            base_case_id,
            {
                "failure_signals": set(),
                "error_flags": set(),
                "public_position_variants": 0,
                "fine_tune_selected_count": 0,
                "fine_tune_rejected_count": 0,
                "fine_tune_critical_count": 0,
                "pairwise_loss_count": 0,
                "score_observation_count": 0,
                "minimum_selected_fine_tune_scores": {},
            },
        )
        item["failure_signals"].update(signals)
        item["error_flags"].update(attributed_flags)
        item["public_position_variants"] += 1
        if fine_tune_selected:
            item["fine_tune_selected_count"] += 1
            item["fine_tune_rejected_count"] += int(not verdict["acceptable"])
            item["fine_tune_critical_count"] += int(verdict["critical_error"])
            item["score_observation_count"] += 1
            minima = item["minimum_selected_fine_tune_scores"]
            for dimension in JUDGE_SCORE_DIMENSIONS:
                minima[dimension] = min(
                    minima.get(dimension, verdict["scores"][dimension]), verdict["scores"][dimension]
                )
        else:
            item["pairwise_loss_count"] += 1

    records: list[dict[str, Any]] = []
    for base_case_id in sorted(grouped):
        item = grouped[base_case_id]
        signals = sorted(item["failure_signals"])
        severity = (
            "critical"
            if {"selected_fine_tune_critical", "deterministic_fine_tune_error"}.intersection(signals)
            else "high"
        )
        record = {
            "schema_version": 1,
            "comparison_kind": comparison_kind,
            "remediation_case_key": _prefixed_sha256(f"{comparison_kind}\0{base_case_id}".encode()),
            "failure_signals": signals,
            "severity": severity,
            "error_flags": sorted(item["error_flags"]),
            "public_position_variants": item["public_position_variants"],
            "fine_tune_selected_count": item["fine_tune_selected_count"],
            "fine_tune_rejected_count": item["fine_tune_rejected_count"],
            "fine_tune_critical_count": item["fine_tune_critical_count"],
            "pairwise_loss_count": item["pairwise_loss_count"],
            "score_observation_count": item["score_observation_count"],
            "minimum_selected_fine_tune_scores": dict(sorted(item["minimum_selected_fine_tune_scores"].items())),
            "future_training_policy": _POLICY,
            "direct_test_case_training_use_allowed": False,
            "provenance": dict(provenance),
        }
        records.append(validate_terra_remediation_record(record))
    return tuple(records)


def _sorted_counts(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _markdown_report(report: Mapping[str, Any]) -> str:
    packages = report["packages"]
    backlog = report["backlog"]
    lines = [
        "# Terra-Zwischenstands-Audit",
        "",
        "## Technische Zusammenfassung",
        "",
        "Die vorliegenden Terra-Verdicts wurden kanonisch und hash-gebunden validiert. "
        "Dieser Bericht ist ausdrücklich kein vollständiges Kampagnen- oder Release-Ergebnis.",
        "",
        f"- Validierte Teilstände: **{len(packages)}**",
        f"- Remediation-Kandidaten: **{backlog['record_count']}**",
        "- Neue Trainingsbeispiele: **0**",
        "- Direkte Wiederverwendung von Test-/Challenge-Fällen: **verboten**",
        "",
        "## Validierte Terra-Teilstände",
        "",
        "| Vergleich | Erwartet | Vollständig | Fehlend | Abdeckung | Status |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for package in packages:
        lines.append(
            "| {comparison_kind} | {expected_case_count} | {completed_case_count} | "
            "{missing_case_count} | {coverage:.2%} | {package_status} |".format(
                coverage=package["coverage_fraction"],
                **package,
            )
        )
    lines.extend(
        [
            "",
            "## Backlog-Signale",
            "",
            "Der Backlog enthält ausschließlich pseudonyme Fall-IDs, Fehlerflags, Scores und Hash-Provenienz. "
            "Er enthält keine Rohtexte, Referenzen, Kandidatentexte oder neue Trainingsbeispiele.",
            "",
        ]
    )
    for severity, count in sorted(backlog["severity_counts"].items()):
        lines.append(f"- Schweregrad `{severity}`: {count}")
    for signal, count in sorted(backlog["failure_signal_counts"].items()):
        lines.append(f"- Signal `{signal}`: {count}")
    lines.extend(
        [
            "",
            "## Methode und Provenienz",
            "",
            "Jeder Teilstand wurde gegen das vollständige Stage-Paket, dessen Manifest, den geblindeten Bundle-Hash, "
            "das private Mapping und die bestehenden Verdict-/Resume-Schemas geprüft. Der Backlog verwendet nur die "
            "bereits bindenden deterministischen Flags und die ausgewählten Terra-Verdicts.",
            "",
            "## Grenzen und Gates",
            "",
            "Die Terra-Teilstände sind unvollständig. Sol-Release- und Tie-Break-Stufen liegen diesem Audit nicht vor. "
            "Daher wurde keine Voll-Aggregation ausgeführt und kein Release-Gate bewertet oder bestanden.",
            "",
            "## Empfohlene nächste Schritte",
            "",
            "1. Die fehlenden Terra-Pakete nur mit den bestehenden bindenden Manifesten abschließen.",
            "2. Danach die unveränderten Terra/Sol-Vollaggregatoren verwenden.",
            "3. Aus dem Backlog ausschließlich Generatoranforderungen für unabhängig geseedete neue Schwesterfälle ableiten.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_terra_interim_artifacts(packages: Sequence[TerraPackageAudit]) -> TerraInterimArtifacts:
    """Build a two-comparison, non-release report and safe remediation backlog."""

    if len(packages) != 2 or {package.comparison_kind for package in packages} != _COMPARISON_KINDS:
        raise ValueError("Terra interim audit requires exactly the two canonical comparisons")
    ordered_packages = tuple(sorted(packages, key=lambda package: package.comparison_kind))
    records = tuple(
        record
        for package in ordered_packages
        for record in build_remediation_records(
            comparison_kind=package.comparison_kind,
            verdicts=package.verdicts,
            cases_by_id=package.cases_by_id,
            private_cases=package.private_cases,
            fine_tune_label=package.fine_tune_label,
            provenance=package.provenance,
        )
    )
    records = tuple(sorted(records, key=lambda record: (record["comparison_kind"], record["remediation_case_key"])))
    backlog_bytes = _canonical_jsonl_bytes(records) if records else b""
    severity_counts = Counter(record["severity"] for record in records)
    signal_counts = Counter(signal for record in records for signal in record["failure_signals"])
    flag_counts = Counter(flag for record in records for flag in record["error_flags"])
    package_summaries = [dict(package.package_summary) for package in ordered_packages]
    report = validate_terra_interim_audit(
        {
            "schema_version": 1,
            "kind": "scriber_terra_interim_audit",
            "status": "in_progress",
            "packages": package_summaries,
            "backlog": {
                "record_count": len(records),
                "severity_counts": _sorted_counts(severity_counts),
                "failure_signal_counts": _sorted_counts(signal_counts),
                "error_flag_counts": _sorted_counts(flag_counts),
                "backlog_sha256": "sha256:" + _sha256_bytes(backlog_bytes),
            },
            "release_aggregation": {
                "full_campaign_aggregation_performed": False,
                "release_gate_evaluated": False,
                "release_gate_passed": False,
                "blocked_reason": "Terra-only interim audit: incomplete Terra coverage and no Sol release or tie-break evidence.",
            },
            "training_policy": {
                "new_training_examples_created": 0,
                "direct_test_or_challenge_reuse": "prohibited",
                "allowed_followup": "derive_generator_requirements_and_create_independently_seeded_novel_siblings_after_evaluation",
            },
        }
    )
    report_bytes = _canonical_json_bytes(report)
    markdown_report = _markdown_report(report)
    markdown_bytes = markdown_report.encode("utf-8")
    input_provenance = {"packages": package_summaries}
    artifact_manifest = validate_terra_interim_artifact_manifest(
        {
            "schema_version": 1,
            "kind": "scriber_terra_interim_artifact_manifest",
            "input_provenance_sha256": _prefixed_sha256(_canonical_json_bytes(input_provenance)),
            "audit_report_sha256": _prefixed_sha256(report_bytes),
            "remediation_backlog_sha256": _prefixed_sha256(backlog_bytes),
            "markdown_report_sha256": _prefixed_sha256(markdown_bytes),
        }
    )
    return TerraInterimArtifacts(
        audit_report=report,
        backlog_records=records,
        markdown_report=markdown_report,
        artifact_manifest=artifact_manifest,
    )


def write_terra_interim_artifacts(output_dir: Path, artifacts: TerraInterimArtifacts) -> dict[str, Path]:
    """Write canonical, new artifacts without replacing an existing output directory."""

    if output_dir.exists():
        raise ValueError("Terra interim audit output directory already exists")
    output_dir.mkdir(parents=True)
    paths = {
        "audit_report": output_dir / "terra-interim-audit.json",
        "backlog": output_dir / "terra-remediation-candidate-backlog.jsonl",
        "markdown_report": output_dir / "TERRA_INTERIM_AUDIT.md",
        "artifact_manifest": output_dir / "terra-interim-artifacts.manifest.json",
    }
    paths["audit_report"].write_bytes(_canonical_json_bytes(artifacts.audit_report))
    paths["backlog"].write_bytes(
        _canonical_jsonl_bytes(artifacts.backlog_records) if artifacts.backlog_records else b""
    )
    paths["markdown_report"].write_bytes(artifacts.markdown_report.encode())
    paths["artifact_manifest"].write_bytes(_canonical_json_bytes(artifacts.artifact_manifest))
    return paths
