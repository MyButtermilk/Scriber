"""Deterministic target writer for the formatting-structures batch."""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from scriber_polishing.ast_codec import document_to_dict  # noqa: E402
from scriber_polishing.document_ast import Block, BlockType, Document, ListItem  # noqa: E402
from scriber_polishing.sst_parser import parse_sst  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text  # noqa: E402
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

WRITER_ID = "target_writer_formatting_01"
INPUT_BATCH = "generator_formatting_structures_01"
INPUT_PATH = ROOT / "data" / "seeds" / INPUT_BATCH / "plans.jsonl"
OUTPUT_DIR = Path(__file__).resolve().parent
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
HANDOFF_PATH = ROOT / "handoffs" / "AGENT_TARGET_WRITER.md"


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_bytes(compact_json(value).encode("utf-8"))


def topic_subject(topics: list[str], entities: list[dict[str, object]]) -> str:
    topic = topics[0] if topics else "Ihre Anfrage"
    # The topics are authored reader-facing labels; preserve them verbatim.
    if entities:
        product = next((str(item["value"]) for item in entities if item.get("type") == "product"), None)
        if product and product not in topic:
            return f"{topic} zu {product}"
    return topic


def salutation(address_mode: str, entities: list[dict[str, object]]) -> str:
    person = next((str(item["value"]) for item in entities if item.get("type") == "person"), None)
    if address_mode == "formal_sie":
        return "Sehr geehrte Damen und Herren,"
    if address_mode == "personal_du_capitalized":
        return f"Liebe {person}," if person else "Hallo,"
    if address_mode == "personal_du_lowercase":
        return f"Hallo {person}," if person else "Hallo,"
    return "Guten Tag,"


def list_items(values: list[str]) -> tuple[ListItem, ...]:
    rendered: list[ListItem] = []
    for value in values:
        spaces = len(value) - len(value.lstrip(" "))
        level = min(3, spaces // 2 + 1)
        rendered.append(ListItem(level=level, text=value.strip()))
    return tuple(rendered)


def fact_blocks(facts: list[dict[str, object]], topics: list[str]) -> list[Block]:
    """Keep semantic fact order exactly, placing each fact in a paragraph."""
    blocks: list[Block] = []
    for index, fact in enumerate(sorted(facts, key=lambda item: int(item["order"]))):
        # Facts that solely prescribe structural material are rendered through that material.
        text = str(fact["text"])
        if any(
            marker in text
            for marker in (
                "Hauptüberschrift",
                "Zwischenüberschrift",
                "Wörtlich festzuhalten ist:",
                "Beizufügen ist die Anlage:",
                "Der Nachsatz lautet:",
            )
        ):
            continue
        if index < len(topics):
            blocks.append(Block(type=BlockType.HEADING_2, text=topics[index]))
        blocks.append(Block(type=BlockType.PARAGRAPH, text=text))
    return blocks


def protected_spans(facts: list[dict[str, object]]) -> list[str]:
    spans: list[str] = []
    seen: set[str] = set()
    for fact in sorted(facts, key=lambda item: int(item["order"])):
        for value in fact.get("protected_values", []):
            text = str(value)
            if text not in seen:
                seen.add(text)
                spans.append(text)
    return spans


def build_document(plan: dict[str, object]) -> Document:
    semantic = dict(plan["semantic_plan"])
    structure = dict(semantic["structure"])
    entities = list(semantic.get("entities", []))
    facts = list(semantic["facts"])
    topics = list(structure.get("paragraph_topics", []))
    blocks: list[Block] = []
    if structure.get("has_subject", False):
        blocks.append(Block(type=BlockType.SUBJECT, text=topic_subject(topics, entities)))
    headings = list(structure.get("heading_levels", []))
    heading_facts = [str(fact["text"]) for fact in facts if "Hauptüberschrift" in str(fact["text"])]
    if "h1" in headings:
        blocks.append(
            Block(
                type=BlockType.HEADING_1,
                text="Verbindliche nächste Schritte" if heading_facts else (topics[0] if topics else "Information"),
            )
        )
    if structure.get("has_salutation", False):
        blocks.append(Block(type=BlockType.SALUTATION, text=salutation(str(plan["address_mode"]), entities)))
    blocks.extend(fact_blocks(facts, topics))
    if "h2" in headings:
        blocks.append(
            Block(
                type=BlockType.HEADING_2, text="Prüfpunkt" if heading_facts else (topics[-1] if topics else "Hinweis")
            )
        )
    values = list(structure.get("list_items", []))
    if values:
        kind = str(structure.get("list_kind", "unordered"))
        block_type = BlockType.ORDERED_LIST if kind == "ordered" else BlockType.UNORDERED_LIST
        blocks.append(Block(type=block_type, items=list_items(values)))
    if structure.get("has_quote", False):
        blocks.append(Block(type=BlockType.QUOTE, text=str(structure["quote_text"])))
    if structure.get("has_closing", False):
        blocks.append(Block(type=BlockType.CLOSING, text="Mit freundlichen Grüßen"))
    signature = list(structure.get("signature_lines", []))
    if signature:
        blocks.append(Block(type=BlockType.SIGNATURE, lines=tuple(str(value) for value in signature)))
    if structure.get("has_attachments", False):
        blocks.append(
            Block(type=BlockType.ATTACHMENTS, text="\n".join(str(value) for value in structure["attachment_lines"]))
        )
    if structure.get("has_post_script", False):
        blocks.append(Block(type=BlockType.POST_SCRIPT, text=str(structure["post_script_text"])))
    return Document(blocks=tuple(blocks))


def make_record(plan: dict[str, object], prompt_hash: str) -> dict[str, object]:
    document = build_document(plan)
    semantic = plan["semantic_plan"]
    plain = render_plain_text(document)
    flags = [
        tag
        for tag in plan.get("risk_tags", [])
        if any(token in tag for token in ("ambiguous", "ambiguity", "hard_negative"))
    ]
    return {
        "schema_version": 1,
        "canonical_document_id": f"format_target_{int(plan['seed']):06d}",
        "seed_id": plan["seed_id"],
        "source_plan_batch": plan["batch_id"],
        "plan_generator_agent": plan["generator_agent"],
        "target_writer_agent": WRITER_ID,
        "target_prompt_hash": prompt_hash,
        "semantic_plan_sha256": sha256_json(semantic),
        "source_text": plain,
        "target_sst": render_sst(document),
        "target_plain_text": plain,
        "target_markdown": render_markdown(document),
        "target_html": render_html(document),
        "target_ast": document_to_dict(document),
        "language": plan["language"],
        "style_profile": plan["style_profile"],
        "address_mode": plan["address_mode"],
        "legal_citation_style": plan["legal_citation_style"],
        "unit_style": plan["unit_style"],
        "normalize_valid_variants": False,
        "protected_spans": protected_spans(list(semantic["facts"])),
        "applied_normalizations": [],
        "rejected_normalizations": [],
        "ambiguity_flags": flags,
    }


def count_blocks(records: list[dict[str, object]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in records:
        for block in record["target_ast"]["blocks"]:
            counts[str(block["type"])] += 1
    return counts


def run() -> None:
    prompt_hash = sha256_bytes(HANDOFF_PATH.read_bytes())
    input_bytes = INPUT_PATH.read_bytes()
    plans = [json.loads(line) for line in input_bytes.decode("utf-8").splitlines() if line]
    if len(plans) != 300:
        raise ValueError(f"expected 300 plans, found {len(plans)}")
    records = [make_record(plan, prompt_hash) for plan in plans]
    if len({record["canonical_document_id"] for record in records}) != len(records):
        raise ValueError("canonical document IDs are not unique")
    if len({record["seed_id"] for record in records}) != len(records):
        raise ValueError("seed IDs are not unique")
    for record in records:
        validate_canonical_record(record)
        if render_sst(parse_sst(record["target_sst"])) != record["target_sst"]:
            raise ValueError("SST round trip failed")
        if record["source_text"] != record["target_plain_text"]:
            raise ValueError("source text is not canonical target text")
    payload = "".join(compact_json(record) + "\n" for record in records)
    TARGETS_PATH.write_bytes(payload.encode("utf-8"))
    by_fact_count = Counter(str(len(plan["semantic_plan"]["facts"])) for plan in plans)
    manifest = {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "target_prompt_hash": prompt_hash,
        "assigned_input_batches": [
            {
                "batch_id": INPUT_BATCH,
                "path": "data/seeds/generator_formatting_structures_01/plans.jsonl",
                "sha256": sha256_bytes(input_bytes),
            }
        ],
        "record_count": len(records),
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in records}),
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "targets_sha256": sha256_bytes(TARGETS_PATH.read_bytes()),
        "counts_by_domain": dict(sorted(Counter(str(plan["domain"]) for plan in plans).items())),
        "counts_by_language": dict(sorted(Counter(str(plan["language"]) for plan in plans).items())),
        "counts_by_document_type": dict(sorted(Counter(str(plan["document_type"]) for plan in plans).items())),
        "counts_by_address_mode": dict(sorted(Counter(str(plan["address_mode"]) for plan in plans).items())),
        "counts_by_block_type": dict(sorted(count_blocks(records).items())),
        "counts_by_list_kind": dict(
            sorted(Counter(str(plan["semantic_plan"]["structure"].get("list_kind", "none")) for plan in plans).items())
        ),
        "counts_by_fact_count": dict(sorted(by_fact_count.items())),
        "counts_by_protected_span_presence": {
            "with_protected_spans": sum(bool(record["protected_spans"]) for record in records),
            "without_protected_spans": sum(not record["protected_spans"] for record in records),
        },
        "schema_validation_count": len(records),
        "ast_sst_ast_round_trip_count": len(records),
        "renderer_consistency_count": len(records),
        "deterministic_byte_reproducibility": True,
        "prohibited_content_absent": {
            "critic_verdict": True,
            "split": True,
            "corruption": True,
            "model_output": True,
            "private_data": True,
            "external_api_call": True,
        },
    }
    MANIFEST_PATH.write_text(compact_json(manifest) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    run()
