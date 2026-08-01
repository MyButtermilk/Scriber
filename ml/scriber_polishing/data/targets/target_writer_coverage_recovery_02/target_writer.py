"""Build the isolated Coverage Recovery 02 canonical-target batch locally.

The writer consumes only the forty semantic plans in its assigned seed package.
It does not inspect split data, model outputs, critic material, or network services.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ML_ROOT = HERE.parents[2]
SRC_ROOT = ML_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from scriber_polishing.ast_codec import document_to_dict  # noqa: E402
from scriber_polishing.canonical_expander import VARIANT_COUNT, expand_canonical_variants  # noqa: E402
from scriber_polishing.document_ast import Block, BlockType, Document, ListItem  # noqa: E402
from scriber_polishing.schemas import validate_seed_plan  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_batch_validator import (  # noqa: E402
    TargetBatchValidationError,
    validate_target_batches,
)
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text  # noqa: E402
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

WRITER_ID = "target_writer_coverage_recovery_02"
SOURCE_PLAN_DIRECTORY = "generator_coverage_recovery_02"
SOURCE_PLAN_BATCH = "coverage_recovery_batch_02"
EXPECTED_RECORD_COUNT = 40
TARGET_PROMPT = (
    "Erzeuge aus ausschließlich den zugewiesenen semantischen Plänen professionelle, "
    "faktentreue deutsche Zieldokumente. Erhalte jede Aussage, geschützte Werte, "
    "Negationen, Modalitäten, Bedingungen, Reihenfolge, Struktur und Anredeform exakt; "
    "leite SST und Projektionen ausschließlich aus dem kanonischen Dokument-AST ab."
)
TARGET_PROMPT_HASH = "sha256:" + hashlib.sha256(TARGET_PROMPT.encode("utf-8")).hexdigest()
AGENT_ID_HASH = "sha256:" + hashlib.sha256(WRITER_ID.encode("utf-8")).hexdigest()

DATA_ROOT = ML_ROOT / "data"
CONTRACTS_ROOT = ML_ROOT / "contracts"
SOURCE_ROOT = DATA_ROOT / "seeds" / SOURCE_PLAN_DIRECTORY
PLAN_PATH = SOURCE_ROOT / "plans.jsonl"
SOURCE_MANIFEST_PATH = SOURCE_ROOT / "manifest.json"
TARGETS_PATH = HERE / "targets.jsonl"
MANIFEST_PATH = HERE / "manifest.json"
VALIDATION_PATH = HERE / "validation.json"
REPORT_PATH = HERE / "generation_report.md"
EXPECTED_MISSING_CANONICAL_EXAMPLE_SCHEMA = CONTRACTS_ROOT / "canonical_example_schema.json"


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _semantic_hash(value: Mapping[str, Any]) -> str:
    return _sha256(_canonical_json(value).encode("utf-8"))


def _jsonl_payload(records: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(_canonical_json(record) + "\n" for record in records).encode("utf-8")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _input_hashes() -> dict[str, str]:
    inputs = (
        PLAN_PATH,
        SOURCE_MANIFEST_PATH,
        CONTRACTS_ROOT / "seed_plan_schema.json",
        CONTRACTS_ROOT / "sst_v1_schema.json",
        CONTRACTS_ROOT / "behavioral_contract.yaml",
        CONTRACTS_ROOT / "de_business_style.yaml",
    )
    return {
        path.relative_to(ML_ROOT).as_posix(): _sha256(path.read_bytes())
        for path in inputs
    }


def _load_source_manifest() -> dict[str, Any]:
    value = json.loads(SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("source manifest must be an object")
    return value


def _load_plans() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_manifest = _load_source_manifest()
    plan_bytes = PLAN_PATH.read_bytes()
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(plan_bytes.decode("utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"source plan line {line_number} must not be blank")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"source plan line {line_number} must be an object")
        values.append(validate_seed_plan(value))
    if len(values) != EXPECTED_RECORD_COUNT:
        raise ValueError(f"expected {EXPECTED_RECORD_COUNT} source plans, got {len(values)}")
    if len({plan["seed_id"] for plan in values}) != EXPECTED_RECORD_COUNT:
        raise ValueError("source plans contain duplicate seed_id values")
    if any(plan["batch_id"] != SOURCE_PLAN_BATCH for plan in values):
        raise ValueError("source plan batch identifier differs from the assigned batch")
    if source_manifest.get("record_count") != EXPECTED_RECORD_COUNT:
        raise ValueError("source manifest record_count differs from the assigned batch")
    expected_plans_hash = source_manifest.get("plans_sha256")
    if expected_plans_hash != _sha256(plan_bytes).removeprefix("sha256:"):
        raise ValueError("source manifest plans_sha256 differs from plans.jsonl")
    return values, source_manifest


def _subject_text(plan: Mapping[str, Any]) -> str:
    """Use the supplied document type verbatim instead of adding a subject claim."""

    return plan["document_type"]


def _salutation(plan: Mapping[str, Any]) -> str:
    if plan["address_mode"] == "formal_sie":
        return "Sehr geehrte Damen und Herren,"
    return "Guten Tag,"


def _structure_blocks(plan: Mapping[str, Any]) -> list[Block]:
    structure = plan["semantic_plan"]["structure"]
    blocks: list[Block] = []
    if structure["has_subject"]:
        blocks.append(Block(BlockType.SUBJECT, text=_subject_text(plan)))
    for level in structure["heading_levels"]:
        block_type = {"h1": BlockType.HEADING_1, "h2": BlockType.HEADING_2}.get(level)
        if block_type is None:
            raise ValueError(f"{plan['seed_id']}: unsupported declared heading level {level!r}")
        # The plan's document_type is the only supplied title text, so no heading is invented.
        blocks.append(Block(block_type, text=plan["document_type"]))
    if structure["has_salutation"]:
        blocks.append(Block(BlockType.SALUTATION, text=_salutation(plan)))
    facts = sorted(plan["semantic_plan"]["facts"], key=lambda fact: fact["order"])
    blocks.extend(Block(BlockType.PARAGRAPH, text=fact["text"]) for fact in facts)
    list_kind = structure["list_kind"]
    if list_kind != "none":
        block_type = BlockType.ORDERED_LIST if list_kind in {"ordered", "nested"} else BlockType.UNORDERED_LIST
        items = tuple(
            ListItem(level=2 if list_kind == "nested" and index > 0 else 1, text=item)
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
    for fact in sorted(plan["semantic_plan"]["facts"], key=lambda item: item["order"]):
        values.extend(fact["protected_values"])
    return list(dict.fromkeys(values))


def _assert_record_semantics(plan: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    plain_text = record["target_plain_text"]
    semantic_plan = plan["semantic_plan"]
    facts = sorted(semantic_plan["facts"], key=lambda fact: fact["order"])
    cursor = 0
    for fact in facts:
        if fact["text"] not in plain_text:
            raise ValueError(f"{plan['seed_id']}: fact {fact['fact_id']} is absent")
        position = plain_text.index(fact["text"], cursor)
        if position < cursor:
            raise ValueError(f"{plan['seed_id']}: fact order changed")
        cursor = position + len(fact["text"])
        for protected_value in fact["protected_values"]:
            if protected_value not in fact["text"]:
                raise ValueError(
                    f"{plan['seed_id']}: protected value missing from fact {fact['fact_id']}: {protected_value!r}"
                )
    if record["semantic_plan"] != semantic_plan:
        raise ValueError(f"{plan['seed_id']}: semantic plan changed")
    if record["address_mode"] != plan["address_mode"]:
        raise ValueError(f"{plan['seed_id']}: address mode changed")
    if "48 €/m²" not in plain_text:
        raise ValueError(f"{plan['seed_id']}: professional unit surface is absent")
    if "48&nbsp;€/m²" not in record["target_html"]:
        raise ValueError(f"{plan['seed_id']}: HTML lacks central non-breaking unit spacing")
    if record["source_text"] != plain_text:
        raise ValueError(f"{plan['seed_id']}: source_text differs from plain target")
    if any(protected_value not in plain_text for protected_value in record["protected_spans"]):
        raise ValueError(f"{plan['seed_id']}: a protected span is absent from the target")
    expected_headings = list(semantic_plan["structure"]["heading_levels"])
    actual_headings = [
        block["type"]
        for block in record["target_ast"]["blocks"]
        if block["type"] in {BlockType.HEADING_1.value, BlockType.HEADING_2.value}
    ]
    if actual_headings != [{"h1": BlockType.HEADING_1.value, "h2": BlockType.HEADING_2.value}[level] for level in expected_headings]:
        raise ValueError(f"{plan['seed_id']}: heading structure changed")
    for block in record["target_ast"]["blocks"]:
        if block["type"] in {BlockType.HEADING_1.value, BlockType.HEADING_2.value} and block["text"] != plan[
            "document_type"
        ]:
            raise ValueError(f"{plan['seed_id']}: heading text was invented or changed")


def build_record(plan: Mapping[str, Any]) -> dict[str, Any]:
    document = Document(blocks=tuple(_structure_blocks(plan)))
    semantic_plan = copy.deepcopy(plan["semantic_plan"])
    document_id = f"canonical_coverage_recovery_{plan['seed_id']}"
    record: dict[str, Any] = {
        "schema_version": 1,
        "canonical_document_id": document_id,
        "parent_canonical_document_id": document_id,
        "canonical_generator_id": "local_canonical_expander",
        "canonical_generator_version": "1",
        "variant_index": 1,
        "seed_id": plan["seed_id"],
        "source_plan_batch": plan["batch_id"],
        "plan_generator_agent": plan["generator_agent"],
        "target_writer_agent": WRITER_ID,
        "target_prompt_hash": TARGET_PROMPT_HASH,
        "semantic_plan_sha256": _semantic_hash(semantic_plan),
        "semantic_plan": semantic_plan,
        "domain": plan["domain"],
        "document_type": plan["document_type"],
        "risk_tags": copy.deepcopy(plan["risk_tags"]),
        "language": plan["language"],
        "style_profile": plan["style_profile"],
        "address_mode": plan["address_mode"],
        "legal_citation_style": plan["legal_citation_style"],
        "unit_style": plan["unit_style"],
        "normalize_valid_variants": True,
        "protected_spans": _protected_spans(plan),
        "applied_normalizations": [],
        "rejected_normalizations": [],
        "ambiguity_flags": [],
        "target_ast": document_to_dict(document),
        "target_sst": render_sst(document),
        "target_plain_text": render_plain_text(document),
        "target_markdown": render_markdown(document),
        "target_html": render_html(document),
    }
    record["source_text"] = record["target_plain_text"]
    validate_canonical_record(record)
    _assert_record_semantics(plan, record)
    return record


def _validate_expansion(plans: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    expanded_records: list[dict[str, Any]] = []
    expansion_hashes: list[str] = []
    for plan, record in zip(plans, records, strict=True):
        first = expand_canonical_variants(plan, record)
        second = expand_canonical_variants(plan, record)
        first_payload = _jsonl_payload(first)
        second_payload = _jsonl_payload(second)
        if len(first) != VARIANT_COUNT or len(second) != VARIANT_COUNT:
            raise ValueError(f"{plan['seed_id']}: canonical expander did not create all seven variants")
        if first_payload != second_payload:
            raise ValueError(f"{plan['seed_id']}: canonical expansion is not deterministic")
        for variant in first:
            validate_canonical_record(variant)
        expanded_records.extend(first)
        expansion_hashes.append(_sha256(first_payload))
    all_payload = _jsonl_payload(expanded_records)
    return {
        "variants_per_target": VARIANT_COUNT,
        "expanded_record_count": len(expanded_records),
        "all_seven_variants_validated": len(expanded_records) == len(records) * VARIANT_COUNT,
        "double_expansion_byte_identical": True,
        "expanded_records_sha256": _sha256(all_payload),
        "per_target_expansion_sha256": expansion_hashes,
    }


def _validate_isolated_batch() -> dict[str, Any]:
    """Run the public batch validator against a temporary root containing only this batch."""

    with tempfile.TemporaryDirectory(prefix="target_writer_validation_", dir=DATA_ROOT) as temporary_name:
        validation_root = Path(temporary_name)
        batch_directory = validation_root / WRITER_ID
        batch_directory.mkdir()
        shutil.copyfile(TARGETS_PATH, batch_directory / "targets.jsonl")
        shutil.copyfile(MANIFEST_PATH, batch_directory / "manifest.json")
        try:
            return validate_target_batches(validation_root)
        except TargetBatchValidationError as error:
            raise ValueError("public target batch validation failed") from error


def _manifest(
    records: Sequence[Mapping[str, Any]],
    targets_payload: bytes,
    input_hashes: Mapping[str, str],
    source_manifest: Mapping[str, Any],
    expansion: Mapping[str, Any],
) -> dict[str, Any]:
    assigned_inputs = [
        {"path": path, "sha256": digest}
        for path, digest in input_hashes.items()
    ]
    return {
        "schema_version": 1,
        "batch_id": WRITER_ID,
        "writer_id": WRITER_ID,
        "target_writer_agent": WRITER_ID,
        "target_writer_agent_sha256": AGENT_ID_HASH,
        "target_prompt_hash": TARGET_PROMPT_HASH,
        "assigned_input_batches": assigned_inputs,
        "record_count": len(records),
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in records}),
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "targets_sha256": _sha256(targets_payload),
        "target_writer_sha256": _sha256(Path(__file__).read_bytes()),
        "source_plan": {
            "batch_id": SOURCE_PLAN_BATCH,
            "generator_agent": source_manifest["generator_agent"],
            "plans_sha256": source_manifest["plans_sha256"],
            "record_count": source_manifest["record_count"],
        },
        "canonical_expansion_validation": {
            "variants_per_target": expansion["variants_per_target"],
            "expanded_record_count": expansion["expanded_record_count"],
            "expanded_records_sha256": expansion["expanded_records_sha256"],
        },
        "generation_boundary": {
            "semantic_plans_only_input": True,
            "external_or_model_api_calls": 0,
            "human_review": False,
            "critic_material_read": False,
            "split_data_read": False,
        },
    }


def _validation(
    records: Sequence[Mapping[str, Any]],
    first_payload: bytes,
    second_payload: bytes,
    input_hashes: Mapping[str, str],
    expansion: Mapping[str, Any],
    batch_summary: Mapping[str, Any],
) -> dict[str, Any]:
    canonical_example_present = EXPECTED_MISSING_CANONICAL_EXAMPLE_SCHEMA.exists()
    output_hashes = {
        "target_writer.py": _sha256(Path(__file__).read_bytes()),
        "targets.jsonl": _sha256(first_payload),
        "manifest.json": _sha256(MANIFEST_PATH.read_bytes()),
    }
    return {
        "schema_version": 1,
        "batch_id": WRITER_ID,
        "writer_id": WRITER_ID,
        "target_writer_agent_sha256": AGENT_ID_HASH,
        "target_prompt_hash": TARGET_PROMPT_HASH,
        "record_count": len(records),
        "input_sha256": dict(input_hashes),
        "output_sha256": output_hashes,
        "determinism": {
            "double_generation_byte_identical": first_payload == second_payload,
            "first_targets_sha256": _sha256(first_payload),
            "second_targets_sha256": _sha256(second_payload),
            "external_or_model_api_calls": 0,
        },
        "canonical_validation": {
            "validate_canonical_record_passed": len(records),
            "all_plain_markdown_html_projections_ast_derived": True,
            "all_protected_spans_present": True,
            "fact_order_preserved": True,
            "negation_modality_and_condition_preserved_in_semantic_plan": True,
            "professional_unit_surface": "48 €/m²",
            "html_nonbreaking_unit_spacing": True,
        },
        "canonical_expander": dict(expansion),
        "batch_validator": dict(batch_summary),
        "contract_resolution": {
            "requested_canonical_example_schema_present": canonical_example_present,
            "active_canonical_contract": "contracts/sst_v1_schema.json plus validators.validate_canonical_record",
            "missing_requested_schema_note": (
                "contracts/canonical_example_schema.json was absent; active SST and canonical validators were used"
                if not canonical_example_present
                else "contracts/canonical_example_schema.json was present but active SST and canonical validators remain authoritative"
            ),
        },
        "generation_boundary": {
            "external_or_model_api_calls": 0,
            "human_review": False,
            "critic_material_read": False,
            "split_data_read": False,
        },
    }


def _report(validation: Mapping[str, Any]) -> str:
    output_hashes = validation["output_sha256"]
    expansion = validation["canonical_expander"]
    inputs = validation["input_sha256"]
    input_lines = "\n".join(f"  - `{path}`: `{digest}`" for path, digest in inputs.items())
    return (
        "# Coverage Recovery 02 – Target Writer Report\n\n"
        f"- Writer-ID: `{WRITER_ID}`\n"
        f"- Writer-ID-SHA-256: `{AGENT_ID_HASH}`\n"
        f"- Target-Prompt-Hash: `{TARGET_PROMPT_HASH}`\n"
        f"- Quelle: `{SOURCE_PLAN_DIRECTORY}` / `{SOURCE_PLAN_BATCH}`\n"
        f"- Targets: `{validation['record_count']}`\n"
        f"- `targets.jsonl`-SHA-256: `{output_hashes['targets.jsonl']}`\n"
        f"- `manifest.json`-SHA-256: `{output_hashes['manifest.json']}`\n"
        f"- `target_writer.py`-SHA-256: `{output_hashes['target_writer.py']}`\n"
        "- Doppelte deterministische Erzeugung: bestanden; die JSONL-Bytes sind identisch.\n"
        "- `validate_canonical_record`: 40/40 bestanden.\n"
        "- Öffentlicher Batch-Validator: bestanden, isoliert gegen ausschließlich dieses Paket.\n"
        f"- Canonical Expander: `{expansion['variants_per_target']}/7` Varianten je Target, "
        f"`{expansion['expanded_record_count']}` Varianten insgesamt, doppelt deterministisch validiert.\n"
        "- Faktenreihenfolge, Negation, Modalität, Bedingungen, geschützte Werte und `48 €/m²`: erhalten.\n"
        "- Plain, Markdown und HTML werden ausschließlich aus dem AST gerendert; HTML verwendet `&nbsp;` "
        "für die zentrale Einheiten-Schreibweise.\n"
        "- Externe oder Modell-API-Aufrufe: 0. Menschliche Prüfung: nein.\n"
        "- Hinweis: `contracts/canonical_example_schema.json` war nicht vorhanden; maßgeblich waren "
        "`contracts/sst_v1_schema.json` und `validators.validate_canonical_record`.\n\n"
        "## Exakte Eingabe-Hashes\n\n"
        f"{input_lines}\n"
    )


def generate() -> dict[str, Any]:
    plans, source_manifest = _load_plans()
    first_records = [build_record(plan) for plan in plans]
    second_records = [build_record(plan) for plan in plans]
    first_payload = _jsonl_payload(first_records)
    second_payload = _jsonl_payload(second_records)
    if first_payload != second_payload:
        raise ValueError("deterministic double generation produced different targets.jsonl bytes")
    if len(first_records) != EXPECTED_RECORD_COUNT:
        raise ValueError("writer did not produce exactly forty records")
    expansion = _validate_expansion(plans, first_records)
    input_hashes = _input_hashes()
    manifest = _manifest(first_records, first_payload, input_hashes, source_manifest, expansion)
    _write_bytes(TARGETS_PATH, first_payload)
    _write_json(MANIFEST_PATH, manifest)
    batch_summary = _validate_isolated_batch()
    validation = _validation(
        first_records,
        first_payload,
        second_payload,
        input_hashes,
        expansion,
        batch_summary,
    )
    _write_json(VALIDATION_PATH, validation)
    _write_bytes(REPORT_PATH, _report(validation).encode("utf-8"))
    return {
        "batch_id": WRITER_ID,
        "record_count": len(first_records),
        "targets_sha256": manifest["targets_sha256"],
        "canonical_expanded_record_count": expansion["expanded_record_count"],
    }


def main() -> int:
    print(json.dumps(generate(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
