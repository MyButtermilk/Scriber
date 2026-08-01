"""Deterministic, fail-closed auditing of the mandatory polishing-data quotas.

The auditor deliberately treats missing provenance as missing evidence.  It never
uses a language model or a heuristic semantic classifier to fill a quota.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ast_codec import document_from_dict
from .dataset_policy import PROJECT_ROOT, read_regular_file_snapshot
from .document_ast import BlockType, Document

_QUOTA_PATH = Path(__file__).parents[2] / "contracts" / "mandatory_coverage_quotas_v1.json"
MANDATORY_QUOTA_PATH = _QUOTA_PATH
_QUOTA_SNAPSHOT = read_regular_file_snapshot(_QUOTA_PATH, allowed_root=PROJECT_ROOT)
MANDATORY_QUOTA_BINDING = _QUOTA_SNAPSHOT.binding
_SPLITS = frozenset({"train", "validation", "test", "challenge"})
_DOMAINS = frozenset(
    {"general_business", "tax_legal", "real_estate_energy", "technical_project", "english_mixed", "hard_negatives"}
)
_LANGUAGES = frozenset({"de-DE", "en", "mixed"})
_ADDRESS_MODES = frozenset(
    {
        "formal_sie",
        "personal_du_capitalized",
        "personal_du_lowercase",
        "neutral_third_person",
        "quoted_speech",
        "mixed_correspondence",
        "ambiguous",
    }
)
_SPACE = re.compile(r"\s+")
_DATE = re.compile(
    r"\b(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}\.\s*(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s*\d{4})\b",
    re.I,
)
_MONEY_PERCENT_UNIT = re.compile(r"(?:€|\b(?:Euro|Prozent|%|m²|m³|kW(?:h)?|MW(?:h)?|km/h|m/s)\b)", re.I)
_SPELLED_UNIT = re.compile(
    r"\b(?:Quadratmeter|Kubikmeter|Kilowattstunden?|Megawattstunden?|Euro(?:\s+je\s+Quadratmeter)?|Kilometer\s+pro\s+Stunde|Meter\s+pro\s+Sekunde)\b",
    re.I,
)
_M2_M3 = re.compile(r"\b\d+(?:[.,]\d+)?\s*m[²³]\b", re.I)
_EURO_M2 = re.compile(r"\b\d+(?:[.,]\d+)?\s*€/m²\b", re.I)
_POWER = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:kW|kWh|MW|MWh)\b", re.I)
_ENERGY_AREA = re.compile(r"\b\d+(?:[.,]\d+)?\s*kWh/\(m²·a\)(?!\w)", re.I)
_SPEED_COMPOUND = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:km/h|m/s|\w+/(?:\w+|\([^)]*\)))(?!\w)",
    re.I,
)
_LAW_NAME = re.compile(
    r"\b(?:Einkommensteuergesetz|Bürgerliches Gesetzbuch|Handelsgesetzbuch|Umsatzsteuergesetz|Abgabenordnung|Grundgesetz)\b",
    re.I,
)
_LEGAL_CITATION = re.compile(
    r"§{1,2}\s*\d+[a-zA-Z]?(?:\s+und\s+\d+[a-zA-Z]?)?"
    r"(?:\s+(?:Abs\.|Absatz)\s*\d+)?(?:\s+Satz\s*\d+)?"
    r"(?:\s+(?:Nr\.|Nummer)\s*\d+)?(?:\s+(?:Buchst\.|Buchstabe)\s*[a-z])?"
    r"\s+(?:EStG|KStG|GewStG|BGB|HGB|UStG|AO|GG)\b"
)
_PARAGRAPH_SENTENCE = re.compile(
    r"§{1,2}\s*\d+.*\b(?:Abs\.|Absatz)\s*\d+.*\bSatz\s*\d+",
    re.I,
)
_NUMBER_LETTER = re.compile(
    r"§{1,2}\s*\d+.*\b(?:Nr\.|Nummer)\s*\d+.*\b(?:Buchst\.|Buchstabe)\s*[a-z]",
    re.I,
)
_ARTICLE = re.compile(r"\b(?:Art\.|Artikel)\s*\d+", re.I)
_TAX_LAW = re.compile(r"\b(?:EStG|KStG|GewStG|UStG|AO)\b")
_UNIT_SYMBOL = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:m²|m³|kW|kWh|MW|MWh|km/h|m/s)|€", re.I)
_NO_HEADING = re.compile(r"\bkeine(?:\s+zulässige)?\s+(?:Zwischen)?überschrift\b", re.I)


@dataclass(frozen=True, slots=True)
class QuotaResult:
    """Evidence-backed result for one section-22 quota."""

    quota_id: str
    minimum: int
    total_count: int
    train_count: int
    total_deficit: int
    train_deficit: int
    evidence: str


@dataclass(frozen=True, slots=True)
class CoverageAuditReport:
    """Stable aggregate report; section-22 minima apply to the train split."""

    record_count: int
    train_record_count: int
    quotas: tuple[QuotaResult, ...]
    diversity: dict[str, int]
    detector_notes: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not any(result.train_deficit for result in self.quotas)

    @property
    def deficits(self) -> dict[str, int]:
        return {
            result.quota_id: result.train_deficit
            for result in sorted(self.quotas, key=lambda item: item.quota_id)
            if result.train_deficit
        }

    def quota(self, quota_id: str) -> QuotaResult:
        for result in self.quotas:
            if result.quota_id == quota_id:
                return result
        raise KeyError(quota_id)


class CoverageAuditError(ValueError):
    """Raised when the mandatory train-only coverage floor is not evidenced."""

    def __init__(self, report: CoverageAuditReport) -> None:
        self.report = report
        self.deficits = report.deficits
        summary = ", ".join(f"{quota_id}={deficit}" for quota_id, deficit in self.deficits.items())
        super().__init__(f"mandatory coverage audit failed (train deficits): {summary}")


def _load_quotas() -> tuple[tuple[str, int], ...]:
    try:
        payload = json.loads(_QUOTA_SNAPSHOT.payload)
    except json.JSONDecodeError as error:  # pragma: no cover - repository-owned contract
        raise RuntimeError("mandatory coverage quota contract is unreadable") from error
    if (
        payload.get("schema_version") != 1
        or payload.get("scope") != "train"
        or not isinstance(payload.get("quotas"), list)
    ):
        raise RuntimeError("mandatory coverage quota contract is invalid")
    quotas: list[tuple[str, int]] = []
    for item in payload["quotas"]:
        if not isinstance(item, Mapping) or set(item) != {"id", "minimum"}:
            raise RuntimeError("mandatory coverage quota contract has an invalid entry")
        quota_id, minimum = item["id"], item["minimum"]
        if (
            not isinstance(quota_id, str)
            or not quota_id
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or minimum <= 0
        ):
            raise RuntimeError("mandatory coverage quota contract has an invalid value")
        quotas.append((quota_id, minimum))
    if len(quotas) != len({quota_id for quota_id, _ in quotas}):
        raise RuntimeError("mandatory coverage quota contract has duplicate identifiers")
    return tuple(quotas)


MANDATORY_QUOTAS = _load_quotas()


def _required_string(record: Mapping[str, Any], field: str, *, limit: int) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"training pair {field} must be a non-empty string of at most {limit} characters")
    return value


def _string_list(record: Mapping[str, Any], field: str) -> frozenset[str]:
    value = record.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"training pair {field} must be a list of non-empty strings")
    return frozenset(value)


def _validate_record(record: object, seen_ids: set[str]) -> tuple[dict[str, Any], Document]:
    if not isinstance(record, Mapping):
        raise ValueError("training pair must be an object")
    result = dict(record)
    example_id = _required_string(result, "example_id", limit=200)
    if example_id in seen_ids:
        raise ValueError(f"training pair example_id is duplicated: {example_id}")
    seen_ids.add(example_id)
    split, domain, language, address_mode = (
        result.get(name) for name in ("split", "domain", "language", "address_mode")
    )
    if (
        split not in _SPLITS
        or domain not in _DOMAINS
        or language not in _LANGUAGES
        or address_mode not in _ADDRESS_MODES
    ):
        raise ValueError("training pair has an invalid split, domain, language, or address_mode")
    risk_tags = result.get("risk_tags")
    if (
        not isinstance(risk_tags, list)
        or not all(isinstance(tag, str) and tag for tag in risk_tags)
        or len(risk_tags) != len(set(risk_tags))
    ):
        raise ValueError("training pair risk_tags must be a duplicate-free list")
    _string_list(result, "risk_tags")
    _string_list(result, "operations")
    _string_list(result, "applied_normalizations")
    _required_string(result, "source_text", limit=30_000)
    _required_string(result, "target_plain_text", limit=20_000)
    profile = _required_string(result, "corruption_profile", limit=80)
    if not profile.replace("_", "").isalnum():
        raise ValueError("training pair corruption_profile must be an identifier")
    mappings = result.get("value_mappings")
    if not isinstance(mappings, list):
        raise ValueError("training pair value_mappings must be a list")
    for mapping in mappings:
        if not isinstance(mapping, Mapping) or set(mapping) != {"kind", "source", "target"}:
            raise ValueError("training pair value_mappings must contain kind, source, and target")
        if not all(isinstance(mapping[key], str) and mapping[key] for key in ("kind", "source", "target")):
            raise ValueError("training pair value_mappings must contain non-empty strings")
    try:
        document = document_from_dict(result.get("target_ast"))
    except ValueError as error:
        raise ValueError("training pair target_ast is invalid") from error
    semantic_plan = result.get("semantic_plan")
    if not isinstance(semantic_plan, Mapping):
        raise ValueError("training pair semantic_plan must be an object")
    entities = semantic_plan.get("entities", [])
    if not isinstance(entities, list) or not all(isinstance(entity, Mapping) for entity in entities):
        raise ValueError("training pair semantic_plan.entities must be a list of objects")
    provenance = result.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("training pair provenance must be an object")
    for key in ("canonical_generator_id", "plan_generator_agent", "target_writer_agent"):
        if key not in provenance or (provenance[key] is not None and not isinstance(provenance[key], str)):
            raise ValueError(f"training pair provenance.{key} must be a string or null")
    return result, document


def _normalise(text: str) -> str:
    return _SPACE.sub(" ", text.casefold()).strip()


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text_ngrams(text: str, *, limit: int = 32) -> tuple[str, ...]:
    tokens = _normalise(text).split()
    return tuple(" ".join(tokens[index : index + 3]) for index in range(min(max(0, len(tokens) - 2), limit)))


def _add_bounded_fingerprint(value: str, values: set[int], heap: list[int], *, limit: int) -> None:
    """Keep the deterministic bottom-k SHA-256 fingerprints in bounded memory."""

    fingerprint = int(_fingerprint(value), 16)
    if fingerprint in values:
        return
    if len(values) < limit:
        values.add(fingerprint)
        heapq.heappush(heap, -fingerprint)
        return
    largest = -heap[0]
    if fingerprint < largest:
        values.remove(largest)
        values.add(fingerprint)
        heapq.heapreplace(heap, -fingerprint)


def _ast_signature(document: Document) -> str:
    blocks: list[str] = []
    for block in document.blocks:
        if block.items:
            blocks.append(f"{block.type.value}:{','.join(str(item.level) for item in block.items)}")
        elif block.lines:
            blocks.append(f"{block.type.value}:lines={len(block.lines)}")
        else:
            blocks.append(block.type.value)
    return "|".join(blocks)


def _has_mapping(mappings: list[Mapping[str, Any]], kind: str) -> bool:
    return any(mapping["kind"] == kind for mapping in mappings)


def _evidence(record: Mapping[str, Any], document: Document) -> frozenset[str]:
    tags = _string_list(record, "risk_tags")
    operations = _string_list(record, "operations")
    normalizations = _string_list(record, "applied_normalizations")
    mappings = record["value_mappings"]
    assert isinstance(mappings, list)  # established by _validate_record
    text = f"{record['source_text']}\n{record['target_plain_text']}"
    target = record["target_plain_text"]
    assert isinstance(text, str) and isinstance(target, str)
    types = [block.type for block in document.blocks]
    paragraph_count = types.count(BlockType.PARAGRAPH)
    semantic_plan = record["semantic_plan"]
    assert isinstance(semantic_plan, Mapping)
    structure = semantic_plan.get("structure", {})
    paragraph_topics = structure.get("paragraph_topics", []) if isinstance(structure, Mapping) else []
    topic_count = (
        len(paragraph_topics)
        if isinstance(paragraph_topics, list) and all(isinstance(item, str) for item in paragraph_topics)
        else 0
    )
    facts = semantic_plan.get("facts", [])
    fact_count = (
        len(facts)
        if isinstance(facts, list) and all(isinstance(item, Mapping) for item in facts)
        else 0
    )
    has = set[str]()
    if record["language"] == "de-DE" and record["domain"] != "hard_negatives":
        has.add("german_business_communication")
    if record["domain"] == "tax_legal" or "tax_legal" in tags:
        has.add("tax_legal_terminology")
    if BlockType.SALUTATION in types or BlockType.CLOSING in types:
        has.add("salutation_or_closing")
    if _DATE.search(text) or {"date", "protected_date"}.intersection(tags):
        has.add("dates")
    if _MONEY_PERCENT_UNIT.search(text) or _has_mapping(mappings, "unit_surface"):
        has.add("money_percent_or_units")
    if paragraph_count >= 2 and topic_count >= 2:
        has.update({"multiple_thematic_paragraphs", "two_or_more_paragraphs"})
    elif paragraph_count >= 2:
        has.add("two_or_more_paragraphs")
    if paragraph_count >= 4 and max(topic_count, fact_count) >= 4:
        has.add("four_or_more_thematic_paragraphs")
    if BlockType.ORDERED_LIST in types:
        has.update({"numbered_lists", "numbered_lists_formatting"})
    if BlockType.UNORDERED_LIST in types:
        has.update({"bullet_lists", "bullet_lists_formatting"})
    if any(block.items and any(item.level > 1 for item in block.items) for block in document.blocks):
        has.add("nested_lists")
    operation_quotas = {
        "repetition": "unnecessary_repetitions",
        "grammar_error": "grammar_errors",
        "spelling_error": "spelling_errors",
        "self_correction": "self_corrections",
        "abandoned_start": "abandoned_starts",
        "spoken_formatting": "spoken_formatting_commands",
        "blank_line_error": "incorrect_blank_lines",
        "merged_paragraphs": "merged_paragraphs",
    }
    has.update(quota_id for operation, quota_id in operation_quotas.items() if operation in operations)
    if {"punctuation_error", "punctuation_removed"}.intersection(operations):
        has.add("punctuation_errors")
    if record["corruption_profile"] in {"identity", "minimal_punctuation"} or str(
        record["corruption_profile"]
    ).startswith(("identity_", "minimal_punctuation_")):
        has.add("identity_or_minimal_changes")
    if BlockType.SUBJECT in types:
        has.add("subject_lines")
    if BlockType.HEADING_1 in types:
        has.add("main_headings")
    if BlockType.HEADING_2 in types:
        has.add("subheadings")
    has_no_heading = BlockType.HEADING_1 not in types and BlockType.HEADING_2 not in types
    if has_no_heading and (
        {"hard_negative", "do_not_invent_heading"}.issubset(tags)
        or {"format_word_ordinary", "do_not_invent_format"}.issubset(tags)
        or _NO_HEADING.search(target)
    ):
        has.add("hard_negative_no_heading")
    if BlockType.SALUTATION in types:
        has.add("salutations")
    if BlockType.CLOSING in types:
        has.add("closings")
    if any(block.type is BlockType.SIGNATURE and len(block.lines) >= 2 for block in document.blocks):
        has.add("multiline_signatures")
    if {"format_word_ordinary", "do_not_invent_format"}.intersection(tags) or (
        has_no_heading and _NO_HEADING.search(target)
    ):
        has.add("hard_negative_formatting_terms")
    if {"blank_line_error", "incorrect_blank_lines"}.intersection(tags):
        has.add("incorrect_blank_lines")
    if {"merged_paragraphs", "paragraphs_merged"}.intersection(tags):
        has.add("merged_paragraphs")
    ast_quotas = {
        BlockType.QUOTE: "quotes",
        BlockType.ATTACHMENTS: "attachments",
        BlockType.POST_SCRIPT: "postscript_or_closing_block",
        BlockType.CLOSING: "postscript_or_closing_block",
    }
    has.update(quota_id for block_type, quota_id in ast_quotas.items() if block_type in types)
    if _SPELLED_UNIT.search(text) or _has_mapping(mappings, "unit_surface"):
        has.add("spelled_out_units")
    if _has_mapping(mappings, "unit_surface"):
        has.add("spoken_unit_conversions")
    if _M2_M3.search(text):
        has.add("square_or_cubic_metres")
    if _EURO_M2.search(text):
        has.add("euros_per_square_metre")
    if _POWER.search(text):
        has.add("power_or_energy_units")
    if _ENERGY_AREA.search(text):
        has.add("energy_per_area_year")
    if _SPEED_COMPOUND.search(text):
        has.add("speed_or_compound_units")
    if (
        "ambiguous_unit_hard_negative" in tags
        or ("unit" in tags and record["domain"] == "hard_negatives")
    ) and not _has_mapping(mappings, "unit_surface"):
        has.add("hard_negative_no_unit_conversion")
    if _LAW_NAME.search(record["source_text"]) or _has_mapping(mappings, "legal_citation_surface"):
        has.add("expanded_law_names")
    if _LEGAL_CITATION.search(target) or _has_mapping(mappings, "legal_citation_surface"):
        has.add("complete_legal_citations")
    if _PARAGRAPH_SENTENCE.search(target):
        has.add("citations_with_paragraph_and_sentence")
    if _NUMBER_LETTER.search(target):
        has.add("citations_with_number_and_letter")
    if "§§" in target:
        has.add("multiple_section_citations")
    if _ARTICLE.search(text):
        has.add("article_citations")
    if _TAX_LAW.search(target):
        has.add("tax_law_citations")
    if "legal_hard_negative" in tags or {"hard_negative", "legal_hierarchy"}.issubset(tags):
        has.add("hard_negative_legal_hierarchy")
    if record["address_mode"] == "formal_sie" or "formal_sie" in tags:
        has.add("formal_sie_addresses")
    if record["address_mode"] in {"personal_du_capitalized", "personal_du_lowercase"} or "personal_du" in tags:
        has.add("personal_du_addresses")
    if "ihr_euch_euer" in tags:
        has.add("ihr_euch_euer_cases")
    if record["address_mode"] == "neutral_third_person" or "third_person_reference" in tags:
        has.add("third_person_pronouns")
    if record["address_mode"] == "ambiguous" or "meaning_dependent_hard_negative" in tags:
        has.add("ambiguous_pronoun_contrasts")
    if {"direct_address", "third_person_reference"}.issubset(tags):
        has.add("direct_and_third_person")
    if (
        "accepted_variant" in tags
        or _has_mapping(mappings, "orthography_surface")
        or ("orthography_pronouns" in tags and record["domain"] == "hard_negatives")
    ):
        has.add("accepted_spelling_variants")
    if (
        "house_style" in normalizations
        or "house_style_normalization" in tags
        or _has_mapping(mappings, "orthography_surface")
    ):
        has.add("house_style_normalizations")
    if "identity_preferred_form" in tags or (
        "orthography_pronouns" in tags and record["domain"] == "general_business"
    ):
        has.add("identity_preferred_house_style")
    if "meaning_dependent_hard_negative" in tags:
        has.add("meaning_dependent_contrasts")
    if (
        {"das_dass", "seit_seid", "wider_wieder", "ss_sz"}.intersection(tags)
        or "context_spelling_error" in operations
    ):
        has.add("das_dass_seit_seid_wider_wieder_ss_sz")
    if "business_capitalization" in tags or (
        "case_error" in operations and record["domain"] != "hard_negatives"
    ):
        has.add("business_capitalization")
    return frozenset(has)


def quota_evidence(record: Mapping[str, Any]) -> frozenset[str]:
    """Return the same fail-closed quota evidence used by the full audit."""

    validated, document = _validate_record(record, set())
    return _evidence(validated, document)


def _evidence_note(quota_id: str) -> str:
    if quota_id.endswith("lists") or quota_id in {"nested_lists", "subject_lines", "main_headings", "subheadings"}:
        return "validated target_ast block types and list-item levels"
    if quota_id in {"grammar_errors", "spelling_errors", "punctuation_errors", "self_corrections", "abandoned_starts"}:
        return "recorded corruption operations only"
    if quota_id in {"spoken_unit_conversions", "expanded_law_names", "complete_legal_citations"}:
        return "reversible value_mappings and strict source/target patterns"
    if "pronoun" in quota_id or quota_id.endswith("addresses") or quota_id == "ihr_euch_euer_cases":
        return "address_mode and explicit risk_tags only"
    if quota_id in {
        "incorrect_blank_lines",
        "merged_paragraphs",
        "hard_negative_no_heading",
        "hard_negative_formatting_terms",
    }:
        return "explicit operation/risk-tag evidence; no text-only guess"
    return "explicit schema fields, risk_tags, source/target text, or target_ast evidence"


def audit_coverage(records: Iterable[Mapping[str, Any]]) -> CoverageAuditReport:
    """Audit all mandatory section-22 quotas in one linear pass over training pairs.

    Counts have two views: all supplied records and the mandatory `train` view.
    A quota is satisfied only by its train count, matching the contract scope.
    """

    total_counts = {quota_id: 0 for quota_id, _ in MANDATORY_QUOTAS}
    train_counts = dict.fromkeys(total_counts, 0)
    source_hashes: set[str] = set()
    target_hashes: set[str] = set()
    ngram_hashes: set[int] = set()
    ngram_heap: list[int] = []
    template_hashes: set[str] = set()
    ast_hashes: set[str] = set()
    entity_hashes: set[str] = set()
    corruption_hashes: set[str] = set()
    family_hashes: set[str] = set()
    seen_ids: set[str] = set()
    record_count = 0
    train_record_count = 0
    for value in records:
        record, document = _validate_record(value, seen_ids)
        record_count += 1
        source = record["source_text"]
        target = record["target_plain_text"]
        assert isinstance(source, str) and isinstance(target, str)
        source_hashes.add(_fingerprint(_normalise(source)))
        target_hashes.add(_fingerprint(_normalise(target)))
        for ngram in _text_ngrams(target):
            _add_bounded_fingerprint(ngram, ngram_hashes, ngram_heap, limit=100_000)
        template_hashes.add(_fingerprint(re.sub(r"\d+(?:[.,]\d+)?", "#", _normalise(target))))
        ast_hashes.add(_fingerprint(_ast_signature(document)))
        semantic_plan = record["semantic_plan"]
        assert isinstance(semantic_plan, Mapping)
        for entity in semantic_plan.get("entities", []):
            assert isinstance(entity, Mapping)
            entity_type, entity_value = entity.get("type"), entity.get("value")
            if isinstance(entity_type, str) and entity_type and isinstance(entity_value, str) and entity_value:
                entity_hashes.add(_fingerprint(f"{entity_type}\0{_normalise(entity_value)}"))
        mappings = record["value_mappings"]
        assert isinstance(mappings, list)
        operations = _string_list(record, "operations")
        mapping_kinds = sorted(str(mapping["kind"]) for mapping in mappings)
        corruption_hashes.add(_fingerprint("|".join((*sorted(operations), *mapping_kinds))))
        provenance = record["provenance"]
        assert isinstance(provenance, Mapping)
        family_hashes.add(
            _fingerprint(
                "|".join(
                    str(provenance.get(key) or "<none>")
                    for key in ("canonical_generator_id", "plan_generator_agent", "target_writer_agent")
                )
            )
        )
        evidence = _evidence(record, document)
        for quota_id in evidence:
            total_counts[quota_id] += 1
            if record["split"] == "train":
                train_counts[quota_id] += 1
        if record["split"] == "train":
            train_record_count += 1
    quota_results = tuple(
        QuotaResult(
            quota_id=quota_id,
            minimum=minimum,
            total_count=total_counts[quota_id],
            train_count=train_counts[quota_id],
            total_deficit=max(0, minimum - total_counts[quota_id]),
            train_deficit=max(0, minimum - train_counts[quota_id]),
            evidence=_evidence_note(quota_id),
        )
        for quota_id, minimum in MANDATORY_QUOTAS
    )
    diversity = {
        "unique_normalized_source_texts": len(source_hashes),
        "unique_normalized_target_texts": len(target_hashes),
        "unique_target_3gram_hashes_sampled": len(ngram_hashes),
        "unique_target_template_hashes": len(template_hashes),
        "unique_ast_signatures": len(ast_hashes),
        "unique_entity_values": len(entity_hashes),
        "unique_corruption_combinations": len(corruption_hashes),
        "unique_generator_families": len(family_hashes),
    }
    notes = (
        "Section-22 minima are assessed against train records; total counts are informational and include every split.",
        "Semantic topicality is not inferred: paragraph coverage is structural target_ast evidence and can undercount semantically distinct paragraphs.",
        "Hard-negative, blank-line, merged-paragraph, and orthography categories require explicit tags or operations; unmarked text is not credited.",
        "Legal and unit text detectors use deliberately strict patterns and reversible mappings; unsupported surface forms remain uncounted.",
        "Diversity scans are linear. Target 3-grams sample the first 32 per record and retain a deterministic SHA-256 bottom-k of at most 100,000 values; other indicators use SHA-256 fingerprints rather than retaining full texts.",
    )
    return CoverageAuditReport(record_count, train_record_count, quota_results, diversity, notes)


def assert_mandatory_coverage(records: Iterable[Mapping[str, Any]]) -> CoverageAuditReport:
    """Return a passing report or raise :class:`CoverageAuditError` fail-closed."""

    report = audit_coverage(records)
    if not report.passed:
        raise CoverageAuditError(report)
    return report
