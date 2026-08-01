"""Build the deterministic non-holdout laptop benchmark suite from local tokenizer bytes."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.benchmark import BenchmarkError  # noqa: E402
from scriber_polishing.benchmark_cases import (  # noqa: E402
    materialize_synthetic_benchmark_cases,
)
from scriber_polishing.quantization import (  # noqa: E402
    QuantizationError,
    load_quantization_config,
)
from scriber_polishing.variant_artifacts import (  # noqa: E402
    VariantArtifactError,
    verify_variant_artifact_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "quantization.yaml",
    )
    parser.add_argument("--tokenizer-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        from transformers import AutoTokenizer

        config = load_quantization_config(args.config)
        artifact = verify_variant_artifact_manifest(args.tokenizer_artifact)
        tokenizer = AutoTokenizer.from_pretrained(
            args.tokenizer_artifact,
            local_files_only=True,
            trust_remote_code=False,
            fix_mistral_regex=False,
        )
        report = materialize_synthetic_benchmark_cases(
            tokenizer=tokenizer,
            config=config,
            config_sha256=(
                "sha256:" + hashlib.sha256(args.config.read_bytes()).hexdigest()
            ),
            tokenizer_tree_sha256=str(artifact["tokenizer"]["tree_sha256"]),
            output_path=args.output,
            manifest_path=args.manifest,
        )
    except (
        ImportError,
        OSError,
        BenchmarkError,
        QuantizationError,
        VariantArtifactError,
    ) as error:
        print(f"synthetic_benchmark_cases=failed error={error}", file=sys.stderr)
        return 2
    print(
        "synthetic_benchmark_cases=complete "
        f"records={report['record_count']} sha256={report['records_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
