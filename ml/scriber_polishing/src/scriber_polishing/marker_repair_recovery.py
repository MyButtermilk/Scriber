"""Fail-closed finalization of one recovered marker-repair evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .curriculum import write_immutable_json
from .evaluation_io import validate_candidate_prediction
from .inference import _inventory_local_model_tree
from .sst_parser import SSTParseError, parse_sst

_EXPECTED_TRAINING_STEPS = 146
_EXPECTED_CASE_COUNT = 200
_FINAL_DIRECTORY = re.compile(r"^final-[a-z0-9][a-z0-9_-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREFIXED_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECOVERY_RESULT = "recovery-evaluation-result.json"
_CLOUD_RESULT = "cloud-result.json"
_REQUIRED_FILES = (
    "acceptance-policy.yaml",
    "evaluation-source-binding.json",
    "evaluation.json",
    "predictions.jsonl",
    "predictions.manifest.json",
    "preflight.json",
    "repair-manifest.json",
    "training-report.json",
)


class MarkerRepairRecoveryError(ValueError):
    """Recovered training or evaluation evidence is incomplete or inconsistent."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stable_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink():
        raise MarkerRepairRecoveryError(f"{label} must not be a symlink: {path}")
    if not path.is_file():
        raise MarkerRepairRecoveryError(f"{label} must be a regular file: {path}")
    before = path.stat()
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise MarkerRepairRecoveryError(f"cannot read {label}: {error}") from error
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
        raise MarkerRepairRecoveryError(f"{label} changed while it was being read")
    return payload


def _unique_json_object(payload: bytes, label: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MarkerRepairRecoveryError(f"{label} contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MarkerRepairRecoveryError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise MarkerRepairRecoveryError(f"{label} must contain a JSON object")
    return value


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    payload = _stable_bytes(path, label)
    return _unique_json_object(payload, label), payload


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MarkerRepairRecoveryError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list | tuple):
        raise MarkerRepairRecoveryError(f"{label} must be a sequence")
    return value


def _exact_int(value: object, expected: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise MarkerRepairRecoveryError(f"{label} must equal {expected}")


def _plain_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise MarkerRepairRecoveryError(f"{label} must be a lowercase SHA-256")
    return value


def _prefixed_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _PREFIXED_SHA256.fullmatch(value):
        raise MarkerRepairRecoveryError(f"{label} must be a lowercase sha256:-prefixed SHA-256")
    return value


def _validate_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise MarkerRepairRecoveryError(f"recovery root must be a regular directory: {root}")
    for relative in _REQUIRED_FILES:
        _stable_bytes(root / relative, f"required recovery file {relative}")
    for result_name in (_RECOVERY_RESULT, _CLOUD_RESULT):
        temporary = root / f"{result_name}.tmp"
        if temporary.exists():
            raise MarkerRepairRecoveryError(f"stale temporary recovery result: {temporary}")
    if (root / _CLOUD_RESULT).exists() and not (root / _RECOVERY_RESULT).exists():
        raise MarkerRepairRecoveryError("cloud result cannot exist without its recovery evaluation result")


def _validate_training(
    root: Path,
) -> tuple[dict[str, Any], str, str, Mapping[str, object]]:
    report, payload = _load_json(
        root / "training-report.json",
        "training report",
    )
    report_sha256 = f"sha256:{_sha256(payload)}"
    schedule = _mapping(report.get("schedule"), "training schedule")
    checkpoint = _mapping(
        report.get("checkpoint_state"),
        "training checkpoint state",
    )
    final_model = _mapping(report.get("final_model"), "final model")
    declared_inventory = _mapping(
        final_model.get("inventory"),
        "final model inventory",
    )
    reload_verification = _mapping(
        final_model.get("reload_verification"),
        "final model reload verification",
    )
    if report.get("training_completed") is not True:
        raise MarkerRepairRecoveryError("training report is not complete")
    for value, label in (
        (report.get("max_steps"), "training max steps"),
        (schedule.get("max_steps"), "training schedule max steps"),
        (
            schedule.get("update_steps_per_epoch"),
            "training schedule update steps",
        ),
        (checkpoint.get("global_step"), "training completed steps"),
    ):
        _exact_int(value, _EXPECTED_TRAINING_STEPS, label)
    if checkpoint.get("last_checkpoint") != "checkpoint-146":
        raise MarkerRepairRecoveryError("training report does not bind checkpoint-146")
    if reload_verification.get("status") != "passed":
        raise MarkerRepairRecoveryError("final model reload verification did not pass")

    final_directory = final_model.get("directory")
    if (
        not isinstance(final_directory, str)
        or not _FINAL_DIRECTORY.fullmatch(final_directory)
        or Path(final_directory).name != final_directory
    ):
        raise MarkerRepairRecoveryError("final model directory name is invalid")
    training_root = root / "training"
    if training_root.is_symlink() or not training_root.is_dir():
        raise MarkerRepairRecoveryError("training output directory is missing")
    final_directories = [path for path in training_root.iterdir() if path.name.startswith("final-") and path.is_dir()]
    if len(final_directories) != 1 or final_directories[0].name != final_directory:
        raise MarkerRepairRecoveryError("training output does not contain exactly the reported final model")
    try:
        actual_inventory = _inventory_local_model_tree(final_directories[0])
    except (OSError, TypeError, ValueError) as error:
        raise MarkerRepairRecoveryError(f"final model inventory is invalid: {error}") from error
    if (
        declared_inventory.get("tree_sha256") != actual_inventory["tree_sha256"]
        or declared_inventory.get("file_count") != actual_inventory["file_count"]
        or declared_inventory.get("total_bytes") != actual_inventory["total_bytes"]
    ):
        raise MarkerRepairRecoveryError("training report final model inventory differs from local model bytes")
    return report, report_sha256, final_directory, actual_inventory


def _validate_recovery_binding(
    root: Path,
    *,
    report_sha256: str,
    final_directory: str,
    model_inventory: Mapping[str, object],
) -> dict[str, Any]:
    binding, _ = _load_json(
        root / "evaluation-source-binding.json",
        "evaluation recovery source binding",
    )
    if (
        binding.get("schema_version") != 1
        or binding.get("kind") != "scriber_marker_repair_evaluation_recovery_binding"
        or binding.get("training_reused") is not True
    ):
        raise MarkerRepairRecoveryError("evaluation source is not a bound training recovery")
    _exact_int(
        binding.get("training_steps_executed"),
        0,
        "recovery training steps executed",
    )
    _exact_int(
        binding.get("completed_training_steps"),
        _EXPECTED_TRAINING_STEPS,
        "recovery completed training steps",
    )
    if (
        _prefixed_sha256(
            binding.get("training_report_sha256"),
            "recovery training report hash",
        )
        != report_sha256
        or binding.get("final_model_directory") != final_directory
        or _prefixed_sha256(
            binding.get("final_model_tree_sha256"),
            "recovery final model tree hash",
        )
        != model_inventory["tree_sha256"]
    ):
        raise MarkerRepairRecoveryError("recovery binding differs from the completed training output")
    for key, label in (
        ("final_model_file_count", "recovery final model file count"),
        ("final_model_total_bytes", "recovery final model total bytes"),
    ):
        expected = int(model_inventory[key.removeprefix("final_model_")])
        _exact_int(binding.get(key), expected, label)
    return binding


def _prediction_rows(payload: bytes) -> tuple[dict[str, Any], ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MarkerRepairRecoveryError("predictions are not valid UTF-8") from error
    lines = text.splitlines()
    if len(lines) != _EXPECTED_CASE_COUNT or any(not line for line in lines):
        raise MarkerRepairRecoveryError("predictions must contain exactly 200 non-empty JSONL records")
    rows: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        row = _unique_json_object(
            line.encode("utf-8"),
            f"prediction row {line_number}",
        )
        try:
            validated = validate_candidate_prediction(row)
            parse_sst(validated["prediction_sst"])
        except (SSTParseError, TypeError, ValueError) as error:
            raise MarkerRepairRecoveryError(f"prediction row {line_number} is not valid SST: {error}") from error
        case_id = validated["case_id"]
        if case_id in case_ids:
            raise MarkerRepairRecoveryError(f"duplicate prediction case_id: {case_id}")
        case_ids.add(case_id)
        rows.append(validated)
    return tuple(rows)


def _validate_evaluation(
    root: Path,
) -> tuple[dict[str, Any], tuple[str, ...], int]:
    predictions_payload = _stable_bytes(
        root / "predictions.jsonl",
        "candidate predictions",
    )
    predictions = _prediction_rows(predictions_payload)
    prediction_sha256 = _sha256(predictions_payload)
    manifest, _ = _load_json(
        root / "predictions.manifest.json",
        "candidate prediction manifest",
    )
    reference_sha256 = _plain_sha256(
        manifest.get("reference_sha256"),
        "candidate reference hash",
    )
    if (
        manifest.get("schema_version") != 1
        or manifest.get("candidate") != "lexical_repair"
        or manifest.get("prediction_sha256") != prediction_sha256
    ):
        raise MarkerRepairRecoveryError("candidate prediction manifest is not bound to lexical_repair bytes")
    for value, expected, label in (
        (manifest.get("record_count"), 200, "candidate prediction count"),
        (manifest.get("valid_sst_count"), 200, "candidate valid SST count"),
        (
            manifest.get("restoration_error_count"),
            0,
            "candidate restoration error count",
        ),
    ):
        _exact_int(value, expected, label)

    evaluation, _ = _load_json(
        root / "evaluation.json",
        "candidate evaluation",
    )
    gate = _mapping(evaluation.get("gate"), "candidate evaluation gate")
    if not isinstance(gate.get("passed"), bool):
        raise MarkerRepairRecoveryError("candidate evaluation gate must contain a boolean decision")
    metrics = _mapping(evaluation.get("metrics"), "candidate evaluation metrics")
    if (
        evaluation.get("schema_version") != 1
        or evaluation.get("candidate") != "lexical_repair"
        or evaluation.get("reference_sha256") != reference_sha256
        or evaluation.get("prediction_sha256") != prediction_sha256
        or evaluation.get("exact_case_coverage") is not True
        or metrics.get("sst_parse_rate") != 1.0
        or metrics.get("protected_span_exact_rate") != 1.0
    ):
        raise MarkerRepairRecoveryError("candidate evaluation is not exact 200-case lexical repair coverage")
    _exact_int(metrics.get("total"), 200, "candidate evaluation case count")
    case_results = _sequence(
        metrics.get("case_results"),
        "candidate evaluation case results",
    )
    _exact_int(
        len(case_results),
        _EXPECTED_CASE_COUNT,
        "candidate evaluation result count",
    )
    prediction_case_ids = tuple(str(row["case_id"]) for row in predictions)
    evaluation_case_ids: list[str] = []
    for index, value in enumerate(case_results):
        case = _mapping(value, f"candidate evaluation case {index}")
        case_id = case.get("case_id")
        if (
            not isinstance(case_id, str)
            or case.get("sst_valid") is not True
            or case.get("protected_spans_exact") is not True
        ):
            raise MarkerRepairRecoveryError(f"candidate evaluation case {index} is not fully valid")
        evaluation_case_ids.append(case_id)
    if tuple(evaluation_case_ids) != prediction_case_ids:
        raise MarkerRepairRecoveryError("candidate evaluation case coverage differs from predictions")
    return manifest, prediction_case_ids, 0 if gate["passed"] else 2


def _inventory_files(
    root: Path,
    *,
    excluded: frozenset[str],
) -> dict[str, dict[str, object]]:
    files: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise MarkerRepairRecoveryError(f"recovery output must not contain symlinks: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise MarkerRepairRecoveryError(f"recovery output contains a non-regular entry: {path}")
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        payload = _stable_bytes(path, f"recovery output file {relative}")
        files[relative] = {
            "bytes": len(payload),
            "sha256": _sha256(payload),
        }
    return files


def finalize_marker_repair_recovery(
    recovery_root: str | Path,
    *,
    evaluation_gate_exit: int,
) -> dict[str, dict[str, Any]]:
    """Validate and immutably finalize one combined recovery cloud output."""

    if (
        isinstance(evaluation_gate_exit, bool)
        or not isinstance(evaluation_gate_exit, int)
        or evaluation_gate_exit not in (0, 2)
    ):
        raise MarkerRepairRecoveryError("evaluation gate exit must be 0 or 2")
    root = Path(recovery_root).expanduser().resolve()
    _validate_root(root)
    _, report_sha256, final_directory, model_inventory = _validate_training(root)
    _validate_recovery_binding(
        root,
        report_sha256=report_sha256,
        final_directory=final_directory,
        model_inventory=model_inventory,
    )
    _, _, bound_evaluation_gate_exit = _validate_evaluation(root)
    if evaluation_gate_exit != bound_evaluation_gate_exit:
        raise MarkerRepairRecoveryError("evaluation gate exit differs from evaluation gate decision")

    recovery_result = {
        "schema_version": 1,
        "kind": "scriber_hf_marker_repair_recovery_evaluation_result",
        "training_reused": True,
        "training_steps_executed": 0,
        "evaluation_completed": True,
        "evaluation_gate_exit": evaluation_gate_exit,
        "final_model_directory": final_directory,
        "final_model_tree_sha256": model_inventory["tree_sha256"],
        "training_report_sha256": report_sha256,
        "files": _inventory_files(
            root,
            excluded=frozenset({_RECOVERY_RESULT, _CLOUD_RESULT}),
        ),
    }
    write_immutable_json(
        root / _RECOVERY_RESULT,
        recovery_result,
        label="marker repair recovery evaluation result",
    )

    cloud_result = {
        "schema_version": 1,
        "kind": "scriber_hf_marker_repair_result",
        "training_completed": True,
        "evaluation_completed": True,
        "evaluation_gate_exit": evaluation_gate_exit,
        "final_model_directory": final_directory,
        "files": _inventory_files(
            root,
            excluded=frozenset({_CLOUD_RESULT}),
        ),
    }
    write_immutable_json(
        root / _CLOUD_RESULT,
        cloud_result,
        label="marker repair cloud result",
    )
    return {
        "recovery_evaluation_result": recovery_result,
        "cloud_result": cloud_result,
    }
