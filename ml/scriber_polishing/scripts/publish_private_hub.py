"""Publish both Scriber artifacts privately and verify exact Hub revisions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hub_publication import (  # noqa: E402
    PublicationError,
    load_publication_config,
    publish_private_bundle,
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
    parser.add_argument(
        "--verification-root",
        "--fresh-download-root",
        dest="verification_root",
        type=Path,
        required=True,
        help="Fresh local root for isolated reload caches (and only in legacy mode, downloads).",
    )
    parser.add_argument(
        "--verification-mode",
        choices=("local_source_remote_identity", "fresh_exact_revision_download"),
        default="local_source_remote_identity",
        help="Default avoids downloading artifacts immediately after uploading them.",
    )
    parser.add_argument("--report", type=Path, required=True, help="Immutable JSON verification report.")
    parser.add_argument(
        "--report-markdown",
        type=Path,
        help="Immutable Markdown verification report (defaults next to --report).",
    )
    args = parser.parse_args()
    try:
        config = load_publication_config(args.config)
        token = os.environ.get(config["token_env"], "")
        result = publish_private_bundle(
            config,
            artifact_dirs={
                "model": args.model_artifact,
                "dataset": args.dataset_artifact,
            },
            completed_training_report_path=args.completed_training_report,
            selected_checkpoint_publication_path=(
                args.selected_checkpoint_publication
            ),
            download_root=args.verification_root,
            report_path=args.report,
            report_markdown_path=args.report_markdown,
            token=token,
            verification_mode=args.verification_mode,
        )
    except (OSError, ValueError, PublicationError) as error:
        print(f"private_hub_publication=failed reason={error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": result["status"],
                "model_revision": result["artifacts"]["model"]["revision"],
                "dataset_revision": result["artifacts"]["dataset"]["revision"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
