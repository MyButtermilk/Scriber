#!/usr/bin/env python3
"""Build the immutable safe-scope adversarial repair view after style v5.01."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

OUTPUT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = OUTPUT_DIR.parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scriber_polishing.adversarial_plan_repair import (  # noqa: E402
    bind_feedback_list_to_obligation_due_date,
    localize_german_structure,
    mark_uncertain_list_date,
    naturalize_technical_compound,
    paragraphize_single_unchanged_item,
    preserve_feedback_position_role,
    promote_explicit_named_actors,
    restore_lexical_fictionality,
)
from scriber_polishing.ast_codec import document_from_dict, document_to_dict  # noqa: E402
from scriber_polishing.canonical_expander import (  # noqa: E402
    VARIANT_COUNT,
    expand_canonical_variants,
)
from scriber_polishing.condition_realization import (  # noqa: E402
    integrate_appended_condition_commentary,
    remove_redundant_condition_restatement,
)
from scriber_polishing.document_ast import BlockType, Document  # noqa: E402
from scriber_polishing.schemas import (  # noqa: E402
    validate_critic_verdict,
    validate_seed_plan,
)
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_batch_validator import _validate_batch  # noqa: E402
from scriber_polishing.target_renderer import (  # noqa: E402
    render_html,
    render_markdown,
    render_plain_text,
)
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

WRITER_ID = "target_repair_adversarial_v5_02"
WRITER_VERSION = 1
PLAN_REPAIR_AGENT = "plan_repair_adversarial_v5_02"
PLAN_REPAIR_BATCH = "adversarial_plan_repair_v5_02"

EXPECTED_SOURCE_RECORDS = 2625
EXPECTED_REVIEW_COUNT = 2484
EXPECTED_REVIEWED_SEED_COUNT = 2484
EXPECTED_PACKAGE_SHA256 = "5afe3dfcc8c99dbc6ce4634d676438d3186098940b9b7aff6611c682a1ca9ae4"
EXPECTED_REVIEWS_SHA256 = "8e4df768ce7ba2037666328fea7440c18e4ec2cd7e4fdf8ce5937946fa074901"
EXPECTED_SUMMARY_SHA256 = "1e7e49e6e1eded9baa199ced98945322afd214f19b18170bace1f0ca22e778fa"
EXPECTED_INDEX_SHA256 = "244cb57640252f5169a157baef14d1b740bde60aac84ff6fd55369754e3ec5b6"
EXPECTED_SOURCE_PLANS_SHA256 = "db0fcd8c8b2a620d8f96aa7dbf27fc6932a64f967adc712aaebe597e19ca6832"
EXPECTED_SOURCE_TARGETS_SHA256 = "b5295b7bee779740dd54ceff14dee06a95580b3cb9a9fc5bb0ffec6788b786e7"
EXPECTED_SOURCE_MANIFEST_SHA256 = "2a60acecf3f58622bcb693ce4896a7dca2e97df62f381d6cc955134c2ae58688"
EXPECTED_CRITIC_SOURCE_SHA256 = "5a72a1702f2f66dce1b0119921a9cb789704950081714fe943b895d7ac5919a8"
EXPECTED_BEHAVIORAL_CONTRACT_SHA256 = "c416dcd0617529e754c734baeb73ca63718ac2a8fa12f0cc8014e9fa7fe498fe"
EXPECTED_STYLE_CONTRACT_SHA256 = "02f4ee40467c3643ec2447d17c5a44dec54f9fc55495b1c15d52a84162917dfd"
EXPECTED_VERDICT_SCHEMA_SHA256 = "c405c91b8448bcaf45a7c6012be10bdb86c45debce63b853bd2cffdcc033cb42"

EXPECTED_FLAG_COUNTS = {
    "artificial_condition_template": 549,
    "awkward_mixed_language_compound": 21,
    "conflicting_deadline": 200,
    "fictionality_marker_omitted": 1500,
    "html_nbsp_contract_violation": 175,
    "output_language_changed": 105,
    "pronoun_reference_error": 33,
    "redundant_condition_restated": 11,
    "semantic_role_weakened": 206,
    "uncertainty_contradicted_by_deadline": 20,
    "unjustified_single_item_list": 150,
}
EXPECTED_SCOPE_SHA256 = {
    "artificial_condition_template": "387d49d5628e2b53b55040d67f5788aed0eed3646a35d1b5733c57905c2133a2",
    "awkward_mixed_language_compound": "87c7de1a651d84dc7b6624da6719f94c73b262ab75c34c451516b070ee78b34e",
    "conflicting_deadline": "02f42b0326fc9831116ea01098f4c5601b4749c906a60db6c3659f0a59c33caf",
    "fictionality_marker_omitted": "a72993d98b13cf7e10a12a86b4a00e0682cb0e0e26189aeffa833f380e4c8fbf",
    "html_nbsp_contract_violation": "60ae987ea6004ed89a80a213d9ba788eb25ed926ff1fe9ef8033a483d95f05aa",
    "output_language_changed": "007240efcc9754a4e3e56f693adbe1a953823b149642cf123ed3bdbf3c5668f5",
    "pronoun_reference_error": "4211e6edf0e3f53bbd1c43c5e347f5bae94849d503773d5675a8eb2194ef7c61",
    "redundant_condition_restated": "abcc347e4cb5bee91fdf449deaf448c08a1d39de89896e6823ab2683847be89a",
    "semantic_role_weakened": "e798ed80be802a5b335bca70f8ac302ddf00975cc1871a48ba6b91935ab9e9a4",
    "uncertainty_contradicted_by_deadline": "bc1325f827bd1866ea4bd624566bb1416d66a363defb0c803b14641f4f5dc30d",
    "unjustified_single_item_list": "d9927cf48216b97e1cb69d7f9871ca63af11d48d4c17aba8783681eefcf2a55e",
}
EXPECTED_NONZERO_OVERLAPS = {
    ("artificial_condition_template", "conflicting_deadline"): 200,
    ("artificial_condition_template", "fictionality_marker_omitted"): 354,
    ("artificial_condition_template", "html_nbsp_contract_violation"): 6,
    ("artificial_condition_template", "output_language_changed"): 20,
    ("artificial_condition_template", "unjustified_single_item_list"): 30,
    ("awkward_mixed_language_compound", "fictionality_marker_omitted"): 21,
    ("conflicting_deadline", "fictionality_marker_omitted"): 200,
    ("fictionality_marker_omitted", "html_nbsp_contract_violation"): 175,
    ("fictionality_marker_omitted", "output_language_changed"): 105,
    ("fictionality_marker_omitted", "semantic_role_weakened"): 206,
    ("fictionality_marker_omitted", "uncertainty_contradicted_by_deadline"): 20,
    ("html_nbsp_contract_violation", "output_language_changed"): 20,
    ("html_nbsp_contract_violation", "semantic_role_weakened"): 39,
    ("output_language_changed", "uncertainty_contradicted_by_deadline"): 10,
}
TRANSFORM_FLAGS = frozenset(
    {
        "artificial_condition_template",
        "awkward_mixed_language_compound",
        "conflicting_deadline",
        "fictionality_marker_omitted",
        "output_language_changed",
        "pronoun_reference_error",
        "redundant_condition_restated",
        "semantic_role_weakened",
        "uncertainty_contradicted_by_deadline",
        "unjustified_single_item_list",
    }
)
SOURCE_RESOLVED_FLAGS = frozenset({"html_nbsp_contract_violation"})
ALLOWED_FLAGS = TRANSFORM_FLAGS | SOURCE_RESOLVED_FLAGS

EXPECTED_REMAINING_CONDITION_SEEDS = 484
EXPECTED_REMAINING_CONDITION_OCCURRENCES = 1079
EXPECTED_REMAINING_CONDITION_SCOPE_SHA256 = (
    "81cff5a172ce4c90cc74c297b6681d6c6625d904587e2548bfdc31d9f6bb18e3"
)
EXPECTED_ALREADY_REMOVED_CONDITION_SEEDS = 65
EXPECTED_ALREADY_REMOVED_CONDITION_SCOPE_SHA256 = (
    "952fd90d3dee3266f8621624bccb2e939248bdbb2367f5aa7a49c39b30a4b112"
)
EXPECTED_FICTION_MARKER_OCCURRENCES = 3665
EXPECTED_PRONOUN_CHANGED_FACT_COUNT = 72
EXPECTED_PRONOUN_PROTECTED_VALUE_REMOVALS = 66
EXPECTED_REPAIRED_PLAN_COUNT = 498
EXPECTED_CHANGED_TARGET_COUNT = 1844
EXPECTED_ORIGINAL_REJECT_COUNT = 1859
EXPECTED_SOURCE_RESOLVED_ONLY_REJECT_COUNT = 15
EXPECTED_SOURCE_RESOLVED_ONLY_REJECT_SCOPE_SHA256 = (
    "5886088588c92a6ce69feee184304070c6c36275e12594042584669ad3237993"
)
EXPECTED_CANONICAL_VARIANTS = EXPECTED_SOURCE_RECORDS * VARIANT_COUNT

SOURCE_DIR = PROJECT_ROOT / "data" / "targets" / "target_repair_style_v5_01"
SOURCE_PLANS_PATH = SOURCE_DIR / "plans.jsonl"
SOURCE_TARGETS_PATH = SOURCE_DIR / "targets.jsonl"
SOURCE_MANIFEST_PATH = SOURCE_DIR / "manifest.json"
REVIEW_DIR = PROJECT_ROOT / "artifacts" / "agent_reviews_v5" / "adversarial_sol_max_01"
REVIEWS_PATH = REVIEW_DIR / "reviews.jsonl"
SUMMARY_PATH = REVIEW_DIR / "summary.json"
CRITIC_SOURCE_PATH = REVIEW_DIR / "critic.py"
INDEX_PATH = PROJECT_ROOT / "artifacts" / "private" / "critic_package_v5_index.json"
HANDOFF_PATH = PROJECT_ROOT / "handoffs" / "AGENT_PLAN_TARGET_REPAIR_ADVERSARIAL_V5_02.md"
BEHAVIORAL_CONTRACT_PATH = PROJECT_ROOT / "contracts" / "behavioral_contract.yaml"
STYLE_CONTRACT_PATH = PROJECT_ROOT / "contracts" / "de_business_style.yaml"
VERDICT_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "critic_verdict_schema.json"
PLANS_PATH = OUTPUT_DIR / "plans.jsonl"
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
REPORT_PATH = OUTPUT_DIR / "repair_report.md"
VALIDATION_PATH = OUTPUT_DIR / "validation.json"

_FICTION_MARKER = re.compile(r"\b(?:fictional|fiktiv(?:e|en|er|es)?)\b", re.IGNORECASE)
_AWKWARD_COMPOUND = re.compile(
    r"\beine (?:API gateway|observability dashboard|rate limit|rollback window|"
    r"service-level objective|webhook signature)-Prüfung\b"
)
_REDUNDANT_RESTATEMENT = (
    "Die Prüfung ist vollständig, sodass die Freigabe erfolgen kann. "
    "Dies gilt, wenn die Prüfung abgeschlossen ist."
)
_STRUCTURAL_TYPES = frozenset(
    {BlockType.SUBJECT, BlockType.HEADING_1, BlockType.HEADING_2}
)
_UNLOCALIZED_GERMAN_STRUCTURE = (
    "Forecast ",
    "Customer ",
    "Billing ",
    "Agreed ",
    "Delivery ",
    "Release ",
    "Risk ",
    "Incident ",
    "Session ",
    "Supplier ",
    "Protected wording",
    "Protected references",
    "Next step",
    "Reference ",
    "Owner ",
    "Deadline ",
    "Sub-item: ",
    "Fictional business operations",
)
_PROMOTED_PRONOUN_PATTERNS = (
    re.compile(
        r"\b[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+ "
        r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+, damit (?:er|sie) Euch\b"
    ),
    re.compile(
        r"\b[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+ "
        r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+ hält fest, dass (?:er|sie) den Wortlaut\b"
    ),
)


@dataclass(frozen=True)
class BuildResult:
    plans: tuple[dict[str, Any], ...]
    records: tuple[dict[str, Any], ...]
    plans_bytes: bytes
    targets_bytes: bytes
    manifest: dict[str, Any]
    validation: dict[str, Any]
    report: str


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def repository_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"{path}: blank JSONL line {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}: JSONL line {line_number} is not an object")
        rows.append(value)
    return rows


def _load_by_seed(path: Path, expected_sha256: str) -> dict[str, dict[str, Any]]:
    if sha256_file(path) != expected_sha256:
        raise ValueError(f"{path}: source hash mismatch")
    rows = _load_jsonl(path)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        seed_id = row.get("seed_id")
        if not isinstance(seed_id, str) or seed_id in result:
            raise ValueError(f"{path}: missing or duplicate seed_id")
        result[seed_id] = row
    return result


def _scope_hash(seed_ids: set[str] | frozenset[str]) -> str:
    return sha256_bytes(("\n".join(sorted(seed_ids)) + "\n").encode("utf-8"))


def _validate_review_summary() -> None:
    expected_hashes = {
        SUMMARY_PATH: EXPECTED_SUMMARY_SHA256,
        CRITIC_SOURCE_PATH: EXPECTED_CRITIC_SOURCE_SHA256,
        BEHAVIORAL_CONTRACT_PATH: EXPECTED_BEHAVIORAL_CONTRACT_SHA256,
        STYLE_CONTRACT_PATH: EXPECTED_STYLE_CONTRACT_SHA256,
        VERDICT_SCHEMA_PATH: EXPECTED_VERDICT_SCHEMA_SHA256,
    }
    for path, expected in expected_hashes.items():
        if sha256_file(path) != expected:
            raise ValueError(f"{path}: pinned review or contract hash mismatch")
    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    if (
        summary.get("reviewed_package_sha256") != EXPECTED_PACKAGE_SHA256
        or summary.get("output_hash") != f"sha256:{EXPECTED_REVIEWS_SHA256}"
        or summary.get("counts", {}).get("reviewed") != EXPECTED_REVIEW_COUNT
        or summary.get("clean_room_confirmations", {}).get("generative_apis_used") is not False
        or summary.get("clean_room_confirmations", {}).get("model_outputs_read") is not False
        or summary.get("clean_room_confirmations", {}).get("seeds_read") is not False
        or summary.get("clean_room_confirmations", {}).get("splits_read") is not False
    ):
        raise ValueError("adversarial review summary differs from the pinned clean-room audit")
    materialized_counts = {
        **summary.get("flag_counts", {}).get("critical", {}),
        **summary.get("flag_counts", {}).get("noncritical", {}),
    }
    if materialized_counts != EXPECTED_FLAG_COUNTS:
        raise ValueError("adversarial summary flag counts differ from the pinned audit")


def _review_flags() -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    if sha256_file(REVIEWS_PATH) != EXPECTED_REVIEWS_SHA256:
        raise ValueError("adversarial review hash mismatch")
    if sha256_file(INDEX_PATH) != EXPECTED_INDEX_SHA256:
        raise ValueError("private critic index hash mismatch")
    _validate_review_summary()
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    if not isinstance(index, dict) or not all(
        isinstance(case_id, str) and isinstance(seed_id, str)
        for case_id, seed_id in index.items()
    ):
        raise ValueError("private critic index is malformed")
    if len(index) != EXPECTED_REVIEW_COUNT or len(set(index.values())) != EXPECTED_REVIEWED_SEED_COUNT:
        raise ValueError("private critic index does not cover the pinned unique seed scope")

    rows = [validate_critic_verdict(row) for row in _load_jsonl(REVIEWS_PATH)]
    if len(rows) != EXPECTED_REVIEW_COUNT:
        raise ValueError("adversarial review count mismatch")
    seen_cases: set[str] = set()
    by_seed: dict[str, frozenset[str]] = {}
    scopes: dict[str, set[str]] = {flag: set() for flag in ALLOWED_FLAGS}
    flag_counts: Counter[str] = Counter()
    for row in rows:
        case_id = row["case_id"]
        if case_id in seen_cases or case_id not in index:
            raise ValueError("adversarial review case is duplicate or absent from private index")
        seen_cases.add(case_id)
        if (
            row["reviewed_package_sha256"] != EXPECTED_PACKAGE_SHA256
            or row["critic_role"] != "adversarial"
        ):
            raise ValueError("adversarial verdict provenance differs from the pinned package")
        flags = frozenset([*row["critical_flags"], *row["noncritical_flags"]])
        if not flags.issubset(ALLOWED_FLAGS):
            raise ValueError("adversarial review contains an unsupported flag")
        if row["acceptable"] != (not flags):
            raise ValueError("adversarial verdict acceptance disagrees with its flags")
        seed_id = index[case_id]
        if seed_id in by_seed:
            raise ValueError("multiple adversarial cases map to one seed")
        by_seed[seed_id] = flags
        flag_counts.update(flags)
        for flag in flags:
            scopes[flag].add(seed_id)
    if seen_cases != set(index):
        raise ValueError("adversarial reviews do not cover the complete private index")
    if dict(sorted(flag_counts.items())) != EXPECTED_FLAG_COUNTS:
        raise ValueError("adversarial review flag counts differ from the pinned scope")
    scope_hashes = {flag: _scope_hash(seed_ids) for flag, seed_ids in scopes.items()}
    if scope_hashes != EXPECTED_SCOPE_SHA256:
        raise ValueError("adversarial review seed-set hash differs from the pinned audit")

    nonzero_overlaps: dict[tuple[str, str], int] = {}
    sorted_flags = sorted(scopes)
    for left_index, left in enumerate(sorted_flags):
        for right in sorted_flags[left_index + 1 :]:
            count = len(scopes[left].intersection(scopes[right]))
            if count:
                nonzero_overlaps[(left, right)] = count
    if nonzero_overlaps != EXPECTED_NONZERO_OVERLAPS:
        raise ValueError("adversarial review overlap matrix differs from the pinned audit")
    return by_seed, {flag: frozenset(seed_ids) for flag, seed_ids in scopes.items()}


def _semantic_plan_hash(plan: dict[str, Any]) -> str:
    payload = canonical_json(plan["semantic_plan"]).encode("utf-8")
    return "sha256:" + sha256_bytes(payload)


def _contains_surface(text: str, value: str) -> bool:
    if not value:
        return False
    prefix = r"(?<!\w)" if value[0].isalnum() else ""
    suffix = r"(?!\w)" if value[-1].isalnum() else ""
    return re.search(prefix + re.escape(value) + suffix, text) is not None


def _protected_spans(
    target: dict[str, Any],
    plan: dict[str, Any],
    plain_text: str,
) -> list[str]:
    candidates = list(target["protected_spans"])
    for fact in plan["semantic_plan"]["facts"]:
        candidates.extend(fact["protected_values"])
    for entity in plan["semantic_plan"]["entities"]:
        if entity["protected"]:
            candidates.append(entity["value"])
    return [value for value in dict.fromkeys(candidates) if _contains_surface(plain_text, value)]


def _conditions(plan: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        fact["condition"]
        for fact in plan["semantic_plan"]["facts"]
        if fact["condition"]
    )


def _fiction_marker_count(document: Document) -> int:
    surfaces = [
        block.text
        for block in document.blocks
        if block.text
    ]
    surfaces.extend(
        item.text
        for block in document.blocks
        if block.items
        for item in block.items
    )
    surfaces.extend(
        line
        for block in document.blocks
        if block.lines
        for line in block.lines
    )
    return len(_FICTION_MARKER.findall("\n".join(surfaces)))


def _fact_fiction_marker_count(plan: dict[str, Any]) -> int:
    return sum(
        len(_FICTION_MARKER.findall(fact["text"]))
        for fact in plan["semantic_plan"]["facts"]
    )


def _has_unlocalized_german_structure(
    plan: dict[str, Any],
    document: Document,
) -> bool:
    structure = plan["semantic_plan"]["structure"]
    plan_values = [
        value
        for field in ("paragraph_topics", "list_items", "signature_lines")
        for value in structure[field]
    ]
    target_values: list[str] = []
    for block in document.blocks:
        if block.type in _STRUCTURAL_TYPES:
            target_values.append(block.text)
        elif block.items:
            target_values.extend(item.text for item in block.items)
        elif block.type is BlockType.SIGNATURE:
            target_values.extend(block.lines)
    return any(
        value.startswith(_UNLOCALIZED_GERMAN_STRUCTURE)
        for value in [*plan_values, *target_values]
    )


def _has_promoted_pronoun_residual(
    plan: dict[str, Any],
    document: Document,
) -> bool:
    surfaces = [fact["text"] for fact in plan["semantic_plan"]["facts"]]
    surfaces.append(render_plain_text(document))
    return any(
        pattern.search("\n".join(surfaces))
        for pattern in _PROMOTED_PRONOUN_PATTERNS
    )


def _assigned_input_batches() -> list[dict[str, str]]:
    paths = [
        SOURCE_PLANS_PATH,
        SOURCE_TARGETS_PATH,
        SOURCE_MANIFEST_PATH,
        REVIEWS_PATH,
        SUMMARY_PATH,
        CRITIC_SOURCE_PATH,
        INDEX_PATH,
        BEHAVIORAL_CONTRACT_PATH,
        STYLE_CONTRACT_PATH,
        VERDICT_SCHEMA_PATH,
        HANDOFF_PATH,
        PROJECT_ROOT / "src" / "scriber_polishing" / "adversarial_plan_repair.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "condition_realization.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "target_renderer.py",
    ]
    return [
        {
            "path": repository_relative(path),
            "sha256": "sha256:" + sha256_file(path),
        }
        for path in paths
    ]


def _repair_target_record(
    source_target: dict[str, Any],
    plan: dict[str, Any],
    document: Document,
    prompt_hash: str,
) -> dict[str, Any]:
    repaired = copy.deepcopy(source_target)
    repaired["canonical_document_id"] = f"advrepair_v5_02_{source_target['seed_id']}"
    repaired["plan_generator_agent"] = plan["generator_agent"]
    repaired["source_plan_batch"] = plan["batch_id"]
    repaired["semantic_plan_sha256"] = _semantic_plan_hash(plan)
    if "semantic_plan" in repaired:
        repaired["semantic_plan"] = copy.deepcopy(plan["semantic_plan"])
    repaired["target_writer_agent"] = WRITER_ID
    repaired["target_prompt_hash"] = prompt_hash
    repaired["target_ast"] = document_to_dict(document)
    repaired["target_sst"] = render_sst(document)
    repaired["target_plain_text"] = render_plain_text(document)
    repaired["target_markdown"] = render_markdown(document)
    repaired["target_html"] = render_html(document)
    repaired["source_text"] = repaired["target_plain_text"]
    repaired["protected_spans"] = _protected_spans(
        repaired,
        plan,
        repaired["target_plain_text"],
    )
    return validate_canonical_record(repaired)


def build() -> BuildResult:
    if sha256_file(SOURCE_MANIFEST_PATH) != EXPECTED_SOURCE_MANIFEST_SHA256:
        raise ValueError("post-style-v5.01 source manifest hash mismatch")
    source_plans = _load_by_seed(SOURCE_PLANS_PATH, EXPECTED_SOURCE_PLANS_SHA256)
    source_targets = _load_by_seed(SOURCE_TARGETS_PATH, EXPECTED_SOURCE_TARGETS_SHA256)
    if (
        set(source_plans) != set(source_targets)
        or len(source_plans) != EXPECTED_SOURCE_RECORDS
    ):
        raise ValueError("source plans and targets do not have the pinned common scope")
    review_flags, _scopes = _review_flags()
    if not set(review_flags).issubset(source_plans):
        raise ValueError("adversarial review references an unknown source seed")
    original_reject_ids = {
        seed_id for seed_id, flags in review_flags.items() if flags
    }
    if len(original_reject_ids) != EXPECTED_ORIGINAL_REJECT_COUNT:
        raise ValueError("original adversarial rejection count differs from the pin")

    prompt_hash = "sha256:" + sha256_file(HANDOFF_PATH)
    plans: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    repaired_plan_count = 0
    changed_ast_count = 0
    changed_plain_count = 0
    changed_markdown_count = 0
    changed_html_count = 0
    condition_seed_count = 0
    condition_occurrence_count = 0
    fiction_marker_occurrence_count = 0
    pronoun_changed_fact_count = 0
    pronoun_protected_value_removals = 0
    already_removed_condition_ids: set[str] = set()
    remaining_condition_ids: set[str] = set()
    changed_ast_ids: set[str] = set()
    applied_counts: Counter[str] = Counter()
    expanded_variant_count = 0

    for seed_id in sorted(source_plans):
        source_plan = source_plans[seed_id]
        source_target = source_targets[seed_id]
        flags = review_flags.get(seed_id, frozenset())
        plan = copy.deepcopy(source_plan)
        source_document = document_from_dict(source_target["target_ast"])
        document = source_document

        if "conflicting_deadline" in flags:
            plan, document = bind_feedback_list_to_obligation_due_date(plan, document)
            applied_counts["conflicting_deadline"] += 1
        if "uncertainty_contradicted_by_deadline" in flags:
            plan, document = mark_uncertain_list_date(plan, document)
            applied_counts["uncertainty_contradicted_by_deadline"] += 1
        if "unjustified_single_item_list" in flags:
            plan, document = paragraphize_single_unchanged_item(plan, document)
            applied_counts["unjustified_single_item_list"] += 1
        if "output_language_changed" in flags:
            plan, document = localize_german_structure(plan, document)
            applied_counts["output_language_changed"] += 1
        if "pronoun_reference_error" in flags:
            source_facts = copy.deepcopy(plan["semantic_plan"]["facts"])
            plan, document = promote_explicit_named_actors(plan, document)
            pronoun_changed_fact_count += sum(
                before["text"] != after["text"]
                for before, after in zip(
                    source_facts,
                    plan["semantic_plan"]["facts"],
                    strict=True,
                )
            )
            pronoun_protected_value_removals += sum(
                len(before["protected_values"]) - len(after["protected_values"])
                for before, after in zip(
                    source_facts,
                    plan["semantic_plan"]["facts"],
                    strict=True,
                )
            )
            applied_counts["pronoun_reference_error"] += 1
        if "semantic_role_weakened" in flags:
            document = preserve_feedback_position_role(plan, document)
            applied_counts["semantic_role_weakened"] += 1
        if "artificial_condition_template" in flags:
            marker_count = sum(
                " Die Regelung gilt" in block.text
                for block in document.blocks
                if block.text
            )
            if marker_count:
                document = integrate_appended_condition_commentary(
                    document,
                    _conditions(plan),
                )
                remaining_condition_ids.add(seed_id)
                condition_seed_count += 1
                condition_occurrence_count += marker_count
                applied_counts["artificial_condition_template"] += 1
            else:
                already_removed_condition_ids.add(seed_id)
        if "redundant_condition_restated" in flags:
            document = remove_redundant_condition_restatement(
                document,
                _conditions(plan),
            )
            applied_counts["redundant_condition_restated"] += 1
        if "awkward_mixed_language_compound" in flags:
            document = naturalize_technical_compound(document)
            applied_counts["awkward_mixed_language_compound"] += 1
        if "fictionality_marker_omitted" in flags:
            expected_fact_markers = _fact_fiction_marker_count(plan)
            before_markers = _fiction_marker_count(document)
            document = restore_lexical_fictionality(plan, document)
            added_markers = _fiction_marker_count(document) - before_markers
            if added_markers != expected_fact_markers:
                raise ValueError(f"{seed_id}: literal fictionality marker count differs")
            fiction_marker_occurrence_count += added_markers
            applied_counts["fictionality_marker_omitted"] += 1

        plan_changed = plan["semantic_plan"] != source_plan["semantic_plan"]
        if plan_changed:
            plan["generator_agent"] = PLAN_REPAIR_AGENT
            plan["batch_id"] = PLAN_REPAIR_BATCH
            plan["prompt_hash"] = prompt_hash
            repaired_plan_count += 1
        validated_plan = validate_seed_plan(plan)
        if (
            validated_plan["semantic_plan"]["entities"]
            != source_plan["semantic_plan"]["entities"]
        ):
            raise ValueError(f"{seed_id}: adversarial repair changed a declared entity")
        facts_changed = (
            validated_plan["semantic_plan"]["facts"]
            != source_plan["semantic_plan"]["facts"]
        )
        if facts_changed != ("pronoun_reference_error" in flags):
            raise ValueError(f"{seed_id}: fact changes are outside the adjudicated pronoun scope")
        if "output_language_changed" in flags and _has_unlocalized_german_structure(
            validated_plan,
            document,
        ):
            raise ValueError(f"{seed_id}: reviewed German structure remains unlocalized")
        if "pronoun_reference_error" in flags and _has_promoted_pronoun_residual(
            validated_plan,
            document,
        ):
            raise ValueError(f"{seed_id}: adjudicated pronoun misreference remains")

        repaired = _repair_target_record(
            source_target,
            validated_plan,
            document,
            prompt_hash,
        )
        ast_changed = repaired["target_ast"] != source_target["target_ast"]
        changed_ast_count += ast_changed
        if ast_changed:
            changed_ast_ids.add(seed_id)
        changed_plain_count += repaired["target_plain_text"] != source_target["target_plain_text"]
        changed_markdown_count += repaired["target_markdown"] != source_target["target_markdown"]
        changed_html_count += repaired["target_html"] != source_target["target_html"]

        if "semantic_role_weakened" in flags and "Rückmeldung zum Thema" in repaired["target_plain_text"]:
            raise ValueError(f"{seed_id}: weakened feedback role remains")
        if (
            "artificial_condition_template" in flags
            and " Die Regelung gilt" in repaired["target_plain_text"]
        ):
            raise ValueError(f"{seed_id}: artificial condition template remains")
        if (
            "redundant_condition_restated" in flags
            and _REDUNDANT_RESTATEMENT in repaired["target_plain_text"]
        ):
            raise ValueError(f"{seed_id}: redundant completed-check condition remains")
        if (
            "awkward_mixed_language_compound" in flags
            and _AWKWARD_COMPOUND.search(repaired["target_plain_text"])
        ):
            raise ValueError(f"{seed_id}: awkward mixed-language compound remains")
        if (
            "fictionality_marker_omitted" in flags
            and _fact_fiction_marker_count(validated_plan)
            > _fiction_marker_count(document)
        ):
            raise ValueError(f"{seed_id}: literal fictionality qualification remains omitted")
        if "html_nbsp_contract_violation" in flags and (
            render_html(document_from_dict(repaired["target_ast"])) != repaired["target_html"]
        ):
            raise ValueError(f"{seed_id}: HTML renderer contract remains violated")

        variants = expand_canonical_variants(validated_plan, repaired)
        if len(variants) != VARIANT_COUNT:
            raise ValueError(f"{seed_id}: canonical expansion did not produce seven variants")
        expanded_variant_count += len(variants)
        plans.append(validated_plan)
        records.append(repaired)

    expected_applied_counts = Counter(
        {
            "artificial_condition_template": EXPECTED_REMAINING_CONDITION_SEEDS,
            "awkward_mixed_language_compound": EXPECTED_FLAG_COUNTS[
                "awkward_mixed_language_compound"
            ],
            "conflicting_deadline": EXPECTED_FLAG_COUNTS["conflicting_deadline"],
            "fictionality_marker_omitted": EXPECTED_FLAG_COUNTS[
                "fictionality_marker_omitted"
            ],
            "output_language_changed": EXPECTED_FLAG_COUNTS[
                "output_language_changed"
            ],
            "pronoun_reference_error": EXPECTED_FLAG_COUNTS[
                "pronoun_reference_error"
            ],
            "redundant_condition_restated": EXPECTED_FLAG_COUNTS[
                "redundant_condition_restated"
            ],
            "semantic_role_weakened": EXPECTED_FLAG_COUNTS["semantic_role_weakened"],
            "uncertainty_contradicted_by_deadline": EXPECTED_FLAG_COUNTS[
                "uncertainty_contradicted_by_deadline"
            ],
            "unjustified_single_item_list": EXPECTED_FLAG_COUNTS[
                "unjustified_single_item_list"
            ],
        }
    )
    if applied_counts != expected_applied_counts:
        raise ValueError("adversarial repair counts differ from the pinned scope")
    if (
        condition_seed_count != EXPECTED_REMAINING_CONDITION_SEEDS
        or condition_occurrence_count != EXPECTED_REMAINING_CONDITION_OCCURRENCES
        or _scope_hash(remaining_condition_ids) != EXPECTED_REMAINING_CONDITION_SCOPE_SHA256
        or len(already_removed_condition_ids) != EXPECTED_ALREADY_REMOVED_CONDITION_SEEDS
        or _scope_hash(already_removed_condition_ids)
        != EXPECTED_ALREADY_REMOVED_CONDITION_SCOPE_SHA256
    ):
        raise ValueError("post-style condition scope differs from the pinned audit")
    if (
        fiction_marker_occurrence_count != EXPECTED_FICTION_MARKER_OCCURRENCES
        or pronoun_changed_fact_count != EXPECTED_PRONOUN_CHANGED_FACT_COUNT
        or pronoun_protected_value_removals
        != EXPECTED_PRONOUN_PROTECTED_VALUE_REMOVALS
    ):
        raise ValueError("adjudicated semantic repair occurrences differ from the pinned scope")
    if (
        repaired_plan_count != EXPECTED_REPAIRED_PLAN_COUNT
        or changed_ast_count != EXPECTED_CHANGED_TARGET_COUNT
        or changed_plain_count != EXPECTED_CHANGED_TARGET_COUNT
        or changed_markdown_count != EXPECTED_CHANGED_TARGET_COUNT
        or changed_html_count != EXPECTED_CHANGED_TARGET_COUNT
        or expanded_variant_count != EXPECTED_CANONICAL_VARIANTS
    ):
        raise ValueError("adversarial output counts differ from the pinned scope")
    source_resolved_only_reject_ids = original_reject_ids - changed_ast_ids
    if (
        not changed_ast_ids.issubset(original_reject_ids)
        or len(changed_ast_ids) != EXPECTED_CHANGED_TARGET_COUNT
        or len(source_resolved_only_reject_ids)
        != EXPECTED_SOURCE_RESOLVED_ONLY_REJECT_COUNT
        or not source_resolved_only_reject_ids.issubset(
            already_removed_condition_ids
        )
        or _scope_hash(source_resolved_only_reject_ids)
        != EXPECTED_SOURCE_RESOLVED_ONLY_REJECT_SCOPE_SHA256
    ):
        raise ValueError("original adversarial rejection disposition differs from the pin")

    plans_bytes = ("\n".join(canonical_json(plan) for plan in plans) + "\n").encode("utf-8")
    targets_bytes = ("\n".join(canonical_json(record) for record in records) + "\n").encode("utf-8")
    manifest = {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "writer_version": WRITER_VERSION,
        "target_prompt_hash": prompt_hash,
        "record_count": len(records),
        "plan_count": len(plans),
        "repaired_plan_count": repaired_plan_count,
        "rejected_seed_count": 0,
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "unique_canonical_document_id_count": len(
            {record["canonical_document_id"] for record in records}
        ),
        "plans_sha256": "sha256:" + sha256_bytes(plans_bytes),
        "targets_sha256": "sha256:" + sha256_bytes(targets_bytes),
        "assigned_input_batches": _assigned_input_batches(),
        "critic_inputs": {
            "package_sha256": EXPECTED_PACKAGE_SHA256,
            "reviews_sha256": EXPECTED_REVIEWS_SHA256,
            "summary_sha256": EXPECTED_SUMMARY_SHA256,
            "private_index_sha256": EXPECTED_INDEX_SHA256,
            "scope_sha256": dict(sorted(EXPECTED_SCOPE_SHA256.items())),
        },
        "repair_flag_counts": dict(sorted(applied_counts.items())),
        "source_resolved_flag_counts": {
            "artificial_condition_template": EXPECTED_ALREADY_REMOVED_CONDITION_SEEDS,
            "html_nbsp_contract_violation": EXPECTED_FLAG_COUNTS[
                "html_nbsp_contract_violation"
            ],
        },
        "independently_adjudicated_flag_counts": {
            flag: EXPECTED_FLAG_COUNTS[flag]
            for flag in (
                "fictionality_marker_omitted",
                "output_language_changed",
                "pronoun_reference_error",
            )
        },
        "independently_adjudicated_scope_sha256": {
            flag: EXPECTED_SCOPE_SHA256[flag]
            for flag in (
                "fictionality_marker_omitted",
                "output_language_changed",
                "pronoun_reference_error",
            )
        },
        "semantic_repair_occurrences": {
            "restored_lexical_fiction_markers": fiction_marker_occurrence_count,
            "pronoun_changed_facts": pronoun_changed_fact_count,
            "pronoun_protected_values_removed": pronoun_protected_value_removals,
        },
        "condition_integration": {
            "seed_count": condition_seed_count,
            "occurrence_count": condition_occurrence_count,
            "seed_scope_sha256": EXPECTED_REMAINING_CONDITION_SCOPE_SHA256,
        },
        "critic_rejection_repair": {
            "original_rejected_seed_count": len(original_reject_ids),
            "layer_changed_rejected_seed_count": len(changed_ast_ids),
            "source_resolved_only_rejected_seed_count": len(
                source_resolved_only_reject_ids
            ),
            "source_resolved_only_scope_sha256": (
                EXPECTED_SOURCE_RESOLVED_ONLY_REJECT_SCOPE_SHA256
            ),
            "remaining_rejected_seed_count": 0,
        },
        "changed_ast_count": changed_ast_count,
        "changed_plain_text_count": changed_plain_count,
        "changed_markdown_count": changed_markdown_count,
        "changed_html_count": changed_html_count,
        "canonical_expansion": {
            "validated_target_count": len(records),
            "variants_per_target": VARIANT_COUNT,
            "validated_variant_count": expanded_variant_count,
        },
        "generation_boundary": {
            "ai_critic_feedback_only": True,
            "no_external_corpus": True,
            "no_generative_api": True,
            "no_human_curation": True,
            "no_training_or_evaluation_split_read": True,
        },
        "deterministic_serialization": {
            "record_order": "seed_id ascending",
            "jsonl": "utf-8 compact sorted JSON with one final LF",
            "timestamps_omitted": True,
        },
    }
    validation = {
        "schema_version": 1,
        "passed": True,
        "source_plan_count": len(source_plans),
        "reviewed_seed_count": len(review_flags),
        "original_rejected_seed_count": len(original_reject_ids),
        "layer_changed_rejected_seed_count": len(changed_ast_ids),
        "source_resolved_only_rejected_seed_count": len(
            source_resolved_only_reject_ids
        ),
        "remaining_rejected_seed_count": 0,
        "selected_plan_count": len(plans),
        "output_target_count": len(records),
        "repaired_plan_count": repaired_plan_count,
        "changed_target_count": changed_ast_count,
        "integrated_condition_seed_count": condition_seed_count,
        "integrated_condition_occurrence_count": condition_occurrence_count,
        "already_source_resolved_condition_seed_count": len(already_removed_condition_ids),
        "genuine_repair_markers_remaining": 0,
        "entities_unchanged": True,
        "fact_changes_limited_to_adjudicated_actor_promotion": True,
        "restored_lexical_fiction_marker_count": fiction_marker_occurrence_count,
        "localized_german_structure_seed_count": applied_counts[
            "output_language_changed"
        ],
        "explicit_name_actor_promotion_seed_count": applied_counts[
            "pronoun_reference_error"
        ],
        "explicit_name_actor_promotion_changed_fact_count": pronoun_changed_fact_count,
        "removed_displaced_pronoun_protected_value_count": (
            pronoun_protected_value_removals
        ),
        "all_source_html_contract_findings_resolved": True,
        "all_plans_schema_valid": True,
        "all_targets_canonical_valid": True,
        "all_targets_expand_to_seven_variants": (
            expanded_variant_count == len(records) * VARIANT_COUNT
        ),
        "plans_sha256": manifest["plans_sha256"],
        "targets_sha256": manifest["targets_sha256"],
    }
    report = (
        "# Adversarial plan/target repair v5.02\n\n"
        f"- Selected valid plans and targets: {len(records)}\n"
        f"- Repaired plans: {repaired_plan_count}\n"
        f"- Targets changed: {changed_ast_count}\n"
        f"- Original critic rejections: {len(original_reject_ids)}\n"
        f"- Rejections changed in this layer: {len(changed_ast_ids)}\n"
        f"- Rejections already resolved by the source layer: {len(source_resolved_only_reject_ids)}\n"
        "- Genuine critic rejections remaining: 0\n"
        f"- Integrated condition-template seeds: {condition_seed_count}\n"
        f"- Integrated condition occurrences: {condition_occurrence_count}\n"
        f"- Previously source-resolved condition seeds: {len(already_removed_condition_ids)}\n"
        f"- Feedback-position role repairs: {applied_counts['semantic_role_weakened']}\n"
        f"- Obligation deadline bindings: {applied_counts['conflicting_deadline']}\n"
        f"- Unconfirmed-date list repairs: {applied_counts['uncertainty_contradicted_by_deadline']}\n"
        f"- Closed redundant-condition removals: {applied_counts['redundant_condition_restated']}\n"
        f"- Naturalized technical compounds: {applied_counts['awkward_mixed_language_compound']}\n"
        f"- Paragraphized one-item lists: {applied_counts['unjustified_single_item_list']}\n"
        f"- Restored lexical fictionality seeds: {applied_counts['fictionality_marker_omitted']}\n"
        f"- Restored lexical fictionality markers: {fiction_marker_occurrence_count}\n"
        f"- Fully localized de-DE structures: {applied_counts['output_language_changed']}\n"
        f"- Explicit-name actor-promotion seeds: {applied_counts['pronoun_reference_error']}\n"
        f"- Actor-promotion fact changes: {pronoun_changed_fact_count}\n"
        f"- Displaced pronoun protected values removed: {pronoun_protected_value_removals}\n"
        f"- Validated canonical variants: {expanded_variant_count}\n"
        f"- Plans SHA-256: `{manifest['plans_sha256']}`\n"
        f"- Targets SHA-256: `{manifest['targets_sha256']}`\n"
    )
    return BuildResult(
        plans=tuple(plans),
        records=tuple(records),
        plans_bytes=plans_bytes,
        targets_bytes=targets_bytes,
        manifest=manifest,
        validation=validation,
        report=report,
    )


def write(result: BuildResult) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payloads = (
        (PLANS_PATH.name, result.plans_bytes),
        (TARGETS_PATH.name, result.targets_bytes),
        (REPORT_PATH.name, result.report.encode("utf-8")),
        (
            VALIDATION_PATH.name,
            (canonical_json(result.validation) + "\n").encode("utf-8"),
        ),
        (
            MANIFEST_PATH.name,
            (canonical_json(result.manifest) + "\n").encode("utf-8"),
        ),
    )
    with tempfile.TemporaryDirectory(
        dir=OUTPUT_DIR.parent,
        prefix=f".{OUTPUT_DIR.name}.",
    ) as stage_name:
        stage_dir = Path(stage_name)
        for name, payload in payloads:
            (stage_dir / name).write_bytes(payload)
        _, errors = _validate_batch(stage_dir, PROJECT_ROOT, set(), set())
        if errors:
            raise ValueError(
                "staged target batch validation failed: " + "; ".join(errors[:3])
            )
        for name, _ in payloads:
            (stage_dir / name).replace(OUTPUT_DIR / name)

    _, errors = _validate_batch(OUTPUT_DIR, PROJECT_ROOT, set(), set())
    if errors:
        raise ValueError("target batch validation failed: " + "; ".join(errors[:3]))


def main() -> int:
    first = build()
    second = build()
    if (
        first.plans_bytes != second.plans_bytes
        or first.targets_bytes != second.targets_bytes
        or canonical_json(first.manifest) != canonical_json(second.manifest)
        or canonical_json(first.validation) != canonical_json(second.validation)
        or first.report != second.report
    ):
        raise RuntimeError("post-v5.02 adversarial repair is not byte-deterministic")
    write(first)
    print(
        canonical_json(
            {
                "status": "complete",
                "plans": len(first.plans),
                "targets": len(first.records),
                "plans_sha256": first.manifest["plans_sha256"],
                "targets_sha256": first.manifest["targets_sha256"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
