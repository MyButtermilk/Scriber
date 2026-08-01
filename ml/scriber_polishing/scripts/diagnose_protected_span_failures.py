"""Diagnose protected-placeholder failures without rerunning model inference."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.protected_span_diagnostics import (  # noqa: E402
    ProtectedSpanDiagnosticError,
    build_protected_span_diagnostic_report,
    write_protected_span_diagnostic_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--prediction-manifest", type=Path, required=True)
    parser.add_argument("--evaluation-report", type=Path, required=True)
    parser.add_argument("--training-data", type=Path, required=True)
    parser.add_argument("--validation-data", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fail-on-restoration-errors", action="store_true")
    args = parser.parse_args(argv)

    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            args.tokenizer.resolve(),
            local_files_only=True,
            trust_remote_code=False,
            fix_mistral_regex=False,
        )
        report = build_protected_span_diagnostic_report(
            references_path=args.references,
            predictions_path=args.predictions,
            prediction_manifest_path=args.prediction_manifest,
            evaluation_report_path=args.evaluation_report,
            training_path=args.training_data,
            validation_path=args.validation_data,
            tokenizer=tokenizer,
        )
        write_protected_span_diagnostic_report(report, args.output)
    except (OSError, TypeError, ValueError, ProtectedSpanDiagnosticError) as error:
        print(f"protected_span_diagnostic=failed reason={error}", file=sys.stderr)
        return 2

    failures = report["counts"]["inferred_restoration_failures"]
    print(
        "protected_span_diagnostic=pass "
        f"cases={report['counts']['evaluation_cases']} "
        f"restoration_failures={failures} "
        f"report={args.output.resolve()}"
    )
    if args.fail_on_restoration_errors and failures:
        print(
            "protected_span_diagnostic=red "
            f"restoration_failures={failures}",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
