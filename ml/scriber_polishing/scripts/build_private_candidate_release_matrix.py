"""Build a private-candidate matrix from mixed cloud and local evidence."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_bundle import EvaluationPackagePart  # noqa: E402
from scriber_polishing.variant_release_builder import (  # noqa: E402
    VariantReleaseBuildError,
    build_private_candidate_release_matrix,
)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise VariantReleaseBuildError(f"{label} must be an object")
    return value


def _path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise VariantReleaseBuildError(f"{label} must be a non-empty path")
    return Path(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-spec", type=Path, required=True)
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
        raw = json.loads(args.source_spec.read_text(encoding="utf-8"))
        spec = _mapping(raw, "private candidate source specification")
        raw_references = _mapping(spec.get("references"), "reference sources")
        references = {
            suite: EvaluationPackagePart(
                _path(_mapping(value, f"{suite} reference").get("records"), f"{suite} records"),
                _path(_mapping(value, f"{suite} reference").get("manifest"), f"{suite} manifest"),
            )
            for suite, value in raw_references.items()
        }
        raw_variants = _mapping(spec.get("variants"), "variant sources")
        variants = {
            variant: {
                "artifact_root": _path(
                    _mapping(value, f"{variant} source").get("artifact_root"),
                    f"{variant} artifact root",
                ),
                "evaluation_root": _path(
                    _mapping(value, f"{variant} source").get("evaluation_root"),
                    f"{variant} evaluation root",
                ),
                "benchmark_report": _path(
                    _mapping(value, f"{variant} source").get("benchmark_report"),
                    f"{variant} benchmark report",
                ),
            }
            for variant, value in raw_variants.items()
        }
        raw_failures = _mapping(
            spec.get("ineligible_variants"),
            "ineligible variant sources",
        )
        failures = {
            variant: {
                "stage": _mapping(value, f"{variant} failure").get("stage"),
                "reason_code": _mapping(value, f"{variant} failure").get(
                    "reason_code"
                ),
                "evidence_path": _path(
                    _mapping(value, f"{variant} failure").get("evidence_path"),
                    f"{variant} failure evidence",
                ),
            }
            for variant, value in raw_failures.items()
        }
        build = build_private_candidate_release_matrix(
            reference_packages=references,
            variant_sources=variants,
            ineligible_variants=failures,
            quantization_config_path=args.quantization_config,
            evaluation_config_path=args.evaluation_config,
            output_dir=args.output_dir,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        VariantReleaseBuildError,
    ) as error:
        print(f"private_candidate_matrix_build=failed error={error}", file=sys.stderr)
        return 2
    print(
        "private_candidate_matrix_build=complete "
        f"status={build.verified_report['status']} matrix={build.matrix_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
