"""Build a hash-bound raw or deterministic-minimal prediction baseline from frozen references."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_io import load_evaluation_references  # noqa: E402
from scriber_polishing.standard_predictions import (  # noqa: E402
    STANDARD_CANDIDATES,
    build_standard_predictions,
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _prediction_payload(rows: list[dict[str, object]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ).encode("utf-8")


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--candidate", choices=sorted(STANDARD_CANDIDATES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.manifest.resolve():
        raise ValueError("--output and --manifest must be different paths")

    references = load_evaluation_references(args.references)
    rows = build_standard_predictions(references, candidate=args.candidate)
    payload = _prediction_payload(rows)
    manifest = {
        "schema_version": 1,
        "producer": "scriber_polishing.standard_predictions.v1",
        "candidate": args.candidate,
        "case_count": len(rows),
        "reference_sha256": _sha256_bytes(args.references.read_bytes()),
        "prediction_sha256": _sha256_bytes(payload),
    }
    _write_bytes(args.output, payload)
    _write_bytes(
        args.manifest,
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(
        f"standard_predictions=complete candidate={args.candidate} cases={len(rows)} "
        f"prediction_sha256={manifest['prediction_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
