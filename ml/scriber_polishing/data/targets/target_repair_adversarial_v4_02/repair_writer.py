#!/usr/bin/env python3
"""Build the plan-aligned post-v4 adversarial repair view."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

sys.dont_write_bytecode = True

OUTPUT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = OUTPUT_DIR.parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scriber_polishing.ast_codec import document_from_dict, document_to_dict  # noqa: E402
from scriber_polishing.canonical_expander import VARIANT_COUNT, expand_canonical_variants  # noqa: E402
from scriber_polishing.critic_repair import repair_document  # noqa: E402
from scriber_polishing.pronoun_plan_repair import (  # noqa: E402
    remove_unsupported_pronoun_commentary,
    repair_pronoun_plan,
)
from scriber_polishing.schemas import validate_seed_plan  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_batch_validator import _validate_batch  # noqa: E402
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text  # noqa: E402
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

WRITER_ID = "target_repair_adversarial_v4_02"
WRITER_VERSION = 1
PLAN_REPAIR_AGENT = "plan_repair_adversarial_v4_02"
PLAN_REPAIR_BATCH = "adversarial_plan_repair_v4_02"
EXPECTED_RECORDS = 2625
EXPECTED_PRONOUN_PLAN_REPAIRS = 141

BASE_WRITER_PATH = PROJECT_ROOT / "data" / "targets" / "target_repair_adversarial_v4_01" / "repair_writer.py"
HANDOFF_PATH = PROJECT_ROOT / "handoffs" / "AGENT_PLAN_TARGET_REPAIR_PRONOUNS.md"
PLANS_PATH = OUTPUT_DIR / "plans.jsonl"
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
REPORT_PATH = OUTPUT_DIR / "repair_report.md"
VALIDATION_PATH = OUTPUT_DIR / "validation.json"


@dataclass(frozen=True)
class BuildResult:
    plans: tuple[dict[str, Any], ...]
    records: tuple[dict[str, Any], ...]
    plans_bytes: bytes
    targets_bytes: bytes
    manifest: dict[str, Any]
    validation: dict[str, Any]
    report: str


def _load_base_writer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "scriber_polishing_target_repair_adversarial_v4_01",
        BASE_WRITER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the pinned v4_01 repair builder")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def repository_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()


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


def _repair_target(
    target: dict[str, Any],
    plan: dict[str, Any],
    *,
    plan_was_repaired: bool,
    v4_flags: frozenset[str],
    prompt_hash: str,
    base: ModuleType,
) -> dict[str, Any]:
    unsupported = sorted(v4_flags.difference(base.EXPECTED_V4_CRITICAL_FLAGS | base.REPAIRABLE_V4_NONCRITICAL_FLAGS))
    if unsupported:
        raise ValueError(f"{target['seed_id']}: unsupported v4 flag {unsupported[0]}")
    document = document_from_dict(target["target_ast"])
    if plan_was_repaired:
        document = remove_unsupported_pronoun_commentary(document)
    style_flags = v4_flags.intersection(base.REPAIRABLE_V4_NONCRITICAL_FLAGS)
    try:
        document = repair_document(document, plan, set(style_flags))
    except ValueError as error:
        raise ValueError(f"{target['seed_id']}: {error}") from error

    repaired = copy.deepcopy(target)
    repaired["canonical_document_id"] = f"advrepair2_{target['seed_id']}"
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
    if "Das Pronomen „" in repaired["target_plain_text"]:
        raise ValueError(f"{target['seed_id']}: unsupported pronoun commentary remains")
    return validate_canonical_record(repaired)


def _assigned_input_batches(base: ModuleType) -> list[dict[str, str]]:
    paths = [
        *sorted(base.PLAN_ROOT.rglob("plans.jsonl")),
        *base.TARGET_INPUT_PATHS,
        base.V4_REVIEWS_PATH,
        base.V4_INDEX_PATH,
        base.V3_REVIEWS_PATH,
        base.V3_INDEX_PATH,
        HANDOFF_PATH,
        BASE_WRITER_PATH,
        PROJECT_ROOT / "src" / "scriber_polishing" / "pronoun_plan_repair.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "critic_repair.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "target_renderer.py",
    ]
    return [
        {
            "path": repository_relative(path),
            "sha256": "sha256:" + sha256_bytes(path.read_bytes()),
        }
        for path in paths
    ]


def build() -> BuildResult:
    base = _load_base_writer()
    source_plans = base.load_plans()
    targets = base.load_targets()
    v4_flags = base.review_flags(
        base.V4_REVIEWS_PATH,
        base.V4_INDEX_PATH,
        expected_reviews_sha256=base.EXPECTED_V4_REVIEWS_SHA256,
        expected_index_sha256=base.EXPECTED_V4_INDEX_SHA256,
        expected_package_sha256=base.EXPECTED_V4_PACKAGE_SHA256,
    )
    v3_flags = base.review_flags(
        base.V3_REVIEWS_PATH,
        base.V3_INDEX_PATH,
        expected_reviews_sha256=base.EXPECTED_V3_REVIEWS_SHA256,
        expected_index_sha256=base.EXPECTED_V3_INDEX_SHA256,
        expected_package_sha256=base.EXPECTED_V3_PACKAGE_SHA256,
    )
    pronoun_seed_ids = {seed_id for seed_id, flags in v3_flags.items() if "pronoun_referent_error" in flags}
    if len(pronoun_seed_ids) != EXPECTED_PRONOUN_PLAN_REPAIRS:
        raise ValueError("pinned pronoun repair scope is not exactly 141 plans")
    v4_critical_seed_ids = {
        seed_id for seed_id, flags in v4_flags.items() if flags.intersection(base.EXPECTED_V4_CRITICAL_FLAGS)
    }
    if not v4_critical_seed_ids.issubset(pronoun_seed_ids):
        raise ValueError("v4 critical scope is not covered by the repaired-plan scope")

    prompt_hash = "sha256:" + sha256_bytes(HANDOFF_PATH.read_bytes())
    selected_plans: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    repair_counts: Counter[str] = Counter()
    changed_plan_count = 0
    changed_plain_count = 0
    changed_html_count = 0
    expanded_variant_count = 0
    for seed_id, target in sorted(targets.items()):
        original_plan = source_plans.get(seed_id)
        if original_plan is None:
            raise ValueError(f"target has no source plan: {seed_id}")
        if seed_id in pronoun_seed_ids:
            plan = repair_pronoun_plan(
                original_plan,
                generator_agent=PLAN_REPAIR_AGENT,
                batch_id=PLAN_REPAIR_BATCH,
                prompt_hash=prompt_hash,
            )
            changed_plan_count += 1
        else:
            plan = copy.deepcopy(original_plan)
        validated_plan = validate_seed_plan(plan)
        flags = v4_flags.get(seed_id, frozenset())
        repaired = _repair_target(
            target,
            validated_plan,
            plan_was_repaired=seed_id in pronoun_seed_ids,
            v4_flags=flags,
            prompt_hash=prompt_hash,
            base=base,
        )
        if repaired["target_plain_text"] != target["target_plain_text"]:
            changed_plain_count += 1
        if repaired["target_html"] != target["target_html"]:
            changed_html_count += 1
        variants = expand_canonical_variants(validated_plan, repaired)
        if len(variants) != VARIANT_COUNT:
            raise ValueError(f"{seed_id}: canonical expansion did not produce seven variants")
        expanded_variant_count += len(variants)
        repair_counts.update(flags.intersection(base.REPAIRABLE_V4_NONCRITICAL_FLAGS))
        selected_plans.append(validated_plan)
        records.append(repaired)

    if (
        len(selected_plans) != EXPECTED_RECORDS
        or len(records) != EXPECTED_RECORDS
        or changed_plan_count != EXPECTED_PRONOUN_PLAN_REPAIRS
    ):
        raise ValueError("post-v4_02 repair counts differ from the pinned scope")
    plans_bytes = ("\n".join(canonical_json(plan) for plan in selected_plans) + "\n").encode("utf-8")
    targets_bytes = ("\n".join(canonical_json(record) for record in records) + "\n").encode("utf-8")
    manifest = {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "writer_version": WRITER_VERSION,
        "target_prompt_hash": prompt_hash,
        "record_count": len(records),
        "plan_count": len(selected_plans),
        "repaired_plan_count": changed_plan_count,
        "rejected_seed_count": 0,
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in records}),
        "plans_sha256": "sha256:" + sha256_bytes(plans_bytes),
        "targets_sha256": "sha256:" + sha256_bytes(targets_bytes),
        "assigned_input_batches": _assigned_input_batches(base),
        "critic_inputs": {
            "v3_package_sha256": base.EXPECTED_V3_PACKAGE_SHA256,
            "v3_reviews_sha256": base.EXPECTED_V3_REVIEWS_SHA256,
            "v4_package_sha256": base.EXPECTED_V4_PACKAGE_SHA256,
            "v4_reviews_sha256": base.EXPECTED_V4_REVIEWS_SHA256,
        },
        "repair_flag_counts": dict(sorted(repair_counts.items())),
        "changed_plain_text_count": changed_plain_count,
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
        "selected_plan_count": len(selected_plans),
        "output_target_count": len(records),
        "repaired_pronoun_plan_count": changed_plan_count,
        "v4_critical_seed_count": len(v4_critical_seed_ids),
        "v4_critical_subset_plan_repaired": True,
        "unsupported_pronoun_commentary_count": 0,
        "all_plans_schema_valid": True,
        "all_targets_canonical_valid": True,
        "all_targets_expand_to_seven_variants": expanded_variant_count == len(records) * VARIANT_COUNT,
        "plans_sha256": manifest["plans_sha256"],
        "targets_sha256": manifest["targets_sha256"],
    }
    report = (
        "# Adversarial plan/target repair v4.02\n\n"
        f"- Selected valid plans and targets: {len(records)}\n"
        f"- Structurally repaired pronoun plans: {changed_plan_count}\n"
        f"- Rejected plans: 0\n"
        f"- Plain-text surfaces changed: {changed_plain_count}\n"
        f"- HTML surfaces changed: {changed_html_count}\n"
        f"- Validated canonical variants: {expanded_variant_count}\n"
        f"- Plans SHA-256: `{manifest['plans_sha256']}`\n"
        f"- Targets SHA-256: `{manifest['targets_sha256']}`\n"
    )
    return BuildResult(
        plans=tuple(selected_plans),
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
    MANIFEST_PATH.write_text(
        canonical_json(result.manifest) + "\n",
        encoding="utf-8",
        newline="\n",
    )
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
        raise RuntimeError("post-v4_02 repair is not byte-deterministic")
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
