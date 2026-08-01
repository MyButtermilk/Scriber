#!/usr/bin/env python3
"""Build the deterministic post-v4 adversarial target-repair overlay."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
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
from scriber_polishing.critic_repair import repair_document  # noqa: E402
from scriber_polishing.schemas import validate_seed_plan  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_batch_validator import _validate_batch  # noqa: E402
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text  # noqa: E402
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

WRITER_ID = "target_repair_adversarial_v4_01"
WRITER_VERSION = 1
EXPECTED_INPUT_TARGETS = 2625
EXPECTED_REJECTED_PRONOUN_PLANS = 141
EXPECTED_OUTPUT_TARGETS = EXPECTED_INPUT_TARGETS - EXPECTED_REJECTED_PRONOUN_PLANS
EXPECTED_V4_PACKAGE_SHA256 = "029268078c87c8aa03d971684daeff9e3ca367386612dae19d8dfd2d4d568eed"
EXPECTED_V4_REVIEWS_SHA256 = "525eba5d90a608a3ff999dd8ca7ab0cb64ade261866e9ed2fdd0a64ef84ceddf"
EXPECTED_V4_INDEX_SHA256 = "4d1bfd9d8438bbb536cc30e6c62d8cd27d5c110d028722dfc9742106ad9d8081"
EXPECTED_V3_PACKAGE_SHA256 = "f94987cd85b0ed98720489e8276d725f795cd02effd1342eabd7a733251f978a"
EXPECTED_V3_REVIEWS_SHA256 = "b41cd20eff8a3ac5866cfc1dccc8911049720e6fb718a0f2a2c1a1791389cdd6"
EXPECTED_V3_INDEX_SHA256 = "27265e71066dcc7c01a048545642dbef39c4df47d291446ed44ab0a227d6df96"
EXPECTED_V4_CRITICAL_FLAGS = frozenset(
    {
        "invented_pronoun_resolution",
        "protected_pronoun_context_changed",
        "unsupported_added_content",
    }
)
REPAIRABLE_V4_NONCRITICAL_FLAGS = frozenset(
    {
        "accidental_repetition",
        "awkward_pronoun_repetition",
        "greeting_agreement_error",
        "missing_government_office_article",
        "redundant_heading",
        "rich_text_nonbreaking_space_missing",
        "technical_compound_error",
        "technical_term_agreement_error",
    }
)

PLAN_ROOT = PROJECT_ROOT / "data" / "seeds"
TARGET_INPUT_PATHS = (
    PROJECT_ROOT / "data" / "targets" / "target_repair_terra_01" / "targets.jsonl",
    PROJECT_ROOT / "data" / "targets" / "target_repair_orthopron_terra_01" / "targets.jsonl",
    PROJECT_ROOT / "data" / "targets" / "target_writer_coverage_completion_01" / "targets.jsonl",
)
V4_REVIEWS_PATH = PROJECT_ROOT / "artifacts" / "agent_reviews_v4" / "adversarial_sol_max_01" / "reviews.jsonl"
V4_INDEX_PATH = PROJECT_ROOT / "artifacts" / "private" / "critic_package_v4_index.json"
V3_REVIEWS_PATH = PROJECT_ROOT / "artifacts" / "agent_reviews_v3" / "adv_sol1_max" / "verdicts.jsonl"
V3_INDEX_PATH = PROJECT_ROOT / "artifacts" / "private" / "critic_package_v3_index.json"
HANDOFF_PATH = PROJECT_ROOT / "handoffs" / "AGENT_TARGET_REPAIR_ADVERSARIAL.md"
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
REJECTS_PATH = OUTPUT_DIR / "rejects.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
REPORT_PATH = OUTPUT_DIR / "repair_report.md"
VALIDATION_PATH = OUTPUT_DIR / "validation.json"


@dataclass(frozen=True)
class BuildResult:
    records: tuple[dict[str, Any], ...]
    rejects: tuple[dict[str, Any], ...]
    targets_bytes: bytes
    rejects_bytes: bytes
    manifest: dict[str, Any]
    validation: dict[str, Any]
    report: str


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def repository_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError(f"{repository_relative(path)} must end with LF")
    if b"\r\n" in raw:
        raise ValueError(f"{repository_relative(path)} must use LF-only JSONL")
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"{repository_relative(path)} has blank line {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{repository_relative(path)} line {line_number} is not an object")
        records.append(value)
    return records


def require_hash(path: Path, expected: str) -> None:
    actual = sha256_bytes(path.read_bytes())
    if actual != expected:
        raise ValueError(f"{repository_relative(path)} SHA-256 mismatch: expected {expected}, got {actual}")


def indexed(records: Iterable[Mapping[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        seed_id = record.get("seed_id")
        if not isinstance(seed_id, str) or not seed_id:
            raise ValueError(f"{label} requires a seed_id")
        if seed_id in result:
            raise ValueError(f"duplicate {label} seed_id: {seed_id}")
        result[seed_id] = dict(record)
    return result


def load_plans() -> dict[str, dict[str, Any]]:
    paths = sorted(PLAN_ROOT.rglob("plans.jsonl"))
    if len(paths) != 9:
        raise ValueError(f"expected nine seed-plan batches, found {len(paths)}")
    plans = [validate_seed_plan(record) for path in paths for record in read_jsonl(path)]
    result = indexed(plans, "plan")
    if len(result) != 2700:
        raise ValueError(f"expected 2700 unique source plans, found {len(result)}")
    return result


def load_targets() -> dict[str, dict[str, Any]]:
    targets = [record for path in TARGET_INPUT_PATHS for record in read_jsonl(path)]
    result = indexed(targets, "target")
    if len(result) != EXPECTED_INPUT_TARGETS:
        raise ValueError(f"expected {EXPECTED_INPUT_TARGETS} unique input targets, found {len(result)}")
    return result


def review_flags(
    reviews_path: Path,
    index_path: Path,
    *,
    expected_reviews_sha256: str,
    expected_index_sha256: str,
    expected_package_sha256: str,
) -> dict[str, frozenset[str]]:
    require_hash(reviews_path, expected_reviews_sha256)
    require_hash(index_path, expected_index_sha256)
    case_to_seed = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(case_to_seed, dict):
        raise ValueError("critic private index must be an object")
    flags_by_seed: defaultdict[str, set[str]] = defaultdict(set)
    seen_case_ids: set[str] = set()
    for verdict in read_jsonl(reviews_path):
        case_id = verdict.get("case_id")
        if not isinstance(case_id, str) or case_id in seen_case_ids:
            raise ValueError("critic verdict case IDs must be unique strings")
        seen_case_ids.add(case_id)
        if verdict.get("reviewed_package_sha256") != expected_package_sha256:
            raise ValueError("critic verdict package SHA-256 differs from the pinned package")
        seed_id = case_to_seed.get(case_id)
        if not isinstance(seed_id, str):
            raise ValueError(f"critic case has no private seed mapping: {case_id}")
        critical = verdict.get("critical_flags")
        noncritical = verdict.get("noncritical_flags")
        if not isinstance(critical, list) or not all(isinstance(flag, str) for flag in critical):
            raise ValueError("critic critical_flags are malformed")
        if not isinstance(noncritical, list) or not all(isinstance(flag, str) for flag in noncritical):
            raise ValueError("critic noncritical_flags are malformed")
        flags_by_seed[seed_id].update(critical)
        flags_by_seed[seed_id].update(noncritical)
    return {seed_id: frozenset(flags) for seed_id, flags in sorted(flags_by_seed.items())}


def _repair_record(
    target: Mapping[str, Any],
    plan: Mapping[str, Any],
    flags: frozenset[str],
    prompt_hash: str,
) -> dict[str, Any]:
    unsupported = sorted(flags.difference(EXPECTED_V4_CRITICAL_FLAGS | REPAIRABLE_V4_NONCRITICAL_FLAGS))
    if unsupported:
        raise ValueError(f"unsupported v4 critic flag: {unsupported[0]}")
    critical = flags.intersection(EXPECTED_V4_CRITICAL_FLAGS)
    if critical:
        raise ValueError(f"critical v4 target reached repair path: {target['seed_id']}")
    try:
        repaired_document = repair_document(
            document_from_dict(target["target_ast"]),
            plan,
            set(flags),
        )
    except ValueError as error:
        raise ValueError(f"{target['seed_id']}: {error}") from error
    repaired = copy.deepcopy(dict(target))
    repaired["canonical_document_id"] = f"advrepair_{target['seed_id']}"
    repaired["target_writer_agent"] = WRITER_ID
    repaired["target_prompt_hash"] = prompt_hash
    repaired["target_ast"] = document_to_dict(repaired_document)
    repaired["target_sst"] = render_sst(repaired_document)
    repaired["target_plain_text"] = render_plain_text(repaired_document)
    repaired["target_markdown"] = render_markdown(repaired_document)
    repaired["target_html"] = render_html(repaired_document)
    repaired["source_text"] = repaired["target_plain_text"]
    return validate_canonical_record(repaired)


def _assigned_input_batches() -> list[dict[str, str]]:
    paths = (
        *sorted(PLAN_ROOT.rglob("plans.jsonl")),
        *TARGET_INPUT_PATHS,
        V4_REVIEWS_PATH,
        V4_INDEX_PATH,
        V3_REVIEWS_PATH,
        V3_INDEX_PATH,
        HANDOFF_PATH,
    )
    return [
        {
            "path": repository_relative(path),
            "sha256": "sha256:" + sha256_bytes(path.read_bytes()),
        }
        for path in paths
    ]


def build() -> BuildResult:
    plans = load_plans()
    targets = load_targets()
    v4_flags = review_flags(
        V4_REVIEWS_PATH,
        V4_INDEX_PATH,
        expected_reviews_sha256=EXPECTED_V4_REVIEWS_SHA256,
        expected_index_sha256=EXPECTED_V4_INDEX_SHA256,
        expected_package_sha256=EXPECTED_V4_PACKAGE_SHA256,
    )
    v3_flags = review_flags(
        V3_REVIEWS_PATH,
        V3_INDEX_PATH,
        expected_reviews_sha256=EXPECTED_V3_REVIEWS_SHA256,
        expected_index_sha256=EXPECTED_V3_INDEX_SHA256,
        expected_package_sha256=EXPECTED_V3_PACKAGE_SHA256,
    )
    rejected_seed_ids = {seed_id for seed_id, flags in v3_flags.items() if "pronoun_referent_error" in flags}
    if len(rejected_seed_ids) != EXPECTED_REJECTED_PRONOUN_PLANS:
        raise ValueError("pinned v3 pronoun-referent rejection count differs from the expected 141")
    v4_critical_seed_ids = {
        seed_id for seed_id, flags in v4_flags.items() if flags.intersection(EXPECTED_V4_CRITICAL_FLAGS)
    }
    if not v4_critical_seed_ids.issubset(rejected_seed_ids):
        missing = sorted(v4_critical_seed_ids.difference(rejected_seed_ids))[0]
        raise ValueError(f"v4 critical seed is not covered by fail-closed rejection: {missing}")

    prompt_hash = "sha256:" + sha256_bytes(HANDOFF_PATH.read_bytes())
    records: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    repair_counts: Counter[str] = Counter()
    changed_plain_count = 0
    changed_html_count = 0
    expanded_variant_count = 0
    for seed_id, target in sorted(targets.items()):
        plan = plans.get(seed_id)
        if plan is None:
            raise ValueError(f"target has no matching plan: {seed_id}")
        if seed_id in rejected_seed_ids:
            rejects.append(
                {
                    "schema_version": 1,
                    "seed_id": seed_id,
                    "decision": "reject",
                    "critical_flags": ["pronoun_referent_error"],
                    "source_package_sha256": EXPECTED_V3_PACKAGE_SHA256,
                    "reason": "ambiguous_or_inconsistent_pronoun_plan_not_safe_to_repair",
                }
            )
            continue
        flags = v4_flags.get(seed_id, frozenset())
        repaired = _repair_record(target, plan, flags, prompt_hash)
        if repaired["target_plain_text"] != target["target_plain_text"]:
            changed_plain_count += 1
        if repaired["target_html"] != target["target_html"]:
            changed_html_count += 1
        repair_counts.update(flags)
        variants = expand_canonical_variants(plan, repaired)
        if len(variants) != VARIANT_COUNT:
            raise ValueError(f"canonical expansion count drift for {seed_id}")
        expanded_variant_count += len(variants)
        records.append(repaired)

    if len(records) != EXPECTED_OUTPUT_TARGETS:
        raise ValueError(f"expected {EXPECTED_OUTPUT_TARGETS} repaired targets, found {len(records)}")
    targets_bytes = ("\n".join(canonical_json(record) for record in records) + "\n").encode("utf-8")
    rejects_bytes = ("\n".join(canonical_json(record) for record in rejects) + "\n").encode("utf-8")
    manifest = {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "writer_version": WRITER_VERSION,
        "target_prompt_hash": prompt_hash,
        "record_count": len(records),
        "rejected_seed_count": len(rejects),
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in records}),
        "targets_sha256": "sha256:" + sha256_bytes(targets_bytes),
        "rejects_sha256": "sha256:" + sha256_bytes(rejects_bytes),
        "assigned_input_batches": _assigned_input_batches(),
        "critic_inputs": {
            "v3_package_sha256": EXPECTED_V3_PACKAGE_SHA256,
            "v3_reviews_sha256": EXPECTED_V3_REVIEWS_SHA256,
            "v4_package_sha256": EXPECTED_V4_PACKAGE_SHA256,
            "v4_reviews_sha256": EXPECTED_V4_REVIEWS_SHA256,
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
            "no_human_curation": True,
            "no_generative_api": True,
            "no_external_corpus": True,
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
        "input_target_count": len(targets),
        "output_target_count": len(records),
        "rejected_pronoun_plan_count": len(rejects),
        "v4_critical_seed_count": len(v4_critical_seed_ids),
        "v4_critical_subset_rejected": True,
        "all_records_canonical_valid": True,
        "all_records_expand_to_seven_variants": expanded_variant_count == len(records) * VARIANT_COUNT,
        "targets_sha256": manifest["targets_sha256"],
        "rejects_sha256": manifest["rejects_sha256"],
    }
    report = (
        "# Adversarial target repair v4\n\n"
        f"- Input targets: {len(targets)}\n"
        f"- Rejected unsafe pronoun plans: {len(rejects)}\n"
        f"- Repaired targets: {len(records)}\n"
        f"- Plain-text surfaces changed: {changed_plain_count}\n"
        f"- HTML surfaces changed: {changed_html_count}\n"
        f"- Validated canonical variants: {expanded_variant_count}\n"
        f"- Targets SHA-256: `{manifest['targets_sha256']}`\n"
        f"- Rejects SHA-256: `{manifest['rejects_sha256']}`\n"
    )
    return BuildResult(
        records=tuple(records),
        rejects=tuple(rejects),
        targets_bytes=targets_bytes,
        rejects_bytes=rejects_bytes,
        manifest=manifest,
        validation=validation,
        report=report,
    )


def write(result: BuildResult) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TARGETS_PATH.write_bytes(result.targets_bytes)
    REJECTS_PATH.write_bytes(result.rejects_bytes)
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
        first.targets_bytes != second.targets_bytes
        or first.rejects_bytes != second.rejects_bytes
        or canonical_json(first.manifest) != canonical_json(second.manifest)
    ):
        raise RuntimeError("adversarial target repair is not byte-deterministic")
    write(first)
    print(
        canonical_json(
            {
                "status": "complete",
                "records": len(first.records),
                "rejected": len(first.rejects),
                "targets_sha256": first.manifest["targets_sha256"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
