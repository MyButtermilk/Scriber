"""Deterministic split-first construction of provenance-rich polishing pairs."""

from __future__ import annotations

import copy
import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from .corruption_engine import (
    CorruptionOperation,
    CorruptionProfile,
    CorruptionResult,
    CorruptionSeverity,
    ErrorOrigin,
    ValueMapping,
    compose_corruption_profiles,
    corrupt_with_profile,
    count_exact_value_occurrences,
)
from .schemas import validate_training_pair
from .split_dataset import DatasetExample, Split, assign_document_splits

_DEFAULT_VARIANTS = {
    Split.TRAIN: 12,
    Split.VALIDATION: 5,
    Split.TEST: 5,
    Split.CHALLENGE: 3,
}
_TRAIN_RECIPES: tuple[tuple[str, tuple[CorruptionProfile, ...]], ...] = (
    ("identity", (CorruptionProfile.IDENTITY,)),
    (
        "minimal_punctuation_blank_lines",
        (CorruptionProfile.MINIMAL_PUNCTUATION, CorruptionProfile.BLANK_LINE_ERROR),
    ),
    ("punctuation_case", (CorruptionProfile.PUNCTUATION_CASE,)),
    ("repetition_self_correction", (CorruptionProfile.REPETITION_SELF_CORRECTION,)),
    ("abandoned_start_a", (CorruptionProfile.ABANDONED_START,)),
    ("abandoned_start_b", (CorruptionProfile.ABANDONED_START,)),
    ("heavy_combined_a", (CorruptionProfile.HEAVY_COMBINED,)),
    ("heavy_combined_b", (CorruptionProfile.HEAVY_COMBINED,)),
    ("heavy_combined_c", (CorruptionProfile.HEAVY_COMBINED,)),
    ("spoken_formatting", (CorruptionProfile.SPOKEN_FORMATTING,)),
    (
        "spoken_formatting_heavy",
        (CorruptionProfile.SPOKEN_FORMATTING, CorruptionProfile.HEAVY_COMBINED),
    ),
    (
        "context_spelling_filler_stutter",
        (CorruptionProfile.CONTEXT_SPELLING, CorruptionProfile.FILLER_STUTTER),
    ),
)
_EVAL_RECIPES: tuple[tuple[str, tuple[CorruptionProfile, ...]], ...] = (
    ("identity", (CorruptionProfile.IDENTITY,)),
    ("minimal_punctuation", (CorruptionProfile.MINIMAL_PUNCTUATION,)),
    ("grammar", (CorruptionProfile.GRAMMAR,)),
    ("heavy_combined", (CorruptionProfile.HEAVY_COMBINED,)),
    ("spoken_formatting", (CorruptionProfile.SPOKEN_FORMATTING,)),
    ("abandoned_start", (CorruptionProfile.ABANDONED_START,)),
)
_CHALLENGE_RECIPES: tuple[tuple[str, tuple[CorruptionProfile, ...]], ...] = (
    ("heavy_combined", (CorruptionProfile.HEAVY_COMBINED,)),
    (
        "spoken_formatting_heavy",
        (CorruptionProfile.SPOKEN_FORMATTING, CorruptionProfile.HEAVY_COMBINED),
    ),
    ("abandoned_start", (CorruptionProfile.ABANDONED_START,)),
    ("repetition_self_correction", (CorruptionProfile.REPETITION_SELF_CORRECTION,)),
)
_SEVERITY_RANK = {
    CorruptionSeverity.IDENTITY: 0,
    CorruptionSeverity.CLEAN: 0,
    CorruptionSeverity.LIGHT: 1,
    CorruptionSeverity.MEDIUM: 2,
    CorruptionSeverity.HEAVY: 3,
    CorruptionSeverity.CHALLENGE: 4,
}


def _targeted_train_recipes(
    record: Mapping[str, Any],
) -> tuple[tuple[str, tuple[CorruptionProfile, ...]], ...]:
    tags = set(record.get("risk_tags", ()))
    recipes: list[tuple[str, tuple[CorruptionProfile, ...]]] = []
    if "orthography_pronouns" in tags and record.get("domain") == "general_business":
        recipes.extend(
            (
                ("coverage_house_style_filler", (CorruptionProfile.FILLER_STUTTER,)),
                (
                    "coverage_house_style_abandoned",
                    (CorruptionProfile.ABANDONED_START, CorruptionProfile.FILLER_STUTTER),
                ),
            )
        )
        if record.get("variant_index") in {1, 2}:
            recipes.append(
                (
                    "coverage_house_style_grammar",
                    (CorruptionProfile.GRAMMAR, CorruptionProfile.FILLER_STUTTER),
                )
            )
    if {"das_dass", "seit_seid", "wider_wieder", "ss_sz"}.intersection(tags):
        recipes.extend(
            (
                f"coverage_spelling_contrast_disfluency_{index:02d}",
                (CorruptionProfile.FILLER_STUTTER,),
            )
            for index in range(1, 9)
        )
    if "ambiguous_unit_hard_negative" in tags or ("unit" in tags and record.get("domain") == "hard_negatives"):
        recipes.extend(
            (
                (
                    "coverage_hard_unit_filler",
                    (CorruptionProfile.FILLER_STUTTER, CorruptionProfile.GRAMMAR),
                ),
                (
                    "coverage_hard_unit_abandoned",
                    (CorruptionProfile.ABANDONED_START, CorruptionProfile.FILLER_STUTTER),
                ),
            )
        )
    return tuple(recipes)


def _required_text(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"canonical record requires non-empty {field}")
    return value


def _recipe(split: Split, ordinal: int) -> tuple[str, tuple[CorruptionProfile, ...]]:
    recipes = (
        _CHALLENGE_RECIPES if split is Split.CHALLENGE else _TRAIN_RECIPES if split is Split.TRAIN else _EVAL_RECIPES
    )
    name, profiles = recipes[ordinal % len(recipes)]
    cycle = ordinal // len(recipes)
    return (f"{name}_{cycle + 1}" if cycle else name, profiles)


def _stable_seed(seed: int, canonical_id: str, ordinal: int, attempt: int) -> int:
    digest = hashlib.sha256(f"{seed}:{canonical_id}:{ordinal}:{attempt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFF_FFFF


def _surface_result(record: Mapping[str, Any], text: str, seed: int) -> CorruptionResult:
    if record.get("domain") == "hard_negatives":
        return compose_corruption_profiles(text, (CorruptionProfile.IDENTITY,), seed=seed)
    profiles: list[CorruptionProfile] = []
    tags = set(record.get("risk_tags", ()))
    if "orthography_pronouns" in tags and record.get("domain") == "general_business":
        profiles.append(CorruptionProfile.ORTHOGRAPHY_HOUSE_STYLE)
    for profile in (CorruptionProfile.UNITS_SPOKEN, CorruptionProfile.LEGAL_SPOKEN):
        probe = corrupt_with_profile(text, profile, seed=seed)
        if probe.value_mappings:
            profiles.append(profile)
    return compose_corruption_profiles(
        text,
        tuple(profiles) if profiles else (CorruptionProfile.IDENTITY,),
        seed=seed,
    )


def _merge_results(surface: CorruptionResult, noise: CorruptionResult) -> CorruptionResult:
    operations = (*surface.operations, *noise.operations)
    origins = {operation.origin for operation in operations}
    if not origins:
        origin = ErrorOrigin.FORMATTING_ONLY
    elif len(origins) == 1:
        origin = next(iter(origins))
    else:
        origin = ErrorOrigin.MIXED
    return CorruptionResult(
        text=noise.text,
        severity=max((surface.severity, noise.severity), key=_SEVERITY_RANK.__getitem__),
        error_origin=origin,
        operations=operations,
        protected_spans=noise.protected_spans,
        value_mappings=(*surface.value_mappings, *noise.value_mappings),
    )


def _mapped_protected_values(
    protected_values: Iterable[str],
    mappings: tuple[ValueMapping, ...],
) -> tuple[str, ...]:
    target_to_sources: dict[str, set[str]] = {}
    for mapping in mappings:
        target_to_sources.setdefault(mapping.target, set()).add(mapping.source)
    result: list[str] = []
    for value in protected_values:
        sources = target_to_sources.get(value)
        if sources is None:
            result.append(value)
        elif len(sources) == 1:
            result.append(next(iter(sources)))
        else:
            raise ValueError(f"protected target value has ambiguous source mappings: {value!r}")
    return tuple(result)


def _assert_protected_values(
    protected_values: Iterable[str],
    target: str,
    source: str,
    mappings: tuple[ValueMapping, ...],
) -> None:
    mapping_counts = Counter((mapping.target, mapping.source) for mapping in mappings)
    for value in protected_values:
        expected_count = count_exact_value_occurrences(target, value)
        if expected_count <= 0:
            raise ValueError(f"protected value is absent from canonical target: {value!r}")
        preserved_count = count_exact_value_occurrences(source, value)
        if preserved_count >= expected_count:
            continue
        mapped_count = sum(
            count_exact_value_occurrences(mapping_target, value)
            * min(count_exact_value_occurrences(source, mapping_source), declared_count)
            for (mapping_target, mapping_source), declared_count in mapping_counts.items()
        )
        if preserved_count + mapped_count < expected_count:
            raise ValueError(f"protected target value was not preserved or reversibly mapped: {value!r}")


def _boundaries(target_ast: Mapping[str, Any]) -> dict[str, Any]:
    blocks = target_ast.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("canonical target_ast requires a blocks list")
    types = [block.get("type") for block in blocks]
    return {
        "formatting_decisions": list(types),
        "paragraph_boundaries": [index for index, value in enumerate(types) if value == "paragraph"],
        "heading_boundaries": [index for index, value in enumerate(types) if value in {"heading_1", "heading_2"}],
        "list_boundaries": [index for index, value in enumerate(types) if value in {"ordered_list", "unordered_list"}],
        "salutation_boundary": next(
            (index for index, value in enumerate(types) if value == "salutation"),
            None,
        ),
        "closing_boundary": next(
            (index for index, value in enumerate(types) if value == "closing"),
            None,
        ),
        "signature_boundaries": [index for index, value in enumerate(types) if value == "signature"],
    }


def _build_pair_payload(
    record: Mapping[str, Any],
    *,
    split: Split,
    ordinal: int,
    recipe_name: str,
    profiles: tuple[CorruptionProfile, ...],
    seed: int,
) -> dict[str, Any]:
    target = _required_text(record, "target_plain_text")
    protected_values = tuple(str(value) for value in record.get("protected_spans", ()))
    surface = _surface_result(record, target, seed)
    noise = compose_corruption_profiles(
        surface.text,
        profiles,
        seed=seed + 101,
        protected_values=_mapped_protected_values(protected_values, surface.value_mappings),
    )
    corruption = _merge_results(surface, noise)
    _assert_protected_values(protected_values, target, corruption.text, corruption.value_mappings)
    canonical_id = _required_text(record, "canonical_document_id")
    parent_id = _required_text(record, "parent_canonical_document_id")
    target_ast = record.get("target_ast")
    if not isinstance(target_ast, Mapping):
        raise ValueError("canonical target_ast must be an object")
    boundary_fields = _boundaries(target_ast)
    operations: tuple[CorruptionOperation, ...] = corruption.operations
    formatting_commands = [operation.kind for operation in operations if operation.kind == "spoken_formatting"]
    return {
            "schema_version": 1,
            "example_id": f"{canonical_id}__pair_{ordinal + 1:02d}",
            "canonical_document_id": canonical_id,
            "parent_canonical_document_id": parent_id,
            "seed_id": _required_text(record, "seed_id"),
            "split": split.value,
            "domain": record.get("domain"),
            "document_type": record.get("document_type"),
            "language": record.get("language"),
            "address_mode": record.get("address_mode"),
            "risk_tags": copy.deepcopy(record.get("risk_tags", [])),
            "source_text": corruption.text,
            "target_sst": _required_text(record, "target_sst"),
            "target_plain_text": target,
            "target_markdown": _required_text(record, "target_markdown"),
            "target_html": _required_text(record, "target_html"),
            "target_ast": copy.deepcopy(target_ast),
            "semantic_plan": copy.deepcopy(record.get("semantic_plan")),
            **boundary_fields,
            "formatting_commands": formatting_commands,
            "protected_spans": list(protected_values),
            "source_protected_spans": [span.value for span in corruption.protected_spans],
            "corruption_profile": recipe_name,
            "severity": corruption.severity.value,
            "error_origin": corruption.error_origin.value,
            "operations": [operation.kind for operation in operations],
            "value_mappings": [
                {"kind": mapping.kind, "source": mapping.source, "target": mapping.target}
                for mapping in corruption.value_mappings
            ],
            "applied_normalizations": copy.deepcopy(record.get("applied_normalizations", [])),
            "ambiguity_flags": copy.deepcopy(record.get("ambiguity_flags", [])),
            "validation_scores": {"deterministic": 1.0},
            "provenance": {
                "seed_id": record["seed_id"],
                "source_plan_batch": record.get("source_plan_batch"),
                "plan_generator_agent": record.get("plan_generator_agent"),
                "target_writer_agent": record.get("target_writer_agent"),
                "canonical_generator_id": record.get("canonical_generator_id"),
                "canonical_generator_version": record.get("canonical_generator_version"),
                "variant_index": record.get("variant_index"),
                "synthetic_only": True,
                "human_curation": False,
                "generative_api": False,
            },
        }


def build_review_candidate_pair(
    record: Mapping[str, Any],
    *,
    split: Split,
    ordinal: int,
    recipe_name: str,
    profiles: tuple[CorruptionProfile, ...],
    seed: int,
) -> dict[str, Any]:
    """Build an unapproved pair candidate without any critic assertions."""

    return _build_pair_payload(
        record,
        split=split,
        ordinal=ordinal,
        recipe_name=recipe_name,
        profiles=profiles,
        seed=seed,
    )


def _build_pair(
    record: Mapping[str, Any],
    *,
    split: Split,
    ordinal: int,
    recipe_name: str,
    profiles: tuple[CorruptionProfile, ...],
    seed: int,
) -> dict[str, Any]:
    pair = _build_pair_payload(
        record,
        split=split,
        ordinal=ordinal,
        recipe_name=recipe_name,
        profiles=profiles,
        seed=seed,
    )
    pair.update(
        {
            "critic_agents": copy.deepcopy(record.get("critic_agents", [])),
            "critic_decisions": copy.deepcopy(record.get("critic_decisions", [])),
            "reviewed_package_sha256": _required_text(record, "reviewed_package_sha256"),
        }
    )
    return validate_training_pair(pair)


def build_dataset_pairs(
    canonical_records: Iterable[Mapping[str, Any]],
    *,
    seed: int,
    variants_per_canonical: Mapping[Split, int] | None = None,
) -> dict[Split, tuple[dict[str, Any], ...]]:
    """Split parent documents first, then generate deterministic local corruptions."""

    if not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    records = tuple(sorted(canonical_records, key=lambda item: _required_text(item, "canonical_document_id")))
    if not records:
        raise ValueError("at least one canonical record is required")
    canonical_ids = [_required_text(record, "canonical_document_id") for record in records]
    if len(canonical_ids) != len(set(canonical_ids)):
        raise ValueError("canonical_document_id values must be unique")
    include_targeted_train_recipes = variants_per_canonical is None
    counts = dict(_DEFAULT_VARIANTS if variants_per_canonical is None else variants_per_canonical)
    if set(counts) != set(Split) or any(not isinstance(value, int) or value <= 0 for value in counts.values()):
        raise ValueError("variants_per_canonical must contain every split with positive integer counts")

    split_assignments = assign_document_splits(
        (
            DatasetExample(
                _required_text(record, "canonical_document_id"),
                _required_text(record, "parent_canonical_document_id"),
            )
            for record in records
        ),
        seed=seed,
    )
    result: dict[Split, list[dict[str, Any]]] = {split: [] for split in Split}
    for record in records:
        canonical_id = _required_text(record, "canonical_document_id")
        split = split_assignments[canonical_id]
        sources: set[str] = set()
        recipes = [_recipe(split, ordinal) for ordinal in range(counts[split])]
        if include_targeted_train_recipes and split is Split.TRAIN:
            recipes.extend(_targeted_train_recipes(record))
        for ordinal, (recipe_name, profiles) in enumerate(recipes):
            pair: dict[str, Any] | None = None
            for attempt in range(20):
                pair = _build_pair(
                    record,
                    split=split,
                    ordinal=ordinal,
                    recipe_name=recipe_name,
                    profiles=profiles,
                    seed=_stable_seed(seed, canonical_id, ordinal, attempt),
                )
                if pair["source_text"] not in sources:
                    break
            if pair is None or pair["source_text"] in sources:
                raise ValueError(f"could not generate distinct source variants for {canonical_id}")
            sources.add(pair["source_text"])
            result[split].append(pair)
    return {split: tuple(sorted(rows, key=lambda item: item["example_id"])) for split, rows in result.items()}
