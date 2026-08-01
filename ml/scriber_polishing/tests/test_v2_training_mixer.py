from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scriber_polishing.v2_training_mixer as mixer
from scriber_polishing.training_integrity import load_training_config
from scriber_polishing.v2_training_mixer import V2TrainingMixError


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _pair(
    source: str,
    index: int,
    split: str,
    *,
    parent: str | None = None,
    reviewers: list[str] | None = None,
    reviewed_package: str = "a" * 64,
) -> dict[str, object]:
    example_id = f"{source}_example_{split}_{index:04d}"
    canonical = f"{source}_canonical_{split}_{index:04d}"
    parent_id = parent or f"{source}_parent_{split}_{index:04d}"
    source_text = f"Rohtext {source} {split} {index}."
    target_text = f"Zieltext {source} {split} {index}."
    return {
        "schema_version": 1,
        "example_id": example_id,
        "canonical_document_id": canonical,
        "parent_canonical_document_id": parent_id,
        "seed_id": "synthetic_seed_01",
        "split": split,
        "domain": "general_business",
        "document_type": "notiz",
        "language": "de-DE",
        "address_mode": "neutral_third_person",
        "risk_tags": ["synthetic_replay"],
        "source_text": source_text,
        "target_sst": f"[DOC][P]{target_text}[/P][/DOC]",
        "target_plain_text": target_text,
        "target_markdown": target_text,
        "target_html": f"<p>{target_text}</p>",
        "target_ast": {},
        "semantic_plan": {},
        "formatting_decisions": [],
        "formatting_commands": [],
        "paragraph_boundaries": [0],
        "heading_boundaries": [],
        "list_boundaries": [],
        "salutation_boundary": None,
        "closing_boundary": None,
        "signature_boundaries": [],
        "protected_spans": [],
        "source_protected_spans": [],
        "corruption_profile": "punctuation_case",
        "severity": "light",
        "error_origin": "stt",
        "operations": ["punctuation_fix"],
        "value_mappings": [],
        "applied_normalizations": [],
        "ambiguity_flags": [],
        "critic_agents": list(reviewers or []),
        "critic_decisions": [],
        "reviewed_package_sha256": reviewed_package,
        "validation_scores": {"deterministic": 1.0},
        "provenance": {
            "seed_id": "synthetic_seed_01",
            "source_plan_batch": None,
            "plan_generator_agent": None,
            "target_writer_agent": None,
            "canonical_generator_id": "mixer_fixture",
            "canonical_generator_version": "1",
            "variant_index": 1,
            "synthetic_only": True,
            "human_curation": False,
            "generative_api": False,
        },
    }


def _jsonl(path: Path, rows: list[dict[str, object]]) -> tuple[str, int]:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return _sha256(payload), len(rows)


def _fixture_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    leaked_v1_parent: bool = False,
    duplicate_v1_pairs: bool = False,
    duplicate_selected_v1_pair: bool = False,
) -> tuple[Path, Path, mixer._MixContract]:
    v1_root = tmp_path / "v1-source"
    v1_train_rows = [_pair("v1", index, "train") for index in range(6)]
    v1_validation_rows = [_pair("v1", index, "validation") for index in range(4)]
    if leaked_v1_parent:
        v1_validation_rows[0]["parent_canonical_document_id"] = v1_train_rows[0]["parent_canonical_document_id"]
    if duplicate_v1_pairs:
        for row in v1_train_rows:
            row["source_text"] = "Doppelter Rohtext."
            row["target_sst"] = "[DOC][P]Doppeltes Ziel.[/P][/DOC]"
    elif duplicate_selected_v1_pair:
        ranked = sorted(
            v1_train_rows,
            key=lambda row: mixer._selection_rank(270023, "train", str(row["example_id"])),
        )
        for field in (
            "source_text",
            "target_sst",
            "target_plain_text",
            "target_markdown",
            "target_html",
            "target_ast",
        ):
            ranked[1][field] = ranked[0][field]
    v1_train_sha, v1_train_count = _jsonl(v1_root / "splits" / "train.jsonl", v1_train_rows)
    v1_validation_sha, v1_validation_count = _jsonl(v1_root / "splits" / "validation.jsonl", v1_validation_rows)
    v1_manifest = {
        "manifest_version": 1,
        "sealed_splits": ["test", "challenge"],
        "split_group": "parent_canonical_document_id",
        "split_counts": {"train": v1_train_count, "validation": v1_validation_count},
        "split_sha256": {
            "train": v1_train_sha.removeprefix("sha256:"),
            "validation": v1_validation_sha.removeprefix("sha256:"),
        },
        "split_leakage": False,
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
    }
    v1_manifest_payload = json.dumps(v1_manifest, sort_keys=True, separators=(",", ":")).encode()
    (v1_root / "dataset_manifest.json").write_bytes(v1_manifest_payload)

    reviewers = ["critic_fact_01", "critic_style_01", "critic_adversarial_01", "critic_adversarial_02"]
    candidate_sha = "sha256:" + "c" * 64
    v2_root = tmp_path / "v2-source"
    v2_train_rows = [
        _pair("v2", index, "train", reviewers=reviewers, reviewed_package=candidate_sha[7:]) for index in range(3)
    ]
    v2_validation_rows = [
        _pair("v2", index, "validation", reviewers=reviewers, reviewed_package=candidate_sha[7:]) for index in range(2)
    ]
    v2_train_sha, v2_train_count = _jsonl(v2_root / "train.jsonl", v2_train_rows)
    v2_validation_sha, v2_validation_count = _jsonl(v2_root / "validation.jsonl", v2_validation_rows)
    v2_manifest = {
        "schema_version": 2,
        "kind": "scriber_v2_reviewed_hardcase_dataset",
        "training_ready": True,
        "review_package_sha256": "sha256:" + "b" * 64,
        "review_candidate_sha256": candidate_sha,
        "review_contract_sha256": "sha256:" + "d" * 64,
        "review_evidence": {
            "sha256": "sha256:" + "e" * 64,
            "evidence_tree_sha256": "sha256:" + "f" * 64,
            "role_counts": {"adversarial": 2, "fact": 1, "style": 1},
            "reviewer_ids": reviewers,
        },
        "artifact_tree_sha256": "sha256:" + "1" * 64,
        "split_counts": {"train": v2_train_count, "validation": v2_validation_count},
        "split_sha256": {"train": v2_train_sha, "validation": v2_validation_sha},
        "split_leakage": False,
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "contains_test_or_challenge_split": False,
        "direct_test_or_challenge_reuse": False,
    }
    v2_manifest_path = v2_root / "manifest.json"
    v2_manifest_path.write_text(json.dumps(v2_manifest, sort_keys=True) + "\n", encoding="utf-8")

    def verify_stub(_root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            manifest=v2_manifest,
            manifest_path=v2_manifest_path,
            train_path=v2_root / "train.jsonl",
            validation_path=v2_root / "validation.jsonl",
        )

    monkeypatch.setattr(mixer, "verify_v2_hardcase_dataset", verify_stub)
    contract = mixer._MixContract(
        seed=270023,
        v1_manifest_sha256=_sha256(v1_manifest_payload),
        v1_train_sha256=v1_train_sha,
        v1_validation_sha256=v1_validation_sha,
        v1_train_source_count=v1_train_count,
        v1_validation_source_count=v1_validation_count,
        v1_train_replay_count=3,
        v1_validation_replay_count=2,
        v2_train_count=v2_train_count,
        v2_validation_count=v2_validation_count,
    )
    return v1_root, v2_root, contract


def test_small_mix_is_deterministic_and_hash_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1_root, v2_root, contract = _fixture_sources(tmp_path, monkeypatch)
    first = mixer._build_v2_training_mix(v1_root, v2_root, tmp_path / "first", contract=contract)
    second = mixer._build_v2_training_mix(v1_root, v2_root, tmp_path / "second", contract=contract)

    assert first.manifest == second.manifest
    assert first.train_path.read_bytes() == second.train_path.read_bytes()
    assert first.validation_path.read_bytes() == second.validation_path.read_bytes()
    assert first.manifest["training_ready"] is True
    assert first.manifest["split_counts"] == {"train": 6, "validation": 4}
    assert first.manifest["source_counts"] == {
        "train": {"v1_replay": 3, "v2_hardcase": 3},
        "validation": {"v1_replay": 2, "v2_hardcase": 2},
    }
    assert not (first.output_dir / "test.jsonl").exists()
    assert not (first.output_dir / "challenge.jsonl").exists()


def test_check_recomputes_selection_and_rejects_tampering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1_root, v2_root, contract = _fixture_sources(tmp_path, monkeypatch)
    result = mixer._build_v2_training_mix(v1_root, v2_root, tmp_path / "mix", contract=contract)

    checked = mixer._verify_v2_training_mix(
        result.output_dir,
        v1_root=v1_root,
        v2_root=v2_root,
        contract=contract,
    )
    assert checked.manifest == result.manifest

    result.train_path.write_bytes(result.train_path.read_bytes() + b"\n")
    with pytest.raises(V2TrainingMixError, match="written mix split"):
        mixer._verify_v2_training_mix(
            result.output_dir,
            v1_root=v1_root,
            v2_root=v2_root,
            contract=contract,
        )


def test_mix_rejects_unreviewed_v2_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1_root, v2_root, contract = _fixture_sources(tmp_path, monkeypatch)
    original = mixer.verify_v2_hardcase_dataset

    def unreviewed(root: Path) -> SimpleNamespace:
        result = original(root)
        result.manifest["training_ready"] = False
        return result

    monkeypatch.setattr(mixer, "verify_v2_hardcase_dataset", unreviewed)
    with pytest.raises(V2TrainingMixError, match="not a reviewed, training-ready"):
        mixer._build_v2_training_mix(v1_root, v2_root, tmp_path / "mix", contract=contract)


def test_mix_rejects_parent_leakage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1_root, v2_root, contract = _fixture_sources(tmp_path, monkeypatch, leaked_v1_parent=True)
    with pytest.raises(V2TrainingMixError, match="parent split leakage"):
        mixer._build_v2_training_mix(v1_root, v2_root, tmp_path / "mix", contract=contract)


def test_mix_rejects_duplicate_selected_pairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1_root, v2_root, contract = _fixture_sources(tmp_path, monkeypatch, duplicate_v1_pairs=True)
    with pytest.raises(V2TrainingMixError, match="eligible distinct source/target pairs"):
        mixer._build_v2_training_mix(v1_root, v2_root, tmp_path / "mix", contract=contract)


def test_mix_replaces_duplicate_ranked_v1_pair_with_next_unique_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v1_root, v2_root, contract = _fixture_sources(
        tmp_path,
        monkeypatch,
        duplicate_selected_v1_pair=True,
    )
    ranked_ids = sorted(
        (f"v1_example_train_{index:04d}" for index in range(6)),
        key=lambda example_id: mixer._selection_rank(270023, "train", example_id),
    )

    result = mixer._build_v2_training_mix(v1_root, v2_root, tmp_path / "mix", contract=contract)
    mixed_ids = {json.loads(line)["example_id"] for line in result.train_path.read_text(encoding="utf-8").splitlines()}

    assert ranked_ids[0] in mixed_ids
    assert ranked_ids[1] not in mixed_ids
    assert len([example_id for example_id in mixed_ids if str(example_id).startswith("v1_")]) == 3


def test_mix_rejects_prohibited_source_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1_root, _v2_root, contract = _fixture_sources(tmp_path, monkeypatch)
    with pytest.raises(V2TrainingMixError, match="prohibited path component"):
        mixer._build_v2_training_mix(v1_root, tmp_path / "challenge", tmp_path / "mix", contract=contract)


def test_production_contract_has_exact_requested_counts() -> None:
    contract = mixer._PRODUCTION_CONTRACT
    assert contract.v1_train_replay_count == contract.v2_train_count == 10_240
    assert contract.v1_validation_replay_count == contract.v2_validation_count == 640
    assert contract.v1_train_replay_count + contract.v2_train_count == 20_480
    assert contract.v1_validation_replay_count + contract.v2_validation_count == 1_280
    config = load_training_config(mixer.PROJECT_ROOT / "configs" / "train_v2_hardcase_mixed.yaml")
    assert config.values["train_examples"] == 20_480
    assert config.values["validation_examples"] == 1_280
    assert config.values["parent_checkpoint_tree_sha256"] == (
        "4cf99b2768c5af9c992ee8f6390822a548d6b546ad354368a8d6cfb8ae288ca4"
    )
