"""Deterministic, AI-Gold-isolated selection for the regular synthetic test set."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SELECTION_ALGORITHM = "domain_profile_parent_round_robin_sha256_v1"
REFERENCE_VALIDATOR = "scriber_polishing.evaluation_io.validate_evaluation_reference"
_PROJECT_ROOT = Path(__file__).parents[2]
_MANIFEST_SCHEMA = _PROJECT_ROOT / "contracts" / "regular_test_selection_manifest_schema.json"


@dataclass(frozen=True, slots=True)
class RegularTestSelection:
    """Selected source rows plus an aggregate-only isolation and coverage audit."""

    records: tuple[dict[str, Any], ...]
    audit: dict[str, Any]


@cache
def _manifest_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8")))


def _required_text(record: Mapping[str, Any], field: str, label: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} requires non-empty {field}")
    return value


def _rank(seed: int, scope: str, value: str) -> bytes:
    return hashlib.sha256(f"regular-test-selection-v1:{seed}:{scope}:{value}".encode()).digest()


def _balanced_targets(values: Sequence[str], total: int, *, seed: int, scope: str) -> dict[str, int]:
    if not values:
        raise ValueError(f"regular test selection has no {scope} values")
    base, remainder = divmod(total, len(values))
    ranked = sorted(values, key=lambda value: (_rank(seed, scope, value), value))
    extras = set(ranked[:remainder])
    return {value: base + int(value in extras) for value in values}


def _balanced_cell_targets(
    domains: Sequence[str],
    profiles: Sequence[str],
    total: int,
    *,
    seed: int,
) -> dict[tuple[str, str], int]:
    domain_targets = _balanced_targets(domains, total, seed=seed, scope="domain")
    profile_targets = _balanced_targets(profiles, total, seed=seed, scope="profile")
    base = total // (len(domains) * len(profiles))
    result = {(domain, profile): base for domain in domains for profile in profiles}
    domain_remaining = {domain: domain_targets[domain] - (base * len(profiles)) for domain in domains}
    profile_remaining = {profile: profile_targets[profile] - (base * len(domains)) for profile in profiles}
    domain_order = sorted(
        domains,
        key=lambda domain: (_rank(seed, "cell-domain", domain), domain),
    )

    def assign(domain_index: int) -> bool:
        if domain_index == len(domain_order):
            return not any(profile_remaining.values())
        domain = domain_order[domain_index]
        required = domain_remaining[domain]
        available = [profile for profile in profiles if profile_remaining[profile] > 0]
        combinations = list(itertools.combinations(available, required))
        combinations.sort(
            key=lambda selected: (
                _rank(seed, f"cell:{domain}", "\0".join(selected)),
                selected,
            )
        )
        for selected in combinations:
            for profile in selected:
                profile_remaining[profile] -= 1
                result[(domain, profile)] += 1
            if assign(domain_index + 1):
                return True
            for profile in selected:
                profile_remaining[profile] += 1
                result[(domain, profile)] -= 1
        return False

    if not assign(0):
        raise ValueError("unable to construct balanced domain/profile quotas")
    return result


def _parent_round_robin(
    rows: Sequence[Mapping[str, Any]],
    *,
    count: int,
    seed: int,
    scope: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_required_text(row, "parent_canonical_document_id", "test source record")].append(row)
    parents = sorted(
        grouped,
        key=lambda parent: (_rank(seed, f"{scope}:parent", parent), parent),
    )
    for parent, members in grouped.items():
        members.sort(
            key=lambda row: (
                _rank(
                    seed,
                    f"{scope}:case:{parent}",
                    _required_text(row, "example_id", "test source record"),
                ),
                _required_text(row, "example_id", "test source record"),
            )
        )
    selected: list[dict[str, Any]] = []
    pass_index = 0
    while len(selected) < count:
        added = False
        for parent in parents:
            members = grouped[parent]
            if pass_index >= len(members):
                continue
            selected.append(dict(members[pass_index]))
            added = True
            if len(selected) == count:
                break
        if not added:
            raise ValueError(f"selection stratum {scope!r} has fewer than {count} records")
        pass_index += 1
    return selected


def _indexed_source(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in records:
        if row.get("split") != "test":
            raise ValueError("regular test source may contain only split='test' records")
        example_id = _required_text(row, "example_id", "test source record")
        _required_text(row, "parent_canonical_document_id", "test source record")
        _required_text(row, "domain", "test source record")
        _required_text(row, "corruption_profile", "test source record")
        if example_id in indexed:
            raise ValueError(f"regular test source contains duplicate example_id: {example_id}")
        indexed[example_id] = row
    if not indexed:
        raise ValueError("regular test source is empty")
    return indexed


def select_regular_test_records(
    records: Sequence[Mapping[str, Any]],
    ai_gold_test_records: Sequence[Mapping[str, Any]],
    *,
    target_count: int,
    seed: int,
) -> RegularTestSelection:
    """Select a balanced holdout while excluding every AI-Gold test ID and, when possible, parent."""

    if isinstance(target_count, bool) or not isinstance(target_count, int) or target_count <= 0:
        raise ValueError("target_count must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    source_by_id = _indexed_source(records)
    gold_by_id: dict[str, Mapping[str, Any]] = {}
    gold_parents: set[str] = set()
    for row in ai_gold_test_records:
        example_id = _required_text(row, "example_id", "AI-Gold test record")
        parent = _required_text(row, "parent_canonical_document_id", "AI-Gold test record")
        if example_id in gold_by_id:
            raise ValueError(f"AI-Gold test records contain duplicate example_id: {example_id}")
        gold_by_id[example_id] = row
        gold_parents.add(parent)
    if not gold_by_id:
        raise ValueError("AI-Gold test exclusion is empty")
    missing = sorted(set(gold_by_id).difference(source_by_id))
    if missing:
        raise ValueError(f"AI-Gold test example_id is absent from source test split: {missing[0]}")
    for example_id, gold_row in gold_by_id.items():
        source_parent = _required_text(
            source_by_id[example_id],
            "parent_canonical_document_id",
            "test source record",
        )
        gold_parent = _required_text(
            gold_row,
            "parent_canonical_document_id",
            "AI-Gold test record",
        )
        if source_parent != gold_parent:
            raise ValueError(f"AI-Gold parent binding differs in source for {example_id}")

    after_id = [row for example_id, row in source_by_id.items() if example_id not in gold_by_id]
    after_parent = [
        row
        for row in after_id
        if _required_text(row, "parent_canonical_document_id", "test source record") not in gold_parents
    ]
    if len(after_id) < target_count:
        raise ValueError(f"only {len(after_id)} records remain after AI-Gold ID exclusion; {target_count} required")
    if len(after_parent) >= target_count:
        pool = after_parent
        parent_exclusion_mode = "full"
    else:
        pool = after_id
        parent_exclusion_mode = "best_effort"

    domains = sorted({_required_text(row, "domain", "test source record") for row in pool})
    profiles = sorted({_required_text(row, "corruption_profile", "test source record") for row in pool})
    cell_targets = _balanced_cell_targets(
        domains,
        profiles,
        target_count,
        seed=seed,
    )
    cells: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in pool:
        cells[
            (
                _required_text(row, "domain", "test source record"),
                _required_text(row, "corruption_profile", "test source record"),
            )
        ].append(row)
    for cell, required in cell_targets.items():
        available = len(cells[cell])
        if available < required:
            raise ValueError(
                f"domain/profile stratum {cell[0]!r}/{cell[1]!r} has {available} records; {required} required"
            )

    selected: list[dict[str, Any]] = []
    for (domain, profile), count in sorted(cell_targets.items()):
        if count:
            selected.extend(
                _parent_round_robin(
                    cells[(domain, profile)],
                    count=count,
                    seed=seed,
                    scope=f"{domain}\0{profile}",
                )
            )
    selected.sort(key=lambda row: _required_text(row, "example_id", "test source record"))
    selected_ids = {_required_text(row, "example_id", "test source record") for row in selected}
    selected_parents = {_required_text(row, "parent_canonical_document_id", "test source record") for row in selected}
    domain_counts = Counter(_required_text(row, "domain", "test source record") for row in selected)
    profile_counts = Counter(_required_text(row, "corruption_profile", "test source record") for row in selected)
    cell_counts = Counter(
        (
            _required_text(row, "domain", "test source record"),
            _required_text(row, "corruption_profile", "test source record"),
        )
        for row in selected
    )
    operation_counts: Counter[str] = Counter()
    for row in selected:
        operations = row.get("operations", ())
        if not isinstance(operations, list | tuple):
            raise ValueError("test source record operations must be an array")
        for operation in operations:
            if not isinstance(operation, str) or not operation:
                raise ValueError("test source record operations must contain non-empty strings")
            operation_counts[operation] += 1
    if not operation_counts:
        operation_counts["none"] = len(selected)
    audit = {
        "source_record_count": len(source_by_id),
        "ai_gold_test_count": len(gold_by_id),
        "ai_gold_ids_matched_in_source_count": len(gold_by_id),
        "ai_gold_parent_group_count": len(gold_parents),
        "eligible_after_id_exclusion": len(after_id),
        "eligible_after_parent_exclusion": len(after_parent),
        "parent_exclusion_mode": parent_exclusion_mode,
        "selected_ai_gold_id_overlap_count": len(selected_ids.intersection(gold_by_id)),
        "selected_ai_gold_parent_overlap_count": len(selected_parents.intersection(gold_parents)),
        "unique_parent_count": len(selected_parents),
        "domain_counts": dict(sorted(domain_counts.items())),
        "corruption_profile_counts": dict(sorted(profile_counts.items())),
        "domain_profile_counts": {
            f"{domain}|{profile}": cell_counts[(domain, profile)] for domain, profile in sorted(cell_counts)
        },
        "operation_counts": dict(sorted(operation_counts.items())),
        "domain_balance_delta": max(domain_counts.values()) - min(domain_counts.values()),
        "corruption_profile_balance_delta": (max(profile_counts.values()) - min(profile_counts.values())),
    }
    if len(selected) != target_count:
        raise ValueError(f"regular test selection produced {len(selected)} records; {target_count} required")
    if audit["selected_ai_gold_id_overlap_count"] != 0:
        raise ValueError("regular test selection overlaps AI-Gold test IDs")
    if parent_exclusion_mode == "full" and audit["selected_ai_gold_parent_overlap_count"] != 0:
        raise ValueError("regular test selection overlaps excluded AI-Gold parent groups")
    return RegularTestSelection(records=tuple(selected), audit=audit)


def validate_regular_test_selection_manifest(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the closed manifest schema and its cross-field count invariants."""

    materialized = dict(manifest)
    errors = sorted(
        _manifest_validator().iter_errors(materialized),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"regular test selection manifest failed schema: {errors[0].message}")
    target = materialized["target_count"]
    output = materialized["output"]
    coverage = materialized["coverage"]
    exclusion = materialized["exclusion"]
    validation = materialized["validation"]
    if output["record_count"] != target or output["split_counts"] != {"test": target}:
        raise ValueError("regular test selection manifest output count differs from target")
    if validation["validated_record_count"] != target:
        raise ValueError("regular test selection validated count differs from target")
    for field in (
        "domain_counts",
        "corruption_profile_counts",
        "domain_profile_counts",
    ):
        if sum(coverage[field].values()) != target:
            raise ValueError(f"regular test selection {field} total differs from target")
    domain_values = coverage["domain_counts"].values()
    profile_values = coverage["corruption_profile_counts"].values()
    if coverage["domain_balance_delta"] != max(domain_values) - min(domain_values):
        raise ValueError("regular test selection domain balance delta is inconsistent")
    if coverage["corruption_profile_balance_delta"] != max(profile_values) - min(profile_values):
        raise ValueError("regular test selection profile balance delta is inconsistent")
    if exclusion["ai_gold_ids_matched_in_source_count"] != exclusion["ai_gold_test_count"]:
        raise ValueError("regular test selection did not match every AI-Gold test ID")
    if exclusion["selected_ai_gold_id_overlap_count"] != 0:
        raise ValueError("regular test selection overlaps AI-Gold test IDs")
    if exclusion["parent_exclusion_mode"] == "full" and exclusion["selected_ai_gold_parent_overlap_count"] != 0:
        raise ValueError("regular test selection overlaps fully excluded AI-Gold parent groups")
    return materialized
