from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scriber_polishing.curriculum import (
    build_curriculum_plan,
    canonical_json_bytes,
)
from scriber_polishing.protected_spans import (
    PROTECTION_POLICY_FILENAME,
    legacy_policy_evidence,
)
from scriber_polishing.training_integrity import (
    TrainingIntegrityError,
    bind_training_dataset,
    bind_training_order_plan,
    build_run_identity,
    capture_code_identity,
    capture_tokenizer_identity,
    fresh_reload_local_model,
    load_training_config,
    prepare_run_identity,
    publish_final_model,
    record_checkpoint_identity,
    record_invocation_evidence,
    summarize_checkpoint_state,
    verify_resume_checkpoint,
    write_immutable_json,
)

BASE_MODEL = "google/gemma-3-270m-it"
BASE_REVISION = "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3"


def _valid_config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_kind": "pilot",
        "base_model": BASE_MODEL,
        "base_revision": BASE_REVISION,
        "method": "full_sft",
        "seed": 270011,
        "minimum_train_examples": 5,
        "minimum_validation_examples": 2,
        "epochs": 2,
        "max_sequence_length": 768,
        "max_sequence_length_candidates": [512, 768, 1024],
        "micro_batch_size": 2,
        "gradient_accumulation_steps": 8,
        "learning_rate": 0.00002,
        "learning_rate_candidates": [0.00002, 0.00005],
        "bf16": True,
        "gradient_checkpointing": True,
        "optimizer": "adamw_torch_fused",
        "early_stopping": True,
        "early_stopping_patience": 3,
        "eval_steps": 50,
        "save_steps": 50,
        "warmup_ratio": 0.03,
        "weight_decay": 0.01,
        "max_grad_norm": 1.0,
        "assistant_output_loss_only": True,
    }


def _write_yaml(path: Path, value: dict[str, object]) -> None:
    import yaml

    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _jsonl(path: Path, split: str, count: int) -> tuple[str, int]:
    payload = "".join(
        json.dumps(
            {
                "example_id": f"{split}-{index}",
                "source_text": f"roh {index}",
                "target_sst": f"[DOC]\n[P]Ziel {index}.[/P]\n[/DOC]",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for index in range(count)
    ).encode()
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest(), count


def _dataset(tmp_path: Path) -> tuple[Path, Path, Path]:
    train = tmp_path / "train.jsonl"
    validation = tmp_path / "validation.jsonl"
    train_hash, train_count = _jsonl(train, "train", 5)
    validation_hash, validation_count = _jsonl(validation, "validation", 2)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "split_counts": {
                    "train": train_count,
                    "validation": validation_count,
                },
                "split_sha256": {
                    "train": train_hash,
                    "validation": validation_hash,
                },
                "split_leakage": False,
                "synthetic_only": True,
                "human_curation": False,
                "generative_api": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return train, validation, manifest


def _curriculum_plan(
    tmp_path: Path,
    train: Path,
    *,
    mode: str = "staged",
) -> tuple[Path, str]:
    records = [
        {
            "example_id": "train-0",
            "split": "train",
            "domain": "general_business",
            "corruption_profile": "identity",
        },
        {
            "example_id": "train-1",
            "split": "train",
            "domain": "general_business",
            "corruption_profile": "grammar",
        },
        {
            "example_id": "train-2",
            "split": "train",
            "domain": "general_business",
            "corruption_profile": "spoken_formatting_list",
        },
        {
            "example_id": "train-3",
            "split": "train",
            "domain": "general_business",
            "corruption_profile": "domain_cleanup",
        },
        {
            "example_id": "train-4",
            "split": "train",
            "domain": "general_business",
            "corruption_profile": "identity",
        },
    ]
    train_sha256 = hashlib.sha256(train.read_bytes()).hexdigest()
    plan = build_curriculum_plan(
        records,
        source_binding={
            "filename": train.name,
            "sha256": train_sha256,
            "record_count": 5,
            "split": "train",
        },
        ai_gold_ids=frozenset({"train-4"}),
        ai_gold_binding={
            "filename": "ai_gold_train.jsonl",
            "sha256": "b" * 64,
            "record_count": 1,
            "split": "train",
        },
        mode=mode,
        epochs=2,
        seed=270011,
    )
    path = tmp_path / f"{mode}.plan.json"
    payload = canonical_json_bytes(plan)
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def test_training_config_is_closed_typed_and_semantically_consumed(tmp_path: Path) -> None:
    path = tmp_path / "pilot.yaml"
    _write_yaml(path, _valid_config())

    snapshot = load_training_config(path)

    assert snapshot.values["run_kind"] == "pilot"
    assert dict(snapshot.values) == _valid_config()
    assert "training_order_mode" not in snapshot.values
    assert "save_total_limit" not in snapshot.values
    assert snapshot.sha256 == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    invalid_cases = [
        ({"learning_rate": "selected_by_pilot"}, "learning_rate"),
        ({"micro_batch_size": "measured"}, "micro_batch_size"),
        ({"unused_declaration": True}, "unknown"),
        ({"assistant_output_loss_only": False}, "assistant_output_loss_only"),
        ({"method": "lora"}, "full_sft"),
        ({"bf16": False}, "BF16"),
        ({"effective_batch_size": 16}, "unknown"),
    ]
    for patch, match in invalid_cases:
        _write_yaml(path, _valid_config() | patch)
        with pytest.raises(TrainingIntegrityError, match=match):
            load_training_config(path)

    path.write_text(
        "schema_version: 1\nschema_version: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(TrainingIntegrityError, match="duplicate"):
        load_training_config(path)


def test_explicit_training_order_config_is_all_or_none_pilot_only_and_epoch_bound(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pilot.yaml"
    order_fields = {
        "training_order_mode": "staged",
        "training_order_plan_path": "staged.plan.json",
        "training_order_plan_sha256": "a" * 64,
        "save_total_limit": 3,
    }
    order_compatible_base = {
        key: value
        for key, value in _valid_config().items()
        if key not in {"early_stopping", "early_stopping_patience"}
    }
    ordered_config = order_compatible_base | order_fields
    _write_yaml(path, ordered_config)
    loaded = load_training_config(path)
    assert loaded.values["training_order_mode"] == "staged"
    assert loaded.values["save_total_limit"] == 3

    invalid_cases = (
        (
            order_compatible_base | {"training_order_mode": "staged"},
            "all mode/path/SHA",
        ),
        (
            ordered_config | {"run_kind": "final"},
            "pilot-only",
        ),
        (
            ordered_config | {"training_order_mode": "random"},
            "shuffled or staged",
        ),
        (
            ordered_config | {"epochs": 1.5},
            "integer epochs",
        ),
        (
            {
                key: value
                for key, value in ordered_config.items()
                if key != "epochs"
            }
            | {"max_steps": 1},
            "max_steps must be absent",
        ),
        (
            ordered_config | {"save_total_limit": 0},
            "save_total_limit",
        ),
        (
            ordered_config | {"early_stopping": True, "early_stopping_patience": 3},
            "early_stopping: false",
        ),
    )
    for value, match in invalid_cases:
        _write_yaml(path, value)
        with pytest.raises(TrainingIntegrityError, match=match):
            load_training_config(path)


def test_training_order_plan_binds_exact_canonical_bytes_config_and_dataset(
    tmp_path: Path,
) -> None:
    train, validation, manifest = _dataset(tmp_path)
    plan_path, plan_sha256 = _curriculum_plan(tmp_path, train)
    config_path = tmp_path / "pilot.yaml"
    configured = {
        key: value
        for key, value in _valid_config().items()
        if key not in {"early_stopping", "early_stopping_patience"}
    } | {
        "training_order_mode": "staged",
        "training_order_plan_path": plan_path.name,
        "training_order_plan_sha256": plan_sha256,
    }
    _write_yaml(config_path, configured)
    dataset = bind_training_dataset(manifest, train=train, validation=validation)

    bound = bind_training_order_plan(
        load_training_config(config_path),
        dataset,
    )

    assert bound is not None
    assert bound.path == plan_path.resolve()
    assert bound.sha256 == "sha256:" + plan_sha256
    assert bound.binding()["source_sha256"] == hashlib.sha256(
        train.read_bytes()
    ).hexdigest()
    original_payload = plan_path.read_bytes()

    _write_yaml(
        config_path,
        configured | {"training_order_mode": "shuffled"},
    )
    with pytest.raises(TrainingIntegrityError, match="mode differs"):
        bind_training_order_plan(load_training_config(config_path), dataset)

    _write_yaml(
        config_path,
        configured | {"epochs": 1},
    )
    with pytest.raises(TrainingIntegrityError, match="epochs differ"):
        bind_training_order_plan(load_training_config(config_path), dataset)

    wrong_source = json.loads(original_payload)
    wrong_source["source"]["sha256"] = "c" * 64
    wrong_source_payload = canonical_json_bytes(wrong_source)
    plan_path.write_bytes(wrong_source_payload)
    _write_yaml(
        config_path,
        configured
        | {
            "training_order_plan_sha256": hashlib.sha256(
                wrong_source_payload
            ).hexdigest()
        },
    )
    with pytest.raises(TrainingIntegrityError, match="source hash differs"):
        bind_training_order_plan(load_training_config(config_path), dataset)

    plan_path.write_bytes(original_payload)
    _write_yaml(
        config_path,
        configured | {"training_order_plan_sha256": "0" * 64},
    )
    with pytest.raises(TrainingIntegrityError, match="SHA-256 differs"):
        bind_training_order_plan(load_training_config(config_path), dataset)

    tampered = json.loads(plan_path.read_text(encoding="utf-8"))
    tampered["epoch_orders"][0]["example_ids"].reverse()
    tampered_payload = canonical_json_bytes(tampered)
    plan_path.write_bytes(tampered_payload)
    _write_yaml(
        config_path,
        configured
        | {
            "training_order_plan_sha256": hashlib.sha256(
                tampered_payload
            ).hexdigest()
        },
    )
    with pytest.raises(TrainingIntegrityError, match="validation failed"):
        bind_training_order_plan(load_training_config(config_path), dataset)


def test_checked_in_smoke_and_pilot_configs_are_executable_but_final_stays_unselected() -> None:
    config_root = Path(__file__).parents[1] / "configs"

    assert load_training_config(config_root / "train_smoke.yaml").values["run_kind"] == "smoke"
    assert load_training_config(config_root / "train_pilot.yaml").values["run_kind"] == "pilot"
    with pytest.raises(TrainingIntegrityError, match="placeholder|micro_batch_size|learning_rate"):
        load_training_config(config_root / "train_final.yaml")


def test_dataset_binding_verifies_exact_manifest_bytes_hashes_counts_and_provenance(
    tmp_path: Path,
) -> None:
    train, validation, manifest = _dataset(tmp_path)

    bound = bind_training_dataset(
        manifest,
        train=train,
        validation=validation,
        train_limit=3,
        validation_limit=None,
    )

    assert [row["example_id"] for row in bound.train.records] == [
        "train-0",
        "train-1",
        "train-2",
    ]
    assert bound.train.total_records == 5
    assert bound.train.selected_records == 3
    assert bound.manifest["sha256"] == ("sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest())
    assert bound.train.sha256 == ("sha256:" + hashlib.sha256(train.read_bytes()).hexdigest())

    train.write_bytes(train.read_bytes().replace(b"roh 0", b"raw 0", 1))
    with pytest.raises(TrainingIntegrityError, match="train SHA-256"):
        bind_training_dataset(manifest, train=train, validation=validation)


class _BackendTokenizer:
    def to_str(self) -> str:
        return '{"model":{"vocab":{"a":0}}}'


class _Tokenizer:
    name_or_path = BASE_MODEL
    vocab_size = 1
    pad_token_id = 0
    eos_token_id = 1
    bos_token_id = 2
    unk_token_id = 3
    chat_template = "{{ messages }}"
    backend_tokenizer = _BackendTokenizer()


def test_tokenizer_identity_accepts_only_the_explicit_bound_local_source(
    tmp_path: Path,
) -> None:
    local_source = tmp_path / "parent-checkpoint"
    local_source.mkdir()
    tokenizer = _Tokenizer()
    tokenizer.name_or_path = str(local_source)

    identity = capture_tokenizer_identity(
        tokenizer,
        model_id=BASE_MODEL,
        revision=BASE_REVISION,
        local_source_path=local_source,
    )

    assert identity["name_or_path"] == BASE_MODEL

    with pytest.raises(
        TrainingIntegrityError,
        match="name_or_path must equal the pinned model id",
    ):
        capture_tokenizer_identity(
            tokenizer,
            model_id=BASE_MODEL,
            revision=BASE_REVISION,
        )

    other_source = tmp_path / "other-checkpoint"
    other_source.mkdir()
    with pytest.raises(
        TrainingIntegrityError,
        match="does not match the explicitly bound local source",
    ):
        capture_tokenizer_identity(
            tokenizer,
            model_id=BASE_MODEL,
            revision=BASE_REVISION,
            local_source_path=other_source,
        )


def test_run_identity_binds_config_dataset_code_cli_and_tokenizer_immutably(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "pilot.yaml"
    _write_yaml(config_path, _valid_config())
    train, validation, manifest = _dataset(tmp_path)
    config = load_training_config(config_path)
    dataset = bind_training_dataset(manifest, train=train, validation=validation)
    tokenizer = capture_tokenizer_identity(
        _Tokenizer(),
        model_id=BASE_MODEL,
        revision=BASE_REVISION,
    )
    identity = build_run_identity(
        config=config,
        dataset=dataset,
        code={
            "git_head": "a" * 40,
            "worktree_status_sha256": "sha256:" + "b" * 64,
            "worktree_diff_sha256": "sha256:" + "c" * 64,
            "dirty": True,
            "files": [
                {
                    "path": "scripts/train_sft.py",
                    "bytes": 123,
                    "sha256": "sha256:" + "d" * 64,
                }
            ],
        },
        cli={"output_dir": str(tmp_path / "run"), "save_final": False},
        tokenizer=tokenizer,
        runtime={"torch": "2.11.0", "transformers": "4.57.6"},
        effective_training={
            "learning_rate": 0.00002,
            "max_sequence_length": 768,
            "micro_batch_size": 2,
            "gradient_accumulation_steps": 8,
            "max_steps": 1,
        },
        training_order={
            "plan_sha256": "a" * 64,
            "rejected_record_count": 1,
            "effective_epoch_orders": [
                {"epoch": 0, "effective_example_ids_sha256": "b" * 64}
            ],
        },
    )

    assert identity["run_fingerprint"].startswith("sha256:")
    assert identity["bindings"]["config"]["sha256"] == config.sha256
    assert identity["bindings"]["dataset"]["train"]["sha256"] == dataset.train.sha256
    assert identity["bindings"]["tokenizer"]["backend_sha256"].startswith("sha256:")
    assert identity["bindings"]["training_order"]["rejected_record_count"] == 1

    run_dir = tmp_path / "run"
    first = prepare_run_identity(run_dir, identity, resume=False)
    assert first.read_bytes().endswith(b"\n")
    with pytest.raises(TrainingIntegrityError, match="fresh run"):
        prepare_run_identity(run_dir, identity, resume=False)
    prepare_run_identity(run_dir, identity, resume=True)

    changed = json.loads(json.dumps(identity))
    changed["bindings"]["effective_training"]["learning_rate"] = 0.00005
    changed["run_fingerprint"] = "sha256:" + "e" * 64
    with pytest.raises(TrainingIntegrityError, match="fingerprint"):
        prepare_run_identity(run_dir, changed, resume=True)


def test_each_exact_cli_invocation_is_immutably_recorded_before_training(
    tmp_path: Path,
) -> None:
    fingerprint = "sha256:" + "a" * 64
    argv = [
        "scripts/train_sft.py",
        "--config",
        "configs/train_pilot.yaml",
        "--resume",
        "auto",
    ]

    first = record_invocation_evidence(
        tmp_path,
        run_fingerprint=fingerprint,
        argv=argv,
        working_directory=tmp_path,
        resume_checkpoint={"checkpoint": "checkpoint-20", "global_step": 20},
        training_order={
            "plan_sha256": "a" * 64,
            "rejected_record_count": 1,
        },
    )
    second = record_invocation_evidence(
        tmp_path,
        run_fingerprint=fingerprint,
        argv=argv,
        working_directory=tmp_path,
        resume_checkpoint={"checkpoint": "checkpoint-20", "global_step": 20},
        training_order={
            "plan_sha256": "a" * 64,
            "rejected_record_count": 1,
        },
    )

    assert first == second
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["argv"] == argv
    assert payload["argv_sha256"] == (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                argv,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        ).hexdigest()
    )
    assert payload["resume_checkpoint"]["global_step"] == 20
    assert payload["training_order"]["rejected_record_count"] == 1
    with pytest.raises(TrainingIntegrityError, match="immutable JSON"):
        write_immutable_json(first, {"foreign": True})


def test_resume_requires_checkpoint_and_trainer_state_fingerprints(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint-20"
    checkpoint.mkdir()
    fingerprint = "sha256:" + "a" * 64
    record_checkpoint_identity(checkpoint, run_fingerprint=fingerprint, global_step=20)
    (checkpoint / "trainer_state.json").write_text(
        json.dumps({"global_step": 20}),
        encoding="utf-8",
    )

    evidence = verify_resume_checkpoint(checkpoint, expected_run_fingerprint=fingerprint)

    assert evidence["global_step"] == 20
    assert evidence["checkpoint"] == "checkpoint-20"

    with pytest.raises(TrainingIntegrityError, match="fingerprint"):
        verify_resume_checkpoint(
            checkpoint,
            expected_run_fingerprint="sha256:" + "b" * 64,
        )
    (checkpoint / "trainer_state.json").write_text(
        json.dumps({"global_step": 19}),
        encoding="utf-8",
    )
    with pytest.raises(TrainingIntegrityError, match="global_step"):
        verify_resume_checkpoint(checkpoint, expected_run_fingerprint=fingerprint)


def _write_fake_bf16_checkpoint(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        json.dumps({"model_type": "gemma3_text", "torch_dtype": "bfloat16"}),
        encoding="utf-8",
    )
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")
    header = {
        "weight": {
            "dtype": "BF16",
            "shape": [1],
            "data_offsets": [0, 2],
        }
    }
    header_bytes = json.dumps(header, separators=(",", ":")).encode()
    (root / "model.safetensors").write_bytes(len(header_bytes).to_bytes(8, "little") + header_bytes + b"\0\0")


def test_final_model_is_reload_verified_in_unique_staging_then_atomically_published(
    tmp_path: Path,
) -> None:
    fingerprint = "sha256:" + "f" * 64
    reloaded: list[Path] = []

    def save(stage: Path) -> None:
        _write_fake_bf16_checkpoint(stage)

    def reload(stage: Path) -> dict[str, object]:
        assert ".staging-" in stage.name
        reloaded.append(stage)
        return {
            "status": "passed",
            "model_type": "gemma3_text",
            "parameter_count": 270_000_000,
        }

    result = publish_final_model(
        tmp_path,
        run_fingerprint=fingerprint,
        save_artifacts=save,
        reload_verify=reload,
        protection_policy=legacy_policy_evidence(),
    )

    destination = tmp_path / result["directory"]
    assert destination.is_dir()
    assert reloaded and not reloaded[0].exists()
    assert result["directory"] == "final-" + "f" * 16
    assert json.loads((destination / "reload-verification.json").read_text())["status"] == "passed"
    inventory = json.loads((destination / "checkpoint-inventory.json").read_text())
    assert inventory["variant"] == "bf16"
    assert {item["path"] for item in inventory["files"]} >= {
        "config.json",
        "model.safetensors",
        PROTECTION_POLICY_FILENAME,
        "reload-verification.json",
        "tokenizer.json",
    }

    foreign = destination / "foreign.txt"
    foreign.write_text("preserve", encoding="utf-8")
    with pytest.raises(TrainingIntegrityError, match="already exists"):
        publish_final_model(
            tmp_path,
            run_fingerprint=fingerprint,
            save_artifacts=save,
            reload_verify=reload,
        )
    assert foreign.read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob(".final-*.staging-*"))


def test_final_model_is_never_published_when_fresh_reload_fails(tmp_path: Path) -> None:
    def fail_reload(_stage: Path) -> dict[str, object]:
        raise RuntimeError("reload failed")

    with pytest.raises(TrainingIntegrityError, match="fresh reload"):
        publish_final_model(
            tmp_path,
            run_fingerprint="sha256:" + "1" * 64,
            save_artifacts=_write_fake_bf16_checkpoint,
            reload_verify=fail_reload,
        )
    assert not list(tmp_path.iterdir())


def test_code_identity_binds_git_revision_worktree_and_exact_file_bytes(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "train.py"
    source.write_text("print('v1')\n", encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "train.py"],
        ["git", "commit", "-qm", "initial"],
    ):
        subprocess.run(command, cwd=repository, check=True)

    clean = capture_code_identity(repository, [source])

    assert clean["dirty"] is False
    assert (
        clean["git_head"]
        == subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    assert clean["files"] == [
        {
            "path": "train.py",
            "bytes": len(source.read_bytes()),
            "sha256": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    ]

    source.write_text("print('v2')\n", encoding="utf-8")
    dirty = capture_code_identity(repository, [source])
    assert dirty["dirty"] is True
    assert dirty["worktree_status_sha256"] != clean["worktree_status_sha256"]
    assert dirty["worktree_diff_sha256"] != clean["worktree_diff_sha256"]
    assert dirty["files"][0]["sha256"] != clean["files"][0]["sha256"]


def test_fresh_reload_uses_only_local_files_and_reports_verified_identity(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    calls: list[tuple[str, str, dict[str, object]]] = []

    def tokenizer_loader(path: str, **kwargs: object) -> _Tokenizer:
        calls.append(("tokenizer", path, kwargs))
        return _Tokenizer()

    class Model:
        config = SimpleNamespace(model_type="gemma3_text")

        def eval(self) -> None:
            calls.append(("eval", "", {}))

        def num_parameters(self) -> int:
            return 270_000_000

    def model_loader(path: str, **kwargs: object) -> Model:
        calls.append(("model", path, kwargs))
        return Model()

    result = fresh_reload_local_model(
        checkpoint,
        model_loader=model_loader,
        tokenizer_loader=tokenizer_loader,
    )

    assert result == {
        "status": "passed",
        "loader": "transformers_local",
        "model_type": "gemma3_text",
        "parameter_count": 270_000_000,
        "tokenizer_class": f"{_Tokenizer.__module__}.{_Tokenizer.__qualname__}",
    }
    assert [kind for kind, _path, _kwargs in calls] == ["tokenizer", "model", "eval"]
    for kind, path, kwargs in calls[:2]:
        assert path == str(checkpoint.resolve()), kind
        assert kwargs["local_files_only"] is True
        assert kwargs["trust_remote_code"] is False


def test_checkpoint_summary_persists_surviving_last_and_best_metric_evidence(
    tmp_path: Path,
) -> None:
    fingerprint = "sha256:" + "a" * 64
    for step in (10, 20):
        checkpoint = tmp_path / f"checkpoint-{step}"
        checkpoint.mkdir()
        record_checkpoint_identity(
            checkpoint,
            run_fingerprint=fingerprint,
            global_step=step,
        )
        (checkpoint / "trainer_state.json").write_text(
            json.dumps({"global_step": step}),
            encoding="utf-8",
        )
    state = SimpleNamespace(
        global_step=20,
        epoch=1.5,
        best_metric=0.42,
        best_model_checkpoint=str(tmp_path / "checkpoint-10"),
    )

    summary = summarize_checkpoint_state(
        tmp_path,
        trainer_state=state,
        run_fingerprint=fingerprint,
    )

    assert summary["global_step"] == 20
    assert summary["epoch"] == 1.5
    assert summary["best_metric"] == 0.42
    assert summary["best_model_checkpoint"] == "checkpoint-10"
    assert summary["last_checkpoint"] == "checkpoint-20"
    assert [item["global_step"] for item in summary["surviving_checkpoints"]] == [10, 20]
