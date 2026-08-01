"""Reproduce one bitsandbytes failure and write privacy-minimal evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.quantization import (  # noqa: E402
    QuantizationError,
    inventory_bf16_checkpoint,
    load_quantization_config,
    materialize_bitsandbytes_variant,
)

_SCHEMA = PROJECT_ROOT / "contracts" / "quantization_failure_evidence_schema.json"


@cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_SCHEMA.read_text(encoding="utf-8")))


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _reason_code(message: str) -> str:
    normalized = message.casefold()
    if "empty completion" in normalized or "no completion tokens" in normalized:
        return "empty_completion"
    if "valid sst" in normalized or "sstparseerror" in normalized:
        return "invalid_sst"
    return "materialization_failed"


def _write_exclusive(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "quantization.yaml",
    )
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--training-fingerprint", required=True)
    parser.add_argument(
        "--variant",
        choices=("int8", "int4_nf4"),
        required=True,
    )
    parser.add_argument("--artifact-output", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.artifact_output.exists() or args.evidence_output.exists():
        print("quantization_failure_probe=failed output_exists", file=sys.stderr)
        return 2
    try:
        config = load_quantization_config(args.config)
        if args.variant not in config["transformers_variants"]:
            raise ValueError(f"variant is not enabled by policy: {args.variant}")
        inventory = inventory_bf16_checkpoint(
            args.checkpoint,
            source_id=args.source_id,
        )
    except (OSError, QuantizationError, ValueError) as error:
        print(f"quantization_failure_probe=failed error={error}", file=sys.stderr)
        return 2
    try:
        materialize_bitsandbytes_variant(
            args.checkpoint,
            args.artifact_output,
            variant=args.variant,
            source_id=args.source_id,
            training_fingerprint=args.training_fingerprint,
        )
    except QuantizationError as error:
        diagnostic = str(error).encode("utf-8", errors="replace")
        report = {
            "schema_version": 1,
            "kind": "scriber_quantization_failure_evidence",
            "status": "failed",
            "stage": "reload",
            "variant": args.variant,
            "reason_code": _reason_code(str(error)),
            "source_id": args.source_id,
            "source_tree_sha256": inventory["tree_sha256"],
            "training_fingerprint": args.training_fingerprint,
            "quantization_config_sha256": _sha256(args.config.read_bytes()),
            "error_type": type(error).__name__,
            "diagnostic_sha256": _sha256(diagnostic),
            "artifact_published": False,
            "fresh_process_required": True,
        }
        errors = sorted(
            _validator().iter_errors(report),
            key=lambda item: list(item.absolute_path),
        )
        if errors:
            print(
                f"quantization_failure_probe=failed schema={errors[0].message}",
                file=sys.stderr,
            )
            return 2
        _write_exclusive(args.evidence_output, report)
        print(
            "quantization_failure_probe=confirmed "
            f"variant={args.variant} reason={report['reason_code']}"
        )
        return 0
    except (OSError, ValueError) as error:
        print(f"quantization_failure_probe=failed error={error}", file=sys.stderr)
        return 2
    print(
        "quantization_failure_probe=unexpectedly_passed "
        f"variant={args.variant} artifact={args.artifact_output}",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
