"""Build the deterministic five-variant, three-suite release-matrix input."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.variant_release_builder import (  # noqa: E402
    VariantReleaseBuildError,
    build_variant_release_matrix,
    parse_benchmark_report_arguments,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quantization-root", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument(
        "--benchmark-report",
        action="append",
        default=[],
        metavar="VARIANT=PATH",
        help="Repeat exactly once for bf16, int8, int4_nf4, Q8_0, and Q4_K_M.",
    )
    parser.add_argument(
        "--quantization-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "quantization.yaml",
    )
    parser.add_argument(
        "--evaluation-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "evaluation.yaml",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        benchmarks = parse_benchmark_report_arguments(args.benchmark_report)
        build = build_variant_release_matrix(
            quantization_root=args.quantization_root,
            reference_root=args.reference_root,
            benchmark_reports=benchmarks,
            quantization_config_path=args.quantization_config,
            evaluation_config_path=args.evaluation_config,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError, VariantReleaseBuildError) as error:
        print(f"variant_release_matrix_build=failed error={error}", file=sys.stderr)
        return 2
    print(
        "variant_release_matrix_build=passed "
        "variants=5 suites=3 cases_per_variant=3250 "
        f"matrix={build.matrix_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
