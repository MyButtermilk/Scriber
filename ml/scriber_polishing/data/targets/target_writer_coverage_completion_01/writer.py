"""Deterministically render the coverage-completion semantic plans as canonical targets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.canonical_expander import expand_canonical_variants
from scriber_polishing.document_ast import Block, BlockType, Document, ListItem
from scriber_polishing.sst_renderer import render_sst
from scriber_polishing.target_batch_validator import _validate_batch
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text
from scriber_polishing.validators import validate_canonical_record

BATCH_ID = "target_writer_coverage_completion_01"
PLAN_BATCH_ID = "generator_coverage_completion_01"
WRITER_ID = "target_writer_coverage_completion_01"
TARGET_PROMPT = (
    "Render every planned fact verbatim in its declared order; add only the explicitly "
    "declared document structure and preserve protected values unchanged."
)
TARGET_PROMPT_HASH = "sha256:" + hashlib.sha256(TARGET_PROMPT.encode("utf-8")).hexdigest()
ROOT = Path(__file__).resolve().parents[3]
PLAN_PATH = ROOT / "data" / "seeds" / PLAN_BATCH_ID / "plans.jsonl"
OUTPUT_DIR = Path(__file__).resolve().parent


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _semantic_hash(value: Mapping[str, Any]) -> str:
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _structure_blocks(plan: Mapping[str, Any]) -> list[Block]:
    structure = plan["semantic_plan"]["structure"]
    blocks: list[Block] = []
    if structure["has_subject"]:
        blocks.append(Block(BlockType.SUBJECT, text=plan["document_type"]))
    for level in structure["heading_levels"]:
        if level != "h1":
            raise ValueError(f"unsupported declared heading level: {level}")
        blocks.append(Block(BlockType.HEADING_1, text=plan["document_type"]))
    if structure["has_salutation"]:
        blocks.append(Block(BlockType.SALUTATION, text="Sehr geehrte Damen und Herren,"))
    facts = sorted(plan["semantic_plan"]["facts"], key=lambda fact: fact["order"])
    blocks.extend(Block(BlockType.PARAGRAPH, text=fact["text"]) for fact in facts)
    list_kind = structure["list_kind"]
    if list_kind != "none":
        block_type = BlockType.ORDERED_LIST if list_kind in {"ordered", "nested"} else BlockType.UNORDERED_LIST
        items = tuple(
            ListItem(level=2 if list_kind == "nested" and index == 1 else 1, text=item)
            for index, item in enumerate(structure["list_items"])
        )
        blocks.append(Block(block_type, items=items))
    if structure.get("has_quote", False):
        blocks.append(Block(BlockType.QUOTE, text=structure["quote_text"]))
    if structure["has_closing"]:
        blocks.append(Block(BlockType.CLOSING, text="Mit freundlichen Grüßen"))
    if structure["signature_lines"]:
        blocks.append(Block(BlockType.SIGNATURE, lines=tuple(structure["signature_lines"])))
    if structure.get("has_attachments", False):
        blocks.append(Block(BlockType.ATTACHMENTS, text="\n".join(structure["attachment_lines"])))
    if structure.get("has_post_script", False):
        blocks.append(Block(BlockType.POST_SCRIPT, text=structure["post_script_text"]))
    return blocks


def _protected_spans(plan: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for entity in plan["semantic_plan"]["entities"]:
        values.append(entity["value"])
    for fact in plan["semantic_plan"]["facts"]:
        values.extend(fact["protected_values"])
    return list(dict.fromkeys(values))


def _assert_semantics(plan: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    text = record["target_plain_text"]
    facts = sorted(plan["semantic_plan"]["facts"], key=lambda fact: fact["order"])
    cursor = 0
    for fact in facts:
        if fact["text"] not in text:
            raise ValueError(f"{plan['seed_id']}: fact {fact['fact_id']} is absent")
        position = text.index(fact["text"], cursor)
        if position < cursor:
            raise ValueError(f"{plan['seed_id']}: fact order changed")
        cursor = position + len(fact["text"])
        for value in fact["protected_values"]:
            if value not in fact["text"]:
                raise ValueError(f"{plan['seed_id']}: protected value missing from fact: {value!r}")
    if plan["domain"] == "hard_negatives":
        for fact in facts:
            if fact["text"] not in text:
                raise ValueError(f"{plan['seed_id']}: hard-negative wording changed")


def build_record(plan: Mapping[str, Any]) -> dict[str, Any]:
    document = Document(blocks=tuple(_structure_blocks(plan)))
    semantic_plan = plan["semantic_plan"]
    document_id = f"canonical_{plan['seed_id']}"
    record: dict[str, Any] = {
        "schema_version": 1,
        "canonical_document_id": document_id,
        "parent_canonical_document_id": document_id,
        "canonical_generator_id": "local_canonical_expander",
        "canonical_generator_version": "1",
        "variant_index": 1,
        "seed_id": plan["seed_id"],
        "source_plan_batch": PLAN_BATCH_ID,
        "plan_generator_agent": plan["generator_agent"],
        "target_writer_agent": WRITER_ID,
        "target_prompt_hash": TARGET_PROMPT_HASH,
        "semantic_plan_sha256": _semantic_hash(semantic_plan),
        "semantic_plan": semantic_plan,
        "domain": plan["domain"],
        "document_type": plan["document_type"],
        "risk_tags": plan["risk_tags"],
        "language": plan["language"],
        "style_profile": plan["style_profile"],
        "address_mode": plan["address_mode"],
        "legal_citation_style": plan["legal_citation_style"],
        "unit_style": plan["unit_style"],
        "normalize_valid_variants": plan["domain"] != "hard_negatives",
        "protected_spans": _protected_spans(plan),
        "applied_normalizations": [],
        "rejected_normalizations": ["no_normalization"] if plan["domain"] == "hard_negatives" else [],
        "ambiguity_flags": ["preserve_ambiguous_wording"] if plan["domain"] == "hard_negatives" else [],
        "target_ast": document_to_dict(document),
        "target_sst": render_sst(document),
        "target_plain_text": render_plain_text(document),
        "target_markdown": render_markdown(document),
        "target_html": render_html(document),
    }
    record["source_text"] = record["target_plain_text"]
    validate_canonical_record(record)
    _assert_semantics(plan, record)
    variants = expand_canonical_variants(plan, record)
    if len(variants) != 7:
        raise ValueError(f"{plan['seed_id']}: expected seven canonical variants")
    return record


def generate() -> dict[str, Any]:
    plans = [json.loads(line) for line in PLAN_PATH.read_text(encoding="utf-8").splitlines() if line]
    if len(plans) != 300 or len({plan["seed_id"] for plan in plans}) != 300:
        raise ValueError("expected exactly 300 unique plans")
    records = [build_record(plan) for plan in plans]
    payload = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for record in records)
    (OUTPUT_DIR / "targets.jsonl").write_text(payload, encoding="utf-8", newline="\n")
    # An empty JSONL file is the deterministic rejection record for a fully accepted batch.
    (OUTPUT_DIR / "rejections.jsonl").write_text("", encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": 1,
        "writer_id": WRITER_ID,
        "target_prompt_hash": TARGET_PROMPT_HASH,
        "assigned_input_batches": [{"path": "data/seeds/generator_coverage_completion_01/plans.jsonl", "sha256": _sha256(PLAN_PATH.read_bytes())}],
        "record_count": len(records),
        "unique_canonical_document_id_count": len(records),
        "unique_seed_id_count": len(records),
        "targets_sha256": _sha256((OUTPUT_DIR / "targets.jsonl").read_bytes()),
    }
    (OUTPUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    summary, errors = _validate_batch(OUTPUT_DIR, ROOT, set(), set())
    if errors or summary is None:
        raise ValueError("target-batch validation failed: " + "; ".join(errors))
    report = {
        "schema_version": 1,
        "batch_id": BATCH_ID,
        "accepted_count": len(records),
        "rejected_count": 0,
        "targets_sha256": manifest["targets_sha256"],
        "renderer_equality": True,
        "protected_occurrences": True,
        "semantic_hashes": True,
        "unique_seed_ids": True,
        "canonical_expansion": "7/7 for every target",
        "batch_validation": summary.as_dict(),
    }
    (OUTPUT_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return report


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, sort_keys=True))
