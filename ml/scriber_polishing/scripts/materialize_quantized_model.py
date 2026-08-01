"""Materialize and reload-test a local Transformers bitsandbytes variant."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.quantization import (  # noqa: E402
    QuantizationError,
    load_quantization_config,
    materialize_bitsandbytes_variant,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "quantization.yaml")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--training-fingerprint", required=True)
    parser.add_argument("--variant", choices=("int8", "int4_nf4"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_quantization_config(args.config)
        if args.variant not in config["transformers_variants"]:
            raise QuantizationError(f"variant is not enabled by policy: {args.variant}")
        manifest = materialize_bitsandbytes_variant(
            args.checkpoint,
            args.output,
            variant=args.variant,
            source_id=args.source_id,
            training_fingerprint=args.training_fingerprint,
        )
    except (OSError, QuantizationError) as error:
        print(f"quantization=failed variant={args.variant} error={error}", file=sys.stderr)
        return 2
    print(
        f"quantization=complete variant={args.variant} bytes={manifest['artifact']['total_bytes']} "
        f"tree={manifest['artifact']['tree_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
