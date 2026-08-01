import json

import pytest
import torch
from torch.utils.data import BatchSampler

from scriber_polishing.curriculum import (
    build_curriculum_plan,
    hash_identifier_sequence,
)
from scriber_polishing.training_runtime import (
    CompletionCollator,
    ResumableEpochOrderSampler,
    assert_configured_example_counts,
    build_effective_training_order,
    load_pair_records,
    resolve_epoch_order_schedule,
    resolve_training_schedule,
)


class FakeTokenizer:
    pad_token_id = 0


def _curriculum_records() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for prefix, profile, domain in (
        ("p1", "identity", "general_business"),
        ("p2", "grammar", "general_business"),
        ("p3", "spoken_formatting_list", "general_business"),
        ("p4", "domain_cleanup", "general_business"),
        ("p5", "identity", "hard_negatives"),
    ):
        for suffix in ("a", "b"):
            result.append(
                {
                    "example_id": f"{prefix}{suffix}",
                    "split": "train",
                    "domain": domain,
                    "corruption_profile": profile,
                }
            )
    return result


def _curriculum_plan(mode: str) -> dict[str, object]:
    records = _curriculum_records()
    return build_curriculum_plan(
        records,
        source_binding={
            "filename": "train.jsonl",
            "sha256": "a" * 64,
            "record_count": len(records),
            "split": "train",
        },
        ai_gold_ids=frozenset({"p5a", "p5b"}),
        ai_gold_binding={
            "filename": "ai_gold_train.jsonl",
            "sha256": "b" * 64,
            "record_count": 2,
            "split": "train",
        },
        mode=mode,
        epochs=2,
        seed=270011,
    )


def _effective(mode: str):
    plan = _curriculum_plan(mode)
    loaded = [str(record["example_id"]) for record in _curriculum_records()]
    rejected = ("p2b", "p4a")
    used = [example_id for example_id in loaded if example_id not in rejected]
    length_by_id = {
        example_id: 10 + index * 7
        for index, example_id in enumerate(loaded)
    }
    effective = build_effective_training_order(
        plan,
        plan_binding={
            "path": f"C:/plans/{mode}.json",
            "sha256": "sha256:" + ("c" if mode == "shuffled" else "d") * 64,
        },
        loaded_example_ids=loaded,
        used_example_ids=used,
        rejected_example_ids=rejected,
        sequence_lengths=[length_by_id[example_id] for example_id in used],
        window_size=2,
    )
    return plan, loaded, used, rejected, effective


def test_completion_collator_dynamically_pads_inputs_and_masks_label_padding() -> None:
    collator = CompletionCollator(FakeTokenizer(), pad_to_multiple_of=4)

    batch = collator(
        [
            {"input_ids": [1, 2, 3], "attention_mask": [1, 1, 1], "labels": [-100, 2, 3]},
            {"input_ids": [4, 5], "attention_mask": [1, 1], "labels": [-100, 5]},
        ]
    )

    assert batch["input_ids"].shape == torch.Size([2, 4])
    assert batch["input_ids"].tolist() == [[1, 2, 3, 0], [4, 5, 0, 0]]
    assert batch["attention_mask"].tolist() == [[1, 1, 1, 0], [1, 1, 0, 0]]
    assert batch["labels"].tolist() == [[-100, 2, 3, -100], [-100, 5, -100, -100]]


def test_pair_loader_is_bounded_and_rejects_missing_or_blank_fields(tmp_path) -> None:
    path = tmp_path / "pairs.jsonl"
    records = [
        {"example_id": "a", "source_text": "roh a", "target_sst": "[DOC]\n[P]A[/P]\n[/DOC]"},
        {"example_id": "b", "source_text": "roh b", "target_sst": "[DOC]\n[P]B[/P]\n[/DOC]"},
    ]
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")

    assert [item["example_id"] for item in load_pair_records(path, limit=1)] == ["a"]

    path.write_text(json.dumps({"example_id": "bad", "source_text": "", "target_sst": "x"}) + "\n")
    with pytest.raises(ValueError, match="source_text"):
        load_pair_records(path)

    path.write_text(
        json.dumps(
            {
                "example_id": "compact",
                "source_text": "roh",
                "target_sst": "[DOC]\n[P]Ziel.[/P]\n[/DOC]",
                "target_ast": {"large": ["unused"] * 100},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert load_pair_records(path, minimal=True) == [
        {
            "example_id": "compact",
            "source_text": "roh",
            "target_sst": "[DOC]\n[P]Ziel.[/P]\n[/DOC]",
        }
    ]


def test_configured_smoke_counts_reject_silent_oversize_filtering() -> None:
    config = {"train_examples": 200, "validation_examples": 50}

    assert_configured_example_counts(
        config,
        loaded={"train": 200, "validation": 50},
        used={"train": 200, "validation": 50},
    )

    with pytest.raises(ValueError, match="train.*200.*159"):
        assert_configured_example_counts(
            config,
            loaded={"train": 200, "validation": 50},
            used={"train": 159, "validation": 50},
        )


def test_minimum_counts_allow_logged_length_rejections_but_fail_below_floor() -> None:
    config = {
        "minimum_train_examples": 5_000,
        "minimum_validation_examples": 500,
    }

    assert_configured_example_counts(
        config,
        loaded={"train": 5_400, "validation": 540},
        used={"train": 5_123, "validation": 501},
    )

    with pytest.raises(ValueError, match="minimum_train_examples.*5000.*4999"):
        assert_configured_example_counts(
            config,
            loaded={"train": 5_400, "validation": 540},
            used={"train": 4_999, "validation": 501},
        )


def test_training_schedule_executes_declared_epochs_or_explicit_bounded_steps() -> None:
    config = {
        "epochs": 2,
        "micro_batch_size": 1,
        "gradient_accumulation_steps": 16,
    }

    schedule = resolve_training_schedule(config, train_examples_used=5_000)

    assert schedule == {
        "max_steps": 625,
        "declared_epochs": 2.0,
        "effective_examples_per_step": 16,
        "schedule_source": "epochs",
    }
    assert resolve_training_schedule(
        config | {"max_steps": 20},
        train_examples_used=5_000,
    ) == {
        "max_steps": 20,
        "declared_epochs": 2.0,
        "effective_examples_per_step": 16,
        "schedule_source": "max_steps",
    }
    assert resolve_training_schedule(
        {
            "max_steps": 20,
            "micro_batch_size": 1,
            "gradient_accumulation_steps": 16,
        },
        train_examples_used=200,
    ) == {
        "max_steps": 20,
        "declared_epochs": None,
        "effective_examples_per_step": 16,
        "schedule_source": "max_steps",
    }

    with pytest.raises(ValueError, match="epochs"):
        resolve_training_schedule(
            config | {"epochs": 0},
            train_examples_used=5_000,
        )


def test_explicit_epoch_order_schedule_preserves_every_epoch_boundary() -> None:
    config = {
        "epochs": 2,
        "micro_batch_size": 2,
        "gradient_accumulation_steps": 4,
    }

    assert resolve_training_schedule(
        config,
        train_examples_used=10,
    )["max_steps"] == 3
    assert resolve_epoch_order_schedule(
        config,
        train_examples_used=10,
    ) == {
        "max_steps": 4,
        "declared_epochs": 2.0,
        "effective_examples_per_step": 8,
        "schedule_source": "epochs",
        "train_batches_per_epoch": 5,
        "update_steps_per_epoch": 2,
        "epoch_boundaries_preserved": True,
    }

    with pytest.raises(ValueError, match="max_steps"):
        resolve_epoch_order_schedule(
            config | {"max_steps": 3},
            train_examples_used=10,
        )


def test_effective_curriculum_order_filters_exact_rejections_and_preserves_staged_phases() -> None:
    plan, _loaded, used, rejected, effective = _effective("staged")
    assignments = {
        item["example_id"]: item["phase"] for item in plan["assignments"]
    }

    assert effective.evidence["rejected_record_count"] == 2
    assert effective.evidence["rejected_example_ids_sha256"] == (
        hash_identifier_sequence(sorted(rejected))
    )
    assert effective.evidence["used_record_count"] == len(used)
    assert effective.evidence["implicit_trainer_shuffling"] is False
    assert len(effective.epoch_indices) == 2
    for indices in effective.epoch_indices:
        assert set(indices) == set(range(len(used)))
        ordered_ids = [used[index] for index in indices]
        phases = [assignments[example_id] for example_id in ordered_ids]
        assert phases == sorted(phases)
        for prefix in ("p1", "p3", "p5"):
            assert ordered_ids.index(f"{prefix}b") < ordered_ids.index(f"{prefix}a")
    assert effective.evidence["effective_phase_counts"] == {
        "1": 2,
        "2": 1,
        "3": 2,
        "4": 1,
        "5": 2,
    }


def test_shuffled_and_staged_arms_use_identical_multisets_and_bucket_algorithm() -> None:
    _shuffled_plan, _loaded, shuffled_used, _rejected, shuffled = _effective(
        "shuffled"
    )
    _staged_plan, _loaded, staged_used, _rejected, staged = _effective("staged")

    assert shuffled_used == staged_used
    assert shuffled.evidence["bucket_algorithm"] == staged.evidence[
        "bucket_algorithm"
    ]
    assert shuffled.evidence["bucket_window"] == staged.evidence["bucket_window"] == 2
    assert shuffled.evidence["rejected_example_ids_sha256"] == staged.evidence[
        "rejected_example_ids_sha256"
    ]
    for shuffled_order, staged_order in zip(
        shuffled.epoch_indices,
        staged.epoch_indices,
        strict=True,
    ):
        assert sorted(shuffled_order) == sorted(staged_order) == list(
            range(len(shuffled_used))
        )


def test_effective_order_rejects_plan_dataset_and_tokenization_set_mismatch() -> None:
    plan, loaded, used, rejected, _effective_order = _effective("staged")
    common = {
        "plan": plan,
        "plan_binding": {
            "path": "C:/plans/staged.json",
            "sha256": "sha256:" + "d" * 64,
        },
        "used_example_ids": used,
        "rejected_example_ids": rejected,
        "sequence_lengths": [100 + index for index in range(len(used))],
        "window_size": 2,
    }
    with pytest.raises(ValueError, match="loaded training/plan.*missing="):
        build_effective_training_order(
            loaded_example_ids=loaded[:-1],
            **common,
        )
    with pytest.raises(ValueError, match="loaded training/plan.*extra=foreign"):
        build_effective_training_order(
            loaded_example_ids=[*loaded, "foreign"],
            **common,
        )
    with pytest.raises(ValueError, match="used/rejected|tokenization rejection"):
        build_effective_training_order(
            loaded_example_ids=loaded,
            **{**common, "rejected_example_ids": rejected[:-1]},
        )


def test_epoch_sampler_set_epoch_and_resume_reproduce_exact_suffix() -> None:
    orders = (
        (2, 0, 3, 1, 4, 5),
        (5, 3, 1, 4, 0, 2),
    )
    sampler = ResumableEpochOrderSampler(orders)

    assert len(sampler) == 6
    assert list(sampler) == list(orders[0])
    sampler.set_epoch(1)
    assert sampler.epoch == 1
    assert list(sampler) == list(orders[1])

    full_batches = list(BatchSampler(sampler, batch_size=2, drop_last=False))
    resumed = ResumableEpochOrderSampler(orders)
    resumed.set_epoch(1)
    resumed_suffix = list(
        BatchSampler(resumed, batch_size=2, drop_last=False)
    )[1:]
    assert resumed_suffix == full_batches[1:]
    assert [index for batch in resumed_suffix for index in batch] == list(
        orders[1][2:]
    )

    with pytest.raises(ValueError, match="between 0 and 1"):
        sampler.set_epoch(2)
    with pytest.raises(ValueError, match="exact dataset permutation"):
        ResumableEpochOrderSampler(((0, 1, 1),))
