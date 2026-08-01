"""Materialize the immutable single-arm lexical-marker repair bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from transformers import AutoTokenizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.marker_repair import (  # noqa: E402
    build_marker_repair_bundle,
)
from scriber_polishing.model_factory import (  # noqa: E402
    BASE_MODEL_ID,
    BASE_MODEL_REVISION,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(
        BASE_MODEL_ID,
        revision=BASE_MODEL_REVISION,
        use_fast=True,
    )
    bundle = build_marker_repair_bundle(
        args.config,
        output_dir=args.output_dir,
        tokenizer=tokenizer,
    )
    print(
        json.dumps(
            {
                "output_dir": str(bundle.output_dir),
                "experiment_id": bundle.manifest["experiment_id"],
                "repair_input_sha256": bundle.manifest[
                    "repair_input_sha256"
                ],
                "train_examples": bundle.manifest["data"]["train"][
                    "record_count"
                ],
                "validation_examples": bundle.manifest["data"][
                    "validation"
                ]["record_count"],
                "update_steps": bundle.manifest["schedule"][
                    "update_steps"
                ],
                "effective_order_sha256": bundle.manifest["order_lock"][
                    "source_effective_order_sha256"
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
