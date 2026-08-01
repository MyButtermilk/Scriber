"""Verify the model-external Hugging Face 846+254 continuation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_training_continuation import (  # noqa: E402
    build_continuation_postflight_result,
    claim_continuation_output_guard,
    verify_hf_training_continuation_bundle,
    verify_hf_training_continuation_packet,
)


def _local_tokenizer(parent_model: Path) -> object:
    from transformers import AutoTokenizer

    root = parent_model.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"accepted-parent tokenizer directory is missing: {root}")
    return AutoTokenizer.from_pretrained(
        root,
        use_fast=True,
        local_files_only=True,
        fix_mistral_regex=False,
    )


def _write_report(path: Path, value: object) -> None:
    destination = path.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite continuation verification: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    destination.write_bytes(payload)
    print(payload.decode("utf-8").rstrip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "packet",
            "claim-output",
            "preflight-remote",
            "preflight-local",
            "postflight",
        ),
    )
    parser.add_argument("--packet-root", type=Path)
    parser.add_argument("--expected-packet-tree-sha256")
    parser.add_argument("--expected-flavor")
    parser.add_argument("--continuation-manifest-sha256")
    parser.add_argument("--owner-id")
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--parent-model", type=Path)
    parser.add_argument("--training-report", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    if args.mode == "packet":
        if args.packet_root is None or args.expected_packet_tree_sha256 is None or args.expected_flavor is None:
            parser.error("packet mode requires --packet-root, --expected-packet-tree-sha256, and --expected-flavor")
        result = verify_hf_training_continuation_packet(
            args.packet_root,
            expected_packet_tree_sha256=args.expected_packet_tree_sha256,
            expected_flavor=args.expected_flavor,
        )
    elif args.mode == "claim-output":
        if (
            args.output_root is None
            or args.expected_packet_tree_sha256 is None
            or args.continuation_manifest_sha256 is None
            or args.owner_id is None
        ):
            parser.error(
                "claim-output requires --output-root, "
                "--expected-packet-tree-sha256, "
                "--continuation-manifest-sha256, and --owner-id"
            )
        result = claim_continuation_output_guard(
            args.output_root,
            expected_packet_tree_sha256=args.expected_packet_tree_sha256,
            continuation_manifest_sha256=args.continuation_manifest_sha256,
            owner_id=args.owner_id,
        )
    elif args.mode == "preflight-local":
        if args.bundle_dir is None or args.parent_model is None:
            parser.error("preflight-local requires --bundle-dir and --parent-model")
        result = verify_hf_training_continuation_bundle(
            args.bundle_dir,
            parent_model=args.parent_model,
            mode=args.mode,
            tokenizer=_local_tokenizer(args.parent_model),
        )
    elif args.mode == "preflight-remote":
        if args.bundle_dir is None or args.parent_model is None:
            parser.error("preflight-remote requires --bundle-dir and --parent-model")
        result = verify_hf_training_continuation_bundle(
            args.bundle_dir,
            parent_model=args.parent_model,
            mode=args.mode,
        )
    else:
        if (
            args.bundle_dir is None
            or args.parent_model is None
            or args.training_report is None
            or args.output_root is None
            or args.expected_flavor is None
        ):
            parser.error(
                "postflight requires --bundle-dir, --parent-model, "
                "--training-report, --output-root, and --expected-flavor"
            )
        input_binding = verify_hf_training_continuation_bundle(
            args.bundle_dir,
            parent_model=args.parent_model,
            mode="preflight-remote",
        )
        result = {
            **build_continuation_postflight_result(
                training_report_path=args.training_report,
                output_root=args.output_root,
                input_binding=input_binding["input_binding"],
                expected_flavor=args.expected_flavor,
            ),
            "input_binding": input_binding,
        }
    _write_report(args.report, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
