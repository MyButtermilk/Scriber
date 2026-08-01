"""Build a clean-room V2 hard-case supplement from aggregate diagnostics only.

The module deliberately does not read judge packets, predictions, references,
or individual remediation records.  A reviewed, repository-owned plan freezes
only aggregate error flags and drives independently seeded synthetic siblings.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .ast_codec import document_to_dict
from .corruption_engine import CorruptionProfile
from .dataset_builder import _stable_seed, build_review_candidate_pair
from .document_ast import Block, BlockType, Document, ListItem
from .schemas import validate_training_pair
from .split_dataset import Split
from .sst_renderer import render_sst
from .target_renderer import render_html, render_markdown, render_plain_text
from .validators import validate_canonical_record

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_V2_HARDCASE_SPEC_PATH = PROJECT_ROOT / "configs" / "v2_hardcase_data.json"
DEFAULT_V2_HARDCASE_OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "v2_hardcase_data_v1"
_SPEC_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "v2_hardcase_spec_schema.json"
_MANIFEST_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "v2_hardcase_dataset_manifest_schema.json"
_REVIEW_PACKAGE_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "v2_hardcase_review_package_schema.json"
_REVIEW_VERDICT_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "v2_hardcase_review_verdict_schema.json"
_TRAINING_PAIR_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "training_pair_schema.json"
_REVIEW_FIELDS = frozenset({"reviewed_package_sha256", "critic_agents", "critic_decisions"})

_ALLOWED_SOURCE_SUFFIXES = {
    "artifacts/judges/full_model_v1/sol_followup_v1/sol-remediation-v2/sol-remediation-summary.json",
    "artifacts/judges/full_model_v1/terra_interim_audit_v2/terra-interim-audit.json",
}
_SUPPORTED_CATEGORIES = {
    "protected_numeric_unit",
    "protected_legal_reference",
    "protected_datetime_amount",
    "polarity_modality_condition",
    "list_structure",
    "heading_structure",
    "clean_text_format_command",
    "content_completeness_layout",
}
_FORBIDDEN_SOURCE_PART = re.compile(
    r"(?:^|[/_.-])(?:test|challenge|reference|prediction|packet|verdict|case)(?:$|[/_.-])",
    re.IGNORECASE,
)

_ORGANIZATIONS = (
    "Fiktivwerk Albatros GmbH",
    "Mosaikpfad Nord KG",
    "Lichtbogen Atelier AG",
    "Kieselstern Dienste eG",
    "Tannenkern Systeme GmbH",
    "Wellenfeld Projekt KG",
    "Fjordwinkel Logistik AG",
    "Quarzblatt Planung GmbH",
    "Sonnenfaden Technik eG",
    "Birkenorbit Service KG",
)
_PEOPLE = (
    "Mara Venn",
    "Jonas Ried",
    "Elif Karo",
    "Timo Lenz",
    "Nina Voigt",
    "Levin Sorn",
    "Kira Mert",
    "Ruben Falk",
    "Alma Dorn",
    "Milan Kest",
)
_PROJECTS = (
    "Projekt Aster",
    "Vorhaben Fjord",
    "Modul Komet",
    "System Lumen",
    "Programm Quarz",
    "Plattform Tanne",
    "Initiative Welle",
    "Paket Birke",
)
_UNITS = (
    "250 ms",
    "99,95 %",
    "4 GiB",
    "12 vCPU",
    "40 req/s",
    "15 min",
    "2,5 TB",
    "768 MiB",
    "3 s",
    "48 h",
    "25 °C",
    "10 Mbit/s",
)
_LEGAL_REFERENCES = (
    "§ 7 Absatz 4 Satz 2 Einkommensteuergesetz",
    "§ 433 Absatz 1 Satz 2 Bürgerliches Gesetzbuch",
    "Artikel 6 Absatz 1 Buchstabe f Datenschutz-Grundverordnung",
    "§ 14 Absatz 2 Nummer 1 Umsatzsteuergesetz",
    "§ 28 Absatz 3 Satz 1 Verwaltungsverfahrensgesetz",
    "Artikel 12 Absatz 2 Verordnung (EU) 2024/1689",
)

_BASE_RECIPES: tuple[tuple[str, tuple[CorruptionProfile, ...]], ...] = (
    ("punctuation_case", (CorruptionProfile.PUNCTUATION_CASE,)),
    ("filler_stutter", (CorruptionProfile.FILLER_STUTTER,)),
    ("grammar_filler", (CorruptionProfile.GRAMMAR, CorruptionProfile.FILLER_STUTTER)),
    ("abandoned_filler", (CorruptionProfile.ABANDONED_START, CorruptionProfile.FILLER_STUTTER)),
    ("repetition_self_correction", (CorruptionProfile.REPETITION_SELF_CORRECTION,)),
    ("spoken_formatting", (CorruptionProfile.SPOKEN_FORMATTING,)),
    ("heavy_combined", (CorruptionProfile.HEAVY_COMBINED,)),
    (
        "spoken_formatting_heavy",
        (CorruptionProfile.SPOKEN_FORMATTING, CorruptionProfile.HEAVY_COMBINED),
    ),
)


class HardCaseDatasetError(RuntimeError):
    """A V2 hard-case input or generated artifact failed closed validation."""


@dataclass(frozen=True, slots=True)
class HardCaseDatasetBuild:
    output_dir: Path
    plan_path: Path
    train_path: Path
    validation_path: Path
    manifest_path: Path
    manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HardCaseReviewCandidateBuild:
    output_dir: Path
    plan_path: Path
    canonicals_path: Path
    train_pairs_path: Path
    validation_pairs_path: Path
    manifest_path: Path
    manifest: Mapping[str, Any]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pretty_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _plain_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_spec_source_path(value: str) -> None:
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or ".." in Path(normalized).parts:
        raise HardCaseDatasetError("diagnostic source must be a safe aggregate-only project path")
    if normalized not in _ALLOWED_SOURCE_SUFFIXES or _FORBIDDEN_SOURCE_PART.search(normalized):
        raise HardCaseDatasetError("diagnostic source must be an approved aggregate-only summary")


def load_v2_hardcase_spec(path: str | Path = DEFAULT_V2_HARDCASE_SPEC_PATH) -> dict[str, Any]:
    """Load the closed aggregate-only generation plan without opening case data."""

    resolved = Path(path).resolve()
    try:
        payload = resolved.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HardCaseDatasetError(f"V2 hard-case spec is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise HardCaseDatasetError("V2 hard-case spec must be an object")
    policy = value.get("policy")
    if isinstance(policy, Mapping) and policy.get("direct_test_or_challenge_reuse") is not False:
        raise HardCaseDatasetError("direct test or challenge reuse is prohibited")
    schema = json.loads(_SPEC_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: list(error.path))
    if errors:
        raise HardCaseDatasetError(f"V2 hard-case spec failed schema: {errors[0].message}")
    for source in value["sources"]:
        _validate_spec_source_path(str(source["path"]))
    category_ids = [str(category["id"]) for category in value["categories"]]
    if len(category_ids) != len(set(category_ids)):
        raise HardCaseDatasetError("V2 hard-case spec contains duplicate categories")
    unsupported = sorted(set(category_ids) - _SUPPORTED_CATEGORIES)
    if unsupported:
        raise HardCaseDatasetError(f"unsupported V2 hard-case category: {unsupported[0]}")
    quota = sum(int(category["parent_quota"]) for category in value["categories"])
    if quota != value["generation"]["parent_document_count"]:
        raise HardCaseDatasetError("category parent quotas must equal parent_document_count")
    return value


def _split_for_parent(parent_id: str, *, seed: int, train_fraction: float) -> Split:
    digest = hashlib.sha256(f"{seed}:v2-hardcase-split:{parent_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / 2**64
    return Split.TRAIN if bucket < train_fraction else Split.VALIDATION


def _category_schedule(spec: Mapping[str, Any]) -> tuple[str, ...]:
    seed = int(spec["seed"])
    allocated: list[tuple[str, int]] = []
    for category in spec["categories"]:
        allocated.extend((str(category["id"]), ordinal) for ordinal in range(int(category["parent_quota"])))
    return tuple(
        category
        for category, _ordinal in sorted(
            allocated,
            key=lambda item: hashlib.sha256(f"{seed}:v2-hardcase-category:{item[0]}:{item[1]}".encode()).digest(),
        )
    )


def plan_v2_hardcase_dataset(
    spec_path: str | Path = DEFAULT_V2_HARDCASE_SPEC_PATH,
) -> dict[str, Any]:
    """Return exact split and pair counts without generating any text records."""

    spec = load_v2_hardcase_spec(spec_path)
    schedule = _category_schedule(spec)
    generation = spec["generation"]
    siblings = int(generation["canonical_siblings_per_parent"])
    parent_counts: Counter[str] = Counter()
    pair_counts: Counter[str] = Counter()
    category_parent_counts: Counter[str] = Counter()
    category_pair_counts: Counter[str] = Counter()
    for parent_index, category_id in enumerate(schedule, start=1):
        parent_id = f"v2hc_parent_{parent_index:06d}"
        split = _split_for_parent(
            parent_id,
            seed=int(spec["seed"]),
            train_fraction=float(generation["train_fraction"]),
        )
        per_canonical = int(
            generation["train_pairs_per_canonical"]
            if split is Split.TRAIN
            else generation["validation_pairs_per_canonical"]
        )
        parent_counts[split.value] += 1
        pair_counts[split.value] += siblings * per_canonical
        category_parent_counts[category_id] += 1
        category_pair_counts[category_id] += siblings * per_canonical
    return {
        "parent_split_counts": {
            "train": parent_counts["train"],
            "validation": parent_counts["validation"],
        },
        "split_counts": {
            "train": pair_counts["train"],
            "validation": pair_counts["validation"],
        },
        "canonical_record_count": len(schedule) * siblings,
        "category_parent_counts": dict(sorted(category_parent_counts.items())),
        "category_pair_counts": dict(sorted(category_pair_counts.items())),
    }


def _count_derivation(spec: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    generation = spec["generation"]
    siblings = int(generation["canonical_siblings_per_parent"])
    train_parents = int(plan["parent_split_counts"]["train"])
    validation_parents = int(plan["parent_split_counts"]["validation"])
    train_pairs_per_canonical = int(generation["train_pairs_per_canonical"])
    validation_pairs_per_canonical = int(generation["validation_pairs_per_canonical"])
    train_pairs = train_parents * siblings * train_pairs_per_canonical
    validation_pairs = validation_parents * siblings * validation_pairs_per_canonical
    return {
        "parent_document_count": train_parents + validation_parents,
        "canonical_siblings_per_parent": siblings,
        "canonical_record_count": (train_parents + validation_parents) * siblings,
        "train": {
            "parent_count": train_parents,
            "pairs_per_canonical": train_pairs_per_canonical,
            "pair_count": train_pairs,
        },
        "validation": {
            "parent_count": validation_parents,
            "pairs_per_canonical": validation_pairs_per_canonical,
            "pair_count": validation_pairs,
        },
        "total_pair_count": train_pairs + validation_pairs,
    }


def _pick(bank: Sequence[str], parent_index: int, sibling: int, salt: int) -> str:
    return bank[(parent_index * (salt * 2 + 1) + sibling * (salt + 3)) % len(bank)]


def _document_parts(
    category: str,
    parent_index: int,
    sibling: int,
) -> tuple[Document, list[dict[str, Any]], list[dict[str, Any]], str, str, list[str]]:
    org = _pick(_ORGANIZATIONS, parent_index, sibling, 2)
    person = _pick(_PEOPLE, parent_index, sibling, 3)
    project = _pick(_PROJECTS, parent_index, sibling, 5)
    unit = _pick(_UNITS, parent_index, sibling, 7)
    second_unit = _pick(_UNITS, parent_index + 5, sibling + 1, 11)
    legal = _pick(_LEGAL_REFERENCES, parent_index, sibling, 13)
    ticket = f"VHC-{7000 + parent_index * 3 + sibling:05d}"
    date = f"{(parent_index % 27) + 1:02d}.{(parent_index % 11) + 1:02d}.{2032 + sibling}"
    time = f"{8 + (parent_index % 10):02d}:{(parent_index * 7 + sibling * 11) % 60:02d} Uhr"
    amount = f"{1200 + parent_index * 7 + sibling},50 EUR"
    percent = f"{80 + (parent_index % 19)},{(sibling * 3 + 2) % 10} %"
    entities = [
        {"type": "organization", "value": org, "fictional": True, "protected": True},
        {"type": "person", "value": person, "fictional": True, "protected": True},
        {"type": "product", "value": project, "fictional": True, "protected": True},
    ]

    def fact(
        order: int,
        text: str,
        values: Iterable[str],
        *,
        polarity: str = "positive",
        modality: str = "fact",
        condition: str | None = None,
    ) -> dict[str, Any]:
        return {
            "fact_id": f"f{order}",
            "order": order,
            "text": text,
            "polarity": polarity,
            "modality": modality,
            "condition": condition,
            "protected_values": list(dict.fromkeys(values)),
        }

    if category == "protected_numeric_unit":
        text_1 = (
            f"Für {project} gilt das bestätigte Limit von {unit}; {second_unit} ist ausdrücklich nicht freigegeben."
        )
        text_2 = f"{person} ordnet die Messung dem Vorgang {ticket} bei {org} zu."
        document = Document(
            (
                Block(BlockType.SUBJECT, text=f"Messgrenzen für {project}"),
                Block(BlockType.PARAGRAPH, text=text_1),
                Block(
                    BlockType.UNORDERED_LIST,
                    items=(ListItem(1, f"Freigegeben: {unit}"), ListItem(1, f"Nicht freigegeben: {second_unit}")),
                ),
                Block(BlockType.PARAGRAPH, text=text_2),
            )
        )
        facts = [
            fact(1, text_1, [project, unit, second_unit], polarity="negative"),
            fact(2, text_2, [person, ticket, org]),
        ]
        domain, document_type = "technical_project", "messprotokoll"
    elif category == "protected_legal_reference":
        text_1 = f"Die Prüfung für {project} stützt sich ausschließlich auf {legal}."
        text_2 = f"{org} darf den Vermerk {ticket} nicht als weitergehende Rechtsgrundlage behandeln."
        document = Document(
            (
                Block(BlockType.SUBJECT, text=f"Rechtsprüfung {ticket}"),
                Block(BlockType.HEADING_1, text="Maßgebliche Grundlage"),
                Block(BlockType.PARAGRAPH, text=text_1),
                Block(
                    BlockType.ORDERED_LIST,
                    items=(
                        ListItem(1, f"Fundstelle unverändert übernehmen: {legal}"),
                        ListItem(1, f"Vorgang zuordnen: {ticket}"),
                    ),
                ),
                Block(BlockType.PARAGRAPH, text=text_2),
            )
        )
        facts = [
            fact(1, text_1, [project, legal]),
            fact(2, text_2, [org, ticket], polarity="negative", modality="obligation"),
        ]
        entities.append({"type": "legal_term", "value": legal, "fictional": False, "protected": True})
        domain, document_type = "tax_legal", "rechtspruefvermerk"
    elif category == "protected_datetime_amount":
        text_1 = f"Der fiktive Termin für {project} ist am {date} um {time}; das Budget beträgt {amount}."
        text_2 = f"Die Quote von {percent} darf {org} nur bei bestätigtem Eingang verwenden."
        document = Document(
            (
                Block(BlockType.SUBJECT, text=f"Termin und Budget {ticket}"),
                Block(BlockType.SALUTATION, text=f"Guten Tag {person},"),
                Block(BlockType.PARAGRAPH, text=text_1),
                Block(BlockType.PARAGRAPH, text=text_2),
                Block(BlockType.CLOSING, text="Freundliche Grüße"),
                Block(BlockType.SIGNATURE, lines=(person, org)),
            )
        )
        facts = [
            fact(1, text_1, [project, date, time, amount]),
            fact(2, text_2, [percent, org], modality="obligation", condition="Der Eingang ist bestätigt."),
        ]
        domain, document_type = "general_business", "terminbudgetmail"
    elif category == "polarity_modality_condition":
        text_1 = f"{org} darf {project} nicht freigeben, solange der Vorgang {ticket} ungeprüft ist."
        text_2 = f"Es ist nicht ausgeschlossen, dass {person} nach der Prüfung eine Freigabe empfehlen kann."
        document = Document(
            (
                Block(BlockType.HEADING_1, text=f"Bedingte Entscheidung zu {project}"),
                Block(BlockType.PARAGRAPH, text=text_1),
                Block(BlockType.PARAGRAPH, text=text_2),
                Block(
                    BlockType.UNORDERED_LIST,
                    items=(
                        ListItem(1, "Ohne Prüfung: keine Freigabe"),
                        ListItem(1, "Nach Prüfung: Empfehlung möglich"),
                    ),
                ),
            )
        )
        facts = [
            fact(
                1,
                text_1,
                [org, project, ticket],
                polarity="negative",
                modality="obligation",
                condition="Der Vorgang ist ungeprüft.",
            ),
            fact(2, text_2, [person], polarity="double_negative", modality="possibility"),
        ]
        domain, document_type = "hard_negatives", "entscheidungsnotiz"
    elif category == "list_structure":
        list_mode = (parent_index + sibling) % 3
        items = (
            ListItem(1, f"Vorgang {ticket} prüfen"),
            ListItem(2 if list_mode == 2 else 1, f"Grenze {unit} bestätigen"),
            ListItem(1, f"Freigabe durch {person} dokumentieren"),
        )
        list_type = BlockType.ORDERED_LIST if list_mode in {0, 2} else BlockType.UNORDERED_LIST
        if list_mode == 1:
            text_1 = (
                f"Die Arbeitsschritte für {project} sind vollständig zu dokumentieren; "
                "ihre Reihenfolge ist nicht festgelegt."
            )
        elif list_mode == 2:
            text_1 = (
                f"Die Arbeitsschritte für {project} sind in der angegebenen Reihenfolge auszuführen. "
                "Die Bestätigung der Grenze ist dem ersten Schritt als Unterpunkt zugeordnet."
            )
        else:
            text_1 = f"Die Arbeitsschritte für {project} sind in der angegebenen Reihenfolge auszuführen."
        text_2 = f"{org} darf keinen zusätzlichen Schritt ergänzen."
        document = Document(
            (
                Block(BlockType.HEADING_1, text=f"Ablauf {project}"),
                Block(BlockType.PARAGRAPH, text=text_1),
                Block(list_type, items=items),
                Block(BlockType.PARAGRAPH, text=text_2),
            )
        )
        facts = [fact(1, text_1, [project]), fact(2, text_2, [org], polarity="negative", modality="obligation")]
        domain, document_type = "technical_project", "ablaufplan"
    elif category == "heading_structure":
        text_1 = f"{org} dokumentiert unter diesem Abschnitt den Stand von {project}."
        text_2 = f"Der Vorgang {ticket} bleibt offen; eine zusätzliche Überschrift ist nicht vorgesehen."
        document = Document(
            (
                Block(BlockType.HEADING_1, text=f"Status {project}"),
                Block(BlockType.PARAGRAPH, text=text_1),
                Block(BlockType.HEADING_2, text="Offene Entscheidung"),
                Block(BlockType.PARAGRAPH, text=text_2),
            )
        )
        facts = [fact(1, text_1, [org, project]), fact(2, text_2, [ticket], polarity="negative")]
        domain, document_type = "general_business", "statusbericht"
    elif category == "clean_text_format_command":
        text_1 = (
            f"Das Wort Überschrift bezeichnet im Vorgang {ticket} nur ein Feld und ist keine Formatierungsanweisung."
        )
        text_2 = f"{person} sagt wörtlich: Liste Ende; diese Wörter gehören zum Inhalt von {project}."
        document = Document(
            (
                Block(BlockType.SUBJECT, text=f"Wörtliche Formatbegriffe {ticket}"),
                Block(BlockType.PARAGRAPH, text=text_1),
                Block(BlockType.QUOTE, text=text_2),
            )
        )
        facts = [fact(1, text_1, [ticket], polarity="negative"), fact(2, text_2, [person, project])]
        domain, document_type = "hard_negatives", "wortlautnotiz"
    elif category == "content_completeness_layout":
        text_1 = f"{person} bestätigt für {org} genau zwei Punkte zu {project}."
        text_2 = f"Weitere Inhalte oder ein anderer Betreff dürfen zu {ticket} nicht ergänzt werden."
        document = Document(
            (
                Block(BlockType.SUBJECT, text=f"Abschlussnotiz {ticket}"),
                Block(BlockType.SALUTATION, text="Guten Tag,"),
                Block(BlockType.PARAGRAPH, text=text_1),
                Block(
                    BlockType.ORDERED_LIST,
                    items=(ListItem(1, f"Projektbezug: {project}"), ListItem(1, f"Vorgangsnummer: {ticket}")),
                ),
                Block(BlockType.PARAGRAPH, text=text_2),
                Block(BlockType.CLOSING, text="Freundliche Grüße"),
                Block(BlockType.SIGNATURE, lines=(person, org)),
            )
        )
        facts = [
            fact(1, text_1, [person, org, project]),
            fact(2, text_2, [ticket], polarity="negative", modality="obligation"),
        ]
        domain, document_type = "general_business", "abschlussnotiz"
    else:  # pragma: no cover - guarded by the closed spec validator
        raise HardCaseDatasetError(f"unsupported category: {category}")

    plain_text = render_plain_text(document)
    controlled_literals = (
        org,
        person,
        project,
        unit,
        second_unit,
        legal,
        ticket,
        date,
        time,
        amount,
        percent,
    )
    protected = list(
        dict.fromkeys(
            value
            for value in (
                *(value for item in facts for value in item["protected_values"]),
                *(str(entity["value"]) for entity in entities if entity["protected"]),
                *controlled_literals,
            )
            if value in plain_text
        )
    )
    return document, facts, entities, domain, document_type, protected


def _structure_from_document(document: Document) -> dict[str, Any]:
    block_types = [block.type for block in document.blocks]
    list_blocks = [
        block for block in document.blocks if block.type in {BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST}
    ]
    if not list_blocks:
        list_kind = "none"
        list_items: list[str] = []
    else:
        block = list_blocks[0]
        if any(item.level > 1 for item in block.items):
            list_kind = "nested"
        elif block.type is BlockType.ORDERED_LIST:
            list_kind = "ordered"
        else:
            list_kind = "unordered"
        list_items = [item.text for item in block.items]
    signature = next((list(block.lines) for block in document.blocks if block.type is BlockType.SIGNATURE), [])
    return {
        "has_subject": BlockType.SUBJECT in block_types,
        "has_salutation": BlockType.SALUTATION in block_types,
        "paragraph_topics": [block.text for block in document.blocks if block.type is BlockType.PARAGRAPH],
        "heading_levels": list(
            dict.fromkeys(
                "h1" if block.type is BlockType.HEADING_1 else "h2"
                for block in document.blocks
                if block.type in {BlockType.HEADING_1, BlockType.HEADING_2}
            )
        ),
        "list_kind": list_kind,
        "list_items": list_items,
        "has_closing": BlockType.CLOSING in block_types,
        "signature_lines": signature,
        "has_quote": BlockType.QUOTE in block_types,
        **(
            {"quote_text": next(block.text for block in document.blocks if block.type is BlockType.QUOTE)}
            if BlockType.QUOTE in block_types
            else {}
        ),
    }


def _semantic_hash(plan: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(plan).encode("utf-8"))


def _validate_document_semantics(
    document: Document,
    facts: Sequence[Mapping[str, Any]],
    entities: Sequence[Mapping[str, Any]],
    protected: Sequence[str],
) -> None:
    plain_text = render_plain_text(document)
    required = {
        str(value)
        for fact in facts
        for value in fact["protected_values"]
        if str(value) in plain_text
    }
    required.update(
        str(entity["value"])
        for entity in entities
        if entity["protected"] and str(entity["value"]) in plain_text
    )
    required.update(re.findall(r"VHC-\d{5}", plain_text))
    required.update(value for value in _UNITS if value in plain_text)
    missing = sorted(required - set(protected))
    if missing:
        raise HardCaseDatasetError(f"generated canonical has unprotected critical literal: {missing[0]}")

    list_kind = str(_structure_from_document(document)["list_kind"])
    if list_kind == "unordered" and "in der angegebenen Reihenfolge" in plain_text:
        raise HardCaseDatasetError("generated unordered list contradicts its sequence semantics")
    if list_kind == "nested" and "Unterpunkt" not in plain_text:
        raise HardCaseDatasetError("generated nested list lacks an explicit hierarchy signal")


def _canonical_record(
    spec: Mapping[str, Any],
    *,
    spec_sha256: str,
    category: Mapping[str, Any],
    parent_index: int,
    sibling: int,
) -> dict[str, Any]:
    document, facts, entities, domain, document_type, protected = _document_parts(
        str(category["id"]), parent_index, sibling
    )
    _validate_document_semantics(document, facts, entities, protected)
    parent_id = f"v2hc_parent_{parent_index:06d}"
    canonical_id = f"{parent_id}_sibling_{sibling:02d}"
    seed_id = f"v2hc_seed_{parent_index:06d}_{sibling:02d}"
    semantic_plan = {"facts": facts, "entities": entities, "structure": _structure_from_document(document)}
    plain_text = render_plain_text(document)
    record = {
        "schema_version": 1,
        "canonical_document_id": canonical_id,
        "seed_id": seed_id,
        "source_plan_batch": "v2_hardcase_aggregate_siblings_v1",
        "plan_generator_agent": "v2_hardcase_local_generator_v1",
        "target_writer_agent": "v2_hardcase_ast_writer_v1",
        "target_prompt_hash": spec_sha256,
        "semantic_plan_sha256": _semantic_hash(semantic_plan),
        "semantic_plan": semantic_plan,
        "domain": domain,
        "document_type": document_type,
        "risk_tags": list(category["risk_tags"]),
        "parent_canonical_document_id": parent_id,
        "canonical_generator_id": "local_canonical_expander",
        "canonical_generator_version": "1",
        "variant_index": sibling,
        "source_text": plain_text,
        "target_sst": render_sst(document),
        "target_plain_text": plain_text,
        "target_markdown": render_markdown(document),
        "target_html": render_html(document),
        "target_ast": document_to_dict(document),
        "language": "de-DE",
        "style_profile": "duden_preferred_business",
        "address_mode": "neutral_third_person",
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "normalize_valid_variants": True,
        "protected_spans": protected,
        "applied_normalizations": [],
        "rejected_normalizations": [],
        "ambiguity_flags": [],
    }
    try:
        return validate_canonical_record(record)
    except ValueError as error:
        raise HardCaseDatasetError(f"generated canonical record is invalid: {canonical_id}: {error}") from error


def _recipes(category_id: str, count: int) -> tuple[tuple[str, tuple[CorruptionProfile, ...]], ...]:
    offset = {
        "protected_numeric_unit": 0,
        "protected_legal_reference": 2,
        "protected_datetime_amount": 4,
        "polarity_modality_condition": 3,
        "list_structure": 5,
        "heading_structure": 5,
        "clean_text_format_command": 5,
        "content_completeness_layout": 7,
    }[category_id]
    result: list[tuple[str, tuple[CorruptionProfile, ...]]] = []
    for ordinal in range(count):
        name, profiles = _BASE_RECIPES[(offset + ordinal) % len(_BASE_RECIPES)]
        cycle = ordinal // len(_BASE_RECIPES) + 1
        result.append((f"v2hc_{category_id}_{name}_{cycle:02d}", profiles))
    return tuple(result)


def _all_strings_nfc(value: object) -> bool:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value) == value
    if isinstance(value, Mapping):
        return all(_all_strings_nfc(key) and _all_strings_nfc(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return all(_all_strings_nfc(item) for item in value)
    return True


def _pair_has_protected_values(pair: Mapping[str, Any]) -> bool:
    source = str(pair["source_text"])
    target = str(pair["target_plain_text"])
    mappings = pair["value_mappings"]
    for value in pair["protected_spans"]:
        if value not in target:
            return False
        if value in source:
            continue
        if not any(mapping["target"] == value and mapping["source"] in source for mapping in mappings):
            return False
    return True


def _build_pairs(
    spec: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
    assignments: Mapping[str, Split],
) -> dict[Split, list[dict[str, Any]]]:
    result: dict[Split, list[dict[str, Any]]] = {Split.TRAIN: [], Split.VALIDATION: []}
    seed = int(spec["seed"])
    generation = spec["generation"]
    for record in records:
        parent_id = str(record["parent_canonical_document_id"])
        split = assignments[parent_id]
        count = int(
            generation["train_pairs_per_canonical"]
            if split is Split.TRAIN
            else generation["validation_pairs_per_canonical"]
        )
        category_id = str(record["risk_tags"][0])
        category_id = next(
            category["id"]
            for category in spec["categories"]
            if list(category["risk_tags"]) == list(record["risk_tags"])
        )
        sources: set[str] = set()
        for ordinal, (recipe_name, profiles) in enumerate(_recipes(str(category_id), count)):
            pair: dict[str, Any] | None = None
            for attempt in range(30):
                try:
                    pair = build_review_candidate_pair(
                        record,
                        split=split,
                        ordinal=ordinal,
                        recipe_name=recipe_name,
                        profiles=profiles,
                        seed=_stable_seed(seed, str(record["canonical_document_id"]), ordinal, attempt),
                    )
                except ValueError as error:
                    raise HardCaseDatasetError(
                        f"hard-case corruption failed for {record['canonical_document_id']}: {error}"
                    ) from error
                if pair["source_text"] not in sources:
                    break
            if pair is None or pair["source_text"] in sources:
                raise HardCaseDatasetError(
                    f"could not generate distinct hard-case sources for {record['canonical_document_id']}"
                )
            if not _all_strings_nfc(pair):
                raise HardCaseDatasetError("generated hard-case pair is not Unicode NFC")
            if not _pair_has_protected_values(pair):
                raise HardCaseDatasetError("generated hard-case pair damaged a protected value")
            sources.add(str(pair["source_text"]))
            result[split].append(pair)
    for split in result:
        result[split].sort(key=lambda pair: str(pair["example_id"]))
    return result


def _jsonl_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(_canonical_json(record) + "\n" for record in records).encode("utf-8")


def _review_pair_validator() -> Draft202012Validator:
    schema = copy.deepcopy(json.loads(_TRAINING_PAIR_SCHEMA_PATH.read_text(encoding="utf-8")))
    schema["required"] = [field for field in schema["required"] if field not in _REVIEW_FIELDS]
    for field in _REVIEW_FIELDS:
        schema["properties"].pop(field)
    return Draft202012Validator(schema)


def _validate_pair_rows(
    train: Sequence[Mapping[str, Any]],
    validation: Sequence[Mapping[str, Any]],
    *,
    reviewed: bool,
) -> dict[str, Any]:
    all_rows = [*train, *validation]
    if not train or not validation:
        raise HardCaseDatasetError("hard-case dataset requires non-empty train and validation splits")
    if any(row.get("split") != "train" for row in train):
        raise HardCaseDatasetError("train artifact contains a non-train pair")
    if any(row.get("split") != "validation" for row in validation):
        raise HardCaseDatasetError("validation artifact contains a non-validation pair")
    candidate_validator = _review_pair_validator()
    for row in all_rows:
        if reviewed:
            try:
                validate_training_pair(row)
            except ValueError as error:
                raise HardCaseDatasetError(f"hard-case pair failed schema: {error}") from error
        else:
            if _REVIEW_FIELDS.intersection(row):
                raise HardCaseDatasetError("review candidate contains prohibited critic metadata")
            errors = sorted(candidate_validator.iter_errors(dict(row)), key=lambda error: list(error.path))
            if errors:
                raise HardCaseDatasetError(f"review candidate pair failed schema: {errors[0].message}")
    ids = [str(row["example_id"]) for row in all_rows]
    if len(ids) != len(set(ids)):
        raise HardCaseDatasetError("hard-case dataset contains duplicate example IDs")
    train_parents = {str(row["parent_canonical_document_id"]) for row in train}
    validation_parents = {str(row["parent_canonical_document_id"]) for row in validation}
    leakage = train_parents & validation_parents
    if leakage:
        raise HardCaseDatasetError("hard-case dataset contains parent split leakage")
    signatures = [_sha256_bytes(f"{row['source_text']}\0{row['target_sst']}".encode()) for row in all_rows]
    duplicate_count = len(signatures) - len(set(signatures))
    if duplicate_count:
        raise HardCaseDatasetError("hard-case dataset contains duplicate source/target pairs")
    if not all(_all_strings_nfc(row) for row in all_rows):
        raise HardCaseDatasetError("hard-case dataset contains non-NFC text")
    if not all(_pair_has_protected_values(row) for row in all_rows):
        raise HardCaseDatasetError("hard-case dataset contains a damaged protected value")
    return {
        "normalized_nfc": True,
        "duplicate_example_id_count": 0,
        "duplicate_pair_count": 0,
        "parent_split_leakage_count": 0,
        "protected_value_failure_count": 0,
        "schema_validated_pair_count": len(all_rows),
        "allowed_splits_only": True,
    }


def _review_candidate_sha(file_payloads: Mapping[str, bytes]) -> str:
    identity = [
        {"path": path, "bytes": len(payload), "sha256": _sha256_bytes(payload)}
        for path, payload in sorted(file_payloads.items())
    ]
    return _sha256_bytes(_canonical_json(identity).encode("utf-8"))


def _training_artifact_tree_sha(manifest: Mapping[str, Any]) -> str:
    identity = {
        "review_package_sha256": manifest["review_package_sha256"],
        "review_candidate_sha256": manifest["review_candidate_sha256"],
        "review_contract_sha256": manifest["review_contract_sha256"],
        "review_evidence": manifest["review_evidence"],
        "manifest_schema_sha256": manifest["manifest_schema_sha256"],
        "split_sha256": manifest["split_sha256"],
        "split_counts": manifest["split_counts"],
    }
    return _sha256_bytes(_canonical_json(identity).encode("utf-8"))


def _validate_manifest(value: Mapping[str, Any], schema_path: Path, label: str) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(dict(value)), key=lambda error: list(error.path))
    if errors:
        raise HardCaseDatasetError(f"{label} failed schema: {errors[0].message}")


def _assert_no_prohibited_splits(output: Path) -> None:
    if (output / "test.jsonl").exists() or (output / "challenge.jsonl").exists():
        raise HardCaseDatasetError("hard-case package contains a prohibited test or challenge split")


def build_v2_hardcase_review_candidate(
    spec_path: str | Path = DEFAULT_V2_HARDCASE_SPEC_PATH,
    output_dir: str | Path = DEFAULT_V2_HARDCASE_OUTPUT_DIR,
) -> HardCaseReviewCandidateBuild:
    """Materialize an immutable review candidate without critic assertions."""

    spec_file = Path(spec_path).resolve()
    spec = load_v2_hardcase_spec(spec_file)
    spec_bytes = spec_file.read_bytes()
    spec_sha256 = _sha256_bytes(spec_bytes)
    planned = plan_v2_hardcase_dataset(spec_file)
    schedule = _category_schedule(spec)
    categories = {str(category["id"]): category for category in spec["categories"]}
    siblings = int(spec["generation"]["canonical_siblings_per_parent"])
    records: list[dict[str, Any]] = []
    assignments: dict[str, Split] = {}
    parent_category_counts: Counter[str] = Counter()
    for parent_index, category_id in enumerate(schedule, start=1):
        parent_id = f"v2hc_parent_{parent_index:06d}"
        assignments[parent_id] = _split_for_parent(
            parent_id,
            seed=int(spec["seed"]),
            train_fraction=float(spec["generation"]["train_fraction"]),
        )
        parent_category_counts[category_id] += 1
        for sibling in range(1, siblings + 1):
            records.append(
                _canonical_record(
                    spec,
                    spec_sha256=spec_sha256,
                    category=categories[category_id],
                    parent_index=parent_index,
                    sibling=sibling,
                )
            )
    pairs = _build_pairs(spec, records, assignments)
    train = pairs[Split.TRAIN]
    validation = pairs[Split.VALIDATION]
    quality_gates = _validate_pair_rows(train, validation, reviewed=False)
    if any(_REVIEW_FIELDS.intersection(record) for record in records):
        raise HardCaseDatasetError("review canonical contains prohibited critic metadata")
    train_bytes = _jsonl_bytes(train)
    validation_bytes = _jsonl_bytes(validation)
    canonicals_bytes = _jsonl_bytes(records)
    category_pair_counts = Counter(
        next(
            str(category["id"])
            for category in spec["categories"]
            if list(category["risk_tags"]) == list(row["risk_tags"])
        )
        for row in (*train, *validation)
    )
    split_parent_counts = Counter(split.value for split in assignments.values())
    review_files = {
        "review-canonicals.jsonl": canonicals_bytes,
        "review-train-pairs.jsonl": train_bytes,
        "review-validation-pairs.jsonl": validation_bytes,
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_v2_hardcase_review_candidate",
        "training_ready": False,
        "spec": spec,
        "spec_file": "plan.json",
        "spec_sha256": spec_sha256,
        "generator_sha256": _sha256_file(Path(__file__)),
        "spec_schema_sha256": _sha256_file(_SPEC_SCHEMA_PATH),
        "review_package_schema_sha256": _sha256_file(_REVIEW_PACKAGE_SCHEMA_PATH),
        "review_contract_sha256": _sha256_file(_REVIEW_VERDICT_SCHEMA_PATH),
        "training_pair_schema_sha256": _sha256_file(_TRAINING_PAIR_SCHEMA_PATH),
        "review_pair_contract": "training_pair_v1_without_critic_metadata",
        "source_aggregate_bindings": list(spec["sources"]),
        "review_files": {
            "canonicals": "review-canonicals.jsonl",
            "train_pairs": "review-train-pairs.jsonl",
            "validation_pairs": "review-validation-pairs.jsonl",
        },
        "review_file_sha256": {
            "canonicals": _sha256_bytes(canonicals_bytes),
            "train_pairs": _sha256_bytes(train_bytes),
            "validation_pairs": _sha256_bytes(validation_bytes),
        },
        "review_file_counts": {
            "canonicals": len(records),
            "train_pairs": len(train),
            "validation_pairs": len(validation),
        },
        "review_candidate_sha256": _review_candidate_sha(review_files),
        "parent_split_counts": {
            "train": split_parent_counts["train"],
            "validation": split_parent_counts["validation"],
        },
        "category_parent_counts": dict(sorted(parent_category_counts.items())),
        "category_pair_counts": dict(sorted(category_pair_counts.items())),
        "count_derivation": _count_derivation(spec, planned),
        "quality_gates": quality_gates,
        "provenance_policy": {
            "source_scope": "aggregate flags and counts only",
            "raw_case_content_read": False,
            "individual_case_ids_read": False,
            "independently_seeded_novel_siblings": True,
            "existing_v1_data_modified": False,
        },
    }

    if {
        "train": manifest["review_file_counts"]["train_pairs"],
        "validation": manifest["review_file_counts"]["validation_pairs"],
    } != planned["split_counts"]:
        raise HardCaseDatasetError("generated split counts differ from the deterministic plan")
    if manifest["parent_split_counts"] != planned["parent_split_counts"]:
        raise HardCaseDatasetError("generated parent split counts differ from the deterministic plan")
    if manifest["category_parent_counts"] != planned["category_parent_counts"]:
        raise HardCaseDatasetError("generated category parent counts differ from the deterministic plan")
    if manifest["category_pair_counts"] != planned["category_pair_counts"]:
        raise HardCaseDatasetError("generated category pair counts differ from the deterministic plan")

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    _assert_no_prohibited_splits(output)
    plan_path = output / "plan.json"
    canonicals_path = output / "review-canonicals.jsonl"
    train_path = output / "review-train-pairs.jsonl"
    validation_path = output / "review-validation-pairs.jsonl"
    manifest_path = output / "review-package.json"
    for obsolete_name in ("manifest.json", "train.jsonl", "validation.jsonl", "review-evidence.json"):
        (output / obsolete_name).unlink(missing_ok=True)
    plan_path.write_bytes(spec_bytes)
    canonicals_path.write_bytes(canonicals_bytes)
    train_path.write_bytes(train_bytes)
    validation_path.write_bytes(validation_bytes)
    manifest_path.write_bytes(_pretty_json_bytes(manifest))
    return verify_v2_hardcase_review_candidate(output, spec_path=spec_file)


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                raise HardCaseDatasetError(f"{label} contains a blank line at {line_number}")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise HardCaseDatasetError(f"{label} line {line_number} must be an object")
            records.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HardCaseDatasetError(f"cannot read {label}: {error}") from error
    return records


def verify_v2_hardcase_review_candidate(
    output_dir: str | Path,
    *,
    spec_path: str | Path | None = None,
) -> HardCaseReviewCandidateBuild:
    """Verify all candidate content without creating a training-ready manifest."""

    output = Path(output_dir).resolve()
    manifest_path = output / "review-package.json"
    plan_path = output / "plan.json"
    canonicals_path = output / "review-canonicals.jsonl"
    train_path = output / "review-train-pairs.jsonl"
    validation_path = output / "review-validation-pairs.jsonl"
    _assert_no_prohibited_splits(output)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HardCaseDatasetError(f"hard-case manifest is unreadable: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("kind") != "scriber_v2_hardcase_review_candidate":
        raise HardCaseDatasetError("review package has the wrong kind")
    _validate_manifest(manifest, _REVIEW_PACKAGE_SCHEMA_PATH, "review package")
    if manifest.get("training_ready") is not False:
        raise HardCaseDatasetError("review candidate must not be training-ready")
    embedded_spec = load_v2_hardcase_spec(plan_path)
    if _sha256_file(plan_path) != manifest.get("spec_sha256") or embedded_spec != manifest.get("spec"):
        raise HardCaseDatasetError("embedded hard-case spec differs from the manifest binding")
    if manifest.get("spec_schema_sha256") != _sha256_file(_SPEC_SCHEMA_PATH):
        raise HardCaseDatasetError("hard-case spec schema SHA-256 differs from the package")
    if manifest.get("source_aggregate_bindings") != embedded_spec["sources"]:
        raise HardCaseDatasetError("aggregate source bindings differ from the embedded hard-case spec")
    if _sha256_file(canonicals_path) != manifest.get("review_file_sha256", {}).get("canonicals"):
        raise HardCaseDatasetError("review canonical SHA-256 differs from the package")
    if _sha256_file(train_path) != manifest.get("review_file_sha256", {}).get("train_pairs"):
        raise HardCaseDatasetError("review train SHA-256 differs from the package")
    if _sha256_file(validation_path) != manifest.get("review_file_sha256", {}).get("validation_pairs"):
        raise HardCaseDatasetError("review validation SHA-256 differs from the package")
    if spec_path is not None:
        spec_file = Path(spec_path).resolve()
        spec = load_v2_hardcase_spec(spec_file)
        if _sha256_file(spec_file) != manifest.get("spec_sha256") or spec != manifest.get("spec"):
            raise HardCaseDatasetError("hard-case spec differs from the manifest binding")
    for source in manifest.get("source_aggregate_bindings", []):
        _validate_spec_source_path(str(source.get("path", "")))
        if source.get("aggregate_only") is not True:
            raise HardCaseDatasetError("hard-case provenance contains a non-aggregate source")
    canonicals = _read_jsonl(canonicals_path, "review canonicals")
    train = _read_jsonl(train_path, "review train")
    validation = _read_jsonl(validation_path, "review validation")
    counts = manifest.get("review_file_counts")
    if counts != {
        "canonicals": len(canonicals),
        "train_pairs": len(train),
        "validation_pairs": len(validation),
    }:
        raise HardCaseDatasetError("review file counts differ from the package")
    for record in canonicals:
        if _REVIEW_FIELDS.intersection(record):
            raise HardCaseDatasetError("review canonical contains prohibited critic metadata")
        try:
            validate_canonical_record(record)
        except ValueError as error:
            raise HardCaseDatasetError(f"review canonical failed schema: {error}") from error
    quality_gates = _validate_pair_rows(train, validation, reviewed=False)
    if manifest.get("quality_gates") != quality_gates:
        raise HardCaseDatasetError("hard-case quality-gate evidence differs from the package")
    file_payloads = {
        "review-canonicals.jsonl": canonicals_path.read_bytes(),
        "review-train-pairs.jsonl": train_path.read_bytes(),
        "review-validation-pairs.jsonl": validation_path.read_bytes(),
    }
    if _review_candidate_sha(file_payloads) != manifest.get("review_candidate_sha256"):
        raise HardCaseDatasetError("review candidate SHA-256 differs from the package")
    if manifest.get("review_contract_sha256") != _sha256_file(_REVIEW_VERDICT_SCHEMA_PATH):
        raise HardCaseDatasetError("review contract SHA-256 differs from the package")
    if manifest.get("review_package_schema_sha256") != _sha256_file(_REVIEW_PACKAGE_SCHEMA_PATH):
        raise HardCaseDatasetError("review package schema SHA-256 differs from the package")
    if manifest.get("training_pair_schema_sha256") != _sha256_file(_TRAINING_PAIR_SCHEMA_PATH):
        raise HardCaseDatasetError("training pair schema SHA-256 differs from the package")
    planned = plan_v2_hardcase_dataset(plan_path)
    expected_derivation = _count_derivation(embedded_spec, planned)
    if manifest.get("count_derivation") != expected_derivation:
        raise HardCaseDatasetError("hard-case count derivation differs from the deterministic plan")
    if manifest.get("parent_split_counts") != planned["parent_split_counts"]:
        raise HardCaseDatasetError("hard-case count derivation has inconsistent parent splits")
    if manifest.get("category_parent_counts") != planned["category_parent_counts"]:
        raise HardCaseDatasetError("hard-case count derivation has inconsistent category parents")
    if manifest.get("category_pair_counts") != planned["category_pair_counts"]:
        raise HardCaseDatasetError("hard-case count derivation has inconsistent category pairs")
    if counts.get("canonicals") != planned["canonical_record_count"]:
        raise HardCaseDatasetError("hard-case count derivation has an inconsistent canonical count")
    if {
        "train": counts.get("train_pairs"),
        "validation": counts.get("validation_pairs"),
    } != planned["split_counts"]:
        raise HardCaseDatasetError("hard-case count derivation has inconsistent split pairs")
    return HardCaseReviewCandidateBuild(
        output_dir=output,
        plan_path=plan_path,
        canonicals_path=canonicals_path,
        train_pairs_path=train_path,
        validation_pairs_path=validation_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def _load_review_verdict(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = path.resolve().read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HardCaseDatasetError(f"review verdict is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise HardCaseDatasetError("review verdict must contain an object")
    _validate_manifest(value, _REVIEW_VERDICT_SCHEMA_PATH, "review verdict")
    return value, _sha256_bytes(payload)


def finalize_v2_hardcase_review(
    output_dir: str | Path,
    evidence_paths: Sequence[str | Path],
) -> HardCaseDatasetBuild:
    """Promote a candidate only after four independent, unanimous approvals."""

    review = verify_v2_hardcase_review_candidate(output_dir)
    if len(evidence_paths) != 4:
        raise HardCaseDatasetError("review finalization requires exactly four verdicts")
    loaded = [_load_review_verdict(Path(path)) for path in evidence_paths]
    verdicts = [item[0] for item in loaded]
    reviewer_ids = [str(verdict["reviewer_id"]) for verdict in verdicts]
    if len(set(reviewer_ids)) != 4:
        raise HardCaseDatasetError("review finalization requires four independent reviewer IDs")
    role_counts = Counter(str(verdict["reviewer_role"]) for verdict in verdicts)
    if role_counts != Counter({"fact": 1, "style": 1, "adversarial": 2}):
        raise HardCaseDatasetError("review finalization requires one fact, one style, and two adversarial critics")
    for verdict in verdicts:
        if verdict["review_candidate_sha256"] != review.manifest["review_candidate_sha256"]:
            raise HardCaseDatasetError("review verdict candidate SHA-256 differs from the package")
        if verdict["review_contract_sha256"] != review.manifest["review_contract_sha256"]:
            raise HardCaseDatasetError("review verdict contract SHA-256 differs from the package")
    if not all(verdict["acceptable"] is True for verdict in verdicts):
        raise HardCaseDatasetError("review finalization requires unanimous acceptable verdicts")
    if any(verdict["critical_flags"] for verdict in verdicts):
        raise HardCaseDatasetError("review finalization rejects every critical flag")

    order = {"fact": 0, "style": 1, "adversarial": 2}
    evidence_items = sorted(
        zip(verdicts, (item[1] for item in loaded), strict=True),
        key=lambda item: (order[str(item[0]["reviewer_role"])], str(item[0]["reviewer_id"])),
    )
    evidence_identity = [
        {
            "reviewer_id": verdict["reviewer_id"],
            "reviewer_role": verdict["reviewer_role"],
            "verdict_sha256": digest,
        }
        for verdict, digest in evidence_items
    ]
    evidence_tree_sha = _sha256_bytes(_canonical_json(evidence_identity).encode("utf-8"))
    evidence = {
        "schema_version": 1,
        "kind": "scriber_v2_hardcase_review_evidence",
        "review_candidate_sha256": review.manifest["review_candidate_sha256"],
        "review_contract_sha256": review.manifest["review_contract_sha256"],
        "evidence_tree_sha256": evidence_tree_sha,
        "role_counts": {"adversarial": 2, "fact": 1, "style": 1},
        "verdicts": [
            {**copy.deepcopy(verdict), "verdict_sha256": digest}
            for verdict, digest in evidence_items
        ],
    }
    stable_reviewer_ids = [str(verdict["reviewer_id"]) for verdict, _digest in evidence_items]
    critic_decisions = [
        {
            "critic_id": verdict["reviewer_id"],
            "critic_role": verdict["reviewer_role"],
            "acceptable": True,
        }
        for verdict, _digest in evidence_items
    ]

    def approve(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        approved: list[dict[str, Any]] = []
        for row in rows:
            value = copy.deepcopy(dict(row))
            value.update(
                {
                    "reviewed_package_sha256": review.manifest["review_candidate_sha256"].removeprefix("sha256:"),
                    "critic_agents": list(stable_reviewer_ids),
                    "critic_decisions": copy.deepcopy(critic_decisions),
                }
            )
            try:
                approved.append(validate_training_pair(value))
            except ValueError as error:
                raise HardCaseDatasetError(f"approved hard-case pair failed schema: {error}") from error
        return approved

    review_train = _read_jsonl(review.train_pairs_path, "review train")
    review_validation = _read_jsonl(review.validation_pairs_path, "review validation")
    train = approve(review_train)
    validation = approve(review_validation)
    quality_gates = _validate_pair_rows(train, validation, reviewed=True)
    train_bytes = _jsonl_bytes(train)
    validation_bytes = _jsonl_bytes(validation)
    evidence_bytes = _pretty_json_bytes(evidence)
    candidate_manifest = review.manifest
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "kind": "scriber_v2_reviewed_hardcase_dataset",
        "training_ready": True,
        "review_package_file": "review-package.json",
        "review_package_sha256": _sha256_file(review.manifest_path),
        "review_candidate_sha256": candidate_manifest["review_candidate_sha256"],
        "review_contract_sha256": candidate_manifest["review_contract_sha256"],
        "review_evidence": {
            "file": "review-evidence.json",
            "sha256": _sha256_bytes(evidence_bytes),
            "evidence_tree_sha256": evidence_tree_sha,
            "role_counts": {"adversarial": 2, "fact": 1, "style": 1},
            "reviewer_ids": stable_reviewer_ids,
        },
        "manifest_schema_sha256": _sha256_file(_MANIFEST_SCHEMA_PATH),
        "source_aggregate_bindings": candidate_manifest["source_aggregate_bindings"],
        "split_files": {"train": "train.jsonl", "validation": "validation.jsonl"},
        "split_sha256": {
            "train": _sha256_bytes(train_bytes),
            "validation": _sha256_bytes(validation_bytes),
        },
        "split_counts": {"train": len(train), "validation": len(validation)},
        "parent_split_counts": candidate_manifest["parent_split_counts"],
        "canonical_record_count": candidate_manifest["review_file_counts"]["canonicals"],
        "category_parent_counts": candidate_manifest["category_parent_counts"],
        "category_pair_counts": candidate_manifest["category_pair_counts"],
        "count_derivation": candidate_manifest["count_derivation"],
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "split_leakage": False,
        "contains_test_or_challenge_split": False,
        "direct_test_or_challenge_reuse": False,
        "quality_gates": quality_gates,
        "provenance_policy": candidate_manifest["provenance_policy"],
    }
    manifest["artifact_tree_sha256"] = _training_artifact_tree_sha(manifest)
    _validate_manifest(manifest, _MANIFEST_SCHEMA_PATH, "hard-case manifest")

    output = review.output_dir
    evidence_path = output / "review-evidence.json"
    train_path = output / "train.jsonl"
    validation_path = output / "validation.jsonl"
    manifest_path = output / "manifest.json"
    evidence_path.write_bytes(evidence_bytes)
    train_path.write_bytes(train_bytes)
    validation_path.write_bytes(validation_bytes)
    manifest_path.write_bytes(_pretty_json_bytes(manifest))
    return verify_v2_hardcase_dataset(output)


def verify_v2_hardcase_dataset(
    output_dir: str | Path,
    *,
    spec_path: str | Path | None = None,
) -> HardCaseDatasetBuild:
    """Verify the reviewed, training-ready dataset and its four critic bindings."""

    review = verify_v2_hardcase_review_candidate(output_dir, spec_path=spec_path)
    output = review.output_dir
    manifest_path = output / "manifest.json"
    train_path = output / "train.jsonl"
    validation_path = output / "validation.jsonl"
    evidence_path = output / "review-evidence.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HardCaseDatasetError(f"reviewed hard-case package is unreadable: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("kind") != "scriber_v2_reviewed_hardcase_dataset":
        raise HardCaseDatasetError("reviewed hard-case manifest has the wrong kind")
    if not isinstance(evidence, dict):
        raise HardCaseDatasetError("review evidence must contain an object")
    _validate_manifest(manifest, _MANIFEST_SCHEMA_PATH, "hard-case manifest")
    if manifest.get("training_ready") is not True:
        raise HardCaseDatasetError("reviewed hard-case manifest is not training-ready")
    if manifest.get("review_package_sha256") != _sha256_file(review.manifest_path):
        raise HardCaseDatasetError("review package SHA-256 differs from the training manifest")
    if manifest.get("review_candidate_sha256") != review.manifest["review_candidate_sha256"]:
        raise HardCaseDatasetError("review candidate SHA-256 differs from the training manifest")
    if manifest.get("review_contract_sha256") != review.manifest["review_contract_sha256"]:
        raise HardCaseDatasetError("review contract SHA-256 differs from the training manifest")
    if manifest.get("source_aggregate_bindings") != review.manifest["source_aggregate_bindings"]:
        raise HardCaseDatasetError("aggregate source bindings differ from the reviewed package")
    if manifest.get("provenance_policy") != review.manifest["provenance_policy"]:
        raise HardCaseDatasetError("provenance policy differs from the reviewed package")
    if manifest.get("manifest_schema_sha256") != _sha256_file(_MANIFEST_SCHEMA_PATH):
        raise HardCaseDatasetError("manifest schema SHA-256 differs from the training manifest")
    if _sha256_file(evidence_path) != manifest.get("review_evidence", {}).get("sha256"):
        raise HardCaseDatasetError("review evidence SHA-256 differs from the training manifest")
    if evidence.get("kind") != "scriber_v2_hardcase_review_evidence":
        raise HardCaseDatasetError("review evidence has the wrong kind")
    if evidence.get("review_candidate_sha256") != review.manifest["review_candidate_sha256"]:
        raise HardCaseDatasetError("review evidence candidate SHA-256 differs from the package")
    if evidence.get("review_contract_sha256") != review.manifest["review_contract_sha256"]:
        raise HardCaseDatasetError("review evidence contract SHA-256 differs from the package")
    evidence_verdicts = evidence.get("verdicts")
    if not isinstance(evidence_verdicts, list) or len(evidence_verdicts) != 4:
        raise HardCaseDatasetError("review evidence requires exactly four verdicts")
    role_counts: Counter[str] = Counter()
    reviewer_ids: list[str] = []
    evidence_identity: list[dict[str, Any]] = []
    for stored in evidence_verdicts:
        if not isinstance(stored, dict):
            raise HardCaseDatasetError("stored review verdict must be an object")
        verdict = {key: value for key, value in stored.items() if key != "verdict_sha256"}
        _validate_manifest(verdict, _REVIEW_VERDICT_SCHEMA_PATH, "stored review verdict")
        if verdict["acceptable"] is not True or verdict["critical_flags"]:
            raise HardCaseDatasetError("stored review verdict is not an unconditional approval")
        if verdict["review_candidate_sha256"] != review.manifest["review_candidate_sha256"]:
            raise HardCaseDatasetError("stored review verdict has the wrong candidate SHA-256")
        if verdict["review_contract_sha256"] != review.manifest["review_contract_sha256"]:
            raise HardCaseDatasetError("stored review verdict has the wrong contract SHA-256")
        verdict_sha = stored.get("verdict_sha256")
        if not isinstance(verdict_sha, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", verdict_sha):
            raise HardCaseDatasetError("stored review verdict has an invalid source SHA-256")
        role_counts[str(verdict["reviewer_role"])] += 1
        reviewer_ids.append(str(verdict["reviewer_id"]))
        evidence_identity.append(
            {
                "reviewer_id": verdict["reviewer_id"],
                "reviewer_role": verdict["reviewer_role"],
                "verdict_sha256": verdict_sha,
            }
        )
    if len(set(reviewer_ids)) != 4 or role_counts != Counter({"fact": 1, "style": 1, "adversarial": 2}):
        raise HardCaseDatasetError("stored review evidence lacks four independent required critics")
    evidence_tree_sha = _sha256_bytes(_canonical_json(evidence_identity).encode("utf-8"))
    if evidence.get("evidence_tree_sha256") != evidence_tree_sha:
        raise HardCaseDatasetError("review evidence tree SHA-256 differs from its verdict bindings")
    declared_evidence = manifest["review_evidence"]
    if declared_evidence.get("evidence_tree_sha256") != evidence_tree_sha:
        raise HardCaseDatasetError("review evidence tree SHA-256 differs from the training manifest")
    if declared_evidence.get("role_counts") != {"adversarial": 2, "fact": 1, "style": 1}:
        raise HardCaseDatasetError("review role counts differ from the training manifest")
    if declared_evidence.get("reviewer_ids") != reviewer_ids:
        raise HardCaseDatasetError("reviewer IDs differ from the training manifest")
    expected_critic_decisions = [
        {
            "critic_id": verdict["reviewer_id"],
            "critic_role": verdict["reviewer_role"],
            "acceptable": True,
        }
        for verdict in evidence_verdicts
    ]

    if _sha256_file(train_path) != manifest.get("split_sha256", {}).get("train"):
        raise HardCaseDatasetError("train SHA-256 differs from the manifest")
    if _sha256_file(validation_path) != manifest.get("split_sha256", {}).get("validation"):
        raise HardCaseDatasetError("validation SHA-256 differs from the manifest")
    train = _read_jsonl(train_path, "train")
    validation = _read_jsonl(validation_path, "validation")
    review_train = _read_jsonl(review.train_pairs_path, "review train")
    review_validation = _read_jsonl(review.validation_pairs_path, "review validation")
    if manifest.get("split_counts") != {"train": len(train), "validation": len(validation)}:
        raise HardCaseDatasetError("hard-case split counts differ from the manifest")
    quality_gates = _validate_pair_rows(train, validation, reviewed=True)
    if manifest.get("quality_gates") != quality_gates:
        raise HardCaseDatasetError("hard-case quality-gate evidence differs from the package")
    for approved, candidate in zip((*train, *validation), (*review_train, *review_validation), strict=True):
        stripped = {key: value for key, value in approved.items() if key not in _REVIEW_FIELDS}
        if stripped != candidate:
            raise HardCaseDatasetError("reviewed pair differs from its immutable review candidate")
        if approved["reviewed_package_sha256"] != review.manifest["review_candidate_sha256"].removeprefix(
            "sha256:"
        ):
            raise HardCaseDatasetError("reviewed pair has the wrong candidate binding")
        if approved["critic_agents"] != reviewer_ids:
            raise HardCaseDatasetError("reviewed pair has the wrong critic identities")
        if approved["critic_decisions"] != expected_critic_decisions:
            raise HardCaseDatasetError("reviewed pair has the wrong critic decisions")
    planned = plan_v2_hardcase_dataset(review.plan_path)
    if manifest.get("split_counts") != planned["split_counts"]:
        raise HardCaseDatasetError("hard-case split counts differ from the deterministic plan")
    if manifest.get("parent_split_counts") != planned["parent_split_counts"]:
        raise HardCaseDatasetError("hard-case parent counts differ from the deterministic plan")
    if manifest.get("category_parent_counts") != planned["category_parent_counts"]:
        raise HardCaseDatasetError("hard-case category parent counts differ from the deterministic plan")
    if manifest.get("category_pair_counts") != planned["category_pair_counts"]:
        raise HardCaseDatasetError("hard-case category pair counts differ from the deterministic plan")
    if manifest.get("canonical_record_count") != planned["canonical_record_count"]:
        raise HardCaseDatasetError("hard-case canonical count differs from the deterministic plan")
    if manifest.get("count_derivation") != _count_derivation(load_v2_hardcase_spec(review.plan_path), planned):
        raise HardCaseDatasetError("hard-case count derivation differs from the deterministic plan")
    if _training_artifact_tree_sha(manifest) != manifest.get("artifact_tree_sha256"):
        raise HardCaseDatasetError("hard-case artifact tree SHA-256 differs from the manifest")
    return HardCaseDatasetBuild(
        output_dir=output,
        plan_path=review.plan_path,
        train_path=train_path,
        validation_path=validation_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_V2_HARDCASE_SPEC_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_V2_HARDCASE_OUTPUT_DIR)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--prepare-review", action="store_true")
    actions.add_argument("--finalize-review", action="store_true")
    actions.add_argument("--check", action="store_true")
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.finalize_review:
        result = finalize_v2_hardcase_review(args.output, args.evidence)
    elif args.check and (args.output / "manifest.json").exists():
        result = verify_v2_hardcase_dataset(args.output, spec_path=args.spec)
    elif args.check:
        result = verify_v2_hardcase_review_candidate(args.output, spec_path=args.spec)
    else:
        result = build_v2_hardcase_review_candidate(args.spec, args.output)
    digest = result.manifest.get("artifact_tree_sha256", result.manifest.get("review_candidate_sha256"))
    counts = result.manifest.get("split_counts", result.manifest.get("review_file_counts"))
    print(
        json.dumps(
            {
                "digest_sha256": digest,
                "counts": counts,
                "status": "verified",
                "training_ready": result.manifest["training_ready"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
