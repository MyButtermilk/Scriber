from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.curriculum import (
    ai_gold_train_ids,
    build_curriculum_plan,
    classify_curriculum_phase,
    load_curriculum_plan,
    load_hash_bound_jsonl,
    validate_curriculum_plan,
    write_curriculum_plan,
)


def _record(
    example_id: str,
    profile: str,
    *,
    domain: str = "general_business",
    split: str = "train",
) -> dict[str, object]:
    return {
        "example_id": example_id,
        "split": split,
        "domain": domain,
        "corruption_profile": profile,
        "provenance": {
            "synthetic_only": True,
            "human_curation": False,
            "generative_api": False,
        },
    }


def _source_binding(count: int, *, split: str = "train") -> dict[str, object]:
    return {
        "filename": f"{split}.jsonl",
        "sha256": "a" * 64,
        "record_count": count,
        "split": split,
    }


def _ai_gold_binding(count: int = 1) -> dict[str, object]:
    return {
        "filename": "ai_gold_train.jsonl",
        "sha256": "b" * 64,
        "record_count": count,
        "split": "train",
    }


def _records() -> list[dict[str, object]]:
    return [
        _record("phase1_identity", "identity"),
        _record("phase1_punctuation", "minimal_punctuation"),
        _record("phase2_grammar", "grammar"),
        _record("phase2_repetition", "repetition_self_correction"),
        _record("phase3_formatting", "spoken_formatting_list"),
        _record("phase3_heading", "spoken_formatting_heading"),
        _record("phase4_general", "sentence_boundary"),
        _record("phase4_domain", "domain_specific_cleanup"),
        _record("phase5_gold", "identity"),
        _record("phase5_hard", "identity", domain="hard_negatives"),
    ]


def test_phase_assignment_uses_exclusive_precedence() -> None:
    records = _records()
    ai_gold_ids = frozenset({"phase5_gold"})

    assert [
        classify_curriculum_phase(record, ai_gold_ids=ai_gold_ids)
        for record in records
    ] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]


def test_matched_plans_are_deterministic_full_permutations_with_only_order_changed() -> None:
    records = _records()
    kwargs = {
        "records": records,
        "source_binding": _source_binding(len(records)),
        "ai_gold_ids": frozenset({"phase5_gold"}),
        "ai_gold_binding": _ai_gold_binding(),
        "epochs": 2,
        "seed": 270013,
    }

    shuffled = build_curriculum_plan(mode="shuffled", **kwargs)
    staged = build_curriculum_plan(mode="staged", **kwargs)
    replay = build_curriculum_plan(mode="staged", **kwargs)

    assert replay == staged
    assert shuffled["assignments"] == staged["assignments"]
    assert shuffled["phase_counts"] == staged["phase_counts"]
    assignments = {
        item["example_id"]: item["phase"] for item in staged["assignments"]
    }
    for order in staged["epoch_orders"]:
        assert [assignments[example_id] for example_id in order["example_ids"]] == sorted(
            assignments.values()
        )
    assert {
        frozenset(order["example_ids"]) for order in shuffled["epoch_orders"]
    } == {frozenset(assignments)}
    assert shuffled["epoch_orders"][0]["example_ids"] != staged["epoch_orders"][0][
        "example_ids"
    ]
    assert staged["epoch_orders"][0]["example_ids"] != staged["epoch_orders"][1][
        "example_ids"
    ]


def test_plan_semantics_reject_tampered_order_and_immutable_writer_rejects_change(
    tmp_path: Path,
) -> None:
    records = _records()
    plan = build_curriculum_plan(
        records,
        source_binding=_source_binding(len(records)),
        ai_gold_ids=frozenset({"phase5_gold"}),
        ai_gold_binding=_ai_gold_binding(),
        mode="staged",
        epochs=1,
        seed=270013,
    )
    tampered = copy.deepcopy(plan)
    tampered["epoch_orders"][0]["example_ids"] = list(
        reversed(tampered["epoch_orders"][0]["example_ids"])
    )
    with pytest.raises(ValueError, match="SHA-256|algorithm"):
        validate_curriculum_plan(tampered)

    destination = tmp_path / "staged.plan.json"
    manifest_destination = tmp_path / "staged.plan.manifest.json"
    first_manifest = write_curriculum_plan(
        plan,
        destination=destination,
        manifest_destination=manifest_destination,
    )
    assert (
        first_manifest["plan_sha256"]
        == hashlib.sha256(destination.read_bytes()).hexdigest()
    )
    loaded, loaded_sha256 = load_curriculum_plan(
        destination,
        expected_sha256=first_manifest["plan_sha256"],
    )
    assert loaded == plan
    assert loaded_sha256 == first_manifest["plan_sha256"]
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_curriculum_plan(destination, expected_sha256="0" * 64)
    assert (
        write_curriculum_plan(
            plan,
            destination=destination,
            manifest_destination=manifest_destination,
        )
        == first_manifest
    )

    changed = build_curriculum_plan(
        records,
        source_binding=_source_binding(len(records)),
        ai_gold_ids=frozenset({"phase5_gold"}),
        ai_gold_binding=_ai_gold_binding(),
        mode="shuffled",
        epochs=1,
        seed=270013,
    )
    with pytest.raises(ValueError, match="overwrite"):
        write_curriculum_plan(
            changed,
            destination=destination,
            manifest_destination=manifest_destination,
        )


def test_hash_bound_loader_rejects_wrong_hash_holdout_and_non_ai_provenance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "train.jsonl"
    rows = [_record("phase1_identity", "identity")]
    source.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )
    expected_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    loaded, binding = load_hash_bound_jsonl(
        source,
        expected_sha256=expected_sha256,
        expected_split="train",
        label="test source",
    )
    assert loaded == rows
    assert binding["sha256"] == expected_sha256
    assert ai_gold_train_ids(loaded) == frozenset({"phase1_identity"})

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_hash_bound_jsonl(
            source,
            expected_sha256="0" * 64,
            expected_split="train",
            label="test source",
        )
    with pytest.raises(ValueError, match="only use train or validation"):
        load_hash_bound_jsonl(
            source,
            expected_sha256=expected_sha256,
            expected_split="test",
            label="test source",
        )

    rows[0]["provenance"] = {
        "synthetic_only": True,
        "human_curation": True,
        "generative_api": False,
    }
    source.write_text(
        json.dumps(rows[0], ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="human_curation=false"):
        load_hash_bound_jsonl(
            source,
            expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            expected_split="train",
            label="test source",
        )


def test_curriculum_plan_cli_binds_exact_source_hashes(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    source = tmp_path / "train.jsonl"
    ai_gold_source = tmp_path / "ai_gold_train.jsonl"
    output = tmp_path / "staged.plan.json"
    manifest = tmp_path / "staged.plan.manifest.json"
    source.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in _records()
        ),
        encoding="utf-8",
    )
    ai_gold_source.write_text(
        json.dumps(
            _record("phase5_gold", "identity"),
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    ai_gold_sha256 = hashlib.sha256(ai_gold_source.read_bytes()).hexdigest()

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_curriculum_plan.py"),
            "--train",
            str(source),
            "--expected-train-sha256",
            source_sha256,
            "--ai-gold-train",
            str(ai_gold_source),
            "--expected-ai-gold-sha256",
            ai_gold_sha256,
            "--mode",
            "staged",
            "--epochs",
            "1",
            "--seed",
            "270013",
            "--output",
            str(output),
            "--manifest",
            str(manifest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    loaded, _ = load_curriculum_plan(
        output,
        expected_sha256=manifest_value["plan_sha256"],
    )
    assert loaded["mode"] == "staged"
    assert loaded["source"]["sha256"] == source_sha256
