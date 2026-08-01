#!/usr/bin/env python3
"""Prepare or verify exact post-terminal V4 external-journal cleanup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation_v4 import (
    build_v4_external_journal_cleanup_plan,
    verify_v4_external_journal_cleanup,
)


def _canonical_object(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict) or payload != canonical_json_bytes(value):
        raise ValueError(f"cleanup evidence is not canonical JSON: {path}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("plan", "verify"), required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--external-journal-root", type=Path, required=True)
    parser.add_argument("--runtime-completion", type=Path)
    parser.add_argument("--terminal-evidence", type=Path)
    parser.add_argument("--cleanup-plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.mode == "plan":
        if args.runtime_completion is None or args.terminal_evidence is None:
            raise ValueError("cleanup plan requires runtime completion and terminal evidence")
        result = build_v4_external_journal_cleanup_plan(
            output_root=args.output_root,
            external_journal_root=args.external_journal_root,
            runtime_completion_path=args.runtime_completion,
            terminal_evidence=_canonical_object(args.terminal_evidence),
        )
    else:
        if args.cleanup_plan is None:
            raise ValueError("cleanup verification requires --cleanup-plan")
        result = verify_v4_external_journal_cleanup(
            output_root=args.output_root,
            external_journal_root=args.external_journal_root,
            cleanup_plan=_canonical_object(args.cleanup_plan),
        )
    output = args.output.expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise ValueError(f"refusing to overwrite cleanup output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
