"""Verify a marker-repair bundle through the last pre-model-load boundary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from transformers import AutoTokenizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.marker_repair import (  # noqa: E402
    verify_marker_repair_bundle,
)
from scriber_polishing.model_factory import (  # noqa: E402
    BASE_MODEL_ID,
    BASE_MODEL_REVISION,
)
from scriber_polishing.training_integrity import (  # noqa: E402
    capture_tokenizer_identity,
)


def _load_tokenizer(tokenizer_path: Path | None) -> object:
    local_path: Path | None = None
    if tokenizer_path is None:
        tokenizer = AutoTokenizer.from_pretrained(
            BASE_MODEL_ID,
            revision=BASE_MODEL_REVISION,
            use_fast=True,
            fix_mistral_regex=False,
        )
    else:
        local_path = tokenizer_path.expanduser().resolve()
        if not local_path.is_dir():
            raise FileNotFoundError(
                f"local tokenizer directory does not exist: {local_path}"
            )
        tokenizer = AutoTokenizer.from_pretrained(
            local_path,
            use_fast=True,
            local_files_only=True,
            fix_mistral_regex=False,
        )
    capture_tokenizer_identity(
        tokenizer,
        model_id=BASE_MODEL_ID,
        revision=BASE_MODEL_REVISION,
        local_source_path=local_path,
    )
    return tokenizer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--tokenizer-path", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    tokenizer = _load_tokenizer(args.tokenizer_path)
    report = verify_marker_repair_bundle(
        args.bundle_dir,
        tokenizer=tokenizer,
    )
    payload = (
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if args.report is not None:
        report_path = args.report.expanduser().resolve()
        if report_path.exists():
            raise FileExistsError(
                f"refusing to overwrite verification report: {report_path}"
            )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(payload)
    print(payload.decode("utf-8").rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
