"""Prepare the inert attempt15 V2 Q8_0/BF16 release plan.

The command is local-only: it neither starts a Hugging Face Job nor creates a
claim, upload, model repository, or commit.
"""

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
    build_v2_release_plan,
    canonical_json_bytes,
    sha256_bytes,
)


def _load_canonical_object(path: Path) -> tuple[dict[str, object], bytes]:
    source = path.expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise V2ReleaseError(f"evaluation binding must be a regular file: {source}")
    payload = source.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2ReleaseError("evaluation binding is not UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise V2ReleaseError("evaluation binding must be a JSON object")
    materialized = dict(value)
    if payload != canonical_json_bytes(materialized):
        raise V2ReleaseError("evaluation binding bytes are not canonical")
    return materialized, payload


def _write_immutable(path: Path, payload: bytes) -> None:
    destination = path.expanduser().resolve()
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != payload:
            raise V2ReleaseError(f"refusing to replace immutable V2 release plan: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as error:
        raise V2ReleaseError("V2 release plan appeared during exclusive creation") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-binding", type=Path, required=True)
    parser.add_argument("--source-git-head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        binding, binding_payload = _load_canonical_object(args.evaluation_binding)
        plan = build_v2_release_plan(
            binding,
            evaluation_binding_sha256=sha256_bytes(binding_payload),
            source_git_head=args.source_git_head,
        )
        _write_immutable(args.output, canonical_json_bytes(plan))
    except (OSError, V2ReleaseError, ValueError) as error:
        print(f"v2_q8_bf16_release_plan=failed error={error}", file=sys.stderr)
        return 2
    print(
        "v2_q8_bf16_release_plan=prepared "
        f"attempt={plan['launch']['attempt']} variants=Q8_0,BF16 "
        f"external_job_started={str(plan['launch']['external_job_started']).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
