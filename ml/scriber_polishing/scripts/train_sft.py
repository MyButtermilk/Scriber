"""Run real, resumable completion-only SFT for the exact pinned Gemma base."""

from __future__ import annotations

import argparse
import hashlib
import platform
import random
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
import transformers
from torch.utils.data import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    EarlyStoppingCallback,
    Trainer,
    TrainerCallback,
    TrainingArguments,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION  # noqa: E402
from scriber_polishing.protected_spans import (  # noqa: E402
    PROTECTION_POLICY_FILENAME,
    legacy_policy_evidence,
)
from scriber_polishing.tokenize import tokenize_example  # noqa: E402
from scriber_polishing.train import _latest_checkpoint  # noqa: E402
from scriber_polishing.training_integrity import (  # noqa: E402
    TrainingIntegrityError,
    bind_improvement_order_plan,
    bind_parent_checkpoint,
    bind_protection_policy,
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
    training_code_identity_paths,
    verify_checkpoint_protection_policy,
    verify_resume_checkpoint,
    write_immutable_json,
)
from scriber_polishing.training_runtime import (  # noqa: E402
    CompletionCollator,
    ProtectedTrainingRecords,
    ResumableEpochOrderSampler,
    apply_training_protection,
    assert_configured_example_counts,
    build_effective_improvement_order,
    build_effective_training_order,
    resolve_epoch_order_schedule,
    resolve_training_schedule,
)

_CODE_IDENTITY_PATHS = training_code_identity_paths(
    PROJECT_ROOT,
    Path(__file__).resolve(),
)


def _load_training_tokenizer(parent_checkpoint: Any | None) -> Any:
    options: dict[str, Any] = {
        "trust_remote_code": False,
        "fix_mistral_regex": False,
    }
    if parent_checkpoint is None:
        source = BASE_MODEL_ID
        options["revision"] = BASE_MODEL_REVISION
    else:
        source = str(parent_checkpoint.path)
        options["local_files_only"] = True
    return AutoTokenizer.from_pretrained(source, **options)


class TokenizedDataset(Dataset):
    def __init__(self, records: list[dict[str, list[int]]]) -> None:
        self.records = records

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index: int) -> dict[str, list[int]]:
        return self.records[index]


class CurriculumOrderTrainer(Trainer):
    """Trainer variant that can only use the explicitly supplied epoch sampler."""

    def __init__(
        self,
        *args: Any,
        training_order_sampler: ResumableEpochOrderSampler,
        **kwargs: Any,
    ) -> None:
        if not isinstance(training_order_sampler, ResumableEpochOrderSampler):
            raise TrainingIntegrityError("curriculum Trainer requires a ResumableEpochOrderSampler")
        self._training_order_sampler = training_order_sampler
        super().__init__(*args, **kwargs)

    def _get_train_sampler(self, train_dataset: Dataset | None = None):
        dataset = self.train_dataset if train_dataset is None else train_dataset
        if dataset is None:
            raise TrainingIntegrityError("curriculum Trainer requires a sized training dataset")
        if len(dataset) != len(self._training_order_sampler):
            raise TrainingIntegrityError("curriculum sampler length differs from training dataset")
        return self._training_order_sampler


def _trainer_class_for_sampler(
    sampler: ResumableEpochOrderSampler | None,
) -> type[Trainer]:
    return Trainer if sampler is None else CurriculumOrderTrainer


def _resolve_eval_save_steps(
    *,
    run_kind: str,
    configured_eval_steps: int,
    configured_save_steps: int,
    max_steps: int,
) -> tuple[int, int]:
    """Keep attested improvement intervals exact; retain legacy clamping."""

    if run_kind == "improvement":
        if configured_eval_steps > max_steps or configured_save_steps > max_steps:
            raise TrainingIntegrityError("improvement eval/save intervals exceed the bound full-epoch update schedule")
        return configured_eval_steps, configured_save_steps
    save_steps = min(configured_save_steps, max_steps)
    return min(configured_eval_steps, save_steps), save_steps


class RunIdentityCheckpointCallback(TrainerCallback):
    """Write the immutable run fingerprint into every completed checkpoint."""

    def __init__(
        self,
        run_fingerprint: str,
        protection_policy: dict[str, object],
    ) -> None:
        self.run_fingerprint = run_fingerprint
        self.protection_policy = protection_policy

    def on_save(self, args: TrainingArguments, state: Any, control: Any, **_: Any) -> Any:
        checkpoint = Path(args.output_dir) / f"checkpoint-{state.global_step}"
        record_checkpoint_identity(
            checkpoint,
            run_fingerprint=self.run_fingerprint,
            global_step=int(state.global_step),
        )
        write_immutable_json(
            checkpoint / PROTECTION_POLICY_FILENAME,
            self.protection_policy,
        )
        return control


def _tokenize_records(
    tokenizer: Any,
    records: list[dict[str, Any]],
    *,
    max_length: int,
) -> tuple[list[dict[str, list[int]]], tuple[str, ...], tuple[str, ...]]:
    encoded: list[dict[str, list[int]]] = []
    used_example_ids: list[str] = []
    rejected_example_ids: list[str] = []
    for record in records:
        item = tokenize_example(
            tokenizer,
            transcript=record["source_text"],
            completion=record["target_sst"],
        )
        if len(item["input_ids"]) > max_length:
            rejected_example_ids.append(record["example_id"])
            continue
        encoded.append(item)
        used_example_ids.append(record["example_id"])
    if not encoded:
        raise ValueError("all training records exceed max_sequence_length")
    return encoded, tuple(used_example_ids), tuple(rejected_example_ids)


def _resume_path(mode: str, output_dir: Path) -> Path | None:
    if mode == "none":
        return None
    if mode == "auto":
        checkpoint = _latest_checkpoint(output_dir)
        if checkpoint is None:
            raise ValueError("resume=auto requires an existing numbered checkpoint")
        return checkpoint.resolve()
    path = Path(mode).resolve()
    if not path.is_dir():
        raise ValueError(f"resume checkpoint does not exist: {path}")
    if path.parent != output_dir or not path.name.removeprefix("checkpoint-").isdigit():
        raise ValueError("resume checkpoint must be a numbered directory inside output-dir")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--resume", default="none", help="none, auto, or an explicit checkpoint directory")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--train-limit", type=int)
    parser.add_argument("--validation-limit", type=int)
    parser.add_argument("--learning-rate", type=float)
    parser.add_argument("--max-sequence-length", type=int)
    parser.add_argument("--micro-batch-size", type=int, choices=(1, 2, 4))
    parser.add_argument("--gradient-accumulation-steps", type=int)
    parser.add_argument("--save-final", action="store_true")
    args = parser.parse_args()

    config_snapshot = load_training_config(args.config)
    config = config_snapshot.values
    output_dir = args.output_dir.resolve()
    report_path = args.report.resolve()
    if report_path.exists():
        raise TrainingIntegrityError(f"training report already exists: {report_path}")
    if config["run_kind"] in {"improvement", "final"}:
        if not args.save_final:
            raise TrainingIntegrityError(f"{config['run_kind']} training requires --save-final")
        override_names = (
            "max_steps",
            "train_limit",
            "validation_limit",
            "learning_rate",
            "max_sequence_length",
            "micro_batch_size",
            "gradient_accumulation_steps",
        )
        selected_overrides = [name for name in override_names if getattr(args, name) is not None]
        if selected_overrides:
            raise TrainingIntegrityError(
                f"{config['run_kind']} training forbids CLI "
                "hyperparameter/data-limit overrides: " + ", ".join(selected_overrides)
            )
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required; CPU training is forbidden")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("the configured BF16 training mode is not supported")
    seed = int(config["seed"])
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(True, warn_only=True)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.cuda.reset_peak_memory_stats()

    parent_checkpoint = bind_parent_checkpoint(config_snapshot)
    tokenizer = _load_training_tokenizer(parent_checkpoint)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    tokenizer_identity = capture_tokenizer_identity(
        tokenizer,
        model_id=BASE_MODEL_ID,
        revision=BASE_MODEL_REVISION,
        local_source_path=(parent_checkpoint.path if parent_checkpoint is not None else None),
    )
    bound_protection_policy = bind_protection_policy(
        config_snapshot,
        tokenizer=tokenizer,
    )
    protection_policy = (
        dict(bound_protection_policy.values)
        if bound_protection_policy is not None
        else legacy_policy_evidence(tokenizer_identity)
    )
    max_length = int(args.max_sequence_length or config["max_sequence_length"])
    if max_length < 128 or max_length > 4096:
        raise TrainingIntegrityError("effective max_sequence_length must be between 128 and 4096")
    length_candidates = config.get("max_sequence_length_candidates")
    if (
        args.max_sequence_length is not None
        and length_candidates is not None
        and args.max_sequence_length not in length_candidates
    ):
        raise ValueError("--max-sequence-length is not a configured candidate")
    learning_rate = float(args.learning_rate if args.learning_rate is not None else config["learning_rate"])
    if not 0.0 < learning_rate <= 0.01:
        raise TrainingIntegrityError("effective learning_rate must be greater than zero and at most 0.01")
    learning_rate_candidates = config.get("learning_rate_candidates")
    if (
        args.learning_rate is not None
        and learning_rate_candidates is not None
        and args.learning_rate not in learning_rate_candidates
    ):
        raise ValueError("--learning-rate is not a configured candidate")
    micro_batch_size = int(args.micro_batch_size if args.micro_batch_size is not None else config["micro_batch_size"])
    gradient_accumulation_steps = int(
        args.gradient_accumulation_steps
        if args.gradient_accumulation_steps is not None
        else config["gradient_accumulation_steps"]
    )
    if gradient_accumulation_steps <= 0:
        raise ValueError("gradient accumulation steps must be positive")
    train_limit = args.train_limit if args.train_limit is not None else config.get("train_examples")
    validation_limit = args.validation_limit if args.validation_limit is not None else config.get("validation_examples")
    dataset = bind_training_dataset(
        args.dataset_manifest,
        train=args.train,
        validation=args.validation,
        train_limit=train_limit,
        validation_limit=validation_limit,
    )
    pilot_order_plan = bind_training_order_plan(config_snapshot, dataset) if config["run_kind"] == "pilot" else None
    if pilot_order_plan is not None and args.max_steps is not None:
        raise TrainingIntegrityError("explicit training order forbids --max-steps; config epochs bind the plan")
    if pilot_order_plan is not None and args.train_limit is not None:
        raise TrainingIntegrityError("explicit training order forbids --train-limit")
    train_records = list(dataset.train.records)
    validation_records = list(dataset.validation.records)
    train_protected: ProtectedTrainingRecords = apply_training_protection(
        train_records,
        policy=protection_policy,
    )
    validation_protected: ProtectedTrainingRecords = apply_training_protection(
        validation_records,
        policy=protection_policy,
    )
    protection_application = {
        "schema_version": 1,
        "policy_id": protection_policy["policy_id"],
        "policy_sha256": protection_policy["policy_sha256"],
        "train": dict(train_protected.evidence),
        "validation": dict(validation_protected.evidence),
    }
    improvement_order_plan = None
    if config["run_kind"] == "improvement":
        if bound_protection_policy is None or parent_checkpoint is None:
            raise TrainingIntegrityError("improvement training requires bound policy and parent inputs")
        improvement_order_plan = bind_improvement_order_plan(
            config_snapshot,
            dataset,
            protection_policy=bound_protection_policy,
            parent_checkpoint=parent_checkpoint,
            protection_application=train_protected.evidence,
        )
    training_order_plan = pilot_order_plan or improvement_order_plan
    train_encoded, train_used_ids, train_rejected_ids = _tokenize_records(
        tokenizer,
        list(train_protected.records),
        max_length=max_length,
    )
    validation_encoded, _validation_used_ids, validation_rejected_ids = _tokenize_records(
        tokenizer,
        list(validation_protected.records),
        max_length=max_length,
    )
    train_oversize_ids = train_rejected_ids
    validation_oversize_ids = validation_rejected_ids
    train_rejected_set = {
        *train_protected.rejected_example_ids,
        *train_oversize_ids,
    }
    validation_rejected_set = {
        *validation_protected.rejected_example_ids,
        *validation_oversize_ids,
    }
    train_rejected_ids = tuple(
        str(record["example_id"]) for record in train_records if record["example_id"] in train_rejected_set
    )
    validation_rejected_ids = tuple(
        str(record["example_id"]) for record in validation_records if record["example_id"] in validation_rejected_set
    )
    validation_oversize = len(validation_oversize_ids)
    assert_configured_example_counts(
        config,
        loaded={"train": len(train_records), "validation": len(validation_records)},
        used={"train": len(train_encoded), "validation": len(validation_encoded)},
    )
    if improvement_order_plan is not None:
        effective_order = build_effective_improvement_order(
            improvement_order_plan.values,
            plan_binding=improvement_order_plan.binding(),
            loaded_example_ids=[str(record["example_id"]) for record in train_records],
            safe_example_ids=train_protected.accepted_example_ids,
            policy_rejected_example_ids=(train_protected.rejected_example_ids),
            policy_rejection_evidence=train_protected.evidence,
            used_example_ids=train_used_ids,
            sequence_rejected_example_ids=train_oversize_ids,
            sequence_lengths=[len(item["input_ids"]) for item in train_encoded],
        )
    elif pilot_order_plan is not None:
        effective_order = build_effective_training_order(
            pilot_order_plan.values,
            plan_binding=pilot_order_plan.binding(),
            loaded_example_ids=[str(record["example_id"]) for record in train_records],
            used_example_ids=train_used_ids,
            rejected_example_ids=train_rejected_ids,
            sequence_lengths=[len(item["input_ids"]) for item in train_encoded],
        )
    else:
        effective_order = None
    training_order_sampler = (
        ResumableEpochOrderSampler(effective_order.epoch_indices) if effective_order is not None else None
    )
    curriculum_order = dict(effective_order.evidence) if effective_order is not None else None

    schedule_config = dict(config)
    schedule_config["micro_batch_size"] = micro_batch_size
    schedule_config["gradient_accumulation_steps"] = gradient_accumulation_steps
    if args.max_steps is not None:
        schedule_config["max_steps"] = args.max_steps
    schedule = (
        resolve_epoch_order_schedule(
            schedule_config,
            train_examples_used=len(train_encoded),
        )
        if training_order_plan is not None
        else resolve_training_schedule(
            schedule_config,
            train_examples_used=len(train_encoded),
        )
    )
    max_steps = int(schedule["max_steps"])
    resume = _resume_path(args.resume, output_dir)
    code_identity = capture_code_identity(
        REPOSITORY_ROOT,
        list(_CODE_IDENTITY_PATHS),
    )
    protection_binding = (
        bound_protection_policy.binding()
        if bound_protection_policy is not None
        else {
            "source": "implicit_legacy_default",
            "policy_id": protection_policy["policy_id"],
            "policy_sha256": protection_policy["policy_sha256"],
            "evidence": protection_policy,
        }
    )
    semantic_cli = {
        "entrypoint": Path(__file__).resolve().relative_to(REPOSITORY_ROOT).as_posix(),
        "config": str(config_snapshot.path),
        "dataset_manifest": str(Path(args.dataset_manifest).resolve()),
        "train": str(dataset.train.path),
        "validation": str(dataset.validation.path),
        "output_dir": str(output_dir),
        "save_final": args.save_final,
        "train_limit": args.train_limit,
        "validation_limit": args.validation_limit,
        "learning_rate_override": args.learning_rate,
        "max_sequence_length_override": args.max_sequence_length,
        "micro_batch_size_override": args.micro_batch_size,
        "gradient_accumulation_steps_override": args.gradient_accumulation_steps,
        "max_steps_override": args.max_steps,
    }
    if training_order_plan is not None:
        semantic_cli["training_order_plan"] = str(training_order_plan.path)
    effective_training = {
        "run_kind": config["run_kind"],
        "method": "full_sft",
        "seed": seed,
        "learning_rate": learning_rate,
        "max_sequence_length": max_length,
        "micro_batch_size": micro_batch_size,
        "gradient_accumulation_steps": gradient_accumulation_steps,
        "effective_batch_size": micro_batch_size * gradient_accumulation_steps,
        "schedule": schedule,
        "train_examples_loaded": len(train_records),
        "train_examples_used": len(train_encoded),
        "validation_examples_loaded": len(validation_records),
        "validation_examples_used": len(validation_encoded),
        "bf16": True,
        "gradient_checkpointing": bool(config["gradient_checkpointing"]),
        "optimizer": "adamw_torch_fused",
        "sdpa": True,
        "completion_only_loss": True,
        "save_total_limit": int(config.get("save_total_limit", 2)),
    }
    if curriculum_order is not None:
        effective_training["training_order_mode"] = curriculum_order["mode"]
    runtime_identity = {
        "python": platform.python_version(),
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "cuda_runtime": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0),
    }
    run_identity = build_run_identity(
        config=config_snapshot,
        dataset=dataset,
        code=code_identity,
        cli=semantic_cli,
        tokenizer=tokenizer_identity,
        runtime=runtime_identity,
        effective_training=effective_training,
        training_order=curriculum_order,
        protection_policy=protection_binding,
        protection_application=protection_application,
        parent_checkpoint=(parent_checkpoint.binding() if parent_checkpoint is not None else None),
    )
    run_fingerprint = str(run_identity["run_fingerprint"])
    identity_path = prepare_run_identity(
        output_dir,
        run_identity,
        resume=resume is not None,
    )
    resume_evidence = (
        {
            **verify_resume_checkpoint(
                resume,
                expected_run_fingerprint=run_fingerprint,
            ),
            "protection_policy": verify_checkpoint_protection_policy(
                resume,
                expected_policy=protection_policy,
                tokenizer=tokenizer,
            ),
        }
        if resume is not None
        else None
    )
    invocation_path = record_invocation_evidence(
        output_dir,
        run_fingerprint=run_fingerprint,
        argv=list(sys.argv),
        working_directory=Path.cwd(),
        resume_checkpoint=resume_evidence,
        training_order=curriculum_order,
    )

    started = time.perf_counter()
    model_source = str(parent_checkpoint.path) if parent_checkpoint is not None else BASE_MODEL_ID
    model_load_options: dict[str, Any] = {
        "dtype": torch.bfloat16,
        "attn_implementation": "sdpa",
        "low_cpu_mem_usage": True,
        "trust_remote_code": False,
    }
    if parent_checkpoint is None:
        model_load_options["revision"] = BASE_MODEL_REVISION
    else:
        model_load_options["local_files_only"] = True
    model = AutoModelForCausalLM.from_pretrained(
        model_source,
        **model_load_options,
    )
    model.config.scriber_protection_policy_id = protection_policy["policy_id"]
    model.config.scriber_protection_policy_sha256 = protection_policy["policy_sha256"]
    model.config.use_cache = False
    model.enable_input_require_grads()
    configured_save_steps = int(config["save_steps"])
    configured_eval_steps = int(config["eval_steps"])
    eval_steps, save_steps = _resolve_eval_save_steps(
        run_kind=str(config["run_kind"]),
        configured_eval_steps=configured_eval_steps,
        configured_save_steps=configured_save_steps,
        max_steps=max_steps,
    )
    early_stopping = bool(config.get("early_stopping", False))
    save_total_limit = int(config.get("save_total_limit", 2))
    training_args = TrainingArguments(
        output_dir=str(output_dir),
        do_train=True,
        do_eval=True,
        eval_strategy="steps",
        eval_steps=max(1, eval_steps),
        per_device_train_batch_size=micro_batch_size,
        per_device_eval_batch_size=micro_batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        learning_rate=learning_rate,
        weight_decay=float(config["weight_decay"]),
        max_grad_norm=float(config["max_grad_norm"]),
        max_steps=max_steps,
        warmup_ratio=float(config["warmup_ratio"]),
        logging_strategy="steps",
        logging_steps=1,
        logging_first_step=True,
        save_strategy="steps",
        save_steps=save_steps,
        save_total_limit=save_total_limit,
        save_safetensors=True,
        bf16=bool(config["bf16"]),
        seed=seed,
        data_seed=seed,
        full_determinism=True,
        gradient_checkpointing=bool(config["gradient_checkpointing"]),
        gradient_checkpointing_kwargs={"use_reentrant": False},
        group_by_length=training_order_sampler is None,
        length_column_name="length",
        optim=str(config["optimizer"]),
        remove_unused_columns=False,
        dataloader_num_workers=0,
        report_to=["tensorboard"],
        run_name=f"scriber-polishing-{args.config.stem}",
        push_to_hub=False,
        use_cpu=False,
        ignore_data_skip=False,
        load_best_model_at_end=early_stopping,
        metric_for_best_model="eval_loss" if early_stopping else None,
        greater_is_better=False if early_stopping else None,
    )
    if training_order_sampler is not None and (training_args.world_size != 1 or training_args.n_gpu != 1):
        raise TrainingIntegrityError(
            "explicit training order currently requires exactly one CUDA device and one training process"
        )
    callbacks: list[TrainerCallback] = [
        RunIdentityCheckpointCallback(run_fingerprint, protection_policy),
    ]
    if early_stopping:
        callbacks.append(EarlyStoppingCallback(early_stopping_patience=int(config["early_stopping_patience"])))
    trainer_kwargs = {
        "model": model,
        "args": training_args,
        "train_dataset": TokenizedDataset(train_encoded),
        "eval_dataset": TokenizedDataset(validation_encoded),
        "data_collator": CompletionCollator(tokenizer),
        "callbacks": callbacks,
    }
    trainer: Trainer
    trainer_class = _trainer_class_for_sampler(training_order_sampler)
    if trainer_class is Trainer:
        trainer = Trainer(**trainer_kwargs)
    else:
        trainer = trainer_class(
            **trainer_kwargs,
            training_order_sampler=training_order_sampler,
        )
        if trainer._get_train_sampler() is not training_order_sampler:
            raise TrainingIntegrityError("curriculum Trainer did not retain the explicit sampler")
    train_result = trainer.train(resume_from_checkpoint=str(resume) if resume else None)
    eval_metrics = trainer.evaluate()
    checkpoint_state = summarize_checkpoint_state(
        output_dir,
        trainer_state=trainer.state,
        run_fingerprint=run_fingerprint,
    )
    final_model: dict[str, object] | None = None
    if args.save_final:

        def save_artifacts(staging: Path) -> None:
            trainer.save_model(staging)
            tokenizer.save_pretrained(staging)

        final_model = publish_final_model(
            output_dir,
            run_fingerprint=run_fingerprint,
            save_artifacts=save_artifacts,
            reload_verify=fresh_reload_local_model,
            protection_policy=protection_policy,
        )
    elapsed = time.perf_counter() - started
    report = {
        "schema_version": 2,
        "base_model": BASE_MODEL_ID,
        "base_revision": BASE_MODEL_REVISION,
        "run_kind": config["run_kind"],
        "run_fingerprint": run_fingerprint,
        "run_identity": {
            "path": str(identity_path),
            "sha256": "sha256:" + hashlib.sha256(identity_path.read_bytes()).hexdigest(),
        },
        "invocation": {
            "path": str(invocation_path),
            "sha256": "sha256:" + hashlib.sha256(invocation_path.read_bytes()).hexdigest(),
        },
        "bindings": run_identity["bindings"],
        "gpu": torch.cuda.get_device_name(0),
        "torch_version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "bf16": bool(config["bf16"]),
        "sdpa": True,
        "seed": seed,
        "method": config["method"],
        "optimizer": "adamw_torch_fused",
        "learning_rate": learning_rate,
        "max_sequence_length": max_length,
        "micro_batch_size": micro_batch_size,
        "gradient_accumulation_steps": gradient_accumulation_steps,
        "effective_batch_size": micro_batch_size * gradient_accumulation_steps,
        "max_steps": max_steps,
        "schedule": schedule,
        "early_stopping": early_stopping,
        "save_total_limit": save_total_limit,
        "resume_from_checkpoint": resume_evidence,
        "warm_start_parent_checkpoint": (parent_checkpoint.binding() if parent_checkpoint is not None else None),
        "protection_policy": protection_binding,
        "protection_application": protection_application,
        "train_examples_loaded": len(train_records),
        "train_examples_used": len(train_encoded),
        "validation_examples_loaded": len(validation_records),
        "validation_examples_used": len(validation_encoded),
        "oversize_rejections": {
            "train": len(train_oversize_ids),
            "validation": validation_oversize,
        },
        "protection_rejections": {
            "train": len(train_protected.rejected_example_ids),
            "validation": len(validation_protected.rejected_example_ids),
        },
        "elapsed_seconds": elapsed,
        "peak_vram_mib": round(torch.cuda.max_memory_allocated() / 1024**2),
        "train_metrics": {
            key: float(value) for key, value in train_result.metrics.items() if isinstance(value, int | float)
        },
        "eval_metrics": {key: float(value) for key, value in eval_metrics.items() if isinstance(value, int | float)},
        "checkpoint_state": checkpoint_state,
        "final_model": final_model,
        "training_completed": True,
    }
    if curriculum_order is not None:
        if improvement_order_plan is not None:
            report["remediation_order"] = curriculum_order
        else:
            report["curriculum_order"] = curriculum_order
    write_immutable_json(report_path, report)
    print(f"training=complete steps={max_steps} peak_vram_mib={report['peak_vram_mib']} elapsed_seconds={elapsed:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
