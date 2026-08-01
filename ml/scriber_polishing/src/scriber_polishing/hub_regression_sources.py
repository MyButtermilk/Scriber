"""Deterministically select private-Hub reload cases from non-holdout data."""

from __future__ import annotations

import hashlib
import heapq
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .evaluation_io import validate_evaluation_reference

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_SCHEMA = _PROJECT_ROOT / "contracts" / "hub_regression_source_schema.json"

SYNTHETIC_CATEGORIES = (
    "protected_span",
    "business_post",
    "tax_law",
    "paragraphs",
    "headings",
    "lists",
    "units",
    "legal_citations",
    "address_pronouns",
    "identity",
)
AI_GOLD_CATEGORY = "ai_gold_smoke"
REQUIRED_CATEGORIES = (AI_GOLD_CATEGORY, *SYNTHETIC_CATEGORIES)


class HubRegressionSourceError(RuntimeError):
    """A non-holdout source pool cannot produce the fixed regression suite."""


@dataclass(frozen=True)
class HubRegressionSourceBuild:
    """Immutable source and evaluation-reference payloads plus their evidence."""

    sources: tuple[dict[str, Any], ...]
    references: tuple[dict[str, Any], ...]
    evidence: dict[str, Any]


@cache
def _source_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_SOURCE_SCHEMA.read_text(encoding="utf-8")))


def _canonical_line(value: Mapping[str, object]) -> bytes:
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


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _iter_jsonl(paths: Sequence[Path], *, label: str) -> Iterable[tuple[Path, int, dict[str, Any]]]:
    for path in paths:
        resolved = path.resolve()
        if resolved.is_symlink() or not resolved.is_file():
            raise HubRegressionSourceError(f"{label} is not a regular file: {path}")
        with resolved.open(encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                line = raw_line.rstrip("\r\n")
                if not line:
                    raise HubRegressionSourceError(
                        f"{label} contains a blank line at {path}:{line_number}"
                    )
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise HubRegressionSourceError(
                        f"{label} contains invalid JSON at {path}:{line_number}"
                    ) from error
                if not isinstance(record, dict):
                    raise HubRegressionSourceError(
                        f"{label} record is not an object at {path}:{line_number}"
                    )
                yield resolved, line_number, record


def _string_set(record: Mapping[str, object], field: str) -> set[str]:
    value = record.get(field)
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _eligible_spans(record: Mapping[str, object]) -> list[str]:
    value = record.get("protected_spans")
    if not isinstance(value, list):
        return []
    spans: list[str] = []
    seen: set[str] = set()
    for item in value:
        if (
            isinstance(item, str)
            and item
            and len(item) <= 300
            and item not in seen
        ):
            spans.append(item)
            seen.add(item)
    return spans[:64]


def _contains_marker(values: Iterable[str], markers: tuple[str, ...]) -> bool:
    return any(any(marker in value for marker in markers) for value in values)


def _categories(record: Mapping[str, object]) -> tuple[str, ...]:
    categories: list[str] = ["protected_span"]
    domain = record.get("domain")
    if domain == "general_business":
        categories.append("business_post")
    if domain == "tax_legal":
        categories.append("tax_law")
    paragraphs = record.get("paragraph_boundaries")
    if isinstance(paragraphs, list) and len(paragraphs) >= 2:
        categories.append("paragraphs")
    headings = record.get("heading_boundaries")
    if isinstance(headings, list) and headings:
        categories.append("headings")
    lists = record.get("list_boundaries")
    if isinstance(lists, list) and lists:
        categories.append("lists")
    markers = (
        _string_set(record, "risk_tags")
        | _string_set(record, "operations")
        | _string_set(record, "applied_normalizations")
    )
    if _contains_marker(
        markers,
        ("unit", "quantity", "currency", "money", "amount", "energy"),
    ):
        categories.append("units")
    if _contains_marker(markers, ("legal_citation", "legal_reference", "law_name")):
        categories.append("legal_citations")
    address_mode = record.get("address_mode")
    if address_mode in {
        "formal_sie",
        "personal_du_capitalized",
        "personal_du_lowercase",
        "mixed_correspondence",
    } or _contains_marker(markers, ("address", "pronoun")):
        categories.append("address_pronouns")
    if record.get("severity") == "identity" or record.get("corruption_profile") == "identity":
        categories.append("identity")
    return tuple(dict.fromkeys(categories))


def _candidate_identity(record: Mapping[str, object]) -> str:
    for field in ("example_id", "canonical_document_id", "seed_id"):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    raise HubRegressionSourceError("source record has no stable identity")


def _score(seed: int, category: str, identity: str) -> str:
    return hashlib.sha256(f"{seed}:{category}:{identity}".encode()).hexdigest()


def _candidate_record(record: Mapping[str, object]) -> dict[str, Any] | None:
    source_text = record.get("source_text")
    target_sst = record.get("target_sst")
    target_plain_text = record.get("target_plain_text")
    target_ast = record.get("target_ast")
    semantic_plan = record.get("semantic_plan")
    provenance = record.get("provenance")
    if (
        not isinstance(source_text, str)
        or not source_text
        or len(source_text) > 20000
        or not isinstance(target_sst, str)
        or not target_sst
        or not isinstance(target_plain_text, str)
        or not target_plain_text
        or not isinstance(target_ast, dict)
        or not isinstance(semantic_plan, dict)
        or not isinstance(provenance, dict)
        or provenance.get("synthetic_only") is not True
        or provenance.get("human_curation") is not False
        or provenance.get("generative_api") is not False
    ):
        return None
    spans = _eligible_spans(record)
    if not spans:
        return None
    split = record.get("split")
    if split not in {"train", "validation"}:
        raise HubRegressionSourceError("holdout record reached the non-holdout selector")
    address_mode = record.get("address_mode")
    return {
        "identity": _candidate_identity(record),
        "source_text": source_text,
        "target_sst": target_sst,
        "target_plain_text": target_plain_text,
        "target_ast": target_ast,
        "semantic_plan": semantic_plan,
        "protected_spans": spans,
        "address_mode": address_mode if isinstance(address_mode, str) else None,
        "domain": record.get("domain"),
        "split": split,
        "is_identity": source_text == target_plain_text,
    }


def _bounded_candidates(
    paths: Sequence[Path],
    *,
    categories: Sequence[str],
    seed: int,
    per_category: int,
    ai_gold: bool,
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    capacity = max(per_category * 20, per_category + 10)
    heaps: dict[str, list[tuple[int, str, dict[str, Any]]]] = {
        category: [] for category in categories
    }
    seen: dict[str, set[str]] = {category: set() for category in categories}
    for _, _, raw in _iter_jsonl(paths, label="AI-Gold source" if ai_gold else "synthetic source"):
        candidate = _candidate_record(raw)
        if candidate is None:
            continue
        available = (AI_GOLD_CATEGORY,) if ai_gold else _categories(raw)
        for category in available:
            if category not in heaps:
                continue
            identity = str(candidate["identity"])
            if identity in seen[category]:
                continue
            seen[category].add(identity)
            score = _score(seed, category, candidate["identity"])
            rank = -int(score, 16)
            entry = (rank, score, candidate)
            heap = heaps[category]
            if len(heap) < capacity:
                heapq.heappush(heap, entry)
            elif entry[0] > heap[0][0]:
                heapq.heapreplace(heap, entry)
    return {
        category: [(score, candidate) for _, score, candidate in sorted(heap, key=lambda item: item[1])]
        for category, heap in heaps.items()
    }


def _select_unique(
    candidates: Mapping[str, list[tuple[str, dict[str, Any]]]],
    *,
    categories: Sequence[str],
    per_category: int,
) -> list[tuple[str, str, dict[str, Any]]]:
    selected: list[tuple[str, str, dict[str, Any]]] = []
    used: set[str] = set()
    for category in categories:
        chosen = 0
        for score, candidate in candidates.get(category, []):
            identity = str(candidate["identity"])
            if identity in used:
                continue
            selected.append((category, score, candidate))
            used.add(identity)
            chosen += 1
            if chosen == per_category:
                break
        if chosen != per_category:
            raise HubRegressionSourceError(
                f"non-holdout pool has only {chosen}/{per_category} unique cases for {category}"
            )
    return selected


def _source_record(
    category: str,
    score: str,
    candidate: Mapping[str, Any],
    *,
    ai_gold: bool,
) -> dict[str, Any]:
    split = candidate["split"]
    source_partition = (
        f"ai_gold_{split}" if ai_gold else "synthetic_non_holdout"
    )
    record = {
        "schema_version": 1,
        "case_id": f"hub_{category}_{score[:16]}",
        "source_kind": "ai_gold_smoke" if ai_gold else "synthetic_regression",
        "source_partition": source_partition,
        "category": category,
        "source_text": candidate["source_text"],
        "protected_spans": candidate["protected_spans"],
        "private_data": False,
        "holdout": False,
    }
    errors = sorted(
        _source_validator().iter_errors(record),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        raise HubRegressionSourceError(
            f"selected source failed schema: {errors[0].message}"
        )
    return record


def _reference_record(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    tags = {
        "hub_regression",
        f"category:{source['category']}",
    }
    domain = candidate.get("domain")
    if isinstance(domain, str) and domain:
        tags.add(f"domain:{domain}")
    reference: dict[str, Any] = {
        "schema_version": 1,
        "case_id": source["case_id"],
        "split": "regression",
        "source_text": source["source_text"],
        "target_sst": candidate["target_sst"],
        "target_plain_text": candidate["target_plain_text"],
        "target_ast": candidate["target_ast"],
        "semantic_plan": candidate["semantic_plan"],
        "protected_spans": source["protected_spans"],
        "slice_tags": sorted(tags),
        "is_identity": bool(candidate["is_identity"]),
    }
    address_mode = candidate.get("address_mode")
    if isinstance(address_mode, str):
        reference["address_mode"] = address_mode
    return validate_evaluation_reference(reference)


def build_hub_regression_sources(
    *,
    synthetic_paths: Sequence[str | Path],
    ai_gold_paths: Sequence[str | Path],
    seed: int = 270043,
    per_category: int = 10,
) -> HubRegressionSourceBuild:
    """Select 100 synthetic and 10 AI-Gold cases without touching holdouts."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("Hub regression source seed must be a nonnegative integer")
    if (
        isinstance(per_category, bool)
        or not isinstance(per_category, int)
        or per_category < 1
    ):
        raise ValueError("Hub regression per-category count must be positive")
    synthetic = tuple(Path(path) for path in synthetic_paths)
    ai_gold = tuple(Path(path) for path in ai_gold_paths)
    if not synthetic or not ai_gold:
        raise ValueError("Hub regression selection requires both source pools")
    synthetic_candidates = _bounded_candidates(
        synthetic,
        categories=SYNTHETIC_CATEGORIES,
        seed=seed,
        per_category=per_category,
        ai_gold=False,
    )
    ai_gold_candidates = _bounded_candidates(
        ai_gold,
        categories=(AI_GOLD_CATEGORY,),
        seed=seed,
        per_category=per_category,
        ai_gold=True,
    )
    selected = _select_unique(
        synthetic_candidates,
        categories=SYNTHETIC_CATEGORIES,
        per_category=per_category,
    )
    selected.extend(
        _select_unique(
            ai_gold_candidates,
            categories=(AI_GOLD_CATEGORY,),
            per_category=per_category,
        )
    )
    sources: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    for category, score, candidate in selected:
        source = _source_record(
            category,
            score,
            candidate,
            ai_gold=category == AI_GOLD_CATEGORY,
        )
        sources.append(source)
        references.append(_reference_record(source, candidate))
    ordered = sorted(
        zip(sources, references, strict=True),
        key=lambda pair: str(pair[0]["case_id"]),
    )
    sources = [pair[0] for pair in ordered]
    references = [pair[1] for pair in ordered]
    source_payload = b"".join(_canonical_line(record) for record in sources)
    reference_payload = b"".join(_canonical_line(record) for record in references)
    counts = Counter(str(record["category"]) for record in sources)
    expected_count = per_category * len(REQUIRED_CATEGORIES)
    if len(sources) != expected_count or any(
        counts[category] != per_category for category in REQUIRED_CATEGORIES
    ):
        raise HubRegressionSourceError("selected regression coverage is incomplete")
    evidence = {
        "schema_version": 1,
        "status": "prepared",
        "seed": seed,
        "case_count": len(sources),
        "synthetic_case_count": per_category * len(SYNTHETIC_CATEGORIES),
        "ai_gold_smoke_count": per_category,
        "category_counts": dict(sorted(counts.items())),
        "source_partition_counts": dict(
            sorted(Counter(str(record["source_partition"]) for record in sources).items())
        ),
        "source_sha256": _sha256_bytes(source_payload),
        "reference_sha256": _sha256_bytes(reference_payload),
        "holdout_data": False,
        "private_data": False,
        "synthetic_ai_only": True,
    }
    return HubRegressionSourceBuild(
        sources=tuple(sources),
        references=tuple(references),
        evidence=evidence,
    )


def write_hub_regression_sources(
    build: HubRegressionSourceBuild,
    *,
    sources_output: str | Path,
    references_output: str | Path,
    report_output: str | Path,
) -> None:
    """Write all source-selection outputs atomically without overwriting them."""

    outputs = {
        Path(sources_output).resolve(): b"".join(
            _canonical_line(record) for record in build.sources
        ),
        Path(references_output).resolve(): b"".join(
            _canonical_line(record) for record in build.references
        ),
        Path(report_output).resolve(): (
            json.dumps(
                build.evidence,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8"),
    }
    if len(outputs) != 3:
        raise ValueError("Hub regression output paths must be distinct")
    for path, payload in outputs.items():
        if path.exists():
            if path.is_file() and path.read_bytes() == payload:
                continue
            raise HubRegressionSourceError(f"refusing to overwrite output: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
