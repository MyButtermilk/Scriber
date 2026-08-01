"""Prepare a local publication handoff for one already selected checkpoint.

This command validates the selected checkpoint, its pinned tokenizer, and fresh
reload/inventory evidence only. It does not publish or contact a model hub.
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
    build_selected_checkpoint_publication,
    canonical_json_sha256,
    load_checkpoint_selection_config,
)


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise CheckpointSelectionError(f"{label} must be a JSON object: {path}")
    return dict(value)


def _write_new(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        raise CheckpointSelectionError(f"refusing to overwrite selected checkpoint publication: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "checkpoint_selection.yaml",
    )
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--publication-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_checkpoint_selection_config(args.config)
        handoff = _load_mapping(args.handoff, "checkpoint selection handoff")
        publication_evidence = _load_mapping(args.publication_evidence, "publication evidence")
        publication = build_selected_checkpoint_publication(
            handoff,
            publication_evidence,
            config,
            selection_handoff_sha256=canonical_json_sha256(handoff),
        )
        _write_new(args.output, publication)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, CheckpointSelectionError) as error:
        print(f"selected_checkpoint_publication=failed error={error}", file=sys.stderr)
        return 2
    print(f"selected_checkpoint_publication=prepared candidate={publication['selected_candidate_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
