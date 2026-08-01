"""Deterministic canonical target writer for the assigned tax and real-estate plans."""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scriber_polishing.ast_codec import document_to_dict  # noqa: E402
from scriber_polishing.document_ast import Block, BlockType, Document, Inline, ListItem  # noqa: E402
from scriber_polishing.sst_parser import parse_sst  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text  # noqa: E402
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

WRITER_ID = "target_writer_tax_realestate_01"
OUTPUT_DIR = Path(__file__).resolve().parent
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
HANDOFF_PATH = PROJECT_ROOT / "handoffs" / "AGENT_TARGET_WRITER.md"
INPUT_RELATIVE_PATHS = (
    Path("data/seeds/generator_tax_legal_01/plans.jsonl"),
    Path("data/seeds/generator_real_estate_energy_01/plans.jsonl"),
)

EXPECTED_PLAN_FIELDS = frozenset(
    {
        "address_mode",
        "batch_id",
        "document_type",
        "domain",
        "generator_agent",
        "language",
        "legal_citation_style",
        "private_data",
        "prompt_hash",
        "risk_tags",
        "schema_version",
        "seed",
        "seed_id",
        "semantic_plan",
        "source_class",
        "style_profile",
        "unit_style",
    }
)
EXPECTED_STRUCTURE_FIELDS = frozenset(
    {
        "has_closing",
        "has_salutation",
        "has_subject",
        "heading_levels",
        "list_items",
        "list_kind",
        "paragraph_topics",
        "signature_lines",
    }
)
EXPECTED_FACT_FIELDS = frozenset({"condition", "fact_id", "modality", "order", "polarity", "protected_values", "text"})
ALLOWED_HEADING_LEVELS = frozenset({"h1", "h2"})
ALLOWED_LIST_KINDS = frozenset({"none", "ordered", "unordered", "nested"})
CONDITIONAL_PREFIXES = ("wenn ", "falls ", "sofern ", "soweit ", "solange ")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def require_string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must be an array of non-empty strings")
    return list(value)


def require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def closed_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected.difference(actual))
        unexpected = sorted(actual.difference(expected))
        raise ValueError(f"{label} fields differ; missing={missing}, unexpected={unexpected}")


def load_plans(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{path} is not UTF-8") from error
    lines = text.splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise ValueError(f"{path} must contain non-empty JSONL records only")
    plans: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_number} is invalid JSON") from error
        plan = dict(require_mapping(value, f"{path}:{line_number} plan"))
        closed_fields(plan, EXPECTED_PLAN_FIELDS, f"{path}:{line_number} plan")
        plans.append(plan)
    return plans


def validate_plan(plan: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any], list[Mapping[str, Any]]]:
    if plan["schema_version"] != 1:
        raise ValueError("plan schema_version must be 1")
    if plan["private_data"] is not False:
        raise ValueError("assigned plans must not contain private data")
    if plan["source_class"] != "ai_generated":
        raise ValueError("assigned plans must be synthetic semantic plans")
    for name in (
        "seed_id",
        "batch_id",
        "generator_agent",
        "document_type",
        "domain",
        "language",
        "style_profile",
        "address_mode",
        "legal_citation_style",
        "unit_style",
    ):
        require_string(plan[name], f"plan {name}")
    if isinstance(plan["seed"], bool) or not isinstance(plan["seed"], int):
        raise ValueError("plan seed must be an integer")
    semantic_plan = require_mapping(plan["semantic_plan"], "semantic_plan")
    structure = require_mapping(semantic_plan.get("structure"), "semantic_plan.structure")
    closed_fields(structure, EXPECTED_STRUCTURE_FIELDS, "semantic_plan.structure")
    raw_facts = semantic_plan.get("facts")
    if not isinstance(raw_facts, list) or not raw_facts:
        raise ValueError("semantic_plan.facts must be a non-empty array")
    facts: list[Mapping[str, Any]] = []
    for raw_fact in raw_facts:
        fact = require_mapping(raw_fact, "semantic fact")
        closed_fields(fact, EXPECTED_FACT_FIELDS, "semantic fact")
        require_string(fact["fact_id"], "fact id")
        require_string(fact["text"], "fact text")
        require_string(fact["modality"], "fact modality")
        require_string(fact["polarity"], "fact polarity")
        if isinstance(fact["order"], bool) or not isinstance(fact["order"], int):
            raise ValueError("fact order must be an integer")
        if fact["condition"] is not None:
            require_string(fact["condition"], "fact condition")
        require_string_list(fact["protected_values"], "fact protected_values")
        facts.append(fact)
    facts.sort(key=lambda item: item["order"])
    if [fact["order"] for fact in facts] != list(range(1, len(facts) + 1)):
        raise ValueError(f"{plan['seed_id']} fact order must be consecutive and unique")
    if len({fact["fact_id"] for fact in facts}) != len(facts):
        raise ValueError(f"{plan['seed_id']} fact IDs must be unique")
    return semantic_plan, structure, facts


def fact_text(fact: Mapping[str, Any]) -> str:
    text = require_string(fact["text"], "fact text")
    condition = fact["condition"]
    if condition is None:
        return text
    condition_text = require_string(condition, "fact condition")
    if condition_text.casefold() in text.casefold():
        return text
    comma = "," if condition_text.casefold().startswith(CONDITIONAL_PREFIXES) else ""
    return f"{text} Dies gilt{comma} {condition_text}."


def paragraph_fact_groups(
    facts: Sequence[Mapping[str, Any]], topics: Sequence[str]
) -> list[tuple[str | None, list[Mapping[str, Any]]]]:
    if not topics:
        return [(None, [fact]) for fact in facts]
    if len(facts) < len(topics):
        raise ValueError("there must be at least one fact per requested paragraph topic")
    groups: list[tuple[str | None, list[Mapping[str, Any]]]] = []
    for index, topic in enumerate(topics):
        group = [facts[index]] if index < len(topics) - 1 else list(facts[index:])
        groups.append((topic, group))
    return groups


def heading_type(level: str) -> BlockType:
    if level == "h1":
        return BlockType.HEADING_1
    if level == "h2":
        return BlockType.HEADING_2
    raise ValueError(f"unsupported heading level: {level}")


def text_block(block_type: BlockType, text: str) -> Block:
    return Block(type=block_type, text=text, inlines=(Inline(text),))


def salutation_for(plan: Mapping[str, Any]) -> str:
    language = plan["language"]
    mode = plan["address_mode"]
    if language == "en":
        return "Dear Sir or Madam," if mode == "formal_sie" else "Hello,"
    if language == "mixed":
        return "Guten Tag / Hello,"
    if language != "de-DE":
        raise ValueError(f"unsupported language: {language}")
    if mode == "formal_sie":
        return "Sehr geehrte Damen und Herren,"
    return "Guten Tag,"


def closing_for(plan: Mapping[str, Any]) -> str:
    language = plan["language"]
    if language == "en":
        return "Kind regards"
    if language == "mixed":
        return "Freundliche Grüße / Kind regards"
    if language != "de-DE":
        raise ValueError(f"unsupported language: {language}")
    return "Mit freundlichen Grüßen" if plan["address_mode"] == "formal_sie" else "Freundliche Grüße"


def protected_spans(facts: Iterable[Mapping[str, Any]]) -> list[str]:
    spans: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        for span in require_string_list(fact["protected_values"], "fact protected_values"):
            if span not in seen:
                seen.add(span)
                spans.append(span)
    return spans


def ambiguity_flags(plan: Mapping[str, Any]) -> list[str]:
    tags = require_string_list(plan["risk_tags"], "risk_tags")
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        lowered = tag.casefold()
        if any(marker in lowered for marker in ("ambiguous", "ambiguity", "hard_negative")) and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def list_block(structure: Mapping[str, Any]) -> Block | None:
    kind = require_string(structure["list_kind"], "list_kind")
    if kind not in ALLOWED_LIST_KINDS:
        raise ValueError(f"unsupported list kind: {kind}")
    item_texts = require_string_list(structure["list_items"], "list_items")
    if kind == "none":
        if item_texts:
            raise ValueError("list_kind none must have no list items")
        return None
    if not item_texts:
        raise ValueError(f"list_kind {kind} must have list items")
    if kind == "ordered":
        block_type = BlockType.ORDERED_LIST
        levels = [1] * len(item_texts)
    elif kind == "unordered":
        block_type = BlockType.UNORDERED_LIST
        levels = [1] * len(item_texts)
    else:
        block_type = BlockType.UNORDERED_LIST
        levels = [1, *([2] * (len(item_texts) - 1))]
    return Block(
        type=block_type,
        items=tuple(
            ListItem(level=level, text=text, inlines=(Inline(text),))
            for level, text in zip(levels, item_texts, strict=True)
        ),
    )


def build_document(
    plan: Mapping[str, Any], structure: Mapping[str, Any], facts: Sequence[Mapping[str, Any]]
) -> Document:
    for optional_flag in ("has_quote", "has_attachments", "has_post_script"):
        if optional_flag in structure:
            raise ValueError(f"unexpected optional structure flag in assigned closed structure: {optional_flag}")
    topics = require_string_list(structure["paragraph_topics"], "paragraph_topics")
    headings = require_string_list(structure["heading_levels"], "heading_levels")
    if any(level not in ALLOWED_HEADING_LEVELS for level in headings):
        raise ValueError("heading_levels contains an unsupported level")
    blocks: list[Block] = []
    has_subject = require_bool(structure["has_subject"], "has_subject")
    has_salutation = require_bool(structure["has_salutation"], "has_salutation")
    has_closing = require_bool(structure["has_closing"], "has_closing")
    if has_subject:
        if not topics:
            raise ValueError("a requested subject requires a supplied paragraph topic")
        blocks.append(text_block(BlockType.SUBJECT, topics[0]))
    if has_salutation:
        blocks.append(text_block(BlockType.SALUTATION, salutation_for(plan)))
    for index, (topic, fact_group) in enumerate(paragraph_fact_groups(facts, topics)):
        if headings:
            if topic is None:
                raise ValueError("requested headings require supplied paragraph topics")
            level = headings[min(index, len(headings) - 1)]
            blocks.append(text_block(heading_type(level), topic))
        blocks.append(text_block(BlockType.PARAGRAPH, " ".join(fact_text(fact) for fact in fact_group)))
    requested_list = list_block(structure)
    if requested_list is not None:
        blocks.append(requested_list)
    if has_closing:
        blocks.append(text_block(BlockType.CLOSING, closing_for(plan)))
    signature_lines = require_string_list(structure["signature_lines"], "signature_lines")
    if signature_lines:
        blocks.append(Block(type=BlockType.SIGNATURE, lines=tuple(signature_lines)))
    return Document(blocks=tuple(blocks))


def build_record(plan: Mapping[str, Any], prompt_hash: str) -> dict[str, Any]:
    semantic_plan, structure, facts = validate_plan(plan)
    document = build_document(plan, structure, facts)
    plain_text = render_plain_text(document)
    record = {
        "schema_version": 1,
        "canonical_document_id": f"canonical-{plan['seed_id']}",
        "seed_id": plan["seed_id"],
        "source_plan_batch": plan["batch_id"],
        "plan_generator_agent": plan["generator_agent"],
        "target_writer_agent": WRITER_ID,
        "target_prompt_hash": prompt_hash,
        "semantic_plan_sha256": sha256_bytes(canonical_json(semantic_plan).encode("utf-8")),
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
        "protected_spans": protected_spans(facts),
        "applied_normalizations": [],
        "rejected_normalizations": [],
        "ambiguity_flags": ambiguity_flags(plan),
    }
    return validate_canonical_record(record)


def records_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    return ("".join(canonical_json(record) + "\n" for record in records)).encode("utf-8")


def sorted_counter(values: Iterable[object]) -> dict[str, int]:
    counts = Counter(str(value) for value in values)
    return {key: counts[key] for key in sorted(counts)}


def validate_semantic_preservation(record: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    semantic_plan, structure, facts = validate_plan(plan)
    expected_provenance = {
        "seed_id": plan["seed_id"],
        "source_plan_batch": plan["batch_id"],
        "plan_generator_agent": plan["generator_agent"],
        "language": plan["language"],
        "style_profile": plan["style_profile"],
        "address_mode": plan["address_mode"],
        "legal_citation_style": plan["legal_citation_style"],
        "unit_style": plan["unit_style"],
        "semantic_plan_sha256": sha256_bytes(canonical_json(semantic_plan).encode("utf-8")),
    }
    for field, expected in expected_provenance.items():
        if record[field] != expected:
            raise ValueError(f"{plan['seed_id']} differs from plan provenance in {field}")
    plain_text = require_string(record["target_plain_text"], "target_plain_text")
    previous_fact_position = -1
    for fact in facts:
        source_fact_text = require_string(fact["text"], "fact text")
        fact_position = plain_text.find(source_fact_text, previous_fact_position + 1)
        if fact_position < 0:
            raise ValueError(f"{plan['seed_id']} omits or reorders {fact['fact_id']}")
        condition = fact["condition"]
        if condition is not None and plain_text.find(require_string(condition, "fact condition"), fact_position) < 0:
            raise ValueError(f"{plan['seed_id']} omits the condition for {fact['fact_id']}")
        previous_fact_position = fact_position
    expected_spans = protected_spans(facts)
    if record["protected_spans"] != expected_spans:
        raise ValueError(f"{plan['seed_id']} protected spans differ from the plan")
    ast = require_mapping(record["target_ast"], "target_ast")
    raw_blocks = ast.get("blocks")
    if not isinstance(raw_blocks, list):
        raise ValueError("target_ast blocks must be an array")
    blocks = [require_mapping(block, "target_ast block") for block in raw_blocks]
    types = [block["type"] for block in blocks]
    expected_presence = {
        BlockType.SUBJECT.value: int(require_bool(structure["has_subject"], "has_subject")),
        BlockType.SALUTATION.value: int(require_bool(structure["has_salutation"], "has_salutation")),
        BlockType.CLOSING.value: int(require_bool(structure["has_closing"], "has_closing")),
        BlockType.SIGNATURE.value: int(bool(require_string_list(structure["signature_lines"], "signature_lines"))),
    }
    for block_type, expected_count in expected_presence.items():
        if types.count(block_type) != expected_count:
            raise ValueError(f"{plan['seed_id']} structure differs for {block_type}")
    for forbidden_type in (BlockType.QUOTE, BlockType.ATTACHMENTS, BlockType.POST_SCRIPT):
        if forbidden_type.value in types:
            raise ValueError(f"{plan['seed_id']} introduced forbidden {forbidden_type.value}")
    topics = require_string_list(structure["paragraph_topics"], "paragraph_topics")
    headings = require_string_list(structure["heading_levels"], "heading_levels")
    actual_headings = [
        block for block in blocks if block["type"] in (BlockType.HEADING_1.value, BlockType.HEADING_2.value)
    ]
    if headings:
        expected_heading_types = [
            heading_type(headings[min(index, len(headings) - 1)]).value for index in range(len(topics))
        ]
        if [block["type"] for block in actual_headings] != expected_heading_types:
            raise ValueError(f"{plan['seed_id']} heading hierarchy differs from the plan")
        if [block["text"] for block in actual_headings] != topics:
            raise ValueError(f"{plan['seed_id']} heading labels differ from supplied topics")
    elif actual_headings:
        raise ValueError(f"{plan['seed_id']} introduced a heading")
    subject_blocks = [block for block in blocks if block["type"] == BlockType.SUBJECT.value]
    if subject_blocks and subject_blocks[0]["text"] != topics[0]:
        raise ValueError(f"{plan['seed_id']} subject does not name the first supplied topic")
    expected_paragraph_count = len(topics) if topics else len(facts)
    if types.count(BlockType.PARAGRAPH.value) != expected_paragraph_count:
        raise ValueError(f"{plan['seed_id']} paragraph structure differs from the plan")
    list_types = (BlockType.ORDERED_LIST.value, BlockType.UNORDERED_LIST.value)
    actual_lists = [block for block in blocks if block["type"] in list_types]
    list_kind = require_string(structure["list_kind"], "list_kind")
    expected_list_count = 0 if list_kind == "none" else 1
    if len(actual_lists) != expected_list_count:
        raise ValueError(f"{plan['seed_id']} list structure differs from the plan")
    if actual_lists:
        expected_items = require_string_list(structure["list_items"], "list_items")
        if [item["text"] for item in actual_lists[0]["items"]] != expected_items:
            raise ValueError(f"{plan['seed_id']} list items differ from the plan")
    signature_blocks = [block for block in blocks if block["type"] == BlockType.SIGNATURE.value]
    if signature_blocks and signature_blocks[0]["lines"] != structure["signature_lines"]:
        raise ValueError(f"{plan['seed_id']} signature lines differ from the plan")


def validate_records(records: Sequence[Mapping[str, Any]], plans: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    if len(records) != len(plans):
        raise ValueError("record count differs from plan count")
    canonical_ids = [record["canonical_document_id"] for record in records]
    record_seed_ids = [record["seed_id"] for record in records]
    plan_seed_ids = [plan["seed_id"] for plan in plans]
    source_seeds = [plan["seed"] for plan in plans]
    if len(set(canonical_ids)) != len(records):
        raise ValueError("canonical document IDs must be unique")
    if len(set(record_seed_ids)) != len(records) or record_seed_ids != plan_seed_ids:
        raise ValueError("record seed IDs must be unique and retain plan order")
    if len(set(source_seeds)) != len(plans):
        raise ValueError("source seeds must be unique")
    schema_count = 0
    round_trip_count = 0
    renderer_count = 0
    semantic_count = 0
    for record, plan in zip(records, plans, strict=True):
        validate_canonical_record(record)
        schema_count += 1
        parsed_document = parse_sst(record["target_sst"])
        if document_to_dict(parsed_document) != record["target_ast"]:
            raise ValueError(f"{record['seed_id']} AST-SST-AST round trip differs")
        round_trip_count += 1
        document = parsed_document
        if (
            render_sst(document) != record["target_sst"]
            or render_plain_text(document) != record["target_plain_text"]
            or render_markdown(document) != record["target_markdown"]
            or render_html(document) != record["target_html"]
            or record["source_text"] != record["target_plain_text"]
        ):
            raise ValueError(f"{record['seed_id']} renderer consistency differs")
        renderer_count += 1
        validate_semantic_preservation(record, plan)
        semantic_count += 1
    return {
        "schema_validation": schema_count,
        "ast_sst_ast_round_trip": round_trip_count,
        "renderer_consistency": renderer_count,
        "semantic_preservation": semantic_count,
    }


def build_manifest(
    plans: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    input_metadata: Sequence[Mapping[str, Any]],
    prompt_hash: str,
    target_bytes: bytes,
    validation_counts: Mapping[str, int],
) -> dict[str, Any]:
    block_types = (
        block["type"] for record in records for block in require_mapping(record["target_ast"], "target_ast")["blocks"]
    )
    protected_present = sum(bool(record["protected_spans"]) for record in records)
    return {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "writer_sha256": sha256_bytes(Path(__file__).read_bytes()),
        "target_prompt_hash": prompt_hash,
        "assigned_input_batches": list(input_metadata),
        "record_count": len(records),
        "unique_counts": {
            "canonical_document_ids": len({record["canonical_document_id"] for record in records}),
            "seed_ids": len({record["seed_id"] for record in records}),
            "source_seeds": len({plan["seed"] for plan in plans}),
        },
        "targets_sha256": sha256_bytes(target_bytes),
        "counts": {
            "by_domain": sorted_counter(plan["domain"] for plan in plans),
            "by_language": sorted_counter(record["language"] for record in records),
            "by_document_type": sorted_counter(plan["document_type"] for plan in plans),
            "by_address_mode": sorted_counter(record["address_mode"] for record in records),
            "by_block_type": sorted_counter(block_types),
            "by_list_kind": sorted_counter(
                require_mapping(plan["semantic_plan"], "semantic_plan")["structure"]["list_kind"] for plan in plans
            ),
            "by_fact_count": sorted_counter(
                len(require_mapping(plan["semantic_plan"], "semantic_plan")["facts"]) for plan in plans
            ),
            "by_protected_span_presence": {
                "absent": len(records) - protected_present,
                "present": protected_present,
            },
        },
        "validation_counts": dict(validation_counts),
        "deterministic_checks": {
            "canonical_compact_sorted_jsonl": True,
            "exactly_one_final_newline": target_bytes.endswith(b"\n") and not target_bytes.endswith(b"\n\n"),
            "in_process_second_generation_byte_identical": True,
        },
        "absence_confirmations": {
            "no_critic_verdict": True,
            "no_split_assignment": True,
            "no_corruption": True,
            "no_model_output": True,
            "no_private_data": all(plan["private_data"] is False for plan in plans),
            "no_external_api_call": True,
        },
    }


def main() -> None:
    prompt_hash = sha256_bytes(HANDOFF_PATH.read_bytes())
    all_plans: list[dict[str, Any]] = []
    input_metadata: list[dict[str, Any]] = []
    for relative_path in INPUT_RELATIVE_PATHS:
        path = PROJECT_ROOT / relative_path
        raw = path.read_bytes()
        plans = load_plans(path)
        all_plans.extend(plans)
        input_metadata.append(
            {
                "path": relative_path.as_posix(),
                "sha256": sha256_bytes(raw),
                "record_count": len(plans),
                "batch_ids": sorted({require_string(plan["batch_id"], "batch_id") for plan in plans}),
                "generator_agents": sorted(
                    {require_string(plan["generator_agent"], "generator_agent") for plan in plans}
                ),
            }
        )
    records = [build_record(plan, prompt_hash) for plan in all_plans]
    first_bytes = records_bytes(records)
    second_records = [build_record(plan, prompt_hash) for plan in all_plans]
    second_bytes = records_bytes(second_records)
    if first_bytes != second_bytes:
        raise ValueError("in-process target generation is not byte reproducible")
    validation_counts = validate_records(records, all_plans)
    manifest = build_manifest(
        all_plans,
        records,
        input_metadata,
        prompt_hash,
        first_bytes,
        validation_counts,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TARGETS_PATH.write_bytes(first_bytes)
    MANIFEST_PATH.write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    print(
        canonical_json(
            {
                "record_count": len(records),
                "target_prompt_hash": prompt_hash,
                "targets_sha256": sha256_bytes(first_bytes),
                "validation_counts": validation_counts,
            }
        )
    )


if __name__ == "__main__":
    main()
