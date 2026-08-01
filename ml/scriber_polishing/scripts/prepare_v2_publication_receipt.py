#!/usr/bin/env python3
"""Bind an uploader-issued V2 public commit to the exact staged local bytes."""

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
    build_v2_publication_receipt,
    canonical_json_bytes,
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


def _write_immutable(path: Path, payload: bytes) -> None:
    destination = path.expanduser().resolve()
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != payload:
            raise V2ReleaseError(f"refusing to replace V2 publication receipt: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--staged-root", type=Path, required=True)
    parser.add_argument("--public-commit", required=True)
    parser.add_argument("--upload-operation-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = build_v2_publication_receipt(
            _load_canonical_object(args.plan, "V2 release plan"),
            inventory=_load_canonical_object(args.inventory, "V2 public inventory"),
            staged_root=args.staged_root,
            public_commit=args.public_commit,
            upload_operation_sha256=args.upload_operation_sha256,
        )
        _write_immutable(args.output, canonical_json_bytes(receipt))
    except (OSError, V2ReleaseError, ValueError) as error:
        print(f"v2_publication_receipt=failed error={error}", file=sys.stderr)
        return 2
    print(
        "v2_publication_receipt=prepared "
        f"repository={receipt['repository_id']} commit={receipt['public_commit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
