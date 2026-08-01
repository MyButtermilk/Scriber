"""AI-Gold selection: all required evidence must be present and agree."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ast_codec import document_from_dict
from .critic_aggregation import (
    CriticVerdict,
    ExampleRisk,
    ReleaseDecision,
    aggregate_critic_verdicts,
)
from .deduplicate import audit_deduplication
from .protected_spans import protect_spans
from .schemas import validate_ai_gold_manifest, validate_semantic_plan, validate_training_pair
from .split_dataset import DatasetExample, Split, validate_no_split_leakage
from .sst_parser import SSTParseError, parse_sst
from .sst_renderer import render_sst
from .target_renderer import render_html, render_markdown, render_plain_text
from .validators import validate_canonical_record

DEFAULT_AI_GOLD_SPLIT_TARGETS = {
    Split.TRAIN: 1_500,
    Split.VALIDATION: 750,
    Split.TEST: 1_000,
    Split.CHALLENGE: 750,
}


@dataclass(frozen=True, slots=True)
class GoldBuildResult:
    accepted: tuple[Mapping[str, Any], ...]
    rejected: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class GoldFreezeResult:
    """Aggregate-only freeze result; individual records remain in optional sealed files."""

    manifest: dict[str, Any]
    manifest_sha256: str
    manifest_path: Path | None
    checksum_path: Path | None
    sealed_files: dict[Split, Path]


def _validate_critic_evidence(candidate: Mapping[str, Any]) -> None:
    decisions = candidate.get("critic_decisions")
    if not isinstance(decisions, list) or not all(isinstance(item, Mapping) for item in decisions):
        raise ValueError("AI-Gold candidate requires structured critic decisions")
    critic_ids: list[str] = []
    role_counts: Counter[str] = Counter()
    for decision in decisions:
        critic_id = decision.get("critic_id")
        role = decision.get("critic_role")
        if not isinstance(critic_id, str) or not critic_id:
            raise ValueError("AI-Gold candidate has an invalid critic identity")
        if critic_id in critic_ids:
            raise ValueError("AI-Gold candidate requires independent critic identities")
        critic_ids.append(critic_id)
        if role not in {"fact", "style", "adversarial"}:
            raise ValueError("AI-Gold candidate has an invalid critic role")
        role_counts[str(role)] += 1
        if decision.get("acceptable") is not True:
            raise ValueError("AI-Gold candidate has a critic rejection")
        critical_flags = decision.get("critical_flags", ())
        if not isinstance(critical_flags, list | tuple) or critical_flags:
            raise ValueError("AI-Gold candidate has critical critic flags")
    if role_counts["fact"] < 1 or role_counts["style"] < 1 or role_counts["adversarial"] < 2:
        raise ValueError(
            "AI-Gold candidate requires a fact critic, a style critic, and two independent adversarial critics"
        )
    declared_agents = candidate.get("critic_agents")
    if not isinstance(declared_agents, list) or set(declared_agents) != set(critic_ids):
        raise ValueError("AI-Gold critic agent identities differ from critic decisions")
    provenance = candidate.get("provenance")
    generator_source = provenance if isinstance(provenance, Mapping) else candidate
    generator_ids = {
        generator_source.get("plan_generator_agent"),
        generator_source.get("target_writer_agent"),
        generator_source.get("canonical_generator_id"),
    }
    if generator_ids.intersection(critic_ids):
        raise ValueError("AI-Gold critics must be independent from generator agents")
    package_hash = candidate.get("reviewed_package_sha256")
    if (
        not isinstance(package_hash, str)
        or len(package_hash) != 64
        or any(character not in "0123456789abcdef" for character in package_hash)
    ):
        raise ValueError("AI-Gold candidate requires a critic-reviewed package hash")


def _validate_protected_spans(candidate: Mapping[str, Any]) -> None:
    source = candidate.get("source_text")
    target = candidate.get("target_plain_text")
    spans = candidate.get("protected_spans")
    if not isinstance(source, str) or not isinstance(target, str) or not isinstance(spans, list):
        raise ValueError("AI-Gold candidate has invalid protected span evidence")
    target_to_sources: dict[str, list[str]] = defaultdict(list)
    mappings = candidate.get("value_mappings", [])
    if not isinstance(mappings, list):
        raise ValueError("AI-Gold candidate has invalid protected value mappings")
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            raise ValueError("AI-Gold candidate has invalid protected value mappings")
        mapping_target = mapping.get("target")
        mapping_source = mapping.get("source")
        if isinstance(mapping_target, str) and isinstance(mapping_source, str):
            target_to_sources[mapping_target].append(mapping_source)
    for value in spans:
        if not isinstance(value, str) or not value:
            raise ValueError("AI-Gold candidate has an invalid protected span")
        target_count = target.count(value)
        if target_count <= 0:
            raise ValueError(f"AI-Gold protected span was not preserved: {value!r}")
        if source.count(value) == target_count:
            continue
        mapped_sources = target_to_sources.get(value, [])
        if len(mapped_sources) != target_count:
            raise ValueError(f"AI-Gold protected span was not reversibly preserved: {value!r}")
        source_counts = Counter(mapped_sources)
        if any(source.count(mapped_value) != count for mapped_value, count in source_counts.items()):
            raise ValueError(f"AI-Gold protected span was not reversibly preserved: {value!r}")


def validate_ai_gold_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Require complete canonical, deterministic, critic, and span evidence."""

    semantic_plan = candidate.get("semantic_plan")
    if not isinstance(semantic_plan, Mapping):
        raise ValueError("AI-Gold candidate requires a semantic plan")
    validate_semantic_plan(semantic_plan)
    is_training_pair = "provenance" in candidate or "split" in candidate
    if is_training_pair:
        first = validate_training_pair(candidate)
        second = validate_training_pair(candidate)
        provenance = candidate.get("provenance")
        if not isinstance(provenance, Mapping) or (
            provenance.get("synthetic_only") is not True
            or provenance.get("human_curation") is not False
            or provenance.get("generative_api") is not False
        ):
            raise ValueError("AI-Gold training pair requires AI-only provenance")
    else:
        first = validate_canonical_record(candidate)
        second = validate_canonical_record(candidate)
    if first != second:
        raise ValueError("AI-Gold deterministic validators disagree")

    document = document_from_dict(candidate["target_ast"])
    parsed = parse_sst(candidate["target_sst"])
    expected_outputs = (
        ("SST", render_sst(document), candidate["target_sst"]),
        ("plain text", render_plain_text(document), candidate["target_plain_text"]),
        ("Markdown", render_markdown(document), candidate["target_markdown"]),
        ("HTML", render_html(document), candidate["target_html"]),
    )
    for label, rendered, expected in expected_outputs:
        if rendered != expected:
            raise ValueError(f"AI-Gold deterministic {label} rendering differs")
    if (
        render_sst(parsed) != candidate["target_sst"]
        or render_plain_text(parsed) != candidate["target_plain_text"]
        or render_markdown(parsed) != candidate["target_markdown"]
        or render_html(parsed) != candidate["target_html"]
    ):
        raise ValueError("AI-Gold SST round-trip failed")

    ambiguity_flags = candidate.get("ambiguity_flags")
    if not isinstance(ambiguity_flags, list) or ambiguity_flags:
        raise ValueError("AI-Gold candidate contains unresolved ambiguity")
    _validate_protected_spans(candidate)
    _validate_critic_evidence(candidate)
    return first


def _candidate_identity(candidate: Mapping[str, Any]) -> str:
    field = "example_id" if "example_id" in candidate else "canonical_document_id"
    value = candidate.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"AI-Gold candidate requires non-empty {field}")
    return value


def _parent_identity(candidate: Mapping[str, Any]) -> str:
    value = candidate.get("parent_canonical_document_id")
    if not isinstance(value, str) or not value:
        raise ValueError("AI-Gold candidate requires parent_canonical_document_id")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rank(seed: int, namespace: str, identity: str) -> bytes:
    return hashlib.sha256(f"ai-gold-v1:{seed}:{namespace}:{identity}".encode()).digest()


def _normalized_text(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", unicodedata.normalize("NFKC", text).casefold()).split())


def _collapse_normalized_source_duplicates(
    candidates: tuple[dict[str, Any], ...],
    *,
    seed: int,
) -> tuple[tuple[dict[str, Any], ...], int]:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identities: set[str] = set()
    for candidate in candidates:
        identity = _candidate_identity(candidate)
        if identity in identities:
            raise ValueError("AI-Gold candidate identities must be unique")
        identities.add(identity)
        by_source[_normalized_text(str(candidate["source_text"]))].append(candidate)

    retained = [
        min(
            duplicates,
            key=lambda candidate: (
                _rank(seed, "normalized-source", _candidate_identity(candidate)),
                _candidate_identity(candidate),
            ),
        )
        for duplicates in by_source.values()
    ]
    return (
        tuple(sorted(retained, key=lambda candidate: _candidate_identity(candidate))),
        len(candidates) - len(retained),
    )


def _collapse_near_duplicate_parent_families(
    candidates: tuple[dict[str, Any], ...],
    *,
    seed: int,
) -> tuple[tuple[dict[str, Any], ...], int]:
    by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_parent[_parent_identity(candidate)].append(candidate)

    representative_rows = [
        {
            "example_id": parent,
            "canonical_document_id": parent,
            "source_text": min(
                records,
                key=lambda candidate: _candidate_identity(candidate),
            )["target_plain_text"],
        }
        for parent, records in sorted(by_parent.items())
    ]
    report = audit_deduplication(representative_rows, near_duplicate_threshold=0.82)
    parents = {parent: parent for parent in by_parent}

    def find(parent: str) -> str:
        while parents[parent] != parent:
            parents[parent] = parents[parents[parent]]
            parent = parents[parent]
        return parent

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for group in (
        *report.exact_groups,
        *report.normalized_groups,
        *report.near_duplicate_groups,
    ):
        first, *rest = group
        for other in rest:
            union(first, other)

    components: dict[str, list[str]] = defaultdict(list)
    for parent in sorted(by_parent):
        components[find(parent)].append(parent)
    retained_parents = {
        min(
            component,
            key=lambda parent: (
                _rank(seed, "family-parent", parent),
                parent,
            ),
        )
        for component in components.values()
    }
    retained = tuple(candidate for candidate in candidates if _parent_identity(candidate) in retained_parents)
    collapsed_family_count = sum(1 for component in components.values() if len(component) > 1)
    return retained, collapsed_family_count


def _normalized_targets(
    split_targets: Mapping[Split | str, int] | None,
) -> dict[Split, int]:
    source = DEFAULT_AI_GOLD_SPLIT_TARGETS if split_targets is None else split_targets
    normalized: dict[Split, int] = {}
    for raw_split, count in source.items():
        split = Split(raw_split)
        if split in normalized:
            raise ValueError(f"duplicate AI-Gold split target: {split.value}")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("AI-Gold split targets must be positive integers")
        normalized[split] = count
    if set(normalized) != set(Split):
        raise ValueError("AI-Gold split targets must contain train, validation, test, and challenge")
    if sum(normalized.values()) < 4_000:
        raise ValueError("AI-Verified Gold Set requires at least 4,000 records")
    return normalized


def _validate_frozen_split_integrity(
    candidates: tuple[dict[str, Any], ...],
) -> None:
    has_split = ["split" in candidate for candidate in candidates]
    if any(has_split) and not all(has_split):
        raise ValueError("AI-Gold candidates must either all carry frozen splits or all be unsplit")
    if not all(has_split):
        return
    parent_splits: dict[str, set[Split]] = defaultdict(set)
    for candidate in candidates:
        parent_splits[_parent_identity(candidate)].add(Split(candidate["split"]))
    if any(len(splits) > 1 for splits in parent_splits.values()):
        raise ValueError("AI-Gold input already contains parent split leakage")


def _select_from_parent_pool(
    parents: list[tuple[str, tuple[dict[str, Any], ...]]],
    *,
    split: Split,
    target: int,
    seed: int,
) -> tuple[list[dict[str, Any]], list[tuple[str, tuple[dict[str, Any], ...]]]]:
    selected: list[dict[str, Any]] = []
    remaining_parents = list(parents)
    while len(selected) < target and remaining_parents:
        parent, records = remaining_parents.pop(0)
        ordered = sorted(
            records,
            key=lambda item: (
                _rank(seed, f"{split.value}:{parent}", _candidate_identity(item)),
                _candidate_identity(item),
            ),
        )
        selected.extend(ordered[: target - len(selected)])
    if len(selected) != target:
        raise ValueError(
            f"insufficient approved AI-Gold candidates for {split.value}: required {target}, found {len(selected)}"
        )
    return selected, remaining_parents


def _select_gold_records(
    candidates: tuple[dict[str, Any], ...],
    *,
    seed: int,
    targets: Mapping[Split, int],
) -> dict[Split, tuple[dict[str, Any], ...]]:
    by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_parent[_parent_identity(candidate)].append(candidate)

    has_split = ["split" in candidate for candidate in candidates]
    if any(has_split) and not all(has_split):
        raise ValueError("AI-Gold candidates must either all carry frozen splits or all be unsplit")
    result: dict[Split, tuple[dict[str, Any], ...]] = {}
    if all(has_split):
        split_parents: dict[Split, list[tuple[str, tuple[dict[str, Any], ...]]]] = {split: [] for split in Split}
        for parent, records in by_parent.items():
            parent_splits = {Split(record["split"]) for record in records}
            if len(parent_splits) != 1:
                raise ValueError("AI-Gold input already contains parent split leakage")
            split = next(iter(parent_splits))
            split_parents[split].append((parent, tuple(records)))
        for split in Split:
            pool = sorted(
                split_parents[split],
                key=lambda item: (_rank(seed, split.value, item[0]), item[0]),
            )
            selected, _ = _select_from_parent_pool(
                pool,
                split=split,
                target=targets[split],
                seed=seed,
            )
            result[split] = tuple(sorted(selected, key=lambda item: _candidate_identity(item)))
    else:
        pool = sorted(
            ((parent, tuple(records)) for parent, records in by_parent.items()),
            key=lambda item: (_rank(seed, "parent", item[0]), item[0]),
        )
        for split in Split:
            selected, pool = _select_from_parent_pool(
                pool,
                split=split,
                target=targets[split],
                seed=seed,
            )
            result[split] = tuple(sorted(selected, key=lambda item: _candidate_identity(item)))
    return result


def _audit_selected_gold(selected: Mapping[Split, tuple[dict[str, Any], ...]]) -> None:
    seen_normalized_sources: dict[str, str] = {}
    examples: list[DatasetExample] = []
    assignments: dict[str, Split] = {}
    representatives: dict[str, dict[str, Any]] = {}
    for split in Split:
        for candidate in selected[split]:
            identity = _candidate_identity(candidate)
            parent = _parent_identity(candidate)
            source = str(candidate["source_text"])
            normalized_source = _normalized_text(source)
            if normalized_source in seen_normalized_sources:
                raise ValueError("AI-Gold record-level duplicate or conflicting normalized source detected")
            seen_normalized_sources[normalized_source] = identity
            examples.append(DatasetExample(identity, parent))
            assignments[identity] = split
            representatives.setdefault(parent, candidate)
    leakage = validate_no_split_leakage(examples, assignments)
    if leakage:
        raise ValueError("AI-Gold parent split leakage detected")
    representative_rows = [
        {
            "example_id": _candidate_identity(candidate),
            "canonical_document_id": parent,
            "source_text": candidate["target_plain_text"],
        }
        for parent, candidate in sorted(representatives.items())
    ]
    deduplication = audit_deduplication(representative_rows)
    if (
        deduplication.exact_groups
        or deduplication.normalized_groups
        or deduplication.near_duplicate_groups
        or deduplication.family_leakage
    ):
        raise ValueError("AI-Gold parent-family deduplication failed")


def _split_payload(records: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(f"{_canonical_json(record)}\n" for record in records).encode("utf-8")


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite different frozen AI-Gold artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _sealed_path(root: Path, split: Split) -> Path:
    if split is Split.TRAIN:
        return root / "training" / "train" / "ai_gold_train.sealed.jsonl"
    if split is Split.VALIDATION:
        return root / "evaluation" / "validation" / "ai_gold_validation.sealed.jsonl"
    return root / "holdout" / split.value / f"ai_gold_{split.value}.sealed.jsonl"


def freeze_ai_verified_gold(
    candidates: Iterable[Mapping[str, Any]],
    *,
    seed: int,
    manifest_path: str | Path | None = None,
    sealed_output_dir: str | Path | None = None,
    split_targets: Mapping[Split | str, int] | None = None,
) -> GoldFreezeResult:
    """Freeze an AI-only Gold Set while returning only aggregate evidence.

    Test and challenge records are materialized only when ``sealed_output_dir``
    is explicitly provided. The ordinary result and manifest contain no record
    identities or content.
    """

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("AI-Gold seed must be a non-negative integer")
    targets = _normalized_targets(split_targets)
    materialized = tuple(candidates)
    if not materialized:
        raise ValueError("AI-Gold candidates must not be empty")
    valid: list[dict[str, Any]] = []
    rejection_counts: Counter[str] = Counter()
    for candidate in materialized:
        try:
            valid.append(validate_ai_gold_candidate(candidate))
        except (KeyError, TypeError, ValueError):
            rejection_counts["candidate_validation_failed"] += 1
    _validate_frozen_split_integrity(tuple(valid))
    unique_sources, normalized_source_duplicates = _collapse_normalized_source_duplicates(
        tuple(valid),
        seed=seed,
    )
    retained_families, collapsed_parent_families = _collapse_near_duplicate_parent_families(
        unique_sources,
        seed=seed,
    )
    selected = _select_gold_records(retained_families, seed=seed, targets=targets)
    _audit_selected_gold(selected)

    payloads = {split: _split_payload(selected[split]) for split in Split}
    split_counts = {split.value: len(selected[split]) for split in Split}
    split_evidence = {
        split.value: {
            "count": len(selected[split]),
            "parent_count": len({_parent_identity(candidate) for candidate in selected[split]}),
            "records_sha256": hashlib.sha256(payloads[split]).hexdigest(),
        }
        for split in Split
    }
    parent_splits: dict[str, set[Split]] = defaultdict(set)
    for split in Split:
        for candidate in selected[split]:
            parent_splits[_parent_identity(candidate)].add(split)
    package_hashes = sorted(
        {str(candidate["reviewed_package_sha256"]) for candidate in valid if "reviewed_package_sha256" in candidate}
    )
    selected_digest = hashlib.sha256()
    for split in Split:
        selected_digest.update(split.value.encode("ascii"))
        selected_digest.update(b"\0")
        selected_digest.update(payloads[split])
    eligible_payload = _split_payload(sorted(valid, key=lambda candidate: _candidate_identity(candidate)))
    sealed = sealed_output_dir is not None
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "name": "AI-Verified Gold Set",
        "selection_algorithm": "parent_grouped_sha256_v1",
        "seed": seed,
        "candidate_count": len(materialized),
        "eligible_count": len(valid),
        "eligible_unselected_count": len(valid) - sum(split_counts.values()),
        "rejected_count": sum(rejection_counts.values()),
        "rejection_reason_counts": dict(sorted(rejection_counts.items())),
        "total_count": sum(split_counts.values()),
        "split_targets": {split.value: targets[split] for split in Split},
        "split_counts": split_counts,
        "splits": split_evidence,
        "input_eligible_sha256": hashlib.sha256(eligible_payload).hexdigest(),
        "selected_records_sha256": selected_digest.hexdigest(),
        "critic_package_count": len(package_hashes),
        "critic_packages_sha256": package_hashes,
        "evidence": {
            "semantic_plan_schema_valid": True,
            "document_ast_valid": True,
            "deterministic_renderings_valid": True,
            "sst_round_trip_valid": True,
            "protected_spans_preserved": True,
            "fact_critic_minimum": 1,
            "style_critic_minimum": 1,
            "adversarial_critic_minimum": 2,
            "critical_flags": 0,
            "deterministic_validator_runs_per_record": 2,
            "normalized_source_duplicate_records_collapsed": normalized_source_duplicates,
            "near_duplicate_parent_families_collapsed": collapsed_parent_families,
            "record_exact_and_normalized_duplicate_count": 0,
            "near_duplicate_parent_family_count": 0,
        },
        "isolation": {
            "parent_split_leakage_count": sum(1 for splits in parent_splits.values() if len(splits) > 1),
            "routine_manifest_contains_individual_records": False,
            "sealed_outputs_written": sealed,
            "test_and_challenge_excluded_from_training": True,
        },
        "provenance": {
            "ai_only": True,
            "human_curation": False,
            "human_gold": False,
            "generative_api": False,
        },
    }
    validate_ai_gold_manifest(manifest)
    manifest_payload = f"{_canonical_json(manifest)}\n".encode()
    manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()

    sealed_files: dict[Split, Path] = {}
    if sealed_output_dir is not None:
        root = Path(sealed_output_dir)
        for split in Split:
            path = _sealed_path(root, split)
            _write_immutable(path, payloads[split])
            sealed_files[split] = path

    resolved_manifest_path = Path(manifest_path) if manifest_path is not None else None
    checksum_path: Path | None = None
    if resolved_manifest_path is not None:
        _write_immutable(resolved_manifest_path, manifest_payload)
        checksum_path = Path(f"{resolved_manifest_path}.sha256")
        _write_immutable(checksum_path, f"{manifest_sha256}\n".encode("ascii"))
    return GoldFreezeResult(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        manifest_path=resolved_manifest_path,
        checksum_path=checksum_path,
        sealed_files=sealed_files,
    )


def _protected_values_survive(source: str, target_sst: str) -> bool:
    return all(target_sst.count(span.value) == 1 for span in protect_spans(source).spans)


def build_gold_examples(candidates: Iterable[Mapping[str, Any]], assignments: Mapping[str, Split]) -> GoldBuildResult:
    """Return only AI-Gold candidates with complete independent and deterministic proof."""
    materialized = tuple(candidates)
    audit = audit_deduplication(materialized)
    examples = tuple(
        DatasetExample(str(item.get("example_id", "")), str(item.get("canonical_document_id", "")))
        for item in materialized
    )
    leakage_documents = {issue.split(":", 1)[0] for issue in validate_no_split_leakage(examples, assignments)}
    duplicate_ids = {example_id for group in audit.near_duplicate_groups for example_id in group}
    accepted: list[Mapping[str, Any]] = []
    rejected: dict[str, tuple[str, ...]] = {}
    for candidate in sorted(materialized, key=lambda item: str(item.get("example_id", ""))):
        example_id = str(candidate.get("example_id", ""))
        reasons: list[str] = []
        if str(candidate.get("canonical_document_id", "")) in leakage_documents:
            reasons.append("split_leakage")
        elif example_id in duplicate_ids:
            reasons.append("duplicate_family")
        semantic_plan = candidate.get("semantic_plan")
        if (
            not isinstance(semantic_plan, Mapping)
            or not isinstance(semantic_plan.get("id"), str)
            or not semantic_plan["id"]
        ):
            reasons.append("invalid_semantic_plan")
        if not isinstance(candidate.get("target_ast"), Mapping):
            reasons.append("invalid_document_ast")
        target_sst = candidate.get("target_sst")
        try:
            document = parse_sst(str(target_sst))
            if render_sst(document) != target_sst:
                reasons.append("sst_roundtrip_failed")
        except (SSTParseError, ValueError):
            reasons.append("invalid_sst")
        source = candidate.get("source_text")
        if not isinstance(source, str) or not _protected_values_survive(source, str(target_sst)):
            reasons.append("protected_span_failed")
        if not candidate.get("deterministic_valid") or not candidate.get("fact_roundtrip_valid"):
            reasons.append("deterministic_gate_failed")
        verdicts = candidate.get("verdicts")
        if not isinstance(verdicts, tuple) or not all(isinstance(item, CriticVerdict) for item in verdicts):
            reasons.append("critic_not_accepted")
        else:
            try:
                decision = aggregate_critic_verdicts(
                    generator_agent_id=str(candidate.get("generator_agent_id", "")),
                    risk=ExampleRisk(candidate.get("risk", ExampleRisk.AI_GOLD)),
                    deterministic_valid=bool(candidate.get("deterministic_valid")),
                    verdicts=verdicts,
                )
            except ValueError:
                decision = ReleaseDecision.REJECT
            if decision is not ReleaseDecision.ACCEPT:
                reasons.append("critic_not_accepted")
        if reasons:
            rejected[example_id] = tuple(dict.fromkeys(reasons))
        else:
            accepted.append(candidate)
    return GoldBuildResult(tuple(accepted), dict(sorted(rejected.items())))
