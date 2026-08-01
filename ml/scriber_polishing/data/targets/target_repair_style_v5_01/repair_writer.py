#!/usr/bin/env python3
"""Build the immutable style-critic repair view after v4.02."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
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

from scriber_polishing.ast_codec import document_from_dict, document_to_dict  # noqa: E402
from scriber_polishing.canonical_expander import VARIANT_COUNT, expand_canonical_variants  # noqa: E402
from scriber_polishing.condition_realization import remove_redundant_condition_commentary  # noqa: E402
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

WRITER_ID = "target_repair_style_v5_01"
WRITER_VERSION = 1
EXPECTED_RECORDS = 2625
EXPECTED_REVIEW_COUNT = 2484
EXPECTED_MECHANICAL_SEEDS = 65
EXPECTED_MECHANICAL_CONDITIONS = 95
EXPECTED_HTML_SEEDS = 175
EXPECTED_MECHANICAL_SEEDS_SHA256 = "952fd90d3dee3266f8621624bccb2e939248bdbb2367f5aa7a49c39b30a4b112"
EXPECTED_HTML_SEEDS_SHA256 = "60ae987ea6004ed89a80a213d9ba788eb25ed926ff1fe9ef8033a483d95f05aa"
EXPECTED_PACKAGE_SHA256 = "5afe3dfcc8c99dbc6ce4634d676438d3186098940b9b7aff6611c682a1ca9ae4"
EXPECTED_REVIEWS_SHA256 = "0551b0cac4cd3ba23b97a2116d9ddaab4dcdf1e2a839cd8d160a94bef3af79a7"
EXPECTED_INDEX_SHA256 = "244cb57640252f5169a157baef14d1b740bde60aac84ff6fd55369754e3ec5b6"
EXPECTED_SOURCE_PLANS_SHA256 = "db0fcd8c8b2a620d8f96aa7dbf27fc6932a64f967adc712aaebe597e19ca6832"
EXPECTED_SOURCE_TARGETS_SHA256 = "374eb6b5b996a9c0555a118e5bb1fc9a0e07c6dfdf4e690e386f395e4a93609b"

SOURCE_DIR = PROJECT_ROOT / "data" / "targets" / "target_repair_adversarial_v4_02"
SOURCE_PLANS_PATH = SOURCE_DIR / "plans.jsonl"
SOURCE_TARGETS_PATH = SOURCE_DIR / "targets.jsonl"
REVIEWS_PATH = PROJECT_ROOT / "artifacts" / "agent_reviews_v5" / "style_terra_max_01" / "reviews.jsonl"
INDEX_PATH = PROJECT_ROOT / "artifacts" / "private" / "critic_package_v5_index.json"
HANDOFF_PATH = PROJECT_ROOT / "handoffs" / "AGENT_PLAN_TARGET_REPAIR_STYLE_V5.md"
PLANS_PATH = OUTPUT_DIR / "plans.jsonl"
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
REPORT_PATH = OUTPUT_DIR / "repair_report.md"
VALIDATION_PATH = OUTPUT_DIR / "validation.json"
ALLOWED_FLAGS = frozenset({"html_nbsp_contract_violation", "mechanical_condition_template"})


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


def _review_flags() -> dict[str, frozenset[str]]:
    if sha256_file(REVIEWS_PATH) != EXPECTED_REVIEWS_SHA256:
        raise ValueError("style review hash mismatch")
    if sha256_file(INDEX_PATH) != EXPECTED_INDEX_SHA256:
        raise ValueError("private critic index hash mismatch")
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    if not isinstance(index, dict) or not all(
        isinstance(case_id, str) and isinstance(seed_id, str)
        for case_id, seed_id in index.items()
    ):
        raise ValueError("private critic index is malformed")

    rows = [validate_critic_verdict(row) for row in _load_jsonl(REVIEWS_PATH)]
    if len(rows) != EXPECTED_REVIEW_COUNT:
        raise ValueError("style review count mismatch")
    seen_cases: set[str] = set()
    result: dict[str, frozenset[str]] = {}
    flag_counts: Counter[str] = Counter()
    for row in rows:
        case_id = row["case_id"]
        if case_id in seen_cases or case_id not in index:
            raise ValueError("style review case is duplicate or absent from private index")
        seen_cases.add(case_id)
        if row["reviewed_package_sha256"] != EXPECTED_PACKAGE_SHA256:
            raise ValueError("style review package hash mismatch")
        if row["critic_role"] != "style":
            raise ValueError("non-style verdict present in style review")
        flags = frozenset([*row["critical_flags"], *row["noncritical_flags"]])
        if row["acceptable"]:
            if flags:
                raise ValueError("accepted style verdict contains a flag")
            continue
        if not flags or not flags.issubset(ALLOWED_FLAGS):
            raise ValueError("style review contains an unsupported rejection flag")
        seed_id = index[case_id]
        if seed_id in result:
            raise ValueError("multiple rejected cases map to the same seed")
        result[seed_id] = flags
        flag_counts.update(flags)

    if flag_counts != Counter(
        {
            "html_nbsp_contract_violation": EXPECTED_HTML_SEEDS,
            "mechanical_condition_template": EXPECTED_MECHANICAL_SEEDS,
        }
    ):
        raise ValueError("style repair flag counts differ from the pinned scope")
    scopes = {
        flag: sorted(seed_id for seed_id, flags in result.items() if flag in flags)
        for flag in ALLOWED_FLAGS
    }
    scope_hashes = {
        flag: sha256_bytes(("\n".join(seed_ids) + "\n").encode("utf-8"))
        for flag, seed_ids in scopes.items()
    }
    if scope_hashes != {
        "mechanical_condition_template": EXPECTED_MECHANICAL_SEEDS_SHA256,
        "html_nbsp_contract_violation": EXPECTED_HTML_SEEDS_SHA256,
    }:
        raise ValueError("style repair seed-set hash differs from the independent audit")
    return result


def _semantic_plan_hash(plan: dict[str, Any]) -> str:
    payload = canonical_json(plan["semantic_plan"]).encode("utf-8")
    return "sha256:" + sha256_bytes(payload)


def _contains_surface(text: str, value: str) -> bool:
    if not value:
        return False
    import re

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


def _assigned_input_batches() -> list[dict[str, str]]:
    paths = [
        SOURCE_PLANS_PATH,
        SOURCE_TARGETS_PATH,
        SOURCE_DIR / "manifest.json",
        REVIEWS_PATH,
        INDEX_PATH,
        HANDOFF_PATH,
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


def build() -> BuildResult:
    source_plans = _load_by_seed(SOURCE_PLANS_PATH, EXPECTED_SOURCE_PLANS_SHA256)
    source_targets = _load_by_seed(SOURCE_TARGETS_PATH, EXPECTED_SOURCE_TARGETS_SHA256)
    if set(source_plans) != set(source_targets) or len(source_plans) != EXPECTED_RECORDS:
        raise ValueError("source plans and targets do not have the pinned common scope")
    review_flags = _review_flags()
    if not set(review_flags).issubset(source_plans):
        raise ValueError("style repair review references an unknown source seed")

    prompt_hash = "sha256:" + sha256_file(HANDOFF_PATH)
    plans: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    removed_condition_count = 0
    changed_ast_count = 0
    changed_plain_count = 0
    changed_markdown_count = 0
    changed_html_count = 0
    expanded_variant_count = 0
    for seed_id in sorted(source_plans):
        source_plan = source_plans[seed_id]
        source_target = source_targets[seed_id]
        flags = review_flags.get(seed_id, frozenset())
        plan = copy.deepcopy(source_plan)
        validated_plan = validate_seed_plan(plan)

        document = document_from_dict(source_target["target_ast"])
        if "mechanical_condition_template" in flags:
            marker = " Die Regelung gilt unter folgender Bedingung: "
            marker_count = sum(marker in block.text for block in document.blocks if block.text)
            conditions = tuple(
                fact["condition"]
                for fact in validated_plan["semantic_plan"]["facts"]
                if fact["condition"]
            )
            document = remove_redundant_condition_commentary(document, conditions)
            removed_condition_count += marker_count

        repaired = copy.deepcopy(source_target)
        repaired["canonical_document_id"] = f"stylefix_{seed_id}"
        repaired["plan_generator_agent"] = validated_plan["generator_agent"]
        repaired["source_plan_batch"] = validated_plan["batch_id"]
        repaired["semantic_plan_sha256"] = _semantic_plan_hash(validated_plan)
        if "semantic_plan" in repaired:
            repaired["semantic_plan"] = copy.deepcopy(validated_plan["semantic_plan"])
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
            validated_plan,
            repaired["target_plain_text"],
        )
        validated_target = validate_canonical_record(repaired)

        changed_ast_count += validated_target["target_ast"] != source_target["target_ast"]
        changed_plain_count += validated_target["target_plain_text"] != source_target["target_plain_text"]
        changed_markdown_count += validated_target["target_markdown"] != source_target["target_markdown"]
        changed_html_count += validated_target["target_html"] != source_target["target_html"]
        variants = expand_canonical_variants(validated_plan, validated_target)
        if len(variants) != VARIANT_COUNT:
            raise ValueError(f"{seed_id}: canonical expansion did not produce seven variants")
        expanded_variant_count += len(variants)
        plans.append(validated_plan)
        records.append(validated_target)

    if (
        removed_condition_count != EXPECTED_MECHANICAL_CONDITIONS
        or changed_ast_count != EXPECTED_MECHANICAL_SEEDS
        or changed_plain_count != EXPECTED_MECHANICAL_SEEDS
        or changed_markdown_count != EXPECTED_MECHANICAL_SEEDS
        or changed_html_count != EXPECTED_MECHANICAL_SEEDS + EXPECTED_HTML_SEEDS
    ):
        raise ValueError("style repair output counts differ from the pinned scope")

    plans_bytes = ("\n".join(canonical_json(plan) for plan in plans) + "\n").encode("utf-8")
    targets_bytes = ("\n".join(canonical_json(record) for record in records) + "\n").encode("utf-8")
    manifest = {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "writer_version": WRITER_VERSION,
        "target_prompt_hash": prompt_hash,
        "record_count": len(records),
        "plan_count": len(plans),
        "repaired_plan_count": 0,
        "removed_redundant_condition_count": removed_condition_count,
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
            "private_index_sha256": EXPECTED_INDEX_SHA256,
            "mechanical_seed_set_sha256": EXPECTED_MECHANICAL_SEEDS_SHA256,
            "html_seed_set_sha256": EXPECTED_HTML_SEEDS_SHA256,
        },
        "repair_flag_counts": {
            "html_nbsp_contract_violation": EXPECTED_HTML_SEEDS,
            "mechanical_condition_template": EXPECTED_MECHANICAL_SEEDS,
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
        "selected_plan_count": len(plans),
        "output_target_count": len(records),
        "repaired_plan_count": 0,
        "removed_redundant_condition_count": removed_condition_count,
        "style_rejection_seed_count": len(review_flags),
        "semantic_plans_unchanged": sha256_bytes(plans_bytes) == EXPECTED_SOURCE_PLANS_SHA256,
        "redundant_condition_commentary_count": 0,
        "all_plans_schema_valid": True,
        "all_targets_canonical_valid": True,
        "all_targets_expand_to_seven_variants": expanded_variant_count == len(records) * VARIANT_COUNT,
        "plans_sha256": manifest["plans_sha256"],
        "targets_sha256": manifest["targets_sha256"],
    }
    report = (
        "# AI style-plan and target repair v5.01\n\n"
        f"- Selected valid plans and targets: {len(records)}\n"
        "- Semantic plans changed: 0\n"
        f"- Redundant condition sentences removed: {removed_condition_count}\n"
        f"- HTML spacing repairs: {EXPECTED_HTML_SEEDS}\n"
        f"- Plain/Markdown/AST surfaces changed: {changed_plain_count}\n"
        f"- HTML surfaces changed in total: {changed_html_count}\n"
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
    PLANS_PATH.write_bytes(result.plans_bytes)
    TARGETS_PATH.write_bytes(result.targets_bytes)
    MANIFEST_PATH.write_text(canonical_json(result.manifest) + "\n", encoding="utf-8", newline="\n")
    VALIDATION_PATH.write_text(
        canonical_json(result.validation) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    REPORT_PATH.write_text(result.report, encoding="utf-8", newline="\n")
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
    ):
        raise RuntimeError("post-v5 style repair is not byte-deterministic")
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
