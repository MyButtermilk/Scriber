"""Create a SHA-256 inventory for one local all-BF16 Transformers checkpoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.quantization import QuantizationError, inventory_bf16_checkpoint  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        inventory = inventory_bf16_checkpoint(args.checkpoint, source_id=args.source_id)
        payload = (
            json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if args.output.exists() and args.output.read_bytes() != payload:
            raise QuantizationError(f"refusing to overwrite different inventory: {args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if not args.output.exists():
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_bytes(payload)
            temporary.replace(args.output)
    except (OSError, QuantizationError) as error:
        print(f"checkpoint_inventory=failed error={error}", file=sys.stderr)
        return 2
    print(
        f"checkpoint_inventory=complete files={inventory['file_count']} "
        f"bytes={inventory['total_bytes']} tree={inventory['tree_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
