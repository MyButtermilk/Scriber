"""Build one immutable single-arm repair for the failed protected-marker pilot.

The builder deliberately derives data, hyperparameters, parent state, and the
effective example order from the completed shuffled control.  The sole modeled
difference is the protected-marker representation and its occurrence mapping.
"""

from __future__ import annotations

import copy
import json
import math
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .curriculum import (
    canonical_json_bytes,
    hash_identifier_sequence,
    load_hash_bound_jsonl,
    sha256_bytes,
)
from .protected_spans import (
    GEMMA_UNUSED_POLICY_ID,
    LEXICAL_KEEP_POLICY_ID,
    validate_protection_policy_evidence,
)
from .remediation_experiment import (
    load_remediation_order_plan,
    validate_remediation_experiment_manifest,
    validate_remediation_order_plan,
)
from .tokenize import IGNORE_INDEX, tokenize_example
from .training_integrity import (
    bind_improvement_order_plan,
    bind_parent_checkpoint,
    bind_protection_policy,
    bind_training_dataset,
    checkpoint_tree_sha256,
    load_training_config,
)
from .training_runtime import (
    apply_training_protection,
    build_effective_improvement_order,
)

MARKER_REPAIR_KIND = "scriber_marker_repair_bundle"
FROZEN_EFFECTIVE_ORDER_ALGORITHM = "frozen_source_effective_order_v1"

_PROJECT_ROOT = Path(__file__).parents[2]
_CONTRACTS_ROOT = _PROJECT_ROOT / "contracts"
_RAW_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREFIXED_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class MarkerRepairError(ValueError):
    """A marker-repair input is incomplete, mutable, or not comparable."""


@dataclass(frozen=True, slots=True)
class MarkerRepairConfig:
    """One exact builder configuration."""

    path: Path
    byte_count: int
    sha256: str
    values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MarkerRepairBundle:
    """One atomically materialized and validated repair bundle."""

    output_dir: Path
    manifest: Mapping[str, Any]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise MarkerRepairError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@cache
def _validator(schema_file: str) -> Draft202012Validator:
    schema_path = _CONTRACTS_ROOT / schema_file
    try:
        schema = json.loads(schema_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MarkerRepairError(
            f"cannot load marker-repair schema {schema_path}: {error}"
        ) from error
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate_schema(
    value: Mapping[str, Any],
    *,
    schema_file: str,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MarkerRepairError(f"{label} must be an object")
    materialized = json.loads(json.dumps(dict(value), ensure_ascii=False))
    errors = sorted(
        _validator(schema_file).iter_errors(materialized),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = (
            ".".join(str(item) for item in errors[0].absolute_path)
            or "<root>"
        )
        raise MarkerRepairError(
            f"{label} schema failed at {location}: {errors[0].message}"
        )
    return materialized


def _stable_bytes(path: str | Path, label: str) -> tuple[Path, bytes]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise MarkerRepairError(f"{label} is not a regular file: {resolved}")
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
        raise MarkerRepairError(f"{label} changed while it was read")
    return resolved, payload


def _resolve_path(base_dir: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _bound_input(
    base_dir: Path,
    binding: Mapping[str, Any],
    *,
    label: str,
) -> tuple[Path, bytes]:
    path, payload = _stable_bytes(
        _resolve_path(base_dir, str(binding["path"])),
        label,
    )
    actual = sha256_bytes(payload)
    if actual != binding["sha256"]:
        raise MarkerRepairError(
            f"{label} SHA-256 mismatch: expected {binding['sha256']}, "
            f"got {actual}"
        )
    return path, payload


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MarkerRepairError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise MarkerRepairError(f"{label} must contain a JSON object")
    return value


def _artifact(filename: str, payload: bytes) -> dict[str, Any]:
    return {"filename": filename, "sha256": sha256_bytes(payload)}


def _split_artifact(
    filename: str,
    payload: bytes,
    *,
    record_count: int,
) -> dict[str, Any]:
    return {
        **_artifact(filename, payload),
        "record_count": record_count,
    }


def validate_marker_repair_config(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the closed repair-builder configuration."""

    return _validate_schema(
        value,
        schema_file="marker_repair_config_schema.json",
        label="marker repair config",
    )


def load_marker_repair_config(path: str | Path) -> MarkerRepairConfig:
    """Load exact YAML bytes with duplicate-key rejection."""

    resolved, payload = _stable_bytes(path, "marker repair config")
    try:
        value = yaml.load(payload.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise MarkerRepairError(
            f"marker repair config is invalid YAML: {error}"
        ) from error
    validated = validate_marker_repair_config(value)
    return MarkerRepairConfig(
        path=resolved,
        byte_count=len(payload),
        sha256=sha256_bytes(payload),
        values=MappingProxyType(validated),
    )


def _policy_binding(
    filename: str,
    payload: bytes,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "filename": filename,
        "artifact_sha256": sha256_bytes(payload),
        "policy_id": policy["policy_id"],
        "policy_sha256": policy["policy_sha256"],
        "detector": policy["detector"],
        "pair_mapping": policy["pair_mapping"],
        "tokenizer_identity_sha256": policy["tokenizer"][
            "identity_sha256"
        ],
    }


def _token_preflight(
    records: Sequence[Mapping[str, Any]],
    *,
    policy: Mapping[str, Any],
    tokenizer: Any,
    sequence_length_limit: int,
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[str, ...],
    tuple[int, ...],
    dict[str, Any],
]:
    protected = apply_training_protection(records, policy=policy)
    if protected.rejected_example_ids:
        raise MarkerRepairError(
            f"{policy['policy_id']} rejected "
            f"{len(protected.rejected_example_ids)} selected records"
        )
    accepted_records: list[dict[str, Any]] = []
    accepted_ids: list[str] = []
    sequence_lengths: list[int] = []
    sequence_rejected_ids: list[str] = []
    for record in protected.records:
        encoded = tokenize_example(
            tokenizer,
            transcript=str(record["source_text"]),
            completion=str(record["target_sst"]),
        )
        labels = encoded["labels"]
        if not labels or not any(label != IGNORE_INDEX for label in labels):
            raise MarkerRepairError(
                f"completion-only mask is empty for {record['example_id']}"
            )
        length = len(encoded["input_ids"])
        if length > sequence_length_limit:
            sequence_rejected_ids.append(str(record["example_id"]))
            continue
        accepted_records.append(dict(record))
        accepted_ids.append(str(record["example_id"]))
        sequence_lengths.append(length)
    if sequence_rejected_ids:
        raise MarkerRepairError(
            f"{len(sequence_rejected_ids)} selected records exceed "
            f"the frozen {sequence_length_limit}-token limit"
        )
    evidence = protected.evidence
    result = {
        "loaded_record_count": len(records),
        "accepted_record_count": len(accepted_records),
        "protection_rejected_record_count": len(
            protected.rejected_example_ids
        ),
        "sequence_rejected_record_count": len(sequence_rejected_ids),
        "protected_example_count": int(evidence["protected_example_count"]),
        "marker_count": int(evidence["protected_marker_count"]),
        "span_count": int(evidence["protected_span_count"]),
        "max_sequence_length": max(sequence_lengths),
        "sequence_length_limit": sequence_length_limit,
        "accepted_example_ids_sha256": hash_identifier_sequence(
            accepted_ids
        ),
        "transformed_pairs_sha256": evidence[
            "transformed_pairs_sha256"
        ],
        "sequence_lengths_sha256": sha256_bytes(
            canonical_json_bytes(sequence_lengths)
        ),
    }
    if (
        policy["policy_id"] == LEXICAL_KEEP_POLICY_ID
        and result["marker_count"] != result["span_count"]
    ):
        raise MarkerRepairError(
            "lexical preflight did not assign one marker per occurrence"
        )
    return (
        tuple(accepted_records),
        tuple(accepted_ids),
        tuple(sequence_lengths),
        result,
    )


def _copy_payload(root: Path, filename: str, payload: bytes) -> Path:
    path = root / filename
    if path.exists():
        raise MarkerRepairError(
            f"refusing to overwrite marker-repair artifact: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _copy_checkpoint(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise MarkerRepairError(
            f"parent checkpoint is not a directory: {source}"
        )
    if destination.exists():
        raise MarkerRepairError(
            f"refusing to overwrite parent checkpoint: {destination}"
        )
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def _source_artifact(
    source_root: Path,
    binding: Mapping[str, Any],
    *,
    label: str,
) -> tuple[Path, bytes]:
    filename = binding.get("filename")
    digest = binding.get("sha256")
    if (
        not isinstance(filename, str)
        or not filename
        or not isinstance(digest, str)
        or not _RAW_SHA256.fullmatch(digest)
    ):
        raise MarkerRepairError(f"{label} has an invalid source binding")
    path, payload = _stable_bytes(source_root / filename, label)
    actual = sha256_bytes(payload)
    if actual != digest:
        raise MarkerRepairError(
            f"{label} differs from its source manifest: "
            f"expected {digest}, got {actual}"
        )
    return path, payload


def _validate_source_run(
    run_identity_payload: bytes,
    training_report_payload: bytes,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_identity = _json_object(
        run_identity_payload,
        "source shuffled run identity",
    )
    training_report = _json_object(
        training_report_payload,
        "source shuffled training report",
    )
    fingerprint = run_identity.get("run_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or not _PREFIXED_SHA256.fullmatch(fingerprint)
        or training_report.get("run_fingerprint") != fingerprint
    ):
        raise MarkerRepairError(
            "source training report and run identity fingerprints differ"
        )
    if training_report.get("training_completed") is not True:
        raise MarkerRepairError("source shuffled training did not complete")
    report_identity = training_report.get("run_identity")
    if (
        not isinstance(report_identity, Mapping)
        or report_identity.get("sha256")
        != "sha256:" + sha256_bytes(run_identity_payload)
    ):
        raise MarkerRepairError(
            "source training report does not bind the supplied run identity"
        )
    return run_identity, training_report


def _source_effective_order(
    *,
    source_plan: Mapping[str, Any],
    source_plan_path: Path,
    source_plan_sha256: str,
    source_training_report: Mapping[str, Any],
    train_records: Sequence[Mapping[str, Any]],
    selection_policy: Mapping[str, Any],
    tokenizer: Any,
    sequence_length_limit: int,
) -> tuple[list[str], dict[str, Any]]:
    protected = apply_training_protection(
        train_records,
        policy=selection_policy,
    )
    if protected.rejected_example_ids:
        raise MarkerRepairError(
            "source selected cohort is no longer safe under its selection policy"
        )
    used_ids: list[str] = []
    lengths: list[int] = []
    oversize: list[str] = []
    for record in protected.records:
        encoded = tokenize_example(
            tokenizer,
            transcript=str(record["source_text"]),
            completion=str(record["target_sst"]),
        )
        length = len(encoded["input_ids"])
        if length > sequence_length_limit:
            oversize.append(str(record["example_id"]))
            continue
        used_ids.append(str(record["example_id"]))
        lengths.append(length)
    if oversize:
        raise MarkerRepairError(
            "source selected cohort no longer reproduces without "
            "sequence-length rejections"
        )
    effective = build_effective_improvement_order(
        source_plan,
        plan_binding={
            "path": str(source_plan_path),
            "sha256": "sha256:" + source_plan_sha256,
        },
        loaded_example_ids=[
            str(record["example_id"]) for record in train_records
        ],
        safe_example_ids=protected.accepted_example_ids,
        policy_rejected_example_ids=protected.rejected_example_ids,
        policy_rejection_evidence=protected.evidence,
        used_example_ids=used_ids,
        sequence_rejected_example_ids=oversize,
        sequence_lengths=lengths,
    )
    if len(effective.epoch_indices) != 1:
        raise MarkerRepairError(
            "source shuffled control must contain exactly one effective epoch"
        )
    ordered_ids = [
        used_ids[index] for index in effective.epoch_indices[0]
    ]
    remediation_order = source_training_report.get("remediation_order")
    if not isinstance(remediation_order, Mapping):
        raise MarkerRepairError(
            "source training report lacks remediation-order evidence"
        )
    reports = remediation_order.get("effective_epoch_orders")
    if not isinstance(reports, list) or len(reports) != 1:
        raise MarkerRepairError(
            "source training report lacks one effective epoch"
        )
    report_epoch = reports[0]
    expected_hash = hash_identifier_sequence(ordered_ids)
    if (
        report_epoch.get("effective_example_ids_sha256") != expected_hash
        or report_epoch.get("record_count") != len(ordered_ids)
        or remediation_order.get("record_count") != len(ordered_ids)
        or remediation_order.get("mode") != "shuffled"
    ):
        raise MarkerRepairError(
            "reconstructed source effective order differs from completed-run evidence"
        )
    bucket_algorithm = remediation_order.get("bucket_algorithm")
    bucket_window = remediation_order.get("bucket_window")
    if (
        not isinstance(bucket_algorithm, str)
        or not bucket_algorithm
        or isinstance(bucket_window, bool)
        or not isinstance(bucket_window, int)
        or bucket_window <= 0
    ):
        raise MarkerRepairError(
            "source training report has invalid bucketing evidence"
        )
    return ordered_ids, {
        "algorithm": FROZEN_EFFECTIVE_ORDER_ALGORITHM,
        "record_count": len(ordered_ids),
        "source_declared_order_sha256": source_plan["epoch_orders"][0][
            "example_ids_sha256"
        ],
        "source_effective_order_sha256": expected_hash,
        "source_bucket_algorithm": bucket_algorithm,
        "source_bucket_window": bucket_window,
    }


def validate_marker_repair_manifest(
    value: Mapping[str, Any],
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Validate schema plus all derivable marker-repair claims."""

    manifest = _validate_schema(
        value,
        schema_file="marker_repair_manifest_schema.json",
        label="marker repair manifest",
    )
    if (
        manifest["policies"]["selection"]["policy_id"]
        != GEMMA_UNUSED_POLICY_ID
        or manifest["policies"]["training"]["policy_id"]
        != LEXICAL_KEEP_POLICY_ID
    ):
        raise MarkerRepairError(
            "marker repair must preserve gemma_unused_v1 selection and "
            "train with gemma_lexical_v1"
        )
    if (
        manifest["policies"]["selection"]["tokenizer_identity_sha256"]
        != manifest["policies"]["training"]["tokenizer_identity_sha256"]
    ):
        raise MarkerRepairError(
            "selection and training policies bind different tokenizers"
        )
    train_count = manifest["data"]["train"]["record_count"]
    validation_count = manifest["data"]["validation"]["record_count"]
    if (
        manifest["preflight"]["train"]["accepted_record_count"]
        != train_count
        or manifest["preflight"]["validation"]["accepted_record_count"]
        != validation_count
    ):
        raise MarkerRepairError(
            "marker-repair preflight counts differ from frozen data"
        )
    for split in ("train", "validation"):
        preflight = manifest["preflight"][split]
        if (
            preflight["loaded_record_count"]
            != preflight["accepted_record_count"]
            or preflight["max_sequence_length"]
            > preflight["sequence_length_limit"]
            or preflight["marker_count"] != preflight["span_count"]
        ):
            raise MarkerRepairError(
                f"marker-repair {split} preflight is not lossless"
            )
    schedule = manifest["schedule"]
    if schedule["update_steps"] != math.ceil(
        train_count / schedule["effective_batch_size"]
    ):
        raise MarkerRepairError(
            "marker-repair update count differs from the full frozen epoch"
        )
    order_lock = manifest["order_lock"]
    if (
        order_lock["record_count"] != train_count
        or order_lock["source_effective_order_sha256"]
        == ""
    ):
        raise MarkerRepairError(
            "marker-repair effective order is not bound to the train cohort"
        )
    if (
        manifest["source"]["prior_comparison_pair_sha256"]
        == manifest["repair_input_sha256"].removeprefix("sha256:")
    ):
        raise MarkerRepairError(
            "marker repair reused the prior A/B comparison identity"
        )
    if root is not None:
        for label, binding in manifest["artifacts"].items():
            path, payload = _stable_bytes(root / binding["filename"], label)
            del path
            if sha256_bytes(payload) != binding["sha256"]:
                raise MarkerRepairError(
                    f"materialized artifact {label} differs from its manifest"
                )
        parent = root / manifest["parent_checkpoint"]["path"]
        if (
            checkpoint_tree_sha256(parent)
            != manifest["parent_checkpoint"]["tree_sha256"]
        ):
            raise MarkerRepairError(
                "materialized parent checkpoint tree differs from manifest"
            )
    return manifest


def verify_marker_repair_bundle(
    bundle_dir: str | Path,
    *,
    tokenizer: Any,
) -> dict[str, Any]:
    """Bind every training input and stop immediately before model loading."""

    root = Path(bundle_dir).expanduser().resolve()
    manifest_path, manifest_payload = _stable_bytes(
        root / "repair-manifest.json",
        "marker repair manifest",
    )
    del manifest_path
    manifest = validate_marker_repair_manifest(
        _json_object(manifest_payload, "marker repair manifest"),
        root=root,
    )
    config = load_training_config(root / "train.yaml")
    dataset = bind_training_dataset(
        root / "dataset-manifest.json",
        train=root / manifest["data"]["train"]["filename"],
        validation=root / manifest["data"]["validation"]["filename"],
        train_limit=int(manifest["data"]["train"]["record_count"]),
        validation_limit=int(
            manifest["data"]["validation"]["record_count"]
        ),
    )
    protection_policy = bind_protection_policy(
        config,
        tokenizer=tokenizer,
    )
    parent = bind_parent_checkpoint(config)
    if protection_policy is None or parent is None:
        raise MarkerRepairError(
            "marker repair did not bind its policy and warm-start parent"
        )
    train_protected = apply_training_protection(
        dataset.train.records,
        policy=protection_policy.values,
    )
    validation_protected = apply_training_protection(
        dataset.validation.records,
        policy=protection_policy.values,
    )
    plan = bind_improvement_order_plan(
        config,
        dataset,
        protection_policy=protection_policy,
        parent_checkpoint=parent,
        protection_application=train_protected.evidence,
    )
    if plan is None:
        raise MarkerRepairError("marker repair did not bind an order plan")
    sequence_limit = int(config.values["max_sequence_length"])
    train_ids: list[str] = []
    train_lengths: list[int] = []
    train_oversize: list[str] = []
    for record in train_protected.records:
        encoded = tokenize_example(
            tokenizer,
            transcript=str(record["source_text"]),
            completion=str(record["target_sst"]),
        )
        length = len(encoded["input_ids"])
        if length > sequence_limit:
            train_oversize.append(str(record["example_id"]))
            continue
        train_ids.append(str(record["example_id"]))
        train_lengths.append(length)
    validation_oversize: list[str] = []
    for record in validation_protected.records:
        encoded = tokenize_example(
            tokenizer,
            transcript=str(record["source_text"]),
            completion=str(record["target_sst"]),
        )
        if len(encoded["input_ids"]) > sequence_limit:
            validation_oversize.append(str(record["example_id"]))
    if (
        train_protected.rejected_example_ids
        or validation_protected.rejected_example_ids
        or train_oversize
        or validation_oversize
    ):
        raise MarkerRepairError(
            "marker-repair dry bind produced a policy or length rejection"
        )
    effective = build_effective_improvement_order(
        plan.values,
        plan_binding=plan.binding(),
        loaded_example_ids=[
            str(record["example_id"]) for record in dataset.train.records
        ],
        safe_example_ids=train_protected.accepted_example_ids,
        policy_rejected_example_ids=(
            train_protected.rejected_example_ids
        ),
        policy_rejection_evidence=train_protected.evidence,
        used_example_ids=train_ids,
        sequence_rejected_example_ids=train_oversize,
        sequence_lengths=train_lengths,
    )
    if len(effective.epoch_indices) != 1:
        raise MarkerRepairError(
            "marker-repair dry bind produced more than one epoch"
        )
    effective_ids = [
        train_ids[index] for index in effective.epoch_indices[0]
    ]
    effective_hash = hash_identifier_sequence(effective_ids)
    if (
        effective_hash
        != manifest["order_lock"]["source_effective_order_sha256"]
        or effective.evidence["effective_epoch_orders"][0][
            "effective_example_ids_sha256"
        ]
        != effective_hash
    ):
        raise MarkerRepairError(
            "marker-repair dry bind changed the frozen effective order"
        )
    update_steps = math.ceil(
        len(train_ids)
        / (
            int(config.values["micro_batch_size"])
            * int(config.values["gradient_accumulation_steps"])
        )
    )
    if update_steps != manifest["schedule"]["update_steps"]:
        raise MarkerRepairError(
            "marker-repair dry bind changed the update schedule"
        )
    return {
        "schema_version": 1,
        "kind": "scriber_marker_repair_dry_bind",
        "repair_input_sha256": manifest["repair_input_sha256"],
        "ready_for_model_load": True,
        "train_examples": len(train_ids),
        "validation_examples": len(validation_protected.records),
        "update_steps": update_steps,
        "effective_order_sha256": effective_hash,
        "protection_policy_id": protection_policy.values["policy_id"],
        "protection_policy_sha256": protection_policy.values[
            "policy_sha256"
        ],
        "parent_checkpoint_tree_sha256": parent.tree_sha256,
    }


def build_marker_repair_bundle(
    config_path: str | Path,
    *,
    output_dir: str | Path,
    tokenizer: Any,
) -> MarkerRepairBundle:
    """Build an immutable repair bundle and refuse every additional difference."""

    config_snapshot = load_marker_repair_config(config_path)
    config = config_snapshot.values
    base_dir = config_snapshot.path.parent
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise MarkerRepairError(
            f"refusing to overwrite marker-repair bundle: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    inputs = config["inputs"]
    source_manifest_path, source_manifest_payload = _bound_input(
        base_dir,
        inputs["source_experiment_manifest"],
        label="source experiment manifest",
    )
    source_manifest = validate_remediation_experiment_manifest(
        _json_object(source_manifest_payload, "source experiment manifest")
    )
    source_root = source_manifest_path.parent
    source_arm = source_manifest["arms"]["shuffled"]
    source_plan_path, source_plan_payload = _source_artifact(
        source_root,
        source_arm["plan"],
        label="source shuffled order plan",
    )
    source_plan, source_plan_sha256 = load_remediation_order_plan(
        source_plan_path,
        expected_sha256=source_arm["plan"]["sha256"],
    )
    source_training_config_path, source_training_config_payload = (
        _source_artifact(
            source_root,
            source_arm["training_config"],
            label="source shuffled training config",
        )
    )
    source_training_config = load_training_config(
        source_training_config_path
    )

    run_identity_path, run_identity_payload = _bound_input(
        base_dir,
        inputs["source_shuffled_run_identity"],
        label="source shuffled run identity",
    )
    training_report_path, training_report_payload = _bound_input(
        base_dir,
        inputs["source_shuffled_training_report"],
        label="source shuffled training report",
    )
    run_identity, training_report = _validate_source_run(
        run_identity_payload,
        training_report_payload,
    )

    selection_policy_path, selection_policy_payload = _bound_input(
        base_dir,
        inputs["selection_policy"],
        label="selection policy",
    )
    training_policy_path, training_policy_payload = _bound_input(
        base_dir,
        inputs["training_policy"],
        label="training policy",
    )
    selection_policy = validate_protection_policy_evidence(
        _json_object(selection_policy_payload, "selection policy"),
        tokenizer=tokenizer,
    )
    training_policy = validate_protection_policy_evidence(
        _json_object(training_policy_payload, "training policy"),
        tokenizer=tokenizer,
    )
    if (
        selection_policy["policy_id"] != GEMMA_UNUSED_POLICY_ID
        or training_policy["policy_id"] != LEXICAL_KEEP_POLICY_ID
        or selection_policy["tokenizer"] != training_policy["tokenizer"]
    ):
        raise MarkerRepairError(
            "repair requires old selection and lexical training policies "
            "bound to one exact tokenizer"
        )
    if (
        source_plan["protection_policy"]["artifact_sha256"]
        != sha256_bytes(selection_policy_payload)
        or source_plan["protection_policy"]["policy_sha256"]
        != selection_policy["policy_sha256"]
    ):
        raise MarkerRepairError(
            "selection policy differs from the source shuffled plan"
        )

    parent_path = _resolve_path(
        base_dir,
        str(inputs["parent_checkpoint"]["path"]),
    )
    expected_parent_tree = inputs["parent_checkpoint"]["tree_sha256"]
    actual_parent_tree = checkpoint_tree_sha256(parent_path)
    if actual_parent_tree != expected_parent_tree:
        raise MarkerRepairError(
            "parent checkpoint differs from repair config"
        )
    if (
        str(source_training_config.values["parent_checkpoint_tree_sha256"])
        != expected_parent_tree
        or source_plan["parent_checkpoint"]["tree_sha256"]
        != expected_parent_tree
    ):
        raise MarkerRepairError(
            "repair parent differs from the completed shuffled control"
        )

    selection = source_manifest["selection"]
    train_path, train_payload = _source_artifact(
        source_root,
        selection["train"],
        label="selected repair train",
    )
    validation_path, validation_payload = _source_artifact(
        source_root,
        selection["validation"],
        label="selected repair validation",
    )
    safety_path, safety_payload = _source_artifact(
        source_root,
        selection["safety_source"],
        label="selection safety source",
    )
    parity_path, parity_payload = _source_artifact(
        source_root,
        source_manifest["parity_dev"]["references"],
        label="parity references",
    )
    parity_manifest_path, parity_manifest_payload = _source_artifact(
        source_root,
        source_manifest["parity_dev"]["manifest"],
        label="parity selection manifest",
    )
    dataset_manifest_path, dataset_manifest_payload = _source_artifact(
        source_root,
        selection["dataset_manifest"],
        label="source dataset manifest",
    )
    del (
        train_path,
        validation_path,
        safety_path,
        parity_path,
        parity_manifest_path,
        dataset_manifest_path,
        selection_policy_path,
        training_policy_path,
        run_identity_path,
        training_report_path,
    )

    train_records, train_binding = load_hash_bound_jsonl(
        source_root / selection["train"]["filename"],
        expected_sha256=selection["train"]["sha256"],
        expected_split="train",
        label="marker repair train",
    )
    validation_records, validation_binding = load_hash_bound_jsonl(
        source_root / selection["validation"]["filename"],
        expected_sha256=selection["validation"]["sha256"],
        expected_split="validation",
        label="marker repair validation",
    )
    sequence_limit = int(
        source_training_config.values["max_sequence_length"]
    )
    effective_ids, order_evidence = _source_effective_order(
        source_plan=source_plan,
        source_plan_path=source_plan_path,
        source_plan_sha256=source_plan_sha256,
        source_training_report=training_report,
        train_records=train_records,
        selection_policy=selection_policy,
        tokenizer=tokenizer,
        sequence_length_limit=sequence_limit,
    )
    (
        _new_train_records,
        new_train_ids,
        _new_train_lengths,
        train_preflight,
    ) = _token_preflight(
        train_records,
        policy=training_policy,
        tokenizer=tokenizer,
        sequence_length_limit=sequence_limit,
    )
    (
        _new_validation_records,
        _new_validation_ids,
        _new_validation_lengths,
        validation_preflight,
    ) = _token_preflight(
        validation_records,
        policy=training_policy,
        tokenizer=tokenizer,
        sequence_length_limit=sequence_limit,
    )
    if (
        tuple(new_train_ids)
        != tuple(str(record["example_id"]) for record in train_records)
        or train_preflight["accepted_example_ids_sha256"]
        != selection["selected_example_ids_sha256"]
    ):
        raise MarkerRepairError(
            "lexical training policy changed the selected train cohort"
        )
    del (
        _new_train_records,
        _new_train_lengths,
        _new_validation_records,
        _new_validation_ids,
        _new_validation_lengths,
    )

    repair_identity = {
        "schema_version": 1,
        "experiment_id": config["experiment_id"],
        "source_manifest_sha256": sha256_bytes(source_manifest_payload),
        "source_run_fingerprint": run_identity["run_fingerprint"],
        "source_effective_order_sha256": order_evidence[
            "source_effective_order_sha256"
        ],
        "selected_train_example_ids_sha256": selection[
            "selected_example_ids_sha256"
        ],
        "selection_policy_sha256": selection_policy["policy_sha256"],
        "training_policy_sha256": training_policy["policy_sha256"],
        "parent_checkpoint_tree_sha256": expected_parent_tree,
    }
    repair_input_sha256 = (
        "sha256:" + sha256_bytes(canonical_json_bytes(repair_identity))
    )
    comparison_pair_sha256 = sha256_bytes(
        canonical_json_bytes(
            {
                **repair_identity,
                "purpose": "single_arm_marker_mechanism_repair_v1",
            }
        )
    )
    if comparison_pair_sha256 == source_manifest["comparison_pair_sha256"]:
        raise MarkerRepairError(
            "repair comparison identity collides with the source A/B pair"
        )

    temp_root = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.tmp-",
            dir=target.parent,
        )
    )
    try:
        filenames: dict[str, bytes] = {
            "source-experiment-manifest.json": source_manifest_payload,
            "source-shuffled.order-plan.json": source_plan_payload,
            "source-shuffled.train.yaml": source_training_config_payload,
            "source-shuffled.run-identity.json": run_identity_payload,
            "source-shuffled.training-report.json": training_report_payload,
            "selection-policy.json": selection_policy_payload,
            "training-policy.json": training_policy_payload,
            "selected-train.jsonl": train_payload,
            "safe-validation.jsonl": validation_payload,
            "pilot-train.safety-source.jsonl": safety_payload,
            "parity-dev.references.jsonl": parity_payload,
            "parity-selection.manifest.json": parity_manifest_payload,
        }
        baseline_manifest: dict[str, dict[str, Any]] = {}
        baseline_names = {
            "prediction_manifest": "baseline.predictions.manifest.json",
            "predictions": "baseline.predictions.jsonl",
            "evaluation": "baseline.evaluation.json",
            "comparison": "baseline.comparison.json",
        }
        for label, filename in baseline_names.items():
            _path, payload = _bound_input(
                base_dir,
                config["baseline"][label],
                label=f"baseline {label}",
            )
            del _path
            filenames[filename] = payload
            baseline_manifest[label] = {
                "path": filename,
                "sha256": sha256_bytes(payload),
            }
        for filename, payload in filenames.items():
            _copy_payload(temp_root, filename, payload)

        source_dataset_manifest = _json_object(
            dataset_manifest_payload,
            "source dataset manifest",
        )
        dataset_manifest = copy.deepcopy(source_dataset_manifest)
        dataset_manifest["protection_policy"] = {
            "artifact_sha256": sha256_bytes(training_policy_payload),
            "policy_id": training_policy["policy_id"],
            "policy_sha256": training_policy["policy_sha256"],
        }
        dataset_manifest_bytes = canonical_json_bytes(dataset_manifest)
        _copy_payload(
            temp_root,
            "dataset-manifest.json",
            dataset_manifest_bytes,
        )

        repair_plan = copy.deepcopy(source_plan)
        repair_plan["comparison_pair_sha256"] = comparison_pair_sha256
        repair_plan["selection_policy"] = {
            "filename": "selection-policy.json",
            "policy_id": selection_policy["policy_id"],
            "policy_sha256": selection_policy["policy_sha256"],
            "artifact_sha256": sha256_bytes(selection_policy_payload),
        }
        repair_plan["protection_policy"] = {
            "policy_id": training_policy["policy_id"],
            "policy_sha256": training_policy["policy_sha256"],
            "artifact_sha256": sha256_bytes(training_policy_payload),
        }
        repair_plan["effective_order_lock"] = {
            "algorithm": FROZEN_EFFECTIVE_ORDER_ALGORITHM,
            "source_order_plan": {
                "filename": "source-shuffled.order-plan.json",
                "sha256": source_plan_sha256,
            },
            "source_training_report": {
                "filename": "source-shuffled.training-report.json",
                "sha256": sha256_bytes(training_report_payload),
            },
            "example_ids": effective_ids,
            "example_ids_sha256": hash_identifier_sequence(effective_ids),
        }
        repair_plan = validate_remediation_order_plan(repair_plan)
        repair_plan_bytes = canonical_json_bytes(repair_plan)
        repair_plan_filename = "frozen-effective.order-plan.json"
        _copy_payload(
            temp_root,
            repair_plan_filename,
            repair_plan_bytes,
        )

        _copy_checkpoint(parent_path, temp_root / "parent-checkpoint")
        if (
            checkpoint_tree_sha256(temp_root / "parent-checkpoint")
            != expected_parent_tree
        ):
            raise MarkerRepairError(
                "copied parent checkpoint differs from its source tree"
            )

        training_config = dict(source_training_config.values)
        training_config["training_order_plan_path"] = repair_plan_filename
        training_config["training_order_plan_sha256"] = sha256_bytes(
            repair_plan_bytes
        )
        training_config["protection_policy_path"] = "training-policy.json"
        training_config["protection_policy_sha256"] = sha256_bytes(
            training_policy_payload
        )
        training_config["parent_checkpoint_path"] = "parent-checkpoint"
        training_config["parent_checkpoint_tree_sha256"] = (
            expected_parent_tree
        )
        training_config_bytes = yaml.safe_dump(
            training_config,
            sort_keys=False,
            allow_unicode=True,
        ).encode("utf-8")
        _copy_payload(temp_root, "train.yaml", training_config_bytes)
        load_training_config(temp_root / "train.yaml")

        preflight = {
            "schema_version": 1,
            "kind": "scriber_marker_repair_preflight",
            "repair_input_sha256": repair_input_sha256,
            "train": train_preflight,
            "validation": validation_preflight,
            "order_lock": order_evidence,
            "schedule": {
                "effective_batch_size": int(
                    source_manifest["schedule"]["effective_batch_size"]
                ),
                "epochs": 1,
                "update_steps": math.ceil(
                    len(train_records)
                    / int(
                        source_manifest["schedule"][
                            "effective_batch_size"
                        ]
                    )
                ),
            },
        }
        preflight_bytes = canonical_json_bytes(preflight)
        _copy_payload(temp_root, "preflight.json", preflight_bytes)

        acceptance_policy = {
            "schema_version": 1,
            "kind": "scriber_marker_repair_acceptance_policy",
            "decision": "accept_only_if_all_hard_gates_pass",
            "case_scope": "validation_only_parity_dev",
            "case_count": int(
                source_manifest["parity_dev"]["references"][
                    "record_count"
                ]
            ),
            "hard_gates": {
                "raw_sst_parse_rate": 1.0,
                "protected_span_restoration_rate": 1.0,
                "protected_span_deletions": 0,
                "protected_span_duplications": 0,
                "protected_span_malformed": 0,
                "protected_span_reordered": 0,
                "new_critical_errors": 0,
                "quality_metrics_noninferior": True,
            },
            "fallback": "reject_repair_keep_frozen_shuffled_control",
        }
        acceptance_bytes = yaml.safe_dump(
            acceptance_policy,
            sort_keys=False,
            allow_unicode=True,
        ).encode("utf-8")
        _copy_payload(
            temp_root,
            "acceptance-policy.yaml",
            acceptance_bytes,
        )

        all_artifact_payloads = {
            **filenames,
            "dataset-manifest.json": dataset_manifest_bytes,
            repair_plan_filename: repair_plan_bytes,
            "train.yaml": training_config_bytes,
            "preflight.json": preflight_bytes,
            "acceptance-policy.yaml": acceptance_bytes,
        }
        artifacts = {
            label.replace(".", "_").replace("-", "_"): _artifact(
                filename,
                payload,
            )
            for label, (filename, payload) in {
                name: (name, payload)
                for name, payload in all_artifact_payloads.items()
            }.items()
        }
        schedule = {
            "effective_batch_size": int(
                source_manifest["schedule"]["effective_batch_size"]
            ),
            "epochs": 1,
            "update_steps": math.ceil(
                len(train_records)
                / int(
                    source_manifest["schedule"]["effective_batch_size"]
                )
            ),
        }
        manifest = {
            "schema_version": 1,
            "kind": MARKER_REPAIR_KIND,
            "experiment_id": config["experiment_id"],
            "repair_input_sha256": repair_input_sha256,
            "source": {
                "experiment_manifest": _artifact(
                    "source-experiment-manifest.json",
                    source_manifest_payload,
                ),
                "shuffled_plan": _artifact(
                    "source-shuffled.order-plan.json",
                    source_plan_payload,
                ),
                "shuffled_training_config": _artifact(
                    "source-shuffled.train.yaml",
                    source_training_config_payload,
                ),
                "shuffled_run_identity": _artifact(
                    "source-shuffled.run-identity.json",
                    run_identity_payload,
                ),
                "shuffled_training_report": _artifact(
                    "source-shuffled.training-report.json",
                    training_report_payload,
                ),
                "prior_run_fingerprint": run_identity[
                    "run_fingerprint"
                ],
                "prior_comparison_pair_sha256": source_manifest[
                    "comparison_pair_sha256"
                ],
            },
            "policies": {
                "selection": _policy_binding(
                    "selection-policy.json",
                    selection_policy_payload,
                    selection_policy,
                ),
                "training": _policy_binding(
                    "training-policy.json",
                    training_policy_payload,
                    training_policy,
                ),
            },
            "data": {
                "train": _split_artifact(
                    "selected-train.jsonl",
                    train_payload,
                    record_count=int(train_binding["record_count"]),
                ),
                "validation": _split_artifact(
                    "safe-validation.jsonl",
                    validation_payload,
                    record_count=int(validation_binding["record_count"]),
                ),
                "safety_source": _split_artifact(
                    "pilot-train.safety-source.jsonl",
                    safety_payload,
                    record_count=int(
                        selection["safety_source"]["record_count"]
                    ),
                ),
                "parity_references": _split_artifact(
                    "parity-dev.references.jsonl",
                    parity_payload,
                    record_count=int(
                        source_manifest["parity_dev"]["references"][
                            "record_count"
                        ]
                    ),
                ),
                "parity_selection_manifest": _artifact(
                    "parity-selection.manifest.json",
                    parity_manifest_payload,
                ),
                "selected_train_example_ids_sha256": selection[
                    "selected_example_ids_sha256"
                ],
            },
            "parent_checkpoint": {
                "path": "parent-checkpoint",
                "tree_sha256": expected_parent_tree,
                "relationship": "warm_start_parent_not_resume",
            },
            "training_contract": dict(
                source_manifest["training_contract"]
            ),
            "schedule": schedule,
            "order_lock": order_evidence,
            "preflight": {
                "train": train_preflight,
                "validation": validation_preflight,
            },
            "baseline": baseline_manifest,
            "holdout_isolation": {
                "opened_splits": ["train", "validation"],
                "test_inputs": False,
                "challenge_inputs": False,
                "ai_gold_inputs": False,
                "decision_data": "validation_only_parity_dev",
            },
            "only_intended_difference": (
                "protected_marker_representation_and_pair_mapping"
            ),
            "execution_contract": {
                "run_kind": "improvement",
                "optimizer_state": "fresh",
                "scheduler_state": "fresh",
                "parent_as_resume": "forbidden",
                "same_run_resume": "identity_bound_recovery_only",
                "implicit_trainer_shuffling": False,
            },
            "artifacts": artifacts,
        }
        manifest = validate_marker_repair_manifest(
            manifest,
            root=temp_root,
        )
        manifest_bytes = canonical_json_bytes(manifest)
        _copy_payload(
            temp_root,
            "repair-manifest.json",
            manifest_bytes,
        )

        temp_root.replace(target)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    return MarkerRepairBundle(
        output_dir=target,
        manifest=MappingProxyType(manifest),
    )
