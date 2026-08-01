"""Prepare and hash-bind private Hub upload folders without network access."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hub_publication import (  # noqa: E402
    PublicationError,
    load_publication_config,
    prepare_publication_bundle,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "publication.yaml")
    parser.add_argument("--model-artifact", type=Path, required=True)
    parser.add_argument("--dataset-artifact", type=Path, required=True)
    parser.add_argument("--completed-training-report", type=Path, required=True)
    parser.add_argument(
        "--selected-checkpoint-publication",
        type=Path,
        required=True,
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare_publication_bundle(
            load_publication_config(args.config),
            artifact_dirs={
                "model": args.model_artifact,
                "dataset": args.dataset_artifact,
            },
            completed_training_report_path=args.completed_training_report,
            selected_checkpoint_publication_path=(
                args.selected_checkpoint_publication
            ),
            report_path=args.report,
        )
    except (OSError, ValueError, PublicationError) as error:
        print(f"private_hub_preparation=failed reason={error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": result["status"],
                "bundle_sha256": result["bundle_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
