#!/usr/bin/env python3
"""Build the immutable Coverage Recovery v1.01 plan and target repair overlay.

The repair is deliberately closed over the forty assigned source plans and
targets plus the pinned blinded preflight package.  It does not derive a wider
repair scope from content that happens to resemble one of the reviewed cases.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

OUTPUT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = OUTPUT_DIR.parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scriber_polishing.ast_codec import document_from_dict, document_to_dict  # noqa: E402
from scriber_polishing.canonical_expander import VARIANT_COUNT, expand_canonical_variants  # noqa: E402
from scriber_polishing.document_ast import Block, BlockType, Document, Inline  # noqa: E402
from scriber_polishing.schemas import validate_seed_plan  # noqa: E402
from scriber_polishing.sst_parser import parse_sst  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_batch_validator import _validate_batch  # noqa: E402
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text  # noqa: E402
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

WRITER_ID = "target_repair_coverage_v1_01"
WRITER_VERSION = 1
PLAN_REPAIR_AGENT = "plan_repair_coverage_v1_01"
PLAN_REPAIR_BATCH = "coverage_repair_v1_01"
EXPECTED_RECORD_COUNT = 40
EXPECTED_ADDRESS_REPAIR_COUNT = 7
EXPECTED_HEADING_REPAIR_COUNT = 10
EXPECTED_OVERLAP_COUNT = 3
EXPECTED_CHANGED_SEED_COUNT = 14
EXPECTED_EXPANDED_RECORD_COUNT = EXPECTED_RECORD_COUNT * VARIANT_COUNT

SOURCE_PLAN_PATH = PROJECT_ROOT / "data" / "seeds" / "generator_coverage_recovery_02" / "plans.jsonl"
SOURCE_PLAN_MANIFEST_PATH = PROJECT_ROOT / "data" / "seeds" / "generator_coverage_recovery_02" / "manifest.json"
SOURCE_TARGET_PATH = PROJECT_ROOT / "data" / "targets" / "target_writer_coverage_recovery_02" / "targets.jsonl"
SOURCE_TARGET_MANIFEST_PATH = PROJECT_ROOT / "data" / "targets" / "target_writer_coverage_recovery_02" / "manifest.json"
PREFLIGHT_CASES_PATH = PROJECT_ROOT / "artifacts" / "critic_package_coverage_recovery_v1" / "cases.jsonl"
PREFLIGHT_MANIFEST_PATH = PROJECT_ROOT / "artifacts" / "critic_package_coverage_recovery_v1" / "manifest.json"
PREFLIGHT_INDEX_PATH = PROJECT_ROOT / "artifacts" / "private" / "critic_package_coverage_recovery_v1_index.json"
HANDOFF_PATH = PROJECT_ROOT / "handoffs" / "AGENT_TARGET_REPAIR_COVERAGE_V1.md"

PLANS_PATH = OUTPUT_DIR / "plans.jsonl"
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
VALIDATION_PATH = OUTPUT_DIR / "validation.json"
REPORT_PATH = OUTPUT_DIR / "report.md"

EXPECTED_SOURCE_PLANS_SHA256 = "bce116acc85a075bcc135755b5fff24999c13328e015fc4287ea168357631c20"
EXPECTED_SOURCE_TARGETS_SHA256 = "1683bb22502f2f1186f2c43850dcd0ff4c047db2c870ff1a607249ec2b9d35c0"
EXPECTED_SOURCE_PLAN_MANIFEST_SHA256 = "364926fd4999e957423fa53da976aa5eb8c678d932c2260f69f29ecd67950c5d"
EXPECTED_SOURCE_TARGET_MANIFEST_SHA256 = "15e362f6bd606f1eabebf9b6ff70fb5e34698a03f339cdccd3e701cdc25a78ea"
EXPECTED_PREFLIGHT_PACKAGE_SHA256 = "a5179bba0bceb23396891f1e3898af39d35553cbbcfb1e9ef4301eddb54c348e"
EXPECTED_PREFLIGHT_INDEX_SHA256 = "9e12b35e4d62de79db44f5a559eeda38da6d16d699fa363e72ab78c331d90cd0"
EXPECTED_PREFLIGHT_BLIND_SEED = 1_020_240

FORMAL_ADDRESS_CASE_IDS = frozenset(
    {
        "case_18273ded3cfa643fc74a1732",
        "case_1c653888b64455af6441d7d2",
        "case_1daeef87c16fb99bbf4414f6",
        "case_57b0a2e70a73797b3c4c81d2",
        "case_c4fe2c5ef7e0ba47fd1fe621",
        "case_75a97bd33a7d0f97277a5ed6",
        "case_a40f85079b4fc123cf49c94c",
    }
)
DUPLICATE_SUBJECT_HEADING_CASE_IDS = frozenset(
    {
        "case_a69e603d0c2c5d68cd538046",
        "case_1c653888b64455af6441d7d2",
        "case_1daeef87c16fb99bbf4414f6",
        "case_3f9890f61addaad90f006251",
        "case_2c9e2480369730bcdaa8fe20",
        "case_c4fe2c5ef7e0ba47fd1fe621",
        "case_235fa897ca92c64a753b466a",
        "case_84aaf86a7816103c196527e8",
        "case_5dd6d6d36242a7a60dc3ad0a",
        "case_194e34a7e17e5ff41d1dda8f",
    }
)

FORMAL_SOURCE_FACT_TEXT = (
    "Seit Montag seid ihr für die Rückmeldung zur Flächenprüfung eingeteilt; "
    "die Reihenfolge der Angaben bleibt unverändert."
)
FORMAL_REPAIRED_FACT_TEXT = (
    "Seit Montag seid Ihr für die Rückmeldung zur Flächenprüfung eingeteilt; "
    "die Reihenfolge der Angaben bleibt unverändert."
)


@dataclass(frozen=True)
class RepairFlags:
    """The two closed repair classes that can apply to one source seed."""

    formal_address: bool = False
    duplicate_subject_heading: bool = False

    @property
    def changed(self) -> bool:
        return self.formal_address or self.duplicate_subject_heading


@dataclass(frozen=True)
class RepairScope:
    """Resolved seed scope only; case IDs remain an implementation-only input."""

    address_seed_ids: frozenset[str]
    heading_seed_ids: frozenset[str]

    @property
    def changed_seed_ids(self) -> frozenset[str]:
        return self.address_seed_ids | self.heading_seed_ids

    @property
    def overlap_seed_ids(self) -> frozenset[str]:
        return self.address_seed_ids & self.heading_seed_ids

    def flags_for(self, seed_id: str) -> RepairFlags:
        return RepairFlags(
            formal_address=seed_id in self.address_seed_ids,
            duplicate_subject_heading=seed_id in self.heading_seed_ids,
        )


@dataclass(frozen=True)
class BuildResult:
    plans: tuple[dict[str, Any], ...]
    records: tuple[dict[str, Any], ...]
    plans_bytes: bytes
    targets_bytes: bytes
    manifest: dict[str, Any]
    manifest_bytes: bytes
    validation: dict[str, Any]
    validation_bytes: bytes
    report: str
    report_bytes: bytes


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_prefixed(value: bytes) -> str:
    return "sha256:" + sha256_bytes(value)


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _jsonl_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    return ("".join(canonical_json(record) + "\n" for record in records)).encode("utf-8")


def _pretty_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def repository_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


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


def _index_by_seed(rows: Sequence[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        seed_id = row.get("seed_id")
        if not isinstance(seed_id, str) or not seed_id:
            raise ValueError(f"{label}: record has no valid seed_id")
        if seed_id in indexed:
            raise ValueError(f"{label}: duplicate seed_id {seed_id}")
        indexed[seed_id] = row
    return indexed


def _semantic_plan_hash(plan: Mapping[str, Any]) -> str:
    semantic_plan = plan["semantic_plan"]
    if not isinstance(semantic_plan, Mapping):
        raise ValueError("plan semantic_plan must be an object")
    return sha256_prefixed(canonical_json(semantic_plan).encode("utf-8"))


def _scope_hash(seed_ids: Sequence[str] | frozenset[str]) -> str:
    return sha256_prefixed(("\n".join(sorted(seed_ids)) + "\n").encode("utf-8"))


def _fact_by_id(plan: Mapping[str, Any], fact_id: str) -> dict[str, Any]:
    facts = plan["semantic_plan"]["facts"]
    matches = [fact for fact in facts if fact["fact_id"] == fact_id]
    if len(matches) != 1:
        raise ValueError(f"{plan['seed_id']}: expected exactly one {fact_id} fact")
    return matches[0]


def _load_source_inputs() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if sha256_file(SOURCE_PLAN_PATH) != EXPECTED_SOURCE_PLANS_SHA256:
        raise ValueError("source plans hash differs from the pinned Coverage Recovery package")
    if sha256_file(SOURCE_TARGET_PATH) != EXPECTED_SOURCE_TARGETS_SHA256:
        raise ValueError("source targets hash differs from the pinned Coverage Recovery package")
    if sha256_file(SOURCE_PLAN_MANIFEST_PATH) != EXPECTED_SOURCE_PLAN_MANIFEST_SHA256:
        raise ValueError("source plan manifest hash differs from the pinned Coverage Recovery package")
    if sha256_file(SOURCE_TARGET_MANIFEST_PATH) != EXPECTED_SOURCE_TARGET_MANIFEST_SHA256:
        raise ValueError("source target manifest hash differs from the pinned Coverage Recovery package")

    plan_manifest = _read_json_object(SOURCE_PLAN_MANIFEST_PATH)
    target_manifest = _read_json_object(SOURCE_TARGET_MANIFEST_PATH)
    if plan_manifest.get("plans_sha256") != EXPECTED_SOURCE_PLANS_SHA256:
        raise ValueError("source plan manifest does not attest the pinned plans bytes")
    if target_manifest.get("targets_sha256") != sha256_prefixed(SOURCE_TARGET_PATH.read_bytes()):
        raise ValueError("source target manifest does not attest the pinned targets bytes")
    if plan_manifest.get("record_count") != EXPECTED_RECORD_COUNT or target_manifest.get("record_count") != EXPECTED_RECORD_COUNT:
        raise ValueError("source manifest record count differs from the assigned scope")

    source_plans = _index_by_seed(_read_jsonl(SOURCE_PLAN_PATH), "source plans")
    source_targets = _index_by_seed(_read_jsonl(SOURCE_TARGET_PATH), "source targets")
    if len(source_plans) != EXPECTED_RECORD_COUNT or len(source_targets) != EXPECTED_RECORD_COUNT:
        raise ValueError("source inputs do not contain exactly forty records each")
    if set(source_plans) != set(source_targets):
        raise ValueError("source plan and target seed sets differ")

    for seed_id in sorted(source_plans):
        plan = validate_seed_plan(source_plans[seed_id])
        target = validate_canonical_record(source_targets[seed_id])
        if target["semantic_plan"] != plan["semantic_plan"]:
            raise ValueError(f"{seed_id}: source target semantic plan differs from source plan")
        if target["semantic_plan_sha256"] != _semantic_plan_hash(plan):
            raise ValueError(f"{seed_id}: source target semantic plan hash differs from source plan")
        if target["address_mode"] != plan["address_mode"]:
            raise ValueError(f"{seed_id}: source target address mode differs from source plan")
        if target["source_plan_batch"] != plan["batch_id"]:
            raise ValueError(f"{seed_id}: source target batch differs from source plan")
        if target["plan_generator_agent"] != plan["generator_agent"]:
            raise ValueError(f"{seed_id}: source target plan generator differs from source plan")
        source_plans[seed_id] = plan
        source_targets[seed_id] = target
    return source_plans, source_targets


def _assert_preflight_case_matches_source(
    case: Mapping[str, Any],
    plan: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    case_id: str,
) -> None:
    if case.get("semantic_plan") != plan["semantic_plan"]:
        raise ValueError(f"{case_id}: blinded preflight semantic plan does not match pinned source")
    for field in ("target_ast", "target_sst", "target_plain_text", "target_markdown", "target_html", "protected_spans"):
        if case.get(field) != target[field]:
            raise ValueError(f"{case_id}: blinded preflight {field} does not match pinned source")


def _assert_formal_source_case(plan: Mapping[str, Any], target: Mapping[str, Any]) -> None:
    seed_id = str(plan["seed_id"])
    if plan["address_mode"] != "formal_sie":
        raise ValueError(f"{seed_id}: formal repair source is not formal_sie")
    if "seit_seid" not in plan["risk_tags"]:
        raise ValueError(f"{seed_id}: formal repair source lacks seit_seid risk tag")
    fact = _fact_by_id(plan, "f3")
    if fact["text"] != FORMAL_SOURCE_FACT_TEXT or fact["protected_values"] != ["Seit", "seid"]:
        raise ValueError(f"{seed_id}: formal repair fact differs from the approved exact source surface")
    document = document_from_dict(target["target_ast"])
    matches = [block for block in document.blocks if block.text == FORMAL_SOURCE_FACT_TEXT]
    if len(matches) != 1 or matches[0].type is not BlockType.PARAGRAPH:
        raise ValueError(f"{seed_id}: formal repair target lacks one exact source fact paragraph")


def _assert_heading_source_case(plan: Mapping[str, Any], target: Mapping[str, Any]) -> None:
    seed_id = str(plan["seed_id"])
    structure = plan["semantic_plan"]["structure"]
    if structure["has_subject"] is not True or not structure["heading_levels"]:
        raise ValueError(f"{seed_id}: heading repair source lacks declared subject and heading")
    document = document_from_dict(target["target_ast"])
    subjects = [block for block in document.blocks if block.type is BlockType.SUBJECT]
    headings = [
        block for block in document.blocks if block.type in {BlockType.HEADING_1, BlockType.HEADING_2}
    ]
    if len(subjects) != 1 or subjects[0].text != plan["document_type"]:
        raise ValueError(f"{seed_id}: heading repair source subject differs from document type")
    if len(headings) != len(structure["heading_levels"]):
        raise ValueError(f"{seed_id}: heading repair source heading count differs from source plan")
    if any(heading.text != subjects[0].text for heading in headings):
        raise ValueError(f"{seed_id}: heading repair source does not contain the redundant heading surface")


def load_repair_scope(
    source_plans: Mapping[str, Mapping[str, Any]],
    source_targets: Mapping[str, Mapping[str, Any]],
) -> RepairScope:
    """Resolve the exact reviewed scope from the pinned blinded preflight evidence."""

    if sha256_file(PREFLIGHT_CASES_PATH) != EXPECTED_PREFLIGHT_PACKAGE_SHA256:
        raise ValueError("Coverage Recovery preflight package hash differs from the pinned input")
    if sha256_file(PREFLIGHT_INDEX_PATH) != EXPECTED_PREFLIGHT_INDEX_SHA256:
        raise ValueError("Coverage Recovery preflight private-index hash differs from the pinned input")
    manifest = _read_json_object(PREFLIGHT_MANIFEST_PATH)
    if manifest.get("package_sha256") != EXPECTED_PREFLIGHT_PACKAGE_SHA256:
        raise ValueError("Coverage Recovery preflight manifest package hash is not pinned")
    if manifest.get("private_index_sha256") != EXPECTED_PREFLIGHT_INDEX_SHA256:
        raise ValueError("Coverage Recovery preflight manifest index hash is not pinned")
    if manifest.get("blind_seed") != EXPECTED_PREFLIGHT_BLIND_SEED or manifest.get("case_count") != EXPECTED_RECORD_COUNT:
        raise ValueError("Coverage Recovery preflight manifest differs from the approved forty-case package")

    raw_index = _read_json_object(PREFLIGHT_INDEX_PATH)
    if not all(isinstance(case_id, str) and isinstance(seed_id, str) for case_id, seed_id in raw_index.items()):
        raise ValueError("Coverage Recovery preflight private index is malformed")
    case_to_seed = {str(case_id): str(seed_id) for case_id, seed_id in raw_index.items()}
    if len(case_to_seed) != EXPECTED_RECORD_COUNT or set(case_to_seed.values()) != set(source_plans):
        raise ValueError("Coverage Recovery preflight private index does not cover the source scope exactly")

    cases_by_id: dict[str, dict[str, Any]] = {}
    for case in _read_jsonl(PREFLIGHT_CASES_PATH):
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id in cases_by_id:
            raise ValueError("Coverage Recovery preflight cases have a missing or duplicate case_id")
        cases_by_id[case_id] = case
    if set(cases_by_id) != set(case_to_seed):
        raise ValueError("Coverage Recovery preflight cases and private index have different case IDs")
    for case_id, seed_id in case_to_seed.items():
        _assert_preflight_case_matches_source(cases_by_id[case_id], source_plans[seed_id], source_targets[seed_id], case_id=case_id)

    all_selected_case_ids = FORMAL_ADDRESS_CASE_IDS | DUPLICATE_SUBJECT_HEADING_CASE_IDS
    if len(FORMAL_ADDRESS_CASE_IDS) != EXPECTED_ADDRESS_REPAIR_COUNT:
        raise ValueError("formal repair case constant count differs from the approved scope")
    if len(DUPLICATE_SUBJECT_HEADING_CASE_IDS) != EXPECTED_HEADING_REPAIR_COUNT:
        raise ValueError("heading repair case constant count differs from the approved scope")
    if len(FORMAL_ADDRESS_CASE_IDS & DUPLICATE_SUBJECT_HEADING_CASE_IDS) != EXPECTED_OVERLAP_COUNT:
        raise ValueError("repair case overlap differs from the approved scope")
    if len(all_selected_case_ids) != EXPECTED_CHANGED_SEED_COUNT or not all_selected_case_ids.issubset(case_to_seed):
        raise ValueError("repair case constants do not resolve to the approved scope")

    scope = RepairScope(
        address_seed_ids=frozenset(case_to_seed[case_id] for case_id in FORMAL_ADDRESS_CASE_IDS),
        heading_seed_ids=frozenset(case_to_seed[case_id] for case_id in DUPLICATE_SUBJECT_HEADING_CASE_IDS),
    )
    if (
        len(scope.address_seed_ids) != EXPECTED_ADDRESS_REPAIR_COUNT
        or len(scope.heading_seed_ids) != EXPECTED_HEADING_REPAIR_COUNT
        or len(scope.overlap_seed_ids) != EXPECTED_OVERLAP_COUNT
        or len(scope.changed_seed_ids) != EXPECTED_CHANGED_SEED_COUNT
    ):
        raise ValueError("repair seed scope differs from the approved preflight mapping")
    for seed_id in sorted(scope.address_seed_ids):
        _assert_formal_source_case(source_plans[seed_id], source_targets[seed_id])
    for seed_id in sorted(scope.heading_seed_ids):
        _assert_heading_source_case(source_plans[seed_id], source_targets[seed_id])
    return scope


def repair_plan(source_plan: Mapping[str, Any], flags: RepairFlags, *, prompt_hash: str) -> dict[str, Any]:
    """Return one repaired plan, changing only an approved closed repair class."""

    plan = copy.deepcopy(dict(source_plan))
    if not flags.changed:
        return validate_seed_plan(plan)

    plan["generator_agent"] = PLAN_REPAIR_AGENT
    plan["batch_id"] = PLAN_REPAIR_BATCH
    plan["prompt_hash"] = prompt_hash
    if flags.formal_address:
        if plan["address_mode"] != "formal_sie":
            raise ValueError(f"{plan['seed_id']}: formal repair cannot alter a non-formal plan")
        fact = _fact_by_id(plan, "f3")
        if fact["text"] != FORMAL_SOURCE_FACT_TEXT:
            raise ValueError(f"{plan['seed_id']}: formal repair fact does not match the closed source text")
        fact["text"] = FORMAL_REPAIRED_FACT_TEXT
        plan["address_mode"] = "personal_du_capitalized"
    if flags.duplicate_subject_heading:
        levels = plan["semantic_plan"]["structure"]["heading_levels"]
        if not levels:
            raise ValueError(f"{plan['seed_id']}: heading repair cannot remove an absent heading")
        plan["semantic_plan"]["structure"]["heading_levels"] = []
    return validate_seed_plan(plan)


def _replace_block_text(block: Block, source: str, repaired: str) -> Block:
    if block.text != source:
        raise ValueError("block text does not match the closed source surface")
    if not block.inlines:
        return replace(block, text=repaired)
    if len(block.inlines) != 1 or block.inlines[0].text != source:
        raise ValueError("cannot safely repair a multi-part inline source block")
    return replace(block, text=repaired, inlines=(Inline(repaired, block.inlines[0].style),))


def repair_document(source_document: Document, source_plan: Mapping[str, Any], flags: RepairFlags) -> Document:
    """Apply exactly the same approved scope to the canonical AST."""

    blocks = list(source_document.blocks)
    if flags.formal_address:
        matching_indices = [
            index
            for index, block in enumerate(blocks)
            if block.type is BlockType.PARAGRAPH and block.text == FORMAL_SOURCE_FACT_TEXT
        ]
        if len(matching_indices) != 1:
            raise ValueError(f"{source_plan['seed_id']}: expected one formal source paragraph in target AST")
        index = matching_indices[0]
        blocks[index] = _replace_block_text(blocks[index], FORMAL_SOURCE_FACT_TEXT, FORMAL_REPAIRED_FACT_TEXT)
    if flags.duplicate_subject_heading:
        subjects = [block for block in blocks if block.type is BlockType.SUBJECT]
        if len(subjects) != 1 or subjects[0].text != source_plan["document_type"]:
            raise ValueError(f"{source_plan['seed_id']}: expected one unchanged source subject")
        redundant_indices = [
            index
            for index, block in enumerate(blocks)
            if block.type in {BlockType.HEADING_1, BlockType.HEADING_2} and block.text == subjects[0].text
        ]
        if len(redundant_indices) != 1:
            raise ValueError(f"{source_plan['seed_id']}: expected one redundant subject heading")
        del blocks[redundant_indices[0]]
    return Document(blocks=tuple(blocks))


def _assert_record_matches_plan(record: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    seed_id = str(plan["seed_id"])
    if record["semantic_plan"] != plan["semantic_plan"]:
        raise ValueError(f"{seed_id}: repaired target semantic plan differs from repaired plan")
    if record["semantic_plan_sha256"] != _semantic_plan_hash(plan):
        raise ValueError(f"{seed_id}: repaired target semantic plan hash differs from repaired plan")
    for field in (
        "domain",
        "document_type",
        "risk_tags",
        "language",
        "style_profile",
        "address_mode",
        "legal_citation_style",
        "unit_style",
    ):
        if record[field] != plan[field]:
            raise ValueError(f"{seed_id}: repaired target {field} differs from repaired plan")
    if record["source_plan_batch"] != plan["batch_id"] or record["plan_generator_agent"] != plan["generator_agent"]:
        raise ValueError(f"{seed_id}: repaired target provenance differs from repaired plan")

    document = document_from_dict(record["target_ast"])
    parsed = parse_sst(record["target_sst"])
    if render_sst(parsed) != render_sst(document):
        raise ValueError(f"{seed_id}: repaired target does not round-trip AST/SST/AST")
    plain_text = record["target_plain_text"]
    cursor = 0
    for fact in sorted(plan["semantic_plan"]["facts"], key=lambda item: item["order"]):
        position = plain_text.find(fact["text"], cursor)
        if position < cursor:
            raise ValueError(f"{seed_id}: repaired target loses or reorders fact {fact['fact_id']}")
        cursor = position + len(fact["text"])
        for protected_value in fact["protected_values"]:
            if protected_value not in fact["text"]:
                raise ValueError(f"{seed_id}: fact {fact['fact_id']} loses protected value {protected_value!r}")
    heading_levels = [
        "h1" if block.type is BlockType.HEADING_1 else "h2"
        for block in document.blocks
        if block.type in {BlockType.HEADING_1, BlockType.HEADING_2}
    ]
    if heading_levels != plan["semantic_plan"]["structure"]["heading_levels"]:
        raise ValueError(f"{seed_id}: repaired target heading structure differs from repaired plan")


def make_record(
    source_target: Mapping[str, Any],
    source_plan: Mapping[str, Any],
    repaired_plan: Mapping[str, Any],
    flags: RepairFlags,
    *,
    prompt_hash: str,
) -> dict[str, Any]:
    """Repair a canonical target AST and derive every projection from it."""

    source_document = document_from_dict(source_target["target_ast"])
    document = repair_document(source_document, source_plan, flags)
    seed_id = str(repaired_plan["seed_id"])
    canonical_document_id = f"coveragefix_{seed_id}"
    record = copy.deepcopy(dict(source_target))
    record["canonical_document_id"] = canonical_document_id
    record["parent_canonical_document_id"] = canonical_document_id
    record["canonical_generator_id"] = "local_canonical_expander"
    record["canonical_generator_version"] = "1"
    record["variant_index"] = 1
    record["source_plan_batch"] = repaired_plan["batch_id"]
    record["plan_generator_agent"] = repaired_plan["generator_agent"]
    record["target_writer_agent"] = WRITER_ID
    record["target_prompt_hash"] = prompt_hash
    record["semantic_plan"] = copy.deepcopy(repaired_plan["semantic_plan"])
    record["semantic_plan_sha256"] = _semantic_plan_hash(repaired_plan)
    for field in (
        "domain",
        "document_type",
        "risk_tags",
        "language",
        "style_profile",
        "address_mode",
        "legal_citation_style",
        "unit_style",
    ):
        record[field] = copy.deepcopy(repaired_plan[field])
    record["protected_spans"] = copy.deepcopy(source_target["protected_spans"])
    record["target_ast"] = document_to_dict(document)
    record["target_sst"] = render_sst(document)
    record["target_plain_text"] = render_plain_text(document)
    record["target_markdown"] = render_markdown(document)
    record["target_html"] = render_html(document)
    record["source_text"] = record["target_plain_text"]
    validated = validate_canonical_record(record)
    _assert_record_matches_plan(validated, repaired_plan)
    return validated


def _assert_plan_change_scope(
    source_plan: Mapping[str, Any],
    repaired_plan: Mapping[str, Any],
    flags: RepairFlags,
    *,
    prompt_hash: str,
) -> None:
    seed_id = str(source_plan["seed_id"])
    if not flags.changed:
        if canonical_json(source_plan) != canonical_json(repaired_plan):
            raise ValueError(f"{seed_id}: unreviewed source plan changed")
        return
    expected = repair_plan(source_plan, flags, prompt_hash=prompt_hash)
    if canonical_json(expected) != canonical_json(repaired_plan):
        raise ValueError(f"{seed_id}: repaired plan differs from the closed repair transform")
    if source_plan["semantic_plan"]["entities"] != repaired_plan["semantic_plan"]["entities"]:
        raise ValueError(f"{seed_id}: repair changed entities")
    for source_fact, repaired_fact in zip(
        source_plan["semantic_plan"]["facts"], repaired_plan["semantic_plan"]["facts"], strict=True
    ):
        if flags.formal_address and source_fact["fact_id"] == "f3":
            expected_fact = copy.deepcopy(source_fact)
            expected_fact["text"] = FORMAL_REPAIRED_FACT_TEXT
            if repaired_fact != expected_fact:
                raise ValueError(f"{seed_id}: formal repair changed more than the approved fact surface")
        elif repaired_fact != source_fact:
            raise ValueError(f"{seed_id}: repair changed an unrelated fact")
    source_structure = source_plan["semantic_plan"]["structure"]
    repaired_structure = repaired_plan["semantic_plan"]["structure"]
    expected_structure = copy.deepcopy(source_structure)
    if flags.duplicate_subject_heading:
        expected_structure["heading_levels"] = []
    if repaired_structure != expected_structure:
        raise ValueError(f"{seed_id}: repair changed structure outside the approved heading removal")


def _assert_target_change_scope(
    source_target: Mapping[str, Any],
    source_plan: Mapping[str, Any],
    repaired_target: Mapping[str, Any],
    flags: RepairFlags,
) -> None:
    seed_id = str(source_plan["seed_id"])
    source_document = document_from_dict(source_target["target_ast"])
    expected_document = repair_document(source_document, source_plan, flags)
    if repaired_target["target_ast"] != document_to_dict(expected_document):
        raise ValueError(f"{seed_id}: repaired target AST differs from the closed repair transform")
    if repaired_target["protected_spans"] != source_target["protected_spans"]:
        raise ValueError(f"{seed_id}: repair changed protected span inventory")
    if not flags.changed:
        for field in ("target_ast", "target_sst", "target_plain_text", "target_markdown", "target_html"):
            if repaired_target[field] != source_target[field]:
                raise ValueError(f"{seed_id}: unreviewed target {field} changed")


def _validate_expansions(
    plans: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    expanded_records: list[dict[str, Any]] = []
    per_target_hashes: list[str] = []
    for plan, record in zip(plans, records, strict=True):
        first = expand_canonical_variants(plan, record)
        second = expand_canonical_variants(plan, record)
        first_bytes = _jsonl_bytes(first)
        second_bytes = _jsonl_bytes(second)
        if len(first) != VARIANT_COUNT or len(second) != VARIANT_COUNT:
            raise ValueError(f"{plan['seed_id']}: canonical expander did not produce seven variants")
        if first_bytes != second_bytes:
            raise ValueError(f"{plan['seed_id']}: canonical expansion is not byte-deterministic")
        if len({variant["canonical_document_id"] for variant in first}) != VARIANT_COUNT:
            raise ValueError(f"{plan['seed_id']}: canonical expansion has duplicate variant IDs")
        if len({variant["target_sst"] for variant in first}) != VARIANT_COUNT:
            raise ValueError(f"{plan['seed_id']}: canonical expansion has duplicate SST variants")
        for variant in first:
            validate_canonical_record(variant)
        expanded_records.extend(first)
        per_target_hashes.append(sha256_prefixed(first_bytes))
    all_bytes = _jsonl_bytes(expanded_records)
    if len(expanded_records) != EXPECTED_EXPANDED_RECORD_COUNT:
        raise ValueError("canonical expansion count differs from 40 × 7")
    return {
        "variants_per_target": VARIANT_COUNT,
        "validated_target_count": len(records),
        "validated_variant_count": len(expanded_records),
        "all_seven_variants_validated": len(expanded_records) == len(records) * VARIANT_COUNT,
        "all_variants_unique_per_target": True,
        "double_expansion_byte_identical": True,
        "expanded_records_sha256": sha256_prefixed(all_bytes),
        "per_target_expansion_sha256": per_target_hashes,
    }


def _assigned_input_batches() -> list[dict[str, str]]:
    paths = (
        SOURCE_PLAN_PATH,
        SOURCE_PLAN_MANIFEST_PATH,
        SOURCE_TARGET_PATH,
        SOURCE_TARGET_MANIFEST_PATH,
        PREFLIGHT_CASES_PATH,
        PREFLIGHT_MANIFEST_PATH,
        PREFLIGHT_INDEX_PATH,
        HANDOFF_PATH,
        PROJECT_ROOT / "src" / "scriber_polishing" / "ast_codec.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "canonical_expander.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "sst_renderer.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "target_renderer.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "validators.py",
    )
    return [
        {"path": repository_relative(path), "sha256": sha256_prefixed(path.read_bytes())}
        for path in paths
    ]


def build() -> BuildResult:
    source_plans, source_targets = _load_source_inputs()
    scope = load_repair_scope(source_plans, source_targets)
    prompt_hash = sha256_prefixed(HANDOFF_PATH.read_bytes())

    plans: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    changed_plan_count = 0
    changed_ast_count = 0
    changed_sst_count = 0
    changed_plain_text_count = 0
    changed_markdown_count = 0
    changed_html_count = 0
    changed_address_mode_count = 0
    changed_heading_structure_count = 0
    for seed_id in sorted(source_plans):
        source_plan = source_plans[seed_id]
        source_target = source_targets[seed_id]
        flags = scope.flags_for(seed_id)
        repaired_plan = repair_plan(source_plan, flags, prompt_hash=prompt_hash)
        repaired_target = make_record(source_target, source_plan, repaired_plan, flags, prompt_hash=prompt_hash)
        _assert_plan_change_scope(source_plan, repaired_plan, flags, prompt_hash=prompt_hash)
        _assert_target_change_scope(source_target, source_plan, repaired_target, flags)

        changed_plan_count += canonical_json(repaired_plan) != canonical_json(source_plan)
        changed_ast_count += repaired_target["target_ast"] != source_target["target_ast"]
        changed_sst_count += repaired_target["target_sst"] != source_target["target_sst"]
        changed_plain_text_count += repaired_target["target_plain_text"] != source_target["target_plain_text"]
        changed_markdown_count += repaired_target["target_markdown"] != source_target["target_markdown"]
        changed_html_count += repaired_target["target_html"] != source_target["target_html"]
        changed_address_mode_count += repaired_plan["address_mode"] != source_plan["address_mode"]
        changed_heading_structure_count += (
            repaired_plan["semantic_plan"]["structure"]["heading_levels"]
            != source_plan["semantic_plan"]["structure"]["heading_levels"]
        )
        plans.append(repaired_plan)
        records.append(repaired_target)

    if len(plans) != EXPECTED_RECORD_COUNT or len(records) != EXPECTED_RECORD_COUNT:
        raise ValueError("repair did not produce exactly forty plans and targets")
    if (
        changed_plan_count != EXPECTED_CHANGED_SEED_COUNT
        or changed_ast_count != EXPECTED_CHANGED_SEED_COUNT
        or changed_sst_count != EXPECTED_CHANGED_SEED_COUNT
        or changed_plain_text_count != EXPECTED_CHANGED_SEED_COUNT
        or changed_markdown_count != EXPECTED_CHANGED_SEED_COUNT
        or changed_html_count != EXPECTED_CHANGED_SEED_COUNT
        or changed_address_mode_count != EXPECTED_ADDRESS_REPAIR_COUNT
        or changed_heading_structure_count != EXPECTED_HEADING_REPAIR_COUNT
    ):
        raise ValueError("repair output change counts differ from the approved closed scope")

    plans_bytes = _jsonl_bytes(plans)
    targets_bytes = _jsonl_bytes(records)
    expansion = _validate_expansions(plans, records)
    implementation_hash = sha256_prefixed(Path(__file__).read_bytes())
    changed_seed_ids = sorted(scope.changed_seed_ids)
    address_seed_ids = sorted(scope.address_seed_ids)
    heading_seed_ids = sorted(scope.heading_seed_ids)
    overlap_seed_ids = sorted(scope.overlap_seed_ids)
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "writer_version": WRITER_VERSION,
        "target_prompt_hash": prompt_hash,
        "record_count": len(records),
        "plan_count": len(plans),
        "repaired_plan_count": changed_plan_count,
        "repaired_target_surface_count": changed_plain_text_count,
        "rejected_seed_count": 0,
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in records}),
        "plans_sha256": sha256_prefixed(plans_bytes),
        "targets_sha256": sha256_prefixed(targets_bytes),
        "repair_writer_sha256": implementation_hash,
        "assigned_input_batches": _assigned_input_batches(),
        "source_packages": {
            "plans_sha256": sha256_prefixed(SOURCE_PLAN_PATH.read_bytes()),
            "targets_sha256": sha256_prefixed(SOURCE_TARGET_PATH.read_bytes()),
            "plan_manifest_sha256": sha256_prefixed(SOURCE_PLAN_MANIFEST_PATH.read_bytes()),
            "target_manifest_sha256": sha256_prefixed(SOURCE_TARGET_MANIFEST_PATH.read_bytes()),
        },
        "preflight_scope": {
            "case_package_sha256": sha256_prefixed(PREFLIGHT_CASES_PATH.read_bytes()),
            "private_index_sha256": sha256_prefixed(PREFLIGHT_INDEX_PATH.read_bytes()),
            "rejected_case_count": EXPECTED_CHANGED_SEED_COUNT,
            "address_case_count": EXPECTED_ADDRESS_REPAIR_COUNT,
            "duplicate_subject_heading_case_count": EXPECTED_HEADING_REPAIR_COUNT,
            "overlap_case_count": EXPECTED_OVERLAP_COUNT,
            "changed_seed_count": len(changed_seed_ids),
            "address_seed_set_sha256": _scope_hash(scope.address_seed_ids),
            "heading_seed_set_sha256": _scope_hash(scope.heading_seed_ids),
            "changed_seed_set_sha256": _scope_hash(scope.changed_seed_ids),
        },
        "change_scope": {
            "changed_seed_ids": changed_seed_ids,
            "address_repair_seed_ids": address_seed_ids,
            "heading_repair_seed_ids": heading_seed_ids,
            "overlap_seed_ids": overlap_seed_ids,
            "unchanged_seed_count": EXPECTED_RECORD_COUNT - len(changed_seed_ids),
            "formal_address_mode_changes": changed_address_mode_count,
            "exact_fact_text_changes": changed_address_mode_count,
            "redundant_heading_block_removals": changed_heading_structure_count,
            "semantic_plan_changes": changed_plan_count,
            "ast_changes": changed_ast_count,
            "sst_changes": changed_sst_count,
            "plain_text_changes": changed_plain_text_count,
            "markdown_changes": changed_markdown_count,
            "html_changes": changed_html_count,
            "all_unreviewed_plans_byte_identical": True,
            "all_unreviewed_target_asts_byte_identical": True,
            "all_entities_preserved": True,
            "all_protected_values_preserved": True,
        },
        "canonical_expansion": expansion,
        "generation_boundary": {
            "immutable_source_packages": True,
            "preflight_scope_only": True,
            "external_or_model_api_calls": 0,
            "no_external_corpus": True,
            "no_training_or_evaluation_split_read": True,
        },
        "deterministic_serialization": {
            "record_order": "seed_id ascending",
            "jsonl": "utf-8 compact sorted JSON with one final LF",
            "timestamps_omitted": True,
        },
    }
    manifest_bytes = _pretty_json_bytes(manifest)
    validation: dict[str, Any] = {
        "schema_version": 1,
        "passed": True,
        "source_plan_count": len(source_plans),
        "source_target_count": len(source_targets),
        "output_plan_count": len(plans),
        "output_target_count": len(records),
        "all_plans_schema_valid": True,
        "all_targets_canonical_valid": True,
        "ast_sst_ast_round_trip_count": len(records),
        "renderer_consistency_count": len(records),
        "all_protected_spans_present": True,
        "all_entities_preserved": True,
        "all_protected_values_preserved": True,
        "exact_change_scope": {
            "rejected_case_count": EXPECTED_CHANGED_SEED_COUNT,
            "changed_seed_count": len(changed_seed_ids),
            "overlap_seed_count": len(overlap_seed_ids),
            "address_fixes": changed_address_mode_count,
            "heading_fixes": changed_heading_structure_count,
            "all_unreviewed_plans_byte_identical": True,
            "all_unreviewed_target_asts_byte_identical": True,
        },
        "canonical_expander": expansion,
        "determinism": {
            "double_build_byte_identical": True,
            "plans_sha256": manifest["plans_sha256"],
            "targets_sha256": manifest["targets_sha256"],
            "external_or_model_api_calls": 0,
        },
        "output_sha256": {
            "repair_writer.py": implementation_hash,
            "plans.jsonl": manifest["plans_sha256"],
            "targets.jsonl": manifest["targets_sha256"],
            "manifest.json": sha256_prefixed(manifest_bytes),
        },
    }
    validation_bytes = _pretty_json_bytes(validation)
    report = "\n".join(
        (
            "# Coverage Recovery v1.01 repair",
            "",
            f"- Repaired plans and targets: `{len(records)}`",
            f"- Preflight rejected cases: `{EXPECTED_CHANGED_SEED_COUNT}`",
            f"- Changed seeds: `{len(changed_seed_ids)}`",
            f"- Address repairs: `{changed_address_mode_count}`",
            f"- Duplicate subject/heading repairs: `{changed_heading_structure_count}`",
            f"- Overlapping repairs: `{len(overlap_seed_ids)}`",
            f"- Unchanged source plans: `{EXPECTED_RECORD_COUNT - len(changed_seed_ids)}`",
            f"- Canonical expansions validated: `{expansion['validated_variant_count']}`",
            f"- Plans SHA-256: `{manifest['plans_sha256']}`",
            f"- Targets SHA-256: `{manifest['targets_sha256']}`",
            f"- Expanded variants SHA-256: `{expansion['expanded_records_sha256']}`",
            f"- Repair writer SHA-256: `{implementation_hash}`",
            "",
            "All fourteen semantic changes are pinned to the supplied blinded preflight mapping. "
            "Seven formal records now use `personal_du_capitalized` and retain the distinct "
            "`Seit`/`seid` spelling while capitalizing only the direct-address `Ihr`. Ten records "
            "retain their subject and remove only its duplicate heading block; three records receive "
            "both repairs. Every SST, plain-text, Markdown, and HTML field was rendered from the "
            "repaired canonical AST. The deterministic rebuild validates all 40 plans and targets, "
            "then all 280 seven-variant canonical expansions.",
            "",
            "## Changed seed scope",
            "",
            f"- Address: {', '.join(address_seed_ids)}",
            f"- Heading: {', '.join(heading_seed_ids)}",
            f"- Both: {', '.join(overlap_seed_ids)}",
            "",
        )
    )
    return BuildResult(
        plans=tuple(plans),
        records=tuple(records),
        plans_bytes=plans_bytes,
        targets_bytes=targets_bytes,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        validation=validation,
        validation_bytes=validation_bytes,
        report=report,
        report_bytes=report.encode("utf-8"),
    )


def _assert_byte_deterministic(first: BuildResult, second: BuildResult) -> None:
    for label, left, right in (
        ("plans.jsonl", first.plans_bytes, second.plans_bytes),
        ("targets.jsonl", first.targets_bytes, second.targets_bytes),
        ("manifest.json", first.manifest_bytes, second.manifest_bytes),
        ("validation.json", first.validation_bytes, second.validation_bytes),
        ("report.md", first.report_bytes, second.report_bytes),
    ):
        if left != right:
            raise ValueError(f"deterministic double build differs for {label}")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def write_build(build: BuildResult) -> None:
    _write_bytes(PLANS_PATH, build.plans_bytes)
    _write_bytes(TARGETS_PATH, build.targets_bytes)
    _write_bytes(MANIFEST_PATH, build.manifest_bytes)
    _write_bytes(VALIDATION_PATH, build.validation_bytes)
    _write_bytes(REPORT_PATH, build.report_bytes)


def verify_written_build(build: BuildResult) -> None:
    expected = {
        PLANS_PATH: build.plans_bytes,
        TARGETS_PATH: build.targets_bytes,
        MANIFEST_PATH: build.manifest_bytes,
        VALIDATION_PATH: build.validation_bytes,
        REPORT_PATH: build.report_bytes,
    }
    for path, payload in expected.items():
        if not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"written repair output differs from deterministic build: {path.name}")
    summary, errors = _validate_batch(OUTPUT_DIR, PROJECT_ROOT, set(), set())
    if errors or summary is None:
        raise ValueError("target-batch validation failed: " + "; ".join(errors))
    if summary.record_count != EXPECTED_RECORD_COUNT or summary.writer_id != WRITER_ID:
        raise ValueError("target-batch validator summary differs from the repaired package")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify existing generated outputs without rewriting")
    arguments = parser.parse_args()
    first = build()
    second = build()
    _assert_byte_deterministic(first, second)
    if not arguments.check:
        write_build(first)
    verify_written_build(first)
    print(
        canonical_json(
            {
                "status": "complete",
                "plans": len(first.plans),
                "targets": len(first.records),
                "expanded_variants": first.manifest["canonical_expansion"]["validated_variant_count"],
                "plans_sha256": first.manifest["plans_sha256"],
                "targets_sha256": first.manifest["targets_sha256"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
