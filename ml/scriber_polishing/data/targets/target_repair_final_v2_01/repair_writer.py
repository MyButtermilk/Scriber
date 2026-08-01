#!/usr/bin/env python3
"""Build the immutable repair view for the final blinded critic findings."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
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
from scriber_polishing.canonical_expander import (  # noqa: E402
    VARIANT_COUNT,
    expand_canonical_variants,
)
from scriber_polishing.document_ast import BlockType, Document  # noqa: E402
from scriber_polishing.final_critic_repair import (  # noqa: E402
    ARTIFICIAL_CAUSAL_NOUN,
    ARTIFICIAL_MEETING_VALUE,
    ARTIFICIAL_REGISTRATION_LANGUAGE,
    ARTIFICIAL_TICKET_LANGUAGE,
    ENGLISH_RECORD_REFERENCE,
    GRAMMAR_CASE_AGREEMENT,
    HEADING_INITIAL_LOWERCASE,
    MALFORMED_MIXED_LANGUAGE_COMPOUND,
    MISSING_GENERAL_ARTICLE,
    MISSING_REE_LIST_ARTICLE,
    MISSING_REQUIRED_ARTICLE,
    MISSING_TAX_ARTICLE,
    PRESERVE_CODE_SWITCH_LANGUAGE,
    REDUNDANT_COMPLETION_CONDITION,
    REDUNDANT_RELEASE_CONDITION,
    REDUNDANT_REVIEW_PHRASE,
    UNSUPPORTED_UNCHANGED_BLOCK,
    apply_final_critic_repairs,
)
from scriber_polishing.schemas import validate_critic_verdict, validate_seed_plan  # noqa: E402
from scriber_polishing.sst_renderer import render_sst  # noqa: E402
from scriber_polishing.target_batch_validator import _validate_batch  # noqa: E402
from scriber_polishing.target_renderer import (  # noqa: E402
    render_html,
    render_markdown,
    render_plain_text,
)
from scriber_polishing.validators import validate_canonical_record  # noqa: E402

WRITER_ID = "target_repair_final_v2_01"
WRITER_VERSION = 1
PLAN_REPAIR_AGENT = "plan_repair_final_v2_01"
PLAN_REPAIR_BATCH = "final_critic_repair_v2_01"

EXPECTED_RECORD_COUNT = 2665
EXPECTED_VARIANT_COUNT = EXPECTED_RECORD_COUNT * VARIANT_COUNT
EXPECTED_CHANGED_TARGET_COUNT = 1355
EXPECTED_REPAIRED_PLAN_COUNT = 1205
EXPECTED_CHANGED_FACT_PLAN_COUNT = 1055
EXPECTED_CHANGED_STRUCTURE_PLAN_COUNT = 150

EXPECTED_PACKAGE_SHA256 = "8d2e9881f0fff2328c1a030b0c70443ae4ab112ebf370e7d724d4b883b558d31"
EXPECTED_PACKAGE_MANIFEST_SHA256 = "c530a98b8a166ba6e87fe7b0981f6e3ccbd013a6e28b3789a30edb6f842a3f33"
EXPECTED_PRIVATE_INDEX_SHA256 = "8b0d08d5f6f3bda6c0c58c615d6df3702d2423a2160c28ee11ff263a60a8d1a9"

EXPECTED_STYLE_CRITIC_SHA256 = "b0d2109702285dd670c3093f3783d74c9b81a482dda8410f452059c9a79b9448"
EXPECTED_STYLE_REVIEWS_SHA256 = "2bba43e31f347c81a011ba483d61fca2b0e1e79bf0c68b290b6fac91347b02f2"
EXPECTED_STYLE_SUMMARY_SHA256 = "bb0170091d8849e4a21e69aae99baf1719396be360bad33d3a4a0b31b517a5a2"

EXPECTED_SOL1_CRITIC_SHA256 = "2899fd2ac2386714c54fec20b51cacfe08a228963feb545f6188d3a9a2f25caf"
EXPECTED_SOL1_REVIEWS_SHA256 = "a199cc8fb7bee50aadc16f57cfc4d782f74ba5d56f1b1f5695e412d41e2e0be4"
EXPECTED_SOL1_SUMMARY_SHA256 = "ef485d335b37ea677e67858850e9ed69482f39fba765d041d03c2fd3a6daa5a1"

EXPECTED_SOL2_CRITIC_SHA256 = "1ca58b02385758bd5bea23b8263617a7eb6bae32585883ef821ecd25936c7df5"
EXPECTED_SOL2_REVIEWS_SHA256 = "9a812f4be5fc6e4cd98aaa4aca9e73d8f8c055ecdc77f08ea45fd7efa1736ba9"
EXPECTED_SOL2_SUMMARY_SHA256 = "616913a5b745a085d03bd4e26896a25b5f25bafb4f110d358aaf17e307d6682c"

EXPECTED_SOURCE_BINDINGS = {
    "adversarial_manifest": "0287fca8ad83afaf5b9b1785b4b92976e597bdb54bb203d55a12c6e8d0be710d",
    "adversarial_plans": "4ac5e6e40a7f5d40918455170333c5a6e3330bc63afddecca59e27f517cd2c66",
    "adversarial_targets": "42030d8400b8d0aac658e2ef9318d2dbde021ee82ed6b845b1612c708f2e7d79",
    "coverage_manifest": "85190adc137b10328ebb8223b1b2fef632638a682d667aecd94b93568a3c9ae1",
    "coverage_plans": "e4168a7b3fc3fafbccc55320651fcd90009741c38c2e213ae04ba4aa0ccbf86c",
    "coverage_targets": "926dab6a48605fadb06ae52aa3af337cf8d83d3c956068754f64e76318b1fd4c",
}

EXPECTED_REPAIR_SEED_COUNTS = {
    ARTIFICIAL_CAUSAL_NOUN: 25,
    ARTIFICIAL_MEETING_VALUE: 25,
    ARTIFICIAL_REGISTRATION_LANGUAGE: 25,
    ARTIFICIAL_TICKET_LANGUAGE: 10,
    ENGLISH_RECORD_REFERENCE: 180,
    GRAMMAR_CASE_AGREEMENT: 10,
    HEADING_INITIAL_LOWERCASE: 50,
    MALFORMED_MIXED_LANGUAGE_COMPOUND: 10,
    MISSING_GENERAL_ARTICLE: 350,
    MISSING_REE_LIST_ARTICLE: 100,
    MISSING_REQUIRED_ARTICLE: 25,
    MISSING_TAX_ARTICLE: 250,
    PRESERVE_CODE_SWITCH_LANGUAGE: 45,
    REDUNDANT_COMPLETION_CONDITION: 2,
    REDUNDANT_RELEASE_CONDITION: 150,
    REDUNDANT_REVIEW_PHRASE: 75,
    UNSUPPORTED_UNCHANGED_BLOCK: 150,
}
EXPECTED_REPAIR_SCOPE_SHA256 = {
    ARTIFICIAL_CAUSAL_NOUN: "5f6b2663f07c31752c2fb54dda6678038252b971f2254fdf5d98df70ca8d54b6",
    ARTIFICIAL_MEETING_VALUE: "df8a172ba7850f6642c7618f6f22ad96e4c754b7cf470637daba74cc05ebe8fe",
    ARTIFICIAL_REGISTRATION_LANGUAGE: "33e2852e2f9f6e63634538b919ab8e504b3b7dcebde2aa07b89a0f80fde04d70",
    ARTIFICIAL_TICKET_LANGUAGE: "17ffcff09d79fae20c41e956a17fd51fafab5bc6bf403a71172f0f9242c0cca8",
    ENGLISH_RECORD_REFERENCE: "2a7284d83d50daa845d8a9d76ada8c93073935a50a3f6df1a81178bfef262dab",
    GRAMMAR_CASE_AGREEMENT: "bb94eb8d4adceadd263edd01f66a78b627fbc4f9339b398bb99e72e6c7b8697a",
    HEADING_INITIAL_LOWERCASE: "0d5ddedbdff4da6157dc3d9d6de3a0edb0f6a847b363165e64d059ceae580acf",
    MALFORMED_MIXED_LANGUAGE_COMPOUND: "1cf07d9341d147368fa99a94b4f12e786e3a45118099a1650bce3ec376d1f382",
    MISSING_GENERAL_ARTICLE: "42d76fe6a2f6fd9ce99f3841a9a85c0ff2164666a8b90f65f866c4d34eabd322",
    MISSING_REE_LIST_ARTICLE: "2bfdef5b68c3e54b76b9d8afefdcd19f6d2c2bb9f2c4c1ce01aa826b25e2544b",
    MISSING_REQUIRED_ARTICLE: "9c4ed406a0b3ad13338b9a91dee2d83ce3709e7984e53e075280ee8be8d5197c",
    MISSING_TAX_ARTICLE: "a793ff9c3b01ec205754c2bab6210c89ef6909bef488f9b81b6bb717072d5897",
    PRESERVE_CODE_SWITCH_LANGUAGE: "54d2fd537ca7c00d08c85d2a54043bf4c0e7f50acf47a4ab97223282311a6eb0",
    REDUNDANT_COMPLETION_CONDITION: "89d4672470aa6d9fe3d8550ffe59c6cc8e8153b4f78f7c2bc3eb8b6b86d25f65",
    REDUNDANT_RELEASE_CONDITION: "92d5509a2599a12e51771a8f4137e8b4904aee432d7a2c8e044d469dccfcbc05",
    REDUNDANT_REVIEW_PHRASE: "a03a563dce86d03b3cb2eb3ab92e032949fbfd89b521bc0c76f1dd44d71ba09d",
    UNSUPPORTED_UNCHANGED_BLOCK: "d9927cf48216b97e1cb69d7f9871ca63af11d48d4c17aba8783681eefcf2a55e",
}
EXPECTED_REPAIR_UNION_SHA256 = "c3162a86f7ae85fa086781ee1c4085c46d45b2efdf18393fe19e774ae3055704"

EXPECTED_STYLE_FLAG_COUNTS = {
    "artificial_business_phrase": 340,
}
EXPECTED_SOL1_FLAG_COUNTS = {
    "artificial_business_language": 60,
    "awkward_redundant_condition": 227,
    "grammar_case_agreement": 10,
    "heading_initial_lowercase": 50,
    "malformed_mixed_language_compound": 10,
    "missing_required_article": 25,
    "unsupported_target_content": 150,
}
EXPECTED_SOL2_FLAG_COUNTS = {
    "artificial_noun_compound": 25,
    "artificial_redundancy": 227,
    "awkward_technical_compound": 10,
    "grammar_case_error": 3,
    "missing_required_article": 700,
    "preserve_language_violation": 45,
    "unsupported_added_content": 150,
}

SOURCE_ADVERSARIAL_DIR = (
    PROJECT_ROOT / "data" / "targets" / "target_repair_adversarial_v5_02"
)
SOURCE_COVERAGE_DIR = (
    PROJECT_ROOT / "data" / "targets" / "target_repair_coverage_v1_01"
)
PACKAGE_DIR = PROJECT_ROOT / "artifacts" / "critic_package_final_v1"
PACKAGE_PATH = PACKAGE_DIR / "cases.jsonl"
PACKAGE_MANIFEST_PATH = PACKAGE_DIR / "manifest.json"
PRIVATE_INDEX_PATH = (
    PROJECT_ROOT / "artifacts" / "private" / "critic_package_final_v1_index.json"
)
STYLE_REVIEW_DIR = (
    PROJECT_ROOT / "artifacts" / "agent_reviews_final_v1" / "style_terra_max_01"
)
SOL1_REVIEW_DIR = (
    PROJECT_ROOT
    / "artifacts"
    / "agent_reviews_final_v1"
    / "adversarial_sol_max_01"
)
SOL2_REVIEW_DIR = (
    PROJECT_ROOT
    / "artifacts"
    / "agent_reviews_final_v1"
    / "adversarial_sol_max_02"
)
HANDOFF_PATH = PROJECT_ROOT / "handoffs" / "AGENT_FINAL_CRITIC_REPAIR_V2.md"

PLANS_PATH = OUTPUT_DIR / "plans.jsonl"
TARGETS_PATH = OUTPUT_DIR / "targets.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
VALIDATION_PATH = OUTPUT_DIR / "validation.json"
REPORT_PATH = OUTPUT_DIR / "repair_report.md"

_FACT_TEXT_CHANGE_CODES = frozenset(
    {
        ARTIFICIAL_CAUSAL_NOUN,
        ARTIFICIAL_MEETING_VALUE,
        ARTIFICIAL_REGISTRATION_LANGUAGE,
        ARTIFICIAL_TICKET_LANGUAGE,
        ENGLISH_RECORD_REFERENCE,
        GRAMMAR_CASE_AGREEMENT,
        MALFORMED_MIXED_LANGUAGE_COMPOUND,
        MISSING_GENERAL_ARTICLE,
        MISSING_REQUIRED_ARTICLE,
        MISSING_TAX_ARTICLE,
        REDUNDANT_RELEASE_CONDITION,
        REDUNDANT_REVIEW_PHRASE,
    }
)
_STRUCTURE_PLAN_CHANGE_CODES = frozenset(
    {
        HEADING_INITIAL_LOWERCASE,
        MISSING_REE_LIST_ARTICLE,
    }
)
_TAX_ARTICLE_PATTERN = re.compile(
    r"Erläuterung zu (?:Abschreibungsbasis|Aktennotiz|Bemessungsgrundlage|"
    r"Bescheidvergleich|Kontenabstimmung|Mietaufwand|Rückfrage|Verlustvortrag|"
    r"Vorsteuerabzug|Zahlungsstand)"
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
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def repository_relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _scope_hash(seed_ids: set[str] | frozenset[str]) -> str:
    return sha256_bytes(("\n".join(sorted(seed_ids)) + "\n").encode("utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            raise ValueError(f"{repository_relative(path)}:{line_number} is blank")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(
                f"{repository_relative(path)}:{line_number} is not an object"
            )
        rows.append(value)
    return rows


def _load_source_file(
    path: Path,
    expected_hash: str,
) -> dict[str, dict[str, Any]]:
    if sha256_file(path) != expected_hash:
        raise ValueError(f"source hash mismatch: {repository_relative(path)}")
    by_seed: dict[str, dict[str, Any]] = {}
    for row in _load_jsonl(path):
        seed_id = row.get("seed_id")
        if not isinstance(seed_id, str) or seed_id in by_seed:
            raise ValueError(f"invalid or duplicate seed_id in {repository_relative(path)}")
        by_seed[seed_id] = row
    return by_seed


def _load_sources() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    paths = (
        (
            SOURCE_ADVERSARIAL_DIR / "plans.jsonl",
            EXPECTED_SOURCE_BINDINGS["adversarial_plans"],
            "plan",
        ),
        (
            SOURCE_ADVERSARIAL_DIR / "targets.jsonl",
            EXPECTED_SOURCE_BINDINGS["adversarial_targets"],
            "target",
        ),
        (
            SOURCE_COVERAGE_DIR / "plans.jsonl",
            EXPECTED_SOURCE_BINDINGS["coverage_plans"],
            "plan",
        ),
        (
            SOURCE_COVERAGE_DIR / "targets.jsonl",
            EXPECTED_SOURCE_BINDINGS["coverage_targets"],
            "target",
        ),
    )
    plans: dict[str, dict[str, Any]] = {}
    targets: dict[str, dict[str, Any]] = {}
    for path, expected_hash, kind in paths:
        loaded = _load_source_file(path, expected_hash)
        destination = plans if kind == "plan" else targets
        overlap = set(destination).intersection(loaded)
        if overlap:
            raise ValueError(f"source roots overlap at seed {sorted(overlap)[0]}")
        destination.update(loaded)

    manifest_paths = (
        (
            SOURCE_ADVERSARIAL_DIR / "manifest.json",
            EXPECTED_SOURCE_BINDINGS["adversarial_manifest"],
        ),
        (
            SOURCE_COVERAGE_DIR / "manifest.json",
            EXPECTED_SOURCE_BINDINGS["coverage_manifest"],
        ),
    )
    for path, expected_hash in manifest_paths:
        if sha256_file(path) != expected_hash:
            raise ValueError(f"source manifest hash mismatch: {repository_relative(path)}")

    if (
        set(plans) != set(targets)
        or len(plans) != EXPECTED_RECORD_COUNT
        or len(targets) != EXPECTED_RECORD_COUNT
    ):
        raise ValueError("source plans and targets do not have the pinned common scope")
    return plans, targets


def _load_package(
    source_plans: dict[str, dict[str, Any]],
    source_targets: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    if sha256_file(PACKAGE_PATH) != EXPECTED_PACKAGE_SHA256:
        raise ValueError("final critic package hash mismatch")
    if sha256_file(PACKAGE_MANIFEST_PATH) != EXPECTED_PACKAGE_MANIFEST_SHA256:
        raise ValueError("final critic package manifest hash mismatch")
    if sha256_file(PRIVATE_INDEX_PATH) != EXPECTED_PRIVATE_INDEX_SHA256:
        raise ValueError("final critic private index hash mismatch")

    manifest = json.loads(PACKAGE_MANIFEST_PATH.read_text(encoding="utf-8"))
    if (
        manifest.get("package_sha256") != EXPECTED_PACKAGE_SHA256
        or manifest.get("case_count") != EXPECTED_RECORD_COUNT
        or manifest.get("private_index_sha256") != EXPECTED_PRIVATE_INDEX_SHA256
    ):
        raise ValueError("final critic package manifest content differs from its pin")
    index = json.loads(PRIVATE_INDEX_PATH.read_text(encoding="utf-8"))
    if (
        not isinstance(index, dict)
        or len(index) != EXPECTED_RECORD_COUNT
        or len(set(index.values())) != EXPECTED_RECORD_COUNT
    ):
        raise ValueError("final critic private index is not a one-to-one mapping")

    cases_by_seed: dict[str, dict[str, Any]] = {}
    for case in _load_jsonl(PACKAGE_PATH):
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id not in index:
            raise ValueError("critic package contains an unindexed case")
        seed_id = index[case_id]
        if seed_id in cases_by_seed or seed_id not in source_plans:
            raise ValueError("critic package maps to a duplicate or unknown seed")
        if case.get("semantic_plan") != source_plans[seed_id]["semantic_plan"]:
            raise ValueError(f"{seed_id}: package semantic plan differs from source")
        if case.get("target_ast") != source_targets[seed_id]["target_ast"]:
            raise ValueError(f"{seed_id}: package target AST differs from source")
        cases_by_seed[seed_id] = case
    if set(cases_by_seed) != set(source_plans):
        raise ValueError("critic package does not cover the complete source union")
    return cases_by_seed, {str(key): str(value) for key, value in index.items()}


def _load_reviews(
    *,
    review_dir: Path,
    expected_critic_hash: str,
    expected_reviews_hash: str,
    expected_summary_hash: str,
    expected_critic_id: str,
    expected_flag_counts: dict[str, int],
    index: dict[str, str],
) -> dict[str, frozenset[str]]:
    critic_path = review_dir / "critic.py"
    reviews_path = review_dir / "reviews.jsonl"
    summary_path = review_dir / "summary.json"
    expected = (
        (critic_path, expected_critic_hash),
        (reviews_path, expected_reviews_hash),
        (summary_path, expected_summary_hash),
    )
    for path, expected_hash in expected:
        if sha256_file(path) != expected_hash:
            raise ValueError(f"review binding mismatch: {repository_relative(path)}")

    rows = [validate_critic_verdict(row) for row in _load_jsonl(reviews_path)]
    if len(rows) != EXPECTED_RECORD_COUNT:
        raise ValueError(f"{review_dir.name}: review count differs from package")
    by_case: dict[str, dict[str, Any]] = {}
    flag_counts: Counter[str] = Counter()
    for row in rows:
        case_id = row["case_id"]
        if case_id in by_case or case_id not in index:
            raise ValueError(f"{review_dir.name}: duplicate or unknown case")
        flags = tuple(row["critical_flags"]) + tuple(row["noncritical_flags"])
        if row["critic_id"] != expected_critic_id:
            raise ValueError(f"{review_dir.name}: critic identity differs from pin")
        if row["reviewed_package_sha256"] != EXPECTED_PACKAGE_SHA256:
            raise ValueError(f"{review_dir.name}: reviewed package hash differs")
        if row["acceptable"] != (not flags):
            raise ValueError(f"{review_dir.name}: acceptability disagrees with flags")
        flag_counts.update(flags)
        by_case[case_id] = row
    if set(by_case) != set(index) or dict(sorted(flag_counts.items())) != dict(
        sorted(expected_flag_counts.items())
    ):
        raise ValueError(f"{review_dir.name}: final flag scope differs from pin")
    return {
        index[case_id]: frozenset(
            tuple(row["critical_flags"]) + tuple(row["noncritical_flags"])
        )
        for case_id, row in by_case.items()
    }


def _target_text(case: dict[str, Any]) -> str:
    value = case.get("target_plain_text")
    if not isinstance(value, str):
        raise ValueError("critic package case has no target_plain_text")
    return value


def _fact_texts(case: dict[str, Any]) -> list[str]:
    return [str(fact["text"]) for fact in case["semantic_plan"]["facts"]]


def _list_items(case: dict[str, Any]) -> list[str]:
    return [str(item) for item in case["semantic_plan"]["structure"]["list_items"]]


def _classify_style(case: dict[str, Any], flags: frozenset[str]) -> set[str]:
    if not flags:
        return set()
    if flags != frozenset({"artificial_business_phrase"}):
        raise ValueError(f"{case['case_id']}: unexpected style critic flags")
    text = _target_text(case)
    block_text = "\n".join(
        str(block.get("text", "")) for block in case["target_ast"]["blocks"]
    )
    matches: list[str] = []
    if re.search(r"^Fictional .+ records reference ", block_text, re.MULTILINE):
        matches.append(ENGLISH_RECORD_REFERENCE)
    if (
        "bleibt vorbehaltlich der Freigabe bestehen, wenn die Freigabe vorliegt."
        in text
    ):
        matches.append(REDUNDANT_RELEASE_CONDITION)
    if "Das Meeting verweist auf " in text and " und den Wert " in text:
        matches.append(ARTIFICIAL_MEETING_VALUE)
    if len(matches) != 1:
        raise ValueError(f"{case['case_id']}: style finding is not closed")
    return {matches[0]}


def _classify_redundancy(case: dict[str, Any]) -> str:
    text = _target_text(case)
    matches: list[str] = []
    if (
        "bleibt vorbehaltlich der Freigabe bestehen, wenn die Freigabe vorliegt."
        in text
    ):
        matches.append(REDUNDANT_RELEASE_CONDITION)
    if (
        "Nach vollständiger Prüfung kann die Rückmeldung nach Prüfung der "
        "Unterlagen erfolgen"
    ) in text:
        matches.append(REDUNDANT_REVIEW_PHRASE)
    if (
        "Die Prüfung ist vollständig, sodass die Freigabe erfolgen kann. "
        "Dies gilt, wenn die Prüfung abgeschlossen ist."
    ) in text:
        matches.append(REDUNDANT_COMPLETION_CONDITION)
    if len(matches) != 1:
        raise ValueError(f"{case['case_id']}: redundancy finding is not closed")
    return matches[0]


def _classify_artificial_language(case: dict[str, Any]) -> str:
    text = _target_text(case)
    matches: list[str] = []
    if "meeting" in text.casefold() and (
        "the value" in text or "den Wert" in text
    ):
        matches.append(ARTIFICIAL_MEETING_VALUE)
    if (
        "Registrations use only " in text
        or "Anmeldungen verwenden ausschließlich " in text
    ):
        matches.append(ARTIFICIAL_REGISTRATION_LANGUAGE)
    if "Die Übergabe ordnet das Ticket " in text and " und schützt " in text:
        matches.append(ARTIFICIAL_TICKET_LANGUAGE)
    if len(matches) != 1:
        raise ValueError(
            f"{case['case_id']}: artificial-business finding is not closed"
        )
    return matches[0]


def _classify_sol1(case: dict[str, Any], flags: frozenset[str]) -> set[str]:
    codes: set[str] = set()
    for flag in flags:
        if flag == "unsupported_target_content":
            codes.add(UNSUPPORTED_UNCHANGED_BLOCK)
        elif flag == "artificial_business_language":
            codes.add(_classify_artificial_language(case))
        elif flag == "awkward_redundant_condition":
            codes.add(_classify_redundancy(case))
        elif flag == "grammar_case_agreement":
            codes.add(GRAMMAR_CASE_AGREEMENT)
        elif flag == "heading_initial_lowercase":
            codes.add(HEADING_INITIAL_LOWERCASE)
        elif flag == "malformed_mixed_language_compound":
            codes.add(MALFORMED_MIXED_LANGUAGE_COMPOUND)
        elif flag == "missing_required_article":
            codes.add(MISSING_REQUIRED_ARTICLE)
        else:
            raise ValueError(f"{case['case_id']}: unexpected Sol-1 flag {flag}")
    return codes


def _classify_missing_article(case: dict[str, Any]) -> str:
    facts = _fact_texts(case)
    list_items = _list_items(case)
    matches: list[str] = []
    if any(
        any(
            marker in text
            for marker in (
                "bestätigt für Vorgang ",
                "hält für Vorgang ",
                "bestätigt für Referenz ",
                "zu Vorgang OP-",
            )
        )
        for text in facts
    ):
        matches.append(MISSING_GENERAL_ARTICLE)
    if any(
        any(
            marker in item
            for marker in (
                "Zuordnung zu Nachtrag ",
                "Zuordnung zu Prüfvermerk ",
                "Zuordnung zu Vorgang ",
            )
        )
        for item in list_items
    ):
        matches.append(MISSING_REE_LIST_ARTICLE)
    if any(_TAX_ARTICLE_PATTERN.search(text) for text in facts):
        matches.append(MISSING_TAX_ARTICLE)
    if len(matches) != 1:
        raise ValueError(f"{case['case_id']}: missing-article finding is not closed")
    return matches[0]


def _classify_sol2(case: dict[str, Any], flags: frozenset[str]) -> set[str]:
    codes: set[str] = set()
    for flag in flags:
        if flag == "unsupported_added_content":
            codes.add(UNSUPPORTED_UNCHANGED_BLOCK)
        elif flag == "preserve_language_violation":
            codes.add(PRESERVE_CODE_SWITCH_LANGUAGE)
        elif flag == "missing_required_article":
            codes.add(_classify_missing_article(case))
        elif flag == "artificial_redundancy":
            codes.add(_classify_redundancy(case))
        elif flag == "grammar_case_error":
            codes.add(GRAMMAR_CASE_AGREEMENT)
        elif flag == "artificial_noun_compound":
            codes.add(ARTIFICIAL_CAUSAL_NOUN)
        elif flag == "awkward_technical_compound":
            codes.add(MALFORMED_MIXED_LANGUAGE_COMPOUND)
        else:
            raise ValueError(f"{case['case_id']}: unexpected Sol-2 flag {flag}")
    return codes


def _unsupported_package_scope(
    cases_by_seed: dict[str, dict[str, Any]],
) -> frozenset[str]:
    scope = {
        seed_id
        for seed_id, case in cases_by_seed.items()
        if sum(
            block.get("type") == "paragraph"
            and block.get("text") == "Inhalt bleibt unverändert."
            for block in case["target_ast"]["blocks"]
        )
        == 1
    }
    if (
        len(scope) != EXPECTED_REPAIR_SEED_COUNTS[UNSUPPORTED_UNCHANGED_BLOCK]
        or _scope_hash(scope)
        != EXPECTED_REPAIR_SCOPE_SHA256[UNSUPPORTED_UNCHANGED_BLOCK]
    ):
        raise ValueError("independent unsupported-content package scope differs")
    return frozenset(scope)


def _repair_scopes(
    cases_by_seed: dict[str, dict[str, Any]],
    style_reviews: dict[str, frozenset[str]],
    sol1_reviews: dict[str, frozenset[str]],
    sol2_reviews: dict[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    by_code: dict[str, set[str]] = defaultdict(set)
    unsupported_scope = _unsupported_package_scope(cases_by_seed)
    for seed_id in sorted(cases_by_seed):
        case = cases_by_seed[seed_id]
        codes = _classify_style(case, style_reviews[seed_id])
        codes.update(_classify_sol1(case, sol1_reviews[seed_id]))
        codes.update(_classify_sol2(case, sol2_reviews[seed_id]))
        if seed_id in unsupported_scope:
            codes.add(UNSUPPORTED_UNCHANGED_BLOCK)
        for code in codes:
            by_code[code].add(seed_id)

    frozen = {code: frozenset(scope) for code, scope in by_code.items()}
    if set(frozen) != set(EXPECTED_REPAIR_SEED_COUNTS):
        raise ValueError("final repair categories differ from the closed contract")
    for code, expected_count in EXPECTED_REPAIR_SEED_COUNTS.items():
        scope = frozen[code]
        if (
            len(scope) != expected_count
            or _scope_hash(scope) != EXPECTED_REPAIR_SCOPE_SHA256[code]
        ):
            raise ValueError(f"{code}: repair scope differs from pin")
    union = frozenset().union(*frozen.values())
    if (
        len(union) != EXPECTED_CHANGED_TARGET_COUNT
        or _scope_hash(union) != EXPECTED_REPAIR_UNION_SHA256
    ):
        raise ValueError("final repair union differs from pin")

    sol1_unsupported = frozenset(
        seed_id
        for seed_id, flags in sol1_reviews.items()
        if "unsupported_target_content" in flags
    )
    sol2_unsupported = frozenset(
        seed_id
        for seed_id, flags in sol2_reviews.items()
        if "unsupported_added_content" in flags
    )
    if sol1_unsupported != unsupported_scope or sol2_unsupported != unsupported_scope:
        raise ValueError("independent unsupported scope disagrees with final critics")
    return frozen


def _semantic_plan_hash(plan: dict[str, Any]) -> str:
    return "sha256:" + sha256_bytes(
        canonical_json(plan["semantic_plan"]).encode("utf-8")
    )


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
    return [
        value
        for value in dict.fromkeys(candidates)
        if _contains_surface(plain_text, value)
    ]


def _assert_semantic_invariants(
    seed_id: str,
    source_plan: dict[str, Any],
    repaired_plan: dict[str, Any],
    codes: frozenset[str],
) -> None:
    source_semantic = source_plan["semantic_plan"]
    repaired_semantic = repaired_plan["semantic_plan"]
    if source_semantic["entities"] != repaired_semantic["entities"]:
        raise ValueError(f"{seed_id}: repair changed a declared entity")
    source_facts = source_semantic["facts"]
    repaired_facts = repaired_semantic["facts"]
    if len(source_facts) != len(repaired_facts):
        raise ValueError(f"{seed_id}: repair changed the fact count")
    for source_fact, repaired_fact in zip(source_facts, repaired_facts, strict=True):
        source_metadata = {
            key: value for key, value in source_fact.items() if key != "text"
        }
        repaired_metadata = {
            key: value for key, value in repaired_fact.items() if key != "text"
        }
        if source_metadata != repaired_metadata:
            raise ValueError(f"{seed_id}: repair changed fact identity or semantics")

    facts_changed = source_facts != repaired_facts
    if facts_changed != bool(codes.intersection(_FACT_TEXT_CHANGE_CODES)):
        raise ValueError(f"{seed_id}: fact wording changed outside its closed scope")
    source_structure = source_semantic["structure"]
    repaired_structure = repaired_semantic["structure"]
    for key in source_structure:
        if key in {"paragraph_topics", "list_items"}:
            continue
        if source_structure[key] != repaired_structure[key]:
            raise ValueError(f"{seed_id}: repair changed document structure metadata")
    structure_changed = source_structure != repaired_structure
    if structure_changed != bool(codes.intersection(_STRUCTURE_PLAN_CHANGE_CODES)):
        raise ValueError(f"{seed_id}: structure wording changed outside its scope")


def _assert_document_order(
    seed_id: str,
    source_document: Document,
    repaired_document: Document,
    codes: frozenset[str],
) -> None:
    source_types = tuple(block.type for block in source_document.blocks)
    repaired_types = tuple(block.type for block in repaired_document.blocks)
    if UNSUPPORTED_UNCHANGED_BLOCK in codes:
        removed = list(source_types)
        phrase_positions = [
            index
            for index, block in enumerate(source_document.blocks)
            if block.type is BlockType.PARAGRAPH
            and block.text == "Inhalt bleibt unverändert."
        ]
        if len(phrase_positions) != 1:
            raise ValueError(f"{seed_id}: unsupported block position differs")
        removed.pop(phrase_positions[0])
        if tuple(removed) != repaired_types:
            raise ValueError(f"{seed_id}: unsupported repair changed block order")
    elif source_types != repaired_types:
        raise ValueError(f"{seed_id}: repair changed block order")

    source_list_shape = tuple(
        (block.type, tuple(item.level for item in block.items))
        for block in source_document.blocks
        if block.items
    )
    repaired_list_shape = tuple(
        (block.type, tuple(item.level for item in block.items))
        for block in repaired_document.blocks
        if block.items
    )
    if source_list_shape != repaired_list_shape:
        raise ValueError(f"{seed_id}: repair changed list type, hierarchy, or order")


def _repair_target_record(
    source_target: dict[str, Any],
    plan: dict[str, Any],
    document: Document,
    prompt_hash: str,
) -> dict[str, Any]:
    repaired = copy.deepcopy(source_target)
    repaired["canonical_document_id"] = (
        f"finalrepair_v2_01_{source_target['seed_id']}"
    )
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


def _assigned_input_batches() -> list[dict[str, str]]:
    paths = [
        SOURCE_ADVERSARIAL_DIR / "plans.jsonl",
        SOURCE_ADVERSARIAL_DIR / "targets.jsonl",
        SOURCE_ADVERSARIAL_DIR / "manifest.json",
        SOURCE_COVERAGE_DIR / "plans.jsonl",
        SOURCE_COVERAGE_DIR / "targets.jsonl",
        SOURCE_COVERAGE_DIR / "manifest.json",
        PACKAGE_PATH,
        PACKAGE_MANIFEST_PATH,
        PRIVATE_INDEX_PATH,
        STYLE_REVIEW_DIR / "critic.py",
        STYLE_REVIEW_DIR / "reviews.jsonl",
        STYLE_REVIEW_DIR / "summary.json",
        SOL1_REVIEW_DIR / "critic.py",
        SOL1_REVIEW_DIR / "reviews.jsonl",
        SOL1_REVIEW_DIR / "summary.json",
        SOL2_REVIEW_DIR / "critic.py",
        SOL2_REVIEW_DIR / "reviews.jsonl",
        SOL2_REVIEW_DIR / "summary.json",
        HANDOFF_PATH,
        PROJECT_ROOT / "src" / "scriber_polishing" / "final_critic_repair.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "canonical_expander.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "sst_renderer.py",
        PROJECT_ROOT / "src" / "scriber_polishing" / "target_renderer.py",
        Path(__file__).resolve(),
    ]
    return [
        {
            "path": repository_relative(path),
            "sha256": "sha256:" + sha256_file(path),
        }
        for path in paths
    ]


def build() -> BuildResult:
    source_plans, source_targets = _load_sources()
    cases_by_seed, index = _load_package(source_plans, source_targets)
    style_reviews = _load_reviews(
        review_dir=STYLE_REVIEW_DIR,
        expected_critic_hash=EXPECTED_STYLE_CRITIC_SHA256,
        expected_reviews_hash=EXPECTED_STYLE_REVIEWS_SHA256,
        expected_summary_hash=EXPECTED_STYLE_SUMMARY_SHA256,
        expected_critic_id="final_style_terra_max_01",
        expected_flag_counts=EXPECTED_STYLE_FLAG_COUNTS,
        index=index,
    )
    sol1_reviews = _load_reviews(
        review_dir=SOL1_REVIEW_DIR,
        expected_critic_hash=EXPECTED_SOL1_CRITIC_SHA256,
        expected_reviews_hash=EXPECTED_SOL1_REVIEWS_SHA256,
        expected_summary_hash=EXPECTED_SOL1_SUMMARY_SHA256,
        expected_critic_id="final_adversarial_sol_max_01",
        expected_flag_counts=EXPECTED_SOL1_FLAG_COUNTS,
        index=index,
    )
    sol2_reviews = _load_reviews(
        review_dir=SOL2_REVIEW_DIR,
        expected_critic_hash=EXPECTED_SOL2_CRITIC_SHA256,
        expected_reviews_hash=EXPECTED_SOL2_REVIEWS_SHA256,
        expected_summary_hash=EXPECTED_SOL2_SUMMARY_SHA256,
        expected_critic_id="final_adversarial_sol_max_02",
        expected_flag_counts=EXPECTED_SOL2_FLAG_COUNTS,
        index=index,
    )
    scopes = _repair_scopes(
        cases_by_seed,
        style_reviews,
        sol1_reviews,
        sol2_reviews,
    )
    prompt_hash = "sha256:" + sha256_file(HANDOFF_PATH)

    plans: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    changed_seed_ids: set[str] = set()
    repaired_plan_count = 0
    changed_fact_plan_count = 0
    changed_structure_plan_count = 0
    changed_plain_count = 0
    changed_markdown_count = 0
    changed_html_count = 0
    expanded_variant_count = 0
    applied_occurrences: Counter[str] = Counter()

    for seed_id in sorted(source_plans):
        source_plan = source_plans[seed_id]
        source_target = source_targets[seed_id]
        codes = frozenset(
            code for code, scope in scopes.items() if seed_id in scope
        )
        source_document = document_from_dict(source_target["target_ast"])
        outcome = apply_final_critic_repairs(source_plan, source_document, codes)
        plan = outcome.plan
        document = outcome.document
        applied_occurrences.update(dict(outcome.applied_occurrences))

        _assert_semantic_invariants(seed_id, source_plan, plan, codes)
        _assert_document_order(seed_id, source_document, document, codes)
        plan_changed = plan["semantic_plan"] != source_plan["semantic_plan"]
        fact_plan_changed = (
            plan["semantic_plan"]["facts"]
            != source_plan["semantic_plan"]["facts"]
        )
        structure_plan_changed = (
            plan["semantic_plan"]["structure"]
            != source_plan["semantic_plan"]["structure"]
        )
        if plan_changed:
            plan["generator_agent"] = PLAN_REPAIR_AGENT
            plan["batch_id"] = PLAN_REPAIR_BATCH
            plan["prompt_hash"] = prompt_hash
            repaired_plan_count += 1
        changed_fact_plan_count += fact_plan_changed
        changed_structure_plan_count += structure_plan_changed
        validated_plan = validate_seed_plan(plan)

        repaired = _repair_target_record(
            source_target,
            validated_plan,
            document,
            prompt_hash,
        )
        ast_changed = repaired["target_ast"] != source_target["target_ast"]
        if ast_changed:
            changed_seed_ids.add(seed_id)
        changed_plain_count += (
            repaired["target_plain_text"] != source_target["target_plain_text"]
        )
        changed_markdown_count += (
            repaired["target_markdown"] != source_target["target_markdown"]
        )
        changed_html_count += (
            repaired["target_html"] != source_target["target_html"]
        )

        variants = expand_canonical_variants(validated_plan, repaired)
        if len(variants) != VARIANT_COUNT:
            raise ValueError(f"{seed_id}: canonical expansion is not seven variants")
        expanded_variant_count += len(variants)
        plans.append(validated_plan)
        records.append(repaired)

    if (
        len(plans) != EXPECTED_RECORD_COUNT
        or len(records) != EXPECTED_RECORD_COUNT
        or len(changed_seed_ids) != EXPECTED_CHANGED_TARGET_COUNT
        or _scope_hash(changed_seed_ids) != EXPECTED_REPAIR_UNION_SHA256
        or repaired_plan_count != EXPECTED_REPAIRED_PLAN_COUNT
        or changed_fact_plan_count != EXPECTED_CHANGED_FACT_PLAN_COUNT
        or changed_structure_plan_count != EXPECTED_CHANGED_STRUCTURE_PLAN_COUNT
        or changed_plain_count != EXPECTED_CHANGED_TARGET_COUNT
        or changed_markdown_count != EXPECTED_CHANGED_TARGET_COUNT
        or changed_html_count != EXPECTED_CHANGED_TARGET_COUNT
        or expanded_variant_count != EXPECTED_VARIANT_COUNT
    ):
        observed_counts = {
            "changed_fact_plan_count": changed_fact_plan_count,
            "changed_html_count": changed_html_count,
            "changed_markdown_count": changed_markdown_count,
            "changed_plain_count": changed_plain_count,
            "changed_structure_plan_count": changed_structure_plan_count,
            "changed_target_count": len(changed_seed_ids),
            "expanded_variant_count": expanded_variant_count,
            "plan_count": len(plans),
            "record_count": len(records),
            "repaired_plan_count": repaired_plan_count,
        }
        raise ValueError(
            "final repair output counts differ from the closed contract: "
            f"{observed_counts}"
        )

    plans_bytes = (
        "\n".join(canonical_json(plan) for plan in plans) + "\n"
    ).encode("utf-8")
    targets_bytes = (
        "\n".join(canonical_json(record) for record in records) + "\n"
    ).encode("utf-8")
    manifest = {
        "manifest_version": 1,
        "writer_id": WRITER_ID,
        "writer_version": WRITER_VERSION,
        "target_prompt_hash": prompt_hash,
        "record_count": len(records),
        "plan_count": len(plans),
        "repaired_plan_count": repaired_plan_count,
        "changed_fact_plan_count": changed_fact_plan_count,
        "changed_structure_plan_count": changed_structure_plan_count,
        "changed_target_count": len(changed_seed_ids),
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "unique_canonical_document_id_count": len(
            {record["canonical_document_id"] for record in records}
        ),
        "plans_sha256": "sha256:" + sha256_bytes(plans_bytes),
        "targets_sha256": "sha256:" + sha256_bytes(targets_bytes),
        "assigned_input_batches": _assigned_input_batches(),
        "source_bindings": {
            key: "sha256:" + value
            for key, value in sorted(EXPECTED_SOURCE_BINDINGS.items())
        },
        "critic_bindings": {
            "package_sha256": "sha256:" + EXPECTED_PACKAGE_SHA256,
            "package_manifest_sha256": (
                "sha256:" + EXPECTED_PACKAGE_MANIFEST_SHA256
            ),
            "private_index_sha256": "sha256:" + EXPECTED_PRIVATE_INDEX_SHA256,
            "style": {
                "critic_sha256": "sha256:" + EXPECTED_STYLE_CRITIC_SHA256,
                "reviews_sha256": "sha256:" + EXPECTED_STYLE_REVIEWS_SHA256,
                "summary_sha256": "sha256:" + EXPECTED_STYLE_SUMMARY_SHA256,
            },
            "adversarial_sol_1": {
                "critic_sha256": "sha256:" + EXPECTED_SOL1_CRITIC_SHA256,
                "reviews_sha256": "sha256:" + EXPECTED_SOL1_REVIEWS_SHA256,
                "summary_sha256": "sha256:" + EXPECTED_SOL1_SUMMARY_SHA256,
            },
            "adversarial_sol_2": {
                "critic_sha256": "sha256:" + EXPECTED_SOL2_CRITIC_SHA256,
                "reviews_sha256": "sha256:" + EXPECTED_SOL2_REVIEWS_SHA256,
                "summary_sha256": "sha256:" + EXPECTED_SOL2_SUMMARY_SHA256,
            },
        },
        "repair_seed_counts": dict(sorted(EXPECTED_REPAIR_SEED_COUNTS.items())),
        "repair_occurrence_counts": dict(sorted(applied_occurrences.items())),
        "repair_scope_sha256": {
            code: "sha256:" + digest
            for code, digest in sorted(EXPECTED_REPAIR_SCOPE_SHA256.items())
        },
        "repair_union": {
            "seed_count": len(changed_seed_ids),
            "seed_scope_sha256": "sha256:" + EXPECTED_REPAIR_UNION_SHA256,
        },
        "canonical_expansion": {
            "validated_target_count": len(records),
            "variants_per_target": VARIANT_COUNT,
            "validated_variant_count": expanded_variant_count,
        },
        "semantic_invariants": {
            "entities_unchanged": True,
            "fact_count_and_order_unchanged": True,
            "fact_identity_modality_polarity_condition_unchanged": True,
            "protected_values_unchanged": True,
            "block_type_and_order_unchanged_except_exact_unsupported_removal": True,
            "list_type_hierarchy_count_and_order_unchanged": True,
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
        "source_target_count": len(source_targets),
        "package_case_count": len(cases_by_seed),
        "output_plan_count": len(plans),
        "output_target_count": len(records),
        "changed_target_count": len(changed_seed_ids),
        "repaired_plan_count": repaired_plan_count,
        "changed_fact_plan_count": changed_fact_plan_count,
        "changed_structure_plan_count": changed_structure_plan_count,
        "all_plans_schema_valid": True,
        "all_targets_canonical_valid": True,
        "all_targets_expand_to_seven_variants": (
            expanded_variant_count == len(records) * VARIANT_COUNT
        ),
        "validated_variant_count": expanded_variant_count,
        "all_genuine_final_findings_repaired": True,
        "calibrated_false_positive_scopes_unchanged": True,
        "plans_sha256": manifest["plans_sha256"],
        "targets_sha256": manifest["targets_sha256"],
    }
    report_lines = [
        "# Final critic repair v2.01",
        "",
        f"- Selected plans and targets: {len(records)}",
        f"- Repaired plans: {repaired_plan_count}",
        f"- Targets changed: {len(changed_seed_ids)}",
        f"- Fact-text plans changed: {changed_fact_plan_count}",
        f"- Structure-label plans changed: {changed_structure_plan_count}",
        f"- Unsupported blocks removed: {EXPECTED_REPAIR_SEED_COUNTS[UNSUPPORTED_UNCHANGED_BLOCK]}",
        f"- Validated canonical variants: {expanded_variant_count}",
        "- Human curation, external corpora, generative APIs, splits, training, and evaluation inputs used: none",
        "- Calibrated structure, postscript, protected-lowercase-heading, and unrelated orthopron false positives changed: none",
        f"- Plans SHA-256: `{manifest['plans_sha256']}`",
        f"- Targets SHA-256: `{manifest['targets_sha256']}`",
        "",
        "## Closed repair seed counts",
        "",
    ]
    report_lines.extend(
        f"- {code}: {count}"
        for code, count in sorted(EXPECTED_REPAIR_SEED_COUNTS.items())
    )
    report = "\n".join(report_lines) + "\n"
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
        for name, _payload in payloads:
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
        raise RuntimeError("final critic repair build is not byte-deterministic")
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
