"""Build the exact 200/50/50 local dataset for the mandatory training smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.smoke_dataset import build_smoke_pairs  # noqa: E402


def _load_targets(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("target_writer_*/targets.jsonl")):
        records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return records


def _json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=270_010)
    args = parser.parse_args()
    pairs = build_smoke_pairs(_load_targets(args.target_root), seed=args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for split, rows in pairs.items():
        content = ("\n".join(_json_line(row) for row in rows) + "\n").encode("utf-8")
        path = args.output_dir / f"{split}.jsonl"
        path.write_bytes(content)
        hashes[split] = hashlib.sha256(content).hexdigest()
    manifest = {
        "manifest_version": 1,
        "seed": args.seed,
        "counts": {split: len(rows) for split, rows in pairs.items()},
        "sha256": hashes,
        "split_leakage": False,
        "synthetic_only": True,
        "human_curation": False,
    }
    (args.output_dir / "manifest.json").write_text(_json_line(manifest) + "\n", encoding="utf-8")
    print(f"smoke_dataset=complete counts={manifest['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
