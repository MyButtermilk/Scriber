"""Prepare a self-contained, cost-bounded Hugging Face marker-repair Job."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from transformers import AutoTokenizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_marker_repair_job import (  # noqa: E402
    build_hf_marker_repair_packet,
)
from scriber_polishing.model_factory import (  # noqa: E402
    BASE_MODEL_ID,
    BASE_MODEL_REVISION,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--flavor", default="l4x1")
    parser.add_argument("--timeout-minutes", type=int, default=60)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(
        BASE_MODEL_ID,
        revision=BASE_MODEL_REVISION,
        use_fast=True,
    )
    packet = build_hf_marker_repair_packet(
        bundle_dir=args.bundle_dir,
        output_dir=args.output_dir,
        tokenizer=tokenizer,
        budget_usd=args.budget_usd,
        flavor=args.flavor,
        timeout_minutes=args.timeout_minutes,
    )
    print(
        json.dumps(
            dict(packet.manifest),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
