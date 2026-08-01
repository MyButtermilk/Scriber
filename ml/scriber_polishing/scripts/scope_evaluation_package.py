"""Give one hash-bound evaluation package deterministic scoped case IDs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_bundle import (  # noqa: E402
    EvaluationBundleError,
    EvaluationPackagePart,
    scope_evaluation_package,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("reference", "prediction"), required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--candidate")
    parser.add_argument(
        "--split-override",
        choices=("test", "challenge", "ai_gold_test", "ai_gold_challenge", "regression"),
    )
    parser.add_argument("--add-slice-tag", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    try:
        manifest = scope_evaluation_package(
            EvaluationPackagePart(args.records, args.source_manifest),
            namespace=args.namespace,
            prediction=args.kind == "prediction",
            candidate=args.candidate,
            split_override=args.split_override,
            add_slice_tags=args.add_slice_tag,
            destination=args.output,
            manifest_destination=args.manifest,
        )
    except (OSError, TypeError, ValueError, EvaluationBundleError) as error:
        print(f"evaluation_scope=failed reason={error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "case_id_namespace": manifest["case_id_namespace"],
                "candidate": manifest.get("candidate"),
                "kind": args.kind,
                "record_count": manifest["record_count"],
                "records_sha256": manifest["records_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
