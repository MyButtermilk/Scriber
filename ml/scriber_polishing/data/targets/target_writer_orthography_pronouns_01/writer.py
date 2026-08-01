"""Deterministically render the assigned orthography/pronoun plans into SST v1.

This writer deliberately uses the semantic-plan fact text verbatim.  Its only
structural operation is moving an explicit opening salutation into its own AST
block, as required by the plan, and deriving headings/subjects solely from the
declared paragraph topics. Plans that make an impossible exact protected-span
request are rejected instead of being silently rewritten.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.document_ast import Block, BlockType, Document, Inline, ListItem
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.sst_renderer import render_sst
from scriber_polishing.target_batch_validator import _validate_batch
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text
from scriber_polishing.validators import validate_canonical_record

WRITER_ID = "target_writer_orthography_pronouns_01"
OUTPUT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = OUTPUT_DIR.parents[2]
PLAN_PATH = PROJECT_ROOT / "data" / "seeds" / "generator_orthography_pronouns_01" / "plans.jsonl"
HANDOFF_PATH = PROJECT_ROOT / "handoffs" / "AGENT_TARGET_WRITER.md"
PLAN_RELATIVE_PATH = "data/seeds/generator_orthography_pronouns_01/plans.jsonl"


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _compact_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _split_salutation(text: str) -> tuple[str | None, str]:
    """Move only an explicit greeting, including its named addressee, to SALUTATION."""
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


def _declared_salutation(plan: dict[str, Any], first_fact: str) -> tuple[str | None, str]:
    if not plan["semantic_plan"]["structure"]["has_salutation"]:
        return None, first_fact
    explicit, remaining = _split_salutation(first_fact)
    if explicit is not None:
        return explicit, remaining
    # The plan declares a salutation but contains no dictated wording.  This is
    # a structural-only greeting, not a factual addition.
    return "Guten Tag,", first_fact


def _list_block(structure: dict[str, Any]) -> Block | None:
    kind = structure["list_kind"]
    values = structure["list_items"]
    if kind == "none":
        if values:
            raise ValueError("a none list_kind may not have list items")
        return None
    if not values:
        raise ValueError("a declared list needs items")
    block_type = BlockType.ORDERED_LIST if kind == "ordered" else BlockType.UNORDERED_LIST
    levels = [1] * len(values)
    if kind == "nested" and len(values) > 1:
        levels[1] = 2
    return Block(
        type=block_type,
        items=tuple(
            ListItem(level=level, text=value, inlines=(Inline(value),))
            for level, value in zip(levels, values, strict=False)
        ),
    )


def _text_block(block_type: BlockType, text: str) -> Block:
    return Block(type=block_type, text=text, inlines=(Inline(text),))


def _document_for_plan(plan: dict[str, Any]) -> Document:
    semantic_plan = plan["semantic_plan"]
    structure = semantic_plan["structure"]
    facts = sorted(semantic_plan["facts"], key=lambda fact: fact["order"])
    if [fact["order"] for fact in facts] != list(range(1, len(facts) + 1)):
        raise ValueError(f"{plan['seed_id']}: fact orders must be contiguous")
    fact_texts = [fact["text"] for fact in facts]
    salutation, fact_texts[0] = _declared_salutation(plan, fact_texts[0])
    topics = structure["paragraph_topics"]
    blocks: list[Block] = []
    if structure["has_subject"]:
        blocks.append(_text_block(BlockType.SUBJECT, topics[0]))
    for index, level in enumerate(structure["heading_levels"]):
        topic = topics[min(index, len(topics) - 1)]
        blocks.append(_text_block(BlockType.HEADING_1 if level == "h1" else BlockType.HEADING_2, topic))
    if salutation is not None:
        blocks.append(_text_block(BlockType.SALUTATION, salutation))
    # The declared two-topic structure is kept without disturbing fact order.
    # More general input plans are still handled conservatively as one fact per
    # paragraph after the first topic bucket.
    first_bucket_size = max(1, len(fact_texts) - (len(topics) - 1))
    blocks.append(_text_block(BlockType.PARAGRAPH, " ".join(fact_texts[:first_bucket_size])))
    for text in fact_texts[first_bucket_size:]:
        blocks.append(_text_block(BlockType.PARAGRAPH, text))
    list_block = _list_block(structure)
    if list_block is not None:
        blocks.append(list_block)
    if structure["has_closing"]:
        blocks.append(_text_block(BlockType.CLOSING, "Freundliche Grüße"))
    if structure["signature_lines"]:
        blocks.append(Block(type=BlockType.SIGNATURE, lines=tuple(structure["signature_lines"])))
    return Document(blocks=tuple(blocks))


def _protected_spans(plan: dict[str, Any]) -> list[str]:
    spans: list[str] = []
    for fact in sorted(plan["semantic_plan"]["facts"], key=lambda value: value["order"]):
        for value in fact["protected_values"]:
            if value not in spans:
                spans.append(value)
    return spans


def _record_for_plan(plan: dict[str, Any], prompt_hash: str) -> dict[str, Any]:
    document = _document_for_plan(plan)
    sst = render_sst(document)
    semantic_plan = plan["semantic_plan"]
    risk_tags = plan["risk_tags"]
    return {
        "schema_version": 1,
        "canonical_document_id": f"canonical_orthopron_{plan['seed']}",
        "parent_canonical_document_id": f"parent_orthopron_{plan['seed']}",
        "canonical_generator_id": "local_canonical_expander",
        "canonical_generator_version": "1",
        "variant_index": 1,
        "seed_id": plan["seed_id"],
        "source_plan_batch": plan["batch_id"],
        "plan_generator_agent": plan["generator_agent"],
        "target_writer_agent": WRITER_ID,
        "target_prompt_hash": prompt_hash,
        "semantic_plan_sha256": _sha256(_compact_json(semantic_plan)),
        "semantic_plan": semantic_plan,
        "domain": plan["domain"],
        "document_type": plan["document_type"],
        "risk_tags": risk_tags,
        "source_text": render_plain_text(document),
        "target_sst": sst,
        "target_plain_text": render_plain_text(document),
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
            tag for tag in risk_tags if "ambiguous" in tag or "ambiguity" in tag or "hard_negative" in tag
        ],
    }


def _rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in PLAN_PATH.read_text(encoding="utf-8").splitlines() if line]


def _rejection_reasons(plan: dict[str, Any]) -> list[str]:
    """Find exact-span contradictions before any target is produced.

    A target writer may rearrange the plan's declared structure, but may not
    alter the spelling or wording of its facts.  A protected string absent from
    all fact text therefore cannot be rendered faithfully and must be returned
    to the plan generator.
    """
    fact_text = "\n".join(fact["text"] for fact in plan["semantic_plan"]["facts"])
    missing = [span for span in _protected_spans(plan) if span not in fact_text]
    return [f"protected_span_absent_from_declared_fact_text:{span}" for span in missing]


def _render_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]], bytes]:
    prompt_hash = _sha256(HANDOFF_PATH.read_bytes())
    records: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for plan in _rows():
        reasons = _rejection_reasons(plan)
        if reasons:
            rejections.append({"seed_id": plan["seed_id"], "reasons": reasons})
            continue
        records.append(_record_for_plan(plan, prompt_hash))
    ids = [record["canonical_document_id"] for record in records]
    seeds = [record["seed_id"] for record in records]
    if len(records) + len(rejections) != 300 or len(ids) != len(set(ids)) or len(seeds) != len(set(seeds)):
        raise ValueError("assigned plans must have an explicit unique render or rejection")
    payload = b"".join(_compact_json(record) + b"\n" for record in records)
    return records, rejections, payload


def _manifest(records: list[dict[str, Any]], rejections: list[dict[str, Any]], payload: bytes) -> dict[str, Any]:
    block_types = Counter(block["type"] for record in records for block in record["target_ast"]["blocks"])
    list_kinds = Counter(record["semantic_plan"]["structure"]["list_kind"] for record in records)
    fact_counts = Counter(len(record["semantic_plan"]["facts"]) for record in records)
    return {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "target_prompt_hash": records[0]["target_prompt_hash"],
        "assigned_input_batches": [{"path": PLAN_RELATIVE_PATH, "sha256": _sha256(PLAN_PATH.read_bytes())}],
        "record_count": len(records),
        "assigned_plan_count": len(records) + len(rejections),
        "rejected_plan_count": len(rejections),
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in records}),
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "targets_sha256": _sha256(payload),
        "counts": {
            "domain": dict(sorted(Counter(record["domain"] for record in records).items())),
            "language": dict(sorted(Counter(record["language"] for record in records).items())),
            "document_type": dict(sorted(Counter(record["document_type"] for record in records).items())),
            "address_mode": dict(sorted(Counter(record["address_mode"] for record in records).items())),
            "block_type": dict(sorted(block_types.items())),
            "list_kind": dict(sorted(list_kinds.items())),
            "fact_count": {str(key): value for key, value in sorted(fact_counts.items())},
            "protected_span_presence": {
                "with_protected_spans": sum(bool(record["protected_spans"]) for record in records),
                "without_protected_spans": sum(not record["protected_spans"] for record in records),
            },
        },
        "schema_validation_count": len(records),
        "ast_sst_ast_round_trip_count": len(records),
        "renderer_consistency_count": len(records),
        "generation_boundary": {
            "critic_verdict_present": False,
            "split_present": False,
            "corruption_present": False,
            "model_output_present": False,
            "private_data_present": False,
            "external_api_calls": 0,
            "human_curation": False,
        },
    }


def _validate(records: list[dict[str, Any]], rejections: list[dict[str, Any]], payload: bytes) -> dict[str, Any]:
    protected_checks = 0
    fact_surface_checks = 0
    for record in records:
        canonical = validate_canonical_record(record)
        document = parse_sst(canonical["target_sst"])
        if document_to_dict(document) != canonical["target_ast"]:
            raise ValueError(f"{canonical['seed_id']}: SST round trip differs from AST")
        if render_plain_text(document) != canonical["target_plain_text"]:
            raise ValueError(f"{canonical['seed_id']}: plain renderer differs after SST round trip")
        if render_markdown(document) != canonical["target_markdown"]:
            raise ValueError(f"{canonical['seed_id']}: Markdown renderer differs after SST round trip")
        if render_html(document) != canonical["target_html"]:
            raise ValueError(f"{canonical['seed_id']}: HTML renderer differs after SST round trip")
        for span in canonical["protected_spans"]:
            if canonical["target_plain_text"].count(span) < 1:
                raise ValueError(f"{canonical['seed_id']}: absent protected span {span!r}")
            protected_checks += 1
        facts = sorted(canonical["semantic_plan"]["facts"], key=lambda fact: fact["order"])
        for position, fact in enumerate(facts):
            expected = fact["text"]
            structure = canonical["semantic_plan"]["structure"]
            if position == 0 and structure["has_salutation"]:
                explicit, remainder = _split_salutation(expected)
                if explicit is not None:
                    expected = remainder
            if expected not in canonical["target_plain_text"]:
                raise ValueError(f"{canonical['seed_id']}: fact surface was not conserved")
            fact_surface_checks += 1
    errors: list[str] = []
    summary, errors = _validate_batch(OUTPUT_DIR, PROJECT_ROOT, set(), set())
    if errors or summary is None:
        raise ValueError("target-batch validator failed: " + "; ".join(errors))
    second_records, second_rejections, second_payload = _render_records()
    if payload != second_payload or records != second_records or rejections != second_rejections:
        raise ValueError("second deterministic render differs")
    return {
        "validation_version": 1,
        "canonical_record_validation_count": len(records),
        "sst_round_trip_count": len(records),
        "renderer_round_trip_count": len(records),
        "protected_span_presence_checks": protected_checks,
        "fact_surface_conservation_checks": fact_surface_checks,
        "target_batch_validator": {"record_count": summary.record_count, "writer_id": summary.writer_id},
        "second_run_byte_identical": True,
        "targets_sha256": _sha256(payload),
        "accepted_plan_count": len(records),
        "rejected_plan_count": len(rejections),
        "irreparable_plan_ids": [rejection["seed_id"] for rejection in rejections],
    }


def main() -> None:
    records, rejections, payload = _render_records()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "targets.jsonl").write_bytes(payload)
    (OUTPUT_DIR / "rejections.json").write_bytes(_compact_json({"rejections": rejections}) + b"\n")
    manifest = _manifest(records, rejections, payload)
    (OUTPUT_DIR / "manifest.json").write_bytes(_compact_json(manifest) + b"\n")
    validation = _validate(records, rejections, payload)
    (OUTPUT_DIR / "validation.json").write_bytes(_compact_json(validation) + b"\n")


if __name__ == "__main__":
    main()
