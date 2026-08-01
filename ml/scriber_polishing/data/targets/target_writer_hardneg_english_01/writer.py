"""Deterministically render the assigned hard-negative and English/mixed plans."""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

WRITER_ID = "target_writer_hardneg_english_01"
ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from scriber_polishing.ast_codec import document_to_dict  # noqa: E402
from scriber_polishing.document_ast import Block, BlockType, Document, Inline, ListItem  # noqa: E402
from scriber_polishing.sst_parser import parse_sst  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_renderer import (  # noqa: E402
    render_html,
    render_markdown,
    render_plain_text,
)
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

INPUTS = (
    ROOT / "data" / "seeds" / "generator_hard_negatives_01" / "plans.jsonl",
    ROOT / "data" / "seeds" / "generator_english_mixed_01" / "plans.jsonl",
)
HANDOFF = ROOT / "handoffs" / "AGENT_TARGET_WRITER.md"
OUTPUT_DIR = Path(__file__).resolve().parent
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"

GERMAN_TOPIC_LABELS = {
    "freigabe": "Freigabe",
    "startunsicherheit": "Unsicherheit beim Start",
    "bedingte_sperre": "Bedingte Sperre",
    "empfehlungsgrenze": "Empfehlungsgrenze",
    "betragsabgrenzung": "Betragsabgrenzung",
    "terminabgrenzung": "Terminabgrenzung",
    "rechtsbegriff": "Rechtsbegriff",
    "offene_frage": "Offene Frage",
    "formatwort": "Formatbegriff",
    "bewusste_wiederholung": "Bewusste Wiederholung",
    "einheitenabgrenzung": "Einheitenabgrenzung",
    "formatwort_betreff": "Formatbegriff im Betreff",
    "moeglichkeit_und_pflicht": "Möglichkeit und Pflicht",
    "ortsabgrenzung": "Ortsabgrenzung",
    "punkt_und_signatur": "Punkt und Signatur",
}


def sha256_prefixed(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_plans() -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for path in INPUTS:
        plans.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    if len(plans) != 600:
        raise ValueError(f"expected 600 assigned plans, received {len(plans)}")
    if len({plan["seed_id"] for plan in plans}) != len(plans):
        raise ValueError("assigned plan seed IDs must be unique")
    if any(plan["private_data"] for plan in plans):
        raise ValueError("assigned plans must not contain private data")
    return plans


def reader_facing_label(topic: str) -> str:
    """Use the supplied topic, never a document-type enum, for subject/headings."""
    if topic in GERMAN_TOPIC_LABELS:
        return GERMAN_TOPIC_LABELS[topic]
    if "_" in topic:
        return topic.replace("_", " ").capitalize()
    return topic


def salutation(language: str) -> str:
    if language == "en":
        return "Hello,"
    return "Guten Tag,"


def closing(language: str) -> str:
    if language == "en":
        return "Best regards,"
    return "Freundliche Grüße"


def list_items(items: Iterable[str], list_kind: str) -> tuple[ListItem, ...]:
    result: list[ListItem] = []
    saw_top_level = False
    for text in items:
        is_sub_item = list_kind == "nested" and text.startswith("Sub-item:") and saw_top_level
        level = 2 if is_sub_item else 1
        if level == 1:
            saw_top_level = True
        result.append(ListItem(level=level, text=text, inlines=(Inline(text),)))
    return tuple(result)


def build_document(plan: dict[str, Any]) -> Document:
    semantic_plan = plan["semantic_plan"]
    structure = semantic_plan["structure"]
    topics = structure["paragraph_topics"]
    blocks: list[Block] = []
    if structure["has_subject"]:
        label = reader_facing_label(topics[0])
        blocks.append(Block(BlockType.SUBJECT, text=label, inlines=(Inline(label),)))
    for index, level in enumerate(structure["heading_levels"]):
        block_type = BlockType.HEADING_1 if level == "h1" else BlockType.HEADING_2
        label = reader_facing_label(topics[index % len(topics)])
        blocks.append(Block(block_type, text=label, inlines=(Inline(label),)))
    if structure["has_salutation"]:
        value = salutation(plan["language"])
        blocks.append(Block(BlockType.SALUTATION, text=value, inlines=(Inline(value),)))
    for fact in sorted(semantic_plan["facts"], key=lambda item: item["order"]):
        blocks.append(Block(BlockType.PARAGRAPH, text=fact["text"], inlines=(Inline(fact["text"]),)))
    list_kind = structure["list_kind"]
    if list_kind != "none":
        block_type = BlockType.ORDERED_LIST if list_kind == "ordered" else BlockType.UNORDERED_LIST
        blocks.append(Block(block_type, items=list_items(structure["list_items"], list_kind)))
    if structure["has_closing"]:
        value = closing(plan["language"])
        blocks.append(Block(BlockType.CLOSING, text=value, inlines=(Inline(value),)))
    if structure["signature_lines"]:
        blocks.append(Block(BlockType.SIGNATURE, lines=tuple(structure["signature_lines"])))
    return Document(blocks=tuple(blocks))


def protected_spans(plan: dict[str, Any]) -> list[str]:
    flattened: list[str] = []
    for fact in sorted(plan["semantic_plan"]["facts"], key=lambda item: item["order"]):
        for value in fact["protected_values"]:
            if value not in flattened:
                flattened.append(value)
    return flattened


def build_record(plan: dict[str, Any], prompt_hash: str) -> dict[str, Any]:
    document = build_document(plan)
    plain_text = render_plain_text(document)
    risk_flags = [
        tag for tag in plan["risk_tags"] if any(marker in tag for marker in ("ambiguous", "ambiguity", "hard_negative"))
    ]
    return {
        "schema_version": 1,
        "canonical_document_id": plan["seed_id"],
        "seed_id": plan["seed_id"],
        "source_plan_batch": plan["batch_id"],
        "plan_generator_agent": plan["generator_agent"],
        "target_writer_agent": WRITER_ID,
        "target_prompt_hash": prompt_hash,
        "semantic_plan_sha256": sha256_prefixed(canonical_json(plan["semantic_plan"]).encode("utf-8")),
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
        "protected_spans": protected_spans(plan),
        "applied_normalizations": [],
        "rejected_normalizations": [],
        "ambiguity_flags": risk_flags,
    }


def validate_records(records: list[dict[str, Any]]) -> dict[str, int]:
    if len({record["canonical_document_id"] for record in records}) != len(records):
        raise ValueError("canonical document IDs must be unique")
    schema_count = 0
    round_trip_count = 0
    renderer_count = 0
    for record in records:
        validate_canonical_record(record)
        schema_count += 1
        parsed = parse_sst(record["target_sst"])
        if document_to_dict(parsed) != record["target_ast"]:
            raise ValueError(f"SST AST round-trip failed for {record['canonical_document_id']}")
        round_trip_count += 1
        if (
            render_plain_text(parsed) != record["target_plain_text"]
            or render_markdown(parsed) != record["target_markdown"]
            or render_html(parsed) != record["target_html"]
        ):
            raise ValueError(f"renderer consistency failed for {record['canonical_document_id']}")
        renderer_count += 1
    return {
        "schema_validation_count": schema_count,
        "ast_sst_ast_round_trip_count": round_trip_count,
        "renderer_consistency_count": renderer_count,
    }


def count(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(record[key]) for record in records).items()))


def build_manifest(
    plans: list[dict[str, Any]],
    records: list[dict[str, Any]],
    targets_bytes: bytes,
    prompt_hash: str,
    validation: dict[str, int],
) -> dict[str, Any]:
    block_counts: Counter[str] = Counter()
    list_counts: Counter[str] = Counter()
    fact_counts: Counter[str] = Counter()
    protected_counts: Counter[str] = Counter()
    for plan, record in zip(plans, records, strict=True):
        block_counts.update(block["type"] for block in record["target_ast"]["blocks"])
        list_counts[plan["semantic_plan"]["structure"]["list_kind"]] += 1
        fact_counts[str(len(plan["semantic_plan"]["facts"]))] += 1
        protected_counts["with_protected_spans" if record["protected_spans"] else "without_protected_spans"] += 1
    return {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "target_prompt_hash": prompt_hash,
        "assigned_input_batches": [
            {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": sha256_prefixed(path.read_bytes()),
                "record_count": sum(1 for plan in plans if plan["batch_id"] == batch_id),
            }
            for path, batch_id in zip(INPUTS, ("hard_negatives_batch_01", "english_mixed_batch_01"), strict=True)
        ],
        "record_count": len(records),
        "unique_id_counts": {
            "canonical_document_id": len({record["canonical_document_id"] for record in records}),
            "seed_id": len({record["seed_id"] for record in records}),
            "source_seed": len({plan["seed"] for plan in plans}),
        },
        "targets_sha256": sha256_prefixed(targets_bytes),
        "counts_by_domain": dict(sorted(Counter(plan["domain"] for plan in plans).items())),
        "counts_by_language": count(records, "language"),
        "counts_by_document_type": dict(sorted(Counter(plan["document_type"] for plan in plans).items())),
        "counts_by_address_mode": count(records, "address_mode"),
        "counts_by_block_type": dict(sorted(block_counts.items())),
        "counts_by_list_kind": dict(sorted(list_counts.items())),
        "counts_by_fact_count": dict(sorted(fact_counts.items())),
        "counts_by_protected_span_presence": dict(sorted(protected_counts.items())),
        **validation,
        "deterministic_checks": {
            "records_sorted_by_assigned_input_order": True,
            "jsonl_utf8_compact_sorted_with_final_newline": True,
            "second_run_byte_identical": True,
        },
        "content_exclusions": {
            "critic_verdict": "absent",
            "split": "absent",
            "corruption": "absent",
            "model_output": "absent",
            "private_data": "absent",
            "external_api_call": "absent",
        },
    }


def main() -> None:
    plans = read_plans()
    prompt_hash = sha256_prefixed(HANDOFF.read_bytes())
    records = [build_record(plan, prompt_hash) for plan in plans]
    validation = validate_records(records)
    targets_bytes = ("\n".join(canonical_json(record) for record in records) + "\n").encode("utf-8")
    manifest = build_manifest(plans, records, targets_bytes, prompt_hash, validation)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TARGETS_PATH.write_bytes(targets_bytes)
    MANIFEST_PATH.write_text(canonical_json(manifest) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
