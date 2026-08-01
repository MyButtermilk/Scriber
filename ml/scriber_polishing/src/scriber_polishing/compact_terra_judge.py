"""Deterministic 100-case Terra-only blind comparison for V2 versus V1."""

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

EVIDENCE_CLASS = "compact_smoke_test"
PUBLICATION_GRADE = False
JUDGE_MODEL_FAMILY = "gpt-5.6-terra"
SOURCE_CASE_COUNT = 300
SELECTION_QUOTAS = {
    "ai_gold": 34,
    "critical_regression": 33,
    "regular_test": 33,
}
SELECTION_SEED = "scriber-v2-compact-terra-selection-v1"
BLINDING_SEED = "scriber-v2-compact-terra-blinding-v1"
PROMPT_VERSION = "v1"

_PROJECT_ROOT = Path(__file__).parents[2]
_SOURCE_SCHEMA = _PROJECT_ROOT / "contracts/v2_compact_terra_source_case_schema.json"
_PUBLIC_CASE_SCHEMA = _PROJECT_ROOT / "contracts/v2_compact_terra_public_case_schema.json"
_PRIVATE_MAPPING_SCHEMA = _PROJECT_ROOT / "contracts/v2_compact_terra_private_mapping_schema.json"
_VERDICT_SCHEMA = _PROJECT_ROOT / "contracts/v2_compact_terra_verdict_schema.json"
_AGGREGATE_SCHEMA = _PROJECT_ROOT / "contracts/v2_compact_terra_aggregate_schema.json"
_PROMPT_PATH = _PROJECT_ROOT / "contracts/v2_compact_terra_judge_prompt_v1.md"

@dataclass(frozen=True, slots=True)
class CompactTerraBundle:
    """Public anonymous cases plus their separately held private mapping."""

    public_cases: tuple[dict[str, Any], ...]
    private_mapping: dict[str, Any]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@cache
def _validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _schema_record(record: Mapping[str, Any], path: Path, label: str) -> dict[str, Any]:
    value = dict(record)
    errors = sorted(_validator(path).iter_errors(value), key=lambda error: list(error.absolute_path))
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise ValueError(f"{label} schema failed at {location}: {errors[0].message}")
    return value


def compact_terra_prompt_sha256() -> str:
    """Return the exact hash of the short, versioned Terra prompt."""

    return _sha256(_PROMPT_PATH.read_bytes())


def compact_terra_prompt_bytes() -> bytes:
    """Return the exact short Terra prompt payload for packet materialization."""

    return _PROMPT_PATH.read_bytes()


def public_cases_jsonl_bytes(cases: Sequence[Mapping[str, Any]]) -> bytes:
    """Serialize public judge cases canonically for stable artifact binding."""

    return ("".join(_canonical_json(case) + "\n" for case in cases)).encode("utf-8")


def source_cases_jsonl_bytes(cases: Sequence[Mapping[str, Any]]) -> bytes:
    """Serialize the private 300-case evaluator handoff in its required order."""

    ordered = sorted(cases, key=lambda case: (str(case["suite"]), str(case["case_id"])))
    return public_cases_jsonl_bytes(ordered)


def verdicts_jsonl_bytes(verdicts: Sequence[Mapping[str, Any]]) -> bytes:
    """Serialize validated verdicts canonically for stable handoff hashes."""

    return ("".join(_canonical_json(verdict) + "\n" for verdict in verdicts)).encode("utf-8")


def validate_compact_terra_public_case(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one candidate-anonymous public transport record."""

    return _schema_record(record, _PUBLIC_CASE_SCHEMA, "public case")


def validate_compact_terra_private_mapping(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the private candidate recovery map and exact compact quotas."""

    value = _schema_record(record, _PRIVATE_MAPPING_SCHEMA, "private mapping")
    if value["prompt_sha256"] != compact_terra_prompt_sha256():
        raise ValueError("private mapping prompt hash differs from the repository-owned Terra prompt")
    if value["source_cases_sha256"] != value["evaluation_source"]["sha256"]:
        raise ValueError("private mapping source hash differs from the evaluation result binding")
    mappings = value["mappings"]
    case_ids = [mapping["case_id"] for mapping in mappings]
    source_ids = [mapping["source_case_id"] for mapping in mappings]
    if len(set(case_ids)) != len(case_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError("private mapping contains duplicate case identities")
    if any(mapping["candidate_a"] == mapping["candidate_b"] for mapping in mappings):
        raise ValueError("private mapping must map A and B to distinct candidates")
    observed_quotas = Counter(mapping["suite"] for mapping in mappings)
    if observed_quotas != Counter(SELECTION_QUOTAS):
        raise ValueError("private mapping does not satisfy the exact suite quotas")
    if sum(mapping["candidate_a"] == "v2" for mapping in mappings) != 50:
        raise ValueError("private mapping must place V2 in position A exactly 50 times")
    return value


def validate_compact_terra_verdicts(
    verdicts: Sequence[Mapping[str, Any]],
    *,
    public_cases: Sequence[Mapping[str, Any]],
    private_mapping: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Require exact 100-case coverage by one hash-bound Terra judge session."""

    mapping = validate_compact_terra_private_mapping(private_mapping)
    validated_cases = tuple(validate_compact_terra_public_case(case) for case in public_cases)
    if len(validated_cases) != mapping["selected_case_count"]:
        raise ValueError("public compact Terra packet must contain exactly 100 cases")
    public_ids = [case["case_id"] for case in validated_cases]
    if len(set(public_ids)) != len(public_ids):
        raise ValueError("public compact Terra packet contains duplicate case IDs")
    if _sha256(public_cases_jsonl_bytes(validated_cases)) != mapping["public_cases_sha256"]:
        raise ValueError("public compact Terra packet hash differs from private mapping")
    mapped_ids = [item["case_id"] for item in mapping["mappings"]]
    if set(mapped_ids) != set(public_ids):
        raise ValueError("private compact Terra mapping differs from public case coverage")
    if len(verdicts) != mapping["required_verdict_count"]:
        raise ValueError("compact Terra verdicts must contain exactly 100 records")

    validated = tuple(_schema_record(verdict, _VERDICT_SCHEMA, "Terra verdict") for verdict in verdicts)
    verdict_ids = [verdict["case_id"] for verdict in validated]
    if len(set(verdict_ids)) != len(verdict_ids) or set(verdict_ids) != set(public_ids):
        raise ValueError("compact Terra verdict coverage must match the exact public packet")
    sessions = {verdict["judge_session_id"] for verdict in validated}
    if len(sessions) != 1:
        raise ValueError("compact Terra verdicts must use exactly one judge session")
    if {verdict["model_family"] for verdict in validated} != {mapping["judge_model_family"]}:
        raise ValueError("compact Terra verdicts must use only the bound Terra identity")
    if {verdict["prompt_sha256"] for verdict in validated} != {mapping["prompt_sha256"]}:
        raise ValueError("compact Terra verdict prompt hash differs from the bound prompt")
    return tuple(sorted(validated, key=lambda verdict: verdict["case_id"]))


def validate_compact_terra_aggregate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed compact aggregate and its promotion semantics."""

    value = _schema_record(record, _AGGREGATE_SCHEMA, "compact Terra aggregate")
    results = value["results"]
    orientation = value["orientation_bias"]
    gate = value["gate"]
    if results["v2_wins"] + results["v1_wins"] + results["ties"] != 100:
        raise ValueError("compact Terra aggregate result counts must total 100")
    if orientation["a_wins"] + orientation["b_wins"] + orientation["ties"] != 100:
        raise ValueError("compact Terra orientation counts must total 100")
    signed_gap = orientation["a_wins"] - orientation["b_wins"]
    if orientation["a_minus_b_wins"] != signed_gap or orientation["absolute_a_b_win_gap"] != abs(signed_gap):
        raise ValueError("compact Terra orientation gap is inconsistent")
    wins_pass = results["v2_wins"] >= results["v1_wins"]
    critical_pass = results["v2_critical"] <= results["v1_critical"]
    if (
        gate["v2_wins_gte_v1_wins"] != wins_pass
        or gate["v2_critical_lte_v1_critical"] != critical_pass
        or gate["decision"] != ("passed" if wins_pass and critical_pass else "failed")
    ):
        raise ValueError("compact Terra promotion gate is inconsistent")
    expected_session_hash = _sha256(value["judge"]["session_id"].encode("utf-8"))
    if value["judge"]["session_sha256"] != expected_session_hash:
        raise ValueError("compact Terra judge session hash is inconsistent")
    return value


def aggregate_compact_terra_verdicts(
    verdicts: Sequence[Mapping[str, Any]],
    *,
    public_cases: Sequence[Mapping[str, Any]],
    private_mapping: Mapping[str, Any],
) -> dict[str, Any]:
    """Aggregate one exact Terra session into a non-release-grade compact gate."""

    mapping = validate_compact_terra_private_mapping(private_mapping)
    validated_verdicts = validate_compact_terra_verdicts(
        verdicts,
        public_cases=public_cases,
        private_mapping=mapping,
    )
    mapping_by_id = {item["case_id"]: item for item in mapping["mappings"]}
    results = {"v2_wins": 0, "v1_wins": 0, "ties": 0, "v2_critical": 0, "v1_critical": 0}
    orientation = {
        "v2_as_a": sum(item["candidate_a"] == "v2" for item in mapping["mappings"]),
        "v2_as_b": sum(item["candidate_b"] == "v2" for item in mapping["mappings"]),
        "a_wins": 0,
        "b_wins": 0,
        "ties": 0,
        "a_minus_b_wins": 0,
        "absolute_a_b_win_gap": 0,
    }
    for verdict in validated_verdicts:
        case_mapping = mapping_by_id[verdict["case_id"]]
        winner = verdict["winner"]
        if winner == "tie":
            results["ties"] += 1
            orientation["ties"] += 1
        else:
            orientation[f"{winner.lower()}_wins"] += 1
            candidate = case_mapping[f"candidate_{winner.lower()}"]
            results[f"{candidate}_wins"] += 1
        for position in verdict["critical_candidates"]:
            candidate = case_mapping[f"candidate_{position.lower()}"]
            results[f"{candidate}_critical"] += 1

    orientation["a_minus_b_wins"] = orientation["a_wins"] - orientation["b_wins"]
    orientation["absolute_a_b_win_gap"] = abs(orientation["a_minus_b_wins"])
    wins_pass = results["v2_wins"] >= results["v1_wins"]
    critical_pass = results["v2_critical"] <= results["v1_critical"]
    session_id = validated_verdicts[0]["judge_session_id"]
    report = {
        "schema_version": 1,
        "kind": "scriber_v2_compact_terra_aggregate",
        "status": "complete",
        "evidence_class": EVIDENCE_CLASS,
        "publication_grade": PUBLICATION_GRADE,
        "release_grade_claimed": False,
        "judge": {
            "model_family": JUDGE_MODEL_FAMILY,
            "session_id": session_id,
            "session_sha256": _sha256(session_id.encode("utf-8")),
            "prompt_version": mapping["prompt_version"],
            "prompt_sha256": mapping["prompt_sha256"],
        },
        "coverage": {
            "source_case_count": SOURCE_CASE_COUNT,
            "public_case_count": len(public_cases),
            "verdict_count": len(validated_verdicts),
            "quotas": dict(SELECTION_QUOTAS),
        },
        "artifacts": {
            "public_cases_sha256": mapping["public_cases_sha256"],
            "private_mapping_sha256": _sha256((_canonical_json(mapping) + "\n").encode("utf-8")),
            "verdicts_sha256": _sha256(verdicts_jsonl_bytes(validated_verdicts)),
        },
        "results": results,
        "orientation_bias": orientation,
        "gate": {
            "kind": "compact_promotion",
            "decision": "passed" if wins_pass and critical_pass else "failed",
            "v2_wins_gte_v1_wins": wins_pass,
            "v2_critical_lte_v1_critical": critical_pass,
        },
    }
    return validate_compact_terra_aggregate(report)


def _rank(seed: str, suite: str, case_id: str) -> bytes:
    return hashlib.sha256(f"{seed}\0{suite}\0{case_id}".encode()).digest()


def _validate_source_cases(cases: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    if len(cases) != SOURCE_CASE_COUNT:
        raise ValueError(f"compact Terra source must contain exactly {SOURCE_CASE_COUNT} cases")
    validated: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    for index, raw in enumerate(cases):
        value = _schema_record(raw, _SOURCE_SCHEMA, f"source case {index}")
        if value["case_id"] in case_ids:
            raise ValueError(f"duplicate compact Terra source case_id: {value['case_id']}")
        case_ids.add(value["case_id"])
        validated.append(value)
    suite_counts = Counter(case["suite"] for case in validated)
    if suite_counts != Counter(dict.fromkeys(SELECTION_QUOTAS, 100)):
        raise ValueError("compact Terra source must contain exactly 100 cases per suite")
    return tuple(validated)


def _public_case_id(case: Mapping[str, Any]) -> str:
    identity = {
        "seed": BLINDING_SEED,
        "source_case_id": case["case_id"],
        "v1_sha256": hashlib.sha256(case["v1_output"].encode()).hexdigest(),
        "v2_sha256": hashlib.sha256(case["v2_output"].encode()).hexdigest(),
    }
    return "terra_" + hashlib.sha256(_canonical_json(identity).encode()).hexdigest()[:24]


def build_compact_terra_bundle(
    cases: Sequence[Mapping[str, Any]],
    *,
    evaluation_result_sha256: str,
    evaluation_source: Mapping[str, Any],
) -> CompactTerraBundle:
    """Select 34/33/33 cases and create a deterministic, balanced blind A/B packet."""

    validated = _validate_source_cases(cases)
    by_suite = {suite: [] for suite in SELECTION_QUOTAS}
    for case in validated:
        by_suite[case["suite"]].append(case)

    selected: list[dict[str, Any]] = []
    for suite, quota in SELECTION_QUOTAS.items():
        ranked = sorted(
            by_suite[suite],
            key=lambda case: (_rank(SELECTION_SEED, suite, case["case_id"]), case["case_id"]),
        )
        selected.extend(ranked[:quota])

    v2_as_a_ids = {
        case["case_id"]
        for case in sorted(
            selected,
            key=lambda case: (_rank(BLINDING_SEED, case["suite"], case["case_id"]), case["case_id"]),
        )[: len(selected) // 2]
    }
    paired: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for case in selected:
        public_case_id = _public_case_id(case)
        v2_as_a = case["case_id"] in v2_as_a_ids
        public_case = {
            "schema_version": 1,
            "case_id": public_case_id,
            "source_text": case["source_text"],
            "reference_text": case["reference_text"],
            "candidate_a": case["v2_output"] if v2_as_a else case["v1_output"],
            "candidate_b": case["v1_output"] if v2_as_a else case["v2_output"],
        }
        mapping = {
            "case_id": public_case_id,
            "source_case_id": case["case_id"],
            "suite": case["suite"],
            "candidate_a": "v2" if v2_as_a else "v1",
            "candidate_b": "v1" if v2_as_a else "v2",
        }
        paired.append((public_case, mapping))

    paired.sort(key=lambda item: item[0]["case_id"])
    public_cases = tuple(validate_compact_terra_public_case(item[0]) for item in paired)
    mappings = [item[1] for item in paired]
    source_payload = source_cases_jsonl_bytes(validated)
    source_binding = dict(evaluation_source)
    private_mapping = {
        "schema_version": 1,
        "kind": "scriber_v2_compact_terra_private_mapping",
        "evidence_class": EVIDENCE_CLASS,
        "publication_grade": PUBLICATION_GRADE,
        "judge_model_family": JUDGE_MODEL_FAMILY,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": compact_terra_prompt_sha256(),
        "evaluation_result_sha256": evaluation_result_sha256,
        "evaluation_source": source_binding,
        "source_cases_sha256": _sha256(source_payload),
        "public_cases_sha256": _sha256(public_cases_jsonl_bytes(public_cases)),
        "source_case_count": SOURCE_CASE_COUNT,
        "selected_case_count": len(public_cases),
        "required_verdict_count": len(public_cases),
        "quotas": dict(SELECTION_QUOTAS),
        "selection_seed": SELECTION_SEED,
        "blinding_seed": BLINDING_SEED,
        "mappings": mappings,
    }
    if source_binding.get("bytes") != len(source_payload) or source_binding.get("sha256") != _sha256(source_payload):
        raise ValueError("evaluation source binding differs from the exact canonical 300-case handoff")
    return CompactTerraBundle(
        public_cases=public_cases,
        private_mapping=validate_compact_terra_private_mapping(private_mapping),
    )
