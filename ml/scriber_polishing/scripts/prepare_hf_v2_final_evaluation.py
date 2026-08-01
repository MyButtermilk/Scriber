"""Build, but never upload or launch, the 300-case V2 final-evaluation packet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_v2_final_evaluation import (  # noqa: E402
    build_v2_final_evaluation_packet,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postflight-binding", type=Path, required=True)
    parser.add_argument("--v1-evaluator-packet", type=Path, required=True)
    parser.add_argument("--v1-completed-result", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_v2_final_evaluation_packet(
        postflight_binding_path=args.postflight_binding,
        v1_evaluator_packet_root=args.v1_evaluator_packet,
        v1_completed_result_path=args.v1_completed_result,
        output_dir=args.output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
