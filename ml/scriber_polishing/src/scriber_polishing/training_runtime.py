"""Runtime helpers for real, completion-only Gemma supervised training."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch
from torch.utils.data import Sampler

from .curriculum import (
    PHASES,
    canonical_json_bytes,
    hash_identifier_sequence,
    sha256_bytes,
    validate_curriculum_plan,
)
from .protected_spans import (
    LEXICAL_KEEP_POLICY_ID,
    TrainingPairProtectionError,
    _mask_training_pair_validated,
    validate_protection_policy_evidence,
)
from .tokenize import IGNORE_INDEX

CURRICULUM_BUCKET_WINDOW = 64
CURRICULUM_BUCKET_ALGORITHM = "fixed_window_length_descending_v1"


@dataclass(frozen=True, slots=True)
class EffectiveTrainingOrder:
    """Exact dataset-index permutations and their immutable report evidence."""

    epoch_indices: tuple[tuple[int, ...], ...]
    evidence: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProtectedTrainingRecords:
    """Accepted masked records plus exact fail-closed rejection evidence."""

    records: tuple[dict[str, Any], ...]
    accepted_example_ids: tuple[str, ...]
    rejected_example_ids: tuple[str, ...]
    evidence: Mapping[str, Any]


def apply_training_protection(
    records: Sequence[Mapping[str, Any]],
    *,
    policy: Mapping[str, object],
) -> ProtectedTrainingRecords:
    """Apply one policy to both sides of every pair and bind all rejections."""

    validated_policy = validate_protection_policy_evidence(policy)
    accepted: list[dict[str, Any]] = []
    accepted_ids: list[str] = []
    rejected_ids: list[str] = []
    rejection_reasons: Counter[str] = Counter()
    rejected_ids_by_reason: dict[str, list[str]] = {}
    protected_marker_count = 0
    protected_span_count = 0
    protected_example_count = 0
    declared_protection_record_count = 0
    runtime_detector_record_count = 0
    mapped_protected_occurrence_count = 0
    deduplicated_source_occurrence_count = 0
    seen_ids: set[str] = set()
    for position, record in enumerate(records):
        example_id = record.get("example_id")
        source_text = record.get("source_text")
        target_sst = record.get("target_sst")
        if (
            not isinstance(example_id, str)
            or not example_id
            or example_id in seen_ids
            or not isinstance(source_text, str)
            or not source_text
            or not isinstance(target_sst, str)
            or not target_sst
        ):
            raise ValueError(
                f"training protection received an invalid record at position {position}"
            )
        seen_ids.add(example_id)
        try:
            masked = _mask_training_pair_validated(
                source_text,
                target_sst,
                validated_policy,
                declared_target_values=record.get("protected_spans"),
                value_mappings=record.get("value_mappings"),
            )
        except TrainingPairProtectionError as error:
            rejected_ids.append(example_id)
            rejection_reasons[error.code] += 1
            rejected_ids_by_reason.setdefault(error.code, []).append(example_id)
            continue
        transformed = dict(record)
        transformed["source_text"] = masked.source_text
        transformed["target_sst"] = masked.target_sst
        accepted.append(transformed)
        accepted_ids.append(example_id)
        protected_marker_count += len(masked.spans)
        protected_span_count += sum(
            span.occurrences for span in masked.spans
        )
        protected_example_count += bool(masked.spans)
        declared_protection_record_count += (
            masked.protection_source == "record_declared_reversible_values_v1"
        )
        runtime_detector_record_count += (
            masked.protection_source == "runtime_detector_v1"
        )
        mapped_protected_occurrence_count += masked.mapped_occurrence_count
        deduplicated_source_occurrence_count += (
            masked.deduplicated_source_occurrence_count
        )
    if (
        validated_policy["policy_id"] == LEXICAL_KEEP_POLICY_ID
        and protected_marker_count != protected_span_count
    ):
        raise ValueError(
            "lexical protection requires one unique marker per protected occurrence"
        )
    if not accepted:
        raise ValueError("protection policy rejected every training record")
    transformed_identity = [
        {
            "example_id": record["example_id"],
            "source_text": record["source_text"],
            "target_sst": record["target_sst"],
        }
        for record in accepted
    ]
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "policy_id": validated_policy["policy_id"],
        "policy_sha256": validated_policy["policy_sha256"],
        "loaded_record_count": len(records),
        "accepted_record_count": len(accepted),
        "accepted_example_ids_sha256": hash_identifier_sequence(accepted_ids),
        "rejected_record_count": len(rejected_ids),
        "rejected_example_ids_sha256": hash_identifier_sequence(rejected_ids),
        "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
        "rejection_example_ids_by_reason": {
            reason: list(example_ids)
            for reason, example_ids in sorted(rejected_ids_by_reason.items())
        },
        "rejection_example_ids_sha256_by_reason": {
            reason: hash_identifier_sequence(example_ids)
            for reason, example_ids in sorted(rejected_ids_by_reason.items())
        },
        "protected_example_count": protected_example_count,
        "protected_marker_count": protected_marker_count,
        "protected_span_count": protected_span_count,
        "declared_protection_record_count": declared_protection_record_count,
        "runtime_detector_record_count": runtime_detector_record_count,
        "mapped_protected_occurrence_count": mapped_protected_occurrence_count,
        "deduplicated_source_occurrence_count": (
            deduplicated_source_occurrence_count
        ),
        "transformed_pairs_sha256": sha256_bytes(
            canonical_json_bytes(transformed_identity)
        ),
        "source_target_marker_parity": (
            validated_policy.get("pair_mapping")
            in {"unique_exact_value_v1", "ordered_occurrence_v1"}
        ),
    }
    return ProtectedTrainingRecords(
        records=tuple(accepted),
        accepted_example_ids=tuple(accepted_ids),
        rejected_example_ids=tuple(rejected_ids),
        evidence=evidence,
    )


class ResumableEpochOrderSampler(Sampler[int]):
    """Deterministic full-permutation sampler selected explicitly by epoch."""

    def __init__(self, epoch_orders: Sequence[Sequence[int]]) -> None:
        if not epoch_orders:
            raise ValueError("epoch order sampler requires at least one epoch")
        materialized = tuple(tuple(order) for order in epoch_orders)
        size = len(materialized[0])
        if size <= 0:
            raise ValueError("epoch order sampler cannot be empty")
        expected = set(range(size))
        for epoch, order in enumerate(materialized):
            if len(order) != size or any(
                isinstance(index, bool) or not isinstance(index, int)
                for index in order
            ):
                raise ValueError(
                    f"epoch order {epoch} must contain {size} integer indices"
                )
            if set(order) != expected:
                raise ValueError(
                    f"epoch order {epoch} is not an exact dataset permutation"
                )
        self._orders = materialized
        self._epoch = 0

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def epoch_count(self) -> int:
        return len(self._orders)

    def set_epoch(self, epoch: int) -> None:
        if (
            isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or not 0 <= epoch < len(self._orders)
        ):
            raise ValueError(
                f"sampler epoch must be between 0 and {len(self._orders) - 1}"
            )
        self._epoch = epoch

    def __iter__(self):
        return iter(self._orders[self._epoch])

    def __len__(self) -> int:
        return len(self._orders[0])


def _checked_unique_ids(values: Sequence[str], label: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{label} must contain non-empty string IDs")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicate IDs")
    return result


def _set_mismatch(
    expected: set[str],
    actual: set[str],
    *,
    label: str,
) -> ValueError:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing={missing[0]}")
    if extra:
        details.append(f"extra={extra[0]}")
    return ValueError(f"{label} ID set differs ({', '.join(details)})")


def _bucket_segment(
    example_ids: Sequence[str],
    *,
    lengths: Mapping[str, int],
    window_size: int,
) -> list[str]:
    bucketed: list[str] = []
    for start in range(0, len(example_ids), window_size):
        window = list(example_ids[start : start + window_size])
        positions = {example_id: position for position, example_id in enumerate(window)}
        window.sort(
            key=lambda example_id: (
                -lengths[example_id],
                positions[example_id],
            )
        )
        bucketed.extend(window)
    return bucketed


def _bucket_epoch(
    example_ids: Sequence[str],
    *,
    mode: str,
    phases: Mapping[str, int],
    lengths: Mapping[str, int],
    window_size: int,
) -> list[str]:
    if mode == "shuffled":
        return _bucket_segment(
            example_ids,
            lengths=lengths,
            window_size=window_size,
        )
    if mode != "staged":
        raise ValueError(f"unsupported effective order mode: {mode}")
    result: list[str] = []
    segment: list[str] = []
    previous_phase = 0
    for example_id in example_ids:
        phase = phases[example_id]
        if phase < previous_phase:
            raise ValueError("staged plan phase order regressed after filtering")
        if segment and phase != previous_phase:
            result.extend(
                _bucket_segment(
                    segment,
                    lengths=lengths,
                    window_size=window_size,
                )
            )
            segment = []
        segment.append(example_id)
        previous_phase = phase
    if segment:
        result.extend(
            _bucket_segment(
                segment,
                lengths=lengths,
                window_size=window_size,
            )
        )
    return result


def build_effective_training_order(
    plan: Mapping[str, Any],
    *,
    plan_binding: Mapping[str, Any],
    loaded_example_ids: Sequence[str],
    used_example_ids: Sequence[str],
    rejected_example_ids: Sequence[str],
    sequence_lengths: Sequence[int],
    window_size: int = CURRICULUM_BUCKET_WINDOW,
) -> EffectiveTrainingOrder:
    """Filter one exact plan after tokenization and deterministically bucket it."""

    validated = validate_curriculum_plan(plan)
    if (
        isinstance(window_size, bool)
        or not isinstance(window_size, int)
        or window_size <= 0
    ):
        raise ValueError("curriculum bucket window must be a positive integer")
    loaded = _checked_unique_ids(loaded_example_ids, "loaded training examples")
    used = _checked_unique_ids(used_example_ids, "used training examples")
    rejected = _checked_unique_ids(
        rejected_example_ids,
        "rejected training examples",
    )
    if not used:
        raise ValueError("effective training order requires at least one used example")
    if len(sequence_lengths) != len(used) or any(
        isinstance(length, bool) or not isinstance(length, int) or length <= 0
        for length in sequence_lengths
    ):
        raise ValueError(
            "sequence lengths must contain one positive integer per used example"
        )
    assignments = {
        item["example_id"]: int(item["phase"])
        for item in validated["assignments"]
    }
    planned_set = set(assignments)
    loaded_set = set(loaded)
    if loaded_set != planned_set:
        raise _set_mismatch(
            planned_set,
            loaded_set,
            label="loaded training/plan",
        )
    used_set = set(used)
    rejected_set = set(rejected)
    if used_set & rejected_set:
        overlap = sorted(used_set & rejected_set)[0]
        raise ValueError(
            f"used and rejected training IDs overlap at {overlap}"
        )
    if used_set | rejected_set != planned_set:
        raise _set_mismatch(
            planned_set,
            used_set | rejected_set,
            label="used/rejected training",
        )
    expected_rejected = planned_set - used_set
    if rejected_set != expected_rejected:
        raise _set_mismatch(
            expected_rejected,
            rejected_set,
            label="tokenization rejection",
        )
    index_by_id = {example_id: index for index, example_id in enumerate(used)}
    length_by_id = dict(zip(used, sequence_lengths, strict=True))
    epoch_indices: list[tuple[int, ...]] = []
    epoch_evidence: list[dict[str, Any]] = []
    for expected_epoch, epoch_order in enumerate(validated["epoch_orders"]):
        if epoch_order["epoch"] != expected_epoch:
            raise ValueError("curriculum plan epoch indices are not contiguous")
        filtered = [
            example_id
            for example_id in epoch_order["example_ids"]
            if example_id in used_set
        ]
        if len(filtered) != len(used) or set(filtered) != used_set:
            raise ValueError(
                f"filtered curriculum epoch {expected_epoch} is not an exact used-ID permutation"
            )
        effective = _bucket_epoch(
            filtered,
            mode=validated["mode"],
            phases=assignments,
            lengths=length_by_id,
            window_size=window_size,
        )
        if len(effective) != len(used) or set(effective) != used_set:
            raise ValueError(
                f"bucketed curriculum epoch {expected_epoch} is not an exact used-ID permutation"
            )
        epoch_indices.append(tuple(index_by_id[example_id] for example_id in effective))
        epoch_evidence.append(
            {
                "epoch": expected_epoch,
                "record_count": len(effective),
                "filtered_plan_example_ids_sha256": hash_identifier_sequence(
                    filtered
                ),
                "effective_example_ids_sha256": hash_identifier_sequence(
                    effective
                ),
            }
        )
    binding_sha256 = str(plan_binding.get("sha256", "")).removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", binding_sha256):
        raise ValueError("training order plan binding requires a valid SHA-256")
    binding_path = plan_binding.get("path")
    if not isinstance(binding_path, str) or not binding_path:
        raise ValueError("training order plan binding requires a path")
    used_phase_counts = Counter(assignments[example_id] for example_id in used)
    sorted_rejected = sorted(rejected)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "mode": validated["mode"],
        "plan_path": binding_path,
        "plan_sha256": binding_sha256,
        "algorithm": validated["algorithm"],
        "phase_rules_version": validated["phase_rules_version"],
        "seed": validated["seed"],
        "epochs": validated["epochs"],
        "record_count": validated["source"]["record_count"],
        "used_record_count": len(used),
        "used_example_ids_sha256": hash_identifier_sequence(sorted(used)),
        "rejected_record_count": len(rejected),
        "rejected_example_ids_sha256": hash_identifier_sequence(sorted_rejected),
        "source_sha256": validated["source"]["sha256"],
        "ai_gold_train_sha256": validated["ai_gold_train"]["sha256"],
        "phase_counts": dict(validated["phase_counts"]),
        "effective_phase_counts": {
            str(phase): used_phase_counts[phase] for phase in PHASES
        },
        "bucket_algorithm": CURRICULUM_BUCKET_ALGORITHM,
        "bucket_window": window_size,
        "effective_epoch_orders": epoch_evidence,
        "implicit_trainer_shuffling": False,
    }
    return EffectiveTrainingOrder(
        epoch_indices=tuple(epoch_indices),
        evidence=evidence,
    )


def build_effective_improvement_order(
    plan: Mapping[str, Any],
    *,
    plan_binding: Mapping[str, Any],
    loaded_example_ids: Sequence[str],
    safe_example_ids: Sequence[str],
    policy_rejected_example_ids: Sequence[str],
    policy_rejection_evidence: Mapping[str, Any],
    used_example_ids: Sequence[str],
    sequence_rejected_example_ids: Sequence[str],
    sequence_lengths: Sequence[int],
    window_size: int = CURRICULUM_BUCKET_WINDOW,
) -> EffectiveTrainingOrder:
    """Filter a bound remediation plan and preserve its matched A/B order."""

    from .remediation_experiment import validate_remediation_order_plan

    validated = validate_remediation_order_plan(plan)
    if (
        isinstance(window_size, bool)
        or not isinstance(window_size, int)
        or window_size <= 0
    ):
        raise ValueError("improvement bucket window must be a positive integer")
    loaded = _checked_unique_ids(
        loaded_example_ids,
        "loaded improvement examples",
    )
    safe = _checked_unique_ids(safe_example_ids, "safe improvement examples")
    policy_rejected = _checked_unique_ids(
        policy_rejected_example_ids,
        "policy-rejected improvement examples",
    )
    used = _checked_unique_ids(used_example_ids, "used improvement examples")
    sequence_rejected = _checked_unique_ids(
        sequence_rejected_example_ids,
        "sequence-rejected improvement examples",
    )
    if not used:
        raise ValueError("effective improvement order requires used examples")
    if len(sequence_lengths) != len(used) or any(
        isinstance(length, bool) or not isinstance(length, int) or length <= 0
        for length in sequence_lengths
    ):
        raise ValueError(
            "improvement sequence lengths require one positive value per used example"
        )
    loaded_set = set(loaded)
    safe_set = set(safe)
    policy_rejected_set = set(policy_rejected)
    if safe_set & policy_rejected_set:
        raise ValueError("safe and policy-rejected improvement IDs overlap")
    if safe_set | policy_rejected_set != loaded_set:
        raise _set_mismatch(
            loaded_set,
            safe_set | policy_rejected_set,
            label="improvement safety filter",
        )
    if policy_rejected:
        raise ValueError(
            "selected improvement source must contain only policy-safe examples"
        )
    used_set = set(used)
    sequence_rejected_set = set(sequence_rejected)
    if used_set & sequence_rejected_set:
        raise ValueError("used and sequence-rejected improvement IDs overlap")
    if used_set | sequence_rejected_set != safe_set:
        raise _set_mismatch(
            safe_set,
            used_set | sequence_rejected_set,
            label="improvement length filter",
        )

    source = validated["source"]
    safety = validated["safety_filter"]
    if (
        source["record_count"] != len(loaded)
        or source["selected_safe_example_ids_sha256"]
        != hash_identifier_sequence(safe)
    ):
        raise ValueError("improvement plan source count differs from loaded data")
    if (
        policy_rejection_evidence.get("loaded_record_count") != len(loaded)
        or policy_rejection_evidence.get("accepted_record_count")
        != len(safe)
        or policy_rejection_evidence.get("rejected_record_count") != 0
        or policy_rejection_evidence.get("accepted_example_ids_sha256")
        != hash_identifier_sequence(safe)
        or policy_rejection_evidence.get("rejected_example_ids_sha256")
        != hash_identifier_sequence([])
    ):
        raise ValueError(
            "selected improvement policy evidence differs from its safe source"
        )
    rejected_by_reason = policy_rejection_evidence.get(
        "rejection_example_ids_by_reason"
    )
    rejected_hashes_by_reason = policy_rejection_evidence.get(
        "rejection_example_ids_sha256_by_reason"
    )
    if not isinstance(rejected_by_reason, Mapping) or not isinstance(
        rejected_hashes_by_reason,
        Mapping,
    ):
        raise ValueError(
            "improvement safety evidence lacks per-reason ID bindings"
        )
    for reason, reason_ids in rejected_by_reason.items():
        if (
            not isinstance(reason, str)
            or not isinstance(reason_ids, list)
            or rejected_hashes_by_reason.get(reason)
            != hash_identifier_sequence(reason_ids)
        ):
            raise ValueError(
                "improvement per-reason rejection evidence is inconsistent"
            )
    if rejected_by_reason or rejected_hashes_by_reason:
        raise ValueError(
            "selected improvement policy evidence unexpectedly contains rejections"
        )

    assignments = {
        item["example_id"]: int(item["phase"])
        for item in validated["assignments"]
    }
    if set(assignments) != safe_set:
        raise _set_mismatch(
            safe_set,
            set(assignments),
            label="improvement assignments",
        )
    if len(validated["epoch_orders"]) != 1:
        raise ValueError("improvement order requires exactly one epoch")
    declared_order = list(validated["epoch_orders"][0]["example_ids"])
    if len(declared_order) != len(safe) or set(declared_order) != safe_set:
        raise ValueError(
            "improvement epoch order is not the exact safe-ID permutation"
        )
    filtered = [
        example_id for example_id in declared_order if example_id in used_set
    ]
    if len(filtered) != len(used) or set(filtered) != used_set:
        raise ValueError(
            "filtered improvement order is not the exact used-ID permutation"
        )
    length_by_id = dict(zip(used, sequence_lengths, strict=True))
    mode = str(validated["mode"])
    effective_order_lock = validated.get("effective_order_lock")
    if effective_order_lock is not None:
        if policy_rejected or sequence_rejected or set(filtered) != set(
            effective_order_lock["example_ids"]
        ):
            raise ValueError(
                "frozen effective order requires the exact unrejected source cohort"
            )
        effective = list(effective_order_lock["example_ids"])
        bucket_algorithm = str(effective_order_lock["algorithm"])
        bucket_window: int | None = None
    else:
        effective = _bucket_epoch(
            filtered,
            mode=mode,
            phases=assignments,
            lengths=length_by_id,
            window_size=window_size,
        )
        bucket_algorithm = CURRICULUM_BUCKET_ALGORITHM
        bucket_window = window_size
    if len(effective) != len(used) or set(effective) != used_set:
        raise ValueError(
            "bucketed improvement order is not the exact used-ID permutation"
        )
    index_by_id = {example_id: index for index, example_id in enumerate(used)}
    binding_sha256 = str(plan_binding.get("sha256", "")).removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", binding_sha256):
        raise ValueError(
            "improvement plan binding requires a valid SHA-256"
        )
    binding_path = plan_binding.get("path")
    if not isinstance(binding_path, str) or not binding_path:
        raise ValueError("improvement plan binding requires a path")
    used_phase_counts = Counter(assignments[example_id] for example_id in used)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "kind": validated["kind"],
        "mode": mode,
        "plan_path": binding_path,
        "plan_sha256": binding_sha256,
        "algorithm": validated["algorithm"],
        "phase_rules_version": validated["phase_rules_version"],
        "seed": validated["seed"],
        "epochs": validated["epochs"],
        "comparison_pair_sha256": validated["comparison_pair_sha256"],
        "record_count": len(loaded),
        "safe_record_count": len(safe),
        "safe_example_ids_sha256": hash_identifier_sequence(safe),
        "selected_policy_rejected_record_count": len(policy_rejected),
        "selected_policy_rejected_example_ids_sha256": hash_identifier_sequence(
            policy_rejected
        ),
        "safety_source": dict(validated["safety_source"]),
        "safety_filter": dict(safety),
        "sequence_rejected_record_count": len(sequence_rejected),
        "sequence_rejected_example_ids_sha256": hash_identifier_sequence(
            sequence_rejected
        ),
        "used_record_count": len(used),
        "used_example_ids_sha256": hash_identifier_sequence(used),
        "source_sha256": source["sha256"],
        "protection_policy": dict(validated["protection_policy"]),
        "parent_checkpoint": dict(validated["parent_checkpoint"]),
        "training_contract": dict(validated["training_contract"]),
        "phase_counts": dict(validated["phase_counts"]),
        "effective_phase_counts": {
            str(phase): used_phase_counts[phase] for phase in PHASES
        },
        "bucket_algorithm": bucket_algorithm,
        "bucket_window": bucket_window,
        "effective_epoch_orders": [
            {
                "epoch": 0,
                "record_count": len(effective),
                "filtered_plan_example_ids_sha256": hash_identifier_sequence(
                    filtered
                ),
                "effective_example_ids_sha256": hash_identifier_sequence(
                    effective
                ),
            }
        ],
        "state_initialization": dict(validated["state_initialization"]),
        "implicit_trainer_shuffling": False,
        "full_epoch_required": True,
    }
    if effective_order_lock is not None:
        evidence["effective_order_lock"] = dict(effective_order_lock)
    return EffectiveTrainingOrder(
        epoch_indices=(
            tuple(index_by_id[example_id] for example_id in effective),
        ),
        evidence=evidence,
    )


def assert_configured_example_counts(
    config: Mapping[str, Any],
    *,
    loaded: Mapping[str, int],
    used: Mapping[str, int],
) -> None:
    """Reject exact configured split counts that loading or length filtering changed."""

    for split in ("train", "validation"):
        loaded_count = loaded.get(split)
        used_count = used.get(split)
        key = f"{split}_examples"
        expected = config.get(key)
        if expected is not None:
            if not isinstance(expected, int) or expected <= 0:
                raise ValueError(f"{key} must be a positive integer")
            if loaded_count != expected:
                raise ValueError(
                    f"configured {split} examples requires {expected} loaded examples, got {loaded_count}"
                )
            if used_count != expected:
                raise ValueError(
                    f"configured {split} examples requires {expected} used examples, got {used_count}"
                )
        minimum_key = f"minimum_{split}_examples"
        minimum = config.get(minimum_key)
        if minimum is None:
            continue
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum <= 0:
            raise ValueError(f"{minimum_key} must be a positive integer")
        if loaded_count is None or loaded_count < minimum:
            raise ValueError(
                f"{minimum_key} requires at least {minimum} loaded examples, got {loaded_count}"
            )
        if used_count is None or used_count < minimum:
            raise ValueError(
                f"{minimum_key} requires at least {minimum} used examples, got {used_count}"
            )


def resolve_training_schedule(
    config: Mapping[str, Any],
    *,
    train_examples_used: int,
) -> dict[str, int | float | str | None]:
    """Resolve a reproducible full-pass schedule from epochs or explicit steps."""

    if not isinstance(train_examples_used, int) or train_examples_used <= 0:
        raise ValueError("train_examples_used must be a positive integer")
    configured_steps = config.get("max_steps")
    epochs = config.get("epochs")
    if epochs is not None and (
        isinstance(epochs, bool) or not isinstance(epochs, int | float) or epochs <= 0
    ):
        raise ValueError("epochs must be a positive number")
    if epochs is None and configured_steps is None:
        raise ValueError("epochs must be a positive number when max_steps is absent")
    micro_batch_size = config.get("micro_batch_size")
    gradient_accumulation_steps = config.get("gradient_accumulation_steps")
    if (
        isinstance(micro_batch_size, bool)
        or not isinstance(micro_batch_size, int)
        or micro_batch_size <= 0
    ):
        raise ValueError("micro_batch_size must be a positive integer")
    if (
        isinstance(gradient_accumulation_steps, bool)
        or not isinstance(gradient_accumulation_steps, int)
        or gradient_accumulation_steps <= 0
    ):
        raise ValueError("gradient_accumulation_steps must be a positive integer")
    examples_per_step = micro_batch_size * gradient_accumulation_steps
    if configured_steps is not None:
        if (
            isinstance(configured_steps, bool)
            or not isinstance(configured_steps, int)
            or configured_steps <= 0
        ):
            raise ValueError("max_steps must be a positive integer")
        max_steps = configured_steps
        source = "max_steps"
    else:
        assert isinstance(epochs, int | float)
        max_steps = math.ceil(train_examples_used * float(epochs) / examples_per_step)
        source = "epochs"
    return {
        "max_steps": max_steps,
        "declared_epochs": float(epochs) if epochs is not None else None,
        "effective_examples_per_step": examples_per_step,
        "schedule_source": source,
    }


def resolve_epoch_order_schedule(
    config: Mapping[str, Any],
    *,
    train_examples_used: int,
) -> dict[str, int | float | str | bool | None]:
    """Resolve complete epoch boundaries for an explicit per-epoch order plan."""

    if "max_steps" in config:
        raise ValueError(
            "explicit epoch order requires max_steps to be absent"
        )
    schedule = resolve_training_schedule(
        config,
        train_examples_used=train_examples_used,
    )
    epochs = config.get("epochs")
    if isinstance(epochs, bool) or not isinstance(epochs, int) or epochs <= 0:
        raise ValueError(
            "explicit epoch order requires a positive integer epoch count"
        )
    micro_batch_size = int(config["micro_batch_size"])
    gradient_accumulation_steps = int(config["gradient_accumulation_steps"])
    batches_per_epoch = math.ceil(train_examples_used / micro_batch_size)
    update_steps_per_epoch = math.ceil(
        batches_per_epoch / gradient_accumulation_steps
    )
    return {
        **schedule,
        "max_steps": epochs * update_steps_per_epoch,
        "train_batches_per_epoch": batches_per_epoch,
        "update_steps_per_epoch": update_steps_per_epoch,
        "epoch_boundaries_preserved": True,
    }


def load_pair_records(
    path: str | Path,
    *,
    limit: int | None = None,
    minimal: bool = False,
) -> list[dict[str, Any]]:
    """Load a bounded JSONL pair file and reject malformed or duplicate examples."""

    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.rstrip("\r\n")
            if not line:
                raise ValueError(f"blank JSONL line at {line_number}")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"training record {line_number} must be an object")
            for field in ("example_id", "source_text", "target_sst"):
                field_value = value.get(field)
                if not isinstance(field_value, str) or not field_value:
                    raise ValueError(f"training record {line_number} requires non-empty {field}")
            if value["example_id"] in seen_ids:
                raise ValueError(f"duplicate example_id: {value['example_id']}")
            seen_ids.add(value["example_id"])
            records.append(
                {field: value[field] for field in ("example_id", "source_text", "target_sst")}
                if minimal
                else value
            )
            if limit is not None and len(records) >= limit:
                break
    if not records:
        raise ValueError("training pair file must contain at least one record")
    return records


class CompletionCollator:
    """Right-pad tokenized examples while masking every label padding token."""

    def __init__(self, tokenizer: Any, *, pad_to_multiple_of: int = 8) -> None:
        if not isinstance(pad_to_multiple_of, int) or pad_to_multiple_of <= 0:
            raise ValueError("pad_to_multiple_of must be a positive integer")
        if tokenizer.pad_token_id is None:
            raise ValueError("tokenizer must define pad_token_id")
        self.pad_token_id = int(tokenizer.pad_token_id)
        self.pad_to_multiple_of = pad_to_multiple_of

    def __call__(self, features: Sequence[Mapping[str, Sequence[int]]]) -> dict[str, torch.Tensor]:
        if not features:
            raise ValueError("cannot collate an empty batch")
        lengths = [len(feature["input_ids"]) for feature in features]
        if any(
            len(feature["attention_mask"]) != length or len(feature["labels"]) != length
            for feature, length in zip(features, lengths, strict=True)
        ):
            raise ValueError("input_ids, attention_mask, and labels must have identical lengths")
        longest = max(lengths)
        padded_length = ((longest + self.pad_to_multiple_of - 1) // self.pad_to_multiple_of) * self.pad_to_multiple_of

        def pad(values: Sequence[int], fill: int) -> list[int]:
            return [*values, *([fill] * (padded_length - len(values)))]

        return {
            "input_ids": torch.tensor(
                [pad(feature["input_ids"], self.pad_token_id) for feature in features],
                dtype=torch.long,
            ),
            "attention_mask": torch.tensor(
                [pad(feature["attention_mask"], 0) for feature in features],
                dtype=torch.long,
            ),
            "labels": torch.tensor(
                [pad(feature["labels"], IGNORE_INDEX) for feature in features],
                dtype=torch.long,
            ),
        }
