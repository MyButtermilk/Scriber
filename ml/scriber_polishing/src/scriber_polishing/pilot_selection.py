"""Fail-closed selection of one learning rate from exactly two fair pilot runs.

The selector consumes immutable evidence only.  It does not load a model, use
the GPU, resume training, or reinterpret failed safety gates as a release pass.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator

_PROJECT_ROOT = Path(__file__).parents[2]
_CONTRACTS_ROOT = _PROJECT_ROOT / "contracts"
_OUTPUT_SCHEMA = _CONTRACTS_ROOT / "pilot_selection_report_schema.json"
_PROTECTED_DIAGNOSTIC_SCHEMA = _CONTRACTS_ROOT / "protected_span_diagnostic_report_schema.json"
_HASH = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CHECKPOINT = re.compile(r"^checkpoint-([1-9][0-9]*)$")
_INVENTORY_FILENAME = "checkpoint-inventory.json"

_SAFETY_DIRECT_METRICS = (
    "protected_span_exact_rate",
    "output_only_compliance_rate",
    "safe_renderer_rate",
    "renderer_round_trip_rate",
)
_TASK_DIRECT_METRICS = (
    "normalized_exact_match",
    "chrfpp_f2",
    "punctuation_f1",
    "filler_f1",
    "repetition_cleanup_f1",
    "capitalization_accuracy",
    "spelling_recovery_accuracy",
    "grammar_recovery_accuracy",
    "self_correction_recovery_accuracy",
    "sentence_boundary_f1",
)
_TASK_ERROR_METRICS = ("cer", "wer")
_STRUCTURE_METRICS = (
    "sst_parse_rate",
    "ast_exact_match_rate",
    "block_sequence_exact_rate",
    "block_type_accuracy",
    "format_command_recovery_accuracy",
)


def _is_platform_neutral_absolute_path(value: str) -> bool:
    """Accept strict absolute POSIX or Windows drive/UNC evidence paths."""

    if not value or "\x00" in value or value.startswith(("\\\\?\\", "\\\\.\\")):
        return False
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    return (posix.is_absolute() and ".." not in posix.parts) or (windows.is_absolute() and ".." not in windows.parts)


def _is_portable_relative_inventory_path(value: str) -> bool:
    """Allow only traversal-free paths that stay relative on both platforms."""

    if (
        not value
        or "\x00" in value
        or _is_platform_neutral_absolute_path(value)
        or "\\" in value
        or ":" in value
        or re.search(r"(?:^|/)\.\.?(?:/|$)", value) is not None
    ):
        return False
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    return (
        not posix.is_absolute()
        and not windows.is_absolute()
        and not windows.drive
        and not windows.root
        and ".." not in posix.parts
        and ".." not in windows.parts
    )


_WEIGHTS = {
    "safety_content": 0.60,
    "task": 0.25,
    "structure": 0.10,
    "normalized_eval_loss": 0.05,
}
_TIE_BREAKERS = (
    {"field": "hard_gates_passed", "direction": "desc"},
    {"field": "composite_score", "direction": "desc"},
    {"field": "safety_content_score", "direction": "desc"},
    {"field": "task_score", "direction": "desc"},
    {"field": "structure_score", "direction": "desc"},
    {"field": "normalized_eval_loss", "direction": "desc"},
    {"field": "eval_loss", "direction": "asc"},
    {"field": "learning_rate", "direction": "asc"},
    {"field": "candidate_id", "direction": "asc"},
)
_SCORE_POLICY: dict[str, Any] = {
    "version": 1,
    "weights": _WEIGHTS,
    "component_definitions": {
        "safety_content": {
            "aggregation": "arithmetic_mean",
            "direct_rate_metrics": list(_SAFETY_DIRECT_METRICS),
            "derived_metrics": {
                "hard_content_mean": ("arithmetic mean of every path-sorted hard_content_exact_rates value"),
                "critical_integrity": ("clip(1 - sum(critical_errors values) / metrics.total, 0, 1)"),
                "semantic_integrity": ("clip(1 - sum(semantic_hard_check_counts values) / metrics.total, 0, 1)"),
            },
        },
        "task": {
            "aggregation": "arithmetic_mean",
            "direct_rate_metrics": list(_TASK_DIRECT_METRICS),
            "complement_metrics": {
                "cer": "clip(1 - cer, 0, 1)",
                "wer": "clip(1 - wer, 0, 1)",
            },
        },
        "structure": {
            "aggregation": "arithmetic_mean",
            "direct_rate_metrics": list(_STRUCTURE_METRICS),
        },
        "normalized_eval_loss": {
            "formula": "minimum_candidate_eval_loss / candidate_eval_loss",
            "domain": "strictly_positive_finite",
        },
    },
    "hard_first": True,
    "both_hard_fail_policy": "NO_RELEASE_WINNER",
    "tie_breakers": list(_TIE_BREAKERS),
}
_SCORE_POLICY_SHA256 = ""


class PilotSelectionError(ValueError):
    """One pilot-selection input or cross-artifact binding was invalid."""


@dataclass(frozen=True, slots=True)
class PilotCandidateInput:
    """Paths required to prove one pilot candidate end to end."""

    candidate_id: str
    training_report: Path
    model_tree: Path
    prediction_manifest: Path
    prediction_part_manifests: tuple[Path, ...]
    evaluation_report: Path
    protected_span_diagnostic: Path | None = None
    diagnostic_evaluation_report: Path | None = None


@dataclass(frozen=True, slots=True)
class _LoadedFile:
    path: Path
    payload: bytes
    sha256: str


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise PilotSelectionError(f"YAML contains duplicate key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one value in the selector's immutable canonical form."""

    try:
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
    except (TypeError, ValueError) as error:
        raise PilotSelectionError(f"value cannot be canonical JSON: {error}") from error


def canonical_json_sha256(value: object) -> str:
    """Hash one canonical JSON value with the repository hash prefix."""

    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


_SCORE_POLICY_SHA256 = canonical_json_sha256(_SCORE_POLICY)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalize_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise PilotSelectionError(f"{label} must be a lowercase SHA-256")
    return "sha256:" + value.removeprefix("sha256:")


def _stable_file(path: str | Path, label: str) -> _LoadedFile:
    candidate = Path(path)
    if candidate.is_symlink():
        raise PilotSelectionError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise PilotSelectionError(f"{label} must be a regular file: {resolved}")
    before = resolved.stat()
    payload = resolved.read_bytes()
    after = resolved.stat()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise PilotSelectionError(f"{label} changed while it was read")
    return _LoadedFile(path=resolved, payload=payload, sha256=_sha256(payload))


def _strict_json(payload: bytes, label: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise PilotSelectionError(f"{label} contains non-finite JSON value {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PilotSelectionError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PilotSelectionError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise PilotSelectionError(f"{label} must contain a JSON object")
    return value


def _load_json(path: str | Path, label: str) -> tuple[_LoadedFile, dict[str, Any]]:
    loaded = _stable_file(path, label)
    return loaded, _strict_json(loaded.payload, label)


def _load_yaml(path: str | Path, label: str) -> tuple[_LoadedFile, dict[str, Any]]:
    loaded = _stable_file(path, label)
    try:
        value = yaml.load(loaded.payload.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise PilotSelectionError(f"{label} is not valid UTF-8 YAML") from error
    if not isinstance(value, dict):
        raise PilotSelectionError(f"{label} must contain a YAML object")
    return loaded, value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PilotSelectionError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise PilotSelectionError(f"{label} must be an array")
    return value


def _finite(
    value: object,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PilotSelectionError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise PilotSelectionError(f"{label} must be a finite number")
    if minimum is not None:
        if exclusive_minimum and number <= minimum:
            raise PilotSelectionError(f"{label} must be greater than {minimum}")
        if not exclusive_minimum and number < minimum:
            raise PilotSelectionError(f"{label} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise PilotSelectionError(f"{label} must be at most {maximum}")
    return number


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PilotSelectionError(f"{label} must be a positive integer")
    return value


def _rate(value: object, label: str) -> float:
    return _finite(value, label, minimum=0.0, maximum=1.0)


def _required(value: Mapping[str, Any], field: str, label: str) -> Any:
    if field not in value:
        raise PilotSelectionError(f"{label} is missing {field}")
    return value[field]


def _dig(value: Mapping[str, Any], path: str, label: str) -> Any:
    current: object = value
    for part in path.split("."):
        current = _mapping(current, label)
        if part not in current:
            raise PilotSelectionError(f"{label} is missing {path}")
        current = current[part]
    return current


def _checkpoint_step(value: object, label: str) -> int:
    if not isinstance(value, str):
        raise PilotSelectionError(f"{label} must be checkpoint-<step>")
    match = _CHECKPOINT.fullmatch(value)
    if match is None:
        raise PilotSelectionError(f"{label} must be checkpoint-<step>")
    return int(match.group(1))


def _tree_hash(files: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _verify_model_tree(
    candidate: PilotCandidateInput,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    root_input = Path(candidate.model_tree)
    if root_input.is_symlink():
        raise PilotSelectionError(f"{candidate.candidate_id} model tree must not be a symlink")
    root = root_input.resolve()
    if not root.is_dir():
        raise PilotSelectionError(f"{candidate.candidate_id} model tree is not a directory")
    final_model = _mapping(
        _required(report, "final_model", f"{candidate.candidate_id} training report"),
        f"{candidate.candidate_id} final_model",
    )
    if Path(str(_required(final_model, "path", "final_model"))).resolve() != root:
        raise PilotSelectionError(f"{candidate.candidate_id} training report final-model path differs from input")
    if final_model.get("directory") != root.name:
        raise PilotSelectionError(f"{candidate.candidate_id} training report final-model directory differs from input")
    if final_model.get("run_fingerprint") != report.get("run_fingerprint"):
        raise PilotSelectionError(f"{candidate.candidate_id} final-model run fingerprint differs from training report")
    reload_report = _mapping(
        _required(final_model, "reload_verification", "final_model"),
        f"{candidate.candidate_id} final reload verification",
    )
    if reload_report.get("status") != "passed" or reload_report.get("run_fingerprint") != report.get("run_fingerprint"):
        raise PilotSelectionError(f"{candidate.candidate_id} final model lacks a bound passed fresh reload")
    inventory = dict(
        _mapping(
            _required(final_model, "inventory", "final_model"),
            f"{candidate.candidate_id} final inventory",
        )
    )
    inventory_file, inventory_value = _load_json(
        root / _INVENTORY_FILENAME,
        f"{candidate.candidate_id} model inventory file",
    )
    if inventory_value != inventory:
        raise PilotSelectionError(f"{candidate.candidate_id} model inventory file differs from training report")
    if inventory_file.payload != canonical_json_bytes(inventory):
        raise PilotSelectionError(f"{candidate.candidate_id} model inventory file is not canonical JSON")
    files_raw = _sequence(inventory.get("files"), f"{candidate.candidate_id} inventory files")
    files: list[dict[str, Any]] = []
    prior_path = ""
    for index, raw in enumerate(files_raw):
        item = _mapping(raw, f"{candidate.candidate_id} inventory file {index}")
        relative = item.get("path")
        if not isinstance(relative, str) or not relative:
            raise PilotSelectionError(f"{candidate.candidate_id} inventory file path must be non-empty")
        normalized = Path(relative)
        if not _is_portable_relative_inventory_path(relative) or relative == _INVENTORY_FILENAME:
            raise PilotSelectionError(f"{candidate.candidate_id} inventory contains unsafe path {relative!r}")
        if relative <= prior_path:
            raise PilotSelectionError(f"{candidate.candidate_id} inventory paths must be strictly sorted")
        prior_path = relative
        declared_bytes = _positive_int(
            item.get("bytes"),
            f"{candidate.candidate_id} inventory bytes for {relative}",
        )
        declared_hash = _normalize_hash(
            item.get("sha256"),
            f"{candidate.candidate_id} inventory hash for {relative}",
        )
        physical = _stable_file(
            root / Path(*normalized.parts),
            f"{candidate.candidate_id} model file {relative}",
        )
        try:
            physical.path.relative_to(root)
        except ValueError as error:
            raise PilotSelectionError(f"{candidate.candidate_id} inventory path escapes model tree") from error
        if len(physical.payload) != declared_bytes or physical.sha256 != declared_hash:
            raise PilotSelectionError(f"{candidate.candidate_id} model file differs from inventory: {relative}")
        files.append({"path": relative, "bytes": declared_bytes, "sha256": declared_hash})
    actual_paths: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise PilotSelectionError(f"{candidate.candidate_id} model tree contains symlink: {path}")
        if path.is_file():
            actual_paths.append(path.relative_to(root).as_posix())
    expected_paths = [item["path"] for item in files] + [_INVENTORY_FILENAME]
    if sorted(actual_paths) != sorted(expected_paths):
        raise PilotSelectionError(f"{candidate.candidate_id} model tree contains unbound or missing files")
    if inventory.get("file_count") != len(files):
        raise PilotSelectionError(f"{candidate.candidate_id} inventory file count is inconsistent")
    total_bytes = sum(int(item["bytes"]) for item in files)
    if inventory.get("total_bytes") != total_bytes:
        raise PilotSelectionError(f"{candidate.candidate_id} inventory byte count is inconsistent")
    tree_sha256 = _tree_hash(files)
    if (
        _normalize_hash(
            inventory.get("tree_sha256"),
            f"{candidate.candidate_id} model tree hash",
        )
        != tree_sha256
    ):
        raise PilotSelectionError(f"{candidate.candidate_id} model tree hash is inconsistent")
    if not any(item["path"].endswith(".safetensors") for item in files):
        raise PilotSelectionError(f"{candidate.candidate_id} model tree has no safetensors weights")
    return {
        "path": str(root),
        "directory": root.name,
        "tree_sha256": tree_sha256,
        "inventory_sha256": inventory_file.sha256,
        "file_count": len(files),
        "total_bytes": total_bytes,
    }


def _training_fairness_binding(report: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    label = f"{candidate_id} training report"
    bindings = _mapping(_required(report, "bindings", label), f"{candidate_id} bindings")
    config_binding = _mapping(
        _required(bindings, "config", f"{candidate_id} bindings"),
        f"{candidate_id} config binding",
    )
    config_values = _mapping(
        _required(config_binding, "values", f"{candidate_id} config binding"),
        f"{candidate_id} config values",
    )
    dataset = _mapping(_required(bindings, "dataset", f"{candidate_id} bindings"), label)
    dataset_manifest = _mapping(_required(dataset, "manifest", f"{candidate_id} dataset"), label)
    train = _mapping(_required(dataset, "train", f"{candidate_id} dataset"), label)
    validation = _mapping(_required(dataset, "validation", f"{candidate_id} dataset"), label)
    effective = _mapping(_required(bindings, "effective_training", f"{candidate_id} bindings"), label)
    runtime = _mapping(_required(bindings, "runtime", f"{candidate_id} bindings"), label)
    tokenizer = _mapping(_required(bindings, "tokenizer", f"{candidate_id} bindings"), label)
    schedule = _mapping(_required(effective, "schedule", f"{candidate_id} effective training"), label)
    code = _mapping(_required(bindings, "code", f"{candidate_id} bindings"), label)
    return {
        "base": {
            "model": report.get("base_model"),
            "revision": report.get("base_revision"),
        },
        "seed": report.get("seed"),
        "data": {
            "manifest_sha256": _normalize_hash(dataset_manifest.get("sha256"), f"{candidate_id} dataset manifest"),
            "declared_train_sha256": _normalize_hash(
                dataset_manifest.get("declared_train_sha256"),
                f"{candidate_id} declared train",
            ),
            "declared_validation_sha256": _normalize_hash(
                dataset_manifest.get("declared_validation_sha256"),
                f"{candidate_id} declared validation",
            ),
            "train_sha256": _normalize_hash(train.get("sha256"), f"{candidate_id} train data"),
            "validation_sha256": _normalize_hash(validation.get("sha256"), f"{candidate_id} validation data"),
            "train_total_records": train.get("total_records"),
            "train_selected_records": train.get("selected_records"),
            "validation_total_records": validation.get("total_records"),
            "validation_selected_records": validation.get("selected_records"),
            "train_examples_loaded": report.get("train_examples_loaded"),
            "train_examples_used": report.get("train_examples_used"),
            "validation_examples_loaded": report.get("validation_examples_loaded"),
            "validation_examples_used": report.get("validation_examples_used"),
            "oversize_rejections": report.get("oversize_rejections"),
            "synthetic_only": dataset_manifest.get("synthetic_only"),
            "generative_api": dataset_manifest.get("generative_api"),
            "human_curation": dataset_manifest.get("human_curation"),
            "split_leakage": dataset_manifest.get("split_leakage"),
        },
        "tokenization": {
            "max_sequence_length": report.get("max_sequence_length"),
            "identity": dict(tokenizer),
        },
        "schedule_and_steps": {
            "configured_max_steps": report.get("max_steps"),
            "schedule": dict(schedule),
            "epochs": config_values.get("epochs"),
            "eval_steps": config_values.get("eval_steps"),
            "save_steps": config_values.get("save_steps"),
            "early_stopping": config_values.get("early_stopping"),
            "early_stopping_patience": config_values.get("early_stopping_patience"),
        },
        "hardware": {
            "gpu": report.get("gpu"),
            "cuda_runtime": report.get("cuda_runtime"),
            "torch_version": report.get("torch_version"),
            "runtime": dict(runtime),
            "bf16": report.get("bf16"),
            "sdpa": report.get("sdpa"),
        },
        "method": {
            "run_kind": report.get("run_kind"),
            "method": report.get("method"),
            "optimizer": report.get("optimizer"),
            "micro_batch_size": report.get("micro_batch_size"),
            "gradient_accumulation_steps": report.get("gradient_accumulation_steps"),
            "effective_batch_size": report.get("effective_batch_size"),
            "gradient_checkpointing": effective.get("gradient_checkpointing"),
            "completion_only_loss": effective.get("completion_only_loss"),
            "assistant_output_loss_only": config_values.get("assistant_output_loss_only"),
            "max_grad_norm": config_values.get("max_grad_norm"),
            "warmup_ratio": config_values.get("warmup_ratio"),
            "weight_decay": config_values.get("weight_decay"),
        },
        "code": {
            "code_tree_sha256": _normalize_hash(code.get("code_tree_sha256"), f"{candidate_id} code tree"),
            "git_head": code.get("git_head"),
            "dirty": code.get("dirty"),
            "worktree_diff_sha256": _normalize_hash(code.get("worktree_diff_sha256"), f"{candidate_id} worktree diff"),
            "worktree_status_sha256": _normalize_hash(
                code.get("worktree_status_sha256"), f"{candidate_id} worktree status"
            ),
            "files": code.get("files"),
        },
        "config": {
            "sha256": _normalize_hash(config_binding.get("sha256"), f"{candidate_id} training config"),
            "schema_version": config_values.get("schema_version"),
            "learning_rate_candidates": config_values.get("learning_rate_candidates"),
            "max_sequence_length_candidates": config_values.get("max_sequence_length_candidates"),
        },
    }


def _validate_training_report(
    candidate: PilotCandidateInput,
    report: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], float]:
    label = f"{candidate.candidate_id} training report"
    if report.get("schema_version") != 2:
        raise PilotSelectionError(f"{label} must use schema_version=2")
    if (
        report.get("training_completed") is not True
        or report.get("run_kind") != "pilot"
        or report.get("method") != "full_sft"
        or report.get("bf16") is not True
    ):
        raise PilotSelectionError(f"{label} is not a completed BF16 full-SFT pilot")
    run_fingerprint = _normalize_hash(report.get("run_fingerprint"), f"{candidate.candidate_id} run fingerprint")
    learning_rate = _finite(
        report.get("learning_rate"),
        f"{candidate.candidate_id} learning rate",
        minimum=0.0,
        exclusive_minimum=True,
    )
    effective_lr = _finite(
        _dig(
            report,
            "bindings.effective_training.learning_rate",
            f"{candidate.candidate_id} bindings",
        ),
        f"{candidate.candidate_id} effective learning rate",
        minimum=0.0,
        exclusive_minimum=True,
    )
    cli_lr = _finite(
        _dig(
            report,
            "bindings.cli.learning_rate_override",
            f"{candidate.candidate_id} bindings",
        ),
        f"{candidate.candidate_id} CLI learning rate",
        minimum=0.0,
        exclusive_minimum=True,
    )
    if learning_rate != effective_lr or learning_rate != cli_lr:
        raise PilotSelectionError(f"{candidate.candidate_id} learning-rate bindings disagree")
    max_steps = _positive_int(report.get("max_steps"), f"{candidate.candidate_id} max_steps")
    checkpoint_state = _mapping(report.get("checkpoint_state"), f"{candidate.candidate_id} checkpoint state")
    actual_step = _positive_int(
        checkpoint_state.get("global_step"),
        f"{candidate.candidate_id} actual global step",
    )
    if actual_step > max_steps:
        raise PilotSelectionError(f"{candidate.candidate_id} actual global step exceeds configured maximum")
    last_checkpoint = checkpoint_state.get("last_checkpoint")
    last_step = _checkpoint_step(last_checkpoint, f"{candidate.candidate_id} last checkpoint")
    if last_step != actual_step:
        raise PilotSelectionError(f"{candidate.candidate_id} actual step differs from last checkpoint")
    best_checkpoint = checkpoint_state.get("best_model_checkpoint")
    best_step = _checkpoint_step(best_checkpoint, f"{candidate.candidate_id} best checkpoint")
    best_metric = _finite(
        checkpoint_state.get("best_metric"),
        f"{candidate.candidate_id} best eval loss",
        minimum=0.0,
        exclusive_minimum=True,
    )
    surviving = _sequence(
        checkpoint_state.get("surviving_checkpoints"),
        f"{candidate.candidate_id} surviving checkpoints",
    )
    surviving_names: list[str] = []
    for raw in surviving:
        entry = _mapping(raw, f"{candidate.candidate_id} surviving checkpoint")
        name = entry.get("checkpoint")
        step = _checkpoint_step(name, f"{candidate.candidate_id} surviving checkpoint")
        if entry.get("global_step") != step:
            raise PilotSelectionError(f"{candidate.candidate_id} surviving checkpoint step is inconsistent")
        if entry.get("run_fingerprint") != run_fingerprint:
            raise PilotSelectionError(f"{candidate.candidate_id} surviving checkpoint run differs")
        surviving_names.append(str(name))
    if best_checkpoint not in surviving_names or last_checkpoint not in surviving_names:
        raise PilotSelectionError(f"{candidate.candidate_id} best/last checkpoint is not surviving")
    model_tree = _verify_model_tree(candidate, report)
    outcome = {
        "candidate_id": candidate.candidate_id,
        "learning_rate": learning_rate,
        "configured_max_steps": max_steps,
        "actual_global_step": actual_step,
        "early_stopping_triggered": actual_step < max_steps,
        "best_checkpoint": best_checkpoint,
        "best_checkpoint_step": best_step,
        "best_eval_loss": best_metric,
        "last_checkpoint": last_checkpoint,
        "model_tree_sha256": model_tree["tree_sha256"],
    }
    if report.get("early_stopping") is not True:
        raise PilotSelectionError(f"{candidate.candidate_id} did not use early stopping")
    return (
        _training_fairness_binding(report, candidate.candidate_id),
        outcome,
        model_tree,
        learning_rate,
    )


def _reference_manifest_binding(
    loaded: _LoadedFile,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "scriber_evaluation_reference_bundle":
        raise PilotSelectionError("reference manifest is not a combined reference bundle")
    record_count = _positive_int(manifest.get("record_count"), "reference record count")
    records_sha256 = _normalize_hash(manifest.get("records_sha256"), "reference records hash")
    case_ids_sha256 = _normalize_hash(manifest.get("case_ids_sha256"), "reference case-ID hash")
    inputs = _sequence(manifest.get("inputs"), "reference manifest inputs")
    if len(inputs) < 2:
        raise PilotSelectionError("combined reference manifest must contain multiple splits")
    positions = [_mapping(entry, "reference manifest input").get("position") for entry in inputs]
    if positions != list(range(len(inputs))):
        raise PilotSelectionError("reference manifest positions must be contiguous and ordered")
    split_records = []
    for raw in inputs:
        item = _mapping(raw, "reference manifest input")
        split_records.append(
            {
                "records_sha256": _normalize_hash(item.get("records_sha256"), "split reference records hash"),
                "record_count": _positive_int(item.get("record_count"), "split reference record count"),
                "manifest_sha256": _normalize_hash(item.get("manifest_sha256"), "split reference manifest hash"),
            }
        )
    if sum(item["record_count"] for item in split_records) != record_count:
        raise PilotSelectionError("reference split counts do not match combined count")
    return {
        "manifest_sha256": loaded.sha256,
        "records_sha256": records_sha256,
        "case_ids_sha256": case_ids_sha256,
        "record_count": record_count,
        "split_inputs": split_records,
    }


def _evaluation_config_binding(
    loaded: _LoadedFile,
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
    if config.get("schema_version") != 2:
        raise PilotSelectionError("evaluation config must use schema_version=2")
    automatic = _mapping(config.get("automatic_evaluation"), "automatic evaluation config")
    hard_gates = tuple(
        _mapping(item, "evaluation hard gate")
        for item in _sequence(automatic.get("hard_gates"), "evaluation hard gates")
    )
    if not hard_gates:
        raise PilotSelectionError("evaluation config has no hard gates")
    ids: list[str] = []
    normalized: list[dict[str, Any]] = []
    for gate in hard_gates:
        gate_id = gate.get("id")
        if not isinstance(gate_id, str) or not gate_id:
            raise PilotSelectionError("evaluation hard gate has no ID")
        ids.append(gate_id)
        normalized.append(
            {
                "id": gate_id,
                "slice": gate.get("slice"),
                "metric": gate.get("metric"),
                "operator": gate.get("operator"),
                "threshold": gate.get("threshold"),
                "minimum_cases": gate.get("minimum_cases"),
            }
        )
    if len(ids) != len(set(ids)):
        raise PilotSelectionError("evaluation config has duplicate hard-gate IDs")
    return {
        "config_sha256": loaded.sha256,
        "schema_version": 2,
        "hard_gate_ids": ids,
    }, tuple(normalized)


def _validate_part_manifest(
    candidate: PilotCandidateInput,
    path: Path,
    *,
    expected: Mapping[str, Any],
    model_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    loaded, manifest = _load_json(path, f"{candidate.candidate_id} prediction part manifest")
    if loaded.path.name != expected.get("manifest_filename"):
        raise PilotSelectionError(f"{candidate.candidate_id} prediction part filename differs from bundle")
    if loaded.sha256 != _normalize_hash(
        expected.get("manifest_sha256"),
        f"{candidate.candidate_id} prediction part manifest hash",
    ):
        raise PilotSelectionError(f"{candidate.candidate_id} prediction part manifest hash differs from bundle")
    record_hash = _normalize_hash(manifest.get("prediction_sha256"), "prediction part records hash")
    if record_hash != _normalize_hash(expected.get("records_sha256"), "bundled prediction part records hash"):
        raise PilotSelectionError(f"{candidate.candidate_id} prediction part record hash differs from bundle")
    record_count = _positive_int(manifest.get("record_count"), "prediction part record count")
    if record_count != expected.get("record_count"):
        raise PilotSelectionError(f"{candidate.candidate_id} prediction part count differs from bundle")
    if Path(str(manifest.get("model"))).resolve() != model_root:
        raise PilotSelectionError(f"{candidate.candidate_id} prediction part was not generated by its model tree")
    return {
        "manifest_sha256": loaded.sha256,
        "prediction_sha256": record_hash,
        "reference_sha256": _normalize_hash(manifest.get("reference_sha256"), "prediction part reference hash"),
        "record_count": record_count,
    }, {
        "device": manifest.get("device"),
        "quantization": manifest.get("quantization"),
        "revision": manifest.get("revision"),
        "reference_sha256": _normalize_hash(manifest.get("reference_sha256"), "prediction part reference hash"),
        "record_count": record_count,
    }


def _validate_prediction_manifest(
    candidate: PilotCandidateInput,
    manifest_loaded: _LoadedFile,
    manifest: Mapping[str, Any],
    reference: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
    label = f"{candidate.candidate_id} prediction manifest"
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "scriber_evaluation_prediction_bundle"
        or manifest.get("exact_case_coverage") is not True
    ):
        raise PilotSelectionError(f"{label} is not an exact combined prediction bundle")
    reference_binding = _mapping(manifest.get("reference"), label)
    for field in ("manifest_sha256", "records_sha256", "case_ids_sha256", "record_count"):
        expected = reference[field]
        actual = reference_binding.get(field)
        if field.endswith("sha256"):
            actual = _normalize_hash(actual, f"{label} reference {field}")
        if actual != expected:
            raise PilotSelectionError(f"{label} reference {field} differs")
    combined_hash = _normalize_hash(manifest.get("prediction_sha256"), f"{label} records hash")
    if combined_hash != _normalize_hash(manifest.get("records_sha256"), f"{label} alternate records hash"):
        raise PilotSelectionError(f"{label} declares conflicting prediction hashes")
    record_count = _positive_int(manifest.get("record_count"), f"{label} record count")
    if record_count != reference["record_count"]:
        raise PilotSelectionError(f"{label} count differs from references")
    inputs = [_mapping(item, f"{label} input") for item in _sequence(manifest.get("inputs"), f"{label} inputs")]
    if len(inputs) != len(candidate.prediction_part_manifests):
        raise PilotSelectionError(f"{label} part count differs from supplied part manifests")
    if [item.get("position") for item in inputs] != list(range(len(inputs))):
        raise PilotSelectionError(f"{label} input positions are not contiguous")
    supplied = {Path(path).name: Path(path) for path in candidate.prediction_part_manifests}
    if len(supplied) != len(candidate.prediction_part_manifests):
        raise PilotSelectionError(f"{label} supplied part manifest basenames are not unique")
    expected_names = [str(item.get("manifest_filename")) for item in inputs]
    if set(supplied) != set(expected_names):
        raise PilotSelectionError(f"{label} supplied part manifests are not exact")
    model_root = Path(candidate.model_tree).resolve()
    part_bindings: list[dict[str, Any]] = []
    inference_bindings: list[dict[str, Any]] = []
    part_hashes: set[str] = set()
    for item in inputs:
        part, inference = _validate_part_manifest(
            candidate,
            supplied[str(item["manifest_filename"])],
            expected=item,
            model_root=model_root,
        )
        part_bindings.append(part)
        inference_bindings.append(inference)
        part_hashes.add(part["manifest_sha256"])
    if sum(item["record_count"] for item in part_bindings) != record_count:
        raise PilotSelectionError(f"{label} part counts do not match combined count")
    reference_hashes = {item["records_sha256"] for item in reference["split_inputs"]}
    if {item["reference_sha256"] for item in part_bindings} != reference_hashes:
        raise PilotSelectionError(f"{label} parts do not cover the exact reference split hashes")
    return (
        {
            "manifest_sha256": manifest_loaded.sha256,
            "prediction_sha256": combined_hash,
            "record_count": record_count,
            "candidate_label": manifest.get("candidate"),
            "part_manifests": part_bindings,
        },
        {"parts": inference_bindings},
        part_hashes,
    )


def _operator_passed(actual: float, operator: object, threshold: object) -> bool:
    threshold_number = _finite(threshold, "hard-gate threshold")
    if operator == "gte":
        return actual >= threshold_number
    if operator == "lte":
        return actual <= threshold_number
    if operator == "eq":
        return actual == threshold_number
    raise PilotSelectionError(f"unsupported hard-gate operator {operator!r}")


def _validate_hard_gates(
    evaluation: Mapping[str, Any],
    expected_gates: Sequence[Mapping[str, Any]],
    candidate_id: str,
) -> tuple[dict[str, bool], list[str]]:
    gate_report = _mapping(evaluation.get("gate"), f"{candidate_id} gate report")
    if gate_report.get("schema_version") != 1:
        raise PilotSelectionError(f"{candidate_id} gate report schema is invalid")
    hard = _mapping(gate_report.get("hard_gates"), f"{candidate_id} hard gates")
    expected_by_id = {str(gate["id"]): gate for gate in expected_gates}
    if set(hard) != set(expected_by_id):
        raise PilotSelectionError(f"{candidate_id} evaluated hard-gate set differs from config")
    results: dict[str, bool] = {}
    for gate_id in sorted(expected_by_id):
        expected = expected_by_id[gate_id]
        result = _mapping(hard[gate_id], f"{candidate_id} hard gate {gate_id}")
        for field in ("id", "slice", "metric", "operator", "threshold", "minimum_cases"):
            if result.get(field) != expected.get(field):
                raise PilotSelectionError(f"{candidate_id} hard gate {gate_id} changed configured field {field}")
        observed = result.get("observed_cases")
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise PilotSelectionError(f"{candidate_id} hard gate {gate_id} has invalid observed count")
        minimum_cases = _positive_int(
            result.get("minimum_cases"),
            f"{candidate_id} hard gate {gate_id} minimum cases",
        )
        actual_raw = result.get("actual")
        expected_pass = False
        if observed >= minimum_cases:
            actual = _finite(actual_raw, f"{candidate_id} hard gate {gate_id} actual")
            expected_pass = _operator_passed(actual, result.get("operator"), result.get("threshold"))
        elif actual_raw is not None:
            raise PilotSelectionError(f"{candidate_id} hard gate {gate_id} has a value without enough cases")
        if result.get("passed") is not expected_pass:
            raise PilotSelectionError(f"{candidate_id} hard gate {gate_id} pass result is inconsistent")
        results[gate_id] = expected_pass
    failed = [gate_id for gate_id in sorted(results) if not results[gate_id]]
    return results, failed


def _mean(values: Mapping[str, float], label: str) -> float:
    if not values:
        raise PilotSelectionError(f"{label} has no component metrics")
    return sum(values.values()) / len(values)


def _score_components(metrics: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    total = _positive_int(metrics.get("total"), f"{candidate_id} evaluated cases")
    safety_values = {name: _rate(metrics.get(name), f"{candidate_id} metric {name}") for name in _SAFETY_DIRECT_METRICS}
    hard_content = _mapping(
        metrics.get("hard_content_exact_rates"),
        f"{candidate_id} hard-content rates",
    )
    if not hard_content:
        raise PilotSelectionError(f"{candidate_id} has no hard-content rates")
    hard_content_values = {
        str(name): _rate(value, f"{candidate_id} hard-content rate {name}")
        for name, value in sorted(hard_content.items())
    }
    critical_errors = _mapping(metrics.get("critical_errors"), f"{candidate_id} critical errors")
    semantic_errors = _mapping(
        metrics.get("semantic_hard_check_counts"),
        f"{candidate_id} semantic hard checks",
    )

    def error_integrity(values: Mapping[str, Any], label: str) -> float:
        count = 0
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PilotSelectionError(f"{candidate_id} {label} {name} must be a non-negative integer")
            count += value
        return max(0.0, min(1.0, 1.0 - (count / total)))

    safety_values["hard_content_mean"] = _mean(hard_content_values, f"{candidate_id} hard content")
    safety_values["critical_integrity"] = error_integrity(critical_errors, "critical error")
    safety_values["semantic_integrity"] = error_integrity(semantic_errors, "semantic hard check")
    task_values = {name: _rate(metrics.get(name), f"{candidate_id} metric {name}") for name in _TASK_DIRECT_METRICS}
    for name in _TASK_ERROR_METRICS:
        error_rate = _finite(metrics.get(name), f"{candidate_id} metric {name}", minimum=0.0)
        task_values[f"one_minus_{name}"] = max(0.0, min(1.0, 1.0 - error_rate))
    structure_values = {name: _rate(metrics.get(name), f"{candidate_id} metric {name}") for name in _STRUCTURE_METRICS}
    return {
        "safety_content": {
            "score": _mean(safety_values, f"{candidate_id} safety/content"),
            "values": safety_values,
        },
        "task": {
            "score": _mean(task_values, f"{candidate_id} task"),
            "values": task_values,
        },
        "structure": {
            "score": _mean(structure_values, f"{candidate_id} structure"),
            "values": structure_values,
        },
    }


def _validate_evaluation(
    candidate: PilotCandidateInput,
    loaded: _LoadedFile,
    evaluation: Mapping[str, Any],
    *,
    evaluation_config_sha256: str,
    reference: Mapping[str, Any],
    prediction: Mapping[str, Any],
    hard_gates: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, bool], list[str]]:
    label = f"{candidate.candidate_id} evaluation report"
    if evaluation.get("schema_version") != 1 or evaluation.get("exact_case_coverage") is not True:
        raise PilotSelectionError(f"{label} is not a complete exact-coverage report")
    if _normalize_hash(evaluation.get("evaluation_config_sha256"), f"{label} config hash") != evaluation_config_sha256:
        raise PilotSelectionError(f"{label} used a different evaluation config")
    if _normalize_hash(evaluation.get("reference_sha256"), f"{label} reference hash") != reference["records_sha256"]:
        raise PilotSelectionError(f"{label} used different references")
    if (
        _normalize_hash(evaluation.get("prediction_sha256"), f"{label} prediction hash")
        != prediction["prediction_sha256"]
    ):
        raise PilotSelectionError(f"{label} used different predictions")
    if evaluation.get("candidate") != prediction["candidate_label"]:
        raise PilotSelectionError(f"{label} candidate label differs from prediction bundle")
    metrics = _mapping(evaluation.get("metrics"), f"{candidate.candidate_id} metrics")
    if metrics.get("total") != reference["record_count"]:
        raise PilotSelectionError(f"{label} metric count differs from references")
    results, failed = _validate_hard_gates(evaluation, hard_gates, candidate.candidate_id)
    components = _score_components(metrics, candidate.candidate_id)
    return (
        {
            "report_sha256": loaded.sha256,
            "hard_gates": results,
            "failed_hard_gates": failed,
            "hard_gates_passed": not failed,
            "components": components,
        },
        results,
        failed,
    )


@cache
def _schema_validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _validate_diagnostic_schema(value: Mapping[str, Any], candidate_id: str) -> None:
    errors = sorted(
        _schema_validator(_PROTECTED_DIAGNOSTIC_SCHEMA).iter_errors(dict(value)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        path = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise PilotSelectionError(
            f"{candidate_id} protected-span diagnostic failed schema at {path}: {errors[0].message}"
        )


def _validate_diagnostic_evaluation(
    candidate_id: str,
    evaluation: Mapping[str, Any],
    *,
    config_sha256: str,
    reference_split_hashes: set[str],
    prediction_part_hashes: set[str],
) -> None:
    if evaluation.get("schema_version") != 1 or evaluation.get("exact_case_coverage") is not True:
        raise PilotSelectionError(f"{candidate_id} diagnostic evaluation is not complete")
    if (
        _normalize_hash(
            evaluation.get("evaluation_config_sha256"),
            f"{candidate_id} diagnostic evaluation config",
        )
        != config_sha256
    ):
        raise PilotSelectionError(f"{candidate_id} diagnostic evaluation used another config")
    if (
        _normalize_hash(
            evaluation.get("reference_sha256"),
            f"{candidate_id} diagnostic evaluation reference",
        )
        not in reference_split_hashes
    ):
        raise PilotSelectionError(f"{candidate_id} diagnostic evaluation used an unbound reference split")
    if (
        _normalize_hash(
            evaluation.get("prediction_sha256"),
            f"{candidate_id} diagnostic evaluation prediction",
        )
        not in prediction_part_hashes
    ):
        raise PilotSelectionError(f"{candidate_id} diagnostic evaluation used an unbound prediction part")


def _validate_protected_diagnostic(
    candidate: PilotCandidateInput,
    diagnostic_loaded: _LoadedFile,
    diagnostic: Mapping[str, Any],
    diagnostic_evaluation_loaded: _LoadedFile,
    diagnostic_evaluation: Mapping[str, Any],
    *,
    training_report: Mapping[str, Any],
    config_sha256: str,
    reference: Mapping[str, Any],
    prediction_part_manifest_hashes: set[str],
    prediction_part_record_hashes: set[str],
) -> dict[str, Any]:
    _validate_diagnostic_schema(diagnostic, candidate.candidate_id)
    _validate_diagnostic_evaluation(
        candidate.candidate_id,
        diagnostic_evaluation,
        config_sha256=config_sha256,
        reference_split_hashes={item["records_sha256"] for item in reference["split_inputs"]},
        prediction_part_hashes=prediction_part_record_hashes,
    )
    inputs = _mapping(diagnostic.get("inputs"), f"{candidate.candidate_id} diagnostic inputs")
    if diagnostic.get("candidate") != diagnostic_evaluation.get("candidate"):
        raise PilotSelectionError(f"{candidate.candidate_id} diagnostic candidate label is inconsistent")
    internal_eval = _mapping(
        inputs.get("evaluation_report"),
        f"{candidate.candidate_id} diagnostic evaluation input",
    )
    if (
        _normalize_hash(internal_eval.get("sha256"), f"{candidate.candidate_id} diagnostic evaluation hash")
        != diagnostic_evaluation_loaded.sha256
    ):
        raise PilotSelectionError(f"{candidate.candidate_id} diagnostic does not bind its evaluation report")
    internal_manifest = _mapping(
        inputs.get("prediction_manifest"),
        f"{candidate.candidate_id} diagnostic prediction manifest",
    )
    if (
        _normalize_hash(
            internal_manifest.get("sha256"),
            f"{candidate.candidate_id} diagnostic prediction manifest hash",
        )
        not in prediction_part_manifest_hashes
    ):
        raise PilotSelectionError(f"{candidate.candidate_id} diagnostic uses an unbound prediction manifest")
    internal_predictions = _mapping(inputs.get("predictions"), f"{candidate.candidate_id} diagnostic predictions")
    if (
        _normalize_hash(
            internal_predictions.get("sha256"),
            f"{candidate.candidate_id} diagnostic prediction hash",
        )
        not in prediction_part_record_hashes
    ):
        raise PilotSelectionError(f"{candidate.candidate_id} diagnostic uses unbound predictions")
    internal_references = _mapping(inputs.get("references"), f"{candidate.candidate_id} diagnostic references")
    reference_by_hash = {item["records_sha256"]: item for item in reference["split_inputs"]}
    diagnostic_reference_hash = _normalize_hash(
        internal_references.get("sha256"),
        f"{candidate.candidate_id} diagnostic reference hash",
    )
    if diagnostic_reference_hash not in reference_by_hash:
        raise PilotSelectionError(f"{candidate.candidate_id} diagnostic uses an unbound reference split")
    expected_reference_count = reference_by_hash[diagnostic_reference_hash]["record_count"]
    if internal_references.get("record_count") != expected_reference_count:
        raise PilotSelectionError(f"{candidate.candidate_id} diagnostic reference count is inconsistent")
    if internal_predictions.get("record_count") != expected_reference_count:
        raise PilotSelectionError(f"{candidate.candidate_id} diagnostic prediction count is inconsistent")
    if (
        _normalize_hash(
            diagnostic_evaluation.get("reference_sha256"),
            f"{candidate.candidate_id} diagnostic evaluation reference",
        )
        != diagnostic_reference_hash
    ):
        raise PilotSelectionError(f"{candidate.candidate_id} diagnostic evaluation reference differs")
    training_data = _mapping(inputs.get("training_data"), f"{candidate.candidate_id} diagnostic training data")
    validation_data = _mapping(
        inputs.get("validation_data"),
        f"{candidate.candidate_id} diagnostic validation data",
    )
    expected_train = _normalize_hash(
        _dig(
            training_report,
            "bindings.dataset.train.sha256",
            f"{candidate.candidate_id} training report",
        ),
        f"{candidate.candidate_id} training data hash",
    )
    expected_validation = _normalize_hash(
        _dig(
            training_report,
            "bindings.dataset.validation.sha256",
            f"{candidate.candidate_id} training report",
        ),
        f"{candidate.candidate_id} validation data hash",
    )
    if (
        _normalize_hash(training_data.get("sha256"), f"{candidate.candidate_id} diagnostic training hash")
        != expected_train
        or _normalize_hash(
            validation_data.get("sha256"),
            f"{candidate.candidate_id} diagnostic validation hash",
        )
        != expected_validation
    ):
        raise PilotSelectionError(f"{candidate.candidate_id} diagnostic uses different training data")
    counts = _mapping(diagnostic.get("counts"), f"{candidate.candidate_id} diagnostic counts")
    exposure = _mapping(
        diagnostic.get("training_exposure"),
        f"{candidate.candidate_id} diagnostic training exposure",
    )
    external = {
        "failure_kind": "inference_placeholders_absent_from_training_targets",
        "training_data_sha256": expected_train,
        "validation_data_sha256": expected_validation,
        "tokenization": diagnostic.get("tokenization"),
        "training_exposure": diagnostic.get("training_exposure"),
        "maskability_by_split": diagnostic.get("maskability_by_split"),
    }
    proves_failure = (
        counts.get("manifest_reconciled") is True
        and counts.get("evaluation_cases") == expected_reference_count
        and _dig(
            diagnostic_evaluation,
            "metrics.total",
            f"{candidate.candidate_id} diagnostic evaluation",
        )
        == expected_reference_count
        and isinstance(counts.get("inferred_restoration_failures"), int)
        and counts["inferred_restoration_failures"] > 0
        and exposure.get("literal_marker_record_count") == 0
        and isinstance(exposure.get("paired_placeholder_eligible_record_count"), int)
        and exposure["paired_placeholder_eligible_record_count"] > 0
    )
    return {
        "diagnostic_sha256": diagnostic_loaded.sha256,
        "diagnostic_evaluation_sha256": diagnostic_evaluation_loaded.sha256,
        "proves_external_failure": proves_failure,
        "external_evidence": external,
        "external_failure_signature_sha256": canonical_json_sha256(external),
    }


def _parent_binding(
    row: Mapping[str, Any],
    input_binding: Mapping[str, Any],
    *,
    release_eligible: bool,
    remediation: bool = False,
    external_failure_signature_sha256: str | None = None,
) -> dict[str, Any]:
    model_tree = _mapping(input_binding["model_tree"], "candidate model tree")
    result: dict[str, Any] = {
        "candidate_id": row["candidate_id"],
        "learning_rate": row["learning_rate"],
        "training_report_sha256": input_binding["training_report_sha256"],
        "model_tree": dict(model_tree),
        "release_eligible": release_eligible,
    }
    if remediation:
        result.update(
            {
                "scope": "remediation_only_not_release",
                "selection_basis": "composite_then_loss_tie_breaks",
                "external_failure_signature_sha256": external_failure_signature_sha256,
            }
        )
    return result


def _sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -int(bool(row["hard_gates_passed"])),
        -float(row["composite_score"]),
        -float(row["components"]["safety_content"]["score"]),
        -float(row["components"]["task"]["score"]),
        -float(row["components"]["structure"]["score"]),
        -float(row["normalized_eval_loss"]),
        float(row["eval_loss"]),
        float(row["learning_rate"]),
        str(row["candidate_id"]),
    )


def validate_pilot_selection_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed report schema and frozen decision policy."""

    materialized = json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))
    errors = sorted(
        _schema_validator(_OUTPUT_SCHEMA).iter_errors(materialized),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        path = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise PilotSelectionError(f"pilot selection report failed schema at {path}: {errors[0].message}")
    if materialized["score_policy"] != _SCORE_POLICY:
        raise PilotSelectionError("pilot selection report changed the frozen score policy")
    if materialized["score_policy_sha256"] != _SCORE_POLICY_SHA256:
        raise PilotSelectionError("pilot selection score-policy hash is inconsistent")
    fairness = materialized["fairness"]
    if fairness["shared_training_binding_sha256"] != canonical_json_sha256(fairness["shared_training_binding"]):
        raise PilotSelectionError("shared training binding hash is inconsistent")
    if fairness["shared_inference_binding_sha256"] != canonical_json_sha256(fairness["shared_inference_binding"]):
        raise PilotSelectionError("shared inference binding hash is inconsistent")
    candidates = materialized["candidates"]
    input_candidates = {row["candidate_id"]: row for row in materialized["inputs"]["candidates"]}
    outcomes = {row["candidate_id"]: row for row in fairness["candidate_outcomes"]}
    candidate_ids = [row["candidate_id"] for row in candidates]
    if (
        len(set(candidate_ids)) != 2
        or set(input_candidates) != set(candidate_ids)
        or set(outcomes) != set(candidate_ids)
    ):
        raise PilotSelectionError("pilot selection candidate identity sets disagree")
    minimum_loss = min(float(row["eval_loss"]) for row in candidates)
    for row in candidates:
        candidate_id = row["candidate_id"]
        expected_failed = sorted(gate_id for gate_id, passed in row["hard_gates"].items() if passed is not True)
        expected_hard_pass = not expected_failed
        if (
            row["failed_hard_gates"] != expected_failed
            or row["hard_gates_passed"] is not expected_hard_pass
            or row["release_eligible"] is not expected_hard_pass
        ):
            raise PilotSelectionError(f"{candidate_id} hard-gate summary is inconsistent")
        for component_name in ("safety_content", "task", "structure"):
            component = row["components"][component_name]
            expected_component = sum(component["values"].values()) / len(component["values"])
            if float(component["score"]) != expected_component:
                raise PilotSelectionError(f"{candidate_id} {component_name} score is inconsistent")
        expected_normalized_loss = minimum_loss / float(row["eval_loss"])
        if float(row["normalized_eval_loss"]) != expected_normalized_loss:
            raise PilotSelectionError(f"{candidate_id} normalized eval loss is inconsistent")
        expected_composite = (
            _WEIGHTS["safety_content"] * float(row["components"]["safety_content"]["score"])
            + _WEIGHTS["task"] * float(row["components"]["task"]["score"])
            + _WEIGHTS["structure"] * float(row["components"]["structure"]["score"])
            + _WEIGHTS["normalized_eval_loss"] * expected_normalized_loss
        )
        if float(row["composite_score"]) != expected_composite:
            raise PilotSelectionError(f"{candidate_id} composite score is inconsistent")
        outcome = outcomes[candidate_id]
        input_candidate = input_candidates[candidate_id]
        if (
            float(outcome["learning_rate"]) != float(row["learning_rate"])
            or float(outcome["best_eval_loss"]) != float(row["eval_loss"])
            or outcome["early_stopping_triggered"]
            is not (outcome["actual_global_step"] < outcome["configured_max_steps"])
            or _checkpoint_step(outcome["best_checkpoint"], f"{candidate_id} outcome best checkpoint")
            != outcome["best_checkpoint_step"]
            or outcome["model_tree_sha256"] != input_candidate["model_tree"]["tree_sha256"]
        ):
            raise PilotSelectionError(f"{candidate_id} training outcome binding is inconsistent")
        model_path = input_candidate["model_tree"]["path"]
        if not isinstance(
            model_path,
            str,
        ) or not _is_platform_neutral_absolute_path(model_path):
            raise PilotSelectionError(f"{candidate_id} model-tree path must be absolute")
    ranked = sorted(candidates, key=_sort_key)
    if [row["candidate_id"] for row in candidates] != [row["candidate_id"] for row in ranked]:
        raise PilotSelectionError("pilot selection candidates are not in frozen rank order")
    if [row["rank"] for row in candidates] != [1, 2]:
        raise PilotSelectionError("pilot selection ranks must be exactly 1 and 2")
    any_safe = any(row["hard_gates_passed"] for row in candidates)
    selected_parent = materialized["release_winner"] if any_safe else materialized["remediation_parent"]
    if selected_parent is not None:
        selected_id = selected_parent["candidate_id"]
        selected_input = input_candidates[selected_id]
        if (
            selected_parent["learning_rate"]
            != next(row["learning_rate"] for row in candidates if row["candidate_id"] == selected_id)
            or selected_parent["training_report_sha256"] != selected_input["training_report_sha256"]
            or selected_parent["model_tree"] != selected_input["model_tree"]
        ):
            raise PilotSelectionError("selected parent input binding is inconsistent")
    if any_safe:
        if (
            materialized["status"] != "release_winner_selected"
            or materialized["release_winner"] is None
            or materialized["release_winner"]["candidate_id"] != ranked[0]["candidate_id"]
            or materialized["remediation_parent"] is not None
            or materialized["release_safety_assertion"] is not True
        ):
            raise PilotSelectionError("safe pilot selection decision is inconsistent")
    elif (
        materialized["status"] != "no_release_winner"
        or materialized["release_winner"] is not None
        or materialized["release_safety_assertion"] is not False
    ):
        raise PilotSelectionError("hard-failed pilot selection implies a release winner")
    remediation = materialized["remediation_parent"]
    evidence = materialized["remediation_evidence"]
    if any_safe and (
        evidence["status"] != "not_applicable_safe_winner" or evidence["external_failure_signature_sha256"] is not None
    ):
        raise PilotSelectionError("safe winner has inconsistent remediation evidence")
    if not any_safe and evidence["status"] == "shared_external_failure_proven" and remediation is None:
        raise PilotSelectionError("proven remediation evidence has no remediation parent")
    if evidence["status"] != "shared_external_failure_proven" and remediation is not None:
        raise PilotSelectionError("unproven remediation evidence implies a parent")
    if remediation is not None and (
        any_safe
        or evidence["status"] != "shared_external_failure_proven"
        or remediation["candidate_id"] != ranked[0]["candidate_id"]
        or remediation["release_eligible"] is not False
        or remediation["scope"] != "remediation_only_not_release"
        or remediation["external_failure_signature_sha256"] != evidence["external_failure_signature_sha256"]
    ):
        raise PilotSelectionError("remediation parent is not safely justified")
    return materialized


def select_pilot_learning_rate(
    candidates: Sequence[PilotCandidateInput],
    *,
    reference_manifest_path: str | Path,
    evaluation_config_path: str | Path,
) -> dict[str, Any]:
    """Select a release winner, or at most a non-release remediation parent."""

    if len(candidates) != 2:
        raise PilotSelectionError("pilot selection requires exactly two candidates")
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if (
        any(_CANDIDATE_ID.fullmatch(candidate_id) is None for candidate_id in candidate_ids)
        or len(set(candidate_ids)) != 2
    ):
        raise PilotSelectionError("pilot candidate IDs must be unique stable identifiers")
    reference_loaded, reference_manifest = _load_json(reference_manifest_path, "combined reference manifest")
    reference = _reference_manifest_binding(reference_loaded, reference_manifest)
    config_loaded, evaluation_config = _load_yaml(evaluation_config_path, "evaluation config")
    config_binding, expected_hard_gates = _evaluation_config_binding(config_loaded, evaluation_config)

    loaded_rows: list[dict[str, Any]] = []
    fairness_bindings: list[dict[str, Any]] = []
    inference_bindings: list[dict[str, Any]] = []
    input_bindings: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any] | None] = []
    learning_rates: list[float] = []
    outcomes: list[dict[str, Any]] = []
    for candidate in candidates:
        if (candidate.protected_span_diagnostic is None) != (candidate.diagnostic_evaluation_report is None):
            raise PilotSelectionError(
                f"{candidate.candidate_id} diagnostic and its evaluation report must be supplied together"
            )
        training_loaded, training_report = _load_json(
            candidate.training_report,
            f"{candidate.candidate_id} training report",
        )
        fairness, outcome, model_tree, learning_rate = _validate_training_report(candidate, training_report)
        prediction_loaded, prediction_manifest = _load_json(
            candidate.prediction_manifest,
            f"{candidate.candidate_id} combined prediction manifest",
        )
        prediction, inference, part_manifest_hashes = _validate_prediction_manifest(
            candidate,
            prediction_loaded,
            prediction_manifest,
            reference,
        )
        evaluation_loaded, evaluation_report = _load_json(
            candidate.evaluation_report,
            f"{candidate.candidate_id} combined evaluation report",
        )
        evaluation, hard_results, failed_hard = _validate_evaluation(
            candidate,
            evaluation_loaded,
            evaluation_report,
            evaluation_config_sha256=config_binding["config_sha256"],
            reference=reference,
            prediction=prediction,
            hard_gates=expected_hard_gates,
        )
        part_record_hashes = {item["prediction_sha256"] for item in prediction["part_manifests"]}
        diagnostic_binding: dict[str, Any] | None = None
        if candidate.protected_span_diagnostic is not None:
            diagnostic_loaded, diagnostic = _load_json(
                candidate.protected_span_diagnostic,
                f"{candidate.candidate_id} protected-span diagnostic",
            )
            diagnostic_eval_loaded, diagnostic_eval = _load_json(
                candidate.diagnostic_evaluation_report,
                f"{candidate.candidate_id} diagnostic evaluation report",
            )
            diagnostic_binding = _validate_protected_diagnostic(
                candidate,
                diagnostic_loaded,
                diagnostic,
                diagnostic_eval_loaded,
                diagnostic_eval,
                training_report=training_report,
                config_sha256=config_binding["config_sha256"],
                reference=reference,
                prediction_part_manifest_hashes=part_manifest_hashes,
                prediction_part_record_hashes=part_record_hashes,
            )
        input_binding = {
            "candidate_id": candidate.candidate_id,
            "training_report_sha256": training_loaded.sha256,
            "model_tree": model_tree,
            "prediction_manifest_sha256": prediction_loaded.sha256,
            "prediction_part_manifest_sha256": sorted(part_manifest_hashes),
            "evaluation_report_sha256": evaluation_loaded.sha256,
            "protected_span_diagnostic_sha256": (
                diagnostic_binding["diagnostic_sha256"] if diagnostic_binding is not None else None
            ),
            "diagnostic_evaluation_report_sha256": (
                diagnostic_binding["diagnostic_evaluation_sha256"] if diagnostic_binding is not None else None
            ),
        }
        row = {
            "candidate_id": candidate.candidate_id,
            "learning_rate": learning_rate,
            "hard_gates": hard_results,
            "hard_gates_passed": not failed_hard,
            "failed_hard_gates": failed_hard,
            "components": evaluation["components"],
            "eval_loss": outcome["best_eval_loss"],
            "normalized_eval_loss": 0.0,
            "composite_score": 0.0,
            "release_eligible": not failed_hard,
            "rank": 0,
        }
        loaded_rows.append(row)
        fairness_bindings.append(fairness)
        inference_bindings.append(inference)
        input_bindings.append(input_binding)
        diagnostics.append(diagnostic_binding)
        learning_rates.append(learning_rate)
        outcomes.append(outcome)
    if learning_rates[0] == learning_rates[1]:
        raise PilotSelectionError("pilot candidates must use distinct learning rates")
    if fairness_bindings[0] != fairness_bindings[1]:
        raise PilotSelectionError("pilot runs are not fair: a training binding other than learning rate differs")
    if inference_bindings[0] != inference_bindings[1]:
        raise PilotSelectionError("pilot runs are not fair: inference bindings differ")
    best_loss = min(float(row["eval_loss"]) for row in loaded_rows)
    for row in loaded_rows:
        normalized_loss = best_loss / float(row["eval_loss"])
        row["normalized_eval_loss"] = normalized_loss
        row["composite_score"] = (
            _WEIGHTS["safety_content"] * float(row["components"]["safety_content"]["score"])
            + _WEIGHTS["task"] * float(row["components"]["task"]["score"])
            + _WEIGHTS["structure"] * float(row["components"]["structure"]["score"])
            + _WEIGHTS["normalized_eval_loss"] * normalized_loss
        )
    ranked = sorted(loaded_rows, key=_sort_key)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
    input_by_id = {item["candidate_id"]: item for item in input_bindings}
    any_safe = any(bool(row["hard_gates_passed"]) for row in ranked)
    release_winner = (
        _parent_binding(
            ranked[0],
            input_by_id[ranked[0]["candidate_id"]],
            release_eligible=True,
        )
        if any_safe
        else None
    )
    remediation_status = "not_applicable_safe_winner" if any_safe else "not_provided"
    shared_signature: str | None = None
    remediation_parent: dict[str, Any] | None = None
    if not any_safe and all(diagnostic is not None for diagnostic in diagnostics):
        first = _mapping(diagnostics[0], "first diagnostic")
        second = _mapping(diagnostics[1], "second diagnostic")
        if (
            first["proves_external_failure"] is True
            and second["proves_external_failure"] is True
            and first["external_evidence"] == second["external_evidence"]
            and first["external_failure_signature_sha256"] == second["external_failure_signature_sha256"]
        ):
            remediation_status = "shared_external_failure_proven"
            shared_signature = str(first["external_failure_signature_sha256"])
            remediation_parent = _parent_binding(
                ranked[0],
                input_by_id[ranked[0]["candidate_id"]],
                release_eligible=False,
                remediation=True,
                external_failure_signature_sha256=shared_signature,
            )
        else:
            remediation_status = "shared_external_failure_not_proven"
    report = {
        "schema_version": 1,
        "kind": "scriber_pilot_learning_rate_selection",
        "status": ("release_winner_selected" if any_safe else "no_release_winner"),
        "score_policy_sha256": _SCORE_POLICY_SHA256,
        "score_policy": _SCORE_POLICY,
        "inputs": {
            "reference_manifest_sha256": reference_loaded.sha256,
            "reference_records_sha256": reference["records_sha256"],
            "evaluation_config_sha256": config_loaded.sha256,
            "candidates": sorted(input_bindings, key=lambda item: item["candidate_id"]),
        },
        "fairness": {
            "passed": True,
            "only_allowed_difference": "learning_rate",
            "shared_training_binding_sha256": canonical_json_sha256(fairness_bindings[0]),
            "shared_training_binding": fairness_bindings[0],
            "shared_inference_binding_sha256": canonical_json_sha256(inference_bindings[0]),
            "shared_inference_binding": inference_bindings[0],
            "candidate_outcomes": sorted(outcomes, key=lambda item: item["candidate_id"]),
        },
        "candidates": ranked,
        "release_winner": release_winner,
        "release_safety_assertion": any_safe,
        "remediation_evidence": {
            "status": remediation_status,
            "external_failure_signature_sha256": shared_signature,
            "diagnostic_sha256": sorted(
                [str(diagnostic["diagnostic_sha256"]) for diagnostic in diagnostics if diagnostic is not None]
            ),
            "assertion": (
                "This evidence may justify only a remediation parent; it never "
                "changes failed hard gates or release eligibility."
            ),
        },
        "remediation_parent": remediation_parent,
    }
    return validate_pilot_selection_report(report)


def write_pilot_selection_report(
    report: Mapping[str, Any],
    destination: str | Path,
) -> Path:
    """Write a validated report exactly once; existing evidence is immutable."""

    validated = validate_pilot_selection_report(report)
    path = Path(destination)
    if path.exists():
        raise PilotSelectionError(f"refusing to overwrite pilot selection report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists():
        raise PilotSelectionError(f"pilot selection temporary path already exists: {temporary}")
    payload = canonical_json_bytes(validated)
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
        if path.exists():
            raise PilotSelectionError(f"pilot selection destination appeared during write: {path}")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


def score_policy() -> dict[str, Any]:
    """Return a defensive copy of the frozen public selection policy."""

    return json.loads(json.dumps(_SCORE_POLICY))
