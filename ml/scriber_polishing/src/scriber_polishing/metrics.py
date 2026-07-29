"""Deterministic safety-first metrics for polishing candidates."""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from .sst_parser import SSTParseError, parse_sst


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    source_text: str
    target_sst: str
    prediction_sst: str
    protected_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    total: int
    sst_parse_rate: float
    normalized_exact_match: float
    protected_span_exact_rate: float
    critical_errors: dict[str, int]


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def evaluate_predictions(cases: Iterable[EvaluationCase]) -> EvaluationReport:
    """Aggregate bounded metrics while treating parser and span failures as critical."""

    materialized = tuple(cases)
    if not materialized:
        raise ValueError("at least one evaluation case is required")
    if len({case.case_id for case in materialized}) != len(materialized):
        raise ValueError("case_id values must be unique")

    parsed = 0
    exact = 0
    protected = 0
    errors: Counter[str] = Counter()
    for case in materialized:
        try:
            parse_sst(case.prediction_sst)
            parsed += 1
        except (SSTParseError, ValueError):
            errors["invalid_sst"] += 1
        if _normalized(case.prediction_sst) == _normalized(case.target_sst):
            exact += 1
        if all(case.prediction_sst.count(value) == 1 for value in case.protected_values):
            protected += 1
        else:
            errors["protected_span_changed"] += 1

    total = len(materialized)
    return EvaluationReport(
        total=total,
        sst_parse_rate=parsed / total,
        normalized_exact_match=exact / total,
        protected_span_exact_rate=protected / total,
        critical_errors=dict(sorted(errors.items())),
    )
