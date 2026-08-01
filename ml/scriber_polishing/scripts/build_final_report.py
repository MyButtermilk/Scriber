"""Build immutable Section 49 JSON and Markdown from hash-bound evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from scriber_polishing.final_report import (  # noqa: E402
    CONTRACTS_DIRECTORY,
    FinalReportError,
    build_final_report,
    sha256_bytes,
    write_immutable_final_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compose the evidence-bound Scriber final report without network or "
            "repository mutations."
        )
    )
    parser.add_argument("--input", type=Path, required=True, help="Source-binding JSON")
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="Root for every relative evidence path in the input",
    )
    parser.add_argument(
        "--contracts-root",
        type=Path,
        default=CONTRACTS_DIRECTORY,
        help="Contract directory (defaults to the checked-in contracts)",
    )
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    parser.add_argument(
        "--allow-not-final",
        action="store_true",
        help="Write an explicitly NOT_FINAL report instead of failing on missing evidence",
    )
    return parser


def _load_input(path: Path) -> tuple[dict[str, object], bytes]:
    if path.is_symlink() or not path.is_file():
        raise FinalReportError(f"input is not a regular file: {path}")
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FinalReportError(f"input is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise FinalReportError("input must be a JSON object")
    return value, payload


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest, manifest_payload = _load_input(args.input)
        report = build_final_report(
            manifest,
            source_root=args.source_root,
            contracts_root=args.contracts_root,
            input_manifest_sha256=sha256_bytes(manifest_payload),
        )
        if report["report_state"] != "FINAL" and not args.allow_not_final:
            missing_sources = ",".join(report["missing_required_sources"]) or "-"
            missing_fields = ",".join(report["missing_required_fields"]) or "-"
            raise FinalReportError(
                "report is NOT_FINAL; "
                f"missing_sources={missing_sources}; missing_fields={missing_fields}"
            )
        json_hash, markdown_hash = write_immutable_final_report(
            report,
            json_path=args.output_json,
            markdown_path=args.output_markdown,
        )
    except (FinalReportError, OSError) as error:
        print(f"final_report=failed reason={error}", file=sys.stderr)
        return 2
    print(
        f"final_report={report['report_state'].lower()} "
        f"json_sha256={json_hash} markdown_sha256={markdown_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
