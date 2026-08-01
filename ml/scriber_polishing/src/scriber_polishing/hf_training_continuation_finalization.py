"""Finalization-only publication of the completed Attempt-9 recovery."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

_PREFIXED_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
ATTEMPT9_JOB_ID = "6a6b94e6b36a6516e96a307c"
ATTEMPT9_RUN_FINGERPRINT = "sha256:b632872e5577383893bebbc66b5d2235ac00912bb96040941758e0b3dbf10263"
ATTEMPT9_SOURCE_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt9/scriber-1100-continuation-recovery-v1"
)
ATTEMPT9_FINAL_MODEL_DIRECTORY = "final-b632872e55773838"
ATTEMPT9_FINAL_MODEL_URI = f"{ATTEMPT9_SOURCE_URI}/training/{ATTEMPT9_FINAL_MODEL_DIRECTORY}"
ATTEMPT9_FINAL_MODEL_TREE_SHA256 = "sha256:c6404ccdef04434ae9457e93b9dbec0697901a224df9ec15e350385354d9ba69"
ATTEMPT9_PACKET_TREE_SHA256 = "sha256:8f37fcfdf1ee7b542c2f078eee7dd57b2f9ea5a2573d265890d5c0d7d6ae31ea"
ATTEMPT8_SOURCE_CONTRACT_SHA256 = "sha256:defb5da36b22995347e15289c720443e3708bba9ac8315654e1930253f443960"
FINALIZATION_REMOTE_OUTPUT_BUCKET = "hf://buckets/Buttermilk03/scriber-polishing-private-runs"
FINALIZATION_REMOTE_OUTPUT_PATH = "attempt10/scriber-1100-continuation-finalization-v1"
FINALIZATION_FLAVOR = "cpu-basic"
FINALIZATION_TIMEOUT_MINUTES = 10
FINALIZATION_IMAGE = "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
_EFFECTIVE_ORDER_SHA256 = "b8a03f8cd657f755c06934aae4169dda4386c8bc31d3eb6fa218ac3faa435c2d"
_FILTERED_ORDER_SHA256 = "625bfc2b54d7403dd41532925795f8e081da7522a45346262a22ba2a9c2fbef3"
_SOURCE_TRAINING_REPORT_SHA256 = "sha256:337315c67c8a75441e4655e532cff33b152c8165be8651de3576351111b7757e"
_SOURCE_RUN_IDENTITY_SHA256 = "sha256:e1282eb75be9380e0bedfb00465379fc6320f9ed6ef7089870d43a71ca7fc8c3"


class ContinuationFinalizationError(ValueError):
    """Attempt 9 cannot be finalized from exact, immutable evidence."""


@dataclass(frozen=True)
class HfContinuationFinalizationPacket:
    output_dir: Path
    manifest: Mapping[str, object]


def canonical_json_bytes(value: object) -> bytes:
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


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContinuationFinalizationError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ContinuationFinalizationError(f"{label} must be an object")
    return value


def _stable_bytes(path: Path, label: str, *, attempts: int = 3) -> bytes:
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            if path.is_symlink() or not path.is_file():
                raise ContinuationFinalizationError(f"{label} must be a regular file: {path}")
            before = path.stat()
            payload = path.read_bytes()
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
                raise ContinuationFinalizationError(f"{label} changed while it was read")
            return payload
        except OSError as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(float(attempt + 1))
    raise ContinuationFinalizationError(f"cannot read {label} after {attempts} attempts: {last_error}")


def _hash_file(path: Path, label: str, *, attempts: int = 3) -> tuple[int, str]:
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            if path.is_symlink() or not path.is_file():
                raise ContinuationFinalizationError(f"{label} must be a regular file: {path}")
            before = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
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
                raise ContinuationFinalizationError(f"{label} changed while it was hashed")
            return after.st_size, "sha256:" + digest.hexdigest()
        except OSError as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(float(attempt + 1))
    raise ContinuationFinalizationError(f"cannot hash {label} after {attempts} attempts: {last_error}")


def _final_model_inventory() -> dict[str, object]:
    files = [
        {
            "bytes": 35,
            "path": "added_tokens.json",
            "sha256": "sha256:50b2f405ba56a26d4913fd772089992252d7f942123cc0a034d96424221ba946",
        },
        {
            "bytes": 1532,
            "path": "chat_template.jinja",
            "sha256": "sha256:7de1c58e208eda46e9c7f86397df37ec49883aeece39fb961e0a6b24088dd3c4",
        },
        {
            "bytes": 1509,
            "path": "config.json",
            "sha256": "sha256:83c1e2b6544bcc6a03e1105c109bea141edbba8d60727e4b47bb96a72e5e8609",
        },
        {
            "bytes": 168,
            "path": "generation_config.json",
            "sha256": "sha256:e452df866db1e3b43e75bcc5af31539e404e67c5bbc4e02a9a9c22357f1a70df",
        },
        {
            "bytes": 536223056,
            "path": "model.safetensors",
            "sha256": "sha256:18f09290fffee124b7362eb3b7e57d9de8f9887212c88e4b900a7f0b0293d22c",
        },
        {
            "bytes": 406,
            "path": "reload-verification.json",
            "sha256": "sha256:3ba3d14a76bed0306ef6cc171c563d214decc8c63b6c7df195d44bea74a1d654",
        },
        {
            "bytes": 4361,
            "path": "scriber-protection-policy.json",
            "sha256": "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d",
        },
        {
            "bytes": 662,
            "path": "special_tokens_map.json",
            "sha256": "sha256:2f7b0adf4fb469770bb1490e3e35df87b1dc578246c5e7e6fc76ecf33213a397",
        },
        {
            "bytes": 33384568,
            "path": "tokenizer.json",
            "sha256": "sha256:4667f2089529e8e7657cfb6d1c19910ae71ff5f28aa7ab2ff2763330affad795",
        },
        {
            "bytes": 4689074,
            "path": "tokenizer.model",
            "sha256": "sha256:1299c11d7cf632ef3b4e11937501358ada021bbdf7c47638d13c0ee982f2e79c",
        },
        {
            "bytes": 1155404,
            "path": "tokenizer_config.json",
            "sha256": "sha256:c54055555aa322627af18187adffe5f6b9298fecb8b4a5837a5e1de1895dd693",
        },
        {
            "bytes": 5969,
            "path": "training_args.bin",
            "sha256": "sha256:bbd1d5801c86a57672989c4e7fff135fde89dc5bef5c515677d25ce7e35a1e57",
        },
    ]
    return {
        "schema_version": 1,
        "source_id": f"training-run:{ATTEMPT9_RUN_FINGERPRINT}",
        "format": "transformers_safetensors",
        "variant": "bf16",
        "model_type": "gemma3_text",
        "declared_dtype": "bfloat16",
        "weight_dtype_counts": {"BF16": 236},
        "tree_sha256": ATTEMPT9_FINAL_MODEL_TREE_SHA256,
        "file_count": 12,
        "total_bytes": 575466744,
        "files": files,
    }


def attempt9_finalization_source_contract() -> dict[str, object]:
    """Exact reviewed contract for the completed Attempt-9 artifacts."""

    files = {
        "checkpoint254-verification.json": {
            "bytes": 18052,
            "sha256": "sha256:3dd9aded45c597bce10d88508ed8fba6af55e0da1be93d349376595b44d57bc3",
        },
        "recovery-launch-guard.json": {
            "bytes": 673,
            "sha256": "sha256:c0c22acc37e9946a0ae2601053d2fa81b83a9ee0ab936b1958a9d2a9ce897c68",
        },
        "recovery-source-contract.json": {
            "bytes": 3360,
            "sha256": ATTEMPT8_SOURCE_CONTRACT_SHA256,
        },
        "training-report.json": {
            "bytes": 29083,
            "sha256": _SOURCE_TRAINING_REPORT_SHA256,
        },
        "training/run-identity.json": {
            "bytes": 15750,
            "sha256": _SOURCE_RUN_IDENTITY_SHA256,
        },
        f"training/{ATTEMPT9_FINAL_MODEL_DIRECTORY}/checkpoint-inventory.json": {
            "bytes": 1937,
            "sha256": "sha256:dbed8235d155814f4261dd99ad60ad851cc25ca037adc805cb1aed3cf8410bc2",
        },
        f"training/{ATTEMPT9_FINAL_MODEL_DIRECTORY}/reload-verification.json": {
            "bytes": 406,
            "sha256": "sha256:3ba3d14a76bed0306ef6cc171c563d214decc8c63b6c7df195d44bea74a1d654",
        },
    }
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_finalization_source",
        "source_remote_uri": ATTEMPT9_SOURCE_URI,
        "source_job_id": ATTEMPT9_JOB_ID,
        "source_job_status": "ERROR",
        "source_job_created_at": "2026-07-30T18:16:06Z",
        "source_job_started_at": "2026-07-30T18:19:20Z",
        "source_job_finished_at": "2026-07-30T18:20:53Z",
        "source_job_running_seconds": 93,
        "source_job_billed_minutes": 2,
        "source_job_hourly_rate_usd": "1.00",
        "source_job_cost_usd": "0.033333",
        "source_packet_tree_sha256": ATTEMPT9_PACKET_TREE_SHA256,
        "source_recovery_contract_sha256": ATTEMPT8_SOURCE_CONTRACT_SHA256,
        "run_fingerprint": ATTEMPT9_RUN_FINGERPRINT,
        "completed_training_steps": 254,
        "cumulative_training_steps": 1100,
        "training_steps_executed": 0,
        "failure_stage": "finalization.input_binding",
        "validation": {
            "status": "passed",
            "expected_steps": 216,
            "completed_steps": 216,
            "validation_reexecuted_in_source_job": True,
            "eval_metrics": {
                "eval_loss": 0.005138558801263571,
                "eval_model_preparation_time": 0.004,
                "eval_runtime": 14.8378,
                "eval_samples_per_second": 29.047,
                "eval_steps_per_second": 14.557,
            },
            "log_evidence": {
                "job_id": ATTEMPT9_JOB_ID,
                "progress_final": "216/216",
                "source": "hf_jobs_logs",
            },
        },
        "final_model": {
            "directory": ATTEMPT9_FINAL_MODEL_DIRECTORY,
            "remote_uri": ATTEMPT9_FINAL_MODEL_URI,
            "tree_sha256": ATTEMPT9_FINAL_MODEL_TREE_SHA256,
            "file_count": 12,
            "total_bytes": 575466744,
            "model_sha256": ("sha256:18f09290fffee124b7362eb3b7e57d9de8f9887212c88e4b900a7f0b0293d22c"),
            "reload_status": "passed",
            "inventory": _final_model_inventory(),
        },
        "files": files,
    }


def _normalize_order(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ContinuationFinalizationError("training order must be an object")
    order = copy.deepcopy(dict(value))
    epochs = order.get("effective_epoch_orders")
    if not isinstance(epochs, list) or len(epochs) != 1:
        raise ContinuationFinalizationError("training order requires exactly one effective epoch")
    epoch = epochs[0]
    if (
        not isinstance(epoch, Mapping)
        or epoch.get("epoch") != 0
        or epoch.get("record_count") != 4064
        or epoch.get("filtered_plan_example_ids_sha256") != _FILTERED_ORDER_SHA256
        or epoch.get("effective_example_ids_sha256") != _EFFECTIVE_ORDER_SHA256
        or order.get("bucket_algorithm") != "fixed_window_length_descending_v1"
    ):
        raise ContinuationFinalizationError("training order effective epoch evidence is invalid")
    lock = {
        "algorithm": "fixed_window_length_descending_v1",
        "example_ids_sha256": _EFFECTIVE_ORDER_SHA256,
    }
    if order.get("effective_order_lock") not in (None, lock):
        raise ContinuationFinalizationError("training order lock differs from epoch evidence")
    order["effective_order_lock"] = lock
    return order


def normalize_attempt9_training_report(
    source_report: Mapping[str, object],
    *,
    run_identity: Mapping[str, object],
    source_training_report_sha256: str,
    source_job_id: str,
) -> dict[str, object]:
    """Add only the derived report lock while preserving run identity."""

    report = copy.deepcopy(dict(source_report))
    identity = copy.deepcopy(dict(run_identity))
    if _sha256(canonical_json_bytes(dict(source_report))) != (source_training_report_sha256):
        raise ContinuationFinalizationError("source training report SHA-256 is invalid")
    bindings = report.get("bindings")
    identity_bindings = identity.get("bindings")
    if not isinstance(bindings, dict) or not isinstance(identity_bindings, dict) or bindings != identity_bindings:
        raise ContinuationFinalizationError("source report bindings differ from run identity")
    identity_core = {
        "schema_version": 1,
        "fingerprint_algorithm": "sha256-canonical-json-v1",
        "bindings": identity_bindings,
    }
    fingerprint = _sha256(canonical_json_bytes(identity_core))
    expected_identity = {**identity_core, "run_fingerprint": fingerprint}
    run_binding = report.get("run_identity")
    if (
        identity != expected_identity
        or report.get("run_fingerprint") != fingerprint
        or not isinstance(run_binding, Mapping)
        or run_binding.get("sha256") != _sha256(canonical_json_bytes(identity))
    ):
        raise ContinuationFinalizationError("source run fingerprint or identity hash is invalid")
    raw_order = bindings.get("training_order")
    if report.get("remediation_order") != raw_order:
        raise ContinuationFinalizationError("source report order views differ")
    normalized = _normalize_order(raw_order)
    corrected_bindings = copy.deepcopy(bindings)
    corrected_bindings["training_order"] = normalized
    report["bindings"] = corrected_bindings
    report["remediation_order"] = copy.deepcopy(normalized)
    recovery = report.get("recovery")
    if (
        not isinstance(recovery, dict)
        or recovery.get("training_steps_executed") != 0
        or recovery.get("validation_reexecuted") is not True
    ):
        raise ContinuationFinalizationError("source recovery completion evidence is invalid")
    report["recovery_finalization"] = {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_report_finalization",
        "source_job_id": source_job_id,
        "source_training_report_sha256": source_training_report_sha256,
        "normalized_fields": [
            "bindings.training_order.effective_order_lock",
            "remediation_order.effective_order_lock",
        ],
        "run_fingerprint_preserved": True,
        "training_steps_executed": 0,
        "validation_reexecuted": False,
        "validation_reused_from_job_id": source_job_id,
    }
    return report


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


def verify_final_model_inventory(
    final_root: str | Path,
    *,
    inventory: Mapping[str, object],
) -> dict[str, object]:
    """Hash the already-materialized model in place; never copy it."""

    root = Path(final_root).expanduser().resolve()
    files = inventory.get("files")
    if not isinstance(files, list):
        raise ContinuationFinalizationError("final model inventory files must be a list")
    declared: list[dict[str, object]] = []
    for raw in files:
        if not isinstance(raw, Mapping):
            raise ContinuationFinalizationError("final model inventory entry must be an object")
        relative = raw.get("path")
        expected_bytes = raw.get("bytes")
        expected_sha = raw.get("sha256")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or "\\" in relative
            or ".." in Path(relative).parts
            or isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or not isinstance(expected_sha, str)
            or not _PREFIXED_SHA256.fullmatch(expected_sha)
        ):
            raise ContinuationFinalizationError("final model inventory entry is invalid")
        size, digest = _hash_file(
            root / relative,
            f"final model {relative}",
        )
        if size != expected_bytes or digest != expected_sha:
            raise ContinuationFinalizationError(f"final model {relative} differs from inventory")
        declared.append({"path": relative, "bytes": size, "sha256": digest})
    declared.sort(key=lambda item: str(item["path"]))
    actual_paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "checkpoint-inventory.json"
    )
    if actual_paths != [str(item["path"]) for item in declared]:
        raise ContinuationFinalizationError("final model contains undeclared or missing files")
    physical_inventory = _json_object(
        _stable_bytes(
            root / "checkpoint-inventory.json",
            "physical final model inventory",
        ),
        "physical final model inventory",
    )
    if physical_inventory != dict(inventory):
        raise ContinuationFinalizationError("physical final model inventory differs from report")
    total = sum(int(item["bytes"]) for item in declared)
    tree = _tree_sha256(declared)
    if (
        inventory.get("file_count") != len(declared)
        or inventory.get("total_bytes") != total
        or inventory.get("tree_sha256") != tree
    ):
        raise ContinuationFinalizationError("final model inventory totals or tree are invalid")
    return {
        "tree_sha256": tree,
        "file_count": len(declared),
        "total_bytes": total,
        "model_copied": False,
    }


def verify_attempt9_finalization_source(
    source_root: str | Path,
    *,
    source_contract: Mapping[str, object],
) -> dict[str, object]:
    """Verify exact Attempt-9 evidence and its complete final model."""

    contract = dict(source_contract)
    if contract != attempt9_finalization_source_contract():
        raise ContinuationFinalizationError("Attempt-9 source contract differs from reviewed evidence")
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ContinuationFinalizationError("Attempt-9 source must be a read-only directory")
    source_files = contract["files"]
    assert isinstance(source_files, dict)
    payloads: dict[str, bytes] = {}
    for relative, binding in source_files.items():
        assert isinstance(relative, str) and isinstance(binding, dict)
        payload = _stable_bytes(root / relative, f"Attempt-9 {relative}")
        if len(payload) != binding["bytes"] or _sha256(payload) != binding["sha256"]:
            raise ContinuationFinalizationError(f"Attempt-9 {relative} differs from source contract")
        payloads[relative] = payload
    report = _json_object(
        payloads["training-report.json"],
        "Attempt-9 training report",
    )
    identity = _json_object(
        payloads["training/run-identity.json"],
        "Attempt-9 run identity",
    )
    checkpoint = _json_object(
        payloads["checkpoint254-verification.json"],
        "Attempt-9 checkpoint verification",
    )
    guard = _json_object(
        payloads["recovery-launch-guard.json"],
        "Attempt-9 recovery launch guard",
    )
    if (
        checkpoint.get("status") != "complete"
        or checkpoint.get("completed_training_steps") != 254
        or checkpoint.get("recovery_training_steps") != 0
        or checkpoint.get("run_fingerprint") != ATTEMPT9_RUN_FINGERPRINT
        or guard.get("recovery_packet_tree_sha256") != ATTEMPT9_PACKET_TREE_SHA256
        or guard.get("source_contract_sha256") != ATTEMPT8_SOURCE_CONTRACT_SHA256
        or guard.get("training_steps_executed") != 0
    ):
        raise ContinuationFinalizationError("Attempt-9 checkpoint or launch evidence is invalid")
    recovery = report.get("recovery")
    final_model = report.get("final_model")
    validation = contract["validation"]
    assert isinstance(validation, dict)
    if (
        report.get("training_completed") is not True
        or report.get("max_steps") != 254
        or not isinstance(recovery, dict)
        or recovery.get("training_steps_executed") != 0
        or recovery.get("validation_reexecuted") is not True
        or report.get("eval_metrics") != validation["eval_metrics"]
        or not isinstance(final_model, dict)
        or final_model.get("directory") != ATTEMPT9_FINAL_MODEL_DIRECTORY
        or final_model.get("inventory") != _final_model_inventory()
        or final_model.get("reload_verification", {}).get("status") != "passed"
    ):
        raise ContinuationFinalizationError("Attempt-9 training, evaluation, or final model report is invalid")
    corrected = normalize_attempt9_training_report(
        report,
        run_identity=identity,
        source_training_report_sha256=_SOURCE_TRAINING_REPORT_SHA256,
        source_job_id=ATTEMPT9_JOB_ID,
    )
    model = verify_final_model_inventory(
        root / "training" / ATTEMPT9_FINAL_MODEL_DIRECTORY,
        inventory=_final_model_inventory(),
    )
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_finalization_source_verification",
        "status": "complete",
        "source_job_id": ATTEMPT9_JOB_ID,
        "run_fingerprint": ATTEMPT9_RUN_FINGERPRINT,
        "completed_training_steps": 254,
        "cumulative_training_steps": 1100,
        "training_steps_executed": 0,
        "validation_steps_verified": 216,
        "validation_reexecuted": False,
        "validation_reused_from_job_id": ATTEMPT9_JOB_ID,
        "final_model": model,
        "corrected_training_report": corrected,
    }


def _write_new_json(path: Path, value: object) -> str:
    payload = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
    except FileExistsError as error:
        raise ContinuationFinalizationError(f"refusing to overwrite finalization output: {path}") from error
    return _sha256(payload)


def claim_finalization_output(
    output_root: str | Path,
    *,
    source_root: str | Path,
    packet_tree_sha256: str,
    source_contract_sha256: str,
    owner_id: str,
) -> dict[str, object]:
    """Exclusively claim a new prefix distinct from Attempt 9."""

    output = Path(output_root).expanduser().resolve()
    source = Path(source_root).expanduser().resolve()
    if output == source or source in output.parents:
        raise ContinuationFinalizationError("finalization output must differ from its read-only source")
    for value in (packet_tree_sha256, source_contract_sha256):
        if not _PREFIXED_SHA256.fullmatch(value):
            raise ContinuationFinalizationError("finalization hashes must be sha256-prefixed")
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        owner_id,
    ):
        raise ContinuationFinalizationError("finalization owner must be a UUIDv4")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir()
    except FileExistsError as error:
        raise ContinuationFinalizationError(f"refusing to overwrite or resume finalization output: {output}") from error
    guard = {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_finalization_launch_guard",
        "status": "active",
        "owner_id": owner_id,
        "output_root": str(output),
        "source_remote_uri": ATTEMPT9_SOURCE_URI,
        "source_job_id": ATTEMPT9_JOB_ID,
        "packet_tree_sha256": packet_tree_sha256,
        "source_contract_sha256": source_contract_sha256,
        "training_steps_executed": 0,
        "validation_reexecuted": False,
        "model_copied": False,
        "overwrite_policy": "forbidden_new_prefix_required",
    }
    _write_new_json(output / "finalization-launch-guard.json", guard)
    return guard


def build_finalization_result(
    *,
    corrected_training_report_sha256: str,
    source_verification_sha256: str,
) -> dict[str, object]:
    for value in (
        corrected_training_report_sha256,
        source_verification_sha256,
    ):
        if not _PREFIXED_SHA256.fullmatch(value):
            raise ContinuationFinalizationError("finalization result hashes must be sha256-prefixed")
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_finalization_result",
        "status": "complete",
        "source_job_id": ATTEMPT9_JOB_ID,
        "source_remote_uri": ATTEMPT9_SOURCE_URI,
        "run_fingerprint": ATTEMPT9_RUN_FINGERPRINT,
        "start_updates": 846,
        "additional_updates": 254,
        "completed_training_steps": 254,
        "cumulative_training_steps": 1100,
        "target_updates": 1100,
        "training_steps_executed": 0,
        "validation_reexecuted": False,
        "validation_reused_from_job_id": ATTEMPT9_JOB_ID,
        "evaluation_executed": False,
        "model_copied": False,
        "optimizer_state_loaded": False,
        "remote_model_uri": ATTEMPT9_FINAL_MODEL_URI,
        "final_model_directory": ATTEMPT9_FINAL_MODEL_DIRECTORY,
        "final_model_tree_sha256": ATTEMPT9_FINAL_MODEL_TREE_SHA256,
        "final_model_file_count": 12,
        "final_model_total_bytes": 575466744,
        "corrected_training_report_sha256": (corrected_training_report_sha256),
        "source_verification_sha256": source_verification_sha256,
    }


def verify_completed_finalization_output(
    output_root: str | Path,
) -> dict[str, object]:
    """Fail closed unless the five-file Attempt-10 output is complete."""

    root = Path(output_root).expanduser().resolve()
    expected_names = {
        "attempt9-finalization-source-contract.json",
        "attempt9-finalization-verification.json",
        "continuation-result.json",
        "finalization-launch-guard.json",
        "training-report.json",
    }
    actual_names = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual_names != expected_names:
        raise ContinuationFinalizationError("completed finalization output must contain exactly five JSON files")
    values: dict[str, dict[str, Any]] = {}
    payloads: dict[str, bytes] = {}
    for name in sorted(expected_names):
        payload = _stable_bytes(root / name, f"finalization {name}")
        value = _json_object(payload, f"finalization {name}")
        if payload != canonical_json_bytes(value):
            raise ContinuationFinalizationError(f"finalization {name} is not canonical JSON")
        payloads[name] = payload
        values[name] = value
    contract = values["attempt9-finalization-source-contract.json"]
    if contract != attempt9_finalization_source_contract():
        raise ContinuationFinalizationError("completed finalization source contract is invalid")
    guard = values["finalization-launch-guard.json"]
    if (
        guard.get("kind") != "scriber_hf_training_continuation_finalization_launch_guard"
        or guard.get("status") != "active"
        or guard.get("source_job_id") != ATTEMPT9_JOB_ID
        or guard.get("source_contract_sha256") != _sha256(payloads["attempt9-finalization-source-contract.json"])
        or guard.get("training_steps_executed") != 0
        or guard.get("validation_reexecuted") is not False
        or guard.get("model_copied") is not False
    ):
        raise ContinuationFinalizationError("completed finalization launch guard is invalid")
    report = values["training-report.json"]
    bindings = report.get("bindings")
    remediation = report.get("remediation_order")
    if not isinstance(bindings, dict) or not isinstance(remediation, dict):
        raise ContinuationFinalizationError("corrected training report lacks order bindings")
    normalized = _normalize_order(remediation)
    bound_order = bindings.get("training_order")
    if bound_order != normalized or remediation != normalized:
        raise ContinuationFinalizationError("corrected training report order views are invalid")
    identity_bindings = copy.deepcopy(bindings)
    identity_order = identity_bindings["training_order"]
    assert isinstance(identity_order, dict)
    identity_order.pop("effective_order_lock", None)
    fingerprint = _sha256(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "fingerprint_algorithm": "sha256-canonical-json-v1",
                "bindings": identity_bindings,
            }
        )
    )
    if fingerprint != ATTEMPT9_RUN_FINGERPRINT:
        raise ContinuationFinalizationError("corrected training report changed the run fingerprint")
    verification = values["attempt9-finalization-verification.json"]
    if (
        verification.get("kind") != "scriber_hf_training_continuation_finalization_source_verification"
        or verification.get("status") != "complete"
        or verification.get("training_steps_executed") != 0
        or verification.get("validation_reexecuted") is not False
        or verification.get("final_model", {}).get("tree_sha256") != ATTEMPT9_FINAL_MODEL_TREE_SHA256
    ):
        raise ContinuationFinalizationError("completed finalization source verification is invalid")
    expected_result = build_finalization_result(
        corrected_training_report_sha256=_sha256(payloads["training-report.json"]),
        source_verification_sha256=_sha256(payloads["attempt9-finalization-verification.json"]),
    )
    result = values["continuation-result.json"]
    if result != expected_result:
        raise ContinuationFinalizationError("completed finalization result is invalid")
    return result


def finalization_budget() -> dict[str, object]:
    prior = Decimal("0.893")
    maximum_job = (Decimal("0.01") * Decimal(FINALIZATION_TIMEOUT_MINUTES) / Decimal(60)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    maximum_total = prior + maximum_job
    return {
        "total_budget_usd": 5.0,
        "prior_cost_usd": float(prior),
        "hourly_rate_usd": 0.01,
        "timeout_minutes": FINALIZATION_TIMEOUT_MINUTES,
        "maximum_job_cost_usd": float(maximum_job),
        "maximum_total_cost_usd": float(maximum_total),
        "remaining_after_reservation_usd": float(Decimal("5.00") - maximum_total),
    }


def finalization_runner_payload() -> bytes:
    script = r"""#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1
export PYTHONUNBUFFERED=1

if [ "$#" -ne 4 ] || \
   [ "$1" != "--expected-packet-tree-sha256" ] || \
   [ "$3" != "--expected-source-contract-sha256" ]; then
  echo "exact finalization packet and source hashes are required" >&2
  exit 1
fi
PACKET_ROOT=/input/repo
PROJECT_ROOT="$PACKET_ROOT/repo/ml/scriber_polishing"
SOURCE_ROOT=/recovered-run
OUTPUT_ROOT=/outputs/attempt10/scriber-1100-continuation-finalization-v1
export PYTHONPATH="$PROJECT_ROOT/src"

test -d "$SOURCE_ROOT"
python "$PROJECT_ROOT/scripts/verify_hf_training_continuation_finalization.py" \
  --packet-root "$PACKET_ROOT" \
  --expected-packet-tree-sha256 "$2" \
  --expected-source-contract-sha256 "$4" \
  --report /tmp/finalization-packet-verification.json
python "$PROJECT_ROOT/scripts/finalize_hf_training_continuation_recovery.py" \
  --source-root "$SOURCE_ROOT" \
  --output-root "$OUTPUT_ROOT" \
  --expected-packet-tree-sha256 "$2" \
  --expected-source-contract-sha256 "$4"
echo "continuation_finalization=complete training_steps_executed=0 validation_reexecuted=false" >&2
"""
    return script.replace("\r\n", "\n").encode("utf-8")


def _packet_inventory(
    root: Path,
    *,
    excluded: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, object]], str]:
    files: list[dict[str, object]] = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        size, digest = _hash_file(path, f"packet {relative}")
        files.append({"path": relative, "bytes": size, "sha256": digest})
    return files, _tree_sha256(files)


def build_hf_continuation_finalization_packet(
    *,
    output_dir: str | Path,
) -> HfContinuationFinalizationPacket:
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise ContinuationFinalizationError(f"refusing to overwrite finalization packet: {target}")
    source_contract = attempt9_finalization_source_contract()
    contract_payload = canonical_json_bytes(source_contract)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.tmp-",
            dir=target.parent,
        )
    )
    try:
        project = temporary / "repo/ml/scriber_polishing"
        module_root = project / "src/scriber_polishing"
        scripts = project / "scripts"
        job_input = project / "job-input"
        module_root.mkdir(parents=True)
        scripts.mkdir()
        job_input.mkdir()
        local_project = Path(__file__).resolve().parents[2]
        shutil.copy2(Path(__file__), module_root / Path(__file__).name)
        shutil.copy2(
            local_project / "scripts/finalize_hf_training_continuation_recovery.py",
            scripts / "finalize_hf_training_continuation_recovery.py",
        )
        shutil.copy2(
            local_project / "scripts/verify_hf_training_continuation_finalization.py",
            scripts / "verify_hf_training_continuation_finalization.py",
        )
        (job_input / "attempt9-finalization-source-contract.json").write_bytes(contract_payload)
        (temporary / "run-continuation-finalization.sh").write_bytes(finalization_runner_payload())
        inventory, tree = _packet_inventory(temporary)
        manifest = {
            "schema_version": 1,
            "kind": "scriber_hf_training_continuation_finalization_job_packet",
            "image": FINALIZATION_IMAGE,
            "flavor": FINALIZATION_FLAVOR,
            "timeout_minutes": FINALIZATION_TIMEOUT_MINUTES,
            "budget": finalization_budget(),
            "source_contract_sha256": _sha256(contract_payload),
            "source_job_id": ATTEMPT9_JOB_ID,
            "source_run_fingerprint": ATTEMPT9_RUN_FINGERPRINT,
            "source_remote_uri": ATTEMPT9_SOURCE_URI,
            "source_mount_mode": "read_only",
            "output_root": "/outputs/" + FINALIZATION_REMOTE_OUTPUT_PATH,
            "remote_result_uri": (
                FINALIZATION_REMOTE_OUTPUT_BUCKET.rstrip("/") + "/" + FINALIZATION_REMOTE_OUTPUT_PATH
            ),
            "completed_training_steps": 254,
            "cumulative_training_steps": 1100,
            "training_steps_executed": 0,
            "validation_reexecuted": False,
            "validation_reused_from_job_id": ATTEMPT9_JOB_ID,
            "evaluation_executed": False,
            "model_copied": False,
            "optimizer_state_loaded": False,
            "packet_tree_sha256": tree,
            "inventory_excludes": ["job-manifest.json"],
            "file_count": len(inventory),
            "byte_count": sum(int(item["bytes"]) for item in inventory),
        }
        (temporary / "job-manifest.json").write_bytes(canonical_json_bytes(manifest))
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return HfContinuationFinalizationPacket(target, manifest)


def verify_hf_continuation_finalization_packet(
    packet_dir: str | Path,
    *,
    expected_packet_tree_sha256: str,
    expected_source_contract_sha256: str,
) -> dict[str, object]:
    root = Path(packet_dir).expanduser().resolve()
    manifest = _json_object(
        _stable_bytes(root / "job-manifest.json", "job manifest"),
        "job manifest",
    )
    inventory, tree = _packet_inventory(
        root,
        excluded=frozenset({"job-manifest.json"}),
    )
    contract_payload = _stable_bytes(
        root / "repo/ml/scriber_polishing/job-input/attempt9-finalization-source-contract.json",
        "source contract",
    )
    if (
        tree != expected_packet_tree_sha256
        or manifest.get("packet_tree_sha256") != tree
        or _sha256(contract_payload) != expected_source_contract_sha256
        or manifest.get("source_contract_sha256") != expected_source_contract_sha256
        or contract_payload != canonical_json_bytes(attempt9_finalization_source_contract())
        or manifest.get("flavor") != FINALIZATION_FLAVOR
        or manifest.get("timeout_minutes") != FINALIZATION_TIMEOUT_MINUTES
        or manifest.get("budget") != finalization_budget()
        or manifest.get("source_remote_uri") != ATTEMPT9_SOURCE_URI
        or manifest.get("output_root") != "/outputs/" + FINALIZATION_REMOTE_OUTPUT_PATH
        or manifest.get("training_steps_executed") != 0
        or manifest.get("validation_reexecuted") is not False
        or manifest.get("model_copied") is not False
        or manifest.get("file_count") != len(inventory)
    ):
        raise ContinuationFinalizationError("finalization packet differs from its external contract")
    runner = _stable_bytes(
        root / "run-continuation-finalization.sh",
        "finalization runner",
    )
    if runner != finalization_runner_payload():
        raise ContinuationFinalizationError("finalization runner differs from reviewed bytes")
    for forbidden in (
        b"train_sft.py",
        b".train(",
        b"optimizer.pt",
        b"evaluator.evaluate",
        b"AutoModel",
        b"AutoTokenizer",
    ):
        if forbidden in runner:
            raise ContinuationFinalizationError("finalization runner contains training or evaluation code")
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_finalization_packet_verification",
        "status": "ready",
        "packet_tree_sha256": tree,
        "source_contract_sha256": expected_source_contract_sha256,
        "file_count": len(inventory),
        "byte_count": sum(int(item["bytes"]) for item in inventory),
        "training_steps_executed": 0,
        "validation_reexecuted": False,
        "model_copied": False,
        "credential_material_present": False,
    }


def build_hf_continuation_finalization_launch_contract(
    packet_dir: str | Path,
) -> dict[str, object]:
    packet = Path(packet_dir).expanduser().resolve()
    manifest = _json_object(
        _stable_bytes(packet / "job-manifest.json", "job manifest"),
        "job manifest",
    )
    tree = str(manifest["packet_tree_sha256"])
    source_contract = str(manifest["source_contract_sha256"])
    verify_hf_continuation_finalization_packet(
        packet,
        expected_packet_tree_sha256=tree,
        expected_source_contract_sha256=source_contract,
    )
    packet_volume = f"{packet}:/input/repo:ro"
    source_volume = f"{ATTEMPT9_SOURCE_URI}:/recovered-run:ro"
    output_volume = f"{FINALIZATION_REMOTE_OUTPUT_BUCKET}:/outputs:rw"
    argv = [
        "hf",
        "jobs",
        "run",
        "--flavor",
        FINALIZATION_FLAVOR,
        "--timeout",
        f"{FINALIZATION_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=continuation-1100-finalization",
        "-v",
        packet_volume,
        "-v",
        source_volume,
        "-v",
        output_volume,
        FINALIZATION_IMAGE,
        "bash",
        "/input/repo/run-continuation-finalization.sh",
        "--expected-packet-tree-sha256",
        tree,
        "--expected-source-contract-sha256",
        source_contract,
    ]
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_finalization_launch_contract",
        "flavor": FINALIZATION_FLAVOR,
        "timeout": f"{FINALIZATION_TIMEOUT_MINUTES}m",
        "image": FINALIZATION_IMAGE,
        "packet_volume": packet_volume,
        "source_volume": source_volume,
        "output_volume": output_volume,
        "output_root": "/outputs/" + FINALIZATION_REMOTE_OUTPUT_PATH,
        "remote_result_uri": (FINALIZATION_REMOTE_OUTPUT_BUCKET.rstrip("/") + "/" + FINALIZATION_REMOTE_OUTPUT_PATH),
        "expected_packet_tree_sha256": tree,
        "expected_source_contract_sha256": source_contract,
        "training_steps_executed": 0,
        "validation_reexecuted": False,
        "model_copied": False,
        "argv": argv,
        "external_job_started": False,
    }
