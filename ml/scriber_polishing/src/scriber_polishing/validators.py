"""Fail-closed validation seams for canonical polishing data and local policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .ast_codec import document_from_dict
from .sst_parser import SSTParseError, parse_sst
from .sst_renderer import render_sst
from .target_renderer import render_html, render_markdown, render_plain_text

_PROJECT_ROOT = Path(__file__).parents[2]
_SCHEMA_PATH = _PROJECT_ROOT / "contracts" / "sst_v1_schema.json"
_SEED_PLAN_SCHEMA_PATH = _PROJECT_ROOT / "contracts" / "seed_plan_schema.json"
_SCANNED_SUFFIXES = frozenset({".json", ".py", ".toml", ".yaml", ".yml"})
_EXCLUDED_SCAN_PARTS = frozenset({".git", ".venv", "__pycache__", "artifacts", "data", "tests"})
_FORBIDDEN_GENERATIVE_API_PATTERNS = (
    "api." + "openai.com",
    "OPENAI_" + "API_KEY",
    "from " + "openai import",
    "import " + "openai",
    "responses." + "create",
    "chat." + "completions",
    "OPENROUTER_" + "API_KEY",
    "ANTHROPIC_" + "API_KEY",
    "GEMINI_" + "API_KEY",
    "GOOGLE_" + "API_KEY",
    "AWS_BEARER_TOKEN_" + "BEDROCK",
    "AZURE_OPENAI_" + "API_KEY",
)


@cache
def _json_schema_validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def validate_canonical_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed schema plus every deterministic target representation."""
    errors = sorted(
        _json_schema_validator(_SCHEMA_PATH).iter_errors(dict(record)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"canonical record failed SST v1 schema: {errors[0].message}")
    try:
        document = document_from_dict(record["target_ast"])
    except (TypeError, ValueError) as error:
        raise ValueError(f"canonical record AST is invalid: {error}") from error
    try:
        parsed = parse_sst(record["target_sst"])
    except SSTParseError as error:
        raise ValueError(f"canonical record SST is invalid: {error}") from error
    if render_sst(parsed) != record["target_sst"] or render_sst(document) != record["target_sst"]:
        raise ValueError("canonical record AST and SST differ")
    if render_plain_text(document) != record["target_plain_text"]:
        raise ValueError("canonical record plain-text target differs from AST")
    if render_markdown(document) != record["target_markdown"]:
        raise ValueError("canonical record Markdown target differs from AST")
    if render_html(document) != record["target_html"]:
        raise ValueError("canonical record HTML target differs from safe AST rendering")
    if (
        "Das Pronomen „" in record["target_plain_text"]
        and "“ bezieht sich dabei auf " in record["target_plain_text"]
    ):
        raise ValueError("canonical record contains unsupported pronoun commentary")
    if "Die Regelung gilt unter folgender Bedingung:" in record["target_plain_text"]:
        raise ValueError("canonical record contains redundant condition commentary")
    semantic_plan = record.get("semantic_plan")
    if semantic_plan is not None:
        seed_plan_schema = _json_schema_validator(_SEED_PLAN_SCHEMA_PATH).schema
        semantic_plan_schema = seed_plan_schema["properties"]["semantic_plan"]
        plan_errors = sorted(
            Draft202012Validator(semantic_plan_schema).iter_errors(semantic_plan),
            key=lambda error: list(error.path),
        )
        if plan_errors:
            raise ValueError(f"canonical record semantic plan is invalid: {plan_errors[0].message}")
        semantic_plan_payload = json.dumps(
            semantic_plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_hash = "sha256:" + hashlib.sha256(semantic_plan_payload).hexdigest()
        if record["semantic_plan_sha256"] != expected_hash:
            raise ValueError("canonical record semantic plan hash differs from semantic plan")
    for protected_span in record["protected_spans"]:
        if protected_span not in record["target_plain_text"]:
            raise ValueError(f"canonical record protected span is absent from target: {protected_span!r}")
    return dict(record)


def find_forbidden_generative_api_references(project_root: str | Path) -> list[str]:
    """Find forbidden generative API references in executable ML files and configs.

    Policy prose and tests are deliberately outside this narrow scan so Scriber-wide
    provider integrations and documentation cannot create false positives.
    """
    root = Path(project_root)
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _SCANNED_SUFFIXES:
            continue
        relative = path.relative_to(root)
        if any(part in _EXCLUDED_SCAN_PARTS for part in relative.parts):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for pattern in _FORBIDDEN_GENERATIVE_API_PATTERNS:
            if pattern in content:
                findings.append(f"{relative.as_posix()}: {pattern}")
    return findings
