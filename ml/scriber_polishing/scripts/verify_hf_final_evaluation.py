"""Verify or finalize the private Hugging Face final-evaluation workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_final_evaluation import (  # noqa: E402
    FinalEvaluationError,
    bind_final_evaluation_prediction_manifest,
    claim_final_evaluation_launch,
    finalize_hf_final_evaluation,
    list_final_evaluation_suites,
    plan_final_evaluation_prediction_lane,
    record_final_evaluation_exit,
    recover_final_evaluation_exit,
    release_final_evaluation_writer,
    verify_base_evaluation_model,
    verify_final_evaluation_lane,
    verify_final_evaluation_model,
    verify_hf_final_evaluation_packet,
)


def _required(value: object, option: str) -> object:
    if value is None:
        raise FinalEvaluationError(f"{option} is required for this mode")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "packet",
            "model",
            "base-model",
            "list-suites",
            "record-exit",
            "recover-exit",
            "plan-predictions",
            "bind-prediction",
            "claim-launch",
            "release-writer",
            "lane",
            "finalize",
        ),
    )
    parser.add_argument("--packet-root", type=Path)
    parser.add_argument("--expected-packet-tree-sha256")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--prediction-manifest", type=Path)
    parser.add_argument("--allow-create", action="store_true")
    parser.add_argument("--candidate")
    parser.add_argument("--suite")
    parser.add_argument("--lane-root", type=Path)
    parser.add_argument("--evaluation-exit", type=int)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--launch-id")
    parser.add_argument("--owner-token")
    args = parser.parse_args(argv)

    if args.mode == "packet":
        result = verify_hf_final_evaluation_packet(
            _required(args.packet_root, "--packet-root"),
            expected_packet_tree_sha256=str(
                _required(
                    args.expected_packet_tree_sha256,
                    "--expected-packet-tree-sha256",
                )
            ),
        )
    elif args.mode in {"model", "base-model"}:
        verifier = verify_final_evaluation_model if args.mode == "model" else verify_base_evaluation_model
        result = verifier(
            manifest_path=_required(args.manifest, "--manifest"),
            model_root=_required(args.model, "--model"),
            output_path=_required(args.output, "--output"),
        )
    elif args.mode == "list-suites":
        suites = list_final_evaluation_suites(_required(args.manifest, "--manifest"))
        for suite_id, filename in suites:
            print(f"{suite_id}\t{filename}")
        return 0
    elif args.mode == "record-exit":
        result = record_final_evaluation_exit(
            candidate=str(_required(args.candidate, "--candidate")),
            suite_id=str(_required(args.suite, "--suite")),
            evaluation_exit=int(_required(args.evaluation_exit, "--evaluation-exit")),
            output_path=_required(args.output, "--output"),
        )
    elif args.mode == "recover-exit":
        result = recover_final_evaluation_exit(
            manifest_path=_required(args.manifest, "--manifest"),
            candidate=str(_required(args.candidate, "--candidate")),
            suite_id=str(_required(args.suite, "--suite")),
            lane_root=_required(args.lane_root, "--lane-root"),
        )
    elif args.mode == "plan-predictions":
        print(plan_final_evaluation_prediction_lane(_required(args.lane_root, "--lane-root")))
        return 0
    elif args.mode == "bind-prediction":
        result = bind_final_evaluation_prediction_manifest(
            manifest_path=_required(args.manifest, "--manifest"),
            prediction_manifest_path=_required(
                args.prediction_manifest,
                "--prediction-manifest",
            ),
            candidate=str(_required(args.candidate, "--candidate")),
            model_root=_required(args.model, "--model"),
            allow_create=args.allow_create,
        )
    elif args.mode == "claim-launch":
        result = claim_final_evaluation_launch(
            manifest_path=_required(args.manifest, "--manifest"),
            output_root=_required(args.output_root, "--output-root"),
            launch_id=str(_required(args.launch_id, "--launch-id")),
            owner_token=str(_required(args.owner_token, "--owner-token")),
        )
    elif args.mode == "release-writer":
        result = release_final_evaluation_writer(
            output_root=_required(args.output_root, "--output-root"),
            launch_id=str(_required(args.launch_id, "--launch-id")),
            owner_token=str(_required(args.owner_token, "--owner-token")),
        )
    elif args.mode == "lane":
        result = verify_final_evaluation_lane(
            manifest_path=_required(args.manifest, "--manifest"),
            candidate=str(_required(args.candidate, "--candidate")),
            suite_id=str(_required(args.suite, "--suite")),
            lane_root=_required(args.lane_root, "--lane-root"),
        )
    else:
        result = finalize_hf_final_evaluation(
            manifest_path=_required(args.manifest, "--manifest"),
            output_root=_required(args.output_root, "--output-root"),
            output_path=_required(args.output, "--output"),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
