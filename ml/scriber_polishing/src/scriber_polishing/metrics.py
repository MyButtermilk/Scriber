"""Deterministic, fail-closed metrics for frozen transcript-polishing evaluations."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .ast_codec import document_from_dict, document_to_dict
from .document_ast import BlockType, Document
from .sst_parser import SSTParseError, parse_sst
from .sst_renderer import render_sst
from .target_renderer import render_html, render_markdown, render_plain_text


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """One frozen reference joined to one candidate SST output."""

    case_id: str
    source_text: str
    target_sst: str
    prediction_sst: str
    protected_values: tuple[str, ...] = ()
    target_plain_text: str | None = None
    target_ast: Mapping[str, Any] | None = None
    semantic_plan: Mapping[str, Any] | None = None
    address_mode: str | None = None
    slice_tags: tuple[str, ...] = ()
    is_identity: bool = False


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    """Auditable per-case evidence; critical errors are deterministic and binding."""

    case_id: str
    sst_valid: bool
    output_only_compliant: bool
    strict_exact_match: bool
    normalized_exact_match: bool
    identity_exact_match: bool | None
    protected_spans_exact: bool
    ast_exact_match: bool
    renderer_round_trip: bool
    safe_renderer: bool
    block_sequence_exact: bool
    reference_block_types: tuple[str, ...]
    prediction_block_types: tuple[str, ...]
    hard_content: dict[str, dict[str, tuple[str, ...] | bool]]
    quality_counts: dict[str, dict[str, int]]
    critical_errors: tuple[str, ...]
    metric_evidence: dict[str, dict[str, float | int]] = field(default_factory=dict)
    semantic_checks: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    total: int
    sst_parse_rate: float
    output_only_compliance_rate: float
    exact_match_rate: float
    normalized_exact_match: float
    punctuation_precision: float
    punctuation_recall: float
    punctuation_f1: float
    filler_precision: float
    filler_recall: float
    filler_f1: float
    repetition_cleanup_precision: float
    repetition_cleanup_recall: float
    repetition_cleanup_f1: float
    identity_total: int
    identity_exact_match: float
    protected_span_exact_rate: float
    ast_exact_match_rate: float
    renderer_round_trip_rate: float
    safe_renderer_rate: float
    block_sequence_exact_rate: float
    block_type_accuracy: float
    block_type_f1: float
    block_metrics: dict[str, dict[str, float | int]]
    hard_content_exact_rates: dict[str, float]
    hard_content_case_counts: dict[str, int]
    hard_gate_passed: bool
    critical_errors: dict[str, int]
    slice_reports: dict[str, dict[str, Any]]
    case_results: tuple[CaseEvaluation, ...]
    cer: float | None = None
    wer: float | None = None
    chrfpp_f2: float | None = None
    capitalization_accuracy: float | None = None
    sentence_boundary_f1: float | None = None
    self_correction_recovery_accuracy: float | None = None
    format_command_recovery_accuracy: float | None = None
    grammar_recovery_accuracy: float | None = None
    spelling_recovery_accuracy: float | None = None
    structural_metrics: dict[str, float | None] = field(default_factory=dict)
    domain_metrics: dict[str, float | None] = field(default_factory=dict)
    semantic_hard_check_counts: dict[str, int] = field(default_factory=dict)
    metric_availability: dict[str, dict[str, Any]] = field(default_factory=dict)


_AMOUNT = re.compile(
    r"(?<![\w])(?:€\s*)?[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2})?\s*(?:€|EUR)(?![\w])",
    re.IGNORECASE,
)
_PERCENTAGE = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)?\s*%(?![\w])")
_DATE = re.compile(r"(?<![\w])(?:\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2})(?![\w])")
_TIME = re.compile(r"(?<![\w])\d{1,2}:\d{2}(?::\d{2})?(?:\s*Uhr)?(?![\w])", re.IGNORECASE)
_LEGAL_REFERENCE = re.compile(
    r"(?:"
    r"§§?\s*\d+[a-zA-Z]*"
    r"(?:\s+(?:und\s+\d+[a-zA-Z]*|Abs(?:atz|\.)\s*\d+|Satz\s*\d+|"
    r"(?:Nummer|Nr\.)\s*\d+|(?:Buchstabe|Buchst\.)\s*[a-z]))*"
    r"(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9]{1,15})?"
    r"|\b\d+\s+[A-Z]\s+\d+/\d+(?:\s+[A-Z])?\b"
    r"|ECLI:[A-Z]{2,}:[A-Z0-9]+(?::[A-Z0-9.]+)+"
    r"|Az\.\s*[\w./ -]+?(?=(?:,|\.|;|$))"
    r")"
)
_UNIT = re.compile(
    r"(?<![\w])[-+]?\d+(?:[.,]\d+)?\s*"
    r"(?:kWh/\(m²·a\)|kWh/m²a|€/m²|m²|m³|mm²|cm²|km²|mm³|cm³|"
    r"kWh|MWh|Wh|kW|MW|W|km/h|m/s|kg|mg|km|cm|mm|°C|K|l|ml|m)(?![\w])"
)
_NUMBER = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)*(?![\w])")
_NEGATION = re.compile(
    r"\b(?:nicht|nichts|kein(?:e|en|em|er|es)?|nie|niemals|weder|ohne|unzulässig)\b",
    re.IGNORECASE,
)
_CONDITION = re.compile(
    r"\b(?:unter\s+der\s+Voraussetzung,\s+dass|sofern|falls|wenn|vorbehaltlich|"
    r"unbeschadet|es\s+sei\s+denn)\b",
    re.IGNORECASE,
)
_MODALITY = re.compile(
    r"\b(?:muss|musst|müssen|müsst|kann|kannst|können|könnt|soll|sollst|sollen|sollt|"
    r"wird|wirst|werden|werdet|darf|darfst|dürfen|dürft|könnte|müsste|sollte|würde)\b",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[,.!?;:]")
_WORD = re.compile(r"[A-Za-zÄÖÜäöüß]+")
_NONSEMANTIC_FILLERS = frozenset({"äh", "ähm", "hm", "hmm"})
_MAX_EXACT_DISTANCE_CELLS = 4_000_000
_ANNOTATION_METRIC_KINDS = {
    "capitalization_accuracy": "accuracy",
    "sentence_boundary_f1": "f1",
    "self_correction_recovery_accuracy": "accuracy",
    "format_command_recovery_accuracy": "accuracy",
    "grammar_recovery_accuracy": "accuracy",
    "spelling_recovery_accuracy": "accuracy",
}
_OPERATION_TAG_TO_METRIC = {
    "operation:self_correction": "self_correction_recovery_accuracy",
    "operation:spoken_formatting": "format_command_recovery_accuracy",
    "operation:grammar_error": "grammar_recovery_accuracy",
    "operation:spelling_error": "spelling_recovery_accuracy",
    "operation:context_spelling_error": "spelling_recovery_accuracy",
}
_STRUCTURAL_METRIC_KINDS = {
    "heading_f1": "f1",
    "paragraph_boundary_f1": "f1",
    "list_type_accuracy": "accuracy",
    "list_nesting_accuracy": "accuracy",
    "inline_emphasis_f1": "f1",
}
_DOMAIN_METRIC_KINDS = {
    "unit_symbol_exact_rate": "accuracy",
    "legal_reference_exact_rate": "accuracy",
    "orthography_accuracy": "accuracy",
    "address_pronoun_accuracy": "accuracy",
}
_SEMANTIC_CHECK_NAMES = (
    "semantic_addition",
    "semantic_omission",
    "semantic_reorder",
    "semantic_question_answer",
    "semantic_summary",
    "semantic_entity_changed",
)
_SEMANTIC_ADDITION_CUE = re.compile(
    r"(?im)(?:^|\n)\s*(?:antwort\s*:|zusammenfassung\s*:|fazit\s*:|kurz gesagt\s*:|tl;dr\s*:|als ki\b)"
)
_SEMANTIC_SUMMARY_CUE = re.compile(r"(?i)\b(?:zusammenfassung|fazit|kurz gesagt|tl;dr)\b")
_ADDRESS_MODE_PRONOUNS = {
    "formal_sie": re.compile(r"\b(?:Sie|Ihnen|Ihr(?:e|en|em|er|es)?)\b"),
    "personal_du_capitalized": re.compile(r"\b(?:Du|Dir|Dich|Dein(?:e|en|em|er|es)?)\b"),
    "personal_du_lowercase": re.compile(r"\b(?:du|dir|dich|dein(?:e|en|em|er|es)?)\b"),
}

_UNIT_DIMENSIONS = {
    "kwh/(m2·a)": "specific_energy",
    "kwh/m2a": "specific_energy",
    "€/m2": "area_price",
    "m2": "area",
    "mm2": "area",
    "cm2": "area",
    "km2": "area",
    "m3": "volume",
    "mm3": "volume",
    "cm3": "volume",
    "wh": "energy",
    "kwh": "energy",
    "mwh": "energy",
    "w": "power",
    "kw": "power",
    "mw": "power",
    "km/h": "speed",
    "m/s": "speed",
    "kg": "mass",
    "mg": "mass",
    "m": "length",
    "km": "length",
    "cm": "length",
    "mm": "length",
    "°c": "temperature",
    "k": "temperature",
    "l": "volume",
    "ml": "volume",
}

_HARD_CATEGORY_TO_ERRORS = {
    "amounts": ("changed_amount",),
    "conditions": ("changed_condition",),
    "dates": ("changed_date_or_time",),
    "legal_references": ("changed_legal_reference",),
    "modalities": ("changed_modality",),
    "negations": ("changed_negation",),
    "numbers": ("changed_number",),
    "percentages": ("changed_percentage",),
    "protected_spans": ("protected_span_changed", "damaged_protected_span"),
    "times": ("changed_date_or_time",),
    "unit_dimensions": ("changed_unit_dimension",),
    "units": ("false_unit_normalization",),
}


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _exact_edit_distance(reference: tuple[str, ...] | str, candidate: tuple[str, ...] | str) -> int | None:
    """Return exact Levenshtein distance within a bounded deterministic work budget."""

    if len(reference) * len(candidate) > _MAX_EXACT_DISTANCE_CELLS:
        return None
    if len(reference) < len(candidate):
        reference, candidate = candidate, reference
    previous = list(range(len(candidate) + 1))
    for reference_index, reference_value in enumerate(reference, start=1):
        current = [reference_index]
        for candidate_index, candidate_value in enumerate(candidate, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[candidate_index] + 1,
                    previous[candidate_index - 1] + (reference_value != candidate_value),
                )
            )
        previous = current
    return previous[-1]


def _ngram_counter(values: tuple[str, ...], order: int) -> Counter[tuple[str, ...]]:
    if len(values) < order:
        return Counter()
    return Counter(tuple(values[index : index + order]) for index in range(len(values) - order + 1))


def _f_beta(precision: float, recall: float, *, beta: float) -> float:
    if precision == 0.0 and recall == 0.0:
        return 0.0
    beta_squared = beta * beta
    return (1 + beta_squared) * precision * recall / (beta_squared * precision + recall)


def _chrfpp_f2(reference: str, candidate: str) -> float:
    """Macro chrF++: character 1..6 and word 1..2 multiset F2 components."""

    reference_characters = tuple(_normalized(reference))
    candidate_characters = tuple(_normalized(candidate))
    reference_words = tuple(token.casefold() for token in _WORD.findall(_normalized(reference)))
    candidate_words = tuple(token.casefold() for token in _WORD.findall(_normalized(candidate)))
    component_scores: list[float] = []
    for values, predicted_values, maximum_order in (
        (reference_characters, candidate_characters, 6),
        (reference_words, candidate_words, 2),
    ):
        for order in range(1, maximum_order + 1):
            expected = _ngram_counter(values, order)
            predicted = _ngram_counter(predicted_values, order)
            if not expected and not predicted:
                component_scores.append(1.0)
                continue
            true_positive = sum((expected & predicted).values())
            precision = true_positive / sum(predicted.values()) if predicted else 0.0
            recall = true_positive / sum(expected.values()) if expected else 0.0
            component_scores.append(_f_beta(precision, recall, beta=2.0))
    return sum(component_scores) / len(component_scores)


def _text_metric_evidence(target: str, candidate: str) -> dict[str, dict[str, float | int]]:
    normalized_target = _normalized(target)
    normalized_candidate = _normalized(candidate)
    evidence: dict[str, dict[str, float | int]] = {
        "chrfpp_f2": {"score": _chrfpp_f2(normalized_target, normalized_candidate)},
    }
    character_distance = _exact_edit_distance(normalized_target, normalized_candidate)
    if normalized_target and character_distance is not None:
        evidence["cer"] = {"distance": character_distance, "reference": len(normalized_target)}
    reference_words = tuple(token.casefold() for token in _WORD.findall(normalized_target))
    candidate_words = tuple(token.casefold() for token in _WORD.findall(normalized_candidate))
    word_distance = _exact_edit_distance(reference_words, candidate_words)
    if reference_words and word_distance is not None:
        evidence["wer"] = {"distance": word_distance, "reference": len(reference_words)}
    return evidence


def _text_similarity_report(results: tuple[CaseEvaluation, ...]) -> tuple[dict[str, float | None], dict[str, dict[str, Any]]]:
    values: dict[str, float | None] = {}
    availability: dict[str, dict[str, Any]] = {}
    for metric_name in ("cer", "wer"):
        entries = [result.metric_evidence.get(metric_name) for result in results]
        if any(entry is None for entry in entries):
            values[metric_name] = None
            availability[metric_name] = {
                "status": "unknown",
                "reason": "reference_has_no_tokens_or_exact_distance_budget_exceeded",
            }
            continue
        typed_entries = tuple(entry for entry in entries if entry is not None)
        denominator = sum(int(entry["reference"]) for entry in typed_entries)
        values[metric_name] = sum(int(entry["distance"]) for entry in typed_entries) / denominator
        availability[metric_name] = {
            "status": "measured",
            "case_count": len(typed_entries),
            "reference_units": denominator,
        }
    chrf_entries = [result.metric_evidence.get("chrfpp_f2") for result in results]
    if any(entry is None for entry in chrf_entries):
        values["chrfpp_f2"] = None
        availability["chrfpp_f2"] = {"status": "unknown", "reason": "missing_text_evidence"}
    else:
        typed_chrf_entries = tuple(entry for entry in chrf_entries if entry is not None)
        values["chrfpp_f2"] = sum(float(entry["score"]) for entry in typed_chrf_entries) / len(
            typed_chrf_entries
        )
        availability["chrfpp_f2"] = {"status": "measured", "case_count": len(typed_chrf_entries)}
    return values, availability


def _word_sequence(text: str) -> tuple[str, ...]:
    return tuple(_WORD.findall(text))


def _sentence_boundaries(text: str) -> tuple[int, ...]:
    word_end_offsets = [match.end() for match in _WORD.finditer(text)]
    boundaries: list[int] = []
    for match in re.finditer(r"[.!?]+", text):
        completed_words = sum(end <= match.start() for end in word_end_offsets)
        if completed_words:
            boundaries.append(completed_words)
    return tuple(dict.fromkeys(boundaries))


def _accuracy_evidence(total: int, correct: int) -> dict[str, int]:
    return {"expected": total, "predicted": total, "true_positive": correct}


def _annotation_metric_evidence(
    case: EvaluationCase,
    target: str,
    candidate: str,
    *,
    target_document: Document,
    prediction_document: Document | None,
) -> dict[str, dict[str, float | int]]:
    evidence: dict[str, dict[str, float | int]] = {}
    source_words = _word_sequence(case.source_text)
    target_words = _word_sequence(target)
    candidate_words = _word_sequence(candidate)
    source_and_target_align = (
        len(source_words) == len(target_words)
        and tuple(value.casefold() for value in source_words) == tuple(value.casefold() for value in target_words)
    )
    if source_and_target_align:
        changed_case_indexes = tuple(
            index for index, (source, expected) in enumerate(zip(source_words, target_words, strict=True)) if source != expected
        )
        if changed_case_indexes:
            correct = sum(
                index < len(candidate_words)
                and candidate_words[index] == target_words[index]
                and candidate_words[index].casefold() == target_words[index].casefold()
                for index in changed_case_indexes
            )
            evidence["capitalization_accuracy"] = _accuracy_evidence(len(changed_case_indexes), correct)

        source_boundaries = _sentence_boundaries(case.source_text)
        target_boundaries = _sentence_boundaries(target)
        if source_boundaries != target_boundaries:
            candidate_boundaries = _sentence_boundaries(candidate)
            evidence["sentence_boundary_f1"] = _counter_prf(
                Counter(target_boundaries), Counter(candidate_boundaries)
            )

    target_is_exact = prediction_document is not None and candidate == target
    for tag, metric_name in _OPERATION_TAG_TO_METRIC.items():
        if tag in case.slice_tags:
            recovered = prediction_document == target_document if tag == "operation:spoken_formatting" else target_is_exact
            evidence[metric_name] = _accuracy_evidence(1, int(recovered))
    return evidence


def _annotation_metric_report(
    results: tuple[CaseEvaluation, ...],
) -> tuple[dict[str, float | None], dict[str, dict[str, Any]]]:
    values: dict[str, float | None] = {}
    availability: dict[str, dict[str, Any]] = {}
    for metric_name, kind in _ANNOTATION_METRIC_KINDS.items():
        entries = [result.metric_evidence[metric_name] for result in results if metric_name in result.metric_evidence]
        if not entries:
            values[metric_name] = None
            availability[metric_name] = {
                "status": "unknown",
                "reason": "no_reference_annotation_supports_this_metric",
            }
            continue
        expected = sum(int(entry["expected"]) for entry in entries)
        predicted = sum(int(entry["predicted"]) for entry in entries)
        true_positive = sum(int(entry["true_positive"]) for entry in entries)
        if kind == "f1":
            values[metric_name] = float(_prf(expected, predicted, true_positive)["f1"])
        else:
            values[metric_name] = true_positive / expected
        availability[metric_name] = {
            "status": "measured",
            "case_count": len(entries),
            "reference_units": expected,
        }
    return values, availability


def _counter_evidence(expected: Counter[str], predicted: Counter[str]) -> dict[str, int]:
    return {
        "expected": sum(expected.values()),
        "predicted": sum(predicted.values()),
        "true_positive": sum((expected & predicted).values()),
    }


def _sequence_accuracy_evidence(expected: tuple[str, ...], candidate: tuple[str, ...]) -> dict[str, int] | None:
    if not expected:
        return None
    return _accuracy_evidence(
        len(expected),
        sum(left == right for left, right in zip(expected, candidate, strict=False)),
    )


def _non_plain_inline_spans(document: Document) -> Counter[str]:
    spans: Counter[str] = Counter()
    for block_index, block in enumerate(document.blocks):
        for inline in block.inlines:
            if inline.style.value != "plain":
                spans.update((f"block:{block_index}:{inline.style.value}:{inline.text}",))
        for item_index, item in enumerate(block.items):
            for inline in item.inlines:
                if inline.style.value != "plain":
                    spans.update((f"item:{block_index}:{item_index}:{inline.style.value}:{inline.text}",))
    return spans


def _structural_metric_evidence(
    reference: Document,
    prediction: Document | None,
) -> dict[str, dict[str, float | int]]:
    prediction_blocks = prediction.blocks if prediction is not None else ()
    evidence: dict[str, dict[str, float | int]] = {}
    heading_types = {BlockType.HEADING_1, BlockType.HEADING_2}
    expected_headings = Counter(block.type.value for block in reference.blocks if block.type in heading_types)
    candidate_headings = Counter(block.type.value for block in prediction_blocks if block.type in heading_types)
    if expected_headings or candidate_headings:
        evidence["structural.heading_f1"] = _counter_evidence(expected_headings, candidate_headings)

    expected_paragraph_boundaries = Counter(
        str(index) for index, block in enumerate(reference.blocks) if block.type is BlockType.PARAGRAPH
    )
    candidate_paragraph_boundaries = Counter(
        str(index) for index, block in enumerate(prediction_blocks) if block.type is BlockType.PARAGRAPH
    )
    if expected_paragraph_boundaries or candidate_paragraph_boundaries:
        evidence["structural.paragraph_boundary_f1"] = _counter_evidence(
            expected_paragraph_boundaries, candidate_paragraph_boundaries
        )

    list_types = {BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST}
    expected_lists = tuple(block for block in reference.blocks if block.type in list_types)
    candidate_lists = tuple(block for block in prediction_blocks if block.type in list_types)
    list_positions = max(len(expected_lists), len(candidate_lists))
    if list_positions:
        exact_types = sum(
            expected.type == candidate.type
            for expected, candidate in zip(expected_lists, candidate_lists, strict=False)
        )
        exact_nesting = sum(
            expected.type == candidate.type
            and tuple(item.level for item in expected.items) == tuple(item.level for item in candidate.items)
            for expected, candidate in zip(expected_lists, candidate_lists, strict=False)
        )
        evidence["structural.list_type_accuracy"] = _accuracy_evidence(list_positions, exact_types)
        evidence["structural.list_nesting_accuracy"] = _accuracy_evidence(list_positions, exact_nesting)

    expected_spans = _non_plain_inline_spans(reference)
    candidate_spans = _non_plain_inline_spans(prediction) if prediction is not None else Counter()
    if expected_spans or candidate_spans:
        evidence["structural.inline_emphasis_f1"] = _counter_evidence(expected_spans, candidate_spans)
    return evidence


def _domain_metric_evidence(
    case: EvaluationCase,
    target: str,
    candidate: str,
) -> dict[str, dict[str, float | int]]:
    evidence: dict[str, dict[str, float | int]] = {}
    unit_evidence = _sequence_accuracy_evidence(_matches(_UNIT, target), _matches(_UNIT, candidate))
    if unit_evidence is not None:
        evidence["domain.unit_symbol_exact_rate"] = unit_evidence
    legal_evidence = _sequence_accuracy_evidence(
        _matches(_LEGAL_REFERENCE, target), _matches(_LEGAL_REFERENCE, candidate)
    )
    if legal_evidence is not None:
        evidence["domain.legal_reference_exact_rate"] = legal_evidence
    if {"operation:spelling_error", "operation:context_spelling_error"} & set(case.slice_tags):
        evidence["domain.orthography_accuracy"] = _accuracy_evidence(1, int(candidate == target))
    address_pronoun = _ADDRESS_MODE_PRONOUNS.get(case.address_mode)
    if address_pronoun is not None:
        expected_pronouns = tuple(address_pronoun.findall(target))
        candidate_pronouns = tuple(address_pronoun.findall(candidate))
        pronoun_evidence = _sequence_accuracy_evidence(expected_pronouns, candidate_pronouns)
        if pronoun_evidence is not None:
            evidence["domain.address_pronoun_accuracy"] = pronoun_evidence
    return evidence


def _named_metric_report(
    results: tuple[CaseEvaluation, ...],
    metric_kinds: Mapping[str, str],
    *,
    prefix: str,
    unavailable_reason: str,
) -> tuple[dict[str, float | None], dict[str, dict[str, Any]]]:
    values: dict[str, float | None] = {}
    availability: dict[str, dict[str, Any]] = {}
    for metric_name, kind in metric_kinds.items():
        evidence_key = f"{prefix}.{metric_name}"
        entries = [result.metric_evidence[evidence_key] for result in results if evidence_key in result.metric_evidence]
        if not entries:
            values[metric_name] = None
            availability[evidence_key] = {"status": "unknown", "reason": unavailable_reason}
            continue
        expected = sum(int(entry["expected"]) for entry in entries)
        predicted = sum(int(entry["predicted"]) for entry in entries)
        true_positive = sum(int(entry["true_positive"]) for entry in entries)
        values[metric_name] = (
            float(_prf(expected, predicted, true_positive)["f1"])
            if kind == "f1"
            else true_positive / expected
        )
        availability[evidence_key] = {
            "status": "measured",
            "case_count": len(entries),
            "reference_units": expected,
        }
    return values, availability


def _semantic_fact_entries(plan: Mapping[str, Any] | None, target: str) -> tuple[tuple[int, tuple[str, ...]], ...]:
    if not isinstance(plan, Mapping):
        return ()
    facts = plan.get("facts")
    if not isinstance(facts, list):
        return ()
    result: list[tuple[int, tuple[str, ...]]] = []
    for index, fact in enumerate(facts):
        if not isinstance(fact, Mapping):
            continue
        raw_values = fact.get("protected_values")
        if not isinstance(raw_values, list):
            continue
        values = tuple(
            value for value in raw_values if isinstance(value, str) and value and value in target
        )
        if not values:
            continue
        order = fact.get("order")
        result.append((order if isinstance(order, int) else index + 1, values))
    return tuple(sorted(result, key=lambda item: item[0]))


def _semantic_plan_checks(case: EvaluationCase, target: str, candidate: str) -> dict[str, bool]:
    if not isinstance(case.semantic_plan, Mapping) or not isinstance(case.semantic_plan.get("facts"), list):
        return {}
    checks = {name: False for name in _SEMANTIC_CHECK_NAMES}
    facts = _semantic_fact_entries(case.semantic_plan, target)
    if not facts:
        return {}
    fact_positions: list[tuple[int, int, int]] = []
    for fact_index, (_order, values) in enumerate(facts):
        target_positions = tuple(target.find(value) for value in values)
        candidate_positions = tuple(candidate.find(value) for value in values)
        if any(position < 0 for position in candidate_positions):
            checks["semantic_omission"] = True
            continue
        fact_positions.append(
            (fact_index, min(target_positions), min(candidate_positions))
        )
    if len(fact_positions) >= 2:
        expected_order = tuple(
            fact_index
            for fact_index, _target_position, _candidate_position in sorted(
                fact_positions,
                key=lambda item: (item[1], item[0]),
            )
        )
        observed_order = tuple(
            fact_index
            for fact_index, _target_position, _candidate_position in sorted(
                fact_positions,
                key=lambda item: (item[2], item[0]),
            )
        )
        checks["semantic_reorder"] = observed_order != expected_order

    entities = case.semantic_plan.get("entities")
    if isinstance(entities, list):
        for entity in entities:
            if not isinstance(entity, Mapping) or entity.get("protected") is not True:
                continue
            value = entity.get("value")
            if isinstance(value, str) and value and value in target and value not in candidate:
                checks["semantic_entity_changed"] = True
                break

    source_and_target = f"{case.source_text}\n{target}"
    candidate_has_addition_cue = _SEMANTIC_ADDITION_CUE.search(candidate) is not None
    if candidate_has_addition_cue and _SEMANTIC_ADDITION_CUE.search(source_and_target) is None:
        checks["semantic_addition"] = True
    if _SEMANTIC_SUMMARY_CUE.search(candidate) and _SEMANTIC_SUMMARY_CUE.search(source_and_target) is None:
        checks["semantic_summary"] = True
    if "?" in case.source_text and candidate_has_addition_cue:
        checks["semantic_question_answer"] = True
    return checks


def _semantic_summary(results: tuple[CaseEvaluation, ...]) -> tuple[dict[str, int], dict[str, Any]]:
    if any(not result.semantic_checks for result in results):
        return {}, {"status": "unknown", "reason": "semantic_plan_not_attached_to_evaluation_case"}
    applicable = results
    return (
        {
            check_name: sum(result.semantic_checks.get(check_name, False) for result in applicable)
            for check_name in _SEMANTIC_CHECK_NAMES
        },
        {"status": "measured", "case_count": len(applicable)},
    )


def _matches(pattern: re.Pattern[str], text: str, *, casefold: bool = False) -> tuple[str, ...]:
    values: Iterable[str] = (_normalized(match.group(0)) for match in pattern.finditer(text))
    if casefold:
        values = (value.casefold() for value in values)
    return tuple(values)


def _explicit_value_sequence(values: tuple[str, ...], text: str) -> tuple[str, ...]:
    located: list[tuple[int, str]] = []
    for value in dict.fromkeys(values):
        start = 0
        while (position := text.find(value, start)) >= 0:
            located.append((position, value))
            start = position + len(value)
    return tuple(value for _, value in sorted(located))


def _protected_values(values: tuple[str, ...], target: str, candidate: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    expected = _explicit_value_sequence(values, target)
    for value in dict.fromkeys(values):
        if value not in target:
            raise ValueError(f"protected value is absent from frozen reference: {value!r}")
    return expected, _explicit_value_sequence(values, candidate)


def _unit_dimensions(text: str) -> tuple[str, ...]:
    dimensions: list[str] = []
    for match in _UNIT.finditer(text):
        unit = re.sub(r"^[-+]?\d+(?:[.,]\d+)?\s*", "", _normalized(match.group(0))).casefold()
        dimension = _UNIT_DIMENSIONS.get(unit)
        if dimension is None:
            raise AssertionError(f"unit extractor emitted an unmapped unit: {unit}")
        dimensions.append(dimension)
    return tuple(dimensions)


def _ordered_difference(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    remaining = Counter(right)
    difference: list[str] = []
    for value in left:
        if remaining[value]:
            remaining[value] -= 1
        else:
            difference.append(value)
    return tuple(difference)


def _sequence_diff(expected: tuple[str, ...], actual: tuple[str, ...]) -> dict[str, tuple[str, ...] | bool]:
    return {
        "exact": expected == actual,
        "reference": expected,
        "candidate": actual,
        "missing": _ordered_difference(expected, actual),
        "added": _ordered_difference(actual, expected),
    }


def _hard_content(target: str, candidate: str, protected_values: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    extractors: dict[str, Callable[[str], tuple[str, ...]]] = {
        "amounts": lambda value: _matches(_AMOUNT, value),
        "conditions": lambda value: _matches(_CONDITION, value, casefold=True),
        "dates": lambda value: _matches(_DATE, value),
        "legal_references": lambda value: _matches(_LEGAL_REFERENCE, value),
        "modalities": lambda value: _matches(_MODALITY, value, casefold=True),
        "negations": lambda value: _matches(_NEGATION, value, casefold=True),
        "numbers": lambda value: _matches(_NUMBER, value),
        "percentages": lambda value: _matches(_PERCENTAGE, value),
        "times": lambda value: _matches(_TIME, value, casefold=True),
        "unit_dimensions": _unit_dimensions,
        "units": lambda value: _matches(_UNIT, value),
    }
    expected_protected, actual_protected = _protected_values(protected_values, target, candidate)
    result = {"protected_spans": _sequence_diff(expected_protected, actual_protected)}
    for name, extractor in extractors.items():
        result[name] = _sequence_diff(extractor(target), extractor(candidate))
    return dict(sorted(result.items()))


def _safe_renderer(document: Document) -> tuple[bool, bool]:
    try:
        canonical_sst = render_sst(document)
        render_plain_text(document)
        render_markdown(document)
        render_html(document)
        round_tripped = document_from_dict(document_to_dict(document))
        canonical_document = parse_sst(canonical_sst)
    except (TypeError, ValueError):
        return False, False
    return True, round_tripped == document and canonical_document == document


def _frozen_reference(case: EvaluationCase) -> tuple[Document, str]:
    try:
        target_document = parse_sst(case.target_sst)
    except (SSTParseError, ValueError) as error:
        raise ValueError(f"case {case.case_id} has invalid frozen reference SST: {error}") from error
    target_plain = render_plain_text(target_document)
    if case.target_plain_text is not None and case.target_plain_text != target_plain:
        raise ValueError(f"case {case.case_id} reference plain text differs from reference SST")
    if case.target_ast is not None:
        try:
            reference_ast = document_from_dict(case.target_ast)
        except (TypeError, ValueError) as error:
            raise ValueError(f"case {case.case_id} reference AST is invalid: {error}") from error
        if render_sst(reference_ast) != render_sst(target_document):
            raise ValueError(f"case {case.case_id} reference AST differs from reference SST")
    return target_document, target_plain


def _prf(expected: int, predicted: int, true_positive: int) -> dict[str, float | int]:
    precision = true_positive / predicted if predicted else float(expected == 0)
    recall = true_positive / expected if expected else float(predicted == 0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "expected": expected,
        "predicted": predicted,
        "true_positive": true_positive,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _block_report(results: tuple[CaseEvaluation, ...]) -> dict[str, Any]:
    reference_counts: Counter[str] = Counter()
    prediction_counts: Counter[str] = Counter()
    true_positive: Counter[str] = Counter()
    aligned_matches = 0
    aligned_total = 0
    for result in results:
        expected = Counter(result.reference_block_types)
        predicted = Counter(result.prediction_block_types)
        reference_counts.update(expected)
        prediction_counts.update(predicted)
        true_positive.update(expected & predicted)
        aligned_matches += sum(
            left == right
            for left, right in zip(
                result.reference_block_types,
                result.prediction_block_types,
                strict=False,
            )
        )
        aligned_total += max(
            len(result.reference_block_types),
            len(result.prediction_block_types),
        )
    block_types = tuple(sorted(set(reference_counts) | set(prediction_counts)))
    micro = _prf(
        sum(reference_counts.values()),
        sum(prediction_counts.values()),
        sum(true_positive.values()),
    )
    return {
        "block_type_accuracy": aligned_matches / aligned_total if aligned_total else 1.0,
        "block_type_f1": micro["f1"],
        "block_metrics": {
            block_type: _prf(
                reference_counts[block_type],
                prediction_counts[block_type],
                true_positive[block_type],
            )
            for block_type in block_types
        },
    }


def _applicable_hard_results(
    results: tuple[CaseEvaluation, ...],
    category: str,
) -> tuple[CaseEvaluation, ...]:
    return tuple(
        result
        for result in results
        if result.hard_content[category]["reference"] or result.hard_content[category]["candidate"]
    )


def _hard_summary(
    results: tuple[CaseEvaluation, ...],
    categories: tuple[str, ...],
) -> tuple[dict[str, float], dict[str, int]]:
    rates: dict[str, float] = {}
    counts: dict[str, int] = {}
    for category in categories:
        applicable = _applicable_hard_results(results, category)
        counts[category] = len(applicable)
        rates[category] = (
            sum(bool(result.hard_content[category]["exact"]) for result in applicable) / len(applicable)
            if applicable
            else 1.0
        )
    return rates, counts


def _counter_prf(expected: Counter[str], predicted: Counter[str]) -> dict[str, int]:
    return {
        "expected": sum(expected.values()),
        "predicted": sum(predicted.values()),
        "true_positive": sum((expected & predicted).values()),
    }


def _punctuation_counts(target: str, candidate: str) -> dict[str, int]:
    return _counter_prf(Counter(_PUNCTUATION.findall(target)), Counter(_PUNCTUATION.findall(candidate)))


def _word_counter(text: str) -> Counter[str]:
    return Counter(token.casefold() for token in _WORD.findall(text))


def _positive_counter_difference(left: Counter[str], right: Counter[str]) -> Counter[str]:
    return left - right


def _filler_counts(source: str, target: str, candidate: str) -> dict[str, int]:
    source_fillers = Counter(
        {token: count for token, count in _word_counter(source).items() if token in _NONSEMANTIC_FILLERS}
    )
    target_fillers = Counter(
        {token: count for token, count in _word_counter(target).items() if token in _NONSEMANTIC_FILLERS}
    )
    candidate_fillers = Counter(
        {token: count for token, count in _word_counter(candidate).items() if token in _NONSEMANTIC_FILLERS}
    )
    expected = _positive_counter_difference(source_fillers, target_fillers)
    predicted = _positive_counter_difference(source_fillers, candidate_fillers)
    return _counter_prf(expected, predicted)


def _adjacent_repetition_count(text: str) -> int:
    tokens = [token.casefold() for token in _WORD.findall(text)]
    return sum(left == right for left, right in zip(tokens, tokens[1:], strict=False))


def _repetition_counts(source: str, target: str, candidate: str) -> dict[str, int]:
    source_count = _adjacent_repetition_count(source)
    expected = max(0, source_count - _adjacent_repetition_count(target))
    predicted = max(0, source_count - _adjacent_repetition_count(candidate))
    return {
        "expected": expected,
        "predicted": predicted,
        "true_positive": min(expected, predicted),
    }


def _quality_report(results: tuple[CaseEvaluation, ...]) -> dict[str, float]:
    report: dict[str, float] = {}
    for category, prefix in (
        ("punctuation", "punctuation"),
        ("filler", "filler"),
        ("repetition_cleanup", "repetition_cleanup"),
    ):
        expected = sum(result.quality_counts[category]["expected"] for result in results)
        predicted = sum(result.quality_counts[category]["predicted"] for result in results)
        true_positive = sum(result.quality_counts[category]["true_positive"] for result in results)
        scores = _prf(expected, predicted, true_positive)
        report[f"{prefix}_precision"] = float(scores["precision"])
        report[f"{prefix}_recall"] = float(scores["recall"])
        report[f"{prefix}_f1"] = float(scores["f1"])
    return report


def _structure_critical_errors(
    reference_types: tuple[BlockType, ...],
    prediction_types: tuple[BlockType, ...],
) -> tuple[str, ...]:
    heading_types = {BlockType.HEADING_1, BlockType.HEADING_2}
    reference_headings = tuple(block_type for block_type in reference_types if block_type in heading_types)
    prediction_headings = tuple(block_type for block_type in prediction_types if block_type in heading_types)
    errors: list[str] = []
    if len(prediction_headings) > len(reference_headings):
        errors.append("invented_heading")
    if len(prediction_headings) < len(reference_headings):
        errors.append("missed_heading")
    if len(prediction_headings) == len(reference_headings) and prediction_headings != reference_headings:
        errors.append("wrong_heading_level")
    return tuple(errors)


def _slice_report(results: tuple[CaseEvaluation, ...]) -> dict[str, Any]:
    total = len(results)
    error_counts = Counter(error for result in results for error in result.critical_errors)
    categories = tuple(sorted(results[0].hard_content)) if results else ()
    identity = tuple(result for result in results if result.identity_exact_match is not None)
    hard_rates, hard_counts = _hard_summary(results, categories)
    text_similarity, metric_availability = _text_similarity_report(results)
    annotation_metrics, annotation_availability = _annotation_metric_report(results)
    structural_metrics, structural_availability = _named_metric_report(
        results,
        _STRUCTURAL_METRIC_KINDS,
        prefix="structural",
        unavailable_reason="no_reference_ast_feature_supports_this_metric",
    )
    domain_metrics, domain_availability = _named_metric_report(
        results,
        _DOMAIN_METRIC_KINDS,
        prefix="domain",
        unavailable_reason="no_reference_annotation_supports_this_metric",
    )
    semantic_hard_check_counts, semantic_availability = _semantic_summary(results)
    return {
        "total": total,
        "sst_parse_rate": sum(result.sst_valid for result in results) / total,
        "output_only_compliance_rate": sum(result.output_only_compliant for result in results) / total,
        "exact_match_rate": sum(result.strict_exact_match for result in results) / total,
        "normalized_exact_match": sum(result.normalized_exact_match for result in results) / total,
        **text_similarity,
        **annotation_metrics,
        "structural_metrics": structural_metrics,
        "domain_metrics": domain_metrics,
        "semantic_hard_check_counts": semantic_hard_check_counts,
        "metric_availability": {
            **metric_availability,
            **annotation_availability,
            **structural_availability,
            **domain_availability,
            "semantic_hard_checks": semantic_availability,
        },
        **_quality_report(results),
        "identity_total": len(identity),
        "identity_exact_match": (
            sum(bool(result.identity_exact_match) for result in identity) / len(identity) if identity else 1.0
        ),
        "protected_span_exact_rate": sum(result.protected_spans_exact for result in results) / total,
        "ast_exact_match_rate": sum(result.ast_exact_match for result in results) / total,
        "renderer_round_trip_rate": sum(result.renderer_round_trip for result in results) / total,
        "safe_renderer_rate": sum(result.safe_renderer for result in results) / total,
        "block_sequence_exact_rate": sum(result.block_sequence_exact for result in results) / total,
        **_block_report(results),
        "hard_content_exact_rates": hard_rates,
        "hard_content_case_counts": hard_counts,
        "hard_gate_passed": not error_counts,
        "critical_errors": dict(sorted(error_counts.items())),
    }


def evaluate_predictions(cases: Iterable[EvaluationCase]) -> EvaluationReport:
    """Evaluate exact joined cases; malformed references or duplicate IDs abort the run."""

    materialized = tuple(cases)
    if not materialized:
        raise ValueError("at least one evaluation case is required")
    if any(not case.case_id for case in materialized):
        raise ValueError("case_id values must be non-empty")
    if len({case.case_id for case in materialized}) != len(materialized):
        raise ValueError("case_id values must be unique")

    case_results: list[CaseEvaluation] = []
    reference_block_counts: Counter[BlockType] = Counter()
    prediction_block_counts: Counter[BlockType] = Counter()
    block_true_positive: Counter[BlockType] = Counter()
    aligned_type_matches = 0
    aligned_type_total = 0

    for case in materialized:
        target_document, target_plain = _frozen_reference(case)
        prediction_document: Document | None
        try:
            prediction_document = parse_sst(case.prediction_sst)
        except (SSTParseError, ValueError):
            prediction_document = None

        prediction_plain = (
            render_plain_text(prediction_document) if prediction_document is not None else case.prediction_sst
        )
        quality_counts = {
            "punctuation": _punctuation_counts(target_plain, prediction_plain),
            "filler": _filler_counts(case.source_text, target_plain, prediction_plain),
            "repetition_cleanup": _repetition_counts(case.source_text, target_plain, prediction_plain),
        }
        metric_evidence = {
            **_text_metric_evidence(target_plain, prediction_plain),
            **_annotation_metric_evidence(
                case,
                target_plain,
                prediction_plain,
                target_document=target_document,
                prediction_document=prediction_document,
            ),
            **_structural_metric_evidence(target_document, prediction_document),
            **_domain_metric_evidence(case, target_plain, prediction_plain),
        }
        hard_content = _hard_content(target_plain, prediction_plain, case.protected_values)
        critical: list[str] = []
        if prediction_document is None:
            critical.extend(("invalid_sst", "output_not_clean_text"))
        for category, evidence in hard_content.items():
            if not evidence["exact"]:
                critical.extend(_HARD_CATEGORY_TO_ERRORS[category])

        reference_types = tuple(block.type for block in target_document.blocks)
        prediction_types = (
            tuple(block.type for block in prediction_document.blocks) if prediction_document is not None else ()
        )
        critical.extend(_structure_critical_errors(reference_types, prediction_types))
        semantic_checks = _semantic_plan_checks(case, target_plain, prediction_plain)
        critical.extend(name for name, triggered in semantic_checks.items() if triggered)
        expected_counter = Counter(reference_types)
        predicted_counter = Counter(prediction_types)
        reference_block_counts.update(expected_counter)
        prediction_block_counts.update(predicted_counter)
        block_true_positive.update(expected_counter & predicted_counter)
        aligned_type_matches += sum(
            left == right for left, right in zip(reference_types, prediction_types, strict=False)
        )
        aligned_type_total += max(len(reference_types), len(prediction_types))

        safe_renderer = False
        renderer_round_trip = False
        if prediction_document is not None:
            safe_renderer, ast_codec_round_trip = _safe_renderer(prediction_document)
            renderer_round_trip = (
                safe_renderer and ast_codec_round_trip and render_sst(prediction_document) == case.prediction_sst
            )
            if not safe_renderer:
                critical.append("unsafe_renderer_output")
        strict_exact = case.prediction_sst == case.target_sst
        normalized_exact = _normalized(case.prediction_sst) == _normalized(case.target_sst)
        case_results.append(
            CaseEvaluation(
                case_id=case.case_id,
                sst_valid=prediction_document is not None,
                output_only_compliant=prediction_document is not None,
                strict_exact_match=strict_exact,
                normalized_exact_match=normalized_exact,
                identity_exact_match=strict_exact if case.is_identity else None,
                protected_spans_exact=bool(hard_content["protected_spans"]["exact"]),
                ast_exact_match=prediction_document == target_document,
                renderer_round_trip=renderer_round_trip,
                safe_renderer=safe_renderer,
                block_sequence_exact=prediction_types == reference_types,
                reference_block_types=tuple(block_type.value for block_type in reference_types),
                prediction_block_types=tuple(block_type.value for block_type in prediction_types),
                hard_content=hard_content,
                quality_counts=quality_counts,
                critical_errors=tuple(dict.fromkeys(critical)),
                metric_evidence=metric_evidence,
                semantic_checks=semantic_checks,
            )
        )

    results = tuple(case_results)
    total = len(results)
    errors = Counter(error for result in results for error in result.critical_errors)
    all_block_types = tuple(
        sorted(set(reference_block_counts) | set(prediction_block_counts), key=lambda item: item.value)
    )
    block_metrics = {
        block_type.value: _prf(
            reference_block_counts[block_type],
            prediction_block_counts[block_type],
            block_true_positive[block_type],
        )
        for block_type in all_block_types
    }
    total_expected_blocks = sum(reference_block_counts.values())
    total_predicted_blocks = sum(prediction_block_counts.values())
    total_block_true_positive = sum(block_true_positive.values())
    block_micro = _prf(total_expected_blocks, total_predicted_blocks, total_block_true_positive)
    hard_categories = tuple(sorted(results[0].hard_content))
    hard_rates, hard_counts = _hard_summary(results, hard_categories)
    identity_results = tuple(result for result in results if result.identity_exact_match is not None)
    quality = _quality_report(results)
    text_similarity, metric_availability = _text_similarity_report(results)
    annotation_metrics, annotation_availability = _annotation_metric_report(results)
    structural_metrics, structural_availability = _named_metric_report(
        results,
        _STRUCTURAL_METRIC_KINDS,
        prefix="structural",
        unavailable_reason="no_reference_ast_feature_supports_this_metric",
    )
    domain_metrics, domain_availability = _named_metric_report(
        results,
        _DOMAIN_METRIC_KINDS,
        prefix="domain",
        unavailable_reason="no_reference_annotation_supports_this_metric",
    )
    semantic_hard_check_counts, semantic_availability = _semantic_summary(results)

    slice_members: dict[str, list[CaseEvaluation]] = {"all": list(results)}
    by_id = {case.case_id: case for case in materialized}
    for result in results:
        tags = set(by_id[result.case_id].slice_tags)
        if by_id[result.case_id].is_identity:
            tags.add("identity")
        for tag in sorted(tags):
            if not tag or tag == "all":
                raise ValueError("slice tags must be non-empty and must not use reserved tag 'all'")
            slice_members.setdefault(tag, []).append(result)

    return EvaluationReport(
        total=total,
        sst_parse_rate=sum(result.sst_valid for result in results) / total,
        output_only_compliance_rate=sum(result.output_only_compliant for result in results) / total,
        exact_match_rate=sum(result.strict_exact_match for result in results) / total,
        normalized_exact_match=sum(result.normalized_exact_match for result in results) / total,
        punctuation_precision=quality["punctuation_precision"],
        punctuation_recall=quality["punctuation_recall"],
        punctuation_f1=quality["punctuation_f1"],
        filler_precision=quality["filler_precision"],
        filler_recall=quality["filler_recall"],
        filler_f1=quality["filler_f1"],
        repetition_cleanup_precision=quality["repetition_cleanup_precision"],
        repetition_cleanup_recall=quality["repetition_cleanup_recall"],
        repetition_cleanup_f1=quality["repetition_cleanup_f1"],
        identity_total=len(identity_results),
        identity_exact_match=(
            sum(bool(result.identity_exact_match) for result in identity_results) / len(identity_results)
            if identity_results
            else 1.0
        ),
        protected_span_exact_rate=sum(result.protected_spans_exact for result in results) / total,
        ast_exact_match_rate=sum(result.ast_exact_match for result in results) / total,
        renderer_round_trip_rate=sum(result.renderer_round_trip for result in results) / total,
        safe_renderer_rate=sum(result.safe_renderer for result in results) / total,
        block_sequence_exact_rate=sum(result.block_sequence_exact for result in results) / total,
        block_type_accuracy=aligned_type_matches / aligned_type_total if aligned_type_total else 1.0,
        block_type_f1=float(block_micro["f1"]),
        block_metrics=block_metrics,
        hard_content_exact_rates=hard_rates,
        hard_content_case_counts=hard_counts,
        hard_gate_passed=not errors,
        critical_errors=dict(sorted(errors.items())),
        slice_reports={tag: _slice_report(tuple(members)) for tag, members in sorted(slice_members.items())},
        case_results=results,
        cer=text_similarity["cer"],
        wer=text_similarity["wer"],
        chrfpp_f2=text_similarity["chrfpp_f2"],
        capitalization_accuracy=annotation_metrics["capitalization_accuracy"],
        sentence_boundary_f1=annotation_metrics["sentence_boundary_f1"],
        self_correction_recovery_accuracy=annotation_metrics["self_correction_recovery_accuracy"],
        format_command_recovery_accuracy=annotation_metrics["format_command_recovery_accuracy"],
        grammar_recovery_accuracy=annotation_metrics["grammar_recovery_accuracy"],
        spelling_recovery_accuracy=annotation_metrics["spelling_recovery_accuracy"],
        structural_metrics=structural_metrics,
        domain_metrics=domain_metrics,
        semantic_hard_check_counts=semantic_hard_check_counts,
        metric_availability={
            **metric_availability,
            **annotation_availability,
            **structural_availability,
            **domain_availability,
            "semantic_hard_checks": semantic_availability,
        },
    )
