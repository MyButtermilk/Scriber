"""Select a final checkpoint from one completed run's best-loss-plus-tail set.

This local-only command consumes immutable JSON evidence. It does not load a
model, use a GPU, or contact a model hub.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.checkpoint_selection import (  # noqa: E402
    CheckpointSelectionError,
    build_checkpoint_selection_handoff,
    canonical_json_sha256,
    load_checkpoint_selection_config,
    select_checkpoint,
)


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise CheckpointSelectionError(f"{label} must be a JSON object: {path}")
    return dict(value)


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise CheckpointSelectionError(f"checkpoint candidates must be a JSON array of objects: {path}")
    return [dict(item) for item in value]


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_new(path: Path, value: Mapping[str, Any], *, label: str) -> None:
    if path.exists():
        raise CheckpointSelectionError(f"refusing to overwrite {label}: {path}")
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
    parser.add_argument("--completed-training-report", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--handoff-output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.output.exists():
            raise CheckpointSelectionError(f"refusing to overwrite selection report: {args.output}")
        if args.handoff_output.exists():
            raise CheckpointSelectionError(f"refusing to overwrite selection handoff: {args.handoff_output}")
        config = load_checkpoint_selection_config(args.config)
        completed_report = _load_mapping(args.completed_training_report, "completed training report")
        candidates = _load_candidates(args.candidates)
        config_sha256 = _sha256_file(args.config)
        completed_report_sha256 = _sha256_file(args.completed_training_report)
        report = select_checkpoint(
            completed_report,
            candidates,
            config,
            completed_training_report_sha256=completed_report_sha256,
            selection_config_sha256=config_sha256,
        )
        handoff = build_checkpoint_selection_handoff(
            report,
            completed_report,
            candidates,
            config,
            completed_training_report_sha256=completed_report_sha256,
            selection_config_sha256=config_sha256,
            selection_report_sha256=canonical_json_sha256(report),
        )
        _write_new(args.output, report, label="selection report")
        _write_new(args.handoff_output, handoff, label="selection handoff")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, CheckpointSelectionError) as error:
        print(f"checkpoint_selection=failed error={error}", file=sys.stderr)
        return 2

    selected = report["selected"]
    selected_id = selected["candidate_id"] if selected is not None else "none"
    print(f"checkpoint_selection=complete status={report['status']} selected={selected_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
