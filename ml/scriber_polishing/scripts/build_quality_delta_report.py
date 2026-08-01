"""Build one immutable BF16-relative quality delta report."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.quality_delta import (  # noqa: E402
    QualityDeltaError,
    build_quality_delta_report,
)


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QualityDeltaError(f"evaluation report must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument(
        "--candidate-variant",
        choices=("bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise QualityDeltaError(f"refusing to overwrite delta report: {args.output}")
        report = build_quality_delta_report(
            _load(args.baseline),
            _load(args.candidate),
            baseline_sha256=_sha256(args.baseline),
            candidate_sha256=_sha256(args.candidate),
            candidate_variant=args.candidate_variant,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(args.output)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, QualityDeltaError) as error:
        print(f"quality_delta=failed error={error}", file=sys.stderr)
        return 2
    print(
        f"quality_delta=complete variant={args.candidate_variant} "
        f"maximum_drop={report['maximum_absolute_metric_drop']} "
        f"new_critical_errors={report['new_critical_errors']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
