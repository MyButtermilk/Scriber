"""Fail-closed validation seams for canonical polishing data and local policy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .ast_codec import document_from_dict
from .sst_parser import SSTParseError, parse_sst
from .sst_renderer import render_sst
from .target_renderer import render_html, render_markdown, render_plain_text

_PROJECT_ROOT = Path(__file__).parents[2]
_SCHEMA_PATH = _PROJECT_ROOT / "contracts" / "sst_v1_schema.json"
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


def validate_canonical_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed schema plus every deterministic target representation."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(dict(record)), key=lambda error: list(error.path))
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
