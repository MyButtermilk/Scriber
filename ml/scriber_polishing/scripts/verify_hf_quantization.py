"""Verify the private five-variant Hugging Face quantization workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_quantization_pipeline import (  # noqa: E402
    HfQuantizationError,
    finalize_hf_quantization,
    list_hf_quantization_suites,
    publish_hf_quantization_result_no_clobber,
    read_json_object,
    record_hf_quantization_evaluation_exit,
    resolve_quantization_llama_cpp_library_path,
    validate_hf_quantization_plan,
    verify_hf_quantization_packet,
    verify_hf_quantization_result_file,
    verify_immutable_bf16_baseline,
    verify_quantization_completed_lane,
    verify_quantization_llama_cpp,
    verify_quantization_source_model,
    verify_quantization_variant_artifact,
)


def _required(value: object, option: str) -> object:
    if value is None:
        raise HfQuantizationError(f"{option} is required for this mode")
    return value


def _plan(path: Path) -> dict[str, object]:
    value, _ = read_json_object(path, "HF quantization plan")
    return validate_hf_quantization_plan(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "packet",
            "source-model",
            "bf16-baseline",
            "llama-cpp",
            "llama-library-path",
            "source-id",
            "training-fingerprint",
            "list-suites",
            "record-exit",
            "variant",
            "lane",
            "finalize",
            "publish",
            "result",
        ),
    )
    parser.add_argument("--packet-root", type=Path)
    parser.add_argument("--expected-packet-tree-sha256")
    parser.add_argument(
        "--require-fresh-actual-cost",
        action="store_true",
        help=(
            "Local pre-preparation/pre-submission gate only; the remote runner "
            "must not recheck wall-clock freshness after scheduler delay."
        ),
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--llama-cpp-bundle", type=Path)
    parser.add_argument("--llama-cpp-bundle-lock", type=Path)
    parser.add_argument("--bf16-baseline-root", type=Path)
    parser.add_argument("--variant")
    parser.add_argument("--suite")
    parser.add_argument("--evaluation-exit", type=int)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--destination-root", type=Path)
    parser.add_argument("--owner-token")
    args = parser.parse_args(argv)

    if args.mode == "packet":
        value = verify_hf_quantization_packet(
            _required(args.packet_root, "--packet-root"),
            expected_packet_tree_sha256=str(
                _required(
                    args.expected_packet_tree_sha256,
                    "--expected-packet-tree-sha256",
                )
            ),
            require_fresh_actual_cost=(args.require_fresh_actual_cost),
        )
    elif args.mode == "source-model":
        value = verify_quantization_source_model(
            manifest_path=_required(args.manifest, "--manifest"),
            model_root=_required(args.model, "--model"),
            output_path=_required(args.output, "--output"),
        )
    elif args.mode == "bf16-baseline":
        value = verify_immutable_bf16_baseline(
            manifest_path=_required(args.manifest, "--manifest"),
            baseline_root=_required(
                args.bf16_baseline_root,
                "--bf16-baseline-root",
            ),
        )
    elif args.mode == "llama-cpp":
        value = verify_quantization_llama_cpp(
            manifest_path=_required(args.manifest, "--manifest"),
            bundle_root=_required(
                args.llama_cpp_bundle,
                "--llama-cpp-bundle",
            ),
            bundle_lock=_required(
                args.llama_cpp_bundle_lock,
                "--llama-cpp-bundle-lock",
            ),
            output_path=_required(args.output, "--output"),
        )
    elif args.mode == "llama-library-path":
        print(
            resolve_quantization_llama_cpp_library_path(
                manifest_path=_required(
                    args.manifest,
                    "--manifest",
                ),
                bundle_root=_required(
                    args.llama_cpp_bundle,
                    "--llama-cpp-bundle",
                ),
                bundle_lock=_required(
                    args.llama_cpp_bundle_lock,
                    "--llama-cpp-bundle-lock",
                ),
            )
        )
        return 0
    elif args.mode in {"source-id", "training-fingerprint"}:
        plan = _plan(_required(args.manifest, "--manifest"))
        source = plan["source_model"]
        if args.mode == "source-id":
            print(source["final_model_tree_sha256"])
        else:
            print(source["training_fingerprint"])
        return 0
    elif args.mode == "list-suites":
        for suite_id, filename in list_hf_quantization_suites(_required(args.manifest, "--manifest")):
            print(f"{suite_id}\t{filename}")
        return 0
    elif args.mode == "record-exit":
        value = record_hf_quantization_evaluation_exit(
            variant=str(_required(args.variant, "--variant")),
            suite_id=str(_required(args.suite, "--suite")),
            evaluation_exit=int(_required(args.evaluation_exit, "--evaluation-exit")),
            output_path=_required(args.output, "--output"),
        )
    elif args.mode == "variant":
        value = verify_quantization_variant_artifact(
            manifest_path=_required(args.manifest, "--manifest"),
            output_root=_required(args.output_root, "--output-root"),
            variant=str(_required(args.variant, "--variant")),
        )
    elif args.mode == "lane":
        value = verify_quantization_completed_lane(
            manifest_path=_required(args.manifest, "--manifest"),
            output_root=_required(args.output_root, "--output-root"),
            variant=str(_required(args.variant, "--variant")),
            suite_id=str(_required(args.suite, "--suite")),
            baseline_root=args.bf16_baseline_root,
        )
    elif args.mode == "finalize":
        value = finalize_hf_quantization(
            manifest_path=_required(args.manifest, "--manifest"),
            output_root=_required(args.output_root, "--output-root"),
            output_path=_required(args.output, "--output"),
            baseline_root=args.bf16_baseline_root,
        )
    elif args.mode == "publish":
        value = publish_hf_quantization_result_no_clobber(
            manifest_path=_required(args.manifest, "--manifest"),
            staged_root=_required(args.output_root, "--output-root"),
            result_path=_required(args.result, "--result"),
            destination_root=_required(
                args.destination_root,
                "--destination-root",
            ),
            baseline_root=_required(
                args.bf16_baseline_root,
                "--bf16-baseline-root",
            ),
            expected_packet_tree_sha256=str(
                _required(
                    args.expected_packet_tree_sha256,
                    "--expected-packet-tree-sha256",
                )
            ),
            owner_token=str(_required(args.owner_token, "--owner-token")),
        )
    else:
        value = verify_hf_quantization_result_file(
            manifest_path=_required(args.manifest, "--manifest"),
            output_root=_required(args.output_root, "--output-root"),
            result_path=_required(args.result, "--result"),
            baseline_root=args.bf16_baseline_root,
        )
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
