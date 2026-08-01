"""Recover the completed HF continuation from checkpoint-254 without training."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
import uuid
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.curriculum import canonical_json_bytes  # noqa: E402
from scriber_polishing.hf_training_continuation import (  # noqa: E402
    build_continuation_postflight_result,
    normalize_continuation_training_order,
)
from scriber_polishing.hf_training_continuation_recovery import (  # noqa: E402
    ATTEMPT8_RUN_FINGERPRINT,
    ContinuationRecoveryError,
    attempt8_recovery_source_contract,
    build_recovered_training_report,
    claim_recovery_output,
    materialize_recovered_final_model,
    publish_recovered_output,
    verify_completed_continuation_checkpoint,
)
from scriber_polishing.protected_spans import (  # noqa: E402
    validate_protection_policy_evidence,
)
from scriber_polishing.tokenize import tokenize_example  # noqa: E402
from scriber_polishing.training_integrity import (  # noqa: E402
    fresh_reload_local_model,
)
from scriber_polishing.training_runtime import (  # noqa: E402
    CompletionCollator,
    apply_training_protection,
)

_PREFIXED_SHA256 = "sha256:"


class _TokenizedDataset(Dataset):
    def __init__(self, records: list[dict[str, list[int]]]) -> None:
        self.records = records

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index: int) -> dict[str, list[int]]:
        return self.records[index]


def _sha256(payload: bytes) -> str:
    return _PREFIXED_SHA256 + hashlib.sha256(payload).hexdigest()


def _write_new_json(path: Path, value: object) -> None:
    payload = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()


def _copy_file_with_retry(
    source: Path,
    destination: Path,
    *,
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
    attempts: int = 3,
) -> None:
    if destination.exists():
        raise ContinuationRecoveryError(f"refusing to overwrite local recovery snapshot: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
                while block := source_stream.read(1024 * 1024):
                    destination_stream.write(block)
                    digest.update(block)
                    size += len(block)
                destination_stream.flush()
            actual_sha256 = _PREFIXED_SHA256 + digest.hexdigest()
            if expected_bytes is not None and size != expected_bytes:
                raise ContinuationRecoveryError(f"copied source size differs for {source}")
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise ContinuationRecoveryError(f"copied source SHA-256 differs for {source}")
            return
        except (OSError, ContinuationRecoveryError) as error:
            last_error = error
            if destination.exists():
                destination.unlink()
            if attempt < attempts:
                time.sleep(float(attempt))
    raise ContinuationRecoveryError(
        f"could not copy immutable recovery source after {attempts} attempts: {source}: {last_error}"
    )


def _snapshot_failed_run(
    remote_root: Path,
    local_root: Path,
    contract: dict[str, object],
) -> None:
    if local_root.exists():
        raise ContinuationRecoveryError(f"local failed-run snapshot already exists: {local_root}")
    files = contract.get("files")
    if not isinstance(files, dict):
        raise ContinuationRecoveryError("recovery source contract has no files")
    local_root.mkdir(parents=True)
    for relative, raw_binding in sorted(files.items()):
        if not isinstance(relative, str) or not isinstance(raw_binding, dict):
            raise ContinuationRecoveryError("recovery source contract contains an invalid file")
        _copy_file_with_retry(
            remote_root / relative,
            local_root / relative,
            expected_bytes=int(raw_binding["bytes"]),
            expected_sha256=str(raw_binding["sha256"]),
        )


def _snapshot_parent(remote_root: Path, local_root: Path) -> None:
    if local_root.exists():
        raise ContinuationRecoveryError(f"local accepted-parent snapshot already exists: {local_root}")
    local_root.mkdir(parents=True)
    paths = sorted(
        remote_root.rglob("*"),
        key=lambda path: path.relative_to(remote_root).as_posix(),
    )
    for path in paths:
        if path.is_symlink():
            raise ContinuationRecoveryError(f"accepted-parent mount contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ContinuationRecoveryError(f"accepted-parent mount contains a non-file: {path}")
        relative = path.relative_to(remote_root)
        _copy_file_with_retry(
            path,
            local_root / relative,
        )


def _load_validation(
    path: Path,
) -> list[dict[str, object]]:
    payload = path.read_bytes()
    if (
        len(payload.splitlines()) != 431
        or hashlib.sha256(payload).hexdigest() != "b4840b2d06af923af4f28643b32e5de50deef3c6995ebdcb92f89a6fd3859a86"
    ):
        raise ContinuationRecoveryError("recovery validation bytes differ from the exact safe split")
    rows: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        try:
            row = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContinuationRecoveryError(f"safe validation row {line_number} is invalid JSON") from error
        if not isinstance(row, dict) or not all(
            isinstance(row.get(field), str) and row.get(field) for field in ("example_id", "source_text", "target_sst")
        ):
            raise ContinuationRecoveryError(f"safe validation row {line_number} is invalid")
        rows.append(row)
    return rows


def evaluate_recovered_model(
    final_root: Path,
    *,
    validation_path: Path,
    policy_path: Path,
    evaluation_output: Path,
) -> dict[str, float]:
    """Re-run only the exact 431-case safe validation; never train."""

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise ContinuationRecoveryError("recovery evaluation requires CUDA BF16")
    seed = 270014
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(True, warn_only=True)
    torch.backends.cuda.matmul.allow_tf32 = False
    tokenizer = AutoTokenizer.from_pretrained(
        final_root,
        local_files_only=True,
        trust_remote_code=False,
        fix_mistral_regex=False,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    policy_raw = json.loads(policy_path.read_bytes())
    if not isinstance(policy_raw, dict):
        raise ContinuationRecoveryError("recovery protection policy is not an object")
    policy = validate_protection_policy_evidence(
        policy_raw,
        tokenizer=tokenizer,
    )
    rows = _load_validation(validation_path)
    protected = apply_training_protection(rows, policy=policy)
    if len(protected.records) != 431 or protected.rejected_example_ids:
        raise ContinuationRecoveryError("recovery validation protection changed the exact 431 cases")
    encoded: list[dict[str, list[int]]] = []
    for row in protected.records:
        item = tokenize_example(
            tokenizer,
            transcript=str(row["source_text"]),
            completion=str(row["target_sst"]),
        )
        if len(item["input_ids"]) > 768:
            raise ContinuationRecoveryError("recovery validation contains an oversize sequence")
        encoded.append(item)
    model = AutoModelForCausalLM.from_pretrained(
        final_root,
        local_files_only=True,
        trust_remote_code=False,
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
        low_cpu_mem_usage=True,
    )
    model.config.use_cache = False
    model.eval()
    arguments = TrainingArguments(
        output_dir=str(evaluation_output),
        do_train=False,
        do_eval=True,
        per_device_eval_batch_size=2,
        bf16=True,
        seed=seed,
        data_seed=seed,
        full_determinism=True,
        remove_unused_columns=False,
        dataloader_num_workers=0,
        report_to=[],
        use_cpu=False,
    )
    evaluator = Trainer(
        model=model,
        args=arguments,
        eval_dataset=_TokenizedDataset(encoded),
        data_collator=CompletionCollator(tokenizer),
    )
    raw_metrics = evaluator.evaluate()
    metrics = {
        str(key): float(value)
        for key, value in raw_metrics.items()
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(float(value))
    }
    for key in (
        "eval_loss",
        "eval_runtime",
        "eval_samples_per_second",
        "eval_steps_per_second",
    ):
        if key not in metrics:
            raise ContinuationRecoveryError(f"recovery validation lacks {key}")
    return metrics


def _input_binding(
    verification: dict[str, object],
    contract: dict[str, object],
) -> dict[str, object]:
    bindings = verification.get("bindings")
    if not isinstance(bindings, dict):
        raise ContinuationRecoveryError("checkpoint verification has no run bindings")
    config = bindings.get("config")
    dataset = bindings.get("dataset")
    order = bindings.get("training_order")
    policy = bindings.get("protection_policy")
    tokenizer = bindings.get("tokenizer")
    parent = bindings.get("parent_checkpoint")
    for value, label in (
        (config, "config"),
        (dataset, "dataset"),
        (order, "training order"),
        (policy, "protection policy"),
        (tokenizer, "tokenizer"),
        (parent, "parent checkpoint"),
    ):
        if not isinstance(value, dict):
            raise ContinuationRecoveryError(f"run binding {label} is missing")
    manifest = dataset.get("manifest")
    train = dataset.get("train")
    validation = dataset.get("validation")
    normalized_order = normalize_continuation_training_order(
        order,
        require_actual_epoch_evidence=True,
    )
    order_lock = normalized_order.get("effective_order_lock")
    if not all(isinstance(value, dict) for value in (manifest, train, validation, order_lock)):
        raise ContinuationRecoveryError("run dataset/order bindings are incomplete")
    return {
        "config_sha256": config["sha256"],
        "dataset_manifest_sha256": manifest["sha256"],
        "train_sha256": train["sha256"],
        "validation_sha256": validation["sha256"],
        "order_plan_sha256": (_PREFIXED_SHA256 + str(order["plan_sha256"])),
        "effective_order_sha256": order_lock["example_ids_sha256"],
        "protection_policy_artifact_sha256": policy["sha256"],
        "protection_policy_sha256": policy["policy_sha256"],
        "tokenizer_identity_sha256": tokenizer["identity_sha256"],
        "parent_directory_tree_sha256": parent["tree_sha256"],
        "base_model": "google/gemma-3-270m-it",
        "base_revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        "continuation_manifest_sha256": contract["source_continuation_manifest_sha256"],
        "hf_flavor": "a10g-small",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failed-run", type=Path, required=True)
    parser.add_argument("--accepted-parent", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--expected-packet-tree-sha256",
        required=True,
    )
    parser.add_argument(
        "--expected-source-contract-sha256",
        required=True,
    )
    parser.add_argument("--owner-id")
    args = parser.parse_args(argv)

    contract = attempt8_recovery_source_contract()
    contract_payload = canonical_json_bytes(contract)
    if _sha256(contract_payload) != args.expected_source_contract_sha256:
        raise ContinuationRecoveryError("recovery source contract differs from the external SHA-256")
    workspace = args.workspace.expanduser().resolve()
    if workspace.exists():
        raise ContinuationRecoveryError(f"recovery workspace must not exist: {workspace}")
    workspace.mkdir(parents=True)
    local_source = workspace / "failed-run"
    local_parent = workspace / "accepted-parent"
    _snapshot_failed_run(
        args.failed_run.expanduser().resolve(),
        local_source,
        contract,
    )
    _snapshot_parent(
        args.accepted_parent.expanduser().resolve(),
        local_parent,
    )
    verification = verify_completed_continuation_checkpoint(
        local_source,
        source_contract=contract,
    )
    final_model = materialize_recovered_final_model(
        local_source,
        accepted_parent_root=local_parent,
        staging_parent=workspace / "final-staging",
        source_verification=verification,
        expected_parent_model_tree_sha256=("sha256:4b4b758f2fa3e00af282845c42de2887bc714c36a5c8e58e7225a1d9a7d25e06"),
        reload_verify=fresh_reload_local_model,
    )
    final_root = Path(str(final_model["path"]))
    eval_metrics = evaluate_recovered_model(
        final_root,
        validation_path=args.bundle_dir / "safe-validation.jsonl",
        policy_path=args.bundle_dir / "training-policy.json",
        evaluation_output=workspace / "evaluation",
    )
    output_root = args.output_root.expanduser().resolve()
    report = build_recovered_training_report(
        local_source,
        output_root=output_root,
        source_contract=contract,
        source_verification=verification,
        final_model=final_model,
        recovery_eval_metrics=eval_metrics,
    )
    owner_id = args.owner_id or str(uuid.uuid4())
    claim_recovery_output(
        output_root,
        recovery_packet_tree_sha256=args.expected_packet_tree_sha256,
        source_contract_sha256=args.expected_source_contract_sha256,
        owner_id=owner_id,
    )
    publication = publish_recovered_output(
        final_root,
        local_source,
        output_root=output_root,
        training_report=report,
        source_contract=contract,
        source_verification=verification,
    )
    input_binding = _input_binding(verification, contract)
    postflight: dict[str, object] | None = None
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            postflight = build_continuation_postflight_result(
                training_report_path=output_root / "training-report.json",
                output_root=output_root,
                input_binding=input_binding,
                expected_flavor="a10g-small",
            )
            break
        except ContinuationRecoveryError:
            raise
        except Exception as error:
            last_error = error
            if attempt < 3:
                time.sleep(float(attempt))
    if postflight is None:
        raise ContinuationRecoveryError(f"recovery postflight failed after three read attempts: {last_error}")
    result = {
        **postflight,
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_recovery_result",
        "status": "complete",
        "failed_job_id": contract["failed_job_id"],
        "run_fingerprint": ATTEMPT8_RUN_FINGERPRINT,
        "completed_training_steps": 254,
        "cumulative_training_steps": 1100,
        "training_steps_executed": 0,
        "validation_reexecuted": True,
        "recovery_eval_metrics": eval_metrics,
        "publication": publication,
    }
    _write_new_json(
        output_root / "continuation-result.json",
        result,
    )
    print(
        "continuation_recovery=complete "
        "completed_training_steps=254 "
        "cumulative_training_steps=1100 "
        "training_steps_executed=0",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
