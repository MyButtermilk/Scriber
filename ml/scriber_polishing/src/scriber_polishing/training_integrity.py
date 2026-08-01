"""Fail-closed identity, resume, and final-artifact contracts for SFT runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .curriculum import (
    canonical_json_bytes,
    hash_identifier_sequence,
    validate_curriculum_plan,
)
from .model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from .protected_spans import (
    GEMMA_UNUSED_POLICY_ID,
    LEXICAL_KEEP_POLICY_ID,
    PROTECTION_POLICY_FILENAME,
    ProtectionPolicyError,
    validate_protection_policy_evidence,
)
from .quantization import QuantizationError, inventory_bf16_checkpoint

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKPOINT_RE = re.compile(r"^checkpoint-([1-9][0-9]*)$")
_RUN_IDENTITY_FILENAME = "run-identity.json"
_CHECKPOINT_IDENTITY_FILENAME = "scriber-run-identity.json"
_INVENTORY_FILENAME = "checkpoint-inventory.json"
_RELOAD_FILENAME = "reload-verification.json"

_REQUIRED_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "run_kind",
        "base_model",
        "base_revision",
        "method",
        "seed",
        "max_sequence_length",
        "micro_batch_size",
        "gradient_accumulation_steps",
        "learning_rate",
        "bf16",
        "gradient_checkpointing",
        "optimizer",
        "eval_steps",
        "save_steps",
        "warmup_ratio",
        "weight_decay",
        "max_grad_norm",
        "assistant_output_loss_only",
    }
)
_OPTIONAL_CONFIG_KEYS = frozenset(
    {
        "train_examples",
        "validation_examples",
        "minimum_train_examples",
        "minimum_validation_examples",
        "epochs",
        "max_steps",
        "max_sequence_length_candidates",
        "learning_rate_candidates",
        "early_stopping",
        "early_stopping_patience",
        "save_total_limit",
        "training_order_mode",
        "training_order_plan_path",
        "training_order_plan_sha256",
        "protection_policy_path",
        "protection_policy_sha256",
        "parent_checkpoint_path",
        "parent_checkpoint_tree_sha256",
    }
)
_CONFIG_KEYS = _REQUIRED_CONFIG_KEYS | _OPTIONAL_CONFIG_KEYS


class TrainingIntegrityError(RuntimeError):
    """A training input or artifact cannot be bound without ambiguity."""


TRAINING_CODE_IDENTITY_PROJECT_RELATIVE_PATHS = tuple(
    Path(value)
    for value in (
        "contracts/curriculum_plan_schema.json",
        "contracts/protection_policy_evidence_schema.json",
        "contracts/remediation_experiment_config_schema.json",
        "contracts/remediation_order_plan_schema.json",
        "scripts/build_remediation_experiment.py",
        "src/scriber_polishing/curriculum.py",
        "src/scriber_polishing/model_factory.py",
        "src/scriber_polishing/protected_spans.py",
        "src/scriber_polishing/quantization.py",
        "src/scriber_polishing/remediation_experiment.py",
        "src/scriber_polishing/tokenize.py",
        "src/scriber_polishing/train.py",
        "src/scriber_polishing/training_integrity.py",
        "src/scriber_polishing/training_runtime.py",
    )
)


def training_code_identity_paths(
    project_root: str | Path,
    entrypoint: str | Path,
) -> tuple[Path, ...]:
    """Return the single shared, project-relative code identity dependency set."""

    root = Path(project_root).expanduser().resolve()
    entry = Path(entrypoint).expanduser().resolve()
    try:
        entry.relative_to(root)
    except ValueError as error:
        raise TrainingIntegrityError(f"training entrypoint must stay below the project root: {entry}") from error
    for relative in TRAINING_CODE_IDENTITY_PROJECT_RELATIVE_PATHS:
        if relative.is_absolute() or ".." in relative.parts:
            raise TrainingIntegrityError(
                f"training code identity path must be a safe project-relative path: {relative}"
            )
    return (
        entry,
        *(root / relative for relative in TRAINING_CODE_IDENTITY_PROJECT_RELATIVE_PATHS),
    )


@dataclass(frozen=True, slots=True)
class TrainingConfigSnapshot:
    """One exact config byte snapshot and its closed parsed value."""

    path: Path
    byte_count: int
    sha256: str
    values: Mapping[str, Any]

    def binding(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "bytes": self.byte_count,
            "sha256": self.sha256,
            "values": _json_copy(self.values),
        }


@dataclass(frozen=True, slots=True)
class BoundPairSplit:
    """Records selected from one fully hashed and validated JSONL split."""

    path: Path
    byte_count: int
    sha256: str
    total_records: int
    selected_records: int
    records: tuple[dict[str, str], ...]

    def binding(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "bytes": self.byte_count,
            "sha256": self.sha256,
            "total_records": self.total_records,
            "selected_records": self.selected_records,
        }


@dataclass(frozen=True, slots=True)
class BoundTrainingDataset:
    """Exact manifest plus exact train/validation bytes consumed by a run."""

    manifest: Mapping[str, object]
    train: BoundPairSplit
    validation: BoundPairSplit

    def binding(self) -> dict[str, object]:
        return {
            "manifest": _json_copy(self.manifest),
            "train": self.train.binding(),
            "validation": self.validation.binding(),
        }


@dataclass(frozen=True, slots=True)
class BoundTrainingOrderPlan:
    """Exact validated curriculum plan bytes bound to one pilot dataset/config."""

    path: Path
    byte_count: int
    sha256: str
    values: Mapping[str, Any]

    def binding(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "bytes": self.byte_count,
            "sha256": self.sha256,
            "mode": self.values["mode"],
            "algorithm": self.values["algorithm"],
            "phase_rules_version": self.values["phase_rules_version"],
            "seed": self.values["seed"],
            "epochs": self.values["epochs"],
            "record_count": self.values["source"]["record_count"],
            "source_sha256": self.values["source"]["sha256"],
            "ai_gold_train_sha256": self.values["ai_gold_train"]["sha256"],
            "phase_counts": _json_copy(self.values["phase_counts"]),
        }


@dataclass(frozen=True, slots=True)
class BoundProtectionPolicy:
    """Exact policy bytes verified against the runtime tokenizer."""

    path: Path
    byte_count: int
    sha256: str
    values: Mapping[str, Any]

    def binding(self) -> dict[str, object]:
        binding: dict[str, object] = {
            "path": str(self.path),
            "bytes": self.byte_count,
            "sha256": self.sha256,
            "policy_id": self.values["policy_id"],
            "policy_sha256": self.values["policy_sha256"],
            "tokenizer": _json_copy(self.values["tokenizer"]),
            "marker_start": self.values["marker_start"],
            "marker_end": self.values["marker_end"],
            "max_spans": self.values["max_spans"],
        }
        if self.values["policy_id"] == GEMMA_UNUSED_POLICY_ID:
            binding["marker_token_ids_sha256"] = self.values["marker_token_ids_sha256"]
        else:
            binding["marker_token_sequences_sha256"] = self.values["marker_token_sequences_sha256"]
            binding["pair_mapping"] = self.values["pair_mapping"]
        return binding


@dataclass(frozen=True, slots=True)
class BoundParentCheckpoint:
    """A warm-start checkpoint bound as immutable input, never as run resume."""

    path: Path
    tree_sha256: str
    file_count: int
    total_bytes: int
    files: tuple[Mapping[str, object], ...]

    def binding(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "tree_sha256": self.tree_sha256,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "files": _json_copy(self.files),
            "relationship": "warm_start_parent_not_resume",
        }


@dataclass(frozen=True, slots=True)
class BoundImprovementOrderPlan:
    """Exact remediation A/B order plan bound to runtime safety inputs."""

    path: Path
    byte_count: int
    sha256: str
    values: Mapping[str, Any]

    def binding(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "bytes": self.byte_count,
            "sha256": self.sha256,
            "kind": self.values["kind"],
            "mode": self.values["mode"],
            "algorithm": self.values["algorithm"],
            "phase_rules_version": self.values["phase_rules_version"],
            "seed": self.values["seed"],
            "epochs": self.values["epochs"],
            "comparison_pair_sha256": self.values["comparison_pair_sha256"],
            "source": _json_copy(self.values["source"]),
            "safety_source": _json_copy(self.values["safety_source"]),
            "protection_policy": _json_copy(self.values["protection_policy"]),
            "parent_checkpoint": _json_copy(self.values["parent_checkpoint"]),
            "safety_filter": _json_copy(self.values["safety_filter"]),
            "training_contract": _json_copy(self.values["training_contract"]),
            "state_initialization": _json_copy(self.values["state_initialization"]),
            "phase_counts": _json_copy(self.values["phase_counts"]),
            "holdout_inputs": self.values["holdout_inputs"],
        }


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise TrainingIntegrityError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _json_copy(value: object) -> Any:
    if isinstance(value, Mapping):
        value = {str(key): _json_copy(item) for key, item in value.items()}
    elif isinstance(value, tuple | list):
        value = [_json_copy(item) for item in value]
    return json.loads(json.dumps(value, ensure_ascii=False))


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _regular_file(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink():
        raise TrainingIntegrityError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise TrainingIntegrityError(f"{label} must be a regular file: {resolved}")
    return resolved


def _read_regular_bytes(path: str | Path, label: str) -> tuple[Path, bytes]:
    resolved = _regular_file(path, label)
    before = resolved.stat()
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise TrainingIntegrityError(f"cannot read {label}: {error}") from error
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
        raise TrainingIntegrityError(f"{label} changed while it was being read")
    return resolved, payload


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TrainingIntegrityError(f"{field} must be a positive integer, not a placeholder")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TrainingIntegrityError(f"{field} must be a non-negative integer")
    return value


def _finite_number(
    value: object,
    field: str,
    *,
    minimum: float,
    maximum: float | None = None,
    inclusive_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TrainingIntegrityError(f"{field} must be a numeric value, not a placeholder")
    numeric = float(value)
    minimum_failed = numeric < minimum if inclusive_minimum else numeric <= minimum
    if not math.isfinite(numeric) or minimum_failed or (maximum is not None and numeric > maximum):
        relation = ">=" if inclusive_minimum else ">"
        upper = f" and <= {maximum}" if maximum is not None else ""
        raise TrainingIntegrityError(f"{field} must be finite and {relation} {minimum}{upper}")
    return numeric


def _numeric_candidates(
    value: object,
    field: str,
    *,
    integer: bool,
) -> list[int | float]:
    if not isinstance(value, list) or not value:
        raise TrainingIntegrityError(f"{field} must be a non-empty list")
    result: list[int | float] = []
    for index, item in enumerate(value):
        if integer:
            result.append(_positive_int(item, f"{field}[{index}]"))
        else:
            result.append(
                _finite_number(
                    item,
                    f"{field}[{index}]",
                    minimum=0.0,
                    maximum=0.01,
                )
            )
    if len(set(result)) != len(result):
        raise TrainingIntegrityError(f"{field} must not contain duplicates")
    return result


def _validate_training_config(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TrainingIntegrityError("training config must be an object with string keys")
    unknown = sorted(set(value) - _CONFIG_KEYS)
    if unknown:
        raise TrainingIntegrityError("training config contains unknown or unused declarations: " + ", ".join(unknown))
    missing = sorted(_REQUIRED_CONFIG_KEYS - set(value))
    if missing:
        raise TrainingIntegrityError("training config is missing required fields: " + ", ".join(missing))
    if value["schema_version"] != 1:
        raise TrainingIntegrityError("schema_version must equal 1")
    if value["run_kind"] not in {"smoke", "pilot", "improvement", "final"}:
        raise TrainingIntegrityError("run_kind must be smoke, pilot, improvement, or final")
    if value["base_model"] != BASE_MODEL_ID:
        raise TrainingIntegrityError(f"base_model must equal {BASE_MODEL_ID}")
    if value["base_revision"] != BASE_MODEL_REVISION:
        raise TrainingIntegrityError(f"base_revision must equal {BASE_MODEL_REVISION}")
    if value["method"] != "full_sft":
        raise TrainingIntegrityError("method must be full_sft")
    _nonnegative_int(value["seed"], "seed")

    schedule_keys = {key for key in ("epochs", "max_steps") if key in value}
    if len(schedule_keys) != 1:
        raise TrainingIntegrityError("training config must declare exactly one of epochs or max_steps")
    if "epochs" in value:
        _finite_number(value["epochs"], "epochs", minimum=0.0, maximum=10.0)
    if "max_steps" in value:
        _positive_int(value["max_steps"], "max_steps")

    exact_and_minimum = (
        ("train_examples", "minimum_train_examples"),
        ("validation_examples", "minimum_validation_examples"),
    )
    for exact, minimum in exact_and_minimum:
        if exact in value and minimum in value:
            raise TrainingIntegrityError(f"{exact} and {minimum} are mutually exclusive")
        if exact in value:
            _positive_int(value[exact], exact)
        if minimum in value:
            _positive_int(value[minimum], minimum)

    max_sequence_length = _positive_int(
        value["max_sequence_length"],
        "max_sequence_length",
    )
    if max_sequence_length < 128 or max_sequence_length > 4096:
        raise TrainingIntegrityError("max_sequence_length must be between 128 and 4096")
    if "max_sequence_length_candidates" in value:
        candidates = _numeric_candidates(
            value["max_sequence_length_candidates"],
            "max_sequence_length_candidates",
            integer=True,
        )
        if max_sequence_length not in candidates:
            raise TrainingIntegrityError("max_sequence_length must be one of max_sequence_length_candidates")
        if value["run_kind"] != "pilot":
            raise TrainingIntegrityError("max_sequence_length_candidates is a pilot-only declaration")

    micro_batch_size = _positive_int(value["micro_batch_size"], "micro_batch_size")
    if micro_batch_size not in {1, 2, 4}:
        raise TrainingIntegrityError("micro_batch_size must be one of 1, 2, or 4")
    gradient_accumulation_steps = _positive_int(
        value["gradient_accumulation_steps"],
        "gradient_accumulation_steps",
    )
    if value["run_kind"] == "improvement" and micro_batch_size * gradient_accumulation_steps != 16:
        raise TrainingIntegrityError("improvement training requires effective batch size 16")
    learning_rate = _finite_number(
        value["learning_rate"],
        "learning_rate",
        minimum=0.0,
        maximum=0.01,
    )
    if "learning_rate_candidates" in value:
        candidates = _numeric_candidates(
            value["learning_rate_candidates"],
            "learning_rate_candidates",
            integer=False,
        )
        if learning_rate not in candidates:
            raise TrainingIntegrityError("learning_rate must be one of learning_rate_candidates")
        if value["run_kind"] != "pilot":
            raise TrainingIntegrityError("learning_rate_candidates is a pilot-only declaration")

    if value["bf16"] is not True:
        raise TrainingIntegrityError("BF16 full-SFT requires bf16: true")
    if not isinstance(value["gradient_checkpointing"], bool):
        raise TrainingIntegrityError("gradient_checkpointing must be boolean")
    if value["optimizer"] != "adamw_torch_fused":
        raise TrainingIntegrityError("optimizer must equal adamw_torch_fused")
    _positive_int(value["eval_steps"], "eval_steps")
    _positive_int(value["save_steps"], "save_steps")
    if value["save_steps"] % value["eval_steps"] != 0:
        raise TrainingIntegrityError("save_steps must be an integer multiple of eval_steps")
    _finite_number(
        value["warmup_ratio"],
        "warmup_ratio",
        minimum=0.0,
        maximum=1.0,
        inclusive_minimum=True,
    )
    _finite_number(
        value["weight_decay"],
        "weight_decay",
        minimum=0.0,
        maximum=1.0,
        inclusive_minimum=True,
    )
    _finite_number(value["max_grad_norm"], "max_grad_norm", minimum=0.0)
    if value["assistant_output_loss_only"] is not True:
        raise TrainingIntegrityError("assistant_output_loss_only must be true for completion-only SFT")

    early_stopping = value.get("early_stopping", False)
    if not isinstance(early_stopping, bool):
        raise TrainingIntegrityError("early_stopping must be boolean")
    if "early_stopping_patience" in value:
        _positive_int(value["early_stopping_patience"], "early_stopping_patience")
        if not early_stopping:
            raise TrainingIntegrityError("early_stopping_patience is unused unless early_stopping is true")
    elif early_stopping:
        raise TrainingIntegrityError("early_stopping_patience is required when early_stopping is true")

    if "save_total_limit" in value:
        _positive_int(value["save_total_limit"], "save_total_limit")

    training_order_fields = {
        "training_order_mode",
        "training_order_plan_path",
        "training_order_plan_sha256",
    }
    declared_training_order = training_order_fields & set(value)
    if declared_training_order and declared_training_order != training_order_fields:
        missing_order_fields = sorted(training_order_fields - declared_training_order)
        raise TrainingIntegrityError(
            "explicit training order requires all mode/path/SHA fields; missing " + ", ".join(missing_order_fields)
        )
    if declared_training_order:
        if value["run_kind"] not in {"pilot", "improvement"}:
            raise TrainingIntegrityError("explicit training order is pilot/improvement-only")
        if "max_steps" in value:
            raise TrainingIntegrityError(
                "explicit training order requires an epoch-derived schedule; max_steps must be absent"
            )
        if early_stopping:
            raise TrainingIntegrityError("explicit training order requires early_stopping: false")
        if value["training_order_mode"] not in {"shuffled", "staged"}:
            raise TrainingIntegrityError("training_order_mode must be shuffled or staged")
        plan_path = value["training_order_plan_path"]
        if (
            not isinstance(plan_path, str)
            or not plan_path.strip()
            or plan_path != plan_path.strip()
            or "\0" in plan_path
        ):
            raise TrainingIntegrityError("training_order_plan_path must be non-empty path text")
        plan_sha256 = value["training_order_plan_sha256"]
        if not isinstance(plan_sha256, str) or not re.fullmatch(
            r"(?:sha256:)?[0-9a-f]{64}",
            plan_sha256,
        ):
            raise TrainingIntegrityError("training_order_plan_sha256 must be a lowercase SHA-256")
        declared_epochs = value.get("epochs")
        if isinstance(declared_epochs, bool) or not isinstance(declared_epochs, int) or not 1 <= declared_epochs <= 10:
            raise TrainingIntegrityError("explicit training order requires integer epochs from 1 to 10")
        if value["run_kind"] == "improvement" and declared_epochs != 1:
            raise TrainingIntegrityError("improvement order plans require exactly one full epoch")
    elif value["run_kind"] == "improvement":
        raise TrainingIntegrityError("improvement training requires an explicit remediation order plan")

    policy_fields = {
        "protection_policy_path",
        "protection_policy_sha256",
    }
    declared_policy = policy_fields & set(value)
    if declared_policy and declared_policy != policy_fields:
        missing_policy_fields = sorted(policy_fields - declared_policy)
        raise TrainingIntegrityError(
            "protection policy requires path and SHA fields; missing " + ", ".join(missing_policy_fields)
        )
    if value["run_kind"] in {"improvement", "final"} and not declared_policy:
        raise TrainingIntegrityError(f"{value['run_kind']} training requires gemma_unused_v1 protection policy")
    if declared_policy:
        if value["run_kind"] not in {"improvement", "final"}:
            raise TrainingIntegrityError("explicit protection policy is improvement/final-only")
        policy_path = value["protection_policy_path"]
        if (
            not isinstance(policy_path, str)
            or not policy_path.strip()
            or policy_path != policy_path.strip()
            or "\0" in policy_path
        ):
            raise TrainingIntegrityError("protection_policy_path must be non-empty path text")
        policy_sha256 = value["protection_policy_sha256"]
        if not isinstance(policy_sha256, str) or not re.fullmatch(
            r"(?:sha256:)?[0-9a-f]{64}",
            policy_sha256,
        ):
            raise TrainingIntegrityError("protection_policy_sha256 must be a lowercase SHA-256")

    parent_fields = {
        "parent_checkpoint_path",
        "parent_checkpoint_tree_sha256",
    }
    declared_parent = parent_fields & set(value)
    if declared_parent and declared_parent != parent_fields:
        missing_parent_fields = sorted(parent_fields - declared_parent)
        raise TrainingIntegrityError(
            "warm-start parent requires path and tree SHA fields; missing " + ", ".join(missing_parent_fields)
        )
    if value["run_kind"] == "improvement" and not declared_parent:
        raise TrainingIntegrityError("improvement training requires a warm-start parent checkpoint")
    if declared_parent:
        if value["run_kind"] not in {"improvement", "final"}:
            raise TrainingIntegrityError(
                "warm-start parent checkpoint is improvement/final-only"
            )
        parent_path = value["parent_checkpoint_path"]
        if (
            not isinstance(parent_path, str)
            or not parent_path.strip()
            or parent_path != parent_path.strip()
            or "\0" in parent_path
        ):
            raise TrainingIntegrityError("parent_checkpoint_path must be non-empty path text")
        parent_sha256 = value["parent_checkpoint_tree_sha256"]
        if not isinstance(parent_sha256, str) or not re.fullmatch(
            r"(?:sha256:)?[0-9a-f]{64}",
            parent_sha256,
        ):
            raise TrainingIntegrityError("parent_checkpoint_tree_sha256 must be a lowercase SHA-256")
    return value


def load_training_config(path: str | Path) -> TrainingConfigSnapshot:
    """Load one exact YAML byte snapshot under the closed SFT schema."""

    resolved, payload = _read_regular_bytes(path, "training config")
    try:
        value = yaml.load(payload.decode("utf-8"), Loader=_UniqueKeyLoader)
    except TrainingIntegrityError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise TrainingIntegrityError(f"training config is invalid YAML: {error}") from error
    validated = _validate_training_config(value)
    return TrainingConfigSnapshot(
        path=resolved,
        byte_count=len(payload),
        sha256=_sha256(payload),
        values=MappingProxyType(_json_copy(validated)),
    )


def _load_bound_pair_split(
    path: str | Path,
    *,
    label: str,
    limit: int | None,
) -> BoundPairSplit:
    if limit is not None:
        _positive_int(limit, f"{label}_limit")
    resolved = _regular_file(path, f"{label} split")
    digest = hashlib.sha256()
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    total_records = 0
    byte_count = 0
    try:
        with resolved.open("rb") as stream:
            before = os.fstat(stream.fileno())
            for line_number, raw_line in enumerate(stream, start=1):
                digest.update(raw_line)
                byte_count += len(raw_line)
                try:
                    line = raw_line.decode("utf-8").rstrip("\r\n")
                except UnicodeDecodeError as error:
                    raise TrainingIntegrityError(f"{label} JSONL is not UTF-8 at line {line_number}") from error
                if not line:
                    raise TrainingIntegrityError(f"blank {label} JSONL line at {line_number}")
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise TrainingIntegrityError(f"invalid {label} JSON at line {line_number}: {error}") from error
                if not isinstance(value, dict):
                    raise TrainingIntegrityError(f"{label} record {line_number} must be an object")
                row: dict[str, Any] = {}
                for field in ("example_id", "source_text", "target_sst"):
                    field_value = value.get(field)
                    if not isinstance(field_value, str) or not field_value:
                        raise TrainingIntegrityError(f"{label} record {line_number} requires non-empty {field}")
                    row[field] = field_value
                if "protected_spans" in value:
                    protected_values = value["protected_spans"]
                    if (
                        not isinstance(protected_values, list)
                        or any(
                            not isinstance(item, str) or not item
                            for item in protected_values
                        )
                    ):
                        raise TrainingIntegrityError(
                            f"{label} record {line_number} has invalid protected_spans"
                        )
                    row["protected_spans"] = protected_values
                if "value_mappings" in value:
                    mappings = value["value_mappings"]
                    if not isinstance(mappings, list) or any(
                        not isinstance(item, dict)
                        or any(
                            not isinstance(item.get(field), str)
                            or not item[field]
                            for field in ("kind", "source", "target")
                        )
                        for item in mappings
                    ):
                        raise TrainingIntegrityError(
                            f"{label} record {line_number} has invalid value_mappings"
                        )
                    row["value_mappings"] = mappings
                if row["example_id"] in seen_ids:
                    raise TrainingIntegrityError(f"duplicate {label} example_id: {row['example_id']}")
                seen_ids.add(row["example_id"])
                total_records += 1
                if limit is None or len(records) < limit:
                    records.append(row)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise TrainingIntegrityError(f"cannot read {label} split: {error}") from error
    if not total_records:
        raise TrainingIntegrityError(f"{label} pair file must contain at least one record")
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or byte_count != after.st_size:
        raise TrainingIntegrityError(f"{label} split changed while it was being read")
    return BoundPairSplit(
        path=resolved,
        byte_count=byte_count,
        sha256="sha256:" + digest.hexdigest(),
        total_records=total_records,
        selected_records=len(records),
        records=tuple(records),
    )


def _manifest_split_contract(
    manifest: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    hashes = manifest.get("split_sha256", manifest.get("sha256"))
    counts = manifest.get("split_counts", manifest.get("counts"))
    if not isinstance(hashes, Mapping) or not isinstance(counts, Mapping):
        raise TrainingIntegrityError("dataset manifest requires split hashes and split counts")
    for split in ("train", "validation"):
        digest = hashes.get(split)
        count = counts.get(split)
        if not isinstance(digest, str) or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", digest):
            raise TrainingIntegrityError(f"dataset manifest requires a valid {split} SHA-256")
        _positive_int(count, f"dataset manifest {split} count")
    return hashes, counts


def bind_training_dataset(
    manifest_path: str | Path,
    *,
    train: str | Path,
    validation: str | Path,
    train_limit: int | None = None,
    validation_limit: int | None = None,
) -> BoundTrainingDataset:
    """Load and bind the exact train/validation bytes against one manifest."""

    resolved_manifest, manifest_bytes = _read_regular_bytes(
        manifest_path,
        "dataset manifest",
    )
    try:
        value = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainingIntegrityError(f"dataset manifest is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise TrainingIntegrityError("dataset manifest must contain an object")
    if value.get("synthetic_only") is not True:
        raise TrainingIntegrityError("dataset manifest must declare synthetic_only: true")
    if value.get("human_curation") is not False:
        raise TrainingIntegrityError("dataset manifest must declare human_curation: false")
    if value.get("split_leakage") is not False:
        raise TrainingIntegrityError("dataset manifest must declare split_leakage: false")
    if value.get("generative_api", False) is not False:
        raise TrainingIntegrityError("dataset manifest must declare generative_api: false")
    hashes, counts = _manifest_split_contract(value)
    bound_train = _load_bound_pair_split(train, label="train", limit=train_limit)
    bound_validation = _load_bound_pair_split(
        validation,
        label="validation",
        limit=validation_limit,
    )
    for split, bound in (
        ("train", bound_train),
        ("validation", bound_validation),
    ):
        expected_hash = str(hashes[split]).removeprefix("sha256:")
        actual_hash = bound.sha256.removeprefix("sha256:")
        if actual_hash != expected_hash:
            raise TrainingIntegrityError(
                f"{split} SHA-256 differs from dataset manifest: expected {expected_hash}, got {actual_hash}"
            )
        if bound.total_records != counts[split]:
            raise TrainingIntegrityError(
                f"{split} record count differs from dataset manifest: "
                f"expected {counts[split]}, got {bound.total_records}"
            )
    manifest_binding: dict[str, object] = {
        "path": str(resolved_manifest),
        "bytes": len(manifest_bytes),
        "sha256": _sha256(manifest_bytes),
        "declared_train_sha256": "sha256:" + str(hashes["train"]).removeprefix("sha256:"),
        "declared_validation_sha256": "sha256:" + str(hashes["validation"]).removeprefix("sha256:"),
        "declared_train_records": int(counts["train"]),
        "declared_validation_records": int(counts["validation"]),
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "split_leakage": False,
    }
    return BoundTrainingDataset(
        manifest=MappingProxyType(manifest_binding),
        train=bound_train,
        validation=bound_validation,
    )


def bind_training_order_plan(
    config: TrainingConfigSnapshot,
    dataset: BoundTrainingDataset,
) -> BoundTrainingOrderPlan | None:
    """Bind an optional canonical pilot order plan to exact config and train bytes."""

    mode = config.values.get("training_order_mode")
    if mode is None:
        return None
    raw_path = str(config.values["training_order_plan_path"])
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = config.path.parent / candidate
    resolved, payload = _read_regular_bytes(candidate, "training order plan")
    expected_sha256 = "sha256:" + str(config.values["training_order_plan_sha256"]).removeprefix("sha256:")
    actual_sha256 = _sha256(payload)
    if actual_sha256 != expected_sha256:
        raise TrainingIntegrityError(
            f"training order plan SHA-256 differs from config: expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        unvalidated = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainingIntegrityError(f"training order plan is invalid JSON: {error}") from error
    if not isinstance(unvalidated, Mapping):
        raise TrainingIntegrityError("training order plan must contain an object")
    try:
        plan = validate_curriculum_plan(unvalidated)
    except (TypeError, ValueError) as error:
        raise TrainingIntegrityError(f"training order plan validation failed: {error}") from error
    if payload != canonical_json_bytes(plan):
        raise TrainingIntegrityError("training order plan bytes are not the canonical immutable serialization")
    if config.values["run_kind"] != "pilot":
        raise TrainingIntegrityError("training order plan is pilot-only")
    if plan["mode"] != mode:
        raise TrainingIntegrityError("training order plan mode differs from training config")
    if plan["epochs"] != config.values.get("epochs"):
        raise TrainingIntegrityError("training order plan epochs differ from training config")
    if plan["seed"] != config.values["seed"]:
        raise TrainingIntegrityError("training order plan seed differs from training config")
    dataset_train_sha256 = dataset.train.sha256.removeprefix("sha256:")
    if plan["source"]["sha256"] != dataset_train_sha256:
        raise TrainingIntegrityError("training order plan source hash differs from bound train split")
    if plan["source"]["record_count"] != dataset.train.total_records:
        raise TrainingIntegrityError("training order plan source count differs from bound train split")
    if dataset.train.selected_records != dataset.train.total_records:
        raise TrainingIntegrityError("explicit training order forbids a partial train-limit selection")
    return BoundTrainingOrderPlan(
        path=resolved,
        byte_count=len(payload),
        sha256=actual_sha256,
        values=MappingProxyType(_json_copy(plan)),
    )


def _improvement_training_contract(
    config: Mapping[str, Any],
) -> dict[str, object]:
    """Resolve the exact no-override contract shared by both remediation arms."""

    micro_batch_size = int(config["micro_batch_size"])
    gradient_accumulation_steps = int(config["gradient_accumulation_steps"])
    effective_batch_size = micro_batch_size * gradient_accumulation_steps
    if effective_batch_size != 16:
        raise TrainingIntegrityError("improvement training requires effective batch size 16")
    return {
        "max_sequence_length": int(config["max_sequence_length"]),
        "micro_batch_size": micro_batch_size,
        "gradient_accumulation_steps": gradient_accumulation_steps,
        "effective_batch_size": effective_batch_size,
        "learning_rate": float(config["learning_rate"]),
        "optimizer": str(config["optimizer"]),
        "scheduler": "linear",
        "eval_steps": int(config["eval_steps"]),
        "save_steps": int(config["save_steps"]),
        "save_total_limit": int(config.get("save_total_limit", 2)),
        "warmup_ratio": float(config["warmup_ratio"]),
        "weight_decay": float(config["weight_decay"]),
        "max_grad_norm": float(config["max_grad_norm"]),
        "bf16": bool(config["bf16"]),
        "gradient_checkpointing": bool(config["gradient_checkpointing"]),
        "assistant_output_loss_only": bool(config["assistant_output_loss_only"]),
        "early_stopping": bool(config.get("early_stopping", False)),
    }


def _bind_improvement_safety_filter(
    split: BoundPairSplit,
    *,
    protection_application: Mapping[str, Any],
    label: str,
) -> tuple[list[str], list[str], list[str]]:
    """Reconstruct accepted/rejected source order from reason-bound evidence."""

    loaded_ids = [str(record["example_id"]) for record in split.records]
    loaded_set = set(loaded_ids)
    loaded_count = protection_application.get("loaded_record_count")
    accepted_count = protection_application.get("accepted_record_count")
    rejected_count = protection_application.get("rejected_record_count")
    if (
        loaded_count != len(loaded_ids)
        or isinstance(accepted_count, bool)
        or not isinstance(accepted_count, int)
        or isinstance(rejected_count, bool)
        or not isinstance(rejected_count, int)
        or accepted_count + rejected_count != len(loaded_ids)
    ):
        raise TrainingIntegrityError(f"{label} protection counts differ from the bound source")
    ids_by_reason = protection_application.get("rejection_example_ids_by_reason")
    hashes_by_reason = protection_application.get("rejection_example_ids_sha256_by_reason")
    reason_counts = protection_application.get("rejection_reason_counts")
    if (
        not isinstance(ids_by_reason, Mapping)
        or not isinstance(hashes_by_reason, Mapping)
        or not isinstance(reason_counts, Mapping)
        or set(ids_by_reason) != set(hashes_by_reason)
        or set(ids_by_reason) != set(reason_counts)
    ):
        raise TrainingIntegrityError(f"{label} protection evidence lacks exact per-reason bindings")
    rejected_set: set[str] = set()
    overlap_set: set[str] = set()
    for reason, raw_ids in ids_by_reason.items():
        if (
            not isinstance(reason, str)
            or not isinstance(raw_ids, list)
            or any(not isinstance(example_id, str) or not example_id for example_id in raw_ids)
            or len(set(raw_ids)) != len(raw_ids)
            or reason_counts[reason] != len(raw_ids)
            or hashes_by_reason[reason] != hash_identifier_sequence(raw_ids)
        ):
            raise TrainingIntegrityError(f"{label} per-reason rejection evidence is inconsistent")
        reason_set = set(raw_ids)
        if not reason_set <= loaded_set or rejected_set & reason_set:
            raise TrainingIntegrityError(f"{label} rejection reasons overlap or contain unknown IDs")
        rejected_set.update(reason_set)
        if reason == "terminal_url_punctuation_overlap":
            overlap_set.update(reason_set)
    if len(rejected_set) != rejected_count:
        raise TrainingIntegrityError(f"{label} rejection reasons do not cover all rejections")
    rejected_ids = [example_id for example_id in loaded_ids if example_id in rejected_set]
    accepted_ids = [example_id for example_id in loaded_ids if example_id not in rejected_set]
    overlap_ids = [example_id for example_id in loaded_ids if example_id in overlap_set]
    if (
        len(accepted_ids) != accepted_count
        or protection_application.get("accepted_example_ids_sha256") != hash_identifier_sequence(accepted_ids)
        or protection_application.get("rejected_example_ids_sha256") != hash_identifier_sequence(rejected_ids)
    ):
        raise TrainingIntegrityError(f"{label} accepted/rejected ID hashes differ from source order")
    return accepted_ids, rejected_ids, overlap_ids


def bind_improvement_order_plan(
    config: TrainingConfigSnapshot,
    dataset: BoundTrainingDataset,
    *,
    protection_policy: BoundProtectionPolicy,
    parent_checkpoint: BoundParentCheckpoint,
    protection_application: Mapping[str, Any],
) -> BoundImprovementOrderPlan:
    """Bind one canonical remediation order plan to all runtime safety inputs."""

    if config.values["run_kind"] != "improvement":
        raise TrainingIntegrityError("remediation order plans are improvement-only")
    if dataset.train.selected_records != dataset.train.total_records:
        raise TrainingIntegrityError("improvement order plans forbid a partial train-limit selection")
    raw_path = str(config.values["training_order_plan_path"])
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = config.path.parent / candidate
    resolved, payload = _read_regular_bytes(
        candidate,
        "remediation order plan",
    )
    expected_sha256 = "sha256:" + str(config.values["training_order_plan_sha256"]).removeprefix("sha256:")
    actual_sha256 = _sha256(payload)
    if actual_sha256 != expected_sha256:
        raise TrainingIntegrityError(
            f"remediation order plan SHA-256 differs from config: expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        unvalidated = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainingIntegrityError(f"remediation order plan is invalid JSON: {error}") from error
    if not isinstance(unvalidated, Mapping):
        raise TrainingIntegrityError("remediation order plan must contain an object")
    try:
        from .remediation_experiment import validate_remediation_order_plan

        plan = validate_remediation_order_plan(unvalidated)
    except (TypeError, ValueError) as error:
        raise TrainingIntegrityError(f"remediation order plan validation failed: {error}") from error
    if payload != canonical_json_bytes(plan):
        raise TrainingIntegrityError("remediation order plan bytes are not the canonical immutable serialization")
    if plan["mode"] != config.values["training_order_mode"]:
        raise TrainingIntegrityError("remediation order plan mode differs from training config")
    if plan["seed"] != config.values["seed"] or plan["epochs"] != config.values["epochs"]:
        raise TrainingIntegrityError("remediation order plan seed/epochs differ from training config")
    plan_policy = plan["protection_policy"]
    if (
        plan_policy["policy_id"] != protection_policy.values["policy_id"]
        or plan_policy["policy_sha256"] != protection_policy.values["policy_sha256"]
        or plan_policy["artifact_sha256"] != protection_policy.sha256.removeprefix("sha256:")
    ):
        raise TrainingIntegrityError("remediation order plan protection policy differs from runtime")
    source = plan["source"]
    if (
        source["filename"] != dataset.train.path.name
        or source["sha256"] != dataset.train.sha256.removeprefix("sha256:")
        or source["record_count"] != dataset.train.total_records
    ):
        raise TrainingIntegrityError("remediation order plan source differs from the bound train split")
    if (
        protection_application.get("policy_id") != protection_policy.values["policy_id"]
        or protection_application.get("policy_sha256") != protection_policy.values["policy_sha256"]
    ):
        raise TrainingIntegrityError("selected train protection evidence uses a different policy")
    selected_accepted_ids, selected_rejected_ids, _ = _bind_improvement_safety_filter(
        dataset.train,
        protection_application=protection_application,
        label="selected improvement train",
    )
    if (
        source["selected_safe_example_ids_sha256"] != hash_identifier_sequence(selected_accepted_ids)
        or selected_rejected_ids
    ):
        raise TrainingIntegrityError("selected remediation train is not the exact policy-safe plan source")
    safety_source = plan["safety_source"]
    safety_path = resolved.parent / safety_source["filename"]
    bound_safety_source = _load_bound_pair_split(
        safety_path,
        label="improvement safety source",
        limit=None,
    )
    if (
        bound_safety_source.sha256.removeprefix("sha256:") != safety_source["sha256"]
        or bound_safety_source.total_records != safety_source["record_count"]
    ):
        raise TrainingIntegrityError("remediation safety source differs from its plan binding")
    safety_audit_policy: Mapping[str, object] = protection_policy.values
    selection_policy_binding = plan.get("selection_policy")
    if selection_policy_binding is not None:
        selection_path, selection_payload = _read_regular_bytes(
            resolved.parent / selection_policy_binding["filename"],
            "remediation selection protection policy",
        )
        if _sha256(selection_payload).removeprefix("sha256:") != selection_policy_binding["artifact_sha256"]:
            raise TrainingIntegrityError("remediation selection policy SHA-256 differs from its plan binding")
        try:
            selection_raw = json.loads(selection_payload)
            selection_policy = validate_protection_policy_evidence(selection_raw)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ProtectionPolicyError,
        ) as error:
            raise TrainingIntegrityError(f"remediation selection policy is invalid: {error}") from error
        if (
            selection_payload != _canonical_json(selection_policy)
            or selection_policy["policy_id"] != selection_policy_binding["policy_id"]
            or selection_policy["policy_sha256"] != selection_policy_binding["policy_sha256"]
        ):
            raise TrainingIntegrityError("remediation selection policy differs from its plan binding")
        safety_audit_policy = selection_policy
    try:
        from .training_runtime import apply_training_protection

        audited_protection = apply_training_protection(
            bound_safety_source.records,
            policy=safety_audit_policy,
        )
    except ValueError as error:
        raise TrainingIntegrityError(f"cannot reproduce remediation safety filter: {error}") from error
    audit_accepted_ids, audit_rejected_ids, audit_overlap_ids = _bind_improvement_safety_filter(
        bound_safety_source,
        protection_application=audited_protection.evidence,
        label="improvement safety source",
    )
    audit_records_by_id = {str(record["example_id"]): record for record in bound_safety_source.records}
    audit_accepted_set = set(audit_accepted_ids)
    for selected_record in dataset.train.records:
        example_id = str(selected_record["example_id"])
        if example_id not in audit_accepted_set or audit_records_by_id.get(example_id) != selected_record:
            raise TrainingIntegrityError("selected remediation train is not an exact safe-source subset")
    safety = plan["safety_filter"]
    if (
        safety["loaded_record_count"] != bound_safety_source.total_records
        or safety["accepted_record_count"] != len(audit_accepted_ids)
        or safety["rejected_record_count"] != len(audit_rejected_ids)
        or safety["rejected_example_ids_sha256"] != hash_identifier_sequence(audit_rejected_ids)
        or safety["url_overlap_rejected_example_ids"] != audit_overlap_ids
        or safety["url_overlap_rejected_example_ids_sha256"] != hash_identifier_sequence(audit_overlap_ids)
    ):
        raise TrainingIntegrityError("remediation plan safety filter differs from reproduced policy output")
    if (
        plan["parent_checkpoint"]["tree_sha256"] != parent_checkpoint.tree_sha256
        or plan["parent_checkpoint"]["relationship"] != "warm_start_parent_not_resume"
    ):
        raise TrainingIntegrityError("remediation order plan parent differs from runtime warm start")
    if plan["training_contract"] != _improvement_training_contract(config.values):
        raise TrainingIntegrityError("remediation order plan training contract differs from config")
    return BoundImprovementOrderPlan(
        path=resolved,
        byte_count=len(payload),
        sha256=actual_sha256,
        values=MappingProxyType(_json_copy(plan)),
    )


def bind_protection_policy(
    config: TrainingConfigSnapshot,
    *,
    tokenizer: Any,
) -> BoundProtectionPolicy | None:
    """Bind an explicit new policy and prove it against the exact tokenizer."""

    raw_path = config.values.get("protection_policy_path")
    if raw_path is None:
        return None
    candidate = Path(str(raw_path))
    if not candidate.is_absolute():
        candidate = config.path.parent / candidate
    resolved, payload = _read_regular_bytes(candidate, "protection policy")
    expected_sha256 = "sha256:" + str(config.values["protection_policy_sha256"]).removeprefix("sha256:")
    actual_sha256 = _sha256(payload)
    if actual_sha256 != expected_sha256:
        raise TrainingIntegrityError(
            f"protection policy SHA-256 differs from config: expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        unvalidated = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainingIntegrityError(f"protection policy is invalid JSON: {error}") from error
    if not isinstance(unvalidated, Mapping):
        raise TrainingIntegrityError("protection policy must contain an object")
    try:
        policy = validate_protection_policy_evidence(
            unvalidated,
            tokenizer=tokenizer,
        )
    except ProtectionPolicyError as error:
        raise TrainingIntegrityError(f"protection policy validation failed: {error}") from error
    if policy["policy_id"] not in {
        GEMMA_UNUSED_POLICY_ID,
        LEXICAL_KEEP_POLICY_ID,
    }:
        raise TrainingIntegrityError("improvement/final training requires an approved Gemma protection policy")
    if payload != _canonical_json(policy):
        raise TrainingIntegrityError("protection policy bytes are not the canonical immutable serialization")
    return BoundProtectionPolicy(
        path=resolved,
        byte_count=len(payload),
        sha256=actual_sha256,
        values=MappingProxyType(_json_copy(policy)),
    )


def _checkpoint_tree_binding(root: Path) -> tuple[str, tuple[Mapping[str, object], ...]]:
    if root.is_symlink() or not root.is_dir():
        raise TrainingIntegrityError(f"parent checkpoint must be a regular directory: {root}")
    files: list[Mapping[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise TrainingIntegrityError(f"parent checkpoint must not contain symlinks: {path}")
        if not path.is_file():
            continue
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
            raise TrainingIntegrityError(f"parent checkpoint file changed while hashing: {path}")
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
        )
    if not files:
        raise TrainingIntegrityError("parent checkpoint contains no files")
    return _sha256(_canonical_json(files)), tuple(files)


def checkpoint_tree_sha256(checkpoint: str | Path) -> str:
    """Return the exact tree hash expected by a warm-start config."""

    return _checkpoint_tree_binding(Path(checkpoint).resolve())[0]


def bind_parent_checkpoint(
    config: TrainingConfigSnapshot,
) -> BoundParentCheckpoint | None:
    """Bind an immutable warm-start checkpoint for improvement or final SFT."""

    raw_path = config.values.get("parent_checkpoint_path")
    if raw_path is None:
        return None
    candidate = Path(str(raw_path))
    if not candidate.is_absolute():
        candidate = config.path.parent / candidate
    root = candidate.resolve()
    tree_sha256, files = _checkpoint_tree_binding(root)
    expected = "sha256:" + str(config.values["parent_checkpoint_tree_sha256"]).removeprefix("sha256:")
    if tree_sha256 != expected:
        raise TrainingIntegrityError(
            f"parent checkpoint tree SHA-256 differs from config: expected {expected}, got {tree_sha256}"
        )
    required = {"config.json", "tokenizer_config.json"}
    paths = {str(item["path"]) for item in files}
    if not required <= paths or not any(path.endswith(".safetensors") for path in paths):
        raise TrainingIntegrityError("parent checkpoint lacks model or tokenizer artifacts")
    return BoundParentCheckpoint(
        path=root,
        tree_sha256=tree_sha256,
        file_count=len(files),
        total_bytes=sum(int(item["bytes"]) for item in files),
        files=files,
    )


def capture_tokenizer_identity(
    tokenizer: Any,
    *,
    model_id: str,
    revision: str,
    local_source_path: Path | None = None,
) -> dict[str, object]:
    """Bind the actual tokenizer backend, template, vocabulary, and special IDs."""

    if model_id != BASE_MODEL_ID or revision != BASE_MODEL_REVISION:
        raise TrainingIntegrityError("tokenizer identity must use the pinned Gemma base")
    tokenizer_source = getattr(tokenizer, "name_or_path", None)
    if local_source_path is None:
        if tokenizer_source != model_id:
            raise TrainingIntegrityError(f"tokenizer name_or_path must equal the pinned model id {model_id}")
    else:
        try:
            expected_local_source = Path(local_source_path).resolve(strict=True)
            actual_local_source = Path(str(tokenizer_source)).resolve(strict=True)
        except (OSError, TypeError, ValueError) as error:
            raise TrainingIntegrityError("tokenizer local source cannot be resolved exactly") from error
        if not expected_local_source.is_dir() or actual_local_source != expected_local_source:
            raise TrainingIntegrityError("tokenizer name_or_path does not match the explicitly bound local source")
    backend = getattr(tokenizer, "backend_tokenizer", None)
    if backend is None:
        backend = getattr(tokenizer, "_tokenizer", None)
    serializer = getattr(backend, "to_str", None)
    if not callable(serializer):
        raise TrainingIntegrityError("tokenizer backend cannot provide an exact serialization")
    serialized = serializer()
    if not isinstance(serialized, str) or not serialized:
        raise TrainingIntegrityError("tokenizer backend serialization is empty")
    vocab_size = getattr(tokenizer, "vocab_size", None)
    _positive_int(vocab_size, "tokenizer vocab_size")
    chat_template = getattr(tokenizer, "chat_template", None)
    if not isinstance(chat_template, str | dict) or not chat_template:
        raise TrainingIntegrityError("tokenizer chat_template must be non-empty")
    special_ids: dict[str, int | None] = {}
    for name in ("pad_token_id", "eos_token_id", "bos_token_id", "unk_token_id"):
        value = getattr(tokenizer, name, None)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise TrainingIntegrityError(f"tokenizer {name} must be an integer or null")
        special_ids[name] = value
    identity: dict[str, object] = {
        "model_id": model_id,
        "revision": revision,
        "class": f"{type(tokenizer).__module__}.{type(tokenizer).__qualname__}",
        "name_or_path": model_id,
        "vocab_size": vocab_size,
        "special_token_ids": special_ids,
        "backend_bytes": len(serialized.encode("utf-8")),
        "backend_sha256": _sha256(serialized.encode("utf-8")),
        "chat_template_sha256": _sha256(_canonical_json(chat_template)),
    }
    identity["identity_sha256"] = _sha256(_canonical_json(identity))
    return identity


def _run_git(repository: Path, arguments: list[str], label: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=False,
            capture_output=True,
            shell=False,
        )
    except OSError as error:
        raise TrainingIntegrityError(f"cannot run git for {label}: {error}") from error
    if completed.returncode != 0:
        error_text = completed.stderr.decode("utf-8", errors="replace").strip()
        raise TrainingIntegrityError(f"git {label} failed: {error_text}")
    return completed.stdout


def capture_code_identity(
    repository: str | Path,
    code_paths: list[str | Path],
) -> dict[str, object]:
    """Bind Git HEAD, scoped worktree state, and exact training-code file bytes."""

    repository_path = Path(repository)
    if repository_path.is_symlink():
        raise TrainingIntegrityError(f"code repository must not be a symlink: {repository_path}")
    root = repository_path.resolve()
    if not root.is_dir():
        raise TrainingIntegrityError(f"code repository is not a directory: {root}")
    discovered_root = Path(
        _run_git(root, ["rev-parse", "--show-toplevel"], "root").decode("utf-8", errors="strict").strip()
    ).resolve()
    if discovered_root != root:
        raise TrainingIntegrityError(f"code repository must be the Git root: expected {discovered_root}, got {root}")
    head = _run_git(root, ["rev-parse", "HEAD"], "revision").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise TrainingIntegrityError(f"git HEAD is not a full commit id: {head!r}")
    if not code_paths:
        raise TrainingIntegrityError("code identity requires at least one source file")
    file_records: list[dict[str, object]] = []
    pathspecs: list[str] = []
    seen: set[str] = set()
    for raw_path in code_paths:
        resolved, payload = _read_regular_bytes(raw_path, "training code")
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as error:
            raise TrainingIntegrityError(f"training code is outside the repository: {resolved}") from error
        if relative in seen:
            raise TrainingIntegrityError(f"duplicate training code path: {relative}")
        seen.add(relative)
        pathspecs.append(relative)
        file_records.append(
            {
                "path": relative,
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
        )
    file_records.sort(key=lambda item: str(item["path"]))
    pathspecs.sort()
    status = _run_git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspecs],
        "status",
    )
    diff = _run_git(
        root,
        ["diff", "--binary", "HEAD", "--", *pathspecs],
        "worktree diff",
    )
    tree_payload = _canonical_json(file_records)
    return {
        "repository": str(root),
        "git_head": head,
        "dirty": bool(status),
        "worktree_status_entry_count": status.count(b"\0"),
        "worktree_status_sha256": _sha256(status),
        "worktree_diff_sha256": _sha256(diff),
        "code_tree_sha256": _sha256(tree_payload),
        "files": file_records,
    }


def fresh_reload_local_model(
    checkpoint: str | Path,
    *,
    model_loader: Callable[..., Any] | None = None,
    tokenizer_loader: Callable[..., Any] | None = None,
) -> dict[str, object]:
    """Freshly instantiate tokenizer and model from only the staged local files."""

    checkpoint_path = Path(checkpoint)
    if checkpoint_path.is_symlink():
        raise TrainingIntegrityError(f"reload checkpoint must not be a symlink: {checkpoint_path}")
    root = checkpoint_path.resolve()
    if not root.is_dir():
        raise TrainingIntegrityError(f"reload checkpoint is not a directory: {root}")
    if model_loader is None or tokenizer_loader is None:
        from transformers import AutoModelForCausalLM, AutoTokenizer

        model_loader = model_loader or AutoModelForCausalLM.from_pretrained
        tokenizer_loader = tokenizer_loader or AutoTokenizer.from_pretrained
    local_path = str(root)
    tokenizer = tokenizer_loader(
        local_path,
        local_files_only=True,
        trust_remote_code=False,
        fix_mistral_regex=False,
    )
    protection_policy_path = root / PROTECTION_POLICY_FILENAME
    protection_policy_sha256: str | None = None
    if protection_policy_path.exists():
        resolved_policy, policy_bytes = _read_regular_bytes(
            protection_policy_path,
            "saved protection policy",
        )
        try:
            raw_policy = json.loads(policy_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TrainingIntegrityError(f"saved protection policy is invalid JSON: {error}") from error
        try:
            validated_policy = validate_protection_policy_evidence(
                raw_policy,
                tokenizer=tokenizer,
            )
        except ProtectionPolicyError as error:
            raise TrainingIntegrityError(f"saved protection policy validation failed: {error}") from error
        if policy_bytes != _canonical_json(validated_policy):
            raise TrainingIntegrityError("saved protection policy is not canonical immutable JSON")
        protection_policy_sha256 = _sha256(resolved_policy.read_bytes())
    model = model_loader(
        local_path,
        local_files_only=True,
        trust_remote_code=False,
        dtype="auto",
        attn_implementation="sdpa",
        low_cpu_mem_usage=True,
    )
    evaluator = getattr(model, "eval", None)
    if not callable(evaluator):
        raise TrainingIntegrityError("freshly reloaded model has no eval method")
    evaluator()
    model_type = getattr(getattr(model, "config", None), "model_type", None)
    if not isinstance(model_type, str) or not model_type:
        raise TrainingIntegrityError("freshly reloaded model has no model_type")
    counter = getattr(model, "num_parameters", None)
    if not callable(counter):
        raise TrainingIntegrityError("freshly reloaded model cannot count parameters")
    parameter_count = counter()
    _positive_int(parameter_count, "freshly reloaded parameter_count")
    result: dict[str, object] = {
        "status": "passed",
        "loader": "transformers_local",
        "model_type": model_type,
        "parameter_count": parameter_count,
        "tokenizer_class": f"{type(tokenizer).__module__}.{type(tokenizer).__qualname__}",
    }
    if protection_policy_sha256 is not None:
        result["protection_policy_sha256"] = protection_policy_sha256
    return result


def build_run_identity(
    *,
    config: TrainingConfigSnapshot,
    dataset: BoundTrainingDataset,
    code: Mapping[str, object],
    cli: Mapping[str, object],
    tokenizer: Mapping[str, object],
    runtime: Mapping[str, object],
    effective_training: Mapping[str, object],
    training_order: Mapping[str, object] | None = None,
    protection_policy: Mapping[str, object] | None = None,
    protection_application: Mapping[str, object] | None = None,
    parent_checkpoint: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the content-derived identity for one exact immutable training run."""

    bindings: dict[str, object] = {
        "config": config.binding(),
        "dataset": dataset.binding(),
        "code": _json_copy(code),
        "cli": _json_copy(cli),
        "tokenizer": _json_copy(tokenizer),
        "runtime": _json_copy(runtime),
        "effective_training": _json_copy(effective_training),
    }
    if training_order is not None:
        bindings["training_order"] = _json_copy(training_order)
    if protection_policy is not None:
        bindings["protection_policy"] = _json_copy(protection_policy)
    if protection_application is not None:
        bindings["protection_application"] = _json_copy(protection_application)
    if parent_checkpoint is not None:
        bindings["parent_checkpoint"] = _json_copy(parent_checkpoint)
    identity: dict[str, object] = {
        "schema_version": 1,
        "fingerprint_algorithm": "sha256-canonical-json-v1",
        "bindings": bindings,
    }
    identity["run_fingerprint"] = _sha256(_canonical_json(identity))
    return identity


def _assert_identity_self_hash(identity: Mapping[str, object]) -> None:
    fingerprint = identity.get("run_fingerprint")
    if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
        raise TrainingIntegrityError("run identity has an invalid fingerprint")
    payload = dict(identity)
    del payload["run_fingerprint"]
    expected = _sha256(_canonical_json(payload))
    if fingerprint != expected:
        raise TrainingIntegrityError(f"run identity fingerprint mismatch: expected {expected}, got {fingerprint}")


def _write_exclusive(path: Path, payload: bytes, label: str) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise TrainingIntegrityError(f"{label} already exists: {path}") from error


def prepare_run_identity(
    output_dir: str | Path,
    identity: Mapping[str, object],
    *,
    resume: bool,
) -> Path:
    """Create a fresh immutable identity or verify it byte-for-byte for resume."""

    _assert_identity_self_hash(identity)
    output_path = Path(output_dir)
    if output_path.is_symlink():
        raise TrainingIntegrityError(f"training output must not be a symlink: {output_path}")
    root = output_path.resolve()
    if root.exists() and not root.is_dir():
        raise TrainingIntegrityError(f"training output must be a directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    identity_path = root / _RUN_IDENTITY_FILENAME
    payload = _canonical_json(identity)
    if resume:
        if not identity_path.is_file() or identity_path.is_symlink():
            raise TrainingIntegrityError("resume requires an existing regular run identity")
        existing = identity_path.read_bytes()
        if existing != payload:
            try:
                prior = json.loads(existing)
                prior_fingerprint = prior.get("run_fingerprint")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                prior_fingerprint = "invalid"
            raise TrainingIntegrityError(
                "resume run fingerprint differs from immutable identity: "
                f"existing {prior_fingerprint}, current {identity['run_fingerprint']}"
            )
        return identity_path
    if identity_path.exists():
        raise TrainingIntegrityError(f"fresh run identity already exists in output directory: {identity_path}")
    unexpected = [path for path in root.iterdir()]
    if unexpected:
        raise TrainingIntegrityError(f"fresh run output directory is not empty: {unexpected[0].name}")
    _write_exclusive(identity_path, payload, "run identity")
    return identity_path


def write_immutable_json(path: str | Path, value: Mapping[str, object]) -> Path:
    """Write one canonical JSON artifact once, allowing only byte-identical replay."""

    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(_json_copy(value))
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != payload:
            raise TrainingIntegrityError(f"refusing to replace different immutable JSON: {destination}")
        return destination
    _write_exclusive(destination, payload, "immutable JSON")
    return destination


def record_invocation_evidence(
    output_dir: str | Path,
    *,
    run_fingerprint: str,
    argv: list[str],
    working_directory: str | Path,
    resume_checkpoint: Mapping[str, object] | None,
    training_order: Mapping[str, object] | None = None,
) -> Path:
    """Persist the exact current argv and resume evidence before model training."""

    if not _SHA256_RE.fullmatch(run_fingerprint):
        raise TrainingIntegrityError("invocation run fingerprint is invalid")
    if not argv or any(not isinstance(argument, str) or not argument or "\0" in argument for argument in argv):
        raise TrainingIntegrityError("invocation argv must contain non-empty strings")
    cwd = Path(working_directory).resolve()
    if cwd.is_symlink() or not cwd.is_dir():
        raise TrainingIntegrityError(f"invocation working directory is invalid: {cwd}")
    argv_sha256 = _sha256(_canonical_json(argv))
    evidence: dict[str, object] = {
        "schema_version": 1,
        "run_fingerprint": run_fingerprint,
        "argv": list(argv),
        "argv_sha256": argv_sha256,
        "working_directory": str(cwd),
        "resume_checkpoint": _json_copy(resume_checkpoint) if resume_checkpoint is not None else None,
    }
    if training_order is not None:
        evidence["training_order"] = _json_copy(training_order)
    evidence_sha256 = _sha256(_canonical_json(evidence)).removeprefix("sha256:")
    path = Path(output_dir).resolve() / "invocations" / f"invocation-{evidence_sha256}.json"
    return write_immutable_json(path, evidence)


def record_checkpoint_identity(
    checkpoint: str | Path,
    *,
    run_fingerprint: str,
    global_step: int,
) -> Path:
    """Bind one Trainer checkpoint to the run identity and directory step."""

    if not _SHA256_RE.fullmatch(run_fingerprint):
        raise TrainingIntegrityError("checkpoint run fingerprint is invalid")
    step = _positive_int(global_step, "checkpoint global_step")
    root = Path(checkpoint).resolve()
    match = _CHECKPOINT_RE.fullmatch(root.name)
    if not root.is_dir() or not match or int(match.group(1)) != step:
        raise TrainingIntegrityError("checkpoint directory name must match its positive global_step")
    path = root / _CHECKPOINT_IDENTITY_FILENAME
    payload = _canonical_json(
        {
            "schema_version": 1,
            "run_fingerprint": run_fingerprint,
            "global_step": step,
        }
    )
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise TrainingIntegrityError(f"checkpoint identity collision at {path}")
        return path
    _write_exclusive(path, payload, "checkpoint identity")
    return path


def verify_resume_checkpoint(
    checkpoint: str | Path,
    *,
    expected_run_fingerprint: str,
) -> dict[str, object]:
    """Verify checkpoint directory, bound identity, and Trainer state agree."""

    if not _SHA256_RE.fullmatch(expected_run_fingerprint):
        raise TrainingIntegrityError("expected resume fingerprint is invalid")
    root = Path(checkpoint).resolve()
    match = _CHECKPOINT_RE.fullmatch(root.name)
    if not root.is_dir() or not match:
        raise TrainingIntegrityError("resume path is not a numbered checkpoint directory")
    directory_step = int(match.group(1))
    identity_path = _regular_file(
        root / _CHECKPOINT_IDENTITY_FILENAME,
        "checkpoint identity",
    )
    try:
        identity = json.loads(identity_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainingIntegrityError(f"checkpoint identity is invalid: {error}") from error
    if not isinstance(identity, dict) or set(identity) != {
        "schema_version",
        "run_fingerprint",
        "global_step",
    }:
        raise TrainingIntegrityError("checkpoint identity has unexpected fields")
    if identity.get("schema_version") != 1:
        raise TrainingIntegrityError("checkpoint identity schema_version must equal 1")
    if identity.get("run_fingerprint") != expected_run_fingerprint:
        raise TrainingIntegrityError("checkpoint run fingerprint differs from expected run fingerprint")
    if identity.get("global_step") != directory_step:
        raise TrainingIntegrityError("checkpoint identity global_step differs from directory name")
    trainer_state_path, trainer_state_bytes = _read_regular_bytes(
        root / "trainer_state.json",
        "checkpoint trainer state",
    )
    try:
        trainer_state = json.loads(trainer_state_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainingIntegrityError(f"checkpoint trainer state is invalid: {error}") from error
    if not isinstance(trainer_state, dict) or trainer_state.get("global_step") != directory_step:
        raise TrainingIntegrityError("checkpoint trainer_state global_step differs from directory name")
    return {
        "checkpoint": root.name,
        "global_step": directory_step,
        "run_fingerprint": expected_run_fingerprint,
        "identity_sha256": _sha256(identity_path.read_bytes()),
        "trainer_state_path": str(trainer_state_path),
        "trainer_state_sha256": _sha256(trainer_state_bytes),
    }


def verify_checkpoint_protection_policy(
    checkpoint: str | Path,
    *,
    expected_policy: Mapping[str, object],
    tokenizer: Any,
) -> dict[str, object]:
    """Verify a checkpoint's policy file and model config before resume/use."""

    root = Path(checkpoint).resolve()
    resolved, payload = _read_regular_bytes(
        root / PROTECTION_POLICY_FILENAME,
        "checkpoint protection policy",
    )
    try:
        raw_policy = json.loads(payload)
        policy = validate_protection_policy_evidence(
            raw_policy,
            tokenizer=tokenizer,
        )
        expected = validate_protection_policy_evidence(expected_policy)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ProtectionPolicyError,
    ) as error:
        raise TrainingIntegrityError(f"checkpoint protection policy validation failed: {error}") from error
    if payload != _canonical_json(policy) or policy != expected:
        raise TrainingIntegrityError("checkpoint protection policy differs from the expected run policy")
    config_path, config_payload = _read_regular_bytes(
        root / "config.json",
        "checkpoint model config",
    )
    try:
        model_config = json.loads(config_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainingIntegrityError(f"checkpoint model config is invalid JSON: {error}") from error
    if (
        not isinstance(model_config, Mapping)
        or model_config.get("scriber_protection_policy_id") != policy["policy_id"]
        or model_config.get("scriber_protection_policy_sha256") != policy["policy_sha256"]
    ):
        raise TrainingIntegrityError("checkpoint model config does not bind its protection policy")
    return {
        "path": str(resolved),
        "sha256": _sha256(payload),
        "policy_id": policy["policy_id"],
        "policy_sha256": policy["policy_sha256"],
        "model_config_path": str(config_path),
        "model_config_sha256": _sha256(config_payload),
    }


def summarize_checkpoint_state(
    output_dir: str | Path,
    *,
    trainer_state: Any,
    run_fingerprint: str,
) -> dict[str, object]:
    """Persist the completed step, best metric, and all surviving checkpoint bindings."""

    output_path = Path(output_dir)
    if output_path.is_symlink():
        raise TrainingIntegrityError(f"checkpoint output must not be a symlink: {output_path}")
    root = output_path.resolve()
    if not root.is_dir():
        raise TrainingIntegrityError(f"checkpoint output is not a directory: {root}")
    global_step = _positive_int(
        getattr(trainer_state, "global_step", None),
        "trainer global_step",
    )
    epoch = getattr(trainer_state, "epoch", None)
    if epoch is not None:
        epoch = _finite_number(
            epoch,
            "trainer epoch",
            minimum=0.0,
            inclusive_minimum=True,
        )
    best_metric = getattr(trainer_state, "best_metric", None)
    if best_metric is not None:
        if isinstance(best_metric, bool) or not isinstance(best_metric, int | float):
            raise TrainingIntegrityError("trainer best_metric must be numeric or null")
        best_metric = float(best_metric)
        if not math.isfinite(best_metric):
            raise TrainingIntegrityError("trainer best_metric must be finite")
    checkpoints: list[tuple[int, Path]] = []
    for path in root.glob("checkpoint-*"):
        match = _CHECKPOINT_RE.fullmatch(path.name)
        if path.is_dir() and match:
            checkpoints.append((int(match.group(1)), path))
    checkpoints.sort()
    if not checkpoints:
        raise TrainingIntegrityError("completed training has no surviving checkpoints")
    evidence = [
        verify_resume_checkpoint(
            checkpoint,
            expected_run_fingerprint=run_fingerprint,
        )
        for _step, checkpoint in checkpoints
    ]
    best_path_value = getattr(trainer_state, "best_model_checkpoint", None)
    best_name: str | None = None
    if best_path_value is not None:
        best_path = Path(best_path_value).resolve()
        if best_path.parent != root or best_path not in {path for _step, path in checkpoints}:
            raise TrainingIntegrityError("trainer best_model_checkpoint is not a surviving run checkpoint")
        best_name = best_path.name
    return {
        "global_step": global_step,
        "epoch": epoch,
        "best_metric": best_metric,
        "best_model_checkpoint": best_name,
        "last_checkpoint": checkpoints[-1][1].name,
        "surviving_checkpoints": evidence,
    }


def publish_final_model(
    output_dir: str | Path,
    *,
    run_fingerprint: str,
    save_artifacts: Callable[[Path], None],
    reload_verify: Callable[[Path], Mapping[str, object]],
    protection_policy: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Save, freshly reload, inventory, and atomically publish one final model."""

    if not _SHA256_RE.fullmatch(run_fingerprint):
        raise TrainingIntegrityError("final-model run fingerprint is invalid")
    output_path = Path(output_dir)
    if output_path.is_symlink():
        raise TrainingIntegrityError(f"final-model output must not be a symlink: {output_path}")
    root = output_path.resolve()
    if root.exists() and not root.is_dir():
        raise TrainingIntegrityError(f"final-model output must be a directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    short = run_fingerprint.removeprefix("sha256:")[:16]
    destination = root / f"final-{short}"
    lock = root / f".final-{short}.publish.lock"
    if destination.exists():
        raise TrainingIntegrityError(f"final-model destination already exists: {destination}")
    try:
        _write_exclusive(lock, run_fingerprint.encode("ascii") + b"\n", "final publish lock")
    except TrainingIntegrityError as error:
        raise TrainingIntegrityError(f"final-model publication collision: {error}") from error
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".final-{short}.staging-",
            dir=root,
        )
    )
    try:
        try:
            save_artifacts(staging)
        except Exception as error:
            raise TrainingIntegrityError(f"final-model save failed in staging: {error}") from error
        if protection_policy is not None:
            try:
                validated_policy = validate_protection_policy_evidence(protection_policy)
            except ProtectionPolicyError as error:
                raise TrainingIntegrityError(f"final-model protection policy is invalid: {error}") from error
            _write_exclusive(
                staging / PROTECTION_POLICY_FILENAME,
                _canonical_json(validated_policy),
                "final-model protection policy",
            )
        try:
            reload_report = dict(reload_verify(staging))
        except Exception as error:
            raise TrainingIntegrityError(f"final-model fresh reload failed: {error}") from error
        if reload_report.get("status") != "passed":
            raise TrainingIntegrityError("final-model fresh reload did not report status=passed")
        reload_payload = {
            **reload_report,
            "schema_version": 1,
            "run_fingerprint": run_fingerprint,
            "status": "passed",
        }
        _write_exclusive(
            staging / _RELOAD_FILENAME,
            _canonical_json(reload_payload),
            "fresh reload verification",
        )
        try:
            inventory = inventory_bf16_checkpoint(
                staging,
                source_id=f"training-run:{run_fingerprint}",
            )
        except QuantizationError as error:
            raise TrainingIntegrityError(f"final-model inventory failed: {error}") from error
        _write_exclusive(
            staging / _INVENTORY_FILENAME,
            _canonical_json(inventory),
            "checkpoint inventory",
        )
        if destination.exists():
            raise TrainingIntegrityError(f"final-model destination already exists: {destination}")
        try:
            staging.rename(destination)
        except OSError as error:
            raise TrainingIntegrityError(f"atomic final-model rename failed: {error}") from error
        return {
            "directory": destination.name,
            "path": str(destination),
            "run_fingerprint": run_fingerprint,
            "inventory": inventory,
            "reload_verification": reload_payload,
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        try:
            lock.unlink(missing_ok=True)
        except OSError as error:
            raise TrainingIntegrityError(f"could not release final publish lock: {error}") from error
