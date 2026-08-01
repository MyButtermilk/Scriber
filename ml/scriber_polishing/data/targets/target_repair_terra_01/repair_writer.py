#!/usr/bin/env python3
"""Deterministically repair plan-led Scriber canonical targets.

The repair is intentionally local and plan-led.  It reads only the seven
semantic-plan batches, repository-owned contracts, and AST/SST implementation.
It reads the approved style-review verdict/index pair only to record the
reviewed core seed coverage and aggregate flag counts. It does not read model,
checkpoint, split, training, challenge, target, or other critic data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Keep imports from creating cache files outside this new repair directory.
sys.dont_write_bytecode = True

OUTPUT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = OUTPUT_DIR.parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scriber_polishing.ast_codec import document_from_dict, document_to_dict  # noqa: E402
from scriber_polishing.canonical_expander import (  # noqa: E402
    EXPANDER_VERSION,
    VARIANT_COUNT,
    expand_canonical_variants,
)
from scriber_polishing.document_ast import Block, BlockType, Document, ListItem  # noqa: E402
from scriber_polishing.schemas import validate_seed_plan  # noqa: E402
from scriber_polishing.sst_parser import parse_sst  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text  # noqa: E402
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

REPAIR_WRITER_ID = "target_repair_terra_01"
REPAIR_WRITER_VERSION = 3
MANIFEST_VERSION = 3
SEED_BATCH_DIRS = (
    "generator_english_mixed_01",
    "generator_formatting_structures_01",
    "generator_general_business_01",
    "generator_hard_negatives_01",
    "generator_real_estate_energy_01",
    "generator_tax_legal_01",
    "generator_technical_project_01",
)
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
REJECTS_PATH = OUTPUT_DIR / "rejects.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
REPORT_PATH = OUTPUT_DIR / "repair_report.md"
TARGET_WRITER_HANDOFF = PROJECT_ROOT / "handoffs" / "AGENT_TARGET_WRITER.md"
STYLE_REVIEW_VERDICTS_PATH = PROJECT_ROOT / "artifacts" / "agent_reviews_v3" / "style_terra_max" / "verdicts.jsonl"
STYLE_REVIEW_PRIVATE_INDEX_PATH = PROJECT_ROOT / "artifacts" / "private" / "critic_package_v3_index.json"
STYLE_REVIEW_PACKAGE_SHA256 = "sha256:f94987cd85b0ed98720489e8276d725f795cd02effd1342eabd7a733251f978a"
STYLE_REVIEW_REJECT_COUNT = 612
STYLE_REVIEW_CRITICAL_FLAG_COUNTS = {
    "adjective_declension_error": 50,
    "article_gender_error": 155,
    "comma_punctuation_error": 50,
    "conditional_clause_fragment": 84,
    "missing_determiner_technical_term": 50,
    "plural_verb_agreement": 37,
    "postscript_marker_missing": 120,
    "terminal_punctuation_defect": 129,
}

_FICTION_LABEL = re.compile(r"\b(?:fiktiv(?:e|en|er|es|em)?|fictional)\b", re.IGNORECASE)
_FICTION_LABEL_WITH_SPACE = re.compile(r"\b(?:fiktiv(?:e|en|er|es|em)?|fictional)\s+", re.IGNORECASE)
_SPACES = re.compile(r"[ \t]{2,}")
_MECHANICAL_REFERENCE = re.compile(r"\bwird ausschließlich als wörtliche Referenz im Vorgang geführt\b", re.IGNORECASE)
_BAD_ARTICLE_PATTERNS = (
    re.compile(
        r"\bzu (?:Rechnungsinformation|Projektstatus|Kurze Projektabstimmung|Reklamationsbearbeitung|"
        r"Terminbestätigung|Bankverbindung|Fristabstimmung|Angebot|Übergabedokumentation|"
        r"Kundeninformation|Budgetmeldung)\b"
    ),
    re.compile(r"\bnach vollständiger Prüfung Vorhaben\b"),
    re.compile(r"\bZuordnung zu Abruf\b"),
)
_BAD_CASE_PATTERNS = (
    re.compile(r"\bdes Finanzamt\b"),
    re.compile(r"\bim (?:Umsatzliste|Saldenbestätigung|Abstimmungsnotiz|Zahlungsübersicht|Rechnungskopie)\b"),
    re.compile(r"\bnach Eingang des (?:Buchungsjournal|Umsatzliste|Abstimmungsnotiz|Kontenblatt)\b"),
)
_H1_DIRECTIVE = re.compile(r"(?:Hauptüberschrift|main heading)\s*[„\"](?P<label>[^„“\"]+)[“\"]", re.IGNORECASE)
_H2_DIRECTIVE = re.compile(
    r"(?:Zwischenüberschrift|subheading|secondary heading)\s*[„\"](?P<label>[^„“\"]+)[“\"]", re.IGNORECASE
)
_NESTED_MARKER = re.compile(r"^(?:Unterpunkt|Sub-item|Subitem|Teilpunkt|Detailpunkt)\s*:\s*", re.IGNORECASE)
_FORMAL_LOWER_DIRECT = re.compile(
    r"\b(?:bitte\s+[A-Za-zÄÖÜäöüß-]+\s+sie|wenn\s+sie|falls\s+sie|sofern\s+sie|"
    r"sobald\s+sie|sie\s+(?:können|müssen|sollen|werden|haben|sind|bestätigen|"
    r"teilen|prüfen|melden|erhalten))\b"
)
_DU_LOWER_DIRECT = re.compile(
    r"\b(?:bitte\s+[A-Za-zÄÖÜäöüß-]+\s+du|wenn\s+du|falls\s+du|sofern\s+du|"
    r"sobald\s+du|du\s+(?:kannst|musst|sollst|wirst|hast|bist|bestätigst|"
    r"teilst|prüfst|meldest|erhältst))\b"
)
_MIXED_LANGUAGE_DIRECTIVE = re.compile(
    r"Bitte keep the technical term (?P<term>.+?) and reference (?P<reference>https://\S+) exactly as written\."
)
_THIRD_PERSON_REPORT = re.compile(
    r"(?P<name>[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]*(?:\s+[A-ZÄÖÜ0-9][A-Za-zÄÖÜäöüß0-9-]*){1,3})"
    r" teilte mit, dass sie (?P<tail>[^.]+)"
)
_TERMINAL_PUNCTUATION = ".!?…"
_TECHNICAL_TERMS = (
    "zero-downtime deployment",
    "observability dashboard",
    "service-level objective",
    "invoice reconciliation",
    "data retention policy",
    "dependency lockfile",
    "cash-flow forecast",
    "acceptance criteria",
    "webhook signature",
    "schema migration",
    "canary deployment",
    "idempotency key",
    "rollback window",
    "purchase order",
    "change request",
    "API gateway",
    "feature flag",
    "client update",
    "load test",
    "CI pipeline",
    "rate limit",
    "single sign-on",
)


class IrreparablePlan(ValueError):
    """A semantic-plan contradiction that cannot be repaired without invention."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class FactEvidence:
    fact_id: str
    order: int
    text: str
    structural: bool


@dataclass(frozen=True)
class RepairBuild:
    records: tuple[dict[str, Any], ...]
    rejects: tuple[dict[str, str], ...]
    targets_bytes: bytes
    rejects_bytes: bytes
    manifest: dict[str, Any]
    report: str


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_prefixed(value: bytes) -> str:
    return "sha256:" + sha256_bytes(value)


def plan_path(directory_name: str) -> Path:
    return PROJECT_ROOT / "data" / "seeds" / directory_name / "plans.jsonl"


def input_batch_entries() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for directory_name in SEED_BATCH_DIRS:
        path = plan_path(directory_name)
        entries.append(
            {
                "path": path.relative_to(PROJECT_ROOT).as_posix(),
                "sha256": sha256_prefixed(path.read_bytes()),
            }
        )
    return entries


def load_plans() -> Iterable[dict[str, Any]]:
    """Yield schema-validated plans in stable source-batch and source-line order."""
    for directory_name in SEED_BATCH_DIRS:
        path = plan_path(directory_name)
        with path.open("r", encoding="utf-8", newline="") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise ValueError(f"{path}: blank JSONL line {line_number}")
                parsed = json.loads(line)
                if not isinstance(parsed, dict):
                    raise ValueError(f"{path}: non-object JSONL line {line_number}")
                try:
                    yield validate_seed_plan(parsed)
                except ValueError as error:
                    raise ValueError(f"{path}: invalid plan on line {line_number}: {error}") from error


def load_style_repair_review(core_seed_ids: set[str]) -> dict[str, object]:
    """Load only aggregate, core-scoped repair-review coverage.

    The private case-to-seed lookup is used transiently to prove that the
    approved review covers only this target's source plans. Neither case IDs
    nor the private lookup path are emitted into the manifest or report.
    """

    expected_package_hash = STYLE_REVIEW_PACKAGE_SHA256.removeprefix("sha256:")
    try:
        case_to_seed = json.loads(STYLE_REVIEW_PRIVATE_INDEX_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError("approved style-review private index is unavailable") from error
    if not isinstance(case_to_seed, dict) or not all(
        isinstance(case_id, str) and isinstance(seed_id, str) for case_id, seed_id in case_to_seed.items()
    ):
        raise ValueError("approved style-review private index is malformed")

    rejected_seed_ids: list[str] = []
    flag_counts: Counter[str] = Counter()
    seen_case_ids: set[str] = set()
    try:
        verdict_lines = STYLE_REVIEW_VERDICTS_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise ValueError("approved style-review verdicts are unavailable") from error
    for line_number, line in enumerate(verdict_lines, start=1):
        if not line.strip():
            raise ValueError(f"approved style-review verdicts contain blank line {line_number}")
        verdict = json.loads(line)
        if not isinstance(verdict, dict):
            raise ValueError(f"approved style-review verdict {line_number} is not an object")
        if verdict.get("reviewed_package_sha256") != expected_package_hash:
            raise ValueError("approved style-review verdict package hash differs from the pinned package")
        if verdict.get("acceptable") is not False:
            continue
        case_id = verdict.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen_case_ids:
            raise ValueError("approved style-review rejected case IDs are invalid or duplicated")
        seen_case_ids.add(case_id)
        seed_id = case_to_seed.get(case_id)
        if not isinstance(seed_id, str):
            raise ValueError("approved style-review rejected case has no seed mapping")
        rejected_seed_ids.append(seed_id)
        critical_flags = verdict.get("critical_flags")
        if not isinstance(critical_flags, list) or not all(isinstance(flag, str) for flag in critical_flags):
            raise ValueError("approved style-review critical flags are malformed")
        flag_counts.update(critical_flags)

    unique_seed_ids = sorted(set(rejected_seed_ids))
    if len(rejected_seed_ids) != STYLE_REVIEW_REJECT_COUNT or len(unique_seed_ids) != STYLE_REVIEW_REJECT_COUNT:
        raise ValueError("approved style-review rejected seed coverage does not match the pinned count")
    if not set(unique_seed_ids).issubset(core_seed_ids):
        raise ValueError("approved style-review includes a seed outside the core target scope")
    observed_flag_counts = dict(sorted(flag_counts.items()))
    if observed_flag_counts != dict(sorted(STYLE_REVIEW_CRITICAL_FLAG_COUNTS.items())):
        raise ValueError("approved style-review critical flag counts differ from the pinned review summary")
    return {
        "reviewed_package_sha256": STYLE_REVIEW_PACKAGE_SHA256,
        "reviewed_seed_count": len(unique_seed_ids),
        "reviewed_seed_ids": unique_seed_ids,
        "critical_flag_counts": observed_flag_counts,
    }


def sorted_facts(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    facts = list(plan["semantic_plan"]["facts"])
    orders = [fact["order"] for fact in facts]
    if len(orders) != len(set(orders)):
        raise IrreparablePlan("duplicate_fact_order", "semantic_plan.facts has duplicate order values")
    return sorted(facts, key=lambda fact: int(fact["order"]))


def flatten_protected_spans(facts: Sequence[Mapping[str, Any]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        for value in fact["protected_values"]:
            if value not in seen:
                result.append(value)
                seen.add(value)
    return result


def clean_whitespace(value: str) -> str:
    return _SPACES.sub(" ", value).strip()


def remove_fiction_labels(value: str) -> str:
    """Remove corpus-provenance labels while retaining every entity value."""
    return clean_whitespace(_FICTION_LABEL_WITH_SPACE.sub("", value))


def _preserves_values(original: str, repaired: str, protected_values: Sequence[str]) -> None:
    for value in protected_values:
        if value in original and value not in repaired:
            raise IrreparablePlan(
                "protected_value_conflicts_with_repair",
                f"the required value {value!r} would be altered by a grammar repair",
            )


def _organization_values(plan: Mapping[str, Any]) -> list[str]:
    return [entity["value"] for entity in plan["semantic_plan"]["entities"] if entity["type"] == "organization"]


def _product_values(plan: Mapping[str, Any]) -> list[str]:
    return [entity["value"] for entity in plan["semantic_plan"]["entities"] if entity["type"] == "product"]


def _replace_business_coordination(value: str, plan: Mapping[str, Any]) -> str:
    """Repair the generated ``zu <topic>`` government without changing facts."""
    organizations = [candidate for candidate in _organization_values(plan) if candidate in value]
    products = [candidate for candidate in _product_values(plan) if candidate in value]
    if len(organizations) < 2 or not products:
        return value
    first, second, product = organizations[0], organizations[1], products[0]
    prefix = " zu "
    suffix = f" für {product} ab."
    if " stimmt sich mit " not in value or prefix not in value or not value.endswith(suffix):
        return value
    topic_start = value.find(prefix) + len(prefix)
    topic_end = len(value) - len(suffix)
    topic = value[topic_start:topic_end].strip()
    if not topic:
        return value
    return f"{first} stimmt sich mit {second} zum Thema „{topic} für {product}“ ab."


def _replace_english_record(value: str, plan: Mapping[str, Any]) -> str:
    """Make the repeated English corpus frame read as ordinary business English."""
    organizations = [candidate for candidate in _organization_values(plan) if candidate in value]
    if len(organizations) < 2 or " records reference " not in value or " with " not in value:
        return value
    return value.replace(f" with {organizations[1]}.", f" together with {organizations[1]}.")


def _repair_german_case_and_articles(value: str) -> str:
    repaired = value
    repaired = _replace_business_coordination_placeholder(repaired)
    repaired = re.sub(
        r"\b(Mitteilung|Bescheid|Schreiben) des (Finanzamt(?: [A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+){1,3})\b",
        r"\1 von \2",
        repaired,
    )
    dative_replacements = {
        "im Umsatzliste": "in der Umsatzliste",
        "im Saldenbestätigung": "in der Saldenbestätigung",
        "im Abstimmungsnotiz": "in der Abstimmungsnotiz",
        "im Zahlungsübersicht": "in der Zahlungsübersicht",
        "im Rechnungskopie": "in der Rechnungskopie",
    }
    for source, target in dative_replacements.items():
        repaired = repaired.replace(source, target)
    for noun in ("Buchungsjournal", "Umsatzliste", "Abstimmungsnotiz", "Kontenblatt"):
        repaired = repaired.replace(f"nach Eingang des {noun}", f"nach Eingang des Dokuments „{noun}“")
    article_replacements = {
        "Erläuterung zu Belegkette": "Erläuterung zur Belegkette",
        "Erläuterung zu Umsatzliste": "Erläuterung zur Umsatzliste",
        "Erläuterung zu Saldenbestätigung": "Erläuterung zur Saldenbestätigung",
        "Erläuterung zu Abstimmungsnotiz": "Erläuterung zur Abstimmungsnotiz",
        "Erläuterung zu Zahlungsübersicht": "Erläuterung zur Zahlungsübersicht",
        "Erläuterung zu Rechnungskopie": "Erläuterung zur Rechnungskopie",
        "Erläuterung zu Buchungsjournal": "Erläuterung zum Buchungsjournal",
        "Erläuterung zu Kontenblatt": "Erläuterung zum Kontenblatt",
        "Zuordnung zu Abruf": "Zuordnung zum Abruf",
        "Zuordnung zu Los": "Zuordnung zum Los",
        "Zuordnung zu Projektakte": "Zuordnung zur Projektakte",
    }
    for source, target in article_replacements.items():
        repaired = repaired.replace(source, target)
    repaired = re.sub(
        r"\bnach vollständiger Prüfung (?P<name>Vorhaben [^.?!;:]+)",
        r"nach vollständiger Prüfung des Vorhabens „\g<name>“",
        repaired,
    )
    repaired = repaired.replace("den Los ", "das Los ")
    repaired = repaired.replace("den Projektakte ", "die Projektakte ")
    repaired = re.sub(
        r"\bDie vereinbarte (?P<value>[^.]+?) bleibt für die Einheit maßgeblich\b",
        r"Der vereinbarte Wert von \g<value> bleibt für die Einheit maßgeblich",
        repaired,
    )
    repaired = re.sub(
        r"Das Zitat (?P<citation>§§[^.]+?) wird ausschließlich als wörtliche Referenz im Vorgang geführt\.",
        r"Die Zitate \g<citation> werden im Vorgang ausschließlich wörtlich wiedergegeben.",
        repaired,
    )
    repaired = re.sub(
        r"Das Zitat (?P<citation>(?:§|Artikel)[^.]+?) wird ausschließlich als wörtliche Referenz im Vorgang geführt\.",
        r"Das Zitat \g<citation> wird im Vorgang ausschließlich wörtlich wiedergegeben.",
        repaired,
    )
    return _repair_explicit_referent(repaired)


def _replace_business_coordination_placeholder(value: str) -> str:
    """Keep case/article repairs independent of plan-aware coordination repair."""
    return value


def _repair_mixed_language_directive(value: str, address_mode: str | None) -> str:
    """Turn the one mixed-language instruction template into grammatical German."""

    match = _MIXED_LANGUAGE_DIRECTIVE.search(value)
    if match is None:
        return value
    term = match.group("term")
    reference = match.group("reference")
    if address_mode == "mixed_correspondence":
        replacement = f"Bitte behalten Sie den technischen Begriff „{term}“ sowie die Referenz {reference} exakt bei."
    else:
        replacement = f"Der technische Begriff „{term}“ sowie die Referenz {reference} sind exakt beizubehalten."
    return value[: match.start()] + replacement + value[match.end() :]


def _repair_explicit_referent(value: str) -> str:
    """Repeat the named reporter where a generated third-person pronoun was ambiguous."""

    return _THIRD_PERSON_REPORT.sub(
        lambda match: f"{match.group('name')} teilte mit, dass {match.group('name')} {match.group('tail')}",
        value,
    )


def _quote_technical_terms(value: str) -> str:
    repaired = value
    for term in _TECHNICAL_TERMS:
        repaired = re.sub(
            rf"(?<![„A-Za-zÄÖÜäöüß0-9-]){re.escape(term)}(?![“A-Za-zÄÖÜäöüß0-9-])",
            f"„{term}“",
            repaired,
        )
    return repaired


def _repair_technical_term_frames(value: str) -> str:
    """Use noun frames around immutable English technical terms instead of inflecting them."""

    repaired = re.sub(
        r"(?P<actor>[^.]+?) schlägt (?P<term>„[^“]+“) mit einem Zielwert",
        r"\g<actor> schlägt den Einsatz von \g<term> mit einem Zielwert",
        value,
    )
    repaired = re.sub(
        r"Als technische Grenze gelten (?P<value>[^.]+?) für Bandbreite im (?P<term>„[^“]+“)\.",
        r"Als technische Grenze gelten \g<value> für die Bandbreite im Kontext von \g<term>.",
        repaired,
    )
    repaired = repaired.replace("gestartet werden, unter der Voraussetzung", "gestartet werden unter der Voraussetzung")
    for term in (
        "webhook signature",
        "API gateway",
        "observability dashboard",
        "rollback window",
        "rate limit",
        "service-level objective",
    ):
        article = "die" if term == "webhook signature" else "das"
        repaired = repaired.replace(f"nicht als Ersatz für den „{term}“", f"nicht als Ersatz für {article} „{term}“")
    repaired = repaired.replace("vor dem „webhook signature“", "vor der „webhook signature“")
    repaired = repaired.replace("das „webhook signature“ gegen", "die „webhook signature“ gegen")
    repaired = repaired.replace("das „idempotency key“ gegen", "den „idempotency key“ gegen")
    repaired = repaired.replace("Fortschritt des „CI pipeline“", "Fortschritt der „CI pipeline“")
    repaired = repaired.replace("als nächsten Schritt den „client update“", "als nächsten Schritt das „client update“")
    repaired = re.sub(
        r"gehört zum „purchase order“ (?P<identifier>[A-Z]{2,}-\d+)\.",
        r"gehört zum Dokument „purchase order \g<identifier>“.",
        repaired,
    )
    repaired = repaired.replace("wenn „invoice reconciliation“ für", "wenn die „invoice reconciliation“ für")
    return repaired


def _integrate_technical_terms(value: str) -> str:
    """Keep immutable English terms while giving them an intelligible German role."""

    return _repair_technical_term_frames(_quote_technical_terms(value))


def _repair_real_estate_style(value: str) -> str:
    """Avoid inflecting variable building and energy labels as ordinary German nouns."""

    repaired = re.sub(
        r"eine Rückmeldung zur Position (?P<term>.+?) bis zum (?P<date>\d{2}\.\d{2}\.\d{4})",
        r"eine Rückmeldung zum Thema „\g<term>“ bis zum \g<date>",
        value,
    )
    repaired = re.sub(
        r"^Prüfung von (?P<term>lichte Höhe|umbauter Raum)$",
        r"Prüfung des Werts „\g<term>“",
        repaired,
    )
    return repaired


def _apply_address_mode(value: str, address_mode: str) -> str:
    if address_mode == "formal_sie":
        value = re.sub(r"\b(bitte\s+[A-Za-zÄÖÜäöüß-]+\s+)sie\b", r"\1Sie", value, flags=re.IGNORECASE)
        value = re.sub(r"\b(wenn|falls|sofern|sobald)\s+sie\b", r"\1 Sie", value, flags=re.IGNORECASE)
        value = re.sub(
            r"\bsie\s+(können|müssen|sollen|werden|haben|sind|bestätigen|teilen|prüfen|melden|erhalten)\b",
            r"Sie \1",
            value,
        )
    elif address_mode == "personal_du_capitalized":
        replacements = {
            "du": "Du",
            "dir": "Dir",
            "dich": "Dich",
            "dein": "Dein",
            "deine": "Deine",
            "deinen": "Deinen",
            "deinem": "Deinem",
            "deiner": "Deiner",
        }
        for source, target in replacements.items():
            value = re.sub(rf"\b{source}\b", target, value, flags=re.IGNORECASE)
    elif address_mode == "personal_du_lowercase":
        value = re.sub(r"(?<![.!?]\s)\bDu\b", "du", value)
        value = re.sub(r"(?<![.!?]\s)\bDir\b", "dir", value)
        value = re.sub(r"(?<![.!?]\s)\bDich\b", "dich", value)
        value = re.sub(r"(?<![.!?]\s)\bDein", "dein", value)
    return value


def ensure_terminal_punctuation(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if value.endswith("..") and not value.endswith("..."):
        return value.rstrip(".") + "."
    if value[-1] in _TERMINAL_PUNCTUATION:
        return value
    return value + "."


def strip_terminal_punctuation(value: str) -> str:
    """Prepare a declared condition for insertion into exactly one complete sentence."""

    return value.strip().rstrip(_TERMINAL_PUNCTUATION).rstrip()


def clean_fact_text(fact: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
    original = str(fact["text"])
    repaired = remove_fiction_labels(original)
    if plan["language"] in {"de-DE", "mixed"}:
        # Entity types are useful in the semantic plan but read as a provenance
        # label in prose ("Die Organisation Aster GmbH").  Keep the exact
        # protected entity value and remove only the redundant role wrapper.
        for organization in _organization_values(plan):
            repaired = repaired.replace(f"Die Organisation {organization}", organization)
        repaired = _repair_german_case_and_articles(repaired)
        repaired = _replace_business_coordination(repaired, plan)
        repaired = _repair_mixed_language_directive(repaired, str(plan["address_mode"]))
        repaired = _integrate_technical_terms(repaired)
        repaired = _repair_real_estate_style(repaired)
    if plan["language"] in {"en", "mixed"}:
        repaired = _replace_english_record(repaired, plan)
    repaired = _apply_address_mode(repaired, str(plan["address_mode"]))
    repaired = ensure_terminal_punctuation(clean_whitespace(repaired))
    _preserves_values(original, repaired, fact["protected_values"])
    return repaired


def clean_condition(value: str, plan: Mapping[str, Any]) -> str:
    repaired = remove_fiction_labels(value)
    if plan["language"] in {"de-DE", "mixed"}:
        repaired = _repair_german_case_and_articles(repaired)
        repaired = _integrate_technical_terms(repaired)
    return _apply_address_mode(clean_whitespace(repaired), str(plan["address_mode"]))


def _comparison_form(value: str) -> str:
    value = value.replace("„", "").replace("“", "")
    return re.sub(r"\s+", " ", value).strip(" .;:").casefold()


def append_missing_condition(value: str, condition: str | None, plan: Mapping[str, Any]) -> str:
    if not condition:
        return value
    cleaned = strip_terminal_punctuation(clean_condition(condition, plan))
    if _comparison_form(cleaned) in _comparison_form(value):
        return value
    body = ensure_terminal_punctuation(value)
    lower = cleaned.casefold()
    if plan["language"] == "en":
        if lower.startswith("if "):
            return f"{body} This applies {cleaned}."
        return f"{body} This applies under the following condition: {cleaned}."
    connective_prefixes = ("wenn ", "falls ", "sofern ", "sobald ", "solange ", "soweit ")
    if lower.startswith(connective_prefixes):
        return f"{body} Die Regelung gilt, {cleaned}."
    if lower.startswith(("unter der voraussetzung, dass ", "nach ", "ohne ")):
        if lower.startswith("ohne ") and "wörtlich wiedergegeben" in body:
            return f"{body} Die wörtliche Wiedergabe erfolgt ohne {cleaned[5:]}."
        return f"{body} Die Regelung gilt {cleaned}."
    return f"{body} Die Regelung gilt unter folgender Bedingung: {cleaned}."


def _extract_heading_directives(fact_text: str) -> dict[BlockType, str]:
    result: dict[BlockType, str] = {}
    h1 = _H1_DIRECTIVE.search(fact_text)
    h2 = _H2_DIRECTIVE.search(fact_text)
    if h1:
        result[BlockType.HEADING_1] = clean_whitespace(h1.group("label"))
    if h2:
        result[BlockType.HEADING_2] = clean_whitespace(h2.group("label"))
    return result


def _humanize_document_type(value: str) -> str:
    result = value.replace("_", " ").replace("Geschaeft", "Geschäft").replace("pruef", "prüf")
    return result[:1].upper() + result[1:]


def _clean_structural_label(value: str) -> str:
    return remove_fiction_labels(value).strip(" .,:;")


def choose_subject(plan: Mapping[str, Any]) -> str:
    structure = plan["semantic_plan"]["structure"]
    topic = _clean_structural_label(structure["paragraph_topics"][0])
    if not topic:
        topic = _humanize_document_type(str(plan["document_type"]))
    products = _product_values(plan)
    entities = products or _organization_values(plan)
    if entities and entities[0] not in topic:
        return f"{topic[:1].upper() + topic[1:]} – {entities[0]}"
    return topic[:1].upper() + topic[1:]


def choose_heading_labels(plan: Mapping[str, Any], subject: str | None) -> dict[BlockType, str]:
    structure = plan["semantic_plan"]["structure"]
    allowed = set(structure["heading_levels"])
    directives: dict[BlockType, str] = {}
    for fact in sorted_facts(plan):
        for block_type, label in _extract_heading_directives(str(fact["text"])).items():
            expected_level = "h1" if block_type is BlockType.HEADING_1 else "h2"
            if expected_level in allowed and block_type not in directives:
                directives[block_type] = _clean_structural_label(label)

    topics = [_clean_structural_label(topic) for topic in structure["paragraph_topics"]]
    used = {subject.casefold()} if subject else set()
    if subject:
        used.add(subject.split(" – ", 1)[0].casefold())
    labels: dict[BlockType, str] = {}
    for block_type, required_level in ((BlockType.HEADING_1, "h1"), (BlockType.HEADING_2, "h2")):
        if required_level not in allowed:
            continue
        explicit_directive = directives.get(block_type)
        if explicit_directive:
            selected = explicit_directive
        else:
            candidates = [*topics, _humanize_document_type(str(plan["document_type"]))]
            selected = next(
                (candidate for candidate in candidates if candidate and candidate.casefold() not in used),
                _humanize_document_type(str(plan["document_type"])),
            )
        labels[block_type] = selected
        used.add(selected.casefold())
    return labels


def choose_salutation(plan: Mapping[str, Any]) -> str:
    language = plan["language"]
    address_mode = plan["address_mode"]
    if language == "en":
        return "Hello,"
    if address_mode == "formal_sie":
        return "Sehr geehrte Damen und Herren,"
    if address_mode in {"personal_du_capitalized", "personal_du_lowercase"}:
        return "Hallo,"
    return "Guten Tag,"


def choose_closing(plan: Mapping[str, Any]) -> str:
    if plan["language"] == "en":
        return "Kind regards,"
    if plan["address_mode"] in {"personal_du_capitalized", "personal_du_lowercase"}:
        return "Beste Grüße"
    return "Mit freundlichen Grüßen"


def classify_structural_fact(
    fact: Mapping[str, Any], structure: Mapping[str, Any], heading_labels: Mapping[BlockType, str]
) -> str | None:
    text = str(fact["text"])
    if structure.get("has_quote") is True and structure.get("quote_text") and structure["quote_text"] in text:
        return "quote"
    if (
        structure.get("has_attachments") is True
        and structure.get("attachment_lines")
        and all(line in text for line in structure["attachment_lines"])
    ):
        return "attachments"
    if structure.get("has_post_script") is True and structure.get("post_script_text") in text:
        return "post_script"
    directives = _extract_heading_directives(text)
    matching_directives = {
        block_type: label for block_type, label in directives.items() if block_type in heading_labels
    }
    if (
        matching_directives
        and len(matching_directives) == len(directives)
        and all(
            heading_labels.get(block_type) == _clean_structural_label(label)
            for block_type, label in matching_directives.items()
        )
    ):
        return "headings"
    return None


def clean_list_item(value: str, plan: Mapping[str, Any]) -> str:
    repaired = remove_fiction_labels(value)
    if plan["language"] in {"de-DE", "mixed"}:
        repaired = _repair_german_case_and_articles(repaired)
        repaired = _integrate_technical_terms(repaired)
        repaired = _repair_real_estate_style(repaired)
    return _apply_address_mode(clean_whitespace(repaired), str(plan["address_mode"]))


def list_items_from_structure(
    structure: Mapping[str, Any], plan: Mapping[str, Any]
) -> tuple[BlockType | None, tuple[ListItem, ...]]:
    kind = str(structure["list_kind"])
    raw_items = list(structure["list_items"])
    if kind == "none":
        if raw_items:
            raise IrreparablePlan("list_items_without_list_kind", "list_kind is none but list_items is non-empty")
        return None, ()
    if not raw_items:
        raise IrreparablePlan("missing_list_items", f"list_kind={kind} has no source list items")
    if kind not in {"ordered", "unordered", "nested"}:
        raise IrreparablePlan("unknown_list_kind", f"unsupported list_kind={kind!r}")
    if kind == "nested" and len(raw_items) < 2:
        raise IrreparablePlan(
            "nested_list_without_possible_child", "a nested list requires at least one root item and one child item"
        )

    items: list[ListItem] = []
    for index, raw in enumerate(raw_items):
        source = str(raw)
        leading = len(source) - len(source.lstrip(" \t"))
        text = source.strip()
        level = 1
        marker = _NESTED_MARKER.match(text) if kind == "nested" else None
        if kind == "nested":
            if marker:
                text = text[marker.end() :].strip()
                level = 2
            elif leading:
                level = min(3, 1 + max(1, leading // 2))
            elif index > 0:
                # The declared nested kind is the semantic hierarchy; later
                # entries become children without adding or altering their text.
                level = 2
        if index == 0 and level != 1:
            raise IrreparablePlan("nested_list_starts_below_root", "the first nested item has no possible parent")
        if not text:
            raise IrreparablePlan("empty_list_item", "a declared list item is empty after its structural marker")
        items.append(ListItem(level=level, text=clean_list_item(text, plan)))
    if kind == "nested" and not any(item.level > 1 for item in items):
        raise IrreparablePlan("nested_list_without_possible_child", "no child level can be realized")
    block_type = BlockType.ORDERED_LIST if kind == "ordered" else BlockType.UNORDERED_LIST
    return block_type, tuple(items)


def make_document(plan: Mapping[str, Any]) -> tuple[Document, tuple[FactEvidence, ...]]:
    facts = sorted_facts(plan)
    structure = plan["semantic_plan"]["structure"]
    protected = flatten_protected_spans(facts)
    if any(_FICTION_LABEL.fullmatch(value.strip()) for value in protected):
        raise IrreparablePlan("protected_fiction_label", "a protected span is only a provenance label")

    list_block_type, list_items = list_items_from_structure(structure, plan)
    subject = choose_subject(plan) if structure["has_subject"] else None
    heading_labels = choose_heading_labels(plan, subject)

    blocks: list[Block] = []
    if subject:
        blocks.append(Block(type=BlockType.SUBJECT, text=subject))
    if BlockType.HEADING_1 in heading_labels:
        blocks.append(Block(type=BlockType.HEADING_1, text=heading_labels[BlockType.HEADING_1]))
    if structure["has_salutation"]:
        blocks.append(Block(type=BlockType.SALUTATION, text=choose_salutation(plan)))
    if BlockType.HEADING_2 in heading_labels:
        blocks.append(Block(type=BlockType.HEADING_2, text=heading_labels[BlockType.HEADING_2]))

    evidence: list[FactEvidence] = []
    emitted_quote = False
    for fact in facts:
        structural = classify_structural_fact(fact, structure, heading_labels)
        if structural == "quote":
            quote_text = str(structure["quote_text"])
            blocks.append(Block(type=BlockType.QUOTE, text=quote_text))
            evidence.append(FactEvidence(str(fact["fact_id"]), int(fact["order"]), quote_text, True))
            emitted_quote = True
            continue
        if structural in {"attachments", "post_script", "headings"}:
            if structural == "attachments":
                source = "; ".join(str(line) for line in structure["attachment_lines"])
            elif structural == "post_script":
                source = str(structure["post_script_text"])
            else:
                source = next(iter(_extract_heading_directives(str(fact["text"])).values()))
            evidence.append(FactEvidence(str(fact["fact_id"]), int(fact["order"]), source, True))
            continue
        text = clean_fact_text(fact, plan)
        text = append_missing_condition(text, fact["condition"], plan)
        _preserves_values(str(fact["text"]), text, fact["protected_values"])
        blocks.append(Block(type=BlockType.PARAGRAPH, text=text))
        evidence.append(FactEvidence(str(fact["fact_id"]), int(fact["order"]), text, False))

    if list_block_type is not None:
        blocks.append(Block(type=list_block_type, items=list_items))
    if structure.get("has_quote") is True and not emitted_quote:
        blocks.append(Block(type=BlockType.QUOTE, text=str(structure["quote_text"])))
    if structure["has_closing"]:
        blocks.append(Block(type=BlockType.CLOSING, text=choose_closing(plan)))
        # Signature lines are a closed structural contract, not ordinary prose.
        # Preserve them byte-for-byte so the canonical expander can safely
        # substitute any typed entities they contain.
        signature_lines = tuple(str(line) for line in structure["signature_lines"])
        if signature_lines:
            blocks.append(Block(type=BlockType.SIGNATURE, lines=signature_lines))
    elif structure["signature_lines"]:
        raise IrreparablePlan("signature_without_closing", "signature_lines require has_closing=true")
    if structure.get("has_attachments") is True:
        attachment_lines = tuple(str(line) for line in structure["attachment_lines"])
        blocks.append(Block(type=BlockType.ATTACHMENTS, text="; ".join(attachment_lines)))
    if structure.get("has_post_script") is True:
        post_script_text = str(structure["post_script_text"])
        visible_post_script = post_script_text if post_script_text.startswith("PS:") else f"PS: {post_script_text}"
        blocks.append(Block(type=BlockType.POST_SCRIPT, text=visible_post_script))
    return Document(blocks=tuple(blocks)), tuple(evidence)


def make_record(plan: Mapping[str, Any], *, prompt_hash: str) -> tuple[dict[str, Any], tuple[FactEvidence, ...]]:
    document, evidence = make_document(plan)
    facts = sorted_facts(plan)
    semantic_plan = plan["semantic_plan"]
    semantic_hash = sha256_prefixed(canonical_json(semantic_plan).encode("utf-8"))
    plain_text = render_plain_text(document)
    record: dict[str, Any] = {
        "schema_version": 1,
        "canonical_document_id": f"repair_{plan['seed_id']}",
        "seed_id": plan["seed_id"],
        "source_plan_batch": plan["batch_id"],
        "plan_generator_agent": plan["generator_agent"],
        "target_writer_agent": REPAIR_WRITER_ID,
        "target_prompt_hash": prompt_hash,
        "semantic_plan_sha256": semantic_hash,
        "source_text": plain_text,
        "target_sst": render_sst(document),
        "target_plain_text": plain_text,
        "target_markdown": render_markdown(document),
        "target_html": render_html(document),
        "target_ast": document_to_dict(document),
        "language": plan["language"],
        "style_profile": plan["style_profile"],
        "address_mode": plan["address_mode"],
        "legal_citation_style": plan["legal_citation_style"],
        "unit_style": plan["unit_style"],
        "normalize_valid_variants": False,
        "protected_spans": flatten_protected_spans(facts),
        "applied_normalizations": [],
        "rejected_normalizations": [],
        "ambiguity_flags": [
            tag
            for tag in plan["risk_tags"]
            if any(marker in tag for marker in ("ambiguous", "ambiguity", "hard_negative"))
        ],
    }
    validate_canonical_record(record)
    validate_semantic_alignment(record, plan, document, evidence)
    return record, evidence


def _lexical_blocks(document: Document) -> Iterable[str]:
    for block in document.blocks:
        # Quotes and signatures are plan-owned verbatim structures.  Mechanical
        # prose checks must not rewrite either one.
        if block.type in {BlockType.QUOTE, BlockType.SIGNATURE}:
            continue
        if block.type in {BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST}:
            yield from (item.text for item in block.items)
        else:
            yield block.text


_UNIT_VALUE = re.compile(
    r"\d[\d.,]*\s*(?:%|€|m²|m³|kW|kWh|MWh|kWh/\(m²·a\)|kWh/m²a|km/h|°C|kg|m|cm|h|ms|min|Mbit/s|vCPU)\b"
)


def _plan_text_for_entity_checks(plan: Mapping[str, Any]) -> str:
    structure = plan["semantic_plan"]["structure"]
    parts = [str(fact["text"]) for fact in plan["semantic_plan"]["facts"]]
    parts.extend(str(fact["condition"]) for fact in plan["semantic_plan"]["facts"] if fact["condition"])
    parts.extend(str(item) for item in structure["list_items"])
    for key in ("quote_text", "post_script_text"):
        if structure.get(key):
            parts.append(str(structure[key]))
    parts.extend(str(item) for item in structure.get("attachment_lines", []))
    return "\n".join(parts)


def validate_semantic_alignment(
    record: Mapping[str, Any],
    plan: Mapping[str, Any],
    document: Document,
    evidence: Sequence[FactEvidence],
) -> None:
    """Closed validation beyond schema/rendering: plan facts and structure only."""
    validated = validate_canonical_record(record)
    if validated["source_text"] != validated["target_plain_text"]:
        raise ValueError("source_text must remain the canonical plain target")
    if document_from_dict(record["target_ast"]) != document:
        raise ValueError("AST codec does not round-trip the repaired document")
    if render_sst(parse_sst(record["target_sst"])) != record["target_sst"]:
        raise ValueError("SST parser does not round-trip the repaired document")

    structure = plan["semantic_plan"]["structure"]
    types = [block.type for block in document.blocks]

    def has(block_type: BlockType) -> bool:
        return block_type in types

    expected_headings = set(structure["heading_levels"])
    if not expected_headings and any(block_type in {BlockType.HEADING_1, BlockType.HEADING_2} for block_type in types):
        raise ValueError("heading block emitted despite an empty heading_levels contract")
    if (BlockType.HEADING_1 in types) != ("h1" in expected_headings):
        raise ValueError("heading_1 does not match the semantic structure")
    if (BlockType.HEADING_2 in types) != ("h2" in expected_headings):
        raise ValueError("heading_2 does not match the semantic structure")
    for flag, block_type in (
        ("has_subject", BlockType.SUBJECT),
        ("has_salutation", BlockType.SALUTATION),
        ("has_closing", BlockType.CLOSING),
        ("has_quote", BlockType.QUOTE),
        ("has_attachments", BlockType.ATTACHMENTS),
        ("has_post_script", BlockType.POST_SCRIPT),
    ):
        if has(block_type) != bool(structure.get(flag, False)):
            raise ValueError(f"{block_type.value} does not match {flag}")
    expected_signature = bool(structure["has_closing"] and structure["signature_lines"])
    if has(BlockType.SIGNATURE) != expected_signature:
        raise ValueError("signature block does not match structure")
    signature_blocks = [block for block in document.blocks if block.type is BlockType.SIGNATURE]
    if signature_blocks and list(signature_blocks[0].lines) != structure["signature_lines"]:
        raise ValueError("signature lines differ from the semantic structure")

    actual_lists = [
        block for block in document.blocks if block.type in {BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST}
    ]
    list_kind = structure["list_kind"]
    expected_list_type = {
        "none": None,
        "ordered": BlockType.ORDERED_LIST,
        "unordered": BlockType.UNORDERED_LIST,
        "nested": BlockType.UNORDERED_LIST,
    }[list_kind]
    if expected_list_type is None:
        if actual_lists:
            raise ValueError("list emitted despite list_kind=none")
    elif len(actual_lists) != 1 or actual_lists[0].type is not expected_list_type:
        raise ValueError("list block does not match list_kind")
    elif list_kind == "nested" and not any(item.level > 1 for item in actual_lists[0].items):
        raise ValueError("nested list lacks an actual child level")

    lexical_text = "\n".join(_lexical_blocks(document))
    if _FICTION_LABEL.search(lexical_text):
        raise ValueError("mechanical fiction label remains outside a protected quote")
    if _MECHANICAL_REFERENCE.search(lexical_text):
        raise ValueError("mechanical literal-reference template remains")
    if any(pattern.search(lexical_text) for pattern in (*_BAD_ARTICLE_PATTERNS, *_BAD_CASE_PATTERNS)):
        raise ValueError("known article or case defect remains outside a protected quote")
    if plan["address_mode"] == "formal_sie" and _FORMAL_LOWER_DIRECT.search(lexical_text):
        raise ValueError("formal address contains lower-case direct Sie")
    if plan["address_mode"] == "personal_du_capitalized" and _DU_LOWER_DIRECT.search(lexical_text):
        raise ValueError("capitalized Du address contains lower-case direct du")

    for block in document.blocks:
        if block.type is BlockType.PARAGRAPH and block.text[-1] not in ".!?…":
            raise ValueError("paragraph lacks terminal punctuation")
        if block.type is BlockType.SALUTATION and not block.text.endswith(","):
            raise ValueError("salutation lacks terminal punctuation")
        if block.type is BlockType.POST_SCRIPT and not block.text.startswith("PS:"):
            raise ValueError("post-script lacks a visible conventional PS: marker")
        if block.type not in {BlockType.SIGNATURE, BlockType.QUOTE} and block.text != block.text.strip():
            raise ValueError("a visible AST text block has boundary whitespace")
    source_entity_context = _plan_text_for_entity_checks(plan)
    for entity in plan["semantic_plan"]["entities"]:
        value = entity["value"]
        if value in source_entity_context and value not in record["target_plain_text"]:
            raise ValueError(f"referenced entity is absent from target: {value!r}")

    plain_text = record["target_plain_text"]
    position = 0
    for item in sorted((entry for entry in evidence if not entry.structural), key=lambda entry: entry.order):
        found = plain_text.find(item.text, position)
        if found < 0:
            raise ValueError(f"fact {item.fact_id} is absent or reordered")
        position = found + len(item.text)
    for item in (entry for entry in evidence if entry.structural):
        if item.text not in plain_text:
            raise ValueError(f"structural fact {item.fact_id} is absent")
    evidence_by_id = {item.fact_id: item.text for item in evidence}
    for fact in plan["semantic_plan"]["facts"]:
        rendered_fact = evidence_by_id[str(fact["fact_id"])]
        if fact["condition"] and _comparison_form(
            clean_condition(str(fact["condition"]), plan)
        ) not in _comparison_form(rendered_fact):
            raise ValueError(f"condition is absent or altered for fact {fact['fact_id']}")
        for protected_value in fact["protected_values"]:
            if _UNIT_VALUE.search(protected_value) and protected_value not in rendered_fact:
                raise ValueError(f"unit-bearing protected value lost its fact relation: {protected_value!r}")


def validate_canonical_expansion(plan: Mapping[str, Any], record: Mapping[str, Any]) -> int:
    """Require the repository canonical expander to accept one repaired target."""
    variants = expand_canonical_variants(plan, record)
    if len(variants) != VARIANT_COUNT:
        raise ValueError(f"canonical expander returned {len(variants)} variants; expected {VARIANT_COUNT}")
    variant_ids = {variant["canonical_document_id"] for variant in variants}
    variant_sst = {variant["target_sst"] for variant in variants}
    if len(variant_ids) != VARIANT_COUNT or len(variant_sst) != VARIANT_COUNT:
        raise ValueError("canonical expander variants are not structurally unique")
    return len(variants)


def build_repair() -> RepairBuild:
    prompt_hash = sha256_prefixed(TARGET_WRITER_HANDOFF.read_bytes())
    records: list[dict[str, Any]] = []
    rejects: list[dict[str, str]] = []
    source_metadata: dict[str, dict[str, str]] = {}
    expansion_variant_count = 0
    signature_contract_repair_count = 0
    seen_seed_ids: set[str] = set()
    for plan in load_plans():
        seed_id = str(plan["seed_id"])
        if seed_id in seen_seed_ids:
            raise ValueError(f"duplicate seed_id in source corpus: {seed_id}")
        seen_seed_ids.add(seed_id)
        source_metadata[seed_id] = {
            "domain": str(plan["domain"]),
            "document_type": str(plan["document_type"]),
            "list_kind": str(plan["semantic_plan"]["structure"]["list_kind"]),
            "fact_count": str(len(plan["semantic_plan"]["facts"])),
        }
        signature_lines = plan["semantic_plan"]["structure"]["signature_lines"]
        if signature_lines and any(remove_fiction_labels(str(line)) != str(line) for line in signature_lines):
            signature_contract_repair_count += 1
        try:
            record, _ = make_record(plan, prompt_hash=prompt_hash)
            expansion_variant_count += validate_canonical_expansion(plan, record)
        except IrreparablePlan as error:
            rejects.append({"seed_id": seed_id, "reason_code": error.code, "reason": error.detail})
            continue
        except ValueError as error:
            raise ValueError(f"{seed_id}: deterministic repair validation failed: {error}") from error
        records.append(record)

    records.sort(key=lambda record: record["seed_id"])
    rejects.sort(key=lambda item: item["seed_id"])
    style_repair_review = load_style_repair_review(seen_seed_ids)
    targets_bytes = ("".join(canonical_json(record) + "\n" for record in records)).encode("utf-8")
    rejects_bytes = ("".join(canonical_json(item) + "\n" for item in rejects)).encode("utf-8")
    manifest = build_manifest(
        records,
        rejects,
        targets_bytes,
        rejects_bytes,
        prompt_hash,
        source_metadata,
        expansion_variant_count,
        signature_contract_repair_count,
        style_repair_review,
    )
    report = build_report(manifest)
    return RepairBuild(tuple(records), tuple(rejects), targets_bytes, rejects_bytes, manifest, report)


def _block_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for record in records:
        counter.update(block["type"] for block in record["target_ast"]["blocks"])
    return dict(sorted(counter.items()))


def build_manifest(
    records: Sequence[Mapping[str, Any]],
    rejects: Sequence[Mapping[str, str]],
    targets_bytes: bytes,
    rejects_bytes: bytes,
    prompt_hash: str,
    source_metadata: Mapping[str, Mapping[str, str]],
    expansion_variant_count: int,
    signature_contract_repair_count: int,
    style_repair_review: Mapping[str, object],
) -> dict[str, Any]:
    def counts(key: str) -> dict[str, int]:
        return dict(sorted(Counter(str(record[key]) for record in records).items()))

    list_kinds = Counter(source_metadata[record["seed_id"]]["list_kind"] for record in records)
    fact_counts = Counter(int(source_metadata[record["seed_id"]]["fact_count"]) for record in records)
    reject_counts = Counter(item["reason_code"] for item in rejects)
    implementation_hash = sha256_prefixed(Path(__file__).read_bytes())
    return {
        "manifest_version": MANIFEST_VERSION,
        "writer_id": REPAIR_WRITER_ID,
        "writer_version": REPAIR_WRITER_VERSION,
        "target_prompt_hash": prompt_hash,
        "repair_implementation_path": Path(__file__).relative_to(PROJECT_ROOT).as_posix(),
        "repair_implementation_sha256": implementation_hash,
        "assigned_input_batches": input_batch_entries(),
        "record_count": len(records),
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in records}),
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "targets_sha256": sha256_prefixed(targets_bytes),
        "rejects_path": REJECTS_PATH.name,
        "rejects_sha256": sha256_prefixed(rejects_bytes),
        "rejected_seed_count": len(rejects),
        "rejected_reason_counts": dict(sorted(reject_counts.items())),
        "rejected_seeds": list(rejects),
        "counts_by_domain": dict(
            sorted(Counter(source_metadata[record["seed_id"]]["domain"] for record in records).items())
        ),
        "counts_by_language": counts("language"),
        "counts_by_document_type": dict(
            sorted(Counter(source_metadata[record["seed_id"]]["document_type"] for record in records).items())
        ),
        "counts_by_address_mode": counts("address_mode"),
        "counts_by_block_type": _block_counts(records),
        "counts_by_list_kind": dict(sorted(list_kinds.items())),
        "counts_by_fact_count": {str(key): value for key, value in sorted(fact_counts.items())},
        "protected_span_presence": {
            "with_protected_spans": sum(bool(record["protected_spans"]) for record in records),
            "without_protected_spans": sum(not record["protected_spans"] for record in records),
        },
        "schema_validation_count": len(records),
        "ast_sst_ast_round_trip_count": len(records),
        "renderer_consistency_count": len(records),
        "canonical_expansion": {
            "expander": EXPANDER_VERSION,
            "validated_target_count": len(records),
            "variants_per_target": VARIANT_COUNT,
            "validated_variant_count": expansion_variant_count,
            "all_variants_unique_per_target": True,
        },
        "style_repair_review": style_repair_review,
        "v2_signature_contract_repair_count": signature_contract_repair_count,
        "deterministic_serialization": {
            "jsonl": "utf-8 compact sorted JSON with one final newline per record",
            "record_order": "seed_id ascending",
            "no_clock_or_randomness": True,
        },
        "clean_room_confirmation": {
            "inputs_limited_to_approved_seed_plans_contracts_handoffs_style_review_and_ast_renderer_validator_code": True,
            "no_human_curation": True,
            "no_external_corpus_web_data_or_generative_api": True,
            "no_model_checkpoint_training_split_test_or_challenge_data": True,
            "no_prior_target_output_or_nonpermitted_critic_material_used_by_repair_implementation": True,
            "writes_confined_to": OUTPUT_DIR.relative_to(PROJECT_ROOT).as_posix(),
        },
    }


def build_report(manifest: Mapping[str, Any]) -> str:
    rejected = manifest["rejected_reason_counts"]
    reject_summary = ", ".join(f"{code}: {count}" for code, count in rejected.items()) or "none"
    style_review = manifest["style_repair_review"]
    flag_summary = ", ".join(f"{flag}: {count}" for flag, count in style_review["critical_flag_counts"].items())
    reviewed_seed_ids = style_review["reviewed_seed_ids"]
    reviewed_seed_lines = tuple(
        "- " + ", ".join(reviewed_seed_ids[index : index + 8]) for index in range(0, len(reviewed_seed_ids), 8)
    )
    return "\n".join(
        (
            "# Deterministic target-repair report",
            "",
            f"- Source plans processed: {manifest['record_count'] + manifest['rejected_seed_count']}",
            f"- Repaired canonical targets: {manifest['record_count']}",
            f"- Explicit seed rejects: {manifest['rejected_seed_count']} ({reject_summary})",
            f"- Exact signature-line repairs from v1: {manifest['v2_signature_contract_repair_count']}",
            f"- Approved style-repair review coverage: {style_review['reviewed_seed_count']} core seeds",
            f"- Approved style-review package SHA-256: `{style_review['reviewed_package_sha256']}`",
            f"- Approved style-review critical-flag counts: {flag_summary}",
            f"- Canonical expansion gate: {manifest['canonical_expansion']['validated_target_count']} targets × {manifest['canonical_expansion']['variants_per_target']} variants = {manifest['canonical_expansion']['validated_variant_count']} validated variants",
            f"- Target JSONL SHA-256: `{manifest['targets_sha256']}`",
            f"- Reject JSONL SHA-256: `{manifest['rejects_sha256']}`",
            f"- Repair implementation SHA-256: `{manifest['repair_implementation_sha256']}`",
            "",
            "The repair builds the closed AST first and derives SST, plain text, Markdown, and safe HTML solely through the repository renderers. It removes non-semantic fictional provenance labels from ordinary prose, preserves plan-owned signature lines verbatim, repairs deterministic German case/article failures, avoids fabricated headings when heading levels are empty, realizes every viable nested list with an actual child level, and preserves protected values exactly. One-item nested-list plans are rejected because they cannot contain both a root and a child without inventing content.",
            "",
            "Closed validation covers schema validity, AST/SST/AST round trips, renderer equivalence, protected-span preservation, exact signature lines, declared structure, fact order, conditions, address mode, the identified mechanical-label/template and case/article regressions, and seven canonical-expander variants for every accepted target. The implementation is local, deterministic, and makes no network or generative API calls.",
            "",
            "## Approved style-repair reviewed seed IDs",
            "",
            *reviewed_seed_lines,
            "",
        )
    )


def write_build(build: RepairBuild) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TARGETS_PATH.write_bytes(build.targets_bytes)
    REJECTS_PATH.write_bytes(build.rejects_bytes)
    MANIFEST_PATH.write_text(
        json.dumps(build.manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    REPORT_PATH.write_text(build.report, encoding="utf-8", newline="\n")


def verify_written_build() -> RepairBuild:
    build = build_repair()
    expected = {
        TARGETS_PATH: build.targets_bytes,
        REJECTS_PATH: build.rejects_bytes,
        MANIFEST_PATH: (json.dumps(build.manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
        REPORT_PATH: build.report.encode("utf-8"),
    }
    for path, expected_bytes in expected.items():
        if not path.is_file() or path.read_bytes() != expected_bytes:
            raise ValueError(f"written repair output differs from deterministic rebuild: {path.name}")
    plans_by_seed = {plan["seed_id"]: plan for plan in load_plans()}
    serialized_target_count = 0
    serialized_variant_count = 0
    for line_number, line in enumerate(TARGETS_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        record = validate_canonical_record(json.loads(line))
        plan = plans_by_seed.get(record["seed_id"])
        if plan is None:
            raise ValueError(f"written target line {line_number} has no source plan")
        serialized_variant_count += validate_canonical_expansion(plan, record)
        serialized_target_count += 1
    expansion_manifest = build.manifest["canonical_expansion"]
    if serialized_target_count != expansion_manifest["validated_target_count"]:
        raise ValueError("serialized canonical-expansion target count differs from manifest")
    if serialized_variant_count != expansion_manifest["validated_variant_count"]:
        raise ValueError("serialized canonical-expansion variant count differs from manifest")
    return build


def inventory() -> dict[str, object]:
    plans = list(load_plans())
    structures = [plan["semantic_plan"]["structure"] for plan in plans]
    return {
        "plan_count": len(plans),
        "domain_counts": dict(sorted(Counter(plan["domain"] for plan in plans).items())),
        "heading_levels": dict(
            sorted(
                Counter(
                    "/".join(item["heading_levels"]) if item["heading_levels"] else "(none)" for item in structures
                ).items()
            )
        ),
        "list_kinds": dict(sorted(Counter(item["list_kind"] for item in structures).items())),
        "one_item_nested_lists": sum(
            item["list_kind"] == "nested" and len(item["list_items"]) == 1 for item in structures
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--inventory", action="store_true")
    action.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if arguments.inventory:
        print(json.dumps(inventory(), ensure_ascii=False, sort_keys=True, indent=2))
        return
    if arguments.check:
        build = verify_written_build()
    else:
        build = build_repair()
        write_build(build)
        verify_written_build()
    print(
        json.dumps(
            {
                "records": len(build.records),
                "rejects": len(build.rejects),
                "expanded_variants": build.manifest["canonical_expansion"]["validated_variant_count"],
                "targets_sha256": build.manifest["targets_sha256"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
