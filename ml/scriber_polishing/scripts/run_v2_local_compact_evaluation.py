"""Run the repaired 300-case V2 compact evaluation locally on Windows CUDA."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.v2_local_compact_evaluation import (  # noqa: E402
    V2LocalCompactEvaluationError,
    recover_local_v2_compact_evaluation_postprocessing,
    run_local_v2_compact_evaluation,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument("--expected-packet-tree-sha256", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-launch-contract-sha256", required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--base-model-metadata-root", type=Path, required=True)
    parser.add_argument("--v1-evaluation-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--recover-postprocessing-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        common = {
            "packet_root": args.packet_root,
            "expected_packet_tree_sha256": args.expected_packet_tree_sha256,
            "expected_manifest_sha256": args.expected_manifest_sha256,
            "expected_launch_contract_sha256": args.expected_launch_contract_sha256,
            "output_root": args.output_root,
        }
        if args.recover_postprocessing_only:
            result = recover_local_v2_compact_evaluation_postprocessing(**common)
        else:
            result = run_local_v2_compact_evaluation(
                **common,
                model_root=args.model_root,
                base_model_metadata_root=args.base_model_metadata_root,
                v1_evaluation_root=args.v1_evaluation_root,
                batch_size=args.batch_size,
            )
    except V2LocalCompactEvaluationError as error:
        print(f"local V2 compact evaluation failed closed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
