"""Fresh-reload and centrally manifest the final all-BF16 checkpoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.quantization import (  # noqa: E402
    QuantizationError,
    materialize_bf16_variant_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--training-fingerprint", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = materialize_bf16_variant_manifest(
            args.checkpoint,
            source_id=args.source_id,
            training_fingerprint=args.training_fingerprint,
        )
    except (OSError, QuantizationError) as error:
        print(f"bf16_variant_manifest=failed error={error}", file=sys.stderr)
        return 2
    print(
        "bf16_variant_manifest=complete "
        f"tree={manifest['artifact']['tree_sha256']} "
        f"files={manifest['artifact']['file_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
