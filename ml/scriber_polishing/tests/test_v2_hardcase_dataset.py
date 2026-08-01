from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scriber_polishing.training_integrity import (
    TrainingIntegrityError,
    bind_training_dataset,
    load_training_config,
)
from scriber_polishing.v2_hardcase_dataset import (
    DEFAULT_V2_HARDCASE_SPEC_PATH,
    HardCaseDatasetError,
    build_v2_hardcase_review_candidate,
    finalize_v2_hardcase_review,
    load_v2_hardcase_spec,
    plan_v2_hardcase_dataset,
    verify_v2_hardcase_dataset,
    verify_v2_hardcase_review_candidate,
)


def _small_spec(tmp_path: Path) -> Path:
    spec = json.loads(DEFAULT_V2_HARDCASE_SPEC_PATH.read_text(encoding="utf-8"))
    spec["generation"]["parent_document_count"] = 16
    spec["generation"]["canonical_siblings_per_parent"] = 2
    for category in spec["categories"]:
        category["parent_quota"] = 2
    path = tmp_path / "v2-hardcase-small.json"
    path.write_text(
        json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_verdict(
    path: Path,
    review: object,
    *,
    reviewer_id: str,
    role: str,
    acceptable: bool = True,
    critical_flags: list[str] | None = None,
) -> Path:
    model = "gpt-5.6-sol" if role == "adversarial" else "gpt-5.6-terra"
    verdict = {
        "schema_version": 1,
        "kind": "scriber_v2_hardcase_review_verdict",
        "review_candidate_sha256": review.manifest["review_candidate_sha256"],
        "review_contract_sha256": review.manifest["review_contract_sha256"],
        "reviewer_id": reviewer_id,
        "reviewer_role": role,
        "model_family": model,
        "reasoning_effort": "max",
        "independence_attestation": True,
        "review_scope": {"entire_candidate": True, "test_or_challenge_opened": False},
        "acceptable": acceptable,
        "critical_flags": critical_flags or [],
        "noncritical_flags": [],
        "confidence": "high",
        "summary_code": "candidate_approved" if acceptable else "candidate_rejected",
    }
    path.write_text(
        json.dumps(verdict, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _valid_verdicts(tmp_path: Path, review: object) -> list[Path]:
    return [
        _write_verdict(tmp_path / "fact.json", review, reviewer_id="fresh_fact_reviewer_01", role="fact"),
        _write_verdict(tmp_path / "style.json", review, reviewer_id="fresh_style_reviewer_01", role="style"),
        _write_verdict(
            tmp_path / "adversarial-a.json",
            review,
            reviewer_id="fresh_adversarial_reviewer_01",
            role="adversarial",
        ),
        _write_verdict(
            tmp_path / "adversarial-b.json",
            review,
            reviewer_id="fresh_adversarial_reviewer_02",
            role="adversarial",
        ),
    ]


def test_review_build_is_deterministic_and_cannot_be_used_for_training(tmp_path: Path) -> None:
    spec_path = _small_spec(tmp_path)
    first = build_v2_hardcase_review_candidate(spec_path, tmp_path / "first")
    second = build_v2_hardcase_review_candidate(spec_path, tmp_path / "second")

    assert first.manifest == second.manifest
    for name in (
        "plan.json",
        "review-canonicals.jsonl",
        "review-train-pairs.jsonl",
        "review-validation-pairs.jsonl",
        "review-package.json",
    ):
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()
    assert first.manifest["training_ready"] is False
    assert not (first.output_dir / "manifest.json").exists()
    assert not (first.output_dir / "train.jsonl").exists()
    assert not (first.output_dir / "validation.jsonl").exists()
    assert not (first.output_dir / "test.jsonl").exists()
    assert not (first.output_dir / "challenge.jsonl").exists()
    with pytest.raises(TrainingIntegrityError, match="training_ready: false"):
        bind_training_dataset(
            first.manifest_path,
            train=first.train_pairs_path,
            validation=first.validation_pairs_path,
        )


def test_review_candidate_has_no_critic_claims_and_no_parent_leakage(tmp_path: Path) -> None:
    result = build_v2_hardcase_review_candidate(_small_spec(tmp_path), tmp_path / "review")
    canonicals = _load_jsonl(result.canonicals_path)
    train = _load_jsonl(result.train_pairs_path)
    validation = _load_jsonl(result.validation_pairs_path)
    review_fields = {"reviewed_package_sha256", "critic_agents", "critic_decisions"}

    assert all(not review_fields.intersection(record) for record in canonicals + train + validation)
    assert {row["parent_canonical_document_id"] for row in train}.isdisjoint(
        {row["parent_canonical_document_id"] for row in validation}
    )
    assert {row["split"] for row in train} == {"train"}
    assert {row["split"] for row in validation} == {"validation"}
    assert result.manifest["quality_gates"]["parent_split_leakage_count"] == 0


def test_spec_rejects_direct_or_blind_evaluation_sources(tmp_path: Path) -> None:
    path = _small_spec(tmp_path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["policy"]["direct_test_or_challenge_reuse"] = True
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(HardCaseDatasetError, match="direct test or challenge reuse"):
        load_v2_hardcase_spec(path)

    spec = json.loads(DEFAULT_V2_HARDCASE_SPEC_PATH.read_text(encoding="utf-8"))
    spec["sources"][0]["path"] = "artifacts/evaluation-input/challenge.references.jsonl"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(HardCaseDatasetError, match="aggregate-only"):
        load_v2_hardcase_spec(path)


def test_review_verifier_fails_after_content_tampering(tmp_path: Path) -> None:
    result = build_v2_hardcase_review_candidate(_small_spec(tmp_path), tmp_path / "review")
    result.train_pairs_path.write_bytes(result.train_pairs_path.read_bytes() + b"\n")
    with pytest.raises(HardCaseDatasetError, match="review train SHA-256"):
        verify_v2_hardcase_review_candidate(result.output_dir)


def test_finalize_requires_exactly_four_independent_bound_approvals(tmp_path: Path) -> None:
    review = build_v2_hardcase_review_candidate(_small_spec(tmp_path), tmp_path / "review")
    verdicts = _valid_verdicts(tmp_path, review)

    with pytest.raises(HardCaseDatasetError, match="exactly four"):
        finalize_v2_hardcase_review(review.output_dir, verdicts[:3])

    duplicate = json.loads(verdicts[-1].read_text(encoding="utf-8"))
    duplicate["reviewer_id"] = "fresh_adversarial_reviewer_01"
    verdicts[-1].write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(HardCaseDatasetError, match="independent reviewer IDs"):
        finalize_v2_hardcase_review(review.output_dir, verdicts)


def test_finalize_rejects_mismatch_rejection_or_critical_flag(tmp_path: Path) -> None:
    review = build_v2_hardcase_review_candidate(_small_spec(tmp_path), tmp_path / "review")
    verdicts = _valid_verdicts(tmp_path, review)
    rejected = json.loads(verdicts[0].read_text(encoding="utf-8"))
    rejected["acceptable"] = False
    rejected["critical_flags"] = ["fact_loss"]
    rejected["summary_code"] = "candidate_rejected"
    verdicts[0].write_text(json.dumps(rejected), encoding="utf-8")
    with pytest.raises(HardCaseDatasetError, match="unanimous acceptable"):
        finalize_v2_hardcase_review(review.output_dir, verdicts)

    verdicts = _valid_verdicts(tmp_path, review)
    mismatch = json.loads(verdicts[1].read_text(encoding="utf-8"))
    mismatch["review_candidate_sha256"] = "sha256:" + "0" * 64
    verdicts[1].write_text(json.dumps(mismatch), encoding="utf-8")
    with pytest.raises(HardCaseDatasetError, match="candidate SHA-256"):
        finalize_v2_hardcase_review(review.output_dir, verdicts)


def test_four_valid_reviewers_materialize_hash_bound_training_data(tmp_path: Path) -> None:
    review = build_v2_hardcase_review_candidate(_small_spec(tmp_path), tmp_path / "review")
    result = finalize_v2_hardcase_review(review.output_dir, _valid_verdicts(tmp_path, review))
    verified = verify_v2_hardcase_dataset(result.output_dir)

    assert verified.manifest["training_ready"] is True
    assert verified.manifest["review_candidate_sha256"] == review.manifest["review_candidate_sha256"]
    assert verified.manifest["review_evidence"]["role_counts"] == {
        "adversarial": 2,
        "fact": 1,
        "style": 1,
    }
    train = _load_jsonl(verified.train_path)
    reviewer_ids = {
        "fresh_fact_reviewer_01",
        "fresh_style_reviewer_01",
        "fresh_adversarial_reviewer_01",
        "fresh_adversarial_reviewer_02",
    }
    assert all(set(row["critic_agents"]) == reviewer_ids for row in train)
    assert all(row["reviewed_package_sha256"] == review.manifest["review_candidate_sha256"][7:] for row in train)
    bound = bind_training_dataset(
        verified.manifest_path,
        train=verified.train_path,
        validation=verified.validation_path,
    )
    assert bound.train.total_records == verified.manifest["split_counts"]["train"]


def test_category_quota_and_training_config_counts_match_plan(tmp_path: Path) -> None:
    path = _small_spec(tmp_path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    broken = copy.deepcopy(spec)
    broken["categories"][0]["parent_quota"] += 1
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(HardCaseDatasetError, match="category parent quotas"):
        load_v2_hardcase_spec(path)

    plan = plan_v2_hardcase_dataset(DEFAULT_V2_HARDCASE_SPEC_PATH)
    config = load_training_config(DEFAULT_V2_HARDCASE_SPEC_PATH.with_name("train_v2_hardcase.yaml"))
    assert config.values["train_examples"] == plan["split_counts"]["train"] == 10240
    assert config.values["validation_examples"] == plan["split_counts"]["validation"] == 640
    assert config.values["parent_checkpoint_tree_sha256"] == (
        "4cf99b2768c5af9c992ee8f6390822a548d6b546ad354368a8d6cfb8ae288ca4"
    )
