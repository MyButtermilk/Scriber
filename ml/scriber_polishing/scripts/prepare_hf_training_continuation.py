"""Prepare, but never launch, the private 846+254 HF training packet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from transformers import AutoTokenizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_training_continuation import (  # noqa: E402
    CONTINUATION_ALLOWED_FLAVORS,
    CONTINUATION_DEFAULT_FLAVOR,
    build_hf_job_launch_contract,
    build_hf_training_continuation_packet,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--flavor",
        choices=tuple(sorted(CONTINUATION_ALLOWED_FLAVORS)),
        default=CONTINUATION_DEFAULT_FLAVOR,
    )
    parser.add_argument("--acceptance-report", type=Path, required=True)
    parser.add_argument("--repair-bundle-dir", type=Path, required=True)
    parser.add_argument("--training-report", type=Path, required=True)
    parser.add_argument("--cloud-result", type=Path, required=True)
    parser.add_argument("--accepted-parent", type=Path, required=True)
    parser.add_argument("--pilot-selection-report", type=Path, required=True)
    parser.add_argument("--ai-gold-manifest", type=Path, required=True)
    parser.add_argument("--ai-gold-train", type=Path, required=True)
    parser.add_argument("--final-manifest", type=Path, required=True)
    parser.add_argument("--final-train", type=Path, required=True)
    parser.add_argument("--pilot-manifest", type=Path, required=True)
    parser.add_argument("--pilot-train", type=Path, required=True)
    args = parser.parse_args()

    parent = args.accepted_parent.expanduser().resolve()
    tokenizer = AutoTokenizer.from_pretrained(
        parent,
        use_fast=True,
        local_files_only=True,
        fix_mistral_regex=False,
    )
    packet = build_hf_training_continuation_packet(
        output_dir=args.output_dir,
        tokenizer=tokenizer,
        flavor=args.flavor,
        acceptance_report_path=args.acceptance_report,
        repair_bundle_dir=args.repair_bundle_dir,
        training_report_path=args.training_report,
        cloud_result_path=args.cloud_result,
        accepted_parent_dir=parent,
        pilot_selection_report_path=args.pilot_selection_report,
        ai_gold_manifest_path=args.ai_gold_manifest,
        ai_gold_train_path=args.ai_gold_train,
        final_manifest_path=args.final_manifest,
        final_train_path=args.final_train,
        pilot_manifest_path=args.pilot_manifest,
        pilot_train_path=args.pilot_train,
    )
    result = {
        "packet": dict(packet.manifest),
        "launch_contract": build_hf_job_launch_contract(
            packet.output_dir,
            expected_flavor=args.flavor,
        ),
        "external_job_started": False,
    }
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
