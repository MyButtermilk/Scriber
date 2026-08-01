"""Identity-bound recovery of the completed 254-step HF continuation."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .curriculum import canonical_json_bytes
from .model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from .quantization import QuantizationError, inventory_bf16_checkpoint

_PREFIXED_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUIRED_SOURCE_FILES = frozenset(
    {
        "training/run-identity.json",
        "training/checkpoint-254/config.json",
        "training/checkpoint-254/generation_config.json",
        "training/checkpoint-254/model.safetensors",
        "training/checkpoint-254/scriber-protection-policy.json",
        "training/checkpoint-254/scriber-run-identity.json",
        "training/checkpoint-254/trainer_state.json",
        "training/checkpoint-254/training_args.bin",
    }
)
_CHECKPOINT_MODEL_FILES = (
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "scriber-protection-policy.json",
    "training_args.bin",
)
_TOKENIZER_FILES = (
    "added_tokens.json",
    "chat_template.jinja",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "tokenizer.model",
)
_PUBLISHED_MODEL_FILES = frozenset(
    {
        *_CHECKPOINT_MODEL_FILES,
        *_TOKENIZER_FILES,
        "reload-verification.json",
    }
)
ATTEMPT8_JOB_ID = "6a6b8a78b36a6516e96a2fb1"
ATTEMPT8_RUN_FINGERPRINT = "sha256:b632872e5577383893bebbc66b5d2235ac00912bb96040941758e0b3dbf10263"
ATTEMPT8_SOURCE_URI = "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt8/scriber-1100-continuation-v1"
RECOVERY_REMOTE_OUTPUT_BUCKET = "hf://buckets/Buttermilk03/scriber-polishing-private-runs"
RECOVERY_REMOTE_OUTPUT_PATH = "attempt9/scriber-1100-continuation-recovery-v1"
RECOVERY_TIMEOUT_MINUTES = 15
RECOVERY_FLAVOR = "a10g-small"
RECOVERY_IMAGE = "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
_ATTEMPT8_EVENT_PATH = (
    "training/runs/"
    "Jul30_17-34-44_j-buttermilk03-6a6b8a78b36a6516e96a2fb1-"
    "u56qauam-eb794-77964/"
    "events.out.tfevents.1785433597."
    "j-buttermilk03-6a6b8a78b36a6516e96a2fb1-u56qauam-eb794-77964.600.1"
)


class ContinuationRecoveryError(ValueError):
    """The failed continuation does not contain exact recoverable evidence."""


@dataclass(frozen=True)
class HfContinuationRecoveryPacket:
    output_dir: Path
    manifest: Mapping[str, object]


def attempt8_recovery_source_contract() -> dict[str, object]:
    """Return the reviewed immutable evidence for the completed failed job."""

    files = {
        "continuation-launch-guard.json": {
            "bytes": 498,
            "sha256": "sha256:32ecf48d1d2b4b962f6b996c7e73ace4456df82a4b6e23908733cc7cd51b83e6",
        },
        "continuation-manifest.json": {
            "bytes": 6440,
            "sha256": "sha256:155f2b49c35fdd13fc60c6e29769ec6d2606e7cddfd7edae52d87ff80146ab34",
        },
        "output-claim.json": {
            "bytes": 498,
            "sha256": "sha256:32ecf48d1d2b4b962f6b996c7e73ace4456df82a4b6e23908733cc7cd51b83e6",
        },
        "training/run-identity.json": {
            "bytes": 15750,
            "sha256": "sha256:e1282eb75be9380e0bedfb00465379fc6320f9ed6ef7089870d43a71ca7fc8c3",
        },
        "training/checkpoint-254/config.json": {
            "bytes": 1509,
            "sha256": "sha256:83c1e2b6544bcc6a03e1105c109bea141edbba8d60727e4b47bb96a72e5e8609",
        },
        "training/checkpoint-254/generation_config.json": {
            "bytes": 168,
            "sha256": "sha256:e452df866db1e3b43e75bcc5af31539e404e67c5bbc4e02a9a9c22357f1a70df",
        },
        "training/checkpoint-254/model.safetensors": {
            "bytes": 536223056,
            "sha256": "sha256:18f09290fffee124b7362eb3b7e57d9de8f9887212c88e4b900a7f0b0293d22c",
        },
        "training/checkpoint-254/scriber-protection-policy.json": {
            "bytes": 4361,
            "sha256": "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d",
        },
        "training/checkpoint-254/scriber-run-identity.json": {
            "bytes": 131,
            "sha256": "sha256:7d062f41b586435484ed7011448b659f2c6e3996337157967301c4cfc4542f91",
        },
        "training/checkpoint-254/trainer_state.json": {
            "bytes": 44712,
            "sha256": "sha256:d2d94f9e889f59ed83ade6e7528a7b7ab61f25ff0750f334a2579603b43e9b26",
        },
        "training/checkpoint-254/training_args.bin": {
            "bytes": 5969,
            "sha256": "sha256:bbd1d5801c86a57672989c4e7fff135fde89dc5bef5c515677d25ce7e35a1e57",
        },
        _ATTEMPT8_EVENT_PATH: {
            "bytes": 359,
            "sha256": "sha256:3b4f006c77c64416eed5ae3909c154a1b787ee5b8cfcf3eddec05d02fc94ca7c",
        },
    }
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_recovery_source",
        "source_remote_uri": ATTEMPT8_SOURCE_URI,
        "failed_job_id": ATTEMPT8_JOB_ID,
        "failed_job_status": "ERROR",
        "job_created_at": "2026-07-30T17:31:36Z",
        "job_started_at": "2026-07-30T17:31:44Z",
        "job_finished_at": "2026-07-30T17:46:56Z",
        "job_running_seconds": 912,
        "job_billed_minutes": 16,
        "job_hourly_rate_usd": "1.00",
        "job_cost_usd": "0.266667",
        "failure_stage": "publish_final_model.inventory_bf16_checkpoint",
        "failure_errno": 5,
        "run_fingerprint": ATTEMPT8_RUN_FINGERPRINT,
        "source_packet_tree_sha256": ("sha256:fbf4c0f0eaac705ec6539540be5380d6404c49dbfd36d476a9b4648ec1143bb2"),
        "source_continuation_manifest_sha256": (
            "sha256:155f2b49c35fdd13fc60c6e29769ec6d2606e7cddfd7edae52d87ff80146ab34"
        ),
        "source_owner_id": "99a06dd9-910f-40de-a50f-4f2374b1b02d",
        "completed_training_steps": 254,
        "cumulative_training_steps": 1100,
        "recovery_training_steps": 0,
        "train_metrics": {
            "train_runtime": 690.8581,
            "train_samples_per_second": 5.883,
            "train_steps_per_second": 0.368,
            "train_loss": 0.002224381026875147,
            "epoch": 1.0,
        },
        "completed_validation_event": {
            "path": _ATTEMPT8_EVENT_PATH,
            "step": 254,
            "eval_loss": 0.005138558801263571,
            "eval_runtime": 15.2225,
            "eval_samples_per_second": 28.313,
            "eval_steps_per_second": 14.189,
        },
        "files": files,
    }


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContinuationRecoveryError(f"{label} must be an object")
    return value


def _stable_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ContinuationRecoveryError(f"{label} must be a regular file: {path}")
    before = path.stat()
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ContinuationRecoveryError(f"cannot read {label}: {error}") from error
    after = path.stat()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ContinuationRecoveryError(f"{label} changed while it was read")
    return payload


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContinuationRecoveryError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ContinuationRecoveryError(f"{label} contains non-finite JSON value {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContinuationRecoveryError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ContinuationRecoveryError(f"{label} must contain an object")
    return value


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path, label: str) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise ContinuationRecoveryError(f"{label} must be a regular file: {path}")
    before = path.stat()
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise ContinuationRecoveryError(f"cannot hash {label}: {error}") from error
    after = path.stat()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ContinuationRecoveryError(f"{label} changed while it was hashed")
    return before.st_size, "sha256:" + digest.hexdigest()


def _write_new(path: Path, payload: bytes, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
    except FileExistsError as error:
        raise ContinuationRecoveryError(f"refusing to overwrite existing {label}: {path}") from error


def _exact_int(value: object, expected: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise ContinuationRecoveryError(f"{label} must equal {expected}")


def _verify_bound_files(
    root: Path,
    raw_files: object,
) -> dict[str, dict[str, object]]:
    files = _mapping(raw_files, "recovery source files")
    if not _REQUIRED_SOURCE_FILES.issubset(files):
        missing = sorted(_REQUIRED_SOURCE_FILES - set(files))
        raise ContinuationRecoveryError("recovery source contract is missing required files: " + ", ".join(missing))
    verified: dict[str, dict[str, object]] = {}
    for relative, raw_binding in sorted(files.items()):
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
        ):
            raise ContinuationRecoveryError(f"recovery source file path is unsafe: {relative!r}")
        binding = _mapping(raw_binding, f"recovery source file {relative}")
        if set(binding) != {"bytes", "sha256"}:
            raise ContinuationRecoveryError(f"recovery source file binding is not exact: {relative}")
        expected_bytes = binding.get("bytes")
        expected_sha256 = binding.get("sha256")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes <= 0
            or not isinstance(expected_sha256, str)
            or not _PREFIXED_SHA256.fullmatch(expected_sha256)
        ):
            raise ContinuationRecoveryError(f"recovery source file binding is invalid: {relative}")
        candidate = root / relative
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise ContinuationRecoveryError(f"recovery source file escapes its root: {relative}") from error
        actual_bytes, actual_sha256 = _sha256_file(
            resolved,
            f"recovery source file {relative}",
        )
        if actual_bytes != expected_bytes or actual_sha256 != expected_sha256:
            raise ContinuationRecoveryError(f"recovery source file differs from its immutable binding: {relative}")
        verified[relative] = {
            "bytes": expected_bytes,
            "sha256": expected_sha256,
        }
    return verified


def verify_completed_continuation_checkpoint(
    failed_run_root: str | Path,
    *,
    source_contract: Mapping[str, object],
) -> dict[str, object]:
    """Prove that checkpoint-254 is the exact completed run without retraining."""

    candidate = Path(failed_run_root).expanduser()
    if candidate.is_symlink():
        raise ContinuationRecoveryError(f"failed continuation root must not be a symlink: {candidate}")
    root = candidate.resolve()
    if not root.is_dir():
        raise ContinuationRecoveryError(f"failed continuation root must be a directory: {root}")
    contract = dict(source_contract)
    required_contract_keys = {
        "schema_version",
        "kind",
        "failed_job_id",
        "failed_job_status",
        "failure_stage",
        "failure_errno",
        "run_fingerprint",
        "completed_training_steps",
        "recovery_training_steps",
        "files",
    }
    if not required_contract_keys.issubset(contract):
        raise ContinuationRecoveryError("recovery source contract is missing required fields")
    if (
        contract.get("schema_version") != 1
        or contract.get("kind") != "scriber_hf_training_continuation_recovery_source"
        or contract.get("failed_job_status") != "ERROR"
        or contract.get("failure_stage") != "publish_final_model.inventory_bf16_checkpoint"
        or contract.get("failure_errno") != 5
    ):
        raise ContinuationRecoveryError("recovery source contract does not bind the publish-only I/O failure")
    _exact_int(
        contract.get("completed_training_steps"),
        254,
        "completed training steps",
    )
    _exact_int(
        contract.get("recovery_training_steps"),
        0,
        "recovery training steps",
    )
    expected_fingerprint = contract.get("run_fingerprint")
    if not isinstance(expected_fingerprint, str) or not _PREFIXED_SHA256.fullmatch(expected_fingerprint):
        raise ContinuationRecoveryError("recovery source run fingerprint is invalid")
    verified_files = _verify_bound_files(root, contract.get("files"))

    run_identity_payload = _stable_bytes(
        root / "training/run-identity.json",
        "continuation run identity",
    )
    run_identity = _json_object(
        run_identity_payload,
        "continuation run identity",
    )
    if set(run_identity) != {
        "schema_version",
        "fingerprint_algorithm",
        "bindings",
        "run_fingerprint",
    }:
        raise ContinuationRecoveryError("continuation run identity has unexpected fields")
    identity_core = {
        "schema_version": run_identity.get("schema_version"),
        "fingerprint_algorithm": run_identity.get("fingerprint_algorithm"),
        "bindings": run_identity.get("bindings"),
    }
    actual_fingerprint = _sha256(canonical_json_bytes(identity_core))
    if (
        run_identity.get("schema_version") != 1
        or run_identity.get("fingerprint_algorithm") != "sha256-canonical-json-v1"
        or run_identity.get("run_fingerprint") != expected_fingerprint
        or actual_fingerprint != expected_fingerprint
    ):
        raise ContinuationRecoveryError("continuation run identity fingerprint is inconsistent")
    bindings = _mapping(
        run_identity.get("bindings"),
        "continuation run bindings",
    )
    effective_training = _mapping(
        bindings.get("effective_training"),
        "continuation effective training",
    )
    schedule = _mapping(
        effective_training.get("schedule"),
        "continuation effective schedule",
    )
    if effective_training.get("run_kind") != "improvement" or schedule.get("max_steps") != 254:
        raise ContinuationRecoveryError("continuation run identity does not bind the 254-step improvement")

    checkpoint_root = root / "training/checkpoint-254"
    checkpoint_identity = _json_object(
        _stable_bytes(
            checkpoint_root / "scriber-run-identity.json",
            "checkpoint-254 identity",
        ),
        "checkpoint-254 identity",
    )
    if checkpoint_identity != {
        "schema_version": 1,
        "run_fingerprint": expected_fingerprint,
        "global_step": 254,
    }:
        raise ContinuationRecoveryError("checkpoint-254 identity differs from the completed run")
    trainer_state = _json_object(
        _stable_bytes(
            checkpoint_root / "trainer_state.json",
            "checkpoint-254 trainer state",
        ),
        "checkpoint-254 trainer state",
    )
    _exact_int(trainer_state.get("global_step"), 254, "global_step")
    _exact_int(trainer_state.get("max_steps"), 254, "max_steps")
    epoch = trainer_state.get("epoch")
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int | float)
        or not math.isfinite(float(epoch))
        or float(epoch) != 1.0
    ):
        raise ContinuationRecoveryError("epoch must equal 1.0")
    for field, expected in (
        ("logging_steps", 1),
        ("save_steps", 25),
        ("eval_steps", 25),
        ("num_train_epochs", 1),
        ("train_batch_size", 2),
    ):
        _exact_int(
            trainer_state.get(field),
            expected,
            f"trainer state {field}",
        )
    history = trainer_state.get("log_history")
    if not isinstance(history, list):
        raise ContinuationRecoveryError("checkpoint-254 trainer state has no log history")
    observed_training_steps: list[int] = []
    for raw_entry in history:
        if not isinstance(raw_entry, Mapping):
            raise ContinuationRecoveryError("checkpoint-254 log history contains a non-object")
        if "loss" not in raw_entry or "eval_loss" in raw_entry:
            continue
        step = raw_entry.get("step")
        if isinstance(step, bool) or not isinstance(step, int):
            raise ContinuationRecoveryError("checkpoint-254 training log contains an invalid step")
        for field in ("loss", "grad_norm", "learning_rate", "epoch"):
            value = raw_entry.get(field)
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
                raise ContinuationRecoveryError(f"checkpoint-254 training log has invalid {field}")
        observed_training_steps.append(step)
    if observed_training_steps != list(range(1, 255)):
        raise ContinuationRecoveryError(
            "checkpoint-254 trainer state does not prove exactly one log for every step 1..254"
        )
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_checkpoint_recovery_verification",
        "status": "complete",
        "failed_job_id": contract["failed_job_id"],
        "failure_stage": contract["failure_stage"],
        "checkpoint": "checkpoint-254",
        "run_fingerprint": expected_fingerprint,
        "completed_training_steps": 254,
        "recovery_training_steps": 0,
        "logged_training_steps": 254,
        "trainer_state_sha256": verified_files["training/checkpoint-254/trainer_state.json"]["sha256"],
        "model_sha256": verified_files["training/checkpoint-254/model.safetensors"]["sha256"],
        "source_file_count": len(verified_files),
        "source_files": verified_files,
        "bindings": bindings,
    }


def _load_parent_inventory(
    parent: Path,
    *,
    expected_tree_sha256: str,
) -> dict[str, dict[str, object]]:
    inventory_payload = _stable_bytes(
        parent / "checkpoint-inventory.json",
        "accepted-parent checkpoint inventory",
    )
    inventory = _json_object(
        inventory_payload,
        "accepted-parent checkpoint inventory",
    )
    if (
        not isinstance(expected_tree_sha256, str)
        or not _PREFIXED_SHA256.fullmatch(expected_tree_sha256)
        or inventory.get("tree_sha256") != expected_tree_sha256
    ):
        raise ContinuationRecoveryError("accepted-parent model tree differs from the recovery binding")
    raw_files = inventory.get("files")
    if not isinstance(raw_files, list):
        raise ContinuationRecoveryError("accepted-parent checkpoint inventory has no files")
    files: dict[str, dict[str, object]] = {}
    for raw in raw_files:
        binding = _mapping(raw, "accepted-parent file binding")
        relative = binding.get("path")
        size = binding.get("bytes")
        digest = binding.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or relative in files
            or "/" in relative
            or "\\" in relative
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(digest, str)
            or not _PREFIXED_SHA256.fullmatch(digest)
        ):
            raise ContinuationRecoveryError("accepted-parent checkpoint inventory contains an invalid file")
        actual_size, actual_digest = _sha256_file(
            parent / relative,
            f"accepted-parent file {relative}",
        )
        if actual_size != size or actual_digest != digest:
            raise ContinuationRecoveryError(f"accepted-parent file differs from its inventory: {relative}")
        files[relative] = {
            "bytes": size,
            "sha256": digest,
        }
    digest = hashlib.sha256()
    for relative, binding in sorted(files.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(binding["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(binding["sha256"]).encode("ascii"))
        digest.update(b"\n")
    calculated_tree_sha256 = "sha256:" + digest.hexdigest()
    if (
        set(files) != _PUBLISHED_MODEL_FILES
        or inventory.get("file_count") != len(files)
        or inventory.get("total_bytes") != sum(int(binding["bytes"]) for binding in files.values())
        or calculated_tree_sha256 != expected_tree_sha256
    ):
        raise ContinuationRecoveryError("accepted-parent inventory tree/counts differ from its bound files")
    return files


def materialize_recovered_final_model(
    failed_run_root: str | Path,
    *,
    accepted_parent_root: str | Path,
    staging_parent: str | Path,
    source_verification: Mapping[str, object],
    expected_parent_model_tree_sha256: str,
    reload_verify: Callable[[Path], Mapping[str, object]],
) -> dict[str, object]:
    """Build the exact final model from checkpoint-254 with zero training."""

    if (
        source_verification.get("status") != "complete"
        or source_verification.get("checkpoint") != "checkpoint-254"
        or source_verification.get("completed_training_steps") != 254
        or source_verification.get("recovery_training_steps") != 0
    ):
        raise ContinuationRecoveryError("final-model recovery requires a completed checkpoint-254 verification")
    fingerprint = source_verification.get("run_fingerprint")
    if not isinstance(fingerprint, str) or not _PREFIXED_SHA256.fullmatch(fingerprint):
        raise ContinuationRecoveryError("final-model recovery run fingerprint is invalid")
    source_candidate = Path(failed_run_root).expanduser()
    parent_candidate = Path(accepted_parent_root).expanduser()
    for candidate, label in (
        (source_candidate, "failed continuation"),
        (parent_candidate, "accepted parent"),
    ):
        if candidate.is_symlink():
            raise ContinuationRecoveryError(f"{label} root must not be a symlink: {candidate}")
    source = source_candidate.resolve()
    parent = parent_candidate.resolve()
    if not source.is_dir() or not parent.is_dir():
        raise ContinuationRecoveryError("failed continuation and accepted parent must be directories")
    parent_files = _load_parent_inventory(
        parent,
        expected_tree_sha256=expected_parent_model_tree_sha256,
    )
    if not set(_TOKENIZER_FILES).issubset(parent_files):
        raise ContinuationRecoveryError("accepted-parent inventory lacks the exact tokenizer files")
    checkpoint = source / "training/checkpoint-254"
    for name in ("config.json", "generation_config.json", "scriber-protection-policy.json"):
        actual_size, actual_digest = _sha256_file(
            checkpoint / name,
            f"checkpoint-254 {name}",
        )
        parent_binding = parent_files.get(name)
        if parent_binding != {
            "bytes": actual_size,
            "sha256": actual_digest,
        }:
            raise ContinuationRecoveryError(f"checkpoint-254 {name} differs from the accepted immutable input")

    staging_candidate = Path(staging_parent).expanduser()
    if staging_candidate.is_symlink():
        raise ContinuationRecoveryError(f"recovery staging parent must not be a symlink: {staging_candidate}")
    staging = staging_candidate.resolve()
    if staging.exists():
        if not staging.is_dir() or any(staging.iterdir()):
            raise ContinuationRecoveryError(f"recovery staging parent must be a new empty directory: {staging}")
    else:
        staging.mkdir(parents=True)
    final_name = "final-" + fingerprint.removeprefix("sha256:")[:16]
    final_root = staging / final_name
    try:
        final_root.mkdir()
    except FileExistsError as error:
        raise ContinuationRecoveryError(f"refusing to overwrite recovered final model: {final_root}") from error

    for name in _CHECKPOINT_MODEL_FILES:
        source_file = checkpoint / name
        if source_file.is_symlink() or not source_file.is_file():
            raise ContinuationRecoveryError(f"checkpoint-254 recovery file is missing: {name}")
        shutil.copyfile(source_file, final_root / name)
    for name in _TOKENIZER_FILES:
        source_file = parent / name
        if source_file.is_symlink() or not source_file.is_file():
            raise ContinuationRecoveryError(f"accepted-parent tokenizer file is missing: {name}")
        shutil.copyfile(source_file, final_root / name)

    try:
        reload_report = dict(reload_verify(final_root))
    except Exception as error:
        raise ContinuationRecoveryError(f"recovered final model fresh reload failed: {error}") from error
    if reload_report.get("status") != "passed":
        raise ContinuationRecoveryError("recovered final model fresh reload did not pass")
    reload_evidence = {
        **reload_report,
        "schema_version": 1,
        "run_fingerprint": fingerprint,
        "status": "passed",
    }
    _write_new(
        final_root / "reload-verification.json",
        canonical_json_bytes(reload_evidence),
        "reload verification",
    )
    try:
        inventory = inventory_bf16_checkpoint(
            final_root,
            source_id=f"training-run:{fingerprint}",
        )
    except QuantizationError as error:
        raise ContinuationRecoveryError(f"recovered final model inventory failed: {error}") from error
    _write_new(
        final_root / "checkpoint-inventory.json",
        canonical_json_bytes(inventory),
        "checkpoint inventory",
    )
    return {
        "directory": final_name,
        "path": str(final_root),
        "run_fingerprint": fingerprint,
        "inventory": inventory,
        "reload_verification": reload_evidence,
        "recovery_training_steps": 0,
        "source_checkpoint": "checkpoint-254",
    }


def _finite_metrics(
    raw_metrics: Mapping[str, object],
    *,
    required: frozenset[str],
    label: str,
) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for key, raw_value in raw_metrics.items():
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float) or not math.isfinite(float(raw_value)):
            raise ContinuationRecoveryError(f"{label} contains invalid metric {key}")
        metrics[str(key)] = float(raw_value)
    if not required.issubset(metrics):
        missing = sorted(required - set(metrics))
        raise ContinuationRecoveryError(f"{label} lacks required metrics: {', '.join(missing)}")
    return metrics


def build_recovered_training_report(
    failed_run_root: str | Path,
    *,
    output_root: str | Path,
    source_contract: Mapping[str, object],
    source_verification: Mapping[str, object],
    final_model: Mapping[str, object],
    recovery_eval_metrics: Mapping[str, object],
) -> dict[str, object]:
    """Reconstruct the schema-v2 report from immutable completed evidence."""

    if (
        source_verification.get("completed_training_steps") != 254
        or source_verification.get("recovery_training_steps") != 0
        or final_model.get("recovery_training_steps") != 0
        or final_model.get("source_checkpoint") != "checkpoint-254"
    ):
        raise ContinuationRecoveryError("training report recovery requires exact 254-step, zero-training evidence")
    root = Path(failed_run_root).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    run_identity_payload = _stable_bytes(
        root / "training/run-identity.json",
        "continuation run identity",
    )
    run_identity = _json_object(
        run_identity_payload,
        "continuation run identity",
    )
    fingerprint = source_verification.get("run_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or run_identity.get("run_fingerprint") != fingerprint
        or final_model.get("run_fingerprint") != fingerprint
    ):
        raise ContinuationRecoveryError("recovered report run fingerprint is inconsistent")
    raw_bindings = _mapping(
        run_identity.get("bindings"),
        "continuation bindings",
    )
    from .hf_training_continuation import (
        TrainingContinuationError,
        normalize_continuation_training_order,
    )

    try:
        normalized_order = normalize_continuation_training_order(
            _mapping(
                raw_bindings.get("training_order"),
                "continuation training order",
            ),
            require_actual_epoch_evidence=True,
        )
    except TrainingContinuationError as error:
        raise ContinuationRecoveryError(str(error)) from error
    bindings = copy.deepcopy(raw_bindings)
    bindings["training_order"] = normalized_order
    effective = _mapping(
        bindings.get("effective_training"),
        "continuation effective training",
    )
    runtime = _mapping(bindings.get("runtime"), "continuation runtime")
    checkpoint_state = _json_object(
        _stable_bytes(
            root / "training/checkpoint-254/trainer_state.json",
            "checkpoint-254 trainer state",
        ),
        "checkpoint-254 trainer state",
    )
    checkpoint_identity_payload = _stable_bytes(
        root / "training/checkpoint-254/scriber-run-identity.json",
        "checkpoint-254 identity",
    )
    trainer_state_payload = _stable_bytes(
        root / "training/checkpoint-254/trainer_state.json",
        "checkpoint-254 trainer state",
    )
    train_metrics = _finite_metrics(
        _mapping(source_contract.get("train_metrics"), "source train metrics"),
        required=frozenset(
            {
                "train_runtime",
                "train_samples_per_second",
                "train_steps_per_second",
                "train_loss",
                "epoch",
            }
        ),
        label="source train metrics",
    )
    eval_metrics = _finite_metrics(
        recovery_eval_metrics,
        required=frozenset(
            {
                "eval_loss",
                "eval_runtime",
                "eval_samples_per_second",
                "eval_steps_per_second",
            }
        ),
        label="recovery eval metrics",
    )
    final_name = final_model.get("directory")
    if not isinstance(final_name, str) or final_name != "final-" + fingerprint.removeprefix("sha256:")[:16]:
        raise ContinuationRecoveryError("recovered final-model directory differs from the run fingerprint")
    report_final_model = {
        "directory": final_name,
        "path": str(output / "training" / final_name),
        "run_fingerprint": fingerprint,
        "inventory": _mapping(
            final_model.get("inventory"),
            "recovered final-model inventory",
        ),
        "reload_verification": _mapping(
            final_model.get("reload_verification"),
            "recovered final-model reload verification",
        ),
    }
    report = {
        "schema_version": 2,
        "base_model": BASE_MODEL_ID,
        "base_revision": BASE_MODEL_REVISION,
        "run_kind": effective.get("run_kind"),
        "run_fingerprint": fingerprint,
        "run_identity": {
            "path": str(output / "training/run-identity.json"),
            "sha256": _sha256(run_identity_payload),
        },
        "bindings": bindings,
        "gpu": runtime.get("gpu"),
        "torch_version": runtime.get("torch"),
        "cuda_runtime": runtime.get("cuda_runtime"),
        "bf16": effective.get("bf16"),
        "sdpa": effective.get("sdpa"),
        "seed": effective.get("seed"),
        "method": effective.get("method"),
        "optimizer": effective.get("optimizer"),
        "learning_rate": effective.get("learning_rate"),
        "max_sequence_length": effective.get("max_sequence_length"),
        "micro_batch_size": effective.get("micro_batch_size"),
        "gradient_accumulation_steps": effective.get("gradient_accumulation_steps"),
        "effective_batch_size": effective.get("effective_batch_size"),
        "max_steps": 254,
        "schedule": effective.get("schedule"),
        "early_stopping": False,
        "save_total_limit": effective.get("save_total_limit"),
        "resume_from_checkpoint": None,
        "warm_start_parent_checkpoint": bindings.get("parent_checkpoint"),
        "protection_policy": bindings.get("protection_policy"),
        "protection_application": bindings.get("protection_application"),
        "train_examples_loaded": effective.get("train_examples_loaded"),
        "train_examples_used": effective.get("train_examples_used"),
        "validation_examples_loaded": effective.get("validation_examples_loaded"),
        "validation_examples_used": effective.get("validation_examples_used"),
        "oversize_rejections": {"train": 0, "validation": 0},
        "protection_rejections": {"train": 0, "validation": 0},
        "train_metrics": train_metrics,
        "eval_metrics": eval_metrics,
        "checkpoint_state": {
            "global_step": 254,
            "epoch": checkpoint_state.get("epoch"),
            "best_metric": checkpoint_state.get("best_metric"),
            "best_model_checkpoint": checkpoint_state.get("best_model_checkpoint"),
            "last_checkpoint": "checkpoint-254",
            "surviving_checkpoints": [
                {
                    "checkpoint": "checkpoint-254",
                    "global_step": 254,
                    "run_fingerprint": fingerprint,
                    "identity_sha256": _sha256(checkpoint_identity_payload),
                    "trainer_state_path": str(root / "training/checkpoint-254/trainer_state.json"),
                    "trainer_state_sha256": _sha256(trainer_state_payload),
                }
            ],
        },
        "remediation_order": bindings.get("training_order"),
        "final_model": report_final_model,
        "recovery": {
            "schema_version": 1,
            "kind": "scriber_hf_training_continuation_no_retrain_recovery",
            "failed_job_id": source_contract.get("failed_job_id"),
            "failure_stage": source_contract.get("failure_stage"),
            "failure_errno": source_contract.get("failure_errno"),
            "source_checkpoint": "checkpoint-254",
            "source_model_sha256": source_verification.get("model_sha256"),
            "completed_training_steps": 254,
            "training_steps_executed": 0,
            "validation_reexecuted": True,
            "original_completed_validation_event": source_contract.get("completed_validation_event"),
        },
        "training_completed": True,
    }
    return report


def claim_recovery_output(
    output_root: str | Path,
    *,
    recovery_packet_tree_sha256: str,
    source_contract_sha256: str,
    owner_id: str,
) -> dict[str, object]:
    """Exclusively claim a new recovery prefix; existing output always fails."""

    for value, label in (
        (recovery_packet_tree_sha256, "recovery packet tree"),
        (source_contract_sha256, "source contract"),
    ):
        if not isinstance(value, str) or not _PREFIXED_SHA256.fullmatch(value):
            raise ContinuationRecoveryError(f"{label} SHA-256 must be sha256:<64 lowercase hex>")
    if not isinstance(owner_id, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        owner_id,
    ):
        raise ContinuationRecoveryError("recovery output owner ID must be a UUIDv4")
    candidate = Path(output_root).expanduser()
    if candidate.is_symlink():
        raise ContinuationRecoveryError(f"recovery output must not be a symlink: {candidate}")
    root = candidate.resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        root.mkdir()
    except FileExistsError as error:
        raise ContinuationRecoveryError(
            f"refusing to overwrite or resume an existing recovery output: {root}"
        ) from error
    guard = {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_recovery_launch_guard",
        "status": "active",
        "output_root": str(root),
        "owner_id": owner_id,
        "recovery_packet_tree_sha256": recovery_packet_tree_sha256,
        "source_contract_sha256": source_contract_sha256,
        "failed_job_id": ATTEMPT8_JOB_ID,
        "run_fingerprint": ATTEMPT8_RUN_FINGERPRINT,
        "completed_training_steps": 254,
        "training_steps_executed": 0,
        "overwrite_policy": "forbidden_new_prefix_required",
    }
    _write_new(
        root / "recovery-launch-guard.json",
        canonical_json_bytes(guard),
        "recovery launch guard",
    )
    return guard


def _copy_new_file(source: Path, destination: Path, label: str) -> None:
    if source.is_symlink() or not source.is_file():
        raise ContinuationRecoveryError(f"{label} source must be a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
            shutil.copyfileobj(
                source_stream,
                destination_stream,
                length=1024 * 1024,
            )
            destination_stream.flush()
    except FileExistsError as error:
        raise ContinuationRecoveryError(f"refusing to overwrite existing {label}: {destination}") from error
    source_size, source_sha256 = _sha256_file(source, f"{label} source")
    destination_size, destination_sha256 = _sha256_file(
        destination,
        f"{label} destination",
    )
    if source_size != destination_size or source_sha256 != destination_sha256:
        raise ContinuationRecoveryError(f"{label} destination differs after copy")


def publish_recovered_output(
    local_final_root: str | Path,
    failed_run_root: str | Path,
    *,
    output_root: str | Path,
    training_report: Mapping[str, object],
    source_contract: Mapping[str, object],
    source_verification: Mapping[str, object],
) -> dict[str, object]:
    """Publish verified local recovery bytes once into an already claimed root."""

    local_final = Path(local_final_root).expanduser().resolve()
    source = Path(failed_run_root).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    guard_path = output / "recovery-launch-guard.json"
    if output.is_symlink() or not output.is_dir() or not guard_path.is_file():
        raise ContinuationRecoveryError("recovery output must be exclusively claimed before publication")
    if (
        local_final.is_symlink()
        or not local_final.is_dir()
        or local_final.name != training_report.get("final_model", {}).get("directory")
    ):
        raise ContinuationRecoveryError("local recovered final model differs from the training report")
    destination_final = output / "training" / local_final.name
    try:
        destination_final.mkdir(parents=True)
    except FileExistsError as error:
        raise ContinuationRecoveryError("refusing to overwrite an existing recovered final model") from error
    local_paths = sorted(
        (path for path in local_final.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(local_final).as_posix(),
    )
    for path in local_paths:
        relative = path.relative_to(local_final)
        _copy_new_file(
            path,
            destination_final / relative,
            f"recovered final-model file {relative.as_posix()}",
        )
    _copy_new_file(
        source / "training/run-identity.json",
        output / "training/run-identity.json",
        "recovered run identity",
    )
    _write_new(
        output / "recovery-source-contract.json",
        canonical_json_bytes(dict(source_contract)),
        "recovery source contract",
    )
    _write_new(
        output / "checkpoint254-verification.json",
        canonical_json_bytes(dict(source_verification)),
        "checkpoint-254 verification",
    )
    _write_new(
        output / "training-report.json",
        canonical_json_bytes(dict(training_report)),
        "recovered training report",
    )
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_recovery_publication",
        "status": "published_once",
        "output_root": str(output),
        "final_model_directory": local_final.name,
        "training_steps_executed": 0,
        "file_count": len(local_paths),
        "training_report_sha256": _sha256(canonical_json_bytes(dict(training_report))),
    }


def recovery_runner_payload() -> bytes:
    """Return the exact no-training recovery runner."""

    script = r"""#!/usr/bin/env bash
set -euo pipefail

export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

if [ "$#" -ne 4 ] || \
   [ "$1" != "--expected-packet-tree-sha256" ] || \
   [ "$3" != "--expected-source-contract-sha256" ]; then
  echo "exact recovery packet and source-contract hashes are required" >&2
  exit 1
fi
EXPECTED_PACKET_TREE_SHA256="$2"
EXPECTED_SOURCE_CONTRACT_SHA256="$4"
if [[ ! "$EXPECTED_PACKET_TREE_SHA256" =~ ^sha256:[0-9a-f]{64}$ ]] || \
   [[ ! "$EXPECTED_SOURCE_CONTRACT_SHA256" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "recovery hash arguments are invalid" >&2
  exit 1
fi

PACKET_ROOT=/input/repo
SOURCE_REPO="$PACKET_ROOT/repo"
FAILED_RUN_ROOT=/failed-run
REMOTE_PARENT_ROOT=/accepted-parent
WORKSPACE_ROOT=/workspace/scriber-1100-recovery-source
WORK_ROOT="$WORKSPACE_ROOT/repo"
PROJECT_ROOT="$WORK_ROOT/ml/scriber_polishing"
RECOVERY_WORKSPACE=/workspace/scriber-1100-recovery-work
OUTPUT_ROOT=/outputs/attempt9/scriber-1100-continuation-recovery-v1

for required in "$SOURCE_REPO" "$FAILED_RUN_ROOT" "$REMOTE_PARENT_ROOT"; do
  if [ ! -d "$required" ]; then
    echo "required read-only recovery mount is missing: $required" >&2
    exit 1
  fi
done
if [ -e "$WORKSPACE_ROOT" ] || [ -e "$RECOVERY_WORKSPACE" ]; then
  echo "refusing a non-empty local recovery workspace" >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends git
fi

python -m pip install --break-system-packages --no-cache-dir \
  accelerate==1.14.0 \
  jsonschema==4.26.0 \
  numpy==2.5.1 \
  pyyaml==6.0.3 \
  safetensors==0.8.0 \
  sentencepiece==0.2.2 \
  tensorboard==2.21.0 \
  transformers==4.57.6

python "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_training_continuation_recovery.py" \
  --packet-root "$PACKET_ROOT" \
  --expected-packet-tree-sha256 "$EXPECTED_PACKET_TREE_SHA256" \
  --expected-source-contract-sha256 "$EXPECTED_SOURCE_CONTRACT_SHA256" \
  --report /workspace/scriber-1100-recovery-packet-verification.json

mkdir -p "$WORKSPACE_ROOT"
cp -a --no-preserve=ownership "$SOURCE_REPO" "$WORK_ROOT"
cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT/src"

python scripts/recover_hf_training_continuation.py \
  --failed-run "$FAILED_RUN_ROOT" \
  --accepted-parent "$REMOTE_PARENT_ROOT" \
  --bundle-dir job-input \
  --workspace "$RECOVERY_WORKSPACE" \
  --output-root "$OUTPUT_ROOT" \
  --expected-packet-tree-sha256 "$EXPECTED_PACKET_TREE_SHA256" \
  --expected-source-contract-sha256 "$EXPECTED_SOURCE_CONTRACT_SHA256"

echo "cloud_training_continuation_recovery=complete training_steps_executed=0" >&2
"""
    return script.replace("\r\n", "\n").encode("utf-8")


def _recovery_budget() -> dict[str, object]:
    prior = Decimal("0.860")
    maximum_job = Decimal(RECOVERY_TIMEOUT_MINUTES) / Decimal(60)
    maximum_total = prior + maximum_job
    return {
        "total_budget_usd": 5.0,
        "prior_cost_usd": float(prior),
        "hourly_rate_usd": 1.0,
        "timeout_minutes": RECOVERY_TIMEOUT_MINUTES,
        "maximum_job_cost_usd": float(maximum_job),
        "maximum_total_cost_usd": float(maximum_total),
        "remaining_after_reservation_usd": float(Decimal("5.00") - maximum_total),
    }


def _recovery_packet_inventory(
    root: Path,
    *,
    excluded_paths: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, object]], str]:
    from .hf_training_continuation import _packet_inventory

    return _packet_inventory(root, excluded_paths=excluded_paths)


def build_hf_continuation_recovery_packet(
    *,
    output_dir: str | Path,
    continuation_bundle_dir: str | Path,
) -> HfContinuationRecoveryPacket:
    """Build one immutable no-training recovery packet."""

    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise ContinuationRecoveryError(f"refusing to overwrite HF recovery packet: {target}")
    bundle_candidate = Path(continuation_bundle_dir).expanduser()
    if bundle_candidate.is_symlink():
        raise ContinuationRecoveryError(f"continuation bundle must not be a symlink: {bundle_candidate}")
    bundle = bundle_candidate.resolve()
    if not bundle.is_dir():
        raise ContinuationRecoveryError(f"continuation bundle is missing: {bundle}")
    validation_payload = _stable_bytes(
        bundle / "safe-validation.jsonl",
        "safe validation",
    )
    if (
        len(validation_payload.splitlines()) != 431
        or hashlib.sha256(validation_payload).hexdigest()
        != "b4840b2d06af923af4f28643b32e5de50deef3c6995ebdcb92f89a6fd3859a86"
    ):
        raise ContinuationRecoveryError("recovery packet safe validation differs from the exact split")
    policy_payload = _stable_bytes(
        bundle / "training-policy.json",
        "training protection policy",
    )
    if hashlib.sha256(policy_payload).hexdigest() != (
        "8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
    ):
        raise ContinuationRecoveryError("recovery packet protection policy differs from acceptance")
    contract = attempt8_recovery_source_contract()
    contract_payload = canonical_json_bytes(contract)
    contract_sha256 = _sha256(contract_payload)
    from .hf_training_continuation import (
        _copy_project_tree,
        _source_git_head,
        build_hf_cost_audit,
    )

    cost_payload = canonical_json_bytes(build_hf_cost_audit())
    budget = _recovery_budget()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.tmp-",
            dir=target.parent,
        )
    )
    try:
        project = temp_root / "repo/ml/scriber_polishing"
        project.mkdir(parents=True)
        _copy_project_tree(
            Path(__file__).resolve().parent,
            project / "src/scriber_polishing",
        )
        _copy_project_tree(
            Path(__file__).resolve().parents[2] / "contracts",
            project / "contracts",
        )
        scripts = project / "scripts"
        scripts.mkdir()
        for name in (
            "recover_hf_training_continuation.py",
            "verify_hf_training_continuation_recovery.py",
        ):
            shutil.copy2(
                Path(__file__).resolve().parents[2] / "scripts" / name,
                scripts / name,
            )
        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(
                Path(__file__).resolve().parents[2] / name,
                project / name,
            )
        job_input = project / "job-input"
        job_input.mkdir()
        (job_input / "safe-validation.jsonl").write_bytes(validation_payload)
        (job_input / "training-policy.json").write_bytes(policy_payload)
        (job_input / "recovery-source-contract.json").write_bytes(contract_payload)
        (job_input / "hf-cost-audit.json").write_bytes(cost_payload)
        (temp_root / "run-continuation-recovery.sh").write_bytes(recovery_runner_payload())
        inventory, packet_tree_sha256 = _recovery_packet_inventory(temp_root)
        manifest = {
            "schema_version": 1,
            "kind": "scriber_hf_training_continuation_recovery_job_packet",
            "source_git_head": _source_git_head(),
            "image": RECOVERY_IMAGE,
            "flavor": RECOVERY_FLAVOR,
            "timeout_minutes": RECOVERY_TIMEOUT_MINUTES,
            "budget": budget,
            "source_contract_sha256": contract_sha256,
            "source_job_id": ATTEMPT8_JOB_ID,
            "source_run_fingerprint": ATTEMPT8_RUN_FINGERPRINT,
            "source_remote_uri": ATTEMPT8_SOURCE_URI,
            "source_mount_mode": "read_only",
            "parent_mount_mode": "read_only",
            "output_root": "/outputs/" + RECOVERY_REMOTE_OUTPUT_PATH,
            "remote_result_uri": (RECOVERY_REMOTE_OUTPUT_BUCKET.rstrip("/") + "/" + RECOVERY_REMOTE_OUTPUT_PATH),
            "completed_training_steps": 254,
            "cumulative_training_steps": 1100,
            "training_steps_executed": 0,
            "validation_examples": 431,
            "validation_reexecuted": True,
            "optimizer_state_loaded": False,
            "publication": False,
            "quantization": False,
            "packet_tree_sha256": packet_tree_sha256,
            "inventory_excludes": ["job-manifest.json"],
            "file_count": len(inventory),
            "byte_count": sum(int(item["bytes"]) for item in inventory),
        }
        (temp_root / "job-manifest.json").write_bytes(canonical_json_bytes(manifest))
        temp_root.replace(target)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    return HfContinuationRecoveryPacket(
        output_dir=target,
        manifest=manifest,
    )


def verify_hf_continuation_recovery_packet(
    packet_dir: str | Path,
    *,
    expected_packet_tree_sha256: str,
    expected_source_contract_sha256: str,
) -> dict[str, object]:
    """Verify every recovery packet byte against two external hashes."""

    for value, label in (
        (expected_packet_tree_sha256, "expected packet tree"),
        (expected_source_contract_sha256, "expected source contract"),
    ):
        if not isinstance(value, str) or not _PREFIXED_SHA256.fullmatch(value):
            raise ContinuationRecoveryError(f"{label} must be sha256:<64 lowercase hex>")
    candidate = Path(packet_dir).expanduser()
    if candidate.is_symlink():
        raise ContinuationRecoveryError(f"recovery packet must not be a symlink: {candidate}")
    root = candidate.resolve()
    if not root.is_dir():
        raise ContinuationRecoveryError(f"recovery packet is missing: {root}")
    manifest_payload = _stable_bytes(
        root / "job-manifest.json",
        "recovery job manifest",
    )
    manifest = _json_object(
        manifest_payload,
        "recovery job manifest",
    )
    if manifest_payload != canonical_json_bytes(manifest):
        raise ContinuationRecoveryError("recovery job manifest is not canonical JSON")
    inventory, actual_tree_sha256 = _recovery_packet_inventory(
        root,
        excluded_paths=frozenset({"job-manifest.json"}),
    )
    budget = _recovery_budget()
    exact = {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_recovery_job_packet",
        "image": RECOVERY_IMAGE,
        "flavor": RECOVERY_FLAVOR,
        "timeout_minutes": RECOVERY_TIMEOUT_MINUTES,
        "budget": budget,
        "source_contract_sha256": expected_source_contract_sha256,
        "source_job_id": ATTEMPT8_JOB_ID,
        "source_run_fingerprint": ATTEMPT8_RUN_FINGERPRINT,
        "source_remote_uri": ATTEMPT8_SOURCE_URI,
        "source_mount_mode": "read_only",
        "parent_mount_mode": "read_only",
        "output_root": "/outputs/" + RECOVERY_REMOTE_OUTPUT_PATH,
        "remote_result_uri": (RECOVERY_REMOTE_OUTPUT_BUCKET.rstrip("/") + "/" + RECOVERY_REMOTE_OUTPUT_PATH),
        "completed_training_steps": 254,
        "cumulative_training_steps": 1100,
        "training_steps_executed": 0,
        "validation_examples": 431,
        "validation_reexecuted": True,
        "optimizer_state_loaded": False,
        "publication": False,
        "quantization": False,
        "packet_tree_sha256": expected_packet_tree_sha256,
        "inventory_excludes": ["job-manifest.json"],
        "file_count": len(inventory),
        "byte_count": sum(int(item["bytes"]) for item in inventory),
    }
    source_git_head = manifest.get("source_git_head")
    if not isinstance(source_git_head, str) or not re.fullmatch(r"[0-9a-f]{40}", source_git_head):
        raise ContinuationRecoveryError("recovery job manifest source Git revision is invalid")
    without_git = dict(manifest)
    without_git.pop("source_git_head", None)
    if without_git != exact or actual_tree_sha256 != expected_packet_tree_sha256:
        raise ContinuationRecoveryError("recovery packet differs from its externally bound launch contract")
    contract_payload = _stable_bytes(
        root / "repo/ml/scriber_polishing/job-input/recovery-source-contract.json",
        "packet recovery source contract",
    )
    if _sha256(contract_payload) != expected_source_contract_sha256 or contract_payload != canonical_json_bytes(
        attempt8_recovery_source_contract()
    ):
        raise ContinuationRecoveryError("packet recovery source contract differs from reviewed evidence")
    validation_payload = _stable_bytes(
        root / "repo/ml/scriber_polishing/job-input/safe-validation.jsonl",
        "packet safe validation",
    )
    policy_payload = _stable_bytes(
        root / "repo/ml/scriber_polishing/job-input/training-policy.json",
        "packet protection policy",
    )
    if (
        hashlib.sha256(validation_payload).hexdigest()
        != "b4840b2d06af923af4f28643b32e5de50deef3c6995ebdcb92f89a6fd3859a86"
        or len(validation_payload.splitlines()) != 431
        or hashlib.sha256(policy_payload).hexdigest()
        != "8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
    ):
        raise ContinuationRecoveryError("recovery packet evaluation inputs differ from acceptance")
    runner = _stable_bytes(
        root / "run-continuation-recovery.sh",
        "recovery runner",
    )
    if runner != recovery_runner_payload():
        raise ContinuationRecoveryError("recovery runner differs from the reviewed no-training runner")
    forbidden = (
        b"train_sft.py",
        b".train(",
        b"optimizer.pt",
        b"checkpoint-250",
    )
    if any(token in runner for token in forbidden):
        raise ContinuationRecoveryError("recovery runner contains a forbidden training/resume path")
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_recovery_packet_verification",
        "status": "ready",
        "packet_tree_sha256": actual_tree_sha256,
        "source_contract_sha256": expected_source_contract_sha256,
        "file_count": len(inventory),
        "byte_count": sum(int(item["bytes"]) for item in inventory),
        "training_steps_executed": 0,
        "optimizer_state_loaded": False,
        "credential_material_present": False,
    }


def build_hf_continuation_recovery_launch_contract(
    packet_dir: str | Path,
) -> dict[str, object]:
    """Build the exact four-volume CLI without launching it."""

    packet = Path(packet_dir).expanduser().resolve()
    manifest = _json_object(
        _stable_bytes(
            packet / "job-manifest.json",
            "recovery job manifest",
        ),
        "recovery job manifest",
    )
    packet_tree = manifest.get("packet_tree_sha256")
    source_contract = manifest.get("source_contract_sha256")
    if not isinstance(packet_tree, str) or not isinstance(
        source_contract,
        str,
    ):
        raise ContinuationRecoveryError("recovery job manifest lacks external hashes")
    verify_hf_continuation_recovery_packet(
        packet,
        expected_packet_tree_sha256=packet_tree,
        expected_source_contract_sha256=source_contract,
    )
    packet_volume = f"{packet}:/input/repo:ro"
    source_volume = f"{ATTEMPT8_SOURCE_URI}:/failed-run:ro"
    from .hf_training_continuation import CONTINUATION_REMOTE_PARENT_URI

    parent_volume = f"{CONTINUATION_REMOTE_PARENT_URI}:/accepted-parent:ro"
    output_volume = f"{RECOVERY_REMOTE_OUTPUT_BUCKET}:/outputs:rw"
    argv = [
        "hf",
        "jobs",
        "run",
        "--flavor",
        RECOVERY_FLAVOR,
        "--timeout",
        f"{RECOVERY_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=continuation-1100-recovery",
        "-v",
        packet_volume,
        "-v",
        source_volume,
        "-v",
        parent_volume,
        "-v",
        output_volume,
        RECOVERY_IMAGE,
        "bash",
        "/input/repo/run-continuation-recovery.sh",
        "--expected-packet-tree-sha256",
        packet_tree,
        "--expected-source-contract-sha256",
        source_contract,
    ]
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_recovery_launch_contract",
        "flavor": RECOVERY_FLAVOR,
        "timeout": f"{RECOVERY_TIMEOUT_MINUTES}m",
        "image": RECOVERY_IMAGE,
        "packet_volume": packet_volume,
        "source_volume": source_volume,
        "parent_volume": parent_volume,
        "output_volume": output_volume,
        "output_root": "/outputs/" + RECOVERY_REMOTE_OUTPUT_PATH,
        "remote_result_uri": (RECOVERY_REMOTE_OUTPUT_BUCKET.rstrip("/") + "/" + RECOVERY_REMOTE_OUTPUT_PATH),
        "expected_packet_tree_sha256": packet_tree,
        "expected_source_contract_sha256": source_contract,
        "training_steps_executed": 0,
        "argv": argv,
        "external_job_started": False,
    }
