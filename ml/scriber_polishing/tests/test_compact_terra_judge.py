from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.compact_terra_judge import (
    aggregate_compact_terra_verdicts,
    build_compact_terra_bundle,
    compact_terra_prompt_sha256,
    public_cases_jsonl_bytes,
    validate_compact_terra_private_mapping,
    validate_compact_terra_public_case,
    validate_compact_terra_verdicts,
)


def _source_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for suite in ("ai_gold", "critical_regression", "regular_test"):
        for index in range(100):
            cases.append(
                {
                    "schema_version": 1,
                    "case_id": f"eval_{suite}_{index:03d}",
                    "suite": suite,
                    "source_text": f"Rohtext {suite} {index}",
                    "reference_text": f"Referenz {suite} {index}",
                    "v1_output": f"Ausgabe alt {suite} {index}",
                    "v2_output": f"Ausgabe neu {suite} {index}",
                }
            )
    return cases


def _source_payload(cases) -> bytes:
    ordered = sorted(cases, key=lambda case: (case["suite"], case["case_id"]))
    return (
        "".join(
            json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for case in ordered
        )
    ).encode("utf-8")


def _source_binding(cases) -> dict[str, object]:
    payload = _source_payload(cases)
    return {
        "path": "terra-source-cases.jsonl",
        "bytes": len(payload),
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "record_count": 300,
        "suite_counts": {"ai_gold": 100, "critical_regression": 100, "regular_test": 100},
        "schema_path": "contracts/v2_compact_terra_source_case_schema.json",
        "contains_candidate_identity": True,
        "judge_public": False,
    }


def _bundle(cases=None):
    records = _source_cases() if cases is None else cases
    return build_compact_terra_bundle(
        records,
        evaluation_result_sha256="sha256:" + "1" * 64,
        evaluation_source=_source_binding(records),
    )


def test_builder_selects_exact_quota_and_blinds_candidates_deterministically() -> None:
    first = _bundle()
    second = _bundle(list(reversed(_source_cases())))

    assert first == second
    assert len(first.public_cases) == 100
    assert first.private_mapping["quotas"] == {
        "ai_gold": 34,
        "critical_regression": 33,
        "regular_test": 33,
    }
    observed = {suite: 0 for suite in first.private_mapping["quotas"]}
    for mapping in first.private_mapping["mappings"]:
        observed[mapping["suite"]] += 1
    assert observed == first.private_mapping["quotas"]
    assert {mapping["candidate_a"] for mapping in first.private_mapping["mappings"]} == {"v1", "v2"}
    assert sum(mapping["candidate_a"] == "v2" for mapping in first.private_mapping["mappings"]) == 50
    assert all("v1" not in case and "v2" not in case for case in first.public_cases)


def test_bundle_is_schema_valid_and_hash_binds_public_cases_and_prompt() -> None:
    bundle = _bundle()

    assert all(validate_compact_terra_public_case(case) == case for case in bundle.public_cases)
    assert validate_compact_terra_private_mapping(bundle.private_mapping) == bundle.private_mapping
    assert bundle.private_mapping["prompt_version"] == "v1"
    assert bundle.private_mapping["prompt_sha256"] == compact_terra_prompt_sha256()
    assert bundle.private_mapping["public_cases_sha256"] == (
        "sha256:" + hashlib.sha256(public_cases_jsonl_bytes(bundle.public_cases)).hexdigest()
    )

    invalid = dict(bundle.public_cases[0])
    invalid["candidate_identity"] = "v2"
    with pytest.raises(ValueError, match="public case schema"):
        validate_compact_terra_public_case(invalid)

    wrong_prompt_mapping = dict(bundle.private_mapping)
    wrong_prompt_mapping["prompt_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="repository-owned Terra prompt"):
        validate_compact_terra_private_mapping(wrong_prompt_mapping)


def test_builder_rejects_a_source_that_differs_from_the_evaluation_result_binding() -> None:
    cases = _source_cases()
    binding = _source_binding(cases)
    binding["sha256"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="evaluation source binding"):
        build_compact_terra_bundle(
            cases,
            evaluation_result_sha256="sha256:" + "1" * 64,
            evaluation_source=binding,
        )


def _tie_verdicts(bundle) -> list[dict[str, object]]:
    return [
        {
            "schema_version": 1,
            "case_id": case["case_id"],
            "model_family": "gpt-5.6-terra",
            "judge_session_id": "session_compact_terra_001",
            "prompt_sha256": bundle.private_mapping["prompt_sha256"],
            "winner": "tie",
            "critical_candidates": [],
            "brief_reason": "Beide Ausgaben sind gleichwertig.",
        }
        for case in bundle.public_cases
    ]


def test_verdict_validator_accepts_exactly_one_hundred_bound_terra_verdicts() -> None:
    bundle = _bundle()

    verdicts = validate_compact_terra_verdicts(
        _tie_verdicts(bundle),
        public_cases=bundle.public_cases,
        private_mapping=bundle.private_mapping,
    )

    assert len(verdicts) == 100
    assert {verdict["model_family"] for verdict in verdicts} == {"gpt-5.6-terra"}
    assert {verdict["judge_session_id"] for verdict in verdicts} == {"session_compact_terra_001"}


def test_verdict_validator_rejects_incomplete_or_unbound_judge_evidence() -> None:
    bundle = _bundle()
    verdicts = _tie_verdicts(bundle)

    with pytest.raises(ValueError, match="exactly 100"):
        validate_compact_terra_verdicts(
            verdicts[:-1],
            public_cases=bundle.public_cases,
            private_mapping=bundle.private_mapping,
        )

    wrong_model = [dict(verdict) for verdict in verdicts]
    wrong_model[0]["model_family"] = "gpt-5.6-unbound"
    with pytest.raises(ValueError, match="Terra verdict schema"):
        validate_compact_terra_verdicts(
            wrong_model,
            public_cases=bundle.public_cases,
            private_mapping=bundle.private_mapping,
        )

    mixed_session = [dict(verdict) for verdict in verdicts]
    mixed_session[0]["judge_session_id"] = "session_compact_terra_002"
    with pytest.raises(ValueError, match="exactly one judge session"):
        validate_compact_terra_verdicts(
            mixed_session,
            public_cases=bundle.public_cases,
            private_mapping=bundle.private_mapping,
        )

    wrong_prompt = [dict(verdict) for verdict in verdicts]
    wrong_prompt[0]["prompt_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="prompt hash"):
        validate_compact_terra_verdicts(
            wrong_prompt,
            public_cases=bundle.public_cases,
            private_mapping=bundle.private_mapping,
        )


def _decisive_verdicts(bundle) -> list[dict[str, object]]:
    by_orientation = {
        "v2_as_a": [item for item in bundle.private_mapping["mappings"] if item["candidate_a"] == "v2"],
        "v2_as_b": [item for item in bundle.private_mapping["mappings"] if item["candidate_b"] == "v2"],
    }
    decisions: dict[str, str] = {}
    for mapping in by_orientation["v2_as_a"][:30]:
        decisions[mapping["case_id"]] = "A"
    for mapping in by_orientation["v2_as_a"][30:]:
        decisions[mapping["case_id"]] = "B"
    for mapping in by_orientation["v2_as_b"][:10]:
        decisions[mapping["case_id"]] = "B"
    for mapping in by_orientation["v2_as_b"][10:25]:
        decisions[mapping["case_id"]] = "A"
    for mapping in by_orientation["v2_as_b"][25:]:
        decisions[mapping["case_id"]] = "tie"

    v2_critical = {item["case_id"] for item in by_orientation["v2_as_a"][:8]}
    v1_critical = {item["case_id"] for item in by_orientation["v2_as_b"][:10]}
    mapping_by_id = {item["case_id"]: item for item in bundle.private_mapping["mappings"]}
    verdicts = _tie_verdicts(bundle)
    for verdict in verdicts:
        case_id = verdict["case_id"]
        mapping = mapping_by_id[case_id]
        verdict["winner"] = decisions[case_id]
        critical_candidates: list[str] = []
        if case_id in v2_critical:
            critical_candidates.append("A" if mapping["candidate_a"] == "v2" else "B")
        if case_id in v1_critical:
            critical_candidates.append("A" if mapping["candidate_a"] == "v1" else "B")
        verdict["critical_candidates"] = critical_candidates
    return verdicts


def test_aggregation_reports_compact_gate_and_orientation_bias_without_release_claim() -> None:
    bundle = _bundle()

    report = aggregate_compact_terra_verdicts(
        _decisive_verdicts(bundle),
        public_cases=bundle.public_cases,
        private_mapping=bundle.private_mapping,
    )

    assert report["results"] == {
        "v2_wins": 40,
        "v1_wins": 35,
        "ties": 25,
        "v2_critical": 8,
        "v1_critical": 10,
    }
    assert report["orientation_bias"] == {
        "v2_as_a": 50,
        "v2_as_b": 50,
        "a_wins": 45,
        "b_wins": 30,
        "ties": 25,
        "a_minus_b_wins": 15,
        "absolute_a_b_win_gap": 15,
    }
    assert report["gate"] == {
        "kind": "compact_promotion",
        "decision": "passed",
        "v2_wins_gte_v1_wins": True,
        "v2_critical_lte_v1_critical": True,
    }
    assert report["evidence_class"] == "compact_smoke_test"
    assert report["publication_grade"] is False
    assert report["release_grade_claimed"] is False


def test_build_cli_writes_separate_public_private_and_versioned_prompt_artifacts(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    source = tmp_path / "terra-source-cases.jsonl"
    source.write_bytes(_source_payload(_source_cases()))
    result = tmp_path / "v2-final-evaluation-result.json"
    result_value = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_final_evaluation_result",
        "status": "complete",
        "evaluation_scope": {
            "evidence_class": "compact_smoke_test",
            "publication_grade": False,
            "total_records": 300,
        },
        "terra_judge_source": _source_binding(_source_cases()),
    }
    result.write_bytes(
        (json.dumps(result_value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )
    public = tmp_path / "terra-public.jsonl"
    private = tmp_path / "terra-private.json"
    prompt = tmp_path / "terra-prompt-v1.md"

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts/build_v2_compact_terra_judge.py"),
            "--evaluation-cases",
            str(source),
            "--evaluation-result",
            str(result),
            "--public-cases",
            str(public),
            "--private-mapping",
            str(private),
            "--prompt-output",
            str(prompt),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["selected_case_count"] == 100
    assert summary["judge_model_family"] == "gpt-5.6-terra"
    assert summary["publication_grade"] is False
    assert len(public.read_text(encoding="utf-8").splitlines()) == 100
    mapping = json.loads(private.read_text(encoding="utf-8"))
    assert mapping["public_cases_sha256"] == summary["public_cases_sha256"]
    assert mapping["evaluation_result_sha256"] == "sha256:" + hashlib.sha256(result.read_bytes()).hexdigest()
    assert mapping["evaluation_source"] == result_value["terra_judge_source"]
    first_public = json.loads(public.read_text(encoding="utf-8").splitlines()[0])
    assert "v1_output" not in first_public and "v2_output" not in first_public
    assert prompt.read_bytes() == (project_root / "contracts/v2_compact_terra_judge_prompt_v1.md").read_bytes()


def test_aggregate_cli_canonicalizes_exact_terra_verdicts_and_writes_compact_report(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    bundle = _bundle()
    public = tmp_path / "terra-public.jsonl"
    public.write_bytes(public_cases_jsonl_bytes(bundle.public_cases))
    private = tmp_path / "terra-private.json"
    private.write_bytes(
        (
            json.dumps(bundle.private_mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    )
    raw_verdicts = tmp_path / "terra-raw-verdicts.jsonl"
    raw_verdicts.write_text(
        "".join(
            json.dumps(verdict, ensure_ascii=False, sort_keys=True) + "\n"
            for verdict in reversed(_decisive_verdicts(bundle))
        ),
        encoding="utf-8",
    )
    canonical_verdicts = tmp_path / "terra-verdicts.jsonl"
    report = tmp_path / "terra-aggregate.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts/aggregate_v2_compact_terra_judge.py"),
            "--public-cases",
            str(public),
            "--private-mapping",
            str(private),
            "--verdicts",
            str(raw_verdicts),
            "--canonical-verdicts-output",
            str(canonical_verdicts),
            "--output",
            str(report),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    aggregate = json.loads(report.read_text(encoding="utf-8"))
    assert summary["decision"] == "passed"
    assert aggregate["results"]["v2_wins"] == 40
    assert aggregate["publication_grade"] is False
    assert aggregate["artifacts"]["verdicts_sha256"] == (
        "sha256:" + hashlib.sha256(canonical_verdicts.read_bytes()).hexdigest()
    )
    canonical_ids = [
        json.loads(line)["case_id"] for line in canonical_verdicts.read_text(encoding="utf-8").splitlines()
    ]
    assert canonical_ids == sorted(canonical_ids)
