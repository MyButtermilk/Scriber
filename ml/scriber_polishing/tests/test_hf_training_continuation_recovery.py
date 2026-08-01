from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.hf_training_continuation_recovery import (
    ContinuationRecoveryError,
    build_recovered_training_report,
    claim_recovery_output,
    materialize_recovered_final_model,
    verify_completed_continuation_checkpoint,
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _tree_sha256(files: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _bf16_safetensors() -> bytes:
    header = json.dumps(
        {
            "weight": {
                "dtype": "BF16",
                "shape": [1],
                "data_offsets": [0, 2],
            }
        },
        separators=(",", ":"),
    ).encode("utf-8")
    padding = (-len(header)) % 8
    padded = header + (b" " * padding)
    return len(padded).to_bytes(8, "little") + padded + b"\0\0"


def _completed_source(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "failed-run"
    identity_core = {
        "schema_version": 1,
        "fingerprint_algorithm": "sha256-canonical-json-v1",
        "bindings": {
            "effective_training": {
                "run_kind": "improvement",
                "schedule": {"max_steps": 254},
                "bf16": True,
                "sdpa": True,
                "seed": 270014,
                "method": "full_sft",
                "optimizer": "adamw_torch_fused",
                "learning_rate": 0.00001,
                "max_sequence_length": 768,
                "micro_batch_size": 2,
                "gradient_accumulation_steps": 8,
                "effective_batch_size": 16,
                "save_total_limit": 4,
                "train_examples_loaded": 4064,
                "train_examples_used": 4064,
                "validation_examples_loaded": 431,
                "validation_examples_used": 431,
            },
            "runtime": {
                "gpu": "fixture GPU",
                "torch": "2.11.0+cu128",
                "cuda_runtime": "12.8",
            },
            "parent_checkpoint": {
                "relationship": "warm_start_parent_not_resume",
                "tree_sha256": "sha256:" + "a" * 64,
            },
            "protection_policy": {"policy_id": "fixture"},
            "protection_application": {"policy_id": "fixture"},
            "training_order": {
                "mode": "shuffled",
                "bucket_algorithm": "fixed_window_length_descending_v1",
                "effective_epoch_orders": [
                    {
                        "epoch": 0,
                        "record_count": 4064,
                        "filtered_plan_example_ids_sha256": (
                            "625bfc2b54d7403dd41532925795f8e081da7522a45346262a22ba2a9c2fbef3"
                        ),
                        "effective_example_ids_sha256": (
                            "b8a03f8cd657f755c06934aae4169dda4386c8bc31d3eb6fa218ac3faa435c2d"
                        ),
                    }
                ],
            },
        },
    }
    fingerprint = _sha256(_json_bytes(identity_core))
    run_identity = {
        **identity_core,
        "run_fingerprint": fingerprint,
    }
    checkpoint_identity = {
        "schema_version": 1,
        "run_fingerprint": fingerprint,
        "global_step": 254,
    }
    trainer_state = {
        "global_step": 254,
        "max_steps": 254,
        "epoch": 1.0,
        "logging_steps": 1,
        "save_steps": 25,
        "eval_steps": 25,
        "num_train_epochs": 1,
        "train_batch_size": 2,
        "log_history": [
            {
                "step": step,
                "epoch": step / 254,
                "loss": 0.01,
                "grad_norm": 0.5,
                "learning_rate": 0.00001,
            }
            for step in range(1, 255)
        ],
    }
    payloads = {
        "training/run-identity.json": _json_bytes(run_identity),
        "training/checkpoint-254/scriber-run-identity.json": _json_bytes(checkpoint_identity),
        "training/checkpoint-254/trainer_state.json": _json_bytes(trainer_state),
        "training/checkpoint-254/config.json": b'{"dtype":"bfloat16"}\n',
        "training/checkpoint-254/generation_config.json": b"{}\n",
        "training/checkpoint-254/model.safetensors": b"exact-step-254-weights",
        "training/checkpoint-254/scriber-protection-policy.json": b"{}\n",
        "training/checkpoint-254/training_args.bin": b"training-arguments",
    }
    for relative, payload in payloads.items():
        _write(root / relative, payload)
    contract = {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_recovery_source",
        "failed_job_id": "fixture-job",
        "failed_job_status": "ERROR",
        "failure_stage": "publish_final_model.inventory_bf16_checkpoint",
        "failure_errno": 5,
        "run_fingerprint": fingerprint,
        "completed_training_steps": 254,
        "recovery_training_steps": 0,
        "files": {
            relative: {
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
            for relative, payload in sorted(payloads.items())
        },
    }
    return root, contract


def test_checkpoint254_is_recoverable_without_any_training_step(
    tmp_path: Path,
) -> None:
    root, contract = _completed_source(tmp_path)

    result = verify_completed_continuation_checkpoint(
        root,
        source_contract=contract,
    )

    assert result["status"] == "complete"
    assert result["completed_training_steps"] == 254
    assert result["recovery_training_steps"] == 0
    assert result["checkpoint"] == "checkpoint-254"
    assert result["run_fingerprint"] == contract["run_fingerprint"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("global_step", 253, "global_step must equal 254"),
        ("max_steps", 253, "max_steps must equal 254"),
        ("epoch", 0.99, "epoch must equal 1.0"),
    ],
)
def test_recovery_rejects_a_checkpoint_that_does_not_prove_all_254_steps(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    root, contract = _completed_source(tmp_path)
    state_path = root / "training/checkpoint-254/trainer_state.json"
    state = json.loads(state_path.read_bytes())
    state[field] = value
    payload = _json_bytes(state)
    state_path.write_bytes(payload)
    files = contract["files"]
    assert isinstance(files, dict)
    files["training/checkpoint-254/trainer_state.json"] = {
        "bytes": len(payload),
        "sha256": _sha256(payload),
    }

    with pytest.raises(ContinuationRecoveryError, match=message):
        verify_completed_continuation_checkpoint(
            root,
            source_contract=contract,
        )


def test_recovery_materializes_only_checkpoint254_weights_and_parent_tokenizer(
    tmp_path: Path,
) -> None:
    source, contract = _completed_source(tmp_path)
    model_payload = _bf16_safetensors()
    model_path = source / "training/checkpoint-254/model.safetensors"
    model_path.write_bytes(model_payload)
    files = contract["files"]
    assert isinstance(files, dict)
    files["training/checkpoint-254/model.safetensors"] = {
        "bytes": len(model_payload),
        "sha256": _sha256(model_payload),
    }
    checkpoint = source / "training/checkpoint-254"
    checkpoint_payloads = {
        "config.json": b'{"dtype":"bfloat16","model_type":"fixture"}\n',
        "generation_config.json": b"{}\n",
        "scriber-protection-policy.json": b"{}\n",
        "training_args.bin": b"new-training-arguments",
    }
    for name, payload in checkpoint_payloads.items():
        (checkpoint / name).write_bytes(payload)
        files[f"training/checkpoint-254/{name}"] = {
            "bytes": len(payload),
            "sha256": _sha256(payload),
        }
    verification = verify_completed_continuation_checkpoint(
        source,
        source_contract=contract,
    )

    parent = tmp_path / "accepted-parent"
    tokenizer_payloads = {
        "added_tokens.json": b"{}\n",
        "chat_template.jinja": b"fixture template\n",
        "special_tokens_map.json": b"{}\n",
        "tokenizer_config.json": b"{}\n",
        "tokenizer.json": b"{}\n",
        "tokenizer.model": b"fixture tokenizer model",
    }
    parent_payloads = {
        **tokenizer_payloads,
        "config.json": checkpoint_payloads["config.json"],
        "generation_config.json": checkpoint_payloads["generation_config.json"],
        "model.safetensors": b"accepted-parent-weights",
        "reload-verification.json": b"{}\n",
        "scriber-protection-policy.json": checkpoint_payloads["scriber-protection-policy.json"],
        "training_args.bin": b"old-training-arguments",
    }
    parent_files: list[dict[str, object]] = []
    for name, payload in sorted(parent_payloads.items()):
        _write(parent / name, payload)
        parent_files.append(
            {
                "path": name,
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
        )
    parent_inventory = {
        "schema_version": 1,
        "tree_sha256": _tree_sha256(parent_files),
        "file_count": len(parent_files),
        "total_bytes": sum(int(item["bytes"]) for item in parent_files),
        "files": parent_files,
    }
    _write(
        parent / "checkpoint-inventory.json",
        _json_bytes(parent_inventory),
    )
    reload_calls: list[Path] = []

    def reload_verify(path: Path) -> dict[str, object]:
        reload_calls.append(path)
        assert (path / "model.safetensors").read_bytes() == model_payload
        assert (path / "tokenizer.model").read_bytes() == tokenizer_payloads["tokenizer.model"]
        return {
            "status": "passed",
            "loader": "transformers_local",
            "model_type": "fixture",
            "parameter_count": 1,
            "tokenizer_class": "fixture.Tokenizer",
        }

    final_model = materialize_recovered_final_model(
        source,
        accepted_parent_root=parent,
        staging_parent=tmp_path / "local-staging",
        source_verification=verification,
        expected_parent_model_tree_sha256=parent_inventory["tree_sha256"],
        reload_verify=reload_verify,
    )

    final_root = Path(str(final_model["path"]))
    assert final_root.name.startswith("final-")
    assert reload_calls == [final_root]
    assert (final_root / "model.safetensors").read_bytes() == model_payload
    assert (final_root / "training_args.bin").read_bytes() == (checkpoint_payloads["training_args.bin"])
    assert (final_root / "tokenizer.model").read_bytes() == (tokenizer_payloads["tokenizer.model"])
    assert not (final_root / "optimizer.pt").exists()
    assert (final_root / "checkpoint-inventory.json").is_file()
    assert final_model["recovery_training_steps"] == 0


def test_recovery_cli_has_no_training_or_optimizer_path() -> None:
    script = Path(__file__).parents[1] / "scripts/recover_hf_training_continuation.py"
    source = script.read_text(encoding="utf-8")

    assert "train_sft.py" not in source
    assert ".train(" not in source
    assert "optimizer.pt" not in source
    assert "checkpoint-250" not in source
    assert "evaluator.evaluate()" in source

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--failed-run" in completed.stdout
    assert "--accepted-parent" in completed.stdout


def test_recovery_report_and_output_claim_preserve_zero_training_steps(
    tmp_path: Path,
) -> None:
    source, contract = _completed_source(tmp_path)
    verification = verify_completed_continuation_checkpoint(
        source,
        source_contract=contract,
    )
    fingerprint = str(contract["run_fingerprint"])
    final_model = {
        "directory": "final-" + fingerprint.removeprefix("sha256:")[:16],
        "path": str(tmp_path / "local-final"),
        "run_fingerprint": fingerprint,
        "inventory": {
            "tree_sha256": "sha256:" + "b" * 64,
            "file_count": 12,
            "total_bytes": 100,
        },
        "reload_verification": {"status": "passed"},
        "recovery_training_steps": 0,
        "source_checkpoint": "checkpoint-254",
    }
    contract["train_metrics"] = {
        "train_runtime": 690.8581,
        "train_samples_per_second": 5.883,
        "train_steps_per_second": 0.368,
        "train_loss": 0.002224381026875147,
        "epoch": 1.0,
    }
    contract["completed_validation_event"] = {
        "step": 254,
        "eval_loss": 0.005138558801263571,
    }
    output = tmp_path / "new-output"
    report = build_recovered_training_report(
        source,
        output_root=output,
        source_contract=contract,
        source_verification=verification,
        final_model=final_model,
        recovery_eval_metrics={
            "eval_loss": 0.0051,
            "eval_runtime": 15.0,
            "eval_samples_per_second": 28.0,
            "eval_steps_per_second": 14.0,
        },
    )

    assert report["training_completed"] is True
    assert report["checkpoint_state"]["global_step"] == 254
    assert report["recovery"]["training_steps_executed"] == 0
    assert report["recovery"]["validation_reexecuted"] is True
    assert report["final_model"]["path"] == str(output.resolve() / "training" / final_model["directory"])
    expected_lock = {
        "algorithm": "fixed_window_length_descending_v1",
        "example_ids_sha256": "b8a03f8cd657f755c06934aae4169dda4386c8bc31d3eb6fa218ac3faa435c2d",
    }
    assert report["bindings"]["training_order"]["effective_order_lock"] == expected_lock
    assert report["remediation_order"]["effective_order_lock"] == expected_lock

    guard = claim_recovery_output(
        output,
        recovery_packet_tree_sha256="sha256:" + "c" * 64,
        source_contract_sha256="sha256:" + "d" * 64,
        owner_id="11111111-1111-4111-8111-111111111111",
    )
    assert guard["training_steps_executed"] == 0
    with pytest.raises(
        ContinuationRecoveryError,
        match="refusing to overwrite or resume",
    ):
        claim_recovery_output(
            output,
            recovery_packet_tree_sha256="sha256:" + "c" * 64,
            source_contract_sha256="sha256:" + "d" * 64,
            owner_id="11111111-1111-4111-8111-111111111111",
        )
