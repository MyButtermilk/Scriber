#!/usr/bin/env python3
"""Run the local-only attempt15 V2 BF16 conversion and Q8_0 quantization."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.v2_q8_bf16_release import (  # noqa: E402
    V2ReleaseError,
    canonical_json_bytes,
    run_v2_quantization,
)


def _load_canonical_object(path: Path, label: str) -> dict[str, object]:
    source = path.expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise V2ReleaseError(f"{label} must be a regular file: {source}")
    payload = source.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2ReleaseError(f"{label} is not UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise V2ReleaseError(f"{label} must be a JSON object")
    materialized = dict(value)
    if payload != canonical_json_bytes(materialized):
        raise V2ReleaseError(f"{label} bytes are not canonical")
    return materialized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--evaluation-binding", type=Path, required=True)
    parser.add_argument("--source-checkpoint", type=Path, required=True)
    parser.add_argument("--llama-cpp-bundle", type=Path, required=True)
    parser.add_argument("--llama-cpp-bundle-lock", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--python-executable", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_v2_quantization(
            _load_canonical_object(args.plan, "V2 release plan"),
            evaluation_binding=_load_canonical_object(
                args.evaluation_binding, "accepted V2 evaluation binding"
            ),
            source_checkpoint=args.source_checkpoint,
            llama_cpp_bundle=args.llama_cpp_bundle,
            llama_cpp_bundle_lock=args.llama_cpp_bundle_lock,
            output_root=args.output_root,
            python_executable=args.python_executable,
        )
    except (OSError, V2ReleaseError, ValueError) as error:
        print(f"v2_q8_bf16_quantization=failed error={error}", file=sys.stderr)
        return 2
    print(
        "v2_q8_bf16_quantization=complete variants=Q8_0,BF16 "
        f"publication_performed={str(result['publication_performed']).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
