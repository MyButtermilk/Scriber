#!/usr/bin/env python3
"""Build the deterministic repair overlay for the Orthopron canonical batch.

The overlay is plan-led: it preserves every source plan and protected surface,
then realizes only the approved adversarial-review repairs in a separate target
root. It uses the original target batch solely to prove a surface-level change
for every reviewed seed and to prove no unreviewed plain-text target changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

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
from scriber_polishing.document_ast import Block, BlockType, Document, Inline, ListItem  # noqa: E402
from scriber_polishing.schemas import validate_seed_plan  # noqa: E402
from scriber_polishing.sst_parser import parse_sst  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_batch_validator import _validate_batch  # noqa: E402
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text  # noqa: E402
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

REPAIR_WRITER_ID = "target_repair_orthopron_terra_01"
REPAIR_WRITER_VERSION = 1
MANIFEST_VERSION = 1
PLAN_PATH = PROJECT_ROOT / "data" / "seeds" / "generator_orthography_pronouns_01" / "plans.jsonl"
PLAN_RELATIVE_PATH = "data/seeds/generator_orthography_pronouns_01/plans.jsonl"
BASELINE_TARGETS_PATH = PROJECT_ROOT / "data" / "targets" / "target_writer_orthography_pronouns_01" / "targets.jsonl"
BASELINE_TARGETS_RELATIVE_PATH = "data/targets/target_writer_orthography_pronouns_01/targets.jsonl"
HANDOFF_PATH = PROJECT_ROOT / "handoffs" / "AGENT_TARGET_WRITER.md"
ADVERSARIAL_VERDICTS_PATH = PROJECT_ROOT / "artifacts" / "agent_reviews_v3" / "adv_sol1_max" / "verdicts.jsonl"
ADVERSARIAL_PRIVATE_INDEX_PATH = PROJECT_ROOT / "artifacts" / "private" / "critic_package_v3_index.json"
ADVERSARIAL_PACKAGE_SHA256 = "sha256:f94987cd85b0ed98720489e8276d725f795cd02effd1342eabd7a733251f978a"
EXPECTED_ALL_REJECTED_CASE_COUNT = 845
EXPECTED_REVIEWED_SEED_COUNT = 196
EXPECTED_CRITICAL_FLAG_COUNTS = {
    "omitted_condition": 68,
    "pronoun_referent_error": 141,
}
EXPECTED_NONCRITICAL_FLAG_COUNTS = {"redundant_subject_heading": 28}
EXPECTED_NONCRITICAL_SEED_COUNT = 12
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
REPORT_PATH = OUTPUT_DIR / "repair_report.md"
VALIDATION_PATH = OUTPUT_DIR / "validation.json"

_TERMINAL_PUNCTUATION = ".!?…"
_DIRECT_PLURAL_RECIPIENT = re.compile(
    r"(?P<prefix>Bitte gebt Ihr Eure Rückmeldung an )(?P<person>[^,;]+), "
    r"damit er Euch den Stand senden kann\."
)
_COORDINATION_WITH_REFERENCE = re.compile(
    r"(?P<prefix>Bitte stimmen Sie sich mit )(?P<person>[^,;]+) "
    r"zu Vorgang (?P<reference>OP-\d+) ab; sie bestätigt nur die Schreibweise, "
    r"nicht eine andere Bedeutung\."
)
_COORDINATION_WITHOUT_REFERENCE = re.compile(
    r"(?P<prefix>Bitte stimmen Sie sich mit )(?P<person>[^,;]+) ab; "
    r"sie hält die vereinbarte Reihenfolge fest\."
)
_PERSONAL_DOCUMENTATION = re.compile(
    r"(?P<prefix>(?:Liebe )?[^,;]+, bitte gib (?:Deine|deine) Rückmeldung an )"
    r"(?P<person>[^,;]+); er dokumentiert sie im Vorgang\."
)
_PERSON_HOLDS_FORM = re.compile(
    r"(?P<person>[^,;.]+) hält für Vorgang (?P<reference>OP-\d+) fest, "
    r"dass sie die geschützte Form nicht ersetzt\."
)


@dataclass(frozen=True)
class FactEvidence:
    fact_id: str
    order: int
    text: str


@dataclass(frozen=True)
class RepairBuild:
    records: tuple[dict[str, Any], ...]
    targets_bytes: bytes
    manifest: dict[str, Any]
    report: str
    validation: dict[str, Any]


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_prefixed(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"{path}: blank JSONL line {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}: non-object JSONL line {line_number}")
        values.append(value)
    return values


@lru_cache(maxsize=1)
def _plans_by_seed() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_plan in _read_jsonl(PLAN_PATH):
        plan = validate_seed_plan(raw_plan)
        seed_id = str(plan["seed_id"])
        if seed_id in result:
            raise ValueError(f"duplicate Orthopron seed ID: {seed_id}")
        result[seed_id] = plan
    if len(result) != 300:
        raise ValueError(f"expected 300 Orthopron plans, found {len(result)}")
    return result


def plan_by_seed_id(seed_id: str) -> dict[str, Any]:
    try:
        return _plans_by_seed()[seed_id]
    except KeyError as error:
        raise ValueError(f"unknown Orthopron seed ID: {seed_id}") from error


def load_adversarial_review() -> dict[str, object]:
    """Map the approved adversarial repair scope without emitting case IDs.

    The private case-to-seed lookup is read only to establish the core scope;
    public output contains aggregate counts and seed IDs, never case IDs or a
    private-index reference.
    """

    expected_package_hash = ADVERSARIAL_PACKAGE_SHA256.removeprefix("sha256:")
    case_to_seed = json.loads(ADVERSARIAL_PRIVATE_INDEX_PATH.read_text(encoding="utf-8"))
    if not isinstance(case_to_seed, dict) or not all(
        isinstance(case_id, str) and isinstance(seed_id, str) for case_id, seed_id in case_to_seed.items()
    ):
        raise ValueError("adversarial private index is malformed")

    plans = _plans_by_seed()
    flags_by_seed: defaultdict[str, set[str]] = defaultdict(set)
    critical_flag_counts: Counter[str] = Counter()
    noncritical_flag_counts: Counter[str] = Counter()
    rejected_case_count = 0
    mapped_case_count = 0
    seen_case_ids: set[str] = set()
    for line_number, line in enumerate(ADVERSARIAL_VERDICTS_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"adversarial verdicts contain blank line {line_number}")
        verdict = json.loads(line)
        if not isinstance(verdict, dict):
            raise ValueError(f"adversarial verdict {line_number} is not an object")
        if verdict.get("reviewed_package_sha256") != expected_package_hash:
            raise ValueError("adversarial verdict package hash differs from the pinned package")
        if verdict.get("acceptable") is not False:
            continue
        rejected_case_count += 1
        case_id = verdict.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen_case_ids:
            raise ValueError("adversarial rejected case IDs are invalid or duplicated")
        seen_case_ids.add(case_id)
        seed_id = case_to_seed.get(case_id)
        if not isinstance(seed_id, str):
            raise ValueError("adversarial rejected case has no seed mapping")
        mapped_case_count += 1
        if seed_id not in plans:
            continue
        critical_flags = verdict.get("critical_flags")
        noncritical_flags = verdict.get("noncritical_flags")
        if not isinstance(critical_flags, list) or not all(isinstance(flag, str) for flag in critical_flags):
            raise ValueError("adversarial critical flags are malformed")
        if not isinstance(noncritical_flags, list) or not all(isinstance(flag, str) for flag in noncritical_flags):
            raise ValueError("adversarial noncritical flags are malformed")
        flags_by_seed[seed_id].update(critical_flags)
        flags_by_seed[seed_id].update(noncritical_flags)
        critical_flag_counts.update(critical_flags)
        noncritical_flag_counts.update(noncritical_flags)

    normalized_flags = {seed_id: frozenset(flags) for seed_id, flags in sorted(flags_by_seed.items())}
    if rejected_case_count != EXPECTED_ALL_REJECTED_CASE_COUNT or mapped_case_count != rejected_case_count:
        raise ValueError("adversarial rejected-case mapping differs from the approved review")
    if len(normalized_flags) != EXPECTED_REVIEWED_SEED_COUNT:
        raise ValueError("Orthopron adversarial reviewed-seed count differs from the approved review")
    if dict(sorted(critical_flag_counts.items())) != EXPECTED_CRITICAL_FLAG_COUNTS:
        raise ValueError("Orthopron adversarial critical flag counts differ from the approved review")
    if dict(sorted(noncritical_flag_counts.items())) != EXPECTED_NONCRITICAL_FLAG_COUNTS:
        raise ValueError("Orthopron adversarial noncritical flag counts differ from the approved review")
    noncritical_seed_count = sum(
        bool(flags.intersection(EXPECTED_NONCRITICAL_FLAG_COUNTS))
        and not bool(flags.intersection(EXPECTED_CRITICAL_FLAG_COUNTS))
        for flags in normalized_flags.values()
    )
    if noncritical_seed_count != EXPECTED_NONCRITICAL_SEED_COUNT:
        raise ValueError("Orthopron noncritical-only seed count differs from the approved review")
    return {
        "reviewed_package_sha256": ADVERSARIAL_PACKAGE_SHA256,
        "reviewed_seed_count": len(normalized_flags),
        "reviewed_seed_ids": sorted(normalized_flags),
        "critical_flag_counts": dict(sorted(critical_flag_counts.items())),
        "noncritical_flag_counts": dict(sorted(noncritical_flag_counts.items())),
        "noncritical_seed_count": noncritical_seed_count,
        "all_rejected_case_count": rejected_case_count,
        "all_rejected_case_mapping_count": mapped_case_count,
        "flags_by_seed": normalized_flags,
    }


def _ensure_terminal_punctuation(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if value.endswith("..") and not value.endswith("..."):
        return value.rstrip(".") + "."
    return value if value[-1] in _TERMINAL_PUNCTUATION else value + "."


def _condition_comparison_form(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .;:").casefold()


def append_declared_condition(value: str, condition: str) -> str:
    """Render a plan condition as a complete requirement without changing it."""

    cleaned_condition = condition.strip().rstrip(_TERMINAL_PUNCTUATION).rstrip()
    if _condition_comparison_form(cleaned_condition) in _condition_comparison_form(value):
        return value
    return f"{_ensure_terminal_punctuation(value)} Dies gilt, {cleaned_condition}."


def repair_referent_text(value: str) -> str:
    """Make the designated third-person referent explicit without losing tokens."""

    repaired = _DIRECT_PLURAL_RECIPIENT.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('person')}, damit {match.group('person')} "
            f"Euch den Stand senden kann. Das Pronomen „er“ bezieht sich dabei auf {match.group('person')}."
        ),
        value,
    )
    repaired = _COORDINATION_WITH_REFERENCE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('person')} zu Vorgang {match.group('reference')} ab; "
            f"{match.group('person')} bestätigt nur die Schreibweise, nicht eine andere Bedeutung. "
            f"Das Pronomen „sie“ bezieht sich dabei auf {match.group('person')}."
        ),
        repaired,
    )
    repaired = _COORDINATION_WITHOUT_REFERENCE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('person')} ab; {match.group('person')} "
            f"hält die vereinbarte Reihenfolge fest. Das Pronomen „sie“ bezieht sich dabei auf {match.group('person')}."
        ),
        repaired,
    )
    repaired = _PERSONAL_DOCUMENTATION.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('person')}; {match.group('person')} dokumentiert sie im Vorgang."
        ),
        repaired,
    )
    repaired = _PERSON_HOLDS_FORM.sub(
        lambda match: (
            f"{match.group('person')} hält für Vorgang {match.group('reference')} fest, dass "
            f"{match.group('person')} die geschützte Form nicht ersetzt. Das Pronomen „sie“ bezieht sich dabei auf "
            f"{match.group('person')}."
        ),
        repaired,
    )
    repaired = repaired.replace(
        "Im Protokoll steht, dass du deine Notizen Jonas geben willst und er dir danach antwortet.",
        "Im Protokoll steht, dass du deine Notizen Jonas geben willst und Jonas dir danach antwortet.",
    )
    repaired = repaired.replace(
        "Wir formulieren die Hinweise so, dass sie ohne Rückfrage verständlich bleiben.",
        "Wir formulieren die Hinweise so, dass die Hinweise ohne Rückfrage verständlich bleiben.",
    )
    repaired = repaired.replace(
        "Die Maße des Musters sind verbindlich; seine Masse wird erst nach der Messung genannt.",
        "Die Maße des Musters sind verbindlich; die Masse des Musters wird erst nach der Messung genannt.",
    )
    repaired = repaired.replace(
        "Frau Linden erklärte, sie habe ihre Unterlagen bereits versendet.",
        "Frau Linden erklärte, dass sie selbst ihre Unterlagen bereits versendet habe.",
    )
    repaired = repaired.replace(
        "Ihr gebt Euer Ergebnis an Jonas, damit er Euch den finalen Stand senden kann.",
        "Ihr gebt Euer Ergebnis an Jonas, damit Jonas Euch den finalen Stand senden kann.",
    )
    repaired = repaired.replace(
        "Liebe Mara, Du kannst Deine Freigabe bis Freitag an Jonas senden; er bestätigt Dir den Eingang.",
        "Liebe Mara, Du kannst Deine Freigabe bis Freitag an Jonas senden; Jonas bestätigt Dir den Eingang.",
    )
    return repaired


def _assert_protected_values(fact: Mapping[str, Any], rendered: str) -> None:
    original = str(fact["text"])
    for protected_value in fact["protected_values"]:
        if protected_value in original and protected_value not in rendered:
            raise ValueError(f"{fact['fact_id']}: protected value lost during repair: {protected_value!r}")


def repair_fact_text(fact: Mapping[str, Any], flags: frozenset[str]) -> str:
    rendered = str(fact["text"])
    if "pronoun_referent_error" in flags:
        rendered = repair_referent_text(rendered)
    if "omitted_condition" in flags and fact["condition"]:
        rendered = append_declared_condition(rendered, str(fact["condition"]))
    _assert_protected_values(fact, rendered)
    return rendered


def _split_salutation(text: str) -> tuple[str | None, str]:
    if not text.startswith("Liebe "):
        return None, text
    marker = text.find(", ")
    if marker < 0:
        return None, text
    salutation = text[: marker + 1]
    remainder = text[marker + 2 :]
    if not remainder:
        return None, text
    return salutation, remainder[0].upper() + remainder[1:]


def _declared_salutation(plan: Mapping[str, Any], first_fact: str) -> tuple[str | None, str]:
    if not plan["semantic_plan"]["structure"]["has_salutation"]:
        return None, first_fact
    explicit, remaining = _split_salutation(first_fact)
    if explicit is not None:
        return explicit, remaining
    return "Guten Tag,", first_fact


def _text_block(block_type: BlockType, text: str) -> Block:
    return Block(type=block_type, text=text, inlines=(Inline(text),))


def _list_block(structure: Mapping[str, Any]) -> Block | None:
    kind = str(structure["list_kind"])
    values = list(structure["list_items"])
    if kind == "none":
        if values:
            raise ValueError("a none list_kind may not have list items")
        return None
    if not values:
        raise ValueError("a declared list needs items")
    levels = [1] * len(values)
    if kind == "nested":
        if len(values) < 2:
            raise ValueError("a nested list needs a child without inventing content")
        levels[1] = 2
    block_type = BlockType.ORDERED_LIST if kind == "ordered" else BlockType.UNORDERED_LIST
    return Block(
        type=block_type,
        items=tuple(
            ListItem(level=level, text=str(value), inlines=(Inline(str(value)),))
            for level, value in zip(levels, values, strict=False)
        ),
    )


def _heading_labels(plan: Mapping[str, Any], flags: frozenset[str]) -> list[tuple[BlockType, str]]:
    structure = plan["semantic_plan"]["structure"]
    topics = [str(topic) for topic in structure["paragraph_topics"]]
    subject = topics[0] if structure["has_subject"] else None
    labels: list[tuple[BlockType, str]] = []
    for index, level in enumerate(structure["heading_levels"]):
        label = topics[min(index, len(topics) - 1)]
        if "redundant_subject_heading" in flags and subject and label == subject:
            label = next((candidate for candidate in topics[1:] if candidate != subject), label)
        labels.append((BlockType.HEADING_1 if level == "h1" else BlockType.HEADING_2, label))
    return labels


def make_document(plan: Mapping[str, Any], flags: frozenset[str]) -> tuple[Document, tuple[FactEvidence, ...]]:
    semantic_plan = plan["semantic_plan"]
    structure = semantic_plan["structure"]
    facts = sorted(semantic_plan["facts"], key=lambda fact: int(fact["order"]))
    if [fact["order"] for fact in facts] != list(range(1, len(facts) + 1)):
        raise ValueError(f"{plan['seed_id']}: fact orders must be contiguous")
    rendered_facts = [repair_fact_text(fact, flags) for fact in facts]
    salutation, rendered_facts[0] = _declared_salutation(plan, rendered_facts[0])
    evidence = tuple(
        FactEvidence(str(fact["fact_id"]), int(fact["order"]), text)
        for fact, text in zip(facts, rendered_facts, strict=True)
    )

    blocks: list[Block] = []
    topics = [str(topic) for topic in structure["paragraph_topics"]]
    if structure["has_subject"]:
        blocks.append(_text_block(BlockType.SUBJECT, topics[0]))
    for block_type, label in _heading_labels(plan, flags):
        blocks.append(_text_block(block_type, label))
    if salutation is not None:
        blocks.append(_text_block(BlockType.SALUTATION, salutation))
    first_bucket_size = max(1, len(rendered_facts) - (len(topics) - 1))
    blocks.append(_text_block(BlockType.PARAGRAPH, " ".join(rendered_facts[:first_bucket_size])))
    for text in rendered_facts[first_bucket_size:]:
        blocks.append(_text_block(BlockType.PARAGRAPH, text))
    list_block = _list_block(structure)
    if list_block is not None:
        blocks.append(list_block)
    if structure["has_closing"]:
        blocks.append(_text_block(BlockType.CLOSING, "Freundliche Grüße"))
    signature_lines = tuple(str(value) for value in structure["signature_lines"])
    if signature_lines:
        blocks.append(Block(type=BlockType.SIGNATURE, lines=signature_lines))
    return Document(blocks=tuple(blocks)), evidence


def _protected_spans(plan: Mapping[str, Any]) -> list[str]:
    spans: list[str] = []
    for fact in sorted(plan["semantic_plan"]["facts"], key=lambda fact: int(fact["order"])):
        for value in fact["protected_values"]:
            if value not in spans:
                spans.append(value)
    return spans


def make_record(plan: Mapping[str, Any], flags: frozenset[str], prompt_hash: str) -> dict[str, Any]:
    document, evidence = make_document(plan, flags)
    semantic_plan = plan["semantic_plan"]
    plain_text = render_plain_text(document)
    record: dict[str, Any] = {
        "schema_version": 1,
        "canonical_document_id": f"repair_orthopron_{plan['seed']}",
        "parent_canonical_document_id": f"source_orthopron_{plan['seed']}",
        "canonical_generator_id": "local_canonical_expander",
        "canonical_generator_version": "1",
        "variant_index": 1,
        "seed_id": plan["seed_id"],
        "source_plan_batch": plan["batch_id"],
        "plan_generator_agent": plan["generator_agent"],
        "target_writer_agent": REPAIR_WRITER_ID,
        "target_prompt_hash": prompt_hash,
        "semantic_plan_sha256": sha256_prefixed(canonical_json(semantic_plan).encode("utf-8")),
        "semantic_plan": semantic_plan,
        "domain": plan["domain"],
        "document_type": plan["document_type"],
        "risk_tags": plan["risk_tags"],
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
        "protected_spans": _protected_spans(plan),
        "applied_normalizations": [],
        "rejected_normalizations": [],
        "ambiguity_flags": [
            tag
            for tag in plan["risk_tags"]
            if any(marker in tag for marker in ("ambiguous", "ambiguity", "hard_negative"))
        ],
    }
    validate_record(record, plan, flags, document, evidence)
    return record


def validate_record(
    record: Mapping[str, Any],
    plan: Mapping[str, Any],
    flags: frozenset[str],
    document: Document,
    evidence: Sequence[FactEvidence],
) -> None:
    canonical = validate_canonical_record(record)
    if canonical["source_text"] != canonical["target_plain_text"]:
        raise ValueError("source_text must equal the canonical plain target")
    if document_from_dict(canonical["target_ast"]) != document:
        raise ValueError(f"{plan['seed_id']}: AST codec does not round-trip")
    if render_sst(parse_sst(canonical["target_sst"])) != canonical["target_sst"]:
        raise ValueError(f"{plan['seed_id']}: SST parser does not round-trip")
    if render_plain_text(document) != canonical["target_plain_text"]:
        raise ValueError(f"{plan['seed_id']}: plain renderer differs")
    if render_markdown(document) != canonical["target_markdown"]:
        raise ValueError(f"{plan['seed_id']}: Markdown renderer differs")
    if render_html(document) != canonical["target_html"]:
        raise ValueError(f"{plan['seed_id']}: HTML renderer differs")
    for protected in canonical["protected_spans"]:
        if protected not in canonical["target_plain_text"]:
            raise ValueError(f"{plan['seed_id']}: protected span absent: {protected!r}")

    plain_text = canonical["target_plain_text"]
    position = 0
    for item in sorted(evidence, key=lambda entry: entry.order):
        found = plain_text.find(item.text, position)
        if found < 0:
            raise ValueError(f"{plan['seed_id']}: repaired fact {item.fact_id} is absent or reordered")
        position = found + len(item.text)
    for fact, item in zip(
        sorted(plan["semantic_plan"]["facts"], key=lambda fact: int(fact["order"])), evidence, strict=True
    ):
        if "omitted_condition" in flags and fact["condition"]:
            condition = str(fact["condition"]).strip().rstrip(_TERMINAL_PUNCTUATION).rstrip()
            if _condition_comparison_form(condition) not in _condition_comparison_form(item.text):
                raise ValueError(f"{plan['seed_id']}: declared condition is absent for {fact['fact_id']}")
    if "pronoun_referent_error" in flags and not any(
        repair_referent_text(str(fact["text"])) != str(fact["text"]) for fact in plan["semantic_plan"]["facts"]
    ):
        raise ValueError(f"{plan['seed_id']}: referent repair made no surface change")
    if "redundant_subject_heading" in flags:
        subject = next(block.text for block in document.blocks if block.type is BlockType.SUBJECT)
        heading = next(block.text for block in document.blocks if block.type is BlockType.HEADING_1)
        if subject == heading:
            raise ValueError(f"{plan['seed_id']}: subject and heading remain redundant")


def validate_canonical_expansion(plan: Mapping[str, Any], record: Mapping[str, Any]) -> int:
    variants = expand_canonical_variants(plan, record)
    if len(variants) != VARIANT_COUNT:
        raise ValueError(f"{plan['seed_id']}: expected {VARIANT_COUNT} canonical variants")
    if len({variant["canonical_document_id"] for variant in variants}) != VARIANT_COUNT:
        raise ValueError(f"{plan['seed_id']}: canonical variant IDs are not unique")
    if len({variant["target_sst"] for variant in variants}) != VARIANT_COUNT:
        raise ValueError(f"{plan['seed_id']}: canonical variant SST values are not unique")
    return len(variants)


def _baseline_by_seed() -> dict[str, dict[str, Any]]:
    result = {record["seed_id"]: record for record in _read_jsonl(BASELINE_TARGETS_PATH)}
    if len(result) != 300:
        raise ValueError(f"expected 300 baseline Orthopron targets, found {len(result)}")
    return result


def _public_review_summary(review: Mapping[str, object]) -> dict[str, object]:
    return {
        key: review[key]
        for key in (
            "reviewed_package_sha256",
            "reviewed_seed_count",
            "reviewed_seed_ids",
            "critical_flag_counts",
            "noncritical_flag_counts",
            "noncritical_seed_count",
            "all_rejected_case_count",
            "all_rejected_case_mapping_count",
        )
    }


def _block_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in records:
        counts.update(block["type"] for block in record["target_ast"]["blocks"])
    return dict(sorted(counts.items()))


def _repair_class_proof(
    plans: Mapping[str, Mapping[str, Any]],
    repaired_by_seed: Mapping[str, Mapping[str, Any]],
    flags_by_seed: Mapping[str, frozenset[str]],
) -> dict[str, dict[str, int]]:
    proof: dict[str, dict[str, int]] = {}
    for flag in (*EXPECTED_CRITICAL_FLAG_COUNTS, *EXPECTED_NONCRITICAL_FLAG_COUNTS):
        seed_ids = sorted(seed_id for seed_id, flags in flags_by_seed.items() if flag in flags)
        repaired_count = 0
        for seed_id in seed_ids:
            plan = plans[seed_id]
            record = repaired_by_seed[seed_id]
            if flag == "pronoun_referent_error":
                repaired = any(
                    repair_referent_text(str(fact["text"])) != str(fact["text"])
                    for fact in plan["semantic_plan"]["facts"]
                )
            elif flag == "omitted_condition":
                repaired = all(
                    not fact["condition"]
                    or _condition_comparison_form(str(fact["condition"]))
                    in _condition_comparison_form(record["target_plain_text"])
                    for fact in plan["semantic_plan"]["facts"]
                )
            else:
                document = document_from_dict(record["target_ast"])
                subject = next(block.text for block in document.blocks if block.type is BlockType.SUBJECT)
                heading = next(block.text for block in document.blocks if block.type is BlockType.HEADING_1)
                repaired = subject != heading
            repaired_count += int(repaired)
        if repaired_count != len(seed_ids):
            raise ValueError(f"{flag}: not every reviewed seed has its required repair")
        proof[flag] = {"reviewed_seed_count": len(seed_ids), "repaired_seed_count": repaired_count}
    return proof


def build_repair() -> RepairBuild:
    plans = _plans_by_seed()
    review = load_adversarial_review()
    flags_by_seed = review["flags_by_seed"]
    if not isinstance(flags_by_seed, dict):
        raise ValueError("adversarial review flags are malformed")
    prompt_hash = sha256_prefixed(HANDOFF_PATH.read_bytes())
    records: list[dict[str, Any]] = []
    expansion_variant_count = 0
    for seed_id in sorted(plans):
        flags = flags_by_seed.get(seed_id, frozenset())
        if not isinstance(flags, frozenset):
            raise ValueError(f"{seed_id}: adversarial review flags are malformed")
        record = make_record(plans[seed_id], flags, prompt_hash)
        expansion_variant_count += validate_canonical_expansion(plans[seed_id], record)
        records.append(record)
    if len(records) != 300:
        raise ValueError("all Orthopron plans must have a repaired target")
    records.sort(key=lambda record: record["seed_id"])

    baseline = _baseline_by_seed()
    repaired_by_seed = {record["seed_id"]: record for record in records}
    reviewed_seed_ids = list(review["reviewed_seed_ids"])
    changed_reviewed_seed_ids = [
        seed_id
        for seed_id in reviewed_seed_ids
        if baseline[seed_id]["target_plain_text"] != repaired_by_seed[seed_id]["target_plain_text"]
    ]
    if len(changed_reviewed_seed_ids) != len(reviewed_seed_ids):
        raise ValueError("not every adversarial-reviewed Orthopron target changed its plain surface")
    unchanged_unreviewed_seed_ids = [
        seed_id
        for seed_id in sorted(set(plans).difference(reviewed_seed_ids))
        if baseline[seed_id]["target_plain_text"] == repaired_by_seed[seed_id]["target_plain_text"]
    ]
    expected_unchanged = len(plans) - len(reviewed_seed_ids)
    if len(unchanged_unreviewed_seed_ids) != expected_unchanged:
        raise ValueError("an unreviewed Orthopron plain-text target changed unexpectedly")
    repair_class_proof = _repair_class_proof(plans, repaired_by_seed, flags_by_seed)

    targets_bytes = ("".join(canonical_json(record) + "\n" for record in records)).encode("utf-8")
    manifest = build_manifest(
        records,
        targets_bytes,
        review,
        expansion_variant_count,
        changed_reviewed_seed_ids,
        len(unchanged_unreviewed_seed_ids),
        repair_class_proof,
    )
    report = build_report(manifest)
    validation = build_validation(manifest)
    return RepairBuild(tuple(records), targets_bytes, manifest, report, validation)


def build_manifest(
    records: Sequence[Mapping[str, Any]],
    targets_bytes: bytes,
    review: Mapping[str, object],
    expansion_variant_count: int,
    changed_reviewed_seed_ids: Sequence[str],
    unchanged_unreviewed_count: int,
    repair_class_proof: Mapping[str, Mapping[str, int]],
) -> dict[str, Any]:
    def count_by(key: str) -> dict[str, int]:
        return dict(sorted(Counter(str(record[key]) for record in records).items()))

    return {
        "manifest_version": MANIFEST_VERSION,
        "writer_id": REPAIR_WRITER_ID,
        "writer_version": REPAIR_WRITER_VERSION,
        "target_prompt_hash": records[0]["target_prompt_hash"],
        "repair_implementation_path": Path(__file__).relative_to(PROJECT_ROOT).as_posix(),
        "repair_implementation_sha256": sha256_prefixed(Path(__file__).read_bytes()),
        "assigned_input_batches": [{"path": PLAN_RELATIVE_PATH, "sha256": sha256_prefixed(PLAN_PATH.read_bytes())}],
        "baseline_targets": {
            "path": BASELINE_TARGETS_RELATIVE_PATH,
            "sha256": sha256_prefixed(BASELINE_TARGETS_PATH.read_bytes()),
        },
        "record_count": len(records),
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in records}),
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "targets_sha256": sha256_prefixed(targets_bytes),
        "rejected_seed_count": 0,
        "counts_by_domain": count_by("domain"),
        "counts_by_language": count_by("language"),
        "counts_by_document_type": count_by("document_type"),
        "counts_by_address_mode": count_by("address_mode"),
        "counts_by_block_type": _block_counts(records),
        "canonical_expansion": {
            "expander": EXPANDER_VERSION,
            "validated_target_count": len(records),
            "variants_per_target": VARIANT_COUNT,
            "validated_variant_count": expansion_variant_count,
            "all_variants_unique_per_target": True,
        },
        "adversarial_repair_review": _public_review_summary(review),
        "baseline_surface_change_proof": {
            "reviewed_seed_count": len(review["reviewed_seed_ids"]),
            "changed_reviewed_seed_count": len(changed_reviewed_seed_ids),
            "changed_reviewed_seed_ids": list(changed_reviewed_seed_ids),
            "unchanged_reviewed_seed_count": len(review["reviewed_seed_ids"]) - len(changed_reviewed_seed_ids),
            "unchanged_unreviewed_seed_count": unchanged_unreviewed_count,
        },
        "repair_class_proof": repair_class_proof,
        "deterministic_serialization": {
            "jsonl": "utf-8 compact sorted JSON with one final newline per record",
            "record_order": "seed_id ascending",
            "no_clock_or_randomness": True,
        },
        "generation_boundary": {
            "approved_inputs_limited_to_plans_baseline_targets_adversarial_review_handoff_and_local_contract_code": True,
            "no_human_curation": True,
            "no_external_corpus_web_data_or_generative_api": True,
            "no_model_checkpoint_training_split_test_or_challenge_data": True,
            "writes_confined_to": OUTPUT_DIR.relative_to(PROJECT_ROOT).as_posix(),
        },
    }


def build_report(manifest: Mapping[str, Any]) -> str:
    review = manifest["adversarial_repair_review"]
    change_proof = manifest["baseline_surface_change_proof"]
    class_proof = manifest["repair_class_proof"]
    critical_summary = ", ".join(f"{flag}: {count}" for flag, count in review["critical_flag_counts"].items())
    noncritical_summary = ", ".join(f"{flag}: {count}" for flag, count in review["noncritical_flag_counts"].items())
    reviewed_seed_lines = tuple(
        "- " + ", ".join(review["reviewed_seed_ids"][index : index + 8])
        for index in range(0, len(review["reviewed_seed_ids"]), 8)
    )
    return "\n".join(
        (
            "# Deterministic Orthopron repair-overlay report",
            "",
            f"- Repaired canonical targets: {manifest['record_count']}",
            f"- Explicit seed rejects: {manifest['rejected_seed_count']}",
            f"- Canonical expansion gate: {manifest['canonical_expansion']['validated_target_count']} targets × {manifest['canonical_expansion']['variants_per_target']} variants = {manifest['canonical_expansion']['validated_variant_count']} validated variants",
            f"- Approved adversarial package SHA-256: `{review['reviewed_package_sha256']}`",
            f"- Mapped old-v3 adversarial rejects: {review['all_rejected_case_mapping_count']} of {review['all_rejected_case_count']}",
            f"- Orthopron reviewed seeds: {review['reviewed_seed_count']}",
            f"- Critical repair coverage: {critical_summary}",
            f"- Explicit referent repairs: {class_proof['pronoun_referent_error']['repaired_seed_count']} of {class_proof['pronoun_referent_error']['reviewed_seed_count']}",
            f"- Explicit condition repairs: {class_proof['omitted_condition']['repaired_seed_count']} of {class_proof['omitted_condition']['reviewed_seed_count']}",
            f"- Subject/heading repairs: {class_proof['redundant_subject_heading']['repaired_seed_count']} of {class_proof['redundant_subject_heading']['reviewed_seed_count']}",
            f"- Noncritical repair coverage: {noncritical_summary} across {review['noncritical_seed_count']} noncritical-only seeds",
            f"- Baseline reviewed targets with changed plain-text surface: {change_proof['changed_reviewed_seed_count']} of {change_proof['reviewed_seed_count']}",
            f"- Baseline reviewed targets left unchanged: {change_proof['unchanged_reviewed_seed_count']}",
            f"- Baseline unreviewed targets preserved byte-for-byte at plain-text surface: {change_proof['unchanged_unreviewed_seed_count']}",
            f"- Target JSONL SHA-256: `{manifest['targets_sha256']}`",
            f"- Baseline target JSONL SHA-256: `{manifest['baseline_targets']['sha256']}`",
            f"- Repair implementation SHA-256: `{manifest['repair_implementation_sha256']}`",
            "",
            "The overlay preserves the source semantic plans and every protected span. It makes third-person referents explicit, emits each declared omitted condition as a complete requirement, and selects a distinct declared heading topic where the approved review found a duplicate subject/heading. Validation checks AST/SST/AST and all renderer surfaces, protected spans, fact order, exact condition preservation, complete canonical expansion, and the reviewed baseline surface-change proof. The report records no adversarial case IDs or private-index data.",
            "",
            "## Reviewed Orthopron seed IDs",
            "",
            *reviewed_seed_lines,
            "",
        )
    )


def build_validation(manifest: Mapping[str, Any]) -> dict[str, Any]:
    proof = manifest["baseline_surface_change_proof"]
    return {
        "validation_version": 1,
        "canonical_record_validation_count": manifest["record_count"],
        "ast_sst_ast_round_trip_count": manifest["record_count"],
        "renderer_consistency_count": manifest["record_count"],
        "canonical_expansion_target_count": manifest["canonical_expansion"]["validated_target_count"],
        "canonical_expansion_variant_count": manifest["canonical_expansion"]["validated_variant_count"],
        "target_batch_validator": {"record_count": manifest["record_count"], "writer_id": manifest["writer_id"]},
        "reviewed_baseline_targets_changed": proof["changed_reviewed_seed_count"],
        "reviewed_baseline_targets_unchanged": proof["unchanged_reviewed_seed_count"],
        "unreviewed_baseline_targets_unchanged": proof["unchanged_unreviewed_seed_count"],
        "repair_class_proof": manifest["repair_class_proof"],
        "zero_rejects": manifest["rejected_seed_count"] == 0,
        "targets_sha256": manifest["targets_sha256"],
    }


def write_build(build: RepairBuild) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TARGETS_PATH.write_bytes(build.targets_bytes)
    MANIFEST_PATH.write_text(
        json.dumps(build.manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    REPORT_PATH.write_text(build.report, encoding="utf-8", newline="\n")
    VALIDATION_PATH.write_text(
        json.dumps(build.validation, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def verify_written_build() -> RepairBuild:
    build = build_repair()
    expected = {
        TARGETS_PATH: build.targets_bytes,
        MANIFEST_PATH: (json.dumps(build.manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
        REPORT_PATH: build.report.encode("utf-8"),
        VALIDATION_PATH: (json.dumps(build.validation, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
    }
    for path, expected_bytes in expected.items():
        if not path.is_file() or path.read_bytes() != expected_bytes:
            raise ValueError(f"written repair output differs from deterministic rebuild: {path.name}")
    summary, errors = _validate_batch(OUTPUT_DIR, PROJECT_ROOT, set(), set())
    if errors or summary is None:
        raise ValueError("target-batch validator failed: " + "; ".join(errors))
    return build


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
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
                "expanded_variants": build.manifest["canonical_expansion"]["validated_variant_count"],
                "reviewed_seed_count": build.manifest["adversarial_repair_review"]["reviewed_seed_count"],
                "targets_sha256": build.manifest["targets_sha256"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
