from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scriber_polishing.training_integrity import TrainingIntegrityError
from scriber_polishing.training_runtime import ResumableEpochOrderSampler


def _load_train_script() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "train_sft.py"
    name = "scriber_test_train_sft_curriculum"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise AssertionError("could not load train_sft.py")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def test_curriculum_trainer_never_falls_back_from_explicit_sampler() -> None:
    module = _load_train_script()
    sampler = ResumableEpochOrderSampler(((2, 0, 1),))
    assert module._trainer_class_for_sampler(None) is module.Trainer
    assert module._trainer_class_for_sampler(sampler) is module.CurriculumOrderTrainer
    trainer = object.__new__(module.CurriculumOrderTrainer)
    trainer._training_order_sampler = sampler
    trainer.train_dataset = [object(), object(), object()]

    assert trainer._get_train_sampler() is sampler
    assert trainer._get_train_sampler(trainer.train_dataset) is sampler

    trainer.train_dataset = [object(), object()]
    with pytest.raises(TrainingIntegrityError, match="length differs"):
        trainer._get_train_sampler()


def test_tokenization_returns_exact_used_and_rejected_example_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_train_script()

    def fake_tokenize(
        _tokenizer: object,
        *,
        transcript: str,
        completion: str,
    ) -> dict[str, list[int]]:
        length = int(transcript)
        return {
            "input_ids": list(range(length)),
            "attention_mask": [1] * length,
            "labels": [-100, *([1] * (length - 1))],
        }

    monkeypatch.setattr(module, "tokenize_example", fake_tokenize)
    records = [
        {"example_id": "case-a", "source_text": "3", "target_sst": "target"},
        {"example_id": "case-b", "source_text": "8", "target_sst": "target"},
        {"example_id": "case-c", "source_text": "4", "target_sst": "target"},
    ]

    encoded, used, rejected = module._tokenize_records(
        object(),
        records,
        max_length=4,
    )

    assert [len(item["input_ids"]) for item in encoded] == [3, 4]
    assert used == ("case-a", "case-c")
    assert rejected == ("case-b",)


def test_warm_start_tokenizer_loads_from_parent_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_train_script()
    parent_path = tmp_path / "parent-checkpoint"
    parent_path.mkdir()
    calls: list[tuple[object, dict[str, object]]] = []
    expected = object()

    def fake_from_pretrained(
        source: object,
        **kwargs: object,
    ) -> object:
        calls.append((source, kwargs))
        return expected

    monkeypatch.setattr(
        module.AutoTokenizer,
        "from_pretrained",
        fake_from_pretrained,
    )

    tokenizer = module._load_training_tokenizer(
        SimpleNamespace(path=parent_path.resolve())
    )

    assert tokenizer is expected
    assert calls == [
        (
            str(parent_path.resolve()),
            {
                "trust_remote_code": False,
                "fix_mistral_regex": False,
                "local_files_only": True,
            },
        )
    ]


def test_improvement_intervals_are_exact_and_never_silently_clamped() -> None:
    module = _load_train_script()

    assert module._resolve_eval_save_steps(
        run_kind="improvement",
        configured_eval_steps=1,
        configured_save_steps=2,
        max_steps=2,
    ) == (1, 2)
    with pytest.raises(TrainingIntegrityError, match="intervals exceed"):
        module._resolve_eval_save_steps(
            run_kind="improvement",
            configured_eval_steps=3,
            configured_save_steps=3,
            max_steps=2,
        )

    assert module._resolve_eval_save_steps(
        run_kind="pilot",
        configured_eval_steps=10,
        configured_save_steps=10,
        max_steps=2,
    ) == (2, 2)
