"""Verify complete artifact/reload/prediction/evaluation/delta/benchmark evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.quantization import (  # noqa: E402
    QuantizationError,
    load_quantization_config,
)
from scriber_polishing.variant_release import (  # noqa: E402
    VariantReleaseError,
    collect_variant_release_failures,
    collect_variant_release_records,
    derive_qualification_failures,
    verify_variant_release_records,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "quantization.yaml",
    )
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise VariantReleaseError(f"refusing to overwrite release report: {args.output}")
        config = load_quantization_config(args.config)
        records = collect_variant_release_records(args.matrix, config)
        report = verify_variant_release_records(
            config,
            records,
            ineligible_variants=collect_variant_release_failures(args.matrix),
            qualification_failures=derive_qualification_failures(config, records),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(args.output)
    except (OSError, QuantizationError, VariantReleaseError) as error:
        print(f"variant_release_matrix=failed error={error}", file=sys.stderr)
        return 2
    print(
        "variant_release_matrix=passed "
        f"variants={','.join(report['variants'])} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
