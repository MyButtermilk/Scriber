"""Rewrite a judge verdict fragment as canonical UTF-8 JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _decode_rows(raw: str, path: Path) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    rows: list[dict[str, Any]] = []
    offset = 0
    while offset < len(raw):
        while offset < len(raw):
            if raw.startswith("\\n", offset):
                offset += 2
            elif raw[offset].isspace():
                offset += 1
            else:
                break
        if offset >= len(raw):
            break
        value, offset = decoder.raw_decode(raw, offset)
        if not isinstance(value, dict):
            raise ValueError(f"judge verdict row must be an object: {path}")
        rows.append(value)
    if not rows:
        raise ValueError(f"judge verdict fragment is empty: {path}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fragment", type=Path, nargs="+")
    args = parser.parse_args()
    for path in args.fragment:
        rows = _decode_rows(path.read_text(encoding="utf-8-sig"), path)
        payload = (
            "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows) + "\n"
        ).encode("utf-8")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
        print(f"judge_verdict_fragment=canonical path={path} rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
