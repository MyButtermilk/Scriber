"""Verify a local selected-checkpoint publication handoff or list later hooks.

Verification is local-only and does not invoke any publication hook.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.checkpoint_selection import (  # noqa: E402
    CheckpointSelectionError,
    canonical_json_sha256,
    load_checkpoint_selection_config,
    publication_integration_hooks,
    validate_selected_checkpoint_publication,
)


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise CheckpointSelectionError(f"{label} must be a JSON object: {path}")
    return dict(value)


def _require(parser: argparse.ArgumentParser, value: Path | None, option: str) -> Path:
    if value is None:
        parser.error(f"{option} is required unless --list-hooks is used")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-hooks", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--publication-evidence", type=Path)
    parser.add_argument("--publication", type=Path)
    args = parser.parse_args(argv)
    if args.list_hooks:
        for hook in publication_integration_hooks():
            print(hook)
        return 0

    config_path = _require(parser, args.config, "--config")
    handoff_path = _require(parser, args.handoff, "--handoff")
    evidence_path = _require(parser, args.publication_evidence, "--publication-evidence")
    publication_path = _require(parser, args.publication, "--publication")
    try:
        config = load_checkpoint_selection_config(config_path)
        handoff = _load_mapping(handoff_path, "checkpoint selection handoff")
        publication_evidence = _load_mapping(evidence_path, "publication evidence")
        publication = _load_mapping(publication_path, "selected checkpoint publication")
        validated = validate_selected_checkpoint_publication(
            publication,
            handoff,
            publication_evidence,
            config,
            selection_handoff_sha256=canonical_json_sha256(handoff),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, CheckpointSelectionError) as error:
        print(f"selected_checkpoint_publication=failed error={error}", file=sys.stderr)
        return 2
    print(
        "selected_checkpoint_publication=verified "
        f"candidate={validated['selected_candidate_id']} hooks={len(validated['integration_hooks'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
