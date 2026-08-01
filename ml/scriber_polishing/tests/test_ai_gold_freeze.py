from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.dataset_builder import build_dataset_pairs
from scriber_polishing.document_ast import Block, BlockType, Document
from scriber_polishing.gold_builder import (
    DEFAULT_AI_GOLD_SPLIT_TARGETS,
    GoldFreezeResult,
    freeze_ai_verified_gold,
    validate_ai_gold_candidate,
)
from scriber_polishing.schemas import validate_ai_gold_manifest
from scriber_polishing.split_dataset import Split
from scriber_polishing.sst_renderer import render_sst
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text


def _approved_canonical(
    index: int,
    *,
    parent: int | str | None = None,
    reference: str | None = None,
) -> dict[str, object]:
    token = reference or hashlib.sha256(f"gold-record-{index}".encode()).hexdigest()
    parent_id = parent if isinstance(parent, str) else f"canonical_parent_{parent if parent is not None else index:05d}"
    document = Document(
        (
            Block(
                BlockType.PARAGRAPH,
                f"Die Referenz {token} bleibt unverändert. Der Termin ist der 12.03.2026.",
            ),
        )
    )
    semantic_plan = {
        "facts": [
            {
                "fact_id": "f1",
                "order": 1,
                "text": f"Die Referenz {token} bleibt unverändert.",
                "polarity": "positive",
                "modality": "fact",
                "condition": None,
                "protected_values": [token],
            },
            {
                "fact_id": "f2",
                "order": 2,
                "text": "Der Termin ist der 12.03.2026.",
                "polarity": "positive",
                "modality": "fact",
                "condition": None,
                "protected_values": ["12.03.2026"],
            },
        ],
        "entities": [],
        "structure": {
            "has_subject": False,
            "has_salutation": False,
            "paragraph_topics": ["Referenz"],
            "heading_levels": [],
            "list_kind": "none",
            "list_items": [],
            "has_closing": False,
            "signature_lines": [],
        },
    }
    critic_decisions = [
        {
            "critic_id": "critic_fact_terra_01",
            "critic_role": "fact",
            "acceptable": True,
        },
        {
            "critic_id": "critic_style_terra_01",
            "critic_role": "style",
            "acceptable": True,
        },
        {
            "critic_id": "critic_adversarial_sol_01",
            "critic_role": "adversarial",
            "acceptable": True,
        },
        {
            "critic_id": "critic_adversarial_sol_02",
            "critic_role": "adversarial",
            "acceptable": True,
        },
    ]
    return {
        "schema_version": 1,
        "canonical_document_id": f"canonical_gold_{index:05d}",
        "parent_canonical_document_id": parent_id,
        "seed_id": f"seed_gold_{index:05d}",
        "source_plan_batch": "batch_gold_example",
        "plan_generator_agent": "generator_gold_example",
        "target_writer_agent": "writer_gold_example",
        "target_prompt_hash": "sha256:" + "a" * 64,
        "semantic_plan_sha256": "sha256:"
        + hashlib.sha256(
            json.dumps(
                semantic_plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "semantic_plan": semantic_plan,
        "domain": "general_business",
        "document_type": "Geschäftsnotiz",
        "risk_tags": ["fictional_entities"],
        "reviewed_package_sha256": "b" * 64,
        "critic_agents": [item["critic_id"] for item in critic_decisions],
        "critic_decisions": critic_decisions,
        "canonical_generator_id": "local_canonical_expander",
        "canonical_generator_version": "1",
        "variant_index": 1,
        "source_text": render_plain_text(document),
        "target_sst": render_sst(document),
        "target_plain_text": render_plain_text(document),
        "target_markdown": render_markdown(document),
        "target_html": render_html(document),
        "target_ast": document_to_dict(document),
        "language": "de-DE",
        "style_profile": "duden_preferred_business",
        "address_mode": "formal_sie",
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "normalize_valid_variants": False,
        "protected_spans": [token, "12.03.2026"],
        "applied_normalizations": [],
        "rejected_normalizations": [],
        "ambiguity_flags": [],
    }


def test_default_ai_verified_gold_split_targets_are_exact() -> None:
    assert DEFAULT_AI_GOLD_SPLIT_TARGETS == {
        Split.TRAIN: 1_500,
        Split.VALIDATION: 750,
        Split.TEST: 1_000,
        Split.CHALLENGE: 750,
    }
    assert sum(DEFAULT_AI_GOLD_SPLIT_TARGETS.values()) == 4_000


def test_checked_in_ai_gold_config_freezes_the_public_defaults() -> None:
    config_path = Path(__file__).parents[1] / "configs" / "ai_gold.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config == {
        "schema_version": 1,
        "name": "AI-Verified Gold Set",
        "seed": 270_004,
        "selection_algorithm": "parent_grouped_sha256_v1",
        "split_targets": {
            "train": 1_500,
            "validation": 750,
            "test": 1_000,
            "challenge": 750,
        },
        "minimum_total": 4_000,
        "requirements": {
            "semantic_plan_valid": True,
            "document_ast_valid": True,
            "deterministic_renderings": [
                "sst",
                "plain_text",
                "markdown",
                "html",
            ],
            "fact_critic_minimum": 1,
            "style_critic_minimum": 1,
            "adversarial_critic_minimum": 2,
            "critical_flags": 0,
            "deterministic_validator_runs_per_record": 2,
            "fact_roundtrip": "exact",
            "sst_roundtrip": "exact",
            "protected_spans": "exact_or_reversibly_mapped",
            "deduplication": "pass",
            "split_leakage": "pass",
            "ambiguity_flags": 0,
        },
        "holdout_policy": {
            "manifest_only_by_default": True,
            "test_and_challenge_never_train": True,
            "sealed_outputs_require_explicit_path": True,
        },
        "provenance": {
            "ai_only": True,
            "human_curation": False,
            "human_gold": False,
            "generative_api": False,
        },
    }


def test_ai_gold_candidate_requires_complete_deterministic_and_independent_proof() -> None:
    candidate = _approved_canonical(1)

    assert validate_ai_gold_candidate(candidate) == candidate


def test_ai_gold_candidate_accepts_schema_valid_ai_only_training_pairs() -> None:
    pairs = build_dataset_pairs(
        [_approved_canonical(3)],
        seed=270_004,
        variants_per_canonical={split: 1 for split in Split},
    )
    pair = next(item for rows in pairs.values() for item in rows)

    assert validate_ai_gold_candidate(pair) == pair


def test_ai_gold_training_pair_requires_critic_package_binding() -> None:
    pairs = build_dataset_pairs(
        [_approved_canonical(4)],
        seed=270_004,
        variants_per_canonical={split: 1 for split in Split},
    )
    pair = copy.deepcopy(next(item for rows in pairs.values() for item in rows))
    pair.pop("reviewed_package_sha256")

    with pytest.raises(ValueError, match="reviewed_package_sha256|critic-reviewed package hash"):
        validate_ai_gold_candidate(pair)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda item: item["critic_decisions"].pop(),
            "two independent adversarial critics",
        ),
        (
            lambda item: item.update({"target_markdown": "abweichend"}),
            "canonical record Markdown target differs",
        ),
        (
            lambda item: item.update({"source_text": str(item["source_text"]).replace("12.03.2026", "13.03.2026")}),
            "protected span",
        ),
        (
            lambda item: item.update({"ambiguity_flags": ["ambiguous_pronoun"]}),
            "ambiguity",
        ),
    ],
)
def test_ai_gold_candidate_fails_closed_when_any_required_proof_is_missing(
    mutation: object,
    reason: str,
) -> None:
    candidate = copy.deepcopy(_approved_canonical(2))
    mutation(candidate)

    with pytest.raises(ValueError, match=reason):
        validate_ai_gold_candidate(candidate)


def _assert_no_individual_record_identity(value: object) -> None:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert "example_id" not in serialized
    assert "canonical_document_id" not in serialized
    assert "parent_canonical_document_id" not in serialized
    assert "source_text" not in serialized
    assert "target_sst" not in serialized


def test_freezes_exact_default_counts_deterministically_without_surfacing_holdout_records(
    tmp_path: Path,
) -> None:
    candidates = [_approved_canonical(index, parent=(index - 1) // 7) for index in range(1, 4_101)]
    first_dir = tmp_path / "routine"
    first_manifest = first_dir / "ai_gold_manifest.json"

    first = freeze_ai_verified_gold(
        candidates,
        seed=270_004,
        manifest_path=first_manifest,
    )
    sealed_run = tmp_path / "sealed-run"
    sealed_run.mkdir()
    reversed_input = sealed_run / "approved.jsonl"
    reversed_input.write_text(
        "".join(json.dumps(candidate, ensure_ascii=False, sort_keys=True) + "\n" for candidate in reversed(candidates)),
        encoding="utf-8",
    )
    sealed_manifest_path = sealed_run / "ai_gold_manifest.json"
    sealed_root = sealed_run / "sealed"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "generate_ai_gold.py"),
            "--input",
            str(reversed_input),
            "--manifest-output",
            str(sealed_manifest_path),
            "--sealed-output-dir",
            str(sealed_root),
            "--seed",
            "270004",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "ai_gold_freeze=pass total=4000" in completed.stdout
    second_manifest = json.loads(sealed_manifest_path.read_text(encoding="utf-8"))

    assert isinstance(first, GoldFreezeResult)
    assert first.manifest == second_manifest | {
        "isolation": second_manifest["isolation"] | {"sealed_outputs_written": False}
    }
    assert first.manifest["split_counts"] == {
        split.value: expected for split, expected in DEFAULT_AI_GOLD_SPLIT_TARGETS.items()
    }
    assert first.manifest["total_count"] == 4_000
    assert first.manifest["isolation"] == {
        "parent_split_leakage_count": 0,
        "routine_manifest_contains_individual_records": False,
        "sealed_outputs_written": False,
        "test_and_challenge_excluded_from_training": True,
    }
    assert validate_ai_gold_manifest(first.manifest) == first.manifest
    unsafe_manifest = copy.deepcopy(first.manifest)
    unsafe_manifest["test_examples"] = [{"source_text": "verboten"}]
    with pytest.raises(ValueError, match="AI-Gold manifest failed schema"):
        validate_ai_gold_manifest(unsafe_manifest)
    inconsistent_manifest = copy.deepcopy(first.manifest)
    inconsistent_manifest["split_counts"]["test"] -= 1
    with pytest.raises(ValueError, match="AI-Gold manifest split counts"):
        validate_ai_gold_manifest(inconsistent_manifest)
    _assert_no_individual_record_identity(first.manifest)
    assert first.sealed_files == {}
    assert sorted(path.name for path in first_dir.iterdir()) == [
        "ai_gold_manifest.json",
        "ai_gold_manifest.json.sha256",
    ]
    assert first.checksum_path is not None
    assert first.checksum_path.read_text(encoding="ascii").strip() == first.manifest_sha256

    sealed_files = {
        Split.TRAIN: sealed_root / "training" / "train" / "ai_gold_train.sealed.jsonl",
        Split.VALIDATION: sealed_root / "evaluation" / "validation" / "ai_gold_validation.sealed.jsonl",
        Split.TEST: sealed_root / "holdout" / "test" / "ai_gold_test.sealed.jsonl",
        Split.CHALLENGE: sealed_root / "holdout" / "challenge" / "ai_gold_challenge.sealed.jsonl",
    }
    assert sealed_files[Split.TEST].parts[-3:] == (
        "holdout",
        "test",
        "ai_gold_test.sealed.jsonl",
    )
    assert sealed_files[Split.CHALLENGE].parts[-3:] == (
        "holdout",
        "challenge",
        "ai_gold_challenge.sealed.jsonl",
    )
    for split, path in sealed_files.items():
        assert len(path.read_text(encoding="utf-8").splitlines()) == DEFAULT_AI_GOLD_SPLIT_TARGETS[split]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == second_manifest["splits"][split.value]["records_sha256"]


def test_freeze_collapses_normalized_duplicate_sources_by_seeded_rank(
    tmp_path: Path,
) -> None:
    seed = 270_004
    candidates = [_approved_canonical(index, parent=(index - 1) // 7) for index in range(1, 4_101)]
    duplicate = copy.deepcopy(candidates[0])
    duplicate["canonical_document_id"] = "canonical_gold_duplicate_source"
    duplicate["parent_canonical_document_id"] = "canonical_parent_duplicate_source"
    duplicate["seed_id"] = "seed_gold_duplicate_source"
    duplicate["source_text"] = (
        str(duplicate["source_text"])
        .replace("Die Referenz", "DIE REFERENZ")
        .replace(" bleibt unverändert. Der Termin", " bleibt unverändert; der Termin")
    )
    candidates.append(duplicate)

    result = freeze_ai_verified_gold(
        candidates,
        seed=seed,
        sealed_output_dir=tmp_path / "sealed",
    )

    assert result.manifest["evidence"]["normalized_source_duplicate_records_collapsed"] == 1
    selected_by_split = {
        split: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for split, path in result.sealed_files.items()
    }
    selected = [row for rows in selected_by_split.values() for row in rows]
    duplicate_ids = {
        str(candidates[0]["canonical_document_id"]),
        str(duplicate["canonical_document_id"]),
    }
    expected = min(
        duplicate_ids,
        key=lambda identity: (
            hashlib.sha256(f"ai-gold-v1:{seed}:normalized-source:{identity}".encode()).digest(),
            identity,
        ),
    )
    assert {row["canonical_document_id"] for row in selected if row["canonical_document_id"] in duplicate_ids} == {
        expected
    }


def test_freeze_collapses_actual_style_near_duplicate_parent_family_before_splitting(
    tmp_path: Path,
) -> None:
    seed = 270_004
    candidates = [_approved_canonical(index, parent=(index - 1) // 7) for index in range(1, 4_095)]
    first_parent = "canonical_actual_duplicate_147090"
    second_parent = "canonical_actual_duplicate_094888"
    third_parent = "canonical_actual_duplicate_118300"
    for variant in range(1, 8):
        shared = f"Projektstatus Hafenlicht Quartal 2026 Freigabe Bericht Variante {variant:02d}"
        candidates.append(
            _approved_canonical(
                10_000 + variant,
                parent=first_parent,
                reference=f"{shared} Alpha Delta Sigma",
            )
        )
        candidates.append(
            _approved_canonical(
                20_000 + variant,
                parent=second_parent,
                reference=(f"{shared} Alpha Delta Sigma Birke Eiche Hafen Licht"),
            )
        )
        candidates.append(
            _approved_canonical(
                30_000 + variant,
                parent=third_parent,
                reference=f"{shared} Birke Eiche Hafen Licht",
            )
        )

    result = freeze_ai_verified_gold(
        candidates,
        seed=seed,
        sealed_output_dir=tmp_path / "sealed",
    )

    assert result.manifest["candidate_count"] == len(candidates)
    assert result.manifest["rejected_count"] == 0
    assert result.manifest["evidence"]["near_duplicate_parent_families_collapsed"] == 1
    selected_by_split = {
        split: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for split, path in result.sealed_files.items()
    }
    selected = [row for rows in selected_by_split.values() for row in rows]
    family_parents = {first_parent, second_parent, third_parent}
    expected_parent = min(
        family_parents,
        key=lambda parent: (
            hashlib.sha256(f"ai-gold-v1:{seed}:family-parent:{parent}".encode()).digest(),
            parent,
        ),
    )
    selected_family_rows = [row for row in selected if row["parent_canonical_document_id"] in family_parents]
    assert len(selected_family_rows) == 7
    assert {row["parent_canonical_document_id"] for row in selected_family_rows} == {expected_parent}
    assert {
        split
        for split, rows in selected_by_split.items()
        if any(row["parent_canonical_document_id"] == expected_parent for row in rows)
    } == {Split.TRAIN}


def test_generate_ai_gold_cli_fails_closed_without_emitting_partial_artifacts(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "approved.jsonl"
    input_path.write_text(
        json.dumps(_approved_canonical(1), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    script = Path(__file__).parents[1] / "scripts" / "generate_ai_gold.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--input",
            str(input_path),
            "--manifest-output",
            str(manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not manifest_path.exists()
    assert "insufficient approved AI-Gold candidates for train" in completed.stderr
    assert "source_text" not in completed.stdout + completed.stderr


def test_frozen_pair_splits_are_never_relabelled_to_fill_another_quota() -> None:
    pairs = build_dataset_pairs(
        [_approved_canonical(9)],
        seed=270_004,
        variants_per_canonical={split: 1 for split in Split},
    )
    pair = copy.deepcopy(next(item for rows in pairs.values() for item in rows))
    pair["split"] = Split.TRAIN.value

    with pytest.raises(
        ValueError,
        match="insufficient approved AI-Gold candidates for validation",
    ):
        freeze_ai_verified_gold(
            [pair],
            seed=270_004,
            split_targets={
                Split.TRAIN: 1,
                Split.VALIDATION: 1,
                Split.TEST: 1,
                Split.CHALLENGE: 3_997,
            },
        )


def test_frozen_pair_parent_leakage_fails_before_any_artifact_is_written(
    tmp_path: Path,
) -> None:
    pairs = build_dataset_pairs(
        [_approved_canonical(10)],
        seed=270_004,
        variants_per_canonical={split: 1 for split in Split},
    )
    train = copy.deepcopy(next(item for rows in pairs.values() for item in rows))
    train["split"] = Split.TRAIN.value
    challenge = copy.deepcopy(train)
    challenge["example_id"] = f"{challenge['example_id']}__challenge"
    challenge["split"] = Split.CHALLENGE.value
    manifest_path = tmp_path / "manifest.json"

    with pytest.raises(ValueError, match="parent split leakage"):
        freeze_ai_verified_gold(
            [train, challenge],
            seed=270_004,
            manifest_path=manifest_path,
        )

    assert not manifest_path.exists()
