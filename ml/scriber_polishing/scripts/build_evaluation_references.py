"""Open one hash-bound sealed split as a closed evaluation-reference JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_reference_builder import (  # noqa: E402
    build_evaluation_references,
    write_evaluation_references,
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"blank JSONL line at {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row {line_number} must be an object")
        result.append(value)
    return result


def _write_immutable_json(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite different evaluation artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument(
        "--output-split",
        choices=("test", "challenge", "ai_gold_test", "ai_gold_challenge", "regression"),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        actual_sha256 = hashlib.sha256(args.input.read_bytes()).hexdigest()
        if actual_sha256 != args.expected_input_sha256:
            raise ValueError(
                f"input SHA-256 mismatch: expected {args.expected_input_sha256}, got {actual_sha256}"
            )
        references = build_evaluation_references(
            _load_jsonl(args.input),
            output_split=args.output_split,
        )
        manifest = write_evaluation_references(references, args.output)
        manifest.update(
            {
                "input_filename": args.input.name,
                "input_sha256": actual_sha256,
                "output_filename": args.output.name,
            }
        )
        _write_immutable_json(args.manifest, manifest)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"evaluation_references=failed reason={error}", file=sys.stderr)
        return 2
    print(
        f"evaluation_references=pass records={manifest['record_count']} "
        f"sha256={manifest['records_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
