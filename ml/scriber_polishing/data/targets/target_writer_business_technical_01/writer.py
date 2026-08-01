"""Deterministic canonical target writer for the assigned business and technical plans."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

WRITER_ID = "target_writer_business_technical_01"
OUTPUT_DIR = Path(__file__).resolve().parent
POLISHING_ROOT = OUTPUT_DIR.parents[2]
SOURCE_ROOT = POLISHING_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scriber_polishing.ast_codec import document_to_dict  # noqa: E402
from scriber_polishing.document_ast import Block, BlockType, Document, ListItem  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_renderer import (  # noqa: E402
    render_html,
    render_markdown,
    render_plain_text,
)
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

INPUTS = (
    POLISHING_ROOT / "data" / "seeds" / "generator_general_business_01" / "plans.jsonl",
    POLISHING_ROOT / "data" / "seeds" / "generator_technical_project_01" / "plans.jsonl",
)
HANDOFF_PATH = POLISHING_ROOT / "handoffs" / "AGENT_TARGET_WRITER.md"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_plans() -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for path in INPUTS:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                raise ValueError(f"blank plan line in {path} at {line_number}")
            plans.append(json.loads(line))
    return plans


def salutation_for(address_mode: str) -> str:
    # This neutral professional salutation never introduces a conflicting pronoun.
    if address_mode in {"formal_sie", "personal_du_capitalized", "personal_du_lowercase", "mixed_correspondence"}:
        return "Guten Tag,"
    return "Guten Tag,"


def preferred_subject_entity(plan: dict[str, Any]) -> str | None:
    entities = plan["semantic_plan"]["entities"]
    for entity_type in ("product", "organization", "person", "location"):
        for entity in entities:
            if entity["type"] == entity_type:
                return entity["value"]
    return None


def subject_text(plan: dict[str, Any]) -> str:
    topic = plan["semantic_plan"]["structure"]["paragraph_topics"][0]
    entity = preferred_subject_entity(plan)
    return f"{topic} – {entity}" if entity else topic


def make_document(plan: dict[str, Any]) -> Document:
    structure = plan["semantic_plan"]["structure"]
    topics = structure["paragraph_topics"]
    blocks: list[Block] = []

    if structure["has_subject"]:
        blocks.append(Block(BlockType.SUBJECT, text=subject_text(plan)))

    for level in structure["heading_levels"]:
        topic_index = 0 if level == "h1" else min(1, len(topics) - 1)
        label = topics[topic_index]
        blocks.append(Block(BlockType.HEADING_1 if level == "h1" else BlockType.HEADING_2, text=label))

    if structure["has_salutation"]:
        blocks.append(Block(BlockType.SALUTATION, text=salutation_for(plan["address_mode"])))

    facts = sorted(plan["semantic_plan"]["facts"], key=lambda fact: fact["order"])
    for fact in facts:
        blocks.append(Block(BlockType.PARAGRAPH, text=fact["text"]))

    list_kind = structure["list_kind"]
    list_items = structure["list_items"]
    if list_kind != "none":
        levels = [1] * len(list_items)
        if list_kind == "nested" and levels:
            levels = [1, *([2] * (len(levels) - 1))]
        block_type = BlockType.ORDERED_LIST if list_kind in {"ordered", "nested"} else BlockType.UNORDERED_LIST
        blocks.append(
            Block(
                block_type,
                items=tuple(ListItem(level=level, text=text) for level, text in zip(levels, list_items, strict=False)),
            )
        )

    if structure["has_closing"]:
        blocks.append(Block(BlockType.CLOSING, text="Freundliche Grüße"))
    if structure["signature_lines"]:
        blocks.append(Block(BlockType.SIGNATURE, lines=tuple(structure["signature_lines"])))
    return Document(blocks=tuple(blocks))


def protected_spans(plan: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for fact in sorted(plan["semantic_plan"]["facts"], key=lambda item: item["order"]):
        for value in fact["protected_values"]:
            if value not in result:
                result.append(value)
    return result


def make_record(plan: dict[str, Any], prompt_hash: str) -> dict[str, Any]:
    document = make_document(plan)
    plain_text = render_plain_text(document)
    risk_tags = plan["risk_tags"]
    return {
        "schema_version": 1,
        "canonical_document_id": f"canonical_{plan['seed_id']}",
        "seed_id": plan["seed_id"],
        "source_plan_batch": plan["batch_id"],
        "plan_generator_agent": plan["generator_agent"],
        "target_writer_agent": WRITER_ID,
        "target_prompt_hash": prompt_hash,
        "semantic_plan_sha256": "sha256:" + sha256_bytes(canonical_json(plan["semantic_plan"]).encode("utf-8")),
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
        "ambiguity_flags": [
            tag for tag in risk_tags if any(marker in tag for marker in ("ambiguous", "ambiguity", "hard_negative"))
        ],
    }


def counts_by(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(record[key] for record in records).items()))


def make_manifest(
    plans: list[dict[str, Any]], records: list[dict[str, Any]], targets_bytes: bytes, prompt_hash: str
) -> dict[str, Any]:
    block_counts: Counter[str] = Counter()
    list_kind_counts: Counter[str] = Counter()
    fact_counts: Counter[str] = Counter()
    protected_presence: Counter[str] = Counter()
    for plan, record in zip(plans, records, strict=False):
        for block in record["target_ast"]["blocks"]:
            block_counts[block["type"]] += 1
        list_kind_counts[plan["semantic_plan"]["structure"]["list_kind"]] += 1
        fact_counts[str(len(plan["semantic_plan"]["facts"]))] += 1
        protected_presence["with_protected_spans" if record["protected_spans"] else "without_protected_spans"] += 1
    return {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "target_prompt_hash": prompt_hash,
        "assigned_input_batches": [
            {"path": path.relative_to(POLISHING_ROOT).as_posix(), "sha256": "sha256:" + sha256_file(path)}
            for path in INPUTS
        ],
        "record_count": len(records),
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in records}),
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "targets_sha256": "sha256:" + sha256_bytes(targets_bytes),
        "counts_by_domain": dict(sorted(Counter(plan["domain"] for plan in plans).items())),
        "counts_by_language": counts_by(records, "language"),
        "counts_by_document_type": dict(sorted(Counter(plan["document_type"] for plan in plans).items())),
        "counts_by_address_mode": counts_by(records, "address_mode"),
        "counts_by_block_type": dict(sorted(block_counts.items())),
        "counts_by_list_kind": dict(sorted(list_kind_counts.items())),
        "counts_by_fact_count": dict(sorted(fact_counts.items())),
        "counts_by_protected_span_presence": dict(sorted(protected_presence.items())),
        "schema_validation_count": len(records),
        "ast_sst_ast_round_trip_count": len(records),
        "renderer_consistency_count": len(records),
        "contains_critic_verdict": False,
        "contains_split": False,
        "contains_corruption": False,
        "contains_model_output": False,
        "contains_private_data": False,
        "contains_external_api_call": False,
    }


def render_outputs() -> tuple[bytes, bytes, int]:
    plans = load_plans()
    prompt_hash = "sha256:" + sha256_file(HANDOFF_PATH)
    records = [make_record(plan, prompt_hash) for plan in plans]
    canonical_ids = [record["canonical_document_id"] for record in records]
    seed_ids = [record["seed_id"] for record in records]
    if len(canonical_ids) != len(set(canonical_ids)) or len(seed_ids) != len(set(seed_ids)):
        raise ValueError("canonical document IDs and seed IDs must be unique")
    for record in records:
        validate_canonical_record(record)
    targets_bytes = ("\n".join(canonical_json(record) for record in records) + "\n").encode("utf-8")
    manifest = make_manifest(plans, records, targets_bytes, prompt_hash)
    manifest_bytes = (canonical_json(manifest) + "\n").encode("utf-8")
    return targets_bytes, manifest_bytes, len(records)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify the checked-in outputs are reproducible")
    args = parser.parse_args()
    targets_bytes, manifest_bytes, count = render_outputs()
    targets_path = OUTPUT_DIR / "targets.jsonl"
    manifest_path = OUTPUT_DIR / "manifest.json"
    if args.check:
        if not targets_path.exists() or not manifest_path.exists():
            raise SystemExit("target outputs do not exist")
        if targets_path.read_bytes() != targets_bytes or manifest_path.read_bytes() != manifest_bytes:
            raise SystemExit("target outputs are not reproducible")
        print(f"reproducible records={count} targets_sha256={sha256_bytes(targets_bytes)}")
        return
    targets_path.write_bytes(targets_bytes)
    manifest_path.write_bytes(manifest_bytes)
    print(f"written records={count} targets_sha256={sha256_bytes(targets_bytes)}")


if __name__ == "__main__":
    main()
