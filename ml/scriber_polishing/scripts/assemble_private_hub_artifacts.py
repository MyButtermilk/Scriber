"""Assemble the Scriber model and dataset private-Hub upload directories."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hub_artifact_assembly import (  # noqa: E402
    HubArtifactAssemblyError,
    assemble_private_hub_artifacts,
)


def _quantized(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        variant, separator, path = value.partition("=")
        if not separator or not variant or not path or variant in result:
            raise ValueError("--quantized-artifact must be a unique VARIANT=PATH value")
        result[variant] = Path(path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-source", type=Path, required=True)
    parser.add_argument("--dataset-source", type=Path, required=True)
    parser.add_argument("--ai-gold-source", type=Path, required=True)
    parser.add_argument("--evaluation-root", type=Path, required=True)
    parser.add_argument("--completed-training-report", type=Path, required=True)
    parser.add_argument("--variant-matrix-report", type=Path, required=True)
    parser.add_argument("--quantization-selection-report", type=Path, required=True)
    parser.add_argument("--benchmark-report", type=Path, required=True)
    parser.add_argument("--terra-judge-report", type=Path, required=True)
    parser.add_argument("--sol-judge-report", type=Path, required=True)
    parser.add_argument("--regression-suite", type=Path, required=True)
    parser.add_argument(
        "--publication-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "publication.yaml",
    )
    parser.add_argument(
        "--gemma-license",
        type=Path,
        default=PROJECT_ROOT / "contracts" / "GEMMA_LICENSE.md",
    )
    parser.add_argument(
        "--apache-license",
        type=Path,
        default=PROJECT_ROOT.parents[1] / "src" / "assets" / "licenses" / "APACHE-2.0.txt",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--selected-variant",
        choices=("bf16", "Q8_0", "Q4_K_M"),
        required=True,
    )
    parser.add_argument(
        "--quantized-artifact",
        action="append",
        default=[],
        help="Repeat as Q8_0=PATH or Q4_K_M=PATH.",
    )
    args = parser.parse_args()
    try:
        result = assemble_private_hub_artifacts(
            model_source=args.model_source,
            dataset_source=args.dataset_source,
            ai_gold_source=args.ai_gold_source,
            evaluation_root=args.evaluation_root,
            completed_training_report=args.completed_training_report,
            variant_matrix_report=args.variant_matrix_report,
            benchmark_report=args.benchmark_report,
            terra_judge_report=args.terra_judge_report,
            sol_judge_report=args.sol_judge_report,
            regression_suite=args.regression_suite,
            publication_config=args.publication_config,
            gemma_license=args.gemma_license,
            apache_license=args.apache_license,
            output_root=args.output_root,
            selected_variant=args.selected_variant,
            quantized_artifacts=_quantized(args.quantized_artifact),
            quantization_selection_report=args.quantization_selection_report,
        )
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        HubArtifactAssemblyError,
    ) as error:
        print(f"hub_artifact_assembly=failed reason={error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
