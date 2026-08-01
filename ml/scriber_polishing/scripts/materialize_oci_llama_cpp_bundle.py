"""Fetch, verify, and safely extract the reviewed llama.cpp b10156 OCI layers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.oci_llama_cpp_bundle import (  # noqa: E402
    OciBundleError,
    extract_verified_oci_layers,
    fetch_reviewed_llama_cpp_oci,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--report-output", type=Path)
    args = parser.parse_args(argv)
    try:
        fetched = fetch_reviewed_llama_cpp_oci(args.cache_dir)
        report: dict[str, object] = {"fetch": fetched}
        if not args.fetch_only:
            layers = [
                (
                    Path(path).read_bytes(),
                    "sha256:" + Path(path).name.removesuffix(".tar.gz"),
                )
                for path in fetched["layer_paths"]
            ]
            report["extraction"] = extract_verified_oci_layers(
                layers,
                args.rootfs,
            )
    except (OSError, ValueError, OciBundleError) as error:
        print(f"oci_llama_cpp_bundle=failed error={error}", file=sys.stderr)
        return 2
    rendered = (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if args.report_output is not None:
        if args.report_output.exists():
            raise ValueError(f"refusing to overwrite report: {args.report_output}")
        args.report_output.parent.mkdir(parents=True, exist_ok=True)
        args.report_output.write_text(
            rendered,
            encoding="utf-8",
            newline="\n",
        )
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
