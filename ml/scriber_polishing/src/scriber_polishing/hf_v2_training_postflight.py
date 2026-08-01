"""Metadata-only, fail-closed postflight for the Attempt-13 V2 training job."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

V2_JOB_ID = "6a6e05bf6b79c09949c1e5fd"
V2_PACKET_TREE_SHA256 = "sha256:3d11fbc9441ee4e563b46a87bf354a34fd9ff59d04a784325c725375f67ec5b6"
V2_PARENT_CHECKPOINT_TREE_SHA256 = "sha256:4cf99b2768c5af9c992ee8f6390822a548d6b546ad354368a8d6cfb8ae288ca4"
V2_LAUNCH_CONTRACT_SHA256 = "sha256:ecb13e460f230f9ee5e265923df82fa8be3975033eddad9cf4e8356104a7a1f9"
V2_POLICY_ARTIFACT_SHA256 = "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
V2_RUN_FINGERPRINT = "sha256:7b23f23e70f60df9d58961808e529ff7738032de75a7a4a62c12529bc734a45c"
V2_FINAL_MODEL_TREE_SHA256 = "sha256:4233c0084cacdd3ba88207cece0ce20d1e1f56cfbfa64e71182a2deb2934c743"
V2_MODEL_WEIGHT_SHA256 = "sha256:fd86d26f70e128fd1e8d4f11e69d4778ae930e628e374aa64595aca5bd0b9bb3"
V2_MODEL_WEIGHT_BYTES = 536_223_056
V2_REMOTE_RESULT_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt13/"
    "scriber-v2-hardcase-replay-v1"
)
V2_OUTPUT_CONTAINER = "/outputs/scriber-v2-hardcase-replay-v1"
V2_COMPLETED_STEPS = 640
V2_LEARNING_RATE = 5e-6

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPLETION_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "hf_v2_training_completion_model_binding_schema.json"
CHECKPOINT_INVENTORY_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "checkpoint_inventory_schema.json"
COMPLETED_TRAINING_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "completed_final_training_report_schema.json"

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RAW_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_UTC_SECONDS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_MAX_METADATA_BYTES = 4 * 1024 * 1024


class V2TrainingPostflightError(ValueError):
    """Attempt-13 completion cannot be proven from the supplied metadata."""


def canonical_json_bytes(value: object) -> bytes:
    """Return the repository's canonical UTF-8 JSON representation."""

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


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise V2TrainingPostflightError(f"{label} must be an object")
    return value


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise V2TrainingPostflightError(f"{label} must be a prefixed lowercase SHA-256")
    return value


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise V2TrainingPostflightError(f"{label} is not a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise V2TrainingPostflightError(f"{label} is not a safe relative POSIX path")
    return value


def _stable_canonical_json(path: str | Path, label: str) -> tuple[bytes, dict[str, Any]]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise V2TrainingPostflightError(f"{label} must be a regular file")
    resolved = candidate.resolve()
    before = resolved.stat()
    if before.st_size > _MAX_METADATA_BYTES:
        raise V2TrainingPostflightError(f"{label} exceeds the metadata-only size limit")
    payload = resolved.read_bytes()
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise V2TrainingPostflightError(f"{label} changed while it was read")

    def duplicate_safe_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise V2TrainingPostflightError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise V2TrainingPostflightError(f"{label} contains non-finite JSON value {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=duplicate_safe_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2TrainingPostflightError(f"{label} is not valid UTF-8 JSON") from error
    materialized = _mapping(value, label)
    if payload != canonical_json_bytes(materialized):
        raise V2TrainingPostflightError(f"{label} is not canonical JSON")
    return payload, materialized


def _stable_external_json(path: str | Path, label: str) -> tuple[bytes, object]:
    """Read one bounded API snapshot without imposing a presentation format."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise V2TrainingPostflightError(f"{label} must be a regular file")
    resolved = candidate.resolve()
    before = resolved.stat()
    if before.st_size > _MAX_METADATA_BYTES:
        raise V2TrainingPostflightError(f"{label} exceeds the metadata-only size limit")
    payload = resolved.read_bytes()
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise V2TrainingPostflightError(f"{label} changed while it was read")

    def duplicate_safe_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise V2TrainingPostflightError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise V2TrainingPostflightError(f"{label} contains non-finite JSON value {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=duplicate_safe_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2TrainingPostflightError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict | list):
        raise V2TrainingPostflightError(f"{label} must contain an object or array")
    return payload, value


def _validate_schema(path: Path, value: Mapping[str, object], label: str) -> None:
    try:
        schema = json.loads(path.read_bytes())
        Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as error:
        raise V2TrainingPostflightError(f"{label} schema is unavailable or invalid") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(value)),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise V2TrainingPostflightError(f"{label} schema failed at {location}: {errors[0].message}")


def _tree_sha256(files: Sequence[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _verify_final_inventory(inventory: Mapping[str, object], *, run_fingerprint: str) -> list[dict[str, object]]:
    _validate_schema(CHECKPOINT_INVENTORY_SCHEMA_PATH, inventory, "final model inventory")
    files_raw = inventory.get("files")
    if not isinstance(files_raw, list):
        raise V2TrainingPostflightError("final model inventory files must be a list")
    files: list[dict[str, object]] = []
    prior_path = ""
    for raw in files_raw:
        item = _mapping(raw, "final model inventory entry")
        path = _safe_relative(item.get("path"), "final model inventory path")
        byte_count = item.get("bytes")
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise V2TrainingPostflightError("final model inventory byte count is invalid")
        sha = _hash(item.get("sha256"), f"final model {path} SHA-256")
        if path <= prior_path:
            raise V2TrainingPostflightError("final model inventory paths must be strictly sorted and unique")
        prior_path = path
        files.append({"path": path, "bytes": byte_count, "sha256": sha})
    if (
        inventory.get("variant") != "bf16"
        or inventory.get("format") != "transformers_safetensors"
        or inventory.get("declared_dtype") not in {"bf16", "bfloat16"}
        or inventory.get("weight_dtype_counts") != {"BF16": 236}
        or inventory.get("source_id") != f"training-run:{run_fingerprint}"
        or inventory.get("file_count") != len(files)
        or inventory.get("total_bytes") != sum(int(item["bytes"]) for item in files)
        or inventory.get("tree_sha256") != _tree_sha256(files)
    ):
        raise V2TrainingPostflightError("final model BF16 inventory binding differs")
    required = {"config.json", "reload-verification.json", "scriber-protection-policy.json"}
    paths = {str(item["path"]) for item in files}
    if not required <= paths:
        raise V2TrainingPostflightError("final model inventory lacks required metadata")
    weights = [item for item in files if str(item["path"]).endswith(".safetensors")]
    if weights != [
        {
            "path": "model.safetensors",
            "bytes": V2_MODEL_WEIGHT_BYTES,
            "sha256": V2_MODEL_WEIGHT_SHA256,
        }
    ]:
        raise V2TrainingPostflightError("final model weight SHA-256 or size differs from completed Attempt 13")
    if inventory.get("tree_sha256") != V2_FINAL_MODEL_TREE_SHA256:
        raise V2TrainingPostflightError("final model tree differs from completed Attempt 13 weights")
    policy = [item for item in files if item["path"] == "scriber-protection-policy.json"]
    if len(policy) != 1 or policy[0]["sha256"] != V2_POLICY_ARTIFACT_SHA256:
        raise V2TrainingPostflightError("final model protection-policy SHA-256 differs")
    return weights


def _verify_parent_binding(parent: Mapping[str, object]) -> None:
    raw_files = parent.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != 12:
        raise V2TrainingPostflightError("V2 parent binding must contain the exact 12-file allowlist")
    files: list[dict[str, object]] = []
    previous = ""
    for raw in raw_files:
        item = _mapping(raw, "V2 parent binding entry")
        path = _safe_relative(item.get("path"), "V2 parent binding path")
        byte_count = item.get("bytes")
        if (
            PurePosixPath(path).name != path
            or path <= previous
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            raise V2TrainingPostflightError("V2 parent binding file list is invalid")
        previous = path
        files.append(
            {
                "path": path,
                "bytes": byte_count,
                "sha256": _hash(item.get("sha256"), f"V2 parent {path} SHA-256"),
            }
        )
    if (
        parent.get("tree_sha256") != V2_PARENT_CHECKPOINT_TREE_SHA256
        or _sha256(canonical_json_bytes(files)) != V2_PARENT_CHECKPOINT_TREE_SHA256
        or parent.get("file_count") != len(files)
        or parent.get("byte_count") != sum(int(item["bytes"]) for item in files)
        or parent.get("ignored_remote_metadata") != ["checkpoint-inventory.json"]
    ):
        raise V2TrainingPostflightError("V2 parent binding files differ from the accepted parent tree")


def _verify_remote_inventory(
    inventory: object,
    *,
    final_directory: str,
    model_files: Sequence[Mapping[str, object]],
    metadata_sizes: Mapping[str, int],
) -> None:
    prefix = "attempt13/scriber-v2-hardcase-replay-v1/"
    if isinstance(inventory, list):
        raw_entries = inventory
        strip_prefix = True
    elif isinstance(inventory, Mapping):
        if (
            inventory.get("schema_version") != 1
            or inventory.get("kind") != "scriber_hf_v2_training_remote_inventory"
            or inventory.get("source") != "hugging_face_bucket_tree_api"
            or inventory.get("remote_uri") != V2_REMOTE_RESULT_URI
            or inventory.get("recursive") is not True
            or not isinstance(inventory.get("observed_at_utc"), str)
            or _UTC_SECONDS.fullmatch(str(inventory.get("observed_at_utc"))) is None
        ):
            raise V2TrainingPostflightError("remote inventory identity differs")
        raw_entries = inventory.get("entries")
        strip_prefix = False
    else:
        raise V2TrainingPostflightError("remote inventory must be an API array")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise V2TrainingPostflightError("remote inventory entries are missing")
    observed: dict[str, int] = {}
    for raw in raw_entries:
        entry = _mapping(raw, "remote inventory entry")
        raw_path = _safe_relative(entry.get("path"), "remote inventory path")
        if strip_prefix:
            if not raw_path.startswith(prefix):
                raise V2TrainingPostflightError("remote inventory path is outside the exact Attempt-13 prefix")
            path = _safe_relative(raw_path.removeprefix(prefix), "remote inventory relative path")
        else:
            path = raw_path
        size = entry.get("size")
        xet_hash = entry.get("xet_hash")
        if (
            (not strip_prefix and entry.get("type") != "file")
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(xet_hash, str)
            or _RAW_SHA256.fullmatch(xet_hash) is None
            or path in observed
        ):
            raise V2TrainingPostflightError("remote inventory contains an ambiguous file entry")
        observed[path] = size
    required = dict(metadata_sizes)
    final_prefix = f"training/{final_directory}/"
    for item in model_files:
        required[final_prefix + str(item["path"])] = int(item["bytes"])
    for path, size in required.items():
        if observed.get(path) != size:
            raise V2TrainingPostflightError(f"remote inventory does not bind required artifact {path}")


def _verify_terminal_job_snapshot(receipt: Mapping[str, object], terminal: object) -> None:
    if isinstance(terminal, Mapping):
        receipt_sha = _sha256(canonical_json_bytes(dict(receipt)))
        if (
            terminal.get("schema_version") != 1
            or terminal.get("kind") != "scriber_hf_v2_training_terminal_evidence"
            or terminal.get("source") != "hugging_face_jobs_rest_api"
            or terminal.get("job_id") != V2_JOB_ID
            or terminal.get("terminal_status") != "COMPLETED"
            or terminal.get("packet_tree_sha256") != V2_PACKET_TREE_SHA256
            or terminal.get("remote_result_uri") != V2_REMOTE_RESULT_URI
            or terminal.get("launch_receipt_sha256") != receipt_sha
            or not isinstance(terminal.get("observed_at_utc"), str)
            or _UTC_SECONDS.fullmatch(str(terminal.get("observed_at_utc"))) is None
        ):
            raise V2TrainingPostflightError("terminal HF job evidence does not prove completed Attempt 13")
        return
    if not isinstance(terminal, list) or len(terminal) != 1:
        raise V2TrainingPostflightError("terminal HF job inspection must contain exactly one job")
    job = _mapping(terminal[0], "terminal HF job")
    status = _mapping(job.get("status"), "terminal HF job status")
    owner = _mapping(job.get("owner"), "terminal HF job owner")
    labels = _mapping(job.get("labels"), "terminal HF job labels")
    inspection = _mapping(receipt.get("job_inspection"), "V2 launch job inspection")
    owner_token = receipt.get("owner_token")
    command = job.get("command")
    if (
        not isinstance(owner_token, str)
        or _RAW_SHA256.fullmatch(owner_token) is None
        or not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) for item in command)
        or inspection.get("command_sha256") != _sha256(canonical_json_bytes(command))
        or command.count(V2_PACKET_TREE_SHA256) != 1
        or command.count(V2_LAUNCH_CONTRACT_SHA256) != 1
        or command.count(owner_token) != 1
    ):
        raise V2TrainingPostflightError("terminal HF job command differs from the launch receipt")
    expected_labels = {
        "name": "scriber-v2-hardcase-replay-v1",
        "project": "scriber-polishing",
        "campaign": "v2-hardcase-replay",
        "attempt": "attempt13",
        "role": "v2-training",
        "output-prefix": "attempt13-scriber-v2-hardcase-replay-v1",
        "launch-claim": owner_token,
    }
    expected_volumes = [
        {
            "type": "bucket",
            "source": "Buttermilk03/scriber-polishing-private-runs",
            "mount_path": "/input/packet",
            "read_only": True,
            "path": "attempt13/packets/scriber-v2-hardcase-replay-v1",
        },
        {
            "type": "bucket",
            "source": "Buttermilk03/scriber-polishing-private-runs",
            "mount_path": "/input/parent",
            "read_only": True,
            "path": "attempt11/scriber-full-120k-v2/training/final-84458762af7b00ac",
        },
        {
            "type": "bucket",
            "source": "Buttermilk03/scriber-polishing-private-runs",
            "mount_path": "/outputs",
            "read_only": False,
            "path": "attempt13",
        },
    ]
    if (
        job.get("id") != V2_JOB_ID
        or status.get("stage") != "COMPLETED"
        or job.get("docker_image")
        != "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
        or job.get("flavor") != "l4x1"
        or labels != expected_labels
        or job.get("volumes") != expected_volumes
        or job.get("arguments") != []
        or job.get("environment") not in ({}, [])
        or job.get("secrets") not in ({}, [])
        or owner.get("name") != "Buttermilk03"
        or job.get("url") != f"https://huggingface.co/jobs/Buttermilk03/{V2_JOB_ID}"
    ):
        raise V2TrainingPostflightError("terminal HF job inspection differs from exact Attempt 13")


def verify_v2_training_postflight(
    *,
    launch_receipt: str | Path,
    terminal_job: str | Path,
    training_report: str | Path,
    training_result: str | Path,
    runtime_verification: str | Path,
    packet_verification: str | Path,
    parent_binding: str | Path,
    final_inventory: str | Path,
    reload_verification: str | Path,
    remote_inventory: str | Path,
) -> dict[str, object]:
    """Verify Attempt 13 from small JSON metadata; never read or download model bytes."""

    sources = {
        "launch_receipt": _stable_canonical_json(launch_receipt, "V2 launch receipt"),
        "terminal_job": _stable_external_json(terminal_job, "V2 terminal job evidence"),
        "training_report": _stable_canonical_json(training_report, "V2 training report"),
        "training_result": _stable_canonical_json(training_result, "V2 training result"),
        "runtime_verification": _stable_canonical_json(runtime_verification, "V2 runtime verification"),
        "packet_verification": _stable_canonical_json(packet_verification, "V2 packet verification"),
        "parent_binding": _stable_canonical_json(parent_binding, "V2 parent binding"),
        "final_inventory": _stable_canonical_json(final_inventory, "V2 final model inventory"),
        "reload_verification": _stable_canonical_json(reload_verification, "V2 reload verification"),
        "remote_inventory": _stable_external_json(remote_inventory, "V2 remote inventory"),
    }
    receipt = sources["launch_receipt"][1]
    terminal = sources["terminal_job"][1]
    report = sources["training_report"][1]
    result = sources["training_result"][1]
    runtime = sources["runtime_verification"][1]
    packet = sources["packet_verification"][1]
    parent = sources["parent_binding"][1]
    inventory = sources["final_inventory"][1]
    reload_report = sources["reload_verification"][1]
    remote = sources["remote_inventory"][1]

    if (
        receipt.get("schema_version") != 1
        or receipt.get("kind") != "scriber_hf_v2_training_launch_receipt"
        or receipt.get("job_id") != V2_JOB_ID
        or receipt.get("packet_tree_sha256") != V2_PACKET_TREE_SHA256
        or receipt.get("launch_contract_sha256") != V2_LAUNCH_CONTRACT_SHA256
        or receipt.get("remote_result_uri") != V2_REMOTE_RESULT_URI
        or receipt.get("duplicate_retry_allowed") is not False
    ):
        raise V2TrainingPostflightError("V2 launch receipt differs from Attempt 13")
    inspection = _mapping(receipt.get("job_inspection"), "V2 launch job inspection")
    if inspection.get("id") != V2_JOB_ID or inspection.get("exact_identity_verified") is not True:
        raise V2TrainingPostflightError("V2 launch receipt does not prove exact job identity")
    receipt_sha = _sha256(sources["launch_receipt"][0])
    _verify_terminal_job_snapshot(receipt, terminal)

    _validate_schema(COMPLETED_TRAINING_SCHEMA_PATH, report, "V2 completed training report")
    fingerprint = _hash(report.get("run_fingerprint"), "V2 run fingerprint")
    if fingerprint != V2_RUN_FINGERPRINT:
        raise V2TrainingPostflightError("V2 run fingerprint differs from completed Attempt 13")
    short = fingerprint.removeprefix("sha256:")[:16]
    final_directory = f"final-{short}"
    checkpoint_state = _mapping(report.get("checkpoint_state"), "V2 checkpoint state")
    schedule = _mapping(report.get("schedule"), "V2 training schedule")
    warm_start = _mapping(report.get("warm_start_parent_checkpoint"), "V2 warm-start parent")
    report_policy = _mapping(report.get("protection_policy"), "V2 protection policy")
    final_model = _mapping(report.get("final_model"), "V2 final model")
    if (
        report.get("training_completed") is not True
        or report.get("run_kind") != "final"
        or report.get("bf16") is not True
        or report.get("learning_rate") != V2_LEARNING_RATE
        or report.get("max_steps") != V2_COMPLETED_STEPS
        or report.get("early_stopping") is not False
        or checkpoint_state.get("global_step") != V2_COMPLETED_STEPS
        or schedule.get("max_steps") != V2_COMPLETED_STEPS
        or warm_start.get("relationship") != "warm_start_parent_not_resume"
        or warm_start.get("tree_sha256") != V2_PARENT_CHECKPOINT_TREE_SHA256
        or report_policy.get("sha256") != V2_POLICY_ARTIFACT_SHA256
        or final_model.get("run_fingerprint") != fingerprint
        or final_model.get("directory") != final_directory
        or final_model.get("path") != f"{V2_OUTPUT_CONTAINER}/training/{final_directory}"
    ):
        raise V2TrainingPostflightError("V2 training report differs from the reviewed 640-step BF16 run")
    if final_model.get("inventory") != inventory or final_model.get("reload_verification") != reload_report:
        raise V2TrainingPostflightError("V2 final model metadata differs from the training report")
    weights = _verify_final_inventory(inventory, run_fingerprint=fingerprint)
    reload_row = next(
        (item for item in inventory["files"] if item.get("path") == "reload-verification.json"),
        None,
    )
    if (
        not isinstance(reload_row, Mapping)
        or reload_row.get("bytes") != len(sources["reload_verification"][0])
        or reload_row.get("sha256") != _sha256(sources["reload_verification"][0])
        or reload_report.get("schema_version") != 1
        or reload_report.get("status") != "passed"
        or reload_report.get("loader") != "transformers_local"
        or reload_report.get("run_fingerprint") != fingerprint
        or reload_report.get("protection_policy_sha256") != V2_POLICY_ARTIFACT_SHA256
    ):
        raise V2TrainingPostflightError("V2 reload verification is not bound and passed")

    report_sha = _sha256(sources["training_report"][0])
    if (
        result.get("schema_version") != 1
        or result.get("kind") != "scriber_hf_v2_training_job_result"
        or result.get("packet_tree_sha256") != V2_PACKET_TREE_SHA256
        or result.get("parent_checkpoint_tree_sha256") != V2_PARENT_CHECKPOINT_TREE_SHA256
        or result.get("training_report_sha256") != report_sha
        or result.get("run_fingerprint") != fingerprint
        or result.get("training_completed") is not True
        or result.get("save_final") is not True
        or result.get("auto_retry_allowed") is not False
    ):
        raise V2TrainingPostflightError("V2 training result does not bind the completed training report")
    source_head = result.get("source_git_head")
    if not isinstance(source_head, str) or _GIT_SHA1.fullmatch(source_head) is None:
        raise V2TrainingPostflightError("V2 training result source Git head is invalid")
    if (
        packet.get("source_git_head") != source_head
        or packet.get("packet_tree_sha256") != V2_PACKET_TREE_SHA256
        or packet.get("launch_contract_sha256") != V2_LAUNCH_CONTRACT_SHA256
        or packet.get("parent_checkpoint_tree_sha256") != V2_PARENT_CHECKPOINT_TREE_SHA256
        or packet.get("remote_result_uri") != V2_REMOTE_RESULT_URI
        or packet.get("training") != {"epochs": 1, "learning_rate": V2_LEARNING_RATE, "bf16": True}
    ):
        raise V2TrainingPostflightError("V2 packet or parent evidence differs from Attempt 13")
    _verify_parent_binding(parent)
    if (
        runtime.get("cuda_available") is not True
        or runtime.get("bf16_supported") is not True
        or runtime.get("device_count") != 1
        or not isinstance(runtime.get("gpu"), str)
        or not runtime.get("gpu")
        or not isinstance(runtime.get("torch_version"), str)
        or not runtime.get("torch_version")
        or not isinstance(runtime.get("cuda_runtime"), str)
        or not runtime.get("cuda_runtime")
    ):
        raise V2TrainingPostflightError("V2 runtime verification does not prove CUDA BF16")

    _verify_remote_inventory(
        remote,
        final_directory=final_directory,
        model_files=inventory["files"],
        metadata_sizes={
            "training-report.json": len(sources["training_report"][0]),
            "v2-training-result.json": len(sources["training_result"][0]),
            "runtime-verification.json": len(sources["runtime_verification"][0]),
            f"training/{final_directory}/checkpoint-inventory.json": len(sources["final_inventory"][0]),
        },
    )

    completion = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_completion_model_binding",
        "status": "complete",
        "job": {
            "id": V2_JOB_ID,
            "terminal_status": "COMPLETED",
            "remote_result_uri": V2_REMOTE_RESULT_URI,
        },
        "source": {
            "source_git_head": source_head,
            "packet_tree_sha256": V2_PACKET_TREE_SHA256,
            "parent_checkpoint_tree_sha256": V2_PARENT_CHECKPOINT_TREE_SHA256,
            "launch_contract_sha256": V2_LAUNCH_CONTRACT_SHA256,
        },
        "training": {
            "completed_steps": V2_COMPLETED_STEPS,
            "max_steps": V2_COMPLETED_STEPS,
            "learning_rate": V2_LEARNING_RATE,
            "bf16": True,
        },
        "model": {
            "run_fingerprint": fingerprint,
            "directory": final_directory,
            "remote_uri": f"{V2_REMOTE_RESULT_URI}/training/{final_directory}",
            "tree_sha256": inventory["tree_sha256"],
            "weight_files": weights,
            "protection_policy_sha256": V2_POLICY_ARTIFACT_SHA256,
            "reload_status": "passed",
            "inventory_sha256": _sha256(sources["final_inventory"][0]),
            "reload_verification_sha256": _sha256(sources["reload_verification"][0]),
        },
        "runtime": {
            "cuda_available": True,
            "bf16_supported": True,
            "device_count": 1,
            "gpu": runtime["gpu"],
            "torch_version": runtime["torch_version"],
            "cuda_runtime": runtime["cuda_runtime"],
            "verification_sha256": _sha256(sources["runtime_verification"][0]),
        },
        "evidence": {
            "launch_receipt_sha256": receipt_sha,
            "terminal_job_sha256": _sha256(sources["terminal_job"][0]),
            "training_report_sha256": report_sha,
            "training_result_sha256": _sha256(sources["training_result"][0]),
            "packet_verification_sha256": _sha256(sources["packet_verification"][0]),
            "parent_binding_sha256": _sha256(sources["parent_binding"][0]),
            "remote_inventory_sha256": _sha256(sources["remote_inventory"][0]),
        },
        "verification": {
            "metadata_only": True,
            "model_downloaded": False,
            "external_mutation_performed": False,
        },
    }
    _validate_schema(COMPLETION_SCHEMA_PATH, completion, "V2 completion/model binding")
    return completion
