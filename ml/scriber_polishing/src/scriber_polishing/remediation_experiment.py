"""Fail-closed construction of a matched protected-span remediation A/B bundle.

This module is deliberately static: it reads frozen evidence, chooses one shared
training subset and one validation-only parity set, and writes plans/configs. It
never loads a model, starts training, opens a holdout split, or chooses a winner.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .curriculum import (
    PHASE_RULES_VERSION,
    PHASES,
    canonical_json_bytes,
    classify_curriculum_phase,
    hash_identifier_sequence,
    load_hash_bound_jsonl,
    sha256_bytes,
)
from .curriculum_comparison import load_curriculum_comparison_config
from .evaluation_reference_builder import build_evaluation_references
from .model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from .protected_spans import (
    GEMMA_UNUSED_POLICY_ID,
    LEXICAL_KEEP_POLICY_ID,
    validate_protection_policy_evidence,
)
from .training_integrity import checkpoint_tree_sha256
from .training_runtime import ProtectedTrainingRecords, apply_training_protection

REMEDIATION_PLAN_KIND = "scriber_remediation_order_plan"
REMEDIATION_PLAN_ALGORITHM = "matched_remediation_seeded_sha256_v1"
REMEDIATION_SUBSET_ALGORITHM = "equal_phase_safe_sha256_bottom_k_v1"
REMEDIATION_DEV_ALGORITHM = "phase_stratified_safe_validation_sha256_bottom_k_v1"
MAX_REMEDIATION_UPDATES = 300
MAX_REMEDIATION_EXAMPLES = 4800
REQUIRED_EFFECTIVE_BATCH_SIZE = 16

_PROJECT_ROOT = Path(__file__).parents[2]
_CONTRACTS_ROOT = _PROJECT_ROOT / "contracts"
_RAW_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREFIXED_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,299}$")


class RemediationExperimentError(ValueError):
    """Frozen remediation evidence is incomplete, inconsistent, or unsafe."""


@dataclass(frozen=True, slots=True)
class RemediationExperimentConfig:
    """One exact builder config plus its immutable byte identity."""

    path: Path
    byte_count: int
    sha256: str
    values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RemediationExperimentBundle:
    """The final validated manifest and the atomically materialized directory."""

    output_dir: Path
    manifest: Mapping[str, Any]


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
            raise RemediationExperimentError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@cache
def _validator(file_name: str) -> Draft202012Validator:
    try:
        schema = json.loads(
            (_CONTRACTS_ROOT / file_name).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise RemediationExperimentError(
            f"cannot load remediation schema {file_name}: {error}"
        ) from error
    return Draft202012Validator(schema)


def _json_copy(value: object) -> Any:
    if isinstance(value, Mapping):
        value = {
            str(key): _json_copy(item) for key, item in value.items()
        }
    elif isinstance(value, tuple | list):
        value = [_json_copy(item) for item in value]
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as error:
        raise RemediationExperimentError(
            "remediation evidence must be finite JSON data"
        ) from error


def _validate_schema(
    value: Mapping[str, Any],
    *,
    schema_file: str,
    label: str,
) -> dict[str, Any]:
    materialized = _json_copy(value)
    errors = sorted(
        _validator(schema_file).iter_errors(materialized),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path)
        prefix = f" at {location}" if location else ""
        raise RemediationExperimentError(
            f"{label} schema failed{prefix}: {errors[0].message}"
        )
    return materialized


def _stable_regular_bytes(
    path: str | Path,
    label: str,
) -> tuple[Path, bytes]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise RemediationExperimentError(
            f"{label} must not be a symlink: {candidate}"
        )
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise RemediationExperimentError(
            f"{label} must be a regular file: {resolved}"
        )
    before = resolved.stat()
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise RemediationExperimentError(
            f"cannot read {label}: {error}"
        ) from error
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
        raise RemediationExperimentError(
            f"{label} changed while it was being read"
        )
    return resolved, payload


def _resolve_from(base_dir: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RemediationExperimentError(f"{label} must be non-empty path text")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _require_raw_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _RAW_SHA256.fullmatch(value) is None:
        raise RemediationExperimentError(
            f"{label} must be 64 lowercase hexadecimal characters"
        )
    return value


def _require_prefixed_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _PREFIXED_SHA256.fullmatch(value) is None:
        raise RemediationExperimentError(
            f"{label} must use sha256:<64 lowercase hexadecimal characters>"
        )
    return value


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RemediationExperimentError(f"{label} is not a stable identifier")
    return value


def _reject_placeholders(value: object, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_placeholders(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_placeholders(item, f"{path}[{index}]")
    elif isinstance(value, str) and "REPLACE_" in value:
        raise RemediationExperimentError(
            f"{path} still contains a non-executable placeholder"
        )


def validate_remediation_experiment_config(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the closed builder config and its cross-field training budget."""

    validated = _validate_schema(
        value,
        schema_file="remediation_experiment_config_schema.json",
        label="remediation experiment config",
    )
    _reject_placeholders(validated)
    training = validated["training"]
    actual_effective_batch = (
        training["micro_batch_size"]
        * training["gradient_accumulation_steps"]
    )
    if (
        actual_effective_batch != REQUIRED_EFFECTIVE_BATCH_SIZE
        or training["effective_batch_size"] != actual_effective_batch
    ):
        raise RemediationExperimentError(
            "remediation training must use effective batch size 16"
        )
    if training["save_steps"] % training["eval_steps"] != 0:
        raise RemediationExperimentError(
            "save_steps must be an integer multiple of eval_steps"
        )
    if training["max_updates"] > MAX_REMEDIATION_UPDATES:
        raise RemediationExperimentError(
            "remediation training exceeds the 300-update ceiling"
        )
    if training["max_train_examples"] > MAX_REMEDIATION_EXAMPLES:
        raise RemediationExperimentError(
            "remediation training exceeds the 4,800-example ceiling"
        )
    return validated


def load_remediation_experiment_config(
    path: str | Path,
) -> RemediationExperimentConfig:
    """Load one exact YAML config without allowing duplicate keys."""

    resolved, payload = _stable_regular_bytes(
        path,
        "remediation experiment config",
    )
    try:
        value = yaml.load(payload.decode("utf-8"), Loader=_UniqueKeyLoader)
    except RemediationExperimentError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise RemediationExperimentError(
            f"remediation experiment config is invalid YAML: {error}"
        ) from error
    if not isinstance(value, Mapping):
        raise RemediationExperimentError(
            "remediation experiment config must contain an object"
        )
    validated = validate_remediation_experiment_config(value)
    return RemediationExperimentConfig(
        path=resolved,
        byte_count=len(payload),
        sha256=sha256_bytes(payload),
        values=MappingProxyType(validated),
    )


def _order_rank(seed: int, epoch: int, example_id: str) -> bytes:
    return hashlib.sha256(
        f"remediation-order-v1:{seed}:{epoch}:{example_id}".encode()
    ).digest()


def _subset_rank(seed: int, phase: int, example_id: str) -> bytes:
    return hashlib.sha256(
        f"remediation-subset-v1:{seed}:{phase}:{example_id}".encode()
    ).digest()


def _dev_rank(seed: int, phase: int, example_id: str) -> bytes:
    return hashlib.sha256(
        f"remediation-parity-dev-v1:{seed}:{phase}:{example_id}".encode()
    ).digest()


def _expected_order(
    assignments: Mapping[str, int],
    *,
    mode: str,
    seed: int,
    epoch: int,
) -> list[str]:
    ranked = sorted(
        assignments,
        key=lambda example_id: (
            _order_rank(seed, epoch, example_id),
            example_id,
        ),
    )
    if mode == "shuffled":
        return ranked
    if mode == "staged":
        return [
            example_id
            for phase in PHASES
            for example_id in ranked
            if assignments[example_id] == phase
        ]
    raise RemediationExperimentError(f"unsupported remediation mode: {mode}")


def validate_remediation_order_plan(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a remediation plan and recompute every derivable order claim."""

    plan = _validate_schema(
        value,
        schema_file="remediation_order_plan_schema.json",
        label="remediation order plan",
    )
    if plan["phase_rules_version"] != PHASE_RULES_VERSION:
        raise RemediationExperimentError(
            "remediation plan phase rules differ from the runtime classifier"
        )
    selection_policy = plan.get("selection_policy")
    if selection_policy is not None and (
        selection_policy["policy_id"] != GEMMA_UNUSED_POLICY_ID
        or plan["protection_policy"]["policy_id"] != LEXICAL_KEEP_POLICY_ID
    ):
        raise RemediationExperimentError(
            "repair selection policy requires gemma_unused_v1 provenance "
            "and gemma_lexical_v1 training"
        )
    safety_filter = plan["safety_filter"]
    if (
        safety_filter["loaded_record_count"]
        != safety_filter["accepted_record_count"]
        + safety_filter["rejected_record_count"]
    ):
        raise RemediationExperimentError(
            "remediation plan protection counts do not partition the source"
        )
    if (
        safety_filter["loaded_record_count"]
        != plan["safety_source"]["record_count"]
    ):
        raise RemediationExperimentError(
            "remediation plan safety filter differs from its safety source"
        )
    if (
        plan["source"]["record_count"]
        > safety_filter["accepted_record_count"]
    ):
        raise RemediationExperimentError(
            "remediation train subset exceeds the protection-safe population"
        )
    url_ids = safety_filter["url_overlap_rejected_example_ids"]
    if (
        safety_filter["url_overlap_rejected_example_ids_sha256"]
        != hash_identifier_sequence(url_ids)
    ):
        raise RemediationExperimentError(
            "remediation plan URL-overlap rejection hash is inconsistent"
        )
    assignments_raw = plan["assignments"]
    assignment_ids = [item["example_id"] for item in assignments_raw]
    if assignment_ids != sorted(assignment_ids) or len(assignment_ids) != len(
        set(assignment_ids)
    ):
        raise RemediationExperimentError(
            "remediation plan assignments must be ID-sorted and unique"
        )
    if plan["source"]["record_count"] != len(assignment_ids):
        raise RemediationExperimentError(
            "remediation plan source count differs from its assignments"
        )
    if (
        plan["source"]["selected_safe_example_ids_sha256"]
        != hash_identifier_sequence(assignment_ids)
    ):
        raise RemediationExperimentError(
            "remediation plan selected-safe ID hash is inconsistent"
        )
    assignments = {
        item["example_id"]: int(item["phase"]) for item in assignments_raw
    }
    phase_counts = Counter(assignments.values())
    expected_phase_counts = {
        str(phase): phase_counts[phase] for phase in PHASES
    }
    if plan["phase_counts"] != expected_phase_counts:
        raise RemediationExperimentError(
            "remediation plan phase counts differ from its assignments"
        )
    orders = plan["epoch_orders"]
    if len(orders) != plan["epochs"]:
        raise RemediationExperimentError(
            "remediation plan epoch-order count differs from epochs"
        )
    expected_ids = set(assignments)
    for epoch, order in enumerate(orders):
        if order["epoch"] != epoch:
            raise RemediationExperimentError(
                "remediation plan epochs must be contiguous from zero"
            )
        identifiers = order["example_ids"]
        if len(identifiers) != len(expected_ids) or set(identifiers) != expected_ids:
            raise RemediationExperimentError(
                "remediation epoch order is not a full source permutation"
            )
        if order["example_ids_sha256"] != hash_identifier_sequence(identifiers):
            raise RemediationExperimentError(
                "remediation epoch-order hash is inconsistent"
            )
        if identifiers != _expected_order(
            assignments,
            mode=plan["mode"],
            seed=plan["seed"],
            epoch=epoch,
        ):
            raise RemediationExperimentError(
                "remediation epoch order differs from the declared algorithm"
            )
    effective_order_lock = plan.get("effective_order_lock")
    if effective_order_lock is not None:
        locked_ids = effective_order_lock["example_ids"]
        if plan["mode"] != "shuffled":
            raise RemediationExperimentError(
                "a frozen effective order is valid only for a shuffled repair arm"
            )
        if len(locked_ids) != len(expected_ids) or set(locked_ids) != expected_ids:
            raise RemediationExperimentError(
                "frozen effective order is not a full source permutation"
            )
        if effective_order_lock[
            "example_ids_sha256"
        ] != hash_identifier_sequence(locked_ids):
            raise RemediationExperimentError(
                "frozen effective-order hash is inconsistent"
            )
    training = plan["training_contract"]
    if (
        training["micro_batch_size"]
        * training["gradient_accumulation_steps"]
        != training["effective_batch_size"]
    ):
        raise RemediationExperimentError(
            "remediation plan effective batch is inconsistent"
        )
    if training["save_steps"] % training["eval_steps"] != 0:
        raise RemediationExperimentError(
            "remediation plan save_steps must be a multiple of eval_steps"
        )
    derived_update_steps = math.ceil(
        plan["source"]["record_count"]
        / training["effective_batch_size"]
    )
    if (
        training["eval_steps"] > derived_update_steps
        or training["save_steps"] > derived_update_steps
    ):
        raise RemediationExperimentError(
            "remediation plan eval/save intervals exceed its derived "
            "full-epoch update steps"
        )
    return plan


def validate_matched_remediation_plans(
    shuffled: Mapping[str, Any],
    staged: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Require two plans whose only semantic difference is their order arm."""

    control = validate_remediation_order_plan(shuffled)
    curriculum = validate_remediation_order_plan(staged)
    if control["mode"] != "shuffled" or curriculum["mode"] != "staged":
        raise RemediationExperimentError(
            "matched remediation plans require shuffled control and staged curriculum"
        )
    control_shared = _json_copy(control)
    curriculum_shared = _json_copy(curriculum)
    for item in (control_shared, curriculum_shared):
        item.pop("mode")
        item.pop("epoch_orders")
    if control_shared != curriculum_shared:
        raise RemediationExperimentError(
            "remediation plans differ outside mode and epoch order"
        )
    if (
        control["epoch_orders"][0]["example_ids"]
        == curriculum["epoch_orders"][0]["example_ids"]
    ):
        raise RemediationExperimentError(
            "remediation plans accidentally produce an identical order"
        )
    return control, curriculum


def load_remediation_order_plan(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Load canonical plan bytes and optionally bind one exact file hash."""

    _, payload = _stable_regular_bytes(path, "remediation order plan")
    actual = sha256_bytes(payload)
    if expected_sha256 is not None:
        expected = str(expected_sha256).removeprefix("sha256:")
        _require_raw_sha256(expected, "remediation plan expected SHA-256")
        if actual != expected:
            raise RemediationExperimentError(
                "remediation plan SHA-256 mismatch: "
                f"expected {expected}, got {actual}"
            )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemediationExperimentError(
            f"remediation order plan is invalid JSON: {error}"
        ) from error
    if not isinstance(value, Mapping):
        raise RemediationExperimentError(
            "remediation order plan must contain an object"
        )
    validated = validate_remediation_order_plan(value)
    if payload != canonical_json_bytes(validated):
        raise RemediationExperimentError(
            "remediation order plan is not canonical immutable JSON"
        )
    return validated, actual


def _jsonl_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for record in records
    ).encode("utf-8")


def _training_contract(training: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "max_sequence_length": training["max_sequence_length"],
        "micro_batch_size": training["micro_batch_size"],
        "gradient_accumulation_steps": training[
            "gradient_accumulation_steps"
        ],
        "effective_batch_size": training["effective_batch_size"],
        "learning_rate": training["learning_rate"],
        "optimizer": training["optimizer"],
        "scheduler": training["scheduler"],
        "eval_steps": training["eval_steps"],
        "save_steps": training["save_steps"],
        "save_total_limit": training["save_total_limit"],
        "warmup_ratio": training["warmup_ratio"],
        "weight_decay": training["weight_decay"],
        "max_grad_norm": training["max_grad_norm"],
        "bf16": training["bf16"],
        "gradient_checkpointing": training["gradient_checkpointing"],
        "assistant_output_loss_only": training[
            "assistant_output_loss_only"
        ],
        "early_stopping": training["early_stopping"],
    }


def _build_order_plan(
    *,
    mode: str,
    seed: int,
    comparison_pair_sha256: str,
    source_binding: Mapping[str, Any],
    safety_source_binding: Mapping[str, Any],
    policy_binding: Mapping[str, Any],
    parent_binding: Mapping[str, Any],
    safety_filter: Mapping[str, Any],
    training_contract: Mapping[str, Any],
    assignments: Mapping[str, int],
) -> dict[str, Any]:
    assignment_rows = [
        {"example_id": example_id, "phase": assignments[example_id]}
        for example_id in sorted(assignments)
    ]
    order = _expected_order(assignments, mode=mode, seed=seed, epoch=0)
    plan: dict[str, Any] = {
        "schema_version": 1,
        "kind": REMEDIATION_PLAN_KIND,
        "mode": mode,
        "algorithm": REMEDIATION_PLAN_ALGORITHM,
        "phase_rules_version": PHASE_RULES_VERSION,
        "seed": seed,
        "epochs": 1,
        "comparison_pair_sha256": comparison_pair_sha256,
        "source": dict(source_binding),
        "safety_source": dict(safety_source_binding),
        "protection_policy": {
            "policy_id": policy_binding["policy_id"],
            "policy_sha256": policy_binding["policy_sha256"],
            "artifact_sha256": policy_binding["artifact_sha256"],
        },
        "parent_checkpoint": {
            "candidate_id": parent_binding["candidate_id"],
            "training_report_sha256": parent_binding[
                "training_report_sha256"
            ],
            "tree_sha256": parent_binding["tree_sha256"],
            "relationship": "warm_start_parent_not_resume",
        },
        "safety_filter": {
            "loaded_record_count": safety_filter["loaded_record_count"],
            "accepted_record_count": safety_filter["accepted_record_count"],
            "rejected_record_count": safety_filter["rejected_record_count"],
            "rejected_example_ids_sha256": safety_filter[
                "rejected_example_ids_sha256"
            ],
            "url_overlap_rejected_example_ids": safety_filter[
                "url_overlap_rejected_example_ids"
            ],
            "url_overlap_rejected_example_ids_sha256": safety_filter[
                "url_overlap_rejected_example_ids_sha256"
            ],
        },
        "training_contract": dict(training_contract),
        "phase_counts": {
            str(phase): sum(value == phase for value in assignments.values())
            for phase in PHASES
        },
        "assignments": assignment_rows,
        "epoch_orders": [
            {
                "epoch": 0,
                "example_ids": order,
                "example_ids_sha256": hash_identifier_sequence(order),
            }
        ],
        "state_initialization": {
            "optimizer": "fresh",
            "scheduler": "fresh",
            "parent_as_resume": "forbidden",
            "same_run_resume": "identity_bound_recovery_only",
        },
        "holdout_inputs": False,
    }
    return validate_remediation_order_plan(plan)


def _load_policy(
    *,
    base_dir: Path,
    inputs: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    path = _resolve_from(
        base_dir,
        inputs["protection_policy_path"],
        "protection policy path",
    )
    resolved, payload = _stable_regular_bytes(path, "protection policy")
    expected = _require_raw_sha256(
        inputs["protection_policy_artifact_sha256"],
        "protection policy artifact SHA-256",
    )
    actual = sha256_bytes(payload)
    if actual != expected:
        raise RemediationExperimentError(
            "protection policy artifact SHA-256 mismatch: "
            f"expected {expected}, got {actual}"
        )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemediationExperimentError(
            f"protection policy is invalid JSON: {error}"
        ) from error
    if not isinstance(value, Mapping):
        raise RemediationExperimentError(
            "protection policy must contain an object"
        )
    try:
        policy = validate_protection_policy_evidence(value)
    except ValueError as error:
        raise RemediationExperimentError(
            f"protection policy is invalid: {error}"
        ) from error
    if policy["policy_id"] != GEMMA_UNUSED_POLICY_ID:
        raise RemediationExperimentError(
            "remediation requires gemma_unused_v1 protection"
        )
    if payload != canonical_json_bytes(policy):
        raise RemediationExperimentError(
            "protection policy artifact is not canonical immutable JSON"
        )
    binding = {
        "filename": resolved.name,
        "artifact_sha256": actual,
        "policy_id": policy["policy_id"],
        "policy_sha256": policy["policy_sha256"],
    }
    return policy, binding, resolved


def _require_checkpoint_shape(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RemediationExperimentError(
            f"remediation parent must be a model directory: {path}"
        )
    required = {"config.json", "tokenizer_config.json"}
    files = [item for item in path.rglob("*") if item.is_file()]
    relative = {item.relative_to(path).as_posix() for item in files}
    if not required <= relative or not any(
        item.endswith(".safetensors") for item in relative
    ):
        raise RemediationExperimentError(
            "remediation parent lacks model or tokenizer artifacts"
        )


def _verify_selector_model_tree(
    checkpoint_path: Path,
    model_tree: Mapping[str, Any],
) -> None:
    inventory_path = checkpoint_path / "checkpoint-inventory.json"
    _, payload = _stable_regular_bytes(
        inventory_path,
        "remediation parent checkpoint inventory",
    )
    if (
        "sha256:" + sha256_bytes(payload)
        != model_tree["inventory_sha256"]
    ):
        raise RemediationExperimentError(
            "remediation parent inventory differs from the selector binding"
        )
    try:
        inventory = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemediationExperimentError(
            f"remediation parent inventory is invalid JSON: {error}"
        ) from error
    if not isinstance(inventory, Mapping):
        raise RemediationExperimentError(
            "remediation parent inventory must contain an object"
        )
    files = inventory.get("files")
    if not isinstance(files, list) or not files:
        raise RemediationExperimentError(
            "remediation parent inventory has no files"
        )
    prior = ""
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(files):
        if not isinstance(raw, Mapping):
            raise RemediationExperimentError(
                f"remediation inventory entry {index} is not an object"
            )
        relative = raw.get("path")
        byte_count = raw.get("bytes")
        digest = raw.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or relative <= prior
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count <= 0
        ):
            raise RemediationExperimentError(
                "remediation parent inventory has an unsafe or unsorted entry"
            )
        prior = relative
        expected_digest = _require_prefixed_sha256(
            digest,
            f"remediation parent inventory hash for {relative}",
        )
        physical, physical_payload = _stable_regular_bytes(
            checkpoint_path / Path(*Path(relative).parts),
            f"remediation parent model file {relative}",
        )
        try:
            physical.relative_to(checkpoint_path)
        except ValueError as error:
            raise RemediationExperimentError(
                "remediation parent inventory escapes its model tree"
            ) from error
        if (
            len(physical_payload) != byte_count
            or "sha256:" + sha256_bytes(physical_payload)
            != expected_digest
        ):
            raise RemediationExperimentError(
                f"remediation parent file differs from inventory: {relative}"
            )
        normalized.append(
            {
                "path": relative,
                "bytes": byte_count,
                "sha256": expected_digest,
            }
        )
    digest = hashlib.sha256()
    for item in normalized:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    tree_sha256 = "sha256:" + digest.hexdigest()
    if (
        tree_sha256 != model_tree["tree_sha256"]
        or inventory.get("tree_sha256") != tree_sha256
        or inventory.get("file_count") != model_tree["file_count"]
        or inventory.get("file_count") != len(normalized)
        or inventory.get("total_bytes") != model_tree["total_bytes"]
        or inventory.get("total_bytes")
        != sum(int(item["bytes"]) for item in normalized)
    ):
        raise RemediationExperimentError(
            "remediation parent model tree differs from selector evidence"
        )
    actual_files = {
        item.relative_to(checkpoint_path).as_posix()
        for item in checkpoint_path.rglob("*")
        if item.is_file()
    }
    expected_files = {
        *(str(item["path"]) for item in normalized),
        "checkpoint-inventory.json",
    }
    if actual_files != expected_files:
        raise RemediationExperimentError(
            "remediation parent contains unbound or missing model files"
        )


def _load_selection_report_parent(
    *,
    base_dir: Path,
    parent_config: Mapping[str, Any],
) -> tuple[dict[str, Any], Path]:
    report_path = _resolve_from(
        base_dir,
        parent_config["report_path"],
        "pilot selection report path",
    )
    _, payload = _stable_regular_bytes(
        report_path,
        "pilot selection report",
    )
    expected = _require_raw_sha256(
        parent_config["report_sha256"],
        "pilot selection report file SHA-256",
    )
    actual = sha256_bytes(payload)
    if actual != expected:
        raise RemediationExperimentError(
            "pilot selection report file SHA-256 mismatch: "
            f"expected {expected}, got {actual}"
        )
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemediationExperimentError(
            f"pilot selection report is invalid JSON: {error}"
        ) from error
    if not isinstance(raw, Mapping):
        raise RemediationExperimentError(
            "pilot selection report must contain an object"
        )
    try:
        from .pilot_selection import (
            canonical_json_bytes as pilot_selection_json_bytes,
        )
        from .pilot_selection import validate_pilot_selection_report

        report = validate_pilot_selection_report(raw)
    except (ImportError, ValueError) as error:
        raise RemediationExperimentError(
            f"pilot selection report validation failed: {error}"
        ) from error
    if payload != pilot_selection_json_bytes(report):
        raise RemediationExperimentError(
            "pilot selection report is not canonical immutable JSON"
        )
    remediation = report.get("remediation_parent")
    evidence = report.get("remediation_evidence")
    if not isinstance(remediation, Mapping):
        raise RemediationExperimentError(
            "pilot selection report has no eligible remediation_parent"
        )
    if (
        report.get("status") != "no_release_winner"
        or report.get("release_safety_assertion") is not False
        or not isinstance(evidence, Mapping)
        or evidence.get("status") != "shared_external_failure_proven"
        or evidence.get("assertion")
        != (
            "This evidence may justify only a remediation parent; it never "
            "changes failed hard gates or release eligibility."
        )
    ):
        raise RemediationExperimentError(
            "pilot selection report does not prove a shared external failure"
        )
    model_tree = remediation.get("model_tree")
    if not isinstance(model_tree, Mapping):
        raise RemediationExperimentError(
            "remediation parent model_tree binding is missing"
        )
    checkpoint_path = Path(str(model_tree["path"])).resolve()
    _require_checkpoint_shape(checkpoint_path)
    _verify_selector_model_tree(checkpoint_path, model_tree)
    if checkpoint_path.name != model_tree["directory"]:
        raise RemediationExperimentError(
            "remediation parent directory differs from selector binding"
        )
    runtime_tree = checkpoint_tree_sha256(checkpoint_path)
    parent = {
        "source": "pilot_selection_report",
        "selection_report_artifact_sha256": actual,
        "candidate_id": _require_identifier(
            remediation["candidate_id"],
            "remediation parent candidate_id",
        ),
        "learning_rate": remediation["learning_rate"],
        "checkpoint_directory": checkpoint_path.name,
        "tree_sha256": _require_prefixed_sha256(
            runtime_tree,
            "remediation parent warm-start tree SHA-256",
        ),
        "selector_model_tree_sha256": _require_prefixed_sha256(
            model_tree["tree_sha256"],
            "selector model-tree SHA-256",
        ),
        "selector_inventory_sha256": _require_prefixed_sha256(
            model_tree["inventory_sha256"],
            "selector inventory SHA-256",
        ),
        "training_report_sha256": _require_prefixed_sha256(
            remediation["training_report_sha256"],
            "remediation parent training-report SHA-256",
        ),
        "external_failure_signature_sha256": _require_prefixed_sha256(
            remediation["external_failure_signature_sha256"],
            "remediation external-failure signature",
        ),
        "scope": remediation["scope"],
        "selection_basis": remediation["selection_basis"],
        "release_eligible": remediation["release_eligible"],
        "relationship": "warm_start_parent_not_resume",
    }
    if parent["scope"] != "remediation_only_not_release":
        raise RemediationExperimentError(
            "selection report parent is not remediation-only"
        )
    if parent["selection_basis"] != "composite_then_loss_tie_breaks":
        raise RemediationExperimentError(
            "selection report parent basis is not frozen"
        )
    if parent["release_eligible"] is not False:
        raise RemediationExperimentError(
            "remediation parent must not be release eligible"
        )
    return parent, checkpoint_path


def _load_explicit_parent(
    *,
    base_dir: Path,
    parent_config: Mapping[str, Any],
) -> tuple[dict[str, Any], Path]:
    checkpoint_path = _resolve_from(
        base_dir,
        parent_config["checkpoint_path"],
        "explicit remediation parent checkpoint path",
    )
    _require_checkpoint_shape(checkpoint_path)
    expected_tree = _require_prefixed_sha256(
        parent_config["checkpoint_tree_sha256"],
        "explicit remediation parent tree SHA-256",
    )
    actual_tree = checkpoint_tree_sha256(checkpoint_path)
    if actual_tree != expected_tree:
        raise RemediationExperimentError(
            "explicit remediation parent tree SHA-256 mismatch: "
            f"expected {expected_tree}, got {actual_tree}"
        )
    return {
        "source": "explicit_remediation_parent",
        "selection_report_artifact_sha256": None,
        "candidate_id": _require_identifier(
            parent_config["candidate_id"],
            "explicit remediation parent candidate_id",
        ),
        "learning_rate": parent_config["learning_rate"],
        "checkpoint_directory": checkpoint_path.name,
        "tree_sha256": actual_tree,
        "selector_model_tree_sha256": None,
        "selector_inventory_sha256": None,
        "training_report_sha256": _require_prefixed_sha256(
            parent_config["training_report_sha256"],
            "explicit parent training-report SHA-256",
        ),
        "external_failure_signature_sha256": _require_prefixed_sha256(
            parent_config["external_failure_signature_sha256"],
            "explicit parent failure signature",
        ),
        "scope": parent_config["scope"],
        "selection_basis": parent_config["selection_basis"],
        "release_eligible": parent_config["release_eligible"],
        "relationship": "warm_start_parent_not_resume",
    }, checkpoint_path


def _load_parent(
    *,
    base_dir: Path,
    inputs: Mapping[str, Any],
) -> tuple[dict[str, Any], Path]:
    parent_config = inputs["parent"]
    mode = parent_config["mode"]
    if mode == "pilot_selection_report":
        return _load_selection_report_parent(
            base_dir=base_dir,
            parent_config=parent_config,
        )
    if mode == "explicit_remediation_parent":
        return _load_explicit_parent(
            base_dir=base_dir,
            parent_config=parent_config,
        )
    raise RemediationExperimentError(
        f"unsupported remediation parent mode: {mode}"
    )


def _load_pilot_sources(
    *,
    base_dir: Path,
    inputs: Mapping[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    manifest_path = _resolve_from(
        base_dir,
        inputs["pilot_manifest_path"],
        "Pilot manifest path",
    )
    manifest_resolved, manifest_payload = _stable_regular_bytes(
        manifest_path,
        "Pilot manifest",
    )
    manifest_expected = _require_raw_sha256(
        inputs["pilot_manifest_sha256"],
        "Pilot manifest file SHA-256",
    )
    manifest_actual = sha256_bytes(manifest_payload)
    if manifest_actual != manifest_expected:
        raise RemediationExperimentError(
            "Pilot manifest file SHA-256 mismatch: "
            f"expected {manifest_expected}, got {manifest_actual}"
        )
    try:
        manifest = json.loads(manifest_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemediationExperimentError(
            f"Pilot manifest is invalid JSON: {error}"
        ) from error
    if not isinstance(manifest, Mapping):
        raise RemediationExperimentError(
            "Pilot manifest must contain an object"
        )
    if (
        manifest.get("schema_version") != 1
        or manifest.get("synthetic_only") is not True
        or manifest.get("human_curation", False) is not False
        or manifest.get("generative_api", False) is not False
        or manifest.get("split_leakage") is not False
    ):
        raise RemediationExperimentError(
            "Pilot manifest lacks the closed AI-only non-leaking declarations"
        )
    split_hashes = manifest.get("split_sha256")
    split_counts = manifest.get("split_counts")
    if not isinstance(split_hashes, Mapping) or not isinstance(
        split_counts, Mapping
    ):
        raise RemediationExperimentError(
            "Pilot manifest lacks split hashes or counts"
        )
    train_expected = _require_raw_sha256(
        inputs["train_sha256"],
        "Pilot train SHA-256",
    )
    validation_expected = _require_raw_sha256(
        inputs["validation_sha256"],
        "Pilot validation SHA-256",
    )
    if (
        split_hashes.get("train") != train_expected
        or split_hashes.get("validation") != validation_expected
    ):
        raise RemediationExperimentError(
            "configured Pilot train/validation hashes differ from the manifest"
        )
    train_path = _resolve_from(
        base_dir,
        inputs["train_path"],
        "Pilot train path",
    )
    validation_path = _resolve_from(
        base_dir,
        inputs["validation_path"],
        "Pilot validation path",
    )
    try:
        train, train_binding = load_hash_bound_jsonl(
            train_path,
            expected_sha256=train_expected,
            expected_split="train",
            label="Pilot train",
        )
        validation, validation_binding = load_hash_bound_jsonl(
            validation_path,
            expected_sha256=validation_expected,
            expected_split="validation",
            label="Pilot validation",
        )
    except ValueError as error:
        raise RemediationExperimentError(str(error)) from error
    if (
        split_counts.get("train") != len(train)
        or split_counts.get("validation") != len(validation)
    ):
        raise RemediationExperimentError(
            "Pilot source counts differ from the manifest"
        )
    train_ids = {str(record["example_id"]) for record in train}
    validation_ids = {str(record["example_id"]) for record in validation}
    overlap = train_ids & validation_ids
    if overlap:
        raise RemediationExperimentError(
            "Pilot train and validation overlap: " + sorted(overlap)[0]
        )
    manifest_binding = {
        "filename": manifest_resolved.name,
        "sha256": manifest_actual,
    }
    return (
        train,
        validation,
        manifest_binding,
        train_binding,
        validation_binding,
    )


def _filter_binding(
    protected: ProtectedTrainingRecords,
) -> dict[str, Any]:
    evidence = protected.evidence
    by_reason = evidence.get("rejection_example_ids_by_reason")
    hashes_by_reason = evidence.get(
        "rejection_example_ids_sha256_by_reason"
    )
    if not isinstance(by_reason, Mapping) or not isinstance(
        hashes_by_reason, Mapping
    ):
        raise RemediationExperimentError(
            "training protection evidence lacks per-reason rejection IDs/hashes"
        )
    url_ids_raw = by_reason.get("terminal_url_punctuation_overlap", [])
    if not isinstance(url_ids_raw, list) or not all(
        isinstance(item, str) for item in url_ids_raw
    ):
        raise RemediationExperimentError(
            "URL-overlap rejection IDs are malformed"
        )
    url_ids = list(url_ids_raw)
    url_hash = hashes_by_reason.get(
        "terminal_url_punctuation_overlap",
        hash_identifier_sequence([]),
    )
    if url_hash != hash_identifier_sequence(url_ids):
        raise RemediationExperimentError(
            "URL-overlap rejection evidence hash is inconsistent"
        )
    rejection_counts = evidence.get("rejection_reason_counts")
    if not isinstance(rejection_counts, Mapping):
        raise RemediationExperimentError(
            "training protection rejection counts are missing"
        )
    if rejection_counts.get("terminal_url_punctuation_overlap", 0) != len(
        url_ids
    ):
        raise RemediationExperimentError(
            "URL-overlap rejection IDs differ from reason counts"
        )
    return {
        "loaded_record_count": evidence["loaded_record_count"],
        "accepted_record_count": evidence["accepted_record_count"],
        "accepted_example_ids_sha256": evidence[
            "accepted_example_ids_sha256"
        ],
        "rejected_record_count": evidence["rejected_record_count"],
        "rejected_example_ids_sha256": evidence[
            "rejected_example_ids_sha256"
        ],
        "rejection_reason_counts": dict(rejection_counts),
        "url_overlap_rejected_example_ids": url_ids,
        "url_overlap_rejected_example_ids_sha256": url_hash,
        "transformed_pairs_sha256": evidence[
            "transformed_pairs_sha256"
        ],
    }


def _phase_groups(
    records: Sequence[Mapping[str, Any]],
) -> dict[int, list[Mapping[str, Any]]]:
    groups: dict[int, list[Mapping[str, Any]]] = {
        phase: [] for phase in PHASES
    }
    for record in records:
        phase = classify_curriculum_phase(
            record,
            ai_gold_ids=frozenset(),
        )
        groups[phase].append(record)
    return groups


def _select_balanced_training_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    training: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int], int, int]:
    groups = _phase_groups(records)
    missing = [
        phase for phase in PHASES if not groups[phase]
    ]
    if missing:
        raise RemediationExperimentError(
            "protection-safe Pilot train lacks curriculum phase(s): "
            + ", ".join(map(str, missing))
        )
    capacity = min(
        int(training["max_train_examples"]),
        int(training["max_updates"])
        * int(training["effective_batch_size"]),
        len(records),
    )
    per_phase = min(
        capacity // len(PHASES),
        *(len(groups[phase]) for phase in PHASES),
    )
    if per_phase <= 0:
        raise RemediationExperimentError(
            "training budget cannot provide one safe example per phase"
        )
    selected: list[Mapping[str, Any]] = []
    seed = int(training["seed"])
    for phase in PHASES:
        ranked = sorted(
            groups[phase],
            key=lambda record: (
                _subset_rank(
                    seed,
                    phase,
                    str(record["example_id"]),
                ),
                str(record["example_id"]),
            ),
        )
        selected.extend(ranked[:per_phase])
    materialized = sorted(
        (dict(record) for record in selected),
        key=lambda record: str(record["example_id"]),
    )
    selected_ids = [str(record["example_id"]) for record in materialized]
    if len(selected_ids) != len(set(selected_ids)):
        raise RemediationExperimentError(
            "balanced training selection contains duplicate IDs"
        )
    update_steps = math.ceil(
        len(materialized) / int(training["effective_batch_size"])
    )
    if update_steps > int(training["max_updates"]):
        raise RemediationExperimentError(
            "balanced remediation subset exceeds the update budget"
        )
    counts = {
        str(phase): sum(
            classify_curriculum_phase(
                record,
                ai_gold_ids=frozenset(),
            )
            == phase
            for record in materialized
        )
        for phase in PHASES
    }
    if len(set(counts.values())) != 1:
        raise RemediationExperimentError(
            "balanced training selection is not phase-equal"
        )
    return materialized, counts, capacity, update_steps


def _select_parity_dev(
    records: Sequence[Mapping[str, Any]],
    *,
    source_binding: Mapping[str, Any],
    filter_binding: Mapping[str, Any],
    policy_sha256: str,
    seed: int,
    per_phase: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    groups = _phase_groups(records)
    selected: list[Mapping[str, Any]] = []
    for phase in PHASES:
        ranked = sorted(
            groups[phase],
            key=lambda record: (
                _dev_rank(
                    seed,
                    phase,
                    str(record["example_id"]),
                ),
                str(record["example_id"]),
            ),
        )
        if len(ranked) < per_phase:
            raise RemediationExperimentError(
                f"insufficient safe validation phase {phase}: "
                f"required {per_phase}, found {len(ranked)}"
            )
        selected.extend(ranked[:per_phase])
    selected_ids = sorted(str(record["example_id"]) for record in selected)
    retagged: list[dict[str, Any]] = []
    for record in selected:
        phase = classify_curriculum_phase(
            record,
            ai_gold_ids=frozenset(),
        )
        materialized = dict(record)
        materialized["split"] = "regression"
        risk_tags = list(materialized.get("risk_tags", ()))
        for tag in (
            "remediation_parity_dev",
            f"remediation_phase_{phase}",
        ):
            if tag not in risk_tags:
                risk_tags.append(tag)
        materialized["risk_tags"] = risk_tags
        retagged.append(materialized)
    try:
        references = build_evaluation_references(
            retagged,
            output_split="regression",
        )
    except ValueError as error:
        raise RemediationExperimentError(
            f"cannot build validation-only parity references: {error}"
        ) from error
    reference_payload = _jsonl_bytes(references)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_remediation_parity_dev",
        "algorithm": REMEDIATION_DEV_ALGORITHM,
        "phase_rules_version": PHASE_RULES_VERSION,
        "seed": seed,
        "per_phase": per_phase,
        "source": dict(source_binding),
        "protection_filter": {
            "policy_id": GEMMA_UNUSED_POLICY_ID,
            "policy_sha256": policy_sha256,
            "accepted_record_count": filter_binding[
                "accepted_record_count"
            ],
            "rejected_record_count": filter_binding[
                "rejected_record_count"
            ],
            "rejected_example_ids_sha256": filter_binding[
                "rejected_example_ids_sha256"
            ],
            "url_overlap_rejected_example_ids": filter_binding[
                "url_overlap_rejected_example_ids"
            ],
            "url_overlap_rejected_example_ids_sha256": filter_binding[
                "url_overlap_rejected_example_ids_sha256"
            ],
        },
        "phase_counts": {
            str(phase): per_phase for phase in PHASES
        },
        "selected_example_ids": selected_ids,
        "selected_example_ids_sha256": hash_identifier_sequence(
            selected_ids
        ),
        "references": {
            "filename": "parity-dev.references.jsonl",
            "sha256": sha256_bytes(reference_payload),
            "record_count": len(references),
            "split": "regression",
        },
        "parent_split": "validation",
        "holdout_inputs": False,
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
    }
    return references, validate_remediation_parity_dev_manifest(manifest)


def validate_remediation_parity_dev_manifest(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate validation-only balanced reference metadata."""

    manifest = _validate_schema(
        value,
        schema_file="remediation_parity_dev_manifest_schema.json",
        label="remediation parity-dev manifest",
    )
    if manifest["phase_rules_version"] != PHASE_RULES_VERSION:
        raise RemediationExperimentError(
            "parity-dev phase rules differ from the runtime classifier"
        )
    identifiers = manifest["selected_example_ids"]
    if identifiers != sorted(identifiers):
        raise RemediationExperimentError(
            "parity-dev selected IDs must be sorted"
        )
    if (
        manifest["selected_example_ids_sha256"]
        != hash_identifier_sequence(identifiers)
    ):
        raise RemediationExperimentError(
            "parity-dev selected ID hash is inconsistent"
        )
    expected_count = manifest["per_phase"] * len(PHASES)
    if (
        len(identifiers) != expected_count
        or manifest["references"]["record_count"] != expected_count
    ):
        raise RemediationExperimentError(
            "parity-dev counts differ from the phase quota"
        )
    expected_phases = {
        str(phase): manifest["per_phase"] for phase in PHASES
    }
    if manifest["phase_counts"] != expected_phases:
        raise RemediationExperimentError(
            "parity-dev is not exactly phase-balanced"
        )
    url_ids = manifest["protection_filter"][
        "url_overlap_rejected_example_ids"
    ]
    if (
        manifest["protection_filter"][
            "url_overlap_rejected_example_ids_sha256"
        ]
        != hash_identifier_sequence(url_ids)
    ):
        raise RemediationExperimentError(
            "parity-dev URL-overlap rejection hash is inconsistent"
        )
    return manifest


def _load_adoption_policy(
    *,
    base_dir: Path,
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    path = _resolve_from(
        base_dir,
        config["comparison_config_path"],
        "curriculum comparison config path",
    )
    resolved, payload = _stable_regular_bytes(
        path,
        "curriculum comparison config",
    )
    expected = _require_raw_sha256(
        config["comparison_config_sha256"],
        "curriculum comparison config SHA-256",
    )
    actual = sha256_bytes(payload)
    if actual != expected:
        raise RemediationExperimentError(
            "curriculum comparison config SHA-256 mismatch: "
            f"expected {expected}, got {actual}"
        )
    try:
        policy = load_curriculum_comparison_config(resolved)
    except ValueError as error:
        raise RemediationExperimentError(
            f"curriculum comparison config is invalid: {error}"
        ) from error
    return (
        policy,
        {
            "filename": "comparison-policy.yaml",
            "sha256": actual,
        },
        payload,
    )


def _training_config_value(
    *,
    mode: str,
    plan_filename: str,
    plan_sha256: str,
    selected_count: int,
    validation_count: int,
    training: Mapping[str, Any],
    policy_path: Path,
    policy_artifact_sha256: str,
    parent_path: Path,
    parent_tree_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_kind": "improvement",
        "base_model": BASE_MODEL_ID,
        "base_revision": BASE_MODEL_REVISION,
        "method": "full_sft",
        "seed": training["seed"],
        "epochs": 1,
        "train_examples": selected_count,
        "validation_examples": validation_count,
        "max_sequence_length": training["max_sequence_length"],
        "micro_batch_size": training["micro_batch_size"],
        "gradient_accumulation_steps": training[
            "gradient_accumulation_steps"
        ],
        "learning_rate": training["learning_rate"],
        "optimizer": training["optimizer"],
        "eval_steps": training["eval_steps"],
        "save_steps": training["save_steps"],
        "save_total_limit": training["save_total_limit"],
        "warmup_ratio": training["warmup_ratio"],
        "weight_decay": training["weight_decay"],
        "max_grad_norm": training["max_grad_norm"],
        "bf16": training["bf16"],
        "gradient_checkpointing": training[
            "gradient_checkpointing"
        ],
        "assistant_output_loss_only": training[
            "assistant_output_loss_only"
        ],
        "early_stopping": False,
        "training_order_mode": mode,
        "training_order_plan_path": plan_filename,
        "training_order_plan_sha256": plan_sha256,
        "protection_policy_path": str(policy_path),
        "protection_policy_sha256": policy_artifact_sha256,
        "parent_checkpoint_path": str(parent_path),
        "parent_checkpoint_tree_sha256": parent_tree_sha256,
    }


def _yaml_bytes(value: Mapping[str, Any]) -> bytes:
    return yaml.safe_dump(
        dict(value),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).encode("utf-8")


def _dataset_manifest(
    *,
    train_sha256: str,
    train_count: int,
    validation_sha256: str,
    validation_count: int,
    pilot_manifest_sha256: str,
    policy_binding: Mapping[str, Any],
    parent_binding: Mapping[str, Any],
    selected_ids_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "scriber_remediation_training_dataset",
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "split_leakage": False,
        "holdout_inputs": False,
        "source_pilot_manifest_sha256": pilot_manifest_sha256,
        "protection_policy": {
            "policy_id": policy_binding["policy_id"],
            "policy_sha256": policy_binding["policy_sha256"],
            "artifact_sha256": policy_binding["artifact_sha256"],
        },
        "parent_checkpoint": {
            "candidate_id": parent_binding["candidate_id"],
            "tree_sha256": parent_binding["tree_sha256"],
            "relationship": "warm_start_parent_not_resume",
        },
        "selected_train_example_ids_sha256": selected_ids_sha256,
        "split_counts": {
            "train": train_count,
            "validation": validation_count,
        },
        "split_sha256": {
            "train": train_sha256,
            "validation": validation_sha256,
        },
    }


def validate_remediation_experiment_manifest(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the final bundle and recompute its matched-arm assertions."""

    manifest = _validate_schema(
        value,
        schema_file="remediation_experiment_manifest_schema.json",
        label="remediation experiment manifest",
    )
    selection = manifest["selection"]
    if (
        selection["selected_examples"]
        != selection["per_phase"] * len(PHASES)
        or selection["train"]["record_count"]
        != selection["selected_examples"]
    ):
        raise RemediationExperimentError(
            "remediation selection counts are inconsistent"
        )
    if len(set(selection["phase_counts"].values())) != 1:
        raise RemediationExperimentError(
            "remediation training selection is not phase-balanced"
        )
    schedule = manifest["schedule"]
    expected_updates = math.ceil(
        selection["selected_examples"]
        / schedule["effective_batch_size"]
    )
    if (
        schedule["update_steps"] != expected_updates
        or schedule["update_steps"] > schedule["maximum_updates"]
    ):
        raise RemediationExperimentError(
            "remediation schedule differs from the selected example count"
        )
    training_contract = manifest["training_contract"]
    if (
        training_contract["eval_steps"] > expected_updates
        or training_contract["save_steps"] > expected_updates
    ):
        raise RemediationExperimentError(
            "remediation manifest eval/save intervals exceed its derived "
            "full-epoch update steps"
        )
    arms = manifest["arms"]
    if (
        arms["shuffled"]["mode"] != "shuffled"
        or arms["staged"]["mode"] != "staged"
    ):
        raise RemediationExperimentError(
            "remediation arm labels and modes are inconsistent"
        )
    if (
        arms["shuffled"]["comparison_pair_sha256"]
        != manifest["comparison_pair_sha256"]
        or arms["staged"]["comparison_pair_sha256"]
        != manifest["comparison_pair_sha256"]
    ):
        raise RemediationExperimentError(
            "remediation arms differ from the comparison-pair binding"
        )
    if (
        arms["shuffled"]["planned_run_input_sha256"]
        == arms["staged"]["planned_run_input_sha256"]
    ):
        raise RemediationExperimentError(
            "remediation arm run-input identities must be distinct"
        )
    weights = manifest["adoption_policy"]["weights"]
    if not math.isclose(
        sum(float(value) for value in weights.values()),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise RemediationExperimentError(
            "remediation adoption weights must sum to one"
        )
    for split in ("train", "validation"):
        filter_value = manifest["safety_filters"][split]
        if (
            filter_value["loaded_record_count"]
            != filter_value["accepted_record_count"]
            + filter_value["rejected_record_count"]
        ):
            raise RemediationExperimentError(
                f"remediation {split} filter counts do not partition the input"
            )
        if (
            filter_value["url_overlap_rejected_example_ids_sha256"]
            != hash_identifier_sequence(
                filter_value["url_overlap_rejected_example_ids"]
            )
        ):
            raise RemediationExperimentError(
                f"remediation {split} URL rejection hash is inconsistent"
            )
    return manifest


def _write_file(path: Path, payload: bytes) -> None:
    if path.exists():
        raise RemediationExperimentError(
            f"refusing to overwrite remediation artifact: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _artifact(filename: str, payload: bytes) -> dict[str, Any]:
    return {"filename": filename, "sha256": sha256_bytes(payload)}


def build_remediation_experiment(
    config_path: str | Path,
    *,
    output_dir: str | Path,
) -> RemediationExperimentBundle:
    """Materialize one static, matched, validation-only remediation A/B bundle."""

    config_snapshot = load_remediation_experiment_config(config_path)
    config = config_snapshot.values
    base_dir = config_snapshot.path.parent
    inputs = config["inputs"]
    (
        train_records,
        validation_records,
        pilot_manifest_binding,
        pilot_train_binding,
        pilot_validation_binding,
    ) = _load_pilot_sources(base_dir=base_dir, inputs=inputs)
    policy, policy_binding, policy_path = _load_policy(
        base_dir=base_dir,
        inputs=inputs,
    )
    parent_binding, parent_path = _load_parent(
        base_dir=base_dir,
        inputs=inputs,
    )
    try:
        train_protected = apply_training_protection(
            train_records,
            policy=policy,
        )
        validation_protected = apply_training_protection(
            validation_records,
            policy=policy,
        )
    except ValueError as error:
        raise RemediationExperimentError(
            f"cannot apply remediation protection policy: {error}"
        ) from error
    train_filter = _filter_binding(train_protected)
    validation_filter = _filter_binding(validation_protected)
    original_train_by_id = {
        str(record["example_id"]): record for record in train_records
    }
    original_validation_by_id = {
        str(record["example_id"]): record for record in validation_records
    }
    safe_train = [
        original_train_by_id[example_id]
        for example_id in train_protected.accepted_example_ids
    ]
    safe_validation = [
        original_validation_by_id[example_id]
        for example_id in validation_protected.accepted_example_ids
    ]
    selected_train, selected_phase_counts, capacity, update_steps = (
        _select_balanced_training_rows(
            safe_train,
            training=config["training"],
        )
    )
    selected_train_ids = [
        str(record["example_id"]) for record in selected_train
    ]
    selected_ids_sha256 = hash_identifier_sequence(selected_train_ids)
    pilot_train_safety_payload = _jsonl_bytes(train_records)
    if (
        sha256_bytes(pilot_train_safety_payload)
        != pilot_train_binding["sha256"]
    ):
        raise RemediationExperimentError(
            "Pilot train safety source is not canonical JSONL"
        )
    safe_validation_sorted = sorted(
        (dict(record) for record in safe_validation),
        key=lambda record: str(record["example_id"]),
    )
    selected_train_payload = _jsonl_bytes(selected_train)
    safe_validation_payload = _jsonl_bytes(safe_validation_sorted)
    selected_train_sha256 = sha256_bytes(selected_train_payload)
    safe_validation_sha256 = sha256_bytes(safe_validation_payload)
    parity_source_binding = {
        "filename": pilot_validation_binding["filename"],
        "sha256": pilot_validation_binding["sha256"],
        "record_count": pilot_validation_binding["record_count"],
        "split": "validation",
    }
    parity_references, parity_manifest = _select_parity_dev(
        safe_validation,
        source_binding=parity_source_binding,
        filter_binding=validation_filter,
        policy_sha256=policy_binding["policy_sha256"],
        seed=int(config["parity_dev"]["seed"]),
        per_phase=int(config["parity_dev"]["per_phase"]),
    )
    parity_references_payload = _jsonl_bytes(parity_references)
    parity_manifest_payload = canonical_json_bytes(parity_manifest)
    (
        comparison_policy,
        comparison_policy_binding,
        comparison_policy_payload,
    ) = _load_adoption_policy(
        base_dir=base_dir,
        config=config["adoption_policy"],
    )
    training_contract = _training_contract(config["training"])
    comparison_pair_value = {
        "schema_version": 1,
        "experiment_id": config["experiment_id"],
        "pilot_manifest_sha256": pilot_manifest_binding["sha256"],
        "selected_train_sha256": selected_train_sha256,
        "selected_train_example_ids_sha256": selected_ids_sha256,
        "safe_validation_sha256": safe_validation_sha256,
        "parity_dev_example_ids_sha256": parity_manifest[
            "selected_example_ids_sha256"
        ],
        "protection_policy": {
            "policy_id": policy_binding["policy_id"],
            "policy_sha256": policy_binding["policy_sha256"],
            "artifact_sha256": policy_binding["artifact_sha256"],
        },
        "parent_checkpoint": {
            "candidate_id": parent_binding["candidate_id"],
            "tree_sha256": parent_binding["tree_sha256"],
            "training_report_sha256": parent_binding[
                "training_report_sha256"
            ],
            "relationship": "warm_start_parent_not_resume",
        },
        "training_contract": training_contract,
        "epochs": 1,
        "update_steps": update_steps,
        "adoption_policy_sha256": comparison_policy_binding["sha256"],
    }
    comparison_pair_sha256 = sha256_bytes(
        canonical_json_bytes(comparison_pair_value)
    )
    source_binding = {
        "filename": "selected-train.jsonl",
        "sha256": selected_train_sha256,
        "record_count": len(selected_train),
        "split": "train",
        "selected_safe_example_ids_sha256": selected_ids_sha256,
    }
    safety_source_binding = {
        "filename": "pilot-train.safety-source.jsonl",
        "sha256": pilot_train_binding["sha256"],
        "record_count": pilot_train_binding["record_count"],
        "split": "train",
    }
    assignments = {
        str(record["example_id"]): classify_curriculum_phase(
            record,
            ai_gold_ids=frozenset(),
        )
        for record in selected_train
    }
    shuffled_plan = _build_order_plan(
        mode="shuffled",
        seed=int(config["training"]["seed"]),
        comparison_pair_sha256=comparison_pair_sha256,
        source_binding=source_binding,
        safety_source_binding=safety_source_binding,
        policy_binding=policy_binding,
        parent_binding=parent_binding,
        safety_filter=train_filter,
        training_contract=training_contract,
        assignments=assignments,
    )
    staged_plan = _build_order_plan(
        mode="staged",
        seed=int(config["training"]["seed"]),
        comparison_pair_sha256=comparison_pair_sha256,
        source_binding=source_binding,
        safety_source_binding=safety_source_binding,
        policy_binding=policy_binding,
        parent_binding=parent_binding,
        safety_filter=train_filter,
        training_contract=training_contract,
        assignments=assignments,
    )
    validate_matched_remediation_plans(shuffled_plan, staged_plan)
    shuffled_plan_payload = canonical_json_bytes(shuffled_plan)
    staged_plan_payload = canonical_json_bytes(staged_plan)
    shuffled_plan_sha256 = sha256_bytes(shuffled_plan_payload)
    staged_plan_sha256 = sha256_bytes(staged_plan_payload)
    shuffled_config = _training_config_value(
        mode="shuffled",
        plan_filename="shuffled.order-plan.json",
        plan_sha256=shuffled_plan_sha256,
        selected_count=len(selected_train),
        validation_count=len(safe_validation_sorted),
        training=config["training"],
        policy_path=policy_path,
        policy_artifact_sha256=policy_binding["artifact_sha256"],
        parent_path=parent_path,
        parent_tree_sha256=parent_binding["tree_sha256"],
    )
    staged_config = _training_config_value(
        mode="staged",
        plan_filename="staged.order-plan.json",
        plan_sha256=staged_plan_sha256,
        selected_count=len(selected_train),
        validation_count=len(safe_validation_sorted),
        training=config["training"],
        policy_path=policy_path,
        policy_artifact_sha256=policy_binding["artifact_sha256"],
        parent_path=parent_path,
        parent_tree_sha256=parent_binding["tree_sha256"],
    )
    shuffled_config_payload = _yaml_bytes(shuffled_config)
    staged_config_payload = _yaml_bytes(staged_config)
    dataset_manifest = _dataset_manifest(
        train_sha256=selected_train_sha256,
        train_count=len(selected_train),
        validation_sha256=safe_validation_sha256,
        validation_count=len(safe_validation_sorted),
        pilot_manifest_sha256=pilot_manifest_binding["sha256"],
        policy_binding=policy_binding,
        parent_binding=parent_binding,
        selected_ids_sha256=selected_ids_sha256,
    )
    dataset_manifest_payload = canonical_json_bytes(dataset_manifest)
    shuffled_run_input_sha256 = sha256_bytes(
        canonical_json_bytes(
            {
                "comparison_pair_sha256": comparison_pair_sha256,
                "mode": "shuffled",
                "plan_sha256": shuffled_plan_sha256,
                "training_config_sha256": sha256_bytes(
                    shuffled_config_payload
                ),
                "state": {
                    "optimizer": "fresh",
                    "scheduler": "fresh",
                    "parent_as_resume": "forbidden",
                },
            }
        )
    )
    staged_run_input_sha256 = sha256_bytes(
        canonical_json_bytes(
            {
                "comparison_pair_sha256": comparison_pair_sha256,
                "mode": "staged",
                "plan_sha256": staged_plan_sha256,
                "training_config_sha256": sha256_bytes(
                    staged_config_payload
                ),
                "state": {
                    "optimizer": "fresh",
                    "scheduler": "fresh",
                    "parent_as_resume": "forbidden",
                },
            }
        )
    )
    parent_manifest_binding = {
        key: parent_binding[key]
        for key in (
            "source",
            "selection_report_artifact_sha256",
            "candidate_id",
            "learning_rate",
            "checkpoint_directory",
            "tree_sha256",
            "selector_model_tree_sha256",
            "selector_inventory_sha256",
            "training_report_sha256",
            "external_failure_signature_sha256",
            "scope",
            "selection_basis",
            "release_eligible",
            "relationship",
        )
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_remediation_experiment_bundle",
        "experiment_id": config["experiment_id"],
        "comparison_pair_sha256": comparison_pair_sha256,
        "inputs": {
            "builder_config": {
                "filename": config_snapshot.path.name,
                "sha256": config_snapshot.sha256,
            },
            "pilot_manifest": pilot_manifest_binding,
            "pilot_train": {
                "filename": pilot_train_binding["filename"],
                "sha256": pilot_train_binding["sha256"],
                "record_count": pilot_train_binding["record_count"],
                "split": "train",
            },
            "pilot_validation": {
                "filename": pilot_validation_binding["filename"],
                "sha256": pilot_validation_binding["sha256"],
                "record_count": pilot_validation_binding["record_count"],
                "split": "validation",
            },
            "protection_policy": policy_binding,
            "parent_checkpoint": parent_manifest_binding,
        },
        "safety_filters": {
            "train": train_filter,
            "validation": validation_filter,
        },
        "selection": {
            "algorithm": REMEDIATION_SUBSET_ALGORITHM,
            "phase_rules_version": PHASE_RULES_VERSION,
            "capacity_examples": capacity,
            "selected_examples": len(selected_train),
            "per_phase": len(selected_train) // len(PHASES),
            "phase_counts": selected_phase_counts,
            "selected_example_ids_sha256": selected_ids_sha256,
            "dataset_manifest": _artifact(
                "dataset-manifest.json",
                dataset_manifest_payload,
            ),
            "safety_source": {
                "filename": "pilot-train.safety-source.jsonl",
                "sha256": pilot_train_binding["sha256"],
                "record_count": pilot_train_binding["record_count"],
                "split": "train",
            },
            "train": {
                "filename": "selected-train.jsonl",
                "sha256": selected_train_sha256,
                "record_count": len(selected_train),
                "split": "train",
            },
            "validation": {
                "filename": "safe-validation.jsonl",
                "sha256": safe_validation_sha256,
                "record_count": len(safe_validation_sorted),
                "split": "validation",
            },
        },
        "schedule": {
            "epochs": 1,
            "effective_batch_size": REQUIRED_EFFECTIVE_BATCH_SIZE,
            "update_steps": update_steps,
            "maximum_updates": int(config["training"]["max_updates"]),
            "within_budget": True,
        },
        "training_contract": training_contract,
        "parity_dev": {
            "manifest": _artifact(
                "parity-dev.manifest.json",
                parity_manifest_payload,
            ),
            "references": {
                "filename": "parity-dev.references.jsonl",
                "sha256": sha256_bytes(parity_references_payload),
                "record_count": len(parity_references),
                "split": "regression",
            },
            "parent_split": "validation",
            "selected_example_ids_sha256": parity_manifest[
                "selected_example_ids_sha256"
            ],
        },
        "arms": {
            "only_intended_difference": (
                "training_order_mode_and_epoch_order"
            ),
            "shuffled": {
                "mode": "shuffled",
                "plan": _artifact(
                    "shuffled.order-plan.json",
                    shuffled_plan_payload,
                ),
                "training_config": _artifact(
                    "shuffled.train.yaml",
                    shuffled_config_payload,
                ),
                "planned_run_input_sha256": shuffled_run_input_sha256,
                "comparison_pair_sha256": comparison_pair_sha256,
            },
            "staged": {
                "mode": "staged",
                "plan": _artifact(
                    "staged.order-plan.json",
                    staged_plan_payload,
                ),
                "training_config": _artifact(
                    "staged.train.yaml",
                    staged_config_payload,
                ),
                "planned_run_input_sha256": staged_run_input_sha256,
                "comparison_pair_sha256": comparison_pair_sha256,
            },
        },
        "adoption_policy": {
            "comparison_config": comparison_policy_binding,
            "weights": dict(comparison_policy["weights"]),
            "bootstrap": {
                "paired": True,
                "resamples": comparison_policy["bootstrap_resamples"],
                "confidence_level": comparison_policy[
                    "confidence_level"
                ],
                "required_lower_bound": 0,
            },
            "minimum_composite_delta": comparison_policy[
                "minimum_composite_delta"
            ],
            "maximum_eval_loss_ratio": comparison_policy[
                "maximum_eval_loss_ratio"
            ],
            "hard_gates": {
                "protected_span_restoration_rate": 1,
                "protected_span_deletions": 0,
                "protected_span_duplications": 0,
                "protected_span_malformed": 0,
                "protected_span_reordered": 0,
                "new_critical_errors": 0,
                "sst_parse_rate_noninferior": True,
                "quality_metrics_noninferior": True,
                "evaluation_scope": "validation_only_parity_dev",
            },
            "decision_rule": (
                "adopt_staged_only_if_all_hard_gates_and_fixed_composite_"
                "and_paired_bootstrap_pass"
            ),
            "fallback": "keep_shuffled",
        },
        "holdout_isolation": {
            "opened_splits": ["train", "validation"],
            "test_inputs": False,
            "challenge_inputs": False,
            "ai_gold_inputs": False,
            "decision_data": "validation_only_parity_dev",
        },
        "execution_contract": {
            "run_kind": "improvement",
            "plan_kind": REMEDIATION_PLAN_KIND,
            "parent_relationship": "warm_start_parent_not_resume",
            "optimizer_state": "fresh",
            "scheduler_state": "fresh",
            "parent_as_resume": "forbidden",
            "same_run_resume": "identity_bound_recovery_only",
            "actual_run_fingerprints": "must_be_new_and_distinct",
            "post_run_match_assertions": [
                "accepted_training_ids_equal",
                "rejected_training_ids_equal",
                "training_contract_equal",
                "parent_checkpoint_equal",
                "protection_policy_equal",
                "parity_dev_case_ids_equal",
            ],
        },
    }
    validated_manifest = validate_remediation_experiment_manifest(manifest)
    manifest_payload = canonical_json_bytes(validated_manifest)
    destination = Path(output_dir).resolve()
    if destination.exists():
        raise RemediationExperimentError(
            f"refusing to replace existing remediation output: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.building-",
            dir=destination.parent,
        )
    )
    try:
        artifacts = {
            "pilot-train.safety-source.jsonl": pilot_train_safety_payload,
            "selected-train.jsonl": selected_train_payload,
            "safe-validation.jsonl": safe_validation_payload,
            "dataset-manifest.json": dataset_manifest_payload,
            "parity-dev.references.jsonl": parity_references_payload,
            "parity-dev.manifest.json": parity_manifest_payload,
            "shuffled.order-plan.json": shuffled_plan_payload,
            "staged.order-plan.json": staged_plan_payload,
            "shuffled.train.yaml": shuffled_config_payload,
            "staged.train.yaml": staged_config_payload,
            comparison_policy_binding["filename"]: (
                comparison_policy_payload
            ),
            "experiment-manifest.json": manifest_payload,
        }
        for filename, payload in artifacts.items():
            _write_file(temporary / filename, payload)
        temporary.replace(destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return RemediationExperimentBundle(
        output_dir=destination,
        manifest=MappingProxyType(validated_manifest),
    )
